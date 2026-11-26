# Append-only backup store down

`reststore-down`: `reststore-admin` could not read rest-server's `/metrics` as the `metrics`
user for 5 minutes, or the admin itself stopped reporting. Off-site copies and restores of
every client fail until it is back. Setup and commands:
[append-only off-site copies](offsite-append-only.md).

## What to do

1. On the store host: `docker compose ps rest-server reststore-admin` and
   `docker compose logs --tail 50 rest-server`.
2. rest-server exits at start with an `htpasswd` error: a variable it builds a user from is
   empty or missing in `.env` (`AYE_RESTSTORE_<HOST>_PASSWORD`,
   `AYE_RESTSTORE_METRICS_PASSWORD`). `docker compose config` names the missing one.
3. rest-server runs but the admin logs nothing useful and `ayeops_reststore_server_up` is 0:
   the `metrics` password differs between the two services. Both read
   `AYE_RESTSTORE_METRICS_PASSWORD`; recreate both after changing it.
4. A full disk or a full `--max-size`: `df -h` on the host or on `data_dir`, and
   [objectstore-quota](objectstore-quota.md).
5. From a client: `curl -s -o /dev/null -w '%{http_code}\n' http://<store address>:8000/`
   answers `401` when the server is up and reachable.

The alert clears once the admin reads `/metrics` again.
