from __future__ import annotations

import json
import re
from typing import Any

import pytest
import yaml

from ayeops.autopilot import unheld
from ayeops.baseline import (
    DASHBOARD_UID,
    WEEKS,
    current,
    dashboard,
    drop_expr,
    empty_expr,
    expand,
    finish,
    is_baseline,
    legend,
    minutes,
    percent,
    quiet_factor,
    quiet_window,
    row_id,
    rule_names,
    series_labels,
    settings,
    share_panel,
    spec_problems,
    spike_expr,
    target,
    usual,
    weeks_known,
    with_matcher,
    zero_expr,
    zero_severity,
)
from ayeops.checks import Check, check_alerts
from ayeops.portal.pages import alert_specs
from ayeops.promql import may_be_empty
from ayeops.registry import Module
from ayeops.render import HOSTS_DOWN, grafana_rules
from ayeops.rules import check_nodata
from tests.conftest import manifest_from, render_once, rules_hit
from tests.test_incidents_agent import a as incidents_agent

SPEC = {"metric": 'listings_total{app="trofey"}', "by": ["source", "category"]}
WEEK_SUM = 'sum by (source, category) (increase(listings_total{app="trofey"}[1h] offset {}w))'


def flow_alert(**baseline: Any) -> dict[str, Any]:
    return {
        "name": "listings-flow",
        "severity": "warning",
        "runbook": "listings-flow",
        "baseline": {**SPEC, **baseline},
    }


def without_flows(data: dict[str, Any]) -> dict[str, Any]:
    for service in data["services"].values():
        service["alerts"] = [a for a in service.get("alerts") or [] if "baseline" not in a]
    return data


def with_flow(data: dict[str, Any], **baseline: Any) -> dict[str, Any]:
    without_flows(data)["services"]["web"]["alerts"].append(flow_alert(**baseline))
    return data


def flow(data: dict[str, Any]) -> dict[str, Any]:
    alert: dict[str, Any] = data["services"]["web"]["alerts"][-1]
    return alert


def rendered_rules(data: dict[str, Any], modules: dict[str, Module]) -> dict[str, dict[str, Any]]:
    tree = render_once(manifest_from(data), modules)
    groups = yaml.safe_load(tree["central/grafana/provisioning/alerting/rules.yaml"])["groups"]
    return {rule["uid"]: rule for group in groups for rule in group["rules"]}


def rules_by_name(**baseline: Any) -> dict[str, dict[str, Any]]:
    alert = flow_alert(**baseline)
    return {rule["name"]: rule for rule in expand(alert, alert["baseline"], "flow", "flow")}


def sql_check(**baseline: Any) -> Check:
    return Check(
        "web",
        "listings",
        {
            "type": "sql",
            "datasource": "d",
            "query": "select category as label, n as value from x",
            "label": "label",
            "alert": {"severity": "warning", "runbook": "r", "baseline": baseline},
        },
        "central",
    )


def test_a_flow_alert_passes_every_rule(data: dict[str, Any]) -> None:
    assert rules_hit(with_flow(data)) == set()


def test_the_window_value_is_the_increase_per_label_set() -> None:
    spec = settings(SPEC)
    assert current(spec).startswith(
        '(sum by (source, category) (increase(listings_total{app="trofey"}[1h])) or '
    )


def test_a_gauge_is_averaged_over_the_window() -> None:
    spec = settings({**SPEC, "kind": "gauge", "window": "30m"})
    assert 'avg_over_time(listings_total{app="trofey"}[30m] offset 2w)' in usual(spec)
    assert "increase" not in usual(spec)


def test_the_usual_level_is_the_median_of_the_same_hour_over_four_weeks() -> None:
    expr = usual(settings(SPEC))
    assert expr.startswith("quantile by (source, category) (0.5, (label_replace(")
    for week in range(1, 5):
        assert f'{WEEK_SUM.replace("{}", str(week))}, "aye_week", "{week}", "", "")' in expr
    assert "offset 5w" not in expr


def test_a_missing_current_series_counts_as_zero_against_its_usual_level() -> None:
    spec = settings(SPEC)
    assert current(spec).endswith(f" or ({usual(spec)} * 0))")


def test_history_counts_weeks_per_label_set() -> None:
    assert weeks_known(settings(SPEC)).startswith("count by (source, category) ((label_replace(")


def test_the_drop_fires_below_the_share_only_above_the_floor_after_two_weeks() -> None:
    spec = settings(SPEC)
    expr = drop_expr(spec)
    assert f"({current(spec)} < bool (0.3 * {usual(spec)}))" in expr
    assert f"({current(spec)} > bool 0)" in expr
    assert f"({usual(spec)} > bool 5)" in expr
    assert f"({weeks_known(spec)} >= bool 2)" in expr


def test_share_and_floor_are_settings() -> None:
    spec = settings({**SPEC, "below": 0.5, "floor": 20})
    expr = drop_expr(spec)
    assert f"< bool (0.5 * {usual(spec)})" in expr
    assert f"({usual(spec)} > bool 20)" in expr


def test_zero_fires_on_exactly_nothing_with_the_same_guards() -> None:
    spec = settings(SPEC)
    expr = zero_expr(spec)
    assert f"({current(spec)} == bool 0)" in expr
    assert f"({usual(spec)} > bool 5)" in expr
    assert f"({weeks_known(spec)} >= bool 2)" in expr


def test_the_spike_compares_with_a_multiple_of_the_usual_level() -> None:
    spec = settings({**SPEC, "above": 4})
    expr = spike_expr(spec)
    assert f"({current(spec)} > bool (4 * {usual(spec)}))" in expr
    assert f"({usual(spec)} > bool 5)" in expr


