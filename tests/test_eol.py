from __future__ import annotations

import email.message
import importlib.util
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, date, datetime
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from ayeops.eol import BASES, IMAGES, Component, image_components, split_reference, tracked
from ayeops.registry import Module
from ayeops.render import EOL_AGENT, host_compose, scanned_images
from tests.conftest import ROOT, manifest_from, pinned, rules_hit, tag_of

MODULE = ROOT / "src" / "ayeops" / "modules" / "eol"
AGENT = MODULE / "files" / "eol_agent.py"
FIXTURES = ROOT / "tests" / "fixtures" / "eol"
CAPTURED = datetime(2026, 11, 5, 12, 0, tzinfo=UTC).timestamp()
TODAY = date(2026, 11, 5)


def load_agent() -> ModuleType:
    spec = importlib.util.spec_from_file_location("ayeops.modules.eol.files.eol_agent", AGENT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


agent = load_agent()


def fixture(product: str) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads((FIXTURES / f"{product}.json").read_text(encoding="utf-8"))
    return loaded


def body(*releases: dict[str, Any]) -> dict[str, Any]:
    return {"result": {"name": "x", "releases": list(releases)}}


def release(name: str, **fields: Any) -> dict[str, Any]:
    return {"name": name, "isEol": False, "eolFrom": None, **fields}


@pytest.mark.parametrize(
    ("reference", "expected"),
    [
        ("postgres:16", ("postgres", "16")),
        ("docker.io/library/postgres:17-alpine", ("postgres", "17-alpine")),
        ("docker.io/valkey/valkey:8.1", ("valkey/valkey", "8.1")),
        ("index.docker.io/library/redis:7.4", ("redis", "7.4")),
        ("library/node:22", ("node", "22")),
        ("ghcr.io/acme/web:1.2", ("ghcr.io/acme/web", "1.2")),
        ("registry:5000/team/app:3", ("registry:5000/team/app", "3")),
        ("registry:5000/app", ("registry:5000/app", "")),
        ("python@sha256:abc", ("python", "")),
        ("python:3.13@sha256:abc", ("python", "3.13")),
        ("quay.io/keycloak/keycloak:26.4", ("quay.io/keycloak/keycloak", "26.4")),
    ],
)
def test_split_reference_names_the_repository_without_the_hub_and_keeps_the_tag(
    reference: str, expected: tuple[str, str]
) -> None:
    assert split_reference(reference) == expected


def pairs(reference: str) -> list[tuple[str, str]]:
    return [(c.product, c.cycle) for c in image_components(reference)]


@pytest.mark.parametrize(
    ("reference", "expected"),
    [
        ("postgres:18.6-alpine3.24", [("postgresql", "18"), ("alpine-linux", "3.24")]),
        ("postgres:16", [("postgresql", "16")]),
        ("postgres:9.6.24", [("postgresql", "9.6")]),
        ("postgres:latest", []),
        ("postgres", []),
        ("postgres:07", []),
        ("redis:7.4.2-alpine", [("redis", "7.4")]),
        ("redis:7", []),
        ("valkey/valkey:8.1-alpine3.22", [("valkey", "8.1"), ("alpine-linux", "3.22")]),
        ("mariadb:11.4", [("mariadb", "11.4")]),
        ("mysql:8.0.40", [("mysql", "8.0")]),
        ("mongo:8.0.4", [("mongodb", "8.0")]),
        ("node:20-alpine", [("nodejs", "20")]),
        ("node:22.11.0-bookworm-slim", [("nodejs", "22"), ("debian", "12")]),
        ("node:lts", []),
        ("node:22rc", []),
        ("python:3.13.16-alpine3.23", [("python", "3.13"), ("alpine-linux", "3.23")]),
        ("python:3.12-slim-trixie", [("python", "3.12"), ("debian", "13")]),
        ("python:3", []),
        ("nginx:1.29.2-alpine", [("nginx", "1.29")]),
        (
            "nginxinc/nginx-unprivileged:1.31.5-alpine3.24",
            [("nginx", "1.31"), ("alpine-linux", "3.24")],
        ),
        ("traefik:v3.7.14", [("traefik", "3.7")]),
        ("grafana/grafana:13.2.3", [("grafana", "13.2")]),
        ("grafana/grafana-oss:12.4.0", [("grafana", "12.4")]),
        ("grafana/loki:3.7.8", [("grafana-loki", "3.7")]),
        ("prom/prometheus:v3.5.0", [("prometheus", "3.5")]),
        ("alpine:3.22.1", [("alpine-linux", "3.22")]),
        ("alpine:latest", []),
        ("debian:12.7-slim", [("debian", "12")]),
        ("debian:bookworm-slim", [("debian", "12")]),
        ("debian:bullseye-20251020", [("debian", "11")]),
        ("debian:stable", []),
        ("debian:12-bookworm", [("debian", "12")]),
        ("ubuntu:24.04", [("ubuntu", "24.04")]),
        ("ubuntu:jammy-20250101", [("ubuntu", "22.04")]),
        ("ubuntu:rolling", []),
        ("golang:1.25.3-alpine3.22", [("go", "1.25"), ("alpine-linux", "3.22")]),
        ("golang:1.24-noble", [("go", "1.24"), ("ubuntu", "24.04")]),
        ("php:8.3-fpm-alpine", [("php", "8.3")]),
        ("caddy:2.10-alpine", [("caddy", "2")]),
        ("eclipse-temurin:21-jre", [("eclipse-temurin", "21")]),
        ("docker.elastic.co/elasticsearch/elasticsearch:8.19.0", [("elasticsearch", "8.19")]),
        ("quay.io/keycloak/keycloak:26.4.2", [("keycloak", "26.4")]),
        ("rabbitmq:4.1-management", [("rabbitmq", "4.1")]),
        ("ghcr.io/acme/web:1.2-alpine3.20", [("alpine-linux", "3.20")]),
        ("ghcr.io/acme/web:1.2", []),
        ("ghcr.io/acme/web:1.2-myalpine3.20", []),
        ("quay.io/oauth2-proxy/oauth2-proxy:v7.15.5-alpine", []),
        ("prom/node-exporter:v1.12.1", []),
        ("example/alpine3.20:1", []),
    ],
)
def test_image_components_read_the_cycle_only_where_the_tag_carries_it(
    reference: str, expected: list[tuple[str, str]]
) -> None:
    assert pairs(reference) == expected
    assert all(c.name == reference for c in image_components(reference))


def test_every_product_of_the_mapping_exists_on_endoflife_date() -> None:
    names = set(json.loads((FIXTURES / "product-names.json").read_text(encoding="utf-8"))["names"])
    products = {rule.product for rule in IMAGES.values()} | {product for _, product, _ in BASES}
    products |= {product for product, _ in agent.OS_PRODUCTS.values()}
    assert sorted(products - names) == []


def test_tracked_lists_unknown_images_once_and_adds_manual_entries_once() -> None:
    manual = [
        {"name": "app", "product": "django", "cycle": "5.2"},
        {"name": "app", "product": "django", "cycle": "5.2"},
        {"name": "postgres:16", "product": "postgresql", "cycle": "16"},
    ]
    components, unmapped = tracked(
        ["z/unknown:1", "postgres:16", "a/unknown:2", "postgres:16"], manual
    )
    assert components == [
        Component("postgres:16", "postgresql", "16"),
        Component("app", "django", "5.2"),
    ]
    assert unmapped == ["a/unknown:2", "z/unknown:1"]
    assert Component("a", "b", "c").as_json() == {"name": "a", "product": "b", "cycle": "c"}


OS_RELEASE = """PRETTY_NAME="Ubuntu 22.04.5 LTS"
NAME="Ubuntu"
VERSION_ID="22.04"
# a comment line
ID=ubuntu
ID_LIKE='debian'

BROKEN
=nokey
"""


def test_parse_os_release_strips_quotes_and_skips_comments_and_junk() -> None:
    fields = agent.parse_os_release(OS_RELEASE)
    assert fields == {
        "PRETTY_NAME": "Ubuntu 22.04.5 LTS",
        "NAME": "Ubuntu",
        "VERSION_ID": "22.04",
        "ID": "ubuntu",
        "ID_LIKE": "debian",
    }
    assert agent.parse_os_release('A="\nB=x"\nC=""') == {"A": '"', "B": 'x"', "C": ""}


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ({"ID": "ubuntu", "VERSION_ID": "22.04"}, ("ubuntu", "22.04")),
        ({"ID": "Ubuntu ", "VERSION_ID": " 24.04"}, ("ubuntu", "24.04")),
        ({"ID": "debian", "VERSION_ID": "12"}, ("debian", "12")),
        ({"ID": "raspbian", "VERSION_ID": "12"}, ("debian", "12")),
        ({"ID": "alpine", "VERSION_ID": "3.20.3"}, ("alpine-linux", "3.20")),
        ({"ID": "rhel", "VERSION_ID": "9.4"}, ("rhel", "9")),
        ({"ID": "rocky", "VERSION_ID": "9.4"}, ("rocky-linux", "9")),
        ({"ID": "almalinux", "VERSION_ID": "10.0"}, ("almalinux", "10")),
        ({"ID": "centos", "VERSION_ID": "7"}, ("centos", "7")),
        ({"ID": "fedora", "VERSION_ID": "42"}, ("fedora", "42")),
        ({"ID": "amzn", "VERSION_ID": "2023"}, ("amazon-linux", "2023")),
        ({"ID": "opensuse-leap", "VERSION_ID": "15.6"}, ("opensuse", "15.6")),
        ({"ID": "alpine", "VERSION_ID": "3"}, None),
        ({"ID": "debian"}, None),
        ({"ID": "linuxmint", "VERSION_ID": "21.3"}, None),
        ({"ID": "arch", "VERSION_ID": "rolling"}, None),
        ({"ID": "ubuntu", "VERSION_ID": "22.04 LTS"}, None),
        ({}, None),
    ],
)
def test_os_component_maps_the_distribution_and_its_cycle(
    fields: dict[str, str], expected: tuple[str, str] | None
) -> None:
    found = agent.os_component(fields)
    if expected is None:
        assert found is None
    else:
        assert found == agent.Component("os", *expected)


