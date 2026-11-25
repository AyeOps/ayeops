from __future__ import annotations

import email.message
import http.client
import importlib.util
import json
import socket
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from ayeops import secretkeys
from ayeops.mcp_catalog import catalog_json
from ayeops.registry import load_modules
from tests.conftest import ROOT, WAIT, example_data, manifest_from

SCRIPT = ROOT / "src" / "ayeops" / "modules" / "mcp" / "files" / "catalog_server.py"


def load_server() -> ModuleType:
    spec = importlib.util.spec_from_file_location("ayeops.modules.mcp.files.catalog_server", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


server = load_server()

CATALOG: dict[str, Any] = {
    "project": "acme",
    "hosts": {
        "central": {"name": "central", "zone": "dc", "modules": ["mcp", "node"]},
        "edge": {"name": "edge", "group": "office", "modules": ["node"]},
    },
    "services": {
        "central/node-exporter": {
            "id": "central/node-exporter",
            "name": "node-exporter",
            "host": "central",
            "source": "module node",
            "state": "live",
            "description": "Host metrics.",
            "metrics": ["node_load1"],
            "alerts": ["host-down", "gone"],
            "status": {"internal": "host metrics"},
        },
        "edge/node-exporter": {
            "id": "edge/node-exporter",
            "name": "node-exporter",
            "host": "edge",
            "group": "office",
            "source": "module node",
            "state": "live",
            "description": "Host metrics.",
            "metrics": ["node_load1"],
            "alerts": ["host-down"],
            "status": {"internal": "host metrics"},
        },
        "central/web": {
            "id": "central/web",
            "name": "web",
            "host": "central",
            "source": "manifest",
            "state": "live",
            "criticality": "critical",
            "description": "W" * 300,
            "metrics": ["probe_success"],
            "alerts": ["web-down"],
            "status": {"public": "Website"},
        },
    },
    "alerts": {
        "host-down": {
            "name": "host-down",
            "module": "node",
            "severity": "critical",
            "runbook": "host-down",
            "runbook_url": "https://example/host-down.md",
        },
        "web-down": {
            "name": "web-down",
            "service": "web",
            "severity": "critical",
            "runbook": "web-down",
            "runbook_url": "https://runbooks.example/web-down",
        },
        "silent": {"name": "silent", "module": "node", "severity": "info"},
    },
    "runbooks": {
        "host-down": {"name": "host-down", "url": "https://example/host-down.md", "text": "# Host"},
        "web-down": {"name": "web-down", "url": "https://runbooks.example/web-down", "text": None},
    },
}


def rpc(method: str, params: Any = None, ident: Any = 1) -> dict[str, Any]:
    message: dict[str, Any] = {"jsonrpc": "2.0", "id": ident, "method": method}
    if params is not None:
        message["params"] = params
    return message


def call(name: str, arguments: Any = None, catalog: Any = None) -> dict[str, Any]:
    params: dict[str, Any] = {"name": name}
    if arguments is not None:
        params["arguments"] = arguments
    reply = server.handle(catalog or CATALOG, rpc("tools/call", params), server.Metrics())
    result: dict[str, Any] = reply["result"]
    return result


def payload(result: dict[str, Any]) -> Any:
    assert result["isError"] is False
    return json.loads(result["content"][0]["text"])


def failure(result: dict[str, Any]) -> str:
    assert result["isError"] is True
    text: str = result["content"][0]["text"]
    return text


@pytest.mark.parametrize(
    ("asked", "answered"),
    [("2025-06-18", "2025-06-18"), ("2024-11-05", "2024-11-05"), ("1999-01-01", "2025-11-25")],
)
def test_initialize_answers_the_version_the_client_asks_when_it_knows_it(
    asked: str, answered: str
) -> None:
    reply = server.handle(CATALOG, rpc("initialize", {"protocolVersion": asked}), server.Metrics())
    assert reply["id"] == 1
    result = reply["result"]
    assert result["protocolVersion"] == answered
    assert result["capabilities"] == {"tools": {"listChanged": False}}
    assert result["serverInfo"]["name"] == "ayeops-catalog"
    assert "get_runbook" in result["instructions"]


def test_every_tool_is_read_only_with_a_closed_input_schema() -> None:
    reply = server.handle(CATALOG, rpc("tools/list"), server.Metrics())
    tools = reply["result"]["tools"]
    assert [tool["name"] for tool in tools] == [
        "list_hosts",
        "list_services",
        "get_service",
        "list_alerts",
        "get_runbook",
    ]
    for tool in tools:
        assert tool["annotations"]["readOnlyHint"] is True
        assert tool["annotations"]["destructiveHint"] is False
        assert tool["inputSchema"]["type"] == "object"
        assert tool["inputSchema"]["additionalProperties"] is False
        assert tool["description"]


def test_ping_answers_an_empty_result() -> None:
    assert server.handle(CATALOG, rpc("ping", ident="a"), server.Metrics()) == {
        "jsonrpc": "2.0",
        "id": "a",
        "result": {},
    }


def test_list_hosts_counts_the_services_of_each_host() -> None:
    found = payload(call("list_hosts"))
    assert found["project"] == "acme"
    assert {host["name"]: host["services"] for host in found["hosts"]} == {"central": 2, "edge": 1}
    assert found["hosts"][1]["group"] == "office"


def test_list_services_shows_status_marks_and_short_descriptions() -> None:
    found = payload(call("list_services", {}))
    assert found["count"] == 3
    assert found["truncated"] is False
    rows = {row["id"]: row for row in found["services"]}
    assert rows["central/web"]["status"] == "public"
    assert rows["central/web"]["status_label"] == "Website"
    assert rows["central/web"]["criticality"] == "critical"
    assert rows["central/web"]["description"] == "W" * 197 + "..."
    assert rows["edge/node-exporter"]["group"] == "office"
    assert rows["central/node-exporter"]["status"] == "internal"
    assert rows["central/node-exporter"]["description"] == "Host metrics."


@pytest.mark.parametrize(
    ("arguments", "ids"),
    [
        ({"host": "edge"}, ["edge/node-exporter"]),
        ({"group": "office"}, ["edge/node-exporter"]),
        ({"criticality": "critical"}, ["central/web"]),
        ({"status": "public"}, ["central/web"]),
        ({"status": "internal", "host": "central"}, ["central/node-exporter"]),
        ({"status": "none"}, []),
    ],
)
def test_list_services_filters(arguments: dict[str, Any], ids: list[str]) -> None:
    assert [row["id"] for row in payload(call("list_services", arguments))["services"]] == ids


def test_list_services_says_when_it_cut_the_list() -> None:
    found = payload(call("list_services", {"limit": 2}))
    assert found["count"] == 3
    assert found["truncated"] is True
    assert len(found["services"]) == 2
    assert payload(call("list_services", {"limit": 3}))["truncated"] is False


def test_list_services_refuses_an_unknown_host() -> None:
    assert "list_hosts" in failure(call("list_services", {"host": "nowhere"}))


@pytest.mark.parametrize(
    "arguments",
    [{"service": "web"}, {"service": "central/web"}, {"service": "web", "host": "central"}],
)
def test_get_service_finds_a_service_by_name_or_by_host(arguments: dict[str, Any]) -> None:
    assert payload(call("get_service", arguments)) == CATALOG["services"]["central/web"]


def test_get_service_asks_for_the_host_when_a_name_runs_on_several() -> None:
    message = failure(call("get_service", {"service": "node-exporter"}))
    assert "central, edge" in message
    found = payload(call("get_service", {"service": "node-exporter", "host": "edge"}))
    assert found["id"] == "edge/node-exporter"


@pytest.mark.parametrize(
    "arguments",
    [{"service": "nope"}, {"service": "web", "host": "edge"}, {"service": "edge/web"}],
)
def test_get_service_refuses_an_unknown_service(arguments: dict[str, Any]) -> None:
    assert "list_services" in failure(call("get_service", arguments))


def test_list_alerts_resolves_the_alerts_of_a_service() -> None:
    found = payload(call("list_alerts", {"service": "central/node-exporter"}))
    assert found["service"] == "central/node-exporter"
    assert found["alerts"] == [CATALOG["alerts"]["host-down"], {"name": "gone", "known": False}]


def test_get_runbook_by_alert_name() -> None:
    assert payload(call("get_runbook", {"alert": "host-down"})) == {
        "alert": "host-down",
        "name": "host-down",
        "url": "https://example/host-down.md",
        "text": "# Host",
    }


def test_get_runbook_by_runbook_name() -> None:
    catalog = {**CATALOG, "alerts": {}}
    found = payload(call("get_runbook", {"alert": "host-down"}, catalog))
    assert found["alert"] is None
    assert found["text"] == "# Host"


def test_get_runbook_without_text_points_at_the_url() -> None:
    found = payload(call("get_runbook", {"alert": "web-down"}))
    assert found["text"] is None
    assert found["url"] == "https://runbooks.example/web-down"
    assert "url" in found["note"]
    assert "note" not in payload(call("get_runbook", {"alert": "host-down"}))


def test_get_runbook_of_a_runbook_outside_the_catalog_gives_its_url() -> None:
    alert = {
        "name": "x",
        "runbook": "https://wiki.example/x",
        "runbook_url": "https://wiki.example/x",
    }
    catalog = {**CATALOG, "alerts": {"x": alert}}
    found = payload(call("get_runbook", {"alert": "x"}, catalog))
    assert found == {
        "alert": "x",
        "name": "https://wiki.example/x",
        "url": "https://wiki.example/x",
        "text": None,
        "note": "the render did not find the page; open the url",
    }


def test_get_runbook_refuses_what_it_does_not_know() -> None:
    assert "has no runbook" in failure(call("get_runbook", {"alert": "silent"}))
    assert "list_alerts" in failure(call("get_runbook", {"alert": "nothing"}))


@pytest.mark.parametrize(
    ("name", "arguments", "message"),
    [
        ("list_hosts", [], "arguments must be an object"),
        ("list_hosts", {"host": "x"}, "unknown argument host"),
        ("get_service", {}, "service is required"),
        ("get_service", {"service": 3}, "service must be a non-empty string"),
        ("get_service", {"service": "  "}, "service must be a non-empty string"),
        ("get_service", {"service": "x" * 201}, "longer than 200"),
        ("list_services", {"criticality": "huge"}, "criticality must be one of"),
        ("list_services", {"limit": "5"}, "limit must be an integer"),
        ("list_services", {"limit": True}, "limit must be an integer"),
        ("list_services", {"limit": 0}, "limit must be from 1 to 1000"),
        ("list_services", {"limit": 1001}, "limit must be from 1 to 1000"),
    ],
)
def test_bad_arguments_are_tool_errors(name: str, arguments: Any, message: str) -> None:
    assert message in failure(call(name, arguments))


def test_limits_at_the_edges_are_accepted() -> None:
    assert payload(call("list_services", {"limit": 1}))["truncated"] is True
    assert payload(call("list_services", {"limit": 1000}))["truncated"] is False
    assert "no service web" in failure(call("get_service", {"service": "web", "host": "c" * 200}))


def test_an_unknown_tool_is_an_invalid_params_error() -> None:
    reply = server.handle(CATALOG, rpc("tools/call", {"name": "drop_all"}), server.Metrics())
    assert reply["error"]["code"] == -32602
    assert "drop_all" in reply["error"]["message"]


@pytest.mark.parametrize(
    ("message", "code"),
    [
        ({"id": 1, "method": "ping"}, -32600),
        ({"jsonrpc": "1.0", "id": 1, "method": "ping"}, -32600),
        ({"jsonrpc": "2.0", "id": 1}, -32600),
        ({"jsonrpc": "2.0", "id": 1, "method": 5}, -32600),
        ({"jsonrpc": "2.0", "id": True, "method": "ping"}, -32600),
        ({"jsonrpc": "2.0", "id": [1], "method": "ping"}, -32600),
        ({"jsonrpc": "2.0", "id": 1, "method": "ping", "params": []}, -32602),
        ({"jsonrpc": "2.0", "id": 1, "method": "resources/list"}, -32601),
        ([1], -32600),
    ],
)
def test_malformed_messages_get_json_rpc_errors(message: Any, code: int) -> None:
    item = message[0] if isinstance(message, list) else message
    reply = server.handle(CATALOG, item, server.Metrics())
    assert reply["jsonrpc"] == "2.0"
    assert reply["error"]["code"] == code


def test_notifications_and_client_responses_get_no_reply() -> None:
    metrics = server.Metrics()
    note = {"jsonrpc": "2.0", "method": "notifications/initialized"}
    assert server.handle(CATALOG, note, metrics) is None
    assert server.handle(CATALOG, {"jsonrpc": "2.0", "id": 4, "result": {}}, metrics) is None
    assert server.handle(CATALOG, {"jsonrpc": "2.0", "id": 4, "error": {}}, metrics) is None
    assert metrics.requests == {"notifications/initialized": 1}


def test_answer_handles_bodies_batches_and_notifications() -> None:
    metrics = server.Metrics()
    assert server.answer(CATALOG, b"{nope", metrics)[0] == 400
    assert server.answer(CATALOG, b"\xff", metrics)[1]["error"]["code"] == -32700
    assert server.answer(CATALOG, b"[]", metrics)[0] == 400
    note = json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}).encode()
    assert server.answer(CATALOG, note, metrics) == (202, None)
    assert server.answer(CATALOG, b"[" + note + b"]", metrics) == (202, None)
    batch = json.dumps([rpc("ping", ident=1), json.loads(note), rpc("ping", ident=2)]).encode()
    status, replies = server.answer(CATALOG, batch, metrics)
    assert status == 200
    assert [reply["id"] for reply in replies] == [1, 2]
    assert server.answer(CATALOG, json.dumps(rpc("ping")).encode(), metrics)[0] == 200


