# Off-site repository above 80 % of its quota

The restic repository of a `url` target uses more than 80 % of the `quota` set for it. At 100 %
the provider refuses writes and the copies stop. A `host` target reports through
[objectstore-quota](objectstore-quota.md) instead. Setup and commands: [offsite](offsite.md).

## What to do

1. `docker compose exec offsite /opt/restic/restic stats --mode raw-data` and
   `restic snapshots`: how many snapshots, how big. More snapshots than
   `keep_daily + keep_weekly + keep_monthly` is a retention problem, see
   [offsite-retention](offsite-retention.md).
2. `ayeops_offsite_last_added_bytes` of a normal day times the number of kept snapshots, plus
   one full dump, is the steady state. If that exceeds the quota, raise the quota with the
   provider and in `quota`, or lower `keep_weekly` and `keep_monthly`.
3. After lowering the policy, the next copy runs `forget` and `prune` and frees the space.
