from __future__ import annotations

import copy
import json
from typing import Any

import pytest
import yaml

from ayeops.backups import backups_context
from ayeops.datastores import (
    Copy,
    apart,
    copies_of,
    datastore_problems,
    datastore_warnings,
    duration_seconds,
    is_database,
    job_rpos,
    offsite_age,
    offsite_max_age,
    spoken,
)
from ayeops.manifest import Manifest
from ayeops.registry import MODULES_DIR, Module
from ayeops.render import compose_file_services, host_compose
from ayeops.rules import Finding, check_warnings
from tests.conftest import manifest_from, rules_hit

URL = "s3:https://s3.eu-central-003.backblazeb2.com/acme-backups/edge"
HOUR = 3600
Problem = tuple[str, str, str]


def db(data: dict[str, Any]) -> dict[str, Any]:
    service: dict[str, Any] = data["services"]["acme-db"]
    return service


def declared(data: dict[str, Any]) -> dict[str, Any]:
    found: dict[str, Any] = db(data)["data"]
    return found


def edge(data: dict[str, Any]) -> dict[str, Any]:
    modules: dict[str, Any] = data["hosts"]["edge"]["modules"]
    return modules


def problems(data: dict[str, Any]) -> list[Problem]:
    return datastore_problems(manifest_from(data))


def rules(data: dict[str, Any]) -> list[str]:
    return [rule for _, rule, _ in problems(data)]


def messages(data: dict[str, Any], rule: str) -> list[str]:
    return [message for _, found, message in problems(data) if found == rule]


def in_utc(data: dict[str, Any]) -> None:
    edge(data)["backups"]["timezone"] = "UTC"
    edge(data)["offsite"]["timezone"] = "UTC"


def append_only_store(data: dict[str, Any]) -> None:
    central = data["hosts"]["central"]["modules"]
    central["objectstore"] = {"provider": "rest-server", "expose": [8000]}


def services(data: dict[str, Any], modules: dict[str, Module]) -> dict[str, Any]:
    result: dict[str, Any] = host_compose(manifest_from(data), "edge", modules)["services"]
    return result


def no_compose(manifest: Manifest, host: str) -> dict[str, dict[str, Any]]:
    return {}


@pytest.mark.parametrize(
    ("text", "seconds"), [("30m", 1800), ("26h", 26 * HOUR), ("2d", 48 * HOUR), ("90m", 5400)]
)
def test_a_duration_is_minutes_hours_or_days(text: str, seconds: int) -> None:
    assert duration_seconds(text) == seconds


@pytest.mark.parametrize("text", ["0h", "26", "1w", "h", " 2h", "2h "])
def test_anything_else_is_not_a_duration(text: str) -> None:
    with pytest.raises(ValueError, match="is not a duration"):
        duration_seconds(text)


def test_seconds_are_spoken_in_whole_hours_or_else_minutes() -> None:
    assert spoken(26 * HOUR) == "26h"
    assert spoken(5400) == "90m"
    assert spoken(HOUR + 60) == "61m"


def test_the_example_declares_its_database_and_passes(data: dict[str, Any]) -> None:
    assert declared(data) == {
        "engine": "postgres",
        "backup": "acme-db",
        "rpo": "26h",
        "rto": "4h",
        "copies": ["host", "offsite"],
    }
    assert problems(data) == []
    assert rules_hit(data) == set()
    assert check_warnings(manifest_from(data)) == []


@pytest.mark.parametrize(
    "value",
    [
        {"engine": "postgres"},
        {"backup": "acme-db"},
        {"engine": "oracle", "backup": "acme-db"},
        {"engine": "postgres", "backup": "Acme"},
        {"engine": "postgres", "backup": "acme-db", "rpo": "26"},
        {"engine": "postgres", "backup": "acme-db", "rto": "1w"},
        {"engine": "postgres", "backup": "acme-db", "copies": []},
        {"engine": "postgres", "backup": "acme-db", "copies": ["host", "host"]},
        {"engine": "postgres", "backup": "acme-db", "copies": ["cloud"]},
        {"engine": "postgres", "backup": "acme-db", "unknown": True},
    ],
)
def test_a_data_block_outside_the_schema_is_rejected(
    data: dict[str, Any], value: dict[str, Any]
) -> None:
    db(data)["data"] = value
    assert rules_hit(data) == {"schema"}


