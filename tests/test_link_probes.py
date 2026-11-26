from __future__ import annotations

import json
from typing import Any

import pytest
import yaml

from ayeops.registry import Module
from tests.conftest import EXAMPLE_STAMP, manifest_from, render_once, rules_hit

RULES = "central/grafana/provisioning/alerting/rules.yaml"
LINK = 'probe_success{job="blackbox-link"}'
HOLD = (
    f"label_replace(max by (peer) ({LINK} unless on (zone) (max by (zone) ({LINK}) > 0)),"
    ' "host", "$1", "peer", "(.+)")'
)
SUNDAY_NIGHT = {"days": ["sun"], "start": "23:00", "duration": "3h"}


def central_probes(data: dict[str, Any]) -> dict[str, Any]:
    probes: dict[str, Any] = data["hosts"]["central"]["modules"]["probes"]
    return probes


def render(data: dict[str, Any], modules: dict[str, Module]) -> dict[str, str]:
    return render_once(manifest_from(data), modules, EXAMPLE_STAMP)


def link_job(tree: dict[str, str]) -> dict[str, Any] | None:
    jobs = yaml.safe_load(tree["central/vmagent/scrape.yaml"])["scrape_configs"]
    return next((job for job in jobs if job["job_name"] == "blackbox-link"), None)


def rules_of(tree: dict[str, str]) -> dict[str, dict[str, Any]]:
    groups = yaml.safe_load(tree[RULES])["groups"]
    return {rule["uid"]: rule for group in groups for rule in group["rules"]}


def expr(rule: dict[str, Any]) -> str:
    return str(rule["data"][0]["model"]["expr"]).replace("$$", "$")


