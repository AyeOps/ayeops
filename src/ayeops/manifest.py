from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Any

from packaging.specifiers import InvalidSpecifier, SpecifierSet

from ayeops import __version__
from ayeops.i18n import DEFAULT_LANG, LANGS, is_reserved
from ayeops.strict import StrictValidator
from ayeops.yamlkeys import read_strict

SCHEMA_PATH = Path(__file__).parent / "schema" / "manifest.schema.json"
DEFAULT_TIMEZONE = "UTC"


def version_problems(
    data: dict[str, Any], running: str = __version__
) -> list[tuple[str, str, str]]:
    pin = data.get("ayeops")
    if not isinstance(pin, str) or not pin:
        return []
    try:
        wanted = SpecifierSet(pin)
    except InvalidSpecifier:
        message = f"{pin!r} is not a version specifier, write e.g. '>=0.2,<0.3' or '==0.2.0'"
        return [("ayeops", "ayeops-version", message)]
    if wanted.contains(running, prereleases=True):
        return []
    message = f"the manifest needs AyeOps {pin}, but AyeOps {running} is running"
    return [("ayeops", "ayeops-version", message)]


@cache
def manifest_schema() -> dict[str, Any]:
    schema: dict[str, Any] = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    return schema


def merge_profile(profile: Any, own: Any) -> Any:
    if isinstance(profile, dict) and own is None:
        return copy.deepcopy(profile)
    if not (isinstance(profile, dict) and isinstance(own, dict)):
        return copy.deepcopy(own)
    merged = copy.deepcopy(profile)
    for key, value in own.items():
        merged[key] = merge_profile(profile[key], value) if key in profile else copy.deepcopy(value)
    return merged


def profile_of(data: dict[str, Any], spec: Any) -> dict[str, Any] | None:
    profiles = data.get("profiles")
    if not isinstance(spec, dict) or not isinstance(profiles, dict):
        return None
    name = spec.get("profile")
    profile = profiles.get(name) if isinstance(name, str) else None
    return profile if isinstance(profile, dict) else None


def expand_profiles(data: dict[str, Any]) -> dict[str, Any]:
    hosts = data.get("hosts")
    if not isinstance(hosts, dict):
        return data
    expanded = {}
    for name, spec in hosts.items():
        profile = profile_of(data, spec)
        expanded[name] = spec if profile is None else merge_profile(profile, spec)
    if all(expanded[name] is spec for name, spec in hosts.items()):
        return data
    return {**data, "hosts": expanded}


def unknown_profiles(data: dict[str, Any]) -> list[tuple[str, str, str]]:
    hosts = data.get("hosts")
    profiles = data.get("profiles")
    known = profiles if isinstance(profiles, dict) else {}
    problems = []
    for host, spec in (hosts if isinstance(hosts, dict) else {}).items():
        name = spec.get("profile") if isinstance(spec, dict) else None
        if isinstance(name, str) and name not in known:
            problems.append((f"hosts.{host}.profile", "profile-known", f"unknown profile {name!r}"))
    return problems


def unused_profiles(data: dict[str, Any]) -> list[tuple[str, str, str]]:
    hosts = data.get("hosts")
    profiles = data.get("profiles")
    used = {
        spec.get("profile")
        for spec in (hosts.values() if isinstance(hosts, dict) else [])
        if isinstance(spec, dict)
    }
    return [
        (f"profiles.{name}", "profile-used", f"no host uses profile {name!r}")
        for name in (profiles if isinstance(profiles, dict) else {})
        if name not in used
    ]


@dataclass(frozen=True)
class Manifest:
    data: dict[str, Any]
    base_dir: Path
    raw: dict[str, Any] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "raw", self.data)
        object.__setattr__(self, "data", expand_profiles(self.data))

    @property
    def project(self) -> str:
        return str(self.data["project"])

    @property
    def timezone(self) -> str:
        return str(self.data.get("timezone") or DEFAULT_TIMEZONE)

    @property
    def hosts(self) -> dict[str, dict[str, Any]]:
        return dict(self.data["hosts"])

    @property
    def services(self) -> dict[str, dict[str, Any]]:
        return dict(self.data.get("services") or {})

    @property
    def active_services(self) -> dict[str, dict[str, Any]]:
        return {name: entry for name, entry in self.services.items() if not is_reserved(entry)}

    def shared_zone(self, first: str, second: str) -> str | None:
        zones = {str(self.hosts[name].get("zone") or "") for name in (first, second)}
        return zones.pop() if len(zones) == 1 and "" not in zones else None

    def reserved_on(self, host: str) -> set[str]:
        return {
            name
            for name, entry in self.services.items()
            if entry.get("host") == host and is_reserved(entry)
        }

    @property
    def portal_locale(self) -> str:
        locale = str((self.data.get("portal") or {}).get("locale") or DEFAULT_LANG)
        return locale if locale in LANGS else DEFAULT_LANG

    @property
    def flows(self) -> list[dict[str, Any]]:
        return [dict(flow) for flow in self.data.get("flows") or []]

    @property
    def externals(self) -> dict[str, dict[str, Any]]:
        return dict(self.data.get("externals") or {})

    @property
    def module_paths(self) -> list[Path]:
        return [self.base_dir / str(path) for path in self.data.get("module_paths") or []]

    @property
    def runbook_base_url(self) -> str:
        return str(self.data.get("runbook_base_url", ""))

    @property
    def contact(self) -> str:
        return str((self.data.get("alerting") or {}).get("contact", "telegram"))

    @property
    def alert_emails(self) -> list[str]:
        return [str(email) for email in (self.data.get("alerting") or {}).get("emails") or []]

    @property
    def mail(self) -> dict[str, Any] | None:
        mail = self.data.get("mail")
        if not mail:
            return None
        return {
            "host": str(mail["host"]),
            "port": int(mail.get("port", 587)),
            "from": str(mail["from"]),
            "from_name": str(mail.get("from_name") or self.project),
            "tls": str(mail.get("tls", "starttls")),
        }

    def host_group(self, host: str) -> str | None:
        spec = self.hosts[host]
        group = spec.get("group") or spec.get("profile")
        return str(group) if group else None

    def host_modules(self, host: str) -> dict[str, dict[str, Any]]:
        modules = self.hosts[host].get("modules") or {}
        return {
            name: dict(settings or {})
            for name, settings in modules.items()
            if settings is not False
        }

    def dropped_modules(self, host: str) -> set[str]:
        modules = self.hosts[host].get("modules") or {}
        return {name for name, settings in modules.items() if settings is False}

    def enabled_modules(self) -> set[str]:
        return {name for host in self.hosts for name in self.host_modules(host)}


def load_manifest(path: Path) -> Manifest:
    data = read_strict(path.read_text(encoding="utf-8"), path.name)
    if not isinstance(data, dict):
        data = {}
    return Manifest(data, path.parent)


def host_required(error: Any) -> bool:
    path = list(error.absolute_path)
    return error.validator == "required" and len(path) == 2 and path[0] == "hosts"


def schema_errors(data: dict[str, Any]) -> list[tuple[str, str]]:
    validator = StrictValidator(manifest_schema())
    found = [error for error in validator.iter_errors(data) if not host_required(error)]
    expanded = validator.iter_errors(expand_profiles(data))
    found += [error for error in expanded if host_required(error)]
    errors = set()
    for error in found:
        location = ".".join(str(part) for part in error.absolute_path) or "<root>"
        errors.add((location, error.message))
    return sorted(errors)
