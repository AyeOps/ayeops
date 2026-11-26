from __future__ import annotations

import argparse
import json
import re
import shlex
import shutil
import sys
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ayeops.backups import unit_files
from ayeops.defaults import add_manifest
from ayeops.deploy import (
    HELPER,
    REMOTE_ROOT,
    SUDO_RS,
    TIMED_OUT,
    Result,
    Runner,
    add_connection,
    chosen_hosts,
    connection,
    host_files,
    hostside,
    positive,
    refuse_helper_dirs,
    remote,
    seconds,
    ssh_runner,
)
from ayeops.manifest import Manifest, load_manifest
from ayeops.portal.pages import PortalDrift
from ayeops.registry import load_modules
from ayeops.render import render_tree
from ayeops.rules import check_manifest, parse_size

TIMEOUT = 60
UNREACHABLE = 255
OK = "ok"
WARN = "warn"
FAIL = "fail"
LOCAL = "local"
DISK_WARN_PCT = 15
DISK_FAIL_PCT = 5
SKEW_WARN_SECONDS = 30
MEMORY_TOLERANCE_PCT = 90
REQUIRED = re.compile(r"\$\{([A-Z][A-Z0-9_]*):\?")
VERSION = re.compile(r"[0-9]+(?:\.[0-9]+)+")
ARCHES = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}
HELPER_KEY = "this key runs"
PULL_POLICY_COMPOSE = (2, 22)
IMAGE_REF = re.compile(r"^[A-Za-z0-9._/:@-]+$")
NO_TAG = ("no such manifest", "manifest unknown")
DETAIL_LIMIT = 160
TERMINATED = 143
HOST_INIT_RULE = "/etc/sudoers.d/ayeops-"

Row = tuple[str, str, str, str]
Clock = Callable[[], float]


@dataclass
class Facts:
    values: dict[str, str] = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)
    empty: list[str] = field(default_factory=list)
    local: dict[str, str] = field(default_factory=dict)
    manifests: dict[str, tuple[int, str]] = field(default_factory=dict)
    finished: bool = False

    def get(self, key: str) -> str:
        return self.values.get(key, "")


def parse_facts(out: str) -> Facts:
    facts = Facts()
    for line in out.splitlines():
        key, _, value = line.partition(" ")
        if key == "end":
            facts.finished = True
        elif key == "missing" and value:
            facts.missing.append(value)
        elif key == "empty" and value:
            facts.empty.append(value)
        elif key == "image":
            ref, _, platform = value.partition(" ")
            facts.local.setdefault(ref, platform.strip())
        elif key == "manifest":
            ref, _, rest = value.partition(" ")
            code, _, text = rest.partition(" ")
            if code.isdigit():
                facts.manifests.setdefault(ref, (int(code), text.strip()))
        elif key and key not in facts.values:
            facts.values[key] = value.strip()
    return facts


def required_env(compose: str) -> list[str]:
    return sorted(set(REQUIRED.findall(compose)))


def host_images(compose: str) -> list[str]:
    data = yaml.safe_load(compose) if compose else None
    services = data.get("services") if isinstance(data, dict) else None
    if not isinstance(services, dict):
        return []
    found = {
        str(spec["image"])
        for spec in services.values()
        if isinstance(spec, dict) and spec.get("image") and "build" not in spec
    }
    return sorted(image for image in found if IMAGE_REF.match(image))


def doctor_script(
    directory: str,
    mode: str,
    names: list[str],
    timers: bool = False,
    images: list[str] | None = None,
) -> str:
    words = [
        directory,
        mode,
        HELPER,
        "timers" if timers else "none",
        " ".join(images or []),
        *names,
    ]
    return f"set -- {' '.join(shlex.quote(word) for word in words)}\n{hostside('doctor.sh')}"


def doctor_command(
    directory: str,
    sudo: bool,
    helper: bool,
    names: list[str],
    timers: bool = False,
    images: list[str] | None = None,
) -> str:
    mode = "helper" if helper else "sudo" if sudo else "plain"
    return remote(doctor_script(directory, mode, names, timers, images), sudo)


def version(text: str) -> str:
    found = VERSION.search(text)
    return found.group(0) if found else ""


