from __future__ import annotations

import hashlib
import hmac
import html
import importlib.util
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC as TZ_UTC
from datetime import datetime, timedelta, tzinfo
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

HHMM = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
ROW = re.compile(r"^\|\s*([A-Za-z0-9_.-]+)\s*\|\s*(.*?)\s*\|\s*$")
HEADERS = frozenset({"Label", "Annotation"})
LABEL_HEADER = re.compile(r"^\|\s*Label\s*\|")
ANNOTATION_HEADER = re.compile(r"^\|\s*Annotation\s*\|")
ACK_PREFIX = "ack:"
ACK_QUERY = (
    "mutation ($ids: [Int!]!) "
    "{ updateAlerts(input: {alertIDs: $ids, newStatus: StatusAcknowledged}) { id } }"
)
READ_QUERY = (
    "query ($input: AlertSearchOptions) { alerts(input: $input) { nodes "
    "{ alertID status summary details createdAt "
    "recentEvents(input: {limit: 100}) { nodes { timestamp message } } } "
    "pageInfo { endCursor hasNextPage } } }"
)
ACK_LINE = re.compile(r"^Acknowledged(?: (by|via) (.+?))?(?: \(([^()]*)\))?$")
READ_PAGE = 100
READ_PAGES = 20
ACKS_SPAN = 8 * 86400.0
PRESS_KEEP = 35 * 86400.0
PAGE = "page"
OPEN = "open"
ACKED = "acked"
CLOSED = "closed"
PREFIX = "ayeops_oncall_"
POLL_TIMEOUT = 25
TICK = 15
TOPICS_RECHECK = 3600
TOPIC_NAME_LIMIT = 128
FIRING = "🔴 "
RESOLVED = "✅ "


def time_zone(key: str) -> tzinfo:
    try:
        return ZoneInfo(key)
    except ZoneInfoNotFoundError:
        if key == "UTC":
            return TZ_UTC
        raise


UTC: tzinfo = time_zone("UTC")

TEXTS_FILE = Path(os.environ.get("AYE_ONCALL_TEXTS", "/app/oncall_texts.json"))


def load_texts(path: Path = TEXTS_FILE) -> dict[str, dict[str, str]]:
    data = json.loads(path.read_text("utf-8"))
    return {str(lang): {str(k): str(v) for k, v in table.items()} for lang, table in data.items()}


def parse_hhmm(text: str) -> tuple[int, int]:
    match = HHMM.match(text.strip())
    if match is None:
        raise ValueError(f"not an HH:MM time: {text!r}")
    return int(match.group(1)), int(match.group(2))


@dataclass(frozen=True)
class Quiet:
    start: tuple[int, int]
    end: tuple[int, int]
    zone: tzinfo

    def active(self, now: float) -> bool:
        local = datetime.fromtimestamp(now, self.zone)
        minute = local.hour * 60 + local.minute
        start = self.start[0] * 60 + self.start[1]
        end = self.end[0] * 60 + self.end[1]
        if start == end:
            return False
        if start < end:
            return start <= minute < end
        return minute >= start or minute < end

    def ends_after(self, now: float) -> float:
        local = datetime.fromtimestamp(now, self.zone)
        day = local.date()
        for offset in range(3):
            date = day + timedelta(days=offset)
            candidate = datetime(
                date.year, date.month, date.day, self.end[0], self.end[1], tzinfo=self.zone
            ).timestamp()
            if candidate > now:
                return candidate
        raise ValueError("quiet hours never end")

    def end_text(self) -> str:
        return f"{self.end[0]:02d}:{self.end[1]:02d}"


def details_fields(details: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in details.splitlines():
        match = ROW.match(line)
        if match is None:
            continue
        key, value = match.group(1), match.group(2)
        if key in HEADERS or set(key) <= {"-"}:
            continue
        fields.setdefault(key, value.replace("\\|", "|").replace("<br />", "\n"))
    return fields


def label_rows(details: str) -> dict[str, str]:
    labels: dict[str, str] = {}
    inside = False
    for line in details.splitlines():
        if LABEL_HEADER.match(line):
            inside = True
            continue
        if ANNOTATION_HEADER.match(line) or not line.startswith("|"):
            inside = False
            continue
        match = ROW.match(line) if inside else None
        if match is None or set(match.group(1)) <= {"-"}:
            continue
        labels.setdefault(match.group(1), match.group(2).replace("\\|", "|"))
    return labels


def parse_stamp(text: Any) -> float | None:
    try:
        moment = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=TZ_UTC)
    return moment.timestamp()