def test_the_backup_job_must_exist_on_the_service_host(data: dict[str, Any]) -> None:
    declared(data)["backup"] = "nightly"
    assert rules_hit(data) == {"data-backup"}
    assert problems(data) == [
        (
            "services.acme-db.data.backup",
            "data-backup",
            "no backups job nightly on edge; its jobs: acme-db",
        )
    ]


def test_a_host_without_the_backups_module_has_no_job_to_name(data: dict[str, Any]) -> None:
    del edge(data)["backups"]
    assert problems(data)[0] == (
        "services.acme-db.data.backup",
        "data-backup",
        "edge runs no backups module: declare the job acme-db in its backups jobs",
    )


def test_a_built_in_dump_counts_as_a_job(data: dict[str, Any]) -> None:
    edge(data)["backups"] = {}
    declared(data)["backup"] = "secrets"
    assert messages(data, "data-backup") == ["no backups job secrets on edge; its jobs: none"]
    edge(data)["secrets"] = {}
    assert messages(data, "data-backup") == []


def test_a_dump_older_than_the_rpo_before_it_counts_as_missing_fails(
    data: dict[str, Any],
) -> None:
    edge(data)["backups"]["jobs"]["acme-db"]["grace"] = "2h"
    assert rules_hit(data) == {"backup-rpo"}
    assert problems(data) == [
        (
            "services.acme-db.data.rpo",
            "backup-rpo",
            "the job acme-db counts as missing only after 27h (the longest gap between its"
            " runs plus grace), longer than the rpo 26h: run it more often or shorten its grace",
        )
    ]


def test_a_daily_off_site_copy_cannot_hold_an_rpo_under_a_day(data: dict[str, Any]) -> None:
    declared(data)["rpo"] = "25h"
    assert messages(data, "backup-rpo") == [
        "the job acme-db counts as missing only after 26h (the longest gap between its runs"
        " plus grace), longer than the rpo 25h: run it more often or shorten its grace",
        "the daily off-site copy of edge at 05:30 can be 26h old (the longest gap between two"
        " copies plus retry_minutes), longer than the rpo 25h",
    ]


def test_an_age_equal_to_the_rpo_passes(data: dict[str, Any]) -> None:
    in_utc(data)
    declared(data)["rpo"] = "25h"
    assert problems(data) == []
    declared(data)["rpo"] = "1499m"
    assert len(messages(data, "backup-rpo")) == 2


def test_the_retry_after_a_failed_copy_counts_towards_the_off_site_age(
    data: dict[str, Any],
) -> None:
    in_utc(data)
    declared(data)["rpo"] = "25h"
    edge(data)["offsite"]["retry_minutes"] = 61
    assert messages(data, "backup-rpo") == [
        "the daily off-site copy of edge at 05:30 can be 1501m old (the longest gap between"
        " two copies plus retry_minutes), longer than the rpo 25h"
    ]


def test_without_offsite_in_copies_only_the_dump_is_held_to_the_rpo(
    data: dict[str, Any],
) -> None:
    edge(data)["backups"]["timezone"] = "UTC"
    declared(data)["rpo"] = "25h"
    declared(data)["copies"] = ["host"]
    assert problems(data) == []


def test_without_an_rpo_nothing_is_measured_against_it(data: dict[str, Any]) -> None:
    del declared(data)["rpo"]
    edge(data)["backups"]["jobs"]["acme-db"]["schedule"] = "30 4 * * 1"
    assert problems(data) == []


