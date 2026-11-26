from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

API = "https://api.cloudflare.com/client/v4"
GRAPHQL = f"{API}/graphql"
QUERY_BUDGET_PER_5M = 300
ORIGIN_ERRORS = frozenset(range(520, 527))
R2_CLASS_A = frozenset(
    {
        "ListBuckets",
        "PutBucket",
        "ListObjects",
        "PutObject",
        "CopyObject",
        "CompleteMultipartUpload",
        "CreateMultipartUpload",
        "LifecycleStorageTierTransition",
        "ListMultipartUploads",
        "UploadPart",
        "UploadPartCopy",
        "ListParts",
        "PutBucketEncryption",
        "PutBucketCors",
        "PutBucketLifecycleConfiguration",
    }
)
R2_CLASS_B = frozenset(
    {
        "HeadBucket",
        "HeadObject",
        "GetObject",
        "UsageSummary",
        "GetBucketEncryption",
        "GetBucketLocation",
        "GetBucketCors",
        "GetBucketLifecycleConfiguration",
    }
)
R2_FREE = frozenset({"DeleteObject", "DeleteBucket", "AbortMultipartUpload"})
FREE_LIMITS: dict[str, float] = {
    "workers_requests_day": 100_000,
    "r2_class_a_month": 1_000_000,
    "r2_class_b_month": 10_000_000,
    "r2_storage_bytes": 10 * 10**9,
    "d1_rows_read_day": 5_000_000,
    "d1_rows_written_day": 100_000,
    "d1_storage_bytes": 5 * 10**9,
}
OTHER = "other"

Labels = tuple[tuple[str, str], ...]
Sample = tuple[str, Labels, float]
Post = Callable[[str, bytes, Mapping[str, str]], Any]
Get = Callable[[str, Mapping[str, str]], Any]
Log = Callable[[Mapping[str, Any]], None]

HELP = {
    "ayeops_cloudflare_requests": "Edge requests in the window by hostname, status class and cache",
    "ayeops_cloudflare_edge_bytes": "Bytes sent by the edge in the window",
    "ayeops_cloudflare_origin_errors": "Responses 520 to 526 in the window",
    "ayeops_cloudflare_country_requests": "Edge requests in the window by client country, top N",
    "ayeops_cloudflare_origin_response_seconds_avg": "Mean origin response time, uncached",
    "ayeops_cloudflare_origin_requests": "Requests that reached the origin in the window",
    "ayeops_cloudflare_window_seconds": "Length of the window the window gauges describe",
    "ayeops_cloudflare_window_end_timestamp_seconds": "End of that window",
    "ayeops_cloudflare_day_requests": "Edge requests of the UTC day so far",
    "ayeops_cloudflare_day_cached_requests": "Cached edge requests of the UTC day so far",
    "ayeops_cloudflare_day_bytes": "Edge bytes of the UTC day so far",
    "ayeops_cloudflare_day_cached_bytes": "Cached edge bytes of the UTC day so far",
    "ayeops_cloudflare_day_threats": "Threats of the UTC day so far",
    "ayeops_cloudflare_zone_info": "Zone id, account and plan, always 1",
    "ayeops_cloudflare_workers_requests_day": "Worker invocations of the UTC day so far",
    "ayeops_cloudflare_workers_errors_day": "Worker errors of the UTC day so far",
    "ayeops_cloudflare_workers_cpu_seconds": "Worker CPU time per invocation of the UTC day",
    "ayeops_cloudflare_r2_operations_month": "R2 operations of the UTC month so far by class",
    "ayeops_cloudflare_r2_storage_bytes": "R2 payload and metadata bytes",
    "ayeops_cloudflare_r2_objects": "R2 object count",
    "ayeops_cloudflare_d1_rows_read_day": "D1 rows read of the UTC day so far",
    "ayeops_cloudflare_d1_rows_written_day": "D1 rows written of the UTC day so far",
    "ayeops_cloudflare_d1_queries_day": "D1 queries of the UTC day so far",
    "ayeops_cloudflare_d1_storage_bytes": "D1 database size",
    "ayeops_cloudflare_usage": "Usage of an account resource that has a free limit",
    "ayeops_cloudflare_free_limit": "The free limit of that resource",
    "ayeops_cloudflare_query_success": "1 when the last query of the dataset answered",
    "ayeops_cloudflare_dataset_available": "1 when the plan and token allow the dataset",
    "ayeops_cloudflare_poll_timestamp_seconds": "When the last poll finished",
    "ayeops_cloudflare_graphql_queries_total": "GraphQL queries sent, the unit of the API budget",
    "ayeops_cloudflare_query_errors_total": "GraphQL queries that failed",
}
COUNTERS = frozenset(
    {"ayeops_cloudflare_graphql_queries_total", "ayeops_cloudflare_query_errors_total"}
)


