from __future__ import annotations

from typing import Any

from ayeops.manifest import Manifest

Problem = tuple[str, str, str]

JOB = "blackbox-link"
PROBE = f'probe_success{{job="{JOB}"}}'
LINK_DOWN = "link-down"
RULE = "probe-links"
BY = "host, project, group, zone"
UNREACHABLE = (
    f"label_replace(max by (peer) ({PROBE} unless on (zone) (max by (zone) ({PROBE}) > 0)),"
    ' "host", "$1", "peer", "(.+)")'
)


def link_zone(manifest: Manifest, peer: str) -> str:
    return str(manifest.hosts[peer].get("zone") or peer)


def every_peer(manifest: Manifest, host: str) -> list[str]:
    return [
        name
        for name, spec in sorted(manifest.hosts.items())
        if name != host and spec.get("address") and not spec.get("volatile")
    ]


def link_peers(manifest: Manifest, host: str, settings: dict[str, Any]) -> list[str]:
    wanted = settings.get("links")
    if wanted is True:
        return every_peer(manifest, host)
    return [str(peer) for peer in wanted or []]


def links_context(
    manifest: Manifest, host: str, settings: dict[str, Any] | None
) -> list[dict[str, str]]:
    return [
        {
            "peer": peer,
            "address": str(manifest.hosts[peer]["address"]),
            "zone": link_zone(manifest, peer),
        }
        for peer in link_peers(manifest, host, settings or {})
        if manifest.hosts.get(peer, {}).get("address")
    ]


def linked(manifest: Manifest) -> bool:
    return any(
        links_context(manifest, host, manifest.host_modules(host).get("probes"))
        for host in manifest.hosts
    )


def reachable(windows: dict[str, str]) -> str:
    if not windows:
        return PROBE
    held = " or ".join(
        f'label_replace({expr}, "peer", "{host}", "", "")' for host, expr in windows.items()
    )
    return f"{PROBE} unless on (peer) ({held})"


def link_down_expr(windows: dict[str, str]) -> str:
    return f"max by ({BY}) ({reachable(windows)}) == bool 0"


def peer_problem(manifest: Manifest, host: str, peer: str) -> str | None:
    if peer not in manifest.hosts:
        return f"unknown host {peer!r}"
    if peer == host:
        return f"{host} cannot probe the link to itself"
    spec = manifest.hosts[peer]
    if not spec.get("address"):
        return f"{peer} has no address to ping over the private network"
    if spec.get("volatile"):
        return f"{peer} is volatile: a link to a host that is expected to disappear pages each time"
    return None


def link_problems(manifest: Manifest) -> list[Problem]:
    problems: list[Problem] = []
    for host in sorted(manifest.hosts):
        probes = manifest.host_modules(host).get("probes")
        if probes is None:
            continue
        wanted = probes.get("links")
        path = f"hosts.{host}.modules.probes.links"
        if wanted is True:
            continue
        for peer in wanted or []:
            problem = peer_problem(manifest, host, str(peer))
            if problem:
                problems.append((path, RULE, problem))
    return problems
