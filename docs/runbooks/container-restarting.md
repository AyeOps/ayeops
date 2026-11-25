# Container restarting

A container started more than twice in 15 minutes.

## What to do

1. `docker compose logs --tail 200 <name>` for the crash reason.
2. OOM kills: `docker inspect <name> --format '{{.State.OOMKilled}}'`; raise its memory in `resources` if justified.
3. A failing healthcheck with `restart: unless-stopped` also looks like this.
