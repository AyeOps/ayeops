from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from ayeops.backups import (
    HOST_ROOT,
    CronError,
    Job,
    backup_units,
    backups_context,
    backups_problems,
    builtin_jobs,
    grace_seconds,
    job_ages,
    longest_gap,
    manifest_jobs,
    max_age,
    metrics_backup_problems,
    metrics_backup_warnings,
    metrics_settings,
    on_calendar,
    parse_cron,
    unit_files,
)
from ayeops.deploy import REMOTE_ROOT
from ayeops.registry import MODULES_DIR, Module
from ayeops.render import grafana_rules, host_compose, render_tree
from tests.conftest import WAIT, manifest_from, pinned, rules_hit, tag_of, warnings_hit

RUN = MODULES_DIR / "backups" / "files" / "backup_run.sh"
JOBS = MODULES_DIR / "backups" / "files" / "backup_jobs.sh"
UTC = ZoneInfo("UTC")
BERLIN = ZoneInfo("Europe/Berlin")
HOUR = 3600
DAY = 86400


def backups(data: dict[str, Any]) -> dict[str, Any]:
    settings: dict[str, Any] = data["hosts"]["edge"]["modules"]["backups"]
    return settings


def job(data: dict[str, Any]) -> dict[str, Any]:
    found: dict[str, Any] = backups(data)["jobs"]["acme-db"]
    return found


def gap(schedule: str, zone: ZoneInfo = UTC) -> int | None:
    return longest_gap(parse_cron(schedule), zone)


def test_a_field_takes_numbers_ranges_lists_and_steps() -> None:
    cron = parse_cron("0,30 */6 1-5 1-12/3 1,3-5")
    assert cron.minutes == {0, 30}
    assert cron.hours == {0, 6, 12, 18}
    assert cron.days == {1, 2, 3, 4, 5}
    assert cron.months == {1, 4, 7, 10}
    assert cron.weekdays == {1, 3, 4, 5}
    assert (cron.any_day, cron.any_weekday) == (False, False)


def test_a_number_with_a_step_runs_from_that_number_to_the_end_of_the_range() -> None:
    assert parse_cron("10/20 * * * *").minutes == {10, 30, 50}
    assert parse_cron("* * * * *").weekdays == set(range(7))


def test_sunday_is_both_zero_and_seven() -> None:
    assert parse_cron("0 0 * * 7").weekdays == {0}
    assert parse_cron("0 0 * * 5-7").weekdays == {5, 6, 0}


def test_a_step_on_a_star_still_counts_as_unrestricted_like_cron_does() -> None:
    cron = parse_cron("0 0 */2 * 1")
    assert cron.any_day
    assert not cron.any_weekday


@pytest.mark.parametrize(
    ("schedule", "where"),
    [
        ("0 4 * *", "has 4 fields"),
        ("0 4 * * * *", "has 6 fields"),
        ("60 4 * * *", "minute"),
        ("0 24 * * *", "hour"),
        ("0 4 0 * *", "day of month"),
        ("0 4 32 * *", "day of month"),
        ("0 4 * 13 *", "month"),
        ("0 4 * * 8", "day of week"),
        ("5-1 * * * *", "minute"),
        ("*/0 * * * *", "at least 1"),
        ("a * * * *", "not a number"),
        ("1- * * * *", "not a number"),
        ("100 * * * *", "not a number"),
        (", * * * *", "not a number"),
    ],
)
def test_a_schedule_outside_cron_is_refused_with_the_field(schedule: str, where: str) -> None:
    with pytest.raises(CronError, match=where):
        parse_cron(schedule)


@pytest.mark.parametrize(
    ("schedule", "seconds"),
    [
        ("30 4 * * *", DAY),
        ("0 */6 * * *", 6 * HOUR),
        ("*/5 * * * *", 300),
        ("0 1,23 * * *", 22 * HOUR),
        ("15 2 * * 1-5", 3 * DAY),
        ("0 0 * * 1", 7 * DAY),
        ("0 0 * * 0", 7 * DAY),
        ("0 3 1 * *", 31 * DAY),
        ("0 0 1 1 *", 366 * DAY),
        ("0 0 */2 * *", 2 * DAY),
        ("0 0 1 * 1", 7 * DAY),
    ],
)
def test_the_longest_gap_between_two_starts(schedule: str, seconds: int) -> None:
    assert gap(schedule) == seconds


def test_day_of_month_and_day_of_week_combine_with_or_when_both_are_restricted() -> None:
    cron = parse_cron("0 0 13 * 5")
    friday_the_twelfth = parse_cron("0 0 * * 5")
    assert gap("0 0 13 * 5") == gap("0 0 * * 5") == 7 * DAY
    assert cron.matches(date(2027, 1, 13))
    assert friday_the_twelfth.matches(date(2027, 2, 12))
    assert cron.matches(date(2027, 2, 12))


def test_the_night_the_clocks_go_back_makes_a_daily_job_wait_25_hours() -> None:
    assert gap("30 4 * * *", BERLIN) == 25 * HOUR
    assert gap("30 2 * * *", BERLIN) == 25 * HOUR


def test_an_hourly_job_waits_two_hours_across_the_hour_the_clocks_go_back() -> None:
    assert gap("0 * * * *", UTC) == HOUR
    assert gap("0 * * * *", BERLIN) == 2 * HOUR


def test_a_schedule_that_fires_less_than_twice_in_four_years_has_no_gap() -> None:
    assert gap("0 0 30 2 *") is None
    assert gap("0 0 29 2 *") is None
    assert gap("0,30 0 29 2 *") is None
    assert gap("0 0 31 12 *") == 366 * DAY


def test_the_age_is_the_longest_gap_plus_grace_two_hours_by_default() -> None:
    assert max_age({"schedule": "30 4 * * *"}, "UTC") == DAY + 2 * HOUR
    assert max_age({"schedule": "30 4 * * *", "grace": "1h"}, "Europe/Berlin") == 26 * HOUR
    assert max_age({"schedule": "*/5 * * * *", "grace": "10m"}, "UTC") == 900
    assert max_age({"schedule": "0 0 30 2 *"}, "UTC") is None


