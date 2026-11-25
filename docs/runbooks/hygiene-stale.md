# Docker prune has not succeeded for 2 days

`ayeops_hygiene_last_success_timestamp_seconds` on this host is more than two days old: the
nightly prune of the [hygiene](../modules.md#hygiene) module failed, or its container is not
running. Unused images and build cache are piling up again.

A runner that has never succeeded counts from its first start, so a new host stays quiet for two
days.

## What to do

1. `docker compose ps hygiene` and `docker compose logs --tail 50 hygiene`. Each run logs one
   JSON line with `success`, `error`, the bytes per kind and the before and after numbers.
2. `docker compose exec -e AYE_HYGIENE_ONCE=1 hygiene python -I /app/hygiene_runner.py` runs one
   prune now and writes the textfile; the alert clears on the next scrape once it succeeds.
3. An error such as `403` or `permission denied` on `/var/run/docker.sock` means the socket is
   not reachable as root inside the container, for example a rootless docker with its socket
   elsewhere.
4. `ayeops_hygiene_last_run_timestamp_seconds` newer than the last success means it runs and
   fails; older means the container is down or the textfile directory is not the one
   node-exporter reads.
