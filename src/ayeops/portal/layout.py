from __future__ import annotations

import itertools
import math
import re
from collections.abc import Iterable
from dataclasses import dataclass, field

BOX_W = 248
BOX_H = 62
COL_GAP = 118
SLOT = 22
ROW_GAP = 18
BAND_HEAD = 40
BAND_PAD = 18
BAND_GAP = 16
HEAD_H = 34
PAD = 20
PORT_STEP = 12
SIDE_ARC = 56
DEFAULT_BAND = ""


@dataclass
class Edge:
    src: str
    dst: str
    label: str = ""
    weak: bool = False
    live: bool = True
    kind: str = "flow"
    back: bool = False
    hidden: bool = False
    via: list[str] = field(default_factory=list)
    ports: tuple[float, float] | None = None
    texts: dict[str, str] = field(default_factory=dict)
    muted: bool = False


@dataclass(frozen=True)
class Point:
    x: float
    y: float


@dataclass(frozen=True)
class Band:
    name: str
    y: float
    height: float


@dataclass
class Layout:
    pos: dict[str, Point]
    slots: dict[str, Point]
    width: float
    height: float
    order: list[str]
    rank: dict[str, int]
    columns: list[list[str]]
    bands: list[Band] = field(default_factory=list)
    heights: dict[str, float] = field(default_factory=dict)
    column_x: list[float] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.order

    def size(self, node: str) -> float:
        return self.heights.get(node, BOX_H)


def mark_back_edges(edges: list[Edge], order: list[str]) -> None:
    out: dict[str, list[Edge]] = {node: [] for node in order}
    for edge in edges:
        out[edge.src].append(edge)
    state: dict[str, int] = {}

    def visit(node: str) -> None:
        state[node] = 1
        for edge in out[node]:
            if edge.back:
                continue
            seen = state.get(edge.dst)
            if seen == 1:
                edge.back = True
            elif seen is None:
                visit(edge.dst)
        state[node] = 2

    for node in order:
        if node not in state:
            visit(node)


def assign_ranks(edges: list[Edge], order: list[str]) -> dict[str, int]:
    strong = [edge for edge in edges if not edge.weak]
    mark_back_edges(strong, order)
    forward = [edge for edge in strong if not edge.back]
    rank = dict.fromkeys(order, 0)
    for _ in order:
        changed = False
        for edge in forward:
            if rank[edge.dst] < rank[edge.src] + 1:
                rank[edge.dst] = rank[edge.src] + 1
                changed = True
        if not changed:
            break
    has_strong = {node for edge in strong for node in (edge.src, edge.dst)}
    weak_rank: dict[str, int] = {}
    for edge in edges:
        if edge.weak and edge.src not in has_strong:
            before = rank[edge.dst] - 1
            weak_rank[edge.src] = min(weak_rank.get(edge.src, before), before)
    rank.update(weak_rank)
    low = min(rank.values())
    return {node: value - low for node, value in rank.items()}


def fixed_ranks(edges: list[Edge], order: list[str], column_of: dict[str, int]) -> dict[str, int]:
    used = sorted({column_of.get(node, 0) for node in order})
    compact = {value: number for number, value in enumerate(used)}
    rank = {node: compact[column_of.get(node, 0)] for node in order}
    for edge in edges:
        edge.back = rank[edge.src] > rank[edge.dst]
    return rank


def _order(edges: list[Edge], ids: Iterable[str]) -> list[str]:
    order: list[str] = []
    seen: set[str] = set()
    for node in [n for edge in edges for n in (edge.src, edge.dst)] + list(ids):
        if node not in seen:
            seen.add(node)
            order.append(node)
    return order


def _sweep(
    column: list[str],
    segments: list[tuple[str, str]],
    index: dict[str, int],
    band: dict[str, int],
    up: bool,
) -> None:
    def barycenter(node: str) -> float:
        if up:
            near = [index[a] for a, b in segments if b == node]
        else:
            near = [index[b] for a, b in segments if a == node]
        return sum(near) / len(near) if near else float(index[node])

    keys = {node: (band[node], barycenter(node), index[node]) for node in column}
    column.sort(key=lambda node: keys[node])
    for position, node in enumerate(column):
        index[node] = position


