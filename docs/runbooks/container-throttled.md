# Container CPU throttled

A container used more than 80 % of its CPU limit on average over 30 minutes: it runs against its
ceiling, so its work gets slower and a collector's data goes stale.

The share of throttled CFS periods is not the signal: a container that is mostly idle is throttled
in most of the few periods it runs, since a short burst (a Python start, one poll) exceeds a small
quota, at a few percent of its limit.

## What to do

1. Compare the CPU in use with the limit: the service tile on the portal, or `rate(container_cpu_usage_seconds_total{name="<name>"}[5m])` against `cpus`.
2. Count the throttled periods: `rate(container_cpu_cfs_throttled_periods_total{name="<name>"}[5m]) / rate(container_cpu_cfs_periods_total{name="<name>"}[5m])`.
3. Raise `cpus` in `resources` to 2-3 times the peak of a burst, if the host has the room.
4. A container that burns its limit all the time is a loop or a runaway job: read `docker compose logs --tail 200 <name>` first.
