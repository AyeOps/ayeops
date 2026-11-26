# AI spend today far above the usual day

Today's cost of a provider (UTC day so far) is more than three times its median day over the
seven days before, and above `spike_floor_usd` (default $1), so a few cents never page.

## What to do

1. `AyeOps / AI` → «Spend of the day so far by model»: which model, since when.
2. «Tokens of the day so far by kind»: more output tokens means longer answers or more calls;
   more uncached input with fewer cache reads means caching broke (a timestamp or a changing
   field in the cached prefix).
3. Find the caller by the API key or project in the provider console's usage page.
4. If it is expected (a backfill, a batch), ignore it for today; the median adapts within a week.
