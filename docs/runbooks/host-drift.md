# Host runs containers the manifest does not declare

For an hour the [reconciler](../modules.md#reconciler) on this host has seen running containers
that belong to no compose project the manifest declares for the host: not to the rendered
project (`<project>-<host>`) and not to any file under the host's `compose`. Typical causes are a
`docker run` by hand that nobody removed, a project started with `docker compose up` from a
folder the manifest does not list, and a leftover after a service moved to another host. Nothing
is broken yet, so it is a warning and pages nobody: an undeclared container has no alert, no
backup and no runbook, holds memory and ports the declared services expect, and disappears from
every view the manifest drives.

## What to do

1. See which containers. `ayeops_undeclared_container_info` in Grafana (the panel
   "Containers the manifest does not declare" on `AyeOps / Health`) names each one with its
   image and, for a compose container, its `compose_project`; up to 50 per host, the rest
   counted in `ayeops_undeclared_containers_overflow`. On the host:
   `docker ps --format '{{.Names}}\t{{.Image}}\t{{.Label "com.docker.compose.project"}}\t{{.RunningFor}}'`.
2. Find out who started it: `docker inspect <name> --format '{{json .Config.Labels}}'`,
   `docker inspect <name> --format '{{.Created}} {{.Config.Cmd}}'` and `docker logs --tail 50
   <name>`. A `compose_project` label names the project folder it came from
   (`com.docker.compose.project.working_dir`).
3. Decide:
   - **Forgotten or left behind**: `docker stop <name>` and `docker rm <name>`, or
     `docker compose -p <compose_project> down` for a whole leftover project. Check first that
     its volumes hold nothing anyone needs (`docker inspect <name> --format '{{json .Mounts}}'`).
   - **It should run here**: declare it. Put its compose file under the host's `compose` in the
     manifest (or the service into a module), run `aye check` and `aye deploy`. It then gets the
     reconciler, the logs and the catalog rules like any declared service.
   - **It is meant to stay undeclared** (a CI runner's job containers, a BuildKit builder, a
     short-lived tool): add a name pattern to the module's `ignore`, for example
     `ignore: ["buildx_buildkit_*"]`, and redeploy.
4. The alert clears on the next pass of the reconciler (every `interval`, 30 seconds by default)
   once no undeclared container runs.

`aye discover` and `aye portal --discovered` give the same picture as a one-off report with
suggestions how to declare what was found.
