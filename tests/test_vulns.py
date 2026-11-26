from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from ayeops.registry import Module
from ayeops.render import host_compose, image_name, render_tree, running_images, scanned_images
from tests.conftest import ROOT, WAIT, manifest_from, pinned, rules_hit, tag_of

MODULE = ROOT / "src" / "ayeops" / "modules" / "vulns"
AGENT = MODULE / "files" / "vulns_agent.py"
LOOP = MODULE / "files" / "vulns_loop.sh"
FIXTURES = ROOT / "tests" / "fixtures" / "vulns"
DEBIAN = FIXTURES / "trivy-debian-buster.json"
ALPINE = FIXTURES / "trivy-alpine-39.json"
TRIVY = (
    "aquasec/trivy:0.76.1@sha256:790fd66d1697395d8693d1acaa1ea702406a20f109b6fc7bac8db623eca6c06c"
)


def load_agent() -> ModuleType:
    spec = importlib.util.spec_from_file_location("ayeops.modules.vulns.files.vulns_agent", AGENT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


agent = load_agent()


def fixture(path: Path) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return loaded


def finding(
    vuln: str = "CVE-1", pkg: str = "p@1", severity: Any = "HIGH", fixed: Any = "2", **extra: Any
) -> dict[str, Any]:
    return {
        "VulnerabilityID": vuln,
        "PkgID": pkg,
        "Severity": severity,
        "FixedVersion": fixed,
        **extra,
    }


def report(*results: list[dict[str, Any]] | None) -> dict[str, Any]:
    return {"Results": [{"Target": f"t{i}", "Vulnerabilities": r} for i, r in enumerate(results)]}


def test_the_debian_report_has_one_fixable_critical_and_one_unfixed_low() -> None:
    assert agent.counts(fixture(DEBIAN)) == Counter({("critical", True): 1, ("low", False): 1})


def test_the_alpine_report_counts_a_cve_once_per_package() -> None:
    assert agent.counts(fixture(ALPINE)) == Counter({("critical", True): 2, ("medium", True): 4})


def test_a_finding_listed_twice_counts_once_and_paths_keep_copies_apart() -> None:
    same = finding()
    copied = finding(PkgPath="app/lib/a.jar")
    assert agent.counts(report([same, same], [same, copied])) == Counter({("high", True): 2})


def test_findings_fall_back_to_the_package_name() -> None:
    entry = {"VulnerabilityID": "CVE-9", "PkgName": "zlib", "Severity": "LOW"}
    assert agent.findings(report([entry])) == {("CVE-9", "zlib", "", "low", False)}


@pytest.mark.parametrize(
    ("severity", "expected"),
    [
        ("CRITICAL", "critical"),
        ("High", "high"),
        ("", "unknown"),
        (None, "unknown"),
        ("x", "unknown"),
    ],
)
def test_severity_is_lower_case_and_unknown_when_trivy_has_none(
    severity: Any, expected: str
) -> None:
    assert agent.severity_of({"Severity": severity}) == expected


def test_empty_results_entries_without_an_id_and_junk_are_skipped() -> None:
    data = {
        "Results": [
            "junk",
            {"Target": "clean"},
            {"Target": "none", "Vulnerabilities": None},
            {"Target": "odd", "Vulnerabilities": ["x", {"Severity": "HIGH"}, finding(fixed="")]},
        ]
    }
    assert agent.counts(data) == Counter({("high", False): 1})
    assert agent.counts({}) == Counter()


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1 2 0\n", agent.Run(1.0, 2.0, 0)),
        ("10 20 1", agent.Run(10.0, 20.0, 1)),
        ("1 2", None),
        ("1 2 3 4", None),
        ("a b c", None),
        ("", None),
    ],
)
def test_parse_run_reads_start_end_and_the_db_exit_code(text: str, expected: Any) -> None:
    assert agent.parse_run(text) == expected


def test_parse_results_maps_each_image_to_its_key_and_exit_code() -> None:
    text = "000 0 nginx:1\n001 1 redis:7\nbroken\n002 x a:1\n003 0 two words here\n"
    assert agent.parse_results(text) == {"nginx:1": ("000", 0), "redis:7": ("001", 1)}


