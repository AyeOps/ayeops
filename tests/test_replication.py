from __future__ import annotations

from typing import Any

import pytest

from ayeops.registry import Module
from ayeops.render import HOSTS_DOWN, MODULE_RUNBOOK_BASE, grafana_rules
from ayeops.replication import (
    BROKEN,
    LAG,
    PRIMARY_METRICS,
    REPLICA_METRICS,
    RUNBOOK_BASE,
    RUNBOOKS,
    SPLIT,
    Pair,
    broken_expr,
    declared_metrics,
    engine_of,
    labelled,
    lag_expr,
    pair_alerts,
    pair_of,
    pairs,
    replication_alerts,
    replication_problems,
    selector,
    spec_of,
    split_expr,
    stream,
    writable,
)
from tests.conftest import ROOT, manifest_from, rules_hit
from tests.test_dashboards import exporter_metrics, metric_names

Problem = tuple[str, str, str]
PAIR = Pair("acme-db-replica", "central", "acme-db", "edge", "10m", "acme-db-replica")
OTHER = Pair("orders-ro", "box", "orders", "db", "5m", "ro")
PRIMARY_UP = '(max(pg_replication_is_replica{service="acme-db"}) == bool 0)'
REPLICA_UP = '(max(pg_replication_is_replica{service="acme-db-replica"}) == bool 0)'
STREAM = '{service="acme-db",application_name="acme-db-replica"}'
SPLIT_EXPR = f"(({PRIMARY_UP} * on () {REPLICA_UP}) or on () vector(0))"
LAG_EXPR = (
    "label_replace(((max(pg_stat_replication_pg_wal_lsn_diff" + STREAM + ") > bool"
    " (max(pg_stat_replication_pg_current_wal_lsn_bytes" + STREAM + ") -"
    " min(min_over_time(pg_stat_replication_pg_current_wal_lsn_bytes" + STREAM + "[10m]))))"
    ' or on () vector(0)), "host", "central", "", "")'
)
BROKEN_EXPR = (
    f"label_replace(label_replace(((({REPLICA_UP} or on () vector(1)) + on () (({PRIMARY_UP}"
    " * on () ((count(pg_stat_replication_pg_current_wal_lsn_bytes"
    '{service="acme-db",application_name="acme-db-replica",state="streaming"})'
    " or on () vector(0)) == bool 0)) or on () vector(0))) * on () (1 - "
    f'{SPLIT_EXPR})), "host", "central", "", ""), "side", "replica", "", "") or'
    ' label_replace(label_replace(((count(pg_replication_is_replica{service="acme-db"})'
    ' or on () vector(0)) == bool 0), "host", "edge", "", ""), "side", "primary", "", "")'
)


def services(data: dict[str, Any]) -> dict[str, Any]:
    found: dict[str, Any] = data["services"]
    return found


def replica(data: dict[str, Any]) -> dict[str, Any]:
    found: dict[str, Any] = services(data)["acme-db-replica"]
    return found


def problems(data: dict[str, Any]) -> list[Problem]:
    return replication_problems(manifest_from(data))


def second_replica(data: dict[str, Any], name: str, **spec: Any) -> dict[str, Any]:
    entry = {
        **replica(data),
        "replication": {"primary": "acme-db", **spec},
        "description": f"Another replica, {name}.",
    }
    data["hosts"]["central"]["units"][name] = "postgresql.service"
    services(data)[name] = entry
    return entry


def test_the_example_declares_a_replica_of_its_database_and_passes(data: dict[str, Any]) -> None:
    assert replica(data)["replication"] == {"primary": "acme-db", "max_lag": "10m"}
    assert problems(data) == []
    assert pairs(manifest_from(data)) == [PAIR]
    assert not {rule for rule in rules_hit(data) if rule.startswith("replication")}


def test_the_alerts_need_only_the_series_the_rule_asks_for() -> None:
    assert REPLICA_METRICS == ("pg_replication_is_replica",)
    assert PRIMARY_METRICS == (
        "pg_replication_is_replica",
        "pg_stat_replication_pg_current_wal_lsn_bytes",
        "pg_stat_replication_pg_wal_lsn_diff",
    )


