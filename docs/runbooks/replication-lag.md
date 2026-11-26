# Replica lags behind its primary

For 5 minutes the replica (label `service`) has not replayed the WAL position its primary had
`max_lag` ago (default 5 minutes): it still receives and applies changes, only too slowly, or it
paused. Reads from the replica return data older than `max_lag`; a failover to it now loses or
delays what it has not replayed. The pair, the threshold and how the lag is measured:
[replication](../manifest.md#replication) and
[database replication](../modules.md#database-replication).

An idle primary does not fire this alert: it writes nothing, so nothing is waiting to be
replayed. A replica that stopped streaming altogether fires
[replication-broken](replication-broken.md) instead.

## What to do

1. On the primary, see what the standby has received and replayed:

   ```sql
   SELECT application_name, state, sent_lsn, write_lsn, flush_lsn, replay_lsn,
          write_lag, flush_lag, replay_lag,
          pg_wal_lsn_diff(pg_current_wal_lsn(), replay_lsn) AS behind_bytes
   FROM pg_stat_replication;
   ```

   - `sent_lsn` far behind `pg_current_wal_lsn()`: the primary cannot send fast enough, the
     network between the hosts is slow or saturated, or the primary is under heavy write load.
   - `flush_lsn` close to `sent_lsn` but `replay_lsn` far behind: the replica receives the WAL and
     cannot apply it; go on with step 2.
2. On the replica:

   ```sql
   SELECT pg_is_wal_replay_paused(), pg_last_wal_receive_lsn(), pg_last_wal_replay_lsn(),
          now() - pg_last_xact_replay_timestamp() AS last_replayed;
   ```

   - replay paused: someone ran `pg_wal_replay_pause()`; `SELECT pg_wal_replay_resume();`
     when that was not deliberate.
   - a long query on the replica holds replay back while `max_standby_streaming_delay` lets it
     (`-1` waits forever): find it in `pg_stat_activity` and cancel it, or lower the setting.
   - the replica is short of disk I/O or CPU: check the host's dashboard, and whether
     [disk-full](disk-full.md) or [memory-low](memory-low.md) fire there.
3. A bulk load, a `VACUUM FULL` or an index build on the primary writes WAL faster than the
   replica applies it; the lag shrinks once it ends. The alert resolves when the replica has
   replayed what the primary had `max_lag` ago.
4. When a lag of this size is normal for the pair, raise `max_lag` in its `replication` block
   instead of muting the alert.
