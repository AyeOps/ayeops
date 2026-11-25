from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml

from ayeops.manifest import Manifest
from ayeops.registry import Module
from ayeops.render import host_compose, render_tree
from ayeops.rules import check_manifest
from tests.conftest import EXAMPLE

POSTGRES = {
    "name": "Shop",
    "uid": "shop",
    "type": "postgres",
    "url": "10.0.0.5:5432",
    "database": "shop",
    "user": "grafana_ro",
    "password_env": "AYE_SHOP_PG_PASSWORD",
}
GRAFANA_RULES = {
    "grafana-dashboards",
    "grafana-dashboard-uid",
    "grafana-datasource",
    "grafana-datasource-unique",
    "module-settings",
}


@pytest.fixture(autouse=True)
def apps(tmp_path: Path) -> None:
    shutil.copytree(EXAMPLE.parent / "apps", tmp_path / "apps")


def panel(uid: str) -> dict[str, Any]:
    return {"datasource": {"type": "postgres", "uid": uid}, "targets": [{"refId": "A"}]}


def dashboard(uid: str, *refs: str) -> dict[str, Any]:
    return {"uid": uid, "title": uid, "panels": [panel(ref) for ref in refs]}


def write(directory: Path, name: str, data: Any) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    text = data if isinstance(data, str) else json.dumps(data)
    (directory / name).write_text(text, encoding="utf-8")


def with_grafana(data: dict[str, Any], **grafana: Any) -> dict[str, Any]:
    data["hosts"]["central"]["modules"]["grafana"].update(grafana)
    return data


def grafana_rules(data: dict[str, Any], base: Path, modules: dict[str, Module]) -> set[str]:
    findings = check_manifest(Manifest(data, base), modules)
    return {finding.rule for finding in findings} & GRAFANA_RULES


def test_datasources_render_with_the_secret_left_to_grafana(
    data: dict[str, Any], modules: dict[str, Module], tmp_path: Path
) -> None:
    with_grafana(data, datasources=[POSTGRES])
    tree = render_tree(Manifest(data, tmp_path), modules)
    text = tree["central/grafana/provisioning/datasources/project.yaml"]
    [source] = yaml.safe_load(text)["datasources"]
    assert source["type"] == "grafana-postgresql-datasource"
    assert source["url"] == "10.0.0.5:5432"
    assert source["user"] == "grafana_ro"
    assert source["jsonData"]["database"] == "shop"
    assert source["jsonData"]["sslmode"] == "disable"
    assert source["secureJsonData"] == {"password": "${AYE_SHOP_PG_PASSWORD}"}
    assert source["isDefault"] is False


def test_the_password_reaches_grafana_through_a_required_compose_variable(
    data: dict[str, Any], modules: dict[str, Module], tmp_path: Path
) -> None:
    with_grafana(data, datasources=[POSTGRES])
    environment = host_compose(Manifest(data, tmp_path), "central", modules)["services"]["grafana"][
        "environment"
    ]
    expected = "${AYE_SHOP_PG_PASSWORD:?set AYE_SHOP_PG_PASSWORD}"
    assert environment["AYE_SHOP_PG_PASSWORD"] == expected
    tree = render_tree(Manifest(data, tmp_path), modules)
    assert "AYE_SHOP_PG_PASSWORD=replace-me" in tree["central/.env.example"]


def test_tls_asks_postgres_for_an_encrypted_session(
    data: dict[str, Any], modules: dict[str, Module], tmp_path: Path
) -> None:
    with_grafana(data, datasources=[{**POSTGRES, "tls": True}])
    tree = render_tree(Manifest(data, tmp_path), modules)
    [source] = yaml.safe_load(tree["central/grafana/provisioning/datasources/project.yaml"])[
        "datasources"
    ]
    assert source["jsonData"]["sslmode"] == "require"


def test_postgres_pool_leaves_room_for_other_sessions_of_a_limited_role(
    data: dict[str, Any], modules: dict[str, Module], tmp_path: Path
) -> None:
    with_grafana(data, datasources=[POSTGRES])
    tree = render_tree(Manifest(data, tmp_path), modules)
    [source] = yaml.safe_load(tree["central/grafana/provisioning/datasources/project.yaml"])[
        "datasources"
    ]
    pool = {
        key: source["jsonData"][key]
        for key in source["jsonData"]
        if key.startswith(("max", "conn"))
    }
    assert pool == {
        "maxOpenConns": 2,
        "maxIdleConns": 0,
        "maxIdleConnsAuto": False,
        "connMaxLifetime": 60,
    }


