from __future__ import annotations

import copy
import os
import re
import sys
import tempfile
from collections import OrderedDict
from collections.abc import Callable
from functools import cache
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

from ayeops.manifest import Manifest
from ayeops.registry import Module, load_modules
from ayeops.render import render_tree
from ayeops.rules import Finding, check_manifest, check_warnings
from ayeops.stamp import Stamp

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "acme.yaml"
RENDERED = ROOT / "examples" / "rendered"
EXAMPLE_STAMP = Stamp("2026-01-01 00:00 UTC", "example")
WAIT = 300.0
TEMP = {repr(tempfile.gettempdir())[1:-1], Path(tempfile.gettempdir()).as_posix()}


def in_lang(html: str, lang: str) -> str:
    tag = r'<(span|tspan) (?:lang="[a-z]{2}" )?data-l="'
    other = re.compile(tag + rf'(?!{lang}")[a-z]{{2}}">.*?</\1>', re.S)
    own = re.compile(tag + rf'{lang}">(.*?)</\1>', re.S)
    return own.sub(r"\2", other.sub("", html))


def english(html: str) -> str:
    return in_lang(html, "en")


INDEX_DIGEST = re.compile(r"@sha256:[a-f0-9]{64}$")


def tag_of(image: str) -> str:
    assert INDEX_DIGEST.search(image), f"{image} is not pinned by digest"
    return INDEX_DIGEST.sub("", image)


TEMPLATE_IMAGE = re.compile(
    r"""^\s*image:\s*["']?([a-z0-9][a-z0-9./_-]*):([A-Za-z0-9][A-Za-z0-9._-]*)@sha256:[a-f0-9]{64}""",
    re.M,
)


@cache
def template_images(module: str) -> frozenset[tuple[str, str]]:
    template = ROOT / "src" / "ayeops" / "modules" / module / "compose.yaml.j2"
    text = template.read_text(encoding="utf-8")
    return frozenset((m[1], m[2]) for m in TEMPLATE_IMAGE.finditer(text))


def pinned(module: str, repository: str, tag: str = "") -> str:
    found = sorted(
        f"{name}:{version}"
        for name, version in template_images(module)
        if name == repository and re.search(tag, version)
    )
    assert len(found) == 1, f"{module} pins {found} for {repository} {tag!r}"
    return found[0]


@cache
def parsed_example() -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    return data


def example_data() -> dict[str, Any]:
    return copy.deepcopy(parsed_example())


def manifest_from(data: dict[str, Any]) -> Manifest:
    return Manifest(data, EXAMPLE.parent)


def under_mutmut() -> bool:
    return "MUTANT_UNDER_TEST" in os.environ or "mutmut" in sys.modules


def memo_key(manifest: Manifest, modules: dict[str, Module], *extra: object) -> str | None:
    if under_mutmut() or modules is not load_modules() or manifest.base_dir != EXAMPLE.parent:
        return None
    key = repr((manifest.raw, *extra))
    return None if any(temp in key for temp in TEMP) else key


def memoized[T](key: str | None, compute: Callable[[], T]) -> T:
    if key is None:
        return compute()
    if key in MEMO:
        MEMO.move_to_end(key)
    else:
        MEMO[key] = compute()
        while len(MEMO) > MEMO_SIZE:
            MEMO.popitem(last=False)
    return cast(T, MEMO[key])


MEMO: OrderedDict[str, object] = OrderedDict()
MEMO_SIZE = 48


def render_once(
    manifest: Manifest, modules: dict[str, Module], stamp: Stamp | None = None
) -> dict[str, str]:
    key = memo_key(manifest, modules, "render", stamp)
    return dict(memoized(key, lambda: render_tree(manifest, modules, stamp)))


def check_once(manifest: Manifest, modules: dict[str, Module]) -> list[Finding]:
    key = memo_key(manifest, modules, "check")
    return list(memoized(key, lambda: tuple(check_manifest(manifest, modules))))


def rules_hit(data: dict[str, Any]) -> set[str]:
    return {finding.rule for finding in check_once(manifest_from(data), load_modules())}


def warnings_hit(data: dict[str, Any]) -> set[str]:
    manifest = manifest_from(data)
    return {finding.rule for finding in check_warnings(manifest, modules=load_modules())}


@pytest.fixture(autouse=True)
def no_default_manifest(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AYE_MANIFEST", raising=False)


@pytest.fixture
def data() -> dict[str, Any]:
    return example_data()


@pytest.fixture
def modules() -> dict[str, Module]:
    return load_modules()
