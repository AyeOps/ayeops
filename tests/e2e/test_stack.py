from __future__ import annotations

import base64
import calendar
import json
import os
import re
import socket
import ssl
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import warnings
from collections.abc import Callable, Iterator
from functools import cache
from pathlib import Path
from typing import Any

import pytest
import yaml

from ayeops.render import HOSTS_DOWN, NODE_UP
from tests.e2e.stack import (
    BACKUP_FILE,
    CA_FILE,
    MANIFEST,
    STATE,
    compose_args,
)

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(os.environ.get("E2E") != "1", reason="needs a running stack, set E2E=1"),
]

RENDER = Path(os.environ.get("E2E_RENDER", "/tmp/aye-e2e"))
WARM_UP = float(os.environ.get("E2E_WARM_UP", "300"))
BUDGET = time.monotonic() + float(os.environ.get("E2E_BUDGET", "480"))
GRACE = 30
SLOW_FOR = 3600
MACHINE_METRICS = ("node_filesystem_", "node_memory_", "node_vmstat_", "node_network_")


@cache
def state() -> dict[str, Any]:
    data: dict[str, Any] = json.loads((RENDER / STATE).read_text(encoding="utf-8"))
    return data


def deployed() -> list[str]:
    return [host for host, spec in state()["hosts"].items() if spec["deployed"]]


def central() -> str:
    return str(state()["central"])


def compose(host: str) -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load(
        (RENDER / host / "compose.yaml").read_text(encoding="utf-8")
    )
    return data


def published(host: str, service: str, port: int) -> str:
    loopback = state()["hosts"][host]["loopback"]
    for entry in compose(host)["services"][service].get("ports") or []:
        address, host_port, container_port = str(entry).rsplit(":", 2)
        if int(container_port) == port:
            bind = loopback if address == "127.0.0.1" else address
            return f"{bind}:{host_port}"
    raise AssertionError(f"{service} on {host} does not publish {port}")


def dotenv(host: str) -> dict[str, str]:
    lines = (RENDER / host / ".env").read_text(encoding="utf-8").splitlines()
    return dict(line.split("=", 1) for line in lines if "=" in line)


def eventually[T](check: Callable[[], T], timeout: float = WARM_UP, every: float = 5) -> T:
    now = time.monotonic()
    deadline = max(now + GRACE, min(now + timeout, BUDGET))
    while True:
        try:
            return check()
        except (AssertionError, OSError) as error:
            if time.monotonic() > deadline:
                raise AssertionError(f"still failing: {error}") from error
            time.sleep(every)


def fetch(url: str, headers: dict[str, str] | None = None, data: bytes | None = None) -> Any:
    request = urllib.request.Request(url, data=data, headers=headers or {})
    with urllib.request.urlopen(request, timeout=15) as response:
        body = response.read()
    return json.loads(body) if body else None


def vm(query: str) -> list[dict[str, Any]]:
    base = published(central(), "victoriametrics", 8428)
    url = f"http://{base}/api/v1/query?" + urllib.parse.urlencode({"query": query})
    result: list[dict[str, Any]] = fetch(url)["data"]["result"]
    return result


def values(query: str) -> list[float]:
    return [float(series["value"][1]) for series in vm(query)]


def grafana(path: str) -> Any:
    env = dotenv(central())
    token = f"{env['AYE_GRAFANA_ADMIN_USER']}:{env['AYE_GRAFANA_ADMIN_PASSWORD']}"
    auth = "Basic " + base64.b64encode(token.encode()).decode()
    base = published(central(), "grafana", 3000)
    return fetch(f"http://{base}{path}", {"Authorization": auth})


def rendered_yaml(relative: str) -> Any:
    return yaml.safe_load((RENDER / central() / relative).read_text(encoding="utf-8"))


def rendered_rules() -> list[dict[str, Any]]:
    data = rendered_yaml("grafana/provisioning/alerting/rules.yaml")
    return [rule for group in data["groups"] for rule in group["rules"]]


