from __future__ import annotations

import base64
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

ROLE = "Viewer"
ACCOUNT = "aye-reader"
LOOP_SECONDS = 300
TIMEOUT = 15
PREFIX = "ayeops_grafana_reader_"
USER_AGENT = "ayeops-grafana-reader"
FILE_MODE = 0o440
ANNOTATION_LIMIT = 100
ENDPOINTS = {
    "rules": "/api/prometheus/grafana/api/v1/rules",
    "silences": "/api/alertmanager/grafana/api/v2/silences",
    "alerts": "/api/alertmanager/grafana/api/v2/alerts",
    "annotations": f"/api/annotations?type=alert&limit={ANNOTATION_LIMIT}",
}
FILTERED = ("rules", "annotations")
SEARCH_LIMIT = 5000

Http = Callable[[urllib.request.Request, float], tuple[int, bytes]]
Clock = Callable[[], float]
Writer = Callable[[Path, str], None]


class GrafanaError(Exception):
    pass


@dataclass(frozen=True)
class Seen:
    status: int
    count: int | None


@dataclass(frozen=True)
class Report:
    ok: bool
    endpoints: dict[str, bool]
    error: str
    checked: float


@dataclass
class State:
    report: Report = field(default_factory=lambda: Report(False, {}, "not checked yet", 0.0))
    last_success: float | None = None
    rotations: int = 0
    ready: bool = False
    unmanaged: int | None = None
    dashboards_error: str = ""


def basic(user: str, password: str) -> str:
    pair = f"{user}:{password}".encode()
    return "Basic " + base64.b64encode(pair).decode("ascii")


def bearer(token: str) -> str:
    return f"Bearer {token}"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


OPENER = urllib.request.build_opener(NoRedirect)


def urlopen_http(request: urllib.request.Request, timeout: float) -> tuple[int, bytes]:
    try:
        with OPENER.open(request, timeout=timeout) as response:
            return int(response.status), response.read()
    except urllib.error.HTTPError as error:
        return int(error.code), error.read()


@dataclass(frozen=True)
class Grafana:
    base: str
    http: Http

    def call(self, method: str, path: str, authorization: str, body: Any = None) -> tuple[int, Any]:
        data = None if body is None else json.dumps(body).encode("utf-8")
        headers = {
            "Authorization": authorization,
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        }
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            f"{self.base}{path}", data=data, method=method, headers=headers
        )
        status, raw = self.http(request, TIMEOUT)
        try:
            return status, json.loads(raw.decode("utf-8") or "null")
        except ValueError:
            return status, None

    def admin(self, method: str, path: str, authorization: str, body: Any = None) -> Any:
        status, data = self.call(method, path, authorization, body)
        if not 200 <= status < 300:
            raise GrafanaError(f"{method} {path.split('?')[0]} answered HTTP {status}")
        return data


def count_items(endpoint: str, data: Any) -> int:
    if endpoint == "rules":
        groups = data["data"]["groups"]
        return sum(len(group.get("rules") or []) for group in groups)
    return len(data)


def look(grafana: Grafana, authorization: str, endpoints: tuple[str, ...]) -> dict[str, Seen]:
    seen: dict[str, Seen] = {}
    for endpoint in endpoints:
        status, data = grafana.call("GET", ENDPOINTS[endpoint], authorization)
        count = count_items(endpoint, data) if status == 200 else None
        seen[endpoint] = Seen(status, count)
    return seen


def refused(seen: Mapping[str, Seen]) -> bool:
    return any(item.status == 401 for item in seen.values())


