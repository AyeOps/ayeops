from __future__ import annotations

import re
from dataclasses import dataclass

OPENERS = {"(": ")", "[": "]", "{": "}"}
QUOTES = "\"'`"
COMPARISON = re.compile(r"==|!=|>=|<=|>|<")
BOOL = re.compile(r"\s*bool(?![A-Za-z0-9_:])")
OR = re.compile(r"(?<![A-Za-z0-9_:])or(?![A-Za-z0-9_:])")


def top_level(expr: str) -> set[int]:
    found: set[int] = set()
    stack: list[str] = []
    index = 0
    while index < len(expr):
        char = expr[index]
        if char in QUOTES:
            index += 1
            while index < len(expr) and expr[index] != char:
                index += 2 if expr[index] == "\\" else 1
        elif char in OPENERS:
            stack.append(OPENERS[char])
        elif stack and char == stack[-1]:
            stack.pop()
        elif not stack:
            found.add(index)
        index += 1
    return found


def unwrap(expr: str) -> str:
    text = expr.strip()
    while text.startswith("(") and text.endswith(")") and not top_level(text):
        text = text[1:-1].strip()
    return text


def split_or(expr: str) -> list[str]:
    outside = top_level(expr)
    cuts = [m for m in OR.finditer(expr) if {m.start(), m.start() + 1} <= outside]
    edges = [0, *(edge for m in cuts for edge in m.span()), len(expr)]
    return [expr[edges[i] : edges[i + 1]] for i in range(0, len(edges), 2)]


def filtering_comparisons(expr: str) -> list[str]:
    outside = top_level(expr)
    return [
        match.group()
        for match in COMPARISON.finditer(expr)
        if set(range(match.start(), match.end())) <= outside and not BOOL.match(expr, match.end())
    ]


def may_be_empty(expr: str) -> bool:
    text = unwrap(expr)
    parts = split_or(text)
    if len(parts) > 1:
        return all(may_be_empty(part) for part in parts)
    return bool(filtering_comparisons(text))


TOKEN = re.compile(
    r"\s*(?:"
    r"(?P<string>\"(?:[^\"\\]|\\.)*\"|'(?:[^'\\]|\\.)*'|`[^`]*`)"
    r"|(?P<duration>(?:[0-9]+(?:ms|[smhdwy]))+(?![A-Za-z0-9_]))"
    r"|(?P<number>(?:0x[0-9a-fA-F]+|(?:[0-9]+[.]?[0-9]*|[.][0-9]+)(?:[eE][-+]?[0-9]+)?)"
    r"(?![A-Za-z0-9_:.]))"
    r"|(?P<name>[A-Za-z_][A-Za-z0-9_:]*)"
    r"|(?P<symbol>=~|!~|==|!=|>=|<=|[-+*/%^<>=@,:(){}\[\]])"
    r")"
)
END = ("end", "")
BALANCE = {"(": 1, ")": -1}
STRAY = frozenset({"{", "}", "]"})
GROUPING = frozenset({"by", "without", "on", "ignoring", "group_left", "group_right"})
WORDS = frozenset({"and", "or", "unless", "bool", "offset", "atan2", "Inf", "NaN"})
MATCH_OPERATORS = frozenset({"=", "!=", "=~", "!~"})
AGGREGATIONS = frozenset(
    {
        "avg",
        "bottomk",
        "count",
        "count_values",
        "group",
        "limitk",
        "max",
        "min",
        "quantile",
        "stddev",
        "stdvar",
        "sum",
        "topk",
    }
)
FUNCTIONS = AGGREGATIONS | frozenset(
    {
        "abs",
        "absent",
        "absent_over_time",
        "avg_over_time",
        "ceil",
        "changes",
        "clamp",
        "clamp_max",
        "clamp_min",
        "count_over_time",
        "day_of_month",
        "day_of_week",
        "day_of_year",
        "days_in_month",
        "delta",
        "deriv",
        "end",
        "exp",
        "floor",
        "histogram_quantile",
        "hour",
        "idelta",
        "increase",
        "irate",
        "label_join",
        "label_replace",
        "lag",
        "last_over_time",
        "ln",
        "log10",
        "log2",
        "max_over_time",
        "median_over_time",
        "min_over_time",
        "minute",
        "month",
        "predict_linear",
        "present_over_time",
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
        "stddev_over_time",
        "stdvar_over_time",
        "sum_over_time",
        "time",
        "timestamp",
        "vector",
        "year",
    }
)