def test_read_report_takes_only_a_json_object_with_a_list_of_results(tmp_path: Path) -> None:
    path = tmp_path / "r.json"
    assert agent.read_report(path) is None
    for text in ("{", "[]", '{"Results": {}}'):
        path.write_text(text, encoding="utf-8")
        assert agent.read_report(path) is None
    path.write_text('{"ArtifactName": "a"}', encoding="utf-8")
    assert agent.read_report(path) == {"ArtifactName": "a"}


def write_scans(state: Path, results: str, reports: dict[str, Any]) -> Path:
    folder = state / "reports"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "results").write_text(results, encoding="utf-8")
    for key, data in reports.items():
        (folder / f"{key}.json").write_text(json.dumps(data), encoding="utf-8")
    return folder


def test_scans_read_each_report_and_fail_what_has_none(tmp_path: Path) -> None:
    folder = write_scans(
        tmp_path,
        "000 0 debian:10\n001 1 broken:1\n002 0 lost:1\n",
        {"000": fixture(DEBIAN), "001": fixture(ALPINE)},
    )
    found = agent.scans(folder, ["missing:1", "debian:10", "broken:1", "lost:1", "debian:10"])
    assert [(s.image, s.ok, s.exit_code) for s in found] == [
        ("broken:1", False, 1),
        ("debian:10", True, 0),
        ("lost:1", False, 0),
        ("missing:1", False, -1),
    ]
    assert found[1].counts == agent.counts(fixture(DEBIAN))
    assert found[0].counts == Counter()


def test_scans_without_a_results_file_fail_every_image(tmp_path: Path) -> None:
    assert [s.ok for s in agent.scans(tmp_path / "reports", ["a:1", "b:2"])] == [False, False]


def scan(image: str, ok: bool = True, **counted: int) -> Any:
    tally: Counter[tuple[str, bool]] = Counter()
    for key, value in counted.items():
        severity, fixed = key.split("_")
        tally[(severity, fixed == "fixed")] = value
    return agent.Scan(image, ok, 0 if ok else 1, tally)


def test_a_run_succeeds_only_with_the_database_and_every_image_scanned() -> None:
    good = agent.Run(1.0, 2.0, 0)
    assert agent.run_succeeded(good, [scan("a:1"), scan("b:1")])
    assert agent.run_succeeded(good, [])
    assert not agent.run_succeeded(good, [scan("a:1"), scan("b:1", ok=False)])
    assert not agent.run_succeeded(agent.Run(1.0, 2.0, 1), [scan("a:1")])


def test_render_metrics_write_every_severity_with_and_without_a_fix() -> None:
    text = agent.render_metrics(
        agent.Run(100.0, 160.4, 0),
        [scan("a:1", critical_fixed=2, low_unfixed=3), scan("b:1", ok=False)],
        90.0,
        50.6,
    )
    assert text.splitlines() == [
        "ayeops_vulns_last_run_timestamp_seconds 160",
        "ayeops_vulns_last_run_success 0",
        "ayeops_vulns_last_success_timestamp_seconds 90",
        "ayeops_vulns_run_duration_seconds 60",
        "ayeops_vulns_db_update_success 1",
        'ayeops_vulns_images{result="ok"} 1',
        'ayeops_vulns_images{result="failed"} 1',
        "ayeops_vulns_db_updated_timestamp_seconds 51",
        'ayeops_vulns_image_scan_success{image="a:1"} 1',
        'ayeops_vulns_vulnerabilities{fixed="true",image="a:1",severity="critical"} 2',
        'ayeops_vulns_vulnerabilities{fixed="false",image="a:1",severity="critical"} 0',
        'ayeops_vulns_vulnerabilities{fixed="true",image="a:1",severity="high"} 0',
        'ayeops_vulns_vulnerabilities{fixed="false",image="a:1",severity="high"} 0',
        'ayeops_vulns_vulnerabilities{fixed="true",image="a:1",severity="medium"} 0',
        'ayeops_vulns_vulnerabilities{fixed="false",image="a:1",severity="medium"} 0',
        'ayeops_vulns_vulnerabilities{fixed="true",image="a:1",severity="low"} 0',
        'ayeops_vulns_vulnerabilities{fixed="false",image="a:1",severity="low"} 3',
        'ayeops_vulns_vulnerabilities{fixed="true",image="a:1",severity="unknown"} 0',
        'ayeops_vulns_vulnerabilities{fixed="false",image="a:1",severity="unknown"} 0',
        'ayeops_vulns_image_scan_success{image="b:1"} 0',
    ]


