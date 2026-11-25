from __future__ import annotations

import json
import re
from typing import Any

import pytest
import yaml

from ayeops.manifest import load_manifest
from ayeops.portal.graph import build_graph
from ayeops.portal.icons import (
    CLOUD,
    HOST,
    MCP,
    SERVICE,
    host_icon,
    icon_data,
    node_icons,
    service_icon,
)
from ayeops.portal.live import container_pattern, live_spec
from ayeops.portal.pages import DARK, LIGHT, PortalDrift, collect_cards, portal_pages
from ayeops.registry import Module
from tests.conftest import (
    EXAMPLE,
    EXAMPLE_STAMP,
    ROOT,
    english,
    manifest_from,
    render_once,
    rules_hit,
)

STATUS_TOKENS = ("s-live", "s-down", "s-none")


def pages(data: dict[str, Any], modules: dict[str, Module]) -> dict[str, str]:
    result = portal_pages(manifest_from(data), modules, EXAMPLE_STAMP)
    return {name: english(html) for name, html in result.items()}


def blob(html: str) -> dict[str, Any]:
    match = re.search(r'<script type="application/json" id="aye-live">(.*?)</script>', html)
    assert match
    data: dict[str, Any] = json.loads(match.group(1))
    return data


def nginx_locations(conf: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for match in re.finditer(r"location\s+([^{]+?)\s*\{", conf):
        depth, index = 1, match.end()
        while depth:
            depth += {"{": 1, "}": -1}.get(conf[index], 0)
            index += 1
        found[match.group(1).strip()] = conf[match.end() : index - 1]
    return found


def rendered_conf(modules: dict[str, Module]) -> str:
    tree = render_once(load_manifest(EXAMPLE), modules, EXAMPLE_STAMP)
    return tree["central/portal-nginx/default.conf"]


def test_module_services_get_the_icon_of_their_product(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    icons = node_icons(manifest_from(data), modules)
    assert icons["central/grafana"] == "grafana"
    assert icons["central/vmagent"] == "victoriametrics"
    assert icons["central/node-exporter"] == "prometheus"
    assert icons["central/traefik"] == "traefikproxy"
    assert icons["central/portal"] == "nginx"
    assert icons["edge/cadvisor"] == "docker"
    assert icons["external:telegram-api"] == "telegram"


def test_a_compose_service_is_guessed_from_its_image(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    assert node_icons(manifest_from(data), modules)["central/web"] == "nginx"
    assert service_icon("db", "docker.io/library/postgres:18.0") == "postgresql"
    assert service_icon("api", "node:22.11.0-alpine") == "nodedotjs"
    assert service_icon("node-exporter", "prom/node-exporter:v1.12.1") == "prometheus"


def test_the_manifest_icon_wins_over_the_guess(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    data["services"]["web"]["icon"] = "redis"
    assert node_icons(manifest_from(data), modules)["central/web"] == "redis"
    assert node_icons(manifest_from(data), modules)["central/mcp"] == MCP


def test_unknown_services_and_hosts_fall_back_to_neutral_glyphs() -> None:
    assert service_icon("worker", "ghcr.io/acme/worker:1.0.0") == SERVICE
    assert host_icon("box", {"arch": "amd64"}) == HOST
    assert host_icon("vm", {"provider": "oracle"}) == CLOUD
    assert host_icon("vm", {"provider": "oracle", "icon": "ubuntu"}) == "ubuntu"


def test_an_unknown_icon_in_the_manifest_is_rejected(data: dict[str, Any]) -> None:
    data["services"]["web"]["icon"] = "no-such-product"
    assert "icon-known" in rules_hit(data)


def test_icons_are_inlined_once_and_referenced_without_external_requests(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    for html in pages(data, modules).values():
        symbols = re.findall(r'<symbol id="i-([a-z]+)"', html)
        assert len(symbols) == len(set(symbols))
        used = set(re.findall(r'href="#i-([a-z]+)"', html))
        assert used <= set(symbols)
        assert {"grafana", "nginx", MCP} <= used
        assert not re.search(r'<(img|link|script)[^>]+(src|href)="https?://', html)


def test_vendored_icons_record_source_version_and_cc0_license() -> None:
    data = icon_data()
    assert data["license"] == "CC0-1.0"
    assert re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", data["version"])
    notice = (ROOT / "NOTICE").read_text(encoding="utf-8")
    assert f"Simple Icons {data['version']}" in notice
    assert "CC0" in notice


def test_the_portal_nginx_proxies_only_the_two_read_only_query_paths(
    modules: dict[str, Module],
) -> None:
    locations = nginx_locations(rendered_conf(modules))
    proxied = {path: body for path, body in locations.items() if "proxy_pass" in body}
    assert set(proxied) == {"= /api/v1/query", "= /api/v1/query_range"}
    for path, body in proxied.items():
        upstream = re.search(r"proxy_pass\s+http://\$metrics(/\S+?)\$is_args\$args;", body)
        assert upstream and upstream.group(1) == path.removeprefix("= ")
        assert re.search(r"limit_except GET\s*\{\s*deny all;\s*\}", body)
    assert "return 403;" in locations["/api/"]
    assert "set $metrics victoriametrics:8428;" in proxied["= /api/v1/query"]


def test_the_portal_container_mounts_the_proxy_config(modules: dict[str, Module]) -> None:
    tree = render_once(load_manifest(EXAMPLE), modules, EXAMPLE_STAMP)
    portal = yaml.safe_load(tree["central/compose.yaml"])["services"]["portal"]
    assert "./portal-nginx/default.conf:/etc/nginx/conf.d/default.conf:ro" in portal["volumes"]
    assert "portal/portal-nginx/default.conf" not in tree


def test_the_portal_needs_a_metrics_server_on_its_host(data: dict[str, Any]) -> None:
    data["hosts"]["central"]["modules"]["metrics"] = {
        "role": "agent",
        "remote_write": {"url": "https://metrics.example/api/v1/write"},
    }
    for server_only in (
        "grafana",
        "delivery",
        "github",
        "oncall",
        "incidents",
        "status",
        "autopilot",
    ):
        del data["hosts"]["central"]["modules"][server_only]
    assert "portal-metrics" in rules_hit(data)


def test_the_example_portal_passes_the_metrics_rule(data: dict[str, Any]) -> None:
    assert "portal-metrics" not in rules_hit(data)


def test_live_queries_cover_every_declared_service_and_host(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    manifest = manifest_from(data)
    spec = blob(pages(data, modules)["services.html"])
    cards = collect_cards(manifest, modules, build_graph(manifest, modules))
    assert set(spec["services"]) == {f"{card.host}/{card.name}" for card in cards}
    assert set(spec["hosts"]) == set(data["hosts"])
    units = {
        f"{host}/{name}"
        for host, entry in data["hosts"].items()
        for name in entry.get("units") or {}
    }
    for key, service in spec["services"].items():
        assert service["up"] or service["probe"] or service["container"] or key in units, key
    for host in data["hosts"]:
        assert re.search(rf'host=~"[^"]*\b{host}\b', spec["queries"]["host_cpu"]), host
    assert {"up", "probe", "latency", "seen", "mem", "cpu", "host_disk_days"} <= set(
        spec["queries"]
    )
    assert "predict_linear" in spec["queries"]["host_disk_days"]
    assert set(spec["ranges"]) == {"host_cpu", "latency", "fs_used"}


def test_a_service_ties_to_its_scrape_target_probe_and_container(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    services = blob(pages(data, modules)["index.html"])["services"]
    assert services["central/grafana"]["up"] == ["grafana:3000"]
    assert services["central/grafana"]["probe"] == ["https://grafana.ops.acme.example"]
    assert services["central/web"]["probe"] == ["https://acme.example"]
    assert services["central/web"]["mem_limit"] == 64 * 1024**2
    assert services["edge/vmagent"]["container"] == "acme-edge[-_]vmagent[-_][0-9]+"
    assert services["central/web"]["container"] == "acme-apps[-_]web[-_][0-9]+"
    for name in ("containers", "oncall"):
        del data["hosts"]["central"]["modules"][name]
    without = blob(pages(data, modules)["index.html"])["services"]
    assert without["central/web"]["container"] is None


def test_container_patterns_match_compose_container_names() -> None:
    assert re.fullmatch(container_pattern("acme-edge", "vmagent", {}), "acme-edge-vmagent-1")
    assert re.fullmatch(container_pattern("acme-apps", "web", {}), "acme-apps-web-2")
    assert not re.fullmatch(container_pattern("acme-apps", "web", {}), "acme-apps-webhook-1")
    assert re.fullmatch(container_pattern("x", "web", {"container_name": "site.1"}), "site.1")
    assert not re.fullmatch(container_pattern("x", "web", {"container_name": "site.1"}), "siteX1")


def test_a_service_without_any_signal_is_still_listed_for_no_data(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    spec = live_spec(manifest_from(data), modules, [("central", "ghost")], 0)
    assert spec["services"]["central/ghost"] == {
        "host": "central",
        "up": [],
        "probe": [],
        "container": None,
        "mem_limit": None,
        "cpus": None,
    }


def test_the_page_renders_the_static_view_with_live_parts_hidden(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    result = pages(data, modules)
    for html in result.values():
        body = html[html.index("<main") : html.index("</main>")]
        assert '<div class="live-strip" hidden>' in body
        live_parts = re.findall(r'<div class="(?:hlive|slive)"[^>]*>', body)
        assert live_parts
        assert all(part.endswith(" hidden>") for part in live_parts)
        assert "data-st=" not in body
    assert 'class="chip"' in result["index.html"]

    assert result["services.html"].count('<div class="slive"') == len(
        blob(result["index.html"])["services"]
    )


def test_the_live_strip_counts_down_first_and_keeps_the_red_count(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    html = pages(data, modules)["index.html"]
    strip = re.search(r'<div class="live-strip" hidden>(.*?)</div>', html)
    assert strip
    order = re.findall(r'<a class="n ([a-z]+)" href="services\.html#filter=\1">', strip.group(1))
    assert order == ["down", "live", "nodata", "red"]
    assert blob(html)["red"] == 0


def test_the_live_blob_is_deterministic_and_carries_no_live_values(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    first, second = pages(data, modules), pages(data, modules)
    assert first == second
    text = json.dumps(blob(first["index.html"]))
    assert EXAMPLE_STAMP.time not in text
    assert '"value"' not in text


def test_the_page_script_polls_every_interval_and_pauses_when_hidden(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    html = pages(data, modules)["index.html"]
    assert blob(html)["interval"] == 30
    assert "visibilitychange" in html
    assert "document.hidden" in html
    assert "'api/v1/' + path" in html
    assert "live data unavailable" in html


def test_the_disk_tile_takes_its_colour_from_the_disks_row_thresholds(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    html = pages(data, modules)["index.html"]
    assert "pct(disk) + soon, soon ? daysLevel(days) : ''" in html
    assert "class=\"days ' + daysLevel(fs.days)" in html
    assert {"days_warn", "days_crit"} <= set(blob(html)["disks"])


def test_status_colours_are_separate_from_the_category_palette() -> None:
    for theme in (LIGHT, DARK):
        status = {theme[token] for token in STATUS_TOKENS}
        categories = {value for name, value in theme.items() if name.startswith("c-")}
        assert len(status) == 3
        assert not status & categories


def test_clients_of_a_service_are_drawn_as_incoming_flows(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    index = pages(data, modules)["index.html"]
    flows = index[index.index('id="flows"') :]
    assert 'data-a="external:ai-assistants" data-b="central/mcp"' in flows


def test_an_unknown_client_fails_the_build_and_the_check(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    data["services"]["mcp"]["clients"] = ["external:nobody"]
    with pytest.raises(PortalDrift) as error:
        pages(data, modules)
    assert any(p.startswith("services.mcp.clients.0") for p in error.value.problems)
    assert "client-endpoint" in rules_hit(data)


def test_an_mcp_endpoint_is_labelled_and_shows_its_public_url(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    result = pages(data, modules)
    card = re.search(
        r'<article class="svc[^"]*" id="svc-central-mcp".*?</article>', result["services.html"]
    )
    assert card
    assert '<span class="fs mcp">MCP</span>' in card.group(0)
    assert '<a class="open" href="https://mcp.acme.example"' in card.group(0)
    assert '<div class="row"><span>public</span>' in card.group(0)
    assert re.search(r'data-node="central/mcp".*?<text class="s"[^>]*>MCP · ', result["index.html"])
