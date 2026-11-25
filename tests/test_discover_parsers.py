from __future__ import annotations

from pathlib import Path

from ayeops.discover import probe

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "discover"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_lsblk_tree_is_flattened_with_parents_and_mountpoints() -> None:
    devices = {d["name"]: d for d in probe.parse_lsblk(fixture("lsblk.json"))}
    assert devices["nvme0n1"]["type"] == "disk"
    assert devices["nvme0n1"]["mountpoints"] == []
    assert devices["nvme0n1p2"]["parent"] == "nvme0n1"
    assert devices["nvme0n1p2"]["mountpoints"] == ["/", "/var/lib/docker"]
    assert devices["sda"]["rotational"] is True
    assert devices["sda"]["size_bytes"] == 4000787030016
    assert devices["sda"]["model"] == "WDC WD40EFRX-68N32N0"


def test_old_lsblk_with_string_numbers_and_a_single_mountpoint_is_understood() -> None:
    devices = {d["name"]: d for d in probe.parse_lsblk(fixture("lsblk-old.json"))}
    assert devices["vda"]["size_bytes"] == 53687091200
    assert devices["vda"]["rotational"] is True
    assert devices["vda1"]["mountpoints"] == ["/"]


def test_disk_kind_tells_nvme_ssd_hdd_usb_and_virtual_apart() -> None:
    assert probe.disk_kind("nvme0n1", False, "nvme") == "nvme"
    assert probe.disk_kind("sda", True, "sata") == "hdd"
    assert probe.disk_kind("sdb", False, "sata") == "ssd"
    assert probe.disk_kind("sdc", False, "usb") == "usb"
    assert probe.disk_kind("vda", True, "") == "virtual"


def test_smartctl_scan_lists_devices_with_their_type() -> None:
    assert probe.parse_smartctl_scan(fixture("smartctl-scan.json")) == {
        "/dev/sda": "sat",
        "/dev/nvme0": "nvme",
    }


def test_smartctl_health_reports_support_and_verdict() -> None:
    assert probe.parse_smartctl_health(fixture("smartctl-sda.json")) == {
        "available": True,
        "enabled": True,
        "passed": True,
    }


def test_nvme_without_a_smart_support_block_counts_as_available() -> None:
    health = probe.parse_smartctl_health(fixture("smartctl-nvme.json"))
    assert health["available"] is True
    assert health["passed"] is False


def test_mounts_keep_real_filesystems_and_drop_pseudo_container_and_duplicate_ones() -> None:
    mounts = probe.parse_mounts(fixture("mounts.txt"))
    assert [(m["mountpoint"], m["fstype"]) for m in mounts] == [
        ("/", "ext4"),
        ("/boot/efi", "vfat"),
        ("/srv/data", "xfs"),
        ("/mnt/nas share", "nfs4"),
    ]


def test_ss_output_gives_ports_with_process_names() -> None:
    sockets = {(s["address"], s["port"]): s["processes"] for s in probe.parse_ss(fixture("ss.txt"))}
    assert sockets[("127.0.0.53", 53)] == ["systemd-resolve"]
    assert sockets[("127.0.0.1", 5432)] == ["postgres"]
    assert sockets[("0.0.0.0", 80)] == ["nginx"]
    assert sockets[("::", 22)] == ["sshd"]
    assert sockets[("*", 9100)] == []


def test_proc_net_tcp_lists_only_listening_sockets_in_both_families() -> None:
    sockets = probe.parse_proc_net_tcp(fixture("proc-net-tcp.txt"))
    sockets += probe.parse_proc_net_tcp(fixture("proc-net-tcp6.txt"))
    assert {(s["address"], s["port"]) for s in sockets} == {
        ("127.0.0.1", 5432),
        ("0.0.0.0", 80),
        ("::", 22),
        ("::ffff:127.0.0.1", 6379),
    }


def test_nvidia_smi_csv_gives_model_memory_and_driver() -> None:
    gpus = probe.parse_nvidia_smi(fixture("nvidia-smi.csv"))
    assert [g["model"] for g in gpus] == ["NVIDIA GeForce RTX 4090", "NVIDIA RTX A4000"]
    assert gpus[0]["memory_bytes"] == 24564 * 1024**2
    assert gpus[0]["driver"] == "nvidia 565.57.01"
    assert gpus[1]["pci_bus_id"] == "00000000:41:00.0"


def test_rocm_smi_json_gives_amd_cards_and_skips_the_system_block() -> None:
    assert probe.parse_rocm_smi(fixture("rocm-smi.json")) == [
        {
            "vendor": "amd",
            "index": 0,
            "model": "Radeon RX 7900 XTX",
            "memory_bytes": 25753026560,
            "driver": "amdgpu",
            "source": "rocm-smi",
        }
    ]


