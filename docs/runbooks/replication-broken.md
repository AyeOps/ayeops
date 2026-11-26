# Replication broken

For 5 minutes the declared pair (label `service` is the replica) has not been replicating, or
cannot be confirmed to. The label `side` says which node the alert is about:

- `side="replica"`: the replica is not in recovery any more (it was promoted, or started
  without its `standby.signal`), it reports nothing, or the primary is up and writable and
  lists no standby with the replica's `application_name` in state `streaming`;
- `side="primary"`: the primary reports nothing, so the pair cannot be checked.

Until this is fixed, the replica falls further behind with every write, and a failover to it
loses everything since it stopped. The pair and the series:
[replication](../manifest.md#replication) and
[database replication](../modules.md#database-replication). When both nodes are writable,
[replication-two-primaries](replication-two-primaries.md) fires instead.

## What to do

1. `side="primary"`: is PostgreSQL on the primary up, and does its postgres_exporter answer?
   `curl -s http://<exporter>:9187/metrics | grep -E '^pg_(up|replication_is_replica)'` must
   show `pg_up 1`. [target-down](target-down.md) fires for an exporter that does not answer;
   a scrape job without the label `service: <primary>` leaves the series unnamed, so check the
   labels in Grafana Explore: `pg_replication_is_replica`.
2. `side="replica"`: on the replica, `SELECT pg_is_in_recovery();`
   - `false`: the replica was promoted, by a failover tool or by hand, or lost its
     `standby.signal`. If the primary still runs, find out which node the applications write
     to before anything else. A deliberate failover leaves the manifest out of date: swap
     `primary` and the replica in the `replication` block. Otherwise rebuild the replica from
     the primary (`pg_basebackup`, or `pg_rewind` when it took only a few writes).
   - `true`: the replica waits for WAL. Its log (`journalctl -u postgresql` or the container
     log) names the reason: a wrong host, port or password in `primary_conninfo`, a
     `pg_hba.conf` of the primary that refuses the replication connection, or
     `requested WAL segment ... has already been removed`, which needs a new base backup, or a
     replication slot so it does not happen again.
3. On the primary, `SELECT application_name, client_addr, state FROM pg_stat_replication;`
   - no row for the replica: it is not connected, see step 2.
   - a row under another name: `application_name` in the replica's `primary_conninfo` is not
     the one of the `replication` block (default the replica's service name; PostgreSQL uses
     `cluster_name` or `walreceiver` when it is unset). Make them the same.
   - a row in state `catchup`: the replica reconnected and is catching up; the alert resolves
     when it reaches `streaming`.
4. The alert resolves once the primary lists the standby as `streaming` again and both nodes
   report.
