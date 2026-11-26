from __future__ import annotations

import dataclasses
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from ayeops.texthygiene import (
    Hit,
    classify,
    find_hits,
    is_mixed_script,
    is_prose,
    main,
    manifest_problems,
    point_name,
    read_text,
    text_hits,
    tracked_files,
    words,
)
from tests.conftest import ROOT, WAIT, rules_hit

ZWSP = "\u200b"
NBSP = "\u00a0"
RLO = "\u202e"
EM_DASH = "\u2014"
CYRILLIC_A = "\u0430"


def kinds(source: str, prose: bool = False) -> list[tuple[int, int, str]]:
    return [(hit.line, hit.column, hit.kind) for hit in text_hits("f", source, prose)]


def test_repository_is_clean() -> None:
    assert [str(hit) for hit in find_hits([ROOT])] == []


@pytest.mark.parametrize(
    ("point", "kind"),
    [
        (0x200B, "invisible"),
        (0x200C, "invisible"),
        (0x200D, "invisible"),
        (0x2060, "invisible"),
        (0x2064, "invisible"),
        (0xFEFF, "invisible"),
        (0x00AD, "invisible"),
        (0x034F, "invisible"),
        (0x180E, "invisible"),
        (0x061C, "bidi"),
        (0x200E, "bidi"),
        (0x200F, "bidi"),
        (0x202A, "bidi"),
        (0x202E, "bidi"),
        (0x2066, "bidi"),
        (0x2069, "bidi"),
        (0x00A0, "space"),
        (0x1680, "space"),
        (0x2000, "space"),
        (0x200A, "space"),
        (0x202F, "space"),
        (0x205F, "space"),
        (0x3000, "space"),
        (0x2028, "separator"),
        (0x2029, "separator"),
        (0x0000, "control"),
        (0x0007, "control"),
        (0x000B, "control"),
        (0x000C, "control"),
        (0x001B, "control"),
        (0x007F, "control"),
        (0x0085, "control"),
        (0xE000, "private-use"),
        (0xF8FF, "private-use"),
        (0xF0000, "private-use"),
        (0x2018, "typography"),
        (0x201D, "typography"),
        (0x2010, "typography"),
        (0x2015, "typography"),
        (0x2212, "typography"),
        (0x2026, "typography"),
    ],
)
def test_classify_flags(point: int, kind: str) -> None:
    found = classify(chr(point))
    assert found is not None
    assert found[0] == kind


@pytest.mark.parametrize(
    "char",
    [
        " ",
        "\t",
        "\n",
        "\r",
        "a",
        CYRILLIC_A,
        "\u02bc",
        "\u00ab",
        "\u00e9",
        "-",
        "'",
        '"',
        "\u1fff",
        "\u2030",
        "\u20b4",
        "\u2027",
        "\u2070",
        "\u205e",
        "\u2065",
        "\u206a",
        "\u2016",
        "\u2017",
        "\u2211",
        "\ud7ff",
        "\uf900",
    ],
)
def test_classify_passes(char: str) -> None:
    assert classify(char) is None


@pytest.mark.parametrize(
    ("char", "fix"),
    [
        (ZWSP, "delete it"),
        (RLO, "delete it"),
        (NBSP, "use a plain space"),
        ("\u2028", "use a plain newline"),
        ("\x07", "delete it"),
        ("\ue000", "delete it"),
        ("\u201c", 'use "'),
        ("\u2019", "use '"),
        (EM_DASH, "use -"),
        ("\u2026", "use ..."),
    ],
)
def test_classify_suggests_a_fix(char: str, fix: str) -> None:
    found = classify(char)
    assert found is not None
    assert found[1] == fix


def test_hit_reports_line_column_name_and_fix() -> None:
    hits = text_hits("a.yaml", "a: 1\nb: x" + ZWSP + "y\n", prose=False)
    assert [str(hit) for hit in hits] == [
        "a.yaml:2:5: U+200B ZERO WIDTH SPACE (invisible): delete it"
    ]


def test_control_character_has_a_category_name() -> None:
    assert point_name(0x07) == "<Cc>"
    assert point_name(0x2028) == "LINE SEPARATOR"


def test_form_feed_and_line_separator_do_not_split_lines() -> None:
    assert kinds("a\x0cb\nc\u2028d\ne" + ZWSP) == [
        (1, 2, "control"),
        (2, 2, "separator"),
        (3, 2, "invisible"),
    ]


def test_leading_byte_order_mark_is_flagged() -> None:
    assert kinds("\ufeffkey: value\n") == [(1, 1, "invisible")]


def test_typography_is_flagged_only_outside_prose() -> None:
    source = "run: echo " + EM_DASH + "help\n"
    assert kinds(source) == [(1, 11, "typography")]
    assert kinds(source, prose=True) == []


def test_prose_still_rejects_invisible_characters() -> None:
    assert kinds("a" + NBSP + "b" + EM_DASH, prose=True) == [(1, 2, "space")]