def test_iw_link_gives_ssid_signal_frequency_and_bitrate() -> None:
    assert probe.parse_iw_link(fixture("iw-link.txt")) == {
        "connected": True,
        "ssid": "HomeNet 5G",
        "signal_dbm": -58,
        "freq_mhz": 5180,
        "tx_bitrate_mbps": 780.0,
        "source": "iw",
    }


def test_iw_reports_a_disconnected_interface() -> None:
    assert probe.parse_iw_link("Not connected.\n") == {"connected": False, "source": "iw"}


def test_nmcli_picks_the_network_in_use_and_unescapes_colons() -> None:
    assert probe.parse_nmcli_wifi(fixture("nmcli-wifi.txt")) == {
        "connected": True,
        "ssid": "Home:Net 5G",
        "signal_percent": 72,
        "freq_mhz": 5180,
        "source": "nmcli",
    }


def test_proc_net_wireless_gives_quality_and_signal_per_interface() -> None:
    links = probe.parse_proc_wireless(fixture("proc-net-wireless.txt"))
    assert links["wlan0"] == {
        "connected": True,
        "link_quality": 52,
        "signal_dbm": -58,
        "source": "proc",
    }
    assert links["wlan1"]["connected"] is False


def test_ip_addr_keeps_global_addresses_only() -> None:
    assert probe.parse_ip_addr(fixture("ip-addr.json")) == {
        "lo": [],
        "enp3s0": ["192.168.1.20/24"],
        "wlan0": [],
        "tailscale0": ["100.101.102.103/32"],
    }


def test_interfaces_are_classified_as_wired_wifi_vpn_virtual_or_loopback() -> None:
    classify = probe.classify_interface
    assert classify("lo", "", False, False) == ("loopback", "")
    assert classify("enp3s0", "", False, True) == ("wired", "")
    assert classify("wlp2s0", "wlan", True, True) == ("wifi", "")
    assert classify("tailscale0", "", False, False) == ("vpn", "tailscale")
    assert classify("wg0", "wireguard", False, False) == ("vpn", "wireguard")
    assert classify("office", "wireguard", False, False) == ("vpn", "wireguard")
    assert classify("docker0", "bridge", False, False) == ("virtual", "container")
    assert classify("veth12ab", "", False, False) == ("virtual", "container")
    assert classify("virbr0", "bridge", False, False) == ("virtual", "bridge")
    assert classify("bond0", "bond", False, False) == ("wired", "bond")
    assert classify("br0", "bridge", False, False) == ("virtual", "bridge")


def test_compose_ls_splits_config_files() -> None:
    assert probe.parse_compose_ls(fixture("compose-ls.json")) == [
        {
            "name": "shop",
            "status": "running(3)",
            "config_files": ["/srv/shop/compose.yaml", "/srv/shop/compose.override.yaml"],
        },
        {"name": "tools", "status": "exited(1)", "config_files": ["/opt/tools/docker-compose.yml"]},
    ]


def test_compose_ps_keeps_service_image_state_and_published_ports_only() -> None:
    services = probe.parse_compose_ps(fixture("compose-ps-shop.json"))
    assert services[1] == {
        "service": "web",
        "container": "shop-web-1",
        "image": "nginx:1.29.2",
        "state": "running",
        "health": "",
        "published_ports": [8080],
        "ports": [
            {"published": 8080, "target": 80, "bind": "0.0.0.0", "protocol": "tcp"},
            {"published": 8080, "target": 80, "bind": "::", "protocol": "tcp"},
        ],
    }
    assert all(set(s) == set(services[1]) for s in services)
    assert "hunter2" not in repr(services)


def test_compose_ps_keeps_bind_address_and_container_port_of_each_published_port() -> None:
    services = {s["service"]: s for s in probe.parse_compose_ps(fixture("compose-ps-shop.json"))}
    assert services["db"]["ports"] == []
    assert services["worker_queue"]["ports"] == [
        {"published": 9001, "target": 9000, "bind": "127.0.0.1", "protocol": "tcp"}
    ]


def test_published_ports_default_the_protocol_and_skip_unpublished_ones() -> None:
    publishers = [
        {"URL": "", "TargetPort": 53, "PublishedPort": 5353, "Protocol": "udp"},
        {"URL": "", "TargetPort": 80, "PublishedPort": 0},
        {"URL": None, "PublishedPort": "8443"},
    ]
    assert probe.published_ports(publishers) == [
        {"published": 5353, "target": 53, "bind": "", "protocol": "udp"},
        {"published": 8443, "target": 8443, "bind": "", "protocol": "tcp"},
    ]
    assert probe.published_ports(None) == []


