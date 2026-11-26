from __future__ import annotations

from pathlib import Path

from ayeops.comments import comments_in, find_comments, main

ROOT = Path(__file__).resolve().parents[1]


def found(name: str, source: str) -> list[int]:
    return [line for line, _ in comments_in(Path(name), source)]


def test_repository_has_no_comments() -> None:
    assert [str(c) for c in find_comments([ROOT])] == []


def test_python_comment_is_flagged() -> None:
    assert found("a.py", "x = 1\n\ny = 2  " + "#" + " why\n") == [3]


def test_python_shebang_is_allowed() -> None:
    assert found("a.py", "#" + "!/usr/bin/env python\nx = 1\n") == []


def test_hash_inside_python_string_is_not_a_comment() -> None:
    assert found("a.py", 'x = "#31C97E"\n') == []


def test_yaml_comment_is_flagged() -> None:
    source = "a: 1\n" + "#" + " note\nb: 2 " + "#" + " trailing\n"
    assert found("a.yaml", source) == [2, 3]


def test_hash_inside_yaml_value_is_not_a_comment() -> None:
    source = "color: '#fff'\nurl: http://x/#frag\ntext: \"a # b\"\nit's: a#b\n"
    assert found("a.yaml", source) == []


def test_yaml_document_markers_are_allowed() -> None:
    assert found("a.yml", "---\na: 1\n...\n") == []


def test_shell_comment_is_flagged_but_shebang_and_parameter_length_are_not() -> None:
    source = "#" + "!/bin/sh\necho ${" + "#" + "x} $" + "#" + "\n" + "#" + " explain\n"
    assert found("a.sh", source) == [3]


def test_dockerfile_syntax_directive_is_allowed() -> None:
    source = "#" + " syntax=docker/dockerfile:1\nFROM alpine:3.23.0\n"
    assert found("Dockerfile", source) == []
    assert found("Dockerfile", "FROM alpine:3.23.0\n" + "#" + " why\n") == [2]


def test_jinja_comment_is_flagged() -> None:
    assert found("a.yaml.j2", "a: 1\n{" + "# note #" + "}\n") == [2]


def test_json5_comment_is_flagged_but_urls_in_strings_are_not() -> None:
    source = '{"url": "https://a.example"}\n' + "/" + "/ note\n" + "/" + "* block */\n"
    assert found("a.json5", source) == [2, 3]


def test_css_comment_is_flagged() -> None:
    assert found("a.css", "a{color:red}\n" + "/" + "* note */\n") == [2]


def test_markdown_is_not_scanned() -> None:
    assert found("a.md", "#" + " Title\n") == []


def test_command_line_fails_when_a_comment_is_found(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("x = 1  " + "#" + " why\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("y = 2\n", encoding="utf-8")
    assert main([str(tmp_path)]) == 1
    assert main([str(tmp_path / "b.py")]) == 0


def test_typescript_comment_is_flagged_but_slashes_in_template_strings_are_not() -> None:
    source = "const a = `https://${host}/x`;\nconst b = 1; " + "/" + "/ why\n"
    assert found("a.ts", source) == [2]


def test_sql_comment_is_flagged_but_dashes_in_strings_are_not() -> None:
    source = "SELECT '--x', \"a--b\";\nSELECT 1; " + "-" + "- why\n" + "/" + "* block */\n"
    assert found("a.sql", source) == [2, 3]


def test_alloy_comment_is_flagged() -> None:
    source = 'a {\n  url = "http://x//y"\n}\n' + "/" + "/ note\n"
    assert found("config.alloy", source) == [4]


def test_a_renovate_version_tag_after_an_action_sha_is_allowed() -> None:
    sha = "3d3c42e5aac5ba805825da76410c181273ba90b1"
    lines = [
        f"      - uses: actions/checkout@{sha} " + "#" + " v7.0.1",
        f"        uses: actions/upload-artifact@{sha} " + "#" + " v4",
    ]
    assert found("ci.yml", "\n".join(lines) + "\n") == []


def test_prose_after_an_action_or_a_tag_on_a_moving_ref_is_still_a_comment() -> None:
    sha = "3d3c42e5aac5ba805825da76410c181273ba90b1"
    lines = [
        f"      - uses: actions/checkout@{sha} " + "#" + " v7.0.1 keep this",
        f"      - uses: actions/checkout@{sha} " + "#" + " pinned for safety",
        "      - uses: actions/checkout@v7 " + "#" + " v7.0.1",
        "      - run: echo " + "#" + " v7",
    ]
    assert found("ci.yml", "\n".join(lines) + "\n") == [1, 2, 3, 4]
