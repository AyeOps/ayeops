# Reconciler gave up on a compose project

The [reconciler](../modules.md#reconciler) on this host started containers of the compose
project in the `compose_project` label as often as its back-off allows (`attempts` within
`window`, 3 in an hour by default) and they are still down. It starts nothing more in that project until a pass
finds nothing left to start there, so the next step is a person's.

## What to do

1. `docker compose logs --tail 50 reconciler` in the rendered project of the host. Each attempt
   is one JSON line: `reasons` says why each container was started (`start-failed`, `crashed`,
   `oom-killed`, `not-back-after-boot`), `errors` what the start or a dependency answered, and
   `before` and `after` the status, exit code, error and health of every container of the project.
2. `docker ps -a --filter label=com.docker.compose.project=<project>` and, for the container that
   stays down, `docker inspect <name> --format '{{json .State}}'` and `docker logs --tail 200
   <name>`.
3. The usual causes:
   - `State.Error` mentions an address (`cannot assign requested address`): the container binds
     a port on an address that is not up, often a VPN address after a reboot. Bring the
     interface up, or make docker start after it.
   - `State.Error` mentions a mount (`no such file or directory`, `not a directory`): a bind-mount
     source is missing, often a disk or network share that is not mounted.
   - `port is already allocated`: something else took the port.
   - The container starts and exits again: its own logs say why; `OOMKilled` true means it needs
     more memory (`resources` in the manifest).
   - `dependency <service> is stopped`: a service it depends on was stopped on purpose. Start that
     one first, or leave both down.
   - A container that is `dead` or gone counts in `ayeops_reconciler_missing`; the reconciler
     never recreates. `aye deploy`, or `docker compose up -d` in the project, does.
4. Once the containers run, the next pass logs `recovered` and the alert clears. To keep a
   service down for good, put it in the module's `exclude` or give it `restart: "no"` in its
   compose file, and redeploy.
