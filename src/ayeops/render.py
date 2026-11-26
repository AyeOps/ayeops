from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

import jinja2
import yaml

from ayeops import downtime, grafana_reader
from ayeops.alert_links import Board, board_from, link_annotations, pick_link
from ayeops.autopilot import CATALOG_FILE as AUTOPILOT_CATALOG
from ayeops.autopilot import NAME as AUTOPILOT
from ayeops.autopilot import RECEIVER as AUTOPILOT_RECEIVER
from ayeops.autopilot import ROUTE as AUTOPILOT_ROUTE
from ayeops.autopilot import autopilot_catalog, autopilot_context
from ayeops.backups import backup_units, backups_context
from ayeops.baseline import DASHBOARD_UID as BASELINE_UID
from ayeops.baseline import dashboard as baseline_dashboard
from ayeops.baseline import expand, is_baseline
from ayeops.checks import baseline_sources, checks_context, service_alerts
from ayeops.dashboards import build, localize, requirement_met
from ayeops.datastores import job_rpos, offsite_max_age
from ayeops.downtime import Downtime
from ayeops.duties import SCOPE as DUTY_SCOPE
from ayeops.duties import overdue_alert, runbook_annotation
from ayeops.eol import tracked as eol_tracked
from ayeops.i18n import LANGS
from ayeops.incidents import incidents_context
from ayeops.links import linked as links_linked
from ayeops.links import links_context
from ayeops.local_modules import with_local_modules
from ayeops.logs import compose_project, declared_projects, logs_context, loki_datasource
from ayeops.manifest import Manifest
from ayeops.mcp import mcp_context, mcp_files
from ayeops.mounts import stamp_config_hashes
from ayeops.offsite import drill_context, offsite_context, quota_bytes, reststore_context
from ayeops.registry import Module, all_alerts
from ayeops.sentinel import sentinel_context
from ayeops.stamp import Stamp
from ayeops.status import reporter_context
from ayeops.strict import EnvRef, compose_safe, load_yaml

LOCALHOST = "127.0.0.1"


def host_port(port: int | str) -> int:
    return int(str(port).split(":", 1)[0])


def container_port(port: int | str) -> int:
    return int(str(port).rsplit(":", 1)[-1])


METRICS_PORT = 8428
DATASOURCE_UID = "victoriametrics"
FOLDER = "AyeOps"
PROJECT_DASHBOARDS = "/etc/grafana/project-dashboards"
POSTGRES_MAX_CONNECTIONS = 2
POSTGRES_CONN_LIFETIME = 60
DATASOURCE_TYPES = {
    "postgres": "grafana-postgresql-datasource",
    "loki": "loki",
    "prometheus": "prometheus",
}
ENV_REFERENCE = re.compile(r"\$\{([A-Z][A-Z0-9_]*)")
PLACEHOLDER = "replace-me"


def plain_str(dumper: yaml.representer.SafeRepresenter, value: str) -> yaml.ScalarNode:
    return dumper.represent_str(str(value))


FAST_DUMPER: Any = getattr(yaml, "CSafeDumper", yaml.SafeDumper)
FAST_DUMPER.add_representer(EnvRef, plain_str)
BMP_END = "\uffff"


def astral(data: Any) -> bool:
    if isinstance(data, str):
        return not data.isascii() and max(data) > BMP_END
    if isinstance(data, dict):
        return any(astral(key) or astral(value) for key, value in data.items())
    if isinstance(data, (list, tuple)):
        return any(astral(item) for item in data)
    return False


def dump_yaml(data: Any) -> str:
    return yaml.dump(
        data,
        Dumper=yaml.SafeDumper if astral(data) else FAST_DUMPER,
        sort_keys=True,
        default_flow_style=False,
        allow_unicode=True,
        width=1000,
    )


def dump_json(data: Any) -> str:
    return json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


MEBIBYTES = {"k": 1 / 1024, "m": 1, "g": 1024}
HEAP_SHARE = 0.75


def heap_mb(size: str) -> int:
    match = re.fullmatch(r"(\d+(?:\.\d+)?)([kmg])b?", str(size).strip().lower())
    if match is None:
        raise ValueError(f"not a size: {size!r}")
    return int(float(match.group(1)) * MEBIBYTES[match.group(2)] * HEAP_SHARE)


@cache
def _jinja() -> jinja2.Environment:
    environment = jinja2.Environment(
        undefined=jinja2.StrictUndefined,
        keep_trailing_newline=True,
        trim_blocks=True,
        lstrip_blocks=True,
        autoescape=False,
    )
    environment.filters["heap_mb"] = heap_mb
    environment.filters["quota_bytes"] = quota_bytes
    return environment


@cache
def compiled(source: str) -> jinja2.Template:
    return _jinja().from_string(source)


DASHBOARD_WORD = {"en": "dashboard", "uk": "дашборд"}


def html_link(url: str, word: str) -> str:
    return f' · <a href="{{{{ {url} }}}}">{word}</a>'


def mrkdwn_link(url: str, word: str) -> str:
    return f" · <{{{{ {url} }}}}|{word}>"


def html_strong(text: str) -> str:
    return f"<b>{text}</b>"


def as_is(text: str) -> str:
    return text


def shown(value: str) -> str:
    return f"{{{{ {value} }}}}"


def escaped(value: str) -> str:
    return f'{{{{ template "aye.escape" {value} }}}}'


@dataclass(frozen=True)
class MessageStyle:
    link: Callable[[str, str], str]
    strong: Callable[[str], str]
    show: Callable[[str], str]


TELEGRAM_STYLE = MessageStyle(html_link, html_strong, shown)
SLACK_STYLE = MessageStyle(mrkdwn_link, as_is, escaped)
SLACK_ESCAPE = (
    '{{ define "aye.escape" }}{{ . | reReplaceAll "&" "&amp;" '
    '| reReplaceAll "<" "&lt;" | reReplaceAll ">" "&gt;" }}{{ end }}'
)


