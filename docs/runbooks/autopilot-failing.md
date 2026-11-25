# Autopilot runs fail

An autopilot run ended without a verdict in the last hour. The alert it was started for went to
the ops channel and to on-call as usual; only the diagnosis is missing. The journal says why:

```sh
docker compose exec autopilot sh -c 'tail -n 5 /tmp/journal.jsonl'
```

The `reason` of a line with `"status":"failed"` starts with the stage:

| Stage | What failed | Look at |
|---|---|---|
| `dossier` | collecting the dossier itself, which is rare: a single failed query only lands in `dossier_missing` | `docker compose logs --tail 50 autopilot` |
| `model: HTTPError: HTTP Error 401` | the API key | `AYE_AUTOPILOT_API_KEY` in `.env`, then `docker compose up -d autopilot` |
| `model: HTTPError: HTTP Error 429` or `529` | the provider is rate limited or overloaded | passes on its own; the run is not retried |
| `model: ModelError: the run cap does not cover ...` | `run_usd` is too small for the dossier | raise `run_usd` or lower `dossier_tokens` |
| `model: ModelError: stop_reason refusal` | the model and its fallback declined | read the dossier in `/tmp/dossiers/<run>.json`; a log line may carry text that trips the safety classifiers |
| `model: TimeoutError` | the run did not finish within `run_seconds` | the provider was slow; raise `run_seconds` if it repeats |
| `model: ModelError: claude ...` | `runner: claude-code` returned an error, the subtype is in the reason | `error_max_budget_usd` means the run hit `run_usd`; an auth error means the token in `.env` expired |

`ayeops_autopilot_failures_total{stage}` counts the same stages, plus `post` (the verdict could
not reach the on-call chat) and `journal` (the volume is full or read-only). Nothing the
autopilot does in shadow mode changes the fleet, so a failing run never needs a rollback.
