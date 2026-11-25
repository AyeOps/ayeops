from __future__ import annotations

import base64
import html
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ayeops.manifest import Manifest

MEDIA = {
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
}
FILES = ("logo", "logo_dark", "favicon")
OPS_COLOR = "#ac8aff"
TILE = "#0b1220"


@dataclass(frozen=True)
class Brand:
    name: str
    logo: str
    logo_dark: str
    favicon: str
    color: str


def brand_spec(manifest: Manifest) -> dict[str, Any]:
    return dict(manifest.data.get("brand") or {})


def brand_problems(manifest: Manifest) -> list[tuple[str, str, str]]:
    spec = brand_spec(manifest)
    problems = []
    for key in FILES:
        if not spec.get(key):
            continue
        path = manifest.base_dir / str(spec[key])
        if path.suffix.lower() not in MEDIA:
            supported = ", ".join(sorted(MEDIA))
            problems.append((f"brand.{key}", "brand-file", f"{path.name}: not {supported}"))
        elif not path.is_file():
            problems.append(
                (f"brand.{key}", "brand-file", f"no file {spec[key]} next to the manifest")
            )
    return problems


def data_uri(payload: bytes, media: str) -> str:
    return f"data:{media};base64,{base64.b64encode(payload).decode('ascii')}"


def file_uri(base_dir: Path, value: Any) -> str:
    if not value:
        return ""
    path = base_dir / str(value)
    media = MEDIA.get(path.suffix.lower())
    if media is None or not path.is_file():
        return ""
    return data_uri(path.read_bytes(), media)


def ops_favicon(glyph: str, name: str, color: str) -> str:
    if glyph:
        mark = f'<image href="{glyph}" x="9" y="7" width="34" height="34"/>'
    else:
        letter = html.escape((name[:1] or "?").upper())
        mark = (
            f'<text x="26" y="35" text-anchor="middle" fill="#ffffff"'
            f' font-family="system-ui,sans-serif" font-size="28" font-weight="700">{letter}</text>'
        )
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
        f'<rect x="2" y="2" width="60" height="60" rx="15" fill="{TILE}"'
        f' stroke="{color}" stroke-width="4"/>'
        f"{mark}"
        f'<rect x="22" y="40" width="40" height="22" rx="8" fill="{color}"/>'
        f'<text x="42" y="56" text-anchor="middle" fill="{TILE}"'
        ' font-family="system-ui,sans-serif" font-size="15" font-weight="800">ops</text>'
        "</svg>"
    )


def load_brand(manifest: Manifest) -> Brand:
    spec = brand_spec(manifest)
    logo = file_uri(manifest.base_dir, spec.get("logo"))
    logo_dark = file_uri(manifest.base_dir, spec.get("logo_dark"))
    color = str(spec.get("color") or OPS_COLOR)
    name = str(spec.get("name") or manifest.project)
    favicon = file_uri(manifest.base_dir, spec.get("favicon"))
    if not favicon:
        svg = ops_favicon(logo_dark or logo, name, color)
        favicon = data_uri(svg.encode("utf-8"), MEDIA[".svg"])
    return Brand(name, logo, logo_dark, favicon, color)
