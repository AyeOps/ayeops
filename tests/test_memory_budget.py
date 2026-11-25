from __future__ import annotations

from typing import Any

from ayeops.registry import Module
from ayeops.render import host_compose
from ayeops.rules import parse_size
from tests.conftest import check_once, manifest_from, rules_hit

MIB = 1024**2


def containers_mib(data: dict[str, Any], host: str, modules: dict[str, Module]) -> int:
    services = host_compose(manifest_from(data), host, modules)["services"]
    return sum(parse_size(str(s["mem_limit"])) for s in services.values()) // MIB


def test_containers_above_85_percent_of_host_memory_fail_even_when_they_fit(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    del data["hosts"]["central"]["reserved"]
    used = containers_mib(data, "central", modules)
    data["hosts"]["central"]["memory"] = f"{used * 100 // 90}m"
    assert rules_hit(data) == {"memory-budget"}


def test_memory_budget_percentage_is_configurable_per_host(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    del data["hosts"]["central"]["reserved"]
    used = containers_mib(data, "central", modules)
    data["hosts"]["central"]["memory"] = f"{used * 100 // 90}m"
    data["hosts"]["central"]["memory_budget_pct"] = 95
    assert rules_hit(data) == set()


def test_declared_host_reservations_count_towards_the_budget(data: dict[str, Any]) -> None:
    data["hosts"]["central"]["reserved"] = {"php-fpm": "1g", "mysql": "4g"}
    assert rules_hit(data) == set()
    data["hosts"]["central"]["reserved"]["mysql"] = "8g"
    assert rules_hit(data) == {"memory-budget"}


def test_memory_budget_finding_names_containers_reservations_and_the_limit(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    data["hosts"]["central"]["reserved"] = {"mysql": "12g"}
    findings = check_once(manifest_from(data), modules)
    assert [f.rule for f in findings] == ["memory-budget"]
    assert "plus reserved 12288m" in findings[0].message
    assert "85 % of memory 12g" in findings[0].message


def test_reservations_must_be_sizes_and_the_percentage_at_most_100(
    data: dict[str, Any],
) -> None:
    data["hosts"]["central"]["reserved"] = {"mysql": "lots"}
    assert rules_hit(data) == {"schema"}
    data["hosts"]["central"]["reserved"] = {}
    data["hosts"]["central"]["memory_budget_pct"] = 120
    assert rules_hit(data) == {"schema"}
