from __future__ import annotations

import importlib.util
import json
import re
import sys
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from types import ModuleType
from typing import Any

import pytest
import yaml

from ayeops.registry import Module
from ayeops.render import _jinja
from tests.conftest import ROOT, rules_hit

MODULES = ROOT / "src" / "ayeops" / "modules"
FIXTURES = ROOT / "tests" / "fixtures" / "cloudflare"
TOKEN = "secret-token-value-never-shown"
NOW = datetime(2026, 1, 15, 12, 7, 42, tzinfo=UTC)
DATASETS = {
    "workersInvocationsAdaptive": "workers.json",
    "r2OperationsAdaptiveGroups": "r2_operations.json",
    "r2StorageAdaptiveGroups": "r2_storage.json",
    "d1AnalyticsAdaptiveGroups": "d1.json",
    "d1StorageAdaptiveGroups": "d1_storage.json",
    "httpRequests1dGroups": "zone_day.json",
}


def load_exporter() -> ModuleType:
    path = MODULES / "cloudflare" / "files" / "cloudflare_exporter.py"
    spec = importlib.util.spec_from_file_location("cloudflare_exporter", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


cf = load_exporter()


def fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def rows(name: str) -> list[dict[str, Any]]:
    viewer = fixture(name)["data"]["viewer"]
    scope = viewer.get("zones") or viewer.get("accounts")
    return list(next(iter(scope[0].values())))


def fixture_for(query: str) -> str:
    if "httpRequestsAdaptiveGroups" in query:
        if "clientCountryName" in query:
            return "zone_countries.json"
        if "originResponseDurationMs" in query:
            return "zone_origin.json"
        return "zone_window.json"
    for dataset, name in DATASETS.items():
        if dataset in query:
            return name
    raise AssertionError(query)


class FakeApi:
    def __init__(self, overrides: Mapping[str, Any] | None = None):
        self.overrides = dict(overrides or {})
        self.posts: list[dict[str, Any]] = []
        self.gets: list[str] = []
        self.headers: list[Mapping[str, str]] = []
        self.lookup: Any = fixture("zone_lookup.json")

    def post(self, url: str, body: bytes, headers: Mapping[str, str]) -> Any:
        assert url == cf.GRAPHQL
        self.headers.append(headers)
        payload = json.loads(body)
        self.posts.append(payload)
        name = fixture_for(payload["query"])
        answer = self.overrides.get(name)
        if isinstance(answer, Exception):
            raise answer
        return answer if answer is not None else fixture(name)

    def get(self, url: str, headers: Mapping[str, str]) -> Any:
        self.gets.append(url)
        self.headers.append(headers)
        if isinstance(self.lookup, Exception):
            raise self.lookup
        return self.lookup


def exporter(api: FakeApi, **config: Any) -> tuple[Any, list[Mapping[str, Any]]]:
    logs: list[Mapping[str, Any]] = []
    settings = {"zones": ["acme.example"], "accounts": ["0123456789abcdef0123456789abcdef"]}
    settings.update(config)
    client = cf.Client(TOKEN, post=api.post, get=api.get)
    return cf.Exporter(cf.Config(**settings), client, log=logs.append), logs


def series(text: str) -> dict[str, float]:
    values: dict[str, float] = {}
    for line in text.splitlines():
        if line and not line.startswith("#"):
            key, value = line.rsplit(" ", 1)
            values[key] = float(value)
    return values


def value(samples: list[Any], name: str, **wanted: str) -> float:
    found = [v for n, pairs, v in samples if n == name and dict(pairs) == wanted]
    assert len(found) == 1, (name, wanted, found)
    return float(found[0])


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (100, "1xx"),
        (200, "2xx"),
        (301, "3xx"),
        (404, "4xx"),
        (522, "5xx"),
        (599, "5xx"),
        (99, "other"),
        (600, "other"),
        (0, "other"),
        (None, "other"),
        ("abc", "other"),
        ("503", "5xx"),
    ],
)
def test_status_class_buckets_codes_and_refuses_garbage(status: Any, expected: str) -> None:
    assert cf.status_class(status) == expected


def test_r2_actions_are_split_into_the_billing_classes() -> None:
    assert cf.r2_class("PutObject") == "a"
    assert cf.r2_class("ListObjects") == "a"
    assert cf.r2_class("GetObject") == "b"
    assert cf.r2_class("HeadObject") == "b"
    assert cf.r2_class("DeleteObject") == "free"
    assert cf.r2_class("SomethingNew") == "unknown"
    assert not (cf.R2_CLASS_A & cf.R2_CLASS_B)
    assert not (cf.R2_CLASS_A | cf.R2_CLASS_B) & cf.R2_FREE


def test_window_keeps_hostname_status_class_and_cache_status() -> None:
    samples = cf.window_samples(rows("zone_window.json"), "acme.example", 20)
    assert (
        value(
            samples,
            "ayeops_cloudflare_requests",
            zone="acme.example",
            hostname="acme.example",
            status_class="2xx",
            cache_status="hit",
        )
        == 900
    )
    assert (
        value(
            samples,
            "ayeops_cloudflare_requests",
            zone="acme.example",
            hostname="acme.example",
            status_class="5xx",
            cache_status="none",
        )
        == 8
    )
    total = sum(v for n, _, v in samples if n == "ayeops_cloudflare_requests")
    assert total == 1404
    assert (
        value(
            samples,
            "ayeops_cloudflare_edge_bytes",
            zone="acme.example",
            hostname="acme.example",
            cache_status="hit",
        )
        == 9_000_000
    )


