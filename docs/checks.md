# Checks

A check watches one thing about a service without writing a module: an HTTP answer, an open
TCP port, a mail server's SMTP greeting, a DNS record, a script's exit code, or a number a
read-only SQL query returns. Declare it under the service, enable the
`checks` module on the host that runs it, and every check gets its metrics and its alert.

```yaml
hosts:
  central:
    modules:
      checks: {}
services:
  web:
    host: central
    checks:
      health:
        type: http
        url: https://acme.example/health
        expect_status: 200
        expect_body: '"status": ?"ok"'
        alert: {for: 3m, severity: critical, runbook: web-health}
      ssh:
        type: tcp
        address: 10.8.0.1:22
        alert: {for: 5m, severity: warning, runbook: ssh-down}
```

The check name is its key, `^[a-z][a-z0-9-]{0,39}$`; `<service>-<check>` is its id, the
`check` label of its metrics and the default name of its alert.

## Common fields

| Field | Default | Meaning |
|---|---|---|
| `type` | required | `http`, `tcp`, `smtp`, `dns`, `script` or `sql` |
| `alert` | required | `{for, severity, runbook}` are required by the rules (`alert-for`, `alert-severity`, `alert-runbook`); `name`, `title`, `summary` and `dashboard` (as for [service alerts](manifest.md#alert)) are optional; `sql` checks add their condition, see [sql](#sql) |
| `from` | the service host | host that runs the check; it needs the `checks` module (`check-module`) |
| `interval` | `60` | seconds between runs, `15` to `3600` (`86400` for scripts and SQL) |
| `timeout` | `10` | seconds, shorter than `interval` (`check-timing`) |

A service whose checks carry alerts passes `service-metrics` and `service-alerts` without a
`metrics` or `alerts` list of its own. Check alerts join the `services` rule group with the
label `service=<name>`, link `<runbook_base_url>/<runbook>` and fire when their query returns
nothing, like every service alert, so a check that stopped reporting is a firing alert.

## http

| Field | Default | Meaning |
|---|---|---|
| `url` | required | `http://` or `https://` URL |
| `method` | `GET` | `GET`, `HEAD` or `POST` |
| `expect_status` | any 2xx | a status code or a list of them |
| `expect_body` | none | regular expression the body must match |
| `via_proxy` | none | reserved for checks through a proxy, which the kit has no module for yet: such a check renders no probe, only its alert on `ayeops_proxy_target_success`, which nothing exports, so leave it out |

Redirects are followed and IPv4 is preferred. Metrics: `probe_success{check}`,
`probe_duration_seconds{check}`, `probe_http_status_code{check}`.

## tcp

| Field | Default | Meaning |
|---|---|---|
| `address` | required | `host:port` or `[ipv6]:port` |

The check passes when the connection opens. Metrics: `probe_success{check}`,
`probe_duration_seconds{check}`.

## smtp

```yaml
services:
  mail:
    host: central
    checks:
      mx:
        type: smtp
        address: mail.example.com:25
        alert: {for: 5m, severity: critical, runbook: mail-down}
      submission:
        type: smtp
        address: mail.example.com:587
        alert: {for: 5m, severity: critical, runbook: mail-down}
      smtps:
        type: smtp
        address: mail.example.com:465
        alert: {for: 5m, severity: warning, runbook: mail-down}
```

| Field | Default | Meaning |
|---|---|---|
| `address` | required | `host:port` or `[ipv6]:port` of the mail server |
| `tls` | `tls` on port 465, `starttls` on any other | `starttls`: greet, upgrade with `STARTTLS` and greet again over TLS, the probe fails when the server does not offer it; `tls`: TLS from the first byte; `none`: greet in plain text only, for a relay on port 25 where STARTTLS is optional or a relay on a trusted network |
| `ehlo` | the domain of [`mail.from`](manifest.md#mail) | the name the probe gives in `EHLO`, a host name such as `probe.example.com` |

The check is a mail server answering as one, not just an open port: blackbox-exporter's `tcp`
prober waits for the `220` greeting, sends `EHLO <ehlo>` and waits for `250`; with `starttls`
it then sends `STARTTLS`, waits for `220`, upgrades the connection, sends `EHLO` again and waits
for `250`; it ends with `QUIT`. It waits for the last line of a multi-line reply (`220 ` or
`250 `, not `220-`), so a server that delays its greeting, like Postfix's postscreen, sees a
client that waits its turn. With `tls` or `starttls` the certificate is verified against the
host of `address`, so name the server as its certificate does, not by its IP address.

`aye check` refuses `tls: tls` on 25 and 587, which greet in plain text, `starttls` or `none` on
465, which speaks TLS from the first byte, and an `smtp` check without `ehlo` in a manifest
without `mail` (`check-smtp`). Other ports, such as 2525, take any `tls`.

A server that answers with another code fails the check, at the latest when the timeout ends.
Metrics: `probe_success{check}`, `probe_duration_seconds{check}`,
`probe_failed_due_to_regex{check}` (`1` when the server closed the connection before the
expected code) and, with TLS, `probe_ssl_earliest_cert_expiry{check}`, which
[`check-tls-expiring`](runbooks/check-tls-expiring.md) watches.

To see what the probe sees, from the host that runs it:

```sh
nc -C mail.example.com 25
openssl s_client -starttls smtp -crlf -connect mail.example.com:587 -servername mail.example.com
openssl s_client -crlf -connect mail.example.com:465 -servername mail.example.com
```

Type `EHLO probe.example.com` once the server has greeted, after the `220` line with `nc` and
after the certificate summary with `openssl` (`-starttls smtp` has already sent `EHLO` and
`STARTTLS` itself), and expect `250` lines back; `Verify return code: 0 (ok)` in the summary is
what the probe needs of the certificate. `QUIT` ends the session.

## dns

| Field | Default | Meaning |
|---|---|---|
| `name` | required | the name to resolve |
| `record_type` | `A` | `A`, `AAAA`, `CNAME`, `MX`, `NS`, `TXT`, `SRV` or `CAA` |
| `expect` | none | regular expression at least one answer record must match, e.g. `10\.8\.0\.1` |
| `server` | module `dns_server` | resolver to ask, `host` or `host:port`; the module default is `1.1.1.1` |

The answer must be `NOERROR`. Metrics: `probe_success{check}`, `probe_duration_seconds{check}`.

## script

| Field | Default | Meaning |
|---|---|---|
| `command` | required | shell command, run with `/bin/sh -c` |
| `network` | `false` | `true` runs it in the runner that has a network |
| `mounts` | `[]` | `<host path>:<container path>` entries, always mounted read-only |
| `env` | `[]` | names of variables passed from the host `.env`; the values never reach the manifest or the rendered files |

Exit code `0` passes. Lines `<metric name> <number>` on stdout become
`ayeops_check_value{check,name}`; other lines are ignored. Metrics: `ayeops_check_success`,
`ayeops_check_exit_code` (`124` on timeout, `127` when the command cannot start),
`ayeops_check_duration_seconds`, `ayeops_check_last_run_timestamp_seconds`, all with
`check` and `service`. The alert fires on a failed run and when no run finished within two
intervals plus the timeout and a minute of scrape lag.

Script checks run in a small Python runner, `check-scripts` without network or
`check-scripts-net` with it, on `python:3.13.16-alpine3.23`: read-only root filesystem, a
16 MiB `/tmp`, every capability dropped except the two that let the runner switch to
`nobody` (uid 65534) before each command, `no-new-privileges`, 64 processes, 64 MiB. The command
gets `PATH`, `HOME=/tmp` and the variables listed in `env`, nothing else; it can use busybox and
Python. All script checks of one runner share its mounts. The runner writes its results to the
`node-textfile` volume, or the node module's `textfile_dir` when set, and node-exporter serves them, so script checks need the `node` module on
their host (`check-script-node`); a runner without a network could not be scraped otherwise.

## sql

```yaml
services:
  orders:
    host: central
    checks:
      queue-stuck:
        type: sql
        datasource: acme-orders
        query: select oldest_pending_sec as value from analytics.ops_health
        alert:
          op: ">"
          threshold: 900
          for: 10m
          severity: critical
          no_data: ok
          runbook: orders-queue-stuck
```

| Field | Default | Meaning |
|---|---|---|
| `datasource` | required | `uid` of a `postgres` entry under `modules.grafana.datasources` of any host (`check-sql-datasource`); the check reuses its `url`, `database`, `user`, `password_env` and `tls`, so the connection is written once |
| `query` | required | one `SELECT` or `WITH` statement that returns a numeric column `value` |
| `label` | none | a column of the query whose text becomes the `label` label of the value, one series per distinct label |
| `timeout` | `10` | seconds, `2` to `300` |
| `alert.op` | required without `baseline` | `>`, `>=`, `<`, `<=`, `==` or `!=`, the value against the threshold |
| `alert.threshold` | required without `baseline` | number |
| `alert.no_data` | `ok` | `alerting` fires when the query returns no row or a `NULL` value |
| `alert.baseline` | none | instead of `op`, `threshold` and `no_data`, compare the value with its usual level for the hour of the week, see [flow baselines](modules.md#flow-baselines) |

The alert fires when the value meets the condition, when the query fails, and when no run
finished within two intervals plus the timeout and a minute of scrape lag; with `label`, any
labelled value meeting the condition fires it. It compares with `bool`, so a healthy check
returns `0` instead of nothing and passes `alert-filter-nodata`.

Reading only is enforced three times. `aye check` refuses a query that does not start with
`SELECT` or `WITH`, holds a `;` before its end, a backslash (a psql command such as `\!`), or one of
`INSERT`, `UPDATE`, `DELETE`, `MERGE`, `TRUNCATE`, `DROP`, `ALTER`, `CREATE`, `GRANT`, `REVOKE`,
`COPY`, `CALL`, `INTO` as a word (`check-sql-query`); the guard is lexical, a column named
`update` trips it. The runner wraps the query as a subquery inside `BEGIN TRANSACTION READ ONLY`
with `SET LOCAL statement_timeout` one second under the timeout, then rolls back, so Postgres
itself rejects a write. And the role of the datasource should only be granted `SELECT`, which is
the guard that holds against functions with side effects.

Metrics, with `check` and `service`: `ayeops_check_sql_value` (plus `label` when set),
`ayeops_check_sql_ok` (`1` when psql exited `0`), `ayeops_check_sql_duration_seconds`,
`ayeops_check_sql_last_run_timestamp_seconds`. Without `label` only the first row counts.

SQL checks run in `check-sql` on `postgres:18.6-alpine3.24`: `psql` driven by a shell loop, one
query at a time so a role with a connection limit is not exhausted, read-only root filesystem, a
16 MiB `/tmp`, every capability dropped, `no-new-privileges`, 64 processes, 64 MiB. It gets the
`password_env` variables of its datasources from the host `.env`, connects with the application
name `ayeops-check-sql`, prints psql errors to its log and writes its results to the textfile
directory like the script runners, so SQL checks need the `node` module on their host
(`check-script-node`).

## The checks module

| Setting | Default | Meaning |
|---|---|---|
| `dns_server` | `1.1.1.1` | resolver of `dns` checks without `server` |
| `expose` | `[]` | `9116` publishes the check blackbox on `public_bind` |

It renders only what the checks of its host need: `check-blackbox` (blackbox-exporter `v0.29.1`
on port `9116`, one module and one scrape job per check, so each check keeps its own interval),
the two script runners and the SQL runner. Alerts: `target-down` for the blackbox,
[`check-tls-expiring`](runbooks/check-tls-expiring.md) when the certificate seen by an `https`
or TLS `smtp` check expires in less than 14 days, `check-runner-stale` when a runner has not
written results for 5 minutes.
