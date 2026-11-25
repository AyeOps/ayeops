from __future__ import annotations

import datetime as dt
import importlib.util
import json
import sys
import threading
import urllib.error
import urllib.request
from collections.abc import Sequence
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import ModuleType
from typing import Any, ClassVar
from zoneinfo import ZoneInfo

import pytest

from tests.conftest import ROOT, WAIT

ADMIN = ROOT / "src" / "ayeops" / "modules" / "objectstore" / "files" / "reststore_admin.py"
UTC = dt.UTC


def load_admin() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "ayeops.modules.objectstore.files.reststore_admin", ADMIN
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


admin = load_admin()


REPO = admin.Repo("edge", 7, 4, 3, "AYE_RESTSTORE_EDGE_RESTIC_PASSWORD")


class FakeRestic:
    def __init__(self, replies: dict[str, Any] | None = None) -> None:
        keep = [{"keep": [{"id": "a"}, {"id": "b"}], "remove": [{"id": "c"}]}]
        self.replies = {
            "unlock": admin.Done(0, "", ""),
            "forget": admin.Done(0, json.dumps(keep), ""),
            "prune": admin.Done(0, "", ""),
            **(replies or {}),
        }
        self.calls: list[tuple[str, str, list[str]]] = []

    def run(self, repo: str, password: str, args: Sequence[str]) -> Any:
        self.calls.append((repo, password, list(args)))
        return self.replies[args[0]]


class Clock:
    def __init__(self, start: float) -> None:
        self.now = start

    def __call__(self) -> float:
        self.now += 1.0
        return self.now


NOW = dt.datetime(2026, 10, 24, 12, 0, tzinfo=UTC).timestamp()


def test_maintain_unlocks_forgets_by_the_clients_policy_and_prunes() -> None:
    restic = FakeRestic()
    outcome = admin.maintain(restic, REPO, Path("/data"), "pw", Clock(NOW))
    location = str(Path("/data") / "edge")
    assert restic.calls == [
        (location, "pw", ["unlock"]),
        (
            location,
            "pw",
            [
                "forget",
                "--json",
                "--host",
                "edge",
                "--group-by",
                "host",
                "--keep-daily",
                "7",
                "--keep-weekly",
                "4",
                "--keep-monthly",
                "3",
            ],
        ),
        (location, "pw", ["prune"]),
    ]
    assert outcome == admin.Outcome(NOW + 1, True, "", NOW + 2, 2, True)


def test_maintain_reports_forget_and_prune_failures_and_retention_drift() -> None:
    failing = admin.Done(1, "", '{"message_type":"exit_error","message":"locked\\nmore"}')
    outcome = admin.maintain(FakeRestic({"forget": failing}), REPO, Path("/d"), "p", Clock(NOW))
    assert outcome == admin.Outcome(NOW + 1, False, "restic forget failed (1): locked")
    restic = FakeRestic({"prune": admin.Done(3, "", "disk full")})
    outcome = admin.maintain(restic, REPO, Path("/d"), "p", Clock(NOW))
    assert outcome == admin.Outcome(
        NOW + 1, False, "restic prune failed (3): disk full", None, 2, True
    )
    groups = [{"keep": [{"id": "a"}]}, {"keep": [{"id": "b"}]}]
    restic = FakeRestic({"forget": admin.Done(0, json.dumps(groups), "")})
    outcome = admin.maintain(restic, REPO, Path("/d"), "p", Clock(NOW))
    assert outcome == admin.Outcome(
        NOW + 1, False, "restic kept 2 snapshots in 2 groups, the policy keeps 14", None, 2, False
    )
    small = admin.Repo("edge", 1, 0, 0, "X")
    keep = [{"keep": [{"id": "a"}, {"id": "b"}]}]
    restic = FakeRestic({"forget": admin.Done(0, json.dumps(keep), "")})
    assert admin.maintain(restic, small, Path("/d"), "p", Clock(NOW)).retention_ok is False
    exact = admin.Repo("edge", 2, 0, 0, "X")
    assert admin.maintain(restic, exact, Path("/d"), "p", Clock(NOW)).ok is True


