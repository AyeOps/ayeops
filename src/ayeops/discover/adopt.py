from __future__ import annotations

import ipaddress
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path, PurePosixPath
from typing import Any

from ayeops.deploy import REMOTE_ROOT
from ayeops.discover.suggest import NAME, compose_projects, host_modules, manifest_name
from ayeops.discover.suggest import reserved_memory as host_reserved
from ayeops.manifest import Manifest
from ayeops.registry import Module
from ayeops.render import MODULE_RUNBOOK_BASE, FleetRender
from ayeops.waivers import WARN_DAYS

MARK = "adopted as is"
TODO = f"{MARK}, TODO:"
RUNBOOK = f"{MODULE_RUNBOOK_BASE}adopted-service-down.md"
COUNTED = "container_start_time_seconds"
WILDCARDS = frozenset({"", "0.0.0.0", "::", "*", "[::]"})
COMPOSE_FILE = "compose.yaml"
NAME_LENGTH = 40
DEFAULT_PROJECT = "ayeops"
ARCHES = frozenset({"amd64", "arm64"})


class AdoptError(Exception):
    pass


@dataclass(frozen=True)
class Adopted:
    project: str
    original: str
    name: str
    image: str
    containers: tuple[str, ...]
    running: int
    ports: tuple[tuple[int, int, str, str], ...]

    @property
    def live(self) -> bool:
        return self.running > 0


@dataclass
class Adoption:
    host: str
    project: str
    manifest: dict[str, Any]
    compose_files: dict[str, dict[str, Any]]
    services: list[Adopted]
    until: date
    notes: list[str] = field(default_factory=list)


def default_until(today: date) -> date:
    return today + timedelta(days=WARN_DAYS)


def unique(base: str, taken: set[str]) -> str:
    name = base[:NAME_LENGTH].rstrip("-")
    index = 2
    while name in taken:
        suffix = f"-{index}"
        name = f"{base[: NAME_LENGTH - len(suffix)].rstrip('-')}{suffix}"
        index += 1
    taken.add(name)
    return name


def port_key(port: dict[str, Any]) -> tuple[int, int, str, str]:
    bind = str(port.get("bind") or "")
    return (
        int(port["published"]),
        int(port.get("target") or port["published"]),
        "" if bind in WILDCARDS else bind,
        str(port.get("protocol") or "tcp"),
    )


def service_ports(containers: list[dict[str, Any]]) -> tuple[tuple[int, int, str, str], ...]:
    found: set[tuple[int, int, str, str]] = set()
    for container in containers:
        detailed = container.get("ports")
        if detailed is None:
            detailed = [{"published": port} for port in container.get("published_ports") or []]
        found |= {port_key(port) for port in detailed}
    return tuple(sorted(found))


def service_name(project: str, original: str, taken: set[str]) -> str:
    candidates = [
        manifest_name(original, "service"),
        manifest_name(f"{project}-{original}", "service"),
    ]
    if NAME.match(original):
        candidates.insert(0, original)
    return next((name for name in candidates if name not in taken), candidates[-1])


def adopted_services(report: dict[str, Any], reserved: Iterable[str]) -> list[Adopted]:
    avoided = set(reserved)
    taken: set[str] = set()
    found: list[Adopted] = []
    for project in compose_projects(report):
        groups: dict[str, list[dict[str, Any]]] = {}
        for container in project.get("services") or []:
            original = str(container.get("service") or "")
            if original:
                groups.setdefault(original, []).append(container)
        for original, containers in sorted(groups.items()):
            base = service_name(str(project["name"]), original, taken | avoided)
            images = sorted({str(c.get("image") or "") for c in containers} - {""})
            found.append(
                Adopted(
                    project=str(project["name"]),
                    original=original,
                    name=unique(base, taken | avoided),
                    image=images[0] if images else "",
                    containers=tuple(sorted(str(c.get("container") or "") for c in containers)),
                    running=sum(1 for c in containers if c.get("state") == "running"),
                    ports=service_ports(containers),
                )
            )
            taken.add(found[-1].name)
    return found


