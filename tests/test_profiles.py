from __future__ import annotations

from typing import Any

import yaml

from ayeops.manifest import expand_profiles, schema_errors
from ayeops.registry import Module
from ayeops.render import host_compose
from tests.conftest import check_once, manifest_from, render_once, rules_hit, warnings_hit


def worker_profile() -> dict[str, Any]:
    return {
        "arch": "amd64",
        "zone": "datacenter",
        "units": {"backup": "backup.timer"},
        "modules": {
            "metrics": {
                "role": "agent",
                "scrape_interval": "60s",
                "remote_write": {"host": "central"},
            },
            "logs": {"role": "agent", "push": {"host": "central"}},
            "node": {"reboot": True},
            "containers": {},
            "smart": {},
            "network": {"interfaces": ["eth0", "eth1"]},
        },
    }


def with_workers(data: dict[str, Any]) -> dict[str, Any]:
    data["profiles"] = {"worker": worker_profile()}
    data["hosts"]["worker-1"] = {"profile": "worker"}
    data["hosts"]["worker-2"] = {"profile": "worker", "address": "10.8.0.12"}
    return data


def findings_of(data: dict[str, Any], modules: dict[str, Module]) -> set[tuple[str, str]]:
    return {(f.path, f.rule) for f in check_once(manifest_from(data), modules)}


def vmagent_command(data: dict[str, Any], host: str, modules: dict[str, Module]) -> list[str]:
    services = host_compose(manifest_from(data), host, modules)["services"]
    return list(services["vmagent"]["command"])


