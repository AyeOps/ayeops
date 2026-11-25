from __future__ import annotations

import argparse
import os
from collections.abc import Mapping
from pathlib import Path

from ayeops.manifest import load_manifest

MANIFEST_ENV = "AYE_MANIFEST"
MANIFEST_NAMES = ("ayeops.yaml", "manifest.yaml")


class UsageError(Exception):
    pass


def find_manifest(
    given: Path | None, environ: Mapping[str, str] | None = None, cwd: Path | None = None
) -> Path:
    if given is not None:
        return given
    named = (os.environ if environ is None else environ).get(MANIFEST_ENV, "")
    if named:
        return Path(named)
    here = Path() if cwd is None else cwd
    found = [here / name for name in MANIFEST_NAMES if (here / name).is_file()]
    if len(found) == 1:
        return found[0]
    if found:
        raise UsageError(
            f"both {' and '.join(MANIFEST_NAMES)} are here; name the manifest,"
            f" e.g. aye check {MANIFEST_NAMES[0]}, or set {MANIFEST_ENV}"
        )
    raise UsageError(
        f"no manifest: name its path, set {MANIFEST_ENV},"
        f" or run where {' or '.join(MANIFEST_NAMES)} is"
    )


def host_names(manifest_path: Path) -> list[str]:
    hosts = load_manifest(manifest_path).data.get("hosts")
    return [str(name) for name in hosts] if isinstance(hosts, dict) else []


def pick_host(manifest_path: Path, host: str | None) -> str:
    if host is None:
        return only_host(manifest_path)
    names = host_names(manifest_path)
    if host not in names:
        raise UsageError(f"{manifest_path}: unknown host {host!r}, hosts: {', '.join(names)}")
    return host


def only_host(manifest_path: Path, fleet: bool = False) -> str:
    names = host_names(manifest_path)
    if len(names) == 1:
        return str(names[0])
    choice = "--host <name> or --all" if fleet else "--host <name>"
    if not names:
        raise UsageError(f"{manifest_path} has no hosts; run aye check {manifest_path}")
    raise UsageError(
        f"{manifest_path} has {len(names)} hosts, name one with {choice}: {', '.join(names)}"
    )


def add_manifest(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "manifest",
        type=Path,
        nargs="?",
        help=f"default ${MANIFEST_ENV}, else {' or '.join(MANIFEST_NAMES)} in this directory",
    )
