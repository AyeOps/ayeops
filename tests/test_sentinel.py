from __future__ import annotations

import email.message
import importlib.util
import json
import re
import ssl
import sys
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import ModuleType, SimpleNamespace
from typing import Any, ClassVar

import pytest

from tests.conftest import ROOT, WAIT

SCRIPT = ROOT / "src" / "ayeops" / "modules" / "sentinel" / "files" / "sentinel.py"
GRAFANA = "https://grafana.ops.acme.example/api/health"
VM_HEALTH = "http://10.8.0.1:8428/health"
VM_QUERY = "http://10.8.0.1:8428/api/v1/query"
PING = "https://hc-ping.com/uuid"
START = 1_767_225_600.0
HOUR = 3600.0


def load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("ayeops.modules.sentinel.files.sentinel", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


s = load_script()


def vm_answer(age: float) -> bytes:
    return json.dumps(
        {
            "status": "success",
            "data": {"resultType": "vector", "result": [{"metric": {}, "value": [1, str(age)]}]},
        }
    ).encode()


class FakeClock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeHttp:
    def __init__(self) -> None:
        self.answers: dict[str, Any] = {}
        self.calls: list[str] = []
        self.timeouts: list[float] = []
        self.healthy()

    def healthy(self) -> None:
        self.answers = {
            GRAFANA: (200, b'{\n  "database": "ok",\n  "version": "13.2.3"\n}'),
            VM_HEALTH: (200, b"OK"),
            VM_QUERY: (200, vm_answer(4.2)),
            PING: (200, b"OK"),
        }

    def dead(self) -> None:
        for url in (GRAFANA, VM_HEALTH, VM_QUERY):
            self.answers[url] = TimeoutError("timed out")

    def __call__(self, url: str, timeout: float) -> tuple[int, bytes]:
        self.calls.append(url)
        self.timeouts.append(timeout)
        answer = self.answers[url.split("?", 1)[0]]
        if isinstance(answer, BaseException):
            raise answer
        status, body = answer
        return int(status), bytes(body)


class FakeTelegram:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.working = True

    def __call__(self, text: str) -> bool:
        if self.working:
            self.sent.append(text)
        return self.working


CHECKS = (
    s.Check("grafana", "http", url=GRAFANA, contains='"database": "ok"'),
    s.Check("victoriametrics", "http", url=VM_HEALTH),
    s.Check("samples", "fresh", url=VM_QUERY, max_age_seconds=180),
)


def config(**changes: Any) -> Any:
    base = s.Config(
        project="acme",
        origin="edge",
        watch="central",
        checks=CHECKS,
        ping_url=PING,
        runbook="https://runbooks.example/sentinel-central-down.md",
    )
    return s.replace(base, **changes)


def no_expiry(host: str, port: int, timeout: float) -> float:
    raise AssertionError("no tls check configured")


@pytest.fixture
def rig() -> tuple[Any, FakeClock, FakeHttp, FakeTelegram]:
    clock = FakeClock()
    http = FakeHttp()
    telegram = FakeTelegram()
    return s.Sentinel(config(), http, no_expiry, telegram, clock), clock, http, telegram


def minutes(sentinel: Any, clock: FakeClock, count: int) -> list[str | None]:
    notices = []
    for _ in range(count):
        notices.append(sentinel.tick())
        clock.advance(60)
    return notices


def test_three_failed_rounds_send_one_alert_then_reminders_every_six_hours_then_all_clear(
    rig: tuple[Any, FakeClock, FakeHttp, FakeTelegram],
) -> None:
    sentinel, clock, http, telegram = rig
    assert minutes(sentinel, clock, 2) == [None, None]
    http.dead()
    assert minutes(sentinel, clock, 2) == [None, None]
    assert telegram.sent == []
    assert sentinel.tick() == "alert"
    assert len(telegram.sent) == 1
    alert = telegram.sent[0]
    assert alert.startswith("🔴 acme: central is not answering\n")
    assert "3 checks in a row failed since 2026-01-01 00:02 UTC:" in alert
    assert "• grafana: TimeoutError: timed out" in alert
    assert "• samples: TimeoutError: timed out" in alert
    assert "Seen from edge" in alert
    assert alert.endswith("Runbook: https://runbooks.example/sentinel-central-down.md")
    clock.advance(60)
    notices = minutes(sentinel, clock, 6 * 60 - 1)
    assert set(notices) == {None}
    assert len(telegram.sent) == 1
    assert sentinel.tick() == "reminder"
    assert telegram.sent[1].startswith(
        "🔴 acme: central is still not answering, down for 6h 2m\nFailing now:\n"
    )
    clock.advance(60)
    assert set(minutes(sentinel, clock, 6 * 60 - 1)) == {None}
    assert sentinel.tick() == "reminder"
    assert len(telegram.sent) == 3
    clock.advance(60)
    http.healthy()
    assert sentinel.tick() == "recovery"
    assert telegram.sent[3] == (
        "✅ acme: central answers again after 12h 3m\nSeen from edge by the AyeOps sentinel."
    )
    clock.advance(60)
    assert minutes(sentinel, clock, 3) == [None, None, None]
    assert len(telegram.sent) == 4


def test_a_passing_round_inside_the_streak_starts_the_count_again(
    rig: tuple[Any, FakeClock, FakeHttp, FakeTelegram],
) -> None:
    sentinel, clock, http, telegram = rig
    http.dead()
    minutes(sentinel, clock, 2)
    http.healthy()
    minutes(sentinel, clock, 1)
    http.dead()
    assert minutes(sentinel, clock, 2) == [None, None]
    assert sentinel.tick() == "alert"
    assert "3 checks in a row failed since 2026-01-01 00:03 UTC" in telegram.sent[0]


def test_one_failing_check_is_enough_and_only_it_is_listed(
    rig: tuple[Any, FakeClock, FakeHttp, FakeTelegram],
) -> None:
    sentinel, clock, http, telegram = rig
    http.answers[VM_QUERY] = (200, vm_answer(412))
    assert minutes(sentinel, clock, 3) == [None, None, "alert"]
    assert telegram.sent[0].count("• ") == 1
    assert "• samples: newest sample is 6m old" in telegram.sent[0]


def test_an_undelivered_alert_is_sent_again_next_round(
    rig: tuple[Any, FakeClock, FakeHttp, FakeTelegram],
) -> None:
    sentinel, clock, http, telegram = rig
    http.dead()
    telegram.working = False
    assert minutes(sentinel, clock, 4) == [None, None, "alert", "alert"]
    assert sentinel.state.alerting is False
    telegram.working = True
    assert sentinel.tick() == "alert"
    assert len(telegram.sent) == 1
    assert sentinel.notice_failures["alert"] == 2
    assert sentinel.notices["alert"] == 1


def test_an_undelivered_reminder_is_sent_again_next_round(
    rig: tuple[Any, FakeClock, FakeHttp, FakeTelegram],
) -> None:
    sentinel, clock, http, telegram = rig
    http.dead()
    minutes(sentinel, clock, 3)
    clock.advance(6 * HOUR)
    telegram.working = False
    assert sentinel.tick() == "reminder"
    clock.advance(60)
    telegram.working = True
    assert sentinel.tick() == "reminder"
    clock.advance(60)
    assert sentinel.tick() is None
    assert len(telegram.sent) == 2


def test_an_undelivered_all_clear_is_sent_on_the_next_passing_round(
    rig: tuple[Any, FakeClock, FakeHttp, FakeTelegram],
) -> None:
    sentinel, clock, http, telegram = rig
    http.dead()
    minutes(sentinel, clock, 3)
    http.healthy()
    telegram.working = False
    assert sentinel.tick() == "recovery"
    assert sentinel.state.alerting is True
    assert sentinel.state.streak == 0
    clock.advance(60)
    telegram.working = True
    assert sentinel.tick() == "recovery"
    assert telegram.sent[-1].startswith("✅ acme: central answers again after 4m\n")
    assert sentinel.state == s.State()


def test_a_relapse_after_an_undelivered_all_clear_keeps_the_alert_and_its_reminder_clock(
    rig: tuple[Any, FakeClock, FakeHttp, FakeTelegram],
) -> None:
    sentinel, clock, http, telegram = rig
    http.dead()
    minutes(sentinel, clock, 3)
    http.healthy()
    telegram.working = False
    minutes(sentinel, clock, 1)
    telegram.working = True
    http.dead()
    assert sentinel.tick() is None
    assert len(telegram.sent) == 1
    assert sentinel.state.alerted_at == START + 2 * 60


def test_ping_only_after_a_round_where_every_check_passed(
    rig: tuple[Any, FakeClock, FakeHttp, FakeTelegram],
) -> None:
    sentinel, clock, http, _ = rig
    minutes(sentinel, clock, 1)
    assert http.calls.count(PING) == 1
    http.answers[VM_HEALTH] = (502, b"")
    minutes(sentinel, clock, 5)
    assert http.calls.count(PING) == 1
    http.healthy()
    minutes(sentinel, clock, 1)
    assert http.calls.count(PING) == 2
    assert sentinel.ping_ok == 1


def test_a_failing_ping_shows_in_the_metrics_but_does_not_alert(
    rig: tuple[Any, FakeClock, FakeHttp, FakeTelegram],
) -> None:
    sentinel, clock, http, telegram = rig
    http.answers[PING] = OSError("unreachable")
    minutes(sentinel, clock, 5)
    assert telegram.sent == []
    assert 'ayeops_sentinel_ping_success{watch="central"} 0' in sentinel.metrics()
    http.answers[PING] = (404, b"")
    minutes(sentinel, clock, 1)
    assert sentinel.ping_ok == 0


def test_no_ping_url_means_no_ping_and_no_ping_metric() -> None:
    http = FakeHttp()
    sentinel = s.Sentinel(config(ping_url=""), http, no_expiry, FakeTelegram(), FakeClock())
    sentinel.tick()
    assert PING not in http.calls
    assert "ayeops_sentinel_ping_success" not in sentinel.metrics()


def test_metrics_report_each_check_the_streak_and_the_notices(
    rig: tuple[Any, FakeClock, FakeHttp, FakeTelegram],
) -> None:
    sentinel, clock, http, _ = rig
    http.answers[GRAFANA] = (200, b'{"database": "failing"}')
    minutes(sentinel, clock, 3)
    text = sentinel.metrics()
    assert 'ayeops_sentinel_check_success{check="grafana",watch="central"} 0' in text
    assert 'ayeops_sentinel_check_success{check="samples",watch="central"} 1' in text
    assert 'ayeops_sentinel_failure_streak{watch="central"} 3' in text
    assert 'ayeops_sentinel_alerting{watch="central"} 1' in text
    assert 'ayeops_sentinel_notices_total{kind="alert",watch="central"} 1' in text
    assert 'ayeops_sentinel_notice_failures_total{kind="alert",watch="central"} 0' in text
    assert 'ayeops_sentinel_last_notice_ok{watch="central"} 1' in text
    assert "# TYPE ayeops_sentinel_notices_total counter" in text
    assert "# TYPE ayeops_sentinel_alerting gauge" in text
    assert (
        f'ayeops_sentinel_last_round_timestamp_seconds{{watch="central"}} {int(START + 120)}'
        in (text)
    )


def test_a_failed_telegram_send_marks_last_notice_ok_zero(
    rig: tuple[Any, FakeClock, FakeHttp, FakeTelegram],
) -> None:
    sentinel, clock, http, telegram = rig
    telegram.working = False
    http.dead()
    minutes(sentinel, clock, 3)
    assert 'ayeops_sentinel_last_notice_ok{watch="central"} 0' in sentinel.metrics()


def test_health_turns_bad_when_rounds_stop(
    rig: tuple[Any, FakeClock, FakeHttp, FakeTelegram],
) -> None:
    sentinel = rig[0]
    sentinel.tick()
    assert sentinel.healthy(START + 3 * 60 + 30)
    assert sentinel.healthy(START + 3 * 60 + 30 + 1) is False


def test_tls_check_reads_the_certificate_expiry() -> None:
    clock = FakeClock()
    expiries: list[tuple[str, int]] = []

    def expiry(host: str, port: int, timeout: float) -> float:
        expiries.append((host, port))
        return START + 3 * 86400

    tls = s.Check("tls", "tls", host="grafana.ops.acme.example", min_days=7)
    sentinel = s.Sentinel(config(checks=(tls,)), FakeHttp(), expiry, FakeTelegram(), clock)
    outcome = sentinel.probe(tls)
    assert expiries == [("grafana.ops.acme.example", 443)]
    assert (outcome.ok, outcome.detail) == (False, "certificate expires in 3.0 days")


def test_a_probe_that_crashes_is_a_failure_not_a_crash() -> None:
    def broken(url: str, timeout: float) -> tuple[int, bytes]:
        raise ValueError("bad url")

    sentinel = s.Sentinel(config(), broken, no_expiry, FakeTelegram(), FakeClock())
    outcome = sentinel.probe(CHECKS[0])
    assert (outcome.ok, outcome.detail) == (False, "ValueError: bad url")


def test_fresh_check_with_http_error_or_bad_json_fails() -> None:
    http = FakeHttp()
    sentinel = s.Sentinel(config(), http, no_expiry, FakeTelegram(), FakeClock())
    http.answers[VM_QUERY] = (503, b"")
    assert sentinel.probe(CHECKS[2]).detail == "HTTP 503"
    http.answers[VM_QUERY] = (200, b"<html>")
    assert sentinel.probe(CHECKS[2]).ok is False


def test_fresh_check_sends_the_query_url_encoded() -> None:
    http = FakeHttp()
    sentinel = s.Sentinel(config(), http, no_expiry, FakeTelegram(), FakeClock())
    sentinel.probe(CHECKS[2])
    assert http.calls == [VM_QUERY + "?query=time%28%29+-+max%28timestamp%28up%29%29"]


def test_probe_duration_is_measured_with_the_clock() -> None:
    clock = FakeClock()

    def slow(url: str, timeout: float) -> tuple[int, bytes]:
        clock.advance(2.5)
        return 200, b"OK"

    sentinel = s.Sentinel(config(), slow, no_expiry, FakeTelegram(), clock)
    assert sentinel.probe(CHECKS[1]).seconds == 2.5


@pytest.mark.parametrize(
    ("text", "seconds"),
    [("60s", 60), ("10m", 600), ("6h", 21600), ("2d", 172800), (" 6H ", 21600)],
)
def test_parse_duration(text: str, seconds: int) -> None:
    assert s.parse_duration(text) == seconds


@pytest.mark.parametrize("text", ["", "6", "h", "0h", "-1h", "1.5h", "6w", "6 h"])
def test_parse_duration_rejects(text: str) -> None:
    with pytest.raises(ValueError):
        s.parse_duration(text)


@pytest.mark.parametrize(
    ("seconds", "text"),
    [
        (0, "0s"),
        (-5, "0s"),
        (59.9, "59s"),
        (60, "1m"),
        (3599, "59m"),
        (3600, "1h 0m"),
        (6 * 3600 + 125, "6h 2m"),
        (86399, "23h 59m"),
        (86400, "1d 0h"),
        (2 * 86400 + 4 * 3600 + 59, "2d 4h"),
    ],
)
def test_format_duration(seconds: float, text: str) -> None:
    assert s.format_duration(seconds) == text


def test_clock_time_is_utc() -> None:
    assert s.clock_time(START + 61) == "2026-01-01 00:01 UTC"


def test_shorten_flattens_whitespace_and_cuts_long_details() -> None:
    assert s.shorten("a\n  b\tc") == "a b c"
    assert s.shorten("x" * 200) == "x" * 200
    cut = s.shorten("x" * 201)
    assert len(cut) == 200
    assert cut.endswith("x…")


@pytest.mark.parametrize(
    ("status", "body", "contains", "verdict"),
    [
        (200, b"OK", "", (True, "HTTP 200")),
        (204, b"", "", (True, "HTTP 204")),
        (299, b"", "", (True, "HTTP 299")),
        (199, b"", "", (False, "HTTP 199")),
        (300, b"", "", (False, "HTTP 300")),
        (302, b"", "", (False, "HTTP 302")),
        (502, b"", "", (False, "HTTP 502")),
        (200, b'{"database": "ok"}', '"database": "ok"', (True, "HTTP 200")),
        (
            200,
            b'{"database": "failing"}',
            '"database": "ok"',
            (False, 'HTTP 200 without \'"database": "ok"\''),
        ),
        (200, b"\xff\xfeok", "ok", (True, "HTTP 200")),
    ],
)
def test_http_verdict(status: int, body: bytes, contains: str, verdict: tuple[bool, str]) -> None:
    assert s.http_verdict(status, body, contains) == verdict


@pytest.mark.parametrize(
    ("answer", "age"),
    [
        ({"status": "success", "data": {"result": [{"value": [1, "3.5"]}]}}, 3.5),
        ({"status": "success", "data": {"result": [{"value": [1, "0"]}]}}, 0.0),
        ({"status": "success", "data": {"result": []}}, None),
        ({"status": "success", "data": {}}, None),
        ({"status": "success"}, None),
        ({"status": "error", "data": {"result": [{"value": [1, "3"]}]}}, None),
        ({"status": "success", "data": {"result": ["x"]}}, None),
        ({"status": "success", "data": {"result": [{"value": [1]}]}}, None),
        ({"status": "success", "data": {"result": [{"value": [1, "2", 3]}]}}, None),
        ({"status": "success", "data": {"result": [{"value": "3"}]}}, None),
        ({"status": "success", "data": {"result": [{"value": [1, "NaNx"]}]}}, None),
        ({"status": "success", "data": {"result": [{"value": [1, None]}]}}, None),
        ({"status": "success", "data": {"result": {"value": [1, "3"]}}}, None),
        ([], None),
        (None, None),
    ],
)
def test_sample_age(answer: Any, age: float | None) -> None:
    assert s.sample_age(answer) == age


def test_sample_age_reads_the_first_series() -> None:
    answer = {"status": "success", "data": {"result": [{"value": [1, "7"]}, {"value": [1, "9"]}]}}
    assert s.sample_age(answer) == 7.0


@pytest.mark.parametrize(
    ("age", "verdict"),
    [
        (4.2, (True, "newest sample is 4s old")),
        (180, (True, "newest sample is 3m old")),
        (180.5, (False, "newest sample is 3m old")),
        (412, (False, "newest sample is 6m old")),
    ],
)
def test_fresh_verdict(age: float, verdict: tuple[bool, str]) -> None:
    answer = json.loads(vm_answer(age))
    assert s.fresh_verdict(answer, 180) == verdict


def test_fresh_verdict_without_samples() -> None:
    assert s.fresh_verdict({"status": "success", "data": {"result": []}}, 180) == (
        False,
        "no samples of up",
    )


@pytest.mark.parametrize(
    ("days", "verdict"),
    [
        (30, (True, "certificate valid for 30 days")),
        (7, (True, "certificate valid for 7 days")),
        (6.95, (False, "certificate expires in 7.0 days")),
        (0.5, (False, "certificate expires in 0.5 days")),
        (0, (False, "certificate expires in 0.0 days")),
        (-0.01, (False, "certificate expired")),
    ],
)
def test_tls_verdict(days: float, verdict: tuple[bool, str]) -> None:
    assert s.tls_verdict(START + days * 86400, START, 7) == verdict


def test_failure_detail_names_the_reason_of_a_url_error() -> None:
    error = s.urllib.error.URLError(TimeoutError("timed out"))
    assert s.failure_detail(error) == "URLError: timed out"
    assert s.failure_detail(ConnectionRefusedError("refused")) == "ConnectionRefusedError: refused"


def test_advance_counts_and_alerts_at_the_threshold() -> None:
    bad = (s.Outcome("a", False, "x"), s.Outcome("b", True, "y"))
    state, notice = s.advance(s.State(), bad, 100, 2, 50)
    assert (state, notice) == (s.State(1, 100, None, (bad[0],)), None)
    state, notice = s.advance(state, bad, 160, 2, 50)
    assert (state, notice) == (s.State(2, 100, 160, (bad[0],)), "alert")
    state, notice = s.advance(state, bad, 209, 2, 50)
    assert (state.alerted_at, notice) == (160, None)
    state, notice = s.advance(state, bad, 210, 2, 50)
    assert (state.alerted_at, state.streak, notice) == (210, 4, "reminder")
    state, notice = s.advance(state, (bad[1],), 260, 2, 50)
    assert (state, notice) == (s.State(), "recovery")


def test_advance_with_threshold_one_alerts_on_the_first_failure() -> None:
    state, notice = s.advance(s.State(), (s.Outcome("a", False, "x"),), 5, 1, 50)
    assert notice == "alert"
    assert state.alerted_at == 5


def test_advance_without_alert_on_pass_says_nothing() -> None:
    state = s.State(2, 100, None, (s.Outcome("a", False, "x"),))
    assert s.advance(state, (s.Outcome("a", True, "y"),), 200, 3, 50) == (s.State(), None)


def test_advance_with_no_checks_counts_as_passing() -> None:
    assert s.advance(s.State(), (), 1, 3, 50) == (s.State(), None)


def test_settle_keeps_the_new_state_when_delivered_or_silent() -> None:
    previous = s.State()
    current = s.State(3, 1, 2, ())
    assert s.settle(previous, current, "alert", True) is current
    assert s.settle(previous, current, None, False) is current


def test_settle_rolls_back_only_the_alert_time_of_an_undelivered_alert_or_reminder() -> None:
    failure = (s.Outcome("a", False, "x"),)
    previous = s.State(5, 1, 10, failure)
    current = s.State(6, 1, 99, failure)
    assert s.settle(previous, current, "reminder", False) == s.State(6, 1, 10, failure)
    assert s.settle(s.State(2, 1, None, failure), s.State(3, 1, 9, failure), "alert", False) == (
        s.State(3, 1, None, failure)
    )


def test_settle_keeps_alerting_after_an_undelivered_recovery() -> None:
    previous = s.State(5, 1, 10, (s.Outcome("a", False, "x"),))
    assert s.settle(previous, s.State(), "recovery", False) == s.State(0, 1, 10, ())


def test_notice_text_without_runbook_and_with_one_failure() -> None:
    current = s.State(3, START, START + 120, (s.Outcome("victoriametrics", False, "HTTP 502"),))
    text = s.notice_text(config(runbook=""), s.State(), current, "alert", START + 120)
    assert text == (
        "🔴 acme: central is not answering\n"
        "3 checks in a row failed since 2026-01-01 00:00 UTC:\n"
        "• victoriametrics: HTTP 502\n"
        "Seen from edge by the AyeOps sentinel; alerts of central may not arrive."
    )


def test_notice_text_of_a_reminder_counts_from_the_first_failure() -> None:
    current = s.State(400, START, START + 6 * HOUR, (s.Outcome("samples", False, "z"),))
    text = s.notice_text(config(), s.State(), current, "reminder", START + 6 * HOUR + 61)
    assert text.splitlines()[:3] == [
        "🔴 acme: central is still not answering, down for 6h 1m",
        "Failing now:",
        "• samples: z",
    ]


def test_render_escapes_label_values_and_formats_numbers() -> None:
    text = s.render(
        [
            ("m", s.labels(check='a"b\\c\nd'), 1.0),
            ("m", s.labels(check="a"), 0.25),
            ("n", (), 3),
            ("ayeops_sentinel_notices_total", (), 1e16),
        ]
    )
    assert text == (
        "# TYPE ayeops_sentinel_notices_total counter\n"
        "ayeops_sentinel_notices_total 1e+16\n"
        "# TYPE m gauge\n"
        'm{check="a"} 0.25\n'
        'm{check="a\\"b\\\\c\\nd"} 1\n'
        "# TYPE n gauge\n"
        "n 3\n"
    )


def check_json(*items: dict[str, Any]) -> str:
    return json.dumps(list(items))


def test_parse_checks_reads_every_field_and_defaults() -> None:
    checks = s.parse_checks(
        check_json(
            {"name": "g", "kind": "http", "url": "https://g/api/health", "contains": "ok"},
            {"name": "f", "kind": "fresh", "url": "http://v:8428/api/v1/query"},
            {"name": "t", "kind": "tls", "host": "g.example", "port": 8443, "min_days": 3},
            {"name": "q", "kind": "fresh", "url": "http://v", "query": "x", "max_age_seconds": 60},
        )
    )
    assert checks == (
        s.Check("g", "http", url="https://g/api/health", contains="ok"),
        s.Check("f", "fresh", url="http://v:8428/api/v1/query"),
        s.Check("t", "tls", host="g.example", port=8443, min_days=3),
        s.Check("q", "fresh", url="http://v", query="x", max_age_seconds=60),
    )
    assert checks[1].query == "time() - max(timestamp(up))"
    assert (checks[1].max_age_seconds, checks[2].port, checks[0].min_days) == (180, 8443, 7)


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("[]", "AYE_SENTINEL_CHECKS must be a non-empty JSON list"),
        ("{}", "AYE_SENTINEL_CHECKS must be a non-empty JSON list"),
        ('["x"]', "a check must be an object: 'x'"),
        (check_json({"kind": "http", "url": "http://x"}), "a check has no name"),
        (
            check_json(
                {"name": "a", "kind": "http", "url": "http://x"},
                {"name": "a", "kind": "http", "url": "http://y"},
            ),
            "check 'a' is listed twice",
        ),
        (
            check_json({"name": "a", "kind": "ping", "url": "http://x"}),
            "check 'a' has unknown kind 'ping'",
        ),
        (check_json({"name": "a", "url": "http://x"}), "check 'a' has unknown kind ''"),
        (check_json({"name": "a", "kind": "tls"}), "check 'a' of kind tls needs host"),
        (
            check_json({"name": "a", "kind": "http", "url": "ftp://x"}),
            "check 'a' of kind http needs an http(s) url",
        ),
        (
            check_json({"name": "a", "kind": "fresh"}),
            "check 'a' of kind fresh needs an http(s) url",
        ),
    ],
)
def test_parse_checks_rejects(text: str, message: str) -> None:
    with pytest.raises(ValueError, match=f"^{re.escape(message)}$"):
        s.parse_checks(text)


