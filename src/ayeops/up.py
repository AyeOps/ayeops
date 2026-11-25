from __future__ import annotations

import argparse
import platform
import shlex
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ayeops.defaults import add_manifest, pick_host
from ayeops.doctor import ARCHES
from ayeops.manifest import load_manifest
from ayeops.secretkeys import KeyProblem
from ayeops.secrets import SecretsError, fill_env_file

COMPOSE = "compose.yaml"
ENV = ".env"
DEFAULT_OUT = Path("rendered")

Compose = Callable[[list[str]], int]
Render = Callable[[Path, Path], int]
Which = Callable[[str], str | None]


def compose_runner(argv: list[str]) -> int:
    return subprocess.run(argv, check=False).returncode


def compose_argv(out_dir: Path, host: str, *action: str) -> list[str]:
    return ["docker", "compose", "-f", (out_dir / host / COMPOSE).as_posix(), *action]


def shown(argv: list[str]) -> str:
    return shlex.join(argv)


def arch_warning(declared: str, host: str, machine: str) -> str | None:
    found = ARCHES.get(machine.lower())
    if found is None or found == declared:
        return None
    return (
        f"{host}: the manifest says arch {declared} and this machine is {found};"
        f" set arch: {found} under hosts.{host} to run native images"
    )


def no_docker(which: Which) -> bool:
    if which("docker") is not None:
        return False
    print("docker is not on PATH; install Docker with Compose v2", file=sys.stderr)
    return True


def run_compose(argv: list[str], runner: Compose) -> int:
    print(f"running: {shown(argv)}", flush=True)
    code = runner(argv)
    if code != 0:
        print(f"{shown(argv)} exited {code}", file=sys.stderr)
        return 1
    return 0


def up(
    manifest_path: Path,
    host: str | None,
    out_dir: Path,
    render: Render,
    start: bool = False,
    runner: Compose = compose_runner,
    which: Which = shutil.which,
    machine: str | None = None,
) -> int:
    name = pick_host(manifest_path, host)
    if start and no_docker(which):
        return 2
    code = render(manifest_path, out_dir)
    if code != 0:
        return code
    declared = str(load_manifest(manifest_path).hosts[name].get("arch") or "")
    warning = arch_warning(declared, name, platform.machine() if machine is None else machine)
    if warning is not None:
        print(f"warning: {warning}", file=sys.stderr)
    env = out_dir / name / ENV
    try:
        filled = fill_env_file(env, manifest_path, name)
    except (OSError, SecretsError, KeyProblem) as error:
        print(f"secrets: {error}", file=sys.stderr)
        return 1
    for line in filled.lines:
        print(f"secrets: {line}")
    start_argv = compose_argv(out_dir, name, "up", "-d")
    if filled.unset:
        print(
            f"{env.as_posix()}: set {', '.join(filled.unset)}, then run: {shown(start_argv)}",
            file=sys.stderr,
        )
        return 1
    if start:
        code = run_compose(start_argv, runner)
        if code != 0:
            return code
    else:
        print(f"start:  {shown(start_argv)}")
    print(f"status: {shown(compose_argv(out_dir, name, 'ps'))}")
    print(f"stop:   {shown(compose_argv(out_dir, name, 'down'))}")
    return 0


def down(
    manifest_path: Path,
    host: str | None,
    out_dir: Path,
    volumes: bool = False,
    stop: bool = False,
    runner: Compose = compose_runner,
    which: Which = shutil.which,
) -> int:
    name = pick_host(manifest_path, host)
    compose = out_dir / name / COMPOSE
    if not compose.is_file():
        print(f"{compose.as_posix()} does not exist; aye up renders it", file=sys.stderr)
        return 2
    argv = compose_argv(out_dir, name, "down", *(["--volumes"] if volumes else []))
    if not stop:
        print(f"stop: {shown(argv)}")
        return 0
    if no_docker(which):
        return 2
    return run_compose(argv, runner)


def add_parser(commands: Any) -> None:
    up_parser: argparse.ArgumentParser = commands.add_parser(
        "up",
        help="check, render, write the generated .env and start one host on this machine",
    )
    down_parser: argparse.ArgumentParser = commands.add_parser(
        "down", help="stop the compose project aye up started on this machine"
    )
    for parser in (up_parser, down_parser):
        add_manifest(parser)
        parser.add_argument("--host", help="a host of the manifest, default its only host")
        parser.add_argument(
            "-o",
            "--out",
            type=Path,
            default=DEFAULT_OUT,
            help=f"render directory, default {DEFAULT_OUT}",
        )
    up_parser.add_argument(
        "--start",
        action="store_true",
        help="run docker compose up -d; without it the command is printed, not run",
    )
    down_parser.add_argument(
        "--volumes",
        action="store_true",
        help="also remove the volumes with the stored metrics and Grafana data",
    )
    down_parser.add_argument(
        "--stop",
        action="store_true",
        help="run docker compose down; without it the command is printed, not run",
    )


def run(args: argparse.Namespace, render: Render) -> int:
    if args.command == "up":
        return up(args.manifest, args.host, args.out, render, args.start)
    return down(args.manifest, args.host, args.out, args.volumes, args.stop)
