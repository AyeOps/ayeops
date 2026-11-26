from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from ayeops.links import (
    BY,
    JOB,
    LINK_DOWN,
    PROBE,
    RULE,
    UNREACHABLE,
    every_peer,
    link_down_expr,
    link_peers,
    link_problems,
    link_zone,
    linked,
    links_context,
    peer_problem,
    reachable,
)
from ayeops.manifest import Manifest
from ayeops.registry import MODULES_DIR

SUN = "(vector(time() - 342000) % 604800 < 10800)"
DAILY = "(vector(time() - 9000) % 86400 < 3600)"
PATH = "hosts.hub.modules.probes.links"


def fleet(**hosts: dict[str, Any]) -> Manifest:
    return Manifest({"version": 1, "project": "acme", "hosts": hosts}, Path())


def host(**fields: Any) -> dict[str, Any]:
    return {"arch": "amd64", "modules": {}, **fields}


def hub(links: Any) -> dict[str, Any]:
    return host(address="10.8.0.1", modules={"probes": {"links": links}})


def test_the_probe_and_the_hold_keep_their_rendered_form() -> None:
    assert JOB == "blackbox-link"
    assert PROBE == 'probe_success{job="blackbox-link"}'
    assert LINK_DOWN == "link-down"
    assert RULE == "probe-links"
    assert BY == "host, project, group, zone"
    assert UNREACHABLE == (
        'label_replace(max by (peer) (probe_success{job="blackbox-link"} unless on (zone)'
        ' (max by (zone) (probe_success{job="blackbox-link"}) > 0)), "host", "$1", "peer", "(.+)")'
    )


def test_a_peer_is_in_its_zone_or_a_zone_of_its_own() -> None:
    manifest = fleet(a=host(zone="office"), b=host(), c=host(zone=""))
    assert link_zone(manifest, "a") == "office"
    assert link_zone(manifest, "b") == "b"
    assert link_zone(manifest, "c") == "c"


def test_every_peer_is_each_other_host_with_an_address_that_stays_by_name() -> None:
    manifest = fleet(
        z=host(address="10.8.0.9"),
        hub=hub(True),
        y=host(),
        x=host(address="10.8.0.8", volatile=True),
        w=host(address="10.8.0.7", volatile="3d"),
        v=host(address="10.8.0.6", volatile=False),
        u=host(address=""),
        t=host(address="10.8.0.5"),
    )
    assert every_peer(manifest, "hub") == ["t", "v", "z"]
    assert every_peer(manifest, "z") == ["hub", "t", "v"]


def test_links_name_their_peers_or_every_peer_or_none() -> None:
    manifest = fleet(hub=hub(True), b=host(address="10.8.0.2"), a=host(address="10.8.0.3"))
    assert link_peers(manifest, "hub", {"links": True}) == ["a", "b"]
    assert link_peers(manifest, "hub", {"links": ["b", "nowhere"]}) == ["b", "nowhere"]
    assert link_peers(manifest, "hub", {"links": False}) == []
    assert link_peers(manifest, "hub", {"links": []}) == []
    assert link_peers(manifest, "hub", {}) == []


def test_the_context_gives_each_reachable_peer_its_address_and_zone() -> None:
    manifest = fleet(
        hub=hub(["b", "a", "nowhere", "c"]),
        a=host(address="10.8.0.3", zone="shop"),
        b=host(address="10.8.0.2"),
        c=host(),
    )
    settings = {"links": ["b", "a", "nowhere", "c"]}
    assert links_context(manifest, "hub", settings) == [
        {"peer": "b", "address": "10.8.0.2", "zone": "b"},
        {"peer": "a", "address": "10.8.0.3", "zone": "shop"},
    ]
    assert links_context(manifest, "hub", None) == []
    assert links_context(manifest, "hub", {}) == []


def test_a_fleet_is_linked_once_any_host_probes_a_host_with_an_address() -> None:
    peer = host(address="10.8.0.2")
    assert linked(fleet(a=peer, hub=hub(["a"])))
    assert linked(fleet(a=peer, hub=hub(True), b=host(modules={"probes": {}})))
    assert not linked(fleet(a=host(), hub=hub(["a"])))
    assert not linked(fleet(a=host(), hub=hub(True)))
    assert not linked(fleet(a=peer, hub=hub([])))
    assert not linked(fleet(a=peer, hub=hub(False)))
    assert not linked(fleet(a=peer, b=host(modules={"probes": {}})))
    assert not linked(fleet(a=peer))


def test_without_windows_every_probe_counts() -> None:
    assert reachable({}) == PROBE
    assert link_down_expr({}) == (
        'max by (host, project, group, zone) (probe_success{job="blackbox-link"}) == bool 0'
    )


def test_a_peer_in_an_open_window_drops_out_of_its_zone() -> None:
    windows = {"a": SUN, "b": DAILY}
    held = (
        f'label_replace({SUN}, "peer", "a", "", "") or label_replace({DAILY}, "peer", "b", "", "")'
    )
    assert reachable(windows) == f"{PROBE} unless on (peer) ({held})"
    assert link_down_expr(windows) == (
        f"max by (host, project, group, zone) ({PROBE} unless on (peer) ({held})) == bool 0"
    )


def test_the_rendered_alert_without_windows_is_the_one_of_the_module() -> None:
    spec = yaml.safe_load((MODULES_DIR / "probes" / "module.yaml").read_text(encoding="utf-8"))
    alert = next(a for a in spec["alerts"] if a["name"] == LINK_DOWN)
    assert alert["expr"] == link_down_expr({})
    assert alert["only_with"] == "links"
    assert alert["severity"] == "critical"


PEERS = fleet(
    hub=hub([]),
    near=host(address="10.8.0.2"),
    bare=host(),
    gone=host(address="10.8.0.3", volatile="2h"),
)


@pytest.mark.parametrize(
    ("peer", "problem"),
    [
        ("nowhere", "unknown host 'nowhere'"),
        ("hub", "hub cannot probe the link to itself"),
        ("bare", "bare has no address to ping over the private network"),
        (
            "gone",
            "gone is volatile: a link to a host that is expected to disappear pages each time",
        ),
        ("near", None),
    ],
)
def test_a_peer_is_another_host_with_an_address_that_stays(peer: str, problem: str | None) -> None:
    assert peer_problem(PEERS, "hub", peer) == problem


def test_the_problems_name_each_bad_peer_of_each_host() -> None:
    manifest = fleet(
        hub=hub(["nowhere", "near", "bare"]),
        near=host(address="10.8.0.2", modules={"probes": {"links": ["near"]}}),
        bare=host(),
        quiet=host(modules={"probes": {}}),
        off=host(modules={"probes": False, "node": {}}),
    )
    assert link_problems(manifest) == [
        (PATH, RULE, "unknown host 'nowhere'"),
        (PATH, RULE, "bare has no address to ping over the private network"),
        ("hosts.near.modules.probes.links", RULE, "near cannot probe the link to itself"),
    ]


def test_links_true_takes_whatever_qualifies_and_false_takes_nothing() -> None:
    lonely = fleet(
        hub=hub(True), bare=host(), late=host(modules={"probes": {"links": ["nowhere"]}})
    )
    assert link_problems(lonely) == [
        ("hosts.late.modules.probes.links", RULE, "unknown host 'nowhere'")
    ]
    assert link_problems(fleet(hub=hub(False), near=host(address="10.8.0.2"))) == []