def test_host_os_reads_the_file_and_reports_whether_it_was_wanted(tmp_path: Path) -> None:
    assert agent.host_os(None) == (None, False)
    assert agent.host_os(tmp_path / "missing") == (None, True)
    path = tmp_path / "os-release"
    path.write_text(OS_RELEASE, encoding="utf-8")
    assert agent.host_os(path) == (agent.Component("os", "ubuntu", "22.04"), True)
    path.write_bytes(b"\xff\xfe")
    assert agent.host_os(path) == (None, True)


@pytest.mark.parametrize(
    ("product", "cycle", "known", "security", "support", "ended"),
    [
        ("postgresql", "14", True, date(2026, 11, 12), date(2026, 11, 12), False),
        ("postgresql", "13", True, date(2025, 11, 13), date(2025, 11, 13), True),
        ("postgresql", "18", True, date(2030, 11, 14), date(2030, 11, 14), False),
        ("postgresql", "99", False, None, None, False),
        ("nodejs", "20", True, date(2026, 4, 30), date(2024, 10, 22), True),
        ("nodejs", "24", True, date(2028, 4, 30), date(2026, 10, 20), False),
        ("ubuntu", "22.04", True, date(2027, 6, 1), date(2024, 9, 30), False),
    ],
)
def test_evaluate_reads_the_captured_endoflife_responses(
    product: str,
    cycle: str,
    known: bool,
    security: date | None,
    support: date | None,
    ended: bool,
) -> None:
    component = agent.Component("c", product, cycle)
    status = agent.evaluate(component, fixture(product), TODAY)
    assert status == agent.Status(component, known, security, support, ended)


def test_evaluate_counts_a_passed_date_as_ended_and_an_unknown_date_as_absent() -> None:
    component = agent.Component("c", "p", "1")
    today = date(2026, 1, 10)
    on_the_day = agent.evaluate(component, body(release("1", eolFrom="2026-01-10")), today)
    assert on_the_day is not None and on_the_day.ended
    before = agent.evaluate(component, body(release("1", eolFrom="2026-01-11")), today)
    assert before is not None and not before.ended
    flagged = agent.evaluate(component, body(release("1", isEol=True)), today)
    assert flagged == agent.Status(component, True, None, None, True)
    no_active = agent.evaluate(
        component, body(release("1", eolFrom="2027-01-01", eoasFrom=None)), today
    )
    assert no_active == agent.Status(component, True, date(2027, 1, 1), None, False)
    garbled = agent.evaluate(component, body(release("1", eolFrom="soon", isEol="yes")), today)
    assert garbled == agent.Status(component, True, None, None, False)
    numeric = agent.evaluate(
        agent.Component("c", "p", "8"), body({"name": 8, "eolFrom": None}), today
    )
    assert numeric is not None and numeric.known


@pytest.mark.parametrize(
    "value",
    [None, [], {}, {"result": None}, {"result": {"releases": {}}}, {"result": []}],
)
def test_evaluate_without_a_release_list_returns_nothing(value: Any) -> None:
    assert agent.evaluate(agent.Component("c", "p", "1"), value, TODAY) is None


def test_releases_drops_entries_that_are_not_objects() -> None:
    assert agent.releases({"result": {"releases": [1, "x", {"name": "a"}]}}) == [{"name": "a"}]


