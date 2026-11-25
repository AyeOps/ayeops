from __future__ import annotations

import argparse
import copy
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TextIO

from ayeops.defaults import add_manifest, find_manifest
from ayeops.manifest import Manifest, load_manifest
from ayeops.registry import load_modules
from ayeops.rules import check_manifest
from ayeops.status import PUSH_PATH, seed_sql, sql_text, status_document

WRANGLER_VERSION = "4.148.0"
WRANGLER = ("npx", "--yes", f"wrangler@{WRANGLER_VERSION}")
DEFAULT_DATABASE = "ayeops-status"
WORKER_DIR = Path(__file__).resolve().parents[2] / "cloud" / "status"
TEMPLATE = "wrangler.toml"
BINDING = "DB"
ACCOUNT_ENV = "CLOUDFLARE_ACCOUNT_ID"
TOKEN_ENV = "CLOUDFLARE_API_TOKEN"
BOT_ENV = "TELEGRAM_BOT_TOKEN"
PUSH_ENV = "AYE_STATUS_PUSH_SECRET"
SECRET_ENVS = (ACCOUNT_ENV, TOKEN_ENV, BOT_ENV, PUSH_ENV)
WORKER_ONLY = (BOT_ENV, PUSH_ENV)
MASK = "***"
STEP_TIMEOUT = 600.0
HTTP_TIMEOUT = 10.0
DEFAULT_WAIT = 180.0
RETRY_SECONDS = 10.0
DRY_DATABASE_ID = "<database-id>"
DATABASE_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
HOSTNAME = re.compile(r"^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?[.])+[a-z]{2,63}$")
USAGE = 2
FAILED = 1


@dataclass
class Result:
    code: int
    out: str = ""
    err: str = ""


@dataclass
class Response:
    status: int
    content_type: str = ""
    body: bytes = b""


Runner = Callable[[list[str], bytes | None, Path], Result]
Fetcher = Callable[[str, str, bytes | None], Response]


class DeployError(Exception):
    pass


@dataclass
class State:
    tenants: dict[str, dict[str, Any]] = field(default_factory=dict)
    components: list[dict[str, Any]] = field(default_factory=list)
    probes: list[tuple[str, str]] = field(default_factory=list)


def wrangler_env(environ: Mapping[str, str]) -> dict[str, str]:
    return {key: value for key, value in environ.items() if key not in WORKER_ONLY}


def wrangler_runner(argv: list[str], stdin: bytes | None, cwd: Path) -> Result:
    command = [shutil.which(argv[0]) or argv[0], *argv[1:]]
    try:
        done = subprocess.run(
            command,
            input=stdin,
            stdin=None if stdin is not None else subprocess.DEVNULL,
            capture_output=True,
            cwd=cwd,
            env=wrangler_env(os.environ),
            timeout=STEP_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return Result(124, "", f"{argv[0]} timed out after {STEP_TIMEOUT:g}s")
    except OSError as error:
        return Result(127, "", f"{argv[0]}: {error.strerror or error}")
    return Result(
        done.returncode,
        done.stdout.decode("utf-8", "replace"),
        done.stderr.decode("utf-8", "replace"),
    )


def http_fetch(method: str, url: str, body: bytes | None) -> Response:
    headers = {"user-agent": "aye-status-deploy"}
    if body is not None:
        headers["content-type"] = "application/json"
    request = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as answer:
            return Response(answer.status, answer.headers.get("content-type", ""), answer.read())
    except urllib.error.HTTPError as error:
        return Response(error.code, error.headers.get("content-type", ""), error.read())
    except (urllib.error.URLError, OSError):
        return Response(0)


def mask(text: str, secrets: list[str]) -> str:
    for secret in sorted((s for s in secrets if s), key=len, reverse=True):
        text = text.replace(secret, MASK)
    return text


def missing_env(environ: Mapping[str, str], bot: bool) -> list[str]:
    names = [ACCOUNT_ENV, TOKEN_ENV, PUSH_ENV] + ([BOT_ENV] if bot else [])
    return [name for name in names if not environ.get(name, "").strip()]


def toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, list) and not any(isinstance(item, dict) for item in value):
        return "[" + ", ".join(toml_value(item) for item in value) + "]"
    raise DeployError(f"{TEMPLATE} holds a value this command cannot write: {value!r}")


