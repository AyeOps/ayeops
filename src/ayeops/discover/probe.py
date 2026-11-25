from __future__ import annotations

import ipaddress
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, NamedTuple

PROBE_VERSION = 1
COMMAND_TIMEOUT = 20
PASSED_ENV = ("PATH", "HOME", "DOCKER_HOST", "DOCKER_CONTEXT", "XDG_RUNTIME_DIR")
DEFAULT_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

ARCHES = {
    "x86_64": "amd64",
    "amd64": "amd64",
    "aarch64": "arm64",
    "arm64": "arm64",
    "armv7l": "arm",
    "armv6l": "arm",
    "riscv64": "riscv64",
}

PCI_VENDORS = {
    "0x10de": "nvidia",
    "0x1002": "amd",
    "0x8086": "intel",
    "0x1a03": "aspeed",
    "0x102b": "matrox",
    "0x1af4": "virtio",
    "0x15ad": "vmware",
    "0x1234": "qemu",
}

PSEUDO_FS = frozenset(
    {
        "autofs",
        "binfmt_misc",
        "bpf",
        "cgroup",
        "cgroup2",
        "configfs",
        "debugfs",
        "devpts",
        "devtmpfs",
        "efivarfs",
        "fuse.gvfsd-fuse",
        "fuse.portal",
        "fusectl",
        "hugetlbfs",
        "mqueue",
        "nsfs",
        "overlay",
        "proc",
        "pstore",
        "ramfs",
        "rpc_pipefs",
        "securityfs",
        "selinuxfs",
        "squashfs",
        "sysfs",
        "tmpfs",
        "tracefs",
        "nfsd",
    }
)
NETWORK_FS = frozenset(
    {"nfs", "nfs4", "cifs", "smb3", "smbfs", "fuse.sshfs", "sshfs", "glusterfs", "ceph", "9p"}
)
SKIPPED_MOUNT_PREFIXES = (
    "/proc",
    "/sys",
    "/dev",
    "/run/docker",
    "/run/containerd",
    "/var/lib/docker",
    "/var/lib/containers",
    "/var/lib/kubelet",
    "/snap",
)

VIRTUAL_PREFIXES = (
    "docker",
    "br-",
    "veth",
    "virbr",
    "vnet",
    "cni",
    "flannel",
    "cali",
    "cilium",
    "vxlan",
    "podman",
    "lxc",
    "lxd",
    "kube",
    "dummy",
)
TUNNEL_PREFIXES = {
    "tailscale": "tailscale",
    "wg": "wireguard",
    "zt": "zerotier",
    "nebula": "nebula",
    "tun": "tunnel",
    "tap": "tunnel",
    "ppp": "ppp",
    "ipsec": "ipsec",
}

DISTRO_UNIT_PREFIXES = (
    "systemd-",
    "getty@",
    "serial-getty@",
    "user@",
    "user-runtime-dir@",
    "dbus",
    "snap.",
    "snapd",
    "apt-",
    "dnf-",
    "packagekit",
    "unattended-upgrades",
    "NetworkManager",
    "ModemManager",
    "wpa_supplicant",
    "polkit",
    "accounts-daemon",
    "avahi-daemon",
    "bluetooth",
    "cups",
    "rsyslog",
    "syslog",
    "cron",
    "crond",
    "anacron",
    "atd",
    "chrony",
    "ntp",
    "auditd",
    "irqbalance",
    "firewalld",
    "ufw",
    "udisks2",
    "upower",
    "thermald",
    "fwupd",
    "rtkit-daemon",
    "switcheroo-control",
    "power-profiles-daemon",
    "colord",
    "gdm",
    "lightdm",
    "sddm",
    "gssproxy",
    "rpcbind",
    "multipathd",
    "lvm2",
    "mdmonitor",
    "mdcheck",
    "dm-event",
    "iscsid",
    "open-iscsi",
    "cloud-",
    "google-",
    "waagent",
    "walinuxagent",
    "amazon-ssm-agent",
    "snap-",
    "qemu-guest-agent",
    "open-vm-tools",
    "vmtoolsd",
    "vgauth",
    "hv-",
    "ssh",
    "sshd",
    "plymouth",
    "kmod",
    "kerneloops",
    "whoopsie",
    "apport",
    "networkd-dispatcher",
    "rngd",
    "haveged",
    "abrt",
    "sssd",
    "realmd",
    "tuned",
    "mcelog",
    "lm_sensors",
    "alsa-",
    "pipewire",
    "wireplumber",
    "pulseaudio",
    "geoclue",
    "low-memory-monitor",
    "iio-sensor-proxy",
    "bolt",
    "fprintd",
    "mlocate",
    "plocate",
    "man-db",
    "logrotate",
    "e2scrub",
    "fstrim",
    "motd-news",
    "shadow",
    "dpkg-db-backup",
    "sysstat",
    "raid-check",
    "unbound-anchor",
    "update-notifier",
    "ua-",
    "ubuntu-advantage",
    "esm-cache",
    "phpsessionclean",
    "pmie",
    "pmlogger",
    "akmods",
    "rpm-ostree",
    "flatpak",
    "lvm2-",
    "uuidd",
    "zfs-",
    "nvidia-persistenced",
    "nvidia-powerd",
    "dracut",
    "selinux",
    "setroubleshootd",
    "libstoragemgmt",
    "mdadm",
)

