from __future__ import annotations

import argparse
import base64
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import yaml

from ayeops.cli import main as aye

HERE = Path(__file__).resolve().parent
MANIFEST = HERE / "e2e.yaml"
APPS = HERE / "apps" / "app.compose.yaml"
APPS_PROJECT = "e2e-apps"
CENTRAL = "central"
DEPLOYED = ("central", "edge")
LOOPBACKS = {"central": "127.0.0.1", "edge": "127.0.0.2"}
LOCALHOST = "127.0.0.1"
NETWORK = "aye-e2e"
SUBNET = "10.213.0.0/24"
GATEWAY = "10.213.0.1"
BACKUP_FILE = Path("/tmp/aye-e2e-backups/e2e.dump")
OVERRIDE = "e2e.override.yaml"
STATE = "e2e-stack.json"
CA_FILE = "e2e-ca.crt"
CA_MOUNT = "/etc/aye-e2e/ca.crt"
CERT_DIR = Path("ingress") / "dynamic"
WAIT_SECONDS = 480
DISK_LIMIT_PCT = 80


def hex_bytes(size: int) -> Callable[[], str]:
    return lambda: secrets.token_hex(size)


def base64_bytes(size: int, urlsafe: bool = False) -> Callable[[], str]:
    encode = base64.urlsafe_b64encode if urlsafe else base64.b64encode
    return lambda: encode(os.urandom(size)).decode()


FORMATS: dict[str, Callable[[], str]] = {
    "AYE_AUTH_COOKIE_SECRET": base64_bytes(32, urlsafe=True),
    "AYE_GRAFANA_ADMIN_USER": lambda: "e2e-admin",
    "AYE_OBJECTSTORE_ACCESS_KEY_ID": lambda: "GK" + secrets.token_hex(12),
    "AYE_OBJECTSTORE_ADMIN_TOKEN": base64_bytes(32),
    "AYE_OBJECTSTORE_RPC_SECRET": hex_bytes(32),
    "AYE_OBJECTSTORE_SECRET_ACCESS_KEY": hex_bytes(32),
    "AYE_OFFSITE_RESTIC_PASSWORD": base64_bytes(33),
    "AYE_SECRETS_AUTH_SECRET": base64_bytes(32),
    "AYE_SECRETS_ENCRYPTION_KEY": hex_bytes(16),
}
SAME_AS = {
    "AYE_OFFSITE_ACCESS_KEY_ID": "AYE_OBJECTSTORE_ACCESS_KEY_ID",
    "AYE_OFFSITE_SECRET_ACCESS_KEY": "AYE_OBJECTSTORE_SECRET_ACCESS_KEY",
}


def env_names(example: str) -> list[str]:
    names = []
    for line in example.splitlines():
        name, sep, _ = line.partition("=")
        if sep and name.strip():
            names.append(name.strip())
    return names


def env_value(name: str, pool: dict[str, str]) -> str:
    canonical = SAME_AS.get(name, name)
    if canonical not in pool:
        pool[canonical] = FORMATS.get(canonical, hex_bytes(24))()
    return pool[canonical]


def env_text(names: Sequence[str], pool: dict[str, str]) -> str:
    return "".join(f"{name}={env_value(name, pool)}\n" for name in names)


class Override(list[str]):
    pass


def _override(dumper: yaml.SafeDumper, data: Override) -> yaml.Node:
    return dumper.represent_sequence("!override", list(data))


yaml.SafeDumper.add_representer(Override, _override)


def rebind(port: str, loopback: str) -> str:
    prefix = f"{LOCALHOST}:"
    return loopback + port[len(LOCALHOST) :] if port.startswith(prefix) else port


def loopback_override(compose: dict[str, Any], loopback: str) -> dict[str, Any]:
    services: dict[str, Any] = {}
    if loopback == LOCALHOST:
        return services
    for name, service in compose["services"].items():
        ports = [str(port) for port in service.get("ports") or []]
        moved = [rebind(port, loopback) for port in ports]
        if moved != ports:
            services[name] = {"ports": Override(moved)}
    return services


def ca_override(compose: dict[str, Any]) -> dict[str, Any]:
    if "blackbox" not in compose["services"]:
        return {}
    return {
        "blackbox": {
            "environment": {"SSL_CERT_FILE": CA_MOUNT},
            "volumes": [f"./{CA_FILE}:{CA_MOUNT}:ro"],
        }
    }


def merge(*parts: dict[str, Any]) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for part in parts:
        for name, service in part.items():
            merged.setdefault(name, {}).update(service)
    return merged


def ingress_domain(host_dir: Path) -> str | None:
    routes = host_dir / CERT_DIR / "routes.yml"
    if not routes.is_file():
        return None
    data = yaml.safe_load(routes.read_text(encoding="utf-8")) or {}
    for router in ((data.get("http") or {}).get("routers") or {}).values():
        for domain in (router.get("tls") or {}).get("domains") or []:
            return str(domain["main"]).removeprefix("*.")
    return None