def test_policy_flags_skip_zero_counts_and_bound_sums_them() -> None:
    assert admin.Repo("x", 0, 2, 0, "X").flags() == ["--keep-weekly", "2"]
    assert admin.Repo("x", 1, 0, 5, "X").flags() == ["--keep-daily", "1", "--keep-monthly", "5"]
    assert REPO.bound() == 14


def test_parse_repos_and_forget() -> None:
    text = json.dumps(
        [
            {
                "name": "edge",
                "keep_daily": 7,
                "keep_weekly": 4,
                "keep_monthly": 3,
                "password_env": "AYE_RESTSTORE_EDGE_RESTIC_PASSWORD",
            }
        ]
    )
    assert admin.parse_repos(text) == (REPO,)
    assert admin.parse_repos("") == ()
    with pytest.raises(ValueError, match="JSON list"):
        admin.parse_repos("{}")
    assert admin.parse_forget("") == (0, 0)
    assert admin.parse_forget('[{"keep": [{"id": "a"}]}, {"keep": null}]') == (2, 1)
    with pytest.raises(ValueError, match="list"):
        admin.parse_forget("{}")


def test_exit_message_prefers_the_json_exit_error_then_the_last_plain_line() -> None:
    assert admin.exit_message("") == ""
    assert admin.exit_message("first\nlast\n\n") == "last"
    assert admin.exit_message('{"message_type":"exit_error","message":"a\\nb"}') == "a"
    assert admin.exit_message('plain\n{"message_type":"status"}') == "plain"
    assert admin.exit_message("plain\n{broken") == "plain"


def test_schedule_and_slots() -> None:
    assert admin.parse_schedule(" 02:05 ") == (2, 5)
    for bad in ("24:00", "12:60", "-1:00"):
        with pytest.raises(ValueError, match="time of day"):
            admin.parse_schedule(bad)
    zone = ZoneInfo("UTC")
    eleven = dt.datetime(2026, 10, 24, 11, 0, tzinfo=UTC).timestamp()
    assert admin.latest_slot(eleven, (11, 0), zone) == eleven
    assert admin.latest_slot(eleven - 1, (11, 0), zone) == eleven - 86400


def test_maintenance_is_due_once_per_slot_and_retries_after_a_failure() -> None:
    slot = NOW - 3600
    assert admin.due(NOW, None, slot, 3600) is True
    done = admin.Outcome(slot + 10, True, "", slot + 20)
    assert admin.due(NOW, done, slot, 3600) is False
    assert admin.due(NOW, admin.Outcome(slot, True, "", slot), slot, 3600) is False
    failed = admin.Outcome(NOW - 3599, False, "x", slot - 86400)
    assert admin.due(NOW, failed, slot, 3600) is False
    assert admin.due(NOW, replace_attempt(failed, NOW - 3600), slot, 3600) is True


def replace_attempt(outcome: Any, attempt: float) -> Any:
    return admin.Outcome(attempt, outcome.ok, outcome.error, outcome.success)


def store(tmp_path: Path, restic: FakeRestic, environ: dict[str, str], **kw: Any) -> Any:
    config = admin.Config(
        repos=(REPO, admin.Repo("db", 1, 0, 0, "AYE_RESTSTORE_DB_RESTIC_PASSWORD")),
        schedule=(11, 0),
        timezone="UTC",
        retry_seconds=3600,
        data=tmp_path,
        state_path=tmp_path / "state.json",
        restic="restic",
        server="http://127.0.0.1:1",
        metrics_password="m",
        quota_bytes=1000,
        port=0,
    )
    return admin.Store(config, restic, environ, clock=Clock(NOW), **kw)


def test_tick_skips_repositories_that_do_not_exist_yet_and_names_a_missing_password(
    tmp_path: Path,
) -> None:
    (tmp_path / "edge").mkdir()
    (tmp_path / "edge" / "config").write_bytes(b"x" * 10)
    (tmp_path / "db").mkdir()
    restic = FakeRestic()
    run = store(tmp_path, restic, {})
    run.tick()
    assert set(run.outcomes) == {"edge"}
    assert run.outcomes["edge"].error == "AYE_RESTSTORE_EDGE_RESTIC_PASSWORD is not set"
    assert restic.calls == []
    assert run.sizes == {"edge": 10, "db": None}
    assert run.data_bytes == 10


