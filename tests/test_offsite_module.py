from __future__ import annotations

from typing import Any

import pytest

from ayeops.offsite import offsite_context, offsite_problems, quota_bytes, source_dir
from ayeops.portal.graph import build_graph
from ayeops.registry import Module
from ayeops.render import grafana_rules, host_compose
from ayeops.rules import parse_size
from tests.conftest import manifest_from, pinned, render_once, rules_hit, tag_of

URL = "s3:https://s3.eu-central-003.backblazeb2.com/acme-backups/edge"


def offsite(data: dict[str, Any]) -> dict[str, Any]:
    settings: dict[str, Any] = data["hosts"]["edge"]["modules"]["offsite"]
    return settings


def services(data: dict[str, Any], modules: dict[str, Module]) -> dict[str, Any]:
    result: dict[str, Any] = host_compose(manifest_from(data), "edge", modules)["services"]
    return result


def agent_env(data: dict[str, Any], modules: dict[str, Module]) -> dict[str, str]:
    env: dict[str, str] = services(data, modules)["offsite"]["environment"]
    return env


@pytest.mark.parametrize(
    ("setting", "value"),
    [
        ("provider", "borg"),
        ("source", "relative/*.dump"),
        ("source", "/var/backups/"),
        ("target", {"host": "central", "url": URL}),
        ("target", {}),
        ("target", {"url": "https://not-a-restic-repo"}),
        ("target", {"host": "Central"}),
        ("schedule", "5:30"),
        ("schedule", "24:00"),
        ("timezone", "Europe Kyiv"),
        ("keep_daily", -1),
        ("restore_test_days", 0),
        ("restore_test_days", 61),
        ("retry_minutes", 1),
        ("settle_minutes", 2000),
        ("verify", "sha"),
        ("quota", "40"),
        ("region", "EU West"),
        ("unknown", True),
    ],
)
def test_settings_outside_the_schema_are_rejected(
    data: dict[str, Any], setting: str, value: Any
) -> None:
    offsite(data)[setting] = value
    assert rules_hit(data) == {"module-settings"}


def test_source_and_target_are_required(data: dict[str, Any]) -> None:
    del offsite(data)["source"]
    assert rules_hit(data) == {"module-settings"}
    data["hosts"]["edge"]["modules"]["offsite"] = {"source": "/var/backups/acme/*.dump"}
    assert rules_hit(data) == {"module-settings"}


def test_offsite_needs_node_for_its_textfile(data: dict[str, Any]) -> None:
    del data["hosts"]["edge"]["modules"]["node"]
    assert "module-requires" in rules_hit(data)


def test_a_policy_that_keeps_nothing_is_rejected(data: dict[str, Any]) -> None:
    offsite(data).update(keep_daily=0, keep_weekly=0, keep_monthly=0)
    assert rules_hit(data) == {"offsite-retention"}
    offsite(data)["keep_monthly"] = 1
    assert rules_hit(data) == set()


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda d: offsite(d).update(target={"host": "edge"}), "same host"),
        (lambda d: offsite(d).update(target={"host": "nowhere"}), "unknown host"),
        (lambda d: d["hosts"]["central"]["modules"].pop("objectstore"), "runs no objectstore"),
        (
            lambda d: d["hosts"]["central"]["modules"]["objectstore"].update(expose=[]),
            "does not expose 3900",
        ),
    ],
)
def test_a_host_target_must_be_another_host_exposing_its_objectstore(
    data: dict[str, Any], change: Any, message: str
) -> None:
    from ayeops.offsite import offsite_problems

    change(data)
    assert rules_hit(data) == {"offsite-target"}
    problems = offsite_problems(manifest_from(data))
    assert any(message in text for _, _, text in problems)


def test_an_objectstore_in_the_zone_of_the_source_host_is_not_off_site(
    data: dict[str, Any],
) -> None:
    del data["hosts"]["edge"]["modules"]["sentinel"]
    data["hosts"]["edge"]["zone"] = "datacenter"
    assert rules_hit(data) == {"offsite-target"}
    problems = offsite_problems(manifest_from(data))
    assert any("both in zone 'datacenter'" in text for _, _, text in problems)
    del data["hosts"]["central"]["zone"]
    assert rules_hit(data) == set()


