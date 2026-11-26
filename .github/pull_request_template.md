## What and why

What this changes, and why it is needed. Link the issue with `Closes` and its number.

## How it was tested

The commands you ran and what they showed; measured numbers when there are any.

## Checklist

- [ ] `uv run ruff check . && uv run ruff format --check .`, `uv run mypy` and `uv run pytest -n auto` pass
- [ ] `uv run python -m ayeops.comments .` and `uv run python -m ayeops.texthygiene .` pass
- [ ] `uv run aye check examples/acme.yaml` passes
- [ ] `examples/rendered` is regenerated if a module or a template changed
- [ ] A new service ships complete: description, metric, alert with severity and runbook, log source, status mark
- [ ] A new alert has its page in `docs/runbooks/<alert>.md`
- [ ] New images are pinned to an exact version and published for `linux/amd64` and `linux/arm64`
- [ ] Ports bind to `127.0.0.1` unless the manifest exposes them
- [ ] README.md, `docs/modules.md`, the runbooks and `CHANGELOG.md` say what changed
- [ ] No secret, token or key in the diff, the fixtures or the logs
- [ ] `npm run typecheck` and `npm test` pass in `cloud/status`, if the Worker changed
