from __future__ import annotations

import io
import json
import urllib.request
from typing import Any

import pytest

from ayeops.cli import dns, main
from ayeops.dns import (
    OWNER_COMMENT,
    TOKEN_ENV,
    Change,
    Cloudflare,
    Record,
    desired_records,
    plan,
    sync,
    zone_candidates,
)
from ayeops.manifest import load_manifest
from ayeops.registry import Module
from tests.conftest import EXAMPLE, manifest_from

DOMAIN = "ops.acme.example"


def owned(name: str, content: str, record_id: str = "r1", **fields: Any) -> Record:
    return Record(f"{name}.{DOMAIN}", content, record_id=record_id, **fields)


def foreign(name: str, content: str, kind: str = "A") -> Record:
    return Record(f"{name}.{DOMAIN}", content, type=kind, comment="", record_id="x")


def actions(changes: list[Change]) -> list[tuple[str, str]]:
    return [(change.action, change.name) for change in changes]


def test_every_route_on_every_host_wants_an_a_record_to_the_host_address(
    modules: dict[str, Module],
) -> None:
    desired = desired_records(load_manifest(EXAMPLE), modules)
    assert desired == {
        DOMAIN: [
            Record(f"auth.{DOMAIN}", "10.8.0.1"),
            Record(f"grafana.{DOMAIN}", "10.8.0.1"),
            Record(f"hub.{DOMAIN}", "10.8.0.1"),
            Record(f"metrics.{DOMAIN}", "10.8.0.1"),
            Record(f"oncall.{DOMAIN}", "10.8.0.1"),
            Record(f"portal.{DOMAIN}", "10.8.0.1"),
        ]
    }


def test_records_are_dns_only_with_ttl_300_and_marked_as_owned() -> None:
    record = Record(f"grafana.{DOMAIN}", "10.8.0.1")
    assert (record.type, record.ttl, record.proxied, record.comment) == (
        "A",
        300,
        False,
        "managed-by: ayeops",
    )


