from __future__ import annotations

import json
import re
from collections.abc import Mapping
from functools import cache
from pathlib import Path
from typing import Any

import pytest
import yaml

from ayeops.dashboards import (
    REQUIRES,
    build,
    is_text,
    localize,
    prune_panels,
    requirement_met,
    wanted,
)
from ayeops.registry import MODULES_DIR, Module
from ayeops.render import blackbox_wanted, grafana_locale
from tests.conftest import ROOT, manifest_from, render_once, rules_hit

EXPORTERS = ROOT / "tests" / "fixtures" / "exporters"
CHECKED = {
    "node": ["host.json"],
    "probes": ["probes.json", "internet.json"],
    "grafana": ["health.json", "services.json", "reliability.json"],
}
ALWAYS = {"metrics", "grafana"}
KEYWORDS = {
    "and",
    "or",
    "unless",
    "bool",
    "offset",
    "by",
    "without",
    "on",
    "ignoring",
    "group_left",
    "group_right",
}
NAME = re.compile(r"(?<![0-9.$])\b[a-zA-Z_:][a-zA-Z0-9_:]*")
GROUPING = re.compile(r"\b(?:by|without|on|ignoring|group_left|group_right)\s*\([^)]*\)")
JOB = re.compile(r'\bjob="([^"$]+)"')
LAYER = re.compile(r'\blayer="([^"$]+)"')
LABEL_VALUES = re.compile(r"^label_values\((.*),\s*[a-z_]+\)$")
PREFIX = re.compile(r'^PREFIX = "(ayeops_[a-z_]+)"$', re.M)
QUOTED = re.compile(r'"([a-z][a-z0-9_]*)"')


def metric_names(expr: str) -> set[str]:
    text = re.sub(r'"[^"]*"', " ", expr)
    text = re.sub(r"\{[^}]*\}", " ", text)
    text = re.sub(r"\[[^\]]*\]", " ", text)
    text = GROUPING.sub(" ", text)
    names = set()
    for match in NAME.finditer(text):
        if text[match.end() :].lstrip().startswith("("):
            continue
        if match.group() not in KEYWORDS:
            names.add(match.group())
    return names


