from __future__ import annotations

import io
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

API = "https://api.github.com"
API_VERSION = "2022-11-28"
INCLUDED_SKUS = {"actions linux": 1.0, "actions windows": 2.0, "actions macos": 10.0}
FREE_VISIBILITY = "public"
VISIBILITY_TTL = 86400.0
REPORT_PREFIX = "aye-report"
REPORT_METRIC = re.compile(r"^aye_[a-z0-9_]{1,80}$")
REPORT_LINE = re.compile(r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{(.*)\})? (\S+) (\d{10,16})$")
REPORT_LABEL = re.compile(r'\s*([a-zA-Z_][a-zA-Z0-9_]*)="((?:[^"\\]|\\.)*)"\s*(?:,|$)')
MAX_ARTIFACT_BYTES = 4 * 1024 * 1024
MAX_REPORT_LINES = 20000
FUTURE_SLACK_MS = 3600 * 1000

Labels = tuple[tuple[str, str], ...]
Sample = tuple[str, Labels, float]
Log = Callable[[Mapping[str, Any]], None]

HELP = {
    "ayeops_github_usage": "Billed usage this month by product, SKU, repository and unit",
    "ayeops_github_usage_gross_dollars": "Gross amount of this month's usage before discounts",
    "ayeops_github_usage_net_dollars": "Net amount of this month's usage after included quota",
    "ayeops_github_included_minutes": "Actions minutes the plan includes per month",
    "ayeops_github_included_minutes_used": "Actions minutes of private repositories this month,"
    " weighted by the runner multiplier",
    "ayeops_github_query_success": "Whether the last GitHub API query succeeded",
    "ayeops_github_poll_timestamp_seconds": "Time of the last poll",
    "ayeops_github_reports_imported_total": "aye report artifacts imported into storage",
    "ayeops_github_report_lines_rejected_total": "Lines of report artifacts that were rejected",
    "ayeops_github_report_import_success": "Whether the last report import of a repository worked",
    "ayeops_github_api_requests_total": "Requests sent to the GitHub API",
}
COUNTERS = frozenset(
    {
        "ayeops_github_reports_imported_total",
        "ayeops_github_report_lines_rejected_total",
        "ayeops_github_api_requests_total",
    }
)


class GitHubError(Exception):
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
    return repr(round(float(value), 6))


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


