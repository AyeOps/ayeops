# Probe failing

The URL did not answer with a 2xx status for 5 minutes, as seen from the probing host.

## What to do

1. Open the URL yourself, from another network if possible.
2. `probe_http_status_code` and `probe_failed_due_to_regex` on the probes dashboard narrow it down.
3. DNS or TLS failures show up as `probe_dns_lookup_time_seconds` or a missing `probe_ssl_earliest_cert_expiry`.
