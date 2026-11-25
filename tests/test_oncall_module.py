from __future__ import annotations

import json
import re
from typing import Any

import pytest
import yaml

from ayeops.permissions import credentials
from ayeops.registry import Module
from ayeops.render import host_compose
from tests.conftest import ROOT, manifest_from, render_once, rules_hit, tag_of

FILES = ROOT / "src" / "ayeops" / "modules" / "oncall" / "files"

ONCALL_SERVICES = {"goalert", "goalert-db", "oncall-bridge"}


def central(data: dict[str, Any]) -> dict[str, Any]:
    modules: dict[str, Any] = data["hosts"]["central"]["modules"]
    return modules


def services(data: dict[str, Any], modules: dict[str, Module]) -> dict[str, Any]:
    rendered: dict[str, Any] = host_compose(manifest_from(data), "central", modules)["services"]
    return rendered


def contact_points(data: dict[str, Any], modules: dict[str, Module]) -> dict[str, Any]:
    tree = render_once(manifest_from(data), modules)
    points: dict[str, Any] = yaml.safe_load(
        tree["central/grafana/provisioning/alerting/contact-points.yaml"]
    )
    return points


def test_the_example_with_oncall_passes_the_check(data: dict[str, Any]) -> None:
    assert "oncall" in central(data)
    assert rules_hit(data) == set()


