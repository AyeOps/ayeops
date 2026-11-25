from __future__ import annotations

import copy
import io
import json
import os
import sqlite3
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from ayeops.cli import main
from ayeops.manifest import Manifest, load_manifest
from ayeops.status import status_document
from ayeops.status_deploy import (
    BOT_ENV,
    DEFAULT_DATABASE,
    MASK,
    PUSH_ENV,
    WORKER_DIR,
    WRANGLER,
    WRANGLER_VERSION,
    DeployError,
    Options,
    Response,
    Result,
    State,
    database_id,
    deploy_status,
    fill_config,
    mask,
    missing_env,
    parse_state,
    secret_sql,
    seed_changes,
    state_query,
    tolerant,
    toml_text,
    wrangler_env,
)
from tests.conftest import ROOT

FIXTURES = ROOT / "cloud" / "status" / "contract" / "fixtures"
MIGRATIONS = WORKER_DIR / "migrations"
TEMPLATE = WORKER_DIR / "wrangler.toml"
HOSTNAME = "status.acme.example"
SECRETS = {
    "CLOUDFLARE_ACCOUNT_ID": "acct-5e7c1d0a9b8f4e21",
    "CLOUDFLARE_API_TOKEN": "cf-token-Qw3rTy9uIoP0aSdF",
    BOT_ENV: "123456789:bot-token-ZxCvBnM0987",
    PUSH_ENV: "push-secret-4f1a8c2e9b7d6035",
}
ENV = {**SECRETS, "PATH": "/usr/bin"}


AUD = "fb52298607fa036764eb5e6e31b89bab96294fddc185ce6324890af471c374f0"


def fixture_manifest(notify: bool = True, access: dict[str, str] | None = None) -> Manifest:
    manifest = load_manifest(FIXTURES / "acme.yaml")
    data = copy.deepcopy(manifest.raw)
    if not notify:
        del data["status_page"]["notify"]
    if access is not None:
        if access:
            data["status_page"]["access"] = access
        else:
            del data["status_page"]["access"]
    return Manifest(data, manifest.base_dir)


@dataclass
class Call:
    args: list[str]
    stdin: bytes | None
    cwd: Path
    file: str = ""
    config: dict[str, Any] = field(default_factory=dict)


def test_no_wrangler_argument_carries_a_line_break_that_cmd_exe_would_cut() -> None:
    result = run_deploy()
    assert result.code == 0, result.err
    breaks = ("\n", "\r")
    broken = [
        arg for argv in result.wrangler.argvs for arg in argv if any(b in arg for b in breaks)
    ]
    assert broken == []


def test_wrangler_emoji_reach_a_cp1251_console_without_a_crash() -> None:
    raw = io.BytesIO()
    console = io.TextIOWrapper(raw, encoding="cp1251", newline="\n")
    tolerant(console)
    console.write(chr(0x26C5) + chr(0xFE0F) + " wrangler 4.148.0 " + chr(0x2705) + "\n")
    console.flush()
    assert raw.getvalue() == b"?? wrangler 4.148.0 ?\n"


