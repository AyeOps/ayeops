from __future__ import annotations

import posixpath
import re
from collections.abc import Callable
from dataclasses import dataclass
from functools import cache
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

from ayeops.backups import Job, host_jobs, longest_gap, parse_cron
from ayeops.manifest import Manifest
from ayeops.offsite import GLOB, REST_SERVER, drill_settings, objectstore_settings, source_dir
from ayeops.registry import load_modules

HOST = "host"
OFFSITE = "offsite"
CRITICAL = "critical"
DEFAULT_COPIES = (HOST,)
DURATION = re.compile(r"^([1-9][0-9]*)([mhd])$")
DURATION_SECONDS = {"m": 60, "h": 3600, "d": 86400}
OFFSITE_STALE_SECONDS = 93600
DATABASES = frozenset({"postgres", "postgresql", "mysql", "mariadb", "mongo", "mongodb", "mongod"})

Problem = tuple[str, str, str]
ComposeServices = Callable[[Manifest, str], dict[str, dict[str, Any]]]


@dataclass(frozen=True)
class Copy:
    name: str
    place: str
    zone: str | None
    provider: str | None
    outside: bool = False
    append_only: bool = False
    drill_minutes: int | None = None


def duration_seconds(text: Any) -> int:
    found = DURATION.match(str(text))
    if found is None:
        raise ValueError(f"{text!r} is not a duration like 30m, 26h or 2d")
    return int(found.group(1)) * DURATION_SECONDS[found.group(2)]


def spoken(seconds: int) -> str:
    if seconds % 3600:
        return f"{seconds // 60}m"
    return f"{seconds // 3600}h"


def declarations(manifest: Manifest) -> list[tuple[str, dict[str, Any], dict[str, Any]]]:
    return [
        (name, entry, entry["data"])
        for name, entry in sorted(manifest.active_services.items())
        if isinstance(entry.get("data"), dict) and entry.get("host") in manifest.hosts
    ]


def copies_of(data: dict[str, Any]) -> list[str]:
    return [str(copy) for copy in data.get("copies") or DEFAULT_COPIES]


def merged_modules(manifest: Manifest, host: str) -> dict[str, Any]:
    registry = load_modules()
    return {
        name: registry[name].settings(raw, manifest.timezone)
        for name, raw in manifest.host_modules(host).items()
        if name in registry
    }


def job_age(job: Job) -> int | None:
    try:
        return job.age()
    except (ValueError, ZoneInfoNotFoundError):
        return None


@cache
def daily_gap(schedule: str, timezone: str) -> int | None:
    hour, _, minute = schedule.partition(":")
    return longest_gap(parse_cron(f"{int(minute)} {int(hour)} * * *"), ZoneInfo(timezone))


def offsite_age(offsite: dict[str, Any]) -> int | None:
    try:
        gap = daily_gap(str(offsite["schedule"]), str(offsite["timezone"]))
    except (ValueError, ZoneInfoNotFoundError):
        return None
    if gap is None:
        return None
    return gap + int(offsite["retry_minutes"]) * 60


def place_of(manifest: Manifest, host: str) -> tuple[str | None, str | None]:
    spec = manifest.hosts.get(host) or {}
    zone, provider = spec.get("zone"), spec.get("provider")
    return (str(zone) if zone else None, str(provider) if provider else None)


def offsite_copy(manifest: Manifest, offsite: dict[str, Any]) -> Copy:
    target = offsite.get("target") or {}
    if "url" in target:
        url = str(target["url"])
        return Copy(OFFSITE, url, None, None, True, bool(target.get("append_only")))
    central = str(target.get("host") or "")
    zone, provider = place_of(manifest, central)
    store = objectstore_settings(manifest, central)
    if store is None or store["provider"] != REST_SERVER:
        return Copy(OFFSITE, central, zone, provider)
    drill = drill_settings(offsite)
    minutes = int(drill["max_minutes"]) if drill else None
    return Copy(OFFSITE, central, zone, provider, False, True, minutes)