def test_a_schedule_that_does_not_parse_is_left_to_backup_schedule(data: dict[str, Any]) -> None:
    edge(data)["backups"]["jobs"]["acme-db"]["schedule"] = "60 4 * * *"
    assert rules_hit(data) == {"backup-schedule"}


def test_the_off_site_age_is_the_longest_daily_gap_plus_one_retry() -> None:
    base = {"schedule": "05:30", "retry_minutes": 60}
    assert offsite_age({**base, "timezone": "UTC"}) == 25 * HOUR
    assert offsite_age({**base, "timezone": "Europe/Berlin"}) == 26 * HOUR
    assert offsite_age({**base, "timezone": "Mars/Base"}) is None
    assert offsite_age({**base, "schedule": "5:x", "timezone": "UTC"}) is None


def test_the_dump_must_land_where_offsite_copies_from(data: dict[str, Any]) -> None:
    edge(data)["offsite"]["source"] = "/var/backups/other/*.dump"
    assert rules_hit(data) == {"backup-placement"}
    assert problems(data) == [
        (
            "services.acme-db.data.copies",
            "backup-placement",
            "offsite on edge copies from /var/backups/other, the job acme-db writes"
            " /var/backups/acme/acme-$(date -u +%Y%m%dT%H%MZ).dump: the off-site copy holds"
            " another file",
        )
    ]


@pytest.mark.parametrize("output", ["/var/backups/other", "/var/backups/other/"])
def test_a_job_writing_a_directory_matches_a_source_inside_it(
    data: dict[str, Any], output: str
) -> None:
    edge(data)["backups"]["jobs"]["acme-db"]["output"] = output
    edge(data)["offsite"]["source"] = "/var/backups/other/*.dump"
    assert problems(data) == []


@pytest.mark.parametrize("output", ["/var/backups/$(hostname)/acme.dump", None])
def test_an_output_folder_aye_cannot_know_is_not_compared(
    data: dict[str, Any], output: str | None
) -> None:
    job = edge(data)["backups"]["jobs"]["acme-db"]
    if output is None:
        del job["output"], job["command"]
    else:
        job["output"] = output
    edge(data)["offsite"]["source"] = "/var/backups/other/*.dump"
    assert problems(data) == []


@pytest.mark.parametrize("source", ["/var/backups/*/acme.dump", "/acme.dump"])
def test_a_source_offsite_source_refuses_is_left_to_that_rule(
    data: dict[str, Any], source: str
) -> None:
    edge(data)["offsite"]["source"] = source
    assert problems(data) == []


def test_without_offsite_in_copies_the_host_needs_no_offsite_module(
    data: dict[str, Any],
) -> None:
    del edge(data)["offsite"]
    declared(data)["copies"] = ["host"]
    assert problems(data) == []


def test_copies_default_to_the_dump_on_the_host() -> None:
    assert copies_of({}) == ["host"]
    assert copies_of({"copies": ["offsite"]}) == ["offsite"]


def test_a_service_on_an_unknown_host_is_left_to_service_provided(data: dict[str, Any]) -> None:
    db(data)["host"] = "nowhere"
    assert problems(data) == []
    assert job_rpos(manifest_from(data), "nowhere") == {}


def test_offsite_in_copies_needs_the_offsite_module(data: dict[str, Any]) -> None:
    del edge(data)["offsite"]
    assert problems(data) == [
        (
            "services.acme-db.data.copies",
            "backup-placement",
            "edge runs no offsite module, so no copy of the dump leaves the host",
        )
    ]


def test_a_critical_service_needs_an_append_only_copy(data: dict[str, Any]) -> None:
    db(data)["criticality"] = "critical"
    assert problems(data) == [
        (
            "services.acme-db.data.copies",
            "backup-placement",
            "no copy is append-only, so whoever takes over edge can delete them all: copy"
            " off-site to an objectstore with provider rest-server, or to a url target with"
            " append_only: true",
        )
    ]
    append_only_store(data)
    assert problems(data) == []


