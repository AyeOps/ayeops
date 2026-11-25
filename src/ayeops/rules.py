from __future__ import annotations

import ipaddress
import json
import re
from collections import Counter
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ayeops.alert_links import board_from, dashboard_problems
from ayeops.autopilot import autopilot_problems
from ayeops.backups import backups_problems
from ayeops.baseline import DASHBOARD_UID as BASELINE_UID
from ayeops.baseline import dashboard as baseline_dashboard
from ayeops.baseline import is_baseline, rule_names, spec_problems
from ayeops.checks import check_alerts, check_problems, service_checks, sql_baseline
from ayeops.datastores import datastore_problems, datastore_warnings
from ayeops.downtime import downtime_problems
from ayeops.grafana_reader import SERVICE as READER_SERVICE
from ayeops.grafana_reader import consumer_problems, declared, mounts_volume
from ayeops.i18n import is_reserved
from ayeops.incidents import incidents_problems
from ayeops.local_modules import load_local_modules, with_local_modules
from ayeops.logs import LOKI_DATASOURCE_UID, loki_url, push_problems
from ayeops.manifest import (
    Manifest,
    schema_errors,
    unknown_profiles,
    unused_profiles,
    version_problems,
)
from ayeops.mcp import mcp_problems
from ayeops.offsite import objectstore_problems, offsite_problems
from ayeops.portal.brand import brand_problems
from ayeops.portal.graph import reference_findings
from ayeops.portal.icons import known_icons, resolve
from ayeops.promql import filtering_comparisons, may_be_empty
from ayeops.registry import Module, all_alerts
from ayeops.render import (
    AUTH_ROUTE,
    DATASOURCE_UID,
    METRICS_PORT,
    SERVICE_SILENCE,
    FleetRender,
    all_baseline_sources,
    compose_entries,
    compose_file_services,
    host_units,
    module_silence,
    project_dashboard_files,
    service_owners,
)
from ayeops.sentinel import sentinel_problems
from ayeops.status import reporter_problems, status_problems
from ayeops.strict import StrictValidator
from ayeops.texthygiene import manifest_problems
from ayeops.unused import unused_problems
from ayeops.waivers import reason_of, today_in, waiver_errors, waiver_warnings
from ayeops.yamlkeys import StrictYAMLError, read_strict

SIZE = re.compile(r"^([0-9]+(?:\.[0-9]+)?)([kmg])$")
SIZE_FACTORS = {"k": 1024, "m": 1024**2, "g": 1024**3}
MEMORY_BUDGET_PCT = 85
TODO = re.compile(r"^todo\b", re.IGNORECASE)


@dataclass(frozen=True, order=True)
class Finding:
    path: str
    rule: str
    message: str
    severity: str = "error"

    def __str__(self) -> str:
        return f"{self.severity} [{self.rule}] {self.path}: {self.message}"


def parse_size(value: str) -> int:
    match = SIZE.match(value.strip().lower())
    if match is None:
        raise ValueError(f"not a size: {value!r}")
    return int(float(match.group(1)) * SIZE_FACTORS[match.group(2)])


def filled(value: Any) -> bool:
    if isinstance(value, dict):
        return bool(value) and all(filled(text) for text in value.values())
    text = str(value or "").strip()
    return bool(text) and not TODO.match(text)


def check_alert(path: str, alert: dict[str, Any]) -> list[Finding]:
    findings = []
    if not alert.get("severity"):
        findings.append(Finding(path, "alert-severity", "alert has no severity"))
    if not filled(alert.get("runbook")):
        findings.append(Finding(path, "alert-runbook", "alert has no runbook"))
    return findings


def check_nodata(path: str, alert: dict[str, Any], no_data: str) -> list[Finding]:
    expr = str(alert.get("expr") or "")
    if no_data != "Alerting" or not may_be_empty(expr):
        return []
    operator = (filtering_comparisons(expr) or [">"])[-1]
    return [
        Finding(
            path,
            "alert-filter-nodata",
            f"{expr!r} filters with {operator!r} and returns no series while healthy, and no data "
            f"counts as firing here, so it fires when all is fine: write {operator} bool "
            "so a healthy target yields 0",
        )
    ]


def check_alert_traps(manifest: Manifest, modules: dict[str, Module]) -> list[Finding]:
    findings = [
        finding
        for name, service in sorted(manifest.active_services.items())
        for index, alert in enumerate(service.get("alerts") or [])
        for finding in check_nodata(f"services.{name}.alerts.{index}", alert, SERVICE_SILENCE[0])
    ]
    for name in sorted(manifest.enabled_modules() & set(modules)):
        findings.extend(module_alert_traps(modules[name]))
    return findings


