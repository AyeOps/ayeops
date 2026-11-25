# Append-only off-site copies (rest-server)

Operating the [objectstore](../modules.md#rest-server-append-only) with
`provider: rest-server`: first run, the store's own key, moving from Garage, and what to check.
The alert pages are [reststore-down](reststore-down.md),
[reststore-maintenance](reststore-maintenance.md) and
[objectstore-quota](objectstore-quota.md) (`reststore-quota`). The client side is the same
[offsite](offsite.md) agent.

The examples use `central` for the store host and `edge` for the host with the dumps. Commands
run in the rendered host directory, next to its `compose.yaml` and `.env`. `EDGE` in a variable
name is the client's host name upper-cased, `-` written as `_`.

## Why

With Garage the agent's S3 key can delete. Whoever takes over `edge` (ransomware, a stolen
deploy key) runs `restic forget --keep-last 0 --prune`, or deletes the bucket's objects, and
the local dumps and the off-site copy are gone together. With rest-server in `--append-only`
mode the same attacker can add snapshots and fill the quota, and every old snapshot stays.
Deleting moves to `central`, which never takes orders from `edge`.

## First run

1. Manifest: on `central`

   ```yaml
   objectstore:
     provider: rest-server
     quota: 40g
     expose: [8000]
     prune_schedule: "11:00"
   ```

   and on `edge` `offsite: {target: {host: central}, ...}` as before. `central` needs `address`
   and `public_bind` on the private network (Tailscale). `aye check` must be clean.

2. On `central`, generate the store's variables into its `.env`. The client's repository key
   comes in step 6; until then the client's own password stands in for it:

   ```sh
   echo "AYE_RESTSTORE_METRICS_PASSWORD=$(openssl rand -base64 33)" >> .env
   echo "AYE_RESTSTORE_EDGE_PASSWORD=$(openssl rand -base64 33)" >> .env
   echo "AYE_RESTSTORE_EDGE_RESTIC_PASSWORD=<edge's AYE_OFFSITE_RESTIC_PASSWORD>" >> .env
   chmod 600 .env
   ```

3. On `edge`, its `.env` gets the same rest-server password. A new client also gets its
   restic password, `echo "AYE_OFFSITE_RESTIC_PASSWORD=$(openssl rand -base64 33)" >> .env`;
   one moving from Garage keeps the one it has:

   ```sh
   echo "AYE_OFFSITE_REST_PASSWORD=<central's AYE_RESTSTORE_EDGE_PASSWORD>" >> .env
   ```

4. Render and deploy `central`, then `edge`. Check from `edge`:

   ```sh
   curl -s -o /dev/null -w '%{http_code}\n' http://<central address>:8000/
   ```

   `401` is right (alive, wants a password); a timeout means `expose` or the firewall.

5. Create the repository from `edge` and let the agent copy:

   ```sh
   docker compose run --rm offsite /opt/restic/restic init
   docker compose logs -f offsite
   ```

   `backed up <file>` and later `restore test passed` are the lines to see.

6. <a id="a-key-for-the-store"></a>**A key for the store.** Give `central` a restic key of its
   own, so `edge`'s password does not have to live there:

   ```sh
   docker compose exec reststore-admin sh -c \
     'RESTIC_PASSWORD="$AYE_RESTSTORE_EDGE_RESTIC_PASSWORD" /opt/restic/restic -r /data/edge key add'
   ```

   restic asks for the new password twice; use `openssl rand -base64 33`. Put it into
   `AYE_RESTSTORE_EDGE_RESTIC_PASSWORD` in `central`'s `.env` and redeploy `central`. Both keys
   now open the repository; `restic key list` shows two. Removing either (from `central`,
   `restic -r /data/edge key remove <id>`) never needs the other host. Keep both passwords in
   a password manager: either one restores everything.

## Prove it is append-only

Run once after the first copy, and after any change to the store:

```sh
docker compose run --rm offsite /opt/restic/restic snapshots
docker compose run --rm offsite /opt/restic/restic forget <one snapshot id>
```

The `forget` must fail with `Forbidden` (HTTP 403) and the snapshot must still be listed. If
it succeeds, the store is not in append-only mode: check that `--append-only` is in
`docker compose exec rest-server cat /proc/1/cmdline | tr '\0' ' '` on `central`.

## What runs where

| Step | Host | Why there |
|---|---|---|
| `backup` | `edge` | the dump is there |
| `snapshots`, `stats` | `edge` | read-only, for its own metrics |
| restore test (`dump`, `check`) | `edge` | read-only; it tests the path a real restore takes |
| restore drill (`ls`, `dump`, `pg_restore`) | `central` | rehearses the loss of `edge`; reads the repository read-only from disk, see [restore drill](../modules.md#restore-drill) |
| `forget`, `prune` | `central` | needs delete rights, which only the store has |

The trade-off: `central` holds a key to the repository, so a taken-over `central` can read and
delete the copies. It could always delete them (it holds the disk). Keep `central` the most
locked-down host: no CI runner, no inbound ports beyond the private network, SSH keys that do
not live on `edge`.

## Moving from Garage

The old Garage copy stays readable until the end, so the move has no window without an
off-site copy beyond one day.

1. Note what exists: on `edge`, `docker compose run --rm offsite /opt/restic/restic snapshots`.
   Decide whether the history is worth copying. A few snapshots of a database that is also
   dumped locally every day are usually not; the new repository fills its policy in a week.
   To keep them, run `restic copy` before step 3 from a host that can reach both stores.
2. Steps 2 and 3 of the first run: the new variables. Keep the S3 ones for now.
3. Change the manifest of `central` to `provider: rest-server`, `expose: [8000]`, and drop
   `bucket`, `region` and `meta_dir` (`aye check` names any left). Keep `data_dir` only if it
   points at a new, empty directory: Garage's blocks are not a restic repository.
4. Deploy `central`. `--remove-orphans` stops `garage` and `garage-admin`; their volumes
   (`objectstore-data`, `objectstore-meta`) or host directories stay untouched.
5. Deploy `edge`. Its agent state still holds today's Garage success, so make it copy now:

   ```sh
   docker compose run --rm offsite /opt/restic/restic init
   docker compose stop offsite
   docker compose run --rm offsite rm -f /state/state.json
   docker compose up -d offsite
   docker compose logs -f offsite
   ```

6. [Prove it is append-only](#prove-it-is-append-only), then step 6 of the first run.
7. Wait for the first `reststore-admin` run at `prune_schedule`:
   `docker compose logs reststore-admin` on `central` shows `edge: forget and prune passed`.
8. Retire Garage once `edge` reports `restore test passed` against the new repository:
   remove `AYE_OFFSITE_ACCESS_KEY_ID` and `AYE_OFFSITE_SECRET_ACCESS_KEY` from `edge`'s `.env`
   and the four `AYE_OBJECTSTORE_*` variables from `central`'s, then
   `docker volume rm <project>-central_objectstore-data <project>-central_objectstore-meta`
   (or the host directories). With the key gone from `edge` there is nothing left on it that
   can delete an off-site copy.

**Rolling back** before step 8: revert the manifest of both hosts and redeploy; Garage starts
on its untouched volumes with the same key.
