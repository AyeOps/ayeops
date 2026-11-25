# Off-site backups (objectstore and offsite)

Operating the [objectstore](../modules.md#objectstore) and [offsite](../modules.md#offsite)
modules: first run, key rotation, the restore drill, moving the data. The alert pages are
[offsite-stale](offsite-stale.md), [offsite-restore-test](offsite-restore-test.md),
[offsite-drill](offsite-drill.md),
[offsite-quota](offsite-quota.md), [offsite-retention](offsite-retention.md),
[objectstore-down](objectstore-down.md) and [objectstore-quota](objectstore-quota.md). The
append-only store (`provider: rest-server`) has its own page:
[append-only off-site copies](offsite-append-only.md), with
[reststore-down](reststore-down.md) and [reststore-maintenance](reststore-maintenance.md).

The examples use `central` for the host with the objectstore and `edge` for the host with the
dumps. Commands run in the rendered host directory, next to its `compose.yaml` and `.env`.

## First run

1. On `central`, generate the objectstore variables into its `.env`:

   ```sh
   echo "AYE_OBJECTSTORE_RPC_SECRET=$(openssl rand -hex 32)" >> .env
   echo "AYE_OBJECTSTORE_ADMIN_TOKEN=$(openssl rand -base64 32)" >> .env
   echo "AYE_OBJECTSTORE_ACCESS_KEY_ID=GK$(openssl rand -hex 12)" >> .env
   echo "AYE_OBJECTSTORE_SECRET_ACCESS_KEY=$(openssl rand -hex 32)" >> .env
   chmod 600 .env
   ```

2. Render, copy, `docker compose up -d`. On its first start Garage creates the single-node
   layout, the access key and the bucket; `garage-admin` sets the quota a few seconds later.
   Check:

   ```sh
   docker compose exec garage /garage status
   docker compose exec garage /garage bucket info <bucket>
   docker compose logs garage-admin
   ```

   `bucket info` lists the key with `RWO` rights and `Size quota: 40.0 GiB` (or your `quota`).
   From `edge`: `curl -s -o /dev/null -w '%{http_code}\n' http://<central address>:3900/`
   answers `403` or `400` (S3 without credentials), not a timeout.

3. On `edge`, put the agent variables into its `.env`. The two S3 values are the objectstore's
   `AYE_OBJECTSTORE_ACCESS_KEY_ID` and `AYE_OBJECTSTORE_SECRET_ACCESS_KEY`:

   ```sh
   echo "AYE_OFFSITE_RESTIC_PASSWORD=$(openssl rand -base64 33)" >> .env
   echo "AYE_OFFSITE_ACCESS_KEY_ID=GK..." >> .env
   echo "AYE_OFFSITE_SECRET_ACCESS_KEY=..." >> .env
   chmod 600 .env
   ```

   Store `AYE_OFFSITE_RESTIC_PASSWORD` in a password manager or on paper now. It is the only
   key to the copies, and the `.env` that holds it is on the host whose loss they are for.

4. Create the repository once:

   ```sh
   docker compose up -d offsite-restic
   docker compose run --rm offsite /opt/restic/restic init
   ```

   `created restic repository ... at s3:http://...` is the answer. Without this step the agent
   logs `the repository does not exist yet: run restic init` and `offsite-stale` fires.

5. `docker compose up -d offsite`. The first copy starts within a minute, because no copy
   exists yet; the first restore test follows it. Watch it:

   ```sh
   docker compose logs -f offsite
   cat <textfile dir>/ayeops_offsite.prom
   ```

   `backed up <file>, <n> bytes added` and `restore test passed` are the two lines to see. The
   first copy uploads the whole dump; later ones upload what deduplication cannot reuse.

## Looking inside the repository

The agent container has restic and the repository settings in its environment:

```sh
docker compose exec offsite /opt/restic/restic snapshots
docker compose exec offsite /opt/restic/restic stats --mode raw-data
docker compose exec offsite /opt/restic/restic check
```

A copy now, without waiting for the slot: `docker compose restart offsite` does not force one.
Delete the state file instead, `docker compose exec offsite rm /state/state.json`, and the next
pass, within a minute, copies and tests again.

## Restore drill

With a rest-server store, set `restore_drill` on `edge`'s `offsite` and the
[automated drill](../modules.md#restore-drill) does this on `central` every `every_days`:
restore the newest copy into a throwaway PostgreSQL, run your SQL checks, record the
duration, alert through [offsite-drill](offsite-drill.md). The steps below are the same drill
by hand: for a Garage or provider target the automated one cannot serve, before trusting a
copy for the first time, and when the automated drill fails and you need every message.

Do it on a machine other than `edge`, with only what you would have after losing `edge`: the
restic password from the password manager, the S3 keys, the address of the objectstore.
`central` itself will do.

1. Point restic at the repository:

   ```sh
   export RESTIC_REPOSITORY=s3:http://<central address>:3900/<bucket>/edge
   export AWS_ACCESS_KEY_ID=GK... AWS_SECRET_ACCESS_KEY=... AWS_DEFAULT_REGION=garage
   read -rs RESTIC_PASSWORD && export RESTIC_PASSWORD
   docker run --rm -e RESTIC_REPOSITORY -e RESTIC_PASSWORD -e AWS_ACCESS_KEY_ID \
     -e AWS_SECRET_ACCESS_KEY -e AWS_DEFAULT_REGION --network host \
     restic/restic:0.19.1 snapshots
   ```

   The newest snapshot's time is this morning's slot; its tags carry `sha256=<hex>`.

2. Restore it into an empty directory with room for one dump:

   ```sh
   mkdir -p /tmp/drill
   docker run --rm -e RESTIC_REPOSITORY -e RESTIC_PASSWORD -e AWS_ACCESS_KEY_ID \
     -e AWS_SECRET_ACCESS_KEY -e AWS_DEFAULT_REGION --network host -v /tmp/drill:/drill \
     restic/restic:0.19.1 restore latest --target /drill
   ```

3. Compare the hash with the tag: `sha256sum /tmp/drill/<path>/<file>`.

4. Read the dump's table of contents:

   ```sh
   docker run --rm -v /tmp/drill:/drill postgres:18.6-alpine3.24 \
     pg_restore --list /drill/<path>/<file> | head -20
   ```

5. Restore it into a scratch database and count something you know:

   ```sh
   docker run -d --name drill-db -e POSTGRES_PASSWORD=drill -v /tmp/drill:/drill \
     postgres:18.6-alpine3.24
   docker exec drill-db createdb -U postgres drill
   time docker exec drill-db pg_restore -U postgres -d drill -j 4 --no-owner /drill/<path>/<file>
   docker exec drill-db psql -U postgres -d drill -c 'select count(*) from <a big table>'
   docker rm -f drill-db && rm -rf /tmp/drill
   ```

   Note the time of steps 2 and 5: together they are how long a restore takes.

## Rotating keys

**S3 key.** Garage refuses to start when `AYE_OBJECTSTORE_ACCESS_KEY_ID` exists with a different
secret, so a rotation is a new pair:

1. On `central`, generate a new `AYE_OBJECTSTORE_ACCESS_KEY_ID` and
   `AYE_OBJECTSTORE_SECRET_ACCESS_KEY`, keep the old id at hand, `docker compose up -d garage`.
   Garage creates the new key and grants it the bucket.
2. On `edge`, set `AYE_OFFSITE_ACCESS_KEY_ID` and `AYE_OFFSITE_SECRET_ACCESS_KEY` to the new
   pair, `docker compose up -d offsite`, and check `restic snapshots` through the container.
3. On `central`, remove the old key: `docker compose exec garage /garage key delete --yes <old id>`.

**Restic password.** Add the new password as a second key, switch, remove the old one:

```sh
openssl rand -base64 33 > new-password
docker compose exec -T offsite sh -c 'umask 077; cat > /tmp/new' < new-password
docker compose exec offsite /opt/restic/restic key add --new-password-file /tmp/new
docker compose exec offsite /opt/restic/restic key list
```

Save the content of `new-password` in the password manager, `shred -u new-password`, set it as `AYE_OFFSITE_RESTIC_PASSWORD`,
`docker compose up -d offsite`, then remove the old key by its id from `key list`:
`docker compose exec offsite /opt/restic/restic key remove <old key id>`. The data is not
re-encrypted; the password only unlocks the repository key.

**Admin token and RPC secret.** Change the variable on `central` and `docker compose up -d`. With
one node nothing else holds them.

## Moving the data to a second disk

1. Mount the disk on `central`, e.g. `/mnt/backup`.
2. `docker compose stop garage garage-admin`, copy the volumes:
   `docker run --rm -v <project>_objectstore-data:/from -v /mnt/backup/garage-data:/to alpine cp -a /from/. /to/`
   and the same for `objectstore-meta` to `/mnt/backup/garage-meta`.
3. Set `data_dir: /mnt/backup/garage-data` and `meta_dir: /mnt/backup/garage-meta`, render,
   copy, `docker compose up -d`, check `garage status` and `restic check` from `edge`.
4. Remove the old volumes once a copy and a restore test have passed.

## Moving to a provider

restic speaks to any S3. Create a bucket and a key with the provider, set
`target: {url: s3:https://<endpoint>/<bucket>/<host>}` plus `quota` and `region`, put the new
keys into `edge`'s `.env`, and either `restic init` a new repository there or copy the existing
snapshots with `restic copy --from-repo s3:http://<central address>:3900/<bucket>/<host>`.

## Limits

With Garage the agent's key can delete what it wrote: whoever takes over `edge` can wipe its
copies. `provider: rest-server` closes that, with the pruning on the store side: see
[append-only off-site copies](offsite-append-only.md).