def test_a_critical_service_needs_two_copies_in_different_zones(data: dict[str, Any]) -> None:
    db(data)["criticality"] = "critical"
    append_only_store(data)
    declared(data)["copies"] = ["host"]
    assert messages(data, "backup-placement") == [
        "a critical service needs at least two copies of its dump in different zones,"
        " the host and offsite",
        "no copy is append-only, so whoever takes over edge can delete them all: copy off-site"
        " to an objectstore with provider rest-server, or to a url target with append_only:"
        " true",
    ]


def test_copies_in_one_zone_do_not_stand_apart(data: dict[str, Any]) -> None:
    db(data)["criticality"] = "critical"
    append_only_store(data)
    data["hosts"]["central"]["zone"] = "office"
    assert messages(data, "backup-placement") == ["every copy of the dump stands in zone 'office'"]


def test_a_copy_without_a_zone_cannot_be_told_apart(data: dict[str, Any]) -> None:
    db(data)["criticality"] = "critical"
    append_only_store(data)
    del data["hosts"]["edge"]["zone"]
    assert messages(data, "backup-placement") == [
        "name the zone of edge, so aye check can tell the copies stand apart"
    ]
    del data["hosts"]["central"]["zone"]
    assert messages(data, "backup-placement") == [
        "name the zone of central, edge, so aye check can tell the copies stand apart"
    ]


def test_copies_with_the_provider_of_the_service_host_share_its_fate(
    data: dict[str, Any],
) -> None:
    db(data)["criticality"] = "critical"
    append_only_store(data)
    data["hosts"]["edge"]["provider"] = "example-cloud"
    assert messages(data, "backup-placement") == [
        "every copy stands with provider 'example-cloud', like edge: one account or one outage"
        " takes them all; keep one with another provider"
    ]
    data["hosts"]["central"]["provider"] = "other-cloud"
    assert problems(data) == []
    del data["hosts"]["central"]["provider"]
    assert problems(data) == []


def test_a_url_target_stands_outside_the_fleet(data: dict[str, Any]) -> None:
    db(data)["criticality"] = "critical"
    data["hosts"]["edge"]["provider"] = "example-cloud"
    del data["hosts"]["edge"]["zone"]
    edge(data)["offsite"]["target"] = {"url": URL, "append_only": True}
    assert problems(data) == []
    edge(data)["offsite"]["target"] = {"url": URL}
    assert [message[:26] for message in messages(data, "backup-placement")] == [
        "no copy is append-only, so"
    ]


def test_placement_is_held_only_against_critical_services(data: dict[str, Any]) -> None:
    declared(data)["copies"] = ["host"]
    for level in ("high", "normal", "low"):
        db(data)["criticality"] = level
        assert problems(data) == []


def test_two_copies_stand_apart_by_zone_or_by_leaving_the_fleet() -> None:
    office = Copy("host", "edge", "office", None)
    datacenter = Copy("offsite", "central", "datacenter", None)
    unknown = Copy("offsite", "central", None, None)
    outside = Copy("offsite", URL, None, None, True)
    assert apart(office, datacenter)
    assert not apart(office, office)
    assert not apart(office, unknown)
    assert not apart(unknown, office)
    assert apart(office, outside)
    assert apart(outside, office)
    assert not apart(outside, outside)


def drilled(data: dict[str, Any], max_minutes: int) -> None:
    append_only_store(data)
    edge(data)["offsite"]["restore_drill"] = {
        "engine": "postgres",
        "version": 18,
        "max_minutes": max_minutes,
        "checks": [{"name": "orders", "query": "select count(*) as value from orders"}],
    }


def test_a_drill_allowed_to_run_longer_than_the_rto_fails_backup_rto(
    data: dict[str, Any],
) -> None:
    drilled(data, 300)
    assert problems(data) == [
        (
            "services.acme-db.data.rto",
            "backup-rto",
            "the restore drill on central may run 300 minutes before it fails, longer than the"
            " rto 4h: set its max_minutes to at most 240, so a restore slower than the rto"
            " fails the drill",
        )
    ]
    edge(data)["offsite"]["restore_drill"]["max_minutes"] = 240
    assert problems(data) == []


