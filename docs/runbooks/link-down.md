# Private network link down

The host in the `host` label has pinged every host of `zone` it probes over the private network
(`probes.links`, the `address` of each host) for 3 minutes and none answered. A host without a
`zone` is a zone of its own, named after the host. While the zone is unreachable, `host-down` of
its probed hosts is held back, so a tunnel or a site that drops shows as this one alert instead
of a `host-down` per host.

It reads the same for three causes: the tunnel between the sites is down (WireGuard, Tailscale,
any VPN), the site lost its uplink, or the site lost power. From the probing host they look
alike; the steps below tell them apart.

## What to do

1. Look at the dashboard `AyeOps / Probes`, panel "Private network links": does the probing host
   reach the other zones? If every zone is red at once, the problem is on the probing host's
   side: its own VPN interface (`ip -br addr`, `wg show` or `tailscale status` there) or its
   uplink.
2. Only this zone is red: is the site on the internet at all? Its public services (the
   `probe-failing` alerts of services on its hosts), the [sentinel](../modules.md#sentinel) on a
   host there, or someone on site tell. A site that is online with the tunnel down is a VPN
   problem: on a host of the zone, `wg show` (latest handshake older than about two minutes with
   traffic flowing means the peer is not answering), `tailscale status`, or the VPN client's log;
   then the firewall, a changed public address of the endpoint, an expired key or login.
3. The site is offline too: power or the provider. Nothing in the fleet can help from outside;
   the hosts' vmagent queues hold their metrics and replay them when the link is back, so the
   dashboards fill in the gap afterwards.
4. The hosts answer `ssh` over the private address but not ping: ICMP is filtered on the VPN
   interface. Allow echo requests from the probing host there, or drop the host from
   `probes.links`; with only ICMP blocked this alert fires while everything works.
5. When the alert resolves, `host-down` of the zone's hosts evaluates again: a host that still
   does not report two minutes later is down by itself, not by the link.