def message_group(state: str, mark: str, style: MessageStyle) -> str:
    alerts = f".Alerts.{state}"
    return (
        f"{{{{ if {alerts} }}}}{mark} "
        + style.strong(f"{{{{ len {alerts} }}}} x " + style.show(".CommonLabels.alertname"))
        + "{{ if .GroupLabels.scope }} · "
        + style.show(".GroupLabels.scope")
        + "{{ end }}"
        + f'{{{{ template "aye.links" index {alerts} 0 }}}}\n'
        + f"{{{{ if .CommonLabels.host }}}}{{{{ range {alerts} }}}}"
        + style.show(".Annotations.summary")
        + "\n{{ end }}"
        + f'{{{{ else }}}}{{{{ template "aye.hosts" {alerts} }}}}\n{{{{ end }}}}\n{{{{ end }}}}'
    )


def message_groups(style: MessageStyle) -> str:
    return message_group("Firing", "🔴", style) + message_group("Resolved", "✅", style)


def message_parts(lang: str, style: MessageStyle) -> str:
    word = DASHBOARD_WORD.get(lang, DASHBOARD_WORD["en"])
    return (
        '{{ define "aye.host" }}'
        "{{ if .Labels.host }}{{ .Labels.host }}{{ else }}{{ .Labels.instance }}{{ end }}"
        "{{ end }}"
        '{{ define "aye.hosts" }}{{ template "aye.host" index . 0 }}'
        '{{ range slice . 1 }}, {{ template "aye.host" . }}{{ end }}{{ end }}'
        '{{ define "aye.links" }}'
        "{{ if .Annotations.runbook_url }}"
        + style.link(".Annotations.runbook_url", "runbook")
        + "{{ end }}{{ if .PanelURL }}"
        + style.link(".PanelURL", word)
        + "{{ else if .DashboardURL }}"
        + style.link(".DashboardURL", word)
        + "{{ end }}{{ end }}"
    )


def telegram_template(lang: str) -> str:
    return (
        message_parts(lang, TELEGRAM_STYLE)
        + '{{ define "aye.telegram" }}{{ if gt (len .Alerts) 1 }}'
        + message_groups(TELEGRAM_STYLE)
        + "{{ else }}{{ range .Alerts }}"
        '{{ if eq .Status "firing" }}🔴{{ else }}✅{{ end }} <b>{{ .Labels.alertname }}</b>\n'
        "{{ .Annotations.summary }}\n"
        '{{ .Labels.host }}{{ template "aye.links" . }}'
        "\n\n{{ end }}{{ end }}{{ end }}"
    )


SLACK_TITLE = (
    '{{ define "aye.slack.title" }}'
    "{{ if .Alerts.Firing }}🔴{{ else }}✅{{ end }} {{ .CommonLabels.alertname }}"
    "{{ if gt (len .Alerts) 1 }}{{ if .GroupLabels.scope }} · {{ .GroupLabels.scope }}"
    "{{ end }}{{ end }}{{ end }}"
)


def slack_template(lang: str) -> str:
    return (
        SLACK_ESCAPE
        + message_parts(lang, SLACK_STYLE)
        + SLACK_TITLE
        + '{{ define "aye.slack" }}{{ if gt (len .Alerts) 1 }}'
        + message_groups(SLACK_STYLE)
        + "{{ else }}{{ range .Alerts }}"
        + escaped(".Annotations.summary")
        + '\n{{ .Labels.host }}{{ template "aye.links" . }}'
        "{{ end }}{{ end }}{{ end }}"
    )


SLACK_WEBHOOK_ENV = "AYE_SLACK_WEBHOOK_URL"
MESSAGE_TEMPLATES: dict[str, Callable[[str], str]] = {
    "telegram": telegram_template,
    "slack": slack_template,
}
TELEGRAM_TEMPLATE = telegram_template("en")

MODULE_RUNBOOK_BASE = "https://github.com/AyeOps/ayeops/blob/main/docs/runbooks/"


def runbook_url(base: str, runbook: str) -> str:
    if runbook.startswith(("http://", "https://")):
        return runbook
    if base.endswith(("#", "/")):
        return f"{base}{runbook}"
    return f"{base}/{runbook}"


def rule_uid(name: str) -> str:
    if len(name) <= 36:
        return f"aye-{name}"
    return f"aye-{hashlib.sha1(name.encode()).hexdigest()[:16]}"


def remote_write_url(manifest: Manifest, settings: dict[str, Any]) -> str:
    target = settings.get("remote_write") or {}
    if "url" in target:
        return str(target["url"])
    if "host" in target:
        address = manifest.hosts.get(target["host"], {}).get("address", target["host"])
        return f"http://{address}:{METRICS_PORT}/api/v1/write"
    if settings.get("role") == "server":
        return f"http://victoriametrics:{METRICS_PORT}/api/v1/write"
    return ""


AUTH_ROUTE = "auth"
AUTH_TARGET = "oauth2-proxy:4180"
SSO = "sso"
VMUI = "vmui"
PROVIDER_TITLES = {"google": "Google", "oidc": "OIDC"}


def link_settings(settings: dict[str, Any]) -> dict[str, Any]:
    ingress = settings.get("ingress")
    if ingress is None or "auth" not in settings:
        return settings
    routes = ingress.get("routes")
    if isinstance(routes, dict):
        settings["ingress"] = {**ingress, "routes": {AUTH_ROUTE: AUTH_TARGET, **routes}}
    return settings


def host_settings(manifest: Manifest, host: str, modules: dict[str, Module]) -> dict[str, Any]:
    return link_settings(
        {
            name: modules[name].settings(overrides, manifest.timezone)
            for name, overrides in sorted(manifest.host_modules(host).items())
            if name in modules
        }
    )


def route_service(target: Any) -> str:
    return str(target).rsplit(":", 1)[0]


def route_middlewares(all_settings: dict[str, Any]) -> dict[str, list[str]]:
    routes = (all_settings.get("ingress") or {}).get("routes") or {}
    auth = all_settings.get("auth")
    open_routes = set((auth or {}).get("open") or [])
    result: dict[str, list[str]] = {}
    for name, target in routes.items():
        service = route_service(target)
        middlewares = []
        gated = auth is not None and name != AUTH_ROUTE and service != "grafana"
        if gated and name not in open_routes:
            middlewares.append(SSO)
        if service == "victoriametrics":
            middlewares.append(VMUI)
        result[name] = middlewares
    return result