def iso_utc(moment: float) -> str:
    return datetime.fromtimestamp(moment, TZ_UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def first_ack(events: Any) -> tuple[float, str, str] | None:
    found: tuple[float, str, str] | None = None
    for event in events if isinstance(events, list) else []:
        if not isinstance(event, Mapping):
            continue
        match = ACK_LINE.match(str(event.get("message") or "").strip())
        moment = parse_stamp(event.get("timestamp"))
        if match is None or moment is None:
            continue
        kind, name, classifier = match.groups()
        name = "" if name is None or name == "[unknown]" else name.strip()
        via = (classifier or "").split(",")[0].strip().lower()
        if kind == "via":
            via, name = via or name.lower(), ""
        if found is None or moment < found[0]:
            found = (moment, name, via)
    return found


def link(value: Any) -> str:
    text = str(value or "").strip()
    return text if text.startswith(("https://", "http://")) else ""


@dataclass
class Alert:
    alert_id: int
    summary: str
    severity: str
    dashboard: str = ""
    runbook: str = ""
    diagnosis: str = ""
    diagnosis_url: str = ""
    goalert_url: str = ""
    sent: int = 0
    next_at: float = 0.0
    state: str = OPEN
    messages: list[int] = field(default_factory=list)
    fingerprint: str = ""


@dataclass
class Topic:
    thread_id: int
    fingerprint: str
    summary: str
    alert_id: int
    opened_at: float
    message_id: int = 0
    closed_at: float = 0.0
    latest: str = ""


def result(answer: Any) -> Mapping[str, Any]:
    found = answer.get("result") if isinstance(answer, Mapping) else None
    return found if isinstance(found, Mapping) else {}


def person(user: Any) -> str:
    if not isinstance(user, Mapping):
        return ""
    name = " ".join(str(user.get(k) or "") for k in ("first_name", "last_name")).strip()
    if name:
        return name
    return "@" + str(user["username"]) if user.get("username") else ""


def topic_name(mark: str, summary: str) -> str:
    return (mark + summary)[:TOPIC_NAME_LIMIT]


def alert_from(payload: Mapping[str, Any]) -> Alert:
    fields = details_fields(str(payload.get("Details") or ""))
    meta = payload.get("Meta") if isinstance(payload.get("Meta"), Mapping) else {}
    assert isinstance(meta, Mapping)

    def pick(name: str) -> str:
        return str(meta.get(name) or fields.get(name) or "").strip()

    summary = str(payload.get("Summary") or fields.get("alertname") or "alert")
    return Alert(
        alert_id=int(payload["AlertID"]),
        summary=summary,
        severity=pick("severity").lower() or "critical",
        dashboard=link(pick("dashboard_url")),
        runbook=link(pick("runbook_url")),
        diagnosis=pick("diagnosis"),
        diagnosis_url=link(pick("diagnosis_url")),
        goalert_url=link(payload.get("GoAlertURL")),
        fingerprint=pick("fingerprint") or f"{fields.get('alertname', '')}|{summary}",
    )


@dataclass(frozen=True)
class Config:
    chat_id: str
    repeat_seconds: float
    max_repeats: int
    quiet: Quiet | None
    language: str = "en"
    texts: Mapping[str, Mapping[str, str]] = field(default_factory=dict)
    ops_chat_id: str = ""
    topics: bool = False
    reopen_seconds: float = 6 * 3600.0
    zone: tzinfo = UTC


Post = Callable[[str, Mapping[str, Any]], Mapping[str, Any]]
Ack = Callable[[int], bool]
Clock = Callable[[], float]
Reader = Callable[[float, float], list[Mapping[str, Any]]]


class NotReady(RuntimeError):
    pass


class Conflict(RuntimeError):
    pass


class Counters:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.values: dict[tuple[str, tuple[tuple[str, str], ...]], float] = {}

    def add(self, name: str, amount: float = 1.0, **labels: str) -> None:
        key = (name, tuple(sorted(labels.items())))
        with self.lock:
            self.values[key] = self.values.get(key, 0.0) + amount

    def set(self, name: str, value: float, **labels: str) -> None:
        with self.lock:
            self.values[(name, tuple(sorted(labels.items())))] = value

    def get(self, name: str, **labels: str) -> float:
        with self.lock:
            return self.values.get((name, tuple(sorted(labels.items()))), 0.0)

    def render(self) -> str:
        with self.lock:
            items = sorted(self.values.items())
        lines = []
        for (name, labels), value in items:
            inner = ",".join(f'{k}="{v}"' for k, v in labels)
            number = f"{value:.15g}"
            lines.append(
                f"{PREFIX}{name}{{{inner}}} {number}" if inner else f"{PREFIX}{name} {number}"
            )
        return "\n".join(lines) + "\n"


class Engine:
    def __init__(
        self,
        config: Config,
        telegram: Post,
        ack: Ack,
        clock: Clock,
        state: Path | None = None,
        counters: Counters | None = None,
    ) -> None:
        self.config = config
        self.telegram = telegram
        self.ack = ack
        self.clock = clock
        self.state = state
        self.counters = counters or Counters()
        self.lock = threading.RLock()
        self.alerts: dict[int, Alert] = {}
        self.topics: dict[int, Topic] = {}
        self.presses: dict[int, dict[str, Any]] = {}
        self.topics_ok: bool | None = None
        self.topics_checked = 0.0
        self.topics_reported = False
        self.load()

    def text(self, key: str, **values: Any) -> str:
        texts = self.config.texts
        table = texts.get(self.config.language) or texts.get("en") or {}
        return table.get(key, key).format(**values)

    def load(self) -> None:
        if self.state is None or not self.state.exists():
            return
        try:
            raw = json.loads(self.state.read_text("utf-8"))
        except (OSError, ValueError):
            return
        for item in raw.get("alerts", []):
            alert = Alert(**item)
            self.alerts[alert.alert_id] = alert
        for item in raw.get("topics", []):
            topic = Topic(**item)
            self.topics[topic.thread_id] = topic
        for item in raw.get("presses", []):
            self.presses[int(item["alert_id"])] = {"by": str(item["by"]), "at": float(item["at"])}

    def save(self) -> None:
        now = self.clock()
        for topic in list(self.topics.values()):
            if topic.closed_at and now - topic.closed_at > self.config.reopen_seconds:
                del self.topics[topic.thread_id]
        for alert_id, press in list(self.presses.items()):
            if now - press["at"] > PRESS_KEEP:
                del self.presses[alert_id]
        if self.state is None:
            return
        live = [asdict(a) for a in self.alerts.values() if a.state == OPEN]
        topics = [asdict(t) for t in self.topics.values()]
        presses = [{"alert_id": k, **v} for k, v in sorted(self.presses.items())]
        temporary = self.state.with_suffix(".tmp")
        data = {"alerts": live, "topics": topics, "presses": presses}
        temporary.write_text(json.dumps(data), "utf-8")
        temporary.replace(self.state)
        self.counters.set("open_alerts", float(len(live)))

    def quiet_now(self, alert: Alert, now: float) -> bool:
        quiet = self.config.quiet
        return quiet is not None and alert.severity != PAGE and quiet.active(now)

    def message(self, alert: Alert, repeat: bool, quiet: bool) -> str:
        head = self.text("page") if alert.severity == PAGE else self.text("alert")
        lines = [f"🚨 <b>{html.escape(head)}: {html.escape(alert.summary)}</b>"]
        if repeat:
            lines.append(html.escape(self.text("repeat", n=alert.sent)))
        if quiet and self.config.quiet is not None:
            lines.append("🌙 " + html.escape(self.text("quiet", end=self.config.quiet.end_text())))
        if alert.diagnosis:
            lines.append(
                f"🤖 {html.escape(self.text('diagnosis'))}: {html.escape(alert.diagnosis)}"
            )
        links = [
            (self.text("dashboard"), alert.dashboard),
            (self.text("runbook"), alert.runbook),
            (self.text("diagnosis"), alert.diagnosis_url),
            (self.text("goalert"), alert.goalert_url),
        ]
        shown = [
            f'<a href="{html.escape(url)}">{html.escape(name)}</a>' for name, url in links if url
        ]
        if shown:
            lines.append(" · ".join(shown))
        return "\n".join(lines)

    def keyboard(self, alert_id: int) -> dict[str, Any]:
        button = {"text": "✅ " + self.text("ack"), "callback_data": f"{ACK_PREFIX}{alert_id}"}
        return {"inline_keyboard": [[button]]}

    def send(self, alert: Alert, repeat: bool, quiet: bool) -> bool:
        payload = {
            "chat_id": self.config.chat_id,
            "text": self.message(alert, repeat, quiet),
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
            "disable_notification": quiet,
            "reply_markup": self.keyboard(alert.alert_id),
        }
        try:
            answer = self.telegram("sendMessage", payload)
        except Exception:
            self.counters.add("failures_total", op="telegram")
            return False
        message = answer.get("result") if isinstance(answer, Mapping) else None
        if isinstance(message, Mapping) and isinstance(message.get("message_id"), int):
            alert.messages.append(int(message["message_id"]))
        self.counters.add(
            "messages_total", kind="repeat" if repeat else "alert", quiet=str(quiet).lower()
        )
        return True

    def topics_problem(self) -> str:
        chat_id = self.config.ops_chat_id
        try:
            if not result(self.telegram("getChat", {"chat_id": chat_id})).get("is_forum"):
                return "not-a-forum"
            me = result(self.telegram("getMe", {}))
            member = result(
                self.telegram("getChatMember", {"chat_id": chat_id, "user_id": me.get("id")})
            )
        except urllib.error.HTTPError as error:
            if error.code in (400, 403):
                return "no-chat"
            raise
        if member.get("status") != "administrator" or member.get("can_manage_topics") is not True:
            return "no-rights"
        return ""

    def topics_ready(self) -> bool:
        if not (self.config.topics and self.config.ops_chat_id):
            return False
        now = self.clock()
        if self.topics_ok is not None and now - self.topics_checked < TOPICS_RECHECK:
            return self.topics_ok
        try:
            problem = self.topics_problem()
        except Exception:
            self.counters.add("failures_total", op="telegram")
            return bool(self.topics_ok)
        self.topics_checked = now
        self.topics_ok = not problem
        self.counters.set("topics_available", 1.0 if self.topics_ok else 0.0)
        if problem and not self.topics_reported:
            print(json.dumps({"event": "oncall-topics-unavailable", "reason": problem}), flush=True)
        self.topics_reported = bool(problem)
        return self.topics_ok

    def topic_of(self, alert_id: int) -> Topic | None:
        for topic in self.topics.values():
            if topic.alert_id == alert_id and not topic.closed_at:
                return topic
        return None

    def reusable(self, fingerprint: str, now: float) -> Topic | None:
        found = [
            t
            for t in self.topics.values()
            if t.closed_at
            and t.fingerprint == fingerprint
            and now - t.closed_at <= self.config.reopen_seconds
        ]
        return max(found, key=lambda t: t.closed_at, default=None)

    def post_topic(self, topic: Topic, text: str, silent: bool = True, button: int = 0) -> int:
        payload: dict[str, Any] = {
            "chat_id": self.config.ops_chat_id,
            "message_thread_id": topic.thread_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
            "disable_notification": silent,
        }
        if button:
            payload["reply_markup"] = self.keyboard(button)
        try:
            answer = self.telegram("sendMessage", payload)
        except Exception:
            self.counters.add("failures_total", op="telegram")
            return 0
        self.counters.add("topic_messages_total")
        message_id = result(answer).get("message_id")
        return message_id if isinstance(message_id, int) else 0

    def note(self, alert_id: int, text: str) -> None:
        topic = self.topic_of(alert_id)
        if topic is not None:
            self.post_topic(topic, text)

    def open_topic(self, alert: Alert, quiet: bool) -> None:
        if not self.topics_ready():
            return
        now = self.clock()
        chat_id = self.config.ops_chat_id
        name = topic_name(FIRING, alert.summary)
        old = self.reusable(alert.fingerprint, now)
        if old is not None:
            try:
                thread = {"chat_id": chat_id, "message_thread_id": old.thread_id}
                self.telegram("reopenForumTopic", thread)
                self.telegram("editForumTopic", {**thread, "name": name})
            except Exception:
                self.counters.add("failures_total", op="topics")
                del self.topics[old.thread_id]
                old = None
        if old is None:
            try:
                created = result(
                    self.telegram("createForumTopic", {"chat_id": chat_id, "name": name})
                )
                thread_id = int(created["message_thread_id"])
            except Exception:
                self.counters.add("failures_total", op="topics")
                self.topics_ok = None
                return
        else:
            thread_id = old.thread_id
        topic = Topic(thread_id, alert.fingerprint, alert.summary, alert.alert_id, now)
        self.topics[thread_id] = topic
        text = self.message(alert, repeat=False, quiet=False)
        topic.message_id = self.post_topic(topic, text, silent=quiet, button=alert.alert_id)
        self.counters.add("topics_total", kind="created" if old is None else "reopened")

    def drop_button(self, chat_id: str, message_id: int) -> None:
        self.telegram(
            "editMessageReplyMarkup",
            {"chat_id": chat_id, "message_id": message_id, "reply_markup": {"inline_keyboard": []}},
        )

    def ack_topic(self, alert: Alert, who: str) -> None:
        topic = self.topic_of(alert.alert_id)
        if topic is None:
            return
        if topic.message_id:
            try:
                self.drop_button(self.config.ops_chat_id, topic.message_id)
            except Exception:
                self.counters.add("failures_total", op="telegram")
            topic.message_id = 0
        stamp = datetime.fromtimestamp(self.clock(), self.config.zone).strftime("%H:%M")
        line = self.text("acked_by", who=who or "?", time=stamp)
        self.post_topic(topic, "✅ " + html.escape(line))

    def duration(self, seconds: float) -> str:
        hours, minutes = divmod(max(1, round(seconds / 60)), 60)
        if hours:
            return self.text("hours", h=hours, m=minutes)
        return self.text("minutes", m=minutes)

    def close_topic(self, alert_id: int) -> None:
        topic = self.topic_of(alert_id)
        if topic is None:
            return
        now = self.clock()
        chat_id = self.config.ops_chat_id
        line = self.text("resolved_after", duration=self.duration(now - topic.opened_at))
        self.post_topic(topic, "✅ " + html.escape(line))
        thread = {"chat_id": chat_id, "message_thread_id": topic.thread_id}
        try:
            if topic.message_id:
                self.drop_button(chat_id, topic.message_id)
            self.telegram("editForumTopic", {**thread, "name": topic_name(RESOLVED, topic.summary)})
            self.telegram("closeForumTopic", thread)
        except Exception:
            self.counters.add("failures_total", op="topics")
        topic.message_id = 0
        topic.closed_at = now

    def diagnosis_note(self, alert: Alert) -> str:
        name = html.escape(self.text("diagnosis"))
        line = f"🤖 {name}: {html.escape(alert.diagnosis)}"
        if alert.diagnosis_url:
            line += f'\n<a href="{html.escape(alert.diagnosis_url)}">{name}</a>'
        return line

    def on_group(self, body: bytes) -> None:
        payload = fold(body)
        leads = [a for a in (payload or {}).get("alerts") or [] if isinstance(a, dict)]
        if not leads or leads[0].get("status") != "firing":
            return
        lead = leads[0]
        summary = str((lead.get("annotations") or {}).get("summary") or "").strip()
        fingerprint = str(lead.get("fingerprint") or "")
        with self.lock:
            for topic in list(self.topics.values()):
                if topic.closed_at or topic.fingerprint != fingerprint:
                    continue
                if not summary or summary == (topic.latest or topic.summary):
                    continue
                topic.latest = summary
                self.post_topic(topic, "🔴 " + html.escape(self.text("now", summary=summary)))
                self.save()

    def on_alert(self, payload: Mapping[str, Any]) -> None:
        incoming = alert_from(payload)
        now = self.clock()
        with self.lock:
            known = self.alerts.get(incoming.alert_id)
            if known is not None and known.state == OPEN:
                if incoming.diagnosis and incoming.diagnosis != known.diagnosis:
                    self.note(known.alert_id, self.diagnosis_note(incoming))
                for name in ("dashboard", "runbook", "diagnosis", "diagnosis_url", "goalert_url"):
                    value = getattr(incoming, name)
                    if value:
                        setattr(known, name, value)
                self.save()
                return
            quiet = self.quiet_now(incoming, now)
            if self.send(incoming, repeat=False, quiet=quiet):
                incoming.sent = 1
            incoming.next_at = now + self.config.repeat_seconds
            self.alerts[incoming.alert_id] = incoming
            self.open_topic(incoming, quiet)
            self.save()

    def finish(self, alert: Alert, state: str, who: str = "") -> None:
        alert.state = state
        if state == ACKED:
            self.ack_topic(alert, who)
        label = self.text("acked") if state == ACKED else self.text("closed")
        for message_id in alert.messages[-1:]:
            try:
                self.telegram(
                    "editMessageReplyMarkup",
                    {
                        "chat_id": self.config.chat_id,
                        "message_id": message_id,
                        "reply_markup": {"inline_keyboard": []},
                    },
                )
                self.telegram(
                    "sendMessage",
                    {
                        "chat_id": self.config.chat_id,
                        "text": f"✅ {html.escape(label)}: {html.escape(alert.summary)}",
                        "parse_mode": "HTML",
                        "disable_notification": True,
                        "reply_to_message_id": message_id,
                    },
                )
            except Exception:
                self.counters.add("failures_total", op="telegram")
        self.counters.add("finished_total", state=state)

    def on_status(self, payload: Mapping[str, Any]) -> None:
        entry = str(payload.get("LogEntry") or "").lower()
        if "acknowledged" in entry:
            state = ACKED
        elif "closed" in entry or "resolved" in entry:
            state = CLOSED
        else:
            return
        alert_id = int(payload.get("AlertID") or 0)
        with self.lock:
            if state == CLOSED:
                self.close_topic(alert_id)
            alert = self.alerts.get(alert_id)
            if alert is None or alert.state != OPEN:
                self.save()
                return
            raw = str(payload.get("LogEntry") or "")
            who = raw.split(" by ", 1)[1].strip() if " by " in raw else "GoAlert"
            self.finish(alert, state, who)
            self.save()

    def allowed(self, chat_id: str) -> bool:
        if hmac.compare_digest(str(chat_id), str(self.config.chat_id)):
            return True
        group = self.config.ops_chat_id
        return self.config.topics and bool(group) and hmac.compare_digest(str(chat_id), group)

    def acknowledge(self, alert_id: int, chat_id: str, who: str = "") -> str:
        if not self.allowed(chat_id):
            return self.text("denied")
        try:
            ok = self.ack(alert_id)
        except Exception:
            ok = False
        if not ok:
            self.counters.add("failures_total", op="goalert")
            return self.text("failed")
        self.counters.add("acks_total")
        with self.lock:
            self.presses[alert_id] = {"by": who, "at": self.clock()}
            alert = self.alerts.get(alert_id)
            if alert is not None and alert.state == OPEN:
                self.finish(alert, ACKED, who)
            self.save()
        return self.text("acked")

    def acks(self, nodes: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
        found = []
        for node in nodes:
            created = parse_stamp(node.get("createdAt"))
            try:
                alert_id = int(node["alertID"])
            except (KeyError, TypeError, ValueError):
                continue
            if created is None:
                continue
            events = (node.get("recentEvents") or {}).get("nodes")
            ack = first_ack(events)
            entry: dict[str, Any] = {
                "alert_id": alert_id,
                "status": str(node.get("status") or ""),
                "summary": str(node.get("summary") or ""),
                "created_at": created,
                "labels": label_rows(str(node.get("details") or "")),
                "acknowledged_at": None,
                "acknowledged_by": None,
                "acknowledged_via": None,
            }
            if ack is not None:
                moment, name, via = ack
                with self.lock:
                    press = self.presses.get(alert_id)
                if not name and not via:
                    via = "telegram" if press is not None else "api-key"
                    name = str(press["by"]) if press is not None else ""
                entry.update(
                    acknowledged_at=moment, acknowledged_by=name or None, acknowledged_via=via
                )
            found.append(entry)
        return found

    def tick(self) -> None:
        now = self.clock()
        with self.lock:
            self.topics_ready()
            for alert in list(self.alerts.values()):
                if alert.state != OPEN or now < alert.next_at:
                    continue
                if alert.sent > self.config.max_repeats:
                    continue
                if self.quiet_now(alert, now):
                    assert self.config.quiet is not None
                    alert.next_at = self.config.quiet.ends_after(now)
                    continue
                if self.send(alert, repeat=alert.sent > 0, quiet=False):
                    if alert.sent > 0:
                        self.note(
                            alert.alert_id, "🔁 " + html.escape(self.text("repeat", n=alert.sent))
                        )
                    alert.sent += 1
                alert.next_at = now + self.config.repeat_seconds
            self.save()


def telegram_poster(
    token: str, api: str = "https://api.telegram.org", timeout: float = 35.0
) -> Post:
    base = f"{api.rstrip('/')}/bot{token}/"

    def post(method: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        request = urllib.request.Request(
            base + method,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            answer = json.load(response)
        if not isinstance(answer, Mapping) or not answer.get("ok"):
            raise RuntimeError(f"telegram {method} failed")
        return answer

    return post


def goalert_ack(base: str, token: Callable[[], str]) -> Ack:
    def ack(alert_id: int) -> bool:
        request = urllib.request.Request(
            base.rstrip("/") + "/api/graphql",
            data=json.dumps({"query": ACK_QUERY, "variables": {"ids": [alert_id]}}).encode(),
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + token()},
        )
        with urllib.request.urlopen(request, timeout=15) as response:
            answer = json.load(response)
        return isinstance(answer, Mapping) and not answer.get("errors")

    return ack


def goalert_reader(
    base: str, token: Callable[[], str], opener: Callable[..., Any] = urllib.request.urlopen
) -> Reader:
    def read(start: float, end: float) -> list[Mapping[str, Any]]:
        secret = token()
        if not secret:
            raise NotReady("the alert log key is not created yet")
        nodes: list[Mapping[str, Any]] = []
        after = ""
        for _ in range(READ_PAGES):
            search = {
                "notCreatedBefore": iso_utc(start),
                "createdBefore": iso_utc(end),
                "first": READ_PAGE,
                "after": after,
            }
            request = urllib.request.Request(
                base.rstrip("/") + "/api/graphql",
                data=json.dumps({"query": READ_QUERY, "variables": {"input": search}}).encode(),
                headers={"Content-Type": "application/json", "Authorization": "Bearer " + secret},
            )
            with opener(request, timeout=15) as response:
                answer = json.load(response)
            if not isinstance(answer, Mapping) or answer.get("errors"):
                raise RuntimeError("goalert refused the alert log query")
            alerts = answer["data"]["alerts"]
            nodes += [node for node in alerts["nodes"] if isinstance(node, Mapping)]
            page = alerts["pageInfo"]
            if not page["hasNextPage"]:
                return nodes
            after = str(page["endCursor"])
        raise RuntimeError(f"more than {READ_PAGES * READ_PAGE} alerts in one window")

    return read


def handle_update(engine: Engine, post: Post, update: Mapping[str, Any]) -> None:
    callback = update.get("callback_query")
    if isinstance(callback, Mapping):
        data = str(callback.get("data") or "")
        chat = str(((callback.get("message") or {}).get("chat") or {}).get("id", ""))
        reply = engine.text("denied")
        if data.startswith(ACK_PREFIX) and data[len(ACK_PREFIX) :].isdigit():
            who = person(callback.get("from"))
            reply = engine.acknowledge(int(data[len(ACK_PREFIX) :]), chat, who)
        post("answerCallbackQuery", {"callback_query_id": callback.get("id"), "text": reply})
        return
    message = update.get("message")
    if isinstance(message, Mapping) and str(message.get("text") or "").startswith("/start"):
        sender = (message.get("chat") or {}).get("id")
        post("sendMessage", {"chat_id": sender, "text": engine.text("start", chat=sender)})


def poll_once(engine: Engine, post: Post, offset: int, clock: Clock) -> int:
    try:
        return poll(engine, post, offset, clock)
    except urllib.error.HTTPError as error:
        if error.code == 409:
            engine.counters.set("poll_conflict", 1.0)
            raise Conflict("another process polls this bot token") from error
        raise


def poll(engine: Engine, post: Post, offset: int, clock: Clock) -> int:
    answer = post(
        "getUpdates",
        {
            "offset": offset,
            "timeout": POLL_TIMEOUT,
            "allowed_updates": ["callback_query", "message"],
        },
    )
    engine.counters.set("last_poll_timestamp_seconds", clock())
    for update in answer.get("result") or []:
        offset = max(offset, int(update.get("update_id", 0)) + 1)
        try:
            handle_update(engine, post, update)
        except Exception:
            engine.counters.add("failures_total", op="telegram")
    return offset


@dataclass(frozen=True)
class Settings:
    token: str
    telegram_api: str
    secret: str
    goalert: str
    public: str
    state_dir: Path
    listen: int
    config: Config
    acks_secret: str = ""


def settings_from(environ: Mapping[str, str]) -> Settings:
    zone = time_zone(environ.get("AYE_ONCALL_TIMEZONE") or "UTC")
    quiet = None
    if environ.get("AYE_ONCALL_QUIET_START") and environ.get("AYE_ONCALL_QUIET_END"):
        quiet = Quiet(
            parse_hhmm(environ["AYE_ONCALL_QUIET_START"]),
            parse_hhmm(environ["AYE_ONCALL_QUIET_END"]),
            zone,
        )
    return Settings(
        token=environ["AYE_ONCALL_TELEGRAM_TOKEN"],
        telegram_api=environ.get("AYE_ONCALL_TELEGRAM_API", "https://api.telegram.org"),
        secret=environ["AYE_ONCALL_WEBHOOK_SECRET"],
        goalert=environ.get("AYE_ONCALL_GOALERT", "http://goalert:8081"),
        public=environ.get("AYE_ONCALL_PUBLIC_URL", ""),
        state_dir=Path(environ.get("AYE_ONCALL_STATE", "/tmp")),
        listen=int(environ.get("AYE_ONCALL_LISTEN", "8080")),
        acks_secret=environ.get("AYE_ONCALL_ACKS_SECRET", ""),
        config=Config(
            chat_id=environ["AYE_ONCALL_CHAT_ID"],
            repeat_seconds=60.0 * float(environ.get("AYE_ONCALL_REPEAT_MINUTES", "5")),
            max_repeats=int(environ.get("AYE_ONCALL_MAX_REPEATS", "12")),
            quiet=quiet,
            language=environ.get("AYE_ONCALL_LANGUAGE", "en"),
            texts=load_texts(Path(environ.get("AYE_ONCALL_TEXTS", str(TEXTS_FILE)))),
            ops_chat_id=environ.get("AYE_ONCALL_OPS_CHAT_ID", ""),
            topics=environ.get("AYE_ONCALL_TOPICS", "").lower() == "true",
            zone=zone,
        ),
    )


def read_setup(state_dir: Path) -> Mapping[str, Any]:
    try:
        data = json.loads((state_dir / "setup.json").read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, Mapping) else {}


def internal(url: str, public: str, goalert: str) -> str:
    if public and url.startswith(public.rstrip("/")):
        return goalert.rstrip("/") + url[len(public.rstrip("/")) :]
    return url


def labels_of(alert: Mapping[str, Any]) -> Mapping[str, Any]:
    labels = alert.get("labels")
    return labels if isinstance(labels, Mapping) else {}


def group_summary(payload: Mapping[str, Any], firing: list[dict[str, Any]]) -> str:
    common = payload.get("commonLabels")
    grouped = payload.get("groupLabels")
    name = str((common if isinstance(common, Mapping) else {}).get("alertname") or "alert")
    scope = str((grouped if isinstance(grouped, Mapping) else {}).get("scope") or "")
    hosts = sorted(
        {str(labels_of(a).get("host") or labels_of(a).get("instance") or "") for a in firing} - {""}
    )
    text = f"{len(firing)} x {name}"
    if scope and hosts != [scope]:
        text += f" in {scope}"
    if hosts:
        text += ": " + ", ".join(hosts)
    return text


def collapse(payload: dict[str, Any]) -> None:
    alerts = [a for a in payload["alerts"] if isinstance(a, dict)]
    if not alerts:
        return
    firing = [a for a in alerts if a.get("status") == "firing"]
    lead = dict((firing or alerts)[0])
    key = str(payload.get("groupKey") or lead.get("fingerprint") or "")
    lead["status"] = "firing" if firing else "resolved"
    lead["fingerprint"] = hashlib.sha256(key.encode()).hexdigest()[:32]
    annotations = dict(lead.get("annotations") or {})
    annotations["fingerprint"] = lead["fingerprint"]
    if len(firing) > 1:
        common = payload.get("commonLabels")
        lead["labels"] = dict(common) if isinstance(common, Mapping) else {}
        annotations["summary"] = group_summary(payload, firing)
    lead["annotations"] = annotations
    payload["alerts"] = [lead]


def fold(body: bytes) -> dict[str, Any] | None:
    try:
        payload = json.loads(body)
    except ValueError:
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("alerts"), list):
        return None
    fill_links(payload)
    collapse(payload)
    return payload


def enrich(body: bytes) -> bytes:
    payload = fold(body)
    return body if payload is None else json.dumps(payload).encode()


def fill_links(payload: dict[str, Any]) -> None:
    for alert in payload["alerts"]:
        if not isinstance(alert, dict):
            continue
        annotations = alert.get("annotations")
        if not isinstance(annotations, dict):
            annotations = alert["annotations"] = {}
        found = link(alert.get("panelURL")) or link(alert.get("dashboardURL"))
        if found and not link(annotations.get("dashboard_url")):
            annotations["dashboard_url"] = found


def relay(
    settings: Settings, body: bytes, opener: Callable[..., Any] = urllib.request.urlopen
) -> int:
    target = str(read_setup(settings.state_dir).get("grafana_url") or "")
    if not target:
        return 503
    request = urllib.request.Request(
        internal(target, settings.public, settings.goalert),
        data=enrich(body),
        headers={"Content-Type": "application/json"},
    )
    try:
        with opener(request, timeout=15) as response:
            return int(response.status)
    except urllib.error.HTTPError as error:
        return int(error.code)
    except OSError:
        return 502


def acks_window(path: str) -> tuple[float, float] | None:
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)
    try:
        start, end = float(query["from"][0]), float(query["to"][0])
    except (KeyError, IndexError, ValueError):
        return None
    if not start <= end <= start + ACKS_SPAN:
        return None
    return start, end


def make_handler(
    settings: Settings, engine: Engine, clock: Clock, reader: Reader | None = None
) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def reply(self, code: int, body: str = "", kind: str = "text/plain") -> None:
            data = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, format: str, *args: Any) -> None:
            return

        def do_GET(self) -> None:
            if self.path == "/metrics":
                self.reply(200, engine.counters.render(), "text/plain; version=0.0.4")
                return
            if self.path == "/health":
                ready = bool(read_setup(settings.state_dir).get("ok"))
                polled = engine.counters.get("last_poll_timestamp_seconds")
                fresh = clock() - polled < 4 * POLL_TIMEOUT
                self.reply(
                    200 if ready and fresh else 503, "ok" if ready and fresh else "not ready"
                )
                return
            if urllib.parse.urlsplit(self.path).path == "/acks" and settings.acks_secret:
                self.answer_acks()
                return
            self.reply(404)

        def answer_acks(self) -> None:
            given = self.headers.get("Authorization") or ""
            if not hmac.compare_digest(given, "Bearer " + settings.acks_secret):
                self.reply(401)
                return
            window = acks_window(self.path)
            if window is None:
                self.reply(400)
                return
            if reader is None:
                self.reply(503)
                return
            try:
                nodes = reader(*window)
            except NotReady:
                self.reply(503)
                return
            except Exception:
                engine.counters.add("failures_total", op="acks")
                self.reply(502)
                return
            engine.counters.add("acks_reads_total")
            body = json.dumps({"alerts": engine.acks(nodes)}, sort_keys=True)
            self.reply(200, body, "application/json")

        def do_POST(self) -> None:
            length = min(int(self.headers.get("Content-Length") or 0), 1_000_000)
            body = self.rfile.read(length)
            if self.path == "/grafana":
                given = self.headers.get("Authorization") or ""
                if not hmac.compare_digest(given, "Bearer " + settings.secret):
                    self.reply(401)
                    return
                code = relay(settings, body)
                engine.counters.add("relayed_total", code=str(code))
                if code >= 400:
                    engine.counters.add("failures_total", op="relay")
                else:
                    engine.on_group(body)
                self.reply(code if code >= 400 else 200)
                return
            if self.path != "/goalert/" + settings.secret:
                self.reply(404)
                return
            try:
                payload = json.loads(body or b"{}")
            except ValueError:
                self.reply(400)
                return
            kind = payload.get("Type")
            if kind == "Alert":
                engine.on_alert(payload)
            elif kind == "AlertStatus":
                engine.on_status(payload)
            elif kind == "Verification":
                path = settings.state_dir / "verification.json"
                path.write_text(json.dumps({"code": str(payload.get("Code") or "")}), "utf-8")
            elif kind == "Test":
                engine.telegram(
                    "sendMessage", {"chat_id": engine.config.chat_id, "text": engine.text("test")}
                )
            self.reply(200)

    return Handler


