from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import yaml

from ayeops import autopilot, render
from ayeops.registry import Module
from ayeops.render import host_compose
from tests.conftest import manifest_from, render_once, rules_hit, tag_of


def central(data: dict[str, Any]) -> dict[str, Any]:
    modules: dict[str, Any] = data["hosts"]["central"]["modules"]
    return modules


def service(data: dict[str, Any], modules: dict[str, Module]) -> dict[str, Any]:
    rendered: dict[str, Any] = host_compose(manifest_from(data), "central", modules)["services"]
    agent: dict[str, Any] = rendered["autopilot"]
    return agent


def tree(data: dict[str, Any], modules: dict[str, Module]) -> dict[str, str]:
    return render_once(manifest_from(data), modules)


def contact_points(data: dict[str, Any], modules: dict[str, Module]) -> dict[str, Any]:
    points: dict[str, Any] = yaml.safe_load(
        tree(data, modules)["central/grafana/provisioning/alerting/contact-points.yaml"]
    )
    return points


def test_the_example_with_the_autopilot_passes_the_check(data: dict[str, Any]) -> None:
    assert "autopilot" in central(data)
    assert rules_hit(data) == set()


def test_critical_alerts_reach_the_autopilot_first_without_waiting(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    points = contact_points(data, modules)
    receiver = next(c for c in points["contactPoints"] if c["name"] == "autopilot")
    settings = receiver["receivers"][0]["settings"]
    assert settings["url"] == "http://autopilot:8080/grafana"
    assert settings["authorization_credentials"] == "${AYE_AUTOPILOT_WEBHOOK_SECRET}"
    routes = points["policies"][0]["routes"]
    assert [r["receiver"] for r in routes] == ["autopilot", "oncall", "telegram"]
    assert routes[0] == {
        "receiver": "autopilot",
        "object_matchers": [["severity", "=~", "critical|page"]],
        "continue": True,
        "group_wait": "0s",
    }


def test_the_autopilot_works_without_oncall(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    del central(data)["oncall"]
    routes = contact_points(data, modules)["policies"][0]["routes"]
    assert [r["receiver"] for r in routes] == ["autopilot", "telegram"]


def test_without_the_module_grafana_gets_neither_route_nor_secret(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    del central(data)["autopilot"]
    rendered = tree(data, modules)
    points = yaml.safe_load(rendered["central/grafana/provisioning/alerting/contact-points.yaml"])
    assert "autopilot" not in [c["name"] for c in points["contactPoints"]]
    assert "central/autopilot/catalog.json" not in rendered
    assert "AYE_AUTOPILOT" not in rendered["central/compose.yaml"]


def test_grafana_gets_the_webhook_secret_only_as_a_required_variable(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    rendered = host_compose(manifest_from(data), "central", modules)["services"]
    assert rendered["grafana"]["environment"]["AYE_AUTOPILOT_WEBHOOK_SECRET"] == (
        "${AYE_AUTOPILOT_WEBHOOK_SECRET:?set AYE_AUTOPILOT_WEBHOOK_SECRET}"
    )


def test_the_agent_is_locked_down_pinned_and_publishes_no_port(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    agent = service(data, modules)
    assert re.fullmatch(r"python:3\.13\.\d+-alpine3\.\d+", tag_of(agent["image"]))
    assert agent["user"] == "65534:65534"
    assert agent["read_only"] is True
    assert agent["cap_drop"] == ["ALL"]
    assert "ports" not in agent
    assert agent.get("network_mode") is None
    assert "autopilot-state:/tmp" in agent["volumes"]
    assert "./autopilot/catalog.json:/app/catalog.json:ro" in agent["volumes"]
    environment = agent["environment"]
    assert environment["AYE_AUTOPILOT_API_KEY"] == (
        "${AYE_AUTOPILOT_API_KEY:?set AYE_AUTOPILOT_API_KEY}"
    )
    assert environment["AYE_AUTOPILOT_POST_VERDICT"] == "false"
    assert "AYE_ONCALL_TELEGRAM_TOKEN" not in environment
    assert environment["AYE_AUTOPILOT_LOKI_URL"] == "http://loki:3100"
    assert json.loads(environment["AYE_AUTOPILOT_CRITICAL_SERVICES"]) == ["web"]


def test_claude_code_runs_on_a_glibc_image_with_the_host_binary_read_only(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    central(data)["autopilot"].update(
        {"runner": "claude-code", "auth": "subscription", "claude_binary": "/opt/claude/claude"}
    )
    assert rules_hit(data) == set()
    agent = service(data, modules)
    assert re.fullmatch(r"python:3\.13\.\d+-slim", tag_of(agent["image"]))
    assert "/opt/claude/claude:/usr/local/bin/claude:ro" in agent["volumes"]
    environment = agent["environment"]
    assert "AYE_AUTOPILOT_API_KEY" not in environment
    assert environment["AYE_AUTOPILOT_OAUTH_TOKEN"] == (
        "${AYE_AUTOPILOT_OAUTH_TOKEN:?set AYE_AUTOPILOT_OAUTH_TOKEN}"
    )


def test_post_verdict_uses_the_oncall_bot_and_chat(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    central(data)["autopilot"]["post_verdict"] = True
    environment = service(data, modules)["environment"]
    assert environment["AYE_AUTOPILOT_POST_VERDICT"] == "true"
    assert environment["AYE_AUTOPILOT_CHAT_ID"] == "100200300"
    assert environment["AYE_ONCALL_TELEGRAM_TOKEN"] == (
        "${AYE_ONCALL_TELEGRAM_TOKEN:?set AYE_ONCALL_TELEGRAM_TOKEN}"
    )


def test_the_incident_journal_is_mounted_read_only_when_incidents_runs_on_the_host(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    agent = service(data, modules)
    assert "incidents-journal:/incidents:ro" in agent["volumes"]
    assert agent["environment"]["AYE_AUTOPILOT_INCIDENTS"] == "/incidents/incidents.jsonl"
    compose = yaml.safe_load(tree(data, modules)["central/compose.yaml"])
    assert "incidents-journal" in compose["volumes"]


def test_without_incidents_the_autopilot_reads_no_incident_journal(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    del central(data)["incidents"]
    agent = service(data, modules)
    assert not any("incidents" in volume for volume in agent["volumes"])
    assert "AYE_AUTOPILOT_INCIDENTS" not in agent["environment"]


def test_post_verdict_needs_oncall(data: dict[str, Any]) -> None:
    del central(data)["oncall"]
    central(data)["autopilot"]["post_verdict"] = True
    assert "autopilot-verdict" in rules_hit(data)


def test_claude_code_needs_its_binary_and_a_subscription_needs_claude_code(
    data: dict[str, Any],
) -> None:
    central(data)["autopilot"]["runner"] = "claude-code"
    assert "autopilot-runner" in rules_hit(data)
    central(data)["autopilot"].update({"runner": "api", "auth": "subscription"})
    assert "autopilot-runner" in rules_hit(data)


def test_a_cap_below_one_run_is_refused(data: dict[str, Any]) -> None:
    central(data)["autopilot"].update({"run_usd": 1, "night_usd": 0.5})
    assert "autopilot-caps" in rules_hit(data)
    central(data)["autopilot"]["quiet_hours"] = None
    assert "autopilot-caps" not in rules_hit(data)


def test_the_autopilot_needs_a_default_contact(data: dict[str, Any]) -> None:
    del central(data)["oncall"]
    data["alerting"]["contact"] = "none"
    assert "autopilot-contact" in rules_hit(data)


def test_the_autopilot_runs_next_to_grafana(data: dict[str, Any]) -> None:
    data["hosts"]["edge"]["modules"]["autopilot"] = {}
    assert "module-requires" in rules_hit(data)


def test_the_catalog_lists_the_triggering_rules_and_the_services(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    catalog = json.loads(tree(data, modules)["central/autopilot/catalog.json"])
    severities = {rule["severity"] for rule in catalog["rules"].values()}
    assert severities <= {"critical", "page"}
    web = catalog["rules"]["aye-web-down"]
    assert web["service"] == "web"
    assert web["title"] == "Website down"
    assert web["expr"] == 'probe_success{instance="https://acme.example"} == bool 0'
    assert "aye-container-restarting" not in catalog["rules"]
    assert catalog["services"]["web"] == {
        "host": "central",
        "container": "acme-apps[-_]web[-_][0-9]+",
        "depends_on": ["cdn"],
        "description": "Public website of acme, served by nginx behind the CDN.",
    }


def test_a_dollar_in_a_rule_reaches_the_catalog_as_written(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    data["services"]["web"]["alerts"][0]["title"] = "Web $down"
    catalog = json.loads(tree(data, modules)["central/autopilot/catalog.json"])
    assert catalog["rules"]["aye-web-down"]["title"] == "Web $down"


def test_the_catalog_reads_the_rule_inside_the_host_down_hold() -> None:
    inner = 'probe_success{instance="https://acme.example"} == bool 0'
    held = render.held_back({"expr": inner}, True)["expr"]
    assert held != inner
    assert autopilot.unheld(held) == inner
    assert autopilot.unheld(inner) == inner


def test_the_catalog_names_the_secret_variables_of_the_fleet(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    names = json.loads(tree(data, modules)["central/autopilot/catalog.json"])["secret_names"]
    assert names == sorted(names)
    for name in (
        "AYE_AUTOPILOT_API_KEY",
        "AYE_AUTOPILOT_WEBHOOK_SECRET",
        "AYE_GRAFANA_PG_PASSWORD",
        "AYE_SENTINEL_PING_URL",
        "AYE_OFFSITE_RESTIC_PASSWORD",
    ):
        assert name in names, name
    assert all(re.fullmatch(r"[A-Z][A-Z0-9_]*[A-Z0-9]", name) for name in names)


def test_a_variable_named_in_module_settings_joins_the_secret_names(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    sources = central(data)["grafana"]["datasources"]
    sources[0]["password_env"] = "AYE_SHOP_DB_PASSWORD"
    names = json.loads(tree(data, modules)["central/autopilot/catalog.json"])["secret_names"]
    assert "AYE_SHOP_DB_PASSWORD" in names


def test_extra_masks_reach_the_agent_as_json_with_dollars_escaped(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    masks = [r"ACME-[0-9]{6}$", r"customer=(\w+)"]
    central(data)["autopilot"]["mask"] = masks
    assert rules_hit(data) == set()
    value = service(data, modules)["environment"]["AYE_AUTOPILOT_MASK"]
    assert "$$" in value
    assert json.loads(value.replace("$$", "$")) == masks
    compose = tree(data, modules)["central/compose.yaml"]
    assert "AYE_AUTOPILOT_MASK" in compose


def test_without_extra_masks_the_agent_gets_no_mask_variables(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    environment = service(data, modules)["environment"]
    assert "AYE_AUTOPILOT_MASK" not in environment
    assert "AYE_AUTOPILOT_MASK_ENV" not in environment


def test_mask_env_hands_the_agent_those_values_to_mask(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    central(data)["autopilot"]["mask_env"] = ["AYE_GRAFANA_PG_PASSWORD"]
    assert rules_hit(data) == set()
    environment = service(data, modules)["environment"]
    assert json.loads(environment["AYE_AUTOPILOT_MASK_ENV"]) == ["AYE_GRAFANA_PG_PASSWORD"]
    assert environment["AYE_GRAFANA_PG_PASSWORD"] == (
        "${AYE_GRAFANA_PG_PASSWORD:?set AYE_GRAFANA_PG_PASSWORD}"
    )


def test_a_mask_that_does_not_compile_or_matches_nothing_is_refused(data: dict[str, Any]) -> None:
    central(data)["autopilot"]["mask"] = ["(unclosed"]
    assert "autopilot-mask" in rules_hit(data)
    central(data)["autopilot"]["mask"] = ["x*"]
    assert "autopilot-mask" in rules_hit(data)
    central(data)["autopilot"]["mask"] = ["x+"]
    assert "autopilot-mask" not in rules_hit(data)


def test_mask_env_refuses_the_agents_own_variables_and_lowercase_names(
    data: dict[str, Any],
) -> None:
    central(data)["autopilot"]["mask_env"] = ["AYE_AUTOPILOT_API_KEY"]
    assert "autopilot-mask" in rules_hit(data)
    central(data)["autopilot"]["mask_env"] = ["AYE_ONCALL_TELEGRAM_TOKEN"]
    assert "autopilot-mask" in rules_hit(data)
    central(data)["autopilot"]["mask_env"] = ["db_password"]
    assert "module-settings" in rules_hit(data)


def test_runner_none_needs_no_model_key(data: dict[str, Any], modules: dict[str, Module]) -> None:
    central(data)["autopilot"]["runner"] = "none"
    assert rules_hit(data) == set()
    environment = service(data, modules)["environment"]
    assert environment["AYE_AUTOPILOT_RUNNER"] == "none"
    assert "AYE_AUTOPILOT_API_KEY" not in environment
    assert "AYE_AUTOPILOT_OAUTH_TOKEN" not in environment
    assert "AYE_AUTOPILOT_API_KEY" not in tree(data, modules)["central/.env.example"]


def test_secret_names_come_from_templates_declarations_and_env_settings(
    data: dict[str, Any], modules: dict[str, Module], tmp_path: Path
) -> None:
    (tmp_path / "compose.yaml.j2").write_text(
        'services:\n  web:\n    environment:\n      A: "${AYE_ONLY_IN_COMPOSE:?set it}"\n',
        "utf-8",
    )
    stand_in = Module("grafana", tmp_path, {"permissions": [{"env": ["AYE_ONLY_DECLARED"]}]})
    names = autopilot.secret_names(manifest_from(data), {**modules, "grafana": stand_in})
    assert "AYE_ONLY_IN_COMPOSE" in names
    assert "AYE_ONLY_DECLARED" in names
    assert "AYE_GRAFANA_PG_PASSWORD" in names
    assert "AYE_SMTP_PASSWORD" in names
