from __future__ import annotations

from typing import Any

import yaml

from ayeops.manifest import load_manifest
from ayeops.registry import Module
from ayeops.render import host_compose
from tests.conftest import EXAMPLE, manifest_from, render_once, rules_hit


def ingress(data: dict[str, Any]) -> dict[str, Any]:
    settings: dict[str, Any] = data["hosts"]["central"]["modules"]["ingress"]
    return settings


def central_tree(modules: dict[str, Module]) -> dict[str, Any]:
    tree = render_once(load_manifest(EXAMPLE), modules)
    return {
        "compose": yaml.safe_load(tree["central/compose.yaml"]),
        "static": yaml.safe_load(tree["central/ingress/traefik.yml"]),
        "routes": yaml.safe_load(tree["central/ingress/dynamic/routes.yml"]),
        "env": tree["central/.env.example"],
    }


def test_traefik_binds_https_and_the_redirect_port_on_public_bind_only(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    traefik = host_compose(manifest_from(data), "central", modules)["services"]["traefik"]
    assert traefik["ports"] == ["10.8.0.1:443:443", "10.8.0.1:80:80"]
    assert not any(p.startswith(("0.0.0.0", ":")) for p in traefik["ports"])


def test_port_80_only_redirects_to_https(modules: dict[str, Module]) -> None:
    entry_points = central_tree(modules)["static"]["entryPoints"]
    assert entry_points["web"]["http"]["redirections"]["entryPoint"] == {
        "to": "websecure",
        "scheme": "https",
    }
    routers = central_tree(modules)["routes"]["http"]["routers"]
    assert all(router["entryPoints"] == ["websecure"] for router in routers.values())


def test_probes_inside_the_host_reach_every_route_name_through_traefik(
    modules: dict[str, Module],
) -> None:
    tree = central_tree(modules)
    aliases = tree["compose"]["services"]["traefik"]["networks"]["default"]["aliases"]
    names = {f"{name}.ops.acme.example" for name in tree["routes"]["http"]["routers"]}
    names = {name for name in names if "_" not in name}
    assert names
    assert set(aliases) == names


def test_every_route_becomes_a_host_rule_below_the_domain(modules: dict[str, Module]) -> None:
    http = central_tree(modules)["routes"]["http"]
    assert http["routers"]["grafana"]["rule"] == "Host(`grafana.ops.acme.example`)"
    assert http["routers"]["metrics"]["rule"] == "Host(`metrics.ops.acme.example`)"
    assert http["services"]["metrics"]["loadBalancer"]["servers"] == [
        {"url": "http://victoriametrics:8428"}
    ]


def test_routes_share_one_wildcard_certificate(modules: dict[str, Module]) -> None:
    routers = central_tree(modules)["routes"]["http"]["routers"]
    for router in routers.values():
        assert router["tls"] == {
            "certResolver": "letsencrypt",
            "domains": [{"main": "*.ops.acme.example"}],
        }


def test_certificate_comes_from_a_cloudflare_dns_challenge_stored_in_a_volume(
    modules: dict[str, Module],
) -> None:
    tree = central_tree(modules)
    acme = tree["static"]["certificatesResolvers"]["letsencrypt"]["acme"]
    assert acme["dnsChallenge"]["provider"] == "cloudflare"
    assert acme["storage"] == "/acme/acme.json"
    traefik = tree["compose"]["services"]["traefik"]
    assert "ingress-acme:/acme" in traefik["volumes"]
    assert "ingress-acme" in tree["compose"]["volumes"]
    assert traefik["environment"]["CF_DNS_API_TOKEN"].startswith("${AYE_CLOUDFLARE_DNS_API_TOKEN:?")
    assert "AYE_CLOUDFLARE_DNS_API_TOKEN=" in tree["env"]


def test_traefik_reads_no_docker_socket(modules: dict[str, Module]) -> None:
    traefik = central_tree(modules)["compose"]["services"]["traefik"]
    assert not any("docker.sock" in volume for volume in traefik["volumes"])
    assert set(central_tree(modules)["static"]["providers"]) == {"file"}


def test_email_reaches_acme_only_when_set(data: dict[str, Any], modules: dict[str, Module]) -> None:
    assert (
        "email"
        not in central_tree(modules)["static"]["certificatesResolvers"]["letsencrypt"]["acme"]
    )
    ingress(data)["email"] = "ops@acme.example"
    tree = render_once(manifest_from(data), modules)
    static = yaml.safe_load(tree["central/ingress/traefik.yml"])
    assert static["certificatesResolvers"]["letsencrypt"]["acme"]["email"] == "ops@acme.example"


def test_hosts_without_ingress_render_no_traefik(modules: dict[str, Module]) -> None:
    tree = render_once(load_manifest(EXAMPLE), modules)
    assert not any(path.startswith("edge/ingress/") for path in tree)
    assert "traefik" not in yaml.safe_load(tree["edge/compose.yaml"])["services"]


def test_route_to_a_service_not_rendered_on_the_host_is_rejected(data: dict[str, Any]) -> None:
    ingress(data)["routes"]["smart"] = "smartctl-exporter:9633"
    assert rules_hit(data) == {"ingress-route"}


def test_route_to_a_port_the_service_does_not_listen_on_is_rejected(
    data: dict[str, Any],
) -> None:
    ingress(data)["routes"]["grafana"] = "grafana:3001"
    assert rules_hit(data) == {"ingress-route"}


def test_ingress_host_needs_an_address(data: dict[str, Any]) -> None:
    del data["hosts"]["central"]["address"]
    data["hosts"]["edge"]["modules"]["metrics"]["remote_write"] = {
        "url": "http://10.8.0.1:8428/api/v1/write"
    }
    data["hosts"]["edge"]["modules"]["logs"]["push"] = {
        "url": "http://10.8.0.1:3100/loki/api/v1/push"
    }
    data["hosts"]["edge"]["modules"]["offsite"]["target"] = {
        "url": "s3:http://10.8.0.1:3900/acme-offsite/edge"
    }
    assert rules_hit(data) == {"ingress-host"}


def test_ingress_address_must_be_ipv4_because_records_are_a_records(
    data: dict[str, Any],
) -> None:
    data["hosts"]["central"]["address"] = "central.vpn.acme.example"
    assert rules_hit(data) == {"ingress-host"}


def test_ingress_host_needs_public_bind(data: dict[str, Any]) -> None:
    del data["hosts"]["central"]["public_bind"]
    data["hosts"]["central"]["modules"]["metrics"]["expose"] = []
    data["hosts"]["central"]["modules"]["ingress"]["expose"] = []
    data["hosts"]["central"]["modules"]["logs"]["expose"] = []
    data["hosts"]["central"]["modules"]["objectstore"]["expose"] = []
    del data["hosts"]["edge"]
    del data["services"]["acme-db"]
    assert rules_hit(data) == {"ingress-host"}


def test_ingress_needs_a_probe_on_a_route_so_the_certificate_is_watched(
    data: dict[str, Any],
) -> None:
    http = data["hosts"]["central"]["modules"]["probes"]["http"]
    http[:] = [url for url in http if not url.endswith(".ops.acme.example")]
    assert rules_hit(data) == {"ingress-probe"}


def test_the_same_name_cannot_be_routed_on_two_hosts(data: dict[str, Any]) -> None:
    edge = data["hosts"]["edge"]
    edge.update(address="10.8.0.2", public_bind="10.8.0.2")
    edge["modules"]["probes"] = {"http": ["https://grafana.ops.acme.example"]}
    edge["modules"]["ingress"] = {
        "domain": "ops.acme.example",
        "routes": {"grafana": "vmagent:8429"},
        "without_auth": "vmagent answers no person",
    }
    assert rules_hit(data) == {"ingress-unique"}


def test_domain_must_be_a_lowercase_hostname(data: dict[str, Any]) -> None:
    ingress(data)["domain"] = "https://Ops.acme.example"
    assert rules_hit(data) == {"module-settings"}


def test_ingress_needs_a_domain_and_routes(data: dict[str, Any]) -> None:
    data["hosts"]["central"]["modules"]["ingress"] = {"provider": "cloudflare"}
    assert rules_hit(data) == {"module-settings"}


def test_cloudflare_is_the_only_dns_provider(data: dict[str, Any]) -> None:
    ingress(data)["provider"] = "route53"
    assert rules_hit(data) == {"module-settings"}