def test_only_520_to_526_count_as_origin_errors() -> None:
    samples = cf.window_samples(rows("zone_window.json"), "acme.example", 20)
    errors = {
        dict(pairs)["status"]: v
        for n, pairs, v in samples
        if n == "ayeops_cloudflare_origin_errors"
    }
    assert errors == {"522": 6.0, "526": 2.0}
    edge = [
        {"count": 1, "dimensions": {"clientRequestHTTPHost": "a", "edgeResponseStatus": s}}
        for s in (519, 520, 526, 527)
    ]
    found = {
        dict(pairs)["status"]
        for n, pairs, _ in cf.window_samples(edge, "z", 20)
        if n == "ayeops_cloudflare_origin_errors"
    }
    assert found == {"520", "526"}


def test_hostnames_past_the_cap_fold_into_other() -> None:
    samples = cf.window_samples(rows("zone_window.json"), "acme.example", 1)
    hosts = {dict(pairs)["hostname"] for n, pairs, _ in samples}
    assert hosts == {"acme.example", "other"}
    total = sum(v for n, _, v in samples if n == "ayeops_cloudflare_requests")
    assert total == 1404


def test_countries_keep_the_top_n_and_the_rest_as_other() -> None:
    samples = cf.country_samples(rows("zone_countries.json"), "acme.example", 1404, 2)
    countries = {dict(pairs)["country"]: v for _, pairs, v in samples}
    assert countries == {"UA": 800, "DE": 300, "other": 304}


def test_countries_add_no_other_when_the_top_covers_everything() -> None:
    samples = cf.country_samples(rows("zone_countries.json"), "acme.example", 1250, 3)
    assert "other" not in {dict(pairs)["country"] for _, pairs, _ in samples}


def test_origin_time_is_weighted_by_request_count() -> None:
    samples = cf.origin_samples(rows("zone_origin.json"), "acme.example", 1)
    assert value(
        samples,
        "ayeops_cloudflare_origin_response_seconds_avg",
        zone="acme.example",
        hostname="acme.example",
    ) == pytest.approx(0.12)
    assert value(
        samples,
        "ayeops_cloudflare_origin_response_seconds_avg",
        zone="acme.example",
        hostname="other",
    ) == pytest.approx(0.04)
    merged = cf.origin_samples(
        [
            {
                "count": 300,
                "avg": {"originResponseDurationMs": 120},
                "dimensions": {"clientRequestHTTPHost": "a"},
            },
            {
                "count": 100,
                "avg": {"originResponseDurationMs": 40},
                "dimensions": {"clientRequestHTTPHost": "b"},
            },
            {
                "count": 100,
                "avg": {"originResponseDurationMs": 80},
                "dimensions": {"clientRequestHTTPHost": "c"},
            },
        ],
        "z",
        1,
    )
    assert value(
        merged, "ayeops_cloudflare_origin_response_seconds_avg", zone="z", hostname="other"
    ) == pytest.approx(0.06)
    assert value(merged, "ayeops_cloudflare_origin_requests", zone="z", hostname="other") == 200


def test_day_totals_come_from_the_daily_dataset() -> None:
    samples = cf.day_samples(rows("zone_day.json"), "acme.example")
    assert value(samples, "ayeops_cloudflare_day_requests", zone="acme.example") == 25000
    assert value(samples, "ayeops_cloudflare_day_cached_bytes", zone="acme.example") == 600_000_000
    empty = cf.day_samples([], "acme.example")
    assert value(empty, "ayeops_cloudflare_day_requests", zone="acme.example") == 0


def test_workers_report_requests_errors_cpu_and_daily_usage() -> None:
    account = "0123456789abcdef0123456789abcdef"
    samples = cf.workers_samples(rows("workers.json"), account)
    assert (
        value(samples, "ayeops_cloudflare_usage", account=account, resource="workers_requests_day")
        == 85000
    )
    assert value(
        samples,
        "ayeops_cloudflare_workers_cpu_seconds",
        account=account,
        script="router",
        quantile="0.99",
    ) == pytest.approx(0.009)
    assert (
        value(samples, "ayeops_cloudflare_workers_errors_day", account=account, script="router")
        == 12
    )


def test_r2_operations_sum_class_a_and_b_and_skip_free_ones() -> None:
    samples = cf.r2_operation_samples(rows("r2_operations.json"), "acc")
    assert (
        value(samples, "ayeops_cloudflare_usage", account="acc", resource="r2_class_a_month")
        == 850010
    )
    assert (
        value(samples, "ayeops_cloudflare_usage", account="acc", resource="r2_class_b_month")
        == 2_000_000
    )
    assert (
        value(
            samples,
            "ayeops_cloudflare_r2_operations_month",
            account="acc",
            bucket="media",
            **{"class": "free"},
        )
        == 50000
    )


def test_r2_storage_takes_the_latest_point_of_each_bucket() -> None:
    samples = cf.r2_storage_samples(rows("r2_storage.json"), "acc")
    assert (
        value(samples, "ayeops_cloudflare_r2_storage_bytes", account="acc", bucket="media")
        == 4_001_000_000
    )
    assert value(samples, "ayeops_cloudflare_r2_objects", account="acc", bucket="media") == 52000
    assert (
        value(samples, "ayeops_cloudflare_usage", account="acc", resource="r2_storage_bytes")
        == 4_251_002_000
    )


def test_d1_without_databases_reports_zero_usage() -> None:
    assert (
        value(
            cf.d1_samples([], "acc"),
            "ayeops_cloudflare_usage",
            account="acc",
            resource="d1_rows_read_day",
        )
        == 0
    )
    storage = cf.d1_storage_samples(
        [
            {
                "max": {"databaseSizeBytes": 10},
                "dimensions": {"databaseId": "d", "date": "2026-01-14"},
            },
            {
                "max": {"databaseSizeBytes": 30},
                "dimensions": {"databaseId": "d", "date": "2026-01-15"},
            },
        ],
        "acc",
    )
    assert value(storage, "ayeops_cloudflare_d1_storage_bytes", account="acc", database="d") == 30