class FakeWrangler:
    def __init__(self, databases: list[dict[str, str]] | None = None) -> None:
        self.databases = list(databases or [])
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.applied: set[str] = set()
        self.calls: list[Call] = []
        self.argvs: list[list[str]] = []
        self.fail: str = ""
        self.echo: str = ""
        self.secrets: dict[str, str] = {}
        self.at_deploy: list[str] = []

    def subcommand(self) -> list[str]:
        return [" ".join(call.args[:2]) for call in self.calls]

    def __call__(self, argv: list[str], stdin: bytes | None, cwd: Path) -> Result:
        self.argvs.append(list(argv))
        assert argv[: len(WRANGLER)] == list(WRANGLER)
        args = argv[len(WRANGLER) :]
        call = Call(args, stdin, cwd)
        self.calls.append(call)
        if "--config" in args:
            config = Path(args[args.index("--config") + 1])
            call.config = tomllib.loads(config.read_text(encoding="utf-8"))
        if self.fail and " ".join(args).startswith(self.fail):
            return Result(1, "", f"{self.fail} went wrong")
        if args[:2] == ["d1", "list"]:
            return Result(0, json.dumps(self.databases))
        if args[:2] == ["d1", "create"]:
            self.databases.append({"uuid": "0f1e2d3c-new", "name": args[2]})
            return Result(0, f"created {args[2]}")
        if args[:3] == ["d1", "migrations", "apply"]:
            for path in sorted(MIGRATIONS.glob("*.sql")):
                if path.name not in self.applied:
                    self.db.executescript(path.read_text(encoding="utf-8"))
                    self.applied.add(path.name)
            return Result(0, "migrations applied")
        if args[:2] == ["d1", "execute"] and "--command" in args:
            blocks = []
            for statement in args[args.index("--command") + 1].split(";"):
                rows = self.db.execute(statement).fetchall()
                blocks.append({"results": [dict(row) for row in rows], "success": True})
            return Result(0, json.dumps(blocks))
        if args[:2] == ["d1", "execute"] and "--file" in args:
            path = Path(args[args.index("--file") + 1])
            call.file = path.read_text(encoding="utf-8")
            self.db.executescript(call.file)
            return Result(0, "executed")
        if args[0] == "deploy":
            self.at_deploy = sorted(path.name for path in cwd.iterdir())
            return Result(0, f"deployed {self.echo}")
        if args[:2] == ["secret", "put"]:
            self.secrets[args[2]] = (stdin or b"").decode("utf-8")
            return Result(0, "secret set")
        raise AssertionError(f"unexpected wrangler call {args}")


class FakeSite:
    def __init__(self, wrangler: FakeWrangler, down: int = 0) -> None:
        self.wrangler = wrangler
        self.down = down
        self.requests: list[tuple[str, str, bytes | None]] = []
        self.push: int = 401

    def __call__(self, method: str, url: str, body: bytes | None) -> Response:
        self.requests.append((method, url, body))
        if self.down > 0:
            self.down -= 1
            return Response(0)
        if url.endswith("/api/push"):
            return Response(self.push, "application/json", b"{}")
        if url.endswith("/api/status"):
            rows = self.wrangler.db.execute(
                "SELECT id FROM components WHERE tenant_id = 'acme' ORDER BY position, id"
            ).fetchall()
            listing = json.dumps({"components": [{"id": row["id"]} for row in rows]})
            return Response(200, "application/json", listing.encode())
        return Response(200, "text/html; charset=utf-8", b"<!doctype html>")


@dataclass
class Run:
    code: int
    out: str
    err: str
    wrangler: FakeWrangler
    site: FakeSite
    slept: list[float]


def run_deploy(
    wrangler: FakeWrangler | None = None,
    manifest: Manifest | None = None,
    environ: dict[str, str] | None = None,
    site: FakeSite | None = None,
    **options: Any,
) -> Run:
    wrangler = wrangler or FakeWrangler()
    site = site or FakeSite(wrangler)
    out, err = io.StringIO(), io.StringIO()
    slept: list[float] = []
    now = [1792742460.0]

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        now[0] += seconds

    code = deploy_status(
        manifest or fixture_manifest(),
        Options(**options),
        ENV if environ is None else environ,
        wrangler,
        site,
        out,
        err,
        sleep,
        lambda: now[0],
    )
    return Run(code, out.getvalue(), err.getvalue(), wrangler, site, slept)


def test_wrangler_is_the_version_the_docs_pin() -> None:
    assert WRANGLER == ("npx", "--yes", "wrangler@4.148.0")
    docs = (ROOT / "docs" / "status.md").read_text(encoding="utf-8")
    assert f"npx wrangler@{WRANGLER_VERSION}" in docs


def test_a_first_deploy_creates_seeds_deploys_and_checks_in_order() -> None:
    result = run_deploy()
    assert result.code == 0, result.err
    assert result.wrangler.subcommand() == [
        "d1 list",
        "d1 create",
        "d1 list",
        "d1 migrations",
        "d1 execute",
        "d1 execute",
        "d1 execute",
        "deploy --config",
        "secret put",
    ]
    seed, secret = (call.file for call in result.wrangler.calls[5:7])
    assert seed.startswith("INSERT INTO tenants")
    assert secret == secret_sql("acme", SECRETS[PUSH_ENV])
    assert [(m, u) for m, u, _ in result.site.requests] == [
        ("GET", f"https://{HOSTNAME}/"),
        ("GET", f"https://{HOSTNAME}/api/status"),
        ("POST", f"https://{HOSTNAME}/api/push"),
    ]
    assert result.wrangler.secrets == {BOT_ENV: SECRETS[BOT_ENV]}
    assert f"https://{HOSTNAME} is live for tenant acme" in result.out
    assert (
        f"admin accepts Cloudflare Access tokens of team acme for the application with AUD tag"
        f" {AUD} on {HOSTNAME}/api/admin" in result.out
    )
    tenant = result.wrangler.db.execute("SELECT access_team, access_aud FROM tenants").fetchone()
    assert tuple(tenant) == ("acme", AUD)