APP_PROCESSES: tuple[tuple[str, str], ...] = (
    ("postgres", r"postgres|postmaster"),
    ("mysql", r"mysqld"),
    ("mariadb", r"mariadbd"),
    ("redis", r"redis-server"),
    ("valkey", r"valkey-server"),
    ("nginx", r"nginx"),
    ("caddy", r"caddy"),
    ("traefik", r"traefik"),
    ("php-fpm", r"php-fpm.*"),
    ("node", r"node|nodejs"),
    ("java", r"java"),
    ("grafana", r"grafana(-server)?"),
    ("prometheus", r"prometheus"),
    ("victoriametrics", r"victoria-metric.*|vmagent|vmstorage|vminsert|vmselect"),
    ("mongodb", r"mongod"),
    ("memcached", r"memcached"),
    ("haproxy", r"haproxy"),
    ("apache", r"apache2|httpd"),
    ("rabbitmq", r"beam\.smp"),
    ("clickhouse", r"clickhouse.*"),
    ("minio", r"minio"),
    ("loki", r"loki"),
    ("influxdb", r"influxd"),
    ("etcd", r"etcd"),
    ("pgbouncer", r"pgbouncer"),
    ("gunicorn", r"gunicorn"),
    ("uwsgi", r"uwsgi"),
    ("docker", r"dockerd"),
    ("kubernetes", r"kubelet|k3s.*|k0s"),
)
APP_PORTS = {
    5432: "postgres",
    6432: "pgbouncer",
    3306: "mysql",
    6379: "redis",
    11211: "memcached",
    27017: "mongodb",
    9200: "elasticsearch",
    5672: "rabbitmq",
    9090: "prometheus",
    8428: "victoriametrics",
    8123: "clickhouse",
    2019: "caddy",
    9092: "kafka",
    2379: "etcd",
    8086: "influxdb",
    3100: "loki",
}
APP_IMAGES: tuple[tuple[str, str], ...] = (
    ("postgres", r"postgres|postgis|timescaledb.*"),
    ("mysql", r"mysql|percona.*"),
    ("mariadb", r"mariadb"),
    ("redis", r"redis.*"),
    ("valkey", r"valkey"),
    ("nginx", r"nginx.*|openresty"),
    ("caddy", r"caddy"),
    ("traefik", r"traefik"),
    ("grafana", r"grafana"),
    ("prometheus", r"prometheus"),
    ("victoriametrics", r"victoria-metrics|vmagent|vmalert"),
    ("mongodb", r"mongo.*"),
    ("memcached", r"memcached"),
    ("haproxy", r"haproxy"),
    ("apache", r"httpd"),
    ("rabbitmq", r"rabbitmq"),
    ("clickhouse", r"clickhouse-server|clickhouse"),
    ("minio", r"minio"),
    ("loki", r"loki"),
    ("elasticsearch", r"elasticsearch|opensearch"),
    ("kafka", r"kafka|cp-kafka|redpanda"),
    ("influxdb", r"influxdb"),
    ("php-fpm", r"php"),
)
CONTAINER_CGROUP = re.compile(r"docker|containerd|libpod|kubepods|lxc|garden")
DOCKER_PS_FORMAT = '{{.Names}}\t{{.Image}}\t{{.State}}\t{{.Label "com.docker.compose.project"}}'


class Result(NamedTuple):
    code: int
    out: str
    err: str


Runner = Callable[[list[str]], Result]


def command_env() -> dict[str, str]:
    env = {name: os.environ[name] for name in PASSED_ENV if name in os.environ}
    env.setdefault("PATH", DEFAULT_PATH)
    env["LC_ALL"] = "C"
    return env


def run_command(argv: list[str]) -> Result:
    if shutil.which(argv[0], path=command_env()["PATH"]) is None:
        return Result(127, "", f"{argv[0]} is not installed")
    try:
        done = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=COMMAND_TIMEOUT,
            env=command_env(),
            stdin=subprocess.DEVNULL,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        return Result(126, "", str(error))
    return Result(done.returncode, done.stdout, done.stderr.strip())


def effective_root() -> bool:
    geteuid = getattr(os, "geteuid", None)
    return bool(geteuid is not None and geteuid() == 0)


class Host:
    def __init__(
        self,
        root: Path = Path("/"),
        run: Runner = run_command,
        machine: str | None = None,
        is_root: bool | None = None,
    ) -> None:
        self.root = root
        self.runner = run
        self.machine = machine or platform.machine()
        self.is_root = effective_root() if is_root is None else is_root
        self.notes: list[str] = []

    def path(self, relative: str) -> Path:
        return self.root / relative.lstrip("/")

    def read(self, relative: str) -> str | None:
        try:
            return self.path(relative).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None

    def value(self, relative: str) -> str:
        return (self.read(relative) or "").strip()

    def listdir(self, relative: str) -> list[str]:
        try:
            return sorted(entry.name for entry in self.path(relative).iterdir())
        except OSError:
            return []

    def exists(self, relative: str) -> bool:
        return self.path(relative).exists()

    def link_name(self, relative: str) -> str:
        try:
            return self.path(relative).readlink().name
        except OSError:
            return ""

    def run(self, *argv: str) -> Result:
        return self.runner(list(argv))

    def note(self, text: str) -> None:
        self.notes.append(text)

    def statvfs(self, mountpoint: str) -> tuple[int, int, int] | None:
        stat = getattr(os, "statvfs", None)
        if stat is None:
            return None
        try:
            result = stat(self.path(mountpoint))
        except OSError:
            return None
        size = result.f_blocks * result.f_frsize
        free = result.f_bfree * result.f_frsize
        available = result.f_bavail * result.f_frsize
        return size, size - free, available


