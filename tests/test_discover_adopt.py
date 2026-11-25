from __future__ import annotations

import copy
import json
from datetime import date, timedelta
from pathlib import Path, PurePosixPath
from typing import Any

import pytest
import yaml

from ayeops.cli import main
from ayeops.diff import Deployed, compare
from ayeops.discover.adopt import (
    RUNBOOK,
    AdoptError,
    Adoption,
    adopt,
    host_address,
    remote_dir,
    takeover,
)
from ayeops.manifest import Manifest
from ayeops.registry import Module
from ayeops.render import FleetRender, dump_yaml
from ayeops.rules import check_manifest, check_nodata, check_warnings
from tests.conftest import check_once, render_once
from tests.test_discover_collect import collect

TODAY = date(2026, 11, 14)
HOST = "build-box-local"
RUNNING = {"db", "web", "worker-queue"}
STOPPED = {"cache", "tools-db"}


@pytest.fixture
def report(tmp_path: Path) -> dict[str, Any]:
    return collect(tmp_path / "host")


def written(adoption: Adoption, root: Path) -> Manifest:
    for path, document in adoption.compose_files.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(dump_yaml(document), encoding="utf-8")
    return Manifest(adoption.manifest, root)


def host_files(tree: dict[str, str], host: str) -> dict[str, str]:
    prefix = f"{host}/"
    return {
        path.removeprefix(prefix): text for path, text in tree.items() if path.startswith(prefix)
    }


def first_deploy(manifest: Manifest, modules: dict[str, Module], host: str) -> dict[str, str]:
    files = host_files(render_once(manifest, modules), host)
    plan = compare(files, Deployed(added=set(files), env_file="absent"), host)
    return dict(plan.services)


def adopted_containers(report: dict[str, Any]) -> set[str]:
    return {
        service["container"]
        for project in report["containers"]["compose_projects"]
        for service in project["services"]
    }


def test_the_adopted_manifest_passes_the_check_with_one_warning_per_waiver(
    report: dict[str, Any], tmp_path: Path, modules: dict[str, Module]
) -> None:
    manifest = written(adopt(report, modules, TODAY), tmp_path / "adopt")
    assert check_once(manifest, modules) == []
    warnings = check_warnings(manifest, TODAY, modules)
    assert sorted((w.path, w.rule) for w in warnings) == sorted(
        [(f"services.{name}.metrics.none", "service-metrics") for name in RUNNING]
        + [(f"services.{name}.status.internal", "service-status") for name in RUNNING]
    )
    assert all("ends on 2026-11-28, in 14 days" in w.message for w in warnings)


def test_the_waivers_fail_the_check_the_day_after_until(
    report: dict[str, Any], tmp_path: Path, modules: dict[str, Module]
) -> None:
    adoption = adopt(report, modules, TODAY, until=date(2026, 12, 1))
    manifest = written(adoption, tmp_path / "adopt")
    assert check_manifest(manifest, modules, date(2026, 12, 1)) == []
    late = check_manifest(manifest, modules, date(2026, 12, 2))
    assert {f.rule for f in late} == {"service-metrics", "service-status"}
    assert len(late) == 2 * len(RUNNING)


def test_running_services_get_every_catalog_field_and_stopped_ones_are_disabled(
    report: dict[str, Any], modules: dict[str, Module]
) -> None:
    services = adopt(report, modules, TODAY).manifest["services"]
    assert set(services) == RUNNING | STOPPED
    web = services["web"]
    assert web["description"] == (
        "web of the compose project shop on build-box-local, image nginx:1.29.2, adopted as is"
    )
    assert web["logs"] == "docker"
    assert web["metrics"]["none"] == {
        "reason": "adopted as is, TODO: name the metrics of web, or why it has none",
        "until": "2026-11-28",
    }
    assert web["status"]["internal"]["reason"].startswith("adopted as is, TODO:")
    assert [(a["name"], a["severity"], a["runbook"]) for a in web["alerts"]] == [
        ("web-down", "warning", RUNBOOK)
    ]
    assert services["cache"] == {
        "host": HOST,
        "state": "disabled",
        "description": "cache of the compose project tools on build-box-local,"
        " image valkey/valkey:8.1.4, adopted as is while stopped",
    }