def test_grace_is_minutes_or_hours() -> None:
    assert grace_seconds("45m") == 2700
    assert grace_seconds("3h") == 3 * HOUR
    with pytest.raises(ValueError, match="minutes or hours"):
        grace_seconds("2d")


def test_jobs_whose_schedule_does_not_resolve_are_left_out_of_the_declaration() -> None:
    settings = {
        "timezone": "UTC",
        "jobs": {
            "b-files": {"schedule": "0 */6 * * *", "grace": "30m"},
            "a-db": {"schedule": "0 3 * * *"},
            "broken": {"schedule": "61 3 * * *"},
            "never": {"schedule": "0 0 30 2 *"},
        },
    }
    assert job_ages(manifest_jobs(settings)) == {
        "a-db": DAY + 2 * HOUR,
        "b-files": 6 * HOUR + 1800,
    }
    assert job_ages(manifest_jobs({**settings, "timezone": "Mars/Base"})) == {}
    context = backups_context({"backups": settings})
    assert context["jobs"] == f"a-db={DAY + 2 * HOUR} b-files={6 * HOUR + 1800}"
    assert context["ages"] == {"a-db": DAY + 2 * HOUR, "b-files": 6 * HOUR + 1800}
    assert backups_context({"node": {}}) == {}


def test_the_example_declares_its_dump_and_passes_the_rules(data: dict[str, Any]) -> None:
    assert job(data)["schedule"] == "30 4 * * *"
    assert backups_problems(manifest_from(data)) == []
    assert rules_hit(data) == set()


@pytest.mark.parametrize(
    ("setting", "value"),
    [
        ("jobs", {}),
        ("jobs", {"Acme": {"schedule": "0 4 * * *"}}),
        ("jobs", {"9db": {"schedule": "0 4 * * *"}}),
        ("jobs", {"acme-db": {}}),
        ("jobs", {"acme-db": {"schedule": "@daily"}}),
        ("jobs", {"acme-db": {"schedule": "0 4 * * MON"}}),
        ("jobs", {"acme-db": {"schedule": "0  4 * * *"}}),
        ("jobs", {"acme-db": {"schedule": "0 4 * * *", "grace": "2d"}}),
        ("jobs", {"acme-db": {"schedule": "0 4 * * *", "grace": "0h"}}),
        ("jobs", {"acme-db": {"schedule": "0 4 * * *", "command": "pg_dump"}}),
        ("jobs", {"acme-db": {"schedule": "0 4 * * *", "output": "/var/backups/a.dump"}}),
        ("jobs", {"acme-db": {"schedule": "0 4 * * *", "command": "x", "output": "a.dump"}}),
        ("jobs", {"acme-db": {"schedule": "0 4 * * *", "command": "x", "output": '/a".dump'}}),
        ("jobs", {"acme-db": {"schedule": "0 4 * * *", "command": "x", "output": "/a`id`"}}),
        ("jobs", {"acme-db": {"schedule": "0 4 * * *", "command": "x", "output": "/a\\b"}}),
        ("jobs", {"acme-db": {"schedule": "0 4 * * *", "command": "a\nb", "output": "/a"}}),
        ("jobs", {"acme-db": {"schedule": "0 4 * * *", "command": "", "output": "/a"}}),
        ("interval", 10),
        ("timezone", "Europe Berlin"),
        ("unknown", True),
    ],
)
def test_settings_outside_the_schema_are_rejected(
    data: dict[str, Any], setting: str, value: Any
) -> None:
    backups(data)[setting] = value
    assert rules_hit(data) == {"module-settings"}