def test_metrics_count_requests_and_tool_outcomes() -> None:
    metrics = server.Metrics()
    server.handle(CATALOG, rpc("tools/call", {"name": "list_hosts"}), metrics)
    server.handle(CATALOG, rpc("tools/call", {"name": "get_service", "arguments": {}}), metrics)
    server.handle(CATALOG, rpc("tools/list"), metrics)
    server.handle(CATALOG, rpc("tools/list"), metrics)
    text = metrics.text(CATALOG)
    assert 'ayeops_mcp_catalog_requests_total{method="tools/call"} 2' in text
    assert 'ayeops_mcp_catalog_requests_total{method="tools/list"} 2' in text
    assert 'ayeops_mcp_catalog_tool_calls_total{tool="list_hosts",outcome="ok"} 1' in text
    assert 'ayeops_mcp_catalog_tool_calls_total{tool="get_service",outcome="error"} 1' in text
    assert 'ayeops_mcp_catalog_entries{kind="services"} 3' in text
    assert 'ayeops_mcp_catalog_entries{kind="runbooks"} 2' in text
    assert "# TYPE ayeops_mcp_catalog_requests_total counter" in text
    assert "# TYPE ayeops_mcp_catalog_entries gauge" in text
    assert text.endswith("\n")


def test_metric_labels_are_escaped() -> None:
    assert server.label('a"b\\c\nd') == 'a\\"b\\\\c\\nd'


