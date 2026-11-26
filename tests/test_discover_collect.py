from __future__ import annotations

import ast
import json
import sys
from pathlib import Path
from typing import Any

from ayeops.discover import probe
from tests.test_discover_parsers import fixture

SECRET = "hunter2"

COMMANDS = {
    ("lsblk",): "lsblk.json",
    ("smartctl", "--scan"): "smartctl-scan.json",
    ("smartctl", "-i", "-H", "-j", "/dev/sda"): "smartctl-sda.json",
    ("smartctl", "-i", "-H", "-j", "/dev/nvme0n1"): "smartctl-nvme.json",
    ("ip", "-j", "addr"): "ip-addr.json",
    ("iw", "dev", "wlan0", "link"): "iw-link.txt",
    ("nvidia-smi",): "nvidia-smi.csv",
    ("docker", "compose", "ls"): "compose-ls.json",
    ("docker", "compose", "-p", "shop"): "compose-ps-shop.json",
    ("docker", "compose", "-p", "tools"): "compose-ps-tools.ndjson",
    ("systemctl", "list-units"): "systemctl-units.txt",
    ("systemctl", "list-timers"): "systemctl-timers.txt",
    ("ss",): "ss.txt",
}
INLINE = {
    ("docker", "version"): "28.5.1\n",
    ("docker", "ps"): "lone\tredis:8.2.2\trunning\t\nshop-db-1\tpostgres:17.6\trunning\tshop\n",
    ("systemd-detect-virt",): "none\n",
}


def fake_run(argv: list[str]) -> probe.Result:
    for table, read in ((COMMANDS, fixture), (INLINE, str)):
        for prefix, value in table.items():
            if tuple(argv[: len(prefix)]) == prefix:
                return probe.Result(0, read(value), "")
    return probe.Result(127, "", f"{argv[0]} is not installed")


def missing(argv: list[str]) -> probe.Result:
    return probe.Result(127, "", f"{argv[0]} is not installed")


