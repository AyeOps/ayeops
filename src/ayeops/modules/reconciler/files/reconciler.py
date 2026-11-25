from __future__ import annotations

import fnmatch
import http.client
import json
import os
import re
import socket
import sys
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

PREFIX = "ayeops_reconciler_"
PROJECT_LABEL = "com.docker.compose.project"
SERVICE_LABEL = "com.docker.compose.service"
ONEOFF_LABEL = "com.docker.compose.oneoff"
DEPENDS_LABEL = "com.docker.compose.depends_on"
UP = {"running", "restarting", "paused", "removing"}
STARTABLE = {"created", "exited"}
STOP_CODES = {0, 137, 143}
DOCKER_RESTARTS = {"always", "unless-stopped"}
ACT = {"start-failed", "oom-killed", "crashed", "not-back-after-boot"}
DURATION = re.compile(r"^([1-9][0-9]*)([smh])$")
DURATION_FACTORS = {"s": 1, "m": 60, "h": 3600}
NAP = 30.0
POLL = 2.0
API_TIMEOUT = 30.0
DRIFT_LIMIT = 50
HEARTBEAT = Path("/tmp/reconciler.alive")
BOOT_ID = Path("/proc/sys/kernel/random/boot_id")

Api = Callable[[str, str, Mapping[str, str]], Any]
Clock = Callable[[], float]
Sleep = Callable[[float], None]
Emit = Callable[[dict[str, Any]], None]


def parse_duration(text: str) -> int:
    match = DURATION.match(text.strip())
    if match is None:
        raise ValueError(f"not a duration: {text!r}")
    return int(match.group(1)) * DURATION_FACTORS[match.group(2)]


