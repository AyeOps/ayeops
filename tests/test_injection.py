from __future__ import annotations

import copy
import json
import re
from html.parser import HTMLParser
from typing import Any

import pytest
import yaml

from ayeops.local_modules import with_local_modules
from ayeops.manifest import schema_errors
from ayeops.portal.pages import PortalDrift
from ayeops.registry import Module, load_modules
from ayeops.render import render_tree
from ayeops.rules import check_modules
from ayeops.strict import StrictValidator
from tests.conftest import EXAMPLE_STAMP, example_data, manifest_from

MARK = "ZQX"
HOSTILE = MARK + "'\"\n`$(id)${HOME}{{7*7}}</script><b>x</b>\\#: - &a *a"
EXPRESSIONS = {"expr", "metrics", "summary", "match"}


def string_paths(node: Any, path: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    if isinstance(node, dict):
        return [p for key, value in node.items() for p in string_paths(value, (*path, key))]
    if isinstance(node, list):
        return [p for i, value in enumerate(node) for p in string_paths(value, (*path, i))]
    return [path] if isinstance(node, str) else []


def with_value(data: dict[str, Any], path: tuple[Any, ...], suffix: str) -> dict[str, Any]:
    changed = copy.deepcopy(data)
    node: Any = changed
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = node[path[-1]] + suffix
    return changed


def render_refuses(data: dict[str, Any]) -> bool:
    if schema_errors(data):
        return True
    manifest = manifest_from(data)
    return bool(check_modules(manifest, with_local_modules(manifest, load_modules())))


def accepted_paths() -> list[tuple[Any, ...]]:
    data = example_data()
    return [
        path
        for path in string_paths(data)
        if not EXPRESSIONS & {str(part) for part in path}
        and not render_refuses(with_value(data, path, HOSTILE))
    ]


class Tags(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: list[str] = []
        self.text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append(tag)
        self.text += [value or "" for _, value in attrs]

    def handle_endtag(self, tag: str) -> None:
        self.tags.append("/" + tag)

    def handle_data(self, data: str) -> None:
        self.text.append(data)


def html_of(content: str) -> Tags:
    parser = Tags()
    parser.feed(content)
    parser.close()
    return parser


def strings_in(node: Any) -> list[str]:
    if isinstance(node, dict):
        return [s for key, value in node.items() for s in [str(key), *strings_in(value)]]
    if isinstance(node, list):
        return [s for value in node for s in strings_in(value)]
    return [node] if isinstance(node, str) else []


def keys_in(node: Any) -> list[str]:
    if isinstance(node, dict):
        return [str(key) for key in node] + [k for value in node.values() for k in keys_in(value)]
    if isinstance(node, list):
        return [k for value in node for k in keys_in(value)]
    return []


DOLLAR_EXPANDING = ("compose.yaml", "grafana/provisioning/")
JSON_STRING = re.compile(r'"(?:[^"\\\n]|\\.)*"')
PAYLOAD_TAGS = ("script", "b")


def unescaped(name: str, value: str) -> str:
    if any(part in name for part in DOLLAR_EXPANDING):
        return value.replace("$$", "$")
    return value


def expands_a_variable(name: str, value: str) -> bool:
    if not any(part in name for part in DOLLAR_EXPANDING):
        return False
    return "${HOME}" in value.replace("$$", "")


def nested(text: str) -> list[str]:
    try:
        inner = json.loads(text)
    except ValueError:
        return [text]
    return [text, *strings_in(inner)]


def breakouts(name: str, content: str, benign: str | None) -> list[str]:
    if MARK not in content:
        return []
    problems: list[str] = []
    suffix = name.rsplit(".", 1)[-1]
    if suffix in ("yaml", "yml", "json"):
        try:
            parsed = yaml.safe_load(content) if suffix != "json" else json.loads(content)
        except (yaml.YAMLError, ValueError) as exc:
            return [f"{name}: no longer parses ({type(exc).__name__})"]
        if any(MARK in key for key in keys_in(parsed)):
            problems.append(f"{name}: the value became a key")
        carriers = [s for s in strings_in(parsed) if MARK in s]
        if not any(HOSTILE in text for s in carriers for text in nested(unescaped(name, s))):
            problems.append(f"{name}: the value did not survive as one string")
        if any(expands_a_variable(name, s) for s in carriers):
            problems.append(f"{name}: the value would expand ${{HOME}}")
        if re.search(r"Host\(`[^`]*" + MARK, content):
            problems.append(f"{name}: the value sits inside a Traefik backtick rule")
    elif suffix == "alloy":
        literals = [json.loads(m) for m in JSON_STRING.findall(content) if MARK in m]
        if not any(HOSTILE in literal for literal in literals):
            problems.append(f"{name}: the value left its Alloy string")
    elif suffix in ("html", "svg"):
        page = html_of(content)
        before = html_of(benign or "").tags
        for tag in PAYLOAD_TAGS:
            if page.tags.count(tag) != before.count(tag):
                problems.append(f"{name}: the value added a <{tag}> element")
        if not any(MARK in t for t in page.text):
            problems.append(f"{name}: the value vanished from the text")
    else:
        problems.append(f"{name}: free text reached a .{suffix} file without a context check")
    return problems


@pytest.fixture(scope="module")
def mods() -> dict[str, Module]:
    return load_modules()


@pytest.fixture(scope="module")
def benign(mods: dict[str, Module]) -> dict[str, str]:
    return render_tree(manifest_from(example_data()), mods, EXAMPLE_STAMP)


@pytest.mark.parametrize("path", accepted_paths(), ids=lambda p: ".".join(map(str, p)))
def test_a_hostile_manifest_value_cannot_leave_its_context_in_any_rendered_file(
    mods: dict[str, Module], benign: dict[str, str], path: tuple[Any, ...]
) -> None:
    try:
        tree = render_tree(
            manifest_from(with_value(example_data(), path, HOSTILE)), mods, EXAMPLE_STAMP
        )
    except PortalDrift:
        return
    problems = [p for name, body in tree.items() for p in breakouts(name, body, benign.get(name))]
    assert not problems, problems


@pytest.mark.parametrize(
    ("pattern", "value"),
    [
        ("^[a-z][a-z0-9-]{0,39}$", "web\n"),
        ("^[a-z0-9.-]+$", "acme.example\n"),
        ("^https?://", "javascript:alert(1)"),
    ],
)
def test_a_trailing_newline_does_not_satisfy_an_anchored_pattern(pattern: str, value: str) -> None:
    assert not StrictValidator({"type": "string", "pattern": pattern}).is_valid(value)


def test_a_service_name_with_a_newline_is_refused_before_rendering() -> None:
    data = example_data()
    data["services"]["web\n"] = data["services"].pop("web")
    assert schema_errors(data)


def test_an_email_carrying_a_second_line_is_refused_before_rendering() -> None:
    data = example_data()
    data["alerting"] = {**(data.get("alerting") or {}), "emails": ["junk\nboss@evil.example"]}
    assert schema_errors(data)


def test_an_admin_email_ending_in_a_backslash_cannot_reopen_the_grafana_role_path(
    mods: dict[str, Module],
) -> None:
    data = example_data()
    auth = data["hosts"]["central"]["modules"]["auth"]
    auth["admins"] = ["ops@acme.example\\"]
    auth["emails"] = [*auth["emails"], "ops@acme.example\\"]
    manifest = manifest_from(data)
    assert check_modules(manifest, with_local_modules(manifest, mods))
