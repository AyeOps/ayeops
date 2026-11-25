from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Protocol
from zoneinfo import ZoneInfo

DAY = 86400
PGDMP = b"PGDMP"
SHA_TAG = "sha256="
AGENT_TAG = "ayeops"
REPO_MISSING = 10
WRONG_PASSWORD = 12
CHUNK = 1 << 20
LOOP_SECONDS = 60
RESTIC_TIMEOUT = 6 * 3600
PREFIX = "ayeops_offsite_"


@dataclass(frozen=True)
class Policy:
    daily: int
    weekly: int
    monthly: int

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
class Snapshot:
    id: str
    time: dt.datetime
    paths: tuple[str, ...]
    tags: tuple[str, ...]

    @property
    def sha256(self) -> str | None:
        for tag in self.tags:
            if tag.startswith(SHA_TAG):
                return tag[len(SHA_TAG) :]
        return None


@dataclass(frozen=True)
class Summary:
    snapshot_id: str
    added_bytes: int
    processed_bytes: int
    duration: float


@dataclass(frozen=True)
class Forget:
    groups: int
    keep: tuple[str, ...]
    remove: tuple[str, ...]


@dataclass(frozen=True)
class Done:
    code: int
    stdout: str
    stderr: str


@dataclass(frozen=True)
class Config:
    source: str
    host: str
    schedule: tuple[int, int]
    timezone: str
    policy: Policy
    restore_every_days: int
    settle_seconds: int
    retry_seconds: int
    verify: str
    quota_bytes: int | None
    state_path: Path
    textfile: Path
    restic: str
    append_only: bool = False
    max_age_seconds: int | None = None


@dataclass(frozen=True)
class State:
    last_attempt: float | None = None
    last_success: float | None = None
    last_ok: bool | None = None
    last_error: str = ""
    last_file: str = ""
    last_snapshot: str = ""
    last_source_bytes: int | None = None
    last_added_bytes: int | None = None
    last_duration: float | None = None
    snapshots: int | None = None
    repo_bytes: int | None = None
    retention_ok: bool | None = None
    repository_ok: bool | None = None
    restore_attempt: float | None = None
    restore_success: float | None = None
    restore_ok: bool | None = None
    restore_error: str = ""


class Restic(Protocol):
    def run(self, args: Sequence[str]) -> Done: ...

    def stream(self, args: Sequence[str], sink: Callable[[bytes], None]) -> Done: ...


def parse_schedule(text: str) -> tuple[int, int]:
    hours, _, minutes = text.strip().partition(":")
    hour, minute = int(hours), int(minutes)
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"not a time of day: {text!r}")
    return hour, minute


def parse_time(text: str) -> dt.datetime:
    moment = dt.datetime.fromisoformat(text)
    if moment.tzinfo is None:
        raise ValueError(f"time without an offset: {text!r}")
    return moment


def parse_snapshot(item: Mapping[str, Any]) -> Snapshot:
    return Snapshot(
        id=str(item["id"]),
        time=parse_time(str(item["time"])),
        paths=tuple(str(path) for path in item.get("paths") or []),
        tags=tuple(str(tag) for tag in item.get("tags") or []),
    )


def parse_snapshots(text: str) -> list[Snapshot]:
    data = json.loads(text)
    if not isinstance(data, list):
        raise ValueError("restic snapshots --json did not return a list")
    return [parse_snapshot(item) for item in data]


def parse_summary(text: str) -> Summary:
    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        message = json.loads(line)
        if message.get("message_type") != "summary":
            continue
        added = message.get("data_added_packed", message.get("data_added", 0))
        return Summary(
            snapshot_id=str(message.get("snapshot_id") or ""),
            added_bytes=int(added),
            processed_bytes=int(message.get("total_bytes_processed", 0)),
            duration=float(message.get("total_duration", 0.0)),
        )
    raise ValueError("restic backup --json printed no summary")


def parse_stats(text: str) -> int:
    data = json.loads(text)
    return int(data["total_size"])


def parse_forget(text: str) -> Forget:
    data = json.loads(text) if text.strip() else []
    if not isinstance(data, list):
        raise ValueError("restic forget --json did not return a list")
    keep: list[str] = []
    remove: list[str] = []
    for group in data:
        keep += [str(item["id"]) for item in group.get("keep") or []]
        remove += [str(item["id"]) for item in group.get("remove") or []]
    return Forget(len(data), tuple(keep), tuple(remove))


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


