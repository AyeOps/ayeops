from __future__ import annotations

import json
import shlex
from dataclasses import dataclass
from typing import Any

from ayeops.baseline import expand, is_baseline
from ayeops.checks_sql import SQL_END, sql_condition, sql_failed, sql_query_problem, sql_text
from ayeops.manifest import Manifest

DEFAULT_INTERVAL = 60
DEFAULT_TIMEOUT = 10
DEFAULT_FOR = "5m"
BLACKBOX = "check-blackbox:9116"
BLACKBOX_TYPES = ("http", "tcp", "dns")
SCRAPE_SLACK = 60

Problem = tuple[str, str, str]


@dataclass(frozen=True)
class Check:
    service: str
    name: str
    spec: dict[str, Any]
    host: str

    @property
    def id(self) -> str:
        return f"{self.service}-{self.name}"

    @property
    def type(self) -> str:
        return str(self.spec["type"])

    @property
    def path(self) -> str:
        return f"services.{self.service}.checks.{self.name}"

    @property
    def interval(self) -> int:
        return int(self.spec.get("interval", DEFAULT_INTERVAL))

    @property
    def timeout(self) -> int:
        return int(self.spec.get("timeout", DEFAULT_TIMEOUT))

    @property
    def via_proxy(self) -> str:
        return str(self.spec.get("via_proxy") or "")

    @property
    def backend(self) -> str:
        if self.via_proxy:
            return "proxy"
        if self.type == "script":
            return "script-net" if self.spec.get("network") else "script"
        if self.type == "sql":
            return "sql"
        return "blackbox"

    @property
    def blackbox_module(self) -> str:
        return "check_" + self.id.replace("-", "_")

    @property
    def target(self) -> str:
        if self.type == "http":
            return str(self.spec["url"])
        if self.type == "tcp":
            return str(self.spec["address"])
        if self.type == "dns":
            return str(self.spec["name"])
        if self.type == "sql":
            return f"datasource {self.spec['datasource']}"
        return "script"


def service_checks(manifest: Manifest) -> list[Check]:
    checks = []
    for service_name, service in sorted(manifest.active_services.items()):
        for name, spec in sorted((service.get("checks") or {}).items()):
            host = str(spec.get("from") or service.get("host") or "")
            checks.append(Check(service_name, name, dict(spec), host))
    return checks


def checks_from(manifest: Manifest, host: str) -> list[Check]:
    return [check for check in service_checks(manifest) if check.host == host]


def stale_after(check: Check) -> int:
    return 2 * check.interval + check.timeout + SCRAPE_SLACK


def sql_expr(check: Check) -> str:
    alert = check.spec.get("alert") or {}
    if is_baseline(alert):
        return sql_failed(check.id, stale_after(check))
    return sql_condition(
        check.id,
        str(alert["op"]),
        alert["threshold"],
        str(alert.get("no_data", "ok")),
        stale_after(check),
    )


def success_expr(check: Check) -> str:
    selector = f'{{check="{check.id}"}}'
    if check.backend == "sql":
        return sql_expr(check)
    if check.backend == "proxy":
        return f"ayeops_proxy_target_success{selector} == bool 0"
    if check.backend.startswith("script"):
        return (
            f"1 - (ayeops_check_success{selector} * "
            f"(time() - ayeops_check_last_run_timestamp_seconds{selector} "
            f"< bool {stale_after(check)}))"
        )
    return f"probe_success{selector} == bool 0"


def check_alert(check: Check) -> dict[str, Any]:
    alert = {
        key: value
        for key, value in (check.spec.get("alert") or {}).items()
        if key not in ("op", "threshold", "no_data", "baseline")
    }
    what = check.target if check.type != "script" else "its script"
    alert.setdefault("title", f"Check {check.id} failing")
    alert.setdefault("summary", f"{check.service}: check {check.name} on {what} fails")
    alert["name"] = str(alert.get("name") or check.id)
    alert["expr"] = success_expr(check)
    alert.setdefault("for", DEFAULT_FOR)
    return alert


def sql_baseline(check: Check) -> dict[str, Any] | None:
    alert = check.spec.get("alert") or {}
    if check.type != "sql" or not is_baseline(alert):
        return None
    label = check.spec.get("label")
    return {
        **alert["baseline"],
        "metric": f'ayeops_check_sql_value{{check="{check.id}"}}',
        "kind": "gauge",
        "by": [label] if label else [],
    }


