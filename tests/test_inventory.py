from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from ayeops.cli import main
from ayeops.inventory import GENERATED_BY, group_name, inventory, inventory_text
from ayeops.registry import load_modules
from tests.conftest import EXAMPLE, ROOT, manifest_from

GOLDEN = ROOT / "tests" / "fixtures" / "inventory" / "acme.yaml"
HOST_VARS = {"ansible_host", "ayeops_arch", "ayeops_group", "ayeops_project", "ayeops_zone"}


def built(data: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = inventory(manifest_from(data), load_modules())
    return result


def members(data: dict[str, Any], group: str) -> list[str]:
    return list(built(data)["all"]["children"][group]["hosts"])


def test_the_acme_inventory_matches_the_committed_golden_file(data: dict[str, Any]) -> None:
    text = inventory_text(manifest_from(data), load_modules(), "ansible-yaml")
    assert text == GOLDEN.read_text(encoding="utf-8")


def test_the_yaml_starts_with_the_inventory_itself_and_carries_no_comment(
    data: dict[str, Any],
) -> None:
    text = inventory_text(manifest_from(data), load_modules(), "ansible-yaml")
    assert text.startswith("all:\n")
    assert "#" not in text
    assert "---" not in text
    assert yaml.safe_load(text)["all"]["vars"] == {"ayeops_generated_by": "aye inventory"}
    assert GENERATED_BY == "aye inventory"


def test_the_json_form_parses_to_the_same_structure_as_the_yaml(data: dict[str, Any]) -> None:
    manifest = manifest_from(data)
    as_json = inventory_text(manifest, load_modules(), "ansible-json")
    as_yaml = inventory_text(manifest, load_modules(), "ansible-yaml")
    assert as_json.startswith("{\n")
    assert as_json.endswith("}\n")
    assert json.loads(as_json) == yaml.safe_load(as_yaml) == built(data)


def test_hosts_carry_the_address_and_the_catalog_fields_as_host_vars(
    data: dict[str, Any],
) -> None:
    hosts = built(data)["all"]["hosts"]
    assert hosts == {
        "central": {
            "ansible_host": "10.8.0.1",
            "ayeops_arch": "arm64",
            "ayeops_project": "acme",
            "ayeops_zone": "datacenter",
        },
        "edge": {
            "ansible_host": "10.8.0.2",
            "ayeops_arch": "amd64",
            "ayeops_project": "acme",
            "ayeops_zone": "office",
        },
    }


def test_a_host_without_address_zone_or_group_gets_no_empty_var(data: dict[str, Any]) -> None:
    del data["hosts"]["central"]["address"]
    del data["hosts"]["central"]["zone"]
    hosts = built(data)["all"]["hosts"]
    assert hosts["central"] == {"ayeops_arch": "arm64", "ayeops_project": "acme"}
    children = built(data)["all"]["children"]
    assert members(data, "zone_office") == ["edge"]
    assert not [name for name in children if name.startswith(("group_", "profile_"))]
    assert "zone_datacenter" not in children


def test_group_names_are_lowercase_with_every_other_character_an_underscore() -> None:
    assert group_name("zone", "DC East-2") == "zone_dc_east_2"
    assert group_name("service", "ai-usage-exporter") == "service_ai_usage_exporter"
    assert group_name("zone", "rack/7.b") == "zone_rack_7_b"
    assert group_name("module", "node_1") == "module_node_1"


def test_every_group_of_the_acme_inventory_is_a_valid_ansible_group_name(
    data: dict[str, Any],
) -> None:
    for name in built(data)["all"]["children"]:
        assert re.fullmatch(r"(zone|group|profile|module|service)_[a-z0-9_]+", name), name


def test_zone_group_and_profile_groups_follow_the_manifest(data: dict[str, Any]) -> None:
    data["hosts"]["central"]["zone"] = "DC East"
    data["hosts"]["central"]["group"] = "core-1"
    data["profiles"] = {"office-box": {"zone": "office"}}
    data["hosts"]["edge"]["profile"] = "office-box"
    del data["hosts"]["edge"]["zone"]
    result = built(data)["all"]
    children = result["children"]
    assert children["zone_dc_east"] == {"hosts": {"central": {}}}
    assert children["zone_office"] == {"hosts": {"edge": {}}}
    assert children["group_core_1"] == {"hosts": {"central": {}}}
    assert children["group_office_box"] == {"hosts": {"edge": {}}}
    assert children["profile_office_box"] == {"hosts": {"edge": {}}}
    assert "profile_core_1" not in children
    assert result["hosts"]["central"]["ayeops_group"] == "core-1"
    assert result["hosts"]["edge"]["ayeops_group"] == "office-box"
    assert result["hosts"]["edge"]["ayeops_zone"] == "office"


def test_module_groups_hold_the_hosts_that_enable_the_module(data: dict[str, Any]) -> None:
    assert members(data, "module_node") == ["central", "edge"]
    assert members(data, "module_grafana") == ["central"]
    assert members(data, "module_disk_forecast") == ["edge"]
    data["hosts"]["edge"]["modules"]["smart"] = False
    assert "module_smart" not in built(data)["all"]["children"]


def test_service_groups_hold_the_hosts_that_run_the_service(data: dict[str, Any]) -> None:
    assert members(data, "service_web") == ["central"]
    assert members(data, "service_victoriametrics") == ["central"]
    assert members(data, "service_vmagent") == ["central", "edge"]
    assert members(data, "service_smartctl_exporter") == ["edge"]


def test_a_planned_service_runs_nowhere_yet(data: dict[str, Any]) -> None:
    data["services"]["web"]["state"] = "planned"
    assert "service_web" not in built(data)["all"]["children"]


def test_a_manifest_service_joins_the_group_of_its_own_host(data: dict[str, Any]) -> None:
    data["services"]["web"]["host"] = "edge"
    assert members(data, "service_web") == ["edge"]


def test_the_order_of_hosts_and_groups_does_not_depend_on_the_manifest(
    data: dict[str, Any],
) -> None:
    first = inventory_text(manifest_from(data), load_modules(), "ansible-yaml")
    data["hosts"] = dict(reversed(list(data["hosts"].items())))
    for spec in data["hosts"].values():
        spec["modules"] = dict(reversed(list(spec["modules"].items())))
    again = built(data)
    assert inventory_text(manifest_from(data), load_modules(), "ansible-yaml") == first
    assert list(again["all"]["hosts"]) == ["central", "edge"]
    children = again["all"]["children"]
    assert list(children) == sorted(children)
    assert list(children["module_node"]["hosts"]) == ["central", "edge"]


def test_no_setting_secret_or_env_name_reaches_the_inventory(data: dict[str, Any]) -> None:
    text = inventory_text(manifest_from(data), load_modules(), "ansible-json")
    for value in ("AYE_GRAFANA_PG_PASSWORD", "100200300", "grafana_ro", ".env", "password"):
        assert value not in text
    for host in built(data)["all"]["hosts"].values():
        assert set(host) <= HOST_VARS


def test_the_command_prints_yaml_by_default_and_json_on_request(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["inventory", str(EXAMPLE)]) == 0
    printed = capsys.readouterr().out
    assert printed == GOLDEN.read_text(encoding="utf-8")
    assert main(["inventory", str(EXAMPLE), "--format", "ansible-json"]) == 0
    assert json.loads(capsys.readouterr().out) == yaml.safe_load(printed)


def test_the_command_writes_the_file_and_check_passes_on_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "hosts.json"
    assert main(["inventory", str(EXAMPLE), "--format", "ansible-json", "-o", str(out)]) == 0
    assert "2 host(s) written to" in capsys.readouterr().out
    assert b"\r" not in out.read_bytes()
    assert json.loads(out.read_text(encoding="utf-8")) == yaml.safe_load(GOLDEN.read_text("utf-8"))
    argv = ["inventory", str(EXAMPLE), "--format", "ansible-json", "-o", str(out), "--check"]
    assert main(argv) == 0
    assert "matches the inventory" in capsys.readouterr().out


def test_check_exits_1_with_a_diff_on_a_stale_file_and_leaves_it_alone(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "hosts.yaml"
    stale = GOLDEN.read_text(encoding="utf-8").replace("10.8.0.1", "10.8.0.9")
    out.write_text(stale, encoding="utf-8")
    assert main(["inventory", str(EXAMPLE), "-o", str(out), "--check"]) == 1
    captured = capsys.readouterr()
    assert "-      ansible_host: 10.8.0.9\n" in captured.out
    assert "+      ansible_host: 10.8.0.1\n" in captured.out
    assert "differs from the inventory" in captured.err
    assert out.read_text(encoding="utf-8") == stale


def test_check_exits_1_when_the_file_is_missing_and_creates_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "hosts.yaml"
    assert main(["inventory", str(EXAMPLE), "-o", str(out), "--check"]) == 1
    assert "does not exist" in capsys.readouterr().err
    assert not out.exists()


def test_check_without_a_file_is_a_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["inventory", str(EXAMPLE), "--check"]) == 2
    assert "--check needs -o" in capsys.readouterr().err


def test_a_manifest_with_errors_gets_no_inventory(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    manifest = tmp_path / "broken.yaml"
    manifest.write_text("version: 1\nproject: acme\nhosts: {}\n", encoding="utf-8")
    out = tmp_path / "hosts.yaml"
    assert main(["inventory", str(manifest), "-o", str(out)]) == 1
    captured = capsys.readouterr()
    assert "no inventory, fix the errors first" in captured.err
    assert captured.out == ""
    assert not out.exists()
