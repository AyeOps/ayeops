# Changelog

All notable changes to AyeOps are written here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and AyeOps follows
[semantic versioning](https://semver.org/): while it is `0.x`, a minor release may change the
manifest or the rendered output in a way that needs action, and a patch release must not
([releasing](docs/releasing.md)).

`0.3.0` is the first release of the public repository. `0.2.0` was tagged while the repository
was private and its build is not published here; `0.1.0` was the version of the code before it
and was never tagged.

## [Unreleased]

### Added

- `probes`: `links` pings the private `address` of other hosts (a list of names, or `true` for
  every other host with an address that is not volatile), whatever VPN carries it. The new
  critical alert `link-down` fires when no probed host of a zone has answered for 3 minutes, and
  while it holds the `host-down` of those hosts is held back, so a dropped tunnel or site pages
  once with the zone's name instead of once per host. Peers in a maintenance window drop out of
  it; rule `probe-links` checks the list. The `AyeOps / Probes` dashboard shows every link.
- `metrics`: the alerts `metrics-shipping-backlog` (warning), when for 15 minutes a host's
  vmagent queue holds more than 30 minutes of its own traffic or more than half of its room, and
  `metrics-shipping-dropped` (critical), when in the last hour the full queue dropped blocks or
  storage rejected one.
- `sentinel`: `digest: {at, timezone, url}` sends a daily backup digest to the alert chat: one
  line per dump job and off-site copy with its last success, size and restore test, and a
  summary of what is stale or failed, so that a quiet day also says the backups are fine. The
  new alert `sentinel-digest-overdue` fires when a digest is 2 hours late, and the rule
  `sentinel-digest` asks for `digest.url` when there is no `samples` check to read it from.
- Hosts declare expected downtime in the manifest instead of a silence in Grafana
  ([volatile hosts and maintenance windows](docs/manifest.md#volatile-hosts-and-maintenance-windows)).
  `volatile: true`, or a duration from `1h` to `7d`, leaves a host out of `host-down`; the new
  warning `host-gone` fires once it has been away for a day, or the duration, so a workstation
  switched off at night never pages. `maintenance: [{days, start, duration}]` (UTC, at most 24
  hours, crossing midnight and the week as needed) holds back every alert of the host,
  `host-down` included, while a window is open. The alerts of the services declared on such a
  host are held too, whichever host probes them. Rules `host-volatile` and
  `maintenance-window` check both.
- `replication` on a service declares it the PostgreSQL streaming replica of its `primary`,
  with an optional `max_lag` (default `5m`) and `application_name`. AyeOps then writes
  `<replica>-replication-lag` (warning), when the replica has not replayed the WAL position the
  primary had `max_lag` ago, measured on the primary so an idle primary never reads as lag;
  `<replica>-replication-broken` (critical), when the replica left recovery, does not stream or
  a node reports nothing; and `<replica>-replication-two-primaries` (critical), when both nodes
  accept writes. `aye check` refuses an unknown, planned or cascaded primary
  (`replication-primary`), an engine other than `postgres` (`replication-engine`), a pair whose
  `metrics` lack the postgres_exporter series the alerts read (`replication-metrics`) and two
  replicas of one primary under one `application_name` (`replication-name`).

### Changed

- `metrics`: vmagent runs with `-remoteWrite.inmemoryQueues=1` and sends fresh samples ahead of
  its on-disk backlog, so a host that reconnects after an outage reports at once and its backlog
  fills in behind it.

## [0.3.0] - 2026-11-15

### Added

- Status page: the "Powered by AyeOps" footer links to the project on GitHub, in the
  page's language, without sending the page's address along.
- `updates`: `allowed_commands` sets Renovate's `allowedCommands`, the `postUpgradeTasks`
  commands a repository may run; each entry must be a whole-command pattern anchored with `^`
  and `$`, and the default runs none.
- `aye discover --adopt <report> -o <manifest>`: a manifest that describes a host with running
  compose projects as it is. Each compose project becomes a `compose` entry with a stub written
  from the report, each running service a catalog entry with a container-count alert and dated
  waivers for its metrics and status mark, each stopped one `state: disabled`. No module is
  enabled unless `--observe`, which leaves off any exporter whose port or container name is
  taken, so the first render and diff create and recreate nothing that runs
  ([adopting a host](docs/discover.md#adopting-a-host)).
- The probe keeps the bind address, container port and protocol of every published port of a
  compose service, as `ports` next to `published_ports`.

- `aye doctor` names the sudo a host runs and, on sudo-rs, warns or fails when sudo-rs rejects a
  line of a sudoers file ([sudo-rs](docs/host-init.md#sudo-rs)).

### Changed

- Images: busybox 1.38.0, diun 4.33.1, Garage v2.5.0 (a node that ran it cannot go back to an
  older Garage), Alloy v1.21.3, blackbox-exporter v0.29.1, nvidia_gpu_exporter 1.16.0, valkey
  9.1.3, each pinned by the digest of its multi-arch index.
- Mutation tests: a pull request runs only the mutants of the functions it changes and fails on
  a survivor there; the full run with the per-module baselines runs before a release, and the
  release workflow refuses a tag whose commit has no successful full run
  ([releasing](docs/releasing.md)).

### Fixed

- `aye host-init` on a host whose `sudo` is sudo-rs (Ubuntu 25.10 and newer): the script left
  `visudo -cf` refused `Defaults:<user> !requiretty`, so the script stopped before installing
  the helper; it now omits that line there.
- `aye status deploy` on Windows: the query of the tenant state reaches wrangler whole
  (`cmd.exe` cut it at the first line break, so the deploy stopped after the migrations), and
  the emoji in wrangler's output no longer stop the command on a console in another code page.

### Security

- A release signs a build provenance of its wheel and sdist, built on a GitHub-hosted runner
  with the uv cache off; `gh attestation verify` checks a downloaded file against it
  ([verify a release](docs/releasing.md#verify-a-release)).
- Every job of a pull request runs on a GitHub-hosted runner whatever `AYE_RUNS_ON` says, every
  checkout drops its git credentials, and the `zizmor` job of CI fails on a high finding in the
  workflows. The hosted-minutes counter of `e2e` and its variables `AYE_HOSTED_MINUTES_BUDGET`
  and `AYE_E2E_RUNS_ON` are gone ([where e2e runs](docs/e2e.md#where-it-runs)).

## [0.2.0] - 2026-11-06

### Stability of the modules

Every module is new, and all of them are young. Each one below is marked:

- **stable**: its settings, metric names and alert names are meant to stay as they are through
  `0.x`; a change that needs action is listed here under the release that makes it;
- **preview**: it works and is tested, but has been in the kit for a short time, acts on its
  own or depends on a design that is still moving, so its settings, metrics or alerts may change
  in a minor release without a deprecation period.

### Added: the catalog and its commands

- The manifest (`version: 1`) and its JSON Schema: hosts, profiles and groups, services with
  their catalog entries, modules per host, checks, cloud dependencies with limits, the status
  marks, and the `ayeops` key that pins the releases a manifest accepts.
- `aye check`: the [catalog rules](docs/rules.md). A service without a description, a metric,
  an alert with a severity and a runbook, a log source and a status mark is refused; the
  services of the built-in modules are held to the same rules by tests. The check also refuses
  an image that is not pinned, a dashboard link to a panel that does not exist, and text with
  invisible, bidi or mixed-script characters. A manifest or `module.yaml` that repeats a key or
  has a key that is not a string is refused ([YAML rules](docs/rules.md#yaml-rules)), and a
  waiver may carry an end date that the check enforces, with a warning in its last fourteen
  days ([waivers end](docs/rules.md#waivers-end)). Unused profiles, externals, local modules and
  their runbook pages are warnings ([unused entries](docs/rules.md#unused-entries)); `--strict`
  fails on warnings too.
- `aye render`: one docker compose project per host, with memory and CPU limits, platform,
  restart policy, labels, log rotation and ports bound to `127.0.0.1` unless exposed. A render
  into the same directory removes the files the previous one wrote and this one no longer
  produces, and `--check` writes nothing and exits 1 when a committed render is out of date.
- `aye deploy`: an atomic swap of a host's files over ssh that recreates exactly the services
  whose configuration changed, and a fleet rollout with a canary first and the central stack
  last; `aye diff` shows beforehand what would change and `aye doctor` checks read-only why a
  deploy would fail.
- `aye host-init` (a deploy user that may run one helper on one directory), `aye permissions`
  (the narrowest external credentials the manifest needs), `aye dns` (the Cloudflare records
  of the ingress routes), `aye secrets` (pull, push, set, ask, gen and run against the secrets
  manager without printing a value, and `gen --env-file` for a local `.env` without one),
  `aye discover` (a read-only probe of a Linux host and the manifest entry it suggests),
  `aye portal`, `aye status` and `aye retire-rules`.
- Declared backup guarantees: a service's [`data`](docs/manifest.md#data) names its engine,
  the dump job, `rpo`, `rto` and the `copies` the dump must exist in, and the
  [data rules](docs/rules.md#data-rules) hold the manifest to it: `data-backup`, `backup-rpo`
  (the job's gap plus grace and the daily off-site copy fit into the `rpo`), `backup-rto` (a
  restore drill fails beyond the `rto`), `backup-placement` (the off-site copy holds the dump;
  a critical service keeps two copies in different zones, one append-only) and the warning
  `data-declared` for a critical service on a database without `data`. With an `rpo`,
  `backup-missing` and `offsite-stale` fire at the `rpo` when it is tighter
  (`ayeops_backup_rpo_seconds`, `ayeops_offsite_max_age_seconds`), and the Reliability
  dashboard shows the last good dump next to its `rpo`. `offsite-stale` now reads its threshold
  from the agent and falls back to 26 hours.
- `aye inventory`: an [Ansible inventory](docs/inventory.md) of the manifest's hosts, grouped
  by zone, group, profile, module and service, as YAML or JSON, with `--check` for a committed
  copy; AyeOps itself runs no Ansible.
- [Checks](docs/checks.md): HTTP, TCP, DNS, sandboxed scripts and read-only SQL queries
  declared under a service, each with its metric and alert.
- [Flow baselines](docs/modules.md#flow-baselines): an alert that compares each series of a
  flow with its own level at the same hour of the week over four weeks.
- Local modules through `module_paths`, held to the same rules as the built-in ones.
- Grafana dashboards and alert messages in English or Ukrainian.
- One runbook page per built-in alert in [docs/runbooks](docs/runbooks/), shipped in the wheel
  so an installed `aye` serves their text.

### Added: monitoring modules (stable)

- `metrics`: VictoriaMetrics single-node and vmagent, with remote_write from agent hosts.
- `logs`: Loki and Alloy for the containers of the declared compose projects and journald.
- `node`: node-exporter with textfile collectors for battery, AC power and a pending reboot.
- `containers`: cAdvisor.
- `smart`: SMART health, bad sectors, NVMe wear and drive temperature.
- `gpu`: NVIDIA GPU temperature, memory and driver health.
- `network`: link state, speed, errors and Wi-Fi signal of the declared interfaces.
- `disk-forecast`: an alert when a filesystem fills up within 7 days.
- `probes`: blackbox-exporter for HTTP, TLS expiry and the host's own internet, and RDAP for
  domain expiry.
- `checks`: the runners of the declared checks.
- `grafana`: Grafana with the datasources, dashboards and alert rules of the enabled modules,
  the fleet Health, Services and Reliability dashboards and a Telegram contact point.

### Added: operations modules (stable)

- `hygiene`: a nightly prune of stopped containers, unused images and build cache.
- `image-updates`: Diun over containers on a floating tag.
- `updates`: self-hosted Renovate over your repositories, with open and overdue updates as
  metrics.
- `sentinel`: a watcher on another host that sends one Telegram message when the central host
  stops answering, and a dead-man ping while all is well.
- `ingress`: Traefik with HTTPS names for internal services and a wildcard certificate.
- `auth`: oauth2-proxy sign-in in front of every ingress route.
- `secrets`: a self-hosted secrets manager (Infisical by default) with its database and a daily
  dump.
- `errors`: self-hosted error tracking for the Sentry SDKs (GlitchTip by default).
- `portal`: two static pages from the manifest, where things run and how to reach them, with
  live status read through a read-only metrics proxy.
- `cloudflare`, `github`, `delivery` and `ai-usage`: edge traffic and free-plan limits,
  Actions minutes, delivery and quality metrics from `aye report`, and the spend and limits of
  AI providers.

### Added: preview modules

- `autopilot` (preview): level 1 in **shadow mode** only. For each critical alert it collects a
  dossier and asks Claude for up to three ranked hypotheses under hard spend caps, journals
  every run and measures its hit rate. Secrets in logs, labels and the environment are masked
  before the dossier leaves the host. It runs no action and messages nobody by default
  ([autopilot](docs/autopilot.md)).
- `reconciler` (preview): starts again a declared container that Docker left down, with a
  back-off and an alert when it gives up. It is the one module that changes a host on its own.
- `oncall` (preview): GoAlert escalation for critical alerts with a Telegram acknowledge button
  and quiet hours.
- `incidents` (preview): the incident journal from Grafana's alert state history and the
  `Incidents` dashboard, with requests lost behind the ingress and, with `oncall`, who
  acknowledged each paged incident in GoAlert and when; downtime in money is next on the
  [roadmap](docs/roadmap.md#failover-by-priority).
- `status` (preview): the reporter that pushes signed component states to the status page
  Worker.
- `mcp` (preview): an MCP hub on agentgateway with read-only tools; signing MCP clients in
  through the ingress is still on the [roadmap](docs/roadmap.md#mcp-hub).
- `vulns` (preview): a daily Trivy scan of every image the host runs.
- `eol` (preview): the days until end of support and of security support of the cycles the
  host's image tags carry, its OS and listed components, from endoflife.date once a day.
- `backups` (preview): the metrics contract of your own dump jobs and of the built-in dumps of
  `secrets` and `errors`, with alerts on a failed and on a missing run, the threshold taken from
  the job's cron schedule; a job with a `command` becomes a systemd timer that `aye deploy`
  installs.
- `objectstore` and `offsite` (preview): restic copies of a host's newest backup to Garage or
  an append-only rest-server on another host, with retention, a weekly restore test and an
  optional restore drill.

### Added: outside the kit

- The status page Worker in `cloud/status` (preview): a multi-tenant Cloudflare Worker with a
  D1 database, a signed push contract, notices and an admin API that must sit behind
  Cloudflare Access ([status page](docs/status.md)), in English (the default), Ukrainian and
  Russian.
- `aye report` for CI pipelines: a GitHub action, a GitLab include and a single-file script
  in `ci/`.

### Known limitations

- Linux hosts only; `aye discover` probes Linux, not macOS or Windows.
- Docker compose only; Kubernetes and Helm are on the roadmap.
- Python 3.13 or newer for `aye` itself.