def test_render_metrics_write_days_left_per_component_and_fetch_state() -> None:
    soon = agent.Status(
        agent.Component("postgres:14", "postgresql", "14"),
        True,
        date(2026, 11, 12),
        date(2026, 11, 12),
        False,
    )
    passed = agent.Status(
        agent.Component("os", "ubuntu", "20.04"), True, date(2025, 5, 31), None, True
    )
    unknown = agent.Status(agent.Component("app", "django", "9.9"), False)
    state = agent.State(1000.0, 2000.4, {"django": True, "postgresql": False})
    data = {"postgresql": agent.Cached(1500.0, body())}
    text = agent.render_metrics([soon, passed, unknown], ["a:1", "os"], state, data, CAPTURED)
    assert text.splitlines() == [
        "ayeops_eol_last_success_timestamp_seconds 1000",
        "ayeops_eol_last_fetch_timestamp_seconds 2000",
        "ayeops_eol_last_fetch_success 0",
        "ayeops_eol_unmapped_components 2",
        'ayeops_eol_unmapped{component="a:1"} 1',
        'ayeops_eol_unmapped{component="os"} 1',
        'ayeops_eol_fetch_success{product="django"} 1',
        'ayeops_eol_fetch_success{product="postgresql"} 0',
        'ayeops_eol_data_timestamp_seconds{product="postgresql"} 1500',
        'ayeops_eol_cycle_known{component="postgres:14",cycle="14",product="postgresql"} 1',
        'ayeops_eol_security_ended{component="postgres:14",cycle="14",product="postgresql"} 0',
        "ayeops_eol_security_days_left{"
        'component="postgres:14",cycle="14",date="2026-11-12",product="postgresql"} 7',
        "ayeops_eol_support_days_left{"
        'component="postgres:14",cycle="14",date="2026-11-12",product="postgresql"} 7',
        'ayeops_eol_cycle_known{component="os",cycle="20.04",product="ubuntu"} 1',
        'ayeops_eol_security_ended{component="os",cycle="20.04",product="ubuntu"} 1',
        "ayeops_eol_security_days_left{"
        'component="os",cycle="20.04",date="2025-05-31",product="ubuntu"} -523',
        'ayeops_eol_cycle_known{component="app",cycle="9.9",product="django"} 0',
    ]


def test_render_metrics_before_the_first_fetch_carry_only_the_clock() -> None:
    assert agent.render_metrics([], [], agent.State(12.0), {}, CAPTURED) == (
        "ayeops_eol_last_success_timestamp_seconds 12\nayeops_eol_unmapped_components 0\n"
    )


def test_metric_escapes_label_values_and_keeps_fractions() -> None:
    assert (
        agent.metric("x", 1.5, component='a"b\\c\nd')
        == 'ayeops_eol_x{component="a\\"b\\\\c d"} 1.5\n'
    )
    assert agent.metric("x", 2.0) == "ayeops_eol_x 2\n"


def test_today_is_the_utc_date() -> None:
    assert agent.today_of(datetime(2026, 11, 5, 23, 59, tzinfo=UTC).timestamp()) == TODAY
    assert agent.today_of(datetime(2026, 11, 6, 0, 0, tzinfo=UTC).timestamp()) == date(2026, 11, 6)


class Response:
    def __init__(self, data: bytes, headers: dict[str, str] | None = None) -> None:
        self.data = data
        self.headers = headers or {}
        self.asked: int | None = None

    def read(self, size: int = -1) -> bytes:
        self.asked = size
        return self.data if size < 0 else self.data[:size]

    def __enter__(self) -> Response:
        return self

    def __exit__(self, *args: object) -> None:
        return None


class Opener:
    def __init__(self, *answers: Any) -> None:
        self.answers = list(answers)
        self.requests: list[urllib.request.Request] = []
        self.timeouts: list[float] = []

    def __call__(self, request: urllib.request.Request, timeout: float) -> Any:
        self.requests.append(request)
        self.timeouts.append(timeout)
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer


def http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://x", code, "x", email.message.Message(), None)


def raw(product: str) -> bytes:
    return (FIXTURES / f"{product}.json").read_bytes()


def test_fetch_asks_the_product_url_and_keeps_the_validators() -> None:
    opener = Opener(
        Response(raw("postgresql"), {"ETag": '"abc"', "Last-Modified": "Sat, 31 Oct 2026"})
    )
    fetched = agent.fetch("https://e.test/api/v1", "postgresql", None, 50.0, opener)
    assert fetched.ok and fetched.error == ""
    assert fetched.cached == agent.Cached(50.0, fixture("postgresql"), '"abc"', "Sat, 31 Oct 2026")
    request = opener.requests[0]
    assert request.full_url == "https://e.test/api/v1/products/postgresql"
    assert request.get_header("User-agent") == agent.USER_AGENT
    assert request.get_header("Accept") == "application/json"
    assert request.get_header("If-none-match") is None
    assert request.get_header("If-modified-since") is None
    assert opener.timeouts == [agent.TIMEOUT]


def test_fetch_sends_the_validators_and_a_304_renews_the_cache() -> None:
    previous = agent.Cached(10.0, body(release("1")), '"abc"', "Sat, 31 Oct 2026")
    opener = Opener(http_error(304))
    fetched = agent.fetch("https://e.test", "p", previous, 99.0, opener)
    assert fetched == agent.Fetched(
        True, agent.Cached(99.0, previous.body, '"abc"', "Sat, 31 Oct 2026")
    )
    assert opener.requests[0].get_header("If-none-match") == '"abc"'
    assert opener.requests[0].get_header("If-modified-since") == "Sat, 31 Oct 2026"


@pytest.mark.parametrize(
    ("answer", "error"),
    [
        (http_error(304), "HTTP 304"),
        (http_error(404), "HTTP 404"),
        (http_error(503), "HTTP 503"),
        (urllib.error.URLError("no route"), "URLError"),
        (TimeoutError(), "TimeoutError"),
        (ValueError("bad url"), "ValueError"),
        (Response(b"<html>"), "not JSON"),
        (Response(b'{"result": {}}'), "no releases in the response"),
        (Response(b" " * (agent.LIMIT + 1)), "response too large"),
    ],
)
def test_a_failed_fetch_keeps_what_was_cached(answer: Any, error: str) -> None:
    previous = None if error == "HTTP 304" else agent.Cached(10.0, body(release("1")))
    fetched = agent.fetch("https://e.test", "p", previous, 99.0, Opener(answer))
    assert fetched == agent.Fetched(False, previous, error)


def test_a_response_of_exactly_the_limit_is_read() -> None:
    data = raw("postgresql")
    padded = data + b" " * (agent.LIMIT - len(data))
    assert agent.fetch("https://e.test", "postgresql", None, 1.0, Opener(Response(padded))).ok


def test_fetch_reads_one_byte_past_the_limit() -> None:
    response = Response(raw("nodejs"))
    agent.fetch("https://e.test", "nodejs", None, 1.0, Opener(response))
    assert response.asked == agent.LIMIT + 1


