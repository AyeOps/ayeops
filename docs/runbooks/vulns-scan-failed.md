# The vulnerability scan of an image failed

In its last run Trivy of the [vulns](../modules.md#vulns) module wrote no report for this image,
so its vulnerabilities are unknown: the counts of the image are absent, not zero. When every
image of the host fails at once, the vulnerability database did not download.

## What to do

1. `docker compose logs --tail 20 vulns-agent`: `db_update` false means the database step failed,
   `failed` lists the images without a report.
2. `docker compose logs --tail 100 vulns` shows Trivy's own errors. Common ones:
   - `failed to download vulnerability DB`, `TOOMANYREQUESTS` or a timeout: no internet from the
     host, or the registry of the database (`mirror.gcr.io`, then `ghcr.io`) limits this
     address. It clears with the next run.
   - `No such image` and `unauthorized`: the image is not on the host (never pulled, or pruned)
     and the registry refuses an anonymous pull. Pull it on the host, or exclude it.
   - exit code `124` in `/state/reports/results`: the scan took longer than `timeout_minutes`;
     raise it for very large images.
   - exit code `137`: Trivy hit its memory limit; raise it with `resources`.
3. `docker compose ps vulns-proxy`: an unhealthy proxy means Trivy cannot read the local images
   and falls back to the registry.
4. `docker compose exec -e AYE_VULNS_ONCE=1 vulns-agent python -I /app/vulns_agent.py` asks for a
   run now; the loop picks it up within 15 seconds and the alert resolves once the image scans.
