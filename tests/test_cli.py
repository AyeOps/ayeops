from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from ayeops.cli import (
    COMMON,
    EXAMPLES,
    OVERVIEW,
    SECRETS_EXAMPLES,
    build_parser,
    example_text,
    main,
    subcommands,
)
from ayeops.report import build_parser as build_report_parser
from tests.conftest import EXAMPLE, example_data


def broken_manifest(tmp_path: Path) -> Path:
    data = example_data()
    del data["services"]["web"]["alerts"]
    data["hosts"]["central"]["compose"] = [str(EXAMPLE.parent / "apps" / "web.compose.yaml")]
    path = tmp_path / "broken.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def test_check_passes_on_the_example(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["check", str(EXAMPLE)]) == 0
    assert "ok" in capsys.readouterr().out


def test_check_fails_and_names_the_rule(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["check", str(broken_manifest(tmp_path))]) == 1
    assert "[service-alerts] services.web" in capsys.readouterr().err


def test_render_refuses_a_manifest_that_fails_check(tmp_path: Path) -> None:
    out = tmp_path / "out"
    assert main(["render", str(broken_manifest(tmp_path)), "-o", str(out)]) == 1
    assert not out.exists()


def test_render_writes_one_compose_project_per_host(tmp_path: Path) -> None:
    assert main(["render", str(EXAMPLE), "-o", str(tmp_path)]) == 0
    assert sorted(p.parent.name for p in tmp_path.glob("*/compose.yaml")) == ["central", "edge"]


def command_names() -> list[str]:
    commands = subcommands(build_parser())
    names = [name for name in commands.choices if name != "report"]
    secrets = subcommands(commands.choices["secrets"])
    report = subcommands(build_report_parser())
    return [
        *names,
        *(f"secrets {name}" for name in secrets.choices),
        *(f"report {name}" for name in report.choices),
        "report",
        "status deploy",
    ]


@pytest.mark.parametrize("command", command_names())
def test_every_help_has_a_description_and_an_example_of_its_command(
    command: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as raised:
        main([*command.split(), "-h"])
    assert raised.value.code == 0
    text = capsys.readouterr().out
    paragraphs = text.split("\n\n")
    assert paragraphs[1].strip()
    assert "\n" not in paragraphs[1].strip()
    epilog = paragraphs[-1]
    assert epilog.startswith(("example:\n", "examples:\n"))
    assert all(line.startswith(f"  aye {command}") for line in epilog.strip().splitlines()[1:])


def test_the_examples_cover_every_command_and_secrets_action() -> None:
    commands = subcommands(build_parser())
    assert set(EXAMPLES) == set(commands.choices) - {"report"}
    assert set(SECRETS_EXAMPLES) == set(subcommands(commands.choices["secrets"]).choices)


def test_aye_alone_prints_the_common_commands_and_the_exit_codes(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main([]) == 0
    out = capsys.readouterr().out
    assert out == OVERVIEW + "\n"
    assert out.startswith("usage: aye <command> [manifest] [options]\n")
    for line in (
        "  aye up examples/quickstart.yaml --start ",
        "  aye check ",
        "  aye render ",
        "  aye doctor --all ",
        "  aye diff --all ",
        "  aye deploy --all --parallel 4 ",
        "exit codes: 0 ok, 1 findings, drift or a failed step, 2 usage or environment error",
    ):
        assert line in out


def test_aye_help_ends_with_the_common_commands(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        main(["-h"])
    out = capsys.readouterr().out
    assert out.rstrip().endswith(COMMON)
    assert "usage: aye [-h] [--version] <command> ...\n" in out


def test_example_text_names_one_or_several_examples() -> None:
    assert example_text(["aye check"]) == "example:\n  aye check"
    assert example_text(["aye a", "aye b"]) == "examples:\n  aye a\n  aye b"
