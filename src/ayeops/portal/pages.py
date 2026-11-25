from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from functools import cache
from pathlib import Path
from typing import Any

from ayeops.alert_links import Board, dashboard_url, expr_metrics, pick_link, related_boards
from ayeops.checks import manifest_alert
from ayeops.i18n import LANGS, is_reserved, localized, service_state
from ayeops.manifest import Manifest
from ayeops.portal.brand import Brand, load_brand
from ayeops.portal.diagram import (
    MODULE_TONE,
    esc,
    flow_scene,
    node_tone,
    render,
    system_scene,
)
from ayeops.portal.graph import (
    UNDECLARED,
    URL,
    Graph,
    Node,
    affects,
    build_graph,
    depends_on,
    ingress_routes,
    zone_of,
)
from ayeops.portal.i18n import (
    Text,
    attrs,
    both,
    counted,
    i18n_json,
    join,
    lang_boot,
    lang_css,
    lookup,
    markup,
    same,
    searchable,
    tr,
)
from ayeops.portal.icons import (
    LINK,
    SERVICE,
    badge,
    brand_css,
    guess,
    host_icon,
    is_mcp,
    node_icons,
    sprite,
    use,
)
from ayeops.portal.live import live_json, live_spec
from ayeops.registry import Module, all_alerts
from ayeops.render import (
    ENV_REFERENCE,
    LOCALHOST,
    MODULE_RUNBOOK_BASE,
    FleetRender,
    compose_file_services,
    grafana_boards,
    grafana_host,
    grafana_root_url,
    runbook_url,
    sign_in,
)
from ayeops.rules import check_service
from ayeops.stamp import Stamp
from ayeops.waivers import WARN_DAYS as WAIVER_WARN_DAYS
from ayeops.waivers import (
    Waiver,
    days_left,
    host_waivers,
    parse_day,
    reason_of,
    service_waivers,
)

ASSETS = Path(__file__).parent / "assets"

LIGHT = {
    "bg": "#eef1f6",
    "stage": "#f8f9fc",
    "panel": "#ffffff",
    "zone": "#e7ecf3",
    "ink": "#132033",
    "mute": "#56647b",
    "line": "#c8d1de",
    "c-data": "#2563d9",
    "c-task": "#0f9284",
    "c-serve": "#cc650a",
    "c-ops": "#7449d1",
    "c-human": "#c2410c",
    "c-fail": "#cf3434",
    "s-live": "#15994f",
    "s-down": "#d42a2a",
    "s-none": "#8b97a8",
    "s-warn": "#b7791f",
}
DARK = {
    "bg": "#090f18",
    "stage": "#0f1724",
    "panel": "#162133",
    "zone": "#121c2b",
    "ink": "#e5ecf6",
    "mute": "#94a4bb",
    "line": "#2a3b52",
    "c-data": "#62a0ff",
    "c-task": "#34c9b7",
    "c-serve": "#ffa04a",
    "c-ops": "#ac8aff",
    "c-human": "#ff935a",
    "c-fail": "#ff6b6b",
    "s-live": "#3fd98a",
    "s-down": "#ff5c5c",
    "s-none": "#66758b",
    "s-warn": "#f2c14e",
}
FONTS = (
    '--f-display:system-ui,-apple-system,"Segoe UI",sans-serif;'
    '--f-body:system-ui,-apple-system,"Segoe UI",sans-serif;'
    '--f-mono:ui-monospace,SFMono-Regular,Menlo,Consolas,"Cascadia Mono",monospace'
)


SERVICES_PAGE = "services.html"
NO_LOGIN = ("none", "unknown")


class PortalDrift(Exception):
    def __init__(self, problems: list[str]) -> None:
        super().__init__(f"{len(problems)} portal problem(s)")
        self.problems = problems


@dataclass
class Card:
    host: str
    name: str
    source: Text
    description: Text
    status: tuple[str, Text]
    login: str
    urls: list[str] = field(default_factory=list)
    tunnels: list[tuple[str, str]] = field(default_factory=list)
    network: list[str] = field(default_factory=list)
    secrets: list[str] = field(default_factory=list)
    secret_where: Text = field(default_factory=lambda: same(""))
    alerts: list[dict[str, str]] = field(default_factory=list)
    dashboards: list[tuple[str, str]] = field(default_factory=list)
    tone: str = "data"
    gaps: list[Text] = field(default_factory=list)
    amber: Text | None = None
    depends: list[tuple[str, str]] = field(default_factory=list)
    affects: list[tuple[str, str]] = field(default_factory=list)
    public: list[str] = field(default_factory=list)
    icon: str = SERVICE
    kind: str = "module"
    state: str = "live"
    waivers: list[tuple[Waiver, int | None]] = field(default_factory=list)

    @property
    def anchor(self) -> str:
        return f"svc-{self.host}-{self.name}"

    @property
    def reserved(self) -> bool:
        return self.state != "live"


