from __future__ import annotations

import argparse
import ast
import fnmatch
import hashlib
import importlib
import json
import math
import os
import re
import subprocess
import sys
import tomllib
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from ayeops.report import MUTMUT_LINE, MUTMUT_RESULTS, mutmut_module, tally

CONFIG = Path("tests/mutants.toml")
PYPROJECT = Path("pyproject.toml")
LOCK = Path("uv.lock")
SHARED_TESTS = ("tests/conftest.py",)
FIXTURES = Path("tests/fixtures")
MIRRORS = frozenset({"only_mutate", "pytest_add_cli_args_test_selection"})
TABLE = re.compile(r"^\[")
HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", re.MULTILINE)
SEPARATOR = "ǁ"
SUFFIX = "__mutmut_"
NEVER_MUTATED = frozenset({"__getattribute__", "__setattr__", "__new__"})
TRAMPOLINE_DECORATORS = frozenset({"staticmethod", "classmethod"})
MISSED = frozenset({"survived", "no_coverage"})
DETECTED = frozenset({"killed", "timeout"})
CACHE_VERSION = 1

Git = Callable[..., str | None]
FunctionNode = ast.FunctionDef | ast.AsyncFunctionDef


class MutantsError(Exception):
    pass


@dataclass(frozen=True)
class Target:
    path: str
    tests: tuple[str, ...]
    score: float | None = None

    @property
    def module(self) -> str:
        return module_name(self.path)


@dataclass(frozen=True)
class Verdict:
    target: Target
    mutants: int
    score: float | None

    @property
    def measured(self) -> float | None:
        return None if self.score is None else floor_tenth(self.score)

    @property
    def status(self) -> str:
        baseline = self.target.score
        if self.measured is None:
            return "not run"
        if baseline is None:
            return "no baseline"
        if self.measured < baseline:
            return "below baseline"
        if self.measured > baseline:
            return "above baseline"
        return "ok"

    @property
    def failed(self) -> bool:
        return self.target.score is not None and self.status in ("not run", "below baseline")


def module_name(path: str) -> str:
    return path.removesuffix(".py").removeprefix("src/").replace("/", ".")


def floor_tenth(value: float) -> float:
    return math.floor(value * 10 + 1e-9) / 10


def parse_targets(text: str) -> dict[str, Target]:
    data = tomllib.loads(text)
    modules = data.get("modules")
    if not isinstance(modules, dict) or not modules:
        raise MutantsError('the mutants file lists no [modules."<path>"] tables')
    targets: dict[str, Target] = {}
    for path, entry in modules.items():
        tests = entry.get("tests") if isinstance(entry, dict) else None
        if not isinstance(tests, list) or not tests or not all(isinstance(t, str) for t in tests):
            raise MutantsError(f"{path}: tests must be a non-empty list of test files")
        score = entry.get("score")
        if score is not None and (
            isinstance(score, bool) or not isinstance(score, int | float) or not 0 <= score <= 100
        ):
            raise MutantsError(f"{path}: score must be a number from 0 to 100")
        targets[path] = Target(path, tuple(tests), None if score is None else float(score))
    return targets


