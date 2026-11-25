from __future__ import annotations

import base64
import hashlib
import os
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from ayeops import diff as diff_module
from ayeops.cli import main
from ayeops.deploy import (
    HELPER,
    INDEX,
    TIMED_OUT,
    TIMEOUT,
    Result,
    deploy,
    diff_script,
    host_files,
    hostside,
    parse_swap,
    swap_script,
    tarball,
)
from ayeops.diff import (
    ENV_NOTES,
    LINES,
    NO_BASELINE,
    Deployed,
    compare,
    diff,
    parse_deployed,
    request,
    unified,
)
from ayeops.registry import load_modules
from ayeops.retire import RULES
from tests.conftest import EXAMPLE, EXAMPLE_STAMP, example_data, manifest_from, render_once
from tests.test_host_init import fake_bin, helper_run, patched_helper, posix, shell_env

TOOLS = ("sh", "tar", "cmp", "grep", "sha256sum", "base64")
needs_shell = pytest.mark.skipif(
    not all(shutil.which(tool) for tool in TOOLS), reason=f"needs {', '.join(TOOLS)}"
)
STAMP: dict[str, Any] = {"time": EXAMPLE_STAMP.time, "commit": EXAMPLE_STAMP.commit}


def rendered(host: str, data: dict[str, Any] | None = None) -> dict[str, str]:
    manifest = manifest_from(data or example_data())
    return host_files(render_once(manifest, load_modules(), EXAMPLE_STAMP), host)


def answer(request_body: bytes, files: dict[str, bytes], index: list[str]) -> str:
    lines: list[str] = []
    wanted: list[str] = []
    for line in request_body.decode().splitlines():
        digest, _, path = line.partition(" ")
        wanted.append(path)
        if path not in files:
            lines.append(f"added {path}")
        elif hashlib.sha256(files[path]).hexdigest() == digest:
            lines.append(f"same {path}")
        elif path in index:
            encoded = base64.encodebytes(files[path]).decode().splitlines()
            lines += [f"old {path}", *encoded, "."]
        else:
            lines.append(f"foreign {path}")
    lines += [f"removed {path}" for path in index if path not in wanted and path in files]
    return "".join(f"{line}\n" for line in lines)


def env_lines(env: dict[str, str] | None, base: dict[str, str] | None, state: str | None) -> str:
    if state is None:
        return ""
    lines = [f"env-file {state}"]
    if state in ("link", "unreadable"):
        return f"{lines[0]}\n"
    if base is None:
        lines.append("env-base absent")
    now = env or {}
    for key in sorted(set(now) | set(base or {})):
        if key in now:
            lines.append(f"env-key {key}")
        if base is not None and now.get(key) != base.get(key):
            lines.append(f"env-changed {key}")
    return "".join(f"{line}\n" for line in lines)


def example_env(files: dict[str, bytes]) -> dict[str, str]:
    text = files.get(".env.example", b"").decode()
    return {line.partition("=")[0]: f"value-of-{line.partition('=')[0]}" for line in text.split()}


KEEP: Any = object()


class FakeHost:
    def __init__(
        self,
        files: dict[str, str | bytes],
        index: list[str] | None = None,
        result: Result | None = None,
        env: dict[str, str] | None = None,
        base: Any = KEEP,
        env_file: str | None = "present",
    ) -> None:
        self.files = {
            path: text.encode() if isinstance(text, str) else text for path, text in files.items()
        }
        self.index = sorted(files) if index is None else index
        self.result = result
        self.env = example_env(self.files) if env is None else env
        self.base = self.env if base is KEEP else base
        self.env_file = env_file
        self.calls: list[tuple[list[str], bytes | None, float]] = []

    def __call__(self, argv: list[str], stdin: bytes | None, timeout: float) -> Result:
        self.calls.append((argv, stdin, timeout))
        if self.result is not None:
            return self.result
        assert stdin is not None
        envs = env_lines(self.env, self.base, self.env_file)
        return Result(0, answer(stdin, self.files, self.index) + envs)

    def requested(self) -> list[str]:
        body = self.calls[0][1]
        assert body is not None
        return [line.partition(" ")[2] for line in body.decode().splitlines()]


def fleet_host(hosts: dict[str, FakeHost]) -> Any:
    def runner(argv: list[str], stdin: bytes | None, timeout: float) -> Result:
        return hosts[argv[1]](argv, stdin, timeout)

    return runner


def test_an_up_to_date_host_is_no_change_in_one_ssh_call(
    capsys: pytest.CaptureFixture[str],
) -> None:
    host = FakeHost(dict(rendered("central")))
    assert diff(EXAMPLE, ["central"], runner=host, timeout=42, **STAMP) == 0
    assert len(host.calls) == 1
    argv, body, timeout = host.calls[0]
    assert argv[:2] == ["ssh", "central"]
    assert argv[2].startswith("sh -c 'set -- /opt/ayeops/central\nset -eu\nD=$1\n")
    assert timeout == 42
    assert body == request(rendered("central"))
    assert capsys.readouterr().out == "central:/opt/ayeops/central: no change\n"
    answer_text = answer(body, host.files, host.index)
    assert {line.split()[0] for line in answer_text.splitlines()} == {"same"}


def test_the_request_names_each_file_in_order_with_the_hash_of_its_render() -> None:
    body = request({"b.yml": "b\n", RULES: "r\n", "a.yml": "a\n"}).decode()
    assert body.splitlines() == [
        f"{hashlib.sha256(b'a\n').hexdigest()} a.yml",
        f"{hashlib.sha256(b'b\n').hexdigest()} b.yml",
        f"{hashlib.sha256(b'r\n').hexdigest()} {RULES}",
    ]
    assert request({}) == b""


