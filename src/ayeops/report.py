#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import json
import math
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ElementTree
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

IMPORT_PATH = "/api/v1/import/prometheus"
LOKI_PATH = "/loki/api/v1/push"
URL_ENV = "AYE_METRICS_URL"
TOKEN_ENV = "AYE_METRICS_TOKEN"
LOGS_URL_ENV = "AYE_LOGS_URL"
LOGS_TOKEN_ENV = "AYE_LOGS_TOKEN"
OUT_ENV = "AYE_REPORT_FILE"
LABEL_NAME = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")
STEP_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,39}$")
RESULTS = ("ok", "fail")
OUTCOMES = ("passed", "failed", "error", "skipped")
MUTANT_STATUSES = ("killed", "survived", "timeout", "no_coverage", "error", "ignored")
FLAKY_TAGS = frozenset({"flakyFailure", "flakyError", "rerunFailure", "rerunError"})
FLAKY_LIMIT = 50
MODULE_LIMIT = 50
PER_ITEM = frozenset({"aye_test_slowest_seconds", "aye_test_flaky", "aye_mutants_survived"})
TEST_NAME_LIMIT = 200

Environment = Mapping[str, str]
Git = Callable[..., str]


class ReportError(Exception):
    pass


@dataclass(frozen=True)
class Sample:
    name: str
    value: float
    labels: Mapping[str, str] = field(default_factory=dict)


def escape_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def format_value(value: float) -> str:
    if math.isfinite(value) and value == int(value) and abs(value) < 1e15:
        return str(int(value))
    return repr(round(float(value), 6))


def prometheus_text(samples: Iterable[Sample], timestamp_ms: int) -> str:
    lines = []
    for sample in samples:
        pairs = [
            f'{name}="{escape_label(value)}"'
            for name, value in sorted(sample.labels.items())
            if value != ""
        ]
        selector = f"{sample.name}{{{','.join(pairs)}}}" if pairs else sample.name
        lines.append(f"{selector} {format_value(sample.value)} {timestamp_ms}")
    return "".join(f"{line}\n" for line in lines)


def first(env: Environment, *names: str) -> str:
    for name in names:
        value = env.get(name, "").strip()
        if value:
            return value
    return ""


def context_labels(env: Environment, project: str = "") -> dict[str, str]:
    repo = first(env, "AYE_REPO", "GITHUB_REPOSITORY", "CI_PROJECT_PATH")
    branch = first(
        env,
        "AYE_BRANCH",
        "GITHUB_HEAD_REF",
        "GITHUB_REF_NAME",
        "CI_MERGE_REQUEST_SOURCE_BRANCH_NAME",
        "CI_COMMIT_BRANCH",
        "CI_COMMIT_REF_NAME",
    )
    name = project or first(env, "AYE_PROJECT") or (repo.rsplit("/", 1)[-1] if repo else "")
    return {
        "project": name,
        "repo": repo,
        "branch": branch,
        "pipeline": first(env, "AYE_PIPELINE", "GITHUB_WORKFLOW", "CI_PIPELINE_NAME"),
        "job": first(env, "AYE_JOB", "GITHUB_JOB", "CI_JOB_NAME"),
    }


def run_labels(env: Environment) -> dict[str, str]:
    url = first(env, "AYE_RUN_URL", "CI_JOB_URL")
    run_id = first(env, "GITHUB_RUN_ID")
    repository = first(env, "GITHUB_REPOSITORY")
    if not url and run_id and repository:
        server = first(env, "GITHUB_SERVER_URL") or "https://github.com"
        url = f"{server}/{repository}/actions/runs/{run_id}"
    return {
        "pipeline_id": first(env, "AYE_PIPELINE_ID", "GITHUB_RUN_ID", "CI_PIPELINE_ID"),
        "job_id": first(env, "AYE_JOB_ID", "CI_JOB_ID"),
        "attempt": first(env, "AYE_RUN_ATTEMPT", "GITHUB_RUN_ATTEMPT"),
        "commit": first(env, "AYE_COMMIT", "GITHUB_SHA", "CI_COMMIT_SHA"),
        "url": url,
    }


def parse_labels(text: str) -> dict[str, str]:
    labels: dict[str, str] = {}
    for part in (item.strip() for item in text.split(",")):
        if not part:
            continue
        name, sep, value = part.partition("=")
        name = name.strip()
        if not sep or not LABEL_NAME.match(name):
            raise argparse.ArgumentTypeError(f"not a label k=v: {part!r}")
        labels[name] = value.strip()
    return labels


def with_labels(samples: Iterable[Sample], base: Mapping[str, str]) -> list[Sample]:
    return [Sample(s.name, s.value, {**base, **s.labels}) for s in samples]


@dataclass(frozen=True)
class Target:
    url: str
    headers: Mapping[str, str]


def import_target(
    url: str,
    token: str = "",
    api: str = IMPORT_PATH,
    env_name: str = URL_ENV,
    content_type: str = "text/plain; charset=utf-8",
) -> Target:
    parts = urllib.parse.urlsplit(url.strip())
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ReportError(f"{env_name} is not an http(s) URL")
    headers = {"Content-Type": content_type}
    if parts.username is not None:
        user = urllib.parse.unquote(parts.username)
        password = urllib.parse.unquote(parts.password or "")
        encoded = base64.b64encode(f"{user}:{password}".encode()).decode()
        headers["Authorization"] = f"Basic {encoded}"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    path = parts.path.rstrip("/")
    if not path.endswith(api):
        path += api
    netloc = parts.netloc.rsplit("@", 1)[-1]
    return Target(urllib.parse.urlunsplit((parts.scheme, netloc, path, parts.query, "")), headers)


