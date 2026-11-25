# Delivery and quality metrics

`aye report` turns what CI and deploy scripts already produce (a step's duration and result,
JUnit XML, a coverage report, a mutation report, a merge) into a few samples in VictoriaMetrics
and one log line in Loki. The [delivery](modules.md#delivery) module turns the samples into
30-day medians, two dashboards and six alerts; the [github](modules.md#github) module adds the
Actions minutes and imports reports from runners outside the private network.

## What is measured

| Command | Series | Labels besides the context |
|---|---|---|
| `step`, `run` | `aye_step_duration_seconds`, `aye_step_success`, `aye_step_finished_timestamp_seconds` | `step`, `result` on the duration |
| `junit` | `aye_tests{outcome}`, `aye_tests_duration_seconds`, `aye_tests_flaky`, `aye_test_slowest_seconds{test,rank}`, `aye_test_flaky{test}` | `suite` |
| `coverage` | `aye_coverage_{lines,branches}_percent`, `..._covered`, `..._valid` | `format`, `suite` |
| `mutants` | `aye_mutants{status}`, `aye_mutation_score_percent`, `aye_mutants_survived{module}` | `tool`, `suite` |
| `pr` | `aye_pr_lead_time_seconds`, `aye_pr_commits` | `start`, `branch` is the base |

Every sample carries the context `project`, `repo`, `branch`, `pipeline` and `job`, read from
the GitHub Actions or GitLab CI variables or from `AYE_PROJECT`, `AYE_REPO`, `AYE_BRANCH`,
`AYE_PIPELINE`, `AYE_JOB`. Run ids, attempts, commits and links go only on
`aye_report_info{report,name}`: a label that changes on every run would make every run its own
series and turn a 30-day median into a median of one sample.

The formats it reads:

| Report | Formats |
|---|---|
| tests | JUnit XML of pytest, Maven Surefire, Jest, Go and others; several files are one run, and a test that failed and then passed, or has `rerunFailure`, is flaky |
| coverage | Cobertura XML (`coverage xml`), coverage.py JSON (`coverage json`), lcov, JaCoCo, Clover, Istanbul `json-summary` |
| mutation | mutmut 3 `mutmut results --all true` and `mutants/mutmut-cicd-stats.json`, the progress output of `mutmut run` (2 and 3), mutmut 2 `junitxml`, Stryker `mutation.json`, Infection JSON, `summary.log` and text log |

The mutation score is killed plus timed-out mutants (and, for mutmut 3, those caught by the type
check) over everything that ran except the ignored ones. `mutmut results` without `--all true`
hides the killed mutants; `aye report` rejects it instead of reporting a score of zero. Modules
with surviving mutants come from the mutant names of mutmut 3, the file names of Stryker and
the `file` of mutmut 2 `junitxml`; the 50 with the most survivors are kept.

Coverage is the line coverage. coverage.py's `percent_covered` mixes lines and branches when
branch coverage is on; `aye report` reports the two apart.

## Where the samples go

`aye report` is one stdlib Python file that runs on Python 3.10 and later;
[`ci/aye-report.py`](../ci/aye-report.py) is the same file for CI images without the kit.

| Runner | Path |
|---|---|
| on the private network (self-hosted runner, deploy script on a host) | `AYE_METRICS_URL=http://<metrics host address>:8428` pushes to `/api/v1/import/prometheus`; `AYE_LOGS_URL=http://<logs host address>:3100` pushes the log line to `/loki/api/v1/push` |
| GitHub-hosted runner | no URL; the [action](../ci/github/action.yml) writes the samples to a file and uploads it as an `aye-report-*` artifact kept three days; the `github` exporter on the central host lists the artifacts of the repositories in `github.reports` every poll and imports each one once |
| anywhere else | `--out <file>` (or `AYE_REPORT_FILE`) appends the samples to a file to carry over by other means, e.g. `curl --data-binary @file` from a machine that can reach storage |

VictoriaMetrics and Loki have no authentication of their own, so the private network is the
allowlist: both ports are exposed only on the `public_bind` address of the host, which is on
the private network. A URL with `user:password@` sends basic auth and `AYE_METRICS_TOKEN` or
`AYE_LOGS_TOKEN` a bearer token, for storage behind an authenticating proxy.

The artifact importer does not trust the artifact: it keeps only lines of `aye_*` series with a
timestamp from the last `lookback_hours` (up to an hour ahead), sets `repo` to the repository it
listed, refuses archives over 4 MiB, and downloads the archive from the storage redirect without
the GitHub token. Imported artifact ids are kept in the `github-state` volume, so a restart does
not import a report twice.

A report that cannot be made or pushed is a warning and exit code 0, so telemetry never fails a
build; `--strict` turns it into exit code 1. `aye report run` keeps the exit code of the command
it timed.

## Logs

Every report also becomes one JSON line in Loki under `{job="aye-report", project, report}`,
with the context, the run id, commit, link and the values, so «which run was that» is a Loki
query while the dashboards stay on low-cardinality series:

```logql
{job="aye-report", project="shop", report="step"} | json | name="deploy" | result="fail"
```

Reports imported from artifacts are logged by the `github-exporter` container (`event="import"`
with run, branch and commit), which the [logs](modules.md#logs) module ships like any other
container. The CI job logs themselves stay in the CI system; the `url` of the report line
links to them. The logs module ships container logs and journald, not arbitrary files, so the
`_diag` logs of a self-hosted runner stay on its host.

## Use it

GitHub Actions, a step on a self-hosted runner on the private network:

```yaml
- run: uv run pytest --junitxml=junit.xml --cov --cov-report=xml
- if: always()
  run: |
    python3 -I ops/aye-report.py junit junit.xml
    python3 -I ops/aye-report.py coverage coverage.xml
  env:
    AYE_METRICS_URL: http://10.8.0.1:8428
    AYE_LOGS_URL: http://10.8.0.1:3100
```

Or the composite action, which times a command and uploads the reports:

```yaml
- uses: AyeOps/ayeops/ci/github@<commit>
  with:
    name: tests
    run: uv run pytest --junitxml=junit.xml --cov --cov-report=xml
    junit: junit.xml
    coverage: coverage.xml
```

Pin the action to a commit. While the AyeOps repository is private, a workflow of another
account cannot use the action; copy `ci/aye-report.py` into the repository and call it as
above.

A mutation score, as AyeOps reports its own in `.github/workflows/mutants.yml`: one job per
module, the `mutmut results --all true` output as the report and the module path as the suite,
so each module is its own series and its own 30-day median. The step has `continue-on-error`,
on top of `aye report` exiting 0 on a reporting error:

```yaml
- run: uv run mutmut results --all true > mutants/results.txt
- uses: ./ci/github
  continue-on-error: true
  with:
    name: mutants
    mutants: mutants/results.txt
    suite: src/ayeops/promql.py
```

GitLab CI: include [`ci/gitlab/aye-report.gitlab-ci.yml`](../ci/gitlab/aye-report.gitlab-ci.yml)
from a copy in your repository and `extends: .aye-report` in a job. It runs the vendored
`ci/aye-report.py` (`AYE_REPORT_SCRIPT`), or downloads `AYE_REPORT_SCRIPT_URL` when you set it
to a raw URL pinned to a commit.

A deploy script sources [`ci/deploy.sh`](../ci/deploy.sh), which needs only `sh` and `curl`:

```sh
. /opt/ayeops/deploy.sh
AYE_PROJECT=shop AYE_JOB=prod aye_run deploy docker compose up -d --wait
```

`aye_run <step> <command...>` runs the command, reports its duration and result, and returns
its exit code. Without `AYE_METRICS_URL` it warns and reports nothing; with `AYE_STRICT=1` a
failed push fails the call.

The lead time of a merge: `aye report pr` in a workflow on `pull_request: closed` reads the
event (first commit of the pull request to `merged_at`), and on a push to the default branch it
reads a merge commit (first commit of the merged branch to the merge). `--started-at` and
`--merged-at` give the times explicitly.

## Retention

The medians reach back 30 days and «since the last deploy» 90 days, while the metrics module
keeps 30 days by default. Set `metrics.retention` to `100d` or more on the central host; the
delivery series are a few hundred samples a day, so the cost is the other series kept as long.
