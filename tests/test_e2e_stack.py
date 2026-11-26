from __future__ import annotations

import base64
import fnmatch
import shutil
import socket
import ssl
import threading
from pathlib import Path
from typing import Any

import pytest
import yaml

from ayeops.cli import main as aye
from tests.conftest import WAIT
from tests.e2e import stack


class OverrideLoader(yaml.SafeLoader):
    pass


def _sequence(loader: yaml.SafeLoader, node: yaml.Node) -> list[Any]:
    assert isinstance(node, yaml.SequenceNode)
    return loader.construct_sequence(node)


OverrideLoader.add_constructor("!override", _sequence)


def load_override(path: Path) -> Any:
    return yaml.load(path.read_text(encoding="utf-8"), Loader=OverrideLoader)


needs_openssl = pytest.mark.skipif(shutil.which("openssl") is None, reason="needs openssl")


def test_values_follow_the_documented_formats() -> None:
    pool: dict[str, str] = {}
    key = stack.env_value("AYE_OBJECTSTORE_ACCESS_KEY_ID", pool)
    assert key.startswith("GK") and len(key) == 26
    int(key[2:], 16)
    assert len(stack.env_value("AYE_OBJECTSTORE_RPC_SECRET", pool)) == 64
    assert len(stack.env_value("AYE_SECRETS_ENCRYPTION_KEY", pool)) == 32
    cookie = stack.env_value("AYE_AUTH_COOKIE_SECRET", pool)
    assert len(base64.urlsafe_b64decode(cookie)) == 32
    assert "+" not in cookie and "/" not in cookie
    assert all(c in "0123456789abcdef" for c in stack.env_value("AYE_ANYTHING_NEW", pool))


def test_offsite_keys_are_the_objectstore_keys() -> None:
    pool: dict[str, str] = {}
    central = stack.env_text(["AYE_OBJECTSTORE_ACCESS_KEY_ID"], pool)
    edge = stack.env_text(["AYE_OFFSITE_ACCESS_KEY_ID", "AYE_OFFSITE_SECRET_ACCESS_KEY"], pool)
    assert central.strip().split("=", 1)[1] == edge.splitlines()[0].split("=", 1)[1]
    assert pool["AYE_OBJECTSTORE_SECRET_ACCESS_KEY"] in edge


def test_env_names_ignore_blank_lines() -> None:
    assert stack.env_names("A=replace-me\n\nB=replace-me\n") == ["A", "B"]


def test_loopback_moves_only_local_ports() -> None:
    compose = {
        "services": {
            "vmagent": {"ports": ["127.0.0.1:8429:8429"]},
            "garage": {"ports": ["10.213.0.1:3900:3900", "127.0.0.1:3903:3903"]},
            "offsite": {},
        }
    }
    moved = stack.loopback_override(compose, "127.0.0.2")
    assert moved == {
        "vmagent": {"ports": ["127.0.0.2:8429:8429"]},
        "garage": {"ports": ["10.213.0.1:3900:3900", "127.0.0.2:3903:3903"]},
    }
    assert stack.loopback_override(compose, "127.0.0.1") == {}


def test_port_lists_replace_instead_of_append() -> None:
    text = yaml.safe_dump({"services": {"a": {"ports": stack.Override(["127.0.0.2:1:1"])}}})
    assert "ports: !override" in text


def test_the_manifest_backs_up_the_file_the_stack_writes() -> None:
    data = yaml.safe_load(stack.MANIFEST.read_text(encoding="utf-8"))
    offsite = [
        spec["modules"]["offsite"]
        for spec in data["hosts"].values()
        if "offsite" in spec["modules"]
    ]
    assert len(offsite) == 1
    assert fnmatch.fnmatch(stack.BACKUP_FILE.as_posix(), offsite[0]["source"])


def test_the_simulated_hosts_are_in_the_manifest() -> None:
    hosts = yaml.safe_load(stack.MANIFEST.read_text(encoding="utf-8"))["hosts"]
    assert set(stack.DEPLOYED) <= set(hosts)
    assert hosts[stack.CENTRAL]["address"] == stack.GATEWAY
    assert len(set(stack.LOOPBACKS.values())) == len(stack.LOOPBACKS)


