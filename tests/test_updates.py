from __future__ import annotations

import importlib.util
import json
import re
import sys
import time
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

from ayeops.registry import Module
from ayeops.render import host_compose
from tests.conftest import RENDERED, ROOT, manifest_from, render_once, rules_hit

MODULE = ROOT / "src" / "ayeops" / "modules" / "updates"
AGENT = MODULE / "files" / "updates_agent.py"
LOOP = MODULE / "files" / "updates_loop.sh"
RENOVATE = ROOT / "renovate.json"
DAY = 86400


def load_agent() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "ayeops.modules.updates.files.updates_agent", AGENT
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


agent = load_agent()


def iso(seconds: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(seconds))


def dep(
    datasource: str, *released: float, update_type: str = "minor", **extra: Any
) -> dict[str, Any]:
    return {
        "depName": f"dep-{datasource}-{len(released)}",
        "datasource": datasource,
        "updates": [{"updateType": update_type, "releaseTimestamp": iso(r)} for r in released],
        **extra,
    }


def repo(
    branches: list[dict[str, Any]], deps: list[dict[str, Any]], problems: list[Any] | None = None
) -> dict[str, Any]:
    return {
        "problems": problems or [],
        "branches": branches,
        "packageFiles": {"docker-compose": [{"packageFile": "compose.yaml", "deps": deps}]},
    }


def test_open_prs_skip_closed_and_automerged_branches_and_branches_without_a_pr() -> None:
    branches = [
        {"prNo": 1, "result": "done"},
        {"prNo": 2, "result": "pr-created"},
        {"prNo": 3, "result": "already-existed"},
        {"prNo": 4, "result": "automerged"},
        {"prNo": None, "result": "pending"},
        {"result": "not-scheduled"},
    ]
    assert agent.open_prs(branches) == 2


def test_repo_stats_count_pending_per_datasource_and_the_oldest_release() -> None:
    stats = agent.repo_stats(
        repo(
            [{"prNo": 7, "result": "done"}],
            [
                dep("docker", 1_000_000.0, 3_000_000.0),
                dep("docker", 2_000_000.0),
                dep("pypi", 5_000_000.0),
                {"depName": "fresh", "datasource": "pypi", "updates": []},
                dep("docker", 10.0, skipReason="unversioned-reference"),
            ],
        )
    )
    assert stats.present
    assert stats.open_prs == 1
    assert stats.dependencies == 4
    assert dict(stats.pending) == {"docker": 2, "pypi": 1}
    assert stats.oldest_pending == 1_000_000.0


def test_pins_and_lock_maintenance_are_not_pending_updates() -> None:
    stats = agent.repo_stats(
        repo(
            [],
            [
                dep("docker", 100.0, update_type="pinDigest"),
                dep("github-tags", 100.0, update_type="pin"),
                dep("pypi", 100.0, update_type="lockFileMaintenance"),
            ],
        )
    )
    assert dict(stats.pending) == {}
    assert stats.oldest_pending is None
    assert stats.dependencies == 3


def test_an_update_without_a_release_date_counts_but_does_not_age() -> None:
    stats = agent.repo_stats(
        repo([], [{"datasource": "docker", "updates": [{"updateType": "major"}]}])
    )
    assert dict(stats.pending) == {"docker": 1}
    assert stats.oldest_pending is None


def test_problems_split_into_errors_and_warnings() -> None:
    stats = agent.repo_stats(
        repo([], [], [{"level": 60}, {"level": 50}, {"level": 40}, {"level": 30}, "x"])
    )
    assert (stats.errors, stats.warnings) == (2, 1)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2026-09-01T12:00:00.000Z", 1788264000.0),
        ("2026-09-01T12:00:00+00:00", 1788264000.0),
        ("2026-09-01T12:00:00", None),
        ("yesterday", None),
        ("", None),
        (None, None),
        (17, None),
    ],
)
def test_timestamp_reads_only_zoned_iso_dates(text: Any, expected: float | None) -> None:
    assert agent.timestamp(text) == expected


def test_a_repository_missing_from_the_report_is_not_present() -> None:
    stats = agent.report_stats({"repositories": {"a/b": repo([], [])}}, ["a/b", "c/d"])
    assert stats["a/b"].present
    assert not stats["c/d"].present


