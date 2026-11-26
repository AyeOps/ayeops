# Backup digest overdue

The sentinel should have delivered its daily backup digest more than 2 hours ago, about 26 hours
after the last one, and has not. The backups themselves may be fine: this alert says only that
the daily "all backups fresh" did not arrive, so a quiet chat no longer proves anything.

## What to do

1. `docker compose logs --tail 50 sentinel` on the sentinel's host. A `digest` line with
   `"delivered": false` next to `telegram_failed` lines means Telegram refused the message:
   follow [sentinel-notify-failing](sentinel-notify-failing.md), which fires with it.
2. No `digest` line at all since the due time: a `round_failed` line names the error that ended
   the round, no lines at all point to stopped rounds ([sentinel-stalled](sentinel-stalled.md)).
   `docker compose restart sentinel` moves the next digest to the next `at` and clears this
   alert.
3. Check the setting: `AYE_SENTINEL_DIGEST_AT` and `AYE_SENTINEL_DIGEST_TIMEZONE` in the
   rendered `compose.yaml` of the sentinel's host are the time you expect.
4. Until a digest arrives, look at the backups directly: the **Last good dump against its RPO**
   table of the Reliability dashboard, and the `backup-missing`, `backup-failed` and
   `offsite-stale` alerts.