def module_alert_traps(module: Module) -> list[Finding]:
    return [
        finding
        for alert in module.alerts
        for finding in check_nodata(
            f"modules.{module.name}.alerts.{alert['name']}", alert, module_silence(alert)[0]
        )
    ]


def check_service(path: str, service: dict[str, Any]) -> list[Finding]:
    findings = []
    if not filled(service.get("description")):
        findings.append(Finding(path, "service-description", "service has no description"))
    if is_reserved(service):
        return findings
    checks = service.get("checks") or {}
    metrics = service.get("metrics")
    if isinstance(metrics, dict):
        if not filled(reason_of(metrics.get("none"))):
            findings.append(
                Finding(path, "service-metrics", "metrics: none needs a reason, e.g. none: <why>")
            )
    elif not metrics and not checks:
        findings.append(
            Finding(path, "service-metrics", "service has no metric and no explicit metrics: none")
        )
    alerts = service.get("alerts") or []
    if not alerts and not checks:
        findings.append(Finding(path, "service-alerts", "service has no alert"))
    for index, alert in enumerate(alerts):
        findings.extend(check_alert(f"{path}.alerts.{index}", alert))
    for name, check in sorted(checks.items()):
        where = f"{path}.checks.{name}.alert"
        alert = check.get("alert") or {}
        findings.extend(check_alert(where, alert))
        if not alert.get("for"):
            findings.append(Finding(where, "alert-for", "a check alert needs for: <duration>"))
    if not service.get("logs"):
        findings.append(Finding(path, "service-logs", "service has no log source"))
    status = service.get("status") or {}
    if not any(filled(reason_of(status.get(key))) for key in ("public", "internal")):
        findings.append(
            Finding(path, "service-status", "service needs public: <component> or internal: <why>")
        )
    for index, dependency in enumerate(service.get("cloud") or []):
        where = f"{path}.cloud.{index}"
        for field in ("limit", "on_exceed"):
            if not filled(dependency.get(field)):
                findings.append(Finding(where, "cloud-limit", f"cloud dependency has no {field}"))
    return findings


def module_catalog_services(modules: dict[str, Module]) -> dict[str, dict[str, Any]]:
    alerts = all_alerts(modules)
    services: dict[str, dict[str, Any]] = {}
    for module in modules.values():
        for name, entry in module.catalog.items():
            resolved = dict(entry)
            resolved["alerts"] = [
                alerts[alert][1] if alert in alerts else {"name": alert}
                for alert in entry.get("alerts") or []
            ]
            services[f"modules.{module.name}.services.{name}"] = resolved
    return services


def check_local_modules(manifest: Manifest, modules: dict[str, Module]) -> list[Finding]:
    local, problems = load_local_modules(manifest)
    findings = [Finding(*problem) for problem in problems]
    alerts = all_alerts({**modules, **local})
    roots = manifest.module_paths
    for name, module in sorted(local.items()):
        path = f"module_paths.{roots.index(module.path.parent)}.{name}"
        referenced: set[str] = set()
        for service, entry in sorted(module.catalog.items()):
            where = f"{path}.services.{service}"
            names = list(entry.get("alerts") or [])
            referenced.update(names)
            findings.extend(
                Finding(where, "alert-known", f"unknown alert {alert!r}")
                for alert in names
                if alert not in alerts
            )
            resolved = {**entry, "alerts": [alerts[a][1] for a in names if a in alerts]}
            findings.extend(check_service(where, resolved))
        for position, alert in enumerate(module.alerts):
            if alert["name"] not in referenced:
                findings.extend(check_alert(f"{path}.alerts.{position}", alert))
    return findings


def check_modules(
    manifest: Manifest, modules: dict[str, Module], renders: FleetRender | None = None
) -> list[Finding]:
    renders = renders or FleetRender(manifest, modules)
    findings = []
    for host in sorted(manifest.hosts):
        for name in sorted(manifest.dropped_modules(host) - set(modules)):
            path = f"hosts.{host}.modules.{name}"
            findings.append(Finding(path, "module-known", f"unknown module {name!r}"))
        enabled = manifest.host_modules(host)
        for name, overrides in sorted(enabled.items()):
            path = f"hosts.{host}.modules.{name}"
            if name not in modules:
                findings.append(Finding(path, "module-known", f"unknown module {name!r}"))
                continue
            validator = StrictValidator(modules[name].settings_schema)
            for error in sorted(validator.iter_errors(overrides), key=str):
                where = ".".join([path, *map(str, error.absolute_path)])
                findings.append(Finding(where, "module-settings", error.message))
        settings = renders.settings(host)
        for name in sorted(settings):
            for required, match in sorted(modules[name].requires.items()):
                actual = settings.get(required)
                if actual is None or any(actual.get(k) != v for k, v in match.items()):
                    wanted = f"{required} {match}" if match else required
                    findings.append(
                        Finding(
                            f"hosts.{host}.modules.{name}",
                            "module-requires",
                            f"{name} needs {wanted} on the same host",
                        )
                    )
    return findings


