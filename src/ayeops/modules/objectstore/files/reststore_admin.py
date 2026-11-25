from __future__ import annotations

import base64
import datetime as dt
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Protocol
from zoneinfo import ZoneInfo

LOOP_SECONDS = 60
SIZE_SECONDS = 300
TIMEOUT = 10
RESTIC_TIMEOUT = 6 * 3600
PREFIX = "ayeops_reststore_"
METRICS_USER = "metrics"


@dataclass(frozen=True)
class Repo:
    name: str
    daily: int
    weekly: int
    monthly: int
    password_env: str

    def flags(self) -> list[str]:
        flags: list[str] = []
        for name, count in (
            ("--keep-daily", self.daily),
            ("--keep-weekly", self.weekly),
            ("--keep-monthly", self.monthly),
        ):
            if count > 0:
                flags += [name, str(count)]
        return flags

    def bound(self) -> int:
        return self.daily + self.weekly + self.monthly


@dataclass(frozen=True)
class Done:
    code: int
    stdout: str
    stderr: str


@dataclass(frozen=True)
class Outcome:
    attempt: float
    ok: bool
    error: str = ""
    success: float | None = None
    snapshots: int | None = None
    retention_ok: bool | None = None


@dataclass(frozen=True)
class Config:
    repos: tuple[Repo, ...]
    schedule: tuple[int, int]
    timezone: str
    retry_seconds: int
    data: Path
    state_path: Path
    restic: str
    server: str
    metrics_password: str
    quota_bytes: int | None
    port: int


class Restic(Protocol):
    def run(self, repo: str, password: str, args: Sequence[str]) -> Done: ...


def parse_schedule(text: str) -> tuple[int, int]:
    hours, _, minutes = text.strip().partition(":")
    hour, minute = int(hours), int(minutes)
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"not a time of day: {text!r}")
    return hour, minute


def parse_repos(text: str) -> tuple[Repo, ...]:
    data = json.loads(text or "[]")
    if not isinstance(data, list):
        raise ValueError("AYE_RESTSTORE_REPOS is not a JSON list")
    return tuple(
        Repo(
            name=str(item["name"]),
            daily=int(item["keep_daily"]),
            weekly=int(item["keep_weekly"]),
            monthly=int(item["keep_monthly"]),
            password_env=str(item["password_env"]),
        )
        for item in data
    )


def parse_forget(text: str) -> tuple[int, int]:
    data = json.loads(text) if text.strip() else []
    if not isinstance(data, list):
        raise ValueError("restic forget --json did not return a list")
    return len(data), sum(len(group.get("keep") or []) for group in data)


