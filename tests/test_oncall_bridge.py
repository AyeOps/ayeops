from __future__ import annotations

import importlib.util
import io
import json
import sys
import threading
import urllib.error
import urllib.request
from collections.abc import Mapping
from datetime import datetime
from email.message import Message
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import ModuleType
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from tests.conftest import ROOT, WAIT

FILES = ROOT / "src" / "ayeops" / "modules" / "oncall" / "files"
KYIV = ZoneInfo("Europe/Kyiv")
MINUTE = 60.0


def load_bridge() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "ayeops.modules.oncall.files.oncall_bridge", FILES / "oncall_bridge.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


bridge = load_bridge()
TEXTS = bridge.load_texts(FILES / "oncall_texts.json")


def at(day: int, hour: int, minute: int = 0, month: int = 10) -> float:
    return datetime(2026, month, day, hour, minute, tzinfo=KYIV).timestamp()


class Clock:
    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class FakeTelegram:
    def __init__(self, fail: bool = False) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.fail = fail

    def __call__(self, method: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        if self.fail:
            raise OSError("telegram is down")
        self.calls.append((method, dict(payload)))
        return {"ok": True, "result": {"message_id": len(self.calls)}}

    def sent(self) -> list[dict[str, Any]]:
        return [p for m, p in self.calls if m == "sendMessage" and "reply_markup" in p]


class FakeAck:
    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.ids: list[int] = []

    def __call__(self, alert_id: int) -> bool:
        self.ids.append(alert_id)
        return self.ok


QUIET = bridge.Quiet((23, 0), (8, 0), KYIV)


def engine(
    clock: Clock,
    quiet: Any = QUIET,
    telegram: FakeTelegram | None = None,
    ack: FakeAck | None = None,
    state: Path | None = None,
    max_repeats: int = 12,
    language: str = "uk",
) -> Any:
    config = bridge.Config(
        chat_id="4242",
        repeat_seconds=5 * MINUTE,
        max_repeats=max_repeats,
        quiet=quiet,
        language=language,
        texts=TEXTS,
    )
    return bridge.Engine(config, telegram or FakeTelegram(), ack or FakeAck(), clock, state)


def details(**rows: str) -> str:
    lines = ["| Label | Value |", "| ----- | ----- |"]
    lines += [f"| {k} | {v} |" for k, v in rows.items()]
    return "\n".join(lines)


def payload(alert_id: int = 7, severity: str = "critical", **extra: Any) -> dict[str, Any]:
    rows = {"alertname": "Disk full", "severity": severity, **extra.pop("rows", {})}
    return {
        "Type": "Alert",
        "AlertID": alert_id,
        "Summary": "Disk on central is full",
        "Details": details(**rows),
        "GoAlertURL": f"https://oncall.ops.acme.example/alerts/{alert_id}",
        **extra,
    }


def advance(clock: Clock, eng: Any, until: float, step: float = 15.0) -> None:
    while clock.now < until:
        clock.now = min(until, clock.now + step)
        eng.tick()


def test_a_day_alert_rings_then_repeats_every_five_minutes_up_to_the_limit() -> None:
    clock = Clock(at(20, 14))
    telegram = FakeTelegram()
    eng = engine(clock, telegram=telegram, max_repeats=3)
    eng.on_alert(payload())
    first = telegram.sent()
    assert len(first) == 1
    assert first[0]["disable_notification"] is False
    advance(clock, eng, at(20, 14, 4))
    assert len(telegram.sent()) == 1
    advance(clock, eng, at(20, 14, 5))
    assert len(telegram.sent()) == 2
    advance(clock, eng, at(20, 15, 30))
    sent = telegram.sent()
    assert len(sent) == 4
    assert all(p["disable_notification"] is False for p in sent)
    assert "повтор 3" in sent[-1]["text"]


def test_a_night_alert_sends_one_silent_message_and_repeats_start_at_eight() -> None:
    clock = Clock(at(20, 23, 30))
    telegram = FakeTelegram()
    eng = engine(clock, telegram=telegram)
    eng.on_alert(payload())
    assert [p["disable_notification"] for p in telegram.sent()] == [True]
    assert "08:00" in telegram.sent()[0]["text"]
    advance(clock, eng, at(21, 7, 59))
    assert len(telegram.sent()) == 1
    advance(clock, eng, at(21, 8, 0))
    assert [p["disable_notification"] for p in telegram.sent()] == [True, False]
    advance(clock, eng, at(21, 8, 5))
    assert len(telegram.sent()) == 3


def test_an_alert_acknowledged_at_night_stays_quiet_at_eight() -> None:
    clock = Clock(at(20, 2))
    telegram = FakeTelegram()
    ack = FakeAck()
    eng = engine(clock, telegram=telegram, ack=ack)
    eng.on_alert(payload())
    clock.now = at(20, 3)
    assert eng.acknowledge(7, "4242") == TEXTS["uk"]["acked"]
    assert ack.ids == [7]
    advance(clock, eng, at(20, 9))
    assert len(telegram.sent()) == 1


def test_a_page_rings_and_repeats_through_the_night() -> None:
    clock = Clock(at(20, 1))
    telegram = FakeTelegram()
    eng = engine(clock, telegram=telegram)
    eng.on_alert(payload(severity="page"))
    advance(clock, eng, at(20, 1, 10))
    sent = telegram.sent()
    assert len(sent) == 3
    assert all(p["disable_notification"] is False for p in sent)
    assert sent[0]["text"].startswith("🚨 <b>" + TEXTS["uk"]["page"])


def test_repeats_of_a_day_alert_pause_for_the_night_and_resume_at_eight() -> None:
    clock = Clock(at(20, 22, 58))
    telegram = FakeTelegram()
    eng = engine(clock, telegram=telegram)
    eng.on_alert(payload())
    advance(clock, eng, at(21, 7, 59))
    assert len(telegram.sent()) == 1
    advance(clock, eng, at(21, 8, 1))
    assert len(telegram.sent()) == 2


def test_without_quiet_hours_the_night_is_like_the_day() -> None:
    clock = Clock(at(20, 3))
    telegram = FakeTelegram()
    eng = engine(clock, quiet=None, telegram=telegram)
    eng.on_alert(payload())
    advance(clock, eng, at(20, 3, 5))
    assert [p["disable_notification"] for p in telegram.sent()] == [False, False]


def test_quiet_windows_wrap_midnight_and_an_empty_window_is_never_quiet() -> None:
    assert QUIET.active(at(20, 23, 0))
    assert QUIET.active(at(21, 7, 59))
    assert not QUIET.active(at(21, 8, 0))
    assert not QUIET.active(at(20, 22, 59))
    day = bridge.Quiet((13, 0), (14, 0), KYIV)
    assert day.active(at(20, 13, 30))
    assert not day.active(at(20, 14, 0))
    assert not day.active(at(20, 12, 59))
    assert not bridge.Quiet((8, 0), (8, 0), KYIV).active(at(20, 8, 0))


def test_quiet_hours_end_at_local_eight_across_the_switch_to_winter_time() -> None:
    assert QUIET.ends_after(at(24, 23, 30)) == at(25, 8, 0)
    assert at(25, 8, 0) - at(24, 23, 30) == pytest.approx(9.5 * 3600)
    assert QUIET.ends_after(at(25, 8, 0)) == at(26, 8, 0)
    assert QUIET.ends_after(at(25, 7, 59)) == at(25, 8, 0)


def test_hhmm_refuses_anything_but_a_clock_time() -> None:
    assert bridge.parse_hhmm(" 07:05 ") == (7, 5)
    for text in ("7:05", "24:00", "23:60", "noon"):
        with pytest.raises(ValueError, match="HH:MM"):
            bridge.parse_hhmm(text)


def test_links_come_from_the_alert_details_and_its_metadata_wins() -> None:
    clock = Clock(at(20, 12))
    telegram = FakeTelegram()
    eng = engine(clock, telegram=telegram)
    eng.on_alert(
        payload(
            rows={
                "runbook_url": "https://docs.acme.example/runbook",
                "dashboard_url": "https://grafana.acme.example/d/x",
                "diagnosis": "from the table",
            },
            Meta={"diagnosis": "disk filled by /var/log", "diagnosis_url": "https://agent/1"},
        )
    )
    text = telegram.sent()[0]["text"]
    assert '<a href="https://grafana.acme.example/d/x">Дашборд</a>' in text
    assert '<a href="https://docs.acme.example/runbook">Runbook</a>' in text
    assert '<a href="https://agent/1">Діагноз</a>' in text
    assert "disk filled by /var/log" in text
    assert "from the table" not in text
    assert '<a href="https://oncall.ops.acme.example/alerts/7">GoAlert</a>' in text


def test_only_web_links_are_shown_and_html_in_the_alert_is_escaped() -> None:
    clock = Clock(at(20, 12))
    telegram = FakeTelegram()
    eng = engine(clock, telegram=telegram, language="en")
    alert = payload(rows={"runbook_url": "javascript:alert(1)"})
    alert["Summary"] = "<b>boom</b> & co"
    eng.on_alert(alert)
    text = telegram.sent()[0]["text"]
    assert "javascript" not in text
    assert "&lt;b&gt;boom&lt;/b&gt; &amp; co" in text
    assert text.startswith("🚨 <b>Alert: ")


def test_details_rows_unescape_pipes_and_breaks_and_skip_headers() -> None:
    text = "\n".join(
        [
            "| Label | Value |",
            "| ----- | ----- |",
            "| a | x \\| y |",
            "| Annotation | Value |",
            "| b | one<br />two |",
            "| a | second |",
            "not a row",
        ]
    )
    assert bridge.details_fields(text) == {"a": "x | y", "b": "one\ntwo"}


def test_a_second_notification_of_an_open_alert_adds_the_diagnosis_without_a_message() -> None:
    clock = Clock(at(20, 12))
    telegram = FakeTelegram()
    eng = engine(clock, telegram=telegram)
    eng.on_alert(payload())
    eng.on_alert(payload(Meta={"diagnosis": "found it"}))
    assert len(telegram.sent()) == 1
    advance(clock, eng, at(20, 12, 5))
    assert "found it" in telegram.sent()[-1]["text"]


def test_status_updates_close_the_alert_and_take_the_button_away() -> None:
    clock = Clock(at(20, 12))
    telegram = FakeTelegram()
    eng = engine(clock, telegram=telegram)
    eng.on_alert(payload())
    eng.on_status({"Type": "AlertStatus", "AlertID": 7, "LogEntry": "Something else"})
    assert eng.alerts[7].state == "open"
    eng.on_status({"Type": "AlertStatus", "AlertID": 7, "LogEntry": "Closed by Grafana"})
    assert eng.alerts[7].state == "closed"
    methods = [m for m, _ in telegram.calls]
    assert methods == ["sendMessage", "editMessageReplyMarkup", "sendMessage"]
    assert telegram.calls[1][1]["reply_markup"] == {"inline_keyboard": []}
    assert TEXTS["uk"]["closed"] in telegram.calls[2][1]["text"]
    advance(clock, eng, at(20, 13))
    assert len(telegram.calls) == 3


def test_an_acknowledged_status_from_goalert_stops_the_repeats() -> None:
    clock = Clock(at(20, 12))
    telegram = FakeTelegram()
    eng = engine(clock, telegram=telegram)
    eng.on_alert(payload())
    eng.on_status({"AlertID": 7, "LogEntry": "Acknowledged by Ops (Web)"})
    assert eng.alerts[7].state == "acked"
    advance(clock, eng, at(20, 13))
    assert len(telegram.sent()) == 1


def test_the_button_works_only_for_the_person_on_call() -> None:
    clock = Clock(at(20, 12))
    ack = FakeAck()
    eng = engine(clock, ack=ack)
    eng.on_alert(payload())
    assert eng.acknowledge(7, "999") == TEXTS["uk"]["denied"]
    assert ack.ids == []
    assert eng.alerts[7].state == "open"


def test_a_refused_acknowledgement_keeps_repeating_and_counts_a_failure() -> None:
    clock = Clock(at(20, 12))
    telegram = FakeTelegram()
    eng = engine(clock, telegram=telegram, ack=FakeAck(ok=False))
    eng.on_alert(payload())
    assert eng.acknowledge(7, "4242") == TEXTS["uk"]["failed"]
    assert eng.counters.get("failures_total", op="goalert") == 1
    advance(clock, eng, at(20, 12, 5))
    assert len(telegram.sent()) == 2


def test_a_telegram_failure_is_counted_and_the_message_is_retried_on_the_next_tick() -> None:
    clock = Clock(at(20, 12))
    telegram = FakeTelegram(fail=True)
    eng = engine(clock, telegram=telegram)
    eng.on_alert(payload())
    assert eng.counters.get("failures_total", op="telegram") == 1
    assert eng.alerts[7].sent == 0
    telegram.fail = False
    advance(clock, eng, at(20, 12, 5))
    assert len(telegram.sent()) == 1
    assert "повтор" not in telegram.sent()[0]["text"]


def test_open_alerts_survive_a_restart(tmp_path: Path) -> None:
    clock = Clock(at(20, 12))
    state = tmp_path / "alerts.json"
    eng = engine(clock, state=state)
    eng.on_alert(payload(alert_id=1))
    eng.on_alert(payload(alert_id=2))
    eng.on_status({"AlertID": 2, "LogEntry": "Closed"})
    telegram = FakeTelegram()
    again = engine(clock, telegram=telegram, state=state)
    assert sorted(again.alerts) == [1]
    advance(clock, again, at(20, 12, 5))
    assert len(telegram.sent()) == 1
    assert again.counters.get("open_alerts") == 1


def test_a_broken_state_file_starts_empty(tmp_path: Path) -> None:
    state = tmp_path / "alerts.json"
    state.write_text("{not json", "utf-8")
    assert engine(Clock(at(20, 12)), state=state).alerts == {}


def test_callbacks_acknowledge_and_start_tells_the_chat_id() -> None:
    clock = Clock(at(20, 12))
    telegram = FakeTelegram()
    ack = FakeAck()
    eng = engine(clock, telegram=telegram, ack=ack)
    eng.on_alert(payload())
    press = {"id": "cb1", "data": "ack:7", "message": {"chat": {"id": 4242}}}
    bridge.handle_update(eng, telegram, {"callback_query": press})
    assert ack.ids == [7]
    assert telegram.calls[-1] == (
        "answerCallbackQuery",
        {"callback_query_id": "cb1", "text": TEXTS["uk"]["acked"]},
    )
    junk = {"id": "cb2", "data": "ack:x", "message": {"chat": {"id": 4242}}}
    bridge.handle_update(eng, telegram, {"callback_query": junk})
    assert telegram.calls[-1][1]["text"] == TEXTS["uk"]["denied"]
    assert ack.ids == [7]
    bridge.handle_update(eng, telegram, {"message": {"text": "/start", "chat": {"id": 555}}})
    assert telegram.calls[-1] == (
        "sendMessage",
        {"chat_id": 555, "text": TEXTS["uk"]["start"].format(chat=555)},
    )


def test_polling_moves_the_offset_and_records_the_time() -> None:
    clock = Clock(at(20, 12))
    eng = engine(clock)
    updates = {"ok": True, "result": [{"update_id": 10}, {"update_id": 12}]}

    def post(method: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        assert method == "getUpdates"
        assert payload["offset"] == 5
        return updates

    assert bridge.poll_once(eng, post, 5, clock) == 13
    assert eng.counters.get("last_poll_timestamp_seconds") == clock.now


def test_a_token_polled_by_another_process_stops_the_bridge_from_polling() -> None:
    eng = engine(Clock(at(20, 12)))

    def conflict(method: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        raise urllib.error.HTTPError("u", 409, "Conflict", Message(), io.BytesIO())

    with pytest.raises(bridge.Conflict):
        bridge.poll_once(eng, conflict, 0, eng.clock)
    assert eng.counters.get("poll_conflict") == 1

    def other(method: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        raise urllib.error.HTTPError("u", 502, "Bad", Message(), io.BytesIO())

    with pytest.raises(urllib.error.HTTPError):
        bridge.poll_once(eng, other, 0, eng.clock)


def test_metrics_render_exact_timestamps_and_labels() -> None:
    counters = bridge.Counters()
    counters.set("last_poll_timestamp_seconds", 1792862392.50517)
    counters.add("failures_total", op="relay")
    counters.add("failures_total", op="relay")
    text = counters.render()
    assert "ayeops_oncall_last_poll_timestamp_seconds 1792862392.50517\n" in text
    assert 'ayeops_oncall_failures_total{op="relay"} 2\n' in text


def test_grafana_links_fill_a_missing_dashboard_annotation_before_goalert() -> None:
    body: dict[str, Any] = {
        "version": "1",
        "alerts": [
            {
                "annotations": {},
                "panelURL": "https://g/d/x?viewPanel=2",
                "dashboardURL": "https://g/d/x",
            },
            {"dashboardURL": "https://g/d/y"},
            {"annotations": {"dashboard_url": "https://kept"}, "dashboardURL": "https://g/d/z"},
            {"annotations": {}, "dashboardURL": "ftp://nope"},
        ],
    }
    bridge.fill_links(body)
    assert [a.get("annotations", {}).get("dashboard_url") for a in body["alerts"]] == [
        "https://g/d/x?viewPanel=2",
        "https://g/d/y",
        "https://kept",
        None,
    ]
    single = {"version": "1", "groupKey": "k", "alerts": [body["alerts"][0]]}
    out = json.loads(bridge.enrich(json.dumps(single).encode()))
    assert out["alerts"][0]["annotations"]["dashboard_url"] == "https://g/d/x?viewPanel=2"
    assert out["version"] == "1"
    assert bridge.enrich(b"not json") == b"not json"
    assert bridge.enrich(b"[1]") == b"[1]"


def grafana_alert(host: str, status: str = "firing", **labels: str) -> dict[str, Any]:
    return {
        "status": status,
        "labels": {"alertname": "Host down", "severity": "critical", "host": host, **labels},
        "annotations": {
            "summary": f"{host} stopped reporting node metrics",
            "runbook_url": "https://runbooks/host-down.md",
        },
        "fingerprint": f"fp-{host}",
    }


def grouped(alerts: list[dict[str, Any]], key: str = '{}:{scope="workers"}') -> bytes:
    return json.dumps(
        {
            "version": "1",
            "groupKey": key,
            "groupLabels": {"alertname": "Host down", "scope": "workers"},
            "commonLabels": {"alertname": "Host down", "severity": "critical"},
            "alerts": alerts,
        }
    ).encode()


def test_a_grouped_notification_reaches_goalert_as_one_alert_naming_count_and_hosts() -> None:
    hosts = [f"worker-{n:02d}" for n in range(14, 0, -1)]
    out = json.loads(bridge.enrich(grouped([grafana_alert(h) for h in hosts])))
    assert len(out["alerts"]) == 1
    alert = out["alerts"][0]
    assert alert["status"] == "firing"
    assert alert["annotations"]["summary"] == (
        "14 x Host down in workers: " + ", ".join(sorted(hosts))
    )
    assert alert["annotations"]["runbook_url"] == "https://runbooks/host-down.md"
    assert alert["labels"] == {"alertname": "Host down", "severity": "critical"}
    assert alert["annotations"]["fingerprint"] == alert["fingerprint"]


def test_every_notification_of_a_group_dedups_on_the_group_key_until_it_resolves() -> None:
    first = json.loads(bridge.enrich(grouped([grafana_alert("worker-01")])))["alerts"][0]
    assert first["annotations"]["summary"] == "worker-01 stopped reporting node metrics"
    more = [grafana_alert("worker-01"), grafana_alert("worker-02", "resolved")]
    second = json.loads(bridge.enrich(grouped(more)))["alerts"][0]
    assert second["status"] == "firing"
    assert second["annotations"]["summary"] == "worker-01 stopped reporting node metrics"
    gone = [grafana_alert("worker-01", "resolved"), grafana_alert("worker-02", "resolved")]
    last = json.loads(bridge.enrich(grouped(gone)))["alerts"][0]
    assert last["status"] == "resolved"
    assert first["fingerprint"] == second["fingerprint"] == last["fingerprint"]
    other = json.loads(bridge.enrich(grouped([grafana_alert("db-1")], key="other")))
    assert other["alerts"][0]["fingerprint"] != first["fingerprint"]


def test_a_group_of_one_host_names_the_count_without_repeating_the_host() -> None:
    disks = [grafana_alert("web-1", mountpoint=m) for m in ("/", "/data")]
    body = json.loads(grouped(disks))
    body["groupLabels"]["scope"] = "web-1"
    out = json.loads(bridge.enrich(json.dumps(body).encode()))
    assert out["alerts"][0]["annotations"]["summary"] == "2 x Host down: web-1"


def test_the_public_integration_url_is_called_inside_the_compose_network() -> None:
    url = "https://oncall.ops.acme.example/api/v2/grafana/incoming?token=abc"
    assert bridge.internal(url, "https://oncall.ops.acme.example", "http://goalert:8081") == (
        "http://goalert:8081/api/v2/grafana/incoming?token=abc"
    )
    assert bridge.internal("http://other/x", "https://oncall.ops.acme.example", "http://g") == (
        "http://other/x"
    )


def settings(tmp_path: Path, **overrides: str) -> Any:
    env = {
        "AYE_ONCALL_TELEGRAM_TOKEN": "1:x",
        "AYE_ONCALL_WEBHOOK_SECRET": "s3cret",
        "AYE_ONCALL_CHAT_ID": "4242",
        "AYE_ONCALL_STATE": str(tmp_path),
        "AYE_ONCALL_TEXTS": str(FILES / "oncall_texts.json"),
        "AYE_ONCALL_LISTEN": "0",
        **overrides,
    }
    return bridge.settings_from(env)


def test_settings_read_quiet_hours_repeats_and_language(tmp_path: Path) -> None:
    parsed = settings(
        tmp_path,
        AYE_ONCALL_QUIET_START="22:30",
        AYE_ONCALL_QUIET_END="07:15",
        AYE_ONCALL_TIMEZONE="Europe/Kyiv",
        AYE_ONCALL_REPEAT_MINUTES="3",
        AYE_ONCALL_MAX_REPEATS="4",
        AYE_ONCALL_LANGUAGE="uk",
    )
    assert parsed.config.quiet == bridge.Quiet((22, 30), (7, 15), KYIV)
    assert (parsed.config.repeat_seconds, parsed.config.max_repeats) == (180.0, 4)
    assert parsed.config.language == "uk"
    assert parsed.goalert == "http://goalert:8081"
    assert parsed.telegram_api == "https://api.telegram.org"
    assert settings(tmp_path).config.quiet is None


class Server:
    def __init__(self, parsed: Any, eng: Any) -> None:
        handler = bridge.make_handler(parsed, eng, eng.clock)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def call(self, path: str, body: Any = None, headers: dict[str, str] | None = None) -> int:
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(self.base + path, data=data, headers=headers or {})
        try:
            with urllib.request.urlopen(request, timeout=WAIT) as response:
                return int(response.status)
        except urllib.error.HTTPError as error:
            return int(error.code)

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


def test_the_http_side_checks_the_secret_and_routes_goalert_messages(tmp_path: Path) -> None:
    parsed = settings(tmp_path)
    clock = Clock(at(20, 12))
    telegram = FakeTelegram()
    eng = engine(clock, telegram=telegram)
    server = Server(parsed, eng)
    try:
        assert server.call("/grafana", {"alerts": []}) == 401
        assert server.call("/grafana", {"alerts": []}, {"Authorization": "Bearer wrong"}) == 401
        assert server.call("/grafana", {"alerts": []}, {"Authorization": "Bearer s3cret"}) == 503
        assert server.call("/goalert/wrong", payload()) == 404
        assert server.call("/goalert/s3cret", payload()) == 200
        assert len(telegram.sent()) == 1
        assert server.call("/goalert/s3cret", {"Type": "Verification", "Code": "123456"}) == 200
        assert json.loads((tmp_path / "verification.json").read_text("utf-8")) == {"code": "123456"}
        assert server.call("/health") == 503
        (tmp_path / "setup.json").write_text(json.dumps({"ok": True}), "utf-8")
        eng.counters.set("last_poll_timestamp_seconds", clock.now)
        assert server.call("/health") == 200
        clock.now += 1000
        assert server.call("/health") == 503
        assert server.call("/metrics") == 200
        assert server.call("/nothing") == 404
    finally:
        server.close()


def test_only_a_notification_goalert_took_reaches_the_incident_topic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    codes = iter([502, 200])
    monkeypatch.setattr(bridge, "relay", lambda settings, body: next(codes))
    clock = Clock(at(20, 12))
    telegram = FakeTelegram()
    group = "-1001234567890"
    config = bridge.Config(
        chat_id="4242",
        repeat_seconds=5 * MINUTE,
        max_repeats=12,
        quiet=None,
        texts=TEXTS,
        ops_chat_id=group,
        topics=True,
    )
    eng = bridge.Engine(config, telegram, FakeAck(), clock)
    body = json.loads(grouped([grafana_alert("worker-01"), grafana_alert("worker-02")]))
    lead = json.loads(bridge.enrich(json.dumps(body).encode()))["alerts"][0]
    eng.topics[5] = bridge.Topic(5, lead["fingerprint"], "worker-01 down", 7, clock.now)
    server = Server(settings(tmp_path), eng)
    try:
        auth = {"Authorization": "Bearer s3cret"}
        assert server.call("/grafana", body, auth) == 502
        assert telegram.calls == []
        assert server.call("/grafana", body, auth) == 200
    finally:
        server.close()
    ((method, sent),) = telegram.calls
    assert (method, sent["chat_id"], sent["message_thread_id"]) == ("sendMessage", group, 5)
    assert "2 x Host down in workers: worker-01, worker-02" in sent["text"]


def test_relay_forwards_to_goalert_and_passes_its_status_through(tmp_path: Path) -> None:
    parsed = settings(tmp_path, AYE_ONCALL_PUBLIC_URL="https://oncall.acme.example")
    (tmp_path / "setup.json").write_text(
        json.dumps({"grafana_url": "https://oncall.acme.example/api/v2/grafana/incoming?token=t"}),
        "utf-8",
    )
    seen: list[str] = []

    class Answer:
        status = 200

        def __enter__(self) -> Answer:
            return self

        def __exit__(self, *args: Any) -> None:
            return None

    def opener(request: urllib.request.Request, timeout: float) -> Answer:
        seen.append(request.full_url)
        return Answer()

    assert bridge.relay(parsed, b"{}", opener) == 200
    assert seen == ["http://goalert:8081/api/v2/grafana/incoming?token=t"]

    def refused(request: urllib.request.Request, timeout: float) -> Answer:
        raise urllib.error.HTTPError(request.full_url, 400, "bad", Message(), io.BytesIO())

    def down(request: urllib.request.Request, timeout: float) -> Answer:
        raise OSError("connection refused")

    assert bridge.relay(parsed, b"{}", refused) == 400
    assert bridge.relay(parsed, b"{}", down) == 502


def test_both_languages_carry_every_text() -> None:
    assert set(TEXTS) == {"en", "uk"}
    assert set(TEXTS["en"]) == set(TEXTS["uk"])
    for table in TEXTS.values():
        assert "{end}" in table["quiet"]
        assert "{chat}" in table["start"]
        assert "{n}" in table["repeat"]


def test_setup_attempts_show_as_a_gauge_and_failures_as_a_counter() -> None:
    counters = bridge.Counters()
    report = bridge.setup_reporter(counters)
    report(False)
    report(False)
    assert counters.get("setup_ok") == 0
    assert counters.get("failures_total", op="setup") == 2
    report(True)
    assert counters.get("setup_ok") == 1
    assert counters.get("failures_total", op="setup") == 2


GOALERT_LOG = json.loads(
    (ROOT / "tests" / "fixtures" / "goalert" / "alert-log.json").read_text("utf-8")
)
LOG_NODES = [node for page in GOALERT_LOG for node in page["data"]["alerts"]["nodes"]]


def utc(text: str) -> float:
    return datetime.fromisoformat(text).timestamp()


class Pages:
    def __init__(self, pages: list[Any]) -> None:
        self.pages = list(pages)
        self.requests: list[urllib.request.Request] = []

    def __call__(self, request: urllib.request.Request, timeout: float) -> io.BytesIO:
        self.requests.append(request)
        return io.BytesIO(json.dumps(self.pages.pop(0)).encode())

    def bodies(self) -> list[dict[str, Any]]:
        return [json.loads(r.data if isinstance(r.data, bytes) else b"{}") for r in self.requests]


def test_the_alert_log_reader_pages_with_its_bound_query_and_the_read_token() -> None:
    pages = Pages(GOALERT_LOG)
    read = bridge.goalert_reader("http://goalert:8081/", lambda: "read-token", pages)
    nodes = read(utc("2026-01-01T04:00:00+00:00"), utc("2026-01-01T09:00:00+00:00"))
    assert [node["alertID"] for node in nodes] == [41, 42, 43, 44]
    assert [r.full_url for r in pages.requests] == ["http://goalert:8081/api/graphql"] * 2
    assert {r.get_header("Authorization") for r in pages.requests} == {"Bearer read-token"}
    first, second = pages.bodies()
    assert first["query"] == second["query"] == bridge.READ_QUERY
    assert first["variables"]["input"] == {
        "notCreatedBefore": "2026-01-01T04:00:00Z",
        "createdBefore": "2026-01-01T09:00:00Z",
        "first": 100,
        "after": "",
    }
    assert second["variables"]["input"]["after"] == "eyJpZCI6NDJ9"


def test_the_reader_waits_for_its_key_and_refuses_errors_and_endless_pages() -> None:
    with pytest.raises(bridge.NotReady):
        bridge.goalert_reader("http://g", lambda: "", Pages([]))(0.0, 60.0)
    refused = Pages([{"errors": [{"message": "query not allowed"}]}])
    with pytest.raises(RuntimeError, match="refused"):
        bridge.goalert_reader("http://g", lambda: "t", refused)(0.0, 60.0)
    endless = {
        "data": {"alerts": {"nodes": [], "pageInfo": {"endCursor": "c", "hasNextPage": True}}}
    }
    many = Pages([endless] * bridge.READ_PAGES)
    with pytest.raises(RuntimeError, match="more than 2000 alerts"):
        bridge.goalert_reader("http://g", lambda: "t", many)(0.0, 60.0)
    assert len(many.requests) == bridge.READ_PAGES


def test_acks_take_who_and_when_from_the_alert_log_and_a_button_press_names_the_person(
    tmp_path: Path,
) -> None:
    clock = Clock(utc("2026-01-01T08:13:10+00:00"))
    ack = FakeAck()
    eng = engine(clock, ack=ack, state=tmp_path / "alerts.json")
    assert eng.acknowledge(41, "4242", "Olena Kovalenko") == TEXTS["uk"]["acked"]
    found = {entry["alert_id"]: entry for entry in eng.acks(LOG_NODES)}
    assert set(found) == {41, 42, 43, 44}
    web = found[41]
    assert web["created_at"] == utc("2026-01-01T08:10:31.204518+00:00")
    assert web["acknowledged_at"] == utc("2026-01-01T08:13:10.482915+00:00")
    assert (web["acknowledged_by"], web["acknowledged_via"]) == ("Olena Kovalenko", "telegram")
    assert web["labels"] == {
        "alertname": "Website down",
        "grafana_folder": "AyeOps",
        "host": "central",
        "instance": "https://acme.example",
        "service": "web",
        "severity": "critical",
    }
    assert web["status"] == "StatusClosed"
    disk = found[42]
    assert (disk["acknowledged_by"], disk["acknowledged_via"]) == ("Acme On-call", "web")
    assert disk["acknowledged_at"] == utc("2026-01-01T05:31:47.9+00:00")
    hosts = found[43]
    assert (hosts["acknowledged_at"], hosts["acknowledged_by"], hosts["acknowledged_via"]) == (
        None,
        None,
        None,
    )
    assert "host" not in hosts["labels"]
    backups = found[44]
    assert backups["acknowledged_at"] == utc("2026-01-01T08:41:00+00:00")
    assert (backups["acknowledged_by"], backups["acknowledged_via"]) == (None, "api-key")


def test_an_alert_without_an_id_or_a_creation_time_is_left_out() -> None:
    eng = engine(Clock(0.0))
    nodes = [{"alertID": "x", "createdAt": "2026-01-01T00:00:00Z"}, {"alertID": 3}]
    assert eng.acks(nodes) == []


def test_ack_lines_with_and_without_a_subject_and_a_classifier() -> None:
    def one(message: str) -> Any:
        return bridge.first_ack([{"timestamp": "2026-01-01T00:00:00Z", "message": message}])

    assert one("Acknowledged by Acme On-call (Web)") == (
        utc("2026-01-01T00:00:00+00:00"),
        "Acme On-call",
        "web",
    )
    assert one("Acknowledged by Bob") == (utc("2026-01-01T00:00:00+00:00"), "Bob", "")
    assert one("Acknowledged by [unknown] (Web)")[1:] == ("", "web")
    assert one("Acknowledged via Grafana integration")[1:] == ("", "grafana integration")
    assert one("Acknowledged")[1:] == ("", "")
    assert one("Closed by Bob (Web)") is None
    assert one("Unacknowledged by Bob (Web)") is None
    assert bridge.first_ack([{"timestamp": "yesterday", "message": "Acknowledged"}]) is None
    assert bridge.first_ack("not a list") is None
    assert bridge.first_ack(["not a map"]) is None


def test_presses_survive_a_restart_and_are_forgotten_after_35_days(tmp_path: Path) -> None:
    clock = Clock(at(20, 12))
    state = tmp_path / "alerts.json"
    eng = engine(clock, state=state)
    eng.acknowledge(9, "4242", "Olena")
    again = engine(clock, state=state)
    assert again.presses == {9: {"by": "Olena", "at": at(20, 12)}}
    clock.now += 35 * 86400
    again.save()
    assert 9 in again.presses
    clock.now += 1
    again.save()
    assert again.presses == {}
    assert engine(clock, state=state).presses == {}


def test_a_refused_press_is_not_remembered() -> None:
    eng = engine(Clock(at(20, 12)), ack=FakeAck(ok=False))
    eng.acknowledge(9, "4242", "Olena")
    eng.acknowledge(9, "999", "Stranger")
    assert eng.presses == {}


def get(server: Server, path: str, headers: dict[str, str] | None = None) -> tuple[int, Any]:
    request = urllib.request.Request(server.base + path, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=WAIT) as response:
            return int(response.status), json.loads(response.read())
    except urllib.error.HTTPError as error:
        return int(error.code), None


def test_acks_answer_with_their_own_secret_for_a_bounded_window(tmp_path: Path) -> None:
    windows: list[tuple[float, float]] = []

    def reader(start: float, end: float) -> list[Any]:
        windows.append((start, end))
        return LOG_NODES

    eng = engine(Clock(at(20, 12)))
    parsed = settings(tmp_path, AYE_ONCALL_ACKS_SECRET="r3ad")
    server = Server(parsed, eng)
    server.httpd.RequestHandlerClass = bridge.make_handler(parsed, eng, eng.clock, reader)
    auth = {"Authorization": "Bearer r3ad"}
    try:
        assert get(server, "/acks?from=0&to=60")[0] == 401
        assert get(server, "/acks?from=0&to=60", {"Authorization": "Bearer s3cret"})[0] == 401
        assert get(server, "/acks", auth)[0] == 400
        assert get(server, "/acks?from=60&to=0", auth)[0] == 400
        assert get(server, "/acks?from=0&to=x", auth)[0] == 400
        assert get(server, f"/acks?from=0&to={8 * 86400 + 1}", auth)[0] == 400
        status, body = get(server, f"/acks?from=100&to={8 * 86400 + 100}", auth)
        assert status == 200
        assert [entry["alert_id"] for entry in body["alerts"]] == [41, 42, 43, 44]
        assert windows == [(100.0, 8 * 86400 + 100.0)]
        assert eng.counters.get("acks_reads_total") == 1
    finally:
        server.close()


def test_acks_say_when_the_key_is_missing_or_goalert_fails(tmp_path: Path) -> None:
    def waiting(start: float, end: float) -> list[Any]:
        raise bridge.NotReady("no key yet")

    def broken(start: float, end: float) -> list[Any]:
        raise OSError("goalert is down")

    eng = engine(Clock(at(20, 12)))
    parsed = settings(tmp_path, AYE_ONCALL_ACKS_SECRET="r3ad")
    auth = {"Authorization": "Bearer r3ad"}
    for reader, code in ((None, 503), (waiting, 503), (broken, 502)):
        server = Server(parsed, eng)
        server.httpd.RequestHandlerClass = bridge.make_handler(parsed, eng, eng.clock, reader)
        try:
            assert get(server, "/acks?from=0&to=60", auth)[0] == code
        finally:
            server.close()
    assert eng.counters.get("failures_total", op="acks") == 1


def test_without_an_acks_secret_there_is_no_acks_endpoint(tmp_path: Path) -> None:
    eng = engine(Clock(at(20, 12)))
    parsed = settings(tmp_path)
    assert parsed.acks_secret == ""
    server = Server(parsed, eng)
    server.httpd.RequestHandlerClass = bridge.make_handler(parsed, eng, eng.clock, lambda s, e: [])
    try:
        assert get(server, "/acks?from=0&to=60", {"Authorization": "Bearer "})[0] == 404
    finally:
        server.close()


def test_labels_come_from_the_label_table_only_even_without_a_blank_line() -> None:
    details = "\n".join(
        [
            "| Label | Value |",
            "| ----- | ----- |",
            "| alertname | Disk full |",
            "| summary | a \\| b |",
            "| Annotation | Value |",
            "| ---------- | ----- |",
            "| host | from-an-annotation |",
            "",
            "| Label | Value |",
            "| host | edge |",
        ]
    )
    assert bridge.label_rows(details) == {
        "alertname": "Disk full",
        "summary": "a | b",
        "host": "edge",
    }
    assert bridge.label_rows("no table at all") == {}


def test_an_unknown_person_keeps_how_the_alert_was_acknowledged() -> None:
    eng = engine(Clock(at(20, 12)))
    node = {
        "alertID": 5,
        "createdAt": "2026-01-01T00:00:00Z",
        "details": "",
        "recentEvents": {
            "nodes": [
                {"timestamp": "2026-01-01T00:01:00Z", "message": "Acknowledged by [unknown] (Web)"}
            ]
        },
    }
    eng.presses[5] = {"by": "Olena", "at": at(20, 12)}
    [entry] = eng.acks([node])
    assert (entry["acknowledged_by"], entry["acknowledged_via"]) == (None, "web")


def test_utc_works_on_a_host_without_a_time_zone_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing(key: str) -> Any:
        raise bridge.ZoneInfoNotFoundError(f"No time zone found with key {key}")

    monkeypatch.setattr(bridge, "ZoneInfo", missing)
    zone = bridge.time_zone("UTC")
    assert datetime(2026, 11, 6, 12, tzinfo=zone).isoformat() == "2026-11-06T12:00:00+00:00"
    with pytest.raises(bridge.ZoneInfoNotFoundError):
        bridge.time_zone("Europe/Kyiv")
