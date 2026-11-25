from __future__ import annotations

import os
import re
import shutil
import subprocess
from functools import cache
from pathlib import Path
from typing import Any

import pytest

from ayeops import doctor as doctor_module
from ayeops.cli import main
from ayeops.deploy import HELPER, TIMED_OUT, Result, hostside
from ayeops.doctor import (
    TIMEOUT,
    doctor,
    doctor_command,
    doctor_script,
    grid,
    host_images,
    parse_facts,
    platforms,
    required_env,
)
from ayeops.manifest import Manifest, load_manifest
from ayeops.registry import Module, load_modules
from ayeops.render import render_tree
from ayeops.rules import Finding, check_manifest
from tests.conftest import EXAMPLE, WAIT, example_data, manifest_from

NOW = 1_800_000_000.0
DF_HALF = "/dev/sda1 1000000 500000 500000 50% /"
SECRETS = ("topsecret-value", "x y z", "#hash-value")
FAKE_DOCKER = """#!/bin/sh
case "$1" in
  --version) echo "Docker version 27.3.1, build ce12230" ;;
  compose) echo 2.29.7 ;;
  info) echo {root} ;;
  image) exit 1 ;;
  manifest) cat {manifest} ;;
esac
"""
MANIFESTS = Path(__file__).parent / "fixtures" / "manifests"
INDEX = (MANIFESTS / "index-amd64-arm64.json").read_text("utf-8")
AMD64_ONLY = (MANIFESTS / "list-amd64.json").read_text("utf-8")
SINGLE_ARM64 = (MANIFESTS / "single-arm64-verbose.json").read_text("utf-8")
needs_sh = pytest.mark.skipif(not shutil.which("sh"), reason="needs sh")
SUDO_FIXTURES = Path(__file__).parent / "fixtures" / "sudo"
CLASSIC_SUDO = "Sudo version 1.9.16p2"
SUDO_RS = "sudo-rs 0.2.8"
REQUIRETTY = "/etc/sudoers.d/ayeops-aye-central:1:15: unknown setting: 'requiretty'"
FAKE_SUDO = """#!/bin/sh
echo "$*" >> "$SUDO_CALLS"
if [ "$1" = -V ]; then
  cat "$SUDO_V"
  exit 0
fi
cat "$SUDO_ERR" >&2
[ -n "$SUDO_OK" ]
"""


def with_lines(text: str, *lines: str) -> str:
    return text.replace("\nend\n", "".join(f"\n{line}" for line in lines) + "\nend\n")


def central_images() -> list[str]:
    return host_images(example_tree()["central/compose.yaml"])


def facts(**changes: str | None) -> str:
    values: dict[str, str | None] = {
        "user": "aye",
        "uname": "aarch64",
        "clock": str(int(NOW)),
        "docker": "Docker version 27.3.1, build ce12230",
        "compose": "2.29.7",
        "daemon": "ok",
        "dockerroot": "/var/lib/docker",
        "dir": "present",
        "writable": "yes",
        "helper": None,
        "sudo": None,
        "sudoversion": CLASSIC_SUDO,
        "dfdir": DF_HALF,
        "dfdocker": DF_HALF,
        "memkb": str(12 * 1024 * 1024),
        "env": "read",
    }
    values.update(changes)
    lines = [f"{key} {value}" for key, value in values.items() if value is not None]
    return "\n".join([*lines, "end", ""])


class FakeHost:
    def __init__(self, out: str | dict[str, str] = "", code: int = 0, err: str = "") -> None:
        self.out = out
        self.code = code
        self.err = err
        self.calls: list[tuple[list[str], bytes | None, float]] = []

    def __call__(self, argv: list[str], stdin: bytes | None, timeout: float) -> Result:
        self.calls.append((argv, stdin, timeout))
        out = self.out if isinstance(self.out, str) else self.out[argv[1]]
        return Result(self.code, out, self.err)


def clock() -> float:
    return NOW


@cache
def example() -> Manifest:
    return load_manifest(EXAMPLE)


@cache
def example_findings() -> list[Finding]:
    return check_manifest(example(), load_modules())


@cache
def example_tree() -> dict[str, str]:
    return render_tree(example(), load_modules())


def cached_check(manifest: Manifest, modules: dict[str, Module]) -> list[Finding]:
    return example_findings() if manifest is example() else check_manifest(manifest, modules)


def cached_render(manifest: Manifest, modules: dict[str, Module]) -> dict[str, str]:
    return example_tree() if manifest is example() else render_tree(manifest, modules)


@pytest.fixture(autouse=True)
def ssh_on_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(doctor_module, "load_manifest", lambda path: example())
    monkeypatch.setattr(doctor_module, "check_manifest", cached_check)
    monkeypatch.setattr(doctor_module, "render_tree", cached_render)


def use_manifest(monkeypatch: pytest.MonkeyPatch, data: dict[str, Any]) -> None:
    monkeypatch.setattr(doctor_module, "load_manifest", lambda path: manifest_from(data))


def table_of(out: str) -> list[list[str]]:
    return [re.split(r"\s{2,}", line.strip()) for line in out.splitlines()[1:]]


def table(capsys: pytest.CaptureFixture[str]) -> list[list[str]]:
    return table_of(capsys.readouterr().out)


def check_of(lines: list[list[str]], host: str, check: str) -> list[str]:
    hits = [line[2:] for line in lines if line[:2] == [host, check]]
    assert len(hits) == 1, (host, check, lines)
    return hits[0]


def test_required_env_reads_only_the_variables_compose_refuses_to_start_without() -> None:
    compose = "a: ${AYE_A:?set AYE_A}\nb: ${AYE_B:-}\nc: ${AYE_C}\nd: ${AYE_A:?again}\n"
    assert required_env(compose) == ["AYE_A"]


def test_required_env_of_the_example_matches_its_env_example() -> None:
    tree = example_tree()
    example = [line.split("=")[0] for line in tree["edge/.env.example"].splitlines()]
    required = required_env(tree["edge/compose.yaml"])
    assert set(example) - set(required) == {"AYE_SENTINEL_PING_URL"}
    assert set(required) <= set(example)


