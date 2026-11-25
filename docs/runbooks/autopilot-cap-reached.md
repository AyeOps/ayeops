# Autopilot spend cap reached

The next autopilot run would cross one of its caps, so runs do not start until the cap resets.
Critical alerts keep arriving in the ops channel and on-call as usual, without a diagnosis. Each
alert that came in the meantime is in the journal as `"status":"skipped"` with
`"reason":"cap:<cap>"`.

| `cap` | Resets | Default |
|---|---|---|
| `hour` | an hour after the oldest of the last runs | 4 runs an hour (`runs_per_hour`) |
| `day` | at midnight in `timezone` | 3 USD (`day_usd`) |
| `night` | at the end of quiet hours | 2 USD for the whole night (`night_usd`) |

A run is counted at `run_usd` when it starts and at its real cost when it ends, so a cap stops
the run that could cross it, not the one after. What to check:

- An alert storm: many critical fingerprints at once. `ayeops_autopilot_runs_last_hour` and the
  `alertname` of the skipped lines show which alert. Fix the storm; raising the cap only spends
  more on the same answer.
- Expensive runs: `cost_usd` per line of the journal. Opus runs (an alert of a service in
  `critical_services`, or the same alert twice within a day) cost about twice as much.
- A cap set too low for the fleet: raise `day_usd`, `night_usd` or `runs_per_hour` in the
  manifest, `aye render`, deploy.

The monthly budget of the provider is the [ai-usage](../modules.md#ai-usage) module's, and its
`ai-budget-80` alert covers the autopilot's spend together with everything else on that key.
