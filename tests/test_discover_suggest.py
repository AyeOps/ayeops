from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from ayeops.cli import main
from ayeops.discover import command
from ayeops.discover.suggest import NOT_ENABLED, manifest_name, size_up, suggest
from ayeops.manifest import Manifest
from ayeops.registry import load_modules
from ayeops.rules import check_manifest, check_service
from tests.test_discover_collect import collect


@pytest.fixture
def report(tmp_path: Path) -> dict[str, Any]:
    return collect(tmp_path / "host")


def test_host_block_carries_arch_memory_reservations_and_compose_files(
    report: dict[str, Any],
) -> None:
    host = suggest(report)["hosts"]["build-box-local"]
    assert host["arch"] == "amd64"
    assert host["memory"] == f"{263842716 * 1024 // 1024**2}m"
    assert host["reserved"] == {"mysql": "3g", "php-fpm": "512m"}
    assert host["compose"] == [
        "shop/compose.yaml",
        "shop/compose.override.yaml",
        "tools/docker-compose.yml",
    ]


def test_modules_follow_the_hardware_the_probe_found(report: dict[str, Any]) -> None:
    assert suggest(report)["hosts"]["build-box-local"]["modules"] == {
        "metrics": {},
        "node": {"battery": True},
        "containers": {},
        "smart": {},
        "disk-forecast": {},
        "gpu": {},
        "network": {"interfaces": ["enp3s0", "wlan0"], "wifi": True},
    }


def test_a_bare_vm_gets_only_the_modules_it_can_use() -> None:
    report = {
        "system": {"hostname": "vm1", "arch": "arm64"},
        "memory": {"total_bytes": 4 * 1024**3},
        "storage": {"disks": [{"name": "vda", "kind": "virtual"}], "filesystems": []},
        "network": [{"name": "eth0", "kind": "virtual", "operstate": "up"}],
    }
    assert suggest(report)["hosts"] == {
        "vm1": {"arch": "arm64", "memory": "4096m", "modules": {"metrics": {}, "node": {}}}
    }


def test_compose_services_become_stubs_with_todo_values_not_comments(
    report: dict[str, Any],
) -> None:
    services = suggest(report)["services"]
    assert sorted(services) == ["cache", "db", "tools-db", "web", "worker-queue"]
    stub = services["web"]
    assert stub["host"] == "build-box-local"
    assert stub["description"].startswith("TODO")
    assert stub["status"]["internal"].startswith("TODO")
    assert (stub["metrics"], stub["alerts"], stub["logs"]) == ([], [], "docker")


def test_services_a_module_already_runs_get_no_stub(report: dict[str, Any]) -> None:
    assert "web" not in suggest(report, {"web"})["services"]


def test_app_exporters_are_listed_as_suggested_not_enabled(report: dict[str, Any]) -> None:
    exporters = suggest(report)["suggested"]["exporters"]
    assert {e["app"] for e in exporters} == {
        "mysql",
        "nginx",
        "php-fpm",
        "postgres",
        "redis",
        "valkey",
    }
    assert {e["status"] for e in exporters} == {NOT_ENABLED}


def test_unsupported_gpus_and_probe_notes_are_passed_on(tmp_path: Path) -> None:
    report = collect(tmp_path, is_root=False)
    notes = suggest(report)["suggested"]["notes"]
    assert notes[0].startswith("intel GPU found")
    assert any("run with --sudo" in note for note in notes)


def test_stubs_pass_the_schema_and_fail_the_catalog_rules_until_filled(
    report: dict[str, Any], tmp_path: Path
) -> None:
    fragment = suggest(report, command.known_services())
    for path in fragment["hosts"]["build-box-local"]["compose"]:
        target = tmp_path / "manifest" / path
        target.parent.mkdir(parents=True, exist_ok=True)
        services = {"shop": ["db", "web", "worker-queue"], "tools": ["cache", "tools-db"]}
        target.write_text(
            yaml.safe_dump({"services": {key: {} for key in services[path.split("/")[0]]}}),
            encoding="utf-8",
        )
    data = {
        "version": 1,
        "project": "lab",
        "runbook_base_url": "https://runbooks.example",
        "hosts": fragment["hosts"],
        "services": fragment["services"],
    }
    findings = check_manifest(Manifest(data, tmp_path / "manifest"), load_modules())
    assert {f.rule for f in findings} == {
        "service-description",
        "service-metrics",
        "service-alerts",
        "service-status",
    }