def compose_port(port: tuple[int, int, str, str]) -> dict[str, Any]:
    published, target, bind, protocol = port
    spec: dict[str, Any] = {"target": target, "published": str(published), "protocol": protocol}
    if bind:
        spec["host_ip"] = bind
    return spec


def default_container(item: Adopted, index: int) -> str:
    return f"{item.project}-{item.name}-{index}"


def stub_service(item: Adopted) -> dict[str, Any]:
    service: dict[str, Any] = {}
    if item.image:
        service["image"] = item.image
    if item.ports:
        service["ports"] = [compose_port(port) for port in item.ports]
    defaults = [default_container(item, index + 1) for index in range(len(item.containers))]
    if len(item.containers) == 1 and item.containers[0] != defaults[0]:
        service["container_name"] = item.containers[0]
    elif len(item.containers) > 1:
        service["scale"] = len(item.containers)
    return service


def stub_compose(project: dict[str, Any], items: list[Adopted]) -> dict[str, Any]:
    return {
        "name": str(project["name"]),
        "x-aye-adopted": {
            "by": "aye discover --adopt",
            "config_files": list(project.get("config_files") or []),
            "status": str(project.get("status") or ""),
            "note": (
                "written from the probe report, not from the compose files: it describes"
                " what runs for aye check, never run docker compose with it;"
                " replace it with the config_files above"
            ),
        },
        "services": {item.name: stub_service(item) for item in items},
    }


def container_matcher(containers: tuple[str, ...]) -> str:
    if len(containers) == 1:
        return f'name="{containers[0]}"'
    escaped = "|".join(name.replace(".", "\\\\.") for name in containers)
    return f'name=~"{escaped}"'


def down_alert(item: Adopted, host: str, name: str) -> dict[str, Any]:
    wanted = item.running
    own = f'{COUNTED}{{host="{host}",{container_matcher(item.containers)}}}'
    any_container = f'{COUNTED}{{host="{host}"}}'
    return {
        "name": name,
        "title": f"{item.name} not running",
        "expr": (
            f"((count({own}) or vector(0)) < bool {wanted})"
            f" * ((count({any_container}) or vector(0)) > bool 0)"
        ),
        "severity": "warning",
        "summary": (
            f"{item.name} on {host} runs fewer than the {wanted} container(s) it ran"
            " when it was adopted"
        ),
        "runbook": RUNBOOK,
    }


def waiver(reason: str, until: date) -> dict[str, Any]:
    return {"reason": f"{TODO} {reason}", "until": until.isoformat()}


def description(item: Adopted, host: str) -> str:
    image = f", image {item.image}" if item.image else ""
    state = "" if item.live else " while stopped"
    return f"{item.original} of the compose project {item.project} on {host}{image}, {MARK}{state}"


def service_entry(item: Adopted, host: str, alert: str, until: date) -> dict[str, Any]:
    if not item.live:
        return {
            "host": host,
            "state": "disabled",
            "description": description(item, host),
        }
    return {
        "host": host,
        "description": description(item, host),
        "logs": "docker",
        "metrics": {"none": waiver(f"name the metrics of {item.name}, or why it has none", until)},
        "alerts": [down_alert(item, host, alert)],
        "status": {
            "internal": waiver(
                f"a public status component for {item.name}, or why it stays internal", until
            )
        },
    }


def link_addresses(link: dict[str, Any]) -> list[ipaddress.IPv4Interface]:
    found = []
    for address in link.get("addresses") or []:
        try:
            interface = ipaddress.ip_interface(str(address))
        except ValueError:
            continue
        if isinstance(interface, ipaddress.IPv4Interface):
            found.append(interface)
    return found