def test_d1_rows_and_queries_are_summed_per_database() -> None:
    samples = cf.d1_samples(
        [
            {
                "sum": {"readQueries": 5, "writeQueries": 2, "rowsRead": 400, "rowsWritten": 9},
                "dimensions": {"databaseId": "d"},
            }
        ],
        "acc",
    )
    assert value(samples, "ayeops_cloudflare_d1_queries_day", account="acc", database="d") == 7
    assert (
        value(samples, "ayeops_cloudflare_usage", account="acc", resource="d1_rows_written_day")
        == 9
    )


def test_limits_default_to_the_free_plan_and_accept_overrides() -> None:
    assert cf.parse_limits("") == cf.FREE_LIMITS
    assert cf.parse_limits("{}")["r2_class_a_month"] == 1_000_000
    limits = cf.parse_limits('{"workers_requests_day": 10000000}')
    assert limits["workers_requests_day"] == 10_000_000
    assert limits["r2_class_b_month"] == 10_000_000
    with pytest.raises(ValueError, match="unknown limit"):
        cf.parse_limits('{"workers_per_day": 1}')
    with pytest.raises(ValueError, match="JSON object"):
        cf.parse_limits("[1]")


def test_window_ends_on_a_whole_minute_before_the_lag() -> None:
    start, end = cf.window(NOW, 900, 180)
    assert cf.iso(end) == "2026-01-15T12:04:00Z"
    assert cf.iso(start) == "2026-01-15T11:49:00Z"
    assert cf.iso(cf.day_start(NOW)) == "2026-01-15T00:00:00Z"
    assert cf.iso(cf.month_start(NOW)) == "2026-01-01T00:00:00Z"


def test_render_escapes_labels_and_types_counters() -> None:
    text = cf.render(
        [
            ("ayeops_cloudflare_requests", cf.labels(hostname='a"b\\c\nd'), 3.0),
            ("ayeops_cloudflare_graphql_queries_total", (), 9.0),
            ("ayeops_cloudflare_origin_response_seconds_avg", cf.labels(hostname="x"), 0.125),
        ]
    )
    assert 'ayeops_cloudflare_requests{hostname="a\\"b\\\\c\\nd"} 3' in text
    assert "# TYPE ayeops_cloudflare_graphql_queries_total counter" in text
    assert "# TYPE ayeops_cloudflare_requests gauge" in text
    assert "ayeops_cloudflare_graphql_queries_total 9\n" in text
    assert 'ayeops_cloudflare_origin_response_seconds_avg{hostname="x"} 0.125' in text


def test_refresh_serves_every_dataset_and_counts_the_queries() -> None:
    api = FakeApi()
    ex, logs = exporter(api)
    ex.refresh(NOW)
    metrics = series(ex.metrics())
    account = "0123456789abcdef0123456789abcdef"
    assert len(api.posts) == ex.config.queries_per_poll() == 9
    assert metrics["ayeops_cloudflare_graphql_queries_total"] == 9
    assert metrics['ayeops_cloudflare_window_seconds{zone="acme.example"}'] == 900
    assert metrics['ayeops_cloudflare_day_requests{zone="acme.example"}'] == 25000
    assert (
        metrics[
            f'ayeops_cloudflare_zone_info{{account="{account}",plan="free",zone="acme.example",'
            'zone_id="00000000000000000000000000000001"}'
        ]
        == 1
    )
    assert (
        metrics[f'ayeops_cloudflare_free_limit{{account="{account}",resource="r2_class_a_month"}}']
        == 1_000_000
    )
    assert all(
        v == 1 for k, v in metrics.items() if k.startswith("ayeops_cloudflare_query_success")
    )
    variables = api.posts[0]["variables"]
    assert variables == {
        "zone": "00000000000000000000000000000001",
        "start": "2026-01-15T11:49:00Z",
        "end": "2026-01-15T12:04:00Z",
    }
    assert [record["event"] for record in logs] == ["poll"]
    assert logs[0]["queries"] == 9


def test_zone_traffic_counts_only_visitor_requests_not_cloudflare_internal_fetches() -> None:
    api = FakeApi()
    ex, _ = exporter(api)
    ex.refresh(NOW)
    adaptive = [
        re.sub(r"\s+", " ", post["query"])
        for post in api.posts
        if "httpRequestsAdaptiveGroups" in post["query"]
    ]
    assert len(adaptive) == 3
    for query in adaptive:
        assert re.search(r'filter: \{[^}]*requestSource: "eyeball"', query), query


def test_free_limit_ratio_crosses_eighty_percent_for_r2_class_a() -> None:
    ex, _ = exporter(FakeApi())
    ex.refresh(NOW)
    metrics = series(ex.metrics())
    account = "0123456789abcdef0123456789abcdef"
    used = metrics[f'ayeops_cloudflare_usage{{account="{account}",resource="r2_class_a_month"}}']
    limit = metrics[
        f'ayeops_cloudflare_free_limit{{account="{account}",resource="r2_class_a_month"}}'
    ]
    assert used / limit > 0.8
    workers = metrics[
        f'ayeops_cloudflare_usage{{account="{account}",resource="workers_requests_day"}}'
    ]
    assert workers / cf.FREE_LIMITS["workers_requests_day"] > 0.8


def test_the_zone_is_looked_up_once_and_retried_until_it_resolves() -> None:
    api = FakeApi()
    api.lookup = {"success": True, "result": []}
    ex, logs = exporter(api, accounts=[])
    ex.refresh(NOW)
    metrics = series(ex.metrics())
    assert metrics['ayeops_cloudflare_query_success{dataset="zone",target="acme.example"}'] == 0
    assert api.posts == []
    assert logs[0]["event"] == "zone_lookup_failed"
    api.lookup = fixture("zone_lookup.json")
    ex.refresh(NOW)
    ex.refresh(NOW)
    assert len(api.gets) == 2
    assert len(api.posts) == 8


