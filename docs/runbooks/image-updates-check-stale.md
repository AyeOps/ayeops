# No finished Diun run for 2 days

`ayeops_image_updates_last_check_success_timestamp_seconds` on this host is more than 2 days old:
Diun of the [image-updates](../modules.md#image-updates) module has not reported a finished
registry check to image-updates-agent, so a rebuilt floating tag would go unnoticed. Diun reports
start, success and failure of every run to the agent; a host that has never had a run stays quiet
until its first one.

## What to do

1. `docker compose logs --tail 50 diun`. Look for `Cannot send Healthchecks` (the agent is not
   reachable) and for registry errors such as `toomanyrequests` or `unauthorized`.
2. `ayeops_image_updates_last_check_failed_timestamp_seconds` newer than the last success means
   the runs fail; `ayeops_image_updates_tags{result="error"}` counts the tags of the last run that
   could not be read.
3. `toomanyrequests` comes from Docker Hub's limit for anonymous pulls (per IP, shared with every
   `docker pull` from the same address). Diun compares digests with HEAD requests, which Docker Hub
   does not count, but a host behind a busy NAT can still hit it; move the schedule or wait.
4. `unauthorized` on `ghcr.io` is a private image without credentials: set `ghcr_username` and
   `AYE_IMAGE_UPDATES_GHCR_TOKEN` (a token with `read:packages`), or accept that it stays unknown.
5. Diun with no log line since its start is waiting for its schedule; it also runs once at start.