def number(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


@dataclass(frozen=True)
class Account:
    kind: str
    name: str
    included_minutes: float

    @property
    def usage_path(self) -> str:
        scope = "organizations" if self.kind == "org" else "users"
        return f"/{scope}/{urllib.parse.quote(self.name)}/settings/billing/usage"


@dataclass(frozen=True)
class Config:
    accounts: Sequence[Account] = ()
    reports: Sequence[str] = ()
    import_url: str = ""
    state_path: str = ""
    lookback_hours: int = 72


def parse_accounts(text: str) -> list[Account]:
    found: list[Account] = []
    for item in json.loads(text or "[]"):
        kind = "org" if "org" in item else "user"
        found.append(Account(kind, str(item[kind]), float(item.get("included_minutes", 2000))))
    return found


def split_list(text: str) -> list[str]:
    return [part.strip() for part in text.split(",") if part.strip()]


def repository(account: Account, item: Mapping[str, Any]) -> str:
    repo = str(item.get("repositoryName") or "")
    if not repo:
        return ""
    if "/" in repo:
        return repo
    owner = str(item.get("organizationName") or "") or account.name
    return f"{owner}/{repo}"


def usage_samples(
    account: Account, items: Sequence[Mapping[str, Any]], visibility: Mapping[str, str]
) -> list[Sample]:
    usage: dict[Labels, float] = {}
    gross: dict[Labels, float] = {}
    net: dict[Labels, float] = {}
    used = 0.0
    for item in items:
        product = str(item.get("product") or "")
        sku = str(item.get("sku") or "")
        unit = str(item.get("unitType") or "")
        repo = repository(account, item)
        quantity = number(item.get("quantity"))
        key = labels(account=account.name, product=product, sku=sku, repo=repo, unit=unit)
        usage[key] = usage.get(key, 0.0) + quantity
        money = labels(account=account.name, product=product)
        gross[money] = gross.get(money, 0.0) + number(item.get("grossAmount"))
        net[money] = net.get(money, 0.0) + number(item.get("netAmount"))
        weight = INCLUDED_SKUS.get(sku.lower())
        free = visibility.get(repo) == FREE_VISIBILITY
        if product == "actions" and unit.lower() == "minutes" and weight and not free:
            used += quantity * weight
    owner = labels(account=account.name)
    return [
        *(("ayeops_github_usage", key, value) for key, value in usage.items()),
        *(("ayeops_github_usage_gross_dollars", key, value) for key, value in gross.items()),
        *(("ayeops_github_usage_net_dollars", key, value) for key, value in net.items()),
        ("ayeops_github_included_minutes_used", owner, used),
        ("ayeops_github_included_minutes", owner, account.included_minutes),
    ]


def parse_report_labels(body: str) -> dict[str, str] | None:
    found: dict[str, str] = {}
    position = 0
    while position < len(body):
        match = REPORT_LABEL.match(body, position)
        if match is None:
            return None
        found[match.group(1)] = match.group(2)
        position = match.end()
    return found


def clean_report(text: str, repo: str, now_ms: int, oldest_ms: int) -> tuple[list[str], int]:
    kept: list[str] = []
    rejected = 0
    for raw in text.splitlines()[:MAX_REPORT_LINES]:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = REPORT_LINE.match(line)
        pairs = parse_report_labels(match.group(2) or "") if match else None
        if match is None or pairs is None or not REPORT_METRIC.match(match.group(1)):
            rejected += 1
            continue
        value, stamp = match.group(3), int(match.group(4))
        try:
            float(value)
        except ValueError:
            rejected += 1
            continue
        if stamp < oldest_ms or stamp > now_ms + FUTURE_SLACK_MS:
            rejected += 1
            continue
        pairs["repo"] = escape(repo)
        body = ",".join(f'{key}="{pairs[key]}"' for key in sorted(pairs))
        kept.append(f"{match.group(1)}{{{body}}} {value} {stamp}")
    rejected += max(0, len(text.splitlines()) - MAX_REPORT_LINES)
    return kept, rejected


def unzip_report(data: bytes) -> str:
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as error:
        raise GitHubError("artifact is not a zip archive") from error
    with archive:
        members = [info for info in archive.infolist() if not info.is_dir()]
        if sum(info.file_size for info in members) > MAX_ARTIFACT_BYTES:
            raise GitHubError("artifact unpacks to more than 4 MiB")
        return "".join(
            archive.read(info).decode("utf-8", errors="replace") + "\n" for info in members
        )


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


@dataclass
class Http:
    timeout: float = 60.0
    opener: Any = field(default_factory=lambda: urllib.request.build_opener(NoRedirect()))

    def get(self, url: str, headers: Mapping[str, str]) -> tuple[int, Mapping[str, str], bytes]:
        request = urllib.request.Request(url, headers=dict(headers))
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                return (
                    int(response.status),
                    dict(response.headers),
                    response.read(MAX_ARTIFACT_BYTES + 1),
                )
        except urllib.error.HTTPError as error:
            with error:
                if error.code in (301, 302, 303, 307, 308):
                    return error.code, dict(error.headers), b""
                raise GitHubError(f"HTTP {error.code}", status=error.code) from None

    def post(self, url: str, body: bytes) -> None:
        request = urllib.request.Request(
            url, data=body, headers={"Content-Type": "text/plain"}, method="POST"
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                response.read()
        except urllib.error.HTTPError as error:
            raise GitHubError(f"storage answered HTTP {error.code}", error.code) from None


class Client:
    def __init__(self, token: str, http: Http | None = None):
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": "ayeops-github-exporter",
        }
        self.http = http or Http()
        self.sent = 0

    def json(self, path: str) -> Any:
        self.sent += 1
        status, _, body = self.http.get(f"{API}{path}", self._headers)
        if status != 200:
            raise GitHubError(f"HTTP {status}", status=status)
        return json.loads(body)

    def download(self, url: str) -> bytes:
        self.sent += 1
        status, headers, body = self.http.get(url, self._headers)
        if status in (301, 302, 303, 307, 308):
            location = headers.get("Location") or headers.get("location") or ""
            if not location.startswith("https://"):
                raise GitHubError("artifact redirect without an https location")
            status, _, body = self.http.get(location, {"User-Agent": self._headers["User-Agent"]})
        if status != 200:
            raise GitHubError(f"artifact download answered HTTP {status}", status=status)
        if len(body) > MAX_ARTIFACT_BYTES:
            raise GitHubError("artifact is larger than 4 MiB")
        return body


def iso_ms(text: str) -> int:
    return int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp() * 1000)