def test_every_series_the_alerts_read_is_one_postgres_exporter_produces() -> None:
    produced = exporter_metrics()["postgres"]
    read = set()
    for alert in pair_alerts(PAIR):
        read |= metric_names(alert["expr"])
    assert read == set(PRIMARY_METRICS) | set(REPLICA_METRICS)
    assert read <= produced


def test_a_replica_names_a_primary_that_exists(data: dict[str, Any]) -> None:
    replica(data)["replication"]["primary"] = "acme-dbx"
    assert problems(data) == [
        (
            "services.acme-db-replica.replication.primary",
            "replication-primary",
            "no service acme-dbx: name the database service the replica streams from",
        )
    ]
    assert pairs(manifest_from(data)) == []
    assert "replication-primary" in rules_hit(data)


def test_a_replica_cannot_stream_from_itself(data: dict[str, Any]) -> None:
    replica(data)["replication"]["primary"] = "acme-db-replica"
    assert problems(data) == [
        (
            "services.acme-db-replica.replication.primary",
            "replication-primary",
            "a service cannot be the replica of itself",
        )
    ]


@pytest.mark.parametrize("state", ["planned", "disabled"])
def test_a_live_replica_needs_a_live_primary(data: dict[str, Any], state: str) -> None:
    services(data)["acme-db"]["state"] = state
    assert problems(data) == [
        (
            "services.acme-db-replica.replication.primary",
            "replication-primary",
            f"the primary acme-db is {state}: a live replica needs a live primary",
        )
    ]
    assert replication_alerts(manifest_from(data), "acme-db-replica", replica(data)) == []


def test_cascading_replication_is_refused_and_so_is_a_cycle(data: dict[str, Any]) -> None:
    services(data)["acme-db"]["replication"] = {"primary": "acme-db-replica"}
    assert problems(data) == [
        (
            "services.acme-db.replication.primary",
            "replication-primary",
            "the primary acme-db-replica is itself a replica of acme-db: cascading replication"
            " is not supported, name the primary that is not in recovery",
        ),
        (
            "services.acme-db-replica.replication.primary",
            "replication-primary",
            "the primary acme-db is itself a replica of acme-db-replica: cascading replication"
            " is not supported, name the primary that is not in recovery",
        ),
    ]
    assert pairs(manifest_from(data)) == []


def test_a_planned_replica_is_not_checked_and_gets_no_alerts(data: dict[str, Any]) -> None:
    replica(data)["state"] = "planned"
    replica(data)["replication"]["primary"] = "acme-dbx"
    assert problems(data) == []
    replica(data)["replication"]["primary"] = "acme-db"
    manifest = manifest_from(data)
    assert pairs(manifest) == []
    assert replication_alerts(manifest, "acme-db-replica", replica(data)) == []


def test_the_primary_must_declare_its_engine(data: dict[str, Any]) -> None:
    del services(data)["acme-db"]["data"]
    assert problems(data) == [
        (
            "services.acme-db.data",
            "replication-engine",
            "acme-db is the primary of acme-db-replica and declares no data, so aye check cannot"
            " tell its engine: declare data with engine postgres",
        )
    ]


def test_replication_is_watched_for_postgres_only(data: dict[str, Any]) -> None:
    services(data)["acme-db"]["data"]["engine"] = "mysql"
    replica(data)["data"] = {"engine": "mariadb", "backup": "acme-db"}
    assert [found for found in problems(data) if found[1] == "replication-engine"] == [
        (
            "services.acme-db.data.engine",
            "replication-engine",
            "acme-db has engine mysql: replication is watched for postgres only",
        ),
        (
            "services.acme-db-replica.data.engine",
            "replication-engine",
            "acme-db-replica has engine mariadb: replication is watched for postgres only",
        ),
    ]


def test_a_replica_of_another_engine_than_its_primary_is_refused(data: dict[str, Any]) -> None:
    replica(data)["data"] = {"engine": "redis", "backup": "acme-db"}
    assert [found for found in problems(data) if found[1] == "replication-engine"] == [
        (
            "services.acme-db-replica.data.engine",
            "replication-engine",
            "acme-db-replica has engine redis: replication is watched for postgres only",
        )
    ]


def test_a_replica_with_its_own_postgres_data_passes(data: dict[str, Any]) -> None:
    replica(data)["data"] = {"engine": "postgres", "backup": "acme-db"}
    assert [found for found in problems(data) if found[1] == "replication-engine"] == []


