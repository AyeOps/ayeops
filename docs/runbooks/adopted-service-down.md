# Adopted service down

A service that [`aye discover --adopt`](../discover.md#adopting-a-host) found running now runs
fewer containers than it did then. cAdvisor of the [containers](../modules.md#containers) module
counts them by container name; the rule stays quiet while the host has no cAdvisor at all.

## What to do

1. `docker ps -a --filter name=<container>` on the host: exited, restarting or gone.
2. `docker compose -p <project> logs --tail 200 <service>` for the reason; the alert summary and
   the service's `description` name the compose project.
3. Stopped on purpose: set `state: disabled` on the service, or change the count in its alert.
4. Renamed or scaled by a new compose file: copy the real file next to the manifest in place of
   the adopted stub, and write the service's own alert, metric and status mark instead of the
   adopted ones.