def test_zone_lookup_that_is_not_json_object_fails_cleanly() -> None:
    api = FakeApi()
    api.lookup = ["unexpected"]
    ex, _ = exporter(api, accounts=[])
    ex.refresh(NOW)
    assert (
        series(ex.metrics())[
            'ayeops_cloudflare_query_success{dataset="zone",target="acme.example"}'
        ]
        == 0
    )


def test_a_denied_required_dataset_fails_the_query_and_marks_it_unavailable() -> None:
    api = FakeApi({"zone_day.json": fixture("denied.json")})
    ex, logs = exporter(api, accounts=[])
    ex.refresh(NOW)
    metrics = series(ex.metrics())
    assert metrics['ayeops_cloudflare_query_success{dataset="day",target="acme.example"}'] == 0
    assert metrics['ayeops_cloudflare_dataset_available{dataset="day",target="acme.example"}'] == 0
    assert metrics['ayeops_cloudflare_query_errors_total{dataset="day"}'] == 1
    failed = [r for r in logs if r["event"] == "query_failed"]
    assert failed[0]["denied"] is True
    assert "does not have permission" in failed[0]["error"]


def test_an_optional_dataset_the_plan_lacks_raises_no_failure() -> None:
    api = FakeApi({"zone_origin.json": fixture("denied.json")})
    ex, _ = exporter(api, accounts=[])
    ex.refresh(NOW)
    metrics = series(ex.metrics())
    assert (
        metrics['ayeops_cloudflare_dataset_available{dataset="origin",target="acme.example"}'] == 0
    )
    assert 'ayeops_cloudflare_query_success{dataset="origin",target="acme.example"}' not in metrics
    assert not any(k.startswith("ayeops_cloudflare_origin_response") for k in metrics)


def test_a_transient_failure_is_not_reported_as_a_missing_dataset() -> None:
    api = FakeApi({"workers.json": OSError("connection reset")})
    ex, _ = exporter(api)
    ex.refresh(NOW)
    metrics = series(ex.metrics())
    target = 'dataset="workers",target="0123456789abcdef0123456789abcdef"'
    assert metrics[f"ayeops_cloudflare_query_success{{{target}}}"] == 0
    assert f"ayeops_cloudflare_dataset_available{{{target}}}" not in metrics


def test_a_failed_window_skips_the_countries_query() -> None:
    api = FakeApi({"zone_window.json": OSError("timeout")})
    ex, _ = exporter(api, accounts=[])
    ex.refresh(NOW)
    queries = [p["query"] for p in api.posts]
    assert not any("clientCountryName" in q for q in queries)
    metrics = series(ex.metrics())
    assert 'ayeops_cloudflare_window_seconds{zone="acme.example"}' not in metrics
    assert metrics['ayeops_cloudflare_query_success{dataset="requests",target="acme.example"}'] == 0


def test_a_response_without_the_scope_is_an_error_not_an_empty_zone() -> None:
    api = FakeApi({"zone_day.json": {"data": {"viewer": {"zones": []}}, "errors": None}})
    ex, _ = exporter(api, accounts=[])
    ex.refresh(NOW)
    assert (
        series(ex.metrics())['ayeops_cloudflare_query_success{dataset="day",target="acme.example"}']
        == 0
    )


def test_metrics_of_the_last_poll_replace_the_previous_ones() -> None:
    api = FakeApi()
    ex, _ = exporter(api, accounts=[])
    ex.refresh(NOW)
    api.overrides["zone_window.json"] = OSError("down")
    ex.refresh(NOW)
    metrics = series(ex.metrics())
    assert not any(k.startswith("ayeops_cloudflare_requests{") for k in metrics)


def test_the_token_is_sent_only_as_a_header_and_never_logged() -> None:
    api = FakeApi({"workers.json": fixture("denied.json"), "zone_day.json": OSError("x")})
    api.lookup = fixture("zone_lookup.json")
    ex, logs = exporter(api)
    ex.refresh(NOW)
    assert all(h["Authorization"] == f"Bearer {TOKEN}" for h in api.headers)
    assert TOKEN not in json.dumps(logs)
    assert TOKEN not in ex.metrics()
    assert all(TOKEN not in json.dumps(p) for p in api.posts)


def test_graphql_errors_are_read_from_the_body() -> None:
    error = cf.graphql_error(fixture("denied.json"))
    assert error is not None and error.denied and error.code == "authz"
    assert cf.graphql_error({"data": {}, "errors": None}) is None
    assert cf.graphql_error({"data": {}, "errors": []}) is None
    assert cf.graphql_error("oops") is not None
    assert cf.CloudflareError("HTTP 403", status=403).denied
    assert not cf.CloudflareError("HTTP 429", status=429).denied


def test_config_reads_the_environment() -> None:
    config = cf.config_from(
        {
            "AYE_CLOUDFLARE_ZONES": "a.example, b.example,",
            "AYE_CLOUDFLARE_ACCOUNTS": "",
            "AYE_CLOUDFLARE_TOP_COUNTRIES": "5",
        }
    )
    assert config.zones == ["a.example", "b.example"]
    assert config.accounts == []
    assert config.top_countries == 5
    assert config.window_seconds == 900
    assert config.queries_per_poll() == 8
    assert cf.budget_per_5m(9, 300) == 9
    assert cf.budget_per_5m(9, 60) == 45