def seconds(duration: str) -> int:
    match = re.fullmatch(r"(\d+)([smhd])", duration)
    assert match, duration
    return int(match.group(1)) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[match.group(2)]


def ps(host: str) -> list[dict[str, Any]]:
    args = [*compose_args(RENDER, host, state()), "ps", "-a", "--format", "json"]
    output = subprocess.run(args, check=True, capture_output=True, text=True).stdout
    text = output.strip()
    if text.startswith("["):
        rows: list[dict[str, Any]] = json.loads(text)
        return rows
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def scrape_jobs() -> Iterator[tuple[str, str]]:
    for host in deployed():
        scrape = RENDER / host / "vmagent" / "scrape.yaml"
        if scrape.is_file():
            data = yaml.safe_load(scrape.read_text(encoding="utf-8"))
            for job in data["scrape_configs"]:
                yield host, str(job["job_name"])


def manifest_checks() -> list[tuple[str, str]]:
    data = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))
    return [
        (f"{service}-{name}", str(check["type"]))
        for service, spec in sorted((data.get("services") or {}).items())
        for name, check in sorted((spec.get("checks") or {}).items())
    ]


def test_every_rendered_service_runs_and_is_healthy() -> None:
    for host in deployed():
        rendered = set(compose(host)["services"])

        def healthy(host: str = host, rendered: set[str] = rendered) -> None:
            rows = {row["Service"]: row for row in ps(host)}
            assert rendered <= set(rows), f"{host}: not created {sorted(rendered - set(rows))}"
            bad = {
                name: f"{row['State']}/{row.get('Health') or '-'}"
                for name, row in rows.items()
                if row["State"] != "running" or row.get("Health") not in ("", None, "healthy")
            }
            assert not bad, f"{host}: {bad}"

        eventually(healthy, timeout=120)


def test_every_rendered_scrape_job_is_up() -> None:
    jobs = sorted(set(scrape_jobs()))
    assert jobs

    def all_up() -> None:
        problems = {}
        for host, job in jobs:
            series = values(f'up{{host="{host}",job="{job}"}}')
            if not series or min(series) < 1:
                problems[f"{host}/{job}"] = series
        assert not problems, f"up != 1: {problems}"

    eventually(all_up)


def test_probes_and_checks_succeed() -> None:
    probes = rendered_yaml("vmagent/scrape.yaml")["scrape_configs"]
    targets = [
        target
        for job in probes
        if job["job_name"] == "blackbox-http"
        for group in job["static_configs"]
        for target in group["targets"]
    ]
    checks = manifest_checks()
    assert targets and checks

    def passing() -> None:
        problems = {}
        for target in targets:
            found = values(f'probe_success{{job="blackbox-http",instance="{target}"}}')
            if found != [1.0]:
                problems[target] = found
        for check, kind in checks:
            metric = "ayeops_check_sql_ok" if kind == "sql" else "probe_success"
            found = values(f'{metric}{{check="{check}"}}')
            if found != [1.0]:
                problems[check] = found
        assert not problems, f"failing: {problems}"

    eventually(passing)
    for check, kind in checks:
        if kind == "sql":
            assert values(f'ayeops_check_sql_value{{check="{check}"}}'), check


def test_grafana_datasources_are_provisioned_and_healthy() -> None:
    directory = RENDER / central() / "grafana" / "provisioning" / "datasources"
    expected = {
        source["uid"]
        for path in directory.glob("*.yaml")
        for source in yaml.safe_load(path.read_text(encoding="utf-8"))["datasources"]
    }
    actual = {source["uid"] for source in grafana("/api/datasources")}
    assert actual == expected

    def healthy() -> None:
        unhealthy = {}
        for uid in sorted(expected):
            try:
                answer = grafana(f"/api/datasources/uid/{uid}/health")
            except urllib.error.HTTPError as error:
                answer = {"status": error.code, "message": error.read().decode()[:200]}
            if answer.get("status") != "OK":
                unhealthy[uid] = answer
        assert not unhealthy, unhealthy

    eventually(healthy, timeout=120)