def test_parse_facts_keeps_the_first_value_and_collects_names() -> None:
    parsed = parse_facts("user a\nuser b\nmissing X\nempty Y\nmissing \nmissing Z\nend\n")
    assert parsed.get("user") == "a"
    assert parsed.missing == ["X", "Z"]
    assert parsed.empty == ["Y"]
    assert parsed.finished
    assert not parse_facts("user a\n").finished
    assert parse_facts("").get("user") == ""


def test_a_healthy_host_passes_every_check(capsys: pytest.CaptureFixture[str]) -> None:
    images = central_images()
    runner = FakeHost(with_lines(facts(), *(f"image {image} linux/arm64" for image in images)))
    assert doctor(EXAMPLE, ["central"], runner=runner, clock=clock) == 0
    lines = table(capsys)
    assert [line[:3] for line in lines if line[1] != "image"] == [
        ["local", "manifest", "ok"],
        ["local", "ssh", "ok"],
        ["central", "ssh", "ok"],
        ["central", "docker", "ok"],
        ["central", "compose", "ok"],
        ["central", "daemon", "ok"],
        ["central", "arch", "ok"],
        ["central", "dir", "ok"],
        ["central", "disk dir", "ok"],
        ["central", "disk docker", "ok"],
        ["central", "clock", "ok"],
        ["central", "memory", "ok"],
        ["central", "env", "ok"],
    ]
    assert check_of(lines, "central", "docker") == ["ok", "27.3.1"]
    assert check_of(lines, "central", "compose") == ["ok", "2.29.7"]
    assert check_of(lines, "central", "arch") == ["ok", "arm64 (aarch64)"]
    assert check_of(lines, "central", "disk dir") == [
        "ok",
        "50 % free (0 GiB) on /opt/ayeops/central",
    ]
    assert check_of(lines, "central", "clock") == ["ok", "0 s off the local clock"]
    assert check_of(lines, "central", "memory") == ["ok", "12288 MiB, the manifest says 12g"]
    assert check_of(lines, "central", "env")[0] == "ok"
    assert check_of(lines, "central", "env")[1].endswith("required variable(s) set")
    assert lines[-len(images) :] == [
        ["central", "image", "ok", f"{image}: on the host, linux/arm64"] for image in images
    ]


def test_the_whole_check_is_one_ssh_call_with_names_and_no_stdin() -> None:
    runner = FakeHost(facts(uname="x86_64"))
    doctor(EXAMPLE, ["edge"], runner=runner, clock=clock, timeout=9)
    assert len(runner.calls) == 1
    argv, stdin, timeout = runner.calls[0]
    assert argv[:2] == ["ssh", "edge"]
    assert len(argv) == 3
    assert stdin is None
    assert timeout == 9
    command = argv[2]
    assert command.startswith("sh -c ")
    assert "AYE_OFFSITE_RESTIC_PASSWORD" in command
    assert "AYE_SENTINEL_PING_URL" not in command
    assert "/opt/ayeops/edge" in command
    assert TIMEOUT == 60


def test_sudo_runs_the_script_under_sudo_and_the_helper_as_the_login() -> None:
    assert doctor_command("/opt/ayeops/a", True, False, []).startswith("sudo sh -c ")
    helper = doctor_command("/opt/ayeops/a", False, True, ["AYE_X"])
    assert helper.startswith("sh -c ")
    assert HELPER in helper
    script = doctor_script("/opt/a b", "plain", ["AYE_X", "AYE_Y"])
    assert script.splitlines()[0] == f"set -- '/opt/a b' plain {HELPER} none '' AYE_X AYE_Y"
    timers = doctor_script("/opt/a", "helper", ["AYE_X"], timers=True)
    assert timers.splitlines()[0] == f"set -- /opt/a helper {HELPER} timers '' AYE_X"
    assert doctor_command("/opt/a", False, True, [], True).startswith(
        f"sh -c 'set -- /opt/a helper {HELPER} timers "
    )
    script = doctor_script("/opt/a", "sudo", ["AYE_X"], images=["busybox:1.37.0", "a/b:1"])
    assert script.splitlines()[0] == (
        f"set -- /opt/a sudo {HELPER} none 'busybox:1.37.0 a/b:1' AYE_X"
    )


@pytest.mark.parametrize(
    ("result", "detail"),
    [
        (
            Result(255, "", "ssh: connect to host central port 22: No route to host\n"),
            "ssh: connect to host central port 22: No route to host",
        ),
        (Result(TIMED_OUT, "", "ssh timed out after 5s"), "ssh timed out after 5s"),
        (
            Result(64, "", "aye-host: this key runs sudo -n /x <directory> <step> only\n"),
            "the key runs only the aye-host helper, use a login with a shell via --ssh",
        ),
        (Result(1, "", "sudo: a password is required\n"), "sudo: a password is required"),
        (Result(0, "user aye\nuname aarch64\n", ""), "ssh exited 0"),
    ],
)
def test_an_unreachable_host_fails_with_the_reason(
    result: Result, detail: str, capsys: pytest.CaptureFixture[str]
) -> None:
    runner = FakeHost(result.out, result.code, result.err)
    assert doctor(EXAMPLE, ["central"], runner=runner, clock=clock, timeout=5) == 1
    lines = table(capsys)
    assert [line for line in lines if line[0] == "central"] == [["central", "ssh", "fail", detail]]


def test_a_runner_that_raises_fails_that_host_only(capsys: pytest.CaptureFixture[str]) -> None:
    def broken(argv: list[str], stdin: bytes | None, timeout: float) -> Result:
        raise OSError("no ssh")

    assert doctor(EXAMPLE, ["central"], runner=broken, clock=clock) == 1
    assert check_of(table(capsys), "central", "ssh") == ["fail", "OSError('no ssh')"]


