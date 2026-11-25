from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml

from ayeops.manifest import Manifest
from ayeops.mcp import CATALOG_TOOLS
from ayeops.mcp_catalog import CATALOG_FILE, catalog_data, catalog_json, scrub
from ayeops.permissions import credentials, generated
from ayeops.registry import Module
from ayeops.render import host_compose, render_tree
from tests.conftest import EXAMPLE, ROOT, manifest_from, pinned, rules_hit, tag_of

TOP_KEYS = {"project", "hosts", "services", "alerts", "runbooks"}
HOST_KEYS = {"name", "group", "zone", "arch", "provider", "modules"}
SERVICE_KEYS = {
    "id",
    "name",
    "host",
    "group",
    "source",
    "state",
    "criticality",
    "description",
    "url",
    "depends_on",
    "clients",
    "metrics",
    "alerts",
    "logs",
    "status",
    "cloud",
}
ALERT_KEYS = {
    "name",
    "module",
    "service",
    "title",
    "severity",
    "for",
    "summary",
    "expr",
    "baseline",
    "runbook",
    "runbook_url",
}
RUNBOOK_KEYS = {"name", "url", "text"}
SENTINEL = "s3cr3t-sentinel-value"


def sources(data: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = data["hosts"]["central"]["modules"]["mcp"]["sources"]
    return result


def served(manifest: Manifest, modules: dict[str, Module]) -> dict[str, Any]:
    result: dict[str, Any] = json.loads(render_tree(manifest, modules)[f"central/{CATALOG_FILE}"])
    return result


def copied(tmp_path: Path, data: dict[str, Any]) -> Manifest:
    shutil.copytree(
        EXAMPLE.parent, tmp_path, ignore=shutil.ignore_patterns("rendered"), dirs_exist_ok=True
    )
    return Manifest(data, tmp_path)


def test_the_example_serves_a_catalog_source(data: dict[str, Any]) -> None:
    assert sources(data)["catalog"]["kind"] == "catalog"
    assert rules_hit(data) == set()


def test_the_tools_the_hub_allows_are_the_tools_the_server_answers() -> None:
    from tests.test_mcp_catalog_server import server

    assert set(CATALOG_TOOLS) == set(server.TOOLS)


def test_manifest_services_carry_their_catalog_fields(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    catalog = served(manifest_from(data), modules)
    web = catalog["services"]["central/web"]
    assert web == {
        "id": "central/web",
        "name": "web",
        "host": "central",
        "source": "manifest",
        "state": "live",
        "description": "Public website of acme, served by nginx behind the CDN.",
        "criticality": "critical",
        "url": "https://acme.example",
        "depends_on": ["cdn"],
        "metrics": ["probe_success", "probe_duration_seconds"],
        "alerts": ["web-down"],
        "logs": "docker",
        "status": {"public": "Website", "probe": "https://acme.example"},
        "cloud": ["cdn"],
    }
    assert catalog["alerts"]["web-down"] == {
        "name": "web-down",
        "service": "web",
        "title": "Website down",
        "severity": "critical",
        "for": "3m",
        "summary": "https://acme.example does not answer with 2xx",
        "expr": 'probe_success{instance="https://acme.example"} == bool 0',
        "runbook": "web-down",
        "runbook_url": "https://runbooks.acme.example/web-down",
    }


def test_module_services_are_listed_on_the_hosts_that_render_them(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    manifest = manifest_from(data)
    catalog = served(manifest, modules)
    for host in manifest.hosts:
        rendered = set(host_compose(manifest, host, modules)["services"])
        listed = {s["name"] for s in catalog["services"].values() if s["source"] != "manifest"}
        mine = {s["name"] for s in catalog["services"].values() if s["host"] == host}
        assert rendered & listed <= mine
    hub = catalog["services"]["central/mcp-catalog"]
    assert hub["source"] == "module mcp"
    assert hub["status"] == {"internal": "catalog tools of the MCP hub"}
    assert hub["alerts"] == ["target-down", "mcp-hub-down"]
    assert "edge/mcp-catalog" not in catalog["services"]


def test_hosts_carry_group_zone_arch_provider_and_modules_but_no_address(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    data["hosts"]["edge"]["group"] = "office"
    catalog = served(manifest_from(data), modules)
    central = catalog["hosts"]["central"]
    assert central["zone"] == "datacenter"
    assert central["arch"] == "arm64"
    assert central["provider"] == "example-cloud"
    assert "mcp" in central["modules"]
    assert catalog["hosts"]["edge"]["group"] == "office"
    assert all(s["group"] == "office" for s in catalog["services"].values() if s["host"] == "edge")
    text = json.dumps(catalog)
    assert "10.8.0.1" not in text
    assert "address" not in central


def test_module_alerts_carry_the_runbook_page_of_the_repository(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    catalog = served(manifest_from(data), modules)
    alert = catalog["alerts"]["mcp-hub-down"]
    assert alert["module"] == "mcp"
    assert alert["severity"] == "warning"
    assert alert["runbook"] == "mcp-hub"
    page = catalog["runbooks"]["mcp-hub"]
    assert page["url"] == "https://github.com/AyeOps/ayeops/blob/main/docs/runbooks/mcp-hub.md"
    assert page["text"] == (ROOT / "docs" / "runbooks" / "mcp-hub.md").read_text(encoding="utf-8")


def test_only_alerts_a_listed_service_names_are_served(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    catalog = served(manifest_from(data), modules)
    named = {alert for service in catalog["services"].values() for alert in service["alerts"]}
    assert set(catalog["alerts"]) <= named
    assert {a["runbook"] for a in catalog["alerts"].values() if "runbook" in a} == set(
        catalog["runbooks"]
    )
    assert all(page["name"] == name for name, page in catalog["runbooks"].items())


def test_a_manifest_runbook_is_read_from_runbooks_next_to_the_manifest(
    tmp_path: Path, data: dict[str, Any], modules: dict[str, Module]
) -> None:
    manifest = copied(tmp_path, data)
    (tmp_path / "runbooks").mkdir()
    (tmp_path / "runbooks" / "web-down.md").write_bytes(b"# web-down\r\n\r\nRestart nginx.\r\n")
    catalog = served(manifest, modules)
    assert catalog["runbooks"]["web-down"] == {
        "name": "web-down",
        "url": "https://runbooks.acme.example/web-down",
        "text": "# web-down\n\nRestart nginx.\n",
    }
    assert catalog["runbooks"]["mcp-down"]["text"] is None


@pytest.mark.parametrize("runbook", ["../outside", "https://wiki.example/page", ".hidden"])
def test_a_runbook_outside_the_runbooks_folder_is_never_read(
    tmp_path: Path, data: dict[str, Any], modules: dict[str, Module], runbook: str
) -> None:
    data["services"]["web"]["alerts"][0]["runbook"] = runbook
    manifest = copied(tmp_path, data)
    (tmp_path / "outside.md").write_text(SENTINEL, encoding="utf-8")
    (tmp_path / "runbooks").mkdir()
    (tmp_path / "runbooks" / ".hidden.md").write_text(SENTINEL, encoding="utf-8")
    catalog = served(manifest, modules)
    assert runbook not in catalog["runbooks"]
    assert catalog["alerts"]["web-down"]["runbook"] == runbook
    assert SENTINEL not in json.dumps(catalog)


def test_a_long_runbook_is_cut(
    tmp_path: Path, data: dict[str, Any], modules: dict[str, Module]
) -> None:
    manifest = copied(tmp_path, data)
    (tmp_path / "runbooks").mkdir()
    (tmp_path / "runbooks" / "web-down.md").write_text("x" * 30000, encoding="utf-8")
    assert len(served(manifest, modules)["runbooks"]["web-down"]["text"]) == 20000


def leaves(node: Any) -> list[str]:
    if isinstance(node, dict):
        return [leaf for value in node.values() for leaf in leaves(value)]
    if isinstance(node, list):
        return [leaf for value in node for leaf in leaves(value)]
    return [str(node)]


def secret_heavy(tmp_path: Path, data: dict[str, Any]) -> Manifest:
    web = data["services"]["web"]
    web["login"] = f"admin / {SENTINEL}-login"
    web["url"] = f"https://admin:{SENTINEL}-url@acme.example/?token={SENTINEL}-query"
    web["checks"] = {
        "home": {
            "type": "http",
            "url": f"https://bot:{SENTINEL}-check@acme.example/health?api_key={SENTINEL}-key",
            "alert": {"severity": "warning", "runbook": "web-down"},
        }
    }
    sources(data)["tickets"] = {
        "kind": "mcp",
        "description": "Issues of the tracker.",
        "url": "https://tracker.acme.example/mcp",
        "token": True,
        "tools": ["search_issues"],
    }
    data["hosts"]["central"]["address"] = "203.0.113.77"
    manifest = copied(tmp_path, data)
    return manifest


def test_no_secret_reaches_the_catalog(
    tmp_path: Path, data: dict[str, Any], modules: dict[str, Module], monkeypatch: Any
) -> None:
    manifest = secret_heavy(tmp_path, data)
    names: set[str] = set()
    for host in manifest.hosts:
        for module in manifest.host_modules(host):
            if module in modules:
                for credential in credentials(manifest, modules, host, module):
                    names.update(credential.env)
                names.update(generated(manifest, modules, host, module))
    assert "AYE_MCP_TICKETS_TOKEN" in names
    values = {name: f"{SENTINEL}-{index}" for index, name in enumerate(sorted(names))}
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    env = "".join(f"{name}={value}\n" for name, value in values.items())
    (tmp_path / ".env").write_text(env, encoding="utf-8")
    for host in manifest.hosts:
        (tmp_path / f"{host}.env").write_text(env, encoding="utf-8")
    text = render_tree(manifest, modules)[f"central/{CATALOG_FILE}"]
    assert SENTINEL not in text
    assert "203.0.113.77" not in text
    catalog = json.loads(text)
    assert catalog["services"]["central/web"]["url"] == "https://***@acme.example/?token=***"
    summaries = [a.get("summary", "") for a in catalog["alerts"].values()]
    assert any("https://***@acme.example/health?api_key=***" in s for s in summaries)


def test_the_catalog_holds_only_the_fields_it_names(
    tmp_path: Path, data: dict[str, Any], modules: dict[str, Module]
) -> None:
    catalog = served(secret_heavy(tmp_path, data), modules)
    assert set(catalog) == TOP_KEYS
    assert all(set(host) <= HOST_KEYS for host in catalog["hosts"].values())
    assert all(set(service) <= SERVICE_KEYS for service in catalog["services"].values())
    assert all(set(alert) <= ALERT_KEYS for alert in catalog["alerts"].values())
    assert all(set(page) == RUNBOOK_KEYS for page in catalog["runbooks"].values())
    assert all("login" not in service for service in catalog["services"].values())


def test_no_variable_reference_of_the_stack_reaches_the_catalog(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    catalog = catalog_data(manifest_from(data), modules)
    for leaf in leaves({k: v for k, v in catalog.items() if k != "runbooks"}):
        assert "${" not in leaf, leaf


@pytest.mark.parametrize(
    ("raw", "clean"),
    [
        ("https://u:p@a.example/x?token=abc&x=1", "https://***@a.example/x?token=***&x=1"),
        ("postgres://me:pw@db:5432/a", "postgres://***@db:5432/a"),
        ("see https://a.example/?API_KEY=zzz#f", "see https://a.example/?API_KEY=***#f"),
        ("https://a.example/?sig=1;password=2", "https://a.example/?sig=***;password=***"),
        (
            "mail ops@acme.example or https://a.example/p@x",
            "mail ops@acme.example or https://a.example/p@x",
        ),
        ("https://a.example/?page=2&q=key", "https://a.example/?page=2&q=key"),
    ],
)
def test_credentials_in_urls_are_masked(raw: str, clean: str) -> None:
    assert scrub({"a": [raw, 1, None]}) == {"a": [clean, 1, None]}


def test_localized_text_is_served_in_the_portal_language(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    data["services"]["web"]["description"] = {"en": "Website", "uk": "Сайт"}
    data["portal"] = {"locale": "uk"}
    assert served(manifest_from(data), modules)["services"]["central/web"]["description"] == "Сайт"


def test_a_service_without_metrics_says_why(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    catalog = served(manifest_from(data), modules)
    assert set(catalog["services"]["central/mcp-tools"]["metrics"]) == {"none"}


def test_the_catalog_json_is_stable(data: dict[str, Any], modules: dict[str, Module]) -> None:
    manifest = manifest_from(data)
    first = catalog_json(manifest, modules)
    assert first == catalog_json(manifest, modules)
    assert first.endswith("}\n")


def test_without_a_catalog_source_nothing_is_rendered(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    del sources(data)["catalog"]
    manifest = manifest_from(data)
    tree = render_tree(manifest, modules)
    assert not any(path.startswith("central/mcp-catalog/") for path in tree)
    assert "mcp-catalog" not in host_compose(manifest, "central", modules)["services"]
    jobs = yaml.safe_load(tree["central/vmagent/scrape.yaml"])["scrape_configs"]
    assert "mcp-catalog" not in {job["job_name"] for job in jobs}


def test_the_catalog_server_runs_read_only_without_a_host_port(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    manifest = manifest_from(data)
    service = host_compose(manifest, "central", modules)["services"]["mcp-catalog"]
    assert tag_of(service["image"]) == pinned("mcp", "python")
    assert service["command"] == ["python", "-I", "/app/catalog_server.py"]
    assert service["user"] == "65534:65534"
    assert service["read_only"] is True
    assert service["cap_drop"] == ["ALL"]
    assert "ports" not in service
    assert service["volumes"] == [
        "./mcp-catalog/catalog_server.py:/app/catalog_server.py:ro",
        "./mcp-catalog/catalog.json:/data/catalog.json:ro",
    ]
    assert not any("$" in str(value) for value in service["environment"].values())
    tree = render_tree(manifest, modules)
    script = tree["central/mcp-catalog/catalog_server.py"]
    assert script == (
        Path(modules["mcp"].path / "files" / "catalog_server.py").read_text(encoding="utf-8")
    )
    jobs = yaml.safe_load(tree["central/vmagent/scrape.yaml"])["scrape_configs"]
    assert {"job_name": "mcp-catalog", "static_configs": [{"targets": ["mcp-catalog:8000"]}]} in (
        jobs
    )


def test_a_changed_catalog_restarts_its_server(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    first = yaml.safe_load(render_tree(manifest_from(data), modules)["central/compose.yaml"])
    data["services"]["web"]["description"] = "Another description."
    second = yaml.safe_load(render_tree(manifest_from(data), modules)["central/compose.yaml"])
    assert first["services"]["mcp-catalog"] != second["services"]["mcp-catalog"]
    assert first["services"]["agentgateway"] == second["services"]["agentgateway"]


def test_one_catalog_source_per_host(data: dict[str, Any]) -> None:
    sources(data)["catalog2"] = {"kind": "catalog", "description": "Again."}
    assert rules_hit(data) == {"mcp-source"}


def test_tools_narrow_the_catalog_tools(data: dict[str, Any], modules: dict[str, Module]) -> None:
    sources(data)["catalog"]["tools"] = ["list_services", "get_runbook"]
    assert rules_hit(data) == set()
    config = yaml.safe_load(
        render_tree(manifest_from(data), modules)["central/mcp/agentgateway.yaml"]
    )
    rules = [rule["allow"] for rule in config["mcp"]["policies"]["mcpAuthorization"]["rules"]]
    assert 'mcp.tool.target == "catalog" && mcp.tool.name in ["get_runbook", "list_services"]' in (
        rules
    )


@pytest.mark.parametrize(
    ("source", "rule"),
    [
        ({"tools": ["delete_service"]}, "mcp-tools"),
        ({"allow_write": ["get_service"]}, "mcp-source"),
        ({"url": "http://elsewhere:8000/mcp"}, "mcp-source"),
        ({"description": " "}, "mcp-description"),
    ],
)
def test_a_catalog_source_takes_only_its_own_keys_and_tools(
    data: dict[str, Any], source: dict[str, Any], rule: str
) -> None:
    sources(data)["catalog"].update(source)
    assert rule in rules_hit(data)
