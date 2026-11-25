from __future__ import annotations

from typing import Any

import yaml

from ayeops.portal.graph import build_graph
from ayeops.portal.pages import collect_cards
from ayeops.registry import Module
from tests.conftest import manifest_from, render_once, rules_hit


def central(data: dict[str, Any]) -> dict[str, Any]:
    modules: dict[str, Any] = data["hosts"]["central"]["modules"]
    return modules


def auth(data: dict[str, Any]) -> dict[str, Any]:
    settings: dict[str, Any] = central(data)["auth"]
    return settings


def tree_of(data: dict[str, Any], modules: dict[str, Module]) -> dict[str, Any]:
    tree = render_once(manifest_from(data), modules)
    return {
        "compose": yaml.safe_load(tree["central/compose.yaml"]),
        "routes": yaml.safe_load(tree["central/ingress/dynamic/routes.yml"])["http"],
        "emails": tree.get("central/auth/emails.txt"),
        "env": tree["central/.env.example"],
    }


def oidc(data: dict[str, Any]) -> None:
    auth(data).update(
        provider="oidc",
        issuer_url="https://id.acme.example/realms/ops",
        endpoints={
            "authorize": "https://id.acme.example/auth",
            "token": "https://id.acme.example/token",
            "userinfo": "https://id.acme.example/userinfo",
        },
    )


