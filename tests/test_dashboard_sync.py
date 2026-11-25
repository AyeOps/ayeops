from __future__ import annotations

import argparse
import base64
import json
import urllib.error
import urllib.parse
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
import yaml

from ayeops import dashboard_sync as sync
from ayeops.cli import build_parser, main
from ayeops.defaults import UsageError
from ayeops.manifest import Manifest
from ayeops.registry import load_modules
from tests.conftest import EXAMPLE, ROOT

QUICKSTART = ROOT / "examples" / "quickstart.yaml"
BASE = "https://grafana.example.test"
ADMIN = {sync.USER_ENV: "admin", sync.PASSWORD_ENV: "s3cret"}
AUTH = "Basic " + base64.b64encode(b"admin:s3cret").decode()
VM = {"type": "prometheus", "uid": "victoriametrics"}


class Fake:
    def __init__(self) -> None:
        self.boards: dict[str, dict[str, Any]] = {}
        self.sources: list[dict[str, Any]] = [
            {"uid": "victoriametrics", "name": "VictoriaMetrics", "type": "prometheus"}
        ]
        self.status: dict[str, int] = {}
        self.calls: list[tuple[str, dict[str, str]]] = []

    def add(
        self,
        uid: str,
        title: str = "",
        folder: str = "",
        provisioned: bool = False,
        dashboard: dict[str, Any] | None = None,
    ) -> None:
        board = {"id": 7, "uid": uid, "title": title, "version": 3, "panels": []}
        self.boards[uid] = {
            "hit": {"uid": uid, "type": "dash-db"}
            | ({"title": title} if title else {})
            | ({"folderTitle": folder} if folder else {}),
            "meta": {"provisioned": provisioned, "folderTitle": folder},
            "dashboard": board if dashboard is None else dashboard,
        }

    def __call__(self, url: str, headers: Mapping[str, str]) -> tuple[int, bytes]:
        self.calls.append((url, dict(headers)))
        parts = urllib.parse.urlsplit(url)
        path = parts.path
        if path in self.status:
            return self.status[path], b'{"message":"no"}'
        if path == "/api/search":
            query = urllib.parse.parse_qs(parts.query)
            limit, page = int(query["limit"][0]), int(query["page"][0])
            hits = [board["hit"] for board in self.boards.values()]
            return 200, json.dumps(hits[(page - 1) * limit : page * limit]).encode()
        if path.startswith("/api/dashboards/uid/"):
            uid = urllib.parse.unquote(path.removeprefix("/api/dashboards/uid/"))
            board = self.boards[uid]
            return 200, json.dumps(
                {"meta": board["meta"], "dashboard": board["dashboard"]}
            ).encode()
        if path == "/api/datasources":
            return 200, json.dumps(self.sources).encode()
        return 404, b'{"message":"Not found"}'

    def paths(self) -> list[str]:
        return [url.removeprefix(BASE) for url, _ in self.calls]


def grafana(fake: Fake) -> sync.Grafana:
    return sync.Grafana(BASE, AUTH, fake)


def write_manifest(tmp_path: Path, grafana_settings: dict[str, Any]) -> Path:
    data = yaml.safe_load(QUICKSTART.read_text(encoding="utf-8"))
    data["hosts"]["local"]["modules"]["grafana"] = grafana_settings
    path = tmp_path / "ayeops.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def context_of(path: Path) -> sync.Context:
    manifest = Manifest(yaml.safe_load(path.read_text(encoding="utf-8")), path.parent)
    return sync.grafana_context(path, manifest, load_modules())


@pytest.fixture
def quick(tmp_path: Path) -> sync.Context:
    return context_of(write_manifest(tmp_path, {"dashboards": "dashboards"}))


@pytest.fixture
def acme() -> sync.Context:
    manifest = Manifest(yaml.safe_load(EXAMPLE.read_text(encoding="utf-8")), EXAMPLE.parent)
    return sync.grafana_context(EXAMPLE, manifest, load_modules())


def provision_all(fake: Fake, context: sync.Context) -> None:
    for uid, title in sync.rendered_uids(context).items():
        fake.add(uid, title, "AyeOps", provisioned=True)


class Response:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self.body = body

    def __enter__(self) -> Response:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return self.body


class Opener:
    def __init__(self, outcome: Response | Exception) -> None:
        self.outcome = outcome
        self.seen: list[tuple[str, dict[str, str], float]] = []

    def open(self, request: Any, timeout: float) -> Response:
        self.seen.append((request.full_url, dict(request.header_items()), timeout))
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def test_urlopen_sends_the_headers_with_a_timeout_and_returns_status_and_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opener = Opener(Response(200, b"[]"))
    monkeypatch.setattr(sync, "OPENER", opener)
    assert sync.urlopen_http(f"{BASE}/api/search", {"Accept": "application/json"}) == (200, b"[]")
    assert opener.seen == [(f"{BASE}/api/search", {"Accept": "application/json"}, 15.0)]


