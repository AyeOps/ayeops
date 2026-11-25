# Incident journal agent stopped

The incident journal agent has not finished a round for 10 minutes, or its metrics are gone.
Its `/health` answers 503 in this state, so Docker marks it unhealthy. Grafana keeps the alert
state history meanwhile, so the agent catches up when it runs again.

## What to do

1. `docker compose ps incidents-agent` and `docker compose logs --tail 50 incidents-agent`:
   a crash at start (an unknown `AYE_INCIDENTS_TIMEZONE`, a broken `AYE_INCIDENTS_SERVICES`)
   or a `round_failed` line.
2. `docker compose restart incidents-agent`.
3. If it stalls again, look at the host: memory (the container is limited to 64 MB, and a
   journal of many thousand records is read into memory at start), CPU steal on a small VM.
4. If the metrics are gone but the container runs, check `target-down` for the `incidents`
   job and vmagent.
