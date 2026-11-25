# A floating image tag points to a newer image

For 12 hours at least one container on this host has run an image whose floating tag (`latest`,
a bare major such as `postgres:18`, or no tag at all) now points to a different digest in the
registry than the image the container was created from. Usually the publisher rebuilt the tag
for a security or patch release; the container keeps running the old one until it is pulled and
recreated. Pinned versions such as `grafana/grafana:13.2.3` never fire this: Renovate opens a pull
request for them instead. See the [image-updates](../modules.md#image-updates) module.

## What to do

1. `AyeOps / Hosts` → Images, «Containers with a newer image», lists `container`, `image` and `tag`.
   The same in PromQL: `ayeops_image_update_available{host="<host>"} == 1`.
2. Read the release notes of the image if it has any. For a database (`postgres:18`) a minor
   rebuild is safe to roll; a new major never arrives under a major tag.
3. Pull and recreate the container the way the stack is deployed: `docker compose pull <service>`
   then `docker compose up -d <service>`, or the project's own deploy script.
4. The agent reads the running containers every minute: once the container runs the image the tag
   points to, its series turns 0 and the alert resolves after the next evaluation.
5. To stop watching one container on purpose, label it `io.ayeops.image-updates=false`; to watch a
   pinned one anyway, `io.ayeops.image-updates=true`.