def test_the_replica_lists_the_series_its_alerts_read(data: dict[str, Any]) -> None:
    replica(data)["metrics"] = ["pg_up"]
    assert problems(data) == [
        (
            "services.acme-db-replica.metrics",
            "replication-metrics",
            "the replication alerts of acme-db-replica read pg_replication_is_replica of"
            " acme-db-replica: scrape its postgres_exporter with the label"
            " service=acme-db-replica and list them in metrics",
        )
    ]
    assert "replication-metrics" in rules_hit(data)


def test_the_primary_lists_the_series_its_alerts_read(data: dict[str, Any]) -> None:
    services(data)["acme-db"]["metrics"] = [
        "pg_stat_replication_pg_wal_lsn_diff",
        "ayeops_backup_last_size_bytes",
    ]
    assert problems(data) == [
        (
            "services.acme-db.metrics",
            "replication-metrics",
            "the replication alerts of acme-db-replica read pg_replication_is_replica,"
            " pg_stat_replication_pg_current_wal_lsn_bytes of acme-db: scrape its"
            " postgres_exporter with the label service=acme-db and list them in metrics",
        )
    ]


def test_metrics_none_lacks_every_series(data: dict[str, Any]) -> None:
    replica(data)["metrics"] = {"none": "no exporter yet"}
    services(data)["acme-db"]["metrics"] = {"none": "no exporter yet"}
    assert [path for path, _, _ in problems(data)] == [
        "services.acme-db-replica.metrics",
        "services.acme-db.metrics",
    ]


def test_metrics_are_read_from_a_list_only() -> None:
    assert declared_metrics({"metrics": ["a", "b"]}) == ["a", "b"]
    assert declared_metrics({"metrics": {"none": "why"}}) == []
    assert declared_metrics({}) == []


def test_two_replicas_of_one_primary_need_two_application_names(data: dict[str, Any]) -> None:
    second_replica(data, "acme-db-ro", application_name="acme-db-replica")
    found = [problem for problem in problems(data) if problem[1] == "replication-name"]
    assert found == [
        (
            "services.acme-db-replica.replication.application_name",
            "replication-name",
            "acme-db-replica and acme-db-ro stream from acme-db as application_name"
            " acme-db-replica: give each replica its own, in primary_conninfo and here",
        ),
        (
            "services.acme-db-ro.replication.application_name",
            "replication-name",
            "acme-db-ro and acme-db-replica stream from acme-db as application_name"
            " acme-db-replica: give each replica its own, in primary_conninfo and here",
        ),
    ]
    assert "replication-name" in rules_hit(data)


def test_three_replicas_under_one_name_each_name_the_other_two(data: dict[str, Any]) -> None:
    replica(data)["replication"]["application_name"] = "walreceiver"
    second_replica(data, "acme-db-ro", application_name="walreceiver")
    second_replica(data, "acme-db-bi", application_name="walreceiver")
    found = [message for _, rule, message in problems(data) if rule == "replication-name"]
    assert [message.split(" stream from ")[0] for message in found] == [
        "acme-db-bi and acme-db-replica, acme-db-ro",
        "acme-db-replica and acme-db-bi, acme-db-ro",
        "acme-db-ro and acme-db-bi, acme-db-replica",
    ]


def test_replicas_with_their_own_names_pass(data: dict[str, Any]) -> None:
    second_replica(data, "acme-db-ro")
    assert problems(data) == []
    assert [pair.replica for pair in pairs(manifest_from(data))] == [
        "acme-db-replica",
        "acme-db-ro",
    ]


def test_problems_and_pairs_come_in_service_order(data: dict[str, Any]) -> None:
    second_replica(data, "acme-db-aa", primary="nowhere")
    second_replica(data, "acme-db-ab", primary="nowhere")
    second_replica(data, "acme-db-ac")
    data["services"] = dict(reversed(list(services(data).items())))
    assert [path for path, _, _ in problems(data)] == [
        "services.acme-db-aa.replication.primary",
        "services.acme-db-ab.replication.primary",
    ]
    assert [pair.replica for pair in pairs(manifest_from(data))] == [
        "acme-db-ac",
        "acme-db-replica",
    ]


