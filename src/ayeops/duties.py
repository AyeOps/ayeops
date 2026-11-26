from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ayeops.manifest import Manifest, load_manifest, schema_errors, version_problems
from ayeops.waivers import parse_day, today_in

ALERT = "duty-overdue"
TITLE = "A recurring duty is overdue"
RUNBOOK = "https://github.com/AyeOps/ayeops/blob/main/docs/runbooks/duty-overdue.md"
MAX_EVERY = 3660
SCOPE = "{{ $labels.duty }}"
SUMMARY = (
    "{{ .Labels.title }} ({{ .Labels.duty }}, every {{ .Labels.every }}) was due on"
    " {{ .Labels.due }}{{ if .Labels.owner }}, owner {{ .Labels.owner }}{{ end }}"
)
OWN_RUNBOOK = "{{ if $labels.runbook }}{{ $labels.runbook }}{{ else }}" + RUNBOOK + "{{ end }}"

Problem = tuple[str, str, str]


@dataclass(frozen=True)
class Duty:
    name: str
    title: str
    every: str
    last_done: Any
    owner: str = ""
    runbook: str = ""

    @property
    def days(self) -> int:
        return int(self.every.removesuffix("d"))

    @property
    def due(self) -> date:
        return parse_day(self.last_done, "last_done") + timedelta(days=self.days)


def manifest_duties(manifest: Manifest) -> list[Duty]:
    return [
        Duty(
            str(name),
            str(spec["title"]),
            str(spec["every"]),
            spec["last_done"],
            str(spec.get("owner") or ""),
            str(spec.get("runbook") or ""),
        )
        for name, spec in sorted((manifest.data.get("duties") or {}).items())
    ]


def duty_problems(manifest: Manifest, today: date) -> list[Problem]:
    problems: list[Problem] = []
    for duty in manifest_duties(manifest):
        path = f"duties.{duty.name}"
        if duty.days > MAX_EVERY:
            problems.append(
                (
                    f"{path}.every",
                    "duty-every",
                    f"every is at most {MAX_EVERY}d, ten years, not {duty.every}",
                )
            )
        try:
            done = parse_day(duty.last_done, "last_done")
        except ValueError as error:
            problems.append((f"{path}.last_done", "duty-done", str(error)))
            continue
        if done > today:
            problems.append(
                (
                    f"{path}.last_done",
                    "duty-done",
                    f"last_done {done.isoformat()} is after today, {today.isoformat()}:"
                    " record a duty on the day it is done",
                )
            )
    return problems


def zone_of(name: str) -> tzinfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return UTC


def overdue_at(duty: Duty, zone: str) -> int:
    start = datetime.combine(duty.due + timedelta(days=1), time(), tzinfo=zone_of(zone))
    return int(start.timestamp())


def duty_labels(duty: Duty, base: str) -> list[tuple[str, str]]:
    from ayeops.render import runbook_url

    found = [
        ("duty", duty.name),
        ("title", duty.title),
        ("due", duty.due.isoformat()),
        ("every", duty.every),
    ]
    if duty.owner:
        found.append(("owner", duty.owner))
    if duty.runbook:
        found.append(("runbook", runbook_url(base, duty.runbook)))
    return found


def duty_series(duty: Duty, zone: str, base: str) -> str:
    expr = f"vector(time() >= bool {overdue_at(duty, zone)})"
    for label, value in duty_labels(duty, base):
        expr = f'label_replace({expr}, "{label}", "{value}", "", "")'
    return expr


def overdue_alert(manifest: Manifest) -> dict[str, Any] | None:
    duties = manifest_duties(manifest)
    if not duties:
        return None
    zone = manifest.timezone
    base = manifest.runbook_base_url
    return {
        "name": ALERT,
        "title": TITLE,
        "expr": " or ".join(duty_series(duty, zone, base) for duty in duties),
        "for": "1h",
        "severity": "warning",
        "summary": SUMMARY,
        "runbook": RUNBOOK,
    }


def runbook_annotation(manifest: Manifest) -> str:
    if any(duty.runbook for duty in manifest_duties(manifest)):
        return OWN_RUNBOOK
    return RUNBOOK


def plural(count: int) -> str:
    return f"{count} day" if count == 1 else f"{count} days"


def state(duty: Duty, today: date) -> str:
    left = (duty.due - today).days
    day = duty.due.isoformat()
    if left < 0:
        return f"overdue, was due on {day}, {plural(-left)} ago"
    if left == 0:
        return f"due today, {day}"
    return f"due on {day}, in {plural(left)}"


def duty_line(duty: Duty, today: date) -> str:
    owner = f", owner {duty.owner}" if duty.owner else ""
    return f"{duty.name}: {state(duty, today)} (every {duty.every}{owner}): {duty.title}"


def listing_problems(manifest: Manifest, today: date) -> list[Problem]:
    pinned = version_problems(manifest.raw)
    if pinned:
        return pinned
    errors = [(path, "schema", message) for path, message in schema_errors(manifest.raw)]
    return errors or duty_problems(manifest, today)


def run(manifest_path: Path, today: date | None = None) -> int:
    manifest = load_manifest(manifest_path)
    day = today or today_in(manifest.timezone)
    problems = listing_problems(manifest, day)
    for path, rule, message in problems:
        print(f"error [{rule}] {path}: {message}", file=sys.stderr)
    if problems:
        print(f"{manifest_path}: no duties listed, fix the errors first", file=sys.stderr)
        return 1
    duties = sorted(manifest_duties(manifest), key=lambda duty: duty.due)
    if not duties:
        print(f"{manifest_path}: no duties")
        return 0
    for duty in duties:
        print(duty_line(duty, day))
    late = [duty for duty in duties if duty.due < day]
    if late:
        print(f"{manifest_path}: {len(late)} of {len(duties)} duty(s) overdue", file=sys.stderr)
        return 1
    print(f"{manifest_path}: {len(duties)} duty(s), none overdue")
    return 0
