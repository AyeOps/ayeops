from __future__ import annotations

import io
import json
import os
import socket
import sys
import threading
import time
from collections.abc import Callable, Mapping
from email.message import Message
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

NAME = "ayeops-catalog"
VERSION = "1"
PREFIX = "ayeops_mcp_catalog_"
VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
MAX_BODY = 65536
DRAIN_BYTES = 1048576
DRAIN_SECONDS = 2.0
DRAIN_CHUNK = 65536
MAX_TEXT = 200
DESCRIPTION_CHARS = 200
LIST_LIMIT = 200
MAX_LIMIT = 1000
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
CRITICALITIES = ["critical", "high", "normal", "low"]
STATUS_KINDS = ["public", "internal", "none"]
READ_ONLY = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": False,
}
INSTRUCTIONS = (
    "The service catalog of this AyeOps fleet and the runbooks of its alerts, read-only and"
    " built when the stack was rendered. Start with list_services, read one service with"
    " get_service, its alerts with list_alerts and what to do about an alert with get_runbook."
)


class ToolError(Exception):
    pass


def string_arg(description: str) -> dict[str, Any]:
    return {"type": "string", "minLength": 1, "maxLength": MAX_TEXT, "description": description}


SERVICE_ARGS = {
    "service": string_arg("the service name, or host/name when the name runs on several hosts"),
    "host": string_arg("the host, when the name runs on several hosts"),
}

TOOLS: dict[str, dict[str, Any]] = {
    "list_hosts": {
        "description": "Hosts of the fleet with their group, zone, architecture, provider,"
        " the modules they run and how many services the catalog lists on each.",
        "properties": {},
        "required": [],
    },
    "list_services": {
        "description": "Services of the catalog, one row each: id (host/name), host, group,"
        " source (manifest or the module that renders it), state, criticality, status mark"
        " (public, internal or none) and a short description. Filters narrow the list.",
        "properties": {
            "host": string_arg("only the services of this host"),
            "group": string_arg("only the services of hosts in this group"),
            "criticality": {"type": "string", "enum": CRITICALITIES},
            "status": {"type": "string", "enum": STATUS_KINDS},
            "limit": {"type": "integer", "minimum": 1, "maximum": MAX_LIMIT},
        },
        "required": [],
    },
    "get_service": {
        "description": "One service's catalog entry: description, host, group, criticality,"
        " url, dependencies, clients, metrics, alert names, log source, status mark and cloud"
        " dependencies.",
        "properties": SERVICE_ARGS,
        "required": ["service"],
    },
    "list_alerts": {
        "description": "The alerts of one service: title, severity, for, summary, the PromQL"
        " expression or baseline metric, the module or service that owns it and its runbook.",
        "properties": SERVICE_ARGS,
        "required": ["service"],
    },
    "get_runbook": {
        "description": "The runbook of an alert by the alert's name (a runbook name works"
        " too): its url and its Markdown text when the render found the page.",
        "properties": {"alert": string_arg("the alert name, for example target-down")},
        "required": ["alert"],
    },
}


def tool_list() -> list[dict[str, Any]]:
    return [
        {
            "name": name,
            "description": spec["description"],
            "inputSchema": {
                "type": "object",
                "properties": spec["properties"],
                "required": spec["required"],
                "additionalProperties": False,
            },
            "annotations": READ_ONLY,
        }
        for name, spec in TOOLS.items()
    ]


def check_args(name: str, arguments: Any) -> dict[str, Any]:
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        raise ToolError("arguments must be an object")
    spec = TOOLS[name]
    unknown = sorted(set(arguments) - set(spec["properties"]))
    if unknown:
        raise ToolError(f"unknown argument {unknown[0]}")
    for key in spec["required"]:
        if key not in arguments:
            raise ToolError(f"{key} is required")
    for key, value in arguments.items():
        rule = spec["properties"][key]
        if rule["type"] == "integer":
            if isinstance(value, bool) or not isinstance(value, int):
                raise ToolError(f"{key} must be an integer")
            if not rule["minimum"] <= value <= rule["maximum"]:
                raise ToolError(f"{key} must be from {rule['minimum']} to {rule['maximum']}")
            continue
        if not isinstance(value, str) or not value.strip():
            raise ToolError(f"{key} must be a non-empty string")
        if len(value) > MAX_TEXT:
            raise ToolError(f"{key} is longer than {MAX_TEXT} characters")
        if "enum" in rule and value not in rule["enum"]:
            raise ToolError(f"{key} must be one of {', '.join(rule['enum'])}")
    return arguments


