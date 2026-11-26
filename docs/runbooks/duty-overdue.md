# A recurring duty is overdue

A [duty](../manifest.md#duties) of the manifest, work only a person can do (an access review, a
manual restore, a credential rotation, a contract renewal), was not recorded as done in time:
its `last_done` plus `every` is the `due` label of the alert, and that day has passed in the
manifest's `timezone`. Nothing has broken yet; the duty has lapsed, and the reason to keep it
was that one day it will matter.

The alert fires once per duty, with the labels `duty`, `title`, `due`, `every`, and `owner` when
the duty names one. Its runbook link is the duty's own `runbook` when it has one, else this
page. It stays on until a render with a later `last_done` is deployed.

## What to do

1. Which duty and since when: the `duty` and `due` labels, or `aye duties` next to the
   manifest, which lists every duty with its due day and the days left or overdue.
2. Do the duty, or hand it to its `owner`, by its own runbook or its `title`.
3. Record it: set `last_done` of the duty to the day it was done, `YYYY-MM-DD`, commit the
   manifest with what was done in the message, so the history of the registry is the git log,
   and deploy the host that runs Grafana. `aye check` refuses a `last_done` after today.
4. If the duty cannot be done now, say so in the alert's channel and silence the alert in
   Grafana for the days it takes. Do not move `last_done` to a day the duty was not done:
   the next due day is counted from it.
5. A duty nobody does any more because it no longer applies: remove it from `duties`; one
   that is done more or less often than declared: change `every`.
