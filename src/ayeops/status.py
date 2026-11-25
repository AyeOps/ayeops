from __future__ import annotations

import hashlib
import hmac
import json
import re
from typing import Any

from ayeops.i18n import pick
from ayeops.manifest import Manifest

STATES = ("operational", "degraded", "outage", "maintenance", "no_data")
DEFAULT_LOCALE = "en"
COMPONENT_ID = re.compile(r"^[a-z][a-z0-9-]{0,39}$")
SIGNATURE_HEADER = "X-AyeOps-Signature"
NOTIFY_MIN_STATES = ("outage", "degraded", "no_data")
DEFAULT_NOTIFY_MIN_STATE = "outage"
REPORTER = "status"
PUSH_PATH = "/api/push"

Problem = tuple[str, str, str]


def slug(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:40].rstrip("-")


def public_marks(manifest: Manifest) -> list[tuple[str, dict[str, Any]]]:
    marks = []
    for name, service in manifest.active_services.items():
        status = service.get("status") or {}
        if title_of(manifest, status.get("public")):
            marks.append((name, status))
    return marks


def title_of(manifest: Manifest, value: Any) -> str:
    locale = str((manifest.data.get("status_page") or {}).get("locale") or DEFAULT_LOCALE)
    return pick(value, locale).strip()


def component_id(mark: dict[str, Any]) -> str:
    return str(mark.get("id") or slug(pick(mark["public"], DEFAULT_LOCALE)))


def status_problems(manifest: Manifest) -> list[Problem]:
    problems: list[Problem] = []
    titles: dict[str, tuple[str, str]] = {}
    for name, mark in public_marks(manifest):
        path = f"services.{name}.status"
        title = title_of(manifest, mark["public"])
        ident = component_id(mark)
        if not COMPONENT_ID.match(ident):
            problems.append(
                (path, "status-component", f"{title!r} gives no usable component id, set id: <id>")
            )
            continue
        seen = titles.setdefault(ident, (name, title))
        if seen[1] != title:
            problems.append(
                (
                    path,
                    "status-component",
                    f"component {ident!r} is titled {seen[1]!r} by {seen[0]} and {title!r} here",
                )
            )
    return problems


def notify_of(page: dict[str, Any]) -> dict[str, str] | None:
    notify = page.get("notify")
    if not notify:
        return None
    return {
        "telegram_chat_id": str(notify["telegram_chat_id"]),
        "min_state": str(notify.get("min_state") or DEFAULT_NOTIFY_MIN_STATE),
    }


def access_of(page: dict[str, Any]) -> dict[str, str] | None:
    access = page.get("access")
    if not access:
        return None
    return {"team": str(access["team"]), "aud": str(access["aud"])}


def status_document(manifest: Manifest) -> dict[str, Any]:
    page = manifest.data.get("status_page") or {}
    components: dict[str, dict[str, Any]] = {}
    for _, mark in public_marks(manifest):
        ident = component_id(mark)
        component = components.setdefault(
            ident,
            {"id": ident, "title": title_of(manifest, mark["public"]), "probes": [], "push": False},
        )
        probe = mark.get("probe")
        if probe:
            if probe not in component["probes"]:
                component["probes"].append(probe)
        else:
            component["push"] = True
    for component in components.values():
        component["probes"].sort()
    return {
        "tenant": manifest.project,
        "hostname": str(page.get("hostname", "")),
        "title": str(page.get("title") or manifest.project),
        "locale": str(page.get("locale") or DEFAULT_LOCALE),
        "notify": notify_of(page),
        "access": access_of(page),
        "components": list(components.values()),
    }


def sql_text(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def seed_sql(document: dict[str, Any]) -> str:
    tenant = sql_text(document["tenant"])
    components = document["components"]
    notify = document.get("notify")
    chat = sql_text(notify["telegram_chat_id"]) if notify else "NULL"
    least = sql_text(notify["min_state"] if notify else DEFAULT_NOTIFY_MIN_STATE)
    access = document.get("access")
    team = sql_text(access["team"]) if access else "NULL"
    aud = sql_text(access["aud"]) if access else "NULL"
    lines = [
        "INSERT INTO tenants (id, hostname, title, locale, notify_chat, notify_min, access_team, "
        f"access_aud) VALUES ({tenant}, {sql_text(document['hostname'])}, "
        f"{sql_text(document['title'])}, {sql_text(document['locale'])}, {chat}, {least}, "
        f"{team}, {aud}) ON CONFLICT (id) DO UPDATE SET "
        "hostname = excluded.hostname, title = excluded.title, locale = excluded.locale, "
        "notify_chat = excluded.notify_chat, notify_min = excluded.notify_min, "
        "access_team = excluded.access_team, access_aud = excluded.access_aud;",
        f"DELETE FROM probes WHERE tenant_id = {tenant};",
    ]
    kept = ", ".join(sql_text(component["id"]) for component in components)
    lines.append(
        f"DELETE FROM components WHERE tenant_id = {tenant}"
        + (f" AND id NOT IN ({kept});" if kept else ";")
    )
    lines.append(
        f"DELETE FROM notices WHERE tenant_id = {tenant}"
        + (f" AND component_id NOT IN ({kept});" if kept and notify else ";")
    )
    for position, component in enumerate(components):
        lines.append(
            "INSERT INTO components (tenant_id, id, title, position, push) VALUES "
            f"({tenant}, {sql_text(component['id'])}, {sql_text(component['title'])}, "
            f"{position}, {int(component['push'])}) ON CONFLICT (tenant_id, id) DO UPDATE SET "
            "title = excluded.title, position = excluded.position, push = excluded.push;"
        )
        for url in component["probes"]:
            lines.append(
                "INSERT INTO probes (tenant_id, component_id, url) VALUES "
                f"({tenant}, {sql_text(component['id'])}, {sql_text(url)});"
            )
    return "\n".join(lines) + "\n"


def document_json(document: dict[str, Any]) -> str:
    return json.dumps(document, ensure_ascii=False, indent=2) + "\n"


def sign(body: bytes, secret: str) -> str:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def push_components(manifest: Manifest) -> list[dict[str, Any]]:
    services: dict[str, list[str]] = {}
    for name, mark in public_marks(manifest):
        services.setdefault(component_id(mark), []).append(name)
    pushed = {c["id"] for c in status_document(manifest)["components"] if c["push"]}
    return [
        {"id": ident, "services": names} for ident, names in services.items() if ident in pushed
    ]


def push_url(manifest: Manifest) -> str:
    hostname = str((manifest.data.get("status_page") or {}).get("hostname") or "")
    return f"https://{hostname}{PUSH_PATH}" if hostname else ""


def reporter_context(manifest: Manifest, settings: dict[str, Any] | None) -> dict[str, Any]:
    if settings is None:
        return {}
    return {
        "tenant": manifest.project,
        "url": push_url(manifest),
        "components": push_components(manifest),
    }


def reporter_problems(manifest: Manifest) -> list[Problem]:
    problems: list[Problem] = []
    for host in sorted(manifest.hosts):
        if REPORTER not in manifest.host_modules(host):
            continue
        path = f"hosts.{host}.modules.{REPORTER}"
        if not push_url(manifest):
            problems.append(
                (path, "status-reporter", "the status reporter needs status_page.hostname")
            )
        elif not push_components(manifest):
            problems.append(
                (
                    path,
                    "status-reporter",
                    "no public component waits for pushes: each has a probe, so the reporter"
                    " would have nothing to send",
                )
            )
    return problems