def test_compose_ps_reads_newline_delimited_json_of_newer_compose() -> None:
    services = probe.parse_compose_ps(fixture("compose-ps-tools.ndjson"))
    assert [(s["service"], s["image"]) for s in services] == [
        ("cache", "valkey/valkey:8.1.4"),
        ("db", "busybox:1.37.0"),
    ]
    assert "hunter2" not in repr(services)


def test_docker_ps_template_lines_give_name_image_state_and_project() -> None:
    text = "shop-db-1\tpostgres:17.6\trunning\tshop\nlone\tredis:8.2.2\texited\t\n"
    assert probe.parse_docker_ps(text) == [
        {"name": "shop-db-1", "image": "postgres:17.6", "state": "running", "project": "shop"},
        {"name": "lone", "image": "redis:8.2.2", "state": "exited", "project": ""},
    ]


def test_meminfo_is_converted_to_bytes() -> None:
    assert probe.parse_meminfo(fixture("meminfo.txt")) == {
        "total_bytes": 263842716 * 1024,
        "available_bytes": 18874368 * 1024,
        "swap_total_bytes": 8388604 * 1024,
        "swap_free_bytes": 1048576 * 1024,
    }


def test_cpuinfo_counts_threads_cores_and_sockets() -> None:
    assert probe.parse_cpuinfo(fixture("cpuinfo-x86.txt")) == {
        "model": "AMD Ryzen 9 7950X 16-Core Processor",
        "threads": 4,
        "cores": 2,
        "sockets": 1,
    }


def test_arm_cpuinfo_without_a_model_name_falls_back_to_lscpu() -> None:
    assert probe.parse_cpuinfo(fixture("cpuinfo-arm.txt"))["model"] == ""
    assert probe.parse_lscpu_json(fixture("lscpu.json")) == "Neoverse-N1"


def test_os_release_gives_id_version_and_pretty_name() -> None:
    assert probe.parse_os_release(fixture("os-release.txt")) == {
        "id": "ubuntu",
        "version_id": "24.04",
        "pretty_name": "Ubuntu 24.04.3 LTS",
    }


def test_running_units_skip_distro_defaults_but_keep_what_was_installed() -> None:
    units = [
        u
        for u in probe.parse_unit_list(fixture("systemctl-units.txt"))
        if not probe.is_distro_unit(u)
    ]
    assert units == [
        "backup-agent.service",
        "containerd.service",
        "docker.service",
        "mysql.service",
        "php8.3-fpm.service",
        "smartd.service",
    ]


def test_timers_keep_custom_ones_and_what_they_activate() -> None:
    timers = [
        t
        for t in probe.parse_timers(fixture("systemctl-timers.txt"))
        if not probe.is_distro_unit(t["timer"])
    ]
    assert timers == [
        {"timer": "backup.timer", "activates": "backup.service"},
        {"timer": "certbot.timer", "activates": "certbot.service"},
    ]


def test_rss_comes_from_proc_status_and_kernel_threads_have_none() -> None:
    assert probe.parse_status_rss("Name:\tmysqld\nVmRSS:\t  2048 kB\n") == 2048 * 1024
    assert probe.parse_status_rss("Name:\tkthreadd\nThreads:\t1\n") is None


def test_apps_are_recognised_from_processes_ports_and_container_images() -> None:
    processes = [
        {"name": "mysqld", "in_container": False, "count": 1, "rss_bytes": 3 * 1024**3},
        {"name": "php-fpm8.3", "in_container": False, "count": 9, "rss_bytes": 1024**3},
        {"name": "postgres", "in_container": True, "count": 7, "rss_bytes": 1024**3},
        {"name": "bash", "in_container": False, "count": 2, "rss_bytes": 1024},
    ]
    ports = [{"address": "127.0.0.1", "port": 5432, "processes": []}]
    containers = {
        "compose_projects": [
            {"name": "shop", "services": probe.parse_compose_ps(fixture("compose-ps-shop.json"))}
        ],
        "standalone_containers": [{"name": "cache", "image": "valkey/valkey:8.1.4"}],
    }
    apps = {a["name"]: a for a in probe.detect_apps(processes, ports, containers)}
    assert set(apps) == {"mysql", "php-fpm", "postgres", "nginx", "valkey"}
    assert apps["mysql"]["host_process"] is True
    assert apps["mysql"]["rss_bytes"] == 3 * 1024**3
    assert apps["postgres"]["host_process"] is False
    assert apps["postgres"]["evidence"] == [
        "container shop/db",
        "port 5432",
        "process postgres (container)",
    ]


def test_a_php_image_counts_as_php_fpm_only_when_it_is_an_fpm_image() -> None:
    assert probe.image_app("php:8.3-fpm-alpine") == "php-fpm"
    assert probe.image_app("php:8.3-cli") is None
    assert probe.image_app("ghcr.io/acme/postgres:17@sha256:abc") == "postgres"
