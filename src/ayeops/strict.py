from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any

import jsonschema
import yaml
from jsonschema.exceptions import ValidationError


def ecma_pattern(pattern: str) -> str:
    out: list[str] = []
    in_class = False
    escaped = False
    for char in pattern:
        if escaped:
            out.append(char)
            escaped = False
        elif char == "\\":
            out.append(char)
            escaped = True
        elif in_class:
            out.append(char)
            in_class = char != "]"
        elif char == "[":
            out.append(char)
            in_class = True
        elif char == "$":
            out.append(r"\Z")
        else:
            out.append(char)
    return "".join(out)


def matches(pattern: str, value: str) -> bool:
    return re.search(ecma_pattern(pattern), value) is not None


def _pattern(
    validator: Any, pattern: str, instance: Any, schema: dict[str, Any]
) -> Iterator[ValidationError]:
    if validator.is_type(instance, "string") and not matches(pattern, instance):
        yield ValidationError(f"{instance!r} does not match {pattern!r}")


_extend: Any = jsonschema.validators.extend
StrictValidator: Any = _extend(jsonschema.Draft202012Validator, {"pattern": _pattern})


def compose_literal(value: Any) -> str:
    return str(value).replace("$", "$$")


class EnvRef(str):
    __slots__ = ()


yaml.SafeDumper.add_representer(EnvRef, yaml.representer.SafeRepresenter.represent_str)

FAST_LOADER: Any = getattr(yaml, "CSafeLoader", yaml.SafeLoader)


def load_yaml(text: str) -> Any:
    return yaml.load(text, Loader=FAST_LOADER)


def compose_safe(value: Any) -> Any:
    if isinstance(value, EnvRef):
        return value
    if isinstance(value, str):
        return compose_literal(value)
    if isinstance(value, dict):
        return {key: compose_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [compose_safe(item) for item in value]
    if isinstance(value, tuple):
        return tuple(compose_safe(item) for item in value)
    return value