class CloudflareError(Exception):
    def __init__(self, message: str, code: str = "", status: int = 0):
        super().__init__(message)
        self.code = code
        self.status = status

    @property
    def denied(self) -> bool:
        return self.code == "authz" or self.status in (401, 403)


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


def iso(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def window(now: datetime, seconds: int, lag: int) -> tuple[datetime, datetime]:
    end = now.astimezone(UTC) - timedelta(seconds=lag)
    end = end.replace(second=0, microsecond=0)
    return end - timedelta(seconds=seconds), end


def day_start(now: datetime) -> datetime:
    return now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)


def month_start(now: datetime) -> datetime:
    return day_start(now).replace(day=1)


def status_class(status: Any) -> str:
    try:
        code = int(status)
    except (TypeError, ValueError):
        return OTHER
    if 100 <= code <= 599:
        return f"{code // 100}xx"
    return OTHER


def r2_class(action: str) -> str:
    if action in R2_CLASS_A:
        return "a"
    if action in R2_CLASS_B:
        return "b"
    if action in R2_FREE:
        return "free"
    return "unknown"


def number(value: Any) -> float:
    if value is None:
        return 0.0
    return float(value)


def top_names(counts: Mapping[str, float], keep: int) -> set[str]:
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return {name for name, _ in ranked[:keep]}


def nodes(data: Any, scope: str, dataset: str) -> list[dict[str, Any]]:
    if not isinstance(data, Mapping):
        raise CloudflareError("response has no data")
    targets = (data.get("viewer") or {}).get(scope) or []
    if not targets:
        raise CloudflareError(f"no {scope} in the response, check the token scope")
    rows = targets[0].get(dataset)
    if rows is None:
        raise CloudflareError(f"no {dataset} in the response")
    return list(rows)


def window_samples(rows: Sequence[Mapping[str, Any]], zone: str, top_hosts: int) -> list[Sample]:
    per_host: dict[str, float] = {}
    for row in rows:
        host = str(row["dimensions"].get("clientRequestHTTPHost") or "")
        per_host[host] = per_host.get(host, 0.0) + number(row.get("count"))
    kept = top_names(per_host, top_hosts)
    requests: dict[Labels, float] = {}
    sent: dict[Labels, float] = {}
    errors: dict[Labels, float] = {}
    for row in rows:
        dims = row["dimensions"]
        host = str(dims.get("clientRequestHTTPHost") or "")
        host = host if host in kept else OTHER
        cache = str(dims.get("cacheStatus") or "none")
        count = number(row.get("count"))
        status = dims.get("edgeResponseStatus")
        key = labels(
            zone=zone, hostname=host, status_class=status_class(status), cache_status=cache
        )
        requests[key] = requests.get(key, 0.0) + count
        bytes_key = labels(zone=zone, hostname=host, cache_status=cache)
        sent[bytes_key] = sent.get(bytes_key, 0.0) + number(
            (row.get("sum") or {}).get("edgeResponseBytes")
        )
        if isinstance(status, int) and status in ORIGIN_ERRORS:
            error_key = labels(zone=zone, hostname=host, status=str(status))
            errors[error_key] = errors.get(error_key, 0.0) + count
    samples: list[Sample] = [("ayeops_cloudflare_requests", k, v) for k, v in requests.items()]
    samples += [("ayeops_cloudflare_edge_bytes", k, v) for k, v in sent.items()]
    samples += [("ayeops_cloudflare_origin_errors", k, v) for k, v in errors.items()]
    return samples


