from __future__ import annotations

import json
import re
from typing import Any

import pytest
import yaml

from ayeops.incidents import (
    WEIGHTS,
    criticality,
    host_services,
    night_hours,
    request_routes,
    scrape_seconds,
    service_criticality,
)
from ayeops.permissions import generated
from ayeops.registry import MODULES_DIR, Module
from ayeops.render import host_compose
from tests.conftest import manifest_from, render_once, rules_hit

MODULE = MODULES_DIR / "incidents"
SCRIPT = MODULE / "files" / "incidents_agent.py"
METRIC = re.compile(r"\b(ayeops_[a-z_]+?)(?:_bucket|_sum|_count)?\b")


def central(data: dict[str, Any]) -> dict[str, Any]:
    modules: dict[str, Any] = data["hosts"]["central"]["modules"]
    return modules


def agent_env(data: dict[str, Any], modules: dict[str, Module]) -> dict[str, str]:
    services = host_compose(manifest_from(data), "central", modules)["services"]
    env: dict[str, str] = services["incidents-agent"]["environment"]
    return env


def test_the_example_runs_the_journal_on_the_central_host(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    assert "incidents" in central(data)
    assert rules_hit(data) == set()
    service = host_compose(manifest_from(data), "central", modules)["services"]["incidents-agent"]
    assert service["ports"] == ["127.0.0.1:9136:9136"]
    assert "incidents-journal:/tmp" in service["volumes"]
    assert service["read_only"] is True


def test_the_agent_reads_grafana_with_the_reader_token_not_the_admin(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    env = agent_env(data, modules)
    assert env["AYE_INCIDENTS_GRAFANA"] == "http://grafana:3000"
    assert env["AYE_GRAFANA_READER_TOKEN_FILE"] == "/run/grafana-reader/token"
    service = host_compose(manifest_from(data), "central", modules)["services"]["incidents-agent"]
    assert "AYE_GRAFANA_ADMIN" not in json.dumps(service)
    assert "PASSWORD" not in json.dumps(env)
    assert service["group_add"] == ["10470"]
    assert "grafana-reader:/run/grafana-reader:ro" in service["volumes"]
    assert service["depends_on"] == {"grafana-reader": {"condition": "service_healthy"}}
    assert modules["incidents"].catalog["incidents-agent"]["grafana_reader"] is True
    assert "grafana-reader-mount" not in rules_hit(data)


def test_services_carry_their_criticality_and_a_public_one_defaults_to_high(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    env = agent_env(data, modules)
    assert json.loads(env["AYE_INCIDENTS_SERVICES"]) == {
        "web": "critical",
        "mcp": "high",
        "acme-db": "high",
    }
    assert env["AYE_INCIDENTS_DOWNTIME_SEVERITIES"] == "critical,page"
    assert env["AYE_INCIDENTS_DEGRADED_SEVERITIES"] == "warning"


@pytest.mark.parametrize(
    ("entry", "level"),
    [
        ({"status": {"public": "Website"}}, "high"),
        ({"status": {"public": "Website"}, "criticality": "low"}, "low"),
        ({"status": {"internal": "tool"}}, "normal"),
        ({"criticality": "critical"}, "critical"),
        ({}, "normal"),
    ],
)
def test_criticality_defaults(entry: dict[str, Any], level: str) -> None:
    assert criticality(entry) == level


def test_planned_services_are_left_out(data: dict[str, Any]) -> None:
    data["services"]["web"]["state"] = "planned"
    assert "web" not in service_criticality(manifest_from(data))


def test_the_weights_are_fixed_tiers() -> None:
    assert WEIGHTS == {"critical": 100.0, "high": 10.0, "normal": 1.0, "low": 0.1}


def test_an_unknown_criticality_is_a_schema_error(data: dict[str, Any]) -> None:
    data["services"]["web"]["criticality"] = "urgent"
    assert "schema" in rules_hit(data)


def test_night_follows_the_oncall_quiet_hours(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    env = agent_env(data, modules)
    assert (env["AYE_INCIDENTS_NIGHT_START"], env["AYE_INCIDENTS_NIGHT_END"]) == ("23:00", "08:00")
    assert env["AYE_INCIDENTS_TIMEZONE"] == "Europe/Kyiv"


def test_the_own_night_setting_wins_and_without_oncall_night_is_23_to_8_utc() -> None:
    quiet = {"start": "22:00", "end": "06:00", "timezone": "Europe/Berlin"}
    own = {"start": "00:00", "end": "07:00", "timezone": "Asia/Tokyo"}
    assert night_hours({"night": own}, {"oncall": {"quiet_hours": quiet}}) == own
    assert night_hours({"night": None}, {"oncall": {"quiet_hours": quiet}}) == quiet
    assert night_hours({"night": None}, {"oncall": {"quiet_hours": None}}) == {
        "start": "23:00",
        "end": "08:00",
        "timezone": "UTC",
    }
    assert night_hours({}, {})["timezone"] == "UTC"


def test_the_journal_needs_grafana_on_its_host(data: dict[str, Any]) -> None:
    data["hosts"]["edge"]["modules"]["incidents"] = {}
    assert "module-requires" in rules_hit(data)


def test_the_dashboard_is_provisioned_and_the_alerts_link_its_panels(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    tree = render_once(manifest_from(data), modules)
    board = json.loads(tree["central/grafana/dashboards/incidents.json"])
    assert board["uid"] == "ayeops-incidents"
    rules = yaml.safe_load(tree["central/grafana/provisioning/alerting/rules.yaml"])
    group = next(g for g in rules["groups"] if g["name"] == "incidents")
    panels = {rule["uid"]: rule["annotations"]["__panelId__"] for rule in group["rules"]}
    assert panels == {"aye-incidents-failing": "17", "aye-incidents-stale": "18"}


def test_without_the_module_nothing_is_rendered(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    del central(data)["incidents"]
    tree = render_once(manifest_from(data), modules)
    assert "central/grafana/dashboards/incidents.json" not in tree
    assert not any(path.startswith("central/incidents/") for path in tree)


def test_every_series_the_dashboard_and_alerts_read_is_produced_by_the_agent(
    modules: dict[str, Module],
) -> None:
    script = SCRIPT.read_text(encoding="utf-8")
    board = json.loads((MODULE / "dashboards" / "incidents.json").read_text(encoding="utf-8"))
    own = [p for p in board["panels"] if p.get("ayeopsRequires") in (None, ["oncall"])]
    exprs = [t["expr"] for p in own for t in p.get("targets", [])]
    exprs += [alert["expr"] for alert in modules["incidents"].alerts]
    exprs += modules["incidents"].catalog["incidents-agent"]["metrics"]
    names = {name for expr in exprs for name in METRIC.findall(expr)}
    assert names
    assert sorted(name for name in names if f'"{name}' not in script) == []


def test_the_autopilot_panels_need_the_autopilot_and_read_what_it_exports() -> None:
    script = (MODULES_DIR / "autopilot" / "files" / "autopilot.py").read_text(encoding="utf-8")
    board = json.loads((MODULE / "dashboards" / "incidents.json").read_text(encoding="utf-8"))
    borrowed = [p for p in board["panels"] if p.get("ayeopsRequires") == ["autopilot"]]
    assert borrowed
    assert all(
        p.get("ayeopsRequires") in (None, ["autopilot"], ["oncall"]) for p in board["panels"]
    )
    exprs = [t["expr"] for p in borrowed for t in p.get("targets", [])]
    names = {name for expr in exprs for name in METRIC.findall(expr)}
    assert names
    assert all(name.startswith("ayeops_autopilot_") for name in names)
    words = {name.removeprefix("ayeops_autopilot_") for name in names}
    assert sorted(word for word in words if f'"{word}"' not in script) == []


def test_without_the_autopilot_its_panels_are_dropped(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    def titles() -> set[str]:
        tree = render_once(manifest_from(data), modules)
        board = json.loads(tree["central/grafana/dashboards/incidents.json"])
        return {panel["title"] for panel in board["panels"]}

    assert {"Autopilot hypotheses", "Hit rate at rank 1"} <= titles()
    del central(data)["autopilot"]
    assert not {"Autopilot hypotheses", "Hit rate at rank 1"} & titles()


def test_panels_fit_the_grid_and_have_unique_ids() -> None:
    board = json.loads((MODULE / "dashboards" / "incidents.json").read_text(encoding="utf-8"))
    ids = [panel["id"] for panel in board["panels"]]
    assert len(ids) == len(set(ids))
    cells: set[tuple[int, int]] = set()
    for panel in board["panels"]:
        grid = panel["gridPos"]
        assert grid["x"] + grid["w"] <= 24
        for x in range(grid["x"], grid["x"] + grid["w"]):
            for y in range(grid["y"], grid["y"] + grid["h"]):
                assert (x, y) not in cells, panel["title"]
                cells.add((x, y))


def test_the_agent_knows_the_services_and_the_modules_of_each_host_and_the_host_alerts(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    env = agent_env(data, modules)
    hosts = json.loads(env["AYE_INCIDENTS_HOSTS"])
    assert hosts == {"central": ["mcp", "web"], "edge": ["acme-db"]}
    host_modules = json.loads(env["AYE_INCIDENTS_HOST_MODULES"])
    assert {"node", "grafana"} <= set(host_modules["central"])
    assert {"node", "offsite"} <= set(host_modules["edge"])
    assert json.loads(env["AYE_INCIDENTS_HOST_ALERTS"]) == [
        {"title": "Host down", "labels": {}},
        {"title": "Scrape target down", "labels": {"job": "node"}},
    ]


def test_planned_services_are_on_no_host(data: dict[str, Any]) -> None:
    data["services"]["mcp"]["state"] = "planned"
    assert "mcp" not in host_services(manifest_from(data))["central"]


def test_host_alerts_can_be_narrowed_and_an_unknown_one_is_refused(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    central(data)["incidents"] = {"host_alerts": ["host-down"]}
    assert json.loads(agent_env(data, modules)["AYE_INCIDENTS_HOST_ALERTS"]) == [
        {"title": "Host down", "labels": {}}
    ]
    assert rules_hit(data) == set()
    central(data)["incidents"] = {"host_alerts": ["host-vanished"]}
    assert "incidents-host-alert" in rules_hit(data)
    central(data)["incidents"] = {"host_alerts": [{"alert": "host-vanished", "job": "node"}]}
    assert "incidents-host-alert" in rules_hit(data)


def test_a_host_alert_entry_matches_labels_and_needs_at_least_one(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    entry = {"alert": "target-down", "job": "node", "instance": "node-exporter:9100"}
    central(data)["incidents"] = {"host_alerts": [entry]}
    assert rules_hit(data) == set()
    assert json.loads(agent_env(data, modules)["AYE_INCIDENTS_HOST_ALERTS"]) == [
        {"title": "Scrape target down", "labels": {"instance": "node-exporter:9100", "job": "node"}}
    ]
    central(data)["incidents"] = {"host_alerts": [{"alert": "target-down"}]}
    assert "module-settings" in rules_hit(data)
    central(data)["incidents"] = {"host_alerts": [{"alert": "target-down", "job": 1}]}
    assert "module-settings" in rules_hit(data)


def test_the_agent_knows_which_traefik_services_serve_which_service(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    env = agent_env(data, modules)
    assert env["AYE_INCIDENTS_VICTORIAMETRICS"] == "http://victoriametrics:8428"
    routes = json.loads(env["AYE_INCIDENTS_ROUTES"])
    assert routes["grafana"] == [
        {"host": "central", "routes": ["grafana@file"], "scrape_seconds": 30}
    ]
    assert routes["metrics"][0]["routes"] == ["metrics@file"]
    assert routes["auth"][0]["routes"] == ["auth@file"]
    assert "web" not in routes


def test_a_route_to_a_manifest_service_counts_for_that_service(data: dict[str, Any]) -> None:
    data["services"]["grafana"] = {**data["services"]["mcp"], "host": "central"}
    routes = request_routes(manifest_from(data))
    assert routes["grafana"][0]["routes"] == ["grafana@file"]


def test_routes_use_the_scrape_interval_of_their_host(data: dict[str, Any]) -> None:
    data["hosts"]["central"]["modules"]["metrics"]["scrape_interval"] = "1m"
    assert request_routes(manifest_from(data))["portal"][0]["scrape_seconds"] == 60
    assert scrape_seconds("15s") == 15
    assert scrape_seconds(None) == 30


def test_traefik_exports_the_request_counter_the_agent_asks_for(
    modules: dict[str, Module],
) -> None:
    script = SCRIPT.read_text(encoding="utf-8")
    assert "traefik_service_requests_total" in modules["ingress"].catalog["traefik"]["metrics"]
    assert "traefik_service_requests_total{" in script


ACKS_SECRET = "${AYE_ONCALL_ACKS_SECRET:?set AYE_ONCALL_ACKS_SECRET}"


def test_with_oncall_the_agent_asks_the_bridge_with_a_secret_of_its_own(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    services = host_compose(manifest_from(data), "central", modules)["services"]
    env = services["incidents-agent"]["environment"]
    assert env["AYE_INCIDENTS_ONCALL"] == "http://oncall-bridge:8080"
    assert env["AYE_INCIDENTS_ONCALL_SECRET"] == ACKS_SECRET
    assert services["oncall-bridge"]["environment"]["AYE_ONCALL_ACKS_SECRET"] == ACKS_SECRET
    assert "AYE_ONCALL_WEBHOOK_SECRET" not in json.dumps(services["incidents-agent"])
    assert "oncall-bridge" not in services["incidents-agent"]["depends_on"]
    manifest = manifest_from(data)
    for name in ("incidents", "oncall"):
        assert "AYE_ONCALL_ACKS_SECRET" in generated(manifest, modules, "central", name), name
    tree = render_once(manifest, modules)
    assert "AYE_ONCALL_ACKS_SECRET=" in tree["central/.env.example"]


def test_without_oncall_the_agent_never_asks_for_acknowledgements(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    del central(data)["oncall"]
    env = agent_env(data, modules)
    assert not {"AYE_INCIDENTS_ONCALL", "AYE_INCIDENTS_ONCALL_SECRET"} & set(env)
    manifest = manifest_from(data)
    assert "AYE_ONCALL_ACKS_SECRET" not in generated(manifest, modules, "central", "incidents")
    tree = render_once(manifest, modules)
    assert "AYE_ONCALL_ACKS_SECRET" not in tree["central/.env.example"]
    board = json.loads(tree["central/grafana/dashboards/incidents.json"])
    titles = {panel["title"] for panel in board["panels"]}
    assert not {"Time to acknowledge", "Median time to acknowledge"} & titles
    assert "Median time to restore" in titles


def test_without_the_journal_the_bridge_serves_no_acknowledgements(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    del central(data)["incidents"]
    services = host_compose(manifest_from(data), "central", modules)["services"]
    assert "AYE_ONCALL_ACKS_SECRET" not in services["oncall-bridge"]["environment"]
    manifest = manifest_from(data)
    assert "AYE_ONCALL_ACKS_SECRET" not in generated(manifest, modules, "central", "oncall")


def test_time_to_acknowledge_sits_next_to_time_to_restore(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    tree = render_once(manifest_from(data), modules)
    board = json.loads(tree["central/grafana/dashboards/incidents.json"])
    titles = [panel["title"] for panel in board["panels"] if panel["type"] == "row"]
    assert titles.index("Time to acknowledge") == titles.index("Time to restore") + 1
    exprs = " ".join(t["expr"] for p in board["panels"] for t in p.get("targets", []))
    assert (
        "sum by (le, criticality, period) (increase(ayeops_incident_acknowledge_seconds_bucket"
        in exprs
    )
    assert "ayeops_incidents_unacknowledged_total" in exprs
