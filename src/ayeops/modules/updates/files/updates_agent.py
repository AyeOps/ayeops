from __future__ import annotations

import json
import os
import re
import sys
import time
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

SCHEDULE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
PREFIX = "ayeops_updates_"
LAST_SUCCESS = re.compile(rf"^{PREFIX}last_success_timestamp_seconds\s+(\S+)\s*$", re.MULTILINE)
CLOSED = frozenset({"already-existed", "automerged"})
NOT_UPDATES = frozenset({"pin", "pinDigest", "lockFileMaintenance", "rollback"})
WARN = 40
ERROR = 50
NAP = 30
HEARTBEAT = Path("/tmp/updates.alive")


def parse_schedule(text: str) -> tuple[int, int]:
    match = SCHEDULE.match(text.strip())
    if match is None:
        raise ValueError(f"not an HH:MM time: {text!r}")
    return int(match.group(1)), int(match.group(2))


def next_run(now: float, hour: int, minute: int) -> float:
    today = time.localtime(now)

    def at(offset: int) -> float:
        return time.mktime(
            (today.tm_year, today.tm_mon, today.tm_mday + offset, hour, minute, 0, 0, 0, -1)
        )

    target = at(0)
    return target if target > now else at(1)


def timestamp(text: Any) -> float | None:
    if not isinstance(text, str) or not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.timestamp()


@dataclass
class RepoStats:
    present: bool = False
    open_prs: int = 0
    dependencies: int = 0
    pending: Counter[str] = field(default_factory=Counter)
    oldest_pending: float | None = None
    warnings: int = 0
    errors: int = 0


def open_prs(branches: Iterable[Mapping[str, Any]]) -> int:
    return sum(1 for b in branches if b.get("prNo") and b.get("result") not in CLOSED)


def real_updates(dep: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return [
        update
        for update in dep.get("updates") or []
        if isinstance(update, Mapping) and update.get("updateType") not in NOT_UPDATES
    ]


def repo_stats(repo: Mapping[str, Any]) -> RepoStats:
    stats = RepoStats(present=True)
    stats.open_prs = open_prs(b for b in repo.get("branches") or [] if isinstance(b, Mapping))
    for files in (repo.get("packageFiles") or {}).values():
        for package_file in files or []:
            for dep in package_file.get("deps") or []:
                if dep.get("skipReason"):
                    continue
                stats.dependencies += 1
                updates = real_updates(dep)
                if not updates:
                    continue
                datasource = str(dep.get("datasource") or "unknown")
                stats.pending[datasource] += 1
                released = [t for t in (timestamp(u.get("releaseTimestamp")) for u in updates) if t]
                if released and (
                    stats.oldest_pending is None or min(released) < stats.oldest_pending
                ):
                    stats.oldest_pending = min(released)
    for problem in repo.get("problems") or []:
        level = problem.get("level", 0) if isinstance(problem, Mapping) else 0
        if level >= ERROR:
            stats.errors += 1
        elif level >= WARN:
            stats.warnings += 1
    return stats


def report_stats(report: Mapping[str, Any], repositories: Iterable[str]) -> dict[str, RepoStats]:
    found = report.get("repositories") or {}
    return {
        name: repo_stats(found[name]) if isinstance(found.get(name), Mapping) else RepoStats()
        for name in repositories
    }


@dataclass(frozen=True)
class Run:
    started: float
    finished: float
    exit_code: int


def parse_run(text: str) -> Run | None:
    parts = text.split()
    if len(parts) != 3:
        return None
    try:
        return Run(float(parts[0]), float(parts[1]), int(parts[2]))
    except ValueError:
        return None


def run_succeeded(run: Run, stats: Mapping[str, RepoStats]) -> bool:
    return run.exit_code == 0 and all(s.present and s.errors == 0 for s in stats.values())


def metric(name: str, value: float, **labels: str) -> str:
    rendered = ",".join(f'{key}="{val}"' for key, val in sorted(labels.items()))
    suffix = "{" + rendered + "}" if rendered else ""
    number = int(value) if float(value).is_integer() else value
    return f"{PREFIX}{name}{suffix} {number}\n"


def render_metrics(run: Run, stats: Mapping[str, RepoStats], success_at: float) -> str:
    lines = [
        metric("last_run_timestamp_seconds", round(run.finished)),
        metric("last_run_success", int(run_succeeded(run, stats))),
        metric("last_run_exit_code", run.exit_code),
        metric("last_success_timestamp_seconds", round(success_at)),
        metric("run_duration_seconds", round(run.finished - run.started)),
    ]
    for name in sorted(stats):
        repo = stats[name]
        lines.append(
            metric("repository_ok", int(repo.present and repo.errors == 0), repository=name)
        )
        lines.append(metric("open_prs", repo.open_prs, repository=name))
        lines.append(metric("dependencies", repo.dependencies, repository=name))
        lines.append(metric("problems", repo.errors, repository=name, level="error"))
        lines.append(metric("problems", repo.warnings, repository=name, level="warn"))
        for datasource in sorted(repo.pending):
            lines.append(
                metric(
                    "pending_updates",
                    repo.pending[datasource],
                    repository=name,
                    datasource=datasource,
                )
            )
        if repo.oldest_pending is not None:
            lines.append(
                metric(
                    "oldest_pending_release_timestamp_seconds",
                    round(repo.oldest_pending),
                    repository=name,
                )
            )
    return "".join(lines)


def startup_metrics(success_at: float) -> str:
    return metric("last_success_timestamp_seconds", round(success_at))


def last_success(text: str) -> float | None:
    match = LAST_SUCCESS.search(text)
    if match is None:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def previous_success(path: Path, fallback: float) -> float:
    try:
        found = last_success(path.read_text(encoding="utf-8"))
    except OSError:
        found = None
    return fallback if found is None else found


def write_atomic(path: Path, text: str) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


@dataclass(frozen=True)
class Settings:
    schedule: tuple[int, int] = (6, 0)
    repositories: tuple[str, ...] = ()
    state: Path = Path("/state")
    textfile: Path = Path("/textfile/ayeops_updates.prom")

    @property
    def request(self) -> Path:
        return self.state / "run.request"

    @property
    def last(self) -> Path:
        return self.state / "run.last"

    @property
    def report(self) -> Path:
        return self.state / "report.json"


def settings_from(environ: Mapping[str, str]) -> Settings:
    repositories = json.loads(environ.get("AYE_UPDATES_REPOSITORIES", "[]"))
    if not isinstance(repositories, list) or not all(isinstance(r, str) for r in repositories):
        raise ValueError("AYE_UPDATES_REPOSITORIES must be a JSON list of strings")
    return Settings(
        schedule=parse_schedule(environ.get("AYE_UPDATES_SCHEDULE", "06:00")),
        repositories=tuple(repositories),
        state=Path(environ.get("AYE_UPDATES_STATE", "/state")),
        textfile=Path(environ.get("AYE_UPDATES_TEXTFILE", "/textfile/ayeops_updates.prom")),
    )


def read_report(path: Path) -> Mapping[str, Any]:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, Mapping) else {}