def first_line(text: str) -> str:
    return text.strip().splitlines()[0] if text.strip() else ""


def to_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(float(str(value).strip()))
    except ValueError:
        return None


def to_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in {"1", "true", "yes"}:
        return True
    if text in {"0", "false", "no"}:
        return False
    return None


def clean(value: Any) -> str:
    return " ".join(str(value or "").split())


def parse_key_values(text: str, separator: str = "=") -> dict[str, str]:
    values: dict[str, str] = {}
    for line in text.splitlines():
        key, found, value = line.partition(separator)
        if found and key.strip() and not key.lstrip().startswith("#"):
            values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def parse_os_release(text: str) -> dict[str, str]:
    values = parse_key_values(text)
    return {
        "id": values.get("ID", ""),
        "version_id": values.get("VERSION_ID", ""),
        "pretty_name": values.get("PRETTY_NAME", values.get("NAME", "")),
    }


def parse_cpuinfo(text: str) -> dict[str, Any]:
    threads = 0
    model = ""
    cores: set[tuple[str, str]] = set()
    sockets: set[str] = set()
    physical = ""
    for line in text.splitlines():
        key, found, value = line.partition(":")
        if not found:
            continue
        key = key.strip().lower()
        value = value.strip()
        if key == "processor":
            threads += 1
        elif key in {"model name", "cpu model", "model", "hardware"} and not model:
            if key != "model" or not value.isdigit():
                model = clean(value)
        elif key == "physical id":
            physical = value
            sockets.add(value)
        elif key == "core id":
            cores.add((physical, value))
    return {
        "model": model,
        "threads": threads,
        "cores": len(cores) or threads,
        "sockets": len(sockets) or 1,
    }


def lscpu_fields(entries: list[dict[str, Any]]) -> dict[str, str]:
    fields: dict[str, str] = {}
    for entry in entries:
        fields.setdefault(str(entry.get("field", "")).rstrip(":"), str(entry.get("data", "")))
        fields.update(
            {k: v for k, v in lscpu_fields(entry.get("children") or []).items() if k not in fields}
        )
    return fields


def parse_lscpu_json(text: str) -> str:
    fields = lscpu_fields(json.loads(text).get("lscpu") or [])
    return clean(fields.get("Model name", ""))


def parse_meminfo(text: str) -> dict[str, int]:
    values: dict[str, int] = {}
    for line in text.splitlines():
        key, found, value = line.partition(":")
        parts = value.split()
        if not found or not parts or not parts[0].isdigit():
            continue
        factor = 1024 if len(parts) > 1 and parts[1].lower() == "kb" else 1
        values[key.strip()] = int(parts[0]) * factor
    return {
        "total_bytes": values.get("MemTotal", 0),
        "available_bytes": values.get("MemAvailable", values.get("MemFree", 0)),
        "swap_total_bytes": values.get("SwapTotal", 0),
        "swap_free_bytes": values.get("SwapFree", 0),
    }


def parse_lsblk(text: str) -> list[dict[str, Any]]:
    devices: list[dict[str, Any]] = []

    def walk(nodes: list[dict[str, Any]], parent: str | None) -> None:
        for node in nodes:
            mounts = node.get("mountpoints")
            if mounts is None:
                mounts = [node.get("mountpoint")]
            devices.append(
                {
                    "name": str(node.get("name", "")),
                    "type": str(node.get("type", "")),
                    "size_bytes": to_int(node.get("size")),
                    "rotational": to_bool(node.get("rota")),
                    "model": clean(node.get("model")),
                    "transport": clean(node.get("tran")),
                    "fstype": clean(node.get("fstype")),
                    "mountpoints": [m for m in mounts if m],
                    "parent": parent,
                }
            )
            walk(node.get("children") or [], str(node.get("name", "")))

    walk(json.loads(text).get("blockdevices") or [], None)
    return devices


def disk_kind(name: str, rotational: bool | None, transport: str) -> str:
    if name.startswith("nvme"):
        return "nvme"
    if name.startswith(("vd", "xvd")):
        return "virtual"
    if transport == "usb":
        return "usb"
    if rotational:
        return "hdd"
    return "ssd"


def parse_smartctl_scan(text: str) -> dict[str, str]:
    devices = json.loads(text).get("devices") or []
    return {str(d.get("name", "")): str(d.get("type", "")) for d in devices if d.get("name")}


def parse_smartctl_health(text: str) -> dict[str, Any]:
    data = json.loads(text)
    support = data.get("smart_support") or {}
    status = data.get("smart_status") or {}
    available = support.get("available")
    if available is None and str((data.get("device") or {}).get("type", "")).startswith("nvme"):
        available = True
    passed = status.get("passed")
    return {
        "available": available if isinstance(available, bool) else None,
        "enabled": support.get("enabled") if isinstance(support.get("enabled"), bool) else None,
        "passed": passed if isinstance(passed, bool) else None,
    }


def unescape_mount(text: str) -> str:
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), text)


