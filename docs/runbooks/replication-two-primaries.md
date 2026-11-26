# Two primaries

For 2 minutes both nodes of a declared pair (label `service` is the replica) have been out of
recovery: each accepts writes on its own. Writes that land on the replica are not on the
primary and the other way round; the longer both stay writable, the more data has to be merged
by hand or is lost. This is a split brain. The pair:
[replication](../manifest.md#replication).

It usually follows a failover that promoted the replica while the old primary was only cut
off, not down, and came back; or a replica restarted without its `standby.signal`.

## What to do

1. Stop the writes to one side first, before you look into why. Find out which node the
   applications write to (their connection settings, a virtual IP, a proxy such as PgBouncer or
   HAProxy) and keep that one; fence the other so nothing can write to it: stop PostgreSQL on
   it, or block its port.
2. On both nodes, `SELECT pg_is_in_recovery(), pg_current_wal_lsn();` and the time of the last
   write each took (for example the newest row of a busy table). The node that took writes the
   other does not have needs those rows copied over by hand before it is rebuilt.
3. Rebuild the fenced node as a replica of the kept one: `pg_rewind` when it diverged a little,
   a new `pg_basebackup` otherwise, with `standby.signal` and a `primary_conninfo` that names the
   kept node.
4. When the kept node is the declared replica, swap the roles in the manifest: the
   `replication` block moves to the old primary and names the new one.
5. Find what promoted the second node: the failover tool's log, or who ran `pg_promote()` or
   `pg_ctl promote`. Fencing the old primary is part of every failover, see
   [the switch, and split brain](../failover.md#the-switch-and-split-brain).

The alert resolves once one of the nodes is in recovery again.
