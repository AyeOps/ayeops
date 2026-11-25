from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, tzinfo
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

SCHEMA = 1
SOURCE = "grafana-annotations"
WEIGHTS = {"critical": 100.0, "high": 10.0, "normal": 1.0, "low": 0.1}
DEFAULT_CRITICALITY = "normal"
BUCKETS = (60.0, 300.0, 900.0, 1800.0, 3600.0, 7200.0, 14400.0, 43200.0, 86400.0)
PAGE_LIMIT = 1000
MAX_PAGES = 50
CHUNK_MS = 6 * 3600 * 1000
OVERLAP_MS = 5 * 60 * 1000
MAX_CHUNKS = 500
TIMELINE_LIMIT = 50
STEPS = ("read", "journal")
ACK_STEP = "acks"
PAGED = frozenset({"critical", "page"})
ACK_BUCKETS = (30.0, 60.0, 120.0, 300.0, 600.0, 900.0, 1800.0, 3600.0, 7200.0, 14400.0, 43200.0)
ACK_EARLY = 5 * 60.0
ACK_LATE = 60.0
ACK_CREATE_SPAN = 6 * 3600.0
ACK_WINDOW = 7 * 86400.0
MATCH_KEYS = ("host", "service")
LABEL_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
ROUTE = re.compile(r"^[a-z][a-z0-9_-]*@[a-z]+$")
DAY = 86400
WEEKS = 4
SUCCESS_CODES = "[123].."
TOKEN = re.compile(r"[\x21-\x7e]+")
USER_AGENT = "ayeops-incidents"

Fetch = Callable[[str, float], tuple[int, bytes]]
Clock = Callable[[], float]
Sample = tuple[str, tuple[tuple[str, str], ...], float]


class ReadError(Exception):
    pass


class TokenError(ReadError):
    pass


@dataclass(frozen=True)
class Routes:
    host: str
    routes: tuple[str, ...]
    scrape_seconds: float


@dataclass(frozen=True)
class HostAlert:
    title: str
    labels: tuple[tuple[str, str], ...] = ()

    def matches(self, alert: str, labels: Mapping[str, str]) -> bool:
        return alert == self.title and all(labels.get(key) == value for key, value in self.labels)


@dataclass(frozen=True)
class Night:
    start: int
    end: int
    zone: tzinfo

    def contains(self, moment: float) -> bool:
        local = datetime.fromtimestamp(moment, self.zone)
        minute = local.hour * 60 + local.minute
        if self.start == self.end:
            return False
        if self.start < self.end:
            return self.start <= minute < self.end
        return minute >= self.start or minute < self.end


@dataclass(frozen=True)
class Config:
    grafana: str
    token_file: Path
    services: Mapping[str, str]
    night: Night
    downtime_severities: frozenset[str]
    state_dir: Path
    degraded_severities: frozenset[str] = frozenset({"warning"})
    hosts: Mapping[str, Sequence[str]] = field(default_factory=dict)
    host_modules: Mapping[str, Sequence[str]] = field(default_factory=dict)
    host_alerts: tuple[HostAlert, ...] = ()
    victoriametrics: str = ""
    routes: Mapping[str, Sequence[Routes]] = field(default_factory=dict)
    refresh_seconds: float = 60
    backfill_days: float = 30
    timeout_seconds: float = 20
    oncall: str = ""


@dataclass(frozen=True)
class Transition:
    id: int
    time_ms: int
    rule_id: int
    title: str
    labels: Mapping[str, str]
    previous: str
    current: str

    @property
    def fingerprint(self) -> str:
        return fingerprint(self.rule_id, self.labels)


@dataclass
class Incident:
    fingerprint: str
    alert: str
    rule_id: int
    labels: dict[str, str]
    start: float
    end: float | None = None
    timeline: list[dict[str, str]] = field(default_factory=list)
    ack: dict[str, Any] | None = None

    @property
    def service(self) -> str:
        return service_of(self.labels, self.alert)

    @property
    def host(self) -> str:
        return self.labels.get("host", "")

    @property
    def severity(self) -> str:
        return self.labels.get("severity", "")

    def to_state(self) -> dict[str, Any]:
        return {
            "fingerprint": self.fingerprint,
            "alert": self.alert,
            "rule_id": self.rule_id,
            "labels": dict(self.labels),
            "start": self.start,
            "timeline": list(self.timeline),
            "ack": dict(self.ack) if self.ack is not None else None,
        }

    @classmethod
    def from_state(cls, data: Mapping[str, Any]) -> Incident:
        ack = data.get("ack")
        return cls(
            fingerprint=str(data["fingerprint"]),
            alert=str(data.get("alert") or ""),
            rule_id=int(data.get("rule_id") or 0),
            labels={str(k): str(v) for k, v in (data.get("labels") or {}).items()},
            start=float(data["start"]),
            timeline=[dict(step) for step in data.get("timeline") or []],
            ack=dict(ack) if isinstance(ack, Mapping) else None,
        )