def tls_files() -> str:
    return (
        "tls:\n"
        "  stores:\n"
        "    default:\n"
        "      defaultCertificate:\n"
        "        certFile: /etc/traefik/dynamic/e2e-tls.crt\n"
        "        keyFile: /etc/traefik/dynamic/e2e-tls.key\n"
    )


def openssl(args: Sequence[str]) -> None:
    subprocess.run(["openssl", *args], check=True, capture_output=True)


def local_ca(host_dir: Path, domain: str) -> None:
    certs = host_dir / CERT_DIR
    with tempfile.TemporaryDirectory() as scratch:
        work = Path(scratch)
        extensions = work / "leaf.ext"
        extensions.write_text(
            f"subjectAltName=DNS:*.{domain},DNS:{domain}\n"
            "basicConstraints=CA:FALSE\n"
            "extendedKeyUsage=serverAuth\n",
            encoding="utf-8",
        )
        openssl(
            [
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-days",
                "30",
                "-keyout",
                str(work / "ca.key"),
                "-out",
                str(host_dir / CA_FILE),
                "-subj",
                "/CN=AyeOps e2e CA",
                "-addext",
                "basicConstraints=critical,CA:TRUE",
                "-addext",
                "keyUsage=critical,keyCertSign,cRLSign",
            ]
        )
        openssl(
            [
                "req",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-keyout",
                str(certs / "e2e-tls.key"),
                "-out",
                str(work / "leaf.csr"),
                "-subj",
                f"/CN=*.{domain}",
            ]
        )
        openssl(
            [
                "x509",
                "-req",
                "-days",
                "30",
                "-in",
                str(work / "leaf.csr"),
                "-CA",
                str(host_dir / CA_FILE),
                "-CAkey",
                str(work / "ca.key"),
                "-CAcreateserial",
                "-out",
                str(certs / "e2e-tls.crt"),
                "-extfile",
                str(extensions),
            ]
        )
    (certs / "e2e-tls.yml").write_text(tls_files(), encoding="utf-8")


def host_dirs(render: Path) -> list[Path]:
    return sorted(path.parent for path in render.glob("*/compose.yaml"))


def prepare(render: Path, backup: Path = BACKUP_FILE) -> dict[str, Any]:
    pool: dict[str, str] = {}
    hosts: dict[str, Any] = {}
    for host_dir in host_dirs(render):
        host = host_dir.name
        example = host_dir / ".env.example"
        names = env_names(example.read_text(encoding="utf-8")) if example.is_file() else []
        (host_dir / ".env").write_text(env_text(names, pool), encoding="utf-8")
        compose = yaml.safe_load((host_dir / "compose.yaml").read_text(encoding="utf-8"))
        loopback = LOOPBACKS.get(host, LOCALHOST)
        domain = ingress_domain(host_dir)
        parts = [loopback_override(compose, loopback)]
        if domain:
            local_ca(host_dir, domain)
            parts.append(ca_override(compose))
        files = ["compose.yaml"]
        services = merge(*parts)
        if services:
            (host_dir / OVERRIDE).write_text(
                yaml.safe_dump({"services": services}, sort_keys=True), encoding="utf-8"
            )
            files.append(OVERRIDE)
        hosts[host] = {
            "loopback": loopback,
            "files": files,
            "deployed": host in DEPLOYED,
            "project": compose["name"],
            "domain": domain,
        }
    backup.parent.mkdir(parents=True, exist_ok=True)
    backup.write_bytes(os.urandom(64 * 1024))
    hour_ago = time.time() - 3600
    os.utime(backup, (hour_ago, hour_ago))
    state = {"network": NETWORK, "gateway": GATEWAY, "central": CENTRAL, "hosts": hosts}
    (render / STATE).write_text(json.dumps(state, indent=2), encoding="utf-8")
    return state


def run(args: Sequence[str], check: bool = True) -> int:
    print("+", " ".join(args), flush=True)
    return subprocess.run(list(args), check=check).returncode


def compose_args(render: Path, host: str, state: dict[str, Any]) -> list[str]:
    host_dir = render / host
    args = ["docker", "compose", "--project-directory", str(host_dir)]
    for name in state["hosts"][host]["files"]:
        args += ["-f", str(host_dir / name)]
    return args


def apps_args(render: Path) -> list[str]:
    return ["docker", "compose", "-f", str(APPS), "--env-file", str(render / CENTRAL / ".env")]


