# AyeOps documentation

AyeOps is an open-source ops kit. One manifest describes the hosts and the services of a small
fleet; `aye check` holds every service to the [catalog rules](rules.md) and `aye render` turns
the manifest into a docker compose project per host.

- [Manifest reference](manifest.md): every field of the manifest
- [Catalog rules](rules.md): what `aye check` enforces and why
- [Modules](modules.md): what each module runs, its settings and alerts
- [Checks](checks.md): HTTP, TCP, DNS, script and read-only SQL checks declared under a service, each with its metrics and alert
- [Deploy](deploy.md): `aye deploy` swaps a host's files atomically and recreates exactly the services whose config changed, and rolls a fleet out canary first; `aye diff` shows beforehand what it would change
- [Host init](host-init.md): `aye host-init` gives a host a deploy user that may run one helper on one directory, `aye permissions` lists the narrowest external credentials, and what neither can promise
- [Ansible inventory](inventory.md): `aye inventory` writes the manifest's hosts as an Ansible inventory, grouped by zone, group, profile, module and service, for playbooks a team already runs
- [Discover](discover.md): what `aye discover` probes on a host, how it suggests manifest entries and how `--adopt` describes a running host as it is
- [Delivery metrics](delivery.md): `aye report` for CI, tests, coverage, mutants, deploys and merges, and how the samples reach storage
- [Runbooks](runbooks/): one page per built-in alert
- [End-to-end run](e2e.md): the CI job that deploys a manifest on one runner and checks what it runs
- [Status page](status.md): the multi-tenant Cloudflare Worker, its push contract and budget
- [MCP hub](mcp.md): the four MCP gateways compared, the sources of tools, what the check refuses
- [Releasing](releasing.md): how a `v<version>` tag becomes a GitHub release, how a manifest pins the releases it accepts and how to verify a release's provenance
- [Roadmap](roadmap.md)
- [Autopilot](autopilot.md): level 1 ships in shadow mode as the `autopilot` module (a dossier and ranked hypotheses for each critical alert, no action, nobody messaged by default), and the design of the levels after it
- [Failover](failover.md): the design of failover by priority; of it, the service `criticality` ships, which the [incident journal](modules.md#incidents) weighs downtime by

## Commands

`aye` alone prints the common commands; `aye <command> -h` gives the options of one and an
example. Every command that reads a manifest takes its path first and may leave it out: the
default is the file `AYE_MANIFEST` names, else the one `ayeops.yaml` or `manifest.yaml` of the
current directory (both there is an error). `--host` may be left out when the manifest has one
host, and `aye deploy`, `diff` and `doctor` take `--ssh`, `--sudo`, `--helper` and `--dir`
from the host's [`connect`](manifest.md#connect) when they are not given.

| Command | What it does |
|---|---|
| `aye up [<manifest>] [--host <name>] [-o rendered] [--start]` | for a host that runs on this machine: runs the check, renders into `-o` as `aye render` does, writes the generated secrets the host's `.env` lacks as `aye secrets gen --env-file` does, then prints `docker compose -f <dir>/<host>/compose.yaml up -d`, or runs it with `--start`; warns when the host's `arch` is not this machine's; exit 1 while a variable no module generates is unset |
| `aye down [<manifest>] [--host <name>] [-o rendered] [--volumes] [--stop]` | prints the matching `docker compose -f <dir>/<host>/compose.yaml down [--volumes]`, or runs it with `--stop`; exit 2 when nothing is rendered there |
| `aye check <manifest>` | checks the manifest's [`ayeops` pin](manifest.md#pinning-a-release) against the running version, then validates the manifest against the JSON Schema and the catalog rules; exit code 1 on any error; warnings (a [waiver](rules.md#waivers-end) that ends within fourteen days, an [unused entry](rules.md#unused-entries)) pass unless `--strict`; `--today YYYY-MM-DD` measures waivers against another day |
| `aye render <manifest> [-o <dir>]` | runs the check, then writes `<dir>/<host>/`, `rendered/<host>/` without `-o`, for every host and the index `<dir>/.aye-render`; removes what the previous render listed there and this one no longer produces, nothing else, see [render into a directory](deploy.md#render-into-a-directory) |
| `aye render <manifest> -o <dir> --check` | writes nothing; lists the `added`, `changed` and `stale` paths a render would produce in `<dir>`, exit code 0 when it matches, 1 otherwise |
| `aye portal <manifest> -o <dir> [--discovered <host.json>]` | writes the [portal](modules.md#portal) pages into `<dir>`; fails on drift, shows coverage gaps red |
| `aye status <manifest> [--sql]` | runs the check, then prints the public [status page](status.md) components as JSON, or as a D1 seed with `--sql` |
| `aye deploy <manifest> [--host <name>] [--ssh <alias>] [--sudo \| --helper] [--dir <path>] [--timeout <seconds>]` | runs the check, pulls the missing images of the new compose file on a staged copy, then ships one host's render over ssh, swaps its files by rename and runs `docker compose up -d --remove-orphans`; a failed pull changes nothing, see [deploy](deploy.md) |
| `aye deploy <manifest> (--host <name> --host <name> ... \| --all) [--parallel <n>] [--dry-run]` | the same for several hosts: a canary alone first, then waves of `--parallel` that all pull before any of them swaps, the central stack last, no new host after a failure, a summary per host, see [fleets](deploy.md#fleets) |
| `aye diff <manifest> (--host <name> ... \| --all) [--ssh <alias>] [--sudo \| --helper] [--dir <path>] [--timeout <seconds>]` | runs the check and, in one read-only ssh call per host, prints what `aye deploy` would change: added, changed and removed files, a unified diff per changed file and the services it would recreate; exit code 0 for no change, 1 for changes, 2 on an error, see [diff](deploy.md#diff) |
| `aye doctor <manifest> (--host <name> ... \| --all) [--ssh <alias>] [--sudo \| --helper] [--dir <path>] [--timeout <seconds>] [--parallel <n>]` | read-only preflight: runs the check, then one ssh call per host for docker and compose, arch, the directory, sudo or sudo-rs, free disk, clock skew, memory, the required `.env` names and a manifest for the host's arch per image, as a table of ok, warn and fail; exit 1 on a fail, see [doctor](deploy.md#doctor) |
| `aye host-init <manifest> [--host <name>] [--user aye] [--ssh-key <file>] [--from <cidr>]` | runs the check, then prints a root script for that host: a deploy user whose key runs only the `aye-host` helper on `/opt/ayeops/<host>`, a sudoers rule for exactly that, the module directories; see [host init](host-init.md) |
| `aye permissions <manifest>` | runs the check, then prints per host and module the external credentials the manifest needs and the narrowest grant of each, then the secrets you generate with their format and those you copy from another host, see [host init](host-init.md#aye-permissions) |
| `aye inventory <manifest> [--format ansible-yaml\|ansible-json] [-o <file>] [--check]` | runs the check, then writes an [Ansible inventory](inventory.md) of the hosts to stdout or `<file>`: `ansible_host` from `address`, the vars `ayeops_arch`, `ayeops_zone`, `ayeops_group`, `ayeops_project` and the groups `zone_`, `group_`, `profile_`, `module_` and `service_`; `--check` writes nothing, prints a diff and exits 1 unless `<file>` matches |
| `aye dns <manifest> [--apply]` | runs the check, then plans the Cloudflare A records of every [ingress](modules.md#ingress) route; `--apply` writes them |
| `--build-time`, `--commit` | options of `render` (with or without `--check`), `portal`, `deploy` and `diff`: the footer stamp; the defaults are now (or `SOURCE_DATE_EPOCH`) and `git rev-parse --short HEAD` next to the manifest, with `+` when the tree is dirty. `examples/rendered` is rendered with `--build-time "2026-01-01 00:00 UTC" --commit example` |
| `aye retire-rules <live rules.yaml> <rendered rules.yaml>` | adds `deleteRules` to the rendered alert rules for every rule the live file has and the render lost, for deploys that copy the tree themselves, see [retired alert rules](deploy.md#retired-alert-rules) |
| `aye discover --ssh <host> [--sudo] -o <file>` | runs the read-only [probe](discover.md) on a Linux host and saves its JSON report |
| `aye discover --suggest <file>` | prints a manifest fragment for a report: host block, modules, service stubs |
| `aye discover --adopt <file> -o <manifest> [--project <name>] [--until YYYY-MM-DD] [--observe]` | writes a manifest and a compose stub per compose project that describe the host as it runs, with dated waivers for what a person must decide, then checks it; its first render creates and recreates nothing that runs, see [adopting a host](discover.md#adopting-a-host); exit code 1 on a check error, a takeover or a file that exists |
| `aye secrets pull [-o .env]` | signs in as the host's machine identity and writes one folder of the [secrets manager](modules.md#secrets) to a `.env`, mode 600, in one rename |
| `aye secrets push <.env>` | adds the keys of an existing `.env` that the folder does not have yet; values already there win |
| `aye secrets set <KEY> [--from-clipboard]`, `ask <KEY> [--hint TEXT]`, `gen <KEY>` | store one key typed hidden, pasted into a one-time page on `127.0.0.1`, or generated in the format its module declares; never print it, refuse an existing key without `--replace` (`gen` never replaces), see [aye secrets](modules.md#aye-secrets) |
| `aye secrets gen [--manifest <file>] [--host <host>]` | stores every generated secret of the host that the folder lacks, each in its catalog format, and names the ones to copy from another host; without `KEY` and `--manifest` it takes the default manifest |
| `aye secrets gen [--manifest <file>] [--host <host>] --env-file <path>` | the same into a local `.env`, mode 600, with no secrets manager: appends the keys the file lacks, never replaces one, and names the variables no module generates |
| `aye secrets run [--folder F] -- <cmd>` | runs `cmd` with one folder in its environment only, keeps its exit code and masks the values in its output |
| `aye report <step\|run\|junit\|coverage\|mutants\|pr> ...` | pushes [delivery and quality metrics](delivery.md) of a CI job or deploy; never fails the build unless `--strict` |
| `python -m ayeops.comments [paths]` | fails when code, configuration or templates contain comments |
| `python -m ayeops.texthygiene [paths]` | fails on [invisible, bidi and lookalike characters](rules.md#the-text-hygiene-rule) in tracked files |

## Exit codes

| Code | Meaning |
|---|---|
| 0 | done: the check passed, the files were written, the hosts are deployed and healthy, nothing differs |
| 1 | findings or drift: the check found errors, `render --check` or `diff` found a difference, `doctor` a fail, a deploy or compose step failed, `aye up` found an unset variable |
| 2 | usage or environment: bad arguments, no manifest or a missing file, an unknown host or no `--host` on a manifest with several, a missing token, identity or `docker`, a `--dir` the helper or the backup timers refuse |

Three commands differ, as documented with them and kept so for the scripts that rely on it:
`aye diff` follows diff(1) and exits 2 on every error, a failing check included, since its 1
means "the deploy would change something"; `aye secrets` exits 2 when the identity is
missing or `run` has no command and 1 on every other error, an unknown `--host` of `gen`
included; and `aye report`
exits 0 even when it cannot push, unless `--strict`.

## Conventions

- No comments in code, configuration or templates. Explanations live here and in test names;
  CI runs the comment check as a test.
- Configuration comes from the manifest and from environment variables. Nothing in the kit
  carries a personal default; [`.env.example`](../.env.example) lists every variable a rendered
  compose project reads.
- Every image is pinned to an exact version and the digest of its multi-arch index
  (`name:tag@sha256:<digest>`), and published for `linux/amd64` and `linux/arm64`.
- Every service gets `mem_limit`, `cpus`, a healthcheck and the label `io.ayeops.service=<name>`.

AyeOps is published by the team behind [trofey.app](https://trofey.app).
