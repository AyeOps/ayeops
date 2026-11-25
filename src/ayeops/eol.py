from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

MAJOR = r"v?(\d+)(?=[._-]|$)"
MINOR = r"v?(\d+\.\d+)(?=[._-]|$)"
POSTGRES = r"([1-9]\d|9\.[0-6])(?=[._-]|$)"

DEBIAN_CODENAMES = {
    "stretch": "9",
    "buster": "10",
    "bullseye": "11",
    "bookworm": "12",
    "trixie": "13",
    "forky": "14",
}
UBUNTU_CODENAMES = {
    "bionic": "18.04",
    "focal": "20.04",
    "jammy": "22.04",
    "noble": "24.04",
    "oracular": "24.10",
    "plucky": "25.04",
    "questing": "25.10",
    "resolute": "26.04",
}


@dataclass(frozen=True)
class Rule:
    product: str
    cycle: str


IMAGES: dict[str, Rule] = {
    "postgres": Rule("postgresql", POSTGRES),
    "redis": Rule("redis", MINOR),
    "valkey/valkey": Rule("valkey", MINOR),
    "mariadb": Rule("mariadb", MINOR),
    "mysql": Rule("mysql", MINOR),
    "mongo": Rule("mongodb", MINOR),
    "node": Rule("nodejs", MAJOR),
    "python": Rule("python", MINOR),
    "nginx": Rule("nginx", MINOR),
    "nginxinc/nginx-unprivileged": Rule("nginx", MINOR),
    "traefik": Rule("traefik", MINOR),
    "grafana/grafana": Rule("grafana", MINOR),
    "grafana/grafana-oss": Rule("grafana", MINOR),
    "grafana/loki": Rule("grafana-loki", MINOR),
    "prom/prometheus": Rule("prometheus", MINOR),
    "alpine": Rule("alpine-linux", MINOR),
    "debian": Rule("debian", MAJOR),
    "ubuntu": Rule("ubuntu", MINOR),
    "golang": Rule("go", MINOR),
    "php": Rule("php", MINOR),
    "ruby": Rule("ruby", MINOR),
    "rabbitmq": Rule("rabbitmq", MINOR),
    "haproxy": Rule("haproxy", MINOR),
    "memcached": Rule("memcached", MINOR),
    "eclipse-temurin": Rule("eclipse-temurin", MAJOR),
    "caddy": Rule("caddy", MAJOR),
    "elasticsearch": Rule("elasticsearch", MINOR),
    "docker.elastic.co/elasticsearch/elasticsearch": Rule("elasticsearch", MINOR),
    "kibana": Rule("kibana", MINOR),
    "docker.elastic.co/kibana/kibana": Rule("kibana", MINOR),
    "quay.io/keycloak/keycloak": Rule("keycloak", MINOR),
    "tomcat": Rule("tomcat", MINOR),
}

BASES: tuple[tuple[re.Pattern[str], str, Mapping[str, str] | None], ...] = (
    (re.compile(r"(?:^|[._-])alpine(\d+\.\d+)(?=[._-]|$)"), "alpine-linux", None),
    (
        re.compile(rf"(?:^|[._-])({'|'.join(DEBIAN_CODENAMES)})(?=[._-]|$)"),
        "debian",
        DEBIAN_CODENAMES,
    ),
    (
        re.compile(rf"(?:^|[._-])({'|'.join(UBUNTU_CODENAMES)})(?=[._-]|$)"),
        "ubuntu",
        UBUNTU_CODENAMES,
    ),
)

HUB_PREFIXES = ("docker.io/", "index.docker.io/")


@dataclass(frozen=True)
class Component:
    name: str
    product: str
    cycle: str

    def as_json(self) -> dict[str, str]:
        return {"name": self.name, "product": self.product, "cycle": self.cycle}


def split_reference(reference: str) -> tuple[str, str]:
    name = reference.split("@", 1)[0]
    head, _, last = name.rpartition("/")
    repository, _, tag = last.partition(":")
    path = f"{head}/{repository}" if head else repository
    for prefix in HUB_PREFIXES:
        path = path.removeprefix(prefix)
    return path.removeprefix("library/"), tag


def cycle_of(rule: Rule, tag: str) -> str | None:
    match = re.match(rule.cycle, tag)
    return match.group(1) if match else None


def base_components(reference: str, tag: str) -> list[Component]:
    found = []
    for pattern, product, codenames in BASES:
        match = pattern.search(tag)
        if match is None:
            continue
        word = match.group(1)
        found.append(Component(reference, product, codenames[word] if codenames else word))
    return found


def image_components(reference: str) -> list[Component]:
    repository, tag = split_reference(reference)
    rule = IMAGES.get(repository)
    found = []
    if rule is not None:
        cycle = cycle_of(rule, tag)
        if cycle is not None:
            found.append(Component(reference, rule.product, cycle))
    for base in base_components(reference, tag):
        if all(base.product != known.product for known in found):
            found.append(base)
    return found


def tracked(
    images: Iterable[str], manual: Iterable[Mapping[str, Any]]
) -> tuple[list[Component], list[str]]:
    components: list[Component] = []
    unmapped: list[str] = []
    for image in sorted({image.split("@", 1)[0] for image in images}):
        found = image_components(image)
        if found:
            components.extend(found)
        else:
            unmapped.append(image)
    for entry in manual:
        component = Component(str(entry["name"]), str(entry["product"]), str(entry["cycle"]))
        if component not in components:
            components.append(component)
    return components, unmapped