def sign_in(all_settings: dict[str, Any], service: str) -> str | None:
    auth = all_settings.get("auth")
    if auth is None:
        return None
    provider = PROVIDER_TITLES[str(auth.get("provider", "google"))]
    if service == "grafana":
        return (
            f"{provider} sign-in for the auth emails; the AYE_GRAFANA_ADMIN_USER password form"
            " stays as break-glass"
        )
    routes = (all_settings.get("ingress") or {}).get("routes") or {}
    middlewares = route_middlewares(all_settings)
    if any(route_service(t) == service and SSO in middlewares[n] for n, t in routes.items()):
        return f"{provider} sign-in through oauth2-proxy, the auth emails only"
    return None


TEXTFILE_VOLUME = "node-textfile"


def textfile_source(all_settings: dict[str, Any]) -> str:
    directory = (all_settings.get("node") or {}).get("textfile_dir")
    return str(directory) if directory else TEXTFILE_VOLUME


def env_required(name: str) -> str:
    return EnvRef("${" + name + ":?set " + name + "}")


def mail_context(manifest: Manifest) -> dict[str, Any] | None:
    mail = manifest.mail
    if mail is None:
        return None
    return {
        **mail,
        "address": f"{mail['host']}:{mail['port']}",
        "user": env_required("AYE_SMTP_USER"),
        "password": env_required("AYE_SMTP_PASSWORD"),
    }


def blackbox_wanted(probes: dict[str, Any] | None) -> bool:
    return any((probes or {}).get(key) for key in ("http", "internet", "gateway", "links"))


def host_context(
    manifest: Manifest, host: str, all_settings: dict[str, Any], reader: bool = False
) -> dict[str, Any]:
    spec = manifest.hosts[host]
    metrics = all_settings.get("metrics")
    return {
        "host": {
            "name": host,
            "arch": spec["arch"],
            "address": spec.get("address", host),
            "group": manifest.host_group(host),
        },
        "modules": all_settings,
        "middlewares": route_middlewares(all_settings),
        "project": manifest.project,
        "timezone": manifest.timezone,
        "contact": manifest.contact,
        "mail": mail_context(manifest),
        "remote_write_url": remote_write_url(manifest, metrics) if metrics else "",
        "checks": checks_context(manifest, host, all_settings.get("checks") or {}),
        "textfile": textfile_source(all_settings),
        "logs": logs_context(manifest, host, all_settings.get("logs")),
        "offsite": offsite_context(manifest, host, all_settings.get("offsite"))
        | {"max_age": offsite_max_age(manifest, host)},
        "backups": backups_context(all_settings, job_rpos(manifest, host)),
        "reststore": reststore_context(manifest, host, all_settings.get("objectstore")),
        "drill": drill_context(manifest, host, all_settings.get("objectstore")),
        "sentinel": sentinel_context(manifest, all_settings.get("sentinel")),
        "incidents": incidents_context(manifest, all_settings),
        "status": reporter_context(manifest, all_settings.get("status")),
        "blackbox": blackbox_wanted(all_settings.get("probes")),
        "links": links_context(manifest, host, all_settings.get("probes")),
        "mcp": mcp_context(manifest, host, all_settings.get("mcp")),
        "autopilot": autopilot_context(manifest, host, all_settings),
        "grafana_reader": grafana_reader.context(reader and "grafana" in all_settings),
    }


