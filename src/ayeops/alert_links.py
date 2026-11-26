from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Any

DASHBOARD_UID = "__dashboardUid__"
PANEL_ID = "__panelId__"
WORD = re.compile(r"[A-Za-z_:][A-Za-z0-9_:]*")
QUOTED = re.compile(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|`[^`]*`')
MATCHERS = re.compile(r"\{[^{}]*\}")
GROUPING = re.compile(
    r"(?<![A-Za-z0-9_:])(by|without|on|ignoring|group_left|group_right)\s*\([^()]*\)"
)
KEYWORDS = frozenset(
    {
        "and",
        "or",
        "unless",
        "bool",
        "offset",
        "inf",
        "nan",
        "by",
        "without",
        "on",
        "ignoring",
        "group_left",
        "group_right",
    }
)
GLUED = frozenset("$._")
SELECTOR = re.compile(r"([A-Za-z_:][A-Za-z0-9_:]*)\s*\{([^{}]*)\}")
EQUALS = re.compile(r'([A-Za-z_][A-Za-z0-9_]*)\s*=\s*"((?:\\.|[^"\\])*)"')


def expr_metrics(expr: str) -> frozenset[str]:
    text = QUOTED.sub(" ", expr)
    text = MATCHERS.sub(" ", text)
    text = GROUPING.sub(" ", text)
    found = set()
    for match in WORD.finditer(text):
        word = match.group()
        before = text[match.start() - 1] if match.start() else " "
        if word.lower() in KEYWORDS or before.isalnum() or before in GLUED:
            continue
        if text[match.end() :].lstrip().startswith("("):
            continue
        found.add(word)
    return frozenset(found)


def fixed_labels(expr: str) -> frozenset[tuple[str, str, str]]:
    return frozenset(
        (selector.group(1), label, value)
        for selector in SELECTOR.finditer(expr)
        for label, value in EQUALS.findall(selector.group(2))
        if "$" not in value
    )


def compatible(
    metric: str, wanted: frozenset[tuple[str, str, str]], labels: frozenset[tuple[str, str, str]]
) -> bool:
    drawn: dict[str, set[str]] = {}
    for name, label, value in labels:
        if name == metric:
            drawn.setdefault(label, set()).add(value)
    return all(
        value in drawn[label] for name, label, value in wanted if name == metric and label in drawn
    )


@dataclass(frozen=True)
class Panel:
    id: int
    title: str
    metrics: frozenset[str]
    labels: frozenset[tuple[str, str, str]] = frozenset()


@dataclass(frozen=True)
class Board:
    uid: str
    title: str
    module: str
    panels: tuple[Panel, ...]
    variables: frozenset[str] = frozenset()

    def panel(self, panel_id: int) -> Panel | None:
        return next((panel for panel in self.panels if panel.id == panel_id), None)


@dataclass(frozen=True)
class Link:
    uid: str
    title: str
    panel: int | None = None


def walk_panels(panels: Iterable[dict[str, Any]]) -> Iterator[dict[str, Any]]:
    for panel in panels:
        yield panel
        yield from walk_panels(panel.get("panels") or [])


def panel_metrics(panel: dict[str, Any]) -> frozenset[str]:
    return frozenset().union(
        *(expr_metrics(str(target.get("expr") or "")) for target in panel.get("targets") or [])
    )


def panel_labels(panel: dict[str, Any]) -> frozenset[tuple[str, str, str]]:
    return frozenset().union(
        *(fixed_labels(str(target.get("expr") or "")) for target in panel.get("targets") or [])
    )


def board_from(module: str, data: dict[str, Any]) -> Board:
    panels = tuple(
        Panel(
            int(panel["id"]),
            str(panel.get("title") or ""),
            panel_metrics(panel),
            panel_labels(panel),
        )
        for panel in walk_panels(data.get("panels") or [])
        if panel.get("type") != "row" and isinstance(panel.get("id"), int)
    )
    variables = frozenset(
        str(variable.get("name"))
        for variable in (data.get("templating") or {}).get("list") or []
        if variable.get("name")
    )
    return Board(str(data["uid"]), str(data.get("title") or data["uid"]), module, panels, variables)


def explicit_target(value: Any) -> tuple[str, int | None]:
    if isinstance(value, dict):
        panel = value.get("panel")
        return str(value["uid"]), int(panel) if panel is not None else None
    return str(value), None


def pick_link(alert: dict[str, Any], module: str, boards: Iterable[Board]) -> Link | None:
    ordered = sorted(boards, key=lambda board: board.uid)
    explicit = alert.get("dashboard")
    if explicit is False:
        return None
    expr = str(alert.get("expr") or "")
    if not explicit:
        return guess_link(expr, module, ordered)
    uid, panel_id = explicit_target(explicit)
    board = next((board for board in ordered if board.uid == uid), None)
    if board is None:
        return None
    panel = board.panel(panel_id) if panel_id is not None else None
    if panel is not None:
        return Link(board.uid, board.title, panel.id)
    guessed = guess_link(expr, module, [board])
    if guessed is not None:
        return guessed
    if board.panels:
        return Link(board.uid, board.title, board.panels[0].id)
    return None


def guess_link(expr: str, module: str, ordered: list[Board]) -> Link | None:
    wanted = expr_metrics(expr)
    fixed = fixed_labels(expr)
    pinned = {(name, label) for name, label, _ in fixed}
    best: tuple[tuple[bool, int, int, int, int], Link] | None = None
    for board_index, board in enumerate(ordered):
        for panel_index, panel in enumerate(board.panels):
            usable = {
                metric
                for metric in wanted & panel.metrics
                if compatible(metric, fixed, panel.labels)
            }
            if not usable:
                continue
            extra = sum(
                1
                for name, label, _ in panel.labels
                if name in usable and (name, label) not in pinned
            )
            key = (board.module != module, -len(usable), extra, board_index, panel_index)
            if best is None or key < best[0]:
                best = (key, Link(board.uid, board.title, panel.id))
    return best[1] if best else None


def link_annotations(link: Link | None) -> dict[str, str]:
    if link is None or link.panel is None:
        return {}
    return {DASHBOARD_UID: link.uid, PANEL_ID: str(link.panel)}


def dashboard_url(root: str, board: Board, host: str | None = None) -> str:
    url = f"{root.rstrip('/')}/d/{board.uid}"
    if host and "host" in board.variables:
        url += f"?var-host={host}"
    return url


def related_boards(
    metrics: Iterable[str], module: str, boards: Iterable[Board], limit: int = 3
) -> list[Board]:
    wanted = frozenset(metrics)
    scored = []
    for board in sorted(boards, key=lambda board: board.uid):
        shared = len(wanted & frozenset().union(*(panel.metrics for panel in board.panels)))
        if shared:
            scored.append(((board.module != module, -shared, board.uid), board))
    return [board for _, board in sorted(scored, key=lambda item: item[0])[:limit]]


def dashboard_problems(
    alerts: Iterable[tuple[str, dict[str, Any]]], known: dict[str, frozenset[int]]
) -> list[tuple[str, str, str]]:
    problems = []
    for path, alert in alerts:
        value = alert.get("dashboard")
        if not value:
            continue
        uid, panel = explicit_target(value)
        where = f"{path}.dashboard"
        if uid not in known:
            problems.append((where, "alert-dashboard", f"no dashboard with uid {uid!r}"))
        elif panel is not None and panel not in known[uid]:
            problems.append(
                (where, "alert-dashboard", f"dashboard {uid!r} has no panel with id {panel}")
            )
    return problems
