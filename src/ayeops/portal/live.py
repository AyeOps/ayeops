from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from ayeops.manifest import Manifest
from ayeops.portal.disks import disk_limits, disk_queries, disk_ranges
from ayeops.portal.graph import SCRAPE_TARGET, URL, ingress_routes
from ayeops.registry import Module
from ayeops.render import FleetRender, compose_file_services
from ayeops.rules import parse_size
from ayeops.strict import load_yaml

INTERVAL_SECONDS = 30
RANGE_SECONDS = 3600
RANGE_STEP_SECONDS = 60
PROBE_JOB = 'job="blackbox-http"'
SEEN_LOOKBACK = "1h"
RATE_WINDOW = "2m"
REGEX_SPECIAL = re.compile(r"([.^$*+?()\[\]{}|\\])")
PROJECT_CHARS = re.compile(r"[^a-z0-9_-]")


def regex_literal(text: str) -> str:
    return REGEX_SPECIAL.sub(r"\\\1", text)


def selector(hosts: list[str], *matchers: str) -> str:
    return "{" + ",".join([*matchers, f'host=~"{"|".join(hosts)}"']) + "}"


def size_bytes(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    try:
        return parse_size(str(value))
    except ValueError:
        return None


def number(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def compose_project(manifest: Manifest, compose: str) -> str:
    path = manifest.base_dir / compose
    data = load_yaml(path.read_text(encoding="utf-8")) if path.is_file() else None
    name = (data or {}).get("name") if isinstance(data, dict) else None
    if name:
        return str(name)
    folder = Path(compose).parent.name or manifest.base_dir.resolve().name
    return PROJECT_CHARS.sub("", folder.lower())


def container_pattern(project: str, name: str, service: dict[str, Any]) -> str:
    if service.get("container_name"):
        return regex_literal(str(service["container_name"]))
    prefix = regex_literal(project) if project else "[a-z0-9_-]+"
    return f"{prefix}[-_]{regex_literal(name)}[-_][0-9]+"


def scrape_instances(
    manifest: Manifest, host: str, modules: dict[str, Module], renders: FleetRender | None = None
) -> dict[str, list[str]]:
    scrape = (renders or FleetRender(manifest, modules)).scrape(host)
    found: dict[str, list[str]] = {}
    for job in (scrape or {}).get("scrape_configs") or []:
        for group in job.get("static_configs") or []:
            for target in group.get("targets") or []:
                match = SCRAPE_TARGET.match(str(target))
                if match and not URL.match(str(target)):
                    found.setdefault(match.group(1), []).append(str(target))
    return found


def probed_urls(
    manifest: Manifest, modules: dict[str, Module], renders: FleetRender | None = None
) -> list[str]:
    renders = renders or FleetRender(manifest, modules)
    urls: set[str] = set()
    for host in manifest.hosts:
        probes = renders.settings(host).get("probes") or {}
        urls.update(str(url) for url in probes.get("http") or [])
    return sorted(urls)


def service_probes(candidates: list[str], probed: list[str]) -> list[str]:
    wanted = {url.rstrip("/") for url in candidates if url}
    return [url for url in probed if url.rstrip("/") in wanted]


def live_spec(
    manifest: Manifest,
    modules: dict[str, Module],
    keys: list[tuple[str, str]],
    red: int,
    renders: FleetRender | None = None,
) -> dict[str, Any]:
    renders = renders or FleetRender(manifest, modules)
    routes: dict[tuple[str, str], list[str]] = {}
    for host, _, service, url in ingress_routes(manifest, modules, renders):
        routes.setdefault((host, service), []).append(url)
    probed = probed_urls(manifest, modules, renders)
    services: dict[str, Any] = {}
    hosts: dict[str, Any] = {}
    scraped_hosts: list[str] = []
    node_hosts: list[str] = []
    container_hosts: list[str] = []
    probe_hosts: list[str] = []
    forecast_hosts: list[str] = []
    smart_hosts: list[str] = []
    battery = False
    wanted = set(keys)
    for host in sorted(manifest.hosts):
        settings = renders.settings(host)
        if "metrics" in settings:
            scraped_hosts.append(host)
        if "node" in settings:
            node_hosts.append(host)
            battery = battery or bool(settings["node"].get("battery"))
        if "containers" in settings:
            container_hosts.append(host)
        if (settings.get("probes") or {}).get("http"):
            probe_hosts.append(host)
        if "node" in settings and "disk-forecast" in settings:
            forecast_hosts.append(host)
        if "smart" in settings:
            smart_hosts.append(host)
        hosts[host] = {
            "node": "node" in settings,
            "battery": bool((settings.get("node") or {}).get("battery")),
            "forecast": "node" in settings and "disk-forecast" in settings,
            "smart": "smart" in settings,
        }
        instances = scrape_instances(manifest, host, modules, renders)
        own = {n: e for n, e in manifest.services.items() if e.get("host") == host}
        project = f"{manifest.project}-{host}"
        rendered = renders.compose(host)["services"]
        composed = compose_file_services(manifest, host)
        names = sorted(set(rendered) | set(composed) | set(own))
        for name in names:
            if (host, name) not in wanted:
                continue
            entry = own.get(name, {})
            target: dict[str, Any]
            if name in rendered:
                target = rendered[name]
                owner = project
            else:
                target = composed.get(name, {})
                compose = str(target.get("x-aye-compose") or "")
                owner = str(target.get("x-aye-project") or "")
                if not owner and compose:
                    owner = compose_project(manifest, compose)
            candidates = [
                *routes.get((host, name), []),
                str(entry.get("url") or ""),
                str((entry.get("status") or {}).get("probe") or ""),
            ]
            has_container = "containers" in settings and (name in rendered or name in composed)
            services[f"{host}/{name}"] = {
                "host": host,
                "up": sorted(instances.get(name, [])),
                "probe": service_probes(candidates, probed),
                "container": container_pattern(owner, name, target) if has_container else None,
                "mem_limit": size_bytes(target.get("mem_limit")),
                "cpus": number(target.get("cpus")),
            }
    for host, name in keys:
        services.setdefault(
            f"{host}/{name}",
            {
                "host": host,
                "up": [],
                "probe": [],
                "container": None,
                "mem_limit": None,
                "cpus": None,
            },
        )
    return {
        "interval": INTERVAL_SECONDS,
        "red": red,
        "queries": {
            **queries(scraped_hosts, probe_hosts, container_hosts, node_hosts, battery),
            **disk_queries(modules, node_hosts, forecast_hosts, smart_hosts),
        },
        "ranges": {**ranges(probe_hosts, node_hosts), **disk_ranges(modules, node_hosts)},
        "disks": disk_limits(modules),
        "services": services,
        "hosts": hosts,
    }


def queries(
    scraped: list[str],
    probing: list[str],
    containers: list[str],
    nodes: list[str],
    battery: bool,
) -> dict[str, str]:
    found: dict[str, str] = {}
    if scraped:
        found["up"] = f"up{selector(scraped)}"
    if probing:
        found["probe"] = f"probe_success{selector(probing, PROBE_JOB)}"
        found["latency"] = f"probe_duration_seconds{selector(probing, PROBE_JOB)}"
    if containers:
        named = selector(containers, 'name!=""')
        found["seen"] = (
            f"time() - max by (host, name) (last_over_time(container_last_seen{named}"
            f"[{SEEN_LOOKBACK}]))"
        )
        found["mem"] = f"max by (host, name) (container_memory_working_set_bytes{named})"
        found["cpu"] = (
            f"sum by (host, name) (rate(container_cpu_usage_seconds_total{named}[{RATE_WINDOW}]))"
        )
    if nodes:
        any_host = selector(nodes)
        root = selector(nodes, 'mountpoint="/"')
        avail = f"node_filesystem_avail_bytes{root}"
        found["host_cpu"] = host_cpu(nodes)
        found["host_mem_total"] = f"max by (host) (node_memory_MemTotal_bytes{any_host})"
        found["host_mem_used"] = (
            f"max by (host) (node_memory_MemTotal_bytes{any_host}"
            f" - node_memory_MemAvailable_bytes{any_host})"
        )
        found["host_disk"] = (
            f"max by (host) (100 * (1 - {avail} / node_filesystem_size_bytes{root}))"
        )
        found["host_swap_total"] = f"max by (host) (node_memory_SwapTotal_bytes{any_host})"
        found["host_swap_used"] = (
            f"max by (host) (node_memory_SwapTotal_bytes{any_host}"
            f" - node_memory_SwapFree_bytes{any_host})"
        )
        found["host_uptime"] = f"max by (host) (time() - node_boot_time_seconds{any_host})"
        if battery:
            found["host_battery"] = f"max by (host) (ayeops_battery_capacity_percent{any_host})"
    return found


def host_cpu(nodes: list[str]) -> str:
    idle = selector(nodes, 'mode="idle"')
    return f"100 * (1 - avg by (host) (rate(node_cpu_seconds_total{idle}[{RATE_WINDOW}])))"


def ranges(probing: list[str], nodes: list[str]) -> dict[str, dict[str, Any]]:
    found: dict[str, str] = {}
    if nodes:
        found["host_cpu"] = host_cpu(nodes)
    if probing:
        found["latency"] = (
            f"max by (host, instance) (probe_duration_seconds{selector(probing, PROBE_JOB)})"
        )
    return {
        name: {"query": query, "seconds": RANGE_SECONDS, "step": RANGE_STEP_SECONDS}
        for name, query in found.items()
    }


def live_json(spec: dict[str, Any]) -> str:
    text = json.dumps(spec, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return text.replace("</", "<\\/")
