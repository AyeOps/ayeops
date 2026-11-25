# Running containers not read for 15 minutes

`ayeops_image_updates_last_scan_timestamp_seconds` on this host is more than 15 minutes old:
image-updates-agent of the [image-updates](../modules.md#image-updates) module has not listed the
running containers through its docker socket proxy, so the image states on the dashboard are
stale.

## What to do

1. `docker compose ps image-updates-agent image-updates-proxy` and
   `docker compose logs --tail 30 image-updates-agent`. Every scan logs one JSON line, `scan` with
   the counts or `scan-failed` with the error.
2. `GET /containers/json: 403` means the proxy refused the call: check that its environment still
   has `CONTAINERS=1` and `IMAGES=1`.
3. A connection error to `image-updates-proxy:2375` means the proxy is down or the two containers
   no longer share the `image-updates-docker` network.
4. A proxy that starts but cannot answer usually cannot open `/var/run/docker.sock`: a rootless
   docker keeps its socket elsewhere.
5. No log line at all and a stale textfile: the agent container is down, or the textfile
   directory is not the one node-exporter reads.
