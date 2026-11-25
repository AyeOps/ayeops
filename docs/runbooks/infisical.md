# Secrets manager (Infisical)

Operating the [secrets](../modules.md#secrets) module: first run, moving `.env` files in,
deploys, Google sign-in, restore and upgrades. Alerts that lead here: `target-down` and
`probe-failing` on `infisical`; the dump reports as the backup job `secrets`, see
[backup-failed](backup-failed.md#built-in-dumps) and [backup-missing](backup-missing.md).

## First run

1. Generate the four variables into the host's `.env` next to `compose.yaml` and keep a copy of
   `AYE_SECRETS_ENCRYPTION_KEY` outside the host:

   ```sh
   echo "AYE_SECRETS_ENCRYPTION_KEY=$(openssl rand -hex 16)" >> .env
   echo "AYE_SECRETS_AUTH_SECRET=$(openssl rand -base64 32)" >> .env
   echo "AYE_SECRETS_DB_PASSWORD=$(openssl rand -hex 24)" >> .env
   echo "AYE_SECRETS_CACHE_PASSWORD=$(openssl rand -hex 24)" >> .env
   chmod 600 .env
   ```

   These four stay in the host's `.env`: the secrets manager cannot hold the keys that open it.
2. `aye dns <manifest> --apply` for the `secrets` record, render, copy, `docker compose up -d`.
   The first start runs the migrations; `docker compose ps infisical` turns healthy after one
   to three minutes.
3. Open the `url` (`https://secrets.<domain>`) at once: the first visitor creates the instance
   admin. Then *Server Admin*, *General*: turn off sign-ups, so nobody else can register.
4. Create the organization, then one project per product (`trofey`, ...) with the environments
   you deploy (`prod`, `staging`). Folders per stack: `/web`, `/bot`, `/shared`. A folder can
   import another (`/shared` into `/web`); the importing folder's own keys win on pull.
5. One machine identity per host: *Organization*, *Access Control*, *Identities*, *Create*,
   role *No access*. Under *Universal Auth* set *Trusted IPs* to the host's private address,
   then *Create client secret*. Add the identity to the project under *Access Control* with a
   role that can only read the environments that host deploys (a read-only custom role, or
   *Viewer*). Use a separate identity with write access for the one-time `push`.
6. On the host, the identity file, mode 600:

   ```sh
   install -d -m 700 ~/.config/ayeops
   umask 077
   cat > ~/.config/ayeops/identity.env <<'END'
   AYE_SECRETS_URL=https://secrets.ops.acme.example
   AYE_SECRETS_CLIENT_ID=...
   AYE_SECRETS_CLIENT_SECRET=...
   AYE_SECRETS_PROJECT_ID=...
   AYE_SECRETS_ENVIRONMENT=prod
   END
   ```

   The project id is under *Project settings*.

## Moving an existing .env in

```sh
aye secrets push --identity ~/.config/ayeops/push.env --path /web /srv/web/.env
aye secrets pull --identity ~/.config/ayeops/identity.env --path /web -o /tmp/web.env
diff <(sort /srv/web/.env | grep -v '^#' | grep .) <(sort /tmp/web.env); rm /tmp/web.env
```

`push` adds only keys the folder lacks and prints their names, never values. The diff shows
quoting differences only; a key missing on the right means it did not arrive. Delete the
push identity's client secret afterwards.

## Deploying from it

Fetch the `.env` right before compose reads it, and stop the deploy when the fetch fails, so a
stack never starts on stale or empty secrets:

```sh
aye secrets pull --identity ~/.config/ayeops/identity.env --path /web -o .env || exit 1
docker compose up -d
```

To rotate a secret, change it in the UI and redeploy; pushing a file never overwrites a value.

## Google sign-in

Once the admin exists, people can sign in with Google instead of a password:

1. In the Google Cloud console, the OAuth client of type *Web application* needs the redirect
   URI `<url>/api/v1/sso/google`, e.g. `https://secrets.ops.acme.example/api/v1/sso/google`.
2. Set `google_login: true` in the module settings and add `AYE_SECRETS_GOOGLE_CLIENT_ID` and
   `AYE_SECRETS_GOOGLE_CLIENT_SECRET` to the host's `.env`; render and `docker compose up -d`.
3. The login page shows *Continue with Google*. An existing user signs in with the Google
   account of the same email; with sign-ups off, nobody new gets in that way either. Keep the
   admin password as the fallback for when Google is unreachable.

## Infisical down

1. `docker compose ps`: `infisical-db` and `infisical-cache` must be healthy first, `infisical`
   waits for both.
2. `docker compose logs --tail 100 infisical`: a migration error after an upgrade, a wrong
   `AYE_SECRETS_DB_PASSWORD`, or an `ENCRYPTION_KEY` that differs from the one the database was
   created with. The last one is never fixed by a new key: restore the old key.
3. Out of memory shows as `OOMKilled` in `docker inspect infisical`; raise
   `resources.infisical.memory`.

Running stacks are unaffected while it is down; only deploys that pull stop.

## Restore

The dump holds ciphertext; it needs the `AYE_SECRETS_ENCRYPTION_KEY` it was taken with.

```sh
docker compose stop infisical infisical-backup
docker compose exec infisical-db dropdb -U infisical infisical
docker compose exec infisical-db createdb -U infisical infisical
docker compose exec -T infisical-db pg_restore -U infisical -d infisical --no-owner < infisical-<stamp>.dump
docker compose start infisical infisical-backup
```

Copy the dump out of the `secrets-backups` volume first, e.g.
`docker compose cp infisical-backup:/backups/infisical-<stamp>.dump .`. Sign in and pull one
folder to check the values decrypt.

## Upgrades

Bump the image in `src/ayeops/modules/secrets/compose.yaml.j2`, take a dump first
([backup-failed](backup-failed.md#built-in-dumps), step 3), then `docker compose up -d
infisical`. Migrations run on start and go only forward: going back means the dump from before
the upgrade.
