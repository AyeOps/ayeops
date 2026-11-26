# Off-site retention drifts from the policy

After a copy, `restic forget` kept other snapshots than the policy keeps, split them into more
than one group, or `restic prune` failed. The repository then grows past what the policy allows
until the quota stops it. Setup and commands: [offsite](offsite.md).

## What to do

1. `docker compose logs offsite | grep -E 'retention drift|forget failed|prune failed'`.
2. `retention drift: restic kept N in G groups` with G above 1: snapshots carry different host
   names, e.g. made by hand without `--host`. List them with
   `docker compose exec offsite /opt/restic/restic snapshots --group-by host`, forget the strays
   by id with `restic forget <id>`, then `restic prune`.
3. One group but another count than the policy: compare `restic snapshots` with the policy.
   Snapshots dated in the future (a clock jump) hold their buckets; forget them by id.
4. `prune failed`: usually the lock of a killed run, `restic unlock` and wait for the next copy;
   or the bucket is full, see [objectstore-quota](objectstore-quota.md), since prune needs a
   little free space to repack.

The alert clears after the next copy whose forget and prune match the policy.