def test_check_problems_accept_https_and_http_urls() -> None:
    checks = [s.Check("a", "http", url="https://x"), s.Check("b", "fresh", url="http://y")]
    assert s.check_problems(checks) == []


ENV = {
    "AYE_SENTINEL_WATCH": "central",
    "AYE_SENTINEL_CHECKS": check_json({"name": "v", "kind": "http", "url": "http://v/health"}),
}


def test_config_from_env_defaults() -> None:
    loaded = s.config_from({**ENV, "AYE_SENTINEL_HOST": "edge", "AYE_SENTINEL_PROJECT": "acme"})
    assert loaded == s.Config(
        project="acme",
        origin="edge",
        watch="central",
        checks=(s.Check("v", "http", url="http://v/health"),),
        interval_seconds=60,
        timeout_seconds=10,
        failures_before_alert=3,
        reminder_seconds=21600,
        ping_url="",
        runbook="",
    )


def test_config_from_env_reads_every_setting() -> None:
    loaded = s.config_from(
        {
            **ENV,
            "AYE_SENTINEL_HOST": "edge",
            "AYE_SENTINEL_INTERVAL": "30s",
            "AYE_SENTINEL_TIMEOUT": "5s",
            "AYE_SENTINEL_FAILURES_BEFORE_ALERT": "1",
            "AYE_SENTINEL_REMINDER": "2h",
            "AYE_SENTINEL_PING_URL": " https://hc-ping.com/x ",
            "AYE_SENTINEL_RUNBOOK": "https://r",
        }
    )
    assert (
        loaded.interval_seconds,
        loaded.timeout_seconds,
        loaded.failures_before_alert,
        loaded.reminder_seconds,
        loaded.ping_url,
        loaded.runbook,
        loaded.project,
    ) == (30, 5, 1, 7200, "https://hc-ping.com/x", "https://r", "ayeops")


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        (
            {"AYE_SENTINEL_FAILURES_BEFORE_ALERT": "0"},
            "AYE_SENTINEL_FAILURES_BEFORE_ALERT must be at least 1",
        ),
        ({"AYE_SENTINEL_REMINDER": "0h"}, "not a positive duration: '0h'"),
    ],
)
def test_config_from_env_rejects(changes: dict[str, str], message: str) -> None:
    with pytest.raises(ValueError, match=f"^{re.escape(message)}$"):
        s.config_from({**ENV, **changes})