def test_grafana_alert_rules_match_the_render() -> None:
    expected = {rule["uid"] for rule in rendered_rules()}
    actual = [rule["uid"] for rule in grafana("/api/v1/provisioning/alert-rules")]
    assert len(actual) == len(expected)
    assert set(actual) == expected


def reader_call(path: str, method: str = "GET", body: Any = None) -> tuple[int, Any]:
    exec_args = [*compose_args(RENDER, central(), state()), "exec", "-T", "grafana-reader"]
    done = subprocess.run(
        [*exec_args, "cat", "/tmp/token"], check=True, capture_output=True, text=True
    )
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(
        f"http://{published(central(), 'grafana', 3000)}{path}",
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {done.stdout.strip()}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return int(response.status), json.loads(response.read() or b"null")
    except urllib.error.HTTPError as error:
        return int(error.code), None


def test_grafana_reader_token_reads_what_the_agents_read_and_changes_nothing() -> None:
    def reported() -> None:
        series = vm("ayeops_grafana_reader_endpoint_ok")
        ok = {s["metric"]["endpoint"]: s["value"][1] for s in series}
        assert ok == {"rules": "1", "silences": "1", "alerts": "1", "annotations": "1"}, ok

    eventually(reported)
    status, rules = reader_call("/api/prometheus/grafana/api/v1/rules")
    assert status == 200
    assert sum(len(g["rules"]) for g in rules["data"]["groups"]) == len(rendered_rules())
    for path in (
        "/api/alertmanager/grafana/api/v2/silences",
        "/api/alertmanager/grafana/api/v2/alerts",
        "/api/annotations?type=alert&limit=10",
    ):
        assert reader_call(path)[0] == 200, path
    now = time.time()
    silence = {
        "matchers": [{"name": "alertname", "value": "e2e", "isRegex": False, "isEqual": True}],
        "startsAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
        "endsAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now + 600)),
        "createdBy": "e2e",
        "comment": "the reader must not be able to silence",
    }
    assert reader_call("/api/alertmanager/grafana/api/v2/silences", "POST", silence)[0] == 403
    assert reader_call("/api/serviceaccounts/search")[0] == 403
    assert reader_call("/api/admin/settings")[0] == 403


def test_grafana_dashboards_match_the_render() -> None:
    folders = ("grafana/dashboards", "grafana/project-dashboards")
    expected = {
        json.loads(path.read_text(encoding="utf-8"))["uid"]
        for folder in folders
        for path in (RENDER / central() / folder).glob("*.json")
    }
    actual = [item["uid"] for item in grafana("/api/search?type=dash-db&limit=5000")]
    assert len(actual) == len(expected)
    assert set(actual) == expected


def loki(query: str, since: float) -> list[str]:
    base = published(central(), "loki", 3100)
    params = {"query": query, "start": str(int(since * 1e9)), "limit": "50"}
    url = f"http://{base}/loki/api/v1/query_range?" + urllib.parse.urlencode(params)
    streams = fetch(url)["data"]["result"]
    return [line for stream in streams for _, line in stream["values"]]


def test_loki_returns_a_pushed_line() -> None:
    started = time.time() - 60
    token = f"aye-e2e-{os.urandom(6).hex()}"
    body = {"streams": [{"stream": {"job": "aye-e2e"}, "values": [[str(time.time_ns()), token]]}]}
    base = published(central(), "loki", 3100)
    fetch(
        f"http://{base}/loki/api/v1/push",
        {"Content-Type": "application/json"},
        json.dumps(body).encode(),
    )

    def found() -> None:
        assert token in loki(f'{{job="aye-e2e"}} |= "{token}"', started)

    eventually(found, timeout=60)


def test_loki_receives_container_logs_from_every_host() -> None:
    since = time.time() - 3600
    hosts = [host for host in deployed() if "alloy" in compose(host)["services"]]
    assert hosts

    def shipped() -> None:
        missing = [host for host in hosts if not loki(f'{{host="{host}"}}', since)]
        assert not missing, f"no log lines from {missing}"

    eventually(shipped, timeout=120)


def offsite_host() -> str:
    hosts = [host for host in deployed() if "offsite" in compose(host)["services"]]
    assert len(hosts) == 1
    return hosts[0]


def test_offsite_backs_up_and_restores_the_file() -> None:
    host = offsite_host()

    def reported() -> None:
        metrics = {
            name: values(f'ayeops_offsite_{name}{{host="{host}"}}')
            for name in ("last_run_success", "snapshots", "restore_test_ok", "repository_ok")
        }
        assert metrics["last_run_success"] == [1.0], metrics
        assert metrics["snapshots"] and metrics["snapshots"][0] >= 1, metrics
        assert metrics["restore_test_ok"] == [1.0], metrics
        assert metrics["repository_ok"] == [1.0], metrics

    eventually(reported)
    args = [*compose_args(RENDER, host, state()), "exec", "-T", "offsite"]
    restic = [*args, "/opt/restic/restic", "dump", "latest", str(BACKUP_FILE)]
    restored = subprocess.run(restic, check=True, capture_output=True).stdout
    assert restored == BACKUP_FILE.read_bytes()


def docker_images() -> list[str]:
    output = subprocess.run(
        ["docker", "image", "ls", "-a", "-q", "--no-trunc"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return sorted(output.split())


def test_hygiene_dry_run_reports_and_removes_nothing() -> None:
    hosts = [host for host in deployed() if "hygiene" in compose(host)["services"]]
    assert hosts
    for host in hosts:
        assert compose(host)["services"]["hygiene"]["environment"]["AYE_HYGIENE_DRY_RUN"] == "true"
        before = docker_images()
        started = time.time()
        args = [*compose_args(RENDER, host, state()), "exec", "-T", "-e", "AYE_HYGIENE_ONCE=1"]
        subprocess.run([*args, "hygiene", "python", "-I", "/app/hygiene_runner.py"], check=True)
        assert docker_images() == before

        def reported(host: str = host, started: float = started) -> None:
            assert values(f'ayeops_hygiene_dry_run{{host="{host}"}}') == [1.0]
            last = values(f'ayeops_hygiene_last_success_timestamp_seconds{{host="{host}"}}')
            assert last and last[0] >= started - 1, last

        eventually(reported, timeout=120)


def test_portal_pages_answer_and_list_every_service() -> None:
    pages = sorted(path.name for path in (RENDER / central() / "portal").glob("*.html"))
    assert "services.html" in pages
    base = published(central(), "portal", 8080)
    bodies = {}
    for page in pages:
        with urllib.request.urlopen(f"http://{base}/{page}", timeout=15) as response:
            assert response.status == 200, page
            bodies[page] = response.read().decode("utf-8")
    names = {name for host in state()["hosts"] for name in compose(host)["services"]}
    names |= set(yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))["services"])
    missing = sorted(name for name in names if name not in bodies["services.html"])
    assert not missing


def https_status(address: str, hostname: str, ca: Path) -> tuple[int, str]:
    context = ssl.create_default_context(cafile=str(ca))
    host, port = address.rsplit(":", 1)
    with (
        socket.create_connection((host, int(port)), timeout=15) as raw,
        context.wrap_socket(raw, server_hostname=hostname) as tls,
    ):
        tls.sendall(f"GET / HTTP/1.1\r\nHost: {hostname}\r\nConnection: close\r\n\r\n".encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = tls.recv(4096)
            if not chunk:
                break
            head += chunk
    lines = head.decode("latin-1").split("\r\n")
    location = next(
        (line.split(":", 1)[1].strip() for line in lines if line.lower().startswith("location:")),
        "",
    )
    return int(lines[0].split()[1]), location


def test_ingress_serves_every_route_and_gates_the_private_ones() -> None:
    hosts = [host for host in deployed() if state()["hosts"][host]["domain"]]
    assert hosts
    for host in hosts:
        domain = state()["hosts"][host]["domain"]
        address = published(host, "traefik", 443)
        ca = RENDER / host / CA_FILE
        routes = yaml.safe_load(
            (RENDER / host / "ingress" / "dynamic" / "routes.yml").read_text(encoding="utf-8")
        )["http"]["routers"]
        for name, router in routes.items():
            if "_" in name:
                continue
            hostname = f"{name}.{domain}"

            def answer(
                address: str = address, hostname: str = hostname, ca: Path = ca
            ) -> tuple[int, str]:
                return https_status(address, hostname, ca)

            status, location = eventually(answer, timeout=60)
            if "sso" in (router.get("middlewares") or []):
                assert status in (302, 303, 307), (name, status)
                assert "accounts.google.com" in location or "/oauth2/" in location, (
                    name,
                    location,
                )
            else:
                assert status < 400, (name, status, location)
        assert https_status(address, f"no-such-route.{domain}", ca)[0] == 404


def utc(stamp: str) -> float:
    return calendar.timegm(time.strptime(stamp[:19], "%Y-%m-%dT%H:%M:%S"))


def test_no_alert_fires_on_a_healthy_stack() -> None:
    rules = rendered_rules()
    hold = {rule["title"]: seconds(rule["for"]) for rule in rules}
    machine = {
        rule["title"]
        for rule in rules
        if any(metric in rule["data"][0]["model"]["expr"] for metric in MACHINE_METRICS)
    }
    started = time.time()

    def evaluated() -> list[dict[str, Any]]:
        groups = grafana("/api/prometheus/grafana/api/v1/rules")["data"]["groups"]
        states = [rule for group in groups for rule in group["rules"]]
        assert len(states) == len(rules), f"{len(states)} rules loaded, {len(rules)} rendered"
        stale = [rule["name"] for rule in states if utc(rule["lastEvaluation"]) < started]
        assert not stale, f"not evaluated since the warm-up: {stale}"
        return states

    states = eventually(evaluated, timeout=150)
    wrong = {
        rule["name"]: f"{rule['state']}/{rule['health']}: {rule.get('lastError', '')}"
        for rule in states
        if rule["health"] == "error"
        or rule["state"] in ("firing", "recovering")
        or (
            rule["state"] == "pending"
            and hold.get(rule["name"], 0) < SLOW_FOR
            and rule["name"] not in machine
        )
    }
    assert not wrong, f"alerts on a healthy stack: {wrong}"
    for rule in states:
        if rule["state"] == "pending":
            warnings.warn(f"pending, judged by the runner itself: {rule['name']}", stacklevel=1)


def labelsets(query: str) -> dict[tuple[tuple[str, str], ...], float]:
    return {
        tuple(sorted(series["metric"].items())): float(series["value"][1]) for series in vm(query)
    }


def test_a_down_host_holds_back_its_other_alerts_in_victoriametrics() -> None:
    rules = {rule["uid"]: rule for rule in rendered_rules()}
    held = str(rules["aye-target-down"]["data"][0]["model"]["expr"]).replace("$$", "$")
    inner = "up == bool 0"
    assert held == f"({inner}) unless on (host) ({HOSTS_DOWN}) or 0 * ({inner})"
    assert vm(HOSTS_DOWN) == []
    assert labelsets(held) == labelsets(inner)
    nodes = {series["metric"]["host"] for series in vm(f"max by (host) ({NODE_UP})")}
    assert nodes
    every_node_down = held.replace(HOSTS_DOWN, f"max by (host) ({NODE_UP}) >= 0")
    down = labelsets(every_node_down)
    assert set(down) == set(labelsets(inner))
    held_hosts = {value for labels, value in down.items() if dict(labels).get("host") in nodes}
    assert held_hosts == {0.0}
