# Host init

`aye deploy --sudo` runs `sudo sh -c '<script>'` on the host, so the ssh user needs sudo for any
command. `aye host-init` replaces that with a deploy user that may run one fixed helper on one
directory and nothing else.

```sh
aye host-init ops/ayeops.yaml --host central --ssh-key ~/.ssh/aye-deploy.pub --from 10.8.0.0/24 > init.sh
less init.sh
scp init.sh root@central: && ssh root@central sh init.sh
aye deploy ops/ayeops.yaml --host central --ssh aye@10.8.0.1 --helper
```

`aye host-init` only prints a POSIX `sh` script. Read it, then run it once as root on the host.
Running it again changes nothing that is already right; every line it prints starts with
`ok:`, `changed:`, `skipped:` or `warning:`, and the last two lines are the number of changes and
the matching `aye deploy` command.

| Option | Default |
|---|---|
| `--host <name>` | a host of the manifest; may be left out when the manifest has one host |
| `--user <name>` | `aye`; a system user name, `[a-z_][a-z0-9_-]{0,31}` |
| `--ssh-key <file>` | none; one OpenSSH public key line without options. Without it the key file is left as it is |
| `--from <cidr>` | none; an address or network the key may connect from, repeatable |

## What the script does

| Step | Exactly |
|---|---|
| user | `useradd --system --user-group --shell /bin/sh --home-dir /var/lib/ayeops/<user> --no-create-home` (busybox `adduser -S` on Alpine). It refuses an existing user with a uid of 1000 or more, another home or a `nologin` shell |
| password | `*` in `/etc/shadow` through `chpasswd -e`: no password matches, and unlike `!` it does not lock the account for an sshd without PAM |
| key | `/var/lib/ayeops/<user>/.ssh/authorized_keys`, owned by root, mode 644, so the user cannot edit its own key options: `restrict,command="/usr/local/libexec/ayeops/aye-host ssh",from="<cidr>" <key>` |
| directory | `/opt/ayeops` root 755, `/opt/ayeops/<host>` owned by the user, mode 750 |
| helper | `/usr/local/libexec/ayeops/aye-host` (755), `pull.sh`, `swap.sh`, `diff.sh` and `timers.sh` (644), all owned by root, copied byte for byte from `src/ayeops/hostside/` and embedded in the script for review |
| sudoers | `/etc/sudoers.d/ayeops-<user>-<host>`, root 440, checked with `visudo -cf` before it is moved into place; a rule `visudo` rejects is deleted and the script stops. On [sudo-rs](#sudo-rs) it has no `Defaults` line |
| module directories | what the manifest's modules declare in `host_directories`, see below |

The sudoers rule lists seven complete command lines and no wildcard:

```text
Defaults:aye !requiretty
aye ALL=(root) NOPASSWD: /usr/local/libexec/ayeops/aye-host /opt/ayeops/central rules, \
    /usr/local/libexec/ayeops/aye-host /opt/ayeops/central pull, \
    /usr/local/libexec/ayeops/aye-host /opt/ayeops/central swap, \
    /usr/local/libexec/ayeops/aye-host /opt/ayeops/central up, \
    /usr/local/libexec/ayeops/aye-host /opt/ayeops/central ps, \
    /usr/local/libexec/ayeops/aye-host /opt/ayeops/central diff, \
    /usr/local/libexec/ayeops/aye-host /opt/ayeops/central timers
```

Several hosts of a manifest on one machine get one rule file each.

### sudo-rs

Ubuntu 25.10 and newer ship [sudo-rs](https://github.com/trifectatechfoundation/sudo-rs) as
`sudo` and `visudo`. It reads a subset of sudoers and refuses the rest with a parse error: a
`Defaults` option outside its short list (`requiretty` is not on it, so
`Defaults:aye !requiretty` is `unknown setting: 'requiretty'`), a wildcard in a command argument
other than a lone `*` as the last one (an error since 0.2.13; older versions ignored such a
pattern), `CHROOT`, `TIMEOUT`, `INTERCEPT`, `NOTBEFORE`, `NOTAFTER`, SELinux `ROLE`/`TYPE` and
netgroups. Its `visudo -cf` fails on any such line, and `sudo` itself prints it and drops it.

The script asks `sudo -V`, which sudo-rs answers with `sudo-rs <version>`. There it leaves the
`Defaults` line out and prints `skipped: Defaults:aye !requiretty, sudo-rs has no requiretty`;
sudo-rs never needs a terminal for `sudo -n`. The command lines are the same for both, exact
arguments without a wildcard, which both accept. A rule written by hand on such a host can be
replaced by running the script: it writes the file only when it differs. If sudo is switched
between the two later, run the script again; [`aye doctor`](deploy.md#doctor) names the
implementation in its `sudo` row and fails or warns when sudo-rs rejects a line of an
`ayeops-*` rule.

A host initialised before a step existed lacks its line and its script; `aye deploy --helper`
then stops at that step and says so. For the `pull` step that is before the swap, so nothing
changes on the host. Run the host-init script of that host again: it rewrites the helper, adds
`pull.sh` and replaces the sudoers rule, and reports every file it changed.

### The helper

`aye-host <dir> <step>` refuses (exit 64) any directory that is not a single, existing, non-link
entry right under `/opt/ayeops`, whose name does not start with a dot, and any step other than:

| Step | Runs in `<dir>` |
|---|---|
| `rules` | prints `grafana/provisioning/alerting/rules.yaml` when it exists |
| `pull` | `sh /usr/local/libexec/ayeops/pull.sh <dir>` with the new `compose.yaml` on stdin: writes it to `<dir>/.aye-staging/compose.yaml`, runs `docker compose --project-directory <dir> -f <dir>/.aye-staging/compose.yaml pull --quiet --policy missing --ignore-buildable` and removes the staged copy; no flag or service comes from the caller, see [pull before swap](deploy.md#pull-before-swap) |
| `swap` | `sh /usr/local/libexec/ayeops/swap.sh <dir>` with the tarball on stdin: the same swap as `--sudo`, see [deploy](deploy.md) |
| `up` | `docker compose up -d --remove-orphans` |
| `ps` | `docker compose ps --all --format json` |
| `diff` | `sh /usr/local/libexec/ayeops/diff.sh <dir>` with the list of files on stdin: the read-only comparison of [`aye diff`](deploy.md#diff), the same script `--sudo` ships as text |
| `timers` | `sh /usr/local/libexec/ayeops/timers.sh <dir>`: installs the [backup timers](modules.md#backups) of `<dir>/backups/systemd/` into `/etc/systemd/system` and enables them, see [Backup timers](#backup-timers) |

It sets its own `PATH`; sudo resets the rest of the environment. `aye-host ssh` is the forced
command of the key: it takes `SSH_ORIGINAL_COMMAND`, accepts only the exact form
`sudo -n /usr/local/libexec/ayeops/aye-host <dir> <step>` made of letters, digits, `._/-` and
spaces, and runs it without a shell. Anything else, an interactive login, `scp` or a second
command after `;`, ends with `this key runs ... only`.

`aye deploy --helper` sends exactly that form, so it works with this key and also with an
administrator's own login that has the same sudo rule.

### Backup timers

The `timers` step is the only one that writes outside `/opt/ayeops`. It runs only when the
render of the host holds backup units, the swap just removed some, or the swap finds
`ayeops-<name>-backup-*` units still in `/etc/systemd/system` (it prints `installed <unit>` for
each), so a host that never had a `backups` job with a `command` never calls it, and a deploy
after a failed timers step runs it again until the old units are gone. With `<name>` the last
part of `<dir>`, it:

1. refuses (exit 1, nothing changed) when `systemctl` is missing, `backups/systemd` is a link,
   or any entry in it is not a regular file named `ayeops-<name>-backup-*.service` or
   `ayeops-<name>-backup-*.timer` of letters, digits and `._-`;
2. copies each unit whose content differs to `/etc/systemd/system` (root, 644) through a
   dot-file renamed into place, and refuses a unit there that is a link;
3. for every `ayeops-<name>-backup-*` unit in `/etc/systemd/system` that the render no longer
   has: `systemctl disable --now` for a timer, then deletes the file;
4. `systemctl daemon-reload` when it copied or deleted anything, then `systemctl enable` and
   `start` (`restart` when it changed) for each timer.

It prints `changed <unit>` and `removed <unit>` like the swap and touches no other unit: not
those of another host, not `ssh.service`. What it cannot limit is what a unit runs: the service
of a backup job runs a command as root, by design, so a deploy key that ships a render can run
a command as root through it, as it already can through a privileged compose service (see
below). The command is in the manifest, in the rendered `backups/jobs/<job>.sh`, in `aye diff`
and in `systemctl cat`.

A host initialised before backup timers existed has neither the `timers` line in its sudoers
rule nor `timers.sh`. A deploy of a job with a `command` then stops after `docker compose up`
with `backup timers not installed` and a hint, and `aye doctor --helper` fails its `timers` row;
running the host-init script of that host again installs both, as it did for the `diff` step.
The `swap.sh` of a host initialised before the swap named installed units does not name them,
so there a failed removal of the last timer is repaired only after the host-init script runs
again.

A host initialised before [.env drift](deploy.md#env-drift) keeps the older `swap.sh` and
`diff.sh`: `aye diff --helper` says `.env not compared` until the host-init script runs again.
That adds no step and leaves the sudoers rule as it is; only the two scripts change.

### Module directories

A module lists the host directories it needs in `module.yaml`:

```yaml
host_directories:
  - setting: textfile_dir
    owner: "0:0"
    mode: "0755"
    for: the textfile collectors, which write as root in their containers, and node-exporter, which reads
```

When the module runs on the host and the setting holds a path (and `when`, a Jinja expression
over `settings` and `modules`, holds), the script creates the directory with that owner and
mode. A directory that already exists with another owner or mode is reported as a warning and
left alone: it may hold data, and chowning it is a decision for a person. Today the node module
declares `textfile_dir`, the objectstore `data_dir` and, for Garage, `meta_dir`.

## What this buys, and what it does not

A user that can run `docker compose` on a compose file it controls is root-equivalent: a
service with `privileged: true` or a bind mount of `/` is a root shell. The deploy user ships the
compose file and then runs `up`, so **whoever holds the deploy key can still become root on that
host**. host-init does not change that, and no sudo rule can while deploys ship compose files.

What it does buy:

- A stolen deploy key gives no shell. sshd runs only the helper; no interactive session, no pty,
  no port, agent or X11 forwarding, no `scp` or `sftp`, and with `--from` no use from another
  address.
- The key reaches one directory. It cannot run compose in another project, read other files
  through `rules`, or point the helper at a path outside `/opt/ayeops/<host>`; outside it, the
  `timers` step writes only the `ayeops-<host>-backup-*` units of that host. `diff` sends back
  only files the previous deploy listed in `.aye-files`, never `.env` or `.env.*` other than
  `.env.example`, and never follows a symbolic link. Of `.env` it sends back key names and, per
  key, whether the value changed since the last deploy, compared on the host with the
  `.aye-env` hashes the swap wrote (root, 600); never a value or a hash, and not at all when
  `.env` is a link (see [.env drift](deploy.md#env-drift)).
  `pull` writes only `<dir>/.aye-staging/compose.yaml`, which it deletes again, and pulls the
  images that file names, which `swap` and `up` could already make the host do; it starts no
  container.
- Becoming root takes a deliberate, visible step: a compose file with a privileged service,
  which `docker compose up` starts and `docker ps`, the logs and the next honest deploy show.
- The swap refuses archives with links or special files and index entries that are absolute,
  contain `.` or `..` components or name the deploy's own files, so the swap itself cannot be
  turned into a write or a delete outside the directory.
- The user has no password, no sudo for anything else and is not in the `docker` group.

What it does not buy: protection against a holder of the key who wants root, a review of the
compose file, or any limit on what the containers do once they run. Keep the private key where
your CI or laptop keeps its other deploy secrets, use `--from`, and rotate it by running
host-init again with a new `--ssh-key`.

## aye discover

`aye discover` runs its probe as `python3 -` over ssh, which the deploy key cannot do, and
host-init grants it nothing. The probe runs best-effort without root and says in `notes` what it
missed. These are the probes that need more than a plain user:

| Probe | Command | Without root |
|---|---|---|
| `storage.disks[].smart` | `smartctl -i -H -j /dev/<disk>` | only whether `smartctl` exists and which devices `smartctl --scan` lists |
| `listening_tcp` | `ss -ltnpH` | ports of every user, process names of your own processes only |
| `containers` | `docker version`, `docker compose ls --all`, `docker compose -p <project> ps --all`, `docker ps --all` | needs the docker socket: root or the `docker` group |
| `top_processes`, `apps` | `/proc/<pid>/status`, `comm` and `cgroup` | complete, unless `/proc` is mounted with `hidepid` |

Everything else reads world-readable files under `/proc`, `/sys` and `/etc`. Granting those
commands through sudo was left out on purpose: discover runs before a host is in the manifest,
so before host-init; `--sudo` would need `sudo python3`, which is root; the `docker` group is
root-equivalent too; and on an initialised host the `smart`, `containers` and `node` modules
already report what these probes would add. Run discover once with an administrator's own login
and `--sudo` when you want the full report, or without it for the rest.

## aye permissions

`aye permissions <manifest>` prints, per host and per module, the external credentials the
manifest needs and the narrowest grant that works for each: Cloudflare token permissions on the
zones and accounts in use, GitHub token permissions on the organizations and repositories
listed, an S3 key for one bucket and prefix, OAuth clients with their redirect URIs, the database
login of a Grafana datasource, the machine identity of each host that pulls secrets. It ends
each host with the variables that need no external grant: those you generate, each with its
format, bytes and length, and those you copy from another AyeOps host, each with its source.
`aye secrets gen --manifest <file> --host <host>` stores the missing generated ones of a host in
its folder, in those formats, and never overwrites one; with `--env-file <path>` it writes them
to a local `.env` instead, for a host without a secrets manager
([aye secrets](modules.md#aye-secrets)). An excerpt, without the generated keys of Grafana and
the other modules of `central`:

```text
central
  host access: aye host-init ops/ayeops.yaml --host central, a deploy user that may run the aye-host helper on its directory and nothing else
  cloudflare
    Cloudflare, custom API token, read only: AYE_CLOUDFLARE_ANALYTICS_TOKEN
      - Zone > Zone > Read and Zone > Analytics > Read on acme.example
      - Account > Account Analytics > Read on account 0123456789abcdef0123456789abcdef
      - no edit permission and no other zone or account
  ingress
    Cloudflare, custom API token: AYE_CLOUDFLARE_DNS_API_TOKEN
      - Zone > Zone > Read and Zone > DNS > Edit on the zone that holds ops.acme.example only
  generated by you, no external grant; aye secrets gen --manifest ops/ayeops.yaml --host central stores the missing ones:
    AYE_AUTH_COOKIE_SECRET: base64url, 32 bytes, 43 characters
    AYE_OBJECTSTORE_ACCESS_KEY_ID: garage-key-id, GK and 24 hex characters
    AYE_OBJECTSTORE_RPC_SECRET: hex, 32 bytes, 64 characters

edge
  host access: aye host-init ops/ayeops.yaml --host edge, a deploy user that may run the aye-host helper on its directory and nothing else
  generated by you, no external grant; aye secrets gen --manifest ops/ayeops.yaml --host edge stores the missing ones:
    AYE_OFFSITE_RESTIC_PASSWORD: base64, 33 bytes, 44 characters
  copied from another AyeOps host, no external grant:
    AYE_OFFSITE_ACCESS_KEY_ID: copy of AYE_OBJECTSTORE_ACCESS_KEY_ID on central, when it runs Garage
    AYE_OFFSITE_SECRET_ACCESS_KEY: copy of AYE_OBJECTSTORE_SECRET_ACCESS_KEY on central, when it runs Garage
    AYE_OFFSITE_REST_PASSWORD: copy of AYE_RESTSTORE_EDGE_PASSWORD on central, when it runs rest-server
```

The output comes from the `permissions` and `generated` sections of each module's
`module.yaml`, rendered with the module's settings, see
[write your own module](modules.md#write-your-own-module). A test fails when a module template
reads an `AYE_*` variable its `module.yaml` does not declare, and when `.env.example` lists one
nobody declares.
