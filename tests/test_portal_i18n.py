from __future__ import annotations

import copy
import json
import re
from typing import Any

import pytest
import yaml

from ayeops.i18n import LANGS, localized, pick, plural_index
from ayeops.manifest import schema_errors
from ayeops.portal.disks import (
    DAYS_CRIT,
    DAYS_WARN,
    FORECAST,
    TREND_SECONDS,
    WINDOW,
    alert_threshold,
    attribute_names,
    catalog_expr,
    disk_limits,
    disk_queries,
    expr_window,
    fs_matcher,
)
from ayeops.portal.i18n import STRINGS, attrs, both, counted, i18n_json, js_strings, tr
from ayeops.portal.pages import portal_pages
from ayeops.registry import Module, load_modules
from ayeops.render import host_compose, host_scrape
from ayeops.status import status_document
from tests.conftest import EXAMPLE_STAMP, check_once, in_lang, manifest_from, render_once

PLACEHOLDER = re.compile(r"\{[a-z_]+\}")


@pytest.fixture
def own_modules() -> dict[str, Module]:
    return copy.deepcopy(load_modules())


def pages(data: dict[str, Any], modules: dict[str, Module]) -> dict[str, str]:
    return portal_pages(manifest_from(data), modules, EXAMPLE_STAMP)


def live_blob(html: str) -> dict[str, Any]:
    match = re.search(r'<script type="application/json" id="aye-live">(.*?)</script>', html)
    assert match
    found: dict[str, Any] = json.loads(match.group(1))
    return found


def findings(data: dict[str, Any], modules: dict[str, Module]) -> set[tuple[str, str]]:
    return {(f.path, f.rule) for f in check_once(manifest_from(data), modules)}


def planned(data: dict[str, Any], name: str = "search", **extra: Any) -> dict[str, Any]:
    data["services"][name] = {
        "host": "central",
        "state": "planned",
        "description": {"en": "Full-text search", "uk": "Повнотекстовий пошук"},
        **extra,
    }
    return data


def test_pick_prefers_the_language_then_english_then_whatever_exists() -> None:
    assert pick("plain", "uk") == "plain"
    assert pick(None, "uk") == ""
    assert pick({"en": "one", "uk": "одна"}, "uk") == "одна"
    assert pick({"en": "one", "uk": "одна"}, "en") == "one"
    assert pick({"en": "one"}, "uk") == "one"
    assert pick({"uk": "одна"}, "en") == "одна"
    assert pick({"ru": "одна"}, "en") == "одна"
    assert pick({"en": "", "uk": "одна"}, "en") == "одна"
    assert pick({}, "en") == ""


def test_localized_fills_every_language() -> None:
    assert localized({"uk": "логи"}) == {"en": "логи", "uk": "логи"}
    assert localized("logs") == {"en": "logs", "uk": "logs"}


@pytest.mark.parametrize(
    ("count", "form"),
    [
        (0, 2),
        (1, 0),
        (2, 1),
        (4, 1),
        (5, 2),
        (11, 2),
        (12, 2),
        (14, 2),
        (21, 0),
        (22, 1),
        (25, 2),
        (101, 0),
        (111, 2),
        (112, 2),
        (122, 1),
        (1004, 1),
    ],
)
def test_ukrainian_plurals_follow_the_one_few_many_rule(count: int, form: int) -> None:
    assert plural_index("uk", count) == form


@pytest.mark.parametrize(("count", "form"), [(0, 1), (1, 0), (2, 1), (21, 1)])
def test_english_plurals_are_one_and_other(count: int, form: int) -> None:
    assert plural_index("en", count) == form


def test_counted_picks_the_form() -> None:
    assert counted("count.services", 1) == {"en": "1 service", "uk": "1 сервіс"}
    assert counted("count.services", 3) == {"en": "3 services", "uk": "3 сервіси"}
    assert counted("count.services", 11) == {"en": "11 services", "uk": "11 сервісів"}


def test_every_string_has_every_language_with_the_same_placeholders() -> None:
    for key, texts in STRINGS.items():
        assert set(texts) == set(LANGS), key
        assert all(texts[lang].strip() for lang in LANGS), key
        holes = {lang: sorted(set(PLACEHOLDER.findall(texts[lang]))) for lang in LANGS}
        assert len({tuple(found) for found in holes.values()}) == 1, key


