# Scrape target down

vmagent could not scrape an exporter for 5 minutes. The `job` label names the exporter, `host` the machine.

## What to do

1. `docker compose ps` on the host: is the container running and healthy?
2. `docker compose logs --tail 100 <service>`.
3. If the container restarts in a loop, see [container-restarting](container-restarting.md).
4. If the exporter answers locally but not to vmagent, check the compose network and the scrape target in `vmagent/scrape.yaml`.
