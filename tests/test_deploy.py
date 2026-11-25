from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path
from typing import Any

import pytest
import yaml

from ayeops import deploy as deploy_module
from ayeops.cli import main
from ayeops.defaults import UsageError
from ayeops.deploy import (
    HELPER,
    HELPER_DIR,
    INDEX,
    REMOTE_ROOT,
    STAGED,
    STAGING,
    STEPS,
    TIMED_OUT,
    TIMEOUT,
    Connection,
    Result,
    add_connection,
    chosen_hosts,
    compose_script,
    connection,
    deploy,
    diff_script,
    fleet,
    healthy,
    host_files,
    hostside,
    installed_units,
    parse_ps,
    parse_pull,
    parse_swap,
    pull_script,
    remote,
    remote_steps,
    ssh_runner,
    status_rows,
    swap_script,
    table,
    tarball,
    timers_script,
)
from ayeops.manifest import Manifest
from ayeops.mounts import (
    CONFIG_HASH_LABEL,
    bind_sources,
    config_hash,
    mounted_files,
    services_touching,
    stamp_config_hashes,
)
from ayeops.registry import load_modules
from ayeops.retire import RULES
from ayeops.rules import check_manifest
from ayeops.stamp import Stamp
from tests.conftest import EXAMPLE, RENDERED, WAIT, example_data, manifest_from, render_once

TOOLS = all(shutil.which(tool) for tool in ("sh", "tar", "cmp", "grep"))
needs_shell = pytest.mark.skipif(not TOOLS, reason="needs sh, tar, cmp and grep")
GZIP = b"\x1f\x8b"
PULL_FLAGS = "pull --quiet --policy missing --ignore-buildable"


def labels(data: dict[str, Any], stamp: Stamp | None = None) -> dict[str, str | None]:
    tree = render_once(manifest_from(data), load_modules(), stamp)
    compose = yaml.safe_load(tree["central/compose.yaml"])
    return {
        name: service["labels"].get(CONFIG_HASH_LABEL)
        for name, service in compose["services"].items()
    }


def test_bind_sources_reads_short_and_long_syntax_and_skips_volumes() -> None:
    service = {
        "volumes": [
            "grafana-data:/var/lib/grafana",
            "./grafana/provisioning:/etc/grafana/provisioning:ro",
            "./portal/:/usr/share/nginx/html",
            "./confX/:/x",
            {"type": "bind", "target": "/no-source"},
            {"type": "bind", "source": "./logs/loki.yaml", "target": "/etc/loki/loki.yaml"},
            {"type": "volume", "source": "./not-a-bind", "target": "/x"},
            "/etc/hosts:/etc/hosts:ro",
            "../outside:/x",
        ]
    }
    assert bind_sources(service) == ["grafana/provisioning", "portal", "confX", "logs/loki.yaml"]
    assert bind_sources({}) == []


def test_mounted_files_matches_whole_path_segments_only() -> None:
    service = {"volumes": ["./grafana/provisioning:/p", "./ingress/traefik.yml:/t"]}
    paths = [
        "grafana/provisioning/alerting/rules.yaml",
        "grafana/provisioning-old/x.yaml",
        "ingress/traefik.yml",
        "ingress/traefik.yml.bak",
        "compose.yaml",
    ]
    assert mounted_files(service, paths) == [
        "grafana/provisioning/alerting/rules.yaml",
        "ingress/traefik.yml",
    ]


def test_config_hash_follows_content_and_path_but_not_live_files() -> None:
    service = {"volumes": ["./conf:/conf"]}
    tree = {"conf/a.yml": "a: 1\n", "conf/live.yml": "x\n", "other": "o"}
    base = config_hash(service, tree, {"conf/live.yml"})
    assert base == "897485cf9d1538b2"
    assert config_hash(service, {**tree, "conf/live.yml": "y\n"}, {"conf/live.yml"}) == base
    assert config_hash(service, {**tree, "other": "p"}, {"conf/live.yml"}) == base
    assert config_hash(service, {**tree, "conf/a.yml": "a: 2\n"}, {"conf/live.yml"}) != base
    moved = {"conf/b.yml": "a: 1\n", "conf/live.yml": "x\n"}
    assert config_hash(service, moved, {"conf/live.yml"}) != base
    assert config_hash(service, {"conf/live.yml": "x\n"}, {"conf/live.yml"}) is None
    assert config_hash({"volumes": ["data:/d"]}, tree, set()) is None


def test_stamp_config_hashes_labels_only_services_with_mounted_config() -> None:
    compose: dict[str, Any] = {
        "name": "p",
        "services": {
            "a": {"labels": {"io.ayeops.service": "a"}, "volumes": ["./a.yml:/a.yml"]},
            "b": {"image": "b"},
        },
    }
    stamped = stamp_config_hashes(compose, {"a.yml": "x"}, set())
    assert stamped["name"] == "p"
    assert stamped["services"]["a"]["labels"]["io.ayeops.service"] == "a"
    assert len(stamped["services"]["a"]["labels"][CONFIG_HASH_LABEL]) == 16
    assert stamped["services"]["b"] == {"image": "b"}
    assert compose["services"]["a"]["labels"] == {"io.ayeops.service": "a"}


def test_a_changed_alert_rule_recreates_grafana_and_the_catalog_and_nothing_else(
    data: dict[str, Any],
) -> None:
    before = labels(data)
    data["services"]["web"]["alerts"][0]["for"] = "7m"
    after = labels(data)
    assert before["grafana"] is not None
    assert CONFIG_HASH_LABEL == "io.ayeops.config-hash"
    changed = {name for name in before if before[name] != after[name]}
    assert changed == {"grafana", "mcp-catalog"}


def test_routes_and_portal_pages_are_read_live_and_keep_their_containers(
    data: dict[str, Any],
) -> None:
    before = labels(data, Stamp("2026-01-01 00:00 UTC", "a"))
    data["hosts"]["central"]["modules"]["ingress"]["routes"]["extra"] = "grafana:3000"
    after = labels(data, Stamp("2026-02-02 00:00 UTC", "b"))
    assert before["traefik"] is not None
    assert after["traefik"] == before["traefik"]
    assert before["portal"] is not None
    assert after["portal"] == before["portal"]


def test_services_touching_names_each_service_with_its_changed_files() -> None:
    compose = {
        "services": {
            "grafana": {"volumes": ["./grafana/provisioning:/p"]},
            "vmagent": {"volumes": ["./vmagent/scrape.yaml:/s"]},
        }
    }
    changed = ["grafana/provisioning/alerting/rules.yaml", "compose.yaml"]
    assert services_touching(compose, changed) == {
        "grafana": ["grafana/provisioning/alerting/rules.yaml"]
    }
    assert services_touching({}, changed) == {}


def test_host_files_keeps_one_host_without_its_prefix() -> None:
    tree = {"central/compose.yaml": "c", "central-2/compose.yaml": "x", "edge/a": "e"}
    assert host_files(tree, "central") == {"compose.yaml": "c"}


def test_tarball_carries_every_file_and_the_index() -> None:
    blob = tarball({"b/run.sh": "echo\n", "a.yml": "a: 1\n", "c.py": ""})
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as archive:
        members = {member.name: member for member in archive.getmembers()}
        index = archive.extractfile(INDEX)
        assert index is not None
        assert index.read().decode() == "a.yml\nb/run.sh\nc.py\n"
        script = archive.extractfile("b/run.sh")
        assert script is not None
        assert script.read() == b"echo\n"
    assert sorted(members) == [".aye-files", "a.yml", "b/run.sh", "c.py"]
    assert members["b/run.sh"].mode == 0o755
    assert members["c.py"].mode == 0o755
    assert members["a.yml"].mode == 0o644
    assert members["a.yml"].uid == 0
    assert tarball({"a": "1"}) == tarball({"a": "1"})


def test_remote_wraps_the_script_for_ssh_and_optionally_sudo() -> None:
    assert remote("echo 'x'", False) == "sh -c 'echo '\"'\"'x'\"'\"''"
    assert remote("true", True) == "sudo sh -c true"
    assert compose_script("/opt/a b", "ps") == "cd '/opt/a b' && docker compose ps"


