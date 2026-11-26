# Catalog rules

**A new service ships with a description, a metric and an alert.** `aye check` turns that
sentence into errors, and `aye render` refuses to render until they are fixed.

The reason is cheap to state and expensive to learn: a service nobody can describe, measure or
be woken up by fails silently, and silence looks exactly like success. Writing the entry costs
five minutes when the service is new; reconstructing it during an outage costs much more.

Every finding names its rule and the path in the manifest:

```text
error [service-alerts] services.web: service has no alert
```

A warning is printed the same way, with `warning` in front, and does not fail `aye check`,
`aye render` or `aye deploy`; `aye check --strict` fails on warnings too, for a pipeline that
wants a clean manifest. Warnings are for what breaks nothing at run time: leftovers, waivers
about to end and data nothing backs up yet.

## Unused entries

These warn and never fail a plain `aye check`: a module written before any host enables it,
or an external kept while its last client moves, is work in progress, not a broken fleet.

| Rule | Warns when |
|---|---|
| `profile-used` | an entry of `profiles` is the `profile` of no host |
| `external-used` | an entry of `externals` is named as `external:<name>` by no end of a flow, no `depends_on` and no `clients` entry |
| `module-used`, `runbook-used` | a local module is enabled on no host, or a page in its `runbooks/` is the runbook of none of its alerts and linked from none, see [modules](modules.md#write-your-own-module) |

## Service rules

| Rule | Requirement | Why |
|---|---|---|
| `service-description` | a non-empty `description` | the person on call needs to know what breaks for whom |
| `service-metrics` | at least one metric, or `metrics: {none: <reason>}` ([a waiver](#waivers-end)) | "no metrics" is allowed as a decision, never as an omission |
| `service-alerts` | at least one alert | a metric nobody watches is a log with extra steps |
| `alert-severity` | every alert has `severity` | routing and urgency depend on it |
| `alert-runbook` | every alert has `runbook`, and `runbook_base_url` is set | an alert without a next step trains people to ignore alerts |
| `service-logs` | a log source | the first question after an alert is "what did it say" |
| `service-status` | `public: <component>` or `internal: <reason>` ([a waiver](#waivers-end)) | decides up front whether users hear about an outage |
| `cloud-limit` | each cloud dependency has `limit` and `on_exceed` | free tiers and quotas end in throttling or bills; both should be known before they happen |
| `service-provided` | a module, a compose file or a declared unit on the service's host provides it | dead entries make the catalog lie |
| `alert-dashboard` | an alert's `dashboard` names a dashboard uid, and a panel id, that a module or project dashboard has | a link to a dashboard that does not exist sends whoever is paged to an empty page |
| `alert-unique` | alert names are unique across modules and services | the name is the rule uid |
| `baseline-skip`, `baseline-quiet`, `baseline-empty` | a [baseline alert](modules.md#flow-baselines) skips only on labels of its series (`by`, with `host` also `project` and `group`), has no quiet window that starts where it ends, and counts empty answers only on a counter; a skip is a [waiver](#waivers-end) | an opt-out on a label the rule never groups by, or an empty window, matches nothing and looks like it works |
| `alert-filter-nodata` | an alert whose no data counts as firing (every service alert, and a module alert with `no_data: alerting`) does not filter at the top level: `> 120` must be `> bool 120`, unless an `or` branch always returns a series | a filtering comparison returns no series while healthy, so with no data as firing the alert fires exactly when everything is fine; `bool` yields 0 or 1 and keeps the series |
| `status-component` | a public component has an id (a slug of its title, or an explicit `id`), and one id carries one title | the [status page](status.md) shows ids and titles only, so they must be stable and unambiguous |

The built-in modules are held to the same rules: every service a module can run has a catalog
entry with a description, metrics, alerts, a log source and a status mark, and every built-in
alert has a page under [runbooks](runbooks/). Tests enforce both, and the other direction too:
every page under runbooks is named after an alert of a built-in module (or the sentinel's
`sentinel-central-down`), or is one of the few operating pages of a module (`glitchtip`,
`infisical`, `oauth2-proxy`, `offsite`, `offsite-append-only`, `flow-baseline`), listed in
`tests/test_unused.py` and linked from [modules](modules.md).

## YAML rules

Every command that reads the manifest reads it with a strict YAML loader first and stops with
these findings before any other rule, `aye check` included:

| Rule | Requirement | Why |
|---|---|---|
| `yaml-syntax` | the file parses as YAML and every value can be read as the type YAML gives it, unquoted or by an explicit tag such as `!!bool` | the other rules need the parsed file; the finding names the line and column where parsing stopped, or of a value such as `2027-02-30`, which YAML reads as a date that does not exist |
| `yaml-duplicate-key` | no mapping repeats a key, quoted or not | YAML keeps the second of two `hosts.a` entries and drops the first without a word: a host, a service or a module setting is gone and `aye check` passes |
| `yaml-key-type` | every key is a string; quote `yes`, `no`, `on`, `off`, `y`, `n`, numbers, dates and `~` | YAML 1.1 reads `on:` as `true` and `123:` as a number, and the JSON Schema checks the names of string keys only, so such a key passes and then names a host `True` |

```yaml
project: acme
hosts:
  a:
    address: 10.0.0.1
  a:
    address: 10.0.0.2
  yes:
    address: 10.0.0.3
```

```text
error [yaml-duplicate-key] hosts.a: fleet.yaml line 5, column 3: the key 'a' repeats line 3, column 3; YAML keeps only the last one
error [yaml-key-type] hosts.yes: fleet.yaml line 7, column 3: the key yes is read as a boolean, not a string; quote it: "yes"
fleet.yaml: 2 error(s)
```

The path is the place in the file, the message names the file, the line and column of the
repeated key and of the first one. All problems of the file come at once. A merge key `<<`
may be overridden by the mapping it is merged into; a problem inside an anchor is reported
once, at the anchor. The same loader reads:

- `module.yaml` of a local module, with paths below `module_paths.<index>.<module>`; a parse
  error there stays `module-spec` and names the file, the line and the column. The built-in
  modules load with it too, and a test holds them to it.
- the compose files a host lists under `compose`: `yaml-syntax` and `yaml-duplicate-key` at
  `hosts.<host>.compose`. Key types are left alone there, since Compose reads YAML 1.2, where
  `yes` is a string.
- the OpenAPI spec of an `mcp` source: a repeated key fails `mcp-source`; numeric response
  codes such as `200:` stay allowed.

## Host and module rules

| Rule | Requirement |
|---|---|
| `ayeops-version` | the running AyeOps satisfies the manifest's `ayeops` specifier, checked first and alone, see [pinning a release](manifest.md#pinning-a-release) |
| `schema` | the manifest matches the JSON Schema; no unknown keys |
| `module-known` | every enabled module exists, built-in or from `module_paths`, and so does every module a host turns off with `false` |
| `profile-known` | a host's `profile` names an entry of `profiles` |
| `module-path`, `module-spec`, `module-clash`, `module-files`, `image-pinned`, `alert-known` | local modules are well formed, see [modules](modules.md#write-your-own-module) |
| `module-settings` | module settings match the module's schema |
| `timezone` | the top-level `timezone` and every `timezone` in module settings is a known IANA zone |
| `module-requires` | required modules run on the same host (`grafana` needs `metrics` as server; exporters need `metrics`; `auth` needs `ingress`) |
| `grafana-single` | grafana runs on at most one host |
| `delivery-single` | delivery runs on at most one host, so its recording rules are written once |
| `grafana-dashboards` | the grafana `dashboards` directory holds `*.json` files, each valid JSON with a `uid` |
| `grafana-dashboard-uid` | a project dashboard uid is used once, and not by a module dashboard |
| `grafana-datasource` | every datasource a project dashboard names is declared in `datasources`, is `victoriametrics`, a Grafana built-in or a `${variable}` |
| `grafana-datasource-unique` | datasource uids and names are unique and leave `victoriametrics` / `VictoriaMetrics` to the module |
| `grafana-reader-mount` | a service whose catalog entry says `grafana_reader: true` runs on the grafana host, mounts `grafana-reader:/run/grafana-reader:ro`, sets `AYE_GRAFANA_READER_TOKEN_FILE=/run/grafana-reader/token`, adds group `10470` and waits for `grafana-reader` to be healthy; no other service mounts that volume, see [grafana-reader](modules.md#grafana-reader) |
| `remote-write-target` | an agent has `remote_write`; a target host exists, runs a metrics server, exposes `8428` and has an `address` |
| `logs-push-target` | a logs agent has `push`; a target host exists, runs a logs server, exposes `3100` and has an `address` |
| `offsite-target` | an offsite `target: {host}` is another host that runs the objectstore, exposes its port (`3900` for Garage, `8000` for rest-server) and has an `address`, in another `zone` when both hosts name one; `quota` and `region` then come from that objectstore; a client of a rest-server is not named `metrics`, the user reserved for its metrics |
| `objectstore-provider` | the objectstore sets no setting of the other provider (`bucket`, `region`, `meta_dir` are Garage's, `prune_schedule` is rest-server's) and exposes only its own port |
| `mcp-provider` | the `mcp` provider is implemented: `agentgateway`; `contextforge`, `docker` and `microsoft` are designed in [mcp.md](mcp.md) and refused as not implemented yet |
| `mcp-kind` | every `mcp` source is of a kind its provider can serve and AyeOps renders: not `openapi` with ContextForge, Docker or Microsoft, not `cli` or stdio with Docker; `cli` and `mcp` from an `image` are not implemented yet |
| `mcp-description` | every `mcp` source and `cli` command has a description, which is what the assistant reads to pick a tool |
| `mcp-write` | a write tool (an S3 write or presign, a non-`GET` operation, a `cli` command with `write: true`) is exposed only through `allow_write`, and `allow_write` names write tools only |
| `mcp-tools` | an `mcp` source lists its read `tools`; a tool name of an `s3` or `openapi` source exists there |
| `mcp-source` | an `mcp` source has the keys of its kind and no others, a host has at most one `grafana` and one `victoriametrics` source, a `grafana` source runs next to Grafana (it reads it with the reader token), a default `url` or `endpoint` has its module on the host (the metrics server, a Garage objectstore), an OpenAPI spec exists, parses and names every operation |
| `mcp-open` | no `auth.open` route points at the MCP hub, which trusts whoever reaches it |
| `backup-schedule` | a `backups` job's `schedule` is five cron fields within their ranges that fire at least twice in four years, so the longest gap between two runs, plus `grace`, is the age after which the job counts as missing |
| `backup-textfile` | a host whose `backups` declares `jobs` runs `node` with a `textfile_dir`: those dump jobs run on the host and write their metrics there |
| `backup-name` | a `backups` job is not named like a built-in dump of a module on the same host (`secrets`, `errors`, `metrics` with `backup: true`), whose metrics carry the same `backup` label |
| `metrics-backup` | a `metrics` with `backup: true` has `role: server` and `backups` on its host, which declares the job `metrics` and alerts on it; it also **warns**, without failing, for a `role: server` without `backup: true` on a host that runs `backups`: its dumps are backed up and the store's history is not ([backup of the store](modules.md#backup-of-the-store)) |
| `offsite-source` | the directory of an offsite `source` is a real directory below `/`, without wildcards |
| `offsite-retention` | an offsite policy keeps at least one snapshot |
| `offsite-drill` | a `restore_drill` has a `target: {host}` running a rest-server objectstore with `node`, a `verify: pg_dump` source, read-only checks with unique names, the same `version` as the other drills on that store, and a window from `schedule` for `max_minutes` that does not hold the store's `prune_schedule` |
| `incidents-host-alert` | every entry of the incidents module's `host_alerts`, a name or the `alert:` of an entry with labels, is an alert of a module |
| `sentinel-watch` | a sentinel's `watch` names a host of the manifest other than its own, in another `zone` when both hosts name one: one outage must not silence both |
| `sentinel-checks` | a sentinel has at least one check after the defaults and overrides, and each has a known kind with its `url` or `host` |
| `sentinel-digest` | a sentinel with `digest` knows where to read the backup metrics: its `digest.url`, or a `samples` check of kind `fresh` to take the url from |
| `status-reporter` | the `status` module has a `status_page.hostname` to push to and at least one public component without a `probe`, which waits for its pushes |
| `host-volatile` | a host's `volatile` is `true` or a duration from `1h` to `7d`, and the host runs `node`, whose `host-down` it softens, see [volatile hosts](manifest.md#volatile-hosts-and-maintenance-windows) |
| `maintenance-window` | a host's `maintenance` window lasts longer than 0 and at most 24 hours, and is not 24 hours on every day, which would never close; its `start` (`"HH:MM"`) and `days` (`mon` to `sun`) are checked by the `schema` |
| `expose-bind` | a host that exposes ports declares `public_bind`; nothing is published on all interfaces by default |
| `memory-budget` | the `mem_limit` of all services plus the host's `reserved` memory fits into `memory_budget_pct` (default 85 %) of the host `memory` |
| `compose-file` | listed compose files exist |
| `ingress-host` | a host with ingress has an IPv4 `address` (its A records point there) and `public_bind` (443 binds there) |
| `ingress-route` | every route targets a service a module renders on the same host, on a port that service listens on |
| `ingress-probe` | `probes.http` on the same host probes at least one route over HTTPS, so `tls-expiring` watches the certificate |
| `ingress-unique` | a name below an ingress domain is routed on one host only |
| `ingress-auth` | a host with ingress runs `auth` too, or its ingress states `without_auth: <why>` ([a waiver](#waivers-end)) |
| `waiver-until` | the `until` of a [waiver](#waivers-end) is a day as `YYYY-MM-DD`; with `rules.waivers_need_until: true` every waiver has one |
| `auth-route` | no ingress route is named `auth`; that name is the sign-in callback |
| `auth-open` | every name in `auth.open` is an ingress route on the same host |
| `auth-admins` | every address in `auth.admins` is in `auth.emails` |
| `auth-endpoints` | with `provider: oidc` and Grafana on the host, `auth.endpoints` is set |
| `portal-route` | a host with the portal module routes `portal:8080` through ingress on the same host |
| `portal-metrics` | a host with the portal module runs `metrics` with `role: server`, since the page reads live status through the portal's proxy to that VictoriaMetrics |
| `icon-known` | an `icon` on a host, service or external is a vendored icon or glyph, see [icons](manifest.md#icons) |
| `brand-file` | `brand.logo`, `brand.logo_dark` and `brand.favicon` name an existing `.svg`, `.png`, `.webp` or `.ico` file next to the manifest |
| `client-endpoint` | every entry in a service's `clients` is in the manifest |
| `text-hygiene` | no key or value carries a character a reader cannot see or tell apart, see [below](#the-text-hygiene-rule); typographic quotes and dashes are fine, values are prose |
| `flow-endpoint` | both ends of every entry in `flows` are in the manifest |
| `depends-known` | every `depends_on` entry is a service, a module service, a cloud dependency or `external:<name>` from `externals` |
| `depends-cycle` | `depends_on` has no cycle |

## Data rules

A service's [`data`](manifest.md#data) block declares what its backups promise; these rules
hold the backups, offsite and objectstore settings of the manifest to it. All paths are below
`services.<name>.data`.

| Rule | Requirement | Why |
|---|---|---|
| `data-backup` | `backup` names a job of the `backups` module on the service's host: one of its `jobs` or a built-in dump (`secrets`, `errors`, `metrics`) | a promise about a dump nobody runs is a promise nobody keeps |
| `backup-rpo` | with an `rpo`: the job's longest gap between two scheduled runs plus `grace`, the age after which `backup-missing` fires, is at most the `rpo`; with `offsite` in `copies` also the age of the off-site copy, the longest gap between two daily copies in the offsite `timezone` (25 hours on the night the clocks go back) plus one `retry_minutes` | the threshold that pages someone must lie inside what the service may lose, or the backups can be healthy by every alert and still lose more than promised |
| `backup-rto` | with an `rto` and a [restore drill](modules.md#restore-drill) on the off-site copy: the drill's `max_minutes` is at most the `rto` | a drill killed only after it ran longer than the `rto` passes while proving the restore too slow; with the limit, a restore slower than the `rto` fails the drill and fires `offsite-drill`. Without a drill the `rto` is documentation |
| `backup-placement` | every entry of `copies` exists: `offsite` needs the offsite module on the host, and its `source` directory is where the job writes its `output` (not compared when the output's directory holds a `$(...)`); for a `criticality: critical` service also: at least two copies in different zones, one copy with another provider than the service's host when every copy's host names its `provider`, and at least one append-only copy | see below |

A critical service's copies must survive what takes its host: a fire in the room, a provider
that closes the account, and an attacker with root on the host.

- **Two zones.** The host copy stands in the host's `zone`, an off-site copy on a host target in
  that host's `zone`; both must be named and differ. A `url` target is outside the fleet and
  counts as another zone. Without a zone the finding asks for one: for a critical service the
  manifest has to be able to tell.
- **Another provider.** When the service's host and every copy's host name a `provider` and they
  are all the same, one account or one outage takes them all. A `url` target counts as another
  provider; when a host names none, the manifest cannot tell and the rule says nothing.
- **Append-only.** At least one copy that the service's host cannot delete: an off-site copy to an
  [objectstore](modules.md#rest-server-append-only) with `provider: rest-server`, or to a `url`
  target with `append_only: true`. That copy is off-site, so its weekly
  [restore test](modules.md#offsite) is always configured (`restore_test_days` is 1 to 60); a
  restore drill on it adds the database check.

```text
error [backup-rpo] services.acme-db.data.rpo: the daily off-site copy of edge at 05:30 can be 26h old (the longest gap between two copies plus retry_minutes), longer than the rpo 25h
error [backup-placement] services.acme-db.data.copies: no copy is append-only, so whoever takes over edge can delete them all: copy off-site to an objectstore with provider rest-server, or to a url target with append_only: true
```

One warning belongs here, printed by `aye check` without failing it:

| Rule | Warns when |
|---|---|
| `data-declared` | a `criticality: critical` service has no `data` and runs on a database: its compose image or its `units` entry is `postgres`, `postgresql`, `mysql`, `mariadb`, `mongo`, `mongodb` or `mongod` (registry, tag, digest and unit suffix aside, so `postgres:18.1-alpine` and `postgresql@16-main.service` count, `postgres-exporter` does not) |

## Waivers end

Some rules accept a written reason instead of the real thing: `metrics: {none: <reason>}`,
`status: {internal: <reason>}`, the `reason` of a baseline `skip` and the ingress
`without_auth: <reason>`. A reason written once stays forever, long after the exporter was
promised or the last client of a retired tool moved. So each of them also takes an end:

```yaml
services:
  billing-sync:
    metrics:
      none:
        reason: the vendor ships an exporter in the next release
        until: 2027-03-31
    status:
      internal: {reason: only the finance team uses it, until: 2027-06-30}
    alerts:
      - name: sync-flow
        baseline:
          metric: billing_rows_total
          by: [vendor]
          skip:
            - match: {vendor: legacy}
              reason: the legacy vendor is switched off in March
              until: 2027-03-31
hosts:
  central:
    modules:
      ingress:
        without_auth: {reason: reachable over the VPN only, until: 2027-01-31}
```

The plain string form is still a waiver, without an end. `until` is a day, `YYYY-MM-DD`,
unquoted or quoted, and the waiver holds through that day. Fourteen days before, `aye check`
warns and still passes; from the day after, the waiver no longer counts and the rule it stood
in for fails, naming the rule, the path, the reason and the day:

```text
warning [service-metrics] services.billing-sync.metrics.none: the service-metrics waiver 'the vendor ships an exporter in the next release' ends on 2027-03-31, in 9 days
manifest.yaml: ok, 1 warning(s)

error [service-metrics] services.billing-sync.metrics.none: the service-metrics waiver 'the vendor ships an exporter in the next release' ended on 2027-03-31, 2 days ago: drop the waiver and meet the rule, or set a later until
```

Warnings are printed by `aye check` only; `render`, `deploy` and the other commands that run
the rules stop on the error alone, like on any other finding. Fix it by meeting the rule, or by
writing a later `until` after a fresh look. "Today" is the day in the manifest's `timezone`;
`aye check --today YYYY-MM-DD` measures against another day, for a test or to see what expires
next month. An `until` that is not a day (`"2027-02-30"`, `2027-1-5`, a time) fails rule
`waiver-until`; an unquoted `2027-02-30` is a date YAML cannot build, so the manifest stops
with `yaml-syntax` at its line and column.

`rules.waivers_need_until: true` at the top of the manifest tightens this: every waiver then
needs an `until`, and a plain string fails `waiver-until`. The manifest has no history, so it
cannot tell a new waiver from an old one; switching the rule on is the moment to give the old
ones their end. A `without_auth` on a host that runs `auth` anyway waives nothing and is not
checked; neither are the waivers of a [reserved](manifest.md#reserved-services) service.

The [portal](modules.md#portal) lists the waivers of a service on its card with their end and
the days left, counted from the build day of the page and recounted by the browser each time
the page opens. A card turns amber inside the fourteen days and red after the end.

## The portal shows the rest red

`aye portal` checks only the `ayeops` pin, the schema and the module settings, so the gaps the catalog rules
reject still get a page: a service without a description, alerts, runbooks or a status mark, a
compose service missing from `services`, a host without the node module, an ingress route to
nothing, and whatever `aye discover` found on a host that the manifest does not declare. Each
shows red with its reason, and the summary at the top counts red first. Drift between the
pages and the manifest (an edge to an unknown node, an unknown flow or dependency endpoint)
still fails the build.

## The no-comments rule

Code, configuration, templates, scripts and stylesheets carry no comments; `python -m ayeops.comments .` fails on one,
and it runs as a test and in CI. Shebangs, `# syntax=` in Dockerfiles and YAML document markers
are allowed. Comments drift from the code they describe; names, tests and these docs are checked
or read, so explanations live there.

## The text-hygiene rule

`python -m ayeops.texthygiene .` fails on characters a reader cannot see or tell apart, the
kind language models emit; it runs as a test and in CI over every file `git ls-files` lists
(every file below the path outside a repository), skipping binary files. Each finding is
`path:line:column: U+XXXX NAME (kind): fix`.

| Kind | Characters | Fix |
|---|---|---|
| `invisible` | zero-width space, joiners (a joiner inside an emoji sequence excepted), word joiner, invisible operators, a byte order mark anywhere, soft hyphen | delete it |
| `bidi` | embeddings, overrides and isolates (U+202A-U+202E, U+2066-U+2069), LRM, RLM, ALM: text that renders in another order than it parses | delete it |
| `space` | no-break and other spaces (U+00A0, U+2000-U+200A, U+202F, U+205F, U+3000, U+1680) | a plain space |
| `separator` | U+2028, U+2029 | a plain newline |
| `control` | control characters other than tab, newline and carriage return | delete it |
| `private-use` | private-use code points | delete it |
| `mixed-script` | a word with both Latin and Cyrillic letters, a letter after a backslash excepted, so an escape like `\n` before a word is not part of it | retype it in one alphabet |
| `typography` | curly quotes, dashes, minus sign, ellipsis | the ASCII character |

`typography` applies only to scripts and configuration, where a curly quote or a long dash
breaks a command: shell, YAML, TOML, INI, `.env`, Alloy, Dockerfiles, Makefiles and ignore files.
Python, TypeScript, JSON locales, templates and Markdown carry user-facing text, where they are
correct. The same characters in a manifest are the [`text-hygiene`](#host-and-module-rules)
rule of `aye check`: manifest values end up in Grafana titles, alert messages and the portal.
