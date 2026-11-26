from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from ayeops.cli import main
from ayeops.render_index import INDEX, Drift, drift, index_text, previous, safe, sync
from tests.conftest import EXAMPLE

STAMP = ["--build-time", "2026-01-01 00:00 UTC", "--commit", "example"]


def files_in(root: Path) -> set[str]:
    return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}


def render(out: Path, *extra: str) -> int:
    return main(["render", str(EXAMPLE), "-o", str(out), *STAMP, *extra])


@pytest.fixture(scope="module")
def first_render(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("first") / "out"
    assert render(out) == 0
    return out


@pytest.fixture
def rendered(first_render: Path, tmp_path: Path) -> Path:
    out = tmp_path / "out"
    shutil.copytree(first_render, out)
    return out


def test_the_index_lists_every_written_file_sorted_one_per_line(tmp_path: Path) -> None:
    sync({"b/x.yaml": "x\n", "a/y.yaml": "y\n", "a/b/z.txt": "z\n"}, tmp_path)
    assert (tmp_path / INDEX).read_bytes() == b"a/b/z.txt\na/y.yaml\nb/x.yaml\n"
    assert files_in(tmp_path) == {INDEX, "a/b/z.txt", "a/y.yaml", "b/x.yaml"}


def test_windows_separators_become_slashes_in_the_index_and_on_disk(tmp_path: Path) -> None:
    sync({"central\\grafana\\rules.yaml": "r\n"}, tmp_path)
    assert (tmp_path / INDEX).read_text(encoding="utf-8") == "central/grafana/rules.yaml\n"
    assert (tmp_path / "central" / "grafana" / "rules.yaml").read_text(encoding="utf-8") == "r\n"
    assert index_text(["b\\a", "a/b", "b/a"]) == "a/b\nb/a\n"


def test_a_windows_path_rendered_again_is_not_stale(tmp_path: Path) -> None:
    tree = {"central\\grafana\\rules.yaml": "r\n"}
    sync(tree, tmp_path)
    assert sync(tree, tmp_path)[1] == []
    assert drift(tree, tmp_path) == Drift()


def test_a_listed_file_behind_a_link_out_of_the_output_directory_stays(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "victim.txt").write_text("v\n", encoding="utf-8")
    out = tmp_path / "out"
    out.mkdir()
    try:
        (out / "link").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("this system does not let the test create a symbolic link")
    (out / INDEX).write_text("link/victim.txt\n", encoding="utf-8")
    assert sync({}, out)[1] == []
    assert (outside / "victim.txt").exists()


def test_a_directory_where_the_render_puts_a_file_is_listed_as_added(tmp_path: Path) -> None:
    sync({"h/a": "a\n"}, tmp_path)
    (tmp_path / "h" / "b").mkdir()
    assert drift({"h/a": "a\n", "h/b": "b\n"}, tmp_path).added == ("h/b",)


def test_a_file_the_previous_render_wrote_and_this_one_does_not_is_removed(
    tmp_path: Path,
) -> None:
    sync({"h/keep.yaml": "k\n", "h/old.yaml": "o\n"}, tmp_path)
    written, removed = sync({"h/keep.yaml": "k2\n"}, tmp_path)
    assert removed == ["h/old.yaml"]
    assert written == [tmp_path / "h" / "keep.yaml"]
    assert files_in(tmp_path) == {INDEX, "h/keep.yaml"}
    assert (tmp_path / INDEX).read_text(encoding="utf-8") == "h/keep.yaml\n"


def test_a_directory_left_empty_by_the_removal_is_removed_up_to_the_output_root(
    tmp_path: Path,
) -> None:
    sync({"h/keep.yaml": "k\n", "h/deep/er/old.yaml": "o\n"}, tmp_path)
    sync({"h/keep.yaml": "k\n"}, tmp_path)
    assert not (tmp_path / "h" / "deep").exists()
    assert (tmp_path / "h").is_dir()
    sync({}, tmp_path)
    assert files_in(tmp_path) == {INDEX}
    assert [p.name for p in tmp_path.iterdir()] == [INDEX]


def test_a_directory_that_still_holds_a_user_file_stays(tmp_path: Path) -> None:
    sync({"h/sub/old.yaml": "o\n"}, tmp_path)
    (tmp_path / "h" / "sub" / "notes.md").write_text("mine\n", encoding="utf-8")
    (tmp_path / "empty-of-mine").mkdir()
    sync({}, tmp_path)
    assert files_in(tmp_path) == {INDEX, "h/sub/notes.md"}
    assert (tmp_path / "empty-of-mine").is_dir()


def test_a_file_the_render_did_not_write_stays(tmp_path: Path) -> None:
    sync({"h/a.yaml": "a\n"}, tmp_path)
    (tmp_path / "h" / ".env").write_text("SECRET=1\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("mine\n", encoding="utf-8")
    sync({"h/b.yaml": "b\n"}, tmp_path)
    assert files_in(tmp_path) == {INDEX, "README.md", "h/.env", "h/b.yaml"}


def test_without_an_index_nothing_is_removed(tmp_path: Path) -> None:
    (tmp_path / "h").mkdir()
    (tmp_path / "h" / "old.yaml").write_text("o\n", encoding="utf-8")
    _, removed = sync({"h/new.yaml": "n\n"}, tmp_path)
    assert removed == []
    assert files_in(tmp_path) == {INDEX, "h/new.yaml", "h/old.yaml"}


def test_a_file_that_replaces_a_stale_directory_is_written(tmp_path: Path) -> None:
    sync({"h/conf/a.yaml": "a\n"}, tmp_path)
    sync({"h/conf": "now a file\n"}, tmp_path)
    assert (tmp_path / "h" / "conf").read_text(encoding="utf-8") == "now a file\n"


@pytest.mark.parametrize(
    "entry",
    [
        "",
        "/etc/passwd",
        "../outside",
        "h/../../x",
        "h/./a",
        "h//a",
        ".aye-render",
        ".aye-files",
        "h\\a",
        "C:/Windows/x",
        "h/",
    ],
)
def test_an_unsafe_index_entry_is_never_followed(entry: str) -> None:
    assert not safe(entry)


def test_safe_entries_are_plain_relative_paths() -> None:
    assert all(safe(e) for e in ["h/a.yaml", "a", "h/.env.example", "h/x.aye-new", "h/.aye-x"])


def test_index_lines_outside_the_output_directory_are_skipped(tmp_path: Path) -> None:
    out = tmp_path / "out"
    out.mkdir()
    victim = tmp_path / "victim.txt"
    victim.write_text("v\n", encoding="utf-8")
    (out / INDEX).write_text("../victim.txt\n.aye-render\nh/a\n", encoding="utf-8")
    assert previous(out) == ["h/a"]
    sync({}, out)
    assert victim.exists()


def test_a_missing_listed_file_is_not_stale(tmp_path: Path) -> None:
    sync({"h/a": "a\n", "h/b": "b\n"}, tmp_path)
    (tmp_path / "h" / "b").unlink()
    assert drift({"h/a": "a\n"}, tmp_path).stale == ()


def test_drift_is_empty_after_a_sync_of_the_same_tree(tmp_path: Path) -> None:
    tree = {"h/a.yaml": "a\n", "g/b.yaml": "b\n"}
    sync(tree, tmp_path)
    assert drift(tree, tmp_path) == Drift()
    assert not drift(tree, tmp_path)


def test_drift_names_added_changed_and_stale_paths_sorted(tmp_path: Path) -> None:
    sync({"h/same": "s\n", "h/edit": "1\n", "h/z-gone": "g\n", "h/a-gone": "g\n"}, tmp_path)
    (tmp_path / "h" / "mine").write_text("m\n", encoding="utf-8")
    found = drift({"h/same": "s\n", "h/edit": "2\n", "h/b-new": "n\n", "a/new": "n\n"}, tmp_path)
    assert found == Drift(("a/new", "h/b-new"), (INDEX, "h/edit"), ("h/a-gone", "h/z-gone"))
    assert found.lines() == [
        "added a/new",
        "added h/b-new",
        f"changed {INDEX}",
        "changed h/edit",
        "stale h/a-gone",
        "stale h/z-gone",
    ]
    assert bool(found)


def test_drift_compares_bytes_so_a_crlf_file_is_changed(tmp_path: Path) -> None:
    sync({"h/a": "a\n"}, tmp_path)
    (tmp_path / "h" / "a").write_bytes(b"a\r\n")
    assert drift({"h/a": "a\n"}, tmp_path).changed == ("h/a",)


def test_drift_writes_nothing(tmp_path: Path) -> None:
    out = tmp_path / "out"
    assert drift({"h/a": "a\n"}, out) == Drift(added=(INDEX, "h/a"))
    assert not out.exists()


def test_a_drift_of_only_stale_files_is_still_a_drift() -> None:
    assert bool(Drift(stale=("x",)))
    assert bool(Drift(changed=("x",)))
    assert bool(Drift(added=("x",)))


def test_render_check_passes_on_a_fresh_render_and_writes_nothing(
    rendered: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    before = {p: p.stat().st_mtime_ns for p in rendered.rglob("*")}
    capsys.readouterr()
    assert render(rendered, "--check") == 0
    out = capsys.readouterr().out
    assert out.splitlines() == [f"{EXAMPLE}: {rendered} matches the render"]
    assert {p: p.stat().st_mtime_ns for p in rendered.rglob("*")} == before


def test_render_check_lists_each_kind_and_exits_one(
    rendered: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (rendered / "edge" / "compose.yaml").write_text("changed\n", encoding="utf-8")
    (rendered / "central" / ".env.example").unlink()
    (rendered / "edge" / "gone.yaml").write_text("old\n", encoding="utf-8")
    index = rendered / INDEX
    index.write_text(index.read_text(encoding="utf-8") + "edge/gone.yaml\n", encoding="utf-8")
    (rendered / "edge" / "mine.txt").write_text("mine\n", encoding="utf-8")
    capsys.readouterr()
    assert render(rendered, "--check") == 1
    captured = capsys.readouterr()
    assert captured.out.splitlines() == [
        "added central/.env.example",
        f"changed {INDEX}",
        "changed edge/compose.yaml",
        "stale edge/gone.yaml",
    ]
    assert f"{rendered} differs from the render: 1 added, 2 changed, 1 stale" in captured.err
    assert (rendered / "edge" / "gone.yaml").exists()
    assert not (rendered / "central" / ".env.example").exists()


def test_render_removes_the_stale_file_and_reports_it(
    rendered: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (rendered / "edge" / "old").mkdir()
    (rendered / "edge" / "old" / "gone.yaml").write_text("old\n", encoding="utf-8")
    index = rendered / INDEX
    index.write_text(index.read_text(encoding="utf-8") + "edge/old/gone.yaml\n", encoding="utf-8")
    (rendered / "edge" / "mine.txt").write_text("mine\n", encoding="utf-8")
    capsys.readouterr()
    assert render(rendered) == 0
    out = capsys.readouterr().out
    assert "removed edge/old/gone.yaml" in out.splitlines()
    assert "1 stale removed" in out
    assert not (rendered / "edge" / "old").exists()
    assert (rendered / "edge" / "mine.txt").exists()


def test_render_check_into_a_directory_without_an_index_lists_no_stale_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "edge").mkdir()
    (tmp_path / "edge" / "old.yaml").write_text("old\n", encoding="utf-8")
    assert render(tmp_path, "--check") == 1
    lines = capsys.readouterr().out.splitlines()
    assert f"added {INDEX}" in lines
    assert "added edge/compose.yaml" in lines
    assert not [line for line in lines if not line.startswith("added ")]
    assert files_in(tmp_path) == {"edge/old.yaml"}


def test_render_check_honours_the_build_stamp(
    rendered: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    capsys.readouterr()
    other = ["--build-time", "2026-01-01 00:00 UTC", "--commit", "other"]
    assert main(["render", str(EXAMPLE), "-o", str(rendered), "--check", *other]) == 1
    assert "changed central/portal/index.html" in capsys.readouterr().out.splitlines()