def test_a_problem_on_the_primary_hides_the_engine_and_metric_problems(
    data: dict[str, Any],
) -> None:
    replica(data)["replication"]["primary"] = "web"
    replica(data)["metrics"] = []
    assert [path for path, _, _ in problems(data)] == [
        "services.web.data",
        "services.acme-db-replica.metrics",
        "services.web.metrics",
    ]
    replica(data)["replication"]["primary"] = "nowhere"
    assert [rule for _, rule, _ in problems(data)] == ["replication-primary"]


def test_the_pair_defaults_to_five_minutes_and_the_replica_name(data: dict[str, Any]) -> None:
    del replica(data)["replication"]["max_lag"]
    pair = pair_of(manifest_from(data), "acme-db-replica", replica(data))
    assert pair == Pair("acme-db-replica", "central", "acme-db", "edge", "5m", "acme-db-replica")


def test_the_pair_takes_max_lag_and_application_name(data: dict[str, Any]) -> None:
    replica(data)["replication"].update(max_lag="2h", application_name="walreceiver")
    pair = pair_of(manifest_from(data), "acme-db-replica", replica(data))
    assert pair == Pair("acme-db-replica", "central", "acme-db", "edge", "2h", "walreceiver")


def test_a_service_without_replication_is_no_pair(data: dict[str, Any]) -> None:
    manifest = manifest_from(data)
    assert pair_of(manifest, "acme-db", services(data)["acme-db"]) is None
    assert replication_alerts(manifest, "acme-db", services(data)["acme-db"]) == []


def test_only_a_mapping_is_a_replication_block() -> None:
    assert spec_of({"replication": {"primary": "db"}}) == {"primary": "db"}
    assert spec_of({"replication": "db"}) is None
    assert spec_of({}) is None


def test_the_engine_is_read_from_data() -> None:
    assert engine_of({"data": {"engine": "postgres", "backup": "db"}}) == "postgres"
    assert engine_of({"data": {"backup": "db"}}) is None
    assert engine_of({"data": "postgres"}) is None
    assert engine_of({}) is None


def test_series_are_picked_by_the_service_label() -> None:
    assert selector("acme-db") == '{service="acme-db"}'
    assert selector("acme-db", 'state="streaming"') == '{service="acme-db",state="streaming"}'
    assert stream(OTHER) == '{service="orders",application_name="ro"}'
    assert stream(OTHER, 'a="b"') == '{service="orders",application_name="ro",a="b"}'
    assert writable("orders") == '(max(pg_replication_is_replica{service="orders"}) == bool 0)'


def test_labels_carry_the_host_of_the_manifest_and_the_side_when_given() -> None:
    assert labelled("x", "box") == 'label_replace(x, "host", "box", "", "")'
    assert labelled("x", "box", "primary") == (
        'label_replace(label_replace(x, "host", "box", "", ""), "side", "primary", "", "")'
    )


def test_the_lag_compares_the_unreplayed_bytes_with_the_wal_of_max_lag() -> None:
    assert lag_expr(PAIR) == LAG_EXPR


def test_two_primaries_multiplies_both_writable_flags() -> None:
    assert split_expr(PAIR) == SPLIT_EXPR


def test_broken_covers_both_sides_and_leaves_two_primaries_to_its_own_rule() -> None:
    assert broken_expr(PAIR) == BROKEN_EXPR


def test_a_pair_gets_three_alerts() -> None:
    assert pair_alerts(PAIR) == [
        {
            "name": "acme-db-replica-replication-lag",
            "title": "Replica lags behind its primary",
            "expr": LAG_EXPR,
            "for": "5m",
            "severity": "warning",
            "summary": "acme-db-replica has not replayed what acme-db wrote 10m ago",
            "runbook": f"{MODULE_RUNBOOK_BASE}replication-lag.md",
        },
        {
            "name": "acme-db-replica-replication-broken",
            "title": "Replication broken",
            "expr": BROKEN_EXPR,
            "for": "5m",
            "severity": "critical",
            "summary": (
                "acme-db-replica does not stream from acme-db: it left recovery, acme-db sees"
                " no streaming standby acme-db-replica, or one of them reports nothing"
            ),
            "runbook": f"{MODULE_RUNBOOK_BASE}replication-broken.md",
        },
        {
            "name": "acme-db-replica-replication-two-primaries",
            "title": "Two primaries",
            "expr": f'label_replace({SPLIT_EXPR}, "host", "central", "", "")',
            "for": "2m",
            "severity": "critical",
            "summary": "acme-db and its replica acme-db-replica both accept writes",
            "runbook": f"{MODULE_RUNBOOK_BASE}replication-two-primaries.md",
        },
    ]


