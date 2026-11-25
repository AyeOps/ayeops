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
PREFIX = "ayeops_vulns_"
LAST_SUCCESS = re.compile(rf"^{PREFIX}last_success_timestamp_seconds\s+(\S+)\s*$", re.MULTILINE)
SEVERITIES = ("critical", "high", "medium", "low", "unknown")
NAP = 30
HEARTBEAT = Path("/tmp/vulns.alive")


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


def severity_of(finding: Mapping[str, Any]) -> str:
    severity = str(finding.get("Severity") or "").lower()
    return severity if severity in SEVERITIES else "unknown"


def findings(report: Mapping[str, Any]) -> set[tuple[str, str, str, str, bool]]:
    found: set[tuple[str, str, str, str, bool]] = set()
    for result in report.get("Results") or []:
        if not isinstance(result, Mapping):
            continue
        for finding in result.get("Vulnerabilities") or []:
            if not isinstance(finding, Mapping) or not finding.get("VulnerabilityID"):
                continue
            found.add(
                (
                    str(finding["VulnerabilityID"]),
                    str(finding.get("PkgID") or finding.get("PkgName") or ""),
                    str(finding.get("PkgPath") or ""),
                    severity_of(finding),
                    bool(finding.get("FixedVersion")),
                )
            )
    return found


def counts(report: Mapping[str, Any]) -> Counter[tuple[str, bool]]:
    return Counter((severity, fixed) for _, _, _, severity, fixed in findings(report))


@dataclass(frozen=True)
class Run:
    started: float
    finished: float
    db_code: int


def parse_run(text: str) -> Run | None:
    parts = text.split()
    if len(parts) != 3:
        return None
    try:
        return Run(float(parts[0]), float(parts[1]), int(parts[2]))
    except ValueError:
        return None


@dataclass(frozen=True)
class Scan:
    image: str
    ok: bool
    exit_code: int
    counts: Counter[tuple[str, bool]] = field(default_factory=Counter)


def parse_results(text: str) -> dict[str, tuple[str, int]]:
    results: dict[str, tuple[str, int]] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) != 3:
            continue
        key, code, image = parts
        try:
            results[image] = (key, int(code))
        except ValueError:
            continue
    return results


def read_report(path: Path) -> Mapping[str, Any] | None:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(loaded, Mapping) or not isinstance(loaded.get("Results", []), list):
        return None
    return loaded


def scans(reports: Path, images: Iterable[str]) -> list[Scan]:
    try:
        results = parse_results((reports / "results").read_text(encoding="utf-8"))
    except OSError:
        results = {}
    found = []
    for image in sorted(set(images)):
        key, code = results.get(image, ("", -1))
        report = read_report(reports / f"{key}.json") if key and code == 0 else None
        if report is None:
            found.append(Scan(image, False, code))
        else:
            found.append(Scan(image, True, code, counts(report)))
    return found


def run_succeeded(run: Run, found: Iterable[Scan]) -> bool:
    return run.db_code == 0 and all(scan.ok for scan in found)


