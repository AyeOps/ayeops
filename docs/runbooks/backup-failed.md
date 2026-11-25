# Backup job failed

The last run of a dump job (label `backup`) exited with an error, or exited 0 without leaving
a fresh, non-empty file at its output. The previous dump is still there; what is at risk is
today's copy, and the off-site copy of tonight copies yesterday's file again. Setup:
[backups](../modules.md#backups).

The label names either a job of the manifest (`backups.jobs.<name>`, run by its systemd timer
or by your own cron) or a built-in dump: `secrets` (the `infisical-backup` service) or `errors`
(`glitchtip-backup`), see [Built-in dumps](#built-in-dumps).

## What to do

1. On the host, `cat /var/lib/ayeops/textfile/ayeops_backup_job_<backup>.prom` (the node
   `textfile_dir`; for a built-in dump on a host without one,
   `docker compose exec node-exporter cat /textfile/ayeops_backup_job_<backup>.prom`):
   `ayeops_backup_last_exit_code` is the exit code of the dump command, `0` with
   `last_size_bytes 0` means it wrote nothing, `0` with a size means the output was not newer
   than the start of the run (the command wrote somewhere else).
2. The command's own output: `journalctl -u ayeops-<host>-backup-<backup>.service` for a job
   with a `command` in the manifest, the cron mail or `grep CRON /var/log/syslog` for your own
   cron line. The wrapper adds one line, `backup_run.sh: <backup> failed with exit code <n>`.
3. Usual causes: the database refused the login (a changed password), the disk is full
   (`df -h` and the `disk-full` alert), the job ran as a user that cannot write the output
   directory, the dump tool is a major version older than the server.
4. Run the job once: `systemctl start ayeops-<host>-backup-<backup>.service` for a job with a
   timer, the same line as the cron otherwise. The wrapper writes the metrics again, and a
   success clears the alert at the next scrape.

The alert clears once a run of the job succeeds. If the job left the manifest, its old file is
ignored: alerts only cover declared jobs. If it fails again by the next scheduled run,
[backup-missing](backup-missing.md) follows after the job's `grace`.

## Built-in dumps

`infisical-backup` and `glitchtip-backup` look for today's dump every 5 minutes and retry a
failed one on the next pass, so one failure right after a restart of the database is normal.

1. `docker compose ps infisical-backup infisical-db` (or `glitchtip-backup glitchtip-db`): both
   up and healthy?
2. `docker compose logs --tail 50 infisical-backup`: the output of `pg_dump` and of the
   wrapper. A wrong `AYE_SECRETS_DB_PASSWORD` (`AYE_ERRORS_DB_PASSWORD`) in `.env`, changed
   after the volume was created, is the usual lasting cause; the password of an existing volume
   changes only with `ALTER USER infisical PASSWORD '...'` inside `infisical-db`.
3. To dump now instead of waiting for the next pass:
   `docker compose exec -e AYE_BACKUP_ONCE=1 -e AYE_BACKUP_HOUR=0 infisical-backup sh /backup.sh`
   writes a dump only when today has none; delete today's file first to force one. Restore:
   [infisical](infisical.md#restore), [glitchtip](glitchtip.md#restore).