def judge(reader: Mapping[str, Seen], admin: Mapping[str, Seen]) -> tuple[dict[str, bool], str]:
    verdicts: dict[str, bool] = {}
    problems: list[str] = []
    for endpoint, item in reader.items():
        if item.status == 403:
            problems.append(f"{endpoint}: the {ROLE} role is not enough (HTTP 403)")
        elif item.status != 200:
            problems.append(f"{endpoint}: HTTP {item.status}")
        elif endpoint in admin and (item.count or 0) < (admin[endpoint].count or 0):
            problems.append(
                f"{endpoint}: the reader sees {item.count} of {admin[endpoint].count}, a folder"
                f" or dashboard does not grant {ROLE} view"
            )
        else:
            verdicts[endpoint] = True
            continue
        verdicts[endpoint] = False
    return verdicts, "; ".join(problems)


def dashboard_uids(grafana: Grafana, admin: str) -> list[str]:
    uids: list[str] = []
    page = 1
    while True:
        query = urllib.parse.urlencode({"type": "dash-db", "limit": SEARCH_LIMIT, "page": page})
        found = grafana.admin("GET", f"/api/search?{query}", admin)
        uids.extend(str(hit["uid"]) for hit in found)
        if len(found) < SEARCH_LIMIT:
            return uids
        page += 1


def count_unmanaged(grafana: Grafana, admin: str) -> int:
    count = 0
    for uid in dashboard_uids(grafana, admin):
        path = f"/api/dashboards/uid/{urllib.parse.quote(uid)}"
        data = grafana.admin("GET", path, admin)
        count += int(data["meta"].get("provisioned") is not True)
    return count


def find_account(grafana: Grafana, admin: str, name: str) -> dict[str, Any] | None:
    query = urllib.parse.urlencode({"query": name, "perpage": 100})
    found = grafana.admin("GET", f"/api/serviceaccounts/search?{query}", admin)
    for account in found.get("serviceAccounts") or []:
        if account.get("name") == name:
            return dict(account)
    return None


def ensure_account(grafana: Grafana, admin: str, name: str) -> int:
    account = find_account(grafana, admin, name)
    if account is None:
        body = {"name": name, "role": ROLE, "isDisabled": False}
        account = grafana.admin("POST", "/api/serviceaccounts", admin, body)
        log(f"created the service account {name} with role {ROLE}")
    elif account.get("role") != ROLE or account.get("isDisabled"):
        body = {"role": ROLE, "isDisabled": False}
        grafana.admin("PATCH", f"/api/serviceaccounts/{account['id']}", admin, body)
        log(f"reset the service account {name} to role {ROLE}, enabled")
    return int(account["id"])


def read_token(path: Path) -> str | None:
    try:
        token = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    return token or None