def board(module: str, name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(
        (MODULES_DIR / module / "dashboards" / name).read_text("utf-8")
    )
    return data


def boards() -> list[tuple[str, str, dict[str, Any]]]:
    return [
        (module, name, board(module, name)) for module, names in CHECKED.items() for name in names
    ]


def exprs(data: dict[str, Any]) -> list[tuple[dict[str, Any], str]]:
    found = [
        (panel, target["expr"]) for panel in data["panels"] for target in panel.get("targets", [])
    ]
    for variable in data["templating"]["list"]:
        match = LABEL_VALUES.match(variable["definition"])
        assert match, variable["definition"]
        found.append(({}, match.group(1)))
    return found


@cache
def scrape_jobs() -> Mapping[str, str]:
    jobs = {}
    for template in MODULES_DIR.glob("*/scrape.yaml.j2"):
        for job in re.findall(r"job_name: ([a-z0-9-]+)", template.read_text(encoding="utf-8")):
            jobs[job] = template.parent.name
    return jobs


@cache
def exporter_metrics() -> Mapping[str, frozenset[str]]:
    return {
        path.stem: frozenset(path.read_text(encoding="utf-8").split())
        for path in EXPORTERS.glob("*.txt")
    }


def file_gate(module: str, source: str) -> str:
    spec = yaml.safe_load((MODULES_DIR / module / "module.yaml").read_text(encoding="utf-8"))
    for entry in spec.get("files") or []:
        when = entry.get("when")
        if entry["src"] == source and when in (spec.get("defaults") or {}):
            return f"{module}.{when}"
    return module


@cache
def textfile_metrics() -> Mapping[str, frozenset[str]]:
    produced: dict[str, set[str]] = {}
    for path in MODULES_DIR.glob("*/files/*"):
        if path.suffix not in (".sh", ".py"):
            continue
        module = path.parent.parent.name
        gate = file_gate(module, f"files/{path.name}")
        text = path.read_text(encoding="utf-8")
        names = set(re.findall(r"\b(?:ayeops|node_wifi)_[a-z0-9_]+[a-z0-9]", text))
        for prefix in PREFIX.findall(text):
            names |= {prefix + word for word in QUOTED.findall(text)}
        for name in names:
            produced.setdefault(name, set()).add(gate)
    return {name: frozenset(gates) for name, gates in produced.items()}


def producers(expr: str) -> list[set[str]]:
    jobs = scrape_jobs()
    named = set(JOB.findall(expr))
    if named:
        return [{jobs[job] for job in named}]
    exported = exporter_metrics()
    textfile = textfile_metrics()
    result = []
    for name in sorted(metric_names(expr)):
        found = {jobs[job] for job, metrics in exported.items() if name in metrics}
        result.append(found | textfile.get(name, set()))
    return result


def covered(needed: str, available: set[str]) -> bool:
    return any(have == needed or have.startswith(needed + ".") for have in available)


def test_every_dashboard_with_a_host_picker_first_narrows_it_by_group_from_all() -> None:
    found = []
    for path in sorted(MODULES_DIR.glob("*/dashboards/*.json")):
        data = json.loads(path.read_text("utf-8"))
        variables = {v["name"]: v for v in data.get("templating", {}).get("list", [])}
        if "host" not in variables:
            continue
        found.append(path.name)
        names = [v["name"] for v in data["templating"]["list"]]
        assert names[:2] == ["group", "host"], path.name
        group = variables["group"]
        assert group["includeAll"] and group["allValue"] == ".*", path.name
        assert group["current"] == {"text": "All", "value": "$__all"}, path.name
        assert group["definition"].endswith(", group)"), path.name
        assert 'group=~"$group"' in variables["host"]["definition"], path.name
        assert "allValue" not in variables["host"], path.name
    assert found == ["services.json", "host.json", "internet.json"]


def test_metric_names_skip_functions_labels_ranges_groupings_and_numbers() -> None:
    expr = (
        'sum by (host, job) (rate(node_cpu_seconds_total{mode="idle"}[5m])) / '
        "count(up offset 15s) * 1e-9 > bool on(host) group_left node_load1"
    )
    assert metric_names(expr) == {"node_cpu_seconds_total", "up", "node_load1"}


def test_every_series_a_system_dashboard_reads_is_produced_by_an_exporter_or_a_collector() -> None:
    known = set().union(*exporter_metrics().values()) | set(textfile_metrics())
    jobs = set(scrape_jobs())
    layers = set(
        re.findall(
            r"layer: ([a-z]+)", (MODULES_DIR / "probes" / "scrape.yaml.j2").read_text("utf-8")
        )
    )
    missing = []
    for _, name, data in boards():
        for _, expr in exprs(data):
            missing += [f"{name}: {metric}" for metric in metric_names(expr) - known]
            missing += [f"{name}: job {job}" for job in set(JOB.findall(expr)) - jobs]
            missing += [f"{name}: layer {layer}" for layer in set(LAYER.findall(expr)) - layers]
    assert missing == []
    assert {"gateway", "internet", "dns"} <= layers


def test_every_panel_requires_the_module_that_produces_its_series() -> None:
    gaps = []
    for module, name, data in boards():
        base = {module, *ALWAYS, *data.get(REQUIRES, [])}
        for panel, expr in exprs(data):
            available = base | set(panel.get(REQUIRES, []))
            for found in producers(expr):
                assert found, f"{name}: {expr}"
                if not any(covered(need, available) for need in found):
                    gaps.append(f"{name} / {panel.get('title')}: {sorted(found)}")
    assert gaps == []


def test_panels_fit_the_grid_without_overlapping_and_have_unique_ids() -> None:
    uids = []
    for _, name, data in boards():
        uids.append(data["uid"])
        cells: set[tuple[int, int]] = set()
        ids = [panel["id"] for panel in data["panels"]]
        assert len(ids) == len(set(ids)), name
        for panel in data["panels"]:
            grid = panel["gridPos"]
            assert grid["x"] + grid["w"] <= 24, name
            for x in range(grid["x"], grid["x"] + grid["w"]):
                for y in range(grid["y"], grid["y"] + grid["h"]):
                    assert (x, y) not in cells, (name, panel["title"])
                    cells.add((x, y))
    assert len(uids) == len(set(uids))


def text_maps(node: Any) -> list[dict[str, Any]]:
    if isinstance(node, dict):
        if set(node) & {"en", "uk"}:
            return [node]
        return [found for value in node.values() for found in text_maps(value)]
    if isinstance(node, list):
        return [found for value in node for found in text_maps(value)]
    return []


def test_every_text_of_the_system_dashboards_is_in_english_and_ukrainian() -> None:
    for _, name, data in boards():
        maps = text_maps(data)
        assert maps, name
        for text in maps:
            assert set(text) == {"en", "uk"}, (name, text)
            assert all(isinstance(value, str) and value.strip() for value in text.values())
        assert is_text(data["title"]), name
        titles = [panel["title"] for panel in data["panels"]]
        assert all(is_text(title) or title in {"DNS", "CPU"} for title in titles), name


def test_a_text_map_needs_only_language_keys_with_strings() -> None:
    assert is_text({"en": "Up"})
    assert is_text({"en": "Up", "uk": "Працює"})
    assert not is_text({})
    assert not is_text({"en": "Up", "de": "Oben"})
    assert not is_text({"en": 1})
    assert not is_text({"en": "Up", "uk": None})
    assert not is_text("Up")
    assert not is_text(["en"])


def test_localize_picks_the_language_everywhere_and_keeps_the_rest() -> None:
    node = {
        "title": {"en": "Hosts", "uk": "Хости"},
        "panels": [{"title": {"en": "Up", "uk": "Стан"}, "id": 1, "gridPos": {"h": 4}}],
        "tags": ["ayeops", {"en": "tag", "uk": "мітка"}],
        "mappings": {"0": {"text": {"en": "Down", "uk": "Лежить"}, "color": "red"}},
        "empty": {},
    }
    assert localize(node, "uk") == {
        "title": "Хости",
        "panels": [{"title": "Стан", "id": 1, "gridPos": {"h": 4}}],
        "tags": ["ayeops", "мітка"],
        "mappings": {"0": {"text": "Лежить", "color": "red"}},
        "empty": {},
    }
    assert localize(node, "en")["panels"][0]["title"] == "Up"
    assert localize({"en": "only English"}, "uk") == "only English"
    assert localize(7, "uk") == 7


def hosts(*settings: dict[str, Any]) -> list[dict[str, Any]]:
    return list(settings)


@pytest.mark.parametrize(
    ("requirement", "expected"),
    [
        ("network", True),
        ("network.wifi", False),
        ("node.battery", True),
        ("node.reboot", False),
        ("probes.internet", True),
        ("probes.gateway", False),
        ("smart", False),
        ("node.missing.deeper", False),
        ("probes.http.0", False),
    ],
)
def test_a_requirement_holds_when_some_host_runs_the_module_with_the_setting(
    requirement: str, expected: bool
) -> None:
    fleet = hosts(
        {"node": {"battery": False, "reboot": False}, "metrics": {}},
        {"node": {"battery": True}, "network": {}, "probes": {"internet": True, "gateway": None}},
        {"node": {"battery": "yes"}, "probes": {"http": ["https://x"]}},
    )
    assert requirement_met(requirement, fleet) is expected


def test_a_module_with_empty_settings_still_counts_and_a_missing_one_never_does() -> None:
    assert requirement_met("containers", [{"containers": {}}])
    assert not requirement_met("containers", [{"node": {}}])
    assert not requirement_met("containers", [])
    assert requirement_met("probes.limits.http", [{"probes": {"limits": {"http": 1}}}])
    assert not requirement_met("probes.limits.http", [{"probes": {"limits": 1}}])


def panel(title: str, *requires: str, kind: str = "timeseries") -> dict[str, Any]:
    item: dict[str, Any] = {"title": title, "type": kind}
    if requires:
        item[REQUIRES] = list(requires)
    return item


def titles(panels: list[dict[str, Any]]) -> list[str]:
    return [p["title"] for p in panels]


def test_panels_and_rows_whose_requirement_fails_are_dropped_with_the_marker() -> None:
    panels = [
        panel("Overview", kind="row"),
        panel("cpu"),
        panel("Network", "network", kind="row"),
        panel("traffic", "network"),
        panel("Disks", kind="row"),
        panel("smart", "smart"),
        panel("Battery", kind="row"),
        panel("charge", "node.battery"),
        panel("Last", kind="row"),
    ]
    kept = prune_panels(panels, lambda requirement: requirement == "node.battery")
    assert titles(kept) == ["Overview", "cpu", "Battery", "charge"]
    assert all(REQUIRES not in p for p in kept)
    assert titles(prune_panels(panels, lambda requirement: True)) == [
        "Overview",
        "cpu",
        "Network",
        "traffic",
        "Disks",
        "smart",
        "Battery",
        "charge",
    ]


def test_a_row_keeps_its_single_last_panel() -> None:
    panels = [panel("cpu"), panel("Battery", kind="row"), panel("charge")]
    assert titles(prune_panels(panels, lambda requirement: True)) == ["cpu", "Battery", "charge"]
    assert titles(prune_panels(panels[1:2], lambda requirement: True)) == []


def test_wanted_needs_every_requirement() -> None:
    item = panel("x", "a", "b")
    assert wanted(item, lambda requirement: True)
    assert not wanted(item, lambda requirement: requirement == "a")
    assert wanted(panel("y"), lambda requirement: False)


def test_build_drops_a_dashboard_whose_requirement_or_every_panel_is_gone() -> None:
    data = {
        "title": {"en": "Internet", "uk": "Інтернет"},
        REQUIRES: ["probes.internet"],
        "panels": [panel("ping", "probes.gateway")],
    }
    assert build(data, "en", lambda requirement: False) is None
    assert build(data, "en", lambda requirement: requirement == "probes.internet") is None
    built = build(data, "uk", lambda requirement: True)
    assert built == {"title": "Інтернет", "panels": [{"title": "ping", "type": "timeseries"}]}
    assert data[REQUIRES] == ["probes.internet"]
    assert build({"uid": "empty", "panels": []}, "en", lambda r: False) == {
        "uid": "empty",
        "panels": [],
    }
    assert build({"uid": "bare"}, "en", lambda r: False) == {"uid": "bare", "panels": []}


def two_hosts(data: dict[str, Any]) -> dict[str, Any]:
    central = data["hosts"]["central"]["modules"]
    edge = data["hosts"]["edge"]["modules"]
    for name in ("network", "smart", "probes", "gpu"):
        edge.pop(name, None)
    edge["node"] = {}
    central.pop("cloudflare")
    return data


def rendered_boards(data: dict[str, Any], modules: dict[str, Module]) -> dict[str, Any]:
    tree = render_once(manifest_from(data), modules)
    prefix = "central/grafana/dashboards/"
    return {
        path.removeprefix(prefix): json.loads(text)
        for path, text in tree.items()
        if path.startswith(prefix)
    }


def rendered_titles(item: dict[str, Any]) -> list[str]:
    return [p["title"] for p in item["panels"]]


def test_a_plain_fleet_gets_host_health_services_reliability_and_probes_without_empty_rows(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    found = rendered_boards(two_hosts(data), modules)
    assert {"host.json", "health.json", "services.json", "reliability.json", "probes.json"} <= set(
        found
    )
    assert "internet.json" not in found
    host = rendered_titles(found["host.json"])
    assert "Hardware temperatures" in host
    for absent in ("Network", "Drives (SMART)", "Battery", "Reboot required", "Traffic"):
        assert absent not in host
    reliability = rendered_titles(found["reliability.json"])
    assert "Availability, %" in reliability
    assert "Internet available, %" not in reliability
    assert "Since the last off-site copy" in reliability
    assert "Domain days left" in rendered_titles(found["probes.json"])
    assert REQUIRES not in json.dumps(found)


def test_internet_dashboard_follows_the_probes_on_the_edge_host(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    data = two_hosts(data)
    data["hosts"]["edge"]["modules"]["probes"] = {"internet": True}
    found = rendered_boards(data, modules)
    internet = rendered_titles(found["internet.json"])
    assert "Internet" in internet and "Gateway" not in internet and "Carrier" not in internet
    data["hosts"]["edge"]["modules"]["probes"]["gateway"] = "192.168.1.1"
    data["hosts"]["edge"]["modules"]["network"] = {"interfaces": ["eth0"]}
    internet = rendered_titles(rendered_boards(data, modules)["internet.json"])
    assert {"Gateway", "Carrier", "Link speed"} <= set(internet)
    assert "Wi-Fi signal" not in internet


def test_grafana_locale_translates_module_dashboards_only(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    data = two_hosts(data)
    data["hosts"]["central"]["modules"]["grafana"]["locale"] = "uk"
    found = rendered_boards(data, modules)
    assert found["host.json"]["title"] == "AyeOps / Хости"
    assert found["health.json"]["title"] == "AyeOps / Здоровʼя"
    labels = {v["name"]: v["label"] for v in found["host.json"]["templating"]["list"]}
    assert labels == {"group": "Група", "host": "Хост"}
    project = json.loads((ROOT / "examples" / "dashboards" / "orders.json").read_text("utf-8"))
    tree = render_once(manifest_from(data), modules)
    assert json.loads(tree["central/grafana/project-dashboards/orders.json"]) == project


def test_the_locale_falls_back_to_the_portal_and_then_english(data: dict[str, Any]) -> None:
    manifest = manifest_from(data)
    assert grafana_locale(manifest, {"locale": None}) == "en"
    assert grafana_locale(manifest, {"locale": "uk"}) == "uk"
    assert grafana_locale(manifest, {}) == "en"
    data["portal"] = {"locale": "uk"}
    assert grafana_locale(manifest_from(data), {"locale": None}) == "uk"
    assert grafana_locale(manifest_from(data), {"locale": "en"}) == "en"
    assert grafana_locale(manifest_from(data), {"locale": "de"}) == "uk"


def test_the_grafana_locale_setting_takes_only_known_languages(data: dict[str, Any]) -> None:
    data["hosts"]["central"]["modules"]["grafana"]["locale"] = "de"
    assert "module-settings" in rules_hit(data)


def test_blackbox_runs_for_urls_internet_or_a_gateway_only() -> None:
    assert not blackbox_wanted(None)
    assert not blackbox_wanted({})
    assert not blackbox_wanted({"http": [], "internet": False, "gateway": None, "domains": ["a.b"]})
    assert blackbox_wanted({"http": ["https://a.b"]})
    assert blackbox_wanted({"internet": True})
    assert blackbox_wanted({"gateway": "10.0.0.1"})


def test_examples_dashboards_exist(modules: dict[str, Module]) -> None:
    names = {path.name for module in modules.values() for path in module.dashboards}
    assert {
        "host.json",
        "internet.json",
        "health.json",
        "services.json",
        "reliability.json",
    } <= names
    assert all(Path(path).is_file() for module in modules.values() for path in module.dashboards)
