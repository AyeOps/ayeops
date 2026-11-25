from __future__ import annotations

import json
import re
from typing import Any

import yaml

from ayeops import report
from ayeops.registry import Module
from tests.conftest import EXAMPLE_STAMP, ROOT, manifest_from, render_once, rules_hit

MODULES = ROOT / "src" / "ayeops" / "modules"
NAMES = re.compile(r"\b(aye:[a-z0-9_:]+|aye_[a-z0-9_]+|ayeops_github_[a-z0-9_]+)\b")


def delivery(data: dict[str, Any], **settings: Any) -> dict[str, Any]:
    data["hosts"]["central"]["modules"]["delivery"] = settings
    return data


def rules_of(data: dict[str, Any], modules: dict[str, Module]) -> dict[str, dict[str, Any]]:
    tree = render_once(manifest_from(data), modules, EXAMPLE_STAMP)
    groups = yaml.safe_load(tree["central/delivery/rules.yml"])["groups"]
    return {group["name"]: group for group in groups}


def test_recording_rules_count_only_the_watched_branches_and_escape_dots(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    groups = rules_of(delivery(data, branches=["main", "release.1"]), modules)
    exprs = [rule["expr"] for rule in groups["delivery"]["rules"]]
    assert exprs
    assert all('branch=~"main|release\\\\.1|"' in expr for expr in exprs)
    assert "delivery-expected" not in groups


def test_expected_reports_turn_into_an_overdue_series_per_step(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    expect = [{"step": "deploy", "every": "2d"}, {"step": "ci", "every": "6h", "project": "web"}]
    rules = rules_of(delivery(data, expect=expect), modules)["delivery-expected"]["rules"]
    overdue = [rule for rule in rules if rule["record"] == "aye:delivery_overdue"]
    assert [rule["labels"] for rule in overdue] == [
        {"project": "acme", "step": "deploy", "expected": "2d"},
        {"project": "web", "step": "ci", "expected": "6h"},
    ]
    assert overdue[0]["expr"].endswith("> bool 172800 or vector(1)")
    assert overdue[1]["expr"].endswith("> bool 21600 or vector(1)")
    assert 'project="web",step="ci"' in overdue[1]["expr"]


def test_delivery_runs_on_one_host_only(data: dict[str, Any]) -> None:
    data["hosts"]["edge"]["modules"]["metrics"] = {"role": "server"}
    data["hosts"]["edge"]["modules"]["delivery"] = {}
    assert "delivery-single" in rules_hit(data)


def test_expect_intervals_are_validated(data: dict[str, Any]) -> None:
    for bad in ({"step": "deploy", "every": "2w"}, {"step": "Deploy", "every": "1d"}):
        assert "module-settings" in rules_hit(delivery(data, expect=[bad]))


def recorded() -> set[str]:
    text = (MODULES / "delivery" / "files" / "rules.yml.j2").read_text(encoding="utf-8")
    names = set(re.findall(r'record: "(aye:[a-z0-9_:]+)"', text))
    names |= {
        f"aye:{metric[4:]}:{kind}"
        for metric in re.findall(r'"(aye_[a-z_]+)"', text)
        for kind in ("last", "median30d")
    }
    return names


def produced() -> set[str]:
    source = (ROOT / "src" / "ayeops" / "report.py").read_text(encoding="utf-8")
    exporter = (MODULES / "github" / "files" / "github_exporter.py").read_text(encoding="utf-8")
    pushed = set(re.findall(r'"(aye_[a-z_]+)"', source))
    pushed |= {
        f"aye_coverage_{kind}_{part}"
        for kind in ("lines", "branches")
        for part in ("covered", "valid")
    }
    return pushed | set(re.findall(r'"(ayeops_github_[a-z_]+)"', exporter)) | recorded()


def exprs_of_dashboard(name: str) -> list[str]:
    board = json.loads((MODULES / "delivery" / "dashboards" / name).read_text(encoding="utf-8"))
    exprs = [t["expr"] for p in board["panels"] for t in p.get("targets", [])]
    exprs += [v["definition"] for v in board["templating"]["list"]]
    return exprs


def test_every_series_a_dashboard_or_alert_reads_is_produced(modules: dict[str, Module]) -> None:
    known = produced()
    exprs = exprs_of_dashboard("delivery.json") + exprs_of_dashboard("quality.json")
    exprs += [alert["expr"] for name in ("delivery", "github") for alert in modules[name].alerts]
    used = {name for expr in exprs for name in NAMES.findall(expr)}
    assert used - known == set()
    assert {"aye_mutants_survived", "aye_tests_flaky", "aye:delivery_overdue"} <= used


def test_the_two_dashboards_have_their_own_uids_and_no_overlapping_panels() -> None:
    for name, uid in (("delivery.json", "ayeops-delivery"), ("quality.json", "ayeops-quality")):
        board = json.loads((MODULES / "delivery" / "dashboards" / name).read_text("utf-8"))
        assert board["uid"] == uid
        cells = set()
        for panel in board["panels"]:
            grid = panel["gridPos"]
            assert grid["x"] + grid["w"] <= 24
            for x in range(grid["x"], grid["x"] + grid["w"]):
                for y in range(grid["y"], grid["y"] + grid["h"]):
                    assert (x, y) not in cells, panel["title"]
                    cells.add((x, y))


def test_report_samples_never_put_run_ids_on_series_the_rules_aggregate() -> None:
    run = {"pipeline_id", "job_id", "attempt", "commit", "url", "number"}
    samples = [
        *report.step_samples("deploy", 1.0, "ok", 1.0),
        *report.junit_samples(
            report.JunitSummary((report.CaseResult("a", "passed", 1.0),), 1.0), 3
        ),
        *report.coverage_samples(report.Coverage("lcov", (1, 2), None)),
        *report.mutant_samples(report.tally("mutmut", [("killed", 1)], [("m", "killed")])),
        *report.pr_samples(report.LeadTime(0.0, 1.0, "given")),
    ]
    assert all(run.isdisjoint(sample.labels) for sample in samples)