def test_the_cache_round_trips_and_refuses_broken_files(tmp_path: Path) -> None:
    path = tmp_path / "p.json"
    cached = agent.Cached(5.0, fixture("ubuntu"), "e", "m")
    agent.write_cached(path, cached)
    assert agent.read_cached(path) == cached
    assert not path.with_name("p.json.tmp").exists()
    path.write_text(
        json.dumps({"fetched": 1, "body": body(), "etag": 3, "modified": None}), encoding="utf-8"
    )
    assert agent.read_cached(path) == agent.Cached(1.0, body(), None, None)
    for broken in [
        "{",
        "[]",
        '{"body": {}}',
        '{"fetched": "x", "body": {}}',
        '{"fetched": 1, "body": {}}',
    ]:
        path.write_text(broken, encoding="utf-8")
        assert agent.read_cached(path) is None
    assert agent.read_cached(tmp_path / "missing.json") is None


def test_the_state_round_trips_and_refuses_broken_files(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    state = agent.State(1.0, 2.0, {"a": True, "b": False})
    agent.write_state(path, state)
    assert agent.read_state(path) == state
    path.write_text('{"last_success": 3, "results": {"a": 1}}', encoding="utf-8")
    assert agent.read_state(path) == agent.State(3.0, None, {"a": False})
    for broken in ["{", "[]", "{}", '{"last_success": "x"}', '{"last_success": 1, "results": [1]}']:
        path.write_text(broken, encoding="utf-8")
        assert agent.read_state(path) is None
    assert agent.State(1.0).ok
    assert not agent.State(1.0, 2.0, {"a": True, "b": False}).ok


def settings(tmp_path: Path, *components: tuple[str, str, str], **extra: Any) -> Any:
    return agent.Settings(
        cache=tmp_path / "cache",
        textfile=tmp_path / "eol.prom",
        os_release=extra.pop("os_release", None),
        components=tuple(agent.Component(*c) for c in components),
        **extra,
    )


class Clock:
    def __init__(self, start: float) -> None:
        self.now = start
        self.slept: list[float] = []

    def wall(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def test_refresh_keeps_the_last_data_and_success_time_when_a_fetch_fails(tmp_path: Path) -> None:
    target = settings(tmp_path)
    clock = Clock(100.0)
    ok = Opener(Response(raw("nodejs")), Response(raw("postgresql")))
    state, errors = agent.refresh(
        target, ["nodejs", "postgresql"], agent.State(1.0), clock.wall, ok
    )
    assert errors == {}
    assert state == agent.State(100.0, 100.0, {"nodejs": True, "postgresql": True})
    assert agent.read_state(target.state) == state
    clock.now = 200.0
    failing = Opener(Response(raw("nodejs")), http_error(500))
    later, errors = agent.refresh(target, ["nodejs", "postgresql"], state, clock.wall, failing)
    assert errors == {"postgresql": "HTTP 500"}
    assert later == agent.State(100.0, 200.0, {"nodejs": True, "postgresql": False})
    kept = agent.read_cached(agent.cache_file(target.cache, "postgresql"))
    assert kept is not None and kept.fetched == 100.0 and kept.body == fixture("postgresql")
    renewed = agent.read_cached(agent.cache_file(target.cache, "nodejs"))
    assert renewed is not None and renewed.fetched == 200.0


def test_refresh_without_products_counts_as_success(tmp_path: Path) -> None:
    state, errors = agent.refresh(settings(tmp_path), [], agent.State(1.0), lambda: 7.0, Opener())
    assert (state, errors) == (agent.State(7.0, 7.0, {}), {})


def test_settings_come_from_the_environment() -> None:
    parsed = agent.settings_from(
        {
            "AYE_EOL_SCHEDULE": "07:15",
            "AYE_EOL_API": "https://mirror.test/api/v1/",
            "AYE_EOL_CACHE": "/c",
            "AYE_EOL_TEXTFILE": "/t/e.prom",
            "AYE_EOL_OS_RELEASE": "",
            "AYE_EOL_COMPONENTS": '[{"name": "a:1", "product": "redis", "cycle": "7.4"}]',
            "AYE_EOL_UNMAPPED": '["b:2"]',
        }
    )
    assert parsed == agent.Settings(
        (7, 15),
        "https://mirror.test/api/v1",
        Path("/c"),
        Path("/t/e.prom"),
        None,
        (agent.Component("a:1", "redis", "7.4"),),
        ("b:2",),
    )
    assert parsed.state == Path("/c/state.json")
    assert agent.settings_from({}) == agent.Settings()


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("AYE_EOL_COMPONENTS", "{}"),
        ("AYE_EOL_COMPONENTS", "[1]"),
        ("AYE_EOL_COMPONENTS", '[{"name": "a", "product": "p"}]'),
        ("AYE_EOL_COMPONENTS", '[{"name": "", "product": "p", "cycle": "1"}]'),
        ("AYE_EOL_COMPONENTS", '[{"name": "a", "product": "../etc", "cycle": "1"}]'),
        ("AYE_EOL_COMPONENTS", '[{"name": "a", "product": "P", "cycle": 1}]'),
        ("AYE_EOL_UNMAPPED", '"a"'),
        ("AYE_EOL_UNMAPPED", '[""]'),
        ("AYE_EOL_UNMAPPED", "[1]"),
        ("AYE_EOL_SCHEDULE", "25:00"),
    ],
)
def test_settings_refuse_malformed_values(key: str, value: str) -> None:
    with pytest.raises(ValueError):
        agent.settings_from({key: value})


def test_tracked_adds_the_host_os_or_lists_it_as_unmapped(tmp_path: Path) -> None:
    path = tmp_path / "os-release"
    path.write_text("ID=debian\nVERSION_ID=12\n", encoding="utf-8")
    target = settings(tmp_path, ("a:1", "redis", "7.4"), os_release=path, unmapped=("z:1", "b:1"))
    assert agent.tracked(target) == (
        [agent.Component("a:1", "redis", "7.4"), agent.Component("os", "debian", "12")],
        ["b:1", "z:1"],
    )
    path.write_text("ID=gentoo\n", encoding="utf-8")
    assert agent.tracked(target)[1] == ["b:1", "os", "z:1"]
    assert agent.tracked(settings(tmp_path)) == ([], [])


def test_products_are_fetched_once_each() -> None:
    components = [agent.Component(n, p, "1") for n, p in [("a", "y"), ("b", "x"), ("c", "y")]]
    assert agent.products_of(components) == ["x", "y"]


def at(hour: int, minute: int) -> float:
    return time.mktime((2026, 11, 5, hour, minute, 0, 0, 0, -1))


