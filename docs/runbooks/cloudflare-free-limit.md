# Cloudflare free limit 80 % used

An account resource passed 80 % of its free-plan limit. Past the limit Cloudflare refuses the
work (Workers requests, D1 rows) or starts billing it (R2, on a paid plan).

| `resource` | Free limit | Resets |
|---|---|---|
| `workers_requests_day` | 100 000 requests | 00:00 UTC daily |
| `r2_class_a_month` | 1 000 000 operations | monthly |
| `r2_class_b_month` | 10 000 000 operations | monthly |
| `r2_storage_bytes` | 10 GB-month | billed on the monthly average; the gauge is the current size |
| `d1_rows_read_day` | 5 000 000 rows | 00:00 UTC daily |
| `d1_rows_written_day` | 100 000 rows | 00:00 UTC daily |
| `d1_storage_bytes` | 5 GB | none, it is a size |

A paid plan has other limits; set them under `cloudflare.limits` so the gauge keeps meaning
«80 % of what we pay for».

## What to do

1. «Free limits used» on `AyeOps / Cloudflare` names the resource; the per-bucket, per-script
   and per-database panels name who uses it.
2. R2 class A (writes, lists): a job that lists or rewrites objects in a loop is the usual cause.
   Class B (reads): a missing cache in front of a public bucket.
3. R2 storage: check the lifecycle rules of the bucket; objects without an expiry pile up.
4. Workers requests: a crawler or a loop calling the Worker; Cloudflare WAF can rate-limit it.
