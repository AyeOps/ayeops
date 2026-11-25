# Manifest reference

A manifest is a YAML file. The JSON Schema lives in
[`src/ayeops/schema/manifest.schema.json`](../src/ayeops/schema/manifest.schema.json); module
settings are validated by the schema of each module. [`examples/acme.yaml`](../examples/acme.yaml)
uses every section; [`examples/quickstart.yaml`](../examples/quickstart.yaml) is the smallest
manifest that runs, one host on a laptop (see the [quick start](../README.md#quick-start)).

Names (project, hosts, services, alerts) match `^[a-z][a-z0-9-]{0,39}$`.

## Top level

| Field | Required | Meaning |
|---|---|---|
| `version` | yes | always `1` |
| `ayeops` | no | the AyeOps releases this manifest is written for, a [PEP 440](https://peps.python.org/pep-0440/#version-specifiers) specifier such as `">=0.2,<0.3"` or `"==0.2.0"` (quote it in YAML); every command that checks the manifest fails first with rule `ayeops-version` when the running AyeOps (`aye --version`) is outside it, see [pinning a release](#pinning-a-release) |
| `project` | yes | prefix of every compose project name: `<project>-<host>` |
| `runbook_base_url` | when any alert exists | base of `runbook_url` in alert rules: `<base>/<runbook>` |
| `timezone` | no | IANA time zone of the fleet, e.g. `Europe/Kyiv`, default `UTC`: the zone of night and quiet hours (`incidents`, `oncall`, whose incident topics also show their times in it) and of daily schedules (`updates`, `image-updates`, `vulns`, `eol`, `offsite`) wherever a module does not set its own `timezone`; an unknown zone fails rule `timezone`. `incidents`, `oncall` and `autopilot` read the zone from the host's `/usr/share/zoneinfo`, mounted read-only; on a host without it (Alpine without `tzdata`) `UTC` still works and any other zone stops the agent at its start |
| `module_paths` | no | directories with your own modules, relative to the manifest, see [modules](modules.md#write-your-own-module) |
| `rules.waivers_need_until` | no | `true` makes every [waiver](#waivers) name its end with `until`, default `false` |
| `alerting.contact` | no | `telegram` (default), `email` (needs `mail` and `alerting.emails`) or `none` |
| `alerting.emails` | with contact `email` | addresses the `email` contact point sends alerts to |
| `mail` | no | the SMTP relay for outgoing mail, see [mail](#mail) |
| `status_page` | for `aye status` | `{hostname, title, locale, notify, access}` of the public [status page](status.md); `title` defaults to `project`, `locale` (`uk`, `en`, `ru`) to `en`; `notify: {telegram_chat_id, min_state}` turns on the Worker's [Telegram notices](status.md#notices); `access: {team, aud}` (the Zero Trust team name and the AUD tag of the Access application) opens the [admin API](status.md#admin), which refuses every request without it |
| `brand` | no | logo, favicon and accent of the [portal](modules.md#portal), see [brand](#brand) |
| `portal.locale` | no | the language the [portal](modules.md#portal) opens in, `en` (default) or `uk`; both are on every page and the switcher in the header remembers the reader's choice in the browser |
| `hosts` | yes | map of host name to host |
| `profiles` | no | map of profile name to a partial host that hosts start from, see [profiles](#profiles) |
| `services` | no | the catalog of your own services, map of name to service |
| `externals` | no | services outside the fleet that others depend on or that call a service: `{<name>: {title, url, icon}}`, referenced as `external:<name>`; one nobody references warns with `external-used` |
| `flows` | no | data flows of the project for the [portal](modules.md#portal), see below |
| `duties` | no | recurring work only a person can do, with the day it was last done; one alert says when one is overdue, see [duties](#duties) |

## Host

| Field | Required | Meaning |
|---|---|---|
| `arch` | yes, here or in the profile | `amd64` or `arm64`; rendered as `platform: linux/<arch>` on every service |
| `profile` | no | the [profile](#profiles) the host starts from |
| `group` | no | the `group` label of the host's metrics and logs, e.g. `workers`, so alerts and dashboards can group hosts; defaults to the host's `profile`, without either the host has no `group` label |
| `address` | for a remote_write target or ingress | address other hosts use to reach this one, e.g. a VPN address; an IPv4 address with ingress, since its names are A records |
| `public_bind` | when a module exposes a port, and with ingress | address that exposed ports bind to; there is no default |
| `memory` | no | memory of the host, e.g. `12g`; the `mem_limit` of all services plus `reserved` must fit into `memory_budget_pct` of it |
| `memory_budget_pct` | no | share of `memory` the budget may use, 1 to 100, default `85`; the rest is for the kernel, the page cache and whatever nobody declared |
| `reserved` | no | memory taken by software outside containers, e.g. `{php-fpm: 40g, mysql: 120g}`; counts towards the budget |
| `zone` | no | where the host stands, e.g. `office`; the portal groups hosts by `zone`, else by `provider`, else one zone per host; a host's sentinel and its off-site copies must stand in another zone (rules `sentinel-watch`, `offsite-target`) |
| `provider` | no | who runs the machine, e.g. a cloud; shown above the zone |
| `icon` | no | portal icon of the host, e.g. `ubuntu`; see [icons](#icons) |
| `connect` | no | how `aye deploy`, `diff` and `doctor` reach the host, so their `--ssh`, `--sudo`, `--helper` and `--dir` need not be typed; see [connect](#connect) |
| `units` | no | services that run as systemd units on this host, as `<service>: <unit>` (`.service`, `.timer`, `.socket` or `.path`); they count as provided, like a cron backup or a host check that no container runs |
| `compose` | no | your own compose files on this host, relative to the manifest; services defined there count as provided. An entry is a path or `{file: <path>, name: <project>}`: `name` is the compose project name, shown as the stack on the portal and used to match its containers |
| `modules` | yes, here or in the profile | map of module name to its settings (`{}` or an empty value for the defaults, `false` for off, see [profiles](#profiles)) |

Every module accepts two common settings:

| Setting | Meaning |
|---|---|
| `expose` | container ports to publish on `public_bind`; all other ports bind to `127.0.0.1` |
| `resources` | per service overrides: `{<service>: {memory: 512m, cpus: 0.5}}` |

The remaining settings are module specific, see [modules](modules.md).

### Connect

```yaml
hosts:
  central:
    arch: arm64
    connect: {ssh: ops-central, sudo: true}
    modules: {}
```

| Field | Default | Meaning |
|---|---|---|
| `ssh` | the host name | the ssh destination, anything `ssh` accepts: an alias of `~/.ssh/config` or `user@address`; it may not start with `-` or hold a space |
| `sudo` | `false` | run the remote side under `sudo sh -c`, as `--sudo` does |
| `helper` | `false` | run each step through the `aye-host` helper of [host init](host-init.md), as `--helper` does; not together with `sudo` |
| `dir` | `/opt/ayeops/<host>` | the remote directory, an absolute path, as `--dir` |

A flag on the command line wins over the field: `--ssh` and `--dir` replace `ssh` and `dir`,
and `--sudo` or `--helper` replaces the whole mode, so `--helper` on a host with `sudo: true`
runs through the helper only. A fleet takes each host's own `connect`, which `--ssh` cannot do
for more than one host. A [profile](#profiles) may carry `connect` for all its hosts, e.g.
`{helper: true}`, and each host adds its `ssh`.

## Pinning a release

A manifest that lives in another repository is rendered by whatever `aye` that repository's CI
or laptop happens to have installed. `ayeops` states which releases it was written for:

```yaml
version: 1
ayeops: ">=0.2,<0.3"
project: acme
```

`aye check`, and with it `render`, `deploy`, `dns`, `status`, `host-init`, `permissions` and
`portal`, then fails before anything else when the running AyeOps is outside the range:

```text
error [ayeops-version] ayeops: the manifest needs AyeOps >=0.2,<0.3, but AyeOps 0.1.0 is running
```

Only this one finding is printed: a manifest written for another release would otherwise show
schema errors that are not the real problem. A malformed specifier (a bare `0.2.0`, `latest`)
fails the same rule. Install the matching release with
`uv tool install git+https://github.com/AyeOps/ayeops@v<version>`, see
[releasing](releasing.md). While AyeOps is `0.x`, a minor release may change the manifest, so
pin the minor line (`>=0.2,<0.3`), or one exact release (`==0.2.0`) when CI should render with
exactly the version it was tested with. A prerelease inside the range counts as inside it.

## Profiles

A fleet of near-identical hosts writes the shared part once, under `profiles`, and each host
names the profile it starts from:

```yaml
profiles:
  worker:
    arch: amd64
    zone: datacenter
    modules:
      metrics:
        role: agent
        remote_write: {host: central}
      node: {}
      smart: {}
hosts:
  worker-1:
    profile: worker
    address: 10.8.0.11
  worker-2:
    profile: worker
    address: 10.8.0.12
    modules:
      metrics:
        scrape_interval: 60s
      smart: false
```

A profile takes every host field except `profile`, and none is required. The host's own fields
are merged over its profile when the manifest loads: maps merge key by key at every depth with
the host winning, a list or a scalar of the host replaces the profile's whole. `worker-2` above
keeps `role` and `remote_write` from the profile and adds `scrape_interval`. An empty value
(`smart:`) keeps the profile's settings, as it means the defaults without a profile.

`false` turns a module off: `smart: false` on a host drops the `smart` the profile enables. It
works on a host without a profile too and names a known module (`module-known`). Profiles do
not nest.

Every check, render and portal page sees the merged host, so findings point at
`hosts.<host>...`. The schema checks each field where it is written, under `profiles` or
`hosts`, and each merged host for its required fields (`arch`, `modules`). A host naming a
profile that does not exist fails with `profile-known`, a profile no host uses warns
with `profile-used`. The text hygiene rule reads the manifest as written, so a character in a
profile is reported once, under `profiles`.

A host's `group` defaults to its profile name, so the workers above carry `group="worker"` on
their metrics and logs; set `group` on the profile or the host to name the group otherwise.

## Service

| Field | Rule | Meaning |
|---|---|---|
| `host` | required | the host that runs it |
| `state` | no | `live` (default), `planned` or `disabled`, see [reserved services](#reserved-services) |
| `criticality` | no | `critical`, `high`, `normal` or `low`: how much a minute of its downtime weighs, 100, 10, 1 and 0.1 on the [`Incidents` dashboard](modules.md#incidents); without it a service with a public [status mark](#status-mark) counts as `high`, any other as `normal` |
| `data` | `data-backup`, `backup-rpo`, `backup-rto`, `backup-placement` | the data the service keeps and what its backups promise: engine, dump job, `rpo`, `rto`, copies, see [data](#data) |
| `description` | `service-description` | one sentence on what it does for whom; a [text](#text-in-several-languages) |
| `metrics` | `service-metrics` | list of metric names, or `{none: <reason>}`; the reason is a [waiver](#waivers) and may be `{reason, until}` |
| `alerts` | `service-alerts` | at least one alert, see below |
| `logs` | `service-logs` | `docker`, `journald`, `syslog` or `file:<path>` |
| `status` | `service-status` | `{public: <status page component>}` or `{internal: <reason>}`; a public mark may add `id` and `probe`, see below; the internal reason is a [waiver](#waivers) |
| `cloud` | `cloud-limit` | cloud dependencies: `{name, limit, on_exceed}` each |
| `depends_on` | `depends-known`, `depends-cycle` | what it needs to work: `<host>/<service>`, a service name unique across hosts, a cloud dependency name or `external:<name>` |
| `url` | no | the address people open, e.g. a public website; the portal adds ingress routes by itself |
| `login` | no | how to sign in, shown on the portal card; name where the credential lives, never the credential |
| `clients` | `client-endpoint` | who calls it from outside, e.g. `[external:ai-assistants]`; same endpoint forms as `depends_on`, drawn as incoming edges on the Data flows diagram |
| `icon` | `icon-known` | portal icon, e.g. `postgresql` or `mcp`; without it the portal guesses from the service name, its image and its `url`; see [icons](#icons) |

A service must be
 provided by a module enabled on its host or by one of the host's `compose`
files, or declared in its host's `units` (`service-provided`).

### Data

A service that keeps data declares it, and what its backups promise, in `data`. The rest of the
manifest already says when a dump runs ([backups](modules.md#backups)) and where it is copied
([offsite](modules.md#offsite)); `data` says what that has to add up to, and `aye check` holds the
jobs to it.

```yaml
services:
  acme-db:
    host: edge
    criticality: high
    data:
      engine: postgres
      backup: acme-db
      rpo: 26h
      rto: 4h
      copies: [host, offsite]
```

| Field | Required | Meaning |
|---|---|---|
| `engine` | yes | `postgres`, `mysql`, `mariadb`, `mongo`, `sqlite`, `redis` or `files`: what the dump holds |
| `backup` | yes | the [backups](modules.md#backups) job on the service's `host` that dumps it, one of its own `jobs` or a built-in dump such as `secrets` (rule `data-backup`) |
| `rpo` | no | the most data the service may lose, as minutes, hours or days (`30m`, `26h`, `2d`): the newest restore point is never older than this while the backups are healthy (rule `backup-rpo`) |
| `rto` | no | how long a restore may take; documentation, except that a [restore drill](modules.md#restore-drill) of its off-site copy must fail when it takes longer (rule `backup-rto`) |
| `copies` | no | where the dump must exist: `host` (the dump file on the service's host) and `offsite` (the off-site copy of that host), default `[host]` (rule `backup-placement`) |

`data` sits on the service rather than in a top-level `datastores:` map, because what a data
store needs is already on the service: its `host` (where the job and the copies are looked up)
and its `criticality` (which decides how strict the placement rules are). A service with two
databases is two services in the catalog.

With an `rpo`, the thresholds follow it, the tighter one wins: `backup-missing` fires when the
job is older than its schedule's gap plus grace or the `rpo`, whichever is shorter, and
`offsite-stale` of the host fires after 26 hours or the `rpo` of a service with `offsite` in
`copies`. Since `backup-rpo` already refuses a job whose gap plus grace exceeds the `rpo`, in a
manifest that passes only `offsite-stale` moves: a daily copy and an `rpo` of 25 hours fire it
after 25 hours instead of 26. The [Reliability dashboard](modules.md#backups) shows the age of
the last good dump next to the `rpo`.

A `criticality: critical` service with `data` needs two copies in different zones, one of them
with another provider when the manifest names them, and one that is append-only; see
[`backup-placement`](rules.md#data-rules). A critical service that runs on a database image or
unit and declares no `data` gets the warning `data-declared`.

### Reserved services

A service with `state: planned` (not built yet) or `state: disabled` (switched off) keeps its
place on the portal: every diagram draws it dashed and muted with a badge, and the Services page
lists it under its own chip. aye does not deploy it (a module service of that name is left out
of the host's compose project), scrape it (its targets drop out of the scrape config), alert on
it (no rules from its `alerts` or `checks`) or put it on the status page. Only `description` is
required; `service-provided`, `service-metrics`, `service-alerts`, `service-logs` and
`service-status` do not apply.

### Text in several languages

`description`, the status mark titles and the flow `label` take a string or a map by language:

```yaml
description:
  en: Telegram bot that sends the finds
  uk: Telegram-бот, що надсилає знахідки
```

The portal shows the reader's language and falls back to English, then to whatever the map
holds. The status page uses the map entry of `status_page.locale`, and the component id is a slug
of the English title. An empty entry counts as missing.

A text value starting with `TODO` counts as missing for every rule above, so the stubs that
[`aye discover --suggest`](discover.md) writes keep failing `aye check` until a person fills them.
[`aye discover --adopt`](discover.md#adopting-a-host) writes dated waivers instead, which pass
with a warning until their `until`.

### Waivers

A reason written instead of the real thing is a waiver: `metrics: {none: <reason>}`,
`status: {internal: <reason>}`, the `reason` of a baseline `skip` and the ingress
`without_auth`. Each takes the plain string, which never ends, or the reason with the last day
it holds:

```yaml
metrics:
  none:
    reason: the vendor ships an exporter in the next release
    until: 2027-03-31
status:
  internal: {reason: {en: only the finance team uses it, uk: лише для фінансистів}, until: 2027-06-30}
```

| Field | Meaning |
|---|---|
| `reason` | required, the same text the plain form holds; a [text](#text-in-several-languages) for `internal`, a string elsewhere |
| `until` | the last day the waiver holds, `YYYY-MM-DD`; `aye check` warns from fourteen days before and fails from the day after, see [waivers end](rules.md#waivers-end) |

A baseline `skip` already is a map, so `until` sits next to its `reason`:
`{match: {category: auctions}, reason: ..., until: 2027-01-31}`.

## Status mark

| Field | Meaning |
|---|---|
| `public` | title of the status page component; services with the same title share one component |
| `id` | component id, `^[a-z][a-z0-9-]{0,39}$`; defaults to a slug of the title, required when the title has no Latin letters |
| `probe` | an `https://` URL the status Worker fetches every minute from outside; without it the component waits for pushes from the stack, and silence shows as no data |

Only component ids and titles reach the page; service names, hosts and reasons stay in the
manifest. `aye status <manifest>` prints the components as JSON, `--sql` as a D1 seed.

## Alert

| Field | Required | Meaning |
|---|---|---|
| `name` | yes | unique across modules and services; becomes the rule uid `aye-<name>` |
| `expr` | one of | MetricsQL; it must evaluate to `1` while firing and `0` otherwise, so write comparisons with `bool` |
| `baseline` | one of | instead of `expr`, compare each series of a flow with its usual level for the hour of the week, see [flow baselines](modules.md#flow-baselines) |
| `severity` | rule | `critical`, `warning` or `info` |
| `runbook` | rule | a slug appended to `runbook_base_url`, or an absolute URL |
| `title` | no | rule title, defaults to the name |
| `summary` | no | annotation; Grafana templating such as `{{ $labels.host }}` works |
| `for` | no | pending period, default `5m` |
| `dashboard` | no | the dashboard the alert links to: `<uid>`, `{uid: <uid>, panel: <id>}`, or `false` for none; by default the panel that draws the alert's metrics, see [modules](modules.md#modules) |

Rules evaluate once a minute. The `bool` convention keeps one threshold (`> 0`) for every rule
and makes `0` an explicit "fine" instead of silence. Silence is therefore a fault: when the
query of a service alert returns no series at all (container gone, exporter down) or fails, the
rule fires (`noDataState: Alerting`, `execErrState: Alerting`). Module alerts decide per alert,
see [modules](modules.md).

## Flows

`flows` lists the data flows the manifest cannot derive, for the Data flows diagram of the
portal:

```yaml
flows:
  - from: central/grafana
    to: external:telegram-api
    label: alert messages
    state: live
  - from: web
    to: central/victoriametrics
    label: request metrics
    state: planned
```

| Field | Required | Meaning |
|---|---|---|
| `from`, `to` | yes | `<host>/<service>`, a service name unique across hosts, a probed URL, a cloud dependency name or `external:<name>` (`flow-endpoint`) |
| `label` | no | text on the edge; a [text](#text-in-several-languages) |
| `state` | no | `live` (default): dots move along it; `planned`: dashed; `disabled`: dashed and faded |

## Duties

Some duties cannot be automated away: an access review every quarter, a restore done by hand,
rotating a credential, renewing a contract, calling the on-call phone to see that it rings.
They live in someone's calendar and lapse without a sound. `duties` lists them with the day
each was last done, and the alert `duty-overdue` says when one is late:

```yaml
duties:
  access-review:
    title: Review who can sign in to Grafana, the portal and the hosts
    every: 90d
    last_done: 2026-10-01
    owner: ops
    runbook: access-review
  token-rotation:
    title: Rotate the Cloudflare and Telegram API tokens
    every: 365d
    last_done: 2026-03-02
```

The key is the duty's name, `^[a-z][a-z0-9-]{0,39}$`, so two duties cannot share one. The word
is `duties` and not checks because `aye check` validates the manifest and a service's
[`checks`](checks.md) are probes a machine runs; a duty is done by a person.

| Field | Required | Meaning |
|---|---|---|
| `title` | yes | what to do, one line of up to 120 characters without `"`, `\` or `$`; the alert shows it |
| `every` | yes | how often, in days: `1d` to `3660d` (ten years, rule `duty-every`) |
| `last_done` | yes | the day it was last done, `YYYY-MM-DD`, quoted or not; a day that is not real or that lies after today in the manifest's `timezone` fails rule `duty-done` |
| `owner` | no | who does it, free text of up to 80 characters, e.g. a team or a person; the alert shows it |
| `runbook` | no | how to do it: a slug appended to `runbook_base_url`, or an absolute URL; the alert links it instead of the [duty-overdue](runbooks/duty-overdue.md) page |

A duty is due on `last_done` plus `every` and overdue from the first moment of the next day in
the manifest's `timezone`. The due moment is written into the alert rule as a Unix time when the
manifest is rendered, and the rule compares it with `time()` at each evaluation, so the render
depends on the manifest alone and stays the same from one day to the next. The rule, one for all
duties in the rule group `duties` on the Grafana host, returns one series per duty with the
labels `duty`, `title`, `due`, `every` and, when set, `owner` and `runbook`; it is `1` from the
overdue moment on and `0` before, and fires (severity `warning`) after an hour. Each duty is
its own notification, through the `scope` label. Nothing is scraped or stored: the duties exist
in the manifest and in the rule.

Doing the duty is recorded by editing `last_done` and committing the manifest, so the registry
is reviewed like any other change and its history is the git log; the alert resolves once the
render with the new day is deployed. `aye duties` lists every duty with its due day and the days
left, earliest due day first, and exits 1 when one is overdue, so a scheduled pipeline can nag too:

```text
$ aye duties --today 2026-12-31
access-review: overdue, was due on 2026-12-30, 1 day ago (every 90d, owner ops): Review who can sign in to Grafana, the portal and the hosts
token-rotation: due on 2027-03-02, in 61 days (every 365d): Rotate the Cloudflare and Telegram API tokens
ayeops.yaml: 1 of 2 duty(s) overdue
```

## Mail

`mail` names one SMTP relay that every module sending mail uses: Grafana for alert e-mails and
invitations, Infisical for sign-up codes and invitations. Any relay works, e.g. Brevo, Amazon SES,
Postmark, Mailgun or your own Postfix. Mail is optional: without `mail` nothing SMTP is rendered
and no module asks for it.

```yaml
mail:
  host: smtp.mail.example
  port: 587
  from: ops@acme.example
  from_name: Acme Ops
```

| Field | Default | Meaning |
|---|---|---|
| `host` | required | relay host name |
| `port` | `587` | relay port |
| `from` | required | sender address; its domain must be a real domain the relay may send for (SPF, DKIM) |
| `from_name` | `project` | sender display name |
| `tls` | `starttls` | `starttls`: upgrade on a submission port such as 587 and refuse to send without it; `tls`: TLS from the first byte, port 465; `none`: plain, for a relay on a trusted network |

The login comes only from `AYE_SMTP_USER` and `AYE_SMTP_PASSWORD`. Rules: `mail-from` (the
domain of `from` does not look like a domain), `mail-tls` (`tls: tls` without port 465, or
`starttls`/`none` on 465), `mail-contact` (contact `email` without `mail` or `emails`, or
`emails` with another contact).

## Environment variables

Rendered compose projects read these at `docker compose up`; [`.env.example`](../.env.example)
lists them and every rendered host gets its own `.env.example` with the subset it needs.

| Variable | Used by | Meaning |
|---|---|---|
| `AYE_GRAFANA_ADMIN_USER` | grafana | admin login |
| `AYE_GRAFANA_ADMIN_PASSWORD` | grafana | admin password |
| each `password_env` | grafana `datasources` | password of that datasource; the example names it `AYE_GRAFANA_PG_PASSWORD` |
| `AYE_TELEGRAM_BOT_TOKEN` | grafana, contact `telegram`; sentinel | bot token from BotFather |
| `AYE_TELEGRAM_CHAT_ID` | grafana, contact `telegram`; sentinel | chat that receives alerts |
| `AYE_SENTINEL_PING_URL` | sentinel, optional | dead-man URL pinged after every round where all checks passed, e.g. `https://hc-ping.com/<uuid>`; empty turns the ping off |
| `AYE_SMTP_USER` | grafana, secrets, with `mail` | SMTP login of the relay |
| `AYE_SMTP_PASSWORD` | grafana, secrets, with `mail` | SMTP password or API key of the relay |
| `AYE_AUTH_CLIENT_ID` | auth, grafana with auth | OAuth client id of the Google or OIDC client |
| `AYE_AUTH_CLIENT_SECRET` | auth, grafana with auth | OAuth client secret |
| `AYE_AUTH_COOKIE_SECRET` | auth | 32 random bytes in URL-safe base64 that sign the session cookie |
| `AYE_CLOUDFLARE_DNS_API_TOKEN` | ingress, `aye dns` | Cloudflare API token with `Zone:Read` and `DNS:Edit` on the zone; Traefik gets it as `CF_DNS_API_TOKEN` |
| `AYE_CLOUDFLARE_ANALYTICS_TOKEN` | cloudflare | read-only Cloudflare API token: `Zone:Read` and `Analytics:Read` on the zones, `Account Analytics:Read` on the accounts |
| `AYE_GITHUB_TOKEN` | github | GitHub token that reads the billing usage (`read:org`, `user` for a user account) and the report artifacts (`repo` for private repositories) |

Module containers also receive variables set by the renderer, documented in [modules](modules.md).

## Brand

```yaml
brand:
  name: Acme
  logo: brand/acme.svg
  logo_dark: brand/acme-dark.svg
  favicon: brand/ops.svg
  color: "#ac8aff"
```

Every field is optional; paths are relative to the manifest and must be `.svg`, `.png`, `.webp`
or `.ico` (rule `brand-file`). `aye render` and `aye portal` inline the files into the pages as
`data:` URIs, so the portal still makes no external request.

| Field | Meaning |
|---|---|
| `name` | the name in the header and the tab title; defaults to `project` |
| `logo` | shown 28 px high before the name; without it the header keeps a dot |
| `logo_dark` | the logo for the dark theme; without it `logo` serves both |
| `favicon` | the tab icon as is |
| `color` | `#rrggbb` accent of the generated favicon; defaults to `#ac8aff` |

Without `favicon` the portal generates one that tells its tab apart from the product's own
site: the logo (`logo_dark` first, else `logo`, else the first letter of `name`) on a dark
rounded tile with a `color` ring and an «ops» badge. The pages also set `theme-color` to the
page background of each theme.

## Icons

The portal draws a product icon next to every service, host and external. `icon` sets it; the
value is one of the vendored [Simple Icons](https://github.com/simple-icons/simple-icons)
16.35.0 slugs (CC0 1.0, see `NOTICE`) or a neutral glyph:

| Kind | Values |
|---|---|
| products | `anthropic`, `caddy`, `claude`, `cloudflare`, `digitalocean`, `discord`, `django`, `docker`, `fastapi`, `flask`, `fluentbit`, `gitea`, `github`, `githubactions`, `go`, `googlecloud`, `grafana`, `helm`, `hetzner`, `homeassistant`, `kubernetes`, `linux`, `mariadb`, `minio`, `modelcontextprotocol` (alias `mcp`), `mysql`, `netdata`, `nextcloud`, `nginx`, `nodedotjs`, `ntfy`, `nvidia`, `ollama`, `ovh`, `pihole`, `portainer`, `postgresql`, `prometheus`, `proxmox`, `python`, `rabbitmq`, `raspberrypi`, `redis`, `renovate`, `sentry`, `sqlite`, `syncthing`, `tailscale`, `telegram`, `traefikproxy`, `ubuntu`, `uptimekuma`, `vaultwarden`, `victoriametrics`, `watchtower`, `wireguard` |
| glyphs | `service`, `host`, `cloud`, `link` |

Without `icon` the portal guesses from the service name, the image repository and the `url`
(`postgres:18` is `postgresql`, `prom/node-exporter` is `prometheus`, `grafana/loki` is
`grafana`, a name or URL with `mcp` is `modelcontextprotocol`), then from the `command` and
`entrypoint` of a compose service (`python -m app` is `python`), a host from its `provider` (a
known cloud draws `cloud`), an external from its name, title and URL. Anything else gets a
letter monogram of its name instead of an icon. Logos are drawn in their brand colour. Oracle,
AWS, Azure, Valkey, Loki, Alloy and cAdvisor have no icon of their own in Simple Icons, and
Debian, Fedora, PHP, Rust, OpenTelemetry, Forgejo and Let's Encrypt are not CC0 there. A service with the MCP icon is labelled «MCP» on its card and on
the diagrams.