@dataclass
class Tracker:
    cursor_ms: int
    recent: dict[int, int] = field(default_factory=dict)
    open: dict[str, Incident] = field(default_factory=dict)
    peaks: dict[str, float] = field(default_factory=dict)

    def to_state(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "cursor_ms": self.cursor_ms,
            "recent": {str(key): value for key, value in sorted(self.recent.items())},
            "open": {key: incident.to_state() for key, incident in sorted(self.open.items())},
            "peaks": dict(sorted(self.peaks.items())),
        }

    @classmethod
    def from_state(cls, data: Mapping[str, Any]) -> Tracker:
        return cls(
            cursor_ms=int(data["cursor_ms"]),
            recent={int(key): int(value) for key, value in (data.get("recent") or {}).items()},
            open={
                str(key): Incident.from_state(value)
                for key, value in (data.get("open") or {}).items()
            },
            peaks={str(key): float(value) for key, value in (data.get("peaks") or {}).items()},
        )

    def apply(self, transition: Transition) -> Incident | None:
        key = transition.fingerprint
        incident = self.open.get(key)
        step = {
            "time": iso(transition.time_ms / 1000),
            "from": transition.previous,
            "to": transition.current,
        }
        if is_firing(transition.current):
            if incident is None:
                self.open[key] = Incident(
                    fingerprint=key,
                    alert=transition.title,
                    rule_id=transition.rule_id,
                    labels=dict(transition.labels),
                    start=transition.time_ms / 1000,
                    timeline=[step],
                )
            elif len(incident.timeline) < TIMELINE_LIMIT:
                incident.timeline.append(step)
            return None
        if incident is None:
            return None
        if len(incident.timeline) < TIMELINE_LIMIT:
            incident.timeline.append(step)
        if not is_normal(transition.current):
            return None
        del self.open[key]
        incident.end = max(incident.start, transition.time_ms / 1000)
        return incident

    def take(self, transitions: Iterable[Transition], window_end_ms: int) -> list[Incident]:
        closed = []
        for transition in sorted(transitions, key=lambda item: (item.time_ms, item.id)):
            if transition.id in self.recent:
                continue
            self.recent[transition.id] = transition.time_ms
            done = self.apply(transition)
            if done is not None:
                closed.append(done)
        self.cursor_ms = max(self.cursor_ms, window_end_ms)
        floor = self.cursor_ms - 2 * OVERLAP_MS
        self.recent = {key: value for key, value in self.recent.items() if value >= floor}
        return closed


def is_firing(state: str) -> bool:
    return state.startswith("Alerting")


def is_normal(state: str) -> bool:
    return state.startswith("Normal")


def iso(moment: float) -> str:
    return datetime.fromtimestamp(moment, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def fingerprint(rule_id: int, labels: Mapping[str, str]) -> str:
    text = json.dumps([rule_id, sorted(labels.items())], ensure_ascii=False)
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def service_of(labels: Mapping[str, str], alert: str) -> str:
    return labels.get("service") or labels.get("module") or labels.get("alertname") or alert


def split_labels(body: str) -> dict[str, str]:
    labels: dict[str, str] = {}
    key: str | None = None
    for part in body.split(", "):
        if key is None or LABEL_KEY.match(part):
            key, _, value = part.partition("=")
            labels[key] = value
        else:
            labels[key] += ", " + part
    return labels


def labels_from_text(text: str, title: str) -> dict[str, str] | None:
    marker = text.rfind("} - ")
    if marker < 0:
        return None
    if title and text.startswith(title + " {"):
        opening = len(title)
    else:
        opening = text.find(" {")
        if opening < 0 or opening > marker:
            return None
    body = text[opening + 1 : marker]
    if not body.startswith("{"):
        return None
    return split_labels(body[1:]) if len(body) > 1 else {}


def labels_from_tags(tags: Sequence[Any]) -> dict[str, str]:
    labels = {}
    for tag in tags:
        key, sep, value = str(tag).partition(":")
        if sep:
            labels[key] = value
    return labels


def transition_from(item: Mapping[str, Any]) -> Transition | None:
    try:
        rule_id = int(item.get("alertId") or 0)
        ident = int(item["id"])
        moment = int(item["time"])
    except (KeyError, TypeError, ValueError):
        return None
    current = str(item.get("newState") or "")
    if rule_id <= 0 or not current:
        return None
    title = str(item.get("alertName") or "")
    labels = labels_from_text(str(item.get("text") or ""), title)
    if labels is None:
        labels = labels_from_tags(item.get("tags") or [])
    if not title:
        title = labels.get("alertname", "")
    return Transition(
        id=ident,
        time_ms=moment,
        rule_id=rule_id,
        title=title,
        labels=labels,
        previous=str(item.get("prevState") or ""),
        current=current,
    )


def annotations_url(base: str, start_ms: int, end_ms: int, limit: int) -> str:
    query = urllib.parse.urlencode(
        {"type": "alert", "from": start_ms, "to": end_ms, "limit": limit}
    )
    return f"{base.rstrip('/')}/api/annotations?{query}"


def read_window(
    fetch: Fetch,
    base: str,
    start_ms: int,
    end_ms: int,
    timeout: float,
    limit: int = PAGE_LIMIT,
    max_pages: int = MAX_PAGES,
) -> list[Transition]:
    items: dict[int, Mapping[str, Any]] = {}
    upper = end_ms
    for _ in range(max_pages):
        status, body = fetch(annotations_url(base, start_ms, upper, limit), timeout)
        if status != 200:
            raise ReadError(f"HTTP {status}")
        try:
            batch = json.loads(body)
        except ValueError as error:
            raise ReadError("answer is not JSON") from error
        if not isinstance(batch, list):
            raise ReadError("answer is not a list")
        for item in batch:
            if isinstance(item, Mapping) and "id" in item:
                items[int(item["id"])] = item
        if len(batch) < limit:
            break
        oldest = min(int(item.get("time") or 0) for item in batch if isinstance(item, Mapping))
        if oldest >= upper:
            raise ReadError(f"more than {limit} annotations share one millisecond")
        upper = oldest
    else:
        raise ReadError(f"more than {max_pages} pages in one window")
    found = (transition_from(item) for item in items.values())
    return [item for item in found if item is not None and start_ms <= item.time_ms <= end_ms]


def host_wide(alert: str, labels: Mapping[str, str], config: Config) -> bool:
    return any(entry.matches(alert, labels) for entry in config.host_alerts)


def affected(
    service: str, host: str, alert: str, labels: Mapping[str, str], config: Config
) -> tuple[list[str], list[str]]:
    if not host_wide(alert, labels, config):
        return [service], []
    services = [service, *(name for name in config.hosts.get(host, ()) if name != service)]
    modules = [name for name in config.host_modules.get(host, ()) if name not in services]
    return services, modules


def incident_affected(incident: Incident, config: Config) -> tuple[list[str], list[str]]:
    return affected(incident.service, incident.host, incident.alert, incident.labels, config)


def record_affected(record: Mapping[str, Any], config: Config) -> tuple[list[str], list[str]]:
    services = record.get("services")
    modules = record.get("modules")
    if isinstance(services, list) and services and isinstance(modules, list):
        return [str(name) for name in services], [str(name) for name in modules]
    labels = record.get("labels")
    return affected(
        str(record.get("service") or ""),
        str(record.get("host") or ""),
        str(record.get("alert") or ""),
        {str(k): str(v) for k, v in labels.items()} if isinstance(labels, Mapping) else {},
        config,
    )


def acks_url(base: str, start: float, end: float) -> str:
    query = urllib.parse.urlencode({"from": format_value(start), "to": format_value(end)})
    return f"{base.rstrip('/')}/acks?{query}"


def optional_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def optional_text(value: Any) -> str | None:
    return str(value) if isinstance(value, str) and value else None


def parse_acks(body: bytes) -> list[dict[str, Any]]:
    try:
        answer = json.loads(body)
    except ValueError as error:
        raise ReadError("answer is not JSON") from error
    items = answer.get("alerts") if isinstance(answer, dict) else None
    if not isinstance(items, list):
        raise ReadError("answer has no alerts")
    found = []
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("labels"), dict):
            continue
        created = optional_float(item.get("created_at"))
        if created is None or not isinstance(item.get("alert_id"), int):
            continue
        found.append(
            {
                "alert_id": item["alert_id"],
                "created_at": created,
                "labels": {str(k): str(v) for k, v in item["labels"].items()},
                "at": optional_float(item.get("acknowledged_at")),
                "by": optional_text(item.get("acknowledged_by")),
                "via": optional_text(item.get("acknowledged_via")),
            }
        )
    return found