def host_address(report: dict[str, Any]) -> str | None:
    links = [link for link in report.get("network") or [] if link.get("operstate") != "down"]
    for link in links:
        if link.get("kind") == "vpn" and link_addresses(link):
            return str(link_addresses(link)[0].ip)
    try:
        router = ipaddress.ip_address(str(report.get("gateway") or ""))
    except ValueError:
        return None
    for link in links:
        for interface in link_addresses(link):
            if router in interface.network:
                return str(interface.ip)
    return None


def existing_projects(report: dict[str, Any]) -> set[str]:
    return {str(project["name"]) for project in compose_projects(report)}


def existing_containers(report: dict[str, Any]) -> set[str]:
    names = {
        str(container.get("container") or "")
        for project in compose_projects(report)
        for container in project.get("services") or []
    }
    containers = report.get("containers") or {}
    names |= {str(c.get("name") or "") for c in containers.get("standalone_containers") or []}
    return names - {""}


def taken_ports(report: dict[str, Any]) -> set[int]:
    ports = {int(entry["port"]) for entry in report.get("listening_tcp") or [] if entry.get("port")}
    for project in compose_projects(report):
        for container in project.get("services") or []:
            ports |= {int(port) for port in container.get("published_ports") or []}
    return ports


def host_port(spec: Any) -> int | None:
    if isinstance(spec, dict):
        published = spec.get("published")
        return int(str(published).split("-")[0]) if published is not None else None
    parts = str(spec).split("/")[0].split(":")
    return int(parts[-2].split("-")[0]) if len(parts) > 1 else None


def remote_dir(host: str) -> PurePosixPath:
    return PurePosixPath(REMOTE_ROOT) / host


def deploy_dir_problems(report: dict[str, Any], directory: PurePosixPath) -> list[str]:
    return [
        f"{path} of the compose project {entry['name']} lies in {directory},"
        " the directory aye deploy replaces"
        for entry in compose_projects(report)
        for path in entry.get("config_files") or []
        if PurePosixPath(path).is_relative_to(directory)
    ]


def takeover(
    report: dict[str, Any], compose: dict[str, Any], directory: PurePosixPath
) -> list[str]:
    project = str(compose.get("name") or "")
    problems = []
    if project in existing_projects(report):
        problems.append(f"the compose project {project} already runs on the host")
    problems += deploy_dir_problems(report, directory)
    containers = existing_containers(report)
    ports = taken_ports(report)
    for name, service in sorted((compose.get("services") or {}).items()):
        container = str(service.get("container_name") or f"{project}-{name}-1")
        if container in containers:
            problems.append(f"{name}: the container {container} already exists on the host")
        for spec in service.get("ports") or []:
            port = host_port(spec)
            if port in ports:
                problems.append(f"{name}: host port {port} is already in use on the host")
    return problems


def offending_modules(report: dict[str, Any], compose: dict[str, Any]) -> dict[str, str]:
    project = str(compose.get("name") or "")
    containers = existing_containers(report)
    ports = taken_ports(report)
    found: dict[str, str] = {}
    for name, service in sorted((compose.get("services") or {}).items()):
        module = str(service["labels"]["io.ayeops.module"])
        busy = sorted(p for p in map(host_port, service.get("ports") or []) if p in ports)
        if busy:
            found.setdefault(module, f"{name} needs host port {busy[0]}, which is in use")
        elif str(service.get("container_name") or f"{project}-{name}-1") in containers:
            found.setdefault(module, f"the container name of {name} is taken")
    return found


def without_unmet(chosen: dict[str, Any], modules: dict[str, Module]) -> dict[str, str]:
    return {
        name: f"needs {required}, which is off"
        for name in chosen
        for required in modules[name].requires
        if required not in chosen
    }


