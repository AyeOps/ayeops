from __future__ import annotations

from typing import Any

from ayeops.registry import Module
from ayeops.render import service_owners
from ayeops.rules import check_service, module_catalog_services, port_clashes
from tests.conftest import ROOT, check_once, manifest_from, rules_hit


def web(data: dict[str, Any]) -> dict[str, Any]:
    service: dict[str, Any] = data["services"]["web"]
    return service


def test_example_manifest_passes_every_rule(data: dict[str, Any]) -> None:
    assert rules_hit(data) == set()


def test_service_without_description_is_rejected(data: dict[str, Any]) -> None:
    del web(data)["description"]
    assert "service-description" in rules_hit(data)


def test_blank_description_is_rejected(data: dict[str, Any]) -> None:
    web(data)["description"] = "   "
    assert "service-description" in rules_hit(data)


def test_service_without_metrics_is_rejected(data: dict[str, Any]) -> None:
    del web(data)["metrics"]
    assert "service-metrics" in rules_hit(data)


def test_empty_metric_list_is_rejected(data: dict[str, Any]) -> None:
    web(data)["metrics"] = []
    assert "service-metrics" in rules_hit(data)


def test_metrics_none_requires_a_reason(data: dict[str, Any]) -> None:
    web(data)["metrics"] = {"none": ""}
    assert "service-metrics" in rules_hit(data)


def test_metrics_none_with_a_reason_is_accepted(data: dict[str, Any]) -> None:
    web(data)["metrics"] = {"none": "static files, the probe is the only signal"}
    assert rules_hit(data) == set()


def test_service_without_alert_is_rejected(data: dict[str, Any]) -> None:
    web(data)["alerts"] = []
    assert "service-alerts" in rules_hit(data)


def test_alert_without_severity_is_rejected(data: dict[str, Any]) -> None:
    del web(data)["alerts"][0]["severity"]
    assert "alert-severity" in rules_hit(data)


def test_alert_without_runbook_is_rejected(data: dict[str, Any]) -> None:
    del web(data)["alerts"][0]["runbook"]
    assert "alert-runbook" in rules_hit(data)


def test_alerts_need_a_runbook_base_url(data: dict[str, Any]) -> None:
    del data["runbook_base_url"]
    assert "alert-runbook" in rules_hit(data)


def test_service_without_log_source_is_rejected(data: dict[str, Any]) -> None:
    del web(data)["logs"]
    assert "service-logs" in rules_hit(data)


def test_service_without_status_mark_is_rejected(data: dict[str, Any]) -> None:
    del web(data)["status"]
    assert "service-status" in rules_hit(data)


def test_internal_status_mark_is_accepted(data: dict[str, Any]) -> None:
    web(data)["status"] = {"internal": "only the team uses it"}
    assert rules_hit(data) == set()


def test_status_mark_cannot_be_both_public_and_internal(data: dict[str, Any]) -> None:
    web(data)["status"] = {"public": "Website", "internal": "both"}
    assert rules_hit(data) == {"schema"}


def test_service_nobody_provides_is_a_dead_entry(data: dict[str, Any]) -> None:
    ghost = dict(web(data), alerts=[dict(web(data)["alerts"][0], name="ghost-down")])
    data["services"]["ghost"] = ghost
    assert rules_hit(data) == {"service-provided"}


def test_service_on_the_wrong_host_is_a_dead_entry(data: dict[str, Any]) -> None:
    web(data)["host"] = "edge"
    assert rules_hit(data) == {"service-provided"}