def test_module_needs_a_zone_or_an_account(data: dict[str, Any]) -> None:
    central = data["hosts"]["central"]["modules"]
    central["cloudflare"] = {}
    assert "module-settings" in rules_hit(data)
    central["cloudflare"] = {"zones": []}
    assert "module-settings" in rules_hit(data)
    central["cloudflare"] = {"accounts": ["0123456789abcdef0123456789abcdef"]}
    assert "module-settings" not in rules_hit(data)
    central["cloudflare"] = {"zones": ["acme.example"], "limits": {"nope": 1}}
    assert "module-settings" in rules_hit(data)


def test_alert_expressions_name_only_exported_metrics(modules: dict[str, Module]) -> None:
    module = modules["cloudflare"]
    source = (module.path / "files" / "cloudflare_exporter.py").read_text(encoding="utf-8")
    dashboard = json.loads(
        (module.path / "dashboards" / "cloudflare.json").read_text(encoding="utf-8")
    )
    exprs = [alert["expr"] for alert in module.alerts]
    exprs += [t["expr"] for p in dashboard["panels"] for t in p["targets"]]
    exprs += [dashboard["templating"]["list"][0]["query"]["query"]]
    names = {name for expr in exprs for name in re.findall(r"ayeops_cloudflare_[a-z0-9_]+", expr)}
    assert names
    assert sorted(n for n in names if f'"{n}"' not in source) == []
    catalog = module.catalog["cloudflare-exporter"]["metrics"]
    assert all(f'"{n}"' in source for n in catalog)


def test_module_renders_its_settings_into_the_exporter_environment(
    modules: dict[str, Module],
) -> None:
    module = modules["cloudflare"]
    settings = module.settings({"zones": ["a.example"], "limits": {"workers_requests_day": 5}})
    text = _jinja().from_string(module.template("compose.yaml.j2")).render(settings=settings)
    env = yaml.safe_load(text)["services"]["cloudflare-exporter"]["environment"]
    assert env["AYE_CLOUDFLARE_ZONES"] == "a.example"
    assert json.loads(env["AYE_CLOUDFLARE_LIMITS"]) == {"workers_requests_day": 5}
    assert cf.parse_limits(env["AYE_CLOUDFLARE_LIMITS"])["workers_requests_day"] == 5


def test_no_exported_series_uses_the_host_label_that_vmagent_owns() -> None:
    ex, _ = exporter(FakeApi())
    ex.refresh(NOW)
    keys = {
        part.split("=", 1)[0]
        for line in ex.metrics().splitlines()
        if "{" in line and not line.startswith("#")
        for part in line[line.index("{") + 1 : line.rindex("}")].split(",")
    }
    assert "host" not in keys
    assert "hostname" in keys


def test_ratio_alerts_still_fire_when_the_numerator_series_is_absent(
    modules: dict[str, Module],
) -> None:
    alerts = {alert["name"]: alert["expr"] for alert in modules["cloudflare"].alerts}
    for name in ("cloudflare-edge-5xx", "cloudflare-cache-ratio-drop"):
        assert "or sum by (zone) (ayeops_cloudflare_requests) * 0" in alerts[name]


def test_values_render_as_integers_only_when_they_are_whole_and_small() -> None:
    assert cf.format_value(3.0) == "3"
    assert cf.format_value(0.5) == "0.5"
    assert cf.format_value(1e15) == "1000000000000000.0"
    assert cf.format_value(999_999_999_999_999.0) == "999999999999999"
    assert cf.format_value(-2.0) == "-2"
    assert cf.number(None) == 0.0
    assert cf.number("7") == 7.0


def test_time_boundaries_drop_seconds_and_microseconds() -> None:
    moment = NOW.replace(microsecond=999_999)
    _, end = cf.window(moment, 900, 0)
    assert end == datetime(2026, 1, 15, 12, 7, tzinfo=UTC)
    assert cf.day_start(moment) == datetime(2026, 1, 15, tzinfo=UTC)
    assert cf.month_start(moment) == datetime(2026, 1, 1, tzinfo=UTC)


def test_top_names_break_ties_by_name() -> None:
    assert cf.top_names({"b": 5, "a": 5, "c": 9}, 2) == {"c", "a"}
    assert cf.top_names({"b": 1, "a": 1}, 1) == {"a"}


def test_missing_scopes_and_datasets_raise_with_a_reason() -> None:
    with pytest.raises(cf.CloudflareError, match=r"^response has no data$"):
        cf.nodes(None, "zones", "x")
    with pytest.raises(cf.CloudflareError, match=r"^no zones in the response, check the token"):
        cf.nodes({"viewer": {"zones": []}}, "zones", "x")
    with pytest.raises(cf.CloudflareError, match=r"^no x in the response$"):
        cf.nodes({"viewer": {"zones": [{}]}}, "zones", "x")


def test_missing_dimensions_get_stable_placeholders() -> None:
    window = cf.window_samples([{"count": 2, "dimensions": {}}], "z", 5)
    assert (
        value(
            window,
            "ayeops_cloudflare_requests",
            zone="z",
            hostname="",
            status_class="other",
            cache_status="none",
        )
        == 2
    )
    countries = cf.country_samples([{"count": 3, "dimensions": {}}], "z", 4, 5)
    assert {dict(p)["country"]: v for _, p, v in countries} == {"XX": 3, "other": 1}
    workers = cf.workers_samples([{"sum": {"requests": 1}, "dimensions": {}}], "acc")
    assert value(workers, "ayeops_cloudflare_workers_requests_day", account="acc", script="") == 1
    r2 = cf.r2_operation_samples([{"sum": {"requests": 4}, "dimensions": {}}], "acc")
    kind = {"class": "unknown"}
    assert value(r2, "ayeops_cloudflare_r2_operations_month", account="acc", bucket="", **kind) == 4
    d1 = cf.d1_samples([{"sum": {}, "dimensions": {}}], "acc")
    assert value(d1, "ayeops_cloudflare_d1_rows_read_day", account="acc", database="") == 0