def push(
    text: str,
    target: Target,
    retries: int = 3,
    timeout: float = 10.0,
    backoff: float = 1.0,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    data = text.encode("utf-8")
    problem = ""
    for attempt in range(retries + 1):
        request = urllib.request.Request(
            target.url, data=data, headers=dict(target.headers), method="POST"
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                status = int(response.status)
            if 200 <= status < 300:
                return
            problem = f"HTTP {status}"
        except urllib.error.HTTPError as error:
            error.close()
            problem = f"HTTP {error.code}"
            if error.code < 500 and error.code != 429:
                raise ReportError(f"{target.url} answered {problem}") from error
        except (urllib.error.URLError, OSError) as error:
            problem = str(getattr(error, "reason", error))
        if attempt < retries:
            sleep(min(backoff * 2**attempt, 30.0))
    raise ReportError(f"{target.url}: {problem} after {retries + 1} attempt(s)")


@dataclass(frozen=True)
class Options:
    url: str
    token: str
    strict: bool
    dry_run: bool
    print_text: bool
    retries: int
    timeout: float
    backoff: float
    out: str = ""
    logs_url: str = ""
    logs_token: str = ""


def warn(message: str, strict: bool) -> int:
    print(f"aye report: {'error' if strict else 'warning'}: {message}", file=sys.stderr)
    return 1 if strict else 0


def log_values(samples: Sequence[Sample]) -> dict[str, float]:
    values: dict[str, float] = {}
    for sample in samples:
        if sample.name == "aye_report_info" or sample.name in PER_ITEM:
            continue
        suffix = "".join(
            f".{sample.labels[key]}" for key in ("outcome", "status") if sample.labels.get(key)
        )
        values[f"{sample.name[4:]}{suffix}"] = sample.value
    return values


def log_record(samples: Sequence[Sample]) -> dict[str, object]:
    info = next((s for s in samples if s.name == "aye_report_info"), None)
    labels = info.labels if info else {}
    record: dict[str, object] = {key: value for key, value in labels.items() if value}
    record["values"] = log_values(samples)
    return record


def loki_body(samples: Sequence[Sample], now: float) -> str:
    record = log_record(samples)
    stream = {"job": "aye-report"}
    for key in ("project", "report"):
        value = record.get(key)
        if isinstance(value, str) and value:
            stream[key] = value
    line = json.dumps(record, sort_keys=True, ensure_ascii=False)
    return json.dumps({"streams": [{"stream": stream, "values": [[str(int(now * 1e9)), line]]}]})


def write_out(path: str, text: str) -> None:
    try:
        with Path(path).open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
    except OSError as error:
        raise ReportError(f"{path}: {error.strerror or error}") from error


def describe(error: Exception) -> str:
    return str(error) if isinstance(error, ReportError) else repr(error)


def deliver(samples: Sequence[Sample], options: Options, now: float) -> int:
    text = prometheus_text(samples, int(now * 1000))
    if options.print_text or options.dry_run:
        sys.stdout.write(text)
    if options.dry_run:
        return 0
    status = 0
    if options.out:
        try:
            write_out(options.out, text)
        except ReportError as error:
            status = warn(str(error), options.strict)
        else:
            print(f"aye report: wrote {len(samples)} sample(s) to {options.out}", file=sys.stderr)
    if options.logs_url:
        try:
            target = import_target(
                options.logs_url, options.logs_token, LOKI_PATH, LOGS_URL_ENV, "application/json"
            )
            push(loki_body(samples, now), target, options.retries, options.timeout, options.backoff)
        except Exception as error:
            status = max(status, warn(f"log line not pushed: {describe(error)}", options.strict))
    if not options.url:
        if options.out:
            return status
        return warn(f"{URL_ENV} is not set, {len(samples)} sample(s) not pushed", options.strict)
    try:
        push(
            text,
            import_target(options.url, options.token),
            options.retries,
            options.timeout,
            options.backoff,
        )
    except Exception as error:
        return max(status, warn(describe(error), options.strict))
    print(f"aye report: pushed {len(samples)} sample(s)", file=sys.stderr)
    return status


def info_sample(report: str, name: str, env: Environment, **extra: str) -> Sample:
    return Sample(
        "aye_report_info", 1.0, {"report": report, "name": name, **run_labels(env), **extra}
    )


def step_samples(name: str, duration: float, result: str, finished: float) -> list[Sample]:
    return [
        Sample("aye_step_duration_seconds", round(duration, 3), {"step": name, "result": result}),
        Sample("aye_step_success", 1.0 if result == "ok" else 0.0, {"step": name}),
        Sample("aye_step_finished_timestamp_seconds", round(finished, 3), {"step": name}),
    ]


def local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_seconds(value: str | None) -> float:
    if not value:
        return 0.0
    try:
        return float(value.replace(",", ""))
    except ValueError:
        return 0.0


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError as error:
        raise ReportError(f"{path}: {error.strerror or error}") from error


def parse_xml(path: Path) -> ElementTree.Element:
    try:
        return ElementTree.fromstring(read_text(path).lstrip("\ufeff"))
    except ElementTree.ParseError as error:
        raise ReportError(f"{path}: not XML: {error}") from error


@dataclass(frozen=True)
class CaseResult:
    name: str
    outcome: str
    seconds: float
    flaky: bool = False


@dataclass(frozen=True)
class JunitSummary:
    cases: tuple[CaseResult, ...]
    duration: float

    def count(self, outcome: str) -> int:
        return sum(1 for case in self.cases if case.outcome == outcome)

    @property
    def flaky(self) -> list[str]:
        return [case.name for case in self.cases if case.flaky]

    def slowest(self, limit: int) -> list[CaseResult]:
        timed = [case for case in self.cases if case.seconds > 0]
        return sorted(timed, key=lambda case: (-case.seconds, case.name))[:limit]


def case_outcome(element: ElementTree.Element) -> str:
    tags = {local(child.tag) for child in element}
    if "failure" in tags:
        return "failed"
    if "error" in tags:
        return "error"
    if "skipped" in tags:
        return "skipped"
    return "passed"


def case_name(element: ElementTree.Element) -> str:
    owner = element.get("classname") or element.get("class") or element.get("file") or ""
    name = element.get("name") or ""
    full = f"{owner}::{name}" if owner else name
    return full[:TEST_NAME_LIMIT]


def junit_duration(root: ElementTree.Element, cases: Sequence[float]) -> float:
    if root.get("time"):
        return parse_seconds(root.get("time"))
    suites = [child for child in root if local(child.tag) == "testsuite" and child.get("time")]
    if suites:
        return sum(parse_seconds(suite.get("time")) for suite in suites)
    return sum(cases)


def parse_junit(paths: Sequence[Path]) -> JunitSummary:
    runs: dict[str, list[CaseResult]] = {}
    duration = 0.0
    for path in paths:
        root = parse_xml(path)
        if local(root.tag) not in ("testsuites", "testsuite"):
            raise ReportError(f"{path}: root element is <{local(root.tag)}>, not a JUnit report")
        seconds: list[float] = []
        for element in root.iter():
            if local(element.tag) != "testcase":
                continue
            outcome = case_outcome(element)
            retried = any(local(child.tag) in FLAKY_TAGS for child in element)
            case = CaseResult(
                case_name(element),
                outcome,
                parse_seconds(element.get("time")),
                retried and outcome == "passed",
            )
            seconds.append(case.seconds)
            runs.setdefault(case.name, []).append(case)
        duration += junit_duration(root, seconds)
    cases = []
    for attempts in runs.values():
        last = attempts[-1]
        failed_before = any(a.outcome in ("failed", "error") for a in attempts[:-1])
        flaky = last.flaky or (last.outcome == "passed" and failed_before)
        total = sum(a.seconds for a in attempts)
        cases.append(CaseResult(last.name, last.outcome, total, flaky))
    return JunitSummary(tuple(cases), duration)


def junit_samples(summary: JunitSummary, slowest: int) -> list[Sample]:
    samples = [
        Sample("aye_tests", float(summary.count(outcome)), {"outcome": outcome})
        for outcome in OUTCOMES
    ]
    samples.append(Sample("aye_tests_duration_seconds", round(summary.duration, 3)))
    samples.append(Sample("aye_tests_flaky", float(len(summary.flaky))))
    for rank, case in enumerate(summary.slowest(slowest), start=1):
        samples.append(
            Sample(
                "aye_test_slowest_seconds",
                round(case.seconds, 3),
                {"test": case.name, "rank": str(rank)},
            )
        )
    for name in summary.flaky[:FLAKY_LIMIT]:
        samples.append(Sample("aye_test_flaky", 1.0, {"test": name}))
    return samples


@dataclass(frozen=True)
class Coverage:
    format: str
    lines: tuple[int, int] | None
    branches: tuple[int, int] | None
    line_rate: float | None = None
    branch_rate: float | None = None

    @property
    def line_percent(self) -> float | None:
        if self.lines and self.lines[1] > 0:
            return 100.0 * self.lines[0] / self.lines[1]
        return None if self.line_rate is None else 100.0 * self.line_rate

    @property
    def branch_percent(self) -> float | None:
        if self.branches is not None:
            return 100.0 * self.branches[0] / self.branches[1] if self.branches[1] > 0 else None
        return None if self.branch_rate is None else 100.0 * self.branch_rate


def counted(covered: object, total: object) -> tuple[int, int] | None:
    try:
        return int(float(str(covered))), int(float(str(total)))
    except (TypeError, ValueError):
        return None


def coverage_from_xml(path: Path) -> Coverage:
    root = parse_xml(path)
    tag = local(root.tag)
    if tag == "coverage" and root.get("line-rate") is not None:
        lines = counted(root.get("lines-covered"), root.get("lines-valid"))
        branches = counted(root.get("branches-covered"), root.get("branches-valid"))
        rate = root.get("branch-rate")
        return Coverage(
            "cobertura",
            lines,
            branches,
            parse_seconds(root.get("line-rate")),
            None if branches is not None or rate is None else parse_seconds(rate),
        )
    if tag == "coverage":
        metrics = root.find("./project/metrics")
        if metrics is None:
            raise ReportError(f"{path}: <coverage> without line-rate or project/metrics")
        conditionals = counted(metrics.get("coveredconditionals"), metrics.get("conditionals"))
        return Coverage(
            "clover",
            counted(metrics.get("coveredstatements"), metrics.get("statements")),
            conditionals,
        )
    if tag == "report":
        totals: dict[str, tuple[int, int]] = {}
        for counter in root.findall("./counter"):
            missed, covered = int(counter.get("missed", "0")), int(counter.get("covered", "0"))
            totals[counter.get("type", "")] = (covered, covered + missed)
        if "LINE" not in totals:
            raise ReportError(f"{path}: JaCoCo report without a LINE counter")
        return Coverage("jacoco", totals["LINE"], totals.get("BRANCH"))
    raise ReportError(f"{path}: <{tag}> is not Cobertura, Clover or JaCoCo")


def coverage_from_json(path: Path, data: object) -> Coverage:
    if isinstance(data, dict) and isinstance(data.get("totals"), dict):
        totals = data["totals"]
        lines = counted(totals.get("covered_lines"), totals.get("num_statements"))
        branches = counted(totals.get("covered_branches"), totals.get("num_branches"))
        if lines is None:
            raise ReportError(f"{path}: coverage.py JSON without covered_lines")
        return Coverage("coverage.py", lines, branches if branches and branches[1] else None)
    if isinstance(data, dict) and isinstance(data.get("total"), dict):
        total = data["total"]
        line, branch = total.get("lines") or {}, total.get("branches") or {}
        lines = counted(line.get("covered"), line.get("total"))
        if lines is None:
            raise ReportError(f"{path}: Istanbul summary without total.lines")
        branches = counted(branch.get("covered"), branch.get("total"))
        return Coverage("istanbul", lines, branches if branches and branches[1] else None)
    raise ReportError(f"{path}: JSON is neither coverage.py nor an Istanbul json-summary")


def coverage_from_lcov(path: Path, text: str) -> Coverage:
    lines = [0, 0]
    branches = [0, 0]
    record: dict[str, int] = {}
    hits: list[int] = []
    taken: list[int] = []

    def close() -> None:
        lines[0] += record.get("LH", sum(1 for hit in hits if hit > 0))
        lines[1] += record.get("LF", len(hits))
        branches[0] += record.get("BRH", sum(1 for hit in taken if hit > 0))
        branches[1] += record.get("BRF", len(taken))

    seen = False
    for raw in text.splitlines():
        line = raw.strip()
        key, _, value = line.partition(":")
        if key in ("LF", "LH", "BRF", "BRH"):
            record[key] = int(value or 0)
            seen = True
        elif key == "DA":
            hits.append(int(float(value.split(",")[1])) if "," in value else 0)
            seen = True
        elif key == "BRDA":
            count = value.split(",")[-1]
            taken.append(0 if count in ("-", "") else int(count))
            seen = True
        elif line == "end_of_record":
            close()
            record, hits, taken = {}, [], []
    if record or hits or taken:
        close()
    if not seen:
        raise ReportError(f"{path}: not Cobertura XML, coverage JSON or lcov")
    return Coverage(
        "lcov", (lines[0], lines[1]), (branches[0], branches[1]) if branches[1] else None
    )


def parse_coverage(path: Path) -> Coverage:
    text = read_text(path).lstrip("\ufeff")
    head = text.lstrip()[:1]
    if head == "<":
        return coverage_from_xml(path)
    if head == "{":
        try:
            data = json.loads(text)
        except ValueError as error:
            raise ReportError(f"{path}: not JSON: {error}") from error
        return coverage_from_json(path, data)
    return coverage_from_lcov(path, text)


def coverage_samples(coverage: Coverage) -> list[Sample]:
    labels = {"format": coverage.format}
    samples = []
    if coverage.line_percent is not None:
        samples.append(
            Sample("aye_coverage_lines_percent", round(coverage.line_percent, 3), labels)
        )
    if coverage.branch_percent is not None:
        samples.append(
            Sample("aye_coverage_branches_percent", round(coverage.branch_percent, 3), labels)
        )
    for kind, pair in (("lines", coverage.lines), ("branches", coverage.branches)):
        if pair is not None and pair[1] > 0:
            samples.append(Sample(f"aye_coverage_{kind}_covered", float(pair[0]), labels))
            samples.append(Sample(f"aye_coverage_{kind}_valid", float(pair[1]), labels))
    if not samples:
        raise ReportError("the coverage report holds no lines")
    return samples


@dataclass(frozen=True)
class Mutants:
    tool: str
    counts: Mapping[str, int]
    survivors: Mapping[str, int] = field(default_factory=dict)

    @property
    def score(self) -> float | None:
        detected = self.counts.get("killed", 0) + self.counts.get("timeout", 0)
        missed = self.counts.get("survived", 0) + self.counts.get("no_coverage", 0)
        return 100.0 * detected / (detected + missed) if detected + missed else None


def tally(
    tool: str, pairs: Iterable[tuple[str, int]], modules: Iterable[tuple[str, str]] = ()
) -> Mutants:
    counts = dict.fromkeys(MUTANT_STATUSES, 0)
    for status, count in pairs:
        counts[status] += count
    by_module: dict[str, int] = {}
    for module, status in modules:
        by_module[module] = by_module.get(module, 0) + (status == "survived")
    return Mutants(tool, counts, by_module)


def mutmut_module(key: str) -> str:
    name = key.rsplit("__mutmut_", 1)[0]
    module, dot, _ = name.rpartition(".")
    return module if dot else name


STRYKER_STATUS = {
    "Killed": "killed",
    "Survived": "survived",
    "Timeout": "timeout",
    "NoCoverage": "no_coverage",
    "CompileError": "error",
    "RuntimeError": "error",
    "Ignored": "ignored",
    "Pending": "ignored",
}

INFECTION_STATS = {
    "killedCount": "killed",
    "killedByStaticAnalysisCount": "killed",
    "escapedCount": "survived",
    "timeOutCount": "timeout",
    "notCoveredCount": "no_coverage",
    "errorCount": "error",
    "syntaxErrorCount": "error",
    "skippedCount": "ignored",
    "ignoredCount": "ignored",
}

MUTMUT_STATS = {
    "killed": "killed",
    "survived": "survived",
    "timeout": "timeout",
    "no_tests": "no_coverage",
    "segfault": "error",
    "caught_by_type_check": "killed",
    "suspicious": "ignored",
    "skipped": "ignored",
    "not_checked": "ignored",
    "check_was_interrupted_by_user": "ignored",
}

MUTMUT_RESULTS = {key.replace("_", " "): status for key, status in MUTMUT_STATS.items()}


def mutants_from_json(path: Path, data: object) -> Mutants:
    if isinstance(data, dict) and isinstance(data.get("files"), dict):
        found = [
            (str(name), STRYKER_STATUS.get(str(mutant.get("status")), "ignored"))
            for name, entry in data["files"].items()
            if isinstance(entry, dict)
            for mutant in entry.get("mutants") or []
            if isinstance(mutant, dict)
        ]
        return tally(
            "stryker",
            ((status, 1) for _, status in found),
            found,
        )
    if isinstance(data, dict) and isinstance(data.get("stats"), dict):
        stats = data["stats"]
        return tally(
            "infection",
            ((status, int(stats.get(key) or 0)) for key, status in INFECTION_STATS.items()),
        )
    if isinstance(data, dict) and "killed" in data and "survived" in data:
        return tally(
            "mutmut",
            ((status, int(data.get(key) or 0)) for key, status in MUTMUT_STATS.items()),
        )
    raise ReportError(f"{path}: JSON is not Stryker mutation.json, infection.json or mutmut stats")


def mutants_from_junit(path: Path) -> Mutants:
    root = parse_xml(path)
    status = {"passed": "killed", "failed": "survived", "error": "error", "skipped": "ignored"}
    found = [
        (element.get("file") or element.get("classname") or "", status[case_outcome(element)])
        for element in root.iter()
        if local(element.tag) == "testcase"
    ]
    return tally(
        "mutmut",
        ((outcome, 1) for _, outcome in found),
        ((owner, outcome) for owner, outcome in found if owner),
    )


INFECTION_SUMMARY = (
    (re.compile(r"(\d+) mutants? (?:were|was) killed"), "killed"),
    (re.compile(r"(\d+) mutants? (?:were|was) caught by Static Analysis"), "killed"),
    (re.compile(r"(\d+) mutants? (?:were|was) not covered by tests"), "no_coverage"),
    (re.compile(r"(\d+) covered mutants? (?:were|was) not detected"), "survived"),
    (re.compile(r"(\d+) (?:syntax )?errors? (?:were|was) encountered"), "error"),
    (re.compile(r"(\d+) time ?outs? (?:were|was) encountered"), "timeout"),
    (re.compile(r"(\d+) mutants? (?:were|was) configured to be ignored"), "ignored"),
    (re.compile(r"(\d+) mutants? required more time than configured"), "ignored"),
)

INFECTION_LOG_SUMMARY = {
    "killed": "killed",
    "killed by test framework": "killed",
    "killed by static analysis": "killed",
    "errored": "error",
    "syntax errors": "error",
    "escaped": "survived",
    "timed out": "timeout",
    "skipped": "ignored",
    "ignored": "ignored",
    "not covered": "no_coverage",
}

INFECTION_SECTIONS = {
    "killed": "killed",
    "killed by static analysis": "killed",
    "escaped": "survived",
    "timed out": "timeout",
    "not covered": "no_coverage",
    "errors": "error",
    "errored": "error",
    "syntax errors": "error",
    "skipped": "ignored",
    "ignored": "ignored",
}

MUTMUT_EMOJI = {
    "\U0001f389": "killed",
    "\U0001fae5": "no_coverage",
    "⏰": "timeout",
    "\U0001f914": "ignored",
    "\U0001f641": "survived",
    "\U0001f507": "ignored",
    "\U0001f9d9": "killed",
    "\U0001f4a5": "error",
    "\U0001f6d1": "ignored",
}

MUTMUT_LINE = re.compile(
    r"^\s*(\S+):\s+("
    + "|".join(sorted(map(re.escape, MUTMUT_RESULTS), key=len, reverse=True))
    + r")\s*$"
)
MUTMUT_HEADER = re.compile(
    r"^(Killed|Survived|Timed out|Suspicious|Untested/skipped)\b.*\((\d+)\)\s*$"
)
MUTMUT_HEADER_STATUS = {
    "Killed": "killed",
    "Survived": "survived",
    "Timed out": "timeout",
    "Suspicious": "ignored",
    "Untested/skipped": "ignored",
}


def infection_summary_text(text: str) -> Mutants | None:
    if "mutations were generated" not in text and "Mutation Score Indicator" not in text:
        return None
    pairs = [
        (status, int(match.group(1)))
        for pattern, status in INFECTION_SUMMARY
        for match in pattern.finditer(text)
    ]
    return tally("infection", pairs)


def infection_summary_log(text: str) -> Mutants | None:
    found: dict[str, int] = {}
    for line in text.splitlines():
        key, sep, value = line.partition(":")
        if sep and key.strip().lower() in INFECTION_LOG_SUMMARY and value.strip().isdigit():
            found[key.strip().lower()] = int(value.strip())
    if "escaped" not in found or not any(k.startswith("killed") for k in found):
        return None
    if "killed" in found:
        found.pop("killed by test framework", None)
        found.pop("killed by static analysis", None)
    return tally("infection", ((INFECTION_LOG_SUMMARY[k], v) for k, v in found.items()))


def infection_sections(path: Path, text: str) -> Mutants | None:
    lines = text.splitlines()
    pairs: list[tuple[str, int]] = []
    current: str | None = None
    seen = False
    for index, line in enumerate(lines):
        following = lines[index + 1].strip() if index + 1 < len(lines) else ""
        heading = line.strip()
        if heading.endswith(" mutants:") and following and set(following) == {"="}:
            current = INFECTION_SECTIONS.get(heading[: -len(" mutants:")].lower(), "ignored")
            seen = True
            continue
        if current and re.match(r"^\d+\) ", line):
            pairs.append((current, 1))
    if not seen:
        return None
    if not any(status == "killed" for status, _ in pairs) and "Killed mutants:" not in text:
        raise ReportError(
            f"{path}: the Infection text log lists no killed mutants; use infection.json,"
            " summary.log or --log-verbosity=all"
        )
    return tally("infection", pairs)


def mutmut_text(path: Path, text: str) -> Mutants | None:
    progress = [part for part in re.split(r"[\r\n]", text) if "\U0001f389" in part]
    if progress:
        tokens = re.findall(r"(\S+)\s+(\d+)", progress[-1].replace("\ufe0f", ""))
        return tally(
            "mutmut",
            ((MUTMUT_EMOJI[emoji], int(count)) for emoji, count in tokens if emoji in MUTMUT_EMOJI),
        )
    found = [match.groups() for match in map(MUTMUT_LINE.match, text.splitlines()) if match]
    lines = [(key, MUTMUT_RESULTS[status]) for key, status in found]
    if lines:
        if not any(status == "killed" for _, status in found):
            raise ReportError(
                f"{path}: mutmut results lists no killed mutants; mutmut 3 hides them unless"
                " run as mutmut results --all true, or report mutmut export-cicd-stats"
            )
        return tally(
            "mutmut",
            ((status, 1) for _, status in lines),
            ((mutmut_module(key), status) for key, status in lines),
        )
    headers = [match for match in map(MUTMUT_HEADER.match, text.splitlines()) if match]
    if headers:
        if not any(match.group(1) == "Killed" for match in headers):
            raise ReportError(
                f"{path}: mutmut results lists no killed mutants; report the mutmut run output,"
                " mutmut junitxml or mutmut export-cicd-stats instead"
            )
        return tally(
            "mutmut",
            ((MUTMUT_HEADER_STATUS[m.group(1)], int(m.group(2))) for m in headers),
        )
    return None


def parse_mutants(path: Path) -> Mutants:
    text = read_text(path).lstrip("\ufeff")
    head = text.lstrip()[:1]
    if head == "{":
        try:
            data = json.loads(text)
        except ValueError as error:
            raise ReportError(f"{path}: not JSON: {error}") from error
        return mutants_from_json(path, data)
    if head == "<":
        return mutants_from_junit(path)
    for parser in (
        infection_summary_text,
        infection_summary_log,
    ):
        found = parser(text)
        if found is not None:
            return found
    for path_parser in (infection_sections, mutmut_text):
        found = path_parser(path, text)
        if found is not None:
            return found
    raise ReportError(f"{path}: not a mutmut, Stryker or Infection report")


def mutant_samples(mutants: Mutants) -> list[Sample]:
    labels = {"tool": mutants.tool}
    if not sum(mutants.counts.values()):
        raise ReportError(f"the {mutants.tool} report holds no mutants")
    samples = [
        Sample("aye_mutants", float(mutants.counts[status]), {**labels, "status": status})
        for status in MUTANT_STATUSES
    ]
    if mutants.score is not None:
        samples.append(Sample("aye_mutation_score_percent", round(mutants.score, 3), labels))
    ranked = sorted(mutants.survivors.items(), key=lambda item: (-item[1], item[0]))
    for module, count in ranked[:MODULE_LIMIT]:
        samples.append(
            Sample("aye_mutants_survived", float(count), {**labels, "module": module[:200]})
        )
    return samples


def parse_time(value: str) -> float:
    text = value.strip()
    try:
        return float(text)
    except ValueError:
        pass
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError as error:
        raise ReportError(f"not a time: {value!r}") from error
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.timestamp()


def run_git(*args: str) -> str:
    try:
        done = subprocess.run(
            ["git", *args], capture_output=True, text=True, check=True, timeout=30
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ReportError(f"git {' '.join(args)} failed") from error
    return done.stdout


@dataclass(frozen=True)
class LeadTime:
    started: float
    merged: float
    start: str
    commits: int | None = None
    number: str = ""
    base: str = ""

    @property
    def seconds(self) -> float:
        return self.merged - self.started


def commit_times(git: Git, revisions: str) -> list[int]:
    try:
        return [int(value) for value in git("log", "--format=%at", revisions).split()]
    except (ReportError, ValueError):
        return []


def github_lead_time(env: Environment, git: Git) -> LeadTime | None:
    path = env.get("GITHUB_EVENT_PATH", "")
    if not path:
        return None
    try:
        event = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    pull = event.get("pull_request") if isinstance(event, dict) else None
    if not isinstance(pull, dict) or not pull.get("merged_at"):
        return None
    base = pull.get("base") or {}
    head = pull.get("head") or {}
    times = commit_times(git, f"{base.get('sha', '')}..{head.get('sha', '')}")
    commits = pull.get("commits") if isinstance(pull.get("commits"), int) else None
    common = {"number": str(pull.get("number", "")), "base": str(base.get("ref", ""))}
    merged = parse_time(str(pull["merged_at"]))
    if times:
        return LeadTime(float(min(times)), merged, "first_commit", commits or len(times), **common)
    if pull.get("created_at"):
        return LeadTime(parse_time(str(pull["created_at"])), merged, "opened", commits, **common)
    return None


def merge_commit_lead_time(git: Git) -> LeadTime | None:
    try:
        parents = git("rev-list", "--parents", "-n", "1", "HEAD").split()
        if len(parents) != 3:
            return None
        merged = float(git("log", "-1", "--format=%ct", "HEAD").strip())
    except (ReportError, ValueError):
        return None
    times = commit_times(git, "HEAD^1..HEAD^2")
    if not times:
        return None
    return LeadTime(float(min(times)), merged, "first_commit", len(times))


def find_lead_time(
    env: Environment, git: Git, started_at: str, merged_at: str, now: float
) -> LeadTime:
    if started_at:
        merged = parse_time(merged_at) if merged_at else now
        return LeadTime(parse_time(started_at), merged, "given")
    found = github_lead_time(env, git) or merge_commit_lead_time(git)
    if found is None:
        raise ReportError(
            "no lead time: not a merged pull_request event and HEAD is not a merge commit;"
            " pass --started-at (and --merged-at)"
        )
    return found


def pr_samples(lead: LeadTime) -> list[Sample]:
    if lead.seconds < 0:
        raise ReportError("the merge is older than the first commit")
    labels = {"start": lead.start, **({"branch": lead.base} if lead.base else {})}
    samples = [Sample("aye_pr_lead_time_seconds", round(lead.seconds, 3), labels)]
    if lead.commits is not None:
        samples.append(Sample("aye_pr_commits", float(lead.commits), labels))
    return samples


def run_command(command: Sequence[str]) -> int:
    try:
        return subprocess.call(list(command))
    except FileNotFoundError:
        print(f"aye report: {command[0]}: command not found", file=sys.stderr)
        return 127
    except PermissionError:
        print(f"aye report: {command[0]}: permission denied", file=sys.stderr)
        return 126


def step_name(value: str) -> str:
    if not STEP_NAME.match(value):
        raise argparse.ArgumentTypeError(f"not a step name: {value!r}")
    return value


def non_negative(value: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise argparse.ArgumentTypeError(f"not a duration in seconds: {value!r}")
    return number


REPORT_EXAMPLES = {
    "step": "aye report step --name build --duration 42.5 --result success",
    "run": "aye report run --name test -- uv run pytest",
    "junit": "aye report junit junit.xml --suite unit",
    "coverage": "aye report coverage coverage.xml",
    "mutants": "aye report mutants mutants/results.txt",
    "pr": "aye report pr --started-at 2026-11-01T09:00:00Z",
}


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--project", default="", help="project label, default AYE_PROJECT")
    common.add_argument(
        "--labels", type=parse_labels, default={}, help="extra labels k=v,k2=v2 on every sample"
    )
    common.add_argument("--url", default=None, help=f"storage URL, default {URL_ENV}")
    common.add_argument(
        "--strict", action="store_true", help="exit 1 when the report cannot be made or pushed"
    )
    common.add_argument("--dry-run", action="store_true", help="print the samples, push nothing")
    common.add_argument("--print", dest="print_text", action="store_true", help="also print")
    common.add_argument(
        "--out", default=None, help=f"also append the samples to this file, default {OUT_ENV}"
    )
    common.add_argument("--logs-url", default=None, help=f"Loki URL, default {LOGS_URL_ENV}")
    common.add_argument("--retries", type=int, default=3)
    common.add_argument("--timeout", type=float, default=10.0)
    common.add_argument("--backoff", type=float, default=1.0, help="first retry delay, doubled")
    parser = argparse.ArgumentParser(
        prog="aye report",
        description="Push delivery and code-quality metrics to VictoriaMetrics, a line to Loki.",
        epilog="examples:\n  aye report run --name test -- uv run pytest\n  aye report junit a.xml",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    kinds = parser.add_subparsers(dest="kind", required=True)
    step = kinds.add_parser("step", parents=[common], help="report a finished step")
    step.add_argument("--name", type=step_name, required=True)
    step.add_argument("--duration", type=non_negative, required=True)
    step.add_argument("--result", choices=RESULTS, required=True)
    run = kinds.add_parser("run", parents=[common], help="time a command and report its result")
    run.add_argument("--name", type=step_name, required=True)
    run.add_argument("command", nargs=argparse.REMAINDER)
    junit = kinds.add_parser("junit", parents=[common], help="report JUnit XML test results")
    junit.add_argument("files", type=Path, nargs="+")
    junit.add_argument("--slowest", type=int, default=10)
    junit.add_argument("--suite", default="")
    coverage = kinds.add_parser("coverage", parents=[common], help="report line/branch coverage")
    coverage.add_argument("file", type=Path)
    coverage.add_argument("--suite", default="")
    mutants = kinds.add_parser("mutants", parents=[common], help="report mutation testing")
    mutants.add_argument("file", type=Path)
    mutants.add_argument("--suite", default="")
    pr = kinds.add_parser("pr", parents=[common], help="report the lead time of a merge")
    pr.add_argument("--started-at", default="", help="epoch seconds or ISO-8601")
    pr.add_argument("--merged-at", default="", help="epoch seconds or ISO-8601, default now")
    for kind, example in REPORT_EXAMPLES.items():
        sub = kinds.choices[kind]
        sub.description = next(a.help for a in kinds._choices_actions if a.dest == kind)
        sub.epilog = f"example:\n  {example}"
        sub.formatter_class = argparse.RawDescriptionHelpFormatter
    return parser


def options_from(args: argparse.Namespace, env: Environment) -> Options:
    return Options(
        url=args.url if args.url is not None else env.get(URL_ENV, ""),
        token=env.get(TOKEN_ENV, ""),
        strict=args.strict,
        dry_run=args.dry_run,
        print_text=args.print_text,
        retries=max(args.retries, 0),
        timeout=args.timeout,
        backoff=max(args.backoff, 0.0),
        out=args.out if args.out is not None else env.get(OUT_ENV, ""),
        logs_url=args.logs_url if args.logs_url is not None else env.get(LOGS_URL_ENV, ""),
        logs_token=env.get(LOGS_TOKEN_ENV, ""),
    )


def report_samples(
    args: argparse.Namespace, env: Environment, git: Git, now: float
) -> list[Sample]:
    suite = {"suite": args.suite} if getattr(args, "suite", "") else {}
    if args.kind == "step":
        result = str(args.result)
        return [
            *step_samples(args.name, args.duration, result, now),
            info_sample("step", args.name, env, result=result),
        ]
    if args.kind == "junit":
        summary = parse_junit(args.files)
        samples = with_labels(junit_samples(summary, max(args.slowest, 0)), suite)
        return [*samples, info_sample("junit", args.suite or "tests", env)]
    if args.kind == "coverage":
        found = parse_coverage(args.file)
        samples = with_labels(coverage_samples(found), suite)
        return [*samples, info_sample("coverage", args.suite or found.format, env)]
    if args.kind == "mutants":
        mutants = parse_mutants(args.file)
        samples = with_labels(mutant_samples(mutants), suite)
        return [*samples, info_sample("mutants", args.suite or mutants.tool, env)]
    lead = find_lead_time(env, git, args.started_at, args.merged_at, now)
    extra = {"number": lead.number} if lead.number else {}
    return [*pr_samples(lead), info_sample("pr", lead.start, env, **extra)]


def main(
    argv: Sequence[str] | None = None,
    env: Environment | None = None,
    git: Git = run_git,
    clock: Callable[[], float] = time.time,
) -> int:
    args = build_parser().parse_args(argv)
    environment = os.environ if env is None else env
    options = options_from(args, environment)
    base = {**context_labels(environment, args.project), **args.labels}
    if args.kind == "run":
        command = list(args.command)
        if command[:1] == ["--"]:
            command = command[1:]
        if not command:
            print("aye report run: no command after --", file=sys.stderr)
            return 2
        started = time.monotonic()
        code = run_command(command)
        duration = time.monotonic() - started
        result = "ok" if code == 0 else "fail"
        samples = [
            *step_samples(args.name, duration, result, clock()),
            info_sample("step", args.name, environment, result=result),
        ]
        pushed = deliver(with_labels(samples, base), options, clock())
        return code if code != 0 else pushed
    now = clock()
    try:
        samples = report_samples(args, environment, git, now)
    except Exception as error:
        return warn(describe(error), options.strict)
    return deliver(with_labels(samples, base), options, now)


if __name__ == "__main__":
    sys.exit(main())