def check_hosts(
    manifest: Manifest, modules: dict[str, Module], renders: FleetRender | None = None
) -> list[Finding]:
    renders = renders or FleetRender(manifest, modules)
    findings = []
    grafana_hosts = [h for h in manifest.hosts if "grafana" in manifest.host_modules(h)]
    if len(grafana_hosts) > 1:
        findings.append(
            Finding("hosts", "grafana-single", f"grafana is enabled on {len(grafana_hosts)} hosts")
        )
    delivery_hosts = [h for h in manifest.hosts if "delivery" in manifest.host_modules(h)]
    if len(delivery_hosts) > 1:
        findings.append(
            Finding(
                "hosts", "delivery-single", f"delivery is enabled on {len(delivery_hosts)} hosts"
            )
        )
    for host in sorted(manifest.hosts):
        spec = manifest.hosts[host]
        settings = renders.settings(host)
        if any(s.get("expose") for s in settings.values()) and not spec.get("public_bind"):
            findings.append(
                Finding(
                    f"hosts.{host}",
                    "expose-bind",
                    "exposed ports need an explicit public_bind address",
                )
            )
        metrics = settings.get("metrics")
        if metrics is not None:
            findings.extend(check_remote_write(manifest, host, metrics, modules, renders))
        if spec.get("memory"):
            findings.extend(check_memory_budget(manifest, host, modules, renders))
        findings.extend(port_clashes(f"hosts.{host}", renders.compose(host)["services"]))
        for compose, _ in compose_entries(spec):
            if not (manifest.base_dir / compose).is_file():
                findings.append(
                    Finding(f"hosts.{host}.compose", "compose-file", f"{compose} does not exist")
                )
    return findings


def check_compose_yaml(manifest: Manifest) -> list[Finding]:
    findings = []
    for host in sorted(manifest.hosts):
        for compose, _ in compose_entries(manifest.hosts[host]):
            path = manifest.base_dir / compose
            if not path.is_file():
                continue
            try:
                read_strict(path.read_text(encoding="utf-8"), compose, key_types=False)
            except StrictYAMLError as error:
                findings += [
                    Finding(f"hosts.{host}.compose", rule, message)
                    for _, rule, message in error.problems
                ]
    return findings


def port_clashes(path: str, services: dict[str, dict[str, Any]]) -> list[Finding]:
    owners: dict[int, list[tuple[str, str]]] = {}
    for name, service in services.items():
        for spec in service.get("ports") or []:
            parts = str(spec).split(":")
            bind = parts[0] if len(parts) == 3 else "0.0.0.0"
            port = int(parts[-2] if len(parts) > 1 else parts[0])
            owners.setdefault(port, []).append((name, bind))
    findings = []
    for port, users in sorted(owners.items()):
        binds = [bind for _, bind in users]
        if len(users) > 1 and (len(set(binds)) < len(binds) or "0.0.0.0" in binds):
            names = ", ".join(sorted(name for name, _ in users))
            findings.append(
                Finding(path, "port-clash", f"host port {port} is published by {names}")
            )
    return findings


def check_memory_budget(
    manifest: Manifest,
    host: str,
    modules: dict[str, Module],
    renders: FleetRender | None = None,
) -> list[Finding]:
    renders = renders or FleetRender(manifest, modules)
    spec = manifest.hosts[host]
    pct = int(spec.get("memory_budget_pct", MEMORY_BUDGET_PCT))
    budget = parse_size(spec["memory"]) * pct // 100
    services = renders.compose(host)["services"]
    containers = sum(parse_size(str(s["mem_limit"])) for s in services.values())
    reserved = sum(parse_size(size) for size in (spec.get("reserved") or {}).values())
    if containers + reserved <= budget:
        return []
    mib = 1024**2
    return [
        Finding(
            f"hosts.{host}",
            "memory-budget",
            f"mem_limit total {containers // mib}m plus reserved {reserved // mib}m"
            f" exceeds {pct} % of memory {spec['memory']} ({budget // mib}m)",
        )
    ]


