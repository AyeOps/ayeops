from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
import yaml

from ayeops.cli import main
from ayeops.duties import (
    ALERT,
    MAX_EVERY,
    OWN_RUNBOOK,
    RUNBOOK,
    SCOPE,
    SUMMARY,
    TITLE,
    Duty,
    duty_labels,
    duty_line,
    duty_problems,
    duty_series,
    listing_problems,
    manifest_duties,
    overdue_alert,
    overdue_at,
    plural,
    run,
    runbook_annotation,
    state,
    zone_of,
)
from ayeops.manifest import Manifest
from ayeops.registry import Module
from tests.conftest import EXAMPLE, example_data, manifest_from, render_once, rules_hit

TODAY = date(2026, 11, 26)
RULES = "central/grafana/provisioning/alerting/rules.yaml"
BASE = "https://runbooks.example"


def duty(**fields: Any) -> Duty:
    spec: dict[str, Any] = {
        "name": "access-review",
        "title": "Review the sign-ins",
        "every": "90d",
        "last_done": date(2026, 10, 1),
        **fields,
    }
    return Duty(**spec)


def with_duties(data: dict[str, Any], duties: dict[str, Any]) -> dict[str, Any]:
    data["duties"] = duties
    return data


def manifest_with(duties: dict[str, Any], **top: Any) -> Manifest:
    hosts = {"one": {"arch": "amd64", "modules": {}}}
    data: dict[str, Any] = {"version": 1, "project": "acme", "hosts": hosts, **top}
    if duties:
        data["duties"] = duties
    return Manifest(data, EXAMPLE.parent)


def spec(**fields: Any) -> dict[str, Any]:
    return {
        "title": "Review the sign-ins",
        "every": "90d",
        "last_done": date(2026, 10, 1),
        **fields,
    }


def write(tmp_path: Path, duties: dict[str, Any], **top: Any) -> Path:
    data: dict[str, Any] = {
        "version": 1,
        "project": "acme",
        "hosts": {"one": {"arch": "amd64", "modules": {}}},
        **top,
    }
    if duties:
        data["duties"] = duties
    path = tmp_path / "ayeops.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


def test_every_counts_days() -> None:
    assert duty(every="1d").days == 1
    assert duty(every="90d").days == 90
    assert duty(every="3660d").days == 3660


def test_the_due_day_is_last_done_plus_every() -> None:
    assert duty().due == date(2026, 12, 30)
    assert duty(last_done="2026-10-01").due == date(2026, 12, 30)
    assert duty(every="1d").due == date(2026, 10, 2)


def test_the_due_day_crosses_a_leap_day() -> None:
    assert duty(last_done=date(2028, 2, 28), every="1d").due == date(2028, 2, 29)
    assert duty(last_done=date(2028, 2, 29), every="365d").due == date(2029, 2, 28)
    assert duty(last_done=date(2027, 2, 28), every="366d").due == date(2028, 2, 29)


def test_a_due_day_of_a_bad_last_done_names_the_field() -> None:
    with pytest.raises(ValueError, match=r"^last_done is a day as YYYY-MM-DD, not '2027-02-30'$"):
        _ = duty(last_done="2027-02-30").due


def test_manifest_duties_read_every_field_in_name_order() -> None:
    manifest = manifest_with(
        {
            "token-rotation": spec(every="365d", last_done="2026-03-02"),
            "access-review": spec(owner="ops", runbook="access-review"),
        }
    )
    assert manifest_duties(manifest) == [
        Duty(
            "access-review", "Review the sign-ins", "90d", date(2026, 10, 1), "ops", "access-review"
        ),
        Duty("token-rotation", "Review the sign-ins", "365d", "2026-03-02", "", ""),
    ]


def test_a_manifest_without_duties_has_none() -> None:
    assert manifest_duties(manifest_with({})) == []


def test_every_is_at_most_ten_years() -> None:
    fine = manifest_with({"long": spec(every=f"{MAX_EVERY}d")})
    assert duty_problems(fine, TODAY) == []
    assert MAX_EVERY == 3660
    long = manifest_with({"long": spec(every="3661d")})
    assert duty_problems(long, TODAY) == [
        ("duties.long.every", "duty-every", "every is at most 3660d, ten years, not 3661d")
    ]


def test_last_done_may_be_today_and_not_tomorrow() -> None:
    assert duty_problems(manifest_with({"review": spec(last_done=TODAY)}), TODAY) == []
    tomorrow = manifest_with({"review": spec(last_done=TODAY + timedelta(days=1))})
    assert duty_problems(tomorrow, TODAY) == [
        (
            "duties.review.last_done",
            "duty-done",
            "last_done 2026-11-27 is after today, 2026-11-26: record a duty on the day it is done",
        )
    ]


