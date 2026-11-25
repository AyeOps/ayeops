from __future__ import annotations

import importlib.util
import json
import sys
import urllib.parse
from datetime import datetime
from email.message import Message
from pathlib import Path
from types import ModuleType
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from ayeops.incidents import WEIGHTS
from tests.conftest import ROOT

SCRIPT = ROOT / "src" / "ayeops" / "modules" / "incidents" / "files" / "incidents_agent.py"
GRAFANA = "http://grafana:3000"
START = 1_767_258_000.0
MINUTE = 60.0
WEB = {
    "alertname": "Website down",
    "grafana_folder": "AyeOps",
    "host": "central",
    "instance": "https://acme.example",
    "service": "web",
    "severity": "critical",
}


def load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("incidents_agent_script", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


a = load_script()


def text_of(title: str, labels: dict[str, str], value: str = "A=1.000000, C=1.000000") -> str:
    body = ", ".join(f"{key}={labels[key]}" for key in sorted(labels))
    return f"{title} {{{body}}} - {value}"


class FakeClock:
    def __init__(self, now: float = START) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeGrafana:
    def __init__(self) -> None:
        self.items: list[dict[str, Any]] = []
        self.status = 200
        self.calls: list[dict[str, str]] = []
        self.next_id = 1

    def change(
        self,
        at: float,
        previous: str,
        current: str,
        labels: dict[str, str] | None = None,
        rule_id: int = 7,
        title: str = "Website down",
    ) -> None:
        labels = dict(WEB if labels is None else labels)
        self.items.append(
            {
                "id": self.next_id,
                "alertId": rule_id,
                "alertName": title,
                "time": int(at * 1000),
                "timeEnd": int(at * 1000),
                "prevState": previous,
                "newState": current,
                "text": text_of(title, labels),
                "tags": [f"{k}:{v}" for k, v in sorted(labels.items())],
                "data": {},
            }
        )
        self.next_id += 1

    def __call__(self, url: str, timeout: float) -> tuple[int, bytes]:
        parsed = urllib.parse.urlsplit(url)
        assert f"{parsed.scheme}://{parsed.netloc}" == GRAFANA
        assert parsed.path == "/api/annotations"
        query = dict(urllib.parse.parse_qsl(parsed.query))
        self.calls.append(query)
        if self.status != 200:
            return self.status, b""
        assert query["type"] == "alert"
        start, end, limit = int(query["from"]), int(query["to"]), int(query["limit"])
        hits = [item for item in self.items if item["time"] <= end and item["time"] >= start]
        hits.sort(key=lambda item: (item["time"], item["id"]), reverse=True)
        return 200, json.dumps(hits[:limit]).encode()


def config(tmp_path: Path, **overrides: Any) -> Any:
    values: dict[str, Any] = {
        "grafana": GRAFANA,
        "token_file": tmp_path / "token",
        "services": {"web": "critical", "mcp": "high", "docs": "low"},
        "night": a.Night(23 * 60, 8 * 60, ZoneInfo("UTC")),
        "downtime_severities": frozenset({"critical", "page"}),
        "state_dir": tmp_path,
        "backfill_days": 1,
    }
    values.update(overrides)
    return a.Config(**values)


def journal(tmp_path: Path) -> list[dict[str, Any]]:
    path = tmp_path / "incidents.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def sample(agent: Any, name: str, **wanted: str) -> float:
    found = [
        value
        for metric, pairs, value in agent.samples()
        if metric == name and all(dict(pairs).get(k) == v for k, v in wanted.items())
    ]
    assert len(found) == 1, (name, wanted, found)
    return float(found[0])


def outage(grafana: FakeGrafana, start: float, minutes: float, **labels: str) -> None:
    merged = {**WEB, **labels}
    grafana.change(start - 3 * MINUTE, "Normal", "Pending", merged)
    grafana.change(start, "Pending", "Alerting", merged)
    grafana.change(start + minutes * MINUTE, "Alerting", "Normal", merged)


def test_labels_come_from_the_text_grafana_writes_for_each_state_change() -> None:
    text = text_of("Website down", WEB)
    assert a.labels_from_text(text, "Website down") == WEB


def test_a_label_value_with_a_comma_and_braces_stays_whole() -> None:
    labels = {**WEB, "summary": "a, b {c} d"}
    assert a.labels_from_text(text_of("Website down", labels), "Website down") == labels


def test_a_title_with_braces_is_cut_by_the_rule_title_and_no_labels_give_an_empty_map() -> None:
    title = "Disk {root} full"
    assert a.labels_from_text(text_of(title, {"host": "edge"}), title) == {"host": "edge"}
    assert a.labels_from_text("Rule {} - Error", "Rule") == {}


def test_tags_stand_in_when_the_text_is_not_grafanas_format() -> None:
    item = {
        "id": 1,
        "alertId": 3,
        "time": 1000,
        "newState": "Alerting",
        "text": "something else",
        "tags": ["host:edge", "severity:warning", "alertname:Disk full"],
    }
    transition = a.transition_from(item)
    assert transition.labels == {"host": "edge", "severity": "warning", "alertname": "Disk full"}
    assert transition.title == "Disk full"


def test_dashboard_annotations_and_broken_items_are_skipped() -> None:
    assert a.transition_from({"id": 1, "alertId": 0, "time": 5, "newState": "Alerting"}) is None
    assert a.transition_from({"id": 1, "alertId": 2, "newState": "Alerting"}) is None
    assert a.transition_from({"id": 1, "alertId": 2, "time": 5, "newState": ""}) is None


def test_the_fingerprint_follows_rule_and_labels_not_their_order() -> None:
    assert a.fingerprint(7, WEB) == a.fingerprint(7, dict(reversed(list(WEB.items()))))
    assert a.fingerprint(7, WEB) != a.fingerprint(8, WEB)
    assert a.fingerprint(7, WEB) != a.fingerprint(7, {**WEB, "host": "edge"})


def test_one_incident_runs_from_the_first_alerting_to_normal(tmp_path: Path) -> None:
    grafana = FakeGrafana()
    clock = FakeClock()
    agent = a.Agent(config(tmp_path), grafana, clock)
    start = START - 50 * MINUTE
    grafana.change(start - 3 * MINUTE, "Normal", "Pending")
    grafana.change(start, "Pending", "Alerting")
    grafana.change(start + 5 * MINUTE, "Alerting", "Alerting (NoData)")
    grafana.change(start + 12 * MINUTE, "Alerting (NoData)", "Normal (MissingSeries)")
    assert agent.tick() == 1
    [record] = journal(tmp_path)
    assert record["service"] == "web"
    assert record["host"] == "central"
    assert record["severity"] == "critical"
    assert record["criticality"] == "critical"
    assert record["weight"] == 100
    assert record["duration_seconds"] == 12 * MINUTE
    assert record["start"] == "2026-01-01T08:10:00Z"
    assert record["end"] == "2026-01-01T08:22:00Z"
    assert record["downtime"] is True
    assert record["night"] is False
    assert record["acknowledged"] is None
    assert record["source"] == "grafana-annotations"
    assert record["fingerprint"] == a.fingerprint(7, WEB)
    assert record["id"] == f"{record['fingerprint']}-{int(start * 1000)}"
    assert [step["to"] for step in record["timeline"]] == [
        "Alerting",
        "Alerting (NoData)",
        "Normal (MissingSeries)",
    ]


def test_pending_that_never_fires_and_normal_without_an_incident_are_no_incidents(
    tmp_path: Path,
) -> None:
    grafana = FakeGrafana()
    agent = a.Agent(config(tmp_path), grafana, FakeClock())
    grafana.change(START - 20 * MINUTE, "Normal", "Pending")
    grafana.change(START - 18 * MINUTE, "Pending", "Normal")
    grafana.change(START - 10 * MINUTE, "Alerting", "Normal")
    grafana.change(START - 5 * MINUTE, "Normal", "Error")
    assert agent.tick() == 0
    assert journal(tmp_path) == []
    assert agent.tracker.open == {}


def test_an_open_incident_is_counted_and_shown_open_and_its_downtime_grows(
    tmp_path: Path,
) -> None:
    grafana = FakeGrafana()
    clock = FakeClock()
    agent = a.Agent(config(tmp_path), grafana, clock)
    grafana.change(START - 10 * MINUTE, "Pending", "Alerting")
    agent.tick()
    assert journal(tmp_path) == []
    assert sample(agent, "ayeops_incidents_open", service="web", alert_host="central") == 1
    assert sample(agent, "ayeops_incidents_total", service="web", criticality="critical") == 1
    assert sample(agent, "ayeops_incident_downtime_minutes_total", service="web") == 10
    clock.advance(5 * MINUTE)
    assert sample(agent, "ayeops_incident_downtime_minutes_total", service="web") == 10
    agent.tick()
    assert sample(agent, "ayeops_incident_downtime_minutes_total", service="web") == 15


def test_downtime_never_goes_down_when_a_close_arrives_late_not_even_after_a_restart(
    tmp_path: Path,
) -> None:
    grafana = FakeGrafana()
    clock = FakeClock()
    agent = a.Agent(config(tmp_path), grafana, clock)
    grafana.change(START - 10 * MINUTE, "Pending", "Alerting")
    agent.tick()
    assert sample(agent, "ayeops_incident_downtime_minutes_total", service="web") == 10
    clock.advance(MINUTE)
    grafana.change(START - 2 * MINUTE, "Alerting", "Normal")
    agent.tick()
    assert journal(tmp_path)[0]["duration_seconds"] == 8 * MINUTE
    assert sample(agent, "ayeops_incident_downtime_minutes_total", service="web") == 10
    agent.tick()
    restarted = a.Agent(config(tmp_path), grafana, clock)
    assert sample(restarted, "ayeops_incident_downtime_minutes_total", service="web") == 10


def test_overlapping_incidents_of_one_service_count_their_downtime_once(tmp_path: Path) -> None:
    grafana = FakeGrafana()
    agent = a.Agent(config(tmp_path), grafana, FakeClock())
    outage(grafana, START - 60 * MINUTE, 20)
    outage(grafana, START - 50 * MINUTE, 20, alertname="Website slow")
    outage(grafana, START - 20 * MINUTE, 5)
    agent.tick()
    assert len(journal(tmp_path)) == 3
    assert sample(agent, "ayeops_incidents_total", service="web") == 3
    assert sample(agent, "ayeops_incident_downtime_minutes_total", service="web") == 35


def test_warnings_count_as_incidents_but_not_as_downtime(tmp_path: Path) -> None:
    grafana = FakeGrafana()
    agent = a.Agent(config(tmp_path), grafana, FakeClock())
    outage(grafana, START - 60 * MINUTE, 20, severity="warning")
    agent.tick()
    [record] = journal(tmp_path)
    assert record["downtime"] is False
    assert sample(agent, "ayeops_incidents_total", service="web", severity="warning") == 1
    assert sample(agent, "ayeops_incident_downtime_minutes_total", service="web") == 0


def test_a_module_alert_names_its_module_as_service_with_normal_weight(tmp_path: Path) -> None:
    grafana = FakeGrafana()
    agent = a.Agent(config(tmp_path), grafana, FakeClock())
    labels = {"alertname": "Disk full", "host": "edge", "module": "node", "severity": "critical"}
    grafana.change(START - 30 * MINUTE, "Pending", "Alerting", labels, rule_id=9)
    grafana.change(START - 10 * MINUTE, "Alerting", "Normal", labels, rule_id=9)
    agent.tick()
    [record] = journal(tmp_path)
    assert (record["service"], record["criticality"], record["weight"]) == ("node", "normal", 1)
    assert sample(agent, "ayeops_service_criticality_weight", service="node") == 1


def test_every_manifest_service_exports_its_weight_and_a_zero_downtime(tmp_path: Path) -> None:
    agent = a.Agent(config(tmp_path), FakeGrafana(), FakeClock())
    agent.tick()
    assert sample(agent, "ayeops_service_criticality_weight", service="web") == 100
    assert sample(agent, "ayeops_service_criticality_weight", service="mcp") == 10
    assert sample(agent, "ayeops_service_criticality_weight", service="docs") == 0.1
    assert sample(agent, "ayeops_incident_downtime_minutes_total", service="docs") == 0
    assert a.WEIGHTS == WEIGHTS


@pytest.mark.parametrize(
    ("hour", "night"),
    [(22, False), (23, True), (2, True), (7, True), (8, False), (12, False)],
)
def test_night_wraps_around_midnight_in_the_configured_zone(hour: int, night: bool) -> None:
    zone = ZoneInfo("Europe/Kyiv")
    window = a.Night(23 * 60, 8 * 60, zone)
    moment = datetime(2026, 1, 10, hour, 30, tzinfo=zone).timestamp()
    assert window.contains(moment) is night


def test_a_window_inside_one_day_and_an_empty_one() -> None:
    zone = ZoneInfo("UTC")
    assert a.Night(60, 300, zone).contains(2 * 3600)
    assert not a.Night(60, 300, zone).contains(6 * 3600)
    assert not a.Night(60, 60, zone).contains(60 * 60)


def test_night_incidents_are_counted_by_their_start(tmp_path: Path) -> None:
    grafana = FakeGrafana()
    clock = FakeClock(START + 20 * 3600)
    agent = a.Agent(config(tmp_path), grafana, clock)
    night_start = START + 16 * 3600
    outage(grafana, night_start, 30)
    outage(grafana, START + 4 * 3600, 30, alertname="Website slow")
    agent.tick()
    records = journal(tmp_path)
    assert [record["night"] for record in records] == [False, True]
    assert sample(agent, "ayeops_incidents_night_total", service="web") == 1


def test_time_to_restore_is_a_histogram_of_closed_incidents(tmp_path: Path) -> None:
    grafana = FakeGrafana()
    agent = a.Agent(config(tmp_path), grafana, FakeClock())
    outage(grafana, START - 120 * MINUTE, 4)
    outage(grafana, START - 60 * MINUTE, 40, alertname="Website slow")
    grafana.change(START - 5 * MINUTE, "Pending", "Alerting", {**WEB, "alertname": "Third"})
    agent.tick()
    bucket = "ayeops_incident_restore_seconds_bucket"
    assert sample(agent, bucket, service="web", le="300") == 1
    assert sample(agent, bucket, service="web", le="1800") == 1
    assert sample(agent, bucket, service="web", le="3600") == 2
    assert sample(agent, bucket, service="web", le="+Inf") == 2
    assert sample(agent, "ayeops_incident_restore_seconds_count", service="web") == 2
    assert sample(agent, "ayeops_incident_restore_seconds_sum", service="web") == 44 * MINUTE
    text = agent.metrics()
    assert "# TYPE ayeops_incident_restore_seconds histogram" in text
    assert "# TYPE ayeops_incidents_total counter" in text
    assert "# TYPE ayeops_incidents_open gauge" in text


def test_pages_are_read_until_the_window_is_complete(tmp_path: Path) -> None:
    grafana = FakeGrafana()
    for index in range(7):
        outage(grafana, START - (100 - 10 * index) * MINUTE, 2, alertname=f"Alert {index}")
    found = a.read_window(
        grafana, GRAFANA, int((START - 3 * 3600) * 1000), int(START * 1000), 5, limit=4
    )
    assert len(found) == 21
    assert len({item.id for item in found}) == 21
    assert len(grafana.calls) > 1


def test_a_window_that_never_ends_is_an_error_not_a_silent_cut() -> None:
    grafana = FakeGrafana()
    for _ in range(5):
        grafana.change(START, "Pending", "Alerting")
    with pytest.raises(a.ReadError, match="share one millisecond"):
        a.read_window(grafana, GRAFANA, 0, int(START * 1000), 5, limit=3)


def test_a_long_backfill_is_read_in_chunks_up_to_now(tmp_path: Path) -> None:
    grafana = FakeGrafana()
    clock = FakeClock()
    agent = a.Agent(config(tmp_path, backfill_days=3), grafana, clock)
    outage(grafana, START - 60 * 3600, 10)
    outage(grafana, START - 2 * 3600, 10, alertname="Website slow")
    agent.tick()
    assert len(journal(tmp_path)) == 2
    assert len(grafana.calls) >= 12
    assert agent.tracker.cursor_ms == int(START * 1000)


def test_a_restart_resumes_the_open_incident_and_never_records_twice(tmp_path: Path) -> None:
    grafana = FakeGrafana()
    clock = FakeClock()
    first = a.Agent(config(tmp_path), grafana, clock)
    grafana.change(START - 10 * MINUTE, "Pending", "Alerting")
    first.tick()
    clock.advance(2 * MINUTE)
    grafana.change(clock.now - 30, "Alerting", "Normal")
    second = a.Agent(config(tmp_path), grafana, clock)
    assert list(second.tracker.open) == [a.fingerprint(7, WEB)]
    second.tick()
    second.tick()
    clock.advance(MINUTE)
    second.tick()
    [record] = journal(tmp_path)
    assert record["duration_seconds"] == 11.5 * MINUTE
    third = a.Agent(config(tmp_path), grafana, clock)
    assert len(third.records) == 1
    assert sample(third, "ayeops_incidents_total", service="web") == 1


def test_a_late_annotation_inside_the_overlap_is_still_taken(tmp_path: Path) -> None:
    grafana = FakeGrafana()
    clock = FakeClock()
    agent = a.Agent(config(tmp_path), grafana, clock)
    grafana.change(START - 10 * MINUTE, "Pending", "Alerting")
    agent.tick()
    clock.advance(MINUTE)
    grafana.change(START - 30, "Alerting", "Normal")
    agent.tick()
    assert len(journal(tmp_path)) == 1


def test_a_failed_read_keeps_the_cursor_and_reports_the_step(tmp_path: Path) -> None:
    grafana = FakeGrafana()
    clock = FakeClock()
    agent = a.Agent(config(tmp_path), grafana, clock)
    cursor = agent.tracker.cursor_ms
    grafana.status = 401
    agent.round()
    assert agent.tracker.cursor_ms == cursor
    assert sample(agent, "ayeops_incidents_step_success", step="read") == 0
    assert sample(agent, "ayeops_incidents_last_round_timestamp_seconds") == START
    grafana.status = 200
    outage(grafana, START - 30 * MINUTE, 5)
    agent.round()
    assert sample(agent, "ayeops_incidents_step_success", step="read") == 1
    assert len(journal(tmp_path)) == 1


def test_a_journal_that_cannot_be_written_keeps_the_incident_for_the_next_round(
    tmp_path: Path,
) -> None:
    grafana = FakeGrafana()
    agent = a.Agent(config(tmp_path), grafana, FakeClock())
    outage(grafana, START - 30 * MINUTE, 5)
    (tmp_path / "incidents.jsonl").mkdir()
    agent.round()
    assert sample(agent, "ayeops_incidents_step_success", step="journal") == 0
    assert agent.records == []
    (tmp_path / "incidents.jsonl").rmdir()
    agent.round()
    assert sample(agent, "ayeops_incidents_step_success", step="journal") == 1
    assert len(journal(tmp_path)) == 1


def test_health_fails_when_rounds_stop(tmp_path: Path) -> None:
    clock = FakeClock()
    agent = a.Agent(config(tmp_path), FakeGrafana(), clock)
    agent.round()
    assert agent.healthy(clock.now + 60)
    assert not agent.healthy(clock.now + 3600)


def test_union_merges_touching_and_nested_intervals() -> None:
    assert a.union_seconds([(0, 10), (5, 8), (10, 20), (30, 31)]) == 21
    assert a.union_seconds([]) == 0


def test_the_environment_of_the_rendered_service_configures_the_agent(tmp_path: Path) -> None:
    built = a.config_from(
        {
            "AYE_GRAFANA_READER_TOKEN_FILE": "/run/grafana-reader/token",
            "AYE_INCIDENTS_SERVICES": '{"web": "critical"}',
            "AYE_INCIDENTS_NIGHT_START": "22:30",
            "AYE_INCIDENTS_NIGHT_END": "07:00",
            "AYE_INCIDENTS_TIMEZONE": "Europe/Kyiv",
            "AYE_INCIDENTS_DOWNTIME_SEVERITIES": "critical,page",
            "AYE_INCIDENTS_STATE": str(tmp_path),
        }
    )
    assert built.services == {"web": "critical"}
    assert (built.night.start, built.night.end) == (22 * 60 + 30, 7 * 60)
    assert built.night.zone == ZoneInfo("Europe/Kyiv")
    assert built.downtime_severities == {"critical", "page"}
    assert built.grafana == GRAFANA
    assert built.token_file == Path("/run/grafana-reader/token")
    with pytest.raises(ValueError, match="unknown criticality"):
        a.parse_services('{"web": "urgent"}')
    with pytest.raises(ValueError, match="time of day"):
        a.parse_hhmm("25:00")


class Response:
    status = 200

    def __enter__(self) -> Response:
        return self

    def __exit__(self, *args: Any) -> None:
        return None

    def read(self) -> bytes:
        return b"[]"


class FakeOpen:
    def __init__(self, token_file: Path | None = None, refuse: int = 0) -> None:
        self.token_file = token_file
        self.refuse = refuse
        self.headers: list[str | None] = []
        self.urls: list[str] = []

    def __call__(self, request: Any, timeout: float) -> Response:
        self.headers.append(request.get_header("Authorization"))
        self.urls.append(request.full_url)
        if len(self.headers) <= self.refuse:
            if self.token_file is not None:
                self.token_file.write_text(f"glsa_rotated_{len(self.headers)}\n", "utf-8")
            raise a.urllib.error.HTTPError(request.full_url, 401, "Unauthorized", Message(), None)
        return Response()


def test_requests_carry_the_reader_token_as_a_bearer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token_file = tmp_path / "token"
    token_file.write_text("glsa_reader_one\n", encoding="utf-8")
    opened = FakeOpen()
    monkeypatch.setattr(a.urllib.request, "urlopen", opened)
    status, body = a.http_fetcher(token_file)(f"{GRAFANA}/api/annotations", 5)
    assert (status, body) == (200, b"[]")
    assert opened.headers == ["Bearer glsa_reader_one"]
    token_file.write_text("glsa_reader_two", encoding="utf-8")
    a.http_fetcher(token_file)(f"{GRAFANA}/api/annotations", 5)
    assert opened.headers[-1] == "Bearer glsa_reader_two"


def test_the_victoriametrics_fetcher_sends_no_token(monkeypatch: pytest.MonkeyPatch) -> None:
    opened = FakeOpen()
    monkeypatch.setattr(a.urllib.request, "urlopen", opened)
    assert a.http_fetcher()("http://victoriametrics:8428/api/v1/query", 5)[0] == 200
    assert opened.headers == [None]


def test_a_401_reads_the_token_file_again_and_retries_with_the_new_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token_file = tmp_path / "token"
    token_file.write_text("glsa_reader_one\n", encoding="utf-8")
    opened = FakeOpen(token_file, refuse=1)
    monkeypatch.setattr(a.urllib.request, "urlopen", opened)
    status, body = a.http_fetcher(token_file)(f"{GRAFANA}/api/annotations", 5)
    assert (status, body) == (200, b"[]")
    assert opened.headers == ["Bearer glsa_reader_one", "Bearer glsa_rotated_1"]


def test_a_401_with_the_same_token_in_the_file_is_not_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token_file = tmp_path / "token"
    token_file.write_text("glsa_reader_one\n", encoding="utf-8")
    opened = FakeOpen(refuse=1)
    monkeypatch.setattr(a.urllib.request, "urlopen", opened)
    assert a.http_fetcher(token_file)(f"{GRAFANA}/api/annotations", 5) == (401, b"")
    assert opened.headers == ["Bearer glsa_reader_one"]


def test_a_refused_token_fails_the_read_step_without_logging_the_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    token_file = tmp_path / "token"
    token_file.write_text("glsa_reader_one\n", encoding="utf-8")
    monkeypatch.setattr(a.urllib.request, "urlopen", FakeOpen(refuse=1000))
    agent = a.Agent(config(tmp_path), a.http_fetcher(token_file), FakeClock(START))
    agent.round()
    assert sample(agent, "ayeops_incidents_step_success", step="read") == 0
    out = capsys.readouterr().out
    assert "HTTP 401" in out
    assert "glsa_" not in out


@pytest.mark.parametrize(
    "content",
    [None, b"", b" \n", b"glsa_two words", "glsa_\u00e9".encode()],
    ids=["missing", "empty", "blank", "two-words", "not-ascii"],
)
def test_a_missing_or_broken_token_file_fails_the_read_and_never_shows_its_content(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    content: bytes | None,
) -> None:
    token_file = tmp_path / "token"
    if content is not None:
        token_file.write_bytes(content)
    opened = FakeOpen()
    monkeypatch.setattr(a.urllib.request, "urlopen", opened)
    agent = a.Agent(config(tmp_path), a.http_fetcher(token_file), FakeClock(START))
    agent.round()
    assert opened.urls == []
    assert sample(agent, "ayeops_incidents_step_success", step="read") == 0
    out = capsys.readouterr().out
    assert "reader token" in out
    assert "glsa_" not in out


def test_the_agent_holds_no_grafana_password() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    assert "Basic" not in text
    assert "PASSWORD" not in text


TOTAL = "ayeops_incidents_total"
DEGRADED = "ayeops_incident_degraded_minutes_total"
DOWNTIME = "ayeops_incident_downtime_minutes_total"
WEIGHT = "ayeops_service_criticality_weight"
HOST_DOWN = {"alertname": "Host down", "host": "central", "module": "node", "severity": "critical"}
TARGET_DOWN = {
    "alertname": "Scrape target down",
    "host": "central",
    "instance": "node-exporter:9100",
    "job": "node",
    "module": "metrics",
    "severity": "warning",
}
AFFECTED = "ayeops_incident_affected_module_minutes_total"
HOST_ALERTS = (a.HostAlert("Host down"), a.HostAlert("Scrape target down", (("job", "node"),)))


def hosted(tmp_path: Path, **overrides: Any) -> Any:
    values: dict[str, Any] = {
        "hosts": {"central": ("web", "mcp"), "edge": ("docs",)},
        "host_modules": {"central": ("grafana", "metrics", "node"), "edge": ("node",)},
        "host_alerts": HOST_ALERTS,
    }
    values.update(overrides)
    return config(tmp_path, **values)


def host_down(grafana: FakeGrafana, start: float, minutes: float | None, **labels: str) -> None:
    merged = {**HOST_DOWN, **labels}
    title = merged["alertname"]
    grafana.change(start, "Pending", "Alerting", merged, 3, title)
    if minutes is not None:
        grafana.change(start + minutes * MINUTE, "Alerting", "Normal", merged, 3, title)


def target_down(grafana: FakeGrafana, start: float, minutes: float, **labels: str) -> None:
    merged = {**TARGET_DOWN, **labels}
    grafana.change(start, "Pending", "Alerting", merged, 4, merged["alertname"])
    grafana.change(start + minutes * MINUTE, "Alerting", "Normal", merged, 4, merged["alertname"])


def samples_named(agent: Any, name: str) -> dict[tuple[tuple[str, str], ...], float]:
    return {pairs: value for metric, pairs, value in agent.samples() if metric == name}


def test_a_host_alert_counts_against_the_manifest_services_of_its_host(tmp_path: Path) -> None:
    grafana = FakeGrafana()
    agent = a.Agent(hosted(tmp_path), grafana, FakeClock())
    host_down(grafana, START - 30 * MINUTE, 10)
    agent.tick()
    [record] = journal(tmp_path)
    assert record["service"] == "node"
    assert record["services"] == ["node", "web", "mcp"]
    assert record["modules"] == ["grafana", "metrics"]
    for service in ("node", "web", "mcp"):
        assert sample(agent, DOWNTIME, service=service) == 10
    assert sample(agent, DOWNTIME, service="docs") == 0
    downtime = {dict(pairs)["service"] for pairs in samples_named(agent, DOWNTIME)}
    assert not {"grafana", "metrics"} & downtime
    samples = agent.samples()
    counted = {dict(pairs)["service"] for name, pairs, _ in samples if name == TOTAL}
    assert counted == {"node"}


def test_the_modules_of_the_host_are_affected_without_weight_or_downtime(tmp_path: Path) -> None:
    grafana = FakeGrafana()
    clock = FakeClock()
    agent = a.Agent(hosted(tmp_path), grafana, clock)
    host_down(grafana, START - 60 * MINUTE, 10)
    host_down(grafana, START - 55 * MINUTE, 10, instance="other")
    target_down(grafana, START - 30 * MINUTE, 5)
    agent.tick()
    assert samples_named(agent, AFFECTED) == {
        (("kind", "degraded"), ("module", "grafana")): 5,
        (("kind", "degraded"), ("module", "node")): 5,
        (("kind", "downtime"), ("module", "grafana")): 15,
        (("kind", "downtime"), ("module", "metrics")): 15,
    }
    weighted = {dict(pairs)["service"] for pairs in samples_named(agent, WEIGHT)}
    assert "grafana" not in weighted
    restarted = a.Agent(hosted(tmp_path), grafana, clock)
    assert sample(restarted, AFFECTED, module="grafana", kind="downtime") == 15


def test_the_node_target_down_degrades_the_services_of_its_host(tmp_path: Path) -> None:
    grafana = FakeGrafana()
    agent = a.Agent(hosted(tmp_path), grafana, FakeClock())
    target_down(grafana, START - 30 * MINUTE, 10)
    agent.tick()
    [record] = journal(tmp_path)
    assert record["services"] == ["metrics", "web", "mcp"]
    assert record["modules"] == ["grafana", "node"]
    assert sample(agent, DEGRADED, service="mcp") == 10
    assert sample(agent, DOWNTIME, service="mcp") == 0


def test_another_target_down_is_an_incident_of_its_own_module_only(tmp_path: Path) -> None:
    grafana = FakeGrafana()
    agent = a.Agent(hosted(tmp_path), grafana, FakeClock())
    target_down(grafana, START - 30 * MINUTE, 10, job="grafana", instance="grafana:3000")
    agent.tick()
    [record] = journal(tmp_path)
    assert (record["services"], record["modules"]) == (["metrics"], [])
    assert sample(agent, DEGRADED, service="metrics") == 10
    assert sample(agent, DEGRADED, service="web") == 0
    assert sample(agent, DEGRADED, service="mcp") == 0
    assert samples_named(agent, AFFECTED) == {}


def test_a_host_outage_during_a_service_outage_counts_once(tmp_path: Path) -> None:
    grafana = FakeGrafana()
    agent = a.Agent(hosted(tmp_path), grafana, FakeClock())
    outage(grafana, START - 60 * MINUTE, 30)
    host_down(grafana, START - 40 * MINUTE, 30)
    agent.tick()
    assert sample(agent, DOWNTIME, service="web") == 50
    assert sample(agent, DOWNTIME, service="mcp") == 30


def test_an_open_host_alert_grows_the_downtime_of_its_services(tmp_path: Path) -> None:
    grafana = FakeGrafana()
    agent = a.Agent(hosted(tmp_path), grafana, FakeClock())
    host_down(grafana, START - 15 * MINUTE, None)
    agent.tick()
    assert sample(agent, DOWNTIME, service="mcp") == 15
    assert sample(agent, AFFECTED, module="grafana", kind="downtime") == 15


def test_other_alerts_and_unknown_hosts_stay_with_their_own_service(tmp_path: Path) -> None:
    grafana = FakeGrafana()
    agent = a.Agent(hosted(tmp_path), grafana, FakeClock())
    host_down(grafana, START - 30 * MINUTE, 10, alertname="Disk full")
    host_down(grafana, START - 30 * MINUTE, 10, host="gone")
    agent.tick()
    records = journal(tmp_path)
    assert [(record["services"], record["modules"]) for record in records] == [
        (["node"], []),
        (["node"], []),
    ]
    assert sample(agent, DOWNTIME, service="web") == 0


def test_a_record_from_before_modules_is_attributed_by_the_current_hosts(tmp_path: Path) -> None:
    cfg = hosted(tmp_path)
    record = {"service": "node", "host": "central", "alert": "Host down"}
    assert a.record_affected(record, cfg) == (["node", "web", "mcp"], ["grafana", "metrics"])
    old = {**record, "services": ["node", "web", "grafana"]}
    assert a.record_affected(old, cfg) == (["node", "web", "mcp"], ["grafana", "metrics"])
    kept = {**record, "services": ["node"], "modules": []}
    assert a.record_affected(kept, cfg) == (["node"], [])
    target = {**record, "service": "metrics", "alert": "Scrape target down"}
    assert a.record_affected({**target, "labels": {"job": "loki"}}, cfg) == (["metrics"], [])
    assert a.record_affected({**target, "labels": {"job": "node"}}, cfg)[0][1:] == ["web", "mcp"]


def test_hosts_modules_and_host_alerts_come_from_the_environment(tmp_path: Path) -> None:
    alerts = [
        {"title": "Host down", "labels": {}},
        {"title": "Target down", "labels": {"job": "node"}},
    ]
    built = a.config_from(
        {
            "AYE_GRAFANA_READER_TOKEN_FILE": "/run/grafana-reader/token",
            "AYE_INCIDENTS_HOSTS": '{"central": ["web"]}',
            "AYE_INCIDENTS_HOST_MODULES": '{"central": ["grafana", "node"]}',
            "AYE_INCIDENTS_HOST_ALERTS": json.dumps(alerts),
            "AYE_INCIDENTS_STATE": str(tmp_path),
        }
    )
    assert built.hosts == {"central": ("web",)}
    assert built.host_modules == {"central": ("grafana", "node")}
    assert built.host_alerts == (
        a.HostAlert("Host down"),
        a.HostAlert("Target down", (("job", "node"),)),
    )
    assert a.parse_host_alerts('["Host down"]') == (a.HostAlert("Host down"),)
    node = a.HostAlert("Target down", (("job", "node"),))
    assert node.matches("Target down", {"job": "node", "instance": "x"})
    assert not node.matches("Target down", {})
    assert not node.matches("Target down", {"job": "loki"})
    assert not a.HostAlert("Host down").matches("Disk full", {})
    with pytest.raises(ValueError, match="JSON object"):
        a.parse_hosts("[]")
    with pytest.raises(ValueError, match="list its services"):
        a.parse_hosts('{"central": "web"}')
    with pytest.raises(ValueError, match="JSON list"):
        a.parse_host_alerts("{}")
    with pytest.raises(ValueError, match="neither a title"):
        a.parse_host_alerts('[{"labels": {}}]')
    with pytest.raises(ValueError, match="neither a title"):
        a.parse_host_alerts('[{"title": "Host down", "labels": ["job"]}]')
    with pytest.raises(ValueError, match="neither a title"):
        a.parse_host_alerts("[1]")


def test_warnings_count_as_degraded_minutes_and_criticals_do_not(tmp_path: Path) -> None:
    grafana = FakeGrafana()
    agent = a.Agent(config(tmp_path), grafana, FakeClock())
    outage(grafana, START - 90 * MINUTE, 20, severity="warning")
    outage(grafana, START - 80 * MINUTE, 20, severity="warning", alertname="Website slow")
    outage(grafana, START - 30 * MINUTE, 10)
    agent.tick()
    records = journal(tmp_path)
    assert [(r["downtime"], r["degraded"]) for r in records] == [
        (False, True),
        (False, True),
        (True, False),
    ]
    assert sample(agent, DEGRADED, service="web", criticality="critical") == 30
    assert sample(agent, DOWNTIME, service="web") == 10
    assert sample(agent, DEGRADED, service="docs") == 0


def test_a_host_warning_degrades_every_manifest_service_of_its_host(tmp_path: Path) -> None:
    grafana = FakeGrafana()
    agent = a.Agent(hosted(tmp_path), grafana, FakeClock())
    host_down(grafana, START - 30 * MINUTE, 10, severity="warning")
    agent.tick()
    assert sample(agent, DEGRADED, service="mcp") == 10
    assert sample(agent, DOWNTIME, service="mcp") == 0
    assert sample(agent, AFFECTED, module="grafana", kind="degraded") == 10
    degraded = {dict(pairs)["service"] for pairs in samples_named(agent, DEGRADED)}
    assert "grafana" not in degraded


def test_degraded_minutes_never_go_down_apart_from_downtime(tmp_path: Path) -> None:
    grafana = FakeGrafana()
    clock = FakeClock()
    agent = a.Agent(config(tmp_path), grafana, clock)
    grafana.change(START - 10 * MINUTE, "Pending", "Alerting", {**WEB, "severity": "warning"})
    agent.tick()
    assert sample(agent, DEGRADED, service="web") == 10
    clock.advance(MINUTE)
    grafana.change(START - 4 * MINUTE, "Alerting", "Normal", {**WEB, "severity": "warning"})
    agent.tick()
    assert sample(agent, DEGRADED, service="web") == 10
    assert sample(agent, DOWNTIME, service="web") == 0
    restarted = a.Agent(config(tmp_path), grafana, clock)
    assert sample(restarted, DEGRADED, service="web") == 10


def test_degraded_severities_come_from_the_environment(tmp_path: Path) -> None:
    env = {
        "AYE_GRAFANA_READER_TOKEN_FILE": "/run/grafana-reader/token",
        "AYE_INCIDENTS_STATE": str(tmp_path),
    }
    assert a.config_from(env).degraded_severities == {"warning"}
    env["AYE_INCIDENTS_DEGRADED_SEVERITIES"] = "warning,info"
    assert a.config_from(env).degraded_severities == {"warning", "info"}


LOST = "ayeops_incident_lost_requests_total"
VM = "http://victoriametrics:8428"


class FakeVictoria:
    def __init__(self, weeks: dict[int, float], service: str = "web") -> None:
        self.weeks = weeks
        self.service = service
        self.status = 200
        self.queries: list[dict[str, str]] = []

    def __call__(self, url: str, timeout: float) -> tuple[int, bytes]:
        parsed = urllib.parse.urlsplit(url)
        assert f"{parsed.scheme}://{parsed.netloc}" == VM
        assert parsed.path == "/api/v1/query"
        self.queries.append(dict(urllib.parse.parse_qsl(parsed.query)))
        if self.status != 200:
            return self.status, b""
        result = [
            {
                "metric": {"aye_week": str(week), "aye_service": self.service, "aye_host": "c"},
                "value": [0, str(value)],
            }
            for week, value in sorted(self.weeks.items())
        ]
        return 200, json.dumps({"status": "success", "data": {"result": result}}).encode()


def routed(tmp_path: Path, **overrides: Any) -> Any:
    values: dict[str, Any] = {
        "victoriametrics": VM,
        "routes": {"web": (a.Routes("central", ("shop@file", "api@file"), 30.0),)},
    }
    values.update(overrides)
    return config(tmp_path, **values)


def test_lost_requests_are_the_median_of_four_weeks_minus_what_was_served(
    tmp_path: Path,
) -> None:
    grafana = FakeGrafana()
    victoria = FakeVictoria({0: 100, 1: 900, 2: 1000, 3: 1100, 4: 5000})
    agent = a.Agent(routed(tmp_path), grafana, FakeClock(), victoria)
    outage(grafana, START - 30 * MINUTE, 10)
    agent.tick()
    [record] = journal(tmp_path)
    assert record["requests"]["web"] == {
        "served": 100,
        "expected": 1050,
        "lost": 950,
        "weeks": 4,
    }
    assert record["lost_requests"] == 950
    assert sample(agent, LOST, service="web", criticality="critical") == 950
    [asked] = victoria.queries
    assert float(asked["time"]) == START - 20 * MINUTE
    assert "[600s]" in asked["query"]
    assert asked["query"].count("label_set(") == 5
    assert ' offset 4w)), "aye_week", "4"' in asked["query"]
    assert 'host="central",service=~"shop@file|api@file",code=~"[123].."' in asked["query"]


def test_more_traffic_than_the_baseline_loses_nothing(tmp_path: Path) -> None:
    grafana = FakeGrafana()
    victoria = FakeVictoria({0: 2000, 1: 900, 2: 1000})
    agent = a.Agent(routed(tmp_path), grafana, FakeClock(), victoria)
    outage(grafana, START - 30 * MINUTE, 10)
    agent.tick()
    [record] = journal(tmp_path)
    assert record["requests"]["web"]["lost"] == 0
    assert record["requests"]["web"]["weeks"] == 2


def test_nothing_served_during_the_incident_loses_the_whole_baseline(tmp_path: Path) -> None:
    grafana = FakeGrafana()
    victoria = FakeVictoria({1: 300, 2: 300, 3: 300})
    agent = a.Agent(routed(tmp_path), grafana, FakeClock(), victoria)
    outage(grafana, START - 30 * MINUTE, 10)
    agent.tick()
    assert journal(tmp_path)[0]["lost_requests"] == 300


def test_without_a_baseline_lost_requests_are_null_not_zero(tmp_path: Path) -> None:
    grafana = FakeGrafana()
    victoria = FakeVictoria({0: 50})
    agent = a.Agent(routed(tmp_path), grafana, FakeClock(), victoria)
    outage(grafana, START - 30 * MINUTE, 10)
    agent.tick()
    [record] = journal(tmp_path)
    assert record["lost_requests"] is None
    assert record["requests"]["web"] == {"lost": None, "reason": "no baseline"}
    assert sample(agent, LOST, service="web") == 0


def test_an_incident_shorter_than_a_scrape_interval_is_not_measured(tmp_path: Path) -> None:
    grafana = FakeGrafana()
    victoria = FakeVictoria({0: 0, 1: 10})
    agent = a.Agent(routed(tmp_path), grafana, FakeClock(), victoria)
    outage(grafana, START - 30 * MINUTE, 0.25)
    agent.tick()
    [record] = journal(tmp_path)
    assert record["lost_requests"] is None
    assert record["requests"]["web"]["reason"] == "shorter than one scrape interval"
    assert victoria.queries == []


def test_a_failed_query_records_null_and_still_journals(tmp_path: Path) -> None:
    grafana = FakeGrafana()
    victoria = FakeVictoria({1: 10})
    victoria.status = 503
    agent = a.Agent(routed(tmp_path), grafana, FakeClock(), victoria)
    outage(grafana, START - 30 * MINUTE, 10)
    agent.tick()
    [record] = journal(tmp_path)
    assert record["requests"]["web"] == {"lost": None, "reason": "query failed"}
    assert sample(agent, "ayeops_incidents_request_queries_total", result="error") == 1
    assert sample(agent, "ayeops_incidents_request_queries_total", result="ok") == 0


def test_a_service_outside_the_ingress_has_no_lost_requests(tmp_path: Path) -> None:
    grafana = FakeGrafana()
    victoria = FakeVictoria({0: 0, 1: 10})
    agent = a.Agent(routed(tmp_path), grafana, FakeClock(), victoria)
    outage(grafana, START - 30 * MINUTE, 10, service="mcp")
    agent.tick()
    [record] = journal(tmp_path)
    assert (record["lost_requests"], record["requests"]) == (None, {})
    assert victoria.queries == []


def test_a_host_outage_measures_the_routed_manifest_services_of_the_host(tmp_path: Path) -> None:
    grafana = FakeGrafana()
    victoria = FakeVictoria({0: 0, 1: 40, 2: 40})
    routes = {
        "web": (a.Routes("central", ("shop@file",), 30.0),),
        "grafana": (a.Routes("central", ("grafana@file",), 30.0),),
    }
    cfg = routed(
        tmp_path,
        routes=routes,
        hosts={"central": ("web", "mcp")},
        host_modules={"central": ("grafana", "node")},
        host_alerts=HOST_ALERTS,
    )
    agent = a.Agent(cfg, grafana, FakeClock(), victoria)
    host_down(grafana, START - 30 * MINUTE, 10)
    agent.tick()
    [record] = journal(tmp_path)
    assert record["modules"] == ["grafana"]
    assert record["requests"]["web"]["lost"] == 40
    assert set(record["requests"]) == {"web"}
    [asked] = victoria.queries
    assert "grafana@file" not in asked["query"]
    assert sample(agent, LOST, service="grafana") == 0


def test_overlapping_incidents_count_their_shared_minutes_of_lost_requests_once() -> None:
    records = [
        {"start_ts": 0, "end_ts": 600, "requests": {"web": {"lost": 600}}},
        {"start_ts": 300, "end_ts": 900, "requests": {"web": {"lost": 300}}},
        {"start_ts": 100, "end_ts": 200, "requests": {"web": {"lost": 50}}},
        {"start_ts": 1000, "end_ts": 1000, "requests": {"web": {"lost": 5}}},
        {"start_ts": 2000, "end_ts": 2100, "requests": {"web": {"lost": None}}},
    ]
    cfg = a.Config(
        grafana=GRAFANA,
        token_file=Path("token"),
        services={},
        night=a.Night(0, 0, ZoneInfo("UTC")),
        downtime_severities=frozenset(),
        state_dir=Path(),
        routes={"web": (), "api": ()},
    )
    assert a.lost_samples(records, cfg) == {"web": 750.0, "api": 0.0}


def test_lost_requests_never_go_down_when_a_longer_incident_closes_later(
    tmp_path: Path,
) -> None:
    grafana = FakeGrafana()
    clock = FakeClock()
    victoria = FakeVictoria({0: 0, 1: 100})
    agent = a.Agent(routed(tmp_path), grafana, clock, victoria)
    grafana.change(START - 60 * MINUTE, "Pending", "Alerting", {**WEB, "alertname": "Slow"})
    outage(grafana, START - 50 * MINUTE, 10)
    agent.tick()
    assert sample(agent, LOST, service="web") == 100
    victoria.weeks = {0: 0, 1: 10}
    clock.advance(MINUTE)
    grafana.change(START - 30 * MINUTE, "Alerting", "Normal", {**WEB, "alertname": "Slow"})
    agent.tick()
    assert sample(agent, LOST, service="web") == 100


def test_routes_come_from_the_environment_and_odd_names_are_refused(tmp_path: Path) -> None:
    built = a.config_from(
        {
            "AYE_GRAFANA_READER_TOKEN_FILE": "/run/grafana-reader/token",
            "AYE_INCIDENTS_STATE": str(tmp_path),
            "AYE_INCIDENTS_VICTORIAMETRICS": VM,
            "AYE_INCIDENTS_ROUTES": json.dumps(
                {"web": [{"host": "central", "routes": ["shop@file"], "scrape_seconds": 30}]}
            ),
        }
    )
    assert built.victoriametrics == VM
    assert built.routes == {"web": (a.Routes("central", ("shop@file",), 30.0),)}
    bad = {"web": [{"host": "c", "routes": ['x"}) or vector(1'], "scrape_seconds": 30}]}
    with pytest.raises(ValueError, match="no Traefik name"):
        a.parse_routes(json.dumps(bad))
    with pytest.raises(ValueError, match="JSON object"):
        a.parse_routes("[]")


def test_victoriametrics_is_asked_without_the_grafana_password(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[dict[str, str]] = []

    class Answer:
        status = 200

        def __enter__(self) -> Answer:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def read(self) -> bytes:
            return b"{}"

    def urlopen(request: Any, timeout: float) -> Answer:
        seen.append(dict(request.header_items()))
        return Answer()

    monkeypatch.setattr(a.urllib.request, "urlopen", urlopen)
    a.http_fetcher()(VM + "/api/v1/query", 5)
    assert "Authorization" not in seen[0]


ONCALL = "http://oncall-bridge:8080"
BRIDGE_FILES = ROOT / "src" / "ayeops" / "modules" / "oncall" / "files"
GOALERT_LOG = json.loads(
    (ROOT / "tests" / "fixtures" / "goalert" / "alert-log.json").read_text("utf-8")
)
ACK = "ayeops_incident_acknowledge_seconds"
UNACKED = "ayeops_incidents_unacknowledged_total"
DISK = {"alertname": "Disk full", "host": "edge", "module": "disk", "severity": "critical"}
BACKUPS = {
    "alertname": "Backups failing",
    "host": "central",
    "module": "backups",
    "severity": "page",
}
WORKER_DOWN = {**HOST_DOWN, "host": "worker-01"}


def load_bridge() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "ayeops.modules.oncall.files.oncall_bridge", BRIDGE_FILES / "oncall_bridge.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def bridge_answer() -> list[dict[str, Any]]:
    bridge = load_bridge()
    config = bridge.Config(
        chat_id="4242",
        repeat_seconds=300.0,
        max_repeats=1,
        quiet=None,
        texts=bridge.load_texts(BRIDGE_FILES / "oncall_texts.json"),
    )
    engine = bridge.Engine(
        config, lambda method, payload: {"ok": True}, lambda alert_id: True, lambda: START
    )
    engine.acknowledge(41, "4242", "Olena Kovalenko")
    nodes = [node for page in GOALERT_LOG for node in page["data"]["alerts"]["nodes"]]
    answer: list[dict[str, Any]] = json.loads(json.dumps(engine.acks(nodes)))
    return answer


class FakeBridge:
    def __init__(self, alerts: list[dict[str, Any]] | None = None) -> None:
        self.alerts = bridge_answer() if alerts is None else alerts
        self.status = 200
        self.calls: list[tuple[float, float]] = []

    def __call__(self, url: str, timeout: float) -> tuple[int, bytes]:
        parsed = urllib.parse.urlsplit(url)
        assert f"{parsed.scheme}://{parsed.netloc}" == ONCALL
        assert parsed.path == "/acks"
        query = dict(urllib.parse.parse_qsl(parsed.query))
        start, end = float(query["from"]), float(query["to"])
        self.calls.append((start, end))
        if self.status != 200:
            return self.status, b""
        hits = [item for item in self.alerts if start <= item["created_at"] <= end]
        return 200, json.dumps({"alerts": hits}).encode()


def utc(text: str) -> float:
    return datetime.fromisoformat(text).timestamp()


def firing(
    grafana: FakeGrafana, start: float, minutes: float, labels: dict[str, str], rule_id: int
) -> None:
    title = labels["alertname"]
    grafana.change(start, "Pending", "Alerting", labels, rule_id, title)
    grafana.change(start + minutes * MINUTE, "Alerting", "Normal", labels, rule_id, title)


def paged(tmp_path: Path, bridge: FakeBridge, **overrides: Any) -> tuple[Any, FakeGrafana]:
    grafana = FakeGrafana()
    values = {"oncall": ONCALL, **overrides}
    agent = a.Agent(config(tmp_path, **values), grafana, FakeClock(), acks=bridge)
    return agent, grafana


def by_alert(tmp_path: Path) -> dict[str, dict[str, Any]]:
    return {record["alert"]: record for record in journal(tmp_path)}


def test_each_paged_incident_takes_who_acknowledged_and_when_from_goalert(tmp_path: Path) -> None:
    bridge = FakeBridge()
    agent, grafana = paged(tmp_path, bridge)
    outage(grafana, utc("2026-01-01T08:10:00+00:00"), 12)
    firing(grafana, utc("2026-01-01T05:00:00+00:00"), 100, DISK, 8)
    firing(grafana, utc("2026-01-01T07:00:00+00:00"), 4, WORKER_DOWN, 9)
    firing(grafana, utc("2026-01-01T08:40:00+00:00"), 15, BACKUPS, 10)
    assert agent.tick() == 4
    found = by_alert(tmp_path)
    web = found["Website down"]
    assert web["acknowledged"] is True
    assert web["acknowledged_at"] == "2026-01-01T08:13:10Z"
    assert web["acknowledged_ts"] == utc("2026-01-01T08:13:10.482915+00:00")
    assert web["acknowledged_by"] == "Olena Kovalenko"
    assert web["acknowledged_via"] == "telegram"
    assert web["acknowledge_seconds"] == 190.483
    assert web["goalert_alert"] == 41
    disk = found["Disk full"]
    assert (disk["acknowledged_by"], disk["acknowledged_via"]) == ("Acme On-call", "web")
    assert disk["acknowledge_seconds"] == 1907.9
    assert disk["night"] is True
    hosts = found["Host down"]
    assert hosts["acknowledged"] is False
    assert hosts["goalert_alert"] == 43
    assert hosts["acknowledged_at"] is None
    assert hosts["acknowledged_by"] is None
    assert hosts["acknowledge_seconds"] is None
    backups = found["Backups failing"]
    assert backups["acknowledge_seconds"] == 60
    assert (backups["acknowledged_by"], backups["acknowledged_via"]) == (None, "api-key")
    assert bridge.calls == [(utc("2026-01-01T04:55:00+00:00"), utc("2026-01-01T08:56:00+00:00"))]
    assert sample(agent, ACK + "_count", criticality="critical", period="day") == 1
    assert sample(agent, ACK + "_sum", criticality="critical", period="day") == 190.483
    assert sample(agent, ACK + "_bucket", criticality="critical", period="day", le="120") == 0
    assert sample(agent, ACK + "_bucket", criticality="critical", period="day", le="300") == 1
    assert sample(agent, ACK + "_count", criticality="normal", period="night") == 1
    assert sample(agent, ACK + "_count", criticality="normal", period="day") == 1
    assert sample(agent, UNACKED, criticality="normal", period="night") == 1
    assert sample(agent, "ayeops_incidents_step_success", step="acks") == 1
    assert sample(agent, "ayeops_incidents_ack_reads_total", result="ok") == 1


def test_without_oncall_goalert_is_never_asked_and_the_record_stays_as_before(
    tmp_path: Path,
) -> None:
    bridge = FakeBridge()
    agent, grafana = paged(tmp_path, bridge, oncall="")
    outage(grafana, START - 50 * MINUTE, 12)
    assert agent.tick() == 1
    [record] = journal(tmp_path)
    assert bridge.calls == []
    for key in (
        "acknowledged",
        "acknowledged_at",
        "acknowledged_ts",
        "acknowledged_by",
        "acknowledged_via",
        "acknowledge_seconds",
        "goalert_alert",
    ):
        assert record[key] is None, key
    names = {name for name, _, _ in agent.samples()}
    assert not {UNACKED, ACK + "_count", "ayeops_incidents_ack_reads_total"} & names
    steps = {dict(pairs)["step"] for name, pairs, _ in agent.samples() if "step" in dict(pairs)}
    assert steps == {"read", "journal"}


def test_a_warning_never_reaches_goalert_so_it_is_not_asked_about(tmp_path: Path) -> None:
    bridge = FakeBridge()
    agent, grafana = paged(tmp_path, bridge)
    outage(grafana, START - 50 * MINUTE, 12, severity="warning")
    agent.tick()
    [record] = journal(tmp_path)
    assert bridge.calls == []
    assert record["acknowledged"] is None


def test_a_paged_incident_without_a_goalert_alert_is_unknown_not_unacknowledged(
    tmp_path: Path,
) -> None:
    bridge = FakeBridge([])
    agent, grafana = paged(tmp_path, bridge)
    outage(grafana, START - 50 * MINUTE, 12)
    agent.tick()
    [record] = journal(tmp_path)
    assert len(bridge.calls) == 1
    assert record["acknowledged"] is None
    assert record["goalert_alert"] is None
    assert UNACKED not in {name for name, _, _ in agent.samples()}


def test_a_failing_bridge_never_stops_the_journal(tmp_path: Path) -> None:
    bridge = FakeBridge()
    bridge.status = 503
    agent, grafana = paged(tmp_path, bridge)
    outage(grafana, START - 50 * MINUTE, 12)
    assert agent.tick() == 1
    [record] = journal(tmp_path)
    assert record["acknowledged"] is None
    assert sample(agent, "ayeops_incidents_step_success", step="acks") == 0
    assert sample(agent, "ayeops_incidents_step_success", step="journal") == 1
    assert sample(agent, "ayeops_incidents_ack_reads_total", result="error") == 1


def test_an_ack_seen_while_the_incident_is_open_survives_a_restart_and_is_not_asked_again(
    tmp_path: Path,
) -> None:
    bridge = FakeBridge()
    grafana = FakeGrafana()
    clock = FakeClock(utc("2026-01-01T08:15:00+00:00"))
    settings = config(tmp_path, oncall=ONCALL)
    agent = a.Agent(settings, grafana, clock, acks=bridge)
    start = utc("2026-01-01T08:10:00+00:00")
    grafana.change(start, "Pending", "Alerting")
    agent.tick()
    assert len(bridge.calls) == 1
    clock.advance(MINUTE)
    agent.tick()
    assert len(bridge.calls) == 1
    restarted = a.Agent(settings, grafana, clock, acks=bridge)
    bridge.status = 502
    grafana.change(start + 12 * MINUTE, "Alerting", "Normal")
    clock.now = start + 13 * MINUTE
    assert restarted.tick() == 1
    [record] = journal(tmp_path)
    assert record["acknowledged_by"] == "Olena Kovalenko"
    assert record["acknowledge_seconds"] == 190.483
    assert len(bridge.calls) == 1


def test_an_open_incident_nobody_acknowledged_is_asked_about_every_round(tmp_path: Path) -> None:
    bridge = FakeBridge([])
    grafana = FakeGrafana()
    clock = FakeClock()
    agent = a.Agent(config(tmp_path, oncall=ONCALL), grafana, clock, acks=bridge)
    grafana.change(START - 23 * 3600, "Pending", "Alerting")
    agent.tick()
    assert len(bridge.calls) == 1
    clock.advance(MINUTE)
    agent.tick()
    assert len(bridge.calls) == 2


def test_matching_needs_the_rule_the_host_the_service_and_a_creation_near_the_start() -> None:
    start = START
    incident = a.Incident("f", "Website down", 7, dict(WEB), start, start + 600)

    def alert(created: float, alert_id: int = 1, **labels: str) -> dict[str, Any]:
        merged = {"alertname": "Website down", "host": "central", "service": "web", **labels}
        return {"alert_id": alert_id, "created_at": created, "labels": merged}

    def match(*alerts: dict[str, Any]) -> Any:
        found = a.goalert_match(incident, list(alerts), start + 900)
        return None if found is None else found["alert_id"]

    assert match(alert(start + 30)) == 1
    assert match(alert(start + 30, alertname="Disk full")) is None
    assert match(alert(start + 30, host="edge")) is None
    assert match(alert(start + 30, service="docs")) is None
    assert match({"alert_id": 2, "created_at": start, "labels": {"alertname": "Website down"}}) == 2
    assert match(alert(start - 300)) == 1
    assert match(alert(start - 301)) is None
    assert match(alert(start + 660)) == 1
    assert match(alert(start + 661)) is None
    assert match(alert(start + 200, 1), alert(start - 100, 2), alert(start + 150, 3)) == 2


def test_an_alert_created_long_after_a_long_incident_began_is_not_its_page() -> None:
    incident = a.Incident("f", "Website down", 7, dict(WEB), START, None)
    late = {
        "alert_id": 1,
        "created_at": START + 6 * 3600 + 61,
        "labels": {"alertname": "Website down"},
    }
    assert a.goalert_match(incident, [late], START + 9 * 3600) is None
    late["created_at"] = START + 6 * 3600 + 60
    assert a.goalert_match(incident, [late], START + 9 * 3600) is late


def test_windows_cover_every_incident_and_stay_under_a_week() -> None:
    def incident(start: float, end: float | None) -> Any:
        return a.Incident("f", "Website down", 7, dict(WEB), start, end)

    near = [incident(START, START + 600), incident(START + 3600, START + 4200)]
    assert a.ack_windows(near, START + 5000) == [(START - 300, START + 4260)]
    far = [incident(START, START + 600), incident(START + 7 * 86400, START + 7 * 86400 + 60)]
    assert a.ack_windows(far, START + 8 * 86400) == [
        (START - 300, START + 660),
        (START + 7 * 86400 - 300, START + 7 * 86400 + 120),
    ]
    assert a.ack_windows([incident(START, None)], START + 86400) == [
        (START - 300, START + 6 * 3600 + 60)
    ]
    assert a.ack_windows([incident(START, None)], START + 60) == [(START - 300, START + 120)]


def test_the_bridge_answer_is_checked() -> None:
    with pytest.raises(a.ReadError, match="not JSON"):
        a.parse_acks(b"<html>")
    with pytest.raises(a.ReadError, match="no alerts"):
        a.parse_acks(b'{"items": []}')
    body = {
        "alerts": [
            {"alert_id": 1, "created_at": 10.5, "labels": {"alertname": "X"}},
            {"alert_id": "2", "created_at": 10, "labels": {}},
            {"alert_id": 3, "created_at": None, "labels": {}},
            {"alert_id": 4, "created_at": 10, "labels": "host=edge"},
            {"alert_id": 5, "created_at": True, "labels": {}},
            "not a map",
        ]
    }
    [only] = a.parse_acks(json.dumps(body).encode())
    assert only == {
        "alert_id": 1,
        "created_at": 10.5,
        "labels": {"alertname": "X"},
        "at": None,
        "by": None,
        "via": None,
    }


def test_time_to_acknowledge_is_split_by_criticality_and_day_or_night(tmp_path: Path) -> None:
    settings = config(tmp_path)
    records = [
        {"service": "web", "night": False, "acknowledge_seconds": 45.0, "acknowledged": True},
        {"service": "web", "night": False, "acknowledge_seconds": 400.0, "acknowledged": True},
        {"service": "web", "night": True, "acknowledge_seconds": 20.0, "acknowledged": True},
        {"service": "docs", "night": True, "acknowledged": False},
        {"service": "docs", "night": True, "acknowledged": False},
        {"service": "mcp", "night": False, "acknowledged": None},
    ]
    found = {
        (name, tuple(sorted(dict(pairs).items()))): value
        for name, pairs, value in a.ack_samples(records, settings)
    }

    def get(name: str, **labels: str) -> float:
        return float(found[(name, tuple(sorted(labels.items())))])

    assert get(ACK + "_bucket", criticality="critical", period="day", le="30") == 0
    assert get(ACK + "_bucket", criticality="critical", period="day", le="60") == 1
    assert get(ACK + "_bucket", criticality="critical", period="day", le="300") == 1
    assert get(ACK + "_bucket", criticality="critical", period="day", le="600") == 2
    assert get(ACK + "_bucket", criticality="critical", period="day", le="+Inf") == 2
    assert get(ACK + "_sum", criticality="critical", period="day") == 445
    assert get(ACK + "_count", criticality="critical", period="night") == 1
    assert get(UNACKED, criticality="low", period="night") == 2
    assert not any(dict(labels).get("criticality") == "high" for _, labels in found)
    text = a.render(a.ack_samples(records, settings))
    assert f"# TYPE {ACK} histogram" in text
    assert f"# TYPE {UNACKED} counter" in text


def test_the_bridge_is_asked_with_the_acks_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    opened = FakeOpen()
    monkeypatch.setattr(a.urllib.request, "urlopen", opened)
    assert a.secret_fetcher("r3ad")(f"{ONCALL}/acks?from=0&to=1", 5)[0] == 200
    assert opened.headers == ["Bearer r3ad"]


def test_the_bridge_address_comes_from_the_environment() -> None:
    env = {"AYE_GRAFANA_READER_TOKEN_FILE": "/run/grafana-reader/token"}
    assert a.config_from(env).oncall == ""
    assert a.config_from({**env, "AYE_INCIDENTS_ONCALL": ONCALL}).oncall == ONCALL


def test_an_acknowledgement_before_the_incident_began_counts_as_at_once(tmp_path: Path) -> None:
    incident = a.Incident("f", "Website down", 7, dict(WEB), START, START + 600)
    incident.ack = {"alert_id": 41, "at": START - 30, "by": "Olena", "via": "telegram"}
    fields = a.ack_fields(incident, config(tmp_path, oncall=ONCALL))
    assert fields["acknowledge_seconds"] == 0
    assert fields["acknowledged_ts"] == START - 30
    assert a.ack_fields(incident, config(tmp_path))["acknowledged"] is None


def test_utc_works_on_a_host_without_a_time_zone_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing(key: str) -> Any:
        raise a.ZoneInfoNotFoundError(f"No time zone found with key {key}")

    monkeypatch.setattr(a, "ZoneInfo", missing)
    zone = a.time_zone("UTC")
    assert datetime(2026, 11, 6, 12, tzinfo=zone).isoformat() == "2026-11-06T12:00:00+00:00"
    with pytest.raises(a.ZoneInfoNotFoundError):
        a.time_zone("Europe/Kyiv")
