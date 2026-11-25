# No vulnerability scan finished for 2 days

`ayeops_vulns_last_run_timestamp_seconds` on this host is more than two days old: the daily
Trivy run of the [vulns](../modules.md#vulns) module has not finished, good or bad. Nothing is
known about vulnerabilities published since, and the counts on the host are as old as the last
run. Silence here is not a clean result.

An agent that has never run counts from its first start, so a new host stays quiet for two days
unless its first run fails.

## What to do

1. `docker compose ps vulns vulns-agent vulns-proxy`: all three should be running and healthy.
   `vulns` unhealthy means its loop has not touched `/state/loop.alive` for longer than two
   scans; `vulns-agent` unhealthy means the agent loop stopped.
2. `docker compose logs --tail 50 vulns`: a run that hangs on one image stops after
   `timeout_minutes` per image; many large images can make a run long, never two days.
3. `docker compose exec vulns-agent ls -l /state`: a `run.request` that stays means the loop
   does not pick requests up (restart `vulns`); no `run.last` means no run ever finished.
4. `node-exporter` reads the textfile: if it is down, `target-down` fires for it as well and
   this alert follows from it.
5. `docker compose exec -e AYE_VULNS_ONCE=1 vulns-agent python -I /app/vulns_agent.py` asks for a
   run now; the alert resolves on the next scrape after it finishes.