def test_without_access_the_admin_stays_closed_and_the_deploy_says_how_to_open_it() -> None:
    result = run_deploy(manifest=fixture_manifest(access={}))
    assert result.code == 0, result.err
    tenant = result.wrangler.db.execute("SELECT access_team, access_aud FROM tenants").fetchone()
    assert tuple(tenant) == (None, None)
    assert (
        f"admin is closed: create a Cloudflare Access application for {HOSTNAME}/api/admin,"
        " put its team and AUD tag into status_page.access and deploy again" in result.out
    )


def test_a_new_access_application_seeds_the_tenant_again() -> None:
    wrangler = FakeWrangler()
    assert run_deploy(wrangler, manifest=fixture_manifest(access={})).code == 0
    wrangler.calls.clear()
    other = "0" * 64
    result = run_deploy(wrangler, manifest=fixture_manifest(access={"team": "acme", "aud": other}))
    assert result.code == 0, result.err
    assert "tenant acme: access_team, access_aud changed" in result.out
    tenant = wrangler.db.execute("SELECT access_team, access_aud FROM tenants").fetchone()
    assert tuple(tenant) == ("acme", other)


def test_the_unsigned_push_is_a_valid_body_without_a_signature() -> None:
    result = run_deploy()
    body = json.loads(result.site.requests[2][2] or b"")
    assert body == {
        "tenant": "acme",
        "ts": 1792742460,
        "components": [{"id": "website", "state": "operational"}],
    }


def test_an_existing_database_is_reused_by_name() -> None:
    wrangler = FakeWrangler(
        [{"uuid": "other", "name": "x"}, {"uuid": "abc-123", "name": "ayeops-status"}]
    )
    result = run_deploy(wrangler)
    assert result.code == 0, result.err
    assert "d1 create" not in wrangler.subcommand()
    deployed = next(call for call in wrangler.calls if call.args[0] == "deploy")
    assert deployed.config["d1_databases"][0]["database_id"] == "abc-123"
    assert "ayeops-status exists: abc-123" in result.out


def test_a_second_run_skips_the_seed_but_sets_the_secrets_again() -> None:
    wrangler = FakeWrangler()
    assert run_deploy(wrangler).code == 0
    wrangler.calls.clear()
    second = run_deploy(wrangler)
    assert second.code == 0, second.err
    files = [call.file for call in wrangler.calls if call.file]
    assert files == [secret_sql("acme", SECRETS[PUSH_ENV])]
    assert "tenant acme and its 4 component(s) are up to date" in second.out
    assert "d1 create" not in wrangler.subcommand()
    assert wrangler.subcommand()[-2:] == ["deploy --config", "secret put"]


def test_a_missing_component_seeds_again_and_says_which() -> None:
    wrangler = FakeWrangler()
    assert run_deploy(wrangler).code == 0
    wrangler.db.execute("DELETE FROM components WHERE id = 'docs'")
    wrangler.calls.clear()
    result = run_deploy(wrangler)
    assert result.code == 0
    assert "component docs is missing" in result.out
    assert [call.file.split(" ")[0] for call in wrangler.calls if call.file] == ["INSERT", "UPDATE"]
    count = wrangler.db.execute("SELECT count(*) FROM components").fetchone()[0]
    assert count == 4


