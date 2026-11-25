# Reconciler stopped checking

`ayeops_reconciler_last_run_timestamp_seconds` on this host is more than 5 minutes old: the
[reconciler](../modules.md#reconciler) has not finished a pass over the declared containers. A
container that goes down now stays down until a person or Docker's own restart policy brings it
back.

## What to do

1. `docker compose ps reconciler` and `docker compose logs --tail 50 reconciler` in the rendered
   project of the host. A pass that could not read Docker logs one line with
   `"event": "error"` and the reason.
2. `docker compose exec -e AYE_RECONCILER_ONCE=1 reconciler python -I /app/reconciler.py` runs
   one pass now and writes the textfile; the alert clears on the next scrape once it succeeds.
3. `permission denied` or `connection refused` on `/var/run/docker.sock`: the socket is not
   reachable as root inside the container, for example a rootless docker with its socket
   elsewhere.
4. The container runs and logs no error, but the metric stays old: the textfile directory is not
   the one node-exporter reads, or node-exporter is down (`target-down`, `host-down`).
5. A pass that hangs: an attempt waits at most `timeout` (2 minutes) for its containers and each
   Docker call at most 30 seconds, so a pass much longer than that points at a daemon that does
   not answer; `docker info` on the host tells.
