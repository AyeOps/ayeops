# Off-site restore test failing

The last restore test of the off-site copy failed, or none has passed for 8 days. A copy that
cannot be read back is no copy. Setup and commands: [offsite](offsite.md).

## What to do

1. `docker compose logs offsite | grep 'restore test'` names the broken link:
   - `restored sha256 ... differs from the recorded ...`: restic returned other bytes than it
     stored. Run `docker compose exec offsite /opt/restic/restic check --read-data` (it reads
     every pack, so it downloads the whole repository) and look for damaged packs; check the
     disk of the objectstore host (`smart`, `dmesg`).
   - `does not start with the pg_dump custom-format header PGDMP`: the copied file is not a
     custom-format dump. Check `source`; a plain SQL dump or a gzip archive needs
     `verify: none`.
   - `the snapshot has no sha256= tag`: the snapshot was made by hand or by another tool; the
     next scheduled copy carries the tag.
   - `restic dump failed` or `restic check failed`: the text after the code is restic's own. A
     lock left by a killed run clears with `restic unlock`; a network error points to
     [objectstore-down](objectstore-down.md).
   - `no snapshot to restore`: see [offsite-stale](offsite-stale.md).
2. After a fix, force a test: `docker compose exec offsite rm /state/state.json`; the next pass
   copies and tests again.
3. If the test keeps failing, do the [restore drill](offsite.md#restore-drill) by hand: it
   shows whether the copies are readable from outside the agent.

The alert clears when a restore test passes.