def parse_mounts(text: str) -> list[dict[str, str]]:
    mounts: list[dict[str, str]] = []
    seen: set[str] = set()
    for line in text.splitlines():
        fields = line.split()
        if len(fields) < 3:
            continue
        device, mountpoint, fstype = unescape_mount(fields[0]), unescape_mount(fields[1]), fields[2]
        if fstype in PSEUDO_FS or mountpoint in seen:
            continue
        if any(mountpoint == p or mountpoint.startswith(p + "/") for p in SKIPPED_MOUNT_PREFIXES):
            continue
        seen.add(mountpoint)
        mounts.append({"device": device, "mountpoint": mountpoint, "fstype": fstype})
    return mounts


def classify_interface(name: str, devtype: str, wireless: bool, physical: bool) -> tuple[str, str]:
    if name == "lo":
        return "loopback", ""
    if wireless or devtype == "wlan":
        return "wifi", ""
    if devtype == "wireguard":
        return "vpn", "wireguard"
    for prefix, detail in TUNNEL_PREFIXES.items():
        if name.startswith(prefix):
            return "vpn", detail
    if name.startswith(VIRTUAL_PREFIXES):
        return "virtual", "container" if name.startswith(("docker", "br-", "veth")) else "bridge"
    if devtype in {"bond", "vlan", "team"}:
        return "wired", devtype
    if devtype in {"bridge", "macvlan", "ipvlan", "vxlan", "tun"}:
        return "virtual", devtype
    if physical:
        return "wired", ""
    return "virtual", devtype


def parse_ip_addr(text: str) -> dict[str, list[str]]:
    addresses: dict[str, list[str]] = {}
    for link in json.loads(text):
        found = [
            f"{info['local']}/{info.get('prefixlen', '')}".rstrip("/")
            for info in link.get("addr_info") or []
            if info.get("local") and info.get("scope") not in {"link", "host"}
        ]
        addresses[str(link.get("ifname", ""))] = found
    return addresses


def parse_iw_link(text: str) -> dict[str, Any]:
    if text.strip().startswith("Not connected"):
        return {"connected": False, "source": "iw"}
    ssid = re.search(r"^\s*SSID:\s*(.*)$", text, re.MULTILINE)
    signal = re.search(r"^\s*signal:\s*(-?[0-9]+)", text, re.MULTILINE)
    freq = re.search(r"^\s*freq:\s*([0-9.]+)", text, re.MULTILINE)
    bitrate = re.search(r"^\s*tx bitrate:\s*([0-9.]+)\s*MBit/s", text, re.MULTILINE)
    return {
        "connected": ssid is not None or signal is not None,
        "ssid": ssid.group(1).strip() if ssid else None,
        "signal_dbm": int(signal.group(1)) if signal else None,
        "freq_mhz": int(float(freq.group(1))) if freq else None,
        "tx_bitrate_mbps": float(bitrate.group(1)) if bitrate else None,
        "source": "iw",
    }


def split_nmcli(line: str) -> list[str]:
    return [p.replace("\\:", ":").replace("\\\\", "\\") for p in re.split(r"(?<!\\):", line)]


def parse_nmcli_wifi(text: str) -> dict[str, Any]:
    for line in text.splitlines():
        fields = split_nmcli(line)
        if len(fields) >= 4 and fields[0].strip() == "*":
            return {
                "connected": True,
                "ssid": fields[1],
                "signal_percent": to_int(fields[2]),
                "freq_mhz": to_int(fields[3].split()[0]) if fields[3].split() else None,
                "source": "nmcli",
            }
    return {"connected": False, "source": "nmcli"}


def parse_proc_wireless(text: str) -> dict[str, dict[str, Any]]:
    links: dict[str, dict[str, Any]] = {}
    for line in text.splitlines()[2:]:
        name, found, rest = line.partition(":")
        fields = rest.split()
        if not found or len(fields) < 3:
            continue
        quality = to_int(fields[1].rstrip("."))
        level = to_int(fields[2].rstrip("."))
        links[name.strip()] = {
            "connected": bool(quality),
            "link_quality": quality,
            "signal_dbm": level,
            "source": "proc",
        }
    return links


def parse_nvidia_smi(text: str) -> list[dict[str, Any]]:
    gpus: list[dict[str, Any]] = []
    for line in text.splitlines():
        fields = [f.strip() for f in line.split(",")]
        if len(fields) < 5:
            continue
        memory = to_int(fields[2])
        gpus.append(
            {
                "vendor": "nvidia",
                "index": to_int(fields[0]),
                "model": fields[1],
                "memory_bytes": memory * 1024**2 if memory is not None else None,
                "driver": f"nvidia {fields[3]}",
                "pci_bus_id": fields[4],
                "source": "nvidia-smi",
            }
        )
    return gpus


def parse_rocm_smi(text: str) -> list[dict[str, Any]]:
    gpus: list[dict[str, Any]] = []
    for card, values in sorted(json.loads(text).items()):
        if not card.startswith("card") or not isinstance(values, dict):
            continue
        model = values.get("Card Series") or values.get("Card series") or values.get("Card model")
        gpus.append(
            {
                "vendor": "amd",
                "index": to_int(card[4:]),
                "model": clean(model),
                "memory_bytes": to_int(values.get("VRAM Total Memory (B)")),
                "driver": "amdgpu",
                "source": "rocm-smi",
            }
        )
    return gpus


def parse_compose_ls(text: str) -> list[dict[str, Any]]:
    projects = json.loads(text or "[]") or []
    return [
        {
            "name": str(p.get("Name", "")),
            "status": str(p.get("Status", "")),
            "config_files": [f for f in str(p.get("ConfigFiles", "")).split(",") if f],
        }
        for p in projects
    ]