def read_acks(fetch: Fetch, base: str, start: float, end: float, timeout: float) -> list[Any]:
    status, body = fetch(acks_url(base, start, end), timeout)
    if status != 200:
        raise ReadError(f"HTTP {status}")
    return parse_acks(body)


def ack_span(incident: Incident, upto: float) -> tuple[float, float]:
    end = incident.end if incident.end is not None else upto
    last = min(max(end, incident.start), incident.start + ACK_CREATE_SPAN)
    return incident.start - ACK_EARLY, last + ACK_LATE


def ack_windows(incidents: Sequence[Incident], upto: float) -> list[tuple[float, float]]:
    windows: list[tuple[float, float]] = []
    for start, end in sorted(ack_span(incident, upto) for incident in incidents):
        if windows and end - windows[-1][0] <= ACK_WINDOW:
            windows[-1] = (windows[-1][0], max(windows[-1][1], end))
        else:
            windows.append((start, end))
    return windows


def goalert_match(
    incident: Incident, alerts: Sequence[Mapping[str, Any]], upto: float
) -> Mapping[str, Any] | None:
    first, last = ack_span(incident, upto)
    names = {incident.alert, incident.labels.get("alertname", "")} - {""}
    best: Mapping[str, Any] | None = None
    gap = float("inf")
    for item in alerts:
        theirs = item["labels"]
        if theirs.get("alertname") not in names:
            continue
        if any(key in theirs and theirs[key] != incident.labels.get(key, "") for key in MATCH_KEYS):
            continue
        created = float(item["created_at"])
        distance = abs(created - incident.start)
        if first <= created <= last and distance < gap:
            best, gap = item, distance
    return best


def ack_fields(incident: Incident, config: Config) -> dict[str, Any]:
    ack = incident.ack if config.oncall and incident.severity in PAGED else None
    at = optional_float(ack.get("at")) if ack is not None else None
    return {
        "acknowledged": None if ack is None else at is not None,
        "acknowledged_at": iso(at) if at is not None else None,
        "acknowledged_ts": at,
        "acknowledged_by": ack.get("by") if ack is not None and at is not None else None,
        "acknowledged_via": ack.get("via") if ack is not None and at is not None else None,
        "acknowledge_seconds": round(max(0.0, at - incident.start), 3) if at is not None else None,
        "goalert_alert": ack.get("alert_id") if ack is not None else None,
    }


def record_of(incident: Incident, config: Config) -> dict[str, Any]:
    end = incident.end if incident.end is not None else incident.start
    level = config.services.get(incident.service, DEFAULT_CRITICALITY)
    services, modules = incident_affected(incident, config)
    return {
        "schema": SCHEMA,
        "id": f"{incident.fingerprint}-{int(incident.start * 1000)}",
        "fingerprint": incident.fingerprint,
        "alert": incident.alert,
        "rule_id": incident.rule_id,
        "service": incident.service,
        "services": services,
        "modules": modules,
        "host": incident.host,
        "severity": incident.severity,
        "criticality": level,
        "weight": WEIGHTS.get(level, 1.0),
        "start": iso(incident.start),
        "end": iso(end),
        "start_ts": incident.start,
        "end_ts": end,
        "duration_seconds": round(end - incident.start, 3),
        "downtime": incident.severity in config.downtime_severities,
        "degraded": incident.severity in config.degraded_severities,
        "night": config.night.contains(incident.start),
        **ack_fields(incident, config),
        "lost_requests": None,
        "requests": {},
        "labels": dict(sorted(incident.labels.items())),
        "timeline": list(incident.timeline),
        "source": SOURCE,
    }


