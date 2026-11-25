from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml

from ayeops.cli import main
from ayeops.manifest import load_manifest
from ayeops.portal.diagram import (
    COLLECTORS,
    DASHBOARDS,
    INGRESS,
    SOURCES,
    STORAGE,
    system_scene,
)
from ayeops.portal.graph import affects, build_graph, drift
from ayeops.portal.icons import SERVICE, badge, brand_css, monogram
from ayeops.portal.layout import Edge
from ayeops.portal.pages import DARK, LIGHT, PortalDrift, asset, portal_pages, theme_css
from ayeops.registry import Module
from ayeops.render import stack_name
from ayeops.stamp import Stamp, build_time, current_stamp
from tests.conftest import EXAMPLE, EXAMPLE_STAMP, english, manifest_from, render_once, rules_hit


def pages(
    data: dict[str, Any], modules: dict[str, Module], discovered: list[dict[str, Any]] | None = None
) -> dict[str, str]:
    result = portal_pages(manifest_from(data), modules, EXAMPLE_STAMP, discovered)
    return {name: english(html) for name, html in result.items()}


def card(html: str, host: str, name: str) -> str:
    anchor = f"svc-{host}-{re.escape(name)}"
    match = re.search(rf'<article class="svc[^"]*" id="{anchor}".*?</article>', html)
    assert match, f"no card for {host}/{name}"
    return match.group(0)


def web(data: dict[str, Any]) -> dict[str, Any]:
    service: dict[str, Any] = data["services"]["web"]
    return service


def test_example_portal_has_both_pages(data: dict[str, Any], modules: dict[str, Module]) -> None:
    result = pages(data, modules)
    assert set(result) == {"index.html", "services.html"}
    assert "Where things run" in result["index.html"]
    assert "<h1>Services</h1>" in result["services.html"]


def test_render_puts_the_portal_next_to_the_compose_file_of_its_host(
    modules: dict[str, Module],
) -> None:
    tree = render_once(load_manifest(EXAMPLE), modules, EXAMPLE_STAMP)
    assert "central/portal/index.html" in tree
    assert "edge/portal/index.html" not in tree
    portal = yaml.safe_load(tree["central/compose.yaml"])["services"]["portal"]
    assert portal["mem_limit"] == "32m"
    assert portal["ports"] == ["127.0.0.1:8088:8080"]
    assert "./portal:/usr/share/nginx/html:ro" in portal["volumes"]


def test_portal_needs_an_ingress_route_on_its_host(data: dict[str, Any]) -> None:
    del data["hosts"]["central"]["modules"]["ingress"]["routes"]["portal"]
    assert "portal-route" in rules_hit(data)


def test_footer_shows_the_injected_build_time_and_commit(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    html = portal_pages(manifest_from(data), modules, Stamp("2030-05-06 07:08 UTC", "abc1234+"))
    assert "built 2030-05-06 07:08 UTC" in html["index.html"]
    assert "<code>abc1234+</code>" in html["services.html"]


def test_build_time_honours_source_date_epoch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "0")
    assert build_time() == "1970-01-01 00:00 UTC"


def test_explicit_stamp_values_win_over_git_and_clock(tmp_path: Path) -> None:
    assert current_stamp(tmp_path / "m.yaml", "t", "c") == Stamp("t", "c")