def test_max_connections_sets_the_postgres_pool_size(
    data: dict[str, Any], modules: dict[str, Module], tmp_path: Path
) -> None:
    with_grafana(data, dashboards=None, datasources=[{**POSTGRES, "max_connections": 4}])
    assert grafana_rules(data, tmp_path, modules) == set()
    tree = render_tree(Manifest(data, tmp_path), modules)
    [source] = yaml.safe_load(tree["central/grafana/provisioning/datasources/project.yaml"])[
        "datasources"
    ]
    assert source["jsonData"]["maxOpenConns"] == 4


def test_http_datasources_sign_in_with_basic_auth(
    data: dict[str, Any], modules: dict[str, Module], tmp_path: Path
) -> None:
    loki = {
        "name": "Logs",
        "uid": "logs",
        "type": "loki",
        "url": "https://logs.example/a$b",
        "user": "reader",
        "password_env": "AYE_LOGS_PASSWORD",
    }
    with_grafana(
        data,
        datasources=[loki, {"name": "M", "uid": "m", "type": "prometheus", "url": "http://m:9090"}],
    )
    tree = render_tree(Manifest(data, tmp_path), modules)
    logs, metrics = yaml.safe_load(tree["central/grafana/provisioning/datasources/project.yaml"])[
        "datasources"
    ]
    assert logs["type"] == "loki"
    assert logs["url"] == "https://logs.example/a$$b"
    assert logs["basicAuth"] is True
    assert logs["basicAuthUser"] == "reader"
    assert logs["secureJsonData"] == {"basicAuthPassword": "${AYE_LOGS_PASSWORD}"}
    assert "jsonData" not in logs
    assert "secureJsonData" not in metrics
    assert "basicAuth" not in metrics


def test_project_dashboards_get_a_folder_named_after_the_project(
    data: dict[str, Any], modules: dict[str, Module], tmp_path: Path
) -> None:
    write(tmp_path / "boards", "sales.json", dashboard("shop-sales", "shop"))
    write(tmp_path / "boards", "notes.txt", "skipped")
    with_grafana(data, dashboards="boards", datasources=[POSTGRES])
    manifest = Manifest(data, tmp_path)
    tree = render_tree(manifest, modules)
    assert json.loads(tree["central/grafana/project-dashboards/sales.json"])["uid"] == "shop-sales"
    assert not any(path.endswith("notes.txt") for path in tree)
    providers = yaml.safe_load(tree["central/grafana/provisioning/dashboards/ayeops.yaml"])[
        "providers"
    ]
    assert [(p["folder"], p["options"]["path"]) for p in providers] == [
        ("AyeOps", "/etc/grafana/dashboards"),
        ("acme", "/etc/grafana/project-dashboards"),
    ]
    volumes = host_compose(manifest, "central", modules)["services"]["grafana"]["volumes"]
    assert "./grafana/project-dashboards:/etc/grafana/project-dashboards:ro" in volumes


def test_without_project_settings_grafana_renders_as_before(
    data: dict[str, Any], modules: dict[str, Module], tmp_path: Path
) -> None:
    del data["hosts"]["central"]["modules"]["grafana"]["dashboards"]
    del data["hosts"]["central"]["modules"]["grafana"]["datasources"]
    manifest = Manifest(data, tmp_path)
    tree = render_tree(manifest, modules)
    assert not any("project" in path for path in tree)
    providers = yaml.safe_load(tree["central/grafana/provisioning/dashboards/ayeops.yaml"])[
        "providers"
    ]
    assert [p["name"] for p in providers] == ["ayeops"]
    service = host_compose(manifest, "central", modules)["services"]["grafana"]
    assert not any("project" in volume for volume in service["volumes"])
    assert "AYE_GRAFANA_PG_PASSWORD" not in service["environment"]


def test_declared_and_builtin_datasources_pass(
    data: dict[str, Any], modules: dict[str, Module], tmp_path: Path
) -> None:
    board = dashboard("shop-sales", "shop", "victoriametrics", "-- Grafana --", "${ds}")
    board["panels"].append({"datasource": "Shop"})
    board["panels"].append({"datasource": None})
    write(tmp_path / "boards", "sales.json", board)
    with_grafana(data, dashboards="boards", datasources=[POSTGRES])
    assert grafana_rules(data, tmp_path, modules) == set()