def day_key(moment: dt.datetime) -> int:
    return moment.year * 10000 + moment.month * 100 + moment.day


def week_key(moment: dt.datetime) -> int:
    year, week, _ = moment.isocalendar()
    return year * 100 + week


def month_key(moment: dt.datetime) -> int:
    return moment.year * 100 + moment.month


def kept(snapshots: Iterable[Snapshot], policy: Policy) -> set[str]:
    ordered = sorted(snapshots, key=lambda snapshot: snapshot.time, reverse=True)
    buckets: list[tuple[Callable[[dt.datetime], int], int]] = [
        (day_key, policy.daily),
        (week_key, policy.weekly),
        (month_key, policy.monthly),
    ]
    counts = [count for _, count in buckets]
    last: list[int | None] = [None for _ in buckets]
    keep: set[str] = set()
    for index, snapshot in enumerate(ordered):
        oldest = index == len(ordered) - 1
        for position, (key, _) in enumerate(buckets):
            if counts[position] <= 0:
                continue
            value = key(snapshot.time)
            if value != last[position] or oldest:
                keep.add(snapshot.id)
                last[position] = value
                counts[position] -= 1
    return keep


def latest_slot(now: float, schedule: tuple[int, int], zone: dt.tzinfo) -> float:
    local = dt.datetime.fromtimestamp(now, zone)
    slot_time = dt.time(schedule[0], schedule[1])
    slot = dt.datetime.combine(local.date(), slot_time, tzinfo=zone)
    if slot.timestamp() > now:
        slot = dt.datetime.combine(local.date() - dt.timedelta(days=1), slot_time, tzinfo=zone)
    return slot.timestamp()


def backup_due(
    now: float, state: State, schedule: tuple[int, int], zone: dt.tzinfo, retry: int
) -> bool:
    if state.last_success is not None and state.last_success >= latest_slot(now, schedule, zone):
        return False
    return state.last_attempt is None or now - state.last_attempt >= retry


def restore_due(now: float, state: State, every_days: int, retry: int) -> bool:
    if state.last_success is None:
        return False
    if state.restore_success is not None and now - state.restore_success < every_days * DAY:
        return False
    return state.restore_attempt is None or now - state.restore_attempt >= retry


def newest(entries: Iterable[tuple[str, float]], now: float, settle: int) -> str | None:
    settled = [(mtime, name) for name, mtime in entries if now - mtime >= settle]
    if not settled:
        return None
    return max(settled)[1]


def matching_files(pattern: str) -> list[tuple[str, float]]:
    entries: list[tuple[str, float]] = []
    where = Path(pattern)
    for path in where.parent.glob(where.name):
        if path.is_file():
            entries.append((str(path), path.stat().st_mtime))
    return entries


def verdict(expected: str | None, actual: str, size: int, header: bytes, verify: str) -> str:
    if expected is None:
        return f"the snapshot has no {SHA_TAG} tag to compare against"
    if size == 0:
        return "the restored file is empty"
    if actual != expected:
        return f"restored sha256 {actual} differs from the recorded {expected}"
    if verify == "pg_dump" and not header.startswith(PGDMP):
        return "the restored file does not start with the pg_dump custom-format header PGDMP"
    return ""


def restore_pick(snapshots: Sequence[Snapshot], recorded: str) -> Snapshot:
    for snapshot in snapshots:
        if recorded and snapshot.id == recorded:
            return snapshot
    return max(snapshots, key=lambda snapshot: snapshot.time)


def flag(value: bool) -> int:
    return 1 if value else 0


