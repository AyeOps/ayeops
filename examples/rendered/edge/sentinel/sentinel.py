from __future__ import annotations

import json
import math
import os
import re
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta, tzinfo
from datetime import time as day_time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

KINDS = ("http", "fresh", "tls")
DURATION_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
DAY = 86400
DETAIL_LIMIT = 200
TELEGRAM_API = "https://api.telegram.org"
SLACK_WEBHOOK_ENV = "AYE_SLACK_WEBHOOK_URL"
FRESH_QUERY = "time() - max(timestamp(up))"
NOTICES = ("alert", "reminder", "recovery")
DIGEST = "digest"
READ_LIMIT = 65536
DIGEST_READ_LIMIT = 1048576
DIGEST_LIMIT = 4000
NAMED = 5
CLOCK = re.compile(r"^([01][0-9]|2[0-3]):([0-5][0-9])$")
DUMP_FIELDS = {
    "ayeops_backup_max_age_seconds": "limit",
    "ayeops_backup_last_success_timestamp_seconds": "success",
    "ayeops_backup_declared_timestamp_seconds": "declared",
    "ayeops_backup_last_run_success": "run_ok",
    "ayeops_backup_last_size_bytes": "size",
}
OFFSITE_FIELDS = {
    "ayeops_offsite_max_age_seconds": "limit",
    "ayeops_offsite_last_success_timestamp_seconds": "success",
    "ayeops_offsite_restore_test_ok": "restore_ok",
    "ayeops_offsite_restore_test_last_success_timestamp_seconds": "restore_at",
}
LOWEST = frozenset({"run_ok", "restore_ok"})
DIGEST_QUERY = '{__name__=~"' + "|".join(sorted([*DUMP_FIELDS, *OFFSITE_FIELDS])) + '"}'
OFFSITE_MAX_AGE = 93600
RESTORE_MAX_AGE = 691200
BYTE_UNITS = ("KiB", "MiB", "GiB", "TiB")

Fetch = Callable[[str, float], tuple[int, bytes]]
Expiry = Callable[[str, int, float], float]
Send = Callable[[str], bool]
Clock = Callable[[], float]
Sample = tuple[str, tuple[tuple[str, str], ...], float]


@dataclass(frozen=True)
class Check:
    name: str
    kind: str
    url: str = ""
    contains: str = ""
    query: str = FRESH_QUERY
    max_age_seconds: float = 180
    host: str = ""
    port: int = 443
    min_days: float = 7


@dataclass(frozen=True)
class Outcome:
    name: str
    ok: bool
    detail: str
    seconds: float = 0.0


@dataclass(frozen=True)
class State:
    streak: int = 0
    down_since: float | None = None
    alerted_at: float | None = None
    failures: tuple[Outcome, ...] = ()

    @property
    def alerting(self) -> bool:
        return self.alerted_at is not None


@dataclass(frozen=True)
class Config:
    project: str
    origin: str
    watch: str
    checks: tuple[Check, ...]
    interval_seconds: float = 60
    timeout_seconds: float = 10
    failures_before_alert: int = 3
    reminder_seconds: float = 6 * 3600
    ping_url: str = ""
    runbook: str = ""
    digest_at: str = ""
    digest_timezone: str = "UTC"
    digest_url: str = ""


@dataclass(frozen=True)
class Backup:
    host: str
    kind: str
    name: str = ""
    limit: float | None = None
    success: float | None = None
    declared: float | None = None
    run_ok: float | None = None
    size: float | None = None
    restore_ok: float | None = None
    restore_at: float | None = None

    @property
    def label(self) -> str:
        if self.kind == "offsite":
            return f"{self.host} off-site copy"
        return f"{self.host}/{self.name}"


def parse_duration(text: str) -> int:
    value = text.strip().lower()
    if len(value) < 2 or value[-1] not in DURATION_UNITS or not value[:-1].isdigit():
        raise ValueError(f"not a duration: {text!r}")
    seconds = int(value[:-1]) * DURATION_UNITS[value[-1]]
    if seconds <= 0:
        raise ValueError(f"not a positive duration: {text!r}")
    return seconds


def format_duration(seconds: float) -> str:
    total = max(0, int(seconds))
    days, rest = divmod(total, DAY)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m"
    return f"{secs}s"