class Recorder(BaseHTTPRequestHandler):
    bodies: ClassVar[list[bytes]] = []
    paths: ClassVar[list[str]] = []
    status: ClassVar[int] = 200
    headers_seen: ClassVar[list[dict[str, str]]] = []

    def do_GET(self) -> None:
        Recorder.paths.append(self.path)
        Recorder.headers_seen.append(dict(self.headers))
        if self.path == "/moved":
            self.send_response(302)
            self.send_header("Location", "/health")
            self.end_headers()
            return
        self.send_response(Recorder.status)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"OK")

    def do_POST(self) -> None:
        Recorder.paths.append(self.path)
        Recorder.headers_seen.append(dict(self.headers))
        length = int(self.headers["Content-Length"])
        Recorder.bodies.append(self.rfile.read(length))
        self.send_response(Recorder.status)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, format: str, *args: Any) -> None:
        return


@pytest.fixture
def server() -> Iterator[str]:
    Recorder.bodies.clear()
    Recorder.headers_seen.clear()
    Recorder.paths.clear()
    Recorder.status = 200
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Recorder)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def test_http_fetch_does_not_follow_redirects(server: str) -> None:
    assert s.http_fetch(f"{server}/moved", WAIT) == (302, b"")
    assert Recorder.paths == ["/moved"]
    assert s.http_fetch(f"{server}/health", WAIT) == (200, b"OK")


def test_http_fetch_returns_error_statuses(server: str) -> None:
    Recorder.status = 503
    assert s.http_fetch(f"{server}/health", WAIT) == (503, b"")


