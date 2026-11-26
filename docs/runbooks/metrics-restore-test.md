# Metrics store restore test failing

The last restore test of the metrics store backup failed, or none has passed within
`restore_test_days` plus one day. The test is the proof that the daily archive of
VictoriaMetrics opens as a store and holds the fleet's samples; without it, an archive can be
written every night and still be useless the day the store's disk dies. Setup and design:
[metrics, backup](../modules.md#backup-of-the-store).

The test runs in `victoriametrics-backup` on the metrics host. Commands run in the rendered
directory of that host; `<backup_dir>` is the module's `backup_dir`,
`/var/backups/ayeops/metrics` by default.

## What to do

1. Read the cause. Every test is one log line:

   ```sh
   docker compose logs victoriametrics-backup | grep 'restore test'
   ```

   - `cannot extract victoriametrics-<stamp>.tar`: the archive is truncated or the disk of
     `<backup_dir>` is full; the test needs room for one more copy of the store while it runs
     (`df -h <backup_dir>`, and [disk-full](disk-full.md)). A truncated archive also failed its
     dump: see [backup-failed](backup-failed.md).
   - `VictoriaMetrics did not start on ...`: the lines before it are the scratch instance's
     own log. `cannot open` or `missing parts.json` means the archive misses files of the
     snapshot: check that the `victoriametrics-data` volume is mounted at `/storage` in
     `victoriametrics-backup` and that nothing else deletes snapshots while the archive is
     written. Memory errors mean the container limit: raise
     `metrics.resources.victoriametrics-backup.memory` (and keep `aye check` clean).
   - `no series of up in the hour before ...`: the store opened, but it held no samples of
     `up` in the hour before the archive was taken. Either vmagent wrote nothing to the store
     then (look for [target-down](target-down.md) and
     [metrics-shipping-backlog](metrics-shipping-backlog.md) at that time), or the archive
     holds old data only, which points at the same missing files as above.
   - `cannot read the time of ...`: an archive was renamed by hand; the name carries the time
     the test queries.
   - no `restore test` line at all, and the alert says overdue: no archive exists yet, see
     [backup-missing](backup-missing.md).

2. After a fix, run the test now instead of waiting for the retry six hours later:

   ```sh
   docker compose stop victoriametrics-backup
   docker compose run --rm --no-deps --entrypoint rm victoriametrics-backup \
     -f /textfile/ayeops_backup_restore_metrics.prom
   docker compose up -d victoriametrics-backup
   docker compose logs -f victoriametrics-backup
   ```

   Without the textfile the test counts as never run and starts two minutes after the
   container, on the newest archive. `restore test passed` is the line to see.

The alert clears when a test passes.

## Restore the store from an archive

The same steps as the test, on the real store. The archive is a copy of one VictoriaMetrics
snapshot with its links resolved, so it unpacks into a directory that VictoriaMetrics opens as
its `-storageDataPath`.

1. Pick the archive: the newest file in `<backup_dir>`, or one restored from the off-site copy
   (`restic restore latest --target /tmp/metrics` on a host with the repository's password,
   see [offsite](offsite.md)).
2. Stop the store and its backup: `docker compose stop victoriametrics-backup victoriametrics`.
   vmagent on every host keeps buffering on disk meanwhile, up to `queue_max`.
3. Empty the volume and unpack the archive into it:

   ```sh
   docker compose run --rm --no-deps --entrypoint sh \
     -v <backup_dir>/victoriametrics-<stamp>.tar:/restore.tar:ro victoriametrics \
     -c 'rm -rf /storage/* && tar -xf /restore.tar -C /storage'
   ```

4. Start it again: `docker compose up -d victoriametrics victoriametrics-backup`. The samples
   between the archive and the restore that vmagent still buffered arrive on their own; the
   rest of that gap is lost.
