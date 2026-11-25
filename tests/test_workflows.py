from __future__ import annotations

import json
import re
import shlex
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).parents[1]
WORKFLOWS = sorted((ROOT / ".github" / "workflows").glob("*.yml"))
ACTIONS = sorted((ROOT / "ci").glob("*/action.yml"))
LIMIT = 30


def load(path: Path) -> dict[Any, Any]:
    data: dict[Any, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data


def jobs() -> list[tuple[str, str, dict[str, Any]]]:
    found = []
    for path in WORKFLOWS:
        for name, job in load(path)["jobs"].items():
            found.append((path.name, name, job))
    return found


def triggers(workflow: str) -> dict[str, Any]:
    on = load(ROOT / ".github" / "workflows" / workflow)[True]
    if isinstance(on, str):
        return {on: None}
    if isinstance(on, list):
        return dict.fromkeys(on)
    found: dict[str, Any] = on
    return found


def test_there_are_workflows_to_check() -> None:
    assert jobs()


@pytest.mark.parametrize(("workflow", "name", "job"), jobs(), ids=lambda v: str(v)[:30])
def test_every_hosted_job_has_a_short_timeout(
    workflow: str, name: str, job: dict[str, Any]
) -> None:
    minutes = job.get("timeout-minutes")
    assert isinstance(minutes, int), f"{workflow}:{name} runs up to 360 minutes when a test hangs"
    assert 0 < minutes <= LIMIT, f"{workflow}:{name} may burn {minutes} minutes"


TOKEN = re.compile(
    r"\s*(?:(?P<string>'(?:[^']|'')*')|(?P<op>==|!=|&&|\|\||[!(),])"
    r"|(?P<number>-?\d+(?:\.\d+)?)|(?P<name>[A-Za-z_][\w.-]*))"
)


class Expression:
    def __init__(self, text: str, context: Mapping[str, Any]):
        self.tokens: list[tuple[str, str]] = []
        position = 0
        text = text.strip()
        while position < len(text):
            match = TOKEN.match(text, position)
            assert match and match.end() > position, f"cannot read {text[position:]!r}"
            kind = match.lastgroup
            assert kind is not None
            self.tokens.append((kind, match.group(kind)))
            position = match.end()
        self.context = context
        self.at = 0

    def peek(self) -> str | None:
        return self.tokens[self.at][1] if self.at < len(self.tokens) else None

    def take(self, expected: str | None = None) -> tuple[str, str]:
        token = self.tokens[self.at]
        assert expected is None or token[1] == expected, f"expected {expected}, got {token}"
        self.at += 1
        return token

    def value(self) -> Any:
        result = self.either()
        assert self.at == len(self.tokens), f"left over: {self.tokens[self.at :]}"
        return result

    def either(self) -> Any:
        left = self.both()
        while self.peek() == "||":
            self.take()
            right = self.both()
            left = left if truthy(left) else right
        return left

    def both(self) -> Any:
        left = self.compare()
        while self.peek() == "&&":
            self.take()
            right = self.compare()
            left = right if truthy(left) else left
        return left

    def compare(self) -> Any:
        left = self.unary()
        while self.peek() in ("==", "!="):
            operator = self.take()[1]
            same = equal(left, self.unary())
            left = same if operator == "==" else not same
        return left

    def unary(self) -> Any:
        if self.peek() == "!":
            self.take()
            return not truthy(self.unary())
        return self.primary()

    def primary(self) -> Any:
        kind, text = self.take()
        if text == "(" and kind == "op":
            inner = self.either()
            self.take(")")
            return inner
        if kind == "string":
            return text[1:-1].replace("''", "'")
        if kind == "number":
            return float(text)
        assert kind == "name", f"unexpected {text}"
        if text in ("true", "false", "null"):
            return {"true": True, "false": False, "null": None}[text]
        if self.peek() == "(":
            self.take("(")
            args = [self.either()]
            while self.peek() == ",":
                self.take()
                args.append(self.either())
            self.take(")")
            return call(text, args)
        return lookup(self.context, text)


def truthy(value: Any) -> bool:
    return value not in (None, False, "", 0) and value == value


def equal(left: Any, right: Any) -> bool:
    if isinstance(left, str) and isinstance(right, str):
        return left.casefold() == right.casefold()
    return bool(left == right)


def call(name: str, args: list[Any]) -> Any:
    if name.casefold() == "fromjson":
        return json.loads(args[0])
    if name.casefold() == "contains":
        haystack, needle = args
        if isinstance(haystack, list):
            return any(equal(item, needle) for item in haystack)
        return str(needle).casefold() in str(haystack).casefold()
    raise AssertionError(f"the test does not know the function {name}")


def lookup(context: Mapping[str, Any], path: str) -> Any:
    value: Any = context
    for part in path.split("."):
        if callable(value):
            value = value(part)
        elif isinstance(value, Mapping):
            value = value.get(part)
        else:
            return None
    return value


def labels(runs_on: Any, context: Mapping[str, Any]) -> list[str]:
    if isinstance(runs_on, str):
        match = re.fullmatch(r"\$\{\{(.*)\}\}", runs_on.strip(), re.S)
        runs_on = Expression(match.group(1), context).value() if match else runs_on
    return [runs_on] if isinstance(runs_on, str) else list(runs_on)


SELF_HOSTED = '["self-hosted","ayeops"]'
HOSTED = ["ubuntu-latest"]
TRUSTED = ("push", "schedule", "workflow_dispatch")
SHARED = (
    '${{ fromJSON(contains(fromJSON(\'["push","schedule","workflow_dispatch"]\'),'
    " github.event_name) && vars.AYE_RUNS_ON || '\"ubuntu-latest\"') }}"
)
PICKED = {
    ("e2e.yml", "e2e"): (
        "${{ fromJSON(github.event_name == 'workflow_dispatch' && inputs.runner == 'self-hosted'"
        " && vars.AYE_RUNS_ON || '\"ubuntu-latest\"') }}"
    ),
    ("release.yml", "release"): "ubuntu-latest",
}


def context(
    event: str, variables: str = SELF_HOSTED, runner: str = "self-hosted"
) -> dict[str, Any]:
    return {
        "github": {"event_name": event},
        "vars": lambda name: variables,
        "inputs": lambda name: runner,
    }


def triggered_jobs() -> list[tuple[str, str, str, Any]]:
    return [
        (workflow, name, event, job["runs-on"])
        for workflow, name, job in jobs()
        for event in triggers(workflow)
    ]


@pytest.mark.parametrize(("workflow", "name", "job"), jobs(), ids=lambda v: str(v)[:30])
def test_every_job_picks_its_runner_with_one_expression_shape(
    workflow: str, name: str, job: dict[str, Any]
) -> None:
    assert job["runs-on"] == PICKED.get((workflow, name), SHARED), f"{workflow}:{name}"


def test_the_expression_reader_follows_github_semantics() -> None:
    def run(text: str, **values: Any) -> Any:
        return Expression(text, values).value()

    assert run("'a' && 'b' || 'c'") == "b"
    assert run("'' && 'b' || 'c'") == "c"
    assert run("false || ''") == ""
    assert run("x.y || 'z'", x={"y": ""}) == "z"
    assert run("'Push' == 'push'") is True
    assert run("contains(fromJSON('[\"push\"]'), 'PUSH')") is True
    assert run("contains(fromJSON('[\"push\"]'), 'pull_request')") is False
    assert run("!(missing.name)") is True
    assert run("'it''s'") == "it's"


@pytest.mark.parametrize(
    ("workflow", "name", "event", "runs_on"), triggered_jobs(), ids=lambda v: str(v)[:30]
)
def test_an_untrusted_event_never_reaches_a_self_hosted_runner(
    workflow: str, name: str, event: str, runs_on: Any
) -> None:
    if event in TRUSTED:
        return
    picked = labels(runs_on, context(event))
    assert picked == HOSTED, f"{workflow}:{name} on {event} runs on {picked}"


def test_pull_requests_trigger_hosted_jobs_and_nothing_trusts_their_code() -> None:
    events = {event for workflow, _, event, _ in triggered_jobs()}
    assert "pull_request" in events
    assert events <= {*TRUSTED, "pull_request"}
    for path in WORKFLOWS:
        assert "pull_request_target" not in path.read_text(encoding="utf-8"), path.name


@pytest.mark.parametrize("workflow", [path.name for path in WORKFLOWS])
def test_a_push_runs_a_workflow_only_for_main_or_a_version_tag(workflow: str) -> None:
    push = triggers(workflow).get("push", {"branches": ["main"]})
    refs = {key: value for key, value in push.items() if key != "paths"}
    assert refs in ({"branches": ["main"]}, {"tags": ["v*"]}), workflow


def test_trusted_events_follow_the_runner_variable_and_fall_back_to_github() -> None:
    ci = load(ROOT / ".github" / "workflows" / "ci.yml")["jobs"]["test"]["runs-on"]
    for event in TRUSTED:
        assert labels(ci, context(event)) == json.loads(SELF_HOSTED)
        assert labels(ci, context(event, variables="")) == HOSTED
    assert labels(ci, context("pull_request")) == HOSTED


def test_e2e_leaves_github_only_when_the_owner_asks_for_the_self_hosted_runner() -> None:
    e2e = e2e_workflow()["jobs"]["e2e"]["runs-on"]
    assert labels(e2e, context("workflow_dispatch")) == json.loads(SELF_HOSTED)
    assert labels(e2e, context("workflow_dispatch", runner="hosted")) == HOSTED
    assert labels(e2e, context("workflow_dispatch", variables="")) == HOSTED
    assert labels(e2e, context("schedule")) == HOSTED
    assert labels(e2e, context("pull_request")) == HOSTED


def checkouts() -> list[tuple[str, dict[str, Any]]]:
    found = []
    for workflow, name, job in jobs():
        for step in job.get("steps", []):
            found.append((f"{workflow}:{name}", step))
    for path in ACTIONS:
        for step in load(path)["runs"].get("steps", []):
            found.append((f"{path.parent.name}/{path.name}", step))
    return [
        (where, step)
        for where, step in found
        if str(step.get("uses", "")).startswith("actions/checkout@")
    ]


PUSHES_WITH_GIT: set[str] = set()


def test_every_checkout_drops_the_token_unless_the_job_pushes() -> None:
    assert checkouts()
    for where, step in checkouts():
        keep = (step.get("with") or {}).get("persist-credentials", True)
        assert keep is False or where in PUSHES_WITH_GIT, where


def test_ci_audits_the_workflows_with_a_pinned_zizmor_that_fails_on_high_findings() -> None:
    job = load(ROOT / ".github" / "workflows" / "ci.yml")["jobs"]["zizmor"]
    (audit,) = [step["run"] for step in job["steps"] if "zizmor" in step.get("run", "")]
    assert re.search(r"\buvx zizmor==\d+\.\d+\.\d+ ", audit), audit
    assert "--min-severity high" in audit
    assert audit.rstrip().endswith(" ."), audit


RELEASE = ROOT / ".github" / "workflows" / "release.yml"
ATTEST = re.compile(
    r"^\s*uses: actions/attest-build-provenance@[0-9a-f]{40} # v\d+\.\d+\.\d+$", re.M
)


def release() -> dict[Any, Any]:
    return load(RELEASE)


def release_steps() -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = release()["jobs"]["release"]["steps"]
    return steps


def release_runs() -> list[str]:
    return [str(step.get("run", "")) for step in release_steps()]


def test_a_release_runs_on_a_pushed_version_tag_only() -> None:
    assert release()[True] == {"push": {"tags": ["v*"]}}


def test_a_release_runs_on_github_and_only_it_may_write_and_sign() -> None:
    workflow = release()
    assert workflow["permissions"] == {"contents": "read"}
    job = workflow["jobs"]["release"]
    assert job["runs-on"] == "ubuntu-latest"
    assert job["permissions"] == {
        "actions": "read",
        "contents": "write",
        "id-token": "write",
        "attestations": "write",
    }
    for workflow_name, name, other in jobs():
        if (workflow_name, name) != ("release.yml", "release"):
            granted = other.get("permissions") or {}
            assert "id-token" not in granted and "attestations" not in granted, name


def test_a_release_needs_a_full_mutation_run_that_passed_on_the_tagged_commit() -> None:
    (run,) = [run for run in release_runs() if "mutants.yml/runs" in run]
    query = run.split("mutants.yml/runs?", 1)[1].split('"', 1)[0]
    expected = ["event=workflow_dispatch", "head_sha=$sha", "status=success"]
    assert sorted(query.split("&")) == expected
    assert 'sha="$(git rev-parse HEAD)"' in run
    assert '[ "$passed" = "0" ]' in run and "exit 1" in run


def test_a_release_checks_the_tag_and_main_before_it_builds_and_publishes() -> None:
    runs = release_runs()
    needles = ('"v$version"', "origin/main", "mutants.yml/runs", "uv build", "gh release create")
    order = [next(i for i, run in enumerate(runs) if needle in run) for needle in needles]
    assert order == sorted(order)
    publish = runs[order[-1]]
    assert all(flag in publish for flag in ("--verify-tag", "--generate-notes", "dist/*"))


def test_a_release_attests_the_provenance_of_what_it_publishes() -> None:
    steps = release_steps()
    uses = [str(step.get("uses", "")) for step in steps]
    (attest,) = [i for i, use in enumerate(uses) if "attest-build-provenance" in use]
    build = next(i for i, run in enumerate(release_runs()) if "uv build" in run)
    publish = next(i for i, run in enumerate(release_runs()) if "gh release create" in run)
    assert build < attest < publish
    assert steps[attest]["with"] == {"subject-path": "dist/*"}
    assert ATTEST.search(RELEASE.read_text(encoding="utf-8"))


def test_a_release_restores_no_cache() -> None:
    for step in release_steps():
        if "setup-uv" in str(step.get("uses", "")):
            assert step["with"]["enable-cache"] is False


def test_a_release_publishes_nowhere_but_the_github_release() -> None:
    text = RELEASE.read_text(encoding="utf-8").lower()
    assert all(word not in text for word in ("pypi", "twine", "uv publish"))


def e2e_workflow() -> dict[Any, Any]:
    return load(ROOT / ".github" / "workflows" / "e2e.yml")


def test_the_owner_can_send_one_e2e_run_to_the_self_hosted_runner() -> None:
    workflow = e2e_workflow()
    runner = workflow[True]["workflow_dispatch"]["inputs"]["runner"]
    assert runner["type"] == "choice"
    assert runner["default"] == "hosted"
    assert runner["options"] == ["hosted", "self-hosted"]
    assert list(workflow["jobs"]) == ["e2e"]
    assert workflow["permissions"] == {"contents": "read"}


BOX = ROOT / "tests" / "e2e" / "box.sh"
BOX_STEP = "sh tests/e2e/box.sh "
HOSTED_ONLY = "runner.environment == 'github-hosted'"
HOST_CHANGES = ("sudo", "iptables", "ip link", "ip addr", "sysctl", "modprobe")
STACK_WORK = ("docker compose", "pytest", "tests.e2e.stack", "curl", "wget")
SHARED_WITH_HOST = (
    "-p",
    "--publish",
    "-P",
    "--publish-all",
    "--network",
    "--net",
    "-v",
    "--volume",
    "--mount",
    "--volumes-from",
    "--ipc",
    "--pid",
    "--uts",
    "--userns",
    "--cgroupns",
    "--device",
)
PINNED = r"[a-z0-9.-]+(?:/[a-z0-9._-]+)+:\d+\.\d+\.\d+(?:-dind)?@sha256:[a-f0-9]{64}"


def e2e_steps() -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = e2e_workflow()["jobs"]["e2e"]["steps"]
    return steps


def box_lines() -> list[str]:
    joined = BOX.read_text(encoding="utf-8").replace("\\\n", " ")
    return [line.strip() for line in joined.splitlines()]


def box_run() -> list[str]:
    (line,) = [line for line in box_lines() if line.startswith("docker run ")]
    return shlex.split(line)


def box_setting(name: str) -> str:
    (line,) = [line for line in box_lines() if line.startswith(f"{name}=")]
    return line.split("=", 1)[1].strip('"')


def test_every_e2e_step_after_the_github_cleanup_goes_through_the_box() -> None:
    for step in e2e_steps():
        if "run" not in step or step.get("if") == HOSTED_ONLY:
            continue
        command = step["run"].strip()
        assert command.startswith(BOX_STEP), step["name"]
        assert "\n" not in command, step["name"]
    names = [step.get("name") for step in e2e_steps()]
    assert names.index("box") < names.index("render") < names.index("up") < names.index("smoke")
    assert "down" not in names
    assert not any("setup-uv" in step.get("uses", "") for step in e2e_steps())


def test_the_stack_is_run_and_tested_only_inside_the_box() -> None:
    for line in box_lines():
        if any(word in line for word in STACK_WORK):
            assert line.startswith("docker exec "), line
    for step in e2e_steps():
        command = step.get("run", "")
        if command.startswith(BOX_STEP + "exec "):
            assert any(word in command for word in STACK_WORK), step["name"]


def test_only_the_github_machine_is_changed_with_sudo() -> None:
    (room,) = [step for step in e2e_steps() if step.get("name") == "room for images"]
    assert room["if"] == HOSTED_ONLY
    assert "sudo" in room["run"]
    for line in box_lines():
        assert not any(word in line for word in HOST_CHANGES), line
    for step in e2e_steps():
        if step.get("if") != HOSTED_ONLY:
            assert not any(word in step.get("run", "") for word in HOST_CHANGES), step


def test_the_box_publishes_nothing_and_shares_nothing_with_the_host() -> None:
    args = box_run()
    for arg in args:
        assert arg.split("=", 1)[0] not in SHARED_WITH_HOST, arg
    assert {"--privileged", "--rm", "--detach"} <= set(args)
    assert [arg for arg in args if arg.startswith("--host=")] == [
        "--host=unix:///var/run/docker.sock"
    ]
    assert not any("tcp://" in line for line in box_lines())


def test_the_box_has_a_memory_cap_without_swap_and_a_lifetime_past_the_job() -> None:
    args = box_run()
    assert box_setting("memory") == "8g"
    assert args[args.index("--memory") + 1] == "$memory"
    assert args[args.index("--memory-swap") + 1] == "$memory"
    assert args[args.index("--entrypoint") + 1] == "timeout"
    assert args[args.index("$image") + 1] == "$lifetime"
    assert int(box_setting("lifetime")) > 60 * e2e_workflow()["jobs"]["e2e"]["timeout-minutes"]


def test_the_box_images_are_pinned_by_version_and_digest() -> None:
    image = box_setting("image")
    assert re.fullmatch(PINNED, image), image
    assert image.startswith("docker.io/library/docker:")
    assert "-dind@" in image
    uv_image = box_setting("uv_image")
    assert re.fullmatch(PINNED, uv_image), uv_image
    assert uv_image.startswith("ghcr.io/astral-sh/uv:")


def test_renovate_bumps_the_images_of_the_box() -> None:
    config = json.loads((ROOT / "renovate.json").read_text(encoding="utf-8"))
    for name in ("image", "uv_image"):
        image = box_setting(name)
        found = False
        for manager in config["customManagers"]:
            patterns = [p.strip("/") for p in manager["managerFilePatterns"]]
            if not any(re.search(p, "tests/e2e/box.sh") for p in patterns):
                continue
            for match in manager["matchStrings"]:
                python_match = re.sub(r"\(\?<(?=[A-Za-z])", "(?P<", match)
                hit = re.search(python_match, f" {image}")
                found = found or bool(hit and hit.group("currentDigest"))
        assert found, image


def test_each_run_and_attempt_has_its_own_box_and_always_removes_it() -> None:
    job = e2e_workflow()["jobs"]["e2e"]
    name = job["env"]["AYE_E2E_BOX"]
    assert "${{ github.run_id }}" in name and "${{ github.run_attempt }}" in name
    assert box_setting("box") == "${AYE_E2E_BOX:-aye-e2e}"
    assert "--name" in box_run() and box_run()[box_run().index("--name") + 1] == "$box"
    last = job["steps"][-1]
    assert last["if"] == "always()"
    assert last["run"] == BOX_STEP + "stop"
    assert 'docker rm --force --volumes "$box" > /dev/null' in box_lines()


def test_logs_are_copied_out_of_the_box_before_it_is_removed() -> None:
    names = [step.get("name") or step.get("uses", "") for step in e2e_steps()]
    upload = next(i for i, name in enumerate(names) if "upload-artifact" in name)
    assert names.index("collect logs") < upload < names.index("remove the box")
    assert any(line.startswith('docker cp "$box:') for line in box_lines())
