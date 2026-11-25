from __future__ import annotations

import importlib.util
import json
import re
import sys
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import ModuleType
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
