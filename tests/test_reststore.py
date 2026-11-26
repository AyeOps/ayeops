from __future__ import annotations

import datetime as dt
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

from ayeops.offsite import (
    objectstore_problems,
    offsite_context,
    rest_server_script,
    reststore_context,
    secret_env,
    store_service,
)
from ayeops.portal.graph import build_graph
from ayeops.registry import Module
from ayeops.render import grafana_rules, host_compose, host_scrape
from tests.conftest import ROOT, manifest_from, pinned, render_once, rules_hit, tag_of

ADMIN = ROOT / "src" / "ayeops" / "modules" / "objectstore" / "files" / "reststore_admin.py"
GIB = 1024**3
UTC = dt.UTC


def load_admin() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "ayeops.modules.objectstore.files.reststore_admin", ADMIN
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


admin = load_admin()


@pytest.fixture
def rest(data: dict[str, Any]) -> dict[str, Any]:
    data["hosts"]["central"]["modules"]["objectstore"] = {
        "provider": "rest-server",
        "quota": "40g",
        "expose": [8000],
    }
    return data


def central(data: dict[str, Any], modules: dict[str, Module]) -> dict[str, Any]:
    result: dict[str, Any] = host_compose(manifest_from(data), "central", modules)["services"]
    return result


def edge_env(data: dict[str, Any], modules: dict[str, Module]) -> dict[str, str]:
    services = host_compose(manifest_from(data), "edge", modules)["services"]
    env: dict[str, str] = services["offsite"]["environment"]
    return env


def test_a_rest_server_store_passes_every_rule(rest: dict[str, Any]) -> None:
    assert rules_hit(rest) == set()


def test_rest_server_runs_append_only_with_private_repos_and_a_pinned_image(
    rest: dict[str, Any], modules: dict[str, Module]
) -> None:
    services = central(rest, modules)
    assert {"rest-server", "reststore-admin", "reststore-restic"} <= set(services)
    assert not {"garage", "garage-admin"} & set(services)
    server = services["rest-server"]
    assert tag_of(server["image"]) == pinned("objectstore", "restic/rest-server")
    assert tag_of(services["reststore-restic"]["image"]) == pinned("objectstore", "restic/restic")
    script = server["command"][0]
    last = script.strip().splitlines()[-1]
    assert last.startswith("exec rest-server --path /data --listen :8000")
    for flag in ("--append-only", "--private-repos", "--prometheus", f"--max-size {40 * GIB}"):
        assert flag in last
    assert "--prometheus-no-auth" not in script
    assert "--no-auth" not in script
    assert server["read_only"] is True
    assert server["cap_drop"] == ["ALL"]


def test_the_htpasswd_is_built_in_tmpfs_from_variables_never_from_literals(
    rest: dict[str, Any], modules: dict[str, Module]
) -> None:
    server = central(rest, modules)["rest-server"]
    script = server["command"][0]
    assert server["tmpfs"] == ["/run/rest-server:size=1m,mode=0700"]
    assert "--htpasswd-file /run/rest-server/htpasswd" in script
    assert 'printf "%s" "$$AYE_RESTSTORE_EDGE_PASSWORD" | htpasswd -B -i' in script
    assert 'printf "%s" "$$AYE_RESTSTORE_METRICS_PASSWORD" | htpasswd -B -i' in script
    assert "htpasswd -b" not in script
    for name, value in server["environment"].items():
        assert value == "${" + name + ":?set " + name + "}"


def test_the_store_listens_on_public_bind_only_when_exposed(
    rest: dict[str, Any], modules: dict[str, Module]
) -> None:
    address = rest["hosts"]["central"]["public_bind"]
    services = central(rest, modules)
    assert services["rest-server"]["ports"] == [f"{address}:8000:8000"]
    assert services["reststore-admin"]["ports"] == ["127.0.0.1:9133:9133"]
    rest["hosts"]["central"]["modules"]["objectstore"]["expose"] = []
    assert central(rest, modules)["rest-server"]["ports"] == ["127.0.0.1:8000:8000"]