def setup_reporter(counters: Counters) -> Callable[[bool], None]:
    def report(ok: bool) -> None:
        counters.set("setup_ok", 1.0 if ok else 0.0)
        if not ok:
            counters.add("failures_total", op="setup")

    return report


SETUP_FILE = Path(__file__).with_name("oncall_setup.py")


def load_setup(path: Path = SETUP_FILE) -> Any:
    spec = importlib.util.spec_from_file_location("oncall_setup", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def main(environ: Mapping[str, str] = os.environ) -> int:
    settings = settings_from(environ)
    post = telegram_poster(settings.token, settings.telegram_api)

    def ack_token() -> str:
        return str(read_setup(settings.state_dir).get("ack_token") or "")

    def read_token() -> str:
        return str(read_setup(settings.state_dir).get("read_token") or "")

    engine = Engine(
        settings.config,
        post,
        goalert_ack(settings.goalert, ack_token),
        time.time,
        settings.state_dir / "alerts.json",
    )
    server = ThreadingHTTPServer(
        ("0.0.0.0", settings.listen),
        make_handler(settings, engine, time.time, goalert_reader(settings.goalert, read_token)),
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    setup = load_setup()

    report = setup_reporter(engine.counters)
    threading.Thread(target=setup.converge, args=(environ, report), daemon=True).start()

    def ticker() -> None:
        while True:
            engine.tick()
            time.sleep(TICK)

    threading.Thread(target=ticker, daemon=True).start()
    offset = 0
    while True:
        try:
            offset = poll_once(engine, post, offset, time.time)
        except Conflict:
            print(json.dumps({"event": "oncall-poll-conflict"}), flush=True)
            while True:
                time.sleep(3600)
        except Exception:
            engine.counters.add("failures_total", op="poll")
            time.sleep(5)


if __name__ == "__main__":
    sys.exit(main())
