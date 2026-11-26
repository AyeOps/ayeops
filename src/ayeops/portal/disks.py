from __future__ import annotations

import re
from typing import Any

from ayeops.registry import Module, all_alerts

COMPARISON = re.compile(r"[<>]=?\s*bool\s+([0-9]+(?:\.[0-9]+)?)")
WINDOW = re.compile(r"\[([0-9]+[smhdw])\]")
FSTYPE = re.compile(r'fstype!~"[^"]*"')
ATTRIBUTES = re.compile(r'attribute_name=~"([^"]*)"')
DAYS_WARN = 14
DAYS_CRIT = 3
TREND_SECONDS = 30 * 86400
TREND_STEP_SECONDS = 6 * 3600
DAY_SECONDS = 86400
USED = "disk-full"
CRITICAL = "disk-critical"
FORECAST = "disk-full-forecast"
SECTORS = "smart-sectors-growing"
WEAR = "nvme-wear"
HEAT = "disk-temperature-high"
ROOT = 'mountpoint="/"'


def alert_threshold(expr: str) -> float | None:
    found = COMPARISON.findall(expr)
    return float(found[-1]) if found else None


def expr_window(expr: str) -> str | None:
    found = WINDOW.search(expr)
    return found.group(1) if found else None


def fs_matcher(expr: str) -> str | None:
    found = FSTYPE.search(expr)
    return found.group(0) if found else None


def attribute_names(expr: str) -> str | None:
    found = ATTRIBUTES.search(expr)
    return found.group(1) if found else None


def catalog_expr(modules: dict[str, Module], name: str) -> str:
    known = all_alerts(modules)
    return str(known[name][1].get("expr", "")) if name in known else ""


def disk_limits(modules: dict[str, Module]) -> dict[str, float | None]:
    return {
        "warn": alert_threshold(catalog_expr(modules, USED)),
        "crit": alert_threshold(catalog_expr(modules, CRITICAL)),
        "days_warn": DAYS_WARN,
        "days_crit": DAYS_CRIT,
        "wear_warn": alert_threshold(catalog_expr(modules, WEAR)),
        "temp_warn": alert_threshold(catalog_expr(modules, HEAT)),
    }


def matchers(hosts: list[str], *extra: str | None) -> str:
    found = [item for item in extra if item]
    return "{" + ",".join([*found, f'host=~"{"|".join(hosts)}"']) + "}"


def days_left(where: str, window: str, by: str) -> str:
    avail = f"node_filesystem_avail_bytes{where}"
    trend = f"{avail}[{window}]"
    return (
        f"min by ({by}) ({avail}"
        f" / (predict_linear({trend}, 0) - predict_linear({trend}, {DAY_SECONDS})))"
    )


def disk_queries(
    modules: dict[str, Module], nodes: list[str], forecast: list[str], smart: list[str]
) -> dict[str, str]:
    found: dict[str, str] = {}
    fstype = fs_matcher(catalog_expr(modules, USED))
    if nodes:
        where = matchers(nodes, fstype)
        labels = "host, mountpoint, device, fstype"
        found["fs_size"] = f"max by ({labels}) (node_filesystem_size_bytes{where})"
        found["fs_avail"] = f"max by ({labels}) (node_filesystem_avail_bytes{where})"
    window = expr_window(catalog_expr(modules, FORECAST))
    if nodes and window:
        found["host_disk_days"] = days_left(matchers(nodes, fstype, ROOT), window, "host")
    if forecast and window:
        found["fs_days"] = days_left(matchers(forecast, fstype), window, "host, mountpoint")
    if smart:
        sectors = catalog_expr(modules, SECTORS)
        names = attribute_names(sectors)
        attribute = matchers(
            smart, f'attribute_name=~"{names}"' if names else None, 'attribute_value_type="raw"'
        )
        found["smart_status"] = (
            f"max by (host, device) (smartctl_device_smart_status{matchers(smart)})"
        )
        found["smart_wear"] = (
            f"max by (host, device) (smartctl_device_percentage_used{matchers(smart)})"
        )
        current = matchers(smart, 'temperature_type="current"')
        found["smart_temp"] = f"max by (host, device) (smartctl_device_temperature{current})"
        if names:
            found["smart_sectors"] = (
                f"max by (host, device, attribute_name) (smartctl_device_attribute{attribute})"
            )
            growth = expr_window(sectors)
            if growth:
                found["smart_growth"] = (
                    f"max by (host, device) (increase(smartctl_device_attribute{attribute}"
                    f"[{growth}]))"
                )
    return found


def disk_ranges(modules: dict[str, Module], nodes: list[str]) -> dict[str, dict[str, Any]]:
    if not nodes:
        return {}
    where = matchers(nodes, fs_matcher(catalog_expr(modules, USED)))
    return {
        "fs_used": {
            "query": (
                f"max by (host, mountpoint) (100 * (1 - node_filesystem_avail_bytes{where}"
                f" / node_filesystem_size_bytes{where}))"
            ),
            "seconds": TREND_SECONDS,
            "step": TREND_STEP_SECONDS,
        }
    }