def test_a_run_succeeds_only_with_exit_0_and_every_repository_clean() -> None:
    clean = agent.RepoStats(present=True)
    run = agent.Run(0.0, 10.0, 0)
    assert agent.run_succeeded(run, {"a/b": clean})
    assert not agent.run_succeeded(agent.Run(0.0, 10.0, 1), {"a/b": clean})
    assert not agent.run_succeeded(run, {"a/b": clean, "c/d": agent.RepoStats()})
    assert not agent.run_succeeded(run, {"a/b": agent.RepoStats(present=True, errors=1)})


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("100 160 0\n", (100.0, 160.0, 0)),
        ("100 160 124", (100.0, 160.0, 124)),
        ("100 160", None),
        ("a b c", None),
        ("", None),
    ],
)
def test_parse_run_reads_start_end_and_exit_code(
    text: str, expected: tuple[float, float, int] | None
) -> None:
    run = agent.parse_run(text)
    assert (None if run is None else (run.started, run.finished, run.exit_code)) == expected


def test_render_metrics_name_every_repository_and_datasource() -> None:
    stats = agent.report_stats(
        {
            "repositories": {
                "a/b": repo(
                    [{"prNo": 3, "result": "done"}], [dep("docker", 1000.0), dep("pypi", 2000.0)]
                )
            }
        },
        ["a/b", "c/d"],
    )
    text = agent.render_metrics(agent.Run(100.0, 160.4, 0), stats, 50.0)
    assert text == (
        "ayeops_updates_last_run_timestamp_seconds 160\n"
        "ayeops_updates_last_run_success 0\n"
        "ayeops_updates_last_run_exit_code 0\n"
        "ayeops_updates_last_success_timestamp_seconds 50\n"
        "ayeops_updates_run_duration_seconds 60\n"
        'ayeops_updates_repository_ok{repository="a/b"} 1\n'
        'ayeops_updates_open_prs{repository="a/b"} 1\n'
        'ayeops_updates_dependencies{repository="a/b"} 2\n'
        'ayeops_updates_problems{level="error",repository="a/b"} 0\n'
        'ayeops_updates_problems{level="warn",repository="a/b"} 0\n'
        'ayeops_updates_pending_updates{datasource="docker",repository="a/b"} 1\n'
        'ayeops_updates_pending_updates{datasource="pypi",repository="a/b"} 1\n'
        'ayeops_updates_oldest_pending_release_timestamp_seconds{repository="a/b"} 1000\n'
        'ayeops_updates_repository_ok{repository="c/d"} 0\n'
        'ayeops_updates_open_prs{repository="c/d"} 0\n'
        'ayeops_updates_dependencies{repository="c/d"} 0\n'
        'ayeops_updates_problems{level="error",repository="c/d"} 0\n'
        'ayeops_updates_problems{level="warn",repository="c/d"} 0\n'
    )


def settings(tmp_path: Path, repositories: tuple[str, ...] = ("a/b",)) -> Any:
    return agent.Settings(
        schedule=(6, 0),
        repositories=repositories,
        state=tmp_path / "state",
        textfile=tmp_path / "ayeops_updates.prom",
    )


def write_report(target: Any, report: dict[str, Any], run: str) -> None:
    target.state.mkdir(parents=True, exist_ok=True)
    target.report.write_text(json.dumps(report), encoding="utf-8")
    target.last.write_text(run, encoding="utf-8")


def test_record_moves_the_last_success_on_a_clean_run(tmp_path: Path) -> None:
    target = settings(tmp_path)
    write_report(target, {"repositories": {"a/b": repo([], [])}}, "100 160 0\n")
    line = agent.record(target, agent.parse_run("100 160 0"))
    assert line["success"] is True
    assert "ayeops_updates_last_success_timestamp_seconds 160\n" in target.textfile.read_text(
        encoding="utf-8"
    )


def test_record_keeps_the_last_success_when_a_repository_is_missing(tmp_path: Path) -> None:
    target = settings(tmp_path, ("a/b", "c/d"))
    target.textfile.write_text(agent.startup_metrics(77.0), encoding="utf-8")
    write_report(target, {"repositories": {"a/b": repo([], [])}}, "100 160 0\n")
    line = agent.record(target, agent.parse_run("100 160 0"))
    assert (line["success"], line["missing"]) == (False, ["c/d"])
    text = target.textfile.read_text(encoding="utf-8")
    assert "ayeops_updates_last_success_timestamp_seconds 77\n" in text
    assert "ayeops_updates_last_run_success 0\n" in text