def country_samples(
    rows: Sequence[Mapping[str, Any]], zone: str, total: float, top: int
) -> list[Sample]:
    counts: dict[str, float] = {}
    for row in rows:
        country = str(row["dimensions"].get("clientCountryName") or "XX")
        counts[country] = counts.get(country, 0.0) + number(row.get("count"))
    kept = top_names(counts, top)
    samples: list[Sample] = [
        ("ayeops_cloudflare_country_requests", labels(zone=zone, country=name), counts[name])
        for name in kept
    ]
    rest = total - sum(counts[name] for name in kept)
    if rest > 0:
        samples.append(
            ("ayeops_cloudflare_country_requests", labels(zone=zone, country=OTHER), rest)
        )
    return samples


def origin_samples(rows: Sequence[Mapping[str, Any]], zone: str, top_hosts: int) -> list[Sample]:
    per_host: dict[str, tuple[float, float]] = {}
    for row in rows:
        host = str(row["dimensions"].get("clientRequestHTTPHost") or "")
        count = number(row.get("count"))
        millis = number((row.get("avg") or {}).get("originResponseDurationMs"))
        weight, total = per_host.get(host, (0.0, 0.0))
        per_host[host] = (weight + count, total + count * millis)
    kept = top_names({host: pair[0] for host, pair in per_host.items()}, top_hosts)
    merged: dict[str, tuple[float, float]] = {}
    for host, (count, total) in per_host.items():
        name = host if host in kept else OTHER
        weight, sum_millis = merged.get(name, (0.0, 0.0))
        merged[name] = (weight + count, sum_millis + total)
    samples: list[Sample] = []
    for host, (count, total) in merged.items():
        key = labels(zone=zone, hostname=host)
        samples.append(("ayeops_cloudflare_origin_requests", key, count))
        if count > 0:
            samples.append(
                ("ayeops_cloudflare_origin_response_seconds_avg", key, total / count / 1000)
            )
    return samples


DAY_FIELDS = {
    "requests": "ayeops_cloudflare_day_requests",
    "cachedRequests": "ayeops_cloudflare_day_cached_requests",
    "bytes": "ayeops_cloudflare_day_bytes",
    "cachedBytes": "ayeops_cloudflare_day_cached_bytes",
    "threats": "ayeops_cloudflare_day_threats",
}


def day_samples(rows: Sequence[Mapping[str, Any]], zone: str) -> list[Sample]:
    totals = dict.fromkeys(DAY_FIELDS, 0.0)
    for row in rows:
        for name in DAY_FIELDS:
            totals[name] += number((row.get("sum") or {}).get(name))
    return [(DAY_FIELDS[name], labels(zone=zone), value) for name, value in totals.items()]


def workers_samples(rows: Sequence[Mapping[str, Any]], account: str) -> list[Sample]:
    samples: list[Sample] = []
    total = 0.0
    for row in rows:
        script = str(row["dimensions"].get("scriptName") or "")
        key = labels(account=account, script=script)
        requests = number((row.get("sum") or {}).get("requests"))
        total += requests
        samples.append(("ayeops_cloudflare_workers_requests_day", key, requests))
        samples.append(
            (
                "ayeops_cloudflare_workers_errors_day",
                key,
                number((row.get("sum") or {}).get("errors")),
            )
        )
        quantiles = row.get("quantiles") or {}
        for field_name, quantile in (("cpuTimeP50", "0.5"), ("cpuTimeP99", "0.99")):
            if quantiles.get(field_name) is not None:
                samples.append(
                    (
                        "ayeops_cloudflare_workers_cpu_seconds",
                        labels(account=account, script=script, quantile=quantile),
                        number(quantiles[field_name]) / 1_000_000,
                    )
                )
    samples.append(usage(account, "workers_requests_day", total))
    return samples


