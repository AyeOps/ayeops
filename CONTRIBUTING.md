# Contributing to AyeOps

Thank you for looking. AyeOps is small and has one maintainer, so this page says exactly what a
change needs to be merged, and why each rule is there. [AGENTS.md](AGENTS.md) holds the same
rules in the short form that coding agents read; when the two differ, fix one of them in your
change.

Before a large change (a new module, a new command, a change to the manifest), open an issue
first, so nobody writes a module the roadmap already plans differently. A bug fix, a docs fix or
a test can go straight to a pull request. Report a vulnerability as [SECURITY.md](SECURITY.md)
says, never in a public issue.

By sending a contribution you agree that it is licensed under the
[Apache License 2.0](LICENSE), as section 5 of the licence says; there is no separate agreement
to sign.

## Setup

You need [uv](https://docs.astral.sh/uv/) and git. uv installs Python 3.13 (the version of
`.python-version`) and the locked dependencies:

```sh
git clone https://github.com/AyeOps/ayeops
cd ayeops
uv sync
uv run aye check examples/acme.yaml
```

Docker is only needed for the end-to-end stack and for trying a rendered host; Node.js only for
the status Worker in `cloud/status`.

The kit runs on Python 3.13 or newer (`requires-python` in `pyproject.toml`), the only version
CI tests. ruff and mypy still check the code against Python 3.12 (`target-version`,
`python_version`), because some of it runs on a host's own interpreter rather than in a pinned
image: the probe of `aye discover` goes to `python3 -` over ssh, and the `sentinel` script can
run under systemd without Docker on Python 3.12.

## The checks

Run these before every commit; CI (`.github/workflows/ci.yml`) runs the same and fails on any:

```sh
uv sync
uv run ruff check . && uv run ruff format --check .
uv run mypy
uv run python -m ayeops.comments .
uv run python -m ayeops.texthygiene .
uv run aye check examples/acme.yaml
uv run pytest -n auto
```

`-n auto` spreads the tests over every core with pytest-xdist; leave it out to run one test or
to debug with `pdb`. The text-hygiene check reads the files git tracks, so `git add` a new file
before you run it.

Tests must pass on a loaded machine: several sessions at once, or one small CI runner, can run a
shell script twenty times slower than an idle laptop. So a test never races the wall clock.
Code that sleeps or reads the time takes its clock and its sleep as parameters; a test waits on
an event, a queue or a file instead of sleeping; and every bound on a thread, a local server or
a subprocess that should answer at once is `WAIT` from `tests/conftest.py`, which only a broken
test ever reaches. A test of a timeout shows that the call returned before the abandoned child
would have finished (a file the child writes after its sleep is still missing), not that it
returned within some seconds. A server that refuses a body without reading it is sent the
headers alone: closing a socket with unread data resets the connection, and the client may
lose the answer. `faulthandler_timeout` in `pyproject.toml` prints the stacks of a test that
runs longer than twice `WAIT`.

The status Worker has its own checks, from `cloud/status`:

```sh
npm ci
npm run typecheck
npm test
```

The end-to-end stack (`tests/e2e`, [docs/e2e.md](docs/e2e.md)) needs Docker and runs in
`.github/workflows/e2e.yml`; you do not need to run it locally.

### The rendered example

`examples/rendered` is the committed render of `examples/acme.yaml`, and `tests/test_render.py`
compares it with a fresh render, so a reviewer sees in the diff what a change does to a real
host. After a change to a module or a template, render it again. The render does not delete
stale files, so remove the directory first:

```sh
rm -rf examples/rendered
uv run aye render examples/acme.yaml -o examples/rendered --build-time "2026-01-01 00:00 UTC" --commit example
```

### Mutation tests

Mutation tests cover the modules listed in `tests/mutants.toml`, each with the test files that
import it; `[tool.mutmut]` of `pyproject.toml` names the same modules and tests, and
`tests/test_mutants.py` keeps the two in step. mutmut runs on Linux only (WSL on Windows):

```sh
uv run --group mutants mutmut run
uv run --group mutants mutmut results --all true > mutants/results.txt
uv run python -m ayeops.mutants score mutants/results.txt --write
```

`mutmut run "ayeops.promql.*"` runs the mutants of one module, but still generates the mutants
and collects the test coverage of all of them; `python -m ayeops.mutants narrow <path>`
rewrites `[tool.mutmut]` in place to that module and its tests, as CI does (revert it after).
mutmut names a mutant after the top-level function or method it sits in
(`ayeops.promql.x_parse__mutmut_3`, `ayeops.promql.xǁLexerǁnext__mutmut_1`), so
`mutmut run "ayeops.promql.x_parse__mutmut_*"` runs the mutants of one function.

