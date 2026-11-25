from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from ayeops.i18n import LANGS, pick

REQUIRES = "ayeopsRequires"


def is_text(node: Any) -> bool:
    return (
        isinstance(node, dict)
        and bool(node)
        and set(node) <= set(LANGS)
        and all(isinstance(text, str) for text in node.values())
    )


def localize(node: Any, lang: str) -> Any:
    if is_text(node):
        return pick(node, lang)
    if isinstance(node, dict):
        return {key: localize(value, lang) for key, value in node.items()}
    if isinstance(node, list):
        return [localize(value, lang) for value in node]
    return node


def requirement_met(requirement: str, hosts: Iterable[dict[str, Any]]) -> bool:
    module, _, path = requirement.partition(".")
    for settings in hosts:
        if module not in settings:
            continue
        value: Any = settings[module]
        for part in path.split(".") if path else []:
            value = value.get(part) if isinstance(value, dict) else None
        if not path or value:
            return True
    return False


def wanted(item: dict[str, Any], met: Callable[[str], bool]) -> bool:
    return all(met(str(requirement)) for requirement in item.get(REQUIRES) or [])


def without_requires(item: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in item.items() if key != REQUIRES}


def is_row(panel: dict[str, Any]) -> bool:
    return panel.get("type") == "row"


def prune_panels(panels: list[dict[str, Any]], met: Callable[[str], bool]) -> list[dict[str, Any]]:
    kept = [without_requires(panel) for panel in panels if wanted(panel, met)]
    return [
        panel
        for index, panel in enumerate(kept)
        if not is_row(panel) or (index + 1 < len(kept) and not is_row(kept[index + 1]))
    ]


def build(data: dict[str, Any], lang: str, met: Callable[[str], bool]) -> dict[str, Any] | None:
    if not wanted(data, met):
        return None
    board = without_requires(data)
    panels = list(board.get("panels") or [])
    board["panels"] = prune_panels(panels, met)
    if panels and not board["panels"]:
        return None
    result: dict[str, Any] = localize(board, lang)
    return result