def disk_row(check: str, line: str, path: str) -> tuple[str, str, str]:
    fields = line.split()
    try:
        size, avail = int(fields[1]), int(fields[3])
    except (IndexError, ValueError):
        return check, WARN, f"df could not read {path}"
    if size <= 0:
        return check, WARN, f"df reports no size for {path}"
    free = avail * 100 / size
    detail = f"{free:.0f} % free ({avail // 1024**2} GiB) on {path}"
    if free < DISK_FAIL_PCT:
        return check, FAIL, detail
    if free < DISK_WARN_PCT:
        return check, WARN, detail
    return check, OK, detail


def docker_rows(facts: Facts, mode: str) -> list[tuple[str, str, str]]:
    if "docker" not in facts.values:
        return [
            ("docker", FAIL, "docker is not on PATH"),
            ("compose", FAIL, "needs docker"),
        ]
    rows = [("docker", OK, version(facts.get("docker")) or facts.get("docker") or "present")]
    compose = version(facts.get("compose"))
    if compose and tuple(int(part) for part in compose.split(".")[:2]) < PULL_POLICY_COMPOSE:
        rows.append(
            (
                "compose",
                FAIL,
                f"{compose}; aye deploy pulls with --policy missing, which needs compose 2.22",
            )
        )
    elif compose:
        rows.append(("compose", OK, compose))
    else:
        rows.append(("compose", FAIL, "the docker compose plugin is missing"))
    if mode == "helper":
        return rows
    who = facts.get("user") or "the ssh user"
    daemon = facts.get("daemon")
    if daemon == "ok":
        rows.append(("daemon", OK, f"reachable as {who}"))
    elif daemon == "timeout":
        rows.append(("daemon", FAIL, f"docker info did not answer {who} within 10 s"))
    else:
        rows.append(
            (
                "daemon",
                FAIL,
                f"{who} cannot reach the docker daemon, add --sudo or --helper or the docker group",
            )
        )
    return rows


def arch_row(facts: Facts, declared: str) -> tuple[str, str, str]:
    machine = facts.get("uname")
    found = ARCHES.get(machine)
    if found is None:
        return "arch", FAIL, f"uname -m {machine or 'empty'} is neither amd64 nor arm64"
    if found != declared:
        return "arch", FAIL, f"host is {found} ({machine}), the manifest says {declared}"
    return "arch", OK, f"{found} ({machine})"


def dir_rows(facts: Facts, directory: str, mode: str) -> list[tuple[str, str, str]]:
    state, _, nearest = facts.get("dir").partition(" ")
    writable = facts.get("writable") == "yes"
    if mode == "helper":
        rows: list[tuple[str, str, str]] = []
        if state != "present":
            rows.append(("dir", FAIL, f"{directory} is not a directory, run aye host-init"))
        else:
            rows.append(("dir", OK, f"{directory} exists"))
        if facts.get("helper") != "present":
            rows.append(("helper", FAIL, f"{HELPER} is missing, run aye host-init"))
        elif facts.get("sudo") != "yes":
            rows.append(
                ("helper", FAIL, f"sudo -n does not allow {HELPER} {directory}, run aye host-init")
            )
        else:
            rows.append(("helper", OK, f"sudo -n may run {HELPER} {directory}"))
        return rows
    who = facts.get("user") or "the ssh user"
    if state == "other":
        return [("dir", FAIL, f"{directory} exists and is not a directory")]
    if state == "absent":
        if writable:
            return [("dir", OK, f"{directory} is absent, deploy creates it in {nearest}")]
        return [("dir", FAIL, f"{directory} is absent and {who} cannot create it in {nearest}")]
    if not writable:
        return [("dir", FAIL, f"{who} cannot write {directory}")]
    return [("dir", OK, f"{directory} is writable by {who}")]


