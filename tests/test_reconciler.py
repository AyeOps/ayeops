from __future__ import annotations

import importlib.util
import json
import sys
from collections.abc import Mapping
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from ayeops.registry import Module
from ayeops.render import desired_projects, host_compose, kept_running
from tests.conftest import ROOT, manifest_from, render_once, rules_hit

AGENT = ROOT / "src" / "ayeops" / "modules" / "reconciler" / "files" / "reconciler.py"
NOW = 2_000_000_000.0
LISTED = {"running", "restarting", "paused"}


def load_agent() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "ayeops.modules.reconciler.files.reconciler", AGENT
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


agent = load_agent()


def container(
    ident: str,
    service: str,
    status: str = "running",
    project: str = "acme-apps",
    exit_code: int = 0,
    error: str = "",
    restart: str = "no",
    oom: bool = False,
    health: str = "",
    depends: str = "",
    oneoff: bool = False,
    image: str = "",
    name: str = "",
) -> dict[str, Any]:
    labels = {
        "com.docker.compose.project": project,
        "com.docker.compose.service": service,
        "com.docker.compose.oneoff": "True" if oneoff else "False",
    }
    if not project:
        labels = {}
    if depends:
        labels["com.docker.compose.depends_on"] = depends
    state: dict[str, Any] = {
        "Status": status,
        "ExitCode": exit_code,
        "Error": error,
        "OOMKilled": oom,
    }
    if health:
        state["Health"] = {"Status": health}
    return {
        "Id": ident,
        "Name": "/" + (name or f"{project}-{service}-1"),
        "Config": {"Labels": labels, "Image": image or f"example/{service}:1.0"},
        "State": state,
        "HostConfig": {"RestartPolicy": {"Name": restart}},
    }


class FakeDocker:
    def __init__(self, *containers: dict[str, Any], boots: Mapping[str, str] | None = None) -> None:
        self.containers = {c["Id"]: c for c in containers}
        self.after_start = dict(boots or {})
        self.refuse: set[str] = set()
        self.calls: list[tuple[str, str, dict[str, str]]] = []

    def __call__(self, method: str, path: str, query: Mapping[str, str]) -> Any:
        self.calls.append((method, path, dict(query)))
        if method == "GET" and path == "/containers/json":
            wanted = json.loads(query.get("filters", "{}")).get("label", [])
            return [
                {
                    "Id": c["Id"],
                    "Names": [c["Name"]],
                    "Image": c["Config"]["Image"],
                    "State": c["State"]["Status"],
                    "Labels": c["Config"]["Labels"],
                }
                for c in self.containers.values()
                if all(label in c["Config"]["Labels"] for label in wanted)
                and (query.get("all") == "true" or c["State"]["Status"] in LISTED)
            ]
        ident = path.split("/")[2]
        if ident not in self.containers:
            raise RuntimeError(f"{method} {path}: 404")
        if method == "GET":
            return self.containers[ident]
        if ident in self.refuse:
            raise RuntimeError(f"{method} {path}: 500 b'port is already allocated'")
        state = self.containers[ident]["State"]
        state.update({"Status": self.after_start.get(ident, "running"), "Error": ""})
        return None

    def started(self) -> list[str]:
        return [path.split("/")[2] for method, path, _ in self.calls if method == "POST"]


class Clock:
    def __init__(self, now: float = NOW) -> None:
        self.now = now
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def settings(**overrides: Any) -> Any:
    values: dict[str, Any] = {"projects": {"acme-apps": ["mcp", "web"]}}
    values.update(overrides)
    return agent.Settings(**values)


def seen(**fields: Any) -> Any:
    base: dict[str, Any] = {
        "id": "c1",
        "project": "acme-apps",
        "service": "web",
        "name": "acme-apps-web-1",
        "status": "exited",
    }
    base.update(fields)
    return agent.Seen(**base)


def run(docker: FakeDocker, state: Any = None, boot: str = "boot-1", **overrides: Any) -> Any:
    lines: list[dict[str, Any]] = []
    clock = Clock()
    result, tallies = agent.reconcile(
        docker,
        settings(**overrides),
        state if state is not None else agent.State(boot_id=boot),
        boot,
        clock,
        clock.sleep,
        lines.append,
    )
    return result, tallies, lines


@pytest.mark.parametrize(
    ("text", "expected"), [("30s", 30), ("2m", 120), ("1h", 3600), (" 45s ", 45)]
)
def test_parse_duration_reads_seconds_minutes_and_hours(text: str, expected: int) -> None:
    assert agent.parse_duration(text) == expected


