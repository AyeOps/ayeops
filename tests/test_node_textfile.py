from __future__ import annotations

from typing import Any

from ayeops.registry import Module
from ayeops.render import host_compose
from tests.conftest import manifest_from, rules_hit
from tests.test_checks import with_checks

HOST_DIR = "/var/lib/ayeops/textfile"
BACKUP = {
    "host": "edge",
    "description": "Nightly database dump.",
    "metrics": ["acme_backup_last_success_timestamp_seconds"],
    "alerts": [
        {
            "name": "backup-stale",
            "expr": "time() - max(acme_backup_last_success_timestamp_seconds) > bool 93600",
            "for": "10m",
            "severity": "critical",
            "runbook": "backup-stale",
        }
    ],
    "logs": "journald",
    "status": {"internal": "nobody sees a backup until it is needed"},
}


def compose_of(data: dict[str, Any], modules: dict[str, Module], host: str) -> dict[str, Any]:
    return host_compose(manifest_from(data), host, modules)


def test_node_keeps_the_named_textfile_volume_by_default(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    del data["hosts"]["edge"]["modules"]["node"]["textfile_dir"]
    compose = compose_of(data, modules, "edge")
    services = compose["services"]
    assert "node-textfile:/textfile:ro" in services["node-exporter"]["volumes"]
    assert "node-textfile:/textfile" in services["battery"]["volumes"]
    assert compose["volumes"]["node-textfile"] == {}


def test_node_mounts_the_host_root_rslave_unless_turned_off_for_docker_desktop(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    exporter = compose_of(data, modules, "edge")["services"]["node-exporter"]
    assert "/:/host:ro,rslave" in exporter["volumes"]
    data["hosts"]["edge"]["modules"]["node"]["rslave"] = False
    exporter = compose_of(data, modules, "edge")["services"]["node-exporter"]
    assert "/:/host:ro" in exporter["volumes"]
    assert not any("rslave" in volume for volume in exporter["volumes"])


def test_node_textfile_dir_bind_mounts_the_host_directory_for_exporter_and_battery(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    data["hosts"]["edge"]["modules"]["node"]["textfile_dir"] = HOST_DIR
    compose = compose_of(data, modules, "edge")
    services = compose["services"]
    assert f"{HOST_DIR}:/textfile:ro" in services["node-exporter"]["volumes"]
    assert f"{HOST_DIR}:/textfile" in services["battery"]["volumes"]
    assert services["battery"]["environment"]["AYE_TEXTFILE_DIR"] == "/textfile"
    assert "node-textfile" not in (compose.get("volumes") or {})
    assert not any(
        v.startswith("node-textfile:") for s in services.values() for v in s.get("volumes", [])
    )


def test_script_check_runners_write_into_the_node_textfile_dir(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    with_checks(data)
    data["hosts"]["central"]["modules"]["node"] = {"textfile_dir": HOST_DIR}
    compose = compose_of(data, modules, "central")
    for runner in ("check-scripts", "check-scripts-net"):
        assert f"{HOST_DIR}:/textfile" in compose["services"][runner]["volumes"]
    assert "node-textfile" not in (compose.get("volumes") or {})


def test_node_textfile_dir_must_be_an_absolute_host_path(data: dict[str, Any]) -> None:
    data["hosts"]["edge"]["modules"]["node"]["textfile_dir"] = "textfile"
    assert rules_hit(data) == {"module-settings"}


def test_a_service_declared_as_a_host_unit_is_provided_and_takes_flows(
    data: dict[str, Any],
) -> None:
    data["hosts"]["edge"]["units"]["backup"] = "acme-backup.timer"
    data["services"]["backup"] = BACKUP
    data["flows"].append({"from": "edge/backup", "to": "edge/node-exporter", "label": "metrics"})
    assert rules_hit(data) == set()


def test_a_service_without_a_unit_compose_or_module_is_still_a_dead_entry(
    data: dict[str, Any],
) -> None:
    data["services"]["backup"] = BACKUP
    assert rules_hit(data) == {"service-provided"}


def test_a_host_unit_must_name_a_systemd_unit_file(data: dict[str, Any]) -> None:
    data["hosts"]["edge"]["units"] = {"backup": "acme-backup"}
    assert rules_hit(data) == {"schema"}
