# Roadmap

What ships today is in the [module table](../README.md#modules) and in the
[changelog](../CHANGELOG.md). Each section below says in a line what of it ships, then what comes
next, roughly in order.

## Backups monitoring: silence is not success

Ships: [`backups`](modules.md#backups) declares each dump job with its cron schedule, a wrapper
writes start, end, success, size and duration of every run, and `backup-failed` and
`backup-missing` fire on a failed run and on a run that did not succeed within the longest gap
of the schedule plus a grace. A job with a `command` becomes a systemd timer that `aye deploy`
installs, so the schedule is written once, and the built-in dumps of `secrets` and `errors`
report through the same wrapper. [`offsite`](modules.md#offsite) copies a host's newest dump
with restic to an [`objectstore`](modules.md#objectstore) on another host, applies the
retention, tests a restore every week and alerts on a stale copy, a failed restore test and a
full bucket; with `provider: rest-server` the store is append-only, so a taken-over host cannot
delete its own copies.

A service's [`data`](manifest.md#data) declares the guarantees the jobs must add up to:
`aye check` holds the dump schedule and the daily off-site copy to its `rpo`, a critical
service's copies to two zones and one append-only store, and a restore drill to its `rto`; the
Reliability dashboard shows the age of the last good dump next to the `rpo`.

Next:

- A dump job for the databases the manifest already knows (the Grafana datasources), so a host
  gets its dump without writing the `pg_dump` line.
- The age of the newest off-site copy next to the `rpo` (the copy's age is measured, the age of
  the dump inside it at copy time is not yet).
- `data.copies` beyond one off-site target per host, and an `rto` checked against the measured
  drill duration rather than its time limit.

## Status page

Ships: the multi-tenant Cloudflare Worker in [`cloud/status`](../cloud/status), with its D1
schema, the signed push contract and an admin API behind Cloudflare Access
([status.md](status.md)), fed from the `status.public` marks of the catalog, and the
[`status`](modules.md#status) module, the reporter that turns the stack's alerts and maintenance
silences into signed pushes.

Next: the Worker, its D1 database and DNS from the manifest with OpenTofu, see
[packaging](#packaging-and-infrastructure).

## MCP hub

Ships: the [`mcp`](modules.md#mcp) module renders agentgateway as the hub, with read-only tools
from Grafana, VictoriaMetrics and S3 stores, the `catalog` source (hosts, services, alerts and
runbooks, rendered from the manifest), other MCP servers and OpenAPI specs, reached over ssh on
the host's loopback port; a tool that writes runs only from `allow_write`.

Next, from the [design](mcp.md): the `cli` adapter, MCP servers from an image, the ContextForge
and Docker providers, per-identity rules for the autopilot, the autopilot's tool list on the
`catalog_*` tools, and recent deploys in the catalog.

MCP clients through ingress: today oauth2-proxy signs people in with a browser cookie, which
Claude Code and other MCP clients do not do, so they use the loopback port over ssh. The
follow-up signs them in with the same OIDC provider as [auth](modules.md#auth): agentgateway's
`mcpAuthentication` as an OAuth protected resource (it serves the metadata MCP clients use to
find the provider and checks the JWT itself), the hub's route moved to `auth.open` with
`mcp-open` relaxed for exactly that case, and per-person or per-group CEL rules on `jwt`
claims; or oauth2-proxy accepting bearer JWTs from the provider in front of an unchanged hub.

## Autopilot, levels 0 to 3

| Level | The kit may |
|---|---|
| 0 | observe and suggest; a person does everything |
| 1 | run the read-only diagnostics of a runbook and attach the output to the alert |
| 2 | run actions a runbook marks as safe (restart a container, prune build cache) after confirmation |
| 3 | run safe actions without confirmation, within limits per service |

Every action is written to a journal with the state before and after, so any step can be
reviewed and undone.

Before any level, the [reconciler](modules.md#reconciler) module on each host starts again a
declared container that Docker left down, with no model at all, and backs off to an alert: the
night a host reboots and one container stays down needs no diagnosis.

Ships: level 1 in shadow mode as the [`autopilot`](modules.md#autopilot) module: a Grafana
webhook for critical alerts, a dossier collected by plain code with secrets masked before it
leaves the host, up to three ranked hypotheses from the model under hard spend caps, and a
journal of every run with its time to first answer, tokens and cost. It runs no action and
messages nobody unless asked. After each incident it labels which hypothesis matched what fixed
it, from the reconciler, a host outage in the incident journal and a person's verdict, tells a
run a person acknowledged in GoAlert apart, and exports the hit rate per incident class and rank
to the `Incidents` dashboard.

Next: deploys during the incident as a further signal, the read-only MCP tools for a bounded
agent loop, and then level 2 for each incident class whose measured hit rate earns it. A
service that is down, as opposed to degraded, may then have its hypotheses tested one after
another with actions that can be undone, before the person on call is awake.

The goal, its metrics, the layers, the reversible envelope, the design of a run, the models,
the lockdown and how runs are paid: [autopilot.md](autopilot.md).

## Failover by priority

When a host is lost, its services come back on a standby host in the order of what their
downtime costs, and `aye check` proves beforehand that they fit there and that their data and
secrets survive the loss: [failover.md](failover.md).

Ships: the baseline. The [`incidents`](modules.md#incidents) module records every incident from
Grafana's alert state history and shows downtime weighted by service `criticality`, degraded
minutes, requests lost behind the ingress against a four-week baseline, time to restore and
night incidents, and with `oncall` who acknowledged each paged incident in GoAlert and the time
to acknowledge by criticality, day and night.

Next: downtime in money from `cost: {monthly_revenue}` or `cost: {value_query}`, then the
`priority`, `failover` and `state` fields of a service and the checks of the
[design](failover.md#what-aye-check-proves-in-advance).

## Packaging and infrastructure

Ships: docker compose per host, secrets from a self-hosted Infisical with `aye secrets pull` at
deploy time, or a local `.env` from `aye secrets gen --env-file`.

Next:

- Helm chart rendering the same modules for Kubernetes.
- OpenTofu for Cloudflare: DNS, tunnels and the status page Worker from the manifest.
- More providers of the [`secrets`](modules.md#providers) module: OpenBao or Vault, 1Password,
  SOPS with age.

## More signals

Ships: vulnerability monitoring of the images in the rendered compose projects as
[`vulns`](modules.md#vulns), with Trivy, and end of support of what runs as
[`eol`](modules.md#eol), from endoflife.date: the cycles the image tags carry, the host OS and
listed components.

Next:

- A row of the vulnerability findings and of the days left on `AyeOps / Hosts`, and images whose
  tag is only known on the host (`${TAG}`).
- The cycle of images whose tag says nothing (`redis:7`, `latest`) from the version inside the
  image, which Trivy's package list of [`vulns`](modules.md#vulns) knows.
- AMD GPUs in the `gpu` module, and XID errors of NVIDIA GPUs once the NVML image of the
  exporter is published for `arm64` and leaves its experimental status.
- `aye discover` for macOS and Windows hosts, and for podman containers.