@pytest.mark.parametrize("text", ["", "0s", "1d", "1.5m", "m"])
def test_parse_duration_refuses_anything_else(text: str) -> None:
    with pytest.raises(ValueError, match="not a duration"):
        agent.parse_duration(text)


def test_settings_come_from_the_environment_with_defaults() -> None:
    assert agent.settings_from({}) == agent.Settings()
    parsed = agent.settings_from(
        {
            "AYE_RECONCILER_PROJECTS": '{"b":["y","x","x"],"a":[]}',
            "AYE_RECONCILER_INTERVAL": "1m",
            "AYE_RECONCILER_ATTEMPTS": "5",
            "AYE_RECONCILER_WINDOW": "2h",
            "AYE_RECONCILER_TIMEOUT": "90s",
            "AYE_RECONCILER_DRY_RUN": "true",
        }
    )
    assert parsed.projects == {"b": ["x", "y"], "a": []}
    assert (parsed.interval, parsed.attempts, parsed.window, parsed.timeout) == (60, 5, 7200, 90)
    assert parsed.dry_run is True


def test_projects_must_be_a_json_object() -> None:
    with pytest.raises(ValueError, match="not a JSON object"):
        agent.parse_projects("[]")


def test_parse_depends_reads_compose_labels_old_and_new() -> None:
    assert agent.parse_depends("db:service_healthy:false,cache:service_started") == (
        ("db", "service_healthy"),
        ("cache", "service_started"),
    )
    assert agent.parse_depends("db") == (("db", "service_started"),)
    assert agent.parse_depends("") == ()


def test_a_running_restarting_or_paused_container_is_up() -> None:
    for status in ("running", "restarting", "paused", "removing"):
        assert agent.verdict(seen(status=status), set()) == "up"


def test_a_dead_container_is_stuck_since_only_a_recreate_helps() -> None:
    assert agent.verdict(seen(status="dead", exit_code=1), set()) == "stuck"


def test_a_failed_start_is_acted_on_whatever_the_restart_policy() -> None:
    for restart in ("", "no", "on-failure", "always", "unless-stopped"):
        failed = seen(exit_code=128, error="bind: cannot assign requested address", restart=restart)
        assert agent.verdict(failed, set()) == "start-failed"
    assert agent.verdict(seen(status="created", error="no such file"), set()) == "start-failed"


def test_docker_owns_a_container_whose_policy_restarts_it() -> None:
    for code in (0, 1, 137, 143):
        for restart in ("always", "unless-stopped"):
            assert agent.verdict(seen(exit_code=code, restart=restart), {"c1"}) == "stopped"


def test_a_crash_without_a_restarting_policy_is_acted_on() -> None:
    assert agent.verdict(seen(exit_code=1, restart="no"), set()) == "crashed"
    assert agent.verdict(seen(exit_code=2, restart="on-failure"), set()) == "crashed"
    assert agent.verdict(seen(exit_code=137, oom=True), set()) == "oom-killed"


def test_a_stop_signal_or_a_clean_exit_counts_as_deliberate() -> None:
    for code in (0, 137, 143):
        assert agent.verdict(seen(exit_code=code), set()) == "stopped"
    assert agent.verdict(seen(status="created"), set()) == "stopped"


def test_a_container_running_before_the_reboot_is_brought_back() -> None:
    for code in (0, 137, 143):
        assert agent.verdict(seen(exit_code=code), {"c1"}) == "not-back-after-boot"


def test_start_order_follows_depends_on_inside_the_project() -> None:
    app = seen(id="a", service="app", depends=(("db", "service_healthy"),))
    db = seen(id="d", service="db")
    worker = seen(id="w", service="worker", depends=(("app", "service_started"), ("x", "s")))
    assert [c.id for c in agent.start_order([worker, app, db])] == ["d", "a", "w"]


def test_start_order_breaks_a_cycle_instead_of_hanging() -> None:
    one = seen(id="1", service="one", depends=(("two", "service_started"),))
    two = seen(id="2", service="two", depends=(("one", "service_started"),))
    assert sorted(c.id for c in agent.start_order([two, one])) == ["1", "2"]


def test_satisfied_matches_the_compose_conditions() -> None:
    assert agent.satisfied(seen(status="running"), "service_started")
    assert agent.satisfied(seen(status="running", health="healthy"), "service_healthy")
    assert not agent.satisfied(seen(status="running", health="starting"), "service_healthy")
    assert agent.satisfied(seen(exit_code=0), "service_completed_successfully")
    assert not agent.satisfied(seen(exit_code=1), "service_completed_successfully")
    assert not agent.satisfied(seen(), "service_started")