def status_kind(service: Mapping[str, Any]) -> str:
    mark = service.get("status") or {}
    for kind in ("public", "internal"):
        if kind in mark:
            return kind
    return "none"


def list_hosts(catalog: Mapping[str, Any], arguments: Mapping[str, Any]) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for service in catalog["services"].values():
        counts[service["host"]] = counts.get(service["host"], 0) + 1
    hosts = [{**entry, "services": counts.get(name, 0)} for name, entry in catalog["hosts"].items()]
    return {"project": catalog["project"], "hosts": hosts}


def short(text: str) -> str:
    if len(text) <= DESCRIPTION_CHARS:
        return text
    return text[: DESCRIPTION_CHARS - 3].rstrip() + "..."


def list_services(catalog: Mapping[str, Any], arguments: Mapping[str, Any]) -> dict[str, Any]:
    host = arguments.get("host")
    if host is not None and host not in catalog["hosts"]:
        raise ToolError(f"no host {host}; list_hosts names them")
    rows = []
    for service in catalog["services"].values():
        if host is not None and service["host"] != host:
            continue
        if "group" in arguments and service.get("group") != arguments["group"]:
            continue
        if "criticality" in arguments and service.get("criticality") != arguments["criticality"]:
            continue
        if "status" in arguments and status_kind(service) != arguments["status"]:
            continue
        mark = service.get("status") or {}
        rows.append(
            {
                "id": service["id"],
                "name": service["name"],
                "host": service["host"],
                "group": service.get("group"),
                "source": service["source"],
                "state": service["state"],
                "criticality": service.get("criticality"),
                "status": status_kind(service),
                "status_label": mark.get("public") or mark.get("internal"),
                "description": short(service.get("description") or ""),
            }
        )
    limit = int(arguments.get("limit", LIST_LIMIT))
    return {"count": len(rows), "truncated": len(rows) > limit, "services": rows[:limit]}


def find_service(catalog: Mapping[str, Any], arguments: Mapping[str, Any]) -> dict[str, Any]:
    services = catalog["services"]
    wanted = str(arguments["service"])
    host = arguments.get("host")
    if "/" in wanted:
        host, wanted = wanted.split("/", 1)
    if host is not None:
        found = services.get(f"{host}/{wanted}")
        if found is None:
            raise ToolError(f"no service {wanted} on {host}; list_services names them")
        return dict(found)
    matches = [service for service in services.values() if service["name"] == wanted]
    if not matches:
        raise ToolError(f"no service {wanted}; list_services names them")
    if len(matches) > 1:
        hosts = ", ".join(sorted(service["host"] for service in matches))
        raise ToolError(f"{wanted} runs on several hosts ({hosts}); pass host")
    return dict(matches[0])


def get_service(catalog: Mapping[str, Any], arguments: Mapping[str, Any]) -> dict[str, Any]:
    return find_service(catalog, arguments)


def list_alerts(catalog: Mapping[str, Any], arguments: Mapping[str, Any]) -> dict[str, Any]:
    service = find_service(catalog, arguments)
    alerts = catalog["alerts"]
    found = [
        dict(alerts[name]) if name in alerts else {"name": name, "known": False}
        for name in service.get("alerts") or []
    ]
    return {"service": service["id"], "alerts": found}


def get_runbook(catalog: Mapping[str, Any], arguments: Mapping[str, Any]) -> dict[str, Any]:
    wanted = str(arguments["alert"])
    alert = catalog["alerts"].get(wanted)
    runbook = alert.get("runbook") if alert else wanted
    page = catalog["runbooks"].get(runbook) if runbook else None
    if page is None and alert is not None and alert.get("runbook_url"):
        page = {"name": runbook, "url": alert["runbook_url"], "text": None}
    if page is None:
        if alert is not None:
            raise ToolError(f"alert {wanted} has no runbook in the catalog")
        raise ToolError(f"no alert or runbook {wanted}; list_alerts names the alerts")
    result = {"alert": wanted if alert else None, **page}
    if page.get("text") is None:
        result["note"] = "the render did not find the page; open the url"
    return result