def dump_targets(targets: Iterable[Target]) -> str:
    blocks = []
    for target in sorted(targets, key=lambda t: t.path):
        lines = [
            f"[modules.{json.dumps(target.path)}]",
            f"tests = {toml_value(list(target.tests))}",
        ]
        if target.score is not None:
            lines.append(f"score = {target.score:.1f}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) + "\n"


def load_targets(path: Path) -> dict[str, Target]:
    try:
        return parse_targets(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise MutantsError(f"{path}: {error.strerror}") from error
    except tomllib.TOMLDecodeError as error:
        raise MutantsError(f"{path}: {error}") from error


def toml_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, list):
        return "[" + ", ".join(toml_value(item) for item in value) + "]"
    raise MutantsError(f"cannot write {value!r} to TOML")


def replace_table(text: str, header: str, body: Mapping[str, object]) -> str:
    lines = text.splitlines(keepends=True)
    try:
        start = next(i for i, line in enumerate(lines) if line.strip() == header)
    except StopIteration as error:
        raise MutantsError(f"no {header} table") from error
    end = next((i for i in range(start + 1, len(lines)) if TABLE.match(lines[i])), len(lines))
    block = [f"{header}\n", *(f"{key} = {toml_value(value)}\n" for key, value in body.items())]
    if end < len(lines):
        block.append("\n")
    return "".join([*lines[:start], *block, *lines[end:]])


def narrowed(pyproject: str, target: Target) -> str:
    table = dict(tomllib.loads(pyproject)["tool"]["mutmut"])
    table["only_mutate"] = [target.path]
    table["pytest_add_cli_args_test_selection"] = list(target.tests)
    return replace_table(pyproject, "[tool.mutmut]", table)


def run_git(*args: str) -> str | None:
    try:
        done = subprocess.run(
            ["git", *args], capture_output=True, text=True, encoding="utf-8", timeout=60
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 else None


def mutation_settings(pyproject: str | None) -> tuple[Any, Any]:
    if pyproject is None:
        return None, None
    try:
        data = tomllib.loads(pyproject)
    except tomllib.TOMLDecodeError:
        return None, None
    table = data.get("tool", {}).get("mutmut")
    if isinstance(table, dict):
        table = {key: value for key, value in table.items() if key not in MIRRORS}
    return table, data.get("dependency-groups", {}).get("mutants")


def changed_lines(diff: str) -> tuple[set[int], set[int]]:
    removed: set[int] = set()
    added: set[int] = set()
    for match in HUNK.finditer(diff):
        old, old_count, new, new_count = match.groups()
        removed.update(range(int(old), int(old) + int(old_count or 1)))
        added.update(range(int(new), int(new) + int(new_count or 1)))
    return removed, added


def mangled(name: str, owner: str | None) -> str:
    return f"x{SEPARATOR}{owner}{SEPARATOR}{name}" if owner else f"x_{name}"


def trampolined(node: FunctionNode) -> bool:
    if node.name in NEVER_MUTATED:
        return False
    decorators = node.decorator_list
    return not decorators or (
        len(decorators) == 1
        and isinstance(decorators[0], ast.Name)
        and decorators[0].id in TRAMPOLINE_DECORATORS
    )


def functions(tree: ast.Module) -> list[tuple[str, FunctionNode, str | None]]:
    found: list[tuple[str, FunctionNode, str | None]] = []
    for node in tree.body:
        if isinstance(node, FunctionNode):
            found.append((mangled(node.name, None), node, None))
        elif isinstance(node, ast.ClassDef):
            found.extend(
                (mangled(item.name, node.name), item, node.name)
                for item in node.body
                if isinstance(item, FunctionNode)
            )
    return found


def mutated_functions(source: str) -> set[str]:
    return {name for name, node, _ in functions(ast.parse(source)) if trampolined(node)}


def touches(node: ast.stmt, lines: set[int]) -> bool:
    decorators = getattr(node, "decorator_list", [])
    first = min([node.lineno, *(decorator.lineno for decorator in decorators)])
    last = node.end_lineno or node.lineno
    return any(first <= line <= last for line in lines)


def bound_names(node: ast.stmt) -> set[str]:
    names: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Store):
            names.add(child.id)
        elif isinstance(child, ast.alias):
            names.add((child.asname or child.name).partition(".")[0])
    return names


def used_names(node: ast.AST, attributes: bool) -> set[str]:
    names: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Name):
            names.add(child.id)
        elif attributes and isinstance(child, ast.Attribute):
            names.add(child.attr)
    return names


def touched(source: str, lines: set[int]) -> set[str]:
    if not lines:
        return set()
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    module_names: set[str] = set()
    class_names: dict[str, set[str]] = {}
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            for item in node.body:
                if not isinstance(item, FunctionNode) and touches(item, lines):
                    class_names.setdefault(node.name, set()).update(bound_names(item))
        elif not isinstance(node, FunctionNode) and touches(node, lines):
            module_names |= bound_names(node)
    return {
        name
        for name, node, owner in functions(tree)
        if touches(node, lines)
        or used_names(node, attributes=False) & module_names
        or (owner is not None and used_names(node, attributes=True) & class_names.get(owner, set()))
    }


def changed_functions(old: str | None, new: str, diff: str) -> list[str]:
    removed, added = changed_lines(diff)
    found = touched(new, added)
    if old is not None:
        found |= touched(old, removed)
    return sorted(found & mutated_functions(new))