def check_remote_write(
    manifest: Manifest,
    host: str,
    metrics: dict[str, Any],
    modules: dict[str, Module],
    renders: FleetRender | None = None,
) -> list[Finding]:
    path = f"hosts.{host}.modules.metrics.remote_write"
    target = metrics.get("remote_write") or {}
    if metrics.get("role") == "agent" and not target:
        return [Finding(path, "remote-write-target", "an agent needs remote_write")]
    if "host" not in target:
        return []
    central = target["host"]
    if central not in manifest.hosts:
        return [Finding(path, "remote-write-target", f"unknown host {central!r}")]
    renders = renders or FleetRender(manifest, modules)
    central_metrics = renders.settings(central).get("metrics") or {}
    problems = []
    if central_metrics.get("role") != "server":
        problems.append(f"{central} runs no metrics server")
    if METRICS_PORT not in (central_metrics.get("expose") or []):
        problems.append(f"{central} does not expose {METRICS_PORT}")
    if not manifest.hosts[central].get("address"):
        problems.append(f"{central} has no address")
    return [Finding(path, "remote-write-target", problem) for problem in problems]


def compose_services(manifest: Manifest, host: str) -> set[str]:
    return set(compose_file_services(manifest, host))


def alert_names(alert: dict[str, Any]) -> list[str]:
    name = str(alert["name"])
    return rule_names(name, name, alert["baseline"]) if is_baseline(alert) else [name]


def check_baselines(manifest: Manifest, modules: dict[str, Module]) -> list[Finding]:
    entries = [
        (f"modules.{name}.alerts.{index}.baseline", alert["baseline"])
        for name in sorted(manifest.enabled_modules() & set(modules))
        for index, alert in enumerate(modules[name].alerts)
        if is_baseline(alert)
    ]
    entries += [
        (f"services.{name}.alerts.{index}.baseline", alert["baseline"])
        for name, service in sorted(manifest.services.items())
        for index, alert in enumerate(service.get("alerts") or [])
        if is_baseline(alert)
    ]
    for check in service_checks(manifest):
        spec = sql_baseline(check)
        if spec is not None:
            entries.append((f"{check.path}.alert.baseline", spec))
    return [Finding(*problem) for path, spec in entries for problem in spec_problems(path, spec)]


def check_services(
    manifest: Manifest, modules: dict[str, Module], renders: FleetRender | None = None
) -> list[Finding]:
    renders = renders or FleetRender(manifest, modules)
    findings = []
    for name, service in sorted(manifest.services.items()):
        path = f"services.{name}"
        findings.extend(check_service(path, service))
        host = service.get("host", "")
        if host not in manifest.hosts:
            findings.append(Finding(path, "service-provided", f"unknown host {host!r}"))
            continue
        provided = (
            set(service_owners(manifest, host, modules, renders))
            | compose_services(manifest, host)
            | set(host_units(manifest, host))
        )
        if name not in provided and not is_reserved(service):
            findings.append(
                Finding(
                    path,
                    "service-provided",
                    f"dead entry: no module, compose file or unit on {host} provides {name}",
                )
            )
    module_alerts = [
        name
        for module in modules.values()
        for alert in module.alerts
        for name in alert_names(alert)
    ]
    service_alerts = [
        name
        for service in manifest.services.values()
        for alert in service.get("alerts") or []
        for name in alert_names(alert)
    ] + [alert["name"] for check in service_checks(manifest) for alert in check_alerts(check)]
    for alert, count in sorted(Counter(module_alerts + service_alerts).items()):
        if count > 1:
            findings.append(
                Finding("services", "alert-unique", f"alert {alert!r} is defined {count} times")
            )
    return findings


def container_ports(service: dict[str, Any]) -> set[int]:
    return {int(str(port).rsplit(":", 1)[-1]) for port in service.get("ports") or []}


def route_url(name: str, domain: str) -> str:
    return f"https://{name}.{domain}"


