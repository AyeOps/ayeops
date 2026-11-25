from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml

from ayeops.cli import main
from ayeops.local_modules import spec_problems
from ayeops.registry import Module, load_modules
from tests.conftest import EXAMPLE, ROOT, manifest_from, render_once, rules_hit

FIXTURE = ROOT / "tests" / "fixtures" / "modules"


def with_queue(data: dict[str, Any], root: Path = FIXTURE) -> dict[str, Any]:
    data["module_paths"] = [str(root)]
    data["hosts"]["edge"]["modules"]["acme-queue"] = {"backlog_max": 500}
    return data


@pytest.fixture
def local_root(tmp_path: Path) -> Path:
    root = tmp_path / "modules"
    shutil.copytree(FIXTURE, root)
    return root


def edit_spec(root: Path, change: Any) -> None:
    path = root / "queue" / "module.yaml"
    spec = yaml.safe_load(path.read_text(encoding="utf-8"))
    change(spec)
    path.write_text(yaml.safe_dump(spec), encoding="utf-8")


def test_a_local_module_passes_the_same_rules_as_the_builtins(data: dict[str, Any]) -> None:
    assert rules_hit(with_queue(data)) == set()


def test_a_local_module_renders_its_services_scrape_jobs_alerts_and_dashboards(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    tree = render_once(manifest_from(with_queue(data)), modules)
    compose = yaml.safe_load(tree["edge/compose.yaml"])
    service = compose["services"]["queue-exporter"]
    assert service["labels"]["io.ayeops.module"] == "acme-queue"
    assert service["mem_limit"] == "32m"
    assert service["ports"] == ["127.0.0.1:9140:9140"]
    jobs = yaml.safe_load(tree["edge/vmagent/scrape.yaml"])["scrape_configs"]
    assert "acme-queue" in {job["job_name"] for job in jobs}
    groups = yaml.safe_load(tree["central/grafana/provisioning/alerting/rules.yaml"])["groups"]
    rules = {g["name"]: g["rules"] for g in groups}["acme-queue"]
    assert rules[0]["annotations"]["runbook_url"] == "https://runbooks.acme.example/queue-backlog"
    assert "central/grafana/dashboards/queue.json" in tree


def test_a_local_module_runbook_base_url_overrides_the_manifest_one(
    data: dict[str, Any], local_root: Path, modules: dict[str, Module]
) -> None:
    edit_spec(local_root, lambda spec: spec.update(runbook_base_url="https://kit.acme.example/"))
    tree = render_once(manifest_from(with_queue(data, local_root)), modules)
    groups = yaml.safe_load(tree["central/grafana/provisioning/alerting/rules.yaml"])["groups"]
    rule = {g["name"]: g["rules"] for g in groups}["acme-queue"][0]
    assert rule["annotations"]["runbook_url"] == "https://kit.acme.example/queue-backlog"


def test_a_local_module_alert_without_a_runbook_fails_check(
    data: dict[str, Any], local_root: Path
) -> None:
    edit_spec(local_root, lambda spec: spec["alerts"][0].pop("runbook"))
    assert rules_hit(with_queue(data, local_root)) == {"alert-runbook"}


def test_aye_check_exits_non_zero_for_a_local_module_without_a_runbook(
    data: dict[str, Any], local_root: Path, tmp_path: Path
) -> None:
    edit_spec(local_root, lambda spec: spec["alerts"][0].pop("runbook"))
    shutil.copytree(EXAMPLE.parent / "apps", tmp_path / "apps")
    manifest = tmp_path / "acme.yaml"
    manifest.write_text(yaml.safe_dump(with_queue(data, local_root)), encoding="utf-8")
    assert main(["check", str(manifest)]) == 1


def test_a_local_module_alert_needs_its_runbook_page(
    data: dict[str, Any], local_root: Path
) -> None:
    (local_root / "queue" / "runbooks" / "queue-backlog.md").unlink()
    assert rules_hit(with_queue(data, local_root)) == {"alert-runbook"}


def test_a_local_module_alert_without_severity_is_rejected(
    data: dict[str, Any], local_root: Path
) -> None:
    edit_spec(local_root, lambda spec: spec["alerts"][0].pop("severity"))
    assert rules_hit(with_queue(data, local_root)) == {"alert-severity"}


def test_a_local_module_service_without_description_is_rejected(
    data: dict[str, Any], local_root: Path
) -> None:
    edit_spec(local_root, lambda spec: spec["services"]["queue-exporter"].pop("description"))
    assert rules_hit(with_queue(data, local_root)) == {"service-description"}


def test_a_local_module_service_referencing_an_unknown_alert_is_rejected(
    data: dict[str, Any], local_root: Path
) -> None:
    edit_spec(
        local_root, lambda spec: spec["services"]["queue-exporter"]["alerts"].append("nowhere")
    )
    assert rules_hit(with_queue(data, local_root)) == {"alert-known"}


def test_a_local_module_named_like_a_builtin_is_rejected(
    data: dict[str, Any], local_root: Path
) -> None:
    edit_spec(local_root, lambda spec: spec.update(name="probes"))
    data["module_paths"] = [str(local_root)]
    assert rules_hit(data) == {"module-clash"}


def test_a_local_module_spec_with_an_unknown_key_is_rejected(
    data: dict[str, Any], local_root: Path
) -> None:
    edit_spec(local_root, lambda spec: spec.update(extras=True))
    assert rules_hit(with_queue(data, local_root)) == {"module-spec"}


def test_a_local_module_settings_schema_must_be_a_valid_schema(
    data: dict[str, Any], local_root: Path
) -> None:
    edit_spec(local_root, lambda spec: spec.update(settings_schema={"type": "nothing"}))
    assert rules_hit(with_queue(data, local_root)) == {"module-spec"}


def test_a_local_module_image_must_be_pinned(data: dict[str, Any], local_root: Path) -> None:
    compose = local_root / "queue" / "compose.yaml.j2"
    text = compose.read_text(encoding="utf-8").replace(":v0.28.0", ":latest")
    compose.write_text(text, encoding="utf-8")
    assert rules_hit(with_queue(data, local_root)) == {"image-pinned"}


def test_a_local_module_dashboard_must_exist(data: dict[str, Any], local_root: Path) -> None:
    (local_root / "queue" / "dashboards" / "queue.json").unlink()
    assert rules_hit(with_queue(data, local_root)) == {"module-files"}


def test_a_missing_module_path_is_rejected(data: dict[str, Any], tmp_path: Path) -> None:
    data["module_paths"] = [str(tmp_path / "absent")]
    assert rules_hit(data) == {"module-path"}


def test_local_module_settings_are_validated_against_its_schema(data: dict[str, Any]) -> None:
    with_queue(data)["hosts"]["edge"]["modules"]["acme-queue"] = {"backlog_max": 0}
    assert rules_hit(data) == {"module-settings"}


def test_every_builtin_module_matches_the_module_schema() -> None:
    problems = [
        problem
        for module in load_modules().values()
        for problem in spec_problems(f"modules.{module.name}", module.spec)
    ]
    assert problems == []