def test_a_changed_file_prints_its_unified_diff_and_the_service_that_reads_it(
    capsys: pytest.CaptureFixture[str],
) -> None:
    files: dict[str, str | bytes] = dict(rendered("central"))
    routes = "ingress/dynamic/routes.yml"
    files[routes] = str(files[routes]).replace("http:", "http:\n  old: true", 1)
    host = FakeHost(files)
    assert diff(EXAMPLE, ["central"], runner=host, **STAMP) == 1
    lines = capsys.readouterr().out.splitlines()
    assert lines[:2] == [
        "central:/opt/ayeops/central: 1 changed, 0 added, 0 removed",
        f"changed  {routes}",
    ]
    assert lines[2:4] == [f"--- central:/opt/ayeops/central/{routes}", f"+++ render/{routes}"]
    assert "-  old: true" in lines
    assert lines[-2:] == ["SERVICE  CHANGE", "traefik  config 1 file(s)"]


def test_a_changed_alert_rule_recreates_grafana_and_the_catalog_and_nothing_else(
    capsys: pytest.CaptureFixture[str],
) -> None:
    before = example_data()
    before["services"]["web"]["alerts"][0]["for"] = "7m"
    host = FakeHost(dict(rendered("central", before)))
    assert diff(EXAMPLE, ["central"], runner=host, **STAMP) == 1
    out = capsys.readouterr().out
    changed = [line.split()[1] for line in out.splitlines() if line.startswith("changed  ")]
    assert "compose.yaml" in changed
    assert RULES in changed
    table = out.splitlines()[out.splitlines().index("SERVICE      CHANGE") :]
    assert table == [
        "SERVICE      CHANGE",
        "grafana      recreated, config 1 file(s)",
        "mcp-catalog  recreated, config 1 file(s)",
    ]
    assert "-    for: 7m\n+    for: 3m\n" in out
    assert "-      io.ayeops.config-hash: " in out


def test_added_and_removed_files_follow_the_previous_index_only(
    capsys: pytest.CaptureFixture[str],
) -> None:
    files: dict[str, str | bytes] = dict(rendered("edge"))
    del files["node/reboot.sh"]
    files["gone/old.yml"] = "x\n"
    files["data/state.db"] = b"\x00\x01"
    index = sorted(set(files) - {"data/state.db"})
    host = FakeHost(files, index)
    assert diff(EXAMPLE, ["edge"], runner=host, **STAMP) == 1
    lines = capsys.readouterr().out.splitlines()
    assert lines[:3] == [
        "edge:/opt/ayeops/edge: 0 changed, 1 added, 1 removed",
        "added    node/reboot.sh",
        "removed  gone/old.yml",
    ]
    assert not any("data/state.db" in line for line in lines)
    assert not any("recreated" in line for line in lines)
    assert any(line.endswith("  config 1 file(s)") for line in lines)


def test_a_new_and_a_dropped_service_are_named(capsys: pytest.CaptureFixture[str]) -> None:
    files: dict[str, str | bytes] = dict(rendered("edge"))
    compose = yaml.safe_load(str(files["compose.yaml"]))
    first = sorted(compose["services"])[0]
    compose["services"]["retired-svc"] = compose["services"].pop(first)
    files["compose.yaml"] = yaml.safe_dump(compose)
    assert diff(EXAMPLE, ["edge"], runner=FakeHost(files), **STAMP) == 1
    lines = capsys.readouterr().out.splitlines()
    start = next(index for index, line in enumerate(lines) if line.startswith("SERVICE  "))
    rows = {line.split(None, 1)[0]: line.split(None, 1)[1] for line in lines[start + 1 :]}
    assert rows[first] == "created"
    assert rows["retired-svc"] == "removed"
    assert not [row for row in rows.values() if "recreated" in row]


def test_a_rule_that_left_the_manifest_is_shown_as_the_deploy_would_ship_it(
    capsys: pytest.CaptureFixture[str],
) -> None:
    files: dict[str, str | bytes] = dict(rendered("central"))
    live = yaml.safe_load(str(files[RULES]))
    live["groups"][0]["rules"].append({"uid": "aye-retired", "title": "gone"})
    files[RULES] = yaml.safe_dump(live)
    assert diff(EXAMPLE, ["central"], runner=FakeHost(files), **STAMP) == 1
    out = capsys.readouterr().out
    assert "central: alert rule aye-retired left the manifest, the deploy deletes it" in out
    assert "+deleteRules:" in out
    assert "grafana  recreated, config 1 file(s)" not in out
    assert "grafana  config 1 file(s)" in out


def test_unreadable_live_rules_are_an_error_like_in_deploy(
    capsys: pytest.CaptureFixture[str],
) -> None:
    files: dict[str, str | bytes] = dict(rendered("central"))
    files[RULES] = "- not\n- a mapping\n"
    assert diff(EXAMPLE, ["central"], runner=FakeHost(files), **STAMP) == 2
    assert capsys.readouterr().err.strip() == (
        "central: live alert rules unreadable (alert rules file is not a mapping), "
        "aye deploy would stop before the swap"
    )


def test_env_files_are_never_requested() -> None:
    files: dict[str, str | bytes] = {**rendered("edge"), ".env": "SECRET=1\n"}
    host = FakeHost(files, sorted(rendered("edge")))
    assert diff(EXAMPLE, ["edge"], runner=host, **STAMP) == 0
    requested = host.requested()
    assert ".env.example" in requested
    assert ".env" not in requested
    assert requested == sorted(rendered("edge"))
    body = host.calls[0][1]
    assert body is not None
    assert b"SECRET" not in body


