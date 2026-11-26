from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

RANK = {"operational": 0, "no_data": 1, "degraded": 2, "outage": 3}
SEVERITY_STATES = {"critical": "outage", "page": "outage", "warning": "degraded"}
FIRING = frozenset({"firing", "recovering"})
FIRING_INSTANCE = frozenset({"Alerting", "Recovering"})
UNKNOWN = frozenset({"Error", "NoData"})
UNKNOWN_HEALTH = frozenset({"error", "nodata"})
RULES_PATH = "/api/prometheus/grafana/api/v1/rules"
SILENCES_PATH = "/api/alertmanager/grafana/api/v2/silences"
SERVICE_LABEL = "service"
MAINTENANCE_LABEL = "maintenance"
SIGNATURE_HEADER = "X-AyeOps-Signature"
NAME = re.compile(r"^[a-z][a-z0-9-]{0,39}$")
MAX_COMPONENTS = 100
MAX_ATTEMPTS = 3
BACKOFF_SECONDS = 2.0
TRANSIENT_CODES = frozenset({408, 425, 429})
DURATION_UNITS = {"s": 1, "m": 60}
DETAIL_LIMIT = 200
USER_AGENT = "ayeops-status-reporter"
COUNTERS = frozenset({"ayeops_status_pushes_total", "ayeops_status_push_failures_total"})
TOKEN = re.compile(r"[!-~]+")

Http = Callable[[urllib.request.Request, float], tuple[int, bytes]]
Clock = Callable[[], float]
Sleep = Callable[[float], None]
Sample = tuple[str, tuple[tuple[str, str], ...], float]


class QueryError(RuntimeError):
    pass


@dataclass(frozen=True)
class Component:
    id: str
    services: tuple[str, ...]


@dataclass(frozen=True)
class Config:
    tenant: str
    url: str
    secret: str
    grafana: str
    token_file: Path
    components: tuple[Component, ...]
    interval_seconds: float = 60
    timeout_seconds: float = 10


@dataclass(frozen=True)
class Outcome:
    ok: bool
    reason: str = ""
    status: int = 0
    attempts: int = 0
    detail: str = ""


def parse_duration(text: str) -> int:
    value = text.strip().lower()
    if len(value) < 2 or value[-1] not in DURATION_UNITS or not value[:-1].isdigit():
        raise ValueError(f"not a duration: {text!r}")
    seconds = int(value[:-1]) * DURATION_UNITS[value[-1]]
    if seconds <= 0:
        raise ValueError(f"not a positive duration: {text!r}")
    return seconds


def parse_components(text: str) -> tuple[Component, ...]:
    raw = json.loads(text)
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_COMPONENTS:
        raise ValueError(f"AYE_STATUS_COMPONENTS must list 1 to {MAX_COMPONENTS} components")
    components: list[Component] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError(f"a component must be an object: {item!r}")
        ident = str(item.get("id") or "")
        services = item.get("services")
        if not NAME.match(ident):
            raise ValueError(f"not a component id: {ident!r}")
        if ident in seen:
            raise ValueError(f"component {ident!r} is listed twice")
        if not isinstance(services, list) or not services:
            raise ValueError(f"component {ident!r} names no services")
        seen.add(ident)
        components.append(Component(ident, tuple(str(name) for name in services)))
    return tuple(components)


def read_token(path: Path) -> str:
    try:
        text = path.read_bytes().decode("ascii").strip()
    except (OSError, UnicodeError) as error:
        raise QueryError(f"cannot read the reader token {path}: {type(error).__name__}") from None
    if not TOKEN.fullmatch(text):
        raise QueryError(f"the reader token {path} holds no token")
    return text


def worst(states: Iterable[str]) -> str:
    return max(states, key=RANK.__getitem__, default="operational")


def instance_state(state: str, severity: str) -> str:
    head, _, reason = state.partition(" (")
    if head in UNKNOWN or reason.rstrip(")") in UNKNOWN:
        return "no_data"
    if head in FIRING_INSTANCE:
        return SEVERITY_STATES.get(severity, "operational")
    return "operational"