@pytest.mark.parametrize(
    ("name", "prose"),
    [
        ("a.sh", False),
        ("a.yaml", False),
        ("a.yml", False),
        ("a.toml", False),
        ("a.ini", False),
        ("a.cfg", False),
        ("a.conf", False),
        ("a.env", False),
        ("a.alloy", False),
        ("a.bash", False),
        ("Dockerfile", False),
        ("web.Dockerfile", False),
        ("Makefile", False),
        (".env.example", False),
        (".gitignore", False),
        (".dockerignore", False),
        ("a.py", True),
        ("a.md", True),
        ("a.json", True),
        ("a.ts", True),
        ("a.j2", True),
    ],
)
def test_is_prose(name: str, prose: bool) -> None:
    assert is_prose(Path("dir") / name) is prose


def test_mixed_script_word_is_flagged() -> None:
    word = "p" + CYRILLIC_A + "ssword"
    hits = text_hits("a.py", "x = '" + word + "'\n", prose=True)
    assert [(hit.column, hit.kind, hit.point) for hit in hits] == [(6, "mixed-script", ord("p"))]
    assert hits[0].suggestion == "retype the word in one alphabet: " + word


def test_words_in_different_scripts_are_fine() -> None:
    assert kinds("hello " + CYRILLIC_A * 3 + " world") == []


def test_letter_after_backslash_is_an_escape_not_a_word() -> None:
    assert kinds("print('\\n" + CYRILLIC_A * 4 + "')") == []
    assert list(words("\\n" + CYRILLIC_A + "b")) == [(2, CYRILLIC_A + "b")]


def test_words_split_on_non_letters() -> None:
    assert list(words("ab1cd ef")) == [(0, "ab"), (3, "cd"), (6, "ef")]
    assert list(words("ab")) == [(0, "ab")]
    assert list(words("")) == []


def test_mixed_script_needs_both_scripts() -> None:
    assert is_mixed_script("a" + CYRILLIC_A)
    assert not is_mixed_script("ab")
    assert not is_mixed_script(CYRILLIC_A * 2)
    assert not is_mixed_script("a\u03b1")


def test_binary_files_are_skipped(tmp_path: Path) -> None:
    (tmp_path / "a.bin").write_bytes(b"\x00\x01" + ZWSP.encode())
    (tmp_path / "b.bin").write_bytes(b"\xff\xfe")
    assert read_text(tmp_path / "a.bin") is None
    assert read_text(tmp_path / "b.bin") is None
    assert find_hits([tmp_path]) == []


def test_files_outside_git_are_walked(tmp_path: Path) -> None:
    (tmp_path / "a.yaml").write_text("a: " + ZWSP + "\n", encoding="utf-8")
    assert tracked_files(tmp_path) == [tmp_path / "a.yaml"]
    assert [hit.path for hit in find_hits([tmp_path])] == [(tmp_path / "a.yaml").as_posix()]


def test_tracked_files_lists_only_what_git_tracks(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "pyvenv.cfg").write_text("home = x\n", encoding="utf-8")
    for argv in (["git", "init", "-q"], ["git", "add", "pyproject.toml"]):
        subprocess.run(argv, cwd=tmp_path, check=True, capture_output=True, timeout=WAIT)
    assert tracked_files(tmp_path) == [tmp_path / "pyproject.toml"]


def test_a_hung_git_ls_files_times_out_and_the_tree_is_walked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "a.yaml").write_text("a: 1\n", encoding="utf-8")
    seen: list[Any] = []

    def hung(argv: list[str], **options: Any) -> Any:
        seen.append(options.get("timeout"))
        raise subprocess.TimeoutExpired(argv, options.get("timeout") or 0)

    monkeypatch.setattr(subprocess, "run", hung)
    assert tracked_files(tmp_path) == [tmp_path / "a.yaml"]
    assert seen and seen[0] is not None and seen[0] > 0


def test_a_single_file_is_checked(tmp_path: Path) -> None:
    path = tmp_path / "a.md"
    path.write_text("x" + RLO, encoding="utf-8")
    assert tracked_files(path) == [path]
    assert [hit.kind for hit in find_hits([path])] == ["bidi"]


def test_main_reports_and_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "a.sh").write_text("echo " + EM_DASH + "\n", encoding="utf-8")
    assert main([str(tmp_path)]) == 1
    out = capsys.readouterr().out
    assert "a.sh:1:6: U+2014 EM DASH (typography): use -" in out
    assert "1 character(s)" in out


def test_main_passes_clean_tree(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "a.sh").write_text("echo -\n", encoding="utf-8")
    assert main([str(tmp_path)]) == 0
    assert capsys.readouterr().out == ""


def test_hits_are_sorted() -> None:
    hits = text_hits("f", "b" + ZWSP + "\n" + NBSP, prose=False)
    assert hits == sorted(hits)
    assert hits[0] == Hit("f", 1, 2, 0x200B, "invisible", "delete it")


