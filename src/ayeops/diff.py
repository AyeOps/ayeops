from __future__ import annotations

import argparse
import base64
import difflib
import hashlib
import re
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TextIO

import yaml

from ayeops.backups import unit_files
from ayeops.defaults import add_manifest
from ayeops.deploy import (
    HELPER,
    TIMED_OUT,
    TIMEOUT,
    Runner,
    add_connection,
    chosen_hosts,
    connection,
    host_files,
    refuse_helper_dirs,
    remote_steps,
    seconds,
    ssh_runner,
)
from ayeops.manifest import load_manifest
from ayeops.mounts import services_touching
from ayeops.registry import load_modules
from ayeops.render import ENV_REFERENCE, render_tree
from ayeops.retire import RULES, retire_rules
from ayeops.rules import check_manifest
from ayeops.stamp import current_stamp

LINES = 200
COMPOSE = "compose.yaml"
UNREACHABLE = 255
OPAQUE = {
    "link": "a symbolic link on the host, not read",
    "foreign": "not shipped by the previous deploy, not read",
}
SAME, CHANGED, ERROR = 0, 1, 2
ENV_EXAMPLE = ".env.example"
ENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
ENV_REQUIRED = re.compile(r"\$\{([A-Z][A-Z0-9_]*):?\?")
ENV_FILES = ("present", "absent", "link", "unreadable")
ENV_OWN = "COMPOSE_"
ENV_NOTES = {
    None: ".env not compared, the host's diff.sh predates it; "
    "run the aye host-init script of this host again",
    "link": ".env is a symbolic link on the host, not read",
    "unreadable": ".env is not a readable file on the host, not compared",
}
NO_BASELINE = (
    "no .env hashes from a previous deploy on the host, changed values show after the next deploy"
)


@dataclass
class Deployed:
    same: set[str] = field(default_factory=set)
    added: set[str] = field(default_factory=set)
    old: dict[str, bytes] = field(default_factory=dict)
    opaque: dict[str, str] = field(default_factory=dict)
    removed: list[str] = field(default_factory=list)
    env_file: str | None = None
    env_keys: set[str] = field(default_factory=set)
    env_changed: set[str] = field(default_factory=set)
    env_base: bool = True


