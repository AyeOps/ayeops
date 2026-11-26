from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from ayeops.baseline import DAYS, minutes
from ayeops.manifest import Manifest

Problem = tuple[str, str, str]
Guard = tuple[str, str]

MINUTE = 60
HOUR = 60 * MINUTE
DAY = 24 * HOUR
WEEK = 7 * DAY
UNITS = {"m": MINUTE, "h": HOUR, "d": DAY}
SPANS = (("d", DAY), ("h", HOUR), ("m", MINUTE))
DURATION = re.compile(r"([0-9]+)([mhd])")
FRESH = 180
NODE_JOB = 'job="node"'
NODE_WINDOW = "1d"
EPOCH_WEEKDAY = DAYS.index("thu")
GONE_AFTER = DAY
GONE_MIN = HOUR
GONE_MAX = WEEK
GONE_TAIL = WEEK
WINDOW_MAX = DAY
HOST_DOWN = "host-down"
HOST_GONE = "host-gone"
VOLATILE_RULE = "host-volatile"
WINDOW_RULE = "maintenance-window"


def node_up(selector: str, window: str) -> str:
    up = f"up{{{selector}}}"
    return f"last_over_time({up}[{window}]) * (lag({up}[{window}]) < bool {FRESH})"


def hosts_down(selector: str, window: str) -> str:
    return f"max by (host) ({node_up(selector, window)}) == 0"


NODE_UP = node_up(NODE_JOB, NODE_WINDOW)
HOSTS_DOWN = hosts_down(NODE_JOB, NODE_WINDOW)


def seconds(duration: str) -> int:
    match = DURATION.fullmatch(duration)
    if match is None:
        raise ValueError(f"{duration!r} is not a duration such as 30m, 2h or 1d")
    return int(match.group(1)) * UNITS[match.group(2)]


def span(total: int) -> str:
    unit, size = next((unit, size) for unit, size in SPANS if total % size == 0)
    return f"{total // size}{unit}"


def gone_after(spec: dict[str, Any]) -> int | None:
    value = spec.get("volatile")
    if value is True:
        return GONE_AFTER
    if isinstance(value, str):
        return seconds(value)
    return None


def volatile_hosts(manifest: Manifest) -> dict[str, int]:
    found: dict[str, int] = {}
    for host, spec in sorted(manifest.hosts.items()):
        after = gone_after(spec)
        if after is not None:
            found[host] = after
    return found


def window_terms(window: dict[str, Any]) -> list[tuple[int, int, int]]:
    start = minutes(str(window["start"])) * MINUTE
    length = seconds(str(window["duration"]))
    days = window.get("days") or []
    if not days:
        return [(start, DAY, length)]
    return [
        ((DAYS.index(day) - EPOCH_WEEKDAY) % len(DAYS) * DAY + start, WEEK, length) for day in days
    ]


def term_expr(offset: int, period: int, length: int) -> str:
    return f"vector(time() - {offset}) % {period} < {length}"


def in_window(windows: list[dict[str, Any]]) -> str:
    terms = [term for window in windows for term in window_terms(window)]
    return " or ".join(f"({term_expr(*term)})" for term in terms)


def maintenance_hosts(manifest: Manifest) -> dict[str, str]:
    return {
        host: in_window(spec["maintenance"])
        for host, spec in sorted(manifest.hosts.items())
        if spec.get("maintenance")
    }


def host_vector(host: str, expr: str) -> str:
    return f'label_replace({expr}, "host", "{host}", "", "")'


def host_set(hosts: list[str]) -> str:
    return " or ".join(host_vector(host, "vector(1)") for host in hosts)


def in_maintenance(windows: dict[str, str]) -> str:
    return " or ".join(host_vector(host, expr) for host, expr in windows.items())


def gone_term(hosts: list[str], after: int) -> str:
    up = f'up{{{NODE_JOB}, host=~"{"|".join(hosts)}"}}'
    seen = f"last_over_time({up}[{span(after + GONE_TAIL)}])"
    return (
        f"(max by (host) (max_over_time({up}[{span(after)}]))"
        f" + on (host) group_right () (0 * {seen})) or 0 * {seen}"
    )


