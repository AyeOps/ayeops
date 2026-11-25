from __future__ import annotations

import argparse
import shlex
import sys
from pathlib import Path

import pytest

from ayeops import up as up_module
from ayeops.cli import build_parser, main, render
from ayeops.defaults import UsageError
from ayeops.secrets import EnvFill, SecretsError, parse_env
from ayeops.up import (
    DEFAULT_OUT,
    arch_warning,
    compose_argv,
    compose_runner,
    down,
    shown,
    up,
)
from tests.conftest import EXAMPLE, ROOT

QUICKSTART = ROOT / "examples" / "quickstart.yaml"
README = ROOT / "README.md"
UP = "docker compose -f rendered/local/compose.yaml up -d"
PS = "docker compose -f rendered/local/compose.yaml ps"
DOWN = "docker compose -f rendered/local/compose.yaml down"
NATIVE = "x86_64"


class FakeCompose:
    def __init__(self, code: int = 0) -> None:
        self.code = code
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str]) -> int:
        self.calls.append(argv)
        return self.code


def docker(name: str) -> str | None:
    return f"/usr/bin/{name}"


def nowhere(name: str) -> str | None:
    return None


@pytest.fixture
def here(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "ayeops.yaml"
    path.write_text(QUICKSTART.read_text(encoding="utf-8"), encoding="utf-8")
    return Path("ayeops.yaml")


def start(manifest: Path, runner: FakeCompose) -> int:
    return up(manifest, None, DEFAULT_OUT, render, True, runner, docker, NATIVE)


def test_the_default_render_directory_is_rendered() -> None:
    assert Path("rendered") == DEFAULT_OUT
    assert build_parser().parse_args(["render"]).out == DEFAULT_OUT
    assert build_parser().parse_args(["up"]).out == DEFAULT_OUT
    assert build_parser().parse_args(["down"]).out == DEFAULT_OUT


def test_compose_argv_names_the_compose_file_of_the_host() -> None:
    argv = compose_argv(Path("rendered"), "local", "up", "-d")
    assert argv == ["docker", "compose", "-f", "rendered/local/compose.yaml", "up", "-d"]
    assert shown(argv) == UP
    assert (
        shown(["docker", "compose", "-f", "my dir/c.yaml"]) == "docker compose -f 'my dir/c.yaml'"
    )


def test_up_renders_writes_the_env_and_prints_the_compose_lines_without_running_them(
    here: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runner = FakeCompose()
    assert up(here, None, DEFAULT_OUT, render, False, runner, nowhere, NATIVE) == 0
    assert runner.calls == []
    out = capsys.readouterr().out.splitlines()
    assert out[-3:] == [f"start:  {UP}", f"status: {PS}", f"stop:   {DOWN}"]
    assert "secrets: rendered/local/.env: generated 2, kept 0 existing value(s)" in out
    assert out[0].startswith("ayeops.yaml: ")
    env = Path("rendered/local/.env")
    values = parse_env(env.read_text(encoding="utf-8"))
    assert set(values) == {"AYE_GRAFANA_ADMIN_USER", "AYE_GRAFANA_ADMIN_PASSWORD"}
    assert not any(value in "\n".join(out) for value in values.values())
    assert Path("rendered/local/compose.yaml").is_file()


def test_up_again_keeps_every_value_of_the_env(
    here: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert start(here, FakeCompose()) == 0
    first = Path("rendered/local/.env").read_bytes()
    runner = FakeCompose()
    assert start(here, runner) == 0
    assert Path("rendered/local/.env").read_bytes() == first
    assert "generated 0, kept 2 existing value(s)" in capsys.readouterr().out
    assert len(runner.calls) == 1


def test_up_with_start_runs_compose_up_once_and_prints_what_it_ran(
    here: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runner = FakeCompose()
    assert start(here, runner) == 0
    assert runner.calls == [shlex.split(UP)]
    out = capsys.readouterr().out.splitlines()
    assert f"running: {UP}" in out
    assert f"start:  {UP}" not in out
    assert out[-2:] == [f"status: {PS}", f"stop:   {DOWN}"]


def test_up_with_start_fails_when_compose_fails(
    here: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runner = FakeCompose(17)
    assert start(here, runner) == 1
    captured = capsys.readouterr()
    assert captured.err.strip() == f"{UP} exited 17"
    assert "status:" not in captured.out


def test_up_with_start_needs_docker_and_then_writes_nothing(
    here: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runner = FakeCompose()
    assert up(here, None, DEFAULT_OUT, render, True, runner, nowhere, NATIVE) == 2
    assert runner.calls == []
    assert not DEFAULT_OUT.exists()
    assert capsys.readouterr().err.strip() == (
        "docker is not on PATH; install Docker with Compose v2"
    )


def test_up_stops_at_the_check_and_writes_nothing(
    here: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    text = here.read_text(encoding="utf-8").replace("retention: 7d", "retention: ages")
    here.write_text(text, encoding="utf-8")
    runner = FakeCompose()
    assert start(here, runner) == 1
    assert runner.calls == []
    assert not DEFAULT_OUT.exists()
    assert "not rendered, fix the errors first" in capsys.readouterr().err


def test_up_refuses_to_start_while_a_variable_is_unset(
    here: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    filled = EnvFill(["x in rendered/local/.env: missing, not generated, you must set it"], ["X"])
    monkeypatch.setattr(up_module, "fill_env_file", lambda *args: filled)
    runner = FakeCompose()
    assert start(here, runner) == 1
    assert runner.calls == []
    captured = capsys.readouterr()
    assert f"secrets: {filled.lines[0]}" in captured.out
    assert captured.err.strip() == f"rendered/local/.env: set X, then run: {UP}"
    assert "start:" not in captured.out


def test_up_reports_an_env_file_it_cannot_write(
    here: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def broken(*args: object) -> EnvFill:
        raise SecretsError("rendered/local/.env is not UTF-8, nothing written")

    monkeypatch.setattr(up_module, "fill_env_file", broken)
    runner = FakeCompose()
    assert start(here, runner) == 1
    assert runner.calls == []
    assert capsys.readouterr().err.strip() == (
        "secrets: rendered/local/.env is not UTF-8, nothing written"
    )


def test_up_takes_a_named_host_and_refuses_an_unknown_one(here: Path) -> None:
    runner = FakeCompose()
    assert up(here, "local", DEFAULT_OUT, render, True, runner, docker, NATIVE) == 0
    with pytest.raises(UsageError, match="unknown host 'nowhere', hosts: local"):
        up(here, "nowhere", DEFAULT_OUT, render, True, runner, docker, NATIVE)
    with pytest.raises(UsageError, match="name one with --host <name>: central, edge"):
        up(EXAMPLE, None, DEFAULT_OUT, render, True, runner, docker, NATIVE)
    assert len(runner.calls) == 1


@pytest.mark.parametrize(
    ("declared", "machine", "warned"),
    [
        ("amd64", "x86_64", None),
        ("amd64", "AMD64", None),
        ("arm64", "aarch64", None),
        ("arm64", "ARM64", None),
        ("amd64", "arm64", "arm64"),
        ("arm64", "x86_64", "amd64"),
        ("amd64", "riscv64", None),
        ("amd64", "", None),
    ],
)
def test_the_arch_warning_names_the_arch_of_this_machine(
    declared: str, machine: str, warned: str | None
) -> None:
    found = arch_warning(declared, "local", machine)
    if warned is None:
        assert found is None
    else:
        assert found == (
            f"local: the manifest says arch {declared} and this machine is {warned};"
            f" set arch: {warned} under hosts.local to run native images"
        )


def test_up_warns_on_another_arch_and_still_renders(
    here: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runner = FakeCompose()
    assert up(here, None, DEFAULT_OUT, render, True, runner, docker, "aarch64") == 0
    assert len(runner.calls) == 1
    assert capsys.readouterr().err.strip() == (
        "warning: local: the manifest says arch amd64 and this machine is arm64;"
        " set arch: arm64 under hosts.local to run native images"
    )


def test_the_cli_up_prints_the_lines_and_down_prints_its_own(
    here: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["up"]) == 0
    assert capsys.readouterr().out.splitlines()[-3] == f"start:  {UP}"
    assert main(["down"]) == 0
    assert capsys.readouterr().out == f"stop: {DOWN}\n"
    assert main(["down", "--volumes"]) == 0
    assert capsys.readouterr().out == f"stop: {DOWN} --volumes\n"


def test_down_needs_the_render_of_aye_up(here: Path, capsys: pytest.CaptureFixture[str]) -> None:
    runner = FakeCompose()
    assert down(here, None, DEFAULT_OUT, False, True, runner, docker) == 2
    assert runner.calls == []
    assert capsys.readouterr().err.strip() == (
        "rendered/local/compose.yaml does not exist; aye up renders it"
    )


def test_down_with_stop_runs_compose_down(here: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert start(here, FakeCompose()) == 0
    runner = FakeCompose()
    assert down(here, None, DEFAULT_OUT, True, True, runner, docker) == 0
    assert runner.calls == [[*shlex.split(DOWN), "--volumes"]]
    assert capsys.readouterr().out.splitlines()[-1] == f"running: {DOWN} --volumes"
    failing = FakeCompose(3)
    assert down(here, None, DEFAULT_OUT, False, True, failing, docker) == 1
    assert failing.calls == [shlex.split(DOWN)]
    assert capsys.readouterr().err.strip() == f"{DOWN} exited 3"
    absent = FakeCompose()
    assert down(here, None, DEFAULT_OUT, False, True, absent, nowhere) == 2
    assert absent.calls == []
    printed = FakeCompose()
    assert down(here, None, DEFAULT_OUT, False, False, printed, nowhere) == 0
    assert printed.calls == []


def test_run_hands_the_parsed_options_to_up_and_down(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[object, ...]] = []

    def fake_up(*args: object) -> int:
        seen.append(args)
        return 5

    def fake_down(*args: object) -> int:
        seen.append(args)
        return 6

    monkeypatch.setattr(up_module, "up", fake_up)
    monkeypatch.setattr(up_module, "down", fake_down)
    args = argparse.Namespace(
        command="up", manifest=Path("m.yaml"), host="h", out=Path("o"), start=True
    )
    assert up_module.run(args, render) == 5
    args = argparse.Namespace(
        command="down", manifest=Path("m.yaml"), host=None, out=Path("o"), volumes=True, stop=False
    )
    assert up_module.run(args, render) == 6
    assert seen == [
        (Path("m.yaml"), "h", Path("o"), render, True),
        (Path("m.yaml"), None, Path("o"), True, False),
    ]


def test_compose_runner_returns_the_exit_code_of_the_command() -> None:
    assert compose_runner([sys.executable, "-c", "raise SystemExit(4)"]) == 4


def quick_start() -> str:
    text = README.read_text(encoding="utf-8")
    begin = text.index("## Quick start\n")
    return text[begin : text.index("\n## ", begin + 1)]


def test_the_quick_start_runs_up_and_down_and_they_match_the_long_form(
    here: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    section = quick_start()
    short_up = "uv run aye up examples/quickstart.yaml --start"
    short_down = "uv run aye down examples/quickstart.yaml --volumes --stop"
    assert short_up in section
    assert short_down in section
    assert section.index(short_up) < section.index("<details>")
    assert f"{UP}\n" in section
    assert f"{DOWN} --volumes\n" in section
    parsed = build_parser().parse_args(shlex.split(short_up)[3:])
    assert parsed.start
    runner = FakeCompose()
    assert up(here, parsed.host, parsed.out, render, parsed.start, runner, docker, NATIVE) == 0
    assert runner.calls == [shlex.split(UP)]
    parsed = build_parser().parse_args(shlex.split(short_down)[3:])
    stopper = FakeCompose()
    assert down(here, parsed.host, parsed.out, parsed.volumes, parsed.stop, stopper, docker) == 0
    assert stopper.calls == [shlex.split(f"{DOWN} --volumes")]
