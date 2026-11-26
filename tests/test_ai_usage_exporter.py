from __future__ import annotations

import importlib.util
import json
import re
import sys
import urllib.parse
from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta, timezone
from types import ModuleType
from typing import Any

import pytest
import yaml

from ayeops.registry import Module
from ayeops.render import _jinja
from tests.conftest import ROOT, rules_hit

MODULES = ROOT / "src" / "ayeops" / "modules"
FIXTURES = ROOT / "tests" / "fixtures" / "ai-usage"
ANTHROPIC_KEY = "sk-ant-admin01-never-shown-anywhere"
OPENAI_KEY = "sk-admin-never-shown-anywhere"
NOW = datetime(2026, 1, 15, 12, 7, 42, tzinfo=UTC)
ROUTES = {
    "/v1/organizations/cost_report": "anthropic_cost.json",
    "/v1/organizations/usage_report/messages": "anthropic_usage.json",
    "/v1/organizations/rate_limits": "anthropic_rate_limits.json",
    "/v1/organizations/api_keys": "anthropic_api_keys.json",
    "/v1/organization/costs": "openai_costs.json",
    "/v1/organization/usage/completions": "openai_usage.json",
}


def load_exporter() -> ModuleType:
    path = MODULES / "ai-usage" / "files" / "ai_usage_exporter.py"
    spec = importlib.util.spec_from_file_location("ai_usage_exporter", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


ai = load_exporter()


def fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class FakeGet:
    def __init__(self, overrides: Mapping[str, Any] | None = None):
        self.overrides = dict(overrides or {})
        self.urls: list[str] = []
        self.headers: list[Mapping[str, str]] = []

    def __call__(self, url: str, headers: Mapping[str, str]) -> Any:
        self.urls.append(url)
        self.headers.append(dict(headers))
        path = urllib.parse.urlsplit(url).path
        name = ROUTES[path]
        answer = self.overrides.get(name)
        if callable(answer):
            return answer(url)
        if isinstance(answer, Exception):
            raise answer
        return answer if answer is not None else fixture(name)

    def params(self, path: str) -> list[dict[str, list[str]]]:
        return [
            urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
            for url in self.urls
            if urllib.parse.urlsplit(url).path == path
        ]


def config(
    providers: tuple[str, ...] = ("anthropic", "openai"),
    keys: Mapping[str, str] | None = None,
    budgets: Mapping[str, float] | None = None,
    floor: float = 1.0,
) -> Any:
    found = keys if keys is not None else {"anthropic": ANTHROPIC_KEY, "openai": OPENAI_KEY}
    budget = budgets or {}
    return ai.Config(
        providers=[ai.Provider(name, budget.get(name)) for name in providers],
        spike_floor_usd=floor,
        keys={name: found.get(name, "") for name in providers},
    )


def exporter(get: FakeGet, cfg: Any | None = None) -> tuple[Any, list[Mapping[str, Any]]]:
    logs: list[Mapping[str, Any]] = []
    cfg = cfg if cfg is not None else config()
    return ai.Exporter(cfg, ai.clients_for(cfg, get), log=logs.append), logs


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


def names(samples: list[Any]) -> set[str]:
    return {n for n, _, _ in samples}


def test_anthropic_cost_is_cents_turned_into_dollars_per_model_month_and_day() -> None:
    samples = ai.cost_samples(
        "anthropic", ai.anthropic_costs([fixture("anthropic_cost.json")]), NOW
    )
    assert value(
        samples, "ayeops_ai_cost_month_usd", provider="anthropic", model="claude-opus-5-5"
    ) == pytest.approx(17.505)
    assert value(
        samples, "ayeops_ai_cost_month_usd", provider="anthropic", model="claude-sonnet-5-5"
    ) == pytest.approx(5.0)
    assert value(
        samples, "ayeops_ai_cost_month_usd", provider="anthropic", model="web_search"
    ) == pytest.approx(0.4)
    assert value(
        samples, "ayeops_ai_cost_day_usd", provider="anthropic", model="claude-opus-5-5"
    ) == pytest.approx(12.005)
    assert value(
        samples, "ayeops_ai_cost_day_usd", provider="anthropic", model="web_search"
    ) == pytest.approx(0.4)
    assert (
        "ayeops_ai_cost_day_usd",
        ai.labels(provider="anthropic", model="claude-sonnet-5-5"),
    ) not in {(n, p) for n, p, _ in samples}


def test_the_median_day_is_taken_over_the_seven_days_before_today() -> None:
    samples = ai.cost_samples(
        "anthropic", ai.anthropic_costs([fixture("anthropic_cost.json")]), NOW
    )
    assert value(samples, "ayeops_ai_cost_daily_median_7d_usd", provider="anthropic") == 1.0
    later = NOW + timedelta(days=2)
    shifted = ai.cost_samples(
        "anthropic", ai.anthropic_costs([fixture("anthropic_cost.json")]), later
    )
    assert value(shifted, "ayeops_ai_cost_daily_median_7d_usd", provider="anthropic") == 1.0
    quiet = NOW + timedelta(days=9)
    empty = ai.cost_samples(
        "anthropic", ai.anthropic_costs([fixture("anthropic_cost.json")]), quiet
    )
    assert value(empty, "ayeops_ai_cost_daily_median_7d_usd", provider="anthropic") == 0.0


def test_days_without_any_cost_count_as_zero_in_the_median() -> None:
    items = [
        ai.Item(date(2026, 1, 14), "m", "cost", 10.0),
        ai.Item(date(2026, 1, 13), "m", "cost", 10.0),
    ]
    samples = ai.cost_samples("openai", items, NOW)
    assert value(samples, "ayeops_ai_cost_daily_median_7d_usd", provider="openai") == 0.0
    busy = [ai.Item(date(2026, 1, 15) - timedelta(days=n), "m", "cost", 4.0) for n in range(1, 5)]
    assert (
        value(
            ai.cost_samples("openai", busy, NOW),
            "ayeops_ai_cost_daily_median_7d_usd",
            provider="openai",
        )
        == 4.0
    )


def test_the_median_window_excludes_today_and_includes_the_seventh_day_back() -> None:
    today = date(2026, 1, 15)
    spent = [0, 1, 2, 3]
    items = [ai.Item(today - timedelta(days=n), "m", "cost", 5.0) for n in spent]
    median = value(
        ai.cost_samples("openai", items, NOW),
        "ayeops_ai_cost_daily_median_7d_usd",
        provider="openai",
    )
    assert median == 0.0
    seventh = [*items, ai.Item(today - timedelta(days=7), "m", "cost", 5.0)]
    assert (
        value(
            ai.cost_samples("openai", seventh, NOW),
            "ayeops_ai_cost_daily_median_7d_usd",
            provider="openai",
        )
        == 5.0
    )
    eighth = [*items, ai.Item(today - timedelta(days=8), "m", "cost", 5.0)]
    assert (
        value(
            ai.cost_samples("openai", eighth, NOW),
            "ayeops_ai_cost_daily_median_7d_usd",
            provider="openai",
        )
        == 0.0
    )


def test_anthropic_usage_splits_input_into_uncached_cache_reads_and_both_cache_writes() -> None:
    samples = ai.usage_samples(
        "anthropic", ai.anthropic_usage([fixture("anthropic_usage.json")]), NOW
    )
    model = {"provider": "anthropic", "model": "claude-opus-5-5"}
    assert value(samples, "ayeops_ai_tokens_month", **model, kind="input_uncached") == 2500
    assert value(samples, "ayeops_ai_tokens_month", **model, kind="input_cache_read") == 10000
    assert value(samples, "ayeops_ai_tokens_month", **model, kind="input_cache_write") == 600
    assert value(samples, "ayeops_ai_tokens_month", **model, kind="output") == 1200
    assert value(samples, "ayeops_ai_tokens_day", **model, kind="output") == 700
    assert value(samples, "ayeops_ai_tokens_day", **model, kind="input_cache_write") == 300
    assert "ayeops_ai_requests_month" not in names(samples)


def test_openai_cost_is_already_dollars_and_the_model_comes_from_the_line_item() -> None:
    samples = ai.cost_samples("openai", ai.openai_costs([fixture("openai_costs.json")]), NOW)
    assert value(
        samples, "ayeops_ai_cost_month_usd", provider="openai", model="gpt-5-mini"
    ) == pytest.approx(2.5)
    assert value(
        samples, "ayeops_ai_cost_day_usd", provider="openai", model="gpt-5-mini"
    ) == pytest.approx(0.75)
    assert value(
        samples, "ayeops_ai_cost_month_usd", provider="openai", model="web search tool calls"
    ) == pytest.approx(0.1)
    assert value(
        samples, "ayeops_ai_cost_month_usd", provider="openai", model="other"
    ) == pytest.approx(0.05)


@pytest.mark.parametrize(
    ("line_item", "model"),
    [
        ("gpt-5-mini, input", "gpt-5-mini"),
        ("gpt-4o-2024-08-06, cached input", "gpt-4o-2024-08-06"),
        ("web search tool calls", "web search tool calls"),
        ("  ", "other"),
        (None, "other"),
    ],
)
def test_line_items_name_their_model_before_the_first_comma(line_item: Any, model: str) -> None:
    assert ai.line_item_model(line_item) == model


def test_openai_uncached_input_is_derived_and_requests_are_counted() -> None:
    samples = ai.usage_samples("openai", ai.openai_usage([fixture("openai_usage.json")]), NOW)
    model = {"provider": "openai", "model": "gpt-5-mini"}
    assert value(samples, "ayeops_ai_tokens_month", **model, kind="input_uncached") == 2200
    assert value(samples, "ayeops_ai_tokens_month", **model, kind="input_cache_read") == 1800
    assert value(samples, "ayeops_ai_tokens_month", **model, kind="output") == 1400
    assert value(samples, "ayeops_ai_requests_month", **model) == 12
    assert value(samples, "ayeops_ai_tokens_day", **model, kind="input_uncached") == 2000


def test_an_explicit_uncached_count_wins_over_the_derived_one() -> None:
    page = {
        "data": [
            {
                "start_time": int(NOW.timestamp()),
                "results": [
                    {
                        "model": "m",
                        "input_tokens": 100,
                        "input_cached_tokens": 10,
                        "input_uncached_tokens": 50,
                    }
                ],
            }
        ]
    }
    samples = ai.usage_samples("openai", ai.openai_usage([page]), NOW)
    assert (
        value(samples, "ayeops_ai_tokens_day", provider="openai", model="m", kind="input_uncached")
        == 50
    )
    clipped = {
        "data": [
            {
                "start_time": int(NOW.timestamp()),
                "results": [{"model": "m", "input_tokens": 5, "input_cached_tokens": 9}],
            }
        ]
    }
    low = ai.usage_samples("openai", ai.openai_usage([clipped]), NOW)
    assert (
        value(low, "ayeops_ai_tokens_day", provider="openai", model="m", kind="input_uncached") == 0
    )


def test_rate_limits_carry_the_limit_group_label_and_every_limiter() -> None:
    samples = ai.rate_limit_samples([fixture("anthropic_rate_limits.json")])
    assert (
        value(
            samples,
            "ayeops_ai_rate_limit",
            provider="anthropic",
            limit_group="Claude Opus 5.5",
            limit="output_tokens_per_minute",
        )
        == 800000
    )
    assert (
        value(
            samples,
            "ayeops_ai_rate_limit",
            provider="anthropic",
            limit_group="batch",
            limit="enqueued_batch_requests",
        )
        == 500000
    )
    assert len(samples) == 4


def test_only_active_keys_with_an_expiry_are_reported() -> None:
    samples = ai.api_key_samples([fixture("anthropic_api_keys.json")])
    assert samples == [
        (
            "ayeops_ai_api_key_expiry_timestamp_seconds",
            ai.labels(provider="anthropic", key="enrich"),
            datetime(2026, 1, 20, tzinfo=UTC).timestamp(),
        )
    ]


def test_a_full_poll_serves_every_dataset_of_both_providers() -> None:
    get = FakeGet()
    ex, logs = exporter(get)
    ex.refresh(NOW)
    metrics = series(ex.metrics())
    for provider, datasets in ai.DATASETS.items():
        for dataset in datasets:
            assert (
                metrics[f'ayeops_ai_query_success{{dataset="{dataset}",provider="{provider}"}}']
                == 1
            )
        assert (
            metrics[f'ayeops_ai_last_success_timestamp_seconds{{provider="{provider}"}}']
            == NOW.timestamp()
        )
        assert metrics[f'ayeops_ai_provider_enabled{{provider="{provider}"}}'] == 1
    assert metrics['ayeops_ai_api_requests_total{provider="anthropic"}'] == 4
    assert metrics['ayeops_ai_api_requests_total{provider="openai"}'] == 2
    assert metrics["ayeops_ai_poll_timestamp_seconds"] == NOW.timestamp()
    assert logs[-1]["event"] == "poll" and logs[-1]["failed"] == 0


def test_queries_start_at_the_month_or_a_week_back_and_group_as_documented() -> None:
    get = FakeGet()
    ex, _ = exporter(get)
    ex.refresh(NOW)
    cost = get.params("/v1/organizations/cost_report")[0]
    assert cost == {
        "starting_at": ["2026-01-01T00:00:00Z"],
        "bucket_width": ["1d"],
        "limit": ["31"],
        "group_by[]": ["description"],
    }
    assert get.params("/v1/organizations/usage_report/messages")[0]["group_by[]"] == ["model"]
    assert get.params("/v1/organizations/api_keys")[0] == {"status": ["active"], "limit": ["100"]}
    openai = get.params("/v1/organization/costs")[0]
    assert openai == {
        "start_time": [str(int(datetime(2026, 1, 1, tzinfo=UTC).timestamp()))],
        "bucket_width": ["1d"],
        "limit": ["31"],
        "group_by": ["line_item"],
    }
    assert get.params("/v1/organization/usage/completions")[0]["group_by"] == ["model"]
    early = FakeGet()
    ex2, _ = exporter(early)
    ex2.refresh(datetime(2026, 2, 3, 9, 0, tzinfo=UTC))
    assert early.params("/v1/organizations/cost_report")[0]["starting_at"] == [
        "2026-01-27T00:00:00Z"
    ]


def test_the_keys_travel_only_in_their_own_header_and_are_never_logged_or_served() -> None:
    get = FakeGet({"anthropic_usage.json": ai.ProviderError("HTTP 500", status=500)})
    ex, logs = exporter(get)
    ex.refresh(NOW)
    for url, headers in zip(get.urls, get.headers, strict=True):
        assert ANTHROPIC_KEY not in url and OPENAI_KEY not in url
        if "api.anthropic.com" in url:
            assert headers["x-api-key"] == ANTHROPIC_KEY
            assert set(headers) == {"x-api-key", "anthropic-version", "User-Agent"}
            assert headers["anthropic-version"] == "2023-06-01"
            assert OPENAI_KEY not in json.dumps(headers)
        else:
            assert headers["Authorization"] == f"Bearer {OPENAI_KEY}"
            assert set(headers) == {"Authorization", "Content-Type", "User-Agent"}
            assert ANTHROPIC_KEY not in json.dumps(headers)
    text = json.dumps(logs) + ex.metrics()
    assert ANTHROPIC_KEY not in text and OPENAI_KEY not in text


def test_a_provider_without_a_key_sends_nothing_and_says_so() -> None:
    get = FakeGet()
    ex, logs = exporter(get, config(keys={"openai": OPENAI_KEY}))
    ex.refresh(NOW)
    assert get.urls and all("api.openai.com" in url for url in get.urls)
    metrics = series(ex.metrics())
    assert metrics['ayeops_ai_provider_enabled{provider="anthropic"}'] == 0
    assert not any('provider="anthropic"' in key for key in metrics if "enabled" not in key)
    assert logs[-1]["providers"] == ["openai"]
    assert (
        ai.config_from({"AYE_AI_PROVIDERS": "anthropic", "AYE_ANTHROPIC_ADMIN_KEY": "  "}).enabled()
        == []
    )


def test_budget_series_exist_only_for_providers_with_a_budget() -> None:
    ex, _ = exporter(FakeGet(), config(budgets={"anthropic": 50}))
    ex.refresh(NOW)
    metrics = series(ex.metrics())
    assert metrics['ayeops_ai_budget_usd{provider="anthropic"}'] == 50
    assert 'ayeops_ai_budget_usd{provider="openai"}' not in metrics
    plain, _ = exporter(FakeGet())
    plain.refresh(NOW)
    assert "ayeops_ai_budget_usd" not in plain.metrics()
    assert series(plain.metrics())['ayeops_ai_spike_floor_usd{provider="openai"}'] == 1


def test_the_month_rolls_over_at_utc_midnight_not_local_midnight() -> None:
    kyiv = timezone(timedelta(hours=2))
    local_february = datetime(2026, 2, 1, 1, 30, tzinfo=kyiv)
    assert ai.month_start(local_february) == datetime(2026, 1, 1, tzinfo=UTC)
    assert ai.day_start(local_february) == datetime(2026, 1, 31, tzinfo=UTC)
    items = [
        ai.Item(date(2026, 1, 31), "m", "cost", 7.0),
        ai.Item(date(2026, 2, 1), "m", "cost", 2.0),
    ]
    january = ai.cost_samples("openai", items, local_february)
    assert value(january, "ayeops_ai_cost_month_usd", provider="openai", model="m") == 7.0
    assert value(january, "ayeops_ai_cost_day_usd", provider="openai", model="m") == 7.0
    february = ai.cost_samples("openai", items, datetime(2026, 2, 1, 0, 5, tzinfo=UTC))
    assert value(february, "ayeops_ai_cost_month_usd", provider="openai", model="m") == 2.0
    assert value(february, "ayeops_ai_cost_daily_median_7d_usd", provider="openai") == 0.0
    tokens = [
        ai.Item(date(2026, 1, 31), "m", "output", 5.0),
        ai.Item(date(2026, 2, 1), "m", "output", 3.0),
    ]
    feb_tokens = ai.usage_samples("openai", tokens, datetime(2026, 2, 1, 0, 5, tzinfo=UTC))
    assert (
        value(feb_tokens, "ayeops_ai_tokens_month", provider="openai", model="m", kind="output")
        == 3.0
    )


def test_pages_follow_next_page_for_reports_and_last_id_for_keys() -> None:
    first = dict(fixture("anthropic_cost.json"), has_more=True, next_page="page_two")
    second = {"data": [], "has_more": False, "next_page": None}

    def cost(url: str) -> Any:
        return second if "page=page_two" in url else first

    keys_first = dict(fixture("anthropic_api_keys.json"), has_more=True, last_id="apikey_03")
    keys_second = {"data": [], "has_more": False, "last_id": None}

    def keys(url: str) -> Any:
        return keys_second if "after_id=apikey_03" in url else keys_first

    get = FakeGet({"anthropic_cost.json": cost, "anthropic_api_keys.json": keys})
    ex, _ = exporter(get, config(providers=("anthropic",)))
    ex.refresh(NOW)
    assert [p.get("page") for p in get.params("/v1/organizations/cost_report")] == [
        None,
        ["page_two"],
    ]
    assert [p.get("after_id") for p in get.params("/v1/organizations/api_keys")] == [
        None,
        ["apikey_03"],
    ]


def test_pagination_stops_at_its_limit_and_on_a_page_without_a_cursor() -> None:
    endless = {"data": [], "has_more": True, "next_page": "again"}
    get = FakeGet({"anthropic_rate_limits.json": endless})
    client = ai.Client("anthropic", ANTHROPIC_KEY, get)
    assert len(client.pages(f"{ai.ANTHROPIC}/rate_limits", [], limit=3)) == 3
    stuck = FakeGet({"anthropic_rate_limits.json": {"data": [], "has_more": True}})
    assert (
        len(ai.Client("anthropic", ANTHROPIC_KEY, stuck).pages(f"{ai.ANTHROPIC}/rate_limits", []))
        == 1
    )


def test_a_failed_dataset_is_marked_and_the_others_still_report() -> None:
    get = FakeGet({"openai_costs.json": ai.ProviderError("HTTP 401", status=401)})
    ex, logs = exporter(get)
    ex.refresh(NOW)
    metrics = series(ex.metrics())
    assert metrics['ayeops_ai_query_success{dataset="cost",provider="openai"}'] == 0
    assert metrics['ayeops_ai_query_success{dataset="usage",provider="openai"}'] == 1
    assert 'ayeops_ai_last_success_timestamp_seconds{provider="openai"}' not in metrics
    assert (
        metrics['ayeops_ai_last_success_timestamp_seconds{provider="anthropic"}'] == NOW.timestamp()
    )
    assert metrics['ayeops_ai_query_errors_total{dataset="cost",provider="openai"}'] == 1
    failed = [r for r in logs if r["event"] == "query_failed"]
    assert failed == [
        {
            "event": "query_failed",
            "provider": "openai",
            "dataset": "cost",
            "status": 401,
            "error": "HTTP 401",
        }
    ]
    assert logs[-1]["failed"] == 1


def test_the_last_full_answer_is_kept_across_a_later_failure() -> None:
    get = FakeGet()
    ex, _ = exporter(get, config(providers=("openai",)))
    ex.refresh(NOW)
    get.overrides["openai_usage.json"] = OSError("reset")
    ex.refresh(NOW + timedelta(minutes=15))
    metrics = series(ex.metrics())
    assert metrics['ayeops_ai_last_success_timestamp_seconds{provider="openai"}'] == NOW.timestamp()
    assert metrics['ayeops_ai_query_success{dataset="usage",provider="openai"}'] == 0


@pytest.mark.parametrize(
    "answer",
    [
        ["not", "an", "object"],
        {"error": {"message": "invalid x-api-key", "type": "authentication_error"}},
        {"error": "denied"},
    ],
)
def test_an_error_body_or_a_non_object_is_a_failed_query(answer: Any) -> None:
    get = FakeGet({"anthropic_rate_limits.json": answer})
    ex, logs = exporter(get, config(providers=("anthropic",)))
    ex.refresh(NOW)
    assert (
        series(ex.metrics())['ayeops_ai_query_success{dataset="rate_limits",provider="anthropic"}']
        == 0
    )
    assert [r["dataset"] for r in logs if r["event"] == "query_failed"] == ["rate_limits"]


def test_config_reads_providers_budgets_floor_and_keys_from_the_environment() -> None:
    cfg = ai.config_from(
        {
            "AYE_AI_PROVIDERS": "anthropic, openai",
            "AYE_AI_BUDGETS": '{"anthropic": 50, "openai": null}',
            "AYE_AI_SPIKE_FLOOR_USD": "2.5",
            "AYE_ANTHROPIC_ADMIN_KEY": f" {ANTHROPIC_KEY} ",
            "AYE_OPENAI_ADMIN_KEY": OPENAI_KEY,
        }
    )
    assert [(p.name, p.budget_usd) for p in cfg.providers] == [
        ("anthropic", 50.0),
        ("openai", None),
    ]
    assert cfg.spike_floor_usd == 2.5
    assert cfg.keys == {"anthropic": ANTHROPIC_KEY, "openai": OPENAI_KEY}
    defaults = ai.config_from({})
    assert defaults.providers == [] and defaults.spike_floor_usd == 1.0


@pytest.mark.parametrize(
    "env",
    [{"AYE_AI_PROVIDERS": "mistral"}, {"AYE_AI_PROVIDERS": "openai", "AYE_AI_BUDGETS": "[1]"}],
)
def test_config_refuses_unknown_providers_and_malformed_budgets(env: dict[str, str]) -> None:
    with pytest.raises(ValueError):
        ai.config_from(env)


def test_values_render_with_help_types_and_escaped_labels() -> None:
    text = ai.render(
        [
            (
                "ayeops_ai_rate_limit",
                ai.labels(provider="anthropic", limit_group='a"b', limit="x"),
                3.0,
            ),
            ("ayeops_ai_api_requests_total", ai.labels(provider="openai"), 2.0),
            ("ayeops_ai_cost_day_usd", ai.labels(provider="openai", model="m"), 0.25),
        ]
    )
    assert "# TYPE ayeops_ai_api_requests_total counter" in text
    assert "# TYPE ayeops_ai_rate_limit gauge" in text
    assert 'ayeops_ai_rate_limit{limit="x",limit_group="a\\"b",provider="anthropic"} 3' in text
    assert 'ayeops_ai_cost_day_usd{model="m",provider="openai"} 0.25' in text
    assert ai.number("12.5") == 12.5 and ai.number(None) == 0.0 and ai.number("x") == 0.0
    assert ai.parse_time("not a time") is None and ai.parse_time(None) is None


def test_no_exported_series_uses_the_host_label_that_vmagent_owns() -> None:
    ex, _ = exporter(FakeGet())
    ex.refresh(NOW)
    keys = {
        part.split("=", 1)[0]
        for line in ex.metrics().splitlines()
        if "{" in line and not line.startswith("#")
        for part in line[line.index("{") + 1 : line.rindex("}")].split(",")
    }
    assert "host" not in keys
    assert {"provider", "model", "kind", "dataset"} <= keys


def test_alert_and_dashboard_expressions_name_only_exported_metrics(
    modules: dict[str, Module],
) -> None:
    module = modules["ai-usage"]
    source = (module.path / "files" / "ai_usage_exporter.py").read_text(encoding="utf-8")
    dashboard = json.loads((module.path / "dashboards" / "ai.json").read_text(encoding="utf-8"))
    exprs = [alert["expr"] for alert in module.alerts]
    exprs += [t["expr"] for p in dashboard["panels"] for t in p.get("targets", [])]
    exprs += [dashboard["templating"]["list"][0]["query"]["query"]]
    found = {name for expr in exprs for name in re.findall(r"ayeops_ai_[a-z0-9_]+", expr)}
    assert found
    assert sorted(n for n in found if f'"{n}"' not in source) == []
    assert all(f'"{n}"' in source for n in module.catalog["ai-usage-exporter"]["metrics"])
    assert set(re.findall(r'"(ayeops_ai_[a-z0-9_]+)"', source)) <= set(ai.HELP)


def test_the_spike_alert_needs_both_the_ratio_and_the_floor(modules: dict[str, Module]) -> None:
    alerts = {alert["name"]: alert for alert in modules["ai-usage"].alerts}
    spike = alerts["ai-spend-spike"]["expr"]
    assert "> bool 3 * on (provider) ayeops_ai_cost_daily_median_7d_usd" in spike
    assert (
        "and sum by (provider) (ayeops_ai_cost_day_usd) > on (provider) ayeops_ai_spike_floor_usd"
        in spike
    )
    assert alerts["ai-budget-80"]["expr"].endswith("ayeops_ai_budget_usd > bool 0.8")
    assert alerts["ai-budget-exceeded"]["expr"].endswith("ayeops_ai_budget_usd > bool 1")
    assert alerts["ai-budget-exceeded"]["severity"] == "critical"


def render_env(modules: dict[str, Module], settings: dict[str, Any]) -> dict[str, str]:
    module = modules["ai-usage"]
    text = (
        _jinja()
        .from_string(module.template("compose.yaml.j2"))
        .render(settings=module.settings(settings))
    )
    return dict(yaml.safe_load(text)["services"]["ai-usage-exporter"]["environment"])


def test_only_the_listed_providers_get_a_required_key_variable(modules: dict[str, Module]) -> None:
    both = render_env(
        modules, {"providers": ["anthropic", "openai"], "monthly_budget_usd": {"openai": 20}}
    )
    assert (
        both["AYE_ANTHROPIC_ADMIN_KEY"] == "${AYE_ANTHROPIC_ADMIN_KEY:?set AYE_ANTHROPIC_ADMIN_KEY}"
    )
    assert both["AYE_OPENAI_ADMIN_KEY"] == "${AYE_OPENAI_ADMIN_KEY:?set AYE_OPENAI_ADMIN_KEY}"
    assert both["AYE_AI_PROVIDERS"] == "anthropic,openai"
    assert json.loads(both["AYE_AI_BUDGETS"]) == {"openai": 20}
    assert both["AYE_AI_REFRESH_SECONDS"] == "900" and both["AYE_AI_SPIKE_FLOOR_USD"] == "1"
    only = render_env(modules, {"providers": ["openai"]})
    assert "AYE_ANTHROPIC_ADMIN_KEY" not in only
    parsed = ai.config_from({**only, "AYE_OPENAI_ADMIN_KEY": OPENAI_KEY})
    assert [p.name for p in parsed.enabled()] == ["openai"]


def with_module(data: dict[str, Any], settings: dict[str, Any]) -> dict[str, Any]:
    data["hosts"]["central"]["modules"]["ai-usage"] = settings
    return data


def test_the_module_passes_the_check_with_providers(data: dict[str, Any]) -> None:
    assert (
        rules_hit(
            with_module(data, {"providers": ["anthropic"], "monthly_budget_usd": {"anthropic": 10}})
        )
        == set()
    )


@pytest.mark.parametrize(
    "settings",
    [
        {},
        {"providers": []},
        {"providers": ["mistral"]},
        {"providers": ["openai", "openai"]},
        {"providers": ["openai"], "monthly_budget_usd": {"openai": 0}},
        {"providers": ["anthropic"], "monthly_budget_usd": {"anthropic": 0}},
        {"providers": ["openai"], "monthly_budget_usd": {"mistral": 5}},
        {"providers": ["openai"], "refresh_seconds": 60},
        {"providers": ["openai"], "spike_floor_usd": -1},
    ],
)
def test_the_schema_refuses_bad_settings(data: dict[str, Any], settings: dict[str, Any]) -> None:
    assert "module-settings" in rules_hit(with_module(data, settings))