def resolve_copies(
    manifest: Manifest, host: str, data: dict[str, Any], modules: dict[str, Any]
) -> list[Copy]:
    found = []
    for name in copies_of(data):
        if name == HOST:
            found.append(Copy(HOST, host, *place_of(manifest, host)))
        elif OFFSITE in modules:
            found.append(offsite_copy(manifest, modules[OFFSITE]))
    return found


def job_problems(
    path: str, host: str, data: dict[str, Any], modules: dict[str, Any]
) -> list[Problem]:
    name = str(data["backup"])
    if "backups" not in modules:
        return [
            (
                f"{path}.backup",
                "data-backup",
                f"{host} runs no backups module: declare the job {name} in its backups jobs",
            )
        ]
    jobs = sorted(job.name for job in host_jobs(modules))
    if name in jobs:
        return []
    known = ", ".join(jobs) or "none"
    return [
        (f"{path}.backup", "data-backup", f"no backups job {name} on {host}; its jobs: {known}")
    ]


def rpo_problems(
    path: str, host: str, data: dict[str, Any], job: Job | None, modules: dict[str, Any]
) -> list[Problem]:
    if not data.get("rpo"):
        return []
    rpo = duration_seconds(data["rpo"])
    problems = []
    age = job_age(job) if job is not None else None
    if job is not None and age is not None and age > rpo:
        problems.append(
            (
                f"{path}.rpo",
                "backup-rpo",
                f"the job {job.name} counts as missing only after {spoken(age)} (the longest gap"
                f" between its runs plus grace), longer than the rpo {data['rpo']}: run it more"
                " often or shorten its grace",
            )
        )
    offsite = modules.get(OFFSITE)
    if offsite is None or OFFSITE not in copies_of(data):
        return problems
    copy_age = offsite_age(offsite)
    if copy_age is not None and copy_age > rpo:
        problems.append(
            (
                f"{path}.rpo",
                "backup-rpo",
                f"the daily off-site copy of {host} at {offsite['schedule']} can be"
                f" {spoken(copy_age)} old (the longest gap between two copies plus"
                f" retry_minutes), longer than the rpo {data['rpo']}",
            )
        )
    return problems


def rto_problems(path: str, data: dict[str, Any], copies: list[Copy]) -> list[Problem]:
    if not data.get("rto"):
        return []
    rto = duration_seconds(data["rto"])
    return [
        (
            f"{path}.rto",
            "backup-rto",
            f"the restore drill on {copy.place} may run {copy.drill_minutes} minutes before it"
            f" fails, longer than the rto {data['rto']}: set its max_minutes to at most"
            f" {rto // 60}, so a restore slower than the rto fails the drill",
        )
        for copy in copies
        if copy.drill_minutes is not None and copy.drill_minutes * 60 > rto
    ]


def source_problems(
    path: str, host: str, data: dict[str, Any], job: Job | None, modules: dict[str, Any]
) -> list[Problem]:
    if OFFSITE not in copies_of(data):
        return []
    offsite = modules.get(OFFSITE)
    if offsite is None:
        return [
            (
                f"{path}.copies",
                "backup-placement",
                f"{host} runs no offsite module, so no copy of the dump leaves the host",
            )
        ]
    if job is None or not job.output:
        return []
    output = str(job.output)
    folder = posixpath.dirname(output)
    if "$" in folder:
        return []
    directory = source_dir(str(offsite["source"]))
    if GLOB.search(directory) or directory in ("/", folder, output.rstrip("/")):
        return []
    return [
        (
            f"{path}.copies",
            "backup-placement",
            f"offsite on {host} copies from {directory}, the job {job.name} writes"
            f" {output}: the off-site copy holds another file",
        )
    ]


def apart(first: Copy, second: Copy) -> bool:
    if first.outside or second.outside:
        return first.place != second.place
    return first.zone is not None and second.zone is not None and first.zone != second.zone


