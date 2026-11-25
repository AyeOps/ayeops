from __future__ import annotations

import json
import re
import zlib
from typing import Any

DASHBOARD_UID = "ayeops-baseline"
DATASOURCE = {"type": "prometheus", "uid": "victoriametrics"}
WEEKS = 4
LEARN_WEEKS = 2
DEFAULTS: dict[str, Any] = {"kind": "counter", "window": "1h", "below": 0.3, "floor": 5}
DEFAULT_FOR = "1h"
DEFAULT_SEVERITY = "warning"
DAYS = ("sun", "mon", "tue", "wed", "thu", "fri", "sat")
CLOCK = re.compile(r"^([01][0-9]|2[0-3]):([0-5][0-9])$")
SELECTOR = re.compile(r"^(?P<name>[A-Za-z_:][A-Za-z0-9_:]*)(?:\{(?P<matchers>.*)\})?$")
EMPTY = "ok_empty"
WEEK_LABEL = "aye_week"
HOST_LABELS = ("project", "group")
PANEL_SLOTS = 100000


def is_baseline(alert: dict[str, Any]) -> bool:
    return isinstance(alert.get("baseline"), dict)


def settings(spec: dict[str, Any]) -> dict[str, Any]:
    return {**DEFAULTS, **spec}


def with_matcher(metric: str, matcher: str) -> str:
    match = SELECTOR.match(metric.strip())
    if match is None:
        raise ValueError(f"{metric!r} is not a metric selector")
    inner = (match.group("matchers") or "").strip().rstrip(",")
    joined = f"{inner}, {matcher}" if inner else matcher
    return f"{match.group('name')}{{{joined}}}"


def series_labels(spec: dict[str, Any]) -> list[str]:
    labels = list(spec.get("by") or [])
    if "host" in labels:
        labels += [label for label in HOST_LABELS if label not in labels]
    return labels


def grouping(spec: dict[str, Any]) -> str:
    return ", ".join(series_labels(spec))


def aggregate(spec: dict[str, Any], inner: str) -> str:
    return f"sum by ({grouping(spec)}) ({inner})"


def window_value(spec: dict[str, Any], metric: str, offset: str = "") -> str:
    function = "increase" if spec["kind"] == "counter" else "avg_over_time"
    shifted = f" offset {offset}" if offset else ""
    return aggregate(spec, f"{function}({metric}[{spec['window']}]{shifted})")


def history(spec: dict[str, Any]) -> str:
    weeks = [
        f"label_replace({window_value(spec, spec['metric'], f'{week}w')}, "
        f'"{WEEK_LABEL}", "{week}", "", "")'
        for week in range(1, WEEKS + 1)
    ]
    return "(" + " or ".join(weeks) + ")"


def usual(spec: dict[str, Any]) -> str:
    return f"quantile by ({grouping(spec)}) (0.5, {history(spec)})"


def weeks_known(spec: dict[str, Any]) -> str:
    return f"count by ({grouping(spec)}) ({history(spec)})"


def current(spec: dict[str, Any]) -> str:
    return f"({window_value(spec, spec['metric'])} or ({usual(spec)} * 0))"


def minutes(clock: str) -> int:
    match = CLOCK.match(clock)
    if match is None:
        raise ValueError(f"{clock!r} is not HH:MM")
    return int(match.group(1)) * 60 + int(match.group(2))


def quiet_window(window: dict[str, Any]) -> str:
    now = "(hour() * 60 + minute())"
    start, end = minutes(str(window["from"])), minutes(str(window["to"]))
    joiner = " * " if start < end else " + "
    inside = f"(({now} >= bool {start}){joiner}({now} < bool {end}))"
    days = window.get("days") or []
    if not days or set(days) == set(DAYS):
        return inside
    on_day = " + ".join(f"(day_of_week() == bool {DAYS.index(day)})" for day in days)
    return f"({inside} * ({on_day}))"


def quiet_factor(spec: dict[str, Any]) -> str:
    windows = spec.get("quiet") or []
    if not windows:
        return ""
    total = " + ".join(quiet_window(window) for window in windows)
    return f" * on() group_left() (1 - clamp_max({total}, 1))"


def skip_vector(match: dict[str, str]) -> str:
    vector = "vector(1)"
    for label, value in sorted(match.items()):
        escaped = json.dumps(str(value), ensure_ascii=False)[1:-1]
        vector = f'label_replace({vector}, "{label}", "{escaped}", "", "")'
    return vector


