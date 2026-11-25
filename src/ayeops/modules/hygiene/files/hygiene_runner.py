from __future__ import annotations

import http.client
import json
import os
import re
import socket
import sys
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

SIZE = re.compile(r"^(\d+(?:\.\d+)?)([kmgt]?)$")
SIZE_FACTORS = {"": 1, "k": 1024, "m": 1024**2, "g": 1024**3, "t": 1024**4}
DURATION = re.compile(r"^(\d+)([smh])$")
DURATION_FACTORS = {"s": 1, "m": 60, "h": 3600}
SCHEDULE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
MAX_USED_SPACE_API = (1, 48)
STOPPED = {"created", "exited", "dead"}
KINDS = ("containers", "images", "build_cache", "volumes")
PREFIX = "ayeops_hygiene_"
LAST_SUCCESS = re.compile(rf"^{PREFIX}last_success_timestamp_seconds\s+(\S+)\s*$", re.MULTILINE)
ANONYMOUS = "com.docker.volume.anonymous"
NAP = 60
API_TIMEOUT = 1800.0
HEARTBEAT = Path("/tmp/hygiene.alive")
STATVFS: Callable[[Path], Any] | None = getattr(os, "statvfs", None)

Api = Callable[[str, str, Mapping[str, str]], Any]


def parse_size(text: str) -> int:
    match = SIZE.match(text.strip().lower())
    if match is None:
        raise ValueError(f"not a size: {text!r}")
    return int(float(match.group(1)) * SIZE_FACTORS[match.group(2)])


def parse_duration(text: str) -> int:
    match = DURATION.match(text.strip())
    if match is None:
        raise ValueError(f"not a duration: {text!r}")
    return int(match.group(1)) * DURATION_FACTORS[match.group(2)]


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


def api_version(text: str) -> tuple[int, int]:
    major, _, minor = text.strip().partition(".")
    return int(major), int(minor or 0)


def build_prune_query(version: str, keep: int) -> dict[str, str]:
    if api_version(version) >= MAX_USED_SPACE_API:
        return {"all": "true", "max-used-space": str(keep)}
    return {"all": "true", "keep-storage": str(keep)}


def filters(**values: str) -> dict[str, str]:
    return {"filters": json.dumps({key: [value] for key, value in values.items()})}


def used_image_ids(containers: Iterable[Mapping[str, Any]]) -> set[str]:
    return {str(c.get("ImageID") or "") for c in containers} - {""}


def removable_containers(
    containers: Iterable[Mapping[str, Any]], cutoff: float
) -> list[Mapping[str, Any]]:
    return [c for c in containers if c.get("State") in STOPPED and c.get("Created", 0) < cutoff]


def removable_images(
    images: Iterable[Mapping[str, Any]], used: set[str], cutoff: float
) -> list[Mapping[str, Any]]:
    return [i for i in images if i.get("Id") not in used and i.get("Created", 0) < cutoff]


def image_unique_bytes(image: Mapping[str, Any]) -> int:
    size = int(image.get("Size") or 0)
    shared = int(image.get("SharedSize") or 0)
    return max(size - max(shared, 0), 0)


def build_cache_bytes(entries: Iterable[Mapping[str, Any]]) -> int:
    return sum(int(e.get("Size") or 0) for e in entries if not e.get("Shared"))


def build_cache_reclaimable(entries: Iterable[Mapping[str, Any]], keep: int) -> int:
    listed = list(entries)
    idle = build_cache_bytes(e for e in listed if not e.get("InUse"))
    return min(max(build_cache_bytes(listed) - keep, 0), idle)


