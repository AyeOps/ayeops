from __future__ import annotations

from dataclasses import dataclass, field
from html import escape

from ayeops.i18n import LANGS
from ayeops.portal.graph import UNDECLARED, Graph, Node
from ayeops.portal.i18n import Text, as_text, attrs, both, lookup, same, text_attr, tr
from ayeops.portal.icons import LINK, SERVICE, is_mcp, svg_badge
from ayeops.portal.layout import (
    BOX_H,
    BOX_W,
    HEAD_H,
    PAD,
    Edge,
    Layout,
    Point,
    edge_path,
    layout,
    num,
    path_length,
)

TONES = ("data", "task", "serve", "ops", "human", "fail")
MODULE_TONE = {
    "metrics": "ops",
    "grafana": "ops",
    "node": "ops",
    "containers": "ops",
    "probes": "task",
    "ingress": "serve",
    "portal": "serve",
}
OUTSIDE = "outside"
SOURCES, COLLECTORS, INGRESS, DASHBOARDS, STORAGE = range(5)
ROLE_CLAIMS = {
    "write": (COLLECTORS, STORAGE),
    "scrape": (SOURCES, COLLECTORS),
    "probe": (COLLECTORS, SOURCES),
    "query": (DASHBOARDS, STORAGE),
    "route": (INGRESS, DASHBOARDS),
}
ROLE_PRIORITY = (INGRESS, STORAGE, COLLECTORS, DASHBOARDS, SOURCES)
BADGE_X = 31
BADGE_R = 17
TEXT_X = 60
TITLE_CHAR = 8.4
NOTE_CHAR = 6.9
LINE_H = 18
GROUP_HEAD = 52
GROUP_LINES = 10
DOT_SPACING = 150
DOT_SPEED = 70


def esc(value: object) -> str:
    return escape(str(value), quote=True)