def test_urlopen_returns_an_http_error_as_its_status(monkeypatch: pytest.MonkeyPatch) -> None:
    error = urllib.error.HTTPError(BASE, 404, "Not Found", None, None)  # type: ignore[arg-type]
    monkeypatch.setattr(error, "read", lambda: b"gone")
    monkeypatch.setattr(sync, "OPENER", Opener(error))
    assert sync.urlopen_http(BASE, {}) == (404, b"gone")


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (urllib.error.URLError("Connection refused"), "Connection refused"),
        (TimeoutError("timed out"), "timed out"),
    ],
)
def test_urlopen_names_the_host_it_cannot_reach(
    monkeypatch: pytest.MonkeyPatch, error: Exception, reason: str
) -> None:
    monkeypatch.setattr(sync, "OPENER", Opener(error))
    with pytest.raises(sync.GrafanaError) as raised:
        sync.urlopen_http(f"{BASE}:3000/api/search", {})
    assert str(raised.value) == f"cannot reach grafana.example.test:3000: {reason}"


def test_the_opener_follows_no_redirect() -> None:
    assert any(isinstance(handler, sync.NoRedirect) for handler in sync.OPENER.handlers)
    assert sync.NoRedirect().redirect_request() is None


def test_basic_auth_is_the_base64_of_user_and_password() -> None:
    assert sync.basic("admin", "s3cret") == AUTH
    assert AUTH == "Basic YWRtaW46czNjcmV0"


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("localhost", True),
        ("127.0.0.1", True),
        ("127.0.0.2", True),
        ("::1", True),
        ("10.0.0.1", False),
        ("grafana.example.test", False),
        ("LOCALHOST", False),
    ],
)
def test_only_loopback_hosts_count_as_loopback(host: str, expected: bool) -> None:
    assert sync.loopback(host) is expected


@pytest.mark.parametrize(
    ("url", "problem"),
    [
        ("https://grafana.example.test", None),
        ("http://127.0.0.1:3000", None),
        ("http://localhost:3000", None),
        ("http://[::1]:3000", None),
        (
            "http://grafana.example.test",
            "refusing to send the admin password over plain http to grafana.example.test:"
            " use https, or an ssh tunnel to 127.0.0.1",
        ),
        ("http:///api", "'http:///api' is not an http(s) URL"),
        ("ftp://grafana.example.test", "'ftp://grafana.example.test' is not an http(s) URL"),
        ("grafana.example.test", "'grafana.example.test' is not an http(s) URL"),
    ],
)
def test_the_url_must_be_https_or_plain_http_to_loopback(url: str, problem: str | None) -> None:
    assert sync.url_problem(url) == problem


def test_get_sends_the_admin_header_and_parses_json() -> None:
    fake = Fake()
    assert grafana(fake).get("/api/datasources") == fake.sources
    assert fake.calls == [
        (
            f"{BASE}/api/datasources",
            {"Accept": "application/json", "Authorization": AUTH, "User-Agent": "aye-dashboards"},
        )
    ]


@pytest.mark.parametrize(
    ("status", "message"),
    [
        (
            401,
            "GET /api/search answered HTTP 401: Grafana refused AYE_GRAFANA_ADMIN_USER and"
            " AYE_GRAFANA_ADMIN_PASSWORD",
        ),
        (403, "GET /api/search answered HTTP 403"),
        (302, "GET /api/search answered HTTP 302"),
        (500, "GET /api/search answered HTTP 500"),
    ],
)
def test_get_names_the_path_without_its_query_and_the_status(status: int, message: str) -> None:
    fake = Fake()
    fake.status["/api/search"] = status
    with pytest.raises(sync.GrafanaError) as raised:
        grafana(fake).get("/api/search?type=dash-db")
    assert str(raised.value) == message
    assert "s3cret" not in message


@pytest.mark.parametrize("body", [b"<html>", b"\xff\xfe\x00"])
def test_get_refuses_a_body_that_is_not_json(body: bytes) -> None:
    client = sync.Grafana(BASE, AUTH, lambda url, headers: (200, body))
    with pytest.raises(sync.GrafanaError) as raised:
        client.get("/api/search?page=1")
    assert str(raised.value) == "GET /api/search answered with a body that is not JSON"


def test_connect_takes_the_url_given_first(acme: sync.Context) -> None:
    fake = Fake()
    environ = ADMIN | {sync.URL_ENV: "https://env.example.test"}
    client = sync.connect("https://given.example.test/", acme.settings, environ, fake)
    assert client == sync.Grafana("https://given.example.test", AUTH, fake)


def test_connect_falls_back_to_the_environment_then_the_root_url(acme: sync.Context) -> None:
    fake = Fake()
    environ = ADMIN | {sync.URL_ENV: "http://127.0.0.1:3000/"}
    assert sync.connect(None, acme.settings, environ, fake).base == "http://127.0.0.1:3000"
    assert sync.connect(None, acme.settings, ADMIN, fake).base == "https://grafana.ops.acme.example"


def test_connect_without_any_url_says_where_to_set_one(quick: sync.Context) -> None:
    with pytest.raises(UsageError) as raised:
        sync.connect(None, quick.settings, ADMIN, Fake())
    assert str(raised.value) == (
        "no Grafana URL: pass --url, set AYE_GRAFANA_URL or grafana.root_url in the manifest"
    )


def test_connect_refuses_plain_http_to_another_host(quick: sync.Context) -> None:
    with pytest.raises(UsageError, match="refusing to send the admin password"):
        sync.connect("http://10.0.0.1:3000", quick.settings, ADMIN, Fake())


