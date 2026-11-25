# Cloudflare analytics query failing

A GraphQL Analytics query of the cloudflare exporter has failed for 30 minutes, so the numbers
of that zone or account are missing and the other Cloudflare alerts cannot fire for it. Silence
is not success.

## What to do

1. `docker compose logs cloudflare-exporter`: each failure is a `query_failed` line with the
   target, the dataset and Cloudflare's message; the token is never logged.
2. `denied: true` (`authz`, HTTP 401/403): the token lacks a permission. Zone datasets need
   `Zone > Analytics > Read` on the zone, the zone lookup `Zone > Zone > Read`, account datasets
   (`workers`, `r2_*`, `d1*`) `Account > Account Analytics > Read` on that account.
3. `dataset: zone` failing: the token cannot see the zone at all, or the name is misspelled.
4. HTTP 429: the GraphQL budget (300 queries per 5 minutes per user) is spent, often by another
   tool sharing the same user. The `start` log line prints this exporter's share.
5. A dataset the plan does not offer reports `ayeops_cloudflare_dataset_available 0`; remove the
   zone or the account from the module if that is permanent.
