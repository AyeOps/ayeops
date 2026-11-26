# AyeOps

[![ci](https://github.com/AyeOps/ayeops/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/AyeOps/ayeops/actions/workflows/ci.yml)
[![e2e](https://github.com/AyeOps/ayeops/actions/workflows/e2e.yml/badge.svg)](https://github.com/AyeOps/ayeops/actions/workflows/e2e.yml)
[![release](https://img.shields.io/github/v/release/AyeOps/ayeops?label=release)](https://github.com/AyeOps/ayeops/releases)
[![license](https://img.shields.io/github/license/AyeOps/ayeops)](LICENSE)
[![python](https://img.shields.io/badge/python-3.13%2B-3776ab)](pyproject.toml)
[![platforms](https://img.shields.io/badge/images-amd64%20%7C%20arm64-2ea44f)](docs/modules.md)

AyeOps is an open-source ops kit for small fleets: a laptop, a home server, a cheap ARM VM.
You describe hosts and services in one manifest; `aye check` refuses a service that ships
without a description, a metric, an alert with a runbook, a log source and a status-page mark,
and `aye render` turns the manifest into a docker compose project per host with VictoriaMetrics,
vmagent, exporters and a provisioned Grafana.

What ships today, as the [modules](#modules) below: metrics and logs, probes and declared
checks, backups monitoring with off-site copies and restore tests, on-call escalation and an
incident journal, a status page, an MCP hub, and the autopilot's first level in shadow mode,
which diagnoses but runs no action. A module marked preview works and is tested, but its
settings, metrics or alerts may still change in a minor release ([changelog](CHANGELOG.md)).
Actions of the autopilot, Kubernetes and the rest are on the [roadmap](docs/roadmap.md).

## Quick start

The quick start runs the monitoring of one host on your own machine:
[`examples/quickstart.yaml`](examples/quickstart.yaml) renders six containers (VictoriaMetrics
and vmagent, node-exporter, cAdvisor, blackbox-exporter and Grafana) with memory limits of
2.2 GB in total, every port on `127.0.0.1`, no alert delivery and no external account.

You need git, [uv](https://docs.astral.sh/uv/getting-started/installation/) and Docker with
Compose v2 (`docker compose version` answers): Docker Engine on Linux, or Docker Desktop on
macOS or Windows. Ports 3000, 8080, 8428, 8429, 9100 and 9115 of `127.0.0.1` must be free.
`aye` needs Python 3.13 or newer; `uv` downloads one when the machine has none.
The commands below work the same in a Linux or macOS shell and in PowerShell.

1. Clone the repository and start the stack:

   ```sh
   git clone https://github.com/AyeOps/ayeops
   cd ayeops
   uv sync
   uv run aye up examples/quickstart.yaml --start
   ```

   `aye up` runs the steps of the long form below in one go: it checks the manifest, renders
   `rendered/local/`, writes the two variables Grafana needs to `rendered/local/.env` (random
   values, mode 600, never printed, kept on the next run) and runs
   `docker compose -f rendered/local/compose.yaml up -d`. Without `--start` it prints that line
   instead of running it. Compose names the project `quickstart-local`; after about a minute
   every container reports `healthy` in `docker compose -f rendered/local/compose.yaml ps`.

   The manifest has one host, `local`, with `arch: amd64`, and every container gets
   `platform: linux/<arch>`. On an ARM machine (a Mac with Apple silicon, a Raspberry Pi, an
   ARM VM) `aye up` warns `the manifest says arch amd64 and this machine is arm64`: change that
   line of `examples/quickstart.yaml` to `arch: arm64` and run it again.

2. Open <http://127.0.0.1:3000> and sign in with the login and password of
   `rendered/local/.env` (`AYE_GRAFANA_ADMIN_USER` and `AYE_GRAFANA_ADMIN_PASSWORD`; open the
   file in an editor to read them). Under Dashboards, folder `AyeOps`:

   - `AyeOps / Health` first: the host's state, load, CPU, memory and fullest filesystem, the
     alerts firing and the scrape targets that are down, and the busiest containers. Samples
     arrive every 15 seconds, so give the graphs a minute or two.
   - `AyeOps / Probes`: the two probed URLs, Grafana's own `/api/health` and
     `https://example.com`, with answer times and the certificate expiry of the HTTPS one.
   - `AyeOps / Hosts` for one host in depth, `AyeOps / Services` for every scrape target.
   - Alerting, Alert rules: the rules the modules ship, each linking its runbook.

   On Docker Desktop the host is Docker Desktop's Linux VM: CPU, memory and disks are the
   VM's, not those of macOS or Windows. The quickstart sets `node: {rslave: false}`, since
   Docker Desktop on macOS refuses node-exporter's default root mount.

3. Stop and clean up:

   ```sh
   uv run aye down examples/quickstart.yaml --volumes --stop
   ```

   This runs `docker compose -f rendered/local/compose.yaml down --volumes`, which removes the
   containers, their network and the volumes with the metrics and Grafana's data; without
   `--volumes` the data stays for the next `aye up`, and without `--stop` the line is only
   printed. Then delete the `rendered` directory.

<details>
<summary>The same, step by step</summary>

1. Check the manifest:

   ```sh
   uv run aye check examples/quickstart.yaml
   ```

2. Render the compose project:

   ```sh
   uv run aye render examples/quickstart.yaml -o rendered
   ```

   This writes `rendered/local/`: `compose.yaml`, the configs, the dashboards and alert rules
   Grafana provisions, and `.env.example` with the two variables the project needs,
   `AYE_GRAFANA_ADMIN_USER` and `AYE_GRAFANA_ADMIN_PASSWORD`. `-o rendered` is the default.

3. Generate `rendered/local/.env`:

   ```sh
   uv run aye secrets gen --manifest examples/quickstart.yaml --host local --env-file rendered/local/.env
   ```

   The grafana module declares both variables as generated secrets, so this one command writes
   the whole file: a random login of 16 hex characters and a random password of 48. It needs
   no secrets manager and no account, prints each key with its length and the first 8 hex
   characters of its SHA-256 but never the value, and creates the file readable by you only
   (mode 600 on Linux and macOS; on Windows the file takes the permissions of its folder). Run
   again, it adds only the keys the file lacks and never replaces a value. A variable that no
   module generates, such as an external API token, it names with
   `missing, not generated, you must set it`; the quickstart has none, and `aye up` refuses to
   start while one is unset. `--host` may be left out when the manifest has one host.

   Or write the two lines yourself, with a login and a password of your choice:

   ```text
   AYE_GRAFANA_ADMIN_USER=<your login>
   AYE_GRAFANA_ADMIN_PASSWORD=<your password>
   ```

   `.env` files are in `.gitignore`, and so is `rendered/`.

4. Start it:

   ```sh
   docker compose -f rendered/local/compose.yaml up -d
   docker compose -f rendered/local/compose.yaml ps
   ```

   Compose reads the `.env` next to `compose.yaml`.

5. Stop and clean up:

   ```sh
   docker compose -f rendered/local/compose.yaml down --volumes
   ```

</details>

To watch a service of your own, add it under `services` of the manifest (see the
[manifest reference](docs/manifest.md#service)): `aye check` refuses it until it has a
description, a metric, an alert with a runbook, a log source and a status mark.

## Install a release

The quick start runs `aye` from the clone. To use it next to a manifest of your own, install
a tagged release, `<version>` being one listed under
[Releases](https://github.com/AyeOps/ayeops/releases), and say in the manifest which releases
it accepts:

```sh
uv tool install git+https://github.com/AyeOps/ayeops@v<version>
aye --version
```

```yaml
version: 1
ayeops: ">=0.3,<0.4"
project: acme
```

`aye check`, `render`, `deploy` and the other commands that read the manifest stop with an
`ayeops-version` error naming both versions when the installed `aye` is outside that range:
see [pinning a release](docs/manifest.md#pinning-a-release). Every release on GitHub carries
the wheel and the sdist and its notes; [docs/releasing.md](docs/releasing.md) says how one is cut.

## Full example

[`examples/acme.yaml`](examples/acme.yaml) is a fleet, not a laptop setup: a central `arm64`
host whose exposed ports bind to its VPN address `10.8.0.1` (`public_bind`), with Traefik and
a wildcard certificate over Cloudflare DNS-01, oauth2-proxy sign-in, Garage, Telegram alerts,
the status page reporter and the autopilot, and an `amd64` host `edge` that ships to it: 56
containers in all. It uses every section of the manifest, and
[`examples/rendered/`](examples/rendered) is its committed render, so you can read what each
module renders. It is not meant to run as is: it needs the VPN address, a Cloudflare token and
OAuth clients, and the `replace-me` values of its `.env.example` are refused by oauth2-proxy
and Garage.

Rendering again into the same directory removes the files the previous render wrote and this
one no longer produces, and only those. A repository that commits its render checks it in CI
with `aye render <manifest> -o <dir> --check`, which writes nothing and exits 1 with the added,
changed and stale paths when the committed tree is out of date: see
[docs/deploy.md](docs/deploy.md#render-into-a-directory).

On a real fleet, `aye deploy examples/acme.yaml --host central --ssh ops-central --sudo`
renders a host, ships it over ssh without a half-written file in between and starts it, and `--all --parallel 4` rolls it out
to every host, a canary first and the central stack last: see [docs/deploy.md](docs/deploy.md).
With `connect: {ssh: ops-central, sudo: true}` on the host in the manifest, `--host central`
alone says the same, and every host of a fleet gets its own ssh alias and mode.
`aye doctor examples/acme.yaml --host central --ssh ops-central --sudo` checks first, read-only,
what would make that deploy fail: docker and compose, arch, the directory, sudo or sudo-rs, disk,
clock, memory and which required `.env` variables are missing, without moving a value.
`aye diff` with the same options shows first what the deploy would change on each host: the
files added, changed and removed, a unified diff of each, the `.env` keys missing, unused or
changed since the last deploy (compared on the host, no value or hash leaves it) and the services
it would recreate.
`aye host-init` prints a root script that replaces that sudo with a deploy user allowed to run
one helper on one directory, and `aye permissions` lists the narrowest external credentials the
manifest needs and the format of every secret you generate yourself, which
`aye secrets gen --manifest <file> --host <host>` then stores in the secrets manager, or with
`--env-file <path>` in a local `.env`: see [docs/host-init.md](docs/host-init.md).
Teams that harden or bootstrap hosts with their own Ansible playbooks target the manifest
instead of a second host list: `aye inventory examples/acme.yaml -o inventory.yaml` writes the
hosts with groups such as `zone_office`, `module_node` and `service_postgres`, and `--check`
fails CI when a committed inventory is out of date: see [docs/inventory.md](docs/inventory.md).
Provisioned dashboards are read-only, so new ones get drawn in the Grafana UI and live only in
its database: `aye dashboards drift examples/acme.yaml` lists them and exits 1 while any exist,
and `aye dashboards pull examples/acme.yaml` writes them as reviewable JSON into the manifest's
dashboards directory, so the next deploy provisions them under the same uid: see
[docs/modules.md](docs/modules.md#dashboards-drawn-in-grafana).

Every image is pinned to an exact version and the digest of its multi-arch index, so a tag
pushed again cannot change what a host pulls, and runs on `linux/amd64` and `linux/arm64`.
Ports bind to `127.0.0.1` unless the manifest exposes them on an explicit address.

## Modules

| Module | Status | What it runs |
|---|---|---|
| `metrics` | stable | VictoriaMetrics single-node and vmagent; `role: agent` ships to a central host over remote_write |
| `logs` | stable | Loki and Alloy; Alloy ships the containers of the compose projects declared for its host, optionally journald, to a central Loki |
| `node` | stable | node-exporter, plus textfile collectors for laptop battery capacity, status and AC, and for a pending reboot; `host-down` leaves out a host the manifest marks [`volatile`](docs/manifest.md#volatile-hosts-and-maintenance-windows), which warns only after a day away, and a host's declared maintenance windows hold its alerts back |
| `containers` | stable | cAdvisor |
| `smart` | stable | smartctl_exporter: SMART health, bad sectors, NVMe wear, drive temperature |
| `gpu` | stable | nvidia_gpu_exporter: NVIDIA GPU temperature, memory and driver health |
| `network` | stable | link state, speed, errors and Wi-Fi signal of the declared host interfaces |
| `disk-forecast` | stable | an alert when a filesystem fills up within 7 days |
| `hygiene` | stable | a nightly prune of stopped containers, unused images and build cache above a kept size, with what it freed as metrics |
| `reconciler` | preview | starts again a declared container that Docker left down (a failed start after a reboot, a crash without a restart policy), with a back-off, an alert when it gives up and one JSON line per attempt; it also counts the running containers the manifest does not declare (a `docker run` by hand, a leftover after a service moved) and warns after an hour |
| `image-updates` | stable | Diun once a day over the running containers on a floating tag (`latest`, `postgres:18`): which of them run an older image than their tag now points to, as metrics and one alert per host |
| `updates` | stable | self-hosted Renovate once a day over your repositories: update pull requests for images, libraries and actions, with open and overdue updates as metrics |
| `vulns` | preview | Trivy once a day over every image the host's compose projects run: known vulnerabilities per image, severity and fix as metrics, an alert for a critical one with a fix and for a scan that failed or did not run |
| `eol` | preview | endoflife.date once a day for the cycles the host's image tags carry, its OS and listed components: days until end of support and of security support as metrics, an alert 90 days before security support ends, after it ended, and when the data is 3 days old |
| `probes` | stable | blackbox-exporter for HTTP 2xx and TLS expiry, for the host's own internet (ping of the gateway and public addresses, DNS) and for the private network links to other hosts (`link-down` per zone instead of a `host-down` per host), RDAP exporter for domain expiry |
| `checks` | stable | the runners of the [checks](docs/checks.md) declared under a service: HTTP, TCP and DNS through blackbox-exporter, sandboxed scripts and read-only SQL queries, each with its metrics and alert |
| `cloudflare` | stable | optional: Cloudflare edge traffic, cache, errors and origin time of proxied zones, and the free-plan limits of R2, Workers and D1 |
| `delivery` | stable | optional: recording rules, the `Delivery` and `Quality` dashboards and alerts over what CI and deploy scripts push with `aye report`: deploy and CI durations and failures, tests, flaky tests, coverage, mutation score, merge lead time |
| `github` | stable | optional: GitHub Actions minutes and billed usage of organizations and users, with an 80 % alert, and the importer of `aye report` artifacts from GitHub-hosted runners |
| `ai-usage` | stable | optional: the month's and day's spend of Anthropic and OpenAI by model, tokens, rate limits and key expiry from their admin reports, with budget, spike and key-expiry alerts |
| `sentinel` | stable | a 48 MB watcher on another host: one Telegram message when the central host stops answering, a reminder every 6 hours, an all-clear, a dead-man ping while all is well, and on request a daily digest of every backup, so that "all fresh" also arrives |
| `oncall` | preview | optional: on-call escalation for critical alerts with GoAlert: a Telegram message with an acknowledge button that repeats until someone presses it, quiet hours at night, email, escalation steps from the manifest |
| `incidents` | preview | optional: the incident journal: every firing alert from Grafana's alert state history becomes one record with service, host, severity, start, end, duration and night, kept as JSON lines on the central host, and the `Incidents` dashboard with downtime weighted by service `criticality` (a host outage counts for every manifest service of the host), degraded minutes from warnings, requests lost behind the ingress against a four-week baseline, time to restore and night incidents; with `oncall` also who acknowledged each paged incident in GoAlert and when, and the time to acknowledge by criticality, day and night |
| `status` | preview | optional: the status page reporter next to Grafana: the firing alerts and maintenance silences of the services behind each public component, pushed once a minute, signed, to the status page Worker |
| `autopilot` | preview | optional: level 1 in shadow mode: for each critical alert a dossier from metrics, logs, containers and deploys, then up to three ranked hypotheses from Claude, within hard caps per run, hour, night and day, and a journal of every run with its timing, tokens and cost; after the incident it labels which hypothesis matched what fixed it (the reconciler, a host outage in the incident journal, or a person's verdict; an acknowledgement in GoAlert marks a run no signal explains as one a person handled) and exports the hit rate per incident class and rank; secrets in logs, labels and the environment are masked before anything leaves the host ([what leaves](docs/autopilot.md#what-leaves-your-host)); it runs no action and messages nobody by default |
| `grafana` | stable | Grafana with the datasource, dashboards and alert rules of the enabled modules, fleet Health, Services and Reliability dashboards in English or Ukrainian, your own dashboards and datasources, Telegram contact point, and a Viewer token in a volume for agents that read alerts |
| `ingress` | stable | Traefik with human HTTPS names for internal services over a private network, wildcard certificate via Cloudflare DNS-01; `aye dns` writes the A records |
| `auth` | stable | oauth2-proxy: Google or OIDC sign-in in front of every ingress route without a login of its own, an email allowlist, and Grafana's own OAuth with the same client |
| `secrets` | stable | a self-hosted secrets manager (Infisical by default) with its own PostgreSQL, Valkey and a daily dump; `aye secrets pull` writes a host's `.env` from it at deploy time; `set`, `ask`, `gen` and `run` store and use single keys without printing them |
| `errors` | stable | self-hosted error tracking for the Sentry SDKs (GlitchTip by default) with its own PostgreSQL, Valkey and a daily dump; the UI behind the sign-in, ingest over the private network |
| `mcp` | preview | optional: an MCP hub, agentgateway by default, with read-only tools from Grafana, VictoriaMetrics, S3 stores, the service catalog and its runbooks, other MCP servers and OpenAPI specs, reached over ssh on the host's loopback port; tools that write are denied unless allowed one by one |
| `objectstore` | preview | the store for the off-site copies of other hosts: Garage (S3, one bucket with a quota) or an append-only restic REST server whose clients cannot delete their copies |
| `backups` | preview | the metrics contract of every dump job, the host's own and the built-in dumps of `secrets` and `errors`: a wrapper writes start, end, success, size and duration of every run, alerts fire on a failed run and on a missing one with the threshold taken from the job's cron schedule, a job with a `command` becomes a systemd timer that `aye deploy` installs, and a service's [`data`](docs/manifest.md#data) declares the `rpo`, `rto` and copies its dump must meet, which `aye check` holds the jobs to |
| `offsite` | preview | restic copies of a host's newest backup file to an objectstore on another host or any restic repository, with retention (on the store when it is append-only), a weekly restore test and an optional restore drill that restores the copy into a throwaway PostgreSQL on the store and checks it |
| `portal` | stable | two static pages generated from the manifest: where things run, and how to reach each service and where its secrets live, with product icons and live status, latency and resources read in the browser through a read-only metrics proxy; anything not described shows red |

Details: [docs/modules.md](docs/modules.md).

## Documentation

- [Documentation index](docs/index.md): every page and every command
- [Manifest reference](docs/manifest.md)
- [Catalog rules](docs/rules.md): a new service ships with a description, a metric and an alert
- [Modules](docs/modules.md)
- [Checks](docs/checks.md): HTTP, TCP, DNS, script and read-only SQL checks declared under a service
- [Deploy](docs/deploy.md): `aye deploy` swaps a host's files atomically and recreates exactly the services whose config changed, and rolls a fleet out canary first; `aye diff` shows beforehand what it would change and `aye doctor` says read-only why a deploy would fail
- [Host init](docs/host-init.md): a deploy user that may run one helper on one directory, and the narrowest credentials with `aye permissions`
- [Ansible inventory](docs/inventory.md): `aye inventory` writes the hosts of the manifest as an Ansible inventory for the hardening and bootstrap playbooks a team already runs
- [Discover](docs/discover.md): `aye discover` probes a Linux host read-only and suggests its manifest entry; `--adopt` writes a manifest that describes a host with running compose projects as it is, so its first render changes nothing that runs
- [Delivery metrics](docs/delivery.md): `aye report` for CI and deploys
- [Runbooks](docs/runbooks/)
- [Status page](docs/status.md): the Worker, its push contract, and `aye status deploy`
- [MCP hub](docs/mcp.md): the four gateways compared, sources, and what `aye check` refuses
- [Autopilot](docs/autopilot.md): level 1 in shadow mode, its caps and what leaves the host
- [Failover](docs/failover.md): the design of failover by priority
- [End-to-end run](docs/e2e.md): the CI job that deploys a manifest on one runner and checks it
- [Roadmap](docs/roadmap.md)
- [Releasing](docs/releasing.md): bump the version, merge, push a `v<version>` tag

## Contributing

Setup, the checks CI runs, the rules of the repository and how to add a module are in
[CONTRIBUTING.md](CONTRIBUTING.md). Report a vulnerability privately as
[SECURITY.md](SECURITY.md) says, never in a public issue. Everyone taking part follows the
[code of conduct](CODE_OF_CONDUCT.md); what changed is in [CHANGELOG.md](CHANGELOG.md).

## License

Apache-2.0, see [LICENSE](LICENSE) and [NOTICE](NOTICE).

AyeOps is published by the team behind [trofey.app](https://trofey.app).
