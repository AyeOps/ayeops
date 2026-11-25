from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from typing import Any
from urllib.parse import urlsplit

from ayeops.i18n import DEFAULT_LANG, is_reserved, localized, pick, service_state
from ayeops.manifest import Manifest
from ayeops.offsite import store_service
from ayeops.portal.layout import Edge
from ayeops.registry import Module
from ayeops.render import FleetRender, compose_file_services, host_units

SCRAPE_TARGET = re.compile(r"^([a-z][a-z0-9_-]*):([0-9]{1,5})$")
URL = re.compile(r"^https?://")
UNDECLARED = "undeclared"
GROUP_OF_KIND = {"app": "compose", "undeclared": UNDECLARED, "missing": "missing"}


@dataclass(frozen=True)
class Node:
    id: str
    label: str
    sub: str
    kind: str
    host: str = ""
    module: str = ""
    live: bool = True
    red: str = ""
    ports: tuple[int, ...] = ()
    stack: str = ""
    state: str = ""

    @property
    def group(self) -> str:
        return self.module or self.stack or GROUP_OF_KIND.get(self.kind, self.kind)


@dataclass
class Graph:
    nodes: dict[str, Node]
    system: list[Edge]
    flows: list[Edge]
    depends: list[Edge] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    host_gaps: dict[str, list[str]] = field(default_factory=dict)

    def gap(self, host: str, reason: str) -> None:
        self.host_gaps.setdefault(host, []).append(reason)


def zone_of(manifest: Manifest, host: str) -> str:
    spec = manifest.hosts.get(host)
    if spec is None:
        return UNDECLARED
    return str(spec.get("zone") or spec.get("provider") or host)


def url_node(url: str, sub: str) -> Node:
    parts = urlsplit(url)
    label = (parts.netloc + parts.path).rstrip("/") or url
    return Node(f"url:{url}", label, sub, "external")


def ingress_routes(
    manifest: Manifest, modules: dict[str, Module], renders: FleetRender | None = None
) -> list[tuple[str, str, str, str]]:
    renders = renders or FleetRender(manifest, modules)
    routes = []
    for host in sorted(manifest.hosts):
        ingress = renders.settings(host).get("ingress")
        if not ingress:
            continue
        for route, target in sorted((ingress.get("routes") or {}).items()):
            service = str(target).rsplit(":", 1)[0]
            routes.append((host, route, service, f"https://{route}.{ingress['domain']}"))
    return routes


def published_ports(ports: list[Any]) -> tuple[int, ...]:
    found = []
    for spec in ports:
        if isinstance(spec, dict):
            if spec.get("published") is not None:
                found.append(int(str(spec["published"]).split("-")[0]))
            continue
        parts = str(spec).split("/")[0].split(":")
        if len(parts) >= 2 and parts[-2].split("-")[0].isdigit():
            found.append(int(parts[-2].split("-")[0]))
    return tuple(found)


def collect_nodes(
    manifest: Manifest, modules: dict[str, Module], renders: FleetRender | None = None
) -> dict[str, Node]:
    renders = renders or FleetRender(manifest, modules)
    nodes: dict[str, Node] = {}
    for host in sorted(manifest.hosts):
        for name, service in renders.compose(host)["services"].items():
            module = service["labels"]["io.ayeops.module"]
            node_id = f"{host}/{name}"
            ports = published_ports(list(service.get("ports") or []))
            nodes[node_id] = Node(
                node_id, name, f"{module} · {host}", "module", host, module, ports=ports
            )
        for name, service in sorted(compose_file_services(manifest, host).items()):
            node_id = f"{host}/{name}"
            ports = published_ports(list(service.get("ports") or []))
            stack = str(service["x-aye-stack"])
            nodes.setdefault(
                node_id,
                Node(node_id, name, f"{stack} · {host}", "app", host, ports=ports, stack=stack),
            )
        for name, unit in sorted(host_units(manifest, host).items()):
            node_id = f"{host}/{name}"
            nodes.setdefault(node_id, Node(node_id, name, f"{unit} · {host}", "app", host))
        settings = renders.settings(host)
        for url in (settings.get("probes") or {}).get("http") or []:
            nodes.setdefault(f"url:{url}", url_node(url, "probed URL"))
    for name, external in sorted(manifest.externals.items()):
        node_id = f"external:{name}"
        title = str(external.get("title") or name)
        nodes[node_id] = Node(node_id, title, str(external.get("url") or "external"), "external")
    for _, service in sorted(manifest.services.items()):
        for dependency in service.get("cloud") or []:
            name = str(dependency["name"])
            nodes.setdefault(f"cloud:{name}", Node(f"cloud:{name}", name, "cloud", "cloud"))
    for name, entry in sorted(manifest.services.items()):
        host = str(entry.get("host") or "")
        if not is_reserved(entry) or host not in manifest.hosts:
            continue
        node_id = f"{host}/{name}"
        state = service_state(entry)
        found = nodes.get(node_id)
        if found is None:
            nodes[node_id] = Node(node_id, name, f"{state} · {host}", "app", host, live=False)
        nodes[node_id] = replace(nodes[node_id], live=False, state=state)
    return nodes


