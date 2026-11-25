# Status page

A public status page for one or many projects, served by one Cloudflare Worker with a D1
database. The code lives in [`cloud/status`](../cloud/status): TypeScript without runtime
dependencies, D1 migrations, a `wrangler.toml` template and the push contract shared with the
Python side.

```text
manifest ──aye status --sql──▶ D1 seed (tenant, components, probe URLs)
                                   │
Cloudflare cron, every minute ─────┤  fetches public probe URLs, sends Telegram notices
stack reporter ──POST /api/push────┤  signed internal states
admin (behind Access) ─────────────┤  incidents, maintenance
                                   ▼
                     minutes (2 days) + daily counts (90 days)
                                   │
              GET /  (HTML)   GET /api/status  (JSON)
```

## Tenants

One Worker serves many projects. A request is matched to a tenant by its hostname
(`status.acme.example` → `acme`) through the `tenants` table, so every project only needs a
custom domain on the same Worker. An unknown hostname gets 404. Each tenant has its own HMAC
secret for pushes, its own components, incidents and maintenance windows, a default locale
and its own Cloudflare Access application for the [admin API](#admin).

## Components

Components come from the manifest: every service with `status: {public: <title>}`, see the
[manifest reference](manifest.md#status-mark). Services with the same title share one
component. `aye status <manifest>` prints the tenant document, `--sql` the seed for D1:

```json
{
  "tenant": "acme",
  "hostname": "status.acme.example",
  "title": "Acme",
  "locale": "en",
  "notify": {"telegram_chat_id": "-1001234567890", "min_state": "outage"},
  "access": {"team": "acme", "aud": "fb52298607fa036764eb5e6e31b89bab96294fddc185ce6324890af471c374f0"},
  "components": [
    {"id": "website", "title": "Website", "probes": ["https://acme.example"], "push": false}
  ]
}
```

`notify` is `null` without `status_page.notify` in the manifest, see [notices](#notices);
`access` is `null` without `status_page.access`, see [admin](#admin).

Only ids, titles, the notify and the Access settings leave the manifest, and the Telegram chat
id and the Access settings stay in D1, off the page and the JSON API. Service names, hosts, descriptions and the reasons of
internal marks never reach the seed, the page or the JSON API; tests check both sides.

The seed is idempotent: it upserts the tenant (with its notify chat and Access settings, or
none) and the components, replaces the probe URLs and deletes components that left the
manifest, together with their notices; without `notify` it deletes all notices of the tenant.
It never touches the push secret, and the bot token never reaches it.

## Sources and states

The five states are `operational`, `degraded`, `outage`, `maintenance` and `no_data`.

| Source | Where it comes from | What it says |
|---|---|---|
| probe | the Worker fetches each `probe` URL once a minute, retrying a failure once | 2xx or 3xx within 3 s: operational; slower: degraded; error or other status: outage |
| push | the stack reporter posts states to `/api/push` | the pushed state, while it is at most 5 minutes old |
| incident | an open incident names the component (or none, meaning all) | its impact, `degraded` or `outage` |
| maintenance | a planned window is open for the component, or the reporter pushes it | maintenance |

A component expects pushes when at least one of its services has no `probe`. For such a
component, more than 5 minutes without a push is `no_data`, never `operational`. A probe-only
component ignores a silent reporter but still shows a fresh pushed problem.

Each minute the Worker records one state per component: maintenance if a window is open or
pushed, otherwise the worst of the sources, ranked `outage` > `degraded` > `no_data` >
`operational`. A component without any source is `no_data`.

The page shows the state of the newest minute of each component. When that minute is more than
3 minutes old (the cron stopped), it shows `no_data`.

The overall banner is the worst component state, ranked `outage` > `degraded` > `maintenance` >
`no_data` > `operational`.

Days are UTC. A day bar is an outage if any minute was an outage, degraded if any minute was
degraded, no data if minutes without data outnumber the operational and maintenance ones,
maintenance if any minute was maintenance, operational otherwise. Uptime counts operational,
maintenance and degraded minutes as up, outage minutes as down and leaves `no_data` minutes
out; the tooltip of each bar says how many minutes had no data.

## Notices

The Worker runs outside the fleet, so it can tell a person when the fleet cannot: a provider
outage that takes down the hosts, Grafana and the [sentinel](modules.md#sentinel) at once still
shows here as failing probes and, after 5 minutes without a push, as `no_data`. A tenant turns
the notices on in the manifest:

```yaml
status_page:
  hostname: status.acme.example
  notify:
    telegram_chat_id: "-1001234567890"
    min_state: no_data
```

| Field | Default | Meaning |
|---|---|---|
| `telegram_chat_id` | required | the chat that receives the notices, a quoted number; the bot must be a member of it |
| `min_state` | `outage` | the mildest state worth a message: `outage`, `degraded` (outage and degraded) or `no_data` (outage, degraded and no data) |

The bot token is a Worker secret, the same for every tenant, and never sits in the manifest, the
seed or D1: [`aye status deploy`](#deploy) sets it from `TELEGRAM_BOT_TOKEN`. Without the secret,
or for a tenant without a chat, the Worker sends nothing. Add the bot to each tenant's chat.

Each minute, after it has recorded the minute, the Worker compares every component of a tenant
with notices with the minute before:

- A state is announced once it holds for **two consecutive cron minutes**; a one-minute blip,
  or two bad minutes with a missed cron run between them, says nothing.
- `operational` held for two minutes after an announced problem sends **one** recovery message
  with the length of the problem. A change from one announced bad state to another, say outage
  to degraded with `min_state: degraded`, is announced too.
- `maintenance`, and states milder than `min_state`, announce nothing and keep the last notice
  open, so an outage that runs into a maintenance window is closed by the recovery after it.
- Every component of one tenant that changed in the same minute goes into one message: the
  title of the page, one line per component with its state and the UTC time it began (or the
  length of the outage), and the page URL. Up to 30 lines, then "and N more". The language is
  the tenant locale.

The last announced state and the start of the problem live in `notices`, one row per component.
The minute is recorded first and the notices are sent only by the run that recorded it, so a
duplicate cron run of the same minute sends nothing, and a fresh isolate reads what was already
announced from D1. A send that fails (an error, a timeout after 10 s, a non-2xx answer) marks
nothing and does not fail the run; the next minute finds the same change and sends it again,
with the time the state really began. A problem that clears before Telegram is back is never
announced at all. The one way to get a message twice is the Worker dying between Telegram's
answer and the write to `notices`.

Use it with the sentinel, not instead of it, when the fleet has more than one failure domain:
the sentinel sees Grafana, VictoriaMetrics and stale samples inside the fleet, the Worker only
what is public or pushed. When every host, the sentinel's included, sits with one provider, the
Worker is the only watcher left standing in that provider's outage.

## Push contract

`POST /api/push` with the JSON body below, the header
`X-AyeOps-Signature: sha256=<hex HMAC-SHA256 of the exact body bytes with the tenant secret>`.

```json
{"tenant": "acme", "ts": 1792742460, "components": [{"id": "api", "state": "degraded"}]}
```

| Check | Answer |
|---|---|
| body over 64 KiB, by its `Content-Length` before any byte is read or, without one, as soon as the bytes read pass the limit | 413 |
| not JSON, or not matching [`push.schema.json`](../cloud/status/contract/push.schema.json) | 400 with the list of problems |
| unknown tenant, tenant without a secret, missing or wrong signature | 401 |
| tenant differs from the tenant of the hostname the push was sent to | 400 |
| `ts` (unix seconds) more than 300 s away from the Worker clock | 401 |
| the same component twice | 400 |
| a component id the tenant does not have | 422 |
| accepted | 202 `{"accepted": <n>}` |

Pushes are idempotent: the Worker keeps the latest state per component and only replaces it
with a push whose `ts` is not older, so a replay inside the 300 s window changes nothing and an
old push never overwrites a newer one.

The contract has one home, [`cloud/status/contract`](../cloud/status/contract): the JSON Schemas
of the push and of the tenant document, and fixtures used by both test suites:
`acme.yaml` (a manifest), `acme.components.json` and `acme.seed.sql` (what `aye status` must
print for it, and what the Worker tests load into D1), and `push.json` (valid, off-contract and
refused pushes with their signatures and expected answers).

## Admin

Incidents and maintenance are written through an API under `/api/admin/`, which sits behind a
Cloudflare Access application for `<status hostname>/api/admin`.

| Request | Body |
|---|---|
| `POST /api/admin/incidents` | `title`, `impact` (`none`, `degraded`, `outage`), optional `body`, `state` (`investigating` by default, `identified`, `monitoring`, `resolved`), `components` (ids; empty means all) |
| `PATCH /api/admin/incidents/<id>` | any of the fields above; `state: resolved` sets the resolution time |
| `POST /api/admin/maintenance` | `title`, `starts_at`, `ends_at` (ISO 8601), optional `body`, `components` |
| `DELETE /api/admin/maintenance/<id>` | none |

Access puts a signed JWT into the `Cf-Access-Jwt-Assertion` header of every request it lets
through. The Worker does not trust the header for being there, since a route that skips Access
(a `workers.dev` URL, another custom domain, an application with the wrong path) would pass any
header. It verifies the token itself, as Cloudflare's
[Validate JWTs](https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/authorization-cookie/validating-json/)
describes, before it looks at the path or the body:

| Check | Refused when |
|---|---|
| tenant settings | the tenant has no valid `access_team` and `access_aud` in D1: every admin request is refused, the API is closed until both are set |
| token | the header is missing, not three base64url parts, or its header or payload is not a JSON object |
| `alg` | anything but `RS256`, `none` included; a header with `crit` is refused too |
| `kid` | missing, or no RS256 signing key of the team's JWKS has it |
| signature | RSASSA-PKCS1-v1_5 with SHA-256 (WebCrypto) does not verify against that key |
| `iss` | not exactly `https://<team>.cloudflareaccess.com` |
| `aud` | does not hold the AUD tag of the tenant's application (a string or a list) |
| `exp` | missing, not a number, or 60 s or more in the past |
| `nbf`, `iat` | not a number, or more than 60 s in the future |

Every refusal is a 403 with the reason in `error`. The reasons that name a claim come only
after the signature has verified, so whoever sends a forged token learns nothing about the
settings. Nothing
is written before every check has passed.

The keys come from `https://<team>.cloudflareaccess.com/cdn-cgi/access/certs`, from its `keys`
list of JWKs (the current and the previous key), matched by `kid`; `public_cert` is not used.
The Worker caches the keys of each team in memory for an hour. A `kid` the cache does not know
makes it fetch the keys once more, so a rotation (every 6 weeks by default; the previous key
stays valid for 7 days) is picked up at once, but at most once per 30 s per team, so tokens with
made-up `kid`s cannot hammer the endpoint. Concurrent requests share one fetch. When the keys
cannot be fetched (an error, a timeout after 5 s, a non-2xx answer), the request gets 503 and
nothing is written.

The settings live per tenant in D1 (`tenants.access_team` and `tenants.access_aud`, migration
`0003_access.sql`), not in Worker variables: one Worker serves many hostnames and every hostname
has its own Access application with its own AUD tag, so a single `POLICY_AUD` variable could
serve one tenant only and every new tenant would change the Worker config. They come from the
manifest through the seed:

```yaml
status_page:
  hostname: status.acme.example
  access:
    team: acme
    aud: fb52298607fa036764eb5e6e31b89bab96294fddc185ce6324890af471c374f0
```

| Field | Meaning |
|---|---|
| `team` | the team name of the Zero Trust organization, the `<team>` of `<team>.cloudflareaccess.com` (Zero Trust > Settings, team name and domain) |
| `aud` | the Application Audience (AUD) Tag of the application, 64 lower-case hex characters |

The AUD tag is not a secret, but it belongs to one install, so it goes into the manifest of that
install and never into a committed default. To find it, open Zero Trust > Access controls >
Applications in the Cloudflare dashboard, select **Edit** on the application for
`<status hostname>/api/admin` and copy the **Application Audience (AUD) Tag** under Additional
settings. It stays the same until the application is deleted and created again; a new
application means a new tag in the manifest and another [`aye status deploy`](#deploy).
Removing `status_page.access` and deploying closes the admin API again.

## Public page

`GET /` renders HTML on the server: overall banner, open incidents (and those resolved in the
last 24 hours), planned and running maintenance, every component with its current state and 90
daily bars, and the time of the last update. It loads no scripts, fonts, images or stylesheets
(the CSP forbids them), follows the light or dark preference of the device, and fits a phone
screen.

The language comes from `?lang=`, then `Accept-Language`, then the tenant locale, and English
when none of them names a language the page has. Texts live in
[`src/locales`](../cloud/status/src/locales): `uk.json` is the source, `en.json` and `ru.json`
translate it; a test fails when a locale misses a key, has an extra one or changes a
placeholder.

`GET /api/status` returns the same data as JSON with CORS open, for widgets and checks.

## Storage

| Table | Holds | Kept |
|---|---|---|
| `tenants` | hostname, title, locale, push secret, notify chat and minimum state, Access team and AUD tag | |
| `components`, `probes` | the seed from `aye status --sql` | |
| `push_latest` | the newest pushed state per component | |
| `minutes` | one state and probe latency per component and minute | 2 days |
| `daily` | minutes per state per component and UTC day, incremented by the minute | 90 days |
| `runs` | one row per cron minute, so a duplicate run writes nothing | 2 days |
| `notices` | the last announced state per component and when its problem began | while the component exists |
| `incidents`, `maintenance` | admin records | |

Retention runs at the top of every hour. R2 is not needed.

## Free-tier budget

Per day, with N components across all tenants:

| Resource | Use | Workers Free / D1 Free limit |
|---|---|---|
| Worker invocations | 1 440 cron runs + page views + pushes (1 440 per reporter at one push a minute) | 100 000 requests |
| D1 rows written | per component: 1 440 minutes, 1 440 daily increments, 1 440 retention deletes, up to 1 440 push upserts; plus 2 880 for `runs` and one row per announced change | 100 000 |
| D1 rows read | cron: about 6 N per minute (the component, its previous minute and its notice), plus the length of the current streak of each component in a minute that announces it; page view: about 90 N plus a few per component | 5 000 000 |
| Subrequests per cron run | one fetch per unique probe URL, a second one when it fails, and one Telegram call per tenant with something to announce: 2 U + T at worst | 50 |
| D1 queries per cron run | 6 reads (5 without a bot token) and one batch of 2 N + 1 statements (3 more at the top of the hour), plus one read and one upsert in a minute that announces anything, for all tenants together | 50 per invocation, if every batched statement counts |
| Cron triggers | 1 | 5 per account |

About 5 760 rows written per pushed component and day, so the free plan carries about 16 pushed
components, or about 22 probe-only ones. The per-invocation query limit allows 2 N + 7 in a
quiet minute and 2 N + 12 at the top of an hour that also announces, so about 21 components,
19 in the worst minute. Notices cost subrequests only when something changes, but they come
when everything changes at once: with U probe URLs that all fail twice, 2 U + T must stay
under 50, so 20 failing URLs leave room for 10 tenants. Pushing every two minutes instead of every minute
halves the push share; the 5 minute silence threshold tolerates it. Beyond that the Workers Paid
plan includes 50 million D1 rows written a month.

## Deploy

One command, run by the owner from a checkout, with the secrets coming from Infisical straight
into its environment:

```sh
infisical run --env=<env> --path=<path> -- uv run aye status deploy manifest.yaml
```

`<env>` and `<path>` are the Infisical environment and folder that hold the variables below.
Add `--dry-run` to print every step with the secrets masked and run none of them; it still
needs the variables, so it also checks that the Infisical folder is complete.

It runs the same on Windows: every wrangler argument stays on one line, since `npx` is
`npx.cmd` there and `cmd.exe` cuts an argument at a line break, and the emoji wrangler prints
reach a console in another code page as `?`. In Git Bash, prefix the command with
`MSYS_NO_PATHCONV=1`, or Git Bash turns `--path=/status` into a Windows path.

| Variable | Used for |
|---|---|
| `CLOUDFLARE_ACCOUNT_ID` | wrangler, the account of the Worker and the D1 database |
| `CLOUDFLARE_API_TOKEN` | wrangler; the token needs Account: Workers Scripts Edit and D1 Edit, and Zone: Workers Routes Edit and DNS Edit on the zone of every status hostname, since the custom domain creates its DNS record and certificate. The "Edit Cloudflare Workers" template plus D1 Edit and DNS Edit covers it |
| `AYE_STATUS_PUSH_SECRET` | the tenant secret in D1, the same value the [reporter](#reporter) signs with |
| `TELEGRAM_BOT_TOKEN` | the Worker secret for [notices](#notices); required when the manifest sets `status_page.notify`, set when present otherwise |

A missing or empty variable stops the command before anything runs, naming every missing
variable and never printing a value. The hostname comes from `status_page.hostname` and the
tenant from `project`, the name the reporter pushes under; `--hostname` overrides the first,
`--database` names the D1 database (default `ayeops-status`), `--wait` the seconds the checks
wait for a new domain (default 180) and `--worker-dir` the `cloud/status` directory when the
command does not run from this checkout. The status hostname's zone must be on Cloudflare.

The steps, each through `npx wrangler@4.148.0` and each safe to run again:

1. Find the D1 database by name in `d1 list --json`; create it only when it is missing.
2. Write a filled copy of `cloud/status/wrangler.toml` into a temporary directory: the database
   name and id, absolute paths to `src/index.ts` and `migrations`, `workers_dev = false`, and
   one custom-domain route per tenant hostname. The committed template keeps its placeholders.
3. `d1 migrations apply --remote`: applies only the migrations D1 has not seen.
4. Read the tenants, and the components and probe URLs of this tenant, from D1. A hostname
   that another tenant holds stops the deploy here.
5. Seed the tenant with `aye status --sql` only when D1 differs from the manifest: a missing
   tenant or component, a changed title, position, probe URL, notify or Access setting, or a
   component that left the manifest; it prints which. An up-to-date tenant is left alone.
6. Set the push secret of the tenant from `AYE_STATUS_PUSH_SECRET`, on every run, so a rotated
   value in Infisical reaches D1.
7. `deploy` with routes for every tenant already in D1 plus this one, so deploying one tenant
   never drops the custom domain of another.
8. `secret put TELEGRAM_BOT_TOKEN`, after the deploy, because the Worker must exist first.
9. Check the live page, retrying every 10 s until `--wait` runs out: `GET /` answers 200 with
   HTML, `GET /api/status` lists exactly the components of the manifest, and an unsigned
   `POST /api/push` gets 401. A failed check leaves the Worker deployed; a new custom domain
   can take a few minutes to resolve and get its certificate, so run the command again or pass
   a longer `--wait`.

The secrets never reach a command line, where a process list or a CI log would show them:
wrangler gets the Cloudflare variables in its environment and nothing else of the four, the bot
token goes to `secret put` on stdin, and the push secret goes to D1 in an `UPDATE` statement
written to a file with mode 0600 in the temporary directory, passed with
`d1 execute --file` and deleted right after, whether the step worked or not
(`d1 execute --command` would put it into the argv). Every line of wrangler's output is printed
with the four values replaced by `***`. The temporary directory is removed when the command
ends.

The admin API stays closed until the tenant has Access settings, so a new hostname goes live in
two rounds:

1. Deploy without `status_page.access`. The page, the JSON API and the pushes work; every admin
   request gets 403, and the command ends with `admin is closed: create a Cloudflare Access
   application for <status hostname>/api/admin, ...`.
2. In Zero Trust > Access controls > Applications, add a self-hosted application with the
   destination `<status hostname>/api/admin` and an Allow policy for the people who post
   incidents (a Service Auth policy for scripts with a service token). Copy its AUD tag and the
   team name into `status_page.access` (see [admin](#admin)) and deploy again; the changed
   settings re-seed the tenant, and the command ends with `admin accepts Cloudflare Access
   tokens of team <team> for the application with AUD tag <aud> on <status hostname>/api/admin`.

## Reporter

The stack side of the push is the [`status`](modules.md#status) module, a small script next to
Grafana, enabled on the Grafana host. A component that waits for pushes shows `no_data` until
something pushes, so a manifest with such a component wants the module; `aye check` refuses the
module when no component waits for pushes or `status_page.hostname` is missing (rule
`status-reporter`). Probe-only components are not pushed, which keeps their D1 writes to the
probe's.

1. Once a minute it reads the alert rules of Grafana
   (`/api/prometheus/grafana/api/v1/rules`) and keeps the rules of the services of every public
   component that expects pushes, by their `service` label. Grafana, not VictoriaMetrics, holds
   the alert states: the stack evaluates every alert as a Grafana rule and writes no `ALERTS`
   series, and reading Grafana keeps `for` and the no-data handling the notifications use.
2. Map to a state: a firing `critical` (or `page`) alert is `outage`, a firing `warning` is
   `degraded`, nothing firing is `operational`, an alert firing on no data or on a query error,
   a service without a loaded rule, and a failed read of Grafana are `no_data`. An active Grafana
   silence (`/api/alertmanager/grafana/api/v2/silences`) with a `maintenance` matcher and a
   `service` matcher puts those services into `maintenance`. A component takes the worst state of
   its services, and is `maintenance` when all of them are.
3. Sign the body with the tenant secret `AYE_STATUS_PUSH_SECRET` from `.env` and post it to
   `https://<status_page.hostname>/api/push`. The script builds the body and the signature itself
   (it is stdlib only); a test checks it byte for byte against `push.json`, against
   `push.schema.json` and against `ayeops.status.sign`. Network errors, `429` and `5xx` are
   retried twice with a backoff of 2 and 4 seconds within the interval, any other refusal not.
4. It exposes `ayeops_status_push_success`, `ayeops_status_last_push_timestamp_seconds` and
   `ayeops_status_push_failures_total{reason}`; the alert `status-push-failing` fires after 5
   minutes without an accepted push, the moment the page turns to `no_data`, so a silent reporter
   is noticed on the stack side as well as on the page.

## Development

```sh
cd cloud/status
npm ci
npm run typecheck
npm test
```

Tests run on Node with `node:sqlite` standing in for D1, so the real migrations, the real seed
from `aye status` and every SQL statement of the Worker run against SQLite, the engine D1 is
built on. Probes and Telegram use a fake network with a virtual clock; no test talks to
Telegram.
