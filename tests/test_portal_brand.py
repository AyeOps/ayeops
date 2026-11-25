from __future__ import annotations

import base64
import re
from pathlib import Path
from typing import Any

from ayeops.manifest import Manifest
from ayeops.portal.brand import OPS_COLOR, load_brand
from ayeops.portal.pages import portal_pages
from ayeops.registry import Module
from tests.conftest import EXAMPLE, EXAMPLE_STAMP, english, manifest_from, rules_hit


def page_html(data: dict[str, Any], modules: dict[str, Module]) -> str:
    return english(portal_pages(manifest_from(data), modules, EXAMPLE_STAMP)["index.html"])


def favicon_of(html: str) -> str:
    found = re.search(r'<link rel="icon" href="data:image/svg\+xml;base64,([^"]+)">', html)
    assert found
    return base64.b64decode(found.group(1)).decode("utf-8")


def uri_of(path: Path) -> str:
    return "data:image/svg+xml;base64," + base64.b64encode(path.read_bytes()).decode("ascii")


def test_header_shows_the_logo_in_both_themes(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    html = page_html(data, modules)
    light = uri_of(EXAMPLE.parent / "brand" / "acme.svg")
    dark = uri_of(EXAMPLE.parent / "brand" / "acme-dark.svg")
    assert f'<img class="logo light" src="{light}" alt="" height="28">' in html
    assert f'<img class="logo dark" src="{dark}" alt="" height="28">' in html
    assert '<span class="mark">' not in html
    assert ">Where things run · Acme</title>" in html
    assert 'name="theme-color"' in html


def test_generated_favicon_puts_the_logo_on_an_ops_tile(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    svg = favicon_of(page_html(data, modules))
    assert uri_of(EXAMPLE.parent / "brand" / "acme-dark.svg") in svg
    assert ">ops</text>" in svg
    assert f'stroke="{OPS_COLOR}"' in svg


def test_brand_color_tints_the_badge(data: dict[str, Any], modules: dict[str, Module]) -> None:
    data["brand"]["color"] = "#ff8800"
    svg = favicon_of(page_html(data, modules))
    assert svg.count('"#ff8800"') == 2
    assert OPS_COLOR not in svg


def test_a_single_logo_serves_both_themes(data: dict[str, Any], modules: dict[str, Module]) -> None:
    del data["brand"]["logo_dark"]
    html = page_html(data, modules)
    light = uri_of(EXAMPLE.parent / "brand" / "acme.svg")
    assert f'<img class="logo" src="{light}" alt="" height="28">' in html
    assert light in favicon_of(html)


def test_without_brand_the_portal_keeps_the_dot_and_a_monogram_favicon(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    del data["brand"]
    html = page_html(data, modules)
    assert '<a class="brand" href="index.html"><span class="mark"></span>acme</a>' in html
    svg = favicon_of(html)
    assert ">A</text>" in svg
    assert ">ops</text>" in svg
    assert "<image" not in svg


def test_a_declared_favicon_is_used_as_is(tmp_path: Path, data: dict[str, Any]) -> None:
    icon = tmp_path / "icon.png"
    icon.write_bytes(b"\x89PNG-bytes")
    data["brand"] = {"favicon": "icon.png"}
    brand = load_brand(Manifest(data, tmp_path))
    assert brand.favicon == "data:image/png;base64," + base64.b64encode(icon.read_bytes()).decode()


def test_a_missing_brand_file_falls_back_when_rendering(
    tmp_path: Path, data: dict[str, Any], modules: dict[str, Module]
) -> None:
    data["brand"] = {"logo": "gone.svg", "favicon": "gone.png"}
    brand = load_brand(Manifest(data, tmp_path))
    assert brand.logo == ""
    assert base64.b64decode(brand.favicon.split(",", 1)[1]).startswith(b"<svg")


def test_brand_files_must_exist_and_be_images(data: dict[str, Any]) -> None:
    assert "brand-file" not in rules_hit(data)
    data["brand"]["logo"] = "brand/missing.svg"
    assert "brand-file" in rules_hit(data)
    data["brand"]["logo"] = "acme.yaml"
    assert "brand-file" in rules_hit(data)


def test_brand_color_must_be_a_hex_colour(data: dict[str, Any]) -> None:
    data["brand"]["color"] = "red"
    assert rules_hit(data) == {"schema"}


def test_brand_name_is_escaped(data: dict[str, Any]) -> None:
    data["brand"] = {"name": "<b>"}
    brand = load_brand(manifest_from(data))
    assert ">&lt;</text>" in base64.b64decode(brand.favicon.split(",", 1)[1]).decode()
