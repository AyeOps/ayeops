from __future__ import annotations

import json
import re
import shutil
from functools import cache
from pathlib import Path
from typing import Any

import pytest
import yaml
from jsonschema import Draft202012Validator

from ayeops.manifest import Manifest
from ayeops.permissions import credentials, generated
from ayeops.registry import Module, load_modules
from ayeops.render import grafana_rules, host_compose
from tests.conftest import (
    EXAMPLE,
    check_once,
    manifest_from,
    pinned,
    render_once,
    rules_hit,
    tag_of,
)

SERVICES = {
    "agentgateway",
    "mcp-grafana",
    "mcp-victoriametrics",
    "mcp-tools",
    "mcp-s3-bin",
    "mcp-catalog",
}
IMAGES = {
    "agentgateway": "ghcr.io/agentgateway/agentgateway",
    "mcp-grafana": "grafana/mcp-grafana",
    "mcp-victoriametrics": "ghcr.io/victoriametrics/mcp-victoriametrics",
    "mcp-tools": "busybox",
    "mcp-s3-bin": "ghcr.io/txn2/mcp-s3",
    "mcp-catalog": "python",
}
SPEC = {
    "openapi": "3.0.3",
    "info": {"title": "Shop", "version": "1"},
    "paths": {
        "/orders": {
            "get": {"operationId": "listOrders", "responses": {"200": {"description": "ok"}}},
            "post": {"operationId": "createOrder", "responses": {"201": {"description": "ok"}}},
        },
        "/orders/{id}": {
            "get": {"operationId": "getOrder", "responses": {"200": {"description": "ok"}}},
            "delete": {
                "operationId": "deleteOrder",
                "responses": {"204": {"description": "ok"}},
            },
        },
    },
}


def mcp(data: dict[str, Any], host: str = "central") -> dict[str, Any]:
    settings: dict[str, Any] = data["hosts"][host]["modules"]["mcp"]
    return settings


