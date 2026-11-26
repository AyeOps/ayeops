from __future__ import annotations

import json
import os
import statistics
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

ANTHROPIC = "https://api.anthropic.com/v1/organizations"
OPENAI = "https://api.openai.com/v1/organization"
ANTHROPIC_VERSION = "2023-06-01"
PROVIDERS = ("anthropic", "openai")
KEY_ENV = {"anthropic": "AYE_ANTHROPIC_ADMIN_KEY", "openai": "AYE_OPENAI_ADMIN_KEY"}
DATASETS = {
    "anthropic": ("cost", "usage", "rate_limits", "api_keys"),
    "openai": ("cost", "usage"),
}
MEDIAN_DAYS = 7
DAY_BUCKETS = 31
OTHER = "other"

Labels = tuple[tuple[str, str], ...]
Sample = tuple[str, Labels, float]
Get = Callable[[str, Mapping[str, str]], Any]
Log = Callable[[Mapping[str, Any]], None]

HELP = {
    "ayeops_ai_cost_month_usd": "Cost of the UTC month so far in USD by provider and model",
    "ayeops_ai_cost_day_usd": "Cost of the UTC day so far in USD by provider and model",
    "ayeops_ai_cost_daily_median_7d_usd": "Median daily cost in USD of the 7 UTC days before today",
    "ayeops_ai_tokens_month": "Tokens of the UTC month so far by provider, model and kind",
    "ayeops_ai_tokens_day": "Tokens of the UTC day so far by provider, model and kind",
    "ayeops_ai_requests_month": "Model requests of the UTC month so far where counted",
    "ayeops_ai_budget_usd": "Monthly budget in USD set in the manifest",
    "ayeops_ai_spike_floor_usd": "Daily cost in USD below which a spike never alerts",
    "ayeops_ai_rate_limit": "Configured rate limit of the organization by limiter",
    "ayeops_ai_api_key_expiry_timestamp_seconds": "When an active API key expires",
    "ayeops_ai_provider_enabled": "1 when the admin key of the provider is set",
    "ayeops_ai_query_success": "1 when the last query of the dataset answered",
    "ayeops_ai_last_success_timestamp_seconds": "When every dataset of the provider last answered",
    "ayeops_ai_poll_timestamp_seconds": "When the last poll finished",
    "ayeops_ai_api_requests_total": "HTTP requests sent to the provider APIs",
    "ayeops_ai_query_errors_total": "Queries that failed",
}
COUNTERS = frozenset({"ayeops_ai_api_requests_total", "ayeops_ai_query_errors_total"})


class ProviderError(Exception):
    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


def labels(**pairs: str) -> Labels:
    return tuple(sorted(pairs.items()))


def escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def format_value(value: float) -> str:
    if value == int(value) and abs(value) < 1e15:
        return str(int(value))
    return repr(float(value))


def render(samples: Iterable[Sample]) -> str:
    grouped: dict[str, list[tuple[Labels, float]]] = {}
    for name, pairs, value in samples:
        grouped.setdefault(name, []).append((pairs, value))
    lines: list[str] = []
    for name in sorted(grouped):
        lines.append(f"# HELP {name} {HELP.get(name, name)}")
        lines.append(f"# TYPE {name} {'counter' if name in COUNTERS else 'gauge'}")
        for pairs, value in sorted(grouped[name]):
            body = ",".join(f'{key}="{escape(text)}"' for key, text in pairs)
            text = format_value(value)
            lines.append(f"{name}{{{body}}} {text}" if body else f"{name} {text}")
    return "\n".join(lines) + "\n"


def day_start(now: datetime) -> datetime:
    return now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)


def month_start(now: datetime) -> datetime:
    return day_start(now).replace(day=1)


def query_start(now: datetime) -> datetime:
    return min(month_start(now), day_start(now) - timedelta(days=MEDIAN_DAYS))


