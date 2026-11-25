from __future__ import annotations

import io
import json
import shutil
import subprocess
import urllib.error
from collections.abc import Callable
from email.message import Message
from pathlib import Path
from typing import Any

import pytest

from ayeops import report
from ayeops.report import ReportError, Sample

ROOT = Path(__file__).resolve().parents[1]

FIXTURES = ROOT / "tests" / "fixtures" / "report"
NOW = 1_792_800_000.0
PUSH_ENV = {
    "GITHUB_REPOSITORY": "acme/shop",
    "GITHUB_REF_NAME": "main",
    "GITHUB_HEAD_REF": "",
    "GITHUB_WORKFLOW": "ci",
    "GITHUB_JOB": "test",
    "GITHUB_RUN_ID": "4242",
    "GITHUB_RUN_ATTEMPT": "2",
    "GITHUB_SHA": "abc123",
}


def fixture(name: str) -> Path:
    return FIXTURES / name


def written(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def samples_of(text: str) -> dict[str, float]:
    found: dict[str, float] = {}
    for line in text.splitlines():
        selector, value, _ = line.rsplit(" ", 2)
        found[selector] = float(value)
    return found


def no_git(*args: str) -> str:
    raise ReportError("no git here")


def test_pytest_junit_counts_every_outcome_and_the_suite_time() -> None:
    summary = report.parse_junit([fixture("pytest-junit.xml")])
    assert {o: summary.count(o) for o in report.OUTCOMES} == {
        "passed": 2,
        "failed": 1,
        "error": 1,
        "skipped": 1,
    }
    assert summary.duration == pytest.approx(0.177)
    assert summary.flaky == []
    assert [c.name for c in summary.slowest(2)] == ["test_calc::test_wrong", "test_calc::test_top"]


def test_a_rerun_that_passed_is_flaky_and_counts_once(tmp_path: Path) -> None:
    path = written(
        tmp_path,
        "rerun.xml",
        '<testsuites><testsuite name="pytest" time="2.5">'
        '<testcase classname="t" name="a" time="1.0"><rerunFailure message="x"/></testcase>'
        '<testcase classname="t" name="b" time="0.5"/>'
        "</testsuite></testsuites>",
    )
    summary = report.parse_junit([path])
    assert summary.flaky == ["t::a"]
    assert summary.count("passed") == 2
    assert summary.duration == 2.5


def test_a_test_that_failed_in_one_file_and_passed_in_the_next_is_flaky(tmp_path: Path) -> None:
    failed = written(
        tmp_path,
        "1.xml",
        '<testsuite time="1"><testcase classname="t" name="a" time="1"><failure/></testcase>'
        "</testsuite>",
    )
    passed = written(
        tmp_path,
        "2.xml",
        '<testsuite time="2"><testcase classname="t" name="a" time="2"/></testsuite>',
    )
    summary = report.parse_junit([failed, passed])
    assert summary.cases == (report.CaseResult("t::a", "passed", 3.0, True),)
    assert summary.duration == 3.0


def test_junit_without_times_sums_its_cases(tmp_path: Path) -> None:
    path = written(
        tmp_path,
        "plain.xml",
        '<testsuites><testsuite><testcase name="a" time="1,234.5"/><testcase name="b" time="x"/>'
        "</testsuite></testsuites>",
    )
    assert report.parse_junit([path]).duration == 1234.5


def test_junit_rejects_other_xml_and_broken_files(tmp_path: Path) -> None:
    with pytest.raises(ReportError, match="not a JUnit report"):
        report.parse_junit([fixture("coverage.xml")])
    with pytest.raises(ReportError, match="not XML"):
        report.parse_junit([written(tmp_path, "bad.xml", "<testsuite>")])
    with pytest.raises(ReportError, match=r"missing\.xml"):
        report.parse_junit([tmp_path / "missing.xml"])


def test_junit_samples_carry_counts_slowest_and_flaky_names() -> None:
    summary = report.JunitSummary(
        (
            report.CaseResult("a", "passed", 3.0, True),
            report.CaseResult("b", "failed", 1.0),
            report.CaseResult("c", "skipped", 0.0),
        ),
        4.0,
    )
    samples = report.junit_samples(summary, 1)
    assert Sample("aye_tests", 1.0, {"outcome": "failed"}) in samples
    assert Sample("aye_tests_flaky", 1.0) in samples
    assert Sample("aye_test_slowest_seconds", 3.0, {"test": "a", "rank": "1"}) in samples
    assert Sample("aye_test_flaky", 1.0, {"test": "a"}) in samples
    assert not any(s.labels.get("test") == "b" for s in samples)


def test_coverage_py_xml_with_branches() -> None:
    found = report.parse_coverage(fixture("coverage.xml"))
    assert (found.format, found.lines, found.branches) == ("cobertura", (16, 16), (8, 16))
    assert (found.line_percent, found.branch_percent) == (100.0, 50.0)


def test_coverage_py_xml_without_branches_reports_no_branch_percent() -> None:
    found = report.parse_coverage(fixture("coverage-nobranch.xml"))
    assert found.branch_percent is None
    names = {s.name for s in report.coverage_samples(found)}
    assert names == {
        "aye_coverage_lines_percent",
        "aye_coverage_lines_covered",
        "aye_coverage_lines_valid",
    }


def test_coverage_py_json_reports_lines_not_the_combined_percent() -> None:
    found = report.parse_coverage(fixture("coverage.json"))
    assert (found.format, found.line_percent, found.branch_percent) == ("coverage.py", 100.0, 50.0)


def test_cobertura_with_only_rates(tmp_path: Path) -> None:
    path = written(tmp_path, "c.xml", '<coverage line-rate="0.75" branch-rate="0.5"/>')
    found = report.parse_coverage(path)
    assert (found.line_percent, found.branch_percent) == (75.0, 50.0)


def test_lcov_jacoco_clover_and_istanbul(tmp_path: Path) -> None:
    lcov = written(
        tmp_path,
        "lcov.info",
        "SF:a.py\nDA:1,1\nDA:2,0\nBRDA:1,0,0,1\nBRDA:1,0,1,-\nend_of_record\n"
        "SF:b.py\nLF:10\nLH:5\nend_of_record\n",
    )
    assert report.parse_coverage(lcov).lines == (6, 12)
    assert report.parse_coverage(lcov).branches == (1, 2)
    jacoco = written(
        tmp_path,
        "jacoco.xml",
        '<report name="x"><counter type="LINE" missed="1" covered="3"/>'
        '<counter type="BRANCH" missed="2" covered="2"/></report>',
    )
    assert (report.parse_coverage(jacoco).line_percent, report.parse_coverage(jacoco).format) == (
        75.0,
        "jacoco",
    )
    clover = written(
        tmp_path,
        "clover.xml",
        '<coverage><project><metrics statements="4" coveredstatements="1" conditionals="0"'
        ' coveredconditionals="0"/></project></coverage>',
    )
    assert report.parse_coverage(clover).line_percent == 25.0
    istanbul = written(
        tmp_path,
        "summary.json",
        json.dumps({"total": {"lines": {"total": 8, "covered": 6}, "branches": {"total": 0}}}),
    )
    found = report.parse_coverage(istanbul)
    assert (found.format, found.line_percent, found.branches) == ("istanbul", 75.0, None)


def test_coverage_rejects_what_it_cannot_read(tmp_path: Path) -> None:
    with pytest.raises(ReportError, match="not Cobertura"):
        report.parse_coverage(written(tmp_path, "x.xml", "<html/>"))
    with pytest.raises(ReportError, match=r"neither coverage\.py"):
        report.parse_coverage(written(tmp_path, "x.json", "{}"))
    with pytest.raises(ReportError, match="not JSON"):
        report.parse_coverage(written(tmp_path, "y.json", "{oops"))
    with pytest.raises(ReportError, match="lcov"):
        report.parse_coverage(written(tmp_path, "x.txt", "hello"))
    with pytest.raises(ReportError, match="no lines"):
        report.coverage_samples(report.Coverage("lcov", (0, 0), None))


def test_mutmut3_results_all_counts_every_status_and_survivors_by_module() -> None:
    found = report.parse_mutants(fixture("mutmut3-results-all.txt"))
    assert found.counts == {
        "killed": 4,
        "survived": 2,
        "timeout": 1,
        "no_coverage": 1,
        "error": 0,
        "ignored": 2,
    }
    assert found.score == pytest.approx(100 * 5 / 8)
    assert found.survivors == {"shop.normalize": 2, "shop.risk": 0, "shop.money": 0}


def test_mutmut3_results_without_all_is_rejected_not_scored_zero() -> None:
    with pytest.raises(ReportError, match="--all true"):
        report.parse_mutants(fixture("mutmut3-results.txt"))


def test_mutmut3_cicd_stats() -> None:
    found = report.parse_mutants(fixture("mutmut3-cicd-stats.json"))
    assert found.counts["killed"] == 1840
    assert found.counts["no_coverage"] == 31
    assert found.score == pytest.approx(100 * (1840 + 25) / (1840 + 25 + 112 + 31))


def test_mutmut3_run_output_takes_the_last_progress_line() -> None:
    found = report.parse_mutants(fixture("mutmut3-run.txt"))
    assert found.counts == {
        "killed": 4,
        "survived": 2,
        "timeout": 1,
        "no_coverage": 1,
        "error": 0,
        "ignored": 2,
    }


def test_mutmut2_run_output() -> None:
    found = report.parse_mutants(fixture("mutmut2-run.txt"))
    assert found.tool == "mutmut"
    assert found.counts == {
        "killed": 741,
        "survived": 6,
        "timeout": 0,
        "no_coverage": 0,
        "error": 0,
        "ignored": 10,
    }


def test_mutmut2_results_without_killed_is_rejected() -> None:
    with pytest.raises(ReportError, match="no killed"):
        report.parse_mutants(fixture("mutmut2-results.txt"))


def test_mutmut2_junitxml_names_the_file_of_each_survivor() -> None:
    found = report.parse_mutants(fixture("mutmut2-junit.xml"))
    assert (found.counts["killed"], found.counts["survived"]) == (20, 6)
    assert found.survivors == {"src/ayeops/report.py": 6}


def test_mutmut_module_strips_the_mangled_function() -> None:
    assert report.mutmut_module("a.b.x_f__mutmut_3") == "a.b"
    assert report.mutmut_module("a.b.xǁCǁm__mutmut_1") == "a.b"
    assert report.mutmut_module("x_f__mutmut_1") == "x_f"


def test_stryker_and_infection_reports(tmp_path: Path) -> None:
    stryker = written(
        tmp_path,
        "mutation.json",
        json.dumps(
            {
                "files": {
                    "src/a.ts": {"mutants": [{"status": "Killed"}, {"status": "Survived"}]},
                    "src/b.ts": {"mutants": [{"status": "NoCoverage"}, {"status": "Odd"}]},
                }
            }
        ),
    )
    found = report.parse_mutants(stryker)
    assert found.tool == "stryker"
    assert found.survivors == {"src/a.ts": 1, "src/b.ts": 0}
    assert found.score == pytest.approx(100 / 3)
    infection = written(
        tmp_path,
        "infection.json",
        json.dumps({"stats": {"killedCount": 8, "escapedCount": 2, "timeOutCount": 0}}),
    )
    assert report.parse_mutants(infection).score == 80.0
    summary = written(
        tmp_path,
        "summary.log",
        "Total: 10\n\nKilled: 6\nErrored: 0\nEscaped: 3\nTimed Out: 1\n"
        "Skipped: 0\nNot Covered: 0\n",
    )
    assert report.parse_mutants(summary).counts["survived"] == 3


def test_unknown_or_empty_mutation_reports_are_errors(tmp_path: Path) -> None:
    with pytest.raises(ReportError, match="not a mutmut"):
        report.parse_mutants(written(tmp_path, "x.txt", "nothing here"))
    with pytest.raises(ReportError, match="holds no mutants"):
        report.mutant_samples(report.tally("mutmut", []))


def test_mutant_samples_cap_modules_and_rank_survivors_first() -> None:
    modules = [(f"m{i:03d}", "killed") for i in range(60)] + [("z", "survived")] * 3
    samples = report.mutant_samples(report.tally("mutmut", [("killed", 60)], modules))
    by_module = [s for s in samples if s.name == "aye_mutants_survived"]
    assert len(by_module) == report.MODULE_LIMIT
    assert by_module[0] == Sample("aye_mutants_survived", 3.0, {"tool": "mutmut", "module": "z"})


def test_prometheus_text_sorts_escapes_and_drops_empty_labels() -> None:
    text = report.prometheus_text(
        [Sample("aye_x", 1.5, {"b": 'say "hi"\n', "a": "", "c": "x\\y"}), Sample("aye_y", 2.0)],
        1000,
    )
    assert text == 'aye_x{b="say \\"hi\\"\\n",c="x\\\\y"} 1.5 1000\naye_y 2 1000\n'


def test_format_value_keeps_integers_short_and_rounds_floats() -> None:
    assert report.format_value(3.0) == "3"
    assert report.format_value(0.1234567) == "0.123457"
    assert report.format_value(float("nan")) == "nan"


def test_context_of_a_push_and_of_a_pull_request() -> None:
    assert report.context_labels(PUSH_ENV) == {
        "project": "shop",
        "repo": "acme/shop",
        "branch": "main",
        "pipeline": "ci",
        "job": "test",
    }
    pull = {**PUSH_ENV, "GITHUB_HEAD_REF": "feature/x", "GITHUB_REF_NAME": "12/merge"}
    assert report.context_labels(pull, "given")["branch"] == "feature/x"
    assert report.context_labels(pull, "given")["project"] == "given"
    gitlab = {"CI_PROJECT_PATH": "g/app", "CI_COMMIT_REF_NAME": "dev", "CI_JOB_URL": "https://j"}
    assert report.context_labels(gitlab)["branch"] == "dev"
    assert report.run_labels(gitlab)["url"] == "https://j"


def test_run_labels_build_the_github_run_link() -> None:
    assert report.run_labels(PUSH_ENV) == {
        "pipeline_id": "4242",
        "job_id": "",
        "attempt": "2",
        "commit": "abc123",
        "url": "https://github.com/acme/shop/actions/runs/4242",
    }


def test_parse_labels_accepts_pairs_and_rejects_junk() -> None:
    assert report.parse_labels(" env=prod , team = core ,") == {"env": "prod", "team": "core"}
    for bad in ("novalue", "1x=y"):
        with pytest.raises(Exception, match="not a label"):
            report.parse_labels(bad)


def test_import_target_moves_userinfo_to_basic_auth_and_appends_the_path() -> None:
    target = report.import_target("https://user:p%40ss@vm.example:8428/")
    assert target.url == "https://vm.example:8428/api/v1/import/prometheus"
    assert target.headers["Authorization"] == "Basic dXNlcjpwQHNz"
    again = report.import_target("http://vm:8428/api/v1/import/prometheus?extra_label=a=b", "t")
    assert again.url == "http://vm:8428/api/v1/import/prometheus?extra_label=a=b"
    assert again.headers["Authorization"] == "Bearer t"
    with pytest.raises(ReportError, match="AYE_METRICS_URL"):
        report.import_target("vm:8428")


class FakeResponse:
    def __init__(self, status: int):
        self.status = status

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *args: object) -> None:
        return None


