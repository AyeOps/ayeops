from __future__ import annotations

import json
import os
import threading
import urllib.parse
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from ayeops.cli import main
from ayeops.secrets import SecretsError, env_text, normalize_path, parse_env

SENTINEL = "s3ntinel-value-never-printed"
CLIENT_ID = "client-1"
CLIENT_SECRET = "client-secret-1"
TOKEN = "token-1"


@dataclass
class Store:
    folders: dict[tuple[str, str], dict[str, str]] = field(default_factory=dict)
    imports: dict[tuple[str, str], list[dict[str, str]]] = field(default_factory=dict)
    requests: list[tuple[str, str, dict[str, Any]]] = field(default_factory=list)


class Handler(BaseHTTPRequestHandler):
    store: Store

    def log_message(self, format: str, *args: Any) -> None:
        return

    def answer(self, status: int, body: dict[str, Any]) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        data: dict[str, Any] = json.loads(self.rfile.read(length) or b"{}")
        return data

    def authorized(self) -> bool:
        if self.headers.get("Authorization") == f"Bearer {TOKEN}":
            return True
        self.answer(401, {"message": "Token missing"})
        return False

    def do_POST(self) -> None:
        url = urllib.parse.urlsplit(self.path)
        body = self.body()
        self.store.requests.append(("POST", url.path, body))
        if url.path == "/api/v1/auth/universal-auth/login":
            if (body.get("clientId"), body.get("clientSecret")) != (CLIENT_ID, CLIENT_SECRET):
                self.answer(401, {"message": "Invalid credentials"})
                return
            self.answer(200, {"accessToken": TOKEN, "expiresIn": 7200, "tokenType": "Bearer"})
            return
        if not self.authorized():
            return
        if url.path == "/api/v2/folders":
            parent = body["path"].rstrip("/")
            path = f"{parent}/{body['name']}"
            key = (body["environment"], path)
            if key in self.store.folders:
                self.answer(400, {"message": "Folder already exists"})
                return
            self.store.folders[key] = {}
            self.answer(200, {"folder": {"name": body["name"]}})
            return
        if url.path == "/api/v4/secrets/batch":
            folder = self.store.folders[(body["environment"], body["secretPath"])]
            for secret in body["secrets"]:
                folder[secret["secretKey"]] = secret["secretValue"]
            self.answer(200, {"secrets": []})
            return
        self.answer(404, {"message": "Route not found"})

    def do_PATCH(self) -> None:
        url = urllib.parse.urlsplit(self.path)
        body = self.body()
        self.store.requests.append(("PATCH", url.path, body))
        if not self.authorized():
            return
        key = urllib.parse.unquote(url.path.removeprefix("/api/v4/secrets/"))
        folder = self.store.folders[(body["environment"], body["secretPath"])]
        if key not in folder:
            self.answer(404, {"message": "Secret not found"})
            return
        folder[key] = body["secretValue"]
        self.answer(200, {"secret": {"secretKey": key}})

    def do_GET(self) -> None:
        url = urllib.parse.urlsplit(self.path)
        query = dict(urllib.parse.parse_qsl(url.query))
        self.store.requests.append(("GET", url.path, query))
        if not self.authorized():
            return
        if url.path != "/api/v4/secrets" or query.get("projectId") != "project-1":
            self.answer(404, {"message": "Route not found"})
            return
        key = (query["environment"], query["secretPath"])
        if key not in self.store.folders:
            message = f"Folder with path '{key[1]}' in environment '{key[0]}' was not found"
            self.answer(404, {"message": message})
            return
        values = query.get("viewSecretValue") == "true"

        def listing(pairs: dict[str, str]) -> list[dict[str, Any]]:
            return [
                {
                    "secretKey": k,
                    "secretValue": v if values else "",
                    "secretValueHidden": not values,
                }
                for k, v in pairs.items()
            ]

        imports = [
            {"secretPath": i["path"], "secrets": listing(self.store.folders[(key[0], i["path"])])}
            for i in self.store.imports.get(key, [])
        ]
        body: dict[str, Any] = {"secrets": listing(self.store.folders[key])}
        if query.get("includeImports") == "true":
            body["imports"] = imports
        self.answer(200, body)


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> Iterator[Store]:
    state = Store()
    handler = type("BoundHandler", (Handler,), {"store": state})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("AYE_SECRETS_URL", f"http://127.0.0.1:{server.server_port}")
    monkeypatch.setenv("AYE_SECRETS_CLIENT_ID", CLIENT_ID)
    monkeypatch.setenv("AYE_SECRETS_CLIENT_SECRET", CLIENT_SECRET)
    monkeypatch.setenv("AYE_SECRETS_PROJECT_ID", "project-1")
    monkeypatch.setenv("AYE_SECRETS_ENVIRONMENT", "prod")
    monkeypatch.setenv("AYE_SECRETS_PATH", "/web")
    yield state
    server.shutdown()
    server.server_close()