def is_probe_job(job: dict[str, Any]) -> bool:
    return any(
        rule.get("target_label") == "__address__" and "replacement" in rule
        for rule in job.get("relabel_configs") or []
    )


def scrape_edges(
    manifest: Manifest,
    host: str,
    modules: dict[str, Module],
    problems: list[str],
    renders: FleetRender | None = None,
) -> list[Edge]:
    scrape = (renders or FleetRender(manifest, modules)).scrape(host)
    if scrape is None:
        return []
    edges = []
    for job in scrape["scrape_configs"]:
        if is_probe_job(job):
            continue
        for group in job.get("static_configs") or []:
            for target in group.get("targets") or []:
                if URL.match(str(target)):
                    continue
                match = SCRAPE_TARGET.match(str(target))
                if match is None:
                    problems.append(
                        f"hosts.{host}: scrape target {target!r} of job {job['job_name']}"
                        " is not <service>:<port>"
                    )
                    continue
                if match.group(1) == "vmagent":
                    continue
                edges.append(
                    Edge(
                        f"{host}/{match.group(1)}",
                        f"{host}/vmagent",
                        "scrape",
                        weak=True,
                        kind="scrape",
                    )
                )
    return edges


def system_edges(
    manifest: Manifest,
    modules: dict[str, Module],
    graph: Graph,
    renders: FleetRender | None = None,
) -> list[Edge]:
    renders = renders or FleetRender(manifest, modules)
    nodes = graph.nodes
    edges: list[Edge] = []
    for host in sorted(manifest.hosts):
        settings = renders.settings(host)
        edges.extend(scrape_edges(manifest, host, modules, graph.problems, renders))
        metrics = settings.get("metrics")
        if metrics:
            target = metrics.get("remote_write") or {}
            if "host" in target:
                central = f"{target['host']}/victoriametrics"
                edges.append(Edge(f"{host}/vmagent", central, "remote_write", kind="write"))
            elif "url" in target:
                url = str(target["url"])
                nodes.setdefault(f"url:{url}", url_node(url, "remote_write target"))
                edges.append(Edge(f"{host}/vmagent", f"url:{url}", "remote_write", kind="write"))
            elif metrics.get("role") == "server":
                local = f"{host}/victoriametrics"
                edges.append(Edge(f"{host}/vmagent", local, "remote_write", kind="write"))
        if "grafana" in settings:
            edges.append(Edge(f"{host}/grafana", f"{host}/victoriametrics", "query", kind="query"))
        target = (settings.get("offsite") or {}).get("target") or {}
        if "host" in target:
            store = f"{target['host']}/{store_service(manifest, str(target['host']))}"
            edges.append(Edge(f"{host}/offsite", store, "restic snapshots", kind="write"))
        elif "url" in target:
            url = str(target["url"])
            nodes.setdefault(f"url:{url}", url_node(url, "off-site repository"))
            edges.append(Edge(f"{host}/offsite", f"url:{url}", "restic snapshots", kind="write"))
        for url in (settings.get("probes") or {}).get("http") or []:
            edges.append(Edge(f"{host}/blackbox", f"url:{url}", "probe", kind="probe"))
        if "node" not in settings:
            graph.gap(host, "no node metrics: enable the node module")
    routes = ingress_routes(manifest, modules, renders)
    for host, route, service, _ in routes:
        target = f"{host}/{service}"
        if target not in nodes:
            reason = f"ingress route {route} targets {service}, which nothing on {host} runs"
            nodes[target] = Node(target, service, f"route {route}", "missing", host, red=reason)
            graph.gap(host, reason)
            edges.append(Edge(f"{host}/traefik", target, route, live=False, kind="route"))
            continue
        edges.append(Edge(f"{host}/traefik", target, route, kind="route"))
    for host, _, _, url in routes:
        for candidate in (url, f"{url}/"):
            if f"url:{candidate}" in nodes:
                edges.append(Edge(f"url:{candidate}", f"{host}/traefik", "https", kind="route"))
    return edges


