# Error tracker (GlitchTip)

Operating the [errors](../modules.md#errors) module: first run, the DSN for services, restore
and upgrades. Alerts that lead here: `target-down` on `glitchtip` and `container-restarting` on
its worker, database and cache; the dump reports as the backup job `errors`, see
[backup-failed](backup-failed.md#built-in-dumps) and [backup-missing](backup-missing.md).

## First run

1. Generate the three variables into the host's `.env` next to `compose.yaml`:

   ```sh
   echo "AYE_ERRORS_SECRET_KEY=$(openssl rand -hex 32)" >> .env
   echo "AYE_ERRORS_DB_PASSWORD=$(openssl rand -hex 24)" >> .env
   echo "AYE_ERRORS_CACHE_PASSWORD=$(openssl rand -hex 24)" >> .env
   chmod 600 .env
   ```

2. Route `errors: glitchtip:8000` in ingress, `aye dns <manifest> --apply` for the record if
   the domain has no wildcard, render, copy, `docker compose up -d`. The first start runs about
   120 migrations before the web server listens; `docker compose ps glitchtip` turns healthy
   after that, and the worker starts only then.
3. Create the owner account from the host, so nobody can take it through the web form first:

   ```sh
   docker compose exec -e DJANGO_SUPERUSER_EMAIL=you@example.com \
     -e DJANGO_SUPERUSER_PASSWORD="$(openssl rand -base64 24)" \
     glitchtip python manage.py createsuperuser --noinput
   ```

   Keep the password in the secrets manager. Sign-ups are off, so this stays the only account
   until you invite people from the UI.
4. Sign in at the `url`, create the organization and one project per product.

## DSN for services

The DSN in the project settings points at the `url`, which is behind the browser sign-in, and an
SDK cannot pass that. Services use the same key with the private address and the exposed port:

```text
http://<public key>@<public_bind>:8095/<project id>
```

for example `http://0123…cdef@10.8.0.1:8095/1`. The public key is the part before `@` in the
project's DSN, the project id the number after the last `/`. Treat the DSN as a secret: anyone
on the private network who has it can send events.

Check from a service host:

```sh
curl -s -o /dev/null -w '%{http_code}\n' http://<public_bind>:8095/_health/
```

`200` means the port answers; an SDK event shows as an issue within seconds.

## Restore

1. `docker compose stop glitchtip glitchtip-worker`.
2. Copy the dump out of the `errors-backups` volume, or take one from the off-site copy.
3. `docker compose exec -T glitchtip-db pg_restore --clean --if-exists -U glitchtip -d glitchtip < glitchtip-<stamp>.dump`
4. `docker compose up -d`: the web container runs the migrations of its version over the
   restored schema before it serves.

## Upgrades

Bump the pinned image in the module and render. The web container migrates on start; read the
GlitchTip changelog for a major version first and take a fresh dump
(`docker compose exec -e AYE_BACKUP_ONCE=1 -e AYE_BACKUP_HOUR=0 glitchtip-backup sh /backup.sh`
after deleting today's file).
