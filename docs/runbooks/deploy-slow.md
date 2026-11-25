# Deploy slower than twice its 30-day median

The latest successful `deploy` of a project took more than twice the median of its successful
deploys over 30 days. It needs at least five deploys in 30 days before it can fire.

## What to do

1. «Deploy duration» on `AyeOps / Delivery` shows whether it is one slow deploy or a trend.
2. A slow image pull is the usual cause: a bigger image, a registry that throttles, or a host
   with a full disk that evicts its layers. `docker system df` on the host shows the cache.
3. A migration that ran during the deploy is the other one; the deploy log names it.
4. If the new duration is the new normal, the alert stops on its own once it becomes the median.