def test_load_catalog_refuses_a_file_without_its_sections(tmp_path: Path) -> None:
    path = tmp_path / "catalog.json"
    path.write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError, match="not a JSON object"):
        server.load_catalog(path)
    path.write_text(json.dumps({**CATALOG, "runbooks": []}), encoding="utf-8")
    with pytest.raises(ValueError, match="no runbooks"):
        server.load_catalog(path)
    path.write_text(json.dumps({k: v for k, v in CATALOG.items() if k != "project"}))
    assert server.load_catalog(path)["project"] == ""


def test_main_fails_without_a_catalog(tmp_path: Path, capsys: Any) -> None:
    assert server.main({"AYE_MCP_CATALOG_FILE": str(tmp_path / "missing.json")}) == 1
    assert "cannot read the catalog" in capsys.readouterr().err


@pytest.fixture
def running() -> Iterator[tuple[int, Any]]:
    metrics = server.Metrics()
    httpd = server.make_server(CATALOG, "127.0.0.1", 0, metrics)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd.server_address[1], metrics
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(WAIT)


def request(
    port: int, method: str, path: str, body: bytes | None = None, **headers: str
) -> tuple[int, dict[str, str], bytes]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=WAIT)
    try:
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


def post(port: int, message: Any, **headers: str) -> tuple[int, dict[str, str], bytes]:
    fields = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    return request(port, "POST", "/mcp", json.dumps(message).encode(), **{**fields, **headers})