@pytest.mark.parametrize(
    ("environ", "names"),
    [
        ({}, "AYE_GRAFANA_ADMIN_USER, AYE_GRAFANA_ADMIN_PASSWORD"),
        ({sync.USER_ENV: "admin"}, "AYE_GRAFANA_ADMIN_PASSWORD"),
        ({sync.PASSWORD_ENV: "s3cret", sync.USER_ENV: ""}, "AYE_GRAFANA_ADMIN_USER"),
    ],
)
def test_connect_names_the_missing_credentials(
    quick: sync.Context, environ: dict[str, str], names: str
) -> None:
    with pytest.raises(UsageError) as raised:
        sync.connect(BASE, quick.settings, environ, Fake())
    assert str(raised.value) == (
        f"missing environment variable(s): {names}; run it under aye secrets run -- <this command>"
    )


def test_search_walks_every_page(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sync, "SEARCH_LIMIT", 2)
    fake = Fake()
    for uid in ("c", "a", "b"):
        fake.add(uid)
    assert [hit["uid"] for hit in sync.search(grafana(fake))] == ["c", "a", "b"]
    assert fake.paths() == [
        "/api/search?type=dash-db&limit=2&page=1",
        "/api/search?type=dash-db&limit=2&page=2",
    ]


def test_search_stops_after_one_page_shorter_than_the_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sync, "SEARCH_LIMIT", 2)
    fake = Fake()
    for uid in ("a", "b"):
        fake.add(uid)
    assert len(sync.search(grafana(fake))) == 2
    assert fake.paths()[-1] == "/api/search?type=dash-db&limit=2&page=2"
    assert len(fake.paths()) == 2


def test_search_asks_for_the_largest_page_grafana_serves() -> None:
    fake = Fake()
    sync.search(grafana(fake))
    assert fake.paths() == ["/api/search?type=dash-db&limit=5000&page=1"]


def test_live_dashboards_read_each_one_sorted_by_uid() -> None:
    fake = Fake()
    fake.add("zeta", "Zeta", "Shared", provisioned=True)
    fake.add("a b", dashboard={"uid": "a b", "panels": [1]})
    found = sync.live_dashboards(grafana(fake))
    assert found == [
        sync.Live("a b", "", "", False, {"uid": "a b", "panels": [1]}),
        sync.Live(
            "zeta",
            "Zeta",
            "Shared",
            True,
            {"id": 7, "uid": "zeta", "title": "Zeta", "version": 3, "panels": []},
        ),
    ]
    assert "/api/dashboards/uid/a%20b" in fake.paths()


def test_only_a_true_provisioned_flag_counts() -> None:
    fake = Fake()
    fake.add("x")
    fake.boards["x"]["meta"] = {"provisioned": "yes"}
    fake.add("y")
    fake.boards["y"]["meta"] = {}
    assert [item.provisioned for item in sync.live_dashboards(grafana(fake))] == [False, False]


def test_live_datasources_map_uid_to_name() -> None:
    fake = Fake()
    fake.sources.append({"uid": "x1", "name": "Shop", "type": "postgres"})
    assert sync.live_datasources(grafana(fake)) == {
        "victoriametrics": "VictoriaMetrics",
        "x1": "Shop",
    }


def test_the_grafana_host_is_found_and_its_absence_named(tmp_path: Path) -> None:
    context = context_of(write_manifest(tmp_path, {}))
    assert context.host == "local"
    assert context.grafana["dashboards"] is None
    data = yaml.safe_load(QUICKSTART.read_text(encoding="utf-8"))
    del data["hosts"]["local"]["modules"]["grafana"]
    with pytest.raises(UsageError) as raised:
        sync.grafana_context(tmp_path / "m.yaml", Manifest(data, tmp_path), load_modules())
    assert str(raised.value) == f"{tmp_path / 'm.yaml'}: no host runs the grafana module"


def test_rendered_uids_hold_module_and_project_dashboards(tmp_path: Path) -> None:
    path = write_manifest(tmp_path, {"dashboards": "boards"})
    (tmp_path / "boards").mkdir()
    (tmp_path / "boards" / "sales.json").write_text('{"uid": "sales", "title": "Sales"}')
    (tmp_path / "boards" / "bare.json").write_text('{"uid": "bare"}')
    uids = sync.rendered_uids(context_of(path))
    assert uids["sales"] == "Sales"
    assert uids["bare"] == ""
    assert uids["ayeops-health"] == "AyeOps / Health"


def test_repo_datasources_of_a_manifest_without_logs_or_extra_sources(
    quick: sync.Context,
) -> None:
    assert sync.repo_datasources(quick) == {"VictoriaMetrics": VM}


def test_repo_datasources_add_loki_and_the_declared_sources(acme: sync.Context) -> None:
    assert sync.repo_datasources(acme) == {
        "VictoriaMetrics": VM,
        "Loki": {"type": "loki", "uid": "loki"},
        "Acme orders": {"type": "grafana-postgresql-datasource", "uid": "acme-orders"},
    }


def mapper(live: dict[str, str] | None = None) -> sync.Mapper:
    repo = {
        "VictoriaMetrics": VM,
        "Shop": {"type": "grafana-postgresql-datasource", "uid": "shop"},
    }
    return sync.Mapper(repo, live or {}, set())