def usage(account: str, resource: str, value: float) -> Sample:
    return ("ayeops_cloudflare_usage", labels(account=account, resource=resource), value)


def r2_operation_samples(rows: Sequence[Mapping[str, Any]], account: str) -> list[Sample]:
    per_bucket: dict[Labels, float] = {}
    per_class = {"a": 0.0, "b": 0.0}
    for row in rows:
        dims = row["dimensions"]
        kind = r2_class(str(dims.get("actionType") or ""))
        count = number((row.get("sum") or {}).get("requests"))
        key = labels(account=account, bucket=str(dims.get("bucketName") or ""), **{"class": kind})
        per_bucket[key] = per_bucket.get(key, 0.0) + count
        if kind in per_class:
            per_class[kind] += count
    samples: list[Sample] = [
        ("ayeops_cloudflare_r2_operations_month", key, value) for key, value in per_bucket.items()
    ]
    samples.append(usage(account, "r2_class_a_month", per_class["a"]))
    samples.append(usage(account, "r2_class_b_month", per_class["b"]))
    return samples


def latest_per(
    rows: Sequence[Mapping[str, Any]], key: str, moment: str
) -> dict[str, Mapping[str, Any]]:
    latest: dict[str, Mapping[str, Any]] = {}
    for row in rows:
        dims = row["dimensions"]
        name = str(dims.get(key) or "")
        if name not in latest or str(dims.get(moment)) > str(
            latest[name]["dimensions"].get(moment)
        ):
            latest[name] = row
    return latest


def r2_storage_samples(rows: Sequence[Mapping[str, Any]], account: str) -> list[Sample]:
    samples: list[Sample] = []
    total = 0.0
    for bucket, row in sorted(latest_per(rows, "bucketName", "datetime").items()):
        peak = row.get("max") or {}
        size = number(peak.get("payloadSize")) + number(peak.get("metadataSize"))
        total += size
        key = labels(account=account, bucket=bucket)
        samples.append(("ayeops_cloudflare_r2_storage_bytes", key, size))
        samples.append(("ayeops_cloudflare_r2_objects", key, number(peak.get("objectCount"))))
    samples.append(usage(account, "r2_storage_bytes", total))
    return samples


def d1_samples(rows: Sequence[Mapping[str, Any]], account: str) -> list[Sample]:
    samples: list[Sample] = []
    read = written = 0.0
    for row in rows:
        sums = row.get("sum") or {}
        key = labels(account=account, database=str(row["dimensions"].get("databaseId") or ""))
        rows_read = number(sums.get("rowsRead"))
        rows_written = number(sums.get("rowsWritten"))
        read += rows_read
        written += rows_written
        samples.append(("ayeops_cloudflare_d1_rows_read_day", key, rows_read))
        samples.append(("ayeops_cloudflare_d1_rows_written_day", key, rows_written))
        queries = number(sums.get("readQueries")) + number(sums.get("writeQueries"))
        samples.append(("ayeops_cloudflare_d1_queries_day", key, queries))
    samples.append(usage(account, "d1_rows_read_day", read))
    samples.append(usage(account, "d1_rows_written_day", written))
    return samples


def d1_storage_samples(rows: Sequence[Mapping[str, Any]], account: str) -> list[Sample]:
    samples: list[Sample] = []
    total = 0.0
    for database, row in sorted(latest_per(rows, "databaseId", "date").items()):
        size = number((row.get("max") or {}).get("databaseSizeBytes"))
        total += size
        samples.append(
            ("ayeops_cloudflare_d1_storage_bytes", labels(account=account, database=database), size)
        )
    samples.append(usage(account, "d1_storage_bytes", total))
    return samples


