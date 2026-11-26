from __future__ import annotations

import operator
import re
from collections.abc import Callable, Mapping
from functools import cache
from typing import Any

import yaml

from ayeops.promql import COMPARISON, Query, parse, top_level
from ayeops.registry import MODULES_DIR, Module
from tests.conftest import RENDERED, ROOT
from tests.test_dashboards import exporter_metrics, scrape_jobs

ALERTS = ROOT / "tests" / "fixtures" / "alerts"
UNFIXTURED = ALERTS / "unfixtured-metrics.yaml"
THRESHOLDS = ALERTS / "thresholds.yaml"
SCRIPTS = (".py", ".sh", ".awk", ".j2")
SCRIPT_PREFIX = re.compile(r"^PREFIX\s*=\s*\"?(ayeops_[a-z0-9_]+)\"?\s*$", re.M)
SCRIPT_NAME = re.compile(r"\bayeops_[a-z0-9_]*[a-z0-9]\b")
WORD = re.compile(r"\b[a-z][a-z0-9_]*\b")
REMOTE_WRITE_LABEL = re.compile(r"-remoteWrite\.label=([a-z_]+)=")
SCRAPE_LABELS = frozenset({"job", "instance"})
SET_OPERATOR = re.compile(r"(?<![A-Za-z0-9_:])(?:and|or|unless)(?![A-Za-z0-9_:])")
CONSTANT = re.compile(r"\s*bool\s+(-?[0-9]+(?:\.[0-9]+)?)\s*")
OPERATORS: dict[str, Callable[[float, float], bool]] = {
    "==": operator.eq,
    "!=": operator.ne,
    ">": operator.gt,
    ">=": operator.ge,
    "<": operator.lt,
    "<=": operator.le,
}


def builtin_alerts(modules: dict[str, Module]) -> list[tuple[str, dict[str, Any]]]:
    return [(module.name, alert) for module in modules.values() for alert in module.alerts]


@cache
def script_words() -> Mapping[str, frozenset[str]]:
    found = {}
    for path in sorted(MODULES_DIR.glob("*/files/*")):
        if path.suffix in SCRIPTS:
            text = path.read_text(encoding="utf-8")
            found[path.relative_to(MODULES_DIR).as_posix()] = frozenset(WORD.findall(text))
    return found


@cache
def script_metrics() -> Mapping[str, frozenset[str]]:
    produced: dict[str, set[str]] = {}
    for path in sorted(MODULES_DIR.glob("*/files/*")):
        if path.suffix not in SCRIPTS:
            continue
        name = path.relative_to(MODULES_DIR).as_posix()
        text = path.read_text(encoding="utf-8")
        names = set(SCRIPT_NAME.findall(text))
        for prefix in SCRIPT_PREFIX.findall(text):
            names |= {prefix + word for word in script_words()[name]}
        for metric in names:
            produced.setdefault(metric, set()).add(name)
    return {metric: frozenset(scripts) for metric, scripts in produced.items()}


@cache
def recorded_metrics() -> frozenset[str]:
    records = set()
    for path in sorted(RENDERED.glob("*/*/rules.yml")):
        for group in yaml.safe_load(path.read_text(encoding="utf-8"))["groups"]:
            records |= {rule["record"] for rule in group["rules"] if "record" in rule}
    return frozenset(records)


def metric_sources(metric: str) -> list[str]:
    sources = [f"fixture {name}" for name, names in exporter_metrics().items() if metric in names]
    sources += [f"script {script}" for script in sorted(script_metrics().get(metric, ()))]
    if metric in recorded_metrics():
        sources.append("recording rule")
    return sorted(sources)


@cache
def unfixtured() -> Mapping[str, str]:
    data: dict[str, str] = yaml.safe_load(UNFIXTURED.read_text(encoding="utf-8"))
    return data


@cache
def pipeline_labels() -> frozenset[str]:
    template = (MODULES_DIR / "metrics" / "compose.yaml.j2").read_text(encoding="utf-8")
    return SCRAPE_LABELS | frozenset(REMOTE_WRITE_LABEL.findall(template))


def simple_comparison(expr: str) -> tuple[str, str] | None:
    outside = top_level(expr)
    found = [
        match
        for match in COMPARISON.finditer(expr)
        if set(range(match.start(), match.end())) <= outside
    ]
    if len(found) != 1:
        return None
    match = found[0]
    constant = CONSTANT.fullmatch(expr, match.end())
    left = expr[: match.start()]
    left_outside = top_level(left)
    joined = any(word.start() in left_outside for word in SET_OPERATOR.finditer(left))
    if constant is None or joined or len(parse(left).metrics) != 1:
        return None
    return match.group(), constant.group(1)


@cache
def thresholds() -> Mapping[str, dict[str, Any]]:
    data: dict[str, dict[str, Any]] = yaml.safe_load(THRESHOLDS.read_text(encoding="utf-8"))
    return data


