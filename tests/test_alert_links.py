from __future__ import annotations

import json
import re
from typing import Any

import pytest
import yaml

from ayeops.alert_links import (
    DASHBOARD_UID,
    PANEL_ID,
    Board,
    Link,
    Panel,
    board_from,
    dashboard_problems,
    dashboard_url,
    expr_metrics,
    fixed_labels,
    link_annotations,
    pick_link,
    related_boards,
)
from ayeops.manifest import load_manifest
from ayeops.portal.pages import portal_pages
from ayeops.registry import Module
from ayeops.render import contact_points, telegram_template
from tests.conftest import EXAMPLE_STAMP, ROOT, english, manifest_from, render_once, rules_hit


def board(uid: str, module: str, *panels: tuple[int, set[str]], host: bool = False) -> Board:
    return Board(
        uid,
        uid.title(),
        module,
        tuple(Panel(pid, f"p{pid}", frozenset(metrics)) for pid, metrics in panels),
        frozenset({"host"}) if host else frozenset(),
    )


def rendered(data: dict[str, Any], modules: dict[str, Module]) -> dict[str, str]:
    return render_once(manifest_from(data), modules, EXAMPLE_STAMP)


def rules_of(tree: dict[str, str]) -> dict[str, dict[str, Any]]:
    loaded = yaml.safe_load(tree["central/grafana/provisioning/alerting/rules.yaml"])
    return {rule["uid"]: rule for group in loaded["groups"] for rule in group["rules"]}


def dashboards_of(tree: dict[str, str]) -> dict[str, set[int]]:
    found = {}
    for path, text in tree.items():
        if re.match(r"central/grafana/(project-)?dashboards/.+\.json$", path):
            data = json.loads(text)
            found[str(data["uid"])] = {panel.id for panel in board_from("", data).panels}
    return found


@pytest.mark.parametrize(
    ("expr", "expected"),
    [
        ("up == 0", {"up"}),
        ('rate(http_errors_total{code=~"5.."}[5m]) > 0', {"http_errors_total"}),
        (
            "sum by (zone) (a_total) / on(zone) group_left b_info > bool 0.02",
            {"a_total", "b_info"},
        ),
        ('node_load1{host="$host"} offset 15s > 1e3', {"node_load1"}),
        ('label_replace(x_seconds, "dst", "$1", "src", "(.*)") and y', {"x_seconds", "y"}),
        ('aye:delivery_overdue{step="deploy"} == 1', {"aye:delivery_overdue"}),
        ("max without (instance) (z_bytes) unless ignoring (job) w_bytes", {"z_bytes", "w_bytes"}),
        ("time() - $__interval_ms", set()),
    ],
)
def test_expr_metrics_reads_metric_names_and_skips_functions_labels_and_keywords(
    expr: str, expected: set[str]
) -> None:
    assert expr_metrics(expr) == expected


def test_the_panel_sharing_most_metrics_wins_within_the_alert_s_own_module() -> None:
    boards = [
        board("b-other", "other", (1, {"a", "b", "c"})),
        board("b-own", "own", (5, {"a"}), (6, {"a", "b"})),
    ]
    link = pick_link({"expr": "a + b + c"}, "own", boards)
    assert link == Link("b-own", "B-Own", 6)


def test_another_module_s_board_is_used_when_the_own_module_has_none() -> None:
    boards = [board("b-other", "other", (3, {"a"}))]
    assert pick_link({"expr": "a > 1"}, "own", boards) == Link("b-other", "B-Other", 3)


def test_ties_go_to_the_first_board_by_uid_and_the_first_panel() -> None:
    boards = [board("zz", "m", (1, {"a"})), board("aa", "m", (9, {"a"}), (2, {"a"}))]
    assert pick_link({"expr": "a"}, "m", boards) == Link("aa", "Aa", 9)


def test_an_alert_without_a_shared_metric_gets_no_link() -> None:
    assert pick_link({"expr": "nothing_here == 0"}, "m", [board("x", "m", (1, {"a"}))]) is None