def diff_functions(path: str, base: str, git: Git = run_git, root: Path = Path()) -> list[str]:
    diff = git("diff", "--unified=0", "--no-renames", f"{base}...HEAD", "--", path)
    fork = git("merge-base", base, "HEAD")
    if diff is None or fork is None:
        raise MutantsError(f"git diff against {base} failed; fetch the base branch first")
    old = git("show", f"{fork.strip()}:{path}")
    return changed_functions(old, (root / path).read_text(encoding="utf-8"), diff)


def patterns(module: str, names: Iterable[str]) -> list[str]:
    return [f"{module}.{name}{SUFFIX}*" for name in names]


def mutmut_counts(path: str, source: str) -> dict[str, int] | None:
    try:
        mutation = importlib.import_module("mutmut.mutation.file_mutation")
    except ImportError:
        return None
    mutated = mutation.mutate_file_contents(path, source)
    return dict(Counter(str(name).partition(SUFFIX)[0] for name in mutated.mutant_names))


def select(
    targets: Mapping[str, Target], base: str, git: Git = run_git, root: Path = Path()
) -> list[Target]:
    if not base:
        return list(targets.values())
    diff = git("diff", "--name-only", "--no-renames", f"{base}...HEAD")
    if diff is None:
        raise MutantsError(f"git diff against {base} failed; fetch the base branch first")
    changed = set(diff.split())
    return [
        target
        for target in targets.values()
        if target.path in changed and diff_functions(target.path, base, git, root)
    ]


@dataclass(frozen=True)
class Checked:
    pattern: str
    statuses: Mapping[str, int]
    missed: tuple[str, ...]

    @property
    def function(self) -> str:
        return self.pattern.removesuffix(f"{SUFFIX}*")

    @property
    def mutants(self) -> int:
        return sum(count for status, count in self.statuses.items() if status != "ignored")

    @property
    def status(self) -> str:
        if self.missed:
            return "survivors"
        return "ok" if self.mutants else "not run"


def checked(results: str, wanted: Iterable[str]) -> list[Checked]:
    lines = [
        (key, MUTMUT_RESULTS[status])
        for key, status in (
            match.groups() for match in map(MUTMUT_LINE.match, results.splitlines()) if match
        )
    ]
    found = []
    for pattern in wanted:
        mine = [(key, status) for key, status in lines if fnmatch.fnmatchcase(key, pattern)]
        missed = tuple(key for key, status in mine if status in MISSED)
        found.append(Checked(pattern, dict(Counter(status for _, status in mine)), missed))
    return found


def checked_table(found: Sequence[Checked]) -> str:
    rows = ["| Function | Mutants | Detected | Missed | Status |", "|---|---|---|---|---|"]
    for item in found:
        detected = sum(item.statuses.get(status, 0) for status in DETECTED)
        rows.append(
            f"| `{item.function}` | {item.mutants} | {detected} | {len(item.missed)} "
            f"| {item.status} |"
        )
    return "\n".join(rows) + "\n"


def functions_table(module: str, names: Sequence[str], counts: Mapping[str, int] | None) -> str:
    if not names:
        return f"No function of {module} that mutmut mutates changed.\n"
    rows = ["| Function | Mutants |", "|---|---|"]
    for name in names:
        count = "?" if counts is None else str(counts.get(name, 0))
        rows.append(f"| `{module}.{name}` | {count} |")
    return "\n".join(rows) + "\n"


def file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def locked_packages(text: str, project: str) -> str:
    data = tomllib.loads(text)
    for package in data.get("package", []):
        if package.get("name") == project:
            package.pop("version", None)
    return json.dumps(data, sort_keys=True)


def cache_keys(target: Target, root: Path = Path()) -> tuple[str, str]:
    digest = hashlib.sha256()
    tests = [*target.tests, *SHARED_TESTS]
    fixtures = sorted(
        path.relative_to(root).as_posix() for path in (root / FIXTURES).rglob("*") if path.is_file()
    )
    for name in [*tests, *fixtures]:
        digest.update(f"{name}\0{file_digest(root / name)}\0".encode())
    pyproject = (root / PYPROJECT).read_text(encoding="utf-8")
    project = tomllib.loads(pyproject).get("project", {})
    settings = [
        *mutation_settings(pyproject),
        project.get("dependencies"),
        project.get("requires-python"),
    ]
    digest.update(json.dumps(settings, sort_keys=True).encode())
    lock = root / LOCK
    if lock.is_file():
        digest.update(
            locked_packages(lock.read_text(encoding="utf-8"), project.get("name", "")).encode()
        )
    prefix = f"mutants-{CACHE_VERSION}-{target.module}-{digest.hexdigest()[:16]}"
    return prefix, f"{prefix}-{file_digest(root / target.path)[:16]}"


