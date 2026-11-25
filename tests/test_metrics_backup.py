from __future__ import annotations

import calendar
import io
import os
import shutil
import subprocess
import tarfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import yaml

from ayeops.hostinit import module_directories
from ayeops.registry import MODULES_DIR, Module
from ayeops.render import host_compose
from tests.conftest import (
    EXAMPLE_STAMP,
    WAIT,
    manifest_from,
    pinned,
    render_once,
    rules_hit,
    tag_of,
)

BACKUP = MODULES_DIR / "metrics" / "files" / "backup.sh"
RUN = MODULES_DIR / "backups" / "files" / "backup_run.sh"
RULES = "central/grafana/provisioning/alerting/rules.yaml"
SERVICE = "victoriametrics-backup"
DAY = 86400
STAMP = "20260105T0400Z"
STAMPED = calendar.timegm((2026, 1, 5, 4, 0, 0))
EPOCH = STAMPED + 120
SNAPSHOT = "20260105040010-188A2B3C4D5E6F70"
QUERY = "count%28last_over_time%28up%5B1h%5D%29%29"
EXPR = (
    "max by (host, project, group, backup) ((time() > bool"
    " ayeops_backup_restore_test_deadline_timestamp_seconds) + ((ayeops_backup_restore_test_ok =="
    " bool 0) or (ayeops_backup_restore_test_deadline_timestamp_seconds * 0)))"
)


def central(data: dict[str, Any]) -> dict[str, Any]:
    modules: dict[str, Any] = data["hosts"]["central"]["modules"]
    return modules


def services(data: dict[str, Any], modules: dict[str, Module]) -> dict[str, Any]:
    found: dict[str, Any] = host_compose(manifest_from(data), "central", modules)["services"]
    return found


