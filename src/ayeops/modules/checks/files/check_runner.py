from __future__ import annotations

import json
import math
import os
import re
import subprocess
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

NOBODY = 65534
TIMED_OUT = 124
NOT_STARTED = 127
VALUE = re.compile(r"^([a-zA-Z_:][a-zA-Z0-9_:]*)\s+(\S+)\s*$")
OUTPUT_LIMIT = 65536
WRITE_EVERY = 15

Run = Callable[..., Any]


@dataclass
class Result:
    exit_code: int
    duration: float
    finished: float
    values: dict[str, float] = field(default_factory=dict)


def parse_values(stdout: str) -> dict[str, float]:
    values: dict[str, float] = {}
    for line in stdout[:OUTPUT_LIMIT].splitlines():
        match = VALUE.match(line.strip())
        if match is None:
            continue
        try:
            number = float(match.group(2))
        except ValueError:
            continue
        if math.isfinite(number):
            values[match.group(1)] = number
    return values


def child_env(names: Sequence[str], environ: Mapping[str, str]) -> dict[str, str]:
    env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/tmp", "LANG": "C.UTF-8"}
    env.update({name: environ[name] for name in names if name in environ})
    return env


def run_check(
    check: Mapping[str, Any],
    run: Run = subprocess.run,
    user: int | None = None,
    environ: Mapping[str, str] = os.environ,
    clock: Callable[[], float] = time.monotonic,
    wall: Callable[[], float] = time.time,
) -> Result:
    started = clock()
    options: dict[str, Any] = {
        "capture_output": True,
        "text": True,
        "timeout": float(check["timeout"]),
        "env": child_env(check.get("env") or [], environ),
        "cwd": "/tmp",
        "stdin": subprocess.DEVNULL,
    }
    if user is not None:
        options.update(user=user, group=user, extra_groups=[])
    stdout = ""
    try:
        done = run(["/bin/sh", "-c", str(check["command"])], **options)
        code = int(done.returncode)
        stdout = str(done.stdout or "")
    except subprocess.TimeoutExpired:
        code = TIMED_OUT
    except OSError:
        code = NOT_STARTED
    return Result(code, clock() - started, wall(), parse_values(stdout) if code == 0 else {})


def label(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def render_metrics(
    checks: Sequence[Mapping[str, Any]],
    results: Mapping[str, Result],
    runner: str,
    now: float,
) -> str:
    lines = [f'ayeops_checks_heartbeat_timestamp_seconds{{runner="{label(runner)}"}} {now:.0f}']
    for check in sorted(checks, key=lambda c: str(c["check"])):
        result = results.get(str(check["check"]))
        if result is None:
            continue
        labels = f'check="{label(str(check["check"]))}",service="{label(str(check["service"]))}"'
        lines.append(f"ayeops_check_success{{{labels}}} {1 if result.exit_code == 0 else 0}")
        lines.append(f"ayeops_check_exit_code{{{labels}}} {result.exit_code}")
        lines.append(f"ayeops_check_duration_seconds{{{labels}}} {result.duration:.3f}")
        lines.append(f"ayeops_check_last_run_timestamp_seconds{{{labels}}} {result.finished:.0f}")
        for name, value in sorted(result.values.items()):
            lines.append(f'ayeops_check_value{{{labels},name="{label(name)}"}} {value:g}')
    return "\n".join(lines) + "\n"


def write_atomic(path: Path, text: str) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


class Runner:
    def __init__(self, checks: Sequence[Mapping[str, Any]], textfile: Path, name: str) -> None:
        self.checks = list(checks)
        self.textfile = textfile
        self.name = name
        self.results: dict[str, Result] = {}
        self.lock = threading.Lock()

    def record(self, check: Mapping[str, Any], result: Result) -> None:
        with self.lock:
            self.results[str(check["check"])] = result
        self.write()

    def write(self) -> None:
        with self.lock:
            text = render_metrics(self.checks, self.results, self.name, time.time())
            write_atomic(self.textfile, text)


def loop_check(runner: Runner, check: Mapping[str, Any], user: int | None) -> None:
    while True:
        started = time.monotonic()
        runner.record(check, run_check(check, user=user))
        time.sleep(max(1.0, float(check["interval"]) - (time.monotonic() - started)))


def main() -> None:
    checks = json.loads(Path(os.environ["AYE_CHECKS_CONFIG"]).read_text(encoding="utf-8"))
    textfile = Path(os.environ["AYE_CHECKS_TEXTFILE"])
    runner = Runner(checks, textfile, os.environ["AYE_CHECKS_RUNNER"])
    user = NOBODY if getattr(os, "geteuid", lambda: -1)() == 0 else None
    for check in checks:
        threading.Thread(target=loop_check, args=(runner, check, user), daemon=True).start()
    while True:
        runner.write()
        time.sleep(WRITE_EVERY)


if __name__ == "__main__":
    main()
