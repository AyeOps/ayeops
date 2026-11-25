from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from ayeops.cli import main
from ayeops.defaults import (
    MANIFEST_ENV,
    MANIFEST_NAMES,
    UsageError,
    add_manifest,
    find_manifest,
    host_names,
    only_host,
    pick_host,
)
from tests.conftest import EXAMPLE, ROOT

QUICKSTART = ROOT / "examples" / "quickstart.yaml"


def test_the_default_manifest_names_are_ayeops_and_manifest_yaml() -> None:
    assert MANIFEST_ENV == "AYE_MANIFEST"
    assert MANIFEST_NAMES == ("ayeops.yaml", "manifest.yaml")


def test_a_given_path_wins_over_the_variable_and_the_directory(tmp_path: Path) -> None:
    (tmp_path / "ayeops.yaml").write_text("x", encoding="utf-8")
    given = Path("given.yaml")
    assert find_manifest(given, {MANIFEST_ENV: "env.yaml"}, tmp_path) is given


def test_the_variable_wins_over_the_directory(tmp_path: Path) -> None:
    (tmp_path / "ayeops.yaml").write_text("x", encoding="utf-8")
    assert find_manifest(None, {MANIFEST_ENV: "ops/env.yaml"}, tmp_path) == Path("ops/env.yaml")


def test_an_empty_variable_counts_as_unset(tmp_path: Path) -> None:
    (tmp_path / "manifest.yaml").write_text("x", encoding="utf-8")
    assert find_manifest(None, {MANIFEST_ENV: ""}, tmp_path) == tmp_path / "manifest.yaml"


@pytest.mark.parametrize("name", MANIFEST_NAMES)
def test_the_one_manifest_of_the_directory_is_the_default(tmp_path: Path, name: str) -> None:
    (tmp_path / name).write_text("x", encoding="utf-8")
    assert find_manifest(None, {}, tmp_path) == tmp_path / name