def test_the_host_block_describes_the_machine_with_no_module(
    report: dict[str, Any], modules: dict[str, Module]
) -> None:
    adoption = adopt(report, modules, TODAY)
    assert adoption.manifest["project"] == "ayeops"
    assert adoption.manifest["hosts"] == {
        HOST: {
            "arch": "amd64",
            "address": "100.101.102.103",
            "memory": f"{263842716 * 1024 // 1024**2}m",
            "reserved": {"mysql": "3g", "php-fpm": "512m"},
            "modules": {},
            "compose": [
                {"file": "shop/compose.yaml", "name": "shop"},
                {"file": "tools/compose.yaml", "name": "tools"},
            ],
        }
    }


def test_the_compose_stub_names_the_real_project_containers_ports_and_files(
    report: dict[str, Any], modules: dict[str, Module]
) -> None:
    shop = adopt(report, modules, TODAY).compose_files["shop/compose.yaml"]
    assert shop["name"] == "shop"
    assert shop["x-aye-adopted"]["config_files"] == [
        "/srv/shop/compose.yaml",
        "/srv/shop/compose.override.yaml",
    ]
    assert shop["services"] == {
        "db": {"image": "postgres:17.6"},
        "web": {
            "image": "nginx:1.29.2",
            "ports": [{"target": 80, "published": "8080", "protocol": "tcp"}],
        },
        "worker-queue": {
            "image": "ghcr.io/acme/shop-worker:2.4.1",
            "ports": [
                {"target": 9000, "published": "9001", "protocol": "tcp", "host_ip": "127.0.0.1"}
            ],
            "container_name": "shop-worker_queue-1",
        },
    }


def test_the_first_render_adds_no_container_and_the_first_diff_recreates_nothing(
    report: dict[str, Any], tmp_path: Path, modules: dict[str, Module]
) -> None:
    adoption = adopt(report, modules, TODAY)
    manifest = written(adoption, tmp_path / "adopt")
    compose = FleetRender(manifest, modules).compose(HOST)
    assert compose == {"name": "ayeops-build-box-local", "services": {}}
    assert takeover(report, compose, remote_dir(HOST)) == []
    assert first_deploy(manifest, modules, HOST) == {}
    files = host_files(render_once(manifest, modules), HOST)
    assert compare(files, Deployed(same=set(files), env_file="absent"), HOST).empty


def test_observe_enables_only_exporters_whose_ports_and_names_are_free(
    report: dict[str, Any], tmp_path: Path, modules: dict[str, Module]
) -> None:
    adoption = adopt(report, modules, TODAY, observe=True)
    chosen = adoption.manifest["hosts"][HOST]["modules"]
    assert set(chosen) == {"metrics", "smart", "gpu", "network"}
    assert "module containers left off: cadvisor needs host port 8080, which is in use" in (
        adoption.notes
    )
    assert "module node left off: node-exporter needs host port 9100, which is in use" in (
        adoption.notes
    )
    assert "module disk-forecast left off: needs node, which is off" in adoption.notes
    manifest = written(adoption, tmp_path / "adopt")
    assert check_once(manifest, modules) == []
    compose = FleetRender(manifest, modules).compose(HOST)
    assert takeover(report, compose, remote_dir(HOST)) == []
    changes = first_deploy(manifest, modules, HOST)
    assert set(changes) == set(compose["services"])
    assert all(change.startswith("created") for change in changes.values())
    assert not set(changes) & (RUNNING | STOPPED)
    names = {f"{compose['name']}-{name}-1" for name in compose["services"]}
    assert not names & adopted_containers(report)


def test_observe_leaves_off_a_module_whose_container_name_exists_and_those_that_need_it(
    report: dict[str, Any], tmp_path: Path, modules: dict[str, Module]
) -> None:
    report["containers"]["standalone_containers"].append(
        {"name": "ayeops-build-box-local-vmagent-1", "image": "x", "state": "running"}
    )
    adoption = adopt(report, modules, TODAY, observe=True)
    chosen = adoption.manifest["hosts"][HOST]["modules"]
    assert "metrics" not in chosen
    assert all("metrics" not in modules[name].requires for name in chosen)
    assert "module metrics left off: the container name of vmagent is taken" in adoption.notes
    manifest = written(adoption, tmp_path / "adopt")
    assert check_once(manifest, modules) == []
    assert takeover(report, FleetRender(manifest, modules).compose(HOST), remote_dir(HOST)) == []


def test_a_project_that_would_take_over_a_running_one_is_refused(
    report: dict[str, Any], modules: dict[str, Module]
) -> None:
    projects = report["containers"]["compose_projects"]
    projects.append({"name": "ayeops-build-box-local", "config_files": [], "services": []})
    with pytest.raises(AdoptError, match="choose another --project"):
        adopt(report, modules, TODAY)
    assert adopt(report, modules, TODAY, project="ops").manifest["project"] == "ops"