def test_the_example_pings_the_private_address_of_edge_from_central(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    tree = render(data, modules)
    job = link_job(tree)
    assert job == {
        "job_name": "blackbox-link",
        "metrics_path": "/probe",
        "params": {"module": ["link"]},
        "static_configs": [{"targets": ["10.8.0.2"], "labels": {"peer": "edge", "zone": "office"}}],
        "relabel_configs": [
            {"source_labels": ["__address__"], "target_label": "__param_target"},
            {"source_labels": ["__param_target"], "target_label": "instance"},
            {"target_label": "__address__", "replacement": "blackbox:9115"},
        ],
    }
    config = yaml.safe_load(tree["central/probes/blackbox.yml"])["modules"]
    assert config["link"] == {
        "prober": "icmp",
        "timeout": "5s",
        "icmp": {"preferred_ip_protocol": "ip4", "ip_protocol_fallback": True},
    }
    blackbox = yaml.safe_load(tree["central/compose.yaml"])["services"]["blackbox"]
    assert blackbox["sysctls"] == {"net.ipv4.ping_group_range": "0 2147483647"}
    assert link_job({"central/vmagent/scrape.yaml": tree["edge/vmagent/scrape.yaml"]}) is None


def test_the_link_alert_names_the_zone_and_pages(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    rule = rules_of(render(data, modules))["aye-link-down"]
    plain = f"max by (host, project, group, zone) ({LINK}) == bool 0"
    assert expr(rule).startswith(f"({plain}) unless on (host) (")
    assert rule["labels"]["severity"] == "critical"
    assert rule["labels"]["module"] == "probes"
    assert rule["for"] == "3m"
    assert rule["noDataState"] == "OK"
    assert rule["annotations"]["runbook_url"].endswith("/docs/runbooks/link-down.md")
    assert rule["annotations"]["__dashboardUid__"] == "ayeops-probes"
    assert rule["annotations"]["__panelId__"] == "5"
    assert "{{ .Labels.zone }}" in rule["annotations"]["summary"]


def test_host_down_waits_while_the_zone_of_its_host_is_unreachable(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    rules = rules_of(render(data, modules))
    plain = str(modules["node"].alerts[0]["expr"]).strip()
    assert expr(rules["aye-host-down"]) == (f"({plain}) unless on (host) ({HOLD}) or 0 * ({plain})")
    assert HOLD not in expr(rules["aye-disk-full"])


def test_a_peer_in_its_maintenance_window_drops_out_of_the_link_alert(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    data["hosts"]["edge"]["maintenance"] = [SUNDAY_NIGHT]
    rule = rules_of(render(data, modules))["aye-link-down"]
    window = "(vector(time() - 342000) % 604800 < 10800)"
    assert expr(rule).startswith(
        f"(max by (host, project, group, zone) ({LINK} unless on (peer)"
        f' (label_replace({window}, "peer", "edge", "", ""))) == bool 0) unless on (host) ('
    )


def test_without_links_nothing_pings_and_host_down_stays_plain(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    del central_probes(data)["links"]
    tree = render(data, modules)
    assert link_job(tree) is None
    assert "link" not in yaml.safe_load(tree["central/probes/blackbox.yml"])["modules"]
    assert "sysctls" not in yaml.safe_load(tree["central/compose.yaml"])["services"]["blackbox"]
    rules = rules_of(tree)
    assert "aye-link-down" not in rules
    assert expr(rules["aye-host-down"]) == str(modules["node"].alerts[0]["expr"]).strip()
    board = json.loads(tree["central/grafana/dashboards/probes.json"])
    assert {panel["id"] for panel in board["panels"]} == {1, 2, 3, 4}


def test_the_probes_dashboard_shows_every_link_with_links(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    board = json.loads(render(data, modules)["central/grafana/dashboards/probes.json"])
    panels = {panel["id"]: panel for panel in board["panels"]}
    assert panels[5]["targets"][0]["expr"] == f"max by (host, zone, peer) ({LINK})"
    assert panels[6]["targets"][0]["expr"] == (
        'probe_icmp_duration_seconds{job="blackbox-link",phase="rtt"}'
    )


def test_links_alone_run_the_blackbox(data: dict[str, Any], modules: dict[str, Module]) -> None:
    data["hosts"]["edge"]["modules"]["probes"] = {"links": ["central"]}
    tree = render(data, modules)
    assert "blackbox" in yaml.safe_load(tree["edge/compose.yaml"])["services"]
    jobs = yaml.safe_load(tree["edge/vmagent/scrape.yaml"])["scrape_configs"]
    link = next(job for job in jobs if job["job_name"] == "blackbox-link")
    assert link["static_configs"] == [
        {"targets": ["10.8.0.1"], "labels": {"peer": "central", "zone": "datacenter"}}
    ]
    assert set(yaml.safe_load(tree["edge/probes/blackbox.yml"])["modules"]) == {"http_2xx", "link"}


def test_the_example_with_links_passes_the_rules(data: dict[str, Any]) -> None:
    assert rules_hit(data) == set()
    central_probes(data)["links"] = ["edge"]
    assert rules_hit(data) == set()
    data["hosts"]["edge"]["volatile"] = True
    central_probes(data)["links"] = True
    assert rules_hit(data) == set()


def test_links_to_no_host_with_an_address_render_no_alert_and_no_hold(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    data["hosts"]["edge"].pop("address")
    tree = render(data, modules)
    assert link_job(tree) is None
    rules = rules_of(tree)
    assert "aye-link-down" not in rules
    assert expr(rules["aye-host-down"]) == str(modules["node"].alerts[0]["expr"]).strip()


@pytest.mark.parametrize(
    ("links", "edge"),
    [
        (["nowhere"], {}),
        (["central"], {}),
        (["edge"], {"address": None}),
        (["edge"], {"volatile": True}),
    ],
)
def test_a_link_to_no_reachable_host_fails_its_rule(
    data: dict[str, Any], links: Any, edge: dict[str, Any]
) -> None:
    central_probes(data)["links"] = links
    for key, value in edge.items():
        if value is None:
            data["hosts"]["edge"].pop(key)
        else:
            data["hosts"]["edge"][key] = value
    assert "probe-links" in rules_hit(data)


@pytest.mark.parametrize("links", ["yes", ["edge", "edge"], ["Edge"], [1]])
def test_links_settings_are_validated(data: dict[str, Any], links: Any) -> None:
    central_probes(data)["links"] = links
    assert "module-settings" in rules_hit(data)