def test_tick_runs_each_due_repository_once_and_keeps_the_last_success_on_failure(
    tmp_path: Path,
) -> None:
    (tmp_path / "edge").mkdir()
    (tmp_path / "edge" / "config").write_text("c")
    restic = FakeRestic()
    run = store(tmp_path, restic, {"AYE_RESTSTORE_EDGE_RESTIC_PASSWORD": "secret"})
    run.tick()
    assert run.outcomes["edge"].ok is True
    assert {call[1] for call in restic.calls} == {"secret"}
    first = run.outcomes["edge"].success
    calls = len(restic.calls)
    run.tick()
    assert len(restic.calls) == calls
    run.clock = Clock(NOW + 86400)
    restic.replies["prune"] = admin.Done(1, "", "boom")
    run.tick()
    assert run.outcomes["edge"].ok is False
    assert run.outcomes["edge"].success == first


def test_tick_turns_a_crash_into_a_failed_outcome(tmp_path: Path) -> None:
    (tmp_path / "edge").mkdir()
    (tmp_path / "edge" / "config").write_text("c")
    restic = FakeRestic({"forget": admin.Done(0, "{not json", "")})
    run = store(tmp_path, restic, {"AYE_RESTSTORE_EDGE_RESTIC_PASSWORD": "s"})
    run.tick()
    assert run.outcomes["edge"].ok is False
    assert run.outcomes["edge"].error


def test_sizes_are_measured_at_most_every_five_minutes(tmp_path: Path) -> None:
    measured: list[Path] = []

    def measure(path: Path) -> int:
        measured.append(path)
        return 1

    run = store(tmp_path, FakeRestic(), {}, measure=measure)
    run.tick()
    count = len(measured)
    run.clock = Clock(NOW + 200)
    run.tick()
    assert len(measured) == count
    run.clock = Clock(NOW + 400)
    run.tick()
    assert len(measured) == 2 * count


def test_metrics_carry_the_repo_label_and_appear_only_after_an_attempt() -> None:
    db = admin.Repo("db", 1, 0, 0, "X")
    outcome = admin.Outcome(100.0, True, "", 101.5, 3, True)
    text = admin.metrics_text(
        [REPO, db], {"edge": outcome}, {"edge": 50, "db": None}, 60, 1000, True
    )
    assert text == (
        "ayeops_reststore_server_up 1\n"
        "ayeops_reststore_data_bytes 60\n"
        "ayeops_reststore_quota_bytes 1000\n"
        'ayeops_reststore_repo_initialized{repo="edge"} 1\n'
        'ayeops_reststore_repo_bytes{repo="edge"} 50\n'
        'ayeops_reststore_maintenance_ok{repo="edge"} 1\n'
        'ayeops_reststore_maintenance_last_success_timestamp_seconds{repo="edge"} 101.500\n'
        'ayeops_reststore_maintenance_last_attempt_timestamp_seconds{repo="edge"} 100\n'
        'ayeops_reststore_snapshots{repo="edge"} 3\n'
        'ayeops_reststore_retention_ok{repo="edge"} 1\n'
        'ayeops_reststore_repo_initialized{repo="db"} 0\n'
    )
    failed = admin.Outcome(100.0, False, "x")
    text = admin.metrics_text([REPO], {"edge": failed}, {}, None, None, False)
    assert text == (
        "ayeops_reststore_server_up 0\n"
        'ayeops_reststore_repo_initialized{repo="edge"} 0\n'
        'ayeops_reststore_maintenance_ok{repo="edge"} 0\n'
        'ayeops_reststore_maintenance_last_success_timestamp_seconds{repo="edge"} 0\n'
        'ayeops_reststore_maintenance_last_attempt_timestamp_seconds{repo="edge"} 100\n'
    )
    drift = admin.Outcome(1.0, False, "x", None, 2, False)
    assert 'retention_ok{repo="edge"} 0' in admin.metrics_text([REPO], {"edge": drift}, {}, 0, 0, 1)
    assert admin.label('a"b\\c') == 'a\\"b\\\\c'


