from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
DOCS = ROOT / "docs"
MODULES_DOC = DOCS / "modules.md"
MODULE_NAMES = sorted(
    path.parent.name for path in (ROOT / "src/ayeops/modules").glob("*/module.yaml")
)
SKIPPED_DIRS = {
    ".git",
    ".venv",
    ".claude",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "__pycache__",
    "node_modules",
    "mutants",
    "dist",
    "build",
}
LINKING_SUFFIXES = {".md", ".py", ".yaml", ".yml", ".j2", ".json", ".ts", ".toml", ".html", ".txt"}
MODULES_LINK = re.compile(r"modules\.md#([A-Za-z0-9_-]+)")


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def outside_fences(text: str) -> list[str]:
    lines: list[str] = []
    fenced = False
    for line in text.splitlines():
        if line.startswith("```"):
            fenced = not fenced
            continue
        if not fenced:
            lines.append(line)
    return lines


def slug(heading: str) -> str:
    text = heading.strip().lower()
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


def anchors(text: str) -> set[str]:
    seen: Counter[str] = Counter()
    found: set[str] = set()
    for line in outside_fences(text):
        match = re.match(r"#{1,6} (.+)$", line)
        if not match:
            continue
        base = slug(match.group(1))
        found.add(base if seen[base] == 0 else f"{base}-{seen[base]}")
        seen[base] += 1
    return found


def module_sections(text: str) -> list[str]:
    return [
        m.group(1)
        for line in outside_fences(text)
        if (m := re.match(r"## ([a-z][a-z0-9-]*)$", line))
    ]


def readme_rows() -> dict[str, str]:
    text = read(ROOT / "README.md")
    start = text.index("\n## Modules\n")
    section = text[start : text.index("\n## ", start + 1)]
    rows = re.findall(r"^\| `([^`]+)` \| ([a-z]+) \|", section, re.M)
    assert len(rows) == len(dict(rows))
    return dict(rows)


def changelog_preview() -> set[str]:
    names: set[str] = set()
    for line in read(ROOT / "CHANGELOG.md").splitlines():
        head, marker, _ = line.partition("(preview)")
        if line.startswith("- ") and marker:
            names.update(re.findall(r"`([^`]+)`", head))
    return names & set(MODULE_NAMES)


def linking_files() -> list[Path]:
    found: list[Path] = []
    for path in ROOT.rglob("*"):
        parts = path.relative_to(ROOT).parts
        if SKIPPED_DIRS.intersection(parts) or path.suffix not in LINKING_SUFFIXES:
            continue
        if path.is_file():
            found.append(path)
    return found


def test_the_module_directories_are_found() -> None:
    assert len(MODULE_NAMES) > 30
    assert {"metrics", "checks", "ai-usage", "backups", "vulns"} <= set(MODULE_NAMES)


@pytest.mark.parametrize("name", MODULE_NAMES)
def test_every_module_has_a_readme_row(name: str) -> None:
    assert name in readme_rows()


@pytest.mark.parametrize("name", MODULE_NAMES)
def test_every_module_has_a_section_in_the_module_docs(name: str) -> None:
    assert name in module_sections(read(MODULES_DOC))


def test_every_readme_row_names_a_real_module() -> None:
    assert sorted(set(readme_rows()) - set(MODULE_NAMES)) == []


def test_every_module_section_names_a_real_module_once() -> None:
    sections = module_sections(read(MODULES_DOC))
    assert sorted(set(sections) - set(MODULE_NAMES)) == []
    assert sorted(name for name, count in Counter(sections).items() if count > 1) == []


def test_the_module_sections_come_before_writing_your_own_module() -> None:
    lines = outside_fences(read(MODULES_DOC))
    own = lines.index("## Write your own module")
    last = max(lines.index(f"## {name}") for name in MODULE_NAMES)
    assert last < own


def test_the_contents_table_lists_every_module_in_section_order() -> None:
    text = read(MODULES_DOC)
    start = text.index("\n## Contents\n")
    contents = text[start : text.index("\n## ", start + 1)]
    listed = re.findall(r"^\| \[`([^`]+)`\]\(#([^)]+)\) \|", contents, re.M)
    assert [name for name, _ in listed] == module_sections(text)
    assert all(name == anchor for name, anchor in listed)


def test_the_readme_status_matches_the_changelog() -> None:
    rows = readme_rows()
    assert set(rows.values()) <= {"stable", "preview"}
    preview = {name for name, status in rows.items() if status == "preview"}
    assert sorted(preview ^ changelog_preview()) == []


def test_every_docs_page_is_linked_from_the_index() -> None:
    index = read(DOCS / "index.md")
    pages = sorted(page.name for page in DOCS.glob("*.md") if page.name != "index.md")
    assert sorted(page for page in pages if f"]({page}" not in index) == []


def test_every_link_into_the_module_docs_names_an_existing_anchor() -> None:
    known = anchors(read(MODULES_DOC))
    broken: list[str] = []
    for path in linking_files():
        try:
            text = read(path)
        except UnicodeDecodeError:
            continue
        rel = path.relative_to(ROOT).as_posix()
        targets = set(MODULES_LINK.findall(text))
        if path == MODULES_DOC:
            targets |= set(re.findall(r"\]\(#([A-Za-z0-9_-]+)\)", text))
        broken.extend(f"{rel}: #{target}" for target in sorted(targets - known))
    assert broken == []


def test_anchors_follow_the_github_slugs() -> None:
    text = (
        "# Modules\n"
        "## The action, and why not `docker compose up -d`\n"
        "### rest-server (append-only)\n"
        "```sh\n# not a heading\n```\n"
        "## a\n"
        "## a\n"
    )
    assert anchors(text) == {
        "modules",
        "the-action-and-why-not-docker-compose-up--d",
        "rest-server-append-only",
        "a",
        "a-1",
    }
