# Cloudflare cannot reach the origin

Cloudflare answered 520-526 for a hostname within the exporter window. These statuses are
Cloudflare's own and mean the origin did not give it a usable answer:

| Status | Meaning |
|---|---|
| 520 | the origin answered with something empty or invalid |
| 521 | the origin refused the connection |
| 522 | the TCP connection to the origin timed out |
| 523 | the origin is unreachable (DNS or routing) |
| 524 | the origin accepted the connection but did not answer within 100 s |
| 525 | the TLS handshake with the origin failed |
| 526 | the origin certificate is invalid under Full (strict) |

## What to do

1. `status` on the alert names the code; the table above says where to look.
2. 521/522/523: is the origin host up (`host-down`, `probe-failing`)? Did a firewall change drop
   Cloudflare's address ranges?
3. 524: a slow request on the origin; find it in the access log by time.
4. 525/526: compare the origin certificate with the zone's SSL/TLS mode; an expired origin
   certificate shows here before any visitor reports it.