def test_every_route_without_its_own_login_asks_oauth2_proxy(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    routers = tree_of(data, modules)["routes"]["routers"]
    assert routers["portal"]["middlewares"] == ["sso"]
    assert routers["metrics"]["middlewares"] == ["sso", "vmui"]


def test_grafana_and_the_callback_stay_outside_the_gate(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    routers = tree_of(data, modules)["routes"]["routers"]
    assert "middlewares" not in routers["grafana"]
    assert "middlewares" not in routers["auth"]
    assert routers["auth"]["rule"] == "Host(`auth.ops.acme.example`)"


def test_grafana_metrics_path_is_gated_because_grafana_serves_it_without_a_login(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    router = tree_of(data, modules)["routes"]["routers"]["grafana_metrics"]
    assert router["rule"] == "Host(`grafana.ops.acme.example`) && PathPrefix(`/metrics`)"
    assert router["middlewares"] == ["sso"]
    assert router["service"] == "grafana"


def test_the_callback_route_reaches_oauth2_proxy(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    services = tree_of(data, modules)["routes"]["services"]
    assert services["auth"]["loadBalancer"]["servers"] == [{"url": "http://oauth2-proxy:4180"}]


def test_forward_auth_asks_oauth2_proxy_and_ignores_client_forwarded_headers(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    middlewares = tree_of(data, modules)["routes"]["middlewares"]
    assert middlewares["sso"] == {
        "forwardAuth": {
            "address": "http://oauth2-proxy:4180/",
            "trustForwardHeader": False,
            "maxResponseBodySize": 1024 * 1024,
        }
    }
    assert middlewares["vmui"]["redirectRegex"]["replacement"] == "https://${1}/vmui/"


def test_open_routes_are_left_without_the_gate(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    auth(data)["open"] = ["portal"]
    routers = tree_of(data, modules)["routes"]["routers"]
    assert "middlewares" not in routers["portal"]
    assert routers["metrics"]["middlewares"] == ["sso", "vmui"]


def test_an_open_name_must_be_a_route(data: dict[str, Any]) -> None:
    auth(data)["open"] = ["dagster"]
    assert rules_hit(data) == {"auth-open"}


def test_without_auth_no_route_is_gated_and_vmui_still_redirects(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    del central(data)["auth"]
    central(data)["ingress"]["without_auth"] = "reachable over the VPN only"
    tree = tree_of(data, modules)
    assert set(tree["routes"]["routers"]) == {"grafana", "hub", "metrics", "oncall", "portal"}
    assert tree["routes"]["routers"]["metrics"]["middlewares"] == ["vmui"]
    assert set(tree["routes"]["middlewares"]) == {"vmui"}
    assert tree["emails"] is None
    assert "oauth2-proxy" not in tree["compose"]["services"]
    environment = tree["compose"]["services"]["grafana"]["environment"]
    assert not any(name.startswith("GF_AUTH_") for name in environment)


def test_emails_file_lists_one_address_per_line(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    assert tree_of(data, modules)["emails"] == "ops@acme.example\noncall@acme.example\n"


def test_oauth2_proxy_reads_its_secrets_from_the_environment_only(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    tree = tree_of(data, modules)
    environment = tree["compose"]["services"]["oauth2-proxy"]["environment"]
    for key, name in (
        ("OAUTH2_PROXY_CLIENT_ID", "AYE_AUTH_CLIENT_ID"),
        ("OAUTH2_PROXY_CLIENT_SECRET", "AYE_AUTH_CLIENT_SECRET"),
        ("OAUTH2_PROXY_COOKIE_SECRET", "AYE_AUTH_COOKIE_SECRET"),
    ):
        assert environment[key] == f"${{{name}:?set {name}}}"
        assert f"{name}=" in tree["env"]


def test_oauth2_proxy_keeps_its_cookie_on_the_ingress_domain(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    service = tree_of(data, modules)["compose"]["services"]["oauth2-proxy"]
    environment = service["environment"]
    assert environment["OAUTH2_PROXY_PROVIDER"] == "google"
    assert environment["OAUTH2_PROXY_COOKIE_DOMAINS"] == ".ops.acme.example"
    assert environment["OAUTH2_PROXY_WHITELIST_DOMAINS"] == ".ops.acme.example"
    assert (
        environment["OAUTH2_PROXY_REDIRECT_URL"] == "https://auth.ops.acme.example/oauth2/callback"
    )
    assert environment["OAUTH2_PROXY_UPSTREAMS"] == "static://202"
    assert environment["OAUTH2_PROXY_COOKIE_EXPIRE"] == "24h"
    assert environment["OAUTH2_PROXY_COOKIE_CSRF_PER_REQUEST"] == "true"
    assert environment["OAUTH2_PROXY_AUTHENTICATED_EMAILS_FILE"] == "/etc/oauth2-proxy/emails.txt"
    assert "OAUTH2_PROXY_EMAIL_DOMAINS" not in environment
    assert "./auth/emails.txt:/etc/oauth2-proxy/emails.txt:ro" in service["volumes"]
    assert "ports" not in service


def test_grafana_signs_in_with_the_same_client_and_refuses_unlisted_addresses(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    environment = tree_of(data, modules)["compose"]["services"]["grafana"]["environment"]
    assert environment["GF_AUTH_GOOGLE_ENABLED"] == "true"
    client_id = environment["GF_AUTH_GOOGLE_CLIENT_ID"]
    assert client_id == "${AYE_AUTH_CLIENT_ID:?set AYE_AUTH_CLIENT_ID}"
    assert environment["GF_AUTH_GOOGLE_ALLOW_SIGN_UP"] == "true"
    assert environment["GF_AUTH_GOOGLE_ROLE_ATTRIBUTE_STRICT"] == "true"
    assert environment["GF_AUTH_GOOGLE_SKIP_ORG_ROLE_SYNC"] == "false"
    assert environment["GF_AUTH_GOOGLE_ROLE_ATTRIBUTE_PATH"] == (
        "contains(['ops@acme.example'], email) && 'GrafanaAdmin'"
        " || contains(['ops@acme.example', 'oncall@acme.example'], email) && 'Viewer' || ''"
    )
    assert environment["GF_SECURITY_ADMIN_PASSWORD"].startswith("${AYE_GRAFANA_ADMIN_PASSWORD:?")


def test_admins_default_to_every_listed_address(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    del auth(data)["admins"]
    environment = tree_of(data, modules)["compose"]["services"]["grafana"]["environment"]
    assert environment["GF_AUTH_GOOGLE_ROLE_ATTRIBUTE_PATH"].startswith(
        "contains(['ops@acme.example', 'oncall@acme.example'], email) && 'GrafanaAdmin'"
    )


def test_grafana_root_url_follows_its_route_when_not_set(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    central(data)["grafana"] = {}
    environment = tree_of(data, modules)["compose"]["services"]["grafana"]["environment"]
    assert environment["GF_SERVER_ROOT_URL"] == "https://grafana.ops.acme.example"


def test_oidc_uses_the_issuer_and_grafana_generic_oauth(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    oidc(data)
    assert rules_hit(data) == set()
    services = tree_of(data, modules)["compose"]["services"]
    proxy = services["oauth2-proxy"]["environment"]
    assert proxy["OAUTH2_PROXY_PROVIDER"] == "oidc"
    assert proxy["OAUTH2_PROXY_OIDC_ISSUER_URL"] == "https://id.acme.example/realms/ops"
    grafana = services["grafana"]["environment"]
    assert not any(name.startswith("GF_AUTH_GOOGLE_") for name in grafana)
    assert grafana["GF_AUTH_GENERIC_OAUTH_ENABLED"] == "true"
    assert grafana["GF_AUTH_GENERIC_OAUTH_TOKEN_URL"] == "https://id.acme.example/token"
    assert grafana["GF_AUTH_GENERIC_OAUTH_ROLE_ATTRIBUTE_STRICT"] == "true"


def test_oidc_needs_an_issuer_url(data: dict[str, Any]) -> None:
    auth(data)["provider"] = "oidc"
    assert rules_hit(data) == {"module-settings"}


def test_oidc_with_grafana_needs_the_endpoints(data: dict[str, Any]) -> None:
    oidc(data)
    del auth(data)["endpoints"]
    assert rules_hit(data) == {"auth-endpoints"}


def test_only_google_and_oidc_are_providers(data: dict[str, Any]) -> None:
    auth(data)["provider"] = "github"
    assert rules_hit(data) == {"module-settings"}


def test_emails_are_required_and_not_empty(data: dict[str, Any]) -> None:
    del auth(data)["emails"]
    del auth(data)["admins"]
    assert rules_hit(data) == {"module-settings"}
    auth(data)["emails"] = []
    assert rules_hit(data) == {"module-settings"}


def test_emails_are_lowercase_addresses(data: dict[str, Any]) -> None:
    auth(data)["emails"] = ["Ops@acme.example"]
    assert rules_hit(data) == {"module-settings"}


def test_admins_must_be_listed_emails(data: dict[str, Any]) -> None:
    auth(data)["admins"] = ["boss@acme.example"]
    assert rules_hit(data) == {"auth-admins"}


def test_a_route_named_auth_clashes_with_the_callback(data: dict[str, Any]) -> None:
    central(data)["ingress"]["routes"]["auth"] = "portal:8080"
    assert rules_hit(data) == {"auth-route"}


def test_auth_needs_ingress_on_the_same_host(data: dict[str, Any]) -> None:
    data["hosts"]["edge"]["modules"]["auth"] = {"emails": ["ops@acme.example"]}
    assert rules_hit(data) == {"module-requires"}


def test_ingress_without_auth_is_rejected_unless_the_reason_is_given(
    data: dict[str, Any],
) -> None:
    del central(data)["auth"]
    assert rules_hit(data) == {"ingress-auth"}
    central(data)["ingress"]["without_auth"] = "reachable over the VPN only"
    assert rules_hit(data) == set()


def test_portal_cards_say_how_to_sign_in(data: dict[str, Any], modules: dict[str, Module]) -> None:
    manifest = manifest_from(data)
    cards = {
        card.name: card
        for card in collect_cards(manifest, modules, build_graph(manifest, modules))
        if card.host == "central"
    }
    assert cards["victoriametrics"].login.startswith("Google sign-in through oauth2-proxy")
    assert cards["portal"].login.startswith("Google sign-in through oauth2-proxy")
    assert cards["grafana"].login.startswith("Google sign-in for the auth emails")
    assert cards["node-exporter"].login == "none"
