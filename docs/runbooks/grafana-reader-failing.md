# Grafana reader token does not work

`grafana-reader` on the Grafana host has not confirmed for 15 minutes that the token in the
`grafana-reader` volume reads every endpoint the agents read with it (alert rules, silences,
alerts, alert annotations). Until it does, the agents that mount that volume (the `status`
reporter, the `incidents` journal, the autopilot) see errors or less than Grafana holds, and the
status page and the journal stop following the alerts. The admin password and Grafana itself are
not affected. Background: [modules](../modules.md#grafana-reader).

1. Which endpoint, and why. On the Grafana host:

   ```sh
   docker compose logs --tail 30 grafana-reader
   docker compose exec grafana-reader python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:9137/metrics').read().decode())"
   ```

   Port 9137 is not published, hence the `exec`. `ayeops_grafana_reader_endpoint_ok{endpoint=...} 0`
   names the endpoint; the last log line says why. The token is never logged, and neither
   command prints it.

2. By what the log says:

   | Log | Meaning | Fix |
   |---|---|---|
   | `... answered HTTP 401` on `/api/serviceaccounts` | the admin password in `.env` is not the one Grafana has | Grafana keeps the password of its first start in `grafana-data`; put that one back into `AYE_GRAFANA_ADMIN_PASSWORD`, or reset it with `docker compose exec grafana grafana cli admin reset-admin-password`, then `docker compose restart grafana-reader` |
   | `Connection refused`, `timed out`, `HTTP 502`/`503` | Grafana is down or starting | see `target-down` for `grafana`; the reader catches up on its next pass, within 5 minutes |
   | `<endpoint>: the Viewer role is not enough (HTTP 403)` | a Grafana upgrade or a changed basic role took a permission from `Viewer` | compare with the table in [modules](../modules.md#grafana-reader); in OSS the role cannot be widened per account, so pin the previous Grafana version and open an issue |
   | `<endpoint>: the reader sees N of M` | a folder or dashboard no longer grants `Viewer` view, so its rules or their annotations are hidden from the reader | in Grafana, the folder's Permissions: give the role `Viewer` `View` again (the AyeOps folders have it by default) |
   | `Permission denied` or `Read-only file system` on `/tmp/token` | the volume was created by another container first and belongs to root | `docker compose stop grafana-reader` and every consumer, `docker volume rm <project>_grafana-reader`, `docker compose up -d` |

3. A stolen or leaked token: delete the file and let the reader write a new one; it removes every
   other token of the `aye-reader` service account when it does.

   ```sh
   docker compose exec grafana-reader rm -f /tmp/token
   docker compose restart grafana-reader
   ```

   Consumers read the file on every request, so they pick the new token up without a restart.

The alert clears once a pass succeeds: `ayeops_grafana_reader_ok 1` and a fresh
`ayeops_grafana_reader_last_success_timestamp_seconds`.
