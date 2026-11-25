from __future__ import annotations

import base64
import hashlib
import hmac
import html
import http.client
import http.server
import io
import secrets as randomness
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable, Mapping, Sequence
from email.message import Message
from typing import Any, BinaryIO, TextIO

ASK_SECONDS = 600
REQUEST_SECONDS = 10.0
MAX_BODY = 65536
DRAIN_BYTES = 1048576
DRAIN_SECONDS = 2.0
DRAIN_CHUNK = 65536
MIN_MASKED = 4
MASK = "***"
CHECK_TIMEOUT = 15
Opener = Callable[..., Any]
MIN_BYTES = 8
DEFAULT_BYTES = 24
GARAGE_PREFIX = "GK"
GARAGE_BYTES = 12
MAKERS: dict[str, Callable[[int], str]] = {
    "hex": randomness.token_hex,
    "base64": lambda size: base64.b64encode(randomness.token_bytes(size)).decode(),
    "base64url": randomness.token_urlsafe,
}
FORMATS = (*MAKERS, "garage-key-id")

CHECKS: tuple[tuple[str, str, str, str], ...] = (
    ("CLOUDFLARE", "TOKEN", "https://api.cloudflare.com/client/v4/user/tokens/verify", "bearer"),
    ("GITHUB", "TOKEN", "https://api.github.com/user", "bearer"),
    ("ANTHROPIC_ADMIN", "KEY", "https://api.anthropic.com/v1/organizations/me", "anthropic"),
    ("OPENAI_ADMIN", "KEY", "https://api.openai.com/v1/organization/projects?limit=1", "bearer"),
)

CLIPBOARDS: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (("pbpaste",), ("pbcopy",)),
    (("wl-paste", "--no-newline"), ("wl-copy", "--clear")),
    (("xclip", "-selection", "clipboard", "-o"), ("xclip", "-selection", "clipboard", "-i")),
    (("xsel", "--clipboard", "--output"), ("xsel", "--clipboard", "--input")),
)


class KeyProblem(Exception):
    pass


def fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:8]


def describe(key: str, where: str, value: str, created: bool) -> str:
    state = "created" if created else "replaced"
    return f"{key} in {where}: {state}, {len(value)} characters, sha256 {fingerprint(value)}"


def generate(size: int, kind: str = "base64url") -> str:
    if kind == "garage-key-id":
        return GARAGE_PREFIX + randomness.token_hex(GARAGE_BYTES)
    if size < MIN_BYTES:
        raise KeyProblem(f"--bytes must be at least {MIN_BYTES}")
    return MAKERS[kind](size)


