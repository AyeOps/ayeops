from __future__ import annotations

from typing import Any

import pytest
import yaml

from ayeops.registry import Module, load_modules
from ayeops.render import SCOPE, escape_provisioning, held_back
from ayeops.status import push_components, status_document
from tests.conftest import check_once, manifest_from, render_once, rules_hit

RULE_SCOPE = escape_provisioning(SCOPE)
READY = "^220( |$)"
OK = "^250( |$)"


def held(expr: str) -> str:
    return str(held_back({"expr": expr}, True)["expr"])


def alert(**extra: Any) -> dict[str, Any]:
    return {"for": "5m", "severity": "critical", "runbook": "mail-down", **extra}


def smtp(address: str, **extra: Any) -> dict[str, Any]:
    return {"type": "smtp", "address": address, "alert": alert(), **extra}


def with_smtp(data: dict[str, Any], **checks: dict[str, Any]) -> dict[str, Any]:
    data["hosts"]["central"]["modules"]["checks"] = {}
    data["services"]["web"]["checks"] = checks or {
        "relay": smtp("mail.example.com:25"),
        "submission": smtp("mail.example.com:587"),
        "smtps": smtp("mail.example.com:465"),
    }
    return data


def checks_of(data: dict[str, Any]) -> dict[str, Any]:
    checks: dict[str, Any] = data["services"]["web"]["checks"]
    return checks


def blackbox(data: dict[str, Any], modules: dict[str, Module]) -> dict[str, Any]:
    tree = render_once(manifest_from(data), modules)
    found: dict[str, Any] = yaml.safe_load(tree["central/checks/blackbox.yml"])["modules"]
    return found


def rendered_rules(data: dict[str, Any], modules: dict[str, Module]) -> dict[str, Any]:
    tree = render_once(manifest_from(data), modules)
    groups = yaml.safe_load(tree["central/grafana/provisioning/alerting/rules.yaml"])["groups"]
    return {g["name"]: {rule["uid"]: rule for rule in g["rules"]} for g in groups}


def messages(data: dict[str, Any]) -> list[str]:
    findings = check_once(manifest_from(data), load_modules())
    return [finding.message for finding in findings if finding.rule == "check-smtp"]


def hello(name: str) -> list[dict[str, Any]]:
    return [{"send": f"EHLO {name}\r"}, {"expect": OK}]


def test_smtp_checks_on_25_587_and_465_pass_every_rule(data: dict[str, Any]) -> None:
    assert rules_hit(with_smtp(data)) == set()


