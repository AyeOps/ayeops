from __future__ import annotations

import copy
import io
import itertools
import shutil
import tarfile
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from ayeops import deploy as deploy_module
from ayeops.cli import main
from ayeops.deploy import (
    TIMED_OUT,
    TIMEOUT,
    Result,
    deploy,
    fleet,
    is_central,
    plan_text,
    rollout,
    summary,
)
from ayeops.registry import load_modules
from tests.conftest import EXAMPLE, WAIT, example_data, manifest_from

LEAVES = ("leaf1", "leaf2", "leaf3", "leaf4")


@pytest.fixture(scope="module")
def fleet_manifest(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("fleet")
    for name in ("apps", "brand", "dashboards"):
        shutil.copytree(EXAMPLE.parent / name, root / name)
    data = example_data()
    for leaf in LEAVES:
        data["hosts"][leaf] = copy.deepcopy(data["hosts"]["edge"])
    path = root / "fleet.yaml"
    path.write_bytes(yaml.safe_dump(data, sort_keys=False).encode("utf-8"))
    return path


Hook = Callable[[str, str], Result | None]


class FleetSsh:
    def __init__(self, hook: Hook | None = None) -> None:
        self.lock = threading.Lock()
        self.calls: list[tuple[str, str]] = []
        self.events: list[tuple[str, str]] = []
        self.services: dict[str, list[str]] = {}
        self.running = 0
        self.most = 0
        self.hook = hook

    def __call__(self, argv: list[str], stdin: bytes | None, timeout: float) -> Result:
        host, command = argv[1], argv[-1]
        step = "swap" if stdin is not None else "rules"
        if "--policy missing" in command:
            step = "pull"
        if "docker compose up" in command:
            step = "up"
        if "docker compose ps" in command:
            step = "ps"
        with self.lock:
            if not any(seen == host for seen, _ in self.calls):
                self.events.append(("start", host))
                self.running += 1
                self.most = max(self.most, self.running)
            self.calls.append((host, step))
        if step == "swap" and stdin is not None:
            with tarfile.open(fileobj=io.BytesIO(stdin), mode="r:gz") as archive:
                member = archive.extractfile("compose.yaml")
                assert member is not None
                compose = yaml.safe_load(member.read())
            self.services[host] = list(compose["services"])
        answer = self.hook(host, step) if self.hook else None
        if answer is None:
            answer = self.answer(host, step)
        if step == "ps" or answer.code != 0:
            with self.lock:
                self.events.append(("end", host))
                self.running -= 1
        return answer

    def answer(self, host: str, step: str) -> Result:
        if step == "pull":
            return Result(0, "staged\n")
        if step != "ps":
            return Result(0)
        rows = [
            f'{{"Service": "{name}", "State": "running", "Health": ""}}'
            for name in self.services[host]
        ]
        return Result(0, "\n".join(rows))

    def hosts(self) -> list[str]:
        return list(dict.fromkeys(host for host, _ in self.calls))


def results(out: str) -> list[list[str]]:
    lines = out.splitlines()
    start = lines.index(next(line for line in lines if line.split() == ["HOST", "RESULT"]))
    return [line.split(None, 1) for line in lines[start + 1 :]]


def test_central_hosts_are_the_ones_that_run_grafana_storage_or_a_server() -> None:
    manifest, modules = manifest_from(example_data()), load_modules()
    assert is_central(manifest, "central", modules)
    assert not is_central(manifest, "edge", modules)
    data = example_data()
    data["hosts"]["edge"]["modules"]["logs"] = {"role": "server", "retention": "7d"}
    assert is_central(manifest_from(data), "edge", modules)


def test_rollout_puts_the_canary_first_the_leaves_next_and_central_hosts_last() -> None:
    hosts = ["central", "a", "b", "hub", "c"]
    assert rollout(hosts, {"central", "hub"}, False, 2) == [
        ["a"],
        ["b", "c"],
        ["central", "hub"],
    ]
    assert rollout(hosts, {"central", "hub"}, True, 1) == [
        ["central"],
        ["a"],
        ["b"],
        ["c"],
        ["hub"],
    ]
    assert rollout(["x", "y"], {"x", "y"}, False, 5) == [["x"], ["y"]]
    assert rollout(["only"], set(), False, 3) == [["only"]]


def test_plan_and_summary_are_plain_aligned_text() -> None:
    assert plan_text([["a"], ["b", "c"], ["d"]]) == "canary: a\nwave 1: b, c\nwave 2: d"
    assert summary({"edge-01": "deployed", "c": "timed out"}).splitlines() == [
        "HOST     RESULT",
        "edge-01  deployed",
        "c        timed out",
    ]


def test_a_dry_run_prints_the_plan_and_connects_to_nothing(
    fleet_manifest: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runner = FleetSsh()
    assert fleet(fleet_manifest, None, runner=runner, parallel=3, dry_run=True) == 0
    assert capsys.readouterr().out.splitlines() == [
        "canary: edge",
        "wave 1: leaf1, leaf2, leaf3",
        "wave 2: leaf4, central",
    ]
    hosts = ["central", "leaf2", "central", "edge"]
    assert fleet(fleet_manifest, hosts, runner=runner, dry_run=True) == 0
    assert capsys.readouterr().out.splitlines() == [
        "canary: central",
        "wave 1: leaf2",
        "wave 2: edge",
    ]
    assert runner.calls == []


def test_the_cli_dry_run_of_the_example_puts_the_leaf_first(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["deploy", str(EXAMPLE), "--all", "--dry-run"]) == 0
    assert capsys.readouterr().out.splitlines() == ["canary: edge", "wave 1: central"]
    assert main(["deploy", str(EXAMPLE), "--host", "central", "--dry-run"]) == 0
    assert capsys.readouterr().out.splitlines() == ["canary: central"]


def test_a_failed_canary_skips_every_other_host(
    fleet_manifest: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def hook(host: str, step: str) -> Result | None:
        return Result(1, "", "no space left") if step == "up" else None

    runner = FleetSsh(hook)
    assert fleet(fleet_manifest, None, runner=runner, parallel=4) == 1
    assert runner.hosts() == ["edge"]
    captured = capsys.readouterr()
    assert captured.out.splitlines()[0] == "== edge (canary)"
    assert captured.err.splitlines() == ["no space left", "edge: docker compose up failed"]
    assert results(captured.out) == [
        ["edge", "failed"],
        *([leaf, "skipped"] for leaf in LEAVES),
        ["central", "skipped"],
    ]


def test_waves_run_n_hosts_at_once_and_wait_for_each_other(
    fleet_manifest: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    together = threading.Barrier(2, timeout=WAIT)

    def hook(host: str, step: str) -> Result | None:
        if host in LEAVES and step == "up":
            together.wait()
        return None

    runner = FleetSsh(hook)
    assert fleet(fleet_manifest, None, runner=runner, parallel=2) == 0
    assert runner.most == 2
    order = runner.events
    waves = [["edge"], ["leaf1", "leaf2"], ["leaf3", "leaf4"], ["central"]]
    for before, after in itertools.pairwise(waves):
        last_end = max(order.index(("end", host)) for host in before)
        first_start = min(order.index(("start", host)) for host in after)
        assert last_end < first_start
    out = capsys.readouterr().out
    assert results(out) == [[host, "deployed"] for wave in waves for host in wave]
    headers = [line for line in out.splitlines() if line.startswith("== ")]
    assert headers[0] == "== edge (canary)"
    assert headers[-1] == "== central (wave 3)"
    assert sorted(headers[1:3]) == ["== leaf1 (wave 1)", "== leaf2 (wave 1)"]


def test_a_failure_mid_wave_lets_the_running_host_finish_and_starts_no_other(
    fleet_manifest: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    started = threading.Event()
    failed = threading.Event()

    def hook(host: str, step: str) -> Result | None:
        if host == "leaf2" and step == "swap":
            started.set()
        if host == "leaf1" and step == "up":
            assert started.wait(WAIT)
            failed.set()
            return Result(1, "", "pull failed")
        if host == "leaf2" and step == "ps":
            assert failed.wait(WAIT)
        return None

    runner = FleetSsh(hook)
    assert fleet(fleet_manifest, None, runner=runner, parallel=2) == 1
    assert sorted(runner.hosts()) == ["edge", "leaf1", "leaf2"]
    assert ("leaf2", "ps") in runner.calls
    captured = capsys.readouterr()
    assert results(captured.out) == [
        ["edge", "deployed"],
        ["leaf1", "failed"],
        ["leaf2", "deployed"],
        ["leaf3", "skipped"],
        ["leaf4", "skipped"],
        ["central", "skipped"],
    ]
    assert captured.err.splitlines() == ["pull failed", "leaf1: docker compose up failed"]


def test_the_summary_follows_the_plan_not_the_finishing_order(
    fleet_manifest: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    leaf3_done = threading.Event()

    def hook(host: str, step: str) -> Result | None:
        if host == "leaf1" and step == "ps":
            assert leaf3_done.wait(WAIT)
        if host == "leaf3" and step == "up":
            leaf3_done.set()
            return Result(TIMED_OUT, "", f"ssh timed out after {TIMEOUT}s")
        if host == "leaf4" and step == "ps":
            raise OSError("connection reset")
        return None

    runner = FleetSsh(hook)
    hosts = ["leaf2", "central", "leaf1", "leaf3", "leaf4"]
    assert fleet(fleet_manifest, hosts, runner=runner, parallel=3) == 1
    assert runner.events.index(("end", "leaf3")) < runner.events.index(("end", "leaf1"))
    assert results(capsys.readouterr().out) == [
        ["leaf2", "deployed"],
        ["leaf1", "deployed"],
        ["leaf3", "timed out"],
        ["leaf4", "failed"],
        ["central", "skipped"],
    ]


def test_a_runner_error_fails_its_host_and_is_printed(
    fleet_manifest: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def hook(host: str, step: str) -> Result | None:
        if host == "leaf1" and step == "ps":
            raise OSError("connection reset")
        return None

    assert fleet(fleet_manifest, ["edge", "leaf1"], runner=FleetSsh(hook)) == 1
    captured = capsys.readouterr()
    assert "leaf1: OSError('connection reset')" in captured.err.splitlines()
    assert results(captured.out) == [["edge", "deployed"], ["leaf1", "failed"]]


def test_a_single_host_deploys_as_before_without_header_or_summary(
    capsys: pytest.CaptureFixture[str],
) -> None:
    runner = FleetSsh()
    assert deploy(EXAMPLE, "edge", runner=runner, time="2026-01-01 00:00 UTC", commit="x") == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "edge:/opt/ayeops/edge: 0 file(s) changed, 0 removed"
    assert lines[1].startswith("edge: 1 backup timer(s), ")
    assert lines[2].split() == ["SERVICE", "STATE", "HEALTH", "CONFIG", "CHANGED"]
    assert len(lines) == 3 + len(runner.services["edge"])
    assert not any(line.startswith("== ") or line.startswith("HOST") for line in lines)


def test_the_cli_sends_one_host_to_deploy_and_the_rest_to_the_fleet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def fake(name: str) -> Callable[..., int]:
        def call(*args: Any, **kwargs: Any) -> int:
            seen.append((name, args, kwargs))
            return 3

        return call

    monkeypatch.setattr(deploy_module, "deploy", fake("deploy"))
    monkeypatch.setattr(deploy_module, "fleet", fake("fleet"))
    assert main(["deploy", "m.yaml", "--host", "h"]) == 3
    assert seen[-1][0] == "deploy"
    assert main(["deploy", "m.yaml", "--host", "a", "--host", "b", "--parallel", "4"]) == 3
    name, args, kwargs = seen[-1]
    assert (name, args[1], kwargs["parallel"], kwargs["dry_run"]) == ("fleet", ["a", "b"], 4, False)
    assert main(["deploy", "m.yaml", "--all", "--helper", "--dir", "/opt/ayeops/x"]) == 3
    name, args, kwargs = seen[-1]
    assert (name, args[1:5], kwargs["helper"], kwargs["parallel"]) == (
        "fleet",
        (None, None, False, "/opt/ayeops/x"),
        True,
        1,
    )
    assert main(["deploy", "m.yaml", "--host", "h", "--dry-run"]) == 3
    assert seen[-1][0] == "fleet"
    assert seen[-1][2]["dry_run"] is True


@pytest.mark.parametrize(
    ("argv", "reason"),
    [
        (["--all", "--host", "h"], "not allowed with argument"),
        (["--all", "--parallel", "0"], "'0' is not a positive whole number"),
        (["--all", "--parallel", "two"], "'two' is not a positive whole number"),
    ],
)
def test_the_cli_refuses_all_with_host_and_a_parallel_below_one(
    argv: list[str], reason: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit):
        main(["deploy", "m.yaml", *argv])
    assert reason in capsys.readouterr().err


def test_a_fleet_refuses_unknown_hosts_one_ssh_alias_and_helper_dirs_before_any_call(
    capsys: pytest.CaptureFixture[str],
) -> None:
    runner = FleetSsh()
    assert fleet(EXAMPLE, ["edge", "nowhere", "gone"], runner=runner) == 2
    assert capsys.readouterr().err.splitlines() == [
        f"{EXAMPLE}: unknown host 'nowhere'",
        f"{EXAMPLE}: unknown host 'gone'",
    ]
    assert fleet(EXAMPLE, None, ssh="ops", runner=runner) == 2
    assert "not of a fleet" in capsys.readouterr().err
    assert fleet(EXAMPLE, None, directory="/srv/x", helper=True, runner=runner) == 2
    assert capsys.readouterr().err.strip() == (
        "--helper deploys only to /opt/ayeops/<name>, not /srv/x"
    )
    assert fleet(EXAMPLE, None, sudo=True, helper=True, runner=runner) == 2
    assert "exclude each other" in capsys.readouterr().err
    assert runner.calls == []


def test_a_fleet_of_one_reaches_it_by_its_ssh_alias(capsys: pytest.CaptureFixture[str]) -> None:
    runner = FleetSsh()
    assert fleet(EXAMPLE, ["edge"], ssh="ops-edge", runner=runner) == 0
    assert runner.hosts() == ["ops-edge"]
    assert results(capsys.readouterr().out) == [["edge", "deployed"]]


def test_every_host_of_a_wave_pulls_before_any_host_of_it_swaps(
    fleet_manifest: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    leaf2_pulled = threading.Event()
    lock = threading.Lock()
    done: list[tuple[str, str]] = []

    def hook(host: str, step: str) -> Result | None:
        if host == "leaf1" and step == "pull":
            assert leaf2_pulled.wait(WAIT)
            time.sleep(0.3)
        if host == "leaf2" and step == "pull":
            leaf2_pulled.set()
        with lock:
            done.append((host, step))
        return None

    runner = FleetSsh(hook)
    assert fleet(fleet_manifest, None, runner=runner, parallel=2) == 0, capsys.readouterr().err
    for wave in (["leaf1", "leaf2"], ["leaf3", "leaf4"]):
        last_pull = max(done.index((host, "pull")) for host in wave)
        first_swap = min(done.index((host, "swap")) for host in wave)
        assert last_pull < first_swap
    for host in ("edge", *LEAVES, "central"):
        steps = [step for seen, step in runner.calls if seen == host]
        assert steps.index("pull") == steps.index("swap") - 1


def test_a_failed_pull_in_a_wave_swaps_no_host_of_that_wave(
    fleet_manifest: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def hook(host: str, step: str) -> Result | None:
        if host == "leaf2" and step == "pull":
            return Result(1, "staged\nfailed node-exporter\n", "toomanyrequests: rate limit")
        return None

    runner = FleetSsh(hook)
    assert fleet(fleet_manifest, None, runner=runner, parallel=2) == 1
    assert ("leaf1", "pull") in runner.calls
    assert ("leaf1", "swap") not in runner.calls
    assert ("leaf2", "swap") not in runner.calls
    assert sorted(runner.hosts()) == ["edge", "leaf1", "leaf2"]
    captured = capsys.readouterr()
    assert results(captured.out) == [
        ["edge", "deployed"],
        ["leaf1", "skipped"],
        ["leaf2", "failed"],
        ["leaf3", "skipped"],
        ["leaf4", "skipped"],
        ["central", "skipped"],
    ]
    err = captured.err.splitlines()
    assert "leaf1: pulled, not swapped: another host of wave 1 failed before its swap" in err
    assert "toomanyrequests: rate limit" in err
    assert any(line.startswith("leaf2: image ") for line in err)
    assert "leaf2: images not pulled, the running stack is untouched" in err
    headers = [line for line in captured.out.splitlines() if line.startswith("== ")]
    assert sorted(headers[1:]) == ["== leaf1 (wave 1)", "== leaf2 (wave 1)"]


def test_a_canary_whose_pull_times_out_stops_the_rollout_untouched(
    fleet_manifest: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def hook(host: str, step: str) -> Result | None:
        if step == "pull":
            return Result(TIMED_OUT, "", f"ssh timed out after {TIMEOUT}s")
        return None

    runner = FleetSsh(hook)
    assert fleet(fleet_manifest, None, runner=runner, parallel=4) == 1
    assert runner.calls == [("edge", "pull")]
    captured = capsys.readouterr()
    assert results(captured.out)[0] == ["edge", "timed out"]
    assert captured.err.splitlines()[-1] == "edge: pull timed out, the running stack is untouched"