def json_records(text: str) -> list[dict[str, Any]]:
    text = text.strip()
    if not text:
        return []
    if text.startswith("["):
        return list(json.loads(text))
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def published_ports(publishers: Any) -> list[dict[str, Any]]:
    found = set()
    for item in publishers or []:
        published = to_int(item.get("PublishedPort"))
        if not published:
            continue
        found.add(
            (
                published,
                to_int(item.get("TargetPort")) or published,
                clean(item.get("URL")),
                clean(item.get("Protocol")) or "tcp",
            )
        )
    return [
        {"published": published, "target": target, "bind": bind, "protocol": protocol}
        for published, target, bind, protocol in sorted(found)
    ]


def parse_compose_ps(text: str) -> list[dict[str, Any]]:
    services: list[dict[str, Any]] = []
    for record in json_records(text):
        ports = published_ports(record.get("Publishers"))
        published = sorted({port["published"] for port in ports})
        services.append(
            {
                "service": str(record.get("Service", "")),
                "container": str(record.get("Name", "")),
                "image": str(record.get("Image", "")),
                "state": str(record.get("State", "")),
                "health": str(record.get("Health", "")),
                "published_ports": published,
                "ports": ports,
            }
        )
    return sorted(services, key=lambda s: (s["service"], s["container"]))


def parse_docker_ps(text: str) -> list[dict[str, str]]:
    containers: list[dict[str, str]] = []
    for line in text.splitlines():
        fields = line.split("\t")
        if len(fields) < 3 or not fields[0]:
            continue
        project = fields[3] if len(fields) > 3 else ""
        containers.append(
            {"name": fields[0], "image": fields[1], "state": fields[2], "project": project}
        )
    return containers


def unit_base(unit: str) -> str:
    for suffix in (".service", ".timer"):
        if unit.endswith(suffix):
            return unit[: -len(suffix)]
    return unit


def is_distro_unit(unit: str) -> bool:
    return unit_base(unit).startswith(DISTRO_UNIT_PREFIXES)


def parse_unit_list(text: str) -> list[str]:
    units = []
    for line in text.splitlines():
        fields = line.replace("●", " ").split()
        if fields and fields[0].endswith(".service"):
            units.append(fields[0])
    return units


def parse_timers(text: str) -> list[dict[str, str]]:
    timers = []
    for line in text.splitlines():
        fields = line.split()
        for index, field in enumerate(fields):
            if field.endswith(".timer"):
                activates = fields[index + 1] if index + 1 < len(fields) else ""
                timers.append({"timer": field, "activates": activates})
                break
    return timers


def split_address(local: str) -> tuple[str, int | None]:
    address, _, port = local.rpartition(":")
    address = address.strip("[]").split("%", 1)[0]
    return address or "*", to_int(port)


def parse_ss(text: str) -> list[dict[str, Any]]:
    sockets: dict[tuple[str, int], set[str]] = {}
    for line in text.splitlines():
        fields = line.split()
        if len(fields) < 5 or fields[0] in {"State", "Netid"}:
            continue
        offset = 1 if fields[0] in {"tcp", "udp"} else 0
        address, port = split_address(fields[3 + offset])
        if port is None:
            continue
        names = set(re.findall(r'\("([^"]+)",pid=', " ".join(fields[5 + offset :])))
        sockets.setdefault((address, port), set()).update(names)
    return [
        {"address": a, "port": p, "processes": sorted(n)}
        for (a, p), n in sorted(sockets.items(), key=lambda item: (item[0][1], item[0][0]))
    ]


def hex_address(text: str) -> str:
    raw = bytes.fromhex(text)
    if len(raw) == 4:
        return str(ipaddress.IPv4Address(raw[::-1]))
    words = b"".join(raw[i : i + 4][::-1] for i in range(0, len(raw), 4))
    address = ipaddress.IPv6Address(words)
    if address.ipv4_mapped is not None:
        return f"::ffff:{address.ipv4_mapped}"
    return str(address)


def parse_proc_net_tcp(text: str) -> list[dict[str, Any]]:
    sockets: set[tuple[str, int]] = set()
    for line in text.splitlines()[1:]:
        fields = line.split()
        if len(fields) < 4 or fields[3] != "0A":
            continue
        address, _, port = fields[1].partition(":")
        sockets.add((hex_address(address), int(port, 16)))
    return [
        {"address": a, "port": p, "processes": []}
        for a, p in sorted(sockets, key=lambda item: (item[1], item[0]))
    ]


def parse_proc_route(text: str) -> str | None:
    routes = []
    for line in text.splitlines()[1:]:
        fields = line.split()
        if len(fields) < 8 or fields[1] != "00000000" or fields[7] != "00000000":
            continue
        if int(fields[3], 16) & 3 != 3:
            continue
        routes.append((int(fields[6]), hex_address(fields[2])))
    return min(routes)[1] if routes else None


def parse_status_rss(text: str) -> int | None:
    match = re.search(r"^VmRSS:\s+([0-9]+)\s+kB", text, re.MULTILINE)
    return int(match.group(1)) * 1024 if match else None


def app_for(name: str, table: tuple[tuple[str, str], ...]) -> str | None:
    for app, pattern in table:
        if re.fullmatch(pattern, name):
            return app
    return None


