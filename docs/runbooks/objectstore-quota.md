# Object store bucket near its quota, or quota not reconciled

`objectstore-quota`: the bucket uses more than 80 % of `quota`; at 100 % Garage refuses writes
and every off-site copy into it fails. `reststore-quota` is the same for the append-only
store (`provider: rest-server`), whose `--max-size` covers all its repositories together.
`objectstore-admin`: `garage-admin` could not read or set
the bucket through the admin API for an hour, so usage and quota are unknown. Setup and
commands: [offsite](offsite.md).

## What to do

1. `docker compose exec garage /garage bucket info <bucket>`: size, objects, quota. On
   rest-server, `ayeops_reststore_repo_bytes` per repository shows who grows; a client host
   writing far more than its dumps is a reason to look at that host first, since it cannot
   delete but can fill the store.
2. On each agent host writing into it, `restic snapshots` and `restic stats --mode raw-data`
   through the `offsite` container. More snapshots than the policy keeps is
   [offsite-retention](offsite-retention.md).
3. Copies of a database grow with it. Raise `quota` and redeploy; `garage-admin` applies it
   within 5 minutes, rest-server restarts with the new `--max-size`. Check the free space first, `df -h` on the host or on `data_dir`, and move
   the data to a [second disk](offsite.md#moving-the-data-to-a-second-disk) when the system disk
   gets tight.
4. `objectstore-admin`: `docker compose logs garage-admin`. `401` or `403` means
   `AYE_OBJECTSTORE_ADMIN_TOKEN` differs between `garage` and `garage-admin`; recreate both
   after changing it. `Connection refused` means Garage is down, see
   [objectstore-down](objectstore-down.md).