def test_render_metrics_without_a_database_date_and_with_a_failed_download() -> None:
    text = agent.render_metrics(agent.Run(1.0, 2.0, 124), [], 2.0, None)
    assert "ayeops_vulns_db_update_success 0\n" in text
    assert "ayeops_vulns_last_run_success 0\n" in text
    assert "db_updated" not in text


def test_metric_labels_are_escaped_and_fractions_kept() -> None:
    assert agent.metric("x", 1.5, image='a"b\\c\nd') == 'ayeops_vulns_x{image="a\\"b\\\\c d"} 1.5\n'
    assert agent.metric("x", 2.0) == "ayeops_vulns_x 2\n"


def settings(tmp_path: Path, *images: str) -> Any:
    return agent.Settings(
        schedule=(5, 0),
        images=images,
        state=tmp_path / "state",
        textfile=tmp_path / "vulns.prom",
        db=tmp_path / "cache" / "db" / "metadata.json",
    )


def write_db(target: Any, updated: str) -> None:
    target.db.parent.mkdir(parents=True, exist_ok=True)
    target.db.write_text(json.dumps({"Version": 2, "UpdatedAt": updated}), encoding="utf-8")


def test_db_updated_reads_trivys_nanosecond_time(tmp_path: Path) -> None:
    target = settings(tmp_path)
    assert agent.db_updated(target.db) is None
    write_db(target, "2026-11-05T06:15:51.107456787Z")
    assert agent.db_updated(target.db) == pytest.approx(1793859351.107456)
    target.db.write_text("[]", encoding="utf-8")
    assert agent.db_updated(target.db) is None
    target.db.write_text("{", encoding="utf-8")
    assert agent.db_updated(target.db) is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2026-01-01T00:00:00Z", 1767225600.0),
        ("2026-01-01T00:00:00", None),
        ("soon", None),
        ("", None),
        (None, None),
    ],
)
def test_timestamp_reads_only_zoned_iso_dates(text: Any, expected: float | None) -> None:
    assert agent.timestamp(text) == expected


def test_record_moves_the_last_success_on_a_clean_run(tmp_path: Path) -> None:
    target = settings(tmp_path, "debian:10", "alpine:3.9")
    write_scans(
        target.state,
        "000 0 alpine:3.9\n001 0 debian:10\n",
        {"000": fixture(ALPINE), "001": fixture(DEBIAN)},
    )
    write_db(target, "2026-01-01T00:00:00Z")
    line = agent.record(target, agent.Run(1000.0, 1100.0, 0))
    assert line == {
        "event": "vulns-run",
        "success": True,
        "db_update": True,
        "seconds": 100,
        "failed": [],
        "critical_fixable": {"alpine:3.9": 2, "debian:10": 1},
    }
    text = target.textfile.read_text(encoding="utf-8")
    assert "ayeops_vulns_last_success_timestamp_seconds 1100\n" in text
    assert "ayeops_vulns_db_updated_timestamp_seconds 1767225600\n" in text
    assert (
        'ayeops_vulns_vulnerabilities{fixed="false",image="debian:10",severity="low"} 1\n' in text
    )


def test_record_keeps_the_last_success_when_an_image_fails(tmp_path: Path) -> None:
    target = settings(tmp_path, "debian:10", "gone:1")
    target.textfile.write_text("ayeops_vulns_last_success_timestamp_seconds 777\n", "utf-8")
    write_scans(target.state, "000 0 debian:10\n001 1 gone:1\n", {"000": fixture(DEBIAN)})
    line = agent.record(target, agent.Run(1000.0, 1100.0, 0))
    assert (line["success"], line["failed"]) == (False, ["gone:1"])
    assert "ayeops_vulns_last_success_timestamp_seconds 777\n" in target.textfile.read_text(
        encoding="utf-8"
    )


