# MCP hub

The [roadmap](roadmap.md#mcp-hub) asks for one MCP endpoint that gives an assistant read access
to the stack: dashboards, metrics, alerts, the backup buckets, and whatever API or CLI a team
already has. AyeOps does not write that gateway. Four open-source gateways exist, the user
picks one in the manifest, and AyeOps renders it with the tools the manifest lists. Its own
code is glue: the rendered configuration, the checks, and small adapters where a gateway lacks
a kind of source.

Module reference: [modules.md](modules.md#mcp). What ships: the `mcp` module with
`provider: agentgateway`, sources of the kinds `grafana`, `victoriametrics`, `s3`, `mcp` over
HTTP, `openapi` and `catalog`. The other providers and kinds are accepted by the schema and refused by
`aye check` as not implemented yet.

## The manifest

One `mcp` module per host, with the gateway and a map of sources. Each source is a few lines
and no code; its tools reach the client as `<source>_<tool>`.

```yaml
hosts:
  central:
    modules:
      mcp:
        provider: agentgateway
        sources:
          grafana:
            kind: grafana
            description: Dashboards, alert rules and datasources of the central Grafana.
          metrics:
            kind: victoriametrics
            description: PromQL over every metric of the fleet.
          backups:
            kind: s3
            description: The off-site backup bucket on Garage.
          catalog:
            kind: catalog
            description: Hosts, services, alerts and runbooks of the catalog.
          releases:
            kind: s3
            description: Release artifacts on Cloudflare R2.
            endpoint: https://0123456789abcdef0123456789abcdef.r2.cloudflarestorage.com
            region: auto
          tickets:
            kind: mcp
            description: Issues of the support tracker.
            url: https://tracker.acme.example/mcp
            token: true
            tools: [search_issues, get_issue]
            allow_write: [add_comment]
          shop:
            kind: openapi
            description: Orders and stock of the shop API.
            spec: apis/shop.json
            url: http://shop:8080
            token: true
          sizes:
            kind: cli
            description: Sizes of backup prefixes in every S3 store.
            image: rclone/rclone:1.75.2
            binary: /usr/local/bin/rclone
            commands:
              size:
                description: Total bytes and objects below a bucket prefix.
                run: [size, --json, "garage:{path}"]
                args:
                  path: {pattern: "^[a-z0-9-]+(/[A-Za-z0-9._-]+)*$"}
                timeout: 30s
      ingress:
        routes:
          hub: agentgateway:3000
```

### Source kinds

| Kind | What runs | Keys |
|---|---|---|
| `grafana` | [mcp-grafana](https://github.com/grafana/mcp-grafana) with `--disable-write`, against the host's Grafana | `tools`, `allow_write` (tools kept with `--enable-write-tools`) |
| `victoriametrics` | [mcp-victoriametrics](https://github.com/VictoriaMetrics-Community/mcp-victoriametrics), which has no write tools | `url` (default `http://victoriametrics:8428`), `tools` |
| `s3` | [mcp-s3](https://github.com/txn2/mcp-s3) with `MCP_S3_EXT_READONLY` | `endpoint` (default the host's Garage), `region`, `path_style`, `tools`, `allow_write` |
| `mcp` | an existing MCP server: proxied when it has a `url`; run from `image` with `command` over stdio | `url`, `transport` (`http` or `sse`), `token`, `image`, `command`, `tools` (required), `allow_write` |
| `openapi` | every operation of an OpenAPI spec as a tool, named by its `operationId` | `spec` (a file next to the manifest), `url` of the API, `token`, `tools`, `allow_write` |
| `cli` | allowlisted commands of one static binary, each a tool with typed arguments, run without a shell and with a timeout | `image`, `binary`, `commands` |
| `catalog` | AyeOps' own MCP server over the manifest's catalog and the runbooks, from a file `aye render` writes | `tools` |

`token: true` sends `Authorization: Bearer $AYE_MCP_<SOURCE>_TOKEN` to that source only. An
S3 source reads `AYE_MCP_<SOURCE>_ACCESS_KEY_ID` and `AYE_MCP_<SOURCE>_SECRET_ACCESS_KEY`; give
it a key that can only read, since the store is the last line of defence. A `grafana` source
needs no variable: mcp-grafana reads the Viewer token that
[grafana-reader](modules.md#grafana-reader) keeps in its volume, through
`GRAFANA_SERVICE_ACCOUNT_TOKEN_FILE`, which mcp-grafana `2.1.1` reads again on every request.
That token is for the Grafana of the same host, so a `grafana` source has no `url`; for the
Grafana of another host, run a hub there.

### Read-only by default

A tool is exposed only when a rule of the gateway allows it, and AyeOps writes those rules:

- `grafana`, `victoriametrics`: every tool their read-only mode registers; `tools` narrows it.
- `s3`: `s3_list_buckets`, `s3_list_objects`, `s3_get_object_metadata` unless `tools` says
  otherwise; `s3_get_object` and `s3_list_connections` are reads too. `s3_put_object`,
  `s3_delete_object`, `s3_copy_object` and `s3_presign_url` (which also signs uploads) write.
- `openapi`: operations with `GET` or `HEAD` are reads and exposed by default; any other method
  writes, whatever it does, so a search over `POST` is exposed only through `allow_write`.
- `mcp`: AyeOps cannot know what a foreign server's tools do, so `tools` is required and names
  the tools that read; the tools that write go into `allow_write`.
- `cli`: each command is a tool; a command that changes anything says `write: true` and runs
  only with `allow_write: true` next to it.
- `catalog`: `list_hosts`, `list_services`, `get_service`, `list_alerts` and `get_runbook`, all
  reads; `tools` narrows them and there is no `allow_write`. The server has no tool that
  writes, and its data is a file rendered without secrets ([below](#the-catalog-source)).

A write tool is never exposed by `tools`, only by `allow_write`, so allowing a write is always
an explicit line in the manifest that review can see.

## How each provider renders

**agentgateway** (implemented): the container `agentgateway` with `mcp/agentgateway.yaml`, one
`mcp` section multiplexing a target per source with `prefixMode: always` (stable names
whatever the number of sources) and `failureMode: failOpen` (one unreachable source does not
take the others down), one `mcpAuthorization` allow rule per source on `mcp.tool.target` and
`mcp.tool.name`, and `timeout` as the request timeout. Sources:

- `grafana`, `victoriametrics`: their own containers `mcp-grafana` and `mcp-victoriametrics`
  over streamable HTTP on the compose network, no host port. The gateway sends mcp-grafana a
  bearer token so nothing else on the network can call it.
- `s3`: the static `mcp-s3` binary is copied from its image into a volume by `mcp-s3-bin` and
  runs as a `stdio` target of the gateway with a cleared environment holding only that
  source's endpoint and keys. No extra long-running container per store.
- `mcp` with `url`, `openapi`: native `mcp`/`sse` and `openapi` targets; the spec is copied to
  `mcp/openapi/<source>.json`.
- `catalog`: the container `mcp-catalog`, a native `mcp` target at
  `http://mcp-catalog:8000/mcp` on the compose network, no host port
  ([below](#the-catalog-source)).
- `mcp` with `image`, `cli`: not implemented yet. The plan is the copy-the-binary pattern for
  static servers, a bridge container for interpreted ones, and a small AyeOps adapter that
  serves the `commands` over streamable HTTP and executes the copied binary with
  `subprocess` (no shell), the typed arguments checked against their patterns, and the timeout.

The gateway listens on `3000` (host port `3010` on `127.0.0.1`, reached over ssh); metrics on
`15020`, readiness on `15021`; the admin UI is off.

**ContextForge** (designed): the container `contextforge` (`ghcr.io/ibm/mcp-context-forge`) with
SQLite in a volume, `TRUST_PROXY_AUTH` behind oauth2-proxy with the signed-in address as the
user. It has no declarative configuration, so `aye deploy` would apply a rendered
`mcpgateway import` file (gateways, tools, one virtual server per host) after the container is
healthy. `grafana`, `victoriametrics` and `mcp` with `url` register as gateways; `s3` and other
stdio servers each need a `mcpgateway.translate` sidecar; `openapi` is refused. Read-only means
the virtual server lists only the allowed tools.

**Docker MCP Gateway** (designed): `docker/mcp-gateway` with a rendered `catalog.yaml` of
`type: remote` entries pointing at the same sidecars, `--tools` from the allowed tools and
`MCP_GATEWAY_AUTH_TOKEN` added by the ingress, without the Docker socket. `openapi`, `cli` and
stdio servers are refused: without the socket it cannot start them, and AyeOps does not hand
the socket to a process reachable from the network.

**Microsoft MCP Gateway**: refused. It needs Kubernetes and Entra ID and ships for amd64 only;
it becomes possible with the Helm chart from the [roadmap](roadmap.md#packaging-and-infrastructure).

## Checked against agentgateway

The tests hold the rendered `agentgateway.yaml` to agentgateway's own JSON schemas, vendored in
`tests/fixtures/agentgateway/` byte for byte from the `v1.7.0` tag (commit
`26f5aebe99a36a037fe54f8080a608aca5547e15`):

| File | Source | SHA-256 |
|---|---|---|
| `config.schema.json` | <https://github.com/agentgateway/agentgateway/blob/v1.7.0/schema/config.json> | `8d757c092bc30a5ed5fd38a07a47d72602941c8344203c2d1f86e02d6dbb2bd3` |
| `cel.schema.json` | <https://github.com/agentgateway/agentgateway/blob/v1.7.0/schema/cel.json> | `f4233b78136e505abe8d0021ec19320bbd3cb16424ad38cc9c08bd0fdf4ddb0c` |

The config schema is strict (`additionalProperties: false` on every object), so a misspelt key
fails the test as it would fail agentgateway; the example and a manifest with every kind the
hub renders (`s3` with a write allowed, `mcp` over HTTP and SSE with a token, `openapi`) validate
with no error. The CEL schema says what a rule sees: `mcp.tool.target` and `mcp.tool.name`
(the name without the prefix), both required strings, which is all the allow rules read. A
schema does not check CEL itself; agentgateway compiles every rule when it loads the file. On
an upgrade, fetch both files from the new tag, replace the fixtures and this table.

The shape of the rules follows agentgateway's docs and source at the tag: `schema/cel.md` lists
the fields, `examples/mcp-authorization` writes rules on `mcp.tool.name`, and the source
evaluates them per tool with the target and the unprefixed name, on `tools/list` (the tool
is left out) and `tools/call` (`crates/agentgateway/src/mcp/handler.rs`, `mcp/session.rs`,
`mcp/rbac.rs`). Allow rules form an allowlist: once one exists, a request that no rule allows
is refused (`http/authorization.rs`). A prompt or resource has no `mcp.tool`, so every rule
fails on it and the hub passes none on.

Once, on 2026-10-31, the `v1.7.0` release binary for Windows
(`agentgateway-windows-amd64.exe`, SHA-256
`909b93dc8fb5486cc0418a818b4a28fa60046cbd619013e33d1e4b20b889ed3c`, the same as its
`.sha256` file) ran against the rendered example:

- `agentgateway --validate-only -f agentgateway.yaml` answered `Configuration is valid!`; with
  a variable unset it failed with `environment variable not found`, with `inn` for `in` in a
  rule it failed with the CEL syntax error, with `clearEnv` for `clear_env` with
  `unknown field`.
- Started with the S3 command replaced by a stand-in stdio server, `grafana` pointed at a
  stand-in HTTP server and `metrics` at a closed port: `tools/list` returned exactly
  `backups_s3_get_object_metadata`, `backups_s3_list_buckets`, `backups_s3_list_objects` and
  `grafana_search_dashboards`; calling `backups_s3_put_object` or `backups_s3_get_object` was
  refused with `authorization denied`; the stand-in prompt was not listed; the closed port
  only cost the `metrics` tools (`failOpen`). The stdio server saw only the variables of its
  target, with an access key and a secret holding quotes, backslashes, `$HOME`, `#`, `:` and
  leading and trailing spaces exactly as set, and the HTTP server saw the bearer token
  exactly as set.
- `agentgateway_mcp_requests_total` carried `method`, `server`, `resource` and
  `resource_type`; `agentgateway_config_synchronized` was `1`, went to `0` when the file was
  replaced by one with a CEL error, and the hub kept serving the previous tools.
- Without `SESSION_KEY`, agentgateway wrote a generated key into its configuration file; the
  hub gets `AYE_MCP_SESSION_KEY` from `.env` instead, as its `/config` is read-only, and the
  file stayed untouched with it set.

Not run: the images on Linux under compose, mcp-s3 against Garage and R2, mcp-grafana against
Grafana with the reader token, mcp-victoriametrics against VictoriaMetrics. A secret with a
line break makes the file fail to parse, unless the break is followed by exactly the block's
indentation, which a token or key does not hold.

## What aye check refuses

| Rule | Refuses |
|---|---|
| `mcp-provider` | a provider that is not implemented yet: `contextforge`, `docker`, `microsoft` |
| `mcp-kind` | a source kind the provider cannot serve (`openapi` on ContextForge or Docker, `cli` and stdio on Docker), or one not implemented yet (`cli`, `mcp` from an `image`) |
| `mcp-description` | a source or a `cli` command without a description: it is what the assistant reads to pick a tool |
| `mcp-write` | a write tool in `tools`, an `allow_write` entry that is not a write tool of its source, a `cli` command with `write: true` and no `allow_write: true` |
| `mcp-tools` | an `mcp` source without `tools`, a tool name its `s3`, `openapi` or `catalog` source does not have |
| `mcp-source` | a key of another kind or a missing one (`spec` and `url` of `openapi`; `image`, `binary` and `commands` of `cli`), a second `grafana`, `victoriametrics` or `catalog` source on a host, a `grafana` source on a host without Grafana, a default `url` or `endpoint` without the module behind it on the host, an OpenAPI spec that is missing, unreadable or has an operation without `operationId` |
| `mcp-open` | a route to the gateway listed in `auth.open`: the gateway trusts whoever reaches it |

## Which provider fits

| Who | Provider | Why |
|---|---|---|
| a small team, one host | agentgateway | one 40 MB container, a file AyeOps renders and the gateway reloads, the sign-in the stack already has, a name allowlist per source |
| a company with SSO and per-person access | agentgateway with `jwtAuth` or `mcpAuthentication` against the company's OIDC provider and CEL rules on claims (`"ops" in jwt.groups`); ContextForge when the tools are managed by people in a UI and need teams and an audit trail in a database | agentgateway keeps the manifest the single source; ContextForge moves the tool list into its database |
| an enterprise | ContextForge on PostgreSQL and Redis, with its RBAC and the policy plugin (OPA or Cedar); the Microsoft gateway where Kubernetes and Entra ID are already the platform | audit, roles and high availability come with them, at the price of a stack that `aye check` cannot see into |

The Docker MCP Gateway fits a developer laptop with Docker Desktop, where it starts servers
from its signed catalog; on a shared server its main feature needs the Docker socket, and its
clients share one static token.

## The autopilot

The [autopilot](autopilot.md) runs Claude Code with one read-only MCP server and
`--allowedTools` limited to it. The hub is that server's transport, not its replacement:

1. **Level 0 and 1 use read-only tools only.** The autopilot connects to the hub on the host's
   loopback port and its `--allowedTools` names tools of read-only sources (`metrics_*`,
   `grafana_*`, `backups_s3_list_objects`), never a tool exposed through `allow_write`. A
   write tool someone allowed for people does not widen what the autopilot may call.
2. **The diagnostics are sources.** `query_metrics` is the `victoriametrics` source, the alert
   state and dashboards are the `grafana` source, the age of the newest backup is the `s3`
   source. The catalog and the runbooks are the `catalog` source, served by a small AyeOps
   container from a file the render writes ([below](#the-catalog-source)); it is our own data,
   so it is our own code. Recent deploys are not in it yet: they live in the metrics that
   `aye report` pushes, which the `victoriametrics` source reads.
3. **Actions stay out of MCP.** From level 2 the autopilot's actions are typed primitives run by
   plain code after the checks in the autopilot design; they are not hub tools, so no
   prompt in a log can reach them through the gateway.
4. **Per-identity rules next.** With agentgateway's `jwtAuth`, the autopilot gets its own token
   and a rule that allows it read tools only, so the gateway enforces what `--allowedTools`
   already promises.

## Open questions

- **MCP clients and the sign-in.** oauth2-proxy signs people in with a browser cookie; Claude
  Code and other MCP clients expect a bearer token or MCP's own OAuth discovery, so through
  ingress they get the sign-in, not the hub. The supported way is the loopback port over ssh
  ([modules](modules.md#mcp)); OIDC sign-in for MCP clients is a follow-up on the
  [roadmap](roadmap.md#mcp-hub): agentgateway's `mcpAuthentication` against the same OIDC
  provider with the route in `auth.open` (and `mcp-open` relaxed for it), or oauth2-proxy
  accepting bearer JWTs.
- **Not yet run under compose.** The configuration and the rules ran on the agentgateway
  binary ([checked](#checked-against-agentgateway)); the first deploy still has to confirm the
  images together, mcp-s3 against Garage and R2, and mcp-grafana with the reader token.
- **Large prefixes.** Summing a prefix of a hundred thousand objects through
  `s3_list_objects` costs a hundred calls of the assistant; the `cli` source with
  `rclone size --json` is the answer once it lands.

## The catalog source

`kind: catalog` gives the assistant, and the autopilot after it, the catalog that `aye check`
enforces, without a database and without a path to anything secret:

```yaml
hosts:
  central:
    modules:
      mcp:
        sources:
          catalog:
            kind: catalog
            description: Hosts, services, alerts and runbooks of the acme catalog.
            tools: [list_services, get_service, list_alerts, get_runbook]
```

**Built at render time.** `aye render` writes `mcp-catalog/catalog.json` for the host with the
source: every host (group, zone, architecture, provider, module names), every service the
render puts in a compose file and every service of the manifest (id `host/name`, source,
state, group, criticality, description, url, `depends_on`, `clients`, metrics, alert names, log
source, status mark, cloud dependency names), the alerts those services name (title, severity,
`for`, summary, expression or baseline metric, owning module or service, runbook name and url),
and the runbook pages of those alerts, each cut at 20000 characters. A page of a built-in
module comes from `docs/runbooks/`, which the wheel carries as the package resource
`ayeops/runbooks/` ([releasing](releasing.md#what-the-wheel-carries)), so a render from an
installed `aye` serves the same text as one from a checkout; a manifest's own alerts read
`runbooks/` next to the manifest, a local module's its own `runbooks/`. The fields are picked one
by one rather than copied and filtered, so a key added to the manifest later stays out until
the builder names it. Left out on purpose: `login` (free text that may hold a user name or a
hint), `checks` (URLs and SQL), every module setting, the host's `address` and `public_bind`,
and anything from `.env`; the render never reads `.env`, and no value of a variable that
`aye permissions` lists can reach the file. A URL's user and password and the values of query
parameters named like a token, key, secret, password, signature or credential are masked as
`***` in every string, the summaries included. The tests render a manifest with such values,
with every variable of `aye permissions` set in the environment and in a `.env` next to it, and
look for them in the file.

**Served by a stdlib server.** `mcp-catalog` runs `catalog_server.py` from the module's
`files/`, one Python file on the standard library alone, on `python:3.13.16-alpine3.23`
(amd64 and arm64) as user 65534 with a read-only root, no capability and the two files mounted
read-only. It speaks MCP's streamable HTTP in its stateless form: `POST /mcp` with one JSON-RPC
message or a batch, answered as `application/json` (a notification gets `202`), `initialize`
echoing a protocol version it knows (`2025-11-25`, `2025-06-18`, `2025-03-26`, `2024-11-05`) or
offering its newest, `ping`, `tools/list` and `tools/call`; `GET /mcp` is `405`, since it sends
nothing unasked. Every tool carries `readOnlyHint` and a closed input schema, and arguments are
checked before a lookup (strings up to 200 characters, enums, a `limit` from 1 to 1000); a
lookup that finds nothing is a tool error that names the tool to ask first. Bodies over 64 KiB
are refused. A request refused before its body is read (`404`, `411`, `413`, `415`, a `400`
for an unknown protocol version) that carries a body gets `Connection: close`; the server
stops sending, then reads away at most the declared length and 1 MiB within 2 seconds, so the
client gets the answer instead of a connection reset. It answers only on the compose network; the hub reaches it, and the hub's allow
rule names its tools.

**When it changes.** The file is a mounted config file, so a deploy that changes the catalog,
an alert or a runbook restarts `mcp-catalog` and nothing else of the hub. vmagent scrapes it as
`mcp-catalog:8000` for `ayeops_mcp_catalog_requests_total{method}`,
`ayeops_mcp_catalog_tool_calls_total{tool,outcome}` and `ayeops_mcp_catalog_entries{kind}`;
`target-down` fires when it stops answering.

Not run: the server under compose behind agentgateway. The tests hold the rendered target to
agentgateway's schema and run a client session against the server over HTTP on loopback.

## The four gateways

Read on 2026-10-31 from each project's README, docs and source at its latest release tag; no
gateway was installed. Every cell names where it was read, in brackets; the keys are listed
under [Sources](#sources). *Not found* means the docs and the source at that tag show nothing,
not that the feature is impossible.

| | agentgateway | IBM ContextForge | Docker MCP Gateway | Microsoft MCP Gateway |
|---|---|---|---|---|
| License, language | Apache-2.0, Rust [AG-repo] | Apache-2.0, Python (FastAPI) [CF-repo] | MIT, Go [DK-repo] | MIT, C# on .NET 8 [MS-repo] |
| Version read | `v1.7.0`, 2026-10-27 | `v1.1.0`, 2026-10-20 | `v0.44.3`, 2026-10-26, every release marked prerelease | `v1.0.0`, 2026-10-16, a tag without a release |
| Proxy an MCP server over HTTP | streamable HTTP (`mcp`) and SSE (`sse`) targets, several targets multiplexed into one endpoint with `<target>_<tool>` names [AG-schema, AG-modes] | streamable HTTP and SSE "gateways" federated into one catalog, exposed as virtual servers `/servers/<id>/mcp` [CF-schemas] | catalog `type: remote` with `transport_type: http` or `sse` [DK-remote] | only through its sample `mcp-proxy` image deployed as an adapter (`MCP_PROXY_URL`) [MS-proxy] |
| Proxy an MCP server over stdio | `stdio` target: the gateway spawns `cmd` with `args` and `env` itself, inheriting its own environment unless `clear_env` [AG-schema, AG-upstream] | not for upstreams (`STDIO ... not supported for gateways`); `mcpgateway.translate` wraps a stdio server into SSE or streamable HTTP as a separate process [CF-schemas, CF-translate] | starts each server as a container through the Docker socket; `--static` connects to containers compose already runs [DK-run, DK-static] | the `mcp-proxy` image with `MCP_COMMAND`, as a Kubernetes StatefulSet [MS-proxy, MS-readme] |
| OpenAPI to MCP tools | `openapi` target: a spec file, inline or URL plus the upstream `host`; one tool per `operationId` [AG-schema, AG-openapi] | no whole-spec import: REST tools are registered one by one, OpenAPI only generates one tool's schema [CF-rest] | not found [DK-tree] | not found; its OpenAPI file describes its own management API [MS-readme] |
| CLI commands as tools | not found [AG-schema] | not found [CF-translate] | `type: poci`: a tool with JSON-schema `parameters` runs one container per call with `{{arg}}` substitution, needs the Docker socket, no timeout setting found [DK-catalog] | not found [MS-readme] |
| Client auth | `jwtAuth` (JWKS file or URL), `mcpAuthentication` (OAuth resource server with protected-resource metadata), `apiKey`, `basicAuth`, `extAuthz` (its own oauth2-proxy example) [AG-schema, AG-o2p] | its own JWTs (HS256, or RS256 with a public key) and gateway-issued API tokens; SSO (Google, GitHub, Entra, Keycloak, OIDC) for the UI; `TRUST_PROXY_AUTH` with `PROXY_USER_HEADER` behind a proxy such as oauth2-proxy [CF-config, CF-proxy] | one static bearer token, `MCP_GATEWAY_AUTH_TOKEN` (random if unset), or `--allow-unauthenticated`; no OIDC for clients [DK-auth] | Entra ID only; otherwise a development mode that trusts `X-Dev-*` headers [MS-program] |
| Per-tool ACL | `mcpAuthorization` CEL rules on `mcp.tool.target`, `mcp.tool.name` (unprefixed) and JWT claims; tools no rule allows are removed from `tools/list` [AG-authz, AG-rbac] | teams, tool visibility and roles with `tools.read` / `tools.execute`; finer rules through the optional PDP plugin (OPA, Cedar) [CF-rbac] | static `--tools server:tool` allowlist; the policy engine is a no-op outside Docker Desktop [DK-run, DK-policy] | per adapter by Entra app role, no per-tool rule inside an adapter [MS-roles] |
| Audit log of calls | a structured log line per MCP request with method, target, tool, session and trace id, text or JSON; OpenTelemetry traces [AG-observability] | JSON logs, an audit trail and permission audit in its database, OpenTelemetry [CF-audit] | `--log-calls` (on by default) to stderr; interceptors receive each call as JSON [DK-calls] | Application Insights only [MS-program] |
| Metrics, health | Prometheus on `15020/metrics` with `agentgateway_mcp_requests_total`; readiness `15021/healthz/ready`; `agentgateway_config_synchronized` after a reload [AG-config, AG-metrics] | `/health`, `/ready`; Prometheus at `/metrics/prometheus` when `ENABLE_METRICS=true`, generic HTTP metrics only [CF-metrics] | `GET /health`; metrics only as OpenTelemetry push [DK-auth, DK-telemetry] | `GET /ping`; Application Insights, no Prometheus found [MS-program] |
| Runtime | one binary, one container; no database, no Kubernetes, no Docker socket [AG-database] | one container with SQLite, or the full stack with PostgreSQL, Redis and nginx; no socket [CF-run] | one container; needs `/var/run/docker.sock` unless every server is remote or `--static` [DK-run, DK-static] | needs Kubernetes: the Kubernetes client is always wired in and adapters become StatefulSets; Redis or Cosmos DB [MS-program] |
| linux/amd64 and linux/arm64 | both, `ghcr.io/agentgateway/agentgateway:v1.7.0` [AG-image] | both, `ghcr.io/ibm/mcp-context-forge:v1.1.0` [CF-image] | both, `docker/mcp-gateway:v0.44.3` [DK-image] | amd64 only, `ghcr.io/microsoft/mcp-gateway:1.0.0` [MS-image] |
| Footprint | 40 MB compressed image; the Helm chart requests 128 MiB [AG-image, AG-helm] | 130 MB compressed image; its compose file reserves 4 GB per replica, nothing smaller documented [CF-image, CF-run] | Go on Alpine, no figure for the gateway; each server container defaults to 2 GB and 1 CPU [DK-run] | 512 MiB limit per gateway pod plus Redis [MS-k8s] |
| Config | one YAML or JSON file, reloaded on change; `$VAR` expanded from the environment; the admin UI on `localhost:15000` can be turned off [AG-config, AG-env] | environment for the server; gateways, tools and roles through the admin API or UI into its database; JSON export and import [CF-config, CF-export] | catalog YAML plus flags [DK-run, DK-remote] | REST API and a portal; entities in Redis or Cosmos DB [MS-readme] |
| Maturity on 2026-10-31 | 5,810 stars, monthly minor releases, 305 open issues [AG-repo] | 4,730 stars, patch releases every one to three weeks, 983 open issues [CF-repo] | 1,617 stars, releases every one to four weeks, all prerelease [DK-repo] | 899 stars, one tag, 65 commits [MS-repo] |

What each gateway lacks, and how AyeOps fills it or refuses it:

| Gap | Fill or refuse |
|---|---|
| stdio servers in the agentgateway image | the image is distroless (`cgr.dev/chainguard/glibc-dynamic`, uid 65532, no shell, node or uv) [AG-dockerfile]. A static server binary is copied from its own image into a volume the gateway mounts and runs as a `stdio` target; that is how the S3 tools run. A server that needs an interpreter runs in its own container behind a stdio bridge; not implemented yet |
| CLI commands, every gateway but Docker's | a small adapter container of AyeOps: the commands from the manifest as tools with typed arguments, executed without a shell and with a timeout, serving streamable HTTP to any gateway |
| OpenAPI on ContextForge, Docker and Microsoft | agentgateway's own OpenAPI support could run as an adapter behind them; until then `aye check` refuses `openapi` with these providers |
| stdio on ContextForge | `mcpgateway.translate` from the ContextForge image as a sidecar per server |
| Docker MCP Gateway without the Docker socket | remote and `--static` servers only; the check refuses `cli` and stdio sources, which would need the socket |
| Microsoft MCP Gateway | Kubernetes, Entra ID and amd64 only: none of them fits a docker compose host, so the check refuses it until AyeOps renders Kubernetes |

## The S3 tools

The tools must list buckets and objects, read object metadata and sizes, and tell the age of
the newest backup, against Garage from the [objectstore](modules.md#objectstore) module,
Cloudflare R2, MinIO, Backblaze B2 and AWS. They need a custom endpoint, region and path-style
addressing.

| Candidate | Fit |
|---|---|
| [awslabs/mcp](https://github.com/awslabs/mcp) | Apache-2.0, but no general S3 server: `s3-tables-mcp-server` serves S3 Tables (Iceberg); `aws-api-mcp-server` wraps the AWS CLI and rejects an `--endpoint-url` that is not a loopback address, so it cannot reach Garage [S3-awslabs] |
| [txn2/mcp-s3](https://github.com/txn2/mcp-s3) `v1.5.1` | Apache-2.0, Go, static binary (`CGO_ENABLED=0`). `s3_list_buckets`, `s3_list_objects` (key, size, last modified, paged), `s3_get_object_metadata` (HEAD), `s3_get_object`; writes `s3_put_object`, `s3_delete_object`, `s3_copy_object` blocked by `MCP_S3_EXT_READONLY`, on by default. `S3_ENDPOINT`, `AWS_REGION`, `S3_USE_PATH_STYLE`. Image `ghcr.io/txn2/mcp-s3:v1.5.1` for amd64 and arm64. stdio only. 11 stars, released monthly, the open issues are dependency updates [S3-txn2] |
| [aws-samples/sample-mcp-server-s3](https://github.com/aws-samples/sample-mcp-server-s3) | MIT-0, a sample: three tools, no HEAD, no endpoint setting, no image, last commit 2025-12 [S3-sample] |
| [rclone](https://github.com/rclone/rclone) `1.75.2` | MIT, every S3 dialect, configured from the environment alone; `lsjson`, `size --json` (count and bytes in one call). No MCP mode, and its remote-control API has no read-only scope. Image `rclone/rclone:1.75.2` for amd64 and arm64 [S3-rclone] |

**Choice: txn2/mcp-s3**, the only maintained, read-only-by-default S3 MCP server that takes a
custom endpoint and path-style addressing. Its listing returns sizes and modification times,
so the total size and the newest backup under a prefix are a listing away (1000 keys a call).
`s3_presign_url` counts as a read but signs `PUT` as well, so AyeOps treats it as a write. The
rclone path stays as the first CLI source once that kind lands: `rclone size --json`
answers the total in one call for prefixes too large to list.

## Sources

Paths are at the release tag named in the table; image cells are the registry's manifest index
for that tag, fetched anonymously.

| Key | Where |
|---|---|
| AG-repo | `gh api repos/agentgateway/agentgateway` |
| AG-schema | agentgateway `schema/config.json`, `schema/config.md` |
| AG-modes, AG-authz, AG-observability | agentgateway.dev standalone docs: `mcp/configuration-modes`, `mcp/mcp-authz`, `mcp/mcp-observability` |
| AG-openapi, AG-o2p | agentgateway `examples/mcp-openapi`, `examples/traffic-oauth2-proxy` |
| AG-upstream, AG-rbac | agentgateway `crates/agentgateway/src/mcp/upstream/mod.rs` (stdio spawn), `mcp/handler.rs` and `mcp/rbac.rs` (rules see the unprefixed name, no rules allow everything) |
| AG-config, AG-metrics, AG-env | agentgateway `crates/agentgateway/src/config.rs` (addresses, defaults), `schema/metrics.md`, `crates/agentgateway/src/types/local.rs` (`expand_env`) |
| AG-database, AG-helm | agentgateway.dev `setup/database`; `controller/install/helm/agentgateway-standalone/values.yaml` |
| AG-image, AG-dockerfile | `ghcr.io/agentgateway/agentgateway:v1.7.0`; agentgateway `Dockerfile` |
| CF-repo, CF-image | `gh api repos/IBM/mcp-context-forge`; `ghcr.io/ibm/mcp-context-forge:v1.1.0` |
| CF-schemas, CF-translate, CF-rest | ContextForge `mcpgateway/schemas.py`; `docs/docs/using/mcpgateway-translate.md`; `docs/docs/using/rest-passthrough.md`, `mcpgateway/routers/openapi_schema_router.py` |
| CF-config, CF-proxy, CF-rbac | `docs/docs/manage/configuration.md`; `docs/docs/deployment/proxy-auth.md`; `docs/docs/manage/rbac.md`, `plugins/unified_pdp/README.md` |
| CF-audit, CF-metrics, CF-run, CF-export | `.env.example`; `docs/docs/manage/observability/prometheus.md`; `README.md` and `docker-compose.yml`; `docs/docs/manage/export-import.md` |
| DK-repo, DK-image, DK-tree | `gh api repos/docker/mcp-gateway`; `docker/mcp-gateway:v0.44.3`; the tree at the tag |
| DK-run, DK-remote, DK-static | `docs/generator/reference/mcp_gateway_run.md`; `docs/server-entry-spec.md`, `examples/remote_mcp`; `examples/compose-static`, `pkg/gateway/clientpool.go` |
| DK-catalog, DK-auth, DK-policy | `docs/catalog.md`; `pkg/gateway/auth.go`, `pkg/gateway/transport.go`; `pkg/policy/policy.go` |
| DK-calls, DK-telemetry | `pkg/interceptors/log_calls.go`, `examples/interceptors`; `docs/telemetry/README.md` |
| MS-repo, MS-image | `gh api repos/microsoft/mcp-gateway`; `ghcr.io/microsoft/mcp-gateway:1.0.0`, `.github/workflows/image.yml` |
| MS-readme, MS-program, MS-proxy | `README.md`; `dotnet/Microsoft.McpGateway.Service/src/Program.cs`; `sample-servers/mcp-proxy/README.md` |
| MS-roles, MS-k8s | `docs/entra-app-roles.md`; `deployment/k8s/local-deployment.yml` |
| S3-awslabs | awslabs/mcp `src/` on main; `src/aws-api-mcp-server/README.md`, `core/parser/parser.py` |
| S3-txn2 | txn2/mcp-s3 `pkg/tools/names.go`, `pkg/client/config.go`, `pkg/extensions/config.go`, `cmd/mcp-s3/main.go`, `.goreleaser.yml`, `Dockerfile`; `ghcr.io/txn2/mcp-s3:v1.5.1` |
| S3-sample | aws-samples/sample-mcp-server-s3 `src/s3_mcp_server/server.py`, `env.example` |
| S3-rclone | rclone `docs/content/rc.md`, `docs/content/s3.md`, `backend/s3/provider/`; `rclone/rclone:1.75.2` |
