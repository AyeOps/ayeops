from __future__ import annotations

import importlib.util
import io
import json
import sys
import urllib.error
from collections.abc import Mapping
from email.message import Message
from pathlib import Path
from types import ModuleType
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from tests.test_oncall_bridge import (
    FILES,
    KYIV,
    MINUTE,
    QUIET,
    TEXTS,
    Clock,
    FakeAck,
    advance,
    at,
    grafana_alert,
    grouped,
    payload,
)


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

GROUP = "-1001234567890"
UTC = ZoneInfo("UTC")
PERSON = "4242"
SUMMARY = "Disk on central is full"


def refused(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://api.telegram.org", code, "no", Message(), io.BytesIO())


class ForumTelegram:
    def __init__(
        self, forum: bool = True, rights: bool = True, status: str = "administrator"
    ) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.forum = forum
        self.rights = rights
        self.status = status
        self.broken: dict[str, Exception] = {}
        self.threads = 100

    def __call__(self, method: str, body: Mapping[str, Any]) -> Mapping[str, Any]:
        if method in self.broken:
            raise self.broken[method]
        self.calls.append((method, dict(body)))
        if method == "getChat":
            return {"ok": True, "result": {"id": int(GROUP), "is_forum": self.forum}}
        if method == "getMe":
            return {"ok": True, "result": {"id": 99}}
        if method == "getChatMember":
            member = {"status": self.status, "can_manage_topics": self.rights}
            return {"ok": True, "result": member}
        if method == "createForumTopic":
            self.threads += 1
            return {"ok": True, "result": {"message_thread_id": self.threads}}
        return {"ok": True, "result": {"message_id": len(self.calls)}}

    def methods(self) -> list[str]:
        return [m for m, _ in self.calls]

    def made(self, method: str) -> list[dict[str, Any]]:
        return [p for m, p in self.calls if m == method]

    def group(self) -> list[dict[str, Any]]:
        return [p for p in self.made("sendMessage") if p["chat_id"] == GROUP]

    def direct(self) -> list[dict[str, Any]]:
        return [p for p in self.made("sendMessage") if p["chat_id"] == PERSON]


def engine(
    clock: Clock,
    telegram: Any,
    ack: FakeAck | None = None,
    state: Path | None = None,
    topics: bool = True,
    quiet: Any = None,
    zone: ZoneInfo = UTC,
) -> Any:
    config = bridge.Config(
        chat_id=PERSON,
        repeat_seconds=5 * MINUTE,
        max_repeats=12,
        quiet=quiet,
        language="uk",
        texts=TEXTS,
        ops_chat_id=GROUP,
        topics=topics,
        zone=zone,
    )
    return bridge.Engine(config, telegram, ack or FakeAck(), clock, state)


def press(eng: Any, telegram: ForumTelegram, chat: str, user: dict[str, str]) -> None:
    callback = {"id": "cb", "data": "ack:7", "message": {"chat": {"id": int(chat)}}, "from": user}
    bridge.handle_update(eng, telegram, {"callback_query": callback})


def test_a_critical_alert_opens_a_topic_and_still_messages_the_person() -> None:
    clock = Clock(at(20, 12))
    telegram = ForumTelegram()
    eng = engine(clock, telegram)
    eng.on_alert(payload(rows={"runbook_url": "https://docs/r"}))
    assert telegram.made("createForumTopic") == [{"chat_id": GROUP, "name": "🔴 " + SUMMARY}]
    (first,) = telegram.group()
    assert first["message_thread_id"] == 101
    assert SUMMARY in first["text"] and 'href="https://docs/r"' in first["text"]
    assert 'href="https://oncall.ops.acme.example/alerts/7"' in first["text"]
    assert first["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "ack:7"
    assert first["disable_notification"] is False
    (direct,) = telegram.direct()
    assert "message_thread_id" not in direct
    assert direct["reply_markup"] == first["reply_markup"]
    assert eng.counters.get("topics_available") == 1
    assert eng.counters.get("topics_total", kind="created") == 1
    assert eng.counters.get("topic_messages_total") == 1


def test_the_whole_incident_lives_in_its_topic_until_it_is_closed() -> None:
    clock = Clock(at(20, 12))
    telegram = ForumTelegram()
    ack = FakeAck()
    eng = engine(clock, telegram, ack=ack)
    eng.on_alert(payload())
    advance(clock, eng, at(20, 12, 5))
    repeat = telegram.group()[-1]
    assert repeat["message_thread_id"] == 101
    assert repeat["text"] == "🔁 " + TEXTS["uk"]["repeat"].format(n=1)
    assert repeat["disable_notification"] is True
    assert "reply_markup" not in repeat
    assert len(telegram.direct()) == 2
    first_id = next(
        i
        for i, (m, p) in enumerate(telegram.calls, 1)
        if p.get("chat_id") == GROUP and m == "sendMessage"
    )
    clock.now = at(20, 12, 7)
    press(eng, telegram, GROUP, {"first_name": "Olena", "last_name": "K"})
    assert ack.ids == [7]
    assert telegram.calls[-1][1]["text"] == TEXTS["uk"]["acked"]
    edits = telegram.made("editMessageReplyMarkup")
    assert {
        "chat_id": GROUP,
        "message_id": first_id,
        "reply_markup": {"inline_keyboard": []},
    } in edits
    assert any(e["chat_id"] == PERSON for e in edits)
    acked = [p for p in telegram.group() if "Olena" in p["text"]]
    assert acked[0]["text"] == "✅ " + TEXTS["uk"]["acked_by"].format(who="Olena K", time="09:07")
    assert acked[0]["message_thread_id"] == 101
    advance(clock, eng, at(20, 13))
    assert len(telegram.direct()) == 3
    clock.now = at(20, 13, 7)
    before = len(telegram.made("editMessageReplyMarkup"))
    eng.on_status({"AlertID": 7, "LogEntry": "Closed by Grafana"})
    resolved = telegram.group()[-1]
    assert resolved["text"] == "✅ " + TEXTS["uk"]["resolved_after"].format(duration="1 год 7 хв")
    assert resolved["message_thread_id"] == 101
    assert len(telegram.made("editMessageReplyMarkup")) == before
    assert telegram.made("editForumTopic") == [
        {"chat_id": GROUP, "message_thread_id": 101, "name": "✅ " + SUMMARY}
    ]
    assert telegram.made("closeForumTopic") == [{"chat_id": GROUP, "message_thread_id": 101}]
    assert telegram.methods()[-2:] == ["editForumTopic", "closeForumTopic"]
    eng.on_status({"AlertID": 7, "LogEntry": "Closed by Grafana"})
    assert len(telegram.made("closeForumTopic")) == 1


def test_a_resolve_without_acknowledgement_takes_the_button_away_in_the_topic() -> None:
    clock = Clock(at(20, 12))
    telegram = ForumTelegram()
    eng = engine(clock, telegram)
    eng.on_alert(payload())
    clock.now = at(20, 12, 0) + 20
    eng.on_status({"AlertID": 7, "LogEntry": "Closed"})
    group_edits = [e for e in telegram.made("editMessageReplyMarkup") if e["chat_id"] == GROUP]
    assert len(group_edits) == 1
    assert telegram.group()[-1]["text"].endswith(TEXTS["uk"]["minutes"].format(m=1))
    assert eng.topics[101].closed_at == clock.now
    assert eng.topic_of(7) is None


def test_an_acknowledgement_in_goalert_names_who_did_it_in_the_topic() -> None:
    clock = Clock(at(20, 12))
    telegram = ForumTelegram()
    eng = engine(clock, telegram, quiet=QUIET, zone=KYIV)
    eng.on_alert(payload())
    eng.on_status({"AlertID": 7, "LogEntry": "Acknowledged by Ops (Web)"})
    assert telegram.group()[-1]["text"] == "✅ " + TEXTS["uk"]["acked_by"].format(
        who="Ops (Web)", time="12:00"
    )
    eng.on_alert(payload(alert_id=8, Summary="Other"))
    eng.on_status({"AlertID": 8, "LogEntry": "Acknowledged"})
    assert "GoAlert" in telegram.group()[-1]["text"]


def test_an_acknowledgement_is_timed_in_the_zone_of_the_bridge_without_quiet_hours() -> None:
    clock = Clock(at(20, 12))
    telegram = ForumTelegram()
    eng = engine(clock, telegram, zone=KYIV)
    eng.on_alert(payload())
    eng.on_status({"AlertID": 7, "LogEntry": "Acknowledged by Ops"})
    assert telegram.group()[-1]["text"].endswith(" 12:00")
    assert bridge.Config(chat_id="1", repeat_seconds=1.0, max_repeats=1, quiet=None).zone == UTC


def test_a_button_in_the_group_without_a_name_and_a_stranger_chat() -> None:
    clock = Clock(at(20, 12))
    telegram = ForumTelegram()
    ack = FakeAck()
    eng = engine(clock, telegram, ack=ack)
    eng.on_alert(payload())
    press(eng, telegram, "-1009999999", {"first_name": "Mallory"})
    assert ack.ids == []
    assert telegram.calls[-1][1]["text"] == TEXTS["uk"]["denied"]
    press(eng, telegram, PERSON, {"username": "olena"})
    assert ack.ids == [7]
    assert "@olena" in telegram.group()[-1]["text"]


def test_the_group_cannot_acknowledge_when_topics_are_off() -> None:
    clock = Clock(at(20, 12))
    telegram = ForumTelegram()
    ack = FakeAck()
    eng = engine(clock, telegram, ack=ack, topics=False)
    eng.on_alert(payload())
    eng.tick()
    assert eng.acknowledge(7, GROUP) == TEXTS["uk"]["denied"]
    assert ack.ids == []
    assert telegram.methods() == ["sendMessage"]
    assert eng.counters.get("topics_available") == 0
    assert "topics_available" not in eng.counters.render()


def test_without_an_ops_chat_topics_stay_off() -> None:
    clock = Clock(at(20, 12))
    telegram = ForumTelegram()
    config = bridge.Config(
        chat_id=PERSON, repeat_seconds=300.0, max_repeats=1, quiet=None, texts=TEXTS, topics=True
    )
    eng = bridge.Engine(config, telegram, FakeAck(), clock)
    eng.on_alert(payload())
    assert telegram.methods() == ["sendMessage"]
    assert eng.acknowledge(7, "") == TEXTS["en"]["denied"]


def test_a_refire_within_six_hours_reopens_the_topic_and_later_one_opens_a_new_one() -> None:
    clock = Clock(at(20, 12))
    telegram = ForumTelegram()
    eng = engine(clock, telegram)
    eng.on_alert(payload())
    eng.on_status({"AlertID": 7, "LogEntry": "Closed"})
    clock.now = at(20, 18)
    eng.on_alert(payload(alert_id=8))
    assert len(telegram.made("createForumTopic")) == 1
    assert telegram.made("reopenForumTopic") == [{"chat_id": GROUP, "message_thread_id": 101}]
    assert telegram.made("editForumTopic")[-1]["name"] == "🔴 " + SUMMARY
    assert telegram.group()[-1]["message_thread_id"] == 101
    assert telegram.group()[-1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "ack:8"
    assert eng.counters.get("topics_total", kind="reopened") == 1
    clock.now = at(20, 18, 30)
    eng.on_status({"AlertID": 8, "LogEntry": "Closed"})
    assert telegram.group()[-1]["text"].endswith(TEXTS["uk"]["minutes"].format(m=30))
    clock.now = at(21, 0, 31)
    eng.on_alert(payload(alert_id=9))
    assert len(telegram.made("createForumTopic")) == 2
    assert len(telegram.made("reopenForumTopic")) == 1
    assert telegram.group()[-1]["message_thread_id"] == 102


def test_another_alert_gets_its_own_topic_even_while_one_is_closed() -> None:
    clock = Clock(at(20, 12))
    telegram = ForumTelegram()
    eng = engine(clock, telegram)
    eng.on_alert(payload())
    eng.on_status({"AlertID": 7, "LogEntry": "Closed"})
    eng.on_alert(payload(alert_id=8, Summary="CPU on central is hot"))
    assert telegram.made("reopenForumTopic") == []
    assert telegram.made("createForumTopic")[-1]["name"] == "🔴 CPU on central is hot"
    eng.on_alert(payload(alert_id=9, Summary="Other", Meta={"fingerprint": "abc"}))
    eng.on_status({"AlertID": 9, "LogEntry": "Closed"})
    eng.on_alert(payload(alert_id=10, Summary="Other again", Meta={"fingerprint": "abc"}))
    assert telegram.made("reopenForumTopic") == [{"chat_id": GROUP, "message_thread_id": 103}]


def test_the_latest_closed_topic_of_a_fingerprint_is_reopened() -> None:
    clock = Clock(at(20, 12))
    telegram = ForumTelegram()
    eng = engine(clock, telegram)
    eng.topics[50] = bridge.Topic(50, "Disk full|" + SUMMARY, SUMMARY, 1, 0.0, closed_at=at(20, 10))
    eng.topics[60] = bridge.Topic(60, "Disk full|" + SUMMARY, SUMMARY, 2, 0.0, closed_at=at(20, 11))
    eng.on_alert(payload())
    assert telegram.made("reopenForumTopic")[0]["message_thread_id"] == 60


def test_a_deleted_topic_that_cannot_reopen_is_replaced_by_a_new_one() -> None:
    clock = Clock(at(20, 12))
    telegram = ForumTelegram()
    eng = engine(clock, telegram)
    eng.on_alert(payload())
    eng.on_status({"AlertID": 7, "LogEntry": "Closed"})
    telegram.broken["reopenForumTopic"] = refused(400)
    eng.on_alert(payload(alert_id=8))
    assert len(telegram.made("createForumTopic")) == 2
    assert 101 not in eng.topics
    assert telegram.group()[-1]["message_thread_id"] == 102
    assert eng.counters.get("failures_total", op="topics") == 1


def test_a_topic_that_cannot_be_renamed_or_closed_still_counts_as_closed() -> None:
    clock = Clock(at(20, 12))
    telegram = ForumTelegram()
    eng = engine(clock, telegram)
    eng.on_alert(payload())
    telegram.broken["editForumTopic"] = OSError("down")
    eng.on_status({"AlertID": 7, "LogEntry": "Resolved"})
    assert eng.counters.get("failures_total", op="topics") == 1
    assert eng.topics[101].closed_at == clock.now
    assert eng.topics[101].message_id == 0


@pytest.mark.parametrize(
    ("setup", "reason"),
    [
        ({"forum": False}, "not-a-forum"),
        ({"rights": False}, "no-rights"),
        ({"status": "member"}, "no-rights"),
    ],
)
def test_without_a_forum_or_the_right_to_manage_topics_only_direct_messages_go_out(
    setup: dict[str, Any], reason: str, capsys: pytest.CaptureFixture[str]
) -> None:
    telegram = ForumTelegram(**setup)
    clock = Clock(at(20, 12))
    eng = engine(clock, telegram)
    eng.tick()
    assert eng.counters.get("topics_available") == 0
    assert "ayeops_oncall_topics_available 0" in eng.counters.render()
    eng.on_alert(payload())
    eng.on_alert(payload(alert_id=8, Summary="Other"))
    assert telegram.made("createForumTopic") == []
    assert telegram.group() == []
    assert len(telegram.direct()) == 2
    assert len(telegram.made("getChat")) == 1
    clock.now += 3600
    eng.tick()
    assert len(telegram.made("getChat")) == 2
    lines = [json.loads(x) for x in capsys.readouterr().out.splitlines()]
    assert lines == [{"event": "oncall-topics-unavailable", "reason": reason}]
    eng.on_status({"AlertID": 7, "LogEntry": "Closed"})
    assert telegram.made("closeForumTopic") == []


@pytest.mark.parametrize("code", [400, 403])
def test_a_group_the_bot_is_not_in_falls_back(
    code: int, capsys: pytest.CaptureFixture[str]
) -> None:
    clock = Clock(at(20, 12))
    telegram = ForumTelegram()
    telegram.broken["getChat"] = refused(code)
    eng = engine(clock, telegram)
    eng.on_alert(payload())
    assert eng.counters.get("topics_available") == 0
    assert eng.counters.get("failures_total", op="telegram") == 0
    assert "no-chat" in capsys.readouterr().out
    assert len(telegram.direct()) == 1


def test_topics_come_back_after_the_rights_are_granted(capsys: pytest.CaptureFixture[str]) -> None:
    clock = Clock(at(20, 12))
    telegram = ForumTelegram(rights=False)
    eng = engine(clock, telegram)
    eng.tick()
    telegram.rights = True
    clock.now += 3599
    eng.tick()
    assert eng.counters.get("topics_available") == 0
    clock.now += 1
    eng.tick()
    assert eng.counters.get("topics_available") == 1
    telegram.rights = False
    clock.now += 3600
    eng.tick()
    out = capsys.readouterr().out.splitlines()
    assert len(out) == 2
    eng.on_alert(payload())
    assert telegram.made("createForumTopic") == []


def test_telegram_down_during_the_check_counts_a_failure_and_tries_again() -> None:
    clock = Clock(at(20, 12))
    telegram = ForumTelegram()
    telegram.broken["getMe"] = OSError("down")
    eng = engine(clock, telegram)
    eng.on_alert(payload())
    assert eng.counters.get("failures_total", op="telegram") == 1
    assert "topics_available" not in eng.counters.render()
    assert telegram.made("createForumTopic") == []
    del telegram.broken["getMe"]
    clock.now += 15
    eng.tick()
    assert eng.counters.get("topics_available") == 1


def test_a_known_good_group_stays_good_while_telegram_is_down_for_the_recheck() -> None:
    clock = Clock(at(20, 12))
    telegram = ForumTelegram()
    eng = engine(clock, telegram)
    eng.tick()
    telegram.broken["getChat"] = OSError("down")
    clock.now += 3600
    eng.tick()
    del telegram.broken["getChat"]
    eng.on_alert(payload())
    assert len(telegram.made("getChat")) == 2
    assert len(telegram.made("createForumTopic")) == 1


def test_a_failed_topic_creation_falls_back_and_checks_the_group_again() -> None:
    clock = Clock(at(20, 12))
    telegram = ForumTelegram()
    telegram.broken["createForumTopic"] = refused(400)
    eng = engine(clock, telegram)
    eng.on_alert(payload())
    assert eng.counters.get("failures_total", op="topics") == 1
    assert len(telegram.direct()) == 1
    assert eng.topics == {}
    del telegram.broken["createForumTopic"]
    eng.on_alert(payload(alert_id=8, Summary="Other"))
    assert len(telegram.made("getChat")) == 2
    assert len(telegram.made("createForumTopic")) == 1


def test_a_topic_answer_without_a_thread_falls_back() -> None:
    clock = Clock(at(20, 12))
    telegram = ForumTelegram()
    original = telegram.__call__

    def odd(method: str, body: Mapping[str, Any]) -> Mapping[str, Any]:
        answer = original(method, body)
        return {"ok": True, "result": {}} if method == "createForumTopic" else answer

    eng = engine(clock, odd)
    eng.on_alert(payload())
    assert eng.topics == {}
    assert eng.counters.get("failures_total", op="topics") == 1


def test_a_failed_post_into_the_topic_is_counted() -> None:
    clock = Clock(at(20, 12))
    telegram = ForumTelegram()
    calls = {"n": 0}

    def flaky(method: str, body: Mapping[str, Any]) -> Mapping[str, Any]:
        if method == "sendMessage" and body["chat_id"] == GROUP:
            calls["n"] += 1
            raise OSError("down")
        return telegram(method, body)

    eng = engine(clock, flaky)
    eng.on_alert(payload())
    assert calls["n"] == 1
    assert eng.counters.get("failures_total", op="telegram") == 1
    assert eng.topics[101].message_id == 0
    assert eng.counters.get("topic_messages_total") == 0
    eng.on_status({"AlertID": 7, "LogEntry": "Acknowledged by Ops"})
    assert telegram.made("editMessageReplyMarkup")[-1]["chat_id"] == PERSON


def test_a_button_that_cannot_be_removed_in_the_topic_is_counted() -> None:
    clock = Clock(at(20, 12))
    telegram = ForumTelegram()
    eng = engine(clock, telegram)
    eng.on_alert(payload())
    telegram.broken["editMessageReplyMarkup"] = OSError("down")
    eng.on_status({"AlertID": 7, "LogEntry": "Acknowledged by Ops"})
    assert eng.counters.get("failures_total", op="telegram") == 2
    assert eng.topics[101].message_id == 0
    assert "Ops" in telegram.group()[-1]["text"]


def test_a_night_alert_posts_silently_in_its_topic() -> None:
    clock = Clock(at(20, 23, 30))
    telegram = ForumTelegram()
    eng = engine(clock, telegram, quiet=QUIET)
    eng.on_alert(payload())
    (first,) = telegram.group()
    assert first["disable_notification"] is True
    assert "🌙" not in first["text"]
    assert telegram.direct()[0]["disable_notification"] is True


def test_the_diagnosis_arrives_in_the_topic_once() -> None:
    clock = Clock(at(20, 12))
    telegram = ForumTelegram()
    eng = engine(clock, telegram)
    eng.on_alert(payload())
    found = {"diagnosis": "disk <full>", "diagnosis_url": "https://autopilot/x"}
    eng.on_alert(payload(Meta=found))
    note = telegram.group()[-1]
    assert note["message_thread_id"] == 101
    assert note["text"] == (
        '🤖 Діагноз: disk &lt;full&gt;\n<a href="https://autopilot/x">Діагноз</a>'
    )
    eng.on_alert(payload(Meta=found))
    eng.on_alert(payload())
    assert len(telegram.group()) == 2
    eng.on_alert(payload(Meta={"diagnosis": "second look"}))
    assert telegram.group()[-1]["text"] == "🤖 Діагноз: second look"
    assert len(telegram.direct()) == 1


def test_a_long_summary_is_cut_to_the_topic_name_limit() -> None:
    clock = Clock(at(20, 12))
    telegram = ForumTelegram()
    eng = engine(clock, telegram)
    eng.on_alert(payload(Summary="x" * 200))
    assert telegram.made("createForumTopic")[0]["name"] == "🔴 " + "x" * 126


def test_topics_survive_a_restart_and_old_closed_ones_are_forgotten(tmp_path: Path) -> None:
    clock = Clock(at(20, 12))
    state = tmp_path / "alerts.json"
    telegram = ForumTelegram()
    eng = engine(clock, telegram, state=state)
    eng.on_alert(payload())
    eng.on_status({"AlertID": 7, "LogEntry": "Acknowledged by Ops"})
    eng.on_alert(payload(alert_id=8, Summary="Other"))
    eng.on_status({"AlertID": 8, "LogEntry": "Closed"})
    clock.now = at(20, 12, 45)
    telegram = ForumTelegram()
    again = engine(clock, telegram, state=state)
    assert sorted(again.topics) == [101, 102]
    again.on_status({"AlertID": 7, "LogEntry": "Closed"})
    assert telegram.made("closeForumTopic") == [{"chat_id": GROUP, "message_thread_id": 101}]
    assert telegram.group()[-1]["text"].endswith("45 хв")
    again.on_alert(payload(alert_id=9, Summary="Other"))
    assert telegram.made("reopenForumTopic") == [{"chat_id": GROUP, "message_thread_id": 102}]
    clock.now = at(21, 12)
    again.save()
    saved = json.loads(state.read_text("utf-8"))
    assert [t["thread_id"] for t in saved["topics"]] == [102]
    assert sorted(again.topics) == [102]


def test_a_closed_topic_is_kept_up_to_exactly_the_reopen_window() -> None:
    clock = Clock(at(20, 12))
    telegram = ForumTelegram()
    eng = engine(clock, telegram)
    eng.on_alert(payload())
    eng.on_status({"AlertID": 7, "LogEntry": "Closed"})
    clock.now += 6 * 3600
    eng.save()
    assert 101 in eng.topics
    eng.on_alert(payload(alert_id=8))
    assert len(telegram.made("reopenForumTopic")) == 1


def test_names_of_people_and_durations() -> None:
    assert bridge.person({"first_name": "Olena"}) == "Olena"
    assert bridge.person({"username": "olena", "last_name": "K"}) == "K"
    assert bridge.person({"username": "olena"}) == "@olena"
    assert bridge.person({}) == ""
    assert bridge.person(None) == ""
    eng = engine(Clock(at(20, 12)), ForumTelegram())
    assert eng.duration(10) == "1 хв"
    assert eng.duration(59 * 60 + 29) == "59 хв"
    assert eng.duration(3600) == "1 год 0 хв"
    assert eng.duration(2 * 3600 + 31 * 60) == "2 год 31 хв"
    assert bridge.result(None) == {}
    assert bridge.result({"result": [1]}) == {}


def test_settings_read_the_ops_chat_and_topics(tmp_path: Path) -> None:
    env = {
        "AYE_ONCALL_TELEGRAM_TOKEN": "1:x",
        "AYE_ONCALL_WEBHOOK_SECRET": "s",
        "AYE_ONCALL_CHAT_ID": PERSON,
        "AYE_ONCALL_STATE": str(tmp_path),
        "AYE_ONCALL_TEXTS": str(FILES / "oncall_texts.json"),
    }
    parsed = bridge.settings_from(env)
    assert (parsed.config.ops_chat_id, parsed.config.topics) == ("", False)
    assert parsed.config.reopen_seconds == 6 * 3600
    assert parsed.config.zone == UTC
    assert bridge.settings_from({**env, "AYE_ONCALL_TIMEZONE": "Europe/Kyiv"}).config.zone == KYIV
    parsed = bridge.settings_from(
        {**env, "AYE_ONCALL_OPS_CHAT_ID": GROUP, "AYE_ONCALL_TOPICS": "True"}
    )
    assert (parsed.config.ops_chat_id, parsed.config.topics) == (GROUP, True)
    assert bridge.settings_from({**env, "AYE_ONCALL_TOPICS": "yes"}).config.topics is False


def test_a_first_message_that_reaches_the_person_late_is_no_repeat_in_the_topic() -> None:
    clock = Clock(at(20, 12))
    telegram = ForumTelegram()
    down = {"person": True}

    def person_down(method: str, body: Mapping[str, Any]) -> Mapping[str, Any]:
        if method == "sendMessage" and body["chat_id"] == PERSON and down["person"]:
            raise OSError("down")
        return telegram(method, body)

    eng = engine(clock, person_down)
    eng.on_alert(payload())
    down["person"] = False
    advance(clock, eng, at(20, 12, 5))
    assert len(telegram.direct()) == 1
    assert len(telegram.group()) == 1
    advance(clock, eng, at(20, 12, 10))
    assert telegram.group()[-1]["text"] == "🔁 " + TEXTS["uk"]["repeat"].format(n=1)


def test_a_press_without_a_name_and_an_odd_message_id() -> None:
    clock = Clock(at(20, 12))
    telegram = ForumTelegram()

    def odd(method: str, body: Mapping[str, Any]) -> Mapping[str, Any]:
        answer = telegram(method, body)
        if method == "sendMessage" and body["chat_id"] == GROUP:
            return {"ok": True, "result": {"message_id": "7"}}
        return answer

    eng = engine(clock, odd)
    eng.on_alert(payload())
    assert eng.topics[101].message_id == 0
    press(eng, telegram, GROUP, {})
    assert telegram.group()[-1]["text"] == "✅ " + TEXTS["uk"]["acked_by"].format(
        who="?", time="09:00"
    )


def goalert_alert(body: bytes, alert_id: int) -> dict[str, Any]:
    lead = json.loads(bridge.enrich(body))["alerts"][0]
    rows = {**lead["labels"], **lead["annotations"]}
    return payload(alert_id=alert_id, Summary=lead["annotations"]["summary"], rows=rows)


def test_a_notification_group_keeps_one_topic_while_its_hosts_come_and_go() -> None:
    clock = Clock(at(20, 12))
    telegram = ForumTelegram()
    eng = engine(clock, telegram)
    one = grouped([grafana_alert("worker-01")])
    fingerprint = json.loads(bridge.enrich(one))["alerts"][0]["fingerprint"]
    eng.on_group(one)
    assert telegram.calls == []
    eng.on_alert(goalert_alert(one, 7))
    assert eng.topics[101].fingerprint == fingerprint
    assert telegram.made("createForumTopic")[0]["name"] == (
        "🔴 worker-01 stopped reporting node metrics"
    )
    eng.on_group(one)
    assert len(telegram.group()) == 1
    two = grouped([grafana_alert("worker-01"), grafana_alert("worker-02")])
    eng.on_group(two)
    eng.on_group(two)
    note = telegram.group()[-1]
    assert note["text"] == "🔴 " + TEXTS["uk"]["now"].format(
        summary="2 x Host down in workers: worker-01, worker-02"
    )
    assert note["message_thread_id"] == 101
    assert note["disable_notification"] is True
    assert "reply_markup" not in note
    assert len(telegram.group()) == 2
    eng.on_group(grouped([grafana_alert("worker-01"), grafana_alert("worker-02", "resolved")]))
    assert telegram.group()[-1]["text"].endswith(": worker-01 stopped reporting node metrics")
    eng.on_group(grouped([grafana_alert("db-1"), grafana_alert("db-2")], key="other"))
    eng.on_group(grouped([grafana_alert("worker-01", "resolved")]))
    assert len(telegram.group()) == 3
    assert len(telegram.direct()) == 1
    assert len(telegram.made("createForumTopic")) == 1
    eng.on_status({"AlertID": 7, "LogEntry": "Closed"})
    eng.on_group(two)
    assert len(telegram.group()) == 4
    clock.now = at(20, 15)
    again = grouped([grafana_alert("worker-03"), grafana_alert("worker-04")])
    eng.on_alert(goalert_alert(again, 8))
    assert len(telegram.made("createForumTopic")) == 1
    assert telegram.made("reopenForumTopic") == [{"chat_id": GROUP, "message_thread_id": 101}]
    assert telegram.made("editForumTopic")[-1]["name"] == (
        "🔴 2 x Host down in workers: worker-03, worker-04"
    )
    eng.on_group(again)
    assert len(telegram.group()) == 5


def test_a_group_notification_without_a_summary_or_a_body_posts_nothing() -> None:
    clock = Clock(at(20, 12))
    telegram = ForumTelegram()
    eng = engine(clock, telegram)
    one = grouped([grafana_alert("worker-01")])
    eng.on_alert(goalert_alert(one, 7))
    bare = grafana_alert("worker-01")
    del bare["annotations"]["summary"]
    for body in (grouped([bare]), b"not json", b'{"alerts": []}', b'{"alerts": [1]}', b"[]"):
        eng.on_group(body)
    assert len(telegram.group()) == 1
