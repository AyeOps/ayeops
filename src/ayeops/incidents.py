from __future__ import annotations

import re
from typing import Any

from ayeops.i18n import is_reserved
from ayeops.local_modules import with_local_modules
from ayeops.manifest import DEFAULT_TIMEZONE, Manifest
from ayeops.registry import Module, all_alerts, load_modules

WEIGHTS = {"critical": 100.0, "high": 10.0, "normal": 1.0, "low": 0.1}
DEFAULT = "normal"
PUBLIC_DEFAULT = "high"
NIGHT = {"start": "23:00", "end": "08:00"}
INTERVAL = re.compile(r"^([0-9]+)([sm])$")
DEFAULT_SCRAPE_SECONDS = 30


def criticality(service: dict[str, Any]) -> str:
    explicit = service.get("criticality")
    if explicit:
        return str(explicit)
    status = service.get("status")
    if isinstance(status, dict) and status.get("public"):
        return PUBLIC_DEFAULT
    return DEFAULT


def service_criticality(manifest: Manifest) -> dict[str, str]:
    return {
        name: criticality(entry)
        for name, entry in sorted(manifest.services.items())
        if not is_reserved(entry)
    }


def night_hours(
    settings: dict[str, Any], all_settings: dict[str, Any], zone: str = DEFAULT_TIMEZONE
) -> dict[str, str]:
    hours = settings.get("night") or (all_settings.get("oncall") or {}).get("quiet_hours")
    if not hours:
        return {**NIGHT, "timezone": zone}
    return {
        "start": str(hours["start"]),
        "end": str(hours["end"]),
        "timezone": str(hours.get("timezone") or zone),
    }


def host_services(manifest: Manifest) -> dict[str, list[str]]:
    hosts: dict[str, set[str]] = {host: set() for host in manifest.hosts}
    for name, entry in manifest.active_services.items():
        host = str(entry.get("host") or "")
        if host in hosts:
            hosts[host].add(name)
    return {host: sorted(names) for host, names in sorted(hosts.items()) if names}


def host_module_names(manifest: Manifest) -> dict[str, list[str]]:
    hosts = {host: sorted(manifest.host_modules(host)) for host in sorted(manifest.hosts)}
    return {host: names for host, names in hosts.items() if names}


def known_modules(manifest: Manifest) -> dict[str, Module]:
    return with_local_modules(manifest, load_modules())


def host_alert_entries(settings: dict[str, Any]) -> list[tuple[str, dict[str, str]]]:
    entries = []
    for entry in settings.get("host_alerts") or []:
        if isinstance(entry, dict):
            labels = {str(key): str(value) for key, value in entry.items() if key != "alert"}
            entries.append((str(entry["alert"]), labels))
        else:
            entries.append((str(entry), {}))
    return entries


def host_alert_matchers(manifest: Manifest, settings: dict[str, Any]) -> list[dict[str, Any]]:
    alerts = all_alerts(known_modules(manifest))
    found = {
        (str(alerts[name][1].get("title", name)), tuple(sorted(labels.items())))
        for name, labels in host_alert_entries(settings)
        if name in alerts
    }
    return [{"title": title, "labels": dict(labels)} for title, labels in sorted(found)]


def scrape_seconds(text: Any) -> int:
    match = INTERVAL.match(str(text or ""))
    if match is None:
        return DEFAULT_SCRAPE_SECONDS
    return int(match.group(1)) * (60 if match.group(2) == "m" else 1)


def request_routes(manifest: Manifest) -> dict[str, list[dict[str, Any]]]:
    from ayeops.render import host_settings

    modules = known_modules(manifest)
    found: dict[str, dict[str, dict[str, Any]]] = {}
    for host in sorted(manifest.hosts):
        settings = host_settings(manifest, host, modules)
        ingress = settings.get("ingress") or {}
        if not ingress.get("domain"):
            continue
        owners = {compose: name for name in sorted(settings) for compose in modules[name].catalog}
        own = {
            name
            for name, entry in manifest.active_services.items()
            if str(entry.get("host") or "") == host
        }
        seconds = scrape_seconds((settings.get("metrics") or {}).get("scrape_interval"))
        for route, target in sorted((ingress.get("routes") or {}).items()):
            compose = str(target).rsplit(":", 1)[0]
            service = compose if compose in own else owners.get(compose)
            if service is None:
                continue
            entry = found.setdefault(service, {}).setdefault(
                host, {"host": host, "routes": [], "scrape_seconds": seconds}
            )
            entry["routes"].append(f"{route}@file")
    return {service: list(hosts.values()) for service, hosts in sorted(found.items())}


def incidents_problems(manifest: Manifest) -> list[tuple[str, str, str]]:
    alerts = all_alerts(known_modules(manifest))
    problems = []
    for host in sorted(manifest.hosts):
        settings = manifest.host_modules(host).get("incidents")
        if settings is None:
            continue
        for name, _ in host_alert_entries(settings):
            if name not in alerts:
                problems.append(
                    (
                        f"hosts.{host}.modules.incidents.host_alerts",
                        "incidents-host-alert",
                        f"no module has an alert {name!r}",
                    )
                )
    return problems


def incidents_context(manifest: Manifest, all_settings: dict[str, Any]) -> dict[str, Any]:
    settings = all_settings.get("incidents")
    if settings is None:
        return {}
    return {
        "services": service_criticality(manifest),
        "night": night_hours(settings, all_settings, manifest.timezone),
        "hosts": host_services(manifest),
        "host_modules": host_module_names(manifest),
        "host_alerts": host_alert_matchers(manifest, settings),
        "routes": request_routes(manifest),
    }