def test_ukrainian_strings_use_only_keyboard_letters() -> None:
    allowed = re.compile(
        r"^["
        r"\x20-\x7e\u0410-\u042f\u0430-\u044f"
        r"\u0406\u0456\u0407\u0457\u0404\u0454\u0490\u0491"
        r"\u02bc\u2019\xab\xbb\u2014\u2013\u2026\xb7\u2197\xb0\u2318%"
        r"]*$"
    )
    for key, texts in STRINGS.items():
        assert allowed.match(texts["uk"]), key


def test_both_renders_one_span_per_language_only_when_they_differ() -> None:
    assert both("same") == "same"
    assert both({"en": "a<b", "uk": "а"}) == (
        '<span lang="en" data-l="en">a&lt;b</span><span lang="uk" data-l="uk">а</span>'
    )
    assert both({"en": "a", "uk": "б"}, "tspan") == (
        '<tspan data-l="en">a</tspan><tspan data-l="uk">б</tspan>'
    )


def test_attributes_carry_every_language_for_the_switcher() -> None:
    assert attrs(title="copy") == ' title="copy"'
    rendered = attrs(aria_label=tr("search.aria"))
    assert rendered.startswith(' aria-label="Search services" data-i18n="')
    blob = json.loads(rendered.split('data-i18n="')[1][:-1].replace("&quot;", '"'))
    assert blob == {"aria-label": tr("search.aria")}


def test_the_script_strings_cover_every_key_the_scripts_ask_for() -> None:
    from ayeops.portal.pages import asset

    asked = set(
        re.findall(r"T\('((?:js|diagram)\.[a-z_.]+)'", asset("live.js") + asset("client.js"))
    )
    asked |= {f"js.{state}" for state in ("live", "down", "nodata")}
    for lang in LANGS:
        assert asked <= set(js_strings()[lang])
    assert "</" not in i18n_json()


