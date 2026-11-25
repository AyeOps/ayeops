from __future__ import annotations

import json
from typing import Any

import pytest
import yaml

from ayeops.manifest import load_manifest
from ayeops.registry import Module
from ayeops.status import push_components, reporter_context
from tests.conftest import manifest_from, pinned, render_once, rules_hit, tag_of
from tests.test_status import FIXTURE_MANIFEST
from tests.test_status_reporter import SCRIPT, r


def central_modules(data: dict[str, Any]) -> dict[str, Any]:
    modules: dict[str, Any] = data["hosts"]["central"]["modules"]
    return modules


def reporter_service(data: dict[str, Any], modules: dict[str, Module]) -> dict[str, Any]:
    tree = render_once(manifest_from(data), modules)
    service: dict[str, Any] = yaml.safe_load(tree["central/compose.yaml"])["services"][
        "status-reporter"
    ]
    return service


def test_the_example_runs_the_reporter_next_to_grafana(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    tree = render_once(manifest_from(data), modules)
    service = yaml.safe_load(tree["central/compose.yaml"])["services"]["status-reporter"]
    assert tag_of(service["image"]) == pinned("status", "python")
    assert service["mem_limit"] == "48m"
    assert service["user"] == "65534:65534"
    assert service["read_only"] is True
    assert "ports" not in service
    assert service["depends_on"] == {
        "grafana": {"condition": "service_healthy"},
        "grafana-reader": {"condition": "service_healthy"},
    }
    assert service["group_add"] == ["10470"]
    assert "grafana-reader:/run/grafana-reader:ro" in service["volumes"]
    environment = service["environment"]
    assert environment["AYE_STATUS_TENANT"] == "acme"
    assert environment["AYE_STATUS_URL"] == "https://status.acme.example/api/push"
    assert environment["AYE_STATUS_GRAFANA"] == "http://grafana:3000"
    assert environment["AYE_STATUS_INTERVAL"] == "1m"
    assert environment["AYE_STATUS_TIMEOUT"] == "10s"
    assert environment["AYE_STATUS_PUSH_SECRET"].startswith("${AYE_STATUS_PUSH_SECRET:?")
    assert environment["AYE_GRAFANA_READER_TOKEN_FILE"] == "/run/grafana-reader/token"
    assert "AYE_GRAFANA_ADMIN" not in json.dumps(service)
    assert json.loads(environment["AYE_STATUS_COMPONENTS"]) == [
        {"id": "mcp-endpoint", "services": ["mcp"]}
    ]
    assert tree["central/status/status_reporter.py"] == SCRIPT.read_text(encoding="utf-8").replace(
        "\r\n", "\n"
    )
    assert "AYE_STATUS_PUSH_SECRET=" in tree["central/.env.example"]
    jobs = yaml.safe_load(tree["central/vmagent/scrape.yaml"])["scrape_configs"]
    assert {
        "job_name": "status-reporter",
        "static_configs": [{"targets": ["status-reporter:9136"]}],
    } in jobs
    assert "status-reporter" not in yaml.safe_load(tree["edge/compose.yaml"])["services"]


def test_the_rendered_environment_starts_the_script(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    environment = dict(reporter_service(data, modules)["environment"])
    environment["AYE_STATUS_PUSH_SECRET"] = "secret"
    config = r.config_from(environment)
    assert config.components == (r.Component("mcp-endpoint", ("mcp",)),)
    assert (config.interval_seconds, config.timeout_seconds) == (60, 10)


def test_the_example_with_the_reporter_passes_the_check(data: dict[str, Any]) -> None:
    assert "status" in central_modules(data)
    assert rules_hit(data) == set()


def test_only_components_waiting_for_pushes_are_pushed_with_all_their_services() -> None:
    assert push_components(load_manifest(FIXTURE_MANIFEST)) == [
        {"id": "api", "services": ["api", "api-worker"]},
        {"id": "payments-checkout", "services": ["checkout"]},
    ]


def test_without_the_module_there_is_no_reporter_context() -> None:
    assert reporter_context(load_manifest(FIXTURE_MANIFEST), None) == {}


def test_the_reporter_needs_a_status_page(data: dict[str, Any]) -> None:
    del data["status_page"]
    assert rules_hit(data) == {"status-reporter"}


def test_the_reporter_needs_a_component_that_waits_for_pushes(data: dict[str, Any]) -> None:
    data["services"]["mcp"]["status"]["probe"] = "https://mcp.acme.example"
    assert rules_hit(data) == {"status-reporter"}


def test_the_reporter_reads_grafana_as_a_declared_reader_consumer(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    assert modules["status"].catalog["status-reporter"]["grafana_reader"] is True
    assert "grafana-reader-mount" not in rules_hit(data)


def test_the_reporter_runs_on_the_grafana_host(data: dict[str, Any]) -> None:
    data["hosts"]["edge"]["modules"]["status"] = central_modules(data).pop("status")
    assert "module-requires" in rules_hit(data)


@pytest.mark.parametrize("settings", [{"interval": "5m"}, {"timeout": "30s"}, {"url": "x"}])
def test_settings_outside_the_push_window_are_refused(
    data: dict[str, Any], settings: dict[str, Any]
) -> None:
    central_modules(data)["status"] = settings
    assert rules_hit(data) == {"module-settings"}


def test_two_minute_interval_reaches_the_container(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    central_modules(data)["status"] = {"interval": "2m", "timeout": "5s"}
    environment = reporter_service(data, modules)["environment"]
    assert (environment["AYE_STATUS_INTERVAL"], environment["AYE_STATUS_TIMEOUT"]) == ("2m", "5s")


def test_the_push_failing_alert_fires_on_silence_and_links_its_runbook(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    tree = render_once(manifest_from(data), modules)
    groups = yaml.safe_load(tree["central/grafana/provisioning/alerting/rules.yaml"])["groups"]
    rules = {rule["uid"]: rule for group in groups for rule in group["rules"]}
    failing = rules["aye-status-push-failing"]
    assert failing["noDataState"] == "Alerting"
    assert failing["annotations"]["runbook_url"].endswith("/docs/runbooks/status-push-failing.md")
    assert "aye-status-query-failing" in rules
