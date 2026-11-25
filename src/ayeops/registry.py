from __future__ import annotations

import copy
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

from ayeops.yamlkeys import load_strict

MODULES_DIR = Path(__file__).parent / "modules"

RESOURCES_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "memory": {"type": "string", "pattern": "^[0-9]+(\\.[0-9]+)?[kmg]$"},
            "cpus": {"type": "number", "exclusiveMinimum": 0},
        },
    },
}


@dataclass(frozen=True)
class Module:
    name: str
    path: Path
    spec: dict[str, Any]
    local: bool = False

    @property
    def summary(self) -> str:
        return str(self.spec["summary"])

    @property
    def requires(self) -> dict[str, dict[str, Any]]:
        return {
            name: dict(match or {}) for name, match in (self.spec.get("requires") or {}).items()
        }

    @property
    def defaults(self) -> dict[str, Any]:
        return copy.deepcopy(self.spec.get("defaults") or {})

    @property
    def settings_schema(self) -> dict[str, Any]:
        schema: dict[str, Any] = copy.deepcopy(self.spec["settings_schema"])
        schema.setdefault("properties", {})["resources"] = RESOURCES_SCHEMA
        return schema

    @property
    def catalog(self) -> dict[str, dict[str, Any]]:
        return dict(self.spec.get("services") or {})

    @property
    def alerts(self) -> list[dict[str, Any]]:
        return list(self.spec.get("alerts") or [])

    @property
    def dashboards(self) -> list[Path]:
        return [self.path / "dashboards" / name for name in self.spec.get("dashboards") or []]

    @property
    def files(self) -> list[dict[str, str]]:
        return list(self.spec.get("files") or [])

    @property
    def permissions(self) -> list[dict[str, Any]]:
        return list(self.spec.get("permissions") or [])

    @property
    def generated(self) -> list[dict[str, Any]]:
        return list(self.spec.get("generated") or [])

    @property
    def host_directories(self) -> list[dict[str, str]]:
        return list(self.spec.get("host_directories") or [])

    @property
    def backup_jobs(self) -> list[dict[str, str]]:
        return list(self.spec.get("backup_jobs") or [])

    def settings(self, overrides: dict[str, Any] | None, zone: str | None = None) -> dict[str, Any]:
        merged = self.defaults
        merged.update(overrides or {})
        if zone is not None:
            self.zoned(merged, overrides or {}, zone)
        return merged

    def zoned(self, merged: dict[str, Any], overrides: dict[str, Any], zone: str) -> None:
        properties = (self.spec.get("settings_schema") or {}).get("properties") or {}
        if "timezone" in properties and "timezone" not in overrides:
            merged["timezone"] = zone
        for key, spec in properties.items():
            value = merged.get(key)
            if not isinstance(value, dict) or "timezone" not in (spec.get("properties") or {}):
                continue
            own = overrides.get(key)
            if not (isinstance(own, dict) and "timezone" in own):
                merged[key] = {**value, "timezone": zone}

    def template(self, name: str) -> str:
        return (self.path / name).read_text(encoding="utf-8")


@cache
def load_modules() -> dict[str, Module]:
    modules: dict[str, Module] = {}
    for spec_path in sorted(MODULES_DIR.glob("*/module.yaml")):
        source = f"{spec_path.parent.name}/module.yaml"
        spec = load_strict(spec_path.read_text(encoding="utf-8"), source)
        modules[spec["name"]] = Module(spec["name"], spec_path.parent, spec)
    return modules


def all_alerts(modules: dict[str, Module]) -> dict[str, tuple[str, dict[str, Any]]]:
    alerts: dict[str, tuple[str, dict[str, Any]]] = {}
    for module in modules.values():
        for alert in module.alerts:
            alerts[alert["name"]] = (module.name, alert)
    return alerts
