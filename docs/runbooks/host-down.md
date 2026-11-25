# Host down

No fresh node metrics from the host for three minutes: the machine is off or asleep, vmagent cannot reach storage, or node-exporter is down.

## What to do

1. Is the host reachable at all (ping, ssh)? A laptop that sleeps fires this by design. If the
   host is expected to disappear (a workstation switched off at night, a machine on an
   unreliable link), mark it `volatile: true` in the manifest: `host-down` then leaves it out
   and [host-gone](host-gone.md) warns only after it has been away for a day, or the duration
   given. If the outage is planned and recurs (an update night, a weekly backup run that takes
   the box down), declare a `maintenance` window on the host instead of a silence in Grafana:
   it lives in the manifest, so nobody forgets it ([manifest](../manifest.md#volatile-hosts-and-maintenance-windows)).
2. On the host: `docker compose ps`; vmagent buffers to disk while storage is unreachable and replays when it is back. The buffer lasts until `queue_max` is full, then [metrics-shipping-dropped](metrics-shipping-dropped.md) follows; `curl -s http://127.0.0.1:8429/metrics | grep pending_data_bytes` on the host shows its size in bytes.
3. On the central host: is VictoriaMetrics up and is port 8428 reachable from the edge over the private address?
   A host that `probes.links` pings is held back while none of its zone answers: then
   [link-down](link-down.md) fires once for the zone instead. A `host-down` of such a host means
   the host still answers ping, or another host of its zone does, so the link is up and the host
   itself stopped reporting.
4. While the host is down its other alerts are held back (value 0 in Grafana, see `docs/modules.md`, grafana). Once `host-down` resolves, look at what fires on the host in the next minutes: that is what was hidden.
5. Fired right after a maintenance window of the host ended? The window holds the alert back
   only while it is open; a host still down two minutes after its end is down.
