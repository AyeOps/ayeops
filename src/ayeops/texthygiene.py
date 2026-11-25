from __future__ import annotations

import subprocess
import sys
import unicodedata
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ayeops.comments import iter_files

DELETE = "delete it"
PLAIN_SPACE = "use a plain space"
RETYPE = "retype the word in one alphabet"

BIDI = frozenset({0x061C, 0x200E, 0x200F, *range(0x202A, 0x202F), *range(0x2066, 0x206A)})
INVISIBLE = frozenset(
    {0x00AD, 0x034F, 0x180E, 0x200B, 0x200C, 0x200D, 0xFEFF, *range(0x2060, 0x2065)}
)
SPACES = frozenset({0x00A0, 0x1680, *range(0x2000, 0x200B), 0x202F, 0x205F, 0x3000})
TYPOGRAPHY = {
    0x2018: "'",
    0x2019: "'",
    0x201A: "'",
    0x201B: "'",
    0x201C: '"',
    0x201D: '"',
    0x201E: '"',
    0x201F: '"',
    0x2010: "-",
    0x2011: "-",
    0x2012: "-",
    0x2013: "-",
    0x2014: "-",
    0x2015: "-",
    0x2212: "-",
    0x2026: "...",
}
SEPARATORS = frozenset({0x2028, 0x2029})
LINE_BREAKS = frozenset({0x09, 0x0A, 0x0D})
ZWJ = chr(0x200D)
EMOJI_STYLE = chr(0xFE0F)
EMOJI_LEFT = frozenset({"So", "Sk"})
STRICT_SUFFIXES = frozenset(
    {".sh", ".bash", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".env", ".alloy"}
)
STRICT_NAMES = frozenset({"Dockerfile", "Makefile", ".env.example", ".gitignore", ".dockerignore"})


@dataclass(frozen=True, order=True)
class Hit:
    path: str
    line: int
    column: int
    point: int
    kind: str
    suggestion: str

    def __str__(self) -> str:
        return (
            f"{self.path}:{self.line}:{self.column}: U+{self.point:04X} {point_name(self.point)} "
            f"({self.kind}): {self.suggestion}"
        )


def point_name(point: int) -> str:
    return unicodedata.name(chr(point), "<" + unicodedata.category(chr(point)) + ">")


def classify(char: str) -> tuple[str, str] | None:
    point = ord(char)
    if point in BIDI:
        return "bidi", DELETE
    if point in SPACES:
        return "space", PLAIN_SPACE
    if point in INVISIBLE:
        return "invisible", DELETE
    if point in SEPARATORS:
        return "separator", "use a plain newline"
    if point in TYPOGRAPHY:
        return "typography", f"use {TYPOGRAPHY[point]}"
    category = unicodedata.category(char)
    if category == "Cc" and point not in LINE_BREAKS:
        return "control", DELETE
    if category == "Co":
        return "private-use", DELETE
    return None


def script(char: str) -> str:
    return unicodedata.name(char, "").split(" ")[0]


def words(line: str) -> Iterator[tuple[int, str]]:
    start = 0
    for index, char in enumerate(line + " "):
        escaped = index > 0 and line[index - 1] == "\\"
        if not char.isalpha() or escaped:
            if index > start:
                yield start, line[start:index]
            start = index + 1


def is_mixed_script(word: str) -> bool:
    scripts = {script(char) for char in word}
    return {"LATIN", "CYRILLIC"} <= scripts


def joins_emoji(line: str, index: int) -> bool:
    if line[index] != ZWJ or index == 0 or index + 1 >= len(line):
        return False
    before = line[index - 1]
    left = before == EMOJI_STYLE or unicodedata.category(before) in EMOJI_LEFT
    return left and unicodedata.category(line[index + 1]) == "So"


def line_hits(line: str, prose: bool) -> Iterator[tuple[int, int, str, str]]:
    for index, char in enumerate(line):
        found = classify(char)
        if found is None or (prose and found[0] == "typography") or joins_emoji(line, index):
            continue
        yield index, ord(char), found[0], found[1]
    for start, word in words(line):
        if is_mixed_script(word):
            first = word[0]
            yield start, ord(first), "mixed-script", f"{RETYPE}: {word}"


def is_prose(path: Path) -> bool:
    strict = path.suffix in STRICT_SUFFIXES or path.name in STRICT_NAMES
    return not (strict or path.name.endswith(".Dockerfile"))


def text_hits(path: str, source: str, prose: bool) -> list[Hit]:
    found = []
    for number, line in enumerate(source.split("\n"), start=1):
        for index, point, kind, suggestion in line_hits(line, prose):
            found.append(Hit(path, number, index + 1, point, kind, suggestion))
    return sorted(found)


def tracked_files(root: Path) -> list[Path]:
    if root.is_file():
        return [root]
    try:
        listed = subprocess.run(
            ["git", "ls-files", "-z"], cwd=root, capture_output=True, check=True, timeout=60
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return list(iter_files([root]))
    names = listed.decode("utf-8").split("\0")
    return [root / name for name in names if name and (root / name).is_file()]


def read_text(path: Path) -> str | None:
    try:
        source = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return None
    return None if "\0" in source else source


def find_hits(roots: Iterable[Path]) -> list[Hit]:
    found: list[Hit] = []
    for root in roots:
        for path in tracked_files(root):
            source = read_text(path)
            if source is not None:
                found += text_hits(path.as_posix(), source, is_prose(path))
    return sorted(found)


def value_hits(value: Any, path: str) -> Iterator[tuple[str, str]]:
    if isinstance(value, dict):
        for key, item in value.items():
            child = f"{path}.{key}" if path else str(key)
            yield from value_hits(str(key), child)
            yield from value_hits(item, child)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from value_hits(item, f"{path}[{index}]")
    elif isinstance(value, str):
        for _, point, kind, suggestion in line_hits(value, prose=True):
            yield path, f"U+{point:04X} {point_name(point)} ({kind}): {suggestion}"


def manifest_problems(data: dict[str, Any]) -> list[tuple[str, str, str]]:
    return [(path, "text-hygiene", message) for path, message in value_hits(data, "")]


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    found = find_hits([Path(arg) for arg in args or ["."]])
    for hit in found:
        print(hit)
    if found:
        print(
            f"{len(found)} character(s) a reader cannot see or tell apart; fix them as suggested."
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
