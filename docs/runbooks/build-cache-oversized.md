# Build cache above twice the kept size

After the nightly prune, the docker build cache on this host
(`ayeops_hygiene_build_cache_bytes{phase="after"}`) is still more than twice `build_cache_keep`.
The prune asks BuildKit to keep at most `build_cache_keep` (`max-used-space`), so something keeps
it from getting there.

## What to do

1. `docker buildx du --verbose | tail` and `docker system df`: build cache records that are
   `in use` or `shared` with images cannot be pruned.
2. A build running at 03:30 holds its cache; a CI runner busy at that hour does. Move `schedule`.
3. A docker older than 28 (API below 1.48) only knows `keep-storage`; check
   `docker version --format '{{.Server.APIVersion}}'`.
4. A host whose builds need more cache than this: raise `build_cache_keep`. Keep it at least as
   large as any bound a CI job applies after itself, so the two do not evict each other's cache.