def test_an_explicit_dashboard_overrides_the_guess_and_false_switches_it_off() -> None:
    boards = [board("x", "m", (1, {"a"})), board("y", "m", (4, {"q"}), (7, {"r"}))]
    assert pick_link({"expr": "a", "dashboard": {"uid": "y", "panel": 7}}, "m", boards) == Link(
        "y", "Y", 7
    )
    assert pick_link({"expr": "a", "dashboard": "y"}, "m", boards) == Link("y", "Y", 4)
    assert pick_link({"expr": "r", "dashboard": "y"}, "m", boards) == Link("y", "Y", 7)
    assert pick_link({"expr": "a", "dashboard": False}, "m", boards) is None


def test_an_explicit_panel_that_was_not_rendered_falls_back_to_a_panel_of_the_dashboard() -> None:
    boards = [board("y", "m", (4, {"q"}), (5, {"a"}))]
    assert pick_link({"expr": "a", "dashboard": {"uid": "y", "panel": 7}}, "m", boards) == Link(
        "y", "Y", 5
    )
    assert pick_link({"expr": "z", "dashboard": {"uid": "y", "panel": 7}}, "m", boards) == Link(
        "y", "Y", 4
    )
    assert pick_link({"expr": "z", "dashboard": "empty"}, "m", [board("empty", "m")]) is None
    assert pick_link({"expr": "q", "dashboard": "gone"}, "m", boards) is None


def test_annotations_carry_the_dashboard_and_the_panel_as_text() -> None:
    assert link_annotations(None) == {}
    assert link_annotations(Link("u", "U")) == {}
    assert link_annotations(Link("u", "U", 12)) == {DASHBOARD_UID: "u", PANEL_ID: "12"}


def test_dashboard_url_selects_the_host_only_where_the_board_has_that_variable() -> None:
    with_host = board("h", "m", (1, {"a"}), host=True)
    plain = board("p", "m", (1, {"a"}))
    assert (
        dashboard_url("https://g.example/", with_host, "edge")
        == "https://g.example/d/h?var-host=edge"
    )
    assert dashboard_url("https://g.example", plain, "edge") == "https://g.example/d/p"
    assert dashboard_url("https://g.example", with_host) == "https://g.example/d/h"


def test_related_boards_rank_own_module_then_overlap_and_stop_at_the_limit() -> None:
    boards = [
        board("a", "x", (1, {"m1", "m2"})),
        board("b", "own", (1, {"m1"})),
        board("c", "x", (1, {"m1"})),
        board("d", "x", (1, {"m9"})),
    ]
    assert [b.uid for b in related_boards(["m1", "m2"], "own", boards)] == ["b", "a", "c"]
    assert [b.uid for b in related_boards(["m1", "m2"], "own", boards, limit=1)] == ["b"]


def test_board_from_reads_nested_panels_skips_rows_and_keeps_the_variables() -> None:
    data = {
        "uid": "u",
        "title": "T",
        "templating": {"list": [{"name": "host"}, {"name": ""}]},
        "panels": [
            {"id": 1, "type": "row", "panels": [{"id": 2, "targets": [{"expr": "a"}]}]},
            {"id": 3, "targets": [{"expr": "b"}, {"expr": "c"}]},
            {"type": "text"},
        ],
    }
    parsed = board_from("m", data)
    assert [(p.id, p.metrics) for p in parsed.panels] == [
        (2, frozenset({"a"})),
        (3, frozenset({"b", "c"})),
    ]
    assert parsed.variables == {"host"}


def test_dashboard_problems_name_unknown_dashboards_and_panels_only() -> None:
    known = {"x": frozenset({1, 2})}
    alerts: list[dict[str, Any]] = [
        {"dashboard": "x"},
        {"dashboard": {"uid": "x", "panel": 2}},
        {"dashboard": False},
        {},
        {"dashboard": "nope"},
        {"dashboard": {"uid": "x", "panel": 9}},
    ]
    entries = [(f"a.{index}", alert) for index, alert in enumerate(alerts)]
    assert dashboard_problems(entries, known) == [
        ("a.4.dashboard", "alert-dashboard", "no dashboard with uid 'nope'"),
        ("a.5.dashboard", "alert-dashboard", "dashboard 'x' has no panel with id 9"),
    ]