@cache
def asset(name: str) -> str:
    return (ASSETS / name).read_text(encoding="utf-8").replace("\r\n", "\n")


def tokens(values: dict[str, str]) -> str:
    return ";".join(f"--{name}:{value}" for name, value in values.items())


def theme_css() -> str:
    light = f":root{{{tokens(LIGHT)};{FONTS};color-scheme:light}}"
    dark = f"{tokens(DARK)};color-scheme:dark"
    return (
        f"{light}\n"
        f'@media (prefers-color-scheme:dark){{:root:not([data-theme="light"]){{{dark}}}}}\n'
        f':root[data-theme="dark"]{{{dark}}}\n'
        f"{lang_css()}\n"
    )


def env_names(environment: Any) -> list[str]:
    text = json.dumps(environment or {}, sort_keys=True)
    return sorted(set(ENV_REFERENCE.findall(text)))


def status_of(entry: dict[str, Any]) -> tuple[str, Text]:
    status = entry.get("status") or {}
    for key in ("public", "internal"):
        text = localized(reason_of(status.get(key)))
        if any(value.strip() for value in text.values()):
            return key, text
    return "none", same("")


def parse_port(spec: Any) -> tuple[str, int] | None:
    if isinstance(spec, dict):
        published = spec.get("published")
        if published is None:
            return None
        return str(spec.get("host_ip") or "0.0.0.0"), int(str(published).split("-")[0])
    parts = str(spec).split("/")[0].split(":")
    if len(parts) == 3:
        return parts[0], int(parts[1].split("-")[0])
    if len(parts) == 2:
        return "0.0.0.0", int(parts[0].split("-")[0])
    return None


def reach(card: Card, address: str, ports: list[Any]) -> None:
    for spec in ports:
        parsed = parse_port(spec)
        if parsed is None:
            continue
        bind, port = parsed
        if bind == LOCALHOST:
            card.tunnels.append(
                (f"ssh -N -L {port}:{LOCALHOST}:{port} {address}", f"http://{LOCALHOST}:{port}")
            )
        else:
            where = address if bind == "0.0.0.0" else bind
            card.network.append(f"{where}:{port}")


def portal_boards(
    manifest: Manifest, modules: dict[str, Module]
) -> tuple[str | None, tuple[Board, ...]]:
    settings = grafana_host(manifest, modules)
    if settings is None:
        return None, ()
    return grafana_root_url(settings), grafana_boards(manifest, modules, settings["grafana"])


def alert_specs(
    modules: dict[str, Module], entry: dict[str, Any]
) -> dict[str, tuple[str, dict[str, Any]]]:
    specs = {
        name: (module, manifest_alert(alert)[0])
        for name, (module, alert) in all_alerts(modules).items()
    }
    for alert in entry.get("alerts") or []:
        specs[str(alert["name"])] = ("", manifest_alert(alert)[0])
    return specs


def link_dashboards(
    card: Card,
    module: str,
    metrics: list[str],
    specs: dict[str, tuple[str, dict[str, Any]]],
    root: str | None,
    boards: tuple[Board, ...],
) -> None:
    if not root or not boards:
        return
    by_uid = {board.uid: board for board in boards}
    wanted = set(metrics)
    for alert in card.alerts:
        owner, spec = specs.get(alert["name"], ("", {}))
        wanted |= expr_metrics(str(spec.get("expr") or ""))
        link = pick_link(spec, owner, boards) if spec else None
        if link is not None:
            alert["dashboard"] = dashboard_url(root, by_uid[link.uid], card.host)
    card.dashboards = [
        (board.title, dashboard_url(root, board, card.host))
        for board in related_boards(sorted(wanted), module, boards)
    ]


def module_alerts(names: list[str], modules: dict[str, Module]) -> list[dict[str, str]]:
    known = all_alerts(modules)
    alerts = []
    for name in names:
        if name not in known:
            continue
        alert = known[name][1]
        alerts.append(
            {
                "name": name,
                "title": str(alert.get("title", name)),
                "severity": str(alert.get("severity", "")),
                "runbook": runbook_url(MODULE_RUNBOOK_BASE, f"{alert['runbook']}.md"),
            }
        )
    return alerts


def manifest_alerts(manifest: Manifest, entry: dict[str, Any]) -> list[dict[str, str]]:
    if is_reserved(entry):
        return []
    return [
        {
            "name": str(alert["name"]),
            "title": str(alert.get("title", alert["name"])),
            "severity": str(alert.get("severity", "")),
            "runbook": runbook_url(manifest.runbook_base_url, str(alert.get("runbook", ""))),
        }
        for alert in entry.get("alerts") or []
    ]


def user_gaps(manifest: Manifest, name: str, entry: dict[str, Any]) -> list[Text]:
    gaps = [same(finding.message) for finding in check_service(f"services.{name}", entry)]
    relative = [
        alert
        for alert in entry.get("alerts") or []
        if alert.get("runbook") and not URL.match(str(alert["runbook"]))
    ]
    if relative and not manifest.runbook_base_url and not is_reserved(entry):
        gaps.append(same("runbook_base_url is not set, so its runbook links lead nowhere"))
    return gaps


