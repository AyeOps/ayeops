# Backup job missed its schedule

A dump job (label `backup`) has no successful run within the longest gap of its cron
`schedule` plus `grace`: it failed every time since, it hangs, it never ran since it was
declared, or it runs and its metrics do not arrive. Silence counts as failure: there may be no
dump younger than the schedule promises. Setup: [backups](../modules.md#backups).

The label names a job of the manifest or a built-in dump, `secrets` (`infisical-backup`) or
`errors` (`glitchtip-backup`); for those two, start with
[backup-failed, built-in dumps](backup-failed.md#built-in-dumps).

## What to do

1. On the host, `cat /var/lib/ayeops/textfile/ayeops_backup_job_<backup>.prom` (the node
   `textfile_dir`):
   - no file: the job never ran through the wrapper. For a job with a `command`,
     `systemctl list-timers 'ayeops-*'` must list `ayeops-<host>-backup-<backup>.timer` with a
     next run; when it does not, the deploy did not install it (its output says
     `backup timers not installed`, and `aye doctor` names the reason: no systemd, or a helper
     host whose host-init predates the `timers` step). For your own cron line, `crontab -l -u
     root` must call `/opt/ayeops/<host>/backups/backup_run.sh ... <backup> ...` with exactly
     the name in the manifest.
   - `ayeops_backup_running 1` with an old `last_start_timestamp_seconds`: the run hangs or was
     killed. `systemctl status ayeops-<host>-backup-<backup>.service`, or
     `ps -ef | grep <dump tool>`; a killed run stays `running` until the next one, and a timer
     does not start a run while the previous one is still active.
   - `last_run_success 0`: the runs fail, see [backup-failed](backup-failed.md).
   - a recent success in the file but not in Grafana: node-exporter does not read that
     directory. `textfile_dir` of the node module must be the directory the wrapper writes to
     (`-d` or `AYE_TEXTFILE_DIR`, `/var/lib/ayeops/textfile` by default).
2. `cat /var/lib/ayeops/textfile/ayeops_backups.prom` lists the declared jobs with their
   `max_age_seconds`; `docker compose ps backup-jobs` must be up. A job with its own cron line
   whose schedule changed on the host but not in the manifest gives the wrong age: keep both the
   same, or give the job a `command` so the kit installs the schedule.
3. Run the job once: `systemctl start ayeops-<host>-backup-<backup>.service`, or the cron line.

The alert clears once a run succeeds and node-exporter has scraped the file.
