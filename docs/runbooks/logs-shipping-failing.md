# Alloy drops log lines

Alloy on `host` gave up on log lines after its retries ran out: the lines of that interval are
not in Loki. Alloy keeps tailing; new lines arrive once Loki answers again.

## What to do

1. Is Loki up? `target-down` with `job="loki"` fires at the same time when it is not; see
   [target-down](target-down.md).
2. From the host, `curl -s -o /dev/null -w '%{http_code}' http://<loki address>:3100/ready`
   should print `200`. A timeout points at the private network or the `expose` of the Loki host.
3. `docker compose logs --tail 100 alloy` on the host for the push errors: `429` means Loki rate
   limits (see [logs-ingestion-errors](logs-ingestion-errors.md)), `5xx` means Loki itself fails.
4. Disk of the Loki host full? See [disk-full](disk-full.md); Loki stops accepting writes.
