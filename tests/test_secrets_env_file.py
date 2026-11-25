from __future__ import annotations

import hashlib
import os
import re
import stat
from pathlib import Path

import pytest

from ayeops import secretkeys
from ayeops.cli import main
from ayeops.permissions import host_generated
from ayeops.registry import load_modules
from ayeops.secrets import IDENTITY_KEYS, parse_env
from tests.conftest import EXAMPLE, ROOT, example_data, manifest_from

QUICKSTART = ROOT / "examples" / "quickstart.yaml"
QUICK = {"AYE_GRAFANA_ADMIN_PASSWORD": 48, "AYE_GRAFANA_ADMIN_USER": 16}
HEX = re.compile(r"^[0-9a-f]+$")


@pytest.fixture(autouse=True)
def no_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in IDENTITY_KEYS:
        monkeypatch.delenv(key, raising=False)


def gen(target: Path, host: str = "local", manifest: Path = QUICKSTART, *extra: str) -> int:
    command = ["secrets", "gen", "--manifest", str(manifest), "--host", host]
    return main([*command, "--env-file", str(target), *extra])


def output(capsys: pytest.CaptureFixture[str]) -> str:
    captured = capsys.readouterr()
    return captured.out + captured.err


def fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:8]


def test_gen_creates_the_env_file_with_every_generated_secret_of_the_host(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / ".env"
    assert gen(target) == 0
    raw = target.read_bytes()
    assert b"\r" not in raw
    assert raw.endswith(b"\n")
    values = parse_env(raw.decode("utf-8"))
    assert {key: len(value) for key, value in values.items()} == QUICK
    assert all(HEX.match(value) for value in values.values())
    assert raw.decode("utf-8").splitlines() == [f"{key}={values[key]}" for key in sorted(QUICK)]
    shown = target.as_posix()
    assert output(capsys).splitlines() == [
        f"secrets: {key} in {shown}: created, {QUICK[key]} characters,"
        f" sha256 {fingerprint(values[key])}"
        for key in sorted(QUICK)
    ] + [f"secrets: {shown}: generated 2, kept 0 existing value(s)"]


@pytest.mark.skipif(os.name != "posix", reason="Windows has no mode bits for group and others")
def test_the_env_file_is_readable_by_its_owner_only(tmp_path: Path) -> None:
    target = tmp_path / ".env"
    target.write_text("AYE_OTHER=1\n", encoding="utf-8")
    target.chmod(0o644)
    assert gen(target) == 0
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_gen_appends_to_an_existing_file_and_keeps_its_lines_in_order(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / ".env"
    before = [
        "# set by hand",
        "AYE_ZETA='spaced value'",
        "",
        "AYE_GRAFANA_ADMIN_USER=owner",
        "export AYE_ALPHA=1",
    ]
    target.write_bytes((chr(0xFEFF) + "\r\n".join(before)).encode("utf-8"))
    assert gen(target) == 0
    raw = target.read_bytes()
    assert b"\r" not in raw
    lines = raw.decode("utf-8").splitlines()
    assert lines[: len(before)] == before
    assert len(lines) == len(before) + 1
    assert lines[-1].startswith("AYE_GRAFANA_ADMIN_PASSWORD=")
    values = parse_env(raw.decode("utf-8"))
    assert values["AYE_GRAFANA_ADMIN_USER"] == "owner"
    assert values["AYE_ZETA"] == "spaced value"
    assert len(values["AYE_GRAFANA_ADMIN_PASSWORD"]) == 48
    assert output(capsys).splitlines()[-1] == (
        f"secrets: {target.as_posix()}: generated 1, kept 1 existing value(s)"
    )


def test_gen_never_overwrites_a_key_even_an_empty_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / ".env"
    target.write_text("AYE_GRAFANA_ADMIN_USER=\n", encoding="utf-8")
    assert gen(target) == 0
    first = target.read_bytes()
    assert parse_env(first.decode("utf-8"))["AYE_GRAFANA_ADMIN_USER"] == ""
    capsys.readouterr()
    assert gen(target) == 0
    assert target.read_bytes() == first
    assert (
        output(capsys) == f"secrets: {target.as_posix()}: generated 0, kept 2 existing value(s)\n"
    )


def test_a_complete_file_is_not_rewritten(tmp_path: Path) -> None:
    target = tmp_path / ".env"
    text = "AYE_GRAFANA_ADMIN_USER=a\r\nAYE_GRAFANA_ADMIN_PASSWORD=b\r\n"
    target.write_bytes(text.encode("utf-8"))
    assert gen(target) == 0
    assert target.read_bytes() == text.encode("utf-8")


def test_gen_refuses_a_path_that_is_a_directory(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert gen(tmp_path) == 1
    assert list(tmp_path.iterdir()) == []
    assert output(capsys) == (
        f"secrets: {tmp_path.as_posix()} is a directory, name the .env file inside it\n"
    )


def test_gen_refuses_a_missing_directory(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert gen(tmp_path / "rendered" / "local" / ".env") == 1
    assert not (tmp_path / "rendered").exists()
    assert "does not exist, run aye render first" in output(capsys)


def test_gen_refuses_a_file_that_is_not_an_env_file_and_leaves_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / ".env"
    target.write_text("AYE_A=1\nnot an assignment\n", encoding="utf-8")
    assert gen(target) == 1
    assert target.read_text(encoding="utf-8") == "AYE_A=1\nnot an assignment\n"
    assert output(capsys) == (
        f"secrets: {target.as_posix()}: line 2 is not KEY=value, nothing written\n"
    )


def test_gen_refuses_a_file_that_is_not_utf8(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / ".env"
    target.write_bytes(b"AYE_A=\xff\n")
    assert gen(target) == 1
    assert target.read_bytes() == b"AYE_A=\xff\n"
    assert "is not UTF-8, nothing written" in output(capsys)


def test_a_last_line_without_a_newline_gets_one_before_the_new_keys(tmp_path: Path) -> None:
    target = tmp_path / ".env"
    target.write_text("AYE_GRAFANA_ADMIN_USER=owner", encoding="utf-8")
    assert gen(target) == 0
    lines = target.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "AYE_GRAFANA_ADMIN_USER=owner"
    assert lines[1].startswith("AYE_GRAFANA_ADMIN_PASSWORD=")


def test_no_value_reaches_the_output(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    target = tmp_path / ".env"
    assert gen(target, "central", EXAMPLE) == 0
    printed = output(capsys)
    values = parse_env(target.read_text(encoding="utf-8"))
    assert values
    assert not any(value in printed for value in values.values())


def test_every_generated_secret_of_a_host_gets_its_catalog_shape(tmp_path: Path) -> None:
    target = tmp_path / ".env"
    assert gen(target, "central", EXAMPLE) == 0
    values = parse_env(target.read_text(encoding="utf-8"))
    wanted = host_generated(manifest_from(example_data()), load_modules(), "central")
    assert sorted(values) == [item.env for item in wanted]
    for item in wanted:
        assert len(values[item.env]) == secretkeys.length(item.format, item.size or 0), item.env


def test_the_variables_no_module_generates_are_named_for_the_person(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / ".env"
    target.write_text("AYE_SMTP_USER=mailer\n", encoding="utf-8")
    assert gen(target, "central", EXAMPLE) == 0
    lines = output(capsys).splitlines()
    shown = target.as_posix()
    must = [line for line in lines if line.endswith("you must set it")]
    assert f"secrets: AYE_SMTP_PASSWORD in {shown}: missing, not generated, you must set it" in (
        must
    )
    assert not any("AYE_SMTP_USER" in line for line in lines)
    made = {
        item.env
        for item in host_generated(manifest_from(example_data()), load_modules(), "central")
    }
    assert not any(line.split(" ")[1] in made for line in must)
    example = (ROOT / "examples" / "rendered" / "central" / ".env.example").read_text(
        encoding="utf-8"
    )
    named = {line.split(" ")[1] for line in must}
    expected = set(parse_env(example)) - set(parse_env(target.read_text(encoding="utf-8")))
    assert named == expected
    assert lines.index(must[0]) > max(i for i, line in enumerate(lines) if "generated " in line)


def test_the_copies_are_left_to_their_owner(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / ".env"
    assert gen(target, "edge", EXAMPLE) == 0
    values = parse_env(target.read_text(encoding="utf-8"))
    assert "AYE_OFFSITE_SECRET_ACCESS_KEY" not in values
    assert "AYE_OFFSITE_RESTIC_PASSWORD" in values
    assert (
        f"secrets: AYE_OFFSITE_SECRET_ACCESS_KEY in {target.as_posix()}: missing, copy"
        " AYE_OBJECTSTORE_SECRET_ACCESS_KEY on central, when it runs Garage"
    ) in output(capsys).splitlines()


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        (["--folder", "/web"], "talks to no provider; drop --path"),
        (["--url", "https://x.example", "-e", "prod"], "drop --url, --environment"),
        (["--identity", "id.env"], "drop --identity"),
        (["--bytes", "16"], "drop KEY, --format and --bytes"),
        (["--format", "hex"], "drop KEY, --format and --bytes"),
        (["AYE_X"], "drop KEY, --format and --bytes"),
    ],
)
def test_gen_to_a_file_refuses_provider_and_shape_flags(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], extra: list[str], message: str
) -> None:
    target = tmp_path / ".env"
    assert gen(target, "local", QUICKSTART, *extra) == 1
    assert not target.exists()
    assert message in output(capsys)


@pytest.mark.parametrize(
    ("command", "message"),
    [
        (["--env-file", ".env"], "--env-file needs a manifest; no manifest: name its path"),
        (
            ["--manifest", str(QUICKSTART), "--host", "nowhere", "--env-file", ".env"],
            "--host must name a host of",
        ),
    ],
)
def test_gen_to_a_file_needs_a_manifest_and_one_of_its_hosts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: list[str],
    message: str,
) -> None:
    monkeypatch.chdir(tmp_path)
    assert main(["secrets", "gen", *command]) == 1
    assert list(tmp_path.iterdir()) == []
    assert message in output(capsys)


def test_gen_to_a_file_takes_the_only_host_and_the_manifest_here(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "ayeops.yaml").write_text(QUICKSTART.read_text(encoding="utf-8"), "utf-8")
    assert main(["secrets", "gen", "--env-file", ".env"]) == 0
    assert ".env: generated 2, kept 0 existing value(s)" in output(capsys)
    assert set(parse_env((tmp_path / ".env").read_text(encoding="utf-8"))) == {
        "AYE_GRAFANA_ADMIN_USER",
        "AYE_GRAFANA_ADMIN_PASSWORD",
    }


def test_gen_to_a_file_of_a_manifest_with_errors_writes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    broken = tmp_path / "broken.yaml"
    text = QUICKSTART.read_text(encoding="utf-8").replace("retention: 7d", "retention: ages")
    broken.write_text(text, encoding="utf-8")
    target = tmp_path / ".env"
    assert gen(target, "local", broken) == 1
    assert not target.exists()
    assert "has errors, run aye check first" in output(capsys)
