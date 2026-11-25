from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ayeops.manifest import Manifest
from ayeops.registry import load_modules
from ayeops.strict import load_yaml

LOKI_PORT = 3100
LOKI_DATASOURCE_UID = "loki"
LOKI_DATASOURCE_FILE = "grafana/provisioning/datasources/loki.yaml"
PROJECT_CHARS = re.compile(r"[^a-z0-9_-]")
PROJECT_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]*$")

Problem = tuple[str, str, str]


def logs_settings(manifest: Manifest, host: str) -> dict[str, Any] | None:
    modules = manifest.host_modules(host)
    if "logs" not in modules:
        return None
    return load_modules()["logs"].settings(modules["logs"])


def push_url(manifest: Manifest, host: str, settings: dict[str, Any]) -> str:
    target = settings.get("push") or {}
    if "url" in target:
        return str(target["url"])
    if "host" in target and target["host"] != host:
        address = manifest.hosts.get(target["host"], {}).get("address", target["host"])
        return f"http://{address}:{LOKI_PORT}/loki/api/v1/push"
    return f"http://loki:{LOKI_PORT}/loki/api/v1/push"


def compose_project(manifest: Manifest, compose: str, declared: str) -> str:
    if declared:
        return declared
    path = manifest.base_dir / compose
    data: Any = None
    if path.is_file():
        data = load_yaml(path.read_text(encoding="utf-8"))
    if isinstance(data, dict) and data.get("name"):
        return str(data["name"])
    directory = Path(compose).parent.name or manifest.base_dir.resolve().name
    return PROJECT_CHARS.sub("", directory.lower())


def declared_projects(manifest: Manifest, host: str) -> list[str]:
    from ayeops.render import compose_entries

    names = [f"{manifest.project}-{host}"]
    for compose, declared in compose_entries(manifest.hosts[host]):
        names.append(compose_project(manifest, compose, declared))
    return names


def shipped_projects(manifest: Manifest, host: str, settings: dict[str, Any]) -> list[str]:
    projects = settings.get("projects")
    if projects is not None:
        return sorted({f"{manifest.project}-{host}", *map(str, projects)})
    return sorted(set(declared_projects(manifest, host)))


def logs_context(manifest: Manifest, host: str, settings: dict[str, Any] | None) -> dict[str, Any]:
    if settings is None:
        return {}
    projects = shipped_projects(manifest, host, settings)
    return {
        "server": settings.get("role") == "server",
        "push_url": push_url(manifest, host, settings),
        "projects": projects,
        "projects_regex": "|".join(
            name if PROJECT_NAME.match(name) else re.escape(name) for name in projects
        ),
    }


def loki_url(manifest: Manifest, grafana_host: str) -> str | None:
    servers = [
        host
        for host in sorted(manifest.hosts)
        if (logs_settings(manifest, host) or {}).get("role") == "server"
    ]
    if not servers:
        return None
    if grafana_host in servers:
        return f"http://loki:{LOKI_PORT}"
    address = manifest.hosts[servers[0]].get("address", servers[0])
    return f"http://{address}:{LOKI_PORT}"


def loki_datasource(manifest: Manifest, host: str) -> dict[str, str]:
    from ayeops.render import dump_yaml

    url = loki_url(manifest, host)
    if url is None:
        return {}
    source = {
        "name": "Loki",
        "uid": LOKI_DATASOURCE_UID,
        "type": "loki",
        "access": "proxy",
        "url": url,
        "isDefault": False,
        "editable": False,
        "jsonData": {"maxLines": 1000},
    }
    return {LOKI_DATASOURCE_FILE: dump_yaml({"apiVersion": 1, "datasources": [source]})}


def push_problems(manifest: Manifest) -> list[Problem]:
    problems: list[Problem] = []
    for host in sorted(manifest.hosts):
        settings = logs_settings(manifest, host)
        if settings is None:
            continue
        path = f"hosts.{host}.modules.logs.push"
        target = settings.get("push") or {}
        if settings["role"] == "agent" and not target:
            problems.append((path, "logs-push-target", "an agent needs push"))
        central = target.get("host")
        if central is None or central == host:
            continue
        if central not in manifest.hosts:
            problems.append((path, "logs-push-target", f"unknown host {central!r}"))
            continue
        central_settings = logs_settings(manifest, central) or {}
        if central_settings.get("role") != "server":
            problems.append((path, "logs-push-target", f"{central} runs no logs server"))
        if LOKI_PORT not in (central_settings.get("expose") or []):
            problems.append((path, "logs-push-target", f"{central} does not expose {LOKI_PORT}"))
        if not manifest.hosts[central].get("address"):
            problems.append((path, "logs-push-target", f"{central} has no address"))
    return problems
