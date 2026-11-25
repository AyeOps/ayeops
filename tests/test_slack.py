from __future__ import annotations

from typing import Any

import pytest
import yaml

from ayeops.permissions import credentials
from ayeops.registry import Module
from ayeops.render import SLACK_WEBHOOK_ENV, host_compose, slack_template, telegram_template
from tests.conftest import check_once, manifest_from, render_once, rules_hit

CONTACT_POINTS = "central/grafana/provisioning/alerting/contact-points.yaml"
WEBHOOK = "https://hooks.slack.com/services/T000/B000/abcdefgh"
REFERENCE = "${AYE_SLACK_WEBHOOK_URL:?set AYE_SLACK_WEBHOOK_URL}"
MESSAGE = (
    "a Slack webhook URL is a secret: put it into AYE_SLACK_WEBHOOK_URL in the .env of the "
    "host, not into the manifest"
)


def slack(data: dict[str, Any]) -> dict[str, Any]:
    data["alerting"] = {"contact": "slack"}
    return data


def points_of(data: dict[str, Any], modules: dict[str, Module]) -> dict[str, Any]:
    points: dict[str, Any] = yaml.safe_load(
        render_once(manifest_from(data), modules)[CONTACT_POINTS]
    )
    return points


def env_of(
    data: dict[str, Any], host: str, service: str, modules: dict[str, Module]
) -> dict[str, str]:
    compose = host_compose(manifest_from(data), host, modules)
    environment: dict[str, str] = compose["services"][service]["environment"]
    return environment


def configs(tree: dict[str, str]) -> list[str]:
    suffixes = ("compose.yaml", ".env.example", "contact-points.yaml")
    return [path for path in tree if path.endswith(suffixes)]


def test_the_example_with_contact_slack_passes_every_rule(data: dict[str, Any]) -> None:
    assert rules_hit(slack(data)) == set()


