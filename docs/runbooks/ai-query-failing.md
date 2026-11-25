# AI provider report query failing

The usage, cost, rate limit or API key report of a provider has not answered for 3 hours. The
numbers on `AyeOps / AI` and the budget alerts for that provider are stale meanwhile.

## What to do

1. `docker compose logs ai-usage-exporter` on the central host: each failure logs one
   `query_failed` line with the provider, dataset and HTTP status, never the key.
2. HTTP 401 or 403: the admin key was revoked or is not an admin key. Anthropic needs an Admin
   API key (`sk-ant-admin…`); a normal API key is refused. OpenAI needs an Admin key
   (`sk-admin…`) with read access to usage. Replace it in the secrets manager and redeploy.
3. HTTP 429: the poll is too frequent for the provider; raise `refresh_seconds`.
4. HTTP 5xx or timeouts: the provider's side; it resolves on its own, check its status page.
