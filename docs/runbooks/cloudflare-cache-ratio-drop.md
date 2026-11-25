# Cloudflare cache hit ratio dropped

The share of a zone's requests served from cache (`hit`, `stale`, `updating`, `revalidated`)
is less than half of what it was at the same time yesterday, with at least 100 requests in the
window, for 30 minutes. More requests reach the origin, which costs it load and visitors latency.

## What to do

1. «Requests per second by cache status»: did `miss` or `dynamic` grow, or `hit` shrink?
2. `dynamic` grew: responses lost their cache headers, or a Cache Rule changed. Check the
   `Cache-Control` of a page that used to be cached (`curl -sI`, look at `cf-cache-status`).
3. `miss` grew: a purge, a deploy that changed asset URLs, or new traffic to pages never cached
   (crawlers). It recovers on its own after a purge.
4. `bypass` grew: a cookie or an authorization header now makes Cloudflare skip the cache.