def check_alerts(check: Check) -> list[dict[str, Any]]:
    alert = check_alert(check)
    spec = sql_baseline(check)
    if spec is None:
        return [alert]
    raw = dict(check.spec.get("alert") or {})
    base = alert["name"]
    return [alert, *expand({**raw, "name": base}, spec, base, f"{base}-low")]


def manifest_alert(alert: dict[str, Any]) -> list[dict[str, Any]]:
    if is_baseline(alert):
        return expand(alert, alert["baseline"], alert["name"], alert["name"])
    return [dict(alert)]


def baseline_sources(manifest: Manifest) -> list[tuple[str, dict[str, Any]]]:
    sources = [
        (str(alert["name"]), dict(alert["baseline"]))
        for _, service in sorted(manifest.active_services.items())
        for alert in service.get("alerts") or []
        if is_baseline(alert)
    ]
    for check in service_checks(manifest):
        spec = sql_baseline(check)
        if spec is not None:
            sources.append((f"{check_alert(check)['name']}-low", spec))
    return sources


def service_alerts(manifest: Manifest, name: str, service: dict[str, Any]) -> list[dict[str, Any]]:
    own = [rule for alert in service.get("alerts") or [] for rule in manifest_alert(alert)]
    return own + [
        rule for c in service_checks(manifest) if c.service == name for rule in check_alerts(c)
    ]


def http_module(check: Check, prober: dict[str, Any]) -> dict[str, Any]:
    spec = check.spec
    http: dict[str, Any] = {
        "method": spec.get("method", "GET"),
        "preferred_ip_protocol": "ip4",
        "follow_redirects": True,
    }
    expected = spec.get("expect_status")
    if expected is not None:
        http["valid_status_codes"] = expected if isinstance(expected, list) else [expected]
    if spec.get("expect_body"):
        http["fail_if_body_not_matches_regexp"] = [spec["expect_body"]]
    prober["http"] = http
    return prober


def dns_module(check: Check, prober: dict[str, Any]) -> dict[str, Any]:
    dns: dict[str, Any] = {
        "query_name": check.spec["name"],
        "query_type": check.spec.get("record_type", "A"),
        "preferred_ip_protocol": "ip4",
        "valid_rcodes": ["NOERROR"],
    }
    if check.spec.get("expect"):
        dns["validate_answer_rrs"] = {"fail_if_none_matches_regexp": [check.spec["expect"]]}
    prober["dns"] = dns
    return prober


def blackbox_module(check: Check) -> dict[str, Any]:
    prober: dict[str, Any] = {"prober": check.type, "timeout": f"{check.timeout}s"}
    if check.type == "http":
        return http_module(check, prober)
    if check.type == "dns":
        return dns_module(check, prober)
    prober["tcp"] = {"preferred_ip_protocol": "ip4"}
    return prober


def blackbox_target(check: Check, settings: dict[str, Any]) -> str:
    if check.type == "dns":
        return str(check.spec.get("server") or settings.get("dns_server", "1.1.1.1"))
    return check.target


def blackbox_job(check: Check, settings: dict[str, Any]) -> dict[str, Any]:
    return {
        "job_name": f"check-{check.id}",
        "metrics_path": "/probe",
        "scrape_interval": f"{check.interval}s",
        "scrape_timeout": f"{min(check.timeout + 1, check.interval)}s",
        "params": {"module": [check.blackbox_module], "target": [blackbox_target(check, settings)]},
        "static_configs": [
            {"targets": [BLACKBOX], "labels": {"check": check.id, "service": check.service}}
        ],
    }


def script_entry(check: Check) -> dict[str, Any]:
    return {
        "check": check.id,
        "service": check.service,
        "command": check.spec["command"],
        "interval": check.interval,
        "timeout": check.timeout,
        "env": list(check.spec.get("env") or []),
    }


def script_group(checks: list[Check], backend: str) -> dict[str, Any]:
    chosen = [check for check in checks if check.backend == backend]
    mounts = sorted({f"{mount}:ro" for c in chosen for mount in c.spec.get("mounts") or []})
    env = sorted({name for c in chosen for name in c.spec.get("env") or []})
    return {
        "checks": [script_entry(check) for check in chosen],
        "mounts": mounts,
        "env": env,
    }


def sql_file(check: Check) -> str:
    label = check.spec.get("label")
    return sql_text(str(check.spec["query"]), str(label) if label else None, check.timeout)


