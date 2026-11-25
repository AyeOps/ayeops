from __future__ import annotations

import json
import os
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

KINDS = ("http", "fresh", "tls")
DURATION_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
DAY = 86400
DETAIL_LIMIT = 200
TELEGRAM_API = "https://api.telegram.org"
FRESH_QUERY = "time() - max(timestamp(up))"
NOTICES = ("alert", "reminder", "recovery")

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


def http_fetch(url: str, timeout: float) -> tuple[int, bytes]:
    request = urllib.request.Request(url, headers={"User-Agent": "ayeops-sentinel"})
    try:
        with OPENER.open(request, timeout=timeout) as response:
            return int(response.status), response.read(65536)
    except urllib.error.HTTPError as error:
        return int(error.code), b""


def certificate_expiry(host: str, port: int, timeout: float) -> float:
    context = ssl.create_default_context()
    raw = socket.create_connection((host, port), timeout=timeout)
    try:
        with context.wrap_socket(raw, server_hostname=host) as tls:
            certificate = tls.getpeercert() or {}
    finally:
        raw.close()
    return float(ssl.cert_time_to_seconds(str(certificate["notAfter"])))


def telegram_sender(token: str, chat: str, timeout: float = 15) -> Send:
    url = f"{TELEGRAM_API}/bot{token}/sendMessage"

    def send(text: str) -> bool:
        body = json.dumps(
            {"chat_id": chat, "text": text, "disable_web_page_preview": True}
        ).encode()
        request = urllib.request.Request(
            url, data=body, headers={"Content-Type": "application/json"}, method="POST"
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return 200 <= int(response.status) < 300
        except urllib.error.HTTPError as error:
            log_json({"event": "telegram_failed", "status": error.code})
        except (OSError, ValueError) as error:
            log_json({"event": "telegram_failed", "error": type(error).__name__})
        return False

    return send


class Sentinel:
    def __init__(
        self, config: Config, fetch: Fetch, expiry: Expiry, send: Send, clock: Clock
    ) -> None:
        self.config = config
        self.fetch = fetch
        self.expiry = expiry
        self.send = send
        self.clock = clock
        self.state = State()
        self.outcomes: tuple[Outcome, ...] = ()
        self.notices = dict.fromkeys(NOTICES, 0)
        self.notice_failures = dict.fromkeys(NOTICES, 0)
        self.last_notice_ok = 1
        self.ping_ok: int | None = None
        self.last_round = 0.0
        self.lock = threading.Lock()

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
        delivered = False
        if notice is not None:
            delivered = self.send(notice_text(self.config, previous, current, notice, now))
            self.notices[notice] += int(delivered)
            self.notice_failures[notice] += int(not delivered)
            self.last_notice_ok = int(delivered)
            log_json({"event": notice, "delivered": delivered, "streak": current.streak})
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
            self.state = settle(previous, current, notice, delivered)
            self.outcomes = outcomes
            self.last_round = now
        if all(outcome.ok for outcome in outcomes):
            self.ping()
        return notice

    def samples(self) -> list[Sample]:
        watch = self.config.watch
        with self.lock:
            state = self.state
            outcomes = self.outcomes
            last_round = self.last_round
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
        for kind in NOTICES:
            pair = labels(watch=watch, kind=kind)
            samples.append(("ayeops_sentinel_notices_total", pair, self.notices[kind]))
            samples.append(
                ("ayeops_sentinel_notice_failures_total", pair, self.notice_failures[kind])
            )
        if self.ping_ok is not None:
            samples.append(("ayeops_sentinel_ping_success", labels(watch=watch), self.ping_ok))
        return samples

    def metrics(self) -> str:
        return render(self.samples())

    def healthy(self, now: float) -> bool:
        with self.lock:
            last_round = self.last_round
        return now - last_round <= 3 * self.config.interval_seconds + self.round_budget()

    def round_budget(self) -> float:
        return self.config.timeout_seconds * len(self.config.checks)


def config_from(env: Mapping[str, str]) -> Config:
    ping_url = env.get("AYE_SENTINEL_PING_URL", "").strip()
    threshold = int(env.get("AYE_SENTINEL_FAILURES_BEFORE_ALERT", "3"))
    if threshold < 1:
        raise ValueError("AYE_SENTINEL_FAILURES_BEFORE_ALERT must be at least 1")
    return Config(
        project=env.get("AYE_SENTINEL_PROJECT", "ayeops"),
        origin=env.get("AYE_SENTINEL_HOST", socket.gethostname()),
        watch=env["AYE_SENTINEL_WATCH"],
        checks=parse_checks(env["AYE_SENTINEL_CHECKS"]),
        interval_seconds=parse_duration(env.get("AYE_SENTINEL_INTERVAL", "60s")),
        timeout_seconds=parse_duration(env.get("AYE_SENTINEL_TIMEOUT", "10s")),
        failures_before_alert=threshold,
        reminder_seconds=parse_duration(env.get("AYE_SENTINEL_REMINDER", "6h")),
        ping_url=ping_url,
        runbook=env.get("AYE_SENTINEL_RUNBOOK", ""),
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
    send = telegram_sender(env["AYE_TELEGRAM_BOT_TOKEN"], env["AYE_TELEGRAM_CHAT_ID"])
    sentinel = Sentinel(config, http_fetch, certificate_expiry, send, time.time)
    log_json(
        {
            "event": "start",
            "watch": config.watch,
            "checks": [check.name for check in config.checks],
            "failures_before_alert": config.failures_before_alert,
            "reminder_seconds": config.reminder_seconds,
            "ping": bool(config.ping_url),
        }
    )
    sentinel.last_round = time.time()
    threading.Thread(target=run_forever, args=(sentinel,), daemon=True).start()
    make_server(sentinel, int(env.get("AYE_SENTINEL_PORT", "9134"))).serve_forever()


if __name__ == "__main__":
    main()
