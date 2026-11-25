from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

TIME_FORMAT = "%Y-%m-%d %H:%M UTC"


@dataclass(frozen=True)
class Stamp:
    time: str = ""
    commit: str = ""


def build_time(now: datetime | None = None) -> str:
    epoch = os.environ.get("SOURCE_DATE_EPOCH", "").strip()
    if now is None:
        now = datetime.fromtimestamp(int(epoch), UTC) if epoch.isdigit() else datetime.now(UTC)
    return now.astimezone(UTC).strftime(TIME_FORMAT)


def _git(directory: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(directory), *args],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip()


def manifest_commit(manifest_path: Path) -> str:
    directory = manifest_path.resolve().parent
    sha = _git(directory, "rev-parse", "--short", "HEAD")
    if not sha:
        return ""
    dirty = _git(directory, "status", "--porcelain")
    return f"{sha}+" if dirty else sha


def current_stamp(manifest_path: Path, time: str | None = None, commit: str | None = None) -> Stamp:
    return Stamp(
        time=build_time() if time is None else time,
        commit=manifest_commit(manifest_path) if commit is None else commit,
    )
