# End-to-end run

The `e2e` workflow ([`.github/workflows/e2e.yml`](../.github/workflows/e2e.yml)) proves that a
manifest deploys from one file: it renders [`tests/e2e/e2e.yaml`](../tests/e2e/e2e.yaml), starts
the result with `docker compose`, and asserts that the stack is up, collects what it says it
collects and stays quiet while it is healthy. It runs on pull requests that touch `src/**`
(the modules live in `src/ayeops/modules/`), `examples/**`, `tests/e2e/**`, the workflow,
`pyproject.toml` or `uv.lock`, weekly on Sunday at 03:17 UTC, and by hand. A merge to `main`
does not run it again: the pull request already ran the same stack. A normal run is meant to
stay under 10 minutes on `ubuntu-latest`; the job stops at 25.

## Where it runs

On `ubuntu-latest`, always, unless the owner asks otherwise. GitHub-hosted minutes on standard
runners are free and unlimited for a public repository, so nothing counts them, and the code of
a pull request, which on a public repository may come from anyone's fork, must never run on a
machine of ours:

| Event | `e2e` runs on |
|---|---|
| pull request | `"ubuntu-latest"`, whatever the variables say |
| weekly schedule | `"ubuntu-latest"` |
| started by hand with `runner: hosted` (the default) | `"ubuntu-latest"` |
| started by hand with `runner: self-hosted` | the labels of `AYE_RUNS_ON`, or `"ubuntu-latest"` when it is not set |

The job's `runs-on` is
`fromJSON(github.event_name == 'workflow_dispatch' && inputs.runner == 'self-hosted' && vars.AYE_RUNS_ON || '"ubuntu-latest"')`:
only a run started by hand, which needs write access to the repository, makes the job of
`main` name the self-hosted runner, and `tests/test_workflows.py` evaluates the expression for every event of
every workflow with the variable set and fails when an event other than a push to `main`, a tag,
the schedule or a manual run could reach it. The same test holds `ci` and `mutants` to their
own rule: their pull-request jobs run on `ubuntu-latest`, and only their `push`, `schedule` and
`workflow_dispatch` runs follow `AYE_RUNS_ON`; `image-tags` and the `baselines` job of
`mutants` use the same expression.

That guards the workflow files on `main` only. A pull request from a fork runs the workflow
files of its own branch and can name any label of a runner registered to the repository, so the
self-hosted runner is for the owner's manual runs only, for example to try the stack on arm64 or
on a bigger machine, and for a public repository it must never be a production host: its user is
in the `docker` group, which is root on that machine. Put it on a machine that holds nothing of
value, keeps no credentials and is not in the fleet, ideally an ephemeral runner
(`./config.sh --ephemeral`) wiped after each job.

### The box

On either runner the whole stack runs inside one container, the box:
[`tests/e2e/box.sh`](../tests/e2e/box.sh) starts a privileged `docker:<version>-dind` container
named `aye-e2e-<run id>-<attempt>`, with its own Docker daemon, network namespace, iptables
rules and loopback addresses. The checkout is copied in, uv and Python are installed inside
(uv from its pinned image, pulled by the inner daemon), and every later step is
`box.sh exec ...`: render, `.env` and certificates, `docker network create`, the `DOCKER-USER`
rule (as root inside the box, no `sudo`), `docker compose up`, the smoke tests and the logs.
So the runner's machine only sees one container:

- **Nothing is published on the host.** The stack's ports (`127.0.0.1:8428`, `:3000`, `:9100`,
  `127.0.0.2:...`, `10.213.0.1:80`, ...) are bound inside the box's network namespace; the box
  itself publishes no port and mounts nothing from the host. Its daemon listens only on its
  own unix socket, not on TCP.
- **The host's network and Docker stay as they are.** No interface, route or iptables rule of
  the host changes; the containers, images, volumes and networks of the stack belong to the
  inner daemon, whose data is the box's anonymous volume.
- **Bounded.** The box gets 8 GB of memory and no swap (the stack needs about 6 GB), so a
  runaway stack is killed inside the box. It stops itself after 30 minutes, past the job's
  25-minute timeout, and `--rm` then removes it with its volume, should the runner die before
  the last step.
- **Always removed.** The last step, `remove the box`, runs with `if: always()`, also after a
  failure, a timeout or a cancel, and runs `docker rm --force --volumes` on the box, which takes
  the inner daemon's images, containers and volumes with it. On a failure, `collect logs` first
  copies the compose logs and the box's own log out to the `e2e-logs` artifact.

The images are pinned by version and digest at the top of `box.sh`, and Renovate bumps them
there. GitHub-hosted runners take the same path; `room for images`, which frees disk on the
GitHub machine with `sudo` and moves Docker to `/mnt`, runs there only, before the box starts,
so the box's volume lands on the larger disk.

Prerequisites for the owner's self-hosted runner, a machine that runs nothing else of value
(never a production host while the repository is public):