HANDLERS: dict[str, Callable[[Mapping[str, Any], Mapping[str, Any]], dict[str, Any]]] = {
    "list_hosts": list_hosts,
    "list_services": list_services,
    "get_service": get_service,
    "list_alerts": list_alerts,
    "get_runbook": get_runbook,
}


class Metrics:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.requests: dict[str, int] = {}
        self.calls: dict[tuple[str, str], int] = {}

    def request(self, method: str) -> None:
        with self.lock:
            self.requests[method] = self.requests.get(method, 0) + 1

    def call(self, tool: str, outcome: str) -> None:
        with self.lock:
            key = (tool, outcome)
            self.calls[key] = self.calls.get(key, 0) + 1

    def text(self, catalog: Mapping[str, Any]) -> str:
        lines = [
            f"# HELP {PREFIX}requests_total MCP requests by JSON-RPC method.",
            f"# TYPE {PREFIX}requests_total counter",
        ]
        with self.lock:
            requests = sorted(self.requests.items())
            calls = sorted(self.calls.items())
        lines += [f'{PREFIX}requests_total{{method="{label(m)}"}} {n}' for m, n in requests]
        lines += [
            f"# HELP {PREFIX}tool_calls_total Tool calls by tool and outcome.",
            f"# TYPE {PREFIX}tool_calls_total counter",
        ]
        lines += [
            f'{PREFIX}tool_calls_total{{tool="{label(t)}",outcome="{o}"}} {n}'
            for (t, o), n in calls
        ]
        lines += [
            f"# HELP {PREFIX}entries Entries of the served catalog by kind.",
            f"# TYPE {PREFIX}entries gauge",
        ]
        lines += [
            f'{PREFIX}entries{{kind="{kind}"}} {len(catalog[kind])}'
            for kind in ("hosts", "services", "alerts", "runbooks")
        ]
        return "\n".join(lines) + "\n"


def label(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def tool_result(payload: dict[str, Any], error: bool = False) -> dict[str, Any]:
    text = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False)
    return {"content": [{"type": "text", "text": text}], "isError": error}


def call_tool(catalog: Mapping[str, Any], params: Mapping[str, Any], metrics: Metrics) -> Any:
    name = params.get("name")
    if not isinstance(name, str) or name not in TOOLS:
        raise RpcError(INVALID_PARAMS, f"unknown tool {name}")
    try:
        arguments = check_args(name, params.get("arguments"))
        payload = HANDLERS[name](catalog, arguments)
    except ToolError as error:
        metrics.call(name, "error")
        return {"content": [{"type": "text", "text": str(error)}], "isError": True}
    metrics.call(name, "ok")
    return tool_result(payload)


class RpcError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def negotiate(params: Mapping[str, Any]) -> str:
    wanted = params.get("protocolVersion")
    return wanted if wanted in VERSIONS else VERSIONS[0]


def dispatch(
    catalog: Mapping[str, Any], method: str, params: Mapping[str, Any], metrics: Metrics
) -> Any:
    if method == "initialize":
        return {
            "protocolVersion": negotiate(params),
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": NAME, "version": VERSION},
            "instructions": INSTRUCTIONS,
        }
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": tool_list()}
    if method == "tools/call":
        return call_tool(catalog, params, metrics)
    raise RpcError(METHOD_NOT_FOUND, f"method {method} is not served")