def test_the_alerts_of_another_pair_name_its_services_hosts_and_stream() -> None:
    alerts = pair_alerts(OTHER)
    assert [alert["name"] for alert in alerts] == [
        "orders-ro-replication-lag",
        "orders-ro-replication-broken",
        "orders-ro-replication-two-primaries",
    ]
    assert "[5m]" in alerts[0]["expr"]
    assert '"host", "box"' in alerts[0]["expr"]
    assert alerts[0]["summary"] == "orders-ro has not replayed what orders wrote 5m ago"
    assert '{service="orders",application_name="ro",state="streaming"}' in alerts[1]["expr"]
    assert '"host", "db", "", ""), "side", "primary"' in alerts[1]["expr"]
    assert "standby ro, or" in alerts[1]["summary"]
    assert alerts[2]["summary"] == "orders and its replica orders-ro both accept writes"


def test_the_runbooks_are_pages_of_this_repository() -> None:
    assert RUNBOOK_BASE == MODULE_RUNBOOK_BASE
    assert (LAG, BROKEN, SPLIT) == (
        "replication-lag",
        "replication-broken",
        "replication-two-primaries",
    )
    assert list(RUNBOOKS) == [
        f"{MODULE_RUNBOOK_BASE}replication-lag.md",
        f"{MODULE_RUNBOOK_BASE}replication-broken.md",
        f"{MODULE_RUNBOOK_BASE}replication-two-primaries.md",
    ]
    for name in (LAG, BROKEN, SPLIT):
        assert (ROOT / "docs" / "runbooks" / f"{name}.md").is_file()


def test_the_rendered_rules_hold_back_and_label_the_replica_alerts(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    groups = grafana_rules(manifest_from(data), modules)["groups"]
    rules = {rule["uid"]: rule for group in groups for rule in group["rules"]}
    lag = rules["aye-acme-db-replica-replication-lag"]
    assert lag["labels"]["service"] == "acme-db-replica"
    assert lag["labels"]["severity"] == "warning"
    assert lag["for"] == "5m"
    assert lag["annotations"]["runbook_url"] == f"{MODULE_RUNBOOK_BASE}replication-lag.md"
    expr = lag["data"][0]["model"]["expr"]
    assert expr == f"({LAG_EXPR}) unless on (host) ({HOSTS_DOWN}) or 0 * ({LAG_EXPR})"
    assert rules["aye-acme-db-replica-replication-broken"]["labels"]["severity"] == "critical"
    split = [rule for rule in rules.values() if rule["title"] == "Two primaries"]
    assert [rule["for"] for rule in split] == ["2m"]


def test_a_replica_counts_its_generated_alerts_as_its_own(data: dict[str, Any]) -> None:
    assert "alerts" not in replica(data)
    assert "service-alerts" not in rules_hit(data)
    del replica(data)["replication"]
    assert "service-alerts" in rules_hit(data)


def test_a_service_alert_cannot_take_a_generated_name(data: dict[str, Any]) -> None:
    services(data)["web"]["alerts"][0]["name"] = "acme-db-replica-replication-lag"
    assert "alert-unique" in rules_hit(data)


@pytest.mark.parametrize(
    "spec",
    [
        {},
        {"primary": "acme-db", "max_lag": "30s"},
        {"primary": "acme-db", "application_name": "with space"},
        {"primary": "acme-db", "application_name": "x" * 64},
        {"primary": "acme-db", "slot": "a"},
    ],
)
def test_a_replication_block_outside_the_schema_is_rejected(
    data: dict[str, Any], spec: dict[str, Any]
) -> None:
    replica(data)["replication"] = spec
    assert "schema" in rules_hit(data)


def test_a_replication_block_within_the_schema_passes(data: dict[str, Any]) -> None:
    replica(data)["replication"] = {
        "primary": "acme-db",
        "max_lag": "1d",
        "application_name": "Standby_1.a-" + "x" * 51,
    }
    assert "schema" not in rules_hit(data)