def stamp_day(stamp: Stamp) -> date:
    try:
        return date.fromisoformat(stamp.time[:10])
    except ValueError:
        return datetime.now(UTC).date()


def mark_waivers(card: Card, waivers: list[Waiver], today: date) -> None:
    card.waivers = [(item, days_left(item, today)) for item in waivers]
    for item, left in card.waivers:
        if left is None or left > WAIVER_WARN_DAYS:
            continue
        until = parse_day(item.until).isoformat()
        if left < 0:
            card.gaps.append(tr("card.waiver.expired", rule=item.rule, until=until))
        elif card.amber is None:
            card.amber = tr("card.waiver.ending", rule=item.rule, until=until)


def collect_cards(
    manifest: Manifest,
    modules: dict[str, Module],
    graph: Graph,
    today: date | None = None,
    renders: FleetRender | None = None,
) -> list[Card]:
    renders = renders or FleetRender(manifest, modules)
    day = today or datetime.now(UTC).date()
    routes: dict[tuple[str, str], list[str]] = {}
    for host, _, service, url in ingress_routes(manifest, modules, renders):
        routes.setdefault((host, service), []).append(url)
    cards: list[Card] = []
    root, boards = portal_boards(manifest, modules)
    for host in sorted(manifest.hosts):
        spec = manifest.hosts[host]
        address = str(spec.get("address") or host)
        rendered = renders.compose(host)["services"]
        settings = renders.settings(host)
        own = {
            name: entry for name, entry in manifest.services.items() if entry.get("host") == host
        }
        for name, service in sorted(rendered.items()):
            module = modules[service["labels"]["io.ayeops.module"]]
            entry = {**module.catalog.get(name, {}), **own.get(name, {})}
            card = Card(
                host,
                name,
                tr("source.module", name=module.name),
                localized(entry.get("description")),
                status_of(entry),
                sign_in(settings, name) or str(entry.get("login") or "none"),
                secret_where=tr("card.env", host=host),
                alerts=module_alerts(
                    list(module.catalog.get(name, {}).get("alerts") or []), modules
                )
                + manifest_alerts(manifest, own.get(name, {})),
                tone=MODULE_TONE.get(module.name, "ops"),
            )
            if name in own:
                card.gaps = user_gaps(manifest, name, own[name])
            waivers = service_waivers(name, own[name]) if name in own else []
            if module.name == "ingress":
                waivers += host_waivers(manifest, host)
            mark_waivers(card, waivers, day)
            card.urls = list(routes.get((host, name), []))
            card.public = [str(entry["url"])] if entry.get("url") else []
            reach(card, address, list(service.get("ports") or []))
            card.secrets = env_names(service.get("environment"))
            link_dashboards(
                card,
                module.name,
                [str(metric) for metric in entry.get("metrics") or []],
                alert_specs(modules, own.get(name, {})),
                root,
                boards,
            )
            cards.append(card)
        composed = compose_file_services(manifest, host)
        for name in sorted(set(own) | set(composed)):
            if name in rendered:
                continue
            entry = own.get(name, {})
            composed_service = composed.get(name, {})
            compose = str(composed_service.get("x-aye-compose") or "")
            stack = str(composed_service.get("x-aye-stack") or "")
            state = service_state(entry)
            source = tr("source.stack", name=stack) if stack else tr("source.outside")
            card = Card(
                host,
                name,
                source,
                localized(entry.get("description")),
                status_of(entry),
                str(entry.get("login") or "none"),
                secret_where=tr(
                    "card.env.next",
                    host=host,
                    compose=same(compose) if compose else tr("card.env.compose"),
                ),
                alerts=manifest_alerts(manifest, entry),
                kind="compose stack" if stack else "outside compose",
                state=state,
            )
            if entry:
                card.gaps = user_gaps(manifest, name, entry)
                if not is_reserved(entry):
                    mark_waivers(card, service_waivers(name, entry), day)
            else:
                card.gaps = [tr("card.uncatalogued", name=name)]
            card.urls = list(routes.get((host, name), []))
            card.public = [str(entry["url"])] if entry.get("url") else []
            reach(card, address, list(composed_service.get("ports") or []))
            card.secrets = env_names(composed_service.get("environment"))
            link_dashboards(
                card,
                "",
                [str(metric) for metric in entry.get("metrics") or []],
                alert_specs(modules, entry),
                root,
                boards,
            )
            cards.append(card)
    for node in sorted(graph.nodes.values(), key=lambda n: n.id):
        if node.kind == UNDECLARED:
            name = node.id.split("/", 1)[1]
            cards.append(
                Card(
                    node.host,
                    name,
                    same(node.sub),
                    same(""),
                    ("none", same("")),
                    "unknown",
                    gaps=[same(node.red)],
                    tone="fail",
                    kind=UNDECLARED,
                )
            )
    for card in cards:
        reachable = card.urls or card.public
        quiet = not card.gaps and not card.reserved
        if card.status[0] == "internal" and card.tunnels and not reachable and quiet:
            card.amber = card.amber or tr("card.amber")
    return cards