def toml_text(config: dict[str, Any]) -> str:
    lines: list[str] = []
    tables: list[tuple[str, dict[str, Any]]] = []
    for key, value in config.items():
        if isinstance(value, dict):
            tables.append((f"[{key}]", value))
        elif isinstance(value, list) and value and all(isinstance(item, dict) for item in value):
            tables.extend((f"[[{key}]]", item) for item in value)
        else:
            lines.append(f"{key} = {toml_value(value)}")
    for header, table in tables:
        lines.extend(["", header])
        lines.extend(f"{key} = {toml_value(value)}" for key, value in table.items())
    return "\n".join(lines) + "\n"


def load_template(worker_dir: Path) -> dict[str, Any]:
    path = worker_dir / TEMPLATE
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise DeployError(f"{path} is missing; pass --worker-dir <checkout>/cloud/status") from None


def fill_config(
    template: dict[str, Any],
    worker_dir: Path,
    database: str,
    database_id: str,
    routes: list[str],
) -> dict[str, Any]:
    config = copy.deepcopy(template)
    config["main"] = (worker_dir / str(template["main"])).as_posix()
    config["workers_dev"] = False
    bound = [entry for entry in config.get("d1_databases", []) if entry.get("binding") == BINDING]
    if len(bound) != 1:
        raise DeployError(f"{TEMPLATE} needs exactly one d1_databases entry with binding {BINDING}")
    entry = bound[0]
    entry["database_name"] = database
    entry["database_id"] = database_id
    entry["migrations_dir"] = (
        worker_dir / str(entry.get("migrations_dir", "migrations"))
    ).as_posix()
    config["routes"] = [{"pattern": hostname, "custom_domain": True} for hostname in routes]
    return config


def parse_json(text: str) -> Any:
    starts = [index for index in (text.find("["), text.find("{")) if index >= 0]
    if not starts:
        raise DeployError("wrangler printed no JSON")
    try:
        value, _ = json.JSONDecoder().raw_decode(text[min(starts) :])
    except json.JSONDecodeError as error:
        raise DeployError(f"wrangler printed JSON this command cannot read: {error}") from None
    return value


def database_id(listing: str, name: str) -> str | None:
    for entry in parse_json(listing):
        if isinstance(entry, dict) and entry.get("name") == name:
            return str(entry.get("uuid") or entry.get("id") or "") or None
    return None


def state_query(tenant: str) -> str:
    quoted = sql_text(tenant)
    return "; ".join(
        [
            "SELECT id, hostname, title, locale, notify_chat, notify_min, access_team, access_aud "
            "FROM tenants ORDER BY id",
            "SELECT id, title, position, push FROM components "
            f"WHERE tenant_id = {quoted} ORDER BY position, id",
            f"SELECT component_id, url FROM probes WHERE tenant_id = {quoted} "
            "ORDER BY component_id, url",
        ]
    )


def parse_state(text: str) -> State:
    blocks = parse_json(text)
    if not isinstance(blocks, list) or len(blocks) != 3:
        raise DeployError("wrangler did not answer the three queries of the tenant state")
    rows = [list(block.get("results") or []) if isinstance(block, dict) else [] for block in blocks]
    return State(
        tenants={str(row["id"]): row for row in rows[0]},
        components=rows[1],
        probes=[(str(row["component_id"]), str(row["url"])) for row in rows[2]],
    )


def wanted_tenant(document: dict[str, Any]) -> dict[str, Any]:
    notify = document.get("notify") or {}
    access = document.get("access") or {}
    return {
        "id": document["tenant"],
        "hostname": document["hostname"],
        "title": document["title"],
        "locale": document["locale"],
        "notify_chat": notify.get("telegram_chat_id"),
        "notify_min": notify.get("min_state") or "outage",
        "access_team": access.get("team"),
        "access_aud": access.get("aud"),
    }


def seed_changes(document: dict[str, Any], state: State) -> list[str]:
    tenant = document["tenant"]
    components = document["components"]
    current = state.tenants.get(tenant)
    if current is None:
        return [f"tenant {tenant} and its {len(components)} component(s) are missing"]
    changes = []
    wanted = wanted_tenant(document)
    fields = [key for key in wanted if current.get(key) != wanted[key]]
    if fields:
        changes.append(f"tenant {tenant}: {', '.join(fields)} changed")
    have = {str(row["id"]): row for row in state.components}
    for position, component in enumerate(components):
        row = have.pop(component["id"], None)
        target = (component["title"], position, int(component["push"]))
        if row is None:
            changes.append(f"component {component['id']} is missing")
        elif (row.get("title"), row.get("position"), row.get("push")) != target:
            changes.append(f"component {component['id']} changed")
    changes.extend(f"component {ident} left the manifest" for ident in have)
    wanted_probes = sorted((c["id"], url) for c in components for url in c["probes"])
    if sorted(state.probes) != wanted_probes:
        changes.append("probe URLs changed")
    return changes


