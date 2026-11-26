from __future__ import annotations

import argparse
import base64
import ipaddress
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from ayeops.defaults import UsageError, add_manifest
from ayeops.local_modules import with_local_modules
from ayeops.logs import LOKI_DATASOURCE_UID, logs_settings
from ayeops.manifest import Manifest, load_manifest
from ayeops.registry import Module, load_modules
from ayeops.render import (
    DATASOURCE_TYPES,
    DATASOURCE_UID,
    dump_json,
    grafana_root_url,
    host_settings,
    module_dashboards,
    project_dashboard_files,
)
from ayeops.rules import BUILTIN_DATASOURCES, check_manifest

USER_ENV = "AYE_GRAFANA_ADMIN_USER"
PASSWORD_ENV = "AYE_GRAFANA_ADMIN_PASSWORD"
URL_ENV = "AYE_GRAFANA_URL"
TIMEOUT = 15.0
SEARCH_LIMIT = 5000
USER_AGENT = "aye-dashboards"
UID = re.compile(r"[A-Za-z0-9_-]{1,40}")
VOLATILE = ("id", "version", "iteration", "folderId", "folderUid")
FORMATS = ("text", "json")
KINDS = ("unmanaged", "stale", "not-deployed")
NOTES = {
    "unmanaged": "no file provisions it, aye dashboards pull writes it",
    "stale": "provisioned, but the render no longer has it, a deploy removes it",
    "not-deployed": "rendered, but Grafana does not provision it yet, deploy the Grafana host",
}

Http = Callable[[str, Mapping[str, str]], tuple[int, bytes]]


class GrafanaError(Exception):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


OPENER = urllib.request.build_opener(NoRedirect)


def urlopen_http(url: str, headers: Mapping[str, str]) -> tuple[int, bytes]:
    request = urllib.request.Request(url, headers=dict(headers))
    try:
        with OPENER.open(request, timeout=TIMEOUT) as response:
            return int(response.status), bytes(response.read())
    except urllib.error.HTTPError as error:
        return int(error.code), bytes(error.read())
    except (urllib.error.URLError, OSError) as error:
        reason = getattr(error, "reason", error)
        host = urllib.parse.urlsplit(url).netloc
        raise GrafanaError(f"cannot reach {host}: {reason}") from None


def basic(user: str, password: str) -> str:
    pair = f"{user}:{password}".encode()
    return "Basic " + base64.b64encode(pair).decode()


def loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def url_problem(url: str) -> str | None:
    parts = urllib.parse.urlsplit(url)
    host = parts.hostname or ""
    if not host or parts.scheme not in ("http", "https"):
        return f"{url!r} is not an http(s) URL"
    if parts.scheme == "http" and not loopback(host):
        return (
            f"refusing to send the admin password over plain http to {host}:"
            " use https, or an ssh tunnel to 127.0.0.1"
        )
    return None


@dataclass(frozen=True)
class Grafana:
    base: str
    authorization: str
    http: Http

    def get(self, path: str) -> Any:
        headers = {
            "Accept": "application/json",
            "Authorization": self.authorization,
            "User-Agent": USER_AGENT,
        }
        status, body = self.http(f"{self.base}{path}", headers)
        where = path.split("?")[0]
        if status == 401:
            raise GrafanaError(
                f"GET {where} answered HTTP 401: Grafana refused {USER_ENV} and {PASSWORD_ENV}"
            )
        if status != 200:
            raise GrafanaError(f"GET {where} answered HTTP {status}")
        try:
            return json.loads(body)
        except ValueError:
            raise GrafanaError(f"GET {where} answered with a body that is not JSON") from None


def connect(
    url: str | None, settings: dict[str, Any], environ: Mapping[str, str], http: Http
) -> Grafana:
    base = url or environ.get(URL_ENV) or grafana_root_url(settings)
    if not base:
        raise UsageError(
            f"no Grafana URL: pass --url, set {URL_ENV} or grafana.root_url in the manifest"
        )
    problem = url_problem(base)
    if problem:
        raise UsageError(problem)
    missing = [name for name in (USER_ENV, PASSWORD_ENV) if not environ.get(name)]
    if missing:
        raise UsageError(
            f"missing environment variable(s): {', '.join(missing)};"
            " run it under aye secrets run -- <this command>"
        )
    authorization = basic(environ[USER_ENV], environ[PASSWORD_ENV])
    return Grafana(base.rstrip("/"), authorization, http)