def limit_samples(account: str, limits: Mapping[str, float]) -> list[Sample]:
    return [
        ("ayeops_cloudflare_free_limit", labels(account=account, resource=name), value)
        for name, value in sorted(limits.items())
    ]


def parse_limits(text: str) -> dict[str, float]:
    limits = dict(FREE_LIMITS)
    overrides = json.loads(text) if text.strip() else {}
    if not isinstance(overrides, Mapping):
        raise ValueError("AYE_CLOUDFLARE_LIMITS must be a JSON object")
    for name, value in overrides.items():
        if name not in FREE_LIMITS:
            raise ValueError(f"unknown limit {name}")
        limits[name] = float(value)
    return limits


def split_list(text: str) -> list[str]:
    return [item.strip() for item in text.split(",") if item.strip()]


ZONE_WINDOW = """query ($zone: string!, $start: string!, $end: string!) {
  viewer { zones(filter: {zoneTag: $zone}) {
    httpRequestsAdaptiveGroups(limit: 10000,
      filter: {datetime_geq: $start, datetime_lt: $end, requestSource: "eyeball"}) {
      count
      sum { edgeResponseBytes }
      dimensions { clientRequestHTTPHost edgeResponseStatus cacheStatus }
    }
  } }
}"""
ZONE_COUNTRIES = """query ($zone: string!, $start: string!, $end: string!) {
  viewer { zones(filter: {zoneTag: $zone}) {
    httpRequestsAdaptiveGroups(limit: %d, orderBy: [count_DESC],
      filter: {datetime_geq: $start, datetime_lt: $end, requestSource: "eyeball"}) {
      count
      dimensions { clientCountryName }
    }
  } }
}"""
ZONE_ORIGIN = """query ($zone: string!, $start: string!, $end: string!) {
  viewer { zones(filter: {zoneTag: $zone}) {
    httpRequestsAdaptiveGroups(limit: 10000,
      filter: {datetime_geq: $start, datetime_lt: $end, requestSource: "eyeball",
        originResponseDurationMs_gt: 0}) {
      count
      avg { originResponseDurationMs }
      dimensions { clientRequestHTTPHost }
    }
  } }
}"""
ZONE_DAY = """query ($zone: string!, $date: Date!) {
  viewer { zones(filter: {zoneTag: $zone}) {
    httpRequests1dGroups(limit: 1, filter: {date: $date}) {
      sum { requests cachedRequests bytes cachedBytes threats }
    }
  } }
}"""
WORKERS = """query ($account: string!, $start: string!, $end: string!) {
  viewer { accounts(filter: {accountTag: $account}) {
    workersInvocationsAdaptive(limit: 10000, filter: {datetime_geq: $start, datetime_lt: $end}) {
      sum { requests errors }
      quantiles { cpuTimeP50 cpuTimeP99 }
      dimensions { scriptName }
    }
  } }
}"""
R2_OPERATIONS = """query ($account: string!, $start: string!, $end: string!) {
  viewer { accounts(filter: {accountTag: $account}) {
    r2OperationsAdaptiveGroups(limit: 10000, filter: {datetime_geq: $start, datetime_lt: $end}) {
      sum { requests }
      dimensions { actionType bucketName }
    }
  } }
}"""
R2_STORAGE = """query ($account: string!, $start: string!, $end: string!) {
  viewer { accounts(filter: {accountTag: $account}) {
    r2StorageAdaptiveGroups(limit: 10000, orderBy: [datetime_DESC],
      filter: {datetime_geq: $start, datetime_lt: $end}) {
      max { payloadSize metadataSize objectCount }
      dimensions { bucketName datetime }
    }
  } }
}"""
D1 = """query ($account: string!, $date: Date!) {
  viewer { accounts(filter: {accountTag: $account}) {
    d1AnalyticsAdaptiveGroups(limit: 10000, filter: {date_geq: $date, date_leq: $date}) {
      sum { readQueries writeQueries rowsRead rowsWritten }
      dimensions { databaseId }
    }
  } }
}"""
D1_STORAGE = """query ($account: string!, $since: Date!, $date: Date!) {
  viewer { accounts(filter: {accountTag: $account}) {
    d1StorageAdaptiveGroups(limit: 10000, orderBy: [date_DESC],
      filter: {date_geq: $since, date_leq: $date}) {
      max { databaseSizeBytes }
      dimensions { databaseId date }
    }
  } }
}"""
OPTIONAL = frozenset({"origin"})


