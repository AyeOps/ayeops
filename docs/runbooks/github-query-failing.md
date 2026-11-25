# GitHub billing query failing

The billing usage query of an account has failed for 30 minutes, so the minutes on the
dashboard are stale and `github-minutes-limit` cannot fire.

## What to do

1. `docker logs github-exporter` shows the HTTP status of `usage-failed`.
2. `401`: `AYE_GITHUB_TOKEN` expired or was revoked.
3. `403` or `404` for an organization: the token needs `read:org` and an owner or billing
   manager of the organization. For a user it needs the `user` scope; GitHub answers `404`
   without it.
4. Check by hand: `gh api organizations/<org>/settings/billing/usage` or
   `gh api users/<user>/settings/billing/usage`.