def exit_message(stderr: str) -> str:
    for line in reversed(stderr.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if message.get("message_type") == "exit_error":
                return next(iter(str(message.get("message") or "").splitlines()), "")
        elif line:
            return line
    return ""


def latest_slot(now: float, schedule: tuple[int, int], zone: dt.tzinfo) -> float:
    local = dt.datetime.fromtimestamp(now, zone)
    slot_time = dt.time(schedule[0], schedule[1])
    slot = dt.datetime.combine(local.date(), slot_time, tzinfo=zone)
    if slot.timestamp() > now:
        slot = dt.datetime.combine(local.date() - dt.timedelta(days=1), slot_time, tzinfo=zone)
    return slot.timestamp()


def due(now: float, outcome: Outcome | None, slot: float, retry: int) -> bool:
    if outcome is None:
        return True
    if outcome.success is not None and outcome.success >= slot:
        return False
    return now - outcome.attempt >= retry


def tree_bytes(path: Path) -> int:
    total = 0
    for root, _, files in os.walk(path):
        for name in files:
            try:
                total += (Path(root) / name).stat().st_size
            except OSError:
                continue
    return total


def has_config(path: Path) -> bool:
    return (path / "config").is_file()


def label(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def number(value: float | int) -> str:
    if isinstance(value, float) and not value.is_integer():
        return f"{value:.3f}"
    return str(int(value))


def metrics_text(
    repos: Sequence[Repo],
    outcomes: Mapping[str, Outcome],
    sizes: Mapping[str, int | None],
    data_bytes: int | None,
    quota: int | None,
    server_up: bool,
) -> str:
    lines = [f"{PREFIX}server_up {1 if server_up else 0}"]
    if data_bytes is not None:
        lines.append(f"{PREFIX}data_bytes {data_bytes}")
    if quota is not None:
        lines.append(f"{PREFIX}quota_bytes {quota}")
    for repo in repos:
        where = f'{{repo="{label(repo.name)}"}}'
        size = sizes.get(repo.name)
        lines.append(f"{PREFIX}repo_initialized{where} {0 if size is None else 1}")
        if size is not None:
            lines.append(f"{PREFIX}repo_bytes{where} {size}")
        outcome = outcomes.get(repo.name)
        if outcome is None:
            continue
        lines.append(f"{PREFIX}maintenance_ok{where} {1 if outcome.ok else 0}")
        success = number(outcome.success or 0)
        lines.append(f"{PREFIX}maintenance_last_success_timestamp_seconds{where} {success}")
        attempt = number(outcome.attempt)
        lines.append(f"{PREFIX}maintenance_last_attempt_timestamp_seconds{where} {attempt}")
        if outcome.snapshots is not None:
            lines.append(f"{PREFIX}snapshots{where} {outcome.snapshots}")
        if outcome.retention_ok is not None:
            lines.append(f"{PREFIX}retention_ok{where} {1 if outcome.retention_ok else 0}")
    return "".join(f"{line}\n" for line in lines)


def maintain(
    restic: Restic, repo: Repo, data: Path, password: str, now: Callable[[], float]
) -> Outcome:
    started = now()
    location = str(data / repo.name)
    restic.run(location, password, ["unlock"])
    host = ["--host", repo.name, "--group-by", "host"]
    done = restic.run(location, password, ["forget", "--json", *host, *repo.flags()])
    if done.code != 0:
        reason = f"restic forget failed ({done.code}): {exit_message(done.stderr)}"
        return Outcome(started, False, reason)
    groups, keep = parse_forget(done.stdout)
    retention_ok = groups <= 1 and keep <= repo.bound()
    prune = restic.run(location, password, ["prune"])
    if prune.code != 0:
        reason = f"restic prune failed ({prune.code}): {exit_message(prune.stderr)}"
        return Outcome(started, False, reason, snapshots=keep, retention_ok=retention_ok)
    if not retention_ok:
        reason = f"restic kept {keep} snapshots in {groups} groups, the policy keeps {repo.bound()}"
        return Outcome(started, False, reason, snapshots=keep, retention_ok=False)
    return Outcome(started, True, "", now(), keep, True)


def log(message: str) -> None:
    print(f"reststore-admin: {message}", file=sys.stderr, flush=True)


@dataclass
class Store:
    config: Config
    restic: Restic
    environ: Mapping[str, str]
    clock: Callable[[], float] = time.time
    initialized: Callable[[Path], bool] = has_config
    measure: Callable[[Path], int] = tree_bytes
    outcomes: dict[str, Outcome] = field(default_factory=dict)
    sizes: dict[str, int | None] = field(default_factory=dict)
    data_bytes: int | None = None
    measured: float | None = None

    def tick(self) -> None:
        now = self.clock()
        slot = latest_slot(now, self.config.schedule, ZoneInfo(self.config.timezone))
        for repo in self.config.repos:
            if not self.initialized(self.config.data / repo.name):
                continue
            previous = self.outcomes.get(repo.name)
            if not due(now, previous, slot, self.config.retry_seconds):
                continue
            password = self.environ.get(repo.password_env, "")
            if not password:
                outcome = Outcome(now, False, f"{repo.password_env} is not set")
            else:
                try:
                    outcome = maintain(self.restic, repo, self.config.data, password, self.clock)
                except (OSError, ValueError, KeyError) as error:
                    outcome = Outcome(now, False, str(error))
            if not outcome.ok and previous is not None:
                outcome = replace(outcome, success=previous.success)
            self.outcomes[repo.name] = outcome
            log(f"{repo.name}: {outcome.error or 'forget and prune passed'}")
        if self.measured is None or now - self.measured >= SIZE_SECONDS:
            self.measure_sizes(now)

    def measure_sizes(self, now: float) -> None:
        for repo in self.config.repos:
            path = self.config.data / repo.name
            self.sizes[repo.name] = self.measure(path) if self.initialized(path) else None
        self.data_bytes = self.measure(self.config.data)
        self.measured = now


def load_outcomes(path: Path) -> dict[str, Outcome]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {str(name): Outcome(**fields) for name, fields in data.items()}
    except (OSError, ValueError, TypeError, AttributeError):
        return {}


def save_outcomes(path: Path, outcomes: Mapping[str, Outcome]) -> None:
    temporary = path.with_suffix(".tmp")
    payload = {name: outcome.__dict__ for name, outcome in outcomes.items()}
    temporary.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    temporary.replace(path)


def basic_auth(user: str, password: str) -> str:
    token = base64.b64encode(f"{user}:{password}".encode()).decode("ascii")
    return f"Basic {token}"


def server_metrics(server: str, password: str) -> str | None:
    request = urllib.request.Request(
        f"{server}/metrics", headers={"Authorization": basic_auth(METRICS_USER, password)}
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            text: str = response.read().decode("utf-8", "replace")
            return text
    except (OSError, urllib.error.URLError, ValueError):
        return None


@dataclass(frozen=True)
class Process:
    binary: str
    base: Mapping[str, str]
    timeout: int = RESTIC_TIMEOUT

    def run(self, repo: str, password: str, args: Sequence[str]) -> Done:
        env = {**self.base, "RESTIC_REPOSITORY": repo, "RESTIC_PASSWORD": password}
        try:
            done = subprocess.run(
                [self.binary, *args],
                capture_output=True,
                text=True,
                timeout=self.timeout,
                stdin=subprocess.DEVNULL,
                env=env,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return Done(124, "", f"restic {args[0]} timed out after {self.timeout}s")
        except OSError as error:
            return Done(127, "", f"cannot run {self.binary}: {error}")
        return Done(done.returncode, done.stdout, done.stderr)


def restic_env(environ: Mapping[str, str]) -> dict[str, str]:
    keep = ("PATH", "HOME", "TZ", "TMPDIR", "RESTIC_CACHE_DIR")
    return {name: environ[name] for name in keep if name in environ}


def load_config(environ: Mapping[str, str]) -> Config:
    quota = environ.get("AYE_RESTSTORE_QUOTA_BYTES", "").strip()
    return Config(
        repos=parse_repos(environ.get("AYE_RESTSTORE_REPOS", "[]")),
        schedule=parse_schedule(environ.get("AYE_RESTSTORE_SCHEDULE", "11:00")),
        timezone=environ.get("AYE_RESTSTORE_TIMEZONE", "UTC"),
        retry_seconds=int(environ.get("AYE_RESTSTORE_RETRY_MINUTES", "60")) * 60,
        data=Path(environ.get("AYE_RESTSTORE_DATA", "/data")),
        state_path=Path(environ.get("AYE_RESTSTORE_STATE", "/state/state.json")),
        restic=environ.get("AYE_RESTIC", "/opt/restic/restic"),
        server=environ.get("AYE_RESTSTORE_SERVER", "http://rest-server:8000"),
        metrics_password=environ["AYE_RESTSTORE_METRICS_PASSWORD"],
        quota_bytes=int(quota) if quota else None,
        port=int(environ.get("AYE_RESTSTORE_ADMIN_PORT", "9133")),
    )


def exposition(store: Store) -> str:
    upstream = server_metrics(store.config.server, store.config.metrics_password)
    own = metrics_text(
        store.config.repos,
        store.outcomes,
        store.sizes,
        store.data_bytes,
        store.config.quota_bytes,
        upstream is not None,
    )
    return own + (upstream or "")


def handler(render: Callable[[], str]) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path == "/health":
                body = b"ok\n"
            elif self.path == "/metrics":
                body = render().encode()
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

    return Handler


def main(environ: Mapping[str, str] = os.environ) -> None:
    config = load_config(environ)
    store = Store(config, Process(config.restic, restic_env(environ)), environ)
    store.outcomes = load_outcomes(config.state_path)

    def work_forever() -> None:
        while True:
            store.tick()
            save_outcomes(config.state_path, store.outcomes)
            time.sleep(LOOP_SECONDS)

    threading.Thread(target=work_forever, daemon=True).start()
    server = ThreadingHTTPServer(("", config.port), handler(lambda: exposition(store)))
    server.serve_forever()


if __name__ == "__main__":
    main()