@dataclass(frozen=True)
class Live:
    uid: str
    title: str
    folder: str
    provisioned: bool
    dashboard: dict[str, Any]


def search(grafana: Grafana) -> list[dict[str, Any]]:
    hits: list[dict[str, Any]] = []
    page = 1
    while True:
        query = urllib.parse.urlencode({"type": "dash-db", "limit": SEARCH_LIMIT, "page": page})
        found = grafana.get(f"/api/search?{query}")
        hits.extend(found)
        if len(found) < SEARCH_LIMIT:
            return hits
        page += 1


def live_dashboards(grafana: Grafana) -> list[Live]:
    found = []
    for hit in search(grafana):
        uid = str(hit["uid"])
        data = grafana.get(f"/api/dashboards/uid/{urllib.parse.quote(uid)}")
        meta = data["meta"]
        title = str(hit.get("title") or "")
        folder = str(hit.get("folderTitle") or "")
        found.append(Live(uid, title, folder, meta.get("provisioned") is True, data["dashboard"]))
    return sorted(found, key=lambda item: item.uid)


def live_datasources(grafana: Grafana) -> dict[str, str]:
    return {str(source["uid"]): str(source["name"]) for source in grafana.get("/api/datasources")}


@dataclass(frozen=True)
class Context:
    path: Path
    manifest: Manifest
    modules: dict[str, Module]
    host: str
    settings: dict[str, Any]

    @property
    def grafana(self) -> dict[str, Any]:
        return dict(self.settings["grafana"])


def grafana_context(manifest_path: Path, manifest: Manifest, modules: dict[str, Module]) -> Context:
    for host in sorted(manifest.hosts):
        settings = host_settings(manifest, host, modules)
        if "grafana" in settings:
            return Context(manifest_path, manifest, modules, host, settings)
    raise UsageError(f"{manifest_path}: no host runs the grafana module")


def load(manifest_path: Path) -> Context | None:
    manifest = load_manifest(manifest_path)
    modules = with_local_modules(manifest, load_modules())
    findings = check_manifest(manifest, modules)
    if findings:
        for finding in findings:
            print(finding, file=sys.stderr)
        print(f"{manifest_path}: fix the errors first", file=sys.stderr)
        return None
    return grafana_context(manifest_path, manifest, modules)


def rendered_uids(context: Context) -> dict[str, str]:
    settings = context.grafana
    boards = [
        json.loads(text)
        for text in module_dashboards(context.manifest, context.modules, settings).values()
    ]
    boards += [
        json.loads(path.read_bytes())
        for path in project_dashboard_files(context.manifest, settings)
    ]
    return {str(board["uid"]): str(board.get("title") or "") for board in boards}


def repo_datasources(context: Context) -> dict[str, dict[str, str]]:
    sources = {"VictoriaMetrics": {"type": "prometheus", "uid": DATASOURCE_UID}}
    manifest = context.manifest
    if any(
        (logs_settings(manifest, host) or {}).get("role") == "server" for host in manifest.hosts
    ):
        sources["Loki"] = {"type": "loki", "uid": LOKI_DATASOURCE_UID}
    for source in context.grafana.get("datasources") or []:
        kind = DATASOURCE_TYPES[str(source["type"])]
        sources[str(source["name"])] = {"type": kind, "uid": str(source["uid"])}
    return sources