def test_record_after_a_failed_download_without_a_textfile_counts_from_the_run(
    tmp_path: Path,
) -> None:
    target = settings(tmp_path, "debian:10")
    write_scans(target.state, "000 1 debian:10\n", {})
    line = agent.record(target, agent.Run(1000.0, 1100.0, 1))
    assert (line["success"], line["db_update"], line["critical_fixable"]) == (False, False, {})
    assert "ayeops_vulns_last_success_timestamp_seconds 1100\n" in target.textfile.read_text(
        encoding="utf-8"
    )


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("ayeops_vulns_last_success_timestamp_seconds 12\n", 12.0),
        ("a 1\nayeops_vulns_last_success_timestamp_seconds 1.5e3 \nb 2\n", 1500.0),
        ("ayeops_vulns_last_success_timestamp_seconds x\n", None),
        ("other 1\n", None),
    ],
)
def test_last_success_reads_the_previous_textfile(text: str, expected: float | None) -> None:
    assert agent.last_success(text) == expected


def test_request_text_numbers_each_image_once_in_order() -> None:
    assert agent.request_text(["b:1", "a:1", "b:1"]) == "000 a:1\n001 b:1\n"
    assert agent.request_text([]) == ""


def test_prepare_opens_the_state_to_trivy_and_starts_the_stale_clock(tmp_path: Path) -> None:
    target = settings(tmp_path)
    agent.prepare(target, 123.0)
    assert target.textfile.read_text(encoding="utf-8") == (
        "ayeops_vulns_last_run_timestamp_seconds 123\n"
        "ayeops_vulns_last_success_timestamp_seconds 123\n"
    )
    if sys.platform != "win32":
        assert target.state.stat().st_mode & 0o777 == 0o777
    agent.prepare(target, 999.0)
    assert "999" not in target.textfile.read_text(encoding="utf-8")


def at(hour: int, minute: int) -> float:
    return time.mktime((2026, 11, 5, hour, minute, 0, 0, 0, -1))


def test_next_run_is_today_before_the_time_and_tomorrow_after_it() -> None:
    assert agent.next_run(at(4, 0), 5, 0) == at(5, 0)
    assert agent.next_run(at(5, 0), 5, 0) == at(5, 0) + 86400
    assert agent.next_run(at(6, 0), 5, 30) == time.mktime((2026, 11, 6, 5, 30, 0, 0, 0, -1))


@pytest.mark.parametrize("text", ["5:00", "24:00", "12:60", "noon", ""])
def test_parse_schedule_refuses_what_is_not_hh_mm(text: str) -> None:
    with pytest.raises(ValueError):
        agent.parse_schedule(text)


def test_parse_schedule_reads_hours_and_minutes() -> None:
    assert agent.parse_schedule(" 23:59 ") == (23, 59)


def test_first_due_runs_at_once_without_a_run_or_after_the_images_changed(
    tmp_path: Path,
) -> None:
    target = settings(tmp_path, "a:1", "b:1")
    now = at(12, 0)
    run = agent.Run(1.0, 2.0, 0)
    assert agent.first_due(target, None, now) == now
    write_scans(target.state, "000 0 a:1\n", {})
    assert agent.first_due(target, run, now) == now
    write_scans(target.state, "000 0 a:1\n001 1 b:1\n", {})
    tomorrow = time.mktime((2026, 11, 6, 5, 0, 0, 0, 0, -1))
    assert agent.first_due(target, run, now) == tomorrow
    target.request.write_text("", encoding="utf-8")
    assert agent.first_due(target, None, now) == tomorrow


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


def test_serve_asks_for_a_first_run_with_every_image(tmp_path: Path) -> None:
    target = settings(tmp_path, "b:1", "a:1")
    agent.prepare(target, 0.0)
    clock = Clock(at(12, 0))
    agent.serve(target, clock.wall, clock.sleep, loops(1), tmp_path / "alive", lambda _: None)
    assert target.request.read_text(encoding="utf-8") == "000 a:1\n001 b:1\n"
    assert (tmp_path / "alive").exists()


