from __future__ import annotations

import http.client
import importlib.util
import json
import re
import sys
import threading
from collections.abc import Iterator, Mapping
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from ayeops.registry import Module
from ayeops.render import host_compose
from tests.conftest import ROOT, WAIT, manifest_from, rules_hit, tag_of

AGENT = ROOT / "src" / "ayeops" / "modules" / "image-updates" / "files" / "image_updates_agent.py"
SUCCESS_LOGS = """7 tag(s) have been scanned:
* 2 new tag(s) found
* 1 tag(s) updated
* 3 tag(s) unchanged
* 0 tag(s) skipped
* 1 tag(s) with error"""
PG = "sha256:" + "a" * 64
PG_NEW = "sha256:" + "b" * 64
DB_PENDING = (
    'ayeops_image_update_available{container="db",image="docker.io/library/postgres",tag="18"} 1'
)
EXACT = re.compile(r"^[a-z0-9./_-]+:v?\d+\.\d+\.\d+(?:-[a-z0-9.]+)?$")


def load_agent() -> ModuleType:
    spec = importlib.util.spec_from_file_location("image_updates_agent", AGENT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


agent = load_agent()


def container(
    name: str, image: str, image_id: str = "sha256:img", labels: dict[str, str] | None = None
) -> dict[str, Any]:
    return {"Names": [f"/{name}"], "Image": image, "ImageID": image_id, "Labels": labels or {}}


class FakeApi:
    def __init__(self, containers: list[dict[str, Any]], images: Mapping[str, Any]) -> None:
        self.containers = containers
        self.images = dict(images)
        self.calls: list[str] = []

    def __call__(self, path: str) -> Any:
        self.calls.append(path)
        if path == "/containers/json":
            return self.containers
        image_id = path.removeprefix("/images/").removesuffix("/json")
        return self.images[image_id]


def fleet() -> FakeApi:
    return FakeApi(
        [
            container("db", "postgres:18", "sha256:pg"),
            container("web", "ghcr.io/acme/web:latest", "sha256:web"),
            container("grafana", "grafana/grafana:13.2.3", "sha256:gf"),
            container("built", "acme-local:dev", "sha256:local"),
            container("forced", "busybox:1.37.0", "sha256:bb", {agent.LABEL: "true"}),
            container("quiet", "redis:7", "sha256:rd", {agent.LABEL: "false"}),
        ],
        {
            "sha256:pg": {
                "RepoDigests": [f"postgres@{PG}"],
                "Os": "linux",
                "Architecture": "arm64",
                "Variant": "v8",
            },
            "sha256:web": {
                "RepoDigests": ["ghcr.io/acme/web@sha256:" + "c" * 64],
                "Os": "linux",
                "Architecture": "amd64",
            },
            "sha256:local": {"RepoDigests": [], "Os": "linux", "Architecture": "amd64"},
            "sha256:bb": {"RepoDigests": ["busybox@sha256:" + "d" * 64], "Architecture": "amd64"},
        },
    )


@pytest.mark.parametrize(
    ("reference", "expected"),
    [
        ("postgres:18", ("docker.io/library/postgres", "18")),
        ("postgres", ("docker.io/library/postgres", "latest")),
        ("grafana/grafana:13.2.3", ("docker.io/grafana/grafana", "13.2.3")),
        ("ghcr.io/acme/web:latest", ("ghcr.io/acme/web", "latest")),
        ("localhost:5000/tool:1", ("localhost:5000/tool", "1")),
        ("registry.local:5000/tool", ("registry.local:5000/tool", "latest")),
        ("docker.io/library/postgres:18", ("docker.io/library/postgres", "18")),
        (f"postgres:18@{PG}", ("docker.io/library/postgres", "18")),
    ],
)
def test_references_normalize_like_the_registry_names_them(
    reference: str, expected: tuple[str, str]
) -> None:
    assert agent.normalize(reference) == expected


@pytest.mark.parametrize(
    "reference",
    [
        "postgres:18",
        "postgres:18-alpine",
        "postgres",
        "ghcr.io/acme/web:latest",
        "python:3.13",
        "nginx:stable",
        "busybox:1",
        "localhost:5000/tool",
    ],
)
def test_a_tag_without_a_full_version_floats(reference: str) -> None:
    assert agent.is_floating(reference)


@pytest.mark.parametrize(
    "reference",
    [
        "grafana/grafana:13.2.3",
        "python:3.13.16-alpine3.23",
        "ghcr.io/google/cadvisor:v0.60.6",
        "victoriametrics/vmagent:v1.153.0",
        "busybox:1.37.0",
        "restic/restic:0.19.1",
        f"postgres:18@{PG}",
        f"postgres@{PG}",
    ],
)
def test_a_full_version_or_a_digest_is_pinned_and_left_to_renovate(reference: str) -> None:
    assert not agent.is_floating(reference)


def test_a_label_forces_or_silences_the_watch_and_a_bare_digest_is_skipped() -> None:
    assert agent.verdict("busybox:1.37.0", {agent.LABEL: "true"}) == "watch"
    assert agent.verdict("redis:7", {agent.LABEL: "False"}) == "skipped"
    assert agent.verdict("sha256:" + "e" * 64, {}) == "skipped"
    assert agent.verdict("", None) == "skipped"
    assert agent.verdict("redis:7", None) == "watch"
    assert agent.verdict("redis:7.4.1", {agent.LABEL: "maybe"}) == "pinned"


def test_the_scan_watches_floating_tags_with_registry_digests_and_sorts_the_rest() -> None:
    api = fleet()
    result = agent.scan(api)
    assert [w.container for w in result.watched] == ["db", "web", "forced"]
    db = result.watched[0]
    assert (db.repository, db.tag, db.digests) == ("docker.io/library/postgres", "18", {PG})
    assert dict(db.platform) == {"os": "linux", "arch": "arm64", "variant": "v8"}
    assert result.states == {"grafana": "pinned", "built": "local", "quiet": "skipped"}
    assert "/images/sha256:gf/json" not in api.calls
    assert "/images/sha256:rd/json" not in api.calls


def test_a_digest_of_another_repository_does_not_count_for_the_image() -> None:
    api = FakeApi(
        [container("db", "postgres:18", "sha256:pg")],
        {"sha256:pg": {"RepoDigests": [f"mirror.local/postgres@{PG}"], "Architecture": "amd64"}},
    )
    assert agent.scan(api).states == {"db": "local"}


def test_a_container_without_a_name_is_named_by_its_short_id() -> None:
    assert agent.container_name({"Id": "0123456789abcdef"}) == "0123456789ab"


def test_diun_gets_each_floating_image_once_with_its_platform() -> None:
    watched = agent.scan(fleet()).watched
    twin = agent.Watched("db2", watched[0].repository, "18", frozenset({PG}), watched[0].platform)
    entries = json.loads(agent.diun_images([*watched, twin]))
    assert entries == [
        {"name": "docker.io/library/busybox:1.37.0", "platform": {"arch": "amd64", "os": "linux"}},
        {
            "name": "docker.io/library/postgres:18",
            "platform": {"arch": "arm64", "os": "linux", "variant": "v8"},
        },
        {"name": "ghcr.io/acme/web:latest", "platform": {"arch": "amd64", "os": "linux"}},
    ]


def test_only_new_and_changed_digests_from_diun_are_kept() -> None:
    state = agent.State()
    payload = {"status": "new", "image": "docker.io/library/postgres:18", "digest": PG}
    assert agent.apply_notification(state, payload)
    assert agent.apply_notification(state, {**payload, "status": "update", "digest": PG_NEW})
    assert not agent.apply_notification(state, {**payload, "status": "unchange", "digest": PG})
    assert not agent.apply_notification(state, {**payload, "digest": "md5:x"})
    assert not agent.apply_notification(state, {**payload, "image": ""})
    assert state.digests == {"docker.io/library/postgres:18": PG_NEW}
    assert state.notifications == 2
    with pytest.raises(ValueError, match="not an object"):
        agent.apply_notification(state, ["new"])


def test_diun_pings_record_the_run_and_its_counts() -> None:
    state = agent.State()
    assert agent.apply_ping(state, "/hc/diun/start", "", 10.0)
    assert agent.apply_ping(state, "/hc/diun", SUCCESS_LOGS, 20.0)
    assert agent.apply_ping(state, "/hc/diun/fail", "boom", 30.0)
    assert (state.check_started, state.check_success, state.check_failed) == (10.0, 20.0, 30.0)
    assert state.counts == {"new": 2, "update": 1, "unchange": 3, "skip": 0, "error": 1}
    assert not agent.apply_ping(state, "/hc/diun/other", "", 40.0)
    assert not agent.apply_ping(state, "/hc", "", 40.0)
    assert not agent.apply_ping(state, "/diun", "", 40.0)


def test_a_container_runs_an_older_image_only_when_its_digest_differs_from_the_tag() -> None:
    result = agent.scan(fleet())
    state = agent.State(
        digests={
            "docker.io/library/postgres:18": PG_NEW,
            "ghcr.io/acme/web:latest": "sha256:" + "c" * 64,
        }
    )
    rows = agent.classify(result, state)
    assert {name: decision for name, (decision, _) in rows.items()} == {
        "db": "pending",
        "web": "current",
        "forced": "unknown",
        "grafana": "pinned",
        "built": "local",
        "quiet": "skipped",
    }


def test_metrics_flag_pending_containers_and_count_every_state() -> None:
    result = agent.scan(fleet())
    state = agent.State(
        digests={
            "docker.io/library/postgres:18": PG_NEW,
            "ghcr.io/acme/web:latest": "sha256:" + "c" * 64,
        },
        check_success=50.0,
        counts={"error": 1, "new": 2},
        notifications=3,
    )
    text = agent.render_metrics(agent.classify(result, state), state, 100.4)
    lines = text.splitlines()
    assert DB_PENDING in lines
    assert (
        'ayeops_image_update_available{container="web",image="ghcr.io/acme/web",tag="latest"} 0'
        in lines
    )
    assert not any('container="forced"' in line for line in lines)
    assert 'ayeops_image_updates_containers{state="pending"} 1' in lines
    assert 'ayeops_image_updates_containers{state="current"} 1' in lines
    assert 'ayeops_image_updates_containers{state="unknown"} 1' in lines
    assert 'ayeops_image_updates_containers{state="pinned"} 1' in lines
    assert 'ayeops_image_updates_containers{state="local"} 1' in lines
    assert 'ayeops_image_updates_containers{state="skipped"} 1' in lines
    assert "ayeops_image_updates_last_scan_timestamp_seconds 100" in lines
    assert "ayeops_image_updates_last_check_success_timestamp_seconds 50" in lines
    assert not any("last_check_started" in line for line in lines)
    assert 'ayeops_image_updates_tags{result="error"} 1' in lines
    assert "ayeops_image_updates_notifications_total 3" in lines


def test_label_values_are_escaped() -> None:
    assert agent.metric("m", 1, container='a"b\\c\nd') == 'm{container="a\\"b\\\\c\\nd"} 1\n'


def test_state_round_trips_and_a_broken_file_starts_empty(tmp_path: Path) -> None:
    state = agent.State(digests={"x:1": PG}, check_success=5.0, counts={"new": 1}, notifications=2)
    assert agent.State.from_json(state.to_json()) == state
    with pytest.raises(ValueError, match="not an object"):
        agent.State.from_json("[]")
    path = tmp_path / "state.json"
    path.write_text("{not json", encoding="utf-8")
    settings = make_settings(tmp_path, state=path)
    assert agent.Agent(settings, fleet()).state == agent.State()


def make_settings(tmp_path: Path, **overrides: Any) -> Any:
    values = {
        "docker": "http://proxy:2375",
        "listen": ("127.0.0.1", 0),
        "state": tmp_path / "state.json",
        "images": tmp_path / "images.json",
        "textfile": tmp_path / "out.prom",
        "interval": 60.0,
    }
    values.update(overrides)
    return agent.Settings(**values)


def test_settings_come_from_the_environment_and_refuse_a_fast_loop() -> None:
    settings = agent.settings_from(
        {"AYE_IMAGES_LISTEN": "0.0.0.0:9000", "AYE_IMAGES_INTERVAL": "30"}
    )
    assert settings.listen == ("0.0.0.0", 9000)
    assert settings.interval == 30
    assert settings.docker == "http://image-updates-proxy:2375"
    assert settings.images == Path("/data/images.json")
    with pytest.raises(ValueError, match="at least 10"):
        agent.settings_from({"AYE_IMAGES_INTERVAL": "5"})
    with pytest.raises(ValueError, match="host:port"):
        agent.settings_from({"AYE_IMAGES_LISTEN": "9000"})


def test_refresh_rewrites_the_diun_list_only_when_it_changes(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    worker = agent.Agent(settings, fleet(), wall=lambda: 7.0)
    worker.refresh()
    listed = settings.images.read_text(encoding="utf-8")
    assert json.loads(listed)[1]["name"] == "docker.io/library/postgres:18"
    assert not agent.write_if_changed(settings.images, listed)
    assert agent.write_if_changed(settings.images, "[]")
    assert agent.write_if_changed(tmp_path / "fresh.json", "[]")
    assert "ayeops_image_updates_last_scan_timestamp_seconds 7" in settings.textfile.read_text(
        encoding="utf-8"
    )


def test_a_notification_before_the_first_scan_is_kept_without_metrics(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    worker = agent.Agent(settings, fleet())
    body = json.dumps({"status": "new", "image": "postgres:18", "digest": PG}).encode()
    assert worker.notification(body)
    assert not settings.textfile.exists()
    assert json.loads(settings.state.read_text(encoding="utf-8"))["digests"] == {
        "docker.io/library/postgres:18": PG
    }


@pytest.fixture()
def served(tmp_path: Path) -> Iterator[tuple[Any, int]]:
    settings = make_settings(tmp_path)
    worker = agent.Agent(settings, fleet(), wall=lambda: 9.0)
    worker.refresh()
    server = ThreadingHTTPServer(("127.0.0.1", 0), agent.handler_for(worker))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield worker, server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()


def request(port: int, method: str, path: str, body: bytes = b"") -> int:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=WAIT)
    try:
        connection.request(method, path, body=body)
        return connection.getresponse().status
    finally:
        connection.close()


def test_the_receiver_takes_diun_webhooks_and_pings_and_refuses_the_rest(
    served: tuple[Any, int],
) -> None:
    worker, port = served
    body = json.dumps(
        {"status": "update", "image": "docker.io/library/postgres:18", "digest": PG_NEW}
    )
    assert request(port, "POST", "/diun", body.encode()) == 204
    assert request(port, "POST", "/hc/diun", SUCCESS_LOGS.encode()) == 200
    assert request(port, "POST", "/hc/diun/sideways") == 404
    assert request(port, "POST", "/diun", b"{broken") == 400
    assert request(port, "POST", "/elsewhere", b"{}") == 404
    assert request(port, "GET", "/health") == 200
    assert request(port, "GET", "/diun") == 404
    text = worker.settings.textfile.read_text(encoding="utf-8")
    assert DB_PENDING in text
    assert "ayeops_image_updates_last_check_success_timestamp_seconds 9" in text


def test_the_receiver_refuses_a_body_above_a_megabyte(served: tuple[Any, int]) -> None:
    _, port = served
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=WAIT)
    try:
        connection.putrequest("POST", "/diun")
        connection.putheader("Content-Length", str(agent.MAX_BODY + 1))
        connection.endheaders()
        assert connection.getresponse().status == 413
    finally:
        connection.close()


class FakeConnection:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self.body = body
        self.requested: list[tuple[str, str]] = []
        self.closed = False

    def request(self, method: str, path: str) -> None:
        self.requested.append((method, path))

    def getresponse(self) -> Any:
        return self

    def read(self) -> bytes:
        return self.body

    def close(self) -> None:
        self.closed = True


def test_the_docker_api_only_reads_and_reports_errors() -> None:
    made: list[tuple[str, int, float, FakeConnection]] = []

    def connect(status: int, body: bytes) -> Any:
        def open_(host: str, port: int, timeout: float) -> FakeConnection:
            connection = FakeConnection(status, body)
            made.append((host, port, timeout, connection))
            return connection

        return open_

    api = agent.docker_api("http://proxy:2375", 3.0, connect(200, b'[{"Id": "x"}]'))
    assert api("/containers/json") == [{"Id": "x"}]
    host, port, timeout, connection = made[-1]
    assert (host, port, timeout, connection.requested, connection.closed) == (
        "proxy",
        2375,
        3.0,
        [("GET", "/containers/json")],
        True,
    )
    assert agent.docker_api("http://proxy", 1.0, connect(200, b""))("/x") is None
    assert made[-1][1] == 2375
    with pytest.raises(RuntimeError, match="403"):
        agent.docker_api("http://proxy:2375", 1.0, connect(403, b"forbidden"))("/containers/json")
    with pytest.raises(ValueError, match="http://host:port"):
        agent.docker_api("unix:///var/run/docker.sock")


def test_serve_touches_the_heartbeat_only_after_a_good_scan(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)

    def broken(path: str) -> Any:
        raise RuntimeError("GET /containers/json: 403")

    heartbeat = tmp_path / "alive"
    turns = iter([True, False])
    agent.serve(agent.Agent(settings, broken), lambda _: None, heartbeat, lambda: next(turns))
    assert not heartbeat.exists()
    turns = iter([True, False])
    naps: list[float] = []
    agent.serve(agent.Agent(settings, fleet()), naps.append, heartbeat, lambda: next(turns))
    assert heartbeat.exists()
    assert naps == [60.0]


def with_module(data: dict[str, Any], **settings: Any) -> dict[str, Any]:
    data["hosts"]["edge"]["modules"]["image-updates"] = settings
    return data


def services(data: dict[str, Any], modules: dict[str, Module]) -> dict[str, Any]:
    rendered: dict[str, Any] = host_compose(manifest_from(data), "edge", modules)["services"]
    return rendered


def test_the_example_runs_the_module_and_passes_every_rule(data: dict[str, Any]) -> None:
    assert "image-updates" in data["hosts"]["edge"]["modules"]
    assert rules_hit(data) == set()


def test_the_proxy_reads_the_socket_read_only_and_allows_only_containers_and_images(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    proxy = services(data, modules)["image-updates-proxy"]
    assert proxy["volumes"] == ["/var/run/docker.sock:/var/run/docker.sock:ro"]
    allowed = {
        key for key, value in proxy["environment"].items() if value == "1" and key != "DISABLE_IPV6"
    }
    assert allowed == {"CONTAINERS", "IMAGES", "PING"}
    assert proxy["environment"]["POST"] == "0"
    assert proxy["networks"] == ["image-updates-docker"]
    assert "ports" not in proxy
    assert (proxy["read_only"], proxy["cap_drop"]) == (True, ["ALL"])


def test_only_the_agent_reaches_the_proxy_and_the_agent_has_no_internet(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    compose = host_compose(manifest_from(data), "edge", modules)
    rendered = compose["services"]
    near_docker = sorted(
        name
        for name, service in rendered.items()
        if "image-updates-docker" in (service.get("networks") or [])
    )
    assert near_docker == ["image-updates-agent", "image-updates-proxy"]
    assert rendered["image-updates-agent"]["networks"] == ["image-updates", "image-updates-docker"]
    assert compose["networks"]["image-updates"] == {"internal": True}
    assert compose["networks"]["image-updates-docker"] == {"internal": True}
    socket_users = sorted(
        name
        for name, service in rendered.items()
        if any("docker.sock" in str(volume) for volume in service.get("volumes") or [])
        and service["labels"]["io.ayeops.module"] == "image-updates"
    )
    assert socket_users == ["image-updates-proxy"]


def test_diun_reads_the_agents_list_and_reports_back_to_it(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    rendered = services(with_module(data, schedule="03:07", timezone="Europe/Kyiv"), modules)
    diun = rendered["diun"]
    environment = diun["environment"]
    assert environment["DIUN_WATCH_SCHEDULE"] == "7 3 * * *"
    assert environment["TZ"] == "Europe/Kyiv"
    assert (
        environment["DIUN_PROVIDERS_FILE_FILENAME"]
        == rendered["image-updates-agent"]["environment"]["AYE_IMAGES_FILE"]
    )
    assert environment["DIUN_NOTIF_WEBHOOK_ENDPOINT"] == "http://image-updates-agent:8098/diun"
    assert environment["DIUN_WATCH_HEALTHCHECKS_BASEURL"] == "http://image-updates-agent:8098/hc/"
    assert environment["DIUN_WATCH_FIRSTCHECKNOTIF"] == "true"
    assert environment["DIUN_WATCH_COMPAREDIGEST"] == "true"
    assert diun["networks"] == ["default", "image-updates"]
    assert not any(key.startswith("DIUN_PROVIDERS_DOCKER") for key in environment)
    assert not any(key.startswith("DIUN_REGOPTS") for key in environment)
    assert "image-updates-data:/data" in diun["volumes"]
    assert "image-updates-data:/data" in rendered["image-updates-agent"]["volumes"]


def test_private_ghcr_images_need_a_user_and_a_token_from_the_environment(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    environment = services(with_module(data, ghcr_username="acme-bot"), modules)["diun"][
        "environment"
    ]
    assert environment["DIUN_REGOPTS_0_NAME"] == "ghcr.io"
    assert environment["DIUN_REGOPTS_0_SELECTOR"] == "image"
    assert environment["DIUN_REGOPTS_0_USERNAME"] == "acme-bot"
    assert (
        environment["DIUN_REGOPTS_0_PASSWORD"]
        == "${AYE_IMAGE_UPDATES_GHCR_TOKEN:?set AYE_IMAGE_UPDATES_GHCR_TOKEN}"
    )


def test_every_image_of_the_module_is_pinned_to_an_exact_version(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    rendered = services(data, modules)
    for name in ("image-updates-proxy", "image-updates-agent", "diun"):
        assert EXACT.match(tag_of(rendered[name]["image"])), rendered[name]["image"]
        assert agent.is_floating(rendered[name]["image"]) is False


def test_the_agent_healthcheck_follows_the_scan_interval(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    rendered = services(with_module(data, interval=20), modules)
    assert rendered["image-updates-agent"]["environment"]["AYE_IMAGES_INTERVAL"] == "20"
    assert "> 90)" in rendered["image-updates-agent"]["healthcheck"]["test"][-1]


@pytest.mark.parametrize(
    "settings",
    [
        {"schedule": "4:15"},
        {"schedule": "24:00"},
        {"interval": 5},
        {"interval": 7200},
        {"ghcr_username": "not a user"},
        {"timezone": "Europe Kyiv"},
        {"socket": "/var/run/docker.sock"},
    ],
)
def test_bad_settings_are_refused(data: dict[str, Any], settings: dict[str, Any]) -> None:
    assert rules_hit(with_module(data, **settings)) == {"module-settings"}