def test_other_tenants_keep_their_routes() -> None:
    wrangler = FakeWrangler()
    wrangler([*WRANGLER, "d1", "migrations", "apply", DEFAULT_DATABASE], None, Path())
    wrangler.db.execute(
        "INSERT INTO tenants (id, hostname, title) VALUES ('beta', 'status.beta.example', 'Beta')"
    )
    result = run_deploy(wrangler)
    assert result.code == 0, result.err
    deployed = next(call for call in wrangler.calls if call.args[0] == "deploy")
    assert [route["pattern"] for route in deployed.config["routes"]] == [
        "status.acme.example",
        "status.beta.example",
    ]


def test_a_hostname_held_by_another_tenant_stops_before_the_seed() -> None:
    wrangler = FakeWrangler()
    wrangler([*WRANGLER, "d1", "migrations", "apply", DEFAULT_DATABASE], None, Path())
    wrangler.db.execute(
        "INSERT INTO tenants (id, hostname, title) VALUES ('beta', 'status.acme.example', 'Beta')"
    )
    result = run_deploy(wrangler)
    assert result.code == 1
    assert "status.acme.example already belongs to tenant beta" in result.err
    assert not [call for call in wrangler.calls if call.file or call.args[0] == "deploy"]


@pytest.mark.parametrize("name", sorted(SECRETS))
def test_a_missing_variable_is_named_and_nothing_runs(name: str) -> None:
    environ = {key: value for key, value in ENV.items() if key != name}
    result = run_deploy(environ=environ)
    assert result.code == 2
    assert f"missing environment variable(s): {name}" in result.err
    assert not result.wrangler.calls
    for value in SECRETS.values():
        assert value not in result.err + result.out


def test_every_missing_variable_is_named_at_once() -> None:
    result = run_deploy(environ={PUSH_ENV: "  "})
    assert (
        "missing environment variable(s): CLOUDFLARE_ACCOUNT_ID, CLOUDFLARE_API_TOKEN,"
        " AYE_STATUS_PUSH_SECRET, TELEGRAM_BOT_TOKEN" in result.err
    )
    assert "infisical run --env=<env> --path=<path> --" in result.err


def test_without_notify_the_bot_token_is_optional_and_skipped() -> None:
    environ = {key: value for key, value in ENV.items() if key != BOT_ENV}
    result = run_deploy(manifest=fixture_manifest(notify=False), environ=environ)
    assert result.code == 0, result.err
    assert "secret put" not in result.wrangler.subcommand()
    assert f"skip {BOT_ENV}" in result.out


def test_missing_env_requires_the_bot_token_only_with_notify() -> None:
    assert missing_env({}, False) == [
        "CLOUDFLARE_ACCOUNT_ID",
        "CLOUDFLARE_API_TOKEN",
        "AYE_STATUS_PUSH_SECRET",
    ]
    assert missing_env(ENV, True) == []


def left_behind(result: Run) -> list[Path]:
    workdirs = {call.cwd for call in result.wrangler.calls}
    assert len(workdirs) == 1
    return [path for path in workdirs if path.exists()]


def test_no_secret_reaches_an_argv_the_output_or_a_file_left_behind() -> None:
    wrangler = FakeWrangler()
    wrangler.echo = SECRETS["CLOUDFLARE_API_TOKEN"] + " " + SECRETS["CLOUDFLARE_ACCOUNT_ID"]
    result = run_deploy(wrangler)
    assert result.code == 0, result.err
    text = result.out + result.err + json.dumps(wrangler.argvs)
    text += json.dumps([call.config for call in wrangler.calls])
    for value in SECRETS.values():
        assert value not in text
    assert f"deployed {MASK} {MASK}" in result.out
    assert "push-secret.sql" not in wrangler.at_deploy
    assert "wrangler.toml" in wrangler.at_deploy
    assert left_behind(result) == []
    secret_call = next(call for call in wrangler.calls if call.args[:2] == ["secret", "put"])
    assert secret_call.stdin == SECRETS[BOT_ENV].encode()
    assert BOT_ENV in secret_call.args
    assert all(call.stdin is None for call in wrangler.calls if call is not secret_call)