def rpc_ok(ident: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": ident, "result": result}


def rpc_error(ident: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": ident, "error": {"code": code, "message": message}}


def handle(catalog: Mapping[str, Any], message: Any, metrics: Metrics) -> dict[str, Any] | None:
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
        return rpc_error(None, INVALID_REQUEST, "not a JSON-RPC 2.0 message")
    method = message.get("method")
    ident = message.get("id")
    if method is None:
        if "result" in message or "error" in message:
            return None
        return rpc_error(ident, INVALID_REQUEST, "a request needs a method")
    if not isinstance(method, str):
        return rpc_error(ident, INVALID_REQUEST, "method must be a string")
    metrics.request(method)
    if "id" not in message:
        return None
    if isinstance(ident, bool) or not isinstance(ident, str | int):
        return rpc_error(None, INVALID_REQUEST, "id must be a string or an integer")
    params = message.get("params")
    if params is None:
        params = {}
    if not isinstance(params, dict):
        return rpc_error(ident, INVALID_PARAMS, "params must be an object")
    try:
        result = dispatch(catalog, method, params, metrics)
    except RpcError as error:
        return rpc_error(ident, error.code, error.message)
    return rpc_ok(ident, result)


def answer(catalog: Mapping[str, Any], body: bytes, metrics: Metrics) -> tuple[int, Any]:
    try:
        message = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return 400, rpc_error(None, PARSE_ERROR, "the body is not JSON")
    if isinstance(message, list):
        if not message:
            return 400, rpc_error(None, INVALID_REQUEST, "an empty batch")
        replies = [reply for item in message if (reply := handle(catalog, item, metrics))]
        return (200, replies) if replies else (202, None)
    reply = handle(catalog, message, metrics)
    return (200, reply) if reply is not None else (202, None)


def load_catalog(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} is not a JSON object")
    for key in ("hosts", "services", "alerts", "runbooks"):
        if not isinstance(data.get(key), dict):
            raise ValueError(f"{path} has no {key} object")
    data.setdefault("project", "")
    return data


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


def make_server(
    catalog: Mapping[str, Any], host: str, port: int, metrics: Metrics | None = None
) -> ThreadingHTTPServer:
    counters = metrics or Metrics()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def reply(self, status: int, body: bytes, kind: str, extra: Mapping[str, str]) -> None:
            self.send_response(status)
            for key, value in extra.items():
                self.send_header(key, value)
            if body:
                self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def json_reply(self, status: int, payload: Any) -> None:
            body = b"" if payload is None else json.dumps(payload).encode("utf-8")
            self.reply(status, body, "application/json", {})

        def refuse(
            self,
            status: int,
            body: bytes = b"",
            kind: str = "",
            extra: Mapping[str, str] | None = None,
        ) -> None:
            pending = unread(self.headers)
            headers = dict(extra or {})
            if pending != 0:
                headers["Connection"] = "close"
            self.reply(status, body, kind, headers)
            if pending != 0:
                drain(self.connection, self.rfile, pending)

        def do_GET(self) -> None:
            path = self.path.split("?", 1)[0]
            if path == "/health":
                self.reply(200, b"ok\n", "text/plain; charset=utf-8", {})
            elif path == "/metrics":
                body = counters.text(catalog).encode("utf-8")
                self.reply(200, body, "text/plain; version=0.0.4; charset=utf-8", {})
            elif path == "/mcp":
                self.refuse(405, extra={"Allow": "POST"})
            else:
                self.refuse(404)

        def do_DELETE(self) -> None:
            self.refuse(405, extra={"Allow": "POST"})

        def do_POST(self) -> None:
            if self.path.split("?", 1)[0] != "/mcp":
                self.refuse(404)
                return
            version = self.headers.get("MCP-Protocol-Version")
            if version is not None and version not in VERSIONS:
                error = rpc_error(None, INVALID_REQUEST, "unsupported version")
                self.refuse(400, json.dumps(error).encode("utf-8"), "application/json")
                return
            kind = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
            if kind != "application/json":
                self.refuse(415)
                return
            try:
                size = int(self.headers.get("Content-Length") or "")
            except ValueError:
                self.refuse(411)
                return
            if size < 0 or size > MAX_BODY:
                self.refuse(413)
                return
            status, payload = answer(catalog, self.rfile.read(size), counters)
            self.json_reply(status, payload)

        def log_message(self, format: str, *args: Any) -> None:
            return None

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    return server


def main(environ: Mapping[str, str] = os.environ) -> int:
    path = Path(environ.get("AYE_MCP_CATALOG_FILE", "/data/catalog.json"))
    try:
        catalog = load_catalog(path)
    except (OSError, ValueError) as error:
        print(f"cannot read the catalog: {error}", file=sys.stderr)
        return 1
    port = int(environ.get("AYE_MCP_CATALOG_PORT", "8000"))
    server = make_server(catalog, "0.0.0.0", port)
    counts = {kind: len(catalog[kind]) for kind in ("hosts", "services", "alerts", "runbooks")}
    print(json.dumps({"event": "serving", "port": port, **counts}), file=sys.stderr, flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