def test_last_done_is_a_real_day_and_stops_the_other_checks_of_its_duty() -> None:
    bad = manifest_with({"review": spec(last_done="2027-02-30", every="3661d")})
    assert duty_problems(bad, TODAY) == [
        ("duties.review.every", "duty-every", "every is at most 3660d, ten years, not 3661d"),
        (
            "duties.review.last_done",
            "duty-done",
            "last_done is a day as YYYY-MM-DD, not '2027-02-30'",
        ),
    ]
    timed = manifest_with({"review": spec(last_done=datetime(2026, 10, 1, 9, tzinfo=UTC))})
    assert duty_problems(timed, TODAY) == [
        (
            "duties.review.last_done",
            "duty-done",
            "last_done is a day without a time, YYYY-MM-DD, not '2026-10-01T09:00:00+00:00'",
        )
    ]


def test_every_duty_is_checked() -> None:
    both = manifest_with(
        {"a": spec(last_done="2026-13-01"), "b": spec(last_done=TODAY + timedelta(days=2))}
    )
    assert [path for path, _, _ in duty_problems(both, TODAY)] == [
        "duties.a.last_done",
        "duties.b.last_done",
    ]


def test_a_zone_falls_back_to_utc() -> None:
    assert zone_of("Europe/Kyiv") == ZoneInfo("Europe/Kyiv")
    assert zone_of("Nowhere/Land") is UTC
    assert zone_of("../etc") is UTC


def test_a_duty_is_overdue_from_the_start_of_the_day_after_its_due_day() -> None:
    assert overdue_at(duty(), "UTC") == int(datetime(2026, 12, 31, tzinfo=UTC).timestamp())
    assert overdue_at(duty(), "UTC") == 1798675200
    assert overdue_at(duty(), "Europe/Kyiv") == 1798675200 - 2 * 3600
    assert overdue_at(duty(), "Nowhere/Land") == 1798675200


def test_the_overdue_moment_is_local_midnight_in_summer_time_too() -> None:
    summer = duty(last_done=date(2026, 6, 1), every="30d")
    assert overdue_at(summer, "Europe/Kyiv") == int(
        datetime(2026, 7, 2, tzinfo=UTC).timestamp() - 3 * 3600
    )


def test_one_second_before_the_overdue_moment_is_still_the_due_day() -> None:
    zone = ZoneInfo("Europe/Kyiv")
    at = overdue_at(duty(), "Europe/Kyiv")
    assert datetime.fromtimestamp(at - 1, zone).date() == date(2026, 12, 30)
    assert datetime.fromtimestamp(at, zone).date() == date(2026, 12, 31)
    assert datetime.fromtimestamp(at + 1, zone).date() == date(2026, 12, 31)


def test_an_overdue_moment_on_a_leap_day() -> None:
    leap = duty(last_done=date(2028, 2, 27), every="1d")
    assert overdue_at(leap, "UTC") == int(datetime(2028, 2, 29, tzinfo=UTC).timestamp())
    after = duty(last_done=date(2028, 2, 28), every="1d")
    assert overdue_at(after, "UTC") == int(datetime(2028, 3, 1, tzinfo=UTC).timestamp())


def test_labels_name_the_duty_and_its_day() -> None:
    assert duty_labels(duty(), BASE) == [
        ("duty", "access-review"),
        ("title", "Review the sign-ins"),
        ("due", "2026-12-30"),
        ("every", "90d"),
    ]


def test_labels_add_the_owner_and_the_runbook_link() -> None:
    assert duty_labels(duty(owner="ops", runbook="access-review"), BASE)[4:] == [
        ("owner", "ops"),
        ("runbook", "https://runbooks.example/access-review"),
    ]
    absolute = duty(runbook="https://wiki.example/review")
    assert duty_labels(absolute, BASE)[4:] == [("runbook", "https://wiki.example/review")]


def test_a_series_compares_the_time_with_the_overdue_moment() -> None:
    assert duty_series(duty(), "UTC", BASE) == (
        "label_replace(label_replace(label_replace(label_replace(vector(time() >= bool 1798675200),"
        ' "duty", "access-review", "", ""), "title", "Review the sign-ins", "", ""),'
        ' "due", "2026-12-30", "", ""), "every", "90d", "", "")'
    )


