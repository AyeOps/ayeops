from __future__ import annotations

from typing import Any

import yaml

from ayeops.registry import Module
from ayeops.render import host_compose
from tests.conftest import manifest_from, render_once, rules_hit

URL = "https://secrets.ops.acme.example"
CONTACT_POINTS = "central/grafana/provisioning/alerting/contact-points.yaml"


def central(data: dict[str, Any]) -> dict[str, Any]:
    modules: dict[str, Any] = data["hosts"]["central"]["modules"]
    return modules


def env_of(data: dict[str, Any], service: str, modules: dict[str, Module]) -> dict[str, str]:
    compose = host_compose(manifest_from(data), "central", modules)
    environment: dict[str, str] = compose["services"][service]["environment"]
    return environment


def with_secrets(data: dict[str, Any]) -> dict[str, Any]:
    central(data)["secrets"] = {"url": URL}
    central(data)["backups"] = {}
    central(data)["ingress"]["routes"]["secrets"] = "infisical:8080"
    return data


def test_the_example_mail_block_passes_every_rule(data: dict[str, Any]) -> None:
    assert data["mail"]
    assert rules_hit(data) == set()


def test_grafana_sends_through_the_relay_with_credentials_from_the_environment(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    env = env_of(data, "grafana", modules)
    assert env["GF_SMTP_ENABLED"] == "true"
    assert env["GF_SMTP_HOST"] == "smtp.mail.example:587"
    assert env["GF_SMTP_USER"] == "${AYE_SMTP_USER:?set AYE_SMTP_USER}"
    assert env["GF_SMTP_PASSWORD"] == "${AYE_SMTP_PASSWORD:?set AYE_SMTP_PASSWORD}"
    assert env["GF_SMTP_FROM_ADDRESS"] == "ops@acme.example"
    assert env["GF_SMTP_FROM_NAME"] == "Acme Ops"
    assert env["GF_SMTP_STARTTLS_POLICY"] == "MandatoryStartTLS"


def test_from_name_defaults_to_the_project_and_port_to_587(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    data["mail"] = {"host": "relay.example", "from": "ops@acme.example"}
    env = env_of(data, "grafana", modules)
    assert env["GF_SMTP_HOST"] == "relay.example:587"
    assert env["GF_SMTP_FROM_NAME"] == "acme"


def test_implicit_tls_and_plain_relays_skip_starttls(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    data["mail"]["tls"] = "tls"
    data["mail"]["port"] = 465
    assert env_of(data, "grafana", modules)["GF_SMTP_STARTTLS_POLICY"] == "NoStartTLS"
    data["mail"]["tls"] = "none"
    data["mail"]["port"] = 25
    assert env_of(data, "grafana", modules)["GF_SMTP_STARTTLS_POLICY"] == "NoStartTLS"


def test_without_mail_nothing_smtp_is_rendered_or_asked_for(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    del data["mail"]
    with_secrets(data)
    assert rules_hit(data) == set()
    tree = render_once(manifest_from(data), modules)
    assert "SMTP" not in tree["central/compose.yaml"]
    assert "AYE_SMTP" not in tree["central/.env.example"]


def test_infisical_gets_the_same_relay(data: dict[str, Any], modules: dict[str, Module]) -> None:
    env = env_of(with_secrets(data), "infisical", modules)
    assert env["SMTP_HOST"] == "smtp.mail.example"
    assert env["SMTP_PORT"] == "587"
    assert env["SMTP_USERNAME"] == env_of(data, "grafana", modules)["GF_SMTP_USER"]
    assert env["SMTP_PASSWORD"] == env_of(data, "grafana", modules)["GF_SMTP_PASSWORD"]
    assert env["SMTP_FROM_ADDRESS"] == "ops@acme.example"
    assert env["SMTP_FROM_NAME"] == "Acme Ops"
    assert env["SMTP_REQUIRE_TLS"] == "true"
    assert env["SMTP_IGNORE_TLS"] == "false"


def test_infisical_tls_flags_follow_the_tls_setting(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    with_secrets(data)
    data["mail"].update(tls="none", port=25)
    env = env_of(data, "infisical", modules)
    assert (env["SMTP_REQUIRE_TLS"], env["SMTP_IGNORE_TLS"]) == ("false", "true")
    data["mail"].update(tls="tls", port=465)
    env = env_of(data, "infisical", modules)
    assert env["SMTP_PORT"] == "465"
    assert "SMTP_REQUIRE_TLS" not in env
    assert "SMTP_IGNORE_TLS" not in env


def test_from_must_be_on_a_domain(data: dict[str, Any]) -> None:
    data["mail"]["from"] = "ops@localhost"
    assert rules_hit(data) == {"mail-from"}
    data["mail"]["from"] = "ops@acme..example"
    assert rules_hit(data) == {"mail-from"}


def test_from_must_be_an_address(data: dict[str, Any]) -> None:
    data["mail"]["from"] = "acme.example"
    assert rules_hit(data) == {"schema"}


def test_tls_setting_and_port_must_agree(data: dict[str, Any]) -> None:
    data["mail"]["tls"] = "tls"
    assert rules_hit(data) == {"mail-tls"}
    data["mail"].update(tls="starttls", port=465)
    assert rules_hit(data) == {"mail-tls"}
    data["mail"]["tls"] = "tls"
    assert rules_hit(data) == set()


def test_email_contact_point_sends_to_the_listed_addresses(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    data["alerting"] = {"contact": "email", "emails": ["a@acme.example", "b@acme.example"]}
    assert rules_hit(data) == set()
    tree = render_once(manifest_from(data), modules)
    points = yaml.safe_load(tree[CONTACT_POINTS])
    receiver = points["contactPoints"][0]["receivers"][0]
    assert receiver["type"] == "email"
    assert receiver["settings"]["addresses"] == "a@acme.example;b@acme.example"
    assert points["policies"][0]["receiver"] == "email"
    assert "templates" not in points
    assert "AYE_TELEGRAM" not in tree["central/compose.yaml"]


def test_email_contact_needs_mail_and_addresses(data: dict[str, Any]) -> None:
    data["alerting"] = {"contact": "email"}
    assert rules_hit(data) == {"mail-contact"}
    del data["mail"]
    data["alerting"]["emails"] = ["a@acme.example"]
    assert rules_hit(data) == {"mail-contact"}


def test_emails_without_the_email_contact_are_flagged(data: dict[str, Any]) -> None:
    data["alerting"]["emails"] = ["a@acme.example"]
    assert rules_hit(data) == {"mail-contact"}