@pytest.mark.parametrize(
    "value",
    [
        {"type": "prometheus", "uid": "victoriametrics"},
        {"type": "datasource", "uid": "grafana"},
        {"type": "datasource", "uid": "-- Mixed --"},
        {"type": "prometheus", "uid": "${datasource}"},
        "victoriametrics",
        "-- Grafana --",
        "$ds",
        None,
        {"type": "prometheus"},
        {"uid": 5},
    ],
)
def test_repo_builtin_variable_and_empty_datasources_stay_as_they_are(value: Any) -> None:
    found = mapper()
    assert found.ref(value) == value
    assert found.unknown == set()


def test_a_datasource_name_becomes_the_repo_reference() -> None:
    found = mapper()
    assert found.ref("VictoriaMetrics") == VM
    assert found.ref("Shop") == {"type": "grafana-postgresql-datasource", "uid": "shop"}
    assert found.unknown == set()


def test_a_live_uid_maps_through_its_name_to_the_repo_uid() -> None:
    found = mapper({"P1809F7CD0C75ACF3": "Shop"})
    value = {"type": "grafana-postgresql-datasource", "uid": "P1809F7CD0C75ACF3"}
    assert found.ref(value) == {"type": "grafana-postgresql-datasource", "uid": "shop"}
    assert found.ref("P1809F7CD0C75ACF3") == {
        "type": "grafana-postgresql-datasource",
        "uid": "shop",
    }
    assert found.unknown == set()


def test_a_datasource_the_repo_does_not_have_is_kept_and_reported() -> None:
    found = mapper({"abc": "Hand made"})
    assert found.ref({"type": "mysql", "uid": "abc"}) == {"type": "mysql", "uid": "abc"}
    assert found.ref("nowhere") == "nowhere"
    assert found.unknown == {"abc", "nowhere"}


def test_the_repo_reference_is_a_copy() -> None:
    found = mapper()
    reference = found.ref("Shop")
    reference["uid"] = "changed"
    assert found.repo["Shop"]["uid"] == "shop"


def test_rewrite_maps_every_datasource_key_and_nothing_else() -> None:
    board = {
        "datasource": "VictoriaMetrics",
        "title": "VictoriaMetrics",
        "panels": [
            {"datasource": "Shop", "targets": [{"datasource": "VictoriaMetrics", "expr": "up"}]},
            "VictoriaMetrics",
        ],
        "templating": {"list": [{"datasource": {"uid": "Shop"}, "name": "Shop"}]},
    }
    assert mapper().rewrite(board) == {
        "datasource": VM,
        "title": "VictoriaMetrics",
        "panels": [
            {
                "datasource": {"type": "grafana-postgresql-datasource", "uid": "shop"},
                "targets": [{"datasource": VM, "expr": "up"}],
            },
            "VictoriaMetrics",
        ],
        "templating": {
            "list": [
                {
                    "datasource": {"type": "grafana-postgresql-datasource", "uid": "shop"},
                    "name": "Shop",
                }
            ]
        },
    }


def test_normalising_drops_the_volatile_top_level_fields_only() -> None:
    board = {
        "id": 12,
        "version": 9,
        "iteration": 1700000000,
        "folderId": 3,
        "folderUid": "f1",
        "uid": "sales",
        "title": "Sales",
        "panels": [{"id": 1, "version": 2, "datasource": "VictoriaMetrics"}],
    }
    assert sync.normalised(board, mapper()) == {
        "uid": "sales",
        "title": "Sales",
        "panels": [{"id": 1, "version": 2, "datasource": VM}],
    }


def live(uid: str, provisioned: bool, title: str = "", folder: str = "") -> sync.Live:
    return sync.Live(uid, title, folder, provisioned, {})


def test_classify_sorts_by_kind_then_uid() -> None:
    entries = sync.classify(
        [
            live("z-hand", False, "Z", "Shared"),
            live("a-hand", False, "A"),
            live("old", True, "Old", "AyeOps"),
            live("kept", True, "Kept"),
            live("pulled", False, "Pulled"),
        ],
        {"kept": "Kept", "pulled": "Pulled", "new": "New", "b-new": ""},
    )
    assert entries == [
        sync.Entry("unmanaged", "a-hand", "A", ""),
        sync.Entry("unmanaged", "z-hand", "Z", "Shared"),
        sync.Entry("stale", "old", "Old", "AyeOps"),
        sync.Entry("not-deployed", "b-new", "", ""),
        sync.Entry("not-deployed", "new", "New", ""),
        sync.Entry("not-deployed", "pulled", "Pulled", ""),
    ]


def test_nothing_drifts_when_grafana_provisions_exactly_the_render() -> None:
    assert sync.classify([live("a", True), live("b", True)], {"a": "", "b": ""}) == []


def test_entry_lines_say_what_to_do() -> None:
    lines = [
        sync.entry_line(sync.Entry("unmanaged", "sales", "Sales", "Shared")),
        sync.entry_line(sync.Entry("stale", "old", "Old", "")),
        sync.entry_line(sync.Entry("not-deployed", "new", "New", "")),
    ]
    assert lines == [
        'unmanaged sales "Sales" in Shared: no file provisions it, aye dashboards pull writes it',
        'stale old "Old": provisioned, but the render no longer has it, a deploy removes it',
        'not-deployed new "New": rendered, but Grafana does not provision it yet,'
        " deploy the Grafana host",
    ]