def finish(spec: dict[str, Any], condition: str) -> str:
    expr = f"({condition}){quiet_factor(spec)}"
    for entry in spec.get("skip") or []:
        match = entry["match"]
        expr = f"({expr}) unless on({', '.join(sorted(match))}) {skip_vector(match)}"
    return f"({expr}) or on() vector(0)"


def guards(spec: dict[str, Any]) -> str:
    return f"({usual(spec)} > bool {spec['floor']}) * ({weeks_known(spec)} >= bool {LEARN_WEEKS})"


def drop_expr(spec: dict[str, Any]) -> str:
    now = current(spec)
    condition = (
        f"({now} < bool ({spec['below']} * {usual(spec)})) * ({now} > bool 0) * {guards(spec)}"
    )
    return finish(spec, condition)


def zero_expr(spec: dict[str, Any]) -> str:
    return finish(spec, f"({current(spec)} == bool 0) * {guards(spec)}")


def spike_expr(spec: dict[str, Any]) -> str:
    condition = f"({current(spec)} > bool ({spec['above']} * {usual(spec)})) * {guards(spec)}"
    return finish(spec, condition)


def empty_expr(spec: dict[str, Any]) -> str:
    empty = window_value(spec, with_matcher(spec["metric"], f'outcome="{EMPTY}"'))
    answers = window_value(spec, with_matcher(spec["metric"], f'outcome=~"ok|{EMPTY}"'))
    condition = (
        f"(({empty} or ({answers} * 0)) / {answers} > bool {spec['empty_share']}) "
        f"* ({answers} > bool {spec['floor']})"
    )
    return finish(spec, condition)


def percent(share: float) -> str:
    return f"{share * 100:g}%"


def zero_severity(severity: str) -> str:
    return "page" if severity == "page" else "critical"


def expand(
    alert: dict[str, Any], spec: dict[str, Any], base: str, drop_name: str
) -> list[dict[str, Any]]:
    spec = settings(spec)
    common = {
        key: value
        for key, value in alert.items()
        if key not in ("baseline", "name", "title", "summary", "expr", "severity", "op")
        and key not in ("threshold", "no_data")
    }
    common.setdefault("for", DEFAULT_FOR)
    common.setdefault("dashboard", {"uid": DASHBOARD_UID, "panel": share_panel(drop_name)})
    severity = str(alert.get("severity") or DEFAULT_SEVERITY)
    title = str(alert.get("title") or base)
    window = spec["window"]
    rules = [
        {
            **common,
            "name": drop_name,
            "title": f"{title}: below usual level",
            "summary": str(
                alert.get("summary")
                or f"{base}: the last {window} is under {percent(spec['below'])} of the usual "
                "level for this hour of the week"
            ),
            "severity": severity,
            "expr": drop_expr(spec),
        },
        {
            **common,
            "name": f"{base}-zero",
            "title": f"{title}: stopped",
            "summary": f"{base}: nothing in the last {window}, where this hour of the week "
            "usually has a flow",
            "severity": zero_severity(severity),
            "expr": zero_expr(spec),
        },
    ]
    if spec.get("above"):
        rules.append(
            {
                **common,
                "name": f"{base}-spike",
                "title": f"{title}: above usual level",
                "summary": f"{base}: the last {window} is over {spec['above']:g} times the usual "
                "level for this hour of the week",
                "severity": severity,
                "expr": spike_expr(spec),
            }
        )
    if spec.get("empty_share"):
        rules.append(
            {
                **common,
                "name": f"{base}-empty",
                "title": f"{title}: empty answers",
                "summary": f"{base}: over {percent(spec['empty_share'])} of the answers in the "
                f"last {window} were {EMPTY}",
                "severity": severity,
                "expr": empty_expr(spec),
            }
        )
    return rules


def rule_names(base: str, drop_name: str, spec: dict[str, Any]) -> list[str]:
    names = [drop_name, f"{base}-zero"]
    if spec.get("above"):
        names.append(f"{base}-spike")
    if spec.get("empty_share"):
        names.append(f"{base}-empty")
    return names


def legend(spec: dict[str, Any]) -> str:
    labels = spec.get("by") or []
    return " ".join(f"{{{{{label}}}}}" for label in labels)


def text(en: str, uk: str) -> dict[str, str]:
    return {"en": en, "uk": uk}


def target(expr: str, legend_format: str, ref: str, instant: bool = False) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "datasource": DATASOURCE,
        "expr": expr,
        "legendFormat": legend_format,
        "refId": ref,
    }
    if instant:
        entry["instant"] = True
        entry["format"] = "table"
    return entry