def datasources(manifest: Manifest) -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    for host in sorted(manifest.hosts):
        grafana = manifest.host_modules(host).get("grafana") or {}
        for source in grafana.get("datasources") or []:
            found.setdefault(str(source["uid"]), dict(source))
    return found


def sql_source(manifest: Manifest, check: Check) -> dict[str, Any] | None:
    source = datasources(manifest).get(str(check.spec.get("datasource")))
    if source is None or source.get("type") != "postgres":
        return None
    return source


def sql_line(check: Check, source: dict[str, Any]) -> str:
    address, port = str(source["url"]).rsplit(":", 1)
    words = [
        "check",
        check.id,
        check.service,
        str(check.interval),
        str(check.timeout),
        address,
        port,
        str(source["database"]),
        str(source.get("user") or ""),
        str(source.get("password_env") or "AYE_NO_PASSWORD"),
        "require" if source.get("tls") else "prefer",
    ]
    command = " ".join(shlex.quote(word) for word in words)
    return f"{command} <<'{SQL_END}'\n{sql_file(check)}{SQL_END}\n"


def sql_group(manifest: Manifest, checks: list[Check]) -> dict[str, Any]:
    chosen = [
        (check, source)
        for check in checks
        if check.backend == "sql"
        for source in [sql_source(manifest, check)]
        if source is not None
    ]
    env = sorted({str(s["password_env"]) for _, s in chosen if s.get("password_env")})
    return {
        "checks": [check.id for check, _ in chosen],
        "env": env,
        "config": "".join(sql_line(check, source) for check, source in chosen),
    }


def checks_context(manifest: Manifest, host: str, settings: dict[str, Any]) -> dict[str, Any]:
    checks = checks_from(manifest, host)
    probes = [check for check in checks if check.backend == "blackbox"]
    scripts = script_group(checks, "script")
    scripts_net = script_group(checks, "script-net")
    sql = sql_group(manifest, checks)
    return {
        "probes": [check.id for check in probes],
        "blackbox": {"modules": {c.blackbox_module: blackbox_module(c) for c in probes}},
        "jobs": [blackbox_job(check, settings) for check in probes],
        "scripts": scripts,
        "scripts_net": scripts_net,
        "scripts_json": dump_config(scripts["checks"]),
        "scripts_net_json": dump_config(scripts_net["checks"]),
        "runner": bool(scripts["checks"] or scripts_net["checks"]),
        "proxied": [check for check in checks if check.backend == "proxy"],
        "sql": sql,
        "textfile": bool(scripts["checks"] or scripts_net["checks"] or sql["checks"]),
    }


def dump_config(data: Any) -> str:
    return json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False)


def check_problems(manifest: Manifest) -> list[Problem]:
    problems: list[Problem] = []
    for check in service_checks(manifest):
        path = check.path
        if check.host not in manifest.hosts:
            problems.append((path, "check-host", f"unknown host {check.host!r}"))
            continue
        enabled = set(manifest.host_modules(check.host))
        if "checks" not in enabled and check.backend != "proxy":
            problems.append(
                (
                    path,
                    "check-module",
                    f"enable the checks module on {check.host}: modules: {{checks: {{}}}}",
                )
            )
        if check.backend.startswith("script") and "node" not in enabled:
            problems.append(
                (path, "check-script-node", f"script checks report through node on {check.host}")
            )
        if check.backend == "sql":
            problems.extend(sql_problems(manifest, check, enabled))
        if check.timeout >= check.interval:
            problems.append(
                (
                    path,
                    "check-timing",
                    f"timeout {check.timeout}s must be shorter than interval {check.interval}s",
                )
            )
    return problems


def sql_problems(manifest: Manifest, check: Check, enabled: set[str]) -> list[Problem]:
    path = check.path
    problems: list[Problem] = []
    if "node" not in enabled:
        problems.append(
            (path, "check-script-node", f"sql checks report through node on {check.host}")
        )
    if sql_source(manifest, check) is None:
        problems.append(
            (
                path,
                "check-sql-datasource",
                f"no postgres datasource with uid {check.spec.get('datasource')!r} "
                "under modules.grafana.datasources of any host",
            )
        )
    problem = sql_query_problem(str(check.spec.get("query") or ""))
    if problem:
        problems.append((path, "check-sql-query", problem))
    return problems
