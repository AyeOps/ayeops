# Off-site copy stale

No successful off-site copy for 26 hours, or for the shorter `rpo` of a service whose
[`data`](../manifest.md#data) copies its dump off-site (`ayeops_offsite_max_age_seconds` holds
the threshold), or no `ayeops_offsite_last_success_timestamp_seconds` reaches the metrics store
at all. The local dumps may be fine; what is missing is the copy in
another place. Setup and commands: [offsite](offsite.md).

## What to do

1. `docker compose ps offsite offsite-restic` on the host with the dumps: both up?
2. `docker compose logs --tail 50 offsite`. The agent names the cause of every failed run:
   - `the repository does not exist yet: run restic init`: the first run was skipped, see
     [first run](offsite.md#first-run), step 4.
   - `wrong AYE_OFFSITE_RESTIC_PASSWORD`: the `.env` does not hold the password the repository
     was created with. Take it from the password manager; never `restic init` a second time
     over a repository that has data.
   - `restic snapshots failed (1): ... connection refused` or a timeout: the objectstore or the
     private network is down, see [objectstore-down](objectstore-down.md). From the agent host,
     `curl -s -o /dev/null -w '%{http_code}' http://<objectstore address>:3900/` must answer.
   - `no settled file matches <source>`: the local dump did not run, or wrote elsewhere. Check
     the host's own backup job first.
   - `restic backup failed` with `Quota exceeded` or `403`: the bucket is full, see
     [objectstore-quota](objectstore-quota.md).
   - `changed while it was backed up`: the dump was still being written; raise
     `settle_minutes` or move `schedule` later.
3. No log line for a day: the agent sleeps between slots and runs one pass a minute.
   `cat <textfile dir>/ayeops_offsite.prom` shows `last_attempt_timestamp_seconds`; a fresh file
   with a stale metric in Grafana means node-exporter does not read that directory
   (`textfile_dir` of the node module).
4. To retry now instead of after `retry_minutes`:
   `docker compose exec offsite rm /state/state.json`; the next pass, within a minute, copies.

The alert clears once a copy succeeds and node-exporter has scraped the textfile.