def fit(text: str, room: float, char_width: float) -> str:
    limit = max(int(room // char_width), 2)
    if len(text) <= limit:
        return text
    cut = text[: limit - 1]
    space = cut.rfind(" ")
    if space > limit * 0.6:
        cut = cut[:space]
    return f"{cut.rstrip(' ,.;:·-')}…"


def fit_text(text: Text, room: float, char_width: float) -> Text:
    return {lang: fit(text[lang], room, char_width) for lang in LANGS}


def node_tone(node: Node) -> str:
    if node.red or node.kind in (UNDECLARED, "missing"):
        return "fail"
    if node.kind in ("external", "cloud", "group"):
        return "human"
    if node.kind == "app":
        return "data"
    return MODULE_TONE.get(node.module, "ops")


@dataclass
class Scene:
    key: str
    title: Text
    lead: Text
    edges: list[Edge]
    nodes: dict[str, Node]
    ids: list[str] = field(default_factory=list)
    band_of: dict[str, str] = field(default_factory=dict)
    bands: list[str] = field(default_factory=list)
    band_titles: dict[str, tuple[str, str, str]] = field(default_factory=dict)
    column_of: dict[str, int] | None = None
    column_titles: list[Text] = field(default_factory=list)
    lists: dict[str, list[str]] = field(default_factory=dict)


def bands_for(scene_nodes: list[Node], hosts: list[str]) -> tuple[dict[str, str], list[str]]:
    band_of = {node.id: node.host or OUTSIDE for node in scene_nodes}
    used = set(band_of.values())
    return band_of, [band for band in [OUTSIDE, *hosts] if band in used]


def system_scene(
    graph: Graph, hosts: list[str], band_titles: dict[str, tuple[str, str, str]]
) -> Scene:
    nodes = dict(graph.nodes)
    edges = [Edge(**vars(edge)) for edge in graph.system]
    probes: dict[str, list[str]] = {}
    for edge in edges:
        if edge.kind == "probe":
            probes.setdefault(edge.src, []).append(edge.dst)
    other = {
        end
        for edge in edges
        if edge.kind != "probe" and not (edge.kind == "route" and edge.src.startswith("url:"))
        for end in (edge.src, edge.dst)
    }
    lists: dict[str, list[str]] = {}
    folded: set[str] = set()
    for prober, targets in probes.items():
        bundle = [target for target in targets if target not in other]
        if len(bundle) < 2:
            continue
        group = f"probes:{prober}"
        nodes[group] = Node(group, "Probed URLs", f"{len(bundle)} checked by blackbox", "group")
        lists[group] = [nodes[target].label for target in bundle]
        folded.update(bundle)
        edges.append(
            Edge(
                prober,
                group,
                f"probe · {len(bundle)}",
                kind="probe",
                texts=tr("edge.probe", n=len(bundle)),
            )
        )
    edges = [edge for edge in edges if edge.src not in folded and edge.dst not in folded]
    ids = [
        node.id
        for node in nodes.values()
        if node.host
        and (node.kind in ("module", "missing") or node.state)
        and node.id not in folded
    ]
    column_of = roles(edges, [*ids, *(n for e in edges for n in (e.src, e.dst))])
    used = sorted(set(column_of.values()))
    scene_nodes = [nodes[n] for n in dict.fromkeys([*ids, *column_of])]
    band_of, bands = bands_for(scene_nodes, hosts)
    return Scene(
        "system",
        tr("scene.system"),
        tr("scene.system.lead"),
        edges,
        nodes,
        ids,
        band_of,
        bands,
        band_titles,
        column_of,
        [tr(f"role.{role}") for role in used],
        lists,
    )


def roles(edges: list[Edge], ids: list[str]) -> dict[str, int]:
    claims: dict[str, set[int]] = {node: set() for node in ids}
    for edge in edges:
        pair = ROLE_CLAIMS.get(edge.kind)
        if pair:
            claims[edge.src].add(pair[0])
            claims[edge.dst].add(pair[1])
    return {
        node: next((role for role in ROLE_PRIORITY if role in found), SOURCES)
        for node, found in claims.items()
    }


def flow_scene(
    key: str,
    title: Text,
    lead: Text,
    edges: list[Edge],
    graph: Graph,
    hosts: list[str],
    band_titles: dict[str, tuple[str, str, str]],
) -> Scene:
    copies = [Edge(**vars(edge)) for edge in edges]
    ends = list(dict.fromkeys(n for edge in copies for n in (edge.src, edge.dst)))
    band_of, bands = bands_for([graph.nodes[n] for n in ends], hosts)
    return Scene(key, title, lead, copies, dict(graph.nodes), [], band_of, bands, band_titles)


def reserved_pill(state: str) -> str:
    word = tr(f"state.{state}")
    width = max(len(word[lang]) for lang in LANGS) * 6.2 + 14
    x = BOX_W - width - 8
    return (
        f'<g class="pill" transform="translate({num(x)} -9)">'
        f'<rect width="{num(width)}" height="18" rx="9"/>'
        f'<text x="{num(width / 2)}" y="13">{both(word, "tspan")}</text></g>'
    )


def node_svg(
    node: Node, x: float, y: float, scraped: bool, href: str, icon: str, note: Text
) -> str:
    room = BOX_W - TEXT_X - 14
    tone = node_tone(node)
    if is_mcp(icon):
        note = {
            lang: text if text.startswith("MCP") else (f"MCP · {text}" if text else "MCP")
            for lang, text in note.items()
        }
    if node.red:
        line = same(node.red)
    elif any(note.values()):
        line = note
    else:
        line = lookup("sub", node.sub)
    scraped_tip = tr("diagram.scraped")
    mark = (
        f'<circle class="scraped" cx="{BOX_W - 12}" cy="12" r="4">'
        f"<title{text_attr(scraped_tip)}>{esc(scraped_tip['en'])}</title></circle>"
        if scraped and not node.state
        else ""
    )
    extra = " human" if tone == "human" else ""
    extra += " red" if node.red else ""
    extra += f" reserved {node.state}" if node.state else ""
    cy = BOX_H / 2
    tip = {lang: f"{node.label} · {line[lang]}" for lang in LANGS}
    box = (
        f'<g class="node{extra}" data-node="{esc(node.id)}" tabindex="0"'
        f' style="--c:var(--c-{tone})" transform="translate({num(x)} {num(y)})">'
        f"<title{text_attr(tip)}>{esc(tip['en'])}</title>"
        f'<rect class="box" width="{BOX_W}" height="{BOX_H}" rx="12"/>{mark}'
        f"{svg_badge(icon, node.label, BADGE_X, cy, BADGE_R)}"
        f'<text class="t" x="{TEXT_X}" y="{num(cy - 4)}">'
        f"{esc(fit(node.label, room, TITLE_CHAR))}</text>"
        f'<text class="s" x="{TEXT_X}" y="{num(cy + 14)}">'
        f"{both(fit_text(line, room, NOTE_CHAR), 'tspan')}</text>"
        f"{reserved_pill(node.state) if node.state else ''}"
        "</g>"
    )
    return f'<a href="{esc(href)}">{box}</a>' if href else box


def group_height(lines: list[str]) -> float:
    shown = min(len(lines), GROUP_LINES)
    return GROUP_HEAD + shown * LINE_H + (LINE_H if len(lines) > shown else 0) + 4


def group_svg(node: Node, x: float, y: float, lines: list[str], icon: str) -> str:
    room = BOX_W - 40
    shown = lines[:GROUP_LINES]
    rows = "".join(
        f'<text class="li" x="34" y="{num(GROUP_HEAD + 10 + i * LINE_H)}">'
        f"{esc(fit(text, room, NOTE_CHAR))}</text>"
        f'<circle class="bullet" cx="22" cy="{num(GROUP_HEAD + 6 + i * LINE_H)}" r="2.5"/>'
        for i, text in enumerate(shown)
    )
    if len(lines) > len(shown):
        rows += (
            f'<text class="li more" x="34" y="{num(GROUP_HEAD + 10 + len(shown) * LINE_H)}">'
            f"{both(tr('group.more', n=len(lines) - len(shown)), 'tspan')}</text>"
        )
    height = group_height(lines)
    title = tr("group.probes")
    tip = {lang: f"{title[lang]}: {', '.join(lines)}" for lang in LANGS}
    return (
        f'<g class="node group human" data-node="{esc(node.id)}" tabindex="0"'
        f' style="--c:var(--c-human)" transform="translate({num(x)} {num(y)})">'
        f"<title{text_attr(tip)}>{esc(tip['en'])}</title>"
        f'<rect class="box" width="{BOX_W}" height="{num(height)}" rx="12"/>'
        f"{svg_badge(icon, node.label, BADGE_X, 27, BADGE_R, letters=False)}"
        f'<text class="t" x="{TEXT_X}" y="24">{both(title, "tspan")}</text>'
        f'<text class="s" x="{TEXT_X}" y="41">'
        f"{both(tr('group.probes.sub', n=len(lines)), 'tspan')}</text>{rows}</g>"
    )


Box = tuple[float, float, float, float]


def overlaps(a: Box, b: Box) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def place_label(text: Text, middle: Point, taken: list[Box]) -> Text | None:
    fitting: dict[str, str] = {}
    boxes: list[Box] = []
    for lang in LANGS:
        width = len(text[lang]) * 6.4 + 12
        box = (middle.x - width / 2, middle.y - 17, middle.x + width / 2, middle.y + 1)
        if not any(overlaps(t, box) for t in taken):
            fitting[lang] = text[lang]
            boxes.append(box)
    taken.extend(boxes)
    if not fitting:
        return None
    return {lang: fitting.get(lang, "") for lang in LANGS}


def markers(key: str) -> str:
    return (
        f'<defs><marker id="{key}-a" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7"'
        ' markerHeight="7" orient="auto-start-reverse"><path d="M0 1 L9 5 L0 9 z"/></marker>'
        f'<marker id="{key}-p" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7"'
        ' markerHeight="7" orient="auto-start-reverse"><path class="plan" d="M0 1 L9 5 L0 9 z"/>'
        "</marker></defs>"
    )


def legend(tones: set[str], planned: bool, scraped: bool, reserved: bool = False) -> str:
    items = [
        f'<li><i style="--c:var(--c-{tone})"></i>{both(tr(f"legend.{tone}"))}</li>'
        for tone in TONES
        if tone in tones
    ]
    items.append(f'<li><i class="flow"></i>{both(tr("legend.flow"))}</li>')
    if planned:
        items.append(f'<li><i class="dash"></i>{both(tr("legend.planned"))}</li>')
    if reserved:
        items.append(f'<li><i class="box-dash"></i>{both(tr("legend.reserved"))}</li>')
    if scraped:
        items.append(f'<li><i style="--c:var(--c-ops)"></i>{both(tr("legend.scraped"))}</li>')
    return f'<ul class="legend">{"".join(items)}</ul>'


def bands_svg(scene: Scene, plan: Layout, icons: dict[str, str]) -> str:
    if len(plan.bands) < 2 and not (plan.bands and plan.bands[0].name):
        return ""
    parts = []
    for band in plan.bands:
        title, facts, icon = scene.band_titles.get(band.name, ("", "", LINK))
        title_text = as_text(title) if title else tr("band.outside")
        facts_text = as_text(facts) if title else tr("band.outside.facts")
        parts.append(
            f'<g class="band{" outside" if band.name == OUTSIDE else ""}">'
            f'<rect x="6" y="{num(band.y)}" width="{num(plan.width - 12)}"'
            f' height="{num(band.height)}" rx="16"/>'
            f'<use class="bico" href="#i-{esc(icon)}" x="20" y="{num(band.y + 11)}" width="16"'
            ' height="16"/>'
            f'<text class="bt" x="44" y="{num(band.y + 24)}">{both(title_text, "tspan")}'
            f'<tspan class="bf" dx="10">{both(facts_text, "tspan")}</tspan></text></g>'
        )
    return f'<g class="bands">{"".join(parts)}</g>'


def columns_svg(scene: Scene, plan: Layout) -> str:
    if not scene.column_titles:
        return ""
    return (
        '<g class="cols">'
        + "".join(
            f'<text x="{num(x + BOX_W / 2)}" y="{num(PAD + 14)}">{both(title, "tspan")}</text>'
            for x, title in zip(plan.column_x, scene.column_titles, strict=False)
        )
        + "</g>"
    )


def render(
    scene: Scene, links: dict[str, str], icons: dict[str, str], notes: dict[str, Text]
) -> str:
    key = scene.key
    heights = {node: group_height(lines) for node, lines in scene.lists.items()}
    top = PAD + (HEAD_H if scene.column_titles else 0)
    edges = scene.edges
    plan: Layout = layout(
        edges, scene.ids, scene.band_of, scene.bands, scene.column_of, heights, top
    )
    nodes = scene.nodes
    paths: list[str] = []
    dots: list[str] = []
    labels: list[str] = []
    tones = {node_tone(nodes[node_id]) for node_id in plan.pos}
    taken = [
        (p.x - 4, p.y - 4, p.x + BOX_W + 4, p.y + plan.size(n) + 4) for n, p in plan.pos.items()
    ]
    scraped = {edge.src for edge in edges if edge.hidden and edge.kind == "scrape"}
    planned = False
    for number, edge in enumerate(edges):
        if edge.hidden:
            continue
        animated = edge.live and nodes[edge.src].live and nodes[edge.dst].live
        planned = planned or not animated
        d, middle = edge_path(edge, plan)
        edge_id = f"{key}-e{number}"
        classes = [
            "edge",
            *([] if animated else ["dash"]),
            *(["weak"] if edge.weak else []),
            *(["muted"] if edge.muted else []),
        ]
        marker = f"{key}-a" if animated else f"{key}-p"
        paths.append(
            f'<g class="{" ".join(classes)}" data-a="{esc(edge.src)}" data-b="{esc(edge.dst)}">'
            f'<path id="{edge_id}" d="{d}" marker-end="url(#{marker})"/></g>'
        )
        if animated:
            length = path_length(d)
            count = max(1, round(length / DOT_SPACING))
            duration = max(length / DOT_SPEED, 1.0)
            for k in range(count):
                dots.append(
                    f'<circle r="3.4" class="dot{" pale" if edge.weak else ""}"'
                    f' data-e="{edge_id}">'
                    f'<animateMotion dur="{num(duration)}s" begin="-{num(k * duration / count)}s"'
                    f' repeatCount="indefinite"><mpath href="#{edge_id}"/></animateMotion></circle>'
                )
        if edge.label:
            shown = place_label(edge.texts or lookup("edge", edge.label), middle, taken)
            if shown:
                labels.append(
                    f'<text class="edge-lab" x="{num(middle.x)}" y="{num(middle.y - 5)}">'
                    f"{both(shown, 'tspan')}</text>"
                )
    boxes = "".join(
        group_svg(nodes[node_id], p.x, p.y, scene.lists[node_id], icons.get(node_id, LINK))
        if node_id in scene.lists
        else node_svg(
            nodes[node_id],
            p.x,
            p.y,
            node_id in scraped,
            links.get(node_id, ""),
            icons.get(node_id, SERVICE),
            notes.get(node_id, same("")),
        )
        for node_id, p in plan.pos.items()
    )
    empty = (
        f'<text class="empty" x="{num(plan.width / 2)}" y="{num(plan.height / 2 + 5)}">'
        f"{both(tr('diagram.empty'), 'tspan')}</text>"
        if plan.empty
        else ""
    )
    ratio = f"{num(plan.width)} / {num(plan.height)}"
    return (
        f'<section class="flow" id="{esc(key)}">'
        f'<div class="head"><h2>{both(scene.title)}</h2>'
        f'<p class="lead">{both(scene.lead)}</p></div>'
        f'<div class="frame" style="--w:{num(plan.width)}px;--h:{num(plan.height)}px;'
        f'--ar:{ratio}">'
        f'<div class="tools" hidden><b class="ttl">{both(scene.title)}</b>'
        '<button type="button" data-act="size" aria-pressed="false"'
        f"{attrs(title=tr('diagram.size.title'))}>{both(tr('diagram.size'))}</button>"
        '<button type="button" data-act="full" aria-pressed="false"'
        f"{attrs(title=tr('diagram.full.title'))}>{both(tr('diagram.full'))}</button></div>"
        '<div class="canvas"><svg class="diagram"'
        f' viewBox="0 0 {num(plan.width)} {num(plan.height)}"'
        f' role="img"{attrs(aria_label=scene.title)}>{markers(key)}'
        f"{bands_svg(scene, plan, icons)}{columns_svg(scene, plan)}"
        f'<g class="edges">{"".join(paths)}</g><g class="dots">{"".join(dots)}</g>'
        f'<g class="nodes">{boxes}</g><g class="labels">{"".join(labels)}</g>{empty}'
        "</svg></div>"
        f'<span class="hint" aria-hidden="true">{both(tr("diagram.hint"))}</span></div>'
        f"{legend(tones, planned, bool(scraped), any(nodes[n].state for n in plan.pos))}"
        "</section>"
    )