def test_a_todo_placeholder_does_not_count_as_a_description_or_status() -> None:
    service = {
        "description": "TODO: what web does",
        "metrics": {"none": "todo later"},
        "alerts": [{"name": "x", "severity": "warning", "runbook": "TODO"}],
        "logs": "docker",
        "status": {"public": "TODO"},
    }
    rules = {f.rule for f in check_service("services.web", service)}
    assert rules == {"service-description", "service-metrics", "alert-runbook", "service-status"}


def test_names_are_made_manifest_safe() -> None:
    assert manifest_name("Build-Box.local", "host") == "build-box-local"
    assert manifest_name("worker_queue", "service") == "worker-queue"
    assert manifest_name("1password", "service") == "service-1password"
    assert manifest_name("", "host") == "host"


def test_reservations_round_up() -> None:
    assert size_up(3 * 1024**3) == "3g"
    assert size_up(3 * 1024**3 + 1) == "4g"
    assert size_up(1) == "1m"


class FakeSsh:
    def __init__(self, code: int, stdout: bytes, stderr: bytes = b"") -> None:
        self.result = subprocess.CompletedProcess[bytes]([], code, stdout, stderr)
        self.calls: list[tuple[list[str], bytes]] = []

    def __call__(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        self.calls.append((argv, kwargs["input"]))
        return self.result


def test_remote_discovery_pipes_the_probe_into_python_over_ssh() -> None:
    ssh = FakeSsh(0, b'{"probe_version": 1}')
    assert command.fetch_remote("ops@box", sudo=True, run=ssh) == {"probe_version": 1}
    argv, stdin = ssh.calls[0]
    assert argv == ["ssh", "-o", "BatchMode=yes", "ops@box", "sudo -n python3 -"]
    assert stdin == command.probe_source()
    assert b"\r\n" not in stdin
    assert b"def collect(" in stdin


def test_remote_discovery_without_sudo_runs_plain_python() -> None:
    ssh = FakeSsh(0, b"{}")
    command.fetch_remote("box", sudo=False, python="python3.12", run=ssh)
    assert ssh.calls[0][0][-1] == "python3.12 -"


def test_ssh_failure_names_the_host_and_the_last_error_line() -> None:
    ssh = FakeSsh(1, b"", b"Warning: something\nsudo: a password is required\n")
    with pytest.raises(command.DiscoverError, match="box: exit code 1: sudo: a password"):
        command.fetch_remote("box", sudo=True, run=ssh)


def test_a_probe_that_prints_no_json_is_an_error() -> None:
    with pytest.raises(command.DiscoverError, match="did not print JSON"):
        command.fetch_remote("box", sudo=False, run=FakeSsh(0, b"Traceback"))


def args(**values: Any) -> argparse.Namespace:
    defaults = {
        "ssh": None,
        "suggest": None,
        "adopt": None,
        "sudo": False,
        "python": "python3",
        "out": None,
        "project": None,
        "until": None,
        "observe": False,
    }
    return argparse.Namespace(**{**defaults, **values})


def test_ssh_report_is_written_to_the_output_file(tmp_path: Path) -> None:
    out = tmp_path / "box.json"

    def fetch(target: str, sudo: bool, python: str) -> dict[str, Any]:
        return {"probe_version": 1, "target": target, "sudo": sudo}

    assert command.run(args(ssh="box", sudo=True, out=out), fetch) == 0
    assert json.loads(out.read_text(encoding="utf-8")) == {
        "probe_version": 1,
        "target": "box",
        "sudo": True,
    }


def test_sudo_without_ssh_is_refused(capsys: pytest.CaptureFixture[str]) -> None:
    assert command.run(args(sudo=True)) == 1
    assert "--sudo applies to --ssh" in capsys.readouterr().err


def test_suggest_command_prints_a_yaml_fragment(
    report: dict[str, Any], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "box.json"
    path.write_text(json.dumps(report), encoding="utf-8")
    assert main(["discover", "--suggest", str(path)]) == 0
    fragment = yaml.safe_load(capsys.readouterr().out)
    assert set(fragment) == {"hosts", "services", "suggested"}
    assert "build-box-local" in fragment["hosts"]


def test_suggest_rejects_a_file_that_is_not_a_report(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "box.json"
    path.write_text("[1, 2]", encoding="utf-8")
    assert main(["discover", "--suggest", str(path)]) == 1
    assert "not a discovery report" in capsys.readouterr().err