def test_a_client_session_round_trips_over_streamable_http(running: tuple[int, Any]) -> None:
    port, metrics = running
    status, headers, body = post(
        port, rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}})
    )
    assert status == 200
    assert headers["Content-Type"] == "application/json"
    assert json.loads(body)["result"]["protocolVersion"] == "2025-06-18"
    version = {"MCP-Protocol-Version": "2025-06-18"}
    note = {"jsonrpc": "2.0", "method": "notifications/initialized"}
    status, _, body = post(port, note, **version)
    assert (status, body) == (202, b"")
    status, _, body = post(port, rpc("tools/list", ident=2), **version)
    assert len(json.loads(body)["result"]["tools"]) == 5
    status, _, body = post(
        port,
        rpc("tools/call", {"name": "get_runbook", "arguments": {"alert": "host-down"}}, 3),
        **version,
    )
    reply = json.loads(body)
    assert reply["id"] == 3
    assert json.loads(reply["result"]["content"][0]["text"])["text"] == "# Host"
    assert metrics.calls == {("get_runbook", "ok"): 1}


def test_the_server_answers_health_and_metrics(running: tuple[int, Any]) -> None:
    port, _ = running
    assert request(port, "GET", "/health")[::2] == (200, b"ok\n")
    status, headers, body = request(port, "GET", "/metrics?x=1")
    assert status == 200
    assert headers["Content-Type"].startswith("text/plain; version=0.0.4")
    assert b'ayeops_mcp_catalog_entries{kind="hosts"} 2' in body


