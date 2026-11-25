from __future__ import annotations

import re
from typing import Any

import yaml

from ayeops.checks import service_alerts
from ayeops.registry import Module
from ayeops.render import (
    HOSTS_DOWN,
    NODE_UP,
    module_rules,
    rule_uid,
    telegram_template,
)
from tests.conftest import EXAMPLE_STAMP, manifest_from, render_once

BY = re.compile(r"\b(by|without)\s*\(([^)]*)\)")


def grouping_labels(expr: str) -> list[tuple[str, set[str]]]:
    return [
        (match.group(1), {label.strip() for label in match.group(2).split(",") if label.strip()})
        for match in BY.finditer(expr)
    ]


def test_grouping_labels_read_by_and_without_clauses() -> None:
    expr = "max by (host, device) (x) * on (host) sum without (instance) (y)"
    assert grouping_labels(expr) == [("by", {"host", "device"}), ("without", {"instance"})]


def rendered_module_rules(modules: dict[str, Module]) -> list[tuple[Module, dict[str, Any]]]:
    return [
        (module, rule)
        for module in modules.values()
        for alert in module.alerts
        for rule in module_rules(alert)
    ]


def test_every_module_alert_that_aggregates_by_host_keeps_project_and_group(
    modules: dict[str, Module],
) -> None:
    lost = [
        f"{module.name}/{alert['name']}: {kind} ({', '.join(sorted(labels))})"
        for module, alert in rendered_module_rules(modules)
        if "on (host) group_right ()" not in alert["expr"]
        for kind, labels in grouping_labels(alert["expr"])
        if (kind == "by" and "host" in labels and not {"project", "group"} <= labels)
        or (kind == "without" and labels & {"host", "project", "group"})
    ]
    assert not lost


def test_aggregations_by_host_are_still_there(modules: dict[str, Module]) -> None:
    by_host = [
        alert["name"]
        for _, alert in rendered_module_rules(modules)
        if any(kind == "by" and "host" in labels for kind, labels in grouping_labels(alert["expr"]))
    ]
    assert len(by_host) >= 25


def rendered_alerting(data: dict[str, Any], modules: dict[str, Module]) -> dict[str, Any]:
    tree = render_once(manifest_from(data), modules, EXAMPLE_STAMP)
    base = "central/grafana/provisioning/alerting/"
    return {
        "rules": yaml.safe_load(tree[base + "rules.yaml"]),
        "points": yaml.safe_load(tree[base + "contact-points.yaml"]),
    }


def all_rules(rules: dict[str, Any]) -> list[dict[str, Any]]:
    return [rule for group in rules["groups"] for rule in group["rules"]]


def test_every_rule_carries_a_scope_of_its_group_else_its_host(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    rules = all_rules(rendered_alerting(data, modules)["rules"])
    assert rules
    assert {rule["labels"]["scope"] for rule in rules} == {
        "{{ if $$labels.group }}{{ $$labels.group }}"
        "{{ else if $$labels.host }}{{ $$labels.host }}{{ end }}"
    }


def test_notifications_group_by_alert_and_scope_not_by_host(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    points = rendered_alerting(data, modules)["points"]
    assert [policy["group_by"] for policy in points["policies"]] == [
        ["grafana_folder", "alertname", "scope"]
    ]


def source_exprs(data: dict[str, Any], modules: dict[str, Module]) -> dict[str, str]:
    manifest = manifest_from(data)
    found = {
        rule_uid(alert["name"]): str(alert["expr"]).strip()
        for name in manifest.enabled_modules()
        for alert in modules[name].alerts
    }
    for service_name, service in manifest.active_services.items():
        for alert in service_alerts(manifest, service_name, service):
            found[rule_uid(alert["name"])] = str(alert["expr"]).strip()
    return found


def rule_expr(rule: dict[str, Any]) -> str:
    return str(rule["data"][0]["model"]["expr"]).replace("$$", "$")


def test_a_down_host_holds_back_every_other_alert_of_that_host_in_the_rendered_rules(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    alerting = rendered_alerting(data, modules)
    rules = {rule["uid"]: rule for rule in all_rules(alerting["rules"])}
    sources = source_exprs(data, modules)
    host_down = rules.pop("aye-host-down")
    assert rule_expr(host_down) == sources["aye-host-down"]
    assert f"1 - (max by (host) ({NODE_UP})" in rule_expr(host_down)
    assert f"max by (host) ({NODE_UP}) == 0" == HOSTS_DOWN
    assert len(rules) > 40
    for uid, rule in rules.items():
        inner = sources[uid]
        assert rule_expr(rule) == f"({inner}) unless on (host) ({HOSTS_DOWN}) or 0 * ({inner})"
    assert rules["aye-target-down"]["noDataState"] == "Alerting"
    points = alerting["points"]
    receivers = {point["name"] for point in points["contactPoints"]}
    routes = list(points["policies"])
    routes += [route for policy in routes for route in policy.get("routes", [])]
    assert {route["receiver"] for route in routes} <= receivers
    assert not [route for route in routes if set(route) & {"mute_time_intervals", "matchers"}]
    assert "muteTimes" not in points


def test_without_node_metrics_there_is_no_host_down_and_nothing_is_held_back(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    for host in data["hosts"].values():
        host["modules"].pop("node", None)
    rules = all_rules(rendered_alerting(data, modules)["rules"])
    assert rules
    assert "aye-host-down" not in {rule["uid"] for rule in rules}
    assert not [rule["uid"] for rule in rules if HOSTS_DOWN in rule_expr(rule)]


def test_the_telegram_message_of_a_group_names_the_count_the_scope_and_the_hosts() -> None:
    template = telegram_template("en")
    firing = "{{ if .Alerts.Firing }}🔴 <b>{{ len .Alerts.Firing }} x "
    assert "{{ if gt (len .Alerts) 1 }}" + firing in template
    assert "x {{ .CommonLabels.alertname }}</b>" in template
    assert "{{ if .GroupLabels.scope }} · {{ .GroupLabels.scope }}{{ end }}" in template
    assert '{{ template "aye.links" index .Alerts.Firing 0 }}' in template
    assert '{{ else }}{{ template "aye.hosts" .Alerts.Firing }}' in template
    assert '{{ range slice . 1 }}, {{ template "aye.host" . }}{{ end }}' in template
    assert "{{ if .Alerts.Resolved }}✅ <b>{{ len .Alerts.Resolved }} x " in template
    assert '{{ .Labels.host }}{{ template "aye.links" . }}' in template
    assert "$" not in template