def state_pill(state: str) -> str:
    return f'<em class="pill {esc(state)}">{both(tr(f"state.{state}"))}</em>'


def chip(node: Node, href: str, icon: str, live: bool) -> str:
    red = " red" if node.red else ""
    red += f" reserved {node.state}" if node.state else ""
    tip = attrs(title=node.red) if node.red else ""
    if live:
        tip += f' data-svc="{esc(node.id)}"'
    dot = '<i class="st" aria-hidden="true"></i>'
    pill = state_pill(node.state) if node.state else ""
    inner = f"{badge(icon, node.label)}<span>{esc(node.label)}</span>{pill}{dot}"
    if href:
        return f'<a class="chip{red}" href="{esc(href)}"{tip}>{inner}</a>'
    return f'<span class="chip{red}"{tip}>{inner}</span>'


HOST_METRICS = ("cpu", "mem", "disk", "swap", "battery", "uptime")


SPARK = (
    '<svg class="spark" viewBox="0 0 80 22" preserveAspectRatio="none" aria-hidden="true"></svg>'
)


def metric_cells(items: tuple[str, ...], sparkline: str) -> str:
    return "".join(
        f'<div class="m" data-m="{key}" hidden><span>{both(tr(f"metric.{key}"))}</span><b></b>'
        f"{SPARK if key == sparkline else ''}</div>"
        for key in items
    )


def host_live(host: str) -> str:
    cells = metric_cells(HOST_METRICS, "cpu")
    return f'<div class="hlive" data-host="{esc(host)}" hidden>{cells}</div>'


def disks_block(host: str) -> str:
    return (
        f'<section class="disks" data-host="{esc(host)}">'
        f"<h4>{both(tr('disks.title'))}</h4>"
        f'<p class="disks-note">{both(tr("disks.waiting"))}</p>'
        '<div class="fs-list"></div><div class="smart-list"></div></section>'
    )


def zones_html(
    manifest: Manifest,
    graph: Graph,
    links: dict[str, str],
    icons: dict[str, str],
    live: set[str],
) -> str:
    zones: dict[str, list[str]] = {}
    for host in sorted(manifest.hosts):
        zones.setdefault(zone_of(manifest, host), []).append(host)
    blocks = []
    for zone, hosts in sorted(zones.items()):
        providers = sorted(
            {str(manifest.hosts[h]["provider"]) for h in hosts if manifest.hosts[h].get("provider")}
            - {zone}
        )
        host_blocks = []
        for host in hosts:
            spec = manifest.hosts[host]
            facts = " · ".join(
                str(v) for v in (spec["arch"], spec.get("address"), spec.get("memory")) if v
            )
            groups: dict[str, list[Node]] = {}
            for node in graph.nodes.values():
                if node.host == host:
                    group = node.state or (f"{node.stack} · compose" if node.stack else node.group)
                    groups.setdefault(group, []).append(node)
            stacks = "".join(
                f'<div class="stack" style="--c:var(--c-{node_tone(members[0])})">'
                f'<div class="stack-h">{both(lookup("state", group))}</div><div class="chips">'
                + "".join(
                    chip(n, links.get(n.id, ""), icons.get(n.id, SERVICE), n.id in live)
                    for n in sorted(members, key=lambda n: n.label)
                )
                + "</div></div>"
                for group, members in sorted(groups.items())
            )
            gaps = "".join(
                f'<p class="gap">{esc(reason)}</p>' for reason in graph.host_gaps.get(host, [])
            )
            red = " red" if gaps else ""
            mark = use(host_icon(host, spec))
            has_node = "node" in manifest.host_modules(host)
            live_parts = f"{host_live(host)}{disks_block(host)}" if has_node else ""
            host_blocks.append(
                f'<div class="host{red}"><div class="host-h"><h3>{mark}{esc(host)}</h3>'
                f"<span>{esc(facts)}</span></div>{gaps}"
                f"{live_parts}{stacks}</div>"
            )
        note = f'<div class="eyebrow">{esc(", ".join(providers))}</div>' if providers else ""
        blocks.append(f'<div class="zone">{note}<h2>{esc(zone)}</h2>{"".join(host_blocks)}</div>')
    return f'<div class="zones">{"".join(blocks)}</div>'


def link_chip(label: str, href: str) -> str:
    if href:
        return f'<a class="chip" href="{esc(href)}">{esc(label)}</a>'
    return f'<span class="chip">{esc(label)}</span>'


SERVICE_METRICS = ("status", "latency", "mem", "cpu")


def service_live(key: str) -> str:
    cells = metric_cells(SERVICE_METRICS, "latency")
    return f'<div class="slive" data-svc="{esc(key)}" hidden>{cells}</div>'