def test_tree_bytes_sums_every_file_below_the_path(tmp_path: Path) -> None:
    (tmp_path / "a" / "b").mkdir(parents=True)
    (tmp_path / "a" / "b" / "x").write_bytes(b"12345")
    (tmp_path / "y").write_bytes(b"123")
    assert admin.tree_bytes(tmp_path) == 8
    assert admin.tree_bytes(tmp_path / "missing") == 0
    assert admin.has_config(tmp_path) is False
    (tmp_path / "config").write_text("c")
    assert admin.has_config(tmp_path) is True


def test_outcomes_survive_a_restart_and_a_broken_state_starts_empty(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    outcomes = {"edge": admin.Outcome(1.0, True, "", 2.0, 3, True)}
    admin.save_outcomes(path, outcomes)
    assert admin.load_outcomes(path) == outcomes
    path.write_text("{broken")
    assert admin.load_outcomes(path) == {}
    path.write_text('{"edge": {"unknown": 1}}')
    assert admin.load_outcomes(path) == {}
    assert admin.load_outcomes(tmp_path / "missing.json") == {}


def test_config_reads_every_variable_and_defaults_the_rest() -> None:
    loaded = admin.load_config({"AYE_RESTSTORE_METRICS_PASSWORD": "m"})
    assert loaded.repos == ()
    assert (loaded.schedule, loaded.timezone, loaded.retry_seconds) == ((11, 0), "UTC", 3600)
    assert (loaded.data, loaded.state_path) == (Path("/data"), Path("/state/state.json"))
    assert (loaded.restic, loaded.server) == ("/opt/restic/restic", "http://rest-server:8000")
    assert (loaded.quota_bytes, loaded.port, loaded.metrics_password) == (None, 9133, "m")
    full = admin.load_config(
        {
            "AYE_RESTSTORE_METRICS_PASSWORD": "m",
            "AYE_RESTSTORE_SCHEDULE": "02:30",
            "AYE_RESTSTORE_RETRY_MINUTES": "5",
            "AYE_RESTSTORE_QUOTA_BYTES": "2048",
            "AYE_RESTSTORE_ADMIN_PORT": "1234",
        }
    )
    assert (full.schedule, full.retry_seconds, full.quota_bytes, full.port) == (
        (2, 30),
        300,
        2048,
        1234,
    )


def test_restic_gets_only_its_own_repository_password_not_the_whole_environment() -> None:
    environ = {"PATH": "/bin", "HOME": "/tmp", "AYE_RESTSTORE_DB_RESTIC_PASSWORD": "other"}
    base = admin.restic_env(environ)
    assert base == {"PATH": "/bin", "HOME": "/tmp"}
    process = admin.Process(sys.executable, base)
    script = "import json, os; print(json.dumps(dict(os.environ)))"
    done = process.run("/data/edge", "secret", ["-c", script])
    env = json.loads(done.stdout)
    assert env["RESTIC_REPOSITORY"] == "/data/edge"
    assert env["RESTIC_PASSWORD"] == "secret"
    assert "AYE_RESTSTORE_DB_RESTIC_PASSWORD" not in env
    missing = admin.Process(str(Path("/nonexistent/restic")), base).run("r", "p", ["x"])
    assert missing.code == 127
    slow = admin.Process(sys.executable, base, timeout=1)
    late = slow.run("r", "p", ["-c", f"import time; time.sleep({WAIT})"])
    assert (late.code, late.stderr) == (124, "restic -c timed out after 1s")


class Upstream(BaseHTTPRequestHandler):
    seen: ClassVar[list[str | None]] = []

    def do_GET(self) -> None:
        Upstream.seen.append(self.headers.get("Authorization"))
        if self.headers.get("Authorization") != admin.basic_auth("metrics", "m"):
            self.send_error(401)
            return
        body = b"rest_server_blob_write_bytes_total 7\n"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        return


def test_the_server_metrics_are_fetched_as_the_metrics_user_and_appended() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}"
        assert admin.server_metrics(url, "m") == "rest_server_blob_write_bytes_total 7\n"
        assert admin.server_metrics(url, "wrong") is None
        assert Upstream.seen[0] == "Basic bWV0cmljczpt"
        run = admin.Store(
            admin.load_config({"AYE_RESTSTORE_METRICS_PASSWORD": "m", "AYE_RESTSTORE_SERVER": url}),
            FakeRestic(),
            {},
        )
        text = admin.exposition(run)
        assert text.startswith("ayeops_reststore_server_up 1\n")
        assert text.endswith("rest_server_blob_write_bytes_total 7\n")
    finally:
        server.shutdown()
    assert admin.server_metrics("http://127.0.0.1:1", "m") is None


