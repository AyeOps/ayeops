# Host down

No fresh node metrics from the host for three minutes: the machine is off or asleep, vmagent cannot reach storage, or node-exporter is down.

## What to do

1. Is the host reachable at all (ping, ssh)? A laptop that sleeps fires this by design; silence it for the night rather than deleting the rule.
2. On the host: `docker compose ps`; vmagent buffers to disk while storage is unreachable and replays when it is back. The buffer lasts until `queue_max` is full, then [metrics-shipping-dropped](metrics-shipping-dropped.md) follows; `curl -s http://127.0.0.1:8429/metrics | grep pending_data_bytes` on the host shows its size in bytes.
3. On the central host: is VictoriaMetrics up and is port 8428 reachable from the edge over the private address?
4. While the host is down its other alerts are held back (value 0 in Grafana, see `docs/modules.md`, grafana). Once `host-down` resolves, look at what fires on the host in the next minutes: that is what was hidden.
