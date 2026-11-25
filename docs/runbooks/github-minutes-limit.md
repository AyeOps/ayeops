# GitHub Actions minutes 80 % used

An account used more than 80 % of the Actions minutes its plan includes this month. Past 100 %
the jobs of private repositories stop or are billed, depending on the spending limit.

The count follows GitHub: only private repositories, Windows minutes count twice, macOS minutes
ten times, larger runners and self-hosted runners do not use the included minutes. The quota is
`included_minutes` of the account in `github.accounts` (2 000 for Free).

## What to do

1. «Minutes by repository» on `AyeOps / Delivery` names the repository and runner type.
2. Usual causes: a workflow that runs on every push of every branch, a matrix that grew, a
   scheduled workflow, or a retry loop.
3. Move heavy jobs to a self-hosted runner, add `concurrency` with `cancel-in-progress`, or
   narrow the `on:` filters.
   Keep pull-request jobs of a public repository on GitHub-hosted runners: they are free there,
   and a fork's code must not run on a machine of yours. AyeOps's own workflows read the runner
   of their `push`, `schedule` and manual runs from the repository variable `AYE_RUNS_ON`, a JSON
   list of runner labels, e.g. `gh variable set AYE_RUNS_ON --body '["self-hosted","ayeops"]'`;
   delete it to go back to `ubuntu-latest` ([e2e](../e2e.md#where-it-runs)).
4. The quota resets on the first day of the month.