def rule_state(rule: Mapping[str, Any]) -> str:
    severity = str((rule.get("labels") or {}).get("severity") or "")
    instances = [a for a in rule.get("alerts") or [] if isinstance(a, Mapping)]
    if instances:
        return worst(instance_state(str(a.get("state") or ""), severity) for a in instances)
    if rule.get("health") in UNKNOWN_HEALTH:
        return "no_data"
    if rule.get("state") in FIRING:
        return SEVERITY_STATES.get(severity, "operational")
    return "operational"


def service_states(answer: Any) -> dict[str, str]:
    if not isinstance(answer, Mapping) or answer.get("status") != "success":
        raise QueryError("the rules answer is not a success")
    groups = (answer.get("data") or {}).get("groups")
    if not isinstance(groups, list):
        raise QueryError("the rules answer has no groups")
    states: dict[str, list[str]] = {}
    for group in groups:
        for rule in (group or {}).get("rules") or []:
            if not isinstance(rule, Mapping) or rule.get("type", "alerting") != "alerting":
                continue
            service = (rule.get("labels") or {}).get(SERVICE_LABEL)
            if service:
                states.setdefault(str(service), []).append(rule_state(rule))
    return {service: worst(found) for service, found in states.items()}


def matcher_hits(matcher: Mapping[str, Any], value: str) -> bool:
    pattern = str(matcher.get("value") or "")
    if matcher.get("isRegex"):
        try:
            hit = re.fullmatch(pattern, value) is not None
        except re.error:
            return False
    else:
        hit = pattern == value
    return hit if matcher.get("isEqual", True) else not hit


def silence_covers(silence: Mapping[str, Any], service: str) -> bool:
    if (silence.get("status") or {}).get("state") != "active":
        return False
    matchers = [m for m in silence.get("matchers") or [] if isinstance(m, Mapping)]
    if not any(m.get("name") == MAINTENANCE_LABEL for m in matchers):
        return False
    on_service = [m for m in matchers if m.get("name") == SERVICE_LABEL]
    if not any(m.get("isEqual", True) for m in on_service):
        return False
    return all(matcher_hits(m, service) for m in on_service)


def maintained(answer: Any, services: Iterable[str]) -> frozenset[str]:
    if not isinstance(answer, list):
        raise QueryError("the silences answer is not a list")
    silences = [s for s in answer if isinstance(s, Mapping)]
    return frozenset(
        service for service in services if any(silence_covers(s, service) for s in silences)
    )


def component_state(
    component: Component, states: Mapping[str, str], maintenance: frozenset[str]
) -> str:
    if all(service in maintenance for service in component.services):
        return "maintenance"
    return worst(
        states.get(service, "no_data")
        for service in component.services
        if service not in maintenance
    )


def decide(
    components: Sequence[Component],
    states: Mapping[str, str] | None,
    maintenance: frozenset[str] | None,
) -> list[tuple[str, str]]:
    if states is None or maintenance is None:
        return [(component.id, "no_data") for component in components]
    return [
        (component.id, component_state(component, states, maintenance)) for component in components
    ]


def push_body(tenant: str, ts: int, decided: Sequence[tuple[str, str]]) -> bytes:
    payload = {
        "tenant": tenant,
        "ts": ts,
        "components": [{"id": ident, "state": state} for ident, state in decided],
    }
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sign(body: bytes, secret: str) -> str:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def transient(status: int) -> bool:
    return status in TRANSIENT_CODES or status >= 500


def shorten(text: str) -> str:
    flat = " ".join(str(text).split())
    return flat if len(flat) <= DETAIL_LIMIT else flat[: DETAIL_LIMIT - 1] + "…"


def failure_detail(error: BaseException) -> str:
    if isinstance(error, urllib.error.URLError) and not isinstance(error, urllib.error.HTTPError):
        return shorten(f"{type(error).__name__}: {error.reason}")
    return shorten(f"{type(error).__name__}: {error}")


def labels(**pairs: str) -> tuple[tuple[str, str], ...]:
    return tuple(sorted(pairs.items()))


def escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def format_value(value: float) -> str:
    if value == int(value) and abs(value) < 1e15:
        return str(int(value))
    return repr(float(value))


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


def urlopen_http(request: urllib.request.Request, timeout: float) -> tuple[int, bytes]:
    try:
        with OPENER.open(request, timeout=timeout) as response:
            return int(response.status), response.read(1 << 22)
    except urllib.error.HTTPError as error:
        return int(error.code), error.read(4096)


class Reporter:
    def __init__(self, config: Config, http: Http, clock: Clock, sleep: Sleep) -> None:
        self.config = config
        self.http = http
        self.clock = clock
        self.sleep = sleep
        self.decided: tuple[tuple[str, str], ...] = ()
        self.query_ok: int | None = None
        self.push_ok: int | None = None
        self.last_push = clock()
        self.last_round = clock()
        self.pushes = 0
        self.failures: dict[str, int] = {"transient": 0, "refused": 0}
        self.lock = threading.Lock()

    def get(self, path: str, token: str) -> tuple[int, bytes]:
        request = urllib.request.Request(
            f"{self.config.grafana}{path}",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                "User-Agent": USER_AGENT,
            },
        )
        return self.http(request, self.config.timeout_seconds)

    def grafana(self, path: str) -> Any:
        token = read_token(self.config.token_file)
        status, body = self.get(path, token)
        if status == 401:
            fresh = read_token(self.config.token_file)
            if fresh != token:
                status, body = self.get(path, fresh)
        if status != 200:
            raise QueryError(f"{path} answered HTTP {status}")
        return json.loads(body)

    def read(self) -> tuple[dict[str, str] | None, frozenset[str] | None]:
        services = sorted({s for c in self.config.components for s in c.services})
        try:
            states = service_states(self.grafana(RULES_PATH))
            maintenance = maintained(self.grafana(SILENCES_PATH), services)
        except (QueryError, OSError, ValueError, TypeError, AttributeError) as error:
            if self.query_ok != 0:
                log_json({"event": "query_failed", "error": failure_detail(error)})
            self.query_ok = 0
            return None, None
        if self.query_ok == 0:
            log_json({"event": "query_ok"})
        self.query_ok = 1
        return states, maintenance

    def post(self, body: bytes) -> tuple[int, bytes]:
        request = urllib.request.Request(
            self.config.url,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                SIGNATURE_HEADER: sign(body, self.config.secret),
                "User-Agent": USER_AGENT,
            },
        )
        return self.http(request, self.config.timeout_seconds)

    def push(self, body: bytes, deadline: float) -> Outcome:
        outcome = Outcome(False)
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                status, answer = self.post(body)
            except OSError as error:
                outcome = Outcome(False, "transient", 0, attempt, failure_detail(error))
            else:
                if 200 <= status < 300:
                    return Outcome(True, "", status, attempt)
                detail = shorten(answer.decode("utf-8", "replace"))
                if not transient(status):
                    return Outcome(False, "refused", status, attempt, detail)
                outcome = Outcome(False, "transient", status, attempt, detail)
            if attempt == MAX_ATTEMPTS:
                break
            pause = BACKOFF_SECONDS * (1 << (attempt - 1))
            if self.clock() + pause + self.config.timeout_seconds > deadline:
                break
            self.sleep(pause)
        return outcome

    def tick(self) -> Outcome:
        started = self.clock()
        states, maintenance = self.read()
        decided = tuple(decide(self.config.components, states, maintenance))
        if decided != self.decided:
            log_json({"event": "states", "components": dict(decided)})
        body = push_body(self.config.tenant, int(self.clock()), decided)
        outcome = self.push(body, started + self.config.interval_seconds)
        now = self.clock()
        if outcome.ok:
            if self.push_ok == 0:
                log_json({"event": "push_ok", "attempts": outcome.attempts})
        else:
            log_json(
                {
                    "event": "push_failed",
                    "reason": outcome.reason,
                    "status": outcome.status,
                    "attempts": outcome.attempts,
                    "detail": outcome.detail,
                }
            )
        with self.lock:
            self.decided = decided
            self.push_ok = int(outcome.ok)
            if outcome.ok:
                self.pushes += 1
                self.last_push = now
            else:
                self.failures[outcome.reason] = self.failures.get(outcome.reason, 0) + 1
            self.last_round = now
        return outcome

    def samples(self) -> list[Sample]:
        with self.lock:
            samples: list[Sample] = [
                ("ayeops_status_last_push_timestamp_seconds", (), self.last_push),
                ("ayeops_status_last_round_timestamp_seconds", (), self.last_round),
                ("ayeops_status_pushes_total", (), self.pushes),
            ]
            for reason, count in self.failures.items():
                samples.append(("ayeops_status_push_failures_total", labels(reason=reason), count))
            if self.push_ok is not None:
                samples.append(("ayeops_status_push_success", (), self.push_ok))
            if self.query_ok is not None:
                samples.append(("ayeops_status_query_success", (), self.query_ok))
            for ident, state in self.decided:
                pair = labels(component=ident, state=state)
                samples.append(("ayeops_status_component", pair, 1))
        return samples

    def metrics(self) -> str:
        return render(self.samples())

    def healthy(self, now: float) -> bool:
        with self.lock:
            last_round = self.last_round
        return now - last_round <= 3 * self.config.interval_seconds + self.round_budget()

    def round_budget(self) -> float:
        requests = self.config.timeout_seconds * (4 + MAX_ATTEMPTS)
        return requests + BACKOFF_SECONDS * (1 << MAX_ATTEMPTS)