def length(kind: str, size: int) -> int:
    if kind == "garage-key-id":
        return len(GARAGE_PREFIX) + 2 * GARAGE_BYTES
    if kind == "hex":
        return 2 * size
    if kind == "base64":
        return 4 * ((size + 2) // 3)
    return (4 * size + 2) // 3


def shape(kind: str, size: int | None) -> str:
    if kind == "garage-key-id":
        return f"{kind}, GK and {2 * GARAGE_BYTES} hex characters"
    count = size or 0
    return f"{kind}, {count} bytes, {length(kind, count)} characters"


class NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


CHECKER = urllib.request.build_opener(NoRedirects)


def check_url(key: str) -> tuple[str, str] | None:
    name = key.upper().removeprefix("AYE_")
    for prefix, suffix, url, style in CHECKS:
        if name.startswith(prefix) and name.endswith(suffix):
            return url, style
    return None


def header_safe(value: str) -> bool:
    return all(33 <= ord(char) <= 126 for char in value)


def validate(key: str, value: str, opener: Opener = CHECKER.open) -> str | None:
    found = check_url(key)
    if found is None:
        return None
    url, style = found
    host = urllib.parse.urlsplit(url).netloc
    if not header_safe(value):
        raise KeyProblem(
            f"{key} holds a character outside printable ASCII or a space, which no {host} key"
            " has; nothing stored, --no-check skips the check"
        )
    headers = {"Accept": "application/json", "User-Agent": "ayeops"}
    if style == "anthropic":
        headers["x-api-key"] = value
        headers["anthropic-version"] = "2023-06-01"
    else:
        headers["Authorization"] = f"Bearer {value}"
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with opener(request, timeout=CHECK_TIMEOUT):
            pass
    except urllib.error.HTTPError as error:
        raise KeyProblem(f"{host} rejected {key} (HTTP {error.code}), nothing stored") from None
    except (urllib.error.URLError, http.client.HTTPException, ValueError, OSError):
        raise KeyProblem(f"could not reach {host} to check {key}, nothing stored") from None
    return host


def read_clipboard() -> tuple[str, tuple[str, ...]]:
    for reader, clearer in CLIPBOARDS:
        try:
            done = subprocess.run(
                reader, stdin=subprocess.DEVNULL, capture_output=True, check=True, timeout=10
            )
        except (OSError, subprocess.SubprocessError):
            continue
        return done.stdout.decode(errors="replace"), clearer
    raise KeyProblem("no clipboard tool found (pbpaste, wl-paste, xclip, xsel)")


def clear_clipboard(clearer: Sequence[str]) -> None:
    try:
        subprocess.run(
            list(clearer),
            input=b"",
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        raise KeyProblem(f"{clearer[0]} could not clear the clipboard, clear it by hand") from None


def masks(values: Iterable[str]) -> list[str]:
    found: set[str] = set()
    for value in values:
        for part in [value, *value.splitlines()]:
            if len(part) >= MIN_MASKED:
                found.add(part)
    return sorted(found, key=len, reverse=True)


def mask_text(text: str, known: Sequence[str]) -> str:
    for value in known:
        text = text.replace(value, MASK)
    return text


def _pump(source: BinaryIO, sink: TextIO, known: Sequence[str]) -> None:
    for raw in iter(source.readline, b""):
        sink.write(mask_text(raw.decode(errors="replace"), known))
        sink.flush()


def run_masked(command: Sequence[str], env: Mapping[str, str], values: Iterable[str]) -> int:
    known = masks(values)
    try:
        child = subprocess.Popen(
            list(command),
            env=dict(env),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError as error:
        raise KeyProblem(f"cannot run {command[0]}: {error.strerror}") from None
    assert child.stdout is not None and child.stderr is not None
    pumps = [
        threading.Thread(target=_pump, args=(child.stdout, sys.stdout, known)),
        threading.Thread(target=_pump, args=(child.stderr, sys.stderr, known)),
    ]
    for pump in pumps:
        pump.start()
    code = child.wait()
    for pump in pumps:
        pump.join()
    return code if code >= 0 else 128 - code


def unread(headers: Message) -> int | None:
    declared = headers.get("Content-Length")
    if declared is None:
        return None if headers.get("Transfer-Encoding") else 0
    declared = declared.strip()
    return int(declared) if declared.isascii() and declared.isdigit() else None


def drain(connection: socket.socket, stream: io.BufferedIOBase, length: int | None) -> None:
    budget = DRAIN_BYTES if length is None else min(length, DRAIN_BYTES)
    deadline = time.monotonic() + DRAIN_SECONDS
    try:
        connection.shutdown(socket.SHUT_WR)
        while budget > 0:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            connection.settimeout(remaining)
            chunk = stream.read1(min(budget, DRAIN_CHUNK))
            if not chunk:
                return
            budget -= len(chunk)
    except OSError:
        return


class PageServer(http.server.HTTPServer):
    allow_reuse_address = False

    def server_bind(self) -> None:
        exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
        if exclusive is not None:
            self.socket.setsockopt(socket.SOL_SOCKET, exclusive, 1)
        super().server_bind()

    def handle_error(self, request: Any, client_address: Any) -> None:
        return


PAGE = """<!doctype html>
<meta charset="utf-8">
<title>Paste {key}</title>
<form method="post" autocomplete="off">
<p>Paste the value of <b>{key}</b>.</p>
<p>{hint}</p>
<input type="password" name="value" autocomplete="off" autofocus required>
<button>Store</button>
</form>
"""
POLICY = "default-src 'none'; form-action 'self'; frame-ancestors 'none'"
DONE = '<!doctype html><meta charset="utf-8"><title>Stored</title><p>Received. Close this tab.</p>'


def ask(
    key: str,
    hint: str,
    announce: Callable[[str], None],
    timeout: float | None = None,
) -> str:
    limit = ASK_SECONDS if timeout is None else timeout
    token = randomness.token_urlsafe(16)
    route = f"/{token}"
    received: list[str] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        timeout = REQUEST_SECONDS

        def log_message(self, format: str, *args: Any) -> None:
            return

        def reply(self, status: int, body: str) -> None:
            data = body.encode()
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", POLICY)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(data)

        def refuse(self, status: int, body: str) -> None:
            pending = unread(self.headers)
            self.reply(status, body)
            drain(self.connection, self.rfile, pending)

        def mine(self) -> bool:
            host = self.headers.get("Host", "") == f"127.0.0.1:{server.server_port}"
            return hmac.compare_digest(self.path.encode(), route.encode()) and host

        def do_GET(self) -> None:
            if not self.mine():
                self.refuse(404, "not found")
                return
            self.reply(200, PAGE.format(key=html.escape(key), hint=html.escape(hint)))

        def do_POST(self) -> None:
            if not self.mine():
                self.refuse(404, "not found")
                return
            declared = self.headers.get("Content-Length")
            if declared is None:
                self.refuse(411, "length required")
                return
            if not (declared.isascii() and declared.isdigit()):
                self.refuse(400, "bad length")
                return
            length = int(declared)
            if length > MAX_BODY:
                self.refuse(413, "too large")
                return
            form = urllib.parse.parse_qs(self.rfile.read(length).decode(errors="replace"))
            value = (form.get("value") or [""])[0].strip()
            if not value:
                self.reply(400, "empty")
                return
            received.append(value)
            self.reply(200, DONE)

    server = PageServer(("127.0.0.1", 0), Handler)
    deadline = time.monotonic() + limit
    try:
        announce(f"http://127.0.0.1:{server.server_port}{route}")
        while not received:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise KeyProblem(f"no value arrived within {int(limit)} seconds")
            server.timeout = remaining
            server.handle_request()
    finally:
        server.server_close()
    return received[0]


def child_env(environ: Mapping[str, str], drop: Iterable[str]) -> dict[str, str]:
    skip = set(drop)
    return {key: value for key, value in environ.items() if key not in skip}