def test_next_run_is_today_before_the_schedule_and_tomorrow_after_it() -> None:
    assert agent.next_run(at(5, 0), 5, 30) == at(5, 30)
    assert agent.next_run(at(5, 30), 5, 30) == time.mktime((2026, 11, 6, 5, 30, 0, 0, 0, -1))
    assert agent.parse_schedule(" 05:30 ") == (5, 30)


def test_first_due_fetches_at_once_without_a_fresh_cache(tmp_path: Path) -> None:
    target = settings(tmp_path)
    now = at(12, 0)
    tomorrow = time.mktime((2026, 11, 6, 5, 30, 0, 0, 0, -1))
    assert agent.first_due(target, [], now) == tomorrow
    assert agent.first_due(target, ["postgresql"], now) == now
    agent.prepare(target, now)
    path = agent.cache_file(target.cache, "postgresql")
    agent.write_cached(path, agent.Cached(now - agent.DAY + 1, fixture("postgresql")))
    assert agent.first_due(target, ["postgresql"], now) == tomorrow
    agent.write_cached(path, agent.Cached(now - agent.DAY, fixture("postgresql")))
    assert agent.first_due(target, ["postgresql"], now) == now


def test_prepare_starts_the_clock_once(tmp_path: Path) -> None:
    target = settings(tmp_path)
    assert agent.prepare(target, 10.0) == agent.State(10.0)
    assert (target.cache / "products").is_dir()
    assert agent.prepare(target, 20.0) == agent.State(10.0)


def loops(count: int) -> Any:
    left = [count]

    def keep_going() -> bool:
        left[0] -= 1
        return left[0] >= 0

    return keep_going


def test_serve_fetches_writes_metrics_and_waits_for_the_schedule(tmp_path: Path) -> None:
    target = settings(
        tmp_path,
        ("postgres:14", "postgresql", "14"),
        ("app", "postgresql", "99"),
        ("old", "postgresql", "13"),
    )
    state = agent.prepare(target, at(12, 0))
    clock = Clock(at(12, 0))
    lines: list[dict[str, Any]] = []
    opener = Opener(Response(raw("postgresql")))
    agent.serve(
        target, state, clock.wall, clock.sleep, loops(1), tmp_path / "alive", opener, lines.append
    )
    assert (tmp_path / "alive").exists()
    assert len(opener.requests) == 1
    assert lines == [
        {
            "event": "eol-fetch",
            "success": True,
            "failed": {},
            "soon": {"postgresql 14 (postgres:14)": 7},
            "ended": ["postgresql 13 (old)"],
            "unknown": ["postgresql 99 (app)"],
        }
    ]
    text = target.textfile.read_text(encoding="utf-8")
    assert (
        'ayeops_eol_security_days_left{component="postgres:14",cycle="14",date="2026-11-12"' in text
    )
    assert f"ayeops_eol_last_success_timestamp_seconds {round(at(12, 0))}" in text
    assert clock.slept == [agent.NAP]


def test_serve_retries_a_failed_fetch_after_an_hour_and_keeps_the_old_success(
    tmp_path: Path,
) -> None:
    target = settings(tmp_path, ("postgres:14", "postgresql", "14"))
    state = agent.prepare(target, 5.0)
    start = at(12, 0)
    clock = Clock(start)
    lines: list[dict[str, Any]] = []
    opener = Opener(http_error(502), Response(raw("postgresql")))
    agent.serve(
        target,
        state,
        clock.wall,
        clock.sleep,
        lambda: clock.now < start + agent.RETRY + 1,
        tmp_path / "alive",
        opener,
        lines.append,
    )
    assert [line["success"] for line in lines] == [False, True]
    assert lines[0]["failed"] == {"postgresql": "HTTP 502"}
    assert len(opener.requests) == 2
    assert agent.read_state(target.state) == agent.State(
        start + agent.RETRY, start + agent.RETRY, {"postgresql": True}
    )


def test_serve_rewrites_the_days_left_every_hour_from_the_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    start = datetime(2026, 11, 5, 23, 30, tzinfo=UTC).timestamp()
    earlier = time.localtime(start - 60)
    target = settings(
        tmp_path, ("postgres:14", "postgresql", "14"), schedule=(earlier.tm_hour, earlier.tm_min)
    )
    state = agent.prepare(target, 5.0)
    path = agent.cache_file(target.cache, "postgresql")
    agent.write_cached(path, agent.Cached(start - 60, fixture("postgresql")))
    clock = Clock(start)
    written: list[float] = []
    original = agent.write_metrics

    def recording(settings_: Any, state_: Any, now: float) -> Any:
        written.append(now)
        return original(settings_, state_, now)

    monkeypatch.setattr(agent, "write_metrics", recording)
    agent.serve(
        target,
        state,
        clock.wall,
        clock.sleep,
        lambda: clock.now < start + agent.REWRITE + agent.NAP,
        tmp_path / "alive",
        Opener(),
        print,
    )
    assert written == [start, start + agent.REWRITE]
    text = target.textfile.read_text(encoding="utf-8")
    assert 'date="2026-11-12",product="postgresql"} 6\n' in text


def test_main_once_fetches_from_a_file_api_and_writes_the_textfile(tmp_path: Path) -> None:
    api = tmp_path / "api"
    (api / "products").mkdir(parents=True)
    (api / "products" / "postgresql").write_bytes(raw("postgresql"))
    environ = {
        "AYE_EOL_ONCE": "1",
        "AYE_EOL_API": api.as_uri(),
        "AYE_EOL_CACHE": str(tmp_path / "cache"),
        "AYE_EOL_TEXTFILE": str(tmp_path / "eol.prom"),
        "AYE_EOL_OS_RELEASE": "",
        "AYE_EOL_COMPONENTS": '[{"name": "pg", "product": "postgresql", "cycle": "18"}]',
    }
    assert agent.main(environ) == 0
    text = (tmp_path / "eol.prom").read_text(encoding="utf-8")
    assert 'ayeops_eol_fetch_success{product="postgresql"} 1' in text
    assert 'ayeops_eol_cycle_known{component="pg",cycle="18",product="postgresql"} 1' in text
    environ["AYE_EOL_COMPONENTS"] = '[{"name": "x", "product": "nosuch", "cycle": "1"}]'
    assert agent.main(environ) == 1
    assert 'ayeops_eol_fetch_success{product="nosuch"} 0' in (tmp_path / "eol.prom").read_text(
        encoding="utf-8"
    )


def eol_services(data: dict[str, Any], modules: dict[str, Module], host: str = "edge") -> Any:
    return host_compose(manifest_from(data), host, modules)["services"]


def tracked_of(rendered: dict[str, Any]) -> tuple[list[dict[str, str]], list[str]]:
    environment = rendered[EOL_AGENT]["environment"]
    return json.loads(environment["AYE_EOL_COMPONENTS"]), json.loads(
        environment["AYE_EOL_UNMAPPED"]
    )