def image_name(image: str) -> str:
    repository = image.split("@", 1)[0]
    last = repository.rsplit("/", 1)[-1]
    return last.split(":", 1)[0]


def image_app(image: str) -> str | None:
    app = app_for(image_name(image), APP_IMAGES)
    if app == "php-fpm" and "fpm" not in image:
        return None
    return app


def system_info(host: Host, report: dict[str, Any]) -> dict[str, Any]:
    release = parse_os_release(host.read("etc/os-release") or host.read("usr/lib/os-release") or "")
    virt = host.run("systemd-detect-virt")
    uptime = (host.read("proc/uptime") or "").split()
    return {
        "hostname": host.value("proc/sys/kernel/hostname") or socket.gethostname(),
        "os": release["pretty_name"],
        "os_id": release["id"],
        "os_version": release["version_id"],
        "kernel": host.value("proc/sys/kernel/osrelease"),
        "machine": host.machine,
        "arch": ARCHES.get(host.machine, host.machine),
        "virtualization": virt.out.strip() if virt.out.strip() else None,
        "uptime_seconds": to_int(uptime[0]) if uptime else None,
    }


def cpu_info(host: Host, report: dict[str, Any]) -> dict[str, Any]:
    info = parse_cpuinfo(host.read("proc/cpuinfo") or "")
    if not info["model"]:
        lscpu = host.run("lscpu", "-J")
        if lscpu.code == 0:
            info["model"] = parse_lscpu_json(lscpu.out)
    if not info["threads"]:
        info["threads"] = os.cpu_count() or 0
        info["cores"] = info["cores"] or info["threads"]
    return info


def memory_info(host: Host, report: dict[str, Any]) -> dict[str, int]:
    return parse_meminfo(host.read("proc/meminfo") or "")


def sys_block_disks(host: Host) -> list[dict[str, Any]]:
    disks: list[dict[str, Any]] = []
    for name in host.listdir("sys/block"):
        base = f"sys/block/{name}"
        sectors = to_int(host.value(f"{base}/size"))
        disks.append(
            {
                "name": name,
                "type": "disk",
                "size_bytes": sectors * 512 if sectors is not None else None,
                "rotational": to_bool(host.value(f"{base}/queue/rotational")),
                "model": clean(host.value(f"{base}/device/model")),
                "transport": "",
                "fstype": "",
                "mountpoints": [],
                "parent": None,
            }
        )
    return disks


def block_devices(host: Host) -> list[dict[str, Any]]:
    columns = "NAME,TYPE,SIZE,ROTA,MODEL,TRAN,FSTYPE"
    for mounts in ("MOUNTPOINTS", "MOUNTPOINT"):
        result = host.run("lsblk", "-J", "-b", "-o", f"{columns},{mounts}")
        if result.code == 0:
            return parse_lsblk(result.out)
    host.note("lsblk: unavailable, block devices read from /sys/block without filesystems")
    return sys_block_disks(host)


def smart_state(host: Host, disks: list[dict[str, Any]]) -> None:
    scan = host.run("smartctl", "--scan", "-j")
    if scan.code == 127:
        host.note("smartctl: not installed, SMART availability unknown")
        for disk in disks:
            disk["smart"] = {"smartctl": False}
        return
    try:
        scanned = parse_smartctl_scan(scan.out)
    except ValueError:
        scanned = {}
    if not host.is_root:
        host.note("smartctl: reading SMART needs root, run with --sudo")
    for disk in disks:
        device = f"/dev/{disk['name']}"
        controller = re.sub(r"n[0-9]+$", "", device) if disk["kind"] == "nvme" else device
        state: dict[str, Any] = {"smartctl": True, "scanned": controller in scanned}
        if host.is_root:
            result = host.run("smartctl", "-i", "-H", "-j", device)
            try:
                state.update(parse_smartctl_health(result.out))
            except ValueError:
                state["available"] = None
        disk["smart"] = state


def disk_info(host: Host, report: dict[str, Any]) -> dict[str, Any]:
    devices = block_devices(host)
    disks = [
        {
            "name": d["name"],
            "size_bytes": d["size_bytes"],
            "rotational": d["rotational"],
            "model": d["model"],
            "transport": d["transport"],
            "kind": disk_kind(d["name"], d["rotational"], d["transport"]),
        }
        for d in devices
        if d["type"] == "disk" and not d["name"].startswith(("loop", "zram", "ram", "sr"))
    ]
    smart_state(host, disks)
    filesystems = []
    for mount in parse_mounts(host.read("proc/self/mounts") or host.read("proc/mounts") or ""):
        record: dict[str, Any] = dict(mount)
        record["network"] = mount["fstype"] in NETWORK_FS
        usage = None if record["network"] else host.statvfs(mount["mountpoint"])
        if usage is not None:
            record["size_bytes"], record["used_bytes"], record["available_bytes"] = usage
        filesystems.append(record)
    return {
        "disks": disks,
        "partitions": [d for d in devices if d["type"] != "disk"],
        "filesystems": filesystems,
    }


def wifi_link(host: Host, name: str) -> dict[str, Any]:
    iw = host.run("iw", "dev", name, "link")
    if iw.code == 0:
        return parse_iw_link(iw.out)
    nmcli = host.run(
        "nmcli",
        "-t",
        "-f",
        "IN-USE,SSID,SIGNAL,FREQ",
        "device",
        "wifi",
        "list",
        "ifname",
        name,
        "--rescan",
        "no",
    )
    if nmcli.code == 0:
        return parse_nmcli_wifi(nmcli.out)
    proc = parse_proc_wireless(host.read("proc/net/wireless") or "")
    return proc.get(name, {"connected": None, "source": "none"})


