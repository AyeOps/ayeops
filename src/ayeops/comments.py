from __future__ import annotations

import io
import re
import sys
import tokenize
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

HASH_SUFFIXES = frozenset({".yaml", ".yml", ".toml", ".sh", ".j2", ".cfg", ".ini", ".conf", ".env"})
HASH_NAMES = frozenset(
    {"Dockerfile", ".gitignore", ".gitattributes", ".dockerignore", ".env.example", "Makefile"}
)
SLASH_SUFFIXES = frozenset(
    {
        ".alloy",
        ".json",
        ".json5",
        ".jsonc",
        ".js",
        ".mjs",
        ".cjs",
        ".ts",
        ".mts",
        ".cts",
        ".tsx",
        ".css",
    }
)
DASH_SUFFIXES = frozenset({".sql"})
PINNED_ACTION = re.compile(
    r"^\s*(?:-\s+)?uses:\s*[\w.-]+/[\w./-]+@[0-9a-f]{40}\s+#\s?v\d+(?:\.\d+){0,2}$"
)
SKIP_DIRS = frozenset(
    {
        ".git",
        ".venv",
        "node_modules",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "dist",
        "mutants",
    }
)


@dataclass(frozen=True, order=True)
class Comment:
    path: str
    line: int
    text: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.text}"


def python_comments(source: str) -> Iterator[tuple[int, str]]:
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type != tokenize.COMMENT:
            continue
        if token.start[0] == 1 and token.string.startswith("#!"):
            continue
        yield token.start[0], token.string


def _hash_index(line: str) -> int | None:
    quote: str | None = None
    escaped = False
    for index, char in enumerate(line):
        if quote == '"':
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quote = None
            continue
        if quote == "'":
            if char == "'":
                quote = None
            continue
        if char in "\"'" and (index == 0 or not line[index - 1].isalnum()):
            quote = char
        elif char == "#" and (index == 0 or line[index - 1] in " \t"):
            return index
    return None


def _is_allowed_directive(number: int, line: str, dockerfile: bool) -> bool:
    if number == 1 and line.startswith("#!"):
        return True
    if PINNED_ACTION.match(line):
        return True
    compact = line.replace(" ", "").lower()
    return dockerfile and compact.startswith("#syntax=")


def hash_comments(
    source: str, dockerfile: bool = False, jinja: bool = False
) -> Iterator[tuple[int, str]]:
    for number, line in enumerate(source.splitlines(), start=1):
        if _is_allowed_directive(number, line, dockerfile):
            continue
        index = _hash_index(line)
        if index is not None:
            yield number, line[index:].strip()
        elif jinja and "{#" in line:
            yield number, line[line.index("{#") :].strip()


def slash_comments(source: str) -> Iterator[tuple[int, str]]:
    for number, line in enumerate(source.splitlines(), start=1):
        quote: str | None = None
        escaped = False
        for index, char in enumerate(line):
            if quote is not None:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == quote:
                    quote = None
                continue
            if char in "\"'`":
                quote = char
            elif char == "/" and line[index + 1 : index + 2] in ("/", "*"):
                yield number, line[index:].strip()
                break


def dash_comments(source: str) -> Iterator[tuple[int, str]]:
    for number, line in enumerate(source.splitlines(), start=1):
        quote: str | None = None
        for index, char in enumerate(line):
            if quote is not None:
                if char == quote:
                    quote = None
                continue
            if char in "\"'":
                quote = char
            elif line[index : index + 2] in ("--", "/*"):
                yield number, line[index:].strip()
                break


def comments_in(path: Path, source: str) -> Iterator[tuple[int, str]]:
    name = path.name
    if path.suffix == ".py":
        yield from python_comments(source)
    elif name == "Dockerfile" or name.endswith(".Dockerfile"):
        yield from hash_comments(source, dockerfile=True)
    elif path.suffix in HASH_SUFFIXES or name in HASH_NAMES:
        yield from hash_comments(source, jinja=path.suffix == ".j2")
    elif path.suffix in SLASH_SUFFIXES:
        yield from slash_comments(source)
    elif path.suffix in DASH_SUFFIXES:
        yield from dash_comments(source)


def iter_files(roots: Iterable[Path]) -> Iterator[Path]:
    for root in roots:
        if root.is_file():
            yield root
            continue
        for path in sorted(root.rglob("*")):
            if path.is_file() and not SKIP_DIRS.intersection(path.relative_to(root).parts):
                yield path


def find_comments(roots: Iterable[Path]) -> list[Comment]:
    found: list[Comment] = []
    for path in iter_files(roots):
        try:
            source = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for line, text in comments_in(path, source):
            found.append(Comment(path.as_posix(), line, text))
    return sorted(found)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    found = find_comments([Path(arg) for arg in args or ["."]])
    for comment in found:
        print(comment)
    if found:
        print(f"{len(found)} comment(s) found; explanations belong in docs/ and test names.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
