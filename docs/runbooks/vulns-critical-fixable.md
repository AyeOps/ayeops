# A running image has a critical vulnerability with a fix

For an hour at least, the last Trivy scan of the [vulns](../modules.md#vulns) module found a
`CRITICAL` vulnerability in an image this host runs, and the advisory names a fixed version of
the package. The fix exists: a newer image, or a rebuild of the same tag on a newer base, would
remove it.

The alert fires once per image. Critical says how bad the flaw is in general, not whether this
container can be reached through it: a vulnerable library the service never loads, or a tool in
the image nobody calls, is still counted.

## What to do

1. `docker compose logs --tail 20 vulns-agent`: the JSON line of the last run lists
   `critical_fixable` per image. The counts in PromQL:
   `ayeops_vulns_vulnerabilities{host="<host>", severity="critical", fixed="true"} > 0`.
2. The details are in the report Trivy wrote:
   `docker compose exec vulns-agent cat /state/reports/results` lists the key, exit code and
   image of every scan, and `/state/reports/<key>.json` lists each `VulnerabilityID`,
   `PkgName`, `InstalledVersion` and `FixedVersion` of that image.
3. A pinned image (`grafana/grafana:13.2.3`): look for a newer release that carries the fixed
   package; Renovate's pull request from the [updates](../modules.md#updates) module may already
   be open. An image of the project: rebuild it on a current base image and bump what pins the
   fixed library.
4. Deploy the new image the way the stack is deployed (`aye deploy`, or the project's script).
   The agent asks for a new scan as soon as the list of images it was rendered with changes, so
   the alert resolves within minutes of the deploy, or after the next daily run.
5. When the advisory does not apply and nothing better exists yet, add the image to the module's
   `exclude` with a note in the manifest of why; the image is then not scanned at all.