def test_the_backup_is_off_by_default_and_renders_nothing(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    assert modules["metrics"].defaults["backup"] is False
    del central(data)["metrics"]["backup"]
    assert SERVICE not in services(data, modules)
    tree = render_once(manifest_from(data), modules, EXAMPLE_STAMP)
    assert "central/metrics/backup.sh" not in tree
    jobs = services(data, modules)["backup-jobs"]["environment"]["AYE_BACKUP_JOBS"]
    assert jobs == ""
    uids = {
        rule["uid"] for group in yaml.safe_load(tree[RULES])["groups"] for rule in group["rules"]
    }
    assert "aye-metrics-restore-test" not in uids
    assert "aye-backup-missing" in uids
    assert module_directories(manifest_from(data), modules, "central") == ()


def test_the_backup_runs_on_the_pinned_store_image_without_privileges(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    found = services(data, modules)
    backup = found[SERVICE]
    assert backup["image"] == found["victoriametrics"]["image"]
    assert tag_of(backup["image"]) == pinned("metrics", "victoriametrics/victoria-metrics")
    assert backup["entrypoint"] == ["sh", "/backup.sh"]
    assert (backup["read_only"], backup["cap_drop"], backup["init"]) == (True, ["ALL"], True)
    assert backup["security_opt"] == ["no-new-privileges:true"]
    assert backup["tmpfs"] == ["/tmp:size=16m"]
    assert "ports" not in backup
    assert "network_mode" not in backup
    assert backup["depends_on"] == {"victoriametrics": {"condition": "service_healthy"}}
    assert (backup["mem_limit"], backup["cpus"]) == ("512m", 0.5)
    assert backup["volumes"] == [
        "./metrics/backup.sh:/backup.sh:ro",
        "./backups/backup_run.sh:/backup_run.sh:ro",
        "victoriametrics-data:/storage:ro",
        "/var/backups/ayeops/metrics:/backups",
        "node-textfile:/textfile",
    ]
    assert backup["environment"] == {
        "AYE_BACKUP_DIR": "/backups",
        "AYE_STORAGE_DIR": "/storage",
        "AYE_TEXTFILE_DIR": "/textfile",
        "AYE_VM_URL": "http://victoriametrics:8428",
        "AYE_BACKUP_HOUR": "2",
        "AYE_BACKUP_KEEP_DAYS": "2",
        "AYE_RESTORE_TEST_DAYS": "7",
    }
    assert backup["healthcheck"]["test"] == [
        "CMD-SHELL",
        "find /tmp/backup.alive -mmin -15 | grep -q .",
    ]
    compose = host_compose(manifest_from(data), "central", modules)
    assert {"victoriametrics-data", "node-textfile"} <= set(compose["volumes"])


def test_the_settings_reach_the_container_and_the_host_textfile_directory(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    central(data)["metrics"].update(
        backup_hour=5, backup_keep_days=9, backup_dir="/srv/metrics-archives", restore_test_days=3
    )
    central(data)["node"] = {"textfile_dir": "/var/lib/ayeops/textfile"}
    backup = services(data, modules)[SERVICE]
    environment = backup["environment"]
    assert (environment["AYE_BACKUP_HOUR"], environment["AYE_BACKUP_KEEP_DAYS"]) == ("5", "9")
    assert environment["AYE_RESTORE_TEST_DAYS"] == "3"
    assert "/srv/metrics-archives:/backups" in backup["volumes"]
    assert "/var/lib/ayeops/textfile:/textfile" in backup["volumes"]
    compose = host_compose(manifest_from(data), "central", modules)
    assert "node-textfile" not in compose["volumes"]
    directories = module_directories(manifest_from(data), modules, "central")
    assert directories[0][:3] == ("/srv/metrics-archives", "0:0", "0700")


def test_the_archive_is_the_built_in_backup_job_metrics(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    jobs = services(data, modules)["backup-jobs"]["environment"]["AYE_BACKUP_JOBS"]
    assert jobs == f"metrics={DAY + 2 * 3600}"
    tree = render_once(manifest_from(data), modules, EXAMPLE_STAMP)
    assert tree["central/metrics/backup.sh"] == BACKUP.read_text(encoding="utf-8")
    assert tree["central/backups/backup_run.sh"] == RUN.read_text(encoding="utf-8")
    assert modules["metrics"].catalog[SERVICE]["alerts"] == [
        "backup-missing",
        "backup-failed",
        "metrics-restore-test",
    ]


def test_an_agent_renders_no_backup_and_fails_the_rule(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    data["hosts"]["edge"]["modules"]["metrics"]["backup"] = True
    edge = host_compose(manifest_from(data), "edge", modules)
    assert SERVICE not in edge["services"]
    directories = module_directories(manifest_from(data), modules, "edge")
    assert [path for path, *_ in directories] == ["/var/lib/ayeops/textfile"]
    assert "metrics-backup" in rules_hit(data)


def test_the_restore_test_alert_fires_on_a_failure_or_a_passed_deadline_also_on_silence(
    modules: dict[str, Module], data: dict[str, Any]
) -> None:
    (alert,) = [a for a in modules["metrics"].alerts if a["name"] == "metrics-restore-test"]
    assert alert["expr"] == EXPR
    assert (alert["for"], alert["severity"], alert["no_data"]) == ("10m", "warning", "alerting")
    assert (alert["setting"], alert["runbook"]) == ("backup", "metrics-restore-test")
    assert (MODULES_DIR.parents[2] / "docs" / "runbooks" / "metrics-restore-test.md").is_file()
    tree = render_once(manifest_from(data), modules, EXAMPLE_STAMP)
    rules = {
        rule["uid"]: rule
        for group in yaml.safe_load(tree[RULES])["groups"]
        for rule in group["rules"]
    }
    rule = rules["aye-metrics-restore-test"]
    assert (rule["noDataState"], rule["for"]) == ("Alerting", "10m")
    assert EXPR in rule["data"][0]["model"]["expr"]


def test_the_script_writes_the_metrics_the_alert_and_the_catalog_name() -> None:
    text = BACKUP.read_text(encoding="utf-8")
    for name in (
        "ayeops_backup_restore_test_deadline_timestamp_seconds",
        "ayeops_backup_restore_test_ok",
        "ayeops_backup_restore_test_last_success_timestamp_seconds",
    ):
        assert f"{name}{{$label}}" in text


@dataclass
class Box:
    root: Path
    backups: Path
    storage: Path
    textfile: Path
    log: Path
    vm_args: Path

    def archives(self) -> list[str]:
        return sorted(p.name for p in self.backups.iterdir() if p.name.endswith(".tar"))

    def calls(self) -> list[str]:
        if not self.log.exists():
            return []
        return self.log.read_text(encoding="utf-8").splitlines()

    def report(self) -> dict[str, str]:
        path = self.textfile / "ayeops_backup_restore_metrics.prom"
        found = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            key, value = line.rsplit(" ", 1)
            name = key.removesuffix('{backup="metrics"}').removeprefix(
                "ayeops_backup_restore_test_"
            )
            found[name] = value
        return found

    def job(self) -> dict[str, str]:
        path = self.textfile / "ayeops_backup_job_metrics.prom"
        found = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            key, value = line.rsplit(" ", 1)
            found[key.removesuffix('{backup="metrics"}').removeprefix("ayeops_backup_")] = value
        return found


FETCH = """#!/bin/sh
echo "$1" >> "$FAKE_LOG"
case "$1" in
  */snapshot/create)
    if [ -n "${FAKE_CREATE:-}" ]; then printf '%s' "$FAKE_CREATE"; exit 0; fi
    printf '{"status":"ok","snapshot":"%s"}' "$FAKE_SNAPSHOT"
    ;;
  */snapshot/delete*)
    if [ -n "${FAKE_DELETE_FAIL:-}" ]; then exit 1; fi
    rm -rf "$FAKE_STORAGE/snapshots/${1#*snapshot=}"
    printf '{"status":"ok"}'
    ;;
  */snapshot/list)
    if [ -n "${FAKE_LIST_FAIL:-}" ]; then exit 1; fi
    printf '{"status":"ok","snapshots":["%s"]}' "${FAKE_LISTED:-}"
    ;;
  */health)
    if [ -n "${FAKE_UNHEALTHY:-}" ]; then exit 1; fi
    printf OK
    ;;
  */api/v1/query*)
    printf '{"status":"success","data":{"resultType":"vector","result":[%s]}}' "$FAKE_RESULT"
    ;;
esac
"""

VM = """#!/bin/sh
printf '%s\\n' "$@" > "$FAKE_VM_ARGS"
storage=${1#-storageDataPath=}
find "$storage" -type f | sed "s|^$storage/||" | sort >> "$FAKE_VM_ARGS"
if [ -n "${FAKE_VM_EXIT:-}" ]; then
  exit 1
fi
exec sleep 30
"""


def shell() -> str:
    found = shutil.which("sh")
    if found is None:
        pytest.skip("no POSIX shell")
    return found


def box(tmp_path: Path) -> Box:
    found = Box(
        tmp_path,
        tmp_path / "backups",
        tmp_path / "storage",
        tmp_path / "textfile",
        tmp_path / "calls.log",
        tmp_path / "vm.args",
    )
    for directory in (found.backups, found.storage, found.textfile):
        directory.mkdir(exist_ok=True)
    return found


def snapshot(where: Box, name: str = SNAPSHOT) -> Path:
    root = where.storage / "snapshots" / name
    for part, body in (
        ("data/small/2026_01/17A0/values.bin", b"small"),
        ("data/big/2025_12/17B0/values.bin", b"big"),
        ("data/indexdb/2026_01/17C0/items.bin", b"index"),
        ("metadata/minTimestampForCompositeIndex", b"0"),
    ):
        path = root / part
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
    return root


def script(path: Path, text: str) -> str:
    path.write_text(text, encoding="utf-8", newline="\n")
    path.chmod(0o755)
    return path.as_posix()


def result(series: int | None) -> str:
    if series is None:
        return ""
    return '{"metric":{},"value":[1767585660,"' + str(series) + '"]}'


def run(
    where: Box,
    clock: str = STAMP,
    epoch: int = EPOCH,
    hour: int = 0,
    keep: int = 2,
    days: int = 7,
    series: int | None = 3,
    **fake: str,
) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "AYE_BACKUP_DIR": where.backups.as_posix(),
        "AYE_STORAGE_DIR": where.storage.as_posix(),
        "AYE_TEXTFILE_DIR": where.textfile.as_posix(),
        "AYE_BACKUP_HOUR": str(hour),
        "AYE_BACKUP_KEEP_DAYS": str(keep),
        "AYE_RESTORE_TEST_DAYS": str(days),
        "AYE_BACKUP_RUN": RUN.as_posix(),
        "AYE_BACKUP_ALIVE": (where.root / "alive").as_posix(),
        "AYE_BACKUP_FETCH": script(where.root / "fetch.sh", FETCH),
        "AYE_VM_BINARY": script(where.root / "vm.sh", VM),
        "AYE_RESTORE_WAIT": "5",
        "AYE_BACKUP_CLOCK": f"echo {clock}",
        "AYE_RESTORE_CLOCK": f"echo {epoch}",
        "AYE_BACKUP_ONCE": "1",
        "FAKE_LOG": where.log.as_posix(),
        "FAKE_STORAGE": where.storage.as_posix(),
        "FAKE_SNAPSHOT": SNAPSHOT,
        "FAKE_VM_ARGS": where.vm_args.as_posix(),
        "FAKE_RESULT": result(series),
        **{f"FAKE_{name.upper()}": value for name, value in fake.items()},
    }
    done = subprocess.run(
        [shell(), BACKUP.as_posix()], env=env, timeout=WAIT, capture_output=True, text=True
    )
    assert done.returncode == 0, done.stderr
    assert (where.root / "alive").is_file()
    return done


def members(path: Path) -> dict[str, bytes]:
    with tarfile.open(path) as archive:
        found = {}
        for member in archive.getmembers():
            if member.isfile():
                handle = archive.extractfile(member)
                assert handle is not None
                found[member.name.removeprefix("./")] = handle.read()
        return found


def old_archive(where: Box, days: int, stamp: str | None = None) -> Path:
    name = stamp or time.strftime("%Y%m%dT0200Z", time.gmtime(STAMPED - days * DAY))
    path = where.backups / f"victoriametrics-{name}.tar"
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        info = tarfile.TarInfo("./data/small/x")
        info.size = 1
        archive.addfile(info, io.BytesIO(b"x"))
    path.write_bytes(buffer.getvalue())
    mtime = time.time() - days * DAY
    os.utime(path, (mtime, mtime))
    return path


def test_a_first_pass_archives_the_snapshot_deletes_it_and_tests_the_restore(
    tmp_path: Path,
) -> None:
    where = box(tmp_path)
    snapshot(where)
    run(where)
    assert where.archives() == [f"victoriametrics-{STAMP}.tar"]
    assert members(where.backups / where.archives()[0]) == {
        "data/small/2026_01/17A0/values.bin": b"small",
        "data/big/2025_12/17B0/values.bin": b"big",
        "data/indexdb/2026_01/17C0/items.bin": b"index",
        "metadata/minTimestampForCompositeIndex": b"0",
    }
    calls = where.calls()
    assert calls[:2] == [
        "http://victoriametrics:8428/snapshot/create",
        f"http://victoriametrics:8428/snapshot/delete?snapshot={SNAPSHOT}",
    ]
    assert calls[-1] == f"http://127.0.0.1:8438/api/v1/query?query={QUERY}&time={STAMPED + 60}"
    assert "http://127.0.0.1:8438/health" in calls
    assert not (where.storage / "snapshots" / SNAPSHOT).exists()
    assert sorted(p.name for p in where.backups.iterdir()) == where.archives()
    job = where.job()
    assert (job["last_run_success"], job["last_exit_code"]) == ("1", "0")
    assert int(job["last_size_bytes"]) > 0
    args = where.vm_args.read_text(encoding="utf-8").splitlines()
    assert args == [
        f"-storageDataPath={where.backups.as_posix()}/.restore/storage",
        "-httpListenAddr=127.0.0.1:8438",
        "-retentionPeriod=100y",
        "data/big/2025_12/17B0/values.bin",
        "data/indexdb/2026_01/17C0/items.bin",
        "data/small/2026_01/17A0/values.bin",
        "metadata/minTimestampForCompositeIndex",
    ]
    assert where.report() == {
        "deadline_timestamp_seconds": str(EPOCH + 8 * DAY),
        "last_success_timestamp_seconds": str(EPOCH),
        "last_attempt_timestamp_seconds": str(EPOCH),
        "ok": "1",
        "duration_seconds": "0",
        "series": "3",
        "backup_timestamp_seconds": str(STAMPED),
    }


@pytest.mark.skipif(os.name == "nt", reason="POSIX symbolic links and file modes")
def test_the_archive_stores_what_the_snapshot_links_point_to_not_the_links(
    tmp_path: Path,
) -> None:
    where = box(tmp_path)
    parts = where.storage / "data" / "small" / "snapshots" / SNAPSHOT / "2026_01" / "17A0"
    parts.mkdir(parents=True)
    (parts / "values.bin").write_bytes(b"linked")
    root = where.storage / "snapshots" / SNAPSHOT / "data"
    root.mkdir(parents=True)
    (root / "small").symlink_to(Path("../../../data/small/snapshots") / SNAPSHOT)
    run(where)
    (archive,) = where.archives()
    assert members(where.backups / archive) == {"data/small/2026_01/17A0/values.bin": b"linked"}
    assert (where.backups / archive).stat().st_mode & 0o077 == 0
    report = where.textfile / "ayeops_backup_restore_metrics.prom"
    assert report.stat().st_mode & 0o777 == 0o644


def test_one_archive_per_day_from_the_backup_hour_and_one_restore_test_a_week(
    tmp_path: Path,
) -> None:
    where = box(tmp_path)
    snapshot(where)
    run(where, hour=3, clock="20260105T0259Z")
    assert where.archives() == ["victoriametrics-20260105T0259Z.tar"]
    snapshot(where)
    run(where, hour=3, clock="20260105T2359Z", epoch=EPOCH + 3600)
    assert len(where.archives()) == 1
    run(where, hour=3, clock="20260106T0259Z", epoch=EPOCH + DAY)
    assert len(where.archives()) == 1
    run(where, hour=3, clock="20260106T0300Z", epoch=EPOCH + DAY)
    assert where.archives()[1:] == ["victoriametrics-20260106T0300Z.tar"]
    assert where.report()["last_attempt_timestamp_seconds"] == str(EPOCH)
    snapshot(where)
    run(where, hour=3, clock="20260112T0300Z", epoch=EPOCH + 7 * DAY - 1)
    assert where.report()["last_attempt_timestamp_seconds"] == str(EPOCH)
    run(where, hour=3, clock="20260112T0305Z", epoch=EPOCH + 7 * DAY)
    report = where.report()
    assert report["last_attempt_timestamp_seconds"] == str(EPOCH + 7 * DAY)
    assert report["backup_timestamp_seconds"] == str(calendar.timegm((2026, 1, 12, 3, 0, 0)))
    assert report["deadline_timestamp_seconds"] == str(EPOCH + 15 * DAY)


def test_archives_older_than_keep_days_go_after_a_new_one_is_written(tmp_path: Path) -> None:
    where = box(tmp_path)
    gone = old_archive(where, 5)
    kept = old_archive(where, 1)
    snapshot(where)
    run(where, keep=2, create='{"status":"error","msg":"no"}')
    assert gone.exists()
    run(where, keep=2)
    assert not gone.exists()
    assert kept.exists()
    assert len(where.archives()) == 2


def test_a_refused_snapshot_is_a_failed_run_and_keeps_the_last_archive(tmp_path: Path) -> None:
    where = box(tmp_path)
    previous = old_archive(where, 1)
    done = run(where, create='{"status":"error","msg":"cannot create snapshot"}')
    assert where.archives() == [previous.name]
    assert "answered without a snapshot" in done.stderr
    job = where.job()
    assert (job["last_run_success"], job["last_exit_code"]) == ("0", "1")
    assert "last_success_timestamp_seconds" not in job
    assert not (where.backups / ".snapshot").exists()
    assert not [p for p in where.backups.iterdir() if p.name.endswith(".tmp")]


def test_a_failed_tar_still_deletes_the_snapshot_and_leaves_no_file(tmp_path: Path) -> None:
    where = box(tmp_path)
    done = run(where)
    assert where.archives() == []
    assert "tar of the snapshot" in done.stderr
    assert f"http://victoriametrics:8428/snapshot/delete?snapshot={SNAPSHOT}" in where.calls()
    assert where.job()["last_run_success"] == "0"
    assert sorted(p.name for p in where.backups.iterdir()) == []
    report = where.report()
    assert report == {
        "deadline_timestamp_seconds": str(EPOCH + 8 * DAY),
        "last_success_timestamp_seconds": "0",
    }


def test_a_snapshot_that_could_not_be_deleted_is_deleted_by_the_next_pass(
    tmp_path: Path,
) -> None:
    where = box(tmp_path)
    snapshot(where)
    done = run(where, delete_fail="1", listed=SNAPSHOT)
    assert where.archives() == [f"victoriametrics-{STAMP}.tar"]
    assert "cannot delete the snapshot" in done.stderr
    assert (where.backups / ".snapshot").read_text(encoding="utf-8") == f"{SNAPSHOT}\n"
    run(where, delete_fail="1", listed=SNAPSHOT)
    assert (where.backups / ".snapshot").exists()
    run(where, delete_fail="1", listed=SNAPSHOT, list_fail="1")
    assert (where.backups / ".snapshot").exists()
    done = run(where)
    assert f"deleted the snapshot {SNAPSHOT} a stopped run left behind" in done.stdout
    assert not (where.backups / ".snapshot").exists()
    assert not (where.storage / "snapshots" / SNAPSHOT).exists()


def test_a_snapshot_the_store_no_longer_lists_counts_as_deleted(tmp_path: Path) -> None:
    where = box(tmp_path)
    (where.backups / ".snapshot").write_text(f"{SNAPSHOT}\n", encoding="utf-8")
    old_archive(where, 0, STAMP)
    run(where, delete_fail="1", listed="20260101000000-0000000000000000")
    assert not (where.backups / ".snapshot").exists()


def test_a_restore_without_samples_of_up_fails_and_retries_after_six_hours(
    tmp_path: Path,
) -> None:
    where = box(tmp_path)
    snapshot(where)
    done = run(where, series=None)
    assert "restore test failed: no series of up in the hour before" in done.stderr
    report = where.report()
    assert (report["ok"], report["series"], report["last_success_timestamp_seconds"]) == (
        "0",
        "0",
        "0",
    )
    assert report["deadline_timestamp_seconds"] == str(EPOCH + 8 * DAY)
    run(where, epoch=EPOCH + 21599, series=0)
    assert where.report()["last_attempt_timestamp_seconds"] == str(EPOCH)
    run(where, epoch=EPOCH + 21600, series=5)
    report = where.report()
    assert (report["ok"], report["series"]) == ("1", "5")
    assert report["last_success_timestamp_seconds"] == str(EPOCH + 21600)
    assert report["deadline_timestamp_seconds"] == str(EPOCH + 21600 + 8 * DAY)
    assert not (where.backups / ".restore").exists()


def test_a_store_that_does_not_start_fails_the_restore_test(tmp_path: Path) -> None:
    where = box(tmp_path)
    old_archive(where, 0, STAMP)
    done = run(where, vm_exit="1", unhealthy="1")
    assert f"VictoriaMetrics did not start on victoriametrics-{STAMP}.tar" in done.stderr
    assert where.report()["ok"] == "0"
    assert not (where.backups / ".restore").exists()
    run(where, epoch=EPOCH + 21600, unhealthy="1")
    assert where.report()["ok"] == "0"
    assert where.report()["last_attempt_timestamp_seconds"] == str(EPOCH + 21600)


def test_a_broken_archive_fails_the_restore_test(tmp_path: Path) -> None:
    where = box(tmp_path)
    (where.backups / f"victoriametrics-{STAMP}.tar").write_bytes(b"not a tar archive" * 64)
    done = run(where)
    assert f"cannot extract victoriametrics-{STAMP}.tar" in done.stderr
    assert where.report()["ok"] == "0"


def test_the_deadline_of_a_store_never_tested_stays_across_restarts(tmp_path: Path) -> None:
    where = box(tmp_path)
    run(where)
    assert where.report() == {
        "deadline_timestamp_seconds": str(EPOCH + 8 * DAY),
        "last_success_timestamp_seconds": "0",
    }
    run(where, epoch=EPOCH + 3 * DAY, days=2)
    assert where.report()["deadline_timestamp_seconds"] == str(EPOCH + 8 * DAY)


def test_a_pass_clears_what_a_killed_run_left_in_the_backup_directory(tmp_path: Path) -> None:
    where = box(tmp_path)
    stale = where.backups / ".victoriametrics-20260104T0200Z.tar.tmp"
    stale.write_bytes(b"half")
    scratch = where.backups / ".restore" / "storage" / "data"
    scratch.mkdir(parents=True)
    old_archive(where, 0, STAMP)
    (where.textfile / "ayeops_backup_restore_metrics.prom").write_text(
        f'ayeops_backup_restore_test_last_success_timestamp_seconds{{backup="metrics"}} {EPOCH}\n',
        encoding="utf-8",
    )
    run(where)
    assert not stale.exists()
    assert not (where.backups / ".restore").exists()
    assert where.report() == {
        "deadline_timestamp_seconds": str(EPOCH + 8 * DAY),
        "last_success_timestamp_seconds": str(EPOCH),
    }
