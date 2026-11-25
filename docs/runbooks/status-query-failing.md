# Status page reporter cannot read Grafana

The [status reporter](../modules.md#status) has failed to read the alert rules or the silences
from Grafana for 5 minutes. It keeps pushing, but every component it feeds shows **no data** on
the public status page, because without the alerts it cannot tell whether they work.

## What to do

1. `docker compose logs status-reporter | grep query_failed`: the `error` names the failure.
2. Go by the error:

| Error | Fix |
|---|---|
| `answered HTTP 401` | Grafana refused the reader token in `/run/grafana-reader/token`, and the file held no newer one when the reporter read it again. The `grafana-reader` service writes a new token on its next pass, within 5 minutes; if `grafana-reader-failing` fires too, follow [that runbook](grafana-reader-failing.md) |
| `answered HTTP 403` | the `Viewer` role of the reader lost a permission, for example after a Grafana upgrade: see [grafana-reader-failing](grafana-reader-failing.md) |
| `cannot read the reader token`, `holds no token` | the `grafana-reader` volume is not mounted or the reader has not written the token yet: `docker compose ps grafana-reader`, then [grafana-reader-failing](grafana-reader-failing.md) |
| `answered HTTP 5xx`, a timeout, `URLError` | Grafana is down or starting: `docker compose ps grafana`, `docker compose logs --tail 50 grafana` |
| `JSONDecodeError` or `the rules answer ...` | Grafana answered something else than its rules API, for example a sign-in page; check that `AYE_STATUS_GRAFANA` is `http://grafana:3000` |

3. Once a read succeeds the log says `query_ok` and the next push carries the real states.