def test_without_a_drill_or_an_rto_the_rto_is_documentation(data: dict[str, Any]) -> None:
    declared(data)["rto"] = "5m"
    assert problems(data) == []
    drilled(data, 300)
    del declared(data)["rto"]
    assert problems(data) == []


def test_a_garage_store_runs_no_drill_to_hold_against_the_rto(data: dict[str, Any]) -> None:
    drilled(data, 300)
    data["hosts"]["central"]["modules"]["objectstore"] = {"expose": [3900]}
    assert messages(data, "backup-rto") == []


def test_reserved_services_and_services_without_data_are_not_checked(
    data: dict[str, Any],
) -> None:
    declared(data)["backup"] = "nightly"
    db(data)["state"] = "planned"
    assert problems(data) == []
    db(data)["state"] = "live"
    del db(data)["data"]
    assert problems(data) == []


def test_the_rpo_of_a_job_is_the_tightest_its_services_declare(data: dict[str, Any]) -> None:
    manifest = manifest_from(data)
    assert job_rpos(manifest, "edge") == {"acme-db": 26 * HOUR}
    assert job_rpos(manifest, "central") == {}
    twin = copy.deepcopy(db(data))
    twin["data"]["rpo"] = "30h"
    data["services"]["acme-db-twin"] = twin
    declared(data)["rpo"] = "1d"
    assert job_rpos(manifest_from(data), "edge") == {"acme-db": 24 * HOUR}
    del declared(data)["rpo"]
    assert job_rpos(manifest_from(data), "edge") == {"acme-db": 30 * HOUR}


def test_the_off_site_threshold_drops_to_a_shorter_rpo(data: dict[str, Any]) -> None:
    assert offsite_max_age(manifest_from(data), "edge") == 26 * HOUR
    assert offsite_max_age(manifest_from(data), "central") == 26 * HOUR
    declared(data)["rpo"] = "25h"
    assert offsite_max_age(manifest_from(data), "edge") == 25 * HOUR
    declared(data)["rpo"] = "27h"
    assert offsite_max_age(manifest_from(data), "edge") == 26 * HOUR
    declared(data)["rpo"] = "25h"
    declared(data)["copies"] = ["host"]
    assert offsite_max_age(manifest_from(data), "edge") == 26 * HOUR


def test_the_missing_threshold_of_a_job_drops_to_a_shorter_rpo() -> None:
    settings = {"backups": {"jobs": {"a-db": {"schedule": "0 4 * * *", "grace": "2h"}}}}
    assert backups_context(settings, {"a-db": 20 * HOUR}) == {
        "ages": {"a-db": 20 * HOUR},
        "jobs": f"a-db={20 * HOUR}",
        "rpo": f"a-db={20 * HOUR}",
    }
    assert backups_context(settings, {"a-db": 30 * HOUR, "gone": 60})["jobs"] == f"a-db={26 * HOUR}"
    assert backups_context(settings, {"a-db": 30 * HOUR, "gone": 60})["rpo"] == f"a-db={30 * HOUR}"
    assert backups_context(settings)["rpo"] == ""