def clock_time(moment: float) -> str:
    return datetime.fromtimestamp(moment, UTC).strftime("%Y-%m-%d %H:%M UTC")


def shorten(text: str) -> str:
    flat = " ".join(str(text).split())
    if len(flat) <= DETAIL_LIMIT:
        return flat
    return flat[: DETAIL_LIMIT - 1] + "…"


def format_bytes(size: float) -> str:
    value = max(0.0, float(size))
    if value < 1024:
        return f"{value:.0f} B"
    value /= 1024
    index = 0
    while value >= 1024 and index < len(BYTE_UNITS) - 1:
        value /= 1024
        index += 1
    return f"{value:.1f} {BYTE_UNITS[index]}"


def parse_clock(text: str) -> tuple[int, int]:
    found = CLOCK.match(text.strip())
    if found is None:
        raise ValueError(f"not a time of day like 08:00: {text!r}")
    return int(found.group(1)), int(found.group(2))


def time_zone(key: str) -> tzinfo:
    try:
        return ZoneInfo(key)
    except ZoneInfoNotFoundError:
        if key == "UTC":
            return UTC
        raise


def next_digest(after: float, at: str, zone: tzinfo) -> float:
    moment = day_time(*parse_clock(at))
    day = datetime.fromtimestamp(after, zone).date()
    slot = datetime.combine(day, moment, tzinfo=zone)
    if slot.timestamp() <= after:
        slot = datetime.combine(day + timedelta(days=1), moment, tzinfo=zone)
    return slot.timestamp()


def series_value(series: Any) -> tuple[str, Mapping[str, Any], float] | None:
    if not isinstance(series, Mapping):
        return None
    metric = series.get("metric")
    value = series.get("value")
    if not isinstance(metric, Mapping) or not isinstance(value, list) or len(value) != 2:
        return None
    try:
        number = float(value[1])
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return str(metric.get("__name__") or ""), metric, number


def backups_from(answer: Any) -> list[Backup] | None:
    if not isinstance(answer, Mapping) or answer.get("status") != "success":
        return None
    result = (answer.get("data") or {}).get("result")
    if not isinstance(result, list):
        return None
    found: dict[tuple[str, str, str], dict[str, float]] = {}
    for series in result:
        parsed = series_value(series)
        if parsed is None:
            continue
        name, metric, number = parsed
        host = str(metric.get("host") or "")
        if name in DUMP_FIELDS:
            key, field = (host, "dump", str(metric.get("backup") or "")), DUMP_FIELDS[name]
        elif name in OFFSITE_FIELDS:
            key, field = (host, "offsite", ""), OFFSITE_FIELDS[name]
        else:
            continue
        fields = found.setdefault(key, {})
        if field in fields:
            number = (min if field in LOWEST else max)(fields[field], number)
        fields[field] = number
    return [
        Backup(host, kind, name, **fields)
        for (host, kind, name), fields in sorted(found.items())
        if kind == "offsite" or "limit" in fields
    ]


def dump_verdict(backup: Backup, now: float) -> tuple[bool, list[str]]:
    reference = backup.declared if backup.success is None else backup.success
    if reference is None or backup.limit is None:
        return False, ["no run recorded"]
    age = now - reference
    stale = age > backup.limit
    problems = ["stale"] if stale else []
    if backup.run_ok == 0:
        problems.append("last run failed")
    if backup.success is None:
        facts = [f"no success yet, declared {format_duration(age)} ago"]
    else:
        facts = [f"last success {format_duration(age)} ago"]
    if stale:
        facts.append(f"limit {format_duration(backup.limit)}")
    if backup.size is not None:
        facts.append(format_bytes(backup.size))
    return not problems, problems + facts


def offsite_verdict(backup: Backup, now: float) -> tuple[bool, list[str]]:
    problems: list[str] = []
    facts: list[str] = []
    limit = OFFSITE_MAX_AGE if backup.limit is None else backup.limit
    if backup.success is None:
        problems.append("no copy recorded")
    else:
        age = now - backup.success
        if age > limit:
            problems.append("stale")
        facts.append(f"last copy {format_duration(age)} ago")
    if backup.restore_ok == 0:
        problems.append("restore test failed")
    if backup.restore_at is None:
        problems.append("no restore test passed")
    else:
        tested = now - backup.restore_at
        if tested > RESTORE_MAX_AGE:
            problems.append("restore test overdue")
        facts.append(f"restore test passed {format_duration(tested)} ago")
    return not problems, problems + facts


