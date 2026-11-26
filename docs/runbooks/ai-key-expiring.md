# AI provider API key expires soon

An active Anthropic API key of the organization has an expiry date less than 14 days away.
When it passes, every request with that key fails with 401.

## What to do

1. `AyeOps / AI` → «API keys: days until expiry» names the key.
2. Create a replacement key in the Claude Console (Settings → API keys), store it where the
   service reads it (the secrets manager), deploy, and check the service works with it.
3. Archive the old key in the console once nothing uses it.