def test_the_push_secret_file_is_private_and_gone_after_a_failure() -> None:
    wrangler = FakeWrangler()
    seen: list[int] = []
    inner = wrangler.__call__

    def runner(argv: list[str], stdin: bytes | None, cwd: Path) -> Result:
        if "--file" in argv and "push-secret" in argv[argv.index("--file") + 1]:
            path = Path(argv[argv.index("--file") + 1])
            seen.append(path.stat().st_mode & 0o777)
            return Result(1, "", "import failed")
        return inner(argv, stdin, cwd)

    out, err = io.StringIO(), io.StringIO()
    code = deploy_status(
        fixture_manifest(),
        Options(),
        ENV,
        runner,
        FakeSite(wrangler),
        out,
        err,
        lambda _: None,
        lambda: 0.0,
    )
    assert code == 1
    assert "set the push secret of acme from AYE_STATUS_PUSH_SECRET failed" in err.getvalue()
    assert len(seen) == 1
    if os.name == "posix":
        assert seen == [0o600]
    workdirs = {call.cwd for call in wrangler.calls}
    assert all(not path.exists() for path in workdirs)
    assert "deploy" not in [call.args[0] for call in wrangler.calls]


def test_the_wrangler_env_drops_the_secrets_wrangler_does_not_need() -> None:
    assert wrangler_env(ENV) == {
        "CLOUDFLARE_ACCOUNT_ID": SECRETS["CLOUDFLARE_ACCOUNT_ID"],
        "CLOUDFLARE_API_TOKEN": SECRETS["CLOUDFLARE_API_TOKEN"],
        "PATH": "/usr/bin",
    }


def test_a_failed_step_stops_the_deploy_and_cleans_up() -> None:
    wrangler = FakeWrangler()
    wrangler.fail = "d1 migrations"
    result = run_deploy(wrangler)
    assert result.code == 1
    assert "apply the D1 migrations failed with exit code 1" in result.err
    assert "d1 migrations went wrong" in result.out
    assert wrangler.subcommand()[-1] == "d1 migrations"
    assert left_behind(result) == []
    assert not result.site.requests


def test_the_dry_run_prints_every_step_and_runs_nothing() -> None:
    def refuse(*_: Any) -> Any:
        raise AssertionError("the dry run must not run anything")

    out, err = io.StringIO(), io.StringIO()
    code = deploy_status(
        fixture_manifest(),
        Options(dry_run=True),
        ENV,
        refuse,
        refuse,
        out,
        err,
        refuse,
        lambda: 0.0,
    )
    text = out.getvalue()
    assert code == 0, err.getvalue()
    for step in (
        "wrangler@4.148.0 d1 list --json",
        "wrangler@4.148.0 d1 create ayeops-status",
        "d1 migrations apply ayeops-status --remote",
        "d1 execute ayeops-status --remote --json",
        "only when D1 differs",
        "push-secret.sql",
        f"UPDATE tenants SET secret = '{MASK}' WHERE id = 'acme';",
        "wrangler@4.148.0 deploy --config",
        f"secret put {BOT_ENV} --config",
        f"< {BOT_ENV}",
        f"GET https://{HOSTNAME}/ wants 200 text/html",
        f"POST https://{HOSTNAME}/api/push wants 401",
        "routes: status.acme.example",
        "admin accepts Cloudflare Access tokens of team acme for the application with AUD tag"
        f" {AUD}",
        "dry run: nothing was changed",
    ):
        assert step in text, step
    for value in SECRETS.values():
        assert value not in text + err.getvalue()


def test_the_checks_wait_for_the_new_domain() -> None:
    wrangler = FakeWrangler()
    result = run_deploy(wrangler, site=FakeSite(wrangler, down=3))
    assert result.code == 0, result.err
    assert result.slept == [10.0, 10.0, 10.0]
    assert "no answer, retrying in 10s" in result.out


def test_the_checks_give_up_after_the_wait() -> None:
    wrangler = FakeWrangler()
    result = run_deploy(wrangler, site=FakeSite(wrangler, down=1000), wait=30)
    assert result.code == 1
    assert "public page gave no answer, wanted 200 text/html" in result.err
    assert len(result.slept) == 3


def test_a_push_that_is_not_refused_fails_the_check() -> None:
    wrangler = FakeWrangler()
    site = FakeSite(wrangler)
    site.push = 202
    result = run_deploy(wrangler, site=site, wait=0)
    assert result.code == 1
    assert "unsigned push gave 202, wanted 401" in result.err