def test_a_healthy_project_needs_nothing_and_writes_its_counts() -> None:
    docker = FakeDocker(container("w", "web"), container("m", "mcp"))
    state, tallies, lines = run(docker)
    assert lines == [] and docker.started() == []
    assert tallies["acme-apps"] == agent.Tally(missing=0, stopped=0)
    assert state.running == {"w", "m"} and state.last_run == NOW


def test_only_declared_services_of_declared_projects_are_looked_at() -> None:
    docker = FakeDocker(
        container("w", "web", "exited", exit_code=1),
        container("o", "other", "exited", project="someone-else", exit_code=1),
        container("x", "extra", "exited", exit_code=1),
        container("r", "web", "exited", exit_code=1, oneoff=True),
        container("m", "mcp"),
    )
    _, _, lines = run(docker)
    assert docker.started() == ["w"]
    assert lines[0]["reasons"] == {"acme-apps-web-1": "crashed"}


def test_an_attempt_logs_before_and_after_and_counts_a_success() -> None:
    docker = FakeDocker(container("w", "web", "exited", exit_code=1), container("m", "mcp"))
    state, _, lines = run(docker)
    [line] = lines
    assert line["event"] == "attempt" and line["success"] is True
    assert (line["attempt"], line["of"], line["started"]) == (1, 3, ["acme-apps-web-1"])
    before = {entry["service"]: entry for entry in line["before"]}
    after = {entry["service"]: entry for entry in line["after"]}
    assert before["web"] == {
        "service": "web",
        "name": "acme-apps-web-1",
        "status": "exited",
        "exit_code": 1,
    }
    assert after["web"]["status"] == "running"
    assert state.books["acme-apps"].totals == {"attempts": 1, "successes": 1, "failures": 0}
    assert state.books["acme-apps"].attempts == [NOW]


def test_a_refused_start_is_a_failure_with_the_reason() -> None:
    docker = FakeDocker(container("w", "web", "exited", error="port is already allocated"))
    docker.refuse.add("w")
    state, _, lines = run(docker)
    assert lines[0]["success"] is False
    assert "port is already allocated" in lines[0]["errors"][0]
    assert state.books["acme-apps"].totals["failures"] == 1


def test_a_container_that_exits_again_fails_the_attempt_without_waiting_out_the_timeout() -> None:
    docker = FakeDocker(container("w", "web", "exited", exit_code=1), boots={"w": "exited"})
    clock = Clock()
    lines: list[dict[str, Any]] = []
    agent.reconcile(docker, settings(), agent.State(), "b", clock, clock.sleep, lines.append)
    assert lines[0]["success"] is False and clock.slept == []


def test_the_attempt_waits_for_a_starting_healthcheck_within_the_timeout() -> None:
    docker = FakeDocker(container("w", "web", "exited", exit_code=1), boots={"w": "running"})
    docker.containers["w"]["State"]["Health"] = {"Status": "starting"}
    clock = Clock()
    lines: list[dict[str, Any]] = []
    agent.reconcile(
        docker, settings(timeout=10), agent.State(), "b", clock, clock.sleep, lines.append
    )
    assert sum(clock.slept) == 10 and all(s <= agent.POLL for s in clock.slept)
    assert lines[0]["success"] is True


def test_an_unhealthy_container_after_the_start_is_a_failure() -> None:
    docker = FakeDocker(container("w", "web", "exited", exit_code=1))
    docker.containers["w"]["State"]["Health"] = {"Status": "unhealthy"}
    _, _, lines = run(docker)
    assert lines[0]["success"] is False


def test_a_dependency_is_started_and_awaited_first() -> None:
    docker = FakeDocker(
        container("a", "app", "exited", exit_code=1, depends="db:service_healthy:false"),
        container("d", "db", "exited", exit_code=1, health="healthy"),
    )
    _, _, lines = run(docker, projects={"acme-apps": ["app", "db"]})
    assert docker.started() == ["d", "a"]
    assert lines[0]["success"] is True


def test_a_deliberately_stopped_dependency_is_not_waited_for() -> None:
    docker = FakeDocker(
        container("a", "app", "exited", exit_code=1, depends="db:service_started:false"),
        container("d", "db", "exited", exit_code=0),
    )
    clock = Clock()
    lines: list[dict[str, Any]] = []
    agent.reconcile(
        docker,
        settings(projects={"acme-apps": ["app", "db"]}),
        agent.State(),
        "b",
        clock,
        clock.sleep,
        lines.append,
    )
    assert docker.started() == [] and clock.slept == []
    assert lines[0]["errors"] == ["app: dependency db is stopped"]