def zone_problem(path: str, copies: list[Copy]) -> Problem | None:
    if any(apart(a, b) for index, a in enumerate(copies) for b in copies[index + 1 :]):
        return None
    unnamed = sorted({copy.place for copy in copies if copy.zone is None})
    if len(copies) < 2:
        message = (
            "a critical service needs at least two copies of its dump in different zones,"
            " the host and offsite"
        )
    elif unnamed:
        message = (
            f"name the zone of {', '.join(unnamed)}, so aye check can tell the copies stand apart"
        )
    else:
        message = f"every copy of the dump stands in zone {copies[0].zone!r}"
    return (f"{path}.copies", "backup-placement", message)


def provider_problem(path: str, host: str, home: str | None, copies: list[Copy]) -> Problem | None:
    if home is None or any(copy.provider != home for copy in copies):
        return None
    return (
        f"{path}.copies",
        "backup-placement",
        f"every copy stands with provider {home!r}, like {host}: one account or one outage"
        " takes them all; keep one with another provider",
    )


def append_problem(path: str, host: str, copies: list[Copy]) -> Problem | None:
    if any(copy.append_only for copy in copies):
        return None
    return (
        f"{path}.copies",
        "backup-placement",
        f"no copy is append-only, so whoever takes over {host} can delete them all: copy off-site"
        " to an objectstore with provider rest-server, or to a url target with append_only: true",
    )


def placement_problems(
    manifest: Manifest, path: str, host: str, entry: dict[str, Any], copies: list[Copy]
) -> list[Problem]:
    if entry.get("criticality") != CRITICAL:
        return []
    found = [
        zone_problem(path, copies),
        provider_problem(path, host, place_of(manifest, host)[1], copies),
        append_problem(path, host, copies),
    ]
    return [problem for problem in found if problem is not None]


def datastore_problems(manifest: Manifest) -> list[Problem]:
    problems: list[Problem] = []
    for name, entry, data in declarations(manifest):
        host = str(entry["host"])
        path = f"services.{name}.data"
        modules = merged_modules(manifest, host)
        job = next((job for job in host_jobs(modules) if job.name == str(data["backup"])), None)
        copies = resolve_copies(manifest, host, data, modules)
        problems += job_problems(path, host, data, modules)
        problems += rpo_problems(path, host, data, job, modules)
        problems += rto_problems(path, data, copies)
        problems += source_problems(path, host, data, job, modules)
        problems += placement_problems(manifest, path, host, entry, copies)
    return problems


def job_rpos(manifest: Manifest, host: str) -> dict[str, int]:
    found: dict[str, int] = {}
    for _, entry, data in declarations(manifest):
        if entry["host"] == host and data.get("rpo"):
            job, rpo = str(data["backup"]), duration_seconds(data["rpo"])
            found[job] = min(found.get(job, rpo), rpo)
    return found


def offsite_max_age(manifest: Manifest, host: str) -> int:
    rpos = [
        duration_seconds(data["rpo"])
        for _, entry, data in declarations(manifest)
        if entry["host"] == host and data.get("rpo") and OFFSITE in copies_of(data)
    ]
    return min([OFFSITE_STALE_SECONDS, *rpos])


def is_database(reference: Any) -> bool:
    base = str(reference or "").split("@")[0].rsplit("/", 1)[-1]
    return base.split(":")[0].split(".")[0].lower() in DATABASES


def database_of(manifest: Manifest, name: str, host: str, compose: ComposeServices) -> str | None:
    unit = (manifest.hosts[host].get("units") or {}).get(name)
    if is_database(unit):
        return f"the unit {unit}"
    try:
        image = (compose(manifest, host).get(name) or {}).get("image")
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return None
    return f"the image {image}" if is_database(image) else None


def datastore_warnings(manifest: Manifest, compose: ComposeServices) -> list[Problem]:
    warnings = []
    for name, entry in sorted(manifest.active_services.items()):
        host = entry.get("host")
        if entry.get("criticality") != CRITICAL or "data" in entry or host not in manifest.hosts:
            continue
        found = database_of(manifest, name, str(host), compose)
        if found:
            warnings.append(
                (
                    f"services.{name}",
                    "data-declared",
                    f"a critical service on {found} keeps data, and has no data: declare its"
                    " engine, backup job, rpo and copies so aye check can hold the backups to them",
                )
            )
    return warnings
