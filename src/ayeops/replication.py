from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from ayeops.manifest import Manifest

ENGINE = "postgres"
DEFAULT_MAX_LAG = "5m"
RUNBOOK_BASE = "https://github.com/AyeOps/ayeops/blob/main/docs/runbooks/"
LAG = "replication-lag"
BROKEN = "replication-broken"
SPLIT = "replication-two-primaries"
RUNBOOKS = tuple(f"{RUNBOOK_BASE}{name}.md" for name in (LAG, BROKEN, SPLIT))
IS_REPLICA = "pg_replication_is_replica"
WAL_BYTES = "pg_stat_replication_pg_current_wal_lsn_bytes"
WAL_DIFF = "pg_stat_replication_pg_wal_lsn_diff"
REPLICA_METRICS = (IS_REPLICA,)
PRIMARY_METRICS = (IS_REPLICA, WAL_BYTES, WAL_DIFF)
STREAMING = 'state="streaming"'
ITSELF = "a service cannot be the replica of itself"

Problem = tuple[str, str, str]


@dataclass(frozen=True, order=True)
class Pair:
    replica: str
    replica_host: str
    primary: str
    primary_host: str
    max_lag: str
    application_name: str


def spec_of(entry: dict[str, Any]) -> dict[str, Any] | None:
    spec = entry.get("replication")
    return spec if isinstance(spec, dict) else None


def primary_problem(manifest: Manifest, primary: str) -> str | None:
    if primary not in manifest.services:
        return f"no service {primary}: name the database service the replica streams from"
    entry = manifest.services[primary]
    if primary not in manifest.active_services:
        return f"the primary {primary} is {entry['state']}: a live replica needs a live primary"
    above = spec_of(entry)
    if above is not None:
        return (
            f"the primary {primary} is itself a replica of {above['primary']}: cascading"
            " replication is not supported, name the primary that is not in recovery"
        )
    return None


def engine_of(entry: dict[str, Any]) -> str | None:
    data = entry.get("data")
    return str(data["engine"]) if isinstance(data, dict) and "engine" in data else None


def engine_problems(manifest: Manifest, name: str, primary: str) -> list[Problem]:
    if engine_of(manifest.services[primary]) is None:
        return [
            (
                f"services.{primary}.data",
                "replication-engine",
                f"{primary} is the primary of {name} and declares no data, so aye check cannot"
                f" tell its engine: declare data with engine {ENGINE}",
            )
        ]
    found = []
    for service in (primary, name):
        engine = engine_of(manifest.services[service])
        if engine is not None and engine != ENGINE:
            found.append(
                (
                    f"services.{service}.data.engine",
                    "replication-engine",
                    f"{service} has engine {engine}: replication is watched for {ENGINE} only",
                )
            )
    return found


def declared_metrics(entry: dict[str, Any]) -> list[str]:
    metrics = entry.get("metrics")
    return [str(metric) for metric in metrics] if isinstance(metrics, list) else []


def metric_problems(manifest: Manifest, name: str, primary: str) -> list[Problem]:
    found = []
    for service, wanted in ((name, REPLICA_METRICS), (primary, PRIMARY_METRICS)):
        declared = declared_metrics(manifest.services[service])
        missing = [metric for metric in wanted if metric not in declared]
        if missing:
            found.append(
                (
                    f"services.{service}.metrics",
                    "replication-metrics",
                    f"the replication alerts of {name} read {', '.join(missing)} of {service}:"
                    f" scrape its postgres_exporter with the label service={service} and list"
                    " them in metrics",
                )
            )
    return found


def pair_of(manifest: Manifest, name: str, entry: dict[str, Any]) -> Pair | None:
    spec = spec_of(entry)
    if spec is None:
        return None
    primary = str(spec["primary"])
    if primary_problem(manifest, primary) is not None:
        return None
    return Pair(
        name,
        str(entry["host"]),
        primary,
        str(manifest.services[primary]["host"]),
        str(spec.get("max_lag") or DEFAULT_MAX_LAG),
        str(spec.get("application_name") or name),
    )


def pairs(manifest: Manifest) -> list[Pair]:
    found = [pair_of(manifest, name, entry) for name, entry in manifest.active_services.items()]
    return sorted(pair for pair in found if pair is not None)