def write(root: Path, relative: str, text: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def build_host(root: Path) -> None:
    write(root, "etc/os-release", fixture("os-release.txt"))
    write(root, "proc/sys/kernel/hostname", "Build-Box.local\n")
    write(root, "proc/sys/kernel/osrelease", "6.8.0-85-generic\n")
    write(root, "proc/uptime", "86400.12 170000.00\n")
    write(root, "proc/cpuinfo", fixture("cpuinfo-x86.txt"))
    write(root, "proc/meminfo", fixture("meminfo.txt"))
    write(root, "proc/self/mounts", fixture("mounts.txt"))
    for name, operstate, speed in (("enp3s0", "up", "1000"), ("wlan0", "up", "-1")):
        write(root, f"sys/class/net/{name}/operstate", operstate + "\n")
        write(root, f"sys/class/net/{name}/speed", speed + "\n")
        write(root, f"sys/class/net/{name}/carrier", "1\n")
        write(root, f"sys/class/net/{name}/device/vendor", "0x8086\n")
    write(root, "sys/class/net/wlan0/uevent", "DEVTYPE=wlan\nINTERFACE=wlan0\n")
    write(root, "sys/class/net/wlan0/phy80211/name", "phy0\n")
    write(root, "sys/class/net/tailscale0/operstate", "unknown\n")
    write(root, "sys/class/net/docker0/uevent", "DEVTYPE=bridge\n")
    write(root, "sys/class/net/docker0/operstate", "down\n")
    write(root, "sys/class/net/lo/operstate", "unknown\n")
    write(root, "sys/class/drm/card0/device/vendor", "0x10de\n")
    write(root, "sys/class/drm/card1/device/vendor", "0x8086\n")
    write(root, "sys/class/drm/card1/device/device", "0xa780\n")
    write(root, "sys/class/drm/card1-HDMI-A-1/status", "connected\n")
    write(root, "sys/class/power_supply/BAT0/type", "Battery\n")
    write(root, "sys/class/power_supply/BAT0/capacity", "81\n")
    write(root, "sys/class/power_supply/BAT0/status", "Charging\n")
    write(root, "sys/class/power_supply/AC/type", "Mains\n")
    write(root, "sys/class/power_supply/AC/online", "1\n")
    write(root, "sys/class/power_supply/hidpp_battery_0/type", "Battery\n")
    write(root, "sys/class/power_supply/hidpp_battery_0/scope", "Device\n")
    processes = (
        ("1234", "mysqld", "0::/system.slice/mysql.service", 3 * 1024 * 1024),
        ("1300", "php-fpm8.3", "0::/system.slice/php8.3-fpm.service", 512 * 1024),
        ("2000", "postgres", "0::/system.slice/docker-0a1b2c.scope", 256 * 1024),
    )
    for pid, comm, cgroup, rss_kb in processes:
        write(root, f"proc/{pid}/comm", comm + "\n")
        write(root, f"proc/{pid}/cgroup", cgroup + "\n")
        write(root, f"proc/{pid}/status", f"Name:\t{comm}\nVmRSS:\t{rss_kb} kB\n")
        write(root, f"proc/{pid}/environ", f"MYSQL_PASSWORD={SECRET}\0")
        write(root, f"proc/{pid}/cmdline", f"{comm}\0--password={SECRET}\0")
    write(root, "proc/2/comm", "kthreadd\n")
    write(root, "proc/2/status", "Name:\tkthreadd\n")
    write(root, "srv/shop/.env", f"DB_PASSWORD={SECRET}\n")


def collect(root: Path, run: probe.Runner = fake_run, is_root: bool = True) -> dict[str, Any]:
    build_host(root)
    return probe.collect(probe.Host(root=root, run=run, machine="x86_64", is_root=is_root))


def test_probe_reports_every_section_of_a_host(tmp_path: Path) -> None:
    report = collect(tmp_path)
    assert report["notes"] == []
    system = report["system"]
    assert isinstance(system, dict)
    assert (system["hostname"], system["arch"], system["os"]) == (
        "Build-Box.local",
        "amd64",
        "Ubuntu 24.04.3 LTS",
    )
    assert report["memory"] == probe.parse_meminfo(fixture("meminfo.txt"))
    assert {s["port"] for s in report["listening_tcp"]} >= {22, 80, 3306, 5432}


def test_disks_report_kind_and_smart_state(tmp_path: Path) -> None:
    storage = collect(tmp_path)["storage"]
    assert isinstance(storage, dict)
    disks = {d["name"]: d for d in storage["disks"]}
    assert set(disks) == {"nvme0n1", "sda"}
    assert disks["sda"]["kind"] == "hdd"
    assert disks["sda"]["smart"] == {
        "smartctl": True,
        "scanned": True,
        "available": True,
        "enabled": True,
        "passed": True,
    }
    assert disks["nvme0n1"]["smart"]["scanned"] is True
    assert disks["nvme0n1"]["smart"]["passed"] is False
    assert [f["mountpoint"] for f in storage["filesystems"]] == [
        "/",
        "/boot/efi",
        "/srv/data",
        "/mnt/nas share",
    ]
    assert storage["filesystems"][3]["network"] is True


def test_network_interfaces_carry_kind_state_speed_addresses_and_wifi(tmp_path: Path) -> None:
    network = collect(tmp_path)["network"]
    assert isinstance(network, list)
    links = {link["name"]: link for link in network}
    assert {name: links[name]["kind"] for name in links} == {
        "docker0": "virtual",
        "enp3s0": "wired",
        "lo": "loopback",
        "tailscale0": "vpn",
        "wlan0": "wifi",
    }
    assert links["enp3s0"]["speed_mbps"] == 1000
    assert links["enp3s0"]["addresses"] == ["192.168.1.20/24"]
    assert links["wlan0"]["speed_mbps"] is None
    assert links["wlan0"]["wifi"]["ssid"] == "HomeNet 5G"
    assert links["tailscale0"]["detail"] == "tailscale"


def test_gpus_come_from_nvidia_smi_and_drm_covers_other_vendors(tmp_path: Path) -> None:
    gpus = collect(tmp_path)["gpus"]
    assert isinstance(gpus, list)
    assert [(g["vendor"], g["source"]) for g in gpus] == [
        ("nvidia", "nvidia-smi"),
        ("nvidia", "nvidia-smi"),
        ("intel", "drm"),
    ]
    assert gpus[2]["model"] == "0x8086:0xa780"


def test_power_supplies_skip_peripheral_batteries(tmp_path: Path) -> None:
    supplies = collect(tmp_path)["power_supplies"]
    assert supplies == [
        {"name": "AC", "type": "Mains", "capacity_percent": None, "status": None, "online": True},
        {
            "name": "BAT0",
            "type": "Battery",
            "capacity_percent": 81,
            "status": "Charging",
            "online": None,
        },
    ]


def test_containers_list_compose_projects_and_standalone_containers(tmp_path: Path) -> None:
    containers = collect(tmp_path)["containers"]
    assert isinstance(containers, dict)
    assert [p["name"] for p in containers["compose_projects"]] == ["shop", "tools"]
    shop = containers["compose_projects"][0]
    assert [s["service"] for s in shop["services"]] == ["db", "web", "worker_queue"]
    assert containers["standalone_containers"] == [
        {"name": "lone", "image": "redis:8.2.2", "state": "running", "project": ""}
    ]


def test_apps_separate_host_processes_from_containers(tmp_path: Path) -> None:
    apps = collect(tmp_path)["apps"]
    assert isinstance(apps, list)
    by_name = {a["name"]: a for a in apps}
    assert by_name["mysql"]["host_process"] is True
    assert by_name["mysql"]["rss_bytes"] == 3 * 1024**3
    assert by_name["postgres"]["host_process"] is False
    assert {"nginx", "php-fpm", "redis", "valkey"} <= set(by_name)


def test_probe_never_outputs_environments_command_lines_or_env_files(tmp_path: Path) -> None:
    assert SECRET not in json.dumps(collect(tmp_path))


def test_probe_never_opens_environment_or_command_line_files() -> None:
    source = Path(probe.__file__).read_text(encoding="utf-8")
    for needle in ("/environ", "/cmdline", '".env', "'.env", "docker inspect", "{{json .}}"):
        assert needle not in source, needle


def test_every_probe_degrades_to_notes_without_tools_or_root(tmp_path: Path) -> None:
    report = collect(tmp_path, run=missing, is_root=False)
    assert report["memory"] == probe.parse_meminfo(fixture("meminfo.txt"))
    assert report["containers"] is None
    assert report["systemd"] is None
    storage = report["storage"]
    assert isinstance(storage, dict)
    assert storage["filesystems"]
    notes = report["notes"]
    assert isinstance(notes, list)
    assert any(n.startswith("lsblk:") for n in notes)
    assert any(n.startswith("smartctl:") for n in notes)
    assert any(n.startswith("ss:") for n in notes)
    assert any("nvidia-smi" in n for n in notes)


def test_without_root_smart_state_is_left_unread_and_noted(tmp_path: Path) -> None:
    report = collect(tmp_path, is_root=False)
    storage = report["storage"]
    assert isinstance(storage, dict)
    assert "available" not in storage["disks"][0]["smart"]
    assert any("run with --sudo" in n for n in report["notes"])


def test_a_crashing_section_becomes_a_note_and_the_rest_still_reports(tmp_path: Path) -> None:
    def broken(argv: list[str]) -> probe.Result:
        if argv[0] == "docker":
            raise RuntimeError("boom")
        return fake_run(argv)

    report = collect(tmp_path, run=broken)
    assert report["containers"] is None
    assert "containers: RuntimeError: boom" in report["notes"]
    assert report["gpus"]


def test_probe_is_python_3_10_stdlib_only_so_it_runs_over_ssh_without_install() -> None:
    source = Path(probe.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source, feature_version=(3, 10))
    modules = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        (node.module or "").split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }
    assert sorted(m for m in modules if m not in sys.stdlib_module_names) == []
