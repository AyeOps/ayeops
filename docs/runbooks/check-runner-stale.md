# Script check runner stale

The script runner of the checks module (`check-scripts` or `check-scripts-net`) has not written
its results to the node-exporter textfile directory for 5 minutes. Every script check on that
host fires its own alert as well, since its results are stale.

## What to do

1. `docker compose ps` on the host: is the runner container up and healthy?
2. `docker compose logs <runner>`: a config file that does not parse stops the runner at start.
3. The runner writes `/textfile/ayeops_<runner>.prom`; check that the `node-textfile` volume, or the node module's `textfile_dir`, is
   mounted and node-exporter runs on the same host.
