# Central host not answering

The sentinel on another host has failed its checks of the central host for
`failures_before_alert` rounds in a row (3 minutes by default). This message does not come from
Grafana: while the central host is down, none of its own alerts can reach you, so treat
everything else as unknown until it is back.

## What to do

1. Read which checks failed. All of them with connection errors or timeouts: the host, its
   network or the private network between the two hosts is down. Only `samples`: storage
   answers but vmagent no longer writes; only `grafana`: Grafana or the ingress in front of it.
2. Is the central host reachable at all? `ssh <central>`; from the provider's console if not.
   A cloud VM that was stopped or reclaimed shows there.
3. On the host: `docker compose ps` in the stack directory, then
   `docker compose logs --tail 100 grafana victoriametrics vmagent traefik`.
4. Check the sentinel's side: from the sentinel's host `curl -sS <url>` of a failing check. If
   that host lost the private network (`tailscale status` shows the central host offline while
   it is up), fix the tunnel; the central host's own alerts still arrive in that case.
5. When it is back, look at what fired meanwhile in Grafana's alert history; alerts that
   resolved during the outage were never sent.

The sentinel repeats this message every `reminder` (6h) and sends an all-clear with the length
of the outage when a round passes again.
