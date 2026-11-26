from __future__ import annotations

import json
import re
from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ayeops.checks import service_alerts
from ayeops.i18n import pick, service_state
from ayeops.manifest import Manifest
from ayeops.registry import Module
from ayeops.waivers import reason_of

if TYPE_CHECKING:
    from ayeops.render import FleetRender

CATALOG_FILE = "mcp-catalog/catalog.json"
CHECKOUT_RUNBOOKS = Path(__file__).resolve().parents[2] / "docs" / "runbooks"
RUNBOOK_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
RUNBOOK_CHARS = 20000
HOST_FIELDS = ("zone", "arch", "provider")
SERVICE_LISTS = ("depends_on", "clients")
ALERT_FIELDS = ("title", "severity", "for", "summary", "expr")
STATUS_FIELDS = ("public", "internal", "probe")
MASK = "***"
USERINFO = re.compile(r"(?i)([a-z][a-z0-9+.-]*://)[^/\s@]+@")
SECRET_PARAM = re.compile(
    r"(?i)([?&;][^=&;\s#]*(?:token|key|secret|passw|pwd|sig|auth|credential)[^=&;\s#]*=)"
    r"[^&;\s#]+"
)

Found = dict[str, tuple[dict[str, Any], Traversable | None]]


def builtin_runbooks() -> Traversable:
    packaged = resources.files("ayeops") / "runbooks"
    return packaged if packaged.is_dir() else CHECKOUT_RUNBOOKS


def scrub(value: Any) -> Any:
    if isinstance(value, str):
        return SECRET_PARAM.sub(rf"\g<1>{MASK}", USERINFO.sub(rf"\g<1>{MASK}@", value))
    if isinstance(value, dict):
        return {key: scrub(item) for key, item in value.items()}
    if isinstance(value, list):
        return [scrub(item) for item in value]
    return value


def text(value: Any, lang: str) -> str:
    return pick(value, lang).strip()


def status_mark(value: Any, lang: str) -> dict[str, str] | None:
    if not isinstance(value, dict):
        return None
    mark = {key: text(reason_of(value[key]), lang) for key in STATUS_FIELDS if value.get(key)}
    return mark or None


def metrics_of(value: Any) -> list[str] | dict[str, str]:
    if isinstance(value, dict):
        return {"none": str(reason_of(value.get("none")) or "")}
    return [str(metric) for metric in value or []]


def host_entry(manifest: Manifest, host: str) -> dict[str, Any]:
    spec = manifest.hosts[host]
    entry: dict[str, Any] = {"name": host}
    group = manifest.host_group(host)
    if group:
        entry["group"] = group
    for key in HOST_FIELDS:
        if spec.get(key):
            entry[key] = str(spec[key])
    entry["modules"] = sorted(manifest.host_modules(host))
    return entry


def service_entry(
    manifest: Manifest,
    host: str,
    name: str,
    entry: dict[str, Any],
    source: str,
    alerts: list[str],
) -> dict[str, Any]:
    lang = manifest.portal_locale
    found: dict[str, Any] = {
        "id": f"{host}/{name}",
        "name": name,
        "host": host,
        "source": source,
        "state": service_state(entry),
        "description": text(entry.get("description"), lang),
    }
    group = manifest.host_group(host)
    if group:
        found["group"] = group
    if entry.get("criticality"):
        found["criticality"] = str(entry["criticality"])
    url = entry.get("url")
    if isinstance(url, str) and url.startswith(("http://", "https://")):
        found["url"] = url
    for key in SERVICE_LISTS:
        if entry.get(key):
            found[key] = [str(item) for item in entry[key]]
    found["metrics"] = metrics_of(entry.get("metrics"))
    found["alerts"] = list(dict.fromkeys(alerts))
    if entry.get("logs"):
        found["logs"] = str(entry["logs"])
    mark = status_mark(entry.get("status"), lang)
    if mark:
        found["status"] = mark
    cloud = [str(item["name"]) for item in entry.get("cloud") or [] if item.get("name")]
    if cloud:
        found["cloud"] = cloud
    return found


def alert_entry(
    alert: dict[str, Any], lang: str, url: str, owner: dict[str, str]
) -> dict[str, Any]:
    found: dict[str, Any] = {"name": str(alert["name"]), **owner}
    for key in ALERT_FIELDS:
        value = text(alert.get(key), lang)
        if value:
            found[key] = value
    baseline = alert.get("baseline")
    if isinstance(baseline, dict) and baseline.get("metric"):
        found["baseline"] = str(baseline["metric"])
    runbook = str(alert.get("runbook") or "").removesuffix(".md")
    if runbook:
        found["runbook"] = runbook
        found["runbook_url"] = url
    return found


