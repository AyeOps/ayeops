# Cloudflare edge answers 5xx

More than 2 % of the requests to a zone ended in a 5xx status at the Cloudflare edge over the
exporter window (15 minutes by default), with at least 100 requests in it, for 10 minutes.
The status is the one the visitor got, so it covers both the origin's own 500s and Cloudflare's
520-526 when it cannot reach the origin. Only visitor requests count (`requestSource: "eyeball"`):
Cloudflare's own fetches, such as Early Hints cache lookups that end in 504, are left out.

## What to do

1. In `AyeOps / Cloudflare`, «Requests per second by status class» and «Requests by hostname»:
   which hostname carries the 5xx?
2. If `cloudflare-origin-errors` fires as well, Cloudflare cannot reach the origin: follow
   [that runbook](cloudflare-origin-errors.md).
3. Otherwise the origin answers 5xx itself: read its logs in Grafana Explore (Loki) for the same
   minutes and check `probe-failing` of its URL.
4. A 5xx that Cloudflare produces without the origin (Workers exceptions, rate limiting rules
   answering 503) shows in the Cloudflare dashboard under Security and Workers, not in the
   origin logs.
