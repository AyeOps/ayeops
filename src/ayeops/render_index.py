from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from ayeops.render import write_tree

INDEX = ".aye-render"


@dataclass(frozen=True)
class Drift:
    added: tuple[str, ...] = ()
    changed: tuple[str, ...] = ()
    stale: tuple[str, ...] = ()

    def lines(self) -> list[str]:
        return [
            f"{kind} {path}"
            for kind, paths in (
                ("added", self.added),
                ("changed", self.changed),
                ("stale", self.stale),
            )
            for path in paths
        ]

    def __bool__(self) -> bool:
        return bool(self.added or self.changed or self.stale)


def posix(relative: str) -> str:
    return relative.replace("\\", "/")


def normalised(tree: dict[str, str]) -> dict[str, str]:
    return {posix(path): text for path, text in tree.items()}


def index_text(paths: Iterable[str]) -> str:
    return "".join(f"{path}\n" for path in sorted({posix(path) for path in paths}))


def safe(entry: str) -> bool:
    parts = entry.split("/")
    return (
        "\\" not in entry
        and ":" not in entry
        and not parts[0].startswith(".aye-")
        and all(part not in ("", ".", "..") for part in parts)
    )


def previous(out_dir: Path) -> list[str]:
    index = out_dir / INDEX
    if not index.is_file():
        return []
    return [line for line in index.read_text(encoding="utf-8").splitlines() if safe(line)]


def stale(tree: dict[str, str], out_dir: Path) -> list[str]:
    root = out_dir.resolve()
    produced = set(normalised(tree))
    return sorted(
        entry
        for entry in set(previous(out_dir)) - produced
        if (out_dir / entry).is_file() and (out_dir / entry).resolve().is_relative_to(root)
    )


def expected(tree: dict[str, str]) -> dict[str, str]:
    files = normalised(tree)
    return dict(sorted({**files, INDEX: index_text(files)}.items()))


def drift(tree: dict[str, str], out_dir: Path) -> Drift:
    added: list[str] = []
    changed: list[str] = []
    for path, text in expected(tree).items():
        target = out_dir / path
        if not target.is_file():
            added.append(path)
        elif target.read_bytes() != text.encode("utf-8"):
            changed.append(path)
    return Drift(tuple(added), tuple(changed), tuple(stale(tree, out_dir)))


def prune(directory: Path, root: Path) -> None:
    while directory != root and directory.is_relative_to(root) and not any(directory.iterdir()):
        directory.rmdir()
        directory = directory.parent


def sync(tree: dict[str, str], out_dir: Path) -> tuple[list[Path], list[str]]:
    files = normalised(tree)
    removed = stale(tree, out_dir)
    for entry in removed:
        target = out_dir / entry
        target.unlink()
        prune(target.parent, out_dir)
    written = write_tree(files, out_dir)
    write_tree({INDEX: index_text(files)}, out_dir)
    return written, removed