def test_a_compose_file_inside_the_deploy_directory_is_refused(
    report: dict[str, Any], modules: dict[str, Module]
) -> None:
    project = report["containers"]["compose_projects"][1]
    project["config_files"] = ["/opt/ayeops/build-box-local/compose.yaml"]
    with pytest.raises(AdoptError, match="the directory aye deploy replaces; move it first"):
        adopt(report, modules, TODAY)


def test_a_report_without_the_containers_is_refused(modules: dict[str, Module]) -> None:
    report = {
        "system": {"hostname": "box", "arch": "amd64"},
        "containers": {"runtime": "docker", "error": "permission denied", "compose_projects": []},
    }
    with pytest.raises(AdoptError, match="could not list the containers"):
        adopt(report, modules, TODAY)


def test_an_arch_aye_does_not_render_for_is_refused(modules: dict[str, Module]) -> None:
    with pytest.raises(AdoptError, match="amd64 and arm64 only"):
        adopt({"system": {"hostname": "pi", "arch": "armhf"}}, modules, TODAY)


def test_an_until_in_the_past_is_refused(
    report: dict[str, Any], modules: dict[str, Module]
) -> None:
    with pytest.raises(AdoptError, match="in the past"):
        adopt(report, modules, TODAY, until=TODAY - timedelta(days=1))


def test_takeover_names_project_directory_container_and_port_collisions(
    report: dict[str, Any],
) -> None:
    report = copy.deepcopy(report)
    report["containers"]["compose_projects"][1]["config_files"] = ["/opt/ayeops/x/compose.yml"]
    compose = {
        "name": "shop",
        "services": {
            "proxy": {"ports": ["127.0.0.1:8080:80"]},
            "cache": {"container_name": "lone"},
            "free": {"ports": ["127.0.0.1:18080:80"]},
        },
    }
    assert takeover(report, compose, PurePosixPath("/opt/ayeops/x")) == [
        "the compose project shop already runs on the host",
        "/opt/ayeops/x/compose.yml of the compose project tools lies in /opt/ayeops/x,"
        " the directory aye deploy replaces",
        "cache: the container lone already exists on the host",
        "proxy: host port 8080 is already in use on the host",
    ]


def test_the_down_alert_counts_the_adopted_containers_and_is_quiet_without_cadvisor(
    report: dict[str, Any], modules: dict[str, Module]
) -> None:
    alert = adopt(report, modules, TODAY).manifest["services"]["worker-queue"]["alerts"][0]
    counted = 'container_start_time_seconds{host="build-box-local",name="shop-worker_queue-1"}'
    assert alert["expr"] == (
        f"((count({counted}) or vector(0)) < bool 1)"
        ' * ((count(container_start_time_seconds{host="build-box-local"}) or vector(0)) > bool 0)'
    )
    assert check_nodata("services.worker-queue.alerts.0", alert, "Alerting") == []


def test_a_service_of_several_containers_is_counted_by_a_name_pattern(
    modules: dict[str, Module],
) -> None:
    report = {
        "system": {"hostname": "box", "arch": "arm64"},
        "containers": {
            "runtime": "docker",
            "compose_projects": [
                {
                    "name": "app",
                    "config_files": ["/srv/app/compose.yaml"],
                    "services": [
                        {
                            "service": "web",
                            "container": f"app-web-{n}",
                            "image": "nginx:1.29.2",
                            "state": "running",
                            "published_ports": [],
                        }
                        for n in (1, 2)
                    ],
                }
            ],
        },
    }
    adoption = adopt(report, modules, TODAY)
    alert = adoption.manifest["services"]["web"]["alerts"][0]
    assert 'name=~"app-web-1|app-web-2"}) or vector(0)) < bool 2)' in alert["expr"]
    assert adoption.compose_files["app/compose.yaml"]["services"]["web"] == {
        "image": "nginx:1.29.2",
        "scale": 2,
    }
    assert "web ran 2 containers; the stub says scale 2, check the real file" in adoption.notes