def gone_expr(volatile: dict[str, int]) -> str:
    groups: dict[int, list[str]] = {}
    for host, after in volatile.items():
        groups.setdefault(after, []).append(host)
    terms = " or ".join(f"({gone_term(hosts, after)})" for after, hosts in sorted(groups.items()))
    return f"1 - ({terms})"


def volatile_down(host: str, after: int) -> str:
    return hosts_down(f'{NODE_JOB}, host="{host}"', span(after + GONE_TAIL))


@dataclass(frozen=True)
class Downtime:
    volatile: dict[str, int]
    windows: dict[str, str]


def downtime_of(manifest: Manifest) -> Downtime:
    return Downtime(volatile_hosts(manifest), maintenance_hosts(manifest))


def guarded(alert: dict[str, Any], guards: list[Guard]) -> dict[str, Any]:
    if not guards:
        return alert
    expr = str(alert["expr"]).strip()
    held = "".join(f" unless on ({labels}) ({vector})" for labels, vector in guards)
    return {**alert, "expr": f"({expr}){held} or 0 * ({expr})"}


def fleet_guards(hold: bool) -> list[Guard]:
    return [("host", HOSTS_DOWN)] if hold else []


def window_guards(down: Downtime) -> list[Guard]:
    return [("host", in_maintenance(down.windows))] if down.windows else []


def alert_guards(name: str, hold: bool, down: Downtime) -> list[Guard]:
    if name == HOST_DOWN:
        first = [("host", host_set(list(down.volatile)))] if down.volatile else []
    elif name == HOST_GONE:
        first = []
    else:
        first = fleet_guards(hold)
    return first + window_guards(down)


def module_alert(alert: dict[str, Any], hold: bool, down: Downtime) -> dict[str, Any]:
    name = str(alert["name"])
    if name == HOST_GONE:
        alert = {**alert, "expr": gone_expr(down.volatile)}
    return guarded(alert, alert_guards(name, hold, down))


def hostless_alert(alert: dict[str, Any]) -> dict[str, Any]:
    return guarded(alert, [])


def service_guards(host: str, hold: bool, down: Downtime) -> list[Guard]:
    guards = fleet_guards(hold) + window_guards(down)
    after = down.volatile.get(host)
    if hold and after is not None:
        guards.append(("", volatile_down(host, after)))
    if host in down.windows:
        guards.append(("", down.windows[host]))
    return guards


def service_alert(alert: dict[str, Any], host: str, hold: bool, down: Downtime) -> dict[str, Any]:
    return guarded(alert, service_guards(host, hold, down))


def volatile_problems(manifest: Manifest, host: str, spec: dict[str, Any]) -> list[Problem]:
    after = gone_after(spec)
    if after is None:
        return []
    path = f"hosts.{host}.volatile"
    if "node" not in manifest.host_modules(host):
        message = "volatile softens host-down, which needs the node module on the host"
        return [(path, VOLATILE_RULE, message)]
    if not GONE_MIN <= after <= GONE_MAX:
        message = f"volatile: {spec['volatile']} is outside 1h to 7d, the time a host may be gone"
        return [(path, VOLATILE_RULE, message)]
    return []


def window_problems(path: str, window: dict[str, Any]) -> list[Problem]:
    duration = str(window["duration"])
    length = seconds(duration)
    where = f"{path}.duration"
    if length == 0:
        return [(where, WINDOW_RULE, "a window of 0 never opens")]
    if length > WINDOW_MAX:
        message = f"{duration} is longer than 24h: split it into windows on consecutive days"
        return [(where, WINDOW_RULE, message)]
    days = window.get("days") or DAYS
    if length == WINDOW_MAX and len(days) == len(DAYS):
        return [(where, WINDOW_RULE, "a window of 24h on every day never closes")]
    return []


def downtime_problems(manifest: Manifest) -> list[Problem]:
    problems: list[Problem] = []
    for host, spec in sorted(manifest.hosts.items()):
        problems += volatile_problems(manifest, host, spec)
        for index, window in enumerate(spec.get("maintenance") or []):
            problems += window_problems(f"hosts.{host}.maintenance.{index}", window)
    return problems