def network_info(host: Host, report: dict[str, Any]) -> list[dict[str, Any]]:
    ip = host.run("ip", "-j", "addr")
    addresses: dict[str, list[str]] = {}
    if ip.code == 0:
        try:
            addresses = parse_ip_addr(ip.out)
        except ValueError:
            host.note("ip: unreadable JSON, addresses skipped")
    interfaces = []
    for name in host.listdir("sys/class/net"):
        base = f"sys/class/net/{name}"
        uevent = parse_key_values(host.read(f"{base}/uevent") or "")
        wireless = host.exists(f"{base}/wireless") or host.exists(f"{base}/phy80211")
        kind, detail = classify_interface(
            name, uevent.get("DEVTYPE", ""), wireless, host.exists(f"{base}/device")
        )
        speed = to_int(host.value(f"{base}/speed"))
        record: dict[str, Any] = {
            "name": name,
            "kind": kind,
            "detail": detail,
            "operstate": host.value(f"{base}/operstate"),
            "carrier": to_int(host.value(f"{base}/carrier")),
            "speed_mbps": speed if speed and speed > 0 else None,
            "mtu": to_int(host.value(f"{base}/mtu")),
            "addresses": addresses.get(name, []),
        }
        if kind == "wifi":
            record["wifi"] = wifi_link(host, name)
        interfaces.append(record)
    return interfaces


def gateway_info(host: Host, report: dict[str, Any]) -> str | None:
    return parse_proc_route(host.read("proc/net/route") or "")


def drm_gpus(host: Host) -> list[dict[str, Any]]:
    gpus = []
    for card in host.listdir("sys/class/drm"):
        if not re.fullmatch(r"card[0-9]+", card):
            continue
        base = f"sys/class/drm/{card}/device"
        vendor_id = host.value(f"{base}/vendor")
        device_id = host.value(f"{base}/device")
        gpus.append(
            {
                "vendor": PCI_VENDORS.get(vendor_id, vendor_id or "unknown"),
                "index": to_int(card[4:]),
                "model": f"{vendor_id}:{device_id}".strip(":"),
                "memory_bytes": to_int(host.value(f"{base}/mem_info_vram_total")),
                "driver": host.link_name(f"{base}/driver"),
                "source": "drm",
            }
        )
    return gpus


def gpu_info(host: Host, report: dict[str, Any]) -> list[dict[str, Any]]:
    gpus: list[dict[str, Any]] = []
    nvidia = host.run(
        "nvidia-smi",
        "--query-gpu=index,name,memory.total,driver_version,pci.bus_id",
        "--format=csv,noheader,nounits",
    )
    if nvidia.code == 0:
        gpus.extend(parse_nvidia_smi(nvidia.out))
    rocm = host.run("rocm-smi", "--showproductname", "--showmeminfo", "vram", "--json")
    if rocm.code == 0:
        try:
            gpus.extend(parse_rocm_smi(rocm.out))
        except ValueError:
            host.note("rocm-smi: unreadable JSON")
    covered = {gpu["vendor"] for gpu in gpus}
    for gpu in drm_gpus(host):
        if gpu["vendor"] in covered:
            continue
        if gpu["vendor"] == "nvidia":
            host.note("gpu: NVIDIA card without a working nvidia-smi, install the driver tools")
        gpus.append(gpu)
    return gpus


def power_info(host: Host, report: dict[str, Any]) -> list[dict[str, Any]]:
    supplies = []
    for name in host.listdir("sys/class/power_supply"):
        base = f"sys/class/power_supply/{name}"
        if host.value(f"{base}/scope") == "Device":
            continue
        supplies.append(
            {
                "name": name,
                "type": host.value(f"{base}/type"),
                "capacity_percent": to_int(host.value(f"{base}/capacity")),
                "status": host.value(f"{base}/status") or None,
                "online": to_bool(host.value(f"{base}/online")),
            }
        )
    return supplies


def container_info(host: Host, report: dict[str, Any]) -> dict[str, Any] | None:
    version = host.run("docker", "version", "--format", "{{.Server.Version}}")
    if version.code == 127:
        podman = host.run("podman", "version", "--format", "{{.Version}}")
        if podman.code == 0:
            host.note("podman: found, its containers are not listed yet")
            return {"runtime": "podman", "version": podman.out.strip(), "compose_projects": []}
        return None
    if version.code != 0:
        reason = first_line(version.err) or "docker daemon unreachable"
        if "permission denied" in reason.lower():
            reason = "permission denied, run with --sudo or join the docker group"
        host.note(f"docker: {reason}")
        return {"runtime": "docker", "version": None, "error": reason, "compose_projects": []}
    projects = []
    listed = host.run("docker", "compose", "ls", "--all", "--format", "json")
    if listed.code == 0:
        for project in parse_compose_ls(listed.out):
            ps = host.run(
                "docker", "compose", "-p", project["name"], "ps", "--all", "--format", "json"
            )
            project["services"] = parse_compose_ps(ps.out) if ps.code == 0 else []
            projects.append(project)
    else:
        host.note(f"docker compose: {first_line(listed.err) or 'plugin not available'}")
    standalone = []
    ps = host.run("docker", "ps", "--all", "--format", DOCKER_PS_FORMAT)
    if ps.code == 0:
        standalone = [c for c in parse_docker_ps(ps.out) if not c["project"]]
    return {
        "runtime": "docker",
        "version": version.out.strip(),
        "compose_projects": projects,
        "standalone_containers": standalone,
    }