def test_serve_waits_for_the_schedule_once_the_images_ran_and_never_naps_past_it(
    tmp_path: Path,
) -> None:
    target = settings(tmp_path, "a:1")
    write_scans(target.state, "000 0 a:1\n", {"000": {"Results": []}})
    target.last.write_text("1 2 0\n", encoding="utf-8")
    start = at(4, 59)
    clock = Clock(start)
    agent.serve(target, clock.wall, clock.sleep, loops(1), tmp_path / "alive", lambda _: None)
    assert not target.request.exists()
    assert clock.slept == [30.0]
    clock.now = start + 50
    clock.slept.clear()
    agent.serve(target, clock.wall, clock.sleep, loops(2), tmp_path / "alive", lambda _: None)
    assert clock.slept[0] == 10.0
    assert target.request.exists()


def test_serve_does_not_overwrite_a_pending_request(tmp_path: Path) -> None:
    target = settings(tmp_path, "a:1")
    target.state.mkdir()
    target.request.write_text("000 old:1\n", encoding="utf-8")
    clock = Clock(at(5, 0) - 1)
    agent.serve(target, clock.wall, clock.sleep, loops(2), tmp_path / "alive", lambda _: None)
    assert target.request.read_text(encoding="utf-8") == "000 old:1\n"


def test_serve_records_each_finished_run_once(tmp_path: Path) -> None:
    target = settings(tmp_path, "a:1")
    target.state.mkdir()
    lines: list[dict[str, Any]] = []
    clock = Clock(at(12, 0))
    finished = iter([False, True, True])

    def keep_going() -> bool:
        step = next(finished, None)
        if step is None:
            return False
        if step and not target.last.exists():
            write_scans(target.state, "000 0 a:1\n", {"000": {"Results": []}})
            target.last.write_text("1 2 0\n", encoding="utf-8")
        return True

    agent.serve(target, clock.wall, clock.sleep, keep_going, tmp_path / "alive", lines.append)
    assert [line["success"] for line in lines] == [True]


def test_settings_come_from_the_environment() -> None:
    parsed = agent.settings_from(
        {
            "AYE_VULNS_SCHEDULE": "07:15",
            "AYE_VULNS_IMAGES": '["a:1", "ghcr.io/o/b:2"]',
            "AYE_VULNS_STATE": "/s",
            "AYE_VULNS_TEXTFILE": "/t/x.prom",
            "AYE_VULNS_DB": "/c/m.json",
        }
    )
    assert parsed == agent.Settings(
        (7, 15), ("a:1", "ghcr.io/o/b:2"), Path("/s"), Path("/t/x.prom"), Path("/c/m.json")
    )
    assert agent.settings_from({}) == agent.Settings()


@pytest.mark.parametrize("value", ['"a:1"', "[1]", "{}", '[""]', '["a b"]', '["a\\tb"]'])
def test_settings_refuse_images_that_are_not_a_list_of_references(value: str) -> None:
    with pytest.raises(ValueError):
        agent.settings_from({"AYE_VULNS_IMAGES": value})


def test_main_once_requests_a_run_without_serving(tmp_path: Path) -> None:
    environ = {
        "AYE_VULNS_ONCE": "1",
        "AYE_VULNS_IMAGES": '["a:1"]',
        "AYE_VULNS_STATE": str(tmp_path / "state"),
        "AYE_VULNS_TEXTFILE": str(tmp_path / "v.prom"),
    }
    assert agent.main(environ) == 0
    assert (tmp_path / "state" / "run.request").read_text(encoding="utf-8") == "000 a:1\n"
    assert (tmp_path / "v.prom").exists()


FAKE_TRIVY = """#!/bin/sh
echo "$*" >> "$FAKE_LOG"
case "$*" in
  *--download-db-only*) exit "$FAKE_DB" ;;
esac
out=""
prev=""
for arg in "$@"; do
  if [ "$prev" = "--output" ]; then out="$arg"; fi
  prev="$arg"
done
case "$arg" in
  *broken*) exit 3 ;;
esac
printf '{"ArtifactName": "%s", "Results": []}' "$arg" > "$out"
"""


