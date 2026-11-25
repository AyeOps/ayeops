from __future__ import annotations

import email.message
import getpass
import hashlib
import os
import queue
import socket
import stat
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from ayeops import secretkeys, secrets
from ayeops.cli import main
from ayeops.registry import load_modules
from tests.conftest import WAIT
from tests.test_secrets_cli import SENTINEL, Store, printed, store

__all__ = ["store"]

FINGERPRINT = hashlib.sha256(SENTINEL.encode()).hexdigest()[:8]


def hidden(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setattr(getpass, "getpass", lambda prompt: value)


def test_set_creates_a_key_and_prints_only_metadata(
    store: Store, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    hidden(monkeypatch, SENTINEL)
    assert main(["secrets", "set", "DB_PASSWORD"]) == 0
    assert store.folders[("prod", "/web")]["DB_PASSWORD"] == SENTINEL
    output = printed(capsys)
    assert SENTINEL not in output
    assert "DB_PASSWORD in prod:/web: created" in output
    assert f"{len(SENTINEL)} characters" in output
    assert f"sha256 {FINGERPRINT}" in output


def test_set_prompts_without_echo_and_names_the_key(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    prompts: list[str] = []

    def fake(prompt: str) -> str:
        prompts.append(prompt)
        return SENTINEL

    monkeypatch.setattr(getpass, "getpass", fake)
    assert main(["secrets", "set", "DB_PASSWORD"]) == 0
    assert "DB_PASSWORD" in prompts[0]


def test_set_with_folder_flag_writes_that_folder(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    hidden(monkeypatch, SENTINEL)
    assert main(["secrets", "set", "TOKEN", "--folder", "/ops"]) == 0
    assert store.folders[("prod", "/ops")] == {"TOKEN": SENTINEL}


def test_set_refuses_to_replace_without_the_flag_and_keeps_the_value(
    store: Store, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    store.folders[("prod", "/web")] = {"DB_PASSWORD": "old-value-1"}
    hidden(monkeypatch, SENTINEL)
    assert main(["secrets", "set", "DB_PASSWORD"]) == 1
    assert store.folders[("prod", "/web")]["DB_PASSWORD"] == "old-value-1"
    output = printed(capsys)
    assert "DB_PASSWORD already exists in prod:/web, pass --replace" in output
    assert SENTINEL not in output


def test_set_does_not_ask_for_the_value_when_it_will_refuse(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.folders[("prod", "/web")] = {"DB_PASSWORD": "old-value-1"}

    def fail(prompt: str) -> str:
        raise AssertionError("asked")

    monkeypatch.setattr(getpass, "getpass", fail)
    assert main(["secrets", "set", "DB_PASSWORD"]) == 1


def test_set_replace_updates_and_says_replaced(
    store: Store, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    store.folders[("prod", "/web")] = {"DB_PASSWORD": "old-value-1"}
    hidden(monkeypatch, SENTINEL)
    assert main(["secrets", "set", "DB_PASSWORD", "--replace"]) == 0
    assert store.folders[("prod", "/web")]["DB_PASSWORD"] == SENTINEL
    assert [r[0] for r in store.requests if r[1].startswith("/api/v4/secrets/")] == ["PATCH"]
    assert not [r for r in store.requests if r[1] == "/api/v4/secrets/batch"]
    output = printed(capsys)
    assert "replaced" in output
    assert "created" not in output
    assert SENTINEL not in output


def test_set_rejects_an_empty_value(store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    hidden(monkeypatch, "  ")
    assert main(["secrets", "set", "DB_PASSWORD"]) == 1
    assert store.folders == {}


def test_set_rejects_a_bad_key_name_before_asking(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    hidden(monkeypatch, SENTINEL)
    assert main(["secrets", "set", "bad name"]) == 1
    assert store.folders == {}


def test_set_strips_the_whitespace_around_a_pasted_value(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    hidden(monkeypatch, f"  {SENTINEL}\n")
    assert main(["secrets", "set", "TOKEN"]) == 0
    assert store.folders[("prod", "/web")]["TOKEN"] == SENTINEL


@pytest.mark.skipif(os.name != "posix", reason="shell stub")
def test_set_from_clipboard_reads_then_clears_it(
    store: Store,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    board = tmp_path / "board"
    board.write_text(f"{SENTINEL}\n", encoding="utf-8")
    tool = tmp_path / "bin" / "xclip"
    tool.parent.mkdir()
    tool.write_text(
        f'#!/bin/sh\nif [ "$3" = "-o" ]; then /bin/cat {board}; else /bin/cat > {board}; fi\n',
        encoding="utf-8",
    )
    tool.chmod(tool.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", str(tool.parent))
    assert main(["secrets", "set", "TOKEN", "--from-clipboard"]) == 0
    assert store.folders[("prod", "/web")]["TOKEN"] == SENTINEL
    assert board.read_text(encoding="utf-8") == ""
    assert SENTINEL not in printed(capsys)


def stub(folder: Path, name: str, script: str) -> None:
    path = folder / name
    path.write_text(f"#!/bin/sh\n{script}\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


@pytest.mark.skipif(os.name != "posix", reason="shell stub")
def test_set_from_clipboard_clears_the_clipboard_it_read_even_when_the_tool_stays_behind(
    store: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    board = tmp_path / "board"
    board.write_text(SENTINEL, encoding="utf-8")
    other = tmp_path / "other"
    behind = tmp_path / "behind"
    folder = tmp_path / "bin"
    folder.mkdir()
    stub(
        folder,
        "xclip",
        f'if [ "$3" = "-o" ]; then /bin/cat {board}; else /bin/cat > {board};'
        f" {{ /bin/sleep {WAIT:.0f}; : > {behind}; }} & fi",
    )
    stub(folder, "wl-copy", f"/bin/cat > {other}")
    monkeypatch.setenv("PATH", str(folder))
    assert main(["secrets", "set", "TOKEN", "--from-clipboard"]) == 0
    assert not behind.exists()
    assert store.folders[("prod", "/web")]["TOKEN"] == SENTINEL
    assert board.read_text(encoding="utf-8") == ""
    assert not other.exists()


@pytest.mark.skipif(os.name != "posix", reason="shell stub")
def test_set_from_clipboard_stores_nothing_when_it_cannot_clear(
    store: Store,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    folder = tmp_path / "bin"
    folder.mkdir()
    stub(folder, "xclip", f'if [ "$3" = "-o" ]; then echo {SENTINEL}; else exit 1; fi')
    monkeypatch.setenv("PATH", str(folder))
    assert main(["secrets", "set", "TOKEN", "--from-clipboard"]) == 1
    assert store.folders == {}
    output = printed(capsys)
    assert "xclip could not clear the clipboard" in output
    assert SENTINEL not in output


def test_set_from_clipboard_without_a_tool_stores_nothing(
    store: Store, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))
    assert main(["secrets", "set", "TOKEN", "--from-clipboard"]) == 1
    assert store.folders == {}


def test_gen_creates_a_random_key_of_the_asked_size(
    store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["secrets", "gen", "SECRET_KEY", "--bytes", "24"]) == 0
    value = store.folders[("prod", "/web")]["SECRET_KEY"]
    assert len(value) == 32
    output = printed(capsys)
    assert value not in output
    assert "32 characters" in output
    assert f"sha256 {hashlib.sha256(value.encode()).hexdigest()[:8]}" in output


def test_gen_values_differ_between_runs(store: Store) -> None:
    assert main(["secrets", "gen", "A"]) == 0
    assert main(["secrets", "gen", "B"]) == 0
    folder = store.folders[("prod", "/web")]
    assert folder["A"] != folder["B"]


def test_gen_never_overwrites(store: Store, capsys: pytest.CaptureFixture[str]) -> None:
    store.folders[("prod", "/web")] = {"SECRET_KEY": "kept-value"}
    assert main(["secrets", "gen", "SECRET_KEY"]) == 1
    with pytest.raises(SystemExit):
        main(["secrets", "gen", "SECRET_KEY", "--replace"])
    assert store.folders[("prod", "/web")]["SECRET_KEY"] == "kept-value"


def test_gen_refuses_too_few_bytes(store: Store) -> None:
    assert main(["secrets", "gen", "SECRET_KEY", "--bytes", "4"]) == 1
    assert store.folders == {}


def folder_with(store: Store, **values: str) -> None:
    store.folders[("prod", "/ops")] = dict(values)


@pytest.mark.skipif(os.name != "posix", reason="shell child")
def test_run_passes_values_only_in_the_child_environment_and_masks_output(
    store: Store, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    folder_with(store, API_TOKEN=SENTINEL)
    monkeypatch.delenv("API_TOKEN", raising=False)
    code = main(
        [
            "secrets",
            "run",
            "--folder",
            "/ops",
            "--",
            "sh",
            "-c",
            'echo "token=$API_TOKEN"; echo "e=$API_TOKEN" >&2',
        ]
    )
    assert code == 0
    captured = capsys.readouterr()
    assert "token=***" in captured.out
    assert "e=***" in captured.err
    assert SENTINEL not in captured.out + captured.err
    assert "API_TOKEN" not in os.environ


@pytest.mark.skipif(os.name != "posix", reason="shell child")
def test_run_keeps_the_exit_code(store: Store) -> None:
    folder_with(store, A_B="value-1234")
    assert main(["secrets", "run", "--folder", "/ops", "--", "sh", "-c", "exit 7"]) == 7


@pytest.mark.skipif(os.name != "posix", reason="shell child")
def test_run_hides_the_identity_from_the_child(
    store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    folder_with(store, A_B="value-1234")
    assert (
        main(
            [
                "secrets",
                "run",
                "--folder",
                "/ops",
                "--",
                "sh",
                "-c",
                "env | grep AYE_SECRETS_ || echo none",
            ]
        )
        == 0
    )
    assert capsys.readouterr().out.strip() == "none"


@pytest.mark.skipif(os.name != "posix", reason="shell child")
def test_run_masks_each_line_of_a_multiline_value(
    store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    folder_with(store, PEM="first-line-secret\nsecond-line-secret")
    assert (
        main(["secrets", "run", "--folder", "/ops", "--", "sh", "-c", 'printf "%s\\n" "$PEM"']) == 0
    )
    out = capsys.readouterr().out
    assert "secret" not in out
    assert out.count("***") == 2


def test_run_without_a_command_is_a_usage_error(store: Store) -> None:
    assert main(["secrets", "run", "--folder", "/ops"]) == 2
    assert main(["secrets", "run", "--folder", "/ops", "--"]) == 2


def test_run_with_a_missing_program_fails_cleanly(
    store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    folder_with(store, A_B="value-1234")
    assert main(["secrets", "run", "--folder", "/ops", "--", "no-such-program-xyz"]) == 1
    assert "no-such-program-xyz" in printed(capsys)


def test_mask_text_masks_the_longest_value_first_and_skips_short_ones() -> None:
    known = secretkeys.masks(["abcd", "abcdefgh", "xy"])
    assert secretkeys.mask_text("abcdefgh abcd xy", known) == "*** *** xy"


class Fetch:
    def __init__(self, status: int = 200) -> None:
        self.status = status
        self.requests: list[urllib.request.Request] = []

    def __call__(self, request: urllib.request.Request, timeout: float) -> Any:
        self.requests.append(request)
        if self.status != 200:
            raise urllib.error.HTTPError(
                request.full_url, self.status, "no", email.message.Message(), None
            )
        return self

    def __enter__(self) -> Fetch:
        return self

    def __exit__(self, *args: object) -> None:
        return None


@pytest.mark.parametrize(
    ("key", "url", "header"),
    [
        (
            "CLOUDFLARE_API_TOKEN",
            "https://api.cloudflare.com/client/v4/user/tokens/verify",
            "Authorization",
        ),
        ("GITHUB_TOKEN", "https://api.github.com/user", "Authorization"),
        ("ANTHROPIC_ADMIN_KEY", "https://api.anthropic.com/v1/organizations/me", "X-api-key"),
        (
            "OPENAI_ADMIN_KEY",
            "https://api.openai.com/v1/organization/projects?limit=1",
            "Authorization",
        ),
    ],
)
def test_validation_asks_the_provider_with_the_value(key: str, url: str, header: str) -> None:
    fetch = Fetch()
    assert secretkeys.validate(key, SENTINEL, fetch) is not None
    request = fetch.requests[0]
    assert request.full_url == url
    assert SENTINEL in (request.get_header(header) or "")


@pytest.mark.parametrize(
    "key",
    [
        "DB_PASSWORD",
        "AYE_CLOUDFLARE_ACCOUNTS",
        "CLOUDFLARE_ACCOUNT_ID",
        "AYE_GITHUB_WEBHOOK_SECRET",
        "AYE_GITHUB_PORT",
        "AYE_ANTHROPIC_ADMIN_URL",
        "OPENAI_ADMIN_ORG",
    ],
)
def test_validation_skips_keys_it_has_no_check_for(key: str) -> None:
    fetch = Fetch()
    assert secretkeys.validate(key, SENTINEL, fetch) is None
    assert fetch.requests == []


def test_the_built_in_credentials_checked_are_exactly_the_provider_api_keys() -> None:
    declared = {
        env
        for module in load_modules().values()
        for entry in module.permissions
        for env in entry["env"]
    }
    assert {name for name in declared if secretkeys.check_url(name)} == {
        "AYE_ANTHROPIC_ADMIN_KEY",
        "AYE_CLOUDFLARE_ANALYTICS_TOKEN",
        "AYE_CLOUDFLARE_DNS_API_TOKEN",
        "AYE_GITHUB_TOKEN",
        "AYE_OPENAI_ADMIN_KEY",
    }


@pytest.mark.parametrize("value", [f"{SENTINEL}\nX-Other: 1", f"{SENTINEL} ", f"{SENTINEL}ф"])
def test_validation_refuses_a_value_no_header_can_carry_without_sending_or_naming_it(
    value: str,
) -> None:
    fetch = Fetch()
    with pytest.raises(secretkeys.KeyProblem) as caught:
        secretkeys.validate("AYE_GITHUB_TOKEN", value, fetch)
    assert fetch.requests == []
    assert SENTINEL not in str(caught.value)
    assert "--no-check" in str(caught.value)


def test_validation_hides_a_client_error_that_quotes_the_value() -> None:
    def quoting(request: Any, timeout: float) -> Any:
        raise ValueError(f"Invalid header value b'Bearer {SENTINEL}'")

    with pytest.raises(secretkeys.KeyProblem) as caught:
        secretkeys.validate("AYE_GITHUB_TOKEN", SENTINEL, quoting)
    assert SENTINEL not in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__


def site(handler: type[BaseHTTPRequestHandler]) -> tuple[ThreadingHTTPServer, str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_port}"


def test_validation_does_not_follow_a_redirect_with_the_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[str] = []

    class Thief(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            return

        def do_GET(self) -> None:
            seen.append(self.headers.get("Authorization") or "")
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

    thief, thief_url = site(Thief)

    class Moved(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            return

        def do_GET(self) -> None:
            self.send_response(302)
            self.send_header("Location", f"{thief_url}/user")
            self.send_header("Content-Length", "0")
            self.end_headers()

    moved, moved_url = site(Moved)
    monkeypatch.setattr(secretkeys, "CHECKS", (("GITHUB", "TOKEN", f"{moved_url}/user", "bearer"),))
    try:
        with pytest.raises(secretkeys.KeyProblem, match="HTTP 302"):
            secretkeys.validate("AYE_GITHUB_TOKEN", SENTINEL)
    finally:
        moved.shutdown()
        thief.shutdown()
        moved.server_close()
        thief.server_close()
    assert seen == []


def test_validation_rejection_names_the_host_and_never_the_value() -> None:
    with pytest.raises(secretkeys.KeyProblem) as caught:
        secretkeys.validate("GITHUB_TOKEN", SENTINEL, Fetch(401))
    assert "api.github.com" in str(caught.value)
    assert "401" in str(caught.value)
    assert SENTINEL not in str(caught.value)


def test_validation_unreachable_provider_is_an_error() -> None:
    def down(request: Any, timeout: float) -> Any:
        raise urllib.error.URLError("boom")

    with pytest.raises(secretkeys.KeyProblem, match="could not reach"):
        secretkeys.validate("GITHUB_TOKEN", SENTINEL, down)


def test_set_stores_nothing_when_the_check_fails_and_stores_with_no_check(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    hidden(monkeypatch, SENTINEL)
    fetch = Fetch(401)
    monkeypatch.setattr(
        secretkeys, "validate", lambda key, value: secretkeys_validate(fetch, key, value)
    )
    assert main(["secrets", "set", "GITHUB_TOKEN"]) == 1
    assert store.folders == {}
    assert main(["secrets", "set", "GITHUB_TOKEN", "--no-check"]) == 0
    assert store.folders[("prod", "/web")]["GITHUB_TOKEN"] == SENTINEL


def secretkeys_validate(fetch: Fetch, key: str, value: str) -> str | None:
    return validate_original(key, value, fetch)


validate_original = secretkeys.validate


def serve(
    key: str = "TOKEN", hint: str = "make it", timeout: float | None = None
) -> tuple[queue.Queue[str], queue.Queue[Any], threading.Thread]:
    urls: queue.Queue[str] = queue.Queue()
    outcome: queue.Queue[Any] = queue.Queue()

    def work() -> None:
        try:
            outcome.put(secretkeys.ask(key, hint, urls.put, timeout))
        except secretkeys.KeyProblem as error:
            outcome.put(error)

    thread = threading.Thread(target=work, daemon=True)
    thread.start()
    return urls, outcome, thread


def post(url: str, body: str, timeout: float = WAIT) -> int:
    request = urllib.request.Request(url, data=body.encode(), method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return int(response.status)
    except urllib.error.HTTPError as error:
        return int(error.code)


def get(url: str) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(url, timeout=WAIT) as response:
            return int(response.status), response.read().decode()
    except urllib.error.HTTPError as error:
        return int(error.code), ""


def test_ask_serves_a_form_on_loopback_with_a_random_path_and_the_hint() -> None:
    urls, outcome, thread = serve(hint="Create it at <dash>")
    url = urls.get(timeout=WAIT)
    assert url.startswith("http://127.0.0.1:")
    assert len(url.rsplit("/", 1)[1]) >= 20
    status, page = get(url)
    assert status == 200
    assert 'type="password"' in page
    assert "Create it at &lt;dash&gt;" in page
    assert post(url, "value=" + SENTINEL) == 200
    thread.join(WAIT)
    assert outcome.get(timeout=WAIT) == SENTINEL


def test_ask_paths_are_random_per_run() -> None:
    first, _, _ = serve(timeout=1)
    second, _, _ = serve(timeout=1)
    assert first.get(timeout=WAIT) != second.get(timeout=WAIT)


def test_ask_ignores_other_paths_and_empty_posts_until_a_real_submit() -> None:
    urls, outcome, thread = serve()
    url = urls.get(timeout=WAIT)
    base = url.rsplit("/", 1)[0]
    assert get(base + "/guess")[0] == 404
    assert raw_post(base + "/guess", "Content-Length: 10\r\n") == 404
    assert post(url, "value=") == 400
    assert thread.is_alive()
    assert post(url, "value=" + SENTINEL) == 200
    thread.join(WAIT)
    assert outcome.get(timeout=WAIT) == SENTINEL


def test_ask_closes_after_one_submit() -> None:
    urls, _outcome, thread = serve()
    url = urls.get(timeout=WAIT)
    assert post(url, "value=" + SENTINEL) == 200
    thread.join(WAIT)
    with pytest.raises(OSError):
        urllib.request.urlopen(url, timeout=WAIT)


def test_ask_times_out_without_a_submit() -> None:
    urls, outcome, thread = serve(timeout=0.3)
    urls.get(timeout=WAIT)
    thread.join(WAIT)
    assert "no value arrived" in str(outcome.get(timeout=WAIT))


def test_ask_refuses_a_foreign_host_header() -> None:
    urls, outcome, thread = serve()
    url = urls.get(timeout=WAIT)
    request = urllib.request.Request(url, headers={"Host": "evil.example"})
    with pytest.raises(urllib.error.HTTPError) as caught:
        urllib.request.urlopen(request, timeout=WAIT)
    assert caught.value.code == 404
    assert thread.is_alive()
    assert post(url, "value=" + SENTINEL) == 200
    thread.join(WAIT)
    assert outcome.get(timeout=WAIT) == SENTINEL


def raw_post(url: str, headers: str, body: bytes = b"") -> int:
    address = urllib.parse.urlsplit(url)
    port = address.port or 80
    head = f"POST {address.path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n{headers}\r\n"
    with socket.create_connection(("127.0.0.1", port), timeout=WAIT) as connection:
        connection.sendall(head.encode() + body)
        answer = b""
        while chunk := connection.recv(4096):
            answer += chunk
    return int(answer.split(b" ", 2)[1])


def test_ask_refuses_an_oversized_body_without_reading_it() -> None:
    urls, outcome, thread = serve()
    url = urls.get(timeout=WAIT)
    assert raw_post(url, f"Content-Length: {secretkeys.MAX_BODY + 1}\r\n") == 413
    assert thread.is_alive()
    assert post(url, "value=" + SENTINEL) == 200
    thread.join(WAIT)
    assert outcome.get(timeout=WAIT) == SENTINEL


def test_ask_answers_a_refused_body_it_was_sent_without_resetting_the_connection() -> None:
    urls, outcome, thread = serve()
    url = urls.get(timeout=WAIT)
    guess = url.rsplit("/", 1)[0] + "/guess"
    body = b"value=" + b"x" * (secretkeys.DRAIN_BYTES // 2)
    length = f"Content-Length: {len(body)}\r\n"
    for _ in range(200):
        assert raw_post(url, length, body) == 413
        assert raw_post(guess, length, body) == 404
    assert post(url, "value=" + SENTINEL) == 200
    thread.join(WAIT)
    assert outcome.get(timeout=WAIT) == SENTINEL


def stall(url: str, timeout: float) -> tuple[socket.socket, bytes]:
    address = urllib.parse.urlsplit(url)
    port = address.port or 80
    head = (
        f"POST {address.path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
        f"Content-Length: {secretkeys.MAX_BODY + 1}\r\n\r\n"
    )
    connection = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    connection.sendall(head.encode() + b"x" * 100)
    answer = b""
    while chunk := connection.recv(4096):
        answer += chunk
    return connection, answer


def test_ask_answers_before_the_refused_body_has_arrived(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(secretkeys, "DRAIN_SECONDS", WAIT)
    urls, outcome, thread = serve()
    url = urls.get(timeout=WAIT)
    connection, answer = stall(url, 30)
    connection.close()
    assert answer.startswith(b"HTTP/1.0 413 ")
    assert post(url, "value=" + SENTINEL) == 200
    thread.join(WAIT)
    assert outcome.get(timeout=WAIT) == SENTINEL


def test_ask_serves_the_next_request_while_a_refused_body_never_arrives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(secretkeys, "DRAIN_SECONDS", 0.3)
    monkeypatch.setattr(secretkeys, "REQUEST_SECONDS", WAIT)
    urls, outcome, thread = serve()
    url = urls.get(timeout=WAIT)
    connection, answer = stall(url, WAIT)
    with connection:
        assert answer.startswith(b"HTTP/1.0 413 ")
        assert post(url, "value=" + SENTINEL, timeout=30) == 200
    thread.join(WAIT)
    assert outcome.get(timeout=WAIT) == SENTINEL


@pytest.mark.parametrize(
    ("headers", "status"),
    [
        ("", 411),
        ("Content-Length: abc\r\n", 400),
        ("Content-Length: -1\r\n", 400),
        ("Content-Length: ²\r\n", 400),
    ],
)
def test_ask_answers_a_bad_length_and_prints_nothing(
    headers: str, status: int, capsys: pytest.CaptureFixture[str]
) -> None:
    urls, outcome, thread = serve()
    url = urls.get(timeout=WAIT)
    assert raw_post(url, headers) == status
    assert thread.is_alive()
    assert post(url, "value=" + SENTINEL) == 200
    thread.join(WAIT)
    assert outcome.get(timeout=WAIT) == SENTINEL
    assert printed(capsys) == ""


def test_ask_drops_an_idle_connection_so_the_person_can_still_submit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(secretkeys, "REQUEST_SECONDS", 0.3)
    urls, outcome, thread = serve()
    url = urls.get(timeout=WAIT)
    port = urllib.parse.urlsplit(url).port or 80
    with socket.create_connection(("127.0.0.1", port), timeout=WAIT):
        assert post(url, "value=" + SENTINEL) == 200
    thread.join(WAIT)
    assert outcome.get(timeout=WAIT) == SENTINEL


def test_ask_times_out_while_a_connection_stays_idle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(secretkeys, "REQUEST_SECONDS", 0.3)
    urls, outcome, thread = serve(timeout=1)
    url = urls.get(timeout=WAIT)
    port = urllib.parse.urlsplit(url).port or 80
    with socket.create_connection(("127.0.0.1", port), timeout=WAIT):
        thread.join(WAIT)
        assert "no value arrived" in str(outcome.get(timeout=WAIT))


def test_ask_page_forbids_scripts_framing_and_foreign_form_targets() -> None:
    urls, _outcome, thread = serve()
    url = urls.get(timeout=WAIT)
    with urllib.request.urlopen(url, timeout=WAIT) as response:
        policy = response.headers["Content-Security-Policy"]
        assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert "default-src 'none'" in policy
    assert "frame-ancestors 'none'" in policy
    assert "form-action 'self'" in policy
    assert post(url, "value=" + SENTINEL) == 200
    thread.join(WAIT)


def test_page_server_takes_its_port_exclusively_and_reports_no_errors(
    capsys: pytest.CaptureFixture[str],
) -> None:
    server = secretkeys.PageServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
    try:
        assert server.socket.getsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR) == 0
        exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
        if exclusive is not None:
            assert server.socket.getsockopt(socket.SOL_SOCKET, exclusive) == 1
        try:
            raise ValueError(SENTINEL)
        except ValueError:
            server.handle_error(None, ("127.0.0.1", 1))
    finally:
        server.server_close()
    assert printed(capsys) == ""


def test_ask_sets_no_store_and_logs_nothing(capsys: pytest.CaptureFixture[str]) -> None:
    urls, _outcome, thread = serve()
    url = urls.get(timeout=WAIT)
    with urllib.request.urlopen(url, timeout=WAIT) as response:
        assert response.headers["Cache-Control"] == "no-store"
    assert post(url, "value=" + SENTINEL) == 200
    thread.join(WAIT)
    assert SENTINEL not in printed(capsys)
    assert sys.stderr is not None


def test_ask_command_stores_the_submitted_value_and_prints_only_metadata(
    store: Store, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    urls: queue.Queue[str] = queue.Queue()
    monkeypatch.setattr(secrets, "announce", urls.put)
    codes: list[int] = []
    thread = threading.Thread(
        target=lambda: codes.append(main(["secrets", "ask", "TOKEN", "--hint", "dash"])),
        daemon=True,
    )
    thread.start()
    assert post(urls.get(timeout=WAIT), "value=" + SENTINEL) == 200
    thread.join(WAIT)
    assert codes == [0]
    assert store.folders[("prod", "/web")]["TOKEN"] == SENTINEL
    output = printed(capsys)
    assert SENTINEL not in output
    assert f"sha256 {FINGERPRINT}" in output


def test_ask_command_refuses_an_existing_key_before_opening_a_page(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.folders[("prod", "/web")] = {"TOKEN": "old-value-1"}

    def fail(message: str) -> None:
        raise AssertionError("page opened")

    monkeypatch.setattr(secrets, "announce", fail)
    assert main(["secrets", "ask", "TOKEN"]) == 1


def test_ask_command_times_out_with_exit_1_and_stores_nothing(
    store: Store, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(secretkeys, "ASK_SECONDS", 0.2)
    monkeypatch.setattr(secrets, "announce", lambda message: None)
    assert main(["secrets", "ask", "TOKEN"]) == 1
    assert store.folders == {}
    assert "no value arrived" in printed(capsys)


def test_announce_prints_the_address_and_no_value(capsys: pytest.CaptureFixture[str]) -> None:
    secrets.announce("http://127.0.0.1:1/abc")
    assert "http://127.0.0.1:1/abc" in capsys.readouterr().out
