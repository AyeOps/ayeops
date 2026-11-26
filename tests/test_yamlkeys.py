from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml

from ayeops.cli import main
from ayeops.manifest import Manifest, load_manifest
from ayeops.mcp import openapi_operations
from ayeops.registry import MODULES_DIR, load_modules
from ayeops.rules import check_manifest
from ayeops.yamlkeys import (
    StrictYAMLError,
    load_strict,
    nested,
    read_strict,
    syntax_problem,
    unreadable,
)
from tests.conftest import EXAMPLE, ROOT, manifest_from, parsed_example, rules_hit

FIXTURE = ROOT / "tests" / "fixtures" / "modules"


def problems_of(text: str, key_types: bool = True) -> list[tuple[str, str, str]]:
    with pytest.raises(StrictYAMLError) as raised:
        load_strict(text, "fleet.yaml", key_types)
    return raised.value.problems


def check_output(tmp_path: Path, text: str, capsys: pytest.CaptureFixture[str]) -> list[str]:
    path = tmp_path / "fleet.yaml"
    path.write_text(text, encoding="utf-8")
    assert main(["check", str(path)]) == 1
    return capsys.readouterr().err.splitlines()


def test_a_repeated_host_is_an_aye_check_finding_with_both_positions(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    text = "project: x\nhosts:\n  a:\n    address: 10.0.0.1\n  a:\n    address: 10.0.0.2\n"
    assert check_output(tmp_path, text, capsys) == [
        "error [yaml-duplicate-key] hosts.a: fleet.yaml line 5, column 3: the key 'a'"
        " repeats line 3, column 3; YAML keeps only the last one",
        "fleet.yaml: 1 error(s)",
    ]


def test_a_service_repeated_inside_a_host_is_found() -> None:
    text = "hosts:\n  a:\n    modules:\n      node: {}\n      metrics: {}\n      node: false\n"
    assert problems_of(text) == [
        (
            "hosts.a.modules.node",
            "yaml-duplicate-key",
            "fleet.yaml line 6, column 7: the key 'node' repeats line 4, column 7;"
            " YAML keeps only the last one",
        )
    ]


def test_a_repeated_top_level_service_is_found() -> None:
    text = "services:\n  web: {host: a}\n  api: {host: a}\n  web: {host: b}\n"
    assert [(path, rule) for path, rule, _ in problems_of(text)] == [
        ("services.web", "yaml-duplicate-key")
    ]


def test_a_repeated_key_in_a_mapping_inside_a_list_is_found() -> None:
    text = (
        "services:\n  web:\n    alerts:\n      - name: up\n        labels: {team: web, team: ops}\n"
    )
    problems = problems_of(text)
    assert [(path, rule) for path, rule, _ in problems] == [
        ("services.web.alerts.0.labels.team", "yaml-duplicate-key")
    ]
    assert "line 5, column 29" in problems[0][2]
    assert "repeats line 5, column 18" in problems[0][2]


def test_quoting_does_not_hide_a_repeated_key() -> None:
    assert [rule for _, rule, _ in problems_of("a: 1\n'a': 2\n")] == ["yaml-duplicate-key"]


def test_a_yes_key_is_read_as_a_boolean_and_must_be_quoted() -> None:
    assert problems_of("hosts:\n  yes: {}\n") == [
        (
            "hosts.yes",
            "yaml-key-type",
            "fleet.yaml line 2, column 3: the key yes is read as a boolean, not a string;"
            ' quote it: "yes"',
        )
    ]


@pytest.mark.parametrize(
    ("key", "kind"),
    [
        ("on", "a boolean"),
        ("No", "a boolean"),
        ("123", "a number"),
        ("0x1f", "a number"),
        ("1.5", "a number"),
        ("~", "null"),
        ("2026-01-01", "a date"),
    ],
)
def test_keys_yaml_1_1_reads_as_another_type_are_named(key: str, kind: str) -> None:
    [(path, rule, message)] = problems_of(f"externals:\n  {key}: {{title: x}}\n")
    assert (path, rule) == (f"externals.{key}", "yaml-key-type")
    assert f"the key {key} is read as {kind}, not a string" in message


def test_a_mapping_used_as_a_key_is_refused() -> None:
    [(path, rule, message)] = problems_of("hosts:\n  ? [a, b]\n  : {}\n")
    assert (path, rule) == ("hosts", "yaml-key-type")
    assert message == "fleet.yaml line 2, column 5: a key is a sequence, not a string"


def test_quoted_keys_are_strings() -> None:
    text = 'hosts:\n  "yes": {}\n  "123": {}\n'
    assert load_strict(text, "x") == {"hosts": {"yes": {}, "123": {}}}


def test_numbers_and_booleans_as_values_stay_as_they_are() -> None:
    assert load_strict("a: yes\nb: 123\nc: [on, 1.5]\n", "x") == {
        "a": True,
        "b": 123,
        "c": [True, 1.5],
    }


def test_without_key_types_only_repeats_count() -> None:
    text = "responses:\n  200: {description: ok}\n  404: {description: no}\n"
    assert load_strict(text, "x", key_types=False) == yaml.safe_load(text)
    assert [rule for _, rule, _ in problems_of("a:\n  200: 1\n  200: 2\n", False)] == [
        "yaml-duplicate-key"
    ]


def test_every_problem_of_a_file_is_reported_at_once() -> None:
    text = "a: 1\na: 2\nyes: 3\nb: {c: 1, c: 2}\n"
    assert [(path, rule) for path, rule, _ in problems_of(text)] == [
        ("a", "yaml-duplicate-key"),
        ("yes", "yaml-key-type"),
        ("b.c", "yaml-duplicate-key"),
    ]


def test_a_merge_key_may_be_overridden() -> None:
    text = "base: &base {a: 1, b: 2}\nhost:\n  <<: *base\n  a: 3\n"
    assert load_strict(text, "x") == yaml.safe_load(text)


def test_a_problem_inside_an_anchor_is_reported_once() -> None:
    text = "base: &base {a: 1, a: 2}\nfirst: *base\nsecond: {<<: *base}\n"
    assert [path for path, _, _ in problems_of(text)] == ["base.a"]


def test_a_recursive_alias_does_not_loop() -> None:
    data = load_strict("a: &loop [*loop]\n", "x")
    assert data["a"][0] is data["a"]


def test_an_empty_document_is_none() -> None:
    assert load_strict("", "x") is None


def test_the_example_manifest_loads_as_before() -> None:
    assert load_manifest(EXAMPLE).raw == parsed_example()


def test_every_builtin_module_spec_is_strict() -> None:
    for spec in sorted(MODULES_DIR.glob("*/module.yaml")):
        text = spec.read_text(encoding="utf-8")
        assert load_strict(text, spec.parent.name) == yaml.safe_load(text)


def test_a_syntax_error_is_a_finding_and_not_a_traceback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert check_output(tmp_path, "project: x\nhosts: {a: [1\n", capsys) == [
        "error [yaml-syntax] <root>: fleet.yaml line 3, column 1: while parsing a flow"
        " sequence; expected ',' or ']', but got '<stream end>'",
        "fleet.yaml: 1 error(s)",
    ]


def test_an_impossible_unquoted_date_in_a_waiver_is_a_finding_and_not_a_traceback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    text = (
        "project: x\nservices:\n  web:\n    metrics:\n      none:\n"
        "        reason: soon\n        until: 2027-02-30\n"
    )
    assert check_output(tmp_path, text, capsys) == [
        "error [yaml-syntax] <root>: fleet.yaml line 7, column 16: 2027-02-30 cannot be read"
        " as a date: day is out of range for month",
        "fleet.yaml: 1 error(s)",
    ]


@pytest.mark.parametrize(
    ("value", "column"),
    [("2027-13-01", 4), ("2027-01-01 25:00:00", 4), ("[1, 2027-04-31]", 8)],
)
def test_a_value_its_type_cannot_hold_names_its_line_and_column(value: str, column: int) -> None:
    with pytest.raises(StrictYAMLError) as raised:
        read_strict(f"a: 1\nb: {value}\n", "fleet.yaml")
    [(path, rule, message)] = raised.value.problems
    assert (path, rule) == ("<root>", "yaml-syntax")
    assert message.startswith(f"fleet.yaml line 2, column {column}: 2027-")
    assert " cannot be read as a date: " in message


def test_a_collection_its_type_cannot_hold_is_named_by_its_kind() -> None:
    mark = yaml.Mark("fleet.yaml", 0, 3, 5, "", 0)
    node = yaml.MappingNode("tag:yaml.org,2002:map", [], mark, mark)
    error = unreadable(node, TypeError("unhashable"))
    assert syntax_problem("fleet.yaml", error) == (
        "<root>",
        "yaml-syntax",
        "fleet.yaml line 4, column 6: a mapping cannot be read: unhashable",
    )


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("!!timestamp soon", "soon cannot be read as a date: 'NoneType' object has no attribute"),
        ("!!bool maybe", "maybe cannot be read as a boolean: 'maybe'"),
        ('!!int ""', " cannot be read as a number: string index out of range"),
        ("!!float abc", "abc cannot be read as a number: could not convert string to float"),
    ],
)
def test_an_explicit_tag_its_value_cannot_hold_is_a_finding(value: str, message: str) -> None:
    with pytest.raises(StrictYAMLError) as raised:
        read_strict(f"a: 1\nb: {value}\n", "fleet.yaml")
    [(path, rule, found)] = raised.value.problems
    assert (path, rule) == ("<root>", "yaml-syntax")
    assert found.startswith("fleet.yaml line 2, column 4: ")
    assert message in found