def test_names_avoid_module_services_other_projects_and_module_alerts(
    modules: dict[str, Module],
) -> None:
    def container(service: str, project: str) -> dict[str, Any]:
        return {
            "service": service,
            "container": f"{project}-{service}-1",
            "image": "busybox:1.37.0",
            "state": "running",
        }

    report = {
        "system": {"hostname": "box", "arch": "amd64"},
        "containers": {
            "runtime": "docker",
            "compose_projects": [
                {"name": "a", "services": [container("cadvisor", "a"), container("host", "a")]},
                {"name": "b", "services": [container("host", "b")]},
            ],
        },
    }
    adoption = adopt(report, modules, TODAY)
    services = adoption.manifest["services"]
    assert set(services) == {"a-cadvisor", "host", "b-host"}
    assert services["host"]["alerts"][0]["name"] == "host-down-2"
    stub = adoption.compose_files["a/compose.yaml"]["services"]
    assert stub["a-cadvisor"]["container_name"] == "a-cadvisor-1"
    assert "container_name" not in stub["host"]
    assert "a/cadvisor is a-cadvisor in the manifest" in adoption.notes


def test_a_report_of_an_older_probe_without_port_details_still_adopts(
    report: dict[str, Any], modules: dict[str, Module]
) -> None:
    for project in report["containers"]["compose_projects"]:
        for service in project["services"]:
            service.pop("ports")
    stub = adopt(report, modules, TODAY).compose_files["shop/compose.yaml"]["services"]
    assert stub["worker-queue"]["ports"] == [
        {"target": 9001, "published": "9001", "protocol": "tcp"}
    ]


def test_the_address_is_a_vpn_one_else_the_one_on_the_gateway_subnet() -> None:
    lan = {"name": "eth0", "kind": "wired", "operstate": "up", "addresses": ["10.0.0.5/24"]}
    vpn = {"name": "wg0", "kind": "vpn", "operstate": "unknown", "addresses": ["10.8.0.2/32"]}
    other = {"name": "eth1", "kind": "wired", "operstate": "up", "addresses": ["172.16.0.9/16"]}
    assert host_address({"network": [other, lan, vpn], "gateway": "10.0.0.1"}) == "10.8.0.2"
    assert host_address({"network": [other, lan], "gateway": "10.0.0.1"}) == "10.0.0.5"
    assert host_address({"network": [other, lan]}) is None
    assert host_address({"network": [{**lan, "operstate": "down"}], "gateway": "10.0.0.1"}) is None


def test_the_command_writes_the_manifest_and_stubs_that_check_and_render(
    report: dict[str, Any], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "box.json"
    source.write_text(json.dumps(report), encoding="utf-8")
    out = tmp_path / "adopt" / "ayeops.yaml"
    assert main(["discover", "--adopt", str(source), "-o", str(out)]) == 0
    err = capsys.readouterr().err
    assert f"{out}: written" in err
    assert "note: shop/worker_queue is worker-queue in the manifest" in err
    assert "3 running and 2 stopped service(s) in 2 compose project(s)" in err
    assert "0 AyeOps container(s) to add, 6 warning(s)" in err
    assert yaml.safe_load((out.parent / "tools" / "compose.yaml").read_text())["name"] == "tools"
    assert main(["check", str(out)]) == 0
    assert main(["render", str(out), "-o", str(tmp_path / "rendered")]) == 0
    assert (tmp_path / "rendered" / HOST / "compose.yaml").is_file()


def test_the_command_replaces_no_file(
    report: dict[str, Any], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "box.json"
    source.write_text(json.dumps(report), encoding="utf-8")
    out = tmp_path / "ayeops.yaml"
    (tmp_path / "shop").mkdir()
    (tmp_path / "shop" / "compose.yaml").write_text("services: {}\n", encoding="utf-8")
    assert main(["discover", "--adopt", str(source), "-o", str(out)]) == 1
    assert "exists already, --adopt writes new files only" in capsys.readouterr().err
    assert not out.exists()


def test_the_command_needs_an_output_file_and_its_flags_need_adopt(
    report: dict[str, Any], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "box.json"
    source.write_text(json.dumps(report), encoding="utf-8")
    assert main(["discover", "--adopt", str(source)]) == 1
    assert "name it with -o" in capsys.readouterr().err
    assert main(["discover", "--suggest", str(source), "--observe"]) == 1
    assert "--observe applies to --adopt" in capsys.readouterr().err


def test_the_command_reports_a_refusal_with_the_report_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "box.json"
    source.write_text('{"system": {"hostname": "pi", "arch": "armhf"}}', encoding="utf-8")
    assert main(["discover", "--adopt", str(source), "-o", str(tmp_path / "a.yaml")]) == 1
    assert f"{source}: arch 'armhf'" in capsys.readouterr().err
