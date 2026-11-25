from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

from ayeops.cli import main
from ayeops.manifest import Manifest, load_manifest
from ayeops.mcp_catalog import metrics_of, status_mark
from ayeops.portal.graph import build_graph
from ayeops.portal.pages import collect_cards, portal_pages, stamp_day
from ayeops.registry import Module
from ayeops.render import render_tree
from ayeops.rules import Finding, check_manifest, check_warnings
from ayeops.stamp import Stamp
from ayeops.waivers import (
    WARN_DAYS,
    Waiver,
    days_left,
    manifest_waivers,
    parse_day,
    reason_of,
    today_in,
    until_of,
)
from tests.conftest import EXAMPLE, english, manifest_from

TODAY = date(2026, 11, 5)
METRICS = "services.web.metrics.none"
STATUS = "services.web.status.internal"
SKIP = "services.mcp.alerts.1.baseline.skip.0"
AUTH = "hosts.central.modules.ingress.without_auth"
DATABASE = "services.acme-db.status.internal"


def dated(reason: Any, days: int) -> dict[str, Any]:
    return {"reason": reason, "until": TODAY + timedelta(days=days)}


def central(data: dict[str, Any]) -> dict[str, Any]:
    modules: dict[str, Any] = data["hosts"]["central"]["modules"]
    return modules


def set_metrics(data: dict[str, Any], value: Any) -> None:
    data["services"]["web"]["metrics"] = {"none": value}


def set_status(data: dict[str, Any], value: Any) -> None:
    data["services"]["web"]["status"] = {"internal": value}


def set_skip(data: dict[str, Any], **entry: Any) -> None:
    skip = {"match": {"tool": "legacy-search"}, **entry}
    data["services"]["mcp"]["alerts"][1]["baseline"]["skip"] = [skip]


def set_auth(data: dict[str, Any], value: Any) -> None:
    del central(data)["auth"]
    central(data)["ingress"]["without_auth"] = value


def errors(data: dict[str, Any], modules: dict[str, Module], today: date = TODAY) -> list[Finding]:
    return check_manifest(manifest_from(data), modules, today)


def warnings(data: dict[str, Any], today: date = TODAY) -> list[Finding]:
    return check_warnings(manifest_from(data), today)


