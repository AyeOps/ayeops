# Off-site retention on the store failed

`reststore-maintenance`: for one repository (`repo` label) the daily `forget` and `prune` on
the store host failed, kept more snapshots than the client's policy, or has not passed for 50
hours. Append-only clients cannot delete, so until it passes the repository only grows.
Setup and commands: [append-only off-site copies](offsite-append-only.md).

## What to do

1. `docker compose logs reststore-admin | grep <repo>` on the store host: the line names the
   failing step.
2. `AYE_RESTSTORE_<HOST>_RESTIC_PASSWORD is not set`, or `wrong password` from restic: the
   store's key for that repository is missing or wrong. Put the right one into `.env`, or make
   a new one with `restic key add` ([a key for the store](offsite-append-only.md#a-key-for-the-store)),
   and redeploy.
3. `forget failed` or `prune failed` with `repository is already locked`: a client's backup or
   restore test was running. The admin runs `unlock` first, which clears stale locks only; it
   retries after an hour. If it keeps colliding, move `prune_schedule` away from the client's
   `schedule`.
4. `restic kept N snapshots in G groups` with G above 1: snapshots carry another host name,
   e.g. made by hand without `--host`. List them on the store with
   `docker compose exec reststore-admin sh -c 'RESTIC_PASSWORD="$AYE_RESTSTORE_<HOST>_RESTIC_PASSWORD" /opt/restic/restic -r /data/<repo> snapshots --group-by host'`
   and forget the strays by id the same way.
5. `prune failed` with no space left: the store is full, see
   [objectstore-quota](objectstore-quota.md); prune needs a little free space to repack.
6. Not run for 50 hours with no failure: the admin is not running or its restic binary is
   missing. `docker compose ps reststore-admin reststore-restic`.

The alert clears after the next run whose forget and prune pass for that repository.