def test_the_slack_contact_point_reads_the_webhook_from_the_environment(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    points = points_of(slack(data), modules)
    first = points["contactPoints"][0]
    assert first["name"] == "slack"
    assert first["receivers"] == [
        {
            "uid": "aye-slack",
            "type": "slack",
            "settings": {
                "url": "${AYE_SLACK_WEBHOOK_URL}",
                "title": '{{ template "aye.slack.title" . }}',
                "text": '{{ template "aye.slack" . }}',
            },
        }
    ]
    assert points["templates"] == [{"orgId": 1, "name": "aye", "template": slack_template("en")}]
    assert SLACK_WEBHOOK_ENV == "AYE_SLACK_WEBHOOK_URL"


def test_critical_alerts_still_reach_slack_after_autopilot_and_oncall(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    points = points_of(slack(data), modules)
    assert [point["name"] for point in points["contactPoints"]] == ["slack", "autopilot", "oncall"]
    policy = points["policies"][0]
    assert policy["receiver"] == "slack"
    assert [route["receiver"] for route in policy["routes"]] == ["autopilot", "oncall", "slack"]
    assert policy["routes"][-1] == {"receiver": "slack"}


def test_grafana_and_the_sentinel_need_the_webhook_and_no_telegram_bot(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    slack(data)
    grafana = env_of(data, "central", "grafana", modules)
    sentinel = env_of(data, "edge", "sentinel", modules)
    for environment in (grafana, sentinel):
        assert environment[SLACK_WEBHOOK_ENV] == REFERENCE
        assert "AYE_TELEGRAM_BOT_TOKEN" not in environment
        assert "AYE_TELEGRAM_CHAT_ID" not in environment
    tree = render_once(manifest_from(data), modules)
    for host in ("central", "edge"):
        assert "AYE_SLACK_WEBHOOK_URL=replace-me" in tree[f"{host}/.env.example"].splitlines()
        assert "AYE_TELEGRAM_BOT_TOKEN" not in tree[f"{host}/.env.example"]
    assert not [path for path in configs(tree) if "hooks.slack" in tree[path]]


@pytest.mark.parametrize("contact", ["telegram", "email", "none"])
def test_other_contacts_render_no_slack(
    contact: str, data: dict[str, Any], modules: dict[str, Module]
) -> None:
    data["alerting"] = {"contact": contact}
    if contact == "email":
        data["alerting"]["emails"] = ["ops@acme.example"]
    tree = render_once(manifest_from(data), modules)
    assert not [path for path in configs(tree) if "slack" in tree[path].lower()]
    sentinel = env_of(data, "edge", "sentinel", modules)
    assert sentinel["AYE_TELEGRAM_BOT_TOKEN"] == (
        "${AYE_TELEGRAM_BOT_TOKEN:?set AYE_TELEGRAM_BOT_TOKEN}"
    )
    assert sentinel["AYE_TELEGRAM_CHAT_ID"] == "${AYE_TELEGRAM_CHAT_ID:?set AYE_TELEGRAM_CHAT_ID}"


def test_the_slack_message_reuses_the_telegram_layout_with_slack_links() -> None:
    template = slack_template("en")
    assert template.startswith(
        '{{ define "aye.escape" }}{{ . | reReplaceAll "&" "&amp;" '
        '| reReplaceAll "<" "&lt;" | reReplaceAll ">" "&gt;" }}{{ end }}'
    )
    assert (
        '{{ define "aye.links" }}{{ if .Annotations.runbook_url }}'
        " · <{{ .Annotations.runbook_url }}|runbook>{{ end }}"
        "{{ if .PanelURL }} · <{{ .PanelURL }}|dashboard>"
        "{{ else if .DashboardURL }} · <{{ .DashboardURL }}|dashboard>{{ end }}{{ end }}"
    ) in template
    assert (
        '{{ define "aye.slack.title" }}'
        "{{ if .Alerts.Firing }}🔴{{ else }}✅{{ end }} {{ .CommonLabels.alertname }}"
        "{{ if gt (len .Alerts) 1 }}{{ if .GroupLabels.scope }} · {{ .GroupLabels.scope }}"
        "{{ end }}{{ end }}{{ end }}"
    ) in template
    assert (
        "{{ if .Alerts.Firing }}🔴 {{ len .Alerts.Firing }} x "
        '{{ template "aye.escape" .CommonLabels.alertname }}'
        '{{ if .GroupLabels.scope }} · {{ template "aye.escape" .GroupLabels.scope }}{{ end }}'
        '{{ template "aye.links" index .Alerts.Firing 0 }}\n'
        "{{ if .CommonLabels.host }}{{ range .Alerts.Firing }}"
        '{{ template "aye.escape" .Annotations.summary }}\n{{ end }}'
        '{{ else }}{{ template "aye.hosts" .Alerts.Firing }}\n{{ end }}\n{{ end }}'
    ) in template
    assert "{{ if .Alerts.Resolved }}✅ {{ len .Alerts.Resolved }} x " in template
    assert template.endswith(
        "{{ else }}{{ range .Alerts }}"
        '{{ template "aye.escape" .Annotations.summary }}\n'
        '{{ .Labels.host }}{{ template "aye.links" . }}{{ end }}{{ end }}{{ end }}'
    )
    assert "<b>" not in template
    assert "<a " not in template
    assert "$" not in template
    opened = sum(template.count("{{ " + word + " ") for word in ("if", "range", "define"))
    assert opened == template.count("{{ end }}")


def test_the_slack_message_names_the_dashboard_in_the_grafana_language(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    assert "|дашборд>" in slack_template("uk")
    assert "|dashboard>" in slack_template("pl")
    slack(data)["hosts"]["central"]["modules"]["grafana"]["locale"] = "uk"
    assert points_of(data, modules)["templates"][0]["template"] == slack_template("uk")


def test_the_telegram_template_keeps_its_html() -> None:
    template = telegram_template("en")
    assert ' · <a href="{{ .Annotations.runbook_url }}">runbook</a>' in template
    assert "🔴 <b>{{ len .Alerts.Firing }} x {{ .CommonLabels.alertname }}</b>" in template
    assert "aye.escape" not in template
    assert "{{ if .GroupLabels.scope }} · {{ .GroupLabels.scope }}{{ end }}" in template


def test_the_permissions_name_the_slack_webhook_for_grafana_and_the_sentinel(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    manifest = manifest_from(slack(data))
    grafana = [c for c in credentials(manifest, modules, "central", "grafana") if c.env]
    sentinel = credentials(manifest, modules, "edge", "sentinel")
    assert [c.issuer for c in grafana if SLACK_WEBHOOK_ENV in c.env] == ["Slack"]
    assert not [c for c in grafana if "AYE_TELEGRAM_BOT_TOKEN" in c.env]
    assert (sentinel[0].issuer, sentinel[0].env) == ("Slack", (SLACK_WEBHOOK_ENV,))
    telegram = manifest_from(data | {"alerting": {"contact": "telegram"}})
    issuers = [c.issuer for c in credentials(telegram, modules, "edge", "sentinel")]
    assert "Telegram" in issuers
    assert "Slack" not in issuers


def test_a_slack_webhook_url_in_the_manifest_is_refused(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    data["services"]["web"]["url"] = WEBHOOK
    found = [f for f in check_once(manifest_from(data), modules) if f.rule == "slack-webhook"]
    assert [(f.path, f.message) for f in found] == [("services.web.url", MESSAGE)]


@pytest.mark.parametrize(
    "value",
    [
        WEBHOOK,
        "HTTPS://HOOKS.SLACK.COM/services/x",
        "https://hooks.slack-gov.com/services/T/B/x",
        "see hooks.slack.com/workflows/x",
    ],
)
def test_every_spelling_of_a_slack_webhook_is_refused_in_any_list_or_map(
    value: str, data: dict[str, Any], modules: dict[str, Module]
) -> None:
    data["services"]["web"]["cloud"][0]["limit"] = value
    assert rules_hit(data) == {"slack-webhook"}
    found = [f for f in check_once(manifest_from(data), modules) if f.rule == "slack-webhook"]
    assert [f.path for f in found] == ["services.web.cloud[0].limit"]


def test_other_slack_and_hook_urls_pass(data: dict[str, Any]) -> None:
    data["services"]["web"]["description"] = "https://acme.slack.com/archives/C1 hooks.example.com/"
    assert rules_hit(data) == set()
