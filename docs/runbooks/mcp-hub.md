# MCP hub down or on an old configuration

`mcp-hub-down`: vmagent has not scraped agentgateway on `15020` for 5 minutes. Assistants and
the autopilot get no tools until it is back; nothing else depends on it.
`mcp-hub-config`: agentgateway answers but rejected its configuration file on the last
reload and still serves the one before. Setup: [mcp](../modules.md#mcp).

## What to do

1. On the host: `docker compose ps agentgateway mcp-tools mcp-s3-bin` and
   `docker compose logs --tail 50 agentgateway`.
2. The hub waits for `mcp-tools` (and `mcp-s3-bin` with an S3 source) to be healthy: when one
   of them restarts, `docker compose logs` of it says which copy failed, usually a full disk.
3. `error looking key 'AYE_MCP_...' up: environment variable not found`: the configuration
   names a variable that the host's `.env` lacks; `invalid config` with a line and column right
   after a `|2-` line: that secret holds a line break, which the hub cannot take. Fix the
   `.env`, then `docker compose up -d agentgateway`. Quotes, backslashes and spaces in a secret
   are fine.
4. A validation error after an edit: `mcp/agentgateway.yaml` is rendered by `aye render`; do not
   edit it on the host. Run `aye check` and `aye deploy` again, or put the previous render back
   with `aye deploy`.
5. One source failing does not take the hub down (`failureMode: failOpen`): its tools are only
   missing from `tools/list`. The log names the target; for `grafana`, `metrics` and the
   catalog see `docker compose logs mcp-grafana mcp-victoriametrics mcp-catalog` and
   [target-down](target-down.md). `mcp-catalog` stops at start with `cannot read the catalog`
   when `mcp-catalog/catalog.json` is missing or not the rendered file: deploy the render again.
6. From the host, `curl -s http://127.0.0.1:3010/mcp -H 'content-type: application/json' -H 'accept: application/json, text/event-stream' -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"curl","version":"0"}}}'`
   answers with the hub's `serverInfo` when it works.

The alerts clear once vmagent scrapes `agentgateway:15020` again and
`agentgateway_config_synchronized` is 1.
