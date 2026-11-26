from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest

from ayeops.discover.adopt import RUNBOOK as ADOPT_RUNBOOK
from ayeops.duties import RUNBOOK as DUTY_RUNBOOK
from ayeops.local_modules import with_local_modules
from ayeops.registry import Module, load_modules
from ayeops.replication import RUNBOOKS as REPLICATION_RUNBOOKS
from ayeops.sentinel import RUNBOOK as SENTINEL_RUNBOOK
from ayeops.unused import (
    linked_pages,
    local_module_paths,
    reachable_pages,
    unused_externals,
    unused_modules,
)
from tests.conftest import ROOT, manifest_from, rules_hit, warnings_hit

FIXTURE = ROOT / "tests" / "fixtures" / "modules"
RUNBOOKS = ROOT / "docs" / "runbooks"
SHARED_PAGES = {
    "flow-baseline",
    "glitchtip",
    "infisical",
    "oauth2-proxy",
    "offsite",
    "offsite-append-only",
}


@pytest.fixture
def local_root(tmp_path: Path) -> Path:
    root = tmp_path / "modules"
    shutil.copytree(FIXTURE, root)
    return root


def with_queue(data: dict[str, Any], root: Path, enabled: bool = True) -> dict[str, Any]:
    data["module_paths"] = [str(root)]
    if enabled:
        data["hosts"]["edge"]["modules"]["acme-queue"] = {}
    return data


def queue_problems(data: dict[str, Any]) -> list[tuple[str, str, str]]:
    manifest = manifest_from(data)
    return unused_modules(manifest, with_local_modules(manifest, load_modules()))


def test_every_runbook_page_belongs_to_an_alert_or_is_a_shared_page(
    modules: dict[str, Module],
) -> None:
    alerts = {str(alert["runbook"]) for module in modules.values() for alert in module.alerts}
    for runbook in (SENTINEL_RUNBOOK, ADOPT_RUNBOOK, DUTY_RUNBOOK, *REPLICATION_RUNBOOKS):
        alerts.add(runbook.rpartition("/")[2].removesuffix(".md"))
    pages = {page.stem for page in RUNBOOKS.glob("*.md")}
    assert sorted(pages - alerts - SHARED_PAGES) == []


def test_a_shared_page_is_no_alert_page_and_the_module_docs_link_to_it(
    modules: dict[str, Module],
) -> None:
    alerts = {str(alert["runbook"]) for module in modules.values() for alert in module.alerts}
    docs = (ROOT / "docs" / "modules.md").read_text(encoding="utf-8")
    assert sorted(SHARED_PAGES & alerts) == []
    assert sorted(name for name in SHARED_PAGES if f"](runbooks/{name}.md" not in docs) == []


def test_links_name_pages_with_or_without_an_anchor() -> None:
    text = "see [a](a.md), [b](./b.md#restore), [c](../c.md), [d](https://x/d.md) and e.md"
    assert linked_pages(text) == {"a", "b"}


def test_pages_are_reached_through_links_from_the_roots() -> None:
    pages = {
        "alert": "[hub](hub.md)",
        "hub": "[deep](deep.md)",
        "deep": "",
        "lone": "[a](alert.md)",
    }
    assert reachable_pages(pages, {"alert", "missing"}) == {"alert", "hub", "deep"}


def test_the_example_has_no_unused_externals(data: dict[str, Any]) -> None:
    assert unused_externals(manifest_from(data)) == []


def test_an_external_nobody_references_is_reported(data: dict[str, Any]) -> None:
    data["externals"]["payments"] = {"title": "Payments API"}
    assert unused_externals(manifest_from(data)) == [
        (
            "externals.payments",
            "external-used",
            "no flow, depends_on or clients entry names external:payments",
        )
    ]
    assert "external-used" in warnings_hit(data)
    assert "external-used" not in rules_hit(data)


@pytest.mark.parametrize("field", ["depends_on", "clients"])
def test_a_service_dependency_or_client_uses_an_external(data: dict[str, Any], field: str) -> None:
    data["externals"]["payments"] = {"title": "Payments API"}
    web = data["services"]["web"]
    web[field] = [*(web.get(field) or []), "external:payments"]
    assert unused_externals(manifest_from(data)) == []