def test_record_without_a_report_counts_every_repository_missing(tmp_path: Path) -> None:
    target = settings(tmp_path)
    target.state.mkdir()
    target.report.write_text("{not json", encoding="utf-8")
    line = agent.record(target, agent.Run(1.0, 2.0, 1))
    assert (line["success"], line["missing"]) == (False, ["a/b"])


def test_prepare_opens_the_state_to_the_renovate_user_and_starts_the_stale_clock(
    tmp_path: Path,
) -> None:
    target = settings(tmp_path)
    agent.prepare(target, 123.0)
    assert (
        target.textfile.read_text(encoding="utf-8")
        == "ayeops_updates_last_success_timestamp_seconds 123\n"
    )
    if sys.platform != "win32":
        assert target.state.stat().st_mode & 0o777 == 0o777
    agent.prepare(target, 999.0)
    assert "123" in target.textfile.read_text(encoding="utf-8")


class Clock:
    def __init__(self, start: float) -> None:
        self.now = start
        self.slept: list[float] = []

    def wall(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def loops(count: int) -> Any:
    left = [count]

    def keep_going() -> bool:
        left[0] -= 1
        return left[0] >= 0

    return keep_going


def test_serve_asks_for_a_first_run_at_once_when_nothing_ran_yet(tmp_path: Path) -> None:
    target = settings(tmp_path)
    agent.prepare(target, 0.0)
    clock = Clock(time.mktime((2026, 10, 24, 12, 0, 0, 0, 0, -1)))
    agent.serve(target, clock.wall, clock.sleep, loops(1), tmp_path / "alive", lambda _: None)
    assert target.request.exists()
    assert (tmp_path / "alive").exists()


def test_serve_waits_for_the_schedule_once_a_run_exists_and_never_naps_past_it(
    tmp_path: Path,
) -> None:
    target = settings(tmp_path)
    write_report(target, {"repositories": {"a/b": repo([], [])}}, "1 2 0\n")
    start = time.mktime((2026, 10, 24, 5, 59, 0, 0, 0, -1))
    clock = Clock(start)
    agent.serve(target, clock.wall, clock.sleep, loops(1), tmp_path / "alive", lambda _: None)
    assert not target.request.exists()
    assert clock.slept == [30.0]
    clock.now = start + 50
    agent.serve(target, clock.wall, clock.sleep, loops(2), tmp_path / "alive", lambda _: None)
    assert target.request.exists()


def test_serve_records_each_finished_run_once(tmp_path: Path) -> None:
    target = settings(tmp_path)
    target.state.mkdir()
    lines: list[dict[str, Any]] = []
    clock = Clock(time.mktime((2026, 10, 24, 12, 0, 0, 0, 0, -1)))
    finished = iter([False, True, True])

    def keep_going() -> bool:
        step = next(finished, None)
        if step is None:
            return False
        if step and not target.last.exists():
            write_report(target, {"repositories": {"a/b": repo([], [])}}, "1 2 0\n")
        return True

    agent.serve(target, clock.wall, clock.sleep, keep_going, tmp_path / "alive", lines.append)
    assert [line["success"] for line in lines] == [True]


def test_settings_come_from_the_environment() -> None:
    parsed = agent.settings_from(
        {
            "AYE_UPDATES_SCHEDULE": "07:15",
            "AYE_UPDATES_REPOSITORIES": '["a/b", "c/d"]',
            "AYE_UPDATES_STATE": "/s",
            "AYE_UPDATES_TEXTFILE": "/t/x.prom",
        }
    )
    assert parsed == agent.Settings((7, 15), ("a/b", "c/d"), Path("/s"), Path("/t/x.prom"))


@pytest.mark.parametrize("value", ['"a/b"', "[1]", "{}"])
def test_settings_refuse_repositories_that_are_not_a_list_of_names(value: str) -> None:
    with pytest.raises(ValueError):
        agent.settings_from({"AYE_UPDATES_REPOSITORIES": value})


def test_main_once_requests_a_run_without_serving(tmp_path: Path) -> None:
    environ = {
        "AYE_UPDATES_ONCE": "1",
        "AYE_UPDATES_REPOSITORIES": '["a/b"]',
        "AYE_UPDATES_STATE": str(tmp_path / "state"),
        "AYE_UPDATES_TEXTFILE": str(tmp_path / "u.prom"),
    }
    assert agent.main(environ) == 0
    assert (tmp_path / "state" / "run.request").exists()


def with_updates(data: dict[str, Any], **settings: Any) -> dict[str, Any]:
    data["hosts"]["edge"]["modules"]["updates"] = {"repositories": ["acme/web"], **settings}
    return data


def services(data: dict[str, Any], modules: dict[str, Module]) -> dict[str, Any]:
    rendered: dict[str, Any] = host_compose(manifest_from(data), "edge", modules)["services"]
    return rendered


def test_the_example_is_valid_with_updates(data: dict[str, Any]) -> None:
    assert rules_hit(with_updates(data)) == set()


def test_updates_needs_at_least_one_repository(data: dict[str, Any]) -> None:
    data["hosts"]["edge"]["modules"]["updates"] = {"repositories": []}
    assert rules_hit(data) == {"module-settings"}
    data["hosts"]["edge"]["modules"]["updates"] = {"repositories": ["not a repo"]}
    assert rules_hit(data) == {"module-settings"}


def test_renovate_gets_the_token_repositories_and_report_path(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    rendered = services(with_updates(data, schedule="05:45", timezone="Europe/Kyiv"), modules)
    renovate, helper = rendered["updates"], rendered["updates-agent"]
    environment = renovate["environment"]
    assert environment["RENOVATE_TOKEN"] == "${AYE_UPDATES_TOKEN:?set AYE_UPDATES_TOKEN}"
    assert json.loads(environment["RENOVATE_REPOSITORIES"]) == ["acme/web"]
    assert environment["RENOVATE_REPORT_TYPE"] == "file"
    assert environment["RENOVATE_REPORT_PATH"] == "/state/report.json"
    assert environment["RENOVATE_PLATFORM"] == "github"
    assert "RENOVATE_ENDPOINT" not in environment
    assert "RENOVATE_DRY_RUN" not in environment
    assert renovate["mem_limit"] == "1g"
    assert renovate["cap_drop"] == ["ALL"]
    assert "updates-state:/state" in renovate["volumes"]
    assert "updates-state:/state" in helper["volumes"]
    assert helper["network_mode"] == "none"
    assert helper["read_only"] is True
    parsed = agent.settings_from(helper["environment"])
    assert (parsed.schedule, parsed.repositories) == ((5, 45), ("acme/web",))
    assert helper["environment"]["TZ"] == renovate["environment"]["TZ"] == "Europe/Kyiv"
    assert renovate["depends_on"] == {"updates-agent": {"condition": "service_healthy"}}


def test_another_forge_and_a_dry_run_reach_renovate(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    rendered = services(
        with_updates(
            data, platform="forgejo", endpoint="https://git.acme.example/api/v1", dry_run="lookup"
        ),
        modules,
    )
    environment = rendered["updates"]["environment"]
    assert environment["RENOVATE_PLATFORM"] == "forgejo"
    assert environment["RENOVATE_ENDPOINT"] == "https://git.acme.example/api/v1"
    assert environment["RENOVATE_DRY_RUN"] == "lookup"


def test_github_commits_go_through_the_api_and_carry_the_named_author(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    author = "Acme Bot <123+acme-bot@users.noreply.github.com>"
    environment = services(with_updates(data, git_author=author), modules)["updates"]["environment"]
    assert environment["RENOVATE_PLATFORM_COMMIT"] == "enabled"
    assert environment["RENOVATE_GIT_AUTHOR"] == author


def test_without_an_author_renovate_keeps_its_own_and_the_api_commits_stay(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    environment = services(with_updates(data), modules)["updates"]["environment"]
    assert "RENOVATE_GIT_AUTHOR" not in environment
    assert environment["RENOVATE_PLATFORM_COMMIT"] == "enabled"


def test_platform_commit_can_be_switched_and_is_not_sent_to_other_forges(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    github = services(with_updates(data, platform_commit="disabled"), modules)["updates"]
    assert github["environment"]["RENOVATE_PLATFORM_COMMIT"] == "disabled"
    gitea = services(
        with_updates(data, platform="gitea", endpoint="https://git.acme.example/api/v1"), modules
    )["updates"]
    assert "RENOVATE_PLATFORM_COMMIT" not in gitea["environment"]


def test_renovate_runs_no_post_upgrade_command_unless_allowed(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    environment = services(with_updates(data), modules)["updates"]["environment"]
    assert "RENOVATE_ALLOWED_COMMANDS" not in environment
    assert "RENOVATE_ALLOW_SHELL_EXECUTOR_FOR_POST_UPGRADE_COMMANDS" not in environment
    assert modules["updates"].defaults["allowed_commands"] == []


def test_allowed_commands_reach_renovate_as_json_with_dollars_escaped_for_compose(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    patterns = [documented_render_pattern(), r"^make generate$"]
    environment = services(with_updates(data, allowed_commands=patterns), modules)["updates"][
        "environment"
    ]
    value = environment["RENOVATE_ALLOWED_COMMANDS"]
    assert re.search(r"(?<!\$)\$(?!\$)", value) is None
    assert json.loads(value.replace("$$", "$")) == patterns


@pytest.mark.parametrize(
    "pattern",
    ["uv run aye render", "^uv run", "uv run$", "^a\nb$", "^$", ""],
)
def test_an_allowed_command_that_is_not_anchored_at_both_ends_is_refused(
    data: dict[str, Any], pattern: str
) -> None:
    assert rules_hit(with_updates(data, allowed_commands=[pattern])) == {"module-settings"}


@pytest.mark.parametrize(
    "author",
    ["renovate@acme.example", "Acme Bot", "Acme Bot <not-an-email>", "<a@b.c>", "Bot <a b@c.d>"],
)
def test_an_author_that_is_not_name_and_address_is_refused(
    data: dict[str, Any], author: str
) -> None:
    assert rules_hit(with_updates(data, git_author=author)) == {"module-settings"}


def test_a_platform_commit_value_renovate_does_not_know_is_refused(data: dict[str, Any]) -> None:
    assert rules_hit(with_updates(data, platform_commit="yes")) == {"module-settings"}


def test_the_loop_timeout_and_the_healthcheck_follow_timeout_minutes(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    renovate = services(with_updates(data, timeout_minutes=45), modules)["updates"]
    assert renovate["environment"]["AYE_UPDATES_TIMEOUT"] == "2700"
    assert "-mmin -75 " in renovate["healthcheck"]["test"][-1]


def test_updates_ships_its_agent_and_loop(data: dict[str, Any], modules: dict[str, Module]) -> None:
    tree = render_once(manifest_from(with_updates(data)), modules)
    for path in (AGENT, LOOP):
        assert tree[f"edge/updates/{path.name}"] == path.read_text(encoding="utf-8").replace(
            "\r\n", "\n"
        )


def test_the_loop_runs_renovate_under_a_timeout_and_hands_over_the_exit_code() -> None:
    text = LOOP.read_text(encoding="utf-8")
    assert 'timeout "$limit" renovate' in text
    assert "code=$?" in text
    assert text.index("run.last.tmp") < text.index('mv -f "$state/run.last.tmp" "$state/run.last"')
    assert 'rm -f "$state/run.request" "$state/report.json"' in text


def renovate_config() -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(RENOVATE.read_text(encoding="utf-8"))
    return loaded


def js_regex(pattern: str) -> re.Pattern[str]:
    return re.compile(re.sub(r"\(\?<(\w+)>", r"(?P<\1>", pattern))


def docker_manager(reads: str) -> dict[str, Any]:
    managers = [
        m
        for m in renovate_config()["customManagers"]
        if m["datasourceTemplate"] == "docker"
        and any(re.search(p.strip("/"), reads) for p in m["managerFilePatterns"])
    ]
    assert len(managers) == 1
    manager: dict[str, Any] = managers[0]
    return manager


def image_manager() -> dict[str, Any]:
    return docker_manager("src/ayeops/modules/updates/compose.yaml.j2")


def workflow_image_manager() -> dict[str, Any]:
    return docker_manager(".github/workflows/ci.yml")


def files_the_manager_reads() -> list[Path]:
    patterns = [re.compile(p.strip("/")) for p in image_manager()["managerFilePatterns"]]
    candidates = sorted(ROOT.glob("src/ayeops/modules/*/compose.yaml.j2")) + sorted(
        RENDERED.glob("*/compose.yaml")
    )
    return [
        path
        for path in candidates
        if any(pattern.search(path.relative_to(ROOT).as_posix()) for pattern in patterns)
    ]


def test_the_image_manager_reads_every_module_template_and_rendered_compose() -> None:
    read = set(files_the_manager_reads())
    assert set(ROOT.glob("src/ayeops/modules/*/compose.yaml.j2")) <= read
    assert set(RENDERED.glob("*/compose.yaml")) <= read
    assert (MODULE / "compose.yaml.j2") in read


def test_the_image_regex_matches_every_image_line_whole() -> None:
    regex = js_regex(image_manager()["matchStrings"][0])
    checked = 0
    for path in files_the_manager_reads():
        for line in path.read_text(encoding="utf-8").splitlines():
            if not re.match(r"\s*image:", line):
                continue
            value = line.split("image:", 1)[1].strip().strip("\"'")
            match = regex.search(line)
            assert match is not None, (path, line)
            digest = f"@{match['currentDigest']}" if match["currentDigest"] else ""
            assert f"{match['depName']}:{match['currentValue']}{digest}" == value, (path, line)
            checked += 1
    assert checked >= 40


def test_the_image_regex_finds_as_many_images_as_there_are_image_lines() -> None:
    regex = js_regex(image_manager()["matchStrings"][0])
    for path in files_the_manager_reads():
        text = path.read_text(encoding="utf-8")
        assert len(regex.findall(text)) == len(re.findall(r"(?m)^\s*image:", text)), path


def test_the_image_regex_takes_a_quoted_image_and_the_digest_after_the_tag() -> None:
    regex = js_regex(image_manager()["matchStrings"][0])
    quoted = regex.search('    image: "valkey/valkey:9.1.2-alpine3.24"')
    assert quoted is not None and quoted["currentValue"] == "9.1.2-alpine3.24"
    assert quoted["currentDigest"] is None
    digested = regex.search("    image: postgres:18.6@sha256:" + "a" * 64)
    assert digested is not None and digested["currentValue"] == "18.6"
    assert digested["currentDigest"] == "sha256:" + "a" * 64


def test_the_workflow_image_regex_reads_every_digest_pinned_image_whole() -> None:
    manager = workflow_image_manager()
    patterns = [re.compile(p.strip("/")) for p in manager["managerFilePatterns"]]
    regex = js_regex(manager["matchStrings"][0])
    pinned = re.compile(r"\S+:\S+@sha256:[0-9a-f]{64}")
    for workflow in sorted((ROOT / ".github" / "workflows").glob("*.yml")):
        assert any(p.search(workflow.relative_to(ROOT).as_posix()) for p in patterns)
        text = workflow.read_text(encoding="utf-8")
        images = pinned.findall(text)
        matches = list(regex.finditer(text))
        assert len(matches) == len(images), workflow.name
        for image, match in zip(images, matches, strict=True):
            whole = f"{match['depName']}:{match['currentValue']}@{match['currentDigest']}"
            assert whole == image, workflow.name
    sample = f"        image: ghcr.io/example/tool:v1.2.3@sha256:{'0' * 64}\n"
    found = regex.search(sample)
    assert found is not None
    assert f"{found['depName']}:{found['currentValue']}" == "ghcr.io/example/tool:v1.2.3"


def test_the_secret_scan_runs_a_pinned_gitleaks_over_the_whole_history() -> None:
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    job = ci.split("\n  secrets:\n", 1)[1].split("\n\n", 1)[0]
    assert "fetch-depth: 0" in job
    assert re.search(r"GITLEAKS_VERSION: \d+\.\d+\.\d+\n", job)
    assert re.search(r"GITLEAKS_SHA256_X64: [0-9a-f]{64}\n", job)
    assert re.search(r"GITLEAKS_SHA256_ARM64: [0-9a-f]{64}\n", job)
    assert "sha256sum -c -" in job and "docker" not in job
    assert "--config .gitleaks.toml" in job and "--redact" in job


def test_the_renovate_image_is_pinned_and_rendered_in_the_example() -> None:
    template = (MODULE / "compose.yaml.j2").read_text(encoding="utf-8")
    image = re.search(r"image:\s*(ghcr\.io/renovatebot/renovate:\S+)", template)
    assert image is not None
    assert re.fullmatch(
        r"ghcr\.io/renovatebot/renovate:\d+\.\d+\.\d+@sha256:[a-f0-9]{64}", image.group(1)
    )
    assert image.group(1) in (RENDERED / "edge" / "compose.yaml").read_text(encoding="utf-8")


def test_renovate_waits_two_weeks_and_never_merges_on_its_own() -> None:
    config = renovate_config()
    assert config["minimumReleaseAge"] == "14 days"
    assert config["internalChecksFilter"] == "strict"
    assert config["automerge"] is False
    assert not any(rule.get("automerge") for rule in config["packageRules"])


def test_renovate_groups_minor_and_patch_and_leaves_majors_alone() -> None:
    rules = [r for r in renovate_config()["packageRules"] if "groupName" in r]
    assert {r["groupName"] for r in rules} == {
        "container images",
        "python packages",
        "github actions",
        "npm packages",
    }
    assert all("major" not in r["matchUpdateTypes"] for r in rules)


def documented_render_pattern() -> str:
    docs = (ROOT / "docs" / "modules.md").read_text(encoding="utf-8")
    section = docs.split("### Post-upgrade commands", 1)[1].split("\n## ", 1)[0]
    block = section.split("```yaml\n", 1)[1].split("```", 1)[0]
    patterns: list[str] = yaml.safe_load(block)["updates"]["allowed_commands"]
    assert len(patterns) == 1
    return patterns[0]


def render_task() -> dict[str, Any]:
    rules = [r for r in renovate_config()["packageRules"] if "postUpgradeTasks" in r]
    assert len(rules) == 1
    assert rules[0]["matchDatasources"] == ["docker"]
    assert set(rules[0]) == {"description", "matchDatasources", "postUpgradeTasks"}
    task: dict[str, Any] = rules[0]["postUpgradeTasks"]
    return task


def test_every_image_branch_renders_the_example_again_and_commits_only_the_render() -> None:
    task = render_task()
    assert task["executionMode"] == "branch"
    assert task["fileFilters"] == ["examples/rendered/**"]
    assert task["installTools"] == {"uv": {}}
    [command] = task["commands"]
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    checked = re.search(r"run: uv run (aye render examples/acme\.yaml .*)$", ci, re.M)
    assert checked is not None
    assert command == "uv run --frozen --no-dev " + checked.group(1).replace(" --check", "")
    agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    assert command.replace("--frozen --no-dev ", "") in agents


def test_the_documented_allowed_command_admits_exactly_the_render_task() -> None:
    [command] = render_task()["commands"]
    pattern = js_regex(documented_render_pattern())
    assert pattern.fullmatch(command)
    for other in (
        command + " && curl x",
        command.replace("examples/acme.yaml", "examples/acmeXyaml"),
        "uv run aye deploy examples/acme.yaml",
        "x" + command,
    ):
        assert pattern.search(other) is None, other


def test_renovate_does_not_skip_the_example_and_e2e_folders() -> None:
    config = renovate_config()
    assert ":ignoreModulesAndTests" in config["ignorePresets"]
    assert not any(
        p.startswith(("examples", "tests/e2e", "**/tests", "**/examples"))
        for p in config["ignorePaths"]
    )


def test_the_weekly_schedule_covers_any_hour_of_the_run() -> None:
    assert renovate_config()["schedule"] == ["* * * * 1"]


def test_every_workflow_action_is_pinned_by_sha_with_its_tag() -> None:
    pinned = re.compile(r"uses:\s*([\w.-]+/[\w./-]+@[0-9a-f]{40} # v\d+(\.\d+)*|\./[\w./-]+)$")
    for workflow in sorted((ROOT / ".github" / "workflows").glob("*.yml")):
        for line in workflow.read_text(encoding="utf-8").splitlines():
            if "uses:" in line:
                assert pinned.search(line), (workflow.name, line)


def test_the_token_is_in_the_env_example() -> None:
    assert "AYE_UPDATES_TOKEN=replace-me" in (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "AYE_UPDATES_TOKEN=" in (RENDERED / "edge" / ".env.example").read_text(encoding="utf-8")


def test_the_stale_alert_allows_two_days_and_the_pending_alert_thirty(
    modules: dict[str, Module],
) -> None:
    alerts = {alert["name"]: alert["expr"] for alert in modules["updates"].alerts}
    assert alerts["updates-stale"].endswith("> bool 172800")
    assert alerts["updates-pending-old"].endswith(f"> bool {30 * DAY}")