def metrics_text(state: State, quota: int | None, max_age: int | None = None) -> str:
    values: list[tuple[str, float | int]] = []
    if state.last_attempt is not None:
        values.append(("last_run_success", flag(bool(state.last_ok))))
        values.append(("last_success_timestamp_seconds", state.last_success or 0))
    optional: list[tuple[str, float | int | None]] = [
        ("last_attempt_timestamp_seconds", state.last_attempt),
        ("last_source_bytes", state.last_source_bytes),
        ("last_added_bytes", state.last_added_bytes),
        ("last_duration_seconds", state.last_duration),
        ("snapshots", state.snapshots),
        ("repo_size_bytes", state.repo_bytes),
        ("quota_bytes", quota),
        ("max_age_seconds", max_age),
    ]
    values += [(name, value) for name, value in optional if value is not None]
    if state.retention_ok is not None:
        values.append(("retention_ok", flag(state.retention_ok)))
    if state.repository_ok is not None:
        values.append(("repository_ok", flag(state.repository_ok)))
    if state.restore_attempt is not None:
        values.append(("restore_test_timestamp_seconds", state.restore_attempt))
        values.append(("restore_test_last_success_timestamp_seconds", state.restore_success or 0))
        values.append(("restore_test_ok", flag(bool(state.restore_ok))))
    return "".join(f"{PREFIX}{name} {number(value)}\n" for name, value in values)


def number(value: float | int) -> str:
    if isinstance(value, float) and not value.is_integer():
        return f"{value:.3f}"
    return str(int(value))


def load_state(path: Path) -> State:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return State()
    known = State.__dataclass_fields__
    return State(**{key: value for key, value in data.items() if key in known})


def save_state(path: Path, state: State) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(state.__dict__, sort_keys=True), encoding="utf-8")
    temporary.replace(path)


def write_metrics(path: Path, text: str) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def file_digest(path: str) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with Path(path).open("rb") as handle:
        while chunk := handle.read(CHUNK):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def fingerprint(path: str) -> tuple[int, float]:
    status = Path(path).stat()
    return status.st_size, status.st_mtime


def log(message: str) -> None:
    print(f"offsite: {message}", file=sys.stderr, flush=True)


