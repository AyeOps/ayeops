from __future__ import annotations

import json
from typing import Any

import yaml

STR_TAG = "tag:yaml.org,2002:str"
MERGE_TAG = "tag:yaml.org,2002:merge"
KINDS = {
    "bool": "a boolean",
    "int": "a number",
    "float": "a number",
    "null": "null",
    "timestamp": "a date",
    "binary": "binary data",
}
Problem = tuple[str, str, str]


class StrictYAMLError(yaml.YAMLError):
    def __init__(self, source: str, problems: list[Problem]) -> None:
        super().__init__("; ".join(message for _, _, message in problems))
        self.source = source
        self.problems = problems


def position(source: str, mark: yaml.Mark) -> str:
    return f"{source} line {mark.line + 1}, column {mark.column + 1}"


def dotted(path: tuple[str, ...]) -> str:
    return ".".join(path) or "<root>"


def key_kind(key: yaml.Node) -> str:
    if isinstance(key, yaml.MappingNode):
        return "a mapping"
    if isinstance(key, yaml.SequenceNode):
        return "a sequence"
    suffix = key.tag.rpartition(":")[2]
    return KINDS.get(suffix, key.tag)


def key_type_problem(source: str, path: tuple[str, ...], key: yaml.Node) -> Problem:
    where = position(source, key.start_mark)
    kind = key_kind(key)
    if not isinstance(key, yaml.ScalarNode):
        return (dotted(path), "yaml-key-type", f"{where}: a key is {kind}, not a string")
    text = str(key.value)
    quoted = json.dumps(text)
    message = f"{where}: the key {text} is read as {kind}, not a string; quote it: {quoted}"
    return (dotted((*path, text)), "yaml-key-type", message)


def duplicate_problem(
    source: str, path: tuple[str, ...], first: yaml.Node, again: yaml.Node
) -> Problem:
    where = position(source, again.start_mark)
    line, column = first.start_mark.line + 1, first.start_mark.column + 1
    message = (
        f"{where}: the key {again.value!r} repeats line {line}, column {column};"
        " YAML keeps only the last one"
    )
    return (dotted(path), "yaml-duplicate-key", message)


def unreadable(node: yaml.Node, error: Exception) -> yaml.constructor.ConstructorError:
    kind = key_kind(node)
    if isinstance(node, yaml.ScalarNode):
        subject = f"{node.value} cannot be read as {kind}"
    else:
        subject = f"{kind} cannot be read"
    return yaml.constructor.ConstructorError(None, None, f"{subject}: {error}", node.start_mark)


class StrictLoader(yaml.SafeLoader):
    source = "<unicode string>"
    key_types = True

    def construct_object(self, node: yaml.Node, deep: bool = False) -> Any:
        try:
            return super().construct_object(node, deep)
        except (ValueError, TypeError, LookupError, AttributeError) as error:
            raise unreadable(node, error) from error

    def get_single_data(self) -> Any:
        node = self.get_single_node()
        if node is None:
            return None
        problems = self.key_problems(node, (), set())
        if problems:
            raise StrictYAMLError(self.source, problems)
        return self.construct_document(node)

    def key_problems(
        self, node: yaml.Node, path: tuple[str, ...], walked: set[int]
    ) -> list[Problem]:
        if id(node) in walked:
            return []
        walked.add(id(node))
        if isinstance(node, yaml.SequenceNode):
            return [
                problem
                for index, item in enumerate(node.value)
                for problem in self.key_problems(item, (*path, str(index)), walked)
            ]
        if not isinstance(node, yaml.MappingNode):
            return []
        problems: list[Problem] = []
        seen: dict[Any, yaml.Node] = {}
        for key, value in node.value:
            if key.tag == MERGE_TAG:
                problems += self.key_problems(value, path, walked)
                continue
            if self.key_types and key.tag != STR_TAG:
                problems.append(key_type_problem(self.source, path, key))
            if not isinstance(key, yaml.ScalarNode):
                continue
            identity = self.construct_object(key)
            here = (*path, str(key.value))
            if identity in seen:
                problems.append(duplicate_problem(self.source, here, seen[identity], key))
            else:
                seen[identity] = key
            problems += self.key_problems(value, here, walked)
        return problems


def load_strict(text: str, source: str, key_types: bool = True) -> Any:
    loader = StrictLoader(text)
    loader.source = source
    loader.key_types = key_types
    try:
        return loader.get_single_data()
    finally:
        loader.dispose()


def syntax_problem(source: str, error: yaml.YAMLError) -> Problem:
    if not isinstance(error, yaml.MarkedYAMLError) or error.problem_mark is None:
        return ("<root>", "yaml-syntax", f"{source}: {error}")
    parts = [part for part in (error.context, error.problem) if part]
    message = f"{position(source, error.problem_mark)}: {'; '.join(parts)}"
    return ("<root>", "yaml-syntax", message)


def read_strict(text: str, source: str, key_types: bool = True) -> Any:
    try:
        return load_strict(text, source, key_types)
    except StrictYAMLError:
        raise
    except yaml.YAMLError as error:
        raise StrictYAMLError(source, [syntax_problem(source, error)]) from error


def nested(prefix: str, problems: list[Problem]) -> list[Problem]:
    return [
        (prefix if path == "<root>" else f"{prefix}.{path}", rule, message)
        for path, rule, message in problems
    ]
