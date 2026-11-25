# No delivery report when one is expected

The manifest expects a `step` report of a project at least every `every` (`delivery.expect`),
and none arrived in that time. Either nothing ran, or it ran and could not report.

## What to do

1. Check that the pipeline or deploy ran at all: a schedule that was turned off, a runner that
   is offline, a branch filter.
2. If it ran, read its log for `aye report:` lines. Without `--strict` a failed push is only a
   warning, so the job stays green.
3. From a self-hosted runner, check that the runner reaches `AYE_METRICS_URL` (port `8428` of the
   metrics server on the private network).
4. From a GitHub-hosted runner, check the `github-report-import-failing` alert and that the
   repository is listed under `github.reports`.