def observed_modules(
    report: dict[str, Any], data: dict[str, Any], host: str, modules: dict[str, Module]
) -> tuple[dict[str, Any], list[str]]:
    chosen = {name: spec for name, spec in host_modules(report).items() if name in modules}
    notes: list[str] = []
    while chosen:
        attempt = {**data, "hosts": {host: {**data["hosts"][host], "modules": chosen}}}
        compose = FleetRender(Manifest(attempt, Path()), modules).compose(host)
        dropped = offending_modules(report, compose) or without_unmet(chosen, modules)
        if not dropped:
            break
        for name, reason in sorted(dropped.items()):
            notes.append(f"module {name} left off: {reason}")
            chosen.pop(name, None)
    return chosen, notes


def adoption_notes(report: dict[str, Any], services: list[Adopted]) -> list[str]:
    notes = [
        f"{item.project}/{item.original} is {item.name} in the manifest"
        for item in services
        if item.name != item.original
    ]
    notes += [
        f"{item.name} ran {len(item.containers)} containers; the stub says scale"
        f" {len(item.containers)}, check the real file"
        for item in services
        if len(item.containers) > 1
    ]
    containers = report.get("containers") or {}
    notes += [
        f"standalone container {c['name']} ({c.get('image', '')}) is not a compose service,"
        " not adopted"
        for c in containers.get("standalone_containers") or []
    ]
    return notes


def adopt(
    report: dict[str, Any],
    modules: dict[str, Module],
    today: date,
    project: str | None = None,
    until: date | None = None,
    observe: bool = False,
) -> Adoption:
    containers = report.get("containers") or {}
    if containers.get("error"):
        raise AdoptError(
            f"the probe could not list the containers ({containers['error']});"
            " adopting would describe a host without them"
        )
    system = report.get("system") or {}
    host = manifest_name(str(system.get("hostname") or ""), "host")
    name = project or DEFAULT_PROJECT
    if not NAME.match(name):
        raise AdoptError(f"project {name!r} is not a manifest name, ^[a-z][a-z0-9-]{{0,39}}$")
    if f"{name}-{host}" in existing_projects(report):
        raise AdoptError(
            f"the compose project {name}-{host} already runs on the host and aye would take it"
            " over; choose another --project"
        )
    inside = deploy_dir_problems(report, remote_dir(host))
    if inside:
        raise AdoptError(f"{inside[0]}; move it first")
    ends = until or default_until(today)
    if ends < today:
        raise AdoptError(f"--until {ends.isoformat()} is in the past")
    arch = str(system.get("arch") or "")
    if arch not in ARCHES:
        raise AdoptError(f"arch {arch or 'unknown'!r}: AyeOps renders for amd64 and arm64 only")
    module_services = {service for module in modules.values() for service in module.catalog}
    services = adopted_services(report, module_services)
    block: dict[str, Any] = {"arch": arch, "modules": {}}
    total = int((report.get("memory") or {}).get("total_bytes") or 0)
    if total:
        block["memory"] = f"{total // 1024**2}m"
    address = host_address(report)
    if address:
        block["address"] = address
    reserved = host_reserved(report)
    if reserved:
        block["reserved"] = reserved
    files: dict[str, dict[str, Any]] = {}
    entries = []
    for entry in compose_projects(report):
        items = [item for item in services if item.project == entry["name"]]
        path = f"{manifest_name(str(entry['name']), 'project')}/{COMPOSE_FILE}"
        files[path] = stub_compose(entry, items)
        entries.append({"file": path, "name": str(entry["name"])})
    if entries:
        block["compose"] = entries
    alert_names = {str(alert["name"]) for module in modules.values() for alert in module.alerts}
    catalog = {
        item.name: service_entry(item, host, unique(f"{item.name}-down", alert_names), ends)
        for item in services
    }
    data: dict[str, Any] = {
        "version": 1,
        "project": name,
        "runbook_base_url": f"https://runbooks.{name}.example",
        "hosts": {host: block},
    }
    if catalog:
        data["services"] = catalog
    notes = adoption_notes(report, services)
    if observe:
        chosen, dropped = observed_modules(report, data, host, modules)
        block["modules"] = chosen
        notes += dropped
    return Adoption(host, name, data, files, services, ends, notes)