def test_remote_steps_carry_the_diff_step_for_every_mode() -> None:
    plain = remote_steps("/opt/ayeops/central", False, False)
    assert plain["diff"] == remote(diff_script("/opt/ayeops/central"), False)
    assert diff_script("/opt/a b") == f"set -- '/opt/a b'\n{hostside('diff.sh')}"
    assert remote_steps("/srv/x", True, False)["diff"].startswith("sudo sh -c 'set -- /srv/x\n")
    helper = remote_steps("/opt/ayeops/central", False, True)
    assert helper["diff"] == f"sudo -n {HELPER} /opt/ayeops/central diff"
    assert list(helper) == list(plain) == list(STEPS)
    assert plain["pull"] == remote(pull_script("/opt/ayeops/central"), False)
    assert pull_script("/opt/a b") == f"set -- '/opt/a b'\n{hostside('pull.sh')}"
    assert helper["pull"] == f"sudo -n {HELPER} /opt/ayeops/central pull"
    assert STEPS.index("pull") == STEPS.index("swap") - 1
    assert STEPS.index("rules") < STEPS.index("pull")


def test_parse_swap_reads_changed_and_removed_lines() -> None:
    out = "changed a/b.yml\nremoved old.yml\nnoise\nnoise here\nchanged \nremoved \nchanged c d\n"
    assert parse_swap(out) == (["a/b.yml", "c d"], ["old.yml"])


def test_parse_ps_reads_a_json_array_and_json_lines() -> None:
    rows = [{"Service": "a", "State": "running"}, {"Service": "b", "State": "exited"}]
    assert parse_ps(json.dumps(rows)) == rows
    assert parse_ps("\n".join(json.dumps(row) for row in rows) + "\n\n") == rows
    assert parse_ps("  ") == []
    assert parse_ps('[1, {"Service": "a"}]') == [{"Service": "a"}]


def test_status_rows_and_table() -> None:
    containers = [
        {"Service": "vmagent", "State": "running", "Health": ""},
        {"Service": "grafana", "State": "running", "Health": "healthy"},
        {"Service": "loki", "State": "exited"},
    ]
    rows = status_rows(containers, {"grafana": ["a", "b"]})
    assert rows == [
        ("grafana", "running", "healthy", "2 file(s)"),
        ("loki", "exited", "", ""),
        ("vmagent", "running", "", ""),
    ]
    assert status_rows([{"Service": "W"}, {}], {}) == [("", "", "", ""), ("W", "", "", "")]
    assert [healthy(row) for row in rows] == [True, False, True]
    assert not healthy(("x", "running", "unhealthy", ""))
    assert healthy(("x", "running", "starting", ""))
    assert table(rows).splitlines() == [
        "SERVICE  STATE    HEALTH   CONFIG CHANGED",
        "grafana  running  healthy  2 file(s)",
        "loki     exited",
        "vmagent  running",
    ]


class FakeSsh:
    def __init__(
        self,
        swap: Result,
        up: Result,
        states: dict[str, str] | None = None,
        ps: Result | None = None,
        drop: set[str] | None = None,
        live: Result | None = None,
        timers: Result | None = None,
        pull: Result | None = None,
    ) -> None:
        self.calls: list[tuple[list[str], bytes | None]] = []
        self.timeouts: list[float] = []
        self.swap = swap
        self.up = up
        self.states = states or {}
        self.ps = ps
        self.drop = drop or set()
        self.live = live or Result(0)
        self.timers = timers or Result(0, "changed a.service\nchanged a.timer\n")
        self.pull = pull or Result(0, f"{STAGED}\n")

    def __call__(self, argv: list[str], stdin: bytes | None, timeout: float) -> Result:
        self.calls.append((argv, stdin))
        self.timeouts.append(timeout)
        command = argv[-1]
        if command.endswith(" timers") or "\nU=/etc/systemd/system\n" in command:
            return self.timers
        if "then cat " in command or command.endswith(" rules"):
            return self.live
        if "--policy missing" in command or command.endswith(" pull"):
            return self.pull
        if "docker compose up" in command or command.endswith(" up"):
            return self.up
        if "docker compose ps" in command or command.endswith(" ps"):
            if self.ps is not None:
                return self.ps
            return Result(0, "\n".join(json.dumps(row) for row in self.rows()))
        return self.swap

    def rows(self) -> list[dict[str, str]]:
        compose = yaml.safe_load(
            (EXAMPLE.parent / "rendered" / "central" / "compose.yaml").read_text("utf-8")
        )
        return [
            {"Service": name, "State": self.states.get(name, "running"), "Health": ""}
            for name in compose["services"]
            if name not in self.drop
        ]


def test_deploy_ships_swaps_starts_and_reports(capsys: pytest.CaptureFixture[str]) -> None:
    runner = FakeSsh(Result(0, "changed grafana/provisioning/alerting/rules.yaml\n"), Result(0))
    code = deploy(EXAMPLE, "central", "ops-arm", True, None, runner, "2026-01-01 00:00 UTC", "x")
    assert code == 0
    assert [argv[:2] for argv, _ in runner.calls] == [["ssh", "ops-arm"]] * 5
    read, pull, swap, up, ps = (argv[-1] for argv, _ in runner.calls)
    assert read.startswith("sudo sh -c ")
    assert "cat /opt/ayeops/central/grafana/provisioning/alerting/rules.yaml" in read
    assert pull == remote(pull_script("/opt/ayeops/central"), True)
    assert runner.calls[1][1] == (RENDERED / "central" / "compose.yaml").read_bytes()
    assert swap.startswith("sudo sh -c ")
    assert swap.startswith("sudo sh -c 'set -- /opt/ayeops/central\nset -eu\nD=$1\n")
    assert "\nS=$D/.aye-staging\n" in swap
    assert "cd /opt/ayeops/central && docker compose up -d --remove-orphans" in up
    assert "ps --all --format json" in ps
    assert runner.calls[0][1] is None
    blob = runner.calls[2][1]
    assert blob is not None
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as archive:
        names = archive.getnames()
    assert "compose.yaml" in names
    assert "grafana/provisioning/alerting/rules.yaml" in names
    assert not any(name.startswith(("central/", "edge/")) for name in names)
    assert runner.calls[3][1] is None
    out = capsys.readouterr().out
    assert out.splitlines()[0] == "ops-arm:/opt/ayeops/central: 1 file(s) changed, 0 removed"
    assert any(
        line.split()[:4] == ["grafana", "running", "1", "file(s)"] for line in out.splitlines()
    )


def test_deploy_stops_before_compose_when_the_swap_fails(
    capsys: pytest.CaptureFixture[str],
) -> None:
    runner = FakeSsh(Result(2, "", "tar: unexpected end"), Result(0))
    assert deploy(EXAMPLE, "central", runner=runner) == 1
    assert len(runner.calls) == 3
    assert runner.calls[1][0][1] == "central"
    assert not runner.calls[1][0][-1].startswith("sudo")
    assert capsys.readouterr().err.splitlines() == [
        "tar: unexpected end",
        "central: files not swapped, the running stack is untouched",
    ]


def test_deploy_gives_every_ssh_call_the_timeout() -> None:
    runner = FakeSsh(Result(0), Result(0))
    assert deploy(EXAMPLE, "central", runner=runner, timeout=42) == 0
    assert runner.timeouts == [42] * 5
    default = FakeSsh(Result(0), Result(0))
    deploy(EXAMPLE, "central", runner=default)
    assert default.timeouts == [TIMEOUT] * 5
    assert TIMEOUT >= 1800


def test_a_swap_that_times_out_stops_before_compose_and_asks_for_another_deploy(
    capsys: pytest.CaptureFixture[str],
) -> None:
    runner = FakeSsh(Result(TIMED_OUT, "", "ssh timed out after 5s"), Result(0))
    assert deploy(EXAMPLE, "central", runner=runner, timeout=5) == 1
    assert len(runner.calls) == 3
    assert capsys.readouterr().err.splitlines() == [
        "ssh timed out after 5s",
        "central: swap timed out, docker compose did not run, deploy again to finish",
    ]


def test_a_compose_up_that_times_out_fails_the_deploy(
    capsys: pytest.CaptureFixture[str],
) -> None:
    runner = FakeSsh(Result(0), Result(TIMED_OUT, "", "ssh timed out after 5s"))
    assert deploy(EXAMPLE, "central", runner=runner, timeout=5) == 1
    assert len(runner.calls) == 4
    assert capsys.readouterr().err.splitlines() == [
        "ssh timed out after 5s",
        "central: docker compose up failed",
    ]