def test_a_panel_on_an_undeclared_datasource_is_a_finding(
    data: dict[str, Any], modules: dict[str, Module], tmp_path: Path
) -> None:
    write(tmp_path / "boards", "sales.json", dashboard("shop-sales", "old-host-uid"))
    with_grafana(data, dashboards="boards", datasources=[POSTGRES])
    findings = check_manifest(Manifest(data, tmp_path), modules)
    [finding] = [f for f in findings if f.rule == "grafana-datasource"]
    assert finding.path == "hosts.central.modules.grafana.dashboards.sales.json"
    assert "'old-host-uid'" in finding.message


def test_the_fleet_loki_datasource_counts_only_with_a_logs_server(
    data: dict[str, Any], modules: dict[str, Module], tmp_path: Path
) -> None:
    write(tmp_path / "boards", "logs.json", dashboard("shop-logs", "loki"))
    with_grafana(data, dashboards="boards")
    servers = [
        host
        for host, spec in data["hosts"].items()
        if (spec.get("modules", {}).get("logs") or {}).get("role") == "server"
    ]
    assert servers
    assert "grafana-datasource" not in grafana_rules(data, tmp_path, modules)
    for host in data["hosts"].values():
        host.get("modules", {}).pop("logs", None)
    assert "grafana-datasource" in grafana_rules(data, tmp_path, modules)


@pytest.mark.parametrize(
    ("files", "rule"),
    [
        ({}, "grafana-dashboards"),
        ({"a.json": "{not json"}, "grafana-dashboards"),
        ({"a.json": {"title": "no uid"}}, "grafana-dashboards"),
        ({"a.json": dashboard("ayeops-probes")}, "grafana-dashboard-uid"),
        ({"a.json": dashboard("same"), "b.json": dashboard("same")}, "grafana-dashboard-uid"),
    ],
)
def test_broken_dashboard_folders_are_findings(
    data: dict[str, Any],
    modules: dict[str, Module],
    tmp_path: Path,
    files: dict[str, Any],
    rule: str,
) -> None:
    (tmp_path / "boards").mkdir()
    for name, content in files.items():
        write(tmp_path / "boards", name, content)
    with_grafana(data, dashboards="boards", datasources=[])
    assert grafana_rules(data, tmp_path, modules) == {rule}


@pytest.mark.parametrize(
    "sources",
    [
        [{**POSTGRES, "uid": "victoriametrics"}],
        [{**POSTGRES, "name": "VictoriaMetrics"}],
        [POSTGRES, {**POSTGRES, "name": "Other"}],
        [POSTGRES, {**POSTGRES, "uid": "other"}],
    ],
)
def test_datasource_uids_and_names_are_unique(
    data: dict[str, Any], modules: dict[str, Module], tmp_path: Path, sources: list[Any]
) -> None:
    with_grafana(data, dashboards=None, datasources=sources)
    assert grafana_rules(data, tmp_path, modules) == {"grafana-datasource-unique"}


@pytest.mark.parametrize(
    "source",
    [
        {key: value for key, value in POSTGRES.items() if key != "database"},
        {**POSTGRES, "url": "postgres://10.0.0.5:5432"},
        {key: value for key, value in POSTGRES.items() if key != "user"},
        {**POSTGRES, "password_env": "lower_case"},
        {**POSTGRES, "type": "mysql"},
        {"name": "L", "uid": "l", "type": "loki", "url": "http://l:3100", "database": "x"},
        {"name": "L", "uid": "l", "type": "loki", "url": "http://l:3100", "tls": True},
        {"name": "L", "uid": "l", "type": "loki", "url": "http://l:3100", "max_connections": 2},
        {**POSTGRES, "max_connections": 0},
        {**POSTGRES, "max_connections": 21},
        {"name": "L", "uid": "l", "type": "loki", "url": "l:3100"},
        {"name": "L", "uid": "l", "type": "loki", "url": "http://l:3100", "secret": "x"},
    ],
)
def test_malformed_datasources_fail_the_module_schema(
    data: dict[str, Any], modules: dict[str, Module], tmp_path: Path, source: dict[str, Any]
) -> None:
    with_grafana(data, dashboards=None, datasources=[source])
    assert grafana_rules(data, tmp_path, modules) == {"module-settings"}
