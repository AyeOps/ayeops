# Off-site restore drill failing

The last restore drill of a client's off-site copy failed, or none has passed within its
`every_days` plus one day. The drill is the only automated proof that the copy restores into a
working database, and the duration it records is the measured time to get the data back. Setup
and the design: [offsite](../modules.md#restore-drill), operating the copies:
[offsite](offsite.md).

The drill runs in `offsite-drill` on the store host (`central` below); the client host (`edge`)
plays no part, on purpose: the drill rehearses the day `edge` is gone. Commands run in the
rendered directory of `central`.

## What to do

1. Read the cause. Every failure is one log line:

   ```sh
   docker compose logs offsite-drill | grep 'drill failed\|check '
   ```

   - `AYE_RESTSTORE_EDGE_RESTIC_PASSWORD is empty`: the store's key to that repository is
     missing from `central`'s `.env`. It is the same variable `reststore-admin` uses.
   - `restic ls: ...` or `restic dump: ...`: restic's own message. `wrong password` means the
     store's key was removed from the repository (`restic key list` from `edge`);
     `repository does not exist` means `edge` never ran `restic init`; a missing pack or
     `file does not exist` that comes and goes points at a `prune` of `reststore-admin` running
     at the same time: move `restore_drill.schedule` away from `prune_schedule`.
   - `no snapshot of edge with a file`: no copy has arrived yet, see
     [offsite-stale](offsite-stale.md).
   - `restored sha256 ... differs from the recorded ...`: the bytes on the store are not the
     bytes `edge` sent. Run `restic check --read-data` as in
     [offsite-restore-test](offsite-restore-test.md) and check the disk of `central`. A copy
     whose file changed while it was uploaded also lands here until the next good copy.
   - `is not a pg_dump custom-format archive`: `source` matches something else than
     `pg_dump -Fc` files.
   - `initdb`, `postgres did not start`, `createdb`: the scratch storage. `No space left on
     device` means the `/scratch` volume's disk is full; the drill needs room for the dump plus
     the restored database. Memory errors mean the container limit: raise
     `objectstore.resources.offsite-drill.memory` on `central` (and keep `aye check` clean).
   - `pg_restore: ...`: the dump does not restore. This is the finding the drill exists for.
     Typical causes: an extension the stock `postgres` image lacks (PostGIS, TimescaleDB), a
     `version` older than the server that made the dump, a collation the image does not have.
     Restore the same file by hand (the [manual drill](offsite.md#restore-drill)) to see the
     whole error output, and fix the dump or the drill's `version`.
   - `check <name> returned '<value>'`: the database restored, and a check found it wrong:
     `0` or a negative number, an empty result, or the error psql printed. A row count of 0
     means the table is empty in the dump; a freshness check of 0 means the newest row is
     older than the check allows, so the dump is old (see whether the dump job on `edge`
     still runs).
   - `stopped after <n> seconds`: the restore took longer than `max_minutes`. Either the
     database grew (raise `max_minutes` and look at `ayeops_offsite_drill_duration_seconds`,
     it is your recovery time) or something hangs (`docker compose logs offsite-drill`).

2. After a fix, run the drill now instead of waiting for the next `schedule`:

   ```sh
   docker compose stop offsite-drill
   docker compose run --rm --no-deps --entrypoint rm offsite-drill \
     -f /textfile/ayeops_offsite_drill_edge.prom
   docker compose up -d offsite-drill
   docker compose logs -f offsite-drill
   ```

   Without the textfile the drill counts as never run and starts within a minute.
   `drill passed in <n>s` is the line to see.

3. If the drill keeps failing and you need to know whether the copy restores at all, do the
   [manual drill](offsite.md#restore-drill): it uses the same repository and shows every
   message.

The alert clears when a drill passes.