def test_a_host_without_jobs_of_its_own_needs_no_textfile_directory(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    data["hosts"]["edge"]["modules"]["backups"] = {}
    del data["hosts"]["edge"]["modules"]["node"]["textfile_dir"]
    del data["services"]["acme-db"]["data"]
    assert rules_hit(data) == set()
    assert services(data, modules)["backup-jobs"]["environment"]["AYE_BACKUP_JOBS"] == ""


@pytest.mark.parametrize("schedule", ["60 4 * * *", "0 4 31 2 *", "5-1 * * * *", "*/0 4 * * *"])
def test_a_schedule_that_is_not_cron_or_never_repeats_fails_backup_schedule(
    data: dict[str, Any], schedule: str
) -> None:
    job(data)["schedule"] = schedule
    assert rules_hit(data) == {"backup-schedule"}
    (problem,) = backups_problems(manifest_from(data))
    assert problem[0] == "hosts.edge.modules.backups.jobs.acme-db.schedule"


def test_an_unknown_zone_is_left_to_the_timezone_rule(data: dict[str, Any]) -> None:
    backups(data)["timezone"] = "Mars/Base"
    assert rules_hit(data) == {"timezone"}


def test_the_jobs_take_the_manifest_zone_without_their_own(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    del backups(data)["timezone"]
    data["timezone"] = "UTC"
    service = services(data, modules)["backup-jobs"]
    assert service["environment"]["AYE_BACKUP_JOBS"] == f"acme-db={DAY + HOUR}"


def test_the_dump_jobs_need_the_host_textfile_directory(data: dict[str, Any]) -> None:
    del data["hosts"]["edge"]["modules"]["node"]["textfile_dir"]
    assert rules_hit(data) == {"backup-textfile"}
    data["hosts"]["edge"]["modules"]["node"] = {}
    assert rules_hit(data) == {"backup-textfile"}


def test_backups_needs_node(data: dict[str, Any]) -> None:
    del data["hosts"]["edge"]["modules"]["node"]
    assert "module-requires" in rules_hit(data)


def services(data: dict[str, Any], modules: dict[str, Module]) -> dict[str, Any]:
    result: dict[str, Any] = host_compose(manifest_from(data), "edge", modules)["services"]
    return result


def test_the_declaration_service_writes_into_the_host_textfile_directory_without_privileges(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    service = services(data, modules)["backup-jobs"]
    assert tag_of(service["image"]) == pinned("backups", "busybox")
    assert service["command"] == ["sh", "/backups/backup_jobs.sh"]
    assert service["network_mode"] == "none"
    assert service["read_only"] is True
    assert service["cap_drop"] == ["ALL"]
    assert "ports" not in service
    assert service["environment"] == {
        "AYE_TEXTFILE_DIR": "/textfile",
        "AYE_BACKUP_JOBS": f"acme-db={26 * HOUR}",
        "AYE_BACKUP_RPO": f"acme-db={26 * HOUR}",
        "AYE_BACKUP_INTERVAL": "300",
    }
    assert "/var/lib/ayeops/textfile:/textfile" in service["volumes"]
    assert "./backups/backup_jobs.sh:/backups/backup_jobs.sh:ro" in service["volumes"]
    assert "-mmin -16" in service["healthcheck"]["test"][1]


def test_the_wrapper_and_the_declaration_ship_next_to_the_compose_file(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    tree = render_tree(manifest_from(data), modules)
    assert tree["edge/backups/backup_run.sh"] == RUN.read_text(encoding="utf-8")
    assert tree["edge/backups/backup_jobs.sh"] == JOBS.read_text(encoding="utf-8")
    assert "central/backups/backup_run.sh" not in tree


def backup_rules(data: dict[str, Any], modules: dict[str, Module]) -> dict[str, Any]:
    groups = grafana_rules(manifest_from(data), modules)["groups"]
    return {r["uid"]: r for g in groups if g["name"] == "backups" for r in g["rules"]}


def test_a_failed_run_warns_and_a_missing_run_is_critical_also_on_silence(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    found = backup_rules(data, modules)
    assert set(found) == {"aye-backup-failed", "aye-backup-missing"}
    failed, missing = found["aye-backup-failed"], found["aye-backup-missing"]
    assert failed["labels"]["severity"] == "warning"
    assert failed["noDataState"] == "OK"
    assert missing["labels"]["severity"] == "critical"
    assert missing["noDataState"] == "Alerting"
    for rule in found.values():
        assert rule["annotations"]["runbook_url"].endswith(".md")
        assert "on (host, project, group, backup)" in rule["data"][0]["model"]["expr"]
        assert "ayeops_backup_max_age_seconds" in rule["data"][0]["model"]["expr"]


def test_the_missing_run_counts_from_the_last_success_or_else_from_the_declaration(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    expr = backup_rules(data, modules)["aye-backup-missing"]["data"][0]["model"]["expr"]
    success = expr.index("ayeops_backup_last_success_timestamp_seconds")
    fallback = expr.index(" or ")
    declared = expr.index("ayeops_backup_declared_timestamp_seconds")
    assert success < fallback < declared
    assert "time() - (" in expr
    assert "> bool on (host, project, group, backup)" in expr
    failed = backup_rules(data, modules)["aye-backup-failed"]["data"][0]["model"]["expr"]
    assert "ayeops_backup_last_run_success == bool 0" in failed
    assert ") and on (host, project, group, backup) ayeops_backup_max_age_seconds" in failed


def test_without_the_module_no_backup_rule_is_rendered(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    del data["hosts"]["edge"]["modules"]["backups"]
    assert backup_rules(data, modules) == {}


def test_both_alerts_have_a_runbook() -> None:
    root = Path(__file__).resolve().parents[1] / "docs" / "runbooks"
    assert (root / "backup-failed.md").is_file()
    assert (root / "backup-missing.md").is_file()


def shell() -> str:
    found = shutil.which("sh")
    if found is None:
        pytest.skip("no POSIX shell")
    return found


def metrics(path: Path) -> dict[str, str]:
    found = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        key, _, value = line.rpartition(" ")
        found[key] = value
    return found


def wrapped(
    tmp_path: Path, *command: str, output: Path | None = None, name: str = "acme-db"
) -> subprocess.CompletedProcess[str]:
    textfile = tmp_path / "textfile"
    textfile.mkdir(exist_ok=True)
    out = output or tmp_path / "acme.dump"
    argv = [shell(), str(RUN), "-d", textfile.as_posix(), "-o", out.as_posix(), name, *command]
    return subprocess.run(argv, capture_output=True, text=True, timeout=WAIT, check=False)


def job_file(tmp_path: Path, name: str = "acme-db") -> Path:
    return tmp_path / "textfile" / f"ayeops_backup_job_{name}.prom"


def key(metric: str, name: str = "acme-db") -> str:
    return f'ayeops_backup_{metric}{{backup="{name}"}}'


def writes(path: Path, text: str = "PGDMP-dump") -> list[str]:
    return [shell(), "-c", f'printf %s "{text}" > "$1"', "dump", path.as_posix()]


def test_a_successful_run_reports_start_end_success_size_and_duration(tmp_path: Path) -> None:
    before = int(time.time())
    out = tmp_path / "acme.dump"
    result = wrapped(tmp_path, *writes(out))
    after = int(time.time())
    assert result.returncode == 0, result.stderr
    found = metrics(job_file(tmp_path))
    start = int(found[key("last_start_timestamp_seconds")])
    end = int(found[key("last_end_timestamp_seconds")])
    assert before <= start <= end <= after
    assert found[key("last_duration_seconds")] == str(end - start)
    assert found[key("last_size_bytes")] == "10"
    assert found[key("last_exit_code")] == "0"
    assert found[key("last_run_success")] == "1"
    assert found[key("running")] == "0"
    assert found[key("last_success_timestamp_seconds")] == str(end)
    assert len(found) == 8
    assert not list((tmp_path / "textfile").glob("*.prom.*"))


def test_a_failed_run_keeps_the_last_success_and_passes_the_exit_code_on(tmp_path: Path) -> None:
    out = tmp_path / "acme.dump"
    assert wrapped(tmp_path, *writes(out)).returncode == 0
    first = metrics(job_file(tmp_path))[key("last_success_timestamp_seconds")]
    result = wrapped(tmp_path, shell(), "-c", "exit 3")
    assert result.returncode == 3
    assert "acme-db failed with exit code 3" in result.stderr
    found = metrics(job_file(tmp_path))
    assert found[key("last_run_success")] == "0"
    assert found[key("last_exit_code")] == "3"
    assert found[key("last_success_timestamp_seconds")] == first
    assert found[key("running")] == "0"


def test_a_job_that_never_succeeded_has_no_last_success(tmp_path: Path) -> None:
    result = wrapped(tmp_path, shell(), "-c", "exit 2")
    assert result.returncode == 2
    found = metrics(job_file(tmp_path))
    assert found[key("last_run_success")] == "0"
    assert key("last_success_timestamp_seconds") not in found
    assert found[key("last_size_bytes")] == "0"


def test_exit_zero_without_output_is_a_failed_run(tmp_path: Path) -> None:
    result = wrapped(tmp_path, shell(), "-c", "true")
    assert result.returncode == 1
    assert "left no fresh, non-empty" in result.stderr
    assert metrics(job_file(tmp_path))[key("last_run_success")] == "0"


def test_an_empty_output_is_a_failed_run(tmp_path: Path) -> None:
    out = tmp_path / "acme.dump"
    result = wrapped(tmp_path, shell(), "-c", ': > "$1"', "dump", out.as_posix())
    assert result.returncode == 1
    found = metrics(job_file(tmp_path))
    assert found[key("last_run_success")] == "0"
    assert found[key("last_size_bytes")] == "0"


def test_an_old_output_left_from_an_earlier_run_is_not_a_success(tmp_path: Path) -> None:
    out = tmp_path / "acme.dump"
    out.write_text("PGDMP-old", encoding="utf-8")
    past = time.time() - 3600
    os.utime(out, (past, past))
    result = wrapped(tmp_path, shell(), "-c", "true")
    assert result.returncode == 1
    found = metrics(job_file(tmp_path))
    assert found[key("last_run_success")] == "0"
    assert found[key("last_size_bytes")] == "9"


def test_a_directory_output_counts_its_files_and_any_new_file_in_it(tmp_path: Path) -> None:
    out = tmp_path / "base"
    out.mkdir()
    old = out / "old.tar"
    old.write_bytes(b"x" * 5000)
    past = time.time() - 3600
    os.utime(old, (past, past))
    os.utime(out, (past, past))
    result = wrapped(tmp_path, *writes(out / "new.tar", "y" * 3000), output=out)
    assert result.returncode == 0, result.stderr
    found = metrics(job_file(tmp_path))
    assert found[key("last_run_success")] == "1"
    size = int(found[key("last_size_bytes")])
    assert size % 1024 == 0
    assert size >= 8000


def test_a_running_job_reports_running_and_keeps_the_previous_run(tmp_path: Path) -> None:
    out = tmp_path / "acme.dump"
    assert wrapped(tmp_path, *writes(out)).returncode == 0
    previous = metrics(job_file(tmp_path))
    seen = tmp_path / "seen.prom"
    peek = 'cat "$1" > "$2" && printf PGDMP > "$3"'
    command = [shell(), "-c", peek, "peek", job_file(tmp_path).as_posix(), seen.as_posix()]
    assert wrapped(tmp_path, *command, out.as_posix()).returncode == 0
    during = metrics(seen)
    assert during[key("running")] == "1"
    assert during[key("last_run_success")] == "1"
    assert (
        during[key("last_success_timestamp_seconds")]
        == previous[key("last_success_timestamp_seconds")]
    )
    assert int(during[key("last_start_timestamp_seconds")]) >= int(
        previous[key("last_start_timestamp_seconds")]
    )
    lines = seen.read_text(encoding="utf-8").splitlines()
    assert [line.rpartition(" ")[0] for line in lines].count(key("running")) == 1
    assert len(lines) == len(during) == 8


def test_the_dump_still_runs_when_the_metrics_cannot_be_written(tmp_path: Path) -> None:
    out = tmp_path / "acme.dump"
    gone = tmp_path / "missing"
    argv = [shell(), str(RUN), "-d", gone.as_posix(), "-o", out.as_posix(), "acme-db"]
    result = subprocess.run(
        [*argv, *writes(out)], capture_output=True, text=True, timeout=WAIT, check=False
    )
    assert result.returncode == 75
    assert "is not a writable directory" in result.stderr
    assert out.read_text(encoding="utf-8") == "PGDMP-dump"
    assert not gone.exists()


def test_the_textfile_directory_comes_from_the_environment_without_d(tmp_path: Path) -> None:
    textfile = tmp_path / "env-textfile"
    textfile.mkdir()
    out = tmp_path / "acme.dump"
    env = {**os.environ, "AYE_TEXTFILE_DIR": textfile.as_posix()}
    argv = [shell(), str(RUN), "-o", out.as_posix(), "acme-db", *writes(out)]
    result = subprocess.run(
        argv, capture_output=True, text=True, timeout=WAIT, check=False, env=env
    )
    assert result.returncode == 0, result.stderr
    assert (textfile / "ayeops_backup_job_acme-db.prom").is_file()


@pytest.mark.parametrize("name", ["Acme", "9db", "acme db", "a/b", "a.b", "-x"])
def test_a_name_that_cannot_be_a_label_and_a_file_is_refused(tmp_path: Path, name: str) -> None:
    result = wrapped(tmp_path, shell(), "-c", "true", name=name)
    assert result.returncode == 64
    assert not list((tmp_path / "textfile").iterdir())


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["acme-db"],
        ["-o", "/tmp/x", "acme-db"],
        ["acme-db", "true"],
        ["-x", "-o", "/tmp/x", "acme-db", "true"],
    ],
)
def test_without_an_output_a_name_and_a_command_the_wrapper_prints_its_usage(
    argv: list[str],
) -> None:
    result = subprocess.run(
        [shell(), str(RUN), *argv], capture_output=True, text=True, timeout=WAIT, check=False
    )
    assert result.returncode == 64
    assert "usage: backup_run.sh" in result.stderr


def declare(
    tmp_path: Path, jobs: str, existing: str | None = None, rpo: str = ""
) -> tuple[subprocess.CompletedProcess[str], dict[str, str]]:
    textfile = tmp_path / "textfile"
    textfile.mkdir(exist_ok=True)
    target = textfile / "ayeops_backups.prom"
    if existing is not None:
        target.write_text(existing, encoding="utf-8", newline="\n")
    env = {
        **os.environ,
        "AYE_TEXTFILE_DIR": textfile.as_posix(),
        "AYE_BACKUP_JOBS": jobs,
        "AYE_BACKUP_RPO": rpo,
        "AYE_BACKUP_ONCE": "1",
    }
    result = subprocess.run(
        [shell(), str(JOBS)], capture_output=True, text=True, timeout=WAIT, check=False, env=env
    )
    return result, metrics(target)


def test_the_declaration_lists_every_job_with_its_age_and_when_it_was_declared(
    tmp_path: Path,
) -> None:
    before = int(time.time())
    result, found = declare(tmp_path, "acme-db=93600 web_files=23400")
    assert result.returncode == 0, result.stderr
    assert found[key("max_age_seconds")] == "93600"
    assert found[key("max_age_seconds", "web_files")] == "23400"
    assert int(found[key("declared_timestamp_seconds")]) >= before
    assert len(found) == 4
    assert not list((tmp_path / "textfile").glob("*.tmp"))


def test_the_declaration_keeps_the_first_declared_time_and_drops_removed_jobs(
    tmp_path: Path,
) -> None:
    existing = (
        'ayeops_backup_max_age_seconds{backup="acme-db"} 90000\n'
        'ayeops_backup_declared_timestamp_seconds{backup="acme-db"} 1000\n'
        'ayeops_backup_declared_timestamp_seconds{backup="acme"} 2000\n'
        'ayeops_backup_declared_timestamp_seconds{backup="gone"} 3000\n'
    )
    _, found = declare(tmp_path, "acme-db=93600 acme=7200", existing)
    assert found[key("max_age_seconds")] == "93600"
    assert found[key("declared_timestamp_seconds")] == "1000"
    assert found[key("declared_timestamp_seconds", "acme")] == "2000"
    assert key("declared_timestamp_seconds", "gone") not in found


def test_the_declaration_skips_entries_that_are_not_a_name_and_seconds(tmp_path: Path) -> None:
    result, found = declare(tmp_path, "Bad=10 ok=abc good=60 =5")
    assert set(found) == {key("max_age_seconds", "good"), key("declared_timestamp_seconds", "good")}
    assert result.stderr.count("skipped") == 3


def test_the_declaration_carries_the_rpo_a_service_declares_for_a_job(tmp_path: Path) -> None:
    result, found = declare(tmp_path, "acme-db=93600", rpo="acme-db=90000 Bad=5 web=x")
    assert found[key("rpo_seconds")] == "90000"
    assert len(found) == 3
    assert result.stderr.count("skipped") == 2
    assert "skipped web, its rpo x is not a number of seconds" in result.stderr


def test_the_scripts_write_the_metrics_the_alerts_read() -> None:
    alerts = (MODULES_DIR / "backups" / "module.yaml").read_text(encoding="utf-8")
    scripts = RUN.read_text(encoding="utf-8") + JOBS.read_text(encoding="utf-8")
    read = set(re.findall(r"ayeops_backup_[a-z_]+", alerts))
    assert read
    assert read <= set(re.findall(r"ayeops_backup_[a-z_]+", scripts))


def test_an_output_written_within_the_second_the_run_started_is_fresh(tmp_path: Path) -> None:
    out = tmp_path / "acme.dump"
    script = 'printf PGDMP > "$1" && touch -d "@$(date +%s)" "$1"'
    result = wrapped(tmp_path, shell(), "-c", script, "dump", out.as_posix())
    assert result.returncode == 0, result.stderr
    assert metrics(job_file(tmp_path))[key("last_run_success")] == "1"


@pytest.mark.parametrize(
    ("schedule", "zone", "lines"),
    [
        ("30 4 * * *", "Europe/Berlin", ["*-*-* 04:30:00 Europe/Berlin"]),
        ("0 */6 * * *", "UTC", ["*-*-* 00,06,12,18:00:00 UTC"]),
        ("*/15 * * * *", "UTC", ["*-*-* *:00,15,30,45:00 UTC"]),
        ("15 2 * * 1-5", "Asia/Tokyo", ["Mon,Tue,Wed,Thu,Fri *-*-* 02:15:00 Asia/Tokyo"]),
        ("0 0 * * 0", "UTC", ["Sun *-*-* 00:00:00 UTC"]),
        ("0 0 * * 6,7", "UTC", ["Sat,Sun *-*-* 00:00:00 UTC"]),
        ("0 4 * * 1-7", "UTC", ["*-*-* 04:00:00 UTC"]),
        ("0 3 1 * *", "UTC", ["*-*-01 03:00:00 UTC"]),
        ("5 0 1 1,7 *", "UTC", ["*-01,07-01 00:05:00 UTC"]),
        ("0 0 1,15 * 1", "UTC", ["*-*-01,15 00:00:00 UTC", "Mon *-*-* 00:00:00 UTC"]),
        ("0 0 1-31 * 1", "UTC", ["*-*-* 00:00:00 UTC", "Mon *-*-* 00:00:00 UTC"]),
        ("0 0 1-31 * 0-6", "UTC", ["*-*-* 00:00:00 UTC"]),
        ("0 0 */10 * 1", "UTC", ["Mon *-*-01,11,21,31 00:00:00 UTC"]),
        ("0 0 1 * */3", "UTC", ["Wed,Sat,Sun *-*-01 00:00:00 UTC"]),
    ],
)
def test_a_cron_schedule_becomes_on_calendar_lines_in_its_zone(
    schedule: str, zone: str, lines: list[str]
) -> None:
    assert on_calendar(parse_cron(schedule), zone) == lines


NAMES = ("Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat")


def calendar_values(text: str) -> set[int] | None:
    return None if text == "*" else {int(value) for value in text.split(",")}


def calendar_fires(lines: list[str], day: date) -> set[int]:
    minutes: set[int] = set()
    for line in lines:
        words = line.split()
        weekdays = set(words[0].split(",")) if len(words) == 4 else None
        _, month_text, day_text = words[-3].split("-")
        hour_text, minute_text, second = words[-2].split(":")
        assert second == "00"
        months, days = calendar_values(month_text), calendar_values(day_text)
        if weekdays is not None and NAMES[day.isoweekday() % 7] not in weekdays:
            continue
        if (months is not None and day.month not in months) or (
            days is not None and day.day not in days
        ):
            continue
        hours = calendar_values(hour_text) or set(range(24))
        at = calendar_values(minute_text) or set(range(60))
        minutes |= {hour * 60 + minute for hour in hours for minute in at}
    return minutes


@pytest.mark.parametrize(
    "schedule",
    [
        "30 4 * * *",
        "0 */6 * * *",
        "15 2 * * 1-5",
        "0 0 1,15 * 1",
        "0 0 */2 * 1",
        "0 0 1-7 * */2",
        "10 1 13 * 5",
        "0 12 * 2-4 0",
        "0 0 29 2 *",
        "0 9 1-3,20 1,6,12 1-3",
    ],
)
def test_the_timer_fires_on_exactly_the_days_and_times_cron_does(schedule: str) -> None:
    cron = parse_cron(schedule)
    lines = on_calendar(cron, "UTC")
    for offset in range(800):
        day = date(2027, 1, 1) + timedelta(days=offset)
        expected = set(cron.times()) if cron.matches(day) else set()
        assert calendar_fires(lines, day) == expected, (schedule, day)


def units(data: dict[str, Any], modules: dict[str, Module]) -> dict[str, str]:
    tree = render_tree(manifest_from(data), modules)
    return {
        path.removeprefix("edge/"): text
        for path, text in tree.items()
        if "/jobs/" in path or "/systemd/" in path
    }


def test_a_job_with_a_command_renders_a_timer_a_service_and_a_job_script(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    found = units(data, modules)
    unit = "backups/systemd/ayeops-edge-backup-acme-db"
    assert set(found) == {f"{unit}.service", f"{unit}.timer", "backups/jobs/acme-db.sh"}
    timer = found[f"{unit}.timer"].splitlines()
    assert "OnCalendar=*-*-* 04:30:00 Europe/Berlin" in timer
    assert "Persistent=true" in timer
    assert "Unit=ayeops-edge-backup-acme-db.service" in timer
    assert timer[-2:] == ["[Install]", "WantedBy=timers.target"]
    service = found[f"{unit}.service"].splitlines()
    assert "Type=oneshot" in service
    assert "ExecStart=/bin/sh /opt/ayeops/edge/backups/jobs/acme-db.sh" in service
    assert not [line for line in service if line.startswith(("User=", "Environment"))]
    script = found["backups/jobs/acme-db.sh"].splitlines()
    assert script[:2] == ["#!/bin/sh", "set -eu"]
    assert script[2] == 'AYE_BACKUP_OUTPUT="/var/backups/acme/acme-$(date -u +%Y%m%dT%H%MZ).dump"'
    assert script[3] == "export AYE_BACKUP_OUTPUT"
    assert script[4].startswith(
        "exec /bin/sh /opt/ayeops/edge/backups/backup_run.sh -d /var/lib/ayeops/textfile"
        ' -o "$AYE_BACKUP_OUTPUT" acme-db /bin/sh -c \'runuser -u postgres -- pg_dump'
    )
    assert len(script) == 5
    assert HOST_ROOT == REMOTE_ROOT


def test_the_example_renders_the_units_it_commits(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    rendered = Path(__file__).resolve().parents[1] / "examples" / "rendered" / "edge"
    for path, text in units(data, modules).items():
        assert (rendered / path).read_text(encoding="utf-8") == text


def test_a_job_without_a_command_and_a_host_without_jobs_render_no_unit(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    del job(data)["command"]
    del job(data)["output"]
    assert units(data, modules) == {}
    assert backup_units("edge", {"node": {}}) == {}
    assert (
        backup_units(
            "edge",
            {
                "backups": {
                    "jobs": {"x": {"schedule": "61 * * * *", "command": "c", "output": "/o"}}
                }
            },
        )
        == {}
    )


def test_the_timer_takes_the_manifest_zone_when_backups_names_none(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    del backups(data)["timezone"]
    data["timezone"] = "Asia/Tokyo"
    timer = units(data, modules)["backups/systemd/ayeops-edge-backup-acme-db.timer"]
    assert "OnCalendar=*-*-* 04:30:00 Asia/Tokyo\n" in timer
    assert services(data, modules)["backup-jobs"]["environment"]["AYE_BACKUP_JOBS"] == (
        f"acme-db={DAY + HOUR}"
    )


def test_every_job_on_the_host_gets_its_own_units_and_the_textfile_directory(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    data["hosts"]["edge"]["modules"]["node"]["textfile_dir"] = "/srv/text file"
    backups(data)["jobs"]["files"] = {
        "schedule": "0 1 1,15 * 1",
        "output": "/var/backups/files",
        "command": "tar -C /srv -cf /var/backups/files/srv.tar data",
    }
    found = units(data, modules)
    assert unit_files(found) == [
        "backups/systemd/ayeops-edge-backup-acme-db.service",
        "backups/systemd/ayeops-edge-backup-acme-db.timer",
        "backups/systemd/ayeops-edge-backup-files.service",
        "backups/systemd/ayeops-edge-backup-files.timer",
    ]
    timer = found["backups/systemd/ayeops-edge-backup-files.timer"]
    assert (
        "OnCalendar=*-*-01,15 01:00:00 Europe/Berlin\nOnCalendar=Mon *-*-* 01:00:00 Europe/Berlin\n"
        in timer
    )
    assert "-d '/srv/text file' -o" in found["backups/jobs/files.sh"]


def test_the_rendered_job_script_runs_the_command_through_the_wrapper(
    tmp_path: Path, data: dict[str, Any], modules: dict[str, Module]
) -> None:
    dumps = tmp_path / "dumps"
    dumps.mkdir()
    job(data)["output"] = f"{dumps.as_posix()}/acme-$(date -u +%Y%m%d).dump"
    job(data)["command"] = 'printf "%s %s" \'PG"DMP\' "$(echo "it\'s")" > "$AYE_BACKUP_OUTPUT"'
    data["hosts"]["edge"]["modules"]["node"]["textfile_dir"] = (tmp_path / "textfile").as_posix()
    (tmp_path / "textfile").mkdir()
    script = units(data, modules)["backups/jobs/acme-db.sh"]
    script = script.replace(f"{HOST_ROOT}/edge/backups/backup_run.sh", RUN.as_posix())
    path = tmp_path / "acme-db.sh"
    path.write_text(script, encoding="utf-8", newline="\n")
    result = subprocess.run(
        [shell(), path.as_posix()], capture_output=True, text=True, timeout=WAIT, check=False
    )
    assert result.returncode == 0, result.stderr
    (dump,) = dumps.iterdir()
    assert re.fullmatch(r"acme-[0-9]{8}\.dump", dump.name)
    assert dump.read_text(encoding="utf-8") == "PG\"DMP it's"
    found = metrics(job_file(tmp_path))
    assert found[key("last_run_success")] == "1"
    assert found[key("last_size_bytes")] == str(len("PG\"DMP it's"))


def test_a_failing_command_fails_the_unit_and_reports_the_run(
    tmp_path: Path, data: dict[str, Any], modules: dict[str, Module]
) -> None:
    job(data)["output"] = f"{tmp_path.as_posix()}/never.dump"
    job(data)["command"] = "exit 3"
    data["hosts"]["edge"]["modules"]["node"]["textfile_dir"] = (tmp_path / "textfile").as_posix()
    (tmp_path / "textfile").mkdir()
    script = units(data, modules)["backups/jobs/acme-db.sh"]
    path = tmp_path / "acme-db.sh"
    path.write_text(
        script.replace(f"{HOST_ROOT}/edge/backups/backup_run.sh", RUN.as_posix()),
        encoding="utf-8",
        newline="\n",
    )
    result = subprocess.run(
        [shell(), path.as_posix()], capture_output=True, text=True, timeout=WAIT, check=False
    )
    assert result.returncode == 3
    found = metrics(job_file(tmp_path))
    assert found[key("last_run_success")] == "0"
    assert found[key("last_exit_code")] == "3"


def test_the_built_in_dumps_are_declared_with_their_utc_hour(data: dict[str, Any]) -> None:
    settings = {"secrets": {"backup_hour": 5}, "errors": {"backup_hour": 4}, "node": {}}
    found = builtin_jobs(settings)
    assert found == [
        Job("errors", "0 4 * * *", "UTC", "2h", module="errors", service="glitchtip-backup"),
        Job("secrets", "0 5 * * *", "UTC", "2h", module="secrets", service="infisical-backup"),
    ]
    assert job_ages(found) == {"errors": 26 * HOUR, "secrets": 26 * HOUR}
    context = backups_context(
        {
            **settings,
            "backups": {"timezone": "UTC", "jobs": {"a": {"schedule": "0 1 * * *", "grace": "1h"}}},
        }
    )
    assert context["jobs"] == f"a={DAY + HOUR} errors={26 * HOUR} secrets={26 * HOUR}"
    assert backups_context(settings) == {}


def test_a_job_named_like_a_built_in_dump_on_its_host_fails_backup_name(
    data: dict[str, Any],
) -> None:
    edge = data["hosts"]["edge"]["modules"]
    edge["secrets"] = {"url": "https://secrets.acme.example"}
    backups(data)["jobs"]["secrets"] = {"schedule": "0 2 * * *"}
    assert "backup-name" in rules_hit(data)
    problems = [p for p in backups_problems(manifest_from(data)) if p[1] == "backup-name"]
    assert problems == [
        (
            "hosts.edge.modules.backups.jobs.secrets",
            "backup-name",
            "secrets is the built-in dump of the secrets module (infisical-backup) on this host:"
            " pick another name",
        )
    ]
    del edge["secrets"]
    assert "backup-name" not in rules_hit(data)


def test_a_command_without_an_output_renders_no_unit() -> None:
    job = {"schedule": "0 1 * * *", "command": "pg_dump acme"}
    assert backup_units("edge", {"backups": {"jobs": {"a": job}}}) == {}
    with_output = {"backups": {"jobs": {"a": dict(job, output="/o")}}}
    assert unit_files(backup_units("edge", with_output)) == [
        "backups/systemd/ayeops-edge-backup-a.service",
        "backups/systemd/ayeops-edge-backup-a.timer",
    ]


METRICS_JOB = Job(
    "metrics", "0 3 * * *", "UTC", "2h", module="metrics", service="victoriametrics-backup"
)


def test_a_built_in_dump_with_when_exists_only_while_its_setting_is_true() -> None:
    assert builtin_jobs({"metrics": {"backup": False, "backup_hour": 3}}) == []
    assert builtin_jobs({"metrics": {"backup_hour": 3}}) == []
    assert builtin_jobs({"metrics": {"backup": True, "backup_hour": 3}}) == [METRICS_JOB]
    secrets = Job("secrets", "0 5 * * *", "UTC", "2h", module="secrets", service="infisical-backup")
    later = {"metrics": {"backup": False, "backup_hour": 3}, "secrets": {"backup_hour": 5}}
    assert builtin_jobs(later) == [secrets]
    both = {"metrics": {"backup": True, "backup_hour": 3}, "secrets": {"backup_hour": 5}}
    assert builtin_jobs(both) == [METRICS_JOB, secrets]
    assert job_ages([METRICS_JOB]) == {"metrics": 26 * HOUR}


FAKE_JOBS = [
    {
        "name": "fake-a",
        "service": "fake-dump",
        "schedule": "0 {{ settings.hour }} * * *",
        "timezone": "UTC",
        "grace": "30m",
        "when": "on",
    },
    {
        "name": "fake-b",
        "service": "fake-dump",
        "schedule": "0 1 * * *",
        "timezone": "Europe/Berlin",
    },
]


def test_each_built_in_job_keeps_its_own_grace_zone_and_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = Module("fake", MODULES_DIR / "fake", {"backup_jobs": FAKE_JOBS})
    monkeypatch.setattr("ayeops.backups.load_modules", lambda: {"fake": fake})
    second = Job("fake-b", "0 1 * * *", "Europe/Berlin", "2h", module="fake", service="fake-dump")
    first = Job("fake-a", "0 4 * * *", "UTC", "30m", module="fake", service="fake-dump")
    assert builtin_jobs({"aaa": {}, "fake": {"on": False, "hour": 4}}) == [second]
    assert builtin_jobs({"aaa": {}, "fake": {"on": True, "hour": 4}}) == [first, second]
    assert builtin_jobs({"zzz": {}}) == []


def modules_of(data: dict[str, Any], host: str) -> dict[str, Any]:
    modules: dict[str, Any] = data["hosts"][host]["modules"]
    return modules


def with_metrics_backup(data: dict[str, Any]) -> dict[str, Any]:
    modules_of(data, "central")["metrics"]["backup"] = True
    modules_of(data, "central")["backups"] = {}
    return data


AGENT_BACKUP = "only role server keeps a store to back up; an agent ships its samples away"
BACKUPS_NEEDED = (
    "the archive reports as the backup job metrics, declared and alerted on by the backups"
    " module: add backups: {} to this host"
)
NO_BACKUP = (
    "this host backs up its dumps but not its metrics store: set backup: true for a daily"
    " snapshot archive and a weekly restore test of it"
)


def test_the_example_has_no_metrics_backup_finding_with_or_without_the_backup(
    data: dict[str, Any],
) -> None:
    assert "backup" not in modules_of(data, "central")["metrics"]
    assert metrics_backup_problems(manifest_from(data)) == []
    assert metrics_backup_warnings(manifest_from(data)) == []
    with_metrics_backup(data)
    assert metrics_backup_problems(manifest_from(data)) == []
    assert metrics_backup_warnings(manifest_from(data)) == []
    assert "metrics-backup" not in rules_hit(data)
    assert "metrics-backup" not in warnings_hit(data)


def test_the_metrics_backup_needs_the_backups_module_on_its_host(data: dict[str, Any]) -> None:
    modules_of(data, "central")["metrics"]["backup"] = True
    assert metrics_backup_problems(manifest_from(data)) == [
        ("hosts.central.modules.metrics.backup", "metrics-backup", BACKUPS_NEEDED)
    ]
    assert "metrics-backup" in rules_hit(data)


def test_an_agent_has_no_store_to_back_up(data: dict[str, Any]) -> None:
    modules_of(data, "edge")["metrics"]["backup"] = True
    assert metrics_backup_problems(manifest_from(data)) == [
        ("hosts.edge.modules.metrics.backup", "metrics-backup", AGENT_BACKUP)
    ]
    with_metrics_backup(data)
    del modules_of(data, "edge")["backups"]
    del modules_of(data, "edge")["offsite"]
    assert metrics_backup_problems(manifest_from(data)) == [
        ("hosts.edge.modules.metrics.backup", "metrics-backup", AGENT_BACKUP),
        ("hosts.edge.modules.metrics.backup", "metrics-backup", BACKUPS_NEEDED),
    ]


def test_a_server_that_backs_up_its_dumps_but_not_its_store_warns(data: dict[str, Any]) -> None:
    modules_of(data, "central")["backups"] = {}
    assert metrics_backup_problems(manifest_from(data)) == []
    assert metrics_backup_warnings(manifest_from(data)) == [
        ("hosts.central.modules.metrics", "metrics-backup", NO_BACKUP)
    ]
    assert "metrics-backup" in warnings_hit(data)
    assert "metrics-backup" not in rules_hit(data)
    modules_of(data, "central")["metrics"]["backup"] = False
    assert len(metrics_backup_warnings(manifest_from(data))) == 1
    edge = modules_of(data, "edge")["metrics"]
    edge["role"] = "server"
    del edge["remote_write"]
    assert [path for path, _, _ in metrics_backup_warnings(manifest_from(data))] == [
        "hosts.central.modules.metrics",
        "hosts.edge.modules.metrics",
    ]
    del modules_of(data, "central")["backups"]
    assert [path for path, _, _ in metrics_backup_warnings(manifest_from(data))] == [
        "hosts.edge.modules.metrics"
    ]


def test_only_hosts_with_the_metrics_module_are_looked_at(data: dict[str, Any]) -> None:
    with_metrics_backup(data)
    data["hosts"]["bare"] = {"arch": "amd64", "modules": {"node": {}}}
    found = metrics_settings(manifest_from(data))
    assert [host for host, _, _ in found] == ["central", "edge"]
    _, modules, settings = found[0]
    assert "backups" in modules
    assert (settings["role"], settings["backup"], settings["backup_hour"]) == ("server", True, 2)
    assert found[1][2]["backup"] is False