def median(values: Sequence[float]) -> float:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def requests_query(wanted: Mapping[str, Sequence[Routes]], seconds: int) -> str:
    parts = []
    for service, entries in sorted(wanted.items()):
        for entry in entries:
            names = "|".join(entry.routes)
            selector = (
                f"traefik_service_requests_total{{host={json.dumps(entry.host)},"
                f'service=~"{names}",code=~"{SUCCESS_CODES}"}}'
            )
            for week in range(WEEKS + 1):
                shift = f" offset {week}w" if week else ""
                parts.append(
                    f"label_set(sum(increase({selector}[{seconds}s]{shift})), "
                    f'"aye_week", "{week}", "aye_service", {json.dumps(service)}, '
                    f'"aye_host", {json.dumps(entry.host)})'
                )
    return " or ".join(parts)


def query_url(base: str, query: str, moment: float) -> str:
    encoded = urllib.parse.urlencode({"query": query, "time": format_value(round(moment, 3))})
    return f"{base.rstrip('/')}/api/v1/query?{encoded}"


def parse_vector(body: bytes) -> list[tuple[dict[str, str], float]]:
    try:
        answer = json.loads(body)
    except ValueError as error:
        raise ReadError("answer is not JSON") from error
    if not isinstance(answer, dict) or answer.get("status") != "success":
        raise ReadError("query did not succeed")
    found = []
    for item in (answer.get("data") or {}).get("result") or []:
        try:
            labels = {str(k): str(v) for k, v in item["metric"].items()}
            found.append((labels, float(item["value"][1])))
        except (KeyError, TypeError, ValueError, IndexError, AttributeError):
            continue
    return found


def lost_of(
    services: Sequence[str],
    start: float,
    end: float,
    config: Config,
    query: Fetch,
) -> dict[str, dict[str, Any]]:
    wanted = {name: config.routes[name] for name in services if config.routes.get(name)}
    if not wanted:
        return {}
    duration = end - start
    result: dict[str, dict[str, Any]] = {}
    measured: dict[str, Sequence[Routes]] = {}
    for name, entries in wanted.items():
        if duration < max(entry.scrape_seconds for entry in entries):
            result[name] = {"lost": None, "reason": "shorter than one scrape interval"}
        else:
            measured[name] = entries
    if not measured:
        return result
    url = query_url(config.victoriametrics, requests_query(measured, round(duration)), end)
    try:
        status, body = query(url, config.timeout_seconds)
        if status != 200:
            raise ReadError(f"HTTP {status}")
        vector = parse_vector(body)
    except Exception as error:
        log_json({"event": "requests_failed", "error": f"{type(error).__name__}: {error}"})
        for name in measured:
            result[name] = {"lost": None, "reason": "query failed"}
        return result
    weeks: dict[str, dict[int, float]] = {name: {} for name in measured}
    for labels, value in vector:
        name = labels.get("aye_service", "")
        if name not in weeks:
            continue
        week = int(labels.get("aye_week") or 0)
        weeks[name][week] = weeks[name].get(week, 0.0) + value
    for name, values in weeks.items():
        baseline = [values[week] for week in range(1, WEEKS + 1) if week in values]
        if not baseline:
            result[name] = {"lost": None, "reason": "no baseline"}
            continue
        served = values.get(0, 0.0)
        expected = median(baseline)
        result[name] = {
            "served": round(served, 1),
            "expected": round(expected, 1),
            "lost": max(0, round(expected - served)),
            "weeks": len(baseline),
        }
    return result


def total_lost(requests: Mapping[str, Mapping[str, Any]]) -> int | None:
    found = [entry["lost"] for entry in requests.values() if entry.get("lost") is not None]
    return int(sum(found)) if found else None


def lost_samples(records: Sequence[Mapping[str, Any]], config: Config) -> dict[str, float]:
    spans: dict[str, list[tuple[float, float, float]]] = {name: [] for name in config.routes}
    for record in records:
        start = float(record.get("start_ts") or 0)
        end = float(record.get("end_ts") or 0)
        requests = record.get("requests")
        if not isinstance(requests, Mapping) or end <= start:
            continue
        for name, entry in requests.items():
            if isinstance(entry, Mapping) and entry.get("lost") is not None:
                spans.setdefault(str(name), []).append((start, end, float(entry["lost"])))
    totals = {}
    for name, items in spans.items():
        total = 0.0
        reach = float("-inf")
        for start, end, lost in sorted(items):
            fresh = max(0.0, end - max(start, reach))
            total += lost * fresh / (end - start)
            reach = max(reach, end)
        totals[name] = total
    return totals


class Journal:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        records = []
        with self.path.open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    log_json({"event": "journal_line_skipped"})
                    continue
                if isinstance(record, dict):
                    records.append(record)
        return records

    def append(self, records: Sequence[Mapping[str, Any]]) -> None:
        if not records:
            return
        text = "".join(
            json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n" for record in records
        )
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())


def save_json(path: Path, data: Mapping[str, Any]) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")
    temporary.replace(path)


def union_seconds(intervals: Iterable[tuple[float, float]]) -> float:
    total = 0.0
    current: tuple[float, float] | None = None
    for start, end in sorted(intervals):
        if current is None or start > current[1]:
            if current is not None:
                total += current[1] - current[0]
            current = (start, end)
        else:
            current = (current[0], max(current[1], end))
    if current is not None:
        total += current[1] - current[0]
    return total


def label_pairs(**pairs: str) -> tuple[tuple[str, str], ...]:
    return tuple(sorted(pairs.items()))