def test_sudo_wraps_the_step_and_ssh_names_the_destination() -> None:
    host = FakeHost(dict(rendered("edge")))
    assert diff(EXAMPLE, ["edge"], "ops-edge", True, "/srv/x", runner=host, **STAMP) == 0
    argv = host.calls[0][0]
    assert argv[:2] == ["ssh", "ops-edge"]
    assert argv[2].startswith("sudo sh -c 'set -- /srv/x\n")


def test_the_helper_runs_its_diff_step(capsys: pytest.CaptureFixture[str]) -> None:
    host = FakeHost(dict(rendered("edge")))
    assert diff(EXAMPLE, ["edge"], "aye@edge", runner=host, helper=True, **STAMP) == 0
    assert host.calls[0][0] == ["ssh", "aye@edge", f"sudo -n {HELPER} /opt/ayeops/edge diff"]
    assert capsys.readouterr().out == "aye@edge:/opt/ayeops/edge: no change\n"


def test_the_connect_of_each_host_names_its_destination_and_mode(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    data = example_data()
    data["hosts"]["edge"]["connect"] = {"ssh": "aye@edge", "helper": True}
    data["hosts"]["central"]["connect"] = {"ssh": "ops-central", "sudo": True}
    monkeypatch.setattr(diff_module, "load_manifest", lambda path: manifest_from(data))
    host = FakeHost(dict(rendered("edge")))
    assert diff(EXAMPLE, ["edge"], runner=host, **STAMP) == 0
    assert host.calls[0][0] == ["ssh", "aye@edge", f"sudo -n {HELPER} /opt/ayeops/edge diff"]
    assert capsys.readouterr().out == "aye@edge:/opt/ayeops/edge: no change\n"
    central = FakeHost(dict(rendered("central")))
    diff(EXAMPLE, ["central"], runner=central, **STAMP)
    assert central.calls[0][0][:2] == ["ssh", "ops-central"]
    assert central.calls[0][0][2].startswith("sudo sh -c ")
    flagged = FakeHost(dict(rendered("edge")))
    assert diff(EXAMPLE, ["edge"], "other", True, runner=flagged, **STAMP) == 0
    assert flagged.calls[0][0][:2] == ["ssh", "other"]
    assert flagged.calls[0][0][2].startswith("sudo sh -c 'set -- /opt/ayeops/edge\n")


def test_a_connect_helper_outside_the_helper_root_stops_before_ssh(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    data = example_data()
    data["hosts"]["edge"]["connect"] = {"helper": True, "dir": "/srv/edge"}
    monkeypatch.setattr(diff_module, "load_manifest", lambda path: manifest_from(data))
    host = FakeHost(dict(rendered("edge")))
    assert diff(EXAMPLE, ["edge"], runner=host, **STAMP) == 2
    assert host.calls == []
    assert capsys.readouterr().err.strip() == (
        "--helper deploys only to /opt/ayeops/<name>, not /srv/edge"
    )


def test_a_helper_without_the_diff_step_asks_for_host_init_again(
    capsys: pytest.CaptureFixture[str],
) -> None:
    host = FakeHost({}, result=Result(1, "", "sudo: a password is required"))
    assert diff(EXAMPLE, ["edge"], runner=host, helper=True, **STAMP) == 2
    assert capsys.readouterr().err.splitlines() == [
        "sudo: a password is required",
        "edge: the deployed files could not be read",
        f"edge: --helper needs the diff step of {HELPER}; "
        "run the aye host-init script of this host again if it predates aye diff",
    ]


@pytest.mark.parametrize(
    "result",
    [
        Result(255, "", "ssh: connect to host edge port 22: Connection refused"),
        Result(TIMED_OUT, "", "ssh timed out after 5s"),
    ],
)
def test_an_unreachable_host_is_an_error(
    result: Result, capsys: pytest.CaptureFixture[str]
) -> None:
    host = FakeHost({}, result=result)
    assert diff(EXAMPLE, ["edge"], runner=host, helper=True, **STAMP) == 2
    assert capsys.readouterr().err.splitlines() == [
        result.err,
        "edge: the deployed files could not be read",
    ]


def test_a_missing_ssh_binary_is_an_error(capsys: pytest.CaptureFixture[str]) -> None:
    def runner(argv: list[str], stdin: bytes | None, timeout: float) -> Result:
        raise FileNotFoundError(2, "No such file or directory", "ssh")

    assert diff(EXAMPLE, ["edge"], runner=runner, **STAMP) == 2
    assert capsys.readouterr().err.startswith("edge: [Errno 2] No such file or directory")


@pytest.mark.parametrize(
    "out",
    ["noise\n", "same\n", "old compose.yaml\nYWJj\n", "old compose.yaml\n!!\n.\n", "added a\n"],
)
def test_a_garbled_answer_is_an_error(out: str, capsys: pytest.CaptureFixture[str]) -> None:
    host = FakeHost({}, result=Result(0, out))
    assert diff(EXAMPLE, ["edge"], runner=host, **STAMP) == 2
    assert capsys.readouterr().err.startswith("edge: ")


def test_all_hosts_print_details_then_a_summary_and_the_worst_exit_code(
    capsys: pytest.CaptureFixture[str],
) -> None:
    edge: dict[str, str | bytes] = dict(rendered("edge"))
    edge["gone.yml"] = "g\n"
    hosts = {"central": FakeHost(dict(rendered("central"))), "edge": FakeHost(edge)}
    assert diff(EXAMPLE, None, runner=fleet_host(hosts), **STAMP) == 1
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "== central"
    assert lines[2] == "== edge"
    assert lines[-3:] == [
        "HOST     DIFF",
        "central  no change",
        "edge     0 changed, 0 added, 1 removed, 0 service(s)",
    ]
    hosts["central"] = FakeHost({}, result=Result(255, "", "down"))
    assert diff(EXAMPLE, None, runner=fleet_host(hosts), **STAMP) == 2
    assert capsys.readouterr().out.splitlines()[-2] == "central  error"
    hosts["central"] = FakeHost(dict(rendered("central")))
    hosts["edge"] = FakeHost(dict(rendered("edge")))
    assert diff(EXAMPLE, None, runner=fleet_host(hosts), **STAMP) == 0


def test_wrong_arguments_are_errors_before_any_ssh(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    host = FakeHost({})
    assert diff(EXAMPLE, ["nowhere"], runner=host) == 2
    assert diff(EXAMPLE, ["edge", "central"], "alias", runner=host) == 2
    assert diff(EXAMPLE, ["edge"], directory="/srv/edge", helper=True, runner=host) == 2
    assert diff(EXAMPLE, ["edge"], sudo=True, helper=True, runner=host) == 2
    data = yaml.safe_load(EXAMPLE.read_text("utf-8"))
    del data["services"]["web"]["description"]
    broken = tmp_path / "broken.yaml"
    broken.write_text(yaml.safe_dump(data), encoding="utf-8")
    assert diff(broken, ["edge"], runner=host) == 2
    assert host.calls == []
    err = capsys.readouterr().err.splitlines()
    assert f"{EXAMPLE}: unknown host 'nowhere'" in err
    assert "--ssh names the destination of one host, not of a fleet" in err
    assert "--helper deploys only to /opt/ayeops/<name>, not /srv/edge" in err
    assert "--helper and --sudo exclude each other" in err
    assert err[-1] == f"{broken}: no diff, fix the errors first"


def test_a_long_diff_is_cut_with_a_count_of_the_rest() -> None:
    old = "".join(f"a{n}\n" for n in range(300))
    new = "".join(f"b{n}\n" for n in range(300))
    lines = unified(old, new, "x", "y")
    assert len(lines) == LINES + 1
    assert lines[:3] == ["--- x", "+++ y", "@@ -1,300 +1,300 @@"]
    assert lines[-1] == f"... {603 - LINES} more line(s) of this diff omitted"
    assert unified("a\n", "a", "x", "y") == [
        "--- x",
        "+++ y",
        "@@ -1 +1 @@",
        "-a",
        "+a",
        "\\ No newline at end of file",
    ]


def test_links_foreign_and_binary_files_count_as_changed_without_a_diff() -> None:
    deployed = parse_deployed(
        "link a.yml\nforeign b.yml\nold c.bin\n/w==\n.\nold d.yml\nZA==\n.\nremoved z\n"
    )
    plan = compare(
        {"a.yml": "a", "b.yml": "b", "c.bin": "c", "d.yml": "d"}, deployed, "h:/opt/ayeops/h"
    )
    assert plan.changed == ["a.yml", "b.yml", "c.bin"]
    assert plan.notes == {
        "a.yml": "a symbolic link on the host, not read",
        "b.yml": "not shipped by the previous deploy, not read",
        "c.bin": "not UTF-8 text on the host, no diff",
    }
    assert plan.diffs == {}
    assert plan.removed == ["z"]
    assert plan.line() == "3 changed, 0 added, 1 removed, 0 service(s)"
    assert compare({}, Deployed(), "h").line() == "no change"


def test_the_cli_passes_every_option_through(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[Any, ...]] = []

    def fake(*args: Any, **kwargs: Any) -> int:
        seen.append((*args, kwargs))
        return 1

    monkeypatch.setattr(diff_module, "diff", fake)
    argv = ["diff", "m.yaml", "--host", "h", "--ssh", "a", "--sudo", "--dir", "/x"]
    assert main([*argv, "--build-time", "t", "--commit", "c", "--timeout", "60"]) == 1
    assert main(["diff", "m.yaml", "--all", "--helper"]) == 1
    options: dict[str, Any] = {"time": "t", "commit": "c", "timeout": 60.0, "helper": False}
    defaults: dict[str, Any] = {"time": None, "commit": None, "timeout": TIMEOUT, "helper": True}
    assert seen == [
        (Path("m.yaml"), ["h"], "a", True, "/x", options),
        (Path("m.yaml"), None, None, False, None, defaults),
    ]
    for bad in (["--host", "h", "--sudo", "--helper"], ["--host", "h", "--all"]):
        with pytest.raises(SystemExit):
            main(["diff", "m.yaml", *bad])
    assert main(["diff", str(EXAMPLE)]) == 2


def sh(script: str, stdin: bytes) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(["sh", "-c", script], input=stdin, capture_output=True, check=False)


@needs_shell
def test_the_host_side_predicts_exactly_what_the_swap_then_does(tmp_path: Path) -> None:
    target = tmp_path / "central"
    first = {"a.yml": "a: 1\n", "same.yml": "s\n", "gone.yml": "g\n", "sub/c.yml": "c\n"}
    assert sh(swap_script(target.as_posix()), tarball(first)).returncode == 0
    (target / ".env").write_text("SECRET=1\n", encoding="utf-8")
    (target / "data.db").write_text("unindexed\n", encoding="utf-8")
    outside = tmp_path / "outside.yml"
    outside.write_text("o\n", encoding="utf-8")
    with (target / INDEX).open("a", encoding="utf-8", newline="\n") as index:
        index.write("../outside.yml\n")
    second = {"a.yml": "a: 2\n", "same.yml": "s\n", "new.yml": "n\n"}
    peek = sh(diff_script(target.as_posix()), request(second))
    assert peek.returncode == 0, peek.stderr
    plan = compare(second, parse_deployed(peek.stdout.decode()), "h:/d")
    assert (plan.changed, plan.added) == (["a.yml"], ["new.yml"])
    assert plan.removed == ["gone.yml", "sub/c.yml"]
    assert plan.diffs["a.yml"][-2:] == ["-a: 1", "+a: 2"]
    swapped = sh(swap_script(target.as_posix()), tarball(second))
    assert swapped.returncode == 0, swapped.stderr
    changed, removed = parse_swap(swapped.stdout.decode())
    assert sorted(changed) == sorted(plan.changed + plan.added)
    assert sorted(removed) == plan.removed
    assert outside.exists()
    assert (target / ".env").exists()
    again = sh(diff_script(target.as_posix()), request(second))
    assert again.stdout.decode().splitlines() == [
        "same a.yml",
        "same new.yml",
        "same same.yml",
        "env-file present",
        "env-key SECRET",
    ]


@needs_shell
def test_the_host_side_never_reads_an_env_file(tmp_path: Path) -> None:
    target = tmp_path / "central"
    assert sh(swap_script(target.as_posix()), tarball({".env.example": "K=\n"})).returncode == 0
    (target / ".env").write_text("SECRET=1\n", encoding="utf-8")
    (target / ".env.prod").write_text("SECRET=2\n", encoding="utf-8")
    (target / INDEX).write_text(".env.example\n.env\n", encoding="utf-8", newline="\n")
    peek = sh(diff_script(target.as_posix()), request({".env.example": "K=1\n"}))
    assert peek.returncode == 0, peek.stderr
    out = peek.stdout.decode()
    assert out.splitlines()[0] == "old .env.example"
    assert out.splitlines()[-4:] == [
        "removed .env",
        "env-file present",
        "env-key SECRET",
        "env-changed SECRET",
    ]
    assert base64.b64encode(b"SECRET=1").decode()[:8] not in out
    assert "=1" not in out
    for path in (".env", ".env.prod", "sub/.env"):
        refused = sh(diff_script(target.as_posix()), f"- {path}\n".encode())
        assert refused.returncode == 1
        assert refused.stdout == b""
        assert b"refused: unsafe path" in refused.stderr


@needs_shell
@pytest.mark.parametrize(
    "path", ["../escape.yml", "/etc/passwd", "a/./b", "a//b", ".aye-files", ".aye-env"]
)
def test_the_host_side_refuses_a_path_outside_the_directory(path: str, tmp_path: Path) -> None:
    target = tmp_path / "central"
    assert sh(swap_script(target.as_posix()), tarball({"keep.yml": "k\n"})).returncode == 0
    done = sh(diff_script(target.as_posix()), f"- keep.yml\n- {path}\n".encode())
    assert done.returncode == 1
    assert b"refused: unsafe path" in done.stderr


@needs_shell
def test_the_host_side_does_not_send_a_file_the_previous_deploy_did_not_ship(
    tmp_path: Path,
) -> None:
    target = tmp_path / "central"
    target.mkdir()
    (target / "compose.yaml").write_text("hand made\n", encoding="utf-8")
    done = sh(diff_script(target.as_posix()), request({"compose.yaml": "c\n", "a.yml": "a\n"}))
    assert done.returncode == 0, done.stderr
    assert done.stdout.decode().splitlines() == [
        "added a.yml",
        "foreign compose.yaml",
        "env-file absent",
        "env-base absent",
    ]
    empty = sh(diff_script((tmp_path / "missing").as_posix()), request({"a.yml": "a\n"}))
    assert empty.stdout == b"added a.yml\nenv-file absent\nenv-base absent\n"


@needs_shell
def test_diff_through_the_helper_after_a_deploy_through_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _, state, bin_dir = fake_bin(tmp_path)
    helper, base = patched_helper(tmp_path, bin_dir)
    env = shell_env(tmp_path, state, bin_dir)
    (state / "ps.json").write_text("[]", encoding="utf-8")
    (base / "edge").mkdir()
    keys = sorted(line.partition("=")[0] for line in rendered("edge")[".env.example"].split())
    values = {key: f"s3cret-{index}-value" for index, key in enumerate(keys)}
    env_text = "".join(f"{key}={value}\n" for key, value in values.items())
    (base / "edge" / ".env").write_text(env_text, encoding="utf-8", newline="\n")
    steps: list[str] = []

    def runner(argv: list[str], stdin: bytes | None, timeout: float) -> Result:
        words = argv[-1].split()
        assert words[:3] == ["sudo", "-n", HELPER]
        steps.append(words[4])
        directory = words[3].replace("/opt/ayeops", posix(base))
        done = helper_run(helper, env, directory, words[4], stdin=stdin)
        return Result(done.returncode, done.stdout.decode(), done.stderr.decode())

    assert diff(EXAMPLE, ["edge"], runner=runner, helper=True, **STAMP) == 1
    out = capsys.readouterr().out
    assert "0 changed, " in out
    assert f"edge: {NO_BASELINE}" in out
    deploy(EXAMPLE, "edge", runner=runner, helper=True, **STAMP)
    capsys.readouterr()
    assert diff(EXAMPLE, ["edge"], runner=runner, helper=True, **STAMP) == 0
    assert capsys.readouterr().out == "edge:/opt/ayeops/edge: no change\n"
    (base / "edge" / "vmagent" / "scrape.yaml").write_text("old\n", encoding="utf-8")
    assert diff(EXAMPLE, ["edge"], runner=runner, helper=True, **STAMP) == 1
    out = capsys.readouterr().out
    assert "changed  vmagent/scrape.yaml" in out
    assert "-old" in out
    assert steps[0] == "diff"
    assert steps[-1] == "diff"
    env_text = env_text.replace(values["AYE_UPDATES_TOKEN"], "s3cret-rotated-value")
    env_text = env_text.replace(f"AYE_TELEGRAM_CHAT_ID={values['AYE_TELEGRAM_CHAT_ID']}\n", "")
    (base / "edge" / ".env").write_text(f"{env_text}AYE_LEFTOVER=s3cret-old\n", newline="\n")
    (base / "edge" / "vmagent" / "scrape.yaml").write_text(
        rendered("edge")["vmagent/scrape.yaml"], encoding="utf-8", newline="\n"
    )
    assert diff(EXAMPLE, ["edge"], runner=runner, helper=True, **STAMP) == 1
    captured = capsys.readouterr()
    assert captured.out.splitlines() == [
        "edge:/opt/ayeops/edge: 0 changed, 0 added, 0 removed; .env 1 changed, 1 missing, 1 unused",
        "SERVICE   CHANGE",
        "sentinel  recreated, env 1 key(s)",
        "updates   recreated, env 1 key(s)",
        "ENV KEY               CHANGE             SERVICES",
        "AYE_LEFTOVER          unused",
        "AYE_TELEGRAM_CHAT_ID  missing, up fails  sentinel",
        "AYE_UPDATES_TOKEN     changed            updates",
    ]
    secrets = [*values.values(), "s3cret-rotated-value", "s3cret-old"]
    hashes = [hashlib.sha256(value.encode()).hexdigest() for value in secrets]
    stored = (base / "edge" / ".aye-env").read_text(encoding="utf-8")
    assert sorted(line.split()[0] for line in stored.splitlines()) == keys
    assert hashlib.sha256(values["AYE_UPDATES_TOKEN"].encode()).hexdigest() in stored
    assert not any(value in stored for value in secrets)
    for text in (captured.out, captured.err):
        assert not any(secret in text for secret in secrets + hashes)


def test_the_helper_dispatches_the_diff_step_to_the_installed_script() -> None:
    helper = hostside("aye-host.sh")
    assert '\n    diff) exec sh "$LIB/diff.sh" "$1" ;;\n' in helper
    assert "rules|pull|swap|up|ps|diff" in helper
    assert hostside("diff.sh").startswith("set -eu\nD=$1\n")


def test_backup_units_to_install_change_and_remove_are_named(
    capsys: pytest.CaptureFixture[str],
) -> None:
    files: dict[str, str | bytes] = dict(rendered("edge"))
    unit = "backups/systemd/ayeops-edge-backup-acme-db"
    del files[f"{unit}.service"]
    files[f"{unit}.timer"] = "[Timer]\nOnCalendar=*-*-* 03:00:00 Europe/Berlin\n"
    files["backups/systemd/ayeops-edge-backup-old.timer"] = "[Timer]\n"
    assert diff(EXAMPLE, ["edge"], runner=FakeHost(files), **STAMP) == 1
    lines = capsys.readouterr().out.splitlines()
    table = lines[lines.index(next(line for line in lines if line.startswith("BACKUP UNIT"))) :]
    assert [line.split() for line in table] == [
        ["BACKUP", "UNIT", "CHANGE"],
        ["ayeops-edge-backup-acme-db.service", "installed"],
        ["ayeops-edge-backup-acme-db.timer", "changed"],
        ["ayeops-edge-backup-old.timer", "removed"],
    ]
    assert "+OnCalendar=*-*-* 04:30:00 Europe/Berlin" in lines


def test_a_host_without_backup_units_prints_no_unit_table(
    capsys: pytest.CaptureFixture[str],
) -> None:
    files: dict[str, str | bytes] = dict(rendered("central"))
    files["compose.yaml"] = "services: {}\n"
    assert diff(EXAMPLE, ["central"], runner=FakeHost(files), **STAMP) == 1
    assert "BACKUP UNIT" not in capsys.readouterr().out


def edge_env() -> dict[str, str]:
    return example_env(FakeHost(dict(rendered("edge"))).files)


def leaked(text: str, values: list[str]) -> list[str]:
    hashes = [hashlib.sha256(value.encode()).hexdigest() for value in values]
    return [secret for secret in values + hashes if secret in text]


def test_env_drift_names_missing_unused_and_changed_keys_and_what_compose_recreates(
    capsys: pytest.CaptureFixture[str],
) -> None:
    base = {**edge_env(), "COMPOSE_PROJECT_NAME": "edge-project"}
    env = {**base, "AYE_UPDATES_TOKEN": "rotated-secret", "AYE_LEFTOVER": "old-secret"}
    del env["AYE_TELEGRAM_CHAT_ID"]
    del env["AYE_SENTINEL_PING_URL"]
    host = FakeHost(dict(rendered("edge")), env=env, base=base)
    assert diff(EXAMPLE, ["edge"], runner=host, **STAMP) == 1
    captured = capsys.readouterr()
    assert captured.out.splitlines() == [
        "edge:/opt/ayeops/edge: 0 changed, 0 added, 0 removed; .env 1 changed, 2 missing, 1 unused",
        "SERVICE   CHANGE",
        "sentinel  recreated, env 2 key(s)",
        "updates   recreated, env 1 key(s)",
        "ENV KEY                CHANGE             SERVICES",
        "AYE_LEFTOVER           unused",
        "AYE_SENTINEL_PING_URL  missing            sentinel",
        "AYE_TELEGRAM_CHAT_ID   missing, up fails  sentinel",
        "AYE_UPDATES_TOKEN      changed            updates",
    ]
    values = [*base.values(), *env.values()]
    assert leaked(captured.out + captured.err, values) == []
    body = host.calls[0][1]
    assert body is not None
    assert leaked(body.decode(), values) == []


def test_a_compose_key_is_never_unused_and_a_changed_one_recreates_nothing(
    capsys: pytest.CaptureFixture[str],
) -> None:
    base = {**edge_env(), "COMPOSE_PROJECT_NAME": "a"}
    host = FakeHost(dict(rendered("edge")), env={**base, "COMPOSE_PROJECT_NAME": "b"}, base=base)
    assert diff(EXAMPLE, ["edge"], runner=host, **STAMP) == 1
    assert capsys.readouterr().out.splitlines() == [
        "edge:/opt/ayeops/edge: 0 changed, 0 added, 0 removed; .env 1 changed, 0 missing, 0 unused",
        "ENV KEY               CHANGE   SERVICES",
        "COMPOSE_PROJECT_NAME  changed",
    ]


def test_both_forms_of_a_required_reference_make_a_missing_key_fail_up() -> None:
    compose = yaml.safe_dump(
        {
            "services": {
                "a": {"environment": {"X": "${AYE_A?set it}", "Y": "${AYE_B:?set it}"}},
                "b": {"environment": ["Z=${AYE_C:-}", "W=${AYE_D}"]},
            }
        }
    )
    files = {"compose.yaml": compose, ".env.example": "AYE_A=\nAYE_B=\nAYE_C=\nAYE_D=\n"}
    deployed = Deployed(same=set(files), env_file="absent")
    assert compare(files, deployed, "h").env.rows == [
        ("AYE_A", "missing, up fails", "a"),
        ("AYE_B", "missing, up fails", "a"),
        ("AYE_C", "missing", "b"),
        ("AYE_D", "missing", "b"),
    ]


def test_unused_keys_alone_are_no_change(capsys: pytest.CaptureFixture[str]) -> None:
    env = {**edge_env(), "AYE_OLD_TOKEN": "x", "lower_case": "y"}
    host = FakeHost(dict(rendered("edge")), env=env)
    assert diff(EXAMPLE, ["edge"], runner=host, **STAMP) == 0
    assert capsys.readouterr().out.splitlines() == [
        "edge:/opt/ayeops/edge: no change",
        "ENV KEY        CHANGE  SERVICES",
        "AYE_OLD_TOKEN  unused",
        "lower_case     unused",
    ]


def test_a_key_referenced_by_any_rendered_file_is_not_unused(
    capsys: pytest.CaptureFixture[str],
) -> None:
    env = {**edge_env(), "AYE_TEXTFILE_DIR": "/srv/textfile", "TMPDIR": "/tmp"}
    host = FakeHost(dict(rendered("edge")), env=env, base={**env, "AYE_TEXTFILE_DIR": "/srv"})
    assert diff(EXAMPLE, ["edge"], runner=host, **STAMP) == 1
    assert capsys.readouterr().out.splitlines()[-2:] == [
        "ENV KEY           CHANGE   SERVICES",
        "AYE_TEXTFILE_DIR  changed",
    ]


def test_a_host_without_hashes_of_a_deploy_still_names_missing_keys(
    capsys: pytest.CaptureFixture[str],
) -> None:
    env = edge_env()
    del env["AYE_UPDATES_TOKEN"]
    host = FakeHost(dict(rendered("edge")), env=env, base=None)
    assert diff(EXAMPLE, ["edge"], runner=host, **STAMP) == 1
    assert capsys.readouterr().out.splitlines() == [
        "edge:/opt/ayeops/edge: 0 changed, 0 added, 0 removed; .env 0 changed, 1 missing, 0 unused",
        "ENV KEY            CHANGE             SERVICES",
        "AYE_UPDATES_TOKEN  missing, up fails  updates",
        f"edge: {NO_BASELINE}",
    ]


def test_a_host_without_env_misses_every_required_key(capsys: pytest.CaptureFixture[str]) -> None:
    host = FakeHost(dict(rendered("edge")), env={}, base={}, env_file="absent")
    assert diff(EXAMPLE, ["edge"], runner=host, **STAMP) == 1
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].endswith("; .env 0 changed, 7 missing, 0 unused")
    assert [line.split()[0] for line in lines[2:]] == sorted(edge_env())


@pytest.mark.parametrize("state", [None, "link", "unreadable"])
def test_an_env_the_host_does_not_compare_is_a_note_and_no_change(
    state: str | None, capsys: pytest.CaptureFixture[str]
) -> None:
    host = FakeHost(dict(rendered("edge")), env={}, base=None, env_file=state)
    assert diff(EXAMPLE, ["edge"], runner=host, **STAMP) == 0
    assert capsys.readouterr().out.splitlines() == [
        "edge:/opt/ayeops/edge: no change",
        f"edge: {ENV_NOTES[state]}",
    ]


@pytest.mark.parametrize(
    "line",
    [
        "env-file maybe",
        "env-base present",
        "env-key bad-key",
        "env-changed 1X",
        "env-key A B",
        "env-other X",
    ],
)
def test_a_garbled_env_answer_is_an_error(line: str, capsys: pytest.CaptureFixture[str]) -> None:
    host = FakeHost({}, result=Result(0, f"env-file present\n{line}\n"))
    assert diff(EXAMPLE, ["edge"], runner=host, **STAMP) == 2
    assert capsys.readouterr().err == f"edge: unexpected answer {line!r}\n"


def test_the_fleet_summary_counts_env_drift(capsys: pytest.CaptureFixture[str]) -> None:
    base = edge_env()
    edge = FakeHost(dict(rendered("edge")), env={**base, "AYE_UPDATES_TOKEN": "new"}, base=base)
    hosts = {"central": FakeHost(dict(rendered("central"))), "edge": edge}
    assert diff(EXAMPLE, None, runner=fleet_host(hosts), **STAMP) == 1
    assert capsys.readouterr().out.splitlines()[-1] == (
        "edge     0 changed, 0 added, 0 removed; .env 1 changed, 0 missing, 0 unused, 1 service(s)"
    )


def test_swap_and_diff_parse_env_with_the_same_function() -> None:
    swap, peek = hostside("swap.sh"), hostside("diff.sh")
    start, end = "env_sum() {\n", "  env_sum\n}\n"
    block = swap[swap.index(start) : swap.index(end) + len(end)]
    assert block in peek
    assert block.count("echo") == 1
    assert '(umask 077 && env_sums "$D/.env" > .aye-env)\nmv -f .aye-env "$D/.aye-env"\n' in swap


ENV_FILE = (
    "# comment\n"
    "\n"
    "PLAIN=plain-secret-1\n"
    "  export SPACED = 'spaced secret 2'\n"
    'MULTI="first-secret-3\n'
    'second-secret-4"\n'
    "DUP=dup-secret-5\n"
    "DUP=dup-secret-6\n"
    "9BAD=not-a-key\n"
    "EMPTY=\n"
    "LAST=last-secret-7"
)
ENV_VALUES = [
    "plain-secret-1",
    "spaced secret 2",
    "first-secret-3",
    "second-secret-4",
    "dup-secret-5",
    "dup-secret-6",
    "last-secret-7",
]


def write_env(target: Path, text: str) -> None:
    (target / ".env").write_bytes(text.encode())


def env_answer_lines(target: Path) -> list[str]:
    done = sh(diff_script(target.as_posix()), b"")
    assert done.returncode == 0, done.stderr
    text = (done.stdout + done.stderr).decode()
    assert leaked(text, ENV_VALUES) == []
    return [line for line in text.splitlines() if line.startswith("env-")]


@needs_shell
def test_the_swap_stores_one_hash_per_key_and_the_diff_names_only_changed_keys(
    tmp_path: Path,
) -> None:
    target = tmp_path / "edge"
    target.mkdir()
    write_env(target, ENV_FILE)
    assert sh(swap_script(target.as_posix()), tarball({"a.yml": "a\n"})).returncode == 0
    stored = (target / ".aye-env").read_text(encoding="utf-8")
    assert [line.split()[0] for line in stored.splitlines()] == [
        "PLAIN",
        "SPACED",
        "MULTI",
        "DUP",
        "DUP",
        "EMPTY",
        "LAST",
    ]
    assert all(len(line.split()[1]) == 64 for line in stored.splitlines())
    assert [secret for secret in ENV_VALUES if secret in stored] == []
    assert hashlib.sha256(b"plain-secret-1").hexdigest() in stored
    if os.name == "posix":
        assert stat.S_IMODE((target / ".aye-env").stat().st_mode) == 0o600
    assert env_answer_lines(target) == [
        "env-file present",
        *(f"env-key {key}" for key in ("PLAIN", "SPACED", "MULTI", "DUP", "EMPTY", "LAST")),
    ]
    edited = (
        ENV_FILE.replace("second-secret-4", "second-secret-8")
        .replace("dup-secret-6", "dup-secret-9")
        .replace("PLAIN=", "export PLAIN=")
        .replace("EMPTY=\n", "")
    )
    write_env(target, f"{edited}\nNEW=new-secret-10\n")
    assert env_answer_lines(target) == [
        "env-file present",
        "env-key PLAIN",
        "env-key SPACED",
        "env-key MULTI",
        "env-changed MULTI",
        "env-key DUP",
        "env-changed DUP",
        "env-key LAST",
        "env-key NEW",
        "env-changed NEW",
        "env-changed EMPTY",
    ]


@needs_shell
def test_the_diff_ignores_a_baseline_line_that_is_not_a_key_and_a_hash(tmp_path: Path) -> None:
    target = tmp_path / "edge"
    target.mkdir()
    write_env(target, "A=one-secret\nB=two-secret\n")
    good = hashlib.sha256(b"one-secret").hexdigest()
    bad = hashlib.sha256(b"two-secret").hexdigest()
    (target / ".aye-env").write_bytes(
        f"A {good}\nB {bad} extra\nroot:x:0:0 {bad}\nC {bad[:10]}\nD {bad.upper()}\n".encode()
    )
    done = sh(diff_script(target.as_posix()), b"")
    assert done.returncode == 0, done.stderr
    assert done.stdout.decode().splitlines() == [
        "env-file present",
        "env-key A",
        "env-key B",
        "env-changed B",
    ]


@needs_shell
def test_the_diff_does_not_read_an_env_that_is_not_a_file(tmp_path: Path) -> None:
    target = tmp_path / "edge"
    target.mkdir()
    (target / ".env").mkdir()
    done = sh(diff_script(target.as_posix()), b"")
    assert done.stdout == b"env-file unreadable\n"


@needs_shell
def test_the_diff_and_the_swap_do_not_follow_an_env_link(tmp_path: Path) -> None:
    target = tmp_path / "edge"
    target.mkdir()
    elsewhere = tmp_path / "secrets.env"
    elsewhere.write_bytes(b"ROOT_ONLY=root-secret\n")
    try:
        (target / ".env").symlink_to(elsewhere)
    except OSError:
        pytest.skip("this system cannot create a symbolic link")
    done = sh(diff_script(target.as_posix()), b"")
    if b"env-file link" not in done.stdout:
        pytest.skip("the shell does not see this symbolic link as one")
    assert done.stdout == b"env-file link\n"
    assert sh(swap_script(target.as_posix()), tarball({"a.yml": "a\n"})).returncode == 0
    assert (target / ".aye-env").read_bytes() == b""
