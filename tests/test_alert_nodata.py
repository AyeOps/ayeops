from __future__ import annotations

from pathlib import Path
from typing import Any

from ayeops.registry import Module, load_modules
from ayeops.rules import module_alert_traps
from tests.conftest import check_once, manifest_from, rules_hit


def web_alert(data: dict[str, Any]) -> dict[str, Any]:
    alert: dict[str, Any] = data["services"]["web"]["alerts"][0]
    return alert


def test_a_service_alert_filtering_without_bool_fires_on_no_data(data: dict[str, Any]) -> None:
    web_alert(data)["expr"] = 'time() - max(container_last_seen{name=~"web"}) > 120'
    findings = [
        f
        for f in check_once(manifest_from(data), load_modules())
        if f.rule == "alert-filter-nodata"
    ]
    assert [f.path for f in findings] == ["services.web.alerts.0"]
    assert "> bool" in findings[0].message


def test_label_matchers_and_groupings_are_not_comparisons(data: dict[str, Any]) -> None:
    web_alert(data)["expr"] = 'sum by (code) (rate(http_requests_total{code!="200"}[5m]))'
    assert "alert-filter-nodata" not in rules_hit(data)


def test_a_grouped_filter_is_still_caught(data: dict[str, Any]) -> None:
    web_alert(data)["expr"] = 'sum by (code) (rate(http_requests_total{code!="200"}[5m])) > 0'
    assert "alert-filter-nodata" in rules_hit(data)


def test_bool_comparison_passes(data: dict[str, Any]) -> None:
    web_alert(data)["expr"] = "time() - max(container_last_seen) > bool 120"
    assert "alert-filter-nodata" not in rules_hit(data)


def module_with(alert: dict[str, Any]) -> Module:
    spec = {"name": "demo", "alerts": [{"name": "demo-stale", **alert}]}
    return Module("demo", Path(), spec)


def test_a_module_alert_filtering_with_no_data_alerting_is_caught() -> None:
    findings = module_alert_traps(module_with({"expr": "x > 5", "no_data": "alerting"}))
    assert [(f.path, f.rule) for f in findings] == [
        ("modules.demo.alerts.demo-stale", "alert-filter-nodata")
    ]


def test_a_module_alert_filtering_with_no_data_ok_is_fine() -> None:
    assert module_alert_traps(module_with({"expr": "x > 5", "no_data": "ok"})) == []
    assert module_alert_traps(module_with({"expr": "x > 5"})) == []


def test_every_builtin_module_alert_is_safe_on_no_data(modules: dict[str, Module]) -> None:
    assert [f for module in modules.values() for f in module_alert_traps(module)] == []