def other_routes(state: State, tenant: str, hostname: str) -> list[str]:
    for ident, row in state.tenants.items():
        if ident != tenant and row.get("hostname") == hostname:
            raise DeployError(f"{hostname} already belongs to tenant {ident} in D1")
    others = {str(row["hostname"]) for ident, row in state.tenants.items() if ident != tenant}
    return sorted(others | {hostname})


def secret_sql(tenant: str, secret: str) -> str:
    return f"UPDATE tenants SET secret = {sql_text(secret)} WHERE id = {sql_text(tenant)};\n"


def write_private(path: Path, text: str) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def unsigned_push(tenant: str, component: str, now: float) -> bytes:
    body = {
        "tenant": tenant,
        "ts": int(now),
        "components": [{"id": component, "state": "operational"}],
    }
    return json.dumps(body, separators=(",", ":")).encode("utf-8")


def listed_ids(response: Response) -> list[str] | None:
    try:
        data = json.loads(response.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("components"), list):
        return None
    return [str(item.get("id")) for item in data["components"] if isinstance(item, dict)]


@dataclass
class Check:
    name: str
    method: str
    path: str
    body: bytes | None
    passes: Callable[[Response], bool]
    expected: str


def page_checks(document: dict[str, Any], now: float) -> list[Check]:
    ids = [component["id"] for component in document["components"]]
    return [
        Check(
            "public page",
            "GET",
            "/",
            None,
            lambda r: r.status == 200 and r.content_type.lower().startswith("text/html"),
            "200 text/html",
        ),
        Check(
            "status JSON",
            "GET",
            "/api/status",
            None,
            lambda r: r.status == 200 and listed_ids(r) == ids,
            f"200 with the components {', '.join(ids)}",
        ),
        Check(
            "unsigned push",
            "POST",
            PUSH_PATH,
            unsigned_push(document["tenant"], ids[0], now),
            lambda r: r.status == 401,
            "401",
        ),
    ]


@dataclass
class Options:
    database: str = DEFAULT_DATABASE
    hostname: str | None = None
    worker_dir: Path = WORKER_DIR
    wait: float = DEFAULT_WAIT
    dry_run: bool = False