def check_ingress(
    manifest: Manifest, modules: dict[str, Module], renders: FleetRender | None = None
) -> list[Finding]:
    renders = renders or FleetRender(manifest, modules)
    findings = []
    names: dict[str, list[str]] = {}
    for host in sorted(manifest.hosts):
        settings = renders.settings(host)
        ingress = settings.get("ingress")
        if ingress is None:
            continue
        spec = manifest.hosts[host]
        path = f"hosts.{host}.modules.ingress"
        domain = str(ingress["domain"])
        address = str(spec.get("address") or "")
        if not address:
            findings.append(
                Finding(
                    f"hosts.{host}", "ingress-host", "ingress needs address for its DNS records"
                )
            )
        elif not is_ipv4(address):
            findings.append(
                Finding(
                    f"hosts.{host}",
                    "ingress-host",
                    f"address {address!r} is not an IPv4 address, ingress publishes A records",
                )
            )
        if not spec.get("public_bind"):
            findings.append(
                Finding(f"hosts.{host}", "ingress-host", "ingress needs public_bind for port 443")
            )
        services = renders.compose(host)["services"]
        for route, target in sorted(ingress["routes"].items()):
            names.setdefault(f"{route}.{domain}", []).append(host)
            service, port = str(target).rsplit(":", 1)
            where = f"{path}.routes.{route}"
            if service not in services:
                findings.append(
                    Finding(where, "ingress-route", f"{service} is not rendered on {host}")
                )
                continue
            ports = container_ports(services[service])
            if ports and int(port) not in ports:
                findings.append(
                    Finding(where, "ingress-route", f"{service} does not listen on {port}")
                )
        probed = {url.rstrip("/") for url in (settings.get("probes") or {}).get("http") or []}
        urls = [route_url(route, domain) for route in sorted(ingress["routes"])]
        if not probed & set(urls):
            findings.append(
                Finding(
                    path,
                    "ingress-probe",
                    f"probe a route from {host} so tls-expiring watches the certificate,"
                    f" e.g. probes.http: [{urls[0]}]",
                )
            )
    for name, hosts in sorted(names.items()):
        if len(hosts) > 1:
            findings.append(
                Finding("hosts", "ingress-unique", f"{name} is routed on {', '.join(hosts)}")
            )
    return findings


def check_portal(
    manifest: Manifest, modules: dict[str, Module], renders: FleetRender | None = None
) -> list[Finding]:
    renders = renders or FleetRender(manifest, modules)
    findings = []
    for host in sorted(manifest.hosts):
        settings = renders.settings(host)
        if "portal" not in settings:
            continue
        routes = (settings.get("ingress") or {}).get("routes") or {}
        if not any(str(target).rsplit(":", 1)[0] == "portal" for target in routes.values()):
            findings.append(
                Finding(
                    f"hosts.{host}.modules.portal",
                    "portal-route",
                    "route the portal through ingress on the same host, e.g. routes:"
                    " {portal: portal:8080}",
                )
            )
        if (settings.get("metrics") or {}).get("role") != "server":
            findings.append(
                Finding(
                    f"hosts.{host}.modules.portal",
                    "portal-metrics",
                    "the portal proxies live queries to VictoriaMetrics on its own host:"
                    " enable metrics with role: server there",
                )
            )
    return findings


BUILTIN_DATASOURCES = {"grafana", "-- Grafana --", "-- Mixed --", "-- Dashboard --", "__expr__"}


def datasource_refs(node: Any) -> list[str]:
    refs: list[str] = []
    if isinstance(node, dict):
        value = node.get("datasource")
        if isinstance(value, dict) and isinstance(value.get("uid"), str):
            refs.append(value["uid"])
        elif isinstance(value, str):
            refs.append(value)
        for child in node.values():
            refs.extend(datasource_refs(child))
    elif isinstance(node, list):
        for child in node:
            refs.extend(datasource_refs(child))
    return refs


def module_dashboard_uids(manifest: Manifest, modules: dict[str, Module]) -> set[str]:
    uids = set()
    for name in sorted(manifest.enabled_modules() & set(modules)):
        for dashboard in modules[name].dashboards:
            uids.add(str(json.loads(dashboard.read_text(encoding="utf-8")).get("uid")))
    return uids


def check_datasources(path: str, sources: list[dict[str, Any]]) -> list[Finding]:
    findings = []
    for key in ("uid", "name"):
        taken = Counter(str(source[key]) for source in sources)
        taken[DATASOURCE_UID if key == "uid" else "VictoriaMetrics"] += 1
        findings += [
            Finding(f"{path}.datasources", "grafana-datasource-unique", f"{key} {value!r} is taken")
            for value, count in sorted(taken.items())
            if count > 1
        ]
    return findings


