from __future__ import annotations

from typing import Any

LANGS = ("en", "uk")
DEFAULT_LANG = "en"
RESERVED_STATES = ("planned", "disabled")


def pick(value: Any, lang: str) -> str:
    if not isinstance(value, dict):
        return "" if value is None else str(value)
    found = {str(key): str(text) for key, text in value.items() if text is not None and str(text)}
    for key in (lang, *LANGS):
        if key in found:
            return found[key]
    return next(iter(found.values()), "")


def localized(value: Any) -> dict[str, str]:
    return {lang: pick(value, lang) for lang in LANGS}


def plural_index(lang: str, count: int) -> int:
    if lang != "uk":
        return 0 if count == 1 else 1
    tens = count % 100
    ones = count % 10
    if ones == 1 and tens != 11:
        return 0
    if 2 <= ones <= 4 and not 12 <= tens <= 14:
        return 1
    return 2


def service_state(entry: dict[str, Any]) -> str:
    return str(entry.get("state") or "live")


def is_reserved(entry: dict[str, Any]) -> bool:
    return service_state(entry) in RESERVED_STATES