@dataclass
class Session:
    options: Options
    environ: Mapping[str, str]
    document: dict[str, Any]
    runner: Runner
    fetch: Fetcher
    out: TextIO
    sleep: Callable[[float], None]
    clock: Callable[[], float]
    workdir: Path = Path()
    template: dict[str, Any] = field(default_factory=dict)

    @property
    def secrets(self) -> list[str]:
        return [self.environ.get(name, "") for name in SECRET_ENVS]

    @property
    def config(self) -> Path:
        return self.workdir / TEMPLATE

    def say(self, text: str) -> None:
        print(mask(text, self.secrets), file=self.out)

    def wrangler(
        self,
        title: str,
        args: list[str],
        stdin: bytes | None = None,
        label: str = "",
        echo: bool = True,
        note: str = "",
    ) -> Result:
        argv = [*WRANGLER, *args]
        self.say(f"==> {title}")
        self.say("    $ " + shlex.join(argv) + (f" < {label}" if label else ""))
        if note:
            self.say(f"    {note}")
        if self.options.dry_run:
            return Result(0)
        result = self.runner(argv, stdin, self.workdir)
        shown = (result.out + result.err) if echo or result.code else ""
        for line in shown.splitlines():
            self.say(f"    | {line}")
        if result.code != 0:
            raise DeployError(f"{title} failed with exit code {result.code}")
        return result

    def write_config(self, database_id: str, routes: list[str]) -> None:
        config = fill_config(
            self.template, self.options.worker_dir, self.options.database, database_id, routes
        )
        self.config.write_text(toml_text(config), encoding="utf-8", newline="\n")
        self.say(f"    {TEMPLATE}: database {self.options.database} ({database_id})")
        self.say(f"    routes: {', '.join(routes)}")

    def ensure_database(self) -> str:
        name = self.options.database
        listing = self.wrangler("find the D1 database", ["d1", "list", "--json"], echo=False)
        if self.options.dry_run:
            create = shlex.join([*WRANGLER, "d1", "create", name])
            self.say(f"    only when the list does not hold {name}: $ {create}")
            return DRY_DATABASE_ID
        found = database_id(listing.out, name)
        if found:
            self.say(f"    {name} exists: {found}")
            return found
        self.wrangler("create the D1 database", ["d1", "create", name])
        listing = self.wrangler("find the new database", ["d1", "list", "--json"], echo=False)
        found = database_id(listing.out, name)
        if not found:
            raise DeployError(f"{name} was created but d1 list does not show it")
        return found

    def read_state(self) -> State | None:
        args = ["d1", "execute", self.options.database, "--remote", "--json"]
        args += ["--config", str(self.config), "--command", state_query(self.document["tenant"])]
        result = self.wrangler("read the tenants and components in D1", args, echo=False)
        return None if self.options.dry_run else parse_state(result.out)

    def execute_file(self, title: str, path: Path, note: str = "") -> None:
        args = ["d1", "execute", self.options.database, "--remote", "--yes"]
        args += ["--config", str(self.config), "--file", str(path)]
        self.wrangler(title, args, note=note)

    def seed(self, state: State | None) -> None:
        tenant = self.document["tenant"]
        if state is None:
            self.say("==> seed the tenant")
            self.say(f"    runs aye status --sql for {tenant} only when D1 differs from it")
            return
        changes = seed_changes(self.document, state)
        if not changes:
            count = len(self.document["components"])
            self.say(f"==> seed: tenant {tenant} and its {count} component(s) are up to date")
            return
        path = self.workdir / "seed.sql"
        path.write_text(seed_sql(self.document), encoding="utf-8", newline="\n")
        self.say("    D1 differs from the manifest: " + "; ".join(changes))
        self.execute_file(f"seed the tenant {tenant}", path)

    def push_secret(self) -> None:
        tenant = self.document["tenant"]
        path = self.workdir / "push-secret.sql"
        title = f"set the push secret of {tenant} from {PUSH_ENV}"
        note = f"{path.name}, mode 0600, deleted after: {secret_sql(tenant, MASK).strip()}"
        if self.options.dry_run:
            self.execute_file(title, path, note)
            return
        try:
            write_private(path, secret_sql(tenant, self.environ[PUSH_ENV]))
            self.execute_file(title, path, note)
        finally:
            path.unlink(missing_ok=True)

    def bot_token(self) -> None:
        token = self.environ.get(BOT_ENV, "").strip()
        if not token:
            self.say(f"==> skip {BOT_ENV}: the manifest sets no notify and the variable is unset")
            return
        args = ["secret", "put", BOT_ENV, "--config", str(self.config)]
        self.wrangler(f"set the Worker secret {BOT_ENV}", args, token.encode("utf-8"), BOT_ENV)

    def check(self) -> None:
        base = f"https://{self.document['hostname']}"
        checks = page_checks(self.document, self.clock())
        if self.options.dry_run:
            for check in checks:
                target = f"{check.method} {base}{check.path}"
                self.say(f"==> check {check.name}: {target} wants {check.expected}")
            return
        deadline = self.clock() + self.options.wait
        for check in checks:
            self.say(f"==> check {check.name}: {check.method} {base}{check.path}")
            while True:
                answer = self.fetch(check.method, base + check.path, check.body)
                if check.passes(answer):
                    self.say(f"    ok: {answer.status}")
                    break
                if self.clock() >= deadline:
                    got = answer.status or "no answer"
                    raise DeployError(
                        f"{check.name} gave {got}, wanted {check.expected}; the Worker is"
                        " deployed, a new domain can take minutes: run again or raise --wait"
                    )
                self.say(f"    {answer.status or 'no answer'}, retrying in {RETRY_SECONDS:g}s")
                self.sleep(RETRY_SECONDS)

    def run(self) -> None:
        self.template = load_template(self.options.worker_dir)
        hostname = self.document["hostname"]
        identifier = self.ensure_database()
        self.write_config(identifier, [hostname])
        args = ["d1", "migrations", "apply", self.options.database, "--remote"]
        self.wrangler("apply the D1 migrations", [*args, "--config", str(self.config)])
        state = self.read_state()
        routes = (
            [hostname] if state is None else other_routes(state, self.document["tenant"], hostname)
        )
        self.seed(state)
        self.push_secret()
        self.say("==> write the deploy config")
        self.write_config(identifier, routes)
        self.wrangler("deploy the Worker", ["deploy", "--config", str(self.config)])
        self.bot_token()
        self.check()