def resolve_endpoint(nodes: dict[str, Node], endpoint: str) -> str | None:
    if endpoint.startswith("external:"):
        return endpoint if endpoint in nodes else None
    if URL.match(endpoint):
        return f"url:{endpoint}" if f"url:{endpoint}" in nodes else None
    if "/" in endpoint:
        return endpoint if endpoint in nodes and nodes[endpoint].kind != "missing" else None
    services = [
        node_id
        for node_id, node in nodes.items()
        if node.host and node.label == endpoint and node.kind in ("module", "app")
    ]
    if len(services) == 1:
        return services[0]
    if not services and f"cloud:{endpoint}" in nodes:
        return f"cloud:{endpoint}"
    return None


def unknown_endpoint(nodes: dict[str, Node], endpoint: str) -> str:
    matches = sorted(
        node_id
        for node_id, node in nodes.items()
        if node.host and node.label == endpoint and node.kind in ("module", "app")
    )
    if len(matches) > 1:
        return f"{endpoint!r} runs on several hosts, write one of {', '.join(matches)}"
    return (
        f"{endpoint!r} is not in the manifest: use <host>/<service>, a service name,"
        " external:<name> from externals, a probed URL or a cloud dependency"
    )


def dependency_edges(
    manifest: Manifest, nodes: dict[str, Node]
) -> tuple[list[tuple[str, str]], list[Edge]]:
    problems: list[tuple[str, str]] = []
    edges: list[Edge] = []
    for name, entry in sorted(manifest.services.items()):
        source = f"{entry.get('host', '')}/{name}"
        for number, dependency in enumerate(entry.get("depends_on") or []):
            target = resolve_endpoint(nodes, str(dependency))
            if target is None:
                path = f"services.{name}.depends_on.{number}"
                problems.append((path, unknown_endpoint(nodes, str(dependency))))
            elif source in nodes:
                edges.append(Edge(source, target, "depends on", kind="depends"))
    return problems, edges


def dependency_cycles(edges: list[Edge]) -> list[list[str]]:
    out: dict[str, list[str]] = {}
    for edge in edges:
        out.setdefault(edge.src, []).append(edge.dst)
    state: dict[str, int] = {}
    stack: list[str] = []
    cycles: list[list[str]] = []

    def visit(node: str) -> None:
        state[node] = 1
        stack.append(node)
        for target in out.get(node, []):
            if state.get(target) == 1:
                cycles.append([*stack[stack.index(target) :], target])
            elif target not in state:
                visit(target)
        stack.pop()
        state[node] = 2

    for node in sorted(out):
        if node not in state:
            visit(node)
    return cycles


def depends_on(edges: list[Edge], node_id: str) -> list[str]:
    return sorted({edge.dst for edge in edges if edge.src == node_id})


def affects(edges: list[Edge], node_id: str) -> list[str]:
    hit: set[str] = set()
    todo = [node_id]
    while todo:
        current = todo.pop()
        for edge in edges:
            if edge.dst == current and edge.src not in hit and edge.src != node_id:
                hit.add(edge.src)
                todo.append(edge.src)
    return sorted(hit)


def client_edges(
    manifest: Manifest, nodes: dict[str, Node]
) -> tuple[list[tuple[str, str]], list[Edge]]:
    problems: list[tuple[str, str]] = []
    edges: list[Edge] = []
    for name, entry in sorted(manifest.services.items()):
        target = f"{entry.get('host', '')}/{name}"
        for number, client in enumerate(entry.get("clients") or []):
            source = resolve_endpoint(nodes, str(client))
            if source is None:
                path = f"services.{name}.clients.{number}"
                problems.append((path, unknown_endpoint(nodes, str(client))))
            elif target in nodes:
                edges.append(Edge(source, target, "calls", kind="flow"))
    return problems, edges


def reference_findings(
    manifest: Manifest, modules: dict[str, Module], renders: FleetRender | None = None
) -> list[tuple[str, str, str]]:
    nodes = collect_nodes(manifest, modules, renders)
    flow_problems, _ = flow_edges(manifest, nodes)
    dependency_problems, dependencies = dependency_edges(manifest, nodes)
    findings = [(path, "flow-endpoint", message) for path, message in flow_problems]
    findings += [(path, "depends-known", message) for path, message in dependency_problems]
    client_problems, _ = client_edges(manifest, nodes)
    findings += [(path, "client-endpoint", message) for path, message in client_problems]
    findings += [
        ("services", "depends-cycle", "dependency cycle " + " -> ".join(cycle))
        for cycle in dependency_cycles(dependencies)
    ]
    return findings