def test_service_provided_by_a_compose_file_is_alive(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    assert "web" not in service_owners(manifest_from(data), "central", modules)
    assert rules_hit(data) == set()


def test_service_provided_by_an_enabled_module_is_alive(data: dict[str, Any]) -> None:
    alert = dict(web(data)["alerts"][0], name="grafana-down")
    data["services"]["grafana"] = dict(web(data), alerts=[alert])
    assert rules_hit(data) == set()


def test_missing_compose_file_is_rejected(data: dict[str, Any]) -> None:
    data["hosts"]["central"]["compose"] = ["apps/missing.yaml"]
    assert rules_hit(data) == {"compose-file", "service-provided", "flow-endpoint"}


def test_cloud_dependency_without_limit_is_rejected(data: dict[str, Any]) -> None:
    del web(data)["cloud"][0]["limit"]
    assert rules_hit(data) == {"cloud-limit"}


def test_cloud_dependency_without_on_exceed_is_rejected(data: dict[str, Any]) -> None:
    del web(data)["cloud"][0]["on_exceed"]
    assert rules_hit(data) == {"cloud-limit"}


def test_alert_names_are_unique_across_modules_and_services(data: dict[str, Any]) -> None:
    web(data)["alerts"][0]["name"] = "host-down"
    assert rules_hit(data) == {"alert-unique"}


def test_unknown_module_is_rejected(data: dict[str, Any]) -> None:
    data["hosts"]["edge"]["modules"]["teleport"] = {}
    assert rules_hit(data) == {"module-known"}


def test_module_settings_are_validated_against_the_module_schema(data: dict[str, Any]) -> None:
    data["hosts"]["edge"]["modules"]["node"]["batery"] = True
    assert rules_hit(data) == {"module-settings"}


def test_grafana_needs_a_metrics_server_on_the_same_host(data: dict[str, Any]) -> None:
    data["hosts"]["edge"]["modules"]["grafana"] = {}
    del data["hosts"]["central"]["modules"]["grafana"]
    assert "module-requires" in rules_hit(data)


def test_exporters_need_the_metrics_module(data: dict[str, Any]) -> None:
    del data["hosts"]["edge"]["modules"]["metrics"]
    assert rules_hit(data) == {"module-requires"}


def test_grafana_runs_on_one_host_only(data: dict[str, Any]) -> None:
    data["hosts"]["edge"]["modules"]["metrics"] = {"role": "server"}
    data["hosts"]["edge"]["modules"]["grafana"] = {}
    assert rules_hit(data) == {"grafana-single"}


def test_agent_without_remote_write_is_rejected(data: dict[str, Any]) -> None:
    del data["hosts"]["edge"]["modules"]["metrics"]["remote_write"]
    assert rules_hit(data) == {"remote-write-target"}


def test_remote_write_target_must_expose_the_metrics_port(data: dict[str, Any]) -> None:
    data["hosts"]["central"]["modules"]["metrics"]["expose"] = []
    assert rules_hit(data) == {"remote-write-target", "sentinel-digest"}


def test_remote_write_target_must_have_an_address(data: dict[str, Any]) -> None:
    del data["hosts"]["central"]["modules"]["ingress"]
    del data["hosts"]["central"]["modules"]["auth"]
    del data["hosts"]["central"]["modules"]["portal"]
    del data["hosts"]["central"]["address"]
    del data["hosts"]["edge"]["modules"]["logs"]
    data["hosts"]["edge"]["modules"]["offsite"]["target"] = {
        "url": "s3:http://10.8.0.1:3900/acme-offsite/edge"
    }
    assert rules_hit(data) == {"remote-write-target"}


def test_remote_write_target_must_be_a_known_host(data: dict[str, Any]) -> None:
    data["hosts"]["edge"]["modules"]["metrics"]["remote_write"] = {"host": "nowhere"}
    assert rules_hit(data) == {"remote-write-target"}


def test_exposed_ports_need_an_explicit_bind_address(data: dict[str, Any]) -> None:
    del data["hosts"]["central"]["modules"]["ingress"]
    del data["hosts"]["central"]["modules"]["auth"]
    del data["hosts"]["central"]["modules"]["portal"]
    del data["hosts"]["central"]["public_bind"]
    assert rules_hit(data) == {"expose-bind"}


def test_memory_limits_must_fit_the_host_memory(data: dict[str, Any]) -> None:
    data["hosts"]["central"]["memory"] = "1g"
    assert rules_hit(data) == {"memory-budget"}


def test_memory_override_counts_towards_the_budget(data: dict[str, Any]) -> None:
    data["hosts"]["edge"]["memory"] = "5g"
    assert rules_hit(data) == set()
    data["hosts"]["edge"]["modules"]["containers"]["resources"] = {"cadvisor": {"memory": "2g"}}
    assert rules_hit(data) == {"memory-budget"}


def test_unknown_top_level_key_is_a_schema_error(data: dict[str, Any]) -> None:
    data["extra"] = True
    assert rules_hit(data) == {"schema"}


def test_findings_point_at_the_offending_path(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    del web(data)["logs"]
    paths = [finding.path for finding in check_once(manifest_from(data), modules)]
    assert paths == ["services.web"]


def test_every_builtin_module_service_passes_the_catalog_rules(
    modules: dict[str, Module],
) -> None:
    findings = [
        finding
        for path, service in module_catalog_services(modules).items()
        for finding in check_service(path, service)
    ]
    assert findings == []


def test_every_builtin_module_service_references_known_alerts(
    modules: dict[str, Module],
) -> None:
    known = {alert["name"] for module in modules.values() for alert in module.alerts}
    referenced = {
        alert
        for module in modules.values()
        for entry in module.catalog.values()
        for alert in entry["alerts"]
    }
    assert referenced - known == set()


def test_every_builtin_alert_has_a_runbook_page(modules: dict[str, Module]) -> None:
    runbooks = {alert["runbook"] for module in modules.values() for alert in module.alerts}
    pages = ROOT / "docs" / "runbooks"
    assert sorted(r for r in runbooks if not (pages / f"{r}.md").is_file()) == []


def test_two_services_on_one_host_port_and_address_clash() -> None:
    services = {
        "cadvisor": {"ports": ["127.0.0.1:8080:8080"]},
        "portal": {"ports": ["127.0.0.1:8080:8080"]},
        "grafana": {"ports": ["10.8.0.1:3000:3000"]},
    }
    assert [f.rule for f in port_clashes("hosts.box", services)] == ["port-clash"]


def test_the_same_port_on_different_addresses_does_not_clash() -> None:
    services = {
        "a": {"ports": ["127.0.0.1:8080:8080"]},
        "b": {"ports": ["10.8.0.1:8080:8080"]},
    }
    assert port_clashes("hosts.box", services) == []


def test_portal_and_cadvisor_share_a_host_without_clashing(data: dict[str, Any]) -> None:
    data["hosts"]["central"]["modules"]["containers"] = {}
    assert "port-clash" not in rules_hit(data)