def test_no_duties_no_alert() -> None:
    assert overdue_alert(manifest_with({})) is None


def test_the_alert_unites_one_series_per_duty() -> None:
    manifest = manifest_with(
        {"b": spec(owner="ops"), "a": spec(every="1d")},
        timezone="Europe/Kyiv",
        runbook_base_url=BASE,
    )
    first, second = manifest_duties(manifest)
    alert = overdue_alert(manifest)
    assert alert == {
        "name": "duty-overdue",
        "title": "A recurring duty is overdue",
        "expr": duty_series(first, "Europe/Kyiv", BASE)
        + " or "
        + duty_series(second, "Europe/Kyiv", BASE),
        "for": "1h",
        "severity": "warning",
        "summary": SUMMARY,
        "runbook": RUNBOOK,
    }
    assert (ALERT, TITLE) == ("duty-overdue", "A recurring duty is overdue")


def test_the_alert_texts() -> None:
    assert RUNBOOK == "https://github.com/AyeOps/ayeops/blob/main/docs/runbooks/duty-overdue.md"
    assert SCOPE == "{{ $labels.duty }}"
    assert SUMMARY == (
        "{{ .Labels.title }} ({{ .Labels.duty }}, every {{ .Labels.every }}) was due on"
        " {{ .Labels.due }}{{ if .Labels.owner }}, owner {{ .Labels.owner }}{{ end }}"
    )


def test_the_runbook_link_is_the_duty_runbook_when_one_has_one() -> None:
    assert runbook_annotation(manifest_with({"a": spec()})) == RUNBOOK
    mixed = manifest_with({"a": spec(), "b": spec(runbook="review")})
    assert runbook_annotation(mixed) == OWN_RUNBOOK
    assert OWN_RUNBOOK == (
        "{{ if $labels.runbook }}{{ $labels.runbook }}{{ else }}"
        "https://github.com/AyeOps/ayeops/blob/main/docs/runbooks/duty-overdue.md{{ end }}"
    )


def test_days_are_counted_in_words() -> None:
    assert plural(0) == "0 days"
    assert plural(1) == "1 day"
    assert plural(2) == "2 days"


@pytest.mark.parametrize(
    ("today", "expected"),
    [
        (date(2026, 12, 28), "due on 2026-12-30, in 2 days"),
        (date(2026, 12, 29), "due on 2026-12-30, in 1 day"),
        (date(2026, 12, 30), "due today, 2026-12-30"),
        (date(2026, 12, 31), "overdue, was due on 2026-12-30, 1 day ago"),
        (date(2027, 1, 1), "overdue, was due on 2026-12-30, 2 days ago"),
    ],
)
def test_the_state_around_the_due_day(today: date, expected: str) -> None:
    assert state(duty(), today) == expected


def test_a_line_names_the_duty_its_state_every_owner_and_title() -> None:
    assert duty_line(duty(), TODAY) == (
        "access-review: due on 2026-12-30, in 34 days (every 90d): Review the sign-ins"
    )
    assert duty_line(duty(owner="ops"), TODAY) == (
        "access-review: due on 2026-12-30, in 34 days (every 90d, owner ops): Review the sign-ins"
    )


def test_listing_checks_the_pin_then_the_schema_then_the_duties() -> None:
    pinned = manifest_with({"a": spec(every="0d")}, ayeops="==0.0.1")
    assert [rule for _, rule, _ in listing_problems(pinned, TODAY)] == ["ayeops-version"]
    broken = manifest_with({"a": spec(every="0d")})
    assert listing_problems(broken, TODAY) == [
        ("duties.a.every", "schema", "'0d' does not match '^[1-9][0-9]{0,3}d$'")
    ]
    future = manifest_with({"a": spec(last_done=TODAY + timedelta(days=1))})
    assert [rule for _, rule, _ in listing_problems(future, TODAY)] == ["duty-done"]
    assert listing_problems(manifest_with({"a": spec()}), TODAY) == []