def row(label: str, body: str) -> str:
    return f'<div class="row"><span>{both(tr(label))}</span>{body}</div>'


def card_details(card: Card) -> tuple[str, list[Text]]:
    hints: list[Text] = []
    ways = "".join(
        row(
            kind,
            f'<div><a class="url" href="{esc(url)}" target="_blank" rel="noopener">'
            f"{esc(url)}</a></div>",
        )
        for kind, urls in (("card.public", card.public), ("card.internal", card.urls))
        for url in urls
    )
    rows = "".join(
        row(
            "card.tunnel",
            f'<div><code class="copy" tabindex="0"{attrs(title=tr("card.copy"))}>'
            f"{esc(command)}</code> <em>{both(tr('card.then', local=local))}</em></div>",
        )
        for command, local in card.tunnels
    )
    rows += "".join(row("card.network", f"<code>{esc(where)}</code>") for where in card.network)
    login = tr(f"card.login.{card.login}") if card.login in NO_LOGIN else same(card.login)
    rows += row("card.login", f"<em>{both(login)}</em>")
    for title, related in (("card.depends", card.depends), ("card.affects", card.affects)):
        if related:
            rows += row(
                title,
                '<div class="chips">'
                + "".join(link_chip(label, href) for label, href in related)
                + "</div>",
            )
    if card.tunnels:
        hints.append(tr("card.tunnel"))
    if card.secrets:
        rows += row(
            "card.secrets",
            f'<div class="secret"><em>{both(card.secret_where)}</em>'
            + "".join(f"<code>{esc(name)}</code>" for name in card.secrets)
            + "</div>",
        )
        hints.append(counted("count.secrets", len(card.secrets)))
    if card.dashboards:
        rows += row(
            "card.dashboards",
            '<div class="chips">'
            + "".join(link_chip(title, href) for title, href in card.dashboards)
            + "</div>",
        )
    runbook = both(tr("card.runbook"))
    dashboard = both(tr("card.dashboard"))
    alerts = "".join(
        f'<li><span class="sev {esc(a["severity"])}">'
        f"{esc(a['severity']) if a['severity'] else both(tr('card.severity.none'))}</span>"
        f"{esc(a['title'])}"
        + (
            f' <a href="{esc(a["runbook"])}" target="_blank" rel="noopener">{runbook}</a>'
            if a["runbook"]
            else ""
        )
        + (
            f' <a href="{esc(a["dashboard"])}" target="_blank" rel="noopener">{dashboard}</a>'
            if a.get("dashboard")
            else ""
        )
        + "</li>"
        for a in card.alerts
    )
    if alerts:
        rows += row("card.alerts", f'<ul class="alerts">{alerts}</ul>')
        hints.append(counted("count.alerts", len(card.alerts)))
    if card.waivers:
        rows += row("card.waivers", waivers_html(card.waivers))
    if len(card.gaps) > 1:
        rows += '<ul class="gaps">' + "".join(f"<li>{both(g)}</li>" for g in card.gaps) + "</ul>"
    return ways + rows, hints


def waiver_left(item: Waiver, left: int | None) -> str:
    if not item.dated:
        return f"<em>{both(tr('card.waiver.open'))}</em>"
    if left is None:
        return f"<em>{esc(str(item.until))}</em>"
    until = parse_day(item.until).isoformat()
    if left < 0:
        note = tr("js.waiver.over", n=-left)
    elif left == 0:
        note = tr("js.waiver.today")
    else:
        note = tr("js.waiver.left", n=left)
    return (
        f"<em>{both(tr('card.waiver.until', until=until))}</em>"
        f' <b class="left" data-until="{until}">{both(note)}</b>'
    )


def waivers_html(waivers: list[tuple[Waiver, int | None]]) -> str:
    items = "".join(
        f'<li><span class="sev">{esc(item.rule)}</span>{both(localized(item.reason))} '
        f"{waiver_left(item, left)}</li>"
        for item, left in waivers
    )
    return f'<ul class="alerts waivers">{items}</ul>'