def test_the_empty_share_divides_ok_empty_by_all_answers() -> None:
    spec = settings({"metric": "answers_total{app='t'}", "by": ["source"], "empty_share": 0.4})
    expr = empty_expr(spec)
    empty = "sum by (source) (increase(answers_total{app='t', outcome=\"ok_empty\"}[1h]))"
    answers = "sum by (source) (increase(answers_total{app='t', outcome=~\"ok|ok_empty\"}[1h]))"
    assert f"(({empty} or ({answers} * 0)) / {answers} > bool 0.4)" in expr
    assert f"({answers} > bool 5)" in expr


@pytest.mark.parametrize(
    ("metric", "expected"),
    [
        ("up", 'up{outcome="ok"}'),
        ('up{job="x"}', 'up{job="x", outcome="ok"}'),
        ('up{job="x",}', 'up{job="x", outcome="ok"}'),
        ("up{}", 'up{outcome="ok"}'),
    ],
)
def test_the_outcome_matcher_joins_the_selector(metric: str, expected: str) -> None:
    assert with_matcher(metric, 'outcome="ok"') == expected


def test_a_non_selector_metric_is_refused() -> None:
    with pytest.raises(ValueError, match="not a metric selector"):
        with_matcher("rate(up[5m])", 'outcome="ok"')


@pytest.mark.parametrize("build", [drop_expr, zero_expr, spike_expr])
def test_every_rule_returns_a_series_while_healthy_or_learning(build: Any) -> None:
    expr = build(settings({**SPEC, "above": 3}))
    assert expr.endswith(" or on() vector(0)")
    assert not may_be_empty(expr)
    assert check_nodata("x", {"expr": expr}, "Alerting") == []


def test_without_by_the_flow_is_one_series() -> None:
    spec = settings({"metric": "jobs_total"})
    assert usual(spec).startswith("quantile by () (0.5, ")
    assert "sum by () (increase(jobs_total[1h]))" in current(spec)


def test_a_quiet_window_holds_the_rule_at_zero_on_its_days() -> None:
    spec = {"quiet": [{"days": ["sat", "sun"], "from": "02:00", "to": "04:30"}]}
    now = "(hour() * 60 + minute())"
    assert quiet_factor(spec) == (
        " * on() group_left() (1 - clamp_max(("
        f"(({now} >= bool 120) * ({now} < bool 270)) * "
        "((day_of_week() == bool 6) + (day_of_week() == bool 0))), 1))"
    )


def test_a_quiet_window_across_midnight_covers_both_sides() -> None:
    now = "(hour() * 60 + minute())"
    spec = {"quiet": [{"from": "23:00", "to": "01:00"}, {"from": "12:00", "to": "12:10"}]}
    assert quiet_factor(spec) == (
        " * on() group_left() (1 - clamp_max("
        f"(({now} >= bool 1380) + ({now} < bool 60)) + "
        f"(({now} >= bool 720) * ({now} < bool 730)), 1))"
    )


def test_every_day_listed_is_the_same_as_no_days() -> None:
    days = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    every = {"quiet": [{"days": days, "from": "01:00", "to": "02:00"}]}
    plain = {"quiet": [{"from": "01:00", "to": "02:00"}]}
    assert quiet_factor(every) == quiet_factor(plain)
    assert "day_of_week" not in quiet_factor(every)


def test_quiet_windows_apply_to_every_rule() -> None:
    spec = settings({**SPEC, "above": 2, "quiet": [{"from": "01:00", "to": "02:00"}]})
    for build in (drop_expr, zero_expr, spike_expr):
        assert quiet_factor(spec) in build(spec)
    assert quiet_factor(settings(SPEC)) == ""


def test_a_skipped_series_is_left_out_by_its_labels() -> None:
    spec = settings(
        {**SPEC, "skip": [{"match": {"source": "olx", "category": 'a"b'}, "reason": "gone"}]}
    )
    vector = (
        'label_replace(label_replace(vector(1), "category", "a\\"b", "", ""), '
        '"source", "olx", "", "")'
    )
    assert f") unless on(category, source) {vector}) or on() vector(0)" in zero_expr(spec)


def test_a_skip_value_stays_one_promql_string_with_backslashes_and_newlines() -> None:
    value = 'a\\b"c\nd\te ключ'
    expr = zero_expr(settings({**SPEC, "skip": [{"match": {"source": value}, "reason": "x"}]}))
    written = expr.split('label_replace(vector(1), "source", ', 1)[1].split(', "", "")', 1)[0]
    assert "\n" not in written
    assert json.loads(written) == value


def test_each_skip_entry_matches_on_its_own_labels() -> None:
    spec = settings(
        {
            **SPEC,
            "skip": [
                {"match": {"source": "olx"}, "reason": "a"},
                {"match": {"category": "cars"}, "reason": "b"},
            ],
        }
    )
    expr = drop_expr(spec)
    assert 'unless on(source) label_replace(vector(1), "source", "olx", "", "")' in expr
    assert 'unless on(category) label_replace(vector(1), "category", "cars", "", "")' in expr


def test_the_rules_are_drop_and_zero_by_default() -> None:
    rules = rules_by_name()
    assert list(rules) == ["flow", "flow-zero"]


def test_spike_and_empty_rules_are_opt_in() -> None:
    assert list(rules_by_name(above=3, empty_share=0.5)) == [
        "flow",
        "flow-zero",
        "flow-spike",
        "flow-empty",
    ]


def test_zero_is_critical_unless_the_alert_pages() -> None:
    assert rules_by_name()["flow-zero"]["severity"] == "critical"
    assert rules_by_name()["flow"]["severity"] == "warning"
    alert = {**flow_alert(), "severity": "page"}
    rules = expand(alert, alert["baseline"], "flow", "flow")
    assert [rule["severity"] for rule in rules] == ["page", "page"]


