# Releasing

A release is a git tag `v<version>` on `main` plus a GitHub release with the wheel and the sdist.
Users install one with `uv tool install git+https://github.com/AyeOps/ayeops@v<version>` and
pin the range they accept with the manifest key [`ayeops`](manifest.md#pinning-a-release).
Nothing is published to PyPI.

The version lives in one place, `version` in `pyproject.toml`; `aye --version` and the
`ayeops-version` rule read it from the installed package. AyeOps follows semantic versioning,
and while it is `0.x` a minor release (`0.5.0` after `0.4.x`) is the one that may change the
manifest or the rendered output in a way that needs action; a patch release must not.

## Cut a release

1. On a branch, set the new version in `pyproject.toml` and run `uv lock` (the lock records the
   project's version). In [`CHANGELOG.md`](../CHANGELOG.md), move the entries of `Unreleased`
   under a new `## [<version>] - <YYYY-MM-DD>` heading and leave `Unreleased` empty.
2. Move the pin of [`examples/acme.yaml`](../examples/acme.yaml) to the new line when the minor
   version changes, e.g. `ayeops: ">=0.4,<0.5"`; a test fails while the example does not accept
   the running version.
3. Run every command of the "Commands" section of `AGENTS.md`, regenerate `examples/rendered`
   if a module or template changed, and open a pull request. The commit subject is
   `Release AyeOps <version>`.
4. Merge it once CI is green.
5. Run the full mutation test on the merge commit: on the Actions page, `mutants`, `Run
   workflow` on `main`. A pull request only tests the mutants of the functions it changed; this
   run tests every module against its `score` baseline, reusing the cached verdicts of the
   functions that did not change ([mutation tests](../CONTRIBUTING.md#mutation-tests)). When a
   module falls below its baseline, fix it in a pull request and start again from step 4. When
   the `baselines` job raised a score, commit its `mutants-toml` artifact in a later pull
   request; the release does not wait for it.
6. Tag the merge commit on `main` and push the tag:

   ```sh
   git switch main && git pull --ff-only
   git tag -a v0.4.0 -m "AyeOps 0.4.0"
   git push origin v0.4.0
   ```

## What the wheel carries

The build backend is hatchling. The wheel holds the package `src/ayeops` (code, modules,
templates, schemas, portal assets) and the runbook pages of `docs/runbooks/` as
`ayeops/runbooks/`, so the MCP `catalog` source of an installed `aye` serves their text
([MCP](mcp.md#the-catalog-source)). `docs/runbooks/` stays the one place a page is written and
the place the alert links point at; the code takes `ayeops/runbooks` from the package when it is
there and `docs/runbooks/` of the checkout otherwise. The sdist holds `src/ayeops`,
`docs/runbooks`, `pyproject.toml`, the README and the license files, so the wheel `uv build`
makes from it is the one `uv tool install git+...` makes from the tag. `tests/test_packaging.py`
builds both with `uv build --no-sources`, checks the pages in each and renders the example from
the unpacked wheel. An editable install (`uv sync`) also copies the pages into `site-packages`;
nothing reads that copy, since `ayeops` imports from `src/`.

## What the workflow does

Pushing a tag that starts with `v` runs [`release.yml`](../.github/workflows/release.yml), one
job on `ubuntu-latest`, never on a self-hosted runner, whatever `AYE_RUNS_ON` says:

1. fails unless the tag is exactly `v` plus the `pyproject.toml` version, so `v0.3` or a tag on
   a commit that still says `0.2.0` never becomes a release;
2. fails unless the tagged commit is on `main`;
3. fails unless a manual run of the `mutants` workflow passed on the tagged commit (step 5
   above), asked of the GitHub API with `gh api`, which is why the job also holds
   `actions: read`;
4. builds the sdist and the wheel with `uv build --no-sources`, with the uv cache off, so nothing
   an earlier run left behind goes into the build;
5. signs a [SLSA build provenance](https://slsa.dev/provenance/v1) attestation of every file in
   `dist/` with `actions/attest-build-provenance`: the repository, the workflow file, the tag,
   the commit and the runner, signed through Sigstore with the job's OIDC identity and kept by
   GitHub next to the repository;
6. runs `gh release create <tag> dist/* --verify-tag --generate-notes`, so the release notes list
   the pull requests merged since the previous tag.

Only this job holds `contents: write`, the token it needs to create the release, `actions:
read` for the mutants check, and `id-token: write` with `attestations: write`, the two the
attestation needs; the workflow grants everything else `contents: read`. Every checkout drops its git credentials
(`persist-credentials: false`): `gh` publishes with `GH_TOKEN`, not with git. Artifact
attestations work on public repositories on every plan; a private repository needs GitHub
Enterprise Cloud, so tag the first release after the repository is public. When it fails
nothing was published: fix the cause, delete the tag (`git push origin :refs/tags/v0.4.0` and
`git tag -d v0.4.0`) and tag again. A release that went out with a defect is not replaced;
cut the next patch release instead, so an installed `@v<version>` always means one build.

## Verify a release

Download the files of a release and check each against its attestation with the GitHub CLI:

```sh
gh release download v0.4.0 --repo AyeOps/ayeops --dir ayeops-0.4.0
gh attestation verify ayeops-0.4.0/ayeops-0.4.0-py3-none-any.whl   --repo AyeOps/ayeops   --signer-workflow AyeOps/ayeops/.github/workflows/release.yml   --source-ref refs/tags/v0.4.0   --deny-self-hosted-runners
```

The same command checks `ayeops-0.4.0.tar.gz`. It succeeds only when the file's SHA-256 is one
that `release.yml` of this repository attested on a GitHub-hosted runner for that tag; a file
that was rebuilt, swapped on the release page or built from another ref fails. `--format json`
prints the provenance, the commit SHA included. A `uv tool install git+...@v<version>` builds
from the tag instead and is not covered: it trusts the tag, which branch protection and the
`tagged commit is on main` step guard.