def run_loop(tmp_path: Path, request: str, db: int) -> Path:
    shell = shutil.which("sh")
    if shell is None:
        pytest.skip("needs a POSIX sh")
    tools = tmp_path / "bin"
    tools.mkdir()
    fake = tools / "trivy"
    fake.write_text(FAKE_TRIVY, encoding="utf-8", newline="\n")
    fake.chmod(0o755)
    state = tmp_path / "state"
    scratch = tmp_path / "scratch"
    state.mkdir()
    scratch.mkdir()
    (scratch / "leftover").write_text("x", encoding="utf-8")
    (state / "run.request").write_text(request, encoding="utf-8", newline="\n")
    environ = {
        **os.environ,
        "PATH": str(tools) + os.pathsep + os.environ.get("PATH", ""),
        "AYE_VULNS_STATE": str(state),
        "AYE_VULNS_TIMEOUT": "60",
        "TMPDIR": str(scratch),
        "FAKE_LOG": str(tmp_path / "calls"),
        "FAKE_DB": str(db),
    }
    process = subprocess.Popen([shell, str(LOOP)], env=environ)
    try:
        deadline = time.monotonic() + WAIT
        while not (state / "run.last").exists() and time.monotonic() < deadline:
            time.sleep(0.2)
    finally:
        process.kill()
        process.wait(WAIT)
    assert (state / "run.last").exists()
    assert not (scratch / "leftover").exists()
    return state


def test_the_loop_scans_each_requested_image_after_one_database_update(tmp_path: Path) -> None:
    state = run_loop(tmp_path, "000 a:1\n001 broken:2\n\n002 c:3\n", 0)
    results = (state / "reports" / "results").read_text(encoding="utf-8")
    assert results.splitlines() == ["000 0 a:1", "001 3 broken:2", "002 0 c:3"]
    report = json.loads((state / "reports" / "000.json").read_text(encoding="utf-8"))
    assert report["ArtifactName"] == "a:1"
    assert not (state / "run.request").exists()
    assert not (state / "reports.next").exists()
    started, finished, code = (state / "run.last").read_text(encoding="utf-8").split()
    assert int(started) <= int(finished)
    assert code == "0"
    calls = (tmp_path / "calls").read_text(encoding="utf-8").splitlines()
    assert calls[0] == "image --download-db-only"
    assert all("--skip-db-update" in call for call in calls[1:])
    assert len(calls) == 4
    found = agent.scans(state / "reports", ["a:1", "broken:2", "c:3"])
    assert [s.ok for s in found] == [True, False, True]


def test_the_loop_scans_nothing_when_the_database_did_not_download(tmp_path: Path) -> None:
    state = run_loop(tmp_path, "000 a:1\n001 b:2\n", 7)
    results = (state / "reports" / "results").read_text(encoding="utf-8")
    assert results.splitlines() == ["000 7 a:1", "001 7 b:2"]
    assert (state / "run.last").read_text(encoding="utf-8").split()[2] == "7"
    assert (tmp_path / "calls").read_text(encoding="utf-8").splitlines() == [
        "image --download-db-only"
    ]


def with_module(data: dict[str, Any], host: str = "edge", **settings: Any) -> dict[str, Any]:
    data["hosts"][host]["modules"]["vulns"] = settings
    return data


def services(data: dict[str, Any], modules: dict[str, Module], host: str = "edge") -> Any:
    return host_compose(manifest_from(data), host, modules)["services"]


def images_of(rendered: dict[str, Any]) -> list[str]:
    loaded: list[str] = json.loads(rendered["vulns-agent"]["environment"]["AYE_VULNS_IMAGES"])
    return loaded


def test_the_example_runs_the_module_and_passes_every_rule(data: dict[str, Any]) -> None:
    assert "vulns" in data["hosts"]["edge"]["modules"]
    assert rules_hit(data) == set()


