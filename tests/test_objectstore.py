from __future__ import annotations

import importlib.util
import json
import sys
import threading
import urllib.error
import urllib.request
from types import ModuleType
from typing import Any

import pytest

from ayeops.registry import Module
from ayeops.render import grafana_rules, host_compose, host_scrape
from ayeops.rules import parse_size
from tests.conftest import ROOT, WAIT, manifest_from, pinned, render_once, rules_hit, tag_of

ADMIN = ROOT / "src" / "ayeops" / "modules" / "objectstore" / "files" / "objectstore_admin.py"
SERVICES = {"garage", "garage-admin"}
GIB = 1024**3


def load_admin() -> ModuleType:
    spec = importlib.util.spec_from_file_location("objectstore_admin", ADMIN)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["objectstore_admin"] = module
    spec.loader.exec_module(module)
    return module


admin = load_admin()


def store(data: dict[str, Any]) -> dict[str, Any]:
    settings: dict[str, Any] = data["hosts"]["central"]["modules"]["objectstore"]
    return settings


def services(data: dict[str, Any], modules: dict[str, Module]) -> dict[str, Any]:
    result: dict[str, Any] = host_compose(manifest_from(data), "central", modules)["services"]
    return result


def test_the_example_objectstore_passes_every_rule(data: dict[str, Any]) -> None:
    assert rules_hit(data) == set()


@pytest.mark.parametrize(
    ("setting", "value"),
    [
        ("provider", "minio"),
        ("bucket", "Upper_Case"),
        ("bucket", "a"),
        ("quota", "40"),
        ("quota", "1.5g"),
        ("quota", "40gb"),
        ("data_dir", "relative/path"),
        ("meta_dir", "/with:colon"),
        ("expose", [3903]),
        ("region", "Not A Region"),
        ("unknown", 1),
    ],
)
def test_settings_outside_the_schema_are_rejected(
    data: dict[str, Any], setting: str, value: Any
) -> None:
    store(data)[setting] = value
    assert rules_hit(data) == {"module-settings"}