def test_the_page_defaults_to_english_and_takes_the_manifest_locale(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    assert '<html lang="en" data-lang="en">' in pages(data, modules)["index.html"]
    data["portal"] = {"locale": "uk"}
    html = pages(data, modules)["index.html"]
    assert '<html lang="uk" data-lang="uk">' in html
    assert 'data-set-lang="uk" lang="uk" aria-pressed="true"' in html
    assert 'data-set-lang="en" lang="en" aria-pressed="false"' in html


def test_the_portal_locale_is_validated(data: dict[str, Any]) -> None:
    data["portal"] = {"locale": "fr"}
    assert any(path == "portal.locale" for path, _ in schema_errors(data))
    data["portal"] = {"locale": "uk"}
    assert schema_errors(data) == []


def test_the_language_choice_is_remembered_and_guarded(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    html = pages(data, modules)["index.html"]
    head = html[: html.index("</head>")]
    assert "try{var l=localStorage.getItem('aye-lang')" in head
    assert '["en", "uk"].indexOf(l)>=0' in head
    assert "}catch(e){}" in head
    assert 'html[data-lang="uk"] [data-l]:not([data-l="uk"]){display:none}' in head
    script = html[html.rindex("<script>") :]
    assert "store.set('aye-lang', button.dataset.setLang)" in script


def test_flow_labels_written_in_both_languages_show_the_current_one(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    data["flows"][0]["label"] = {"en": "alert messages", "uk": "алерти"}
    html = pages(data, modules)["index.html"]
    flows = html[html.index('id="flows"') :]
    assert "алерти" in in_lang(flows, "uk")
    assert "алерти" not in in_lang(flows, "en")
    assert "alert messages" in in_lang(flows, "en")
    assert "alert messages" not in in_lang(flows, "uk")


def test_a_label_in_one_language_falls_back_in_the_other(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    data["flows"][0]["label"] = {"uk": "алерти"}
    assert schema_errors(data) == []
    flows = pages(data, modules)["index.html"]
    flows = flows[flows.index('id="flows"') :]
    assert "алерти" in in_lang(flows, "en")


def test_the_ukrainian_view_has_no_english_chrome(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    result = pages(data, modules)
    index = in_lang(result["index.html"], "uk")
    services = in_lang(result["services.html"], "uk")
    for english in ("Where things run", "Monitoring and access", "Data flows", "Dependencies"):
        assert f">{english}<" not in index
    assert "Де що працює" in index
    for english in ("Access and details", ">Open ↗<", ">described<", "no status mark"):
        assert english not in services
    assert "Доступ і деталі" in services


def test_descriptions_and_status_names_may_be_maps(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    web = data["services"]["web"]
    web["description"] = {"en": "Public website", "uk": "Публічний сайт"}
    web["status"]["public"] = {"en": "Website", "uk": "Сайт"}
    assert schema_errors(data) == []
    services = pages(data, modules)["services.html"]
    assert "Публічний сайт" in in_lang(services, "uk")
    assert "Public website" in in_lang(services, "en")
    data["status_page"] = {**(data.get("status_page") or {}), "locale": "uk"}
    titles = {c["title"] for c in status_document(manifest_from(data))["components"]}
    assert "Сайт" in titles


def test_an_empty_translation_still_counts_as_missing_description(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    data["services"]["web"]["description"] = {"en": "Public website", "uk": ""}
    assert ("services.web", "service-description") in findings(data, modules)


def test_a_planned_service_needs_only_a_description(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    found = findings(planned(data), modules)
    assert not {f for f in found if f[0].startswith("services.search")}
    del data["services"]["search"]["description"]
    found = findings(data, modules)
    assert {f for f in found if f[0].startswith("services.search")} == {
        ("services.search", "service-description")
    }


def test_a_reserved_service_is_not_deployed_scraped_alerted_or_on_the_status_page(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    data["services"]["cadvisor"] = {
        "host": "edge",
        "state": "disabled",
        "description": "Container metrics, off for now",
        "alerts": [
            {
                "name": "cadvisor-silent",
                "title": "cAdvisor silent",
                "expr": "up == bool 0",
                "severity": "warning",
                "runbook": "https://runbooks.acme.example/cadvisor",
            }
        ],
        "checks": {
            "page": {"type": "http", "url": "https://cadvisor.acme.example", "interval": "60s"}
        },
        "status": {"public": "Containers"},
    }
    manifest = manifest_from(data)
    assert "cadvisor" not in host_compose(manifest, "edge", modules)["services"]
    scrape = host_scrape(manifest, "edge", modules)
    assert scrape is not None
    targets = [
        target
        for job in scrape["scrape_configs"]
        for group in job.get("static_configs") or []
        for target in group.get("targets") or []
    ]
    assert targets and not any(str(t).startswith("cadvisor:") for t in targets)
    assert all(job.get("static_configs") for job in scrape["scrape_configs"])
    tree = render_once(manifest, modules)
    rules = yaml.safe_load(tree["central/grafana/provisioning/alerting/rules.yaml"])
    titles = {rule["title"] for group in rules["groups"] for rule in group["rules"]}
    assert "cAdvisor silent" not in titles
    assert "cadvisor" not in json.dumps({k: v for k, v in tree.items() if "blackbox" in k})
    assert all(c["title"] != "Containers" for c in status_document(manifest)["components"])
    assert ("services.cadvisor", "service-provided") not in findings(data, modules)


def test_an_active_service_keeps_its_scrape_target(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    scrape = host_scrape(manifest_from(data), "edge", modules)
    assert scrape is not None
    assert "cadvisor:8080" in json.dumps(scrape)


def test_a_planned_service_shows_dashed_with_a_badge_everywhere(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    data["flows"].append({"from": "web", "to": "search", "label": "queries", "state": "planned"})
    result = pages(planned(data), modules)
    index = in_lang(result["index.html"], "en")
    assert re.search(
        r'<a class="chip reserved planned" href="services.html#svc-central-search"', index
    )
    nodes = re.findall(r'<g class="node reserved planned" data-node="central/search"', index)
    assert len(nodes) == 2
    assert "dashed box: planned or disabled, not deployed" in index
    services = in_lang(result["services.html"], "en")
    assert 'class="svc reserved planned" id="svc-central-search"' in services
    assert 'data-value="planned" aria-pressed="false">Planned <b>1</b>' in services
    assert "reserved: not deployed, scraped or alerted" in services
    assert "central/search" not in live_blob(result["services.html"])["services"]


def test_a_disabled_flow_is_drawn_muted(data: dict[str, Any], modules: dict[str, Module]) -> None:
    data["flows"][1]["state"] = "disabled"
    assert schema_errors(data) == []
    index = pages(data, modules)["index.html"]
    flows = index[index.index('id="flows"') :]
    assert '<g class="edge dash muted"' in flows


def test_thresholds_are_read_from_the_alert_expressions() -> None:
    assert alert_threshold("(1 - a / b) * 100 > bool 80") == 80
    assert alert_threshold("x < bool 0.5") == 0.5
    assert alert_threshold("(a > bool 0) * (b < bool 7)") == 7
    assert alert_threshold("a >= bool 3") == 3
    assert alert_threshold("a > 3") is None
    assert expr_window('predict_linear(a{x="y"}[1d], 7 * 86400)') == "1d"
    assert expr_window("rate(a[5m]) + rate(b[1h])") == "5m"
    assert expr_window("a") is None
    expr = 'node_filesystem_avail_bytes{fstype!~"tmpfs|overlay"} > bool 1'
    assert fs_matcher(expr) == 'fstype!~"tmpfs|overlay"'
    assert fs_matcher("a") is None
    assert attribute_names('a{attribute_name=~"A|B", x="raw"}') == "A|B"
    assert attribute_names("a") is None


def test_disk_limits_come_from_the_module_catalog(own_modules: dict[str, Module]) -> None:
    assert disk_limits(own_modules) == {
        "warn": 80.0,
        "crit": 90.0,
        "days_warn": DAYS_WARN,
        "days_crit": DAYS_CRIT,
        "wear_warn": 80.0,
        "temp_warn": 60.0,
    }
    for alert in own_modules["node"].alerts:
        if alert["name"] == "disk-full":
            alert["expr"] = str(alert["expr"]).replace("> bool 80", "> bool 75")
    assert disk_limits(own_modules)["warn"] == 75.0


def test_disk_queries_cover_every_filesystem_forecast_and_smart(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    spec = live_blob(pages(data, modules)["index.html"])
    queries = spec["queries"]
    fstype = 'fstype!~"tmpfs|overlay|squashfs|ramfs|devtmpfs|nsfs"'
    assert fstype in queries["fs_size"] and fstype in queries["fs_avail"]
    assert 'host=~"central|edge"' in queries["fs_size"]
    assert "[1d]" in queries["fs_days"] and 'host=~"edge"' in queries["fs_days"]
    assert 'host=~"edge"' in queries["smart_status"]
    assert "Reallocated_Sector_Ct|Current_Pending_Sector" in queries["smart_sectors"]
    assert "increase(" in queries["smart_growth"] and "[1d]" in queries["smart_growth"]
    assert 'temperature_type="current"' in queries["smart_temp"]
    assert spec["ranges"]["fs_used"]["seconds"] == TREND_SECONDS
    assert spec["hosts"]["edge"] == {"node": True, "battery": True, "forecast": True, "smart": True}
    assert spec["disks"]["crit"] == 90.0


def test_disk_queries_skip_what_no_host_runs(modules: dict[str, Module]) -> None:
    assert disk_queries(modules, [], [], []) == {}
    assert set(disk_queries(modules, ["a"], [], [])) == {"fs_size", "fs_avail", "host_disk_days"}


def test_a_disk_has_one_forecast_on_a_card(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    alert = expr_window(catalog_expr(modules, FORECAST))
    assert alert == "1d"
    queries = live_blob(pages(data, modules)["index.html"])["queries"]
    tile, row = queries["host_disk_days"], queries["fs_days"]
    assert WINDOW.findall(tile) == WINDOW.findall(row) == [alert, alert]
    found = disk_queries(modules, ["a"], ["a"], [])
    root = found["fs_days"].replace("by (host, mountpoint)", "by (host)")
    assert found["host_disk_days"] == root.replace('host=~"a"', 'mountpoint="/",host=~"a"')


def test_every_node_host_gets_a_disks_block_that_says_when_it_has_nothing(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    index = in_lang(pages(data, modules)["index.html"], "en")
    for host in data["hosts"]:
        assert f'<section class="disks" data-host="{host}"><h4>Disks</h4>' in index
    assert "Disk usage comes live from the metrics server; nothing has arrived yet." in index
    script = index[index.rindex("<script>") :]
    assert "T('js.disks.none')" in script and "T('js.disks.off')" in script


def test_an_edge_label_too_long_in_one_language_still_shows_in_the_other() -> None:
    from ayeops.portal.diagram import place_label
    from ayeops.portal.layout import Point

    taken = [(60.0, -20.0, 200.0, 5.0)]
    shown = place_label({"en": "logs", "uk": "дуже довгий підпис потоку"}, Point(0, 0), taken)
    assert shown == {"en": "logs", "uk": ""}
    assert len(taken) == 2
    assert place_label({"en": "a" * 40, "uk": "b" * 40}, Point(0, 0), taken) is None
    assert len(taken) == 2


def test_disk_queries_are_exact(modules: dict[str, Module]) -> None:
    fs = 'fstype!~"tmpfs|overlay|squashfs|ramfs|devtmpfs|nsfs"'
    where = "{" + fs + ',host=~"a"}'
    raw = '{attribute_name=~"Reallocated_Sector_Ct|Current_Pending_Sector|Offline_Uncorrectable"'
    raw += ',attribute_value_type="raw",host=~"b"}'
    labels = "host, mountpoint, device, fstype"
    trend = f"node_filesystem_avail_bytes{where}[1d]"
    root = "{" + fs + ',mountpoint="/",host=~"a"}'
    tile = f"node_filesystem_avail_bytes{root}[1d]"
    assert disk_queries(modules, ["a"], ["a"], ["b"]) == {
        "fs_size": f"max by ({labels}) (node_filesystem_size_bytes{where})",
        "fs_avail": f"max by ({labels}) (node_filesystem_avail_bytes{where})",
        "host_disk_days": f"min by (host) (node_filesystem_avail_bytes{root}"
        f" / (predict_linear({tile}, 0) - predict_linear({tile}, 86400)))",
        "fs_days": f"min by (host, mountpoint) (node_filesystem_avail_bytes{where}"
        f" / (predict_linear({trend}, 0) - predict_linear({trend}, 86400)))",
        "smart_status": 'max by (host, device) (smartctl_device_smart_status{host=~"b"})',
        "smart_wear": 'max by (host, device) (smartctl_device_percentage_used{host=~"b"})',
        "smart_temp": "max by (host, device) (smartctl_device_temperature"
        '{temperature_type="current",host=~"b"})',
        "smart_sectors": f"max by (host, device, attribute_name) (smartctl_device_attribute{raw})",
        "smart_growth": f"max by (host, device) (increase(smartctl_device_attribute{raw}[1d]))",
    }


def test_disk_ranges_ask_for_thirty_days_every_six_hours(modules: dict[str, Module]) -> None:
    from ayeops.portal.disks import disk_ranges

    assert disk_ranges(modules, []) == {}
    where = '{fstype!~"tmpfs|overlay|squashfs|ramfs|devtmpfs|nsfs",host=~"a|b"}'
    assert disk_ranges(modules, ["a", "b"]) == {
        "fs_used": {
            "query": f"max by (host, mountpoint) (100 * (1 - node_filesystem_avail_bytes{where}"
            f" / node_filesystem_size_bytes{where}))",
            "seconds": 2592000,
            "step": 21600,
        }
    }
    assert (DAYS_WARN, DAYS_CRIT) == (14, 3)


def test_a_catalog_alert_without_an_expression_gives_no_threshold(
    own_modules: dict[str, Module],
) -> None:
    from ayeops.portal.disks import catalog_expr

    assert catalog_expr(own_modules, "no-such-alert") == ""
    for alert in own_modules["hygiene"].alerts:
        if alert["name"] == "disk-critical":
            del alert["expr"]
    assert catalog_expr(own_modules, "disk-critical") == ""
    assert disk_limits(own_modules)["crit"] is None


def test_without_catalog_windows_the_forecast_and_growth_queries_drop_out(
    own_modules: dict[str, Module],
) -> None:
    for module in ("disk-forecast", "smart"):
        for alert in own_modules[module].alerts:
            alert["expr"] = str(alert["expr"]).replace("[1d]", "")
    found = disk_queries(own_modules, ["a"], ["a"], ["a"])
    assert "fs_days" not in found and "smart_growth" not in found
    assert "host_disk_days" not in found
    assert "smart_sectors" in found
    for alert in own_modules["smart"].alerts:
        alert["expr"] = "x > bool 1"
    found = disk_queries(own_modules, ["a"], [], ["a"])
    assert "smart_sectors" not in found
    assert found["smart_status"].endswith('{host=~"a"})')