def answering(monkeypatch: pytest.MonkeyPatch, codes: list[int | Exception]) -> list[Any]:
    calls: list[Any] = []

    def urlopen(request: Any, timeout: float) -> FakeResponse:
        calls.append(request)
        code = codes.pop(0)
        if isinstance(code, Exception):
            raise code
        if code >= 400:
            raise urllib.error.HTTPError(request.full_url, code, "x", Message(), io.BytesIO())
        return FakeResponse(code)

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    return calls


def test_push_retries_server_errors_and_stops_on_client_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = report.import_target("http://vm:8428")
    delays: list[float] = []
    calls = answering(monkeypatch, [503, urllib.error.URLError("refused"), 204])
    report.push("x 1 1\n", target, retries=3, backoff=1.0, sleep=delays.append)
    assert (len(calls), delays) == (3, [1.0, 2.0])
    assert calls[0].data == b"x 1 1\n"
    answering(monkeypatch, [400])
    with pytest.raises(ReportError, match="HTTP 400"):
        report.push("x", target, retries=3, sleep=delays.append)
    answering(monkeypatch, [500, 500])
    with pytest.raises(ReportError, match="after 2 attempt"):
        report.push("x", target, retries=1, sleep=delays.append)


def options(**changes: Any) -> report.Options:
    base: dict[str, Any] = {
        "url": "",
        "token": "",
        "strict": False,
        "dry_run": False,
        "print_text": False,
        "retries": 0,
        "timeout": 1.0,
        "backoff": 0.0,
    }
    return report.Options(**{**base, **changes})