def flow_edges(
    manifest: Manifest, nodes: dict[str, Node]
) -> tuple[list[tuple[str, str]], list[Edge]]:
    problems: list[tuple[str, str]] = []
    edges: list[Edge] = []
    for number, flow in enumerate(manifest.flows):
        ends: dict[str, str] = {}
        for side in ("from", "to"):
            endpoint = str(flow[side])
            resolved = resolve_endpoint(nodes, endpoint)
            if resolved is None:
                problems.append((f"flows.{number}.{side}", unknown_endpoint(nodes, endpoint)))
            else:
                ends[side] = resolved
        if len(ends) == 2:
            state = str(flow.get("state", "live"))
            label = flow.get("label") or ""
            edges.append(
                Edge(
                    ends["from"],
                    ends["to"],
                    pick(label, DEFAULT_LANG),
                    live=state == "live",
                    kind="flow",
                    texts=localized(label),
                    muted=state == "disabled",
                )
            )
    return problems, edges


def drift(nodes: dict[str, Node], edges: list[Edge]) -> list[str]:
    return [
        f"edge {edge.src} -> {edge.dst}: unknown node {end!r}"
        for edge in edges
        for end in (edge.src, edge.dst)
        if end not in nodes
    ]


def _strings(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if isinstance(item, str | int) and str(item)]


def add_discovered(manifest: Manifest, graph: Graph, report: dict[str, Any]) -> None:
    host = str(report.get("host") or "")
    if not host:
        graph.problems.append("discovered report without a host")
        return
    if host not in manifest.hosts:
        graph.gap(
            host, f"{UNDECLARED}: aye discover found host {host}, the manifest has no such host"
        )
    nodes = graph.nodes
    names = {node.label for node in nodes.values() if node.host == host and node.kind != "missing"}
    declared = {name for name, entry in manifest.services.items() if entry.get("host") == host}
    ports = {port for node in nodes.values() if node.host == host for port in node.ports}

    def undeclared(key: str, label: str, sub: str, reason: str) -> None:
        node_id = f"{host}/{key}"
        nodes.setdefault(node_id, Node(node_id, label, sub, UNDECLARED, host, red=reason))

    for project in report.get("compose") or []:
        if not isinstance(project, dict):
            continue
        name = str(project.get("project") or "")
        for service in _strings(project.get("services")):
            if service not in names:
                undeclared(
                    service,
                    service,
                    f"compose project {name}" if name else "compose",
                    f"{UNDECLARED}: compose service {service} runs on {host}, the manifest"
                    " does not declare it",
                )
    for unit in _strings(report.get("systemd")):
        short = unit.removesuffix(".service")
        if short not in names and short not in declared:
            undeclared(
                f"systemd:{short}",
                short,
                "systemd unit",
                f"{UNDECLARED}: systemd unit {unit} runs on {host},"
                " the manifest does not declare it",
            )
    for socket in report.get("listening") or []:
        if not isinstance(socket, dict) or not str(socket.get("port", "")).isdigit():
            continue
        port = int(socket["port"])
        if port in ports:
            continue
        process = str(socket.get("process") or "unknown process")
        undeclared(
            f"port:{port}",
            f":{port} {process}",
            "listening port",
            f"{UNDECLARED}: {process} listens on port {port} on {host}, the manifest"
            " does not publish it",
        )


def build_graph(
    manifest: Manifest,
    modules: dict[str, Module],
    discovered: list[dict[str, Any]] | None = None,
    renders: FleetRender | None = None,
) -> Graph:
    renders = renders or FleetRender(manifest, modules)
    graph = Graph(collect_nodes(manifest, modules, renders), [], [])
    graph.system = system_edges(manifest, modules, graph, renders)
    flow_problems, graph.flows = flow_edges(manifest, graph.nodes)
    dependency_problems, graph.depends = dependency_edges(manifest, graph.nodes)
    client_problems, clients = client_edges(manifest, graph.nodes)
    graph.flows += clients
    graph.problems += [f"{path}: {message}" for path, message in flow_problems]
    graph.problems += [f"{path}: {message}" for path, message in client_problems]
    graph.problems += [f"{path}: {message}" for path, message in dependency_problems]
    graph.problems += [
        "dependency cycle " + " -> ".join(cycle) for cycle in dependency_cycles(graph.depends)
    ]
    graph.problems += drift(graph.nodes, graph.system + graph.flows + graph.depends)
    for report in discovered or []:
        add_discovered(manifest, graph, report)
    return graph