def test_the_current_directory_is_the_default_place(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "ayeops.yaml").write_text("x", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert find_manifest(None) == Path("ayeops.yaml")
    monkeypatch.setenv(MANIFEST_ENV, "elsewhere.yaml")
    assert find_manifest(None) == Path("elsewhere.yaml")


def test_two_manifests_in_the_directory_are_ambiguous(tmp_path: Path) -> None:
    for name in MANIFEST_NAMES:
        (tmp_path / name).write_text("x", encoding="utf-8")
    with pytest.raises(UsageError) as raised:
        find_manifest(None, {}, tmp_path)
    assert str(raised.value) == (
        "both ayeops.yaml and manifest.yaml are here; name the manifest,"
        " e.g. aye check ayeops.yaml, or set AYE_MANIFEST"
    )


def test_a_directory_of_that_name_is_not_a_manifest(tmp_path: Path) -> None:
    (tmp_path / "ayeops.yaml").mkdir()
    with pytest.raises(UsageError) as raised:
        find_manifest(None, {}, tmp_path)
    assert str(raised.value) == (
        "no manifest: name its path, set AYE_MANIFEST, or run where ayeops.yaml or manifest.yaml is"
    )


def test_the_only_host_is_the_default_host() -> None:
    assert host_names(QUICKSTART) == ["local"]
    assert only_host(QUICKSTART) == "local"
    assert only_host(QUICKSTART, fleet=True) == "local"
    assert pick_host(QUICKSTART, None) == "local"


def test_several_hosts_need_a_choice_and_the_error_lists_them() -> None:
    with pytest.raises(UsageError) as single:
        only_host(EXAMPLE)
    assert str(single.value) == (
        f"{EXAMPLE} has 2 hosts, name one with --host <name>: central, edge"
    )
    with pytest.raises(UsageError) as fleet:
        only_host(EXAMPLE, fleet=True)
    assert str(fleet.value) == (
        f"{EXAMPLE} has 2 hosts, name one with --host <name> or --all: central, edge"
    )


def test_a_manifest_without_hosts_points_at_the_check(tmp_path: Path) -> None:
    path = tmp_path / "ayeops.yaml"
    path.write_text("version: 1\nproject: x\n", encoding="utf-8")
    assert host_names(path) == []
    with pytest.raises(UsageError) as raised:
        only_host(path)
    assert str(raised.value) == f"{path} has no hosts; run aye check {path}"


def test_pick_host_keeps_a_known_host_and_refuses_an_unknown_one() -> None:
    assert pick_host(EXAMPLE, "edge") == "edge"
    with pytest.raises(UsageError) as raised:
        pick_host(EXAMPLE, "nowhere")
    assert str(raised.value) == f"{EXAMPLE}: unknown host 'nowhere', hosts: central, edge"


def test_the_manifest_argument_is_optional_and_says_its_default() -> None:
    parser = argparse.ArgumentParser()
    add_manifest(parser)
    assert parser.parse_args([]).manifest is None
    assert parser.parse_args(["m.yaml"]).manifest == Path("m.yaml")
    assert "default $AYE_MANIFEST, else ayeops.yaml or manifest.yaml" in parser.format_help()


def copy_quickstart(directory: Path, name: str = "ayeops.yaml") -> Path:
    path = directory / name
    path.write_text(QUICKSTART.read_text(encoding="utf-8"), encoding="utf-8")
    return path


def test_check_and_render_without_a_path_use_the_manifest_here(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    copy_quickstart(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert main(["check"]) == 0
    assert capsys.readouterr().out == "ayeops.yaml: ok\n"
    assert main(["render"]) == 0
    assert (tmp_path / "rendered" / "local" / "compose.yaml").is_file()
    assert "file(s) written to rendered," in capsys.readouterr().out


def test_aye_manifest_names_the_manifest_from_anywhere(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AYE_MANIFEST", str(QUICKSTART))
    assert main(["check"]) == 0
    assert capsys.readouterr().out == f"{QUICKSTART}: ok\n"


@pytest.mark.parametrize(
    "command",
    [
        ["check"],
        ["render"],
        ["dns"],
        ["status"],
        ["permissions"],
        ["portal", "-o", "out"],
        ["deploy", "--host", "a"],
        ["diff", "--all"],
        ["doctor", "--all"],
        ["host-init"],
        ["up"],
        ["down"],
        ["status", "deploy", "--dry-run"],
    ],
)
def test_every_manifest_command_without_one_exits_2_and_says_where_it_looked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: list[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    assert main(command) == 2
    assert capsys.readouterr().err == (
        "aye: no manifest: name its path, set AYE_MANIFEST,"
        " or run where ayeops.yaml or manifest.yaml is\n"
    )
    assert list(tmp_path.iterdir()) == []


def test_a_missing_manifest_file_exits_2_without_a_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    assert main(["check", "nope.yaml"]) == 2
    err = capsys.readouterr().err
    assert err.startswith("aye: nope.yaml: ")
    assert "Traceback" not in err


def test_a_command_on_a_fleet_without_host_or_all_exits_2_and_lists_the_hosts(
    capsys: pytest.CaptureFixture[str],
) -> None:
    for command in ("deploy", "diff", "doctor"):
        assert main([command, str(EXAMPLE)]) == 2
        assert capsys.readouterr().err == (
            f"aye: {EXAMPLE} has 2 hosts, name one with --host <name> or --all: central, edge\n"
        )
    assert main(["host-init", str(EXAMPLE)]) == 2
    assert capsys.readouterr().err == (
        f"aye: {EXAMPLE} has 2 hosts, name one with --host <name>: central, edge\n"
    )


def test_host_init_takes_the_only_host_of_the_manifest(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["host-init", str(QUICKSTART)]) == 0
    script = capsys.readouterr().out
    assert "/opt/ayeops/local" in script