@dataclass
class Mapper:
    repo: dict[str, dict[str, str]]
    live: dict[str, str]
    unknown: set[str]

    def ref(self, value: Any) -> Any:
        if isinstance(value, dict) and isinstance(value.get("uid"), str):
            key = str(value["uid"])
        elif isinstance(value, str):
            key = value
        else:
            return value
        uids = {ref["uid"] for ref in self.repo.values()}
        if key.startswith("$") or key in BUILTIN_DATASOURCES or key in uids:
            return value
        name = self.live.get(key, key)
        if name in self.repo:
            return dict(self.repo[name])
        self.unknown.add(key)
        return value

    def rewrite(self, node: Any) -> Any:
        if isinstance(node, dict):
            return {
                key: self.ref(value) if key == "datasource" else self.rewrite(value)
                for key, value in node.items()
            }
        if isinstance(node, list):
            return [self.rewrite(value) for value in node]
        return node


def normalised(dashboard: dict[str, Any], mapper: Mapper) -> dict[str, Any]:
    board = {key: value for key, value in dashboard.items() if key not in VOLATILE}
    result: dict[str, Any] = mapper.rewrite(board)
    return result


@dataclass(frozen=True)
class Entry:
    kind: str
    uid: str
    title: str
    folder: str


def classify(live: list[Live], rendered: dict[str, str]) -> list[Entry]:
    entries = [
        Entry("stale" if item.provisioned else "unmanaged", item.uid, item.title, item.folder)
        for item in live
        if item.uid not in rendered
    ]
    provisioned = {item.uid for item in live if item.provisioned}
    entries += [
        Entry("not-deployed", uid, title, "")
        for uid, title in sorted(rendered.items())
        if uid not in provisioned
    ]
    return sorted(entries, key=lambda entry: (KINDS.index(entry.kind), entry.uid))


def entry_line(entry: Entry) -> str:
    where = f" in {entry.folder}" if entry.folder else ""
    return f'{entry.kind} {entry.uid} "{entry.title}"{where}: {NOTES[entry.kind]}'


def drift_summary(entries: list[Entry], total: int, url: str) -> str:
    if not entries:
        return f"all {total} dashboard(s) in {url} are provisioned from the render"
    counts = Counter(entry.kind for entry in entries)
    parts = ", ".join(f"{counts[kind]} {kind}" for kind in KINDS if counts[kind])
    return f"{parts} among the {total} dashboard(s) in {url}"


def drift_json(entries: list[Entry], total: int, url: str) -> str:
    return dump_json(
        {"grafana": url, "dashboards": total, "drift": [asdict(entry) for entry in entries]}
    )


def drift(context: Context, grafana: Grafana, form: str) -> int:
    live = live_dashboards(grafana)
    entries = classify(live, rendered_uids(context))
    if form == "json":
        sys.stdout.write(drift_json(entries, len(live), grafana.base))
        return int(bool(entries))
    for entry in entries:
        print(entry_line(entry))
    summary = f"{context.path}: {drift_summary(entries, len(live), grafana.base)}"
    if entries:
        print(summary, file=sys.stderr)
        return 1
    print(summary)
    return 0


def choose(live: list[Live], uids: list[str]) -> list[Live]:
    if not uids:
        return [item for item in live if not item.provisioned]
    by_uid = {item.uid: item for item in live}
    chosen = []
    for uid in uids:
        item = by_uid.get(uid)
        if item is None:
            raise UsageError(f"no dashboard with uid {uid!r} in Grafana")
        if item.provisioned:
            raise UsageError(f"dashboard {uid!r} is provisioned from a file already, edit the file")
        chosen.append(item)
    return chosen


def target_dir(context: Context, out: Path | None) -> Path:
    configured = context.grafana.get("dashboards")
    provisioned = context.manifest.base_dir / str(configured) if configured else None
    if out is None:
        if provisioned is None:
            raise UsageError(
                f"{context.path}: grafana.dashboards is not set; pull into a new directory"
                " next to the manifest with -o <dir>, then set grafana.dashboards to it"
            )
        return provisioned
    if provisioned is None or out.resolve() != provisioned.resolve():
        print(
            f"note: the render provisions grafana.dashboards only, so {out} stays"
            " unprovisioned until grafana.dashboards names it",
            file=sys.stderr,
        )
    return out


def files_by_uid(directory: Path) -> dict[str, Path]:
    found: dict[str, Path] = {}
    for path in sorted(directory.glob("*.json")):
        try:
            data = json.loads(path.read_bytes())
        except ValueError:
            continue
        if isinstance(data, dict) and isinstance(data.get("uid"), str):
            found.setdefault(data["uid"], path)
    return found