def check_dashboards(path: str, directory: Path, known: set[str], taken: set[str]) -> list[Finding]:
    files = sorted(directory.glob("*.json")) if directory.is_dir() else []
    if not files:
        return [Finding(f"{path}.dashboards", "grafana-dashboards", f"no *.json in {directory}")]
    findings = []
    for file in files:
        where = f"{path}.dashboards.{file.name}"
        try:
            data = json.loads(file.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            findings.append(Finding(where, "grafana-dashboards", f"not JSON: {error}"))
            continue
        uid = data.get("uid") if isinstance(data, dict) else None
        if not isinstance(uid, str) or not uid:
            findings.append(Finding(where, "grafana-dashboards", "a dashboard needs a uid"))
        elif uid in taken:
            findings.append(Finding(where, "grafana-dashboard-uid", f"uid {uid!r} is taken"))
        else:
            taken.add(uid)
        for ref in sorted(set(datasource_refs(data))):
            if ref.startswith("$") or ref in BUILTIN_DATASOURCES or ref in known:
                continue
            findings.append(
                Finding(
                    where,
                    "grafana-datasource",
                    f"datasource {ref!r} is not declared: add it under datasources"
                    " or point the panels at a declared uid",
                )
            )
    return findings


def check_grafana(
    manifest: Manifest, modules: dict[str, Module], renders: FleetRender | None = None
) -> list[Finding]:
    renders = renders or FleetRender(manifest, modules)
    findings = []
    for host in sorted(manifest.hosts):
        settings = renders.settings(host).get("grafana")
        if settings is None:
            continue
        path = f"hosts.{host}.modules.grafana"
        sources = list(settings.get("datasources") or [])
        findings += check_datasources(path, sources)
        if settings.get("dashboards"):
            known = {DATASOURCE_UID, "VictoriaMetrics"}
            known |= {str(source["uid"]) for source in sources}
            known |= {str(source["name"]) for source in sources}
            if loki_url(manifest, host):
                known |= {LOKI_DATASOURCE_UID, "Loki"}
            directory = manifest.base_dir / str(settings["dashboards"])
            taken = module_dashboard_uids(manifest, modules)
            findings += check_dashboards(path, directory, known, taken)
    return findings


def check_grafana_reader(
    manifest: Manifest, modules: dict[str, Module], renders: FleetRender | None = None
) -> list[Finding]:
    renders = renders or FleetRender(manifest, modules)
    findings = []
    for host in sorted(manifest.hosts):
        grafana_here = "grafana" in renders.settings(host)
        services = renders.compose(host)["services"]
        for name, service in sorted(services.items()):
            owner = service["labels"]["io.ayeops.module"]
            path = f"hosts.{host}.modules.{owner}"
            if declared(modules[owner].catalog.get(name) or {}):
                problems = consumer_problems(name, service, grafana_here)
                findings += [Finding(path, "grafana-reader-mount", text) for text in problems]
            elif name != READER_SERVICE and mounts_volume(service):
                findings.append(
                    Finding(
                        path,
                        "grafana-reader-mount",
                        f"{name} mounts the reader token without grafana_reader: true"
                        " in its catalog entry",
                    )
                )
    return findings


def known_dashboards(
    manifest: Manifest, modules: dict[str, Module], renders: FleetRender | None = None
) -> dict[str, frozenset[int]]:
    renders = renders or FleetRender(manifest, modules)
    known = {}
    for module in modules.values():
        for dashboard in module.dashboards:
            if dashboard.is_file():
                board = board_from(module.name, json.loads(dashboard.read_text(encoding="utf-8")))
                known[board.uid] = frozenset(panel.id for panel in board.panels)
    for host in sorted(manifest.hosts):
        settings = renders.settings(host).get("grafana")
        for dashboard in project_dashboard_files(manifest, settings or {}):
            try:
                data = json.loads(dashboard.read_text(encoding="utf-8"))
            except ValueError:
                continue
            if isinstance(data, dict) and data.get("uid"):
                board = board_from("", data)
                known[board.uid] = frozenset(panel.id for panel in board.panels)
    return known


def check_alert_dashboards(
    manifest: Manifest, modules: dict[str, Module], renders: FleetRender | None = None
) -> list[Finding]:
    renders = renders or FleetRender(manifest, modules)
    entries = [
        (f"modules.{name}.alerts.{index}", alert)
        for name in sorted(manifest.enabled_modules() & set(modules))
        for index, alert in enumerate(modules[name].alerts)
    ]
    entries += [
        (f"services.{name}.alerts.{index}", alert)
        for name, service in sorted(manifest.services.items())
        for index, alert in enumerate(service.get("alerts") or [])
    ]
    entries += [
        (f"services.{check.service}.checks.{check.name}.alert", dict(check.spec.get("alert") or {}))
        for check in service_checks(manifest)
    ]
    known = known_dashboards(manifest, modules, renders)
    sources = all_baseline_sources(manifest, modules)
    if sources:
        board = board_from("", baseline_dashboard(sources))
        known[BASELINE_UID] = frozenset(panel.id for panel in board.panels)
    return [Finding(*problem) for problem in dashboard_problems(entries, known)]


def check_auth(
    manifest: Manifest, modules: dict[str, Module], renders: FleetRender | None = None
) -> list[Finding]:
    renders = renders or FleetRender(manifest, modules)
    findings = []
    for host in sorted(manifest.hosts):
        settings = renders.settings(host)
        ingress = settings.get("ingress")
        auth = settings.get("auth")
        if ingress is None:
            continue
        path = f"hosts.{host}.modules"
        if auth is None:
            if not ingress.get("without_auth"):
                findings.append(
                    Finding(
                        f"{path}.ingress",
                        "ingress-auth",
                        "ingress publishes UIs without a sign-in: enable auth on the same host,"
                        " or set without_auth: <why>",
                    )
                )
            continue
        own_routes = (manifest.host_modules(host).get("ingress") or {}).get("routes") or {}
        if AUTH_ROUTE in own_routes:
            findings.append(
                Finding(
                    f"{path}.ingress.routes.{AUTH_ROUTE}",
                    "auth-route",
                    f"the name {AUTH_ROUTE} is the sign-in callback of the auth module",
                )
            )
        for name in auth.get("open") or []:
            if name not in own_routes:
                findings.append(
                    Finding(f"{path}.auth.open", "auth-open", f"{name} is not an ingress route")
                )
        for email in sorted(set(auth.get("admins") or []) - set(auth.get("emails") or [])):
            findings.append(
                Finding(f"{path}.auth.admins", "auth-admins", f"{email} is not in emails")
            )
        if auth.get("provider") == "oidc" and "grafana" in settings and not auth.get("endpoints"):
            findings.append(
                Finding(
                    f"{path}.auth",
                    "auth-endpoints",
                    "Grafana's generic OAuth needs endpoints: {authorize, token, userinfo}"
                    " from the issuer's /.well-known/openid-configuration",
                )
            )
    return findings


def check_icons(manifest: Manifest) -> list[Finding]:
    declared = [
        *((f"hosts.{name}", spec.get("icon")) for name, spec in manifest.hosts.items()),
        *((f"services.{name}", spec.get("icon")) for name, spec in manifest.services.items()),
        *((f"externals.{name}", spec.get("icon")) for name, spec in manifest.externals.items()),
    ]
    known = ", ".join(sorted(known_icons()))
    return [
        Finding(f"{path}.icon", "icon-known", f"unknown icon {icon!r}; known: {known}, mcp")
        for path, icon in declared
        if icon and resolve(str(icon)) is None
    ]


MAIL_DOMAIN = re.compile(r"^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?[.])+[a-z]{2,63}$", re.IGNORECASE)
IMPLICIT_TLS_PORT = 465


def check_mail(manifest: Manifest) -> list[Finding]:
    findings = []
    mail = manifest.mail
    if mail is not None:
        domain = mail["from"].rpartition("@")[2]
        if not MAIL_DOMAIN.match(domain):
            findings.append(Finding("mail.from", "mail-from", f"{domain!r} is not a domain"))
        if (mail["tls"] == "tls") != (mail["port"] == IMPLICIT_TLS_PORT):
            findings.append(
                Finding("mail.tls", "mail-tls", "tls: tls means port 465, the others another port")
            )
    if manifest.contact == "email":
        if mail is None:
            findings.append(Finding("alerting.contact", "mail-contact", "contact email needs mail"))
        if not manifest.alert_emails:
            findings.append(
                Finding("alerting.emails", "mail-contact", "contact email needs emails")
            )
    elif manifest.alert_emails:
        findings.append(
            Finding("alerting.emails", "mail-contact", "emails are read only with contact email")
        )
    return findings


def check_references(
    manifest: Manifest, modules: dict[str, Module], renders: FleetRender | None = None
) -> list[Finding]:
    renders = renders or FleetRender(manifest, modules)
    return [
        Finding(path, rule, message)
        for path, rule, message in reference_findings(manifest, modules, renders)
    ]


def is_ipv4(address: str) -> bool:
    try:
        ipaddress.IPv4Address(address)
    except ValueError:
        return False
    return True


def check_oncall(manifest: Manifest) -> list[Finding]:
    findings = []
    for host in sorted(manifest.hosts):
        settings = manifest.host_modules(host).get("oncall")
        if settings is None:
            continue
        path = f"hosts.{host}.modules.oncall"
        if manifest.contact == "none":
            findings.append(
                Finding(
                    "alerting.contact",
                    "oncall-fallback",
                    "oncall needs contact telegram or email: critical alerts go there too, "
                    "so a stopped on-call path never means silence",
                )
            )
        person = settings.get("person") or {}
        if settings.get("twilio") and not person.get("phone"):
            findings.append(Finding(f"{path}.person", "oncall-phone", "twilio needs person.phone"))
    return findings


def known_zone(name: str) -> bool:
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return False
    return True


def zone_settings(manifest: Manifest) -> list[tuple[str, Any]]:
    found: list[tuple[str, Any]] = []
    if "timezone" in manifest.data:
        found.append(("timezone", manifest.data["timezone"]))
    for host in sorted(manifest.hosts):
        for name, settings in sorted(manifest.host_modules(host).items()):
            path = f"hosts.{host}.modules.{name}"
            if "timezone" in settings:
                found.append((f"{path}.timezone", settings["timezone"]))
            for key, value in sorted(settings.items()):
                if isinstance(value, dict) and "timezone" in value:
                    found.append((f"{path}.{key}.timezone", value["timezone"]))
    return found


def check_timezones(manifest: Manifest) -> list[Finding]:
    return [
        Finding(path, "timezone", f"unknown time zone {zone!r}; use an IANA name like Europe/Kyiv")
        for path, zone in zone_settings(manifest)
        if not known_zone(str(zone))
    ]


def check_waivers(manifest: Manifest, today: date | None = None) -> list[Finding]:
    day = today or today_in(manifest.timezone)
    return [Finding(*problem) for problem in waiver_errors(manifest, day)]


def unused_warnings(manifest: Manifest, modules: dict[str, Module]) -> list[tuple[str, str, str]]:
    problems = unused_profiles(manifest.raw)
    if unknown_profiles(manifest.raw) or check_local_modules(manifest, modules):
        return problems
    return problems + unused_problems(manifest, with_local_modules(manifest, modules))


def check_warnings(
    manifest: Manifest, today: date | None = None, modules: dict[str, Module] | None = None
) -> list[Finding]:
    if version_problems(manifest.raw) or schema_errors(manifest.raw):
        return []
    day = today or today_in(manifest.timezone)
    problems = waiver_warnings(manifest, day) + datastore_warnings(manifest, compose_file_services)
    if modules is not None:
        problems += unused_warnings(manifest, modules)
    return sorted(Finding(*problem, severity="warning") for problem in problems)


def check_manifest(
    manifest: Manifest, modules: dict[str, Module], today: date | None = None
) -> list[Finding]:
    pinned = [Finding(*problem) for problem in version_problems(manifest.raw)]
    if pinned:
        return pinned
    errors = [Finding(path, "schema", message) for path, message in schema_errors(manifest.raw)]
    errors += [Finding(*problem) for problem in unknown_profiles(manifest.raw)]
    if errors:
        return sorted(errors)
    findings = check_local_modules(manifest, modules)
    findings += check_compose_yaml(manifest)
    if findings:
        return sorted(findings)
    modules = with_local_modules(manifest, modules)
    renders = FleetRender(manifest, modules)
    findings = check_modules(manifest, modules, renders)
    if findings:
        return sorted(findings)
    findings += check_hosts(manifest, modules, renders)
    findings += check_services(manifest, modules, renders)
    findings += check_ingress(manifest, modules, renders)
    findings += check_portal(manifest, modules, renders)
    findings += check_grafana(manifest, modules, renders)
    findings += check_grafana_reader(manifest, modules, renders)
    findings += check_alert_dashboards(manifest, modules, renders)
    findings += check_baselines(manifest, modules)
    findings += check_auth(manifest, modules, renders)
    findings += check_icons(manifest)
    findings += check_mail(manifest)
    findings += check_oncall(manifest)
    findings += check_timezones(manifest)
    findings += [Finding(*problem) for problem in incidents_problems(manifest)]
    findings += [Finding(*problem) for problem in brand_problems(manifest)]
    findings += [Finding(*problem) for problem in push_problems(manifest)]
    findings += [Finding(*problem) for problem in check_problems(manifest)]
    findings += [Finding(*problem) for problem in downtime_problems(manifest)]
    findings += [Finding(*problem) for problem in offsite_problems(manifest)]
    findings += [Finding(*problem) for problem in backups_problems(manifest)]
    findings += [Finding(*problem) for problem in objectstore_problems(manifest)]
    findings += [Finding(*problem) for problem in datastore_problems(manifest)]
    findings += [Finding(*problem) for problem in mcp_problems(manifest)]
    findings += check_references(manifest, modules, renders)
    findings += check_alert_traps(manifest, modules)
    findings += [Finding(*problem) for problem in status_problems(manifest)]
    findings += [Finding(*problem) for problem in reporter_problems(manifest)]
    findings += [Finding(*problem) for problem in sentinel_problems(manifest)]
    findings += [Finding(*problem) for problem in autopilot_problems(manifest)]
    findings += [Finding(*problem) for problem in manifest_problems(manifest.raw)]
    findings += check_waivers(manifest, today)
    if not manifest.runbook_base_url and (manifest.services or manifest.enabled_modules()):
        findings.append(Finding("runbook_base_url", "alert-runbook", "runbook_base_url is not set"))
    return sorted(findings)
