from __future__ import annotations

import argparse
import difflib
import re
import sys
from pathlib import Path
from typing import Any

from ayeops.defaults import add_manifest
from ayeops.local_modules import with_local_modules
from ayeops.manifest import Manifest, load_manifest
from ayeops.mcp_catalog import rendered_module_services
from ayeops.registry import Module, load_modules
from ayeops.render import dump_json, dump_yaml
from ayeops.rules import check_manifest

FORMATS = ("ansible-yaml", "ansible-json")
GENERATED_BY = "aye inventory"
NOT_A_GROUP_CHARACTER = re.compile(r"[^a-z0-9_]")


def group_name(kind: str, value: str) -> str:
    return f"{kind}_{NOT_A_GROUP_CHARACTER.sub('_', value.lower())}"


def host_vars(manifest: Manifest, host: str) -> dict[str, str]:
    spec = manifest.hosts[host]
    found = {
        "ansible_host": spec.get("address"),
        "ayeops_arch": spec.get("arch"),
        "ayeops_group": manifest.host_group(host),
        "ayeops_project": manifest.project,
        "ayeops_zone": spec.get("zone"),
    }
    return {key: str(value) for key, value in found.items() if value}


def services_on(manifest: Manifest, host: str, modules: dict[str, Module]) -> set[str]:
    names = {name for name, _ in rendered_module_services(manifest, host, modules)}
    names.update(
        name for name, entry in manifest.active_services.items() if entry.get("host") == host
    )
    return names


def groups_of(manifest: Manifest, host: str, modules: dict[str, Module]) -> set[str]:
    spec = manifest.hosts[host]
    pairs = [
        ("zone", spec.get("zone")),
        ("group", manifest.host_group(host)),
        ("profile", spec.get("profile")),
    ]
    pairs += [("module", name) for name in manifest.host_modules(host)]
    pairs += [("service", name) for name in services_on(manifest, host, modules)]
    return {group_name(kind, str(value)) for kind, value in pairs if value}


def inventory(manifest: Manifest, modules: dict[str, Module]) -> dict[str, Any]:
    hosts = sorted(manifest.hosts)
    children: dict[str, dict[str, dict[str, Any]]] = {}
    for host in hosts:
        for group in groups_of(manifest, host, modules):
            children.setdefault(group, {})[host] = {}
    return {
        "all": {
            "vars": {"ayeops_generated_by": GENERATED_BY},
            "hosts": {host: host_vars(manifest, host) for host in hosts},
            "children": {group: {"hosts": members} for group, members in sorted(children.items())},
        }
    }


def inventory_text(manifest: Manifest, modules: dict[str, Module], form: str) -> str:
    data = inventory(manifest, modules)
    return dump_json(data) if form == "ansible-json" else dump_yaml(data)


def check_file(manifest_path: Path, out: Path, text: str) -> int:
    try:
        current = out.read_text(encoding="utf-8")
    except FileNotFoundError:
        print(f"{manifest_path}: {out} does not exist", file=sys.stderr)
        return 1
    if current == text:
        print(f"{manifest_path}: {out} matches the inventory")
        return 0
    lines = difflib.unified_diff(
        current.splitlines(keepends=True),
        text.splitlines(keepends=True),
        str(out),
        "aye inventory",
    )
    sys.stdout.writelines(lines)
    print(f"{manifest_path}: {out} differs from the inventory", file=sys.stderr)
    return 1


def write_inventory(
    manifest_path: Path,
    form: str = "ansible-yaml",
    out: Path | None = None,
    check_only: bool = False,
) -> int:
    manifest = load_manifest(manifest_path)
    modules = with_local_modules(manifest, load_modules())
    findings = check_manifest(manifest, modules)
    if findings:
        for finding in findings:
            print(finding, file=sys.stderr)
        print(f"{manifest_path}: no inventory, fix the errors first", file=sys.stderr)
        return 1
    text = inventory_text(manifest, modules, form)
    if out is None:
        sys.stdout.write(text)
        return 0
    if check_only:
        return check_file(manifest_path, out, text)
    out.write_text(text, encoding="utf-8", newline="\n")
    print(f"{manifest_path}: {len(manifest.hosts)} host(s) written to {out}")
    return 0


def add_parser(commands: Any) -> None:
    parser: argparse.ArgumentParser = commands.add_parser(
        "inventory",
        help="write an Ansible inventory of the manifest's hosts, grouped by zone, group,"
        " profile, module and service",
    )
    add_manifest(parser)
    parser.add_argument("--format", choices=FORMATS, default=FORMATS[0])
    parser.add_argument("-o", "--out", type=Path, help="the inventory file, default stdout")
    parser.add_argument(
        "--check",
        action="store_true",
        help="write nothing, print a diff against -o, exit 1 unless identical",
    )


def run(args: argparse.Namespace) -> int:
    if args.check and args.out is None:
        print("inventory: --check needs -o FILE to compare with", file=sys.stderr)
        return 2
    return write_inventory(args.manifest, args.format, args.out, args.check)
