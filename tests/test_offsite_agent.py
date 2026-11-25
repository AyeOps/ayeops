from __future__ import annotations

import datetime as dt
import hashlib
import importlib.util
import json
import os
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path
from types import ModuleType
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from tests.conftest import ROOT, WAIT

AGENT = ROOT / "src" / "ayeops" / "modules" / "offsite" / "files" / "offsite_agent.py"
FIXTURES = ROOT / "tests" / "fixtures" / "restic"
KYIV = ZoneInfo("Europe/Kyiv")
UTC = dt.UTC
DAY = 86400


def load_agent() -> ModuleType:
    spec = importlib.util.spec_from_file_location("offsite_agent", AGENT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["offsite_agent"] = module
    spec.loader.exec_module(module)
    return module


agent = load_agent()


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def at(text: str, zone: dt.tzinfo = KYIV) -> float:
    return dt.datetime.fromisoformat(text).replace(tzinfo=zone).timestamp()


def snap(ident: str, when: str, tags: Sequence[str] = ()) -> Any:
    moment = dt.datetime.fromisoformat(when)
    return agent.Snapshot(ident, moment, ("/var/backups/acme/a.dump",), tuple(tags))


POLICY = agent.Policy(7, 4, 3)
DUMP = b"PGDMP" + b"\x01\x0e\x00" + b"x" * 5000
DUMP_SHA = hashlib.sha256(DUMP).hexdigest()


def snapshot_json(
    ident: str, when: str, sha: str | None = DUMP_SHA, path: str = ""
) -> dict[str, Any]:
    tags = ["ayeops"] + ([f"sha256={sha}"] if sha else [])
    return {
        "id": ident,
        "time": when,
        "paths": [path or "/var/backups/acme/acme-2.dump"],
        "hostname": "edge",
        "tags": tags,
    }


class FakeRestic:
    def __init__(self, replies: dict[str, Any] | None = None, dump: bytes = DUMP) -> None:
        self.replies = {
            "snapshots": agent.Done(0, "[]", ""),
            "unlock": agent.Done(0, "", ""),
            "backup": agent.Done(0, fixture("backup.jsonl"), ""),
            "forget": agent.Done(0, fixture("forget.json"), ""),
            "prune": agent.Done(0, "", ""),
            "stats": agent.Done(0, fixture("stats.json"), ""),
            "check": agent.Done(0, "no errors were found", ""),
            "dump": agent.Done(0, "", ""),
            **(replies or {}),
        }
        self.dump = dump
        self.chunk = 1000
        self.calls: list[list[str]] = []

    def reply(self, args: Sequence[str]) -> Any:
        value = self.replies[args[0]]
        return value(list(args)) if callable(value) else value

    def run(self, args: Sequence[str]) -> Any:
        self.calls.append(list(args))
        return self.reply(args)

    def stream(self, args: Sequence[str], sink: Callable[[bytes], None]) -> Any:
        self.calls.append(list(args))
        done = self.reply(args)
        if done.code == 0:
            for start in range(0, len(self.dump), self.chunk):
                sink(self.dump[start : start + self.chunk])
        return done

    def verbs(self) -> list[str]:
        return [call[0] for call in self.calls]

    def call(self, verb: str) -> list[str]:
        return next(call for call in self.calls if call[0] == verb)


def config(**changes: Any) -> Any:
    base = agent.Config(
        source="/var/backups/acme/*.dump",
        host="edge",
        schedule=(5, 30),
        timezone="Europe/Kyiv",
        policy=POLICY,
        restore_every_days=7,
        settle_seconds=900,
        retry_seconds=3600,
        verify="pg_dump",
        quota_bytes=40 * 1024**3,
        state_path=Path("state.json"),
        textfile=Path("offsite.prom"),
        restic="/opt/restic/restic",
    )
    return replace(base, **changes)


NOW = at("2026-10-24T06:00:00")


class Clock:
    def __init__(self, start: float = NOW, step: float = 10.0) -> None:
        self.now = start
        self.step = step

    def __call__(self) -> float:
        self.now += self.step
        return self.now


def make_pass(
    restic: FakeRestic,
    files: list[tuple[str, float]] | None = None,
    stats: list[tuple[int, float]] | None = None,
    **changes: Any,
) -> Any:
    listed = files if files is not None else [("/var/backups/acme/acme-2.dump", NOW - 7200)]
    stamps = list(stats or [])

    def stat(path: str) -> tuple[int, float]:
        return stamps.pop(0) if stamps else (len(DUMP), NOW - 7200)

    return agent.Pass(
        config(**changes),
        restic,
        clock=Clock(),
        files=lambda pattern: list(listed),
        digest=lambda path: (DUMP_SHA, len(DUMP)),
        stat=stat,
    )


def test_snapshots_parse_from_real_restic_output_with_offsets_paths_and_sha_tags() -> None:
    snapshots = agent.parse_snapshots(fixture("snapshots.json"))
    assert [s.id[:8] for s in snapshots] == ["c784ce44", "0c2d4d92"]
    first = snapshots[0]
    assert first.time == dt.datetime(
        2026, 10, 24, 1, 1, 51, 975427, tzinfo=dt.timezone(dt.timedelta(hours=3))
    )
    assert first.paths == ("/var/backups/acme/acme-1.dump",)
    assert first.sha256 == "9ad88a21c762b05a467e1c50558d35810cb3aca8a00ae8a946143a2ec0003851"
    assert snap("a", "2026-01-01T00:00:00+00:00", ["ayeops"]).sha256 is None


def test_snapshots_reject_anything_but_a_list_and_times_without_an_offset() -> None:
    with pytest.raises(ValueError, match="list"):
        agent.parse_snapshots('{"id": "x"}')
    with pytest.raises(ValueError, match="offset"):
        agent.parse_time("2026-10-24T05:30:00")
    assert agent.parse_time("2026-10-24T05:30:00Z").utcoffset() == dt.timedelta(0)
    assert agent.parse_snapshots("[]") == []


def test_snapshot_without_tags_or_paths_parses_to_empty_tuples() -> None:
    parsed = agent.parse_snapshot({"id": "abc", "time": "2026-01-01T00:00:00+02:00"})
    assert (parsed.paths, parsed.tags, parsed.sha256) == ((), (), None)


def test_backup_summary_prefers_the_packed_bytes_restic_actually_stored() -> None:
    summary = agent.parse_summary(fixture("backup.jsonl"))
    assert summary.snapshot_id.startswith("c784ce44")
    assert summary.added_bytes == 406863
    assert summary.processed_bytes == 405268
    assert summary.duration == pytest.approx(0.7513232)


def test_backup_summary_is_found_after_status_lines_and_falls_back_to_data_added() -> None:
    lines = [
        '{"message_type":"status","percent_done":0.5}',
        "not json at all",
        '{"message_type":"summary","data_added":42,"total_bytes_processed":7,'
        '"total_duration":1.5,"snapshot_id":"abc"}',
        '{"message_type":"verbose_status","action":"new"}',
    ]
    summary = agent.parse_summary("\n".join(lines))
    assert (summary.snapshot_id, summary.added_bytes, summary.processed_bytes) == ("abc", 42, 7)
    assert summary.duration == 1.5


def test_backup_without_a_summary_line_is_an_error() -> None:
    with pytest.raises(ValueError, match="summary"):
        agent.parse_summary('{"message_type":"status"}\n')
    defaults = agent.parse_summary('{"message_type":"summary"}')
    assert (defaults.snapshot_id, defaults.added_bytes, defaults.duration) == ("", 0, 0.0)


def test_stats_read_the_raw_data_size_of_the_repository() -> None:
    assert agent.parse_stats(fixture("stats.json")) == 813403


def test_forget_grouped_by_host_is_one_group_keeping_both_snapshots() -> None:
    result = agent.parse_forget(fixture("forget.json"))
    assert result.groups == 1
    assert [ident[:8] for ident in result.keep] == ["0c2d4d92", "c784ce44"]
    assert result.remove == ()


def test_forget_with_the_default_grouping_splits_daily_file_names_into_groups() -> None:
    result = agent.parse_forget(fixture("forget-paths.json"))
    assert result.groups == 2
    assert len(result.keep) == 2


def test_forget_reads_removed_snapshots_and_tolerates_empty_output() -> None:
    text = json.dumps([{"keep": [{"id": "a"}], "remove": [{"id": "b"}, {"id": "c"}]}])
    assert agent.parse_forget(text) == agent.Forget(1, ("a",), ("b", "c"))
    assert agent.parse_forget("  \n") == agent.Forget(0, (), ())
    with pytest.raises(ValueError, match="list"):
        agent.parse_forget('{"keep": []}')


def test_exit_message_reads_the_json_exit_error_and_plain_stderr() -> None:
    assert agent.exit_message(fixture("missing.err")).startswith("Fatal: repository does not exist")
    assert "\n" not in agent.exit_message(fixture("missing.err"))
    assert agent.exit_message(fixture("wrong.err")) == "Fatal: wrong password or no key found"
    assert agent.exit_message("first\nFatal: plain text\n\n") == "Fatal: plain text"
    assert agent.exit_message('{"message_type":"status"}\n{broken') == ""
    assert agent.exit_message("") == ""


def test_policy_flags_skip_zero_buckets_and_bound_sums_them() -> None:
    assert POLICY.flags() == ["--keep-daily", "7", "--keep-weekly", "4", "--keep-monthly", "3"]
    assert agent.Policy(0, 52, 0).flags() == ["--keep-weekly", "52"]
    assert agent.Policy(0, 0, 0).flags() == []
    assert POLICY.bound() == 14


def test_bucket_keys_use_calendar_day_iso_week_and_month() -> None:
    moment = dt.datetime(2027, 1, 1, 0, 10, tzinfo=KYIV)
    assert agent.day_key(moment) == 20270101
    assert agent.week_key(moment) == 202653
    assert agent.week_key(dt.datetime(2027, 1, 4, tzinfo=KYIV)) == 202701
    assert agent.month_key(moment) == 202701


def daily(count: int, start: str = "2026-07-01T05:30:00+03:00") -> list[Any]:
    first = dt.datetime.fromisoformat(start)
    return [
        agent.Snapshot(f"s{day:03d}", first + dt.timedelta(days=day), (), ())
        for day in range(count)
    ]


def test_a_year_of_daily_snapshots_keeps_seven_days_two_more_weeks_and_two_more_months() -> None:
    snapshots = daily(365)
    keep = agent.kept(snapshots, POLICY)
    days = {f"s{n}" for n in range(358, 365)}
    assert keep == days | {"s354", "s347", "s334", "s303"}
    assert len(keep) <= POLICY.bound()


def test_too_few_snapshots_keeps_them_all_including_the_oldest() -> None:
    snapshots = daily(3)
    assert agent.kept(snapshots, POLICY) == {"s000", "s001", "s002"}
    same_day = [snap("new", "2026-10-24T05:30:00+03:00"), snap("old", "2026-10-24T01:00:00+03:00")]
    assert agent.kept(same_day, agent.Policy(7, 0, 0)) == {"new", "old"}
    assert agent.kept(same_day, agent.Policy(0, 0, 0)) == set()
    assert agent.kept([], POLICY) == set()


def test_full_buckets_drop_the_older_snapshot_of_the_same_day() -> None:
    snapshots = [
        snap("d3", "2026-10-24T05:30:00+03:00"),
        snap("d3-early", "2026-10-24T01:00:00+03:00"),
        snap("d2", "2026-10-23T05:30:00+03:00"),
        snap("d1", "2026-10-22T05:30:00+03:00"),
    ]
    assert agent.kept(snapshots, agent.Policy(2, 0, 0)) == {"d3", "d2"}
    assert agent.kept(snapshots, agent.Policy(3, 0, 0)) == {"d3", "d2", "d1"}


def oracle_cases() -> list[str]:
    return sorted(path.name for path in FIXTURES.glob("oracle-forget-*.json"))


@pytest.mark.parametrize("name", oracle_cases())
def test_kept_matches_what_restic_forget_kept_on_a_real_repository(name: str) -> None:
    snapshots = agent.parse_snapshots(fixture("oracle-snapshots.json"))
    daily_count, weekly_count, monthly_count = (int(n) for n in name[14:-5].split("-"))
    policy = agent.Policy(daily_count, weekly_count, monthly_count)
    restic = agent.parse_forget(fixture(name))
    assert restic.groups == 1
    assert agent.kept(snapshots, policy) == set(restic.keep)
    assert len(restic.keep) + len(restic.remove) == len(snapshots)


def test_latest_slot_is_today_after_the_slot_and_yesterday_before_it() -> None:
    assert agent.latest_slot(at("2026-10-24T06:00:00"), (5, 30), KYIV) == at("2026-10-24T05:30:00")
    assert agent.latest_slot(at("2026-10-24T05:29:59"), (5, 30), KYIV) == at("2026-10-23T05:30:00")
    assert agent.latest_slot(at("2026-10-24T05:30:00"), (5, 30), KYIV) == at("2026-10-24T05:30:00")


def test_latest_slot_follows_local_time_across_the_autumn_clock_change() -> None:
    after = agent.latest_slot(at("2026-10-25T12:00:00"), (5, 30), KYIV)
    assert after == dt.datetime(2026, 10, 25, 3, 30, tzinfo=UTC).timestamp()
    before = agent.latest_slot(at("2026-10-24T12:00:00"), (5, 30), KYIV)
    assert after - before == DAY + 3600


def test_backup_is_due_once_per_slot_and_retries_after_a_failure() -> None:
    state = agent.State()
    assert agent.backup_due(NOW, state, (5, 30), KYIV, 3600)
    done = agent.State(last_attempt=NOW - 600, last_success=at("2026-10-24T05:31:00"))
    assert not agent.backup_due(NOW, done, (5, 30), KYIV, 3600)
    yesterday = agent.State(last_attempt=NOW - DAY, last_success=NOW - DAY)
    assert agent.backup_due(NOW, yesterday, (5, 30), KYIV, 3600)
    failed = agent.State(last_attempt=NOW - 600, last_success=NOW - DAY)
    assert not agent.backup_due(NOW, failed, (5, 30), KYIV, 3600)
    assert agent.backup_due(NOW, failed, (5, 30), KYIV, 600)
    at_slot = agent.State(last_attempt=NOW, last_success=at("2026-10-24T05:30:00"))
    assert not agent.backup_due(NOW, at_slot, (5, 30), KYIV, 0)


def test_restore_test_waits_for_a_backup_then_runs_every_n_days_with_retry() -> None:
    assert not agent.restore_due(NOW, agent.State(), 7, 3600)
    backed = agent.State(last_success=NOW - 60)
    assert agent.restore_due(NOW, backed, 7, 3600)
    recent = replace(backed, restore_success=NOW - 6 * DAY, restore_attempt=NOW - 6 * DAY)
    assert not agent.restore_due(NOW, recent, 7, 3600)
    week = replace(backed, restore_success=NOW - 7 * DAY, restore_attempt=NOW - 7 * DAY)
    assert agent.restore_due(NOW, week, 7, 3600)
    failing = replace(backed, restore_success=NOW - 9 * DAY, restore_attempt=NOW - 600)
    assert not agent.restore_due(NOW, failing, 7, 3600)
    assert agent.restore_due(NOW, failing, 7, 600)


def test_newest_settled_file_wins_and_files_still_being_written_wait() -> None:
    entries = [("a.fc", NOW - 3 * DAY), ("c.fc", NOW - 100), ("b.fc", NOW - 7200)]
    assert agent.newest(entries, NOW, 900) == "b.fc"
    assert agent.newest(entries, NOW, 100) == "c.fc"
    assert agent.newest(entries, NOW, 101) == "b.fc"
    assert agent.newest([], NOW, 0) is None
    assert agent.newest([("a.fc", NOW)], NOW, 1) is None
    assert agent.newest([("b.fc", NOW - 5), ("a.fc", NOW - 5)], NOW, 0) == "b.fc"


def test_matching_files_lists_regular_files_only(tmp_path: Path) -> None:
    (tmp_path / "one.fc").write_bytes(b"PGDMP")
    (tmp_path / "two.fc").mkdir()
    (tmp_path / "three.txt").write_bytes(b"x")
    found = agent.matching_files(str(tmp_path / "*.fc"))
    assert [Path(name).name for name, _ in found] == ["one.fc"]
    assert found[0][1] == pytest.approx((tmp_path / "one.fc").stat().st_mtime)


def test_verdict_demands_the_recorded_sha_a_non_empty_file_and_the_pg_dump_header() -> None:
    sha = "a" * 64
    assert agent.verdict(sha, sha, 10, b"PGDMP", "pg_dump") == ""
    assert "no sha256= tag" in agent.verdict(None, sha, 10, b"PGDMP", "pg_dump")
    assert "empty" in agent.verdict(sha, sha, 0, b"", "pg_dump")
    assert "differs" in agent.verdict(sha, "b" * 64, 10, b"PGDMP", "pg_dump")
    assert "PGDMP" in agent.verdict(sha, sha, 10, b"PK\x03\x04", "pg_dump")
    assert agent.verdict(sha, sha, 10, b"PK\x03\x04", "none") == ""


def test_metrics_of_a_fresh_agent_are_empty_but_the_quota() -> None:
    assert agent.metrics_text(agent.State(), None) == ""
    assert agent.metrics_text(agent.State(), 1024) == "ayeops_offsite_quota_bytes 1024\n"
    assert agent.metrics_text(agent.State(), None, 93600) == (
        "ayeops_offsite_max_age_seconds 93600\n"
    )


def test_an_attempt_that_never_succeeded_reports_zero_so_staleness_fires() -> None:
    text = agent.metrics_text(agent.State(last_attempt=100.0, last_ok=False), None)
    assert "ayeops_offsite_last_run_success 0\n" in text
    assert "ayeops_offsite_last_success_timestamp_seconds 0\n" in text
    failed = agent.State(restore_attempt=50.0, restore_ok=False)
    restore = agent.metrics_text(failed, None)
    assert "ayeops_offsite_restore_test_ok 0\n" in restore
    assert "ayeops_offsite_restore_test_last_success_timestamp_seconds 0\n" in restore
    assert "ayeops_offsite_restore_test_timestamp_seconds 50\n" in restore


def test_metrics_of_a_working_agent_cover_every_documented_series() -> None:
    state = agent.State(
        last_attempt=1000.0,
        last_success=1100.5,
        last_ok=True,
        last_source_bytes=2_800_000_000,
        last_added_bytes=123,
        last_duration=100.25,
        snapshots=12,
        repo_bytes=9_000_000_000,
        retention_ok=True,
        repository_ok=True,
        restore_attempt=2000.0,
        restore_success=2100.0,
        restore_ok=True,
    )
    lines = agent.metrics_text(state, 42).splitlines()
    assert lines == [
        "ayeops_offsite_last_run_success 1",
        "ayeops_offsite_last_success_timestamp_seconds 1100.500",
        "ayeops_offsite_last_attempt_timestamp_seconds 1000",
        "ayeops_offsite_last_source_bytes 2800000000",
        "ayeops_offsite_last_added_bytes 123",
        "ayeops_offsite_last_duration_seconds 100.250",
        "ayeops_offsite_snapshots 12",
        "ayeops_offsite_repo_size_bytes 9000000000",
        "ayeops_offsite_quota_bytes 42",
        "ayeops_offsite_retention_ok 1",
        "ayeops_offsite_repository_ok 1",
        "ayeops_offsite_restore_test_timestamp_seconds 2000",
        "ayeops_offsite_restore_test_last_success_timestamp_seconds 2100",
        "ayeops_offsite_restore_test_ok 1",
    ]
    broken = replace(state, retention_ok=False, repository_ok=False, snapshots=0)
    text = agent.metrics_text(broken, None)
    assert "ayeops_offsite_retention_ok 0\n" in text
    assert "ayeops_offsite_repository_ok 0\n" in text
    assert "ayeops_offsite_snapshots 0\n" in text
    assert "quota" not in text


def test_numbers_print_integers_plain_and_fractions_with_three_digits() -> None:
    assert agent.number(5) == "5"
    assert agent.number(5.0) == "5"
    assert agent.number(1.23456) == "1.235"
    assert agent.flag(True) == 1
    assert agent.flag(False) == 0


def test_state_survives_a_round_trip_and_garbage_or_unknown_keys(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    assert agent.load_state(path) == agent.State()
    state = agent.State(last_attempt=1.0, last_ok=True, last_file="/x", snapshots=3)
    agent.save_state(path, state)
    assert agent.load_state(path) == state
    assert not path.with_suffix(".tmp").exists()
    path.write_text('{"snapshots": 4, "removed_field": 1}', encoding="utf-8")
    assert agent.load_state(path) == agent.State(snapshots=4)
    path.write_text("{broken", encoding="utf-8")
    assert agent.load_state(path) == agent.State()


def test_metrics_file_is_replaced_whole(tmp_path: Path) -> None:
    target = tmp_path / "ayeops_offsite.prom"
    agent.write_metrics(target, "a 1\n")
    agent.write_metrics(target, "b 2\n")
    assert target.read_text(encoding="utf-8") == "b 2\n"
    assert [p.name for p in tmp_path.iterdir()] == ["ayeops_offsite.prom"]


def test_file_digest_and_fingerprint_read_the_real_file(tmp_path: Path) -> None:
    path = tmp_path / "a.dump"
    path.write_bytes(DUMP * 300)
    sha, size = agent.file_digest(str(path))
    assert (sha, size) == (hashlib.sha256(DUMP * 300).hexdigest(), len(DUMP) * 300)
    assert agent.fingerprint(str(path))[0] == size


def snapshots_after_backup() -> Any:
    listing = json.dumps(
        [
            snapshot_json(ident, when)
            for ident, when in (
                (
                    "c784ce443e3143616cee76c006727e4e28d7a05a64970ac101e8d5ba03595a0a",
                    "2026-10-24T01:01:51.9754275+03:00",
                ),
                (
                    "0c2d4d92b6f0b8c34b80f9f1a1fd4c07d8c8b8c22e4b9b1a2d2d71d0f0a6f6c1",
                    "2026-10-24T01:01:53.2054705+03:00",
                ),
            )
        ]
    )
    return agent.Done(0, listing, "")


def forget_keeping(*groups: Sequence[str]) -> Any:
    return agent.Done(
        0,
        json.dumps([{"keep": [{"id": i} for i in group], "remove": []} for group in groups]),
        "",
    )


def test_backup_pass_tags_the_sha_groups_by_host_prunes_and_records_everything() -> None:
    ids = [
        "c784ce443e3143616cee76c006727e4e28d7a05a64970ac101e8d5ba03595a0a",
        "0c2d4d92b6f0b8c34b80f9f1a1fd4c07d8c8b8c22e4b9b1a2d2d71d0f0a6f6c1",
    ]
    restic = FakeRestic({"snapshots": snapshots_after_backup(), "forget": forget_keeping(ids)})
    run = make_pass(restic)
    state = run.backup(agent.State())
    assert restic.verbs() == [
        "snapshots",
        "unlock",
        "backup",
        "snapshots",
        "forget",
        "prune",
        "stats",
    ]
    backup = restic.call("backup")
    assert backup[-1] == "/var/backups/acme/acme-2.dump"
    assert backup[2:4] == ["--host", "edge"]
    assert f"sha256={DUMP_SHA}" in backup
    assert "ayeops" in backup
    forget = restic.call("forget")
    assert forget[forget.index("--group-by") + 1] == "host"
    assert forget[-6:] == POLICY.flags()
    assert (state.last_ok, state.retention_ok, state.repository_ok) == (True, True, True)
    assert state.snapshots == 2
    assert state.repo_bytes == 813403
    assert state.last_added_bytes == 406863
    assert state.last_source_bytes == len(DUMP)
    assert state.last_file == "/var/backups/acme/acme-2.dump"
    assert state.last_attempt is not None and state.last_success is not None
    assert state.last_success > state.last_attempt
    assert state.last_duration == pytest.approx(state.last_success - state.last_attempt)
    assert state.last_error == ""


def test_a_missing_repository_stops_the_pass_and_points_at_restic_init() -> None:
    restic = FakeRestic({"snapshots": agent.Done(10, "", fixture("missing.err"))})
    state = make_pass(restic).backup(agent.State())
    assert restic.verbs() == ["snapshots"]
    assert (state.last_ok, state.repository_ok) == (False, False)
    assert "restic init" in state.last_error


def test_a_wrong_password_and_other_listing_errors_name_their_cause() -> None:
    restic = FakeRestic({"snapshots": agent.Done(12, "", fixture("wrong.err"))})
    assert "AYE_OFFSITE_RESTIC_PASSWORD" in make_pass(restic).backup(agent.State()).last_error
    restic = FakeRestic({"snapshots": agent.Done(1, "", "Fatal: connection refused")})
    state = make_pass(restic).backup(agent.State())
    assert state.last_error == "restic snapshots failed (1): Fatal: connection refused"


def test_no_settled_file_is_a_failed_run_without_touching_restic() -> None:
    restic = FakeRestic()
    state = make_pass(restic, files=[("/var/backups/acme/a.dump", NOW)]).backup(agent.State())
    assert restic.verbs() == ["snapshots"]
    assert state.last_ok is False
    assert state.repository_ok is True
    assert "no settled file" in state.last_error


def test_a_failed_backup_keeps_the_last_success_and_says_why() -> None:
    restic = FakeRestic({"backup": agent.Done(3, "", "Fatal: unable to read")})
    previous = agent.State(last_success=NOW - DAY, last_ok=True)
    state = make_pass(restic).backup(previous)
    assert state.last_success == NOW - DAY
    assert state.last_ok is False
    assert state.last_error == "restic backup failed (3): Fatal: unable to read"
    assert "forget" not in restic.verbs()


def test_a_file_that_changed_during_the_backup_loses_its_snapshot() -> None:
    restic = FakeRestic()
    run = make_pass(restic, stats=[(len(DUMP), NOW - 7200), (len(DUMP) + 1, NOW - 1)])
    state = run.backup(agent.State())
    assert state.last_ok is False
    assert "changed while" in state.last_error
    assert restic.calls[-1] == [
        "forget",
        "c784ce449e3143616cee76c006727e4e28d7a05a64970ac101e8d5ba03595a0a",
    ]


def test_restic_grouping_by_path_is_reported_as_retention_drift() -> None:
    restic = FakeRestic(
        {
            "snapshots": snapshots_after_backup(),
            "forget": agent.Done(0, fixture("forget-paths.json"), ""),
        }
    )
    run = make_pass(restic)
    state = run.backup(agent.State())
    assert state.last_ok is True
    assert state.retention_ok is False
    assert any("in 2 groups" in note for note in run.notes)


def test_retention_drift_when_restic_keeps_other_snapshots_than_the_policy() -> None:
    restic = FakeRestic({"snapshots": snapshots_after_backup(), "forget": forget_keeping(["zzz"])})
    run = make_pass(restic)
    state = run.backup(agent.State())
    assert state.retention_ok is False
    assert any("retention drift" in note for note in run.notes)


def test_more_kept_snapshots_than_the_policy_bound_is_drift() -> None:
    many = [f"id{n:02d}" for n in range(15)]
    listing = json.dumps(
        [snapshot_json(i, f"2026-10-{n + 1:02d}T05:30:00+03:00") for n, i in enumerate(many)]
    )
    restic = FakeRestic({"snapshots": agent.Done(0, listing, ""), "forget": forget_keeping(many)})
    state = make_pass(restic, policy=agent.Policy(30, 0, 0)).backup(agent.State())
    assert state.retention_ok is True
    state = make_pass(
        FakeRestic({"snapshots": agent.Done(0, listing, ""), "forget": forget_keeping(many)}),
        policy=agent.Policy(14, 0, 0),
    ).backup(agent.State())
    assert state.retention_ok is False


def test_failed_forget_prune_or_stats_keep_the_backup_but_flag_retention() -> None:
    restic = FakeRestic(
        {"snapshots": snapshots_after_backup(), "forget": agent.Done(1, "", "Fatal: lock")}
    )
    state = make_pass(restic).backup(agent.State())
    assert (state.last_ok, state.retention_ok) == (True, False)
    assert "prune" not in restic.verbs()
    ids = [i["id"] for i in json.loads(snapshots_after_backup().stdout)]
    restic = FakeRestic(
        {
            "snapshots": snapshots_after_backup(),
            "forget": forget_keeping(ids),
            "prune": agent.Done(1, "", "Fatal: prune"),
            "stats": agent.Done(1, "", ""),
        }
    )
    state = make_pass(restic).backup(agent.State(repo_bytes=77))
    assert (state.last_ok, state.retention_ok, state.repo_bytes) == (True, False, 77)


def test_listing_failure_before_forget_flags_retention() -> None:
    replies = iter([snapshots_after_backup(), agent.Done(1, "", "Fatal: gone")])
    restic = FakeRestic({"snapshots": lambda args: next(replies)})
    state = make_pass(restic).backup(agent.State())
    assert state.retention_ok is False


def restore_restic(dump: bytes = DUMP, sha: str | None = DUMP_SHA, **replies: Any) -> FakeRestic:
    listing = json.dumps(
        [
            snapshot_json(
                "old" + "0" * 61,
                "2026-10-17T05:30:00+03:00",
                "f" * 64,
                "/var/backups/acme/old.dump",
            ),
            snapshot_json("new" + "0" * 61, "2026-10-24T05:30:00+03:00", sha),
        ]
    )
    return FakeRestic({"snapshots": agent.Done(0, listing, ""), **replies}, dump=dump)


def test_restore_test_streams_the_newest_snapshot_and_compares_its_sha() -> None:
    restic = restore_restic()
    run = make_pass(restic)
    state = run.restore(agent.State(last_success=NOW))
    assert restic.call("dump") == ["dump", "new" + "0" * 61, "/var/backups/acme/acme-2.dump"]
    assert restic.verbs()[-1] == "check"
    assert state.restore_ok is True
    assert state.restore_success is not None and state.restore_attempt is not None
    assert state.restore_success > state.restore_attempt
    assert state.restore_error == ""


@pytest.mark.parametrize(
    ("restic", "verify", "reason"),
    [
        (restore_restic(dump=DUMP + b"!"), "pg_dump", "differs"),
        (restore_restic(dump=b""), "pg_dump", "empty"),
        (restore_restic(sha=None), "pg_dump", "no sha256= tag"),
        (restore_restic(dump=b"PK", sha=hashlib.sha256(b"PK").hexdigest()), "pg_dump", "PGDMP"),
        (
            restore_restic(dump=agent.Done(1, "", "Fatal: not found")),
            "pg_dump",
            "restic dump failed (1)",
        ),
        (
            restore_restic(check=agent.Done(1, "", "Fatal: pack damaged")),
            "pg_dump",
            "restic check failed (1): Fatal: pack damaged",
        ),
        (FakeRestic(), "pg_dump", "no snapshot"),
        (FakeRestic({"snapshots": agent.Done(10, "", "")}), "pg_dump", "no snapshot"),
    ],
)
def test_restore_test_fails_on_every_broken_link(restic: Any, verify: str, reason: str) -> None:
    if isinstance(restic.dump, agent.Done):
        restic.replies["dump"] = restic.dump
        restic.dump = b""
    previous = agent.State(last_success=NOW, restore_success=NOW - 8 * DAY, restore_ok=True)
    state = make_pass(restic, verify=verify).restore(previous)
    assert state.restore_ok is False
    assert reason in state.restore_error
    assert state.restore_success == NOW - 8 * DAY


def test_restore_without_pg_dump_check_accepts_any_matching_file() -> None:
    data = b"PK\x03\x04 zip"
    restic = restore_restic(dump=data, sha=hashlib.sha256(data).hexdigest())
    assert make_pass(restic, verify="none").restore(agent.State(last_success=NOW)).restore_ok


def test_restore_of_a_snapshot_without_paths_fails() -> None:
    listing = json.dumps([{"id": "x", "time": "2026-10-24T05:30:00+03:00", "paths": []}])
    restic = FakeRestic({"snapshots": agent.Done(0, listing, "")})
    state = make_pass(restic).restore(agent.State(last_success=NOW))
    assert state.restore_ok is False
    assert "has no path" in state.restore_error


def test_tick_runs_the_backup_then_the_first_restore_test() -> None:
    ids = [i["id"] for i in json.loads(snapshots_after_backup().stdout)]
    restic = FakeRestic({"snapshots": snapshots_after_backup(), "forget": forget_keeping(ids)})
    restic.replies["dump"] = agent.Done(0, "", "")
    listing = json.loads(snapshots_after_backup().stdout)
    restic.dump = DUMP
    state = make_pass(restic).tick(agent.State())
    assert "backup" in restic.verbs()
    assert "dump" in restic.verbs()
    assert state.last_ok is True
    assert state.restore_ok is True
    assert listing[1]["id"] in restic.call("dump")


def test_tick_does_nothing_between_slots() -> None:
    restic = FakeRestic()
    state = agent.State(
        last_attempt=NOW - 100,
        last_success=NOW - 100,
        restore_attempt=NOW - 100,
        restore_success=NOW - 100,
    )
    assert make_pass(restic).tick(state) == state
    assert restic.calls == []


def test_tick_turns_unexpected_errors_into_failed_runs_instead_of_crashing() -> None:
    restic = FakeRestic({"backup": agent.Done(0, "garbage without summary", "")})
    state = make_pass(restic).tick(agent.State())
    assert state.last_ok is False
    assert "backup pass" in state.last_error
    assert state.last_attempt is not None
    restic = restore_restic(snapshots=agent.Done(0, "{not json", ""))
    state = make_pass(restic).tick(agent.State(last_attempt=NOW, last_success=NOW, last_ok=True))
    assert state.restore_ok is False
    assert state.restore_attempt is not None


def test_config_reads_every_variable_and_defaults_the_rest() -> None:
    environ = {"AYE_OFFSITE_SOURCE": "/var/backups/acme/*.dump", "AYE_OFFSITE_HOST": "edge"}
    loaded = agent.load_config(environ)
    assert loaded.schedule == (5, 30)
    assert loaded.policy == POLICY
    assert (loaded.settle_seconds, loaded.retry_seconds) == (900, 3600)
    assert loaded.quota_bytes is None
    assert loaded.timezone == "UTC"
    assert loaded.verify == "pg_dump"
    assert loaded.restore_every_days == 7
    assert loaded.state_path == Path("/state/state.json")
    assert loaded.textfile == Path("/textfile/ayeops_offsite.prom")
    assert loaded.restic == "/opt/restic/restic"
    assert loaded.max_age_seconds is None
    full = agent.load_config(
        {
            **environ,
            "AYE_OFFSITE_SCHEDULE": "23:05",
            "AYE_OFFSITE_TIMEZONE": "Europe/Kyiv",
            "AYE_OFFSITE_KEEP_DAILY": "1",
            "AYE_OFFSITE_KEEP_WEEKLY": "0",
            "AYE_OFFSITE_KEEP_MONTHLY": " ",
            "AYE_OFFSITE_RESTORE_TEST_DAYS": "3",
            "AYE_OFFSITE_SETTLE_MINUTES": "0",
            "AYE_OFFSITE_RETRY_MINUTES": "5",
            "AYE_OFFSITE_VERIFY": "none",
            "AYE_OFFSITE_QUOTA_BYTES": "1024",
            "AYE_OFFSITE_MAX_AGE_SECONDS": "90000",
        }
    )
    assert full.max_age_seconds == 90000
    assert full.schedule == (23, 5)
    assert full.policy == agent.Policy(1, 0, 3)
    assert (full.settle_seconds, full.retry_seconds, full.restore_every_days) == (0, 300, 3)
    assert (full.verify, full.quota_bytes, full.timezone) == ("none", 1024, "Europe/Kyiv")


@pytest.mark.parametrize("text", ["24:00", "05:60", "-1:00", "5"])
def test_schedule_outside_a_day_is_rejected(text: str) -> None:
    with pytest.raises(ValueError):
        agent.parse_schedule(text)


def test_schedule_accepts_the_edges_of_the_day() -> None:
    assert agent.parse_schedule("00:00") == (0, 0)
    assert agent.parse_schedule(" 23:59 ") == (23, 59)


def test_process_runs_restic_and_streams_its_stdout() -> None:
    process = agent.Process(sys.executable)
    done = process.run(
        ["-c", "import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)"]
    )
    assert (done.code, done.stdout.strip(), done.stderr.strip()) == (3, "out", "err")
    chunks: list[bytes] = []
    streamed = process.stream(
        ["-c", "import sys; sys.stdout.buffer.write(b'PGDMP' * 1000); print('e', file=sys.stderr)"],
        chunks.append,
    )
    assert streamed.code == 0
    assert b"".join(chunks) == b"PGDMP" * 1000
    assert streamed.stderr.strip() == "e"


def test_process_reports_a_missing_binary_and_a_timeout() -> None:
    missing = agent.Process(str(Path("no") / "such" / "restic"))
    assert missing.run(["snapshots"]).code == 127
    assert missing.stream(["dump"], lambda chunk: None).code == 127
    slow = agent.Process(sys.executable, timeout=1)
    assert slow.run(["-c", f"import time; time.sleep({WAIT})"]).code == 124
    timed = slow.stream(
        [
            "-c",
            "import sys, time\n"
            "while True:\n"
            " sys.stdout.buffer.write(b'x' * 2**20)\n"
            " sys.stdout.flush()\n"
            " time.sleep(0.3)",
        ],
        lambda chunk: None,
    )
    assert timed.code == 124


def outlives(marker: Path) -> str:
    return f"import pathlib, time; time.sleep({WAIT}); pathlib.Path({str(marker)!r}).touch()"


def test_a_stream_that_stalls_without_output_still_times_out(tmp_path: Path) -> None:
    slow = agent.Process(sys.executable, timeout=1)
    finished = tmp_path / "finished"
    stalled = slow.stream(["-c", outlives(finished)], lambda chunk: None)
    assert stalled.code == 124
    assert "timed out after 1s" in stalled.stderr
    assert not finished.exists()


def test_a_stream_survives_a_flood_on_stderr_and_reports_it() -> None:
    process = agent.Process(sys.executable, timeout=WAIT)
    chunks: list[bytes] = []
    done = process.stream(
        [
            "-c",
            "import sys; sys.stderr.write('e' * 300000); sys.stderr.flush();"
            " sys.stdout.buffer.write(b'PGDMP'); sys.exit(2)",
        ],
        chunks.append,
    )
    assert done.code == 2
    assert b"".join(chunks) == b"PGDMP"
    assert len(done.stderr) == 300000


def test_a_failing_sink_stops_the_child_and_raises(tmp_path: Path) -> None:
    process = agent.Process(sys.executable, timeout=2 * WAIT)
    finished = tmp_path / "finished"

    def sink(chunk: bytes) -> None:
        raise OSError("disk gone")

    with pytest.raises(OSError, match="disk gone"):
        process.stream(
            [
                "-c",
                "import sys; sys.stdout.buffer.write(b'x' * 2**21); sys.stdout.flush()\n"
                + outlives(finished),
            ],
            sink,
        )
    assert not finished.exists()


def test_main_once_writes_state_and_metrics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ids = [i["id"] for i in json.loads(snapshots_after_backup().stdout)]
    restic = FakeRestic({"snapshots": snapshots_after_backup(), "forget": forget_keeping(ids)})
    source = tmp_path / "acme-2.dump"
    source.write_bytes(DUMP)
    old = time.time() - 7200
    os.utime(source, (old, old))
    monkeypatch.setattr(agent, "Process", lambda binary: restic)
    environ = {
        "AYE_OFFSITE_SOURCE": str(tmp_path / "*.dump"),
        "AYE_OFFSITE_HOST": "edge",
        "AYE_OFFSITE_STATE": str(tmp_path / "state.json"),
        "AYE_OFFSITE_TEXTFILE": str(tmp_path / "ayeops_offsite.prom"),
        "AYE_OFFSITE_QUOTA_BYTES": "100",
        "AYE_OFFSITE_ONCE": "1",
    }
    for name, value in environ.items():
        monkeypatch.setenv(name, value)
    agent.main()
    metrics = (tmp_path / "ayeops_offsite.prom").read_text(encoding="utf-8")
    assert "ayeops_offsite_last_run_success 1\n" in metrics
    assert "ayeops_offsite_quota_bytes 100\n" in metrics
    assert "ayeops_offsite_restore_test_ok 1\n" in metrics
    saved = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert saved["last_file"] == str(source)
    assert f"sha256={DUMP_SHA}" in restic.call("backup")


def test_operational_constants_fit_the_healthcheck_window() -> None:
    assert agent.RESTIC_TIMEOUT == 6 * 3600
    assert agent.Process("restic").timeout == agent.RESTIC_TIMEOUT
    assert agent.RESTIC_TIMEOUT < 25200
    assert agent.LOOP_SECONDS == 60
    assert agent.CHUNK == 1024 * 1024


def test_a_fresh_state_knows_nothing() -> None:
    fresh = agent.State()
    assert (fresh.last_error, fresh.last_file, fresh.restore_error) == ("", "", "")
    assert fresh.last_ok is None and fresh.restore_ok is None


def test_policy_flags_keep_a_bucket_of_one() -> None:
    assert agent.Policy(1, 1, 1).flags() == [
        "--keep-daily",
        "1",
        "--keep-weekly",
        "1",
        "--keep-monthly",
        "1",
    ]


def test_parse_errors_name_what_was_wrong() -> None:
    with pytest.raises(ValueError, match=r"^not a time of day: '24:00'$"):
        agent.parse_schedule("24:00")
    with pytest.raises(ValueError, match=r"^time without an offset: '2026-10-24T05:30:00'$"):
        agent.parse_time("2026-10-24T05:30:00")
    with pytest.raises(ValueError, match=r"^restic snapshots --json did not return a list$"):
        agent.parse_snapshots("{}")
    with pytest.raises(ValueError, match=r"^restic backup --json printed no summary$"):
        agent.parse_summary("")
    with pytest.raises(ValueError, match=r"^restic forget --json did not return a list$"):
        agent.parse_forget("{}")


def test_summary_skips_trailing_text_and_defaults_missing_numbers_to_zero() -> None:
    text = '{"message_type":"summary","data_added":5,"snapshot_id":"s"}\nrestic: done\n'
    summary = agent.parse_summary(text)
    assert (summary.snapshot_id, summary.added_bytes, summary.processed_bytes) == ("s", 5, 0)


def test_forget_collects_removals_across_groups() -> None:
    text = json.dumps(
        [
            {"keep": [{"id": "a"}], "remove": [{"id": "b"}]},
            {"keep": [{"id": "c"}], "remove": [{"id": "d"}]},
        ]
    )
    assert agent.parse_forget(text) == agent.Forget(2, ("a", "c"), ("b", "d"))


def test_exit_message_skips_broken_json_and_tolerates_an_empty_message() -> None:
    text = '{"message_type":"exit_error","message":"Fatal: x"}\n{broken'
    assert agent.exit_message(text) == "Fatal: x"
    assert agent.exit_message('{"message_type":"exit_error"}') == ""


def test_verdict_messages_are_exact() -> None:
    sha = "a" * 64
    assert agent.verdict(None, sha, 1, b"PGDMP", "pg_dump") == (
        "the snapshot has no sha256= tag to compare against"
    )
    assert agent.verdict(sha, sha, 0, b"", "pg_dump") == "the restored file is empty"
    assert agent.verdict(sha, "b" * 64, 1, b"PGDMP", "pg_dump") == (
        f"restored sha256 {'b' * 64} differs from the recorded {sha}"
    )
    assert agent.verdict(sha, sha, 1, b"PK", "pg_dump") == (
        "the restored file does not start with the pg_dump custom-format header PGDMP"
    )


def test_config_reads_each_variable_by_its_own_name() -> None:
    loaded = agent.load_config(
        {
            "AYE_OFFSITE_SOURCE": "/s/*.fc",
            "AYE_OFFSITE_HOST": "h",
            "AYE_OFFSITE_KEEP_MONTHLY": "9",
            "AYE_OFFSITE_STATE": "/x/state.json",
            "AYE_OFFSITE_TEXTFILE": "/x/metrics.prom",
            "AYE_RESTIC": "/x/restic",
        }
    )
    assert loaded.policy == agent.Policy(7, 4, 9)
    assert loaded.state_path == Path("/x/state.json")
    assert loaded.textfile == Path("/x/metrics.prom")
    assert loaded.restic == "/x/restic"


def test_backup_pass_issues_exactly_these_restic_commands() -> None:
    ids = [i["id"] for i in json.loads(snapshots_after_backup().stdout)]
    restic = FakeRestic({"snapshots": snapshots_after_backup(), "forget": forget_keeping(ids)})
    run = make_pass(restic)
    run.clock = Clock(step=0.12345)
    state = run.backup(agent.State())
    host = ["--host", "edge"]
    assert restic.calls == [
        ["snapshots", "--json", *host],
        ["unlock"],
        [
            "backup",
            "--json",
            *host,
            "--tag",
            "ayeops",
            "--tag",
            f"sha256={DUMP_SHA}",
            "/var/backups/acme/acme-2.dump",
        ],
        ["snapshots", "--json", *host],
        ["forget", "--json", *host, "--group-by", "host", *POLICY.flags()],
        ["prune"],
        ["stats", "--json", "--mode", "raw-data"],
    ]
    assert state.last_duration == 0.123
    assert run.notes == ["backed up /var/backups/acme/acme-2.dump, 406863 bytes added"]


def test_backup_failures_carry_exact_reasons() -> None:
    restic = FakeRestic({"snapshots": agent.Done(10, "", fixture("missing.err"))})
    assert make_pass(restic).backup(agent.State()).last_error == (
        "the repository does not exist yet: run restic init, see the offsite runbook"
    )
    restic = FakeRestic({"snapshots": agent.Done(12, "", "")})
    assert make_pass(restic).backup(agent.State()).last_error == (
        "wrong AYE_OFFSITE_RESTIC_PASSWORD for this repository"
    )
    stale = make_pass(FakeRestic(), files=[("/var/backups/acme/a.dump", NOW)])
    assert stale.backup(agent.State()).last_error == (
        "no settled file matches /var/backups/acme/*.dump"
    )
    moving = make_pass(FakeRestic(), stats=[(1, 1.0), (2, 2.0)])
    assert moving.backup(agent.State()).last_error == (
        "/var/backups/acme/acme-2.dump changed while it was backed up"
    )


def test_retention_notes_are_exact() -> None:
    restic = FakeRestic(
        {"snapshots": snapshots_after_backup(), "forget": agent.Done(1, "", "Fatal: lock")}
    )
    run = make_pass(restic)
    run.backup(agent.State())
    assert "restic forget failed (1): Fatal: lock" in run.notes
    restic = FakeRestic({"snapshots": snapshots_after_backup(), "forget": forget_keeping(["z"])})
    run = make_pass(restic)
    run.backup(agent.State())
    assert "retention drift: restic kept 1 in 1 groups, the policy keeps 2" in run.notes
    ids = [i["id"] for i in json.loads(snapshots_after_backup().stdout)]
    restic = FakeRestic(
        {
            "snapshots": snapshots_after_backup(),
            "forget": forget_keeping(ids),
            "prune": agent.Done(1, "", "Fatal: prune"),
        }
    )
    run = make_pass(restic)
    run.backup(agent.State())
    assert "restic prune failed (1): Fatal: prune" in run.notes


def test_exactly_as_many_kept_snapshots_as_the_bound_is_fine() -> None:
    many = [f"id{n:02d}" for n in range(15)]
    listing = json.dumps(
        [snapshot_json(i, f"2026-10-{n + 1:02d}T05:30:00+03:00") for n, i in enumerate(many)]
    )
    restic = FakeRestic({"snapshots": agent.Done(0, listing, ""), "forget": forget_keeping(many)})
    state = make_pass(restic, policy=agent.Policy(15, 0, 0)).backup(agent.State())
    assert state.retention_ok is True


@pytest.mark.parametrize(
    ("restic", "reason"),
    [
        (FakeRestic(), "no snapshot to restore"),
        (
            restore_restic(dump=agent.Done(1, "", "Fatal: not found")),
            "restic dump failed (1): Fatal: not found",
        ),
        (
            restore_restic(check=agent.Done(1, "", "Fatal: pack damaged")),
            "restic check failed (1): Fatal: pack damaged",
        ),
    ],
)
def test_restore_failures_carry_exact_reasons(restic: Any, reason: str) -> None:
    if isinstance(restic.dump, agent.Done):
        restic.replies["dump"] = restic.dump
        restic.dump = b""
    run = make_pass(restic)
    assert run.restore(agent.State(last_success=NOW)).restore_error == reason
    assert run.notes == [f"restore test failed: {reason}"]


def test_restore_reads_the_header_and_size_across_tiny_chunks() -> None:
    restic = restore_restic()
    restic.chunk = 2
    run = make_pass(restic)
    state = run.restore(agent.State(last_success=NOW))
    assert state.restore_ok is True
    assert state.restore_error == ""
    assert run.notes == [
        f"restore test passed: /var/backups/acme/acme-2.dump from new00000, {len(DUMP)} bytes"
    ]


def test_a_snapshot_without_paths_is_named() -> None:
    listing = json.dumps([{"id": "x1", "time": "2026-10-24T05:30:00+03:00", "paths": []}])
    restic = FakeRestic({"snapshots": agent.Done(0, listing, "")})
    state = make_pass(restic).restore(agent.State(last_success=NOW))
    assert state.restore_error == "snapshot x1 has no path"


def test_tick_reads_the_slot_in_the_configured_zone() -> None:
    restic = FakeRestic()
    now = dt.datetime(2026, 10, 24, 3, 0, tzinfo=UTC).timestamp()
    last = dt.datetime(2026, 10, 23, 20, 0, tzinfo=UTC).timestamp()
    run = make_pass(restic, timezone="Pacific/Kiritimati")
    run.clock = Clock(start=now, step=0.0)
    state = agent.State(
        last_attempt=last, last_success=last, restore_attempt=last, restore_success=last
    )
    assert run.tick(state) == state
    assert restic.calls == []


def test_a_crashing_backup_pass_is_reported_as_such() -> None:
    restic = FakeRestic({"backup": agent.Done(0, "garbage", "")})
    state = make_pass(restic).tick(agent.State())
    assert state.last_error == "backup pass: restic backup --json printed no summary"


def test_notes_go_to_stderr_with_the_agent_name(capsys: pytest.CaptureFixture[str]) -> None:
    run = make_pass(FakeRestic())
    run.note("hello")
    assert capsys.readouterr().err == "offsite: hello\n"
    assert run.notes == ["hello"]


def test_an_append_only_target_gets_a_backup_and_an_inventory_but_no_forget_or_prune() -> None:
    restic = FakeRestic({"snapshots": snapshots_after_backup()})
    run = make_pass(restic, append_only=True)
    state = run.backup(agent.State(retention_ok=True))
    host = ["--host", "edge"]
    assert restic.calls[3:] == [
        ["snapshots", "--json", *host],
        ["stats", "--json", "--mode", "raw-data"],
    ]
    assert "forget" not in restic.verbs() and "prune" not in restic.verbs()
    assert state.last_ok is True
    assert state.retention_ok is None
    assert state.snapshots == len(json.loads(snapshots_after_backup().stdout))
    assert state.repo_bytes == agent.parse_stats(fixture("stats.json"))
    assert state.last_snapshot == (
        "c784ce449e3143616cee76c006727e4e28d7a05a64970ac101e8d5ba03595a0a"
    )
    assert "retention_ok" not in agent.metrics_text(state, None)


def test_an_append_only_inventory_keeps_the_last_numbers_when_restic_fails() -> None:
    failing = agent.Done(1, "", "boom")
    restic = FakeRestic({"stats": failing})
    run = make_pass(restic, append_only=True)
    previous = agent.State(snapshots=5, repo_bytes=123)
    restic.replies["snapshots"] = lambda args: (
        agent.Done(0, "[]", "") if len(restic.calls) == 1 else failing
    )
    state = run.backup(previous)
    assert state.last_ok is True
    assert (state.snapshots, state.repo_bytes) == (5, 123)


def test_an_append_only_target_keeps_a_snapshot_whose_file_changed() -> None:
    restic = FakeRestic()
    stats = [(len(DUMP), NOW - 7200), (len(DUMP) + 1, NOW - 1)]
    state = make_pass(restic, stats=stats, append_only=True).backup(agent.State())
    assert state.last_ok is False
    assert "changed while" in state.last_error
    assert "forget" not in restic.verbs()
    assert state.last_snapshot == ""


def test_the_restore_test_reads_the_snapshot_the_last_good_backup_recorded() -> None:
    restic = restore_restic()
    recorded = "old" + "0" * 61
    state = make_pass(restic, verify="none").restore(
        agent.State(last_success=NOW, last_snapshot=recorded)
    )
    assert restic.call("dump") == ["dump", recorded, "/var/backups/acme/old.dump"]
    assert state.restore_ok is False
    assert "differs from the recorded" in state.restore_error


def test_restore_pick_falls_back_to_the_newest_when_the_recorded_one_is_gone() -> None:
    older = snap("a", "2026-10-20T05:30:00+03:00")
    newer = snap("b", "2026-10-24T05:30:00+03:00")
    assert agent.restore_pick([older, newer], "").id == "b"
    assert agent.restore_pick([older, newer], "gone").id == "b"
    assert agent.restore_pick([newer, older], "a").id == "a"


def test_config_turns_append_only_on_with_exactly_one() -> None:
    environ = {"AYE_OFFSITE_SOURCE": "/var/backups/acme/*.dump", "AYE_OFFSITE_HOST": "edge"}
    assert agent.load_config(environ).append_only is False
    assert agent.load_config({**environ, "AYE_OFFSITE_APPEND_ONLY": "1"}).append_only is True
    assert agent.load_config({**environ, "AYE_OFFSITE_APPEND_ONLY": "0"}).append_only is False


def test_state_round_trips_the_recorded_snapshot(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    agent.save_state(path, agent.State(last_snapshot="abc"))
    assert agent.load_state(path).last_snapshot == "abc"
