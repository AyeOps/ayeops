# Deploy failed

The latest `deploy` step a project reported ended with `result="fail"`. The alert stays until a
later deploy of the same project and job reports `ok`, or for seven days.

## What to do

1. «Latest runs» on `AyeOps / Delivery` links the run (`url`) and names the commit.
2. In Grafana Explore, `{job="aye-report", project="<project>", report="step"}` in Loki shows the
   log line of every reported step with its duration, result, commit and link.
3. Look at the deploy log itself: the CI job, or the output of the deploy script on the host.
4. Check that the services are back on the previous version; a half-done deploy is worse than
   a failed one. The `containers` alerts fire for a service that keeps restarting.