def test_the_rendered_agents_carry_the_thresholds(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    declared(data)["rpo"] = "25h"
    found = services(data, modules)
    assert found["offsite"]["environment"]["AYE_OFFSITE_MAX_AGE_SECONDS"] == str(25 * HOUR)
    assert found["backup-jobs"]["environment"]["AYE_BACKUP_JOBS"] == f"acme-db={25 * HOUR}"
    assert found["backup-jobs"]["environment"]["AYE_BACKUP_RPO"] == f"acme-db={25 * HOUR}"


def test_offsite_stale_reads_the_threshold_and_falls_back_to_26_hours() -> None:
    spec = yaml.safe_load((MODULES_DIR / "offsite" / "module.yaml").read_text(encoding="utf-8"))
    (alert,) = [alert for alert in spec["alerts"] if alert["name"] == "offsite-stale"]
    assert "> bool on (host, project, group)" in alert["expr"]
    assert "max by (host, project, group) (ayeops_offsite_max_age_seconds) or " in alert["expr"]
    assert alert["expr"].endswith("* 0 + 93600)")


def test_the_reliability_board_shows_the_rpo_next_to_the_dump_age() -> None:
    board = json.loads(
        (MODULES_DIR / "grafana" / "dashboards" / "reliability.json").read_text(encoding="utf-8")
    )
    (panel,) = [panel for panel in board["panels"] if panel["id"] == 17]
    assert panel["ayeopsRequires"] == ["backups"]
    exprs = [target["expr"] for target in panel["targets"]]
    assert all("ayeops_backup_rpo_seconds" in expr for expr in exprs)
    assert "ayeops_backup_last_success_timestamp_seconds" in exprs[0]


@pytest.mark.parametrize(
    "reference",
    [
        "postgres:18.1-alpine",
        "docker.io/library/mariadb:11.4",
        "bitnami/postgresql:16@sha256:0123",
        "mongo:8",
        "MySQL:8",
        "registry.example:5000/team/postgres",
        "postgresql@16-main.service",
        "mysql.service",
        "mongod.service",
    ],
)
def test_a_database_is_told_by_its_image_or_unit(reference: str) -> None:
    assert is_database(reference)


@pytest.mark.parametrize(
    "reference",
    [
        "nginx:1.29",
        "postgrest/postgrest:12",
        "prometheuscommunity/postgres-exporter:0.17",
        "",
        None,
    ],
)
def test_other_images_are_no_database(reference: str | None) -> None:
    assert not is_database(reference)


def test_a_critical_database_without_data_warns(data: dict[str, Any]) -> None:
    del db(data)["data"]
    db(data)["criticality"] = "critical"
    manifest = manifest_from(data)
    expected = (
        "services.acme-db",
        "data-declared",
        "a critical service on the unit postgresql.service keeps data, and has no data: declare"
        " its engine, backup job, rpo and copies so aye check can hold the backups to them",
    )
    assert datastore_warnings(manifest, no_compose) == [expected]
    assert check_warnings(manifest) == [Finding(*expected, severity="warning")]
    assert rules_hit(data) == set()


def test_a_critical_service_on_a_database_image_warns(data: dict[str, Any]) -> None:
    def images(manifest: Manifest, host: str) -> dict[str, dict[str, Any]]:
        return {"web": {"image": "postgres:18.1"}} if host == "central" else {}

    assert datastore_warnings(manifest_from(data), images) == [
        (
            "services.web",
            "data-declared",
            "a critical service on the image postgres:18.1 keeps data, and has no data: declare"
            " its engine, backup job, rpo and copies so aye check can hold the backups to them",
        )
    ]
    data["services"]["web"]["criticality"] = "high"
    assert datastore_warnings(manifest_from(data), images) == []


def test_the_example_web_service_runs_nginx_and_does_not_warn(data: dict[str, Any]) -> None:
    assert datastore_warnings(manifest_from(data), compose_file_services) == []


def test_a_compose_file_that_does_not_parse_warns_nothing(data: dict[str, Any]) -> None:
    def broken(manifest: Manifest, host: str) -> dict[str, dict[str, Any]]:
        raise yaml.YAMLError("bad")

    assert datastore_warnings(manifest_from(data), broken) == []


def test_a_declared_or_reserved_database_does_not_warn(data: dict[str, Any]) -> None:
    db(data)["criticality"] = "critical"
    assert datastore_warnings(manifest_from(data), no_compose) == []
    del db(data)["data"]
    db(data)["state"] = "disabled"
    assert datastore_warnings(manifest_from(data), no_compose) == []