def results_by_module(text: str) -> dict[str, list[tuple[str, int]]]:
    found: dict[str, list[tuple[str, int]]] = {}
    for line in text.splitlines():
        match = MUTMUT_LINE.match(line)
        if match:
            key, status = match.groups()
            found.setdefault(mutmut_module(key), []).append((MUTMUT_RESULTS[status], 1))
    return found


def verdicts(targets: Iterable[Target], results: str) -> list[Verdict]:
    by_module = results_by_module(results)
    found = []
    for target in targets:
        pairs = by_module.get(target.module, [])
        mutants = tally("mutmut", pairs)
        checked = sum(count for status, count in mutants.counts.items() if status != "ignored")
        found.append(Verdict(target, checked, mutants.score))
    return found


def table(found: Sequence[Verdict]) -> str:
    rows = [
        "| Module | Mutants | Score | Baseline | Status |",
        "|---|---|---|---|---|",
    ]
    for verdict in found:
        score = "-" if verdict.measured is None else f"{verdict.measured:.1f}"
        baseline = "-" if verdict.target.score is None else f"{verdict.target.score:.1f}"
        rows.append(
            f"| `{verdict.target.path}` | {verdict.mutants} | {score} | {baseline} "
            f"| {verdict.status} |"
        )
    return "\n".join(rows) + "\n"


def advice(found: Sequence[Verdict], config: Path) -> list[str]:
    lines = []
    for verdict in found:
        if verdict.measured is not None and verdict.status in ("no baseline", "above baseline"):
            lines.append(
                f"Raise the baseline: set score = {verdict.measured:.1f} under"
                f' [modules."{verdict.target.path}"] in {config.as_posix()}.'
            )
        if verdict.status == "below baseline":
            lines.append(
                f"{verdict.target.path} dropped to {verdict.measured:.1f} from"
                f" {verdict.target.score:.1f}: kill the new survivors"
                f' (mutmut results, mutmut show <name>) or add tests to its "tests".'
            )
    return lines


def raised(targets: Mapping[str, Target], found: Iterable[Verdict]) -> dict[str, Target]:
    updated = dict(targets)
    for verdict in found:
        current = verdict.target.score
        if verdict.measured is not None and (current is None or verdict.measured > current):
            updated[verdict.target.path] = replace(verdict.target, score=verdict.measured)
    return updated


def write_output(env: Mapping[str, str], key: str, value: str) -> None:
    path = env.get(key, "")
    if path:
        with Path(path).open("a", encoding="utf-8") as handle:
            handle.write(value)


def pick(targets: Mapping[str, Target], paths: Sequence[str]) -> list[Target]:
    unknown = [path for path in paths if path not in targets]
    if unknown:
        raise MutantsError(f"not in the mutants file: {', '.join(unknown)}")
    return [targets[path] for path in paths] if paths else list(targets.values())


def command_select(args: argparse.Namespace, env: Mapping[str, str]) -> int:
    chosen = select(load_targets(args.config), args.base)
    paths = [target.path for target in chosen]
    for path in paths:
        print(path)
    if not paths:
        print(
            "no function that mutmut mutates changed in a module of the mutants file",
            file=sys.stderr,
        )
    write_output(env, "GITHUB_OUTPUT", f"modules={json.dumps(paths)}\n")
    return 0


def command_functions(args: argparse.Namespace, env: Mapping[str, str]) -> int:
    (target,) = pick(load_targets(args.config), [args.module])
    names = diff_functions(target.path, args.base)
    counts = mutmut_counts(target.path, Path(target.path).read_text(encoding="utf-8"))
    if counts is not None:
        names = [name for name in names if counts.get(name)]
    args.output.write_text("".join(f"{line}\n" for line in patterns(target.module, names)), "utf-8")
    report = functions_table(target.module, names, counts)
    print(report, end="")
    write_output(env, "GITHUB_STEP_SUMMARY", report)
    write_output(env, "GITHUB_OUTPUT", f"count={len(names)}\n")
    return 0


