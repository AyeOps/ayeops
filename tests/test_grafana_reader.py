from __future__ import annotations

import base64
import importlib.util
import json
import shutil
import stat
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

from ayeops import grafana_reader as convention
from ayeops.registry import Module
from tests.conftest import ROOT, manifest_from, render_once, rules_hit

SCRIPT = ROOT / "src" / "ayeops" / "modules" / "grafana" / "files" / "grafana_reader.py"
FIXTURE = ROOT / "tests" / "fixtures" / "modules"
ADMIN = "Basic " + base64.b64encode(b"admin:secret-admin").decode()


def load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("grafana_reader_script", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["grafana_reader_script"] = module
    spec.loader.exec_module(module)
    return module


reader = load_script()


class FakeGrafana:
    def __init__(self) -> None:
        self.accounts: dict[int, dict[str, Any]] = {}
        self.tokens: dict[int, dict[int, str]] = {}
        self.next_id = 1
        self.rules = 3
        self.reader_rules = 3
        self.annotations = 5
        self.forbidden: set[str] = set()
        self.down = False
        self.dashboards: dict[str, Any] = {}
        self.dashboard_status = 200
        self.calls: list[tuple[str, str]] = []

    def new_id(self) -> int:
        self.next_id += 1
        return self.next_id

    def issued(self) -> set[str]:
        return {key for tokens in self.tokens.values() for key in tokens.values()}

    def __call__(self, request: urllib.request.Request, timeout: float) -> tuple[int, bytes]:
        if self.down:
            raise urllib.error.URLError("Connection refused")
        method = request.get_method()
        url = urllib.parse.urlsplit(request.full_url)
        self.calls.append((method, url.path))
        auth = request.get_header("Authorization") or ""
        body = json.loads(request.data) if isinstance(request.data, bytes) else None
        if auth == ADMIN:
            return self.as_admin(method, url.path, url.query, body)
        if auth.startswith("Bearer ") and auth.removeprefix("Bearer ") in self.issued():
            return self.as_reader(url.path)
        return 401, b'{"message":"invalid API key"}'

    def endpoint(self, path: str, rules: int) -> tuple[int, bytes]:
        if path.endswith("/v1/rules"):
            groups = [{"name": "g", "rules": [{"name": f"r{i}"} for i in range(rules)]}]
            return 200, json.dumps({"status": "success", "data": {"groups": groups}}).encode()
        if path.endswith("/annotations"):
            return 200, json.dumps([{"id": i} for i in range(self.annotations)]).encode()
        return 200, b"[]"

    def as_reader(self, path: str) -> tuple[int, bytes]:
        for name, endpoint in reader.ENDPOINTS.items():
            if endpoint.split("?")[0] == path and name in self.forbidden:
                return 403, b'{"message":"permissions needed"}'
        if path.startswith("/api/serviceaccounts") or path.startswith("/api/datasources"):
            return 403, b'{"message":"permissions needed"}'
        return self.endpoint(path, self.reader_rules)

    def as_admin(self, method: str, path: str, query: str, body: Any) -> tuple[int, bytes]:
        parts = path.strip("/").split("/")
        if path == "/api/search":
            asked = urllib.parse.parse_qs(query)
            limit, page = int(asked["limit"][0]), int(asked["page"][0])
            hits = [{"uid": uid, "type": "dash-db"} for uid in self.dashboards]
            return 200, json.dumps(hits[(page - 1) * limit : page * limit]).encode()
        if path.startswith("/api/dashboards/uid/"):
            uid = urllib.parse.unquote(path.removeprefix("/api/dashboards/uid/"))
            meta = {"provisioned": self.dashboards[uid]}
            board: dict[str, Any] = {"meta": meta, "dashboard": {"uid": uid}}
            return self.dashboard_status, json.dumps(board).encode()
        if path == "/api/serviceaccounts/search":
            name = urllib.parse.parse_qs(query)["query"][0]
            found = [a for a in self.accounts.values() if name in a["name"]]
            return 200, json.dumps({"serviceAccounts": found}).encode()
        if method == "POST" and path == "/api/serviceaccounts":
            account = {"id": self.new_id(), "isDisabled": False, **body}
            self.accounts[account["id"]] = account
            self.tokens[account["id"]] = {}
            return 201, json.dumps(account).encode()
        if parts[:2] == ["api", "serviceaccounts"] and len(parts) == 3 and method == "PATCH":
            self.accounts[int(parts[2])].update(body)
            return 200, json.dumps(self.accounts[int(parts[2])]).encode()
        if parts[:2] == ["api", "serviceaccounts"] and parts[3:4] == ["tokens"]:
            tokens = self.tokens[int(parts[2])]
            if method == "POST":
                token_id = self.new_id()
                tokens[token_id] = f"glsa_secret_{token_id}"
                answer = {"id": token_id, "name": body["name"], "key": tokens[token_id]}
                return 200, json.dumps(answer).encode()
            if method == "GET":
                listed = [{"id": token_id, "name": "t"} for token_id in tokens]
                return 200, json.dumps(listed).encode()
            if method == "DELETE":
                del tokens[int(parts[4])]
                return 200, b'{"message":"deleted"}'
        return self.endpoint(path, self.rules)


def plain_writer(path: Path, token: str) -> None:
    path.write_text(f"{token}\n", encoding="utf-8")


def reconciler(grafana: FakeGrafana, tmp_path: Path) -> Any:
    client = reader.Grafana("http://grafana:3000", grafana)
    clock = iter(range(1_700_000_000, 1_800_000_000, 300))
    return reader.Reconciler(
        client, ADMIN, tmp_path / "token", clock=lambda: float(next(clock)), writer=plain_writer
    )


def metric(text: str, name: str) -> str:
    for line in text.splitlines():
        if line.startswith(f"{reader.PREFIX}{name} "):
            return line.rsplit(" ", 1)[1]
    raise AssertionError(f"{name} not in {text}")


def test_a_first_pass_creates_a_viewer_account_and_writes_its_token(tmp_path: Path) -> None:
    grafana = FakeGrafana()
    rec = reconciler(grafana, tmp_path)
    assert not rec.ready()
    report = rec.refresh()
    assert report.ok, report.error
    [account] = grafana.accounts.values()
    assert (account["name"], account["role"]) == ("aye-reader", "Viewer")
    assert (tmp_path / "token").read_text(encoding="utf-8").strip() in grafana.issued()
    assert rec.ready()
    text = rec.metrics()
    assert metric(text, "ok") == "1"
    assert metric(text, "rotations_total") == "1"
    assert metric(text, "last_success_timestamp_seconds") == "1700000000"
    for endpoint in reader.ENDPOINTS:
        assert f'{reader.PREFIX}endpoint_ok{{endpoint="{endpoint}"}} 1' in text


def test_a_working_token_is_kept_and_no_new_token_is_made(tmp_path: Path) -> None:
    grafana = FakeGrafana()
    rec = reconciler(grafana, tmp_path)
    rec.refresh()
    before = (tmp_path / "token").read_text(encoding="utf-8")
    keys = grafana.issued()
    assert rec.refresh().ok
    assert (tmp_path / "token").read_text(encoding="utf-8") == before
    assert grafana.issued() == keys
    assert metric(rec.metrics(), "rotations_total") == "1"
    assert grafana.calls.count(("POST", "/api/serviceaccounts")) == 1


def test_a_refused_token_is_replaced_and_every_other_token_deleted(tmp_path: Path) -> None:
    grafana = FakeGrafana()
    rec = reconciler(grafana, tmp_path)
    rec.refresh()
    [account] = grafana.tokens
    grafana.tokens[account][999] = "glsa_leaked"
    for token_id in [t for t in grafana.tokens[account] if t != 999]:
        del grafana.tokens[account][token_id]
    assert rec.refresh().ok
    written = (tmp_path / "token").read_text(encoding="utf-8").strip()
    assert list(grafana.tokens[account].values()) == [written]
    assert metric(rec.metrics(), "rotations_total") == "2"


def test_a_missing_file_gets_a_new_token(tmp_path: Path) -> None:
    grafana = FakeGrafana()
    rec = reconciler(grafana, tmp_path)
    rec.refresh()
    (tmp_path / "token").unlink()
    assert rec.refresh().ok
    assert (tmp_path / "token").exists()
    assert len(grafana.issued()) == 1


def test_an_account_raised_above_viewer_or_disabled_is_set_back(tmp_path: Path) -> None:
    grafana = FakeGrafana()
    rec = reconciler(grafana, tmp_path)
    rec.refresh()
    [account] = grafana.accounts.values()
    account.update(role="Admin", isDisabled=True)
    assert rec.refresh().ok
    assert (account["role"], account["isDisabled"]) == ("Viewer", False)


def test_a_forbidden_endpoint_names_the_role_and_rotates_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    grafana = FakeGrafana()
    rec = reconciler(grafana, tmp_path)
    rec.refresh()
    grafana.forbidden = {"silences"}
    report = rec.refresh()
    assert not report.ok
    assert report.endpoints == {
        "rules": True,
        "silences": False,
        "alerts": True,
        "annotations": True,
    }
    assert "silences: the Viewer role is not enough (HTTP 403)" in capsys.readouterr().err
    assert metric(rec.metrics(), "rotations_total") == "1"
    assert metric(rec.metrics(), "last_success_timestamp_seconds") == "1700000000"


def test_rules_hidden_from_the_reader_fail_the_rules_endpoint(tmp_path: Path) -> None:
    grafana = FakeGrafana()
    grafana.reader_rules = 2
    report = reconciler(grafana, tmp_path).refresh()
    assert not report.ok
    assert report.endpoints["rules"] is False
    assert "the reader sees 2 of 3" in report.error


def test_grafana_down_reports_failure_and_no_success(tmp_path: Path) -> None:
    grafana = FakeGrafana()
    grafana.down = True
    rec = reconciler(grafana, tmp_path)
    report = rec.refresh()
    assert not report.ok
    assert "Connection refused" in report.error
    text = rec.metrics()
    assert metric(text, "ok") == "0"
    assert "last_success_timestamp_seconds" not in text
    assert not rec.ready()


def test_the_admin_password_failing_is_reported_without_a_token(tmp_path: Path) -> None:
    grafana = FakeGrafana()
    client = reader.Grafana("http://grafana:3000", grafana)
    wrong = reader.basic("admin", "wrong")
    report = reader.Reconciler(client, wrong, tmp_path / "token", writer=plain_writer).refresh()
    assert not report.ok
    assert "answered HTTP 401" in report.error
    assert not (tmp_path / "token").exists()


def test_no_token_or_password_reaches_the_log_or_the_metrics(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    grafana = FakeGrafana()
    rec = reconciler(grafana, tmp_path)
    rec.refresh()
    (tmp_path / "token").write_text("glsa_stale_value\n", encoding="utf-8")
    rec.refresh()
    grafana.forbidden = {"rules"}
    rec.refresh()
    grafana.down = True
    rec.refresh()
    out = capsys.readouterr()
    shown = out.out + out.err + rec.metrics()
    for secret in [*grafana.issued(), "glsa_stale_value", "glsa_secret", "secret-admin"]:
        assert secret not in shown


def test_the_reader_reads_only_the_consumer_endpoints_with_its_token(tmp_path: Path) -> None:
    grafana = FakeGrafana()
    reconciler(grafana, tmp_path).refresh()
    paths = {path for _, path in grafana.calls}
    assert {e.split("?")[0] for e in reader.ENDPOINTS.values()} <= paths
    assert reader.ENDPOINTS["annotations"].startswith("/api/annotations?type=alert")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_the_token_file_is_written_whole_with_mode_0440(tmp_path: Path) -> None:
    path = tmp_path / "token"
    reader.write_token(path, "glsa_one")
    assert stat.S_IMODE(path.stat().st_mode) == 0o440
    reader.write_token(path, "glsa_two")
    assert path.read_text(encoding="utf-8") == "glsa_two\n"
    assert stat.S_IMODE(path.stat().st_mode) == 0o440
    assert [p.name for p in tmp_path.iterdir()] == ["token"]


def test_the_reader_counts_the_dashboards_no_file_provisions(tmp_path: Path) -> None:
    grafana = FakeGrafana()
    grafana.dashboards = {"ayeops-health": True, "drawn": False, "a b": "yes", "kept": True}
    rec = reconciler(grafana, tmp_path)
    assert rec.refresh().ok
    assert metric(rec.metrics(), "dashboards_unmanaged") == "2"
    assert ("GET", "/api/dashboards/uid/a%20b") in grafana.calls
    grafana.dashboards = {"ayeops-health": True}
    rec.refresh()
    assert metric(rec.metrics(), "dashboards_unmanaged") == "0"


def test_the_dashboard_count_reads_every_page_as_admin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(reader, "SEARCH_LIMIT", 2)
    grafana = FakeGrafana()
    grafana.dashboards = {"a": False, "b": True, "c": False, "d": False}
    client = reader.Grafana("http://grafana:3000", grafana)
    assert reader.dashboard_uids(client, ADMIN) == ["a", "b", "c", "d"]
    assert grafana.calls.count(("GET", "/api/search")) == 3
    assert reader.count_unmanaged(client, ADMIN) == 3
    assert reader.SEARCH_LIMIT == 2


def test_a_failing_count_drops_the_metric_and_logs_once(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    grafana = FakeGrafana()
    grafana.dashboards = {"drawn": False}
    rec = reconciler(grafana, tmp_path)
    rec.refresh()
    capsys.readouterr()
    grafana.dashboard_status = 500
    assert rec.refresh().ok
    rec.refresh()
    text = rec.metrics()
    assert "dashboards_unmanaged" not in text
    assert metric(text, "ok") == "1"
    assert capsys.readouterr().err == (
        "grafana-reader: dashboards: GET /api/dashboards/uid/drawn answered HTTP 500\n"
    )
    grafana.dashboard_status = 200
    rec.refresh()
    assert metric(rec.metrics(), "dashboards_unmanaged") == "1"
    assert capsys.readouterr().err == "grafana-reader: dashboards: counted again\n"


def test_metrics_carry_the_count_only_once_there_is_one() -> None:
    assert "dashboards_unmanaged" not in reader.metrics_text(reader.State())
    assert f"{reader.PREFIX}dashboards_unmanaged 0\n" in reader.metrics_text(
        reader.State(unmanaged=0)
    )


def test_metrics_omit_the_success_time_before_a_success() -> None:
    text = reader.metrics_text(reader.State())
    assert f"{reader.PREFIX}ok 0\n" in text
    assert "last_success" not in text


def test_the_script_imports_only_the_standard_library() -> None:
    allowed = set(sys.stdlib_module_names) | {"__future__"}
    text = SCRIPT.read_text(encoding="utf-8")
    imported = {
        line.split()[1].split(".")[0]
        for line in text.splitlines()
        if line.startswith(("import ", "from "))
    }
    assert imported <= allowed


def central(data: dict[str, Any], modules: dict[str, Module]) -> dict[str, Any]:
    tree = render_once(manifest_from(data), modules)
    compose: dict[str, Any] = yaml.safe_load(tree["central/compose.yaml"])
    return compose


def test_the_reader_runs_next_to_grafana_as_its_own_user(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    compose = central(data, modules)
    service = compose["services"]["grafana-reader"]
    assert service["user"] == f"{convention.GROUP}:{convention.GROUP}"
    assert service["read_only"] is True
    assert "ports" not in service
    assert f"{convention.VOLUME}:/tmp" in service["volumes"]
    assert service["depends_on"]["grafana"]["condition"] == "service_healthy"
    assert service["environment"]["AYE_GRAFANA_ADMIN_PASSWORD"].startswith("${")
    assert convention.VOLUME in compose["volumes"]
    users = {s.get("user") for n, s in compose["services"].items() if n != "grafana-reader"}
    assert service["user"] not in users


CONSUMER = """services:
  queue-exporter:
    image: python:3.13.16-alpine3.23
    user: "65534:65534"
{group}{env}{volumes}{depends}"""

PARTS = {
    "group": "    group_add: [{{ grafana_reader.group | tojson }}]\n",
    "env": "    environment:\n"
    "      {{ grafana_reader.env }}: {{ grafana_reader.token_file | tojson }}\n",
    "volumes": "    volumes:\n      - {{ grafana_reader.volume | tojson }}\n",
    "depends": "    depends_on:\n      {{ grafana_reader.service }}:\n"
    "        condition: service_healthy\n",
}


def consumer(
    tmp_path: Path,
    data: dict[str, Any],
    host: str = "central",
    declared: bool = True,
    override: dict[str, str] | None = None,
) -> dict[str, Any]:
    root = tmp_path / "modules"
    shutil.copytree(FIXTURE, root)
    spec_path = root / "queue" / "module.yaml"
    spec = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
    if declared:
        spec["services"]["queue-exporter"]["grafana_reader"] = True
    spec_path.write_text(yaml.safe_dump(spec), encoding="utf-8")
    template = CONSUMER.format(**{**PARTS, **(override or {})})
    (root / "queue" / "compose.yaml.j2").write_text(template, encoding="utf-8")
    data["module_paths"] = [str(root)]
    data["hosts"][host]["modules"]["acme-queue"] = {}
    return data


def test_a_consumer_written_as_the_docs_say_passes(tmp_path: Path, data: dict[str, Any]) -> None:
    assert rules_hit(consumer(tmp_path, data)) == set()


def test_the_docs_snippet_renders_to_the_convention(
    tmp_path: Path, data: dict[str, Any], modules: dict[str, Module]
) -> None:
    service = central(consumer(tmp_path, data), modules)["services"]["queue-exporter"]
    assert service["volumes"] == ["grafana-reader:/run/grafana-reader:ro"]
    assert service["environment"] == {"AYE_GRAFANA_READER_TOKEN_FILE": "/run/grafana-reader/token"}
    assert service["group_add"] == ["10470"]


@pytest.mark.parametrize(
    "override",
    [
        {"volumes": "    volumes:\n      - grafana-reader:/run/grafana-reader\n"},
        {"volumes": "    volumes:\n      - grafana-reader:/tmp:ro\n"},
        {"volumes": ""},
        {"env": ""},
        {"group": ""},
        {"depends": ""},
    ],
    ids=["writable", "elsewhere", "unmounted", "no-env", "no-group", "no-wait"],
)
def test_a_consumer_that_misses_a_part_fails_the_mount_rule(
    tmp_path: Path, data: dict[str, Any], override: dict[str, str]
) -> None:
    assert rules_hit(consumer(tmp_path, data, override=override)) == {"grafana-reader-mount"}


def test_a_consumer_away_from_the_grafana_host_fails(tmp_path: Path, data: dict[str, Any]) -> None:
    assert "grafana-reader-mount" in rules_hit(consumer(tmp_path, data, host="edge"))


def test_an_undeclared_service_may_not_mount_the_reader_volume(
    tmp_path: Path, data: dict[str, Any]
) -> None:
    assert rules_hit(consumer(tmp_path, data, declared=False)) == {"grafana-reader-mount"}


def without_consumers(data: dict[str, Any]) -> dict[str, Any]:
    modules = data["hosts"]["central"]["modules"]
    for name in ("status", "incidents", "mcp"):
        modules.pop(name, None)
    modules["ingress"]["routes"].pop("hub", None)
    return data


def reader_parts(data: dict[str, Any], modules: dict[str, Module]) -> dict[str, bool]:
    tree = render_once(manifest_from(data), modules)
    compose = yaml.safe_load(tree["central/compose.yaml"])
    jobs = yaml.safe_load(tree["central/vmagent/scrape.yaml"])["scrape_configs"]
    rules = yaml.safe_load(tree["central/grafana/provisioning/alerting/rules.yaml"])["groups"]
    titles = {rule["title"] for group in rules for rule in group["rules"]}
    return {
        "service": convention.SERVICE in compose["services"],
        "volume": convention.VOLUME in (compose.get("volumes") or {}),
        "job": any(job["job_name"] == convention.SERVICE for job in jobs),
        "script": "central/grafana/grafana_reader.py" in tree,
        "alert": "Grafana reader token does not work" in titles,
    }


def test_without_a_consumer_on_the_grafana_host_no_reader_is_rendered(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    data = without_consumers(data)
    assert set(reader_parts(data, modules).values()) == {False}
    assert rules_hit(data) == set()


def test_the_example_renders_the_reader_for_its_consumers(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    assert set(reader_parts(data, modules).values()) == {True}


def test_one_consumer_is_enough_to_render_the_reader(
    tmp_path: Path, data: dict[str, Any], modules: dict[str, Module]
) -> None:
    data = consumer(tmp_path, without_consumers(data))
    assert set(reader_parts(data, modules).values()) == {True}
    assert rules_hit(data) == set()


def test_a_consumer_on_another_host_does_not_render_the_reader(
    tmp_path: Path, data: dict[str, Any], modules: dict[str, Module]
) -> None:
    data = consumer(tmp_path, without_consumers(data), host="edge")
    assert set(reader_parts(data, modules).values()) == {False}
    assert "grafana-reader-mount" in rules_hit(data)