@pytest.mark.parametrize(
    ("method", "path", "status"),
    [("GET", "/mcp", 405), ("DELETE", "/mcp", 405), ("GET", "/", 404), ("POST", "/other", 404)],
)
def test_other_routes_are_refused(
    running: tuple[int, Any], method: str, path: str, status: int
) -> None:
    port, _ = running
    answer = request(port, method, path, b"{}" if method == "POST" else None)
    assert answer[0] == status
    assert ("Connection" in answer[1]) == (method == "POST")
    if status == 405:
        assert answer[1]["Allow"] == "POST"


def unsent(port: int, length: int) -> int:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=WAIT)
    try:
        connection.putrequest("POST", "/mcp")
        connection.putheader("Content-Type", "application/json")
        connection.putheader("Content-Length", str(length))
        connection.endheaders()
        return connection.getresponse().status
    finally:
        connection.close()


def test_a_bad_post_is_refused_before_it_is_read(running: tuple[int, Any]) -> None:
    port, _ = running
    body = json.dumps(rpc("ping")).encode()
    assert request(port, "POST", "/mcp", body, **{"Content-Type": "text/plain"})[0] == 415
    status, _, answer = post(port, rpc("ping"), **{"MCP-Protocol-Version": "1999-01-01"})
    assert status == 400
    assert json.loads(answer)["error"]["code"] == -32600
    assert unsent(port, server.MAX_BODY + 1) == 413
    good = request(
        port, "POST", "/mcp", body, **{"Content-Type": "application/json; charset=utf-8"}
    )
    assert good[0] == 200


