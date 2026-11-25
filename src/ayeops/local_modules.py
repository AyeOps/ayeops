from __future__ import annotations

import json
import re
from functools import cache
from pathlib import Path
from typing import Any

import jsonschema
import yaml

from ayeops.manifest import Manifest
from ayeops.registry import Module, load_modules
from ayeops.strict import StrictValidator
from ayeops.yamlkeys import StrictYAMLError, load_strict, nested, syntax_problem

MODULE_SCHEMA_PATH = Path(__file__).parent / "schema" / "module.schema.json"
IMAGE = re.compile(r"image:\s*(\S+)")
PINNED_IMAGE = re.compile(
    r"^[a-z0-9./_-]+:v?[0-9]+(\.[0-9]+)+(-[a-z0-9.]+)?(@sha256:[a-f0-9]{64})?$"
)
TEMPLATES = ("compose.yaml.j2", "scrape.yaml.j2")

Problem = tuple[str, str, str]


@cache
def module_schema() -> dict[str, Any]:
    schema: dict[str, Any] = json.loads(MODULE_SCHEMA_PATH.read_text(encoding="utf-8"))
    return schema


def spec_problems(path: str, spec: Any) -> list[Problem]:
    validator = StrictValidator(module_schema())
    problems = [
        (".".join([path, *map(str, error.absolute_path)]), "module-spec", error.message)
        for error in sorted(validator.iter_errors(spec), key=str)
    ]
    if problems:
        return problems
    try:
        jsonschema.Draft202012Validator.check_schema(spec["settings_schema"])
    except jsonschema.SchemaError as error:
        problems.append((f"{path}.settings_schema", "module-spec", error.message))
    return problems


def is_url(value: str) -> bool:
    return value.startswith(("http://", "https://"))


def module_problems(path: str, module: Module, known: set[str]) -> list[Problem]:
    problems: list[Problem] = []
    for required in sorted(module.requires):
        if required not in known:
            problems.append((f"{path}.requires", "module-spec", f"unknown module {required!r}"))
    for entry in module.files:
        if not (module.path / entry["src"]).is_file():
            problems.append((f"{path}.files", "module-files", f"{entry['src']} does not exist"))
    for dashboard in module.dashboards:
        if not dashboard.is_file():
            problems.append(
                (
                    f"{path}.dashboards",
                    "module-files",
                    f"dashboards/{dashboard.name} does not exist",
                )
            )
    for template in TEMPLATES:
        if (module.path / template).is_file():
            for image in IMAGE.findall(module.template(template)):
                if not PINNED_IMAGE.match(image) or "latest" in image:
                    problems.append(
                        (
                            f"{path}.{template}",
                            "image-pinned",
                            f"{image} is not pinned to an exact version",
                        )
                    )
    for index, alert in enumerate(module.alerts):
        runbook = str(alert.get("runbook") or "").strip()
        page = module.path / "runbooks" / f"{runbook}.md"
        if runbook and not is_url(runbook) and not page.is_file():
            problems.append(
                (
                    f"{path}.alerts.{index}",
                    "alert-runbook",
                    f"runbook page runbooks/{runbook}.md is missing",
                )
            )
    return problems


def load_local_modules(manifest: Manifest) -> tuple[dict[str, Module], list[Problem]]:
    builtins = load_modules()
    modules: dict[str, Module] = {}
    paths: dict[str, str] = {}
    problems: list[Problem] = []
    raw = manifest.data.get("module_paths") or []
    for index, directory in enumerate(manifest.module_paths):
        where = f"module_paths.{index}"
        if not directory.is_dir():
            problems.append((where, "module-path", f"{raw[index]} is not a directory"))
            continue
        spec_paths = sorted(directory.glob("*/module.yaml"))
        if not spec_paths:
            problems.append((where, "module-path", f"{raw[index]} has no <name>/module.yaml"))
        for spec_path in spec_paths:
            path = f"{where}.{spec_path.parent.name}"
            source = f"{spec_path.parent.name}/module.yaml"
            try:
                spec = load_strict(spec_path.read_text(encoding="utf-8"), source)
            except StrictYAMLError as error:
                problems.extend(nested(path, error.problems))
                continue
            except yaml.YAMLError as error:
                problems.append((path, "module-spec", syntax_problem(source, error)[2]))
                continue
            errors = spec_problems(path, spec)
            if errors:
                problems.extend(errors)
                continue
            name = str(spec["name"])
            if name in builtins:
                problems.append(
                    (path, "module-clash", f"module {name!r} clashes with a built-in module")
                )
            elif name in modules:
                problems.append((path, "module-clash", f"module {name!r} is defined twice"))
            else:
                modules[name] = Module(name, spec_path.parent, spec, local=True)
                paths[name] = path
    known = set(builtins) | set(modules)
    for name, module in modules.items():
        problems.extend(module_problems(paths[name], module, known))
    return modules, problems


def with_local_modules(manifest: Manifest, modules: dict[str, Module]) -> dict[str, Module]:
    if not manifest.module_paths:
        return modules
    merged = dict(modules)
    for name, module in load_local_modules(manifest)[0].items():
        if name not in merged:
            merged[name] = module
    return merged