def test_the_admin_gets_each_clients_retention_and_its_repository_password(
    rest: dict[str, Any], modules: dict[str, Module]
) -> None:
    rest["hosts"]["edge"]["modules"]["offsite"].update(keep_daily=10, keep_monthly=0)
    env = central(rest, modules)["reststore-admin"]["environment"]
    assert json.loads(env["AYE_RESTSTORE_REPOS"]) == [
        {
            "name": "edge",
            "keep_daily": 10,
            "keep_weekly": 4,
            "keep_monthly": 0,
            "password_env": "AYE_RESTSTORE_EDGE_RESTIC_PASSWORD",
        }
    ]
    assert env["AYE_RESTSTORE_EDGE_RESTIC_PASSWORD"] == (
        "${AYE_RESTSTORE_EDGE_RESTIC_PASSWORD:?set AYE_RESTSTORE_EDGE_RESTIC_PASSWORD}"
    )
    assert "AYE_RESTSTORE_EDGE_PASSWORD" not in env
    assert env["AYE_RESTSTORE_SCHEDULE"] == "11:00"
    assert env["AYE_RESTSTORE_QUOTA_BYTES"] == str(40 * GIB)
    rest["hosts"]["central"]["modules"]["objectstore"]["prune_schedule"] = "02:15"
    env = central(rest, modules)["reststore-admin"]["environment"]
    assert env["AYE_RESTSTORE_SCHEDULE"] == "02:15"


def test_server_and_admin_share_the_data_volume_or_a_host_path(
    rest: dict[str, Any], modules: dict[str, Module]
) -> None:
    compose = host_compose(manifest_from(rest), "central", modules)
    services = compose["services"]
    assert "reststore-data:/data" in services["rest-server"]["volumes"]
    assert "reststore-data:/data" in services["reststore-admin"]["volumes"]
    assert {"reststore-data", "reststore-restic", "reststore-state"} <= set(compose["volumes"])
    assert "objectstore-meta" not in compose["volumes"]
    rest["hosts"]["central"]["modules"]["objectstore"]["data_dir"] = "/srv/offsite"
    compose = host_compose(manifest_from(rest), "central", modules)
    assert "/srv/offsite:/data" in compose["services"]["rest-server"]["volumes"]
    assert "reststore-data" not in compose["volumes"]


def test_only_the_files_of_the_chosen_provider_ship(
    data: dict[str, Any], rest: dict[str, Any], modules: dict[str, Module]
) -> None:
    tree = render_once(manifest_from(rest), modules)
    assert "central/objectstore/reststore_admin.py" in tree
    assert "central/objectstore/garage.toml" not in tree
    assert "central/objectstore/objectstore_admin.py" not in tree