def card_html(card: Card, live: bool) -> str:
    badge_key, reason = card.status
    details, hints = card_details(card)
    mcp = is_mcp(card.icon)
    state = " red" if card.gaps else " amber" if card.amber else ""
    state += f" reserved {card.state}" if card.reserved else ""
    search = searchable(
        card.host,
        card.name,
        card.source,
        card.description,
        badge_key,
        tr("filter.red") if card.gaps else "",
        "red" if card.gaps else "",
        "amber" if card.amber else "",
        tr(f"state.{card.state}") if card.reserved else "",
        *card.urls,
        *card.public,
        "mcp" if mcp else "",
        *card.secrets,
    ).lower()
    key = f"{card.host}/{card.name}"
    data = f' data-svc="{esc(key)}"' if live else ""
    target = (card.public or card.urls or [""])[0]
    button = (
        f'<a class="open" href="{esc(target)}" target="_blank" rel="noopener">'
        f"{both(tr('card.open'))}</a>"
        if target
        else ""
    )
    facts_html = "".join(f"<span>{both(fact)}</span>" for fact in (card.host, card.source))
    if card.reserved:
        facts_html += state_pill(card.state)
    else:
        facts_html += (
            f'<span class="fs {esc(badge_key)}"{attrs(title=reason)}>'
            f"{both(tr(f'status.{badge_key}'))}</span>"
        )
    if mcp:
        facts_html += '<span class="fs mcp">MCP</span>'
    problem = ""
    if card.gaps:
        problem = f'<p class="problem">{both(card.gaps[0])}</p>'
    elif card.amber:
        problem = f'<p class="problem amber-note">{both(card.amber)}</p>'
    elif card.reserved:
        problem = f'<p class="reserved-note">{both(tr("state.note"))}</p>'
    more = markup(
        {
            lang: esc(tr("card.details")[lang]) + "".join(f" · {esc(hint[lang])}" for hint in hints)
            for lang in LANGS
        }
    )
    described = any(card.description.values())
    return (
        f'<article class="svc{state}" id="{esc(card.anchor)}" style="--c:var(--c-{card.tone})"'
        f' data-search="{esc(search)}" data-host="{esc(card.host)}" data-kind="{esc(card.kind)}"'
        f' data-state="{esc(card.state)}">'
        f'<div class="svc-h">{badge(card.icon, card.name)}<div class="svc-t">'
        f'<h3{data}><span>{esc(card.name)}</span><i class="st" aria-hidden="true"></i></h3>'
        f'<div class="facts">{facts_html}</div></div>{button}</div>'
        + problem
        + (f'<p class="about">{both(card.description)}</p>' if described else "")
        + (service_live(key) if live else "")
        + f'<details class="more"><summary>{more}</summary>'
        f'<div class="more-b">{details}</div></details>' + "</article>"
    )


def tile(name: str, count: int | str, label: str, target: str) -> str:
    href = f"{SERVICES_PAGE}#filter={target}"
    return f'<a class="n {name}" href="{href}"><b>{count}</b> {both(tr(label))}</a>'


def summary(cards: list[Card], graph: Graph) -> str:
    red_cards = sum(1 for card in cards if card.gaps)
    red_hosts = len(graph.host_gaps)
    described = len(cards) - red_cards
    red = red_cards + red_hosts
    hosts = tr("summary.hosts", hosts=counted("count.hosts", red_hosts)) if red_hosts else same("")
    why = tr("summary.why", services=counted("count.services", red_cards), hosts=hosts)
    return (
        f'<div class="summary{" has-red" if red else ""}">'
        + tile("red", red, "tile.red", "red")
        + tile("all", len(cards), "tile.services", "all")
        + tile("ok", described, "tile.described", "described")
        + f'<span class="why">{both(why)}</span></div>'
        '<div class="live-strip" hidden>'
        + tile("down", 0, "tile.down", "down")
        + tile("live", 0, "tile.live", "live")
        + tile("nodata", 0, "tile.nodata", "nodata")
        + tile("red", red, "tile.red", "red")
        + f'<span class="why">{both(tr("summary.live"))}</span></div>'
    )


def quick_filter(group: str, value: str, label: Text, count: int, live: bool = False) -> str:
    pressed = "true" if (group, value) == ("filter", "all") else "false"
    hidden = " hidden" if live else ""
    return (
        f'<button type="button" class="qf" data-group="{group}" data-value="{esc(value)}"'
        f' aria-pressed="{pressed}"{hidden}>{both(label)} <b>{count}</b></button>'
    )


def quick_filters(cards: list[Card]) -> str:
    red = sum(1 for card in cards if card.gaps)
    states = [
        quick_filter("filter", "all", tr("filter.all"), len(cards)),
        quick_filter("filter", "red", tr("filter.red"), red),
        quick_filter("filter", "described", tr("filter.described"), len(cards) - red),
        quick_filter("filter", "down", tr("filter.down"), 0, live=True),
        quick_filter("filter", "nodata", tr("filter.nodata"), 0, live=True),
        quick_filter("filter", "live", tr("filter.live"), 0, live=True),
    ]
    for reserved in ("planned", "disabled"):
        count = sum(1 for card in cards if card.state == reserved)
        if count:
            states.append(quick_filter("filter", reserved, tr(f"filter.{reserved}"), count))
    rows = [("filters.status", states)]
    hosts = sorted({card.host for card in cards})
    rows.append(
        (
            "filters.host",
            [
                quick_filter("host", host, same(host), sum(1 for c in cards if c.host == host))
                for host in hosts
            ],
        )
    )
    kinds = sorted({card.kind for card in cards})
    if len(kinds) > 1:
        rows.append(
            (
                "filters.kind",
                [
                    quick_filter(
                        "kind", kind, lookup("kind", kind), sum(1 for c in cards if c.kind == kind)
                    )
                    for kind in kinds
                ],
            )
        )
    return (
        f'<div class="qfs" role="group"{attrs(aria_label=tr("filters.aria"))}>'
        + "".join(
            f'<div class="qf-row"><span class="qf-l">{both(tr(label))}</span>{"".join(items)}</div>'
            for label, items in rows
        )
        + "</div>"
    )