@pytest.mark.parametrize("end", ["from", "to"])
def test_either_end_of_a_flow_uses_an_external(data: dict[str, Any], end: str) -> None:
    data["externals"]["payments"] = {"title": "Payments API"}
    flow = {"from": "web", "to": "web", end: "external:payments"}
    data["flows"] = [*data.get("flows", []), flow]
    assert unused_externals(manifest_from(data)) == []


def test_an_enabled_local_module_with_its_alert_runbooks_is_used(
    data: dict[str, Any], local_root: Path
) -> None:
    assert queue_problems(with_queue(data, local_root)) == []
    assert rules_hit(with_queue(data, local_root)) == set()
    assert warnings_hit(with_queue(data, local_root)) == set()


def test_a_local_module_enabled_on_no_host_is_reported(
    data: dict[str, Any], local_root: Path
) -> None:
    problems = queue_problems(with_queue(data, local_root, enabled=False))
    assert problems == [
        ("module_paths.0.queue", "module-used", "module 'acme-queue' is enabled on no host")
    ]
    assert warnings_hit(with_queue(data, local_root, enabled=False)) == {"module-used"}
    assert rules_hit(with_queue(data, local_root, enabled=False)) == set()


def test_a_local_module_turned_off_everywhere_is_unused(
    data: dict[str, Any], local_root: Path
) -> None:
    with_queue(data, local_root, enabled=False)
    data["hosts"]["edge"]["modules"]["acme-queue"] = False
    assert [rule for _, rule, _ in queue_problems(data)] == ["module-used"]


def test_a_local_module_enabled_through_a_profile_is_used(
    data: dict[str, Any], local_root: Path
) -> None:
    with_queue(data, local_root, enabled=False)
    data["profiles"] = {"queue": {"modules": {"acme-queue": {}}}}
    data["hosts"]["edge"]["profile"] = "queue"
    assert queue_problems(data) == []


def test_a_local_runbook_page_without_an_alert_is_reported(
    data: dict[str, Any], local_root: Path
) -> None:
    (local_root / "queue" / "runbooks" / "queue-stalled.md").write_text("# Old\n", encoding="utf-8")
    assert queue_problems(with_queue(data, local_root)) == [
        (
            "module_paths.0.queue.runbooks.queue-stalled",
            "runbook-used",
            "runbooks/queue-stalled.md is the runbook of no alert of acme-queue"
            " and no runbook of one links to it",
        )
    ]
    assert warnings_hit(with_queue(data, local_root)) == {"runbook-used"}
    assert rules_hit(with_queue(data, local_root)) == set()


def test_a_local_page_linked_from_an_alert_runbook_is_used(
    data: dict[str, Any], local_root: Path
) -> None:
    pages = local_root / "queue" / "runbooks"
    (pages / "queue-ops.md").write_text("# Operating the queue\n", encoding="utf-8")
    page = pages / "queue-backlog.md"
    page.write_text(
        page.read_text(encoding="utf-8") + "\nSee [operating](queue-ops.md#restore).\n",
        encoding="utf-8",
    )
    assert queue_problems(with_queue(data, local_root)) == []


def test_a_local_alert_runbook_with_an_md_suffix_counts(
    data: dict[str, Any], local_root: Path
) -> None:
    manifest = manifest_from(with_queue(data, local_root))
    modules = with_local_modules(manifest, load_modules())
    module = modules["acme-queue"]
    alerts = [{**alert, "runbook": f"{alert['runbook']}.md"} for alert in module.alerts]
    renamed = Module(module.name, module.path, {**module.spec, "alerts": alerts}, local=True)
    assert unused_modules(manifest, {**modules, "acme-queue": renamed}) == []


def test_only_local_modules_of_the_manifest_paths_are_checked(
    data: dict[str, Any], local_root: Path
) -> None:
    manifest = manifest_from(with_queue(data, local_root))
    modules = with_local_modules(manifest, load_modules())
    assert local_module_paths(manifest, modules) == {"acme-queue": "module_paths.0.queue"}
    assert local_module_paths(manifest_from(data | {"module_paths": []}), modules) == {}


def test_builtin_modules_are_never_unused(data: dict[str, Any]) -> None:
    assert unused_modules(manifest_from(data), load_modules()) == []