def test_hosts_without_ingress_want_no_records(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    del data["hosts"]["central"]["modules"]["ingress"]
    assert desired_records(manifest_from(data), modules) == {}


def test_missing_records_are_created() -> None:
    desired = [Record(f"grafana.{DOMAIN}", "10.8.0.1")]
    assert actions(plan(desired, [], DOMAIN)) == [("create", f"grafana.{DOMAIN}")]


def test_matching_owned_records_need_no_change() -> None:
    desired = [Record(f"grafana.{DOMAIN}", "10.8.0.1")]
    assert plan(desired, [owned("grafana", "10.8.0.1")], DOMAIN) == []


def test_owned_record_with_another_address_is_updated_in_place() -> None:
    desired = [Record(f"grafana.{DOMAIN}", "10.8.0.1")]
    changes = plan(desired, [owned("grafana", "10.8.0.9", record_id="abc")], DOMAIN)
    assert actions(changes) == [("update", f"grafana.{DOMAIN}")]
    assert changes[0].record.record_id == "abc"
    assert changes[0].record.content == "10.8.0.1"


def test_owned_record_that_got_proxied_is_put_back_to_dns_only() -> None:
    desired = [Record(f"grafana.{DOMAIN}", "10.8.0.1")]
    changes = plan(desired, [owned("grafana", "10.8.0.1", proxied=True)], DOMAIN)
    assert actions(changes) == [("update", f"grafana.{DOMAIN}")]
    assert changes[0].record.proxied is False


def test_owned_records_no_route_wants_are_deleted() -> None:
    existing = [owned("old", "10.8.0.1")]
    assert actions(plan([], existing, DOMAIN)) == [("delete", f"old.{DOMAIN}")]


def test_duplicate_owned_records_are_reduced_to_one() -> None:
    desired = [Record(f"grafana.{DOMAIN}", "10.8.0.1")]
    existing = [owned("grafana", "10.8.0.1", "a"), owned("grafana", "10.8.0.1", "b")]
    changes = plan(desired, existing, DOMAIN)
    assert actions(changes) == [("delete", f"grafana.{DOMAIN}")]
    assert changes[0].record.record_id == "b"


def test_records_without_the_owner_comment_are_never_deleted_or_updated() -> None:
    existing = [foreign("www", "203.0.113.1"), foreign("mail", "203.0.113.2")]
    assert plan([], existing, DOMAIN) == []


def test_unmanaged_record_on_a_wanted_name_is_a_conflict_not_an_overwrite() -> None:
    desired = [Record(f"grafana.{DOMAIN}", "10.8.0.1")]
    for kind in ("A", "AAAA", "CNAME"):
        changes = plan(desired, [foreign("grafana", "203.0.113.1", kind)], DOMAIN)
        assert actions(changes) == [("conflict", f"grafana.{DOMAIN}")]


def test_owned_records_outside_the_ingress_domain_are_left_alone() -> None:
    elsewhere = Record("grafana.lab.acme.example", "10.9.0.1", record_id="z")
    assert plan([], [elsewhere], DOMAIN) == []


def test_zone_is_looked_up_from_the_longest_name_down_to_the_registrable_part() -> None:
    assert zone_candidates("ops.acme.example") == ["ops.acme.example", "acme.example"]
    assert zone_candidates("ops.acme.com.ua") == ["ops.acme.com.ua", "acme.com.ua", "com.ua"]


class FakeProvider:
    def __init__(self, existing: list[Record]) -> None:
        self.existing = existing
        self.applied: list[Change] = []

    def zone_id(self, domain: str) -> str:
        return f"zone-{domain}"

    def records(self, zone_id: str) -> list[Record]:
        return list(self.existing)

    def apply(self, zone_id: str, change: Change) -> None:
        self.applied.append(change)


def test_plan_without_apply_prints_and_writes_nothing(modules: dict[str, Module]) -> None:
    provider = FakeProvider([])
    lines: list[str] = []
    assert sync(load_manifest(EXAMPLE), modules, provider, False, lines.append) == 0
    assert provider.applied == []
    assert lines == [
        f"{DOMAIN}: create A auth.{DOMAIN} -> 10.8.0.1 (ttl 300, dns only)",
        f"{DOMAIN}: create A grafana.{DOMAIN} -> 10.8.0.1 (ttl 300, dns only)",
        f"{DOMAIN}: create A hub.{DOMAIN} -> 10.8.0.1 (ttl 300, dns only)",
        f"{DOMAIN}: create A metrics.{DOMAIN} -> 10.8.0.1 (ttl 300, dns only)",
        f"{DOMAIN}: create A oncall.{DOMAIN} -> 10.8.0.1 (ttl 300, dns only)",
        f"{DOMAIN}: create A portal.{DOMAIN} -> 10.8.0.1 (ttl 300, dns only)",
    ]


def test_apply_writes_every_planned_change(modules: dict[str, Module]) -> None:
    provider = FakeProvider([owned("old", "10.8.0.1")])
    assert sync(load_manifest(EXAMPLE), modules, provider, True, lambda _: None) == 0
    assert actions(provider.applied) == [
        ("create", f"auth.{DOMAIN}"),
        ("create", f"grafana.{DOMAIN}"),
        ("create", f"hub.{DOMAIN}"),
        ("create", f"metrics.{DOMAIN}"),
        ("delete", f"old.{DOMAIN}"),
        ("create", f"oncall.{DOMAIN}"),
        ("create", f"portal.{DOMAIN}"),
    ]


def test_apply_refuses_to_write_anything_while_a_conflict_exists(
    modules: dict[str, Module],
) -> None:
    provider = FakeProvider([foreign("grafana", "203.0.113.1")])
    assert sync(load_manifest(EXAMPLE), modules, provider, True, lambda _: None) == 1
    assert provider.applied == []


class FakeResponse(io.BytesIO):
    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


def test_cloudflare_writes_owned_dns_only_records_with_the_bearer_token() -> None:
    sent: list[urllib.request.Request] = []

    def opener(request: urllib.request.Request) -> FakeResponse:
        sent.append(request)
        return FakeResponse(json.dumps({"success": True, "result": {}}).encode())

    client = Cloudflare("secret", opener)
    client.apply(
        "z1", Change(f"grafana.{DOMAIN}", "create", Record(f"grafana.{DOMAIN}", "10.8.0.1"))
    )
    request = sent[0]
    assert request.get_method() == "POST"
    assert request.full_url.endswith("/zones/z1/dns_records")
    assert request.get_header("Authorization") == "Bearer secret"
    assert isinstance(request.data, bytes)
    body = json.loads(request.data)
    assert body == {
        "type": "A",
        "name": f"grafana.{DOMAIN}",
        "content": "10.8.0.1",
        "ttl": 300,
        "proxied": False,
        "comment": OWNER_COMMENT,
    }


def test_cloudflare_reads_every_page_of_records() -> None:
    pages = {
        1: [{"id": "a", "name": f"a.{DOMAIN}", "type": "A", "content": "10.0.0.1", "ttl": 300}],
        2: [{"id": "b", "name": f"b.{DOMAIN}", "type": "A", "content": "10.0.0.2", "ttl": 1}],
    }

    def opener(request: urllib.request.Request) -> FakeResponse:
        page = int(request.full_url.rsplit("page=", 1)[1])
        payload = {"success": True, "result": pages[page], "result_info": {"total_pages": 2}}
        return FakeResponse(json.dumps(payload).encode())

    records = Cloudflare("secret", opener).records("z1")
    assert [(r.record_id, r.comment, r.proxied) for r in records] == [
        ("a", "", False),
        ("b", "", False),
    ]


def test_dns_needs_the_cloudflare_token(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv(TOKEN_ENV, raising=False)
    assert main(["dns", str(EXAMPLE)]) == 2
    assert TOKEN_ENV in capsys.readouterr().err


def test_dns_plans_through_the_given_provider(capsys: pytest.CaptureFixture[str]) -> None:
    assert dns(EXAMPLE, False, FakeProvider([])) == 0
    assert f"create A grafana.{DOMAIN}" in capsys.readouterr().out