def _slots(
    edges: list[Edge], rank: dict[str, int], band_index: dict[str, int]
) -> tuple[list[str], list[tuple[str, str]]]:
    virtual: list[str] = []
    segments: list[tuple[str, str]] = []
    for number, edge in enumerate(edges):
        edge.via = []
        if edge.hidden:
            continue
        a, b = rank[edge.src], rank[edge.dst]
        step = 1 if b >= a else -1
        between = list(range(a + step, b, step))
        previous = edge.src
        for count, level in enumerate(between):
            point = f"~{number}.{level}"
            virtual.append(point)
            rank[point] = level
            owner = edge.src if count < len(between) / 2 else edge.dst
            band_index[point] = band_index[owner]
            edge.via.append(point)
            segments.append((previous, point) if step > 0 else (point, previous))
            previous = point
        segments.append((previous, edge.dst) if step > 0 else (edge.dst, previous))
    return virtual, segments


def layout(
    edges: list[Edge],
    ids: Iterable[str] = (),
    band_of: dict[str, str] | None = None,
    bands: list[str] | None = None,
    column_of: dict[str, int] | None = None,
    heights: dict[str, float] | None = None,
    top: float = PAD,
) -> Layout:
    order = _order(edges, ids)
    heights = dict(heights or {})
    if not order:
        return Layout({}, {}, BOX_W + PAD * 2, BOX_H + PAD * 2, [], {}, [], heights=heights)
    band_of = band_of or {}
    names = list(bands or [])
    for node in order:
        name = band_of.get(node, DEFAULT_BAND)
        if name not in names:
            names.append(name)
    ranked = assign_ranks if column_of is None else None
    rank = ranked(edges, order) if ranked else fixed_ranks(edges, order, column_of or {})
    for edge in edges:
        if edge.weak and rank[edge.src] >= rank[edge.dst]:
            edge.hidden = True
    band_index = {node: names.index(band_of.get(node, DEFAULT_BAND)) for node in order}
    virtual, segments = _slots(edges, rank, band_index)

    columns: list[list[str]] = [[] for _ in range(max(rank.values()) + 1)]
    for node in order + virtual:
        columns[rank[node]].append(node)
    for column in columns:
        column.sort(key=lambda node: band_index[node])
    index = {node: position for column in columns for position, node in enumerate(column)}
    for column in columns[1:]:
        _sweep(column, segments, index, band_index, up=True)
    for column in reversed(columns[:-1]):
        _sweep(column, segments, index, band_index, up=False)
    for column in columns[1:]:
        _sweep(column, segments, index, band_index, up=True)

    slot_set = set(virtual)

    def size(node: str) -> float:
        return SLOT if node in slot_set else heights.get(node, BOX_H)

    def extent(members: list[str]) -> float:
        return sum(size(node) for node in members) + ROW_GAP * max(len(members) - 1, 0)

    labelled = len(names) > 1 or bool(names[0])
    head = BAND_HEAD if labelled else 0
    pad = BAND_PAD if labelled else 0
    column_x = [float(PAD + number * (BOX_W + COL_GAP)) for number in range(len(columns))]
    pos: dict[str, Point] = {}
    slots: dict[str, Point] = {}
    placed: list[Band] = []
    y = top
    for number, name in enumerate(names):
        cells = [[node for node in column if band_index[node] == number] for column in columns]
        inner = max(extent(cell) for cell in cells)
        if inner == 0:
            continue
        for column_number, cell in enumerate(cells):
            at = y + head + (inner - extent(cell)) / 2
            for node in cell:
                x = column_x[column_number]
                if node in slot_set:
                    slots[node] = Point(x, at + SLOT / 2)
                else:
                    pos[node] = Point(x, at)
                at += size(node) + ROW_GAP
        height = head + inner + pad
        placed.append(Band(name, y, height))
        y += height + BAND_GAP
    count = len(columns)
    width = PAD * 2 + count * BOX_W + (count - 1) * COL_GAP
    height = y - BAND_GAP + PAD
    plan = Layout(pos, slots, width, height, order, rank, columns, placed, heights, column_x)
    assign_ports(edges, plan)
    return plan


def sides(edge: Edge, plan: Layout) -> tuple[str, str]:
    a, b = plan.rank[edge.src], plan.rank[edge.dst]
    if a == b:
        return "r", "r"
    return ("r", "l") if b > a else ("l", "r")


def _next_y(edge: Edge, plan: Layout, start: bool) -> float:
    if edge.via:
        return plan.slots[edge.via[0] if start else edge.via[-1]].y
    other = edge.dst if start else edge.src
    return plan.pos[other].y + plan.size(other) / 2