def test_garage_ships_its_own_files_and_not_the_rest_server_admin(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    tree = render_once(manifest_from(data), modules)
    assert "central/objectstore/garage.toml" in tree
    assert "central/objectstore/reststore_admin.py" not in tree


def test_vmagent_scrapes_the_admin_which_carries_the_server_metrics(
    rest: dict[str, Any], modules: dict[str, Module]
) -> None:
    scrape = host_scrape(manifest_from(rest), "central", modules)
    assert scrape is not None
    jobs = {job["job_name"]: job for job in scrape["scrape_configs"]}
    assert jobs["reststore-admin"]["static_configs"] == [{"targets": ["reststore-admin:9133"]}]
    assert "garage" not in jobs


def test_the_offsite_agent_writes_through_rest_with_its_own_user_and_no_s3_keys(
    rest: dict[str, Any], modules: dict[str, Module]
) -> None:
    env = edge_env(rest, modules)
    assert env["RESTIC_REPOSITORY"] == "rest:http://10.8.0.1:8000/edge/"
    assert env["RESTIC_REST_USERNAME"] == "edge"
    assert env["RESTIC_REST_PASSWORD"] == (
        "${AYE_OFFSITE_REST_PASSWORD:?set AYE_OFFSITE_REST_PASSWORD}"
    )
    assert env["AYE_OFFSITE_APPEND_ONLY"] == "1"
    assert not {"AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_DEFAULT_REGION"} & set(env)
    assert "rest:http://" in env["RESTIC_REPOSITORY"] and "@" not in env["RESTIC_REPOSITORY"]


def test_a_garage_target_stays_read_write_and_keeps_its_s3_keys(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    env = edge_env(data, modules)
    assert "AYE_OFFSITE_APPEND_ONLY" not in env
    assert "RESTIC_REST_USERNAME" not in env
    assert env["AWS_ACCESS_KEY_ID"].startswith("${AYE_OFFSITE_ACCESS_KEY_ID")


def test_a_url_target_can_be_declared_append_only(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    url = "s3:https://s3.eu-central-003.backblazeb2.com/acme-backups/edge"
    data["hosts"]["edge"]["modules"]["offsite"]["target"] = {"url": url, "append_only": True}
    env = edge_env(data, modules)
    assert env["AYE_OFFSITE_APPEND_ONLY"] == "1"
    assert env["RESTIC_REPOSITORY"] == url
    data["hosts"]["edge"]["modules"]["offsite"]["target"] = {"url": url}
    assert "AYE_OFFSITE_APPEND_ONLY" not in edge_env(data, modules)


def test_every_secret_lands_in_the_env_examples_and_nowhere_else(
    rest: dict[str, Any], modules: dict[str, Module]
) -> None:
    tree = render_once(manifest_from(rest), modules)
    store = [line for line in tree["central/.env.example"].splitlines() if "RESTSTORE" in line]
    assert store == [
        "AYE_RESTSTORE_EDGE_PASSWORD=replace-me",
        "AYE_RESTSTORE_EDGE_RESTIC_PASSWORD=replace-me",
        "AYE_RESTSTORE_METRICS_PASSWORD=replace-me",
    ]
    agent = [line for line in tree["edge/.env.example"].splitlines() if "OFFSITE" in line]
    assert agent == [
        "AYE_OFFSITE_RESTIC_PASSWORD=replace-me",
        "AYE_OFFSITE_REST_PASSWORD=replace-me",
    ]


def test_no_htpasswd_or_bcrypt_hash_is_ever_committed() -> None:
    listed = subprocess.run(
        ["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True
    ).stdout.decode("utf-8")
    names = [name for name in listed.split("\0") if name]
    assert not [name for name in names if Path(name).name in {".htpasswd", "htpasswd"}]
    hashes = []
    for name in names:
        path = ROOT / name
        if (
            path == Path(__file__)
            or not path.is_file()
            or path.suffix in {".png", ".ico", ".woff2", ".jpg", ".webp"}
        ):
            continue
        text = path.read_bytes().decode("utf-8", "replace")
        if any(marker in text for marker in ("$2y$", "$2b$", "$apr1$")):
            hashes.append(name)
    assert hashes == []


def test_rest_server_alerts_compare_with_bool_and_down_fires_on_silence(
    rest: dict[str, Any], modules: dict[str, Module]
) -> None:
    rules = {
        rule["title"]: rule
        for group in grafana_rules(manifest_from(rest), modules)["groups"]
        for rule in group["rules"]
    }
    down = rules["Append-only backup store down"]
    assert down["noDataState"] == "Alerting"
    maintenance = rules["Off-site retention on the store failed or older than 50 hours"]
    quota = rules["Append-only store above 80 % of its quota"]
    for rule in (down, maintenance, quota):
        expr = rule["data"][0]["model"]["expr"]
        assert "bool" in expr
    assert maintenance["noDataState"] == "OK"


def test_the_portal_draws_the_agent_writing_into_the_rest_server(
    rest: dict[str, Any], modules: dict[str, Module]
) -> None:
    graph = build_graph(manifest_from(rest), modules)
    edges = {(e.src, e.dst, e.label) for e in graph.system}
    assert ("edge/offsite", "central/rest-server", "restic snapshots") in edges
    assert ("central/reststore-admin", "central/vmagent", "scrape") in edges
    assert graph.problems == []
    assert store_service(manifest_from(rest), "central") == "rest-server"
    assert store_service(manifest_from(rest), "edge") == "garage"


@pytest.mark.parametrize(
    ("change", "path", "message"),
    [
        ({"bucket": "acme"}, "objectstore.bucket", "bucket belongs to garage"),
        ({"region": "garage"}, "objectstore.region", "region belongs to garage"),
        ({"meta_dir": "/srv/meta"}, "objectstore.meta_dir", "meta_dir belongs to garage"),
        ({"expose": [3900]}, "objectstore.expose", "rest-server listens on 8000, not 3900"),
    ],
)
def test_garage_settings_on_a_rest_server_are_named(
    rest: dict[str, Any], change: dict[str, Any], path: str, message: str
) -> None:
    rest["hosts"]["central"]["modules"]["objectstore"].update(change)
    problems = objectstore_problems(manifest_from(rest))
    assert (f"hosts.central.modules.{path}", "objectstore-provider", message) in problems


def test_rest_server_settings_on_garage_are_named(data: dict[str, Any]) -> None:
    data["hosts"]["central"]["modules"]["objectstore"].update(
        prune_schedule="02:00", expose=[3900, 8000]
    )
    assert objectstore_problems(manifest_from(data)) == [
        (
            "hosts.central.modules.objectstore.prune_schedule",
            "objectstore-provider",
            "prune_schedule belongs to rest-server",
        ),
        (
            "hosts.central.modules.objectstore.expose",
            "objectstore-provider",
            "garage listens on 3900, not 8000",
        ),
    ]


def test_an_unexposed_rest_server_and_a_client_named_metrics_are_rejected(
    rest: dict[str, Any],
) -> None:
    rest["hosts"]["central"]["modules"]["objectstore"]["expose"] = []
    assert "offsite-target" in rules_hit(rest)
    rest["hosts"]["central"]["modules"]["objectstore"]["expose"] = [8000]
    rest["hosts"]["metrics"] = rest["hosts"].pop("edge")
    rest["hosts"]["metrics"]["modules"]["sentinel"] = {"watch": "central"}
    from ayeops.offsite import target_problems

    assert target_problems(manifest_from(rest), "metrics", "central", "p") == [
        ("p", "offsite-target", "the host name 'metrics' is the rest-server metrics user")
    ]


@pytest.mark.parametrize(
    ("setting", "value"),
    [("prune_schedule", "3:00"), ("prune_schedule", "24:00"), ("expose", [9000])],
)
def test_rest_server_settings_outside_the_schema_are_rejected(
    rest: dict[str, Any], setting: str, value: Any
) -> None:
    rest["hosts"]["central"]["modules"]["objectstore"][setting] = value
    assert "module-settings" in rules_hit(rest)


def test_secret_env_names_are_upper_case_with_underscores() -> None:
    assert secret_env("edge", "PASSWORD") == "AYE_RESTSTORE_EDGE_PASSWORD"
    assert secret_env("db-1", "RESTIC_PASSWORD") == "AYE_RESTSTORE_DB_1_RESTIC_PASSWORD"


def test_the_start_script_adds_every_user_and_caps_the_size_only_when_asked() -> None:
    script = rest_server_script([("edge", "E"), ("db-1", "D")], None)
    assert script == (
        "set -eu\n"
        "umask 077\n"
        ": > /run/rest-server/htpasswd\n"
        'printf "%s" "$AYE_RESTSTORE_METRICS_PASSWORD" | htpasswd -B -i'
        " /run/rest-server/htpasswd metrics\n"
        'printf "%s" "$E" | htpasswd -B -i /run/rest-server/htpasswd edge\n'
        'printf "%s" "$D" | htpasswd -B -i /run/rest-server/htpasswd db-1\n'
        "exec rest-server --path /data --listen :8000 --htpasswd-file /run/rest-server/htpasswd"
        " --append-only --private-repos --prometheus\n"
    )
    assert rest_server_script([], 1024).endswith("--prometheus --max-size 1024\n")


def test_contexts_without_a_rest_server(data: dict[str, Any]) -> None:
    manifest = manifest_from(data)
    assert reststore_context(manifest, "edge", None) == {
        "enabled": False,
        "garage": False,
        "repos": [],
        "repos_json": "[]",
        "start": "",
    }
    garage = reststore_context(manifest, "central", {"provider": "garage"})
    assert (garage["enabled"], garage["garage"]) == (False, True)
    context = offsite_context(manifest, "edge", {"source": "/b/x.dump", "target": {"host": "x"}})
    assert (context["append_only"], context["rest_user"]) == (False, "")


def test_clients_skip_the_store_itself_and_hosts_backing_up_elsewhere(
    rest: dict[str, Any],
) -> None:
    rest["hosts"]["central"]["modules"]["offsite"] = {
        "source": "/var/backups/c/*.dump",
        "target": {"url": "s3:https://example/bucket"},
    }
    names = [
        repo["name"]
        for repo in reststore_context(
            manifest_from(rest), "central", {"provider": "rest-server", "quota": "1g"}
        )["repos"]
    ]
    assert names == ["edge"]


def test_rendered_compose_is_valid_yaml_with_the_start_script_intact(
    rest: dict[str, Any], modules: dict[str, Module]
) -> None:
    text = render_once(manifest_from(rest), modules)["central/compose.yaml"]
    parsed = yaml.safe_load(text)
    assert parsed["services"]["rest-server"]["entrypoint"] == ["/bin/sh", "-c"]
    assert len(parsed["services"]["rest-server"]["command"]) == 1


def alert_names(data: dict[str, Any], modules: dict[str, Module]) -> set[str]:
    return {
        rule["title"]
        for group in grafana_rules(manifest_from(data), modules)["groups"]
        if group["name"] == "objectstore"
        for rule in group["rules"]
    }


def test_a_rest_server_gets_none_of_garages_alerts_which_would_fire_on_no_data(
    rest: dict[str, Any], modules: dict[str, Module]
) -> None:
    names = alert_names(rest, modules)
    assert "Object store down" not in names
    assert "Object store quota and usage not reconciled for an hour" not in names
    assert "Append-only backup store down" in names


def test_garage_gets_none_of_the_rest_server_alerts(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    names = alert_names(data, modules)
    assert "Object store down" in names
    assert not {name for name in names if "Append-only" in name or "on the store" in name}


def test_both_providers_on_two_hosts_get_both_alert_sets(
    rest: dict[str, Any], modules: dict[str, Module]
) -> None:
    rest["hosts"]["edge"]["modules"]["objectstore"] = {}
    names = alert_names(rest, modules)
    assert {"Object store down", "Append-only backup store down"} <= names