def ack_samples(records: Sequence[Mapping[str, Any]], config: Config) -> list[Sample]:
    waits: dict[tuple[str, str], list[float]] = {}
    missed: dict[tuple[str, str], int] = {}
    for record in records:
        service = str(record.get("service") or "")
        key = (
            config.services.get(service, DEFAULT_CRITICALITY),
            "night" if record.get("night") else "day",
        )
        seconds = optional_float(record.get("acknowledge_seconds"))
        if seconds is not None:
            waits.setdefault(key, []).append(max(0.0, seconds))
        elif record.get("acknowledged") is False:
            missed[key] = missed.get(key, 0) + 1
    samples: list[Sample] = []
    for (level, period), values in sorted(waits.items()):
        for bound in ACK_BUCKETS:
            pair = label_pairs(criticality=level, period=period, le=format_value(bound))
            hits = sum(1 for value in values if value <= bound)
            samples.append(("ayeops_incident_acknowledge_seconds_bucket", pair, hits))
        pair = label_pairs(criticality=level, period=period, le="+Inf")
        samples.append(("ayeops_incident_acknowledge_seconds_bucket", pair, len(values)))
        pair = label_pairs(criticality=level, period=period)
        samples.append(("ayeops_incident_acknowledge_seconds_sum", pair, sum(values)))
        samples.append(("ayeops_incident_acknowledge_seconds_count", pair, len(values)))
    for (level, period), count in sorted(missed.items()):
        pair = label_pairs(criticality=level, period=period)
        samples.append(("ayeops_incidents_unacknowledged_total", pair, count))
    return samples


def incident_samples(
    records: Sequence[Mapping[str, Any]],
    open_now: Sequence[Incident],
    config: Config,
    upto: float,
) -> list[Sample]:
    total: dict[tuple[str, str, str], int] = {}
    night: dict[tuple[str, str], int] = {}
    restore: dict[tuple[str, str], list[float]] = {}
    down: dict[str, list[tuple[float, float]]] = {}
    degraded: dict[str, list[tuple[float, float]]] = {}
    touched: dict[tuple[str, str], list[tuple[float, float]]] = {}
    opened: dict[tuple[str, str, str], int] = {}
    rows = [
        (
            str(record.get("service") or ""),
            str(record.get("severity") or ""),
            float(record.get("start_ts") or 0),
            float(record.get("end_ts") or 0),
            bool(record.get("night")),
            True,
            *record_affected(record, config),
        )
        for record in records
    ]
    for incident in open_now:
        rows.append(
            (
                incident.service,
                incident.severity,
                incident.start,
                max(incident.start, upto),
                config.night.contains(incident.start),
                False,
                *incident_affected(incident, config),
            )
        )
        key = (incident.service, incident.severity, incident.host)
        opened[key] = opened.get(key, 0) + 1
    for service, severity, start, end, at_night, closed, hit, modules in rows:
        level = config.services.get(service, DEFAULT_CRITICALITY)
        total[(service, severity, level)] = total.get((service, severity, level), 0) + 1
        if at_night:
            night[(service, severity)] = night.get((service, severity), 0) + 1
        if closed:
            restore.setdefault((service, severity), []).append(max(0.0, end - start))
        kinds = []
        if severity in config.downtime_severities:
            kinds.append("downtime")
            for name in hit:
                down.setdefault(name, []).append((start, end))
        if severity in config.degraded_severities:
            kinds.append("degraded")
            for name in hit:
                degraded.setdefault(name, []).append((start, end))
        for kind in kinds:
            for name in modules:
                touched.setdefault((name, kind), []).append((start, end))
    samples: list[Sample] = []
    for (service, severity, level), count in sorted(total.items()):
        pair = label_pairs(service=service, severity=severity, criticality=level)
        samples.append(("ayeops_incidents_total", pair, count))
    for (service, severity), count in sorted(night.items()):
        samples.append(
            ("ayeops_incidents_night_total", label_pairs(service=service, severity=severity), count)
        )
    for (service, severity, host), count in sorted(opened.items()):
        pair = label_pairs(service=service, severity=severity, alert_host=host)
        samples.append(("ayeops_incidents_open", pair, count))
    for (service, severity), durations in sorted(restore.items()):
        for bound in BUCKETS:
            pair = label_pairs(service=service, severity=severity, le=format_value(bound))
            hits = sum(1 for value in durations if value <= bound)
            samples.append(("ayeops_incident_restore_seconds_bucket", pair, hits))
        pair = label_pairs(service=service, severity=severity, le="+Inf")
        samples.append(("ayeops_incident_restore_seconds_bucket", pair, len(durations)))
        pair = label_pairs(service=service, severity=severity)
        samples.append(("ayeops_incident_restore_seconds_sum", pair, sum(durations)))
        samples.append(("ayeops_incident_restore_seconds_count", pair, len(durations)))
    samples += ack_samples(records, config)
    lost = lost_samples(records, config)
    seen = set(config.services) | {service for service, _, _ in total} | set(down) | set(degraded)
    seen |= set(lost)
    for service in sorted(seen):
        level = config.services.get(service, DEFAULT_CRITICALITY)
        pair = label_pairs(service=service, criticality=level)
        if service in lost:
            samples.append(("ayeops_incident_lost_requests_total", pair, round(lost[service], 1)))
        minutes = union_seconds(down.get(service, [])) / 60
        samples.append(("ayeops_incident_downtime_minutes_total", pair, round(minutes, 3)))
        slow = union_seconds(degraded.get(service, [])) / 60
        samples.append(("ayeops_incident_degraded_minutes_total", pair, round(slow, 3)))
        samples.append(("ayeops_service_criticality_weight", pair, WEIGHTS.get(level, 1.0)))
    for (module, kind), spans in sorted(touched.items()):
        minutes = union_seconds(spans) / 60
        pair = label_pairs(module=module, kind=kind)
        samples.append(("ayeops_incident_affected_module_minutes_total", pair, round(minutes, 3)))
    return samples


def escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def format_value(value: float) -> str:
    if value == int(value) and abs(value) < 1e15:
        return str(int(value))
    return repr(float(value))


COUNTERS = frozenset(
    {
        "ayeops_incidents_total",
        "ayeops_incidents_night_total",
        "ayeops_incident_downtime_minutes_total",
        "ayeops_incident_degraded_minutes_total",
        "ayeops_incident_lost_requests_total",
        "ayeops_incident_affected_module_minutes_total",
        "ayeops_incidents_request_queries_total",
        "ayeops_incidents_transitions_total",
        "ayeops_incidents_unacknowledged_total",
        "ayeops_incidents_ack_reads_total",
    }
)
HISTOGRAMS = {"ayeops_incident_restore_seconds", "ayeops_incident_acknowledge_seconds"}
PEAKED = {
    "ayeops_incident_downtime_minutes_total": "",
    "ayeops_incident_degraded_minutes_total": "degraded/",
    "ayeops_incident_lost_requests_total": "lost/",
    "ayeops_incident_affected_module_minutes_total": "affected/",
}


def peak_key(name: str, pairs: tuple[tuple[str, str], ...]) -> str:
    labels = dict(pairs)
    if "module" in labels:
        return f"{PEAKED[name]}{labels.get('kind', '')}/{labels['module']}"
    return PEAKED[name] + labels.get("service", "")


def metric_type(name: str) -> tuple[str, str]:
    for base in HISTOGRAMS:
        if name.startswith(base + "_"):
            return base, "histogram"
    return name, "counter" if name in COUNTERS else "gauge"


def render(samples: Iterable[Sample]) -> str:
    grouped: dict[str, list[tuple[str, tuple[tuple[str, str], ...], float]]] = {}
    kinds: dict[str, str] = {}
    for name, pairs, value in samples:
        family, kind = metric_type(name)
        kinds[family] = kind
        grouped.setdefault(family, []).append((name, pairs, value))
    lines: list[str] = []
    for family in sorted(grouped):
        lines.append(f"# TYPE {family} {kinds[family]}")
        for name, pairs, value in grouped[family]:
            body = ",".join(f'{key}="{escape(text)}"' for key, text in pairs)
            text = format_value(value)
            lines.append(f"{name}{{{body}}} {text}" if body else f"{name} {text}")
    return "\n".join(lines) + "\n"


def log_json(record: Mapping[str, Any]) -> None:
    print(json.dumps(record, sort_keys=True, ensure_ascii=False), flush=True)


