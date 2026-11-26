# Autopilot

Design of the autopilot from the [roadmap](roadmap.md#autopilot-levels-0-to-3). Level 1 comes
first: the autopilot reads and explains, it changes nothing. It ships in shadow mode as the
[`autopilot`](modules.md#autopilot) module; where it differs from this design, the next section
says so.

## What ships: level 1 in shadow mode

The north star is minimal downtime, and the first useful answer is due within about 60 seconds
of the alert. Shadow mode diagnoses and records; it runs no action and messages nobody by
default. Actions come later, one incident class at a time, once the measured hit rate of its
hypotheses earns it.

| Design | Shipped |
|---|---|
| Trigger: a critical alert from Grafana, or the first step of the on-call escalation policy | Grafana's webhook only: a contact point and the first child route for `critical` and `page` with `group_wait: 0s`, so no grouping delay eats into the minute |
| Each runbook carries the metrics, log queries and containers of its alert | No runbook carries a machine-readable part yet. The dossier comes from the rule's own expression, the alert's host (node-exporter), the service's containers (cAdvisor, read from metrics, no Docker socket), deploys pushed by `aye report`, and Loki lines grouped by signature; no 20 lines around the first occurrence |
| Bounded agent loop with the autopilot's read-only MCP server | One model call with the dossier and no tools at all; the MCP tools come with the agent loop |
| Verdict: likely cause, confidence, an action | Up to three hypotheses, most likely first, each with evidence, a confidence, a `cause_class` and a target that code can compare, and the action id that would test it (`restart-container`, `redeploy-previous-render`, `prune-build-cache`) or `needs-a-person`; code drops unknown actions |
| Action after a confirmation in Telegram; the verdict goes to the alert and the on-call message | No action. The verdict goes to the journal; with `post_verdict: true` also as a silent message to the on-call chat |
| Escalation to Opus also on a low-confidence Sonnet verdict | Only for `critical_services` and for the same alert again within 24 hours, decided before the call; a second call per run would double the time to first answer |
| Claude Code only | Two runners behind one interface: the Messages API over HTTPS (the default) and `claude -p` |
| `--allowedTools "mcp__autopilot__*"` | No MCP server is configured, so `--strict-mcp-config` leaves none and no tool is allowed; `--system-prompt` replaces Claude Code's own, and `HOME` is an empty directory too |
| One run: 12 tool calls | No tool calls; per run the dollar cap (`--max-budget-usd`, or `max_tokens` computed from the price for the API) and the wall time |
| Month: the provider budget of `ai-usage` | Not enforced by the autopilot; `ai-usage` alerts on the budget of the whole key |
| One message to the operator when a cap is reached | The `autopilot-cap-reached` alert, a warning in the ops channel |
| `auth: subscription_then_api_key`, runs per auth path, the subscription token's expiry alert | Not yet: `api_key`, or `subscription` with the `claude-code` runner |
| The incident journal per incident with downtime and cost | A run journal: one line per run with the timeline, model, tokens, cost and hypotheses, then outcome lines for the resolve and a person's verdict, and a label line saying which hypothesis matched what fixed it; downtime stays in the journal of the [incidents](modules.md#incidents) module |
| Who fixed it: the reconciler, the autopilot or a person | The reconciler (its metrics, and its log lines when Loki is there), a host outage in the incident journal, or a person's verdict through the existing `/outcome` endpoint; an acknowledgement in GoAlert, from the incident journal, marks a run that no signal explains as `acknowledged` instead of `no-signal`; deploys during the incident are not read yet |

## What leaves your host

The autopilot sends one thing to a third party: the dossier of a run, as the user message of
one model call. With `runner: api` it goes to Anthropic's Messages API over HTTPS; with
`runner: claude-code` the host's `claude` binary sends it to the same provider. With
`post_verdict: true` the verdict also goes to Telegram. The journal, the kept dossiers and the
metrics stay in the agent's volume and in VictoriaMetrics.

**What is collected.** The dossier is built by plain code and capped at `dossier_tokens`
(about 16 KB of JSON by default):

| Part | Holds | Can carry a secret |
|---|---|---|
| `alert` | the alert's name, labels, `summary` annotation, firing values, the rule's PromQL | yes: labels and the summary are text that rule authors and exporters wrote |
| `series` | up to 5 series of the rule's expression: labels and 12 numbers | rarely, in a label value |
| `host`, `containers` | numbers, mount points, container names | no |
| `deploys` | project, job, the first 12 characters of the commit, minutes before the alert | no |
| `logs` | the Loki query, then at most 8 signatures of up to 500 error lines, each with a count, its sources and 2 samples of at most 200 characters | yes, the likeliest place: apps log URLs, headers, DSNs and tokens |
| `service` | host, `depends_on`, description from the manifest | no |
| `missing` | the names of the sources that failed | no |

Nothing else: no other annotation, no raw log beyond the samples, no environment of another
container, no file, no Docker socket.

**What is masked.** Before the dossier is kept or sent, every string in it, every key, and
every log line before it is grouped and cut to its sample (so a token at character 190 is not
cut in half and slips through) goes through these steps, in order. Each match becomes a marker
`[masked:<kind>]`, so the model still sees that a value was there and of what kind:

| Kind | What |
|---|---|
| `private-key` | a PEM private key block, up to its END line or, when the line was cut, to the end |
| `env` | the value of a secret variable the agent can see, also URL-encoded, as `[masked:env:<NAME>]`; see below |
| `custom` | a match of the install's own `mask` patterns |
| `jwt` | three base64url segments, the first starting `eyJ` |
| `anthropic-key`, `openai-key`, `github-token`, `gitlab-token`, `slack-token`, `stripe-key`, `google-key`, `aws-key`, `telegram-token` | `sk-ant-`, `sk-` (with `proj-`, `svcacct-`, `admin-`), `ghp_`/`gho_`/`ghu_`/`ghs_`/`ghr_`/`github_pat_`, `glpat-`, `xoxb-`/`xoxp-`/`xoxa-`/`xoxo-`/`xoxr-`/`xoxs-`, `sk_live_`/`rk_live_`/`sk_test_`, `AIza`, `AKIA`/`ASIA`, and the `<digits>:AA...` token of a bot (also inside `/bot<token>/` URLs) |
| `slack-webhook` | a whole Slack incoming webhook URL, `https://hooks.slack.com/...` or `https://hooks.slack-gov.com/...` |
| `url-password` | the password of `scheme://user:password@host`; the user and the host stay |
| `authorization` | the value of an `Authorization` or `Proxy-Authorization` header (the scheme word stays), and a `Bearer` or `Basic` credential anywhere |
| `cookie` | the value of a `Cookie` or `Set-Cookie` header |
| `password`, `api-key`, `secret`, `token` | the value of a key that reads as secret, in `key=value` (query strings, logfmt, env files), `key: value`, `"key": "value"`, JSON escaped inside a JSON string, and `key => value`; and a long hex or base64 string right after `token`, `secret`, `password`, `api key` or `signature` |

A key reads as secret when one of its words (split at `_`, `-`, `.` and camelCase) is
`password`, `passwd`, `pass`, `passphrase`, `secret`, `token`, `credential(s)`, `dsn`,
`signature`, `sig`, `jwt`, `auth`, `session` or `cookie`, ends in `password`, `passwd`,
`secret` or `token` (`PGPASSWORD`, `authToken`, `apiToken`), or is a pair like `api key`,
`access key`, `private key`, `secret key`, `signing key`, `encryption key`. A key about a secret is not one: `token_type`, `max_tokens`,
`password_file`, `secret_name`, `token_expires`, `*_url`, `*_length` and the like keep their
value. Every variable in the catalog's `secret_names` is a secret key whatever its name says
(`AYE_SENTINEL_PING_URL=...`, `AYE_TELEGRAM_CHAT_ID=...`). Placeholders (`null`, `***`,
`[REDACTED]`, `${VAR}`) are left as they are. In prose (`key: value` without quotes) only a
value that looks like a secret is masked, one with a digit, a symbol, a capital after the first
letter or 24 characters or more, so `invalid token: expired` reaches the model as it is.
Timestamps, UUIDs, IP addresses, image digests and git SHAs stay, unless they stand right
after a secret key.

Before and after, from one log line (the password is the value of a variable the agent holds):

```text
{"level":"error","msg":"POST /hook?token=4f2a9c71 failed","dsn":"postgres://shop:Kx7-shop-db@db:5432/shop","auth":"Bearer eyJhbGciOiJ9.eyJzdWIiOjF9.c2ln"}
{"level":"error","msg":"POST /hook?token=[masked:token] failed","dsn":"postgres://shop:[masked:env:AYE_SHOP_DB_PASSWORD]@db:5432/shop","auth":"Bearer [masked:jwt]"}
```

**Values of secret variables.** The render writes `secret_names` into
`autopilot/catalog.json`: the `${...}` variables of the compose templates of every module the
manifest enables, the variables of their declared credentials and generated secrets, and every
`*_env` setting of the manifest (a datasource's `password_env`, for instance). Names only,
never values. The agent masks the value of every variable of its own environment that is in
that list or whose name reads as secret, when the value is 8 characters or longer (shorter
values would mask ordinary words; the patterns still apply to them). Its own settings, which
hold a mode, a model, a URL, a path or a number, are never masked by value, even where the
name reads as secret: `AYE_AUTOPILOT_AUTH` holds `subscription`, and masking it would hide
that word in every log line and label. By default its
environment holds only its own secrets: the webhook secret, the API key or OAuth token, and
with `post_verdict` the bot token. It never reads a `.env` file, and it does not get the values
of other services. `mask_env` hands it more: each listed variable of this host's `.env` is
passed to the agent only to be masked, at the price that the agent then holds that secret too.

**Counted, never recorded.** The run's journal line carries `masked`, the count per kind of
what was masked in that run (`{"env": 1, "jwt": 2}`), and the agent exports
`ayeops_autopilot_masked_total{kind}`. Neither holds a value or a hash of one. The kept dossier
in `dossiers/<run>.json` in the volume is the masked one, exactly what was sent, so it is the
file to read when you audit what leaves.

**What is not guaranteed.** Masking is a filter, not a proof:

- A secret in a format no pattern knows, after a key that does not read as secret, whose value
  the agent does not hold, reaches the model: `login with hunter2 failed`, a password in a
  column of a SQL error, an internal token format.
- Personal data is not masked: e-mail addresses, names, phone numbers, IP addresses, order
  numbers. Add patterns for what your apps log.
- Values shorter than 8 characters are not masked by value.
- The secrets of other services are masked by value only when `mask_env` hands them over, and
  only those in this host's `.env`.
- The journal is local and keeps the alert's labels as Grafana sent them.

The best defence is still logs without secrets in them; the masking catches what slips.

**Adding patterns.** `mask` takes Python regular expressions; each match becomes
`[masked:custom]`, or only its first group when the pattern has one:

```yaml
hosts:
  central:
    modules:
      autopilot:
        mask:
          - 'ACME-[0-9]{6}'
          - 'customer_email=([^&\s"]+)'
        mask_env: [AYE_GRAFANA_PG_PASSWORD]
```

`aye check` refuses a pattern that does not compile or that matches the empty string, and a
`mask_env` entry that is one of the agent's own variables (rule `autopilot-mask`); the agent
refuses to start on a broken pattern, so a hand-edited environment cannot switch masking off.

**Without a model.** `runner: none` collects, masks and keeps the dossier of every run and
journals the run (`status: ok`, `reason: runner none: no model call`, cost 0), and sends
nothing anywhere: no model call, no Telegram message, no API key needed. Run it for a while,
read the kept dossiers (`docker compose exec autopilot ls /tmp/dossiers`), add the patterns
your logs need, then switch to `api`. Without the module nothing is collected at all.

## What it is for

A service falls at night. The person on call is asleep, and some services lose money every
minute they are down. The autopilot exists to shorten the time from the first failed probe to
a healthy service, and to wake a person only when it cannot.

The north star is **cost-weighted downtime**: the sum over incidents of the minutes a service
was down times what such a minute costs, as well as the kit knows it, see
[What downtime costs](#what-downtime-costs). It falls when incidents are rarer, shorter or hit
cheaper services, and it cannot be gamed by splitting one long incident into many short ones.
Every number behind it comes from the [incident journal](#the-incident-journal).

## What downtime costs

The person who writes the manifest rarely knows what a minute of downtime costs. So nothing
about money is required, and the kit uses the best of three answers it has for each service:

| Known | Manifest | A minute down costs | Shown as |
|---|---|---|---|
| nothing | `criticality: critical`, `high`, `normal` (default) or `low` | no money; the weight of the tier orders services and pages | weighted minutes, lost requests |
| a rough figure | `cost: {monthly_revenue: 300000}`: what passes through the service in a month, a guess from the finance people is enough | the monthly figure spread over the month by the traffic of the service: a minute at the evening peak costs more than one at 4 a.m. | estimated money |
| a business metric | `cost: {value_query: <PromQL>}`: money per minute the service earns, e.g. the sum of paid orders | measured, see below | measured money |

Without any of these, a service whose [status mark](manifest.md#status-mark) is public counts
as `high`: its customers see the outage.

**Lost requests** need no input at all: for a service behind the [ingress](modules.md#ingress),
the successful requests during the incident are compared with the same minutes of the week over
the last four weeks. The difference is shown for every incident of every such service, money or
not.

**Measured money** works the same way on `value_query`: what the service earned during the
incident against the same minutes of the past four weeks, minus the surplus over that baseline
in the two hours after recovery, because some customers come back and pay later. What is left is
the loss that did not come back.

After every incident, and per service per month, the [journal](#the-incident-journal) and the
`Incidents` dashboard show the downtime in minutes, the lost requests and the money when there
is some, each labelled `measured`, `estimated` or `tier only`, so nobody mistakes a guess for a
measurement.

| Supporting metric | What it says |
|---|---|
| time to detect | first failed probe to alert; set by scrape intervals and the `for:` of the alert |
| time to first answer | alert to the first message with likely causes; target under 60 seconds |
| time to restore | alert to healthy again |
| time to acknowledge | alert to the first acknowledgement in GoAlert, per criticality, by day and by night; with the `oncall` module the [incidents](modules.md#incidents) journal measures it |
| resolved without a person | share of incidents the reconciler or the autopilot closed before anyone acknowledged; the journal counts the paged incidents whose GoAlert alert closed unacknowledged |
| night pages | how often a person was woken; the number that says whether on-call sleeps |
| hypothesis hit rate | which of the proposed causes turned out right, and at which rank, per class of what fixed it; see [The hit rate](#the-hit-rate) |
| harmful actions | actions after which the service got worse or failed again within 24 hours; must stay 0 |
| AI cost per incident | model, tokens and dollars per run, next to the downtime cost it saved |

## Layers, fastest first

| Layer | Handles | Model | Time |
|---|---|---|---|
| reconciler | a declared container is not running (a host rebooted, a container exited) | none | seconds |
| paging | a `critical` service, or one whose minute costs money, pages at once and does not wait for a diagnosis | none | seconds |
| first wave | the dossier as a message: what changed before the incident, which host resources are off, which dependencies answer | none | about 10 seconds |
| second wave | likely causes ranked with evidence and where to look, then hypotheses tested | Sonnet, Opus for critical | 30 to 90 seconds |
| a person | anything the layers above could not fix, or may not touch | | |

**The reconciler** runs on each host. The render says which containers the host must run;
when one is missing or exited and its compose project is declared, the reconciler runs
`docker compose up -d` for that project, writes the before and after to the journal, and backs
off: a container that it started three times within an hour is left for a person, with an
alert. It needs no model, no confirmation and no budget, because it only restores the state
the manifest already declares.

**Both waves go to the same on-call thread** as the page, so whoever wakes up reads what is
known and what was tried, and does not repeat it.

## Down or degraded

How much the autopilot may do depends on how broken the service already is:

| State | Decided by | The autopilot may |
|---|---|---|
| down | the external probe of the service fails, not only an internal metric | test hypotheses one after another with actions inside the [reversible envelope](#the-reversible-envelope), without confirmation |
| degraded | the service answers, but slowly or with errors | explain and propose; an action runs only after a confirmation |

A service that is down cannot be broken further by a reversible action; a degraded one can.
Neither state ever allows what lies outside the envelope: deleting volumes, changing database
data, rolling back migrations, anything irreversible.

## Hypotheses

The verdict of the second wave holds up to three hypotheses, most likely first, each with its
evidence and the action that tests it. Each runbook carries the hypotheses that usually explain
its alert, in order, with an action and whether that action can be undone; the model ranks and
explains them against the dossier and may add its own.

When the service is down, plain code walks the list:

1. Save the evidence first: the logs and `docker inspect` of the affected containers and the
   last hour of their metrics, so a restart does not destroy the cause.
2. Take a snapshot of what the action changes, run it, and watch the health of the service for
   a window (90 seconds by default).
3. Healthy: stop, report which hypothesis was right. Not healthy: restore the snapshot and take
   the next hypothesis.
4. Stop after the list, at the time box or at the budget, and say so in the thread.

The usual causes and the actions that test them:

| Cause | Action |
|---|---|
| a bad deploy or config change | go back to the previous render of the host; `aye deploy` keeps it |
| a host rebooted and a container did not start | the reconciler, before any model |
| a full disk | prune build cache and old logs, as the `hygiene` module does |
| a dependency is down | point at it; restart it only when its own allowlist says so |
| memory leak or OOM | restart the container, and show the memory trend to the person |
| an expired certificate, a broken DNS record | point at it only |

The budget of a run in down mode scales with what the downtime costs: at most a small share of
what one minute of downtime costs, never above the hard caps in [Spend limits](#spend-limits).

## The reversible envelope

An action that no allowlist names may still run when the service is down, if it stays inside
what the autopilot can undo:

| Can be undone | Cannot be undone by a snapshot |
|---|---|
| the rendered files of a host and its compose projects | data in a database or a volume, unless the filesystem takes snapshots |
| an image tag, an environment variable, a resource limit | mail sent, messages queued, calls to payment and other external APIs |
| starting, stopping and recreating a container | anything on another host or service |

The model never writes a shell command. It composes an action from typed primitives: set an
image tag, set an environment variable, set a memory or CPU limit, change a declared config
file (as a diff), recreate a container. Plain code checks that the action touches one service,
stays inside the envelope and fits the budget, takes the snapshot, runs it and restores it if
the health window fails. The confidence the model reports orders the hypotheses; it never
decides whether an action runs, because a model's stated confidence is not calibrated.

An action outside every allowlist that fixed an incident becomes a pull request against the
runbook of its alert. Once a person merges it, the action is allowed: every incident the
autopilot solves can widen what it may do next time.

## The incident journal

Each incident is one record, kept on the central host and exported as metrics for the
`Incidents` dashboard:

- the timeline: first failed probe, alert, page, first and second wave, each hypothesis with
  its start, end and result, healthy again, acknowledged by a person, closed;
- who fixed it: the reconciler, the autopilot (which hypothesis, which action) or a person;
- every model call: model, effort, input, output and cache tokens, tool calls, cost in USD
  (Claude Code reports `total_cost_usd` and the usage in its JSON result);
- the downtime in minutes, the lost requests and the lost money, labelled as in
  [What downtime costs](#what-downtime-costs).

The record is also the draft of the postmortem.

## The hit rate

Level 2 is switched on per incident class, and only when the hypotheses of that class have
earned it, so shadow mode labels every run after its incident: which hypothesis matched what
actually fixed it, and at which rank. The details are in [the module](modules.md#autopilot);
the choices behind them:

- **A class and a target, not prose.** A hypothesis carries `cause_class` (`container-down`,
  `bad-change`, `disk-full`, `host-down`, `dependency-down`, `cert-or-dns`, `other`) and the
  `target` it already had. The classes follow the usual causes above and the actions that test
  them, so the hit rate of `container-down` is the number that decides whether
  `restart-container` may run on its own. "Incident class" in the metrics is the class of what
  fixed the incident, not the alert, because one alert (`Website down`) has many causes and one
  cause many alerts; `alertname` stays a label for the cut by alert.
- **Signals the kit already has.** The reconciler restarting a container during the incident
  (its metric on every host, its own log lines when Loki is there for the container names), a
  host outage in the incident journal that ended right before the alert resolved, and a
  person's verdict. Each source is read-only, and the labeller waits 5 minutes after the
  resolve, so the last scrapes and log lines are in. A run no source explains is
  `unexplained`, not a miss: a hit rate over guesses would flatter or punish the model for
  what nobody measured.
- **The person's verdict uses the endpoint that was already there.** The agent already served
  an authenticated `/outcome` and an `outcome` command; it now takes the class and target of
  the real fix, and a `runs` command lists the runs to grade. A button in the oncall bridge's
  Telegram message would be nicer to tap at night, but the bridge's message is GoAlert's page,
  which knows no run, the verdict is posted only with `post_verdict`, and the bridge would need
  the autopilot's webhook secret: a new path between two modules for a grade given the next
  morning. A person's verdict wins over every automatic label.
- **Numbers from the journal.** The hit rate is recomputed from the journal at start, after
  every label and every verdict, so a restart loses nothing and a later verdict corrects an
  earlier label. It is the share of labelled runs whose matching hypothesis had at most that
  rank (rank 1, the top two, the top three), per class and for all; the
  `AyeOps / Incidents` dashboard shows it next to the downtime it is meant to shorten.

- **An acknowledgement is not a fix.** The [incidents](modules.md#incidents) record says who
  acknowledged in GoAlert and when. That a person was on it says nothing about what fixed
  the incident, so it never makes a label: the label line carries the acknowledgement (`at`,
  `by`, `via`, `seconds`), and a run no source explains but a person acknowledged is
  unexplained with reason `acknowledged` instead of `no-signal`. Those are the runs where a
  person's verdict through `/outcome` is missing and would count.

Not read yet: deploys during the incident from `aye report` (a fix by redeploy, the
`bad-change` class, is labelled only by a person), and actions a person took on the host by
hand.

## One run

1. **Trigger.** A `critical` alert from Grafana, or the first step of the on-call escalation
   policy. One run per alert fingerprint; a repeat of the same alert joins the open run.
2. **Dossier, no model.** Each runbook carries a machine-readable part: the metrics, log
   queries and containers that matter for its alert. Plain code collects them into a dossier of
   3 to 5 thousand tokens: the firing values and their last hour, logs grouped by signature with
   counts and two or three samples each plus 20 lines around the first occurrence, container
   state, the last deploys and commits. The model never receives a raw log.
3. **Bounded agent loop.** Claude Code in print mode (`claude -p`) with only the autopilot's
   own read-only MCP server: `query_metrics`, `search_logs`, `container_status`,
   `recent_changes`, `runbook`. Every tool caps its answer (at most about 50 series, log
   signatures instead of lines). The limits of a run are in [Spend limits](#spend-limits).
4. **Verdict.** `--json-schema` returns summary, evidence (values and signatures with their
   times), up to three [hypotheses](#hypotheses) with confidence, and for each either an
   action from the allowlist or the [envelope](#the-reversible-envelope) with its parameters,
   or "needs a person".
5. **Action, not by the model.** From level 2 plain code checks the action against the
   service's allowlist and its rate limits, records the state before and after, and runs it only
   after a confirmation in Telegram. The verdict is attached to the alert and to the on-call
   message.

## Models

| Stage | Model |
|---|---|
| dossier, log grouping | none, plain code |
| diagnosis | Sonnet, effort `medium` |
| escalation | Opus, effort `high`: when a service marked critical (database, backups, payments) is affected, when the Sonnet verdict has low confidence, or when the same alert repeats within 24 hours |
| action | none, plain code from the allowlist |

A small model is not used for diagnosis: deciding that a restart helps and breaks nothing is
the hard part, and the restart itself is one line of code.

## Locked down

Logs carry text that strangers wrote (listing titles, user input), so an instruction planted
in a log must not be able to do anything:

- `--tools ""` removes every built-in tool (shell, file edits, web), and
  `--strict-mcp-config` with `--allowedTools "mcp__autopilot__*"` leaves only the read-only
  MCP server; `--permission-mode dontAsk` denies anything else without waiting.
- The run starts in an empty working directory, so no project hooks or `.mcp.json` load.
- The model can only name an action from the allowlist; code decides whether it runs.

## Spend limits

A night incident must not spend the budget while nobody watches, so every level has a hard
cap, enforced by the autopilot's own code and not left to the model:

| Level | Default |
|---|---|
| one run | 12 tool calls, 3 minutes, `--max-budget-usd 0.50` |
| alert storm | one run per fingerprint, at most 4 runs an hour |
| quiet hours (23:00 to 08:00 by default) | 2 USD for the whole night |
| day | 3 USD |
| month | the provider budget of the `ai-usage` module |

A run that would cross a cap does not start, and a cap that is reached stops the runs until it
resets. There are no retries: the operator gets one message that the cap is reached and that
alerts keep coming without a diagnosis. Subscription runs count against the same caps with the
estimated cost Claude Code reports (`total_cost_usd`), so a night on the subscription cannot use
up the weekly allowance either.

## Paying for it

The `auth` setting picks how runs are paid:

| `auth` | Runs on |
|---|---|
| `api_key` (default) | an Anthropic API key, `AYE_AUTOPILOT_API_KEY`, under the spend limits |
| `subscription` | a Claude Pro or Max subscription through the one-year token of `claude setup-token`, `AYE_AUTOPILOT_OAUTH_TOKEN` |
| `subscription_then_api_key` | the subscription first; a run that hits the session or weekly limit, or fails on auth, runs again on the API key within the caps |

Anthropic's terms ask products and services built on Claude, the Agent SDK included, to use API
keys, and size Pro and Max limits for ordinary individual use. The subscription modes are for
an operator's own internal stack only, at their own risk; a kit installed for anyone else uses
`api_key`. The subscription limit is shared with the owner's own Claude Code work: a busy day of
development can leave nothing for a night incident, which is what
`subscription_then_api_key` covers.

Claude Code itself does not fall back between the two: with `ANTHROPIC_API_KEY` present a
print-mode run always bills the key. So the autopilot starts each subscription run without
that variable and decides the fallback itself, from the limit message in the JSON result.

Metrics: runs per `auth` path, limit hits, cap hits per level, the estimated cost of each run,
verdict confidence, and the days left on the subscription token, with an alert 30 days before
it expires.
