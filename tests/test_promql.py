from __future__ import annotations

import pytest

from ayeops.promql import filtering_comparisons, may_be_empty, split_or, top_level, unwrap


@pytest.mark.parametrize(
    "expr",
    [
        "time() - max(container_last_seen) > 120",
        "up == 0",
        "up != 1",
        "x >= 5",
        "x <= 5",
        "x < 5",
        'rate(http_requests_total{code!="200"}[5m]) > 0',
        "sum by (job) (rate(errors_total[5m])) > 0.1",
        "sum(rate(x[5m])) by (job) >= 3",
        "(x > 5)",
        "((x > 5))",
        "x > 5 and on(job) y",
        "x > 5 or y < 3",
        "(x > 5) or (y < 3)",
        "x > 5 unless y",
        'x{a="b"} > on(job) group_left y',
        "x > boolean_metric",
        "maX_total > 5",
    ],
)
def test_filtering_comparisons_may_return_nothing(expr: str) -> None:
    assert may_be_empty(expr)


@pytest.mark.parametrize(
    "expr",
    [
        "time() - max(container_last_seen) > bool 120",
        "up == bool 0",
        "x != bool 1",
        "x >= bool 5",
        "x <=bool 5",
        "x < bool on(job) y",
        'rate(http_requests_total{code!="200"}[5m])',
        'count(up{job=~"a|b"} == 0)',
        "sum by (job) (x > 5)",
        "1 - (up * (lag(up[1d]) < bool 180))",
        "x > 5 or vector(0)",
        "(x > 5) or on() vector(0)",
        'x{path="a > b"}',
        "x{path='a == b'}",
        'x{path="a \\" > b"}',
        "error_or_total",
        "x + y",
        "",
    ],
)
def test_bool_comparisons_and_plain_values_always_return_a_series(expr: str) -> None:
    assert not may_be_empty(expr)


def test_operators_are_reported_in_order_and_bool_ones_are_skipped() -> None:
    assert filtering_comparisons("a >= 1 and b == bool 2 and c < 3 and d != 4") == [">=", "<", "!="]
    assert filtering_comparisons("a == b") == ["=="]
    assert filtering_comparisons("a <= b") == ["<="]
    assert filtering_comparisons(">= 1") == [">="]
    assert filtering_comparisons(">") == [">"]
    assert filtering_comparisons('{a!="b"} <bool 1') == []


def test_top_level_skips_brackets_braces_and_strings() -> None:
    assert top_level('a{b="}"}[5m] (c) "d" e') == {0, 12, 16, 20, 21}
    assert top_level("(a)") == set()
    assert top_level("a") == {0}
    assert top_level('"" a') == {2, 3}
    assert top_level('"\\"" a') == {4, 5}
    assert top_level('"abc') == set()


def test_unwrap_removes_only_parens_around_the_whole_expression() -> None:
    assert unwrap(" ( (a > 5) ) ") == "a > 5"
    assert unwrap("(a) > (b)") == "(a) > (b)"
    assert unwrap("a") == "a"


def test_split_or_cuts_only_at_top_level_or_words() -> None:
    assert split_or("a or (b or c) or error_or") == ["a ", " (b or c) ", " error_or"]
    assert split_or("vendor or x") == ["vendor ", " x"]
    assert split_or("a") == ["a"]
    assert split_or("(a)or b") == ["(a)", " b"]
    assert split_or("a or(b)") == ["a ", "(b)"]
    assert split_or("or") == ["", ""]