@dataclass
class Pass:
    config: Config
    restic: Restic
    clock: Callable[[], float] = time.time
    files: Callable[[str], list[tuple[str, float]]] = matching_files
    digest: Callable[[str], tuple[str, int]] = file_digest
    stat: Callable[[str], tuple[int, float]] = fingerprint
    notes: list[str] = field(default_factory=list)

    def note(self, message: str) -> None:
        self.notes.append(message)
        log(message)

    def host_args(self) -> list[str]:
        return ["--host", self.config.host]

    def snapshots(self) -> tuple[Done, list[Snapshot]]:
        done = self.restic.run(["snapshots", "--json", *self.host_args()])
        if done.code != 0:
            return done, []
        return done, parse_snapshots(done.stdout)

    def repository_problem(self, done: Done) -> str:
        if done.code == REPO_MISSING:
            return "the repository does not exist yet: run restic init, see the offsite runbook"
        if done.code == WRONG_PASSWORD:
            return "wrong AYE_OFFSITE_RESTIC_PASSWORD for this repository"
        return f"restic snapshots failed ({done.code}): {exit_message(done.stderr)}"

    def backup(self, state: State) -> State:
        started = self.clock()
        state = replace(state, last_attempt=started)
        listed, _ = self.snapshots()
        if listed.code != 0:
            return self.failed(state, self.repository_problem(listed), repository_ok=False)
        state = replace(state, repository_ok=True)
        chosen = newest(self.files(self.config.source), started, self.config.settle_seconds)
        if chosen is None:
            return self.failed(state, f"no settled file matches {self.config.source}")
        before = self.stat(chosen)
        sha, size = self.digest(chosen)
        self.restic.run(["unlock"])
        tags = ["--tag", AGENT_TAG, "--tag", f"{SHA_TAG}{sha}"]
        done = self.restic.run(["backup", "--json", *self.host_args(), *tags, chosen])
        if done.code != 0:
            reason = f"restic backup failed ({done.code}): {exit_message(done.stderr)}"
            return self.failed(state, reason)
        summary = parse_summary(done.stdout)
        if self.stat(chosen) != before:
            if summary.snapshot_id and not self.config.append_only:
                self.restic.run(["forget", summary.snapshot_id])
            return self.failed(state, f"{chosen} changed while it was backed up")
        state = replace(
            state,
            last_file=chosen,
            last_snapshot=summary.snapshot_id,
            last_source_bytes=size,
            last_added_bytes=summary.added_bytes,
        )
        state = self.inventory(state) if self.config.append_only else self.retention(state)
        finished = self.clock()
        self.note(f"backed up {chosen}, {summary.added_bytes} bytes added")
        return replace(
            state,
            last_success=finished,
            last_ok=True,
            last_error="",
            last_duration=round(finished - started, 3),
        )

    def retention(self, state: State) -> State:
        listed, before = self.snapshots()
        args = ["forget", "--json", *self.host_args(), "--group-by", "host"]
        done = self.restic.run([*args, *self.config.policy.flags()])
        if listed.code != 0 or done.code != 0:
            self.note(f"restic forget failed ({done.code}): {exit_message(done.stderr)}")
            return replace(state, retention_ok=False)
        result = parse_forget(done.stdout)
        expected = kept(before, self.config.policy)
        ok = result.groups == 1 and set(result.keep) == expected
        ok = ok and len(result.keep) <= self.config.policy.bound()
        if not ok:
            self.note(
                f"retention drift: restic kept {len(result.keep)} in {result.groups} groups,"
                f" the policy keeps {len(expected)}"
            )
        prune = self.restic.run(["prune"])
        if prune.code != 0:
            self.note(f"restic prune failed ({prune.code}): {exit_message(prune.stderr)}")
            ok = False
        stats = self.restic.run(["stats", "--json", "--mode", "raw-data"])
        repo = parse_stats(stats.stdout) if stats.code == 0 else state.repo_bytes
        return replace(state, retention_ok=ok, snapshots=len(result.keep), repo_bytes=repo)

    def inventory(self, state: State) -> State:
        listed, snapshots = self.snapshots()
        stats = self.restic.run(["stats", "--json", "--mode", "raw-data"])
        repo = parse_stats(stats.stdout) if stats.code == 0 else state.repo_bytes
        count = len(snapshots) if listed.code == 0 else state.snapshots
        return replace(state, retention_ok=None, snapshots=count, repo_bytes=repo)

    def failed(self, state: State, reason: str, repository_ok: bool | None = None) -> State:
        self.note(reason)
        changed = replace(state, last_ok=False, last_error=reason)
        if repository_ok is not None:
            changed = replace(changed, repository_ok=repository_ok)
        return changed

    def restore(self, state: State) -> State:
        state = replace(state, restore_attempt=self.clock())
        listed, snapshots = self.snapshots()
        if listed.code != 0 or not snapshots:
            return self.restore_failed(state, "no snapshot to restore")
        latest = restore_pick(snapshots, state.last_snapshot)
        if not latest.paths:
            return self.restore_failed(state, f"snapshot {latest.id} has no path")
        digest = hashlib.sha256()
        header = bytearray()
        size = 0

        def sink(chunk: bytes) -> None:
            nonlocal size
            digest.update(chunk)
            if len(header) < len(PGDMP):
                header.extend(chunk[: len(PGDMP) - len(header)])
            size += len(chunk)

        done = self.restic.stream(["dump", latest.id, latest.paths[0]], sink)
        if done.code != 0:
            return self.restore_failed(
                state, f"restic dump failed ({done.code}): {exit_message(done.stderr)}"
            )
        problem = verdict(
            latest.sha256, digest.hexdigest(), size, bytes(header), self.config.verify
        )
        if problem:
            return self.restore_failed(state, problem)
        check = self.restic.run(["check"])
        if check.code != 0:
            return self.restore_failed(
                state, f"restic check failed ({check.code}): {exit_message(check.stderr)}"
            )
        self.note(f"restore test passed: {latest.paths[0]} from {latest.id[:8]}, {size} bytes")
        return replace(state, restore_success=self.clock(), restore_ok=True, restore_error="")

    def restore_failed(self, state: State, reason: str) -> State:
        self.note(f"restore test failed: {reason}")
        return replace(state, restore_ok=False, restore_error=reason)

    def tick(self, state: State) -> State:
        now = self.clock()
        zone = ZoneInfo(self.config.timezone)
        if backup_due(now, state, self.config.schedule, zone, self.config.retry_seconds):
            try:
                state = self.backup(state)
            except (OSError, ValueError, KeyError) as error:
                state = self.failed(replace(state, last_attempt=now), f"backup pass: {error}")
        now = self.clock()
        if restore_due(now, state, self.config.restore_every_days, self.config.retry_seconds):
            try:
                state = self.restore(state)
            except (OSError, ValueError, KeyError) as error:
                state = self.restore_failed(replace(state, restore_attempt=now), str(error))
        return state


