# Incident journal not updated

The incident journal agent has failed one of its steps for 10 minutes: `read` (the alert state
history from Grafana), `journal` (appending records and its state to the volume) or, with
`oncall`, `acks` (who acknowledged the paged incidents, read through the on-call bridge).
For `read` and `journal`, incidents of that time are not recorded yet and the `Incidents`
dashboard is behind. Nothing is lost while Grafana keeps its history: the agent reads from
where it stopped once the step works again. For `acks`, incidents still get recorded, but
those that close while it fails keep `acknowledged: null` for good, unless an earlier round
already saw their acknowledgement.

## What to do

1. `docker compose logs --tail 50 incidents-agent`: `read_failed` or `journal_failed` carries
   the error.
2. `read`, `HTTP 401`: Grafana refused the reader token in `/run/grafana-reader/token`, and the
   file held no newer one when the agent read it again. `grafana-reader` writes a new token on
   its next pass, within 5 minutes, and the agent picks it up without a restart; if
   `grafana-reader-failing` fires too, follow [that runbook](grafana-reader-failing.md).
   `HTTP 403` means the `Viewer` role lost a permission, same runbook.
   `TokenError: cannot read the reader token` or `holds no token`: the `grafana-reader` volume
   is not mounted or the reader has not written the token yet, `docker compose ps grafana-reader`.
3. `read`, a connection error: Grafana is down or restarting; see `target-down` for `grafana`.
4. `read`, `more than ... annotations`: an alert storm wrote more state changes than the agent
   reads in one window. Lower `backfill_days` or wait; the error names the limit it hit.
5. `journal`, `OSError`: the volume `incidents-journal` is full or read-only; check `df -h` on
   the host and `disk-full`.
6. `acks`, `HTTP 401`: the agent and the bridge hold different `AYE_ONCALL_ACKS_SECRET`; the
   `.env` of the host changed after one of them started. `docker compose up -d
   incidents-agent oncall-bridge` restarts both with the same value.
   `HTTP 503`: the bridge's setup has not created the alert log key yet; `docker compose logs
   oncall-bridge` shows `oncall-setup-retry` with the reason, and `ayeops_oncall_setup_ok` is 0.
   `HTTP 502`: GoAlert refused or did not answer the bridge; `failures_total{op="acks"}` of the
   bridge counts it, see `target-down` of `goalert` and [oncall-failing](oncall-failing.md).
   A connection error: the bridge is down, `docker compose ps oncall-bridge`.
7. Check by hand that Grafana answers the agent's token; this prints the HTTP status, never the
   token:

   ```sh
   docker compose exec incidents-agent python -c "import urllib.request as u; t = open('/run/grafana-reader/token').read().strip(); print(u.urlopen(u.Request('http://grafana:3000/api/annotations?type=alert&limit=5', headers={'Authorization': 'Bearer ' + t}), timeout=10).status)"
   ```