def iso(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def number(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def bucket_day(value: Any) -> date:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, UTC).date()
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(UTC).date()


def parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


@dataclass(frozen=True)
class Item:
    day: date
    model: str
    kind: str
    value: float


def buckets(pages: Sequence[Mapping[str, Any]]) -> list[tuple[date, list[Mapping[str, Any]]]]:
    found: list[tuple[date, list[Mapping[str, Any]]]] = []
    for page in pages:
        for bucket in page.get("data") or []:
            start = bucket.get("starting_at", bucket.get("start_time"))
            found.append((bucket_day(start), list(bucket.get("results") or [])))
    return found


def anthropic_costs(pages: Sequence[Mapping[str, Any]]) -> list[Item]:
    items = []
    for day, results in buckets(pages):
        for row in results:
            model = str(row.get("model") or row.get("cost_type") or OTHER)
            items.append(Item(day, model, "cost", number(row.get("amount")) / 100))
    return items


def anthropic_usage(pages: Sequence[Mapping[str, Any]]) -> list[Item]:
    items = []
    for day, results in buckets(pages):
        for row in results:
            model = str(row.get("model") or OTHER)
            cache = row.get("cache_creation") or {}
            write = number(cache.get("ephemeral_1h_input_tokens")) + number(
                cache.get("ephemeral_5m_input_tokens")
            )
            items += [
                Item(day, model, "input_uncached", number(row.get("uncached_input_tokens"))),
                Item(day, model, "input_cache_read", number(row.get("cache_read_input_tokens"))),
                Item(day, model, "input_cache_write", write),
                Item(day, model, "output", number(row.get("output_tokens"))),
            ]
    return items


def line_item_model(line_item: Any) -> str:
    text = str(line_item or "").strip()
    if not text:
        return OTHER
    return text.split(", ", 1)[0]


def openai_costs(pages: Sequence[Mapping[str, Any]]) -> list[Item]:
    items = []
    for day, results in buckets(pages):
        for row in results:
            amount = row.get("amount") or {}
            items.append(
                Item(
                    day, line_item_model(row.get("line_item")), "cost", number(amount.get("value"))
                )
            )
    return items


def openai_usage(pages: Sequence[Mapping[str, Any]]) -> list[Item]:
    items = []
    for day, results in buckets(pages):
        for row in results:
            model = str(row.get("model") or OTHER)
            cached = number(row.get("input_cached_tokens"))
            uncached = row.get("input_uncached_tokens")
            if uncached is None:
                uncached = max(number(row.get("input_tokens")) - cached, 0.0)
            items += [
                Item(day, model, "input_uncached", number(uncached)),
                Item(day, model, "input_cache_read", cached),
                Item(day, model, "input_cache_write", number(row.get("input_cache_write_tokens"))),
                Item(day, model, "output", number(row.get("output_tokens"))),
                Item(day, model, "requests", number(row.get("num_model_requests"))),
            ]
    return items


def add(totals: dict[tuple[str, ...], float], key: tuple[str, ...], value: float) -> None:
    totals[key] = totals.get(key, 0.0) + value


def cost_samples(provider: str, items: Sequence[Item], now: datetime) -> list[Sample]:
    today = day_start(now).date()
    first = month_start(now).date()
    month: dict[tuple[str, ...], float] = {}
    day: dict[tuple[str, ...], float] = {}
    daily: dict[date, float] = {}
    for item in items:
        if first <= item.day <= today:
            add(month, (item.model,), item.value)
        if item.day == today:
            add(day, (item.model,), item.value)
        daily[item.day] = daily.get(item.day, 0.0) + item.value
    previous = [daily.get(today - timedelta(days=n), 0.0) for n in range(1, MEDIAN_DAYS + 1)]
    out: list[Sample] = [
        ("ayeops_ai_cost_month_usd", labels(provider=provider, model=model), value)
        for (model,), value in month.items()
    ]
    out += [
        ("ayeops_ai_cost_day_usd", labels(provider=provider, model=model), value)
        for (model,), value in day.items()
    ]
    out.append(
        (
            "ayeops_ai_cost_daily_median_7d_usd",
            labels(provider=provider),
            statistics.median(previous),
        )
    )
    return out


def usage_samples(provider: str, items: Sequence[Item], now: datetime) -> list[Sample]:
    today = day_start(now).date()
    first = month_start(now).date()
    month: dict[tuple[str, ...], float] = {}
    day: dict[tuple[str, ...], float] = {}
    for item in items:
        if first <= item.day <= today:
            add(month, (item.model, item.kind), item.value)
        if item.day == today:
            add(day, (item.model, item.kind), item.value)
    out: list[Sample] = []
    for (model, kind), value in month.items():
        if kind == "requests":
            out.append(("ayeops_ai_requests_month", labels(provider=provider, model=model), value))
        else:
            out.append(
                ("ayeops_ai_tokens_month", labels(provider=provider, model=model, kind=kind), value)
            )
    out += [
        ("ayeops_ai_tokens_day", labels(provider=provider, model=model, kind=kind), value)
        for (model, kind), value in day.items()
        if kind != "requests"
    ]
    return out


def rate_limit_samples(pages: Sequence[Mapping[str, Any]]) -> list[Sample]:
    out: list[Sample] = []
    for page in pages:
        for entry in page.get("data") or []:
            group = entry.get("group") or {}
            name = str(
                group.get("display_name") or group.get("type") or entry.get("group_type") or OTHER
            )
            for limit in entry.get("limits") or []:
                out.append(
                    (
                        "ayeops_ai_rate_limit",
                        labels(
                            provider="anthropic",
                            limit_group=name,
                            limit=str(limit.get("type") or OTHER),
                        ),
                        number(limit.get("value")),
                    )
                )
    return out


def api_key_samples(pages: Sequence[Mapping[str, Any]]) -> list[Sample]:
    out: list[Sample] = []
    for page in pages:
        for key in page.get("data") or []:
            expires = parse_time(key.get("expires_at"))
            if key.get("status") != "active" or expires is None:
                continue
            out.append(
                (
                    "ayeops_ai_api_key_expiry_timestamp_seconds",
                    labels(
                        provider="anthropic", key=str(key.get("name") or key.get("id") or OTHER)
                    ),
                    expires.timestamp(),
                )
            )
    return out


@dataclass
class Provider:
    name: str
    budget_usd: float | None = None


@dataclass
class Config:
    providers: list[Provider]
    spike_floor_usd: float = 1.0
    keys: dict[str, str] = field(default_factory=dict)

    def enabled(self) -> list[Provider]:
        return [p for p in self.providers if self.keys.get(p.name)]


def http_get(url: str, headers: Mapping[str, str]) -> Any:
    request = urllib.request.Request(url, headers=dict(headers))
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        raise ProviderError(f"HTTP {error.code}", status=error.code) from None


def log_json(record: Mapping[str, Any]) -> None:
    print(json.dumps(record, sort_keys=True), flush=True)


def with_query(url: str, params: Sequence[tuple[str, str]]) -> str:
    return f"{url}?{urllib.parse.urlencode(list(params))}"


class Client:
    def __init__(self, provider: str, key: str, get: Get = http_get):
        self.provider = provider
        if provider == "anthropic":
            self._headers = {
                "x-api-key": key,
                "anthropic-version": ANTHROPIC_VERSION,
                "User-Agent": "ayeops-ai-usage-exporter",
            }
        else:
            self._headers = {
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                "User-Agent": "ayeops-ai-usage-exporter",
            }
        self._get = get
        self.sent = 0

    def fetch(self, url: str) -> Mapping[str, Any]:
        self.sent += 1
        answer = self._get(url, self._headers)
        if not isinstance(answer, Mapping):
            raise ProviderError("response is not a JSON object")
        if answer.get("error"):
            error = answer["error"]
            message = error.get("message") if isinstance(error, Mapping) else error
            raise ProviderError(str(message)[:300])
        return answer

    def pages(
        self, url: str, params: Sequence[tuple[str, str]], limit: int = 20
    ) -> list[Mapping[str, Any]]:
        found: list[Mapping[str, Any]] = []
        cursor: tuple[str, str] | None = None
        for _ in range(limit):
            page = self.fetch(with_query(url, [*params, *([cursor] if cursor else [])]))
            found.append(page)
            if not page.get("has_more"):
                break
            if page.get("next_page"):
                cursor = ("page", str(page["next_page"]))
            elif page.get("last_id"):
                cursor = ("after_id", str(page["last_id"]))
            else:
                break
        return found


def anthropic_queries(now: datetime) -> dict[str, tuple[str, list[tuple[str, str]]]]:
    start = iso(query_start(now))
    window = [("starting_at", start), ("bucket_width", "1d"), ("limit", str(DAY_BUCKETS))]
    return {
        "cost": (f"{ANTHROPIC}/cost_report", [*window, ("group_by[]", "description")]),
        "usage": (f"{ANTHROPIC}/usage_report/messages", [*window, ("group_by[]", "model")]),
        "rate_limits": (f"{ANTHROPIC}/rate_limits", []),
        "api_keys": (f"{ANTHROPIC}/api_keys", [("status", "active"), ("limit", "100")]),
    }


def openai_queries(now: datetime) -> dict[str, tuple[str, list[tuple[str, str]]]]:
    start = str(int(query_start(now).timestamp()))
    window = [("start_time", start), ("bucket_width", "1d"), ("limit", str(DAY_BUCKETS))]
    return {
        "cost": (f"{OPENAI}/costs", [*window, ("group_by", "line_item")]),
        "usage": (f"{OPENAI}/usage/completions", [*window, ("group_by", "model")]),
    }


class Exporter:
    def __init__(self, config: Config, clients: Mapping[str, Client], log: Log = log_json):
        self.config = config
        self.clients = dict(clients)
        self.log = log
        self.samples: list[Sample] = []
        self.requests_total: dict[str, int] = {}
        self.errors_total: dict[tuple[str, str], int] = {}
        self.last_success: dict[str, float] = {}
        self.lock = threading.Lock()

    def run(
        self,
        out: list[Sample],
        client: Client,
        dataset: str,
        query: tuple[str, list[tuple[str, str]]],
        parse: Callable[[list[Mapping[str, Any]]], list[Sample]],
    ) -> bool:
        key = labels(provider=client.provider, dataset=dataset)
        try:
            produced = parse(client.pages(*query))
        except (ProviderError, OSError, ValueError, KeyError, TypeError, AttributeError) as error:
            self.errors_total[(client.provider, dataset)] = (
                self.errors_total.get((client.provider, dataset), 0) + 1
            )
            self.log(
                {
                    "event": "query_failed",
                    "provider": client.provider,
                    "dataset": dataset,
                    "status": getattr(error, "status", 0),
                    "error": str(error)[:300],
                }
            )
            out.append(("ayeops_ai_query_success", key, 0))
            return False
        out.extend(produced)
        out.append(("ayeops_ai_query_success", key, 1))
        return True

    def collect(self, out: list[Sample], provider: Provider, now: datetime) -> None:
        client = self.clients[provider.name]
        if provider.name == "anthropic":
            queries = anthropic_queries(now)
            parsers: dict[str, Callable[[list[Mapping[str, Any]]], list[Sample]]] = {
                "cost": lambda pages: cost_samples("anthropic", anthropic_costs(pages), now),
                "usage": lambda pages: usage_samples("anthropic", anthropic_usage(pages), now),
                "rate_limits": rate_limit_samples,
                "api_keys": api_key_samples,
            }
        else:
            queries = openai_queries(now)
            parsers = {
                "cost": lambda pages: cost_samples("openai", openai_costs(pages), now),
                "usage": lambda pages: usage_samples("openai", openai_usage(pages), now),
            }
        answered = [
            self.run(out, client, dataset, queries[dataset], parsers[dataset])
            for dataset in DATASETS[provider.name]
        ]
        if all(answered):
            self.last_success[provider.name] = now.timestamp()

    def refresh(self, now: datetime | None = None) -> None:
        moment = now or datetime.now(UTC)
        began = time.monotonic()
        out: list[Sample] = []
        enabled = {provider.name for provider in self.config.enabled()}
        for provider in self.config.providers:
            key = labels(provider=provider.name)
            out.append(("ayeops_ai_provider_enabled", key, 1 if provider.name in enabled else 0))
            if provider.name not in enabled:
                continue
            out.append(("ayeops_ai_spike_floor_usd", key, self.config.spike_floor_usd))
            if provider.budget_usd is not None:
                out.append(("ayeops_ai_budget_usd", key, provider.budget_usd))
            sent_before = self.clients[provider.name].sent
            self.collect(out, provider, moment)
            sent = self.clients[provider.name].sent - sent_before
            self.requests_total[provider.name] = self.requests_total.get(provider.name, 0) + sent
            if provider.name in self.last_success:
                out.append(
                    (
                        "ayeops_ai_last_success_timestamp_seconds",
                        key,
                        self.last_success[provider.name],
                    )
                )
        out.append(("ayeops_ai_poll_timestamp_seconds", (), moment.timestamp()))
        failed = sum(1 for name, _, value in out if name == "ayeops_ai_query_success" and not value)
        self.log(
            {
                "event": "poll",
                "providers": sorted(enabled),
                "failed": failed,
                "seconds": round(time.monotonic() - began, 2),
            }
        )
        with self.lock:
            self.samples = out

    def metrics(self) -> str:
        with self.lock:
            counters: list[Sample] = [
                ("ayeops_ai_api_requests_total", labels(provider=name), float(count))
                for name, count in self.requests_total.items()
            ]
            counters += [
                ("ayeops_ai_query_errors_total", labels(provider=p, dataset=d), float(count))
                for (p, d), count in self.errors_total.items()
            ]
            return render([*self.samples, *counters])


def serve(exporter: Exporter, port: int, refresh_seconds: int) -> None:
    def refresh_forever() -> None:
        while True:
            exporter.refresh()
            time.sleep(refresh_seconds)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path == "/health":
                body = b"ok\n"
            elif self.path == "/metrics":
                body = exporter.metrics().encode()
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:
            return

    threading.Thread(target=refresh_forever, daemon=True).start()
    ThreadingHTTPServer(("", port), Handler).serve_forever()


def split_list(text: str) -> list[str]:
    return [item.strip() for item in text.split(",") if item.strip()]


def parse_budgets(text: str) -> dict[str, float]:
    data = json.loads(text or "{}")
    if not isinstance(data, Mapping):
        raise ValueError("AYE_AI_BUDGETS is not a JSON object")
    return {str(name): float(value) for name, value in data.items() if value is not None}


def parse_providers(names: Iterable[str], budgets: Mapping[str, float]) -> list[Provider]:
    found = []
    for name in names:
        if name not in PROVIDERS:
            raise ValueError(f"unknown provider {name!r}")
        found.append(Provider(name, budgets.get(name)))
    return found


def config_from(env: Mapping[str, str]) -> Config:
    providers = parse_providers(
        split_list(env.get("AYE_AI_PROVIDERS", "")), parse_budgets(env.get("AYE_AI_BUDGETS", ""))
    )
    return Config(
        providers=providers,
        spike_floor_usd=float(env.get("AYE_AI_SPIKE_FLOOR_USD", "1")),
        keys={p.name: env.get(KEY_ENV[p.name], "").strip() for p in providers},
    )


def clients_for(config: Config, get: Get = http_get) -> dict[str, Client]:
    return {p.name: Client(p.name, config.keys[p.name], get) for p in config.enabled()}


def main() -> None:
    env = os.environ
    config = config_from(env)
    refresh_seconds = int(env.get("AYE_AI_REFRESH_SECONDS", "900"))
    log_json(
        {
            "event": "start",
            "providers": [p.name for p in config.providers],
            "enabled": [p.name for p in config.enabled()],
            "refresh_seconds": refresh_seconds,
        }
    )
    serve(Exporter(config, clients_for(config)), int(env["AYE_AI_PORT"]), refresh_seconds)


if __name__ == "__main__":
    main()
