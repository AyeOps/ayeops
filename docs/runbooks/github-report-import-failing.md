# aye report artifacts not imported

The exporter could not list or import the `aye-report*` artifacts of a repository for 30
minutes, so the CI metrics of its GitHub-hosted runners are missing.

## What to do

1. `docker logs github-exporter` shows `artifacts-failed` or `import-failed` with the reason.
2. `404` on the artifact list: the token cannot read the repository; it needs `repo` for a
   private one.
3. `import-failed` with a storage error: VictoriaMetrics on the same host is down or full.
4. Lines that do not look like `aye report` output are counted in
   `ayeops_github_report_lines_rejected_total` and dropped; they do not fail the import.
