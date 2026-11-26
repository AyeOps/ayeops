from __future__ import annotations

import pytest
import yaml

from ayeops.strict import (
    EnvRef,
    StrictValidator,
    compose_literal,
    compose_safe,
    ecma_pattern,
    matches,
)


@pytest.mark.parametrize(
    ("pattern", "expected"),
    [
        ("^a$", r"^a\Z"),
        (r"^a\$", r"^a\$"),
        ("^[$]$", r"^[$]\Z"),
        (r"^[a\]$]x$", r"^[a\]$]x\Z"),
        ("a$|b$", r"a\Z|b\Z"),
        ("[x]$y", r"[x]\Zy"),
        ("", ""),
        ("$", r"\Z"),
        ("[$]", "[$]"),
    ],
)
def test_only_an_unescaped_dollar_outside_a_class_becomes_end_of_input(
    pattern: str, expected: str
) -> None:
    assert ecma_pattern(pattern) == expected


def test_a_trailing_newline_no_longer_ends_a_match() -> None:
    assert matches("^ab$", "ab")
    assert not matches("^ab$", "ab\n")
    assert matches("b", "abc")
    assert not matches("^b", "abc")


def test_the_strict_validator_reports_the_value_and_the_pattern() -> None:
    errors = list(StrictValidator({"pattern": "^a$"}).iter_errors("a\n"))
    assert [error.message for error in errors] == ["'a\\n' does not match '^a$'"]


def test_the_strict_validator_ignores_what_is_not_a_string() -> None:
    assert StrictValidator({"pattern": "^a$"}).is_valid(5)
    assert StrictValidator({"pattern": "^a$"}).is_valid("a")


@pytest.mark.parametrize(
    ("value", "expected"),
    [("a$b", "a$$b"), ("${X}", "$${X}"), ("$$", "$$$$"), ("plain", "plain"), (7, "7")],
)
def test_a_dollar_is_doubled_so_compose_reads_it_literally(value: object, expected: str) -> None:
    assert compose_literal(value) == expected


def test_compose_safe_doubles_every_string_but_our_own_references() -> None:
    ref = EnvRef("${AYE_X}")
    context = {"a": "x$y", "b": [ref, "$z", 3], "c": ("$", None), "d": {"e": "${HOME}"}}
    safe = compose_safe(context)
    assert safe == {
        "a": "x$$y",
        "b": ["${AYE_X}", "$$z", 3],
        "c": ("$$", None),
        "d": {"e": "$${HOME}"},
    }
    assert safe["b"][0] is ref
    assert context["a"] == "x$y"


def test_an_env_reference_dumps_as_a_plain_yaml_string() -> None:
    assert yaml.safe_dump({"k": EnvRef("${X}")}) == "k: ${X}\n"
