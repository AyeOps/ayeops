# Loki discards log lines

Loki rejected lines in the last 10 minutes. The `reason` label says why, `host` is the machine
that runs Loki. Rejected lines are gone: Alloy does not retry a rejection.

## What to do

1. Read the reason:
   - `rate_limited`, `per_stream_rate_limit`: a container floods the logs. Find it in Grafana
     with `topk(5, sum by (host, container) (rate({job="docker"}[5m])))` and fix the noise at
     the source, or raise `ingestion_rate_mb` in `logs/loki.yaml` if the volume is real.
   - `greater_than_max_sample_age`: a host shipped lines older than a week, usually after a long
     outage of Alloy or a clock jump. It stops on its own; check the host clock with `timedatectl`.
   - `line_too_long`: one container writes huge lines; shorten them at the source.
   - `stream_limit`: too many label combinations; a label with an unbounded value was added.
2. `docker compose logs --tail 100 loki` on the Loki host for the matching warnings.