def services_html(manifest: Manifest, cards: list[Card], graph: Graph, live: set[str]) -> str:
    groups = []
    hosts = sorted(set(manifest.hosts) | set(graph.host_gaps) | {card.host for card in cards})
    for host in hosts:
        mine = [card for card in cards if card.host == host]
        host_gaps = graph.host_gaps.get(host, [])
        if not mine and not host_gaps:
            continue
        gaps = (
            '<ul class="gaps">' + "".join(f"<li>{esc(g)}</li>" for g in host_gaps) + "</ul>"
            if host_gaps
            else ""
        )
        eyebrow = join([same(zone_of(manifest, host)), counted("count.services", len(mine))], " · ")
        groups.append(
            f'<section class="group"><div class="head"><div class="eyebrow">'
            f"{both(eyebrow)}</div>"
            f"<h2>{esc(host)}</h2>{gaps}</div>"
            '<div class="cards">'
            + "".join(card_html(card, f"{card.host}/{card.name}" in live) for card in mine)
            + "</div></section>"
        )
    return (
        f'<div class="head"><h1>{both(tr("nav.services"))}</h1>'
        f'<p class="lead">{both(tr("services.lead"))}</p></div>'
        + summary(cards, graph)
        + '<div class="filter"><input id="q" type="search"'
        f"{attrs(placeholder=tr('search.placeholder'), aria_label=tr('search.aria'))}"
        ' autocomplete="off"></div>'
        + quick_filters(cards)
        + "".join(groups)
        + f'<p class="qf-empty" hidden>{both(tr("filter.empty"))} <button type="button"'
        f' class="qf-clear">{both(tr("filter.clear"))}</button></p>'
    )


def brand_mark(brand: Brand) -> str:
    if not brand.logo:
        return '<span class="mark"></span>'
    if not brand.logo_dark:
        return f'<img class="logo" src="{brand.logo}" alt="" height="28">'
    return (
        f'<img class="logo light" src="{brand.logo}" alt="" height="28">'
        f'<img class="logo dark" src="{brand.logo_dark}" alt="" height="28">'
    )


def lang_switch(locale: str) -> str:
    buttons = "".join(
        f'<button type="button" data-set-lang="{lang}" lang="{lang}"'
        f' aria-pressed="{"true" if lang == locale else "false"}"'
        f' title="{esc(tr(f"lang.name.{lang}")[lang])}">{esc(tr(f"lang.{lang}")[lang])}</button>'
        for lang in LANGS
    )
    return f'<div class="langs" role="group"{attrs(aria_label=tr("lang.aria"))}>{buttons}</div>'


def page(
    manifest: Manifest,
    title: str,
    active: str,
    body: str,
    stamp: Stamp,
    icons: set[str],
    live: str,
) -> str:
    current = ' aria-current="page"'
    nav = "".join(
        f'<a href="{href}"{current if key == active else ""}>{both(tr(label))}</a>'
        for href, label, key in (
            ("index.html", "nav.where", "where"),
            (SERVICES_PAGE, "nav.services", "services"),
        )
    )
    facts = []
    if stamp.time:
        facts.append(f"<span>{both(tr('footer.built', time=stamp.time))}</span>")
    if stamp.commit:
        facts.append(f"<span>{both(tr('footer.commit'))} <code>{esc(stamp.commit)}</code></span>")
    facts.append(f"<span>{both(tr('footer.generated'))}</span>")
    brand = load_brand(manifest)
    project = esc(brand.name)
    locale = manifest.portal_locale
    heading = tr(title)
    return (
        f'<!doctype html>\n<html lang="{locale}" data-lang="{locale}"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta name="robots" content="noindex, nofollow">'
        f"<title{attrs_text(heading, project)}>{esc(heading[locale])} · {project}</title>"
        f'<link rel="icon" href="{brand.favicon}">'
        f'<meta name="theme-color" content="{LIGHT["bg"]}" media="(prefers-color-scheme: light)">'
        f'<meta name="theme-color" content="{DARK["bg"]}" media="(prefers-color-scheme: dark)">'
        "<script>try{var t=localStorage.getItem('aye-theme');"
        "if(t)document.documentElement.dataset.theme=t}catch(e){}</script>"
        f"{lang_boot()}"
        f"<style>\n{theme_css()}{asset('style.css')}{brand_css(icons)}</style></head>\n"
        f'<body><header class="top"><a class="brand" href="index.html">{brand_mark(brand)}'
        f"{project}</a><nav>{nav}</nav>{lang_switch(locale)}"
        f'<button class="theme" type="button"{attrs(aria_label=tr("theme.aria"))}>'
        f"{both(tr('theme'))}</button>"
        "</header>\n"
        f"{sprite(icons)}\n"
        f'<main class="{active}">{body}</main>\n'
        f"<footer>{''.join(facts)}</footer>\n"
        f'<script type="application/json" id="aye-live">{live}</script>\n'
        f'<script type="application/json" id="aye-i18n">{i18n_json()}</script>\n'
        f"<script>\n{asset('client.js')}\n{asset('live.js')}</script></body></html>\n"
    )