def sources(data: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = mcp(data)["sources"]
    return result


def config_of(manifest: Manifest, modules: dict[str, Module]) -> dict[str, Any]:
    result: dict[str, Any] = yaml.safe_load(
        render_once(manifest, modules)["central/mcp/agentgateway.yaml"]
    )
    return result


def targets(config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {target["name"]: target for target in config["mcp"]["targets"]}


def rules(config: dict[str, Any]) -> list[str]:
    return [rule["allow"] for rule in config["mcp"]["policies"]["mcpAuthorization"]["rules"]]


def services(data: dict[str, Any], modules: dict[str, Module]) -> dict[str, Any]:
    result: dict[str, Any] = host_compose(manifest_from(data), "central", modules)["services"]
    return result


def with_shop(tmp_path: Path, data: dict[str, Any], **source: Any) -> Manifest:
    shutil.copytree(
        EXAMPLE.parent, tmp_path, ignore=shutil.ignore_patterns("rendered"), dirs_exist_ok=True
    )
    (tmp_path / "apis").mkdir()
    (tmp_path / "apis" / "shop.json").write_text(json.dumps(SPEC), encoding="utf-8")
    sources(data)["shop"] = {
        "kind": "openapi",
        "description": "Orders of the shop.",
        "spec": "apis/shop.json",
        "url": "http://shop:8080",
        **source,
    }
    return Manifest(data, tmp_path)


def hits(manifest: Manifest) -> set[str]:
    return {finding.rule for finding in check_once(manifest, load_modules())}


def test_the_example_hub_passes_every_rule(data: dict[str, Any]) -> None:
    assert "mcp" in data["hosts"]["central"]["modules"]
    assert rules_hit(data) == set()


def test_the_hub_multiplexes_every_source_with_stable_prefixes(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    config = config_of(manifest_from(data), modules)
    assert config["config"] == {"adminAddr": "off"}
    assert config["mcp"]["prefixMode"] == "always"
    assert config["mcp"]["failureMode"] == "failOpen"
    assert config["mcp"]["policies"]["timeout"] == {"requestTimeout": "60s"}
    found = targets(config)
    assert set(found) == {"backups", "catalog", "grafana", "metrics"}
    assert found["catalog"]["mcp"] == {"host": "http://mcp-catalog:8000/mcp"}
    assert found["grafana"]["mcp"] == {"host": "http://mcp-grafana:8000/mcp"}
    assert found["metrics"]["mcp"] == {"host": "http://mcp-victoriametrics:8080/mcp"}


def test_only_allowed_tools_pass_and_s3_defaults_to_listing_and_metadata(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    assert rules(config_of(manifest_from(data), modules)) == [
        'mcp.tool.target == "backups" && mcp.tool.name in '
        '["s3_get_object_metadata", "s3_list_buckets", "s3_list_objects"]',
        'mcp.tool.target == "catalog" && mcp.tool.name in '
        '["get_runbook", "get_service", "list_alerts", "list_hosts", "list_services"]',
        'mcp.tool.target == "grafana"',
        'mcp.tool.target == "metrics"',
    ]


def test_s3_runs_in_the_hub_with_only_its_own_keys(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    stdio = targets(config_of(manifest_from(data), modules))["backups"]["stdio"]
    assert stdio["cmd"] == "/opt/aye/s3/mcp-s3"
    assert stdio["clear_env"] is True
    env = stdio["env"]
    assert env["AWS_ACCESS_KEY_ID"] == "$AYE_MCP_BACKUPS_ACCESS_KEY_ID"
    assert env["AWS_SECRET_ACCESS_KEY"] == "$AYE_MCP_BACKUPS_SECRET_ACCESS_KEY"
    assert env["S3_ENDPOINT"] == "http://garage:3900"
    assert env["AWS_REGION"] == "garage"
    assert env["S3_USE_PATH_STYLE"] == "true"
    assert env["MCP_S3_EXT_READONLY"] == "true"


SECRET_VALUES = (
    'with "double" quotes',
    "back\\slash and \\n and \\\\",
    "it's # not: a comment, {flow} [list]",
    "  leading and trailing spaces  ",
    "$HOME ${PATH} stay as they are",
    "plain-hex-0123456789abcdef",
)


def expand(text: str, values: dict[str, str]) -> str:
    return re.sub(r"\$([A-Z][A-Z0-9_]*)", lambda match: values[match.group(1)], text)


def leaves(node: Any, path: str = "") -> dict[str, Any]:
    if isinstance(node, dict):
        children = {f"{path}.{key}": item for key, item in node.items()}
    elif isinstance(node, list):
        children = {f"{path}[{index}]": item for index, item in enumerate(node)}
    else:
        return {path: node}
    found: dict[str, Any] = {}
    for child, item in children.items():
        found.update(leaves(item, child))
    return found


def env_refs(config: dict[str, Any]) -> dict[str, str]:
    return {
        path: value[1:]
        for path, value in leaves(config).items()
        if isinstance(value, str) and value.startswith("$")
    }


def test_variables_are_literal_blocks_so_the_gateway_expansion_keeps_every_byte(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    text = render_once(manifest_from(data), modules)["central/mcp/agentgateway.yaml"]
    lines = text.splitlines()
    references = [index for index, line in enumerate(lines) if "$" in line]
    assert references
    assert all(lines[index - 1].endswith(": |2-") for index in references)
    refs = env_refs(yaml.safe_load(text))
    assert sorted(refs.values()) == [
        "AYE_MCP_BACKUPS_ACCESS_KEY_ID",
        "AYE_MCP_BACKUPS_SECRET_ACCESS_KEY",
        "AYE_MCP_GRAFANA_SERVER_TOKEN",
    ]
    for value in SECRET_VALUES:
        expanded = leaves(yaml.safe_load(expand(text, dict.fromkeys(refs.values(), value))))
        assert {path: expanded[path] for path in refs} == dict.fromkeys(refs, value)


@pytest.mark.parametrize("value", ["first\nsecond", "first\nsecond: line", "\nsecond"])
def test_a_secret_with_a_line_break_stops_the_config_from_parsing(
    data: dict[str, Any], modules: dict[str, Module], value: str
) -> None:
    text = render_once(manifest_from(data), modules)["central/mcp/agentgateway.yaml"]
    names = set(env_refs(yaml.safe_load(text)).values())
    with pytest.raises(yaml.YAMLError):
        yaml.safe_load(expand(text, dict.fromkeys(names, value)))


def test_an_s3_source_defaults_to_the_garage_of_its_host(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    del sources(data)["backups"]["endpoint"]
    del sources(data)["backups"]["region"]
    data["hosts"]["central"]["modules"]["objectstore"]["region"] = "acme"
    assert rules_hit(data) == set()
    env = targets(config_of(manifest_from(data), modules))["backups"]["stdio"]["env"]
    assert (env["S3_ENDPOINT"], env["AWS_REGION"]) == ("http://garage:3900", "acme")
    data["hosts"]["central"]["modules"]["objectstore"] = {"provider": "rest-server"}
    assert "mcp-source" in rules_hit(data)


def test_a_remote_store_takes_its_endpoint_region_and_path_style(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    sources(data)["releases"] = {
        "kind": "s3",
        "description": "Release artifacts on R2.",
        "endpoint": "https://0123456789abcdef.r2.cloudflarestorage.com",
        "region": "auto",
        "path_style": False,
    }
    assert rules_hit(data) == set()
    env = targets(config_of(manifest_from(data), modules))["releases"]["stdio"]["env"]
    assert env["S3_ENDPOINT"] == "https://0123456789abcdef.r2.cloudflarestorage.com"
    assert env["AWS_REGION"] == "auto"
    assert env["S3_USE_PATH_STYLE"] == "false"
    hub = services(data, modules)["agentgateway"]["environment"]
    assert "AYE_MCP_RELEASES_SECRET_ACCESS_KEY" in hub


def test_a_write_tool_listed_as_a_read_is_refused(data: dict[str, Any]) -> None:
    sources(data)["backups"]["tools"] = ["s3_list_objects", "s3_delete_object"]
    assert rules_hit(data) == {"mcp-write"}


def test_presigning_counts_as_a_write(data: dict[str, Any]) -> None:
    sources(data)["backups"]["tools"] = ["s3_presign_url"]
    assert rules_hit(data) == {"mcp-write"}


def test_an_explicitly_allowed_write_is_exposed_and_lifts_read_only(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    sources(data)["backups"]["allow_write"] = ["s3_put_object"]
    assert rules_hit(data) == set()
    config = config_of(manifest_from(data), modules)
    assert "s3_put_object" in rules(config)[0]
    assert targets(config)["backups"]["stdio"]["env"]["MCP_S3_EXT_READONLY"] == "false"


def test_allow_write_for_a_read_tool_is_refused(data: dict[str, Any]) -> None:
    sources(data)["backups"]["allow_write"] = ["s3_list_objects"]
    assert rules_hit(data) == {"mcp-write"}


def test_an_unknown_s3_tool_is_refused(data: dict[str, Any]) -> None:
    sources(data)["backups"]["tools"] = ["s3_list_everything"]
    assert rules_hit(data) == {"mcp-tools"}


@pytest.mark.parametrize("provider", ["contextforge", "docker", "microsoft"])
def test_other_providers_are_accepted_by_the_schema_and_refused_as_not_implemented(
    data: dict[str, Any], modules: dict[str, Module], provider: str
) -> None:
    mcp(data)["provider"] = provider
    del data["hosts"]["central"]["modules"]["ingress"]["routes"]["hub"]
    assert rules_hit(data) == {"mcp-provider"}
    messages = [f.message for f in check_once(manifest_from(data), modules)]
    assert any("not implemented yet" in message for message in messages)


def test_a_source_without_a_description_is_refused(data: dict[str, Any]) -> None:
    sources(data)["metrics"]["description"] = "  "
    assert rules_hit(data) == {"mcp-description"}


def test_a_key_of_another_kind_is_refused(data: dict[str, Any]) -> None:
    sources(data)["metrics"]["endpoint"] = "http://garage:3900"
    assert rules_hit(data) == {"mcp-source"}


def test_one_grafana_source_per_host(data: dict[str, Any]) -> None:
    sources(data)["dashboards"] = {"kind": "grafana", "description": "Again."}
    assert rules_hit(data) == {"mcp-source"}


def test_defaults_need_the_module_behind_them_on_the_same_host(data: dict[str, Any]) -> None:
    data["hosts"]["edge"]["modules"]["mcp"] = {
        "sources": {
            "grafana": {"kind": "grafana", "description": "Grafana."},
            "metrics": {"kind": "victoriametrics", "description": "Metrics."},
            "backups": {"kind": "s3", "description": "Backups."},
        }
    }
    findings = check_once(manifest_from(data), load_modules())
    assert {(f.path.rsplit(".", 1)[-1], f.rule) for f in findings} == {
        ("grafana", "mcp-source"),
        ("metrics", "mcp-source"),
        ("backups", "mcp-source"),
        ("mcp", "grafana-reader-mount"),
    }
    sources_on_edge = mcp(data, "edge")["sources"]
    del sources_on_edge["grafana"]
    sources_on_edge["metrics"]["url"] = "http://10.8.0.1:8428"
    sources_on_edge["backups"]["endpoint"] = "http://10.8.0.1:3900"
    assert rules_hit(data) == set()


def test_grafana_is_always_the_one_of_the_host_read_with_the_reader_token(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    sources(data)["grafana"]["url"] = "http://10.8.0.1:3000"
    assert rules_hit(data) == {"mcp-source"}
    del sources(data)["grafana"]["url"]
    grafana = services(data, modules)["mcp-grafana"]
    assert modules["mcp"].catalog["mcp-grafana"]["grafana_reader"] is True
    assert grafana["environment"]["GRAFANA_URL"] == "http://grafana:3000"
    token = "/run/grafana-reader/token"
    assert grafana["environment"]["GRAFANA_SERVICE_ACCOUNT_TOKEN_FILE"] == token
    assert grafana["environment"]["AYE_GRAFANA_READER_TOKEN_FILE"] == token
    assert "GRAFANA_SERVICE_ACCOUNT_TOKEN" not in grafana["environment"]
    assert "grafana-reader:/run/grafana-reader:ro" in grafana["volumes"]
    assert grafana["group_add"] == ["10470"]
    assert grafana["depends_on"]["grafana-reader"]["condition"] == "service_healthy"
    tree = render_once(manifest_from(data), modules)
    assert "AYE_MCP_GRAFANA_TOKEN" not in tree["central/.env.example"]


def test_a_proxied_mcp_server_must_name_its_read_tools(data: dict[str, Any]) -> None:
    sources(data)["tickets"] = {
        "kind": "mcp",
        "description": "Issues of the tracker.",
        "url": "https://tracker.acme.example/mcp",
    }
    assert rules_hit(data) == {"mcp-tools"}


def test_a_proxied_mcp_server_gets_its_own_bearer_and_transport(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    sources(data)["tickets"] = {
        "kind": "mcp",
        "description": "Issues of the tracker.",
        "url": "https://tracker.acme.example/sse",
        "transport": "sse",
        "token": True,
        "tools": ["search_issues", "get_issue"],
        "allow_write": ["add_comment"],
    }
    assert rules_hit(data) == set()
    config = config_of(manifest_from(data), modules)
    tickets = targets(config)["tickets"]
    assert tickets["sse"] == {"host": "https://tracker.acme.example/sse"}
    assert tickets["policies"]["backendAuth"]["key"]["value"] == "$AYE_MCP_TICKETS_TOKEN"
    assert (
        'mcp.tool.target == "tickets" && mcp.tool.name in '
        '["add_comment", "get_issue", "search_issues"]'
    ) in rules(config)
    hub = services(data, modules)["agentgateway"]["environment"]
    assert hub["AYE_MCP_TICKETS_TOKEN"] == "${AYE_MCP_TICKETS_TOKEN:?set AYE_MCP_TICKETS_TOKEN}"


def test_an_mcp_server_from_an_image_is_not_implemented_yet(data: dict[str, Any]) -> None:
    sources(data)["git"] = {
        "kind": "mcp",
        "description": "Git history.",
        "image": "mcp/git:1.0.0",
        "command": ["mcp-server-git"],
        "tools": ["git_log"],
    }
    assert rules_hit(data) == {"mcp-kind"}


def test_cli_sources_are_schema_accepted_and_not_implemented_yet(data: dict[str, Any]) -> None:
    sources(data)["sizes"] = {
        "kind": "cli",
        "description": "Sizes of backup prefixes.",
        "image": "rclone/rclone:1.75.2",
        "binary": "/usr/local/bin/rclone",
        "commands": {
            "size": {
                "description": "Bytes below a prefix.",
                "run": ["size", "--json", "garage:{path}"],
                "args": {"path": {"pattern": "^[a-z0-9-]+$"}},
                "timeout": "30s",
            }
        },
    }
    assert rules_hit(data) == {"mcp-kind"}


def test_a_cli_command_that_writes_needs_its_own_allow_and_a_description(
    data: dict[str, Any],
) -> None:
    sources(data)["sizes"] = {
        "kind": "cli",
        "description": "Sizes.",
        "image": "rclone/rclone:1.75.2",
        "binary": "/usr/local/bin/rclone",
        "commands": {"purge": {"run": ["purge", "garage:x"], "write": True}},
    }
    assert rules_hit(data) == {"mcp-kind", "mcp-write", "mcp-description"}


def test_docker_cannot_serve_openapi_whatever_it_implements(data: dict[str, Any]) -> None:
    mcp(data)["provider"] = "docker"
    sources(data)["shop"] = {
        "kind": "openapi",
        "description": "Shop.",
        "spec": "apis/shop.json",
        "url": "http://shop:8080",
    }
    findings = check_once(manifest_from(data), load_modules())
    assert any(f.rule == "mcp-kind" and "OpenAPI" in f.message for f in findings)


def test_openapi_reads_are_exposed_and_writes_need_an_allow(
    tmp_path: Path, data: dict[str, Any], modules: dict[str, Module]
) -> None:
    manifest = with_shop(tmp_path, data, token=True)
    assert hits(manifest) == set()
    tree = render_once(manifest, modules)
    assert json.loads(tree["central/mcp/openapi/shop.json"]) == SPEC
    config = yaml.safe_load(tree["central/mcp/agentgateway.yaml"])
    assert targets(config)["shop"]["openapi"] == {
        "schema": {"file": "/config/openapi/shop.json"},
        "host": "http://shop:8080",
    }
    assert 'mcp.tool.target == "shop" && mcp.tool.name in ["getOrder", "listOrders"]' in rules(
        config
    )


def test_an_openapi_post_listed_as_a_read_is_refused(tmp_path: Path, data: dict[str, Any]) -> None:
    assert hits(with_shop(tmp_path, data, tools=["listOrders", "createOrder"])) == {"mcp-write"}


def test_an_openapi_write_with_an_allow_is_exposed(
    tmp_path: Path, data: dict[str, Any], modules: dict[str, Module]
) -> None:
    manifest = with_shop(tmp_path, data, allow_write=["createOrder"])
    assert hits(manifest) == set()
    assert '"createOrder"' in rules(config_of(manifest, modules))[-1]


def test_an_openapi_spec_must_exist_and_name_every_operation(
    tmp_path: Path, data: dict[str, Any]
) -> None:
    manifest = with_shop(tmp_path, data)
    spec = json.loads(json.dumps(SPEC))
    del spec["paths"]["/orders"]["get"]["operationId"]
    (tmp_path / "apis" / "shop.json").write_text(json.dumps(spec), encoding="utf-8")
    assert hits(manifest) == {"mcp-source"}
    (tmp_path / "apis" / "shop.json").unlink()
    assert hits(manifest) == {"mcp-source"}


@pytest.mark.parametrize(
    ("kind", "keys"),
    [("openapi", {"url": "http://shop:8080"}), ("cli", {"image": "rclone/rclone:1.75.2"})],
)
def test_a_source_without_the_keys_of_its_kind_is_refused(
    data: dict[str, Any], kind: str, keys: dict[str, Any]
) -> None:
    sources(data)["shop"] = {"kind": kind, "description": "Shop.", **keys}
    assert rules_hit(data) == {"mcp-source"}


def test_the_hub_route_cannot_be_opened_past_the_sign_in(data: dict[str, Any]) -> None:
    data["hosts"]["central"]["modules"]["auth"]["open"] = ["hub"]
    assert rules_hit(data) == {"mcp-open"}


def test_the_hub_route_is_behind_the_sign_in(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    tree = render_once(manifest_from(data), modules)
    routers = yaml.safe_load(tree["central/ingress/dynamic/routes.yml"])["http"]["routers"]
    assert routers["hub"]["middlewares"] == ["sso"]


def test_every_service_runs_a_pinned_multi_arch_image_with_a_healthcheck(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    rendered = services(data, modules)
    images = {name: tag_of(rendered[name]["image"]) for name in SERVICES}
    assert images == {name: pinned("mcp", repository) for name, repository in IMAGES.items()}
    assert all(rendered[name]["healthcheck"]["test"] for name in SERVICES)
    assert all(rendered[name]["cap_drop"] == ["ALL"] for name in SERVICES)


def test_only_the_hub_has_a_host_port_and_it_is_loopback(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    rendered = services(data, modules)
    assert rendered["agentgateway"]["ports"] == ["127.0.0.1:3010:3000"]
    assert all("ports" not in rendered[name] for name in SERVICES - {"agentgateway"})


def test_grafana_tools_run_read_only_behind_the_hubs_token(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    grafana = services(data, modules)["mcp-grafana"]
    assert "--disable-write" in grafana["command"]
    assert "--enable-write-tools" not in grafana["command"]
    assert "MCP_GRAFANA_SERVER_TOKEN" in grafana["environment"]
    hub = services(data, modules)["agentgateway"]["environment"]
    assert not any("READER" in key for key in hub)
    assert "AYE_MCP_GRAFANA_SERVER_TOKEN" in hub


def test_the_hub_gets_a_session_key_so_it_never_writes_its_read_only_config(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    hub = services(data, modules)["agentgateway"]
    assert hub["environment"]["SESSION_KEY"] == "${AYE_MCP_SESSION_KEY:?set AYE_MCP_SESSION_KEY}"
    assert "./mcp:/config:ro" in hub["volumes"]
    del sources(data)["grafana"]
    del sources(data)["backups"]
    hub = services(data, modules)["agentgateway"]
    assert hub["environment"] == {"SESSION_KEY": "${AYE_MCP_SESSION_KEY:?set AYE_MCP_SESSION_KEY}"}


def test_storage_keys_stay_out_of_the_sidecars(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    rendered = services(data, modules)
    for name in SERVICES - {"agentgateway"}:
        environment = rendered[name].get("environment") or {}
        assert not any("ACCESS_KEY" in key for key in environment), name


def test_without_s3_sources_no_binary_is_copied(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    del sources(data)["backups"]
    rendered = services(data, modules)
    assert "mcp-s3-bin" not in rendered
    assert "mcp-s3-bin" not in rendered["agentgateway"]["depends_on"]


def test_hub_alerts_and_scrape_jobs_ship_with_the_module(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    groups = grafana_rules(manifest_from(data), modules)["groups"]
    titles = {rule["uid"] for group in groups if group["name"] == "mcp" for rule in group["rules"]}
    assert titles == {"aye-mcp-hub-down", "aye-mcp-hub-config"}
    tree = render_once(manifest_from(data), modules)
    jobs = {
        j["job_name"] for j in yaml.safe_load(tree["central/vmagent/scrape.yaml"])["scrape_configs"]
    }
    assert {"agentgateway", "mcp-grafana", "mcp-victoriametrics", "mcp-catalog"} <= jobs


@pytest.mark.parametrize(
    ("setting", "value"),
    [
        ("provider", "kong"),
        ("timeout", "forever"),
        ("sources", {}),
        ("sources", {"Bad-Name": {"kind": "s3", "description": "x"}}),
        ("sources", {"x": {"kind": "ftp", "description": "x"}}),
        ("sources", {"x": {"kind": "mcp", "description": "x", "url": "http://a/$HOME"}}),
    ],
)
def test_settings_outside_the_schema_are_rejected(
    data: dict[str, Any], setting: str, value: Any
) -> None:
    mcp(data)[setting] = value
    assert rules_hit(data) == {"module-settings"}


def test_the_mcp_module_is_registered(modules: dict[str, Module]) -> None:
    assert set(modules["mcp"].catalog) == SERVICES


AGENTGATEWAY = Path(__file__).parent / "fixtures" / "agentgateway"
RULE = re.compile(
    r'mcp\.tool\.target == "[a-z][a-z0-9]*"'
    r'( && mcp\.tool\.name in \["[A-Za-z0-9_.-]+"(, "[A-Za-z0-9_.-]+")*\])?'
)


@cache
def agentgateway_schema(name: str) -> dict[str, Any]:
    schema: dict[str, Any] = json.loads((AGENTGATEWAY / name).read_text(encoding="utf-8"))
    return schema


def schema_errors(config: dict[str, Any]) -> list[str]:
    validator = Draft202012Validator(agentgateway_schema("config.schema.json"))
    return sorted(
        f"{'.'.join(map(str, error.absolute_path))}: {error.message}"
        for error in validator.iter_errors(config)
    )


def every_kind(tmp_path: Path, data: dict[str, Any]) -> Manifest:
    mcp(data)["timeout"] = "2m"
    sources(data)["backups"]["allow_write"] = ["s3_put_object"]
    sources(data)["tickets"] = {
        "kind": "mcp",
        "description": "Issues of the tracker.",
        "url": "https://tracker.acme.example/sse",
        "transport": "sse",
        "token": True,
        "tools": ["search_issues"],
    }
    sources(data)["wiki"] = {
        "kind": "mcp",
        "description": "Pages of the wiki.",
        "url": "http://wiki:8080/mcp",
        "tools": ["read_page"],
    }
    return with_shop(tmp_path, data, token=True)


def test_the_example_config_is_valid_for_agentgateway_1_7_0(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    assert schema_errors(config_of(manifest_from(data), modules)) == []


def test_every_kind_the_hub_renders_is_valid_for_agentgateway_1_7_0(
    tmp_path: Path, data: dict[str, Any], modules: dict[str, Module]
) -> None:
    manifest = every_kind(tmp_path, data)
    assert hits(manifest) == set()
    config = config_of(manifest, modules)
    assert set(targets(config)) == {
        "backups",
        "catalog",
        "grafana",
        "metrics",
        "shop",
        "tickets",
        "wiki",
    }
    assert schema_errors(config) == []


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("mcp", "prefixMode"), "sometimes"),
        (("mcp", "targets", 0, "stdio", "clearEnv"), True),
        (("mcp", "policies", "mcpAuthorization", "rules", 0), {"permit": "true"}),
        (("mcp", "policies", "timeout", "requestTimeout"), 60),
        (("config", "adminAddress"), "off"),
    ],
)
def test_the_vendored_schema_refuses_what_agentgateway_refuses(
    data: dict[str, Any], modules: dict[str, Module], path: tuple[Any, ...], value: Any
) -> None:
    config = config_of(manifest_from(data), modules)
    node: Any = config
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    assert schema_errors(config) != []


def test_allow_rules_read_only_the_tool_fields_agentgateway_documents(
    tmp_path: Path, data: dict[str, Any], modules: dict[str, Module]
) -> None:
    allow = rules(config_of(every_kind(tmp_path, data), modules))
    assert allow
    assert all(RULE.fullmatch(rule) for rule in allow)
    tool = agentgateway_schema("cel.schema.json")["properties"]["mcp"]["properties"]["tool"]
    assert set(tool["required"]) == {"target", "name"}
    assert {tool["properties"][field]["type"] for field in ("target", "name")} == {"string"}
    fields = {match for rule in allow for match in re.findall(r"\bmcp(?:\.\w+)+", rule)}
    assert fields == {"mcp.tool.target", "mcp.tool.name"}


def test_permissions_name_only_what_each_source_needs(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    found = credentials(manifest_from(data), modules, "central", "mcp")
    assert [(c.issuer, c.env) for c in found] == [
        (
            "the S3 store of source backups, http://garage:3900",
            ("AYE_MCP_BACKUPS_ACCESS_KEY_ID", "AYE_MCP_BACKUPS_SECRET_ACCESS_KEY"),
        )
    ]
    assert found[0].grants[-1] == "no write, no delete"
    made = generated(manifest_from(data), modules, "central", "mcp")
    assert set(made) == {"AYE_MCP_SESSION_KEY", "AYE_MCP_GRAFANA_SERVER_TOKEN"}
    sources(data)["backups"]["allow_write"] = ["s3_put_object"]
    del sources(data)["grafana"]
    found = credentials(manifest_from(data), modules, "central", "mcp")
    assert found[0].grants[-1] == "also write for s3_put_object, which allow_write lists"
    assert generated(manifest_from(data), modules, "central", "mcp") == ["AYE_MCP_SESSION_KEY"]
