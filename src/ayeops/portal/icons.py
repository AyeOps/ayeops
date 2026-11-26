from __future__ import annotations

import json
import re
from functools import cache
from html import escape
from pathlib import Path
from typing import Any

from ayeops.manifest import Manifest
from ayeops.registry import Module
from ayeops.render import FleetRender, compose_file_services

ICONS_FILE = Path(__file__).parent / "assets" / "icons.json"
SERVICE = "service"
HOST = "host"
CLOUD = "cloud"
LINK = "link"
MCP = "modelcontextprotocol"
ALIASES = {
    "mcp": MCP,
    "traefik": "traefikproxy",
    "nodejs": "nodedotjs",
    "node.js": "nodedotjs",
    "postgres": "postgresql",
    "victoria": "victoriametrics",
}
GUESSES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (MCP, ("mcp", "modelcontextprotocol")),
    ("victoriametrics", ("victoriametrics", "victoria", "vmagent", "vmalert", "vmauth")),
    ("grafana", ("grafana", "loki", "alloy", "tempo", "mimir", "pyroscope")),
    ("prometheus", ("prometheus", "exporter", "blackbox", "alertmanager", "pushgateway")),
    ("traefikproxy", ("traefik",)),
    ("caddy", ("caddy",)),
    ("postgresql", ("postgres", "postgresql", "postgis", "pgbouncer", "timescaledb")),
    ("mariadb", ("mariadb",)),
    ("mysql", ("mysql",)),
    ("redis", ("redis",)),
    ("sqlite", ("sqlite",)),
    ("rabbitmq", ("rabbitmq",)),
    ("minio", ("minio",)),
    ("nginx", ("nginx", "openresty")),
    ("kubernetes", ("kubernetes", "k8s", "k3s", "kubectl")),
    ("helm", ("helm",)),
    ("cloudflare", ("cloudflare", "cloudflared")),
    ("tailscale", ("tailscale",)),
    ("wireguard", ("wireguard", "wg-easy", "wg")),
    ("telegram", ("telegram",)),
    ("discord", ("discord",)),
    ("ntfy", ("ntfy",)),
    ("github", ("github",)),
    ("gitea", ("gitea",)),
    ("renovate", ("renovate",)),
    ("watchtower", ("watchtower",)),
    ("portainer", ("portainer",)),
    ("uptimekuma", ("uptime-kuma", "uptimekuma")),
    ("netdata", ("netdata",)),
    ("fluentbit", ("fluent-bit", "fluentbit")),
    ("sentry", ("sentry",)),
    ("vaultwarden", ("vaultwarden",)),
    ("nextcloud", ("nextcloud",)),
    ("homeassistant", ("home-assistant", "homeassistant")),
    ("pihole", ("pihole", "pi-hole")),
    ("syncthing", ("syncthing",)),
    ("ollama", ("ollama",)),
    ("claude", ("claude",)),
    ("anthropic", ("anthropic",)),
    ("nvidia", ("nvidia", "gpu", "cuda")),
    ("proxmox", ("proxmox", "pve")),
    ("raspberrypi", ("raspberry", "raspberrypi", "rpi")),
    ("docker", ("docker", "dind", "cadvisor", "containers")),
    ("fastapi", ("fastapi",)),
    ("django", ("django",)),
    ("flask", ("flask",)),
    ("python", ("python", "python3", "uvicorn", "gunicorn", "celery", "uv")),
    ("nodedotjs", ("node", "nodejs", "npm", "npx")),
    ("go", ("golang",)),
    ("ubuntu", ("ubuntu",)),
)
CLOUD_WORDS = (
    "oracle",
    "oci",
    "aws",
    "amazon",
    "azure",
    "gcp",
    "google",
    "hetzner",
    "digitalocean",
    "ovh",
    "linode",
    "scaleway",
    "cloud",
)
TOKEN = re.compile(r"[^a-z0-9]+")


@cache
def icon_data() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(ICONS_FILE.read_text(encoding="utf-8"))
    return data


def known_icons() -> dict[str, dict[str, str]]:
    data = icon_data()
    return {**data["glyphs"], **data["icons"]}


def resolve(slug: str) -> str | None:
    name = ALIASES.get(slug.lower(), slug.lower())
    return name if name in known_icons() else None


def image_repo(image: str) -> str:
    repo = image.split("@", 1)[0]
    head, _, tail = repo.rpartition("/")
    tail = tail.split(":", 1)[0]
    return f"{head}/{tail}" if head else tail


def matches(keyword: str, text: str, tokens: set[str]) -> bool:
    if keyword in tokens:
        return True
    return len(keyword) >= 5 and keyword in text


def guess(*texts: str) -> str | None:
    text = " ".join(t.lower() for t in texts if t)
    tokens = set(TOKEN.split(text)) | set(text.split())
    for slug, keywords in GUESSES:
        if any(matches(keyword, text, tokens) for keyword in keywords):
            return slug
    return None