def test_run_lists_duties_by_due_day(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = write(
        tmp_path,
        {
            "late": {"title": "Late one", "every": "365d", "last_done": "2026-03-02"},
            "early": {"title": "Early one", "every": "90d", "last_done": "2026-10-01"},
            "same": {"title": "Same day", "every": "1d", "last_done": "2026-12-29"},
        },
    )
    assert run(path, date(2026, 12, 30)) == 0
    out, err = capsys.readouterr()
    assert out.splitlines() == [
        "early: due today, 2026-12-30 (every 90d): Early one",
        "same: due today, 2026-12-30 (every 1d): Same day",
        "late: due on 2027-03-02, in 62 days (every 365d): Late one",
        f"{path}: 3 duty(s), none overdue",
    ]
    assert err == ""


def test_run_fails_the_day_after_the_due_day(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = write(
        tmp_path,
        {
            "early": {"title": "Early one", "every": "90d", "last_done": "2026-10-01"},
            "late": {"title": "Late one", "every": "365d", "last_done": "2026-03-02"},
        },
    )
    assert run(path, date(2026, 12, 31)) == 1
    out, err = capsys.readouterr()
    assert out.splitlines() == [
        "early: overdue, was due on 2026-12-30, 1 day ago (every 90d): Early one",
        "late: due on 2027-03-02, in 61 days (every 365d): Late one",
    ]
    assert err == f"{path}: 1 of 2 duty(s) overdue\n"


def test_run_without_duties(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = write(tmp_path, {})
    assert run(path, TODAY) == 0
    assert capsys.readouterr().out == f"{path}: no duties\n"


def test_run_counts_from_today_by_default(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = write(tmp_path, {"old": {"title": "Old", "every": "1d", "last_done": "2000-01-01"}})
    assert run(path) == 1
    assert capsys.readouterr().err == f"{path}: 1 of 1 duty(s) overdue\n"


def test_run_refuses_a_manifest_with_errors(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = write(tmp_path, {"soon": {"title": "Soon", "every": "1d", "last_done": "2026-11-27"}})
    assert run(path, TODAY) == 1
    out, err = capsys.readouterr()
    assert out == ""
    assert err.splitlines() == [
        "error [duty-done] duties.soon.last_done: last_done 2026-11-27 is after today,"
        " 2026-11-26: record a duty on the day it is done",
        f"{path}: no duties listed, fix the errors first",
    ]


def test_the_cli_runs_duties_with_today(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = write(tmp_path, {"old": {"title": "Old", "every": "90d", "last_done": "2026-10-01"}})
    assert main(["duties", str(path), "--today", "2026-12-30"]) == 0
    assert main(["duties", str(path), "--today", "2026-12-31"]) == 1
    assert "overdue" in capsys.readouterr().out


def test_check_refuses_a_last_done_in_the_future(data: dict[str, Any]) -> None:
    future = (datetime.now(UTC) + timedelta(days=3)).date()
    with_duties(data, {"review": spec(last_done=future)})
    assert "duty-done" in rules_hit(data)


def test_check_refuses_an_every_over_ten_years(data: dict[str, Any]) -> None:
    with_duties(data, {"review": spec(every="9999d")})
    assert "duty-every" in rules_hit(data)


def test_the_duty_alert_name_is_taken_while_duties_exist(
    data: dict[str, Any],
) -> None:
    data["services"]["web"]["alerts"][0]["name"] = ALERT
    assert "alert-unique" in rules_hit(data)
    data.pop("duties")
    assert "alert-unique" not in rules_hit(data)


def test_the_example_renders_one_duty_rule(modules: dict[str, Module]) -> None:
    rules = yaml.safe_load(render_once(manifest_from(example_data()), modules)[RULES])
    groups = [group for group in rules["groups"] if group["name"] == "duties"]
    assert len(groups) == 1
    (rule,) = groups[0]["rules"]
    assert rule["uid"] == "aye-duty-overdue"
    assert rule["labels"] == {"severity": "warning", "scope": "{{ $$labels.duty }}"}
    assert rule["annotations"]["runbook_url"] == OWN_RUNBOOK.replace("$", "$$")
    assert (rule["noDataState"], rule["execErrState"], rule["for"]) == ("OK", "Error", "1h")
    expr = rule["data"][0]["model"]["expr"]
    assert '"runbook", "https://runbooks.acme.example/access-review"' in expr
    assert '"due", "2026-12-30"' in expr


def test_no_duties_render_no_duty_rule(modules: dict[str, Module]) -> None:
    data = example_data()
    data.pop("duties")
    rules = yaml.safe_load(render_once(manifest_from(data), modules)[RULES])
    assert "duties" not in [group["name"] for group in rules["groups"]]


def test_duties_without_runbooks_link_the_kit_runbook(modules: dict[str, Module]) -> None:
    data = example_data()
    for entry in data["duties"].values():
        entry.pop("runbook", None)
    rules = yaml.safe_load(render_once(manifest_from(data), modules)[RULES])
    (rule,) = next(group for group in rules["groups"] if group["name"] == "duties")["rules"]
    assert rule["annotations"]["runbook_url"] == RUNBOOK
