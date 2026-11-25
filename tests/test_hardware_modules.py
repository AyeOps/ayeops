from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from ayeops.registry import MODULES_DIR, Module
from ayeops.render import host_compose, host_scrape
from tests.conftest import ROOT, WAIT, manifest_from, rules_hit

WIFI_METRICS = ROOT / "tests" / "fixtures" / "network" / "node-exporter-wifi.txt"


def edge(data: dict[str, Any]) -> dict[str, Any]:
    modules: dict[str, Any] = data["hosts"]["edge"]["modules"]
    return modules


def services(data: dict[str, Any], modules: dict[str, Module]) -> dict[str, Any]:
    result: dict[str, Any] = host_compose(manifest_from(data), "edge", modules)["services"]
    return result


def scrape_job(data: dict[str, Any], modules: dict[str, Module], name: str) -> dict[str, Any]:
    scrape = host_scrape(manifest_from(data), "edge", modules)
    assert scrape is not None
    jobs = {job["job_name"]: job for job in scrape["scrape_configs"]}
    job: dict[str, Any] = jobs[name]
    return job


def test_network_module_needs_declared_interfaces(data: dict[str, Any]) -> None:
    edge(data)["network"] = {"wifi": True}
    assert rules_hit(data) == {"module-settings"}


def test_network_exporter_and_scrape_keep_only_declared_interfaces(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    edge(data)["network"] = {"interfaces": ["eth0.100", "wlan0"]}
    command = services(data, modules)["net-exporter"]["command"]
    assert "--collector.netdev.device-include=^(eth0[.]100|wlan0)$" in command
    relabel = scrape_job(data, modules, "network")["metric_relabel_configs"]
    assert relabel == [{"source_labels": ["device"], "regex": "eth0[.]100|wlan0", "action": "keep"}]


def test_network_exporter_reads_the_host_namespace_through_proc_of_pid_one(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    exporter = services(data, modules)["net-exporter"]
    assert "--path.procfs=/host/proc/1" in exporter["command"]
    assert "--no-collector.netdev.netlink" in exporter["command"]
    assert "--path.sysfs=/host/sys" in exporter["command"]
    assert "network_mode" not in exporter
    assert exporter["ports"] == ["127.0.0.1:9101:9101"]


def test_wifi_collector_runs_only_when_enabled_and_without_ports(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    wifi = services(data, modules)["wifi"]
    assert wifi["network_mode"] == "host"
    assert "ports" not in wifi
    assert wifi["image"].startswith("prom/node-exporter:")
    assert wifi["entrypoint"] == ["sh", "/wifi.sh"]
    assert wifi["user"] == "0:0"
    assert wifi["environment"]["AYE_WIFI_PORT"] == "19102"
    edge(data)["network"]["wifi_port"] = 19555
    assert services(data, modules)["wifi"]["environment"]["AYE_WIFI_PORT"] == "19555"
    edge(data)["network"]["wifi"] = False
    assert "wifi" not in services(data, modules)


def test_smart_exporter_runs_privileged_and_takes_explicit_devices(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    edge(data)["smart"] = {"devices": ["/dev/sda", "/dev/nvme0"], "interval": "5m"}
    exporter = services(data, modules)["smartctl-exporter"]
    assert (exporter["privileged"], exporter["user"]) == (True, "root")
    assert exporter["command"] == [
        "--smartctl.interval=5m",
        "--smartctl.device=/dev/sda",
        "--smartctl.device=/dev/nvme0",
    ]


def test_gpu_exporter_reserves_nvidia_gpus_with_the_utility_capability(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    exporter = services(data, modules)["nvidia-gpu-exporter"]
    devices = exporter["deploy"]["resources"]["reservations"]["devices"]
    assert devices == [{"driver": "nvidia", "count": "all", "capabilities": ["gpu"]}]
    assert exporter["environment"]["NVIDIA_DRIVER_CAPABILITIES"] == "utility"
    assert exporter["healthcheck"]["test"] == ["CMD", "nvidia-smi", "-L"]


def test_gpu_module_watches_nvidia_only_for_now(data: dict[str, Any]) -> None:
    edge(data)["gpu"] = {"vendor": "amd"}
    assert rules_hit(data) == {"module-settings"}


def test_disk_forecast_needs_the_node_module(data: dict[str, Any]) -> None:
    del edge(data)["node"]
    assert "module-requires" in rules_hit(data)


def test_disk_forecast_extrapolates_a_day_a_week_ahead(modules: dict[str, Module]) -> None:
    (alert,) = modules["disk-forecast"].alerts
    assert "predict_linear(node_filesystem_avail_bytes" in alert["expr"]
    assert "[1d], 7 * 86400" in alert["expr"]


def test_node_alerts_on_sustained_swap_out(modules: dict[str, Module]) -> None:
    alerts = {alert["name"]: alert for alert in modules["node"].alerts}
    swap = alerts["swap-out-sustained"]
    assert swap["expr"] == "rate(node_vmstat_pswpout[5m]) > bool 256"
    assert swap["for"] == "10m"


def test_hardware_alerts_cover_the_required_conditions(modules: dict[str, Module]) -> None:
    names = {
        alert["name"] for name in ("smart", "gpu", "network") for alert in modules[name].alerts
    }
    assert names == {
        "smart-failing",
        "smart-sectors-growing",
        "nvme-wear",
        "disk-temperature-high",
        "gpu-temperature-high",
        "gpu-memory-full",
        "gpu-collect-failing",
        "interface-down",
        "wifi-signal-weak",
        "network-errors",
        "link-speed-degraded",
    }


def fake_exporter(tmp_path: Path, body: str) -> Path:
    script = tmp_path / "node_exporter"
    args = (tmp_path / "args").as_posix()
    script.write_text(f'#!/bin/sh\necho "$@" > "{args}"\n{body}\n', "utf-8")
    script.chmod(0o755)
    return script


def started_fetch(tmp_path: Path) -> Path:
    script = tmp_path / "fetch"
    args = (tmp_path / "args").as_posix()
    script.write_text(
        f'#!/bin/sh\nwhile [ ! -s "{args}" ]; do sleep 0.1; done\nexec cat "$1"\n', "utf-8"
    )
    script.chmod(0o755)
    return script


def run_wifi(
    tmp_path: Path, metrics: str | None, exporter: str = "exec sleep 600", **extra: str
) -> subprocess.CompletedProcess[str]:
    shell = shutil.which("sh")
    if shell is None:
        pytest.skip("no POSIX shell")
    out = tmp_path / "textfile"
    out.mkdir()
    source = tmp_path / "metrics"
    if metrics is not None:
        source.write_text(metrics, encoding="utf-8")
    env = {
        **os.environ,
        "AYE_TEXTFILE_DIR": out.as_posix(),
        "AYE_WIFI_EXPORTER": fake_exporter(tmp_path, exporter).as_posix(),
        "AYE_WIFI_FETCH": started_fetch(tmp_path).as_posix(),
        "AYE_WIFI_URL": source.as_posix(),
        "AYE_WIFI_ONCE": "1",
        **extra,
    }
    script = (MODULES_DIR / "network" / "files" / "wifi.sh").as_posix()
    return subprocess.run(
        [shell, script], env=env, capture_output=True, text=True, check=False, timeout=WAIT
    )


def wifi_textfile(tmp_path: Path) -> list[str]:
    return (tmp_path / "textfile" / "wifi.prom").read_text(encoding="utf-8").splitlines()


def test_wifi_collector_keeps_the_nl80211_metrics_and_derives_the_alerted_signal(
    tmp_path: Path,
) -> None:
    done = run_wifi(tmp_path, WIFI_METRICS.read_text(encoding="utf-8"))
    assert done.returncode == 0, done.stderr
    assert wifi_textfile(tmp_path) == [
        'node_wifi_interface_frequency_hertz{device="wlp3s0"} 5.18e+09',
        'node_wifi_station_info{bssid="02:00:5e:10:00:01",device="wlp3s0",mode="client",'
        'ssid="Workshop 5G"} 1',
        'node_wifi_station_receive_bits_per_second{device="wlp3s0",'
        'mac_address="02:00:5e:10:00:01"} 4.333e+08',
        'node_wifi_station_signal_dbm{device="wlp3s0",mac_address="02:00:5e:10:00:01"} -62',
        'ayeops_wifi_signal_dbm{device="wlp3s0"} -62',
        'node_wifi_station_transmit_bits_per_second{device="wlp3s0",'
        'mac_address="02:00:5e:10:00:01"} 0',
    ]
    assert not (tmp_path / "textfile" / "wifi.raw").exists()


def test_wifi_collector_starts_only_the_wifi_collector_on_loopback(tmp_path: Path) -> None:
    done = run_wifi(tmp_path, "", AYE_WIFI_PORT="19555")
    assert done.returncode == 0, done.stderr
    assert (tmp_path / "args").read_text(encoding="utf-8").split() == [
        "--web.listen-address=127.0.0.1:19555",
        "--collector.disable-defaults",
        "--collector.wifi",
    ]


def test_wifi_collector_writes_an_empty_file_on_a_host_without_a_radio(tmp_path: Path) -> None:
    done = run_wifi(tmp_path, 'node_scrape_collector_success{collector="wifi"} 1\n')
    assert done.returncode == 0, done.stderr
    assert wifi_textfile(tmp_path) == []


def test_wifi_collector_fails_when_its_exporter_dies(tmp_path: Path) -> None:
    done = run_wifi(tmp_path, "", exporter="exit 1", AYE_WIFI_ONCE="0", AYE_WIFI_INTERVAL="1")
    assert done.returncode == 1
    assert done.stderr.strip().endswith("wifi: node_exporter exited")


def test_wifi_collector_gives_up_on_an_exporter_it_cannot_read(tmp_path: Path) -> None:
    done = run_wifi(tmp_path, None, AYE_WIFI_RETRIES="2")
    assert done.returncode == 1
    assert done.stderr.count("cannot read") == 2
    assert not (tmp_path / "textfile" / "wifi.prom").exists()
