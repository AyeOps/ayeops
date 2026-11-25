# vmagent falls behind shipping metrics

For 15 minutes vmagent on `host` has held more unshipped data than it collects in 30 minutes,
or more than half of the room its queue has: `queue_max` of the
[metrics](../modules.md#metrics) module, or the free disk under the queue when that is less.
Nothing is lost yet. When the queue is full, vmagent drops its oldest blocks and
[metrics-shipping-dropped](metrics-shipping-dropped.md) fires.

vmagent sends fresh samples ahead of the backlog, so the host keeps reporting while the queue
drains and this alert sees the queue grow. While storage is out of reach entirely, nothing
arrives from the host: [host-down](host-down.md) fires and holds this alert back until the host
reports again.

## What to do

1. How fast does the queue grow and how much room is left? In Grafana Explore:
   `sum by (host) (vmagent_remotewrite_pending_data_bytes{host="<host>"})` and
   `sum by (host) (vm_persistentqueue_free_disk_space_bytes{host="<host>"})`. The room divided by
   the growth per second, `deriv(vmagent_remotewrite_pending_data_bytes{host="<host>"}[30m])`,
   is the time left before data loss. The health dashboard draws the queue of every host in
   "Metrics waiting to be shipped".
2. On the host: `docker compose logs --tail 100 vmagent`. Timeouts or `connection refused`
   point at the network or the central VictoriaMetrics; `429` or `5xx` answers mean storage
   cannot keep up.
3. From the host, `curl -s http://<central address>:8428/health` should print `OK`. On the
   central host: is VictoriaMetrics up, is its disk full ([disk-full](disk-full.md))?
4. A queue that shrinks after an outage needs nothing; the alert resolves once the backlog is
   under 30 minutes of traffic. A link that never catches up needs less traffic (a longer
   `scrape_interval`, fewer scrape targets) or a faster link; a larger `queue_max` only buys time.