def test_objectstore_runs_without_node_because_the_admin_serves_its_own_metrics(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    del data["hosts"]["central"]["modules"]["node"]
    del data["hosts"]["central"]["modules"]["reconciler"]
    assert "module-requires" not in rules_hit(data)
    admin_service = services(data, modules)["garage-admin"]
    assert admin_service["ports"] == ["127.0.0.1:9132:9132"]
    assert admin_service["user"] == "65534:65534"
    assert "volumes" in admin_service
    assert all("textfile" not in volume for volume in admin_service["volumes"])


def test_garage_and_its_admin_run_pinned_multi_arch_images_with_limits(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    rendered = services(data, modules)
    assert tag_of(rendered["garage"]["image"]) == pinned("objectstore", "dxflrs/garage")
    assert tag_of(rendered["garage-admin"]["image"]) == pinned("objectstore", "python")
    assert rendered["garage"]["platform"] == "linux/arm64"
    total = sum(parse_size(str(rendered[name]["mem_limit"])) for name in SERVICES)
    assert total <= parse_size("320m")
    assert rendered["garage"]["healthcheck"]["test"] == ["CMD", "/garage", "health"]
    assert rendered["garage-admin"]["depends_on"] == {"garage": {"condition": "service_healthy"}}


def test_s3_binds_to_public_bind_only_when_exposed_and_admin_stays_on_loopback(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    assert services(data, modules)["garage"]["ports"] == [
        "10.8.0.1:3900:3900",
        "127.0.0.1:3903:3903",
    ]
    store(data)["expose"] = []
    assert services(data, modules)["garage"]["ports"] == [
        "127.0.0.1:3900:3900",
        "127.0.0.1:3903:3903",
    ]
    assert services(data, modules)["garage-admin"]["ports"] == ["127.0.0.1:9132:9132"]


def test_single_node_layout_key_and_bucket_come_from_env_at_start(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    garage = services(data, modules)["garage"]
    assert garage["command"] == ["/garage", "server", "--single-node", "--default-bucket"]
    env = garage["environment"]
    for name, variable in (
        ("GARAGE_RPC_SECRET", "AYE_OBJECTSTORE_RPC_SECRET"),
        ("GARAGE_ADMIN_TOKEN", "AYE_OBJECTSTORE_ADMIN_TOKEN"),
        ("GARAGE_DEFAULT_ACCESS_KEY", "AYE_OBJECTSTORE_ACCESS_KEY_ID"),
        ("GARAGE_DEFAULT_SECRET_KEY", "AYE_OBJECTSTORE_SECRET_ACCESS_KEY"),
    ):
        assert env[name] == "${" + variable + ":?set " + variable + "}"
    assert env["GARAGE_DEFAULT_BUCKET"] == "acme-offsite"
    assert "GARAGE_METRICS_TOKEN" not in env
    tree = render_once(manifest_from(data), modules)
    for variable in ("RPC_SECRET", "ADMIN_TOKEN", "ACCESS_KEY_ID", "SECRET_ACCESS_KEY"):
        assert f"AYE_OBJECTSTORE_{variable}=" in tree["central/.env.example"]


def test_the_admin_reconciles_the_quota_from_the_setting(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    env = services(data, modules)["garage-admin"]["environment"]
    assert env["AYE_OBJECTSTORE_QUOTA_BYTES"] == str(40 * GIB)
    assert env["AYE_OBJECTSTORE_BUCKET"] == "acme-offsite"
    assert env["GARAGE_ADMIN_TOKEN"].startswith("${AYE_OBJECTSTORE_ADMIN_TOKEN:?")
    store(data)["quota"] = "2t"
    env = services(data, modules)["garage-admin"]["environment"]
    assert env["AYE_OBJECTSTORE_QUOTA_BYTES"] == str(2048 * GIB)


def test_data_and_metadata_live_in_named_volumes_or_on_a_host_path(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    compose = host_compose(manifest_from(data), "central", modules)
    assert {"objectstore-data", "objectstore-meta"} <= set(compose["volumes"])
    assert compose["services"]["garage"]["volumes"] == [
        "./objectstore/garage.toml:/etc/garage.toml:ro",
        "objectstore-meta:/var/lib/garage/meta",
        "objectstore-data:/var/lib/garage/data",
    ]
    store(data)["data_dir"] = "/mnt/backup/garage-data"
    store(data)["meta_dir"] = "/srv/garage-meta"
    compose = host_compose(manifest_from(data), "central", modules)
    assert compose["services"]["garage"]["volumes"][1:] == [
        "/srv/garage-meta:/var/lib/garage/meta",
        "/mnt/backup/garage-data:/var/lib/garage/data",
    ]
    assert "objectstore-data" not in compose["volumes"]
    assert "objectstore-meta" not in compose["volumes"]


def test_garage_config_is_single_node_sqlite_without_compression_and_the_region(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    store(data)["region"] = "acme-1"
    text = render_once(manifest_from(data), modules)["central/objectstore/garage.toml"]
    assert 'db_engine = "sqlite"' in text
    assert "replication_factor = 1" in text
    assert 'compression_level = "none"' in text
    assert "metadata_fsync = true" in text
    assert 's3_region = "acme-1"' in text
    assert 'api_bind_addr = "[::]:3903"' in text
    assert "rpc_secret" not in text
    assert "token" not in text


def test_vmagent_scrapes_garage_metrics_on_the_admin_port(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    scrape = host_scrape(manifest_from(data), "central", modules)
    assert scrape is not None
    jobs = {job["job_name"]: job for job in scrape["scrape_configs"]}
    assert jobs["garage"]["static_configs"] == [{"targets": ["garage:3903"]}]
    assert jobs["garage-admin"]["static_configs"] == [{"targets": ["garage-admin:9132"]}]


def rules(data: dict[str, Any], modules: dict[str, Module]) -> dict[str, dict[str, Any]]:
    groups = grafana_rules(manifest_from(data), modules)["groups"]
    return {r["uid"]: r for g in groups if g["name"] == "objectstore" for r in g["rules"]}


def test_alerts_compare_with_bool_and_down_fires_on_silence(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    found = rules(data, modules)
    assert set(found) == {
        "aye-objectstore-down",
        "aye-objectstore-unhealthy",
        "aye-objectstore-quota",
        "aye-objectstore-admin",
    }
    for rule in found.values():
        assert " bool " in rule["data"][0]["model"]["expr"]
    assert found["aye-objectstore-down"]["noDataState"] == "Alerting"
    assert found["aye-objectstore-down"]["labels"]["severity"] == "critical"
    assert found["aye-objectstore-admin"]["noDataState"] == "Alerting"
    assert found["aye-objectstore-quota"]["noDataState"] == "OK"
    assert "> bool 0.8" in found["aye-objectstore-quota"]["data"][0]["model"]["expr"]


INFO = {
    "id": "b1d2",
    "globalAliases": ["acme-offsite"],
    "bytes": 3 * GIB,
    "objects": 211,
    "quotas": {"maxSize": None, "maxObjects": None},
}


def test_bucket_info_parses_the_admin_api_shape() -> None:
    bucket = admin.parse_bucket(INFO)
    assert bucket == admin.Bucket("b1d2", 3 * GIB, 211, None, None)
    limited = admin.parse_bucket({**INFO, "quotas": {"maxSize": 5, "maxObjects": 9}})
    assert (limited.max_size, limited.max_objects) == (5, 9)
    bare = admin.parse_bucket({"id": "x"})
    assert (bare.bytes, bare.objects, bare.max_size, bare.max_objects) == (0, 0, None, None)


def test_quota_change_sets_both_quotas_and_keeps_an_object_limit() -> None:
    bucket = admin.parse_bucket({**INFO, "quotas": {"maxSize": None, "maxObjects": 9}})
    assert admin.quota_change(bucket, 40 * GIB) == {
        "quotas": {"maxSize": 40 * GIB, "maxObjects": 9}
    }
    done = admin.parse_bucket({**INFO, "quotas": {"maxSize": 40 * GIB}})
    assert admin.quota_change(done, 40 * GIB) is None


class FakeAdmin:
    def __init__(self, quota: int | None = None, fail: Exception | None = None) -> None:
        self.quota = quota
        self.fail = fail
        self.calls: list[tuple[str, str, Any]] = []

    def __call__(self, method: str, path: str, body: bytes | None) -> Any:
        self.calls.append((method, path, json.loads(body) if body else None))
        if self.fail is not None:
            raise self.fail
        if method == "POST":
            self.quota = json.loads(body or b"{}")["quotas"]["maxSize"]
            return {}
        return {**INFO, "quotas": {"maxSize": self.quota, "maxObjects": None}}


def test_reconcile_sets_a_missing_quota_once_and_reports_usage() -> None:
    fake = FakeAdmin()
    report = admin.reconcile(fake, "acme-offsite", 40 * GIB, lambda: 1000.0)
    assert [call[0] for call in fake.calls] == ["GET", "POST", "GET"]
    assert fake.calls[0][1] == "/v2/GetBucketInfo?globalAlias=acme-offsite"
    assert fake.calls[1][1] == "/v2/UpdateBucket?id=b1d2"
    assert fake.calls[1][2] == {"quotas": {"maxSize": 40 * GIB, "maxObjects": None}}
    assert fake.calls[2][1] == "/v2/GetBucketInfo?globalAlias=acme-offsite"
    assert report.ok
    assert report.error == ""
    assert report.info.max_size == 40 * GIB
    again = admin.reconcile(fake, "acme-offsite", 40 * GIB, lambda: 1300.0)
    assert [call[0] for call in fake.calls][3:] == ["GET"]
    assert again.ok and again.checked == 1300.0


def test_reconcile_reports_api_errors_and_a_quota_that_did_not_stick() -> None:
    failed = admin.reconcile(
        FakeAdmin(fail=urllib.error.URLError("refused")), "b", GIB, lambda: 5.0
    )
    assert (failed.ok, failed.info) == (False, None)
    assert "refused" in failed.error

    class Stubborn(FakeAdmin):
        def __call__(self, method: str, path: str, body: bytes | None) -> Any:
            if method == "POST":
                return {}
            return super().__call__(method, path, body)

    stuck = admin.reconcile(Stubborn(), "b", GIB, lambda: 5.0)
    assert stuck.ok is False
    assert stuck.error == "the quota did not apply"
    assert stuck.info is not None


def test_admin_metrics_carry_the_bucket_label_and_the_last_success() -> None:
    ok = admin.Report(
        "acme-offsite", True, admin.parse_bucket({**INFO, "quotas": {"maxSize": 4}}), "", 99.9
    )
    assert admin.metrics_text(ok).splitlines() == [
        'ayeops_objectstore_admin_ok{bucket="acme-offsite"} 1',
        f'ayeops_objectstore_bucket_bytes{{bucket="acme-offsite"}} {3 * GIB}',
        'ayeops_objectstore_bucket_objects{bucket="acme-offsite"} 211',
        'ayeops_objectstore_bucket_quota_bytes{bucket="acme-offsite"} 4',
        'ayeops_objectstore_admin_last_success_timestamp_seconds{bucket="acme-offsite"} 99',
    ]
    down = admin.metrics_text(admin.Report('we"ird\\', False, None, "x", 1.0))
    assert down == 'ayeops_objectstore_admin_ok{bucket="we\\"ird\\\\"} 0\n'
    no_quota = admin.Report("b", False, admin.parse_bucket(INFO), "", 1.0)
    assert "quota" not in admin.metrics_text(no_quota)


def test_reconciler_serves_the_latest_report_over_http() -> None:
    fake = FakeAdmin()
    reconciler = admin.Reconciler(fake, "acme-offsite", GIB)
    assert reconciler.report == admin.Report("acme-offsite", False, None, "not checked yet", 0.0)
    assert reconciler.metrics() == 'ayeops_objectstore_admin_ok{bucket="acme-offsite"} 0\n'
    report = reconciler.refresh()
    assert report.ok
    assert fake.quota == GIB
    server = admin.ThreadingHTTPServer(("127.0.0.1", 0), admin.handler(reconciler))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        with urllib.request.urlopen(f"{base}/metrics", timeout=WAIT) as response:
            body = response.read().decode()
            assert response.status == 200
            assert response.headers["Content-Type"] == "text/plain; version=0.0.4"
            assert response.headers["Content-Length"] == str(len(body))
        assert body == reconciler.metrics()
        assert 'ayeops_objectstore_admin_ok{bucket="acme-offsite"} 1' in body
        with urllib.request.urlopen(f"{base}/health", timeout=WAIT) as response:
            assert response.status == 200
            assert response.read() == b"ok\n"
        with pytest.raises(urllib.error.HTTPError) as missing:
            urllib.request.urlopen(f"{base}/other", timeout=WAIT)
        assert missing.value.code == 404
    finally:
        server.shutdown()
        server.server_close()


class Stop(Exception):
    pass


def test_admin_main_refreshes_every_five_minutes_and_serves_on_9132(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started: list[Any] = []
    served: list[Any] = []
    calls: list[tuple[str, str]] = []
    sleeps: list[float] = []

    class FakeThread:
        def __init__(self, target: Any, daemon: bool) -> None:
            started.append((target, daemon))

        def start(self) -> None:
            return None

    class FakeServer:
        def __init__(self, address: Any, handler_class: Any) -> None:
            served.append((address, handler_class))

        def serve_forever(self) -> None:
            return None

    def fetch(self: Any, method: str, path: str, body: bytes | None) -> Any:
        calls.append((self.url, self.token))
        return {**INFO, "quotas": {"maxSize": GIB, "maxObjects": None}}

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        raise Stop

    monkeypatch.setattr(admin.threading, "Thread", FakeThread)
    monkeypatch.setattr(admin, "ThreadingHTTPServer", FakeServer)
    monkeypatch.setattr(admin.Admin, "fetch", fetch)
    monkeypatch.setattr(admin.time, "sleep", sleep)
    admin.main(
        {
            "GARAGE_ADMIN_TOKEN": "t",
            "AYE_OBJECTSTORE_BUCKET": "acme-offsite",
            "AYE_OBJECTSTORE_QUOTA_BYTES": str(GIB),
        }
    )
    assert started[0][1] is True
    assert served[0][0] == ("", 9132)
    assert issubclass(served[0][1], admin.BaseHTTPRequestHandler)
    with pytest.raises(Stop):
        started[0][0]()
    assert calls == [("http://garage:3903", "t")]
    assert sleeps == [300]
    admin.main(
        {
            "GARAGE_ADMIN_TOKEN": "t",
            "AYE_OBJECTSTORE_BUCKET": "b",
            "AYE_OBJECTSTORE_QUOTA_BYTES": "1",
            "AYE_OBJECTSTORE_ADMIN_PORT": "9999",
            "AYE_GARAGE_ADMIN_URL": "http://elsewhere:3903",
        }
    )
    assert served[1][0] == ("", 9999)
    with pytest.raises(Stop):
        started[1][0]()
    assert calls[1] == ("http://elsewhere:3903", "t")


def test_admin_fetch_sends_the_bearer_token_and_decodes_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}
    bodies = [b'{"id": "x"}', b""]

    class Response:
        def __enter__(self) -> Response:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def read(self) -> bytes:
            return bodies.pop(0)

    def urlopen(request: Any, timeout: int) -> Response:
        seen["url"] = request.full_url
        seen["auth"] = request.get_header("Authorization")
        seen["type"] = request.get_header("Content-type")
        seen["method"] = request.get_method()
        seen["body"] = request.data
        seen["timeout"] = timeout
        return Response()

    monkeypatch.setattr(admin.urllib.request, "urlopen", urlopen)
    client = admin.Admin("http://garage:3903", "secret")
    result = client.fetch("GET", "/v2/GetBucketInfo?globalAlias=b", None)
    assert result == {"id": "x"}
    assert seen == {
        "url": "http://garage:3903/v2/GetBucketInfo?globalAlias=b",
        "auth": "Bearer secret",
        "type": "application/json",
        "method": "GET",
        "body": None,
        "timeout": 30,
    }
    assert client.fetch("POST", "/v2/UpdateBucket?id=x", b"{}") is None
    assert (seen["method"], seen["body"]) == ("POST", b"{}")


def test_admin_logs_to_stderr_with_its_name(capsys: pytest.CaptureFixture[str]) -> None:
    admin.log("hello")
    assert capsys.readouterr().err == "objectstore-admin: hello\n"


def test_reconcile_logs_the_quota_change_and_the_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    admin.reconcile(FakeAdmin(), "acme-offsite", GIB, lambda: 1.0)
    assert capsys.readouterr().err == (
        f"objectstore-admin: set the quota of acme-offsite to {GIB} bytes\n"
    )
    admin.reconcile(FakeAdmin(fail=OSError("refused")), "b", GIB, lambda: 1.0)
    assert capsys.readouterr().err == "objectstore-admin: b: refused\n"
