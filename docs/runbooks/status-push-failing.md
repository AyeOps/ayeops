# Status page reporter cannot push

The [status reporter](../modules.md#status) has not had a push accepted by the status page
Worker for 5 minutes. The Worker shows every component that waits for pushes as **no data**
after the same 5 minutes, so the public page has lost the stack's view of them. The alert also
fires when the reporter stopped exporting its metrics at all (no data counts as firing).

## What to do

1. `docker compose logs --tail 20 status-reporter`: each failed round logs one `push_failed`
   line with `reason`, the HTTP `status` and the Worker's answer in `detail`.
2. Go by the reason and status:

| `reason` / `status` | Meaning | Fix |
|---|---|---|
| `refused` / `401` `signature does not verify` | `AYE_STATUS_PUSH_SECRET` differs from the tenant secret in D1, or the tenant has none | set the same value on both sides: `aye status deploy` for D1 ([status.md](../status.md#deploy)), the host's `.env` for the reporter, then `docker compose up -d status-reporter` |
| `refused` / `401` `ts is more than 300 s away` | the host clock is off by more than 5 minutes | `timedatectl status`; enable NTP (`timedatectl set-ntp true`) |
| `refused` / `422` `unknown components` | the manifest has a component the D1 seed does not | run `aye status deploy`, which seeds the components D1 is missing ([status.md](../status.md#deploy)) |
| `refused` / `400` `tenant does not match the hostname` | `status_page.hostname` routes to another tenant | fix the hostname in the manifest or the `tenants` row |
| `refused` / `404` | the hostname does not route to the Worker | the routes `aye status deploy` prints and the custom domain in the Cloudflare dashboard |
| `transient` / `0` | DNS, TLS or the connection failed three times in a row | `docker compose exec status-reporter python -c "import urllib.request; urllib.request.urlopen('https://<status hostname>/api/status', timeout=10)"`; outbound HTTPS from the host |
| `transient` / `429` or `5xx` | Cloudflare or the Worker failed | the Worker's logs (`npx wrangler@4.148.0 tail`) and the Workers and D1 free-tier limits in [status.md](../status.md#free-tier-budget) |

3. No `push_failed` lines and no recent lines at all: the loop stalled or the container is
   gone. `docker compose ps status-reporter`, then `docker compose restart status-reporter`.

`ayeops_status_push_failures_total{reason}` counts failed rounds; a `refused` one never heals by
itself, a `transient` one usually does.