- **Docker access for the runner's user**: `sudo usermod -aG docker <runner user>`, then restart
  the runner service (`sudo ./svc.sh stop && sudo ./svc.sh start` in the runner's directory), so
  the new group applies. Nothing else needs `sudo`. Membership of `docker` amounts to root on
  that host, which starting a privileged box needs anyway, so give it to a user that only runs
  the runner.
- **Memory and disk**: 8 GB that the host can spare during a run, and room for the images (about
  as much as a fresh AyeOps host pulls) in Docker's data root, which `up` checks: above 80 % used
  it stops before the tests.

Every run pulls its images afresh inside the box, from the runner's address and without the
host's credentials or cache, so a busy self-hosted runner can meet Docker Hub's pull limits for
anonymous clients.

## What runs

Three hosts in one box. `central` and `edge` are two compose projects on the box's Docker
daemon; `offline` is rendered and validated only.

| Host | Modules | Started |
|---|---|---|
| `central` | metrics server with `backup: true`, logs server, node, probes, grafana, checks, ingress, auth, portal, objectstore, secrets, backups | everything |
| `edge` | metrics agent, logs agent, node, containers, disk-forecast, network, offsite, hygiene (`dry_run`) | everything |
| `offline` | metrics agent, cloudflare, smart, gpu | `docker compose config -q` only |

Next to them `tests/e2e/apps/app.compose.yaml` runs the catalog services of the manifest as
the project `e2e-apps`: `web` (nginx with a page that says `aye-e2e`) and `db` (PostgreSQL with
one `orders` row). They are the targets of the probe, of the `http`, `tcp` and `sql` checks and
of the Grafana PostgreSQL datasource.

## What the smoke tests assert

[`tests/e2e/test_stack.py`](../tests/e2e/test_stack.py), marker `e2e`, skipped unless `E2E=1`.
Everything it compares against is read from the render, so a new module, scrape job, alert
rule, dashboard or datasource is covered without touching the test.

- Every service of every started host exists, runs and is `healthy` where it has a healthcheck.
- `up == 1` in VictoriaMetrics for every job of every host's `vmagent/scrape.yaml`, labelled with
  that host: the edge samples arrive through `remote_write`.
- Every `blackbox-http` target answers (`probe_success == 1`), including the ingress route
  over HTTPS; every `http` and `tcp` check passes, every `sql` check reports `ok` and a value.
- Grafana: the datasources are exactly the rendered ones and each answers its health check
  (VictoriaMetrics, Loki, PostgreSQL); the provisioned alert rules and the dashboards are exactly
  the rendered ones, by uid and count.
- Grafana reader, rendered because the stack enables `incidents`, which reads Grafana with its
  token: `ayeops_grafana_reader_endpoint_ok` is `1` for every endpoint; the token
  from the `grafana-reader` volume sees every rendered rule and reads silences, alerts and alert
  annotations, and is refused (`403`) creating a silence, listing service accounts and reading
  the server settings.
- Dashboards drawn in Grafana: the reader counts `0` unprovisioned dashboards and
  `aye dashboards drift` exits `0`; a dashboard created through the API makes it exit `1` with
  that uid as `unmanaged`, `aye dashboards pull -o <tmp>` writes it without `id` and `version`
  and a second pull leaves it `unchanged`; the dashboard is deleted again.
- Loki returns a line pushed to it, and holds container logs from every host that runs Alloy.
- Offsite: the agent made a snapshot of `/tmp/aye-e2e-backups/e2e.dump` in Garage on `central`,
  its restore test passed, and `restic dump latest` gives back the same bytes.
- Metrics store backup: two minutes after the start `victoriametrics-backup` archived a
  snapshot of the store (`ayeops_backup_last_run_success{backup="metrics"} 1` with a size),
  deleted the snapshot (`vm_snapshots 0`), and its restore test opened the archive in a scratch
  VictoriaMetrics and found series of `up` (`ayeops_backup_restore_test_ok 1`).
- Hygiene: one pass in dry-run removes no image and reports `ayeops_hygiene_dry_run 1`.
- Portal: every page answers `200`, and `services.html` names every service of every host,
  `offline` included, and every catalog service.
- Ingress: every route answers through Traefik with the certificate of the route's name; the
  routes behind `auth` redirect to the sign-in (Google or `/oauth2/`), the open ones answer,
  an unknown name gets `404`.
- No alert on a healthy stack: after every rule was evaluated once more, no rule is `firing` or
  in `error`, and none is `pending`. Two kinds of pending rules are reported as warnings instead:
  rules with `for` of an hour or more, which a run this short cannot judge, and rules on the
  runner's own disk, memory, swap and network card (`node_filesystem_*`, `node_memory_*`,
  `node_vmstat_*`, `node_network_*`),
  which describe the GitHub machine rather than the stack.

## What it does to the render, and why

The rendered files are used as they are. [`tests/e2e/stack.py`](../tests/e2e/stack.py) adds:

