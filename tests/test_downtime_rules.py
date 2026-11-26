from __future__ import annotations

from typing import Any

import pytest
import yaml

from ayeops.autopilot import unheld
from ayeops.downtime import HOSTS_DOWN
from ayeops.registry import Module
from tests.conftest import EXAMPLE_STAMP, manifest_from, render_once, rules_hit

RULES = "central/grafana/provisioning/alerting/rules.yaml"
SUNDAY_NIGHT = {"days": ["sun"], "start": "23:00", "duration": "3h"}
EDGE_WINDOW = 'label_replace((vector(time() - 342000) % 604800 < 10800), "host", "edge", "", "")'
VOLATILE_EDGE = 'label_replace(vector(1), "host", "edge", "", "")'


def edge(data: dict[str, Any]) -> dict[str, Any]:
    host: dict[str, Any] = data["hosts"]["edge"]
    return host


def rendered_rules(data: dict[str, Any], modules: dict[str, Module]) -> dict[str, dict[str, Any]]:
    tree = render_once(manifest_from(data), modules, EXAMPLE_STAMP)
    groups = yaml.safe_load(tree[RULES])["groups"]
    return {rule["uid"]: rule for group in groups for rule in group["rules"]}


def expr(rule: dict[str, Any]) -> str:
    return str(rule["data"][0]["model"]["expr"]).replace("$$", "$")


