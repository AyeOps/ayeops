from __future__ import annotations

import pytest

from ayeops.promql import (
    PromQLError,
    Query,
    Selector,
    filtering_comparisons,
    may_be_empty,
    parse,
    split_or,
    tokens,
    top_level,
    unwrap,
)


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


def test_tokens_split_strings_durations_numbers_names_and_symbols() -> None:
    assert tokens('aye:x{a="b\\"c",d=~\'e\'}[1h30m] offset 5ms > bool 1e-9') == [
        ("name", "aye:x"),
        ("symbol", "{"),
        ("name", "a"),
        ("symbol", "="),
        ("string", '"b\\"c"'),
        ("symbol", ","),
        ("name", "d"),
        ("symbol", "=~"),
        ("string", "'e'"),
        ("symbol", "}"),
        ("symbol", "["),
        ("duration", "1h30m"),
        ("symbol", "]"),
        ("name", "offset"),
        ("duration", "5ms"),
        ("symbol", ">"),
        ("name", "bool"),
        ("number", "1e-9"),
    ]


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("`a\\`", "string"),
        ("'a\\'b'", "string"),
        ("0x1F", "number"),
        (".5", "number"),
        ("1.", "number"),
        ("2E+3", "number"),
        ("86400", "number"),
        ("1y", "duration"),
        ("2w", "duration"),
        ("_a:b_1", "name"),
    ],
)
def test_a_token_is_read_whole(text: str, kind: str) -> None:
    assert tokens(f" {text} ") == [(kind, text)]


def test_every_operator_is_one_symbol() -> None:
    symbols = [
        "=~",
        "!~",
        "==",
        "!=",
        ">=",
        "<=",
        "-",
        "+",
        "*",
        "/",
        "%",
        "^",
        "<",
        ">",
        "=",
        "@",
        ",",
        ":",
        "(",
        ")",
        "{",
        "}",
        "[",
        "]",
    ]
    assert tokens(" ".join(symbols)) == [("symbol", symbol) for symbol in symbols]
    assert tokens("a>=b") == [("name", "a"), ("symbol", ">="), ("name", "b")]


@pytest.mark.parametrize(
    ("expr", "rest"),
    [
        ("a $x", "$x"),
        ("a 5x", "5x"),
        ("a 1.5h", "1.5h"),
        ("rate(x[5mm])", "5mm])"),
        ('x{a="b}', '"b}'),
    ],
)
def test_tokens_refuse_what_they_cannot_read(expr: str, rest: str) -> None:
    with pytest.raises(PromQLError) as error:
        tokens(expr)
    assert str(error.value) == f"cannot read {rest!r} in {expr!r}"


def test_parse_finds_metrics_matchers_and_grouping_labels() -> None:
    query = parse(
        'sum by (host, project) (rate(a_total{job="node", mode!="idle",}[5m]))'
        ' / on (host) group_left (zone) b{c=~"d|e", f!~"g"} > bool 0.5'
    )
    assert query.selectors == (
        Selector("a_total", (("job", "=", "node"), ("mode", "!=", "idle"))),
        Selector("b", (("c", "=~", "d|e"), ("f", "!~", "g"))),
    )
    assert query.grouping == {"host", "project", "zone"}
    assert query.metrics == {"a_total", "b"}
    assert query.labels == {"host", "project", "zone", "job", "mode", "c", "f"}


def test_parse_keeps_every_selector_of_a_repeated_metric() -> None:
    query = parse('x{a="1"} + x')
    assert query.selectors == (Selector("x", (("a", "=", "1"),)), Selector("x"))
    assert query.metrics == {"x"}


def test_parse_reads_the_value_between_the_quotes_as_written() -> None:
    assert parse('x{a="b\\"c"}').selectors == (Selector("x", (("a", "=", 'b\\"c'),)),)
    assert parse("x{a=''}").selectors == (Selector("x", (("a", "=", ""),)),)
    assert parse("x{}").selectors == (Selector("x"),)


@pytest.mark.parametrize(
    ("expr", "grouping"),
    [
        ("sum by (a) (x)", {"a"}),
        ("sum by (a,) (x)", {"a"}),
        ("sum without (a, b) (x)", {"a", "b"}),
        ("count_values without (a) (x)", {"a"}),
        ("sum(x) by (a)", {"a"}),
        ("x + ignoring (a) x", {"a"}),
        ("x * on (a) group_right (b) x", {"a", "b"}),
        ("x * on (a) group_left x", {"a"}),
        ("x * on (h) group_right () (0 * x)", {"h"}),
        ("x * on () x", set()),
        ("sum by (group, on) (x)", {"group", "on"}),
    ],
)
def test_parse_reads_every_label_list(expr: str, grouping: set[str]) -> None:
    query = parse(expr)
    assert query.metrics == {"x"}
    assert query.grouping == grouping