@dataclass(frozen=True)
class EnvDrift:
    rows: list[tuple[str, str, str]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    services: dict[str, list[str]] = field(default_factory=dict)

    def count(self, change: str) -> int:
        return len([row for row in self.rows if row[1].startswith(change)])

    @property
    def drifted(self) -> bool:
        return any(row[1] != "unused" for row in self.rows)

    def counts(self) -> str:
        return (
            f".env {self.count('changed')} changed, {self.count('missing')} missing,"
            f" {self.count('unused')} unused"
        )


@dataclass(frozen=True)
class Plan:
    added: list[str]
    changed: list[str]
    removed: list[str]
    notes: dict[str, str]
    diffs: dict[str, list[str]]
    services: list[tuple[str, str]]
    retired: list[str]
    env: EnvDrift = field(default_factory=EnvDrift)

    @property
    def empty(self) -> bool:
        return not (self.added or self.changed or self.removed or self.env.drifted)

    def counts(self) -> str:
        files = f"{len(self.changed)} changed, {len(self.added)} added, {len(self.removed)} removed"
        return f"{files}; {self.env.counts()}" if self.env.rows else files

    def line(self) -> str:
        if self.empty:
            return "no change"
        return f"{self.counts()}, {len(self.services)} service(s)"


def request(files: dict[str, str]) -> bytes:
    return "".join(
        f"{hashlib.sha256(files[path].encode()).hexdigest()} {path}\n" for path in sorted(files)
    ).encode()


def content(lines: Iterator[str], path: str) -> bytes:
    chunks: list[str] = []
    for chunk in lines:
        if chunk == ".":
            return base64.b64decode("".join(chunks), validate=True)
        chunks.append(chunk)
    raise ValueError(f"the content of {path} is cut off")


def env_answer(deployed: Deployed, word: str, value: str) -> bool:
    if word == "env-file" and value in ENV_FILES:
        deployed.env_file = value
    elif word == "env-base" and value == "absent":
        deployed.env_base = False
    elif word in ("env-key", "env-changed") and ENV_KEY.match(value):
        (deployed.env_keys if word == "env-key" else deployed.env_changed).add(value)
    else:
        return False
    return True


def parse_deployed(out: str) -> Deployed:
    deployed = Deployed()
    lines = iter(out.splitlines())
    for line in lines:
        word, _, path = line.partition(" ")
        if not path:
            raise ValueError(f"unexpected answer {line!r}")
        if word.startswith("env-"):
            if not env_answer(deployed, word, path):
                raise ValueError(f"unexpected answer {line!r}")
        elif word == "same":
            deployed.same.add(path)
        elif word == "added":
            deployed.added.add(path)
        elif word == "removed":
            deployed.removed.append(path)
        elif word == "old":
            deployed.old[path] = content(lines, path)
        elif word in OPAQUE:
            deployed.opaque[path] = OPAQUE[word]
        else:
            raise ValueError(f"unexpected answer {line!r}")
    return deployed


def kept_lines(text: str) -> list[str]:
    parts = text.split("\n")
    return [f"{part}\n" for part in parts[:-1]] + ([parts[-1]] if parts[-1] else [])


def unified(old: str, new: str, before: str, after: str, limit: int = LINES) -> list[str]:
    lines: list[str] = []
    for line in difflib.unified_diff(kept_lines(old), kept_lines(new), before, after):
        if line.endswith("\n"):
            lines.append(line[:-1])
        else:
            lines += [line, "\\ No newline at end of file"]
    if len(lines) > limit:
        return [*lines[:limit], f"... {len(lines) - limit} more line(s) of this diff omitted"]
    return lines


def compose_services(text: str) -> dict[str, Any]:
    data = yaml.safe_load(text) or {}
    services = data.get("services") if isinstance(data, dict) else None
    return dict(services) if isinstance(services, dict) else {}


def deployed_services(deployed: Deployed, new: dict[str, Any]) -> dict[str, Any] | None:
    if COMPOSE in deployed.same:
        return new
    if COMPOSE in deployed.added:
        return {}
    if COMPOSE not in deployed.old:
        return None
    try:
        return compose_services(deployed.old[COMPOSE].decode("utf-8"))
    except (UnicodeDecodeError, yaml.YAMLError):
        return None


def strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from strings(key)
            yield from strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from strings(item)


def interpolated(services: dict[str, Any], pattern: re.Pattern[str]) -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for name, definition in services.items():
        for text in strings(definition):
            for key in pattern.findall(text):
                found.setdefault(key, set()).add(str(name))
    return found


def env_names(text: str) -> set[str]:
    names = set()
    for line in text.splitlines():
        name = line.partition("=")[0].strip()
        if ENV_KEY.match(name):
            names.add(name)
    return names


def env_drift(files: dict[str, str], deployed: Deployed) -> EnvDrift:
    if deployed.env_file not in ("present", "absent"):
        return EnvDrift(notes=[ENV_NOTES[deployed.env_file]])
    services = compose_services(files.get(COMPOSE, ""))
    users = interpolated(services, ENV_REFERENCE)
    required = interpolated(services, ENV_REQUIRED)
    wanted = env_names(files.get(ENV_EXAMPLE, ""))
    referenced = wanted | {key for text in files.values() for key in ENV_REFERENCE.findall(text)}
    rows = []
    for key in sorted(wanted | deployed.env_keys):
        if key not in deployed.env_keys:
            change = "missing, up fails" if key in required else "missing"
        elif key not in referenced and not key.startswith(ENV_OWN):
            rows.append((key, "unused", ""))
            continue
        elif key in deployed.env_changed:
            change = "changed"
        else:
            continue
        rows.append((key, change, ", ".join(sorted(users.get(key, ())))))
    recreated: dict[str, list[str]] = {}
    for key in sorted(deployed.env_changed):
        for name in users.get(key, ()):
            recreated.setdefault(name, []).append(key)
    notes = [] if deployed.env_base else [NO_BASELINE]
    return EnvDrift(rows, notes, recreated)


def service_changes(
    files: dict[str, str],
    deployed: Deployed,
    paths: list[str],
    env: dict[str, list[str]] | None = None,
) -> list[tuple[str, str]]:
    new = compose_services(files.get(COMPOSE, ""))
    old = deployed_services(deployed, new)
    touched = services_touching({"services": new}, paths)
    env = env or {}
    rows = []
    for name in sorted(set(new) | set(old or {})):
        reasons = []
        if old is not None and name not in new:
            reasons.append("removed")
        elif old is not None and name not in old:
            reasons.append("created")
        elif (old is not None and old[name] != new[name]) or name in env:
            reasons.append("recreated")
        if name in touched:
            reasons.append(f"config {len(touched[name])} file(s)")
        if name in env:
            reasons.append(f"env {len(env[name])} key(s)")
        if reasons:
            rows.append((name, ", ".join(reasons)))
    return rows


def live_rules(files: dict[str, str], deployed: Deployed) -> list[str]:
    if RULES not in files:
        return []
    try:
        live = deployed.old.get(RULES, b"").decode("utf-8")
        files[RULES], retired = retire_rules(live, files[RULES])
    except (ValueError, yaml.YAMLError) as error:
        reason = str(error).splitlines()[0] if str(error) else type(error).__name__
        raise ValueError(
            f"live alert rules unreadable ({reason}), aye deploy would stop before the swap"
        ) from error
    return retired


def compare(rendered: dict[str, str], deployed: Deployed, label: str) -> Plan:
    files = dict(rendered)
    retired = live_rules(files, deployed)
    added: list[str] = []
    changed: list[str] = []
    notes: dict[str, str] = {}
    diffs: dict[str, list[str]] = {}
    for path in sorted(files):
        if path in deployed.same:
            continue
        if path in deployed.added:
            added.append(path)
        elif path in deployed.opaque:
            changed.append(path)
            notes[path] = deployed.opaque[path]
        elif path not in deployed.old:
            raise ValueError(f"the host gave no answer for {path}")
        elif deployed.old[path] != files[path].encode():
            changed.append(path)
            try:
                old = deployed.old[path].decode("utf-8")
            except UnicodeDecodeError:
                notes[path] = "not UTF-8 text on the host, no diff"
                continue
            diffs[path] = unified(old, files[path], f"{label}/{path}", f"render/{path}")
    env = env_drift(files, deployed)
    services = service_changes(files, deployed, added + changed, env.services)
    removed = sorted(deployed.removed)
    return Plan(added, changed, removed, notes, diffs, services, retired, env)


def unit_changes(plan: Plan) -> list[tuple[str, str]]:
    rows = [(path, "installed") for path in unit_files(plan.added)]
    rows += [(path, "changed") for path in unit_files(plan.changed)]
    rows += [(path, "removed") for path in unit_files(plan.removed)]
    return sorted((path.rsplit("/", 1)[-1], change) for path, change in rows)


def columns(rows: list[tuple[str, ...]]) -> str:
    widths = [max(len(row[column]) for row in rows) for column in range(len(rows[0]))]
    return "\n".join(
        "  ".join(cell.ljust(width) for cell, width in zip(row, widths, strict=True)).rstrip()
        for row in rows
    )


def report_env(plan: Plan, alias: str, out: TextIO) -> None:
    if plan.env.rows:
        print(columns([("ENV KEY", "CHANGE", "SERVICES"), *plan.env.rows]), file=out)
    for note in plan.env.notes:
        print(f"{alias}: {note}", file=out)


def report(plan: Plan, label: str, alias: str, out: TextIO) -> None:
    if plan.empty:
        print(f"{label}: no change", file=out)
        report_env(plan, alias, out)
        return
    print(f"{label}: {plan.counts()}", file=out)
    for path in plan.added:
        print(f"added    {path}", file=out)
    for path in plan.changed:
        note = f"  ({plan.notes[path]})" if path in plan.notes else ""
        print(f"changed  {path}{note}", file=out)
    for path in plan.removed:
        print(f"removed  {path}", file=out)
    for uid in plan.retired:
        print(f"{alias}: alert rule {uid} left the manifest, the deploy deletes it", file=out)
    for path in plan.changed:
        for line in plan.diffs.get(path, []):
            print(line, file=out)
    if plan.services:
        print(columns([("SERVICE", "CHANGE"), *plan.services]), file=out)
    units = unit_changes(plan)
    if units:
        print(columns([("BACKUP UNIT", "CHANGE"), *units]), file=out)
    report_env(plan, alias, out)


def one(
    files: dict[str, str],
    alias: str,
    target: str,
    step: str,
    helper: bool,
    runner: Runner,
    timeout: float,
) -> Plan | None:
    try:
        done = runner(["ssh", alias, step], request(files), timeout)
    except OSError as error:
        print(f"{alias}: {error}", file=sys.stderr)
        return None
    if done.code != 0:
        if done.err.strip():
            print(done.err.strip(), file=sys.stderr)
        print(f"{alias}: the deployed files could not be read", file=sys.stderr)
        if helper and done.code not in (TIMED_OUT, UNREACHABLE):
            print(
                f"{alias}: --helper needs the diff step of {HELPER}; "
                "run the aye host-init script of this host again if it predates aye diff",
                file=sys.stderr,
            )
        return None
    try:
        plan = compare(files, parse_deployed(done.out), f"{alias}:{target}")
    except ValueError as error:
        print(f"{alias}: {error}", file=sys.stderr)
        return None
    report(plan, f"{alias}:{target}", alias, sys.stdout)
    return plan


def diff(
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
) -> int:
    if helper and sudo:
        print("--helper and --sudo exclude each other", file=sys.stderr)
        return ERROR
    manifest = load_manifest(manifest_path)
    modules = load_modules()
    findings = check_manifest(manifest, modules)
    for finding in findings:
        print(finding, file=sys.stderr)
    if findings:
        print(f"{manifest_path}: no diff, fix the errors first", file=sys.stderr)
        return ERROR
    selected = list(dict.fromkeys(hosts if hosts is not None else manifest.hosts))
    unknown = [host for host in selected if host not in manifest.hosts]
    for host in unknown:
        print(f"{manifest_path}: unknown host {host!r}", file=sys.stderr)
    if unknown or not selected:
        return ERROR
    if ssh and len(selected) > 1:
        print("--ssh names the destination of one host, not of a fleet", file=sys.stderr)
        return ERROR
    reach = {host: connection(manifest, host, ssh, sudo, helper, directory) for host in selected}
    if refuse_helper_dirs(reach.values()):
        return ERROR
    tree = render_tree(manifest, modules, current_stamp(manifest_path, time, commit))
    fleet = hosts is None or len(selected) > 1
    results: dict[str, str] = {}
    code = SAME
    for host in selected:
        if fleet:
            print(f"== {host}", flush=True)
        found = reach[host]
        step = remote_steps(found.directory, found.sudo, found.helper)["diff"]
        files = host_files(tree, host)
        plan = one(files, found.alias, found.directory, step, found.helper, runner, timeout)
        sys.stdout.flush()
        if plan is None:
            results[host] = "error"
            code = ERROR
        else:
            results[host] = plan.line()
            if not plan.empty and code == SAME:
                code = CHANGED
    if fleet:
        print(columns([("HOST", "DIFF"), *results.items()]))
    return code


def add_parser(commands: Any) -> None:
    parser = commands.add_parser(
        "diff",
        help="show what aye deploy would change on hosts: files, their diffs, services recreated",
    )
    add_manifest(parser)
    select = parser.add_mutually_exclusive_group()
    select.add_argument(
        "--host",
        action="append",
        help="a host to compare, repeat for several; default the only host of the manifest",
    )
    select.add_argument("--all", action="store_true", help="every host of the manifest")
    add_connection(
        parser,
        "read the host's files under sudo sh",
        f"read them through {HELPER} on a host prepared by aye host-init",
    )
    parser.add_argument("--build-time", help="footer build time, default now or SOURCE_DATE_EPOCH")
    parser.add_argument("--commit", help="footer manifest commit, default git rev-parse")
    parser.add_argument(
        "--timeout",
        type=seconds,
        default=TIMEOUT,
        help=f"seconds the ssh call of each host may take, default {TIMEOUT}",
    )


def run(args: argparse.Namespace) -> int:
    return diff(
        args.manifest,
        chosen_hosts(args),
        args.ssh,
        args.sudo,
        args.dir,
        time=args.build_time,
        commit=args.commit,
        timeout=args.timeout,
        helper=args.helper,
    )
