from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

from ayeops.discover import probe
from ayeops.discover.adopt import AdoptError, adopt, remote_dir, takeover
from ayeops.discover.suggest import suggest
from ayeops.manifest import DEFAULT_TIMEZONE, load_manifest
from ayeops.registry import load_modules
from ayeops.render import FleetRender, dump_yaml
from ayeops.rules import check_manifest, check_warnings
from ayeops.waivers import today_in

PROBE_PATH = Path(probe.__file__)
SSH_TIMEOUT = 300
ADOPT_FLAGS = ("project", "until", "observe")

Runner = Callable[..., "subprocess.CompletedProcess[bytes]"]


class DiscoverError(Exception):
    pass


def probe_source() -> bytes:
    return PROBE_PATH.read_bytes().replace(b"\r\n", b"\n")


def remote_command(sudo: bool, python: str) -> str:
    return f"sudo -n {python} -" if sudo else f"{python} -"


def ssh_argv(target: str, sudo: bool, python: str) -> list[str]:
    return ["ssh", "-o", "BatchMode=yes", target, remote_command(sudo, python)]


def fetch_remote(
    target: str, sudo: bool, python: str = "python3", run: Runner = subprocess.run
) -> dict[str, Any]:
    try:
        done = run(
            ssh_argv(target, sudo, python),
            input=probe_source(),
            capture_output=True,
            timeout=SSH_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise DiscoverError(f"ssh {target}: {error}") from error
    stderr = done.stderr.decode("utf-8", errors="replace").strip()
    if done.returncode != 0:
        reason = stderr.splitlines()[-1] if stderr else "no output"
        raise DiscoverError(f"ssh {target}: exit code {done.returncode}: {reason}")
    try:
        report: dict[str, Any] = json.loads(done.stdout.decode("utf-8", errors="replace"))
    except ValueError as error:
        raise DiscoverError(f"ssh {target}: the probe did not print JSON") from error
    return report


def known_services() -> set[str]:
    return {name for module in load_modules().values() for name in module.catalog}


def read_report(report_path: Path) -> dict[str, Any]:
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise DiscoverError(f"{report_path}: {error}") from error
    if not isinstance(report, dict):
        raise DiscoverError(f"{report_path}: not a discovery report")
    return report


def suggestion_text(report_path: Path) -> str:
    return dump_yaml(suggest(read_report(report_path), known_services()))


def day(text: str) -> date:
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a day as YYYY-MM-DD") from None


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def adopt_host(args: argparse.Namespace, today: date | None = None) -> int:
    if args.out is None:
        raise DiscoverError("--adopt writes a manifest and its compose files: name it with -o")
    today = today or today_in(DEFAULT_TIMEZONE)
    report = read_report(args.adopt)
    modules = load_modules()
    try:
        adoption = adopt(report, modules, today, args.project, args.until, args.observe)
    except AdoptError as error:
        raise DiscoverError(f"{args.adopt}: {error}") from error
    files = {args.out: dump_yaml(adoption.manifest)}
    files |= {
        args.out.parent / path: dump_yaml(doc) for path, doc in adoption.compose_files.items()
    }
    existing = [str(path) for path in files if path.exists()]
    if existing:
        raise DiscoverError(f"{', '.join(existing)}: exists already, --adopt writes new files only")
    for path, text in files.items():
        write_text(path, text)
        print(f"{path}: written", file=sys.stderr)
    for note in adoption.notes:
        print(f"note: {note}", file=sys.stderr)
    manifest = load_manifest(args.out)
    findings = check_manifest(manifest, modules, today)
    warnings = check_warnings(manifest, today, modules)
    for finding in findings + warnings:
        print(finding, file=sys.stderr)
    if findings:
        print(f"{args.out}: {len(findings)} error(s), fix them before aye render", file=sys.stderr)
        return 1
    compose = FleetRender(manifest, modules).compose(adoption.host)
    taken = takeover(report, compose, remote_dir(adoption.host))
    for problem in taken:
        print(f"error [adopt-takeover] hosts.{adoption.host}: {problem}", file=sys.stderr)
    if taken:
        return 1
    live = sum(1 for item in adoption.services if item.live)
    print(
        f"{args.out}: {adoption.host} adopted, {live} running and"
        f" {len(adoption.services) - live} stopped service(s) in"
        f" {len(adoption.compose_files)} compose project(s), {len(compose['services'])} AyeOps"
        f" container(s) to add, {len(warnings)} warning(s); the waivers end on {adoption.until}",
        file=sys.stderr,
    )
    return 0


def report_text(report: dict[str, Any]) -> str:
    return json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def local_report() -> dict[str, Any]:
    if not sys.platform.startswith("linux"):
        raise DiscoverError("the probe reads Linux hosts; run it on one or use --ssh <host>")
    return probe.collect(probe.Host())


def add_parser(commands: Any) -> None:
    parser: argparse.ArgumentParser = commands.add_parser(
        "discover",
        help="probe a Linux host read-only, turn a report into manifest suggestions or adopt it",
    )
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--ssh", metavar="HOST", help="run the probe on HOST over ssh")
    source.add_argument(
        "--suggest", type=Path, metavar="REPORT", help="print a manifest fragment for a report"
    )
    source.add_argument(
        "--adopt",
        type=Path,
        metavar="REPORT",
        help="write a manifest that describes the host as it runs, with -o",
    )
    parser.add_argument("--sudo", action="store_true", help="run the remote probe with sudo -n")
    parser.add_argument("--python", default="python3", help="remote interpreter, 3.10 or newer")
    parser.add_argument("-o", "--out", type=Path, help="write to a file instead of stdout")
    parser.add_argument(
        "--project", help="manifest project of --adopt, default ayeops: compose project <p>-<host>"
    )
    parser.add_argument(
        "--until",
        type=day,
        metavar="YYYY-MM-DD",
        help="last day of the waivers --adopt writes, default in fourteen days",
    )
    parser.add_argument(
        "--observe",
        action="store_true",
        help="--adopt also enables the read-only exporters whose ports and names are free",
    )


def run(args: argparse.Namespace, fetch: Callable[..., dict[str, Any]] = fetch_remote) -> int:
    try:
        if args.sudo and not args.ssh:
            raise DiscoverError("--sudo applies to --ssh; run sudo aye discover locally instead")
        extra = [flag for flag in ADOPT_FLAGS if getattr(args, flag)]
        if extra and args.adopt is None:
            raise DiscoverError(f"--{extra[0]} applies to --adopt")
        if args.adopt is not None:
            return adopt_host(args)
        if args.suggest is not None:
            text = suggestion_text(args.suggest)
        elif args.ssh:
            text = report_text(fetch(args.ssh, args.sudo, args.python))
        else:
            text = report_text(local_report())
    except DiscoverError as error:
        print(error, file=sys.stderr)
        return 1
    if args.out is None:
        sys.stdout.write(text)
        return 0
    write_text(args.out, text)
    print(f"{args.out}: written", file=sys.stderr)
    return 0