def test_deploy_fails_when_compose_up_fails(capsys: pytest.CaptureFixture[str]) -> None:
    runner = FakeSsh(Result(0), Result(1, "", "boom"))
    assert deploy(EXAMPLE, "central", runner=runner) == 1
    assert len(runner.calls) == 4
    assert capsys.readouterr().err.splitlines() == ["boom", "central: docker compose up failed"]


def test_deploy_fails_when_compose_ps_fails(capsys: pytest.CaptureFixture[str]) -> None:
    runner = FakeSsh(Result(0), Result(0), ps=Result(1, "", "no ps"))
    assert deploy(EXAMPLE, "central", runner=runner) == 1
    assert capsys.readouterr().err.splitlines() == ["no ps"]


def test_deploy_fails_when_a_declared_service_has_no_container(
    capsys: pytest.CaptureFixture[str],
) -> None:
    runner = FakeSsh(Result(0), Result(0), drop={"grafana"})
    assert deploy(EXAMPLE, "central", runner=runner) == 1
    assert capsys.readouterr().err.splitlines() == ["grafana: declared but has no container"]


def test_deploy_refuses_a_manifest_that_fails_the_check(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    data = yaml.safe_load(EXAMPLE.read_text("utf-8"))
    del data["services"]["web"]["description"]
    broken = tmp_path / "broken.yaml"
    broken.write_text(yaml.safe_dump(data), encoding="utf-8")
    runner = FakeSsh(Result(0), Result(0))
    assert deploy(broken, "central", runner=runner) == 1
    assert runner.calls == []
    err = capsys.readouterr().err.splitlines()
    assert any("[service-description] services.web" in line for line in err)
    assert err[-1] == f"{broken}: not deployed, fix the errors first"


def test_deploy_fails_when_a_service_is_not_running(capsys: pytest.CaptureFixture[str]) -> None:
    runner = FakeSsh(Result(0), Result(0), {"grafana": "restarting"})
    assert deploy(EXAMPLE, "central", runner=runner) == 1
    lines = capsys.readouterr().out.splitlines()
    assert ["grafana", "restarting"] in [line.split()[:2] for line in lines]


def test_deploy_refuses_an_unknown_host(capsys: pytest.CaptureFixture[str]) -> None:
    runner = FakeSsh(Result(0), Result(0))
    assert deploy(EXAMPLE, "nowhere", runner=runner) == 2
    assert runner.calls == []
    assert capsys.readouterr().err.splitlines() == [f"{EXAMPLE}: unknown host 'nowhere'"]


def test_result_defaults_to_empty_output_and_is_hashable() -> None:
    assert Result(3) == Result(3, "", "")
    assert hash(Result(3)) == hash(Result(3, "", ""))


def test_ssh_runner_feeds_stdin_and_decodes_both_streams_leniently() -> None:
    code = "import sys; b = sys.stdin.buffer.read(); sys.stdout.buffer.write(b); "
    code += "sys.stderr.buffer.write(b'e\\xff'); sys.exit(3)"
    done = ssh_runner([sys.executable, "-c", code], b"hi\xff", WAIT)
    assert done == Result(3, "hi\ufffd", "e\ufffd")


def test_ssh_runner_kills_a_hung_ssh_and_reports_the_timeout(tmp_path: Path) -> None:
    finished = tmp_path / "finished"
    hung = f"import pathlib, time; time.sleep({WAIT}); pathlib.Path({str(finished)!r}).touch()"
    done = ssh_runner([sys.executable, "-c", hung], None, 1)
    assert not finished.exists()
    assert done.code == TIMED_OUT
    assert done.err == f"{sys.executable} timed out after 1s"


def test_the_cli_takes_a_positive_timeout(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: list[Any] = []

    def fake(*args: Any, **kwargs: Any) -> int:
        seen.append(kwargs["timeout"])
        return 0

    monkeypatch.setattr(deploy_module, "deploy", fake)
    assert main(["deploy", "m.yaml", "--host", "h", "--timeout", "90.5"]) == 0
    assert seen == [90.5]
    for bad in ("0", "-5", "soon", "inf", "nan"):
        with pytest.raises(SystemExit):
            main(["deploy", "m.yaml", "--host", "h", "--timeout", bad])
        assert "is not a positive number of seconds" in capsys.readouterr().err


def test_the_cli_passes_every_deploy_option_through(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[Any, ...]] = []

    def fake(*args: Any, **kwargs: Any) -> int:
        seen.append((*args, kwargs))
        return 7

    monkeypatch.setattr(deploy_module, "deploy", fake)
    argv = ["deploy", "m.yaml", "--host", "h", "--ssh", "a", "--sudo", "--dir", "/x"]
    assert main([*argv, "--build-time", "t", "--commit", "c", "--timeout", "60"]) == 7
    options = {"time": "t", "commit": "c", "timeout": 60.0, "helper": False}
    assert seen == [(Path("m.yaml"), "h", "a", True, "/x", options)]
    assert main(["deploy", "m.yaml", "--host", "h"]) == 7
    defaults = {"time": None, "commit": None, "timeout": TIMEOUT, "helper": False}
    assert seen[-1] == (Path("m.yaml"), "h", None, False, None, defaults)
    assert main(["deploy", "m.yaml", "--host", "h", "--helper"]) == 7
    assert seen[-1][3] is False
    assert seen[-1][5]["helper"] is True


def test_the_cli_refuses_sudo_and_helper_together(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        main(["deploy", "m.yaml", "--host", "h", "--sudo", "--helper"])
    assert "not allowed with argument" in capsys.readouterr().err


def sh(script: str, stdin: bytes) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(["sh", "-c", script], input=stdin, capture_output=True, check=False)


@needs_shell
def test_swap_replaces_changed_files_by_rename_and_keeps_the_rest(tmp_path: Path) -> None:
    target = tmp_path / "central"
    first = {"ingress/dynamic/routes.yml": "a: 1\n", "same.yml": "s\n", "gone.yml": "g\n"}
    done = sh(swap_script(target.as_posix()), tarball(first))
    assert done.returncode == 0, done.stderr
    assert sorted(parse_swap(done.stdout.decode())[0]) == sorted(first)
    (target / ".env").write_text("SECRET=1\n", encoding="utf-8")
    same_inode = (target / "same.yml").stat().st_ino
    second = {"ingress/dynamic/routes.yml": "a: 2\n", "same.yml": "s\n", "new.yml": "n\n"}
    done = sh(swap_script(target.as_posix()), tarball(second))
    assert done.returncode == 0, done.stderr
    changed, removed = parse_swap(done.stdout.decode())
    assert sorted(changed) == ["ingress/dynamic/routes.yml", "new.yml"]
    assert removed == ["gone.yml"]
    assert (target / "ingress/dynamic/routes.yml").read_text(encoding="utf-8") == "a: 2\n"
    assert (target / "same.yml").stat().st_ino == same_inode
    assert not (target / "gone.yml").exists()
    assert (target / ".env").read_text(encoding="utf-8") == "SECRET=1\n"
    assert not (target / ".aye-staging").exists()
    assert (target / ".aye-files").read_text(encoding="utf-8").split() == sorted(second)


@needs_shell
def test_a_truncated_upload_leaves_the_live_files_untouched(tmp_path: Path) -> None:
    target = tmp_path / "central"
    sh(swap_script(target.as_posix()), tarball({"ingress/dynamic/routes.yml": "a: 1\n"}))
    big = {"ingress/dynamic/routes.yml": "b: 2\n" * 50000, "z.yml": "z\n" * 50000}
    blob = tarball(big)
    done = sh(swap_script(target.as_posix()), blob[: len(blob) // 2])
    assert done.returncode != 0
    assert (target / "ingress/dynamic/routes.yml").read_text(encoding="utf-8") == "a: 1\n"
    assert not (target / "z.yml").exists()


def test_the_cli_needs_a_host_to_deploy_a_fleet(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["deploy", str(EXAMPLE)]) == 2
    assert "name one with --host <name> or --all: central, edge" in capsys.readouterr().err


def live_rules_with(uid: str) -> str:
    rendered = yaml.safe_load((EXAMPLE.parent / "rendered" / "central" / RULES).read_text("utf-8"))
    rendered["groups"][0]["rules"].append({"uid": uid, "title": uid})
    return yaml.safe_dump(rendered)


def shipped_rules(runner: FakeSsh) -> dict[str, Any]:
    blob = next(stdin for _, stdin in runner.calls if stdin and stdin.startswith(GZIP))
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as archive:
        member = archive.extractfile(RULES)
        assert member is not None
        return dict(yaml.safe_load(member.read()))


def test_deploy_ships_a_deletion_for_a_rule_that_left_the_manifest(
    capsys: pytest.CaptureFixture[str],
) -> None:
    runner = FakeSsh(Result(0), Result(0), live=Result(0, live_rules_with("aye-retired")))
    assert deploy(EXAMPLE, "central", runner=runner) == 0
    assert shipped_rules(runner)["deleteRules"] == [{"orgId": 1, "uid": "aye-retired"}]
    assert (
        "central: alert rule aye-retired left the manifest, Grafana deletes it on start"
        in capsys.readouterr().out.splitlines()
    )


def test_deploy_without_retired_rules_ships_the_render_unchanged() -> None:
    unchanged = FakeSsh(Result(0), Result(0))
    assert deploy(EXAMPLE, "central", runner=unchanged) == 0
    assert "deleteRules" not in shipped_rules(unchanged)


@pytest.mark.parametrize(
    ("live", "reason"),
    [
        (Result(255, "", "ssh: connect to host ops-arm port 22: timed out"), "ssh: connect"),
        (Result(TIMED_OUT, "", "ssh timed out after 1800s"), "ssh timed out after 1800s"),
        (Result(0, "- not\n- a mapping\n"), "alert rules file is not a mapping"),
        (Result(0, "groups: [unclosed\n"), "while parsing"),
    ],
)
def test_deploy_stops_before_the_swap_when_the_live_rules_are_unreadable(
    live: Result, reason: str, capsys: pytest.CaptureFixture[str]
) -> None:
    runner = FakeSsh(Result(0), Result(0), live=live)
    assert deploy(EXAMPLE, "central", runner=runner) == 1
    assert len(runner.calls) == 1
    err = capsys.readouterr().err
    assert reason in err
    assert err.splitlines()[-1] == (
        "central: live alert rules unreadable, the running stack is untouched"
    )


def test_a_host_without_grafana_is_not_asked_for_alert_rules() -> None:
    runner = FakeSsh(Result(0), Result(0))
    deploy(EXAMPLE, "edge", runner=runner)
    assert not any("then cat " in argv[-1] for argv, _ in runner.calls)
    assert runner.calls[0][1] is not None


def test_deploy_with_the_helper_runs_each_step_through_sudo_and_the_helper(
    capsys: pytest.CaptureFixture[str],
) -> None:
    runner = FakeSsh(Result(0, "changed compose.yaml\n"), Result(0))
    code = deploy(EXAMPLE, "central", "aye@ops", runner=runner, helper=True)
    assert code == 0
    helper = "sudo -n /usr/local/libexec/ayeops/aye-host /opt/ayeops/central"
    assert [argv for argv, _ in runner.calls] == [
        ["ssh", "aye@ops", f"{helper} {step}"] for step in ("rules", "pull", "swap", "up", "ps")
    ]
    assert [stdin is None for _, stdin in runner.calls] == [True, False, False, True, True]
    assert capsys.readouterr().out.splitlines()[0] == (
        "aye@ops:/opt/ayeops/central: 1 file(s) changed, 0 removed"
    )


def test_deploy_with_the_helper_stops_before_compose_when_the_swap_fails(
    capsys: pytest.CaptureFixture[str],
) -> None:
    runner = FakeSsh(Result(64, "", "aye-host: refused"), Result(0))
    assert deploy(EXAMPLE, "central", runner=runner, helper=True) == 1
    assert len(runner.calls) == 3
    assert capsys.readouterr().err.splitlines() == [
        "aye-host: refused",
        "central: files not swapped, the running stack is untouched",
    ]


@pytest.mark.parametrize("target", ["/srv/central", "/opt/ayeops/a/b", "/opt/ayeops/.x"])
def test_deploy_with_the_helper_refuses_a_directory_the_helper_refuses(
    target: str, capsys: pytest.CaptureFixture[str]
) -> None:
    runner = FakeSsh(Result(0), Result(0))
    assert deploy(EXAMPLE, "central", directory=target, runner=runner, helper=True) == 2
    assert runner.calls == []
    assert capsys.readouterr().err.strip() == (
        f"--helper deploys only to /opt/ayeops/<name>, not {target}"
    )


def test_deploy_refuses_sudo_and_helper_together(capsys: pytest.CaptureFixture[str]) -> None:
    runner = FakeSsh(Result(0), Result(0))
    assert deploy(EXAMPLE, "central", sudo=True, runner=runner, helper=True) == 2
    assert runner.calls == []
    assert "exclude each other" in capsys.readouterr().err


def test_the_helper_and_the_swap_name_the_paths_deploy_uses() -> None:
    helper = hostside("aye-host.sh")
    assert f"\nRULES={RULES}\n" in helper
    assert f"\nBASE={REMOTE_ROOT}\n" in helper
    assert f"\nLIB={HELPER_DIR}\n" in helper
    assert f"{HELPER_DIR}/aye-host" == HELPER
    assert all(f"\n    {step})" in helper for step in STEPS)
    swap = hostside("swap.sh")
    assert f"\nS=$D/{STAGING}\n" in swap
    assert f"< {INDEX}\n" in swap


def bad_archive(members: list[tuple[str, bytes | None, str | None]]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz", format=tarfile.PAX_FORMAT) as archive:
        for name, data, link in members:
            info = tarfile.TarInfo(name)
            if link is not None:
                info.type = tarfile.SYMTYPE
                info.linkname = link
                archive.addfile(info)
            else:
                info.size = len(data or b"")
                archive.addfile(info, io.BytesIO(data or b""))
    return buffer.getvalue()


@needs_shell
@pytest.mark.parametrize("line", ["../escape.yml", "/etc/passwd", "a/./b", "a//b", ".aye-x", ""])
def test_the_swap_refuses_an_index_line_outside_the_directory(line: str, tmp_path: Path) -> None:
    target = tmp_path / "central"
    sh(swap_script(target.as_posix()), tarball({"keep.yml": "k\n"}))
    index = f"keep.yml\n{line}\n".encode()
    blob = bad_archive([("keep.yml", b"new\n", None), (INDEX, index, None)])
    done = sh(swap_script(target.as_posix()), blob)
    assert done.returncode == 1
    assert b"refused: unsafe path" in done.stderr
    assert (target / "keep.yml").read_text(encoding="utf-8") == "k\n"


@needs_shell
@pytest.mark.skipif(sys.platform == "win32", reason="MSYS tar extracts a link as a copy")
def test_the_swap_refuses_an_archive_with_a_link(tmp_path: Path) -> None:
    target = tmp_path / "central"
    sh(swap_script(target.as_posix()), tarball({"keep.yml": "k\n"}))
    blob = bad_archive(
        [("keep.yml", b"new\n", None), ("etc", None, "/etc"), (INDEX, b"keep.yml\netc\n", None)]
    )
    done = sh(swap_script(target.as_posix()), blob)
    assert done.returncode == 1
    assert b"refused: the archive holds links" in done.stderr
    assert (target / "keep.yml").read_text(encoding="utf-8") == "k\n"
    assert not (target / "etc").exists()


@needs_shell
def test_the_swap_ignores_an_unsafe_line_left_in_the_old_index(tmp_path: Path) -> None:
    target = tmp_path / "central"
    sh(swap_script(target.as_posix()), tarball({"keep.yml": "k\n"}))
    outside = tmp_path / "outside.yml"
    outside.write_text("o\n", encoding="utf-8")
    (target / INDEX).write_text("keep.yml\n../outside.yml\n", encoding="utf-8")
    done = sh(swap_script(target.as_posix()), tarball({"keep.yml": "k\n"}))
    assert done.returncode == 0, done.stderr
    assert outside.read_text(encoding="utf-8") == "o\n"


def edge_ps() -> Result:
    compose = yaml.safe_load(
        (EXAMPLE.parent / "rendered" / "edge" / "compose.yaml").read_text("utf-8")
    )
    rows = [{"Service": name, "State": "running"} for name in compose["services"]]
    return Result(0, json.dumps(rows))


def test_deploy_installs_the_backup_timers_after_compose_up(
    capsys: pytest.CaptureFixture[str],
) -> None:
    runner = FakeSsh(Result(0, "changed compose.yaml\n"), Result(0), ps=edge_ps())
    assert deploy(EXAMPLE, "edge", "ops-edge", True, runner=runner) == 0
    _, _, up, timers, ps = (argv[-1] for argv, _ in runner.calls)
    assert "docker compose up" in up and "docker compose ps" in ps
    assert timers.startswith("sudo sh -c 'set -- /opt/ayeops/edge\nset -eu\nD=$1\n")
    assert "\nU=/etc/systemd/system\n" in timers
    assert runner.calls[3][1] is None
    with tarfile.open(fileobj=io.BytesIO(runner.calls[1][1] or b""), mode="r:gz") as archive:
        names = archive.getnames()
    assert "backups/systemd/ayeops-edge-backup-acme-db.timer" in names
    assert "backups/jobs/acme-db.sh" in names
    out = capsys.readouterr().out.splitlines()
    assert out[1] == "ops-edge: 1 backup timer(s), 2 unit file(s) changed, 0 removed"


def test_deploy_with_the_helper_runs_the_timers_step_of_the_helper() -> None:
    runner = FakeSsh(Result(0), Result(0), ps=edge_ps(), timers=Result(0))
    assert deploy(EXAMPLE, "edge", "aye@edge", runner=runner, helper=True) == 0
    helper = f"sudo -n {HELPER} /opt/ayeops/edge"
    assert [argv[-1] for argv, _ in runner.calls] == [
        f"{helper} {step}" for step in ("pull", "swap", "up", "timers", "ps")
    ]


def test_a_host_without_backup_units_never_runs_the_timers_step() -> None:
    runner = FakeSsh(Result(0), Result(0))
    assert deploy(EXAMPLE, "central", runner=runner) == 0
    assert not any(argv[-1].endswith(" timers") or "U=/etc" in argv[-1] for argv, _ in runner.calls)


def test_units_the_swap_removed_are_taken_off_the_host_too(
    capsys: pytest.CaptureFixture[str],
) -> None:
    removed = "removed backups/systemd/ayeops-central-backup-old.timer\n"
    runner = FakeSsh(
        Result(0, removed), Result(0), timers=Result(0, "removed ayeops-central-backup-old.timer\n")
    )
    assert deploy(EXAMPLE, "central", runner=runner, helper=True) == 0
    assert [argv[-1].split()[-1] for argv, _ in runner.calls] == [
        "rules",
        "pull",
        "swap",
        "up",
        "timers",
        "ps",
    ]
    assert "central: 0 backup timer(s), 0 unit file(s) changed, 1 removed" in (
        capsys.readouterr().out.splitlines()
    )


def test_the_deploy_after_a_failed_removal_of_the_last_timer_takes_the_units_off(
    capsys: pytest.CaptureFixture[str],
) -> None:
    removed = "removed backups/systemd/ayeops-central-backup-old.timer\n"
    first = FakeSsh(Result(0, removed), Result(0), timers=Result(1, "", "refused: x"))
    assert deploy(EXAMPLE, "central", runner=first, helper=True) == 1
    capsys.readouterr()
    installed = (
        "installed ayeops-central-backup-old.service\ninstalled ayeops-central-backup-old.timer\n"
    )
    gone = "removed ayeops-central-backup-old.service\nremoved ayeops-central-backup-old.timer\n"
    second = FakeSsh(Result(0, installed), Result(0), timers=Result(0, gone))
    assert deploy(EXAMPLE, "central", runner=second, helper=True) == 0
    assert [argv[-1].split()[-1] for argv, _ in second.calls] == [
        "rules",
        "pull",
        "swap",
        "up",
        "timers",
        "ps",
    ]
    out = capsys.readouterr().out.splitlines()
    assert "central: 0 backup timer(s), 0 unit file(s) changed, 2 removed" in out
    assert out[0] == "central:/opt/ayeops/central: 0 file(s) changed, 0 removed"


def test_installed_units_reads_only_the_installed_lines_of_the_swap() -> None:
    out = "changed a.yml\ninstalled ayeops-edge-backup-db.timer\nremoved b.yml\ninstalled \n"
    assert installed_units(out) == ["ayeops-edge-backup-db.timer"]
    assert parse_swap(out) == (["a.yml"], ["b.yml"])


@needs_shell
def test_the_swap_names_the_backup_units_of_its_host_still_installed(tmp_path: Path) -> None:
    units = tmp_path / "etc-systemd"
    units.mkdir()
    for name in (
        "ayeops-edge-backup-db.timer",
        "ayeops-edge-backup-db.service",
        "ayeops-central-backup-db.timer",
        "ayeops-edge-other.timer",
    ):
        (units / name).write_text("[Unit]\n", encoding="utf-8")
    script = swap_script((tmp_path / "edge").as_posix())
    assert script.count("/etc/systemd/system/") == 1
    done = sh(script.replace("/etc/systemd/system/", f"{units.as_posix()}/"), tarball({}))
    assert done.returncode == 0, done.stderr
    assert installed_units(done.stdout.decode()) == [
        "ayeops-edge-backup-db.service",
        "ayeops-edge-backup-db.timer",
    ]
    empty = tmp_path / "empty"
    empty.mkdir()
    again = sh(script.replace("/etc/systemd/system/", f"{empty.as_posix()}/"), tarball({}))
    assert again.returncode == 0, again.stderr
    assert again.stdout.decode() == ""


def test_a_failed_timers_step_fails_the_deploy_after_the_stack_is_up(
    capsys: pytest.CaptureFixture[str],
) -> None:
    refused = Result(64, "", "aye-host: usage: aye-host </opt/ayeops/name> rules|swap|up|ps|diff")
    runner = FakeSsh(Result(0), Result(0), ps=edge_ps(), timers=refused)
    assert deploy(EXAMPLE, "edge", runner=runner, helper=True) == 1
    assert runner.calls[-1][0][-1].endswith(" ps")
    err = capsys.readouterr().err.splitlines()
    assert err[:2] == [refused.err, "edge: backup timers not installed"]
    assert "run the aye host-init script of this host again" in err[2]
    plain = FakeSsh(Result(0), Result(0), ps=edge_ps(), timers=Result(1, "", "refused: x"))
    assert deploy(EXAMPLE, "edge", runner=plain, sudo=True) == 1
    assert capsys.readouterr().err.splitlines() == [
        "refused: x",
        "edge: backup timers not installed",
    ]
    timed = FakeSsh(
        Result(0), Result(0), ps=edge_ps(), timers=Result(TIMED_OUT, "", "ssh timed out")
    )
    assert deploy(EXAMPLE, "edge", runner=timed, helper=True) == 1
    assert "host-init" not in capsys.readouterr().err


def test_a_host_with_backup_timers_deploys_only_to_its_own_directory(
    capsys: pytest.CaptureFixture[str],
) -> None:
    runner = FakeSsh(Result(0), Result(0), ps=edge_ps())
    assert deploy(EXAMPLE, "edge", directory="/srv/edge", runner=runner) == 2
    assert runner.calls == []
    assert capsys.readouterr().err.strip() == (
        "edge: its backup timers run the jobs from /opt/ayeops/edge,"
        " deploy it there and not to /srv/edge"
    )
    assert deploy(EXAMPLE, "edge", directory="/opt/ayeops/edge", runner=runner) == 0
    central = FakeSsh(Result(0), Result(0))
    assert deploy(EXAMPLE, "central", directory="/srv/central", runner=central) == 0
    many = FakeSsh(Result(0), Result(0))
    assert fleet(EXAMPLE, ["central", "edge"], directory="/srv/x", runner=many) == 2
    assert many.calls == []
    assert "edge: its backup timers run the jobs from /opt/ayeops/edge" in capsys.readouterr().err


TIMER = "[Timer]\nOnCalendar=*-*-* 04:30:00 UTC\n"


def timers_env(tmp_path: Path) -> tuple[Path, Path, dict[str, str]]:
    units = tmp_path / "etc-systemd"
    units.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "systemctl"
    fake.write_text('#!/bin/sh\necho "$*" >> "$LOG"\n', encoding="utf-8", newline="\n")
    fake.chmod(0o755)
    log = tmp_path / "systemctl.log"
    path = f"{bin_dir.as_posix()}{os.pathsep}{os.environ.get('PATH', '')}"
    return units, log, {"PATH": path, "LOG": log.as_posix()}


def run_timers(
    target: Path, units: Path, env: dict[str, str]
) -> subprocess.CompletedProcess[bytes]:
    script = timers_script(target.as_posix()).replace(
        "U=/etc/systemd/system\n", f"U={units.as_posix()}\n"
    )
    return subprocess.run(
        [shutil.which("sh") or "sh", "-c", script],
        capture_output=True,
        check=False,
        env={**os.environ, **env},
    )


def rendered_units(target: Path, files: dict[str, str]) -> None:
    folder = target / "backups" / "systemd"
    shutil.rmtree(folder, ignore_errors=True)
    folder.mkdir(parents=True)
    for name, text in files.items():
        (folder / name).write_text(text, encoding="utf-8", newline="\n")


def systemctl_calls(log: Path) -> list[str]:
    calls = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    log.unlink(missing_ok=True)
    return calls


@needs_shell
def test_the_timers_step_installs_enables_and_starts_the_units_once(tmp_path: Path) -> None:
    units, log, env = timers_env(tmp_path)
    target = tmp_path / "edge"
    unit = "ayeops-edge-backup-acme-db"
    rendered_units(target, {f"{unit}.service": "[Service]\n", f"{unit}.timer": TIMER})
    done = run_timers(target, units, env)
    assert done.returncode == 0, done.stderr
    assert done.stdout.decode().splitlines() == [
        f"changed {unit}.service",
        f"changed {unit}.timer",
    ]
    assert (units / f"{unit}.timer").read_text(encoding="utf-8") == TIMER
    assert sorted(path.name for path in units.iterdir()) == [f"{unit}.service", f"{unit}.timer"]
    assert systemctl_calls(log) == [
        "daemon-reload",
        f"--quiet enable {unit}.timer",
        f"restart {unit}.timer",
    ]
    again = run_timers(target, units, env)
    assert (again.returncode, again.stdout) == (0, b"")
    assert systemctl_calls(log) == [f"--quiet enable {unit}.timer", f"start {unit}.timer"]
    rendered_units(target, {f"{unit}.service": "[Service]\n", f"{unit}.timer": TIMER + "#\n"})
    changed = run_timers(target, units, env)
    assert changed.stdout.decode().splitlines() == [f"changed {unit}.timer"]
    assert systemctl_calls(log) == [
        "daemon-reload",
        f"--quiet enable {unit}.timer",
        f"restart {unit}.timer",
    ]


@needs_shell
def test_the_timers_step_removes_the_units_of_its_host_that_left_the_render(
    tmp_path: Path,
) -> None:
    units, log, env = timers_env(tmp_path)
    target = tmp_path / "edge"
    for name in ("ayeops-edge-backup-old.timer", "ayeops-edge-backup-old.service"):
        (units / name).write_text("x\n", encoding="utf-8")
    for name in ("ayeops-central-backup-db.timer", "ssh.service", "ayeops-edge-other.timer"):
        (units / name).write_text("keep\n", encoding="utf-8")
    target.mkdir()
    done = run_timers(target, units, env)
    assert done.returncode == 0, done.stderr
    assert sorted(done.stdout.decode().splitlines()) == [
        "removed ayeops-edge-backup-old.service",
        "removed ayeops-edge-backup-old.timer",
    ]
    assert sorted(path.name for path in units.iterdir()) == [
        "ayeops-central-backup-db.timer",
        "ayeops-edge-other.timer",
        "ssh.service",
    ]
    assert systemctl_calls(log) == [
        "--quiet disable --now ayeops-edge-backup-old.timer",
        "daemon-reload",
    ]
    nothing = run_timers(target, units, env)
    assert (nothing.returncode, nothing.stdout) == (0, b"")
    assert systemctl_calls(log) == []


@needs_shell
@pytest.mark.parametrize(
    "name",
    [
        "ssh.service",
        "ayeops-central-backup-db.timer",
        "ayeops-edge-backup-db.path",
        "ayeops-edge-backup-db.timer.d",
        "ayeops-edge-backup-a b.timer",
    ],
)
def test_the_timers_step_refuses_any_file_but_a_backup_unit_of_its_host(
    tmp_path: Path, name: str
) -> None:
    units, log, env = timers_env(tmp_path)
    target = tmp_path / "edge"
    rendered_units(target, {"ayeops-edge-backup-db.timer": TIMER, name: "[Service]\n"})
    done = run_timers(target, units, env)
    assert done.returncode == 1
    assert done.stderr.startswith(b"refused: ")
    assert list(units.iterdir()) == []
    assert systemctl_calls(log) == []


@needs_shell
def test_the_timers_step_refuses_a_directory_in_place_of_a_unit(tmp_path: Path) -> None:
    units, _, env = timers_env(tmp_path)
    target = tmp_path / "edge"
    rendered_units(target, {"ayeops-edge-backup-db.timer": TIMER})
    (target / "backups" / "systemd" / "ayeops-edge-backup-db.service").mkdir()
    done = run_timers(target, units, env)
    assert done.returncode == 1
    assert b"is not a regular file" in done.stderr
    assert list(units.iterdir()) == []


@needs_shell
def test_the_timers_step_needs_systemctl(tmp_path: Path) -> None:
    units = tmp_path / "units"
    units.mkdir()
    target = tmp_path / "edge"
    rendered_units(target, {"ayeops-edge-backup-db.timer": TIMER})
    empty = tmp_path / "empty"
    empty.mkdir()
    done = run_timers(target, units, {"PATH": empty.as_posix()})
    assert done.returncode == 1
    assert b"systemctl is missing" in done.stderr
    assert list(units.iterdir()) == []


def test_the_timers_step_names_the_unit_directory_and_the_render_folder() -> None:
    script = hostside("timers.sh")
    assert "\nU=/etc/systemd/system\n" in script
    assert "\nS=$D/backups/systemd\n" in script
    assert "\nP=ayeops-$N-backup-\n" in script
    assert timers_script("/opt/ayeops/edge").startswith("set -- /opt/ayeops/edge\nset -eu\n")


def test_parse_pull_reads_the_staged_mark_and_the_failed_services() -> None:
    out = "staged\nfailed vmagent\nfailed \nfailed loki\nstaged later\nnoise\n"
    assert parse_pull(out) == (True, ["vmagent", "loki"])
    assert parse_pull("failed a\n") == (False, ["a"])
    assert parse_pull("staged later\nstaged:\n") == (False, [])
    assert parse_pull("") == (False, [])
    assert STAGED == "staged"


def test_deploy_pulls_on_the_staged_compose_before_it_swaps() -> None:
    runner = FakeSsh(Result(0), Result(0))
    assert deploy(EXAMPLE, "central", runner=runner, helper=True) == 0
    steps = [argv[-1].split()[-1] for argv, _ in runner.calls]
    assert steps == ["rules", "pull", "swap", "up", "ps"]
    assert runner.calls[1][1] == (RENDERED / "central" / "compose.yaml").read_bytes()
    blob = runner.calls[2][1]
    assert blob is not None
    assert blob.startswith(GZIP)


def central_image(service: str) -> str:
    compose = yaml.safe_load((RENDERED / "central" / "compose.yaml").read_text("utf-8"))
    return str(compose["services"][service]["image"])


def test_a_failed_pull_stops_before_the_swap_and_names_each_image(
    capsys: pytest.CaptureFixture[str],
) -> None:
    pulled = Result(1, "staged\nfailed vmagent\nfailed ghost\n", "manifest unknown\n")
    runner = FakeSsh(Result(0), Result(0), pull=pulled)
    assert deploy(EXAMPLE, "central", runner=runner) == 1
    assert len(runner.calls) == 2
    assert "--policy missing" in runner.calls[1][0][-1]
    assert not any(stdin and stdin.startswith(GZIP) for _, stdin in runner.calls)
    assert capsys.readouterr().err.splitlines() == [
        "manifest unknown",
        f"central: image {central_image('vmagent')} of vmagent did not pull",
        "central: ghost did not pull",
        "central: images not pulled, the running stack is untouched",
    ]


def test_a_pull_that_times_out_stops_before_the_swap(capsys: pytest.CaptureFixture[str]) -> None:
    runner = FakeSsh(Result(0), Result(0), pull=Result(TIMED_OUT, "", "ssh timed out after 5s"))
    assert deploy(EXAMPLE, "edge", runner=runner, timeout=5, helper=True) == 1
    assert len(runner.calls) == 1
    assert capsys.readouterr().err.splitlines() == [
        "ssh timed out after 5s",
        "edge: pull timed out, the running stack is untouched",
    ]


@pytest.mark.parametrize(
    ("pulled", "hint"),
    [
        (Result(1, "", "sudo: a password is required"), True),
        (Result(64, "", "aye-host: usage: aye-host <dir> rules|swap|up|ps|diff"), True),
        (Result(1, "staged\n", "required variable AYE_X is missing a value"), False),
        (Result(255, "", "ssh: connect to host edge port 22: No route to host"), False),
    ],
)
def test_a_helper_without_the_pull_step_is_told_to_run_host_init_again(
    pulled: Result, hint: bool, capsys: pytest.CaptureFixture[str]
) -> None:
    runner = FakeSsh(Result(0), Result(0), pull=pulled)
    assert deploy(EXAMPLE, "edge", runner=runner, helper=True) == 1
    assert len(runner.calls) == 1
    err = capsys.readouterr().err.splitlines()
    assert err[:2] == [pulled.err, "edge: images not pulled, the running stack is untouched"]
    assert (len(err) == 3) is hint
    if hint:
        assert err[2] == (
            f"edge: --helper needs the pull step of {HELPER}; "
            "run the aye host-init script of this host again if it predates the pull step"
        )


def test_without_the_helper_a_pull_that_never_staged_gets_no_host_init_hint(
    capsys: pytest.CaptureFixture[str],
) -> None:
    runner = FakeSsh(Result(0), Result(0), pull=Result(1, "", "mkdir: Permission denied"))
    assert deploy(EXAMPLE, "edge", runner=runner) == 1
    assert capsys.readouterr().err.splitlines() == [
        "mkdir: Permission denied",
        "edge: images not pulled, the running stack is untouched",
    ]


FAKE_COMPOSE = """#!/bin/sh
echo "$*" >> "$STATE/docker.log"
prev=
for a in "$@"; do
  if [ "$prev" = -f ]; then cp "$a" "$STATE/seen.yaml"; fi
  prev=$a
done
case " $* " in
  *" config --services "*) echo good; echo bad; exit 0 ;;
  *" ps "*) cat "$STATE/ps.json" 2>/dev/null; exit 0 ;;
  *" pull "*) ;;
  *) exit 0 ;;
esac
if [ -f "$STATE/fail" ]; then
  case $prev in
    --ignore-buildable) echo "Error response from daemon: manifest unknown" >&2; exit 1 ;;
    bad) echo "no matching manifest for linux/arm64 in the manifest list entries" >&2; exit 1 ;;
  esac
fi
exit 0
"""
SHELL_TOOLS = ("sh", "tar", "cmp", "grep", "cat", "cp", "mkdir", "rm", "mv", "find", "dirname")


def compose_env(tmp_path: Path) -> tuple[dict[str, str], Path]:
    state = tmp_path / "state"
    bin_dir = tmp_path / "bin"
    state.mkdir()
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(FAKE_COMPOSE, encoding="utf-8", newline="\n")
    docker.chmod(0o755)
    found = {str(Path(found).parent) for tool in SHELL_TOOLS if (found := shutil.which(tool))}
    names = ("docker", "docker.exe", "docker.cmd", "docker.bat")
    if os.name == "posix":
        for folder in sorted(found):
            for entry in Path(folder).iterdir():
                link = bin_dir / entry.name
                if entry.name not in names and not link.is_symlink() and not link.exists():
                    link.symlink_to(entry)
        return {**os.environ, "PATH": str(bin_dir), "STATE": state.as_posix()}, state
    assert not [name for folder in found for name in names if (Path(folder) / name).exists()]
    path = os.pathsep.join([str(bin_dir), *sorted(found)])
    return {**os.environ, "PATH": path, "STATE": state.as_posix()}, state


def sh_env(script: str, stdin: bytes, env: dict[str, str]) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["sh", "-c", script], input=stdin, capture_output=True, check=False, env=env
    )


def local_runner(env: dict[str, str]) -> Any:
    def local(argv: list[str], stdin: bytes | None, timeout: float) -> Result:
        done = sh_env(argv[-1], stdin or b"", env)
        return Result(done.returncode, done.stdout.decode(), done.stderr.decode())

    return local


@needs_shell
def test_the_pull_runs_on_a_staged_copy_and_leaves_the_live_directory_alone(
    tmp_path: Path,
) -> None:
    env, state = compose_env(tmp_path)
    target = tmp_path / "central"
    target.mkdir()
    (target / "compose.yaml").write_text("old\n", encoding="utf-8", newline="\n")
    done = sh_env(pull_script(target.as_posix()), b"services: {}\n", env)
    assert done.returncode == 0, done.stderr
    assert done.stdout == b"staged\n"
    assert (state / "seen.yaml").read_bytes() == b"services: {}\n"
    staged = f"{target.as_posix()}/{STAGING}/compose.yaml"
    log = (state / "docker.log").read_text("utf-8").splitlines()
    assert log == [f"compose --project-directory {target.as_posix()} -f {staged} {PULL_FLAGS}"]
    assert (target / "compose.yaml").read_text("utf-8") == "old\n"
    assert sorted(path.name for path in target.iterdir()) == ["compose.yaml"]


@needs_shell
def test_a_failed_pull_names_the_services_that_did_not_pull_and_cleans_up(
    tmp_path: Path,
) -> None:
    env, state = compose_env(tmp_path)
    (state / "fail").write_text("", encoding="utf-8")
    target = tmp_path / "central"
    done = sh_env(pull_script(target.as_posix()), b"services: {}\n", env)
    assert done.returncode == 1
    assert done.stdout == b"staged\nfailed bad\n"
    assert b"manifest unknown" in done.stderr
    assert b"no matching manifest for linux/arm64" in done.stderr
    log = (state / "docker.log").read_text("utf-8").splitlines()
    assert [line.split(" -f ")[-1].split(" ", 1)[-1] for line in log] == [
        PULL_FLAGS,
        "config --services",
        f"{PULL_FLAGS} good",
        f"{PULL_FLAGS} bad",
    ]
    assert list(target.iterdir()) == []


@needs_shell
def test_a_deploy_whose_pull_fails_keeps_the_live_files_and_never_runs_up(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    env, state = compose_env(tmp_path)
    target = tmp_path / "central"
    first = {"compose.yaml": "old\n", "kept.yml": "k\n"}
    assert sh_env(swap_script(target.as_posix()), tarball(first), env).returncode == 0
    before = {path.name: path.read_bytes() for path in target.iterdir()}
    (state / "fail").write_text("", encoding="utf-8")
    assert deploy(EXAMPLE, "central", directory=target.as_posix(), runner=local_runner(env)) == 1
    assert {path.name: path.read_bytes() for path in target.iterdir()} == before
    log = (state / "docker.log").read_text("utf-8").splitlines()
    assert log
    assert all(" pull " in line or line.endswith(" config --services") for line in log)
    err = capsys.readouterr().err.splitlines()
    assert err[-1] == "central: images not pulled, the running stack is untouched"


@needs_shell
def test_a_deploy_whose_pull_passes_swaps_then_starts_compose(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    env, state = compose_env(tmp_path)
    target = tmp_path / "central"
    compose = yaml.safe_load((RENDERED / "central" / "compose.yaml").read_text("utf-8"))
    rows = [{"Service": name, "State": "running"} for name in compose["services"]]
    (state / "ps.json").write_text(json.dumps(rows), encoding="utf-8")
    code = deploy(EXAMPLE, "central", directory=target.as_posix(), runner=local_runner(env))
    assert code == 0, capsys.readouterr().err
    log = (state / "docker.log").read_text("utf-8").splitlines()
    assert [line.split()[-1] for line in log] == ["--ignore-buildable", "--remove-orphans", "json"]
    assert (state / "seen.yaml").read_bytes() == (target / "compose.yaml").read_bytes()
    assert not (target / STAGING).exists()


def connected(**hosts: Any) -> Manifest:
    data = example_data()
    for host, connect in hosts.items():
        data["hosts"][host]["connect"] = connect
    return manifest_from(data)


def test_a_host_without_connect_is_reached_by_its_name_as_the_ssh_user() -> None:
    assert connection(connected(), "central") == Connection(
        "central", False, False, "/opt/ayeops/central"
    )


def test_connect_gives_a_host_its_alias_mode_and_directory() -> None:
    manifest = connected(
        central={"ssh": "ops-central", "sudo": True, "dir": "/opt/ayeops/hub"},
        edge={"helper": True},
    )
    assert connection(manifest, "central") == Connection(
        "ops-central", True, False, "/opt/ayeops/hub"
    )
    assert connection(manifest, "edge") == Connection("edge", False, True, "/opt/ayeops/edge")


def test_flags_win_over_connect_and_a_mode_flag_replaces_the_whole_mode() -> None:
    manifest = connected(central={"ssh": "ops-central", "sudo": True, "dir": "/srv/a"})
    assert connection(manifest, "central", "other", directory="/srv/b") == Connection(
        "other", True, False, "/srv/b"
    )
    assert connection(manifest, "central", helper=True) == Connection(
        "ops-central", False, True, "/srv/a"
    )
    helped = connected(edge={"helper": True})
    assert connection(helped, "edge", sudo=True) == Connection(
        "edge", True, False, "/opt/ayeops/edge"
    )


def test_a_connect_of_the_wrong_shape_waits_for_the_check_instead_of_crashing() -> None:
    manifest = connected(central="ops-central")
    assert connection(manifest, "central") == Connection(
        "central", False, False, "/opt/ayeops/central"
    )
    data = example_data()
    data["hosts"]["central"] = None
    assert connection(manifest_from(data), "central", "a") == Connection(
        "a", False, False, "/opt/ayeops/central"
    )


def test_connect_false_flags_and_an_empty_connect_mean_a_plain_ssh_login() -> None:
    manifest = connected(central={"sudo": False, "helper": False}, edge={})
    assert connection(manifest, "central") == Connection(
        "central", False, False, "/opt/ayeops/central"
    )
    assert connection(manifest, "edge") == Connection("edge", False, False, "/opt/ayeops/edge")


@pytest.mark.parametrize(
    ("connect", "rule"),
    [
        ({"ssh": "-oProxyCommand=x"}, "schema"),
        ({"ssh": "two words"}, "schema"),
        ({"ssh": ""}, "schema"),
        ({"dir": "relative/dir"}, "schema"),
        ({"sudo": True, "helper": True}, "schema"),
        ({"sudo": "yes"}, "schema"),
        ({"port": 22}, "schema"),
    ],
)
def test_the_schema_refuses_a_bad_connect(connect: dict[str, Any], rule: str) -> None:
    findings = check_manifest(connected(central=connect), load_modules())
    assert any(f.rule == rule and f.path.startswith("hosts.central") for f in findings)


def test_the_schema_takes_a_good_connect_on_a_host_and_on_a_profile() -> None:
    manifest = connected(
        central={"ssh": "aye@10.8.0.1", "helper": True, "dir": "/opt/ayeops/central"},
        edge={"ssh": "ops-edge", "sudo": True, "helper": False},
    )
    assert check_manifest(manifest, load_modules()) == []
    data = example_data()
    data["profiles"] = {"fleet": {"connect": {"sudo": True}}}
    data["hosts"]["edge"]["profile"] = "fleet"
    data["hosts"]["edge"]["connect"] = {"ssh": "ops-edge"}
    merged = manifest_from(data)
    assert check_manifest(merged, load_modules()) == []
    assert connection(merged, "edge") == Connection("ops-edge", True, False, "/opt/ayeops/edge")


def loaded(monkeypatch: pytest.MonkeyPatch, manifest: Manifest) -> None:
    monkeypatch.setattr(deploy_module, "load_manifest", lambda path: manifest)


def test_deploy_uses_the_connect_of_the_host(monkeypatch: pytest.MonkeyPatch) -> None:
    loaded(monkeypatch, connected(central={"ssh": "ops-central", "sudo": True}))
    runner = FakeSsh(Result(0), Result(0))
    assert deploy(EXAMPLE, "central", runner=runner) == 0
    assert {argv[1] for argv, _ in runner.calls} == {"ops-central"}
    assert all(argv[-1].startswith("sudo sh -c ") for argv, _ in runner.calls)


def test_deploy_with_connect_helper_runs_the_helper_and_checks_its_directory(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    loaded(monkeypatch, connected(central={"helper": True}))
    runner = FakeSsh(Result(0), Result(0))
    assert deploy(EXAMPLE, "central", runner=runner) == 0
    assert runner.calls[0][0][-1] == f"sudo -n {HELPER} /opt/ayeops/central rules"
    loaded(monkeypatch, connected(central={"helper": True, "dir": "/srv/central"}))
    refused = FakeSsh(Result(0), Result(0))
    assert deploy(EXAMPLE, "central", runner=refused) == 2
    assert refused.calls == []
    assert capsys.readouterr().err.strip() == (
        "--helper deploys only to /opt/ayeops/<name>, not /srv/central"
    )


def test_a_fleet_reaches_each_host_by_its_own_connect(monkeypatch: pytest.MonkeyPatch) -> None:
    manifest = connected(
        central={"ssh": "ops-central", "sudo": True}, edge={"ssh": "ops-edge", "helper": True}
    )
    loaded(monkeypatch, manifest)
    runner = FakeSsh(Result(0), Result(0))
    fleet(EXAMPLE, ["central", "edge"], runner=runner)
    aliases = [argv[1] for argv, _ in runner.calls]
    assert aliases[0] == "ops-central"
    assert aliases[-1] == "ops-edge"
    assert set(aliases) == {"ops-central", "ops-edge"}
    commands = {argv[1]: argv[-1] for argv, _ in runner.calls}
    assert commands["ops-edge"].startswith(f"sudo -n {HELPER} /opt/ayeops/edge ")


def test_a_fleet_refuses_a_connect_helper_outside_the_helper_root(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    loaded(monkeypatch, connected(edge={"helper": True, "dir": "/srv/edge"}))
    runner = FakeSsh(Result(0), Result(0))
    assert fleet(EXAMPLE, ["central", "edge"], runner=runner, dry_run=True) == 2
    assert runner.calls == []
    assert capsys.readouterr().err.strip() == (
        "--helper deploys only to /opt/ayeops/<name>, not /srv/edge"
    )


def test_a_fleet_takes_a_connect_dir_into_the_backup_timer_rule(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    loaded(monkeypatch, connected(edge={"dir": "/srv/edge"}))
    runner = FakeSsh(Result(0), Result(0))
    assert fleet(EXAMPLE, ["central", "edge"], runner=runner) == 2
    assert runner.calls == []
    assert "edge: its backup timers run the jobs from /opt/ayeops/edge" in capsys.readouterr().err


def namespace(**values: Any) -> argparse.Namespace:
    return argparse.Namespace(**{"all": False, "host": None, "manifest": EXAMPLE, **values})


def test_chosen_hosts_reads_all_the_named_hosts_or_the_only_host() -> None:
    assert chosen_hosts(namespace(all=True)) is None
    named = ["edge", "central"]
    assert chosen_hosts(namespace(host=named)) == named
    quickstart = EXAMPLE.parent / "quickstart.yaml"
    assert chosen_hosts(namespace(manifest=quickstart)) == ["local"]
    with pytest.raises(UsageError):
        chosen_hosts(namespace())


def test_the_cli_deploys_the_only_host_without_host(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[Any, ...]] = []

    def fake(*args: Any, **kwargs: Any) -> int:
        seen.append(args)
        return 0

    monkeypatch.setattr(deploy_module, "deploy", fake)
    quickstart = EXAMPLE.parent / "quickstart.yaml"
    assert main(["deploy", str(quickstart)]) == 0
    assert seen == [(quickstart, "local", None, False, None)]


def test_the_connection_flags_say_their_manifest_defaults() -> None:
    parser = argparse.ArgumentParser()
    add_connection(parser, "sudo text", "helper text")
    text = " ".join(parser.format_help().split())
    assert "default connect.ssh or the host name" in text
    assert "sudo text; default connect.sudo" in text
    assert "helper text; default connect.helper" in text
    assert "default connect.dir or /opt/ayeops/<host>" in text
    with pytest.raises(SystemExit):
        parser.parse_args(["--sudo", "--helper"])