Token = tuple[str, str]
Matcher = tuple[str, str, str]


class PromQLError(ValueError):
    pass


@dataclass(frozen=True)
class Selector:
    metric: str
    matchers: tuple[Matcher, ...] = ()


@dataclass(frozen=True)
class Query:
    selectors: tuple[Selector, ...]
    grouping: frozenset[str]

    @property
    def metrics(self) -> frozenset[str]:
        return frozenset(selector.metric for selector in self.selectors)

    @property
    def labels(self) -> frozenset[str]:
        matched = {label for selector in self.selectors for label, _, _ in selector.matchers}
        return self.grouping | matched


def tokens(expr: str) -> list[Token]:
    text = expr.rstrip()
    found = []
    position = 0
    while position < len(text):
        match = TOKEN.match(text, position)
        if match is None:
            raise PromQLError(f"cannot read {text[position:].lstrip()!r} in {expr!r}")
        kind = str(match.lastgroup)
        found.append((kind, match.group(kind)))
        position = match.end()
    return found


def range_end(items: list[Token], index: int, expr: str) -> int:
    while items[index][1] != "]":
        if items[index][0] != "duration" and items[index][1] != ":":
            raise PromQLError(f"cannot read the range in {expr!r}")
        index += 1
    return index


def label_list(items: list[Token], index: int, expr: str) -> tuple[list[str], int]:
    labels = []
    while items[index][1] != ")":
        kind, text = items[index]
        separator = items[index + 1][1]
        if kind != "name" or separator not in (",", ")"):
            raise PromQLError(f"cannot read the label list in {expr!r}")
        labels.append(text)
        index += 2 if separator == "," else 1
    return labels, index


def matcher_list(items: list[Token], index: int, expr: str) -> tuple[tuple[Matcher, ...], int]:
    found = []
    while items[index][1] != "}":
        (kind, label), (_, operator), (quoted, value), (_, separator) = items[index : index + 4]
        if (
            kind != "name"
            or operator not in MATCH_OPERATORS
            or quoted != "string"
            or separator not in (",", "}")
        ):
            raise PromQLError(f"cannot read the matchers in {expr!r}")
        found.append((label, operator, value[1:-1]))
        index += 4 if separator == "," else 3
    return tuple(found), index


def parse(expr: str) -> Query:
    items = [*tokens(expr), END, END, END, END]
    selectors = []
    grouping: set[str] = set()
    depth = 0
    index = 0
    while items[index] != END:
        kind, text = items[index]
        after = items[index + 1][1]
        if kind != "name":
            depth += BALANCE.get(text, 0)
            if depth < 0 or text in STRAY:
                raise PromQLError(f"unexpected {text!r} in {expr!r}")
            if text == "[":
                index = range_end(items, index + 1, expr)
        elif text in GROUPING:
            if after == "(":
                labels, index = label_list(items, index + 2, expr)
                grouping.update(labels)
            elif not text.startswith("group_"):
                raise PromQLError(f"{text} needs a label list in {expr!r}")
        elif after == "(" and text not in WORDS:
            if text not in FUNCTIONS:
                raise PromQLError(f"unknown function {text!r} in {expr!r}")
        elif text not in WORDS and not (after in ("by", "without") and text in AGGREGATIONS):
            matchers: tuple[Matcher, ...] = ()
            if after == "{":
                matchers, index = matcher_list(items, index + 2, expr)
            selectors.append(Selector(text, matchers))
        index += 1
    if depth:
        raise PromQLError(f"unbalanced parentheses in {expr!r}")
    return Query(tuple(selectors), frozenset(grouping))
