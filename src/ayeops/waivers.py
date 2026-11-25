from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ayeops.i18n import DEFAULT_LANG, pick
from ayeops.manifest import Manifest

WARN_DAYS = 14
DAY = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
NEED_UNTIL = "waivers_need_until"

Problem = tuple[str, str, str]


@dataclass(frozen=True)
class Waiver:
    path: str
    rule: str
    reason: Any
    until: Any = None

    @property
    def dated(self) -> bool:
        return self.until is not None

    @property
    def text(self) -> str:
        return pick(self.reason, DEFAULT_LANG).strip()


def reason_of(value: Any) -> Any:
    if isinstance(value, dict) and "reason" in value:
        return value["reason"]
    return value


def until_of(value: Any) -> Any:
    if isinstance(value, dict) and "reason" in value:
        return value.get("until")
    return None


def waiver(path: str, rule: str, value: Any) -> Waiver:
    return Waiver(path, rule, reason_of(value), until_of(value))


def parse_day(value: Any) -> date:
    if isinstance(value, datetime):
        raise ValueError(f"until is a day without a time, YYYY-MM-DD, not {value.isoformat()!r}")
    if isinstance(value, date):
        return value
    text = str(value).strip() if isinstance(value, str) else ""
    if DAY.match(text):
        try:
            return date.fromisoformat(text)
        except ValueError:
            pass
    raise ValueError(f"until is a day as YYYY-MM-DD, not {value!r}")


def today_in(zone: str, now: datetime | None = None) -> date:
    try:
        tz: Any = ZoneInfo(zone)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        tz = UTC
    return (now or datetime.now(UTC)).astimezone(tz).date()


def service_waivers(name: str, service: dict[str, Any]) -> list[Waiver]:
    path = f"services.{name}"
    found = []
    metrics = service.get("metrics")
    if isinstance(metrics, dict) and "none" in metrics:
        found.append(waiver(f"{path}.metrics.none", "service-metrics", metrics["none"]))
    status = service.get("status")
    if isinstance(status, dict) and "internal" in status:
        found.append(waiver(f"{path}.status.internal", "service-status", status["internal"]))
    for index, alert in enumerate(service.get("alerts") or []):
        found += skip_waivers(f"{path}.alerts.{index}.baseline", alert.get("baseline"))
    for check, spec in sorted((service.get("checks") or {}).items()):
        alert = spec.get("alert") or {}
        found += skip_waivers(f"{path}.checks.{check}.alert.baseline", alert.get("baseline"))
    return found


def skip_waivers(path: str, baseline: Any) -> list[Waiver]:
    if not isinstance(baseline, dict):
        return []
    return [
        Waiver(f"{path}.skip.{index}", "baseline-skip", entry.get("reason"), entry.get("until"))
        for index, entry in enumerate(baseline.get("skip") or [])
    ]


def host_waivers(manifest: Manifest, host: str) -> list[Waiver]:
    modules = manifest.host_modules(host)
    ingress = modules.get("ingress")
    if ingress is None or "auth" in modules or not ingress.get("without_auth"):
        return []
    path = f"hosts.{host}.modules.ingress.without_auth"
    return [waiver(path, "ingress-auth", ingress["without_auth"])]


def manifest_waivers(manifest: Manifest) -> list[Waiver]:
    found = [
        item
        for name, service in sorted(manifest.active_services.items())
        for item in service_waivers(name, service)
    ]
    for host in sorted(manifest.hosts):
        found += host_waivers(manifest, host)
    return found


def need_until(manifest: Manifest) -> bool:
    rules = manifest.data.get("rules") or {}
    return bool(rules.get(NEED_UNTIL))


def days_left(item: Waiver, today: date) -> int | None:
    if not item.dated:
        return None
    try:
        return (parse_day(item.until) - today).days
    except ValueError:
        return None


def ends(item: Waiver, left: int) -> str:
    day = parse_day(item.until).isoformat()
    if left < 0:
        ago = -left
        return f"ended on {day}, {ago} day{'s' if ago != 1 else ''} ago"
    if left == 0:
        return f"ends on {day}, today"
    return f"ends on {day}, in {left} day{'s' if left != 1 else ''}"


def waiver_errors(manifest: Manifest, today: date) -> list[Problem]:
    problems: list[Problem] = []
    strict = need_until(manifest)
    for item in manifest_waivers(manifest):
        if not item.dated:
            if strict:
                problems.append(
                    (
                        item.path,
                        "waiver-until",
                        f"rules.{NEED_UNTIL} is on, so the {item.rule} waiver {item.text!r}"
                        " needs an end: {reason: <why>, until: YYYY-MM-DD}",
                    )
                )
            continue
        try:
            parse_day(item.until)
        except ValueError as error:
            problems.append((f"{item.path}.until", "waiver-until", str(error)))
            continue
        left = days_left(item, today)
        if left is not None and left < 0:
            problems.append(
                (
                    item.path,
                    item.rule,
                    f"the {item.rule} waiver {item.text!r} {ends(item, left)}: drop the waiver"
                    " and meet the rule, or set a later until",
                )
            )
    return problems


def waiver_warnings(manifest: Manifest, today: date) -> list[Problem]:
    problems: list[Problem] = []
    for item in manifest_waivers(manifest):
        left = days_left(item, today)
        if left is not None and 0 <= left <= WARN_DAYS:
            problems.append(
                (item.path, item.rule, f"the {item.rule} waiver {item.text!r} {ends(item, left)}")
            )
    return problems