def test_the_agent_gets_every_image_the_host_project_runs(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    rendered = services(data, modules)
    images = images_of(rendered)
    assert images == sorted({service["image"] for service in rendered.values()})
    assert TRIVY in images
    assert pinned("vulns", "python") in {image.split("@", 1)[0] for image in images}


def test_the_images_of_the_hosts_compose_files_are_scanned_too(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    rendered = services(with_module(data, "central"), modules, "central")
    images = images_of(rendered)
    assert {"nginx:1.29.2-alpine", "python:3.13.9-slim"} <= set(images)
    assert len(images) == len(set(images))


def test_exclude_drops_an_exact_reference_or_every_tag_of_a_name(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    rendered = services(
        with_module(data, "central", exclude=["nginx:1.29.2-alpine", "python"]), modules, "central"
    )
    images = images_of(rendered)
    assert "nginx:1.29.2-alpine" not in images
    assert not any(image.startswith("python:") for image in images)
    assert TRIVY in images


def test_scanned_images_skip_variables_stopped_services_and_missing_files(
    data: dict[str, Any],
) -> None:
    data["hosts"]["edge"]["compose"] = ["apps/none.compose.yaml"]
    listed = {
        "a": {"image": "a:1", "restart": "unless-stopped"},
        "b": {"image": "b:${TAG}"},
        "c": {"image": "c:1", "profiles": ["debug"]},
        "d": {"build": "."},
    }
    assert scanned_images(manifest_from(data), "edge", listed, {}) == ["a:1"]


@pytest.mark.parametrize(
    ("listed", "expected"),
    [
        (None, set()),
        ([], set()),
        ({"a": None, "b": "x", "c": {"image": ""}}, set()),
        ({"a": {"image": "a:1", "deploy": {"replicas": 0}}, "b": {"image": "b:1"}}, {"b:1"}),
        ({"a": {"image": "a:1", "restart": "no"}}, set()),
    ],
)
def test_running_images_keep_only_services_that_stay_up(listed: Any, expected: set[str]) -> None:
    assert running_images(listed) == expected


@pytest.mark.parametrize(
    ("reference", "expected"),
    [
        ("nginx:1.29.2-alpine", "nginx"),
        ("nginx", "nginx"),
        ("ghcr.io/acme/web:1.2", "ghcr.io/acme/web"),
        ("registry:5000/team/app:3", "registry:5000/team/app"),
        ("registry:5000/app", "registry:5000/app"),
        ("busybox@sha256:abc", "busybox"),
        ("ghcr.io/o/a:1@sha256:abc", "ghcr.io/o/a"),
    ],
)
def test_image_name_drops_the_tag_and_digest_but_not_a_registry_port(
    reference: str, expected: str
) -> None:
    assert image_name(reference) == expected


def test_the_proxy_answers_only_image_reads_and_only_trivy_reaches_it(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    compose = host_compose(manifest_from(data), "edge", modules)
    rendered = compose["services"]
    proxy = rendered["vulns-proxy"]
    assert proxy["volumes"] == ["/var/run/docker.sock:/var/run/docker.sock:ro"]
    allowed = {
        key for key, value in proxy["environment"].items() if value == "1" and key != "DISABLE_IPV6"
    }
    assert allowed == {"IMAGES", "PING", "VERSION"}
    assert proxy["environment"]["POST"] == "0"
    assert "ports" not in proxy
    near_docker = sorted(
        name
        for name, service in rendered.items()
        if "vulns-docker" in (service.get("networks") or [])
    )
    assert near_docker == ["vulns", "vulns-proxy"]
    assert compose["networks"]["vulns-docker"] == {"internal": True}
    assert rendered["vulns"]["environment"]["DOCKER_HOST"] == "tcp://vulns-proxy:2375"
    assert rendered["vulns-agent"]["network_mode"] == "none"
    for name in ("vulns", "vulns-agent", "vulns-proxy"):
        assert rendered[name]["read_only"] is True
        assert rendered[name]["cap_drop"] == ["ALL"]
        assert "ports" not in rendered[name]


def test_trivy_keeps_its_database_in_a_volume_the_agent_reads_only(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    compose = host_compose(manifest_from(data), "edge", modules)
    trivy = compose["services"]["vulns"]
    reader = compose["services"]["vulns-agent"]
    assert "vulns-cache:/cache" in trivy["volumes"]
    assert "vulns-cache:/cache:ro" in reader["volumes"]
    assert "vulns-state:/state" in trivy["volumes"]
    assert "vulns-state:/state" in reader["volumes"]
    assert {"vulns-cache", "vulns-state"} <= set(compose["volumes"])
    environment = trivy["environment"]
    assert environment["TRIVY_CACHE_DIR"] == "/cache"
    assert environment["TMPDIR"].startswith("/cache/")
    assert reader["environment"]["AYE_VULNS_DB"] == "/cache/db/metadata.json"
    assert environment["TRIVY_SCANNERS"] == "vuln"
    assert environment["TRIVY_FORMAT"] == "json"
    assert environment["TRIVY_IMAGE_SRC"] == "docker,remote"
    assert trivy["entrypoint"] == ["sh", "/app/vulns_loop.sh"]


def test_the_timeout_and_the_healthcheck_follow_timeout_minutes(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    rendered = services(with_module(data, timeout_minutes=20), modules)
    trivy = rendered["vulns"]
    assert trivy["environment"]["AYE_VULNS_TIMEOUT"] == "1200"
    assert trivy["environment"]["TRIVY_TIMEOUT"] == "20m"
    assert "-mmin -70 " in trivy["healthcheck"]["test"][-1]


def test_schedule_and_zone_reach_the_agent(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    rendered = services(with_module(data, schedule="02:10", timezone="Europe/Kyiv"), modules)
    assert rendered["vulns-agent"]["environment"]["AYE_VULNS_SCHEDULE"] == "02:10"
    assert rendered["vulns-agent"]["environment"]["TZ"] == "Europe/Kyiv"
    assert rendered["vulns"]["environment"]["TZ"] == "Europe/Kyiv"


@pytest.mark.parametrize(
    "settings",
    [
        {"schedule": "5:00"},
        {"timeout_minutes": 0},
        {"timeout_minutes": 121},
        {"exclude": ["a b"]},
        {"exclude": ["a:${TAG}"]},
        {"exclude": ["a", "a"]},
        {"scanners": ["secret"]},
    ],
)
def test_settings_outside_the_schema_are_refused(
    data: dict[str, Any], settings: dict[str, Any]
) -> None:
    assert "module-settings" in rules_hit(with_module(data, **settings))


def test_the_module_ships_its_agent_and_loop(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    tree = render_tree(manifest_from(data), modules)
    for path in (AGENT, LOOP):
        assert tree[f"edge/vulns/{path.name}"] == path.read_text(encoding="utf-8").replace(
            "\r\n", "\n"
        )


def test_trivy_is_pinned_to_an_exact_version_and_its_index_digest(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    rendered = services(data, modules)
    assert rendered["vulns"]["image"] == TRIVY
    assert tag_of(rendered["vulns-agent"]["image"]) == pinned("vulns", "python")
    proxy = pinned("vulns", "tecnativa/docker-socket-proxy")
    assert tag_of(rendered["vulns-proxy"]["image"]) == proxy


def test_the_alerts_read_the_series_the_agent_writes(modules: dict[str, Module]) -> None:
    spec = modules["vulns"].spec
    alerts = {alert["name"]: alert for alert in spec["alerts"]}
    assert set(alerts) == {"vulns-critical-fixable", "vulns-scan-failed", "vulns-scan-stale"}
    sample = agent.render_metrics(
        agent.Run(1.0, 2.0, 0), [scan("a:1", critical_fixed=1)], 2.0, None
    )
    assert 'fixed="true",image="a:1",severity="critical"} 1' in sample
    assert 'severity="critical", fixed="true"' in alerts["vulns-critical-fixable"]["expr"]
    for alert in alerts.values():
        metric = next(word for word in alert["expr"].split("(") if "ayeops_vulns_" in word)
        name = metric.split("{")[0].split(")")[0].strip()
        assert f"\n{name}" in "\n" + sample, name
        assert alert["severity"] == "warning"
        assert (ROOT / "docs" / "runbooks" / f"{alert['runbook']}.md").is_file()