def same_board(path: Path, board: dict[str, Any]) -> bool:
    try:
        return bool(json.loads(path.read_bytes()) == board)
    except ValueError:
        return False


def write_board(path: Path, board: dict[str, Any], force: bool) -> str:
    exists = path.exists()
    if exists and same_board(path, board):
        return "unchanged"
    if exists and not force:
        return "refused"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(dump_json(board).encode())
    return "replaced" if exists else "wrote"


def pull(
    context: Context,
    grafana: Grafana,
    uids: list[str],
    out: Path | None,
    force: bool,
) -> int:
    directory = target_dir(context, out)
    chosen = choose(live_dashboards(grafana), uids)
    if not chosen:
        print(f"{context.path}: no unmanaged dashboard in {grafana.base}")
        return 0
    repo = repo_datasources(context)
    live = live_datasources(grafana)
    existing = files_by_uid(directory)
    counts: Counter[str] = Counter()
    for item in chosen:
        if not UID.fullmatch(item.uid):
            print(
                f"refused {item.uid!r}: a uid that names a file is 1 to 40 of A-Z a-z 0-9 - _",
                file=sys.stderr,
            )
            counts["refused"] += 1
            continue
        mapper = Mapper(repo, live, set())
        board = normalised(item.dashboard, mapper)
        path = existing.get(item.uid, directory / f"{item.uid}.json")
        verdict = write_board(path, board, force)
        counts[verdict] += 1
        if verdict == "refused":
            print(
                f"refused {path}: it differs from {item.uid} in Grafana, --force replaces it",
                file=sys.stderr,
            )
        else:
            print(f"{verdict} {path}")
        for ref in sorted(mapper.unknown):
            print(
                f"warning: {path} reads datasource {ref!r}, which the manifest does not"
                " provision: declare it under grafana.datasources, or aye check fails",
                file=sys.stderr,
            )
    print(
        f"{context.path}: {len(chosen)} dashboard(s) into {directory}: {counts['wrote']} written,"
        f" {counts['replaced']} replaced, {counts['unchanged']} unchanged,"
        f" {counts['refused']} refused; render and deploy to provision them"
    )
    return int(counts["refused"] > 0)


def add_parser(commands: Any) -> None:
    parser: argparse.ArgumentParser = commands.add_parser(
        "dashboards",
        help="compare the dashboards in Grafana with the render, pull those drawn in the UI",
    )
    actions = parser.add_subparsers(dest="action", required=True)
    drift_parser = actions.add_parser(
        "drift",
        help="list dashboards no file provisions and rendered ones Grafana lacks, exit 1 on any",
    )
    drift_parser.add_argument("--format", choices=FORMATS, default="text")
    pull_parser = actions.add_parser(
        "pull", help="write the dashboards no file provisions into grafana.dashboards"
    )
    pull_parser.add_argument(
        "--uid", action="append", default=[], help="only this dashboard, repeatable"
    )
    pull_parser.add_argument("-o", "--out", type=Path, help="default grafana.dashboards")
    pull_parser.add_argument(
        "--force", action="store_true", help="replace a file that differs from Grafana's copy"
    )
    for sub in (drift_parser, pull_parser):
        add_manifest(sub)
        sub.add_argument("--url", help=f"Grafana's URL, default ${URL_ENV}, else grafana.root_url")


def run(args: argparse.Namespace, environ: Mapping[str, str], http: Http) -> int:
    context = load(args.manifest)
    if context is None:
        return 1
    grafana = connect(args.url, context.settings, environ, http)
    try:
        if args.action == "drift":
            return drift(context, grafana, args.format)
        return pull(context, grafana, args.uid, args.out, args.force)
    except GrafanaError as error:
        print(f"aye dashboards: {error}", file=sys.stderr)
    except (KeyError, TypeError) as error:
        print(
            f"aye dashboards: Grafana answered in a shape this version does not read"
            f" ({type(error).__name__}: {error})",
            file=sys.stderr,
        )
    return 2