def queries(modules: dict[str, Module]) -> list[tuple[str, Query]]:
    return [(alert["name"], parse(alert["expr"])) for _, alert in builtin_alerts(modules)]


def test_every_builtin_alert_expression_parses(modules: dict[str, Module]) -> None:
    parsed = queries(modules)
    assert parsed
    assert all(query.metrics for _, query in parsed)


def test_every_rendered_rule_expression_parses() -> None:
    exprs = []
    for path in sorted(RENDERED.glob("*/grafana/provisioning/alerting/rules.yaml")):
        for group in yaml.safe_load(path.read_text(encoding="utf-8"))["groups"]:
            for rule in group["rules"]:
                exprs += [item["model"]["expr"] for item in rule["data"] if "expr" in item["model"]]
    for path in sorted(RENDERED.glob("*/*/rules.yml")):
        for group in yaml.safe_load(path.read_text(encoding="utf-8"))["groups"]:
            exprs += [rule["expr"] for rule in group["rules"]]
    assert exprs
    for expr in exprs:
        parse(expr)


def unsourced(modules: dict[str, Module]) -> list[str]:
    return [
        f"{name}: {metric}"
        for name, query in queries(modules)
        for metric in sorted(query.metrics)
        if not metric_sources(metric) and metric not in unfixtured()
    ]


def stale_unfixtured(modules: dict[str, Module]) -> dict[str, Any]:
    used = {metric for _, query in queries(modules) for metric in query.metrics}
    return {
        metric: metric_sources(metric) or "read by no built-in alert"
        for metric in unfixtured()
        if metric not in used or metric_sources(metric)
    }


def unscraped_jobs(modules: dict[str, Module]) -> list[str]:
    return [
        f"{name}: job {value}"
        for name, query in queries(modules)
        for selector in query.selectors
        for label, kind, value in selector.matchers
        if label == "job" and kind == "=" and value not in scrape_jobs()
    ]


def unwritten_labels(modules: dict[str, Module]) -> tuple[int, list[str]]:
    unknown = []
    checked = 0
    for name, query in queries(modules):
        producers = [script_metrics().get(metric, frozenset()) for metric in query.metrics]
        if not all(producers):
            continue
        checked += 1
        words = set().union(*(script_words()[script] for found in producers for script in found))
        unknown += [
            f"{name}: {label}"
            for label in sorted(query.labels - pipeline_labels())
            if label not in words
        ]
    return checked, unknown


def fake(*alerts: tuple[str, str]) -> dict[str, Module]:
    spec = {"alerts": [{"name": name, "expr": expr} for name, expr in alerts]}
    return {"fake": Module("fake", MODULES_DIR / "fake", spec)}


def test_every_metric_of_a_builtin_alert_is_captured_produced_or_explained(
    modules: dict[str, Module],
) -> None:
    assert unsourced(modules) == [], (
        "a metric no captured exporter fixture under tests/fixtures/exporters, no module script"
        " and no recording rule produces: fix a typo, capture the exporter's /metrics, or explain"
        f" it in {UNFIXTURED.relative_to(ROOT).as_posix()}"
    )


def test_a_misspelled_metric_is_refused() -> None:
    modules = fake(
        ("typo", "max by (host) (ayeops_offsite_drill_okk) == bool 0"),
        ("right", "ayeops_offsite_drill_ok == bool 0"),
        ("third", "rate(node_cpu_second_total[5m]) > bool 0"),
    )
    assert unsourced(modules) == ["typo: ayeops_offsite_drill_okk", "third: node_cpu_second_total"]


def test_every_unfixtured_metric_is_still_read_by_an_alert_and_still_has_no_source(
    modules: dict[str, Module],
) -> None:
    assert stale_unfixtured(modules) == {}


def test_an_unfixtured_metric_no_alert_reads_any_more_is_stale() -> None:
    stale = stale_unfixtured(fake(("gpu", "nvidia_smi_temperature_gpu > bool 85")))
    assert "nvidia_smi_temperature_gpu" not in stale
    assert stale["nvidia_smi_memory_used_bytes"] == "read by no built-in alert"


def test_every_unfixtured_metric_has_a_reason() -> None:
    assert unfixtured()
    for metric, reason in unfixtured().items():
        assert re.fullmatch(r"[a-z_:][a-z0-9_:]*", metric), metric
        assert isinstance(reason, str) and len(reason.split()) >= 8, metric


def test_every_job_an_alert_matches_is_scraped(modules: dict[str, Module]) -> None:
    assert unscraped_jobs(modules) == []


def test_a_misspelled_job_is_refused() -> None:
    modules = fake(("typo", 'up{job="nodes"} == bool 0'), ("right", 'up{job="node"} == bool 0'))
    assert unscraped_jobs(modules) == ["typo: job nodes"]


def test_labels_of_alerts_on_ayeops_series_appear_in_the_script_that_writes_them(
    modules: dict[str, Module],
) -> None:
    checked, unknown = unwritten_labels(modules)
    assert checked
    assert unknown == []