def env_int(environ: Mapping[str, str], name: str, default: int) -> int:
    value = environ.get(name, "")
    return int(value) if value.strip() else default


def load_config(environ: Mapping[str, str]) -> Config:
    quota = environ.get("AYE_OFFSITE_QUOTA_BYTES", "").strip()
    max_age = environ.get("AYE_OFFSITE_MAX_AGE_SECONDS", "").strip()
    return Config(
        source=environ["AYE_OFFSITE_SOURCE"],
        host=environ["AYE_OFFSITE_HOST"],
        schedule=parse_schedule(environ.get("AYE_OFFSITE_SCHEDULE", "05:30")),
        timezone=environ.get("AYE_OFFSITE_TIMEZONE", "UTC"),
        policy=Policy(
            env_int(environ, "AYE_OFFSITE_KEEP_DAILY", 7),
            env_int(environ, "AYE_OFFSITE_KEEP_WEEKLY", 4),
            env_int(environ, "AYE_OFFSITE_KEEP_MONTHLY", 3),
        ),
        restore_every_days=env_int(environ, "AYE_OFFSITE_RESTORE_TEST_DAYS", 7),
        settle_seconds=env_int(environ, "AYE_OFFSITE_SETTLE_MINUTES", 15) * 60,
        retry_seconds=env_int(environ, "AYE_OFFSITE_RETRY_MINUTES", 60) * 60,
        verify=environ.get("AYE_OFFSITE_VERIFY", "pg_dump"),
        quota_bytes=int(quota) if quota else None,
        state_path=Path(environ.get("AYE_OFFSITE_STATE", "/state/state.json")),
        textfile=Path(environ.get("AYE_OFFSITE_TEXTFILE", "/textfile/ayeops_offsite.prom")),
        restic=environ.get("AYE_RESTIC", "/opt/restic/restic"),
        append_only=environ.get("AYE_OFFSITE_APPEND_ONLY") == "1",
        max_age_seconds=int(max_age) if max_age else None,
    )


@dataclass(frozen=True)
class Process:
    binary: str
    timeout: int = RESTIC_TIMEOUT

    def run(self, args: Sequence[str]) -> Done:
        try:
            done = subprocess.run(
                [self.binary, *args],
                capture_output=True,
                text=True,
                timeout=self.timeout,
                stdin=subprocess.DEVNULL,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return Done(124, "", f"restic {args[0]} timed out after {self.timeout}s")
        except OSError as error:
            return Done(127, "", f"cannot run {self.binary}: {error}")
        return Done(done.returncode, done.stdout, done.stderr)

    def stream(self, args: Sequence[str], sink: Callable[[bytes], None]) -> Done:
        try:
            child = subprocess.Popen(
                [self.binary, *args],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
            )
        except OSError as error:
            return Done(127, "", f"cannot run {self.binary}: {error}")
        assert child.stdout is not None and child.stderr is not None
        output, failure = child.stdout, child.stderr
        errors: list[bytes] = []
        reader = threading.Thread(target=lambda: errors.append(failure.read()), daemon=True)
        reader.start()
        expired = threading.Event()

        def expire() -> None:
            expired.set()
            child.kill()

        watchdog = threading.Timer(self.timeout, expire)
        watchdog.start()
        try:
            while chunk := output.read(CHUNK):
                sink(chunk)
            code = child.wait()
        except BaseException:
            child.kill()
            child.wait()
            raise
        finally:
            watchdog.cancel()
        reader.join()
        if expired.is_set():
            return Done(124, "", f"restic {args[0]} timed out after {self.timeout}s")
        return Done(code, "", b"".join(errors).decode("utf-8", "replace"))


def main() -> None:
    config = load_config(os.environ)
    runner = Pass(config, Process(config.restic))
    once = os.environ.get("AYE_OFFSITE_ONCE") == "1"
    while True:
        state = load_state(config.state_path)
        write_metrics(
            config.textfile, metrics_text(state, config.quota_bytes, config.max_age_seconds)
        )
        state = runner.tick(state)
        save_state(config.state_path, state)
        write_metrics(
            config.textfile, metrics_text(state, config.quota_bytes, config.max_age_seconds)
        )
        if once:
            return
        time.sleep(LOOP_SECONDS)


if __name__ == "__main__":
    main()