def sudo_row(facts: Facts, mode: str) -> tuple[str, str, str] | None:
    if mode == "plain":
        return None
    found = facts.get("sudoversion")
    if not found:
        return "sudo", WARN, "sudo -V printed no version"
    if not found.startswith(SUDO_RS):
        return "sudo", OK, found
    error = facts.get("sudoerror")
    if not error:
        return "sudo", OK, f"{found}, every sudoers line parses"
    if error.startswith(HOST_INIT_RULE):
        status = FAIL if mode == "helper" and facts.get("sudo") != "yes" else WARN
        return (
            "sudo",
            status,
            f"{found} rejects {short(error)}; run aye host-init again, its rule suits sudo-rs",
        )
    return "sudo", WARN, f"{found} skips {short(error)}; visudo -c lists every such line"


def clock_row(facts: Facts, before: float, after: float) -> tuple[str, str, str]:
    try:
        stamp = int(facts.get("clock"))
    except ValueError:
        return "clock", WARN, "date +%s gave no time"
    skew = max(before - stamp, stamp - after, 0.0)
    detail = f"{skew:.0f} s off the local clock"
    return "clock", WARN if skew > SKEW_WARN_SECONDS else OK, detail


def memory_row(facts: Facts, declared: str | None) -> tuple[str, str, str] | None:
    if not declared:
        return None
    try:
        total = int(facts.get("memkb")) * 1024
    except ValueError:
        return "memory", WARN, "/proc/meminfo has no MemTotal"
    wanted = parse_size(declared)
    detail = f"{total // 1024**2} MiB, the manifest says {declared}"
    if total * 100 < wanted * MEMORY_TOLERANCE_PCT:
        return "memory", WARN, detail
    return "memory", OK, detail


def env_row(facts: Facts, directory: str, names: list[str]) -> tuple[str, str, str]:
    state = facts.get("env")
    if not names or state == "none":
        return "env", OK, "no required variable"
    if state == "absent":
        return "env", FAIL, f"{directory}/.env is missing, {len(names)} variable(s) required"
    if state != "read":
        who = facts.get("user") or "the ssh user"
        return "env", WARN, f"{who} cannot read {directory}/.env, not checked"
    problems = []
    if facts.missing:
        problems.append(f"missing {', '.join(facts.missing)}")
    if facts.empty:
        problems.append(f"empty {', '.join(facts.empty)}")
    if problems:
        return "env", FAIL, "; ".join(problems)
    return "env", OK, f"{len(names)} required variable(s) set"


def timers_row(facts: Facts, target: Target, mode: str) -> tuple[str, str, str] | None:
    if not target.timers:
        return None
    home = f"{REMOTE_ROOT}/{target.host}"
    if target.directory != home:
        return "timers", FAIL, f"the backup timers run the jobs from {home}, not {target.directory}"
    if facts.get("systemd") != "yes":
        return "timers", FAIL, "systemctl is missing; the backup jobs with a command need systemd"
    if mode == "helper" and facts.get("timers") != "yes":
        return (
            "timers",
            FAIL,
            f"sudo -n does not allow {HELPER} {target.directory} timers, run aye host-init again",
        )
    return "timers", OK, f"{target.timers} backup timer(s), systemctl present"


def described(entry: Any) -> Any:
    descriptor = entry.get("Descriptor") if isinstance(entry, dict) else None
    return descriptor.get("platform") if isinstance(descriptor, dict) else None


def platforms(text: str) -> set[str] | None:
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if isinstance(data, dict) and isinstance(data.get("manifests"), list):
        listed = [entry.get("platform") for entry in data["manifests"] if isinstance(entry, dict)]
    elif isinstance(data, list):
        listed = [described(entry) for entry in data]
    else:
        listed = [described(data)]
    found = {
        f"{entry['os']}/{entry['architecture']}"
        for entry in listed
        if isinstance(entry, dict) and entry.get("os") and entry.get("architecture")
    }
    found.discard("unknown/unknown")
    return found or None