def test_hosts_are_ranked_by_their_total_across_statuses() -> None:
    rows_in = [
        {"count": 3, "dimensions": {"clientRequestHTTPHost": "a", "edgeResponseStatus": 200}},
        {"count": 3, "dimensions": {"clientRequestHTTPHost": "a", "edgeResponseStatus": 404}},
        {"count": 5, "dimensions": {"clientRequestHTTPHost": "b", "edgeResponseStatus": 200}},
    ]
    samples = cf.window_samples(rows_in, "z", 1)
    assert {dict(p)["hostname"] for _, p, _ in samples} == {"a", "other"}
    assert {dict(p)["cache_status"] for _, p, _ in samples} == {"none"}


def test_a_remainder_of_one_request_still_shows_as_other() -> None:
    samples = cf.country_samples(rows("zone_countries.json"), "z", 1251, 3)
    assert {dict(p)["country"]: v for _, p, v in samples}["other"] == 1


def origin_row(host: str, count: int, millis: float | None) -> dict[str, Any]:
    average = {} if millis is None else {"originResponseDurationMs": millis}
    return {"count": count, "avg": average, "dimensions": {"clientRequestHTTPHost": host}}


def test_origin_time_needs_at_least_one_request() -> None:
    samples = cf.origin_samples([origin_row("a", 1, 50), origin_row("b", 0, None)], "z", 5)
    times = {dict(p)["hostname"]: v for n, p, v in samples if n.endswith("seconds_avg")}
    assert times == {"a": 0.05}
    ranked = cf.origin_samples([origin_row("b", 9, 10), origin_row("a", 1, 10)], "z", 1)
    assert {dict(p)["hostname"] for _, p, _ in ranked} == {"b", "other"}


def test_several_rows_add_up_instead_of_replacing_each_other() -> None:
    day = cf.day_samples([{"sum": {"requests": 2}}, {"sum": {"requests": 3}}], "z")
    assert value(day, "ayeops_cloudflare_day_requests", zone="z") == 5
    d1 = cf.d1_samples(
        [
            {"sum": {"rowsRead": 2, "rowsWritten": 1}, "dimensions": {"databaseId": "a"}},
            {"sum": {"rowsRead": 3, "rowsWritten": 4}, "dimensions": {"databaseId": "b"}},
        ],
        "acc",
    )
    assert value(d1, "ayeops_cloudflare_usage", account="acc", resource="d1_rows_read_day") == 5
    assert value(d1, "ayeops_cloudflare_usage", account="acc", resource="d1_rows_written_day") == 5
    assert value(d1, "ayeops_cloudflare_d1_rows_read_day", account="acc", database="b") == 3
    assert value(d1, "ayeops_cloudflare_d1_rows_written_day", account="acc", database="b") == 4
    storage = cf.d1_storage_samples(
        [
            {"max": {"databaseSizeBytes": 10}, "dimensions": {"databaseId": "a", "date": "d"}},
            {"max": {"databaseSizeBytes": 30}, "dimensions": {"databaseId": "b", "date": "d"}},
        ],
        "acc",
    )
    total = value(storage, "ayeops_cloudflare_usage", account="acc", resource="d1_storage_bytes")
    assert total == 40


def test_workers_cpu_is_converted_from_microseconds_exactly() -> None:
    samples = cf.workers_samples(rows("workers.json"), "acc")
    name = "ayeops_cloudflare_workers_cpu_seconds"
    assert value(samples, name, account="acc", script="router", quantile="0.5") == 0.0015
    assert value(samples, name, account="acc", script="router", quantile="0.99") == 0.009
    requests = "ayeops_cloudflare_workers_requests_day"
    assert value(samples, requests, account="acc", script="og-image") == 15000


def test_latest_point_wins_in_any_order() -> None:
    ascending = list(reversed(rows("r2_storage.json")))
    samples = cf.r2_storage_samples(ascending, "acc")
    assert value(samples, "ayeops_cloudflare_r2_objects", account="acc", bucket="media") == 52000


def test_limit_errors_say_exactly_what_is_wrong() -> None:
    with pytest.raises(ValueError, match=r"^unknown limit workers_per_day$"):
        cf.parse_limits('{"workers_per_day": 1}')
    with pytest.raises(ValueError, match=r"^AYE_CLOUDFLARE_LIMITS must be a JSON object$"):
        cf.parse_limits('"x"')


def test_config_defaults_and_environment_names() -> None:
    config = cf.Config(zones=[], accounts=[])
    defaults = (config.window_seconds, config.lag_seconds, config.top_hosts, config.top_countries)
    assert defaults == (900, 180, 20, 10)
    full = cf.config_from(
        {
            "AYE_CLOUDFLARE_ZONES": "a.example",
            "AYE_CLOUDFLARE_ACCOUNTS": "x,y",
            "AYE_CLOUDFLARE_WINDOW_SECONDS": "600",
            "AYE_CLOUDFLARE_LAG_SECONDS": "60",
            "AYE_CLOUDFLARE_TOP_HOSTS": "3",
            "AYE_CLOUDFLARE_TOP_COUNTRIES": "4",
            "AYE_CLOUDFLARE_LIMITS": '{"d1_storage_bytes": 7}',
        }
    )
    assert full.accounts == ["x", "y"]
    assert (full.window_seconds, full.lag_seconds, full.top_hosts, full.top_countries) == (
        600,
        60,
        3,
        4,
    )
    assert full.limits["d1_storage_bytes"] == 7
    empty = cf.config_from({})
    assert (empty.zones, empty.accounts, empty.lag_seconds, empty.top_hosts) == ([], [], 180, 20)
    assert empty.limits == cf.FREE_LIMITS