def systemd_info(host: Host, report: dict[str, Any]) -> dict[str, Any] | None:
    units = host.run(
        "systemctl",
        "list-units",
        "--type=service",
        "--state=running",
        "--no-legend",
        "--plain",
        "--no-pager",
    )
    if units.code != 0:
        host.note(f"systemctl: {first_line(units.err) or 'not available'}")
        return None
    timers = host.run("systemctl", "list-timers", "--all", "--no-legend", "--plain", "--no-pager")
    return {
        "services": [u for u in parse_unit_list(units.out) if not is_distro_unit(u)],
        "timers": [
            t
            for t in parse_timers(timers.out if timers.code == 0 else "")
            if not is_distro_unit(t["timer"])
        ],
    }


def ports_info(host: Host, report: dict[str, Any]) -> list[dict[str, Any]]:
    ss = host.run("ss", "-ltnpH")
    if ss.code == 0:
        if not host.is_root:
            host.note("ss: process names of other users need root, run with --sudo")
        return parse_ss(ss.out)
    host.note("ss: unavailable, ports read from /proc/net/tcp without process names")
    sockets: list[dict[str, Any]] = []
    for table in ("proc/net/tcp", "proc/net/tcp6"):
        sockets.extend(parse_proc_net_tcp(host.read(table) or ""))
    return sockets


def process_groups(host: Host) -> list[dict[str, Any]]:
    groups: dict[tuple[str, bool], dict[str, Any]] = {}
    for pid in host.listdir("proc"):
        if not pid.isdigit():
            continue
        rss = parse_status_rss(host.read(f"proc/{pid}/status") or "")
        comm = host.value(f"proc/{pid}/comm")
        if rss is None or not comm:
            continue
        contained = bool(CONTAINER_CGROUP.search(host.read(f"proc/{pid}/cgroup") or ""))
        group = groups.setdefault(
            (comm, contained),
            {"name": comm, "in_container": contained, "count": 0, "rss_bytes": 0},
        )
        group["count"] += 1
        group["rss_bytes"] += rss
    return sorted(groups.values(), key=lambda g: (-g["rss_bytes"], g["name"]))


def detect_apps(
    processes: list[dict[str, Any]],
    ports: list[dict[str, Any]],
    containers: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    apps: dict[str, dict[str, Any]] = {}

    def entry(name: str) -> dict[str, Any]:
        return apps.setdefault(
            name, {"name": name, "evidence": [], "host_process": False, "rss_bytes": 0}
        )

    for group in processes:
        app = app_for(group["name"], APP_PROCESSES)
        if app is None:
            continue
        found = entry(app)
        where = "container" if group["in_container"] else "host"
        found["evidence"].append(f"process {group['name']} ({where})")
        if not group["in_container"]:
            found["host_process"] = True
            found["rss_bytes"] += group["rss_bytes"]
    for socket_info in ports:
        app = APP_PORTS.get(socket_info["port"])
        if app is not None:
            entry(app)["evidence"].append(f"port {socket_info['port']}")
    for project in (containers or {}).get("compose_projects") or []:
        for service in project.get("services") or []:
            app = image_app(service["image"])
            if app is not None:
                entry(app)["evidence"].append(f"container {project['name']}/{service['service']}")
    for container in (containers or {}).get("standalone_containers") or []:
        app = image_app(container["image"])
        if app is not None:
            entry(app)["evidence"].append(f"container {container['name']}")
    for found in apps.values():
        found["evidence"] = sorted(set(found["evidence"]))
    return [apps[name] for name in sorted(apps)]


def process_info(host: Host, report: dict[str, Any]) -> list[dict[str, Any]]:
    return process_groups(host)[:20]


def apps_info(host: Host, report: dict[str, Any]) -> list[dict[str, Any]]:
    return detect_apps(
        process_groups(host), report.get("listening_tcp") or [], report.get("containers")
    )


SECTIONS: tuple[tuple[str, Callable[[Host, dict[str, Any]], Any]], ...] = (
    ("system", system_info),
    ("cpu", cpu_info),
    ("memory", memory_info),
    ("storage", disk_info),
    ("network", network_info),
    ("gateway", gateway_info),
    ("gpus", gpu_info),
    ("power_supplies", power_info),
    ("containers", container_info),
    ("systemd", systemd_info),
    ("listening_tcp", ports_info),
    ("top_processes", process_info),
    ("apps", apps_info),
)


def collect(host: Host) -> dict[str, Any]:
    report: dict[str, Any] = {"probe_version": PROBE_VERSION, "root": host.is_root}
    for name, probe in SECTIONS:
        try:
            report[name] = probe(host, report)
        except Exception as error:
            report[name] = None
            host.note(f"{name}: {type(error).__name__}: {error}")
    report["notes"] = host.notes
    return report


def main() -> int:
    if not sys.platform.startswith("linux"):
        print(json.dumps({"probe_version": PROBE_VERSION, "error": "Linux only"}))
        return 2
    print(json.dumps(collect(Host()), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
