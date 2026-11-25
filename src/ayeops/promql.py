from __future__ import annotations

import re

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
