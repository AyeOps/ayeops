from __future__ import annotations

import json
import urllib.parse
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from tests.test_autopilot_agent import (
    NOON,
    SECRET,
    Harness,
    api_answer,
    autopilot,
    grafana_alert,
    payload,
)

RESOLVED = NOON + 600
SETTLED = RESOLVED + autopilot.SETTLE + 1
ALERT_LABELS = {"alertname": "Website down", "severity": "critical", "grafana_folder": "AyeOps"}


def attempt_line(started: list[str], success: bool = True) -> str:
    return json.dumps(
        {"event": "attempt", "project": "acme-apps", "started": started, "success": success},
        sort_keys=True,
    )


def hypothesis(cause_class: str, action: str, target: str) -> dict[str, Any]:
    return {
        "cause": f"{cause_class} of {target}",
        "cause_class": cause_class,
        "confidence": "medium",
        "evidence": [],
        "action": action,
        "target": target,
    }


def resolved_run(h: Harness) -> str:
    h.engine.on_webhook(payload(grafana_alert()))
    h.clock.now = RESOLVED
    h.engine.on_webhook(payload(grafana_alert(status="resolved")))
    run: str = h.lines()[0]["run"]
    return run


def metrics(h: Harness) -> str:
    text: str = h.engine.counters.render()
    return text


def queries(h: Harness, path: str) -> list[dict[str, str]]:
    found = []
    for url, _, _ in h.http.calls:
        parsed = urllib.parse.urlparse(url)
        if parsed.path == path:
            found.append({k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()})
    return found


def incident(
    alert: str,
    start: float,
    end: float,
    labels: Mapping[str, str],
    host: str = "",
    modules: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "schema": 1,
        "id": f"{alert}-{int(start)}",
        "alert": alert,
        "host": host,
        "labels": dict(labels),
        "modules": modules or [],
        "start_ts": start,
        "end_ts": end,
    }


def write_incidents(path: Path, *records: Mapping[str, Any]) -> Path:
    path.write_text("".join(json.dumps(r) + "\n" for r in records) + "not json\n", "utf-8")
    return path


def test_hypotheses_carry_a_cause_class_from_the_model_or_from_their_action(
    tmp_path: Path,
) -> None:
    h = Harness(tmp_path)
    h.http.model_answer = api_answer(
        [
            hypothesis("dependency-down", "needs-a-person", "postgres"),
            {**hypothesis("made-up", "restart-container", "web"), "cause_class": "made-up"},
            {
                k: v
                for k, v in hypothesis("x", "prune-build-cache", "central").items()
                if k != "cause_class"
            },
        ]
    )
    h.engine.on_webhook(payload(grafana_alert()))
    classes = [x["cause_class"] for x in h.lines()[0]["hypotheses"]]
    assert classes == ["dependency-down", "container-down", "disk-full"]
    schema = h.model_calls()[0]["output_config"]["format"]["schema"]
    item = schema["properties"]["hypotheses"]["items"]
    assert "cause_class" in item["required"]
    assert item["properties"]["cause_class"]["enum"] == list(autopilot.CAUSES)


def test_a_journal_line_without_a_cause_class_takes_it_from_its_action() -> None:
    assert autopilot.cause_of({"action": "redeploy-previous-render"}) == "bad-change"
    assert autopilot.cause_of({"action": "restart-container"}) == "container-down"
    assert autopilot.cause_of({"action": "needs-a-person"}) == "other"
    assert autopilot.cause_of({}) == "other"
    assert autopilot.cause_of({"action": "restart-container", "cause_class": "host-down"}) == (
        "host-down"
    )


@pytest.mark.parametrize(
    ("one", "other", "same"),
    [
        ("web", "acme-apps-web-1", True),
        ("acme-apps-web-1", "web", True),
        ("Acme_Apps", "acme-apps-web-1", True),
        ("apps web", "acme-apps-web-1", True),
        ("acme-apps-web-1", "acme-apps-web-1", True),
        ("we", "acme-apps-web-1", False),
        ("web-2", "acme-apps-web-1", False),
        ("central", "acme-apps", False),
        ("", "acme-apps", False),
        ("web", "", False),
    ],
)
def test_targets_match_on_whole_parts_of_a_name(one: str, other: str, same: bool) -> None:
    assert autopilot.same_target(one, other) is same