def test_the_example_runs_the_module_on_edge_and_passes_every_rule(data: dict[str, Any]) -> None:
    assert "eol" in data["hosts"]["edge"]["modules"]
    assert "eol" not in data["hosts"]["central"]["modules"]
    assert rules_hit(data) == set()


def test_the_agent_gets_the_mapped_images_the_manual_entries_and_the_rest_as_unmapped(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    rendered = eol_services(data, modules)
    components, unmapped = tracked_of(rendered)
    images = {tag_of(image) for image in scanned_images(manifest_from(data), "edge", rendered, {})}
    assert {c["name"] for c in components} | set(unmapped) == images | {"acme web app"}
    python = pinned("eol", "python")
    assert {"name": python, "product": "python", "cycle": "3.13"} in components
    assert {"name": python, "product": "alpine-linux", "cycle": "3.23"} in components
    assert {"name": "acme web app", "product": "django", "cycle": "5.2"} in components
    assert pinned("node", "prom/node-exporter") in unmapped
    assert not {c["name"] for c in components} & set(unmapped)


def test_the_images_of_the_hosts_compose_files_are_mapped_too(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    data["hosts"]["central"]["modules"]["eol"] = {}
    components, unmapped = tracked_of(eol_services(data, modules, "central"))
    assert {"name": "nginx:1.29.2-alpine", "product": "nginx", "cycle": "1.29"} in components
    assert {"name": "python:3.13.9-slim", "product": "python", "cycle": "3.13"} in components
    grafana = pinned("grafana", "grafana/grafana")
    assert {"name": grafana, "product": "grafana", "cycle": "13.2"} in components
    assert {
        "name": pinned("oncall", "postgres"),
        "product": "postgresql",
        "cycle": "18",
    } in components
    assert pinned("mcp", "busybox") in unmapped


def test_exclude_drops_an_image_from_both_lists(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    node_exporter = pinned("node", "prom/node-exporter")
    data["hosts"]["edge"]["modules"]["eol"]["exclude"] = ["python", node_exporter]
    components, unmapped = tracked_of(eol_services(data, modules))
    assert not any(c["name"].startswith("python:") for c in components)
    assert node_exporter not in unmapped
    assert {"name": "acme web app", "product": "django", "cycle": "5.2"} in components


def test_the_agent_reads_the_host_os_release_read_only_unless_turned_off(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    agent_service = eol_services(data, modules)[EOL_AGENT]
    assert "/etc/os-release:/host/os-release:ro" in agent_service["volumes"]
    assert agent_service["environment"]["AYE_EOL_OS_RELEASE"] == "/host/os-release"
    data["hosts"]["edge"]["modules"]["eol"]["os"] = False
    agent_service = eol_services(data, modules)[EOL_AGENT]
    assert all("os-release" not in volume for volume in agent_service["volumes"])
    assert agent_service["environment"]["AYE_EOL_OS_RELEASE"] == ""


def test_the_agent_is_locked_down_and_keeps_its_cache_in_a_volume(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    compose = host_compose(manifest_from(data), "edge", modules)
    service = compose["services"][EOL_AGENT]
    assert tag_of(service["image"]) == pinned("eol", "python")
    assert service["read_only"] is True
    assert service["cap_drop"] == ["ALL"]
    assert "ports" not in service and "network_mode" not in service
    assert "networks" not in service
    assert "eol-cache:/cache" in service["volumes"]
    assert "eol-cache" in compose["volumes"]
    assert "/var/lib/ayeops/textfile:/textfile" in service["volumes"]
    environment = service["environment"]
    assert environment["AYE_EOL_API"] == "https://endoflife.date/api/v1"
    assert environment["AYE_EOL_SCHEDULE"] == "05:30"
    assert environment["AYE_EOL_TEXTFILE"] == "/textfile/ayeops_eol.prom"
    assert environment["TZ"] == "Europe/Berlin"


def test_a_host_without_the_module_gets_no_agent(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    assert EOL_AGENT not in eol_services(data, modules, "central")


def test_the_module_needs_node_for_its_textfile(data: dict[str, Any]) -> None:
    data["hosts"]["central"]["modules"]["eol"] = {}
    del data["hosts"]["central"]["modules"]["node"]
    del data["hosts"]["central"]["modules"]["reconciler"]
    assert rules_hit(data) == {"module-requires"}


@pytest.mark.parametrize(
    ("settings_", "rule"),
    [
        ({"components": [{"name": "a", "product": "Django", "cycle": "5.2"}]}, "module-settings"),
        ({"components": [{"name": "a", "product": "django"}]}, "module-settings"),
        ({"api": "http://endoflife.date/api/v1"}, "module-settings"),
        ({"api": "https://endoflife.date/api/v1/"}, "module-settings"),
        ({"schedule": "5:30"}, "module-settings"),
        ({"os": "yes"}, "module-settings"),
        ({"timezone": "Mars/Olympus"}, "timezone"),
    ],
)
def test_bad_settings_fail_the_check(
    data: dict[str, Any], settings_: dict[str, Any], rule: str
) -> None:
    data["hosts"]["edge"]["modules"]["eol"] = settings_
    assert rule in rules_hit(data)


def test_the_alerts_are_warnings_with_the_thresholds_of_the_docs(
    modules: dict[str, Module],
) -> None:
    alerts = {alert["name"]: alert for alert in modules["eol"].alerts}
    assert set(alerts) == {"eol-soon", "eol-passed", "eol-cycle-unknown", "eol-data-stale"}
    assert {alert["severity"] for alert in alerts.values()} == {"warning"}
    assert "(ayeops_eol_security_days_left > 0) < bool 90" in alerts["eol-soon"]["expr"]
    assert "ayeops_eol_security_ended" in alerts["eol-passed"]["expr"]
    assert "== bool 0" in alerts["eol-cycle-unknown"]["expr"]
    assert "> bool 259200" in alerts["eol-data-stale"]["expr"]
    assert 3 * agent.DAY == 259200
    assert agent.SOON == 90


@pytest.mark.parametrize(
    ("text", "expected"),
    [("00:00", (0, 0)), ("23:59", (23, 59)), ("19:07", (19, 7)), ("\t05:30\n", (5, 30))],
)
def test_parse_schedule_reads_the_hour_and_the_minute(text: str, expected: tuple[int, int]) -> None:
    assert agent.parse_schedule(text) == expected


@pytest.mark.parametrize("text", ["24:00", "5:30", "05:60", "05:30:00", "0530", ""])
def test_parse_schedule_names_the_text_it_refuses(text: str) -> None:
    with pytest.raises(ValueError) as refused:
        agent.parse_schedule(text)
    assert str(refused.value) == f"not an HH:MM time: {text!r}"


def test_next_run_moves_to_tomorrow_only_once_the_minute_is_reached() -> None:
    tomorrow = time.mktime((2026, 11, 6, 5, 30, 0, 0, 0, -1))
    assert agent.next_run(at(5, 30) - 1, 5, 30) == at(5, 30)
    assert agent.next_run(at(5, 30) + 1, 5, 30) == tomorrow
    assert agent.next_run(at(23, 59), 0, 0) == time.mktime((2026, 11, 6, 0, 0, 0, 0, 0, -1))
    assert agent.next_run(at(0, 0), 23, 59) == at(23, 59)
    assert tomorrow - at(5, 30) == agent.DAY


def test_parse_os_release_unquotes_only_a_matching_pair_of_quotes() -> None:
    text = '#OFF=1\n  A=  b  \nB=\'c d\'\nC=XaX\nD="e\'\nE=\'\nF=aba\nG=""x""\nH=a=b\nI=\n'
    assert agent.parse_os_release(text) == {
        "A": "b",
        "B": "c d",
        "C": "XaX",
        "D": "\"e'",
        "E": "'",
        "F": "aba",
        "G": '"x"',
        "H": "a=b",
        "I": "",
    }
    assert agent.parse_os_release("") == {}
    assert agent.parse_os_release("A=1\nA=2\n") == {"A": "2"}


def test_a_cached_validator_that_is_not_text_is_dropped_and_a_text_one_kept(
    tmp_path: Path,
) -> None:
    path = tmp_path / "p.json"
    path.write_text(
        json.dumps({"fetched": "2.5", "body": body(), "etag": "e", "modified": 5}),
        encoding="utf-8",
    )
    assert agent.read_cached(path) == agent.Cached(2.5, body(), "e", None)
    path.write_text(
        json.dumps({"fetched": 2, "body": body(), "etag": ["e"], "modified": "m"}),
        encoding="utf-8",
    )
    assert agent.read_cached(path) == agent.Cached(2.0, body(), None, "m")


def test_write_atomic_goes_through_the_tmp_file_next_to_the_target(tmp_path: Path) -> None:
    path = tmp_path / "eol.prom"
    path.write_text("old", encoding="utf-8")
    accented = b"caf\xc3\xa9 \xe2\x9c\x93"
    agent.write_atomic(path, accented.decode("utf-8"))
    assert path.read_bytes() == accented
    assert sorted(entry.name for entry in tmp_path.iterdir()) == ["eol.prom"]
    (tmp_path / "eol.prom.tmp").mkdir()
    with pytest.raises(OSError):
        agent.write_atomic(path, "new")
    assert path.read_bytes() == accented


def test_the_cache_and_the_state_are_written_as_json_with_sorted_keys(tmp_path: Path) -> None:
    path = tmp_path / "p.json"
    agent.write_cached(path, agent.Cached(5.0, {"result": {"releases": []}}, "e", None))
    assert path.read_text(encoding="utf-8") == (
        '{"body": {"result": {"releases": []}}, "etag": "e", "fetched": 5.0, "modified": null}'
    )
    agent.write_cached(path, agent.Cached(6.5, {"z": 1, "result": {"releases": []}}, None, "m"))
    assert path.read_text(encoding="utf-8") == (
        '{"body": {"result": {"releases": []}, "z": 1}, "etag": null, "fetched": 6.5,'
        ' "modified": "m"}'
    )
    state = tmp_path / "state.json"
    agent.write_state(state, agent.State(1.0, 2.0, {"b": False, "a": True}))
    assert state.read_text(encoding="utf-8") == (
        '{"last_attempt": 2.0, "last_success": 1.0, "results": {"a": true, "b": false}}'
    )
    agent.write_state(state, agent.State(3.0))
    assert state.read_text(encoding="utf-8") == (
        '{"last_attempt": null, "last_success": 3.0, "results": {}}'
    )


def test_the_default_opener_opens_the_request_with_the_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def fake(*args: Any, **kwargs: Any) -> str:
        seen.append((args, kwargs))
        return "answer"

    monkeypatch.setattr(agent.urllib.request, "urlopen", fake)
    request = urllib.request.Request("https://e.test/products/p")
    assert agent.default_opener(request, 4.5) == "answer"
    assert seen == [((request,), {"timeout": 4.5})]


@pytest.mark.parametrize(
    ("previous", "validators"),
    [
        (None, {}),
        ((None, None), {}),
        (("", ""), {}),
        (('"e"', None), {"If-None-Match": '"e"'}),
        ((None, "Sat, 31 Oct 2026"), {"If-Modified-Since": "Sat, 31 Oct 2026"}),
        (('"e"', "m"), {"If-None-Match": '"e"', "If-Modified-Since": "m"}),
    ],
)
def test_the_request_names_its_headers_as_http_spells_them(
    previous: tuple[str | None, str | None] | None,
    validators: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    built: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def fake(*args: Any, **kwargs: Any) -> str:
        built.append((args, kwargs))
        return "request"

    monkeypatch.setattr(agent.urllib.request, "Request", fake)
    cached = None if previous is None else agent.Cached(1.0, body(), *previous)
    assert agent.request_for("https://e.test/api/v1", "redis", cached) == "request"
    headers = {
        "User-Agent": "ayeops-eol/1 (+https://github.com/AyeOps/ayeops)",
        "Accept": "application/json",
        **validators,
    }
    assert built == [(("https://e.test/api/v1/products/redis",), {"headers": headers})]
    assert list(built[0][1]["headers"]) == list(headers)


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("{}", "AYE_EOL_COMPONENTS must be a JSON list"),
        ('"redis"', "AYE_EOL_COMPONENTS must be a JSON list"),
        ("[1]", "a component needs name, product and cycle: 1"),
        (
            '[{"name": "a", "product": "p"}]',
            "a component needs name, product and cycle: {'name': 'a', 'product': 'p'}",
        ),
        (
            '[{"name": "a", "product": "p", "cycle": ""}]',
            "a component needs name, product and cycle: {'name': 'a', 'product': 'p', 'cycle': ''}",
        ),
        (
            '[{"name": "a", "product": "../etc", "cycle": "1"}]',
            "not an endoflife.date product: '../etc'",
        ),
        (
            '[{"name": "a", "product": "Redis", "cycle": "1"}]',
            "not an endoflife.date product: 'Redis'",
        ),
    ],
)
def test_a_malformed_component_list_is_refused_with_the_reason(text: str, message: str) -> None:
    with pytest.raises(ValueError) as refused:
        agent.components_from(text)
    assert str(refused.value) == message


def test_components_keep_their_order_and_take_name_product_and_cycle_by_key() -> None:
    text = json.dumps(
        [
            {"cycle": "7.4", "product": "redis", "name": "cache:7.4", "extra": 1},
            {"name": "db", "product": "postgresql", "cycle": "18"},
        ]
    )
    assert agent.components_from(text) == (
        agent.Component("cache:7.4", "redis", "7.4"),
        agent.Component("db", "postgresql", "18"),
    )
    assert agent.components_from("[]") == ()


@pytest.mark.parametrize("text", ['"a"', "{}", '[""]', "[1]", '["a", null]', '["a", ""]'])
def test_a_malformed_unmapped_list_is_refused_with_the_reason(text: str) -> None:
    with pytest.raises(ValueError) as refused:
        agent.unmapped_from(text)
    assert str(refused.value) == "AYE_EOL_UNMAPPED must be a JSON list of image references"


def test_unmapped_images_are_kept_in_their_order() -> None:
    assert agent.unmapped_from('["z/app:1", "a:2"]') == ("z/app:1", "a:2")
    assert agent.unmapped_from("[]") == ()


def test_settings_strip_only_the_slashes_at_the_end_of_the_api() -> None:
    parsed = agent.settings_from({"AYE_EOL_API": "https://mirror.test/apiX//"})
    assert parsed.api == "https://mirror.test/apiX"
    assert agent.settings_from({"AYE_EOL_OS_RELEASE": "/etc/os-release"}).os_release == Path(
        "/etc/os-release"
    )
    assert agent.Settings() == agent.Settings(
        (5, 30),
        "https://endoflife.date/api/v1",
        Path("/cache"),
        Path("/textfile/ayeops_eol.prom"),
        Path("/host/os-release"),
        (),
        (),
    )


tzset = getattr(time, "tzset", None)


@pytest.mark.skipif(tzset is None, reason="needs time.tzset to move the local zone")
def test_today_is_the_utc_date_in_a_zone_far_from_utc(monkeypatch: pytest.MonkeyPatch) -> None:
    assert tzset is not None
    monkeypatch.setenv("TZ", "AYE-14")
    tzset()
    try:
        assert time.localtime(CAPTURED).tm_mday == 6
        assert agent.today_of(CAPTURED) == TODAY
    finally:
        monkeypatch.undo()
        tzset()


def test_the_summary_lists_what_ends_within_ninety_days_what_ended_and_what_is_unknown() -> None:
    def status(name: str, known: bool, days: int | None = None, ended: bool = False) -> Any:
        security = None if days is None else date.fromordinal(TODAY.toordinal() + days)
        return agent.Status(agent.Component(name, "p", "1"), known, security, None, ended)

    found = [
        status("z-ended", True, -1, True),
        status("soon", True, 89),
        status("today", True, 0),
        status("later", True, 90),
        status("open", True),
        status("z-unknown", False),
        status("a-unknown", False, 5),
        status("a-ended", True, None, True),
    ]
    failed = agent.State(1.0, 2.0, {"p": False})
    assert agent.summary(found, failed, {"p": "HTTP 500"}, TODAY) == {
        "event": "eol-fetch",
        "success": False,
        "failed": {"p": "HTTP 500"},
        "soon": {"p 1 (soon)": 89, "p 1 (today)": 0},
        "ended": ["p 1 (a-ended)", "p 1 (z-ended)"],
        "unknown": ["p 1 (a-unknown)", "p 1 (z-unknown)"],
    }
    assert agent.summary([], agent.State(1.0), {}, TODAY) == {
        "event": "eol-fetch",
        "success": True,
        "failed": {},
        "soon": {},
        "ended": [],
        "unknown": [],
    }


EMPTY_LINE = '{"ended": [], "event": "eol-fetch", "failed": {}, "soon": {}, "success": true, '
EMPTY_LINE += '"unknown": []}\n'


def test_serve_fetches_at_the_scheduled_second_and_prints_one_sorted_json_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = settings(tmp_path, schedule=(5, 30))
    clock = Clock(at(5, 30) - 10)
    state = agent.prepare(target, clock.now)
    agent.serve(target, state, clock.wall, clock.sleep, loops(2), tmp_path / "alive", Opener())
    assert clock.slept == [10, agent.NAP]
    assert capsys.readouterr().out == EMPTY_LINE
    assert agent.read_state(target.state) == agent.State(at(5, 30), at(5, 30), {})


def test_serve_never_sleeps_less_than_a_second_or_more_than_a_nap(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = settings(tmp_path, schedule=(5, 30))
    clock = Clock(at(5, 30) - 30.25)
    state = agent.prepare(target, clock.now)
    agent.serve(target, state, clock.wall, clock.sleep, loops(2), tmp_path / "alive", Opener())
    assert clock.slept == [30, 1.0]
    assert capsys.readouterr().out == ""
    assert target.textfile.exists()
    again = Clock(at(5, 30) - 30.25)
    agent.serve(target, state, again.wall, again.sleep, loops(3), tmp_path / "alive", Opener())
    assert again.slept == [30, 1.0, 30]
    assert capsys.readouterr().out == EMPTY_LINE


def main_environ(tmp_path: Path, **extra: str) -> dict[str, str]:
    return {
        "AYE_EOL_CACHE": str(tmp_path / "cache"),
        "AYE_EOL_TEXTFILE": str(tmp_path / "eol.prom"),
        "AYE_EOL_OS_RELEASE": "",
        **extra,
    }


@pytest.mark.parametrize("ok", [True, False])
def test_main_once_runs_one_fetch_prints_its_line_and_exits_by_its_success(
    ok: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[tuple[Any, ...]] = []
    after = agent.State(1.0, 2.0, {"p": ok})

    def once(*args: Any) -> tuple[Any, dict[str, Any]]:
        calls.append(args)
        return after, {"b": 1, "a": [2]}

    def never(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("served")

    monkeypatch.setattr(agent, "run_once", once)
    monkeypatch.setattr(agent, "serve", never)
    environ = main_environ(tmp_path, AYE_EOL_ONCE="1")
    assert agent.main(environ) == (0 if ok else 1)
    assert capsys.readouterr().out == '{"a": [2], "b": 1}\n'
    assert len(calls) == 1
    target, state, wall, opener = calls[0]
    assert target == agent.settings_from(environ)
    assert state == agent.read_state(target.state)
    assert isinstance(state.last_success, float) and state.last_attempt is None
    assert wall is time.time
    assert opener is agent.default_opener


@pytest.mark.parametrize("once", [None, "", "0", "11", "true", " 1"])
def test_main_serves_for_ever_unless_once_is_exactly_one(
    once: str | None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    served: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def serve(*args: Any, **kwargs: Any) -> None:
        served.append((args, kwargs))

    def never(*args: Any) -> None:
        raise AssertionError("fetched once")

    monkeypatch.setattr(agent, "serve", serve)
    monkeypatch.setattr(agent, "run_once", never)
    environ = main_environ(tmp_path) if once is None else main_environ(tmp_path, AYE_EOL_ONCE=once)
    assert agent.main(environ) == 0
    target = agent.settings_from(environ)
    state = agent.read_state(target.state)
    assert isinstance(state, agent.State) and isinstance(state.last_success, float)
    assert served == [((target, state), {})]
    assert capsys.readouterr().out == ""