def name_problems(manifest: Manifest) -> list[Problem]:
    streams: dict[tuple[str, str], list[str]] = defaultdict(list)
    for pair in pairs(manifest):
        streams[(pair.primary, pair.application_name)].append(pair.replica)
    found = []
    for (primary, application), replicas in sorted(streams.items()):
        if len(replicas) < 2:
            continue
        for replica in replicas:
            others = ", ".join(other for other in replicas if other != replica)
            found.append(
                (
                    f"services.{replica}.replication.application_name",
                    "replication-name",
                    f"{replica} and {others} stream from {primary} as application_name"
                    f" {application}: give each replica its own, in primary_conninfo and here",
                )
            )
    return found


def replication_problems(manifest: Manifest) -> list[Problem]:
    problems: list[Problem] = []
    for name, entry in sorted(manifest.active_services.items()):
        spec = spec_of(entry)
        if spec is None:
            continue
        primary = str(spec["primary"])
        found = ITSELF if primary == name else primary_problem(manifest, primary)
        if found is not None:
            problems.append((f"services.{name}.replication.primary", "replication-primary", found))
            continue
        problems += engine_problems(manifest, name, primary)
        problems += metric_problems(manifest, name, primary)
    return problems + name_problems(manifest)


def selector(service: str, *matchers: str) -> str:
    return "{" + ",".join([f'service="{service}"', *matchers]) + "}"


def stream(pair: Pair, *matchers: str) -> str:
    return selector(pair.primary, f'application_name="{pair.application_name}"', *matchers)


def labelled(expr: str, host: str, side: str = "") -> str:
    found = f'label_replace({expr}, "host", "{host}", "", "")'
    if not side:
        return found
    return f'label_replace({found}, "side", "{side}", "", "")'


def writable(service: str) -> str:
    return f"(max({IS_REPLICA}{selector(service)}) == bool 0)"


def split_expr(pair: Pair) -> str:
    return f"(({writable(pair.primary)} * on () {writable(pair.replica)}) or on () vector(0))"


def lag_expr(pair: Pair) -> str:
    behind = f"max({WAL_DIFF}{stream(pair)})"
    newest = f"max({WAL_BYTES}{stream(pair)})"
    oldest = f"min(min_over_time({WAL_BYTES}{stream(pair)}[{pair.max_lag}]))"
    return labelled(
        f"(({behind} > bool ({newest} - {oldest})) or on () vector(0))", pair.replica_host
    )


def broken_expr(pair: Pair) -> str:
    promoted = f"({writable(pair.replica)} or on () vector(1))"
    standby = stream(pair, STREAMING)
    streaming = f"(count({WAL_BYTES}{standby}) or on () vector(0))"
    unstreamed = f"(({writable(pair.primary)} * on () ({streaming} == bool 0)) or on () vector(0))"
    replica = f"(({promoted} + on () {unstreamed}) * on () (1 - {split_expr(pair)}))"
    silent = f"((count({IS_REPLICA}{selector(pair.primary)}) or on () vector(0)) == bool 0)"
    return (
        f"{labelled(replica, pair.replica_host, 'replica')}"
        f" or {labelled(silent, pair.primary_host, 'primary')}"
    )


def runbook(name: str) -> str:
    return f"{RUNBOOK_BASE}{name}.md"


def pair_alerts(pair: Pair) -> list[dict[str, Any]]:
    replica, primary = pair.replica, pair.primary
    return [
        {
            "name": f"{replica}-{LAG}",
            "title": "Replica lags behind its primary",
            "expr": lag_expr(pair),
            "for": "5m",
            "severity": "warning",
            "summary": f"{replica} has not replayed what {primary} wrote {pair.max_lag} ago",
            "runbook": runbook(LAG),
        },
        {
            "name": f"{replica}-{BROKEN}",
            "title": "Replication broken",
            "expr": broken_expr(pair),
            "for": "5m",
            "severity": "critical",
            "summary": (
                f"{replica} does not stream from {primary}: it left recovery, {primary} sees no"
                f" streaming standby {pair.application_name}, or one of them reports nothing"
            ),
            "runbook": runbook(BROKEN),
        },
        {
            "name": f"{replica}-{SPLIT}",
            "title": "Two primaries",
            "expr": labelled(split_expr(pair), pair.replica_host),
            "for": "2m",
            "severity": "critical",
            "summary": f"{primary} and its replica {replica} both accept writes",
            "runbook": runbook(SPLIT),
        },
    ]


def replication_alerts(
    manifest: Manifest, name: str, entry: dict[str, Any]
) -> list[dict[str, Any]]:
    if name not in manifest.active_services:
        return []
    pair = pair_of(manifest, name, entry)
    return pair_alerts(pair) if pair is not None else []
