from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import jinja2

from ayeops import secretkeys
from ayeops.defaults import add_manifest
from ayeops.local_modules import with_local_modules
from ayeops.manifest import Manifest, load_manifest
from ayeops.registry import Module, load_modules
from ayeops.rules import check_manifest

TEMPLATES = jinja2.Environment(autoescape=False, undefined=jinja2.ChainableUndefined)
COPY = "copy"


@dataclass(frozen=True)
class Credential:
    module: str
    issuer: str
    credential: str
    env: tuple[str, ...]
    grants: tuple[str, ...]


@dataclass(frozen=True)
class Generated:
    module: str
    env: str
    format: str
    size: int | None
    source: str

    @property
    def copied(self) -> bool:
        return self.format == COPY

    def shape(self) -> str:
        if self.copied:
            return f"copy of {self.source}"
        return secretkeys.shape(self.format, self.size)


def module_context(
    manifest: Manifest, modules: dict[str, Module], host: str, name: str
) -> dict[str, Any]:
    merged = {
        module: modules[module].settings(settings)
        for module, settings in manifest.host_modules(host).items()
        if module in modules
    }
    return {
        "settings": merged.get(name, {}),
        "modules": merged,
        "host": host,
        "project": manifest.project,
        "mail": manifest.mail,
        "contact": manifest.contact,
    }


def render(text: str, context: dict[str, Any]) -> str:
    return TEMPLATES.from_string(text).render(context).strip()


def holds(expression: str | None, context: dict[str, Any]) -> bool:
    if not expression:
        return True
    return bool(TEMPLATES.compile_expression(expression)(context))


def contexts(entry: dict[str, Any], context: dict[str, Any]) -> list[dict[str, Any]]:
    if not entry.get("each"):
        return [context] if holds(entry.get("when"), context) else []
    items = TEMPLATES.compile_expression(entry["each"])(context) or []
    chosen = [{**context, "item": item} for item in items]
    return [scope for scope in chosen if holds(entry.get("when"), scope)]


def credentials(
    manifest: Manifest, modules: dict[str, Module], host: str, name: str
) -> list[Credential]:
    found: list[Credential] = []
    context = module_context(manifest, modules, host, name)
    for entry in modules[name].permissions:
        for scope in contexts(entry, context):
            grants = [
                line.strip()
                for grant in entry["grants"]
                for line in render(grant, scope).splitlines()
                if line.strip()
            ]
            found.append(
                Credential(
                    name,
                    render(entry["issuer"], scope),
                    render(entry["credential"], scope),
                    tuple(render(env, scope) for env in entry["env"]),
                    tuple(grants),
                )
            )
    return found


def entry_items(name: str, entry: dict[str, Any], source: str) -> list[Generated]:
    return [
        Generated(name, env, entry["format"], entry.get("bytes"), source) for env in entry["env"]
    ]


def generated_entries(
    manifest: Manifest, modules: dict[str, Module], host: str, name: str
) -> list[Generated]:
    context = module_context(manifest, modules, host, name)
    return [
        item
        for entry in modules[name].generated
        if holds(entry.get("when"), context)
        for item in entry_items(name, entry, render(entry.get("from") or "", context))
    ]


def generated(manifest: Manifest, modules: dict[str, Module], host: str, name: str) -> list[str]:
    return [item.env for item in generated_entries(manifest, modules, host, name)]


def host_generated(manifest: Manifest, modules: dict[str, Module], host: str) -> list[Generated]:
    found: dict[str, Generated] = {}
    for name in sorted(manifest.host_modules(host)):
        if name in modules:
            for item in generated_entries(manifest, modules, host, name):
                found.setdefault(item.env, item)
    return [found[env] for env in sorted(found)]


def catalog_generated(modules: dict[str, Module]) -> dict[str, Generated]:
    found: dict[str, Generated] = {}
    for name in sorted(modules):
        for entry in modules[name].generated:
            for item in entry_items(name, entry, entry.get("from") or ""):
                found.setdefault(item.env, item)
    return found


def host_lines(manifest: Manifest, modules: dict[str, Module], host: str, path: Path) -> list[str]:
    lines = [
        host,
        f"  host access: aye host-init {path.as_posix()} --host {host}, a deploy user that may run"
        " the aye-host helper on its directory and nothing else",
    ]
    for name in sorted(manifest.host_modules(host)):
        if name not in modules:
            continue
        found = credentials(manifest, modules, host, name)
        if not found:
            continue
        lines.append(f"  {name}")
        for credential in found:
            lines.append(
                f"    {credential.issuer}, {credential.credential}: {', '.join(credential.env)}"
            )
            lines.extend(f"      - {grant}" for grant in credential.grants)
    own = host_generated(manifest, modules, host)
    made = [item for item in own if not item.copied]
    copies = [item for item in own if item.copied]
    if made:
        lines.append(
            "  generated by you, no external grant;"
            f" aye secrets gen --manifest {path.as_posix()} --host {host} stores the missing ones:"
        )
        lines.extend(f"    {item.env}: {item.shape()}" for item in made)
    if copies:
        lines.append("  copied from another AyeOps host, no external grant:")
        lines.extend(f"    {item.env}: {item.shape()}" for item in copies)
    return lines


def permissions_text(manifest: Manifest, modules: dict[str, Module], path: Path) -> str:
    blocks = ["\n".join(host_lines(manifest, modules, host, path)) for host in manifest.hosts]
    return "\n\n".join(blocks) + "\n"


def add_parser(commands: Any) -> None:
    parser: argparse.ArgumentParser = commands.add_parser(
        "permissions",
        help="print the minimal external credentials the manifest needs, per host and module",
    )
    add_manifest(parser)


def run(args: argparse.Namespace) -> int:
    manifest = load_manifest(args.manifest)
    modules = with_local_modules(manifest, load_modules())
    findings = check_manifest(manifest, modules)
    if findings:
        for finding in findings:
            print(finding, file=sys.stderr)
        print(f"{args.manifest}: no permissions, fix the errors first", file=sys.stderr)
        return 1
    sys.stdout.write(permissions_text(manifest, modules, args.manifest))
    return 0