def test_a_status_json_without_the_seeded_components_fails_the_check() -> None:
    wrangler = FakeWrangler()
    site = FakeSite(wrangler)
    original = site.__call__

    def stale(method: str, url: str, body: bytes | None) -> Response:
        if url.endswith("/api/status"):
            return Response(200, "application/json", b'{"components": [{"id": "website"}]}')
        return original(method, url, body)

    out, err = io.StringIO(), io.StringIO()
    code = deploy_status(
        fixture_manifest(),
        Options(wait=0),
        ENV,
        wrangler,
        stale,
        out,
        err,
        lambda _: None,
        lambda: 0.0,
    )
    assert code == 1
    assert "status JSON gave 200, wanted 200 with the components website, api" in err.getvalue()


def test_the_committed_template_is_never_touched() -> None:
    before = TEMPLATE.read_bytes()
    assert run_deploy().code == 0
    assert TEMPLATE.read_bytes() == before
    assert b"<d1-database-id>" in before


def test_the_config_fills_the_template_with_absolute_paths() -> None:
    template = tomllib.loads(TEMPLATE.read_text(encoding="utf-8"))
    config = fill_config(template, WORKER_DIR, "status-db", "id-1", ["a.example", "b.example"])
    parsed = tomllib.loads(toml_text(config))
    assert parsed == config
    assert parsed["main"] == (WORKER_DIR / "src" / "index.ts").as_posix()
    assert parsed["workers_dev"] is False
    assert parsed["triggers"] == {"crons": ["* * * * *"]}
    assert parsed["d1_databases"] == [
        {
            "binding": "DB",
            "database_name": "status-db",
            "database_id": "id-1",
            "migrations_dir": (WORKER_DIR / "migrations").as_posix(),
        }
    ]
    assert parsed["routes"] == [
        {"pattern": "a.example", "custom_domain": True},
        {"pattern": "b.example", "custom_domain": True},
    ]
    assert template["d1_databases"][0]["database_id"] == "<d1-database-id>"


def test_a_template_without_the_db_binding_is_refused() -> None:
    with pytest.raises(DeployError, match="binding DB"):
        fill_config({"main": "x.ts", "d1_databases": []}, WORKER_DIR, "d", "i", ["a.example"])


def test_a_value_toml_text_cannot_write_is_refused() -> None:
    with pytest.raises(DeployError, match="cannot write"):
        toml_text({"limits": {"nested": {"x": 1}}})
    assert (
        toml_text({"a": 1, "b": "x", "c": ["y"], "d": True})
        == 'a = 1\nb = "x"\nc = ["y"]\nd = true\n'
    )


def test_flags_override_the_hostname_and_the_database() -> None:
    wrangler = FakeWrangler()
    result = run_deploy(wrangler, hostname="status.other.example", database="tenant-db")
    assert result.code == 0, result.err
    assert wrangler.calls[1].args == ["d1", "create", "tenant-db"]
    deployed = next(call for call in wrangler.calls if call.args[0] == "deploy")
    assert deployed.config["routes"] == [{"pattern": "status.other.example", "custom_domain": True}]
    assert deployed.config["d1_databases"][0]["database_name"] == "tenant-db"
    assert "https://status.other.example/api/push" in [url for _, url, _ in result.site.requests]
    row = wrangler.db.execute("SELECT hostname FROM tenants WHERE id = 'acme'").fetchone()
    assert row[0] == "status.other.example"


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"hostname": "Not A Host"}, "is not a hostname"),
        ({"database": "Bad_Name"}, "is not a D1 database name"),
    ],
)
def test_bad_flags_are_refused_before_anything_runs(options: dict[str, Any], message: str) -> None:
    result = run_deploy(None, None, None, None, **options)
    assert result.code == 2
    assert message in result.err
    assert not result.wrangler.calls


def test_a_manifest_without_a_status_hostname_is_refused() -> None:
    manifest = fixture_manifest()
    data = copy.deepcopy(manifest.raw)
    del data["status_page"]
    result = run_deploy(manifest=Manifest(data, manifest.base_dir))
    assert result.code == 2
    assert "status_page.hostname is not set" in result.err


def test_a_manifest_without_public_components_is_refused() -> None:
    manifest = fixture_manifest()
    data = copy.deepcopy(manifest.raw)
    for service in data["services"].values():
        service["status"] = {"internal": "not public"}
    result = run_deploy(manifest=Manifest(data, manifest.base_dir))
    assert result.code == 2
    assert "no public component" in result.err


