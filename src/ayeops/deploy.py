from __future__ import annotations

import argparse
import io
import json
import re
import shlex
import subprocess
import sys
import tarfile
import threading
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

import yaml

from ayeops.backups import unit_files
from ayeops.defaults import add_manifest, only_host
from ayeops.manifest import Manifest, load_manifest
from ayeops.mounts import services_touching
from ayeops.registry import Module, load_modules
from ayeops.render import host_settings, render_tree
from ayeops.retire import RULES, read_script, retire_rules
from ayeops.rules import check_manifest
from ayeops.stamp import current_stamp

REMOTE_ROOT = "/opt/ayeops"
INDEX = ".aye-files"
STAGING = ".aye-staging"
EXECUTABLE = (".sh", ".py")
TIMEOUT = 1800
TIMED_OUT = 124
HOSTSIDE = Path(__file__).parent / "hostside"
HELPER_DIR = "/usr/local/libexec/ayeops"
HELPER = f"{HELPER_DIR}/aye-host"
SUDO_RS = "sudo-rs "
STEPS = ("rules", "pull", "swap", "up", "ps", "diff", "timers")
HELPER_TARGET = re.compile(r"^/opt/ayeops/[A-Za-z0-9_-][A-Za-z0-9._-]*$")
CENTRAL = ("grafana", "objectstore")
SERVERS = ("metrics", "logs")
DEPLOYED = "deployed"
FAILED = "failed"
SKIPPED = "skipped"
TIMED_OUT_RESULT = "timed out"
UNREACHABLE = 255
STAGED = "staged"


@dataclass(frozen=True)
class Result:
    code: int
    out: str = ""
    err: str = ""


Runner = Callable[[list[str], bytes | None, float], Result]


@dataclass(frozen=True)
class Connection:
    alias: str
    sudo: bool
    helper: bool
    directory: str


def connection(
    manifest: Manifest,
    host: str,
    ssh: str | None = None,
    sudo: bool = False,
    helper: bool = False,
    directory: str | None = None,
) -> Connection:
    entry = manifest.hosts[host]
    spec = entry.get("connect") if isinstance(entry, dict) else None
    if not isinstance(spec, dict):
        spec = {}
    if not (sudo or helper):
        sudo, helper = spec.get("sudo") is True, spec.get("helper") is True
    alias = ssh or spec.get("ssh") or host
    target = directory or spec.get("dir") or f"{REMOTE_ROOT}/{host}"
    return Connection(str(alias), sudo, helper, str(target))


def refuse_helper_dirs(reach: Iterable[Connection]) -> bool:
    refused = sorted(
        {
            found.directory
            for found in reach
            if found.helper and not HELPER_TARGET.match(found.directory)
        }
    )
    for path in refused:
        print(f"--helper deploys only to {REMOTE_ROOT}/<name>, not {path}", file=sys.stderr)
    return bool(refused)


