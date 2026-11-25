from __future__ import annotations

import re
from collections import Counter
from pathlib import Path
from typing import Any

import pytest
import yaml

from ayeops import grafana_reader, render
from ayeops.duties import OWN_RUNBOOK as DUTY_RUNBOOK_LINK
from ayeops.duties import TITLE as DUTY_TITLE
from ayeops.manifest import load_manifest
from ayeops.portal.pages import portal_pages
from ayeops.registry import MODULES_DIR, Module, load_modules
from ayeops.render import (
    ENV_REFERENCE,
    FAST_DUMPER,
    MODULE_RUNBOOK_BASE,
    FleetRender,
    astral,
    dump_yaml,
    host_compose,
    host_render,
    host_scrape,
    host_settings,
    module_context,
    render_tree,
    runbook_url,
    write_tree,
)
from ayeops.render_index import Drift, drift, expected
from ayeops.rules import check_manifest
from ayeops.strict import EnvRef, load_yaml
from tests.conftest import EXAMPLE, EXAMPLE_STAMP, RENDERED, ROOT, manifest_from, render_once

PINNED = re.compile(r"^[a-z0-9./_-]+:v?[0-9]+(\.[0-9]+)+(-[a-z0-9.]+)?@sha256:[a-f0-9]{64}$")
RENDERED_IMAGE = re.compile(r"(?m)^    image: (\S+)$")
QUICKSTART = ROOT / "examples" / "quickstart.yaml"


def compose_of(data: dict[str, Any], host: str, modules: dict[str, Module]) -> dict[str, Any]:
    return host_compose(manifest_from(data), host, modules)


def all_services(data: dict[str, Any], modules: dict[str, Module]) -> dict[str, dict[str, Any]]:
    return {
        f"{host}/{name}": service
        for host in data["hosts"]
        for name, service in compose_of(data, host, modules)["services"].items()
    }


def test_render_is_deterministic_and_matches_the_committed_example(
    modules: dict[str, Module],
) -> None:
    first = render_tree(load_manifest(EXAMPLE), modules, EXAMPLE_STAMP)
    second = render_tree(load_manifest(EXAMPLE), modules, EXAMPLE_STAMP)
    assert first == second
    assert drift(first, RENDERED) == Drift()
    committed = {p.relative_to(RENDERED).as_posix() for p in RENDERED.rglob("*") if p.is_file()}
    assert committed == set(expected(first))


def test_written_files_use_unix_line_endings(tmp_path: Path, modules: dict[str, Module]) -> None:
    write_tree(render_once(load_manifest(EXAMPLE), modules), tmp_path)
    assert all(b"\r\n" not in p.read_bytes() for p in tmp_path.rglob("*") if p.is_file())