def test_an_impossible_date_as_a_key_is_a_finding_too() -> None:
    with pytest.raises(StrictYAMLError) as raised:
        read_strict("a: 1\n2027-02-30: x\n", "fleet.yaml")
    assert [(rule, message) for _, rule, message in raised.value.problems] == [
        (
            "yaml-syntax",
            "fleet.yaml line 2, column 1: 2027-02-30 cannot be read as a date:"
            " day is out of range for month",
        )
    ]


def test_a_local_module_spec_with_an_impossible_date_is_a_finding_with_its_position(
    data: dict[str, Any], tmp_path: Path
) -> None:
    root = tmp_path / "modules"
    shutil.copytree(FIXTURE, root)
    spec = root / "queue" / "module.yaml"
    spec.write_text(spec.read_text(encoding="utf-8") + "released: 2027-02-30\n", encoding="utf-8")
    findings = check_manifest(manifest_from(local_queue(data, root)), load_modules())
    lines = len(spec.read_text(encoding="utf-8").splitlines())
    assert [(finding.path, finding.rule, finding.message) for finding in findings] == [
        (
            "module_paths.0.queue",
            "module-spec",
            f"queue/module.yaml line {lines}, column 11: 2027-02-30 cannot be read as a date:"
            " day is out of range for month",
        )
    ]