def quoted(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def metric(name: str, value: float, **labels: str) -> str:
    rendered = ",".join(f'{key}="{quoted(val)}"' for key, val in sorted(labels.items()))
    suffix = "{" + rendered + "}" if rendered else ""
    number = int(value) if float(value).is_integer() else value
    return f"{PREFIX}{name}{suffix} {number}\n"


def render_metrics(run: Run, found: list[Scan], success_at: float, db_updated: float | None) -> str:
    lines = [
        metric("last_run_timestamp_seconds", round(run.finished)),
        metric("last_run_success", int(run_succeeded(run, found))),
        metric("last_success_timestamp_seconds", round(success_at)),
        metric("run_duration_seconds", round(run.finished - run.started)),
        metric("db_update_success", int(run.db_code == 0)),
        metric("images", sum(1 for scan in found if scan.ok), result="ok"),
        metric("images", sum(1 for scan in found if not scan.ok), result="failed"),
    ]
    if db_updated is not None:
        lines.append(metric("db_updated_timestamp_seconds", round(db_updated)))
    for scan in found:
        lines.append(metric("image_scan_success", int(scan.ok), image=scan.image))
        if not scan.ok:
            continue
        for severity in SEVERITIES:
            for fixed in (True, False):
                lines.append(
                    metric(
                        "vulnerabilities",
                        scan.counts[(severity, fixed)],
                        image=scan.image,
                        severity=severity,
                        fixed=str(fixed).lower(),
                    )
                )
    return "".join(lines)


def startup_metrics(wall: float) -> str:
    return metric("last_run_timestamp_seconds", round(wall)) + metric(
        "last_success_timestamp_seconds", round(wall)
    )


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


def db_updated(path: Path) -> float | None:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return timestamp(loaded.get("UpdatedAt")) if isinstance(loaded, Mapping) else None


def write_atomic(path: Path, text: str) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


@dataclass(frozen=True)
class Settings:
    schedule: tuple[int, int] = (5, 0)
    images: tuple[str, ...] = ()
    state: Path = Path("/state")
    textfile: Path = Path("/textfile/ayeops_vulns.prom")
    db: Path = Path("/cache/db/metadata.json")

    @property
    def request(self) -> Path:
        return self.state / "run.request"

    @property
    def last(self) -> Path:
        return self.state / "run.last"

    @property
    def reports(self) -> Path:
        return self.state / "reports"


def settings_from(environ: Mapping[str, str]) -> Settings:
    images = json.loads(environ.get("AYE_VULNS_IMAGES", "[]"))
    if not isinstance(images, list) or not all(
        isinstance(i, str) and i and not any(c.isspace() for c in i) for i in images
    ):
        raise ValueError("AYE_VULNS_IMAGES must be a JSON list of image references")
    return Settings(
        schedule=parse_schedule(environ.get("AYE_VULNS_SCHEDULE", "05:00")),
        images=tuple(images),
        state=Path(environ.get("AYE_VULNS_STATE", "/state")),
        textfile=Path(environ.get("AYE_VULNS_TEXTFILE", "/textfile/ayeops_vulns.prom")),
        db=Path(environ.get("AYE_VULNS_DB", "/cache/db/metadata.json")),
    )


def record(settings: Settings, run: Run) -> dict[str, Any]:
    found = scans(settings.reports, settings.images)
    success = run_succeeded(run, found)
    success_at = run.finished if success else previous_success(settings.textfile, run.finished)
    write_atomic(settings.textfile, render_metrics(run, found, success_at, db_updated(settings.db)))
    return {
        "event": "vulns-run",
        "success": success,
        "db_update": run.db_code == 0,
        "seconds": round(run.finished - run.started),
        "failed": [scan.image for scan in found if not scan.ok],
        "critical_fixable": {
            scan.image: scan.counts[("critical", True)]
            for scan in found
            if scan.counts[("critical", True)]
        },
    }


def read_run(path: Path) -> tuple[Run | None, float]:
    try:
        return parse_run(path.read_text(encoding="utf-8")), path.stat().st_mtime
    except OSError:
        return None, 0.0


def request_text(images: Iterable[str]) -> str:
    return "".join(f"{index:03d} {image}\n" for index, image in enumerate(sorted(set(images))))


def request_run(settings: Settings) -> None:
    write_atomic(settings.request, request_text(settings.images))


def scanned_last(settings: Settings) -> set[str]:
    try:
        return set(parse_results((settings.reports / "results").read_text(encoding="utf-8")))
    except OSError:
        return set()


def first_due(settings: Settings, run: Run | None, now: float) -> float:
    if settings.request.exists():
        return next_run(now, *settings.schedule)
    if run is None or scanned_last(settings) != set(settings.images):
        return now
    return next_run(now, *settings.schedule)


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
    due = first_due(settings, run, wall())
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
    if environ.get("AYE_VULNS_ONCE") == "1":
        request_run(settings)
        return 0
    serve(settings)
    return 0


if __name__ == "__main__":
    sys.exit(main())