def test_manifest_values_and_keys_are_checked() -> None:
    data: dict[str, Any] = {
        "services": {
            "web": {"description": "shop" + ZWSP, "alerts": [{"title": "down" + EM_DASH}]},
            "a" + RLO: {"description": "ok"},
        },
        "port": 80,
    }
    assert manifest_problems(data) == [
        (
            "services.web.description",
            "text-hygiene",
            "U+200B ZERO WIDTH SPACE (invisible): delete it",
        ),
        (
            "services.a" + RLO,
            "text-hygiene",
            "U+202E RIGHT-TO-LEFT OVERRIDE (bidi): delete it",
        ),
    ]


def test_manifest_list_items_carry_their_index() -> None:
    data = {"hosts": {"a": {"tags": ["ok", "x" + NBSP]}}}
    assert [path for path, _, _ in manifest_problems(data)] == ["hosts.a.tags[1]"]


def test_aye_check_rejects_an_invisible_character(data: dict[str, Any]) -> None:
    service = next(iter(data["services"].values()))
    service["description"] += ZWSP
    assert "text-hygiene" in rules_hit(data)


def test_aye_check_accepts_typography_in_a_title(data: dict[str, Any]) -> None:
    service = next(iter(data["services"].values()))
    service["description"] += " " + EM_DASH + " fine"
    assert "text-hygiene" not in rules_hit(data)


@pytest.mark.parametrize(
    ("point", "ascii_text"),
    [
        (0x2018, "'"),
        (0x2019, "'"),
        (0x201A, "'"),
        (0x201B, "'"),
        (0x201C, '"'),
        (0x201D, '"'),
        (0x201E, '"'),
        (0x201F, '"'),
        (0x2010, "-"),
        (0x2011, "-"),
        (0x2012, "-"),
        (0x2013, "-"),
        (0x2014, "-"),
        (0x2015, "-"),
        (0x2212, "-"),
        (0x2026, "..."),
    ],
)
def test_typography_suggests_its_ascii_twin(point: int, ascii_text: str) -> None:
    assert classify(chr(point)) == ("typography", "use " + ascii_text)


def test_hit_is_frozen() -> None:
    hit = Hit("f", 1, 1, 0x200B, "invisible", "delete it")
    with pytest.raises(dataclasses.FrozenInstanceError):
        hit.__setattr__("line", 2)


def git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def test_only_tracked_files_are_checked(tmp_path: Path) -> None:
    git(tmp_path, "init", "-q")
    (tmp_path / "kept.yaml").write_text("a: 1\n", encoding="utf-8")
    (tmp_path / "gone.yaml").write_text("a: 1\n", encoding="utf-8")
    git(tmp_path, "add", "kept.yaml", "gone.yaml")
    (tmp_path / "gone.yaml").unlink()
    (tmp_path / "loose.yaml").write_text("a: " + ZWSP + "\n", encoding="utf-8")
    assert tracked_files(tmp_path) == [tmp_path / "kept.yaml"]
    assert find_hits([tmp_path]) == []


SHRUG = chr(0x1F937)
MALE = chr(0x2642)
ZWJ = chr(0x200D)


def test_zero_width_joiner_inside_an_emoji_is_fine() -> None:
    assert kinds(SHRUG + ZWJ + MALE) == []
    assert kinds(SHRUG + chr(0x1F3FB) + ZWJ + MALE) == []
    assert kinds(SHRUG + chr(0xFE0F) + ZWJ + MALE + chr(0xFE0F)) == []


@pytest.mark.parametrize(
    "text",
    [
        "a" + ZWJ + "b",
        SHRUG + ZWJ + "b",
        "a" + ZWJ + MALE,
        ZWJ + MALE,
        SHRUG + ZWJ,
        SHRUG + ZWSP + MALE,
    ],
)
def test_joiner_outside_an_emoji_is_flagged(text: str) -> None:
    assert [kind for _, _, kind in kinds(text)] == ["invisible"]


def test_main_reads_argv_and_defaults_to_the_current_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "a.sh").write_text("echo " + ZWSP + "\n", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["texthygiene", str(tmp_path / "a.sh")])
    assert main() == 1
    monkeypatch.setattr(sys, "argv", ["texthygiene"])
    monkeypatch.chdir(tmp_path)
    assert main() == 1
    lines = capsys.readouterr().out.splitlines()
    assert lines[-2] == "a.sh:1:6: U+200B ZERO WIDTH SPACE (invisible): delete it"
    assert lines[-1] == ("1 character(s) a reader cannot see or tell apart; fix them as suggested.")


def test_hits_from_every_file_are_kept(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text(ZWSP, encoding="utf-8")
    (tmp_path / "b.md").write_text(RLO, encoding="utf-8")
    assert [hit.kind for hit in find_hits([tmp_path])] == ["invisible", "bidi"]