def backup_line(backup: Backup, now: float) -> tuple[bool, str]:
    verdict = offsite_verdict if backup.kind == "offsite" else dump_verdict
    ok, parts = verdict(backup, now)
    return ok, f"• {backup.label}: " + ", ".join(parts)


def fit(head: Sequence[str], lines: Sequence[str], tail: Sequence[str], limit: int) -> str:
    kept = list(lines)
    while True:
        dropped = len(lines) - len(kept)
        more = [f"• and {dropped} more"] if dropped else []
        text = "\n".join([*head, *kept, *more, *tail])
        if len(text) <= limit or not kept:
            return text
        kept.pop()


def digest_text(config: Config, backups: Sequence[Backup] | None, error: str, now: float) -> str:
    head = [f"💾 {config.project}: backup digest, {clock_time(now)}"]
    tail = [
        f"Read from {config.watch} by the AyeOps sentinel on {config.origin},"
        f" every day at {config.digest_at} {config.digest_timezone}."
    ]
    lines: list[str] = []
    if backups is None:
        head.append(f"🟠 Cannot read the backup metrics from {config.watch}: {error}")
    elif not backups:
        head.append(f"🟠 No backups reported to {config.watch}: no dump job, no off-site copy")
    else:
        rows = [(backup.label, *backup_line(backup, now)) for backup in backups]
        bad = [label for label, ok, _ in rows if not ok]
        if bad:
            names = ", ".join(bad[:NAMED])
            if len(bad) > NAMED:
                names += f" and {len(bad) - NAMED} more"
            head.append(f"🟠 {len(bad)} of {len(backups)} backups stale or failed: {names}")
        else:
            head.append(f"✅ All {len(backups)} backups fresh")
        lines = [line for _, ok, line in rows if not ok] + [line for _, ok, line in rows if ok]
    return fit(head, lines, tail, DIGEST_LIMIT)


def parse_checks(text: str) -> tuple[Check, ...]:
    raw = json.loads(text)
    if not isinstance(raw, list) or not raw:
        raise ValueError("AYE_SENTINEL_CHECKS must be a non-empty JSON list")
    checks: list[Check] = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError(f"a check must be an object: {item!r}")
        check = Check(
            name=str(item.get("name") or ""),
            kind=str(item.get("kind") or ""),
            url=str(item.get("url") or ""),
            contains=str(item.get("contains") or ""),
            query=str(item.get("query") or FRESH_QUERY),
            max_age_seconds=float(item.get("max_age_seconds", 180)),
            host=str(item.get("host") or ""),
            port=int(item.get("port", 443)),
            min_days=float(item.get("min_days", 7)),
        )
        checks.append(check)
    for problem in check_problems(checks):
        raise ValueError(problem)
    return tuple(checks)


def check_problems(checks: Sequence[Check]) -> list[str]:
    problems: list[str] = []
    seen: set[str] = set()
    for check in checks:
        if not check.name:
            problems.append("a check has no name")
        elif check.name in seen:
            problems.append(f"check {check.name!r} is listed twice")
        seen.add(check.name)
        if check.kind not in KINDS:
            problems.append(f"check {check.name!r} has unknown kind {check.kind!r}")
        elif check.kind == "tls":
            if not check.host:
                problems.append(f"check {check.name!r} of kind tls needs host")
        elif not check.url.startswith(("http://", "https://")):
            problems.append(f"check {check.name!r} of kind {check.kind} needs an http(s) url")
    return problems


def http_verdict(status: int, body: bytes, contains: str) -> tuple[bool, str]:
    if not 200 <= status < 300:
        return False, f"HTTP {status}"
    if contains and contains not in body.decode("utf-8", "replace"):
        return False, f"HTTP {status} without {contains!r}"
    return True, f"HTTP {status}"


def sample_age(answer: Any) -> float | None:
    if not isinstance(answer, Mapping) or answer.get("status") != "success":
        return None
    result = (answer.get("data") or {}).get("result")
    if not isinstance(result, list) or not result:
        return None
    value = result[0].get("value") if isinstance(result[0], Mapping) else None
    if not isinstance(value, list) or len(value) != 2:
        return None
    try:
        return float(value[1])
    except (TypeError, ValueError):
        return None


