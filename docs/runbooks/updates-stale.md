# No successful dependency update run for 2 days

`ayeops_updates_last_success_timestamp_seconds` on this host is more than two days old: the
daily Renovate run of the [updates](../modules.md#updates) module failed, did not reach every
repository, or did not run. New versions are not turning into pull requests.

An agent that has never succeeded counts from its first start, so a new host stays quiet for two
days.

## What to do

1. `docker compose logs --tail 20 updates-agent`: one JSON line per run with `success`,
   `exit_code`, `missing` (repositories absent from the report) and the open and pending counts.
2. `ayeops_updates_last_run_exit_code`: `124` is the `timeout_minutes` limit, `137` the memory
   limit (raise it with `resources`), `1` a Renovate error.
3. `docker compose logs --tail 200 updates | grep -E '"level":(50|60)'` shows Renovate's errors.
   `Bad credentials` or `401` means `AYE_UPDATES_TOKEN` expired or was revoked; `Repository is
   disabled` or a missing repository means the token does not cover it.
4. `docker compose exec -e AYE_UPDATES_ONCE=1 updates-agent python -I /app/updates_agent.py`
   asks for a run now; the alert clears on the next scrape once a run succeeds.
5. `ayeops_updates_last_run_timestamp_seconds` older than a day means the loop is not running:
   `docker compose ps updates updates-agent`.
