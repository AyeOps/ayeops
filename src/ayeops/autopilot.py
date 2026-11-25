from __future__ import annotations

import json
import re
from collections.abc import Iterator
from typing import Any

from ayeops.logs import loki_url
from ayeops.manifest import Manifest
from ayeops.registry import Module, load_modules

NAME = "autopilot"
SEVERITIES = "critical|page"
CATALOG_FILE = "autopilot/catalog.json"
INCIDENTS_JOURNAL = "/incidents/incidents.jsonl"
DESCRIPTION_CHARS = 300
ENV_REFERENCE = re.compile(r"\$\{([A-Z][A-Z0-9_]*[A-Z0-9])")
ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")
OWN_ENV = re.compile(r"^AYE_AUTOPILOT_|^AYE_ONCALL_TELEGRAM_TOKEN$")
RECEIVER = {
    "uid": "aye-autopilot",
    "type": "webhook",
    "settings": {
        "url": "http://autopilot:8080/grafana",
        "httpMethod": "POST",
        "authorization_scheme": "Bearer",
        "authorization_credentials": "${AYE_AUTOPILOT_WEBHOOK_SECRET}",
    },
}
ROUTE = {
    "receiver": NAME,
    "object_matchers": [["severity", "=~", SEVERITIES]],
    "continue": True,
    "group_wait": "0s",
}

Problem = tuple[str, str, str]


def autopilot_context(
    manifest: Manifest, host: str, all_settings: dict[str, Any]
) -> dict[str, str]:
    if NAME not in all_settings:
        return {}
    person = (all_settings.get("oncall") or {}).get("person") or {}
    return {
        "loki_url": loki_url(manifest, host) or "",
        "chat_id": str(person.get("telegram_chat_id") or ""),
        "incidents": INCIDENTS_JOURNAL if "incidents" in all_settings else "",
    }


def literal(text: str) -> str:
    return str(text).replace("$$", "$")


HELD_MARKER = ") unless on (host) ("


def unheld(expr: str) -> str:
    if not expr.startswith("(") or HELD_MARKER not in expr:
        return expr
    inner = expr[1 : expr.index(HELD_MARKER)]
    return inner if expr.endswith(f" or 0 * ({inner})") else expr


def rule_expr(rule: dict[str, Any]) -> str:
    for query in rule.get("data") or []:
        expr = (query.get("model") or {}).get("expr")
        if expr:
            return unheld(str(expr))
    return ""


def trigger_rules(rules: dict[str, Any]) -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    for group in rules.get("groups") or []:
        for rule in group.get("rules") or []:
            labels = rule.get("labels") or {}
            if str(labels.get("severity")) not in SEVERITIES.split("|"):
                continue
            found[str(rule["uid"])] = {
                "title": literal(rule["title"]),
                "expr": literal(rule_expr(rule)),
                "severity": labels["severity"],
                "module": labels.get("module"),
                "service": labels.get("service"),
                "runbook_url": literal((rule.get("annotations") or {}).get("runbook_url") or ""),
            }
    return found


def service_entries(manifest: Manifest, modules: dict[str, Module]) -> dict[str, dict[str, Any]]:
    from ayeops.portal.live import live_spec

    services = {
        name: entry
        for name, entry in manifest.active_services.items()
        if entry.get("host") in manifest.hosts
    }
    keys = [(str(entry["host"]), name) for name, entry in services.items()]
    live = live_spec(manifest, modules, keys, 0)["services"]
    found: dict[str, dict[str, Any]] = {}
    for name, entry in sorted(services.items()):
        host = str(entry["host"])
        description = str(entry.get("description") or "")
        found[name] = {
            "host": host,
            "container": (live.get(f"{host}/{name}") or {}).get("container"),
            "depends_on": [str(d) for d in entry.get("depends_on") or []],
            "description": description[:DESCRIPTION_CHARS],
        }
    return found


