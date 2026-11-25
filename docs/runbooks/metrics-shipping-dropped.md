# vmagent drops metrics

vmagent on `host` threw away metric data in the last hour. Those samples are not in
VictoriaMetrics and do not come back; dashboards and reports show a gap for that host. There are
two causes, one counter each:

- `vm_persistentqueue_blocks_dropped_total`: the queue reached its maximum, `queue_max` of the
  [metrics](../modules.md#metrics) module or the free disk under it, and vmagent dropped its
  oldest blocks to make room for new ones. Storage was out of reach or too slow for hours;
  [host-down](host-down.md) or [metrics-shipping-backlog](metrics-shipping-backlog.md) fired
  first.
- `vmagent_remotewrite_packets_dropped_total`: storage answered `400` or `409` to a block, and
  vmagent does not retry a rejected block.

The alert counts drops over one hour, so it stays firing for an hour after the last one. After
a long outage it fires once the host reports again, since the counters travel with the samples.

## What to do

1. Which counter grew? In Grafana Explore:
   `increase(vm_persistentqueue_blocks_dropped_total{host="<host>"}[1h])` and
   `increase(vmagent_remotewrite_packets_dropped_total{host="<host>"}[1h])`.
2. Queue full: restore the path to storage as in
   [metrics-shipping-backlog](metrics-shipping-backlog.md), then check `df -h` on the host for
   the volume `vmagent-data`. To bridge a longer outage next time, raise `queue_max`: the queue
   lasts `queue_max` divided by `rate(vmagent_remotewrite_block_size_bytes_sum{host="<host>"}[1d])`
   seconds of outage.
3. Rejected blocks: `docker compose logs vmagent | grep "was rejected"` on the host prints the
   status code and the answer of storage; `docker compose logs --tail 200 victoriametrics` on the
   central host says why it refused, for example a label longer than its limit. Fix the scrape
   target or relabeling that produces such series.