def test_a_missing_worker_dir_is_named(tmp_path: Path) -> None:
    result = run_deploy(worker_dir=tmp_path)
    assert result.code == 1
    assert "pass --worker-dir" in result.err


def test_a_created_database_that_never_shows_up_fails() -> None:
    wrangler = FakeWrangler()
    original = wrangler.__call__

    def lost(argv: list[str], stdin: bytes | None, cwd: Path) -> Result:
        result = original(argv, stdin, cwd)
        wrangler.databases.clear()
        return result

    out, err = io.StringIO(), io.StringIO()
    code = deploy_status(
        fixture_manifest(),
        Options(),
        ENV,
        lost,
        FakeSite(wrangler),
        out,
        err,
        lambda _: None,
        lambda: 0.0,
    )
    assert code == 1
    assert "ayeops-status was created but d1 list does not show it" in err.getvalue()


def test_seed_changes_name_each_difference() -> None:
    document = status_document(fixture_manifest())
    assert seed_changes(document, State()) == ["tenant acme and its 4 component(s) are missing"]
    state = State(
        tenants={
            "acme": {
                "id": "acme",
                "hostname": HOSTNAME,
                "title": "Old",
                "locale": "uk",
                "notify_chat": "-1001234567890",
                "notify_min": "no_data",
                "access_team": "acme",
                "access_aud": AUD,
            }
        },
        components=[
            {"id": "website", "title": "Website", "position": 0, "push": 0},
            {"id": "api", "title": "API", "position": 1, "push": 0},
            {"id": "gone", "title": "Gone", "position": 2, "push": 0},
        ],
        probes=[("website", "https://acme.example")],
    )
    assert seed_changes(document, state) == [
        "tenant acme: title changed",
        "component api changed",
        "component payments-checkout is missing",
        "component docs is missing",
        "component gone left the manifest",
        "probe URLs changed",
    ]


def test_parse_state_reads_the_three_result_blocks() -> None:
    text = "noise\n" + json.dumps(
        [
            {"results": [{"id": "acme", "hostname": HOSTNAME}]},
            {"results": [{"id": "api", "title": "API", "position": 0, "push": 1}]},
            {"results": [{"component_id": "api", "url": "https://x.example"}]},
        ]
    )
    state = parse_state(text)
    assert state.tenants == {"acme": {"id": "acme", "hostname": HOSTNAME}}
    assert state.components == [{"id": "api", "title": "API", "position": 0, "push": 1}]
    assert state.probes == [("api", "https://x.example")]
    with pytest.raises(DeployError, match="three queries"):
        parse_state(json.dumps([{"results": []}]))
    with pytest.raises(DeployError, match="no JSON"):
        parse_state("nothing here")


def test_database_id_finds_the_uuid_by_name() -> None:
    listing = json.dumps([{"uuid": "1", "name": "a"}, {"uuid": "2", "name": "ayeops-status"}])
    assert database_id(listing, "ayeops-status") == "2"
    assert database_id(listing, "missing") is None


def test_state_query_quotes_the_tenant_on_one_line_for_npx_cmd() -> None:
    query = state_query("acme")
    assert query.count(";") == 2
    assert "\n" not in query and "\r" not in query
    assert "WHERE tenant_id = 'acme' ORDER BY position, id" in query
    assert (
        "SELECT id, hostname, title, locale, notify_chat, notify_min, access_team, access_aud"
        " FROM tenants" in query
    )


def test_secret_sql_escapes_quotes() -> None:
    assert secret_sql("acme", "it's") == "UPDATE tenants SET secret = 'it''s' WHERE id = 'acme';\n"


def test_mask_replaces_the_longest_secret_first() -> None:
    assert mask("abc abcdef", ["abc", "abcdef", ""]) == f"{MASK} {MASK}"
    assert mask("plain", []) == "plain"


def test_the_cli_dispatches_status_deploy(monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
    for name in SECRETS:
        monkeypatch.delenv(name, raising=False)
    assert main(["status", "deploy", str(FIXTURES / "acme.yaml"), "--dry-run"]) == 2
    assert "missing environment variable(s)" in capsys.readouterr().err