def share_thresholds(spec: dict[str, Any]) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = [
        {"color": "red", "value": None},
        {"color": "green", "value": spec["below"]},
    ]
    if spec.get("above"):
        steps.append({"color": "red", "value": spec["above"]})
    return steps


def alert_panels(name: str, spec: dict[str, Any], first_id: int, y: int) -> list[dict[str, Any]]:
    spec = settings(spec)
    names = legend(spec)
    return [
        {
            "collapsed": False,
            "gridPos": {"h": 1, "w": 24, "x": 0, "y": y},
            "id": first_id,
            "panels": [],
            "title": name,
            "type": "row",
        },
        {
            "datasource": DATASOURCE,
            "fieldConfig": {
                "defaults": {
                    "unit": "percentunit",
                    "min": 0,
                    "custom": {"thresholdsStyle": {"mode": "line+area"}},
                    "thresholds": {"mode": "absolute", "steps": share_thresholds(spec)},
                },
                "overrides": [],
            },
            "gridPos": {"h": 8, "w": 10, "x": 0, "y": y + 1},
            "id": first_id + 1,
            "targets": [
                target(f"{current(spec)} / {usual(spec)}", names or name, "A"),
            ],
            "title": text("Share of the usual level", "Частка звичного рівня"),
            "type": "timeseries",
        },
        {
            "datasource": DATASOURCE,
            "fieldConfig": {
                "defaults": {"min": 0},
                "overrides": [
                    {
                        "matcher": {"id": "byRegexp", "options": ".*usual$"},
                        "properties": [
                            {"id": "custom.lineStyle", "value": {"dash": [10, 10], "fill": "dash"}}
                        ],
                    }
                ],
            },
            "gridPos": {"h": 8, "w": 10, "x": 10, "y": y + 1},
            "id": first_id + 2,
            "targets": [
                target(current(spec), f"{names} actual".strip(), "A"),
                target(usual(spec), f"{names} usual".strip(), "B"),
            ],
            "title": text("Actual and usual", "Фактично і зазвичай"),
            "type": "timeseries",
        },
        {
            "datasource": DATASOURCE,
            "fieldConfig": {
                "defaults": {
                    "max": WEEKS,
                    "min": 0,
                    "thresholds": {
                        "mode": "absolute",
                        "steps": [
                            {"color": "blue", "value": None},
                            {"color": "green", "value": LEARN_WEEKS},
                        ],
                    },
                },
                "overrides": [],
            },
            "gridPos": {"h": 8, "w": 4, "x": 20, "y": y + 1},
            "id": first_id + 3,
            "options": {"displayMode": "basic", "orientation": "horizontal"},
            "targets": [target(weeks_known(spec), names or name, "A", instant=True)],
            "title": text("Weeks of history, under 2 learning", "Тижнів історії, менше 2 навчання"),
            "type": "bargauge",
        },
    ]


def row_id(name: str) -> int:
    return (zlib.crc32(name.encode()) % PANEL_SLOTS) * 10 + 1


def share_panel(name: str) -> int:
    return row_id(name) + 1


def dashboard(entries: list[tuple[str, dict[str, Any]]]) -> dict[str, Any]:
    panels: list[dict[str, Any]] = []
    for index, (name, spec) in enumerate(sorted(entries, key=lambda entry: entry[0])):
        panels += alert_panels(name, spec, row_id(name), index * 9)
    return {
        "editable": False,
        "panels": panels,
        "refresh": "1m",
        "schemaVersion": 39,
        "tags": ["ayeops"],
        "templating": {"list": []},
        "time": {"from": "now-7d", "to": "now"},
        "timezone": "browser",
        "title": text("Flow baselines", "Базові рівні потоків"),
        "uid": DASHBOARD_UID,
        "version": 1,
    }


def spec_problems(path: str, spec: dict[str, Any]) -> list[tuple[str, str, str]]:
    spec = settings(spec)
    problems = []
    by = set(series_labels(spec))
    for index, entry in enumerate(spec.get("skip") or []):
        unknown = sorted(set(entry["match"]) - by)
        if unknown:
            problems.append(
                (
                    f"{path}.skip.{index}",
                    "baseline-skip",
                    f"skip matches {', '.join(unknown)}, which is not in by, so it matches nothing",
                )
            )
    for index, window in enumerate(spec.get("quiet") or []):
        if window["from"] == window["to"]:
            problems.append(
                (f"{path}.quiet.{index}", "baseline-quiet", "the quiet window starts where it ends")
            )
    if spec.get("empty_share") and spec["kind"] != "counter":
        problems.append(
            (
                path,
                "baseline-empty",
                "empty_share counts answers by outcome, which needs a counter",
            )
        )
    return problems