def test_the_matched_rank_is_the_first_hypothesis_of_the_class_on_the_target() -> None:
    hypotheses = [
        hypothesis("bad-change", "redeploy-previous-render", "web"),
        hypothesis("container-down", "restart-container", "postgres"),
        hypothesis("container-down", "restart-container", "web"),
    ]
    exact = autopilot.Fix("reconciler", "container-down", ("acme-apps-web-1",), True, "")
    loose = autopilot.Fix("reconciler", "container-down", ("acme-apps",), False, "")
    other = autopilot.Fix("incidents", "host-down", ("central",), False, "")
    assert autopilot.matched_rank(hypotheses, exact) == 3
    assert autopilot.matched_rank(hypotheses, loose) == 2
    assert autopilot.matched_rank(hypotheses, other) == 0
    assert autopilot.matched_rank([*hypotheses, *hypotheses], other) == 0
    old = [{"action": "restart-container", "target": "acme-apps-web-1"}]
    assert autopilot.matched_rank(old, exact) == 1


def test_a_restart_by_the_reconciler_labels_the_run_once_it_settles(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.http.restarted_projects = ["acme-apps"]
    h.http.reconciler_lines = [
        attempt_line(["acme-apps-web-1"]),
        attempt_line(["acme-apps-mcp-1"], success=False),
        "not json",
        json.dumps({"event": "gave-up", "started": ["acme-apps-worker-1"]}),
    ]
    run = resolved_run(h)
    h.clock.now = RESOLVED + autopilot.SETTLE - 1
    assert h.engine.label(h.clock.now) == 0
    assert "ayeops_autopilot_label_pending 1" in metrics(h)
    h.clock.now = SETTLED
    assert h.engine.label(h.clock.now) == 1
    (label,) = h.lines("label")
    assert label["run"] == run
    assert label["source"] == "reconciler"
    assert label["matched_rank"] == 2
    assert label["fix"] == {
        "cause_class": "container-down",
        "targets": ["acme-apps-web-1"],
        "exact": True,
        "evidence": "the reconciler on central started acme-apps-web-1",
    }
    (asked,) = [q for q in queries(h, "/api/v1/query") if "reconciler" in q["query"]]
    assert asked["query"] == 'increase(ayeops_reconciler_successes_total{host="central"}[1000s])'
    assert float(asked["time"]) == RESOLVED + 60
    (logs,) = [q for q in queries(h, "/loki/api/v1/query_range") if "reconciler" in q["query"]]
    assert logs["query"] == '{host="central",compose_service="reconciler"} |= "attempt"'
    assert int(logs["start"]) == int((NOON - 40 - autopilot.LEAD) * 1e9)
    text = metrics(h)
    assert (
        'ayeops_autopilot_labelled_runs{alertname="Website down",cause="container-down",'
        'matched="2",source="reconciler"} 1'
    ) in text
    assert 'ayeops_autopilot_hit_rate{cause="container-down",rank="1"} 0' in text
    assert 'ayeops_autopilot_hit_rate{cause="container-down",rank="2"} 1' in text
    assert 'ayeops_autopilot_hit_rate{cause="all",rank="3"} 1' in text
    assert "ayeops_autopilot_label_pending 0" in text
    assert h.engine.label(h.clock.now + 60) == 0
    assert len(h.lines("label")) == 1


def test_without_loki_the_reconciler_names_its_project_and_matches_by_class(
    tmp_path: Path,
) -> None:
    h = Harness(tmp_path, loki="")
    h.http.restarted_projects = ["acme-apps"]
    resolved_run(h)
    h.clock.now = SETTLED
    h.engine.label(h.clock.now)
    (label,) = h.lines("label")
    assert label["fix"]["targets"] == ["acme-apps"]
    assert label["fix"]["exact"] is False
    assert label["matched_rank"] == 2
    assert queries(h, "/loki/api/v1/query_range") == []


def test_a_restart_found_only_in_the_logs_still_counts(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.http.reconciler_lines = [attempt_line(["acme-apps-web-1"])]
    resolved_run(h)
    h.clock.now = SETTLED
    h.engine.label(h.clock.now)
    assert h.lines("label")[0]["fix"]["targets"] == ["acme-apps-web-1"]


def test_a_host_outage_in_the_incident_journal_labels_the_run_host_down(
    tmp_path: Path,
) -> None:
    journal = write_incidents(
        tmp_path / "incidents.jsonl",
        incident("Host down", NOON - 120, RESOLVED - 30, {"host": "central"}, "central", ["node"]),
        incident("Host down", NOON - 120, RESOLVED - 30, {"host": "edge"}, "edge", ["node"]),
        incident("Website down", NOON - 40, RESOLVED, ALERT_LABELS),
    )
    h = Harness(tmp_path, incidents=journal)
    h.http.model_answer = api_answer(
        [
            hypothesis("container-down", "restart-container", "web"),
            hypothesis("host-down", "needs-a-person", "central"),
        ]
    )
    resolved_run(h)
    h.clock.now = SETTLED
    h.engine.label(h.clock.now)
    (label,) = h.lines("label")
    assert label["source"] == "incidents"
    assert label["fix"]["cause_class"] == "host-down"
    assert label["fix"]["targets"] == ["central"]
    assert label["matched_rank"] == 2
    assert "Host down on central" in label["fix"]["evidence"]


@pytest.mark.parametrize(
    ("start", "end", "host_down"),
    [
        (NOON - 120, RESOLVED - 30, True),
        (NOON - 400, NOON - 40 - autopilot.LEAD, True),
        (NOON - 120, RESOLVED + autopilot.LEAD, True),
        (RESOLVED, RESOLVED + 60, True),
        (NOON - 120, NOON - 40 - autopilot.LEAD - 1, False),
        (NOON - 120, RESOLVED + autopilot.LEAD + 1, False),
        (RESOLVED + 1, RESOLVED + 60, False),
    ],
)
def test_a_host_outage_counts_only_when_its_end_is_near_the_recovery(
    tmp_path: Path, start: float, end: float, host_down: bool
) -> None:
    journal = write_incidents(
        tmp_path / "incidents.jsonl",
        incident("Host down", start, end, {"host": "central"}, "central", ["node"]),
    )
    h = Harness(tmp_path, incidents=journal)
    resolved_run(h)
    h.clock.now = SETTLED
    h.engine.label(h.clock.now)
    assert (h.lines("label")[0]["source"] == "incidents") is host_down


def test_the_run_s_own_incident_and_a_service_incident_are_no_host_outage(
    tmp_path: Path,
) -> None:
    journal = write_incidents(
        tmp_path / "incidents.jsonl",
        incident("Website down", NOON - 40, RESOLVED, ALERT_LABELS, "central", ["node"]),
        incident("Disk full", NOON - 40, RESOLVED, {"host": "central"}, "central"),
    )
    h = Harness(tmp_path, incidents=journal)
    resolved_run(h)
    h.clock.now = SETTLED
    h.engine.label(h.clock.now)
    assert h.lines("label")[0]["source"] == "none"


def test_the_reconciler_wins_over_a_host_outage(tmp_path: Path) -> None:
    journal = write_incidents(
        tmp_path / "incidents.jsonl",
        incident("Host down", NOON - 120, RESOLVED - 30, {"host": "central"}, "central", ["node"]),
    )
    h = Harness(tmp_path, incidents=journal)
    h.http.reconciler_lines = [attempt_line(["acme-apps-web-1"])]
    resolved_run(h)
    h.clock.now = SETTLED
    h.engine.label(h.clock.now)
    assert h.lines("label")[0]["source"] == "reconciler"


def test_the_incident_journal_closes_a_run_whose_resolve_never_came(tmp_path: Path) -> None:
    journal = write_incidents(
        tmp_path / "incidents.jsonl",
        incident("Website down", NOON - 40 + 90, RESOLVED, {**ALERT_LABELS, "host": "edge"}),
        incident("Website down", NOON - 40 + 200, RESOLVED + 120, ALERT_LABELS),
        incident(
            "Website down", NOON - 40 + 10, RESOLVED + 180, {**ALERT_LABELS, "service": "mcp"}
        ),
        incident("Website down", NOON - 40 + 100, RESOLVED + 60, ALERT_LABELS),
        incident("Website down", NOON - 40 + 30, RESOLVED, ALERT_LABELS),
    )
    h = Harness(tmp_path, incidents=journal)
    h.engine.on_webhook(payload(grafana_alert()))
    assert "ayeops_autopilot_open_runs 1" in metrics(h)
    h.clock.now = RESOLVED + 10
    assert h.engine.label(h.clock.now) == 0
    closes = [line for line in h.lines("outcome") if line.get("from") == "incidents"]
    assert len(closes) == 1
    assert autopilot.parse_time(closes[0]["resolved_at"]) == RESOLVED
    assert closes[0]["minutes_to_resolve"] == 10.7
    assert "ayeops_autopilot_open_runs 0" in metrics(h)
    h.clock.now = SETTLED
    assert h.engine.label(h.clock.now) == 1
    assert len([line for line in h.lines("outcome") if line.get("from")]) == 1
    h.engine.on_webhook(payload(grafana_alert(status="resolved")))
    assert len(h.lines("outcome")) == 1


def test_no_signal_leaves_the_run_unexplained_not_missed(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    resolved_run(h)
    h.clock.now = SETTLED
    h.engine.label(h.clock.now)
    (label,) = h.lines("label")
    assert (label["source"], label["reason"], label["fix"], label["matched_rank"]) == (
        "none",
        "no-signal",
        None,
        None,
    )
    text = metrics(h)
    assert 'ayeops_autopilot_unexplained_runs{alertname="Website down",reason="no-signal"} 1' in (
        text
    )
    assert "ayeops_autopilot_hit_rate" not in text
    assert "ayeops_autopilot_labelled_runs" not in text


ACKNOWLEDGED = {
    "acknowledged": True,
    "acknowledged_at": "2026-01-01T12:02:10Z",
    "acknowledged_by": "Olena Kovalenko",
    "acknowledged_via": "telegram",
    "acknowledge_seconds": 170.0,
}


def test_a_person_who_acknowledged_explains_why_no_signal_was_found(tmp_path: Path) -> None:
    journal = write_incidents(
        tmp_path / "incidents.jsonl",
        {**incident("Website down", NOON - 40, RESOLVED, ALERT_LABELS), **ACKNOWLEDGED},
    )
    h = Harness(tmp_path, incidents=journal)
    resolved_run(h)
    h.clock.now = SETTLED
    h.engine.label(h.clock.now)
    (label,) = h.lines("label")
    assert (label["source"], label["reason"], label["fix"]) == ("none", "acknowledged", None)
    assert label["acknowledged"] == {
        "at": "2026-01-01T12:02:10Z",
        "by": "Olena Kovalenko",
        "via": "telegram",
        "seconds": 170.0,
    }
    text = metrics(h)
    assert (
        'ayeops_autopilot_unexplained_runs{alertname="Website down",reason="acknowledged"} 1'
        in (text)
    )


def test_an_acknowledgement_rides_along_with_an_automatic_fix(tmp_path: Path) -> None:
    journal = write_incidents(
        tmp_path / "incidents.jsonl",
        {**incident("Website down", NOON - 40, RESOLVED, ALERT_LABELS), **ACKNOWLEDGED},
    )
    h = Harness(tmp_path, incidents=journal)
    h.http.reconciler_lines = [attempt_line(["acme-apps-web-1"])]
    resolved_run(h)
    h.clock.now = SETTLED
    h.engine.label(h.clock.now)
    (label,) = h.lines("label")
    assert (label["source"], label["reason"]) == ("reconciler", None)
    assert label["acknowledged"]["by"] == "Olena Kovalenko"


@pytest.mark.parametrize("acknowledged", [False, None])
def test_an_incident_nobody_acknowledged_stays_without_a_signal(
    tmp_path: Path, acknowledged: bool | None
) -> None:
    record = incident("Website down", NOON - 40, RESOLVED, ALERT_LABELS)
    journal = write_incidents(
        tmp_path / "incidents.jsonl", {**record, "acknowledged": acknowledged}
    )
    h = Harness(tmp_path, incidents=journal)
    resolved_run(h)
    h.clock.now = SETTLED
    h.engine.label(h.clock.now)
    (label,) = h.lines("label")
    assert (label["reason"], label["acknowledged"]) == ("no-signal", None)


def test_the_acknowledgement_of_another_alert_does_not_count() -> None:
    other = {**incident("Disk full", NOON - 40, RESOLVED, {"host": "central"}), **ACKNOWLEDGED}
    run = {
        "alertname": "Website down",
        "labels": ALERT_LABELS,
        "alert_at": autopilot.iso(NOON - 40),
    }
    assert autopilot.incident_of(run, [other]) is None
    assert autopilot.acknowledgement(None) is None


def test_a_run_never_resolved_is_given_up_after_two_days(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.engine.on_webhook(payload(grafana_alert()))
    h.clock.now = NOON + autopilot.LABEL_HORIZON - 1
    assert h.engine.label(h.clock.now) == 0
    h.clock.now = NOON + autopilot.LABEL_HORIZON
    assert h.engine.label(h.clock.now) == 1
    label = h.lines("label")[0]
    assert (label["reason"], label["resolved_at"]) == ("unresolved", None)
    assert queries(h, "/api/v1/query") == [
        q for q in queries(h, "/api/v1/query") if "reconciler" not in q["query"]
    ]


def test_a_failed_query_labels_nothing_and_is_retried(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.http.fail_reconciler = True
    resolved_run(h)
    h.clock.now = SETTLED
    assert h.engine.label(h.clock.now) == 0
    assert h.lines("label") == []
    assert 'ayeops_autopilot_failures_total{stage="label"} 1' in metrics(h)
    h.http.fail_reconciler = False
    h.http.restarted_projects = ["acme-apps"]
    assert h.engine.label(h.clock.now + 60) == 1


def test_an_unreadable_incident_journal_labels_nothing(tmp_path: Path) -> None:
    broken = tmp_path / "incidents.jsonl"
    broken.write_bytes(b"\xff\xfe\x00")
    h = Harness(tmp_path, incidents=broken)
    resolved_run(h)
    h.clock.now = SETTLED
    assert h.engine.label(h.clock.now) == 0
    assert 'ayeops_autopilot_failures_total{stage="label"} 1' in metrics(h)


def test_a_missing_incident_journal_is_no_error(tmp_path: Path) -> None:
    h = Harness(tmp_path, incidents=tmp_path / "absent.jsonl")
    resolved_run(h)
    h.clock.now = SETTLED
    assert h.engine.label(h.clock.now) == 1


def test_failed_and_skipped_runs_and_runs_without_hypotheses_get_no_label(
    tmp_path: Path,
) -> None:
    h = Harness(tmp_path)
    h.http.model_answer = api_answer([])
    resolved_run(h)
    h.clock.now = SETTLED
    assert h.engine.label(h.clock.now) == 0
    assert "ayeops_autopilot_label_pending 0" in metrics(h)


def test_a_person_s_verdict_wins_over_the_automatic_label(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.http.restarted_projects = ["acme-apps"]
    run = resolved_run(h)
    h.clock.now = SETTLED
    h.engine.label(h.clock.now)
    assert h.engine.outcome({"run": run, "matched_rank": 1, "note": "rolled back"})
    text = metrics(h)
    assert 'matched="2",source="reconciler"' not in text
    assert (
        'ayeops_autopilot_labelled_runs{alertname="Website down",cause="bad-change",'
        'matched="1",source="person"} 1'
    ) in text
    assert 'ayeops_autopilot_hit_rate{cause="bad-change",rank="1"} 1' in text


def test_a_graded_run_is_not_labelled_again(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    run = resolved_run(h)
    assert h.engine.outcome({"run": run, "matched_rank": 0, "cause": "dependency-down"})
    h.clock.now = SETTLED
    assert h.engine.label(h.clock.now) == 0
    assert queries(h, "/api/v1/query") == [
        q for q in queries(h, "/api/v1/query") if "reconciler" not in q["query"]
    ]
    text = metrics(h)
    assert 'cause="dependency-down",matched="none",source="person"} 1' in text
    assert 'ayeops_autopilot_hit_rate{cause="dependency-down",rank="3"} 0' in text


@pytest.mark.parametrize(
    ("rank", "cause", "expected"),
    [
        (2, None, "container-down"),
        (0, None, "unknown"),
        (3, None, "unknown"),
        (1, "disk-full", "disk-full"),
    ],
)
def test_the_class_of_a_verdict_is_given_or_comes_from_the_hypothesis_at_its_rank(
    tmp_path: Path, rank: int, cause: str | None, expected: str
) -> None:
    h = Harness(tmp_path)
    run = resolved_run(h)
    assert h.engine.outcome({"run": run, "matched_rank": rank, "cause": cause})
    assert f'cause="{expected}"' in metrics(h)


def test_a_verdict_with_an_unknown_class_is_refused(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    run = resolved_run(h)
    assert not h.engine.outcome({"run": run, "matched_rank": 1, "cause": "gremlins"})
    assert [line for line in h.lines("outcome") if "recorded_at" in line] == []


def test_a_verdict_keeps_its_class_and_target_in_the_journal(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    run = resolved_run(h)
    h.engine.outcome({"run": run, "matched_rank": 0, "cause": "cert-or-dns", "target": "cdn"})
    (verdict,) = [line for line in h.lines("outcome") if "recorded_at" in line]
    assert (verdict["cause_class"], verdict["target"]) == ("cert-or-dns", "cdn")


def test_the_hit_rate_mixes_sources_and_ranks(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.http.restarted_projects = ["acme-apps"]
    for index, fingerprint in enumerate(("a1", "a2", "a3", "a4")):
        h.clock.now = NOON + index * 1000
        h.engine.on_webhook(payload(grafana_alert(fingerprint=fingerprint, starts=h.clock.now)))
        h.clock.now += 300
        h.engine.on_webhook(payload(grafana_alert(fingerprint=fingerprint, status="resolved")))
    runs = [line["run"] for line in h.lines()]
    h.engine.outcome({"run": runs[0], "matched_rank": 1})
    h.engine.outcome({"run": runs[1], "matched_rank": 0, "cause": "bad-change"})
    h.clock.now += autopilot.SETTLE + 1
    assert h.engine.label(h.clock.now) == 2
    text = metrics(h)
    assert 'ayeops_autopilot_hit_rate{cause="all",rank="1"} 0.25' in text
    assert 'ayeops_autopilot_hit_rate{cause="all",rank="2"} 0.75' in text
    assert 'ayeops_autopilot_hit_rate{cause="bad-change",rank="1"} 0.5' in text
    assert 'ayeops_autopilot_hit_rate{cause="container-down",rank="2"} 1' in text


def test_the_hit_rate_is_rebuilt_from_the_journal_after_a_restart(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.http.restarted_projects = ["acme-apps"]
    resolved_run(h)
    h.clock.now = SETTLED
    h.engine.label(h.clock.now)
    before = [line for line in metrics(h).splitlines() if "hit_rate" in line or "labelled" in line]
    assert before
    again = h.build()
    after = [
        line
        for line in again.counters.render().splitlines()
        if "hit_rate" in line or "labelled" in line
    ]
    assert after == before


def test_labelling_only_reads_and_messages_nobody(tmp_path: Path) -> None:
    sent: list[str] = []
    h = Harness(tmp_path, post=sent.append)
    h.http.restarted_projects = ["acme-apps"]
    resolved_run(h)
    calls_before = len(h.http.calls)
    h.clock.now = SETTLED
    h.engine.label(h.clock.now)
    later = h.http.calls[calls_before:]
    assert later
    assert all(body is None for _, body, _ in later)
    hosts = {urllib.parse.urlparse(url).netloc for url, _, _ in later}
    assert hosts == {"victoriametrics:8428", "loki:3100"}
    assert len(sent) == 1


def test_the_label_loop_survives_an_error_and_keeps_its_pace() -> None:
    class Broken:
        counters = autopilot.Counters()

        def clock(self) -> float:
            return NOON

        def label(self, now: float) -> int:
            raise RuntimeError("boom")

    slept: list[float] = []

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        if len(slept) == 2:
            raise KeyboardInterrupt

    engine = Broken()
    with pytest.raises(KeyboardInterrupt):
        autopilot.label_loop(engine, sleep)
    assert slept == [autopilot.LABEL_EVERY, autopilot.LABEL_EVERY]
    assert engine.counters.get("failures_total", stage="label") == 2


def test_the_incident_journal_path_comes_from_the_environment(tmp_path: Path) -> None:
    environ = {"AYE_AUTOPILOT_WEBHOOK_SECRET": SECRET}
    assert autopilot.settings_from(environ).incidents is None
    environ["AYE_AUTOPILOT_INCIDENTS"] = "/incidents/incidents.jsonl"
    assert autopilot.settings_from(environ).incidents == Path("/incidents/incidents.jsonl")


class Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any], dict[str, str]]] = []

    def __call__(
        self, url: str, body: bytes | None, headers: Mapping[str, str], timeout: float
    ) -> bytes:
        self.calls.append((url, json.loads(body or b"{}"), dict(headers)))
        return b""


def test_the_outcome_command_sends_the_class_target_and_note() -> None:
    http = Recorder()
    environ = {"AYE_AUTOPILOT_WEBHOOK_SECRET": SECRET}
    argv = ["r1", "0", "--cause", "dependency-down", "postgres", "was", "--target", "db", "full"]
    assert autopilot.record_outcome(environ, argv, http) == 0
    ((url, body, headers),) = http.calls
    assert url == "http://127.0.0.1:8080/outcome"
    assert headers["Authorization"] == f"Bearer {SECRET}"
    assert body == {
        "run": "r1",
        "matched_rank": 0,
        "fixed_by": "person",
        "cause": "dependency-down",
        "target": "db",
        "note": "postgres was full",
    }


def test_the_outcome_command_refuses_an_unknown_class_before_sending() -> None:
    http = Recorder()
    environ = {"AYE_AUTOPILOT_WEBHOOK_SECRET": SECRET}
    assert autopilot.record_outcome(environ, ["r1", "1", "--cause", "gremlins"], http) == 2
    assert autopilot.record_outcome(environ, ["r1", "one"], http) == 2
    assert autopilot.record_outcome(environ, ["r1"], http) == 2
    assert http.calls == []
    assert autopilot.record_outcome(environ, ["r1", "1", "--cause"], http) == 0
    assert http.calls[0][1]["cause"] is None
    assert http.calls[0][1]["note"] == "--cause"


def test_the_runs_command_lists_the_last_runs_with_their_label(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    h = Harness(tmp_path)
    h.http.restarted_projects = ["acme-apps"]
    run = resolved_run(h)
    h.clock.now = SETTLED
    h.engine.label(h.clock.now)
    h.clock.now += 10
    h.engine.on_webhook(payload(grafana_alert(fingerprint="beef", starts=h.clock.now)))
    environ = {"AYE_AUTOPILOT_STATE": str(tmp_path)}
    assert autopilot.main(environ, ["runs"]) == 0
    first, second = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert first["run"] == run
    assert first["label"] == {
        "source": "reconciler",
        "cause_class": "container-down",
        "matched_rank": 2,
    }
    assert autopilot.parse_time(first["resolved_at"]) == RESOLVED
    assert [x["cause_class"] for x in first["hypotheses"]] == ["bad-change", "container-down"]
    assert first["hypotheses"][1] == {
        "rank": 2,
        "cause_class": "container-down",
        "action": "restart-container",
        "target": "acme-apps-web-1",
    }
    assert second["label"] is None and second["resolved_at"] is None
    assert autopilot.main(environ, ["runs", "1"]) == 0
    (only,) = capsys.readouterr().out.splitlines()
    assert json.loads(only)["run"] == second["run"]
    assert autopilot.main(environ, ["runs", "0"]) == 0
    assert capsys.readouterr().out == ""
    assert autopilot.main(environ, ["runs", "many"]) == 2