def test_graphql_error_defaults_and_truncation() -> None:
    error = cf.graphql_error({"errors": [{"message": "m" * 400}]})
    assert error is not None and str(error) == "m" * 300 and error.code == ""
    bare = cf.graphql_error({"errors": ["not an object"]})
    assert bare is not None and str(bare) == "GraphQL error"
    assert str(cf.graphql_error([])) == "response is not a JSON object"
    plain = cf.CloudflareError("x")
    assert (plain.code, plain.status, plain.denied) == ("", 0, False)
    assert cf.CloudflareError("x", status=401).denied
    assert not cf.CloudflareError("x", status=500).denied


def test_client_sends_json_with_its_own_user_agent_and_quotes_the_zone() -> None:
    api = FakeApi()
    client = cf.Client(TOKEN, post=api.post, get=api.get)
    assert client.sent == 0
    client.query(cf.ZONE_DAY, {"zone": "z", "date": "2026-01-15"})
    assert client.sent == 1
    assert api.headers[0]["Content-Type"] == "application/json"
    assert api.headers[0]["User-Agent"] == "ayeops-cloudflare-exporter"
    zone = client.zone("a b.example")
    assert api.gets == [f"{cf.API}/zones?name=a%20b.example"]
    assert (zone.account, zone.plan) == ("0123456789abcdef0123456789abcdef", "free")
    api.lookup = {"success": True, "result": [{"id": "i"}]}
    assert client.zone("x.example") == cf.Zone("x.example", "i", "", "")
    api.lookup = {"success": False, "result": [{"id": "i"}]}
    with pytest.raises(cf.CloudflareError, match=r"^zone x.example is not visible to the token$"):
        client.zone("x.example")
    api.lookup = "nope"
    with pytest.raises(cf.CloudflareError, match=r"^zone lookup answered with no JSON object$"):
        client.zone("x.example")


def test_account_queries_cover_the_day_the_month_and_the_last_day() -> None:
    api = FakeApi()
    ex, _ = exporter(api, zones=[])
    ex.refresh(NOW)
    sent = {fixture_for(p["query"]): p["variables"] for p in api.posts}
    account = "0123456789abcdef0123456789abcdef"
    end = "2026-01-15T12:04:00Z"
    assert sent["workers.json"] == {"account": account, "start": "2026-01-15T00:00:00Z", "end": end}
    month = {"account": account, "start": "2026-01-01T00:00:00Z", "end": end}
    assert sent["r2_operations.json"] == month
    last_day = {"account": account, "start": "2026-01-14T12:04:00Z", "end": end}
    assert sent["r2_storage.json"] == last_day
    assert sent["d1.json"] == {"account": account, "date": "2026-01-15"}
    since = {"account": account, "since": "2026-01-14", "date": "2026-01-15"}
    assert sent["d1_storage.json"] == since
    metrics = series(ex.metrics())
    for dataset in ("workers", "r2_operations", "r2_storage", "d1", "d1_storage"):
        key = f'dataset="{dataset}",target="{account}"'
        assert metrics[f"ayeops_cloudflare_dataset_available{{{key}}}"] == 1
        assert metrics[f"ayeops_cloudflare_query_success{{{key}}}"] == 1


def test_zone_queries_ask_for_the_window_and_the_utc_day() -> None:
    api = FakeApi()
    ex, _ = exporter(api, accounts=[])
    ex.refresh(NOW)
    sent = {fixture_for(p["query"]): p["variables"] for p in api.posts}
    day = {"zone": "00000000000000000000000000000001", "date": "2026-01-15"}
    assert sent["zone_day.json"] == day
    metrics = series(ex.metrics())
    end = datetime(2026, 1, 15, 12, 4, tzinfo=UTC).timestamp()
    assert metrics['ayeops_cloudflare_window_end_timestamp_seconds{zone="acme.example"}'] == end
    assert metrics["ayeops_cloudflare_poll_timestamp_seconds"] == NOW.timestamp()
    for dataset in ("requests", "countries", "day"):
        key = f'dataset="{dataset}",target="acme.example"'
        assert metrics[f"ayeops_cloudflare_query_success{{{key}}}"] == 1
        assert metrics[f"ayeops_cloudflare_dataset_available{{{key}}}"] == 1
    origin = 'dataset="origin",target="acme.example"'
    assert metrics[f"ayeops_cloudflare_dataset_available{{{origin}}}"] == 1
    assert f"ayeops_cloudflare_query_success{{{origin}}}" not in metrics
    assert metrics['ayeops_cloudflare_query_success{dataset="zone",target="acme.example"}'] == 1


def test_one_failing_zone_does_not_stop_the_next_one() -> None:
    api = FakeApi()
    lookups = iter([{"success": False}, fixture("zone_lookup.json")])

    def lookup(url: str, headers: Mapping[str, str]) -> Any:
        return next(lookups)

    client = cf.Client(TOKEN, post=api.post, get=lookup)
    logs: list[Mapping[str, Any]] = []
    config = cf.Config(zones=["bad.example", "acme.example"], accounts=[])
    ex = cf.Exporter(config, client, log=logs.append)
    ex.refresh(NOW)
    metrics = series(ex.metrics())
    assert metrics['ayeops_cloudflare_query_success{dataset="zone",target="bad.example"}'] == 0
    assert metrics['ayeops_cloudflare_query_success{dataset="zone",target="acme.example"}'] == 1
    assert logs[0] == {
        "event": "zone_lookup_failed",
        "zone": "bad.example",
        "error": "zone bad.example is not visible to the token",
    }


