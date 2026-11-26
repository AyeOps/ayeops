from __future__ import annotations

from typing import Any

from ayeops.incidents import night_hours
from ayeops.registry import Module
from ayeops.render import host_compose, host_settings
from tests.conftest import manifest_from, rules_hit


def edge(data: dict[str, Any]) -> dict[str, Any]:
    modules: dict[str, Any] = data["hosts"]["edge"]["modules"]
    return modules


def test_the_example_sets_one_zone_for_the_fleet(data: dict[str, Any]) -> None:
    assert data["timezone"] == "Europe/Kyiv"
    assert manifest_from(data).timezone == "Europe/Kyiv"
    del data["timezone"]
    assert manifest_from(data).timezone == "UTC"


def test_modules_without_their_own_zone_take_the_manifest_zone(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    for name in ("offsite", "image-updates", "updates"):
        del edge(data)[name]["timezone"]
    settings = host_settings(manifest_from(data), "edge", modules)
    assert {settings[name]["timezone"] for name in ("offsite", "image-updates", "updates")} == {
        "Europe/Kyiv"
    }


def test_a_module_zone_wins_over_the_manifest_zone(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    settings = host_settings(manifest_from(data), "edge", modules)
    assert settings["offsite"]["timezone"] == "Europe/Berlin"
    services = host_compose(manifest_from(data), "edge", modules)["services"]
    assert services["offsite"]["environment"]["TZ"] == "Europe/Berlin"


def test_quiet_hours_without_a_zone_take_the_manifest_zone(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    oncall = data["hosts"]["central"]["modules"]["oncall"]
    assert "timezone" not in oncall["quiet_hours"]
    settings = host_settings(manifest_from(data), "central", modules)
    assert settings["oncall"]["quiet_hours"]["timezone"] == "Europe/Kyiv"
    assert "timezone" not in oncall["quiet_hours"]
    oncall["quiet_hours"]["timezone"] = "Asia/Tokyo"
    settings = host_settings(manifest_from(data), "central", modules)
    assert settings["oncall"]["quiet_hours"]["timezone"] == "Asia/Tokyo"


def test_quiet_hours_off_stay_off(data: dict[str, Any], modules: dict[str, Module]) -> None:
    data["hosts"]["central"]["modules"]["oncall"]["quiet_hours"] = None
    settings = host_settings(manifest_from(data), "central", modules)
    assert settings["oncall"]["quiet_hours"] is None


def test_night_without_oncall_is_23_to_8_in_the_manifest_zone(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    del data["hosts"]["central"]["modules"]["oncall"]
    data["timezone"] = "America/New_York"
    services = host_compose(manifest_from(data), "central", modules)["services"]
    env = services["incidents-agent"]["environment"]
    assert env["AYE_INCIDENTS_NIGHT_START"] == "23:00"
    assert env["AYE_INCIDENTS_TIMEZONE"] == "America/New_York"
    assert night_hours({"night": {"start": "01:00", "end": "05:00"}}, {}, "Asia/Tokyo") == {
        "start": "01:00",
        "end": "05:00",
        "timezone": "Asia/Tokyo",
    }


def test_an_unknown_zone_fails_the_timezone_rule(data: dict[str, Any]) -> None:
    assert "timezone" not in rules_hit(data)
    data["timezone"] = "Europe/Atlantis"
    assert "timezone" in rules_hit(data)


def test_an_unknown_module_zone_fails_the_timezone_rule(data: dict[str, Any]) -> None:
    edge(data)["updates"]["timezone"] = "Mars/Olympus"
    assert "timezone" in rules_hit(data)
    edge(data)["updates"]["timezone"] = "Europe/Berlin"
    data["hosts"]["central"]["modules"]["oncall"]["quiet_hours"]["timezone"] = "Europe"
    assert "timezone" in rules_hit(data)


def test_a_zone_that_is_no_name_is_a_schema_error(data: dict[str, Any]) -> None:
    data["timezone"] = "../etc/passwd"
    assert "schema" in rules_hit(data)