class Agent:
    def __init__(
        self,
        config: Config,
        fetch: Fetch,
        clock: Clock,
        query: Fetch | None = None,
        acks: Fetch | None = None,
    ) -> None:
        self.config = config
        self.fetch = fetch
        self.query = query
        self.acks = acks if config.oncall else None
        self.clock = clock
        self.queries = {"ok": 0, "error": 0}
        self.ack_reads = {"ok": 0, "error": 0}
        self.steps = (*STEPS, ACK_STEP) if self.acks is not None else STEPS
        self.journal = Journal(config.state_dir / "incidents.jsonl")
        self.state_path = config.state_dir / "state.json"
        self.records = self.journal.load()
        self.tracker = self.load_tracker()
        self.peaks = dict(self.tracker.peaks)
        self.success = dict.fromkeys(self.steps, 1)
        self.transitions = 0
        self.last_round = clock()
        self.last_sync = clock()
        self.lock = threading.Lock()

    def load_tracker(self) -> Tracker:
        if self.state_path.exists():
            try:
                return Tracker.from_state(json.loads(self.state_path.read_text("utf-8")))
            except (ValueError, KeyError, TypeError):
                log_json({"event": "state_unreadable"})
        start = self.clock() - self.config.backfill_days * DAY
        return Tracker(cursor_ms=int(start * 1000))

    def tick(self) -> int:
        now_ms = int(self.clock() * 1000)
        closed_total = 0
        for _ in range(MAX_CHUNKS):
            tracker = copy.deepcopy(self.tracker)
            start_ms = max(0, tracker.cursor_ms - OVERLAP_MS)
            end_ms = min(now_ms, tracker.cursor_ms + CHUNK_MS)
            try:
                found = read_window(
                    self.fetch, self.config.grafana, start_ms, end_ms, self.config.timeout_seconds
                )
            except Exception as error:
                self.mark("read", False, error)
                raise
            self.mark("read", True)
            closed = tracker.take(found, end_ms)
            latest = end_ms >= now_ms
            self.acknowledge([*closed, *(tracker.open.values() if latest else ())], end_ms / 1000)
            with self.lock:
                tracker.peaks = dict(self.peaks)
            records = [self.measured(record_of(incident, self.config)) for incident in closed]
            try:
                self.journal.append(records)
                save_json(self.state_path, tracker.to_state())
            except OSError as error:
                self.mark("journal", False, error)
                raise
            self.mark("journal", True)
            for record in records:
                log_json({"event": "incident_closed", **{k: record[k] for k in LOG_KEYS}})
            with self.lock:
                self.tracker = tracker
                self.records.extend(records)
                self.transitions += len(found)
                self.last_sync = self.clock()
            closed_total += len(closed)
            if end_ms >= now_ms:
                break
        return closed_total

    def acknowledge(self, incidents: Sequence[Incident], upto: float) -> None:
        if self.acks is None:
            return
        wanted = [
            incident
            for incident in incidents
            if incident.severity in PAGED and (incident.ack or {}).get("at") is None
        ]
        if not wanted:
            return
        alerts: list[Any] = []
        try:
            for start, end in ack_windows(wanted, upto):
                alerts += read_acks(
                    self.acks, self.config.oncall, start, end, self.config.timeout_seconds
                )
        except Exception as error:
            self.mark(ACK_STEP, False, error)
            with self.lock:
                self.ack_reads["error"] += 1
            return
        self.mark(ACK_STEP, True)
        with self.lock:
            self.ack_reads["ok"] += 1
        for incident in wanted:
            found = goalert_match(incident, alerts, upto)
            if found is not None:
                incident.ack = {k: found[k] for k in ("alert_id", "at", "by", "via")}

    def measured(self, record: dict[str, Any]) -> dict[str, Any]:
        if self.query is None or not self.config.victoriametrics:
            return record
        requests = lost_of(
            list(record["services"]),
            float(record["start_ts"]),
            float(record["end_ts"]),
            self.config,
            self.query,
        )
        failed = any(entry.get("reason") == "query failed" for entry in requests.values())
        asked = failed or any(
            "served" in entry or entry.get("reason") == "no baseline" for entry in requests.values()
        )
        if asked:
            with self.lock:
                self.queries["error" if failed else "ok"] += 1
        record["requests"] = requests
        record["lost_requests"] = total_lost(requests)
        return record

    def mark(self, step: str, ok: bool, error: BaseException | None = None) -> None:
        if not ok and self.success.get(step, 1):
            log_json({"event": f"{step}_failed", "error": f"{type(error).__name__}: {error}"})
        elif ok and not self.success.get(step, 1):
            log_json({"event": f"{step}_ok"})
        self.success[step] = int(ok)

    def round(self) -> None:
        try:
            self.tick()
        except Exception as error:
            log_json({"event": "round_failed", "error": f"{type(error).__name__}: {error}"})
        with self.lock:
            self.last_round = self.clock()

    def samples(self) -> list[Sample]:
        with self.lock:
            records = list(self.records)
            open_now = list(self.tracker.open.values())
            last_round = self.last_round
            last_sync = self.last_sync
            transitions = self.transitions
            upto = min(self.clock(), self.tracker.cursor_ms / 1000)
        samples = self.never_lower(incident_samples(records, open_now, self.config, upto))
        for step in self.steps:
            samples.append(
                ("ayeops_incidents_step_success", label_pairs(step=step), self.success[step])
            )
        samples.append(("ayeops_incidents_last_round_timestamp_seconds", (), last_round))
        samples.append(("ayeops_incidents_last_sync_timestamp_seconds", (), last_sync))
        samples.append(("ayeops_incidents_journal_records", (), len(records)))
        samples.append(("ayeops_incidents_transitions_total", (), transitions))
        with self.lock:
            queries = sorted(self.queries.items())
            reads = sorted(self.ack_reads.items()) if self.acks is not None else []
        for result, count in queries:
            samples.append(
                ("ayeops_incidents_request_queries_total", label_pairs(result=result), count)
            )
        for result, count in reads:
            samples.append(("ayeops_incidents_ack_reads_total", label_pairs(result=result), count))
        return samples

    def never_lower(self, samples: list[Sample]) -> list[Sample]:
        kept: list[Sample] = []
        with self.lock:
            for name, pairs, value in samples:
                if name in PEAKED:
                    key = peak_key(name, pairs)
                    value = max(value, self.peaks.get(key, 0.0))
                    self.peaks[key] = value
                kept.append((name, pairs, value))
        return kept

    def metrics(self) -> str:
        return render(self.samples())

    def healthy(self, now: float) -> bool:
        with self.lock:
            last_round = self.last_round
        return now - last_round <= 3 * self.config.refresh_seconds + 120


LOG_KEYS = ("id", "alert", "service", "host", "severity", "start", "end", "duration_seconds")


def parse_hhmm(text: str) -> int:
    hours, _, minutes = text.strip().partition(":")
    value = int(hours) * 60 + int(minutes)
    if not 0 <= value < 24 * 60:
        raise ValueError(f"not a time of day: {text!r}")
    return value


def parse_services(text: str) -> dict[str, str]:
    raw = json.loads(text or "{}")
    if not isinstance(raw, dict):
        raise ValueError("AYE_INCIDENTS_SERVICES must be a JSON object")
    services = {}
    for name, level in raw.items():
        if level not in WEIGHTS:
            raise ValueError(f"service {name!r} has unknown criticality {level!r}")
        services[str(name)] = str(level)
    return services


def parse_hosts(text: str) -> dict[str, tuple[str, ...]]:
    raw = json.loads(text or "{}")
    if not isinstance(raw, dict):
        raise ValueError("AYE_INCIDENTS_HOSTS must be a JSON object")
    hosts = {}
    for host, names in raw.items():
        if not isinstance(names, list):
            raise ValueError(f"host {host!r} must list its services")
        hosts[str(host)] = tuple(str(name) for name in names)
    return hosts


def parse_routes(text: str) -> dict[str, tuple[Routes, ...]]:
    raw = json.loads(text or "{}")
    if not isinstance(raw, dict):
        raise ValueError("AYE_INCIDENTS_ROUTES must be a JSON object")
    found = {}
    for service, entries in raw.items():
        parsed = []
        for entry in entries:
            routes = tuple(str(name) for name in entry["routes"])
            bad = [name for name in routes if not ROUTE.match(name)]
            if bad:
                raise ValueError(f"service {service!r} has a route that is no Traefik name: {bad}")
            parsed.append(Routes(str(entry["host"]), routes, float(entry["scrape_seconds"])))
        found[str(service)] = tuple(parsed)
    return found