def test_poll_logs_and_counters_accumulate_across_polls() -> None:
    api = FakeApi({"zone_day.json": OSError("boom")})
    ex, logs = exporter(api)
    ex.refresh(NOW)
    ex.refresh(NOW)
    failed = [r for r in logs if r["event"] == "query_failed"]
    assert failed[0] == {
        "event": "query_failed",
        "target": "acme.example",
        "dataset": "day",
        "denied": False,
        "error": "boom",
    }
    polls = [r for r in logs if r["event"] == "poll"]
    assert [(p["queries"], p["failed"]) for p in polls] == [(9, 1), (9, 1)]
    assert isinstance(polls[0]["seconds"], float)
    metrics = series(ex.metrics())
    assert metrics["ayeops_cloudflare_graphql_queries_total"] == 18
    assert metrics['ayeops_cloudflare_query_errors_total{dataset="day"}'] == 2


def test_metrics_before_the_first_poll_hold_only_the_counters() -> None:
    ex, _ = exporter(FakeApi())
    assert series(ex.metrics()) == {"ayeops_cloudflare_graphql_queries_total": 0}


def test_queries_are_graphql_documents_naming_the_fields_the_parsers_read() -> None:
    expected = {
        cf.ZONE_WINDOW: ["clientRequestHTTPHost", "edgeResponseStatus", "cacheStatus"],
        cf.ZONE_COUNTRIES: ["clientCountryName", "count_DESC"],
        cf.ZONE_ORIGIN: ["originResponseDurationMs", "clientRequestHTTPHost"],
        cf.ZONE_DAY: ["httpRequests1dGroups", "cachedRequests", "cachedBytes", "threats"],
        cf.WORKERS: ["scriptName", "cpuTimeP50", "cpuTimeP99", "errors"],
        cf.R2_OPERATIONS: ["actionType", "bucketName"],
        cf.R2_STORAGE: ["payloadSize", "metadataSize", "objectCount", "datetime_DESC"],
        cf.D1: ["rowsRead", "rowsWritten", "readQueries", "writeQueries"],
        cf.D1_STORAGE: ["databaseSizeBytes", "date_DESC"],
    }
    for query, fields in expected.items():
        assert query.startswith("query ($")
        assert query.endswith("}")
        assert query.count("{") == query.count("}")
        assert all(name in query for name in fields)
    assert "edgeResponseBytes" in cf.ZONE_WINDOW
    assert "limit: 7," in cf.ZONE_COUNTRIES % 7


def test_country_series_carry_the_metric_name_and_the_window_total() -> None:
    ex, _ = exporter(FakeApi(), accounts=[], top_countries=2)
    ex.refresh(NOW)
    metrics = series(ex.metrics())
    countries = {
        k: v for k, v in metrics.items() if k.startswith("ayeops_cloudflare_country_requests{")
    }
    assert countries == {
        'ayeops_cloudflare_country_requests{country="UA",zone="acme.example"}': 800,
        'ayeops_cloudflare_country_requests{country="DE",zone="acme.example"}': 300,
        'ayeops_cloudflare_country_requests{country="other",zone="acme.example"}': 304,
    }


def test_origin_hosts_are_ranked_by_requests_not_by_time() -> None:
    slow = cf.origin_samples([origin_row("b", 9, 10), origin_row("a", 1, 1000)], "z", 1)
    assert {dict(p)["hostname"] for _, p, _ in slow} == {"b", "other"}
    unnamed = cf.origin_samples([origin_row("", 2, 10)], "z", 5)
    assert {dict(p)["hostname"] for _, p, _ in unnamed} == {""}


def test_storage_rows_without_a_name_keep_an_empty_label() -> None:
    samples = cf.r2_storage_samples(
        [{"max": {"payloadSize": 5}, "dimensions": {"datetime": "2026-01-15T11:00:00Z"}}], "acc"
    )
    assert value(samples, "ayeops_cloudflare_r2_storage_bytes", account="acc", bucket="") == 5


def test_a_resolved_zone_does_not_stop_the_lookup_of_the_next() -> None:
    api = FakeApi()
    answers = [fixture("zone_lookup.json"), {"success": False}, fixture("zone_lookup.json")]

    def lookup(url: str, headers: Mapping[str, str]) -> Any:
        return answers.pop(0)

    client = cf.Client(TOKEN, post=api.post, get=lookup)
    config = cf.Config(zones=["acme.example", "late.example"], accounts=[])
    ex = cf.Exporter(config, client, log=lambda record: None)
    ex.refresh(NOW)
    ex.refresh(NOW)
    assert set(ex.zones) == {"acme.example", "late.example"}
    assert answers == []


def test_logged_errors_are_cut_at_300_characters() -> None:
    long = "e" * 400
    api = FakeApi({"zone_day.json": OSError(long)})
    api.lookup = {"success": False, "errors": [], "result": None}
    ex, logs = exporter(api, accounts=[])
    ex.refresh(NOW)
    assert len(logs[0]["error"]) == len("zone acme.example is not visible to the token")
    api.lookup = fixture("zone_lookup.json")
    ex.refresh(NOW)
    failed = [r for r in logs if r["event"] == "query_failed"]
    assert failed[0]["error"] == "e" * 300


def test_zone_lookup_errors_are_cut_at_300_characters() -> None:
    api = FakeApi()
    api.lookup = OSError("z" * 400)
    ex, logs = exporter(api, accounts=[])
    ex.refresh(NOW)
    assert logs[0]["error"] == "z" * 300


def test_poll_duration_is_the_elapsed_time() -> None:
    ex, logs = exporter(FakeApi())
    began = time.monotonic()
    ex.refresh(NOW)
    spent = time.monotonic() - began
    assert 0 <= logs[-1]["seconds"] <= spent + 0.01