def ssh_runner(argv: list[str], stdin: bytes | None, timeout: float) -> Result:
    try:
        done = subprocess.run(argv, input=stdin, capture_output=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return Result(TIMED_OUT, "", f"{argv[0]} timed out after {timeout:g}s")
    return Result(
        done.returncode,
        done.stdout.decode("utf-8", "replace"),
        done.stderr.decode("utf-8", "replace"),
    )


def hostside(name: str) -> str:
    return (HOSTSIDE / name).read_text(encoding="utf-8").replace("\r\n", "\n")


def host_files(tree: dict[str, str], host: str) -> dict[str, str]:
    prefix = f"{host}/"
    return {
        path.removeprefix(prefix): text for path, text in tree.items() if path.startswith(prefix)
    }


def tarball(files: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    entries = {**files, INDEX: "".join(f"{path}\n" for path in sorted(files))}
    with tarfile.open(fileobj=buffer, mode="w:gz", format=tarfile.PAX_FORMAT) as archive:
        for path, text in sorted(entries.items()):
            data = text.encode("utf-8")
            info = tarfile.TarInfo(path)
            info.size = len(data)
            info.mode = 0o755 if path.endswith(EXECUTABLE) else 0o644
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def swap_script(directory: str) -> str:
    return f"set -- {shlex.quote(directory)}\n{hostside('swap.sh')}"


def diff_script(directory: str) -> str:
    return f"set -- {shlex.quote(directory)}\n{hostside('diff.sh')}"


def timers_script(directory: str) -> str:
    return f"set -- {shlex.quote(directory)}\n{hostside('timers.sh')}"


def pull_script(directory: str) -> str:
    return f"set -- {shlex.quote(directory)}\n{hostside('pull.sh')}"


def remote(script: str, sudo: bool) -> str:
    command = f"sh -c {shlex.quote(script)}"
    return f"sudo {command}" if sudo else command


def compose_script(directory: str, action: str) -> str:
    return f"cd {shlex.quote(directory)} && docker compose {action}"


def remote_steps(target: str, sudo: bool, helper: bool) -> dict[str, str]:
    if helper:
        return {step: f"sudo -n {HELPER} {target} {step}" for step in STEPS}
    return {
        "rules": remote(read_script(f"{target}/{RULES}"), sudo),
        "pull": remote(pull_script(target), sudo),
        "swap": remote(swap_script(target), sudo),
        "up": remote(compose_script(target, "up -d --remove-orphans"), sudo),
        "ps": remote(compose_script(target, "ps --all --format json"), sudo),
        "diff": remote(diff_script(target), sudo),
        "timers": remote(timers_script(target), sudo),
    }


def parse_swap(out: str) -> tuple[list[str], list[str]]:
    changed: list[str] = []
    removed: list[str] = []
    for line in out.splitlines():
        word, _, path = line.partition(" ")
        if word == "changed" and path:
            changed.append(path)
        elif word == "removed" and path:
            removed.append(path)
    return changed, removed


def installed_units(out: str) -> list[str]:
    found = []
    for line in out.splitlines():
        word, _, unit = line.partition(" ")
        if word == "installed" and unit:
            found.append(unit)
    return found


def parse_pull(out: str) -> tuple[bool, list[str]]:
    staged = False
    failed: list[str] = []
    for line in out.splitlines():
        word, _, service = line.partition(" ")
        if line == STAGED:
            staged = True
        elif word == "failed" and service:
            failed.append(service)
    return staged, failed


def failed_pull(compose: dict[str, Any], service: str) -> str:
    spec = (compose.get("services") or {}).get(service)
    image = spec.get("image") if isinstance(spec, dict) else None
    return f"image {image} of {service} did not pull" if image else f"{service} did not pull"


def pull_failed(
    pulled: Result, files: dict[str, str], alias: str, helper: bool, err: TextIO
) -> None:
    if pulled.err.strip():
        print(pulled.err.strip(), file=err)
    staged, failed = parse_pull(pulled.out)
    compose = yaml.safe_load(files["compose.yaml"]) or {}
    for service in failed:
        print(f"{alias}: {failed_pull(compose, service)}", file=err)
    if pulled.code == TIMED_OUT:
        print(f"{alias}: pull timed out, the running stack is untouched", file=err)
        return
    print(f"{alias}: images not pulled, the running stack is untouched", file=err)
    if helper and not staged and pulled.code != UNREACHABLE:
        print(
            f"{alias}: --helper needs the pull step of {HELPER}; "
            "run the aye host-init script of this host again if it predates the pull step",
            file=err,
        )


def parse_ps(out: str) -> list[dict[str, Any]]:
    text = out.strip()
    if not text:
        return []
    if text.startswith("["):
        rows = json.loads(text)
    else:
        rows = [json.loads(line) for line in text.splitlines() if line.strip()]
    return [row for row in rows if isinstance(row, dict)]


def status_rows(
    containers: list[dict[str, Any]], touched: dict[str, list[str]]
) -> list[tuple[str, str, str, str]]:
    rows = []
    for container in containers:
        service = str(container.get("Service") or "")
        config = f"{len(touched[service])} file(s)" if service in touched else ""
        rows.append(
            (service, str(container.get("State") or ""), str(container.get("Health") or ""), config)
        )
    return sorted(rows)


def healthy(row: tuple[str, str, str, str]) -> bool:
    return row[1] == "running" and row[2] != "unhealthy"


def table(rows: list[tuple[str, str, str, str]]) -> str:
    header = ("SERVICE", "STATE", "HEALTH", "CONFIG CHANGED")
    lines = [header, *rows]
    columns = range(len(header))
    widths = [max(len(line[column]) for line in lines) for column in columns]
    return "\n".join(
        "  ".join(line[column].ljust(widths[column]) for column in columns).rstrip()
        for line in lines
    )


def misplaced(files: dict[str, str], host: str, target: str) -> str | None:
    home = f"{REMOTE_ROOT}/{host}"
    if not unit_files(files) or target == home:
        return None
    return (
        f"{host}: its backup timers run the jobs from {home}, deploy it there and not to {target}"
    )


def install_timers(
    files: dict[str, str],
    removed: list[str],
    installed: list[str],
    alias: str,
    steps: dict[str, str],
    runner: Runner,
    timeout: float,
    out: TextIO,
    err: TextIO,
) -> bool:
    units = unit_files(files)
    if not units and not unit_files(removed) and not installed:
        return True
    done = runner(["ssh", alias, steps["timers"]], None, timeout)
    if done.code != 0:
        print(done.err.strip(), file=err)
        print(f"{alias}: backup timers not installed", file=err)
        if steps["timers"].startswith(f"sudo -n {HELPER} ") and done.code != TIMED_OUT:
            print(
                f"{alias}: --helper needs the timers step of {HELPER}; "
                "run the aye host-init script of this host again if it predates backup timers",
                file=err,
            )
        return False
    changed, gone = parse_swap(done.out)
    timers = len([unit for unit in units if unit.endswith(".timer")])
    print(
        f"{alias}: {timers} backup timer(s), {len(changed)} unit file(s) changed,"
        f" {len(gone)} removed",
        file=out,
    )
    return True


def checked(manifest_path: Path) -> tuple[Manifest, dict[str, Module]] | None:
    manifest = load_manifest(manifest_path)
    modules = load_modules()
    findings = check_manifest(manifest, modules)
    if not findings:
        return manifest, modules
    for finding in findings:
        print(finding, file=sys.stderr)
    print(f"{manifest_path}: not deployed, fix the errors first", file=sys.stderr)
    return None


def prepare(
    files: dict[str, str],
    alias: str,
    steps: dict[str, str],
    runner: Runner,
    timeout: float,
    out: TextIO,
    err: TextIO,
    helper: bool = False,
) -> bool:
    if RULES in files:
        live = runner(["ssh", alias, steps["rules"]], None, timeout)
        try:
            if live.code != 0:
                raise ValueError(live.err.strip())
            files[RULES], retired = retire_rules(live.out, files[RULES])
        except (ValueError, yaml.YAMLError) as error:
            print(str(error), file=err)
            print(f"{alias}: live alert rules unreadable, the running stack is untouched", file=err)
            return False
        for uid in retired:
            print(
                f"{alias}: alert rule {uid} left the manifest, Grafana deletes it on start",
                file=out,
            )
    pulled = runner(["ssh", alias, steps["pull"]], files["compose.yaml"].encode("utf-8"), timeout)
    if pulled.code != 0:
        pull_failed(pulled, files, alias, helper, err)
        return False
    return True


def finish(
    files: dict[str, str],
    alias: str,
    target: str,
    steps: dict[str, str],
    runner: Runner,
    timeout: float,
    out: TextIO,
    err: TextIO,
) -> int:
    shipped = runner(["ssh", alias, steps["swap"]], tarball(files), timeout)
    if shipped.code == TIMED_OUT:
        print(shipped.err.strip(), file=err)
        print(
            f"{alias}: swap timed out, docker compose did not run, deploy again to finish",
            file=err,
        )
        return 1
    if shipped.code != 0:
        print(shipped.err.strip(), file=err)
        print(f"{alias}: files not swapped, the running stack is untouched", file=err)
        return 1
    changed, removed = parse_swap(shipped.out)
    print(f"{alias}:{target}: {len(changed)} file(s) changed, {len(removed)} removed", file=out)
    up = runner(["ssh", alias, steps["up"]], None, timeout)
    if up.code != 0:
        print(up.err.strip(), file=err)
        print(f"{alias}: docker compose up failed", file=err)
        return 1
    installed = installed_units(shipped.out)
    timers = install_timers(files, removed, installed, alias, steps, runner, timeout, out, err)
    listed = runner(["ssh", alias, steps["ps"]], None, timeout)
    if listed.code != 0:
        print(listed.err.strip(), file=err)
        return 1
    compose = yaml.safe_load(files["compose.yaml"]) or {}
    rows = status_rows(parse_ps(listed.out), services_touching(compose, changed))
    print(table(rows), file=out)
    expected = set(compose.get("services") or {})
    missing = sorted(expected - {row[0] for row in rows})
    for name in missing:
        print(f"{name}: declared but has no container", file=err)
    return 0 if timers and all(healthy(row) for row in rows) and not missing else 1


def ship(
    files: dict[str, str],
    alias: str,
    target: str,
    steps: dict[str, str],
    runner: Runner,
    timeout: float,
    out: TextIO,
    err: TextIO,
    helper: bool = False,
) -> int:
    if not prepare(files, alias, steps, runner, timeout, out, err, helper):
        return 1
    return finish(files, alias, target, steps, runner, timeout, out, err)


def deploy(
    manifest_path: Path,
    host: str,
    ssh: str | None = None,
    sudo: bool = False,
    directory: str | None = None,
    runner: Runner = ssh_runner,
    time: str | None = None,
    commit: str | None = None,
    timeout: float = TIMEOUT,
    helper: bool = False,
) -> int:
    if helper and sudo:
        print("--helper and --sudo exclude each other", file=sys.stderr)
        return 2
    loaded = checked(manifest_path)
    if loaded is None:
        return 1
    manifest, modules = loaded
    if host not in manifest.hosts:
        print(f"{manifest_path}: unknown host {host!r}", file=sys.stderr)
        return 2
    files = host_files(
        render_tree(manifest, modules, current_stamp(manifest_path, time, commit)), host
    )
    reach = connection(manifest, host, ssh, sudo, helper, directory)
    target = reach.directory
    if refuse_helper_dirs([reach]):
        return 2
    problem = misplaced(files, host, target)
    if problem is not None:
        print(problem, file=sys.stderr)
        return 2
    steps = remote_steps(target, reach.sudo, reach.helper)
    return ship(
        files, reach.alias, target, steps, runner, timeout, sys.stdout, sys.stderr, reach.helper
    )


def is_central(manifest: Manifest, host: str, modules: dict[str, Module]) -> bool:
    settings = host_settings(manifest, host, modules)
    return any(name in settings for name in CENTRAL) or any(
        (settings.get(name) or {}).get("role") == "server" for name in SERVERS
    )


def rollout(hosts: list[str], central: set[str], explicit: bool, parallel: int) -> list[list[str]]:
    leaves = [host for host in hosts if host not in central]
    canary = hosts[0] if explicit or not leaves else leaves[0]
    rest = [host for host in hosts if host != canary]
    ordered = [host for host in rest if host not in central]
    ordered += [host for host in rest if host in central]
    return [[canary], *(ordered[i : i + parallel] for i in range(0, len(ordered), parallel))]


def wave_name(index: int) -> str:
    return "canary" if index == 0 else f"wave {index}"


def plan_text(waves: list[list[str]]) -> str:
    return "\n".join(f"{wave_name(index)}: {', '.join(wave)}" for index, wave in enumerate(waves))


def summary(results: dict[str, str]) -> str:
    rows = [("HOST", "RESULT"), *results.items()]
    width = max(len(host) for host, _ in rows)
    return "\n".join(f"{host.ljust(width)}  {result}" for host, result in rows)


def fleet(
    manifest_path: Path,
    hosts: list[str] | None,
    ssh: str | None = None,
    sudo: bool = False,
    directory: str | None = None,
    runner: Runner = ssh_runner,
    time: str | None = None,
    commit: str | None = None,
    timeout: float = TIMEOUT,
    helper: bool = False,
    parallel: int = 1,
    dry_run: bool = False,
) -> int:
    if helper and sudo:
        print("--helper and --sudo exclude each other", file=sys.stderr)
        return 2
    loaded = checked(manifest_path)
    if loaded is None:
        return 1
    manifest, modules = loaded
    selected = list(dict.fromkeys(hosts if hosts is not None else manifest.hosts))
    unknown = [host for host in selected if host not in manifest.hosts]
    for host in unknown:
        print(f"{manifest_path}: unknown host {host!r}", file=sys.stderr)
    if unknown or not selected:
        return 2
    if ssh and len(selected) > 1:
        print("--ssh names the destination of one host, not of a fleet", file=sys.stderr)
        return 2
    reach = {host: connection(manifest, host, ssh, sudo, helper, directory) for host in selected}
    if refuse_helper_dirs(reach.values()):
        return 2
    central = {host for host in selected if is_central(manifest, host, modules)}
    waves = rollout(selected, central, hosts is not None, parallel)
    if dry_run:
        print(plan_text(waves))
        return 0
    tree = render_tree(manifest, modules, current_stamp(manifest_path, time, commit))
    problems = [
        problem
        for host in selected
        if (problem := misplaced(host_files(tree, host), host, reach[host].directory)) is not None
    ]
    for problem in problems:
        print(problem, file=sys.stderr)
    if problems:
        return 2
    lock = threading.Lock()

    class Lane:
        def __init__(self, host: str, label: str, live: bool) -> None:
            self.host = host
            self.live = live
            self.header = f"== {host} ({label})"
            self.timed: list[Result] = []
            self.captured, self.failures = io.StringIO(), io.StringIO()
            self.out: TextIO = sys.stdout if live else self.captured
            self.err: TextIO = sys.stderr if live else self.failures
            self.reach = reach[host]
            self.steps = remote_steps(self.reach.directory, self.reach.sudo, self.reach.helper)
            self.files = host_files(tree, host)

        def watched(self, argv: list[str], stdin: bytes | None, limit: float) -> Result:
            done = runner(argv, stdin, limit)
            if done.code == TIMED_OUT:
                self.timed.append(done)
            return done

        def pull(self) -> bool:
            if self.live:
                print(self.header, flush=True)
            alias = self.reach.alias
            args = (self.files, alias, self.steps, self.watched, timeout, self.out, self.err)
            try:
                return prepare(*args, self.reach.helper)
            except Exception as error:
                print(f"{self.host}: {error!r}", file=self.err)
                return False

        def swap(self) -> str:
            alias, target = self.reach.alias, self.reach.directory
            try:
                code = finish(
                    self.files, alias, target, self.steps, self.watched, timeout, self.out, self.err
                )
            except Exception as error:
                print(f"{self.host}: {error!r}", file=self.err)
                code = 1
            self.flush()
            if code == 0:
                return DEPLOYED
            return self.failed()

        def failed(self) -> str:
            return TIMED_OUT_RESULT if self.timed else FAILED

        def flush(self) -> None:
            if self.live:
                return
            with lock:
                print(self.header)
                sys.stdout.write(self.captured.getvalue())
                sys.stdout.flush()
                sys.stderr.write(self.failures.getvalue())
                sys.stderr.flush()

    results = dict.fromkeys((host for wave in waves for host in wave), SKIPPED)
    with ThreadPoolExecutor(max_workers=parallel) as pool:
        for index, wave in enumerate(waves):
            label = wave_name(index)
            lanes = [Lane(host, label, len(wave) == 1) for host in wave]
            pulled = list(pool.map(Lane.pull, lanes))
            if all(pulled):
                results.update(zip(wave, pool.map(Lane.swap, lanes), strict=True))
            else:
                for lane, ready in zip(lanes, pulled, strict=True):
                    if ready:
                        print(
                            f"{lane.host}: pulled, not swapped: another host of {label} "
                            "failed before its swap",
                            file=lane.err,
                        )
                    else:
                        results[lane.host] = lane.failed()
                    lane.flush()
            if any(results[host] != DEPLOYED for host in wave):
                break
    print(summary(results))
    return 0 if all(result == DEPLOYED for result in results.values()) else 1


def seconds(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        value = 0.0
    if not 0 < value < float("inf"):
        raise argparse.ArgumentTypeError(f"{text!r} is not a positive number of seconds")
    return value


def positive(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        value = 0
    if value < 1:
        raise argparse.ArgumentTypeError(f"{text!r} is not a positive whole number")
    return value


def add_parser(commands: Any) -> None:
    parser = commands.add_parser(
        "deploy",
        help="render hosts, swap their files atomically over ssh, docker compose up, canary first",
    )
    add_manifest(parser)
    select = parser.add_mutually_exclusive_group()
    select.add_argument(
        "--host",
        action="append",
        help="a host to deploy, repeat for several; the first is canary;"
        " default the only host of the manifest",
    )
    select.add_argument("--all", action="store_true", help="every host of the manifest")
    add_connection(
        parser,
        "run the remote side under sudo sh",
        f"run each step through {HELPER} on a host prepared by aye host-init",
    )
    parser.add_argument("--build-time", help="footer build time, default now or SOURCE_DATE_EPOCH")
    parser.add_argument("--commit", help="footer manifest commit, default git rev-parse")
    parser.add_argument(
        "--timeout",
        type=seconds,
        default=TIMEOUT,
        help=f"seconds each ssh call may take, default {TIMEOUT}",
    )
    parser.add_argument(
        "--parallel",
        type=positive,
        default=1,
        help="hosts deployed at once after the canary, default 1",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="print the canary and the waves, connect to nothing"
    )


def add_connection(parser: argparse.ArgumentParser, sudo: str, helper: str) -> None:
    parser.add_argument(
        "--ssh", help="ssh destination of a single host, default connect.ssh or the host name"
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--sudo", action="store_true", help=f"{sudo}; default connect.sudo")
    mode.add_argument("--helper", action="store_true", help=f"{helper}; default connect.helper")
    parser.add_argument(
        "--dir", help=f"remote directory, default connect.dir or {REMOTE_ROOT}/<host>"
    )


def chosen_hosts(args: argparse.Namespace) -> list[str] | None:
    if args.all:
        return None
    return list(args.host or [only_host(args.manifest, fleet=True)])


def run(args: argparse.Namespace) -> int:
    hosts = chosen_hosts(args)
    if hosts is not None and len(hosts) == 1 and not args.dry_run:
        return deploy(
            args.manifest,
            hosts[0],
            args.ssh,
            args.sudo,
            args.dir,
            time=args.build_time,
            commit=args.commit,
            timeout=args.timeout,
            helper=args.helper,
        )
    return fleet(
        args.manifest,
        hosts,
        args.ssh,
        args.sudo,
        args.dir,
        time=args.build_time,
        commit=args.commit,
        timeout=args.timeout,
        helper=args.helper,
        parallel=args.parallel,
        dry_run=args.dry_run,
    )
