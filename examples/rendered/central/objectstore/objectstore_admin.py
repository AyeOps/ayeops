from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

LOOP_SECONDS = 300
TIMEOUT = 30
PREFIX = "ayeops_objectstore_"

Fetch = Callable[[str, str, bytes | None], Any]


@dataclass(frozen=True)
class Bucket:
    id: str
    bytes: int
    objects: int
    max_size: int | None
    max_objects: int | None


@dataclass(frozen=True)
class Report:
    bucket: str
    ok: bool
    info: Bucket | None
    error: str
    checked: float


def parse_bucket(data: Mapping[str, Any]) -> Bucket:
    quotas = data.get("quotas") or {}
    max_size = quotas.get("maxSize")
    max_objects = quotas.get("maxObjects")
    return Bucket(
        id=str(data["id"]),
        bytes=int(data.get("bytes") or 0),
        objects=int(data.get("objects") or 0),
        max_size=None if max_size is None else int(max_size),
        max_objects=None if max_objects is None else int(max_objects),
    )


def quota_change(bucket: Bucket, quota: int) -> dict[str, Any] | None:
    if bucket.max_size == quota:
        return None
    return {"quotas": {"maxSize": quota, "maxObjects": bucket.max_objects}}


def label(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def metrics_text(report: Report) -> str:
    where = f'{{bucket="{label(report.bucket)}"}}'
    lines = [f"{PREFIX}admin_ok{where} {1 if report.ok else 0}"]
    info = report.info
    if info is not None:
        lines.append(f"{PREFIX}bucket_bytes{where} {info.bytes}")
        lines.append(f"{PREFIX}bucket_objects{where} {info.objects}")
        if info.max_size is not None:
            lines.append(f"{PREFIX}bucket_quota_bytes{where} {info.max_size}")
    if report.ok:
        lines.append(f"{PREFIX}admin_last_success_timestamp_seconds{where} {int(report.checked)}")
    return "".join(f"{line}\n" for line in lines)


@dataclass(frozen=True)
class Admin:
    url: str
    token: str

    def fetch(self, method: str, path: str, body: bytes | None) -> Any:
        request = urllib.request.Request(
            f"{self.url}{path}",
            data=body,
            method=method,
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return json.loads(response.read().decode("utf-8") or "null")


def reconcile(fetch: Fetch, bucket: str, quota: int, clock: Callable[[], float]) -> Report:
    query = urllib.parse.urlencode({"globalAlias": bucket})
    try:
        info = parse_bucket(fetch("GET", f"/v2/GetBucketInfo?{query}", None))
        change = quota_change(info, quota)
        if change is not None:
            target = urllib.parse.urlencode({"id": info.id})
            fetch("POST", f"/v2/UpdateBucket?{target}", json.dumps(change).encode("utf-8"))
            info = parse_bucket(fetch("GET", f"/v2/GetBucketInfo?{query}", None))
            log(f"set the quota of {bucket} to {quota} bytes")
    except (OSError, ValueError, KeyError, TypeError, urllib.error.URLError) as error:
        log(f"{bucket}: {error}")
        return Report(bucket, False, None, str(error), clock())
    if info.max_size != quota:
        return Report(bucket, False, info, "the quota did not apply", clock())
    return Report(bucket, True, info, "", clock())


def log(message: str) -> None:
    print(f"objectstore-admin: {message}", file=sys.stderr, flush=True)


class Reconciler:
    def __init__(self, fetch: Fetch, bucket: str, quota: int) -> None:
        self.fetch = fetch
        self.bucket = bucket
        self.quota = quota
        self.lock = threading.Lock()
        self.report = Report(bucket, False, None, "not checked yet", 0.0)

    def refresh(self) -> Report:
        report = reconcile(self.fetch, self.bucket, self.quota, time.time)
        with self.lock:
            self.report = report
        return report

    def metrics(self) -> str:
        with self.lock:
            return metrics_text(self.report)


def handler(reconciler: Reconciler) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path == "/health":
                body = b"ok\n"
            elif self.path == "/metrics":
                body = reconciler.metrics().encode()
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

    return Handler


def main(environ: Mapping[str, str] = os.environ) -> None:
    admin = Admin(
        environ.get("AYE_GARAGE_ADMIN_URL", "http://garage:3903"), environ["GARAGE_ADMIN_TOKEN"]
    )
    reconciler = Reconciler(
        admin.fetch, environ["AYE_OBJECTSTORE_BUCKET"], int(environ["AYE_OBJECTSTORE_QUOTA_BYTES"])
    )

    def refresh_forever() -> None:
        while True:
            reconciler.refresh()
            time.sleep(LOOP_SECONDS)

    threading.Thread(target=refresh_forever, daemon=True).start()
    port = int(environ.get("AYE_OBJECTSTORE_ADMIN_PORT", "9132"))
    ThreadingHTTPServer(("", port), handler(reconciler)).serve_forever()


if __name__ == "__main__":
    main()