def removable_volumes(volumes: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return [
        v
        for v in volumes
        if ANONYMOUS in (v.get("Labels") or {}) and (v.get("UsageData") or {}).get("RefCount") == 0
    ]


def volume_bytes(volume: Mapping[str, Any]) -> int:
    return max(int(volume.get("UsageData", {}).get("Size") or 0), 0)


def disk_used_percent(blocks: int, free: int, available: int) -> float:
    used = blocks - free
    total = used + available
    if total <= 0:
        return 0.0
    return round(used * 100 / total, 2)


def allocated_bytes(stat: Any) -> int:
    blocks = getattr(stat, "st_blocks", None)
    if blocks is None:
        return int(stat.st_size)
    return int(blocks) * 512


def directory_bytes(root: Path, lstat: Callable[[Path], Any] = os.lstat) -> int:
    total = 0
    for directory, _, names in os.walk(root):
        for name in names:
            try:
                total += allocated_bytes(lstat(Path(directory) / name))
            except OSError:
                continue
    return total


def last_success(text: str) -> float | None:
    match = LAST_SUCCESS.search(text)
    if match is None:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


@dataclass
class Settings:
    schedule: tuple[int, int] = (3, 30)
    images_until: str = "168h"
    containers_until: str = "24h"
    build_cache_keep: int = 10 * 1024**3
    volumes: bool = False
    dry_run: bool = False
    textfile: Path = Path("/textfile/ayeops_hygiene.prom")
    disk: Path | None = Path("/disk")
    journal: Path | None = None


def flag(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def optional_path(value: str | None) -> Path | None:
    return Path(value) if value else None


def settings_from(environ: Mapping[str, str]) -> Settings:
    return Settings(
        schedule=parse_schedule(environ.get("AYE_HYGIENE_SCHEDULE", "03:30")),
        images_until=environ.get("AYE_HYGIENE_IMAGES_UNTIL", "168h"),
        containers_until=environ.get("AYE_HYGIENE_CONTAINERS_UNTIL", "24h"),
        build_cache_keep=parse_size(environ.get("AYE_HYGIENE_BUILD_CACHE_KEEP", "10g")),
        volumes=flag(environ.get("AYE_HYGIENE_VOLUMES")),
        dry_run=flag(environ.get("AYE_HYGIENE_DRY_RUN")),
        textfile=Path(environ.get("AYE_HYGIENE_TEXTFILE", "/textfile/ayeops_hygiene.prom")),
        disk=optional_path(environ.get("AYE_HYGIENE_DISK", "/disk")),
        journal=optional_path(environ.get("AYE_HYGIENE_JOURNAL")),
    )


@dataclass
class Snapshot:
    images: int
    build_cache: int
    disk_used: float | None


@dataclass
class Report:
    started: float
    dry_run: bool
    finished: float
    success: bool = False
    before: Snapshot | None = None
    after: Snapshot | None = None
    bytes: dict[str, int] = field(default_factory=dict)
    counts: dict[str, int] = field(default_factory=dict)
    journal: int | None = None
    error: str = ""


def disk_usage(path: Path | None, statvfs: Callable[[Path], Any] | None = STATVFS) -> float | None:
    if path is None or statvfs is None:
        return None
    try:
        stat = statvfs(path)
    except OSError:
        return None
    return disk_used_percent(stat.f_blocks, stat.f_bfree, stat.f_bavail)


def system_df(api: Api, *types: str) -> dict[str, Any]:
    query = urlencode([("type", t) for t in types])
    result: dict[str, Any] = api("GET", f"/system/df?{query}", {})
    return result


def snapshot(api: Api, settings: Settings) -> Snapshot:
    df = system_df(api, "image", "build-cache")
    return Snapshot(
        images=len(df.get("Images") or []),
        build_cache=build_cache_bytes(df.get("BuildCache") or []),
        disk_used=disk_usage(settings.disk),
    )


def prune(api: Api, settings: Settings, version: str, report: Report) -> None:
    calls = [
        ("containers", "/containers/prune", filters(until=settings.containers_until)),
        ("images", "/images/prune", filters(dangling="false", until=settings.images_until)),
        ("build_cache", "/build/prune", build_prune_query(version, settings.build_cache_keep)),
    ]
    if settings.volumes:
        calls.append(("volumes", "/volumes/prune", {}))
    removed_key = {
        "containers": "ContainersDeleted",
        "images": "ImagesDeleted",
        "build_cache": "CachesDeleted",
        "volumes": "VolumesDeleted",
    }
    for kind, path, query in calls:
        result = api("POST", path, query) or {}
        report.bytes[kind] = int(result.get("SpaceReclaimed") or 0)
        report.counts[kind] = len(result.get(removed_key[kind]) or [])


def estimate(api: Api, settings: Settings, now: float, report: Report) -> None:
    containers = api("GET", "/containers/json", {"all": "true", "size": "true"}) or []
    stale = removable_containers(containers, now - parse_duration(settings.containers_until))
    report.counts["containers"] = len(stale)
    report.bytes["containers"] = sum(int(c.get("SizeRw") or 0) for c in stale)
    images = api("GET", "/images/json", {"shared-size": "true"}) or []
    cutoff = now - parse_duration(settings.images_until)
    old = removable_images(images, used_image_ids(containers), cutoff)
    report.counts["images"] = len(old)
    report.bytes["images"] = sum(image_unique_bytes(i) for i in old)
    entries = system_df(api, "build-cache").get("BuildCache") or []
    report.bytes["build_cache"] = build_cache_reclaimable(entries, settings.build_cache_keep)
    if settings.volumes:
        volumes = removable_volumes(system_df(api, "volume").get("Volumes") or [])
        report.counts["volumes"] = len(volumes)
        report.bytes["volumes"] = sum(volume_bytes(v) for v in volumes)


def run_once(api: Api, settings: Settings, wall: Callable[[], float] = time.time) -> Report:
    started = wall()
    report = Report(started=started, dry_run=settings.dry_run, finished=started)
    try:
        version = str(api("GET", "/version", {}).get("ApiVersion") or "1.24")
        report.before = snapshot(api, settings)
        if settings.dry_run:
            estimate(api, settings, report.started, report)
            report.after = report.before
        else:
            prune(api, settings, version, report)
            report.after = snapshot(api, settings)
        report.success = True
    except Exception as error:
        report.error = f"{type(error).__name__}: {error}"
    if settings.journal is not None:
        report.journal = directory_bytes(settings.journal)
    report.finished = wall()
    return report


def metric(name: str, value: float, **labels: str) -> str:
    rendered = ",".join(f'{key}="{val}"' for key, val in sorted(labels.items()))
    suffix = "{" + rendered + "}" if rendered else ""
    number = int(value) if float(value).is_integer() else value
    return f"{PREFIX}{name}{suffix} {number}\n"


def render_metrics(report: Report, settings: Settings, success_at: float) -> str:
    lines = [
        metric("last_run_timestamp_seconds", round(report.finished)),
        metric("last_run_success", int(report.success)),
        metric("last_success_timestamp_seconds", round(success_at)),
        metric("run_duration_seconds", round(report.finished - report.started, 3)),
        metric("dry_run", int(report.dry_run)),
        metric("build_cache_keep_bytes", settings.build_cache_keep),
    ]
    bytes_name, count_name = (
        ("reclaimable_bytes", "removable") if report.dry_run else ("reclaimed_bytes", "removed")
    )
    for kind in KINDS:
        if kind in report.bytes:
            lines.append(metric(bytes_name, report.bytes[kind], kind=kind))
        if kind in report.counts:
            lines.append(metric(count_name, report.counts[kind], kind=kind))
    shots = [("before", report.before), ("after", report.after)]
    for phase, shot in [(phase, shot) for phase, shot in shots if shot is not None]:
        lines.append(metric("images", shot.images, phase=phase))
        lines.append(metric("build_cache_bytes", shot.build_cache, phase=phase))
        if shot.disk_used is not None:
            lines.append(metric("disk_used_percent", shot.disk_used, phase=phase))
    if report.journal is not None:
        lines.append(metric("journal_bytes", report.journal))
    return "".join(lines)


def startup_metrics(settings: Settings, success_at: float) -> str:
    return metric("last_success_timestamp_seconds", round(success_at)) + metric(
        "build_cache_keep_bytes", settings.build_cache_keep
    )


def temporary_path(path: Path) -> Path:
    return path.with_name(path.name + ".tmp")


def write_atomic(path: Path, text: str) -> None:
    temporary = temporary_path(path)
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def previous_success(path: Path, fallback: float) -> float:
    try:
        found = last_success(path.read_text(encoding="utf-8"))
    except OSError:
        found = None
    return fallback if found is None else found


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


def log(report: Report) -> None:
    print(
        json.dumps(
            {
                "dry_run": report.dry_run,
                "success": report.success,
                "error": report.error,
                "bytes": report.bytes,
                "counts": report.counts,
                "before": vars(report.before) if report.before else None,
                "after": vars(report.after) if report.after else None,
                "journal_bytes": report.journal,
                "seconds": round(report.finished - report.started, 1),
            },
            sort_keys=True,
        ),
        flush=True,
    )


def record(
    api: Api, settings: Settings, success_at: float, wall: Callable[[], float] = time.time
) -> float:
    report = run_once(api, settings, wall)
    if report.success:
        success_at = report.finished
    write_atomic(settings.textfile, render_metrics(report, settings, success_at))
    log(report)
    return success_at


def main(environ: Mapping[str, str] = os.environ) -> int:
    settings = settings_from(environ)
    api = docker_api(environ.get("AYE_HYGIENE_SOCKET", "/var/run/docker.sock"))
    success_at = previous_success(settings.textfile, time.time())
    if not settings.textfile.exists():
        write_atomic(settings.textfile, startup_metrics(settings, success_at))
    if flag(environ.get("AYE_HYGIENE_ONCE")):
        record(api, settings, success_at)
        return 0
    serve(api, settings, success_at)
    return 0


def serve(
    api: Api,
    settings: Settings,
    success_at: float,
    wall: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
    heartbeat: Path = HEARTBEAT,
    going: Callable[[], bool] = lambda: True,
) -> float:
    due = next_run(wall(), *settings.schedule)
    while going():
        heartbeat.touch()
        now = wall()
        if now >= due:
            success_at = record(api, settings, success_at, wall)
            due = next_run(wall(), *settings.schedule)
            continue
        sleep(min(NAP, due - now))
    return success_at


if __name__ == "__main__":
    sys.exit(main())