def config_from(env: Mapping[str, str]) -> Config:
    url = env["AYE_STATUS_URL"].strip()
    if not url.startswith("https://"):
        raise ValueError("AYE_STATUS_URL must be an https:// URL")
    tenant = env["AYE_STATUS_TENANT"].strip()
    if not NAME.match(tenant):
        raise ValueError(f"not a tenant: {tenant!r}")
    secret = env["AYE_STATUS_PUSH_SECRET"]
    if not secret:
        raise ValueError("AYE_STATUS_PUSH_SECRET is empty")
    return Config(
        tenant=tenant,
        url=url,
        secret=secret,
        grafana=env.get("AYE_STATUS_GRAFANA", "http://grafana:3000").rstrip("/"),
        token_file=Path(env["AYE_GRAFANA_READER_TOKEN_FILE"]),
        components=parse_components(env["AYE_STATUS_COMPONENTS"]),
        interval_seconds=parse_duration(env.get("AYE_STATUS_INTERVAL", "1m")),
        timeout_seconds=parse_duration(env.get("AYE_STATUS_TIMEOUT", "10s")),
    )


def make_server(
    reporter: Reporter, port: int, host: str = "", now: Clock = time.time
) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path == "/health":
                if not reporter.healthy(now()):
                    self.send_error(503)
                    return
                body = b"ok\n"
            elif self.path == "/metrics":
                body = reporter.metrics().encode()
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


def one_round(reporter: Reporter, monotonic: Clock = time.monotonic) -> float:
    started = monotonic()
    try:
        reporter.tick()
    except Exception as error:
        log_json({"event": "round_failed", "error": failure_detail(error)})
    elapsed = monotonic() - started
    return max(1.0, reporter.config.interval_seconds - elapsed)


def run_forever(reporter: Reporter) -> None:
    while True:
        time.sleep(one_round(reporter))


def main() -> None:
    env = os.environ
    config = config_from(env)
    reporter = Reporter(config, urlopen_http, time.time, time.sleep)
    log_json(
        {
            "event": "start",
            "tenant": config.tenant,
            "url": config.url,
            "components": [component.id for component in config.components],
            "interval_seconds": config.interval_seconds,
        }
    )
    threading.Thread(target=run_forever, args=(reporter,), daemon=True).start()
    make_server(reporter, int(env.get("AYE_STATUS_PORT", "9136"))).serve_forever()


if __name__ == "__main__":
    main()