def test_the_admin_serves_health_and_metrics_over_http() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), admin.handler(lambda: "x 1\n"))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        with urllib.request.urlopen(f"{base}/metrics", timeout=WAIT) as response:
            assert response.read() == b"x 1\n"
        with urllib.request.urlopen(f"{base}/health", timeout=WAIT) as response:
            assert response.read() == b"ok\n"
        with pytest.raises(urllib.error.HTTPError):
            urllib.request.urlopen(f"{base}/other", timeout=WAIT)
    finally:
        server.shutdown()


def test_a_skipped_repository_does_not_stop_the_ones_after_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "db").mkdir()
    (tmp_path / "db" / "config").write_text("c")
    restic = FakeRestic()
    run = store(tmp_path, restic, {"AYE_RESTSTORE_DB_RESTIC_PASSWORD": "d"})
    run.tick()
    assert set(run.outcomes) == {"db"}
    assert run.outcomes["db"].ok is False
    assert "reststore-admin: db: restic kept 2 snapshots" in capsys.readouterr().err
    (tmp_path / "edge").mkdir()
    (tmp_path / "edge" / "config").write_text("c")
    run.outcomes["edge"] = admin.Outcome(NOW, True, "", NOW + 5)
    run.outcomes["db"] = admin.Outcome(NOW - 7200, False, "x")
    run.environ = {"AYE_RESTSTORE_DB_RESTIC_PASSWORD": "d"}
    restic.calls.clear()
    run.tick()
    assert {call[0] for call in restic.calls} == {str(tmp_path / "db")}


def test_a_missing_password_is_a_failure_and_a_pass_is_logged(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "edge").mkdir()
    (tmp_path / "edge" / "config").write_text("c")
    run = store(tmp_path, FakeRestic(), {})
    run.tick()
    assert run.outcomes["edge"].ok is False
    keep = [{"keep": [{"id": "a"}]}]
    run = store(
        tmp_path,
        FakeRestic({"forget": admin.Done(0, json.dumps(keep), "")}),
        {"AYE_RESTSTORE_EDGE_RESTIC_PASSWORD": "s"},
    )
    run.tick()
    assert "reststore-admin: edge: forget and prune passed\n" in capsys.readouterr().err


def test_sizes_are_measured_again_exactly_five_minutes_later(tmp_path: Path) -> None:
    calls: list[Path] = []

    def measure(path: Path) -> int:
        calls.append(path)
        return 0

    run = store(tmp_path, FakeRestic(), {}, measure=measure)
    run.measured = NOW
    run.clock = Clock(NOW + admin.SIZE_SECONDS - 1)
    run.tick()
    assert calls == [tmp_path]
    assert run.measured == NOW + admin.SIZE_SECONDS


def test_metrics_continue_past_a_repository_without_an_outcome() -> None:
    db = admin.Repo("db", 1, 0, 0, "X")
    outcome = admin.Outcome(1.0, True, "", 2.0)
    text = admin.metrics_text([db, REPO], {"edge": outcome}, {}, None, None, True)
    assert 'ayeops_reststore_maintenance_ok{repo="edge"} 1' in text