def parse_host_alerts(text: str) -> tuple[HostAlert, ...]:
    raw = json.loads(text or "[]")
    if not isinstance(raw, list):
        raise ValueError("AYE_INCIDENTS_HOST_ALERTS must be a JSON list")
    found = []
    for entry in raw:
        if isinstance(entry, str):
            found.append(HostAlert(entry))
            continue
        labels = entry.get("labels") or {} if isinstance(entry, dict) else None
        if not isinstance(labels, dict) or "title" not in entry:
            raise ValueError(f"host alert {entry!r} is neither a title nor a title with labels")
        pairs = tuple(sorted((str(key), str(value)) for key, value in labels.items()))
        found.append(HostAlert(str(entry["title"]), pairs))
    return tuple(found)


def time_zone(key: str) -> tzinfo:
    try:
        return ZoneInfo(key)
    except ZoneInfoNotFoundError:
        if key == "UTC":
            return UTC
        raise


def config_from(env: Mapping[str, str]) -> Config:
    severities = env.get("AYE_INCIDENTS_DOWNTIME_SEVERITIES", "critical,page")
    degraded = env.get("AYE_INCIDENTS_DEGRADED_SEVERITIES", "warning")
    return Config(
        grafana=env.get("AYE_INCIDENTS_GRAFANA", "http://grafana:3000"),
        token_file=Path(env["AYE_GRAFANA_READER_TOKEN_FILE"]),
        services=parse_services(env.get("AYE_INCIDENTS_SERVICES", "{}")),
        night=Night(
            parse_hhmm(env.get("AYE_INCIDENTS_NIGHT_START", "23:00")),
            parse_hhmm(env.get("AYE_INCIDENTS_NIGHT_END", "08:00")),
            time_zone(env.get("AYE_INCIDENTS_TIMEZONE") or "UTC"),
        ),
        downtime_severities=frozenset(s.strip() for s in severities.split(",") if s.strip()),
        degraded_severities=frozenset(s.strip() for s in degraded.split(",") if s.strip()),
        state_dir=Path(env.get("AYE_INCIDENTS_STATE", "/tmp")),
        hosts=parse_hosts(env.get("AYE_INCIDENTS_HOSTS", "{}")),
        host_modules=parse_hosts(env.get("AYE_INCIDENTS_HOST_MODULES", "{}")),
        host_alerts=parse_host_alerts(env.get("AYE_INCIDENTS_HOST_ALERTS", "[]")),
        victoriametrics=env.get("AYE_INCIDENTS_VICTORIAMETRICS", ""),
        routes=parse_routes(env.get("AYE_INCIDENTS_ROUTES", "{}")),
        refresh_seconds=float(env.get("AYE_INCIDENTS_REFRESH_SECONDS", "60")),
        backfill_days=float(env.get("AYE_INCIDENTS_BACKFILL_DAYS", "30")),
        oncall=env.get("AYE_INCIDENTS_ONCALL", ""),
    )


def read_token(path: Path) -> str:
    try:
        text = path.read_bytes().decode("ascii").strip()
    except (OSError, UnicodeError) as error:
        raise TokenError(f"cannot read the reader token {path}: {type(error).__name__}") from None
    if not TOKEN.fullmatch(text):
        raise TokenError(f"the reader token {path} holds no token")
    return text


def http_get(url: str, headers: Mapping[str, str], timeout: float) -> tuple[int, bytes]:
    request = urllib.request.Request(url, headers=dict(headers))
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return int(response.status), response.read()
    except urllib.error.HTTPError as error:
        return int(error.code), b""


def http_fetcher(token_file: Path | None = None) -> Fetch:
    headers = {"Accept": "application/json", "User-Agent": USER_AGENT}

    def fetch(url: str, timeout: float) -> tuple[int, bytes]:
        if token_file is None:
            return http_get(url, headers, timeout)
        token = read_token(token_file)
        status, body = http_get(url, {**headers, "Authorization": f"Bearer {token}"}, timeout)
        if status == 401:
            fresh = read_token(token_file)
            if fresh != token:
                status, body = http_get(
                    url, {**headers, "Authorization": f"Bearer {fresh}"}, timeout
                )
        return status, body

    return fetch


def secret_fetcher(secret: str) -> Fetch:
    headers = {
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
        "Authorization": f"Bearer {secret}",
    }

    def fetch(url: str, timeout: float) -> tuple[int, bytes]:
        return http_get(url, headers, timeout)

    return fetch


def make_server(agent: Agent, port: int, now: Clock = time.time) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path == "/health":
                if not agent.healthy(now()):
                    self.send_error(503)
                    return
                body = b"ok\n"
            elif self.path == "/metrics":
                body = agent.metrics().encode()
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

    return ThreadingHTTPServer(("", port), Handler)


def run_forever(agent: Agent) -> None:
    while True:
        started = time.monotonic()
        agent.round()
        time.sleep(max(1.0, agent.config.refresh_seconds - (time.monotonic() - started)))


def main() -> None:
    env = os.environ
    config = config_from(env)
    acks = secret_fetcher(env.get("AYE_INCIDENTS_ONCALL_SECRET", ""))
    agent = Agent(config, http_fetcher(config.token_file), time.time, http_fetcher(), acks)
    log_json(
        {
            "event": "start",
            "grafana": config.grafana,
            "oncall": config.oncall,
            "records": len(agent.records),
            "open": len(agent.tracker.open),
            "cursor": iso(agent.tracker.cursor_ms / 1000),
            "services": len(config.services),
        }
    )
    threading.Thread(target=run_forever, args=(agent,), daemon=True).start()
    make_server(agent, int(env.get("AYE_INCIDENTS_PORT", "9136"))).serve_forever()


if __name__ == "__main__":
    main()