def test_without_url_a_report_file_is_enough_and_appends(tmp_path: Path) -> None:
    out = tmp_path / "report.prom"
    samples = [Sample("aye_x", 1.0)]
    assert report.deliver(samples, options(out=str(out)), NOW) == 0
    assert report.deliver(samples, options(out=str(out)), NOW + 1) == 0
    assert out.read_text(encoding="utf-8").splitlines() == [
        "aye_x 1 1792800000000",
        "aye_x 1 1792800001000",
    ]


def test_without_url_or_file_it_warns_and_fails_only_when_strict(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert report.deliver([Sample("aye_x", 1.0)], options(), NOW) == 0
    assert report.deliver([Sample("aye_x", 1.0)], options(strict=True), NOW) == 1
    assert "AYE_METRICS_URL is not set" in capsys.readouterr().err


def test_a_failed_push_is_a_warning_unless_strict(monkeypatch: pytest.MonkeyPatch) -> None:
    answering(monkeypatch, [400, 400])
    assert report.deliver([Sample("aye_x", 1.0)], options(url="http://vm"), NOW) == 0
    assert report.deliver([Sample("aye_x", 1.0)], options(url="http://vm", strict=True), NOW) == 1


def test_the_log_line_goes_to_loki_with_run_details_off_the_stream_labels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = answering(monkeypatch, [204, 204])
    samples = report.with_labels(
        [
            *report.step_samples("deploy", 12.5, "ok", NOW),
            report.info_sample("step", "deploy", PUSH_ENV, result="ok"),
        ],
        report.context_labels(PUSH_ENV),
    )
    assert report.deliver(samples, options(url="http://vm", logs_url="http://loki:3100"), NOW) == 0
    loki = calls[0]
    assert loki.full_url == "http://loki:3100/loki/api/v1/push"
    stream = json.loads(loki.data)["streams"][0]
    assert stream["stream"] == {"job": "aye-report", "project": "shop", "report": "step"}
    line = json.loads(stream["values"][0][1])
    assert line["url"] == "https://github.com/acme/shop/actions/runs/4242"
    assert line["values"] == {
        "step_duration_seconds": 12.5,
        "step_success": 1.0,
        "step_finished_timestamp_seconds": NOW,
    }
    assert stream["values"][0][0] == "1792800000000000000"


def test_run_ids_and_commits_live_only_on_the_info_series(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = report.main(
        ["junit", "--dry-run", str(fixture("pytest-junit.xml"))], PUSH_ENV, no_git, lambda: NOW
    )
    assert code == 0
    text = capsys.readouterr().out
    for line in text.splitlines():
        has_run = "abc123" in line or "4242" in line
        assert has_run == line.startswith("aye_report_info"), line
    assert (
        samples_of(text)[
            'aye_tests{branch="main",job="test",outcome="failed",pipeline="ci",project="shop",repo="acme/shop"}'
        ]
        == 1.0
    )


def test_step_report_and_suite_label(capsys: pytest.CaptureFixture[str]) -> None:
    argv = ["step", "--dry-run", "--name", "deploy", "--duration", "30", "--result", "fail"]
    assert report.main(argv, {"AYE_PROJECT": "shop"}, no_git, lambda: NOW) == 0
    found = samples_of(capsys.readouterr().out)
    assert found['aye_step_success{project="shop",step="deploy"}'] == 0.0
    assert found['aye_step_duration_seconds{project="shop",result="fail",step="deploy"}'] == 30.0
    argv = ["coverage", "--dry-run", "--suite", "unit", str(fixture("coverage.xml"))]
    assert report.main(argv, {}, no_git, lambda: NOW) == 0
    assert (
        'aye_coverage_lines_percent{format="cobertura",suite="unit"} 100' in capsys.readouterr().out
    )


def test_unreadable_report_is_a_warning_and_strict_makes_it_fail(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    missing = str(tmp_path / "none.xml")
    assert report.main(["junit", missing], {}, no_git, lambda: NOW) == 0
    assert report.main(["junit", "--strict", missing], {}, no_git, lambda: NOW) == 1
    assert "none.xml" in capsys.readouterr().err


def test_bad_step_names_and_durations_are_usage_errors() -> None:
    for argv in (
        ["step", "--name", "Deploy", "--duration", "1", "--result", "ok"],
        ["step", "--name", "deploy", "--duration", "-1", "--result", "ok"],
        ["step", "--name", "deploy", "--duration", "inf", "--result", "ok"],
    ):
        with pytest.raises(SystemExit):
            report.main(argv, {}, no_git, lambda: NOW)


def test_run_times_the_command_and_keeps_its_exit_code(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(report, "run_command", lambda command: 3)
    argv = ["run", "--dry-run", "--name", "build", "--", "false"]
    assert report.main(argv, {}, no_git, lambda: NOW) == 3
    assert 'aye_step_success{step="build"} 0' in capsys.readouterr().out
    assert report.main(["run", "--name", "build", "--"], {}, no_git, lambda: NOW) == 2


def test_run_command_maps_missing_and_forbidden_commands(tmp_path: Path) -> None:
    assert report.run_command([str(tmp_path / "missing-command")]) == 127


def event(tmp_path: Path, pull: dict[str, Any]) -> dict[str, str]:
    path = written(tmp_path, "event.json", json.dumps({"pull_request": pull}))
    return {"GITHUB_EVENT_PATH": str(path)}


def fake_git(answers: dict[str, str]) -> Callable[..., str]:
    def git(*args: str) -> str:
        key = " ".join(args)
        if key not in answers:
            raise ReportError(key)
        return answers[key]

    return git


def test_pr_lead_time_from_a_merged_pull_request_event(tmp_path: Path) -> None:
    env = event(
        tmp_path,
        {
            "number": 7,
            "merged_at": "2026-10-20T12:00:00Z",
            "created_at": "2026-10-20T10:00:00Z",
            "base": {"ref": "main", "sha": "b"},
            "head": {"sha": "h"},
            "commits": 2,
        },
    )
    git = fake_git({"log --format=%at b..h": "1792488000\n1792490000\n"})
    lead = report.find_lead_time(env, git, "", "", NOW)
    assert (lead.start, lead.commits, lead.number, lead.base) == ("first_commit", 2, "7", "main")
    assert lead.seconds == report.parse_time("2026-10-20T12:00:00Z") - 1792488000
    opened = report.find_lead_time(env, fake_git({}), "", "", NOW)
    assert (opened.start, opened.seconds) == ("opened", 7200.0)


def test_pr_lead_time_from_a_merge_commit_and_from_given_times() -> None:
    git = fake_git(
        {
            "rev-list --parents -n 1 HEAD": "m p1 p2\n",
            "log -1 --format=%ct HEAD": "1000\n",
            "log --format=%at HEAD^1..HEAD^2": "400\n100\n",
        }
    )
    lead = report.find_lead_time({}, git, "", "", NOW)
    assert (lead.seconds, lead.commits) == (900.0, 2)
    given = report.find_lead_time(
        {}, no_git, "2026-01-01T00:00:00", "2026-01-01T01:00:00+00:00", NOW
    )
    assert given.seconds == 3600.0
    with pytest.raises(ReportError, match="no lead time"):
        report.find_lead_time({}, fake_git({"rev-list --parents -n 1 HEAD": "m p1\n"}), "", "", NOW)
    with pytest.raises(ReportError, match="older than"):
        report.pr_samples(report.LeadTime(10.0, 5.0, "given"))
    with pytest.raises(ReportError, match="not a time"):
        report.parse_time("yesterday")


def test_pr_samples_label_the_base_branch() -> None:
    samples = report.pr_samples(report.LeadTime(0.0, 60.0, "first_commit", 3, "7", "main"))
    assert samples == [
        Sample("aye_pr_lead_time_seconds", 60.0, {"start": "first_commit", "branch": "main"}),
        Sample("aye_pr_commits", 3.0, {"start": "first_commit", "branch": "main"}),
    ]


def test_the_python_310_copy_is_the_same_file() -> None:
    source = (ROOT / "src" / "ayeops" / "report.py").read_bytes()
    assert (ROOT / "ci" / "aye-report.py").read_bytes() == source


def test_the_cli_hands_report_its_arguments(capsys: pytest.CaptureFixture[str]) -> None:
    from ayeops.cli import main

    argv = ["report", "step", "--dry-run", "--name", "ci", "--duration", "1", "--result", "ok"]
    assert main(argv) == 0
    assert "aye_step_success" in capsys.readouterr().out


@pytest.mark.skipif(shutil.which("sh") is None, reason="needs a POSIX shell")
def test_the_deploy_helper_writes_the_same_series(tmp_path: Path) -> None:
    script = (ROOT / "ci" / "deploy.sh").as_posix()
    body = subprocess.run(
        ["sh", "-c", f'. "{script}"; aye__body deploy 42 ok'],
        env={"AYE_PROJECT": "shop", "AYE_JOB": 'say "hi"', "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    lines = body.splitlines()
    assert lines[0].startswith('aye_step_duration_seconds{project="shop",job="say \\"hi\\"",')
    assert ',result="ok",step="deploy"} 42 ' in lines[0]
    assert lines[1].startswith(
        'aye_step_success{project="shop",job="say \\"hi\\"",step="deploy"} 1 '
    )
    assert lines[3].startswith("aye_report_info{")