def test_critical_and_page_alerts_go_to_oncall_and_still_reach_the_ops_channel(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    points = contact_points(data, modules)
    names = [c["name"] for c in points["contactPoints"]]
    assert names == ["telegram", "autopilot", "oncall"]
    oncall = points["contactPoints"][2]["receivers"][0]
    assert oncall["type"] == "webhook"
    assert oncall["settings"]["url"] == "http://oncall-bridge:8080/grafana"
    assert oncall["settings"]["authorization_scheme"] == "Bearer"
    assert oncall["settings"]["authorization_credentials"] == "${AYE_ONCALL_WEBHOOK_SECRET}"
    policy = points["policies"][0]
    assert policy["receiver"] == "telegram"
    first, second = oncall_routes(points)
    assert first == {
        "receiver": "oncall",
        "object_matchers": [["severity", "=~", "critical|page"]],
        "continue": True,
    }
    assert second == {"receiver": "telegram"}


def oncall_routes(points: dict[str, Any]) -> list[dict[str, Any]]:
    routes: list[dict[str, Any]] = points["policies"][0]["routes"]
    return [r for r in routes if r["receiver"] != "autopilot"]


def matches(matcher: list[str], severity: str) -> bool:
    label, op, pattern = matcher
    assert (label, op) == ("severity", "=~")
    return re.fullmatch(pattern, severity) is not None


@pytest.mark.parametrize(
    ("severity", "oncall"),
    [("critical", True), ("page", True), ("warning", False), ("info", False), ("", False)],
)
def test_only_critical_and_page_reach_oncall_and_every_alert_reaches_the_channel(
    data: dict[str, Any], modules: dict[str, Module], severity: str, oncall: bool
) -> None:
    first, second = oncall_routes(contact_points(data, modules))
    assert matches(first["object_matchers"][0], severity) is oncall
    reached = {first["receiver"]} if matches(first["object_matchers"][0], severity) else set()
    if not reached or first["continue"]:
        reached.add(second["receiver"])
    assert "telegram" in reached
    assert ("oncall" in reached) is oncall


def test_without_oncall_the_policy_has_no_routes_and_grafana_no_secret(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    del central(data)["oncall"]
    del central(data)["autopilot"]
    points = contact_points(data, modules)
    assert [c["name"] for c in points["contactPoints"]] == ["telegram"]
    assert "routes" not in points["policies"][0]
    grafana = services(data, modules)["grafana"]
    assert "AYE_ONCALL_WEBHOOK_SECRET" not in grafana["environment"]


def test_grafana_gets_the_webhook_secret_only_as_a_required_variable(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    grafana = services(data, modules)["grafana"]
    assert grafana["environment"]["AYE_ONCALL_WEBHOOK_SECRET"] == (
        "${AYE_ONCALL_WEBHOOK_SECRET:?set AYE_ONCALL_WEBHOOK_SECRET}"
    )


def test_no_oncall_service_publishes_a_port_or_joins_the_host_network(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    rendered = services(data, modules)
    assert set(rendered) >= ONCALL_SERVICES
    for name in ONCALL_SERVICES:
        assert "ports" not in rendered[name], name
        assert rendered[name].get("network_mode") is None, name


def test_images_are_pinned_and_secrets_are_required(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    rendered = services(data, modules)
    for name in ONCALL_SERVICES:
        image = tag_of(rendered[name]["image"])
        assert re.fullmatch(r"[a-z0-9./_-]+:v?[0-9]+\.[0-9]+[0-9A-Za-z.-]*", image), image
    text = json.dumps({n: rendered[n]["environment"] for n in ONCALL_SERVICES})
    for secret in (
        "AYE_ONCALL_DB_PASSWORD",
        "AYE_ONCALL_ENCRYPTION_KEY",
        "AYE_ONCALL_ADMIN_PASSWORD",
        "AYE_ONCALL_WEBHOOK_SECRET",
        "AYE_ONCALL_TELEGRAM_TOKEN",
    ):
        assert f"${{{secret}:?set {secret}}}" in text, secret


def test_the_bridge_uses_its_own_bot_token_not_the_alerting_one(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    environment = services(data, modules)["oncall-bridge"]["environment"]
    assert "AYE_ONCALL_TELEGRAM_TOKEN" in environment
    assert "AYE_TELEGRAM_BOT_TOKEN" not in json.dumps(environment)


def test_goalert_waits_for_its_database_and_prepares_itself_before_it_serves(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    rendered = services(data, modules)
    goalert = rendered["goalert"]
    assert goalert["entrypoint"] == ["sh", "/app/goalert_init.sh"]
    assert goalert["depends_on"] == {"goalert-db": {"condition": "service_healthy"}}
    script = (FILES / "goalert_init.sh").read_text("utf-8").splitlines()
    assert script.index("goalert migrate") < script.index("exec goalert")
    assert script[-1] == "exec goalert"
    assert "unset AYE_ONCALL_ADMIN_PASSWORD" in script
    assert "depends_on" not in rendered["oncall-bridge"]
    for name in ONCALL_SERVICES:
        assert rendered[name]["restart"] == "unless-stopped", name
        assert rendered[name]["healthcheck"]["test"], name


def test_the_helpers_run_unprivileged_and_read_only(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    rendered = services(data, modules)
    for name in ("oncall-bridge",):
        service = rendered[name]
        assert service["user"] == "65534:65534"
        assert service["read_only"] is True
        assert service["cap_drop"] == ["ALL"]
        assert "oncall-state:/tmp" in service["volumes"]


def test_quiet_hours_repeats_and_language_reach_the_bridge(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    central(data)["oncall"].update(repeat_minutes=3, max_repeats=4, language="uk")
    environment = services(data, modules)["oncall-bridge"]["environment"]
    assert environment["AYE_ONCALL_QUIET_START"] == "23:00"
    assert environment["AYE_ONCALL_QUIET_END"] == "08:00"
    assert environment["AYE_ONCALL_TIMEZONE"] == "Europe/Kyiv"
    assert (environment["AYE_ONCALL_REPEAT_MINUTES"], environment["AYE_ONCALL_MAX_REPEATS"]) == (
        "3",
        "4",
    )
    assert environment["AYE_ONCALL_LANGUAGE"] == "uk"
    assert environment["AYE_ONCALL_CHAT_ID"] == "100200300"
    central(data)["oncall"]["quiet_hours"] = None
    environment = services(data, modules)["oncall-bridge"]["environment"]
    assert not any(key.startswith("AYE_ONCALL_QUIET") for key in environment)
    assert environment["AYE_ONCALL_TIMEZONE"] == "Europe/Kyiv"


def test_the_bridge_takes_the_zone_of_quiet_hours_over_the_one_of_the_manifest(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    central(data)["oncall"]["quiet_hours"] = {"start": "22:00", "end": "07:00", "timezone": "UTC"}
    environment = services(data, modules)["oncall-bridge"]["environment"]
    assert environment["AYE_ONCALL_TIMEZONE"] == "UTC"
    central(data)["oncall"]["quiet_hours"] = None
    data["timezone"] = "America/New_York"
    environment = services(data, modules)["oncall-bridge"]["environment"]
    assert environment["AYE_ONCALL_TIMEZONE"] == "America/New_York"


def test_the_ops_group_and_topics_reach_the_bridge_only_when_topics_are_on(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    environment = services(data, modules)["oncall-bridge"]["environment"]
    assert "AYE_ONCALL_TOPICS" not in environment
    assert "AYE_ONCALL_OPS_CHAT_ID" not in environment
    central(data)["oncall"].update(ops_chat_id="-1001234567890")
    environment = services(data, modules)["oncall-bridge"]["environment"]
    assert "AYE_ONCALL_TOPICS" not in environment
    central(data)["oncall"]["topics"] = True
    assert rules_hit(data) == set()
    environment = services(data, modules)["oncall-bridge"]["environment"]
    assert environment["AYE_ONCALL_OPS_CHAT_ID"] == "-1001234567890"
    assert environment["AYE_ONCALL_TOPICS"] == "true"


def test_the_bot_needs_the_right_to_manage_topics_only_when_topics_are_on(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    def grants() -> tuple[str, ...]:
        found = credentials(manifest_from(data), modules, "central", "oncall")
        (bot,) = [c for c in found if c.issuer == "Telegram"]
        return bot.grants

    assert grants()[1] == "it needs no group and no admin rights"
    central(data)["oncall"].update(ops_chat_id="-1001234567890", topics=True)
    assert grants()[1] == (
        "administrator of the ops group -1001234567890 with the right to manage topics only"
    )


def test_the_topics_alert_watches_the_gauge_of_the_bridge(modules: dict[str, Module]) -> None:
    alerts = {a["name"]: a for a in modules["oncall"].spec["alerts"]}
    alert = alerts["oncall-topics-unavailable"]
    assert alert["expr"] == (
        "max by (host, project, group) (ayeops_oncall_topics_available) == bool 0"
    )
    assert alert["severity"] == "warning"
    assert (ROOT / "docs" / "runbooks" / "oncall-topics-unavailable.md").exists()


def test_escalation_steps_from_the_manifest_reach_the_bridge(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    steps = [
        {"delay_minutes": 3, "webhook": "http://autopilot:8080/diagnose"},
        {"notify": "person"},
    ]
    central(data)["oncall"]["escalation"] = steps
    environment = services(data, modules)["oncall-bridge"]["environment"]
    assert json.loads(environment["AYE_ONCALL_ESCALATION"]) == steps


def test_mail_settings_and_twilio_reach_the_bridge_only_when_wanted(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    environment = services(data, modules)["oncall-bridge"]["environment"]
    assert environment["AYE_ONCALL_EMAIL"] == "true"
    assert environment["AYE_ONCALL_SMTP_PASSWORD"] == "${AYE_SMTP_PASSWORD:?set AYE_SMTP_PASSWORD}"
    assert "AYE_ONCALL_TWILIO_ACCOUNT_SID" not in environment
    central(data)["oncall"].update(email=False, twilio=True)
    central(data)["oncall"]["person"]["phone"] = "+380501234567"
    environment = services(data, modules)["oncall-bridge"]["environment"]
    assert environment["AYE_ONCALL_EMAIL"] == "false"
    assert "AYE_ONCALL_SMTP_ADDRESS" not in environment
    assert environment["AYE_ONCALL_TWILIO_ACCOUNT_SID"] == (
        "${AYE_ONCALL_TWILIO_ACCOUNT_SID:?set AYE_ONCALL_TWILIO_ACCOUNT_SID}"
    )
    assert environment["AYE_ONCALL_PERSON_PHONE"] == "+380501234567"


def test_oncall_without_an_ops_channel_is_refused(data: dict[str, Any]) -> None:
    data["alerting"] = {"contact": "none"}
    assert "oncall-fallback" in rules_hit(data)


def test_twilio_without_a_phone_is_refused(data: dict[str, Any]) -> None:
    central(data)["oncall"]["twilio"] = True
    assert rules_hit(data) == {"oncall-phone"}


@pytest.mark.parametrize(
    "change",
    [
        {"url": "http://oncall.ops.acme.example"},
        {"person": {"username": "x", "name": "n", "email": "e@x", "telegram_chat_id": "1"}},
        {
            "person": {
                "username": "oncall",
                "name": "n",
                "email": "nope",
                "telegram_chat_id": "100200",
            }
        },
        {"quiet_hours": {"start": "24:00", "end": "08:00", "timezone": "UTC"}},
        {"escalation": []},
        {"escalation": [{"notify": "someone"}]},
        {"escalation": [{"webhook": "autopilot"}]},
        {"escalation": [{"notify": "person", "webhook": "http://a"}]},
        {"repeat_minutes": 0},
        {"language": "de"},
        {"topics": True},
        {"topics": True, "ops_chat_id": None},
        {"topics": "yes", "ops_chat_id": "-1001234567890"},
        {"ops_chat_id": "100200300"},
        {"ops_chat_id": "-1001234"},
    ],
)
def test_settings_outside_the_schema_are_refused(
    data: dict[str, Any], change: dict[str, Any]
) -> None:
    central(data)["oncall"].update(change)
    assert rules_hit(data) == {"module-settings"}


def test_page_is_a_severity_alerts_may_use(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    service = next(s for s in data["services"].values() if s.get("alerts"))
    service["alerts"][0]["severity"] = "page"
    assert rules_hit(data) == set()
    tree = render_once(manifest_from(data), modules)
    rules = yaml.safe_load(tree["central/grafana/provisioning/alerting/rules.yaml"])
    labels = [r["labels"] for g in rules["groups"] for r in g["rules"]]
    assert {"severity": "page"}.items() <= next(
        label for label in labels if label.get("severity") == "page"
    ).items()
    service["alerts"][0]["severity"] = "urgent"
    assert "schema" in rules_hit(data)


@pytest.mark.parametrize(
    ("tls", "disable"), [("starttls", "true"), ("none", "true"), ("tls", "false")]
)
def test_goalert_dials_plain_and_upgrades_unless_the_relay_wants_tls_from_the_first_byte(
    data: dict[str, Any], modules: dict[str, Module], tls: str, disable: str
) -> None:
    data["mail"]["tls"] = tls
    environment = services(data, modules)["oncall-bridge"]["environment"]
    assert environment["AYE_ONCALL_SMTP_DISABLE_TLS"] == disable