@pytest.mark.parametrize(
    ("changes", "check", "expected"),
    [
        ({"docker": None, "compose": None, "daemon": None}, "docker", "fail"),
        ({"docker": None, "compose": None, "daemon": None}, "compose", "fail"),
        ({"compose": ""}, "compose", "fail"),
        ({"compose": "2.21.9"}, "compose", "fail"),
        ({"compose": "1.29.2"}, "compose", "fail"),
        ({"compose": "2.22.0"}, "compose", "ok"),
        ({"compose": "v5.0.1"}, "compose", "ok"),
        ({"daemon": "fail"}, "daemon", "fail"),
        ({"daemon": "timeout"}, "daemon", "fail"),
        ({"uname": "x86_64"}, "arch", "fail"),
        ({"uname": "armv7l"}, "arch", "fail"),
        ({"uname": ""}, "arch", "fail"),
        ({"dir": "absent /opt"}, "dir", "ok"),
        ({"dir": "absent /opt", "writable": "no"}, "dir", "fail"),
        ({"writable": "no"}, "dir", "fail"),
        ({"dir": "other"}, "dir", "fail"),
        ({"dfdir": "/dev/sda1 1000000 900000 140000 90% /"}, "disk dir", "warn"),
        ({"dfdir": "/dev/sda1 1000000 960000 40000 96% /"}, "disk dir", "fail"),
        ({"dfdir": "/dev/sda1 1000000 850000 150000 85% /"}, "disk dir", "ok"),
        ({"dfdir": "/dev/sda1 1000000 950000 50000 95% /"}, "disk dir", "warn"),
        ({"dfdir": ""}, "disk dir", "warn"),
        ({"dfdir": "/dev/x 0 0 0 - /"}, "disk dir", "warn"),
        ({"dfdocker": "/dev/sdb 1000000 900000 100000 90% /var"}, "disk docker", "warn"),
        ({"dfdocker": "/dev/sdb 1000000 990000 10000 99% /var"}, "disk docker", "fail"),
        ({"clock": str(int(NOW) + 31)}, "clock", "warn"),
        ({"clock": str(int(NOW) - 31)}, "clock", "warn"),
        ({"clock": str(int(NOW) + 30)}, "clock", "ok"),
        ({"clock": ""}, "clock", "warn"),
        ({"memkb": str(8 * 1024 * 1024)}, "memory", "warn"),
        ({"memkb": str(int(11.6 * 1024 * 1024))}, "memory", "ok"),
        ({"memkb": None}, "memory", "warn"),
        ({"env": "absent"}, "env", "fail"),
        ({"env": "unreadable"}, "env", "warn"),
    ],
)
def test_each_check_reports_ok_warn_or_fail(
    changes: dict[str, str | None],
    check: str,
    expected: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = doctor(EXAMPLE, ["central"], runner=FakeHost(facts(**changes)), clock=clock)
    assert check_of(table(capsys), "central", check)[0] == expected
    assert code == (1 if expected == "fail" else 0)


def test_the_details_name_what_is_wrong(capsys: pytest.CaptureFixture[str]) -> None:
    out = facts(uname="x86_64", daemon="fail", dir="absent /opt", writable="no", user="bob")
    doctor(EXAMPLE, ["central"], runner=FakeHost(out), clock=clock)
    lines = table(capsys)
    assert check_of(lines, "central", "arch")[1] == (
        "host is amd64 (x86_64), the manifest says arm64"
    )
    assert check_of(lines, "central", "daemon")[1].startswith("bob cannot reach the docker daemon")
    assert check_of(lines, "central", "dir")[1] == (
        "/opt/ayeops/central is absent and bob cannot create it in /opt"
    )
    doctor(EXAMPLE, ["central"], runner=FakeHost(facts(daemon="timeout")), clock=clock)
    assert check_of(table(capsys), "central", "daemon") == [
        "fail",
        "docker info did not answer aye within 10 s",
    ]


def test_a_clock_inside_the_round_trip_is_not_skewed(capsys: pytest.CaptureFixture[str]) -> None:
    ticks = iter([NOW - 20, NOW + 20])
    out = facts(clock=str(int(NOW + 10)))
    doctor(EXAMPLE, ["central"], runner=FakeHost(out), clock=lambda: next(ticks))
    assert check_of(table(capsys), "central", "clock") == ["ok", "0 s off the local clock"]


def test_the_docker_disk_names_an_assumed_root(capsys: pytest.CaptureFixture[str]) -> None:
    out = facts(dockerroot="/var/lib/docker assumed", daemon="fail")
    doctor(EXAMPLE, ["central"], runner=FakeHost(out), clock=clock, sudo=True)
    detail = check_of(table(capsys), "central", "disk docker")[1]
    assert detail.endswith("on /var/lib/docker (assumed)")


def test_no_docker_disk_row_without_a_docker_root(capsys: pytest.CaptureFixture[str]) -> None:
    doctor(EXAMPLE, ["central"], runner=FakeHost(facts(dfdocker=None)), clock=clock)
    assert not [line for line in table(capsys) if line[1] == "disk docker"]


def test_a_host_without_declared_memory_has_no_memory_row(
    data: dict[str, Any], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    del data["hosts"]["central"]["memory"]
    del data["hosts"]["central"]["reserved"]
    use_manifest(monkeypatch, data)
    assert doctor(EXAMPLE, ["central"], runner=FakeHost(facts()), clock=clock) == 0
    assert not [line for line in table(capsys) if line[1] == "memory"]


def test_missing_and_empty_variables_are_named(capsys: pytest.CaptureFixture[str]) -> None:
    out = facts().replace("end\n", "missing AYE_SMTP_USER\nempty AYE_GITHUB_TOKEN\nend\n")
    assert doctor(EXAMPLE, ["central"], runner=FakeHost(out), clock=clock) == 1
    assert check_of(table(capsys), "central", "env") == [
        "fail",
        "missing AYE_SMTP_USER; empty AYE_GITHUB_TOKEN",
    ]


def test_the_helper_mode_checks_the_helper_and_not_the_login_daemon(
    capsys: pytest.CaptureFixture[str],
) -> None:
    out = facts(daemon="fail", helper="present", sudo="yes", writable="no")
    assert doctor(EXAMPLE, ["central"], runner=FakeHost(out), clock=clock, helper=True) == 0
    lines = table(capsys)
    assert not [line for line in lines if line[1] == "daemon"]
    assert check_of(lines, "central", "dir") == ["ok", "/opt/ayeops/central exists"]
    assert check_of(lines, "central", "helper") == [
        "ok",
        f"sudo -n may run {HELPER} /opt/ayeops/central",
    ]


@pytest.mark.parametrize(
    ("changes", "check"),
    [
        ({"helper": "present", "sudo": "yes", "dir": "absent /opt/ayeops"}, "dir"),
        ({"helper": "present", "sudo": "yes", "dir": "link"}, "dir"),
        ({"helper": "absent", "sudo": "no"}, "helper"),
        ({"helper": "present", "sudo": "no"}, "helper"),
    ],
)
def test_the_helper_mode_fails_a_host_that_host_init_did_not_prepare(
    changes: dict[str, str], check: str, capsys: pytest.CaptureFixture[str]
) -> None:
    runner = FakeHost(facts(**changes))
    assert doctor(EXAMPLE, ["central"], runner=runner, clock=clock, helper=True) == 1
    result, detail = check_of(table(capsys), "central", check)
    assert result == "fail"
    assert detail.endswith("run aye host-init")


def test_all_checks_every_host_in_manifest_order(capsys: pytest.CaptureFixture[str]) -> None:
    edge = facts(uname="x86_64", memkb="16777216", systemd="yes")
    runner = FakeHost({"central": facts(), "edge": edge})
    assert doctor(EXAMPLE, None, runner=runner, clock=clock, parallel=2) == 0
    hosts = [line[0] for line in table(capsys)]
    assert hosts.index("central") < hosts.index("edge")
    assert sorted(argv[1] for argv, _, _ in runner.calls) == ["central", "edge"]


def test_one_failing_host_fails_the_run_but_not_the_others(
    capsys: pytest.CaptureFixture[str],
) -> None:
    runner = FakeHost({"central": facts(env="absent"), "edge": facts(uname="x86_64")})
    assert doctor(EXAMPLE, ["central", "edge"], runner=runner, clock=clock) == 1
    lines = table(capsys)
    assert check_of(lines, "central", "env")[0] == "fail"
    assert check_of(lines, "edge", "env")[0] == "ok"


def test_a_manifest_that_fails_the_check_stops_before_ssh(
    data: dict[str, Any], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    del data["services"]["web"]["description"]
    use_manifest(monkeypatch, data)
    runner = FakeHost(facts())
    assert doctor(EXAMPLE, ["central"], runner=runner, clock=clock) == 1
    assert runner.calls == []
    captured = capsys.readouterr()
    assert "[service-description] services.web" in captured.err
    assert check_of(table_of(captured.out), "local", "manifest") == [
        "fail",
        "1 error(s) of aye check, see above",
    ]


def test_without_ssh_on_path_nothing_connects(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: None)
    runner = FakeHost(facts())
    assert doctor(EXAMPLE, ["central"], runner=runner, clock=clock) == 1
    assert runner.calls == []
    assert check_of(table(capsys), "local", "ssh") == ["fail", "ssh is not on PATH"]


@pytest.mark.parametrize(
    ("hosts", "options", "message"),
    [
        (["nowhere"], {}, "unknown host 'nowhere'"),
        (None, {"ssh": "a@b"}, "--ssh names the destination of one host"),
        (["central"], {"helper": True, "directory": "/srv/x"}, "--helper deploys only to"),
        (["central"], {"helper": True, "sudo": True}, "exclude each other"),
    ],
)
def test_bad_arguments_exit_2_before_any_ssh(
    hosts: list[str] | None,
    options: dict[str, Any],
    message: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runner = FakeHost(facts())
    assert doctor(EXAMPLE, hosts, runner=runner, clock=clock, **options) == 2
    assert runner.calls == []
    assert message in capsys.readouterr().err


def test_the_cli_passes_every_option_through(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[Any, ...]] = []

    def fake(*args: Any, **kwargs: Any) -> int:
        seen.append((*args, kwargs))
        return 3

    monkeypatch.setattr(doctor_module, "doctor", fake)
    argv = ["doctor", "m.yaml", "--host", "h", "--ssh", "a", "--sudo", "--dir", "/x"]
    assert main([*argv, "--timeout", "7", "--parallel", "2"]) == 3
    options: dict[str, Any] = {"timeout": 7.0, "helper": False, "parallel": 2}
    assert seen[0] == (Path("m.yaml"), ["h"], "a", True, "/x", options)
    assert main(["doctor", "m.yaml", "--all", "--helper"]) == 3
    defaults: dict[str, Any] = {"timeout": TIMEOUT, "helper": True, "parallel": 1}
    assert seen[1] == (Path("m.yaml"), None, None, False, None, defaults)


def test_the_cli_needs_hosts_and_one_mode(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["doctor", str(EXAMPLE)]) == 2
    assert "name one with --host <name> or --all" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        main(["doctor", "m.yaml", "--all", "--sudo", "--helper"])
    assert "not allowed with argument" in capsys.readouterr().err


def test_grid_aligns_the_columns() -> None:
    assert grid([("h", "env", "ok", "x"), ("host", "c", "fail", "")]).splitlines() == [
        "HOST  CHECK  RESULT  DETAIL",
        "h     env    ok      x",
        "host  c      fail",
    ]


def env_lines(script: str) -> list[str]:
    return [line.strip() for line in script.splitlines() if "$E" in line]


def test_the_script_reads_the_env_file_only_through_quiet_grep() -> None:
    script = hostside("doctor.sh")
    for line in env_lines(script):
        assert line.startswith(("elif [ ! -e", "elif [ ! -r", "if ! grep -Eq", "elif ! grep -Eq"))
        if "grep" in line:
            assert line.endswith('"$E" 2>/dev/null; then')
    assert len(env_lines(script)) == 4
    for word in ("cat ", "source ", ". ", "sed ", "awk ", "head ", "set -x", "read -r n", "$("):
        assert all(word not in line for line in env_lines(script))
    assert "set -x" not in script
    assert "E=$D/.env" in script


def fake_docker(tmp_path: Path) -> dict[str, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    docker = bin_dir / "docker"
    index = (MANIFESTS / "index-amd64-arm64.json").as_posix()
    docker.write_text(FAKE_DOCKER.format(root=tmp_path.as_posix(), manifest=index), "utf-8")
    docker.chmod(0o755)
    return {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"}


def sh_c(command: str, tmp_path: Path, timeout: float = WAIT) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["sh", "-c", command],
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
        env=fake_docker(tmp_path),
    )


def run_script(
    directory: Path, names: list[str], tmp_path: Path
) -> subprocess.CompletedProcess[str]:
    return sh_c(doctor_script(directory.as_posix(), "plain", names), tmp_path)


@needs_sh
def test_the_script_names_missing_and_empty_variables_and_never_prints_a_value(
    tmp_path: Path,
) -> None:
    target = tmp_path / "central"
    target.mkdir()
    (target / ".env").write_text(
        "\n".join(
            [
                f"AYE_SET={SECRETS[0]}",
                "AYE_BLANK=",
                f'export AYE_QUOTED="{SECRETS[1]}"',
                'AYE_EMPTY_QUOTES=""',
                "AYE_EMPTY_SINGLE=''",
                "AYE_COMMENT= # nothing",
                f"AYE_HASH={SECRETS[2]}",
                f"  AYE_SPACED = {SECRETS[0]}",
                f"AYE_SETTING={SECRETS[0]}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    names = [
        "AYE_SET",
        "AYE_BLANK",
        "AYE_QUOTED",
        "AYE_EMPTY_QUOTES",
        "AYE_EMPTY_SINGLE",
        "AYE_COMMENT",
        "AYE_HASH",
        "AYE_SPACED",
        "AYE_SETT",
        "AYE_ABSENT",
        "bad;name",
    ]
    done = run_script(target, names, tmp_path)
    assert done.returncode == 0, done.stderr
    parsed = parse_facts(done.stdout)
    assert parsed.finished
    assert parsed.get("env") == "read"
    assert parsed.get("dir") == "present"
    assert parsed.get("writable") == "yes"
    assert parsed.missing == ["AYE_SETT", "AYE_ABSENT"]
    assert parsed.empty == ["AYE_BLANK", "AYE_EMPTY_QUOTES", "AYE_EMPTY_SINGLE", "AYE_COMMENT"]
    for secret in (*SECRETS, "topsecret", "nothing"):
        assert secret not in done.stdout
        assert secret not in done.stderr


@needs_sh
def test_the_script_reports_an_absent_directory_and_env(tmp_path: Path) -> None:
    done = run_script(tmp_path / "a" / "b", ["AYE_X"], tmp_path)
    parsed = parse_facts(done.stdout)
    assert parsed.finished
    assert parsed.get("dir") == f"absent {tmp_path.as_posix()}"
    assert parsed.get("env") == "absent"
    assert parse_facts(run_script(tmp_path, [], tmp_path).stdout).get("env") == "none"


@needs_sh
def test_the_script_runs_end_to_end_through_the_probe(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "edge"
    target.mkdir()
    (target / ".env").write_text(f"AYE_UPDATES_TOKEN={SECRETS[0]}\n", encoding="utf-8")

    def local(argv: list[str], stdin: bytes | None, timeout: float) -> Result:
        done = sh_c(argv[2], tmp_path, timeout)
        return Result(done.returncode, done.stdout, done.stderr)

    doctor(EXAMPLE, ["edge"], directory=target.as_posix(), runner=local, timeout=600)
    captured = capsys.readouterr()
    lines = table_of(captured.out)
    assert check_of(lines, "edge", "ssh")[0] == "ok"
    assert check_of(lines, "edge", "dir")[0] == "ok"
    assert check_of(lines, "edge", "clock")[0] == "ok"
    assert check_of(lines, "edge", "docker") == ["ok", "27.3.1"]
    assert check_of(lines, "edge", "compose") == ["ok", "2.29.7"]
    assert check_of(lines, "edge", "daemon")[0] == "ok"
    assert check_of(lines, "edge", "disk docker")[0] in ("ok", "warn", "fail")
    result, detail = check_of(lines, "edge", "env")
    assert result == "fail"
    assert "AYE_UPDATES_TOKEN" not in detail
    assert "missing AYE_OFFSITE_ACCESS_KEY_ID" in detail
    assert SECRETS[0] not in captured.out + captured.err
    images = [line for line in lines if line[:2] == ["edge", "image"]]
    assert len(images) == len(host_images(example_tree()["edge/compose.yaml"])) > 0
    assert all(line[2] == "ok" and line[3].endswith(" in the registry") for line in images)


GRAFANA_TAG = "grafana/grafana:13.2.3"
GRAFANA = next(image for image in central_images() if image.startswith(f"{GRAFANA_TAG}@sha256:"))
NO_PLATFORM = '{"schemaVersion": 2, "config": {}, "layers": []}'
ATTESTATION_ONLY = '{"manifests": [{"platform": {"os": "unknown", "architecture": "unknown"}}]}'


def one_line(text: str) -> str:
    return text.replace("\r", "").replace("\n", "")


def test_host_images_lists_each_pulled_image_once_and_skips_builds() -> None:
    compose = """services:
  a: {image: "grafana/grafana:13.2.3"}
  b: {image: "grafana/grafana:13.2.3"}
  c: {image: "app:1", build: ./app}
  d: {build: ./d}
  e: {image: "${REGISTRY}/x:1"}
  f: {image: "busybox:1.37.0-musl"}
  g: "not a mapping"
"""
    assert host_images(compose) == ["busybox:1.37.0-musl", GRAFANA_TAG]
    assert host_images("") == []
    assert host_images("services: []\n") == []
    images = central_images()
    assert GRAFANA in images
    assert images == sorted(set(images))


def test_platforms_reads_an_index_a_list_and_a_verbose_single_manifest() -> None:
    assert platforms(INDEX) == {"linux/amd64", "linux/arm64"}
    assert platforms(one_line(INDEX)) == {"linux/amd64", "linux/arm64"}
    assert platforms(AMD64_ONLY) == {"linux/amd64", "linux/arm"}
    assert platforms(SINGLE_ARM64) == {"linux/arm64"}
    assert platforms(f'[{one_line(SINGLE_ARM64)}, {{"Descriptor": 3}}, 7]') == {"linux/arm64"}
    assert platforms(NO_PLATFORM) is None
    assert platforms(ATTESTATION_ONLY) is None
    assert platforms("not json") is None
    assert platforms('"text"') is None
    assert platforms("") is None


def image_rows_of(
    capsys: pytest.CaptureFixture[str], *lines: str, **changes: str | None
) -> list[list[str]]:
    runner = FakeHost(with_lines(facts(**changes), *lines))
    code = doctor(EXAMPLE, ["central"], runner=runner, clock=clock)
    lines_out = table(capsys)
    assert code == (1 if any(row[2] == "fail" for row in lines_out) else 0)
    return [line for line in lines_out if line[1] == "image"]


def grafana_row(rows: list[list[str]]) -> list[str]:
    found = [row[2:] for row in rows if row[3].startswith(f"{GRAFANA}:")]
    assert len(found) == 1, rows
    return found[0]


@pytest.mark.parametrize(
    ("line", "result", "detail"),
    [
        (f"manifest {GRAFANA} 0 {one_line(INDEX)}", "ok", "linux/arm64 in the registry"),
        (
            f"manifest {GRAFANA} 0 {one_line(AMD64_ONLY)}",
            "fail",
            "no linux/arm64 manifest, only linux/amd64, linux/arm",
        ),
        (f"manifest {GRAFANA} 0 {one_line(SINGLE_ARM64)}", "ok", "linux/arm64 in the registry"),
        (f"image {GRAFANA} linux/arm64", "ok", "on the host, linux/arm64"),
        (f"image {GRAFANA} linux/amd64", "fail", "on the host for linux/amd64, not linux/arm64"),
        (f"image {GRAFANA}", "fail", "on the host for no platform, not linux/arm64"),
        (
            f"manifest {GRAFANA} 1 no such manifest: docker.io/grafana/grafana:13.2.3",
            "fail",
            "the registry has no such image or tag",
        ),
        (
            f"manifest {GRAFANA} 1 MANIFEST_UNKNOWN: Manifest Unknown",
            "fail",
            "the registry has no such image or tag",
        ),
        (
            f"manifest {GRAFANA} 1 errors: denied: requested access to the resource is denied",
            "warn",
            "unreadable as aye: errors: denied: requested access to the resource is denied",
        ),
        (f"manifest {GRAFANA} 1", "warn", "unreadable as aye: exit 1"),
        (f"manifest {GRAFANA} 124", "warn", "the registry did not answer within 10 s"),
        (f"manifest {GRAFANA} 143 Terminated", "warn", "the registry did not answer within 10 s"),
        (f"manifest {GRAFANA} 0 {NO_PLATFORM}", "warn", "the registry answer names no platform"),
        (f"manifest {GRAFANA} 0 not json", "warn", "the registry answer names no platform"),
        (f"manifest {GRAFANA} x {{}}", "warn", "not checked"),
        ("", "warn", "not checked"),
    ],
)
def test_each_image_gets_a_finding_for_the_host_arch(
    line: str, result: str, detail: str, capsys: pytest.CaptureFixture[str]
) -> None:
    rows = image_rows_of(capsys, line)
    assert grafana_row(rows) == [result, f"{GRAFANA}: {detail}"]
    assert len(rows) == len(central_images())


def test_the_image_check_follows_the_arch_the_host_reports(
    capsys: pytest.CaptureFixture[str],
) -> None:
    line = f"manifest {GRAFANA} 0 {one_line(SINGLE_ARM64)}"
    rows = image_rows_of(capsys, line, uname="x86_64")
    assert grafana_row(rows) == ["fail", f"{GRAFANA}: no linux/amd64 manifest, only linux/arm64"]
    rows = image_rows_of(capsys, line, uname="armv7l")
    assert grafana_row(rows) == ["ok", f"{GRAFANA}: linux/arm64 in the registry"]


def test_a_local_image_wins_and_the_first_registry_answer_counts(
    capsys: pytest.CaptureFixture[str],
) -> None:
    failed = f"manifest {GRAFANA} 1 toomanyrequests: {'x' * 400}"
    rows = image_rows_of(
        capsys, failed, f"manifest {GRAFANA} 0 {one_line(INDEX)}", f"image {GRAFANA} linux/arm64"
    )
    assert grafana_row(rows) == ["ok", f"{GRAFANA}: on the host, linux/arm64"]
    rows = image_rows_of(capsys, failed, f"manifest {GRAFANA} 0 {one_line(INDEX)}")
    result, detail = grafana_row(rows)
    prefix = f"{GRAFANA}: unreadable as aye: "
    assert result == "warn"
    assert detail.startswith(f"{prefix}toomanyrequests: xxx")
    assert detail.endswith("...")
    assert len(detail) == len(prefix) + 160


def test_without_docker_no_image_is_checked(capsys: pytest.CaptureFixture[str]) -> None:
    runner = FakeHost(facts(docker=None, compose=None, daemon=None))
    assert doctor(EXAMPLE, ["central"], runner=runner, clock=clock) == 1
    assert [line for line in table(capsys) if line[1] == "image"] == []


def test_the_images_travel_in_the_one_ssh_call() -> None:
    runner = FakeHost(facts())
    doctor(EXAMPLE, ["central"], runner=runner, clock=clock)
    assert len(runner.calls) == 1
    assert f"'{' '.join(central_images())}'" in runner.calls[0][0][2]


@needs_sh
def test_the_script_asks_the_registry_only_for_images_not_on_the_host(tmp_path: Path) -> None:
    env = fake_docker(tmp_path)
    docker = tmp_path / "bin" / "docker"
    local = '  image) if [ "$5" = local:1 ]; then echo linux/arm64; else exit 1; fi ;;'
    docker.write_text(
        docker.read_text("utf-8").replace("  image) exit 1 ;;", local), encoding="utf-8"
    )
    images = ["local:1", "remote/x:2", "bad;ref", "$(id)"]
    script = doctor_script(tmp_path.as_posix(), "plain", [], images=images)
    done = subprocess.run(
        ["sh", "-c", script], capture_output=True, text=True, check=False, timeout=60, env=env
    )
    parsed = parse_facts(done.stdout)
    assert parsed.finished
    assert parsed.local == {"local:1": "linux/arm64"}
    assert list(parsed.manifests) == ["remote/x:2"]
    code, text = parsed.manifests["remote/x:2"]
    assert code == 0
    assert platforms(text) == {"linux/amd64", "linux/arm64"}
    assert "uid=" not in done.stdout


def edge_facts(**changes: str | None) -> str:
    return facts(uname="x86_64", memkb="16777216", **changes)


def test_a_host_with_backup_timers_needs_systemctl(capsys: pytest.CaptureFixture[str]) -> None:
    runner = FakeHost(edge_facts(systemd="yes"))
    assert doctor(EXAMPLE, ["edge"], runner=runner, clock=clock) == 0
    assert check_of(table(capsys), "edge", "timers") == [
        "ok",
        "1 backup timer(s), systemctl present",
    ]
    assert f"set -- /opt/ayeops/edge plain {HELPER} timers " in runner.calls[0][0][2]
    assert doctor(EXAMPLE, ["edge"], runner=FakeHost(edge_facts(systemd="no")), clock=clock) == 1
    assert check_of(table(capsys), "edge", "timers") == [
        "fail",
        "systemctl is missing; the backup jobs with a command need systemd",
    ]
    assert doctor(EXAMPLE, ["edge"], runner=FakeHost(edge_facts()), clock=clock) == 1
    assert check_of(table(capsys), "edge", "timers")[0] == "fail"


def test_a_host_without_backup_timers_has_no_timers_row(
    capsys: pytest.CaptureFixture[str],
) -> None:
    runner = FakeHost(facts(systemd="no"))
    assert doctor(EXAMPLE, ["central"], runner=runner, clock=clock) == 0
    assert not [line for line in table(capsys) if line[1] == "timers"]
    assert f"{HELPER} none" in runner.calls[0][0][2]


def test_the_helper_mode_needs_the_timers_step_in_the_sudo_rule(
    capsys: pytest.CaptureFixture[str],
) -> None:
    ready = edge_facts(helper="present", sudo="yes", systemd="yes", timers="yes")
    assert doctor(EXAMPLE, ["edge"], runner=FakeHost(ready), clock=clock, helper=True) == 0
    assert check_of(table(capsys), "edge", "timers")[0] == "ok"
    old = edge_facts(helper="present", sudo="yes", systemd="yes", timers="no")
    assert doctor(EXAMPLE, ["edge"], runner=FakeHost(old), clock=clock, helper=True) == 1
    assert check_of(table(capsys), "edge", "timers") == [
        "fail",
        f"sudo -n does not allow {HELPER} /opt/ayeops/edge timers, run aye host-init again",
    ]


def test_backup_timers_fail_a_directory_other_than_the_host_default(
    capsys: pytest.CaptureFixture[str],
) -> None:
    runner = FakeHost(edge_facts(systemd="yes"))
    assert doctor(EXAMPLE, ["edge"], directory="/srv/edge", runner=runner, clock=clock) == 1
    assert check_of(table(capsys), "edge", "timers") == [
        "fail",
        "the backup timers run the jobs from /opt/ayeops/edge, not /srv/edge",
    ]


@needs_sh
def test_the_script_reports_systemctl_and_the_timers_step_only_when_asked(
    tmp_path: Path,
) -> None:
    env = fake_docker(tmp_path)
    bin_dir = tmp_path / "bin"
    (bin_dir / "sudo").write_text(
        '#!/bin/sh\n[ "$5" = pull ] || [ -f "$ALLOW_TIMERS" ]\n', encoding="utf-8"
    )
    (bin_dir / "sudo").chmod(0o755)
    target = tmp_path / "edge"
    target.mkdir()

    def run(mode: str, timers: bool, **extra: str) -> Any:
        done = subprocess.run(
            ["sh", "-c", doctor_script(target.as_posix(), mode, [], timers)],
            capture_output=True,
            text=True,
            check=False,
            timeout=WAIT,
            env={**env, **extra},
        )
        assert done.returncode == 0, done.stderr
        return parse_facts(done.stdout)

    assert "systemd" not in run("helper", False).values
    found = run("plain", True)
    assert found.get("systemd") in ("yes", "no")
    assert "timers" not in found.values
    (bin_dir / "systemctl").write_text("#!/bin/sh\n", encoding="utf-8")
    (bin_dir / "systemctl").chmod(0o755)
    found = run("helper", True)
    assert (found.get("systemd"), found.get("timers"), found.get("sudo")) == ("yes", "no", "yes")
    allow = tmp_path / "allow"
    allow.write_text("", encoding="utf-8")
    assert run("helper", True, ALLOW_TIMERS=allow.as_posix()).get("timers") == "yes"


def test_the_helper_mode_fails_when_the_host_does_not_answer_for_the_timers_step(
    capsys: pytest.CaptureFixture[str],
) -> None:
    silent = edge_facts(helper="present", sudo="yes", systemd="yes")
    assert doctor(EXAMPLE, ["edge"], runner=FakeHost(silent), clock=clock, helper=True) == 1
    assert check_of(table(capsys), "edge", "timers")[0] == "fail"
    assert doctor(EXAMPLE, ["edge"], runner=FakeHost(edge_facts(systemd="yes")), clock=clock) == 0


def test_doctor_reaches_each_host_by_its_connect(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    data = example_data()
    data["hosts"]["central"]["connect"] = {"ssh": "ops-central", "sudo": True}
    data["hosts"]["edge"]["connect"] = {"ssh": "admin@edge", "helper": True}
    use_manifest(monkeypatch, data)
    runner = FakeHost({"ops-central": facts(), "admin@edge": edge_facts()})
    doctor(EXAMPLE, None, runner=runner, clock=clock)
    commands = {argv[1]: argv[2] for argv, _, _ in runner.calls}
    assert set(commands) == {"ops-central", "admin@edge"}
    assert commands["ops-central"].startswith("sudo sh -c ")
    assert "set -- /opt/ayeops/central sudo " in commands["ops-central"]
    assert not commands["admin@edge"].startswith("sudo ")
    assert "set -- /opt/ayeops/edge helper " in commands["admin@edge"]
    assert check_of(table(capsys), "central", "ssh")[1] == "ops-central runs as aye"


def test_doctor_refuses_a_connect_helper_outside_the_helper_root(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    data = example_data()
    data["hosts"]["edge"]["connect"] = {"helper": True, "dir": "/srv/edge"}
    use_manifest(monkeypatch, data)
    runner = FakeHost(facts())
    assert doctor(EXAMPLE, ["edge"], runner=runner, clock=clock) == 2
    assert runner.calls == []
    assert capsys.readouterr().err.strip() == (
        "--helper deploys only to /opt/ayeops/<name>, not /srv/edge"
    )


HELPER_READY = {"helper": "present", "sudo": "yes"}


@pytest.mark.parametrize(
    ("mode", "changes", "expected"),
    [
        ("helper", HELPER_READY, ["ok", CLASSIC_SUDO]),
        ("sudo", {}, ["ok", CLASSIC_SUDO]),
        (
            "helper",
            {**HELPER_READY, "sudoversion": SUDO_RS},
            ["ok", f"{SUDO_RS}, every sudoers line parses"],
        ),
        (
            "sudo",
            {"sudoversion": SUDO_RS},
            ["ok", f"{SUDO_RS}, every sudoers line parses"],
        ),
        (
            "helper",
            {"helper": "present", "sudo": "no", "sudoversion": SUDO_RS, "sudoerror": REQUIRETTY},
            [
                "fail",
                f"{SUDO_RS} rejects {REQUIRETTY}; run aye host-init again, its rule suits sudo-rs",
            ],
        ),
        (
            "helper",
            {**HELPER_READY, "sudoversion": SUDO_RS, "sudoerror": REQUIRETTY},
            [
                "warn",
                f"{SUDO_RS} rejects {REQUIRETTY}; run aye host-init again, its rule suits sudo-rs",
            ],
        ),
        (
            "sudo",
            {"sudoversion": SUDO_RS, "sudoerror": REQUIRETTY},
            [
                "warn",
                f"{SUDO_RS} rejects {REQUIRETTY}; run aye host-init again, its rule suits sudo-rs",
            ],
        ),
        (
            "sudo",
            {"sudoversion": SUDO_RS, "sudoerror": "/etc/sudoers.d/ops:3:20: expected command"},
            [
                "warn",
                f"{SUDO_RS} skips /etc/sudoers.d/ops:3:20: expected command; "
                "visudo -c lists every such line",
            ],
        ),
        (
            "sudo",
            {"sudoerror": "/etc/sudoers.d/ops:3:20: expected command"},
            ["ok", CLASSIC_SUDO],
        ),
        ("sudo", {"sudoversion": ""}, ["warn", "sudo -V printed no version"]),
        ("helper", {**HELPER_READY, "sudoversion": None}, ["warn", "sudo -V printed no version"]),
        ("helper", {**HELPER_READY, "sudoversion": "sudo-rsx 1"}, ["ok", "sudo-rsx 1"]),
    ],
)
def test_the_sudo_row_names_the_implementation_and_a_sudoers_line_sudo_rs_rejects(
    mode: str,
    changes: dict[str, str | None],
    expected: list[str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    runner = FakeHost(facts(**changes))
    code = doctor(
        EXAMPLE,
        ["central"],
        runner=runner,
        clock=clock,
        sudo=mode == "sudo",
        helper=mode == "helper",
    )
    lines = table(capsys)
    assert check_of(lines, "central", "sudo") == expected
    assert code == (1 if any(line[2] == "fail" for line in lines) else 0)
    checks = [line[1] for line in lines if line[0] == "central"]
    assert checks.index("dir") < checks.index("sudo") < checks.index("disk dir")


def test_the_sudo_row_is_left_out_without_sudo_or_helper(
    capsys: pytest.CaptureFixture[str],
) -> None:
    out = facts(sudoversion=SUDO_RS, sudoerror=REQUIRETTY)
    assert doctor(EXAMPLE, ["central"], runner=FakeHost(out), clock=clock) == 0
    assert not [line for line in table(capsys) if line[1] == "sudo"]


def probe_sudo(
    tmp_path: Path, mode: str, version: str, stderr: str, allowed: bool
) -> tuple[Any, list[str]]:
    env = fake_docker(tmp_path)
    bin_dir = tmp_path / "bin"
    (bin_dir / "sudo").write_text(FAKE_SUDO, encoding="utf-8", newline="\n")
    (bin_dir / "sudo").chmod(0o755)
    calls = tmp_path / "sudo-calls"
    calls.write_text("", encoding="utf-8")
    target = tmp_path / "central"
    target.mkdir(exist_ok=True)
    done = subprocess.run(
        ["sh", "-c", doctor_script(target.as_posix(), mode, [])],
        capture_output=True,
        text=True,
        check=False,
        timeout=WAIT,
        env={
            **env,
            "SUDO_CALLS": calls.as_posix(),
            "SUDO_V": (SUDO_FIXTURES / version).as_posix(),
            "SUDO_ERR": (SUDO_FIXTURES / stderr).as_posix(),
            "SUDO_OK": "yes" if allowed else "",
        },
    )
    assert done.returncode == 0, done.stderr
    return parse_facts(done.stdout), calls.read_text("utf-8").splitlines()


@needs_sh
def test_the_probe_reads_the_classic_sudo_version_and_ignores_its_messages(
    tmp_path: Path,
) -> None:
    found, calls = probe_sudo(tmp_path, "helper", "classic-V.txt", "classic-password.txt", False)
    assert found.get("sudoversion") == CLASSIC_SUDO
    assert found.get("sudo") == "no"
    assert "sudoerror" not in found.values
    target = (tmp_path / "central").as_posix()
    assert calls == [f"-n -l {HELPER} {target} pull", "-V"]


@needs_sh
def test_the_probe_reads_sudo_rs_and_the_first_line_it_rejects(tmp_path: Path) -> None:
    found, _ = probe_sudo(tmp_path, "helper", "sudo-rs-V.txt", "sudo-rs-requiretty.txt", True)
    assert found.get("sudoversion") == SUDO_RS
    assert found.get("sudo") == "yes"
    assert found.get("sudoerror") == REQUIRETTY
    found, calls = probe_sudo(tmp_path, "sudo", "sudo-rs-V.txt", "sudo-rs-requiretty.txt", True)
    assert (found.get("sudoversion"), found.get("sudoerror")) == (SUDO_RS, REQUIRETTY)
    assert "sudo" not in found.values
    assert calls == ["-n -l", "-V"]
    found, calls = probe_sudo(tmp_path, "plain", "sudo-rs-V.txt", "sudo-rs-requiretty.txt", True)
    assert "sudoversion" not in found.values
    assert "sudoerror" not in found.values
    assert calls == []
