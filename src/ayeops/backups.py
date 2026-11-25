from __future__ import annotations

import re
import shlex
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from itertools import pairwise
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import jinja2

from ayeops.manifest import Manifest
from ayeops.registry import load_modules

MODULE = "backups"
FIELDS = (
    ("minute", 0, 59),
    ("hour", 0, 23),
    ("day of month", 1, 31),
    ("month", 1, 12),
    ("day of week", 0, 7),
)
NUMBER = re.compile(r"^[0-9]{1,2}$")
GRACE = re.compile(r"^([1-9][0-9]*)([mh])$")
GRACE_SECONDS = {"m": 60, "h": 3600}
DEFAULT_GRACE = "2h"
WINDOW_START = date(2027, 1, 1)
WINDOW_DAYS = 1461
HOST_ROOT = "/opt/ayeops"
UNITS = "backups/systemd"
SCRIPTS = "backups/jobs"
WEEKDAYS = ("Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat")
WEEK = (1, 2, 3, 4, 5, 6, 0)
EVERY_WEEKDAY = frozenset(range(7))
TEMPLATES = jinja2.Environment(autoescape=False, undefined=jinja2.StrictUndefined)


class CronError(ValueError):
    pass


@dataclass(frozen=True)
class Cron:
    minutes: frozenset[int]
    hours: frozenset[int]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]
    any_day: bool
    any_weekday: bool

    def matches(self, day: date) -> bool:
        if day.month not in self.months:
            return False
        on_day = day.day in self.days
        on_weekday = day.isoweekday() % 7 in self.weekdays
        if self.any_day or self.any_weekday:
            return on_day and on_weekday
        return on_day or on_weekday

    def times(self) -> list[int]:
        return sorted(hour * 60 + minute for hour in self.hours for minute in self.minutes)


def number(text: str, name: str) -> int:
    if not NUMBER.match(text):
        raise CronError(f"{name}: {text!r} is not a number")
    return int(text)


def parse_field(text: str, name: str, low: int, high: int) -> frozenset[int]:
    values: set[int] = set()
    for part in text.split(","):
        base, slash, step_text = part.partition("/")
        step = number(step_text, name) if slash else 1
        if step < 1:
            raise CronError(f"{name}: the step of {part!r} must be at least 1")
        if base == "*":
            start, end = low, high
        elif "-" in base:
            first, _, last = base.partition("-")
            start, end = number(first, name), number(last, name)
        else:
            start = number(base, name)
            end = high if slash else start
        if not low <= start <= end <= high:
            raise CronError(f"{name}: {part!r} is outside {low}-{high}")
        values.update(range(start, end + 1, step))
    return frozenset(values)


def parse_cron(text: str) -> Cron:
    fields = text.split()
    if len(fields) != len(FIELDS):
        raise CronError(f"{text!r} has {len(fields)} fields, a cron schedule has 5")
    parsed = [
        parse_field(field, name, low, high)
        for field, (name, low, high) in zip(fields, FIELDS, strict=True)
    ]
    weekdays = frozenset(day % 7 for day in parsed[4])
    return Cron(
        parsed[0],
        parsed[1],
        parsed[2],
        parsed[3],
        weekdays,
        fields[2].startswith("*"),
        fields[4].startswith("*"),
    )