def module_context(
    manifest: Manifest,
    host: str,
    settings: dict[str, Any],
    all_settings: dict[str, Any],
    reader: bool = False,
    shared: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if shared is None:
        shared = host_context(manifest, host, all_settings, reader)
    return {**shared, "settings": settings}


@dataclass(frozen=True)
class HostRender:
    settings: dict[str, Any]
    context: dict[str, Any]


def host_render(
    manifest: Manifest,
    host: str,
    modules: dict[str, Module],
    settings: dict[str, Any] | None = None,
) -> HostRender:
    all_settings = host_settings(manifest, host, modules) if settings is None else settings
    reader = grafana_reader.consumed(all_settings, modules)
    return HostRender(all_settings, host_context(manifest, host, all_settings, reader))


def render_template(module: Module, name: str, context: dict[str, Any]) -> Any:
    if not (module.path / name).exists():
        return None
    return load_yaml(compiled(module.template(name)).render(**context))


DEFAULT_LOGGING: dict[str, Any] = {
    "driver": "json-file",
    "options": {"max-size": "10m", "max-file": "3"},
}


def service_logging(module: Module, service: dict[str, Any]) -> dict[str, Any]:
    if service.get("logging"):
        return dict(service["logging"])
    return copy.deepcopy(module.spec.get("logging") or DEFAULT_LOGGING)


def finalize_service(
    manifest: Manifest,
    host: str,
    module: Module,
    settings: dict[str, Any],
    name: str,
    service: dict[str, Any],
) -> dict[str, Any]:
    spec = manifest.hosts[host]
    catalog = module.catalog.get(name, {})
    resources = (settings.get("resources") or {}).get(name, {})
    result = dict(service)
    exposed = set(settings.get("expose") or [])
    public_bind = spec.get("public_bind", LOCALHOST)
    ports = result.pop("ports", None) or []
    if ports:
        result["ports"] = [
            f"{public_bind if host_port(port) in exposed else LOCALHOST}:{host_port(port)}:"
            f"{container_port(port)}"
            for port in ports
        ]
    result["mem_limit"] = resources.get("memory", catalog.get("memory"))
    result["cpus"] = resources.get("cpus", catalog.get("cpus"))
    result["platform"] = f"linux/{spec['arch']}"
    result["restart"] = "unless-stopped"
    result["logging"] = service_logging(module, service)
    labels = dict(result.get("labels") or {})
    labels["io.ayeops.service"] = name
    labels["io.ayeops.module"] = module.name
    result["labels"] = labels
    return result


RECONCILER = "reconciler"
RECONCILER_PROJECTS = "AYE_RECONCILER_PROJECTS"
RECONCILER_DECLARED = "AYE_RECONCILER_DECLARED"


def kept_running(service: dict[str, Any]) -> bool:
    if service.get("restart") in ("no", False) or service.get("profiles"):
        return False
    replicas = (service.get("deploy") or {}).get("replicas", service.get("scale"))
    return replicas != 0


def desired_projects(
    manifest: Manifest,
    host: str,
    project: str,
    services: dict[str, Any],
    settings: dict[str, Any],
) -> dict[str, list[str]]:
    excluded = {str(entry) for entry in settings.get("exclude") or []}
    found: dict[str, set[str]] = {project: set(services)}
    for compose, declared in compose_entries(manifest.hosts[host]):
        path = manifest.base_dir / compose
        data = yaml.safe_load(path.read_text(encoding="utf-8")) if path.is_file() else None
        listed = (data or {}).get("services") if isinstance(data, dict) else None
        name = compose_project(manifest, compose, declared)
        kept = {s for s, spec in (listed or {}).items() if kept_running(spec or {})}
        found[name] = found.get(name, set()) | kept
    desired = {
        name: sorted(s for s in names if s not in excluded and f"{name}/{s}" not in excluded)
        for name, names in found.items()
    }
    return {name: names for name, names in sorted(desired.items()) if names}


def with_desired(
    service: dict[str, Any], desired: dict[str, list[str]], declared: list[str]
) -> dict[str, Any]:
    environment = dict(service.get("environment") or {})
    environment[RECONCILER_PROJECTS] = json.dumps(desired, sort_keys=True, separators=(",", ":"))
    environment[RECONCILER_DECLARED] = json.dumps(sorted(set(declared)), separators=(",", ":"))
    return {**service, "environment": environment}


VULNS_AGENT = "vulns-agent"
VULNS_IMAGES = "AYE_VULNS_IMAGES"


def image_name(reference: str) -> str:
    name = reference.split("@", 1)[0]
    head, _, last = name.rpartition("/")
    return f"{head}/{last.split(':', 1)[0]}" if head else last.split(":", 1)[0]


def running_images(listed: Any) -> set[str]:
    if not isinstance(listed, dict):
        return set()
    return {
        str(spec["image"])
        for spec in listed.values()
        if isinstance(spec, dict) and spec.get("image") and kept_running(spec)
    }


def scanned_images(
    manifest: Manifest, host: str, services: dict[str, Any], settings: dict[str, Any]
) -> list[str]:
    excluded = {str(entry) for entry in settings.get("exclude") or []}
    found = running_images(services)
    for compose, _ in compose_entries(manifest.hosts[host]):
        path = manifest.base_dir / compose
        data = yaml.safe_load(path.read_text(encoding="utf-8")) if path.is_file() else None
        found |= running_images(data.get("services") if isinstance(data, dict) else None)
    return sorted(
        image
        for image in found
        if "$" not in image
        and image not in excluded
        and image.split("@", 1)[0] not in excluded
        and image_name(image) not in excluded
    )


def with_images(service: dict[str, Any], images: list[str]) -> dict[str, Any]:
    environment = dict(service.get("environment") or {})
    environment[VULNS_IMAGES] = json.dumps(images, separators=(",", ":"))
    return {**service, "environment": environment}


EOL_AGENT = "eol-agent"


def with_eol(
    manifest: Manifest, host: str, services: dict[str, Any], settings: dict[str, Any]
) -> dict[str, Any]:
    images = scanned_images(manifest, host, services, settings)
    components, unmapped = eol_tracked(images, settings.get("components") or [])
    environment = dict(services[EOL_AGENT].get("environment") or {})
    environment["AYE_EOL_COMPONENTS"] = json.dumps(
        [component.as_json() for component in components], separators=(",", ":")
    )
    environment["AYE_EOL_UNMAPPED"] = json.dumps(unmapped, separators=(",", ":"))
    return {**services[EOL_AGENT], "environment": environment}


def host_compose(
    manifest: Manifest,
    host: str,
    modules: dict[str, Module],
    prepared: HostRender | None = None,
) -> dict[str, Any]:
    prepared = prepared or host_render(manifest, host, modules)
    all_settings = prepared.settings
    services: dict[str, Any] = {}
    volumes: dict[str, Any] = {}
    networks: dict[str, Any] = {}
    reserved = manifest.reserved_on(host)
    shared = compose_safe(prepared.context)
    for name, settings in all_settings.items():
        module = modules[name]
        context = {**shared, "settings": compose_safe(settings)}
        fragment = render_template(module, "compose.yaml.j2", context) or {}
        for service_name, service in (fragment.get("services") or {}).items():
            if service_name in reserved:
                continue
            services[service_name] = finalize_service(
                manifest, host, module, settings, service_name, service
            )
        volumes.update(fragment.get("volumes") or {})
        networks.update(fragment.get("networks") or {})
    project = f"{manifest.project}-{host}"
    if RECONCILER in services:
        desired = desired_projects(manifest, host, project, services, all_settings[RECONCILER])
        declared = declared_projects(manifest, host)
        services[RECONCILER] = with_desired(services[RECONCILER], desired, declared)
    if VULNS_AGENT in services:
        images = scanned_images(manifest, host, services, all_settings["vulns"])
        services[VULNS_AGENT] = with_images(services[VULNS_AGENT], images)
    if EOL_AGENT in services:
        services[EOL_AGENT] = with_eol(manifest, host, services, all_settings["eol"])
    compose: dict[str, Any] = {"name": project, "services": services}
    if volumes:
        compose["volumes"] = volumes
    if networks:
        compose["networks"] = networks
    return compose


def service_owners(
    manifest: Manifest,
    host: str,
    modules: dict[str, Module],
    renders: FleetRender | None = None,
) -> dict[str, str]:
    renders = renders or FleetRender(manifest, modules)
    owners: dict[str, str] = {}
    for service in renders.compose(host)["services"].values():
        labels = service["labels"]
        owners[labels["io.ayeops.service"]] = labels["io.ayeops.module"]
    return owners


def host_scrape(
    manifest: Manifest,
    host: str,
    modules: dict[str, Module],
    prepared: HostRender | None = None,
) -> dict[str, Any] | None:
    all_settings = prepared.settings if prepared else host_settings(manifest, host, modules)
    if "metrics" not in all_settings:
        return None
    prepared = prepared or host_render(manifest, host, modules)
    jobs: list[dict[str, Any]] = []
    for name, settings in all_settings.items():
        context = module_context(manifest, host, settings, all_settings, shared=prepared.context)
        jobs.extend(render_template(modules[name], "scrape.yaml.j2", context) or [])
    jobs = without_targets(jobs, manifest.reserved_on(host))
    return {
        "global": {"scrape_interval": all_settings["metrics"]["scrape_interval"]},
        "scrape_configs": jobs,
    }


class FleetRender:
    def __init__(self, manifest: Manifest, modules: dict[str, Module]) -> None:
        self.manifest = manifest
        self.modules = modules
        self._settings: dict[str, dict[str, Any]] = {}
        self._hosts: dict[str, HostRender] = {}
        self._compose: dict[str, dict[str, Any]] = {}
        self._scrape: dict[str, dict[str, Any] | None] = {}

    def settings(self, host: str) -> dict[str, Any]:
        if host not in self._settings:
            self._settings[host] = host_settings(self.manifest, host, self.modules)
        return self._settings[host]

    def host(self, host: str) -> HostRender:
        if host not in self._hosts:
            self._hosts[host] = host_render(self.manifest, host, self.modules, self.settings(host))
        return self._hosts[host]

    def compose(self, host: str) -> dict[str, Any]:
        if host not in self._compose:
            self._compose[host] = host_compose(self.manifest, host, self.modules, self.host(host))
        return self._compose[host]

    def scrape(self, host: str) -> dict[str, Any] | None:
        if host not in self._scrape:
            scraped = "metrics" in self.settings(host)
            self._scrape[host] = (
                host_scrape(self.manifest, host, self.modules, self.host(host)) if scraped else None
            )
        return self._scrape[host]


def without_targets(jobs: list[dict[str, Any]], services: set[str]) -> list[dict[str, Any]]:
    if not services:
        return jobs
    kept = []
    for job in jobs:
        groups = []
        for group in job.get("static_configs") or []:
            targets = [
                target
                for target in group.get("targets") or []
                if str(target).rsplit(":", 1)[0] not in services
            ]
            if targets:
                groups.append({**group, "targets": targets})
        if groups or not job.get("static_configs"):
            kept.append({**job, "static_configs": groups} if groups else job)
    return kept


def escape_provisioning(text: str) -> str:
    return text.replace("$", "$$")


NO_DATA_STATES = {"alerting": "Alerting", "ok": "OK"}
SCOPE = (
    "{{ if $labels.group }}{{ $labels.group }}{{ else if $labels.host }}{{ $labels.host }}{{ end }}"
)
GROUP_BY = ["grafana_folder", "alertname", "scope"]
SERVICE_SILENCE = ("Alerting", "Alerting")


def module_silence(alert: dict[str, Any]) -> tuple[str, str]:
    return NO_DATA_STATES[alert.get("no_data", "ok")], "Error"


def grafana_rule(
    alert: dict[str, Any],
    base: str,
    labels: dict[str, str],
    silence: tuple[str, str],
    links: dict[str, str] | None = None,
) -> dict[str, Any]:
    title = alert.get("title", alert["name"])
    return {
        "uid": rule_uid(alert["name"]),
        "title": escape_provisioning(title),
        "condition": "C",
        "for": alert.get("for", "5m"),
        "noDataState": silence[0],
        "execErrState": silence[1],
        "labels": {"severity": alert["severity"], "scope": escape_provisioning(SCOPE), **labels},
        "annotations": {
            "summary": escape_provisioning(alert.get("summary", title)),
            "runbook_url": escape_provisioning(runbook_url(base, alert["runbook"])),
            **(links or {}),
        },
        "data": [
            {
                "refId": "A",
                "relativeTimeRange": {"from": 600, "to": 0},
                "datasourceUid": DATASOURCE_UID,
                "model": {
                    "refId": "A",
                    "expr": escape_provisioning(alert["expr"]),
                    "instant": True,
                },
            },
            {
                "refId": "C",
                "relativeTimeRange": {"from": 0, "to": 0},
                "datasourceUid": "__expr__",
                "model": {
                    "refId": "C",
                    "type": "threshold",
                    "expression": "A",
                    "conditions": [{"evaluator": {"type": "gt", "params": [0]}}],
                },
            },
        ],
    }


def module_runbook(
    manifest: Manifest, module: Module, alert: dict[str, Any]
) -> tuple[dict[str, Any], str]:
    if module.local:
        return alert, str(module.spec.get("runbook_base_url") or manifest.runbook_base_url)
    return {**alert, "runbook": f"{alert['runbook']}.md"}, MODULE_RUNBOOK_BASE


HOST_DOWN_MODULE = "node"
NODE_UP = downtime.NODE_UP
HOSTS_DOWN = downtime.HOSTS_DOWN


def held_back(alert: dict[str, Any], hold: bool) -> dict[str, Any]:
    return downtime.guarded(alert, downtime.fleet_guards(hold))


def held_module_alert(
    manifest: Manifest, module: Module, alert: dict[str, Any], hold: bool, down: Downtime
) -> tuple[dict[str, Any], str]:
    found, base = module_runbook(manifest, module, alert)
    return downtime.module_alert(found, hold, down), base


def reader_rendered(manifest: Manifest, modules: dict[str, Module]) -> bool:
    return any(
        "grafana" in names and grafana_reader.consumed(names, modules)
        for names in (manifest.host_modules(host) for host in manifest.hosts)
    )


def alert_applies(
    manifest: Manifest, modules: dict[str, Module], module: Module, alert: dict[str, Any]
) -> bool:
    if alert.get("only_with") == "grafana_reader" and not reader_rendered(manifest, modules):
        return False
    if alert.get("only_with") == "volatile_hosts" and not downtime.volatile_hosts(manifest):
        return False
    if alert.get("only_with") == "links" and not links_linked(manifest):
        return False
    wanted = alert.get("provider")
    setting = alert.get("setting")
    if wanted is None and setting is None:
        return True
    return any(
        (wanted is None or merged.get("provider") == wanted)
        and (setting is None or bool(merged.get(setting)))
        for host in sorted(manifest.hosts)
        for name, settings in manifest.host_modules(host).items()
        if name == module.name
        for merged in [module.settings(settings)]
    )


def module_rules(alert: dict[str, Any]) -> list[dict[str, Any]]:
    if is_baseline(alert):
        return expand(alert, alert["baseline"], alert["name"], alert["name"])
    return [alert]


def applied_alerts(
    manifest: Manifest, modules: dict[str, Module], module: Module
) -> list[dict[str, Any]]:
    return [a for a in module.alerts if alert_applies(manifest, modules, module, a)]


def all_baseline_sources(
    manifest: Manifest, modules: dict[str, Module]
) -> list[tuple[str, dict[str, Any]]]:
    sources = [
        (str(alert["name"]), dict(alert["baseline"]))
        for name in sorted(manifest.enabled_modules() & set(modules))
        for alert in applied_alerts(manifest, modules, modules[name])
        if is_baseline(alert)
    ]
    return sources + baseline_sources(manifest)


def grafana_rules(
    manifest: Manifest, modules: dict[str, Module], boards: tuple[Board, ...] = ()
) -> dict[str, Any]:
    enabled = manifest.enabled_modules()
    base = manifest.runbook_base_url
    hold = HOST_DOWN_MODULE in enabled & set(modules)
    down = downtime.downtime_of(manifest)
    groups: list[dict[str, Any]] = []
    for name in sorted(enabled & set(modules)):
        rules = [
            grafana_rule(
                *held_module_alert(manifest, modules[name], a, hold, down),
                {"module": name},
                module_silence(a),
                link_annotations(pick_link(a, name, boards)),
            )
            for alert in applied_alerts(manifest, modules, modules[name])
            for a in module_rules(alert)
        ]
        if rules:
            groups.append(
                {"orgId": 1, "name": name, "folder": FOLDER, "interval": "1m", "rules": rules}
            )
    service_rules = [
        grafana_rule(
            downtime.service_alert(alert, str(service.get("host") or ""), hold, down),
            base,
            {"service": service_name},
            SERVICE_SILENCE,
            link_annotations(pick_link(alert, "", boards)),
        )
        for service_name, service in sorted(manifest.active_services.items())
        for alert in service_alerts(manifest, service_name, service)
    ]
    if service_rules:
        groups.append(
            {
                "orgId": 1,
                "name": "services",
                "folder": FOLDER,
                "interval": "1m",
                "rules": service_rules,
            }
        )
    duty = overdue_alert(manifest)
    if duty is not None:
        rule = grafana_rule(
            downtime.hostless_alert(duty),
            MODULE_RUNBOOK_BASE,
            {"scope": escape_provisioning(DUTY_SCOPE)},
            ("OK", "Error"),
        )
        rule["annotations"]["runbook_url"] = escape_provisioning(runbook_annotation(manifest))
        groups.append(
            {"orgId": 1, "name": "duties", "folder": FOLDER, "interval": "1m", "rules": [rule]}
        )
    return {"apiVersion": 1, "groups": groups}


def contact_receiver(manifest: Manifest) -> dict[str, Any] | None:
    if manifest.contact == "telegram":
        return {
            "uid": "aye-telegram",
            "type": "telegram",
            "settings": {
                "bottoken": "${AYE_TELEGRAM_BOT_TOKEN}",
                "chatid": "${AYE_TELEGRAM_CHAT_ID}",
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
                "message": '{{ template "aye.telegram" . }}',
            },
        }
    if manifest.contact == "email":
        return {
            "uid": "aye-email",
            "type": "email",
            "settings": {"addresses": ";".join(manifest.alert_emails), "singleEmail": True},
        }
    if manifest.contact == "slack":
        return {
            "uid": "aye-slack",
            "type": "slack",
            "settings": {
                "url": "${" + SLACK_WEBHOOK_ENV + "}",
                "title": '{{ template "aye.slack.title" . }}',
                "text": '{{ template "aye.slack" . }}',
            },
        }
    return None


ONCALL = "oncall"
ONCALL_SEVERITIES = "critical|page"
ONCALL_RECEIVER = {
    "uid": "aye-oncall",
    "type": "webhook",
    "settings": {
        "url": "http://oncall-bridge:8080/grafana",
        "httpMethod": "POST",
        "authorization_scheme": "Bearer",
        "authorization_credentials": "${AYE_ONCALL_WEBHOOK_SECRET}",
    },
}


def contact_points(
    manifest: Manifest, lang: str = "en", oncall: bool = False, autopilot: bool = False
) -> dict[str, Any] | None:
    receiver = contact_receiver(manifest)
    if receiver is None:
        return None
    points: dict[str, Any] = {
        "apiVersion": 1,
        "contactPoints": [{"orgId": 1, "name": manifest.contact, "receivers": [receiver]}],
    }
    template = MESSAGE_TEMPLATES.get(manifest.contact)
    if template is not None:
        points["templates"] = [{"orgId": 1, "name": "aye", "template": template(lang)}]
    policy: dict[str, Any] = {
        "orgId": 1,
        "receiver": manifest.contact,
        "group_by": GROUP_BY,
    }
    routes: list[dict[str, Any]] = []
    if autopilot:
        points["contactPoints"].append(
            {"orgId": 1, "name": AUTOPILOT, "receivers": [AUTOPILOT_RECEIVER]}
        )
        routes.append(copy.deepcopy(AUTOPILOT_ROUTE))
    if oncall:
        points["contactPoints"].append({"orgId": 1, "name": ONCALL, "receivers": [ONCALL_RECEIVER]})
        routes.append(
            {
                "receiver": ONCALL,
                "object_matchers": [["severity", "=~", ONCALL_SEVERITIES]],
                "continue": True,
            }
        )
    if routes:
        policy["routes"] = [*routes, {"receiver": manifest.contact}]
    points["policies"] = [policy]
    return points


def env_value(name: str) -> str:
    return EnvRef("${" + name + "}")


def project_datasource(source: dict[str, Any]) -> dict[str, Any]:
    kind = source["type"]
    result: dict[str, Any] = {
        "name": escape_provisioning(source["name"]),
        "uid": source["uid"],
        "type": DATASOURCE_TYPES[kind],
        "access": "proxy",
        "url": escape_provisioning(source["url"]),
        "isDefault": False,
        "editable": False,
    }
    user = source.get("user")
    secret = source.get("password_env")
    if kind == "postgres":
        result["jsonData"] = {
            "database": escape_provisioning(source["database"]),
            "sslmode": "require" if source.get("tls") else "disable",
            "maxOpenConns": source.get("max_connections", POSTGRES_MAX_CONNECTIONS),
            "maxIdleConns": 0,
            "maxIdleConnsAuto": False,
            "connMaxLifetime": POSTGRES_CONN_LIFETIME,
        }
        if user:
            result["user"] = escape_provisioning(user)
        if secret:
            result["secureJsonData"] = {"password": env_value(secret)}
        return result
    if user:
        result["basicAuth"] = True
        result["basicAuthUser"] = escape_provisioning(user)
    if secret:
        result["secureJsonData"] = {"basicAuthPassword": env_value(secret)}
    return result


def project_dashboard_files(manifest: Manifest, settings: dict[str, Any]) -> list[Path]:
    directory = settings.get("dashboards")
    if not directory:
        return []
    path = manifest.base_dir / str(directory)
    return sorted(path.glob("*.json")) if path.is_dir() else []


def dashboard_providers(manifest: Manifest, settings: dict[str, Any]) -> list[dict[str, Any]]:
    providers: list[dict[str, Any]] = [
        {
            "name": "ayeops",
            "folder": FOLDER,
            "type": "file",
            "allowUiUpdates": False,
            "options": {"path": "/etc/grafana/dashboards"},
        }
    ]
    if settings.get("dashboards"):
        providers.append(
            {
                "name": "project",
                "folder": manifest.project,
                "type": "file",
                "allowUiUpdates": False,
                "options": {"path": PROJECT_DASHBOARDS},
            }
        )
    return providers


def grafana_locale(manifest: Manifest, settings: dict[str, Any]) -> str:
    locale = settings.get("locale")
    return str(locale) if locale in LANGS else manifest.portal_locale


def built_dashboards(
    manifest: Manifest, modules: dict[str, Module], settings: dict[str, Any]
) -> list[tuple[str, str, dict[str, Any]]]:
    hosts = [host_settings(manifest, host, modules) for host in sorted(manifest.hosts)]
    lang = grafana_locale(manifest, settings)
    built = []
    for name in sorted(manifest.enabled_modules() & set(modules)):
        for dashboard in modules[name].dashboards:
            data = json.loads(dashboard.read_text(encoding="utf-8"))
            board = build(data, lang, lambda requirement: requirement_met(requirement, hosts))
            if board is not None:
                built.append((name, dashboard.name, board))
    return built


def baseline_board(
    manifest: Manifest, modules: dict[str, Module], settings: dict[str, Any]
) -> dict[str, Any] | None:
    sources = all_baseline_sources(manifest, modules)
    if not sources:
        return None
    return dict(localize(baseline_dashboard(sources), grafana_locale(manifest, settings)))


def module_dashboards(
    manifest: Manifest, modules: dict[str, Module], settings: dict[str, Any]
) -> dict[str, str]:
    files = {
        f"grafana/dashboards/{file}": dump_json(board)
        for _, file, board in built_dashboards(manifest, modules, settings)
    }
    board = baseline_board(manifest, modules, settings)
    if board is not None:
        files[f"grafana/dashboards/{BASELINE_UID}.json"] = dump_json(board)
    return files


def grafana_boards(
    manifest: Manifest, modules: dict[str, Module], settings: dict[str, Any]
) -> tuple[Board, ...]:
    boards = [
        board_from(name, data) for name, _, data in built_dashboards(manifest, modules, settings)
    ]
    baseline = baseline_board(manifest, modules, settings)
    if baseline is not None:
        boards.append(board_from("", baseline))
    for dashboard in project_dashboard_files(manifest, settings):
        data = json.loads(dashboard.read_text(encoding="utf-8"))
        if data.get("uid"):
            boards.append(board_from("", data))
    return tuple(boards)


def grafana_host(manifest: Manifest, modules: dict[str, Module]) -> dict[str, Any] | None:
    for host in sorted(manifest.hosts):
        settings = host_settings(manifest, host, modules)
        if "grafana" in settings:
            return settings
    return None


def grafana_root_url(settings: dict[str, Any]) -> str | None:
    grafana = settings.get("grafana") or {}
    if grafana.get("root_url"):
        return str(grafana["root_url"])
    if not settings.get("auth"):
        return None
    ingress = settings.get("ingress") or {}
    for name, target in sorted((ingress.get("routes") or {}).items()):
        if str(target).split(":")[0] == "grafana":
            return f"https://{name}.{ingress['domain']}"
    return None


def grafana_tree(
    manifest: Manifest,
    modules: dict[str, Module],
    settings: dict[str, Any],
    oncall: bool = False,
    autopilot: bool = False,
) -> dict[str, str]:
    tree = {
        "grafana/provisioning/datasources/victoriametrics.yaml": dump_yaml(
            {
                "apiVersion": 1,
                "datasources": [
                    {
                        "name": "VictoriaMetrics",
                        "uid": DATASOURCE_UID,
                        "type": "prometheus",
                        "access": "proxy",
                        "url": f"http://victoriametrics:{METRICS_PORT}",
                        "isDefault": True,
                        "editable": False,
                    }
                ],
            }
        ),
        "grafana/provisioning/dashboards/ayeops.yaml": dump_yaml(
            {"apiVersion": 1, "providers": dashboard_providers(manifest, settings)}
        ),
    }
    rules = grafana_rules(manifest, modules, grafana_boards(manifest, modules, settings))
    tree["grafana/provisioning/alerting/rules.yaml"] = dump_yaml(rules)
    if autopilot:
        tree[AUTOPILOT_CATALOG] = autopilot_catalog(manifest, modules, rules)
    contact = contact_points(manifest, grafana_locale(manifest, settings), oncall, autopilot)
    if contact:
        tree["grafana/provisioning/alerting/contact-points.yaml"] = dump_yaml(contact)
    tree.update(module_dashboards(manifest, modules, settings))
    sources = settings.get("datasources") or []
    if sources:
        tree["grafana/provisioning/datasources/project.yaml"] = dump_yaml(
            {"apiVersion": 1, "datasources": [project_datasource(s) for s in sources]}
        )
    for dashboard in project_dashboard_files(manifest, settings):
        data = json.loads(dashboard.read_text(encoding="utf-8"))
        tree[f"grafana/project-dashboards/{dashboard.name}"] = dump_json(data)
    return tree


def env_example(texts: list[str]) -> str:
    names = sorted({match for text in texts for match in ENV_REFERENCE.findall(text)})
    return "".join(f"{name}={PLACEHOLDER}\n" for name in names)


def render_file(source: str, dest: str, context: dict[str, Any]) -> str:
    text = compiled(source).render(**context)
    if dest.endswith((".yml", ".yaml")):
        return dump_yaml(load_yaml(text))
    return text


def gate(when: str, settings: dict[str, Any], context: dict[str, Any]) -> bool:
    if when in settings:
        return bool(settings[when])
    value: Any = context
    for part in when.split("."):
        value = value.get(part) if isinstance(value, dict) else None
    return bool(value)


def host_units(manifest: Manifest, host: str) -> dict[str, str]:
    return {
        str(name): str(unit) for name, unit in (manifest.hosts[host].get("units") or {}).items()
    }


ENVIRONMENT_WORDS = {
    "prod",
    "production",
    "dev",
    "development",
    "stage",
    "staging",
    "test",
    "local",
}
COMPOSE_WORDS = re.compile(r"(^|[._-])(docker-compose|compose)($|[._-])")


def compose_entries(spec: dict[str, Any]) -> list[tuple[str, str]]:
    entries = []
    for entry in spec.get("compose") or []:
        if isinstance(entry, dict):
            entries.append((str(entry["file"]), str(entry.get("name") or "")))
        else:
            entries.append((str(entry), ""))
    return entries


def stack_name(manifest: Manifest, compose: str, declared: str, data: dict[str, Any]) -> str:
    if declared:
        return declared
    if data.get("name"):
        return str(data["name"])
    stem = Path(compose).name
    for suffix in (".yaml", ".yml"):
        stem = stem.removesuffix(suffix)
    rest = COMPOSE_WORDS.sub(".", stem).strip("._-")
    if rest.lower() in ENVIRONMENT_WORDS:
        return f"{manifest.project} {rest}"
    return rest or manifest.project


def compose_file_services(manifest: Manifest, host: str) -> dict[str, dict[str, Any]]:
    services: dict[str, dict[str, Any]] = {}
    for compose, declared in compose_entries(manifest.hosts[host]):
        path = manifest.base_dir / compose
        if path.is_file():
            data = load_yaml(path.read_text(encoding="utf-8")) or {}
            stack = stack_name(manifest, compose, declared, data)
            for name, service in (data.get("services") or {}).items():
                services[name] = {
                    **(service or {}),
                    "x-aye-compose": compose,
                    "x-aye-stack": stack,
                    "x-aye-project": declared,
                }
    return services


def host_tree(
    manifest: Manifest,
    host: str,
    modules: dict[str, Module],
    stamp: Stamp | None = None,
    renders: FleetRender | None = None,
) -> dict[str, str]:
    renders = renders or FleetRender(manifest, modules)
    tree: dict[str, str] = {}
    live: set[str] = set()
    prepared = renders.host(host)
    scrape = renders.scrape(host)
    if scrape is not None:
        tree["vmagent/scrape.yaml"] = dump_yaml(scrape)
    all_settings = prepared.settings
    for name, settings in all_settings.items():
        module = modules[name]
        context = module_context(manifest, host, settings, all_settings, shared=prepared.context)
        for entry in module.files:
            if "when" not in entry or gate(entry["when"], settings, context):
                source = module.template(entry["src"]).replace("\r\n", "\n")
                if entry.get("template"):
                    source = render_file(source, entry["dest"], context)
                tree[entry["dest"]] = source
                if entry.get("live"):
                    live.add(entry["dest"])
    if "grafana" in all_settings:
        tree.update(
            grafana_tree(
                manifest,
                modules,
                all_settings["grafana"],
                ONCALL in all_settings,
                AUTOPILOT in all_settings,
            )
        )
        tree.update(loki_datasource(manifest, host))
    tree.update(mcp_files(manifest, host, modules, renders))
    tree.update(backup_units(host, all_settings))
    if "portal" in all_settings:
        from ayeops.portal.pages import portal_pages

        pages = portal_pages(manifest, modules, stamp or Stamp(), renders=renders)
        for name, page in pages.items():
            tree[f"portal/{name}"] = page
            live.add(f"portal/{name}")
    compose = stamp_config_hashes(renders.compose(host), tree, live)
    tree["compose.yaml"] = dump_yaml(compose)
    env = env_example([tree["compose.yaml"]])
    if env:
        tree[".env.example"] = env
    return tree


def render_tree(
    manifest: Manifest, modules: dict[str, Module], stamp: Stamp | None = None
) -> dict[str, str]:
    modules = with_local_modules(manifest, modules)
    renders = FleetRender(manifest, modules)
    tree: dict[str, str] = {}
    for host in sorted(manifest.hosts):
        for path, content in host_tree(manifest, host, modules, stamp, renders).items():
            tree[f"{host}/{path}"] = content
    return dict(sorted(tree.items()))


def write_tree(tree: dict[str, str], out_dir: Path) -> list[Path]:
    written: list[Path] = []
    for relative, content in tree.items():
        target = out_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
        written.append(target)
    return written


def known_alerts(modules: dict[str, Module]) -> dict[str, dict[str, Any]]:
    return {name: alert for name, (_, alert) in all_alerts(modules).items()}