def test_every_explicit_dashboard_in_the_catalog_points_at_a_real_panel(
    modules: dict[str, Module],
) -> None:
    known = {}
    for module in modules.values():
        for path in module.dashboards:
            parsed = board_from(module.name, json.loads(path.read_text(encoding="utf-8")))
            known[parsed.uid] = frozenset(panel.id for panel in parsed.panels)
    explicit = [
        alert for module in modules.values() for alert in module.alerts if alert.get("dashboard")
    ]
    assert len(explicit) >= 10
    for module in modules.values():
        entries = [(f"{module.name}.{index}", alert) for index, alert in enumerate(module.alerts)]
        assert dashboard_problems(entries, known) == []


def test_rendered_rules_link_only_to_dashboards_and_panels_that_were_rendered(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    tree = rendered(data, modules)
    rules = rules_of(tree)
    boards = dashboards_of(tree)
    linked = [rule for rule in rules.values() if DASHBOARD_UID in rule["annotations"]]
    assert len(linked) >= 40
    for rule in linked:
        uid = rule["annotations"][DASHBOARD_UID]
        assert uid in boards, rule["uid"]
        panel = rule["annotations"].get(PANEL_ID)
        assert panel is not None and int(panel) in boards[uid], rule["uid"]


def test_explicit_module_mappings_reach_the_rendered_rules(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    rules = rules_of(rendered(data, modules))
    edge = rules["aye-cloudflare-edge-5xx"]["annotations"]
    assert (edge[DASHBOARD_UID], edge[PANEL_ID]) == ("ayeops-cloudflare", "4")
    probe = rules["aye-probe-failing"]["annotations"]
    assert (probe[DASHBOARD_UID], probe[PANEL_ID]) == ("ayeops-probes", "1")


def test_a_service_alert_links_to_a_dashboard_its_metric_is_drawn_on(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    web = rules_of(rendered(data, modules))["aye-web-down"]["annotations"]
    assert web[DASHBOARD_UID] in {"ayeops-reliability", "ayeops-probes", "ayeops-internet"}


def test_a_service_alert_can_point_at_a_project_dashboard_or_opt_out(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    data["services"]["web"]["alerts"][0]["dashboard"] = {"uid": "acme-orders", "panel": 1}
    web = rules_of(rendered(data, modules))["aye-web-down"]["annotations"]
    assert (web[DASHBOARD_UID], web[PANEL_ID]) == ("acme-orders", "1")
    data["services"]["web"]["alerts"][0]["dashboard"] = False
    web = rules_of(rendered(data, modules))["aye-web-down"]["annotations"]
    assert DASHBOARD_UID not in web and PANEL_ID not in web


@pytest.mark.parametrize(
    "value", ["nope", {"uid": "acme-orders", "panel": 99}, {"uid": "ayeops-host", "panel": 999}]
)
def test_aye_check_refuses_a_dashboard_or_panel_that_does_not_exist(
    data: dict[str, Any], value: Any
) -> None:
    data["services"]["web"]["alerts"][0]["dashboard"] = value
    assert rules_hit(data) == {"alert-dashboard"}


@pytest.mark.parametrize("value", [True, "", {"panel": 1}, {"uid": "x", "panel": 0}, 3])
def test_the_schema_refuses_a_malformed_dashboard_value(data: dict[str, Any], value: Any) -> None:
    data["services"]["web"]["alerts"][0]["dashboard"] = value
    assert rules_hit(data) == {"schema"}


def test_the_example_with_valid_explicit_dashboards_passes_the_check(data: dict[str, Any]) -> None:
    data["services"]["web"]["alerts"][0]["dashboard"] = {"uid": "acme-orders", "panel": 1}
    assert rules_hit(data) == set()


def test_telegram_message_links_the_panel_else_the_dashboard_in_the_grafana_language() -> None:
    uk = telegram_template("uk")
    assert (
        '{{ if .PanelURL }} · <a href="{{ .PanelURL }}">дашборд</a>'
        '{{ else if .DashboardURL }} · <a href="{{ .DashboardURL }}">дашборд</a>{{ end }}'
    ) in uk
    assert ">dashboard</a>" in telegram_template("en")
    assert ">dashboard</a>" in telegram_template("pl")
    opened = sum(uk.count("{{ " + word + " ") for word in ("if", "with", "range", "define"))
    assert opened == uk.count("{{ end }}")


def test_the_contact_point_template_follows_the_grafana_locale(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    data["hosts"]["central"]["modules"]["grafana"]["locale"] = "uk"
    points = yaml.safe_load(
        rendered(data, modules)["central/grafana/provisioning/alerting/contact-points.yaml"]
    )
    assert ">дашборд</a>" in points["templates"][0]["template"]
    english_points = contact_points(manifest_from(data))
    assert english_points is not None
    assert english_points["templates"][0]["template"] == telegram_template("en")


def test_portal_cards_and_alerts_link_their_dashboards_on_the_public_grafana(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    html = english(portal_pages(manifest_from(data), modules, EXAMPLE_STAMP)["services.html"])
    card = re.search(r'<article class="svc[^"]*" id="svc-central-web".*?</article>', html)
    assert card
    body = card.group(0)
    assert "dashboards</span>" in body
    assert re.search(r'class="chip" href="https://grafana\.ops\.acme\.example/d/[a-z-]+', body)
    assert re.search(r'href="https://grafana\.ops\.acme\.example/d/[^"]+"[^>]*>dashboard ↗', body)
    node = re.search(r'<article class="svc[^"]*" id="svc-edge-node-exporter".*?</article>', html)
    assert node and "/d/ayeops-host?var-host=edge" in node.group(0)


def test_without_a_public_grafana_the_portal_shows_no_dashboard_links(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    data["hosts"]["central"]["modules"]["grafana"].pop("root_url")
    data["hosts"]["central"]["modules"].pop("auth", None)
    html = english(portal_pages(manifest_from(data), modules, EXAMPLE_STAMP)["services.html"])
    assert "dashboards</span>" not in html
    assert "dashboard ↗" not in html


def test_the_manifest_root_url_wins_and_the_grafana_route_is_the_fallback(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    data["hosts"]["central"]["modules"]["grafana"]["root_url"] = "https://dash.acme.example"
    html = portal_pages(manifest_from(data), modules, EXAMPLE_STAMP)["services.html"]
    assert "https://dash.acme.example/d/" in html
    assert "https://grafana.ops.acme.example/d/" not in html
    data["hosts"]["central"]["modules"]["grafana"].pop("root_url")
    html = portal_pages(manifest_from(data), modules, EXAMPLE_STAMP)["services.html"]
    assert "https://grafana.ops.acme.example/d/" in html


def labelled(uid: str, pid: int, metric: str, **labels: str) -> Board:
    fixed = frozenset((metric, key, value) for key, value in labels.items())
    return Board(uid, uid, "m", (Panel(pid, f"p{pid}", frozenset({metric}), fixed),))


def test_fixed_labels_keep_equality_matchers_only_and_skip_variables() -> None:
    expr = 'min(p{job="icmp",layer!="x",host="$host",code=~"5.."}) + up{job = "a"} + q'
    assert fixed_labels(expr) == {("p", "job", "icmp"), ("up", "job", "a")}


def test_a_panel_pinning_another_value_of_the_alert_s_label_is_not_linked() -> None:
    boards = [
        labelled("a-gateway", 1, "probe_success", layer="gateway"),
        labelled("b-internet", 2, "probe_success", layer="internet"),
    ]
    alert = {"expr": 'probe_success{layer="internet"} == bool 0'}
    assert pick_link(alert, "m", boards) == Link("b-internet", "b-internet", 2)


def test_among_compatible_panels_the_one_pinning_fewer_other_labels_wins() -> None:
    boards = [
        labelled("a-narrow", 1, "probe_success", job="icmp", layer="gateway"),
        labelled("b-wide", 2, "probe_success", job="http"),
    ]
    alert = {"expr": 'probe_success{instance="https://x.example"} == bool 0'}
    assert pick_link(alert, "m", boards) == Link("b-wide", "b-wide", 2)
    pinned = {"expr": 'probe_success{instance="https://x.example",job="icmp"} == bool 0'}
    assert pick_link(pinned, "m", boards) == Link("a-narrow", "a-narrow", 1)


def test_board_from_keeps_the_fixed_labels_of_every_target() -> None:
    data = {
        "uid": "u",
        "panels": [
            {"id": 1, "targets": [{"expr": 'a{x="1"}'}, {"expr": 'a{x="2"}'}]},
        ],
    }
    assert board_from("m", data).panels[0].labels == {("a", "x", "1"), ("a", "x", "2")}


def test_a_check_alert_can_name_its_dashboard_and_aye_check_validates_it(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    data["hosts"]["central"]["modules"]["checks"] = {}
    data["services"]["web"]["checks"] = {
        "homepage": {
            "type": "http",
            "url": "https://acme.example/health",
            "alert": {
                "for": "3m",
                "severity": "critical",
                "runbook": "web-down",
                "dashboard": {"uid": "ayeops-probes", "panel": 2},
            },
        }
    }
    assert rules_hit(data) == set()
    rule = next(
        rule for rule in rules_of(rendered(data, modules)).values() if "homepage" in rule["uid"]
    )
    assert (rule["annotations"][DASHBOARD_UID], rule["annotations"][PANEL_ID]) == (
        "ayeops-probes",
        "2",
    )
    data["services"]["web"]["checks"]["homepage"]["alert"]["dashboard"] = "nope"
    assert rules_hit(data) == {"alert-dashboard"}


def test_a_label_the_alert_pins_itself_does_not_count_against_the_panel() -> None:
    boards = [labelled("a-pinned", 1, "up", job="icmp"), labelled("b-plain", 2, "up")]
    assert pick_link({"expr": 'up{job="icmp"} == bool 0'}, "m", boards) == Link(
        "a-pinned", "a-pinned", 1
    )


def test_a_sql_check_alert_can_link_a_project_dashboard(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    data["hosts"]["central"]["modules"]["checks"] = {}
    data["services"]["web"]["checks"] = {
        "stuck": {
            "type": "sql",
            "datasource": "acme-orders",
            "query": "select count(*) as value from orders where stuck",
            "alert": {
                "op": ">",
                "threshold": 0,
                "for": "5m",
                "severity": "warning",
                "runbook": "web-down",
                "dashboard": "acme-orders",
            },
        }
    }
    assert rules_hit(data) == set()
    rule = next(r for r in rules_of(rendered(data, modules)).values() if "stuck" in r["uid"])
    assert rule["annotations"][DASHBOARD_UID] == "acme-orders"
    assert rule["annotations"][PANEL_ID].isdigit()


@pytest.mark.parametrize("manifest", ["examples/acme.yaml", "tests/e2e/e2e.yaml"])
def test_no_rule_names_a_dashboard_without_a_panel_which_grafana_refuses_to_provision(
    manifest: str, modules: dict[str, Module]
) -> None:
    tree = render_once(load_manifest(ROOT / manifest), modules, EXAMPLE_STAMP)
    rules = rules_of(tree)
    assert rules
    for rule in rules.values():
        keys = {DASHBOARD_UID, PANEL_ID} & set(rule["annotations"])
        assert keys in (set(), {DASHBOARD_UID, PANEL_ID}), rule["uid"]