def test_an_alert_without_severity_warns() -> None:
    alert = {"name": "flow", "runbook": "r", "baseline": SPEC}
    assert expand(alert, SPEC, "flow", "flow")[0]["severity"] == "warning"


def test_the_duration_defaults_to_an_hour_and_is_kept_when_set() -> None:
    assert {rule["for"] for rule in rules_by_name(above=2).values()} == {"1h"}
    alert = {**flow_alert(), "for": "20m"}
    assert {rule["for"] for rule in expand(alert, alert["baseline"], "f", "f")} == {"20m"}


def test_titles_and_summaries_name_the_condition() -> None:
    alert = {**flow_alert(above=3, empty_share=0.25), "title": "Listings"}
    rules = expand(alert, alert["baseline"], "flow", "flow")
    assert [rule["title"] for rule in rules] == [
        "Listings: below usual level",
        "Listings: stopped",
        "Listings: above usual level",
        "Listings: empty answers",
    ]
    assert "under 30% of the usual level" in rules[0]["summary"]
    assert "nothing in the last 1h" in rules[1]["summary"]
    assert "over 3 times" in rules[2]["summary"]
    assert "over 25% of the answers" in rules[3]["summary"]
    assert "baseline" not in rules[0] and "baseline" not in rules[1]


def test_an_own_summary_replaces_the_drop_summary() -> None:
    alert = {**flow_alert(), "summary": "OLX quiet"}
    assert expand(alert, alert["baseline"], "flow", "flow")[0]["summary"] == "OLX quiet"


def test_every_rule_links_the_share_panel_of_its_alert() -> None:
    link = {"uid": DASHBOARD_UID, "panel": share_panel("flow")}
    assert {json.dumps(rule["dashboard"]) for rule in rules_by_name(above=2).values()} == {
        json.dumps(link)
    }
    alert = {**flow_alert(), "dashboard": False}
    assert expand(alert, alert["baseline"], "f", "f")[0]["dashboard"] is False