def test_a_fleet_of_profiled_hosts_passes_the_check(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    assert findings_of(with_workers(data), modules) == set()


def test_a_host_without_own_fields_gets_the_whole_profile(data: dict[str, Any]) -> None:
    host = manifest_from(with_workers(data)).hosts["worker-1"]
    assert host == {"profile": "worker", **worker_profile()}


def test_maps_merge_at_every_depth_with_the_host_winning(data: dict[str, Any]) -> None:
    with_workers(data)["hosts"]["worker-1"]["modules"] = {
        "metrics": {
            "scrape_interval": "15s",
            "remote_write": {"url": "http://vm:8428/api/v1/write"},
        },
        "gpu": {},
    }
    modules = manifest_from(data).hosts["worker-1"]["modules"]
    assert modules["metrics"] == {
        "role": "agent",
        "scrape_interval": "15s",
        "remote_write": {"host": "central", "url": "http://vm:8428/api/v1/write"},
    }
    assert set(modules) == {"metrics", "logs", "node", "containers", "smart", "network", "gpu"}


def test_lists_and_scalars_of_the_host_replace_the_profile(data: dict[str, Any]) -> None:
    worker = with_workers(data)["hosts"]["worker-1"]
    worker.update({"arch": "arm64", "modules": {"network": {"interfaces": ["eno1"]}}})
    host = manifest_from(data).hosts["worker-1"]
    assert host["arch"] == "arm64"
    assert host["modules"]["network"] == {"interfaces": ["eno1"]}
    assert host["zone"] == "datacenter"


def test_an_empty_module_value_keeps_the_profile_settings(data: dict[str, Any]) -> None:
    with_workers(data)["hosts"]["worker-1"]["modules"] = {"node": None}
    assert manifest_from(data).host_modules("worker-1")["node"] == {"reboot": True}


def test_false_drops_a_module_the_profile_enables(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    with_workers(data)["hosts"]["worker-2"]["modules"] = {"smart": False}
    manifest = manifest_from(data)
    assert "smart" not in manifest.host_modules("worker-2")
    assert "smart" in manifest.host_modules("worker-1")
    tree = render_once(manifest, modules)
    assert "smartctl-exporter" not in yaml.safe_load(tree["worker-2/compose.yaml"])["services"]
    assert findings_of(data, modules) == set()


def test_false_turns_off_a_module_on_a_host_without_a_profile(data: dict[str, Any]) -> None:
    data["hosts"]["edge"]["modules"]["gpu"] = False
    assert schema_errors(data) == []
    assert "gpu" not in manifest_from(data).host_modules("edge")


def test_dropping_an_unknown_module_is_a_finding(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    with_workers(data)["hosts"]["worker-2"]["modules"] = {"smrt": False}
    assert ("hosts.worker-2.modules.smrt", "module-known") in findings_of(data, modules)


def test_true_is_not_a_module_value(data: dict[str, Any]) -> None:
    data["hosts"]["edge"]["modules"]["gpu"] = True
    assert [path for path, _ in schema_errors(data)] == ["hosts.edge.modules.gpu"]


def test_expansion_does_not_change_the_profile_or_the_raw_host(data: dict[str, Any]) -> None:
    with_workers(data)["hosts"]["worker-1"]["modules"] = {"metrics": {"scrape_interval": "15s"}}
    manifest = manifest_from(data)
    manifest.hosts["worker-2"]["modules"]["metrics"]["role"] = "server"
    assert data["profiles"]["worker"] == worker_profile()
    assert data["hosts"]["worker-1"] == {
        "profile": "worker",
        "modules": {"metrics": {"scrape_interval": "15s"}},
    }
    assert manifest.raw is data


def test_expanding_twice_gives_the_same_hosts(data: dict[str, Any]) -> None:
    with_workers(data)["hosts"]["worker-2"]["modules"] = {"smart": False, "node": None}
    once = expand_profiles(data)
    assert expand_profiles(once) == once


def test_a_manifest_without_profiles_is_used_as_written(data: dict[str, Any]) -> None:
    assert manifest_from(data).data is data


def test_an_unknown_profile_is_a_finding_next_to_the_schema_errors(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    with_workers(data)["hosts"]["worker-1"]["profile"] = "wroker"
    findings = findings_of(data, modules)
    assert ("hosts.worker-1.profile", "profile-known") in findings
    assert ("hosts.worker-1", "schema") in findings


def test_an_unknown_profile_on_a_complete_host_still_fails(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    data["profiles"] = {}
    data["hosts"]["edge"]["profile"] = "laptop"
    assert findings_of(data, modules) == {("hosts.edge.profile", "profile-known")}


def test_a_profile_no_host_uses_is_a_finding(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    with_workers(data)["profiles"]["spare"] = {"arch": "arm64"}
    assert findings_of(data, modules) == set()
    assert warnings_hit(data) == {"profile-used"}


def test_a_host_misses_a_required_field_only_after_its_profile(data: dict[str, Any]) -> None:
    del with_workers(data)["profiles"]["worker"]["arch"]
    assert schema_errors(data) == [
        ("hosts.worker-1", "'arch' is a required property"),
        ("hosts.worker-2", "'arch' is a required property"),
    ]


def test_a_bad_profile_field_is_reported_once_under_the_profile(data: dict[str, Any]) -> None:
    profile = with_workers(data)["profiles"]["worker"]
    profile["arch"] = "x86"
    profile["colour"] = "blue"
    assert [path for path, _ in schema_errors(data)] == ["profiles.worker", "profiles.worker.arch"]


def test_a_bad_host_field_is_reported_under_the_host(data: dict[str, Any]) -> None:
    with_workers(data)["hosts"]["worker-2"]["memory"] = "lots"
    assert [path for path, _ in schema_errors(data)] == ["hosts.worker-2.memory"]


def test_a_profile_cannot_name_another_profile(data: dict[str, Any]) -> None:
    with_workers(data)["profiles"]["worker"]["profile"] = "worker"
    assert [path for path, _ in schema_errors(data)] == ["profiles.worker"]


def test_module_settings_inherited_from_a_profile_are_checked_on_the_host(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    with_workers(data)["profiles"]["worker"]["modules"]["node"] = {"batery": True}
    findings = findings_of(data, modules)
    assert ("hosts.worker-1.modules.node", "module-settings") in findings
    assert ("hosts.worker-2.modules.node", "module-settings") in findings


def test_findings_of_a_merged_host_point_at_the_host(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    with_workers(data)["profiles"]["worker"]["compose"] = ["apps/missing.compose.yaml"]
    findings = findings_of(data, modules)
    assert ("hosts.worker-1.compose", "compose-file") in findings
    assert ("hosts.worker-2.compose", "compose-file") in findings


def test_text_in_a_profile_is_checked_once_where_it_is_written(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    with_workers(data)["profiles"]["worker"]["zone"] = "data\u200bcenter"
    assert "text-hygiene" in rules_hit(data)
    findings = check_once(manifest_from(data), modules)
    assert [f.path for f in findings if f.rule == "text-hygiene"] == ["profiles.worker.zone"]


def test_the_group_defaults_to_the_profile_name(data: dict[str, Any]) -> None:
    manifest = manifest_from(with_workers(data))
    assert manifest.host_group("worker-1") == "worker"
    assert manifest.host_group("edge") is None


def test_an_own_group_wins_over_the_profile_name(data: dict[str, Any]) -> None:
    with_workers(data)["profiles"]["worker"]["group"] = "batch"
    data["hosts"]["worker-2"]["group"] = "gpu"
    data["hosts"]["edge"]["group"] = "office"
    manifest = manifest_from(data)
    groups = [manifest.host_group(host) for host in ("worker-1", "worker-2", "edge")]
    assert groups == ["batch", "gpu", "office"]


def test_a_group_must_be_a_name(data: dict[str, Any]) -> None:
    data["hosts"]["edge"]["group"] = "Office Laptops"
    assert [path for path, _ in schema_errors(data)] == ["hosts.edge.group"]


def test_vmagent_labels_samples_with_host_project_and_group(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    command = vmagent_command(with_workers(data), "worker-1", modules)
    assert "-remoteWrite.url=http://10.8.0.1:8428/api/v1/write" in command
    labels = [arg for arg in command if arg.startswith("-remoteWrite.label=")]
    assert labels == [
        "-remoteWrite.label=host=worker-1",
        "-remoteWrite.label=project=acme",
        "-remoteWrite.label=group=worker",
    ]


def test_a_host_without_a_group_has_no_group_label(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    command = vmagent_command(with_workers(data), "edge", modules)
    labels = [arg for arg in command if arg.startswith("-remoteWrite.label=")]
    assert labels == ["-remoteWrite.label=host=edge", "-remoteWrite.label=project=acme"]


def test_the_central_host_keeps_its_own_labels_while_agents_write_to_it(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    with_workers(data)["hosts"]["central"]["group"] = "core"
    central = vmagent_command(data, "central", modules)
    worker = vmagent_command(data, "worker-2", modules)
    assert "-remoteWrite.label=group=core" in central
    assert "-remoteWrite.label=group=worker" in worker
    assert "-remoteWrite.label=host=worker-2" in worker
    assert not [arg for arg in worker if "core" in arg or "host=central" in arg]


def test_alloy_labels_lines_with_host_project_and_group(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    tree = render_once(manifest_from(with_workers(data)), modules)
    worker = tree["worker-1/logs/config.alloy"]
    edge = tree["edge/logs/config.alloy"]
    assert 'external_labels = {host = "worker-1", project = "acme", group = "worker"}' in worker
    assert 'external_labels = {host = "edge", project = "acme"}' in edge
