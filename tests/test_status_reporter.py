from __future__ import annotations

import importlib.util
import json
import sys
import threading
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import jsonschema
import pytest

from ayeops.status import sign as manifest_sign
from tests.conftest import ROOT, WAIT

SCRIPT = ROOT / "src" / "ayeops" / "modules" / "status" / "files" / "status_reporter.py"
CONTRACT = ROOT / "cloud" / "status" / "contract"
PUSH_FIXTURE = CONTRACT / "fixtures" / "push.json"
GRAFANA = "http://grafana:3000"
RULES = f"{GRAFANA}/api/prometheus/grafana/api/v1/rules"
SILENCES = f"{GRAFANA}/api/alertmanager/grafana/api/v2/silences"
PUSH = "https://status.acme.example/api/push"
SECRET = "acme-push-secret"
START = 1_792_742_460.0
TOKEN_FILE = Path("token")
TOKEN = "glsa_reader_one"


def load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("status_reporter_script", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


r = load_script()


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def token_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    path = tmp_path / TOKEN_FILE
    path.write_text(f"{TOKEN}\n", encoding="utf-8")
    return path


def rule(
    service: str,
    severity: str = "critical",
    state: str = "inactive",
    health: str = "ok",
    instances: tuple[str, ...] = (),
) -> dict[str, Any]:
    return {
        "name": f"{service} {severity}",
        "type": "alerting",
        "state": state,
        "health": health,
        "labels": {"service": service, "severity": severity},
        "alerts": [
            {"labels": {"service": service, "alertname": f"{service} {severity}"}, "state": s}
            for s in instances
        ],
    }


def rules_answer(*rules: dict[str, Any]) -> bytes:
    groups = [{"name": "services", "file": "AyeOps", "rules": list(rules)}]
    return json.dumps({"status": "success", "data": {"groups": groups}}).encode()


def silence(*matchers: tuple[str, str, bool, bool], state: str = "active") -> dict[str, Any]:
    return {
        "id": "s1",
        "status": {"state": state},
        "matchers": [
            {"name": name, "value": value, "isRegex": regex, "isEqual": equal}
            for name, value, regex, equal in matchers
        ],
    }


MAINTENANCE = ("maintenance", ".*", True, True)


def on(service: str) -> tuple[str, str, bool, bool]:
    return ("service", service, False, True)


def healthy_rules() -> bytes:
    return rules_answer(rule("api"), rule("api-worker", "warning"), rule("checkout"))


class FakeClock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeHttp:
    def __init__(self, clock: FakeClock, cost: float = 0.0) -> None:
        self.clock = clock
        self.cost = cost
        self.answers: dict[str, list[Any]] = {
            RULES: [(200, healthy_rules())],
            SILENCES: [(200, b"[]")],
            PUSH: [(202, b'{"accepted": 2}')],
        }
        self.requests: list[urllib.request.Request] = []
        self.timeouts: list[float] = []

    def answer(self, url: str, *answers: Any) -> None:
        self.answers[url] = list(answers)

    def __call__(self, request: urllib.request.Request, timeout: float) -> tuple[int, bytes]:
        self.requests.append(request)
        self.timeouts.append(timeout)
        self.clock.advance(self.cost)
        queue = self.answers[request.full_url]
        answer = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(answer, BaseException):
            raise answer
        status, body = answer
        return int(status), bytes(body)

    def pushes(self) -> list[urllib.request.Request]:
        return [request for request in self.requests if request.full_url == PUSH]


def config(**overrides: Any) -> Any:
    values: dict[str, Any] = {
        "tenant": "acme",
        "url": PUSH,
        "secret": SECRET,
        "grafana": GRAFANA,
        "token_file": TOKEN_FILE,
        "components": (
            r.Component("api", ("api", "api-worker")),
            r.Component("payments-checkout", ("checkout",)),
        ),
        "interval_seconds": 60,
        "timeout_seconds": 10,
    }
    values.update(overrides)
    return r.Config(**values)


class Harness:
    def __init__(self, cost: float = 0.0, **overrides: Any) -> None:
        self.clock = FakeClock()
        self.http = FakeHttp(self.clock, cost)
        self.sleeps: list[float] = []
        self.reporter = r.Reporter(config(**overrides), self.http, self.clock, self.sleep)

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.clock.advance(seconds)


def body_of(request: urllib.request.Request) -> bytes:
    data = request.data
    assert isinstance(data, bytes)
    return data


def pushed(request: urllib.request.Request) -> dict[str, Any]:
    body: dict[str, Any] = json.loads(body_of(request))
    return body


def pushed_states(request: urllib.request.Request) -> dict[str, str]:
    return {c["id"]: c["state"] for c in pushed(request)["components"]}


def log_lines(capsys: pytest.CaptureFixture[str]) -> list[dict[str, Any]]:
    return [json.loads(line) for line in capsys.readouterr().out.splitlines() if line]


@pytest.mark.parametrize("case", load_json(PUSH_FIXTURE)["valid"], ids=lambda case: case["name"])
def test_a_push_built_by_the_reporter_is_byte_for_byte_the_shared_fixture(
    case: dict[str, Any],
) -> None:
    fixture = json.loads(case["body"])
    decided = [(c["id"], c["state"]) for c in fixture["components"]]
    body = r.push_body(fixture["tenant"], fixture["ts"], decided)
    assert body == case["body"].encode("utf-8")
    assert r.sign(body, load_json(PUSH_FIXTURE)["secret"]) == case["signature"]


@pytest.mark.parametrize("state", ["operational", "degraded", "outage", "maintenance", "no_data"])
def test_a_push_of_every_state_follows_the_push_schema(state: str) -> None:
    body = r.push_body("acme", int(START), [("api", state), ("payments-checkout", "outage")])
    jsonschema.validate(json.loads(body), load_json(CONTRACT / "push.schema.json"))


def test_the_reporter_signs_like_the_manifest_side_and_the_worker() -> None:
    body = r.push_body("acme", int(START), [("api", "degraded")])
    assert r.sign(body, SECRET) == manifest_sign(body, SECRET)
    assert r.SIGNATURE_HEADER == "X-AyeOps-Signature"


@pytest.mark.parametrize(
    ("severity", "expected"),
    [("critical", "outage"), ("page", "outage"), ("warning", "degraded"), ("info", "operational")],
)
def test_a_firing_alert_maps_by_severity(severity: str, expected: str) -> None:
    assert r.rule_state(rule("api", severity, "firing", instances=("Alerting",))) == expected


def test_pending_and_inactive_alerts_are_operational() -> None:
    assert r.rule_state(rule("api", state="pending", instances=("Pending",))) == "operational"
    assert r.rule_state(rule("api", state="inactive")) == "operational"
    assert r.rule_state(rule("api", instances=("Normal",))) == "operational"


def test_a_recovering_alert_still_counts_as_firing() -> None:
    assert r.rule_state(rule("api", state="recovering", instances=("Recovering",))) == "outage"


@pytest.mark.parametrize(
    "instance", ["Alerting (NoData)", "Alerting (Error)", "NoData", "Error", "Normal (Error)"]
)
def test_an_alert_without_data_or_with_a_query_error_is_no_data(instance: str) -> None:
    assert r.rule_state(rule("api", state="firing", instances=(instance,))) == "no_data"


def test_a_rule_whose_health_is_not_ok_and_lists_no_instances_is_no_data() -> None:
    assert r.rule_state(rule("api", health="error")) == "no_data"
    assert r.rule_state(rule("api", health="nodata")) == "no_data"


def test_a_firing_rule_without_listed_instances_maps_by_severity() -> None:
    assert r.rule_state(rule("api", "warning", state="firing")) == "degraded"


def test_the_worst_instance_of_a_rule_wins() -> None:
    instances = ("Normal", "Alerting (NoData)", "Alerting")
    assert r.rule_state(rule("api", state="firing", instances=instances)) == "outage"


def test_service_states_take_the_worst_rule_and_skip_rules_without_a_service() -> None:
    module_rule = {**rule("x", state="firing", instances=("Alerting",)), "labels": {"module": "m"}}
    recording = {**rule("api", state="firing"), "type": "recording"}
    answer = json.loads(
        rules_answer(
            rule("api"),
            rule("api", "warning", "firing", instances=("Alerting",)),
            module_rule,
            recording,
        )
    )
    assert r.service_states(answer) == {"api": "degraded"}


@pytest.mark.parametrize(
    "answer", [{"status": "error"}, [], {"status": "success", "data": {}}, None]
)
def test_an_unexpected_rules_answer_is_a_failed_query(answer: Any) -> None:
    with pytest.raises(r.QueryError):
        r.service_states(answer)


def test_an_active_silence_with_a_maintenance_matcher_covers_its_service() -> None:
    assert r.silence_covers(silence(on("api"), MAINTENANCE), "api")
    assert not r.silence_covers(silence(on("api"), MAINTENANCE), "api-worker")


def test_a_maintenance_silence_may_name_services_by_regex() -> None:
    covering = silence(("service", "api|api-worker", True, True), MAINTENANCE)
    assert r.silence_covers(covering, "api-worker")
    assert not r.silence_covers(covering, "checkout")
    assert not r.silence_covers(silence(("service", "(", True, True), MAINTENANCE), "api")


def test_silences_that_are_not_maintenance_of_a_named_service_do_not_count() -> None:
    assert not r.silence_covers(silence(on("api")), "api")
    assert not r.silence_covers(silence(on("api"), MAINTENANCE, state="expired"), "api")
    assert not r.silence_covers(silence(on("api"), MAINTENANCE, state="pending"), "api")
    assert not r.silence_covers(silence(MAINTENANCE), "api")
    assert not r.silence_covers(silence(("service", "web", False, False), MAINTENANCE), "api")


def test_a_negative_service_matcher_narrows_a_maintenance_silence() -> None:
    narrowed = silence(
        ("service", "api.*", True, True), ("service", "api", False, False), MAINTENANCE
    )
    assert r.silence_covers(narrowed, "api-worker")
    assert not r.silence_covers(narrowed, "api")


def test_maintained_lists_the_covered_services_and_refuses_a_non_list() -> None:
    answer = [silence(on("checkout"), MAINTENANCE), "junk"]
    assert r.maintained(answer, ["api", "checkout"]) == frozenset({"checkout"})
    with pytest.raises(r.QueryError):
        r.maintained({"silences": []}, ["api"])


def test_a_component_is_in_maintenance_only_when_all_its_services_are() -> None:
    api = r.Component("api", ("api", "api-worker"))
    states = {"api": "operational", "api-worker": "outage"}
    assert r.component_state(api, states, frozenset({"api", "api-worker"})) == "maintenance"
    assert r.component_state(api, states, frozenset({"api-worker"})) == "operational"
    assert r.component_state(api, states, frozenset({"api"})) == "outage"


def test_a_component_takes_the_worst_state_of_its_services() -> None:
    api = r.Component("api", ("api", "api-worker"))
    assert r.component_state(api, {"api": "degraded", "api-worker": "no_data"}, frozenset()) == (
        "degraded"
    )
    assert r.component_state(api, {"api": "operational"}, frozenset()) == "no_data"


def test_a_failed_query_decides_no_data_for_every_component() -> None:
    components = config().components
    assert r.decide(components, None, frozenset()) == [
        ("api", "no_data"),
        ("payments-checkout", "no_data"),
    ]
    assert r.decide(components, {"api": "operational"}, None)[0] == ("api", "no_data")


def test_a_round_reads_grafana_and_pushes_a_signed_body_with_explicit_timeouts() -> None:
    harness = Harness()
    harness.http.answer(
        RULES,
        (
            200,
            rules_answer(
                rule("api"),
                rule("api-worker", "warning", "firing", instances=("Alerting",)),
                rule("checkout", "critical", "firing", instances=("Alerting",)),
            ),
        ),
    )
    outcome = harness.reporter.tick()
    assert outcome == r.Outcome(True, "", 202, 1)
    get_rules, get_silences, post = harness.http.requests
    assert (get_rules.full_url, get_silences.full_url, post.full_url) == (RULES, SILENCES, PUSH)
    expected = f"Bearer {TOKEN}"
    assert get_rules.get_header("Authorization") == expected
    assert get_silences.get_header("Authorization") == expected
    assert post.get_method() == "POST"
    assert post.get_header("Content-type") == "application/json"
    assert post.get_header("Authorization") is None
    assert post.get_header("X-ayeops-signature") == manifest_sign(body_of(post), SECRET)
    assert pushed(post) == {
        "tenant": "acme",
        "ts": int(START),
        "components": [
            {"id": "api", "state": "degraded"},
            {"id": "payments-checkout", "state": "outage"},
        ],
    }
    assert harness.http.timeouts == [10, 10, 10]
    assert harness.sleeps == []


def test_service_names_never_leave_in_a_push() -> None:
    harness = Harness()
    harness.reporter.tick()
    body = body_of(harness.http.pushes()[0])
    for name in (b'"api-worker"', b'"checkout"', b"service"):
        assert name not in body


def test_a_maintenance_silence_pushes_maintenance() -> None:
    harness = Harness()
    harness.http.answer(
        SILENCES, (200, json.dumps([silence(on("checkout"), MAINTENANCE)]).encode())
    )
    harness.reporter.tick()
    assert pushed_states(harness.http.pushes()[0]) == {
        "api": "operational",
        "payments-checkout": "maintenance",
    }


@pytest.mark.parametrize(
    "failure",
    [
        (401, b'{"message": "Unauthorized"}'),
        (200, b"not json"),
        TimeoutError("timed out"),
        r.urllib.error.URLError("connection refused"),
    ],
)
def test_grafana_that_cannot_be_read_pushes_no_data_and_counts_the_failed_query(
    failure: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    harness = Harness()
    harness.http.answer(RULES, failure)
    assert harness.reporter.tick().ok
    assert pushed_states(harness.http.pushes()[0]) == {
        "api": "no_data",
        "payments-checkout": "no_data",
    }
    assert "ayeops_status_query_success 0" in harness.reporter.metrics()
    assert [line["event"] for line in log_lines(capsys)] == ["query_failed", "states"]


class Rotation:
    def __init__(self, token_file: Path, http: FakeHttp, rotate: bool = True) -> None:
        self.token_file = token_file
        self.http = http
        self.rotate = rotate
        self.headers: list[str | None] = []

    def __call__(self, request: urllib.request.Request, timeout: float) -> tuple[int, bytes]:
        self.headers.append(request.get_header("Authorization"))
        if request.full_url == RULES and len(self.headers) == 1:
            if self.rotate:
                self.token_file.write_text("glsa_reader_two\n", encoding="utf-8")
            return 401, b'{"message": "invalid API key"}'
        return self.http(request, timeout)


def test_a_401_reads_the_token_file_again_and_retries_with_the_new_token(
    token_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    harness = Harness()
    rotation = Rotation(token_file, harness.http)
    harness.reporter.http = rotation
    harness.reporter.tick()
    assert rotation.headers[:3] == [
        f"Bearer {TOKEN}",
        "Bearer glsa_reader_two",
        "Bearer glsa_reader_two",
    ]
    assert pushed_states(harness.http.pushes()[0]) == {
        "api": "operational",
        "payments-checkout": "operational",
    }
    assert "ayeops_status_query_success 1" in harness.reporter.metrics()
    assert "glsa_" not in capsys.readouterr().out


def test_a_401_with_the_same_token_in_the_file_is_not_retried(
    token_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    harness = Harness()
    rotation = Rotation(token_file, harness.http, rotate=False)
    harness.reporter.http = rotation
    harness.reporter.tick()
    assert rotation.headers == [f"Bearer {TOKEN}", None]
    assert set(pushed_states(harness.http.pushes()[0]).values()) == {"no_data"}
    out = capsys.readouterr().out
    assert "HTTP 401" in out
    assert "glsa_" not in out


def test_every_round_reads_the_token_file_again(token_file: Path) -> None:
    harness = Harness()
    harness.reporter.tick()
    token_file.write_text("glsa_reader_two", encoding="utf-8")
    harness.reporter.tick()
    rules = [q for q in harness.http.requests if q.full_url == RULES]
    assert [q.get_header("Authorization") for q in rules] == [
        f"Bearer {TOKEN}",
        "Bearer glsa_reader_two",
    ]


@pytest.mark.parametrize(
    "content",
    [None, b"", b"  \n", b"glsa_two words", "glsa_\u00e9".encode()],
    ids=["missing", "empty", "blank", "two-words", "not-ascii"],
)
def test_a_missing_or_broken_token_file_is_a_failed_query_that_never_shows_its_content(
    token_file: Path, content: bytes | None, capsys: pytest.CaptureFixture[str]
) -> None:
    if content is None:
        token_file.unlink()
    else:
        token_file.write_bytes(content)
    harness = Harness()
    harness.reporter.tick()
    assert [q.full_url for q in harness.http.requests] == [PUSH]
    assert set(pushed_states(harness.http.pushes()[0]).values()) == {"no_data"}
    failed = log_lines(capsys)[0]
    assert failed["event"] == "query_failed"
    assert "reader token" in failed["error"]
    assert "glsa_" not in failed["error"]


def test_failing_silences_also_push_no_data() -> None:
    harness = Harness()
    harness.http.answer(SILENCES, (500, b"oops"))
    harness.reporter.tick()
    assert set(pushed_states(harness.http.pushes()[0]).values()) == {"no_data"}


def test_a_transient_failure_is_retried_with_backoff() -> None:
    harness = Harness()
    harness.http.answer(PUSH, (503, b"busy"), TimeoutError("timed out"), (202, b"{}"))
    outcome = harness.reporter.tick()
    assert (outcome.ok, outcome.attempts) == (True, 3)
    assert harness.sleeps == [2.0, 4.0]
    bodies = [request.data for request in harness.http.pushes()]
    assert len(set(bodies)) == 1


def test_retries_are_bounded_and_end_in_a_transient_failure() -> None:
    harness = Harness()
    harness.http.answer(PUSH, r.urllib.error.URLError("connection refused"))
    outcome = harness.reporter.tick()
    assert (outcome.ok, outcome.reason, outcome.attempts) == (False, "transient", 3)
    assert outcome.detail == "URLError: connection refused"
    assert len(harness.http.pushes()) == r.MAX_ATTEMPTS
    assert harness.sleeps == [2.0, 4.0]


@pytest.mark.parametrize("status", [429, 500, 502, 504])
def test_rate_limits_and_server_errors_are_transient(status: int) -> None:
    assert r.transient(status)


@pytest.mark.parametrize("status", [400, 401, 404, 413, 422])
def test_a_refused_push_is_not_retried(status: int) -> None:
    harness = Harness()
    harness.http.answer(PUSH, (status, b'{"error": "signature does not verify"}'))
    outcome = harness.reporter.tick()
    assert (outcome.ok, outcome.reason, outcome.status, outcome.attempts) == (
        False,
        "refused",
        status,
        1,
    )
    assert "signature does not verify" in outcome.detail
    assert harness.sleeps == []


def test_no_retry_starts_that_could_not_finish_within_the_interval() -> None:
    harness = Harness(cost=15.0, timeout_seconds=20)
    harness.http.answer(PUSH, (503, b"busy"))
    outcome = harness.reporter.tick()
    assert (outcome.ok, outcome.attempts) == (False, 1)
    assert harness.sleeps == []


def test_metrics_show_the_last_successful_push_and_the_failures() -> None:
    harness = Harness()
    reporter = harness.reporter
    assert "ayeops_status_push_success" not in reporter.metrics()
    assert f"ayeops_status_last_push_timestamp_seconds {int(START)}" in reporter.metrics()
    harness.clock.advance(60)
    reporter.tick()
    text = reporter.metrics()
    assert "ayeops_status_push_success 1" in text
    assert f"ayeops_status_last_push_timestamp_seconds {int(START) + 60}" in text
    assert "ayeops_status_query_success 1" in text
    assert "ayeops_status_pushes_total 1" in text
    assert 'ayeops_status_component{component="api",state="operational"} 1' in text
    assert "# TYPE ayeops_status_push_failures_total counter" in text
    harness.http.answer(PUSH, (401, b"{}"))
    harness.clock.advance(60)
    reporter.tick()
    text = reporter.metrics()
    assert "ayeops_status_push_success 0" in text
    assert f"ayeops_status_last_push_timestamp_seconds {int(START) + 60}" in text
    assert 'ayeops_status_push_failures_total{reason="refused"} 1' in text
    assert 'ayeops_status_push_failures_total{reason="transient"} 0' in text


def test_failures_are_logged_every_round_and_recoveries_once(
    capsys: pytest.CaptureFixture[str],
) -> None:
    harness = Harness()
    harness.reporter.tick()
    harness.http.answer(PUSH, (422, b'{"error": "unknown components"}'))
    harness.reporter.tick()
    harness.reporter.tick()
    harness.http.answer(PUSH, (202, b"{}"))
    harness.reporter.tick()
    harness.reporter.tick()
    events = [line["event"] for line in log_lines(capsys)]
    assert events == ["states", "push_failed", "push_failed", "push_ok"]


def test_parse_components_reads_the_rendered_list() -> None:
    text = '[{"id": "api", "services": ["api", "api-worker"]}]'
    assert r.parse_components(text) == (r.Component("api", ("api", "api-worker")),)


@pytest.mark.parametrize(
    "text",
    [
        "[]",
        '{"id": "api"}',
        '[{"id": "API", "services": ["api"]}]',
        '[{"id": "api", "services": []}]',
        '[{"id": "api", "services": ["a"]}, {"id": "api", "services": ["b"]}]',
        '["api"]',
    ],
)
def test_parse_components_refuses_what_the_worker_would_refuse(text: str) -> None:
    with pytest.raises(ValueError):
        r.parse_components(text)


def environment(**overrides: str) -> dict[str, str]:
    values = {
        "AYE_STATUS_TENANT": "acme",
        "AYE_STATUS_URL": PUSH,
        "AYE_STATUS_PUSH_SECRET": SECRET,
        "AYE_STATUS_COMPONENTS": '[{"id": "api", "services": ["api"]}]',
        "AYE_GRAFANA_READER_TOKEN_FILE": "/run/grafana-reader/token",
        "AYE_STATUS_INTERVAL": "2m",
        "AYE_STATUS_TIMEOUT": "5s",
    }
    values.update(overrides)
    return values


def test_config_from_the_environment() -> None:
    loaded = r.config_from(environment())
    assert (loaded.tenant, loaded.url, loaded.grafana) == ("acme", PUSH, GRAFANA)
    assert (loaded.interval_seconds, loaded.timeout_seconds) == (120, 5)
    assert loaded.token_file == Path("/run/grafana-reader/token")


def test_config_needs_the_reader_token_file_and_no_grafana_password() -> None:
    env = environment()
    del env["AYE_GRAFANA_READER_TOKEN_FILE"]
    with pytest.raises(KeyError):
        r.config_from(env)
    text = SCRIPT.read_text(encoding="utf-8")
    assert "Basic" not in text
    assert "ADMIN" not in text


@pytest.mark.parametrize(
    "overrides",
    [
        {"AYE_STATUS_URL": "http://status.acme.example/api/push"},
        {"AYE_STATUS_TENANT": "Acme"},
        {"AYE_STATUS_PUSH_SECRET": ""},
        {"AYE_STATUS_INTERVAL": "0m"},
        {"AYE_STATUS_TIMEOUT": "10"},
    ],
)
def test_config_refuses_a_broken_environment(overrides: dict[str, str]) -> None:
    with pytest.raises(ValueError):
        r.config_from(environment(**overrides))


def test_one_round_survives_an_error_and_keeps_the_interval(
    capsys: pytest.CaptureFixture[str],
) -> None:
    harness = Harness()
    harness.http.answer(PUSH, RuntimeError("boom"))
    ticks = iter([0.0, 7.5])
    assert r.one_round(harness.reporter, lambda: next(ticks)) == 52.5
    assert log_lines(capsys)[-1] == {"event": "round_failed", "error": "RuntimeError: boom"}


@pytest.fixture
def endpoint() -> Iterator[tuple[str, Any, FakeClock]]:
    harness = Harness()
    httpd = r.make_server(harness.reporter, 0, "127.0.0.1", harness.clock)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", harness.reporter, harness.clock
    httpd.shutdown()
    httpd.server_close()


def get(url: str) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(url, timeout=WAIT) as response:
            return int(response.status), response.read().decode()
    except r.urllib.error.HTTPError as error:
        return int(error.code), ""


def test_endpoint_serves_metrics_and_health(endpoint: tuple[str, Any, FakeClock]) -> None:
    url, reporter, clock = endpoint
    reporter.tick()
    assert get(f"{url}/metrics") == (200, reporter.metrics())
    assert get(f"{url}/health") == (200, "ok\n")
    clock.advance(3600)
    assert get(f"{url}/health")[0] == 503
    assert get(f"{url}/other")[0] == 404
