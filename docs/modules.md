# Modules

Every built-in module, what it runs, its settings and its alerts. The module table of the
[README](../README.md#modules) marks each one stable or preview.

## Contents

| Module | What it is for |
|---|---|
| [`metrics`](#metrics) | VictoriaMetrics and vmagent; an agent host ships to a central one |
| [`logs`](#logs) | Loki and Alloy for the containers of the declared compose projects and journald |
| [`node`](#node) | node-exporter with textfile collectors for battery, AC power and a pending reboot |
| [`containers`](#containers) | cAdvisor: CPU, memory and restarts per container |
| [`smart`](#smart) | SMART health, bad sectors, NVMe wear and drive temperature |
| [`gpu`](#gpu) | NVIDIA GPU temperature, memory and driver health |
| [`network`](#network) | link state, speed, errors and Wi-Fi signal of the declared interfaces |
| [`disk-forecast`](#disk-forecast) | an alert when a filesystem fills up within 7 days |
| [`probes`](#probes) | blackbox-exporter for HTTP, TLS expiry and the host's own internet, RDAP for domains |
| [`checks`](#checks) | the runners of the HTTP, TCP, DNS, script and SQL checks declared under services |
| [`hygiene`](#hygiene) | a nightly prune of stopped containers, unused images and build cache |
| [`reconciler`](#reconciler) | starts again a declared container that Docker left down, with a back-off |
| [`image-updates`](#image-updates) | Diun over the containers on a floating tag |
| [`updates`](#updates) | self-hosted Renovate over your repositories, open and overdue updates as metrics |
| [`vulns`](#vulns) | a daily Trivy scan of every image the host's compose projects run |
| [`eol`](#eol) | end of support and security support of what the host runs, from endoflife.date |
| [`cloudflare`](#cloudflare) | edge traffic of proxied zones and the free-plan limits of R2, Workers and D1 |
| [`delivery`](#delivery) | deploy, CI, test and quality metrics from `aye report` |
| [`github`](#github) | GitHub Actions minutes and billed usage, and the importer of `aye report` artifacts |
| [`ai-usage`](#ai-usage) | spend and limits of AI providers |
| [`sentinel`](#sentinel) | a watcher on another host that messages when the central host stops answering |
| [`oncall`](#oncall) | GoAlert escalation of critical alerts with a Telegram acknowledge button |
| [`incidents`](#incidents) | the incident journal and the `Incidents` dashboard |
| [`status`](#status) | the reporter that pushes signed component states to the status page Worker |
| [`autopilot`](#autopilot) | level 1 in shadow mode: a dossier and ranked hypotheses for each critical alert |
| [`grafana`](#grafana) | Grafana with the datasources, dashboards and alert rules of the enabled modules |
| [`ingress`](#ingress) | Traefik with HTTPS names for internal services and a wildcard certificate |
| [`auth`](#auth) | oauth2-proxy sign-in in front of every ingress route |
| [`portal`](#portal) | two static pages from the manifest: where things run and how to reach them |
| [`secrets`](#secrets) | a self-hosted secrets manager (Infisical by default) and `aye secrets` |
| [`errors`](#errors) | self-hosted error tracking for the Sentry SDKs (GlitchTip by default) |
| [`mcp`](#mcp) | an MCP hub on agentgateway with read-only tools |
| [`objectstore`](#objectstore) | the store for the off-site copies of other hosts: Garage or rest-server |
| [`backups`](#backups) | the metrics contract of every dump job, with alerts on a failed and a missing run |
| [`offsite`](#offsite) | restic copies of a host's newest backup to another host, with restore tests |

After the modules: [flow baselines](#flow-baselines), an alert that compares a flow with its
own usual level, and [write your own module](#write-your-own-module).

## How a module renders

A module is data under `src/ayeops/modules/<name>/`:

| File | Purpose |
|---|---|
| `module.yaml` | summary, required modules, default settings, settings schema, the catalog entry of every service it can run, alerts, dashboards, files to copy, the external credentials it needs (`permissions`, `generated`) and the host directories it needs (`host_directories`) |
| `compose.yaml.j2` | Jinja template of its compose services and volumes |
| `scrape.yaml.j2` | Jinja template of its vmagent scrape jobs |
| `files/`, `dashboards/` | scripts, configs and Grafana dashboards copied into the rendered host; a `files` entry with `template: true` is rendered with the module settings first, and one with `live: true` is read by its service while it runs, so it stays out of the [config hash](deploy.md#config-hash) |

The renderer adds `mem_limit`, `cpus`, `platform`, `restart: unless-stopped`, the labels
`io.ayeops.service` and `io.ayeops.module`, log rotation, and turns container ports into
`127.0.0.1:<port>:<port>` unless the port is in `expose`. Every service a module can run has a
catalog entry that passes the same [rules](rules.md) as your own services; a test enforces it.

Log rotation is `logging: {driver: json-file, options: {max-size: 10m, max-file: "3"}}`, at
most 30 MB of docker log per container, whatever the daemon's own default is: a daemon without
`log-opts` in `daemon.json` keeps a container's `-json.log` growing until the container is
recreated. A service template that sets its own `logging` keeps it, and a module can set
`logging` at the top of its `module.yaml` for all its services. The [logs](#logs) module reads
container logs through the docker API (`loki.source.docker`), which follows json-file rotation,
so the rotation costs nothing but the history older than 30 MB on the host itself.

A module alert may set `no_data: alerting` when the absence of its series is the signal
(`host-down`, `target-down`); Grafana then fires the rule when the query returns nothing. The
default `no_data: ok` keeps the rule quiet without data, so a desktop without a battery does
not page through `battery-low`. Query errors of module alerts stay in Grafana's `Error` state.
An alert may also name a `provider`: it is rendered only when some host runs the module with
that `provider` setting, so a store on rest-server gets no `objectstore-down`, whose silence
would otherwise page forever. Likewise an alert with a `setting` is rendered only when some
host sets that setting of the module, so `offsite-drill` exists only with a `restore_drill`.
An alert with `only_with: grafana_reader` is rendered only when the
[`grafana-reader`](#grafana-reader) is, so `grafana-reader-failing` does not page on an install
without a consumer of the reader.

A module alert that aggregates by host keeps the `project` and `group` labels that
[vmagent](#metrics) puts on every sample: `max by (host, project, group) (...)`, not
`max by (host) (...)`, so the alert carries the host's group into
[notification grouping](#grafana). A host without a group has no `group` label, so its alert is
the same series as before; a test fails on a module alert that aggregates by host and drops them.

Every alert, of a module or of a service, links to the dashboard panel that draws what it
measures, when one is rendered. The rule gets Grafana's `__dashboardUid__` and `__panelId__`
annotations, so the alert in Grafana has «Go to dashboard/panel», the email carries Grafana's
dashboard and panel buttons, and the Telegram message a «dashboard» link («дашборд» with
`locale: uk`) next to the runbook. The panel is the one whose queries share the most metric
names with the alert's expression, panels of the alert's own module first; a panel that pins a
label of such a metric to another value than the alert does (`layer="gateway"` against
`layer="internet"`) does not count, and among the rest the panel pinning fewer labels the alert
leaves open wins, then dashboards by uid and panels in order; it is chosen among the dashboards as rendered for this manifest, so a
panel dropped by `ayeopsRequires` is never linked. Where the guess is wrong, an alert names its
dashboard itself: `dashboard: <uid>`, `dashboard: {uid: <uid>, panel: <id>}`, or
`dashboard: false` for none; `aye check` refuses a uid or panel id that no module or project
dashboard has (rule `alert-dashboard`). Grafana refuses to provision a rule with
`__dashboardUid__` and no `__panelId__`, and with it every other rule, so a link always names a
panel: a bare `<uid>`, or a panel this manifest does not render, links the panel of that
dashboard that draws the alert's metrics, else its first panel.

Image versions were the newest stable releases at least two weeks old when the slice was cut;
every one is published for `linux/amd64` and `linux/arm64`, which CI verifies.

## metrics

VictoriaMetrics `v1.153.0` single-node and vmagent `v1.153.0`.

| Setting | Default | Meaning |
|---|---|---|
| `role` | `server` | `server` runs storage and vmagent; `agent` runs only vmagent |
| `retention` | `30d` | `-retentionPeriod` of storage |
| `scrape_interval` | `30s` | global scrape interval of vmagent |
| `queue_max` | `1GB` | on-disk buffer per remote_write URL while the target is unreachable |
| `remote_write` | none | `{host: <name>}` or `{url: <url>}`; required for `agent` |
| `networks` | `[]` | existing docker networks vmagent joins besides its own, e.g. the default network of your application's compose project, so a scrape job reaches its services by name without a published port; declared `external`, so the application stack never depends on this one |

vmagent labels every sample with `host=<host name>` and `project=<manifest project>`, and with
`group=<group>` when the host has a [group](manifest.md#host) (its `group`, else its `profile`).
Each host's own vmagent adds them on its way out, so an agent's samples carry the agent's
labels, not those of the host it writes to. They replace a label of the same name that a scraped
target exposes. Samples written straight to VictoriaMetrics (the reports of `aye report`, the
GitHub importer, the recording rules of `delivery`) do not pass vmagent and keep their own
labels; their `project` is the reporting project. With `remote_write: {host: central}` it
writes to `http://<central address>:8428/api/v1/write`; the central host must run `role: server`
and expose `8428` on a private `public_bind` address, since VictoriaMetrics has no
authentication. A laptop that sleeps keeps its samples on disk and replays them on wake.

While storage is out of reach, vmagent buffers to `/vmagent-data` up to `queue_max`, then drops
the oldest blocks. With `-remoteWrite.inmemoryQueues=1` it sends freshly scraped samples ahead of
that backlog, so a host that reconnects reports at once and its backlog fills in behind; samples
arrive out of order, which VictoriaMetrics accepts, and so must a `remote_write: {url: ...}`
store, as it already must for vmagent's parallel queues. During an outage the newest blocks wait
in the in-memory queue, a few hundred blocks, before they move to disk, so a hard crash of the
host loses them; `docker compose stop` writes them to disk first.

Alerts: `target-down`; `metrics-shipping-backlog` (warning) when for 15 minutes the queue of a
host holds more than 30 minutes of its own traffic, compressed bytes as
`rate(vmagent_remotewrite_block_size_bytes_sum[1h])` measures them, or more than half of its
room; `metrics-shipping-dropped` (critical) when in the last hour the full queue dropped blocks
or storage rejected one with `400` or `409`. The queue's own gauges travel through the queue:
while a host cannot reach storage at all, `host-down` fires and holds both back, and the drops
of such an outage show once the host reports again.

## logs

Loki `3.7.8` single binary on local disk and Alloy `v1.21.3`. Needs `metrics` on the same host,
which scrapes both.

| Setting | Default | Meaning |
|---|---|---|
| `role` | `server` | `server` runs Loki and Alloy; `agent` runs only Alloy |
| `retention` | `30d` | `retention_period` of Loki, enforced by its compactor |
| `push` | none | `{host: <name>}` or `{url: <url>}`; required for `agent` |
| `projects` | none | compose projects to ship instead of the declared ones |
| `journald` | `false` | also ship the systemd journal of the host |

Alloy reads the docker socket read-only and tails running containers only. Every line carries
`host`, `project`, `compose_project`, `compose_service`, `container` and `job` (`docker` or
`journald`), and `group` when the host has one, the same values as on its metrics.
It ships only the compose projects of its host: the AyeOps stack itself (`<project>-<host>`)
and every entry of the host's `compose` list, by its `name`, else the `name:` of the file, else
the directory of the file, as docker compose names it. A host shared with other stacks therefore
ships nothing of theirs. `projects` replaces the `compose` list; the AyeOps stack stays. A
one-shot `docker run` belongs to no compose project and is not shipped.

With `journald: true` Alloy also reads `/var/log/journal` and `/run/log/journal`, labels lines
with `unit` and `level`, and drops the entries of containers that log through the journald
driver, which Alloy already ships from docker.

With `push: {host: central}` Alloy pushes to `http://<central address>:3100/loki/api/v1/push`;
the central host must run `role: server` and expose `3100` on a private `public_bind` address,
since Loki has no authentication. Grafana gets a `Loki` datasource (uid `loki`) when a logs
server exists in the fleet: `http://loki:3100` on its own host, the server's address otherwise.
Loki and Alloy log at `warn`, so their own request logs do not loop back into Loki.

Alerts: `target-down` for both, `logs-ingestion-errors` when Loki discards lines,
`logs-shipping-failing` when Alloy drops lines it could not push. A full Loki volume shows as
`disk-full` of the docker root filesystem.

## node

node-exporter `v1.12.1` with the host root mounted read-only and `pid: host`. It runs on the
compose network, so its network interface metrics describe the container, not the host; the
[network](#network) module reads the host's.

| Setting | Default | Meaning |
|---|---|---|
| `battery` | `false` | run the battery textfile collector (busybox `1.38.0`) |
| `battery_interval` | `60` | seconds between battery readings |
| `textfile_dir` | none | absolute host directory bind-mounted as the textfile directory instead of the `node-textfile` volume |
| `reboot` | `false` | run the reboot textfile collector (busybox `1.38.0`) |
| `reboot_interval` | `600` | seconds between reboot checks |
| `rslave` | `true` | mount the host root into node-exporter with `rslave` propagation, so a filesystem mounted after the start shows up without a restart; `false` for Docker Desktop on macOS, which refuses that mount (`path / is mounted on / but it is not a shared or slave mount`) |

The battery collector reads `/sys/class/power_supply` and writes
`ayeops_battery_capacity_percent{battery}`, `ayeops_battery_status{battery,status}` and
`ayeops_power_on_ac` (only when a Mains or USB supply exists) to a volume that node-exporter
reads. Its variables: `AYE_POWER_SUPPLY_DIR`, `AYE_TEXTFILE_DIR`, `AYE_BATTERY_INTERVAL`, and
`AYE_BATTERY_ONCE=1` to write once and exit.

The reboot collector mounts the host root read-only and writes
`ayeops_reboot_required{reason="flag"}`, 1 while `/run/reboot-required` exists (Debian and
Ubuntu put it there after an update that needs a restart), and
`ayeops_reboot_required{reason="kernel"}`, 1 while `/usr/lib/modules` (or `/lib/modules`) holds a
kernel newer than the running one, which is how Fedora, RHEL and Arch show it. Versions are
compared field by field, numbers as numbers. Its variables: `AYE_HOST_ROOT`,
`AYE_OSRELEASE_FILE`, `AYE_TEXTFILE_DIR`, `AYE_REBOOT_INTERVAL`, and `AYE_REBOOT_ONCE=1`.

Without `textfile_dir` the battery collector, the reboot collector, the script check runners and node-exporter share
the `node-textfile` named volume, which nothing outside compose can reach. With
`textfile_dir: /var/lib/ayeops/textfile` the same three mount that host directory instead, so a
host job, a cron or systemd unit, can write its own `*.prom` file there and node-exporter serves
it on the next scrape. Create the directory before `docker compose up`, world-readable, since
node-exporter runs as `nobody`; a host job writes its file to a temporary name in the same
directory and renames it, so a scrape never reads half a file.

Alerts: `host-down` (no fresh `up{job="node"}` for three minutes, using MetricsQL `lag`, or the
exporter answering `up == 0`; judged per host with `max by (host)` and fired on every node
series of the host seen in the last day, so a series whose labels changed, a host that moved to
another group or the `project` label arriving, does not fire for a day while the new series is
up; while the host is down it [holds back](#grafana) its other alerts; a host marked
[`volatile`](manifest.md#volatile-hosts-and-maintenance-windows) is left out), `host-gone`
(warning, rendered only when some host is volatile: no successful node scrape of a volatile host
for its `volatile` duration, a day by default, `for` 5m; the expression in `module.yaml` is the
shape for one day over every host, the rendered rule names the volatile hosts and their
durations), `disk-full` (> 80 % for 15m), `memory-low` (< 10 % available for
10m), `swap-out-sustained` (`rate(node_vmstat_pswpout[5m])` above 256 pages per second, about
1 MiB/s, for 10m), `battery-low` (< 20 % and not on AC), `reboot-pending` (info, a reboot has
waited for 7 days).

Dashboard `AyeOps / Hosts`, one host at a time, picked from the hosts that report
`node_uname_info`: state, uptime, cores, memory, clock sync, OS and kernel; CPU by mode, load
against cores, pressure (PSI) and hardware temperatures; memory, swap and OOM kills; filesystem
fill, free space now and in 7 days at the pace of the last day, disk throughput and busy time.
Rows that need another module appear only when that module runs on some host: network traffic
and errors with [network](#network), drives with [smart](#smart), the battery with `battery`, the
reboot flag with `reboot`.

`memory-low` misses a host that keeps enough memory available by swapping out; a large host with
a database sized close to its RAM does exactly that, which is what `swap-out-sustained` catches.
Host network interfaces are the job of the [network](#network) module.

## containers

cAdvisor `v0.60.6` from `ghcr.io/google/cadvisor`, privileged, docker containers only.

| Setting | Default | Meaning |
|---|---|---|
| `housekeeping_interval` | `30s` | cAdvisor housekeeping interval |

Alerts: `container-restarting` (more than two starts in 15 minutes), `container-throttled` (a container
that used more than 80 % of its CPU limit on average over 30 minutes). It does not count throttled
CFS periods: a collector that sleeps and wakes for a short burst is throttled in most of the few
periods it runs at all while it uses a few percent of its limit, which only slows the burst.

## smart

[smartctl_exporter](https://github.com/prometheus-community/smartctl_exporter) `v0.15.0` from
`prometheuscommunity/smartctl-exporter`, privileged and as root, since SMART needs raw access to
the drives. It scans for drives itself and skips drives in standby (`--smartctl.powermode-check`
defaults to `standby`), so it does not spin up sleeping disks.

| Setting | Default | Meaning |
|---|---|---|
| `interval` | `60s` | how often smartctl runs |
| `devices` | `[]` | explicit devices such as `/dev/sda`; empty means scan |

Alerts: `smart-failing` (the drive's own health verdict fails, critical), `smart-sectors-growing`
(`Reallocated_Sector_Ct`, `Current_Pending_Sector` or `Offline_Uncorrectable` raw values, or NVMe
media errors, grew within a day), `nvme-wear` (`percentage_used` above 80 % for 1h),
`disk-temperature-high` (above 60 °C for 15m; one threshold for HDD and NVMe, between the HDD
comfort zone and the NVMe throttling point).

USB enclosures often hide SMART behind their bridge chip; add them to `devices` once
`smartctl -d <type>` works for them, or accept that they are not watched.

SMART health, drive temperature, NVMe wear and power-on time fill the drives row of
`AyeOps / Hosts`.

## gpu

[nvidia_gpu_exporter](https://github.com/utkuozdemir/nvidia_gpu_exporter) `1.16.0` from
`utkuozdemir/nvidia_gpu_exporter`. It runs `nvidia-smi`, which the NVIDIA Container Toolkit
injects into the container through a device reservation with the `utility` capability; the host
needs the NVIDIA driver and the toolkit (`nvidia-ctk runtime configure --runtime=docker`). The
healthcheck runs `nvidia-smi -L` in the container, so it fails when the driver is out of reach.

| Setting | Default | Meaning |
|---|---|---|
| `vendor` | `nvidia` | the only vendor so far |

Why not `dcgm-exporter`: DCGM supports data-center GPUs; on GeForce cards most of its fields are
unsupported, and consumer GPUs are the common case in a small fleet. `nvidia-smi` answers the
same way on every card. The exporter's NVML backend also counts XID errors; since `1.16.0` its
image (`1.16.0-nvml`) is no longer experimental and is published for `linux/amd64` and
`linux/arm64`, but the module still runs the `nvidia-smi` backend, so XIDs are not collected
yet; `gpu-collect-failing` covers the worst of them (XID 79 makes `nvidia-smi` fail).
AMD GPUs are planned; `aye discover` lists them as suggested, not enabled.

Alerts: `gpu-temperature-high` (above 85 °C for 10m), `gpu-memory-full` (above 95 % for 15m),
`gpu-collect-failing` (`nvidia_smi_last_collect_success == 0` for 5m, critical).

## network

A second node-exporter `v1.12.1`, `net-exporter`, with only the `netdev`, `netclass` and
`textfile` collectors, and a Wi-Fi collector (the same node-exporter image).

| Setting | Default | Meaning |
|---|---|---|
| `interfaces` | required | the interfaces to watch, e.g. `[enp3s0, wlan0]`; nothing else is collected or alerted on |
| `wifi` | `false` | run the Wi-Fi collector |
| `wifi_interval` | `30` | seconds between Wi-Fi readings |
| `wifi_port` | `19102` | the `127.0.0.1` port of the Wi-Fi collector's own node-exporter on the host |

The exporter stays on the compose network, so vmagent reaches it by name and its port binds to
`127.0.0.1`, but it reads the host's namespace: `--path.procfs=/host/proc/1` makes `netdev` read
`/proc/1/net/dev`, the counters of the host's init process and so of the host network namespace
(netlink, which would answer for the container, is switched off), and `--path.sysfs=/host/sys`
makes `netclass` read the host's `/sys/class/net`. A host that mounts `/proc` with `hidepid` hides
`/proc/1` and leaves the job without traffic counters.

The Wi-Fi collector runs node-exporter's `wifi` collector, which asks the kernel over nl80211.
nl80211 answers for the network namespace it is asked in, so the service runs with
`network_mode: host`; its node-exporter listens on `127.0.0.1:<wifi_port>` only and publishes no
port. Every `wifi_interval` seconds a shell loop in the same container reads it and writes an
allowlist of series into the textfile that `net-exporter` serves, so the series arrive with
`job="network"` and the declared-interface filter: `node_wifi_station_signal_dbm`, receive and
transmit bitrate, `node_wifi_station_info{ssid,bssid}`, `node_wifi_interface_frequency_hertz`,
connected seconds, beacon losses, retries and failed transmits, plus
`ayeops_wifi_signal_dbm{device}` for the alert. Kernels without wireless extensions have no
`/proc/net/wireless`, which an earlier version read; nl80211 works on every kernel with cfg80211.
The service runs as root because the shared textfile volume belongs to root; if its
node-exporter dies, the loop exits and the container restarts. Its variables:
`AYE_TEXTFILE_DIR`, `AYE_WIFI_INTERVAL`, `AYE_WIFI_PORT`, `AYE_WIFI_RETRIES` (unread
answers before it gives up, 10), and `AYE_WIFI_ONCE=1`.

Alerts: `interface-down` (`operstate` down, lowerlayerdown, dormant or notpresent for 5m),
`wifi-signal-weak` (below -75 dBm for 15m), `network-errors` (errors plus drops above 1 % of
packets for 15m), `link-speed-degraded` (a wired link negotiated less than its maximum of the last
7 days, e.g. 1 Gbit/s down to 100 Mbit/s, for 10m). The scrape job keeps only the declared
interfaces, so a stopped container's veth never pages. An interface that disappears completely
(an unplugged USB adapter) stops reporting instead of firing.

The traffic and errors of these interfaces fill the network row of `AyeOps / Hosts`, and their
carrier, speed, traffic and Wi-Fi signal the link row of `AyeOps / Internet`.

## disk-forecast

No container: one alert on node-exporter metrics, so it requires `node` on the same host.

Alerts: `disk-full-forecast` fires when a filesystem that is more than half full runs out within
7 days at the pace of the last day (`predict_linear(node_filesystem_avail_bytes[1d], 7 * 86400)`
below 0, for 1h). The half-full condition keeps young, empty volumes with a burst of writes quiet.

## probes

blackbox-exporter `v0.29.1` with an `http_2xx` module (IPv4, follows redirects), and an RDAP
exporter: a stdlib Python script on `python:3.13.16-alpine3.23` that picks the registry of each
domain from the IANA bootstrap file and reads the `expiration` event.

| Setting | Default | Meaning |
|---|---|---|
| `http` | `[]` | URLs probed for a 2xx answer; HTTPS URLs also report certificate expiry |
| `internet` | `false` | ping `internet_targets` and ask `dns_servers` from this host |
| `internet_targets` | `[1.1.1.1, 8.8.8.8]` | addresses pinged for the `internet` layer |
| `dns_servers` | `[1.1.1.1, 8.8.8.8]` | resolvers asked for `dns_query` for the `dns` layer |
| `dns_query` | `example.com` | the name asked for, an A record |
| `gateway` | none | the router's IPv4 address, pinged for the `gateway` layer; `aye discover` suggests it from the default route |
| `internet_interval` | `15s` | scrape interval of the ping and DNS probes |
| `domains` | `[]` | domains whose expiry is looked up over RDAP |
| `rdap_bootstrap_url` | IANA `dns.json` | bootstrap file |
| `rdap_refresh_seconds` | `21600` | how often RDAP is asked |

RDAP exporter metrics: `ayeops_domain_expiry_days{domain}`,
`ayeops_domain_expiry_timestamp_seconds{domain}` and `ayeops_domain_rdap_success{domain}`; a
failed lookup is reported as `0`, not dropped. Its variables: `AYE_RDAP_DOMAINS`,
`AYE_RDAP_BOOTSTRAP_URL`, `AYE_RDAP_REFRESH_SECONDS`, `AYE_RDAP_PORT`.

Alerts: `probe-failing` (5m), `tls-expiring` (< 14 days), `domain-expiring-60d` (info),
`domain-expiring-30d` (warning), `domain-expiring-7d` (critical), `domain-expiry-unknown` (RDAP
silent for a day). Dashboard: `AyeOps / Probes`.

The internet probes run on the host whose connection they describe, usually one at home or in an
office, so enable `probes` there with `internet: true`. blackbox-exporter pings without raw
sockets: the container gets `net.ipv4.ping_group_range` for every group. The jobs are
`blackbox-icmp` (label `layer`: `gateway` or `internet`) and `blackbox-dns` (`layer: dns`), so a
break shows where it is: the router, the provider, or name resolution. Dashboard
`AyeOps / Internet`, per host: what answers now, minutes offline and outages in 24 hours, ping
and lost pings, DNS answer time, minutes offline per day, and with [network](#network) the
carrier, Wi-Fi signal, link speed and traffic of the declared interfaces. Outages and minutes
offline are counted at 15-second steps.

There is no internet-down alert on purpose: while the link is down the host cannot ship the
failing probes to the central host, so an alert there would fire late, after the link is back.
vmagent keeps the samples in its queue and sends them on reconnection, so the dashboard shows
the outage afterwards.

## checks

The runners of the [checks](checks.md) declared under `services.<name>.checks`: HTTP, TCP and
DNS through blackbox-exporter, scripts in a sandboxed runner, read-only SQL queries against a
Postgres datasource. Requires `metrics` on the same host; script and SQL checks also need
`node`, whose textfile directory serves their results.

| Setting | Default | Meaning |
|---|---|---|
| `dns_server` | `1.1.1.1` | resolver of `dns` checks without `server` |
| `expose` | `[]` | `9116` publishes the check blackbox on `public_bind` |

It renders only what the checks of its host need: `check-blackbox`, `check-scripts`,
`check-scripts-net` and `check-sql`. Every check gets its own alert; the module adds
`target-down` for the blackbox and
[`check-runner-stale`](runbooks/check-runner-stale.md) when a runner has not written results
for 5 minutes. Fields, sandboxes and metrics of each check type:
[the checks module](checks.md#the-checks-module).

## hygiene

A nightly prune of what docker leaves behind, with numbers. One small container
(`python:3.13.16-alpine3.23`, the image the script check runners already pull) talks to the
docker socket through the Engine API, without a network, read-only, all capabilities dropped,
and at `schedule` host time (it mounts `/etc/localtime`) runs, in this order. The API rather
than the `docker` CLI because the API answers with exact byte counts and the CLI prints rounded
decimal units (`9.431GB`), and because it lets the runner pick the build-prune parameter the
daemon understands:

| Step | Engine API | Same as |
|---|---|---|
| stopped containers | `POST /containers/prune` with `until=containers_until` | `docker container prune --filter until=24h` |
| unused images | `POST /images/prune` with `dangling=false`, `until=images_until` | `docker image prune -a --filter until=168h` |
| build cache | `POST /build/prune` with `all`, `max-used-space=build_cache_keep` | `docker buildx prune --all --max-used-space 10gb` |
| anonymous volumes | `POST /volumes/prune`, only with `volumes: true` | `docker volume prune` |

| Setting | Default | Meaning |
|---|---|---|
| `schedule` | `03:30` | daily run, `HH:MM` host time |
| `images_until` | `168h` | an unused image must have been created this long ago to go |
| `containers_until` | `24h` | a stopped container must have been created this long ago to go |
| `build_cache_keep` | `10g` | build cache BuildKit may keep, least recently used goes first |
| `volumes` | `false` | also prune anonymous volumes no container uses |
| `dry_run` | `false` | prune nothing, write what a run would free |
| `disk_path` | `/var/lib/docker` | directory whose filesystem `disk_used_percent` describes |
| `journal_dir` | `/var/log/journal` | journal directory to measure, `null` to skip |

What it never touches: an image any container uses, running or stopped, is not unused, so
`image prune -a` keeps it; a running or paused container is not stopped. The `until` of images
is the image's creation time, not when it was pulled: a release built a month ago and pulled
yesterday is old, and goes as soon as no container uses it. That is the rollback window to
think about. A stopped container created more than a day ago goes, and with it its local logs;
`docker compose up` recreates it, and the [logs](#logs) module has already shipped what it said.

Build cache: `build_cache_keep` is BuildKit's `max-used-space` (docker 28 renamed
`--keep-storage`; against an API older than 1.48 the runner sends `keep-storage`). On a host
with a CI runner that bounds its own cache after each job, set `build_cache_keep` to the same
size, so the nightly prune removes nothing the jobs would keep and their next build stays warm.

Volumes stay off by default. A volume prune is data loss by design: an anonymous volume is the
only copy of whatever a container wrote to a `VOLUME` path it never named, a database image's
data directory for one. Turn it on for hosts where every volume that matters is named.

Run it now, or try a dry run, with
`docker compose exec -e AYE_HYGIENE_ONCE=1 hygiene python -I /app/hygiene_runner.py` (add
`-e AYE_HYGIENE_DRY_RUN=1` for the estimate). Each run logs one JSON line.

Metrics, in `ayeops_hygiene.prom` in the [node](#node) textfile directory:

| Metric | Meaning |
|---|---|
| `ayeops_hygiene_last_success_timestamp_seconds` | last run that finished without an error; a fresh runner starts the clock at its first start |
| `ayeops_hygiene_last_run_timestamp_seconds`, `_last_run_success`, `_run_duration_seconds` | the last run, good or not |
| `ayeops_hygiene_reclaimed_bytes{kind}`, `ayeops_hygiene_removed{kind}` | what the last run freed, per `containers`, `images`, `build_cache`, `volumes` |
| `ayeops_hygiene_reclaimable_bytes{kind}`, `ayeops_hygiene_removable{kind}` | the same, estimated, when `dry_run` is on (`ayeops_hygiene_dry_run 1`) |
| `ayeops_hygiene_images{phase}`, `ayeops_hygiene_build_cache_bytes{phase}`, `ayeops_hygiene_disk_used_percent{phase}` | `before` and `after` the run |
| `ayeops_hygiene_build_cache_keep_bytes` | `build_cache_keep` in bytes |
| `ayeops_hygiene_journal_bytes` | allocated size of the journal directory |

The image estimate subtracts each image's shared size from its size, the way
`docker system df` does, but never below zero per image: with the containerd image store a
shared size above the size is common, and the two figures part ways (13.5 GB against
`docker system df`'s 9.4 GB on one host). Neither is what a prune frees; a real run reports that
in `reclaimed_bytes`.
`build_cache_bytes` leaves out records shared with images, which no prune can free; it equals
the reclaimable column of `docker system df` when no build is running.

Alerts: `hygiene-stale` (no successful run for two days), `build-cache-oversized` (build cache
still above twice `build_cache_keep` after a run), `disk-critical` (a filesystem above 90 % for
5m, critical; the 80 % warning is [node](#node)'s `disk-full`, the trend is
[disk-forecast](#disk-forecast)).

### Journald

The journal is the host's, not a container's, so AyeOps does not prune it; it only measures it.
journald keeps up to 10 % of the filesystem, at most 4 GB, by default. A fixed bound, in
`/etc/systemd/journald.conf.d/size.conf`:

```ini
[Journal]
SystemMaxUse=1G
```

then `systemctl restart systemd-journald`, and `journalctl --vacuum-size=1G` to apply it to what
is already there. `ayeops_hygiene_journal_bytes` shows the effect after the next run.

## reconciler

The first layer of the [incident path](autopilot.md): a declared container that is not running
and that Docker will not bring back by itself is started again, without a model, a confirmation
or a person. A host rebooted at night and one container did not come up; a one-shot worker
without a restart policy crashed. Nobody should be woken for that.

### What Docker already does, and what is left

Most of the job is Docker's own, and the module leans on it instead of redoing it:

| Case | Who handles it |
|---|---|
| a container of the rendered project exits, crashes or is OOM-killed | Docker: the renderer sets `restart: unless-stopped` on every service |
| the host reboots | Docker: on start the daemon starts every `always` and `unless-stopped` container that nobody stopped |
| a person runs `docker stop` or `docker compose stop` | Docker remembers the manual stop and leaves an `unless-stopped` container down, through reboots too |
| the start after a reboot or a restart fails: a port on an address that comes up later (a VPN `public_bind`), a bind mount on a disk not mounted yet, a dependency network not there | **reconciler**: the daemon tries once, logs it and leaves the container `exited` with `State.Error` set |
| a declared compose service without a restart policy (compose's default is `no`) crashes, or stays down after a reboot | **reconciler** |
| an `on-failure:N` service used up its retries | **reconciler** |
| a container keeps failing | **reconciler**: a back-off and an alert instead of a loop |

[docker-autoheal](https://github.com/willfarrell/docker-autoheal) (MIT) was considered: it
restarts containers whose healthcheck reports `unhealthy`, which is not this case (a container
that is not running has no health), it restarts without a back-off or a journal, and it sees
commits a few times a year. Restarting an unhealthy container is a diagnosis, the
[hypotheses](autopilot.md#hypotheses) of a later layer, so it is not reused here.

So the code that is ours is what neither covers: the desired state from the manifest, the
decision which down container is fair game, the back-off, the journal and the metrics.

### Desired state

The render, not the running containers, says what should run. `aye render` passes the agent
`AYE_RECONCILER_PROJECTS`, a JSON object of compose project name to service names: the host's
own project (`<project>-<host>`, every rendered service) and every `compose` file the manifest
declares for the host, named as compose names it (the entry's `name`, else the file's `name:`,
else the lowercased folder). From a declared file it leaves out what compose itself would not
keep running: `restart: "no"`, a service behind `profiles`, `replicas: 0` or `scale: 0`.
`exclude` drops more, as `<service>` or `<project>/<service>`. The agent matches containers by
the compose labels `com.docker.compose.project` and `com.docker.compose.service` and ignores
one-off `docker compose run` containers and every project the render did not name.

### Deliberate stops

Docker records a manual stop but does not show it in the API, so the agent reads the decision
from what Docker shows, conservatively:

| Container | Verdict |
|---|---|
| `running`, `restarting`, `paused`, `removing` | up, nothing to do |
| `exited` or `created` with `State.Error` (a failed start) | **start**, whatever the restart policy |
| restart policy `always` or `unless-stopped`, no error | left alone: Docker restarts these itself unless a person stopped them |
| policy `no` or `on-failure`, OOM-killed or an exit code other than 0, 137, 143 | **start**: it crashed and nothing else will start it |
| policy `no` or `on-failure`, exit code 0, 137 (SIGKILL after the stop timeout) or 143 (SIGTERM) | left alone: a clean exit or a stop, unless it was running when the reconciler last looked before the host rebooted, then **start** |
| `dead`, or no container at all | counted in `missing`, not touched: only a recreate helps, see below |

The reboot rule is what makes a container without a restart policy come back after a reboot
without overriding a stop: the agent keeps the set of containers it saw running, keyed to the
kernel's boot id (`/proc/sys/kernel/random/boot_id`), in its volume. After a new boot only the
containers of that set are started, and a container leaves the set as soon as it is seen
running again, so a stop later in the same boot is respected. An app that exits with another
code on SIGTERM looks like a crash when stopped; give it `restart: "no"` in its compose file or
put it in `exclude`. The first pass waits one `interval` after the agent starts, so after a
boot the daemon has started its own containers first.

### The action, and why not `docker compose up -d`

The agent calls the Engine API, `POST /containers/<id>/start`, for each container to start, in
the order of the `com.docker.compose.depends_on` label, and waits up to `timeout` for a
`service_healthy` or `service_started` dependency first, as compose does. A dependency that is
itself stopped on purpose is not waited for: the attempt fails at once with that reason. Then it
waits, again within `timeout`, until the started containers run and their healthcheck left
`starting`. The attempt succeeds when every started container runs and is not `unhealthy`.

`start` runs the container exactly as the last deploy created it. `docker compose up -d` from
inside a container would need every project's directory mounted at its host path, its `.env`
and the environment the operator deployed with; any difference recreates containers with
another configuration, which is a deploy, not a repair. It would also need the compose CLI next
to Python in one image. So the agent does not recreate: a container that is gone or `dead`
counts in `ayeops_reconciler_missing`, the alerts of the service notice it is down, and
`aye deploy` (or `docker compose up -d` in the project) brings it back. The
[hygiene](#hygiene) module prunes stopped containers after `containers_until`, so a container
the reconciler left alone for a day may end up missing.

### Back-off and journal

A project the agent started `attempts` times (default 3) within `window` (default 1 hour) is
given up on: nothing more is started there, `ayeops_reconciler_given_up` goes to 1 and
`reconciler-gave-up` fires. It stays given up, also across restarts of the agent, until a pass
finds nothing to start in the project (a person started it, or removed it), which logs a
`recovered` line.

Every attempt is one JSON line on stdout, shipped by the [logs](#logs) module like any other
container log:

```json
{"event": "attempt", "project": "acme-apps", "attempt": 1, "of": 3, "reasons": {"acme-apps-web-1": "start-failed"}, "before": [...], "started": ["acme-apps-web-1"], "errors": [], "after": [...], "success": true, "seconds": 4.2}
```

`before` and `after` list each container of the project with its status, exit code, error and
health. The other events are `gave-up`, `recovered`, `would-start` (with `dry_run`, once per
change of the plan) and `error` (the socket did not answer).

### Drift: what runs but is not declared

The other side of the desired state is a container that runs on the host although the manifest
does not declare it: a `docker run` by hand that nobody removed, a project started with
`docker compose up` from a folder the manifest does not list, a leftover after a service moved
to another host. [`aye portal --discovered`](#portal) shows those once, when someone passes a
report of `aye discover`; the reconciler measures them on every pass, read-only.

`aye render` passes the agent `AYE_RECONCILER_DECLARED` too, a JSON list of the compose projects
the host declares: its own project and every `compose` entry, named as above. Unlike
`AYE_RECONCILER_PROJECTS` it keeps a project whose services are all in `exclude` or not kept
running, because such a project is still declared. Each pass lists the running containers
(`GET /containers/json`, no `all`, so stopped ones do not count: the [hygiene](#hygiene) module
prunes those) and counts a container as undeclared when its `com.docker.compose.project` label
names none of the declared projects, or when it has no such label at all. The check is per
project: a container of a declared project counts as declared whatever its service, one-off
`docker compose run` containers included. Names that match a pattern in `ignore` are left out,
as shell globs (`*`, `?`, `[...]`) matched case-sensitively against the container name, for
containers that are meant to stay outside the manifest: a CI runner's job containers, a BuildKit
builder (`buildx_buildkit_*`).

The names go into a series each, with the image and the compose project, so a panel can say
which; to keep a host full of job containers from flooding the metrics store, at most 50 are
listed (the first by name), and the rest only counted. `host-drift` fires when a host has run
an undeclared container for an hour, as a warning: it pages nobody, at night or by day. Its
[runbook](runbooks/host-drift.md) walks through removing, declaring or ignoring what was found.

### Settings

| Setting | Default | Meaning |
|---|---|---|
| `interval` | `30s` | time between passes |
| `attempts` | `3` | starts of one project within `window` before it is given up on |
| `window` | `1h` | the back-off window |
| `timeout` | `2m` | the longest one attempt waits for dependencies and for the started containers |
| `exclude` | `[]` | services never touched, `<service>` or `<project>/<service>` |
| `ignore` | `[]` | container names never counted as undeclared, shell globs such as `buildx_buildkit_*` |
| `dry_run` | `false` | start nothing, log what would be started |

One small container (`python:3.13.16-alpine3.23`, the image [hygiene](#hygiene) already pulls)
without a network, read-only, all capabilities dropped, 32 MB. It needs the [node](#node)
module for its textfile and keeps its state in the volume `reconciler-data`. Run one pass now
with `docker compose exec -e AYE_RECONCILER_ONCE=1 reconciler python -I /app/reconciler.py`.

**It mounts the Docker socket read-write**, because starting a container is a write; a
read-only mount would not stop it anyway, since `:ro` protects the socket file, not the API
behind it. Whoever can write to the socket is root on the host. The agent only lists and
inspects containers and calls `start`; it never creates, removes, pulls or executes anything,
and it has no network to receive orders from. A socket proxy was considered and rejected: the
one the [image-updates](#image-updates) module uses (Tecnativa's) lets a `POST` through only
with `POST=1`, and listing containers needs `CONTAINERS=1`, which together allow
`POST /containers/create` too, so it would add a container without taking a privilege away.

### What it measures

In `ayeops_reconciler.prom` in the [node](#node) textfile directory, per declared compose
project in the label `compose_project` (`project` is the manifest project that vmagent sets on
every series):

| Metric | Meaning |
|---|---|
| `ayeops_reconciler_last_run_timestamp_seconds` | the last pass that could read Docker |
| `ayeops_reconciler_attempts_total`, `_successes_total`, `_failures_total` | attempts and their outcome since the volume was created |
| `ayeops_reconciler_given_up` | 1 while the project is given up on |
| `ayeops_reconciler_missing` | declared services without a container, plus `dead` containers |
| `ayeops_reconciler_stopped` | containers left alone as deliberately stopped |
| `ayeops_reconciler_projects`, `ayeops_reconciler_dry_run` | projects declared, and whether `dry_run` is on |
| `ayeops_undeclared_containers` | running containers of no declared project, not ignored, all of them |
| `ayeops_undeclared_container_info` | 1 per undeclared container, labels `name`, `image` and `compose_project` (empty for a container without compose labels); at most 50 per host |
| `ayeops_undeclared_containers_overflow` | undeclared containers beyond the 50 listed |

Alerts: `reconciler-gave-up` (a project given up on, warning; the alerts of the service itself
decide whether someone is paged), `reconciler-stale` (no finished pass for 5 minutes) and
`host-drift` (undeclared containers for an hour, warning). With `grafana`, `AyeOps / Health`
lists the undeclared containers of every host in the panel "Containers the manifest does not
declare".

## image-updates

Renovate opens pull requests for images whose version is written in git. A tag that floats,
`latest`, a bare major such as `postgres:18` or `redis:7`, or no tag at all, changes under the
same name whenever the publisher rebuilds it, usually for a security fix, and nothing in git moves:
the container keeps the image it was created from. This module finds those containers.

Three containers, none with a published port:

- `image-updates-proxy` (`tecnativa/docker-socket-proxy` `v0.5.0`) mounts the docker socket
  read-only and answers only `GET` for the container list and image inspect (`CONTAINERS=1`,
  `IMAGES=1`, `PING=1`, everything else and every `POST` refused). It sits alone with the agent on
  the internal network `image-updates-docker`, so no other container can reach it.
- `image-updates-agent` (`python:3.13.16-alpine3.23`, read-only, no internet: both its networks are
  internal) lists the running containers every `interval` seconds, keeps those whose image floats,
  writes them for Diun to `/data/images.json` with the platform each one runs, receives Diun's
  webhook with the digest each tag points to now and Diun's run reports, and writes the textfile.
- `diun` (`crazymax/diun` `4.33.1`) reads that list at `schedule` in `timezone`, and once at start,
  asks each registry for the current digest of every tag with HEAD requests (`compareDigest`) and
  reports new and changed digests and each finished run to the agent. It has no docker access.

Which containers are watched:

| Image reference | Watched |
|---|---|
| tag with a full version, `13.2.3`, `v0.60.6`, `3.13.16-alpine3.23` | no, Renovate's |
| any `@sha256:` digest | no |
| `latest`, no tag, a bare major or major.minor (`18`, `18-alpine`, `3.13`), a word (`stable`) | yes |
| an image that only exists locally (no repo digest, a local build) | no, counted as `local` |
| label `io.ayeops.image-updates=true` / `=false` | always / never |

Two numbers are not a full version on purpose: official images rebuild `postgres:18.6-alpine3.24`
or `python:3.13` under the same tag when their base image gets a security fix, and Renovate only
proposes the next version, never the rebuild.

A container counts as `pending` when the digest Diun last reported for its tag is not among the
repo digests of the image it runs, `current` when it is, and `unknown` until Diun has reported the
tag once (right after the first start, or a private image without credentials).

| Setting | Default | Meaning |
|---|---|---|
| `schedule` | `04:15` | daily Diun run, `HH:MM` in `timezone` (Diun adds up to 5 minutes of jitter) |
| `timezone` | the manifest's [`timezone`](manifest.md#top-level) | time zone of `schedule` |
| `interval` | `60` | seconds between two reads of the running containers, 10 to 3600 |
| `ghcr_username` | `null` | GitHub user for private `ghcr.io` images; the token goes in `AYE_IMAGE_UPDATES_GHCR_TOKEN` (`read:packages`) |

Docker Hub limits anonymous pulls per IP, shared with every `docker pull` from the same address.
Diun's digest check is a HEAD request, which Docker Hub does not count, and one run a day over a
few dozen tags stays far below any limit; keep the schedule daily on a host behind a shared NAT.
Without `ghcr_username` a private `ghcr.io` image stays `unknown` and Diun logs `unauthorized` for
it; nothing fires.

Metrics, in `ayeops_image_updates.prom` in the [node](#node) textfile directory:

| Metric | Meaning |
|---|---|
| `ayeops_image_update_available{container,image,tag}` | 1 when the tag points to a newer image than the container runs, 0 when it runs the current one; absent while `unknown` |
| `ayeops_image_updates_containers{state}` | running containers by `current`, `pending`, `unknown`, `pinned`, `local`, `skipped` |
| `ayeops_image_updates_last_scan_timestamp_seconds` | last read of the running containers |
| `ayeops_image_updates_last_check_started_timestamp_seconds`, `_success_`, `_failed_` | start, finish and failure of the last Diun run |
| `ayeops_image_updates_tags{result}` | tags of the last Diun run by `new`, `update`, `unchange`, `skip`, `error` |
| `ayeops_image_updates_notifications_total` | digests Diun has reported since the state volume was created |

The Images row of `AyeOps / Hosts` lists the pending containers and the counts per state.

Alerts: `image-update-available` (a pending container on the host for 12h, one alert per host),
`image-updates-scan-stale` (no read of the containers for 15 minutes), `image-updates-check-stale`
(no finished Diun run for 2 days).

## updates

New versions of what the project runs and builds with, as pull requests, from a Renovate CLI
that runs on your own host: no hosted app, no GitHub App, only a token. Two containers share a
volume:

- `updates` is the pinned `ghcr.io/renovatebot/renovate` image (amd64 and arm64) running
  `updates_loop.sh`. It waits for a request file, runs `renovate` over `repositories` under a
  `timeout_minutes` limit, then writes the start, end and exit code next to Renovate's JSON
  report (`reportType: file`). Clones and the tools Renovate installs on demand (`uv` for
  `uv.lock`, for example) live in the `updates-cache` volume, so the next run starts warm.
- `updates-agent` (`python:3.13.16-alpine3.23`, no network, read-only) asks for a run at
  `schedule` in `timezone`, and for one right away on a host that has never run, and turns
  every finished run into textfile metrics.

What a run does is up to each repository's own `renovate.json`: managers, grouping,
`minimumReleaseAge`, schedule, automerge. A repository without one gets Renovate's onboarding
pull request first. The schedule in a repository's config is read in its `timezone`; set the
module's `schedule` inside that window, or the daily run only refreshes the metrics and the
dependency dashboard issue.

| Setting | Default | Meaning |
|---|---|---|
| `repositories` | required | `owner/name` of every repository to check |
| `platform` | `github` | `github`, `gitlab`, `gitea`, `forgejo` or `bitbucket` |
| `endpoint` | `null` | API URL of a self-hosted forge, for example `https://git.example/api/v1` |
| `schedule` | `06:00` | daily run, `HH:MM` in `timezone` |
| `timezone` | the manifest's [`timezone`](manifest.md#top-level) | time zone of `schedule` |
| `timeout_minutes` | `120` | a run that takes longer is stopped and counts as failed |
| `dry_run` | `null` | `extract`, `lookup` or `full`: look, open nothing |
| `git_author` | `null` | `Name <address>` Renovate commits as, e.g. `Acme Bot <123+acme-bot@users.noreply.github.com>` |
| `platform_commit` | `enabled` | GitHub only: `enabled` commits through the API, `auto` only with a GitHub App token, `disabled` pushes with git |
| `allowed_commands` | `[]` | Renovate's `allowedCommands`: regular expressions, each anchored with `^` and `$`, of the `postUpgradeTasks` commands a repository may run; empty runs none |

On GitHub the commits go through the API (`platformCommit`), so GitHub signs them and shows them
as Verified, made by the account that owns the token. Set `git_author` to that account too, with
its noreply address (`<id>+<login>@users.noreply.github.com`; the id is in
`https://api.github.com/users/<login>`): without it Renovate keeps its default
`renovate@whitesourcesoftware.com`, which on github.com belongs to a user of Mend, and warns
about it on every run.

The token goes in `AYE_UPDATES_TOKEN`. On GitHub a fine-grained token limited to the listed
repositories needs Contents, Pull requests, Issues (the dependency dashboard), Commit statuses and
Workflows (to bump pinned actions) with read and write, and Metadata read; Dependabot alerts
read is optional. A fine-grained token covers the repositories of one owner only: repositories
of a user and of an organization together need a classic token with `repo` and `workflow`, or
one module per owner on different hosts. On Gitea, Forgejo and GitLab the token of
a bot user with write access to the repositories.

Memory is 1 GB: Renovate is a Node.js process that holds each repository's dependency tree and
the lookups, and on a lock file it runs the package manager beside it. A run that hits the
limit ends with exit code 137 and fails `updates-stale`; raise it with `resources`.

Run it now with
`docker compose exec updates-agent python -I /app/updates_agent.py` with
`-e AYE_UPDATES_ONCE=1`; the loop picks the request up within 15 seconds. Each finished run logs
one JSON line from `updates-agent`; Renovate itself logs JSON to the container log.

Metrics, in `ayeops_updates.prom` in the [node](#node) textfile directory:

| Metric | Meaning |
|---|---|
| `ayeops_updates_last_success_timestamp_seconds` | last run with exit code 0 in which every repository was reached without an error; a fresh agent starts the clock at its first start |
| `ayeops_updates_last_run_timestamp_seconds`, `_last_run_success`, `_last_run_exit_code`, `_run_duration_seconds` | the last run, good or not; exit code 124 is the timeout |
| `ayeops_updates_repository_ok{repository}` | 1 when the repository is in the report without an error-level problem |
| `ayeops_updates_open_prs{repository}` | open update pull requests |
| `ayeops_updates_pending_updates{repository,datasource}` | dependencies with a newer version that passed the repository's filters, per datasource (`docker`, `pypi`, `github-tags`, ...) |
| `ayeops_updates_oldest_pending_release_timestamp_seconds{repository}` | release time of the oldest version still waiting; absent when nothing waits |
| `ayeops_updates_dependencies{repository}` | dependencies Renovate tracks |
| `ayeops_updates_problems{repository,level}` | Renovate's own warnings and errors for the repository |

`updates-pending-old` counts from the release of the version, not from the pull request: with
a 14-day `minimumReleaseAge` the pull request is at most 16 days old when the alert fires.

### Post-upgrade commands

A repository can ask Renovate to run a command on its branch after the bump and before the
commit (`postUpgradeTasks`), for files that follow from the bumped ones: a generated file, a
committed render, a lock file of a tool Renovate has no manager for. Renovate runs none of them
unless the admin config lists the command in `allowedCommands`, which `allowed_commands` sets.
Each entry is a regular expression that must match the whole command, so the module refuses one
without `^` at the start and `$` at the end; write out the exact command with its dots escaped
rather than a prefix like `^uv run`. A `$` in a pattern is escaped for compose by the render.

An allowed command runs the repository's own code inside the `updates` container, as the
Renovate user, with the network, the clones of every repository in `repositories` and
`AYE_UPDATES_TOKEN` within reach (the environment Renovate hands the command is trimmed, but
the process runs as the same user as Renovate and can read its environment through `/proc`).
Renovate's branches start from the default branch and it stops touching a branch someone else
pushed to, so whoever can merge into the default branch can make the next run do anything the
token allows, in every repository the token covers. Allow only commands of repositories whose
default branch only trusted people can change, keep the token limited to `repositories`, and
leave the module on a host away from production secrets. Renovate runs the command with its argument list split like a shell
would but without a shell (`allowShellExecutorForPostUpgradeCommands` stays off), so pipes,
`&&` and `$VAR` do not work and cannot be smuggled in through a template.

The tools such a command needs are installed by Renovate on demand, through Containerbase in
the pinned image, when the repository names them in `postUpgradeTasks.installTools` (for
example `uv`, which then downloads the Python the project asks for), and the command shares
the container's memory limit with Renovate. AyeOps itself uses this: an image bump changes more of the committed
`examples/rendered` than the `image:` lines Renovate edits (the image lists of the `vulns` and
`eol` agents, the portal, the render index), so its `renovate.json` renders the example again on
every image branch and commits only `examples/rendered/**`. The host that runs Renovate for it
allows exactly that command:

```yaml
updates:
  repositories: [AyeOps/ayeops]
  allowed_commands:
    - '^uv run --frozen --no-dev aye render examples/acme\.yaml -o examples/rendered --build-time "2026-01-01 00:00 UTC" --commit example$'
```

Without it the pull request still comes, with the `image:` lines bumped and Renovate's
"Artifact update problem" comment naming the command it was not allowed to run; render the
example by hand on the branch then. A host that does not want to run repository code at all
can leave `allowed_commands` empty and let a workflow of the repository render and push to
the Renovate branch instead, which needs a token with contents write in the workflow, and on
GitHub a push made with the workflow's own `GITHUB_TOKEN` starts no new checks.

## vulns

Known vulnerabilities in the images the host runs, from [Trivy](https://trivy.dev) once a day.
`image-updates` and `updates` say that a newer image exists; this module says why it matters:
which running image carries a critical flaw that a newer package already fixes.

```yaml
hosts:
  edge:
    modules:
      node: {}
      vulns:
        schedule: "05:00"
        exclude: [ghcr.io/acme/legacy]
```

Three containers, none with a published port:

- `vulns-proxy` (`tecnativa/docker-socket-proxy` `v0.5.0`) mounts the docker socket read-only
  and answers only `GET` for image inspect and export (`IMAGES=1`, `PING=1`, `VERSION=1`; the
  container list, every other endpoint and every `POST` refused). It sits alone with Trivy on
  the internal network `vulns-docker`.
- `vulns` (`aquasec/trivy` `0.76.1`, pinned by the digest of its multi-arch index since the
  tags of Trivy were pushed again with malicious images in March 2026, CVE-2026-33634; amd64
  and arm64, read-only) runs `vulns_loop.sh`: it waits
  for a request file, refreshes the vulnerability database once (`--download-db-only`), then
  scans each requested image with `--skip-db-update` under a `timeout_minutes` limit and writes
  one JSON report per image and its exit code. Trivy reads the image from the local docker
  images through the proxy (`TRIVY_IMAGE_SRC=docker,remote`), so it scans exactly what runs,
  private and locally built images included, without registry credentials; only an image that
  is not on the host is pulled from its registry. The database (`trivy-db`, and the Java
  database when an image has jar files) and the exported images while they are scanned live
  in the `vulns-cache` volume; count about 1 GB of disk for it, more while a large image is
  scanned.
- `vulns-agent` (`python:3.13.16-alpine3.23`, no network, read-only) asks for a run at
  `schedule` in `timezone`, at once on a host that has never run and whenever the list of
  images it was rendered with differs from the last run's, and turns each finished run into
  textfile metrics. It reads the database date from the cache volume, mounted read-only.

Which images: `aye render` writes into `AYE_VULNS_IMAGES` of `vulns-agent` every image of the
host's rendered project, the AyeOps modules and the host's `compose` files together, of every
service that is kept running (no `profiles`, no `replicas: 0`, no `restart: "no"`). An image
whose reference holds a `${VARIABLE}` is skipped: it is only known on the host. An `aye deploy`
that changes the list recreates `vulns-agent`, which then asks for a scan right away.

| Setting | Default | Meaning |
|---|---|---|
| `schedule` | `05:00` | daily run, `HH:MM` in `timezone` |
| `timezone` | the manifest's [`timezone`](manifest.md#top-level) | time zone of `schedule` |
| `timeout_minutes` | `15` | limit of the database download and of each image scan, 1 to 120; a scan that takes longer counts as failed |
| `exclude` | `[]` | images not to scan: an exact reference (`nginx:1.29.2-alpine`) or a name for every tag (`ghcr.io/acme/legacy`) |

Trivy counts a finding per vulnerability and package: the same CVE in `libssl` and `libcrypto`
counts twice, as Trivy's own table does, and a finding is `fixed="true"` when the advisory
names a `FixedVersion`. Only the `vuln` scanner runs; secrets, misconfigurations and licenses
are not scanned. Memory is 1 GB for Trivy, which holds the database and the package list of one
image at a time; raise it with `resources` for very large images. The database is published
about every 6 hours on `mirror.gcr.io` and `ghcr.io`; one download a day stays far below their
limits.

Run it now with
`docker compose exec -e AYE_VULNS_ONCE=1 vulns-agent python -I /app/vulns_agent.py`; the loop
picks the request up within 15 seconds. Each finished run logs one JSON line from `vulns-agent`
with `success`, `db_update`, the `failed` images and `critical_fixable` per image; the reports
stay in `/state/reports` until the next run.

Metrics, in `ayeops_vulns.prom` in the [node](#node) textfile directory:

| Metric | Meaning |
|---|---|
| `ayeops_vulns_vulnerabilities{image,severity,fixed}` | findings of the last scan of the image by `critical`, `high`, `medium`, `low`, `unknown` and `fixed` `true` or `false`; every combination is written, 0 included; absent for an image whose scan failed |
| `ayeops_vulns_image_scan_success{image}` | 1 when the last run wrote a report for the image, 0 when it did not |
| `ayeops_vulns_images{result}` | images of the last run by `ok` and `failed` |
| `ayeops_vulns_last_run_timestamp_seconds` | end of the last run, good or not; a fresh agent starts the clock at its first start |
| `ayeops_vulns_last_success_timestamp_seconds` | last run with the database refreshed and every image scanned |
| `ayeops_vulns_last_run_success`, `_run_duration_seconds` | the last run |
| `ayeops_vulns_db_update_success`, `_db_updated_timestamp_seconds` | the database step of the last run, and when the database in the cache was built |

Alerts: `vulns-critical-fixable` (an image with a critical finding that has a fix, for 1h, one
alert per image), `vulns-scan-failed` (the last run wrote no report for an image, one alert per
image; all of them at once when the database did not download), `vulns-scan-stale` (no run
finished for 2 days). All three are `warning`: a known vulnerability is work for the day, not a
reason to wake anyone.

## eol

Until when the products a host runs get support and security fixes, from
[endoflife.date](https://endoflife.date) once a day. [`updates`](#updates) and
[`image-updates`](#image-updates) say that a newer version exists; this module says when staying
on the current one stops being safe: PostgreSQL 14, Ubuntu 22.04 or Node 20 losing security
fixes in N days.

```yaml
hosts:
  central:
    modules:
      node: {}
      eol:
        schedule: "05:30"
        exclude: [ghcr.io/acme/legacy]
        components:
          - name: acme web app
            product: django
            cycle: "5.2"
```

One container, without a published port: `eol-agent` (`python:3.13.16-alpine3.23`, read-only,
all capabilities dropped) runs `eol_agent.py`, a standard-library script. At `schedule` in
`timezone`, at once on a first start or when the cache is more than a day old, and every hour
after a failed attempt, it asks `GET <api>/products/<product>` of the
[endoflife.date API v1](https://endoflife.date/docs/api/v1/) once per tracked product, with
`If-None-Match` and `If-Modified-Since` from the last answer, and keeps each answer in the
`eol-cache` volume. A failed fetch (an HTTP error, a timeout, no JSON, no `releases`) keeps the
last cached answer, so the days left stay right while the API is away; only the success
timestamp stops. The agent rewrites the textfile from the cache every hour, so the days count
down without a fetch. It reaches the internet over the project network and nothing else.

What it tracks:

- **Images.** `aye render` takes the same list of images as [vulns](#vulns) (every image of the
  host's rendered project and its `compose` files that is kept running, without `${VARIABLE}`
  references and without `exclude`) and maps each to an endoflife.date product and cycle with
  the table below, in `src/ayeops/eol.py`. The cycle comes only from a tag that carries it:
  `postgres:16.4` is PostgreSQL 16, `redis:7` and `postgres:latest` are not mapped. The tag's
  base is tracked too: `-alpine3.23` is Alpine 3.23, `-bookworm` Debian 12, `-noble` Ubuntu
  24.04. Images that map to nothing are listed in `ayeops_eol_unmapped`. `aye render` writes the
  result into `AYE_EOL_COMPONENTS` and `AYE_EOL_UNMAPPED` of `eol-agent`, so a deploy that
  changes the images recreates it.
- **The host OS.** With `os: true` the host's `/etc/os-release` is mounted read-only and its
  `ID` and `VERSION_ID` become `component="os"`: `ubuntu` 22.04, `debian` and `raspbian` 12,
  `alpine` 3.20 (from 3.20.3), `rhel`, `rocky`, `almalinux`, `centos`, `fedora`, `amzn` (major),
  `opensuse-leap` 15.6. Any other distribution, or one without `VERSION_ID`, is unmapped. The
  agent reads the file itself rather than joining `node_os_info` of node-exporter centrally:
  node-exporter reads the same file, and this keeps each host's verdict on its own host,
  without a recording rule and with the same alerts as the images.
- **Manual entries.** `components` lists what neither covers, such as a framework inside an
  application image: `name` (the `component` label), `product` (the endoflife.date name, lower
  case) and `cycle` exactly as endoflife.date lists it.

| Image | Product | Cycle from the tag |
|---|---|---|
| `postgres` | `postgresql` | major from 10 (`16`), `9.x` before |
| `redis`, `valkey/valkey`, `mariadb`, `mysql`, `mongo` | `redis`, `valkey`, `mariadb`, `mysql`, `mongodb` | `X.Y` |
| `node` | `nodejs` | major |
| `python`, `golang`, `php`, `ruby` | `python`, `go`, `php`, `ruby` | `X.Y` |
| `nginx`, `nginxinc/nginx-unprivileged`, `traefik`, `haproxy`, `caddy` | `nginx`, `traefik`, `haproxy`, `caddy` | `X.Y`, `caddy` major; a leading `v` is fine |
| `grafana/grafana`, `grafana/grafana-oss`, `grafana/loki`, `prom/prometheus` | `grafana`, `grafana-loki`, `prometheus` | `X.Y` |
| `alpine`, `debian`, `ubuntu` | `alpine-linux`, `debian`, `ubuntu` | `3.22`; `12` or `bookworm`; `24.04` or `noble` |
| `rabbitmq`, `memcached`, `tomcat`, `eclipse-temurin` | the same names | `X.Y`, `eclipse-temurin` major |
| `elasticsearch`, `kibana` (Docker Hub or `docker.elastic.co`), `quay.io/keycloak/keycloak` | `elasticsearch`, `kibana`, `keycloak` | `X.Y` |
| any image whose tag carries `alpine3.Y`, a Debian or an Ubuntu codename | `alpine-linux`, `debian`, `ubuntu` | from that part of the tag |

`docker.io/` and `library/` are ignored. A test checks every product of the table against the
product list of endoflife.date.

| Setting | Default | Meaning |
|---|---|---|
| `schedule` | `05:30` | daily fetch, `HH:MM` in `timezone` |
| `timezone` | the manifest's [`timezone`](manifest.md#top-level) | time zone of `schedule` |
| `api` | `https://endoflife.date/api/v1` | base URL of the API, HTTPS, for a mirror |
| `os` | `true` | track the host's operating system from `/etc/os-release` |
| `components` | `[]` | manual entries `{name, product, cycle}` |
| `exclude` | `[]` | images not to track, as in [vulns](#vulns): an exact reference or a name for every tag |

Dates: support ends on the cycle's `eoasFrom` (end of active support, bug fixes) where the
product has one, otherwise with security support; security support ends on `eolFrom`. Extended
support that is sold (`eoesFrom`: Ubuntu Pro, Debian ELTS, commercial Node support) is not
counted. Days are whole days from today in UTC to that date, 0 on the date itself and negative
after it; a cycle the API marks `isEol` counts as ended even without a date.

Run a fetch now with
`docker compose exec -e AYE_EOL_ONCE=1 eol-agent python -I /app/eol_agent.py`; it prints the
JSON line every fetch logs, with `success`, the `failed` products and their reason, the `soon`
components (under 90 days) with their days, and the `ended` and `unknown` ones, and exits 1 when
a product failed.

Metrics, in `ayeops_eol.prom` in the [node](#node) textfile directory:

| Metric | Meaning |
|---|---|
| `ayeops_eol_security_days_left{component,product,cycle,date}` | days until the end of security support on `date`; absent when endoflife.date has no date |
| `ayeops_eol_support_days_left{component,product,cycle,date}` | days until the end of active support, or of security support for a product without that phase |
| `ayeops_eol_security_ended{component,product,cycle}` | 1 when security support has ended |
| `ayeops_eol_cycle_known{component,product,cycle}` | 1 when the product's data lists the cycle, 0 when it does not; absent while the product has never been fetched |
| `ayeops_eol_unmapped{component}`, `ayeops_eol_unmapped_components` | images (and `os`) that map to no product, and how many |
| `ayeops_eol_fetch_success{product}`, `ayeops_eol_data_timestamp_seconds{product}` | the last fetch of the product, and when its cached data was last confirmed |
| `ayeops_eol_last_fetch_timestamp_seconds`, `ayeops_eol_last_fetch_success` | the last attempt over every product |
| `ayeops_eol_last_success_timestamp_seconds` | last attempt in which every product was fetched; a fresh agent starts the clock at its first start |

Alerts, all `warning` (an end of support is daytime work, nobody is paged for it at night):
`eol-soon` (security support of a component ends in under 90 days, one alert per component and
cycle, with the date), `eol-passed` (it has ended), `eol-cycle-unknown` (the product lists no such
cycle, so neither of the first two could fire) and `eol-data-stale` (no successful fetch for 3
days, although the agent retries hourly).

## cloudflare

Optional. An exporter that polls the Cloudflare
[GraphQL Analytics API](https://developers.cloudflare.com/analytics/graphql-api/) and serves the
numbers to vmagent, so a project behind Cloudflare sees what happens at the edge, before its
own origin: traffic, cache, the statuses visitors got, origin time, and how close the account
is to the free limits of R2, Workers and D1. Like the RDAP exporter it is one stdlib Python
script on `python:3.13.16-alpine3.23`; nothing else in the kit depends on it.

| Setting | Default | Meaning |
|---|---|---|
| `zones` | `[]` | zone names; each is looked up once (`GET /zones?name=`) for its id, account and plan |
| `accounts` | `[]` | account ids whose R2, Workers and D1 usage is read; a zone's account id is in `ayeops_cloudflare_zone_info` |
| `refresh_seconds` | `300` | how often the API is asked |
| `window_seconds` | `900` | length of the window the edge gauges describe |
| `lag_seconds` | `180` | how far before now the window ends, so Cloudflare has ingested it |
| `top_hosts` | `20` | hostnames kept as labels; the rest is `hostname="other"` |
| `top_countries` | `10` | countries kept as labels; the rest is `country="other"` |
| `limits` | free plan | overrides of the limits below for a paid plan |

At least one zone or account is required (rule `module-settings`). The token comes only from
`AYE_CLOUDFLARE_ANALYTICS_TOKEN` and is never logged. It needs read permissions only:

| Permission | On | For |
|---|---|---|
| `Zone > Zone > Read` | the zones | the zone lookup |
| `Zone > Analytics > Read` | the zones | edge traffic |
| `Account > Account Analytics > Read` | the accounts | R2, Workers, D1 |

Create it under My Profile, API Tokens, Create Token, Custom token; a user token may span
several accounts, an account-owned token only its own.

### What it asks and what it serves

Every poll sends one GraphQL query per dataset: four per zone and five per account, against a
budget of 300 queries per 5 minutes per user. The `start` log line prints this exporter's share
and every poll logs a `poll` line with the queries sent and failed.

| Dataset | Window | Metrics |
|---|---|---|
| `httpRequestsAdaptiveGroups` by hostname, status, cache status | `window_seconds` | `ayeops_cloudflare_requests{zone,hostname,status_class,cache_status}`, `ayeops_cloudflare_edge_bytes{zone,hostname,cache_status}`, `ayeops_cloudflare_origin_errors{zone,hostname,status}` for 520-526 |
| same, by country | `window_seconds` | `ayeops_cloudflare_country_requests{zone,country}`, top N plus `other` |
| same, uncached, `avg.originResponseDurationMs` | `window_seconds` | `ayeops_cloudflare_origin_response_seconds_avg{zone,hostname}`, `ayeops_cloudflare_origin_requests` |
| `httpRequests1dGroups` | UTC day so far | `ayeops_cloudflare_day_requests`, `_day_cached_requests`, `_day_bytes`, `_day_cached_bytes`, `_day_threats` |
| `workersInvocationsAdaptive` | UTC day so far | `ayeops_cloudflare_workers_requests_day{account,script}`, `_workers_errors_day`, `_workers_cpu_seconds{quantile}` |
| `r2OperationsAdaptiveGroups` | UTC month so far | `ayeops_cloudflare_r2_operations_month{account,bucket,class}`, class `a`, `b`, `free` as in R2 pricing |
| `r2StorageAdaptiveGroups` | latest point of the last day | `ayeops_cloudflare_r2_storage_bytes{account,bucket}`, `ayeops_cloudflare_r2_objects` |
| `d1AnalyticsAdaptiveGroups`, `d1StorageAdaptiveGroups` | UTC day so far, latest size | `ayeops_cloudflare_d1_rows_read_day{account,database}`, `_d1_rows_written_day`, `_d1_queries_day`, `_d1_storage_bytes` |

The three `httpRequestsAdaptiveGroups` queries keep only `requestSource: "eyeball"`, the
requests visitors sent. Cloudflare also logs its own fetches in the same dataset, and its Early
Hints cache lookups end in 504 with no origin status: on one zone they were 1318 of 1323 5xx in
a day, enough to hold `cloudflare-edge-5xx` pending while every visitor got a page.

The window gauges are counts over the window, not counters: divide by
`ayeops_cloudflare_window_seconds` for a rate, as the dashboard does. The HTTP hostname is the
label `hostname` because vmagent writes `host` as the AyeOps host that scraped it.

Each account also gets `ayeops_cloudflare_usage{account,resource}` and
`ayeops_cloudflare_free_limit{account,resource}`, so one ratio covers every limit:

| `resource` | Free limit |
|---|---|
| `workers_requests_day` | 100 000 |
| `r2_class_a_month` | 1 000 000 |
| `r2_class_b_month` | 10 000 000 |
| `r2_storage_bytes` | 10 GB, compared with the current size, while Cloudflare bills the monthly average |
| `d1_rows_read_day` | 5 000 000 |
| `d1_rows_written_day` | 100 000 |
| `d1_storage_bytes` | 5 GB |

`ayeops_cloudflare_query_success{target,dataset}` is `0` after a failed query, and
`ayeops_cloudflare_dataset_available{target,dataset}` is `0` when Cloudflare refused it
(`authz`, HTTP 401/403): a permission the token lacks or a dataset the plan does not offer.
Origin time is optional, since plans differ in what they expose: a refusal marks it
unavailable and raises no alert. Metrics of a failed query are dropped, not kept stale.

Alerts: `cloudflare-edge-5xx` (more than 2 % 5xx at the edge in the window, at least 100
requests, for 10m), `cloudflare-origin-errors` (any 520-526, 5m), `cloudflare-cache-ratio-drop`
(cache hit ratio, counting `hit`, `stale`, `updating` and `revalidated`, below half of the same
time a day earlier, 30m, info), `cloudflare-free-limit` (any resource above 80 % of its limit,
15m), `cloudflare-query-failing` (a query failing for 30m). Dashboard: `AyeOps / Cloudflare`.

## delivery

Optional, on the host that runs the metrics server. vmalert `v1.153.0` evaluates recording rules
over the samples that [`aye report`](delivery.md) pushes from CI and deploy scripts and writes
the results back to storage, so dashboards and alerts read cheap pre-computed series. It does
not notify anyone itself (`-notifier.blackhole`); the alerts are Grafana rules like every other.

| Setting | Default | Meaning |
|---|---|---|
| `branches` | `[main]` | branches whose reports count for the medians and alerts; reports without a branch (a deploy script) always count |
| `expect` | `[]` | `{step, every, project?}`: a `step` report of `project` (default: the manifest `project`) is expected at least every `every` (`6h`, `2d`); `delivery-silent` fires when it is late or has never come |
| `evaluation_interval` | `1m` | vmalert evaluation interval |

Recorded series, all per `project`, `repo`, `branch`, `pipeline`, `job` and `step` where they
apply: `aye:step_duration_seconds:median30d`, `:median3d`, `:last` (successful runs only),
`aye:step_ok_runs:30d`, `aye:step_runs:1d`, `aye:step_failure_ratio:7d`, `aye:step_success:last`,
`aye:coverage_lines_percent:last` and `:median30d`, `aye:mutation_score_percent:last` and
`:median30d`, `aye:pr_lead_time_seconds:median30d`, and with `expect`
`aye:delivery_last_report_age_seconds` and `aye:delivery_overdue`. The medians need the raw
samples for 30 days; keep `metrics.retention` at `100d` or more.

Alerts: `deploy-failed` (the latest deploy failed, critical), `deploy-slow` (latest deploy more
than twice the 30-day median, at least five deploys), `ci-slower` (3-day median of `ci` 50 %
above the 30-day one for 1h, at least ten runs), `coverage-dropped` (more than 2 points below
the 30-day median), `mutation-score-dropped` (more than 3 points below), `delivery-silent`
(an expected report is late, 15m). Dashboards: `AyeOps / Delivery` (deploys, CI, pull requests,
Actions minutes) and `AyeOps / Quality` (tests, flaky and slowest tests, coverage, mutation
score and survivors by module). Rule `delivery-single`: one host only.

## github

Optional, on the host that runs the metrics server. One stdlib Python script on
`python:3.13.16-alpine3.23`, like the Cloudflare exporter, that does two jobs with one token:

- polls `GET /organizations/{org}/settings/billing/usage?year=&month=` (or `/users/{user}/...`)
  for this month's billed usage and serves it to vmagent;
- lists the `aye-report*` artifacts of the repositories in `reports`, which the
  [GitHub action](delivery.md#use-it) uploads from GitHub-hosted runners that cannot reach
  storage, and imports each one once into VictoriaMetrics, see [delivery](delivery.md#where-the-samples-go).

| Setting | Default | Meaning |
|---|---|---|
| `accounts` | `[]` | `{org: <name>}` or `{user: <name>}`, each with `included_minutes` (default `2000`, the Free plan; Pro `3000`, Team `3000`) |
| `reports` | `[]` | `owner/repo` whose report artifacts are imported |
| `refresh_seconds` | `300` | how often both are polled |
| `lookback_hours` | `72` | artifacts and samples older than this are skipped |

At least one account or repository is required (rule `module-settings`). The token comes only
from `AYE_GITHUB_TOKEN` and is never logged. A classic token needs `read:org` for an
organization's usage (the token's user must be an owner or billing manager), `user` for a
user's usage (GitHub answers `404` without it), and `repo` to read the artifacts of a private
repository. A fine-grained token needs `Administration: read` on the organization for billing
and `Actions: read` on the repositories. Check with `gh api organizations/<org>/settings/billing/usage`.

Metrics: `ayeops_github_usage{account,product,sku,repo,unit}` (this month's quantity),
`ayeops_github_usage_gross_dollars{account,product}` and `_net_dollars` (net is what is billed
after the included quota), `ayeops_github_included_minutes{account}` and
`ayeops_github_included_minutes_used{account}`. The used minutes count as GitHub counts them
against the quota: Actions minutes of private and internal repositories (visibility is looked up
once a day per repository), Windows twice, macOS ten times, without larger runners, which are
always billed, and without self-hosted runners, which are free and absent from the report. The
importer serves `ayeops_github_report_import_success{repo}`,
`ayeops_github_reports_imported_total{repo}` and `ayeops_github_report_lines_rejected_total{repo}`.

Alerts: `github-minutes-limit` (more than 80 % of the included minutes used, 15m),
`github-query-failing` (a billing query failing for 30m), `github-report-import-failing`
(artifacts of a repository not listed or not imported for 30m). The minutes are on
`AyeOps / Delivery`.

## ai-usage

An exporter (`python:3.13.16-alpine3.23`, `ai-usage-exporter`, port 9147) for what the AI
providers of the project cost and allow: the spend of the month and of the day by model, the
tokens behind it, the configured rate limits and the expiry of API keys, read from the
providers' own admin reports, the same numbers as their consoles' usage and cost pages.

| Setting | Default | Meaning |
|---|---|---|
| `providers` | required | `anthropic`, `openai` or both |
| `monthly_budget_usd` | `{}` | per provider, e.g. `{anthropic: 50}`; without one there are no budget alerts for it |
| `spike_floor_usd` | `1` | a day below this never counts as a spike |
| `refresh_seconds` | `900` | poll interval, at least 300 |

Each listed provider needs its admin key in the host's `.env`; a provider whose key is empty
is reported as `ayeops_ai_provider_enabled 0` and sent no request.

| Provider | Key | Endpoints |
|---|---|---|
| Anthropic | `AYE_ANTHROPIC_ADMIN_KEY`, an Admin API key `sk-ant-admin01-…` (`platform.claude.com/settings/admin-keys`, organization admins only; individual accounts have no Admin API) | `GET /v1/organizations/cost_report` grouped by `description`, `GET /v1/organizations/usage_report/messages` grouped by `model`, `GET /v1/organizations/rate_limits`, `GET /v1/organizations/api_keys?status=active` |
| OpenAI | `AYE_OPENAI_ADMIN_KEY`, an Admin key `sk-admin-…` (`platform.openai.com/settings/organization/admin-keys`, organization owners) | `GET /v1/organization/costs` grouped by `line_item`, `GET /v1/organization/usage/completions` grouped by `model` |

A normal API key is refused by these endpoints. The key goes only in the provider's own
header (`x-api-key` for Anthropic, `Authorization: Bearer` for OpenAI) and never into logs,
URLs or metrics.

Days and months are UTC, as both providers bucket them: each poll asks for daily buckets from
the first of the UTC month or seven days back, whichever is earlier, so the month-to-date and
the median of the last seven days come from one answer. Anthropic reports cost in cents as a
decimal string and OpenAI in dollars; both arrive as `*_usd`. Anthropic costs carry the model;
OpenAI's carry a line item such as `gpt-5-mini, input`, and the model is the part before the
first comma. Costs without a model (web search, code execution) get their cost type or line
item as `model`. Reports lag the requests by about five minutes; Anthropic asks for at most one
poll per minute.

| Metric | Labels | Meaning |
|---|---|---|
| `ayeops_ai_cost_month_usd`, `ayeops_ai_cost_day_usd` | `provider`, `model` | cost of the UTC month and day so far |
| `ayeops_ai_cost_daily_median_7d_usd` | `provider` | median daily cost of the seven UTC days before today, days without cost count as 0 |
| `ayeops_ai_tokens_month`, `ayeops_ai_tokens_day` | `provider`, `model`, `kind` | `input_uncached`, `input_cache_read`, `input_cache_write`, `output` |
| `ayeops_ai_requests_month` | `provider`, `model` | model requests, OpenAI only (Anthropic does not report them) |
| `ayeops_ai_rate_limit` | `provider`, `limit_group`, `limit` | Anthropic organization limits, e.g. `output_tokens_per_minute` of `Claude Opus 5.5` |
| `ayeops_ai_api_key_expiry_timestamp_seconds` | `provider`, `key` | active Anthropic keys that have an expiry date |
| `ayeops_ai_budget_usd`, `ayeops_ai_spike_floor_usd` | `provider` | the settings above |
| `ayeops_ai_query_success` | `provider`, `dataset` | 1 when the last query of that report answered |
| `ayeops_ai_last_success_timestamp_seconds` | `provider` | the last poll in which every report of the provider answered |

Alerts: `ai-budget-80` (month-to-date above 80 % of the budget, warning), `ai-budget-exceeded`
(above 100 %, critical), `ai-spend-spike` (today above three times the median day and above
the floor, for 30m), `ai-query-failing` (a report failing for 3h), `ai-key-expiring` (an
active key expires within 14 days). The dashboard `AyeOps / AI` puts the providers side by
side, including the actual cost per million tokens of each model: the month's cost divided by
all its tokens, cache reads included, which is what a model really costs you with your cache
hit rate, not its list price.

## sentinel

Who watches the watcher. Every alert of the kit leaves through the Grafana of the central host,
so when that host dies, nobody hears about it. The sentinel is one stdlib Python script on
`python:3.13.16-alpine3.23` (48 MB limit) that runs on **another** host, checks the central
one every minute and talks to Telegram itself, or to Slack with `alerting.contact: slack`.

```yaml
hosts:
  web:
    modules:
      sentinel:
        watch: central
```

| Setting | Default | Meaning |
|---|---|---|
| `watch` | required | the host to watch; it must exist, must not be the sentinel's own host and must not share its `zone` (rule `sentinel-watch`) |
| `checks` | derived | overrides of the derived checks by `name`, see below |
| `failures_before_alert` | `3` | rounds in a row with at least one failing check before the first message |
| `reminder` | `6h` | how often a message repeats while the host stays down |
| `interval` | `60s` | time between rounds |
| `timeout` | `10s` | per check |

Without `checks`, the watched host's own modules give the defaults:

| Name | Kind | From | Passes when |
|---|---|---|---|
| `grafana` | `http` | `grafana.root_url` over HTTPS, else `http://<address>:3000` if exposed | `/api/health` answers 2xx with `"database": "ok"` |
| `victoriametrics` | `http` | `metrics` server exposing `8428` | `/health` answers 2xx |
| `samples` | `fresh` | the same | `time() - max(timestamp(up))` is at most `max_age_seconds` (180): vmagent still writes into storage |

An entry with a known name changes that check (`{name: samples, max_age_seconds: 300}`),
`enabled: false` drops it, and a new name adds one. Kinds:

| Kind | Fields | Passes when |
|---|---|---|
| `http` | `url`, optional `contains` | 2xx without following redirects, the body contains `contains`; a redirect to a sign-in page is a failure |
| `fresh` | `url` of `/api/v1/query`, optional `query`, `max_age_seconds` | the query returns a number at most `max_age_seconds` |
| `tls` | `host`, optional `port` (443), `min_days` (7) | the certificate verifies and is valid for at least `min_days` |

A watched host without Grafana or a metrics server has no defaults and needs `checks` (rule
`sentinel-checks`). The Traefik certificate is not checked by default, since the HTTPS
`grafana` check already fails on a bad one; add `{name: tls, kind: tls, host: grafana.<domain>}`
for an early warning on expiry.

### Messages

After `failures_before_alert` failing rounds it sends **one** message naming every failing
check and the time of the first failure, then one every `reminder` while it stays down, and an
all-clear with the length of the outage when a round passes again. A message that Telegram did
not accept is sent again next round; an all-clear that did not arrive keeps the alert open, so a
relapse is not announced twice. Messages are plain text to `AYE_TELEGRAM_BOT_TOKEN` and
`AYE_TELEGRAM_CHAT_ID`, the same chat as the Grafana contact point. With
[`alerting.contact: slack`](manifest.md#slack) they go to the Grafana contact point's incoming
webhook in `AYE_SLACK_WEBHOOK_URL` instead, as plain text with `&`, `<` and `>` escaped, and the
host needs no Telegram bot; with `email` or `none` the sentinel stays on Telegram.

The sentinel shares the fate of its own host. When it and the host it watches sit with one
provider (or in one zone of it), turn on the status Worker's [notices](status.md#notices) as
well: the Worker runs on Cloudflare, sees the same outage through its probes and push gaps, and
still pages when the provider is down. With no second host at all, the Worker is the watch
instead of the sentinel, limited to what is public or pushed.

### Dead-man ping

When a whole round passes, the sentinel fetches `AYE_SENTINEL_PING_URL`. If the sentinel's host
dies, or loses the network, or the central host stays down, the pings stop and the service
behind the URL tells you by its own channel. With [healthchecks.io](https://healthchecks.io):

1. New check, name `<project> sentinel`, schedule **Simple**, period **1 minute**, grace
   **5 minutes**.
2. Integrations: email (on by default), and Telegram if you want both; email keeps working when
   Telegram or the bot token is the problem.
3. Copy the ping URL (`https://hc-ping.com/<uuid>`) into `AYE_SENTINEL_PING_URL` in the `.env`
   of the sentinel's host.

Empty turns the ping off. A placeholder or a wrong URL does not stop the watch: the ping fails,
`ayeops_sentinel_ping_success` is `0` and `sentinel-ping-failing` fires on the central host.

### Reachability

The sentinel must reach what it checks. When the central host publishes Grafana and
VictoriaMetrics on a private address (a Tailscale `100.x` address, a WireGuard tunnel), the
sentinel's host has to be on the same network. Two ways:

| Option | What it watches | Cost |
|---|---|---|
| join the private network (`tailscale up`) | Grafana, VictoriaMetrics and the freshness of samples, the same paths the fleet uses | one more agent on the host (tailscaled, 30-50 MB resident) |
| probe public endpoints only (`checks` with public URLs, e.g. the status page) | only what is public; a dead VictoriaMetrics behind a live website goes unseen | nothing on the host |

Joining is the only option that can tell "the monitoring is dead" from "the site is up". A
broken tunnel from the sentinel's side also fires: the message then names every check with a
connection error while the central host's own alerts keep arriving.

### Metrics and alerts

`/metrics` on `9134`: `ayeops_sentinel_check_success{watch,check}`,
`ayeops_sentinel_check_duration_seconds`, `ayeops_sentinel_failure_streak{watch}`,
`ayeops_sentinel_alerting`, `ayeops_sentinel_last_round_timestamp_seconds`,
`ayeops_sentinel_last_notice_ok`, `ayeops_sentinel_notices_total{kind}`,
`ayeops_sentinel_notice_failures_total{kind}`, `ayeops_sentinel_ping_success`. With `metrics`
on the sentinel's host vmagent scrapes it and ships it to the central storage, so the central
Grafana watches the sentinel in turn: `target-down`, `sentinel-notify-failing` (the last
message did not reach Telegram or Slack), `sentinel-ping-failing` (15m), `sentinel-stalled` (no finished
round for 5 minutes). `/health` answers 503 once rounds stop. `node` on the same host adds
`host-down` for the sentinel's machine.

Each check logs one JSON line when it starts failing, when its error changes and when it
passes again, not every minute.

### Without Docker

The script needs nothing but Python 3.12 or newer, so a small host without Docker can run the
rendered `sentinel/sentinel.py` under systemd with the environment of the compose service:

```ini
[Service]
ExecStart=/usr/bin/python3 -I /opt/ayeops/sentinel/sentinel.py
EnvironmentFile=/opt/ayeops/sentinel.env
DynamicUser=yes
MemoryMax=48M
Restart=always
```

`sentinel.env` holds the `AYE_SENTINEL_*` lines of the rendered `compose.yaml` plus the two
`AYE_TELEGRAM_*` values (or `AYE_SLACK_WEBHOOK_URL`) and `AYE_SENTINEL_PING_URL`. `aye deploy` and the config hash do not
cover this path.

## oncall

On-call escalation for the alerts that must not wait for someone to glance at a chat. Grafana
keeps sending every alert to the ops channel as before; `critical` and `page` alerts also go to
[GoAlert](https://goalert.me) `v0.35.0`, which keeps each alert, who acknowledged it and when,
and runs the escalation steps of the manifest. A bridge (stdlib Python on
`python:3.13.16-alpine3.23`) messages the person on call in Telegram with an acknowledge
button and repeats until someone presses it. It runs on the host of `grafana`, next to
`metrics` and `containers` (cAdvisor watches GoAlert's PostgreSQL, which has no exporter).

```yaml
hosts:
  central:
    modules:
      ingress:
        routes:
          oncall: goalert:8081
      oncall:
        url: https://oncall.ops.acme.example
        person:
          username: oncall
          name: Acme On-call
          email: oncall@acme.example
          telegram_chat_id: "100200300"
        quiet_hours: {start: "23:00", end: "08:00"}
```

| Setting | Default | Meaning |
|---|---|---|
| `url` | required | the public `https://` address of GoAlert, the ingress route above |
| `person` | required | `username` (the GoAlert admin), `name`, `email`, `telegram_chat_id`, optional `phone` in E.164 |
| `language` | `en` | `en` or `uk`, the language of the Telegram messages |
| `repeat_minutes` | `5` | time between repeats of an unacknowledged alert |
| `max_repeats` | `12` | repeats after the first message, then the bridge stops; GoAlert still shows the alert |
| `quiet_hours` | `23:00` to `08:00` in the manifest's [`timezone`](manifest.md#top-level) | `start`, `end`, optional `timezone`; `null` switches quiet hours off |
| `escalation` | one step, the person | the GoAlert escalation steps, see below |
| `email` | `true` | an email contact method for the person, when the manifest has `mail` |
| `twilio` | `false` | a Twilio voice call to `person.phone`, `voice_after_minutes` (default `10`) after the alert |
| `ops_chat_id` | none | the id of the ops group in Telegram, a supergroup, so it starts with `-100` |
| `topics` | `false` | one forum topic per incident in `ops_chat_id`, see below; needs `ops_chat_id` |

**Routing.** The rendered notification policy has two child routes: `severity=~"critical|page"`
to the `oncall` contact point with `continue: true`, then a catch-all to the ops contact. So a
critical alert reaches both, and a stopped GoAlert, bridge or Telegram bot never means silence:
the ops channel still gets it. `aye check` refuses the module with `alerting.contact: none`
(rule `oncall-fallback`). `warning` and `info` never reach GoAlert. With
[`alerting.contact: slack`](manifest.md#slack) the ops channel is Slack, while the messages to
the person on call and the topics stay in Telegram: the acknowledge button needs a bot.

**How a message travels.** Grafana posts to `http://oncall-bridge:8080/grafana` with
`Authorization: Bearer $AYE_ONCALL_WEBHOOK_SECRET`. The bridge adds `dashboard_url` from
Grafana's `panelURL` or `dashboardURL` when the rule has no such annotation and forwards to
GoAlert's Grafana integration. GoAlert `v0.35.0` opens one alert per Grafana alert, so the
bridge first folds a [notification group](#grafana) into one: its fingerprint is a hash of
Grafana's `groupKey`, it stays firing while any alert of the group fires, and with more than
one firing its summary names the count, the scope and the hosts
(`14 x Host down in workers: worker-01, worker-02, ...`; GoAlert cuts a summary at 256
characters). Every notification of the group reaches GoAlert's open alert, so a host that joins
an open group does not page again; the Telegram contact point lists it. GoAlert notifies the person's contact methods; the webhook one
goes back to `http://oncall-bridge:8080/goalert/$AYE_ONCALL_WEBHOOK_SECRET`. GoAlert does not
sign webhooks, so the secret in the path and `Webhook.AllowedURLs` are the check. The bridge
sends the Telegram message with the summary, the dashboard, runbook and GoAlert links and an
Acknowledge button. The button press arrives by long polling; the bridge acknowledges in GoAlert
with an API key whose only allowed query is the acknowledge mutation, and GoAlert's status
update removes the button. Grafana resolving the alert closes it in GoAlert, and the message
gets a Resolved reply. No service of the module publishes a port; GoAlert's web UI is reachable
only through the ingress route, behind the `auth` sign-in and GoAlert's own login.

**Repeats and quiet hours.** GoAlert notifies once per step (`repeat: 0` on the policy); the
repeats are the bridge's, because they depend on the time of day:

| Alert | During the day | During quiet hours |
|---|---|---|
| `critical` | a message with sound, then every `repeat_minutes` | one silent message (`disable_notification`), no repeats; when quiet hours end, the repeats start with sound if nobody acknowledged |
| `page` | as `critical` | as during the day: sound and repeats |

A day alert still open when quiet hours begin pauses and resumes at their end. No module maps an
alert to `page`; set `severity: page` on the few alerts worth waking up for. A Telegram message
cannot break through a phone's do-not-disturb, so real night wake-up needs `twilio: true` and a
voice call; Twilio calls ignore quiet hours.

**A separate bot.** A Telegram bot token can be long-polled by one process only. The token of
the alerting contact point (`AYE_TELEGRAM_BOT_TOKEN`) is often the product's own bot, polled by
the product; the bridge therefore takes its own `AYE_ONCALL_TELEGRAM_TOKEN`. Create a bot with
BotFather, send it `/start` from the person's account and it replies with the chat id for
`person.telegram_chat_id`. If another process polls the same token anyway, Telegram answers
`409 Conflict`; the bridge then stops polling for good instead of fighting over the token,
keeps sending messages without working buttons, and `oncall-telegram-silent` fires.

**A topic per incident.** With `topics: true` the ops group gets one forum topic per incident,
that is per GoAlert alert, so per folded notification group: 14 workers going down are one
topic, not 14. People discuss the incident where its alert is, and the topic is its history
afterwards:

```yaml
oncall:
  ops_chat_id: "-1001234567890"
  topics: true
```

The group must have Topics turned on, and the bridge's bot must be an administrator with the
right to manage topics (`aye permissions` lists it). The bridge creates the topic
`🔴 <summary>` and posts the summary, the dashboard, runbook and GoAlert links and the
Acknowledge button into it. Everything about the incident follows in the topic, silently: each
repeat, a `🔴 Now: <summary>` line whenever a later notification of the group changes which
alerts fire (a host joins or recovers; GoAlert's alert keeps its first summary, so this is the
only place that shows it), who acknowledged and at what time (the time zone of `quiet_hours`,
else the manifest's [`timezone`](manifest.md#top-level)), the diagnosis of an escalation
`webhook:` step when it arrives (below), and the resolve with the incident's duration. On
resolve the bridge renames the topic to `✅ <summary>` and closes it.

The fold gives the alert its group's fingerprint, and the bridge also writes it as annotation
`fingerprint`, which GoAlert passes on in the alert details. A new alert with the same
fingerprint within 6 hours of the close reopens that topic instead of creating one, even when
other hosts of the group fire this time; so a flapping group keeps one topic. An alert that did
not come through the fold (GoAlert metadata `fingerprint`, else the rule name and the summary)
matches the same way. The bridge keeps its topics in its state, so this survives a restart.
The button in the group acknowledges the whole incident like the one in the direct message,
for anyone in the group. The direct messages to the person stay exactly as without topics,
with sound, repeats and quiet hours, so a muted group never silences the escalation; the first
post in the topic is silent only during quiet hours. Grafana's own messages to the group, the
per-host list of each notification included, stay in General.

The bridge checks the group at start and every hour. When the group is not a forum, the bot is
not in it or lacks the right, the bridge sends direct messages only, logs
`oncall-topics-unavailable` once with the reason, and `ayeops_oncall_topics_available` is 0,
which fires the alert `oncall-topics-unavailable`. A failed topic call counts as
`failures_total{op="topics"}` and makes the bridge check the group again.

**Escalation steps and the autopilot.** `escalation` lists GoAlert steps in order, each with an
optional `delay_minutes` before the next one (GoAlert's minimum is 1):

```yaml
escalation:
  - delay_minutes: 5
    webhook: http://responder.example:8080/diagnose
  - notify: person
```

`notify: person` targets the person; `webhook:` posts GoAlert's alert notification to that
address. This is an extension point for an automatic first responder: it gets the alert first,
diagnoses read-only, and writes its finding back to the alert as GoAlert metadata `diagnosis`
(text) and `diagnosis_url` (a link). The [autopilot](#autopilot) module takes Grafana's own
webhook instead and needs no such step. When the person's step notifies, the bridge reads those
two keys from the alert metadata, and also from rule annotations of the same names, and puts
them into the message. A change of the steps creates a new policy named after a hash of the
steps and moves the service to it.

**Setup.** GoAlert migrates its database and creates the admin before it starts serving. The
bridge, at every start and so after every change of the module, writes the manifest into GoAlert
over its GraphQL API, retrying with growing pauses until GoAlert answers:
the application config (webhooks allowed only to the bridge, SMTP from `mail`, Twilio), the
person's name and email, the contact methods with an immediate notification rule, the policy,
the service with its Grafana integration key, the acknowledge-only API key and, for the
[incident journal](#incidents), an API key that may only read alerts and their log (a new one
when that query changes). It verifies the
webhook contact method itself: GoAlert sends the code to the bridge, the bridge leaves it in the
shared volume. Email verification is the person's: GoAlert mails a code, enter it in GoAlert
under the profile's contact methods; until then email stays disabled.

Secrets in `.env`: `AYE_ONCALL_DB_PASSWORD`, `AYE_ONCALL_ENCRYPTION_KEY` (encrypts contact
methods and keys in the database; keep it with the backups), `AYE_ONCALL_ADMIN_PASSWORD`,
`AYE_ONCALL_WEBHOOK_SECRET`, `AYE_ONCALL_TELEGRAM_TOKEN`, with `incidents` on the host
`AYE_ONCALL_ACKS_SECRET` (the journal's key to `GET /acks`, see
[Acknowledgements](#incidents)), and with `twilio` also
`AYE_ONCALL_TWILIO_ACCOUNT_SID`, `AYE_ONCALL_TWILIO_AUTH_TOKEN`, `AYE_ONCALL_TWILIO_FROM_NUMBER`.

Metrics from the bridge (job `oncall-bridge`): `ayeops_oncall_messages_total{kind,quiet}`,
`ayeops_oncall_relayed_total{code}`, `ayeops_oncall_acks_total`,
`ayeops_oncall_finished_total{state}`, `ayeops_oncall_failures_total{op}` with `op` one of
`telegram`, `goalert`, `relay`, `poll`, `setup`, `topics`, `acks`, `ayeops_oncall_open_alerts`,
with `incidents` `ayeops_oncall_acks_reads_total`,
`ayeops_oncall_last_poll_timestamp_seconds`, `ayeops_oncall_poll_conflict`, and with `topics`
`ayeops_oncall_topics_available`, `ayeops_oncall_topics_total{kind}` (`created`, `reopened`),
`ayeops_oncall_topic_messages_total`; GoAlert's own on
`goalert:9105` (job `goalert`). Alerts: `oncall-telegram-silent` (no Telegram poll for 10
minutes), `oncall-failing` (more than two failures of one kind in 15 minutes),
`oncall-topics-unavailable` (the ops group cannot hold the incident topics), `target-down` of
both jobs; `ayeops_oncall_setup_ok` is 0 while the setup has not got through, and every failed
attempt counts as `failures_total{op="setup"}`. The module does not dump the GoAlert database;
the bridge rebuilds everything in it except the alert history.

## incidents

The incident journal: what happened, for how long, to which service and at what time of day,
measured before anything tries to fix it. It is the baseline of the
[autopilot](autopilot.md)'s north star, downtime weighted by what the service is worth. One
stdlib Python agent (`incidents-agent`, `python:3.13.16-alpine3.23`, port 9136) on the host of
`grafana`; it changes nothing and pages nobody.

```yaml
hosts:
  central:
    modules:
      incidents: {}
```

| Setting | Default | Meaning |
|---|---|---|
| `refresh_seconds` | `60` | how often the alert state history is read |
| `backfill_days` | `30` | how far back the first start reads, so the journal starts with history |
| `downtime_severities` | `[critical, page]` | incidents of these severities count as downtime; others are still incidents |
| `degraded_severities` | `[warning]` | incidents of these severities count as degraded minutes, apart from downtime |
| `host_alerts` | `[host-down, {alert: target-down, job: node}]` | module alerts that are about a whole host: their incidents count against every manifest service of that host, see below. An entry is an alert name, or `alert:` with labels the instance must carry, such as `job: node`; an alert no module has fails rule `incidents-host-alert` |
| `night` | quiet hours of `oncall`, else `23:00` to `08:00` | `start`, `end` and optional `timezone` of the hours whose incidents count as night incidents; the zone defaults to the manifest's [`timezone`](manifest.md#top-level) |

**Where the history comes from.** Grafana writes every state change of every alert instance
(`Normal` to `Pending`, `Pending` to `Alerting`, `Alerting` to `Normal`, with a reason such as
`Alerting (NoData)` or `Normal (MissingSeries)`) to its alert state history. In Grafana
`13.2.3` the history is on by default and its backend defaults to `annotations`, rows in
Grafana's own database; the kit sets no `[unified_alerting.state_history]`, so that is where it
is, and it is kept forever unless `[unified_alerting.state_history.annotations]` limits it. The
agent reads it from `GET /api/annotations?type=alert&from=&to=&limit=` with the `Viewer` token
of the [`grafana-reader`](#grafana-reader), not with the Grafana admin: it mounts the
`grafana-reader` volume read-only (`AYE_GRAFANA_READER_TOKEN_FILE=/run/grafana-reader/token`,
group `10470`), waits for the reader to be healthy, reads the file for every request and once
more after a `401` (retrying only when the file holds another token), and never logs it. It
has no Grafana secret in `.env`. It reads in windows of 6 hours, paging backwards through a window of
more than 1000 changes, and re-reading the last 5 minutes so a change written late is not
missed. The labels
of an instance come from the annotation text Grafana writes, `<title> {<labels>} - <values>`,
and from its `key:value` tags when the text does not parse. The Loki backend would need a
setting on Grafana and Loki on the same host; the annotations need neither.

**An incident** is one alert instance from its first `Alerting` to the next `Normal`: one per
fingerprint, a hash of the rule and the instance labels, so a repeat of the same alert while
it fires joins the open incident. `Pending` that never fires is no incident; `Recovering` and
`Error` keep an incident open. The start is when the alert fired, so the duration is the time
to restore as the [design](autopilot.md) defines it, alert to healthy.

| Field | Meaning |
|---|---|
| `service` | the `service` label of a service alert; for a module alert the `module` label, e.g. `node` |
| `services` | the services whose downtime the incident counts for: `service`, and for a host alert every manifest service of its host |
| `modules` | for a host alert the other modules enabled on its host, affected but not counted; empty otherwise |
| `host` | the `host` label of the instance, empty for an alert without one |
| `severity`, `criticality`, `weight` | the alert's severity; the service's criticality and its weight, see below |
| `start`, `end`, `start_ts`, `end_ts`, `duration_seconds` | UTC times in ISO 8601 and Unix seconds |
| `downtime` | the severity is in `downtime_severities` |
| `degraded` | the severity is in `degraded_severities` |
| `night` | the incident started in the night hours |
| `acknowledged` | `true` when the GoAlert alert of the incident was acknowledged, `false` when it had no acknowledgement at the read that closed the incident (an acknowledgement after that does not count), `null` without `oncall`, for a severity GoAlert never gets, or when no GoAlert alert matched; see below |
| `acknowledged_at`, `acknowledged_ts`, `acknowledge_seconds` | the first acknowledgement in UTC ISO 8601 and Unix seconds, and the time to acknowledge from the incident's start, never below 0 |
| `acknowledged_by`, `acknowledged_via` | the person (`null` when GoAlert names nobody) and how: `telegram`, `web`, a contact method, or `api-key` |
| `goalert_alert` | the id of the GoAlert alert the incident was matched to |
| `lost_requests` | the requests the incident cost its services behind the ingress, see below; `null` when none of them is behind the ingress or none could be measured |
| `requests` | per service behind the ingress: `served`, `expected`, `lost` and the `weeks` of baseline, or `lost: null` with a `reason` |
| `id`, `fingerprint`, `alert`, `rule_id`, `labels`, `timeline` | identity, the rule title, all labels and the state changes in between (at most 50) |

Each closed incident is appended as one JSON line (`schema: 1`, `source: grafana-annotations`)
to `incidents.jsonl` in the volume `incidents-journal`, next to the agent's cursor in
`state.json`; the record is the draft of the postmortem. Read it with
`docker compose exec incidents-agent cat /tmp/incidents.jsonl`; the [autopilot](#autopilot) on
the same host mounts the volume read-only to label its runs. An open incident lives in the
state until it closes. The agent writes the journal and the state before it moves the cursor,
so a failed write is retried on the next round and a restart never records an incident twice.

**Acknowledgements.** With [`oncall`](#oncall) on the host (both run on the host of
`grafana`), an incident of a severity that reaches GoAlert, `critical` or `page`, gets who
acknowledged it in GoAlert and when. GoAlert `v0.35.0` keeps both only in its alert log, behind
its GraphQL API, and an API key there is bound to one GraphQL document and minted by an admin
session, so the kit cannot generate one ahead. The bridge's setup creates a second key next to
the acknowledge key, `oncall-bridge alert log reader`, whose only allowed document reads alerts
with their log (`alerts(input:) { nodes { alertID status summary details createdAt
recentEvents(input: {limit: 100}) { nodes { timestamp message } } } pageInfo { endCursor
hasNextPage } }`): it can read alerts and the 100 newest entries of each log, and do nothing
else. It stays in the bridge's `setup.json` (mode 0600); the journal holds no GoAlert key, and
mounting the bridge's volume would have handed it the acknowledge key too.

The agent asks the bridge instead: `GET http://oncall-bridge:8080/acks?from=&to=` (Unix
seconds, at most 8 days) with `Authorization: Bearer $AYE_ONCALL_ACKS_SECRET`, a secret made
for this one path (hex, 32 bytes, declared by both modules when both run, so
[`aye secrets gen`](#aye-secrets) makes it) that cannot post an alert or acknowledge anything.
The bridge reads GoAlert with the reader key, 100 alerts a page, and answers for each alert
created in the window its id, creation time, the labels of the label table GoAlert writes into
the details, and the first `Acknowledged` entry of its log: when, by whom and how. GoAlert
writes `Acknowledged by <person> (Web)` for its UI and the contact method for a reply to a
notification; an acknowledgement through an API key has no subject in GoAlert's log, so for its
own Telegram button the bridge names the Telegram user who pressed it (`via: telegram`), which
it keeps for 35 days in its state, and any other key shows as `via: api-key` without a name.

The agent asks once per window of at most 6 hours that a round reads, for the `critical` and
`page` incidents that closed in it and, in the newest window, for the open ones nobody has
acknowledged yet, in windows of at most a week. A GoAlert alert belongs to an incident when its `alertname` label is the incident's rule
title, its `host` and `service` labels, where it has them, equal the incident's, and it was
created between 5 minutes before the incident's start and 1 minute after its end (at most 6
hours after the start); the creation closest to the start wins. A [folded](#oncall) group of
several hosts has no common `host` label, so it matches the incident of each host; a host that
joins a group already open in GoAlert does not page again and gets no acknowledgement of its
own (`null`). Two incidents of one GoAlert alert, two probe URLs of a host, share its
acknowledgement. An acknowledgement seen while the incident is open stays in the state, so a
bridge that is down at the close loses nothing; a failed read sets step `acks` to 0 and the
record is written without it, never later. Without `oncall` the agent asks nothing, has no
`acks` step, and the fields are `null` as before. Incidents that closed before the reader key
existed, or while the bridge was down, keep `null`.

**Criticality.** A service may set `criticality: critical | high | normal | low` in the
[manifest](manifest.md#service); without it a service whose [status mark](manifest.md#status-mark)
is public counts as `high`, any other as `normal`, and so does a module. The dashboard weights
downtime minutes by fixed tier weights:

| `criticality` | Weight of a minute down |
|---|---|
| `critical` | 100 |
| `high` | 10 |
| `normal` | 1 |
| `low` | 0.1 |

Downtime of a service is the union of its downtime incidents, so two alerts firing about the
same outage count its minutes once, and splitting an incident cannot lower it.

**Host outages.** A host that is down takes its services with it, while the alert that says so
(`host-down`, a module alert) names only its module, `node`. The incident of an alert in
`host_alerts` therefore counts against every manifest service with the `host:` of the
instance's `host` label. The union above still holds per service, so a `web` outage inside a
host outage counts its minutes once. The incident itself is still one incident of `node` in
`ayeops_incidents_total` and the time to restore; only the downtime is shared.

An entry with labels matches only an instance that carries all of them. `target-down` fires for
any scrape target (`up == 0`), so the default takes it as a host alert only with `job: node`:
node-exporter not answering means the host itself is unreachable. Any other target down, a
single exporter, is an incident of `metrics`, the module whose rule it is, with the exporter in
its `job` label, and costs no service of the host anything. `target-down` is a `warning`, so
with the defaults the node target adds degraded minutes, not downtime.

The modules enabled on the host get no downtime, degraded minutes or lost requests from a host
alert: they are the kit around the services, and counting each of them at its own weight would
multiply one outage by the number of modules. They still count their own incidents from their
own alerts. The record lists them in `modules`, and
`ayeops_incident_affected_module_minutes_total` counts their minutes inside host alerts, as a
union per module like the downtime, without a weight. A record written before `modules` existed
is attributed by the hosts and `host_alerts` of the current manifest.

**Degraded minutes.** A `warning` says the service works, but not well: slow, near a limit,
without a backup. Its minutes are not downtime and are counted apart, as degraded minutes, the
same way: the union per service of the incidents in `degraded_severities`, host alerts shared
with the manifest services of the host. A service that is down and degraded at once has the
minutes in both; the two are separate measures, not parts of one sum.

**Lost requests** need no input. Traefik of the [ingress](#ingress) (`traefik:v3.7.14`) counts
every answer of a route in `traefik_service_requests_total{service="<route>@file", code}`
(service labels are on by default in Traefik 3, router labels are not); the `metrics` module
scrapes it with the `host` label of its host. A route targets a compose service a module
renders, so it counts for that module, or for the manifest service of that name on the host.
When an incident closes, the agent asks VictoriaMetrics on its own host
(`http://victoriametrics:8428`, no credentials) once for all its services behind the ingress:
the successful answers (`code=~"[123].."`) in the incident's window, `increase(...[<duration>])`
at its end, and the same window `offset 1w` to `offset 4w`, the same minutes of the week. The
expected count is the **median** of the weeks that have data, not the mean: with four weeks, one
week that had its own outage or a campaign spike would move a mean by a quarter of its
difference and leaves the median close to a normal week. `lost` is expected minus served, never
below 0. It is `null`, not 0, when the incident is shorter than one scrape interval of the
ingress host (there is no sample inside it), when no week of the baseline has data (a new route,
or a retention under four weeks), or when the query fails; no data during the incident itself
counts as nothing served, since Traefik down serves nothing. The query runs when the close is
read, a minute or so after the end, so the last scrape is usually in. A host alert measures every
routed manifest service of its host, not the routes of its modules. A
[flow baseline](#flow-baselines) alerts on the same median while a flow is low, before anyone
opens an incident.
`ayeops_incident_lost_requests_total` sums the records per service; where incidents of one
service overlap, a later-starting one adds only the share of its lost requests that falls
outside the earlier ones, assuming the loss spread evenly over its window, so the shared minutes
count once.

Metrics (job `incidents`; the `host` label is the host that scrapes the agent, so the incident's
own host is `alert_host`):

| Metric | Labels | Meaning |
|---|---|---|
| `ayeops_incidents_total` | `service`, `severity`, `criticality` | incidents, open ones included |
| `ayeops_incidents_night_total` | `service`, `severity` | incidents that started at night |
| `ayeops_incidents_open` | `service`, `severity`, `alert_host` | incidents open now |
| `ayeops_incident_restore_seconds` | `service`, `severity` | histogram of the time to restore of closed incidents, buckets 1m to 1d |
| `ayeops_incident_acknowledge_seconds` | `criticality`, `period` (`day`, `night`) | with `oncall`: histogram of the time to acknowledge of closed incidents, buckets 30s to 12h; `night` is the night hours above |
| `ayeops_incidents_unacknowledged_total` | `criticality`, `period` | with `oncall`: closed incidents whose GoAlert alert had no acknowledgement when the incident closed, resolved without a person |
| `ayeops_incident_downtime_minutes_total` | `service`, `criticality` | minutes down, growing while an incident is open up to the last read; never lower than before, even across restarts, so a close written late does not look like a counter reset |
| `ayeops_incident_degraded_minutes_total` | `service`, `criticality` | minutes degraded, kept like the downtime minutes |
| `ayeops_incident_affected_module_minutes_total` | `module`, `kind` (`downtime`, `degraded`) | minutes a module of the host spent inside host alerts of that kind, unweighted, kept like the downtime minutes |
| `ayeops_service_criticality_weight` | `service`, `criticality` | the tier weight, for `* on (service) group_left` |
| `ayeops_incidents_step_success` | `step` (`read`, `journal`, with `oncall` `acks`) | 1 when the last read of Grafana, write of the journal or read of the acknowledgements worked |
| `ayeops_incidents_last_round_timestamp_seconds`, `ayeops_incidents_last_sync_timestamp_seconds` | | the last round, and the last one that got through |
| `ayeops_incidents_journal_records`, `ayeops_incidents_transitions_total` | | records in the journal, state changes read |
| `ayeops_incident_lost_requests_total` | `service`, `criticality` | lost requests of closed incidents, for services behind the ingress; never lower than before |
| `ayeops_incidents_request_queries_total` | `result` (`ok`, `error`) | queries for lost requests and how they ended |
| `ayeops_incidents_ack_reads_total` | `result` (`ok`, `error`) | with `oncall`: reads of the acknowledgements from the bridge |

The `AyeOps / Incidents` dashboard shows the weighted downtime in the range and per week by
service (the north star in tier units), raw downtime minutes, degraded minutes in the range
and per week by service, lost requests in the range and per service, the modules affected by
host outages (minutes, unweighted), incidents per service, open incidents, night incidents, the
median, 90th percentile and per-service mean time to restore, with `oncall` next to it the
median and 90th percentile time to acknowledge, its median per criticality by day and night
and the incidents closed without an acknowledgement, the weights, the agent itself,
and, when the [autopilot](#autopilot) is enabled, the hit rate of its hypotheses. Alerts: `incidents-failing` (a step failing for 10 minutes),
`incidents-stale` (no round for 10 minutes, or no metrics), and `target-down`.

Money (`cost: {monthly_revenue}`, `cost: {value_query}`) is the next slice.

## status

The reporter of the [status page](status.md): the stack side of `POST /api/push`. One stdlib
Python script on `python:3.13.16-alpine3.23` (48 MB limit, read-only, no published port) on the
host of `grafana`, which it requires. Once a minute it reads the alert rules and the silences of
that Grafana, turns them into the state of every public component that waits for pushes and posts
them, signed, to `https://<status_page.hostname>/api/push`.

```yaml
hosts:
  central:
    modules:
      status: {}
```

| Setting | Default | Meaning |
|---|---|---|
| `interval` | `1m` | `1m` or `2m` between pushes; both stay inside the Worker's 5 minute silence threshold, `2m` halves the D1 writes of the pushes |
| `timeout` | `10s` | per request to Grafana and to the Worker, `1s` to `20s` |

`aye check` refuses the module without `status_page.hostname`, and when no public component
waits for pushes (every one has a `probe`), since the reporter would have nothing to send (rule
`status-reporter`).

**What it pushes.** A component waits for pushes when at least one of its services has no
`probe`; it is fed from the alerts of all its services, matched by the `service` label every
service rule carries. Grafana evaluates those rules, so `for`, no data and query errors work as
for the notifications:

| Grafana says | State |
|---|---|
| a `critical` (or `page`) alert of a service is firing | `outage` |
| a `warning` alert is firing | `degraded` |
| an alert fires because its query returned no data or failed, or a service has no rule loaded | `no_data` |
| nothing fires, pending alerts and `info` alerts included | `operational` |
| the rules or the silences could not be read | `no_data` for every component |

A component takes the worst state of its services, ranked `outage` > `degraded` > `no_data` >
`operational`. The body is the compact JSON of the [push contract](status.md#push-contract)
with the components in manifest order; service names never leave the host.

**Maintenance.** An active Grafana silence with a matcher on the label `maintenance` and a
positive matcher on `service` puts the services it names into maintenance, for example the
matchers `service = api` and `maintenance =~ .*` (the second matches every alert, since no alert
carries that label, so the silence still silences the service). A component shows `maintenance`
while all its services are in maintenance; otherwise the services outside the silence decide.
A silence without a `maintenance` matcher only mutes notifications and changes nothing on the
page.

**Retries.** A push that fails on the network, with `429` or with a `5xx` is sent again after 2
and then 4 seconds, at most three attempts, and never when the attempt could not finish within
the interval; the same body is resent, which the Worker treats as a replay. Any other answer
(`400`, `401`, `413`, `422`) is not retried: a wrong secret or an unknown component does not heal
by itself. The next round builds a new push either way.

Secrets in `.env`: `AYE_STATUS_PUSH_SECRET`, the tenant secret the Worker verifies, which
[`aye status deploy`](status.md#deploy) sets in D1 from a variable of the same name. The
reporter has no Grafana user or password: it reads Grafana over the compose network with the
`Viewer` token of the
[`grafana-reader`](#grafana-reader), mounted read-only from the `grafana-reader` volume
(`AYE_GRAFANA_READER_TOKEN_FILE=/run/grafana-reader/token`, group `10470`), and it waits for the
reader to be healthy. It reads the file for every request and once more after a `401`, retrying
only when the file holds another token, so a rotation needs no restart; the token is never
logged. A missing or unreadable file is a failed read like any other.

Metrics on `status-reporter:9136` (job `status-reporter`): `ayeops_status_push_success` (the
last push was accepted), `ayeops_status_last_push_timestamp_seconds` (the last accepted push;
the start time until then), `ayeops_status_pushes_total`,
`ayeops_status_push_failures_total{reason}` with `reason` `transient` (all attempts failed) or
`refused`, `ayeops_status_query_success`, `ayeops_status_component{component,state}` (1 for the
state of the last push) and `ayeops_status_last_round_timestamp_seconds`. Alerts:
`status-push-failing` (no accepted push for 5 minutes, which is when the page turns to no data;
it also fires when the metrics are gone), `status-query-failing` (Grafana unreadable for 5
minutes) and `target-down`. `/health` answers 503 once rounds stop. The log has one JSON line per
change of the pushed states, per failed push and per change of the Grafana read, not one per
minute.

## autopilot

Level 1 of the [autopilot](autopilot.md), in shadow mode: for every critical alert it writes
down, within about a minute, what it thinks broke and what would test it, and it does nothing
else. No action runs, nobody is messaged (unless `post_verdict` is set), and every run lands in
a journal, so the hit rate of its hypotheses can be measured against what the person on call
actually did before any action class is ever switched on. One stdlib Python agent
(`autopilot`, `python:3.13.16-alpine3.23`) on the host of `grafana`, without a published port.

```yaml
hosts:
  central:
    modules:
      autopilot:
        critical_services: [web]
        timezone: Europe/Kyiv
```

| Setting | Default | Meaning |
|---|---|---|
| `runner` | `api` | `api`: the Anthropic Messages API over HTTPS; `claude-code`: `claude -p`, see below; `none`: no model, each run's dossier is only collected, masked and kept, see [Without a model](autopilot.md#what-leaves-your-host) |
| `auth` | `api_key` | `api_key` (`AYE_AUTOPILOT_API_KEY`) or `subscription` (`AYE_AUTOPILOT_OAUTH_TOKEN` from `claude setup-token`, `runner: claude-code` only) |
| `claude_binary` | none | host path of the `claude` binary, required with `runner: claude-code` |
| `model` | `claude-sonnet-5-5` | the diagnosis, effort `medium` |
| `escalation_model` | `claude-opus-5-5` | effort `high`, for an alert of a service in `critical_services`, or the same alert again within 24 hours |
| `critical_services` | `[]` | manifest services whose alerts go to the escalation model at once |
| `lookback_minutes` | `60` | how far back the dossier reads metrics and logs before the alert |
| `dossier_tokens` | `4000` | the cap of the dossier, about four characters a token |
| `run_usd`, `run_seconds` | `0.5`, `180` | the dollar and wall-time cap of one run |
| `runs_per_hour` | `4` | runs started in any hour |
| `day_usd` | `3` | spend per day, from midnight in `timezone` |
| `night_usd` | `2` | spend for the whole of `quiet_hours` |
| `quiet_hours` | `23:00` to `08:00` | `start`, `end`; `null` drops the night cap |
| `timezone` | the manifest's [`timezone`](manifest.md#top-level) | for the day and the night |
| `post_verdict` | `false` | also post the verdict, silently, to the chat of the [oncall](#oncall) module on the same host |
| `mask` | `[]` | extra Python regular expressions masked as `[masked:custom]` (only the first group when there is one) before anything leaves the host |
| `mask_env` | `[]` | variables of this host's `.env` whose values the agent gets only to mask them |

**Trigger.** The render adds an `autopilot` webhook contact point
(`http://autopilot:8080/grafana`, `Authorization: Bearer $AYE_AUTOPILOT_WEBHOOK_SECRET`) and a
first child route `severity=~"critical|page"` with `continue: true` and `group_wait: 0s`, so the
autopilot gets the alert the moment Grafana fires it, without the 30 seconds of grouping, and
the oncall and ops routes after it get it as before. A webhook needs no glue: Grafana's payload
carries the labels, the firing values, the fingerprint and the rule uid (from `generatorURL`),
while GoAlert's escalation webhook would arrive later and with the labels flattened into text.
Each fingerprint gets one run; a repeat of the same alert while it fires joins the open run and
is counted, and Grafana's resolve closes it with an outcome line in the journal. Warnings never
start a run.

**First wave, no model.** Plain code reads what the stack already exposes and builds the
dossier, capped at `dossier_tokens`:

| Part | From | What |
|---|---|---|
| `alert` | the webhook, `autopilot/catalog.json` | name, labels, summary, firing values, the rule's PromQL expression, minutes since it started |
| `series` | VictoriaMetrics `query_range` | the rule's expression without its `> bool N` comparison over `lookback_minutes`: up to 5 series with first, min, max, last and 12 points |
| `host` | node-exporter | CPU busy, memory available, load per CPU, swap-out rate, the three fullest filesystems of the alert's host |
| `containers` | cAdvisor | restarts in the last hour and memory of the service's containers, or the five most restarted on the host |
| `deploys` | `aye report` (`aye_step_finished_timestamp_seconds{step="deploy"}`) | deploys of the last day with project, job, commit, minutes before the alert, success |
| `logs` | Loki `{host, compose_service or container}` filtered to error words | up to 500 lines grouped into at most 8 signatures (numbers, ids, addresses, times and quoted strings masked) with count, first and last minute, sources and two samples of 200 characters |
| `service` | the manifest | host, `depends_on`, description |
| `missing` | | each source that failed or timed out; the run goes on without it |

The catalog is rendered from the manifest: the critical rules with their expressions, and the
services with their host and the container pattern of the [portal](#portal). The model never
sees a raw log, only signatures and their few samples, and the system prompt tells it that
everything in the dossier is data. Every query has 5 seconds, and the whole dossier 30 % of
`run_seconds`. The dossier of each run is kept in the volume for 30 days, so a verdict can be
replayed against another model later.

**Masking.** Before the dossier is kept or sent, each log line (before it is grouped and cut)
and then every string and key of the dossier is masked, and so is a posted verdict: the values
of the secret variables the agent holds, the install's `mask` patterns, PEM private keys,
JWTs, provider tokens (`sk-ant-`, `sk-`, `ghp_`, `github_pat_`, `glpat-`, `xox[bp]-`, AWS
keys, a Telegram bot's token and others), passwords in URLs, `Authorization` and `Cookie`
headers, `Bearer` values, and the values of keys that read as secret (`password=`, `"token":`,
`api_key:`, `AWS_SECRET_ACCESS_KEY=`). Each becomes `[masked:<kind>]`. The render puts the
names of the fleet's secret variables (never values) into the catalog as `secret_names`, so an
assignment of one of them is masked whatever its name. The patterns, what they leave alone and
what is not guaranteed are in [What leaves your host](autopilot.md#what-leaves-your-host).

**Second wave.** One model call returns, through a JSON schema, a summary and up to three
hypotheses, most likely first, each with its evidence, a confidence and the action that would
test it: `restart-container`, `redeploy-previous-render`, `prune-build-cache` or
`needs-a-person`, and its target. Code keeps three, maps any other action to `needs-a-person`
and runs none of them.

| Runner | How it is called |
|---|---|
| `api` | `POST https://api.anthropic.com/v1/messages`, `anthropic-version: 2026-10-21`, `output_config.format` with the schema and `effort`, `fallbacks: "default"` (`server-side-fallback-2026-07-01`). `max_tokens` is computed so that the dossier plus the answer cannot cost more than `run_usd` at the model's list price; a run whose dossier alone would not fit fails before the call. The cost is computed from `usage` at the price of the model that answered. |
| `claude-code` | `claude -p --tools "" --strict-mcp-config --permission-mode dontAsk --output-format json --json-schema <schema> --model <model> --max-budget-usd <run_usd> --max-turns 3 --system-prompt <prompt>`, the dossier on stdin, in a new empty working directory with an empty `HOME` that is deleted after the run, so no project settings, hooks or `.mcp.json` load. No built-in tool and no MCP server: the model has nothing to call. The cost is Claude Code's `total_cost_usd`. With `auth: subscription` the process gets `CLAUDE_CODE_OAUTH_TOKEN` and no API key. The image is `python:3.13.16-slim` (Debian trixie, glibc), and the binary is the host's, mounted read-only. |

Neither runner retries. A refusal, a cut-off answer, a timeout or an HTTP error is a failed run.

**Spend limits.** Enforced in the agent's code before a run starts: a run that would cross a cap
does not start and is journaled as `skipped` with `reason: cap:<cap>`. A starting run reserves
`run_usd` against the caps and settles at its real cost when it ends, so parallel runs cannot
cross a cap together. A failed run counts at the cost it reported (a refusal, Claude Code's
`error_max_budget_usd`), at nothing when the provider refused the request with an HTTP error,
and at the full `run_usd` when it timed out, because what it spent is unknown. Spend is rebuilt
from the journal at start, so a restart resets nothing.

| Cap | Default |
|---|---|
| one run | `run_usd` 0.50 USD, `run_seconds` 180 s, no tool calls (the model gets no tools) |
| alert storm | one run per fingerprint, `runs_per_hour` 4 |
| quiet hours | `night_usd` 2 USD for the whole night |
| day | `day_usd` 3 USD |
| month | the provider budget and alerts of [ai-usage](#ai-usage) |

**The journal.** `/tmp/journal.jsonl` in the `autopilot-state` volume, one JSON line per run:

| Field | Meaning |
|---|---|
| `run`, `fingerprint`, `alertname`, `severity`, `labels`, `rule_uid` | which alert |
| `mode`, `status`, `reason` | `shadow`; `ok`, `failed` or `skipped`; the stage and error, or the cap |
| `alert_at`, `received_at`, `dossier_at`, `verdict_at` | the timeline, UTC with milliseconds |
| `first_answer_seconds` | from the start of the alert to the verdict |
| `runner`, `model`, `effort`, `turns` | how it was answered |
| `input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_write_tokens`, `cost_usd` | what it cost |
| `dossier_tokens`, `dossier_missing` | size of the dossier and the sources it lacked |
| `summary`, `hypotheses` | the verdict: `rank`, `cause`, `cause_class`, `confidence`, `evidence`, `action`, `target` |
| `posted` | whether the verdict went to the on-call chat |
| `masked` | how many values were masked in the run, per kind (`env`, `jwt`, `token`, ...); never a value or a hash |

`cause_class` makes a hypothesis comparable by code: `container-down` (crashed, hangs, leaks
memory or did not start), `bad-change` (a deploy or config change), `disk-full`, `host-down`,
`dependency-down`, `cert-or-dns` or `other`. The schema asks the model for it; an unknown class,
and every hypothesis of a journal written before the field existed, takes the class of its
action (`restart-container` is `container-down`, `redeploy-previous-render` is `bad-change`,
`prune-build-cache` is `disk-full`, `needs-a-person` is `other`).

After a run come outcome lines (`"kind": "outcome"`): one when the alert resolves
(`resolved_at`, `minutes_to_resolve`, `repeats`; with `"from": "incidents"` when the
[incident journal](#incidents) closed it, see below), and one per verdict a person gives
(`recorded_at`, `matched_rank`, `cause_class`, `target`, `note`). Then one label line
(`"kind": "label"`) per run with hypotheses: `source`, `fix` (`cause_class`, `targets`, `exact`,
`evidence`), `matched_rank`, or `source: none` with `reason`, and `acknowledged` (`at`, `by`,
`via`, `seconds`) when a person acknowledged the incident in GoAlert.

**Hit rate.** Shadow mode exists to measure it: after an incident, which hypothesis matched what
actually fixed it, and at which rank. Every minute the agent looks at the runs that have
hypotheses and no label yet. A run is labelled 5 minutes after its alert resolved, so the
reconciler's metrics and logs of the last minutes are in; the resolve is Grafana's webhook, or,
when that never came (the agent was down or restarted), the end of the run's incident in the
journal of the [incidents](#incidents) module: the same alert title, the same `host` and
`service` labels, started within 2 minutes of the run's alert. What fixed it, first match wins:

| Source | Signal | The fix |
|---|---|---|
| a person | a verdict, see below | as given; it wins over any label, before or after it |
| `reconciler` | `increase(ayeops_reconciler_successes_total{host}[...])` from VictoriaMetrics over the alert, from 5 minutes before it to its resolve, and with [logs](#logs) the reconciler's own `attempt` lines with `success: true` from Loki | `container-down` on the containers it started (`exact`), or on its compose projects when Loki has no line |
| `incidents` | a host alert in the incident journal (a record with `modules`, such as `host-down`) on the run's host that ended between 5 minutes before the alert and 5 minutes after its resolve; the run's own incident does not count | `host-down` on the host |
| `none` | nothing of the above (`reason: no-signal`, or `reason: acknowledged` when a person acknowledged the incident in GoAlert), or no resolve within two days (`reason: unresolved`) | none; the run is unexplained, not a miss |

A hypothesis matches when its `cause_class` is the class of the fix and, for an `exact` fix,
its `target` names one of the fix's targets on whole parts of the name: `web` matches
`acme-apps-web-1`, `we` and `web-2` do not. `matched_rank` is the rank of the first one that
matches, `0` when none does. The labeller only reads: VictoriaMetrics, Loki and the incident
journal, which the render mounts read-only (`incidents-journal:/incidents:ro`) when incidents
runs on the same host; it writes its own journal and messages nobody.

A person gives a verdict through the agent's authenticated `/outcome` endpoint, from the host:

```sh
docker compose exec autopilot python -I /app/autopilot.py runs [how many]
docker compose exec autopilot python -I /app/autopilot.py outcome <run> <rank> [--cause <class>] [--target <name>] [what was done]
```

`runs` prints the last runs (20 by default), one JSON line each, with their hypotheses and
label. `<rank>` is the hypothesis that matched what fixed it, `0` when none did; `--cause` is
the class of what really fixed it, needed only for `0`, since a rank takes the class of its
hypothesis. A posted verdict ends with its run id.

The hit rate comes from the journal and survives restarts:
`ayeops_autopilot_hit_rate{cause,rank}` is the share of labelled runs whose matching hypothesis
had at most that rank, per class of the fix and for `cause="all"`;
`ayeops_autopilot_labelled_runs{alertname,cause,matched,source}` counts them (`matched` `1` to
`3` or `none`), `ayeops_autopilot_unexplained_runs{alertname,reason}` the runs no signal
explains, and `ayeops_autopilot_label_pending` the runs still waiting. The `AyeOps / Incidents` dashboard shows them in its `Autopilot hypotheses` row
when the autopilot is enabled. An action class is switched on only when its measured hit rate
earns it.

**Posting the verdict.** With `post_verdict: true` the agent sends one silent message
(`disable_notification`) with the summary and the hypotheses to `person.telegram_chat_id` of
the oncall module through its bot (`AYE_ONCALL_TELEGRAM_TOKEN`; sending does not conflict with
the bridge's polling). `aye check` refuses it without oncall on the same host (rule
`autopilot-verdict`).

Secrets in `.env`: `AYE_AUTOPILOT_WEBHOOK_SECRET` (Grafana gets it too), and
`AYE_AUTOPILOT_API_KEY` or, with `auth: subscription`, `AYE_AUTOPILOT_OAUTH_TOKEN`; with
`runner: none` neither. They are read from the environment only, sent only in the provider's
own header and never written to the journal, the logs or the metrics, and their values are
masked in every dossier. `aye check` refuses `alerting.contact: none`
(`autopilot-contact`), `runner: claude-code` without `claude_binary` and `auth: subscription`
without it (`autopilot-runner`), a `day_usd` or `night_usd` below `run_usd`
(`autopilot-caps`), and a `mask` pattern that does not compile or matches the empty string or
a `mask_env` entry that is one of the agent's own variables (`autopilot-mask`).

Metrics (job `autopilot`): `ayeops_autopilot_runs_total{status}`,
`ayeops_autopilot_skipped_total{cap}`, `ayeops_autopilot_cap_reached{cap}`,
`ayeops_autopilot_answer_seconds_sum`/`_count` and `ayeops_autopilot_last_answer_seconds` (time
to first answer), `ayeops_autopilot_dossier_seconds_sum`/`_count`,
`ayeops_autopilot_cost_usd_total{model}`, `ayeops_autopilot_tokens_total{model,kind}`,
`ayeops_autopilot_spend_day_usd`, `ayeops_autopilot_spend_night_usd`,
`ayeops_autopilot_runs_last_hour`, `ayeops_autopilot_outcomes_total{matched}`,
`ayeops_autopilot_hit_rate{cause,rank}`,
`ayeops_autopilot_labelled_runs{alertname,cause,matched,source}`,
`ayeops_autopilot_unexplained_runs{alertname,reason}`,
`ayeops_autopilot_label_pending`, `ayeops_autopilot_open_runs`, `ayeops_autopilot_joined_total`,
`ayeops_autopilot_masked_total{kind}`,
`ayeops_autopilot_failures_total{stage}` (`dossier`, `model`, `post`, `journal`, `webhook`,
`label`),
`ayeops_autopilot_webhooks_total`, `ayeops_autopilot_mode{mode="shadow"}`. Alerts:
`target-down` (the agent is down), `autopilot-failing` (a run failed in the last hour),
`autopilot-cap-reached` (a cap stops the runs until it resets).

## grafana

Grafana `13.2.3`. Requires `metrics` with `role: server` on the same host, and runs on at most
one host.

| Setting | Default | Meaning |
|---|---|---|
| `root_url` | none | `GF_SERVER_ROOT_URL`; with `auth` on the host it defaults to the route that targets `grafana:3000` |
| `dashboards` | none | a directory, relative to the manifest, whose `*.json` dashboards go into a folder named after `project`, next to the `AyeOps` folder |
| `datasources` | `[]` | extra datasources for those dashboards, see below |
| `locale` | `portal.locale` | language of the titles and legends of the AyeOps dashboards, `en` or `uk` |

Provisioned: the VictoriaMetrics datasource (uid `victoriametrics`), the dashboards and alert
rules of every module enabled on any host plus the alerts of your services, and with
`alerting.contact: telegram` a Telegram contact point and the default notification policy.
With `alerting.contact: email` the contact point is an `email` one that sends to
`alerting.emails`; with `alerting.contact: slack` a `slack` one that posts to the incoming
webhook in `AYE_SLACK_WEBHOOK_URL` ([Slack](manifest.md#slack)). All three carry a link to the dashboard panel of the alert, see
[alerts](#modules); the links point at `root_url`, so without one they lead to Grafana's
`localhost`. With [`mail`](manifest.md#mail) Grafana sends through that relay
(`GF_SMTP_*`; `tls: starttls` becomes `GF_SMTP_STARTTLS_POLICY=MandatoryStartTLS`, `tls` and
`none` become `NoStartTLS`, and Grafana itself speaks TLS from the start on port 465).
Grafana expands `$VAR` in provisioning files, so the renderer writes `$` in rule expressions and
summaries as `$$`.

**Notification grouping.** A fleet of a hundred hosts must not send a hundred messages when a
rack loses power. Every rule carries a templated label `scope`: the host's `group` when its
series has one, else its `host`, else none
(`{{ if $labels.group }}{{ $labels.group }}{{ else if $labels.host }}{{ $labels.host }}{{ end }}`;
Grafana `13.2.3` expands label templates per alert instance and drops a label whose value comes
out empty). The notification policy groups by `grafana_folder`, `alertname` and `scope`, not by
`host`: 14 of 20 workers going down is one notification of the group `workers`, a host without
a group still gets its own, and an alert without a host groups by its name alone. Grafana's
defaults hold: it waits 30 seconds after the first alert of a group, sends what joined later
after 5 minutes, and repeats an unchanged group every 4 hours. `scope` is a label of its own
because a rule label wins over a query label of the same name in Grafana, so a rule label
`group` would overwrite the host's group. A notification with more than one alert reads, in
Telegram:

```text
🔴 14 x Host down · workers · runbook · dashboard
worker-01, worker-02, worker-03, ...
```

with the hosts of the firing alerts (their `instance` for an alert without a host), or each
alert's summary when they all are on one host (two full disks of one host name their mount
points), and a `✅ 3 x Host down` part for the alerts that resolved since. A single alert reads
as before. Grafana cuts a Telegram message at 4096 characters, about 300 host names. A Slack
message has the same lines with Slack links (`<url|runbook>`) instead of HTML and `&`, `<` and
`>` escaped in alert names, scopes and summaries; its title is the mark, the alert name and, for
a group, the scope (`🔴 Host down · workers`), and links to the alert rule. Slack collapses a text
of more than 700 characters or 5 lines behind "Show more". The email
contact point uses Grafana's default template, which already lists every alert of the group;
[oncall](#oncall) folds the group into one GoAlert alert.

**A down host holds back its other alerts.** With [node](#node) on some host, every rule but
`host-down` itself, of a module or of a service, is rendered as

```text
(<expr>) unless on (host) (max by (host) (<node up>) == 0) or 0 * (<expr>)
```

where `<node up>` is the freshness `host-down` judges by
(`last_over_time(up{job="node"}[1d]) * (lag(up{job="node"}[1d]) < bool 180)`). While a host has
node series but none fresh and up, its series of the rule keep their labels with the value 0, so
they stay normal instead of paging next to `host-down`: an overloaded host whose scrapes time
out does not add a `target-down` per job, and a dying host does not add the alerts of its last
samples. The `or` branch keeps every series, so a rule whose silence fires (`no_data:
alerting`) does not fire when all its series are held back. An alert already firing on the host
resolves when the host goes down and fires again after it is back if its cause remains. A host
without node, or an alert without a `host` label, is never held back; probes of the host's
services from another host carry the prober's `host` and still page, since users see that
outage.

**Volatile hosts and maintenance windows.** A host's
[`volatile` and `maintenance`](manifest.md#volatile-hosts-and-maintenance-windows) fields
add more guards of the same form, each an `unless` before the `or` branch, so the series stay
at 0 instead of vanishing:

| Rule | Guard |
|---|---|
| `host-down` | `unless on (host) (label_replace(vector(1), "host", "<volatile host>", "", "") or ...)`: the volatile hosts are left out |
| `host-gone` | none from the hold: it judges the volatile hosts itself, by `1 - ((max by (host) (max_over_time(<up>[<volatile>])) + on (host) group_right () (0 * last_over_time(<up>[<volatile> + 7d]))) or 0 * last_over_time(<up>[<volatile> + 7d]))` with `<up>` `up{job="node", host=~"<hosts>"}`, one term per duration |
| every rule, both of these included | `unless on (host) (label_replace(<windows>, "host", "<host>", "", "") or ...)` for every host with a window |
| a service rule of a service whose `host` is volatile | also `unless on () (max by (host) (<node up of that host over <volatile> + 7d>) == 0)` |
| a service rule of a service whose `host` has windows | also `unless on () (<windows of that host>)` |

A window term is `vector(time() - <offset>) % <period> < <duration>`, in seconds: the period is
a day for a window without `days`, else a week, and the offset is the window's start counted
from the epoch (1970-01-01, a Thursday, 00:00 UTC), one term per day. The remainder is the
time since the window last opened, so a window that crosses midnight or the end of the week
needs no second term, and a host's terms join with `or`. `<windows>` exists only while a window
is open; the query depends on nothing but the clock, so it holds through a restart of Grafana
and a window that opened before the rule was provisioned.

The `on ()` guards make the declared host, not the label of the series, decide for a service:
an HTTP check from the central host of a service on a volatile workstation is held while the
workstation is down or in its window, which the host label alone (the prober's) cannot do. A
host without `node` has no freshness to judge, so `volatile` needs `node` on the host; a window
needs nothing.

Three limits. A rule with `no_data: alerting` whose only series come from a volatile host still
fires as no data once those series have been gone for the lookback (5 minutes): a module whose
silence pages (backups, offsite, objectstore, oncall, status) belongs on a host that stays on.
The guards hold rules, not notifications: an alert that fired before a window opened resolves
when it opens, and fires again after it closes if its cause remains. And the
[status page](status.md) shows the components of a host in its window as operational, since
nothing fires; announce a public maintenance through the status admin API, or a Grafana
silence with a `maintenance` matcher, as before.

Grafana's mute timings were the other way to do windows: a notification policy per host, with
its `mute_time_intervals` and a copy of the autopilot and oncall routes under it. They mute
notifications only, so the alert still fires: the incident journal records it and the status
reporter turns its component into an outage; and a window crossing midnight is two time
intervals. Generated silences would need an
API call on every deploy and go stale when the manifest changes. The guard lives in the rule,
where the hold already is, and every reader of alert state sees the same thing.

The rule query is the least magic of what Grafana `13.2.3` offers. Grafana 13 has inhibition
rules, but only through its App Platform API (`notifications.alerting.grafana.app/v1beta1`,
no UI, no file provisioning, documented for imported Alertmanager configurations), so they
would need an API call on every deploy beside the files of the render. A label that marks held
alerts and a route to a muted policy still needs the same join in every query and changes the
alert's identity when the host goes down. The join is in the rendered `rules.yaml`, visible in
Grafana's rule view, and needs nothing at run time.

Grafana's own dashboards cover the fleet as a whole: `AyeOps / Health` (state, uptime, load,
CPU, memory and the fullest filesystem of every host; alerts firing, rules without data, scrape
targets down and the vmagent queue waiting to be shipped; with `containers` the busiest and
largest containers and their starts; with `reconciler` the containers the manifest does not
declare), `AyeOps / Services` (every scrape target by host and job:
up, a timeline, scrape duration and series, and with `containers` per-container CPU, memory,
traffic and starts) and `AyeOps / Reliability` (30-day availability and minutes down of the
probed URLs and of each host's internet, alerts over time, the age of the last off-site copy and
restore test with `offsite`, and of the secrets dump with `secrets`).

The dashboards that pick hosts (`AyeOps / Hosts`, `AyeOps / Services`, `AyeOps / Internet`)
first offer a `Group` picker from the `group` label of the hosts, `All` by default; it narrows
the host picker to the hosts of that group, and `All` keeps the hosts without a group. With
`Group` set and `Host` on `All`, `AyeOps / Services` shows that group's hosts only.

A module dashboard may mark a panel, a row or the whole dashboard with
`"ayeopsRequires": ["module"]` or `["module.setting"]`: the renderer keeps it only when that
module runs, or that setting is set, on at least one host, and drops a row left without panels.
Any string in a module dashboard may be a map by language, `{"en": "...", "uk": "..."}`, and is
rendered in `locale`. Your own dashboards under `dashboards` are copied as they are.

Each entry of `datasources` takes `name` and `uid` (both unique, and not the built-in
`VictoriaMetrics` / `victoriametrics`), `type` (`postgres`, `loki` or `prometheus`) and `url`:
`host:port` for `postgres`, `http(s)://...` for the other two. `postgres` also needs `database`
and takes `tls: true` for `sslmode=require` (default `disable`, for a private network). `user`
plus `password_env` sign in: as the database role for `postgres`, with basic auth for the others.
The password never lands in a rendered file: compose requires the variable named by
`password_env` (`${VAR:?}`), passes it into the Grafana container, and the datasource file reads
it as `${VAR}`. Grafana expands that form once, so a `$` inside the password survives; the
`$__env{VAR}` form would be expanded a second time.

```yaml
grafana:
  dashboards: dashboards
  datasources:
    - name: Shop
      uid: shop
      type: postgres
      url: 10.0.0.5:5432
      database: shop
      user: grafana_ro
      password_env: AYE_GRAFANA_PG_PASSWORD
```

A `postgres` datasource opens at most `max_connections` sessions (default `2`, up to `20`) and keeps
none idle, closing each after 60 seconds. Grafana's own default keeps every session it ever opened, so
a read-only role with a connection limit (`ALTER ROLE ... CONNECTION LIMIT 5`) ends up held entirely
by an open dashboard and every other client of that role, such as the `sql` [checks](checks.md), gets
`too many connections for role`. Keep `max_connections` plus the checks below the role's limit.

`aye check` reads every dashboard: it must be JSON with a `uid` that no other dashboard uses, and
every `datasource` it names must be declared here, be `victoriametrics`, a Grafana built-in or a
`${variable}`. A dashboard exported from another Grafana keeps that Grafana's datasource uids, so
either declare the datasource with the same uid or rewrite the uids in the JSON.

With [`auth`](#auth) on the same host Grafana signs people in itself with the same OAuth client
(`GF_AUTH_GOOGLE_*` for `google`, `GF_AUTH_GENERIC_OAUTH_*` for `oidc`): sign-up is allowed, the
`admins` get `GrafanaAdmin`, the other `emails` get `Viewer`, and `role_attribute_strict` refuses
every other account. The admin password form stays as break-glass.

### grafana-reader

Agents that read Grafana (the `status` reporter, the `incidents` journal, the autopilot, the
[MCP hub](#mcp)'s `mcp-grafana`) read it with a token of a service account with role `Viewer`, not with the admin password: a leaked
admin password is the whole Grafana, its contact points and data sources; a leaked reader
token reads what any `Viewer` reads.

The reader is rendered only when it has a consumer: an enabled module on the Grafana host whose
catalog declares a service with `grafana_reader: true` (`status` and `incidents` among the
built-in ones). Without one there is no `grafana-reader` service, volume, script, scrape job or
`grafana-reader-failing` alert, so an install that reads Grafana with nothing does not pay its
32 MB; a consumer on another host does not count, it fails `grafana-reader-mount` instead.

`grafana-reader` (`python:3.13.16-alpine3.23`, stdlib only) starts once Grafana is healthy and
then, every 5 minutes:

1. finds the service account `aye-reader` with the admin password, creates it with role
   `Viewer` if it is missing, and sets it back to `Viewer` and enabled if someone changed it;
2. reads `/tmp/token` in the `grafana-reader` volume and calls every consumer endpoint with it;
3. only when the file is missing or Grafana answers `401`, creates a new token, writes it
   (temporary file, `fsync`, rename) and then deletes every other token of the account, so a
   token that leaked before stops working;
4. compares what the token sees with what the admin sees, and serves the result on 9137
   (not published, scraped as `grafana-reader`).

The token is never logged or printed; logs name the endpoint and the HTTP status. Tokens do not
expire: a time limit would break the consumers between the expiry and the next pass. To rotate
by hand, delete the file, see [grafana-reader-failing](runbooks/grafana-reader-failing.md).

The file is `token`, mode `0440`, owner and group `10470`: the reader runs as `10470:10470`, so
the files it creates are its own, and nothing else in the stack runs as that uid or in that
group. A consumer runs as its own user (`65534`) with `group_add: ["10470"]` and reads the file
through the group bit; a container that mounts the volume without the group cannot read it, and
a container that does not mount it does not see it. The reader mounts the volume on `/tmp`, so
on first use the volume takes the mode of the image's `/tmp` (`1777`) and the reader can write
it without root. The volume is local, so consumers run on the Grafana host.

What `Viewer` can do, from the Grafana `13.2.3` source (`pkg/services/ngalert/accesscontrol/roles.go`,
`pkg/services/ngalert/api/authorization.go`, `pkg/api/accesscontrol.go`, folder defaults in
`pkg/registry/apis/folders/register.go`):

| Endpoint | Consumer | Needs | Where `Viewer` gets it |
|---|---|---|---|
| `GET /api/prometheus/grafana/api/v1/rules` | status, autopilot | `alert.rules:read` and `folders:read` on each rule's folder; rules in other folders are left out | `fixed:alerting.rules:reader`, and the `View` that every root folder grants `Viewer` when it is created, the provisioned AyeOps folders included |
| `GET /api/alertmanager/grafana/api/v2/silences` | status | `alert.instances:read`, or `alert.silences:read` on all folders | `fixed:alerting.instances:reader` |
| `GET /api/alertmanager/grafana/api/v2/alerts` | autopilot | `alert.instances:read` | `fixed:alerting.instances:reader` |
| `GET /api/annotations?type=alert` | incidents | `annotations:read`: organization annotations, and those of dashboards the account can read | `fixed:annotations:reader` (organization), and `annotations:read` in the `View` of a folder's dashboards |

Rules and annotations are filtered rather than refused, so the reader compares counts with the
admin's: if a folder stops granting `Viewer` view, `endpoint_ok` goes to `0` instead of the
agents silently seeing fewer rules. Alert state history comes from annotations because Grafana
writes it there by default (`[unified_alerting.state_history] backend`).

`Viewer` is the narrowest role that works in Grafana OSS. A service account with no basic role
plus fixed roles for exactly these endpoints would be narrower, but OSS has no API to assign a
fixed or custom role (`/api/access-control` serves only the caller's own permissions; role
assignment is Enterprise and Cloud). So the token can also do what every `Viewer` can: open
dashboards, list contact points without their secrets, and **query every data source**
(VictoriaMetrics, Loki and any `datasources` entry, a PostgreSQL one included), since data
source permissions are Enterprise too. It cannot create, change or delete anything, read
contact point secrets, manage users or service accounts, or use the provisioning API.

A module consumes the reader in two places. In `module.yaml` the catalog entry of the service
says so:

```yaml
services:
  status-reporter:
    grafana_reader: true
```

In `compose.yaml.j2` the service mounts the volume read-only, joins the group, waits for the
reader and gets the path in `AYE_GRAFANA_READER_TOKEN_FILE`; `grafana_reader` is in the
template's scope with these values:

```yaml
  status-reporter:
    user: "65534:65534"
    group_add: [{{ grafana_reader.group | tojson }}]
    environment:
      {{ grafana_reader.env }}: {{ grafana_reader.token_file | tojson }}
    volumes:
      - {{ grafana_reader.volume | tojson }}
    depends_on:
      {{ grafana_reader.service }}:
        condition: service_healthy
```

which renders as `grafana-reader:/run/grafana-reader:ro`,
`AYE_GRAFANA_READER_TOKEN_FILE=/run/grafana-reader/token` and group `10470`. The agent sends
`Authorization: Bearer <the file's content, stripped>` and reads the file again for every
round (or after a `401`), so a rotation needs no restart; it needs no Grafana user or password
in its environment. `aye check` holds both sides together with the rule
[`grafana-reader-mount`](rules.md#host-and-module-rules): a service with `grafana_reader: true`
runs on the Grafana host, mounts exactly that entry read-only, sets the variable to that path,
adds the group and waits for `grafana-reader` to be healthy; a service that mounts the volume
without declaring it is an error too.

## ingress

Traefik `v3.7.14` serving human HTTPS names for the internal services of a host over a private
network, e.g. `https://grafana.ops.acme.example` instead of `http://10.8.0.1:3000`.

| Setting | Default | Meaning |
|---|---|---|
| `domain` | required | names are `<route>.<domain>`; a lowercase hostname |
| `provider` | `cloudflare` | DNS provider for the certificate challenge and the records; the only one so far |
| `routes` | required | map of name to `<service>:<port>`, a service a module renders on this host |
| `email` | none | ACME account email; Let's Encrypt works without one |
| `expose` | `[443, 80]` | `443` serves the routes, `80` only redirects to HTTPS; both bind to `public_bind` |
| `without_auth` | none | the reason this host publishes its routes without [`auth`](#auth), or `{reason, until}` with the last day it holds; silences rule `ingress-auth` until then, see [waivers end](rules.md#waivers-end) |

Traefik reads its static configuration from `ingress/traefik.yml` and the routes from the file
provider in `ingress/dynamic/`; it never sees the docker socket. One wildcard certificate for
`*.<domain>` comes from Let's Encrypt through a DNS-01 challenge with the Cloudflare provider, so
the host needs no inbound access from the internet; it is stored in the `ingress-acme` volume.
The token comes from `AYE_CLOUDFLARE_DNS_API_TOKEN`. Traefik metrics are scraped from
`traefik:8082`, which also answers the healthcheck ping.

Routes reach services by their compose name, so they can target services rendered by modules on
the same host only; services from your own compose files live on another compose network. Probe
one route from the same host in `probes.http` (rule `ingress-probe`): `probe-failing` then
covers the whole path and `tls-expiring` the certificate renewal.

`aye dns <manifest>` plans an A record `<route>.<domain>` pointing at the host `address` for every
route on every host: DNS only, never proxied, TTL 300, with the record comment
`managed-by: ayeops`. The zone is the longest suffix of the domain that Cloudflare knows. The
plan creates missing records, updates owned records whose address, TTL or proxy flag drifted,
and deletes owned records below the domain that no route wants. Records without the comment are
never changed; one on a wanted name is a conflict, and `--apply` refuses to write anything until
it is gone. Without `--apply` the command only reads. Records of a domain removed from the
manifest are not cleaned up.

A route that targets `victoriametrics` redirects `/` to `/vmui/`, so the bare name lands in the UI.

## auth

oauth2-proxy `v7.15.5` (the `-alpine` image, which has `wget` for the healthcheck) in front of
the ingress routes. Requires `ingress` on the same host.

| Setting | Default | Meaning |
|---|---|---|
| `provider` | `google` | `google` or `oidc`; any OpenID Connect provider works through `oidc` |
| `issuer_url` | none | the OIDC issuer, required with `provider: oidc` |
| `endpoints` | none | `{authorize, token, userinfo}` of the OIDC provider for Grafana's generic OAuth, which has no discovery; required with `oidc` when Grafana runs on the host (rule `auth-endpoints`) |
| `emails` | required | lowercase addresses allowed in; rendered to `auth/emails.txt` |
| `admins` | all `emails` | the addresses that become `GrafanaAdmin` in Grafana (rule `auth-admins`: a subset of `emails`) |
| `open` | `[]` | route names left without the gate, e.g. a UI with a login of its own (rule `auth-open`) |

```yaml
modules:
  ingress:
    domain: ops.acme.example
    routes: {grafana: grafana:3000, metrics: victoriametrics:8428, portal: portal:8080}
  auth:
    emails: [ops@acme.example, oncall@acme.example]
    admins: [ops@acme.example]
```

The module adds the route `auth` to `oauth2-proxy:4180` (so `aye dns` plans `auth.<domain>` and
the portal lists it) and a Traefik `forwardAuth` middleware `sso` on every route except the
callback, routes to Grafana and the names in `open`; `trustForwardHeader` stays off, so the
`X-Forwarded-*` headers oauth2-proxy reads come from Traefik. Grafana's `/metrics` answers
without a login, so a second router on that path of a Grafana route asks for the sign-in too.
oauth2-proxy answers forwardAuth on `/` with a static `202` when the session is valid and with the
redirect to the provider when it is not; an address outside `emails` gets `403`.

The session lives only in a cookie `_aye_sso` on `.<domain>`, secure, `SameSite=Lax`, valid for
24 hours, with one CSRF cookie per sign-in so tabs opened together do not overwrite each other.
The callback is `https://auth.<domain>/oauth2/callback`; register it on the OAuth client together
with Grafana's `<root_url>/login/google` (`/login/generic_oauth` with `oidc`) and, when
[errors](#errors) runs on the same host with `provider: google`, GlitchTip's
`<url>/accounts/google/login/callback/`. The client comes
from `AYE_AUTH_CLIENT_ID` and `AYE_AUTH_CLIENT_SECRET`, the cookie key from
`AYE_AUTH_COOKIE_SECRET` (`openssl rand -base64 32 | tr -- '+/' '-_'`). Metrics are scraped from
`oauth2-proxy:9180`; `target-down` fires when it stops answering, and then every gated UI fails
closed, see the [runbook](runbooks/oauth2-proxy.md).

The gate covers the names below the ingress domain only. A port a module publishes on
`public_bind` directly, such as VictoriaMetrics `8428` for remote write from agents, is not behind it.

## portal

Two static pages generated from the manifest, served by `nginxinc/nginx-unprivileged`
`1.31.5-alpine3.24` (32 MiB, port `8080` on `127.0.0.1`, one worker per CPU of its limit). Route
it through ingress on the same host, `routes: {portal: portal:8080}` (rule `portal-route`), and
it answers at `https://portal.<domain>`. `aye render` writes the pages to `<host>/portal/` and
the nginx config to `<host>/portal-nginx/default.conf`; `aye portal <manifest> -o <dir>` writes
the pages anywhere, also for a manifest that does not pass every rule yet. No settings besides
`expose` and `resources`.

**Brand.** The header shows the project's logo and name, and the tab carries a favicon that
is clearly the ops portal, not the product site, both from the top-level
[`brand`](manifest.md#brand); without it the header keeps a dot and the name of the project, and
the favicon is its first letter on the same «ops» tile.

**Live data.** The pages stay static and deterministic: `aye render` bakes in which series to
ask for (a JSON blob generated from the manifest), never a value. The browser asks every 30 s,
pausing while the tab is hidden, through a read-only proxy in the portal's nginx: only `GET` on
`/api/v1/query` and `/api/v1/query_range` reaches `http://victoriametrics:8428` (resolved
through the Docker DNS at request time, cookies and `Authorization` stripped); everything else
under `/api/` answers 403. So the portal needs a metrics server on its host (rule
`portal-metrics`). It shows:

- per service a status dot, from the `up` of its scrape target, `probe_success` of the probed
  URLs that lead to it (its ingress routes, `url` and `status.probe`) and the age of
  `container_last_seen` with the `containers` module (gone for more than 5 minutes is down):
  *live* when every signal is fine, *down* when one fails, *no data* when there is none; probe
  latency (`probe_duration_seconds`) with a sparkline of the last hour, container memory against
  its `mem_limit` and CPU against its `cpus`: the share of the limit is the coloured value, the cores in
  use stand beside the limit (`88 % · 0.088 of 0.1 cpu`), and a service without a limit shows the
  share of one core;
- per service up to three Grafana dashboards that draw its metrics (the `metrics` of the
  service and those its alerts query), its module's own first, and beside each alert the
  dashboard that alert links to; a dashboard with a `host` variable opens on the card's host.
  The links use Grafana's `root_url` and are left out when there is none;
- per host with the `node` module: CPU with a sparkline of the last hour, memory used of total,
  disk used on `/` with «full in ~N d» when it runs out within a year, swap when there is any,
  battery when enabled, uptime. The forecast is the Disks row's for `/`: the same query over the
  window of the `disk-full-forecast` alert (1 day today), so one disk has one forecast on a card
  and it is the pace that pages, coloured like that row (yellow under 14 days, red under 3);
  without that window in the catalog the tile shows no forecast;
- per host with the `node` module, a **Disks** block: every filesystem node-exporter reports
  (the same `fstype` filter as the `disk-full` alert, one row per device) with a usage bar that
  turns yellow at the `disk-full` threshold and red at the `disk-critical` one (both read from
  the module catalog, 80 % and 90 % today), used of total and a 30-day sparkline (one point per
  6 hours); with `disk-forecast` on the host, «full in ~N d» from the same `predict_linear`
  window as its alert (the tile above shows the same number for `/`), yellow under 14 days and
  red under 3; with `smart`, one row per drive: SMART health, reallocated and pending sectors
  (yellow at the first one, red when they grew within the `smart-sectors-growing` window), NVMe
  wear and temperature (yellow past the `nvme-wear` and `disk-temperature-high` thresholds).
  Without data the block says so;
- a strip under the summary: down first, then live, no data and the red (coverage) count.

Containers are matched by compose naming, `<project>-<service>-<n>` (`<project>-<host>` for
module services, the compose file's `name:` or folder for your own), or `container_name`. When
the proxy does not answer (a file opened locally, `aye portal` output elsewhere), the strip
says so and the static view stays as it is. Status colours (live green, down red, no data
grey) are separate tokens from the category palette; coverage gaps keep their red outline.

**Languages.** Every page carries English and Ukrainian: one builder with one dictionary of
strings, so a section cannot exist in one language only. The page opens in `portal.locale`
(`en` by default); the EN · UA switcher in the header swaps the language in place and remembers
the choice in `localStorage` when the browser allows it. Text written in the manifest
(`description`, status titles, flow `label`) may be a map by language, see
[text in several languages](manifest.md#text-in-several-languages). Rule findings shown in red
stay in English, as `aye check` prints them.

**Icons.** Host cards, chips, diagram boxes and service cards carry the product logo of each
service in its brand colour on a round badge (see [icons](manifest.md#icons)), inlined once per
page as an SVG sprite, so the page makes no external request; a dark brand colour is lightened
in the dark theme. A service without a known product gets a letter monogram of its name.


**Where things run** (`index.html`): zones, hosts and the services each module or compose stack
runs on them, then diagrams laid out automatically. Every diagram has one band per host (and
one for what is outside the fleet), boxes with the logo, the name and the first words of the
description, and curved edges with a label; inside a band the order follows the barycenter of
the neighbours, and an edge that skips a column runs through its own slot instead of a box.

- *Monitoring and access*: fixed columns by role, left to right sources, collectors, ingress,
  dashboards and storage, derived from the edges: vmagent scrapes (a scrape that would point
  backwards becomes a corner dot on the scraped box), remote_write to the central storage,
  Grafana queries, ingress routes to their services, and blackbox probes to one «Probed URLs»
  box per prober that lists the URLs instead of one edge per URL;
- *Dependencies*: `depends_on` of every service;
- *Data flows*: the `flows` section and the `clients` of every service, drawn as incoming
  edges, so an API or MCP endpoint shows who calls it from outside.

Dependencies and data flows take their columns from the longest path from the sources, cycles
drawn as one edge going back.


Dots move along an edge only when both ends are live; planned edges are dashed. Hovering a box
dims everything not connected to it, and a click opens its card. Diagrams off screen pause
(`IntersectionObserver`), and with `prefers-reduced-motion` there are no dots. A diagram fits
its box and never grows past its real size; «100 %» keeps the box where it is and shows the real
size inside it, centred, to scroll or drag around, «Fit» goes back, «Full screen» fits the
diagram into the window, Esc leaves. On a phone the diagram is drawn at a readable size inside
its box and pans there, the page itself does not scroll sideways. Light and dark follow the
system; the Theme button overrides it per browser.

**Services** (`services.html`): a card per module service and per catalog service. The card
shows the logo, the name with one status dot, one muted line of host, module or stack, status
mark and «MCP» on MCP endpoints, one Open button for the public `url` or else the internal URL
`https://<route>.<domain>`, the first red reason as one line (or the amber «no internal URL», or a waiver that ends within
fourteen days), and the description. «Access and details» opens the rest: every URL, the
`ssh -N -L` tunnel, addresses on `public_bind`, how to sign in, where its secrets live
(`.env on <host>: AYE_GRAFANA_ADMIN_PASSWORD`; values are never read), its alerts with severity
and runbook, its [waivers](rules.md#waivers-end) with the rule, the reason, the end and the days
left (the ingress card carries the host's `without_auth`), what it depends on and what it
affects (everything that depends on it, transitively). The
footer carries the build time and the manifest commit.

What is not described shows red with the reason, and the summary at the top counts red first:
a catalog service failing the [service rules](rules.md#service-rules), a compose service
missing from `services`, a host without the `node` module, an ingress route to nothing, and
everything in a `--discovered <host.json>` report (from `aye discover`) that the manifest does
not declare: compose services, systemd units, listening ports no service publishes. Missing
keys in the report are fine. Drift fails the build with a list instead: an edge to an unknown
node, a flow or dependency endpoint that is not in the manifest.

## secrets

A self-hosted secrets manager that the `.env` of every project moves into: each host signs in
with its own machine identity and fetches its folder at deploy time with
[`aye secrets pull`](#aye-secrets). The default provider is [Infisical](https://infisical.com)
`v0.166.3` (`infisical/infisical`), with its own PostgreSQL `18.6-alpine3.24` and Valkey
`9.1.3-alpine3.24`. Requires `metrics`, `node` and [`backups`](#backups) on the same host.

| Setting | Default | Meaning |
|---|---|---|
| `url` | required | the address people and hosts use, e.g. `https://secrets.ops.acme.example`; Infisical's `SITE_URL`, no path |
| `provider` | `infisical` | the only provider so far, see [providers](#providers) |
| `backup_hour` | `3` | UTC hour from which the daily dump runs |
| `backup_keep_days` | `14` | dumps older than this are deleted |
| `google_login` | `false` | sign-in with Google; needs `AYE_SECRETS_GOOGLE_CLIENT_ID` and `AYE_SECRETS_GOOGLE_CLIENT_SECRET`, redirect URI `<url>/api/v1/sso/google`, see the [runbook](runbooks/infisical.md#google-sign-in) |
| `expose` | `[]` | `8090` publishes the web port on `public_bind` instead of `127.0.0.1` |

| Service | Limit | Role |
|---|---|---|
| `infisical` | 1 GiB, 1 CPU | web UI and API on `8080` (host port `8090` on `127.0.0.1`), Prometheus metrics on `9464` |
| `infisical-db` | 256 MiB | PostgreSQL in the `secrets-db` volume, `shared_buffers=64MB` |
| `infisical-cache` | 64 MiB | Valkey with a password, `maxmemory 48mb`, `noeviction` for the job queues, no persistence |
| `infisical-backup` | 64 MiB | daily `pg_dump` into the `secrets-backups` volume |

Infisical keeps about 800 MiB at rest and runs its database migrations itself on start, a
minute or more on one CPU, hence `start_period: 180s`. Its V8 heap is capped at three quarters
of its memory limit (768 MiB of the default 1 GiB), so it collects garbage before the kernel
kills it. The module needs about 1.4 GiB; a 1 GB VM does not fit it. When listing secrets gets
slow, raise `resources.infisical.memory` and `cpus`; the heap follows the limit.

Route it through ingress on the same host, `routes: {secrets: infisical:8080}`, and probe the
route in `probes.http` so `probe-failing` and `tls-expiring` watch it. Keep the route reachable
without a browser sign-in in front of it: hosts call `/api/` through it with their machine
identity.

Variables, all required. The passwords are hex because they end up inside connection URLs.

| Variable | Generate with |
|---|---|
| `AYE_SECRETS_ENCRYPTION_KEY` | `openssl rand -hex 16`, exactly 32 hex characters |
| `AYE_SECRETS_AUTH_SECRET` | `openssl rand -base64 32` |
| `AYE_SECRETS_DB_PASSWORD` | `openssl rand -hex 24` |
| `AYE_SECRETS_CACHE_PASSWORD` | `openssl rand -hex 24` |

`AYE_SECRETS_ENCRYPTION_KEY` decrypts everything: the database and its dumps hold only
ciphertext without it. Keep a copy outside the host (a password manager, paper). Losing it loses
every secret, and a different key on an existing database keeps Infisical from starting.

Metrics come from Infisical's OpenTelemetry Prometheus exporter (`OTEL_EXPORT_TYPE=prometheus`),
scraped as job `infisical` from `infisical:9464`. `OTEL_DROP_HIGH_CARDINALITY_METERS=true` drops
the meters labelled with secret names, emails and client addresses, so no secret name reaches
the metrics store. Product telemetry is off.

**Backup.** `infisical-backup` runs `secrets/backup.sh` on the PostgreSQL image, read-only and
without capabilities. Every 5 minutes it looks for today's dump; when there is none and the UTC
hour has reached `backup_hour`, or when no dump exists at all, it writes
`/backups/infisical-<YYYYmmddTHHMMZ>.dump` (`pg_dump --format=custom`, mode 600, renamed into
place only when complete) and deletes dumps older than `backup_keep_days`. `pg_dump` runs
through [`backups/backup_run.sh`](#backups), so each attempt reports as the backup job `secrets`
in the textfile directory node-exporter reads (the host `textfile_dir`, else the
`node-textfile` volume). The container touches `/tmp/backup.alive` on every pass, which its
healthcheck watches. Its variables: `AYE_BACKUP_DIR`, `AYE_TEXTFILE_DIR`, `AYE_BACKUP_HOUR`,
`AYE_BACKUP_KEEP_DAYS`, `AYE_BACKUP_INTERVAL`, `AYE_PG_DUMP`, `AYE_BACKUP_RUN` (the wrapper,
`/backup_run.sh`), `AYE_BACKUP_ALIVE`, and `AYE_BACKUP_ONCE=1` to run once and exit. One
reading of the clock per pass decides the day, the hour and the name of the dump; for tests
only, `AYE_BACKUP_CLOCK` replaces that reading, `date -u +%Y%m%dT%H%MZ`, with a command that
prints such a time (`echo 20260105T0400Z`), so a run that straddles midnight UTC cannot dump
twice, the way `AYE_DRILL_CLOCK` replaces the [restore drill](#restore-drill)'s `date +%s`.
The volume sits on the same disk:
copy it off the host (restic, rclone) for a disaster. The [runbook](runbooks/infisical.md) has
the first run, the migration of existing `.env` files and the restore.

**Mail.** With [`mail`](manifest.md#mail) Infisical sends its sign-up codes and invitations
through that relay: `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`,
`SMTP_FROM_ADDRESS`, `SMTP_FROM_NAME`. `tls: starttls` sets `SMTP_REQUIRE_TLS=true`, `none` sets
`SMTP_IGNORE_TLS=true`; on port 465 Infisical speaks TLS from the start and ignores both. Without
`mail` Infisical cannot send mail: invite people by link instead.

Alerts: `backup-missing` (no successful dump for 26 hours, or none since the job was declared)
and `backup-failed` for the [dump](#backups), `target-down` and `probe-failing` for the
application.

### Providers

The module and `aye secrets` speak of folders of key and value pairs per project and
environment, read by a machine identity bound to one project. A provider has to offer exactly
that: a non-human identity per host limited to its project, listing a folder with values,
creating keys, and an audit log of who read what.

| Provider | Host identity | Fits when |
|---|---|---|
| Infisical (default) | machine identity, Universal Auth with trusted IPs | one self-hosted service with a UI, folders, imports and an audit log for a small fleet |
| OpenBao or HashiCorp Vault | AppRole on a KV v2 mount | you already run Vault, or need dynamic database credentials and leases |
| 1Password | service account or Connect server | the team lives in 1Password and accepts a hosted dependency |
| SOPS with age | an age key per host | no server at all: encrypted files in git, rotation is a commit |

Adding one is a branch in `compose.yaml.j2` keyed on `provider` and a class implementing
`SecretsProvider` in `src/ayeops/secrets.py`; the CLI, the file format and the settings stay.

### aye secrets

`aye secrets pull` signs in as a machine identity and writes one folder to a `.env`;
`aye secrets push <.env>` imports an existing `.env` into a folder, for the migration. Both use
the REST API with the standard library only, no provider CLI on the host.

| Variable | Flag | Meaning |
|---|---|---|
| `AYE_SECRETS_URL` | `--url` | the module's `url` |
| `AYE_SECRETS_CLIENT_ID` | | client id of the machine identity |
| `AYE_SECRETS_CLIENT_SECRET` | | its client secret; never a flag, so it stays out of the process list |
| `AYE_SECRETS_PROJECT_ID` | `--project` | the project id |
| `AYE_SECRETS_ENVIRONMENT` | `-e`, `--environment` | environment slug, e.g. `prod` |
| `AYE_SECRETS_PATH` | `--path` | folder, default `/` |

Flags win over variables, variables over `--identity <file>`: a file of these variables that is
parsed, never sourced, and refused when group or others can read it. Exit code 2 means the
identity is incomplete, 1 that the API or a file failed.

- `pull -o <file>` (default `.env`) writes the folder sorted by key, mode 600, through a
  temporary file in the same directory and one rename, so compose never reads half a file.
  Imported folders are merged first and the folder's own keys win; references are expanded.
  A value is written bare when it holds only `A-Za-z0-9_./:@%+=,-`, otherwise in single quotes,
  which compose and `sh` both take literally. A value with a single quote or a line break stops
  the run before anything is written, naming the key.
- `push <file>` reads `KEY=value` lines (`export`, quotes and ` #` comments as compose reads
  them, blank and comment lines skipped), creates the folder path when it is missing and adds
  only the keys the folder does not have yet: a value already there wins, so a stale file never
  rolls back a rotation. Change a secret in the provider, not by pushing.
- Neither prints a value: output names keys and counts, errors name keys and line numbers.

Keys that people create go in with `set` and `ask`, and come out with `run`, so that a value
never shows in a terminal, a chat or a script. Every one of these prints the key, the folder, the
length, the first 8 hex characters of the value's SHA-256 and `created` or `replaced`, and nothing
else; the same fields appear in errors. The fingerprint lets you compare the value with the one
you meant, and lets anyone who reads it test guesses of a weak value, so store generated or
issued keys rather than words.

| Command | What it does |
|---|---|
| `set <KEY>` | a person types the value, hidden; `--from-clipboard` reads it from `pbpaste`, `wl-paste`, `xclip` or `xsel` and clears the clipboard afterwards with the same tool; when that fails, nothing is stored |
| `ask <KEY> [--hint TEXT]` | for an assistant or a script: prints the address of a one-time page on `127.0.0.1` and waits; the person pastes the key into its one password field |
| `gen <KEY>` | stores a random key and never overwrites, so a deploy script calls it instead of embedding a generator. A name a module declares under `generated` gets that entry's format and length, and `--bytes` or `--format` that disagree with it are refused; a `copy` name is refused. Any other name takes `--format` (`hex`, `base64`, `base64url`, `garage-key-id`, default `base64url`) and `--bytes` (at least 8, default 24) |
| `gen --manifest <file> --host <host>` | runs the check, then stores every `generated` name of that host the folder lacks, each in its format, in one batch; prints a line per key it created, the count of keys it kept, and each missing `copy` with its source, which it leaves to you. `--host` may be left out when the manifest has one host, and `--manifest` when no `KEY` is given: the default is `AYE_MANIFEST`, else the `ayeops.yaml` or `manifest.yaml` of the current directory |
| `gen --manifest <file> --host <host> --env-file <path>` | the same without a secrets manager: writes the missing names into a local `.env`, see below; `--manifest` and `--host` have the same defaults. `aye up` runs this step for a host on this machine |
| `run [--folder F] -- <cmd>` | runs `cmd` with the folder's values in its environment only, keeps its exit code and masks the values in its output |

**A local `.env` without a provider.** `gen --manifest <file> --host <host> --env-file <path>`
signs in nowhere and needs no `AYE_SECRETS_*` identity; `--identity`, `--url`, `--project`,
`--environment` and `--path` are refused with it, and so are `KEY`, `--format` and `--bytes`.
It runs the check right after the first step, so a bad file is reported before the manifest:

- reads `<path>` when it exists: a directory, a file that is not UTF-8 or a line that is not
  `KEY=value` (as `push` reads them) stops the run before anything is written, and a missing
  parent directory is refused rather than created, so a typo does not scatter files; run
  `aye render` first;
- appends each `generated` name of the host that the file lacks, sorted by key, in its catalog
  format, after the file's own lines, which keep their order and text. A key that is in the file
  is never replaced, even with an empty value; removing its line is how to regenerate it. When
  nothing is missing, the file is not touched at all;
- writes through a temporary file in the same directory and one rename, with LF line endings
  (CRLF lines of the old file become LF, a UTF-8 byte order mark is dropped) and mode 600, so
  an existing file readable by others is tightened. On Windows Python can only set or clear the
  read-only flag: the file is writable, takes the access list of its directory and is as private
  as that directory is, so keep the checkout in your own profile;
- prints, as `gen --manifest` does, one line per created key with its length and SHA-256 prefix,
  `<path>: generated N, kept K existing value(s)`, each missing `copy` with its source, and
  then each variable of the host's `.env.example` that no module generates and the file lacks
  as `<KEY> in <path>: missing, not generated, you must set it`: SMTP logins, bot tokens, admin
  keys of external providers. A value is never printed.

For the [quick start](../README.md#quick-start) this is the whole `.env`: the grafana module
declares `AYE_GRAFANA_ADMIN_USER` (hex, 8 bytes, 16 characters) as well as
`AYE_GRAFANA_ADMIN_PASSWORD` (hex, 24 bytes, 48 characters) under `generated`, so the login is
random too and `.env.example` carries no default `admin`: a known login is half of a guessed
sign-in, and a placeholder that works unchanged is one that stays unchanged in production.

- **No silent replace.** `set` and `ask` refuse a key that exists, before they ask for the value;
  `--replace` overwrites it. `gen` has no such flag: on a key that exists it stops and names
  `aye secrets set KEY --replace` as the way to change it.
- **The page** has a random path of 128 bits, answers only to the `127.0.0.1:<port>` host name,
  takes one POST of at most 64 KiB, then closes; it also closes after 10 minutes. It holds its
  port exclusively, drops a connection that sends nothing for 10 seconds, so an idle one neither
  blocks the person nor the timeout, and answers a POST without a numeric `Content-Length` with
  411 or 400. After such an early answer (also a 404 or a 413) it stops sending and reads away
  what the browser already sent, at most the declared length and 1 MiB within 2 seconds, so the
  browser shows the answer instead of a connection reset and a client that never finishes
  holds the page for 2 seconds at most. Its handler and server log nothing; the response is
  `no-store` with a `Content-Security-Policy` that allows no script, no frame and no other form
  target.
- **Checks** run before the key is stored, by key name with or without the `AYE_` prefix:
  `CLOUDFLARE*TOKEN` against `/user/tokens/verify`, `GITHUB*TOKEN` against `/user`,
  `ANTHROPIC_ADMIN*KEY` and `OPENAI_ADMIN*KEY` against their organization endpoints, which covers
  `AYE_CLOUDFLARE_DNS_API_TOKEN`, `AYE_CLOUDFLARE_ANALYTICS_TOKEN`, `AYE_GITHUB_TOKEN`,
  `AYE_ANTHROPIC_ADMIN_KEY` and `AYE_OPENAI_ADMIN_KEY`. A redirect is not followed, so the key
  goes to that host only; a value with a space, a control or a non-ASCII character is refused
  before any request. A rejected or unreachable check stores nothing; `--no-check` skips it, for
  example for a Cloudflare token owned by an account, which the user endpoint does not know.
  Keys with other names are not checked.
- **Masking in `run`** replaces each value, and each line of a multi-line value, of 4 characters
  or more with `***`, in stdout and stderr. A value cut by the child's own output (base64 of it,
  one character at a time) is not recognised, so `run` is no defence against a command that
  means to leak. The `AYE_SECRETS_*` identity is removed from the child's environment.
- `--folder` is the same flag as `--path`.

```sh
aye secrets ask AYE_CLOUDFLARE_DNS_API_TOKEN --folder /ops --hint "dash.cloudflare.com, My Profile, API Tokens"
aye secrets gen AYE_ERRORS_DB_PASSWORD --folder /central
aye secrets gen --manifest examples/acme.yaml --host central --folder /central
aye secrets gen --manifest examples/quickstart.yaml --host local --env-file rendered/local/.env
aye secrets run --folder /ops -- aye dns examples/acme.yaml --apply
```

What goes where: the secrets manager holds the value; the identity file or environment holds only
the machine identity that reads it; scripts and chat hold key names. A value reaches a screen only
when a person reads it in the secrets manager's own interface.

A deploy script fetches the `.env` right before compose reads it:

```sh
aye secrets pull --identity ~/.config/ayeops/identity.env -e prod --path /web -o .env
docker compose up -d
```

## errors

Self-hosted error tracking that the Sentry SDKs report to: each exception arrives with its stack
trace, the request and the release, grouped into issues. The default provider is
[GlitchTip](https://glitchtip.com) `6.2.6` (`glitchtip/glitchtip`, MIT, amd64 and arm64), with
its own PostgreSQL `18.6-alpine3.24` and Valkey `9.1.3-alpine3.24`. Requires `metrics`, `node`
and [`backups`](#backups) on the same host.

| Setting | Default | Meaning |
|---|---|---|
| `url` | required | the address of the web UI, e.g. `https://errors.ops.acme.example`; GlitchTip's `GLITCHTIP_DOMAIN`, no path |
| `provider` | `glitchtip` | the only provider so far |
| `retention_days` | `30` | events, issues and files older than this are deleted (`GLITCHTIP_RETENTION_DAYS`) |
| `backup_hour` | `4` | UTC hour from which the daily dump runs |
| `backup_keep_days` | `14` | dumps older than this are deleted |
| `expose` | `[]` | `8095` publishes the web and ingest port on `public_bind` instead of `127.0.0.1` |

| Service | Limit | Role |
|---|---|---|
| `glitchtip` | 768 MiB, 1 CPU | web UI, API and event ingest on `8000` (host port `8095`), Prometheus metrics on `/metrics`; runs the migrations before it serves |
| `glitchtip-worker` | 512 MiB | background tasks: event processing, alert mail, the retention cleanup |
| `glitchtip-db` | 512 MiB | PostgreSQL in the `errors-db` volume, `max_locks_per_transaction=512` as GlitchTip asks |
| `glitchtip-cache` | 64 MiB | Valkey with a password for the task queue and cache, no persistence |
| `glitchtip-backup` | 64 MiB | daily `pg_dump` into the `errors-backups` volume, written like the [secrets](#secrets) dump |

At rest the module takes about 300 MiB (web 170, worker 90, database 35, measured on an idle
instance); the limits leave room for bursts of events.

Two ways in, on purpose:

- **People** use the `url` through ingress, `routes: {errors: glitchtip:8000}`, behind the
  [auth](#auth) sign-in and then GlitchTip's own login. Sign-ups and new organizations are off
  (`ENABLE_USER_REGISTRATION`, `ENABLE_SOCIAL_APPS_USER_REGISTRATION`,
  `ENABLE_ORGANIZATION_CREATION` all `false`): GlitchTip lets the first account register and
  closes the door after it.
- **HTTPS behind ingress.** GlitchTip's server (Granian) does not read `X-Forwarded-Proto`, so
  Django would take every request for plain HTTP and build `http://` links: Google sign-in then
  sends a redirect URI that no provider accepts. Web and worker load `ayeops_settings`, which
  keeps every GlitchTip setting and adds
  `SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")`.
- **Google sign-in.** When [auth](#auth) uses `provider: google`, the web container creates or
  updates the allauth social app `google` from `AYE_AUTH_CLIENT_ID` and `AYE_AUTH_CLIENT_SECRET`
  after the migrations (`manage.py shell --no-imports < /code/google_app.py`), so a fresh
  install or a restore onto an empty database gets the provider back. `--no-imports` keeps the
  step silent: the shell of Django 6.0, which GlitchTip 6.2.6 runs, otherwise imports every model
  and logs how many on each start. The app is linked to no site (GlitchTip installs no
  `django.contrib.sites`) and to no organization, since GlitchTip treats an app of an
  organization as an invitation that opens sign-ups through it; with
  `ENABLE_SOCIAL_APPS_USER_REGISTRATION` at `false`, Google only logs in accounts that already
  exist. `ayeops_settings` sets `SOCIALACCOUNT_EMAIL_AUTHENTICATION` and
  `SOCIALACCOUNT_EMAIL_AUTHENTICATION_AUTO_CONNECT` to `True`, so an existing user whose email
  Google has verified is logged in and connected on the first sign-in instead of landing on
  `/login?socialLoginError=true`. That is safe because Google verifies the emails it returns and
  sign-ups stay closed, so no account can be created or taken over through it. Register
  `<url>/accounts/google/login/callback/` on the OAuth client; `aye permissions` lists it under
  both `auth` and `errors`. With `oidc` or without `auth` on the same host the step is absent.
- **Services** send events to the exposed port over the private network, not through ingress:
  an SDK cannot pass a browser sign-in. The DSN GlitchTip shows uses the `url`; give services
  the same key with the private address instead:
  `http://<public key>@<public_bind>:8095/<project id>`. `ALLOWED_HOSTS` holds the url's host,
  the host's address, `glitchtip`, `localhost` and `127.0.0.1`, so requests by any of these
  names are answered.

Uptime checks and the log store of GlitchTip are switched off: probes and logs have their own
modules. vmagent scrapes `/metrics` (`glitchtip_projects_total` and the
`django_http_*` request metrics), which drives `target-down`.

Variables, all required.

| Variable | Generate with |
|---|---|
| `AYE_ERRORS_SECRET_KEY` | `openssl rand -hex 32` |
| `AYE_ERRORS_DB_PASSWORD` | `openssl rand -hex 24` |
| `AYE_ERRORS_CACHE_PASSWORD` | `openssl rand -hex 24` |

With [mail](manifest.md#mail) set, GlitchTip sends issue alerts and password resets through it
with `AYE_SMTP_USER` and `AYE_SMTP_PASSWORD`; without it, mail is off.

The dump reports as the [backup job](#backups) `errors`: `backup-missing` (no
successful dump for 26 hours, or none since the job was declared) and `backup-failed`. First
run, the DSN, restore and upgrades: [runbook](runbooks/glitchtip.md).

## mcp

An MCP hub: one endpoint that gives an assistant read access to the stack, through a gateway
the manifest picks. Requires `metrics` on the same host. The design, the comparison of the four
gateways and the S3 server choice are in [mcp.md](mcp.md).

| Setting | Default | Meaning |
|---|---|---|
| `provider` | `agentgateway` | `agentgateway`; `contextforge`, `docker` and `microsoft` are accepted and refused by `aye check` as not implemented yet |
| `sources` | required | name (lowercase letters and digits) to source; every tool reaches the client as `<name>_<tool>` |
| `timeout` | `60s` | request timeout of the hub towards a source |

A source has a `kind`, a `description` and the keys of its kind:

| Kind | Runs | Keys |
|---|---|---|
| `grafana` | `mcp-grafana` with `--disable-write`, reading the Grafana of the same host with the [grafana-reader](#grafana-reader) token | `tools`, `allow_write` |
| `victoriametrics` | `mcp-victoriametrics` | `url` (default `http://victoriametrics:8428` with `metrics` as server), `tools` |
| `s3` | `mcp-s3` inside the hub, one process per source | `endpoint` (default the host's Garage), `region` (Garage's, else `us-east-1`), `path_style` (default `true`), `tools`, `allow_write` |
| `mcp` | proxies an MCP server over streamable HTTP or SSE | `url`, `transport` (`http` or `sse`), `token`, `tools` (required: the tools that read), `allow_write` |
| `openapi` | every operation of a spec as a tool named by `operationId` | `spec` (path next to the manifest, JSON or YAML), `url`, `token`, `tools`, `allow_write` |
| `catalog` | `mcp-catalog`: the manifest's catalog and the runbooks of its alerts, read-only, one per host | `tools` |
| `cli`, `mcp` with `image` | not implemented yet, see [mcp.md](mcp.md#how-each-provider-renders) | |

**Read-only by default.** The hub lets through only the tools of `tools`, or the read tools of
the source when `tools` is not set: all of Grafana's read-only mode and of VictoriaMetrics,
`s3_list_buckets`, `s3_list_objects` and `s3_get_object_metadata` of S3, the `GET` and `HEAD`
operations of a spec, the five tools of the catalog. A tool that writes is exposed only from `allow_write`: an S3 write tool
there also turns off `MCP_S3_EXT_READONLY`, a Grafana one is kept with `--enable-write-tools`.
Everything else is removed from `tools/list` and refused when called. Prompts and resources of
the sources are not passed on.

| Service | Limit | Role |
|---|---|---|
| `agentgateway` | 256 MiB, 0.5 CPU | agentgateway `v1.7.0` (`ghcr.io/agentgateway/agentgateway`, Apache-2.0): the hub on `3000` (host port `3010` on `127.0.0.1`), `/mcp` for streamable HTTP and `/sse`; metrics on `15020`, readiness on `15021`, the admin UI off; runs the S3 sources as stdio servers |
| `mcp-grafana` | 128 MiB | Grafana's MCP server `2.1.1`, streamable HTTP on `8000` for the hub only (`--allowed-hosts`, a bearer token), metrics and `/healthz` on `9090`; reads `http://grafana:3000` with the Viewer token of `grafana-reader` (`GRAFANA_SERVICE_ACCOUNT_TOKEN_FILE`, read again on every request, so a rotation needs no restart) |
| `mcp-victoriametrics` | 128 MiB | VictoriaMetrics' MCP server `v1.20.2` on `8080`, which has no tools that write |
| `mcp-tools` | 16 MiB | copies the static busybox `1.37.0-musl` into a volume: the wget of the healthchecks, since the hub and mcp-grafana images have no wget (the hub's has no shell either) |
| `mcp-s3-bin` | 16 MiB | with an `s3` source: copies the static `mcp-s3` `v1.5.1` binary and a CA bundle from `ghcr.io/txn2/mcp-s3` into a volume the hub mounts read-only |
| `mcp-catalog` | 64 MiB, 0.1 CPU | with a `catalog` source: `catalog_server.py`, one standard-library Python file on `python:3.13.16-alpine3.23` as user 65534 with a read-only root, streamable HTTP on `8000` for the hub, `/metrics` and `/health` on the same port, no host port |

The hub configuration is `mcp/agentgateway.yaml`, rendered from the manifest: one `mcp` target
per source, `prefixMode: always`, `failureMode: failOpen` (a source that is down only loses its
own tools) and one `mcpAuthorization` allow rule per source. agentgateway reloads it when it
changes, and expands `$VARIABLE` from its environment, so secrets stay in `.env`. The
expansion is textual and happens before the YAML is parsed, so every reference is rendered as a
literal block (`|2-` and the variable on the next line): the parser takes the line as it is,
and a secret may hold quotes, backslashes, `#`, `:`, `$` and spaces. A secret must be one line:
a line break ends the block, and the hub refuses the file (`invalid config` at start,
`mcp-hub-config` on a reload). An S3 source runs with a cleared environment that holds only its
own endpoint and keys. An `openapi` spec is copied to `mcp/openapi/<name>.json`.

**The catalog source.** `aye render` writes `mcp-catalog/catalog.json` from the manifest and
the runbook pages, and `mcp-catalog` serves it read-only; a deploy that changes it restarts the
server. Its tools, `<name>_<tool>` at the client:

| Tool | Answers |
|---|---|
| `list_hosts` | every host with its group, zone, architecture, provider, modules and number of services |
| `list_services` | one row per service (`host/name`): source (`manifest` or `module <name>`), state, criticality, status mark (`public`, `internal`, `none`) and the description, cut to 197 characters and `...` when longer; filters `host`, `group`, `criticality`, `status`, `limit` (default 200) |
| `get_service` | one service's entry: description, host, group, criticality, url, `depends_on`, `clients`, metrics, alert names, log source, status mark, cloud dependencies; `service` is a name, or `host/name` when it runs on several hosts |
| `list_alerts` | the alerts of a service: title, severity, `for`, summary, the expression or the baseline metric, the owning module or service, the runbook name and url |
| `get_runbook` | the runbook of an alert by the alert's name (or by the runbook's): its url and Markdown text |

The file holds only these fields, picked one by one: no `login`, `checks`, module settings,
host address or `public_bind`, no `.env` value and no variable of `aye permissions`; user names,
passwords and token-like query parameters in a URL or a summary are masked as `***`. The text of
a runbook comes from AyeOps' `docs/runbooks/` for a built-in module (the wheel carries them, so
an installed `aye` serves them too), from the module's `runbooks/` for a local module and from `runbooks/<name>.md` next
to the manifest for an alert of the manifest; without a page, `get_runbook` gives the url. The
catalog has no owner field, so none is served. Design: [mcp.md](mcp.md#the-catalog-source).

Variables:

| Variable | For | Value |
|---|---|---|
| `AYE_MCP_SESSION_KEY` | the hub | `openssl rand -hex 32`; agentgateway's `SESSION_KEY`, which encrypts the MCP session ids it hands out (without it, agentgateway tries to write a generated key into its read-only `/config`) |
| `AYE_MCP_GRAFANA_SERVER_TOKEN` | `grafana` | `openssl rand -hex 32`; the hub sends it to mcp-grafana |
| `AYE_MCP_<NAME>_ACCESS_KEY_ID`, `AYE_MCP_<NAME>_SECRET_ACCESS_KEY` | `s3` | a key that can only read: on Garage `garage key create mcp-ro` and `garage bucket allow --read --key mcp-ro <bucket>`, on R2 an Object Read token |
| `AYE_MCP_<NAME>_TOKEN` | `mcp`, `openapi` with `token: true` | the bearer token the hub sends to that source only |

How clients reach it. **The supported way is the loopback port over ssh**: the hub publishes
`3000` only as `127.0.0.1:3010` on its host and asks for no credentials itself, so the people
who can ssh to the host can use the tools, and so can the containers on the host's compose
network, which reach `agentgateway:3000` directly.

```sh
ssh -N -L 3010:127.0.0.1:3010 central
claude mcp add --transport http aye http://127.0.0.1:3010/mcp
```

On the host itself, the autopilot included, the same URL works without the tunnel. Any MCP
client that speaks streamable HTTP (`/mcp`) or SSE (`/sse`) connects the same way.

A route through ingress, `routes: {hub: agentgateway:3000}`, works for a browser behind the
[auth](#auth) sign-in like every route, but MCP clients do not do oauth2-proxy's browser
sign-in, so they are answered by the sign-in and never reach the hub; it is not a way for MCP
clients yet. `aye check` refuses the route in `auth.open` (rule `mcp-open`), since that would
hand the tools to anyone who reaches the name. Sign-in for MCP clients through the same OIDC
provider is on the [roadmap](roadmap.md#mcp-hub).

The total size and the age of the newest backup below a prefix come from `s3_list_objects`,
which returns each object's size and modification time, up to 1000 keys a call.

Metrics: `agentgateway_mcp_requests_total` (labels `method`, `server`, `resource`,
`resource_type`) for every call, `agentgateway_config_synchronized`,
`mcp_server_operation_duration_seconds` of mcp-grafana, `mcp_victoriametrics_call_tool_total`
(with `is_error`) and `ayeops_mcp_catalog_requests_total`, `ayeops_mcp_catalog_tool_calls_total`
(`tool`, `outcome`) and `ayeops_mcp_catalog_entries` (`kind`) of mcp-catalog. Every call is a line in the hub's log with the tool and the target. Alerts: `mcp-hub-down`, `mcp-hub-config` (the
hub rejected a new configuration and serves the old one), `target-down` for the sidecars,
and [`grafana-reader-failing`](runbooks/grafana-reader-failing.md) for the token mcp-grafana
reads; [runbook](runbooks/mcp-hub.md).

## objectstore

A self-hosted store for the [off-site copies](#offsite) of other hosts. Requires `metrics` on
the same host. Put it on a host in another place than the hosts it backs up: another provider,
another country, at least another building. Two providers:

- `garage` (default): [Garage](https://garagehq.deuxfleurs.fr) `v2.5.0` (`dxflrs/garage`), an
  S3 store with one node and one bucket with a size quota. The agents' key can read, write and
  **delete**: whoever takes over a backed-up host can wipe its copies. Garage `v2.5.0` is not
  backwards-compatible: once a store has run it, do not go back to an older image.
- `rest-server`: restic's [REST server](https://github.com/restic/rest-server) `0.14.0`
  (`restic/rest-server`) with `--append-only --private-repos`. A backed-up host can add
  snapshots and read its own repository, and cannot delete or overwrite anything, not even
  through restic. Retention runs on the store host instead. **Use it for backups whose loss
  matters**; keep Garage when something needs S3.

| Setting | Default | Meaning |
|---|---|---|
| `provider` | `garage` | `garage` or `rest-server` |
| `quota` | `40g` | maximum size in `m`, `g` or `t` (binary units): Garage's bucket quota, or rest-server's `--max-size` for the whole store; writes beyond it fail |
| `data_dir` | none | host path for the data instead of the `objectstore-data` (Garage) or `reststore-data` (rest-server) volume, e.g. a second disk; rest-server needs it owned by root |
| `expose` | `[]` | publishes the API on `public_bind`, required for agents on other hosts: `3900` for Garage, `8000` for rest-server |
| `bucket` | `offsite` | Garage only: the bucket Garage creates on its first start; every agent writes below `<bucket>/<its host>` |
| `region` | `garage` | Garage only: the S3 region Garage answers with; agents get it as `AWS_DEFAULT_REGION` |
| `meta_dir` | none | Garage only: host path for the metadata instead of the `objectstore-meta` volume |
| `prune_schedule` | `11:00` | rest-server only: UTC time of day of the daily `forget` and `prune`; keep it away from the agents' `schedule`, a prune locks the repository |

The rule `objectstore-provider` rejects a setting of the other provider.

### Garage

| Service | Limit | Role |
|---|---|---|
| `garage` | 256 MiB, 1 CPU | S3 API on `3900`, admin API and Prometheus metrics on `3903` (host port on `127.0.0.1` only) |
| `garage-admin` | 32 MiB | keeps the bucket quota equal to `quota` through the admin API every 5 minutes, metrics on `9132` |

Garage starts with `server --single-node --default-bucket`: on the first start it creates a
single-node layout (zone `dc1`, capacity the size of the data disk), the access key from
`AYE_OBJECTSTORE_ACCESS_KEY_ID` and `AYE_OBJECTSTORE_SECRET_ACCESS_KEY` with read, write and
owner rights on the bucket, and the bucket. Every later start finds them and changes nothing,
so there is no init step to forget. The configuration is `objectstore/garage.toml`: SQLite
metadata with `metadata_fsync` and `data_fsync` (a backup store must survive a power cut),
`replication_factor = 1`, and no zstd compression, since restic data is encrypted and does not
compress. The RPC port `3901` is never published.

`garage-admin` runs `objectstore/objectstore_admin.py` (Python standard library) with the admin
token: `GetBucketInfo` by the bucket's alias, `UpdateBucket` when the quota differs from the
setting. It serves `ayeops_objectstore_bucket_bytes`, `ayeops_objectstore_bucket_objects`,
`ayeops_objectstore_bucket_quota_bytes`, `ayeops_objectstore_admin_ok` and
`ayeops_objectstore_admin_last_success_timestamp_seconds`, each with a `bucket` label, on
`9132/metrics` (job `garage-admin`). Changing `quota` and redeploying is all a resize takes.

Variables, all required:

| Variable | Generate with |
|---|---|
| `AYE_OBJECTSTORE_RPC_SECRET` | `openssl rand -hex 32`, exactly 64 hex characters |
| `AYE_OBJECTSTORE_ADMIN_TOKEN` | `openssl rand -base64 32` |
| `AYE_OBJECTSTORE_ACCESS_KEY_ID` | `echo GK$(openssl rand -hex 12)`, Garage's own key format |
| `AYE_OBJECTSTORE_SECRET_ACCESS_KEY` | `openssl rand -hex 32` |

Metrics are scraped as job `garage` from `garage:3903/metrics` without a token; the admin port
is published on loopback only. Alerts: `objectstore-down` (no successful scrape for 5 minutes,
critical, also on silence), `objectstore-unhealthy` (`cluster_healthy` is 0),
`objectstore-quota` (bucket above 80 % of its quota for 30 minutes), `objectstore-admin` (quota
and usage not reconciled for an hour). Operating it: [runbook](runbooks/offsite.md).

### rest-server (append-only)

| Service | Limit | Role |
|---|---|---|
| `rest-server` | 128 MiB, 1 CPU | restic REST API on `8000`, `--append-only --private-repos --prometheus`, read-only root, no capabilities |
| `reststore-admin` | 512 MiB, 1 CPU | daily `forget` and `prune` of every client's repository with that client's policy, sizes against the quota, metrics on `9133` (loopback) |
| `reststore-restic` | 64 MiB | copies the static restic `0.19.1` binary into a volume `reststore-admin` mounts read-only |
| `offsite-drill` | 1 GiB, 2 CPUs | only with a client's `restore_drill`: restores its newest snapshot into a throwaway PostgreSQL, no network, repository read-only; see [restore drill](#restore-drill) |

**Users.** Every host whose `offsite` target is this host gets a user of its own name and a
repository at `/<host>/`; `--private-repos` keeps it out of the others. A second user,
`metrics`, may read `/metrics` only. At every start `rest-server` builds the htpasswd file
(bcrypt, `htpasswd -B`) in a tmpfs from the variables below: the hashes never touch a disk,
the passwords never appear on a command line, and nothing is committed. Adding a client host
and redeploying adds its user.

**Retention on the store side.** The agent on a client host cannot delete, so it no longer
runs `forget`/`prune`. `reststore-admin` does, once a day at `prune_schedule`, straight on the
repository directory (`restic -r /data/<host>`, the local backend, not through the append-only
API): `unlock`, `forget --host <host> --group-by host --keep-*` with the client's `keep_*`, then
`prune`. That needs the repository password, so **the store host holds a key to every client's
repository**. The trade-off, honestly:

- A taken-over **client** host can add snapshots and fill the quota, and cannot delete or
  rewrite old ones. This is the case the module closes.
- A taken-over **store** host can read and delete every copy on it. It always could delete
  (it holds the disk); the repository password adds reading. Keep the store host more locked
  down than the clients, and give it a key of its own with `restic key add` so the client's
  password never has to live there and either key can be removed without the other (see
  [append-only off-site copies](runbooks/offsite-append-only.md#a-key-for-the-store)).
- The restore test stays on the client: it only reads, it exercises the path a real restore
  takes (that host, its credentials, the network), and it needs no extra secret on the store.

**Transport.** The repository URL is `rest:http://<address>:8000/<host>/`: plain HTTP with
basic auth, so `address` and `public_bind` must be on a private network that encrypts on the
wire (Tailscale, WireGuard), as with Garage. restic encrypts the content itself either way;
what HTTP would expose on an open network is the basic-auth password, which lets one add
snapshots, never delete them.

Variables on the store host, all required:

| Variable | Value |
|---|---|
| `AYE_RESTSTORE_<HOST>_PASSWORD` | per client (`<HOST>` upper-cased, `-` as `_`): the rest-server password of that host, `openssl rand -base64 33`; the same value is the client's `AYE_OFFSITE_REST_PASSWORD` |
| `AYE_RESTSTORE_<HOST>_RESTIC_PASSWORD` | per client: a restic key to that host's repository, ideally one made with `restic key add` for the store |
| `AYE_RESTSTORE_METRICS_PASSWORD` | `openssl rand -base64 33`, the `metrics` user's password |

Metrics on `reststore-admin:9133/metrics` (job `reststore-admin`): rest-server's own
`rest_server_*` families, fetched with the `metrics` user, follow
`ayeops_reststore_server_up`, `ayeops_reststore_data_bytes`, `ayeops_reststore_quota_bytes`, and
per `repo` label `ayeops_reststore_repo_initialized`, `ayeops_reststore_repo_bytes`,
`ayeops_reststore_maintenance_ok`, `ayeops_reststore_maintenance_last_success_timestamp_seconds`
(0 until the first success), `ayeops_reststore_maintenance_last_attempt_timestamp_seconds`,
`ayeops_reststore_snapshots` and `ayeops_reststore_retention_ok`. A repository that does not
exist yet is skipped until its client's first copy runs `restic init`. Alerts:
`reststore-down` (rest-server does not answer the admin for 5 minutes, critical, also on
silence), `reststore-maintenance` (forget or prune failed, kept more than the policy, or has not
passed for 50 hours), `reststore-quota` (store above 80 % of `quota`). Setup and moving from
Garage: [runbook](runbooks/offsite-append-only.md).

## backups

The metrics contract of a host's dump jobs: the host's own `pg_dump`, `mysqldump` or `tar`, and
the built-in dumps of [secrets](#secrets) and [errors](#errors). Every job runs its command
through the wrapper `backups/backup_run.sh` (POSIX sh), which writes start, end, success, size
and duration into the [node](#node) textfile directory; the manifest declares the job with its
cron schedule, and `aye check` turns the schedule into the age after which a job without a
success counts as failed. Silence is not success: a job that stopped, hangs or never ran alerts
like one that fails. Requires `metrics` and `node` on the same host, and a node `textfile_dir`
when the manifest declares jobs of its own (rule `backup-textfile`).

A job with a `command` is installed by the kit: `aye render` writes a systemd timer and service
for it, and `aye deploy` puts them in place, so the schedule is written once, in the manifest.
A job without one is only declared, for a dump that cron or your own unit already runs.

```yaml
node:
  textfile_dir: /var/lib/ayeops/textfile
backups:
  timezone: Europe/Berlin
  jobs:
    acme-db:
      schedule: "30 4 * * *"
      grace: 1h
      output: /var/backups/acme/acme-$(date -u +%Y%m%dT%H%MZ).dump
      command: >-
        runuser -u postgres -- pg_dump --format=custom --file="$AYE_BACKUP_OUTPUT" acme
        && find /var/backups/acme -name 'acme-*.dump' -mtime +14 -delete
```

| Setting | Default | Meaning |
|---|---|---|
| `jobs` | `{}` | map of job name (`^[a-z][a-z0-9_-]{0,39}$`, the `backup` label) to `{schedule, grace, command, output}`; a name may not be one of the built-in dumps on the same host (rule `backup-name`) |
| `jobs.<name>.schedule` | required | the five cron fields the job runs at, in `timezone`: numbers, `*`, ranges, lists and steps (`30 4 * * *`, `0 */6 * * *`, `15 2 * * 1-5`); no names or `@daily`. Day of month and day of week combine as in cron: either matches when both are restricted |
| `jobs.<name>.grace` | `2h` | minutes or hours (`30m`, `2h`) a run may take and start late; the missing threshold is the longest gap between two scheduled starts plus `grace` |
| `jobs.<name>.command` | none | one line of `sh` that writes the dump, run as root by the job's systemd service; with it, `output` is required |
| `jobs.<name>.output` | none | absolute path of the file or directory the command writes, the wrapper's `-o`. `sh` expands it at the start of each run inside double quotes, so `$(date ...)` works and `"`, `\` and a backquote are refused; the command finds the result in `$AYE_BACKUP_OUTPUT` |
| `timezone` | the manifest's [`timezone`](manifest.md#top-level) | IANA zone of the schedules: the zone of the rendered timers, and of the host's cron for a job without a `command` |
| `interval` | `300` | seconds between rewrites of the declaration file |

**The threshold.** `aye check` walks four years of the schedule from 2027 in `timezone` and
takes the longest gap between two consecutive starts: `86400` for a daily job in UTC, `90000`
in a zone with summer time (the night the clocks go back has 25 hours), three days for a
weekday job, 31 days for a monthly one. Plus `grace`, that is the job's
`ayeops_backup_max_age_seconds`. A schedule that does not parse, or fires less than twice in
four years (`0 0 30 2 *`, `0 0 29 2 *`), fails rule `backup-schedule`.

**The RPO.** A service names the job that dumps it in its [`data`](manifest.md#data) block, with
the `rpo` it promises. `aye check` then refuses a job whose threshold exceeds that `rpo` (rule
[`backup-rpo`](rules.md#data-rules)), and the job's `ayeops_backup_max_age_seconds` is the
shorter of the threshold and the `rpo`. `backup-jobs` also writes the declared `rpo` as
`ayeops_backup_rpo_seconds` (from `AYE_BACKUP_RPO`, `name=seconds` like `AYE_BACKUP_JOBS`); the
table **Last good dump against its RPO** on the Reliability dashboard shows, for every job with an
`rpo`, the age of its last success, the `rpo` and the age as a share of it, orange from 80 % and
red from 100 %.

**The timer.** For a job with a `command`, `aye render` writes three files under `backups/`:

| File | Content |
|---|---|
| `systemd/ayeops-<host>-backup-<name>.timer` | `OnCalendar=` lines of the schedule in `timezone`, `Persistent=true` (a run missed while the host was off starts at boot), `WantedBy=timers.target` |
| `systemd/ayeops-<host>-backup-<name>.service` | `Type=oneshot`, `ExecStart=/bin/sh /opt/ayeops/<host>/backups/jobs/<name>.sh`, `Nice=10`, best-effort I/O at the lowest priority, as root |
| `jobs/<name>.sh` | sets and exports `AYE_BACKUP_OUTPUT` from `output`, then runs `backup_run.sh -d <textfile_dir> -o "$AYE_BACKUP_OUTPUT" <name> /bin/sh -c '<command>'` |

The schedule converts field by field: each cron field becomes the sorted list of its values
(`*/15` minutes is `00,15,30,45`, `1-5` weekdays is `Mon,Tue,Wed,Thu,Fri`), a full range becomes
`*`, and the zone goes at the end, so `30 4 * * *` in Berlin is
`OnCalendar=*-*-* 04:30:00 Europe/Berlin`. systemd then follows the zone's summer time itself, as
the threshold above assumes. When day of month and day of week are both restricted, cron runs on
either, and a calendar line requires both, so the timer gets two lines: `0 1 1,15 * 1` is
`*-*-01,15 01:00:00 <zone>` and `Mon *-*-* 01:00:00 <zone>`. `systemd-analyze calendar
'<line>'` on the host prints the next run of a line; a systemd too old for a zone in a calendar
expression refuses it, and the deploy then fails at the timers step.

`aye deploy` ships the three files with the rest of the render and, after `docker compose up`,
runs the timers step (see [deploy](deploy.md)): the units go to `/etc/systemd/system`, systemd
reloads when one changed, each timer is enabled and started, a changed one restarted, and units
of the host that left the render are disabled and deleted; when that fails, the next deploy
runs the step again, since the swap names the units still installed. systemd starts no second
run of a service that is still active, so a slow dump never overlaps the next one. The command, its
output and its exit code are in `journalctl -u ayeops-<host>-backup-<name>.service`. A host
prepared by [host-init](host-init.md) before backup timers existed needs its host-init script
run again for the `timers` step; `aye doctor` says so.

Units name `/opt/ayeops/<host>`, so a host with timers deploys only there (`--dir` elsewhere is
refused before any ssh call). A host without systemd (Alpine with OpenRC) keeps `command` out of
the manifest and runs the job from its own cron with the same `schedule`.

**The wrapper.** `aye render` writes it to `backups/backup_run.sh`, so on the host it sits at
`/opt/ayeops/<host>/backups/backup_run.sh`. A job runs

```sh
backup_run.sh [-d textfile_dir] -o <output> <name> <command> [argument...]
```

as root (the textfile directory belongs to root) and drops privileges in the command, for
example `sudo -u postgres` or `runuser`. `-d` defaults to `AYE_TEXTFILE_DIR`, else
`/var/lib/ayeops/textfile`. `-o` is the file or directory the command writes: a run succeeds
when the command exits 0 **and** something at or below `-o` is newer than one second before the
start of the run and the size is above 0, so a dump that exits 0 and writes nothing, or writes
elsewhere, fails. The second of slack is for busybox `find -newer`, which compares whole
seconds: a dump written within the second the run started would otherwise count as old. The
wrapper exits with the command's code, 1 for a success without fresh output, and 75 when the
dump worked but the metrics could not be written (the dump still runs then). A job without a
`command`, called by cron as `30 4 * * * /usr/local/bin/acme-dump`:

```sh
#!/bin/sh
out=/var/backups/acme/acme-$(date -u +%Y%m%dT%H%MZ).dump
exec /opt/ayeops/edge/backups/backup_run.sh -o "$out" acme-db \
  runuser -u postgres -- pg_dump --format=custom --file="$out" acme
```

A file name with `date` belongs in a script: cron treats `%` in a crontab line as a newline.
Keep that cron line and `schedule` the same; the kit does not see it.

Metrics of a job, in `ayeops_backup_job_<name>.prom`, each with the label `backup`:
`ayeops_backup_last_start_timestamp_seconds`, `ayeops_backup_running` (1 from the start to the
end of a run; a killed run stays at 1), `ayeops_backup_last_end_timestamp_seconds`,
`ayeops_backup_last_duration_seconds`, `ayeops_backup_last_size_bytes` (a file's bytes, a
directory's `du -sk` times 1024), `ayeops_backup_last_exit_code`,
`ayeops_backup_last_run_success` and `ayeops_backup_last_success_timestamp_seconds` (the end of
the last successful run, kept across failed runs). The file is written to a temporary name and
renamed, so a scrape never reads half of it.

**Built-in dumps.** A module that dumps its own database declares the job in its
`module.yaml` (`backup_jobs`: name, service, schedule over its settings, zone, grace), and its
dump container runs `pg_dump` through the same wrapper, mounted from `backups/backup_run.sh`.
So `secrets` and `errors` require `backups` on their host (`backups: {}` is enough), and their
dumps report under the label of the module:

| Job | Service | Schedule | Threshold |
|---|---|---|---|
| `secrets` | `infisical-backup` | `0 <backup_hour> * * *` UTC | `93600` (26 hours) |
| `errors` | `glitchtip-backup` | `0 <backup_hour> * * *` UTC | `93600` (26 hours) |

The container still decides when to dump (today's dump missing and the UTC hour at
`backup_hour`, checked every 5 minutes) and still writes `<dump>-<YYYYmmddTHHMMZ>.dump` through
a dot-file renamed into place, so [offsite](#offsite) picks up the newest dump as before. They
replace `secrets-backup-stale` and `errors-backup-stale` and the `ayeops_secrets_backup_*` and
`ayeops_errors_backup_*` metrics: `backup-missing` is the same 26 hours, now also for a dump
that never ran, and `backup-failed` adds the failed run.

| Service | Limit | Role |
|---|---|---|
| `backup-jobs` | 16 MiB | busybox `1.38.0`, no network, read-only root, no capabilities: every `interval` writes `ayeops_backups.prom` with `ayeops_backup_max_age_seconds` and `ayeops_backup_declared_timestamp_seconds` (when the job was first declared, kept across rewrites) per declared job, the built-in dumps of the host included, and `ayeops_backup_rpo_seconds` per job a service's `data` gives an `rpo` |

The declaration is what makes a job that never ran visible: without it there would be no series
to miss. A job removed from the manifest leaves the declaration on the next rewrite, and its
old file no longer alerts, because both alerts join on `ayeops_backup_max_age_seconds`.

Alerts: `backup-failed` (warning: the last run failed or left no fresh output, for 5m; it
clears with the next success) and `backup-missing` (critical, also on silence: no success for
longer than `max_age_seconds`, counted from the last success or, for a job that never
succeeded, from its declaration, for 10m). Runbooks: [backup-failed](runbooks/backup-failed.md),
[backup-missing](runbooks/backup-missing.md). The [offsite](#offsite) module copies the newest
dump afterwards; put its `schedule` after the dump's end plus `settle_minutes`. The Reliability
dashboard shows the time since the last success of every job, and next to the `rpo` of the jobs
a service declares one for.

## offsite

Encrypted, deduplicated copies of a host's newest backup file in another place, with
[restic](https://restic.net) `0.19.1`. The target is the [objectstore](#objectstore) of another
host or any restic repository URL (Backblaze B2, Cloudflare R2, MinIO, a REST server), so moving
to a provider is a change of `target`. Requires `metrics` and `node` on the same host.

| Setting | Default | Meaning |
|---|---|---|
| `source` | required | absolute glob of the backup files, e.g. `/var/backups/trofey/*.fc`; the newest settled match is copied, its directory is mounted read-only |
| `target` | required | `{host: <name>}` for the objectstore of that host, or `{url: <restic repository>}` such as `s3:https://<endpoint>/<bucket>/<prefix>`; add `append_only: true` to a `url` target whose key cannot delete (a B2 key without `deleteFiles`, another append-only REST server) |
| `schedule` | `05:30` | local time of day from which the daily copy runs; put it after the dump |
| `timezone` | the manifest's [`timezone`](manifest.md#top-level) | IANA zone of `schedule`, also the `TZ` restic stamps snapshots with |
| `keep_daily`, `keep_weekly`, `keep_monthly` | `7`, `4`, `3` | `restic forget` policy; at least one above 0 |
| `restore_test_days` | `7` | days between restore tests |
| `settle_minutes` | `15` | a file modified more recently is still being written and waits |
| `retry_minutes` | `60` | wait after a failed run before the next attempt |
| `verify` | `pg_dump` | `pg_dump` also checks that the restored file starts with the custom-format header `PGDMP`; `none` checks the sha256 only |
| `quota`, `region` | none | with a `url` target: the provider's quota for `offsite-quota` and its region; with a `host` target both come from the objectstore |
| `restore_drill` | none | restore the newest copy into a throwaway database on the store and check it, see [restore drill](#restore-drill) |

| Service | Limit | Role |
|---|---|---|
| `offsite` | 512 MiB, 1 CPU | the agent, `offsite/offsite_agent.py` on Python 3.13 (standard library), read-only root, no capabilities |
| `offsite-restic` | 16 MiB | copies the static restic binary from `restic/restic` into the `offsite-restic` volume the agent mounts read-only, then sleeps |

With `target: {host: central}` the repository is
`s3:http://<central address>:3900/<bucket>/<this host>` on Garage, or
`rest:http://<central address>:8000/<this host>/` on rest-server with
`RESTIC_REST_USERNAME=<this host>` and the password from `AYE_OFFSITE_REST_PASSWORD`: plain
HTTP, so the address must be on a private network that encrypts on the wire (WireGuard,
Tailscale); restic encrypts the content itself either way. The rule `offsite-target` checks
that the target is another host in another `zone` when both name one, runs the objectstore,
exposes its port and has an `address`.

**Append-only targets.** A rest-server target, or a `url` target with `append_only: true`, sets
`AYE_OFFSITE_APPEND_ONLY=1`: the agent backs up, then lists the snapshots and reads
`restic stats`, and runs no `forget` or `prune` (they would fail; retention belongs to the
store side). `ayeops_offsite_retention_ok` is then not written, and `offsite-retention` stays
quiet; on rest-server `reststore-maintenance` takes its place. A file that changed during the
copy keeps its snapshot, since nothing can be deleted; the agent records the id of the last
good snapshot and the restore test reads that one.

**A pass.** Every minute the agent checks whether today's slot has passed without a successful
copy. Then it lists the repository (a missing repository or a wrong password stops here, with
the cause in the log), picks the newest file matching `source` whose modification time is at
least `settle_minutes` old, hashes it, and runs
`restic backup --host <host> --tag ayeops --tag sha256=<hex> <file>`. A file whose size or
modification time changed during the copy loses its snapshot and the run fails. Then
`restic forget --host <host> --group-by host --keep-*` and `restic prune`. The grouping is by
host on purpose: restic groups by host and paths by default, every daily dump has a new file
name, so every snapshot would sit alone in its group and never be forgotten. The agent
recomputes the policy itself (the same rules as restic 0.19, including keeping the oldest
snapshot while a bucket is not full) and sets `retention_ok` to 0 when restic kept anything
else or used more than one group.

**Restore test.** After the first copy and then every `restore_test_days`, the agent streams
the newest snapshot back with `restic dump`, hashes it on the fly and compares the result with
the `sha256=` tag recorded at backup time, checks the `PGDMP` header, then runs `restic check`.
Nothing is written to disk. A sha256 match proves the bytes came back unchanged through
encryption, chunking, the network and the object store; that the dump itself restores into a
database is the job of the [restore drill](#restore-drill).

Metrics in the node textfile directory (`ayeops_offsite.prom`):
`ayeops_offsite_last_success_timestamp_seconds` (0 until the first success),
`ayeops_offsite_last_run_success`, `ayeops_offsite_last_attempt_timestamp_seconds`,
`ayeops_offsite_last_source_bytes`, `ayeops_offsite_last_added_bytes` (what the copy added to
the repository after deduplication and compression), `ayeops_offsite_last_duration_seconds`,
`ayeops_offsite_snapshots`, `ayeops_offsite_repo_size_bytes` (`restic stats --mode raw-data`),
`ayeops_offsite_retention_ok`, `ayeops_offsite_repository_ok`, `ayeops_offsite_quota_bytes`
(url targets only), `ayeops_offsite_restore_test_timestamp_seconds`,
`ayeops_offsite_restore_test_last_success_timestamp_seconds` and
`ayeops_offsite_restore_test_ok`, and `ayeops_offsite_max_age_seconds`, the threshold of
`offsite-stale` from `AYE_OFFSITE_MAX_AGE_SECONDS` (`93600`, or a shorter `rpo`; the alert
falls back to `93600` for an agent that does not write it). State survives restarts in the
`offsite-state` volume, the restic cache in `offsite-cache`.

Variables, all required:

| Variable | Value |
|---|---|
| `AYE_OFFSITE_RESTIC_PASSWORD` | `openssl rand -base64 33`; keep a copy outside the host, see below |
| `AYE_OFFSITE_ACCESS_KEY_ID` | S3 targets: the key id; for a Garage `host` target the objectstore's `AYE_OBJECTSTORE_ACCESS_KEY_ID` |
| `AYE_OFFSITE_SECRET_ACCESS_KEY` | S3 targets: the secret; for a Garage `host` target the objectstore's `AYE_OBJECTSTORE_SECRET_ACCESS_KEY` |
| `AYE_OFFSITE_REST_PASSWORD` | rest-server `host` targets instead of the two above: the store's `AYE_RESTSTORE_<HOST>_PASSWORD` |

The restic password is the only key to the copies, and the host that keeps it in its `.env` is
the host whose loss the copy is for. Keep the password in a password manager or on paper as
well, or the off-site copy cannot be read when it is needed.

Alerts: `offsite-stale` (no successful copy for 26 hours, or for the shorter `rpo` of a service
whose [`data`](manifest.md#data) lists `offsite` in `copies`, critical, also on silence),
`offsite-restore-test` (the last restore test failed, or none passed for 8 days, also on
silence), `offsite-quota` (url targets: repository above 80 % of `quota`; host targets use
`objectstore-quota`), `offsite-retention`, `offsite-drill` (below). All compare with `> bool`
or `== bool`: a filtered comparison returns nothing when the condition is false, which
Grafana's no-data state would turn into a false firing. Operating it:
[runbook](runbooks/offsite.md).

### Restore drill

A backup counts once it is made **and** restored. The restore test above proves the bytes; the
drill proves the database: on a schedule it restores the newest off-site snapshot into a
throwaway PostgreSQL, runs SQL checks against it and records how long the restore took. That
duration is the measured recovery time of the data. Enable it on the client's `offsite`:

```yaml
offsite:
  source: /var/backups/shop/*.fc
  target: {host: central}
  restore_drill:
    engine: postgres
    version: 18
    every_days: 7
    schedule: "13:00"
    max_minutes: 60
    checks:
      - name: orders
        query: select count(*) as value from orders
      - name: fresh
        query: select (max(placed_at) > now() - interval '36 hours')::int as value from orders
```

| Setting | Default | Meaning |
|---|---|---|
| `engine` | required | `postgres`: `source` holds `pg_dump -Fc` files (`verify: pg_dump`) |
| `version` | required | major of the drill's PostgreSQL, `14` to `18`; use the production major, a dump never restores into an older one |
| `checks` | required | one or more `{name, query}`: a `SELECT` or `WITH` with a column `value`, read-only, one statement, as for [SQL checks](checks.md); the drill passes only when every check returns a number above 0 |
| `every_days` | `7` | days between passing drills |
| `schedule` | `13:00` | UTC time of day the drill starts; must not hold the store's `prune_schedule` within `max_minutes` |
| `max_minutes` | `60` | the drill is killed and fails after this long |
| `jobs` | `2` | parallel `pg_restore` jobs |

**Where it runs.** On the store host, in the service `offsite-drill` of the
[objectstore](#rest-server-append-only), not on the client. The drill rehearses the day the
client is gone: it uses nothing of the client, only what the store holds, the repository and
the store's own key to it (`AYE_RESTSTORE_<HOST>_RESTIC_PASSWORD`, the variable
`reststore-admin` already has). It reads the repository straight from the store's disk,
mounted **read-only**, with `restic --no-lock --no-cache`: it can neither delete nor write
anything, not even a lock, and needs no network at all (`network_mode: none`). On the client
the drill would measure the same `pg_restore` but would need the client to survive, and the
client host usually has the less memory to spare and the production database to protect.
That is also why the drill needs `target: {host: ...}` with `provider: rest-server`: a Garage
store would hand it an S3 key that can delete, and a `url` target has no store host in the
manifest (rule `offsite-drill`).

**What a drill does.** At `schedule`, when the last passing drill is `every_days` old (and
right away on the first start), the container

1. wipes its scratch space and stops any PostgreSQL a killed drill left behind;
2. lists the newest snapshot of the client with `restic ls --host <client> latest` and
   writes the file with `restic dump` to `/scratch`, an anonymous volume. A temp file rather
   than a pipe: `pg_restore` needs a seekable archive for `-j` and for items out of archive
   order, and the file is checked against the snapshot's `sha256=` tag and the `PGDMP` header
   before anything else;
3. runs `initdb` in `/scratch` as the `postgres` user, with `listen_addresses = ''`: the
   server listens on a Unix socket in the scratch space only, no port, no TCP;
4. `pg_restore --no-owner --no-privileges --exit-on-error -j <jobs>` into a database `drill`;
5. runs every check in a read-only transaction with a statement timeout;
6. stops PostgreSQL and wipes `/scratch`, also after a failure, and kills the whole drill
   after `max_minutes`.

A failed drill retries at the next day's `schedule`. Drills of several clients of one store
run one after the other in one container, so they must share `version`.

**What it proves and what it does not.** A passing drill proves that the newest off-site copy
can be read with the key on the store, is the file the client sent, restores completely into
a stock PostgreSQL of that major, and holds what the checks expect. It does not prove the
application starts on it, that roles, grants and settings outside the dump are right (they
are skipped), that extensions outside the stock image exist (the drill fails on them, which is
the point), or how long it takes on other hardware: the duration is measured on the store.

**Resources.** `offsite-drill` has 1 GiB and 2 CPUs by default; the restored database lives
on the scratch volume, not in memory, so the store's disk needs room for one dump plus one
restored database while a drill runs. Change the limit on the store,
`objectstore: {resources: {offsite-drill: {memory: 2g}}}`. The container counts towards the
store's [memory budget](rules.md) like every other.

Metrics in the store's node textfile directory, one file per client
(`ayeops_offsite_drill_<client>.prom`), each with a `repo` label:
`ayeops_offsite_drill_ok`, `ayeops_offsite_drill_last_attempt_timestamp_seconds`,
`ayeops_offsite_drill_last_success_timestamp_seconds`,
`ayeops_offsite_drill_duration_seconds` (fetch plus restore: the recovery time),
`ayeops_offsite_drill_fetch_seconds`, `ayeops_offsite_drill_restore_seconds`,
`ayeops_offsite_drill_dump_bytes`, `ayeops_offsite_drill_restored_bytes`
(`pg_database_size`), per `check` label `ayeops_offsite_drill_check_value` and
`ayeops_offsite_drill_check_ok`, and `ayeops_offsite_drill_deadline_timestamp_seconds`, when a
pass is due at the latest: the last pass, or the first start, plus `every_days` plus one day.
The textfile is also the drill's state; a client without a drill loses its file at the next
start. For tests only, `AYE_DRILL_CLOCK` replaces the drill's clock, `date +%s`, with a command
that prints Unix seconds.

Alert `offsite-drill` (warning, also on silence): the last drill failed, or the deadline
passed. It exists only while some host has a `restore_drill`. Runbook:
[offsite-drill](runbooks/offsite-drill.md); by hand: the
[manual drill](runbooks/offsite.md#restore-drill).

## Flow baselines

A flow can collapse while every request still succeeds: an upstream API answers 200 with an
empty list, a parser stops matching after a markup change, a queue drains into nowhere. No
error rate notices, and a "zero for an hour" alert fires only when the whole flow stops, not
when one source or category of it drops tenfold. A `baseline` alert compares each series of a
flow with its own usual level instead of a fixed threshold. It replaces `expr` in a service
alert or a module alert, and `op`/`threshold` in an [`sql` check](checks.md#sql):

```yaml
services:
  scraper:
    alerts:
      - name: listings-flow
        title: New listings
        severity: warning
        runbook: listings-flow
        baseline:
          metric: listings_new_total{app="scraper"}
          by: [source, category]
          below: 0.3
          floor: 5
          above: 5
          quiet:
            - {days: [sun], from: "02:00", to: "04:00"}
          skip:
            - match: {category: auctions}
              reason: the site retired auctions in October
```

| Field | Default | Meaning |
|---|---|---|
| `metric` | required | a metric selector, `name` or `name{matchers}`; not an expression |
| `kind` | `counter` | `counter` takes the `increase` over the window, `gauge` its `avg_over_time` |
| `by` | `[]` | labels that split the flow; every label combination is its own series and alert instance, so a new source or category is covered without a manifest change; with `host`, the [profile](manifest.md#profiles) labels `project` and `group` are kept too |
| `window` | `1h` | the last window compared, in minutes or hours |
| `below` | `0.3` | share of the usual level under which the window counts as a drop |
| `floor` | `5` | the usual level must be above this for any rule to fire, so a flow that is near zero anyway never alerts |
| `above` | none | with a number above `1`, a spike rule fires at that many times the usual level (duplicates, loops) |
| `empty_share` | none | with a share, a rule fires when more than that share of the answers in the window were `outcome="ok_empty"`, see below |
| `quiet` | `[]` | planned windows, `{days, from, to}` in UTC with `days` from `mon` to `sun` (default every day); `from` after `to` crosses midnight |
| `skip` | `[]` | per-series opt-outs, `{match: {label: value}, reason, until}`; the labels must be in `by`, or `project` and `group` with `host` in `by` (rule `baseline-skip`); the optional `until`, a day `YYYY-MM-DD`, ends the opt-out, see [waivers end](rules.md#waivers-end) |

The usual level of a series is the median of the same window at the same hour of the week over
the last 4 weeks (`offset 1w` to `offset 4w`), so night lows and weekends are not anomalies and
one odd week does not move it. A series with fewer than 2 of those weeks is learning and never
fires. A series that disappears while its usual level is known counts as zero, so an app that
stopped exporting a category is caught too. It is the expected count of the
[incident journal's](#incidents) lost requests, read live: the same 4 weeks, the same median.

One alert renders these Grafana rules, each one instance per series:

| Rule | Fires when | Severity |
|---|---|---|
| `<name>` | the window is above zero but under `below` times the usual level | the alert's, default `warning` |
| `<name>-zero` | the window is exactly zero | `critical`, or `page` when the alert pages |
| `<name>-spike` | with `above`, the window is over `above` times the usual level | the alert's |
| `<name>-empty` | with `empty_share`, the empty share of the window is over it | the alert's |

All of them need the usual level above `floor` (the empty rule needs the number of answers above
it) and 2 weeks of history, wait `for` (default `1h`), return `0` inside a `quiet` window or for a
skipped series, and end in `or on() vector(0)`, so they return a series while healthy or
learning and pass `alert-filter-nodata`. The generated names count for `alert-unique`. In an
`sql` check the series is `ayeops_check_sql_value` of the check, `kind: gauge` and split by its
`label` column, so a skip matches `label`; the check keeps its own `<service>-<check>` rule for a
failed or stale query, and the baseline rules are `<service>-<check>-low`, `-zero`, `-spike`.

Like every rule but `host-down`, the baseline rules are [held back](#grafana) while a host is
down, which works per series only when the series carries a `host`: split a flow that runs on
several hosts with `by: [host, ...]` and the series of a down host read 0 instead of firing
`<name>-zero` next to `host-down`. A flow summed over hosts has no host to hold back, so it
still fires, as the flow did stop.

### Empty answers

A collector or an app that fetches items counts each answer by `outcome`: `ok` for an answer
with items, `ok_empty` for a successful answer that should have carried items and did not (a
category page listing nothing, a feed with no entries), and its error outcomes apart. A poll for
"anything new since the last poll" that is often empty by design is not `ok_empty`. With
`empty_share` the baseline rule divides the increase of `outcome="ok_empty"` by that of
`outcome=~"ok|ok_empty"` per series; it needs a counter (rule `baseline-empty`). The drop and zero
rules of the same alert count every outcome, so empty answers alone do not look like a drop.

### Dashboard

With at least one baseline alert Grafana gets `AyeOps / Flow baselines` (uid
`ayeops-baseline`), a row per alert: the share of the usual level per series with the band from
`below` to `above` shaded, the actual window against the dashed usual level, and the weeks of
history per series, blue while learning. Every rule of the alert links to its share panel, so
the alert opens on the series that tripped it. Runbook: [flow-baseline](runbooks/flow-baseline.md).

## Write your own module

A project can keep modules of its own next to the manifest and enable them like built-ins:

```yaml
module_paths:
  - ./ops/modules
hosts:
  edge:
    modules:
      acme-queue:
        backlog_max: 500
```

Every directory listed in `module_paths` holds one directory per module with the same layout as
`src/ayeops/modules/<name>/`, plus a `runbooks/` directory for the pages of its alerts:

```text
ops/modules/
  queue/
    module.yaml
    compose.yaml.j2
    scrape.yaml.j2
    runbooks/queue-backlog.md
    dashboards/queue.json
```

A minimal `module.yaml`:

```yaml
name: acme-queue
summary: Exporter for the acme job queue.
requires:
  metrics: {}
defaults:
  backlog_max: 1000
  expose: []
  resources: {}
settings_schema:
  type: object
  additionalProperties: false
  properties:
    backlog_max: {type: integer, minimum: 1}
    expose: {type: array, items: {enum: [9140]}}
services:
  queue-exporter:
    description: Reports the length and the oldest job age of the acme job queue.
    memory: 32m
    cpus: 0.1
    metrics: [acme_queue_backlog]
    alerts: [target-down, queue-backlog]
    logs: docker
    status:
      internal: queue telemetry, the jobs themselves carry the public marks
alerts:
  - name: queue-backlog
    title: Queue backlog too long
    expr: acme_queue_backlog > bool 1000
    for: 10m
    severity: warning
    runbook: queue-backlog
dashboards: [queue.json]
files: []
```

`compose.yaml.j2` renders `services:` and `volumes:` with `settings`, `host`, `project` and
`remote_write_url` in scope, `scrape.yaml.j2` renders a list of vmagent jobs. The renderer adds
limits, platform, restart policy and labels exactly as for built-ins.

A module that reads a credential from the `.env` declares it, so that
[`aye permissions`](host-init.md#aye-permissions) can say what to create and how narrow it may be:

```yaml
permissions:
  - issuer: Acme queue cloud
    credential: API token
    env: [ACME_QUEUE_TOKEN]
    when: "settings.backlog_max > 0"
    grants:
      - "queues:read on {{ settings.queue }} only"
generated:
  - env: [ACME_QUEUE_EXPORTER_PASSWORD]
    format: hex
    bytes: 24
host_directories:
  - setting: spool_dir
    owner: "65534:65534"
    mode: "0750"
    for: the exporter's spool, written as nobody
```

`issuer`, `credential`, `env` and every `grants` line are Jinja templates with `settings` (this
module), `modules` (every module of the host), `host`, `project`, `mail` and `contact` in scope;
a grant that renders empty is dropped and one that renders several lines becomes several grants.
`when` is a Jinja expression; `each` (an expression yielding a list) repeats the entry with
`item` in scope, as the grafana module does for datasources with a `password_env`. `generated`
lists variables you make yourself or copy from another AyeOps host, each entry with the `format`
its values take, so that [`aye secrets gen`](#aye-secrets) makes them without a guess:

| `format` | Value | `bytes` |
|---|---|---|
| `hex` | lower-case hex, 2 characters per byte; safe inside URLs, connection strings and SQL | required, 8 to 64 |
| `base64` | standard base64 with `+`, `/` and `=` padding, 4 characters per 3 bytes | required, 8 to 64 |
| `base64url` | URL-safe base64 without padding (`-` and `_`) | required, 8 to 64 |
| `garage-key-id` | `GK` and 24 hex characters, Garage's own access key id | none |
| `copy` | never generated: the value of another host, named by `from` (a template, like `issuer`) | none |

`bytes` is the number of random bytes, and a value is exactly that long: a component that needs
exactly 32 bytes gets 32. Every built-in entry carries a format, and a name that two modules
declare has the same format in both.
`host_directories` are created by [`aye host-init`](host-init.md#module-directories) when the
setting holds a path. A test holds every built-in module to this: each `${AYE_*}` its templates
read is declared in `permissions` or `generated`.

A local module is held to the rules of a built-in one, and `aye check` reports them with the
path `module_paths.<index>.<module>`:

| Rule | Requirement |
|---|---|
| `module-path` | every entry of `module_paths` is a directory with at least one `<name>/module.yaml` |
| `module-spec` | `module.yaml` matches [`module.schema.json`](../src/ayeops/schema/module.schema.json) and its `settings_schema` is a valid JSON Schema; `requires` names known modules |
| `module-clash` | the name is not taken by a built-in module or another local one |
| `module-files` | every `files` source and every dashboard exists |
| `image-pinned` | every image is pinned to an exact version, never `latest`; a digest after the tag (`name:tag@sha256:<digest>`) is accepted and recommended, see [images pinned](../CONTRIBUTING.md#images-pinned-and-multi-arch) |
| `alert-known` | every alert a catalog entry lists is defined by some module |
| `service-*`, `alert-*` | every catalog entry passes the [service rules](rules.md#service-rules); every alert has a severity and a runbook, and a relative runbook has its page in `runbooks/<slug>.md` |
| `module-used` | a warning, not an error: the module is enabled on some host; a module nobody runs is a leftover, or one enabled under another name |
| `runbook-used` | a warning, not an error: every page in `runbooks/` is the runbook of one of the module's alerts, or is linked (`[text](page.md)`) from such a page or from a page linked that way; an operating page the alert pages link to counts |
| `yaml-duplicate-key`, `yaml-key-type` | `module.yaml` repeats no key and has string keys only, see [YAML rules](rules.md#yaml-rules) |

Alerts of a local module link `<runbook_base_url>/<slug>`: the module's own `runbook_base_url`
when `module.yaml` sets one (where the module is published), else the manifest's. Publish the
pages of `runbooks/` there.