#### On a pull request: the changed functions

The `mutants` workflow runs on a pull request that changes a listed module, and there it tests
only the functions the pull request changed, the way Infection's `--git-diff-lines` does:

1. `python -m ayeops.mutants select --base origin/main` keeps the modules of
   `tests/mutants.toml` with a changed function that mutmut mutates, one job per module;
2. in each job, `python -m ayeops.mutants functions <path> --base origin/main --output <file>`
   maps the lines of `git diff --unified=0 origin/main...HEAD` to the top-level functions and
   methods around them, keeps the ones mutmut gives mutants to, and writes one pattern per
   function (`ayeops.promql.x_parse__mutmut_*`);
3. `mutmut run --max-children $(nproc) <patterns>` runs only those mutants, on the module's
   tests;
4. `python -m ayeops.mutants gate mutants/results.txt --functions <file>` fails the job when a
   mutant of a changed function survived or no test reached it, or when a changed function's
   mutants did not run. Killed, timed-out and type-check mutants pass, as in the score; the
   suspicious and skipped ones are left out. The job summary names each survivor for
   `mutmut show`.

How a line maps to a function:

- a line inside a function, its decorators or a function nested in it maps to the top-level
  function, a line inside a method to the method (`xǁClassǁmethod`), as mutmut groups mutants;
- removed lines map through the source at the merge base, so a function that lost lines
  counts as changed; a deleted function is gone and runs nothing, a renamed one runs under its
  new name;
- a changed statement outside any function (a constant, an import) maps to the functions that
  use a name it binds; a changed class attribute to the methods of that class that use it;
- functions mutmut never mutates are left out: those with a decorator other than one
  `@staticmethod` or `@classmethod`, methods of nested classes, `__new__`, `__getattribute__`
  and `__setattr__`, and (from the mutants mutmut generates for the file) functions with
  nothing to mutate.

A pull request that changes only tests, `tests/mutants.toml`, `[tool.mutmut]` or the workflow
runs no mutant: the tests of a module only matter against its mutants, and the next full run
measures them. The per-module `score` is not checked on a pull request, since a partial run
cannot measure it. To check a change to the mutation setup itself, run the workflow by hand on
the branch (`Run workflow` with the branch selected), which is a full run.

#### Full run: once per release

A manual dispatch (`Run workflow` on the Actions page) runs every module in full, and it is the
run that checks the baselines: a module job fails when its score, killed and timed-out mutants
over all checked ones floored to 0.1, falls below its `score` in `tests/mutants.toml`; a module
without a `score` only prints its score. Each job reports its score with `aye report mutants`
(suite = module path), and the `baselines` job runs `score --write` over the results of every
module: when a score went up, the summary shows the diff and the raised `tests/mutants.toml` is
the `mutants-toml` artifact of the run, to commit as it is. `score --write` after a local run
does the same; it never lowers a baseline. [Releasing](docs/releasing.md) runs the full run on
the commit to tag, and the release workflow refuses a tag without one that passed.

A full run reuses the verdicts of the previous one. mutmut keeps a hash of every function it
mutates in `mutants/<path>.meta` and, on the next run, tests again only the mutants of functions
whose hash changed and those not tested yet. The workflow keeps those `.meta` files with
`actions/cache` (not the copied sources, which go stale, nor `mutmut-stats.json`: the test
mapping is collected again, so a new function finds its tests). The key is
`python -m ayeops.mutants cache-key <path>`: a prefix hashed from the module's test files,
`tests/conftest.py`, `tests/fixtures/`, the `[tool.mutmut]` settings, the `mutants` group, the
project's dependencies and `uv.lock` (without the project's own version, so a release bump
keeps the cache), then the hash of the module's source. A run restores the newest cache of the
same source, else of the same prefix; a change to a module's tests or the settings starts it
from scratch. The cache is saved even when the job fails or hits its timeout, so a long module
goes on where it stopped. One thing the key does not see: a change in another module that
changes what this module's tests detect keeps the old verdicts of the functions that did not
change. Tick `fresh` when dispatching to test every mutant again.

A new Python module goes into both `tests/mutants.toml` and `[tool.mutmut]`. A script under
`modules/*/files/` must be loaded by its tests under its dotted name
(`ayeops.modules.<name>.files.<script>`, with `importlib.util.spec_from_file_location`), or
mutmut cannot switch its mutants on.

### What else CI runs

