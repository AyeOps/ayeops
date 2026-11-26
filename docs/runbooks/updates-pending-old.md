# An update has waited for more than 30 days

A dependency of this repository has a newer version released more than 30 days ago that is
still not merged (`ayeops_updates_oldest_pending_release_timestamp_seconds`). The repository
waits 14 days before a version is offered, so its pull request has been open for about two
weeks, or Renovate could not open one.

## What to do

1. Open the repository's dependency dashboard issue: it lists every pending update and why it
   waits (awaiting schedule, pull request limit, pending approval, or an error).
2. Merge the pull request when CI is green, or close it if the version must not be taken:
   Renovate will not reopen a closed pull request for the same version, and the dependency
   drops out of the count after the next run.
3. A major version that needs a migration can be pinned out in the repository's
   `renovate.json` with a `packageRules` entry (`allowedVersions` or `enabled: false`), with the
   reason in its `description`.
4. No pull request at all: the update may be blocked by `prConcurrentLimit`, or the branch
   failed; the run's log in `docker compose logs updates` says which.
