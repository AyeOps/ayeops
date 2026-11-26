# aye discover

`aye discover` finds out what a Linux machine runs, so that its manifest entry starts from facts
instead of memory. It has two halves:

1. **The probe** runs on the host. It is one file of plain Python 3.10+ standard library
   ([`src/ayeops/discover/probe.py`](../src/ayeops/discover/probe.py)), reads the system and
   prints a JSON report. Nothing is installed and nothing is written on the host.
2. **The suggestion** runs on your machine. `aye discover --suggest report.json` turns the report
   into a manifest fragment: a host block, the modules that fit the hardware, and stubs for the
   compose services it found. `aye discover --adopt report.json -o ayeops.yaml` instead writes a
   whole manifest that describes the host as it runs, see [adopting a host](#adopting-a-host).

## Running it

| Command | What it does |
|---|---|
| `aye discover --ssh <host> -o host.json` | pipes the probe into `python3 -` over ssh and saves the report |
| `aye discover --ssh <host> --sudo -o host.json` | the same with `sudo -n python3 -`, for SMART data, process names of every port and the docker socket |
| `aye discover --ssh <host> --python python3.12` | uses another interpreter on the host |
| `aye discover -o host.json` | runs the probe on the local Linux machine |
| `aye discover --suggest host.json` | prints the manifest fragment for a report |
| `aye discover --adopt host.json -o adopt/ayeops.yaml` | writes a manifest and a compose stub per compose project that describe the host as it runs, then checks them |

Without `aye` installed, the probe runs on its own, which is all `--ssh` does:

```sh
ssh ops@box python3 - < src/ayeops/discover/probe.py > box.json
ssh ops@box sudo -n python3 - < src/ayeops/discover/probe.py > box.json
```

`--sudo` uses `sudo -n`, so it needs passwordless sudo for `python3`; with a password prompt the
probe fails with `sudo: a password is required` instead of hanging. ssh runs with
`BatchMode=yes` for the same reason. Sudo for `python3` is root, and the deploy user of
[`aye host-init`](host-init.md#aye-discover) cannot run the probe at all; without `--sudo` the
probe runs best-effort, and that page lists the exact commands that would need root.

## What is probed

| Section | Source | Without root |
|---|---|---|
| `system` | `/etc/os-release`, `/proc/sys/kernel/{hostname,osrelease}`, `/proc/uptime`, `systemd-detect-virt` | complete |
| `cpu` | `/proc/cpuinfo`, `lscpu -J` when the model is missing (ARM) | complete |
| `memory` | `/proc/meminfo`: total, available, swap | complete |
| `storage.disks` | `lsblk -J -b` (or `/sys/block`), kind `nvme`, `ssd`, `hdd`, `usb`, `virtual` | complete |
| `storage.disks[].smart` | `smartctl --scan -j`, `smartctl -i -H -j <device>` | only whether smartctl exists |
| `storage.filesystems` | `/proc/self/mounts` and `statvfs`; pseudo, container and snap mounts are skipped; network filesystems are listed but never stat-ed, a dead NFS server would hang the probe | complete |
| `network` | `/sys/class/net`, `ip -j addr`; kind `wired`, `wifi`, `vpn` (Tailscale, WireGuard, ZeroTier, tunnels), `virtual`, `loopback`; state, carrier, speed, MTU, global addresses | complete |
| `network[].wifi` | `iw dev <if> link`, else `nmcli`, else `/proc/net/wireless`: SSID, signal, frequency, bitrate | complete |
| `gateway` | `/proc/net/route`: the gateway of the default route with the lowest metric | complete |
| `gpus` | `nvidia-smi --query-gpu`, `rocm-smi --json`, and `/sys/class/drm` for every other vendor | complete |
| `power_supplies` | `/sys/class/power_supply`, peripheral batteries (mice, headsets) skipped | complete |
| `containers` | `docker version`, `docker compose ls`, `docker compose -p <name> ps`, `docker ps` with a fixed template | needs the docker group |
| `systemd` | running services and timers, minus a list of distribution defaults | complete |
| `listening_tcp` | `ss -ltnpH`, else `/proc/net/tcp{,6}` | process names of your own processes only |
| `top_processes` | the 20 process names with the most resident memory, split host or container | complete |
| `apps` | well-known applications recognised from process names, ports and container images: PostgreSQL, MySQL, MariaDB, Redis, Valkey, nginx, Caddy, Traefik, PHP-FPM, Node.js, Java, Grafana, Prometheus, VictoriaMetrics, MongoDB, Memcached, HAProxy, Apache, RabbitMQ, ClickHouse, MinIO, Loki, Elasticsearch, Kafka, InfluxDB, etcd and others | complete |

Every section degrades on its own. A missing tool, a missing permission or a crash in one
section becomes a line in `notes` and the rest of the report is still printed.

## What is never read

- Environment variables of any process (`/proc/<pid>/environ`) and command lines
  (`/proc/<pid>/cmdline`): process names come from `/proc/<pid>/comm` only.
- `.env` files, compose files, configuration files of applications, shell history, keys.
- `docker inspect` and `docker ps --format '{{json .}}'`: both print environment and commands.
  `docker compose ps --format json` does carry the command line; the probe keeps only service,
  container, image, state, health and published ports (each with its bind address, container
  port and protocol) from it.
- The probe passes only `PATH`, `HOME`, `DOCKER_HOST`, `DOCKER_CONTEXT` and `XDG_RUNTIME_DIR` to
  the commands it runs, and prints none of them.

Tests hold the probe to this: a fake host with secrets in `environ`, `cmdline`, a `.env` file
and a compose command line must produce a report without them.

## Sample report (shortened)

```json
{
  "probe_version": 1,
  "root": true,
  "system": {
    "arch": "amd64",
    "hostname": "build-box",
    "kernel": "6.8.0-85-generic",
    "machine": "x86_64",
    "os": "Ubuntu 24.04.3 LTS",
    "os_id": "ubuntu",
    "os_version": "24.04",
    "uptime_seconds": 86400,
    "virtualization": "none"
  },
  "cpu": {"cores": 16, "model": "AMD Ryzen 9 7950X 16-Core Processor", "sockets": 1, "threads": 32},
  "memory": {
    "available_bytes": 19327352832,
    "swap_free_bytes": 1073741824,
    "swap_total_bytes": 8589930496,
    "total_bytes": 270174941184
  },
  "storage": {
    "disks": [
      {
        "kind": "nvme",
        "model": "Samsung SSD 980 PRO 1TB",
        "name": "nvme0n1",
        "rotational": false,
        "size_bytes": 1000204886016,
        "smart": {"available": true, "enabled": null, "passed": true, "scanned": true, "smartctl": true},
        "transport": "nvme"
      }
    ],
    "filesystems": [
      {"available_bytes": 512000000000, "device": "/dev/nvme0n1p2", "fstype": "ext4",
       "mountpoint": "/", "network": false, "size_bytes": 983000000000, "used_bytes": 421000000000}
    ]
  },
  "network": [
    {"addresses": ["192.168.1.20/24"], "carrier": 1, "detail": "", "kind": "wired", "mtu": 1500,
     "name": "enp3s0", "operstate": "up", "speed_mbps": 1000},
    {"addresses": [], "carrier": 1, "detail": "", "kind": "wifi", "mtu": 1500, "name": "wlan0",
     "operstate": "up", "speed_mbps": null,
     "wifi": {"connected": true, "freq_mhz": 5180, "signal_dbm": -58, "source": "iw",
              "ssid": "HomeNet 5G", "tx_bitrate_mbps": 780.0}}
  ],
  "gpus": [
    {"driver": "nvidia 565.57.01", "index": 0, "memory_bytes": 25757220864,
     "model": "NVIDIA GeForce RTX 4090", "pci_bus_id": "00000000:01:00.0",
     "source": "nvidia-smi", "vendor": "nvidia"}
  ],
  "containers": {
    "runtime": "docker",
    "version": "28.5.1",
    "compose_projects": [
      {"name": "shop", "status": "running(2)", "config_files": ["/srv/shop/compose.yaml"],
       "services": [
         {"container": "shop-db-1", "health": "healthy", "image": "postgres:17.6",
          "ports": [{"bind": "127.0.0.1", "protocol": "tcp", "published": 5432, "target": 5432}],
          "published_ports": [5432], "service": "db", "state": "running"}
       ]}
    ],
    "standalone_containers": []
  },
  "systemd": {"services": ["mysql.service", "php8.3-fpm.service"], "timers": [{"activates": "backup.service", "timer": "backup.timer"}]},
  "listening_tcp": [{"address": "127.0.0.1", "port": 3306, "processes": ["mysqld"]}],
  "apps": [
    {"evidence": ["port 3306", "process mysqld (host)"], "host_process": true,
     "name": "mysql", "rss_bytes": 128849018880}
  ],
  "notes": []
}
```

## The suggested fragment

```yaml
hosts:
  build-box:
    arch: amd64
    compose:
    - shop/compose.yaml
    memory: 257658m
    modules:
      containers: {}
      disk-forecast: {}
      gpu: {}
      metrics: {}
      network:
        interfaces:
        - enp3s0
        - wlan0
        wifi: true
      node: {}
      smart: {}
    reserved:
      mysql: 120g
      php-fpm: 512m
services:
  db:
    alerts: []
    description: 'TODO: what db does and for whom'
    host: build-box
    logs: docker
    metrics: []
    status:
      internal: 'TODO: a public status component, or why it is internal'
suggested:
  compose_files:
    shop:
    - /srv/shop/compose.yaml
  exporters:
  - app: mysql
    evidence:
    - port 3306
    - process mysqld (host)
    exporter: prom/mysqld-exporter
    status: suggested, not enabled
  - app: postgres
    evidence:
    - container shop/db
    exporter: prometheuscommunity/postgres-exporter
    status: suggested, not enabled
  systemd_services:
  - mysql.service
  - php8.3-fpm.service
  systemd_timers:
  - backup.timer
```

How the fragment is built:

| Part | Rule |
|---|---|
| host name | the hostname, lowercased, with every other character turned into `-` |
| `arch`, `memory` | from the report; `memory` is the total in MiB |
| `reserved` | resident memory of well-known applications running on the host itself, outside containers, rounded up; replace it with what they are configured to take (a buffer pool, `pm.max_children` times the worker size) |
| `modules` | `metrics` and `node` always; `battery: true` with a battery; `containers` with docker; `smart` with a physical drive whose SMART is not known to be missing; `disk-forecast` with filesystems; `gpu` with an NVIDIA card; `network` with the wired and Wi-Fi interfaces that are up, `wifi: true` with a Wi-Fi one; `probes` with `internet: true` and the found `gateway` |
| `compose` | `<project>/<file name>` for every compose file; copy the files there, next to the manifest, so `aye check` can read their services |
| `services` | one stub per compose service that no AyeOps module runs, under its compose name; a name the manifest cannot hold, or a second service of the same name, becomes `<project>-<service>` |
| `suggested` | what was seen but is not enabled: exporters for the detected applications, the original compose paths, standalone containers, custom systemd services and timers, GPUs of other vendors and the probe notes |

`suggested` is not a manifest key, and the schema rejects it on purpose: copy `hosts` and
`services` into the manifest and act on the rest. The stubs are valid YAML with `TODO` values, not
comments, and the catalog rules treat a value starting with `TODO` as missing, so `aye check`
keeps asking for a description, a metric, an alert and a status mark until a person writes them.
Change `metrics: {}` to `role: agent` with `remote_write` when the host ships to a central one.

## Adopting a host

A host that already runs compose projects is adopted by describing it first and changing it
later. `--suggest` proposes modules and leaves the catalog to a person; `--adopt` writes a
manifest that passes `aye check` as it is, whose first `aye render` and `aye diff` for the host
create, recreate and remove nothing that runs:

```sh
aye discover --ssh box --sudo -o box.json
aye discover --adopt box.json -o adopt/ayeops.yaml
aye check adopt/ayeops.yaml
```

| Option | Default | Meaning |
|---|---|---|
| `-o <file>` | required | the manifest to write; the compose stubs go next to it, and no file that exists is replaced |
| `--project <name>` | `ayeops` | the manifest `project`; the host's own compose project is `<project>-<host>` |
| `--until YYYY-MM-DD` | today plus fourteen days | the last day of the waivers it writes |
| `--observe` | off | also enable the read-only exporters, see [modules](#modules-of-an-adopted-host) |

### What it writes

| Part | Content |
|---|---|
| `project`, `runbook_base_url` | `--project`, and `https://runbooks.<project>.example` as a placeholder: set it to where your runbooks live before you write alerts of your own |
| `hosts.<host>` | `arch`, `memory`, `reserved` as `--suggest` writes them; `address` from a VPN interface, else the address in the subnet of the default gateway, none when neither is known; `modules: {}`; one `compose` entry `{file: <project>/compose.yaml, name: <project>}` per compose project, so logs, the portal and the reconciler match its containers by the real project name |
| `<project>/compose.yaml` | a stub per compose project, written from the report because the probe never reads compose files: `name`, and per service its `image`, its published ports (bind address, host and container port, protocol), `container_name` when the real container is not named `<project>-<service>-1`, `scale` when it ran several; the top-level `x-aye-adopted` names the real `config_files` |
| `services` | one entry per compose service, see below |

AyeOps never runs `docker compose` with a file of `compose`: those files only tell `aye check`
which services the host provides. The stub is a description, not a copy, and lacks the
environment, volumes and commands of the real file, so `docker compose up` with it would replace
the running containers; copy the real files over the stubs (`scp box:/srv/shop/compose.yaml
adopt/shop/`) as soon as you can, and keep the service names.

A running service (any of its containers `running` in the report) gets every field the
[catalog rules](rules.md) ask for:

| Field | Value |
|---|---|
| `description` | `<service> of the compose project <project> on <host>, image <image>, adopted as is`: facts, not an answer to what it does for whom; rewrite it |
| `logs` | `docker` |
| `metrics` | `{none: {reason: "adopted as is, TODO: name the metrics of <service>, or why it has none", until: <until>}}` |
| `alerts` | one alert `<service>-down`, severity `warning`, runbook [`adopted-service-down`](runbooks/adopted-service-down.md) |
| `status` | `{internal: {reason: "adopted as is, TODO: a public status component for <service>, or why it stays internal", until: <until>}}` |

A service whose containers had all stopped gets `state: disabled` and a description that ends
with `while stopped`: it is drawn on the portal and nothing deploys, scrapes or alerts on it.

The two `none` and `internal` reasons are [waivers](manifest.md#waivers) that end on `--until`.
Their text starts with `adopted as is`, not with `TODO`, because a reason starting with `TODO`
counts as missing; `grep -n "adopted as is, TODO" adopt/ayeops.yaml` lists what is left to
decide. With the default end, fourteen days from today, `aye check` passes with one warning per
waiver from the first day and fails from the day after `--until`
([waivers end](rules.md#waivers-end)), so the decisions cannot be forgotten.

The alert counts the adopted containers by name in cAdvisor's `container_start_time_seconds`:

```text
((count(container_start_time_seconds{host="box",name="shop-web-1"}) or vector(0)) < bool 1)
  * ((count(container_start_time_seconds{host="box"}) or vector(0)) > bool 0)
```

It fires when the host has fewer of them than it ran when it was adopted, and stays `0` while
the host has no cAdvisor at all, so it is quiet until the [containers](modules.md#containers)
module runs there; a dead cAdvisor fires the module's own `target-down`.

A compose service keeps its name when the manifest can hold it. Otherwise it becomes the name
made safe (`worker_queue` is `worker-queue`), then `<project>-<service>` for a name that a second
project or a built-in module service already uses, so that a module service of the same name is
never left out of the render. The stub uses the manifest name too, with `container_name` set to
the real container, and the command prints a note for every renamed service, every service of
several containers and every standalone container, which is not adopted.

### Modules of an adopted host

Adopt enables no module. The host's render is then a compose project `<project>-<host>` with no
service, and `aye diff` lists only added files, with no service it would create, recreate or
remove. The adopted projects are other compose projects: `aye deploy` runs `docker compose up -d
--remove-orphans` only in `/opt/ayeops/<host>` for `<project>-<host>`, so it never touches them.
Nothing needs deploying until a module is on.

`--observe` enables the modules `--suggest` picks from the hardware: `metrics`, `node`,
`containers`, `smart`, `disk-forecast`, `gpu`, `network` and `probes`. They read the host and add
containers of their own; none of them starts, stops or recreates another container. A module
whose host port is already in use (a listening socket or a published port of the report) or
whose container name exists on the host is left off with a note, and so is a module that needs
one that was left off. The modules that act on what runs, `reconciler` (it starts stopped
services), `updates`, `image-updates` and `autopilot`, stay off in both modes: turn them on one at
a time once the real compose files are in place.

### What adopt refuses

Adopt stops with exit code 1 and writes nothing when the report says the probe could not list
the containers (run it with `--sudo`), when a compose project named `<project>-<host>` already
runs (choose another `--project`), when a compose file of the host lies in `/opt/ayeops/<host>`,
the directory `aye deploy` replaces, when the host's arch is neither `amd64` nor `arm64`, and when
a file it would write exists.

After writing, it runs `aye check` on the new manifest, prints the notes, errors and warnings,
and compares the host's render with the report: the project name, the container names and the
host ports of the render must not be among those that already run. The exit code is 0 when the
check has no error and nothing would be taken over, warnings included, and 1 otherwise.