def command_text(service: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in ("entrypoint", "command"):
        value = service.get(key) or []
        parts.extend(str(item) for item in (value if isinstance(value, list) else [value]))
    return " ".join(parts)


def service_icon(
    name: str, image: str = "", declared: str = "", url: str = "", command: str = ""
) -> str:
    if declared and resolve(declared):
        return str(resolve(declared))
    return guess(name, image_repo(image), url) or guess(command) or SERVICE


def host_icon(name: str, spec: dict[str, Any]) -> str:
    declared = str(spec.get("icon") or "")
    if declared and resolve(declared):
        return str(resolve(declared))
    provider = " ".join(str(spec.get(key) or "") for key in ("provider", "zone"))
    found = guess(provider, name)
    if found in ("cloudflare", "ubuntu", "kubernetes"):
        return found
    tokens = set(TOKEN.split(provider.lower()))
    if any(word in tokens for word in CLOUD_WORDS):
        return CLOUD
    return HOST


def external_icon(name: str, spec: dict[str, Any]) -> str:
    declared = str(spec.get("icon") or "")
    if declared and resolve(declared):
        return str(resolve(declared))
    return guess(name, str(spec.get("title") or ""), str(spec.get("url") or "")) or LINK


def node_icons(
    manifest: Manifest, modules: dict[str, Module], renders: FleetRender | None = None
) -> dict[str, str]:
    renders = renders or FleetRender(manifest, modules)
    icons: dict[str, str] = {}
    for host in sorted(manifest.hosts):
        own = {n: e for n, e in manifest.services.items() if e.get("host") == host}
        rendered = renders.compose(host)["services"]
        for name, service in rendered.items():
            module = modules[service["labels"]["io.ayeops.module"]]
            entry = {**module.catalog.get(name, {}), **own.get(name, {})}
            icons[f"{host}/{name}"] = service_icon(
                name, str(service.get("image") or ""), str(entry.get("icon") or "")
            )
        for name, service in compose_file_services(manifest, host).items():
            entry = own.get(name, {})
            icons.setdefault(
                f"{host}/{name}",
                service_icon(
                    name,
                    str(service.get("image") or ""),
                    str(entry.get("icon") or ""),
                    str(entry.get("url") or ""),
                    command_text(service),
                ),
            )
    for name, spec in manifest.externals.items():
        icons[f"external:{name}"] = external_icon(name, spec)
    for entry in manifest.services.values():
        for dependency in entry.get("cloud") or []:
            dep = str(dependency["name"])
            icons[f"cloud:{dep}"] = guess(dep) or CLOUD
    return icons


def is_mcp(icon: str) -> bool:
    return icon == MCP


def symbol(slug: str) -> str:
    data = icon_data()
    glyph = slug in data["glyphs"]
    entry = known_icons()[slug]
    rule = ' fill-rule="evenodd"' if glyph else ""
    return (
        f'<symbol id="i-{slug}" viewBox="0 0 24 24"><title>{escape(entry["title"])}</title>'
        f'<path{rule} d="{entry["path"]}"/></symbol>'
    )


def sprite(slugs: set[str]) -> str:
    if not slugs:
        return ""
    return (
        '<svg class="sprite" aria-hidden="true" width="0" height="0">'
        + "".join(symbol(slug) for slug in sorted(slugs))
        + "</svg>"
    )


def use(slug: str, cls: str = "ico") -> str:
    return f'<svg class="{cls}" aria-hidden="true" focusable="false"><use href="#i-{slug}"/></svg>'


def is_product(slug: str) -> bool:
    return slug in icon_data()["icons"]


def monogram(label: str) -> str:
    words = [word for word in TOKEN.split(label.lower()) if word]
    if not words:
        return "?"
    if len(words) == 1:
        return words[0][0].upper()
    return (words[0][0] + words[1][0]).upper()


def luminance(hex_colour: str) -> float:
    channels = [int(hex_colour[i : i + 2], 16) / 255 for i in (0, 2, 4)]
    linear = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def lighten(hex_colour: str, share: float) -> str:
    channels = [int(hex_colour[i : i + 2], 16) for i in (0, 2, 4)]
    mixed = [round(c + (255 - c) * share) for c in channels]
    return "#" + "".join(f"{c:02X}" for c in mixed)


def brand_css(slugs: set[str]) -> str:
    rules = []
    for slug in sorted(s for s in slugs if is_product(s)):
        colour = str(icon_data()["icons"][slug]["hex"])
        dark = lighten(colour, 0.5) if luminance(colour) < 0.22 else f"#{colour}"
        rules.append(f".b-{slug}{{--b:#{colour};--b2:{dark}}}")
    return "".join(rules)


def badge(slug: str, label: str, cls: str = "lgo") -> str:
    if slug in (SERVICE, LINK):
        return f'<span class="{cls} mono" aria-hidden="true">{escape(monogram(label))}</span>'
    brand = f" b-{slug}" if is_product(slug) else ""
    return f'<span class="{cls}{brand}" aria-hidden="true">{use(slug, "ico lg")}</span>'


def svg_badge(
    slug: str, label: str, cx: float, cy: float, radius: float, letters: bool = True
) -> str:
    size = radius * 1.12
    disc = f'<circle class="pin" cx="{cx:g}" cy="{cy:g}" r="{radius:g}"/>'
    if letters and slug in (SERVICE, LINK):
        return (
            f'<g class="badge mono">{disc}<text class="mono" x="{cx:g}" y="{cy + 4.5:g}">'
            f"{escape(monogram(label))}</text></g>"
        )
    brand = f" b-{slug}" if is_product(slug) else ""
    return (
        f'<g class="badge{brand}">{disc}<use class="lg" href="#i-{slug}"'
        f' x="{cx - size / 2:g}" y="{cy - size / 2:g}" width="{size:g}" height="{size:g}"/></g>'
    )