def flag(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def parse_projects(text: str) -> dict[str, list[str]]:
    data = json.loads(text or "{}")
    if not isinstance(data, dict):
        raise ValueError("AYE_RECONCILER_PROJECTS is not a JSON object")
    return {str(name): sorted({str(s) for s in services}) for name, services in data.items()}


def parse_names(text: str, variable: str) -> list[str]:
    data = json.loads(text or "[]")
    if not isinstance(data, list):
        raise ValueError(f"{variable} is not a JSON array")
    return sorted({str(item) for item in data})


@dataclass
class Settings:
    projects: dict[str, list[str]] = field(default_factory=dict)
    declared: list[str] = field(default_factory=list)
    ignore: list[str] = field(default_factory=list)
    interval: int = 30
    attempts: int = 3
    window: int = 3600
    timeout: int = 120
    dry_run: bool = False
    textfile: Path = Path("/textfile/ayeops_reconciler.prom")
    state: Path = Path("/data/state.json")


def settings_from(environ: Mapping[str, str]) -> Settings:
    return Settings(
        projects=parse_projects(environ.get("AYE_RECONCILER_PROJECTS", "{}")),
        declared=parse_names(
            environ.get("AYE_RECONCILER_DECLARED", "[]"), "AYE_RECONCILER_DECLARED"
        ),
        ignore=parse_names(environ.get("AYE_RECONCILER_IGNORE", "[]"), "AYE_RECONCILER_IGNORE"),
        interval=parse_duration(environ.get("AYE_RECONCILER_INTERVAL", "30s")),
        attempts=int(environ.get("AYE_RECONCILER_ATTEMPTS", "3")),
        window=parse_duration(environ.get("AYE_RECONCILER_WINDOW", "1h")),
        timeout=parse_duration(environ.get("AYE_RECONCILER_TIMEOUT", "2m")),
        dry_run=flag(environ.get("AYE_RECONCILER_DRY_RUN")),
        textfile=Path(environ.get("AYE_RECONCILER_TEXTFILE", "/textfile/ayeops_reconciler.prom")),
        state=Path(environ.get("AYE_RECONCILER_STATE", "/data/state.json")),
    )


@dataclass(frozen=True)
class Seen:
    id: str
    project: str
    service: str
    name: str
    status: str
    exit_code: int = 0
    error: str = ""
    oom: bool = False
    health: str = ""
    restart: str = ""
    depends: tuple[tuple[str, str], ...] = ()

    def brief(self) -> dict[str, Any]:
        found: dict[str, Any] = {"service": self.service, "name": self.name, "status": self.status}
        if self.status not in UP:
            found["exit_code"] = self.exit_code
        if self.error:
            found["error"] = self.error
        if self.oom:
            found["oom_killed"] = True
        if self.health:
            found["health"] = self.health
        return found


def parse_depends(text: str) -> tuple[tuple[str, str], ...]:
    found = []
    for entry in (text or "").split(","):
        parts = entry.strip().split(":")
        if parts[0]:
            found.append((parts[0], parts[1] if len(parts) > 1 else "service_started"))
    return tuple(found)


def container_name(item: Mapping[str, Any]) -> str:
    names = item.get("Names") or [""]
    return str(names[0]).lstrip("/")


def seen_from_list(item: Mapping[str, Any]) -> Seen:
    labels = item.get("Labels") or {}
    return Seen(
        id=str(item.get("Id") or ""),
        project=str(labels.get(PROJECT_LABEL) or ""),
        service=str(labels.get(SERVICE_LABEL) or ""),
        name=container_name(item),
        status=str(item.get("State") or ""),
        depends=parse_depends(str(labels.get(DEPENDS_LABEL) or "")),
    )


def seen_from_inspect(item: Mapping[str, Any]) -> Seen:
    config = item.get("Config") or {}
    labels = config.get("Labels") or {}
    state = item.get("State") or {}
    policy = (item.get("HostConfig") or {}).get("RestartPolicy") or {}
    return Seen(
        id=str(item.get("Id") or ""),
        project=str(labels.get(PROJECT_LABEL) or ""),
        service=str(labels.get(SERVICE_LABEL) or ""),
        name=str(item.get("Name") or "").lstrip("/"),
        status=str(state.get("Status") or ""),
        exit_code=int(state.get("ExitCode") or 0),
        error=str(state.get("Error") or ""),
        oom=bool(state.get("OOMKilled")),
        health=str((state.get("Health") or {}).get("Status") or ""),
        restart=str(policy.get("Name") or ""),
        depends=parse_depends(str(labels.get(DEPENDS_LABEL) or "")),
    )


def declared(item: Mapping[str, Any], projects: Mapping[str, list[str]]) -> bool:
    labels = item.get("Labels") or {}
    project = labels.get(PROJECT_LABEL)
    if project not in projects or labels.get(ONEOFF_LABEL) == "True":
        return False
    return labels.get(SERVICE_LABEL) in projects[str(project)]


def inspect(api: Api, container: str) -> Seen:
    return seen_from_inspect(api("GET", f"/containers/{container}/json", {}) or {})


def observe(api: Api, projects: Mapping[str, list[str]]) -> dict[str, list[Seen]]:
    query = {"all": "true", "filters": json.dumps({"label": [PROJECT_LABEL]})}
    listed = [
        item for item in api("GET", "/containers/json", query) or [] if declared(item, projects)
    ]
    found: dict[str, list[Seen]] = {name: [] for name in projects}
    for item in listed:
        seen = seen_from_list(item)
        if seen.status not in UP:
            seen = inspect(api, seen.id)
        found[seen.project].append(seen)
    for containers in found.values():
        containers.sort(key=lambda c: (c.service, c.name))
    return found


@dataclass(frozen=True)
class Stray:
    name: str
    image: str
    compose_project: str


@dataclass
class Drift:
    total: int = 0
    listed: list[Stray] = field(default_factory=list)


def stray_from(item: Mapping[str, Any]) -> Stray:
    labels = item.get("Labels") or {}
    return Stray(
        name=container_name(item),
        image=str(item.get("Image") or ""),
        compose_project=str(labels.get(PROJECT_LABEL) or ""),
    )


def undeclared(stray: Stray, known: set[str], ignore: Iterable[str]) -> bool:
    if stray.compose_project in known:
        return False
    return not any(fnmatch.fnmatchcase(stray.name, pattern) for pattern in ignore)


def drift(api: Api, settings: Settings, limit: int = DRIFT_LIMIT) -> Drift:
    known = set(settings.declared) | set(settings.projects)
    found = sorted(
        (
            stray
            for stray in map(stray_from, api("GET", "/containers/json", {}) or [])
            if undeclared(stray, known, settings.ignore)
        ),
        key=lambda stray: stray.name,
    )
    return Drift(total=len(found), listed=found[:limit])


def verdict(container: Seen, after_boot: set[str]) -> str:
    if container.status in UP:
        return "up"
    if container.status not in STARTABLE:
        return "stuck"
    if container.error:
        return "start-failed"
    if container.restart in DOCKER_RESTARTS:
        return "stopped"
    if container.oom:
        return "oom-killed"
    if container.exit_code not in STOP_CODES:
        return "crashed"
    if container.id in after_boot:
        return "not-back-after-boot"
    return "stopped"


def start_order(containers: Iterable[Seen]) -> list[Seen]:
    pending = sorted(containers, key=lambda c: (c.service, c.name))
    services = {c.service for c in pending}
    ordered: list[Seen] = []
    placed: set[str] = set()
    while pending:
        ready = [
            c for c in pending if all(dep in placed or dep not in services for dep, _ in c.depends)
        ]
        batch = ready or pending[:1]
        for container in batch:
            ordered.append(container)
            pending.remove(container)
        placed |= {c.service for c in ordered} - {c.service for c in pending}
    return ordered


def satisfied(container: Seen, condition: str) -> bool:
    if condition == "service_completed_successfully":
        return container.status == "exited" and container.exit_code == 0
    if container.status != "running":
        return False
    if condition == "service_healthy":
        return container.health in ("healthy", "")
    return True


def settled(container: Seen) -> bool:
    if container.status in ("exited", "dead", "missing"):
        return True
    return container.status == "running" and container.health != "starting"


def healthy(container: Seen) -> bool:
    return container.status == "running" and container.health != "unhealthy"


def wait(check: Callable[[], bool], deadline: float, clock: Clock, sleep: Sleep) -> bool:
    while True:
        if check():
            return True
        left = deadline - clock()
        if left <= 0:
            return False
        sleep(min(POLL, left))


@dataclass
class Attempt:
    project: str
    reasons: dict[str, str]
    before: list[dict[str, Any]]
    started: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    after: list[dict[str, Any]] = field(default_factory=list)
    success: bool = False
    seconds: float = 0.0


def look(api: Api, container: Seen) -> Seen:
    try:
        return inspect(api, container.id)
    except Exception:
        return Seen(container.id, container.project, container.service, container.name, "missing")


def all_satisfied(api: Api, containers: list[Seen], condition: str) -> bool:
    return all(satisfied(look(api, c), condition) for c in containers)


def all_settled(api: Api, containers: list[Seen]) -> bool:
    return all(settled(look(api, c)) for c in containers)


def dependencies_ready(
    api: Api,
    container: Seen,
    project: list[Seen],
    starting: set[str],
    deadline: float,
    clock: Clock,
    sleep: Sleep,
) -> str:
    for dep, condition in container.depends:
        targets = [c for c in project if c.service == dep]
        if not targets:
            continue
        if not any(c.status in UP or c.id in starting for c in targets):
            return f"{container.service}: dependency {dep} is stopped"
        if not wait(partial(all_satisfied, api, targets, condition), deadline, clock, sleep):
            return f"{container.service}: dependency {dep} not {condition} in time"
    return ""


def attempt(
    api: Api,
    project: str,
    containers: list[Seen],
    reasons: dict[str, str],
    timeout: int,
    clock: Clock,
    sleep: Sleep,
) -> Attempt:
    started_at = clock()
    deadline = started_at + timeout
    result = Attempt(project, reasons, briefs(containers))
    candidates = [c for c in containers if c.id in reasons]
    starting = {c.id for c in candidates}
    started: list[Seen] = []
    for container in start_order(candidates):
        problem = dependencies_ready(api, container, containers, starting, deadline, clock, sleep)
        if problem:
            result.errors.append(problem)
            continue
        try:
            api("POST", f"/containers/{container.id}/start", {})
            started.append(container)
        except Exception as error:
            result.errors.append(f"{container.service}: {type(error).__name__}: {error}")
    result.started = [c.name for c in started]
    wait(partial(all_settled, api, started), deadline, clock, sleep)
    after = [look(api, c) for c in containers]
    result.after = briefs(after)
    result.success = not result.errors and all(healthy(c) for c in after if c.id in starting)
    result.seconds = round(clock() - started_at, 1)
    return result


@dataclass
class Book:
    attempts: list[float] = field(default_factory=list)
    given_up: bool = False
    totals: dict[str, int] = field(
        default_factory=lambda: {"attempts": 0, "successes": 0, "failures": 0}
    )


@dataclass
class State:
    boot_id: str = ""
    running: set[str] = field(default_factory=set)
    after_boot: set[str] = field(default_factory=set)
    books: dict[str, Book] = field(default_factory=dict)
    last_run: float = 0.0
    planned: dict[str, dict[str, str]] = field(default_factory=dict)


def state_from(data: Mapping[str, Any]) -> State:
    books = {}
    for name, book in (data.get("projects") or {}).items():
        totals = {"attempts": 0, "successes": 0, "failures": 0}
        totals.update({k: int(v) for k, v in (book.get("totals") or {}).items() if k in totals})
        books[str(name)] = Book(
            attempts=[float(t) for t in book.get("attempts") or []],
            given_up=bool(book.get("given_up")),
            totals=totals,
        )
    return State(
        boot_id=str(data.get("boot_id") or ""),
        running=set(map(str, data.get("running") or [])),
        after_boot=set(map(str, data.get("after_boot") or [])),
        books=books,
        last_run=float(data.get("last_run") or 0.0),
    )


def state_data(state: State) -> dict[str, Any]:
    return {
        "boot_id": state.boot_id,
        "running": sorted(state.running),
        "after_boot": sorted(state.after_boot),
        "last_run": state.last_run,
        "projects": {
            name: {"attempts": book.attempts, "given_up": book.given_up, "totals": book.totals}
            for name, book in sorted(state.books.items())
        },
    }


def load_state(path: Path) -> State:
    try:
        return state_from(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, AttributeError, TypeError):
        return State()


def new_boot(state: State, boot_id: str) -> State:
    if not boot_id or boot_id == state.boot_id:
        return state
    after_boot = set(state.running) if state.boot_id else set()
    return State(boot_id, set(), after_boot, state.books, state.last_run, state.planned)


@dataclass
class Tally:
    missing: int = 0
    stopped: int = 0


def plan(containers: list[Seen], after_boot: set[str]) -> dict[str, str]:
    reasons = {c.id: verdict(c, after_boot) for c in containers}
    return {container: reason for container, reason in reasons.items() if reason in ACT}


def tally(containers: list[Seen], services: list[str], after_boot: set[str]) -> Tally:
    present = {c.service for c in containers}
    verdicts = [verdict(c, after_boot) for c in containers]
    return Tally(
        missing=len(set(services) - present) + verdicts.count("stuck"),
        stopped=verdicts.count("stopped"),
    )


def reconcile(
    api: Api,
    settings: Settings,
    state: State,
    boot_id: str,
    clock: Clock,
    sleep: Sleep,
    emit: Emit,
) -> tuple[State, dict[str, Tally]]:
    state = new_boot(state, boot_id)
    found = observe(api, settings.projects)
    seen = [c for containers in found.values() for c in containers]
    after_boot = state.after_boot - {c.id for c in seen if c.status in UP}
    tallies: dict[str, Tally] = {}
    for project, services in sorted(settings.projects.items()):
        containers = found.get(project, [])
        tallies[project] = tally(containers, services, after_boot)
        book = state.books.setdefault(project, Book())
        now = clock()
        book.attempts = [t for t in book.attempts if t > now - settings.window]
        reasons = plan(containers, after_boot)
        if not reasons:
            if book.given_up:
                emit({"event": "recovered", "project": project, "state": briefs(containers)})
            book.given_up = False
            state.planned.pop(project, None)
            continue
        if book.given_up:
            continue
        if len(book.attempts) >= settings.attempts:
            book.given_up = True
            emit(gave_up_line(project, book, settings, containers, reasons))
            continue
        if settings.dry_run:
            wanted = named(containers, reasons)
            if state.planned.get(project) != wanted:
                emit({"event": "would-start", "project": project, "reasons": wanted})
            state.planned[project] = wanted
            continue
        book.attempts.append(now)
        book.totals["attempts"] += 1
        result = attempt(api, project, containers, reasons, settings.timeout, clock, sleep)
        book.totals["successes" if result.success else "failures"] += 1
        emit(attempt_line(result, containers, len(book.attempts), settings))
    state.running = {c.id for c in seen if c.status == "running"}
    state.after_boot = after_boot
    state.books = {name: book for name, book in state.books.items() if name in settings.projects}
    state.last_run = clock()
    return state, tallies


def named(containers: list[Seen], reasons: Mapping[str, str]) -> dict[str, str]:
    return {c.name: reasons[c.id] for c in containers if c.id in reasons}


def briefs(containers: Iterable[Seen]) -> list[dict[str, Any]]:
    return [c.brief() for c in containers]


def gave_up_line(
    project: str, book: Book, settings: Settings, containers: list[Seen], reasons: dict[str, str]
) -> dict[str, Any]:
    return {
        "event": "gave-up",
        "project": project,
        "attempts": len(book.attempts),
        "window_seconds": settings.window,
        "reasons": named(containers, reasons),
        "state": briefs(containers),
    }


def attempt_line(
    result: Attempt, containers: list[Seen], number: int, settings: Settings
) -> dict[str, Any]:
    return {
        "event": "attempt",
        "project": result.project,
        "attempt": number,
        "of": settings.attempts,
        "reasons": named(containers, result.reasons),
        "before": result.before,
        "started": result.started,
        "errors": result.errors,
        "after": result.after,
        "success": result.success,
        "seconds": result.seconds,
    }


def escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def series(name: str, value: float, /, **labels: str) -> str:
    rendered = ",".join(f'{key}="{escape(val)}"' for key, val in sorted(labels.items()))
    suffix = "{" + rendered + "}" if rendered else ""
    number = int(value) if float(value).is_integer() else value
    return f"{name}{suffix} {number}\n"


def metric(name: str, value: float, **labels: str) -> str:
    return series(PREFIX + name, value, **labels)


def drift_metrics(found: Drift) -> list[str]:
    lines = [
        series("ayeops_undeclared_containers", found.total),
        series("ayeops_undeclared_containers_overflow", found.total - len(found.listed)),
    ]
    for stray in found.listed:
        labels = {
            "name": stray.name,
            "image": stray.image,
            "compose_project": stray.compose_project,
        }
        lines.append(series("ayeops_undeclared_container_info", 1, **labels))
    return lines


def render_metrics(
    state: State, tallies: Mapping[str, Tally], settings: Settings, found: Drift
) -> str:
    lines = [
        metric("last_run_timestamp_seconds", round(state.last_run)),
        metric("dry_run", int(settings.dry_run)),
        metric("projects", len(settings.projects)),
    ]
    for project in sorted(settings.projects):
        book = state.books.get(project, Book())
        counted = tallies.get(project, Tally())
        lines.append(metric("attempts_total", book.totals["attempts"], compose_project=project))
        lines.append(metric("successes_total", book.totals["successes"], compose_project=project))
        lines.append(metric("failures_total", book.totals["failures"], compose_project=project))
        lines.append(metric("given_up", int(book.given_up), compose_project=project))
        lines.append(metric("missing", counted.missing, compose_project=project))
        lines.append(metric("stopped", counted.stopped, compose_project=project))
    return "".join(lines + drift_metrics(found))


def write_atomic(path: Path, text: str) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def read_boot_id(path: Path = BOOT_ID) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


class UnixConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: str, timeout: float) -> None:
        super().__init__("localhost", timeout=timeout)
        self.socket_path = socket_path

    def connect(self) -> None:
        if sys.platform == "win32":
            raise OSError("the docker socket needs a unix host")
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self.socket_path)
        self.sock = sock