def fresh_verdict(answer: Any, max_age: float) -> tuple[bool, str]:
    age = sample_age(answer)
    if age is None:
        return False, "no samples of up"
    if age > max_age:
        return False, f"newest sample is {format_duration(age)} old"
    return True, f"newest sample is {format_duration(age)} old"


def tls_verdict(not_after: float, now: float, min_days: float) -> tuple[bool, str]:
    days = (not_after - now) / DAY
    if days < 0:
        return False, "certificate expired"
    if days < min_days:
        return False, f"certificate expires in {days:.1f} days"
    return True, f"certificate valid for {days:.0f} days"


def failure_detail(error: BaseException) -> str:
    if isinstance(error, urllib.error.URLError) and not isinstance(error, urllib.error.HTTPError):
        return shorten(f"{type(error).__name__}: {error.reason}")
    return shorten(f"{type(error).__name__}: {error}")


def advance(
    state: State, outcomes: Sequence[Outcome], now: float, threshold: int, reminder: float
) -> tuple[State, str | None]:
    failures = tuple(outcome for outcome in outcomes if not outcome.ok)
    if not failures:
        return State(), "recovery" if state.alerting else None
    streak = state.streak + 1
    since = now if state.down_since is None else state.down_since
    if state.alerted_at is None:
        notice = "alert" if streak >= threshold else None
    else:
        notice = "reminder" if now - state.alerted_at >= reminder else None
    alerted_at = now if notice else state.alerted_at
    return State(streak, since, alerted_at, failures), notice


def settle(previous: State, current: State, notice: str | None, delivered: bool) -> State:
    if notice is None or delivered:
        return current
    if notice == "recovery":
        return replace(previous, streak=0, failures=())
    return replace(current, alerted_at=previous.alerted_at)


def changed(before: Outcome | None, after: Outcome) -> bool:
    if before is None:
        return not after.ok
    return before.ok != after.ok or (not after.ok and before.detail != after.detail)


def failure_lines(failures: Iterable[Outcome]) -> list[str]:
    return [f"• {outcome.name}: {outcome.detail}" for outcome in failures]


def notice_text(config: Config, previous: State, current: State, notice: str, now: float) -> str:
    who = f"{config.project}: {config.watch}"
    if notice == "recovery":
        since = previous.down_since if previous.down_since is not None else now
        return (
            f"✅ {who} answers again after {format_duration(now - since)}\n"
            f"Seen from {config.origin} by the AyeOps sentinel."
        )
    since = current.down_since if current.down_since is not None else now
    if notice == "alert":
        head = f"🔴 {who} is not answering"
        when = f"{current.streak} checks in a row failed since {clock_time(since)}:"
    else:
        head = f"🔴 {who} is still not answering, down for {format_duration(now - since)}"
        when = "Failing now:"
    lines = [head, when, *failure_lines(current.failures)]
    lines.append(
        f"Seen from {config.origin} by the AyeOps sentinel; "
        f"alerts of {config.watch} may not arrive."
    )
    if config.runbook:
        lines.append(f"Runbook: {config.runbook}")
    return "\n".join(lines)


def labels(**pairs: str) -> tuple[tuple[str, str], ...]:
    return tuple(sorted(pairs.items()))


def escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def format_value(value: float) -> str:
    if value == int(value) and abs(value) < 1e15:
        return str(int(value))
    return repr(float(value))


COUNTERS = frozenset({"ayeops_sentinel_notices_total", "ayeops_sentinel_notice_failures_total"})


def render(samples: Iterable[Sample]) -> str:
    grouped: dict[str, list[tuple[tuple[tuple[str, str], ...], float]]] = {}
    for name, pairs, value in samples:
        grouped.setdefault(name, []).append((pairs, value))
    lines: list[str] = []
    for name in sorted(grouped):
        lines.append(f"# TYPE {name} {'counter' if name in COUNTERS else 'gauge'}")
        for pairs, value in sorted(grouped[name]):
            body = ",".join(f'{key}="{escape(text)}"' for key, text in pairs)
            text = format_value(value)
            lines.append(f"{name}{{{body}}} {text}" if body else f"{name} {text}")
    return "\n".join(lines) + "\n"


