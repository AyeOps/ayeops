from __future__ import annotations

import hashlib
from typing import Any

CONFIG_HASH_LABEL = "io.ayeops.config-hash"


def bind_sources(service: dict[str, Any]) -> list[str]:
    sources: list[str] = []
    for volume in service.get("volumes") or []:
        if isinstance(volume, dict):
            source = volume.get("source") if volume.get("type") == "bind" else None
        else:
            source = str(volume).partition(":")[0]
        if isinstance(source, str) and source.startswith("./"):
            sources.append(source.removeprefix("./").rstrip("/"))
    return sources


def under(path: str, source: str) -> bool:
    return path == source or path.startswith(f"{source}/")


def mounted_files(service: dict[str, Any], paths: list[str]) -> list[str]:
    sources = bind_sources(service)
    return sorted(path for path in paths if any(under(path, source) for source in sources))


def config_hash(service: dict[str, Any], tree: dict[str, str], live: set[str]) -> str | None:
    files = [path for path in mounted_files(service, list(tree)) if path not in live]
    if not files:
        return None
    digest = hashlib.sha256()
    for path in files:
        digest.update(f"{path}\0{tree[path]}\0".encode())
    return digest.hexdigest()[:16]


def stamp_config_hashes(
    compose: dict[str, Any], tree: dict[str, str], live: set[str]
) -> dict[str, Any]:
    services: dict[str, Any] = {}
    for name, service in compose["services"].items():
        stamped = dict(service)
        value = config_hash(service, tree, live)
        if value is not None:
            stamped["labels"] = {**(service.get("labels") or {}), CONFIG_HASH_LABEL: value}
        services[name] = stamped
    return {**compose, "services": services}


def services_touching(compose: dict[str, Any], paths: list[str]) -> dict[str, list[str]]:
    touched: dict[str, list[str]] = {}
    for name, service in (compose.get("services") or {}).items():
        files = mounted_files(service, paths)
        if files:
            touched[name] = files
    return touched