def test_back_off_gives_up_after_the_attempts_in_the_window_and_logs_it_once() -> None:
    state = agent.State(boot_id="boot-1")
    state.books["acme-apps"] = agent.Book(attempts=[NOW - 3000, NOW - 2000, NOW - 1000])
    docker = FakeDocker(container("w", "web", "exited", exit_code=1))
    state, _, lines = run(docker, state)
    assert docker.started() == []
    assert [line["event"] for line in lines] == ["gave-up"]
    assert lines[0]["reasons"] == {"acme-apps-web-1": "crashed"}
    assert state.books["acme-apps"].given_up is True
    _, _, again = run(docker, state)
    assert again == [] and docker.started() == []


def test_attempts_older_than_the_window_do_not_count() -> None:
    state = agent.State(boot_id="boot-1")
    state.books["acme-apps"] = agent.Book(attempts=[NOW - 7200, NOW - 2000, NOW - 1000])
    docker = FakeDocker(container("w", "web", "exited", exit_code=1))
    state, _, lines = run(docker, state)
    assert docker.started() == ["w"] and lines[0]["attempt"] == 3


def test_a_project_given_up_on_stays_left_alone_until_it_is_up_again() -> None:
    state = agent.State(boot_id="boot-1")
    state.books["acme-apps"] = agent.Book(given_up=True)
    docker = FakeDocker(container("w", "web", "exited", exit_code=1))
    state, _, lines = run(docker, state)
    assert lines == [] and docker.started() == []
    docker.containers["w"]["State"].update({"Status": "running"})
    state, _, lines = run(docker, state)
    assert [line["event"] for line in lines] == ["recovered"]
    assert state.books["acme-apps"].given_up is False


def test_a_dry_run_starts_nothing_counts_nothing_and_logs_a_plan_once() -> None:
    docker = FakeDocker(container("w", "web", "exited", exit_code=1))
    state, _, lines = run(docker, dry_run=True)
    assert docker.started() == []
    assert lines == [
        {"event": "would-start", "project": "acme-apps", "reasons": {"acme-apps-web-1": "crashed"}}
    ]
    assert state.books["acme-apps"].attempts == []
    _, _, again = run(docker, state, dry_run=True)
    assert again == []


def test_missing_and_dead_containers_are_counted_not_touched() -> None:
    docker = FakeDocker(container("d", "web", "dead", exit_code=1))
    _, tallies, lines = run(docker)
    assert tallies["acme-apps"] == agent.Tally(missing=2, stopped=0)
    assert lines == [] and docker.started() == []


def test_a_deliberate_stop_is_counted_and_left_alone() -> None:
    docker = FakeDocker(
        container("w", "web", "exited", exit_code=143),
        container("m", "mcp", "exited", exit_code=0, restart="unless-stopped"),
    )
    _, tallies, lines = run(docker)
    assert tallies["acme-apps"].stopped == 2 and lines == [] and docker.started() == []


def test_after_a_reboot_only_containers_seen_running_before_are_started() -> None:
    docker = FakeDocker(container("w", "web"), container("m", "mcp", "exited", exit_code=0))
    state, _, _ = run(docker, boot="boot-1")
    assert state.running == {"w"}
    docker.containers["w"]["State"].update({"Status": "exited", "ExitCode": 143})
    state, _, lines = run(docker, state, boot="boot-2")
    assert docker.started() == ["w"]
    assert lines[0]["reasons"] == {"acme-apps-web-1": "not-back-after-boot"}
    assert (state.boot_id, state.after_boot) == ("boot-2", {"w"})
    state, _, _ = run(docker, state, boot="boot-2")
    assert (state.running, state.after_boot) == ({"w"}, set())


def test_a_stop_later_in_the_same_boot_is_respected_once_the_container_ran() -> None:
    docker = FakeDocker(container("w", "web", "exited", exit_code=0))
    state = agent.State(boot_id="boot-1", running={"w"})
    state, _, _ = run(docker, state, boot="boot-2")
    state, _, _ = run(docker, state, boot="boot-2")
    assert state.after_boot == set() and docker.started() == ["w"]
    docker.containers["w"]["State"].update({"Status": "exited", "ExitCode": 0})
    _, _, lines = run(docker, state, boot="boot-2")
    assert lines == [] and docker.started() == ["w"]


