# Domain expiry unknown

RDAP has not answered for a domain for a day. Silence is not success: an unknown expiry is reported, not dropped.

## What to do

1. Is the IANA bootstrap file reachable from the probing host?
2. Does the registry of this TLD offer RDAP at all? Some ccTLDs do not; then monitor the date by other means and remove the domain from `probes.domains`.
3. `docker compose logs rdap-exporter`.