def test_a_fleet_without_downtime_renders_no_host_gone_and_the_plain_host_down(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    rules = rendered_rules(data, modules)
    assert "aye-host-gone" not in rules
    plain = str(modules["node"].alerts[0]["expr"]).strip()
    assert expr(rules["aye-host-down"]) == plain
    assert rules["aye-host-down"]["labels"]["severity"] == "critical"


def test_a_volatile_host_leaves_host_down_and_gets_a_late_warning(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    edge(data)["volatile"] = "3d"
    rules = rendered_rules(data, modules)
    plain = str(modules["node"].alerts[0]["expr"]).strip()
    assert expr(rules["aye-host-down"]) == (
        f"({plain}) unless on (host) ({VOLATILE_EDGE}) or 0 * ({plain})"
    )
    assert rules["aye-host-down"]["labels"]["severity"] == "critical"
    gone = rules["aye-host-gone"]
    up = 'up{job="node", host=~"edge"}'
    assert expr(gone) == (
        f"1 - (((max by (host) (max_over_time({up}[3d]))"
        f" + on (host) group_right () (0 * last_over_time({up}[10d])))"
        f" or 0 * last_over_time({up}[10d])))"
    )
    assert gone["labels"]["severity"] == "warning"
    assert gone["labels"]["module"] == "node"
    assert gone["for"] == "5m"
    assert gone["noDataState"] == "OK"
    assert gone["annotations"]["runbook_url"].endswith("/docs/runbooks/host-gone.md")


def test_the_other_alerts_of_a_volatile_host_stay_held_while_it_is_down(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    edge(data)["volatile"] = True
    rules = rendered_rules(data, modules)
    assert f"unless on (host) ({HOSTS_DOWN})" in expr(rules["aye-disk-full"])
    assert f"unless on (host) ({HOSTS_DOWN})" not in expr(rules["aye-host-gone"])
    dump = expr(rules["aye-acme-db-dump-small"])
    edge_down = (
        'max by (host) (last_over_time(up{job="node", host="edge"}[8d])'
        ' * (lag(up{job="node", host="edge"}[8d]) < bool 180)) == 0'
    )
    assert f"unless on () ({edge_down}) or 0 * (" in dump
    assert "unless on () (" not in expr(rules["aye-web-down"])


def test_a_maintenance_window_holds_every_rule_of_the_fleet_on_that_host(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    edge(data)["maintenance"] = [SUNDAY_NIGHT]
    rules = rendered_rules(data, modules)
    duty = rules.pop("aye-duty-overdue")
    assert unheld(expr(duty)) == expr(duty)
    assert "unless" not in expr(duty)
    assert len(rules) > 40
    for uid, rule in rules.items():
        assert f" unless on (host) ({EDGE_WINDOW}) " in expr(rule), uid
        assert unheld(expr(rule)) != expr(rule), uid
    assert "unless on () ((vector(time() - 342000)" in expr(rules["aye-acme-db-dump-small"])
    assert "unless on () (" not in expr(rules["aye-web-down"])
    assert rules["aye-host-down"]["for"] == "2m"


def test_the_catalog_of_the_autopilot_reads_the_rule_inside_every_guard(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    edge(data)["volatile"] = True
    edge(data)["maintenance"] = [SUNDAY_NIGHT]
    rules = rendered_rules(data, modules)
    plain = str(modules["node"].alerts[0]["expr"]).strip()
    assert unheld(expr(rules["aye-host-down"])) == plain
    dump = expr(rules["aye-acme-db-dump-small"])
    assert unheld(dump) == (
        'max by (host) (ayeops_backup_last_size_bytes{backup="acme-db"}) < bool 1048576'
    )


def test_a_profile_gives_its_hosts_their_windows(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    data["profiles"] = {"office": {"maintenance": [SUNDAY_NIGHT], "volatile": True}}
    edge(data)["profile"] = "office"
    assert rules_hit(data) == set()
    rules = rendered_rules(data, modules)
    assert EDGE_WINDOW in expr(rules["aye-disk-full"])
    assert "aye-host-gone" in rules


@pytest.mark.parametrize(
    "fields",
    [
        {"volatile": True},
        {"volatile": False},
        {"volatile": "1h"},
        {"volatile": "7d"},
        {"maintenance": [SUNDAY_NIGHT, {"start": "02:00", "duration": "90m"}]},
        {"maintenance": [{"days": ["mon", "sun"], "start": "00:00", "duration": "24h"}]},
        {"maintenance": []},
    ],
)
def test_declared_downtime_passes_the_rules(data: dict[str, Any], fields: dict[str, Any]) -> None:
    edge(data).update(fields)
    assert rules_hit(data) == set()


@pytest.mark.parametrize(
    ("fields", "rule"),
    [
        ({"volatile": "30m"}, "host-volatile"),
        ({"volatile": "8d"}, "host-volatile"),
        ({"volatile": "0h"}, "schema"),
        ({"volatile": "yes"}, "schema"),
        ({"maintenance": [{"start": "02:00", "duration": "0m"}]}, "maintenance-window"),
        ({"maintenance": [{"start": "02:00", "duration": "25h"}]}, "maintenance-window"),
        ({"maintenance": [{"start": "02:00", "duration": "24h"}]}, "maintenance-window"),
        ({"maintenance": [{"start": "24:00", "duration": "1h"}]}, "schema"),
        ({"maintenance": [{"start": "2:00", "duration": "1h"}]}, "schema"),
        (
            {"maintenance": [{"days": ["mon", "funday"], "start": "02:00", "duration": "1h"}]},
            "schema",
        ),
        ({"maintenance": [{"days": [], "start": "02:00", "duration": "1h"}]}, "schema"),
        ({"maintenance": [{"days": ["mon", "mon"], "start": "02:00", "duration": "1h"}]}, "schema"),
        ({"maintenance": [{"start": "02:00", "duration": "1w"}]}, "schema"),
        ({"maintenance": [{"start": "02:00"}]}, "schema"),
        ({"maintenance": [{"start": "02:00", "duration": "1h", "end": "03:00"}]}, "schema"),
    ],
)
def test_malformed_downtime_fails_its_rule(
    data: dict[str, Any], fields: dict[str, Any], rule: str
) -> None:
    edge(data).update(fields)
    assert rules_hit(data) == {rule}


def test_a_volatile_host_without_node_fails(data: dict[str, Any]) -> None:
    data["hosts"]["lab"] = {"arch": "amd64", "volatile": True, "modules": {}}
    assert rules_hit(data) == {"host-volatile"}