def test_drift_summary_counts_each_kind_present() -> None:
    entries = [
        sync.Entry("unmanaged", "a", "", ""),
        sync.Entry("unmanaged", "b", "", ""),
        sync.Entry("not-deployed", "c", "", ""),
    ]
    assert sync.drift_summary(entries, 9, BASE) == (
        f"2 unmanaged, 1 not-deployed among the 9 dashboard(s) in {BASE}"
    )
    assert sync.drift_summary([sync.Entry("stale", "a", "", "")], 1, BASE) == (
        f"1 stale among the 1 dashboard(s) in {BASE}"
    )
    assert sync.drift_summary([], 4, BASE) == (
        f"all 4 dashboard(s) in {BASE} are provisioned from the render"
    )


def test_drift_is_clean_when_everything_is_provisioned(
    quick: sync.Context, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = Fake()
    provision_all(fake, quick)
    total = len(fake.boards)
    assert sync.drift(quick, grafana(fake), "text") == 0
    out, err = capsys.readouterr()
    assert (
        out == f"{quick.path}: all {total} dashboard(s) in {BASE} are provisioned from the render\n"
    )
    assert err == ""


def test_drift_lists_each_entry_and_exits_1(
    quick: sync.Context, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = Fake()
    provision_all(fake, quick)
    fake.add("sales", "Sales", "Shared")
    total = len(fake.boards)
    assert sync.drift(quick, grafana(fake), "text") == 1
    out, err = capsys.readouterr()
    assert out == (
        'unmanaged sales "Sales" in Shared: no file provisions it, aye dashboards pull writes it\n'
    )
    assert err == f"{quick.path}: 1 unmanaged among the {total} dashboard(s) in {BASE}\n"


def test_drift_json_carries_the_url_the_count_and_every_entry(
    quick: sync.Context, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = Fake()
    provision_all(fake, quick)
    fake.add("sales", "Sales", "Shared")
    assert sync.drift(quick, grafana(fake), "json") == 1
    out, err = capsys.readouterr()
    assert json.loads(out) == {
        "grafana": BASE,
        "dashboards": len(fake.boards),
        "drift": [{"kind": "unmanaged", "uid": "sales", "title": "Sales", "folder": "Shared"}],
    }
    assert out.endswith("}\n")
    assert err == ""


def test_drift_json_exits_0_without_drift(
    quick: sync.Context, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = Fake()
    provision_all(fake, quick)
    assert sync.drift(quick, grafana(fake), "json") == 0
    assert json.loads(capsys.readouterr().out)["drift"] == []


def test_choose_takes_every_unprovisioned_dashboard_without_uids() -> None:
    found = [live("a", False), live("b", True), live("c", False)]
    assert [item.uid for item in sync.choose(found, [])] == ["a", "c"]


def test_choose_takes_the_named_uids_in_order() -> None:
    found = [live("a", False), live("b", True), live("c", False)]
    assert [item.uid for item in sync.choose(found, ["c", "a"])] == ["c", "a"]


def test_choose_refuses_an_unknown_or_provisioned_uid() -> None:
    found = [live("a", False), live("b", True)]
    with pytest.raises(UsageError) as raised:
        sync.choose(found, ["a", "nope"])
    assert str(raised.value) == "no dashboard with uid 'nope' in Grafana"
    with pytest.raises(UsageError) as raised:
        sync.choose(found, ["b"])
    assert str(raised.value) == "dashboard 'b' is provisioned from a file already, edit the file"


def test_target_dir_defaults_to_grafana_dashboards(
    quick: sync.Context, capsys: pytest.CaptureFixture[str]
) -> None:
    assert sync.target_dir(quick, None) == quick.path.parent / "dashboards"
    assert (
        sync.target_dir(quick, quick.path.parent / "dashboards") == quick.path.parent / "dashboards"
    )
    assert capsys.readouterr().err == ""


def test_target_dir_notes_a_directory_the_render_does_not_provision(
    quick: sync.Context, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert sync.target_dir(quick, tmp_path / "elsewhere") == tmp_path / "elsewhere"
    assert capsys.readouterr().err == (
        f"note: the render provisions grafana.dashboards only, so {tmp_path / 'elsewhere'} stays"
        " unprovisioned until grafana.dashboards names it\n"
    )


def test_target_dir_without_grafana_dashboards(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    context = context_of(write_manifest(tmp_path, {}))
    with pytest.raises(UsageError) as raised:
        sync.target_dir(context, None)
    assert str(raised.value) == (
        f"{context.path}: grafana.dashboards is not set; pull into a new directory next to the"
        " manifest with -o <dir>, then set grafana.dashboards to it"
    )
    assert sync.target_dir(context, tmp_path / "out") == tmp_path / "out"
    assert "stays unprovisioned" in capsys.readouterr().err


def test_files_by_uid_skips_what_is_not_a_dashboard(tmp_path: Path) -> None:
    (tmp_path / "a-broken.json").write_text("{")
    (tmp_path / "b-list.json").write_text("[]")
    (tmp_path / "c-number.json").write_text('{"uid": 5}')
    (tmp_path / "d-sales.json").write_text('{"uid": "sales"}')
    (tmp_path / "e-sales.json").write_text('{"uid": "sales"}')
    (tmp_path / "f-bytes.json").write_bytes(b"\xff")
    (tmp_path / "g.yaml").write_text('{"uid": "yaml"}')
    assert sync.files_by_uid(tmp_path) == {"sales": tmp_path / "d-sales.json"}
    assert sync.files_by_uid(tmp_path / "missing") == {}


def test_write_board_writes_sorted_indented_json_into_a_new_directory(tmp_path: Path) -> None:
    path = tmp_path / "a" / "b" / "sales.json"
    assert sync.write_board(path, {"uid": "sales", "title": "Ventes été"}, False) == "wrote"
    assert path.read_bytes() == '{\n  "title": "Ventes été",\n  "uid": "sales"\n}\n'.encode()
    second = tmp_path / "a" / "b" / "other.json"
    assert sync.write_board(second, {"uid": "other"}, False) == "wrote"


def test_write_board_keeps_an_equal_file_as_it_is(tmp_path: Path) -> None:
    path = tmp_path / "sales.json"
    path.write_text('{"uid": "sales", "title": "Sales"}')
    assert sync.write_board(path, {"title": "Sales", "uid": "sales"}, True) == "unchanged"
    assert path.read_text() == '{"uid": "sales", "title": "Sales"}'


@pytest.mark.parametrize("current", ['{"uid": "sales"}', "{", ""])
def test_write_board_refuses_a_different_file_unless_forced(tmp_path: Path, current: str) -> None:
    path = tmp_path / "sales.json"
    path.write_text(current)
    board = {"uid": "sales", "title": "Sales"}
    assert sync.write_board(path, board, False) == "refused"
    assert path.read_text() == current
    assert sync.write_board(path, board, True) == "replaced"
    assert json.loads(path.read_bytes()) == board


def test_same_board_compares_parsed_json(tmp_path: Path) -> None:
    path = tmp_path / "x.json"
    path.write_text('{"b": 1, "a": 2}')
    assert sync.same_board(path, {"a": 2, "b": 1}) is True
    assert sync.same_board(path, {"a": 2}) is False
    path.write_text("not json")
    assert sync.same_board(path, {}) is False


def drawn(uid: str, datasource: Any = "VictoriaMetrics") -> dict[str, Any]:
    return {
        "id": 40,
        "uid": uid,
        "title": uid.title(),
        "version": 6,
        "panels": [{"id": 1, "datasource": datasource, "type": "timeseries"}],
    }


def test_pull_writes_every_unmanaged_dashboard_normalised(
    quick: sync.Context, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = Fake()
    provision_all(fake, quick)
    fake.add("sales", "Sales", "Shared", dashboard=drawn("sales"))
    fake.add("costs", "Costs", dashboard=drawn("costs", {"type": "prometheus", "uid": "P0"}))
    fake.sources.append({"uid": "P0", "name": "VictoriaMetrics"})
    directory = quick.path.parent / "dashboards"
    assert sync.pull(quick, grafana(fake), [], None, False) == 0
    out, err = capsys.readouterr()
    assert out == (
        f"wrote {directory / 'costs.json'}\n"
        f"wrote {directory / 'sales.json'}\n"
        f"{quick.path}: 2 dashboard(s) into {directory}: 2 written, 0 replaced, 0 unchanged,"
        " 0 refused; render and deploy to provision them\n"
    )
    assert err == ""
    assert json.loads((directory / "sales.json").read_bytes()) == {
        "uid": "sales",
        "title": "Sales",
        "panels": [{"id": 1, "datasource": VM, "type": "timeseries"}],
    }
    assert json.loads((directory / "costs.json").read_bytes())["panels"][0]["datasource"] == VM
    assert sync.rendered_uids(quick)["sales"] == "Sales"


def test_pull_again_leaves_the_files_and_refuses_a_changed_one(
    quick: sync.Context, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = Fake()
    fake.add("sales", "Sales", dashboard=drawn("sales"))
    fake.add("costs", "Costs", dashboard=drawn("costs"))
    directory = quick.path.parent / "dashboards"
    assert sync.pull(quick, grafana(fake), [], None, False) == 0
    capsys.readouterr()
    (directory / "sales.json").rename(directory / "renamed.json")
    fake.boards["costs"]["dashboard"]["title"] = "Costs v2"
    assert sync.pull(quick, grafana(fake), [], None, False) == 1
    out, err = capsys.readouterr()
    assert out == (
        f"unchanged {directory / 'renamed.json'}\n"
        f"{quick.path}: 2 dashboard(s) into {directory}: 0 written, 0 replaced, 1 unchanged,"
        " 1 refused; render and deploy to provision them\n"
    )
    costs = directory / "costs.json"
    assert err == f"refused {costs}: it differs from costs in Grafana, --force replaces it\n"
    assert sync.pull(quick, grafana(fake), ["costs"], None, True) == 0
    out, _ = capsys.readouterr()
    assert out.startswith(f"replaced {directory / 'costs.json'}\n")
    assert ", 1 replaced," in out
    assert json.loads((directory / "costs.json").read_bytes())["title"] == "Costs v2"


def test_pull_refuses_a_uid_that_cannot_name_a_file_and_goes_on(
    quick: sync.Context, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = Fake()
    fake.add("../evil", dashboard=drawn("../evil"))
    fake.add("good", dashboard=drawn("good"))
    out_dir = tmp_path / "out"
    assert sync.pull(quick, grafana(fake), [], out_dir, False) == 1
    out, err = capsys.readouterr()
    assert err.endswith(
        "refused '../evil': a uid that names a file is 1 to 40 of A-Z a-z 0-9 - _\n"
    )
    assert out == (
        f"wrote {out_dir / 'good.json'}\n"
        f"{quick.path}: 2 dashboard(s) into {out_dir}: 1 written, 0 replaced, 0 unchanged,"
        " 1 refused; render and deploy to provision them\n"
    )
    assert sorted(path.name for path in tmp_path.rglob("*.json")) == ["good.json"]


@pytest.mark.parametrize(("uid", "ok"), [("a" * 40, True), ("a" * 41, False), ("A-z_9", True)])
def test_a_uid_names_a_file_up_to_40_characters(uid: str, ok: bool) -> None:
    assert bool(sync.UID.fullmatch(uid)) is ok


def test_pull_warns_about_a_datasource_the_manifest_does_not_provision(
    quick: sync.Context, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = Fake()
    fake.add("sales", dashboard=drawn("sales", {"type": "mysql", "uid": "m1"}))
    fake.add("zz", dashboard=drawn("zz", "Other"))
    directory = quick.path.parent / "dashboards"
    assert sync.pull(quick, grafana(fake), [], None, False) == 0
    assert capsys.readouterr().err == (
        f"warning: {directory / 'sales.json'} reads datasource 'm1', which the manifest does not"
        " provision: declare it under grafana.datasources, or aye check fails\n"
        f"warning: {directory / 'zz.json'} reads datasource 'Other', which the manifest does not"
        " provision: declare it under grafana.datasources, or aye check fails\n"
    )


def test_pull_with_nothing_unmanaged_writes_nothing(
    quick: sync.Context, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = Fake()
    provision_all(fake, quick)
    assert sync.pull(quick, grafana(fake), [], None, False) == 0
    assert capsys.readouterr().out == f"{quick.path}: no unmanaged dashboard in {BASE}\n"
    assert not (quick.path.parent / "dashboards").exists()
    assert "/api/datasources" not in fake.paths()


def parse(*argv: str) -> argparse.Namespace:
    return build_parser().parse_args(["dashboards", *argv])


def test_the_parser_defaults() -> None:
    drift_args = parse("drift")
    assert (drift_args.action, drift_args.format, drift_args.url) == ("drift", "text", None)
    assert drift_args.manifest is None
    pull_args = parse("pull", "m.yaml", "--uid", "a", "--uid", "b", "-o", "out", "--force")
    assert pull_args.uid == ["a", "b"]
    assert pull_args.out == Path("out")
    assert pull_args.force is True
    assert pull_args.manifest == Path("m.yaml")
    bare = parse("pull")
    assert (bare.uid, bare.out, bare.force) == ([], None, False)
    assert parse("drift", "--format", "json", "--url", BASE).url == BASE


def test_the_dashboards_command_needs_an_action(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as raised:
        parse()
    assert raised.value.code == 2
    assert "the following arguments are required: action" in capsys.readouterr().err


def help_text(monkeypatch: pytest.MonkeyPatch, *argv: str) -> str:
    monkeypatch.setenv("COLUMNS", "100")
    parser = build_parser()
    for name in argv:
        parser = sync_subparser(parser, name)
    return parser.format_help()


def sync_subparser(parser: argparse.ArgumentParser, name: str) -> argparse.ArgumentParser:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            found: argparse.ArgumentParser = action.choices[name]
            return found
    raise AssertionError(name)


def test_the_help_of_dashboards(monkeypatch: pytest.MonkeyPatch) -> None:
    assert help_text(monkeypatch, "dashboards") == (
        "usage: aye dashboards [-h] {drift,pull} ...\n"
        "\n"
        "compare the dashboards in Grafana with the render, pull those drawn in the UI\n"
        "\n"
        "positional arguments:\n"
        "  {drift,pull}\n"
        "    drift       list dashboards no file provisions and rendered ones Grafana lacks,"
        " exit 1 on any\n"
        "    pull        write the dashboards no file provisions into grafana.dashboards\n"
        "\n"
        "options:\n"
        "  -h, --help    show this help message and exit\n"
        "\n"
        "examples:\n"
        "  aye dashboards drift ops/ayeops.yaml\n"
        "  aye dashboards pull ops/ayeops.yaml --uid sales-funnel\n"
    )


def test_the_help_of_drift(monkeypatch: pytest.MonkeyPatch) -> None:
    assert help_text(monkeypatch, "dashboards", "drift") == (
        "usage: aye dashboards drift [-h] [--format {text,json}] [--url URL] [manifest]\n"
        "\n"
        "list dashboards no file provisions and rendered ones Grafana lacks, exit 1 on any\n"
        "\n"
        "positional arguments:\n"
        "  manifest              default $AYE_MANIFEST, else ayeops.yaml or manifest.yaml in"
        " this directory\n"
        "\n"
        "options:\n"
        "  -h, --help            show this help message and exit\n"
        "  --format {text,json}\n"
        "  --url URL             Grafana's URL, default $AYE_GRAFANA_URL, else grafana.root_url\n"
        "\n"
        "examples:\n"
        "  aye dashboards drift ops/ayeops.yaml\n"
        "  aye dashboards drift --url http://127.0.0.1:3000 --format json\n"
    )


def test_the_help_of_pull(monkeypatch: pytest.MonkeyPatch) -> None:
    assert help_text(monkeypatch, "dashboards", "pull") == (
        "usage: aye dashboards pull [-h] [--uid UID] [-o OUT] [--force] [--url URL] [manifest]\n"
        "\n"
        "write the dashboards no file provisions into grafana.dashboards\n"
        "\n"
        "positional arguments:\n"
        "  manifest       default $AYE_MANIFEST, else ayeops.yaml or manifest.yaml in this"
        " directory\n"
        "\n"
        "options:\n"
        "  -h, --help     show this help message and exit\n"
        "  --uid UID      only this dashboard, repeatable\n"
        "  -o, --out OUT  default grafana.dashboards\n"
        "  --force        replace a file that differs from Grafana's copy\n"
        "  --url URL      Grafana's URL, default $AYE_GRAFANA_URL, else grafana.root_url\n"
        "\n"
        "examples:\n"
        "  aye dashboards pull ops/ayeops.yaml\n"
        "  aye dashboards pull --uid sales-funnel --force\n"
    )


def namespace(path: Path, action: str, **extra: Any) -> argparse.Namespace:
    values = {"manifest": path, "action": action, "url": BASE, "format": "text"}
    values |= {"uid": [], "out": None, "force": False}
    return argparse.Namespace(**(values | extra))


@pytest.fixture
def manifest_file(tmp_path: Path) -> Path:
    (tmp_path / "dashboards").mkdir()
    (tmp_path / "dashboards" / "orders.json").write_text('{"uid": "orders", "title": "Orders"}')
    return write_manifest(tmp_path, {"dashboards": "dashboards"})


def test_run_drifts_against_the_checked_manifest(
    manifest_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = Fake()
    provision_all(fake, context_of(manifest_file))
    assert sync.run(namespace(manifest_file, "drift"), ADMIN, fake) == 0
    assert "are provisioned from the render" in capsys.readouterr().out
    assert fake.calls[0][1]["Authorization"] == AUTH
    fake.add("sales")
    assert sync.run(namespace(manifest_file, "drift", format="json"), ADMIN, fake) == 1
    assert json.loads(capsys.readouterr().out)["drift"][0]["uid"] == "sales"


def test_run_pulls(manifest_file: Path, capsys: pytest.CaptureFixture[str]) -> None:
    fake = Fake()
    fake.add("sales", dashboard=drawn("sales"))
    fake.add("costs", dashboard=drawn("costs"))
    assert sync.run(namespace(manifest_file, "pull", uid=["sales"]), ADMIN, fake) == 0
    assert capsys.readouterr().out.startswith("wrote ")
    shown = sorted(p.name for p in (manifest_file.parent / "dashboards").iterdir())
    assert shown == ["orders.json", "sales.json"]


def test_run_stops_on_a_manifest_with_errors(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = write_manifest(tmp_path, {"dashboards": "dashboards", "locale": "xx"})
    fake = Fake()
    assert sync.run(namespace(path, "drift"), ADMIN, fake) == 1
    out, err = capsys.readouterr()
    assert out == ""
    assert "module-settings" in err
    assert err.endswith(f"{path}: fix the errors first\n")
    assert fake.calls == []


def test_run_reports_grafana_errors_with_exit_2(
    manifest_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = Fake()
    fake.status["/api/search"] = 401
    assert sync.run(namespace(manifest_file, "pull"), ADMIN, fake) == 2
    assert capsys.readouterr() == (
        "",
        "aye dashboards: GET /api/search answered HTTP 401: Grafana refused"
        " AYE_GRAFANA_ADMIN_USER and AYE_GRAFANA_ADMIN_PASSWORD\n",
    )


@pytest.mark.parametrize(
    ("answer", "error"),
    [
        (b'[{"title": "no uid"}]', "KeyError: 'uid'"),
        (b"[5]", "TypeError: 'int' object is not subscriptable"),
    ],
)
def test_run_reports_an_unexpected_answer_with_exit_2(
    manifest_file: Path, capsys: pytest.CaptureFixture[str], answer: bytes, error: str
) -> None:
    def http(url: str, headers: Mapping[str, str]) -> tuple[int, bytes]:
        return 200, answer

    assert sync.run(namespace(manifest_file, "drift"), ADMIN, http) == 2
    assert capsys.readouterr() == (
        "",
        f"aye dashboards: Grafana answered in a shape this version does not read ({error})\n",
    )


def test_the_cli_runs_dashboards_and_a_usage_error_exits_2(
    manifest_file: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv(sync.USER_ENV, raising=False)
    monkeypatch.delenv(sync.PASSWORD_ENV, raising=False)
    monkeypatch.delenv(sync.URL_ENV, raising=False)
    assert main(["dashboards", "drift", str(manifest_file), "--url", BASE]) == 2
    assert capsys.readouterr().err == (
        "aye: missing environment variable(s): AYE_GRAFANA_ADMIN_USER, AYE_GRAFANA_ADMIN_PASSWORD;"
        " run it under aye secrets run -- <this command>\n"
    )
