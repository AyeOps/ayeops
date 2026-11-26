from __future__ import annotations

import math
import re
from collections.abc import Iterable
from pathlib import PurePosixPath
from typing import Any

NAME = re.compile(r"^[a-z][a-z0-9-]{0,39}$")
MIB = 1024**2
GIB = 1024**3
SMART_KINDS = frozenset({"hdd", "ssd", "nvme"})
LINK_KINDS = frozenset({"wired", "wifi"})
COVERED_APPS = frozenset({"docker", "victoriametrics", "grafana"})
NOT_ENABLED = "suggested, not enabled"

EXPORTERS = {
    "postgres": "prometheuscommunity/postgres-exporter",
    "pgbouncer": "prometheuscommunity/pgbouncer-exporter",
    "mysql": "prom/mysqld-exporter",
    "mariadb": "prom/mysqld-exporter",
    "redis": "oliver006/redis_exporter",
    "valkey": "oliver006/redis_exporter",
    "nginx": "nginx/nginx-prometheus-exporter, needs stub_status",
    "caddy": "built-in metrics on the admin endpoint",
    "traefik": "built-in Prometheus metrics",
    "php-fpm": "hipages/php-fpm_exporter, needs pm.status_path",
    "node": "prom-client inside the application",
    "java": "prometheus/jmx_exporter as a Java agent",
    "prometheus": "built-in /metrics",
    "mongodb": "percona/mongodb_exporter",
    "memcached": "prom/memcached-exporter",
    "haproxy": "built-in Prometheus exporter",
    "apache": "lusitaniae/apache_exporter, needs mod_status",
    "rabbitmq": "built-in rabbitmq_prometheus plugin",
    "clickhouse": "built-in Prometheus endpoint",
    "minio": "built-in Prometheus endpoint",
    "loki": "built-in /metrics",
    "elasticsearch": "prometheuscommunity/elasticsearch-exporter",
    "kafka": "danielqsj/kafka-exporter",
    "influxdb": "built-in /metrics",
    "etcd": "built-in /metrics",
    "gunicorn": "statsd-exporter, or prometheus_client in the application",
    "uwsgi": "timonwong/uwsgi_exporter",
    "kubernetes": "kube-state-metrics and the kubelet metrics",
}


def manifest_name(text: str, fallback: str) -> str:
    name = re.sub(r"[^a-z0-9-]+", "-", text.strip().lower()).strip("-")
    if not name:
        name = fallback
    if not name[0].isalpha():
        name = f"{fallback}-{name}"
    return name[:40].rstrip("-")


def size_up(value: int) -> str:
    if value >= GIB:
        return f"{math.ceil(value / GIB)}g"
    return f"{max(1, math.ceil(value / MIB))}m"


def host_modules(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    modules: dict[str, dict[str, Any]] = {"metrics": {}, "node": {}}
    if any(s.get("type") == "Battery" for s in report.get("power_supplies") or []):
        modules["node"] = {"battery": True}
    if (report.get("containers") or {}).get("runtime") == "docker":
        modules["containers"] = {}
    storage = report.get("storage") or {}
    if any(
        disk.get("kind") in SMART_KINDS and (disk.get("smart") or {}).get("available") is not False
        for disk in storage.get("disks") or []
    ):
        modules["smart"] = {}
    if storage.get("filesystems"):
        modules["disk-forecast"] = {}
    if any(gpu.get("vendor") == "nvidia" for gpu in report.get("gpus") or []):
        modules["gpu"] = {}
    links = [
        link
        for link in report.get("network") or []
        if link.get("kind") in LINK_KINDS and link.get("operstate") == "up"
    ]
    if links:
        network: dict[str, Any] = {"interfaces": [link["name"] for link in links]}
        if any(link["kind"] == "wifi" for link in links):
            network["wifi"] = True
        modules["network"] = network
    if report.get("gateway"):
        modules["probes"] = {"internet": True, "gateway": report["gateway"]}
    return modules


def reserved_memory(report: dict[str, Any]) -> dict[str, str]:
    return {
        app["name"]: size_up(int(app["rss_bytes"]))
        for app in report.get("apps") or []
        if app.get("host_process") and app.get("rss_bytes") and app["name"] not in COVERED_APPS
    }


def compose_projects(report: dict[str, Any]) -> list[dict[str, Any]]:
    return list((report.get("containers") or {}).get("compose_projects") or [])


def service_stubs(
    report: dict[str, Any], host: str, known_services: Iterable[str]
) -> dict[str, dict[str, Any]]:
    known = set(known_services)
    stubs: dict[str, dict[str, Any]] = {}
    for project in compose_projects(report):
        for service in project.get("services") or []:
            original = str(service.get("service", ""))
            if not original or original in known:
                continue
            name = original if NAME.match(original) else manifest_name(original, "service")
            if name in stubs:
                name = manifest_name(f"{project['name']}-{original}", "service")
            stubs[name] = {
                "host": host,
                "description": f"TODO: what {original} does and for whom",
                "metrics": [],
                "alerts": [],
                "logs": "docker",
                "status": {"internal": "TODO: a public status component, or why it is internal"},
            }
    return stubs


def exporter_suggestions(report: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "app": app["name"],
            "evidence": list(app.get("evidence") or []),
            "exporter": EXPORTERS[app["name"]],
            "status": NOT_ENABLED,
        }
        for app in report.get("apps") or []
        if app.get("name") in EXPORTERS
    ]


def gpu_notes(report: dict[str, Any]) -> list[str]:
    vendors = sorted({gpu.get("vendor", "") for gpu in report.get("gpus") or []} - {"nvidia"})
    return [
        f"{vendor} GPU found; the gpu module watches NVIDIA cards only, {vendor} is {NOT_ENABLED}"
        for vendor in vendors
        if vendor in {"amd", "intel"}
    ]


def suggest(report: dict[str, Any], known_services: Iterable[str] = ()) -> dict[str, Any]:
    system = report.get("system") or {}
    host = manifest_name(str(system.get("hostname") or ""), "host")
    block: dict[str, Any] = {
        "arch": system.get("arch") or "unknown",
        "modules": host_modules(report),
    }
    total = int((report.get("memory") or {}).get("total_bytes") or 0)
    if total:
        block["memory"] = f"{total // MIB}m"
    reserved = reserved_memory(report)
    if reserved:
        block["reserved"] = reserved
    projects = compose_projects(report)
    compose = [
        f"{project['name']}/{PurePosixPath(path).name}"
        for project in projects
        for path in project.get("config_files") or []
    ]
    if compose:
        block["compose"] = compose
    containers = report.get("containers") or {}
    systemd = report.get("systemd") or {}
    suggested: dict[str, Any] = {
        "exporters": exporter_suggestions(report),
        "compose_files": {
            project["name"]: list(project.get("config_files") or []) for project in projects
        },
        "standalone_containers": [c["name"] for c in containers.get("standalone_containers") or []],
        "systemd_services": list(systemd.get("services") or []),
        "systemd_timers": [t["timer"] for t in systemd.get("timers") or []],
        "notes": gpu_notes(report) + list(report.get("notes") or []),
    }
    return {
        "hosts": {host: block},
        "services": service_stubs(report, host, known_services),
        "suggested": {key: value for key, value in suggested.items() if value},
    }