def env_settings(value: Any, key: str = "") -> Iterator[str]:
    if isinstance(value, dict):
        for name, inner in value.items():
            yield from env_settings(inner, str(name))
    elif isinstance(value, list):
        for item in value:
            yield from env_settings(item, key)
    elif isinstance(value, str) and key.endswith("_env") and ENV_NAME.match(value):
        yield value


def secret_names(manifest: Manifest, modules: dict[str, Module]) -> list[str]:
    names: set[str] = set()
    used = {name for host in manifest.hosts for name in manifest.host_modules(host)}
    for name in sorted(used & set(modules)):
        module = modules[name]
        if (module.path / "compose.yaml.j2").exists():
            names.update(ENV_REFERENCE.findall(module.template("compose.yaml.j2")))
        for entry in [*module.permissions, *module.generated]:
            names.update(env for env in entry.get("env") or [] if ENV_NAME.match(str(env)))
    for host in manifest.hosts:
        names.update(env_settings(manifest.host_modules(host)))
    return sorted(names)


def autopilot_catalog(manifest: Manifest, modules: dict[str, Module], rules: dict[str, Any]) -> str:
    data = {
        "rules": trigger_rules(rules),
        "services": service_entries(manifest, modules),
        "secret_names": secret_names(manifest, modules),
    }
    return json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def autopilot_problems(manifest: Manifest) -> list[Problem]:
    known = load_modules()
    if NAME not in known:
        return []
    problems: list[Problem] = []
    for host in sorted(manifest.hosts):
        enabled = manifest.host_modules(host)
        if NAME not in enabled:
            continue
        settings = known[NAME].settings(enabled[NAME])
        path = f"hosts.{host}.modules.{NAME}"
        if manifest.contact == "none":
            problems.append(
                (
                    "alerting.contact",
                    "autopilot-contact",
                    "autopilot needs contact telegram or email: its webhook is a route of the "
                    "notification policy, which needs a default contact",
                )
            )
        if settings["runner"] == "claude-code" and not settings.get("claude_binary"):
            problems.append(
                (
                    f"{path}.claude_binary",
                    "autopilot-runner",
                    "runner claude-code needs claude_binary, the host path of the claude binary",
                )
            )
        if settings["auth"] == "subscription" and settings["runner"] != "claude-code":
            problems.append(
                (f"{path}.auth", "autopilot-runner", "auth subscription needs runner claude-code")
            )
        if settings["post_verdict"] and "oncall" not in enabled:
            problems.append(
                (
                    f"{path}.post_verdict",
                    "autopilot-verdict",
                    "post_verdict posts to the on-call chat and needs oncall on the same host",
                )
            )
        for index, text in enumerate(settings.get("mask") or []):
            try:
                pattern = re.compile(text)
            except re.error as error:
                problems.append(
                    (
                        f"{path}.mask[{index}]",
                        "autopilot-mask",
                        f"mask {text!r} is not a regular expression: {error}",
                    )
                )
                continue
            if pattern.match("") is not None:
                problems.append(
                    (
                        f"{path}.mask[{index}]",
                        "autopilot-mask",
                        f"mask {text!r} matches the empty string",
                    )
                )
        for index, env in enumerate(settings.get("mask_env") or []):
            if OWN_ENV.match(env):
                problems.append(
                    (
                        f"{path}.mask_env[{index}]",
                        "autopilot-mask",
                        f"{env} is a variable of the autopilot itself and is masked already",
                    )
                )
        caps = [("day_usd", settings["day_usd"])]
        if settings.get("quiet_hours"):
            caps.append(("night_usd", settings["night_usd"]))
        for key, cap in caps:
            if float(settings["run_usd"]) > float(cap):
                problems.append(
                    (
                        f"{path}.{key}",
                        "autopilot-caps",
                        f"{key} {cap} is below run_usd {settings['run_usd']}, so no run starts",
                    )
                )
    return problems