- **`.env` per host** from its `.env.example`, values generated in the formats of
  [modules](modules.md) (Garage's `GK` key id, hex where a value lands in a URL, the oauth2-proxy
  cookie secret), one value per name across hosts, and `AYE_OFFSITE_*_KEY` equal to the
  objectstore's keys, as the [offsite runbook](runbooks/offsite.md) says.
- **A second loopback for `edge`.** Two hosts on one daemon would both bind `127.0.0.1:8429`,
  `:9100`, `:12345`. `e2e.override.yaml` moves the `127.0.0.1` ports of `edge` to `127.0.0.2`;
  ports on `public_bind` stay.
- **A local CA instead of Let's Encrypt.** The runner has no DNS zone for the ACME challenge.
  Traefik gets a default certificate for `*.ops.e2e.test` signed by a CA made for the run
  (`ingress/dynamic/e2e-tls.yml`), and blackbox trusts that CA through `SSL_CERT_FILE`. The
  ACME resolver still runs and fails in the log.
- **The address `10.213.0.1`.** `central`'s `address` and `public_bind` are the gateway of a
  Docker network `aye-e2e` created for the run, an address inside the box that containers
  reach. A rule in `DOCKER-USER` of the box accepts forwarded traffic to published ports, which
  is what a real private network gives two hosts. The box's daemon is told to use the nftables
  flavour of iptables (`DOCKER_IPTABLES_LEGACY` empty), so the rule lands in the same tables as
  Docker's own.
- **Docker on `/mnt`.** The GitHub runner's root disk starts three quarters full, and the images
  and volumes of the stack push it past 90 %, where Loki's write-ahead log refuses every push
  (`Ingester is shutting down`, HTTP 500) and the disk alerts fire for the runner. The job moves
  the runner's Docker data root, and with it the box's volume, to the larger `/mnt` disk, and
  `up` stops before the tests when the box's data root is above 80 % full.
- **`restic init`** once before `edge` starts, the manual first-run step of the offsite runbook.

## What it cannot cover

- **Certificates and DNS.** No ACME certificate is issued and `aye dns` is not run: both need
  a Cloudflare zone and token.
- **Sign-in.** oauth2-proxy and Grafana's Google login are configured with generated client
  ids; the test sees the redirect to Google, not a completed sign-in.
- **Cloudflare analytics** (`offline`): needs an account and an API token.
- **SMART and GPU** (`offline`): the runner has neither a physical disk nor an NVIDIA card.
- **Alert delivery.** `alerting.contact` is `none`: Telegram and mail need real accounts.
- **Two machines.** Both hosts share one kernel, one disk and the box's Docker daemon, so
  host-level numbers (disk, memory, `node_*`) are the runner's, and Alloy on each host still
  only ships its own compose projects.
- **arm64.** The runner is amd64; the `compose` job of `ci.yml` checks that every image is
  published for both architectures.

## Run it locally

The way CI runs it, in a box, needs a Docker that runs privileged containers, `git`, `tar`
and 8 GB of memory for the box; nothing else is installed on the machine, and the tests reach
`10.213.0.1` and `127.0.0.2` inside the box. CI uses Docker Engine on Linux; Docker Desktop
has not been tried.

```sh
sh tests/e2e/box.sh start
sh tests/e2e/box.sh exec uv run python -m tests.e2e.stack all /tmp/aye-e2e
sh tests/e2e/box.sh exec env E2E=1 uv run pytest tests/e2e -m e2e -v
sh tests/e2e/box.sh logs e2e-logs
sh tests/e2e/box.sh stop
```

`start` copies the files git knows (tracked, and untracked ones that are not ignored), so a
change made after `start` needs `stop` and `start` again. `AYE_E2E_BOX` names the box
(`aye-e2e` by default).

Without the box, on the machine itself, it needs Linux with Docker Engine and compose v2.24 or
newer, `openssl`, about 6 GB of memory, and a machine that runs no AyeOps stack of its own: the
stack binds the same ports as one (`127.0.0.1:8428`, `:3000`, `:9100` and more). Docker
Desktop does not fit this way: the tests reach `10.213.0.1` and `127.0.0.2`, which live inside
its VM.

```sh
uv run python -m tests.e2e.stack all /tmp/aye-e2e
E2E=1 E2E_RENDER=/tmp/aye-e2e uv run pytest tests/e2e -m e2e -v
uv run python -m tests.e2e.stack logs /tmp/aye-e2e --out e2e-logs
uv run python -m tests.e2e.stack down /tmp/aye-e2e
```

`all` is `render`, `prepare` and `up`; each also runs on its own. As root (in the box) the
`DOCKER-USER` rule is added with `iptables` directly, otherwise with `sudo -n`; without
passwordless `sudo` it is skipped and `sudo` says why. `down`
removes the three compose projects with their volumes and the network; the backup file in
`/tmp/aye-e2e-backups` stays. `E2E_WARM_UP` (default 300) and `E2E_BUDGET` (default 480)
are the seconds a single assertion and the whole run may wait for the stack to settle.