def attrs_text(heading: Text, project: str) -> str:
    blob = {lang: f"{heading[lang]} · {project}" for lang in LANGS}
    return f' data-i18n-text="{esc(json.dumps(blob, ensure_ascii=False, sort_keys=True))}"'


def portal_pages(
    manifest: Manifest,
    modules: dict[str, Module],
    stamp: Stamp,
    discovered: list[dict[str, Any]] | None = None,
    renders: FleetRender | None = None,
) -> dict[str, str]:
    renders = renders or FleetRender(manifest, modules)
    graph = build_graph(manifest, modules, discovered, renders)
    if graph.problems:
        raise PortalDrift(graph.problems)
    cards = collect_cards(manifest, modules, graph, stamp_day(stamp), renders)
    for card in cards:
        node_id = f"{card.host}/{card.name}"
        if card.gaps and node_id in graph.nodes and not graph.nodes[node_id].red:
            first = card.gaps[0]["en"]
            graph.nodes[node_id] = replace(graph.nodes[node_id], red=first)
    links = {f"{card.host}/{card.name}": f"services.html#{card.anchor}" for card in cards}

    def named(node_ids: list[str]) -> list[tuple[str, str]]:
        return [(graph.nodes[n].label, links.get(n, "")) for n in node_ids]

    for card in cards:
        node_id = f"{card.host}/{card.name}"
        card.depends = named(depends_on(graph.depends, node_id))
        card.affects = named(affects(graph.depends, node_id))
    icons = all_icons(manifest, modules, graph, renders)
    for card in cards:
        card.icon = icons.get(f"{card.host}/{card.name}", SERVICE)
    keys = [
        (card.host, card.name)
        for card in cards
        if getattr(graph.nodes.get(f"{card.host}/{card.name}"), "kind", "") != UNDECLARED
        and not card.reserved
    ]
    live = {f"{host}/{name}" for host, name in keys}
    red = sum(1 for card in cards if card.gaps) + len(graph.host_gaps)
    blob = live_json(live_spec(manifest, modules, keys, red, renders))
    used = {
        *icons.values(),
        *(host_icon(h, manifest.hosts[h]) for h in manifest.hosts),
        LINK,
    }
    hosts = sorted(manifest.hosts)
    band_titles = {
        host: (
            host,
            " · ".join(
                str(v)
                for v in (
                    manifest.hosts[host]["arch"],
                    zone_of(manifest, host),
                    manifest.hosts[host].get("memory"),
                )
                if v
            ),
            host_icon(host, manifest.hosts[host]),
        )
        for host in hosts
    }
    notes: dict[str, Text] = {
        f"{card.host}/{card.name}": card.description
        for card in cards
        if any(card.description.values())
    }
    for node_id, node in graph.nodes.items():
        if node.kind in ("external", "cloud"):
            notes.setdefault(node_id, lookup("sub", node.sub))
    body = (
        f'<div class="head"><div class="eyebrow">{esc(manifest.project)}</div>'
        f'<h1>{both(tr("nav.where"))}</h1><p class="lead">{both(tr("where.lead"))}</p></div>'
        + summary(cards, graph)
        + zones_html(manifest, graph, links, icons, live)
        + render(system_scene(graph, hosts, band_titles), links, icons, notes)
    )
    if graph.depends:
        body += render(
            flow_scene(
                "depends",
                tr("scene.depends"),
                tr("scene.depends.lead"),
                graph.depends,
                graph,
                hosts,
                band_titles,
            ),
            links,
            icons,
            notes,
        )
    if graph.flows:
        body += render(
            flow_scene(
                "flows",
                tr("scene.flows"),
                tr("scene.flows.lead"),
                graph.flows,
                graph,
                hosts,
                band_titles,
            ),
            links,
            icons,
            notes,
        )
    services = services_html(manifest, cards, graph, live)
    return {
        "index.html": page(manifest, "nav.where", "where", body, stamp, used, blob),
        "services.html": page(manifest, "nav.services", "services", services, stamp, used, blob),
    }


def all_icons(
    manifest: Manifest,
    modules: dict[str, Module],
    graph: Graph,
    renders: FleetRender | None = None,
) -> dict[str, str]:
    icons = node_icons(manifest, modules, renders)
    for node_id, node in graph.nodes.items():
        if node_id in icons:
            continue
        if node.kind == "external":
            icons[node_id] = guess(node.label) or LINK
        else:
            icons[node_id] = SERVICE
    return icons