def printed(capsys: pytest.CaptureFixture[str]) -> str:
    captured = capsys.readouterr()
    return captured.out + captured.err


def test_pull_writes_the_folder_as_a_private_env_file(
    store: Store, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store.folders[("prod", "/web")] = {"TOKEN": SENTINEL, "GREETING": "hello world (1)"}
    out = tmp_path / ".env"
    assert main(["secrets", "pull", "-o", str(out)]) == 0
    assert out.read_text(encoding="utf-8") == f"GREETING='hello world (1)'\nTOKEN={SENTINEL}\n"
    if os.name == "posix":
        assert out.stat().st_mode & 0o777 == 0o600
    assert [p.name for p in tmp_path.iterdir()] == [".env"]
    output = printed(capsys)
    assert "2 secret(s) from prod:/web" in output
    assert SENTINEL not in output


def test_pull_merges_imports_under_the_folder_values(
    store: Store, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store.folders[("prod", "/shared")] = {"R2_KEY": "shared", "MODE": "shared"}
    store.folders[("prod", "/web")] = {"MODE": "own"}
    store.imports[("prod", "/web")] = [{"path": "/shared"}]
    out = tmp_path / ".env"
    assert main(["secrets", "pull", "-o", str(out)]) == 0
    assert out.read_text(encoding="utf-8") == "MODE=own\nR2_KEY=shared\n"


def test_pull_replaces_an_existing_file_in_one_step(store: Store, tmp_path: Path) -> None:
    store.folders[("prod", "/web")] = {"A": "new"}
    out = tmp_path / ".env"
    out.write_text("A=old\nSTALE=1\n", encoding="utf-8")
    assert main(["secrets", "pull", "-o", str(out)]) == 0
    assert out.read_text(encoding="utf-8") == "A=new\n"


def test_pull_refuses_a_value_a_env_line_cannot_carry_and_writes_nothing(
    store: Store, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store.folders[("prod", "/web")] = {"PEM": f"line one\n{SENTINEL}"}
    out = tmp_path / ".env"
    assert main(["secrets", "pull", "-o", str(out)]) == 1
    assert not out.exists()
    assert list(tmp_path.iterdir()) == []
    output = printed(capsys)
    assert "PEM:" in output
    assert SENTINEL not in output


def test_pull_with_a_wrong_client_secret_fails_without_a_file(
    store: Store,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("AYE_SECRETS_CLIENT_SECRET", SENTINEL)
    out = tmp_path / ".env"
    assert main(["secrets", "pull", "-o", str(out)]) == 1
    assert not out.exists()
    output = printed(capsys)
    assert "HTTP 401: Invalid credentials" in output
    assert SENTINEL not in output


def test_pull_of_a_missing_folder_is_an_error(
    store: Store, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["secrets", "pull", "-o", str(tmp_path / ".env"), "--path", "/nope"]) == 1
    assert "was not found" in printed(capsys)


def test_missing_identity_variables_are_named_and_exit_2(
    store: Store, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("AYE_SECRETS_CLIENT_SECRET")
    monkeypatch.delenv("AYE_SECRETS_ENVIRONMENT")
    assert main(["secrets", "pull"]) == 2
    assert "set AYE_SECRETS_CLIENT_SECRET, AYE_SECRETS_ENVIRONMENT" in printed(capsys)
    assert not store.requests


def test_flags_override_the_environment(store: Store, tmp_path: Path) -> None:
    store.folders[("staging", "/api")] = {"A": "1"}
    out = tmp_path / ".env"
    assert main(["secrets", "pull", "-o", str(out), "-e", "staging", "--path", "api/"]) == 0
    assert out.read_text(encoding="utf-8") == "A=1\n"


def test_identity_file_fills_what_the_environment_lacks(
    store: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.folders[("prod", "/web")] = {"A": "1"}
    monkeypatch.delenv("AYE_SECRETS_CLIENT_SECRET")
    identity = tmp_path / "identity.env"
    identity.write_text(
        f"AYE_SECRETS_CLIENT_SECRET='{CLIENT_SECRET}'\nOTHER=ignored\n", encoding="utf-8"
    )
    identity.chmod(0o600)
    out = tmp_path / ".env"
    assert main(["secrets", "pull", "-o", str(out), "--identity", str(identity)]) == 0
    assert out.read_text(encoding="utf-8") == "A=1\n"


@pytest.mark.skipif(os.name != "posix", reason="POSIX file modes")
def test_identity_file_readable_by_others_is_refused(
    store: Store, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    identity = tmp_path / "identity.env"
    identity.write_text(f"AYE_SECRETS_CLIENT_SECRET={CLIENT_SECRET}\n", encoding="utf-8")
    identity.chmod(0o644)
    assert main(["secrets", "pull", "--identity", str(identity)]) == 2
    assert "chmod 600" in printed(capsys)


def test_push_adds_only_missing_keys_and_never_prints_values(
    store: Store, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store.folders[("prod", "/web")] = {"KEEP": "rotated"}
    source = tmp_path / ".env"
    source.write_text(
        "# database\n\nKEEP=stale\nexport NEW_TOKEN=" + SENTINEL + "\nQUOTED='a b' # note\n",
        encoding="utf-8",
    )
    assert main(["secrets", "push", str(source)]) == 0
    assert store.folders[("prod", "/web")] == {
        "KEEP": "rotated",
        "NEW_TOKEN": SENTINEL,
        "QUOTED": "a b",
    }
    output = printed(capsys)
    assert "added NEW_TOKEN, QUOTED; kept 1 existing value(s)" in output
    assert SENTINEL not in output
    listing = next(q for m, p, q in store.requests if m == "GET")
    assert listing["viewSecretValue"] == "false"


def test_push_creates_a_missing_folder_path_first(store: Store, tmp_path: Path) -> None:
    store.folders[("prod", "/")] = {}
    store.folders[("prod", "/apps")] = {}
    source = tmp_path / ".env"
    source.write_text("A=1\n", encoding="utf-8")
    assert main(["secrets", "push", str(source), "--path", "/apps/web"]) == 0
    assert store.folders[("prod", "/apps/web")] == {"A": "1"}
    folders = [body for method, path, body in store.requests if path == "/api/v2/folders"]
    assert [(f["path"], f["name"]) for f in folders] == [("/", "apps"), ("/apps", "web")]


def test_push_with_nothing_new_writes_nothing(
    store: Store, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store.folders[("prod", "/web")] = {"A": "1"}
    source = tmp_path / ".env"
    source.write_text("A=2\n", encoding="utf-8")
    assert main(["secrets", "push", str(source)]) == 0
    assert store.folders[("prod", "/web")] == {"A": "1"}
    assert not [p for m, p, b in store.requests if p == "/api/v4/secrets/batch"]
    assert "added nothing; kept 1" in printed(capsys)


def test_push_of_a_broken_line_names_the_line_not_its_content(
    store: Store, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / ".env"
    source.write_text(f"A=1\n{SENTINEL}\n", encoding="utf-8")
    assert main(["secrets", "push", str(source)]) == 1
    output = printed(capsys)
    assert "line 2 is not KEY=value" in output
    assert SENTINEL not in output
    assert not store.requests


def test_parse_env_reads_compose_quoting() -> None:
    text = (
        "A=plain\n"
        "B = spaced \n"
        "C='single # kept'\n"
        'D="double \\"q\\" \\n and $x"\n'
        "E=value # comment\n"
        "F=\n"
        "  # indented comment\n"
    )
    assert parse_env(text) == {
        "A": "plain",
        "B": "spaced",
        "C": "single # kept",
        "D": 'double "q" \n and $x',
        "E": "value",
        "F": "",
    }


@pytest.mark.parametrize("line", ["A='open", 'A="open', "A='x' tail", "1A=x", "=x"])
def test_parse_env_rejects_malformed_lines(line: str) -> None:
    with pytest.raises(SecretsError, match="line 1"):
        parse_env(line)


def test_env_text_quotes_what_compose_or_sh_would_change() -> None:
    assert env_text({"B": "a$b", "A": "x", "C": "has space", "D": ""}) == (
        "A=x\nB='a$b'\nC='has space'\nD=\n"
    )
    with pytest.raises(SecretsError, match="E:"):
        env_text({"E": "it's"})


def test_paths_are_normalized_and_cannot_climb() -> None:
    assert normalize_path("apps//web/") == "/apps/web"
    assert normalize_path("") == "/"
    with pytest.raises(SecretsError):
        normalize_path("/apps/../root")