def request_target(path: str, query: Mapping[str, str]) -> str:
    if not query:
        return path
    return path + ("&" if "?" in path else "?") + urlencode(query)


def docker_api(
    socket_path: str,
    timeout: float = API_TIMEOUT,
    connect: Callable[[str, float], Any] = UnixConnection,
) -> Api:
    def call(method: str, path: str, query: Mapping[str, str]) -> Any:
        target = request_target(path, query)
        connection = connect(socket_path, timeout)
        try:
            connection.request(method, target)
            response = connection.getresponse()
            body = response.read()
        finally:
            connection.close()
        if response.status >= 400:
            raise RuntimeError(f"{method} {path}: {response.status} {body[:300]!r}")
        return json.loads(body) if body else None

    return call


def log(line: Mapping[str, Any]) -> None:
    print(json.dumps(line, sort_keys=True), flush=True)


def record(
    api: Api,
    settings: Settings,
    state: State,
    clock: Clock = time.time,
    sleep: Sleep = time.sleep,
    emit: Emit = log,
    boot: Callable[[], str] = read_boot_id,
) -> State:
    try:
        state, tallies = reconcile(api, settings, state, boot(), clock, sleep, emit)
        found = drift(api, settings)
    except Exception as error:
        emit({"event": "error", "error": f"{type(error).__name__}: {error}"})
        return state
    write_atomic(settings.state, json.dumps(state_data(state), sort_keys=True))
    write_atomic(settings.textfile, render_metrics(state, tallies, settings, found))
    return state


def napper(sleep: Sleep, heartbeat: Path) -> Sleep:
    def nap(seconds: float) -> None:
        heartbeat.touch()
        sleep(min(seconds, NAP))

    return nap


def serve(
    api: Api,
    settings: Settings,
    state: State,
    clock: Clock = time.time,
    sleep: Sleep = time.sleep,
    heartbeat: Path = HEARTBEAT,
    going: Callable[[], bool] = lambda: True,
    boot: Callable[[], str] = read_boot_id,
) -> State:
    nap = napper(sleep, heartbeat)
    while going():
        due = clock() + settings.interval
        while clock() < due:
            nap(due - clock())
        state = record(api, settings, state, clock, nap, log, boot)
    return state


def main(environ: Mapping[str, str] = os.environ) -> int:
    settings = settings_from(environ)
    api = docker_api(environ.get("AYE_RECONCILER_SOCKET", "/var/run/docker.sock"))
    state = load_state(settings.state)
    if flag(environ.get("AYE_RECONCILER_ONCE")):
        record(api, settings, state)
        return 0
    serve(api, settings, state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
