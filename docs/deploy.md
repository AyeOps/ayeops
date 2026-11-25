# Deploy

```sh
aye deploy ops/ayeops.yaml --host central --ssh ops-central --sudo
aye deploy ops/ayeops.yaml --host central --ssh aye@10.8.0.1 --helper
aye deploy --host central
```

The last line does what the first does when `AYE_MANIFEST` names `ops/ayeops.yaml` (or an
`ayeops.yaml` or `manifest.yaml` is in the current directory) and the host says how it is
reached:

```yaml
hosts:
  central:
    connect: {ssh: ops-central, sudo: true}
```

`connect` holds `ssh`, `sudo`, `helper` and `dir`, the defaults of the flags of the same name;
a flag given on the command line wins, see [connect](manifest.md#connect). `aye diff` and
`aye doctor` read it too, and all three take the manifest's only host when `--host` and
`--all` are left out.

`aye deploy` runs the [check](rules.md), renders one host in memory and puts it live over ssh in
four calls, one more on the host that runs Grafana and one more on a host with backup timers.
Everything that can fail on the registry or on
the render runs before the first file moves:

| Step | Runs on the host | When it fails |
|---|---|---|
| `rules` | reads the live alert rules (Grafana host only), see [Retired alert rules](#retired-alert-rules) | stops before anything else; the running stack is untouched |
| `pull` | the new `compose.yaml` goes to `<dir>/.aye-staging/compose.yaml` and `docker compose --project-directory <dir> -f <dir>/.aye-staging/compose.yaml pull --quiet --policy missing --ignore-buildable` fetches every image it names that the host lacks, see [Pull before swap](#pull-before-swap) | stops before the swap, names each image that did not pull; no file moved, no container changed |
| `swap` | the render goes over as one gzipped tarball and every file moves into place by rename, see below | stops before compose; the running stack is untouched |
| `up` | `docker compose up -d --remove-orphans` in `<dir>`; the images are already on the host | `docker compose up failed`; the next deploy brings the stack to the render |
| `timers` | only on a host whose render holds `backups/systemd/` units, whose swap just removed some, or whose swap names `ayeops-<host>-backup-*` units still in `/etc/systemd/system` (as after a failed timers step): `src/ayeops/hostside/timers.sh` puts the systemd timers of the [backup jobs](modules.md#backups) with a `command` in place, enables and starts them, and takes off those that left the manifest, see [host-init](host-init.md#backup-timers); it prints `<alias>: 1 backup timer(s), 2 unit file(s) changed, 0 removed` | `backup timers not installed`; the deploy fails after the `ps` table |
| `ps` | `docker compose ps --all --format json`, printed as a table | the deploy fails |

The swap unpacks the tarball into `<dir>/.aye-staging`, inside the target directory and
therefore on the same filesystem. Only after the whole archive unpacked does each file move into
place with `mv`, a `rename(2)`: a reader sees the old file or the new one, never half of one. A
file whose content did not change is left alone, inode included. A file the previous deploy
shipped (listed in `<dir>/.aye-files`) and this one does not is removed. Nothing else is
touched: `.env` and any other file the manifest does not render stay. Last, the swap writes
`<dir>/.aye-env`, the SHA-256 of each `.env` value by key, mode 600 (owned by root under
`--sudo` and `--helper`), which [`aye diff`](#env-drift) compares on the host; no hash leaves it.
Then `ps` prints:

```text
central:/opt/ayeops/central: 1 file(s) changed, 0 removed
SERVICE          STATE    HEALTH   CONFIG CHANGED
grafana          running  healthy  1 file(s)
traefik          running  healthy
```

The exit code is 0 only when every declared service has a container that is `running` and not
`unhealthy`, and the backup timers, if any, are installed. A failure in the swap (a dropped connection, a truncated archive, a full disk) exits
before any file is moved and before compose runs, so the running stack is untouched.

Every ssh call has a deadline, `--timeout`, 1800 seconds by default: long enough for the pull to
fetch new images on a slow link, short enough that a hung connection does not hold a CI job
forever. When it passes, `ssh` is killed, the step fails with `ssh timed out after <n>s` and the
deploy exits 1 without running the steps after it. A timeout while reading the live alert rules
or in the pull stops before the swap, so the stack is untouched
(`pull timed out, the running stack is untouched`). A timeout in the swap stops before compose,
so no container changes, but the deploy cannot tell whether the host finished the swap before
the connection died, so it says `swap timed out, docker compose did not run, deploy again to
finish` rather than promising the old files: the swap is idempotent, and the next deploy
completes it. A timeout in `up` fails the deploy with `docker compose up failed`; compose on the
host may have stopped part way or still be running, and the next deploy brings the stack to the
render.

| Option | Default |
|---|---|
| `--host <name>` | a host of the manifest; repeat it for a [fleet](#fleets), the first one is the canary. With neither `--host` nor `--all` the manifest's only host; a manifest with several exits 2 and names them |
| `--all` | off; every host of the manifest, see [fleets](#fleets) |
| `--parallel <n>` | `1`; hosts deployed at once after the canary |
| `--dry-run` | off; prints the canary and the waves after the check and connects to nothing |
| `--ssh <alias>` | the host's `connect.ssh`, else the host name; anything `ssh` accepts, including an alias from `~/.ssh/config`. Only with a single host: a fleet reaches each host by its `connect.ssh` or its name |
| `--sudo` | the host's `connect.sudo`, else off; runs the remote side, pull, file swap and `docker compose`, under `sudo sh -c`, which needs sudo for any command |
| `--helper` | the host's `connect.helper`, else off; runs each step as `sudo -n /usr/local/libexec/ayeops/aye-host <dir> <step>` on a host prepared by [`aye host-init`](host-init.md); excludes `--sudo`, and `--dir` must then be `/opt/ayeops/<name>` |
| `--dir <path>` | the host's `connect.dir`, else `/opt/ayeops/<host>`; a host with backup timers deploys only there, since its units run the jobs from that path, and any other `--dir` exits 2 before the first ssh call |
| `--timeout <seconds>` | `1800`; how long each ssh call may take before it is killed and the deploy fails |
| `--build-time`, `--commit` | the portal footer stamp, as for `aye render` |

The remote host needs `sh`, `tar`, `cmp`, `grep`, `find`, `sha256sum` and `docker compose` 2.22
or newer (the first with `pull --policy`); [`aye doctor`](#doctor) fails an older one. A host
that runs backup timers also needs systemd (`systemctl`).

`--helper` is an option rather than detected: detection would cost an ssh call per deploy and
would have to guess from a failure, while an explicit flag fails loudly (`sudo: a password is
required`, or the helper's refusal) on a host that was not initialised. Both modes run the same
scripts: the helper's `pull` and `swap` steps are `src/ayeops/hostside/pull.sh` and `swap.sh`,
the files that `--sudo` ships as script text, so the guarantees above hold for both. The swap
refuses an archive that holds a link or a special file and an index entry that is absolute,
contains a `.` or `..` component or names `.aye-*`, before anything moves; a line like that in
the previous index is skipped instead of removed. [Host init](host-init.md) says what the helper
restricts and what a deploy key can still do.

## Pull before swap

Until the pull step existed, images were pulled inside `docker compose up -d`, after the new
files had already been swapped in. A registry login that expired, a rate limit, a typo in a tag
or an image without a `linux/<arch>` manifest then failed with the new `compose.yaml` live and
some services possibly recreated. Now the deploy pulls first, on a staged copy, and the running
stack only changes once every image it needs is on the host.

The step sends only `compose.yaml` on stdin; `pull.sh` writes it to `<dir>/.aye-staging/compose.yaml`
and runs

```sh
docker compose --project-directory <dir> -f <dir>/.aye-staging/compose.yaml \
  pull --quiet --policy missing --ignore-buildable
```

- `--project-directory <dir>` reads the live `<dir>/.env` for interpolation and keeps the
  project name of the running stack, so the pull resolves exactly the image references `up`
  will use. A variable the new render requires and `.env` lacks fails here, before the swap,
  where it used to fail in `up` after it.
- `--policy missing` pulls only the images the host does not have, which is what `up -d` pulls
  by default. An unchanged deploy asks the registry nothing, a registry outage blocks only the
  deploys that need a new image, and a tag that moved upstream is not refreshed, as before.
- `--ignore-buildable` skips services with a `build:` section, which compose builds on `up`.
  The rendered stacks have none today; the flag costs nothing without one, and it lets the
  helper run one fixed command line.
- `--quiet` drops the progress bars; errors still reach stderr.

When the pull fails, the script asks `docker compose config --services` for the services and
pulls each one alone, so the deploy can name what did not pull; the images that did pull stay
in the image cache, where they do no harm. Then it removes the staged copy:

```text
Error response from daemon: manifest for grafana/grafana:13.9.9 not found: manifest unknown
edge-02: image grafana/grafana:13.9.9 of grafana did not pull
edge-02: images not pulled, the running stack is untouched
```

A failed pull means: no file of `<dir>` moved, `compose up` did not run and no container
changed. On a first deploy the step creates the empty `<dir>` and nothing in it. Fix the cause
(log in, wait for the rate limit, correct the tag, pick an image with the host's arch) and
deploy again. [`aye doctor`](#doctor) checks the image manifests against the host's arch
beforehand.

A host prepared by `aye host-init` before the pull step has neither the `pull` line in its
sudoers rule nor `pull.sh`, so `--helper` stops at the pull, before the swap, with
`--helper needs the pull step of /usr/local/libexec/ayeops/aye-host; run the aye host-init script
of this host again if it predates the pull step`. Running the host-init script again installs
both.

## Diff

```sh
aye diff ops/ayeops.yaml --host central --ssh ops-central --sudo
aye diff ops/ayeops.yaml --all --helper
```

`aye diff` shows what `aye deploy` would change, before it does. It takes the options of a
deploy (`--host` or `--all`, `--ssh`, `--sudo` or `--helper`, `--dir`, `--timeout`,
`--build-time`, `--commit`), runs the check, renders each host in memory as the deploy would
and makes one ssh call per host that changes nothing on it:

```text
edge:/opt/ayeops/edge: 1 changed, 3 added, 0 removed
added    backups/jobs/acme-db.sh
added    backups/systemd/ayeops-edge-backup-acme-db.service
added    backups/systemd/ayeops-edge-backup-acme-db.timer
changed  compose.yaml
...
BACKUP UNIT                           CHANGE
ayeops-edge-backup-acme-db.service    installed
ayeops-edge-backup-acme-db.timer      installed
```

```text
central:/opt/ayeops/central: 2 changed, 0 added, 1 removed
changed  compose.yaml
changed  grafana/provisioning/alerting/rules.yaml
removed  grafana/dashboards/old.json
--- central:/opt/ayeops/central/compose.yaml
+++ render/compose.yaml
@@ -573,7 +573,7 @@
...
-      io.ayeops.config-hash: 422898b4cd2c5b3e
+      io.ayeops.config-hash: fc1ca68e8e5ed03b
...
SERVICE  CHANGE
grafana  recreated, config 1 file(s)
```

The exit code follows diff(1): 0 when the deploy would change no file, 1 when it would, 2 on an
error (the check failed, an unknown host, a host that cannot be reached or answers nonsense).
With several hosts each one prints under `== <host>`, and a summary closes the run:

```text
HOST     DIFF
edge-01  no change
edge-02  1 changed, 0 added, 0 removed, 1 service(s)
central  error
```

The ssh call sends the list of files the deploy would ship, each with the SHA-256 of its
rendered content, and the host answers per file: the same, added, or its current content in
base64 when the hash differs. Unchanged files never cross the wire: for the example's central
host, 49 files and 1.2 MB rendered, an up-to-date diff sends 4.6 KB and gets 1.6 KB back. The
script is
`src/ayeops/hostside/diff.sh`; `--sudo` ships it as text as it does the swap, `--helper` runs it
as the helper's `diff` step. It reads only what the deploy owns, and `.env` by key as described
in [.env drift](#env-drift):

- **Files to ship.** It reads only the paths it was asked for, and refuses one that is absolute,
  has a `.` or `..` component, names `.aye-*`, or is `.env` or `.env.*` other than
  `.env.example`. Content goes back only for a file listed in `<dir>/.aye-files`, the index of the
  previous deploy; a file in the way that the deploy did not put there is reported as changed
  without its content, and a symbolic link is reported as changed and never followed.
- **Removed files** come from the same rule as the swap: a path of the previous `.aye-files` that
  this render lacks, is safe and is a regular file. Its content is not read. `.env` and every
  other file the manifest does not render stay out of the answer, exactly as the swap leaves
  them alone.
- **Alert rules.** When the Grafana host's `rules.yaml` differs from the render, its content comes
  back like any other and the diff applies [retired alert rules](#retired-alert-rules) to the
  render as the deploy would, so a rule that left the manifest shows as a `deleteRules` entry and
  an `alert rule <uid> left the manifest` line. When it equals the render nothing is retired, so
  it is not sent. A live file that is not YAML is an error, since the deploy would stop on it.

A changed text file gets a unified diff, the deployed version against the render, cut after 200
lines with a count of the lines left out. The services come from the same two sources as the
deploy: `config N file(s)` is the `CONFIG CHANGED` column of the deploy's table (the services
whose `./` bind mounts hold an added or changed file), and `recreated`, `created` and `removed`
compare each service of the deployed `compose.yaml` with the render, the
[config hash](#config-hash) included, which is what `up -d --remove-orphans` acts on. A service
with `config` alone reads the changed file live and keeps its container. A `BACKUP UNIT` table
names the systemd units of [backup jobs](modules.md#backups) the deploy would install, change or
take off. `.env` is compared by key without moving a value, see [.env drift](#env-drift). What
the diff cannot see: images that moved under an unchanged tag, and units in
`/etc/systemd/system`: it compares the copies in `<dir>/backups/systemd/`, which equal the
installed units unless a timers step failed (the deploy repeats that step on every deploy of a
host whose render has timers or that still carries backup units of its own, so the next one
repairs it).

The portal pages carry the build time and commit in their footer, so a diff without
`--build-time` and `--commit` reports them changed just as a deploy at that moment would
rewrite them; pass the stamp the deploy will use to see only the manifest's changes.

### .env drift

Compose interpolates `<dir>/.env` into the services, so a key that changed there since the last
deploy recreates every service that names it, and a key the render requires that `.env` lacks
stops `up` on `${KEY:?...}`. The operator machine never has those values, so the same ssh call
compares them on the host and sends back key names only:

```text
edge:/opt/ayeops/edge: 0 changed, 0 added, 0 removed; .env 1 changed, 1 missing, 1 unused
SERVICE   CHANGE
sentinel  recreated, env 1 key(s)
updates   recreated, env 1 key(s)
ENV KEY               CHANGE             SERVICES
AYE_LEFTOVER          unused
AYE_TELEGRAM_CHAT_ID  missing, up fails  sentinel
AYE_UPDATES_TOKEN     changed            updates
```

- **missing**: a key of the rendered `.env.example` that the host's `.env` does not set;
  `up fails` when the compose reference is `${KEY:?...}` or `${KEY?...}`, which compose refuses
  to start without.
- **unused**: a key of the host's `.env` that no rendered file references any more (no
  `${KEY` anywhere in the render, not in `.env.example`); `COMPOSE_*` keys, which compose reads
  itself, are never unused. Nothing changes for it, so it does not make the exit code 1.
- **changed**: a key whose value differs from the `.env` of the last deploy. The swap stores
  `<dir>/.aye-env`, one line `KEY <sha256 of the value>` per key, mode 600, next to
  `.aye-files`; the diff hashes the current values on the host, compares the two there and
  answers `env-changed KEY`. Neither a value nor a hash crosses the wire or reaches the output.
  A key added or removed since that deploy is changed too; a removed key the render requires
  shows as missing. `SERVICES` and the `env N key(s)` reason of the service table name the
  services whose rendered compose definition holds `${KEY`, which `up -d` recreates.

The host side reads `.env` line by line as compose does: blank lines and `#` comments skipped,
an optional `export`, `KEY=value` with the key made of letters, digits and `_`, a line that is
not `KEY=` belonging to the value of the key above it (a quoted multi-line value). A value is
the text after the first `=`, as written, so a value that only changed its quotes counts as
changed. A `docker compose up` by hand after an edit of `.env` is not seen: the hashes are those
of the last `aye deploy`. A host whose last deploy predates this has no `.aye-env`, so the diff
names missing and unused keys and says `no .env hashes from a previous deploy on the host`; the
next deploy writes it. `.env` that is a symbolic link is not read (under `--helper` and `--sudo`
the diff runs as root in a directory the deploy user owns), nor one the ssh user cannot read.

The host needs `sha256sum` and `base64` besides the tools of the deploy; coreutils and busybox
have both. A host prepared by `aye host-init` before `aye diff` existed has neither the `diff`
step in its sudoers rule nor `diff.sh`, so `--helper` fails there with a hint; running the
host-init script again installs both. A host prepared before `.env` drift existed keeps an older
`diff.sh` and `swap.sh`: the diff then says `.env not compared, the host's diff.sh predates it`,
and running the host-init script again installs the new ones; the helper has no new step and
the sudoers rule does not change.

## Fleets

```sh
aye deploy ops/ayeops.yaml --all --helper --parallel 4 --dry-run
aye deploy ops/ayeops.yaml --all --helper --parallel 4
aye deploy ops/ayeops.yaml --host edge-07 --host edge-08 --host central --sudo
```

With more than one host, or with `--all`, the deploy rolls out in waves, in the spirit of a Kamal
rolling deploy. The check runs and the manifest renders once for the whole fleet, then:

1. **The canary** deploys alone and must come out healthy, by the same rule as a single host:
   every declared service has a container that is `running` and not `unhealthy`. With `--host` the
   canary is the first host named; with `--all` it is the first host of the manifest that does not
   hold the central stack.
2. **The rest** follow in waves of `--parallel` hosts, in the order given (`--all`: the manifest
   order), each wave started only after the previous one finished healthy.
3. **The central stack goes last.** A host that runs `grafana` or `objectstore`, or `metrics` or
   `logs` as `server` (the default role), holds what the others report to; it deploys after
   every leaf, so a bad change hits a leaf first and the dashboards and alerts still work while
   the leaves roll out. Named explicitly as the canary, it stays the canary.
4. **A wave pulls before it swaps.** Every host of a wave reads its alert rules and
   [pulls](#pull-before-swap) first; only when all of them pulled does any of them swap. One
   host whose registry login expired or whose arch lacks an image stops its whole wave before a
   single file moves, so a wave is never left half on the new render because of an image. The
   hosts of that wave that did pull report `pulled, not swapped: another host of wave <n> failed
   before its swap` and are `skipped` in the summary.
5. **A failure stops the rollout.** After a host fails no new host starts; the hosts of the same
   wave that are already past the pull finish. Every ssh call keeps its `--timeout`.

`--dry-run` prints the plan without any ssh:

```text
canary: edge-01
wave 1: edge-02, edge-03, edge-04, edge-05
wave 2: edge-06, central
```

Each host's output is printed under a `== <host> (<wave>)` line. A host deployed alone (the canary,
and every host with `--parallel 1`) prints as it goes; hosts of a wider wave print their block
when they finish, so lines of two hosts never mix. At the end comes a summary in plan order, and
the exit code is 0 only when every host is `deployed`:

```text
HOST     RESULT
edge-01  deployed
edge-02  deployed
edge-03  failed
edge-04  timed out
edge-05  deployed
edge-06  skipped
central  skipped
```

`timed out` means a step of that host hit `--timeout`, `failed` any other failure, `skipped` that
the host never started or pulled and did not swap; a `skipped` host runs what it ran before. A
fleet is deployed again by the same command: hosts already up to date
change no file and recreate no container. `--dir` applies to every host of a fleet; with
`--helper` each target must be `/opt/ayeops/<name>`, and a fleet with one that is not stops
before the first ssh call. A single `--host` without `--dry-run` deploys exactly as before, with
no header and no summary.

The rollout runs in waves rather than a sliding window so that the plan `--dry-run` prints is the
plan that runs: a fleet of 100 hosts at `--parallel 10` is a canary and ten waves, the last one
holding the central stack. A slow host holds back the next wave; that is the price of a plan you
can read before it starts. The pull barrier inside a wave adds no ssh call: each host still makes
the same four or five, and the slowest pull of a wave holds back its swaps.

## Doctor

```sh
aye doctor ops/ayeops.yaml --host central --ssh ops-central --sudo
aye doctor ops/ayeops.yaml --all --sudo --parallel 4
```

`aye doctor` tells you why a deploy would fail before it does. It takes the options of
`aye deploy` that say where and how to connect (`--host`, `--all`, `--ssh`, `--sudo`, `--helper`,
`--dir`, `--timeout`, 60 seconds by default, and `--parallel`) and changes nothing: it renders in
memory, writes no file locally or on the host and starts no container.

Locally it runs the check and looks for `ssh` on `PATH`; when either fails it stops there. Then
it makes one ssh call per host that runs one POSIX `sh` script (it runs on busybox), under
`sudo sh -c` with `--sudo` as the deploy would, and prints one table for all hosts:

```text
HOST     CHECK        RESULT  DETAIL
local    manifest     ok      aye check passes
local    ssh          ok      ssh is on PATH
central  ssh          ok      ops-central runs as root
central  docker       ok      27.3.1
central  compose      ok      2.29.7
central  daemon       ok      reachable as root
central  arch         ok      arm64 (aarch64)
central  dir          ok      /opt/ayeops/central is writable by root
central  sudo         ok      Sudo version 1.9.16p2
central  disk dir     ok      61 % free (28 GiB) on /opt/ayeops/central
central  disk docker  warn    12 % free (5 GiB) on /var/lib/docker
central  clock        ok      0 s off the local clock
central  memory       ok      11938 MiB, the manifest says 12g
central  env          fail    missing AYE_SMTP_USER; empty AYE_GITHUB_TOKEN
central  image        ok      busybox:1.37.0-musl: on the host, linux/arm64
central  image        ok      grafana/grafana:13.2.3: linux/arm64 in the registry
central  image        fail    ghcr.io/acme/tool:1.4.2: no linux/arm64 manifest, only linux/amd64
central  image        fail    grafana/loki:3.9.9: the registry has no such image or tag
```

| Check | ok, warn, fail |
|---|---|
| `ssh` | fail when the host does not answer (ssh exit 255), the call passes `--timeout`, sudo asks for a password, or the script does not finish; the other rows of that host are then left out |
| `docker`, `compose` | the client version of `docker --version` and `docker compose version --short`; fail when either is missing, and when compose is older than 2.22, which lacks the `pull --policy` of the [pull step](#pull-before-swap) |
| `daemon` | without `--helper`: fail when `docker info` cannot reach the daemon as the user the deploy runs as, or does not answer within 10 seconds (a hung daemon would otherwise hold the call until `--timeout`) |
| `arch` | `uname -m` (`x86_64` is `amd64`, `aarch64` is `arm64`); fail when it is not the manifest `arch` |
| `dir` | the remote directory: ok when it is writable, or absent with a writable parent (the swap creates it); fail otherwise. With `--helper` it must exist, as `aye host-init` creates it |
| `helper` | with `--helper` only: fail when `/usr/local/libexec/ayeops/aye-host` is missing or `sudo -n -l` does not allow its `pull` step on the directory, the newest step, which a host initialised before it lacks |
| `sudo` | with `--sudo` or `--helper` only: the first line of `sudo -V`, ok for classic sudo. For [sudo-rs](host-init.md#sudo-rs) the script also keeps the first line `sudo -n -l` reports as rejected (`<file>:<line>:<col>: <message>`): ok when there is none; when it is in an `/etc/sudoers.d/ayeops-*` rule, fail if the helper is not allowed and warn otherwise, with `run aye host-init again`; warn for a line of another file. Warn when `sudo -V` prints nothing |
| `timers` | only on a host with [backup timers](modules.md#backups): fail when `systemctl` is missing, when `--dir` is not `/opt/ayeops/<host>`, or with `--helper` when `sudo -n -l` does not allow the `timers` step (host-init predates it: run it again) |
| `disk dir`, `disk docker` | free space of the filesystem of the directory and of docker's root dir (`docker info`, else `/var/lib/docker`, marked `assumed`); warn under 15 %, fail under 5 % |
| `clock` | the host's `date +%s` against the local clock before and after the call; warn over 30 seconds off |
| `memory` | `MemTotal` of `/proc/meminfo` against the manifest `memory`, when the host declares one; warn under 90 % of it (the kernel keeps a few percent for itself) |
| `env` | every variable the host's compose file requires (`${NAME:?...}`; the optional `${NAME:-}` ones are not checked) is set and not empty in `<dir>/.env`; fail with the names that are missing or empty, fail when there is no `.env`, warn when the user cannot read it |
| `image` | one row per image the host's compose runs (services with `build:` and references with `${...}` are left out) against `linux/<arch>`, the arch of `uname -m`, else the manifest's: ok when the image is already on the host for that platform or the registry lists it; fail when the image on the host has another platform, the registry's manifest list lacks the platform (the row names the platforms it has), or the registry has no such image or tag; warn when the registry refuses the user, rate-limits, does not answer within 10 seconds or answers without a platform |

The exit code is 1 when any row is `fail`, 0 otherwise, warnings included; 2 for bad arguments,
before any ssh call.

The `.env` check never moves a value. The host is sent the variable names only; the script reads
`.env` with `grep -Eq` alone, which prints nothing, and answers `missing <NAME>` or
`empty <NAME>`. No value is copied, printed or logged on either side, and tests run the script on
a `.env` of known values and fail if any of them appears in its output. `NAME=`, `NAME=""`,
`NAME=''` and `NAME= # comment` count as empty, as they do for compose.

The image check runs `docker` on the host, as the user the script runs as. An image already on
the host is answered by `docker image inspect` (it needs the daemon, so not as a user without
it) and costs no registry request; the deploy's pull skips it too. For any other image
`docker manifest inspect <image>` asks the registry with the docker CLI's credentials of that
user, root with `--sudo`, and, for a single-platform image without a manifest list, once more
with `-v` for its platform. Docker Hub counts a manifest request against the pull rate limit,
so a doctor of a host that has none of its images yet spends about one pull per image, and
anonymous users get few. The registry answer goes back as one line of JSON per image and is
read locally. On a host with many images not yet pulled, raise `--timeout`: each registry call
may take up to 10 seconds. With `--helper` the administrator's login may use other registry
credentials than root, which pulls; a private image may then warn in doctor and still pull.

`--helper` checks the helper, not through it: the helper runs its fixed steps and nothing else,
and the deploy key's forced command refuses a script, which the `ssh` row reports as
`the key runs only the aye-host helper, use a login with a shell via --ssh`. So run doctor on a
helper host with an administrator's own login, for example `--ssh admin@10.8.0.1 --helper`.
The directory `aye host-init` creates is mode 750 for the deploy user, so that login may not
read `.env`, and the `env` row then warns instead of guessing.

## Render into a directory

`aye deploy` renders in memory and needs no directory. A repository that keeps its render in
git, or a pipeline that copies the tree to hosts itself, writes it with `aye render`, into
`rendered` when `-o` is left out:

```sh
aye render ops/ayeops.yaml -o ops/rendered
aye render ops/ayeops.yaml -o ops/rendered --check
```

Next to the hosts' directories the render writes `.aye-render`, the index of the files it
produced: one path per line, relative to the output directory, `/` as the separator on every
system, sorted, each line ending in a newline. It is the format of the `.aye-files` index a
deploy leaves on a host, rooted one level higher. On the next render into the same directory, a
file the previous index lists and the new render does not produce is removed and named as
`removed <path>`; a directory that removal leaves empty is removed too, up to the output
directory. Nothing else is touched: a file of your own in the output directory has no index
entry and stays, a directory that already was empty stays, and a render into a directory
without an index removes nothing, so the first render after an upgrade keeps any stale file and
only the renders after it clean up. An index line that is absolute, contains `.`, `..`, an empty
component, a backslash or a colon, names `.aye-*` at the root, or resolves outside the output
directory is skipped rather than followed.

`--check` renders in memory, writes nothing and compares the result, the index included, with
the output directory byte for byte. It prints one line per path, sorted within each kind:

```text
added edge/backups/backup_run.sh
changed .aye-render
changed edge/compose.yaml
stale edge/old.yaml
ops/ayeops.yaml: ops/rendered differs from the render: 1 added, 2 changed, 1 stale
```

`added` is a file the render would create, `changed` one whose content would change and `stale`
one the render would remove; the last line goes to stderr. The exit code is 0 when the directory
matches (`ops/ayeops.yaml: ops/rendered matches the render`), 1 when it does not or when the
manifest fails the check. The footer stamp is part of the render, so a check of a committed tree
passes the stamp it was rendered with; CI checks `examples/rendered` with
`aye render examples/acme.yaml -o examples/rendered --check --build-time "2026-01-01 00:00 UTC" --commit example`.

## Retired alert rules

Grafana never deletes a provisioned alert rule because its entry left the file: the rule stays,
its query finds nothing, and it fires as NoData for good. Four `aye-*-container-gone` rules of a
service removed from a manifest did exactly that until someone deleted them by hand.

So before the swap `aye deploy` reads `grafana/provisioning/alerting/rules.yaml` from the host,
and every uid the live file has (as a rule or an earlier `deleteRules` entry) and the new render
does not goes into the shipped file as `deleteRules`. The rule file changed, so the config hash
recreates Grafana, which deletes those rules on start. A rule that comes back to the manifest
leaves `deleteRules` on the next deploy. Grafana 13 starts normally with a `deleteRules` entry
for a uid it does not have, which is why earlier entries are carried forward rather than dropped:
a deploy whose `up` failed before Grafana restarted still deletes them next time. The output
names each newly retired rule:

```text
central: alert rule aye-loki-container-gone left the manifest, Grafana deletes it on start
```

If the live file cannot be read or is not YAML, the deploy stops before the swap.

A deploy that copies the rendered tree by other means does the same with

```sh
aye retire-rules live-rules.yaml rendered/central/grafana/provisioning/alerting/rules.yaml
```

which rewrites the rendered file in place and prints `retired <uid>` per rule.

## Why not `tar | ssh tar -x`

Extracting straight into the live directory writes each file in place. Traefik watches
`ingress/dynamic/` and reloaded a half-written `routes.yml` in between (`yaml: line 68: could not
find expected ':'`). A rename inside the watched directory is a single event with the full file.

## Config hash

`up -d` recreates a container only when its compose entry changes, and a changed alert rule or
scrape config is a file, not compose. So every rendered service that bind-mounts rendered files
carries a label `io.ayeops.config-hash`: a hash of the path and content of every rendered file
under its `./` bind mounts. Change Grafana's alert rules and only Grafana's label changes, so
`up -d` recreates Grafana and nothing else. No blanket restarts.

Files the service reads while it runs are left out of the hash, so changing them does not
recreate anything: the Traefik dynamic routes (`live: true` on the module's `files` entry) and
the portal pages that nginx serves from disk. A live file must sit in a bind-mounted directory,
not be mounted on its own: a rename replaces the inode, and a single-file bind mount keeps
pointing at the old one. Files mounted on their own are always in the hash, so a change to them
recreates the container, which mounts the new inode.