def stamp(day: date, minute: int, zone: ZoneInfo) -> int:
    moment = datetime(day.year, day.month, day.day, minute // 60, minute % 60, tzinfo=zone)
    return int(moment.timestamp())


def shifts(day: date, zone: ZoneInfo) -> bool:
    start = datetime(day.year, day.month, day.day, tzinfo=zone)
    following = day + timedelta(days=1)
    end = datetime(following.year, following.month, following.day, tzinfo=zone)
    return start.utcoffset() != end.utcoffset()


def longest_gap(cron: Cron, zone: ZoneInfo) -> int | None:
    times = cron.times()
    steady = max((b - a for a, b in pairwise(times)), default=0) * 60
    gap = 0
    last: int | None = None
    days = 0
    for offset in range(WINDOW_DAYS):
        day = WINDOW_START + timedelta(days=offset)
        if not cron.matches(day):
            continue
        days += 1
        if shifts(day, zone):
            stamps = [stamp(day, minute, zone) for minute in times]
            gap = max([gap, *(b - a for a, b in pairwise(stamps))])
        else:
            stamps = [stamp(day, times[0], zone), stamp(day, times[-1], zone)]
            gap = max(gap, steady)
        if last is not None:
            gap = max(gap, stamps[0] - last)
        last = stamps[-1]
    return gap if days >= 2 else None


def grace_seconds(text: str) -> int:
    found = GRACE.match(text)
    if found is None:
        raise ValueError(f"grace {text!r} is not minutes or hours like 30m or 2h")
    return int(found.group(1)) * GRACE_SECONDS[found.group(2)]


def max_age(job: dict[str, Any], timezone: str) -> int | None:
    gap = longest_gap(parse_cron(str(job["schedule"])), ZoneInfo(timezone))
    if gap is None:
        return None
    return gap + grace_seconds(str(job.get("grace") or DEFAULT_GRACE))


@dataclass(frozen=True)
class Job:
    name: str
    schedule: str
    timezone: str
    grace: str = DEFAULT_GRACE
    command: str | None = None
    output: str | None = None
    module: str = MODULE
    service: str | None = None

    def age(self) -> int | None:
        return max_age({"schedule": self.schedule, "grace": self.grace}, self.timezone)


def manifest_jobs(settings: dict[str, Any]) -> list[Job]:
    zone = str(settings.get("timezone") or "UTC")
    return [
        Job(
            str(name),
            str(job.get("schedule")),
            zone,
            str(job.get("grace") or DEFAULT_GRACE),
            job.get("command"),
            job.get("output"),
        )
        for name, job in sorted((settings.get("jobs") or {}).items())
    ]


def builtin_jobs(all_settings: dict[str, Any]) -> list[Job]:
    modules = load_modules()
    jobs: list[Job] = []
    for name, settings in sorted(all_settings.items()):
        module = modules.get(name)
        if module is None:
            continue
        for entry in module.backup_jobs:
            schedule = TEMPLATES.from_string(entry["schedule"]).render(settings=settings)
            jobs.append(
                Job(
                    entry["name"],
                    schedule,
                    entry["timezone"],
                    entry.get("grace") or DEFAULT_GRACE,
                    module=name,
                    service=entry["service"],
                )
            )
    return jobs


def host_jobs(all_settings: dict[str, Any]) -> list[Job]:
    settings = all_settings.get(MODULE)
    if settings is None:
        return []
    return manifest_jobs(settings) + builtin_jobs(all_settings)


def job_ages(jobs: list[Job]) -> dict[str, int]:
    ages: dict[str, int] = {}
    for job in sorted(jobs, key=lambda job: job.name):
        try:
            age = job.age()
        except (ValueError, ZoneInfoNotFoundError):
            continue
        if age is not None:
            ages[job.name] = age
    return ages


def backups_context(
    all_settings: dict[str, Any], rpos: dict[str, int] | None = None
) -> dict[str, Any]:
    if MODULE not in all_settings:
        return {}
    limits = rpos or {}
    ages = {
        name: min(age, limits.get(name, age))
        for name, age in job_ages(host_jobs(all_settings)).items()
    }
    declared = {name: limits[name] for name in ages if name in limits}
    return {
        "ages": ages,
        "jobs": " ".join(f"{name}={age}" for name, age in ages.items()),
        "rpo": " ".join(f"{name}={seconds}" for name, seconds in declared.items()),
    }


def calendar_field(values: frozenset[int], low: int, high: int) -> str:
    if values >= frozenset(range(low, high + 1)):
        return "*"
    return ",".join(f"{value:02d}" for value in sorted(values))


def calendar_line(cron: Cron, weekdays: frozenset[int], days: str, zone: str) -> str:
    months = calendar_field(cron.months, 1, 12)
    hours = calendar_field(cron.hours, 0, 23)
    minutes = calendar_field(cron.minutes, 0, 59)
    moment = f"*-{months}-{days} {hours}:{minutes}:00 {zone}"
    if weekdays >= EVERY_WEEKDAY:
        return moment
    return ",".join(WEEKDAYS[day] for day in WEEK if day in weekdays) + " " + moment


def on_calendar(cron: Cron, zone: str) -> list[str]:
    days = calendar_field(cron.days, 1, 31)
    if cron.any_day or cron.any_weekday:
        return [calendar_line(cron, cron.weekdays, days, zone)]
    lines = [
        calendar_line(cron, EVERY_WEEKDAY, days, zone),
        calendar_line(cron, cron.weekdays, "*", zone),
    ]
    return list(dict.fromkeys(lines))


def unit_name(host: str, job: str) -> str:
    return f"ayeops-{host}-backup-{job}"


def job_script(host: str, job: Job, textfile: str | None) -> str:
    words = ["exec", "/bin/sh", f"{HOST_ROOT}/{host}/backups/backup_run.sh"]
    if textfile:
        words += ["-d", shlex.quote(textfile)]
    words += [
        "-o",
        '"$AYE_BACKUP_OUTPUT"',
        job.name,
        "/bin/sh",
        "-c",
        shlex.quote(str(job.command)),
    ]
    return (
        "#!/bin/sh\nset -eu\n"
        f'AYE_BACKUP_OUTPUT="{job.output}"\n'
        "export AYE_BACKUP_OUTPUT\n" + " ".join(words) + "\n"
    )


def service_unit(host: str, job: Job) -> str:
    return (
        "[Unit]\n"
        f"Description=AyeOps backup job {job.name} of {host}\n"
        "After=docker.service network-online.target\n"
        "\n[Service]\n"
        "Type=oneshot\n"
        f"ExecStart=/bin/sh {HOST_ROOT}/{host}/{SCRIPTS}/{job.name}.sh\n"
        "Nice=10\n"
        "IOSchedulingClass=best-effort\n"
        "IOSchedulingPriority=7\n"
    )


def timer_unit(host: str, job: Job, calendar: list[str]) -> str:
    return (
        "[Unit]\n"
        f"Description=Schedule of the AyeOps backup job {job.name} of {host}\n"
        "\n[Timer]\n" + "".join(f"OnCalendar={line}\n" for line in calendar) + "Persistent=true\n"
        f"Unit={unit_name(host, job.name)}.service\n"
        "\n[Install]\n"
        "WantedBy=timers.target\n"
    )


def backup_units(host: str, all_settings: dict[str, Any]) -> dict[str, str]:
    settings = all_settings.get(MODULE)
    if settings is None:
        return {}
    textfile = (all_settings.get("node") or {}).get("textfile_dir")
    files: dict[str, str] = {}
    for job in manifest_jobs(settings):
        if not job.command or not job.output:
            continue
        try:
            cron = parse_cron(job.schedule)
        except CronError:
            continue
        unit = unit_name(host, job.name)
        files[f"{SCRIPTS}/{job.name}.sh"] = job_script(host, job, textfile)
        files[f"{UNITS}/{unit}.service"] = service_unit(host, job)
        files[f"{UNITS}/{unit}.timer"] = timer_unit(host, job, on_calendar(cron, job.timezone))
    return dict(sorted(files.items()))


def unit_files(paths: Iterable[str]) -> list[str]:
    return sorted(path for path in paths if path.startswith(f"{UNITS}/"))


def job_problems(path: str, job: Job) -> list[tuple[str, str, str]]:
    try:
        cron = parse_cron(job.schedule)
    except CronError as error:
        return [(f"{path}.schedule", "backup-schedule", f"not a cron schedule: {error}")]
    try:
        zone = ZoneInfo(job.timezone)
    except (ValueError, ZoneInfoNotFoundError):
        return []
    if longest_gap(cron, zone) is None:
        return [
            (
                f"{path}.schedule",
                "backup-schedule",
                f"{job.schedule!r} fires less than twice in four years: nothing could tell a"
                " missing run from a late one",
            )
        ]
    return []


def backups_problems(manifest: Manifest) -> list[tuple[str, str, str]]:
    problems: list[tuple[str, str, str]] = []
    registry = load_modules()
    for host in sorted(manifest.hosts):
        modules = manifest.host_modules(host)
        if MODULE not in modules:
            continue
        path = f"hosts.{host}.modules.{MODULE}"
        all_settings = {
            name: registry[name].settings(settings, manifest.timezone)
            for name, settings in modules.items()
            if name in registry
        }
        jobs = manifest_jobs(all_settings[MODULE])
        node = all_settings.get("node")
        if jobs and not (isinstance(node, dict) and node.get("textfile_dir")):
            problems.append(
                (
                    path,
                    "backup-textfile",
                    "the dump jobs of the manifest run on the host and write their metrics into"
                    " the node textfile directory: set node.textfile_dir on this host",
                )
            )
        builtin = {job.name: job for job in builtin_jobs(all_settings)}
        for job in jobs:
            where = f"{path}.jobs.{job.name}"
            if job.name in builtin:
                owner = builtin[job.name]
                problems.append(
                    (
                        where,
                        "backup-name",
                        f"{job.name} is the built-in dump of the {owner.module} module"
                        f" ({owner.service}) on this host: pick another name",
                    )
                )
            problems += job_problems(where, job)
    return problems
