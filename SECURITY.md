# Security policy

AyeOps renders the monitoring, ingress, sign-in and secrets of a fleet, so a flaw in it can
open a host. Please report one privately, give us time to fix it, and we will credit you.

## Report a vulnerability

Use GitHub's private vulnerability reporting: on the
[repository page](https://github.com/AyeOps/ayeops), open the **Security** tab and press
**Report a vulnerability**
([direct link](https://github.com/AyeOps/ayeops/security/advisories/new)). Only you and the
maintainers see the report, and the fix can be prepared in a private fork of the advisory.

Do not open a public issue, pull request or discussion for a vulnerability, and do not put one
in a commit message.

A useful report says:

- the version (`aye --version`, or the commit of `main`) and where the flaw is: a command, a
  module, a rendered file, the status Worker;
- the smallest manifest or request that shows it, and what happens against what should happen;
- what an attacker needs first (network access, a deploy key, a shell on the host, nothing) and
  what they gain.

Never send a real secret, token, key or `.env` file, yours or anyone else's. Replace values with
`EXAMPLE`; the report is about the flaw, not your data.

## What to expect

AyeOps has one maintainer, who answers in spare time. These are targets, not guarantees:

| Step | Target |
|---|---|
| acknowledgement of the report | within 7 days |
| first assessment: confirmed or not, severity, plan | within 14 days |
| fix released for a confirmed high or critical issue | within 30 days |
| fix released for other confirmed issues | within 90 days, or with the next release |

You hear back at each step, including when a target slips and why. When a fix is released, we
publish a GitHub security advisory with a CVE where one applies, and name you in it unless you
ask not to be named. Please keep the details private until the advisory is out or 90 days have
passed since your report, whichever comes first; if we need longer, we will ask and say why.
There is no bug bounty.

## Supported versions

| Version | Supported |
|---|---|
| `0.4.x` | yes, until 1.0 |
| `main` | yes, fixes land here first |
| anything older | no |

While AyeOps is `0.x`, a fix ships as a patch release of the current minor line
(see [releasing](docs/releasing.md)), and the advisory names the first fixed version.
After 1.0 this page will say how long each line receives fixes.

## Scope

In scope:

- the `aye` command and its subcommands (`check`, `render`, `deploy`, `diff`, `doctor`,
  `host-init`, `permissions`, `dns`, `secrets`, `discover`, `report`, `portal`, `status`);
- the built-in modules under `src/ayeops/modules/` and the scripts they mount into containers;
- the rendered configuration as `aye render` writes it: compose files, scrape and alert rules,
  Grafana provisioning, Traefik and oauth2-proxy settings, the deploy helper and the sudoers
  rule of `aye host-init`. Examples: a port reachable beyond `127.0.0.1` without being exposed
  in the manifest, a secret written somewhere it can be read, a manifest value that injects into
  a template, a route that skips the sign-in, the deploy helper writing outside its directory;
- the status page Worker in `cloud/status`: the signed push, tenant isolation, the admin API
  and the public page;
- the CI helpers in `ci/` (`aye report`, the GitHub action and the GitLab include) and the
  workflows of this repository.

Out of scope:

- vulnerabilities in the software AyeOps runs (Grafana, VictoriaMetrics, Loki, Traefik,
  Infisical, GoAlert, Trivy and the rest): report them to their projects. If AyeOps pins an
  affected version and a fixed release exists, a normal issue asking for the update is welcome;
- limits the docs already state, such as a deploy key being root-equivalent on its host
  ([host init](docs/host-init.md#what-this-buys-and-what-it-does-not)); a way around a stated
  guarantee is in scope;
- a manifest that deliberately exposes a port, turns off the sign-in or allows a writing tool,
  and the hosts, networks and accounts you run AyeOps on;
- reports from automated scanners without a demonstrated impact, missing hardening headers
  without an attack, denial of service by volume, and social engineering of the maintainers;
- the websites of AyeOps and trofey.app.

Please test only against systems you own. Do not run tests against other people's status pages
or fleets.