def assign_ports(edges: list[Edge], plan: Layout) -> None:
    attached: dict[tuple[str, str], list[tuple[float, int, bool]]] = {}
    for number, edge in enumerate(edges):
        edge.ports = None
        if edge.hidden:
            continue
        out_side, in_side = sides(edge, plan)
        attached.setdefault((edge.src, out_side), []).append(
            (_next_y(edge, plan, True), number, True)
        )
        attached.setdefault((edge.dst, in_side), []).append(
            (_next_y(edge, plan, False), number, False)
        )
    start: dict[int, float] = {}
    end: dict[int, float] = {}
    for (node, _), items in attached.items():
        items.sort()
        room = max(plan.size(node) - 24, 0)
        step = min(PORT_STEP, room / max(len(items) - 1, 1))
        middle = plan.pos[node].y + plan.size(node) / 2
        for position, (_, number, outgoing) in enumerate(items):
            offset = (position - (len(items) - 1) / 2) * step
            (start if outgoing else end)[number] = middle + offset
    for number, edge in enumerate(edges):
        if number in start and number in end:
            edge.ports = (start[number], end[number])


def num(value: float) -> str:
    return f"{round(value, 2):g}"


def _hop(p: Point, q: Point) -> tuple[Point, Point]:
    direction = 1 if q.x >= p.x else -1
    k = max(30.0, abs(q.x - p.x) / 2)
    return Point(p.x + direction * k, p.y), Point(q.x - direction * k, q.y)


def edge_path(edge: Edge, plan: Layout) -> tuple[str, Point]:
    a = plan.pos[edge.src]
    b = plan.pos[edge.dst]
    middle_a = a.y + plan.size(edge.src) / 2
    middle_b = b.y + plan.size(edge.dst) / 2
    ys, ye = edge.ports or (middle_a, middle_b)
    out_side, in_side = sides(edge, plan)
    start = Point(a.x + (BOX_W if out_side == "r" else 0), ys)
    end = Point(b.x + (BOX_W if in_side == "r" else 0), ye)
    if out_side == in_side:
        x = start.x + SIDE_ARC
        d = (
            f"M{num(start.x)} {num(start.y)} C {num(x)} {num(start.y)} {num(x)} {num(end.y)}"
            f" {num(end.x)} {num(end.y)}"
        )
        return d, Point(x, (start.y + end.y) / 2)
    forward = out_side == "r"
    stops: list[tuple[Point, Point]] = []
    for point in edge.via:
        slot = plan.slots[point]
        enter = Point(slot.x if forward else slot.x + BOX_W, slot.y)
        leave = Point(slot.x + BOX_W if forward else slot.x, slot.y)
        stops.append((enter, leave))
    d = f"M{num(start.x)} {num(start.y)}"
    current = start
    for enter, leave in stops:
        c1, c2 = _hop(current, enter)
        d += f" C {num(c1.x)} {num(c1.y)} {num(c2.x)} {num(c2.y)} {num(enter.x)} {num(enter.y)}"
        d += f" C {num(enter.x)} {num(enter.y)} {num(leave.x)} {num(leave.y)}"
        d += f" {num(leave.x)} {num(leave.y)}"
        current = leave
    c1, c2 = _hop(current, end)
    d += f" C {num(c1.x)} {num(c1.y)} {num(c2.x)} {num(c2.y)} {num(end.x)} {num(end.y)}"
    if stops:
        enter, leave = stops[len(stops) // 2]
        return d, Point((enter.x + leave.x) / 2, enter.y)
    return d, Point(
        (current.x + 3 * c1.x + 3 * c2.x + end.x) / 8, (current.y + 3 * c1.y + 3 * c2.y + end.y) / 8
    )


def path_length(d: str) -> float:
    numbers = [float(value) for value in re.findall(r"-?[0-9]+(?:\.[0-9]+)?", d)]
    points = [Point(numbers[i], numbers[i + 1]) for i in range(0, len(numbers) - 1, 2)]
    if len(points) < 2:
        return 0.0
    polygon = sum(math.dist((a.x, a.y), (b.x, b.y)) for a, b in itertools.pairwise(points))
    ends = [points[0], *points[3::3]]
    chord = sum(math.dist((a.x, a.y), (b.x, b.y)) for a, b in itertools.pairwise(ends))
    return (polygon + chord) / 2