@dataclass(frozen=True)
class Zone:
    name: str
    zone_id: str
    account: str
    plan: str


@dataclass
class Config:
    zones: list[str]
    accounts: list[str]
    window_seconds: int = 900
    lag_seconds: int = 180
    top_hosts: int = 20
    top_countries: int = 10
    limits: dict[str, float] = field(default_factory=lambda: dict(FREE_LIMITS))

    def queries_per_poll(self) -> int:
        return len(self.zones) * 4 + len(self.accounts) * 5


def budget_per_5m(queries_per_poll: int, refresh_seconds: int) -> float:
    return queries_per_poll * 300 / refresh_seconds


def http_post(url: str, body: bytes, headers: Mapping[str, str]) -> Any:
    request = urllib.request.Request(url, data=body, headers=dict(headers), method="POST")
    return _open(request)


def http_get(url: str, headers: Mapping[str, str]) -> Any:
    return _open(urllib.request.Request(url, headers=dict(headers)))


def _open(request: urllib.request.Request) -> Any:
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        raise CloudflareError(f"HTTP {error.code}", status=error.code) from None


def graphql_error(answer: Any) -> CloudflareError | None:
    if not isinstance(answer, Mapping):
        return CloudflareError("response is not a JSON object")
    errors = answer.get("errors") or []
    if not errors:
        return None
    first = errors[0] if isinstance(errors[0], Mapping) else {}
    code = str((first.get("extensions") or {}).get("code") or "")
    return CloudflareError(str(first.get("message") or "GraphQL error")[:300], code=code)


def log_json(record: Mapping[str, Any]) -> None:
    print(json.dumps(record, sort_keys=True), flush=True)


class Client:
    def __init__(self, token: str, post: Post = http_post, get: Get = http_get):
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "ayeops-cloudflare-exporter",
        }
        self._post = post
        self._get = get
        self.sent = 0

    def query(self, text: str, variables: Mapping[str, Any]) -> Any:
        self.sent += 1
        body = json.dumps({"query": text, "variables": dict(variables)}).encode()
        answer = self._post(GRAPHQL, body, self._headers)
        error = graphql_error(answer)
        if error is not None:
            raise error
        return answer["data"]

    def zone(self, name: str) -> Zone:
        answer = self._get(f"{API}/zones?name={urllib.parse.quote(name)}", self._headers)
        if not isinstance(answer, Mapping):
            raise CloudflareError("zone lookup answered with no JSON object")
        results = answer.get("result")
        if not answer.get("success") or not results:
            raise CloudflareError(f"zone {name} is not visible to the token")
        found = results[0]
        return Zone(
            name=name,
            zone_id=str(found["id"]),
            account=str((found.get("account") or {}).get("id") or ""),
            plan=str((found.get("plan") or {}).get("legacy_id") or ""),
        )


