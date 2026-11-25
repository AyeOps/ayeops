# Disk above 90 %

A filesystem has been more than 90 % full for 5 minutes. Writes start failing soon: databases
stop, logs are lost, `docker pull` fails half-way.

## What to do

1. `df -h` and `docker system df` for a first split between docker and everything else.
2. Docker: run the [hygiene](../modules.md#hygiene) prune now,
   `docker compose exec -e AYE_HYGIENE_ONCE=1 hygiene python -I /app/hygiene_runner.py`, then
   look at `docker system df` again. Volumes are never pruned by default; find the large ones with
   `docker system df -v`.
3. Journal: `journalctl --disk-usage`; `journalctl --vacuum-size=500M` frees it now and
   `SystemMaxUse=` keeps it bounded, see the hygiene module.
4. Container logs: `du -sh /var/lib/docker/containers/*/*-json.log*`; a container not started by
   AyeOps may lack the `max-size` rotation its compose file never set.
