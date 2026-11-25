from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

SERVICE = "grafana-reader"
VOLUME = "grafana-reader"
MOUNT = "/run/grafana-reader"
TOKEN_FILE = f"{MOUNT}/token"
ENV = "AYE_GRAFANA_READER_TOKEN_FILE"
GROUP = 10470
MOUNT_ENTRY = f"{VOLUME}:{MOUNT}:ro"


def context(enabled: bool = False) -> dict[str, Any]:
    return {
        "enabled": enabled,
        "service": SERVICE,
        "volume_name": VOLUME,
        "volume": MOUNT_ENTRY,
        "env": ENV,
        "token_file": TOKEN_FILE,
        "group": str(GROUP),
    }


def declared(entry: dict[str, Any]) -> bool:
    return bool(entry.get("grafana_reader"))


def consumed(names: Iterable[str], modules: Mapping[str, Any]) -> bool:
    return any(
        declared(entry)
        for name in names
        if name in modules
        for entry in modules[name].catalog.values()
    )


def environment(service: dict[str, Any]) -> dict[str, str]:
    raw = service.get("environment") or {}
    if isinstance(raw, dict):
        return {str(key): str(value) for key, value in raw.items()}
    pairs = (str(item).partition("=") for item in raw)
    return {key: value for key, _, value in pairs}


def volume_entries(service: dict[str, Any]) -> list[str]:
    entries: list[str] = []
    for volume in service.get("volumes") or []:
        if isinstance(volume, dict):
            mode = ":ro" if volume.get("read_only") else ""
            entries.append(f"{volume.get('source')}:{volume.get('target')}{mode}")
        else:
            entries.append(str(volume))
    return entries


def mounts_volume(service: dict[str, Any]) -> bool:
    return any(entry.partition(":")[0] == VOLUME for entry in volume_entries(service))


def waits_for_reader(service: dict[str, Any]) -> bool:
    depends = service.get("depends_on") or {}
    if isinstance(depends, list):
        return False
    condition = (depends.get(SERVICE) or {}).get("condition")
    return bool(condition == "service_healthy")


def consumer_problems(name: str, service: dict[str, Any], grafana_here: bool) -> list[str]:
    if not grafana_here:
        return [f"{name} needs the grafana reader, which runs only on the grafana host"]
    problems: list[str] = []
    entries = volume_entries(service)
    if MOUNT_ENTRY not in entries:
        mounted = [entry for entry in entries if entry.partition(":")[0] == VOLUME]
        found = f", not {', '.join(mounted)}" if mounted else ""
        problems.append(f"{name} must mount {MOUNT_ENTRY}{found}")
    if environment(service).get(ENV) != TOKEN_FILE:
        problems.append(f"{name} must set {ENV}={TOKEN_FILE}")
    if str(GROUP) not in [str(group) for group in service.get("group_add") or []]:
        problems.append(f"{name} must add group {GROUP}, which owns the token file")
    if not waits_for_reader(service):
        problems.append(f"{name} must depend on {SERVICE} with condition service_healthy")
    return problems