def test_a_syntax_error_without_a_mark_still_names_the_file() -> None:
    error = yaml.YAMLError("broken")
    assert syntax_problem("fleet.yaml", error) == ("<root>", "yaml-syntax", "fleet.yaml: broken")


def test_read_strict_keeps_strict_problems() -> None:
    with pytest.raises(StrictYAMLError) as raised:
        read_strict("a: 1\na: 2\n", "fleet.yaml")
    assert raised.value.source == "fleet.yaml"
    assert [rule for _, rule, _ in raised.value.problems] == ["yaml-duplicate-key"]
    assert str(raised.value).startswith("fleet.yaml line 2, column 1: the key 'a'")


def test_other_commands_report_the_same_findings(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "fleet.yaml"
    path.write_text("project: x\nproject: y\n", encoding="utf-8")
    assert main(["render", str(path), "-o", str(tmp_path / "out")]) == 1
    assert capsys.readouterr().err.startswith("error [yaml-duplicate-key] project: ")
    assert not (tmp_path / "out").exists()


def test_nested_prefixes_problem_paths() -> None:
    problems = [("<root>", "r", "m"), ("a.b", "r", "m")]
    assert nested("module_paths.0.q", problems) == [
        ("module_paths.0.q", "r", "m"),
        ("module_paths.0.q.a.b", "r", "m"),
    ]


def local_queue(data: dict[str, Any], root: Path) -> dict[str, Any]:
    data["module_paths"] = [str(root)]
    data["hosts"]["edge"]["modules"]["acme-queue"] = {}
    return data


def test_a_local_module_spec_with_a_repeated_key_is_a_finding(
    data: dict[str, Any], tmp_path: Path
) -> None:
    root = tmp_path / "modules"
    shutil.copytree(FIXTURE, root)
    spec = root / "queue" / "module.yaml"
    spec.write_text(spec.read_text(encoding="utf-8") + "dashboards: []\n", encoding="utf-8")
    assert rules_hit(local_queue(data, root)) == {"yaml-duplicate-key"}


def test_a_local_module_spec_with_a_boolean_key_is_a_finding(
    data: dict[str, Any], tmp_path: Path
) -> None:
    root = tmp_path / "modules"
    shutil.copytree(FIXTURE, root)
    spec = root / "queue" / "module.yaml"
    text = spec.read_text(encoding="utf-8").replace("  backlog_max: 1000\n", "  on: 1000\n", 1)
    spec.write_text(text, encoding="utf-8")
    assert rules_hit(local_queue(data, root)) == {"yaml-key-type"}


def compose_findings(data: dict[str, Any], tmp_path: Path, tail: str) -> list[tuple[str, str]]:
    shutil.copytree(
        EXAMPLE.parent, tmp_path, dirs_exist_ok=True, ignore=shutil.ignore_patterns("rendered")
    )
    compose = tmp_path / "apps" / "web.compose.yaml"
    compose.write_text(compose.read_text(encoding="utf-8") + tail, encoding="utf-8")
    findings = check_manifest(Manifest(data, tmp_path), load_modules())
    assert all(finding.message.startswith("apps/web.compose.yaml line ") for finding in findings)
    return [(finding.path, finding.rule) for finding in findings]


def test_a_compose_file_with_a_repeated_key_is_a_finding(
    data: dict[str, Any], tmp_path: Path
) -> None:
    assert compose_findings(data, tmp_path, "services: {}\n") == [
        ("hosts.central.compose", "yaml-duplicate-key")
    ]


def test_a_compose_file_that_does_not_parse_is_a_finding(
    data: dict[str, Any], tmp_path: Path
) -> None:
    assert compose_findings(data, tmp_path, "x: [1\n") == [("hosts.central.compose", "yaml-syntax")]


def test_a_compose_file_may_have_yaml_1_1_keys(data: dict[str, Any], tmp_path: Path) -> None:
    assert compose_findings(data, tmp_path, "x-flags: {on: 1, 8080: 2}\n") == []


def test_an_openapi_spec_with_a_repeated_path_is_refused(tmp_path: Path) -> None:
    spec = tmp_path / "shop.yaml"
    spec.write_text(
        "paths:\n  /a:\n    get: {operationId: one}\n  /a:\n    get: {operationId: two}\n",
        encoding="utf-8",
    )
    operations, error = openapi_operations(Manifest({"hosts": {}}, tmp_path), "shop.yaml")
    assert operations == {}
    assert error == (
        "shop.yaml line 4, column 3: the key '/a' repeats line 2, column 3;"
        " YAML keeps only the last one"
    )


def test_an_openapi_spec_may_keep_numeric_response_codes(tmp_path: Path) -> None:
    spec = tmp_path / "shop.yaml"
    spec.write_text(
        "paths:\n  /a:\n    get:\n      operationId: one\n      responses: {200: {}}\n",
        encoding="utf-8",
    )
    assert openapi_operations(Manifest({"hosts": {}}, tmp_path), "shop.yaml") == (
        {"one": "get"},
        None,
    )
