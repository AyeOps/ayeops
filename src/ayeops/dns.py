from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from ayeops.manifest import Manifest
from ayeops.registry import Module
from ayeops.render import host_settings

OWNER_COMMENT = "managed-by: ayeops"
TTL = 300
TOKEN_ENV = "AYE_CLOUDFLARE_DNS_API_TOKEN"
CLOUDFLARE_API = "https://api.cloudflare.com/client/v4"
NAME_TYPES = frozenset({"A", "AAAA", "CNAME"})


@dataclass(frozen=True, order=True)
class Record:
    name: str
    content: str
    type: str = "A"
    ttl: int = TTL
    proxied: bool = False
    comment: str = OWNER_COMMENT
    record_id: str = ""

    @property
    def owned(self) -> bool:
        return self.comment == OWNER_COMMENT

    def matches(self, other: Record) -> bool:
        return (self.content, self.ttl, self.proxied) == (other.content, other.ttl, other.proxied)

    def describe(self) -> str:
        mode = "proxied" if self.proxied else "dns only"
        return f"{self.content} (ttl {self.ttl}, {mode})"


@dataclass(frozen=True, order=True)
class Change:
    name: str
    action: str
    record: Record
    current: Record | None = None

    def __str__(self) -> str:
        if self.action == "create":
            return f"create {self.record.type} {self.name} -> {self.record.describe()}"
        if self.action == "update" and self.current is not None:
            return (
                f"update {self.record.type} {self.name} {self.current.describe()}"
                f" -> {self.record.describe()}"
            )
        if self.action == "delete":
            return f"delete {self.record.type} {self.name} {self.record.describe()}"
        return f"conflict {self.name}: a record without the comment {OWNER_COMMENT} exists"


def desired_records(manifest: Manifest, modules: dict[str, Module]) -> dict[str, list[Record]]:
    desired: dict[str, list[Record]] = {}
    for host in sorted(manifest.hosts):
        ingress = host_settings(manifest, host, modules).get("ingress")
        if ingress is None:
            continue
        domain = str(ingress["domain"])
        address = str(manifest.hosts[host]["address"])
        records = desired.setdefault(domain, [])
        records.extend(Record(f"{route}.{domain}", address) for route in ingress["routes"])
    return {domain: sorted(records) for domain, records in sorted(desired.items())}


def zone_candidates(domain: str) -> list[str]:
    labels = domain.split(".")
    return [".".join(labels[index:]) for index in range(len(labels) - 1)]


def in_domain(name: str, domain: str) -> bool:
    return name == domain or name.endswith(f".{domain}")


def plan(desired: list[Record], existing: list[Record], domain: str) -> list[Change]:
    scope = [record for record in existing if in_domain(record.name, domain)]
    owned = sorted(r for r in scope if r.owned and r.type == "A")
    foreign = {r.name for r in scope if not r.owned and r.type in NAME_TYPES}
    wanted = {record.name for record in desired}
    changes: list[Change] = []
    for record in desired:
        if record.name in foreign:
            changes.append(Change(record.name, "conflict", record))
            continue
        mine = [current for current in owned if current.name == record.name]
        if not mine:
            changes.append(Change(record.name, "create", record))
            continue
        current, *extra = mine
        if not record.matches(current):
            target = Record(record.name, record.content, record_id=current.record_id)
            changes.append(Change(record.name, "update", target, current))
        changes.extend(Change(r.name, "delete", r) for r in extra)
    changes.extend(Change(r.name, "delete", r) for r in owned if r.name not in wanted)
    return sorted(changes)


class DnsProvider(Protocol):
    def zone_id(self, domain: str) -> str: ...

    def records(self, zone_id: str) -> list[Record]: ...

    def apply(self, zone_id: str, change: Change) -> None: ...


class CloudflareError(RuntimeError):
    pass


Opener = Callable[[urllib.request.Request], Any]


def _open(request: urllib.request.Request) -> Any:
    return urllib.request.urlopen(request, timeout=30)


class Cloudflare:
    def __init__(self, token: str, opener: Opener = _open) -> None:
        self.token = token
        self.opener = opener

    def call(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        request = urllib.request.Request(
            CLOUDFLARE_API + path,
            data=None if body is None else json.dumps(body).encode(),
            method=method,
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
        )
        try:
            with self.opener(request) as response:
                payload: dict[str, Any] = json.load(response)
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", "replace")
            raise CloudflareError(f"{method} {path}: HTTP {error.code} {detail}") from error
        if not payload.get("success"):
            raise CloudflareError(f"{method} {path}: {payload.get('errors')}")
        return payload

    def zone_id(self, domain: str) -> str:
        for candidate in zone_candidates(domain):
            result = self.call("GET", f"/zones?name={urllib.parse.quote(candidate)}")["result"]
            if result:
                return str(result[0]["id"])
        raise CloudflareError(f"no Cloudflare zone for {domain}")

    def records(self, zone_id: str) -> list[Record]:
        records: list[Record] = []
        page = 1
        while True:
            payload = self.call("GET", f"/zones/{zone_id}/dns_records?per_page=100&page={page}")
            records.extend(
                Record(
                    name=item["name"],
                    content=item["content"],
                    type=item["type"],
                    ttl=int(item["ttl"]),
                    proxied=bool(item.get("proxied")),
                    comment=item.get("comment") or "",
                    record_id=item["id"],
                )
                for item in payload["result"]
            )
            if page >= int((payload.get("result_info") or {}).get("total_pages") or 1):
                return records
            page += 1

    def apply(self, zone_id: str, change: Change) -> None:
        record = change.record
        body = {
            "type": record.type,
            "name": record.name,
            "content": record.content,
            "ttl": record.ttl,
            "proxied": record.proxied,
            "comment": OWNER_COMMENT,
        }
        base = f"/zones/{zone_id}/dns_records"
        if change.action == "create":
            self.call("POST", base, body)
        elif change.action == "update":
            self.call("PUT", f"{base}/{record.record_id}", body)
        elif change.action == "delete":
            self.call("DELETE", f"{base}/{record.record_id}")


def sync(
    manifest: Manifest,
    modules: dict[str, Module],
    provider: DnsProvider,
    apply: bool,
    say: Callable[[str], None],
) -> int:
    desired = desired_records(manifest, modules)
    if not desired:
        say("no ingress routes, nothing to plan")
        return 0
    planned: list[tuple[str, list[Change]]] = []
    for domain, records in desired.items():
        zone = provider.zone_id(domain)
        changes = plan(records, provider.records(zone), domain)
        planned.append((zone, changes))
        if not changes:
            say(f"{domain}: up to date")
        for change in changes:
            say(f"{domain}: {change}")
    conflicts = [c for _, changes in planned for c in changes if c.action == "conflict"]
    if conflicts:
        say(f"{len(conflicts)} conflict(s): remove or rename those records first")
        return 1
    if not apply:
        return 0
    applied = 0
    for zone, changes in planned:
        for change in changes:
            provider.apply(zone, change)
            applied += 1
    say(f"{applied} change(s) applied")
    return 0