def test_starttls_greets_upgrades_and_greets_again_with_the_domain_of_mail_from(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    module = blackbox(with_smtp(data), modules)["check_web_submission"]
    assert module == {
        "prober": "tcp",
        "timeout": "10s",
        "tcp": {
            "preferred_ip_protocol": "ip4",
            "query_response": [
                {"expect": READY},
                *hello("acme.example"),
                {"send": "STARTTLS\r"},
                {"expect": READY},
                {"starttls": True},
                *hello("acme.example"),
                {"send": "QUIT\r"},
            ],
        },
    }


def test_port_25_defaults_to_starttls_like_587(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    modules_found = blackbox(with_smtp(data), modules)
    assert modules_found["check_web_relay"] == modules_found["check_web_submission"]


def test_port_465_speaks_tls_from_the_first_byte(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    module = blackbox(with_smtp(data), modules)["check_web_smtps"]
    assert module["prober"] == "tcp"
    assert module["tcp"] == {
        "preferred_ip_protocol": "ip4",
        "tls": True,
        "query_response": [{"expect": READY}, *hello("acme.example"), {"send": "QUIT\r"}],
    }


def test_tls_none_greets_without_starttls_and_without_tls(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    with_smtp(data, relay=smtp("mail.example.com:25", tls="none"))
    assert rules_hit(data) == set()
    assert blackbox(data, modules)["check_web_relay"]["tcp"] == {
        "preferred_ip_protocol": "ip4",
        "query_response": [{"expect": READY}, *hello("acme.example"), {"send": "QUIT\r"}],
    }


def test_ehlo_overrides_the_domain_of_mail_from(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    with_smtp(data, relay=smtp("mail.example.com:25", tls="none", ehlo="probe.example.org"))
    steps = blackbox(data, modules)["check_web_relay"]["tcp"]["query_response"]
    assert steps[1] == {"send": "EHLO probe.example.org\r"}


def test_without_mail_an_smtp_check_needs_ehlo(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    del with_smtp(data)["mail"]
    assert rules_hit(data) == {"check-smtp"}
    missing = "the probe greets with EHLO <name>: set ehlo: <hostname>, or mail.from to take "
    assert messages(data) == [missing + "its domain"] * 3
    for check in checks_of(data).values():
        check["ehlo"] = "probe.example.org"
    assert rules_hit(data) == set()
    steps = blackbox(data, modules)["check_web_smtps"]["tcp"]["query_response"]
    assert steps[1] == {"send": "EHLO probe.example.org\r"}


@pytest.mark.parametrize("port", [25, 587])
def test_implicit_tls_on_a_plain_port_is_refused(data: dict[str, Any], port: int) -> None:
    with_smtp(data, relay=smtp(f"mail.example.com:{port}", tls="tls"))
    assert messages(data) == [
        f"port {port} greets in plain text and upgrades with STARTTLS, use tls: starttls "
        "or none; TLS from the first byte is port 465"
    ]


@pytest.mark.parametrize("tls", ["starttls", "none"])
def test_plain_text_on_465_is_refused(data: dict[str, Any], tls: str) -> None:
    with_smtp(data, smtps=smtp("mail.example.com:465", tls=tls))
    assert messages(data) == [
        "port 465 speaks TLS from the first byte, use tls: tls or leave tls out"
    ]


@pytest.mark.parametrize(("port", "tls"), [(2525, "tls"), (2525, "starttls"), (4650, "none")])
def test_other_ports_take_any_tls(data: dict[str, Any], port: int, tls: str) -> None:
    with_smtp(data, relay=smtp(f"mail.example.com:{port}", tls=tls))
    assert rules_hit(data) == set()


def test_other_ports_default_to_starttls_so_implicit_tls_is_explicit(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    with_smtp(data, relay=smtp("mail.example.com:2525"), smtps=smtp("mail.example.com:4650"))
    found = blackbox(data, modules)
    assert {"starttls": True} in found["check_web_relay"]["tcp"]["query_response"]
    assert "tls" not in found["check_web_smtps"]["tcp"]
    assert {"starttls": True} in found["check_web_smtps"]["tcp"]["query_response"]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("address", ":25"),
        ("address", "mail.example.com"),
        ("tls", "optional"),
        ("ehlo", "localhost"),
        ("ehlo", "[10.8.0.1]"),
        ("url", "smtp://mail.example.com"),
    ],
)
def test_malformed_smtp_fields_are_schema_errors(
    data: dict[str, Any], field: str, value: str
) -> None:
    checks_of(with_smtp(data))["relay"][field] = value
    assert rules_hit(data) == {"schema"}


def test_an_smtp_check_from_an_unknown_host_is_rejected(data: dict[str, Any]) -> None:
    checks_of(with_smtp(data))["relay"]["from"] = "nowhere"
    assert rules_hit(data) == {"check-host"}


def test_an_smtp_check_is_probed_at_its_address_and_alerts_on_probe_success(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    tree = render_once(manifest_from(with_smtp(data)), modules)
    jobs = {
        job["job_name"]: job
        for job in yaml.safe_load(tree["central/vmagent/scrape.yaml"])["scrape_configs"]
    }
    assert jobs["check-web-smtps"]["params"] == {
        "module": ["check_web_smtps"],
        "target": ["mail.example.com:465"],
    }
    rule = rendered_rules(data, modules)["services"]["aye-web-smtps"]
    assert rule["labels"] == {"severity": "critical", "scope": RULE_SCOPE, "service": "web"}
    assert rule["annotations"]["summary"] == "web: check smtps on mail.example.com:465 fails"
    assert rule["data"][0]["model"]["expr"] == held('probe_success{check="web-smtps"} == bool 0')


def test_smtp_checks_alone_give_a_service_its_metrics_alerts_and_status_pushes(
    data: dict[str, Any],
) -> None:
    web = with_smtp(data)["services"]["web"]
    del web["metrics"]
    del web["alerts"]
    del web["status"]["probe"]
    assert rules_hit(data) == set()
    manifest = manifest_from(data)
    website = [c for c in status_document(manifest)["components"] if c["title"] == "Website"]
    assert website == [{"id": "website", "title": "Website", "probes": [], "push": True}]
    assert {"id": "website", "services": ["web"]} in push_components(manifest)


def test_certificates_of_checks_expire_into_check_tls_expiring(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    rule = rendered_rules(with_smtp(data), modules)["checks"]["aye-check-tls-expiring"]
    assert rule["for"] == "1h"
    assert rule["labels"]["severity"] == "warning"
    assert rule["annotations"]["runbook_url"].endswith("/docs/runbooks/check-tls-expiring.md")
    assert rule["data"][0]["model"]["expr"] == held(
        '(probe_ssl_earliest_cert_expiry{job=~"check-.+"} - time()) / 86400 < bool 14'
    )