def runbook_text(directory: Traversable | None, runbook: str) -> str | None:
    if directory is None or not RUNBOOK_NAME.match(runbook):
        return None
    page = directory / f"{runbook}.md"
    if not page.is_file():
        return None
    return page.read_text(encoding="utf-8").replace("\r\n", "\n")[:RUNBOOK_CHARS]


def module_alerts(manifest: Manifest, modules: dict[str, Module]) -> Found:
    from ayeops.render import alert_applies, module_runbook, runbook_url

    lang = manifest.portal_locale
    found: Found = {}
    builtin = builtin_runbooks()
    for name in sorted(manifest.enabled_modules() & set(modules)):
        module = modules[name]
        directory = module.path / "runbooks" if module.local else builtin
        for alert in module.alerts:
            if not alert_applies(manifest, modules, module, alert):
                continue
            shaped, base = module_runbook(manifest, module, alert)
            url = runbook_url(base, str(shaped["runbook"])) if alert.get("runbook") else ""
            entry = alert_entry(alert, lang, url, {"module": name})
            found.setdefault(entry["name"], (entry, directory))
    return found


def own_alerts(
    manifest: Manifest, name: str, entry: dict[str, Any] | None, found: Found
) -> list[str]:
    from ayeops.render import runbook_url

    if not entry:
        return []
    lang = manifest.portal_locale
    directory = manifest.base_dir / "runbooks"
    names: list[str] = []
    for alert in service_alerts(manifest, name, entry):
        runbook = str(alert.get("runbook") or "")
        url = runbook_url(manifest.runbook_base_url, runbook) if runbook else ""
        shaped = alert_entry(alert, lang, url, {"service": name})
        found.setdefault(shaped["name"], (shaped, directory))
        names.append(shaped["name"])
    return names


def rendered_module_services(
    manifest: Manifest,
    host: str,
    modules: dict[str, Module],
    renders: FleetRender | None = None,
) -> list[tuple[str, Module]]:
    from ayeops.render import FleetRender

    rendered = (renders or FleetRender(manifest, modules)).compose(host)["services"]
    found: list[tuple[str, Module]] = []
    for name, service in sorted(rendered.items()):
        module = modules.get(str((service.get("labels") or {}).get("io.ayeops.module")))
        if module is not None and name in module.catalog:
            found.append((name, module))
    return found


def catalog_data(
    manifest: Manifest, modules: dict[str, Module], renders: FleetRender | None = None
) -> dict[str, Any]:
    found = module_alerts(manifest, modules)
    own = manifest.services
    services: dict[str, dict[str, Any]] = {}
    for host in sorted(manifest.hosts):
        for name, module in rendered_module_services(manifest, host, modules, renders):
            names = [str(alert) for alert in module.catalog[name].get("alerts") or []]
            mine = own.get(name) if (own.get(name) or {}).get("host") == host else None
            names += own_alerts(manifest, name, mine, found)
            entry = {**module.catalog[name], **(mine or {})}
            shaped = service_entry(manifest, host, name, entry, f"module {module.name}", names)
            services[shaped["id"]] = shaped
    for name, entry in sorted(own.items()):
        host = str(entry.get("host") or "")
        if host not in manifest.hosts or f"{host}/{name}" in services:
            continue
        names = own_alerts(manifest, name, entry, found)
        shaped = service_entry(manifest, host, name, entry, "manifest", names)
        services[shaped["id"]] = shaped
    referenced = {alert for service in services.values() for alert in service["alerts"]}
    alerts = {name: found[name][0] for name in sorted(referenced & set(found))}
    runbooks: dict[str, dict[str, Any]] = {}
    for name in sorted(alerts):
        entry, directory = found[name]
        runbook = entry.get("runbook")
        if runbook and RUNBOOK_NAME.match(runbook) and runbook not in runbooks:
            runbooks[runbook] = {
                "name": runbook,
                "url": entry["runbook_url"],
                "text": runbook_text(directory, runbook),
            }
    return {
        "project": manifest.project,
        "hosts": {host: host_entry(manifest, host) for host in sorted(manifest.hosts)},
        "services": dict(sorted(services.items())),
        "alerts": alerts,
        "runbooks": dict(sorted(runbooks.items())),
    }


def catalog_json(
    manifest: Manifest, modules: dict[str, Module], renders: FleetRender | None = None
) -> str:
    data = scrub(catalog_data(manifest, modules, renders))
    return json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