def test_ports_bind_to_localhost_unless_exposed(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    ports = {
        name: service.get("ports", []) for name, service in all_services(data, modules).items()
    }
    assert ports["central/victoriametrics"] == ["10.8.0.1:8428:8428"]
    assert ports["central/loki"] == ["10.8.0.1:3100:3100"]
    assert ports["central/traefik"] == ["10.8.0.1:443:443", "10.8.0.1:80:80"]
    assert ports["central/grafana"] == ["127.0.0.1:3000:3000"]
    assert ports["central/garage"] == ["10.8.0.1:3900:3900", "127.0.0.1:3903:3903"]
    others = [
        p
        for name, ps in ports.items()
        for p in ps
        if name
        not in {"central/victoriametrics", "central/loki", "central/traefik", "central/garage"}
    ]
    assert others
    assert all(p.startswith("127.0.0.1:") for p in others)


def test_every_service_has_limits_healthcheck_and_service_label(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    for name, service in all_services(data, modules).items():
        assert service["mem_limit"], name
        assert service["cpus"] > 0, name
        assert service["healthcheck"]["test"], name
        assert service["labels"]["io.ayeops.service"] == name.split("/")[1]


def test_every_service_runs_on_the_host_architecture(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    for name, service in all_services(data, modules).items():
        assert service["platform"] == f"linux/{data['hosts'][name.split('/')[0]]['arch']}"


def test_module_images_are_pinned_to_an_exact_version_and_an_index_digest(
    modules: dict[str, Module],
) -> None:
    images = set()
    for module in modules.values():
        text = module.template("compose.yaml.j2")
        images.update(re.findall(r"image:\s*(\S+)", text))
    assert len(images) >= 38
    assert sorted(i for i in images if not PINNED.match(i) or "latest" in i) == []


def test_one_tag_has_one_digest_across_the_module_templates(modules: dict[str, Module]) -> None:
    digests: dict[str, set[str]] = {}
    for module in modules.values():
        for image in re.findall(r"image:\s*(\S+)", module.template("compose.yaml.j2")):
            tagged, _, digest = image.partition("@")
            digests.setdefault(tagged, set()).add(digest)
    assert {tagged: found for tagged, found in digests.items() if len(found) > 1} == {}


@pytest.mark.parametrize("path", [EXAMPLE, QUICKSTART], ids=["acme", "quickstart"])
def test_every_rendered_image_carries_an_exact_tag_and_a_digest(
    path: Path, modules: dict[str, Module]
) -> None:
    tree = render_once(load_manifest(path), modules, EXAMPLE_STAMP)
    composes = [text for name, text in tree.items() if name.endswith("/compose.yaml")]
    images = {image for text in composes for image in RENDERED_IMAGE.findall(text)}
    services = sum(text.count("\n    image: ") for text in composes)
    assert sum(len(RENDERED_IMAGE.findall(text)) for text in composes) == services
    assert len(images) >= 5
    assert sorted(i for i in images if not PINNED.match(i) or "latest" in i) == []


def test_every_rendered_service_is_described_in_its_module_catalog(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    data["hosts"]["edge"]["modules"]["probes"] = {
        "http": ["https://a.example"],
        "domains": ["a.example"],
    }
    for service in all_services(data, modules).values():
        labels = service["labels"]
        assert labels["io.ayeops.service"] in modules[labels["io.ayeops.module"]].catalog


def test_memory_override_replaces_the_module_default(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    data["hosts"]["edge"]["modules"]["containers"]["resources"] = {
        "cadvisor": {"memory": "128m", "cpus": 0.2}
    }
    cadvisor = compose_of(data, "edge", modules)["services"]["cadvisor"]
    assert (cadvisor["mem_limit"], cadvisor["cpus"]) == ("128m", 0.2)


def test_retention_reaches_victoriametrics(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    command = compose_of(data, "central", modules)["services"]["victoriametrics"]["command"]
    assert "-retentionPeriod=90d" in command


def test_agent_host_remote_writes_to_the_central_address(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    edge = compose_of(data, "edge", modules)["services"]
    assert "victoriametrics" not in edge
    assert "-remoteWrite.url=http://10.8.0.1:8428/api/v1/write" in edge["vmagent"]["command"]
    assert "-remoteWrite.label=host=edge" in edge["vmagent"]["command"]


def test_server_host_writes_to_its_own_storage(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    command = compose_of(data, "central", modules)["services"]["vmagent"]["command"]
    assert "-remoteWrite.url=http://victoriametrics:8428/api/v1/write" in command


def test_battery_collector_only_runs_where_enabled(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    assert "battery" in compose_of(data, "edge", modules)["services"]
    assert "battery" not in compose_of(data, "central", modules)["services"]


def test_scrape_config_lists_the_enabled_exporters(modules: dict[str, Module]) -> None:
    tree = render_once(load_manifest(EXAMPLE), modules)
    jobs = {
        host: {
            j["job_name"]
            for j in yaml.safe_load(tree[f"{host}/vmagent/scrape.yaml"])["scrape_configs"]
        }
        for host in ("central", "edge")
    }
    assert jobs["edge"] == {
        "vmagent",
        "alloy",
        "node",
        "cadvisor",
        "smartctl",
        "gpu",
        "network",
        "sentinel",
        "blackbox",
        "blackbox-icmp",
        "blackbox-dns",
    }
    assert jobs["central"] == {
        "vmagent",
        "victoriametrics",
        "alloy",
        "loki",
        "node",
        "blackbox",
        "blackbox-http",
        "rdap",
        "grafana",
        "traefik",
        "oauth2-proxy",
        "cloudflare",
        "vmalert",
        "github",
        "ai-usage",
        "garage",
        "garage-admin",
        "grafana-reader",
        "cadvisor",
        "goalert",
        "oncall-bridge",
        "agentgateway",
        "mcp-grafana",
        "mcp-victoriametrics",
        "mcp-catalog",
        "autopilot",
        "incidents",
        "status-reporter",
    }


def test_service_alerts_link_the_project_runbooks_and_module_alerts_link_the_kit_runbooks(
    modules: dict[str, Module],
) -> None:
    tree = render_once(load_manifest(EXAMPLE), modules)
    rules = yaml.safe_load(tree["central/grafana/provisioning/alerting/rules.yaml"])
    urls = {
        rule["title"]: (rule["labels"], rule["annotations"]["runbook_url"])
        for group in rules["groups"]
        for rule in group["rules"]
    }
    assert urls["Website down"][1] == "https://runbooks.acme.example/web-down"
    duty = urls.pop(DUTY_TITLE)[1]
    assert duty == DUTY_RUNBOOK_LINK.replace("$", "$$")
    for labels, url in urls.values():
        if "module" in labels:
            assert url.startswith(MODULE_RUNBOOK_BASE) and url.endswith(".md")
        else:
            assert url.startswith("https://runbooks.acme.example/")


def test_every_module_runbook_link_has_its_document() -> None:
    names = {path.name for path in (ROOT / "docs" / "runbooks").glob("*.md")}
    for module in load_modules().values():
        for alert in module.alerts:
            assert f"{alert['runbook']}.md" in names


def test_alert_rules_cover_the_required_conditions(modules: dict[str, Module]) -> None:
    tree = render_once(load_manifest(EXAMPLE), modules)
    rules = yaml.safe_load(tree["central/grafana/provisioning/alerting/rules.yaml"])
    uids = {rule["uid"] for group in rules["groups"] for rule in group["rules"]}
    assert {
        "aye-host-down",
        "aye-disk-full",
        "aye-memory-low",
        "aye-battery-low",
        "aye-container-restarting",
        "aye-probe-failing",
        "aye-tls-expiring",
        "aye-domain-expiring-60d",
        "aye-domain-expiring-30d",
        "aye-domain-expiring-7d",
    } <= uids


def test_alert_rules_only_cover_enabled_modules(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    del data["hosts"]["edge"]["modules"]["containers"]
    for name in ("containers", "oncall"):
        del data["hosts"]["central"]["modules"][name]
    tree = render_once(manifest_from(data), modules)
    rules = yaml.safe_load(tree["central/grafana/provisioning/alerting/rules.yaml"])
    assert "containers" not in {group["name"] for group in rules["groups"]}


def test_alert_templates_carry_no_dollar_because_grafana_alerting_provisioning_keeps_it_literal(
    modules: dict[str, Module],
) -> None:
    text = render_once(load_manifest(EXAMPLE), modules)[
        "central/grafana/provisioning/alerting/rules.yaml"
    ]
    rules = yaml.safe_load(text)
    for group in rules["groups"]:
        for rule in group["rules"]:
            assert "$" not in rule["annotations"]["summary"]
    assert ".Labels." in text


def test_the_fast_yaml_loader_reads_every_rendered_file_like_safe_load() -> None:
    paths = sorted(p for p in RENDERED.rglob("*") if p.suffix in (".yaml", ".yml"))
    assert paths
    for path in paths:
        text = path.read_text(encoding="utf-8")
        assert load_yaml(text) == yaml.safe_load(text), path


def test_the_fast_yaml_loader_reads_every_module_spec_like_safe_load() -> None:
    paths = sorted(MODULES_DIR.glob("*/module.yaml"))
    assert paths
    for path in paths:
        text = path.read_text(encoding="utf-8")
        assert load_yaml(text) == yaml.safe_load(text), path
    assert {name: module.spec for name, module in load_modules().items()} == {
        spec["name"]: spec
        for spec in (yaml.safe_load(path.read_text(encoding="utf-8")) for path in paths)
    }


def python_dump(data: Any) -> str:
    return yaml.dump(
        data,
        Dumper=yaml.SafeDumper,
        sort_keys=True,
        default_flow_style=False,
        allow_unicode=True,
        width=1000,
    )


def test_the_fast_yaml_dumper_writes_every_rendered_file_like_the_python_emitter() -> None:
    paths = sorted(p for p in RENDERED.rglob("*") if p.suffix in (".yaml", ".yml"))
    assert paths
    for path in paths:
        data = load_yaml(path.read_text(encoding="utf-8"))
        assert dump_yaml(data) == python_dump(data), path


def test_the_fast_yaml_dumper_is_libyaml() -> None:
    assert FAST_DUMPER is yaml.CSafeDumper


def test_dump_yaml_writes_multi_line_quoted_and_env_values_like_the_python_emitter() -> None:
    data = {
        "multi": "first\nsecond\n",
        "quoted": 'it\'s: a "value"',
        "cyrillic": "дашборд ✅",
        "env": EnvRef("${AYE_TOKEN}"),
        "list": [1, 2.5, None, True, "x" * 200],
    }
    assert dump_yaml(data) == python_dump(data)
    assert "env: ${AYE_TOKEN}\n" in dump_yaml(data)


def test_dump_yaml_keeps_astral_characters_literal_where_libyaml_would_escape_them() -> None:
    data = {"template": "\U0001f534 <b>{{ x }}</b>\nnext line"}
    assert yaml.dump(data, Dumper=FAST_DUMPER, allow_unicode=True) != python_dump(data)
    assert dump_yaml(data) == python_dump(data)
    assert "\U0001f534" in dump_yaml(data)


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        ("plain", False),
        ("дашборд ✅", False),
        ("￿", False),
        ("\U00010000", True),
        ("x \U0001f534", True),
        ({"key": "\U0001f534"}, True),
        ({"\U0001f534": "value"}, True),
        ({"key": "value"}, False),
        (["a", ["\U0001f534"]], True),
        (["a", "b"], False),
        (("a", "\U0001f534"), True),
        (7, False),
        (None, False),
    ],
)
def test_astral_finds_characters_beyond_the_basic_plane(data: Any, expected: bool) -> None:
    assert astral(data) is expected


def test_one_host_context_serves_every_module_of_the_host(modules: dict[str, Module]) -> None:
    manifest = load_manifest(EXAMPLE)
    for host in sorted(manifest.hosts):
        prepared = host_render(manifest, host, modules)
        assert host_compose(manifest, host, modules, prepared) == host_compose(
            manifest, host, modules
        )
        assert host_scrape(manifest, host, modules, prepared) == host_scrape(
            manifest, host, modules
        )
        for settings in prepared.settings.values():
            shared = module_context(
                manifest, host, settings, prepared.settings, shared=prepared.context
            )
            reader = grafana_reader.consumed(prepared.settings, modules)
            fresh = module_context(manifest, host, settings, prepared.settings, reader)
            assert shared == fresh
            assert shared["settings"] is settings


def test_a_fleet_render_builds_each_host_once_and_matches_the_helpers(
    modules: dict[str, Module],
) -> None:
    manifest = load_manifest(EXAMPLE)
    renders = FleetRender(manifest, modules)
    for host in sorted(manifest.hosts):
        assert renders.settings(host) == host_settings(manifest, host, modules)
        assert renders.host(host).settings is renders.settings(host)
        assert renders.host(host) is renders.host(host)
        assert renders.compose(host) == host_compose(manifest, host, modules)
        assert renders.compose(host) is renders.compose(host)
        assert renders.scrape(host) == host_scrape(manifest, host, modules)
        assert renders.scrape(host) is renders.scrape(host)


def test_a_host_without_metrics_has_no_scrape_in_the_fleet_render(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    for host in data["hosts"].values():
        host["modules"].pop("metrics", None)
    manifest = manifest_from(data)
    renders = FleetRender(manifest, modules)
    for host in sorted(manifest.hosts):
        assert renders.scrape(host) is None
        assert host_scrape(manifest, host, modules) is None


def counted_compose(monkeypatch: pytest.MonkeyPatch) -> Counter[str]:
    calls: Counter[str] = Counter()
    real = render.host_compose

    def counting(
        manifest: Any, host: str, modules: dict[str, Module], prepared: Any = None
    ) -> dict[str, Any]:
        calls[host] += 1
        return real(manifest, host, modules, prepared)

    monkeypatch.setattr(render, "host_compose", counting)
    return calls


def test_check_renders_each_host_once(
    modules: dict[str, Module], monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = load_manifest(EXAMPLE)
    calls = counted_compose(monkeypatch)
    assert check_manifest(manifest, modules) == []
    assert calls == dict.fromkeys(manifest.hosts, 1)


def test_the_portal_pages_reuse_the_fleet_render(
    modules: dict[str, Module], monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = load_manifest(EXAMPLE)
    calls = counted_compose(monkeypatch)
    renders = FleetRender(manifest, modules)
    pages = portal_pages(manifest, modules, EXAMPLE_STAMP, renders=renders)
    assert pages == portal_pages(manifest, modules, EXAMPLE_STAMP)
    assert calls == dict.fromkeys(manifest.hosts, 2)


def test_runbook_url_joins_onto_an_anchor_base() -> None:
    assert runbook_url("https://a.example/doc.md#", "x") == "https://a.example/doc.md#x"


def test_runbook_url_keeps_absolute_links() -> None:
    assert runbook_url("https://a.example/", "x") == "https://a.example/x"
    assert runbook_url("https://a.example", "https://b.example/y") == "https://b.example/y"


def test_env_example_documents_every_variable_the_compose_files_use(
    modules: dict[str, Module],
) -> None:
    tree = render_once(load_manifest(EXAMPLE), modules)
    documented = {
        line.split("=", 1)[0]
        for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
        if line
    }
    used = {
        name
        for path, text in tree.items()
        if path.endswith("compose.yaml")
        for name in ENV_REFERENCE.findall(text)
    }
    assert used
    assert used - documented == set()


def test_telegram_alerts_use_the_short_template_not_the_label_dump(
    modules: dict[str, Module],
) -> None:
    text = render_once(load_manifest(EXAMPLE), modules)[
        "central/grafana/provisioning/alerting/contact-points.yaml"
    ]
    data = yaml.safe_load(text)
    settings = data["contactPoints"][0]["receivers"][0]["settings"]
    assert settings["message"] == '{{ template "aye.telegram" . }}'
    assert settings["parse_mode"] == "HTML"
    template = data["templates"][0]["template"]
    assert '{{ define "aye.telegram" }}' in template
    assert "$" not in template


def rules_by_uid(modules: dict[str, Module]) -> dict[str, dict[str, Any]]:
    tree = render_once(load_manifest(EXAMPLE), modules)
    rules = yaml.safe_load(tree["central/grafana/provisioning/alerting/rules.yaml"])
    return {rule["uid"]: rule for group in rules["groups"] for rule in group["rules"]}


def test_service_alert_fires_when_its_metric_disappears(modules: dict[str, Module]) -> None:
    rule = rules_by_uid(modules)["aye-web-down"]
    assert rule["noDataState"] == "Alerting"
    assert rule["execErrState"] == "Alerting"


def test_battery_alert_stays_quiet_on_a_host_without_a_battery(
    modules: dict[str, Module],
) -> None:
    rule = rules_by_uid(modules)["aye-battery-low"]
    assert rule["noDataState"] == "OK"
    assert rule["execErrState"] == "Error"


def test_host_down_and_target_down_fire_on_silence_because_absence_is_their_signal(
    modules: dict[str, Module],
) -> None:
    rules = rules_by_uid(modules)
    assert rules["aye-host-down"]["noDataState"] == "Alerting"
    assert rules["aye-target-down"]["noDataState"] == "Alerting"


def test_module_alerts_declare_no_data_as_alerting_or_ok(modules: dict[str, Module]) -> None:
    for module in modules.values():
        for alert in module.alerts:
            assert alert.get("no_data", "ok") in {"alerting", "ok"}, alert["name"]


def test_vmagent_joins_the_external_networks_its_host_lists(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    data["hosts"]["edge"]["modules"].pop("image-updates")
    data["hosts"]["edge"]["modules"].pop("vulns")
    data["hosts"]["edge"]["modules"]["metrics"]["networks"] = ["app_default"]
    compose = compose_of(data, "edge", modules)
    assert compose["services"]["vmagent"]["networks"] == ["default", "app_default"]
    assert compose["networks"] == {"default": {}, "app_default": {"external": True}}


def test_without_networks_vmagent_stays_on_the_project_network_only(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    data["hosts"]["edge"]["modules"].pop("image-updates")
    data["hosts"]["edge"]["modules"].pop("vulns")
    compose = compose_of(data, "edge", modules)
    assert "networks" not in compose["services"]["vmagent"]
    assert "networks" not in compose
