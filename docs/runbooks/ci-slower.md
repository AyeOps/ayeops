# CI duration up 50 %

The median of the successful `ci` runs of the last three days is more than 1.5 times the
30-day median of the same job. It needs at least ten runs in 30 days.

## What to do

1. «CI duration per job» on `AyeOps / Delivery` shows when it started.
2. «Slowest tests» and «Test suite duration» on `AyeOps / Quality` show whether the tests got
   slower or something around them (dependency install, cache, image build).
3. A dependency cache that stopped hitting is common: the lock file changed, or the cache key
   includes something that changes on every run.
4. On a self-hosted runner, check the host itself: CPU steal, a full disk, swap.
