from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import yaml

ROOT = Path(__file__).resolve().parent.parent


def module(name: str) -> dict[str, Any]:
    text = (ROOT / "src/ayeops/modules" / name / "module.yaml").read_text(encoding="utf-8")
    return cast(dict[str, Any], yaml.safe_load(text))


def throttled_alert() -> dict[str, Any]:
    return next(a for a in module("containers")["alerts"] if a["name"] == "container-throttled")


def test_the_cloudflare_exporter_has_room_for_its_poll_bursts() -> None:
    assert module("cloudflare")["services"]["cloudflare-exporter"]["cpus"] == 0.5


def test_throttling_alert_has_a_severity_and_a_runbook_that_exists() -> None:
    alert = throttled_alert()
    assert alert["severity"] == "warning"
    assert alert["runbook"] == "container-throttled"
    runbook = ROOT / "docs" / "runbooks" / "container-throttled.md"
    assert runbook.read_text(encoding="utf-8").startswith("# Container CPU throttled")


def test_throttling_alert_fires_on_average_use_over_80_percent_of_the_limit() -> None:
    expr = " ".join(throttled_alert()["expr"].split())
    assert expr == (
        "sum by (host, project, group, name) "
        '(rate(container_cpu_usage_seconds_total{name!=""}[30m])) '
        "/ max by (host, project, group, name) "
        '(container_spec_cpu_quota{name!=""} / container_spec_cpu_period{name!=""}) > bool 0.8'
    )
    assert "cfs_throttled" not in expr


def test_throttling_alert_fires_at_once_and_the_service_lists_its_metrics() -> None:
    assert throttled_alert()["for"] == "0m"
    service = module("containers")["services"]["cadvisor"]
    assert "container-throttled" in service["alerts"]
    assert "container_cpu_usage_seconds_total" in service["metrics"]
    assert {"container_spec_cpu_quota", "container_spec_cpu_period"} <= set(service["metrics"])
