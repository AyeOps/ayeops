from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from ayeops.logs import compose_project
from ayeops.manifest import Manifest
from ayeops.registry import Module
from ayeops.render import host_compose
from tests.conftest import manifest_from, render_once, rules_hit


def compose_of(data: dict[str, Any], host: str, modules: dict[str, Module]) -> dict[str, Any]:
    return host_compose(manifest_from(data), host, modules)


def tree_of(data: dict[str, Any], modules: dict[str, Module]) -> dict[str, str]:
    return render_once(manifest_from(data), modules)


def edge_logs(data: dict[str, Any]) -> dict[str, Any]:
    settings: dict[str, Any] = data["hosts"]["edge"]["modules"]["logs"]
    return settings


def test_server_runs_loki_and_alloy_and_agent_runs_only_alloy(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    central = compose_of(data, "central", modules)
    edge = compose_of(data, "edge", modules)
    assert {"loki", "alloy"} <= set(central["services"])
    assert "loki-data" in central["volumes"]
    assert "alloy" in edge["services"]
    assert "loki" not in edge["services"]
    assert "loki-data" not in edge["volumes"]


def test_loki_listens_on_the_private_address_and_alloy_on_localhost(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    central = compose_of(data, "central", modules)["services"]
    assert central["loki"]["ports"] == ["10.8.0.1:3100:3100"]
    assert central["alloy"]["ports"] == ["127.0.0.1:12345:12345"]


def test_loki_without_expose_stays_on_localhost(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    data["hosts"]["central"]["modules"]["logs"]["expose"] = []
    data["hosts"]["edge"]["modules"]["logs"]["push"] = {"url": "https://logs.example/push"}
    central = compose_of(data, "central", modules)["services"]
    assert central["loki"]["ports"] == ["127.0.0.1:3100:3100"]


def test_retention_reaches_the_loki_compactor(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    data["hosts"]["central"]["modules"]["logs"]["retention"] = "14d"
    config = yaml.safe_load(tree_of(data, modules)["central/logs/loki.yaml"])
    assert config["limits_config"]["retention_period"] == "14d"
    assert config["compactor"]["retention_enabled"] is True
    assert config["server"]["log_level"] == "warn"


def test_loki_config_is_written_only_on_the_server(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    tree = tree_of(data, modules)
    assert "central/logs/loki.yaml" in tree
    assert "edge/logs/loki.yaml" not in tree
    assert "edge/logs/config.alloy" in tree


def test_agent_pushes_to_the_central_address_with_its_host_label(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    config = tree_of(data, modules)["edge/logs/config.alloy"]
    assert 'url = "http://10.8.0.1:3100/loki/api/v1/push"' in config
    assert 'external_labels = {host = "edge", project = "acme"}' in config


def test_server_pushes_to_its_own_loki(data: dict[str, Any], modules: dict[str, Module]) -> None:
    config = tree_of(data, modules)["central/logs/config.alloy"]
    assert 'url = "http://loki:3100/loki/api/v1/push"' in config


def test_push_url_is_used_as_given(data: dict[str, Any], modules: dict[str, Module]) -> None:
    edge_logs(data)["push"] = {"url": "https://logs.example/loki/api/v1/push"}
    config = tree_of(data, modules)["edge/logs/config.alloy"]
    assert 'url = "https://logs.example/loki/api/v1/push"' in config


def test_alloy_ships_only_the_compose_projects_declared_for_the_host(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    tree = tree_of(data, modules)
    assert 'regex         = "acme-apps|acme-central"' in tree["central/logs/config.alloy"]
    assert 'regex         = "acme-edge"' in tree["edge/logs/config.alloy"]


def test_declared_compose_name_overrides_the_file(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    data["hosts"]["central"]["compose"] = [{"file": "apps/web.compose.yaml", "name": "shop"}]
    config = tree_of(data, modules)["central/logs/config.alloy"]
    assert 'regex         = "acme-central|shop"' in config


def test_compose_file_without_name_ships_its_directory_project(tmp_path: Path) -> None:
    (tmp_path / "Shop.App").mkdir()
    (tmp_path / "Shop.App" / "compose.yaml").write_text("services: {}\n", encoding="utf-8")
    (tmp_path / "named.yaml").write_text("name: billing\nservices: {}\n", encoding="utf-8")
    manifest = Manifest({"project": "acme", "hosts": {}}, tmp_path)
    assert compose_project(manifest, "Shop.App/compose.yaml", "") == "shopapp"
    assert compose_project(manifest, "named.yaml", "") == "billing"
    assert compose_project(manifest, "named.yaml", "declared") == "declared"


def test_projects_setting_replaces_the_declared_list_but_keeps_the_own_stack(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    data["hosts"]["central"]["modules"]["logs"]["projects"] = ["billing", "acme-apps"]
    config = tree_of(data, modules)["central/logs/config.alloy"]
    assert 'regex         = "acme-apps|acme-central|billing"' in config


def test_alloy_maps_docker_metadata_to_the_four_labels(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    config = tree_of(data, modules)["central/logs/config.alloy"]
    for label in ("compose_project", "compose_service", "container"):
        assert f'target_label  = "{label}"' in config
    assert 'values = ["running"]' in config
    assert "host = " in config


def test_journald_is_shipped_only_where_enabled_and_drops_container_lines(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    tree = tree_of(data, modules)
    edge = tree["edge/logs/config.alloy"]
    assert 'loki.source.journal "system"' in edge
    assert '"__journal_container_id"' in edge
    assert 'loki.source.journal "system"' not in tree["central/logs/config.alloy"]
    volumes = compose_of(data, "edge", modules)["services"]["alloy"]["volumes"]
    assert "/var/log/journal:/var/log/journal:ro" in volumes
    central = compose_of(data, "central", modules)["services"]["alloy"]["volumes"]
    assert "/var/log/journal:/var/log/journal:ro" not in central


def test_alloy_reads_the_docker_socket_read_only(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    volumes = compose_of(data, "edge", modules)["services"]["alloy"]["volumes"]
    assert "/var/run/docker.sock:/var/run/docker.sock:ro" in volumes


def test_vmagent_scrapes_loki_on_the_server_and_alloy_everywhere(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    tree = tree_of(data, modules)
    jobs = {
        host: {
            j["job_name"]
            for j in yaml.safe_load(tree[f"{host}/vmagent/scrape.yaml"])["scrape_configs"]
        }
        for host in ("central", "edge")
    }
    assert {"loki", "alloy"} <= jobs["central"]
    assert "alloy" in jobs["edge"]
    assert "loki" not in jobs["edge"]


def test_grafana_gets_a_loki_datasource_next_to_loki(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    tree = tree_of(data, modules)
    source = yaml.safe_load(tree["central/grafana/provisioning/datasources/loki.yaml"])
    assert source["datasources"] == [
        {
            "name": "Loki",
            "uid": "loki",
            "type": "loki",
            "access": "proxy",
            "url": "http://loki:3100",
            "isDefault": False,
            "editable": False,
            "jsonData": {"maxLines": 1000},
        }
    ]


def test_grafana_reaches_loki_on_another_host_by_its_address(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    data["hosts"]["central"]["modules"]["logs"] = {"role": "agent", "push": {"host": "edge"}}
    data["hosts"]["edge"]["address"] = "10.8.0.2"
    data["hosts"]["edge"]["public_bind"] = "10.8.0.2"
    edge_logs(data).clear()
    edge_logs(data)["expose"] = [3100]
    tree = tree_of(data, modules)
    source = yaml.safe_load(tree["central/grafana/provisioning/datasources/loki.yaml"])
    assert source["datasources"][0]["url"] == "http://10.8.0.2:3100"
    assert rules_hit(data) == set()


def test_no_loki_datasource_without_a_logs_server(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    del data["hosts"]["central"]["modules"]["logs"]
    del data["hosts"]["edge"]["modules"]["logs"]
    tree = tree_of(data, modules)
    assert "central/grafana/provisioning/datasources/loki.yaml" not in tree
    assert "central/grafana/provisioning/datasources/victoriametrics.yaml" in tree


def test_logs_alerts_are_provisioned(data: dict[str, Any], modules: dict[str, Module]) -> None:
    rules = yaml.safe_load(
        tree_of(data, modules)["central/grafana/provisioning/alerting/rules.yaml"]
    )
    group = next(g for g in rules["groups"] if g["name"] == "logs")
    assert {r["uid"] for r in group["rules"]} == {
        "aye-logs-ingestion-errors",
        "aye-logs-shipping-failing",
    }


def test_agent_without_push_is_rejected(data: dict[str, Any]) -> None:
    del edge_logs(data)["push"]
    assert rules_hit(data) == {"logs-push-target"}


def test_push_target_must_expose_the_loki_port(data: dict[str, Any]) -> None:
    data["hosts"]["central"]["modules"]["logs"]["expose"] = []
    assert rules_hit(data) == {"logs-push-target"}


def test_push_target_must_run_a_logs_server(data: dict[str, Any]) -> None:
    data["hosts"]["central"]["modules"]["logs"]["role"] = "agent"
    data["hosts"]["central"]["modules"]["logs"]["push"] = {"url": "https://logs.example/push"}
    assert rules_hit(data) == {"logs-push-target"}


def test_push_target_must_be_a_known_host(data: dict[str, Any]) -> None:
    edge_logs(data)["push"] = {"host": "nowhere"}
    assert rules_hit(data) == {"logs-push-target"}


def test_push_target_must_have_an_address(data: dict[str, Any]) -> None:
    for name in ("ingress", "auth", "portal"):
        del data["hosts"]["central"]["modules"][name]
    del data["hosts"]["central"]["address"]
    data["hosts"]["edge"]["modules"]["metrics"]["remote_write"] = {"url": "http://vm.example"}
    data["hosts"]["edge"]["modules"]["offsite"]["target"] = {
        "url": "s3:http://10.8.0.1:3900/acme-offsite/edge"
    }
    assert rules_hit(data) == {"logs-push-target"}


def test_logs_need_metrics_on_the_same_host(data: dict[str, Any]) -> None:
    for name in ("metrics", "node", "containers", "smart", "gpu", "network", "disk-forecast"):
        del data["hosts"]["edge"]["modules"][name]
    assert rules_hit(data) == {"module-requires"}


def test_unknown_logs_setting_is_rejected(data: dict[str, Any]) -> None:
    edge_logs(data)["retention_days"] = 7
    assert rules_hit(data) == {"module-settings"}


def test_project_names_follow_compose_naming(data: dict[str, Any]) -> None:
    edge_logs(data)["projects"] = ["Bad Name"]
    assert rules_hit(data) == {"module-settings"}