def load_state(path: str) -> dict[str, int]:
    if not path:
        return {}
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {str(key): int(value) for key, value in (data.get("imported") or {}).items()}


def save_state(path: str, imported: Mapping[str, int]) -> None:
    if not path:
        return
    target = Path(path)
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps({"imported": dict(imported)}, sort_keys=True), "utf-8")
    temporary.replace(target)


def log_json(record: Mapping[str, Any]) -> None:
    print(json.dumps(record, sort_keys=True), flush=True)


class Exporter:
    def __init__(
        self,
        config: Config,
        client: Client,
        log: Log = log_json,
        clock: Callable[[], float] = time.time,
    ):
        self.config = config
        self.client = client
        self.log = log
        self.clock = clock
        self.samples: list[Sample] = []
        self.visibility: dict[str, tuple[str, float]] = {}
        self.imported = load_state(config.state_path)
        self.imported_total: dict[str, int] = {}
        self.rejected_total: dict[str, int] = {}
        self.lock = threading.Lock()

    def repo_visibility(self, repo: str) -> str:
        now = self.clock()
        cached = self.visibility.get(repo)
        if cached and now - cached[1] < VISIBILITY_TTL:
            return cached[0]
        try:
            answer = self.client.json(f"/repos/{repo}")
            value = str(answer.get("visibility") or ("private" if answer.get("private") else ""))
        except (GitHubError, ValueError, AttributeError):
            value = "unknown"
        self.visibility[repo] = (value, now)
        return value

    def poll_account(self, account: Account, moment: datetime) -> list[Sample]:
        query = f"?year={moment.year}&month={moment.month}"
        try:
            answer = self.client.json(account.usage_path + query)
            items = [item for item in answer.get("usageItems") or [] if isinstance(item, dict)]
        except (GitHubError, ValueError, AttributeError) as error:
            self.log({"event": "usage-failed", "account": account.name, "error": str(error)})
            return [("ayeops_github_query_success", labels(account=account.name), 0.0)]
        repos = {repository(account, item) for item in items} - {""}
        visibility = {repo: self.repo_visibility(repo) for repo in sorted(repos)}
        return [
            *usage_samples(account, items, visibility),
            ("ayeops_github_query_success", labels(account=account.name), 1.0),
        ]

    def import_repo(self, repo: str) -> float:
        now_ms = int(self.clock() * 1000)
        oldest_ms = now_ms - self.config.lookback_hours * 3600 * 1000
        try:
            answer = self.client.json(f"/repos/{repo}/actions/artifacts?per_page=100")
            artifacts = answer.get("artifacts") or []
        except (GitHubError, ValueError, AttributeError) as error:
            self.log({"event": "artifacts-failed", "repo": repo, "error": str(error)})
            return 0.0
        ok = 1.0
        for artifact in artifacts:
            if not str(artifact.get("name", "")).startswith(REPORT_PREFIX):
                continue
            key = str(artifact.get("id"))
            try:
                created = iso_ms(str(artifact.get("created_at")))
            except ValueError:
                continue
            if artifact.get("expired") or key in self.imported or created < oldest_ms:
                continue
            try:
                text = unzip_report(self.client.download(str(artifact["archive_download_url"])))
                kept, rejected = clean_report(text, repo, now_ms, oldest_ms)
                if kept:
                    self.client.http.post(self.config.import_url, "\n".join(kept).encode() + b"\n")
            except (GitHubError, KeyError, OSError) as error:
                ok = 0.0
                self.log(
                    {"event": "import-failed", "repo": repo, "artifact": key, "error": str(error)}
                )
                continue
            self.imported[key] = created
            self.imported_total[repo] = self.imported_total.get(repo, 0) + 1
            self.rejected_total[repo] = self.rejected_total.get(repo, 0) + rejected
            run = artifact.get("workflow_run") or {}
            self.log(
                {
                    "event": "import",
                    "repo": repo,
                    "artifact": artifact.get("name"),
                    "run": run.get("id"),
                    "branch": run.get("head_branch"),
                    "commit": run.get("head_sha"),
                    "samples": len(kept),
                    "rejected": rejected,
                }
            )
        self.imported = {k: v for k, v in self.imported.items() if v >= oldest_ms - 86400000}
        try:
            save_state(self.config.state_path, self.imported)
        except OSError as error:
            self.log({"event": "state-save-failed", "error": str(error)})
            return 0.0
        return ok

    def refresh(self) -> None:
        began = time.monotonic()
        moment = datetime.fromtimestamp(self.clock(), UTC)
        out: list[Sample] = []
        for account in self.config.accounts:
            out.extend(self.poll_account(account, moment))
        for repo in self.config.reports:
            success = self.import_repo(repo)
            out.append(("ayeops_github_report_import_success", labels(repo=repo), success))
        out.append(("ayeops_github_poll_timestamp_seconds", (), moment.timestamp()))
        self.log(
            {
                "event": "poll",
                "requests": self.client.sent,
                "seconds": round(time.monotonic() - began, 2),
            }
        )
        with self.lock:
            self.samples = out

    def metrics(self) -> str:
        with self.lock:
            counters: list[Sample] = [
                ("ayeops_github_api_requests_total", (), float(self.client.sent))
            ]
            counters += [
                ("ayeops_github_reports_imported_total", labels(repo=repo), float(count))
                for repo, count in self.imported_total.items()
            ]
            counters += [
                ("ayeops_github_report_lines_rejected_total", labels(repo=repo), float(count))
                for repo, count in self.rejected_total.items()
            ]
            return render([*self.samples, *counters])


def serve(exporter: Exporter, port: int, refresh_seconds: int) -> None:
    def refresh_forever() -> None:
        while True:
            try:
                exporter.refresh()
            except Exception as error:
                exporter.log({"event": "poll-crashed", "error": repr(error)})
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
        accounts=parse_accounts(env.get("AYE_GITHUB_ACCOUNTS", "")),
        reports=split_list(env.get("AYE_GITHUB_REPORTS", "")),
        import_url=env.get("AYE_GITHUB_IMPORT_URL", ""),
        state_path=env.get("AYE_GITHUB_STATE", ""),
        lookback_hours=int(env.get("AYE_GITHUB_LOOKBACK_HOURS", "72")),
    )


def main() -> None:
    env = os.environ
    config = config_from(env)
    refresh_seconds = int(env.get("AYE_GITHUB_REFRESH_SECONDS", "300"))
    log_json(
        {
            "event": "start",
            "accounts": [account.name for account in config.accounts],
            "reports": list(config.reports),
            "refresh_seconds": refresh_seconds,
        }
    )
    client = Client(env["AYE_GITHUB_TOKEN"])
    serve(Exporter(config, client), int(env["AYE_GITHUB_PORT"]), refresh_seconds)


if __name__ == "__main__":
    main()