def test_a_misspelled_label_of_an_ayeops_series_is_refused() -> None:
    modules = fake(
        ("typo", "max by (host, repos) (ayeops_offsite_drill_ok) == bool 0"),
        ("right", 'max by (host, repo) (ayeops_offsite_drill_ok{repo="a"}) == bool 0'),
        ("foreign", "max by (nonsense) (up) == bool 0"),
    )
    assert unwritten_labels(modules) == (2, ["typo: repos"])


def test_the_pipeline_labels_are_the_scrape_labels_and_the_remote_write_ones() -> None:
    assert pipeline_labels() == {"job", "instance", "host", "project", "group"}


def test_sources_name_fixtures_scripts_and_recording_rules() -> None:
    assert metric_sources("up") == [
        f"fixture {name}" for name in sorted(exporter_metrics()) if "up" in exporter_metrics()[name]
    ]
    assert metric_sources("ayeops_offsite_drill_ok") == [
        "script objectstore/files/restore_drill.sh"
    ]
    assert metric_sources("ayeops_grafana_reader_last_success_timestamp_seconds") == [
        "script grafana/files/grafana_reader.py"
    ]
    assert metric_sources("aye:delivery_overdue") == ["recording rule"]
    assert metric_sources("ayeops_offsite_drill_okk") == []
    assert metric_sources("node_load_one") == []


def test_simple_comparisons_are_one_metric_against_a_constant() -> None:
    assert simple_comparison("up == bool 0") == ("==", "0")
    assert simple_comparison("ayeops_wifi_signal_dbm < bool -75") == ("<", "-75")
    assert simple_comparison('max by (host) (x{a!="b"}) >= bool 0.5') == (">=", "0.5")
    assert simple_comparison("time() - x > bool 300") == (">", "300")
    assert simple_comparison("min by (h) (x > 0) < bool 90") == ("<", "90")
    assert simple_comparison("x / y > bool 0.8") is None
    assert simple_comparison("x > bool 3 * y") is None
    assert simple_comparison("x > 5") is None
    assert simple_comparison("(x > bool 1) + (x < bool 0)") is None
    assert simple_comparison("x > bool 1 and x < bool 2") is None
    assert simple_comparison("x or vector(0) > bool 1") is None
    assert simple_comparison("x") is None


def comparisons(modules: dict[str, Module]) -> dict[str, tuple[str, str]]:
    found = {}
    for _, alert in builtin_alerts(modules):
        comparison = simple_comparison(alert["expr"])
        if comparison is not None:
            found[alert["name"]] = comparison
    return found


def test_every_threshold_of_a_simple_alert_has_a_reviewed_healthy_and_failing_value(
    modules: dict[str, Module],
) -> None:
    found = comparisons(modules)
    assert sorted(found) == sorted(thresholds()), (
        f"{THRESHOLDS.relative_to(ROOT).as_posix()} needs a row for every alert that compares one"
        " metric with a constant, and no other"
    )


def threshold_problems(modules: dict[str, Module], rows: Mapping[str, dict[str, Any]]) -> list[str]:
    wrong = []
    for name, (sign, constant) in comparisons(modules).items():
        row = rows.get(name)
        if row is None:
            continue
        compare = OPERATORS[sign]
        limit = float(constant)
        if row["when"] != f"{sign} {constant}":
            wrong.append(f"{name}: the expression now says {sign} {constant}, review the row")
        if compare(float(row["healthy"]), limit):
            wrong.append(f"{name}: healthy {row['healthy']} fires {sign} {constant}")
        if not compare(float(row["failing"]), limit):
            wrong.append(f"{name}: failing {row['failing']} stays quiet {sign} {constant}")
    return wrong


def test_each_threshold_separates_its_healthy_value_from_its_failing_one(
    modules: dict[str, Module],
) -> None:
    assert threshold_problems(modules, thresholds()) == []


def test_a_threshold_on_the_wrong_side_or_changed_since_the_review_is_refused() -> None:
    modules = fake(
        ("flipped", "nvidia_smi_temperature_gpu < bool 85"),
        ("moved", "nvidia_smi_temperature_gpu > bool 95"),
        ("right", "nvidia_smi_temperature_gpu > bool 85"),
    )
    row = {"when": "> 85", "healthy": 65, "failing": 90}
    rows = {"flipped": row, "moved": row, "right": row}
    assert threshold_problems(modules, rows) == [
        "flipped: the expression now says < 85, review the row",
        "flipped: healthy 65 fires < 85",
        "flipped: failing 90 stays quiet < 85",
        "moved: the expression now says > 95, review the row",
        "moved: failing 90 stays quiet > 95",
    ]


def test_threshold_rows_hold_only_the_reviewed_fields() -> None:
    for name, row in thresholds().items():
        assert set(row) - {"why"} == {"when", "healthy", "failing"}, name
        assert isinstance(row["healthy"], int | float), name
        assert isinstance(row["failing"], int | float), name