@pytest.mark.parametrize(
    "expr",
    [
        "x and y",
        "x or y",
        "x unless y",
        "x > bool y",
        "x atan2 y",
        "x offset 1d + y",
        "x @ start() + y @ end()",
        "x > Inf or y < NaN",
        "x or (y)",
        "x and (y)",
        "x > bool (y)",
        "x[1h:5m] + y[5m:]",
        "-x + 2 * y % 3 ^ 4 - 0x10",
        'label_replace(x, "a", "$1", "b", "(.*)") + y',
        "sum(x) + max(y)",
    ],
)
def test_parse_skips_keywords_functions_ranges_and_numbers(expr: str) -> None:
    assert parse(expr).metrics == {"x", "y"}


def test_an_aggregation_name_without_a_grouping_after_it_is_a_metric() -> None:
    assert parse("sum").metrics == {"sum"}
    assert parse("max + group").metrics == {"max", "group"}
    assert parse("x by (a)").metrics == {"x"}


def test_parse_of_nothing_is_an_empty_query() -> None:
    assert parse("") == Query((), frozenset())
    assert parse("time() - 5").metrics == set()


@pytest.mark.parametrize(
    "name",
    [
        "abs",
        "absent",
        "absent_over_time",
        "avg",
        "avg_over_time",
        "bottomk",
        "ceil",
        "changes",
        "clamp",
        "clamp_max",
        "clamp_min",
        "count",
        "count_over_time",
        "count_values",
        "day_of_month",
        "day_of_week",
        "day_of_year",
        "days_in_month",
        "delta",
        "deriv",
        "end",
        "exp",
        "floor",
        "group",
        "histogram_quantile",
        "hour",
        "idelta",
        "increase",
        "irate",
        "label_join",
        "label_replace",
        "lag",
        "last_over_time",
        "limitk",
        "ln",
        "log10",
        "log2",
        "max",
        "max_over_time",
        "median_over_time",
        "min",
        "min_over_time",
        "minute",
        "month",
        "predict_linear",
        "present_over_time",
        "quantile",
        "quantile_over_time",
        "rate",
        "resets",
        "round",
        "scalar",
        "sgn",
        "sort",
        "sort_desc",
        "sqrt",
        "start",
        "stddev",
        "stddev_over_time",
        "stdvar",
        "stdvar_over_time",
        "sum",
        "sum_over_time",
        "time",
        "timestamp",
        "topk",
        "vector",
        "year",
    ],
)
def test_parse_knows_the_function(name: str) -> None:
    assert parse(f"{name} (x)").metrics == {"x"}


@pytest.mark.parametrize(
    ("expr", "message"),
    [
        ("rates(x[5m])", "unknown function 'rates'"),
        ("sum by x", "by needs a label list"),
        ("x + on x", "on needs a label list"),
        ("x + ignoring x", "ignoring needs a label list"),
        ("sum without x", "without needs a label list"),
        ('{a="b"}', "unexpected '{'"),
        ("x }", "unexpected '}'"),
        ("x ]", "unexpected ']'"),
        ("x)", "unexpected ')'"),
        ("(x))", "unexpected ')'"),
        ("(x", "unbalanced parentheses"),
        ("((x)", "unbalanced parentheses"),
        ("x[5]", "cannot read the range"),
        ("x[y]", "cannot read the range"),
        ("x[5m", "cannot read the range"),
        ("sum by (a b) (x)", "cannot read the label list"),
        ("sum by (a,,b) (x)", "cannot read the label list"),
        ('sum by ("a") (x)', "cannot read the label list"),
        ("sum by (a", "cannot read the label list"),
        ("x{a}", "cannot read the matchers"),
        ('x{"a"="b"}', "cannot read the matchers"),
        ("x{a=b}", "cannot read the matchers"),
        ('x{a=="b"}', "cannot read the matchers"),
        ('x{a="b" c="d"}', "cannot read the matchers"),
        ('x{a="b"', "cannot read the matchers"),
        ('x{a="b",', "cannot read the matchers"),
    ],
)
def test_parse_refuses_what_it_cannot_read(expr: str, message: str) -> None:
    with pytest.raises(PromQLError) as error:
        parse(expr)
    assert str(error.value) == f"{message} in {expr!r}"


def test_a_matcher_list_hands_back_the_next_token() -> None:
    assert parse('sum(x{a="b"})').selectors == (Selector("x", (("a", "=", "b"),)),)
    assert parse('x{a="b",c="d"}[5m]').selectors == (
        Selector("x", (("a", "=", "b"), ("c", "=", "d"))),
    )


def test_a_promql_error_is_a_value_error() -> None:
    assert issubclass(PromQLError, ValueError)