def log_json(record: Mapping[str, Any]) -> None:
    print(json.dumps(record, sort_keys=True, ensure_ascii=False), flush=True)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


OPENER = urllib.request.build_opener(NoRedirect)


def http_fetch(url: str, timeout: float, limit: int = READ_LIMIT) -> tuple[int, bytes]:
    request = urllib.request.Request(url, headers={"User-Agent": "ayeops-sentinel"})
    try:
        with OPENER.open(request, timeout=timeout) as response:
            return int(response.status), response.read(limit)
    except urllib.error.HTTPError as error:
        return int(error.code), b""


def query_fetch(url: str, timeout: float) -> tuple[int, bytes]:
    return http_fetch(url, timeout, DIGEST_READ_LIMIT)


def certificate_expiry(host: str, port: int, timeout: float) -> float:
    context = ssl.create_default_context()
    raw = socket.create_connection((host, port), timeout=timeout)
    try:
        with context.wrap_socket(raw, server_hostname=host) as tls:
            certificate = tls.getpeercert() or {}
    finally:
        raw.close()
    return float(ssl.cert_time_to_seconds(str(certificate["notAfter"])))


def post_json(url: str, payload: dict[str, Any], event: str, timeout: float) -> bool:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return 200 <= int(response.status) < 300
    except urllib.error.HTTPError as error:
        log_json({"event": event, "status": error.code})
    except (OSError, ValueError) as error:
        log_json({"event": event, "error": type(error).__name__})
    return False


def telegram_sender(token: str, chat: str, timeout: float = 15) -> Send:
    url = f"{TELEGRAM_API}/bot{token}/sendMessage"

    def send(text: str) -> bool:
        payload: dict[str, Any] = {"chat_id": chat, "text": text, "disable_web_page_preview": True}
        return post_json(url, payload, "telegram_failed", timeout)

    return send