def test_a_flow_to_an_unknown_endpoint_fails_the_build(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    data["flows"].append({"from": "web", "to": "nowhere"})
    with pytest.raises(PortalDrift) as error:
        pages(data, modules)
    assert any(problem.startswith("flows.3.to") for problem in error.value.problems)
    assert "flow-endpoint" in rules_hit(data)


def test_a_flow_endpoint_on_several_hosts_asks_for_the_host(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    data["flows"].append({"from": "vmagent", "to": "web"})
    with pytest.raises(PortalDrift) as error:
        pages(data, modules)
    assert any("central/vmagent, edge/vmagent" in p for p in error.value.problems)


def test_an_edge_to_an_unknown_node_is_drift(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    nodes = build_graph(manifest_from(data), modules).nodes
    assert drift(nodes, [Edge("central/web", "central/ghost")]) == [
        "edge central/web -> central/ghost: unknown node 'central/ghost'"
    ]


def test_the_cli_lists_the_drift_and_exits_non_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    manifest = copy_example(tmp_path)
    data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    data["flows"].append({"from": "web", "to": "nowhere"})
    manifest.write_text(yaml.safe_dump(data), encoding="utf-8")
    assert main(["portal", str(manifest), "-o", str(tmp_path / "out")]) == 1
    assert "error [portal-drift] flows.3.to" in capsys.readouterr().err
    assert not (tmp_path / "out").exists()


def test_an_ingress_route_without_a_node_shows_red_instead_of_vanishing(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    data["hosts"]["central"]["modules"]["ingress"]["routes"]["ghost"] = "ghost:80"
    index = pages(data, modules)["index.html"]
    assert "ingress route ghost targets ghost, which nothing on central runs" in index
    assert 'class="host red"' in index
    assert "ingress-route" in rules_hit(data)


def copy_example(tmp_path: Path) -> Path:
    shutil.copytree(EXAMPLE.parent / "apps", tmp_path / "apps")
    target = tmp_path / "acme.yaml"
    shutil.copy(EXAMPLE, target)
    return target


def test_the_services_page_never_contains_values_from_the_env_file(tmp_path: Path) -> None:
    manifest = copy_example(tmp_path)
    secrets = {
        "AYE_GRAFANA_ADMIN_PASSWORD": "hunter2-grafana-secret",
        "AYE_TELEGRAM_BOT_TOKEN": "123456:telegram-secret-token",
        "AYE_CLOUDFLARE_DNS_API_TOKEN": "cloudflare-secret-token",
    }
    env = "".join(f"{key}={value}\n" for key, value in secrets.items())
    for folder in (tmp_path, tmp_path / "central", tmp_path / "apps"):
        folder.mkdir(exist_ok=True)
        (folder / ".env").write_text(env, encoding="utf-8")
    out = tmp_path / "out"
    stamp = ["--build-time", "t", "--commit", "c"]
    assert main(["portal", str(manifest), "-o", str(out), *stamp]) == 0
    html = (out / "services.html").read_text(encoding="utf-8")
    html += (out / "index.html").read_text(encoding="utf-8")
    assert [value for value in secrets.values() if value in html] == []
    assert ".env on central" in html
    assert "AYE_GRAFANA_ADMIN_PASSWORD" in html


def test_light_and_dark_themes_define_the_same_tokens() -> None:
    assert set(LIGHT) == set(DARK)
    css = theme_css()
    assert '@media (prefers-color-scheme:dark){:root:not([data-theme="light"])' in css
    assert ':root[data-theme="dark"]' in css
    dark_blocks = css.split("\n")[1:3]
    assert all(f"--{token}:" in block for token in DARK for block in dark_blocks)


def test_every_token_the_stylesheet_uses_is_defined() -> None:
    used = set(re.findall(r"var\(--([a-z0-9-]+)", asset("style.css")))
    defined = set(LIGHT) | {
        "f-display",
        "f-body",
        "f-mono",
        "c",
        "t",
        "w",
        "h",
        "z",
        "ar",
        "b",
        "b2",
        "pc",
    }
    assert used - defined == set()


def test_reduced_motion_hides_the_dots_and_pauses_the_animation(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    index = pages(data, modules)["index.html"]
    assert "@media (prefers-reduced-motion:reduce){svg .dot{display:none}" in index
    script = asset("client.js")
    reduced = script.index("prefers-reduced-motion: reduce")
    assert script.index("pauseAnimations", reduced) < script.index("IntersectionObserver", reduced)


def test_dots_move_only_on_live_edges_and_planned_flows_are_dashed(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    index = pages(data, modules)["index.html"]
    flows = index[index.index('id="flows"') :]
    planned = re.search(r'<g class="edge dash"[^>]*><path id="(flows-e\d+)"', flows)
    assert planned
    assert f'data-e="{planned.group(1)}"' not in flows
    assert "<animateMotion" in flows


def test_the_example_has_no_red(data: dict[str, Any], modules: dict[str, Module]) -> None:
    services = pages(data, modules)["services.html"]
    assert '<a class="n red" href="services.html#filter=red"><b>0</b> red</a>' in services
    assert 'class="svc red"' not in services


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        (lambda s: s.pop("description"), "service has no description"),
        (lambda s: s.pop("alerts"), "service has no alert"),
        (lambda s: s["alerts"][0].pop("runbook"), "alert has no runbook"),
        (lambda s: s.pop("status"), "service needs public: &lt;component&gt; or internal"),
    ],
)
def test_an_undescribed_service_shows_red_with_the_reason(
    data: dict[str, Any], modules: dict[str, Module], change: Any, reason: str
) -> None:
    change(web(data))
    result = pages(data, modules)
    web_card = card(result["services.html"], "central", "web")
    assert web_card.startswith('<article class="svc red"')
    assert reason in web_card
    assert (
        '<a class="n red" href="services.html#filter=red"><b>1</b> red</a>'
        in result["services.html"]
    )
    assert 'class="node red"' in result["index.html"]


def test_relative_runbooks_without_a_base_url_show_red(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    del data["runbook_base_url"]
    web_card = card(pages(data, modules)["services.html"], "central", "web")
    assert "runbook_base_url is not set" in web_card


def test_a_compose_service_missing_from_the_catalog_shows_red(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    del data["services"]
    data.pop("flows")
    web_card = card(pages(data, modules)["services.html"], "central", "web")
    assert "not in the services catalog" in web_card


def test_a_host_without_node_metrics_shows_red(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    del data["hosts"]["edge"]["modules"]["node"]
    result = pages(data, modules)
    assert "no node metrics: enable the node module" in result["index.html"]
    assert "1 host" in result["services.html"]


def test_the_summary_puts_the_red_count_first(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    services = pages(data, modules)["services.html"]
    strip = services[services.index('<div class="summary') :]
    assert strip.index("red</a>") < strip.index("services</a>")
    assert strip.index("services</a>") < strip.index("described</a>")


def test_an_internal_service_with_a_route_links_its_internal_url(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    grafana = card(pages(data, modules)["services.html"], "central", "grafana")
    assert 'href="https://grafana.ops.acme.example"' in grafana
    assert "Open ↗" in grafana
    assert "amber" not in grafana


def test_an_internal_service_without_a_route_shows_the_tunnel_and_amber(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    exporter = card(pages(data, modules)["services.html"], "central", "node-exporter")
    assert exporter.startswith('<article class="svc amber"')
    assert "no internal URL" in exporter
    assert "ssh -N -L 9100:127.0.0.1:9100 10.8.0.1" in exporter


def test_alerts_on_a_card_carry_severity_and_runbook(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    web_card = card(pages(data, modules)["services.html"], "central", "web")
    assert '<span class="sev critical">critical</span>Website down' in web_card
    assert 'href="https://runbooks.acme.example/web-down"' in web_card


DISCOVERED = {
    "host": "central",
    "compose": [{"project": "acme-apps", "services": ["web", "redis"]}],
    "systemd": ["sshd.service"],
    "listening": [{"port": 8080, "process": "nginx"}, {"port": 6379, "process": "redis-server"}],
}


def test_discovered_services_absent_from_the_manifest_are_red_undeclared_cards(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    result = pages(data, modules, [DISCOVERED])
    services = result["services.html"]
    assert "undeclared: compose service redis runs on central" in card(services, "central", "redis")
    assert "undeclared: systemd unit sshd.service" in card(services, "central", "systemd:sshd")
    assert "redis-server listens on port 6379" in card(services, "central", "port:6379")
    assert 'id="svc-central-port:8080"' not in services
    assert not card(services, "central", "web").startswith('<article class="svc red"')
    assert '<a class="n red" href="services.html#filter=red"><b>3</b> red</a>' in services
    assert "undeclared" in result["index.html"]


def test_a_discovered_report_with_missing_keys_adds_nothing(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    services = pages(data, modules, [{"host": "edge"}])["services.html"]
    assert '<a class="n red" href="services.html#filter=red"><b>0</b> red</a>' in services


def test_a_discovered_host_missing_from_the_manifest_is_red(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    report = {"host": "stray", "compose": [{"services": ["app"]}]}
    services = pages(data, modules, [report])["services.html"]
    assert "the manifest has no such host" in services
    assert "undeclared: compose service app runs on stray" in services


def test_the_cli_reads_discovered_reports(tmp_path: Path) -> None:
    manifest = copy_example(tmp_path)
    report = tmp_path / "central.json"
    report.write_text(json.dumps(DISCOVERED), encoding="utf-8")
    out = tmp_path / "out"
    assert main(["portal", str(manifest), "-o", str(out), "--discovered", str(report)]) == 0
    assert "redis-server" in (out / "services.html").read_text(encoding="utf-8")


def test_an_unknown_dependency_is_rejected(data: dict[str, Any]) -> None:
    web(data)["depends_on"] = ["nowhere"]
    assert "depends-known" in rules_hit(data)


def test_an_external_dependency_must_be_declared_under_externals(data: dict[str, Any]) -> None:
    web(data)["depends_on"] = ["external:payments"]
    assert "depends-known" in rules_hit(data)
    data["externals"]["payments"] = {"title": "Payments API"}
    assert "depends-known" not in rules_hit(data)


def with_grafana_entry(data: dict[str, Any], depends_on: list[str]) -> None:
    entry = dict(web(data))
    entry.update(
        description="Grafana as the project sees it.",
        depends_on=depends_on,
        alerts=[{**web(data)["alerts"][0], "name": "grafana-down"}],
    )
    data["services"]["grafana"] = entry


def test_a_dependency_cycle_is_rejected(data: dict[str, Any]) -> None:
    web(data)["depends_on"] = ["central/grafana"]
    with_grafana_entry(data, ["web"])
    assert "depends-cycle" in rules_hit(data)


def test_a_service_affects_everything_that_depends_on_it_transitively(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    web(data)["depends_on"] = ["central/victoriametrics"]
    with_grafana_entry(data, ["web"])
    assert "depends-cycle" not in rules_hit(data)
    graph = build_graph(manifest_from(data), modules)
    assert affects(graph.depends, "central/victoriametrics") == ["central/grafana", "central/web"]
    services = pages(data, modules)["services.html"]
    store = card(services, "central", "victoriametrics")
    assert store.index("affects") < store.index('href="services.html#svc-central-grafana"')
    assert 'href="services.html#svc-central-web"' in store
    assert "depends on" in card(services, "central", "web")


def test_dependencies_are_drawn_in_their_own_diagram(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    index = pages(data, modules)["index.html"]
    depends = index[index.index('id="depends"') : index.index('id="flows"')]
    assert 'data-a="central/web" data-b="cloud:cdn"' in depends
    assert "<animateMotion" in depends


def test_a_compose_stack_is_named_by_the_manifest_then_the_file(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    manifest = manifest_from(data)
    assert stack_name(manifest, "apps/web.compose.yaml", "", {"name": "acme-apps"}) == "acme-apps"
    assert stack_name(manifest, "apps/web.compose.yaml", "", {}) == "web"
    assert stack_name(manifest, "../docker-compose.prod.yml", "", {}) == "acme prod"
    assert stack_name(manifest, "compose.yaml", "", {}) == "acme"
    data["hosts"]["central"]["compose"] = [{"file": "apps/web.compose.yaml", "name": "shop"}]
    assert "compose-file" not in rules_hit(data)
    services = pages(data, modules)["services.html"]
    assert "stack shop" in card(services, "central", "web")
    assert "../" not in services


def test_probed_urls_are_bundled_into_one_box_per_prober(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    index = pages(data, modules)["index.html"]
    system = index[index.index('id="system"') : index.index('id="depends"')]
    assert 'data-node="probes:central/blackbox"' in system
    assert system.count('data-a="central/blackbox"') == 1
    assert 'data-b="url:https://acme.example"' not in system
    assert ">acme.example</text>" in system


def test_monitoring_columns_follow_the_role_of_each_box(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    graph = build_graph(manifest_from(data), modules)
    scene = system_scene(graph, sorted(data["hosts"]), {})
    columns = scene.column_of or {}
    assert columns["central/node-exporter"] == SOURCES
    assert columns["central/vmagent"] == COLLECTORS
    assert columns["central/traefik"] == INGRESS
    assert columns["central/grafana"] == DASHBOARDS
    assert columns["central/victoriametrics"] == STORAGE
    assert scene.bands[0] == "outside"


def test_a_service_without_a_product_logo_gets_a_monogram() -> None:
    assert monogram("opendata-api") == "OA"
    assert monogram("watchdog") == "W"
    assert 'class="lgo mono"' in badge(SERVICE, "watchdog")
    assert "b-postgresql" in badge("postgresql", "db")
    css = brand_css({"postgresql", "modelcontextprotocol", SERVICE})
    assert ".b-postgresql{--b:#4169E1;" in css
    assert ".b-modelcontextprotocol{--b:#000000;--b2:#" in css
    assert ".b-service" not in css


def test_a_plain_wheel_scrolls_the_page_and_only_ctrl_wheel_zooms(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    script = asset("client.js")
    wheel = script[script.index("addEventListener('wheel'") :]
    wheel = wheel[: wheel.index("{ passive: false }")]
    guard = wheel.index("if (!event.ctrlKey && !event.metaKey) return;")
    assert guard < wheel.index("event.preventDefault()")
    assert wheel.count("preventDefault") == 1
    css = asset("style.css")
    assert re.search(r"\.canvas\{[^}]*overflow:hidden;touch-action:pan-y", css)
    assert re.search(r"\.frame\.full \.canvas\{[^}]*overflow:auto", css)
    assert "overscroll-behavior:contain}" not in css.split(".frame.full .canvas")[0]
    index = pages(data, modules)["index.html"]
    assert '<span class="hint" aria-hidden="true">Ctrl + wheel to zoom</span>' in index


def test_a_vertical_touch_swipe_is_left_to_the_page(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    script = asset("client.js")
    assert (
        "drag.touch && !frame.classList.contains('full') && Math.abs(dy) >= Math.abs(dx)" in script
    )
    assert "points.size === 2 && event.pointerType !== 'mouse'" in script


def test_the_services_page_has_quick_filters_with_counts(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    services = pages(data, modules)["services.html"]
    chips = services[services.index('<div class="qfs"') : services.index('<section class="group"')]
    assert (
        '<button type="button" class="qf" data-group="filter" data-value="all"'
        ' aria-pressed="true">All <b>'
    ) in chips
    for value in ("red", "described"):
        assert f'data-group="filter" data-value="{value}" aria-pressed="false">' in chips
    for value in ("down", "nodata", "live"):
        assert f'data-value="{value}" aria-pressed="false" hidden>' in chips
    for host in ("central", "edge"):
        assert f'data-group="host" data-value="{host}"' in chips
    assert 'data-group="kind" data-value="module"' in chips
    assert 'data-host="central" data-kind="module" data-state="live">' in services
    assert '<p class="qf-empty" hidden>Nothing matches' in services
    assert 'class="qf-clear">clear filters</button>' in services
    index = pages(data, modules)["index.html"]
    assert 'href="services.html#filter=described"' in index
    assert 'href="services.html#filter=down"' in index


def test_filters_live_in_the_url_hash_and_follow_live_status() -> None:
    script = asset("client.js")
    assert "window.addEventListener('hashchange'" in script
    assert "new URLSearchParams(text)" in script
    assert "history.replaceState(null, '', text ? '#' + text" in script
    assert "if (!text.includes('=')) return false;" in script
    assert "document.addEventListener('aye:live', applyFilters);" in script
    assert "chip.setAttribute('aria-pressed'" in script
    live = asset("live.js")
    assert live.count("document.dispatchEvent(new Event('aye:live'));") == 2
    assert live.index("setStrip(counts, T('js.updated'") < live.rindex("new Event('aye:live')")