CI also scans the whole git history for secrets: the `secrets` job of `ci.yml` runs gitleaks,
the release binary checked against its pinned SHA-256, so the runner needs no Docker access.
The `compose` job runs `docker compose config` on every rendered host and checks that every
image of `examples/rendered` is published for `linux/amd64` and `linux/arm64`. The
`image-tags` workflow runs `ci/image_digest.py --check`: on a pull request or a push to `main`
over the image lines that change adds, so a new pin must match its tag, and every Monday (or
by hand) over every module template and rendered compose, so a tag pushed again shows up as a
failed scheduled run that names the image, the pinned digest and the one the tag points to now.
Find out why the tag moved, then repin with `ci/image_digest.py --write`. A pull request is
never blocked by a pin it does not touch, because official images move their tags on every
base-image rebuild.

Every job of a pull request runs on `ubuntu-latest`, whatever the repository variables say.
Only a push to `main`, a schedule or a manual run of `ci`, `mutants` and `image-tags` follows
the runner labels of the repository variable `AYE_RUNS_ON` (`ubuntu-latest` when it is not
set); `e2e` leaves GitHub only when the owner starts it by hand with `runner: self-hosted`, and
a release always builds on GitHub with a signed build provenance. The `zizmor` job of `ci.yml`
audits the workflows themselves and fails on a high finding. `tests/test_workflows.py`
evaluates every `runs-on` for every trigger with the variable set and fails when an event a
stranger can start could reach a self-hosted label. See
[where e2e runs](docs/e2e.md#where-it-runs) and [releasing](docs/releasing.md).

## Rules of this repository

### No comments in code

Code, configuration, templates, scripts and stylesheets carry no comments, and
`python -m ayeops.comments .` fails on one. The exceptions are a shebang, `# syntax=` in a
Dockerfile and the version comment after a SHA-pinned action (`uses: org/action@<sha> # v1.2.3`),
which tools read.

Why: nothing checks a comment, so it drifts away from the code it describes and then misleads.
An explanation that matters goes where it is kept true: a test whose name states the behaviour
fails when the behaviour changes, and a page of `docs/` is where an operator looks. Templates
matter twice, because what they contain is copied into every rendered host; a stale remark in a
template becomes a stale remark on every server. The same holds for issue forms and the pull
request template, which carry no `#` comments and no `<!-- -->`.

### Text hygiene

No invisible, bidi, control or private-use characters, and no word that mixes Latin and
Cyrillic letters: they make text that reads one way and runs another, and a reviewer cannot see
them in a diff. In shell, YAML, TOML, INI, `.env`, Alloy, Dockerfiles and ignore files, quotes,
dashes and ellipses are plain ASCII too, because a typographic quote there is a syntax error or
a different value. Prose in Markdown may use them. `python -m ayeops.texthygiene .` enforces it;
[docs/rules.md](docs/rules.md#the-text-hygiene-rule) has the details.

### A new service ships complete

Every service, of your manifest or of a built-in module, has a description, a metric, an alert
with a severity and a runbook, a log source and a status-page mark. That is the product:
`aye check` refuses a service without them, and tests hold the built-in modules to the same
[catalog rules](docs/rules.md). A service nobody can describe, measure or be woken up by fails
silently, and silence looks exactly like success.

### A runbook per alert

Every alert has a page at `docs/runbooks/<alert>.md`, named after the alert; tests fail without
it. Alert messages link there, so the page is what the person on call reads at night: what the
alert means, what to look at first and what to do, in that order.

### Images pinned and multi-arch

Every image is pinned to an exact version, never `latest` or a floating major tag, plus the
digest of its multi-arch index (`name:tag@sha256:<digest>`), and is published for
`linux/amd64` and `linux/arm64`. A pinned version makes a render reproducible and a deploy diff
honest; the second architecture is there because a small fleet often has an ARM VM or a
Raspberry Pi in it. The `compose` job of CI checks both architectures. Pick the newest stable
release that is at least two weeks old.

The digest is there because a tag is only a name a registry lets its owner move: in March 2026
the tags of `aquasec/trivy` were pushed again with malicious images (CVE-2026-33634), and a
host that pulls by tag alone takes whatever the tag points to that day. With the digest, docker
pulls exactly the image that was reviewed, and one index digest covers both architectures.

Get the pin with the read-only script, which asks the registry for the index digest of the tag
(`HEAD /v2/<repository>/manifests/<tag>` with an anonymous token; Docker Hub does not count a
HEAD against its pull limit) and refuses a single-platform manifest:

```sh
uv run python -I ci/image_digest.py grafana/grafana:13.2.3
uv run python -I ci/image_digest.py --write --file src/ayeops/modules/grafana/compose.yaml.j2
uv run python -I ci/image_digest.py --check --file examples/rendered/central/compose.yaml
```

The first prints `name:tag@sha256:...`, `--write` repins every `image:` line of the files from
their tags, and `--check` fails when an image has no digest or its tag now points to another one.
Renovate's image manager reads the tag and the digest together, so a version bump moves both,
and a new digest under an unchanged tag comes in the weekly container images pull request.
An image branch also renders `examples/rendered` again (`postUpgradeTasks` in `renovate.json`),
because the render lists every image in more places than the `image:` lines; the host that
runs Renovate must allow that command ([updates](docs/modules.md#post-upgrade-commands)), or the
render is left to whoever takes the pull request. Tests compare rendered images with the pin of
the template (`pinned` in `tests/conftest.py`) rather than naming a tag, so a bump needs no test
change; the versions named in `docs/modules.md` are still updated by hand.
Official images (`postgres`, `python`, `busybox`, `traefik`)
are rebuilt under their tag when their base image gets a fix, so a moved tag is usually that;
check the image's release notes or build history before you take the new digest.

### Ports on 127.0.0.1

A rendered port binds to `127.0.0.1` unless the manifest exposes it on an explicit address.
Nothing becomes reachable from the network because a module was enabled; exposing a port is a
decision written in the manifest.

### Module scripts are one file, standard library only

A Python script in a module's `files/` is one self-contained file that imports only the
standard library, and never imports `ayeops`. It is mounted into a container that runs it as
`python -I /app/<script>.py` on a pinned `python:*-alpine` image. That keeps the whole program
in one file a reviewer reads at once, needs no image of our own to build, publish for two
architectures and scan, and leaves no dependency to install or to update on the host; `-I`
keeps the host's environment and the working directory out of the import path. The render
copies the file and the [config hash](docs/deploy.md#config-hash) covers it, so a changed
script recreates exactly its container. A script that may also run without Docker (the
`sentinel`) needs nothing but the interpreter.

### Docs in the same change

A change in behaviour changes its documentation in the same commit: README.md,
[docs/modules.md](docs/modules.md), the runbooks, and [CHANGELOG.md](CHANGELOG.md) under
`Unreleased`. A reviewer checks the docs against the code in one diff, and `main` never
describes a kit that does not exist.

### Commits

The subject is one imperative English sentence without a trailing period, for example
`Add an oncall module: GoAlert with a Telegram bridge for critical alerts`. The body says why,
in short wrapped prose, with measured numbers when there are any (memory, run time, a score).
Then `Closes #N` for an issue. A change an agent wrote ends with its `Co-Authored-By:` trailer.

## Adding a module

A module is data under `src/ayeops/modules/<name>/`; [docs/modules.md](docs/modules.md) has the
layout and [Write your own module](docs/modules.md#write-your-own-module) a minimal example. A
module of your own can live next to your manifest through `module_paths` and never needs to come
here; a built-in one is held to these steps:

1. `module.yaml`: summary, `requires`, defaults and a `settings_schema` that refuses unknown
   keys, the catalog entry of every service it runs, its alerts, dashboards and files. It must
   match [`module.schema.json`](src/ayeops/schema/module.schema.json).
2. `compose.yaml.j2` and `scrape.yaml.j2`. The renderer adds memory and CPU limits, platform,
   restart policy, labels, log rotation and the `127.0.0.1` port binding; do not repeat them.
3. Every service passes the catalog rules: a description, metrics, alerts, `logs` and a
   `status` mark. Every alert has a severity and a page in `docs/runbooks/<alert>.md`.
4. Every `${AYE_*}` variable the templates read is declared in `permissions` (a credential
   someone issues, with the narrowest grant) or `generated` (with its `format`), and listed in
   `.env.example`; a test checks the declarations.
5. A script in `files/` follows the module-script rule above, has its own tests that load it
   by its dotted name, and goes into `tests/mutants.toml` and `[tool.mutmut]`.
6. Tests in `tests/test_<name>_module.py`: what it renders for a small manifest, and what its
   script does with captured inputs from `tests/fixtures/`.
7. A section in [docs/modules.md](docs/modules.md) (what it runs, the image and version,
   settings, metrics, alerts), a row in the module table of README.md, and an entry in
   CHANGELOG.md, marked preview until it has run on a real fleet.
8. If `examples/acme.yaml` enables it, regenerate `examples/rendered`.

## Pull requests

Keep one change per pull request, with the checks above green. The template asks what changed
and why, how you tested it, and which docs you updated. The maintainer aims to answer within a
week, but reviews in spare time; a pull request that needs work gets a comment saying what, not
a silent close.
