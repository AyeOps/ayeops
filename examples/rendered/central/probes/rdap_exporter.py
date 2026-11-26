from __future__ import annotations

import json
import os
import threading
import time
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

Fetch = Callable[[str], Any]
LOOKUP_ERRORS = (OSError, ValueError, KeyError, TypeError, IndexError)


def fetch_json(url: str) -> Any:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/rdap+json, application/json",
            "User-Agent": "ayeops-rdap-exporter",
        },
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.load(response)


def rdap_base(bootstrap: Mapping[str, Any], domain: str) -> str | None:
    labels = domain.lower().rstrip(".").split(".")
    best_length = 0
    best_url: str | None = None
    for suffixes, urls in bootstrap.get("services", []):
        secure = [url for url in urls if url.startswith("https://")] or list(urls)
        for suffix in suffixes:
            parts = suffix.lower().split(".")
            if len(parts) > best_length and labels[-len(parts) :] == parts and secure:
                best_length = len(parts)
                best_url = secure[0]
    return None if best_url is None else best_url.rstrip("/") + "/"


def expiry_timestamp(record: Mapping[str, Any]) -> float | None:
    for event in record.get("events", []):
        if event.get("eventAction") == "expiration" and event.get("eventDate"):
            moment = datetime.fromisoformat(str(event["eventDate"]).replace("Z", "+00:00"))
            return moment.timestamp()
    return None


def lookup(domains: Sequence[str], bootstrap_url: str, fetch: Fetch) -> dict[str, float | None]:
    try:
        bootstrap = fetch(bootstrap_url)
    except LOOKUP_ERRORS:
        return dict.fromkeys(domains)
    results: dict[str, float | None] = {}
    for domain in domains:
        base = rdap_base(bootstrap, domain)
        if base is None:
            results[domain] = None
            continue
        try:
            results[domain] = expiry_timestamp(fetch(f"{base}domain/{domain}"))
        except LOOKUP_ERRORS:
            results[domain] = None
    return results


def render_metrics(results: Mapping[str, float | None], now: float) -> str:
    lines: list[str] = []
    for domain in sorted(results):
        expiry = results[domain]
        label = f'domain="{domain}"'
        lines.append(f"ayeops_domain_rdap_success{{{label}}} {0 if expiry is None else 1}")
        if expiry is not None:
            lines.append(f"ayeops_domain_expiry_timestamp_seconds{{{label}}} {expiry:.0f}")
            lines.append(f"ayeops_domain_expiry_days{{{label}}} {(expiry - now) / 86400:.2f}")
    return "\n".join(lines) + "\n"


class Exporter:
    def __init__(self, domains: Sequence[str], bootstrap_url: str, fetch: Fetch = fetch_json):
        self.domains = list(domains)
        self.bootstrap_url = bootstrap_url
        self.fetch = fetch
        self.results: dict[str, float | None] = dict.fromkeys(self.domains)
        self.lock = threading.Lock()

    def refresh(self) -> None:
        results = lookup(self.domains, self.bootstrap_url, self.fetch)
        with self.lock:
            self.results = results

    def metrics(self, now: float) -> str:
        with self.lock:
            return render_metrics(self.results, now)


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
                body = exporter.metrics(time.time()).encode()
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


def main() -> None:
    domains = [d.strip() for d in os.environ.get("AYE_RDAP_DOMAINS", "").split(",") if d.strip()]
    exporter = Exporter(domains, os.environ["AYE_RDAP_BOOTSTRAP_URL"])
    serve(exporter, int(os.environ["AYE_RDAP_PORT"]), int(os.environ["AYE_RDAP_REFRESH_SECONDS"]))


if __name__ == "__main__":
    main()
