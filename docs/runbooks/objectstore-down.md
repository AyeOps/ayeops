# Object store down or unhealthy

`objectstore-down`: vmagent has not scraped Garage for 5 minutes. `objectstore-unhealthy`:
Garage answers but reports its cluster as unhealthy. Off-site copies and restores fail until it
is back. Setup and commands: [offsite](offsite.md).

## What to do

1. On the objectstore host: `docker compose ps garage garage-admin` and
   `docker compose logs --tail 50 garage`.
2. Garage exits at start with
   `Access key ... is associated with a secret key different than the one given`: the `.env`
   changed `AYE_OBJECTSTORE_SECRET_ACCESS_KEY` for an existing key id. Put the old secret back,
   or follow [rotating keys](offsite.md#rotating-keys) with a new key id.
3. `Refusing to run in single-node mode`: the layout was changed by hand to more nodes or a
   later version. Single-node mode needs layout version 1; undo the change with `garage layout`
   or drop `--single-node` and manage the layout yourself.
4. A full disk stops Garage: `df -h` on the host or on `data_dir`, and the `disk-full` alert.
5. `docker compose exec garage /garage status` lists the node as healthy when it runs;
   `/garage health` is the healthcheck.

The alerts clear once vmagent scrapes `garage:3903` again and `cluster_healthy` is 1.
