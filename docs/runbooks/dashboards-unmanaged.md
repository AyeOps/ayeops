# Dashboards drawn in Grafana, not in the repository

For a day, Grafana has held at least one dashboard that no file provisions: someone drew it in
the UI, or saved a copy of a provisioned one. It lives only in Grafana's database, in the
`grafana-data` volume: nobody reviewed it, the manifest's repository has no copy, and a lost
volume or a fresh Grafana loses it. Nothing is broken now. `grafana-reader` counts these
dashboards every 5 minutes as `ayeops_grafana_reader_dashboards_unmanaged`. Background:
[modules](../modules.md#dashboards-drawn-in-grafana).

1. See which dashboards, from a machine with the repository and the Grafana admin credentials:

   ```sh
   aye secrets run -- aye dashboards drift ops/ayeops.yaml
   ```

   Each `unmanaged` line names a uid, a title and a folder. `stale` and `not-deployed` lines mean
   the Grafana host runs an older render: deploy it.

2. Keep the ones worth keeping: run `aye dashboards pull`.

   ```sh
   aye secrets run -- aye dashboards pull ops/ayeops.yaml
   aye secrets run -- aye dashboards pull ops/ayeops.yaml --uid <uid>
   ```

   It writes each one as `<uid>.json` into the manifest's `grafana.dashboards` directory, without
   `id` and `version`, with the datasource uids of the manifest. It refuses to replace a file that
   differs unless `--force`. A `warning` line names a datasource the manifest does not provision:
   declare it under `grafana.datasources` or point the panels at one that is.

3. Review the files, run `aye check`, commit, render and deploy the Grafana host. Grafana then
   provisions each dashboard under the same uid, in the folder named after `project`, and the UI
   copy becomes that provisioned dashboard: links and bookmarks keep working.

4. A dashboard nobody needs: delete it in Grafana (Dashboard settings, Delete).

The alert clears on the next pass of the reader once `ayeops_grafana_reader_dashboards_unmanaged`
is `0`.