def write_token(path: Path, token: str) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}")
    temporary.unlink(missing_ok=True)
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, FILE_MODE)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(f"{token}\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(FILE_MODE)
        temporary.replace(path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def rotate(
    grafana: Grafana, admin: str, account: int, path: Path, writer: Writer, clock: Clock
) -> str:
    name = f"{ACCOUNT}-{int(clock())}"
    created = grafana.admin("POST", f"/api/serviceaccounts/{account}/tokens", admin, {"name": name})
    key = str(created["key"])
    writer(path, key)
    for token in grafana.admin("GET", f"/api/serviceaccounts/{account}/tokens", admin) or []:
        if token.get("id") != created.get("id"):
            grafana.admin("DELETE", f"/api/serviceaccounts/{account}/tokens/{token['id']}", admin)
    return key


@dataclass
class Reconciler:
    grafana: Grafana
    admin: str
    path: Path
    clock: Clock = time.time
    writer: Writer = write_token
    name: str = ACCOUNT
    state: State = field(default_factory=State)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def check(self) -> tuple[dict[str, bool], str, bool]:
        account = ensure_account(self.grafana, self.admin, self.name)
        token = read_token(self.path)
        reader = look(self.grafana, bearer(token), tuple(ENDPOINTS)) if token else None
        rotated = False
        if reader is None or refused(reader):
            why = "missing" if token is None else "refused by Grafana"
            token = rotate(self.grafana, self.admin, account, self.path, self.writer, self.clock)
            log(f"wrote a new reader token, the previous one was {why}")
            rotated = True
            reader = look(self.grafana, bearer(token), tuple(ENDPOINTS))
        baseline = look(self.grafana, self.admin, FILTERED)
        verdicts, problems = judge(reader, baseline)
        return verdicts, problems, rotated

    def refresh(self) -> Report:
        now = self.clock()
        try:
            verdicts, problems, rotated = self.check()
        except (GrafanaError, OSError, ValueError, KeyError, TypeError) as error:
            verdicts, problems, rotated = {}, str(error) or type(error).__name__, False
        ok = bool(verdicts) and all(verdicts.values()) and not problems
        report = Report(ok, verdicts, problems, now)
        with self.lock:
            previous = self.state.report
            self.state.report = report
            self.state.rotations += int(rotated)
            self.state.ready = self.state.ready or self.path.exists()
            if ok:
                self.state.last_success = now
        if report.error != previous.error:
            log(report.error if report.error else "the reader token reads every endpoint")
        self.count_dashboards()
        return report

    def count_dashboards(self) -> None:
        try:
            unmanaged: int | None = count_unmanaged(self.grafana, self.admin)
            problem = ""
        except (GrafanaError, OSError, ValueError, KeyError, TypeError, AttributeError) as error:
            unmanaged, problem = None, str(error) or type(error).__name__
        with self.lock:
            previous = self.state.dashboards_error
            self.state.unmanaged = unmanaged
            self.state.dashboards_error = problem
        if problem != previous:
            log(f"dashboards: {problem}" if problem else "dashboards: counted again")

    def metrics(self) -> str:
        with self.lock:
            state = State(
                self.state.report,
                self.state.last_success,
                self.state.rotations,
                unmanaged=self.state.unmanaged,
            )
        return metrics_text(state)

    def ready(self) -> bool:
        with self.lock:
            return self.state.ready


def metrics_text(state: State) -> str:
    report = state.report
    lines = [f"{PREFIX}ok {int(report.ok)}"]
    for endpoint in ENDPOINTS:
        value = int(report.endpoints.get(endpoint, False))
        lines.append(f'{PREFIX}endpoint_ok{{endpoint="{endpoint}"}} {value}')
    lines.append(f"{PREFIX}rotations_total {state.rotations}")
    if state.last_success is not None:
        lines.append(f"{PREFIX}last_success_timestamp_seconds {int(state.last_success)}")
    if state.unmanaged is not None:
        lines.append(f"{PREFIX}dashboards_unmanaged {state.unmanaged}")
    return "".join(f"{line}\n" for line in lines)


def log(message: str) -> None:
    print(f"grafana-reader: {message}", file=sys.stderr, flush=True)


def handler(reconciler: Reconciler) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path == "/health" and reconciler.ready():
                body = b"ok\n"
            elif self.path == "/metrics":
                body = reconciler.metrics().encode()
            else:
                self.send_error(503 if self.path == "/health" else 404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:
            return

    return Handler


def from_env(environ: Mapping[str, str], http: Http = urlopen_http) -> Reconciler:
    grafana = Grafana(environ.get("AYE_GRAFANA_URL", "http://grafana:3000").rstrip("/"), http)
    admin = basic(environ["AYE_GRAFANA_ADMIN_USER"], environ["AYE_GRAFANA_ADMIN_PASSWORD"])
    path = Path(environ.get("AYE_GRAFANA_READER_TOKEN_FILE", "/tmp/token"))
    return Reconciler(grafana, admin, path)


def main(environ: Mapping[str, str] = os.environ) -> None:
    reconciler = from_env(environ)
    interval = float(environ.get("AYE_GRAFANA_READER_INTERVAL", str(LOOP_SECONDS)))

    def refresh_forever() -> None:
        while True:
            reconciler.refresh()
            time.sleep(interval)

    threading.Thread(target=refresh_forever, daemon=True).start()
    port = int(environ.get("AYE_GRAFANA_READER_PORT", "9137"))
    ThreadingHTTPServer(("", port), handler(reconciler)).serve_forever()


if __name__ == "__main__":
    main()