def test_a_post_without_a_length_is_refused(running: tuple[int, Any]) -> None:
    port, _ = running
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=WAIT)
    try:
        connection.putrequest("POST", "/mcp")
        connection.putheader("Content-Type", "application/json")
        connection.endheaders()
        assert connection.getresponse().status == 411
    finally:
        connection.close()


def head(path: str, length: int, kind: str = "application/json") -> bytes:
    lines = [f"POST {path} HTTP/1.1", "Host: 127.0.0.1", f"Content-Type: {kind}"]
    return ("\r\n".join([*lines, f"Content-Length: {length}", "", ""])).encode()


def exchange(port: int, data: bytes, timeout: float = WAIT) -> bytes:
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as connection:
        connection.sendall(data)
        answer = b""
        while chunk := connection.recv(65536):
            answer += chunk
    return answer


@pytest.mark.parametrize(
    ("path", "kind", "status"),
    [
        ("/mcp", "application/json", 413),
        ("/other", "application/json", 404),
        ("/mcp", "text/plain", 415),
    ],
)
def test_an_early_answer_reaches_a_client_that_already_sent_its_body(
    running: tuple[int, Any], path: str, kind: str, status: int
) -> None:
    port, _ = running
    body = b"x" * (server.DRAIN_BYTES // 2)
    for _ in range(200):
        answer = exchange(port, head(path, len(body), kind) + body)
        assert answer.startswith(f"HTTP/1.1 {status} ".encode())
        assert b"\r\nConnection: close\r\n" in answer


def test_an_early_answer_arrives_while_the_body_is_still_on_its_way(
    running: tuple[int, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(server, "DRAIN_SECONDS", WAIT)
    port, _ = running
    answer = exchange(port, head("/mcp", server.MAX_BODY + 1) + b"x" * 100, timeout=30)
    assert answer.startswith(b"HTTP/1.1 413 ")
    assert request(port, "GET", "/health")[0] == 200


@pytest.fixture(params=["catalog", "secrets"])
def kit(request: pytest.FixtureRequest) -> ModuleType:
    return server if request.param == "catalog" else secretkeys


def headers(**fields: str) -> email.message.Message:
    message = email.message.Message()
    for key, value in fields.items():
        message[key.replace("_", "-")] = value
    return message


@pytest.mark.parametrize(
    ("fields", "length"),
    [
        ({}, 0),
        ({"Content_Length": "12"}, 12),
        ({"Content_Length": " 7 "}, 7),
        ({"Content_Length": "-1"}, None),
        ({"Content_Length": "abc"}, None),
        ({"Content_Length": "²"}, None),
        ({"Transfer_Encoding": "chunked"}, None),
        ({"Transfer_Encoding": ""}, 0),
    ],
)
def test_unread_is_the_declared_body_or_none_when_it_cannot_be_known(
    kit: ModuleType, fields: dict[str, str], length: int | None
) -> None:
    assert kit.unread(headers(**fields)) == length


@contextmanager
def drained(
    kit: ModuleType, length: int | None, sent: bytes, close: bool = False
) -> Iterator[tuple[socket.socket, Any]]:
    ours, theirs = socket.socketpair()
    stream = ours.makefile("rb")
    try:
        theirs.sendall(sent)
        if close:
            theirs.shutdown(socket.SHUT_WR)
        returned: list[Any] = []
        worker = threading.Thread(
            target=lambda: returned.append(kit.drain(ours, stream, length)), daemon=True
        )
        worker.start()
        worker.join(30)
        assert returned == [None]
        yield theirs, stream
    finally:
        theirs.close()
        stream.close()
        ours.close()


def test_drain_reads_the_declared_body_and_stops_without_waiting_for_the_end(
    kit: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(kit, "DRAIN_SECONDS", WAIT)
    with drained(kit, 5, b"12345678") as (theirs, stream):
        theirs.settimeout(30)
        assert theirs.recv(10) == b""
        theirs.shutdown(socket.SHUT_WR)
        assert stream.read() == b"678"


def test_drain_of_an_unknown_length_reads_to_the_end(
    kit: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(kit, "DRAIN_SECONDS", WAIT)
    with drained(kit, None, b"12345678", close=True) as (_, stream):
        assert stream.read() == b""


def test_drain_reads_no_more_than_its_budget(
    kit: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(kit, "DRAIN_SECONDS", WAIT)
    monkeypatch.setattr(kit, "DRAIN_BYTES", 1000)
    monkeypatch.setattr(kit, "DRAIN_CHUNK", 300)
    with drained(kit, 5000, b"x" * 1500) as (theirs, stream):
        theirs.shutdown(socket.SHUT_WR)
        assert len(stream.read()) == 500


def test_drain_gives_up_on_a_client_that_never_sends(
    kit: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(kit, "DRAIN_SECONDS", 0.2)
    with drained(kit, kit.MAX_BODY, b"x"):
        pass


def test_drain_stops_at_its_deadline_even_while_bytes_keep_coming(
    kit: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    ticks = iter([0.0, kit.DRAIN_SECONDS + 1])
    monkeypatch.setattr(kit, "time", SimpleNamespace(monotonic=lambda: next(ticks)))
    with drained(kit, 10, b"x" * 10) as (theirs, stream):
        theirs.shutdown(socket.SHUT_WR)
        assert stream.read() == b"x" * 10


def test_drain_of_a_closed_socket_returns_quietly(kit: ModuleType) -> None:
    ours, theirs = socket.socketpair()
    with ours.makefile("rb") as stream:
        ours.close()
        kit.drain(ours, stream, 10)
    theirs.close()


def test_the_rendered_example_catalog_is_served(tmp_path: Path) -> None:
    path = tmp_path / "catalog.json"
    path.write_text(catalog_json(manifest_from(example_data()), load_modules()), encoding="utf-8")
    catalog = server.load_catalog(path)
    hub = payload(call("get_service", {"service": "mcp-catalog"}, catalog))
    assert hub["host"] == "central"
    alerts = payload(call("list_alerts", {"service": "web"}, catalog))["alerts"]
    assert [alert["name"] for alert in alerts] == ["web-down"]
    page = payload(call("get_runbook", {"alert": "mcp-hub-down"}, catalog))
    assert page["name"] == "mcp-hub"
    assert page["text"].startswith("#")


def test_a_service_without_a_status_mark_is_listed_as_none() -> None:
    bare = {**CATALOG["services"]["central/web"], "id": "central/bare", "name": "bare"}
    del bare["status"]
    catalog = {**CATALOG, "services": {**CATALOG["services"], "central/bare": bare}}
    rows = payload(call("list_services", {"status": "none"}, catalog))["services"]
    assert [(row["id"], row["status"], row["status_label"]) for row in rows] == [
        ("central/bare", "none", None)
    ]


def test_a_description_of_exactly_the_limit_is_kept_whole() -> None:
    assert server.short("x" * 200) == "x" * 200
    assert server.short("x" * 201) == "x" * 197 + "..."