def test_the_plain_string_waivers_of_today_stay_valid_without_an_end(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    set_metrics(data, "no exporter yet")
    set_status(data, "only the team uses it")
    set_skip(data, reason="retired tool")
    set_auth(data, "reachable over the VPN only")
    assert errors(data, modules) == []
    assert warnings(data) == []
    found = {item.path: item for item in manifest_waivers(manifest_from(data))}
    assert set(found) == {METRICS, STATUS, SKIP, AUTH, DATABASE}
    assert all(not found[path].dated for path in (METRICS, STATUS, SKIP, AUTH))


def test_every_waiver_takes_a_reason_with_an_until_and_passes_before_the_window(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    set_metrics(data, dated("no exporter yet", WARN_DAYS + 1))
    set_status(data, dated("only the team uses it", WARN_DAYS + 1))
    set_skip(data, reason="retired tool", until=TODAY + timedelta(days=WARN_DAYS + 1))
    set_auth(data, dated("reachable over the VPN only", WARN_DAYS + 1))
    assert errors(data, modules) == []
    assert warnings(data) == []
    found = {item.path: item for item in manifest_waivers(manifest_from(data))}
    assert {path: item.rule for path, item in found.items()} == {
        METRICS: "service-metrics",
        STATUS: "service-status",
        SKIP: "baseline-skip",
        AUTH: "ingress-auth",
        DATABASE: "service-status",
    }
    assert found[METRICS].text == "no exporter yet"
    assert found[METRICS].until == TODAY + timedelta(days=WARN_DAYS + 1)


def test_an_until_written_as_a_quoted_string_counts_like_a_yaml_date(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    set_metrics(data, {"reason": "no exporter yet", "until": "2026-11-10"})
    assert [f.message for f in warnings(data)] == [
        "the service-metrics waiver 'no exporter yet' ends on 2026-11-10, in 5 days"
    ]


def test_an_internal_status_reason_with_an_until_may_be_in_several_languages(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    set_status(data, dated({"en": "only the team", "uk": "лише команда"}, 3))
    assert errors(data, modules) == []
    assert [str(f) for f in warnings(data)] == [
        f"warning [service-status] {STATUS}: the service-status waiver 'only the team'"
        " ends on 2026-11-08, in 3 days"
    ]


@pytest.mark.parametrize(
    ("days", "expected"),
    [
        (WARN_DAYS + 1, []),
        (WARN_DAYS, ["ends on 2026-11-19, in 14 days"]),
        (1, ["ends on 2026-11-06, in 1 day"]),
        (0, ["ends on 2026-11-05, today"]),
        (-1, []),
    ],
)
def test_a_waiver_warns_from_fourteen_days_before_its_until_through_that_day(
    data: dict[str, Any], days: int, expected: list[str]
) -> None:
    set_metrics(data, dated("no exporter yet", days))
    assert [f.message.split("' ", 1)[1] for f in warnings(data)] == expected
    assert all(f.severity == "warning" and f.rule == "service-metrics" for f in warnings(data))


@pytest.mark.parametrize(("days", "fails"), [(0, False), (-1, True), (-30, True)])
def test_a_waiver_fails_the_check_from_the_day_after_its_until(
    data: dict[str, Any], modules: dict[str, Module], days: int, fails: bool
) -> None:
    set_metrics(data, dated("no exporter yet", days))
    assert bool(errors(data, modules)) is fails


def test_an_expired_waiver_names_the_rule_the_path_the_reason_and_the_date(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    set_skip(data, reason="retired tool", until=date(2026, 10, 1))
    assert [str(f) for f in errors(data, modules)] == [
        f"error [baseline-skip] {SKIP}: the baseline-skip waiver 'retired tool' ended on"
        " 2026-10-01, 35 days ago: drop the waiver and meet the rule, or set a later until"
    ]
    set_skip(data, reason="retired tool", until=date(2026, 11, 4))
    assert "ended on 2026-11-04, 1 day ago" in errors(data, modules)[0].message


def test_an_expired_waiver_of_the_ingress_fails_ingress_auth(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    set_auth(data, dated("reachable over the VPN only", -2))
    found = errors(data, modules)
    assert [(f.path, f.rule) for f in found] == [(AUTH, "ingress-auth")]
    assert "2026-11-03, 2 days ago" in found[0].message


def test_without_auth_is_no_waiver_while_auth_runs_on_the_host(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    central(data)["ingress"]["without_auth"] = dated("left over", -5)
    assert errors(data, modules) == []
    assert AUTH not in {item.path for item in manifest_waivers(manifest_from(data))}


def test_a_waiver_of_a_planned_service_is_not_checked(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    data["services"]["web"]["state"] = "planned"
    set_metrics(data, dated("no exporter yet", -3))
    assert errors(data, modules) == []


def test_a_skip_of_a_sql_check_baseline_carries_an_until_too(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    data["services"]["web"]["checks"] = {
        "orders": {
            "type": "sql",
            "datasource": "shop",
            "query": "select count(*) from orders",
            "label": "shop",
            "alert": {
                "severity": "warning",
                "runbook": "web-down",
                "for": "10m",
                "baseline": {
                    "skip": [{"match": {"shop": "old"}, "reason": "closed", "until": "2026-11-01"}]
                },
            },
        }
    }
    found = {item.path: item for item in manifest_waivers(manifest_from(data))}
    item = found["services.web.checks.orders.alert.baseline.skip.0"]
    assert (item.rule, item.text, days_left(item, TODAY)) == ("baseline-skip", "closed", -4)


@pytest.mark.parametrize(
    "until",
    [
        "2027-02-30",
        "2027-1-5",
        "20270105",
        "2027-W01-1",
        "next year",
        "",
        2027,
        [2027],
        datetime(2027, 1, 1, 10, tzinfo=UTC),
    ],
)
def test_an_until_that_is_not_a_day_fails_waiver_until(
    data: dict[str, Any], modules: dict[str, Module], until: Any
) -> None:
    set_metrics(data, {"reason": "no exporter yet", "until": until})
    found = errors(data, modules)
    assert [(f.path, f.rule) for f in found] == [(f"{METRICS}.until", "waiver-until")]
    assert "YYYY-MM-DD" in found[0].message
    assert warnings(data) == []


def test_an_until_with_a_time_is_named_with_its_time() -> None:
    with pytest.raises(ValueError, match="without a time"):
        parse_day(datetime(2027, 1, 1, 10, tzinfo=UTC))


@pytest.mark.parametrize(
    "value",
    [
        {"until": "2027-01-01"},
        {"reason": "x", "until": "2027-01-01", "owner": "ops"},
        {"why": "x"},
    ],
)
def test_the_dated_form_takes_reason_and_until_only(
    data: dict[str, Any], modules: dict[str, Module], value: dict[str, Any]
) -> None:
    set_metrics(data, value)
    assert {f.rule for f in errors(data, modules)} == {"schema"}
    assert warnings(data) == []
    set_auth(data, value)
    set_metrics(data, "fine")
    assert {f.rule for f in errors(data, modules)} == {"module-settings"}


def test_a_dated_waiver_still_needs_a_real_reason(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    set_metrics(data, dated("TODO: why", 100))
    set_status(data, dated("", 100))
    assert {(f.path, f.rule) for f in errors(data, modules)} == {
        ("services.web", "service-metrics"),
        ("services.web", "service-status"),
    }


def test_waivers_need_until_makes_every_waiver_name_its_end(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    data["rules"] = {"waivers_need_until": True}
    set_metrics(data, "no exporter yet")
    set_status(data, dated("only the team uses it", 100))
    set_auth(data, "reachable over the VPN only")
    found = errors(data, modules)
    assert [(f.path, f.rule) for f in found] == [
        (AUTH, "waiver-until"),
        (METRICS, "waiver-until"),
    ]
    assert str(found[1]) == (
        f"error [waiver-until] {METRICS}: rules.waivers_need_until is on, so the"
        " service-metrics waiver 'no exporter yet' needs an end:"
        " {reason: <why>, until: YYYY-MM-DD}"
    )
    set_skip(data, reason="retired tool")
    assert (SKIP, "waiver-until") in {(f.path, f.rule) for f in errors(data, modules)}


def test_waivers_need_until_accepts_the_example_whose_one_waiver_is_dated(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    data["rules"] = {"waivers_need_until": True}
    assert errors(data, modules) == []
    data["rules"] = {"waivers_need_until": False}
    set_metrics(data, "no exporter yet")
    assert errors(data, modules) == []


def test_rules_takes_known_switches_only(data: dict[str, Any], modules: dict[str, Module]) -> None:
    data["rules"] = {"waivers_need_owner": True}
    assert {f.rule for f in errors(data, modules)} == {"schema"}
    data["rules"] = {"waivers_need_until": "yes"}
    assert {f.rule for f in errors(data, modules)} == {"schema"}


def test_warnings_wait_for_a_manifest_that_passes_the_schema(data: dict[str, Any]) -> None:
    set_metrics(data, dated("no exporter yet", 3))
    data["services"]["web"]["colour"] = "blue"
    assert warnings(data) == []


def test_today_is_the_day_in_the_manifest_timezone() -> None:
    late = datetime(2026, 11, 5, 23, 30, tzinfo=UTC)
    assert today_in("Europe/Kyiv", late) == date(2026, 11, 6)
    assert today_in("America/New_York", late) == date(2026, 11, 5)
    assert today_in("Mars/Olympus", late) == date(2026, 11, 5)
    before = datetime.now(UTC).date()
    today = today_in("UTC")
    assert today in {before, datetime.now(UTC).date()}


def test_check_manifest_measures_against_today_without_a_day(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    set_metrics(data, {"reason": "no exporter yet", "until": date(2000, 1, 1)})
    assert check_manifest(manifest_from(data), modules) != []
    set_metrics(data, {"reason": "no exporter yet", "until": date.today() + timedelta(days=3)})
    assert check_manifest(manifest_from(data), modules) == []
    assert len(check_warnings(manifest_from(data))) == 1


def test_reason_and_until_read_both_forms() -> None:
    assert reason_of("why") == "why"
    assert until_of("why") is None
    assert reason_of({"en": "why"}) == {"en": "why"}
    assert until_of({"en": "why"}) is None
    assert reason_of({"reason": "why", "until": "2027-01-01"}) == "why"
    assert until_of({"reason": "why", "until": "2027-01-01"}) == "2027-01-01"
    assert Waiver("p", "r", {"uk": "чому", "en": " why "}).text == "why"


EXAMPLE_SKIP = (
    "the baseline-skip waiver 'legacy-search is retired and answers only until the clients move'"
)


def test_check_prints_a_warning_inside_the_window_and_still_passes(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["check", str(EXAMPLE), "--today", "2029-12-25"]) == 0
    out = capsys.readouterr()
    assert out.err == (
        f"warning [baseline-skip] {SKIP}: {EXAMPLE_SKIP} ends on 2030-01-01, in 7 days\n"
    )
    assert out.out == f"{EXAMPLE}: ok, 1 warning(s)\n"
    assert main(["check", str(EXAMPLE), "--today", "2029-12-17"]) == 0
    assert capsys.readouterr() == (f"{EXAMPLE}: ok\n", "")


def test_check_strict_fails_on_a_warning_and_passes_without_one(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["check", str(EXAMPLE), "--strict", "--today", "2029-12-25"]) == 1
    err = capsys.readouterr().err.splitlines()
    assert err[0].startswith("warning [baseline-skip] ")
    assert err[1] == f"{EXAMPLE}: 1 warning(s), failed under --strict"
    assert main(["check", str(EXAMPLE), "--strict", "--today", "2029-12-17"]) == 0
    assert capsys.readouterr() == (f"{EXAMPLE}: ok\n", "")


def test_check_fails_the_day_after_until_and_render_refuses(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["check", str(EXAMPLE), "--today", "2030-01-01"]) == 0
    capsys.readouterr()
    assert main(["check", str(EXAMPLE), "--today", "2030-01-02"]) == 1
    assert capsys.readouterr().err == (
        f"error [baseline-skip] {SKIP}: {EXAMPLE_SKIP} ended on 2030-01-01, 1 day ago:"
        " drop the waiver and meet the rule, or set a later until\n"
        f"{EXAMPLE}: 1 error(s)\n"
    )


def test_the_example_reads_its_unquoted_until_as_a_yaml_date() -> None:
    manifest = load_manifest(EXAMPLE)
    found = {item.path: item for item in manifest_waivers(manifest)}
    assert found[SKIP].until == date(2030, 1, 1)


def test_check_refuses_a_today_that_is_not_a_day(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as stopped:
        main(["check", str(EXAMPLE), "--today", "2026-02-30"])
    assert stopped.value.code == 2
    assert "'2026-02-30' is not a day as YYYY-MM-DD" in capsys.readouterr().err


def cards(data: dict[str, Any], modules: dict[str, Module], today: date) -> dict[str, Any]:
    manifest = manifest_from(data)
    graph = build_graph(manifest, modules)
    return {card.name: card for card in collect_cards(manifest, modules, graph, today)}


def test_the_portal_lists_a_waiver_with_its_days_left(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    html = english(
        portal_pages(manifest_from(data), modules, Stamp("2029-12-20 08:00 UTC"))["services.html"]
    )
    assert (
        '<ul class="alerts waivers"><li><span class="sev">baseline-skip</span>legacy-search is'
        " retired and answers only until the clients move <em>until 2030-01-01</em>"
        ' <b class="left" data-until="2030-01-01">12 d left</b></li></ul>'
    ) in html


def test_a_portal_card_turns_amber_inside_the_window_and_red_after_until(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    set_metrics(data, dated("no exporter yet", 0))
    data["hosts"]["central"]["compose"] = [str(EXAMPLE.parent / "apps" / "web.compose.yaml")]
    web = cards(data, modules, TODAY)["web"]
    assert web.gaps == []
    assert web.amber is not None
    assert web.amber["en"] == (
        "the service-metrics waiver ends on 2026-11-05: meet the rule or renew it"
    )
    web = cards(data, modules, TODAY + timedelta(days=1))["web"]
    assert [gap["en"] for gap in web.gaps] == [
        "the service-metrics waiver ended on 2026-11-05: meet the rule or renew it"
    ]
    assert cards(data, modules, TODAY - timedelta(days=WARN_DAYS))["web"].amber is not None
    assert cards(data, modules, TODAY - timedelta(days=WARN_DAYS + 1))["web"].amber is None


def test_a_portal_card_shows_an_undated_waiver_as_open_ended(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    set_auth(data, "reachable over the VPN only")
    found = cards(data, modules, TODAY)
    assert [(item.path, left) for item, left in found["traefik"].waivers] == [(AUTH, None)]
    html = english(
        portal_pages(manifest_from(data), modules, Stamp("2026-11-05 00:00 UTC"))["services.html"]
    )
    assert "reachable over the VPN only <em>no end date</em>" in html


def test_the_portal_day_is_the_build_day_of_the_stamp() -> None:
    assert stamp_day(Stamp("2026-01-01 00:00 UTC")) == date(2026, 1, 1)
    before = datetime.now(UTC).date()
    day = stamp_day(Stamp())
    assert day in {before, datetime.now(UTC).date()}


def test_the_portal_shows_the_reason_of_a_dated_internal_status(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    set_status(data, dated({"en": "only the team", "uk": "лише команда"}, 100))
    web = cards(data, modules, TODAY)["web"]
    assert web.status == ("internal", {"en": "only the team", "uk": "лише команда"})


def test_the_mcp_catalog_gives_the_reason_of_a_dated_waiver() -> None:
    assert metrics_of({"none": {"reason": "no exporter", "until": date(2030, 1, 1)}}) == {
        "none": "no exporter"
    }
    assert metrics_of({"none": "no exporter"}) == {"none": "no exporter"}
    mark = status_mark({"internal": {"reason": {"en": "team", "uk": "команда"}}}, "uk")
    assert mark == {"internal": "команда"}


def test_a_dated_without_auth_renders_like_the_plain_reason(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    set_auth(data, "reachable over the VPN only")
    plain = render_tree(manifest_from(data), modules, Stamp("t", "c"))
    central(data)["ingress"]["without_auth"] = dated("reachable over the VPN only", 400)
    later = render_tree(manifest_from(data), modules, Stamp("t", "c"))
    assert set(plain) == set(later)
    assert {path for path in plain if plain[path] != later[path]} <= {
        "central/portal/services.html"
    }


def test_a_manifest_is_unchanged_by_reading_its_waivers(data: dict[str, Any]) -> None:
    set_metrics(data, dated("no exporter yet", 3))
    before = yaml.safe_dump(data)
    manifest_waivers(Manifest(data, EXAMPLE.parent))
    assert yaml.safe_dump(data) == before