def test_the_service_alert_renders_per_label_rules_with_links(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    rules = rendered_rules(with_flow(data, above=3), modules)
    drop = rules["aye-listings-flow"]
    assert set(drop["labels"]) == {"severity", "service", "scope"}
    assert (drop["labels"]["severity"], drop["labels"]["service"]) == ("warning", "web")
    assert drop["noDataState"] == "Alerting"
    assert drop["for"] == "1h"
    assert drop["annotations"]["__dashboardUid__"] == DASHBOARD_UID
    assert drop["annotations"]["__panelId__"] == str(share_panel("listings-flow"))
    assert "quantile by (source, category)" in drop["data"][0]["model"]["expr"]
    assert rules["aye-listings-flow-zero"]["labels"]["severity"] == "critical"
    assert "aye-listings-flow-spike" in rules


def test_every_rule_is_held_back_while_its_host_is_down(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    spec = settings({**SPEC, "by": ["host"], "above": 3})
    rules = rendered_rules(with_flow(data, by=["host"], above=3), modules)
    for uid, build in (
        ("aye-listings-flow", drop_expr),
        ("aye-listings-flow-zero", zero_expr),
        ("aye-listings-flow-spike", spike_expr),
    ):
        expr = rules[uid]["data"][0]["model"]["expr"]
        plain = build(spec)
        assert expr == f"({plain}) unless on (host) ({HOSTS_DOWN}) or 0 * ({plain})"
        assert unheld(expr) == plain
        assert not may_be_empty(expr)


def test_a_flow_summed_over_hosts_has_no_host_to_hold_it_back(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    model = rendered_rules(with_flow(data), modules)["aye-listings-flow-zero"]["data"][0]["model"]
    plain = zero_expr(settings(SPEC))
    assert model["expr"].startswith(f"({plain}) unless on (host) (")
    assert "host" not in plain.replace("on (host)", "")
    assert check_nodata("x", model, "Alerting") == []


def test_a_flow_split_by_host_keeps_the_profile_labels() -> None:
    spec = settings({"metric": "jobs_total", "by": ["host"]})
    assert usual(spec).startswith("quantile by (host, project, group) (0.5, ")
    assert "sum by (host, project, group) (increase(jobs_total[1h]))" in current(spec)
    assert weeks_known(spec).startswith("count by (host, project, group) (")
    assert legend(spec) == "{{host}}"


def test_profile_labels_join_only_a_split_by_host_and_only_once() -> None:
    assert series_labels({"by": ["source"]}) == ["source"]
    assert series_labels({"by": ["group", "host"]}) == ["group", "host", "project"]
    assert series_labels({}) == []


def test_a_flow_split_by_host_may_skip_a_group(data: dict[str, Any]) -> None:
    with_flow(data, by=["host"], skip=[{"match": {"group": "workers"}, "reason": "x"}])
    assert rules_hit(data) == set()


def test_the_usual_level_spans_the_weeks_of_the_incident_journal() -> None:
    assert incidents_agent.WEEKS == WEEKS
    assert incidents_agent.median([4.0, 1.0, 9.0, 2.0]) == 3.0
    assert "quantile by (source, category) (0.5, " in usual(settings(SPEC))


def test_the_portal_links_a_flow_alert_to_its_share_panel(modules: dict[str, Module]) -> None:
    owner, spec = alert_specs(modules, {"alerts": [flow_alert()]})["listings-flow"]
    assert owner == ""
    assert spec["dashboard"] == {"uid": DASHBOARD_UID, "panel": share_panel("listings-flow")}
    assert spec["expr"] == drop_expr(settings(SPEC))


def test_a_hand_link_to_a_share_panel_is_known(data: dict[str, Any]) -> None:
    with_flow(data)
    web = data["services"]["web"]["alerts"][0]
    web["dashboard"] = {"uid": DASHBOARD_UID, "panel": share_panel("listings-flow")}
    assert rules_hit(data) == set()
    web["dashboard"] = {"uid": DASHBOARD_UID, "panel": share_panel("listings-flow") + 7}
    assert rules_hit(data) == {"alert-dashboard"}


def test_the_baseline_dashboard_is_rendered_only_with_flow_alerts(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    path = f"central/grafana/dashboards/{DASHBOARD_UID}.json"
    assert path not in render_once(manifest_from(without_flows(data)), modules)
    board = json.loads(render_once(manifest_from(with_flow(data)), modules)[path])
    assert board["uid"] == DASHBOARD_UID
    assert board["title"] == "Flow baselines"
    ids = [panel["id"] for panel in board["panels"]]
    assert share_panel("listings-flow") in ids
    assert len(ids) == len(set(ids)) == 4


def test_the_dashboard_follows_the_grafana_locale(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    with_flow(data)["hosts"]["central"]["modules"]["grafana"]["locale"] = "uk"
    tree = render_once(manifest_from(data), modules)
    board = json.loads(tree[f"central/grafana/dashboards/{DASHBOARD_UID}.json"])
    assert board["title"] == "Базові рівні потоків"


def test_the_dashboard_shows_share_actual_usual_and_history() -> None:
    spec = {**SPEC, "above": 3}
    row, share, actual, weeks = dashboard([("flow", spec)])["panels"]
    full = settings(spec)
    assert row["type"] == "row" and row["title"] == "flow"
    assert share["targets"][0]["expr"] == f"{current(full)} / {usual(full)}"
    assert share["targets"][0]["legendFormat"] == "{{source}} {{category}}"
    steps = share["fieldConfig"]["defaults"]["thresholds"]["steps"]
    assert [step["value"] for step in steps] == [None, 0.3, 3]
    assert [target["expr"] for target in actual["targets"]] == [current(full), usual(full)]
    assert [target["legendFormat"] for target in actual["targets"]] == [
        "{{source}} {{category}} actual",
        "{{source}} {{category}} usual",
    ]
    assert weeks["targets"][0]["expr"] == weeks_known(full)
    assert weeks["targets"][0]["instant"] is True
    assert [s["value"] for s in weeks["fieldConfig"]["defaults"]["thresholds"]["steps"]] == [
        None,
        2,
    ]


def test_without_a_spike_the_band_is_open_above() -> None:
    share = dashboard([("flow", SPEC)])["panels"][1]
    assert [s["value"] for s in share["fieldConfig"]["defaults"]["thresholds"]["steps"]] == [
        None,
        0.3,
    ]


def test_the_band_starts_at_the_configured_share() -> None:
    share = dashboard([("flow", {**SPEC, "below": 0.6})])["panels"][1]
    steps = share["fieldConfig"]["defaults"]["thresholds"]["steps"]
    assert [step["value"] for step in steps] == [None, 0.6]


def test_a_sql_baseline_has_its_dashboard_row_and_unique_names(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    without_flows(data)["hosts"]["central"]["modules"]["checks"] = {}
    data["services"]["web"]["checks"] = {
        "listings": {
            "type": "sql",
            "datasource": "acme-orders",
            "query": "select 1 as value",
            "alert": {"for": "1h", "severity": "warning", "runbook": "r", "baseline": {}},
        }
    }
    tree = render_once(manifest_from(data), modules)
    board = json.loads(tree[f"central/grafana/dashboards/{DASHBOARD_UID}.json"])
    rows = [panel["title"] for panel in board["panels"] if panel["type"] == "row"]
    assert rows == ["web-listings-low"]
    data["services"]["web"]["alerts"].append(
        {"name": "web-listings-zero", "expr": "up == bool 0", "severity": "info", "runbook": "x"}
    )
    assert rules_hit(data) == {"alert-unique"}


def test_dashboard_rows_are_sorted_and_stacked() -> None:
    panels = dashboard([("b", SPEC), ("a", {"metric": "x"})])["panels"]
    rows = [panel for panel in panels if panel["type"] == "row"]
    assert [row["title"] for row in rows] == ["a", "b"]
    assert [row["gridPos"]["y"] for row in rows] == [0, 9]
    assert panels[1]["targets"][0]["legendFormat"] == "a"


def test_a_sql_check_with_a_baseline_keeps_its_failure_rule() -> None:
    rules = check_alerts(sql_check(above=2))
    assert [rule["name"] for rule in rules] == [
        "web-listings",
        "web-listings-low",
        "web-listings-zero",
        "web-listings-spike",
    ]
    failing = rules[0]["expr"]
    assert failing.startswith('1 - max(ayeops_check_sql_ok{check="web-listings"}')
    assert "ayeops_check_sql_value" not in failing
    assert "baseline" not in rules[0]


def test_a_sql_baseline_averages_the_value_per_label() -> None:
    low = check_alerts(sql_check())[1]
    assert (
        'sum by (label) (avg_over_time(ayeops_check_sql_value{check="web-listings"}[1h]))'
        in low["expr"]
    )
    assert low["title"] == "web-listings: below usual level"
    assert low["dashboard"] == {"uid": DASHBOARD_UID, "panel": share_panel("web-listings-low")}


def test_a_sql_check_with_a_baseline_renders(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    data["hosts"]["central"]["modules"]["checks"] = {}
    data["services"]["web"]["checks"] = {
        "listings": {
            "type": "sql",
            "datasource": "acme-orders",
            "query": "select category as label, n as value from x",
            "label": "label",
            "alert": {"for": "1h", "severity": "warning", "runbook": "r", "baseline": {"floor": 3}},
        }
    }
    assert rules_hit(data) == set()
    rules = rendered_rules(data, modules)
    assert {"aye-web-listings", "aye-web-listings-low", "aye-web-listings-zero"} <= set(rules)
    assert "> bool 3)" in rules["aye-web-listings-low"]["data"][0]["model"]["expr"]


def test_a_module_alert_can_be_a_baseline(data: dict[str, Any], modules: dict[str, Module]) -> None:
    node = modules["node"]
    alert = {"name": "jobs-flow", "severity": "warning", "runbook": "x", "baseline": SPEC}
    changed = {**modules, "node": Module("node", node.path, {**node.spec, "alerts": [alert]})}
    groups = grafana_rules(manifest_from(data), changed)["groups"]
    node_rules = next(group for group in groups if group["name"] == "node")["rules"]
    assert [rule["uid"] for rule in node_rules] == ["aye-jobs-flow", "aye-jobs-flow-zero"]
    assert node_rules[0]["noDataState"] == "OK"
    assert node_rules[0]["annotations"]["runbook_url"].endswith("/runbooks/x.md")
    assert rules_hit(data) == set()
    tree = render_once(manifest_from(without_flows(data)), changed)
    board = json.loads(tree[f"central/grafana/dashboards/{DASHBOARD_UID}.json"])
    assert [panel["title"] for panel in board["panels"] if panel["type"] == "row"] == ["jobs-flow"]


def test_the_example_renders_its_flow_alert(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    tree = render_once(manifest_from(data), modules)
    groups = yaml.safe_load(tree["central/grafana/provisioning/alerting/rules.yaml"])["groups"]
    names = {rule["uid"] for group in groups for rule in group["rules"]}
    assert {
        "aye-mcp-calls-flow",
        "aye-mcp-calls-flow-zero",
        "aye-mcp-calls-flow-spike",
        "aye-mcp-calls-flow-empty",
    } <= names
    board = json.loads(tree[f"central/grafana/dashboards/{DASHBOARD_UID}.json"])
    assert board["panels"][0]["title"] == "mcp-calls-flow"


@pytest.mark.parametrize(
    ("change", "value"),
    [
        ("below", 1),
        ("below", 0),
        ("above", 1),
        ("floor", -1),
        ("kind", "histogram"),
        ("window", "1w"),
        ("metric", "rate(x[5m])"),
        ("empty_share", 0),
        ("by", ["a", "a"]),
        ("skip", [{"match": {"source": "olx"}}]),
        ("skip", [{"match": {}, "reason": "x"}]),
        ("quiet", [{"from": "24:00", "to": "01:00"}]),
        ("quiet", [{"days": ["monday"], "from": "01:00", "to": "02:00"}]),
        ("threshold", 3),
    ],
)
def test_a_bad_baseline_is_a_schema_error(data: dict[str, Any], change: str, value: Any) -> None:
    flow(with_flow(data))["baseline"][change] = value
    assert rules_hit(data) == {"schema"}


def test_an_alert_has_an_expr_or_a_baseline_not_both(data: dict[str, Any]) -> None:
    flow(with_flow(data))["expr"] = "up == bool 0"
    assert rules_hit(data) == {"schema"}
    del flow(data)["expr"]
    del flow(data)["baseline"]
    assert rules_hit(data) == {"schema"}


@pytest.mark.parametrize(
    "extra", [{"op": ">"}, {"threshold": 1}, {"no_data": "ok"}, {"op": ">", "threshold": 1}]
)
def test_a_sql_baseline_replaces_the_condition(data: dict[str, Any], extra: dict[str, Any]) -> None:
    data["hosts"]["central"]["modules"]["checks"] = {}
    data["services"]["web"]["checks"] = {
        "listings": {
            "type": "sql",
            "datasource": "acme-orders",
            "query": "select 1 as value",
            "alert": {"severity": "warning", "runbook": "r", "baseline": {}, **extra},
        }
    }
    assert rules_hit(data) == {"schema"}


def test_a_sql_baseline_takes_no_metric(data: dict[str, Any]) -> None:
    data["hosts"]["central"]["modules"]["checks"] = {}
    data["services"]["web"]["checks"] = {
        "listings": {
            "type": "sql",
            "datasource": "acme-orders",
            "query": "select 1 as value",
            "alert": {"severity": "warning", "runbook": "r", "baseline": {"metric": "x"}},
        }
    }
    assert rules_hit(data) == {"schema"}


def test_a_skip_on_a_label_outside_by_is_refused(data: dict[str, Any]) -> None:
    with_flow(data, skip=[{"match": {"region": "eu"}, "reason": "x"}])
    assert rules_hit(data) == {"baseline-skip"}


def test_a_sql_skip_matches_the_label_column(data: dict[str, Any]) -> None:
    data["hosts"]["central"]["modules"]["checks"] = {}
    skip = [{"match": {"label": "cars"}, "reason": "category retired"}]
    data["services"]["web"]["checks"] = {
        "listings": {
            "type": "sql",
            "datasource": "acme-orders",
            "query": "select category as label, n as value from x",
            "alert": {
                "for": "1h",
                "severity": "warning",
                "runbook": "r",
                "baseline": {"skip": skip},
            },
        }
    }
    assert rules_hit(data) == {"baseline-skip"}
    data["services"]["web"]["checks"]["listings"]["label"] = "label"
    assert rules_hit(data) == set()


def test_an_empty_quiet_window_is_refused(data: dict[str, Any]) -> None:
    with_flow(data, quiet=[{"from": "03:00", "to": "03:00"}])
    assert rules_hit(data) == {"baseline-quiet"}


def test_the_empty_share_needs_a_counter(data: dict[str, Any]) -> None:
    with_flow(data, kind="gauge", empty_share=0.5)
    assert rules_hit(data) == {"baseline-empty"}
    flow(data)["baseline"]["kind"] = "counter"
    assert rules_hit(data) == set()


@pytest.mark.parametrize("suffix", ["zero", "spike", "empty"])
def test_generated_rule_names_are_unique(data: dict[str, Any], suffix: str) -> None:
    with_flow(data, above=2, empty_share=0.5)
    data["services"]["web"]["alerts"].append(
        {
            "name": f"listings-flow-{suffix}",
            "expr": "up == bool 0",
            "severity": "info",
            "runbook": "x",
        }
    )
    assert rules_hit(data) == {"alert-unique"}


def test_the_baseline_dashboard_can_be_named_by_hand(data: dict[str, Any]) -> None:
    without_flows(data)["services"]["web"]["alerts"][0]["dashboard"] = DASHBOARD_UID
    assert rules_hit(data) == {"alert-dashboard"}
    assert rules_hit(with_flow(data)) == set()


NOW = "(hour() * 60 + minute())"
SOURCE = {"type": "prometheus", "uid": "victoriametrics"}


def weeks_of(by: str, selector: str, function: str = "increase") -> str:
    parts = [
        f"label_replace(sum by ({by}) ({function}({selector} offset {week}w)), "
        f'"aye_week", "{week}", "", "")'
        for week in ("1", "2", "3", "4")
    ]
    return "(" + " or ".join(parts) + ")"


def usual_of(by: str, selector: str) -> str:
    return f"quantile by ({by}) (0.5, {weeks_of(by, selector)})"


def known_of(by: str, selector: str) -> str:
    return f"count by ({by}) ({weeks_of(by, selector)})"


def current_of(by: str, selector: str) -> str:
    return f"(sum by ({by}) (increase({selector})) or ({usual_of(by, selector)} * 0))"


def guards_of(by: str, selector: str) -> str:
    return f"({usual_of(by, selector)} > bool 5) * ({known_of(by, selector)} >= bool 2)"


def test_every_flow_expression_is_pinned_to_the_last_bracket() -> None:
    spec = settings({"metric": "jobs_total", "by": ["source"], "above": 3, "empty_share": 0.5})
    now, normal = current_of("source", "jobs_total[1h]"), usual_of("source", "jobs_total[1h]")
    guards = guards_of("source", "jobs_total[1h]")
    assert current(spec) == now
    assert usual(spec) == normal
    assert weeks_known(spec) == known_of("source", "jobs_total[1h]")
    assert drop_expr(spec) == (
        f"((({now} < bool (0.3 * {normal})) * ({now} > bool 0) * {guards})) or on() vector(0)"
    )
    assert zero_expr(spec) == f"((({now} == bool 0) * {guards})) or on() vector(0)"
    assert spike_expr(spec) == f"((({now} > bool (3 * {normal})) * {guards})) or on() vector(0)"
    empty = 'sum by (source) (increase(jobs_total{outcome="ok_empty"}[1h]))'
    answers = 'sum by (source) (increase(jobs_total{outcome=~"ok|ok_empty"}[1h]))'
    assert empty_expr(spec) == (
        f"(((({empty} or ({answers} * 0)) / {answers} > bool 0.5) * ({answers} > bool 5))) "
        "or on() vector(0)"
    )


def test_a_gauge_averages_every_week_of_its_history() -> None:
    spec = settings({"metric": "temp", "kind": "gauge", "window": "15m"})
    history = weeks_of("", "temp[15m]", "avg_over_time")
    assert weeks_known(spec) == f"count by () ({history})"
    assert current(spec) == (
        f"(sum by () (avg_over_time(temp[15m])) or (quantile by () (0.5, {history}) * 0))"
    )


def test_quiet_windows_and_skips_wrap_the_condition_in_order() -> None:
    spec = {
        "quiet": [{"from": "01:00", "to": "02:00"}],
        "skip": [
            {"match": {"source": "olx"}, "reason": "a"},
            {"match": {"category": "cars", "source": "x"}, "reason": "b"},
        ],
    }
    quiet = f" * on() group_left() (1 - clamp_max((({NOW} >= bool 60) * ({NOW} < bool 120)), 1))"
    first = 'label_replace(vector(1), "source", "olx", "", "")'
    second = (
        'label_replace(label_replace(vector(1), "category", "cars", "", ""), "source", "x", "", "")'
    )
    assert finish(spec, "C") == (
        f"((((C){quiet}) unless on(source) {first}) unless on(category, source) {second})"
        " or on() vector(0)"
    )
    assert finish({}, "C") == "((C)) or on() vector(0)"


def test_a_skip_value_keeps_its_letters_and_turns_numbers_into_text() -> None:
    expr = zero_expr(settings({**SPEC, "skip": [{"match": {"source": "ключ"}, "reason": "x"}]}))
    assert 'label_replace(vector(1), "source", "ключ", "", "")' in expr
    expr = zero_expr(settings({**SPEC, "skip": [{"match": {"source": 7}, "reason": "x"}]}))
    assert 'label_replace(vector(1), "source", "7", "", "")' in expr


@pytest.mark.parametrize(
    ("clock", "expected"), [("00:00", 0), ("09:05", 545), ("19:59", 1199), ("23:59", 1439)]
)
def test_a_clock_is_minutes_since_midnight(clock: str, expected: int) -> None:
    assert minutes(clock) == expected


@pytest.mark.parametrize("clock", ["24:00", "12:60", "7:00", "07:00:00", " 07:00", "noon"])
def test_a_clock_outside_hh_mm_is_refused(clock: str) -> None:
    with pytest.raises(ValueError, match=f"^{re.escape(repr(clock))} is not HH:MM$"):
        minutes(clock)


def test_a_window_that_starts_where_it_ends_is_read_across_midnight() -> None:
    window = {"from": "03:00", "to": "03:00"}
    assert quiet_window(window) == f"(({NOW} >= bool 180) + ({NOW} < bool 180))"


def test_a_window_on_one_day_checks_that_day() -> None:
    window = {"days": ["mon"], "from": "00:00", "to": "00:01"}
    assert quiet_window(window) == (
        f"((({NOW} >= bool 0) * ({NOW} < bool 1)) * ((day_of_week() == bool 1)))"
    )
    window = {"days": [], "from": "22:00", "to": "06:00"}
    assert quiet_window(window) == f"(({NOW} >= bool 1320) + ({NOW} < bool 360))"


def test_the_selector_is_trimmed_and_named_in_the_refusal() -> None:
    assert with_matcher("  up  ", 'a="b"') == 'up{a="b"}'
    assert with_matcher('up{ job="x" }', 'a="b"') == 'up{job="x", a="b"}'
    assert with_matcher("ns:up_total", 'a="b"') == 'ns:up_total{a="b"}'
    with pytest.raises(ValueError, match=r"^'rate\(up\[5m\]\)' is not a metric selector$"):
        with_matcher("rate(up[5m])", 'a="b"')
    with pytest.raises(ValueError, match=r"^'1up' is not a metric selector$"):
        with_matcher("1up", 'a="b"')


def test_the_defaults_fill_only_what_the_spec_leaves_out() -> None:
    assert settings({}) == {"kind": "counter", "window": "1h", "below": 0.3, "floor": 5}
    assert settings({"floor": 1, "metric": "m"}) == {
        "kind": "counter",
        "window": "1h",
        "below": 0.3,
        "floor": 1,
        "metric": "m",
    }


def test_only_a_mapping_makes_an_alert_a_baseline() -> None:
    assert is_baseline({"baseline": {}}) is True
    assert is_baseline({"baseline": {"metric": "m"}}) is True
    assert is_baseline({"baseline": None}) is False
    assert is_baseline({"baseline": "m"}) is False
    assert is_baseline({}) is False


def test_rule_names_follow_the_opt_in_rules() -> None:
    assert rule_names("b", "d", {}) == ["d", "b-zero"]
    assert rule_names("b", "d", {"above": 0, "empty_share": 0}) == ["d", "b-zero"]
    assert rule_names("b", "d", {"above": 2}) == ["d", "b-zero", "b-spike"]
    assert rule_names("b", "d", {"empty_share": 0.1}) == ["d", "b-zero", "b-empty"]
    assert rule_names("b", "d", {"above": 2, "empty_share": 0.1}) == [
        "d",
        "b-zero",
        "b-spike",
        "b-empty",
    ]


def test_zero_pages_only_a_paging_alert() -> None:
    assert [zero_severity(s) for s in ("page", "critical", "warning", "info")] == [
        "page",
        "critical",
        "critical",
        "critical",
    ]


def test_shares_print_as_percent_without_trailing_zeros() -> None:
    assert [percent(share) for share in (0.3, 0.125, 1, 0.05)] == ["30%", "12.5%", "100%", "5%"]


def test_legends_name_each_label_or_nothing() -> None:
    assert legend({}) == ""
    assert legend({"by": []}) == ""
    assert legend({"by": ["a", "b"]}) == "{{a}} {{b}}"


def test_a_target_is_a_table_only_when_instant() -> None:
    assert target("e", "l", "A") == {
        "datasource": SOURCE,
        "expr": "e",
        "legendFormat": "l",
        "refId": "A",
    }
    assert target("e", "l", "B", instant=True) == {
        "datasource": SOURCE,
        "expr": "e",
        "legendFormat": "l",
        "refId": "B",
        "instant": True,
        "format": "table",
    }


def test_row_ids_are_crc32_slots_ending_in_one() -> None:
    assert [row_id(name) for name in ("flow", "a", "b")] == [695201, 559071, 386811]
    assert share_panel("flow") == 695202


def test_expand_keeps_the_alert_fields_and_drops_the_condition() -> None:
    alert = {
        "name": "flow",
        "severity": "info",
        "runbook": "r",
        "baseline": {"metric": "m"},
        "threshold": 1,
        "op": ">",
        "no_data": "ok",
        "expr": "x",
        "summary": "S",
        "labels": {"a": "b"},
    }
    spec = {"metric": "m", "above": 2.5, "empty_share": 0.125, "window": "30m"}
    rules = expand(alert, spec, "base", "drop")
    common = {
        "runbook": "r",
        "labels": {"a": "b"},
        "for": "1h",
        "dashboard": {"uid": DASHBOARD_UID, "panel": share_panel("drop")},
    }
    assert [{k: v for k, v in rule.items() if k != "expr"} for rule in rules] == [
        {
            **common,
            "name": "drop",
            "title": "base: below usual level",
            "summary": "S",
            "severity": "info",
        },
        {
            **common,
            "name": "base-zero",
            "title": "base: stopped",
            "summary": "base: nothing in the last 30m, where this hour of the week usually "
            "has a flow",
            "severity": "critical",
        },
        {
            **common,
            "name": "base-spike",
            "title": "base: above usual level",
            "summary": "base: the last 30m is over 2.5 times the usual level for this hour of "
            "the week",
            "severity": "info",
        },
        {
            **common,
            "name": "base-empty",
            "title": "base: empty answers",
            "summary": "base: over 12.5% of the answers in the last 30m were ok_empty",
            "severity": "info",
        },
    ]
    full = settings(spec)
    assert [rule["expr"] for rule in rules] == [
        drop_expr(full),
        zero_expr(full),
        spike_expr(full),
        empty_expr(full),
    ]


def test_the_drop_summary_names_the_window_and_the_share() -> None:
    alert = {"name": "x", "runbook": "r", "baseline": {}}
    drop = expand(alert, {"metric": "m", "below": 0.45}, "flow", "flow-low")[0]
    assert drop["summary"] == (
        "flow: the last 1h is under 45% of the usual level for this hour of the week"
    )
    assert drop["title"] == "flow: below usual level"
    assert drop["dashboard"] == {"uid": DASHBOARD_UID, "panel": share_panel("flow-low")}
    keys = {"runbook", "for", "dashboard", "name", "title", "summary", "severity", "expr"}
    assert set(drop) == keys


def test_the_dashboard_is_pinned_panel_by_panel() -> None:
    board = dashboard([("flow", {"metric": "jobs_total"})])
    now, normal = current_of("", "jobs_total[1h]"), usual_of("", "jobs_total[1h]")
    rows = board.pop("panels")
    assert board == {
        "editable": False,
        "refresh": "1m",
        "schemaVersion": 39,
        "tags": ["ayeops"],
        "templating": {"list": []},
        "time": {"from": "now-7d", "to": "now"},
        "timezone": "browser",
        "title": {"en": "Flow baselines", "uk": "Базові рівні потоків"},
        "uid": "ayeops-baseline",
        "version": 1,
    }
    assert rows == [
        {
            "collapsed": False,
            "gridPos": {"h": 1, "w": 24, "x": 0, "y": 0},
            "id": 695201,
            "panels": [],
            "title": "flow",
            "type": "row",
        },
        {
            "datasource": SOURCE,
            "fieldConfig": {
                "defaults": {
                    "unit": "percentunit",
                    "min": 0,
                    "custom": {"thresholdsStyle": {"mode": "line+area"}},
                    "thresholds": {
                        "mode": "absolute",
                        "steps": [
                            {"color": "red", "value": None},
                            {"color": "green", "value": 0.3},
                        ],
                    },
                },
                "overrides": [],
            },
            "gridPos": {"h": 8, "w": 10, "x": 0, "y": 1},
            "id": 695202,
            "targets": [
                {
                    "datasource": SOURCE,
                    "expr": f"{now} / {normal}",
                    "legendFormat": "flow",
                    "refId": "A",
                }
            ],
            "title": {"en": "Share of the usual level", "uk": "Частка звичного рівня"},
            "type": "timeseries",
        },
        {
            "datasource": SOURCE,
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
            "gridPos": {"h": 8, "w": 10, "x": 10, "y": 1},
            "id": 695203,
            "targets": [
                {"datasource": SOURCE, "expr": now, "legendFormat": "actual", "refId": "A"},
                {"datasource": SOURCE, "expr": normal, "legendFormat": "usual", "refId": "B"},
            ],
            "title": {"en": "Actual and usual", "uk": "Фактично і зазвичай"},
            "type": "timeseries",
        },
        {
            "datasource": SOURCE,
            "fieldConfig": {
                "defaults": {
                    "max": 4,
                    "min": 0,
                    "thresholds": {
                        "mode": "absolute",
                        "steps": [{"color": "blue", "value": None}, {"color": "green", "value": 2}],
                    },
                },
                "overrides": [],
            },
            "gridPos": {"h": 8, "w": 4, "x": 20, "y": 1},
            "id": 695204,
            "options": {"displayMode": "basic", "orientation": "horizontal"},
            "targets": [
                {
                    "datasource": SOURCE,
                    "expr": known_of("", "jobs_total[1h]"),
                    "legendFormat": "flow",
                    "refId": "A",
                    "instant": True,
                    "format": "table",
                }
            ],
            "title": {
                "en": "Weeks of history, under 2 learning",
                "uk": "Тижнів історії, менше 2 навчання",
            },
            "type": "bargauge",
        },
    ]


def test_each_dashboard_row_takes_nine_units_and_its_own_ids() -> None:
    panels = dashboard([("b", SPEC), ("a", {"metric": "x"})])["panels"]
    assert [panel["gridPos"]["y"] for panel in panels] == [0, 1, 1, 1, 9, 10, 10, 10]
    assert [panel["id"] for panel in panels] == [
        559071,
        559072,
        559073,
        559074,
        386811,
        386812,
        386813,
        386814,
    ]


def test_spec_problems_name_each_path_rule_and_reason() -> None:
    spec = {
        "metric": "m",
        "by": ["a"],
        "kind": "gauge",
        "empty_share": 0.5,
        "skip": [{"match": {"a": "x"}}, {"match": {"z": "1", "y": "2", "a": "3"}}],
        "quiet": [{"from": "01:00", "to": "02:00"}, {"from": "03:00", "to": "03:00"}],
    }
    assert spec_problems("p", spec) == [
        (
            "p.skip.1",
            "baseline-skip",
            "skip matches y, z, which is not in by, so it matches nothing",
        ),
        ("p.quiet.1", "baseline-quiet", "the quiet window starts where it ends"),
        ("p", "baseline-empty", "empty_share counts answers by outcome, which needs a counter"),
    ]


def test_a_clean_spec_has_no_problems() -> None:
    assert spec_problems("p", {"metric": "m"}) == []
    assert spec_problems("p", {"metric": "m", "empty_share": 0.5}) == []
    assert spec_problems("p", {"metric": "m", "kind": "gauge", "empty_share": 0}) == []
    host = {"metric": "m", "by": ["host"], "skip": [{"match": {"project": "a", "group": "b"}}]}
    assert spec_problems("p", host) == []
    assert spec_problems("p", {"metric": "m", "skip": [{"match": {"a": "x"}}]}) == [
        ("p.skip.0", "baseline-skip", "skip matches a, which is not in by, so it matches nothing")
    ]