def short(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= DETAIL_LIMIT else f"{text[: DETAIL_LIMIT - 3]}..."


def image_row(facts: Facts, image: str, arch: str) -> tuple[str, str, str]:
    wanted = f"linux/{arch}"
    if image in facts.local:
        found = facts.local[image]
        if found == wanted:
            return "image", OK, f"{image}: on the host, {found}"
        return "image", FAIL, f"{image}: on the host for {found or 'no platform'}, not {wanted}"
    if image not in facts.manifests:
        return "image", WARN, f"{image}: not checked"
    code, text = facts.manifests[image]
    if code in (TIMED_OUT, TERMINATED):
        return "image", WARN, f"{image}: the registry did not answer within 10 s"
    if code != 0:
        if any(word in text.lower() for word in NO_TAG):
            return "image", FAIL, f"{image}: the registry has no such image or tag"
        who = facts.get("user") or "the ssh user"
        return "image", WARN, f"{image}: unreadable as {who}: {short(text) or f'exit {code}'}"
    listed = platforms(text)
    if listed is None:
        return "image", WARN, f"{image}: the registry answer names no platform"
    if wanted in listed:
        return "image", OK, f"{image}: {wanted} in the registry"
    return "image", FAIL, f"{image}: no {wanted} manifest, only {', '.join(sorted(listed))}"


def image_rows(facts: Facts, images: list[str], declared: str) -> list[tuple[str, str, str]]:
    if "docker" not in facts.values:
        return []
    arch = ARCHES.get(facts.get("uname")) or declared
    return [image_row(facts, image, arch) for image in images]


def unreachable(done: Result, timeout: float) -> str:
    if done.code == TIMED_OUT:
        return f"ssh timed out after {timeout:g}s"
    lines = [line for line in done.err.splitlines() if line.strip()]
    if any(HELPER_KEY in line for line in lines):
        return "the key runs only the aye-host helper, use a login with a shell via --ssh"
    return lines[-1].strip() if lines else f"ssh exited {done.code}"


@dataclass(frozen=True)
class Target:
    host: str
    alias: str
    directory: str
    arch: str
    memory: str | None
    names: list[str]
    timers: int = 0
    sudo: bool = False
    helper: bool = False
    images: list[str] = field(default_factory=list)


def probe(
    target: Target,
    sudo: bool,
    helper: bool,
    runner: Runner,
    timeout: float,
    clock: Clock,
) -> list[Row]:
    mode = "helper" if helper else "sudo" if sudo else "plain"
    command = doctor_command(
        target.directory, sudo, helper, target.names, bool(target.timers), target.images
    )
    before = clock()
    done = runner(["ssh", target.alias, command], None, timeout)
    after = clock()
    facts = parse_facts(done.out)
    if done.code in (UNREACHABLE, TIMED_OUT) or not facts.finished:
        return [(target.host, "ssh", FAIL, unreachable(done, timeout))]
    checks: list[tuple[str, str, str]] = [
        ("ssh", OK, f"{target.alias} runs as {facts.get('user') or 'unknown'}")
    ]
    checks += docker_rows(facts, mode)
    checks.append(arch_row(facts, target.arch))
    checks += dir_rows(facts, target.directory, mode)
    sudo_check = sudo_row(facts, mode)
    if sudo_check is not None:
        checks.append(sudo_check)
    checks.append(disk_row("disk dir", facts.get("dfdir"), target.directory))
    root, _, assumed = facts.get("dockerroot").partition(" ")
    if "dfdocker" in facts.values:
        label = f"{root} (assumed)" if assumed else root
        checks.append(disk_row("disk docker", facts.get("dfdocker"), label))
    checks.append(clock_row(facts, before, after))
    memory = memory_row(facts, target.memory)
    if memory is not None:
        checks.append(memory)
    checks.append(env_row(facts, target.directory, target.names))
    timers = timers_row(facts, target, mode)
    if timers is not None:
        checks.append(timers)
    checks += image_rows(facts, target.images, target.arch)
    return [(target.host, check, status, detail) for check, status, detail in checks]


def grid(rows: list[Row]) -> str:
    lines = [("HOST", "CHECK", "RESULT", "DETAIL"), *rows]
    widths = [max(len(line[column]) for line in lines) for column in range(4)]
    return "\n".join(
        "  ".join(line[column].ljust(widths[column]) for column in range(4)).rstrip()
        for line in lines
    )


def finish(rows: list[Row]) -> int:
    print(grid(rows))
    return 1 if any(row[2] == FAIL for row in rows) else 0


def targets(
    manifest: Manifest,
    tree: dict[str, str],
    hosts: list[str],
    ssh: str | None,
    directory: str | None,
    sudo: bool = False,
    helper: bool = False,
) -> list[Target]:
    found = []
    for host in hosts:
        spec = manifest.hosts[host]
        files = host_files(tree, host)
        compose = files.get("compose.yaml", "")
        memory = spec.get("memory")
        reach = connection(manifest, host, ssh, sudo, helper, directory)
        found.append(
            Target(
                host,
                reach.alias,
                reach.directory,
                str(spec.get("arch") or ""),
                str(memory) if memory else None,
                required_env(compose),
                len([unit for unit in unit_files(files) if unit.endswith(".timer")]),
                reach.sudo,
                reach.helper,
                host_images(compose),
            )
        )
    return found


def doctor(
    manifest_path: Path,
    hosts: list[str] | None,
    ssh: str | None = None,
    sudo: bool = False,
    directory: str | None = None,
    runner: Runner = ssh_runner,
    timeout: float = TIMEOUT,
    helper: bool = False,
    parallel: int = 1,
    clock: Clock = time.time,
) -> int:
    if helper and sudo:
        print("--helper and --sudo exclude each other", file=sys.stderr)
        return 2
    manifest = load_manifest(manifest_path)
    selected = list(dict.fromkeys(hosts if hosts is not None else manifest.hosts))
    unknown = [host for host in selected if host not in manifest.hosts]
    for host in unknown:
        print(f"{manifest_path}: unknown host {host!r}", file=sys.stderr)
    if unknown or not selected:
        return 2
    if ssh and len(selected) > 1:
        print("--ssh names the destination of one host, not of a fleet", file=sys.stderr)
        return 2
    reach = [connection(manifest, host, ssh, sudo, helper, directory) for host in selected]
    if refuse_helper_dirs(reach):
        return 2
    findings = check_manifest(manifest, load_modules())
    for finding in findings:
        print(finding, file=sys.stderr)
    rows: list[Row] = []
    if findings:
        rows.append((LOCAL, "manifest", FAIL, f"{len(findings)} error(s) of aye check, see above"))
    else:
        rows.append((LOCAL, "manifest", OK, "aye check passes"))
    if shutil.which("ssh"):
        rows.append((LOCAL, "ssh", OK, "ssh is on PATH"))
    else:
        rows.append((LOCAL, "ssh", FAIL, "ssh is not on PATH"))
    if any(row[2] == FAIL for row in rows):
        return finish(rows)
    try:
        tree = render_tree(manifest, load_modules())
    except PortalDrift as error:
        rows.append((LOCAL, "render", FAIL, "; ".join(error.problems)))
        return finish(rows)
    planned = targets(manifest, tree, selected, ssh, directory, sudo, helper)

    def one(target: Target) -> list[Row]:
        try:
            return probe(target, target.sudo, target.helper, runner, timeout, clock)
        except Exception as error:
            return [(target.host, "ssh", FAIL, repr(error))]

    with ThreadPoolExecutor(max_workers=parallel) as pool:
        for found in pool.map(one, planned):
            rows.extend(found)
    return finish(rows)


def add_parser(commands: Any) -> None:
    parser = commands.add_parser(
        "doctor",
        help="read-only preflight over ssh: why a deploy of these hosts would fail",
    )
    add_manifest(parser)
    select = parser.add_mutually_exclusive_group()
    select.add_argument(
        "--host",
        action="append",
        help="a host to check, repeat for several; default the only host of the manifest",
    )
    select.add_argument("--all", action="store_true", help="every host of the manifest")
    add_connection(
        parser, "check as aye deploy --sudo would run", f"check {HELPER} as deploy would use"
    )
    parser.add_argument(
        "--timeout",
        type=seconds,
        default=TIMEOUT,
        help=f"seconds the ssh call of each host may take, default {TIMEOUT}",
    )
    parser.add_argument(
        "--parallel", type=positive, default=1, help="hosts checked at once, default 1"
    )


def run(args: argparse.Namespace) -> int:
    return doctor(
        args.manifest,
        chosen_hosts(args),
        args.ssh,
        args.sudo,
        args.dir,
        timeout=args.timeout,
        helper=args.helper,
        parallel=args.parallel,
    )
