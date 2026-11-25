from __future__ import annotations

import re
from pathlib import Path

from ayeops.manifest import Manifest
from ayeops.registry import Module

Problem = tuple[str, str, str]
PAGE_LINK = re.compile(r"\]\((?:\./)?([A-Za-z0-9][A-Za-z0-9_-]*)\.md(?:#[^)]*)?\)")


def linked_pages(text: str) -> set[str]:
    return set(PAGE_LINK.findall(text))


def reachable_pages(pages: dict[str, str], roots: set[str]) -> set[str]:
    reached: set[str] = set()
    queue = sorted(roots & set(pages))
    while queue:
        name = queue.pop()
        if name in reached:
            continue
        reached.add(name)
        queue.extend(sorted((linked_pages(pages[name]) & set(pages)) - reached))
    return reached


def runbook_pages(directory: Path) -> dict[str, str]:
    return {page.stem: page.read_text(encoding="utf-8") for page in sorted(directory.glob("*.md"))}


def alert_runbooks(module: Module) -> set[str]:
    return {str(alert.get("runbook") or "").strip().removesuffix(".md") for alert in module.alerts}


def unused_runbooks(path: str, module: Module) -> list[Problem]:
    pages = runbook_pages(module.path / "runbooks")
    used = reachable_pages(pages, alert_runbooks(module))
    return [
        (
            f"{path}.runbooks.{name}",
            "runbook-used",
            f"runbooks/{name}.md is the runbook of no alert of {module.name}"
            " and no runbook of one links to it",
        )
        for name in sorted(set(pages) - used)
    ]


def local_module_paths(manifest: Manifest, modules: dict[str, Module]) -> dict[str, str]:
    found = {}
    for index, directory in enumerate(manifest.module_paths):
        for name, module in modules.items():
            if module.local and module.path.parent == directory:
                found[name] = f"module_paths.{index}.{module.path.name}"
    return found


def unused_modules(manifest: Manifest, modules: dict[str, Module]) -> list[Problem]:
    enabled = manifest.enabled_modules()
    problems = []
    for name, path in sorted(local_module_paths(manifest, modules).items()):
        if name not in enabled:
            problems.append((path, "module-used", f"module {name!r} is enabled on no host"))
        problems += unused_runbooks(path, modules[name])
    return problems


def endpoint_refs(manifest: Manifest) -> set[str]:
    refs = set()
    for flow in manifest.flows:
        refs |= {str(flow.get("from")), str(flow.get("to"))}
    for service in manifest.services.values():
        refs |= {str(entry) for entry in service.get("depends_on") or []}
        refs |= {str(entry) for entry in service.get("clients") or []}
    return refs


def unused_externals(manifest: Manifest) -> list[Problem]:
    refs = endpoint_refs(manifest)
    return [
        (
            f"externals.{name}",
            "external-used",
            f"no flow, depends_on or clients entry names external:{name}",
        )
        for name in sorted(manifest.externals)
        if f"external:{name}" not in refs
    ]


def unused_problems(manifest: Manifest, modules: dict[str, Module]) -> list[Problem]:
    return unused_modules(manifest, modules) + unused_externals(manifest)