def record(settings: Settings, run: Run) -> dict[str, Any]:
    stats = report_stats(read_report(settings.report), settings.repositories)
    success = run_succeeded(run, stats)
    success_at = run.finished if success else previous_success(settings.textfile, run.finished)
    write_atomic(settings.textfile, render_metrics(run, stats, success_at))
    return {
        "event": "updates-run",
        "success": success,
        "exit_code": run.exit_code,
        "seconds": round(run.finished - run.started),
        "open_prs": {name: s.open_prs for name, s in sorted(stats.items())},
        "pending": {name: sum(s.pending.values()) for name, s in sorted(stats.items())},
        "missing": sorted(name for name, s in stats.items() if not s.present),
    }


def read_run(path: Path) -> tuple[Run | None, float]:
    try:
        return parse_run(path.read_text(encoding="utf-8")), path.stat().st_mtime
    except OSError:
        return None, 0.0


def request_run(settings: Settings) -> None:
    settings.request.write_text("", encoding="utf-8")


def prepare(settings: Settings, wall: float) -> None:
    settings.state.mkdir(parents=True, exist_ok=True)
    settings.state.chmod(0o777)
    if not settings.textfile.exists():
        write_atomic(settings.textfile, startup_metrics(wall))


def serve(
    settings: Settings,
    wall: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
    keep_going: Callable[[], bool] = lambda: True,
    heartbeat: Path = HEARTBEAT,
    emit: Callable[[dict[str, Any]], None] = lambda line: print(
        json.dumps(line, sort_keys=True), flush=True
    ),
) -> None:
    run, seen = read_run(settings.last)
    due = (
        wall()
        if run is None and not settings.request.exists()
        else next_run(wall(), *settings.schedule)
    )
    while keep_going():
        heartbeat.touch()
        now = wall()
        if now >= due:
            if not settings.request.exists():
                request_run(settings)
            due = next_run(now, *settings.schedule)
        run, mtime = read_run(settings.last)
        if run is not None and mtime > seen:
            seen = mtime
            emit(record(settings, run))
        sleep(max(1.0, min(NAP, due - wall())))


def main(environ: Mapping[str, str] = os.environ) -> int:
    settings = settings_from(environ)
    prepare(settings, time.time())
    if environ.get("AYE_UPDATES_ONCE") == "1":
        request_run(settings)
        return 0
    serve(settings)
    return 0


if __name__ == "__main__":
    sys.exit(main())