def test_quota_and_region_belong_to_the_objectstore_with_a_host_target(
    data: dict[str, Any],
) -> None:
    offsite(data)["quota"] = "10g"
    assert rules_hit(data) == {"offsite-target"}
    offsite(data)["target"] = {"url": URL}
    assert rules_hit(data) == set()


def test_the_source_directory_must_not_hold_a_wildcard(data: dict[str, Any]) -> None:
    offsite(data)["source"] = "/var/backups/*/acme.dump"
    assert rules_hit(data) == {"offsite-source"}
    offsite(data)["source"] = "/acme.dump"
    assert rules_hit(data) == {"offsite-source"}


def test_the_agent_points_restic_at_the_objectstore_bucket_with_a_prefix_per_host(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    env = agent_env(data, modules)
    assert env["RESTIC_REPOSITORY"] == "s3:http://10.8.0.1:3900/acme-offsite/edge"
    assert env["AWS_DEFAULT_REGION"] == "garage"
    assert env["AYE_OFFSITE_HOST"] == "edge"
    assert "AYE_OFFSITE_QUOTA_BYTES" not in env
    assert env["AYE_OFFSITE_SOURCE"] == "/var/backups/acme/*.dump"
    assert (env["AYE_OFFSITE_SCHEDULE"], env["AYE_OFFSITE_TIMEZONE"], env["TZ"]) == (
        "05:30",
        "Europe/Berlin",
        "Europe/Berlin",
    )
    assert [env[f"AYE_OFFSITE_KEEP_{n}"] for n in ("DAILY", "WEEKLY", "MONTHLY")] == ["7", "4", "3"]
    assert env["AYE_OFFSITE_RESTORE_TEST_DAYS"] == "7"
    assert env["AYE_OFFSITE_VERIFY"] == "pg_dump"


def test_a_url_target_is_used_as_is_with_its_own_quota_and_region(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    offsite(data).update(target={"url": URL}, quota="100g", region="eu-central-003")
    env = agent_env(data, modules)
    assert env["RESTIC_REPOSITORY"] == URL
    assert env["AYE_OFFSITE_QUOTA_BYTES"] == str(100 * 1024**3)
    assert env["AWS_DEFAULT_REGION"] == "eu-central-003"
    offsite(data).pop("region")
    offsite(data).pop("quota")
    env = agent_env(data, modules)
    assert "AWS_DEFAULT_REGION" not in env
    assert "AYE_OFFSITE_QUOTA_BYTES" not in env


def test_every_secret_comes_from_a_required_variable(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    env = agent_env(data, modules)
    for name, variable in (
        ("RESTIC_PASSWORD", "AYE_OFFSITE_RESTIC_PASSWORD"),
        ("AWS_ACCESS_KEY_ID", "AYE_OFFSITE_ACCESS_KEY_ID"),
        ("AWS_SECRET_ACCESS_KEY", "AYE_OFFSITE_SECRET_ACCESS_KEY"),
    ):
        assert env[name] == "${" + variable + ":?set " + variable + "}"
    example = render_once(manifest_from(data), modules)["edge/.env.example"]
    offsite_lines = [line for line in example.splitlines() if line.startswith("AYE_OFFSITE_")]
    assert offsite_lines == [
        "AYE_OFFSITE_ACCESS_KEY_ID=replace-me",
        "AYE_OFFSITE_RESTIC_PASSWORD=replace-me",
        "AYE_OFFSITE_SECRET_ACCESS_KEY=replace-me",
    ]


def test_the_agent_reads_the_source_directory_only_and_runs_without_privileges(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    agent = services(data, modules)["offsite"]
    assert "/var/backups/acme:/var/backups/acme:ro" in agent["volumes"]
    assert "offsite-restic:/opt/restic:ro" in agent["volumes"]
    assert "/var/lib/ayeops/textfile:/textfile" in agent["volumes"]
    assert (agent["read_only"], agent["cap_drop"], agent["init"]) == (True, ["ALL"], True)
    assert tag_of(agent["image"]) == pinned("offsite", "python")
    assert agent["mem_limit"] == "512m"
    assert "ports" not in agent
    assert agent["depends_on"] == {"offsite-restic": {"condition": "service_healthy"}}


def test_restic_temp_packs_go_to_the_disk_backed_cache_not_the_small_tmpfs(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    agent = services(data, modules)["offsite"]
    env = agent["environment"]
    assert env["TMPDIR"] == env["RESTIC_CACHE_DIR"] == "/cache"
    assert "offsite-cache:/cache" in agent["volumes"]
    assert not any(mount.startswith("/cache") for mount in agent.get("tmpfs", []))


def test_the_binary_copier_has_room_for_the_restic_binary_it_copies_and_runs(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    copier = services(data, modules)["offsite-restic"]
    restic_binary = parse_size("30m")
    assert parse_size(str(copier["mem_limit"])) >= 2 * restic_binary


def test_the_textfile_lands_in_the_host_dir_node_exporter_reads(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    data["hosts"]["edge"]["modules"]["node"]["textfile_dir"] = "/var/lib/ayeops/textfile"
    agent = services(data, modules)["offsite"]
    assert "/var/lib/ayeops/textfile:/textfile" in agent["volumes"]
    compose = host_compose(manifest_from(data), "edge", modules)
    assert "node-textfile" not in compose["volumes"]


def test_restic_comes_pinned_from_its_official_image_and_stays_running(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    holder = services(data, modules)["offsite-restic"]
    assert tag_of(holder["image"]) == pinned("offsite", "restic/restic")
    command = holder["entrypoint"][-1]
    assert command.startswith("cp /usr/bin/restic /out/.restic.tmp && mv -f ")
    assert command.endswith("exec sleep 2147483647")
    assert holder["network_mode"] == "none"
    assert holder["restart"] == "unless-stopped"
    assert holder["healthcheck"]["test"] == ["CMD", "/out/restic", "version"]
    assert holder["volumes"] == ["offsite-restic:/out"]


def test_the_agent_script_ships_next_to_the_compose_file(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    tree = render_once(manifest_from(data), modules)
    assert "edge/offsite/offsite_agent.py" in tree
    assert tree["edge/offsite/offsite_agent.py"].startswith("from __future__ import annotations")


def offsite_rules(data: dict[str, Any], modules: dict[str, Module]) -> dict[str, Any]:
    groups = grafana_rules(manifest_from(data), modules)["groups"]
    return {r["uid"]: r for g in groups if g["name"] == "offsite" for r in g["rules"]}


def test_offsite_alerts_compare_with_bool_and_staleness_fires_on_silence(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    found = offsite_rules(data, modules)
    assert set(found) == {
        "aye-offsite-stale",
        "aye-offsite-restore-test",
        "aye-offsite-quota",
        "aye-offsite-retention",
    }
    stale = found["aye-offsite-stale"]
    assert stale["labels"]["severity"] == "critical"
    assert stale["noDataState"] == "Alerting"
    assert "ayeops_offsite_max_age_seconds) or " in stale["data"][0]["model"]["expr"]
    assert stale["data"][0]["model"]["expr"].endswith("* 0 + 93600))")
    restore = found["aye-offsite-restore-test"]
    assert restore["noDataState"] == "Alerting"
    assert "> bool 691200" in restore["data"][0]["model"]["expr"]
    assert "ayeops_offsite_restore_test_ok == bool 0" in restore["data"][0]["model"]["expr"]
    for rule in found.values():
        assert " bool " in rule["data"][0]["model"]["expr"]
        assert rule["annotations"]["runbook_url"].endswith(".md")


def test_the_portal_draws_the_agent_writing_into_the_objectstore(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    graph = build_graph(manifest_from(data), modules)
    edges = {(e.src, e.dst, e.label) for e in graph.system}
    assert ("edge/offsite", "central/garage", "restic snapshots") in edges
    assert ("central/garage", "central/vmagent", "scrape") in edges
    assert graph.problems == []
    offsite(data)["target"] = {"url": URL}
    graph = build_graph(manifest_from(data), modules)
    edges = {(e.src, e.dst, e.label) for e in graph.system}
    assert ("edge/offsite", f"url:{URL}", "restic snapshots") in edges
    assert graph.nodes[f"url:{URL}"].sub == "off-site repository"


def test_quota_sizes_are_binary_and_reject_other_units() -> None:
    assert quota_bytes("40g") == 40 * 1024**3
    assert quota_bytes("512m") == 512 * 1024**2
    assert quota_bytes("2T") == 2 * 1024**4
    assert quota_bytes(None) is None
    assert quota_bytes("") is None
    with pytest.raises(ValueError, match="not a quota"):
        quota_bytes("40gb")


def test_source_dir_is_the_directory_of_the_glob() -> None:
    assert source_dir("/var/backups/trofey/*.fc") == "/var/backups/trofey"
    assert source_dir("/acme.dump") == "/"
    assert source_dir("acme.dump") == "/"


SOURCE_MESSAGE = (
    "the directory '/var/backups/*' is mounted read-only into the agent:"
    " name a real directory, the wildcard goes into the file name only"
)


def test_offsite_problems_name_the_exact_path_rule_and_cause(data: dict[str, Any]) -> None:
    offsite(data).update(
        keep_daily=0, keep_weekly=0, keep_monthly=0, source="/var/backups/*/acme.dump"
    )
    assert offsite_problems(manifest_from(data)) == [
        (
            "hosts.edge.modules.offsite",
            "offsite-retention",
            "keep at least one snapshot: a keep_* above 0",
        ),
        ("hosts.edge.modules.offsite.source", "offsite-source", SOURCE_MESSAGE),
    ]


TARGET = "hosts.edge.modules.offsite.target"


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        (
            lambda d: offsite(d).update(target={"host": "edge"}),
            [(TARGET, "offsite-target", "an off-site copy on the same host is not off-site")],
        ),
        (
            lambda d: offsite(d).update(target={"host": "nowhere"}),
            [(TARGET, "offsite-target", "unknown host 'nowhere'")],
        ),
        (
            lambda d: d["hosts"]["central"]["modules"].pop("objectstore"),
            [(TARGET, "offsite-target", "central runs no objectstore")],
        ),
        (
            lambda d: d["hosts"]["central"]["modules"]["objectstore"].update(expose=[]),
            [(TARGET, "offsite-target", "central does not expose 3900")],
        ),
        (
            lambda d: d["hosts"]["central"].pop("address"),
            [(TARGET, "offsite-target", "central has no address")],
        ),
        (
            lambda d: offsite(d).update(region="eu-west-1"),
            [
                (
                    "hosts.edge.modules.offsite",
                    "offsite-target",
                    "quota and region come from the objectstore on central:"
                    " set them only with a url target",
                )
            ],
        ),
    ],
)
def test_target_problems_name_the_exact_cause(
    data: dict[str, Any], change: Any, expected: list[tuple[str, str, str]]
) -> None:
    change(data)
    assert offsite_problems(manifest_from(data)) == expected


def test_context_without_an_objectstore_leaves_the_repository_empty(
    data: dict[str, Any],
) -> None:
    del data["hosts"]["central"]["modules"]["objectstore"]
    manifest = manifest_from(data)
    settings = {"source": "/var/backups/acme/*.dump", "target": {"host": "central"}}
    assert offsite_context(manifest, "edge", settings) == {
        "source_dir": "/var/backups/acme",
        "repository": "",
        "region": "",
        "quota_bytes": None,
        "append_only": False,
        "rest_user": "",
    }
    assert offsite_context(manifest, "edge", None) == {}


def test_a_bad_quota_names_itself() -> None:
    with pytest.raises(ValueError, match=r"^not a quota: '40gb'$"):
        quota_bytes("40gb")