def test_schedule_accepts_the_edges_of_the_day_and_errors_are_exact() -> None:
    assert admin.parse_schedule("00:00") == (0, 0)
    assert admin.parse_schedule("23:59") == (23, 59)
    with pytest.raises(ValueError, match=r"^not a time of day: '24:00'$"):
        admin.parse_schedule("24:00")
    with pytest.raises(ValueError, match=r"^AYE_RESTSTORE_REPOS is not a JSON list$"):
        admin.parse_repos("{}")
    with pytest.raises(ValueError, match=r"^restic forget --json did not return a list$"):
        admin.parse_forget("{}")
    assert admin.exit_message('{"message_type":"exit_error"}') == ""
    assert admin.exit_message('{"message_type":"exit_error","message":""}') == ""


def test_value_types_are_frozen_and_an_outcome_has_no_error_by_default() -> None:
    assert admin.Outcome(1.0, True).error == ""
    for value in (REPO, admin.Outcome(1.0, True), admin.Done(0, "", ""), admin.Process("r", {})):
        with pytest.raises(AttributeError):
            value.code = 1
    config = admin.load_config({"AYE_RESTSTORE_METRICS_PASSWORD": "m"})
    with pytest.raises(AttributeError):
        config.port = 1


def test_state_is_written_with_sorted_keys(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    admin.save_outcomes(path, {"edge": admin.Outcome(1.0, True, "", 2.0, 3, True)})
    assert path.read_text(encoding="utf-8") == (
        '{"edge": {"attempt": 1.0, "error": "", "ok": true, "retention_ok": true,'
        ' "snapshots": 3, "success": 2.0}}'
    )


def test_config_reads_the_paths_and_names_from_their_variables() -> None:
    loaded = admin.load_config(
        {
            "AYE_RESTSTORE_METRICS_PASSWORD": "m",
            "AYE_RESTSTORE_REPOS": json.dumps(
                [
                    {
                        "name": "x",
                        "keep_daily": 1,
                        "keep_weekly": 0,
                        "keep_monthly": 0,
                        "password_env": "P",
                    }
                ]
            ),
            "AYE_RESTSTORE_TIMEZONE": "Europe/Kyiv",
            "AYE_RESTSTORE_DATA": "/srv/data",
            "AYE_RESTSTORE_STATE": "/srv/state.json",
            "AYE_RESTIC": "/bin/restic",
        }
    )
    assert [repo.name for repo in loaded.repos] == ["x"]
    assert loaded.timezone == "Europe/Kyiv"
    assert (loaded.data, loaded.state_path) == (Path("/srv/data"), Path("/srv/state.json"))
    assert loaded.restic == "/bin/restic"


def test_restic_output_is_text_and_failures_carry_no_stdout() -> None:
    base = admin.restic_env({"PATH": "/bin", "TZ": "UTC", "OTHER": "x"})
    assert base == {"PATH": "/bin", "TZ": "UTC"}
    done = admin.Process(sys.executable, base).run("r", "p", ["-c", "print('hi')"])
    assert isinstance(done.stdout, str) and done.stdout.strip() == "hi"
    missing = admin.Process(str(Path("/nonexistent/restic")), base).run("r", "p", ["x"])
    assert missing.stdout == "" and missing.stderr.startswith("cannot run")
    late = admin.Process(sys.executable, base, timeout=1).run(
        "r", "p", ["-c", f"import time; time.sleep({WAIT})"]
    )
    assert late.stdout == ""


class Garbled(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        body = b"x 1 \xff\n"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        return


def test_garbled_server_metrics_are_replaced_not_fatal_and_silence_adds_nothing() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Garbled)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}"
        assert admin.server_metrics(url, "m") == "x 1 �\n"
    finally:
        server.shutdown()
    run = admin.Store(
        admin.load_config(
            {"AYE_RESTSTORE_METRICS_PASSWORD": "m", "AYE_RESTSTORE_SERVER": "http://127.0.0.1:1"}
        ),
        FakeRestic(),
        {},
    )
    assert admin.exposition(run) == "ayeops_reststore_server_up 0\n"


def test_the_admin_answers_404_and_names_the_exposition_format() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), admin.handler(lambda: "x 1\n"))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        with urllib.request.urlopen(f"{base}/metrics", timeout=WAIT) as response:
            assert response.headers["Content-Type"] == "text/plain; version=0.0.4"
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(f"{base}/other", timeout=WAIT)
        assert error.value.code == 404
    finally:
        server.shutdown()