def test_the_first_boot_the_reconciler_sees_has_no_previous_running_set() -> None:
    state = agent.new_boot(agent.State(boot_id="", running={"w"}), "boot-1")
    assert state.after_boot == set() and state.boot_id == "boot-1"
    same = agent.State(boot_id="boot-1", running={"w"})
    assert agent.new_boot(same, "boot-1") is same
    assert agent.new_boot(same, "") is same


def test_books_of_projects_no_longer_declared_are_dropped() -> None:
    state = agent.State(boot_id="boot-1")
    state.books["gone"] = agent.Book(given_up=True)
    state, _, _ = run(FakeDocker(), state)
    assert set(state.books) == {"acme-apps"}


def test_state_survives_a_round_trip_through_json(tmp_path: Path) -> None:
    state = agent.State(
        boot_id="b",
        running={"x", "y"},
        after_boot={"z"},
        books={"p": agent.Book(attempts=[1.0], given_up=True, totals={"attempts": 4})},
        last_run=5.0,
    )
    path = tmp_path / "state.json"
    agent.write_atomic(path, json.dumps(agent.state_data(state)))
    loaded = agent.load_state(path)
    assert (loaded.boot_id, loaded.running, loaded.after_boot) == ("b", {"x", "y"}, {"z"})
    assert loaded.books["p"].totals == {"attempts": 4, "successes": 0, "failures": 0}
    assert loaded.books["p"].given_up is True and loaded.last_run == 5.0


def test_an_unreadable_state_starts_fresh(tmp_path: Path) -> None:
    assert agent.load_state(tmp_path / "missing.json") == agent.State()
    broken = tmp_path / "broken.json"
    broken.write_text("{nope", encoding="utf-8")
    assert agent.load_state(broken) == agent.State()
    broken.write_text("[]", encoding="utf-8")
    assert agent.load_state(broken) == agent.State()


def test_render_metrics_writes_every_project_with_its_counters() -> None:
    state = agent.State(last_run=NOW + 0.4)
    state.books["acme-apps"] = agent.Book(
        given_up=True, totals={"attempts": 3, "successes": 1, "failures": 2}
    )
    tallies = {"acme-apps": agent.Tally(missing=1, stopped=2)}
    text = agent.render_metrics(state, tallies, settings(), agent.Drift())
    assert text.splitlines() == [
        "ayeops_reconciler_last_run_timestamp_seconds 2000000000",
        "ayeops_reconciler_dry_run 0",
        "ayeops_reconciler_projects 1",
        'ayeops_reconciler_attempts_total{compose_project="acme-apps"} 3',
        'ayeops_reconciler_successes_total{compose_project="acme-apps"} 1',
        'ayeops_reconciler_failures_total{compose_project="acme-apps"} 2',
        'ayeops_reconciler_given_up{compose_project="acme-apps"} 1',
        'ayeops_reconciler_missing{compose_project="acme-apps"} 1',
        'ayeops_reconciler_stopped{compose_project="acme-apps"} 2',
        "ayeops_undeclared_containers 0",
        "ayeops_undeclared_containers_overflow 0",
    ]


def test_metric_labels_are_escaped() -> None:
    assert agent.metric("x", 1, project='a"b\\c') == 'ayeops_reconciler_x{project="a\\"b\\\\c"} 1\n'


def test_record_writes_state_and_textfile(tmp_path: Path) -> None:
    docker = FakeDocker(container("w", "web", "exited", exit_code=1))
    configured = settings(textfile=tmp_path / "r.prom", state=tmp_path / "state.json")
    lines: list[dict[str, Any]] = []
    clock = Clock()
    agent.record(docker, configured, agent.State(), clock, clock.sleep, lines.append, lambda: "b")
    assert 'ayeops_reconciler_attempts_total{compose_project="acme-apps"} 1' in (
        tmp_path / "r.prom"
    ).read_text(encoding="utf-8")
    assert agent.load_state(tmp_path / "state.json").books["acme-apps"].totals["successes"] == 1


def test_record_reports_an_unreachable_docker_and_keeps_the_textfile(tmp_path: Path) -> None:
    def refuse(method: str, path: str, query: Mapping[str, str]) -> Any:
        raise OSError("connection refused")

    configured = settings(textfile=tmp_path / "r.prom", state=tmp_path / "state.json")
    lines: list[dict[str, Any]] = []
    before = agent.State(last_run=1.0)
    after = agent.record(refuse, configured, before, Clock(), Clock().sleep, lines.append, str)
    assert after is before
    assert lines == [{"event": "error", "error": "OSError: connection refused"}]
    assert not (tmp_path / "r.prom").exists()