def slack_text(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def slack_sender(url: str, timeout: float = 15) -> Send:
    def send(text: str) -> bool:
        payload: dict[str, Any] = {"text": slack_text(text), "mrkdwn": False}
        return post_json(url, payload, "slack_failed", timeout)

    return send


def sender_from(env: Mapping[str, str]) -> Send:
    webhook = env.get(SLACK_WEBHOOK_ENV, "").strip()
    if webhook:
        return slack_sender(webhook)
    return telegram_sender(env["AYE_TELEGRAM_BOT_TOKEN"], env["AYE_TELEGRAM_CHAT_ID"])


class Sentinel:
    def __init__(
        self,
        config: Config,
        fetch: Fetch,
        expiry: Expiry,
        send: Send,
        clock: Clock,
        query: Fetch | None = None,
    ) -> None:
        self.config = config
        self.fetch = fetch
        self.query = fetch if query is None else query
        self.expiry = expiry
        self.send = send
        self.clock = clock
        self.state = State()
        self.outcomes: tuple[Outcome, ...] = ()
        self.kinds = (*NOTICES, DIGEST) if config.digest_at else NOTICES
        self.notices = dict.fromkeys(self.kinds, 0)
        self.notice_failures = dict.fromkeys(self.kinds, 0)
        self.last_notice_ok = 1
        self.ping_ok: int | None = None
        self.last_round = 0.0
        self.digest_due = self.next_digest(clock()) if config.digest_at else None
        self.digest_sent: float | None = None
        self.lock = threading.Lock()

    def next_digest(self, after: float) -> float:
        zone = time_zone(self.config.digest_timezone)
        return next_digest(after, self.config.digest_at, zone)

    def read_backups(self) -> tuple[list[Backup] | None, str]:
        query = urllib.parse.urlencode({"query": DIGEST_QUERY})
        try:
            status, body = self.query(
                f"{self.config.digest_url}?{query}", self.config.timeout_seconds
            )
            if not 200 <= status < 300:
                return None, f"HTTP {status}"
            backups = backups_from(json.loads(body))
        except Exception as error:
            return None, failure_detail(error)
        if backups is None:
            return None, "the answer is not a query result"
        return backups, ""

    def digest(self, now: float) -> bool | None:
        if self.digest_due is None or now < self.digest_due:
            return None
        backups, error = self.read_backups()
        delivered = self.send(digest_text(self.config, backups, error, now))
        self.notices[DIGEST] += int(delivered)
        self.notice_failures[DIGEST] += int(not delivered)
        self.last_notice_ok = int(delivered)
        count = None if backups is None else len(backups)
        log_json({"event": DIGEST, "delivered": delivered, "backups": count, "error": error})
        if delivered:
            due = self.next_digest(now)
            with self.lock:
                self.digest_sent = now
                self.digest_due = due
        return delivered

    def probe(self, check: Check) -> Outcome:
        started = self.clock()
        timeout = self.config.timeout_seconds
        try:
            if check.kind == "tls":
                ok, detail = tls_verdict(
                    self.expiry(check.host, check.port, timeout), started, check.min_days
                )
            elif check.kind == "fresh":
                query = urllib.parse.urlencode({"query": check.query})
                status, body = self.fetch(f"{check.url}?{query}", timeout)
                if not 200 <= status < 300:
                    ok, detail = False, f"HTTP {status}"
                else:
                    ok, detail = fresh_verdict(json.loads(body), check.max_age_seconds)
            else:
                status, body = self.fetch(check.url, timeout)
                ok, detail = http_verdict(status, body, check.contains)
        except Exception as error:
            ok, detail = False, failure_detail(error)
        return Outcome(check.name, ok, detail, max(0.0, self.clock() - started))

    def ping(self) -> None:
        if not self.config.ping_url:
            return
        try:
            status, _ = self.fetch(self.config.ping_url, self.config.timeout_seconds)
            self.ping_ok = int(200 <= status < 300)
        except Exception as error:
            self.ping_ok = 0
            log_json({"event": "ping_failed", "error": failure_detail(error)})

    def tick(self) -> str | None:
        outcomes = tuple(self.probe(check) for check in self.config.checks)
        now = self.clock()
        previous = self.state
        current, notice = advance(
            previous,
            outcomes,
            now,
            self.config.failures_before_alert,
            self.config.reminder_seconds,
        )
        settled = current
        if notice is not None:
            delivered = self.send(notice_text(self.config, previous, current, notice, now))
            self.notices[notice] += int(delivered)
            self.notice_failures[notice] += int(not delivered)
            self.last_notice_ok = int(delivered)
            log_json({"event": notice, "delivered": delivered, "streak": current.streak})
            settled = settle(previous, current, notice, delivered)
        before = {outcome.name: outcome for outcome in self.outcomes}
        for outcome in outcomes:
            if changed(before.get(outcome.name), outcome):
                log_json(
                    {
                        "event": "check_ok" if outcome.ok else "check_failed",
                        "check": outcome.name,
                        "detail": outcome.detail,
                    }
                )
        with self.lock:
            self.state = settled
            self.outcomes = outcomes
            self.last_round = now
        self.digest(now)
        if all(outcome.ok for outcome in outcomes):
            self.ping()
        return notice

    def samples(self) -> list[Sample]:
        watch = self.config.watch
        with self.lock:
            state = self.state
            outcomes = self.outcomes
            last_round = self.last_round
            due = self.digest_due
            sent = self.digest_sent
        samples: list[Sample] = [
            ("ayeops_sentinel_failure_streak", labels(watch=watch), state.streak),
            ("ayeops_sentinel_alerting", labels(watch=watch), int(state.alerting)),
            ("ayeops_sentinel_last_round_timestamp_seconds", labels(watch=watch), last_round),
            ("ayeops_sentinel_last_notice_ok", labels(watch=watch), self.last_notice_ok),
        ]
        for outcome in outcomes:
            pair = labels(watch=watch, check=outcome.name)
            samples.append(("ayeops_sentinel_check_success", pair, int(outcome.ok)))
            samples.append(("ayeops_sentinel_check_duration_seconds", pair, outcome.seconds))
        for kind in self.kinds:
            pair = labels(watch=watch, kind=kind)
            samples.append(("ayeops_sentinel_notices_total", pair, self.notices[kind]))
            samples.append(
                ("ayeops_sentinel_notice_failures_total", pair, self.notice_failures[kind])
            )
        if self.ping_ok is not None:
            samples.append(("ayeops_sentinel_ping_success", labels(watch=watch), self.ping_ok))
        if due is not None:
            samples.append(
                ("ayeops_sentinel_digest_due_timestamp_seconds", labels(watch=watch), due)
            )
        if sent is not None:
            samples.append(
                ("ayeops_sentinel_digest_last_sent_timestamp_seconds", labels(watch=watch), sent)
            )
        return samples

    def metrics(self) -> str:
        return render(self.samples())

    def healthy(self, now: float) -> bool:
        with self.lock:
            last_round = self.last_round
        return now - last_round <= 3 * self.config.interval_seconds + self.round_budget()

    def round_budget(self) -> float:
        fetches = len(self.config.checks) + int(bool(self.config.digest_at))
        return self.config.timeout_seconds * fetches


def digest_from(env: Mapping[str, str]) -> tuple[str, str, str]:
    at = env.get("AYE_SENTINEL_DIGEST_AT", "").strip()
    zone = env.get("AYE_SENTINEL_DIGEST_TIMEZONE", "").strip() or "UTC"
    url = env.get("AYE_SENTINEL_DIGEST_URL", "").strip()
    if not at:
        return "", "UTC", ""
    parse_clock(at)
    time_zone(zone)
    if not url.startswith(("http://", "https://")):
        raise ValueError(
            f"AYE_SENTINEL_DIGEST_URL must be the http(s) url of /api/v1/query, not {url!r}"
        )
    return at, zone, url


def env_seconds(env: Mapping[str, str], name: str, default: int) -> int:
    return parse_duration(env[name]) if name in env else default


def config_from(env: Mapping[str, str]) -> Config:
    ping_url = env.get("AYE_SENTINEL_PING_URL", "").strip()
    threshold = int(env.get("AYE_SENTINEL_FAILURES_BEFORE_ALERT", "3"))
    if threshold < 1:
        raise ValueError("AYE_SENTINEL_FAILURES_BEFORE_ALERT must be at least 1")
    digest_at, digest_timezone, digest_url = digest_from(env)
    return Config(
        project=env.get("AYE_SENTINEL_PROJECT", "ayeops"),
        origin=env.get("AYE_SENTINEL_HOST", socket.gethostname()),
        watch=env["AYE_SENTINEL_WATCH"],
        checks=parse_checks(env["AYE_SENTINEL_CHECKS"]),
        interval_seconds=env_seconds(env, "AYE_SENTINEL_INTERVAL", 60),
        timeout_seconds=env_seconds(env, "AYE_SENTINEL_TIMEOUT", 10),
        failures_before_alert=threshold,
        reminder_seconds=env_seconds(env, "AYE_SENTINEL_REMINDER", 21600),
        ping_url=ping_url,
        runbook=env.get("AYE_SENTINEL_RUNBOOK", ""),
        digest_at=digest_at,
        digest_timezone=digest_timezone,
        digest_url=digest_url,
    )


def make_server(
    sentinel: Sentinel, port: int, host: str = "", now: Clock = time.time
) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path == "/health":
                if not sentinel.healthy(now()):
                    self.send_error(503)
                    return
                body = b"ok\n"
            elif self.path == "/metrics":
                body = sentinel.metrics().encode()
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:
            return

    return ThreadingHTTPServer((host, port), Handler)


def one_round(sentinel: Sentinel, monotonic: Clock = time.monotonic) -> float:
    started = monotonic()
    try:
        sentinel.tick()
    except Exception as error:
        log_json({"event": "round_failed", "error": failure_detail(error)})
    elapsed = monotonic() - started
    return max(1.0, sentinel.config.interval_seconds - elapsed)


def run_forever(sentinel: Sentinel) -> None:
    while True:
        time.sleep(one_round(sentinel))


def main() -> None:
    env = os.environ
    config = config_from(env)
    send = sender_from(env)
    sentinel = Sentinel(config, http_fetch, certificate_expiry, send, time.time, query_fetch)
    log_json(
        {
            "event": "start",
            "watch": config.watch,
            "checks": [check.name for check in config.checks],
            "failures_before_alert": config.failures_before_alert,
            "reminder_seconds": config.reminder_seconds,
            "ping": bool(config.ping_url),
            "digest": config.digest_at,
        }
    )
    sentinel.last_round = time.time()
    threading.Thread(target=run_forever, args=(sentinel,), daemon=True).start()
    make_server(sentinel, int(env.get("AYE_SENTINEL_PORT", "9134"))).serve_forever()


if __name__ == "__main__":
    main()