def test_telegram_sender_posts_the_text_and_never_logs_the_token(
    server: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(s, "TELEGRAM_API", server)
    send = s.telegram_sender("123:secret", "-100", timeout=WAIT)
    assert send("🔴 down") is True
    assert Recorder.paths == ["/bot123:secret/sendMessage"]
    assert json.loads(Recorder.bodies[0]) == {
        "chat_id": "-100",
        "text": "🔴 down",
        "disable_web_page_preview": True,
    }
    Recorder.status = 400
    assert send("again") is False
    monkeypatch.setattr(s, "TELEGRAM_API", "http://127.0.0.1:1")
    assert s.telegram_sender("123:secret", "-100", timeout=2)("x") is False
    assert Recorder.headers_seen[0]["Content-Type"] == "application/json"
    logged = capsys.readouterr().out
    assert [json.loads(line) for line in logged.splitlines()] == [
        {"event": "telegram_failed", "status": 400},
        {"event": "telegram_failed", "error": "URLError"},
    ]
    assert "secret" not in logged


def test_check_state_changes_are_logged_once_not_every_round(
    rig: tuple[Any, FakeClock, FakeHttp, FakeTelegram], capsys: pytest.CaptureFixture[str]
) -> None:
    sentinel, clock, http, _ = rig
    minutes(sentinel, clock, 2)
    http.answers[VM_HEALTH] = (502, b"")
    minutes(sentinel, clock, 2)
    http.answers[VM_HEALTH] = (503, b"")
    minutes(sentinel, clock, 2)
    http.healthy()
    minutes(sentinel, clock, 2)
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    checks = [(e["event"], e["detail"]) for e in events if e["event"].startswith("check_")]
    assert checks == [
        ("check_failed", "HTTP 502"),
        ("check_failed", "HTTP 503"),
        ("check_ok", "HTTP 200"),
    ]


@pytest.mark.parametrize(
    ("before", "after", "logged"),
    [
        (None, s.Outcome("a", True, "x"), False),
        (None, s.Outcome("a", False, "x"), True),
        (s.Outcome("a", True, "x"), s.Outcome("a", True, "y"), False),
        (s.Outcome("a", True, "x"), s.Outcome("a", False, "x"), True),
        (s.Outcome("a", False, "x"), s.Outcome("a", False, "x"), False),
        (s.Outcome("a", False, "x"), s.Outcome("a", False, "y"), True),
        (s.Outcome("a", False, "x"), s.Outcome("a", True, "x"), True),
    ],
)
def test_changed(before: Any, after: Any, logged: bool) -> None:
    assert s.changed(before, after) is logged


def test_a_placeholder_ping_url_fails_the_ping_but_never_stops_the_watch() -> None:
    loaded = s.config_from({**ENV, "AYE_SENTINEL_PING_URL": "replace-me"})
    assert loaded.ping_url == "replace-me"
    http = FakeHttp()

    def fetch(url: str, timeout: float) -> tuple[int, bytes]:
        if url == "replace-me":
            status, body = s.http_fetch(url, timeout)
            return int(status), bytes(body)
        return http(url, timeout)

    sentinel = s.Sentinel(
        config(ping_url="replace-me"), fetch, no_expiry, FakeTelegram(), FakeClock()
    )
    sentinel.tick()
    assert sentinel.ping_ok == 0
    assert 'ayeops_sentinel_ping_success{watch="central"} 0' in sentinel.metrics()


def test_parse_duration_messages() -> None:
    with pytest.raises(ValueError, match=r"^not a duration: 'soon'$"):
        s.parse_duration("soon")
    assert s.parse_duration("1s") == 1


def test_records_are_frozen_and_have_their_defaults() -> None:
    outcome = s.Outcome("a", True, "x")
    assert outcome.seconds == 0.0
    assert s.State().failures == ()
    assert s.Config("p", "o", "w", ()).ping_url == ""
    assert s.Config("p", "o", "w", ()).runbook == ""
    for record, name in (
        (outcome, "ok"),
        (s.State(), "streak"),
        (CHECKS[0], "url"),
        (config(), "watch"),
    ):
        with pytest.raises(AttributeError):
            setattr(record, name, None)
    assert s.TELEGRAM_API == "https://api.telegram.org"


def test_format_value_switches_to_float_notation_at_1e15() -> None:
    assert s.format_value(999_999_999_999_999.0) == "999999999999999"
    assert s.format_value(1e15) == "1000000000000000.0"
    assert s.format_value(1.5e15) == "1500000000000000.0"


def test_both_notice_counters_are_typed_counter(
    rig: tuple[Any, FakeClock, FakeHttp, FakeTelegram],
) -> None:
    text = rig[0].metrics()
    assert "# TYPE ayeops_sentinel_notice_failures_total counter" in text
    assert "# TYPE ayeops_sentinel_notices_total counter" in text


def test_metrics_before_the_first_round(
    rig: tuple[Any, FakeClock, FakeHttp, FakeTelegram],
) -> None:
    text = rig[0].metrics()
    assert 'ayeops_sentinel_last_notice_ok{watch="central"} 1' in text
    assert 'ayeops_sentinel_last_round_timestamp_seconds{watch="central"} 0' in text
    assert "ayeops_sentinel_check_success" not in text


def test_every_probe_gets_the_configured_timeout(
    rig: tuple[Any, FakeClock, FakeHttp, FakeTelegram],
) -> None:
    sentinel, _, http, _ = rig
    sentinel.tick()
    assert http.timeouts == [10, 10, 10, 10]


def test_check_duration_is_exported_and_zero_for_an_instant_probe(
    rig: tuple[Any, FakeClock, FakeHttp, FakeTelegram],
) -> None:
    sentinel = rig[0]
    sentinel.tick()
    assert sentinel.outcomes[0].seconds == 0.0
    text = sentinel.metrics()
    assert 'ayeops_sentinel_check_duration_seconds{check="grafana",watch="central"} 0' in text


@pytest.mark.parametrize("status", [199, 300, 404])
def test_fresh_check_fails_on_a_non_2xx_answer(status: int) -> None:
    http = FakeHttp()
    http.answers[VM_QUERY] = (status, vm_answer(1))
    sentinel = s.Sentinel(config(), http, no_expiry, FakeTelegram(), FakeClock())
    outcome = sentinel.probe(CHECKS[2])
    assert (outcome.ok, outcome.detail) == (False, f"HTTP {status}")


@pytest.mark.parametrize(("status", "ok"), [(200, 1), (299, 1), (300, 0), (199, 0)])
def test_ping_succeeds_only_on_2xx(
    rig: tuple[Any, FakeClock, FakeHttp, FakeTelegram], status: int, ok: int
) -> None:
    sentinel, _, http, _ = rig
    http.answers[PING] = (status, b"")
    sentinel.tick()
    assert sentinel.ping_ok == ok


def log_lines(capsys: pytest.CaptureFixture[str]) -> list[dict[str, Any]]:
    return [json.loads(line) for line in capsys.readouterr().out.splitlines()]


def test_log_lines_name_the_event_and_the_detail(
    rig: tuple[Any, FakeClock, FakeHttp, FakeTelegram], capsys: pytest.CaptureFixture[str]
) -> None:
    sentinel, clock, http, _ = rig
    http.dead()
    minutes(sentinel, clock, 3)
    http.healthy()
    http.answers[PING] = OSError("unreachable")
    minutes(sentinel, clock, 1)
    lines = log_lines(capsys)
    assert lines[0] == {
        "event": "check_failed",
        "check": "grafana",
        "detail": "TimeoutError: timed out",
    }
    assert {"event": "alert", "delivered": True, "streak": 3} in lines
    assert {"event": "recovery", "delivered": True, "streak": 0} in lines
    assert {"event": "ping_failed", "error": "OSError: unreachable"} in lines


def test_log_json_sorts_keys_and_keeps_unicode(capsys: pytest.CaptureFixture[str]) -> None:
    s.log_json({"b": "ї", "a": 1})
    assert capsys.readouterr().out == '{"a": 1, "b": "ї"}\n'


def test_notice_counters_add_up_over_two_outages(
    rig: tuple[Any, FakeClock, FakeHttp, FakeTelegram],
) -> None:
    sentinel, clock, http, _ = rig
    for _ in range(2):
        http.dead()
        minutes(sentinel, clock, 3)
        http.healthy()
        minutes(sentinel, clock, 1)
    assert sentinel.notices == {"alert": 2, "reminder": 0, "recovery": 2}


def test_one_round_survives_a_crashing_tick_and_sleeps_the_rest_of_the_interval(
    capsys: pytest.CaptureFixture[str],
) -> None:
    sentinel = s.Sentinel(config(ping_url=""), FakeHttp(), no_expiry, FakeTelegram(), FakeClock())
    ticks = iter([100.0, 112.5])
    assert s.one_round(sentinel, lambda: next(ticks)) == 47.5

    def crash() -> None:
        raise RuntimeError("boom")

    sentinel.tick = crash
    ticks = iter([0.0, 59.5])
    assert s.one_round(sentinel, lambda: next(ticks)) == 1.0
    assert log_lines(capsys) == [{"event": "round_failed", "error": "RuntimeError: boom"}]


@pytest.fixture
def endpoint() -> Iterator[tuple[str, Any, FakeClock]]:
    clock = FakeClock()
    sentinel = s.Sentinel(config(), FakeHttp(), no_expiry, FakeTelegram(), clock)
    httpd = s.make_server(sentinel, 0, "127.0.0.1", clock)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", sentinel, clock
    httpd.shutdown()
    httpd.server_close()


def get(url: str) -> tuple[int, str, dict[str, str]]:
    try:
        with s.urllib.request.urlopen(url, timeout=WAIT) as response:
            return int(response.status), response.read().decode(), dict(response.headers)
    except s.urllib.error.HTTPError as error:
        return int(error.code), "", {}


def test_endpoint_serves_metrics_and_health(endpoint: tuple[str, Any, FakeClock]) -> None:
    url, sentinel, clock = endpoint
    sentinel.tick()
    status, body, headers = get(f"{url}/metrics")
    assert (status, headers["Content-Type"]) == (200, "text/plain; version=0.0.4")
    assert body == sentinel.metrics()
    assert headers["Content-Length"] == str(len(body.encode()))
    assert get(f"{url}/health")[:2] == (200, "ok\n")
    clock.advance(3600)
    assert get(f"{url}/health")[0] == 503
    assert get(f"{url}/other")[0] == 404


def test_http_fetch_names_itself(server: str) -> None:
    s.http_fetch(f"{server}/health", WAIT)
    assert Recorder.headers_seen[0]["User-Agent"] == "ayeops-sentinel"


tzset = getattr(time, "tzset", None)


@pytest.mark.skipif(tzset is None, reason="needs time.tzset to move the local zone")
def test_clock_time_is_utc_in_a_zone_far_from_utc(monkeypatch: pytest.MonkeyPatch) -> None:
    assert tzset is not None
    monkeypatch.setenv("TZ", "AYE-14")
    tzset()
    try:
        assert time.localtime(START).tm_hour == 14
        assert s.clock_time(START + 61) == "2026-01-01 00:01 UTC"
    finally:
        monkeypatch.undo()
        tzset()


def test_http_verdict_reads_a_body_that_is_not_utf8() -> None:
    assert s.http_verdict(200, b"\xff ok \xfe", "ok") == (True, "HTTP 200")
    assert s.http_verdict(200, b"\xff ok \xfe", "fine") == (False, "HTTP 200 without 'fine'")
    assert s.http_verdict(204, b"\xff", "") == (True, "HTTP 204")


def test_the_all_clear_counts_the_outage_from_its_first_failure() -> None:
    previous = s.State(5, START, START + 120, (s.Outcome("samples", False, "z"),))
    text = s.notice_text(config(), previous, s.State(), "recovery", START + 2 * HOUR + 60)
    assert text == (
        "✅ acme: central answers again after 2h 1m\nSeen from edge by the AyeOps sentinel."
    )
    unknown = s.notice_text(config(), s.State(), s.State(), "recovery", START + HOUR)
    assert unknown == (
        "✅ acme: central answers again after 0s\nSeen from edge by the AyeOps sentinel."
    )


def test_an_alert_and_a_reminder_without_a_known_start_count_from_now() -> None:
    failures = (s.Outcome("grafana", False, "HTTP 502"), s.Outcome("samples", False, "z"))
    current = s.State(3, None, None, failures)
    alert = s.notice_text(config(), s.State(), current, "alert", START + HOUR + 60)
    assert alert == (
        "🔴 acme: central is not answering\n"
        "3 checks in a row failed since 2026-01-01 01:01 UTC:\n"
        "• grafana: HTTP 502\n"
        "• samples: z\n"
        "Seen from edge by the AyeOps sentinel; alerts of central may not arrive.\n"
        "Runbook: https://runbooks.example/sentinel-central-down.md"
    )
    reminder = s.notice_text(config(runbook=""), s.State(), current, "reminder", START + HOUR)
    assert reminder == (
        "🔴 acme: central is still not answering, down for 0s\n"
        "Failing now:\n"
        "• grafana: HTTP 502\n"
        "• samples: z\n"
        "Seen from edge by the AyeOps sentinel; alerts of central may not arrive."
    )
    known = s.State(9, START, START + 120, failures[:1])
    assert s.notice_text(config(), s.State(), known, "reminder", START + 90).splitlines() == [
        "🔴 acme: central is still not answering, down for 1m",
        "Failing now:",
        "• grafana: HTTP 502",
        "Seen from edge by the AyeOps sentinel; alerts of central may not arrive.",
        "Runbook: https://runbooks.example/sentinel-central-down.md",
    ]


class Stream:
    def __init__(self) -> None:
        self.written: list[str] = []
        self.flushed = 0

    def write(self, text: str) -> int:
        self.written.append(text)
        return len(text)

    def flush(self) -> None:
        self.flushed += 1


def test_a_log_line_is_flushed_as_soon_as_it_is_written(monkeypatch: pytest.MonkeyPatch) -> None:
    stream = Stream()
    monkeypatch.setattr(sys, "stdout", stream)
    s.log_json({"event": "start", "checks": ["b", "a"]})
    monkeypatch.undo()
    assert "".join(stream.written) == '{"checks": ["b", "a"], "event": "start"}\n'
    assert stream.flushed == 1


class Answer:
    def __init__(self, status: Any, data: bytes = b"") -> None:
        self.status = status
        self.data = data
        self.sizes: list[Any] = []

    def read(self, *size: Any) -> bytes:
        self.sizes.append(size)
        return self.data

    def __enter__(self) -> Answer:
        return self

    def __exit__(self, *args: object) -> None:
        return None


class Web:
    def __init__(self, *answers: Any) -> None:
        self.answers = list(answers)
        self.built: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        self.opened: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        self.request = SimpleNamespace(Request=self.build, urlopen=self.open)
        self.error = s.urllib.error

    def build(self, *args: Any, **kwargs: Any) -> str:
        self.built.append((args, kwargs))
        return "request"

    def open(self, *args: Any, **kwargs: Any) -> Any:
        self.opened.append((args, kwargs))
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer


def http_error(code: int) -> Any:
    return s.urllib.error.HTTPError("https://x", code, "x", email.message.Message(), None)


def test_http_fetch_names_its_header_passes_the_timeout_and_reads_at_most_64_kib(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answer = Answer("204", b"body")
    web = Web(answer, http_error(503))
    monkeypatch.setattr(s, "urllib", web)
    monkeypatch.setattr(s, "OPENER", web)
    assert s.http_fetch("http://v/health", 2.5) == (204, b"body")
    assert web.built == [(("http://v/health",), {"headers": {"User-Agent": "ayeops-sentinel"}})]
    assert web.opened == [(("request",), {"timeout": 2.5})]
    assert answer.sizes == [(65536,)]
    assert s.http_fetch("http://v/health", 2.5) == (503, b"")


def test_telegram_sender_builds_a_json_post_and_takes_only_a_2xx_answer_as_delivered(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    web = Web(Answer("200"), Answer(299), Answer(199), Answer(300), Answer(204))
    monkeypatch.setattr(s, "urllib", web)
    send = s.telegram_sender("123:secret", "-100")
    assert [send("hi") for _ in range(4)] == [True, True, False, False]
    request = (
        ("https://api.telegram.org/bot123:secret/sendMessage",),
        {
            "data": b'{"chat_id": "-100", "text": "hi", "disable_web_page_preview": true}',
            "headers": {"Content-Type": "application/json"},
            "method": "POST",
        },
    )
    assert web.built == [request] * 4
    assert web.opened == [(("request",), {"timeout": 15})] * 4
    assert s.telegram_sender("t", "c", 2.5)("x") is True
    assert web.opened[-1] == (("request",), {"timeout": 2.5})
    assert capsys.readouterr().out == ""


def test_telegram_sender_logs_why_a_message_was_not_delivered(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    web = Web(http_error(403), ValueError("bad url"), TimeoutError("slow"))
    monkeypatch.setattr(s, "urllib", web)
    send = s.telegram_sender("123:secret", "-100")
    assert [send("hi") for _ in range(3)] == [False, False, False]
    assert capsys.readouterr().out == (
        '{"event": "telegram_failed", "status": 403}\n'
        '{"error": "ValueError", "event": "telegram_failed"}\n'
        '{"error": "TimeoutError", "event": "telegram_failed"}\n'
    )


def test_the_origin_defaults_to_the_host_name_of_the_container(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(s, "socket", SimpleNamespace(gethostname=lambda: "box-7"))
    assert s.config_from(ENV).origin == "box-7"
    assert s.config_from({**ENV, "AYE_SENTINEL_HOST": "edge"}).origin == "edge"
    assert s.config_from({**ENV, "AYE_SENTINEL_PING_URL": "   "}).ping_url == ""
    assert (
        s.config_from({**ENV, "AYE_SENTINEL_FAILURES_BEFORE_ALERT": "1"}).failures_before_alert == 1
    )
    assert (
        s.config_from({**ENV, "AYE_SENTINEL_FAILURES_BEFORE_ALERT": "2"}).failures_before_alert == 2
    )


def test_the_endpoint_listens_on_every_address_unless_told_a_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    made: list[tuple[Any, ...]] = []

    def listen(*args: Any) -> str:
        made.append(args)
        return "server"

    monkeypatch.setattr(s, "ThreadingHTTPServer", listen)
    sentinel = s.Sentinel(config(), FakeHttp(), no_expiry, FakeTelegram(), FakeClock())
    assert s.make_server(sentinel, 9134) == "server"
    assert s.make_server(sentinel, 9200, "127.0.0.1") == "server"
    assert [args[0] for args in made] == [("", 9134), ("127.0.0.1", 9200)]
    assert all(len(args) == 2 and issubclass(args[1], BaseHTTPRequestHandler) for args in made)


class Wrapped:
    def __init__(self, certificate: Any) -> None:
        self.certificate = certificate
        self.closed = 0

    def getpeercert(self) -> Any:
        return self.certificate

    def __enter__(self) -> Wrapped:
        return self

    def __exit__(self, *args: object) -> None:
        self.closed += 1


class Tls:
    def __init__(self, certificate: Any, error: Exception | None = None) -> None:
        self.wrapped = Wrapped(certificate)
        self.error = error
        self.contexts: list[tuple[Any, ...]] = []
        self.connected: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        self.wraps: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        self.times: list[tuple[Any, ...]] = []
        self.raw = SimpleNamespace(close=self.close)
        self.closed = 0

    def close(self) -> None:
        self.closed += 1

    def create_default_context(self, *args: Any) -> Tls:
        self.contexts.append(args)
        return self

    def create_connection(self, *args: Any, **kwargs: Any) -> Any:
        self.connected.append((args, kwargs))
        return self.raw

    def wrap_socket(self, *args: Any, **kwargs: Any) -> Wrapped:
        self.wraps.append((args, kwargs))
        if self.error is not None:
            raise self.error
        return self.wrapped

    def cert_time_to_seconds(self, *args: Any) -> int:
        self.times.append(args)
        return ssl.cert_time_to_seconds(*args)


def test_the_certificate_expiry_is_read_over_a_verified_connection_to_the_named_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tls = Tls({"notAfter": "Jan  1 00:00:00 2027 GMT", "subject": ()})
    monkeypatch.setattr(s, "ssl", tls)
    monkeypatch.setattr(s, "socket", tls)
    found = s.certificate_expiry("grafana.example", 8443, 4.5)
    assert found == 1798761600.0
    assert isinstance(found, float)
    assert tls.contexts == [()]
    assert tls.connected == [((("grafana.example", 8443),), {"timeout": 4.5})]
    assert tls.wraps == [((tls.raw,), {"server_hostname": "grafana.example"})]
    assert tls.times == [("Jan  1 00:00:00 2027 GMT",)]
    assert (tls.wrapped.closed, tls.closed) == (1, 1)


@pytest.mark.parametrize("certificate", [None, {}, {"subject": ()}])
def test_a_certificate_without_an_expiry_fails_the_check_and_closes_the_socket(
    certificate: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    tls = Tls(certificate)
    monkeypatch.setattr(s, "ssl", tls)
    monkeypatch.setattr(s, "socket", tls)
    with pytest.raises(KeyError, match="notAfter"):
        s.certificate_expiry("grafana.example", 443, 10)
    assert (tls.wrapped.closed, tls.closed, tls.times) == (1, 1, [])


def test_a_failed_handshake_closes_the_socket_and_fails_the_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tls = Tls({}, ssl.SSLError("certificate verify failed"))
    monkeypatch.setattr(s, "ssl", tls)
    monkeypatch.setattr(s, "socket", tls)
    with pytest.raises(ssl.SSLError, match="certificate verify failed"):
        s.certificate_expiry("grafana.example", 443, 10)
    assert (tls.wrapped.closed, tls.closed) == (0, 1)


class Stop(Exception):
    pass


def test_the_watch_sleeps_what_each_round_leaves_and_starts_the_next_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rounds: list[tuple[Any, ...]] = []
    naps: list[float] = []

    def one_round(*args: Any) -> float:
        rounds.append(args)
        return 7.5 + len(rounds)

    def sleep(seconds: float) -> None:
        naps.append(seconds)
        if len(naps) == 3:
            raise Stop

    monkeypatch.setattr(s, "one_round", one_round)
    monkeypatch.setattr(s, "time", SimpleNamespace(sleep=sleep))
    with pytest.raises(Stop):
        s.run_forever("sentinel")
    assert rounds == [("sentinel",)] * 3
    assert naps == [8.5, 9.5, 10.5]


@pytest.mark.parametrize(
    ("extra", "port", "ping"),
    [
        ({}, 9134, False),
        ({"AYE_SENTINEL_PORT": "9200", "AYE_SENTINEL_PING_URL": PING}, 9200, True),
    ],
)
def test_main_starts_the_watch_in_a_daemon_thread_and_serves_the_endpoint(
    extra: dict[str, str],
    port: int,
    ping: bool,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    senders: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    sentinels: list[Any] = []
    built: list[tuple[Any, ...]] = []
    threads: list[Any] = []
    servers: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    served: list[int] = []
    order: list[str] = []

    def sender(*args: Any, **kwargs: Any) -> str:
        senders.append((args, kwargs))
        return "send"

    def wall() -> float:
        return 123.5

    real = s.Sentinel

    def sentinel_of(*args: Any) -> Any:
        built.append(args)
        sentinels.append(real(*args))
        return sentinels[-1]

    class Thread:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.args, self.kwargs, self.started = args, kwargs, 0
            self.round_at_start: float | None = None
            threads.append(self)

        def start(self) -> None:
            self.started += 1
            self.round_at_start = sentinels[0].last_round
            order.append("watch")

    def serve_forever() -> None:
        served.append(threads[0].started)
        order.append("serve")

    def make_server(*args: Any, **kwargs: Any) -> Any:
        servers.append((args, kwargs))
        return SimpleNamespace(serve_forever=serve_forever)

    env = {
        **ENV,
        "AYE_SENTINEL_HOST": "edge",
        "AYE_TELEGRAM_BOT_TOKEN": "123:secret",
        "AYE_TELEGRAM_CHAT_ID": "-100",
        **extra,
    }
    monkeypatch.setattr(s, "os", SimpleNamespace(environ=env))
    monkeypatch.setattr(s, "telegram_sender", sender)
    monkeypatch.setattr(s, "Sentinel", sentinel_of)
    monkeypatch.setattr(s, "time", SimpleNamespace(time=wall))
    monkeypatch.setattr(s, "threading", SimpleNamespace(Thread=Thread, Lock=threading.Lock))
    monkeypatch.setattr(s, "make_server", make_server)
    assert s.main() is None
    assert senders == [(("123:secret", "-100"), {})]
    assert len(sentinels) == len(threads) == 1
    sentinel, thread = sentinels[0], threads[0]
    assert built == [
        (s.config_from(env), s.http_fetch, s.certificate_expiry, "send", wall, s.query_fetch)
    ]
    assert built[0][1] is s.http_fetch and built[0][2] is s.certificate_expiry
    assert built[0][5] is s.query_fetch
    assert sentinel.last_round == 123.5
    assert thread.args == ()
    assert sorted(thread.kwargs) == ["args", "daemon", "target"]
    assert thread.kwargs["target"] is s.run_forever
    assert thread.kwargs["args"] == (sentinel,) and thread.kwargs["args"][0] is sentinel
    assert thread.kwargs["daemon"] is True
    assert (thread.started, thread.round_at_start) == (1, 123.5)
    assert servers == [((sentinel, port), {})]
    assert servers[0][0][0] is sentinel
    assert (served, order) == ([1], ["watch", "serve"])
    flag = "true" if ping else "false"
    assert capsys.readouterr().out == (
        '{"checks": ["v"], "digest": "", "event": "start", "failures_before_alert": 3, '
        f'"ping": {flag}, "reminder_seconds": 21600, "watch": "central"}}\n'
    )


DAY = 86400.0
EIGHT = START + 8 * HOUR
DIGEST_CALL = f"{VM_QUERY}?{s.urllib.parse.urlencode({'query': s.DIGEST_QUERY})}"
FOOTER = "Read from central by the AyeOps sentinel on edge, every day at 08:00 UTC."


def series(name: str, value: Any, **pairs: str) -> dict[str, Any]:
    return {"metric": {"__name__": name, **pairs}, "value": [START, str(value)]}


def vm_series(*items: Any) -> bytes:
    return json.dumps(
        {"status": "success", "data": {"resultType": "vector", "result": list(items)}}
    ).encode()


def job(name: str, value: Any, backup: str = "acme-db", host: str = "edge") -> dict[str, Any]:
    return series(f"ayeops_backup_{name}", value, host=host, backup=backup)


def copy(name: str, value: Any, host: str = "edge") -> dict[str, Any]:
    return series(f"ayeops_offsite_{name}", value, host=host)


FRESH_SERIES = (
    job("max_age_seconds", 93600),
    job("last_success_timestamp_seconds", EIGHT - 3 * HOUR),
    job("last_run_success", 1),
    job("last_size_bytes", 1_288_490_189),
    copy("last_success_timestamp_seconds", EIGHT - 2 * HOUR),
    copy("restore_test_ok", 1),
    copy("restore_test_last_success_timestamp_seconds", EIGHT - 2 * DAY - HOUR),
)
FRESH_DIGEST = (
    "💾 acme: backup digest, 2026-01-01 08:00 UTC\n"
    "✅ All 2 backups fresh\n"
    "• edge/acme-db: last success 3h 0m ago, 1.2 GiB\n"
    "• edge off-site copy: last copy 2h 0m ago, restore test passed 2d 1h ago\n" + FOOTER
)


def digest_config(**changes: Any) -> Any:
    return config(**{"digest_at": "08:00", "digest_url": VM_QUERY, **changes})


@pytest.fixture
def digest_rig() -> tuple[Any, FakeClock, FakeHttp, FakeTelegram]:
    clock = FakeClock()
    vm = FakeHttp()
    vm.answers[VM_QUERY] = (200, vm_series(*FRESH_SERIES))
    telegram = FakeTelegram()
    sentinel = s.Sentinel(digest_config(), FakeHttp(), no_expiry, telegram, clock, vm)
    return sentinel, clock, vm, telegram


@pytest.mark.parametrize(
    ("size", "text"),
    [
        (-5, "0 B"),
        (0, "0 B"),
        (0.4, "0 B"),
        (1023, "1023 B"),
        (1024, "1.0 KiB"),
        (1536, "1.5 KiB"),
        (1048575, "1024.0 KiB"),
        (1048576, "1.0 MiB"),
        (1.5 * 1024**3, "1.5 GiB"),
        (1024**4, "1.0 TiB"),
        (1024**5, "1024.0 TiB"),
    ],
)
def test_format_bytes(size: float, text: str) -> None:
    assert s.format_bytes(size) == text


@pytest.mark.parametrize(
    ("text", "at"), [("08:00", (8, 0)), (" 23:59 ", (23, 59)), ("00:07", (0, 7))]
)
def test_parse_clock_reads_hours_and_minutes(text: str, at: tuple[int, int]) -> None:
    assert s.parse_clock(text) == at


@pytest.mark.parametrize("text", ["24:00", "8:00", "08:60", "", "08:00:00", "0800"])
def test_parse_clock_refuses_anything_but_hh_mm(text: str) -> None:
    message = f"^not a time of day like 08:00: {re.escape(repr(text))}$"
    with pytest.raises(ValueError, match=message):
        s.parse_clock(text)


def test_time_zone_falls_back_to_utc_only_for_utc(monkeypatch: pytest.MonkeyPatch) -> None:
    assert str(s.time_zone("Europe/Kyiv")) == "Europe/Kyiv"

    def missing(key: str) -> Any:
        raise s.ZoneInfoNotFoundError(key)

    monkeypatch.setattr(s, "ZoneInfo", missing)
    assert s.time_zone("UTC") is s.UTC
    with pytest.raises(s.ZoneInfoNotFoundError):
        s.time_zone("Europe/Kyiv")


@pytest.mark.parametrize(
    ("after", "at", "zone", "slot"),
    [
        (START + 5 * HOUR, "08:00", "UTC", EIGHT),
        (EIGHT - 1, "08:00", "UTC", EIGHT),
        (EIGHT, "08:00", "UTC", EIGHT + DAY),
        (EIGHT + 1, "08:00", "UTC", EIGHT + DAY),
        (START, "23:59", "UTC", START + 23 * HOUR + 59 * 60),
        (START, "08:00", "Europe/Kyiv", START + 6 * HOUR),
        (START + 7 * HOUR, "08:00", "Europe/Kyiv", START + DAY + 6 * HOUR),
    ],
)
def test_next_digest_is_the_first_slot_after_the_moment(
    after: float, at: str, zone: str, slot: float
) -> None:
    assert s.next_digest(after, at, s.time_zone(zone)) == slot


def test_next_digest_keeps_the_local_time_across_a_clock_change() -> None:
    zone = s.time_zone("Europe/Kyiv")
    before = s.datetime(2026, 3, 28, 7, 0, tzinfo=s.UTC).timestamp()
    after = s.datetime(2026, 3, 29, 5, 0, tzinfo=s.UTC).timestamp()
    assert s.next_digest(before, "08:00", zone) == after


@pytest.mark.parametrize(
    "item",
    [
        "junk",
        {"value": [1, "1"]},
        {"metric": "x", "value": [1, "1"]},
        {"metric": {}, "value": "1"},
        {"metric": {}, "value": [1]},
        {"metric": {}, "value": [1, "1", 2]},
        {"metric": {}, "value": [1, "x"]},
        {"metric": {}, "value": [1, None]},
        {"metric": {}, "value": [1, "NaN"]},
        {"metric": {}, "value": [1, "+Inf"]},
        {"metric": {}, "value": [1, "-Inf"]},
    ],
)
def test_series_value_skips_what_is_not_one_finite_sample(item: Any) -> None:
    assert s.series_value(item) is None


def test_series_value_reads_the_name_the_labels_and_the_number() -> None:
    assert s.series_value(series("a", "1.5", host="h")) == (
        "a",
        {"__name__": "a", "host": "h"},
        1.5,
    )
    plain = {"metric": {"host": "h"}, "value": [1, "2"]}
    assert s.series_value(plain) == ("", {"host": "h"}, 2.0)


@pytest.mark.parametrize(
    "answer",
    [
        None,
        [],
        {"status": "error"},
        {"data": {"result": []}},
        {"status": "success"},
        {"status": "success", "data": None},
        {"status": "success", "data": {"result": "x"}},
    ],
)
def test_backups_from_refuses_what_is_not_a_query_result(answer: Any) -> None:
    assert s.backups_from(answer) is None


def test_backups_from_an_empty_result_is_an_empty_list() -> None:
    assert s.backups_from(json.loads(vm_series())) == []


def test_backups_from_groups_by_host_and_job_and_keeps_the_worst_verdict() -> None:
    answer = json.loads(
        vm_series(
            job("max_age_seconds", 93600),
            job("max_age_seconds", 90000),
            job("last_success_timestamp_seconds", 100),
            job("last_success_timestamp_seconds", 200),
            job("declared_timestamp_seconds", 50),
            job("last_run_success", 1),
            job("last_run_success", 0),
            job("last_size_bytes", 10),
            job("last_success_timestamp_seconds", 300, backup="gone"),
            job("max_age_seconds", 7200, backup="glitchtip-db", host="central"),
            copy("last_success_timestamp_seconds", 400),
            copy("restore_test_ok", 0),
            copy("restore_test_ok", 1),
            copy("restore_test_last_success_timestamp_seconds", 600),
            copy("restore_test_last_success_timestamp_seconds", 500),
            copy("max_age_seconds", 3600),
            copy("restore_test_ok", 1, host="central"),
            series("ayeops_backup_max_age_seconds", 60, backup="nohost"),
            series("ayeops_backup_max_age_seconds", 5, host="z"),
            series("up", 1, host="edge"),
            {"metric": {"host": "x"}, "value": [1, "1"]},
            "junk",
        )
    )
    assert s.backups_from(answer) == [
        s.Backup("", "dump", "nohost", limit=60.0),
        s.Backup("central", "dump", "glitchtip-db", limit=7200.0),
        s.Backup("central", "offsite", "", restore_ok=1.0),
        s.Backup(
            "edge",
            "dump",
            "acme-db",
            limit=93600.0,
            success=200.0,
            declared=50.0,
            run_ok=0.0,
            size=10.0,
        ),
        s.Backup(
            "edge", "offsite", "", limit=3600.0, success=400.0, restore_ok=0.0, restore_at=600.0
        ),
        s.Backup("z", "dump", "", limit=5.0),
    ]


def test_backup_labels_and_defaults() -> None:
    backup = s.Backup("edge", "dump", "acme-db")
    assert backup.label == "edge/acme-db"
    assert s.Backup("edge", "offsite").label == "edge off-site copy"
    assert s.Backup("edge", "offsite").name == ""
    assert (backup.limit, backup.success, backup.declared, backup.run_ok) == (None,) * 4
    assert (backup.size, backup.restore_ok, backup.restore_at) == (None,) * 3
    with pytest.raises(AttributeError):
        backup.host = "x"


@pytest.mark.parametrize(
    ("fields", "ok", "line"),
    [
        ({"limit": 3600, "success": EIGHT - 3600}, True, "last success 1h 0m ago"),
        (
            {"limit": 3600, "success": EIGHT - 3601},
            False,
            "stale, last success 1h 0m ago, limit 1h 0m",
        ),
        (
            {"limit": 3600, "success": EIGHT - 60, "run_ok": 0},
            False,
            "last run failed, last success 1m ago",
        ),
        ({"limit": 3600, "success": EIGHT - 60, "run_ok": 1}, True, "last success 1m ago"),
        (
            {"limit": 3600, "success": EIGHT - 7200, "run_ok": 0, "size": 2048},
            False,
            "stale, last run failed, last success 2h 0m ago, limit 1h 0m, 2.0 KiB",
        ),
        ({"limit": 3600, "declared": EIGHT - 300}, True, "no success yet, declared 5m ago"),
        (
            {"limit": 3600, "declared": EIGHT - 3601},
            False,
            "stale, no success yet, declared 1h 0m ago, limit 1h 0m",
        ),
        (
            {"limit": 3600, "success": EIGHT - 60, "declared": EIGHT - 7200, "size": 2048},
            True,
            "last success 1m ago, 2.0 KiB",
        ),
        ({"limit": 3600, "success": EIGHT, "size": 0}, True, "last success 0s ago, 0 B"),
        ({"limit": 3600}, False, "no run recorded"),
        ({"success": EIGHT, "declared": EIGHT}, False, "no run recorded"),
    ],
)
def test_a_dump_line_tells_its_age_against_its_limit(
    fields: dict[str, float], ok: bool, line: str
) -> None:
    backup = s.Backup("h", "dump", "j", **fields)
    assert s.backup_line(backup, EIGHT) == (ok, f"• h/j: {line}")


@pytest.mark.parametrize(
    ("fields", "ok", "line"),
    [
        (
            {"success": EIGHT - 93600, "restore_ok": 1, "restore_at": EIGHT - 691200},
            True,
            "last copy 1d 2h ago, restore test passed 8d 0h ago",
        ),
        (
            {"success": EIGHT - 93601, "restore_ok": 1, "restore_at": EIGHT - 60},
            False,
            "stale, last copy 1d 2h ago, restore test passed 1m ago",
        ),
        (
            {"limit": 3600, "success": EIGHT - 3600, "restore_at": EIGHT - 60},
            True,
            "last copy 1h 0m ago, restore test passed 1m ago",
        ),
        (
            {"limit": 3600, "success": EIGHT - 3601, "restore_at": EIGHT - 60},
            False,
            "stale, last copy 1h 0m ago, restore test passed 1m ago",
        ),
        (
            {"restore_ok": 1, "restore_at": EIGHT - 60},
            False,
            "no copy recorded, restore test passed 1m ago",
        ),
        (
            {"success": EIGHT - 60, "restore_ok": 0, "restore_at": EIGHT - 60},
            False,
            "restore test failed, last copy 1m ago, restore test passed 1m ago",
        ),
        (
            {"success": EIGHT - 60, "restore_ok": 1},
            False,
            "no restore test passed, last copy 1m ago",
        ),
        (
            {"success": EIGHT - 60, "restore_at": EIGHT - 691201},
            False,
            "restore test overdue, last copy 1m ago, restore test passed 8d 0h ago",
        ),
        ({}, False, "no copy recorded, no restore test passed"),
    ],
)
def test_an_off_site_line_tells_the_copy_and_the_restore_test(
    fields: dict[str, float], ok: bool, line: str
) -> None:
    backup = s.Backup("h", "offsite", **fields)
    assert s.backup_line(backup, EIGHT) == (ok, f"• h off-site copy: {line}")


def test_fit_keeps_every_line_up_to_the_limit_and_counts_the_rest() -> None:
    lines = ["x" * 20, "y" * 20]
    full = "h\n" + "x" * 20 + "\n" + "y" * 20 + "\nt"
    one = "h\n" + "x" * 20 + "\n• and 1 more\nt"
    assert (len(full), len(one)) == (45, 37)
    assert s.fit(["h"], lines, ["t"], 45) == full
    assert s.fit(["h"], lines, ["t"], 44) == one
    assert s.fit(["h"], lines, ["t"], 37) == one
    assert s.fit(["h"], lines, ["t"], 36) == "h\n• and 2 more\nt"
    assert s.fit(["h"], lines, ["t"], 1) == "h\n• and 2 more\nt"
    assert s.fit(["h", "o"], [], ["t"], 1) == "h\no\nt"
    assert lines == ["x" * 20, "y" * 20]


def test_digest_of_fresh_backups_says_so_and_lists_each() -> None:
    backups = s.backups_from(json.loads(vm_series(*FRESH_SERIES)))
    assert s.digest_text(digest_config(), backups, "", EIGHT) == FRESH_DIGEST


def test_digest_puts_the_stale_and_failed_backups_first_and_names_them() -> None:
    backups = [
        s.Backup("central", "dump", "glitchtip-db", limit=93600, success=EIGHT - 60),
        s.Backup("edge", "dump", "acme-db", limit=90000, success=EIGHT - 26 * HOUR, size=1024),
        s.Backup("edge", "offsite", success=EIGHT - 60, restore_ok=0, restore_at=EIGHT - DAY),
    ]
    assert s.digest_text(digest_config(), backups, "", EIGHT) == (
        "💾 acme: backup digest, 2026-01-01 08:00 UTC\n"
        "🟠 2 of 3 backups stale or failed: edge/acme-db, edge off-site copy\n"
        "• edge/acme-db: stale, last success 1d 2h ago, limit 1d 1h, 1.0 KiB\n"
        "• edge off-site copy: restore test failed, last copy 1m ago,"
        " restore test passed 1d 0h ago\n"
        "• central/glitchtip-db: last success 1m ago\n" + FOOTER
    )


def test_digest_without_backups_says_none_are_reported() -> None:
    assert s.digest_text(digest_config(), [], "", EIGHT) == (
        "💾 acme: backup digest, 2026-01-01 08:00 UTC\n"
        "🟠 No backups reported to central: no dump job, no off-site copy\n" + FOOTER
    )


def test_digest_without_metrics_names_the_reason() -> None:
    changed = digest_config(digest_at="21:30", digest_timezone="Europe/Kyiv", origin="web")
    assert s.digest_text(changed, None, "HTTP 503", EIGHT) == (
        "💾 acme: backup digest, 2026-01-01 08:00 UTC\n"
        "🟠 Cannot read the backup metrics from central: HTTP 503\n"
        "Read from central by the AyeOps sentinel on web, every day at 21:30 Europe/Kyiv."
    )


@pytest.mark.parametrize(
    ("count", "names"),
    [
        (5, "h/a, h/b, h/c, h/d, h/e"),
        (6, "h/a, h/b, h/c, h/d, h/e and 1 more"),
        (7, "h/a, h/b, h/c, h/d, h/e and 2 more"),
    ],
)
def test_digest_names_five_problems_in_its_summary(count: int, names: str) -> None:
    backups = [s.Backup("h", "dump", name, limit=1) for name in "abcdefg"[:count]]
    text = s.digest_text(digest_config(), backups, "", EIGHT)
    assert text.split("\n")[1] == f"🟠 {count} of {count} backups stale or failed: {names}"
    assert text.count("no run recorded") == count


def test_a_long_digest_stays_under_the_telegram_limit() -> None:
    backups = [
        s.Backup(f"host-{index:03d}", "dump", "db", limit=HOUR, success=EIGHT - 60)
        for index in range(300)
    ]
    text = s.digest_text(digest_config(), backups, "", EIGHT)
    assert len(text) <= s.DIGEST_LIMIT
    lines = text.split("\n")
    kept = len(lines) - 4
    assert lines[1] == "✅ All 300 backups fresh"
    assert lines[2] == "• host-000/db: last success 1m ago"
    assert lines[-2] == f"• and {300 - kept} more"
    assert lines[-1] == FOOTER
    extra = f"• host-{kept:03d}/db: last success 1m ago"
    assert len("\n".join([*lines[:-2], extra, f"• and {299 - kept} more", FOOTER])) > 4000


def test_digest_query_selects_every_backup_and_off_site_series() -> None:
    assert s.DIGEST_QUERY == (
        '{__name__=~"ayeops_backup_declared_timestamp_seconds'
        "|ayeops_backup_last_run_success|ayeops_backup_last_size_bytes"
        "|ayeops_backup_last_success_timestamp_seconds|ayeops_backup_max_age_seconds"
        "|ayeops_offsite_last_success_timestamp_seconds|ayeops_offsite_max_age_seconds"
        "|ayeops_offsite_restore_test_last_success_timestamp_seconds"
        '|ayeops_offsite_restore_test_ok"}'
    )


def test_the_digest_goes_out_once_a_day_at_its_time(
    digest_rig: tuple[Any, FakeClock, FakeHttp, FakeTelegram],
) -> None:
    sentinel, clock, vm, telegram = digest_rig
    text = sentinel.metrics()
    assert 'ayeops_sentinel_digest_due_timestamp_seconds{watch="central"} 1767254400' in text
    assert "ayeops_sentinel_digest_last_sent_timestamp_seconds" not in text
    assert 'ayeops_sentinel_notices_total{kind="digest",watch="central"} 0' in text
    clock.advance(8 * HOUR - 1)
    assert sentinel.digest(clock.now) is None
    sentinel.tick()
    assert (telegram.sent, vm.calls) == ([], [])
    clock.advance(1)
    sentinel.tick()
    assert telegram.sent == [FRESH_DIGEST]
    assert (vm.calls, vm.timeouts) == ([DIGEST_CALL], [10])
    assert (sentinel.digest_sent, sentinel.digest_due) == (EIGHT, EIGHT + DAY)
    text = sentinel.metrics()
    assert 'ayeops_sentinel_digest_due_timestamp_seconds{watch="central"} 1767340800' in text
    sent = 'ayeops_sentinel_digest_last_sent_timestamp_seconds{watch="central"} 1767254400'
    assert sent in text
    assert 'ayeops_sentinel_notices_total{kind="digest",watch="central"} 1' in text
    assert 'ayeops_sentinel_notice_failures_total{kind="digest",watch="central"} 0' in text
    minutes(sentinel, clock, 3)
    assert len(telegram.sent) == 1
    clock.now = EIGHT + DAY
    assert sentinel.digest(clock.now) is True
    assert len(telegram.sent) == 2
    assert sentinel.digest_due == EIGHT + 2 * DAY
    assert (sentinel.notices["digest"], sentinel.notice_failures["digest"]) == (2, 0)


def test_a_digest_telegram_refused_is_sent_again_next_round(
    digest_rig: tuple[Any, FakeClock, FakeHttp, FakeTelegram],
) -> None:
    sentinel, clock, _, telegram = digest_rig
    clock.now = EIGHT
    telegram.working = False
    assert sentinel.digest(clock.now) is False
    assert (sentinel.digest_sent, sentinel.digest_due) == (None, EIGHT)
    assert sentinel.last_notice_ok == 0
    assert sentinel.notice_failures["digest"] == 1
    assert sentinel.notices["digest"] == 0
    assert 'ayeops_sentinel_last_notice_ok{watch="central"} 0' in sentinel.metrics()
    clock.advance(60)
    telegram.working = True
    assert sentinel.digest(clock.now) is True
    assert (sentinel.digest_sent, sentinel.digest_due) == (EIGHT + 60, EIGHT + DAY)
    assert sentinel.last_notice_ok == 1
    assert (sentinel.notices["digest"], sentinel.notice_failures["digest"]) == (1, 1)


def test_the_digest_logs_one_line(
    digest_rig: tuple[Any, FakeClock, FakeHttp, FakeTelegram],
    capsys: pytest.CaptureFixture[str],
) -> None:
    sentinel, _, vm, _ = digest_rig
    sentinel.digest(EIGHT)
    vm.answers[VM_QUERY] = (503, b"")
    sentinel.digest(EIGHT + DAY)
    assert capsys.readouterr().out == (
        '{"backups": 2, "delivered": true, "error": "", "event": "digest"}\n'
        '{"backups": null, "delivered": true, "error": "HTTP 503", "event": "digest"}\n'
    )


@pytest.mark.parametrize(
    ("answer", "error"),
    [
        ((199, vm_series()), "HTTP 199"),
        ((300, vm_series()), "HTTP 300"),
        (TimeoutError("timed out"), "TimeoutError: timed out"),
        ((200, b"not json"), "JSONDecodeError: Expecting value: line 1 column 1 (char 0)"),
        ((200, b'{"status": "error"}'), "the answer is not a query result"),
    ],
)
def test_a_digest_without_metrics_still_goes_out_with_the_reason(
    digest_rig: tuple[Any, FakeClock, FakeHttp, FakeTelegram], answer: Any, error: str
) -> None:
    sentinel, _, vm, telegram = digest_rig
    vm.answers[VM_QUERY] = answer
    assert sentinel.read_backups() == (None, error)
    assert sentinel.digest(EIGHT) is True
    assert f"🟠 Cannot read the backup metrics from central: {error}\n" in telegram.sent[0]
    assert sentinel.digest_due == EIGHT + DAY


@pytest.mark.parametrize("status", [200, 299])
def test_read_backups_takes_any_2xx(
    digest_rig: tuple[Any, FakeClock, FakeHttp, FakeTelegram], status: int
) -> None:
    sentinel, _, vm, _ = digest_rig
    vm.answers[VM_QUERY] = (status, vm_series())
    assert sentinel.read_backups() == ([], "")


def test_the_digest_query_uses_the_round_timeout() -> None:
    vm = FakeHttp()
    vm.answers[VM_QUERY] = (200, vm_series())
    changed = digest_config(timeout_seconds=7)
    sentinel = s.Sentinel(changed, FakeHttp(), no_expiry, FakeTelegram(), FakeClock(), vm)
    sentinel.read_backups()
    assert vm.timeouts == [7]


def test_the_digest_follows_its_time_zone() -> None:
    changed = digest_config(digest_timezone="Europe/Kyiv")
    sentinel = s.Sentinel(changed, FakeHttp(), no_expiry, FakeTelegram(), FakeClock())
    assert sentinel.digest_due == START + 6 * HOUR
    assert sentinel.next_digest(START + 6 * HOUR) == START + DAY + 6 * HOUR


def test_without_a_query_fetch_the_digest_uses_the_check_fetch() -> None:
    http = FakeHttp()
    other = FakeHttp()
    plain = s.Sentinel(digest_config(), http, no_expiry, FakeTelegram(), FakeClock())
    given = s.Sentinel(digest_config(), http, no_expiry, FakeTelegram(), FakeClock(), other)
    assert plain.query is http
    assert given.query is other


def test_a_sentinel_without_digest_never_sends_or_exports_one(
    rig: tuple[Any, FakeClock, FakeHttp, FakeTelegram],
) -> None:
    sentinel, clock, http, telegram = rig
    assert sentinel.kinds == s.NOTICES
    assert sentinel.digest_due is None
    clock.now = EIGHT
    assert sentinel.digest(EIGHT) is None
    sentinel.tick()
    assert telegram.sent == []
    assert DIGEST_CALL not in http.calls
    assert "digest" not in sentinel.metrics()
    assert sentinel.round_budget() == 30


def test_the_digest_adds_one_fetch_to_the_round_budget(
    digest_rig: tuple[Any, FakeClock, FakeHttp, FakeTelegram],
) -> None:
    sentinel = digest_rig[0]
    assert sentinel.kinds == ("alert", "reminder", "recovery", "digest")
    assert sentinel.round_budget() == 40


def test_digest_settings_default_to_off() -> None:
    assert s.digest_from({}) == ("", "UTC", "")
    unused = {"AYE_SENTINEL_DIGEST_URL": "http://v", "AYE_SENTINEL_DIGEST_TIMEZONE": "Asia/Tokyo"}
    assert s.digest_from(unused) == ("", "UTC", "")
    assert (config().digest_at, config().digest_timezone, config().digest_url) == ("", "UTC", "")


def test_digest_settings_are_read_and_trimmed() -> None:
    env = {
        **ENV,
        "AYE_SENTINEL_DIGEST_AT": " 08:30 ",
        "AYE_SENTINEL_DIGEST_TIMEZONE": " Europe/Kyiv ",
        "AYE_SENTINEL_DIGEST_URL": " http://vm/api/v1/query ",
    }
    loaded = s.config_from(env)
    assert (loaded.digest_at, loaded.digest_timezone, loaded.digest_url) == (
        "08:30",
        "Europe/Kyiv",
        "http://vm/api/v1/query",
    )
    plain = {"AYE_SENTINEL_DIGEST_AT": "08:30", "AYE_SENTINEL_DIGEST_URL": "https://vm/q"}
    assert s.digest_from(plain) == ("08:30", "UTC", "https://vm/q")
    blank = {**plain, "AYE_SENTINEL_DIGEST_TIMEZONE": " "}
    assert s.digest_from(blank) == ("08:30", "UTC", "https://vm/q")


@pytest.mark.parametrize("url", ["", "ftp://vm/q", "vm/api/v1/query"])
def test_a_digest_needs_the_url_of_the_query_api(url: str) -> None:
    env = {"AYE_SENTINEL_DIGEST_AT": "08:30", "AYE_SENTINEL_DIGEST_URL": url}
    message = r"^AYE_SENTINEL_DIGEST_URL must be the http\(s\) url of /api/v1/query$"
    with pytest.raises(ValueError, match=message):
        s.digest_from(env)


def test_a_digest_with_a_bad_time_or_zone_stops_the_start() -> None:
    env = {"AYE_SENTINEL_DIGEST_AT": "8:30", "AYE_SENTINEL_DIGEST_URL": "http://vm/q"}
    with pytest.raises(ValueError, match=r"^not a time of day like 08:00: '8:30'$"):
        s.digest_from(env)
    zoned = {**env, "AYE_SENTINEL_DIGEST_AT": "08:30", "AYE_SENTINEL_DIGEST_TIMEZONE": "No/City"}
    with pytest.raises(s.ZoneInfoNotFoundError):
        s.digest_from(zoned)


def test_http_fetch_reads_up_to_the_given_limit_and_the_query_fetch_up_to_1_mib(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answer = Answer("200", b"body")
    web = Web(answer)
    monkeypatch.setattr(s, "urllib", web)
    monkeypatch.setattr(s, "OPENER", web)
    assert s.http_fetch("http://v/q", 2.5, 7) == (200, b"body")
    assert answer.sizes == [(7,)]
    calls: list[tuple[Any, ...]] = []

    def fetch(*args: Any) -> tuple[int, bytes]:
        calls.append(args)
        return 200, b"x"

    monkeypatch.setattr(s, "http_fetch", fetch)
    assert s.query_fetch("http://v/q", 2.5) == (200, b"x")
    assert calls == [("http://v/q", 2.5, 1048576)]