class Exporter:
    def __init__(self, config: Config, client: Client, log: Log = log_json):
        self.config = config
        self.client = client
        self.log = log
        self.zones: dict[str, Zone] = {}
        self.samples: list[Sample] = []
        self.queries_total = 0
        self.errors_total: dict[str, int] = {}
        self.lock = threading.Lock()

    def run(
        self,
        out: list[Sample],
        target: str,
        dataset: str,
        query: str,
        variables: Mapping[str, Any],
        parse: Callable[[Any], list[Sample]],
    ) -> bool:
        key = labels(target=target, dataset=dataset)
        try:
            produced = parse(self.client.query(query, variables))
        except (CloudflareError, OSError, ValueError, KeyError, TypeError, IndexError) as error:
            self.errors_total[dataset] = self.errors_total.get(dataset, 0) + 1
            denied = isinstance(error, CloudflareError) and error.denied
            self.log(
                {
                    "event": "query_failed",
                    "target": target,
                    "dataset": dataset,
                    "denied": denied,
                    "error": str(error)[:300],
                }
            )
            if denied:
                out.append(("ayeops_cloudflare_dataset_available", key, 0))
            if dataset not in OPTIONAL:
                out.append(("ayeops_cloudflare_query_success", key, 0))
            return False
        out.extend(produced)
        out.append(("ayeops_cloudflare_dataset_available", key, 1))
        if dataset not in OPTIONAL:
            out.append(("ayeops_cloudflare_query_success", key, 1))
        return True

    def resolve(self, out: list[Sample]) -> None:
        for name in self.config.zones:
            if name in self.zones:
                continue
            key = labels(target=name, dataset="zone")
            try:
                self.zones[name] = self.client.zone(name)
            except (CloudflareError, OSError, ValueError, KeyError, TypeError) as error:
                self.log({"event": "zone_lookup_failed", "zone": name, "error": str(error)[:300]})
                out.append(("ayeops_cloudflare_query_success", key, 0))
                continue
            out.append(("ayeops_cloudflare_query_success", key, 1))

    def collect_zone(self, out: list[Sample], zone: Zone, now: datetime) -> None:
        config = self.config
        start, end = window(now, config.window_seconds, config.lag_seconds)
        span = {"zone": zone.zone_id, "start": iso(start), "end": iso(end)}
        info = labels(zone=zone.name, zone_id=zone.zone_id, account=zone.account, plan=zone.plan)
        out.append(("ayeops_cloudflare_zone_info", info, 1))
        totals: list[float] = []

        def parse_window(data: Any) -> list[Sample]:
            rows = nodes(data, "zones", "httpRequestsAdaptiveGroups")
            totals.append(sum(number(row.get("count")) for row in rows))
            return window_samples(rows, zone.name, config.top_hosts)

        if self.run(out, zone.name, "requests", ZONE_WINDOW, span, parse_window):
            out.append(
                ("ayeops_cloudflare_window_seconds", labels(zone=zone.name), config.window_seconds)
            )
            out.append(
                (
                    "ayeops_cloudflare_window_end_timestamp_seconds",
                    labels(zone=zone.name),
                    end.timestamp(),
                )
            )
            self.run(
                out,
                zone.name,
                "countries",
                ZONE_COUNTRIES % config.top_countries,
                span,
                lambda data: country_samples(
                    nodes(data, "zones", "httpRequestsAdaptiveGroups"),
                    zone.name,
                    totals[0],
                    config.top_countries,
                ),
            )
        self.run(
            out,
            zone.name,
            "origin",
            ZONE_ORIGIN,
            span,
            lambda data: origin_samples(
                nodes(data, "zones", "httpRequestsAdaptiveGroups"), zone.name, config.top_hosts
            ),
        )
        self.run(
            out,
            zone.name,
            "day",
            ZONE_DAY,
            {"zone": zone.zone_id, "date": day_start(now).date().isoformat()},
            lambda data: day_samples(nodes(data, "zones", "httpRequests1dGroups"), zone.name),
        )

    def collect_account(self, out: list[Sample], account: str, now: datetime) -> None:
        _, end = window(now, 0, self.config.lag_seconds)
        today = day_start(now).date()
        day_span = {"account": account, "start": iso(day_start(now)), "end": iso(end)}
        month_span = {"account": account, "start": iso(month_start(now)), "end": iso(end)}
        storage_span = {"account": account, "start": iso(end - timedelta(days=1)), "end": iso(end)}
        out.extend(limit_samples(account, self.config.limits))
        self.run(
            out,
            account,
            "workers",
            WORKERS,
            day_span,
            lambda data: workers_samples(
                nodes(data, "accounts", "workersInvocationsAdaptive"), account
            ),
        )
        self.run(
            out,
            account,
            "r2_operations",
            R2_OPERATIONS,
            month_span,
            lambda data: r2_operation_samples(
                nodes(data, "accounts", "r2OperationsAdaptiveGroups"), account
            ),
        )
        self.run(
            out,
            account,
            "r2_storage",
            R2_STORAGE,
            storage_span,
            lambda data: r2_storage_samples(
                nodes(data, "accounts", "r2StorageAdaptiveGroups"), account
            ),
        )
        self.run(
            out,
            account,
            "d1",
            D1,
            {"account": account, "date": today.isoformat()},
            lambda data: d1_samples(nodes(data, "accounts", "d1AnalyticsAdaptiveGroups"), account),
        )
        self.run(
            out,
            account,
            "d1_storage",
            D1_STORAGE,
            {
                "account": account,
                "since": (today - timedelta(days=1)).isoformat(),
                "date": today.isoformat(),
            },
            lambda data: d1_storage_samples(
                nodes(data, "accounts", "d1StorageAdaptiveGroups"), account
            ),
        )

    def refresh(self, now: datetime | None = None) -> None:
        moment = now or datetime.now(UTC)
        began = time.monotonic()
        sent_before = self.client.sent
        out: list[Sample] = []
        self.resolve(out)
        for name in self.config.zones:
            if name in self.zones:
                self.collect_zone(out, self.zones[name], moment)
        for account in self.config.accounts:
            self.collect_account(out, account, moment)
        sent = self.client.sent - sent_before
        out.append(("ayeops_cloudflare_poll_timestamp_seconds", (), moment.timestamp()))
        failed = sum(
            1 for name, _, value in out if name == "ayeops_cloudflare_query_success" and not value
        )
        self.log(
            {
                "event": "poll",
                "queries": sent,
                "failed": failed,
                "seconds": round(time.monotonic() - began, 2),
            }
        )
        with self.lock:
            self.queries_total += sent
            self.samples = out

    def metrics(self) -> str:
        with self.lock:
            counters: list[Sample] = [
                ("ayeops_cloudflare_graphql_queries_total", (), float(self.queries_total))
            ]
            counters += [
                ("ayeops_cloudflare_query_errors_total", labels(dataset=name), float(count))
                for name, count in self.errors_total.items()
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


def config_from(env: Mapping[str, str]) -> Config:
    return Config(
        zones=split_list(env.get("AYE_CLOUDFLARE_ZONES", "")),
        accounts=split_list(env.get("AYE_CLOUDFLARE_ACCOUNTS", "")),
        window_seconds=int(env.get("AYE_CLOUDFLARE_WINDOW_SECONDS", "900")),
        lag_seconds=int(env.get("AYE_CLOUDFLARE_LAG_SECONDS", "180")),
        top_hosts=int(env.get("AYE_CLOUDFLARE_TOP_HOSTS", "20")),
        top_countries=int(env.get("AYE_CLOUDFLARE_TOP_COUNTRIES", "10")),
        limits=parse_limits(env.get("AYE_CLOUDFLARE_LIMITS", "")),
    )


def main() -> None:
    env = os.environ
    config = config_from(env)
    refresh_seconds = int(env.get("AYE_CLOUDFLARE_REFRESH_SECONDS", "300"))
    log_json(
        {
            "event": "start",
            "zones": config.zones,
            "accounts": config.accounts,
            "queries_per_poll": config.queries_per_poll(),
            "queries_per_5m": budget_per_5m(config.queries_per_poll(), refresh_seconds),
            "budget_per_5m": QUERY_BUDGET_PER_5M,
        }
    )
    client = Client(env["AYE_CLOUDFLARE_ANALYTICS_TOKEN"])
    serve(Exporter(config, client), int(env["AYE_CLOUDFLARE_PORT"]), refresh_seconds)


if __name__ == "__main__":
    main()