def test_the_e2e_manifest_passes_the_rules() -> None:
    assert aye(["check", str(stack.MANIFEST)]) == 0


@needs_openssl
def test_prepare_writes_env_overrides_and_state(tmp_path: Path) -> None:
    render = tmp_path / "r"
    assert aye(["render", str(stack.MANIFEST), "-o", str(render)]) == 0
    backup = tmp_path / "backups" / "e2e.dump"
    state = stack.prepare(render, backup)
    assert backup.stat().st_size == 64 * 1024
    assert set(state["hosts"]) == {"central", "edge", "offline"}
    for host in state["hosts"]:
        example = (render / host / ".env.example").read_text(encoding="utf-8")
        env = (render / host / ".env").read_text(encoding="utf-8")
        assert stack.env_names(env) == stack.env_names(example)
        assert "replace-me" not in env
    edge = load_override(render / "edge" / stack.OVERRIDE)
    for service in edge["services"].values():
        assert all(port.startswith("127.0.0.2:") for port in service["ports"])
    central = load_override(render / "central" / stack.OVERRIDE)
    assert central["services"]["blackbox"]["environment"]["SSL_CERT_FILE"] == stack.CA_MOUNT
    assert state["hosts"]["central"]["domain"] == "ops.e2e.test"
    assert state["hosts"]["central"]["files"] == ["compose.yaml", stack.OVERRIDE]
    assert state["hosts"]["offline"]["deployed"] is False


@needs_openssl
def test_the_local_ca_signs_a_certificate_for_every_route(tmp_path: Path) -> None:
    (tmp_path / stack.CERT_DIR).mkdir(parents=True)
    stack.local_ca(tmp_path, "ops.e2e.test")
    certs = tmp_path / stack.CERT_DIR
    server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server.load_cert_chain(certs / "e2e-tls.crt", certs / "e2e-tls.key")
    listener = socket.create_server(("127.0.0.1", 0))
    port = listener.getsockname()[1]

    def serve() -> None:
        connection, _ = listener.accept()
        try:
            with server.wrap_socket(connection, server_side=True) as tls:
                tls.recv(1)
        except (ssl.SSLError, OSError):
            pass

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    client = ssl.create_default_context(cafile=str(tmp_path / stack.CA_FILE))
    with (
        socket.create_connection(("127.0.0.1", port), timeout=WAIT) as raw,
        client.wrap_socket(raw, server_hostname="portal.ops.e2e.test") as tls,
    ):
        assert tls.version()
    thread.join(timeout=WAIT)
    listener.close()
    tls_yaml = yaml.safe_load((certs / "e2e-tls.yml").read_text(encoding="utf-8"))
    default = tls_yaml["tls"]["stores"]["default"]["defaultCertificate"]
    assert default["certFile"].endswith("/e2e-tls.crt")


def test_a_nearly_full_docker_disk_stops_the_run_before_loki_does() -> None:
    gib = 2**30
    assert stack.disk_problem(100 * gib, 25 * gib) is None
    assert stack.disk_problem(100 * gib, 20 * gib) is None
    problem = stack.disk_problem(100 * gib, 10 * gib)
    assert problem is not None
    assert "90.0 %" in problem


@pytest.mark.parametrize(
    ("root", "prefix"), [(True, ["iptables"]), (False, ["sudo", "-n", "iptables"])]
)
def test_the_forwarding_rule_needs_sudo_only_outside_the_box(
    monkeypatch: pytest.MonkeyPatch, root: bool, prefix: list[str]
) -> None:
    calls: list[list[str]] = []

    def record(args: list[str], check: bool = True) -> int:
        calls.append(list(args))
        return 1

    monkeypatch.setattr(stack, "is_root", lambda: root)
    monkeypatch.setattr(stack, "run", record)
    stack.open_forwarding()
    rule = ["DOCKER-USER", "-m", "conntrack", "--ctstate", "DNAT", "-j", "ACCEPT"]
    assert calls == [[*prefix, "-C", *rule], [*prefix, "-I", *rule]]