def load_state(render: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((render / STATE).read_text(encoding="utf-8"))
    return data


def validate(render: Path, state: dict[str, Any]) -> None:
    for host in state["hosts"]:
        run([*compose_args(render, host, state), "config", "-q"])
    run([*apps_args(render), "config", "-q"])


def is_root() -> bool:
    geteuid = getattr(os, "geteuid", None)
    return geteuid is not None and geteuid() == 0


def open_forwarding() -> None:
    rule = ["DOCKER-USER", "-m", "conntrack", "--ctstate", "DNAT", "-j", "ACCEPT"]
    iptables = ["iptables"] if is_root() else ["sudo", "-n", "iptables"]
    if run([*iptables, "-C", *rule], check=False) != 0:
        run([*iptables, "-I", *rule], check=False)


def pull(render: Path, state: dict[str, Any]) -> None:
    commands = [[*apps_args(render), "pull", "--quiet"]]
    commands += [
        [*compose_args(render, host, state), "pull", "--quiet"]
        for host, spec in state["hosts"].items()
        if spec["deployed"]
    ]
    with ThreadPoolExecutor(len(commands)) as pool:
        codes = list(pool.map(lambda args: run(args, check=False), commands))
    if any(codes):
        raise SystemExit("docker compose pull failed")


def wait_args() -> list[str]:
    return ["up", "-d", "--wait", "--wait-timeout", str(WAIT_SECONDS)]


def restic_init(render: Path, state: dict[str, Any], host: str) -> None:
    base = compose_args(render, host, state)
    run([*base, *wait_args(), "offsite-restic"])
    restic = [*base, "run", "--rm", "-T", "offsite", "/opt/restic/restic"]
    for attempt in range(5):
        if run([*restic, "cat", "config"], check=False) == 0:
            return
        if run([*restic, "init"], check=False) == 0:
            return
        time.sleep(5 * (attempt + 1))
    raise SystemExit("restic init failed")


def disk_problem(total: int, free: int, limit: int = DISK_LIMIT_PCT) -> str | None:
    used = 100 * (total - free) / total
    if used <= limit:
        return None
    return (
        f"the Docker data root is {used:.1f} % full, above {limit} %: Loki refuses every push"
        " once its disk passes 90 %, and the disk alerts fire on the runner instead of the stack"
    )


def docker_root() -> str:
    args = ["docker", "info", "--format", "{{.DockerRootDir}}"]
    return subprocess.run(args, check=True, capture_output=True, text=True).stdout.strip()


def check_disk() -> None:
    root = docker_root()
    usage = shutil.disk_usage(root)
    print(f"{root}: {usage.free / 2**30:.1f} GiB free of {usage.total / 2**30:.1f}", flush=True)
    problem = disk_problem(usage.total, usage.free)
    if problem:
        raise SystemExit(problem)


def up(render: Path) -> None:
    state = load_state(render)
    validate(render, state)
    if run(["docker", "network", "inspect", NETWORK], check=False) != 0:
        run(["docker", "network", "create", "--subnet", SUBNET, "--gateway", GATEWAY, NETWORK])
    open_forwarding()
    pull(render, state)
    check_disk()
    run([*apps_args(render), *wait_args()])
    for host in DEPLOYED:
        compose = yaml.safe_load((render / host / "compose.yaml").read_text(encoding="utf-8"))
        if "offsite" in compose["services"]:
            restic_init(render, state, host)
        args = compose_args(render, host, state)
        if run([*args, *wait_args()], check=False) != 0:
            run([*args, "ps", "-a"], check=False)
            run([*args, "logs", "--no-color", "--tail", "60"], check=False)
            raise SystemExit(f"{host}: the stack did not become healthy")


def logs(render: Path, out: Path) -> None:
    state = load_state(render)
    out.mkdir(parents=True, exist_ok=True)
    targets = {APPS_PROJECT: apps_args(render)}
    targets.update({host: compose_args(render, host, state) for host in DEPLOYED})
    for name, args in targets.items():
        for command, suffix in ((["logs", "--no-color", "-t"], "log"), (["ps", "-a"], "ps")):
            with (out / f"{name}.{suffix}").open("w", encoding="utf-8") as handle:
                subprocess.run([*args, *command], stdout=handle, stderr=subprocess.STDOUT)


def down(render: Path) -> None:
    state = load_state(render)
    for host in reversed(DEPLOYED):
        run([*compose_args(render, host, state), "down", "-v", "--remove-orphans"], check=False)
    run([*apps_args(render), "down", "-v"], check=False)
    run(["docker", "network", "rm", NETWORK], check=False)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tests.e2e.stack")
    parser.add_argument(
        "command", choices=["render", "prepare", "validate", "up", "all", "logs", "down"]
    )
    parser.add_argument("render", type=Path)
    parser.add_argument("--out", type=Path, default=Path("e2e-logs"))
    args = parser.parse_args(argv)
    render: Path = args.render
    if args.command in ("render", "all"):
        code = aye(["render", str(MANIFEST), "-o", str(render)])
        if code:
            return code
    if args.command in ("prepare", "all"):
        prepare(render)
    if args.command == "validate":
        validate(render, load_state(render))
    if args.command in ("up", "all"):
        up(render)
    if args.command == "logs":
        logs(render, args.out)
    if args.command == "down":
        down(render)
    return 0


if __name__ == "__main__":
    sys.exit(main())