def test_serve_waits_an_interval_before_each_pass_and_touches_the_heartbeat(
    tmp_path: Path,
) -> None:
    clock = Clock()
    heartbeat = tmp_path / "alive"
    passes = iter([True, True, False])
    configured = settings(
        projects={}, interval=70, textfile=tmp_path / "r.prom", state=tmp_path / "s.json"
    )
    state = agent.serve(
        FakeDocker(),
        configured,
        agent.State(),
        clock,
        clock.sleep,
        heartbeat,
        lambda: next(passes),
        lambda: "b",
    )
    assert clock.slept == [30.0, 30.0, 10.0, 30.0, 30.0, 10.0]
    assert heartbeat.exists() and state.last_run == NOW + 140


def test_read_boot_id_is_empty_without_proc(tmp_path: Path) -> None:
    assert agent.read_boot_id(tmp_path / "none") == ""
    (tmp_path / "id").write_text("abc\n", encoding="utf-8")
    assert agent.read_boot_id(tmp_path / "id") == "abc"


def test_main_once_runs_one_pass(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sockets: list[str] = []

    def connect(path: str) -> FakeDocker:
        sockets.append(path)
        return FakeDocker(container("w", "web"))

    monkeypatch.setattr(agent, "docker_api", connect)
    environ = {
        "AYE_RECONCILER_ONCE": "1",
        "AYE_RECONCILER_PROJECTS": '{"acme-apps":["web"]}',
        "AYE_RECONCILER_TEXTFILE": str(tmp_path / "r.prom"),
        "AYE_RECONCILER_STATE": str(tmp_path / "s.json"),
    }
    assert agent.main(environ) == 0
    assert sockets == ["/var/run/docker.sock"]
    assert "ayeops_reconciler_missing" in (tmp_path / "r.prom").read_text(encoding="utf-8")


def test_the_listing_asks_docker_for_compose_containers_only() -> None:
    docker = FakeDocker()
    agent.observe(docker, {"p": ["s"]})
    assert docker.calls == [
        (
            "GET",
            "/containers/json",
            {"all": "true", "filters": '{"label": ["com.docker.compose.project"]}'},
        )
    ]


def test_running_containers_are_not_inspected() -> None:
    docker = FakeDocker(container("w", "web"), container("m", "mcp", "exited"))
    agent.observe(docker, {"acme-apps": ["mcp", "web"]})
    assert [path for _, path, _ in docker.calls] == ["/containers/json", "/containers/m/json"]


def test_the_agent_imports_nothing_but_the_standard_library() -> None:
    lines = AGENT.read_text(encoding="utf-8").splitlines()
    imported = {
        line.split()[1].split(".")[0] for line in lines if line.startswith(("import ", "from "))
    }
    assert imported - {"mutmut"} <= set(sys.stdlib_module_names) | {"__future__"}


def compose_of(data: dict[str, Any], host: str, modules: dict[str, Module]) -> dict[str, Any]:
    return host_compose(manifest_from(data), host, modules)


def test_the_example_runs_the_reconciler_on_central(data: dict[str, Any]) -> None:
    assert "reconciler" in data["hosts"]["central"]["modules"]
    assert rules_hit(data) == set()


def test_the_reconciler_reaches_the_socket_without_a_network(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    service = compose_of(data, "central", modules)["services"]["reconciler"]
    assert service["network_mode"] == "none"
    assert service["read_only"] is True and service["cap_drop"] == ["ALL"]
    assert "ports" not in service
    assert "/var/run/docker.sock:/var/run/docker.sock" in service["volumes"]
    assert "node-textfile:/textfile" in service["volumes"]
    assert "reconciler-data:/data" in service["volumes"]
    environment = service["environment"]
    parsed = agent.settings_from(environment)
    assert parsed == agent.Settings(projects=parsed.projects, declared=parsed.declared)
    assert set(parsed.projects) == {"acme-apps", "acme-central"}
    assert parsed.declared == ["acme-apps", "acme-central"]
    assert parsed.projects["acme-apps"] == ["mcp", "web"]
    assert {"grafana", "reconciler", "victoriametrics"} <= set(parsed.projects["acme-central"])


def test_the_desired_state_is_every_rendered_service_of_the_host(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    compose = compose_of(data, "central", modules)
    projects = json.loads(
        compose["services"]["reconciler"]["environment"]["AYE_RECONCILER_PROJECTS"]
    )
    assert projects[compose["name"]] == sorted(compose["services"])


def test_reconciler_settings_reach_the_agent(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    data["hosts"]["central"]["modules"]["reconciler"] = {
        "interval": "1m",
        "attempts": 5,
        "window": "2h",
        "timeout": "90s",
        "dry_run": True,
        "exclude": ["mcp", "acme-central/grafana"],
    }
    service = compose_of(data, "central", modules)["services"]["reconciler"]
    parsed = agent.settings_from(service["environment"])
    assert (parsed.interval, parsed.attempts, parsed.window, parsed.timeout) == (60, 5, 7200, 90)
    assert parsed.dry_run is True
    assert parsed.projects["acme-apps"] == ["web"]
    assert "grafana" not in parsed.projects["acme-central"]
    assert "victoriametrics" in parsed.projects["acme-central"]


def test_the_reconciler_requires_node_for_its_textfile(data: dict[str, Any]) -> None:
    del data["hosts"]["central"]["modules"]["node"]
    assert "module-requires" in rules_hit(data)


@pytest.mark.parametrize(
    "bad",
    [{"interval": "30"}, {"attempts": 0}, {"window": "10s"}, {"ignore": ["a b"]}, {"ignore": "x"}],
)
def test_reconciler_settings_are_validated(data: dict[str, Any], bad: dict[str, Any]) -> None:
    data["hosts"]["central"]["modules"]["reconciler"] = bad
    assert "module-settings" in rules_hit(data)


def test_the_reconciler_ships_its_agent(data: dict[str, Any], modules: dict[str, Module]) -> None:
    tree = render_once(manifest_from(data), modules)
    assert tree["central/reconciler/reconciler.py"] == AGENT.read_text(encoding="utf-8").replace(
        "\r\n", "\n"
    )


def test_hosts_without_the_module_get_no_desired_state(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    assert "reconciler" not in compose_of(data, "edge", modules)["services"]


@pytest.mark.parametrize(
    ("service", "kept"),
    [
        ({}, True),
        ({"restart": "unless-stopped"}, True),
        ({"restart": "no"}, False),
        ({"restart": False}, False),
        ({"profiles": ["debug"]}, False),
        ({"deploy": {"replicas": 0}}, False),
        ({"scale": 0}, False),
        ({"deploy": {"replicas": 2}}, True),
    ],
)
def test_kept_running_respects_what_compose_would_not_start(
    service: dict[str, Any], kept: bool
) -> None:
    assert kept_running(service) is kept


def test_declared_compose_projects_are_named_like_compose_names_them(
    data: dict[str, Any], tmp_path: Path
) -> None:
    folder = tmp_path / "Shop"
    folder.mkdir()
    (folder / "compose.yaml").write_text(
        "services:\n  api: {}\n  once: {restart: 'no'}\n  debug: {profiles: [debug]}\n",
        encoding="utf-8",
    )
    (tmp_path / "named.yaml").write_text("name: billing\nservices:\n  worker: {}\n", "utf-8")
    data["hosts"]["central"]["compose"] = [
        "Shop/compose.yaml",
        "named.yaml",
        {"file": "named.yaml", "name": "billing-2"},
        "absent.yaml",
    ]
    from ayeops.manifest import Manifest

    manifest = Manifest(data, tmp_path)
    found = desired_projects(manifest, "central", "acme-central", {"x": {}}, {"exclude": []})
    assert found == {
        "acme-central": ["x"],
        "billing": ["worker"],
        "billing-2": ["worker"],
        "shop": ["api"],
    }


def by_hand(ident: str, name: str, status: str = "running", image: str = "") -> dict[str, Any]:
    return container(ident, "", status, project="", name=name, image=image or "redis:7")


def test_drift_lists_running_containers_of_no_declared_project() -> None:
    docker = FakeDocker(
        container("w", "web"),
        container("g", "grafana", project="acme-central"),
        container("o", "old", project="shop", image="shop/old:2"),
        by_hand("h", "scratch", image="redis:7.4"),
        by_hand("x", "gone", status="exited"),
        container("y", "api", "exited", project="billing"),
    )
    found = agent.drift(docker, settings(declared=["acme-central"]))
    assert found == agent.Drift(
        total=2,
        listed=[
            agent.Stray("scratch", "redis:7.4", ""),
            agent.Stray("shop-old-1", "shop/old:2", "shop"),
        ],
    )
    assert docker.calls == [("GET", "/containers/json", {})]


def test_drift_counts_a_declared_project_without_desired_services_as_declared() -> None:
    docker = FakeDocker(container("w", "web", project="acme-apps"))
    assert agent.drift(docker, settings(projects={}, declared=["acme-apps"])).total == 0
    assert agent.drift(docker, settings(projects={"acme-apps": []})).total == 0
    assert agent.drift(docker, settings(projects={}, declared=[])).total == 1


def test_drift_skips_ignored_names_by_glob_and_case() -> None:
    docker = FakeDocker(
        by_hand("a", "buildx_buildkit_0"),
        by_hand("b", "Buildx_other"),
        by_hand("c", "tmp1"),
        by_hand("d", "tmp12"),
    )
    found = agent.drift(docker, settings(ignore=["buildx_*", "tmp?"]))
    assert [stray.name for stray in found.listed] == ["Buildx_other", "tmp12"]


def test_drift_caps_the_listed_names_and_keeps_the_total() -> None:
    docker = FakeDocker(*(by_hand(f"i{n}", f"stray-{n:02d}") for n in range(5)))
    found = agent.drift(docker, settings(), limit=3)
    assert found.total == 5
    assert [stray.name for stray in found.listed] == ["stray-00", "stray-01", "stray-02"]
    assert agent.DRIFT_LIMIT == 50
    many = FakeDocker(*(by_hand(f"m{n}", f"stray-{n:03d}") for n in range(52)))
    assert len(agent.drift(many, settings()).listed) == 50


def test_drift_survives_an_empty_answer_and_a_container_without_image_or_labels() -> None:
    def empty(method: str, path: str, query: Mapping[str, str]) -> Any:
        return None

    assert agent.drift(empty, settings()) == agent.Drift()
    bare = {"Names": ["/bare"], "State": "running"}
    assert agent.stray_from(bare) == agent.Stray("bare", "", "")


def test_drift_metrics_count_list_and_overflow() -> None:
    found = agent.Drift(
        total=3,
        listed=[agent.Stray("scratch", "redis:7.4", ""), agent.Stray("o-1", "x:1", "shop")],
    )
    assert agent.drift_metrics(found) == [
        "ayeops_undeclared_containers 3\n",
        "ayeops_undeclared_containers_overflow 1\n",
        'ayeops_undeclared_container_info{compose_project="",image="redis:7.4",name="scratch"} 1\n',
        'ayeops_undeclared_container_info{compose_project="shop",image="x:1",name="o-1"} 1\n',
    ]


def test_record_writes_the_drift_into_the_textfile(tmp_path: Path) -> None:
    docker = FakeDocker(container("w", "web"), by_hand("h", "scratch"))
    configured = settings(textfile=tmp_path / "r.prom", state=tmp_path / "state.json")
    clock = Clock()
    agent.record(docker, configured, agent.State(), clock, clock.sleep, [].append, lambda: "b")
    text = (tmp_path / "r.prom").read_text(encoding="utf-8")
    assert "ayeops_undeclared_containers 1\n" in text
    listed = 'ayeops_undeclared_container_info{compose_project="",image="redis:7",name="scratch"} 1'
    assert listed in text


def test_declared_and_ignored_names_come_from_the_environment() -> None:
    parsed = agent.settings_from(
        {"AYE_RECONCILER_DECLARED": '["b","a","b"]', "AYE_RECONCILER_IGNORE": '["tmp-*"]'}
    )
    assert (parsed.declared, parsed.ignore) == (["a", "b"], ["tmp-*"])
    assert agent.parse_names("", "X") == []
    with pytest.raises(ValueError, match=r"^AYE_RECONCILER_IGNORE is not a JSON array$"):
        agent.settings_from({"AYE_RECONCILER_IGNORE": '{"a": 1}'})


def test_the_render_declares_every_compose_project_of_the_host_even_when_excluded(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    data["hosts"]["central"]["modules"]["reconciler"] = {
        "exclude": ["acme-apps/mcp", "acme-apps/web"],
        "ignore": ["buildx_buildkit_*"],
    }
    environment = compose_of(data, "central", modules)["services"]["reconciler"]["environment"]
    parsed = agent.settings_from(environment)
    assert "acme-apps" not in parsed.projects
    assert environment["AYE_RECONCILER_DECLARED"] == '["acme-apps","acme-central"]'
    assert parsed.ignore == ["buildx_buildkit_*"]


def test_host_drift_alerts_after_an_hour_without_paging(modules: dict[str, Module]) -> None:
    spec = modules["reconciler"].spec
    (alert,) = [a for a in spec["alerts"] if a["name"] == "host-drift"]
    assert (alert["for"], alert["severity"], alert["runbook"]) == ("1h", "warning", "host-drift")
    assert "ayeops_undeclared_containers" in alert["expr"]
    assert "host-drift" in spec["services"]["reconciler"]["alerts"]
    assert (ROOT / "docs" / "runbooks" / "host-drift.md").is_file()
