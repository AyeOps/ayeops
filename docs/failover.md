# Failover

Design. Of it, only the service `criticality` ships, which the
[incident journal](modules.md#incidents) weighs downtime by; the rest does not yet. A host is
gone: its disk died, its data center lost power, or it was destroyed. A standby host exists. The kit brings the services of the lost host back on
the standby in the order of what their downtime costs, and `aye check` proves long before that
day that the plan fits.

## Priorities

Each service may declare how much its downtime matters and where it goes when its host is lost:

```yaml
services:
  payments-intake:
    host: central
    priority: 0
    criticality: critical
    failover: [standby-kyiv, standby-cloud]
    state: stateless
  payments-db:
    host: central
    priority: 0
    criticality: critical
    failover: [standby-kyiv]
    state: replicated
  reports:
    host: central
    priority: 2
    state: restore
```

| Field | Meaning |
|---|---|
| `priority` | `0` comes back first; services without one come back last, or not at all |
| `criticality`, `cost` | how much a minute of downtime matters, as a tier or as money when it is known, see [What downtime costs](autopilot.md#what-downtime-costs); it weighs the north star, picks who is paged and how fast, and sizes the autopilot budget |
| `failover` | standby hosts in order of preference |
| `state` | `stateless` (nothing to carry over), `replicated` (a replica runs on the standby), `restore` (comes back from the newest [off-site](modules.md#offsite) copy, losing what changed since) |

Within one priority, services come back in the order of their dependencies: the database
before the application that writes to it. That order needs a `depends_on` list on the service;
the [flows](manifest.md#flows) of the manifest only draw the portal diagram and say nothing about
start order.

## Data that survives the host

This part ships. A failover with `state: restore` is only as good as the newest copy of the data,
so a service declares what its backups promise in [`data`](manifest.md#data): the engine, the
[backups](modules.md#backups) job that dumps it, the `rpo` (how much it may lose), the `rto`
(how long a restore may take) and the `copies` the dump must exist in (`host`, `offsite`).
`aye check` holds the manifest to it ([data rules](rules.md#data-rules)):

- `backup-rpo`: the dump job's threshold (longest gap plus grace) and the age of the daily
  off-site copy (longest gap plus one retry) fit into the `rpo`; with an `rpo`, `backup-missing`
  and `offsite-stale` fire at the `rpo` when it is the tighter limit;
- `backup-placement`: for `criticality: critical`, two copies in different zones, one with
  another provider than the host when the hosts name their `provider`, and one append-only
  copy, which is off-site and so restore-tested every week;
- `backup-rto`: a restore drill of the copy fails when the restore takes longer than the `rto`;
- the warning `data-declared`: a critical service on a database image or unit without `data`.

The `rpo` is what a `state: restore` failover loses at worst; the drill's measured duration is
the part of the `rto` the kit can measure today.

## What aye check proves in advance

A failover plan that was never checked fails on the day it is needed. So `aye check` refuses a
manifest when:

- the services of priority `0` of a host do not fit into the memory budget of their first
  standby, next to what the standby already runs;
- a standby stands in the `zone` of the host it replaces;
- a stateful service of priority `0` has `state: restore`: coming back from last night's copy
  loses a day of payments, so it needs a replica;
- the secrets of a failover service are only reachable from the host that is lost: a secrets
  manager on that host takes the keys of the standby with it;
- a service names a standby that runs no metrics agent, so nobody would see it come up.

## Warm standby

The standby is prepared before anything fails: every deploy also renders the failover services
for each standby, pulls their images there and keeps their config in place, stopped. Coming
back is then `docker compose up` and a switch of traffic, minutes instead of hours of pulling
and restoring.

## The switch, and split brain

The dangerous case is a host that is not dead but cut off: its network failed, its services
still run. If the standby takes over the payments while the original keeps processing, there
are two live payment services, double charges and two databases that drift apart. So the
failover runs in two steps:

| Step | Who runs it |
|---|---|
| bring the stateless services up on the standby, ready for traffic, and the replicas ready for promotion | the kit, at once, without confirmation |
| switch DNS or ingress to the standby and promote the replica | a person with one confirmation, or the kit alone only when the old host is proven dead: the provider API reports the machine off, and sentinels in two other zones cannot reach it |

## Drills

Like the weekly restore test of the [offsite](modules.md#offsite) module, a failover that was
never rehearsed does not count. A scheduled drill brings the priority `0` services of a host up
on its standby, against copies of the data, without switching traffic, and measures how long
each took. The measured times are metrics, with an alert when a drill fails or takes longer
than the downtime the project accepts.

## Metrics

- time from the host lost to each service healthy on its standby, per priority;
- the drill results and their durations;
- the age of the newest copy of every `state: restore` service, the data that a failover
  would lose; for the dump on the host this ships as the Reliability table **Last good dump
  against its RPO**.