def command_gate(args: argparse.Namespace, env: Mapping[str, str]) -> int:
    try:
        results = args.results.read_text(encoding="utf-8")
        wanted = args.functions.read_text(encoding="utf-8").split()
    except OSError as error:
        raise MutantsError(f"{error.filename}: {error.strerror}") from error
    found = checked(results, wanted)
    if not found:
        print("no function that mutmut mutates changed")
        return 0
    report = checked_table(found)
    notes = [f"survived: {key} (mutmut show {key})" for item in found for key in item.missed]
    print(report, end="")
    for note in notes:
        print(note)
    write_output(env, "GITHUB_STEP_SUMMARY", report + "".join(f"\n{n}\n" for n in notes))
    failed = [item.function for item in found if item.status != "ok"]
    if failed:
        print(f"surviving or untested mutants in: {', '.join(failed)}", file=sys.stderr)
        return 1
    return 0


def command_cache_key(args: argparse.Namespace, env: Mapping[str, str]) -> int:
    (target,) = pick(load_targets(args.config), [args.module])
    prefix, key = cache_keys(target)
    print(key)
    write_output(env, "GITHUB_OUTPUT", f"prefix={prefix}\nkey={key}\n")
    return 0


def command_narrow(args: argparse.Namespace, env: Mapping[str, str]) -> int:
    (target,) = pick(load_targets(args.config), [args.module])
    PYPROJECT.write_text(narrowed(PYPROJECT.read_text(encoding="utf-8"), target), "utf-8")
    print(f"[tool.mutmut] narrowed to {target.path} and {' '.join(target.tests)}")
    return 0


def command_score(args: argparse.Namespace, env: Mapping[str, str]) -> int:
    targets = load_targets(args.config)
    try:
        results = args.results.read_text(encoding="utf-8")
    except OSError as error:
        raise MutantsError(f"{args.results}: {error.strerror}") from error
    found = verdicts(pick(targets, args.modules), results)
    report = table(found)
    notes = advice(found, args.config)
    print(report, end="")
    for note in notes:
        print(note)
    write_output(env, "GITHUB_STEP_SUMMARY", report + "".join(f"\n{n}\n" for n in notes))
    if args.write:
        args.config.write_text(dump_targets(raised(targets, found).values()), "utf-8")
    failed = [verdict.target.path for verdict in found if verdict.failed]
    if failed:
        print(f"mutation score below the baseline: {', '.join(failed)}", file=sys.stderr)
        return 1
    return 0


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog="python -m ayeops.mutants",
        description="select, narrow and score the mutation tests of tests/mutants.toml",
    )
    root.add_argument("--config", type=Path, default=CONFIG)
    commands = root.add_subparsers(dest="command", required=True)
    chosen = commands.add_parser("select", help="modules with functions changed since --base")
    chosen.add_argument("--base", default="", help="git ref to diff against; empty selects all")
    chosen.set_defaults(handler=command_select)
    narrow = commands.add_parser("narrow", help="rewrite [tool.mutmut] for one module")
    narrow.add_argument("module")
    narrow.set_defaults(handler=command_narrow)
    changed = commands.add_parser("functions", help="mutant patterns of the changed functions")
    changed.add_argument("module")
    changed.add_argument("--base", required=True, help="git ref to diff against")
    changed.add_argument("--output", type=Path, required=True, help="file for the patterns")
    changed.set_defaults(handler=command_functions)
    gate = commands.add_parser("gate", help="fail on a surviving mutant of the changed functions")
    gate.add_argument("results", type=Path, help="output of mutmut results --all true")
    gate.add_argument("--functions", type=Path, required=True, help="output of functions")
    gate.set_defaults(handler=command_gate)
    key = commands.add_parser("cache-key", help="actions/cache keys of the module's verdicts")
    key.add_argument("module")
    key.set_defaults(handler=command_cache_key)
    score = commands.add_parser("score", help="per-module scores against the baselines")
    score.add_argument("results", type=Path, help="output of mutmut results --all true")
    score.add_argument("--modules", nargs="*", default=[], help="modules to gate, default all")
    score.add_argument("--write", action="store_true", help="raise the baselines that improved")
    score.set_defaults(handler=command_score)
    return root


def main(argv: list[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    args = parser().parse_args(argv)
    handler: Callable[[argparse.Namespace, Mapping[str, str]], int] = args.handler
    try:
        return handler(args, os.environ if env is None else env)
    except MutantsError as error:
        print(f"mutants: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