def deploy_document(manifest: Manifest, options: Options, err: TextIO) -> dict[str, Any] | None:
    findings = check_manifest(manifest, load_modules())
    if findings:
        for finding in findings:
            print(finding, file=err)
        print("the manifest has errors, fix them first", file=err)
        return None
    document = status_document(manifest)
    if options.hostname:
        document["hostname"] = options.hostname
    if not document["hostname"]:
        print(
            "status_page.hostname is not set: set it in the manifest or pass --hostname", file=err
        )
        return None
    if not HOSTNAME.match(document["hostname"]):
        print(f"{document['hostname']!r} is not a hostname", file=err)
        return None
    if not document["components"]:
        print("no public component: mark a service with status: {public: <title>}", file=err)
        return None
    if not DATABASE_NAME.match(options.database):
        print(f"{options.database!r} is not a D1 database name", file=err)
        return None
    return document


def deploy_status(
    manifest: Manifest,
    options: Options,
    environ: Mapping[str, str],
    runner: Runner = wrangler_runner,
    fetch: Fetcher = http_fetch,
    out: TextIO = sys.stdout,
    err: TextIO = sys.stderr,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.time,
) -> int:
    document = deploy_document(manifest, options, err)
    if document is None:
        return USAGE
    missing = missing_env(environ, document.get("notify") is not None)
    if missing:
        print(f"missing environment variable(s): {', '.join(missing)}", file=err)
        print("run it under: infisical run --env=<env> --path=<path> -- <this command>", file=err)
        return USAGE
    session = Session(options, environ, document, runner, fetch, out, sleep, clock)
    with tempfile.TemporaryDirectory(prefix="aye-status-") as workdir:
        session.workdir = Path(workdir)
        try:
            session.run()
        except DeployError as error:
            print(mask(f"status deploy stopped: {error}", session.secrets), file=err)
            return FAILED
    if options.dry_run:
        session.say(admin_note(document))
        session.say("dry run: nothing was changed")
        return 0
    session.say(f"https://{document['hostname']} is live for tenant {document['tenant']}")
    session.say(admin_note(document))
    return 0


def admin_note(document: dict[str, Any]) -> str:
    target = f"{document['hostname']}/api/admin"
    access = document.get("access")
    if access is None:
        return (
            "admin is closed: create a Cloudflare Access application for "
            f"{target}, put its team and AUD tag into status_page.access and deploy again"
        )
    return (
        f"admin accepts Cloudflare Access tokens of team {access['team']} for the application "
        f"with AUD tag {access['aud']} on {target}"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="aye status deploy",
        description="deploy the status page Worker, its D1 database and the tenant of a manifest",
        epilog="examples:\n  aye status deploy --dry-run\n  aye status deploy ops/ayeops.yaml",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    add_manifest(parser)
    parser.add_argument("--database", default=DEFAULT_DATABASE, help="D1 database name")
    parser.add_argument("--hostname", help="status hostname, default status_page.hostname")
    parser.add_argument("--worker-dir", type=Path, default=WORKER_DIR, help="cloud/status")
    parser.add_argument(
        "--wait", type=float, default=DEFAULT_WAIT, help="seconds the checks wait for the domain"
    )
    parser.add_argument("--dry-run", action="store_true", help="print the steps, run none")
    return parser


def tolerant(stream: TextIO) -> None:
    reconfigure = getattr(stream, "reconfigure", None)
    if callable(reconfigure):
        reconfigure(errors="replace")


def main(argv: list[str]) -> int:
    tolerant(sys.stdout)
    tolerant(sys.stderr)
    args = build_parser().parse_args(argv)
    options = Options(args.database, args.hostname, args.worker_dir, args.wait, args.dry_run)
    manifest = load_manifest(find_manifest(args.manifest))
    return deploy_status(
        manifest, options, os.environ, wrangler_runner, http_fetch, sys.stdout, sys.stderr
    )
