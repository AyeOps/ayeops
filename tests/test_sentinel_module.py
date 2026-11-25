from __future__ import annotations

import json
from typing import Any

import pytest
import yaml

from ayeops.registry import Module, load_modules
from ayeops.sentinel import (
    check_problem,
    digest_url,
    grafana_health,
    merged_checks,
    sentinel_context,
)
from tests.conftest import check_once, manifest_from, pinned, render_once, rules_hit, tag_of
from tests.test_sentinel import GRAFANA, SCRIPT, VM_HEALTH, VM_QUERY, s


def acme_with_sentinel(data: dict[str, Any], **settings: Any) -> dict[str, Any]:
    data["hosts"]["edge"]["modules"]["sentinel"] = {"watch": "central", **settings}
    return data


def edge_compose(data: dict[str, Any], modules: dict[str, Module]) -> dict[str, Any]:
    tree = render_once(manifest_from(data), modules)
    compose: dict[str, Any] = yaml.safe_load(tree["edge/compose.yaml"])
    return compose


def rendered_checks(data: dict[str, Any], modules: dict[str, Module]) -> list[dict[str, Any]]:
    environment = edge_compose(data, modules)["services"]["sentinel"]["environment"]
    checks: list[dict[str, Any]] = json.loads(environment["AYE_SENTINEL_CHECKS"])
    return checks


def test_example_renders_the_sentinel_on_edge_with_checks_derived_from_central(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    tree = render_once(manifest_from(data), modules)
    service = yaml.safe_load(tree["edge/compose.yaml"])["services"]["sentinel"]
    assert service["mem_limit"] == "48m"
    assert tag_of(service["image"]) == pinned("sentinel", "python")
    assert service["user"] == "65534:65534"
    assert service["ports"] == ["127.0.0.1:9134:9134"]
    environment = service["environment"]
    assert environment["AYE_SENTINEL_WATCH"] == "central"
    assert environment["AYE_SENTINEL_HOST"] == "edge"
    assert environment["AYE_SENTINEL_FAILURES_BEFORE_ALERT"] == "3"
    assert environment["AYE_SENTINEL_REMINDER"] == "6h"
    assert environment["AYE_SENTINEL_PING_URL"] == "${AYE_SENTINEL_PING_URL:-}"
    assert environment["AYE_TELEGRAM_BOT_TOKEN"].startswith("${AYE_TELEGRAM_BOT_TOKEN:?")
    assert json.loads(environment["AYE_SENTINEL_CHECKS"]) == [
        {"name": "grafana", "kind": "http", "url": GRAFANA, "contains": '"database": "ok"'},
        {"name": "victoriametrics", "kind": "http", "url": VM_HEALTH},
        {"name": "samples", "kind": "fresh", "url": VM_QUERY, "max_age_seconds": 180},
    ]
    assert tree["edge/sentinel/sentinel.py"] == SCRIPT.read_text(encoding="utf-8").replace(
        "\r\n", "\n"
    )
    assert "AYE_SENTINEL_PING_URL=" in tree["edge/.env.example"]
    jobs = yaml.safe_load(tree["edge/vmagent/scrape.yaml"])["scrape_configs"]
    assert {"job_name": "sentinel", "static_configs": [{"targets": ["sentinel:9134"]}]} in jobs
    assert "sentinel" not in yaml.safe_load(tree["central/compose.yaml"])["services"]


def test_rendered_checks_parse_in_the_script(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    environment = edge_compose(data, modules)["services"]["sentinel"]["environment"]
    assert [c.name for c in s.parse_checks(environment["AYE_SENTINEL_CHECKS"])] == [
        "grafana",
        "victoriametrics",
        "samples",
    ]


def test_checks_override_by_name_disable_and_add(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    acme_with_sentinel(
        data,
        checks=[
            {"name": "grafana", "enabled": False},
            {"name": "samples", "max_age_seconds": 300},
            {"name": "tls", "kind": "tls", "host": "grafana.ops.acme.example"},
        ],
    )
    assert rendered_checks(data, modules) == [
        {"name": "victoriametrics", "kind": "http", "url": VM_HEALTH},
        {"name": "samples", "kind": "fresh", "url": VM_QUERY, "max_age_seconds": 300},
        {"name": "tls", "kind": "tls", "host": "grafana.ops.acme.example"},
    ]
    assert rules_hit(data) == set()


def test_a_check_that_changes_kind_drops_the_inherited_fields(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    acme_with_sentinel(data, checks=[{"name": "grafana", "kind": "tls", "host": "g.example"}])
    assert rendered_checks(data, modules)[0] == {
        "name": "grafana",
        "kind": "tls",
        "host": "g.example",
    }


def test_grafana_without_https_root_url_is_checked_on_its_exposed_port(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    grafana = data["hosts"]["central"]["modules"]["grafana"]
    grafana["root_url"] = "http://grafana.local"
    assert [c["name"] for c in rendered_checks(data, modules)] == ["victoriametrics", "samples"]
    grafana["expose"] = [3000]
    assert rendered_checks(data, modules)[0]["url"] == "http://10.8.0.1:3000/api/health"


def test_settings_are_passed_to_the_container(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    acme_with_sentinel(data, failures_before_alert=5, reminder="2h", interval="30s", timeout="5s")
    environment = edge_compose(data, modules)["services"]["sentinel"]["environment"]
    assert environment["AYE_SENTINEL_FAILURES_BEFORE_ALERT"] == "5"
    assert environment["AYE_SENTINEL_REMINDER"] == "2h"
    assert environment["AYE_SENTINEL_INTERVAL"] == "30s"
    assert environment["AYE_SENTINEL_TIMEOUT"] == "5s"
    passed = {k: str(v) for k, v in environment.items() if not str(v).startswith("${")}
    loaded = s.config_from(passed)
    assert (loaded.failures_before_alert, loaded.reminder_seconds) == (5, 7200)
    assert (loaded.interval_seconds, loaded.timeout_seconds) == (30, 5)


def test_a_sentinel_must_watch_another_known_host(data: dict[str, Any]) -> None:
    acme_with_sentinel(data, watch="nowhere")
    assert rules_hit(data) == {"sentinel-watch"}
    data["hosts"]["central"]["modules"]["sentinel"] = {"watch": "central"}
    del data["hosts"]["edge"]["modules"]["sentinel"]
    assert rules_hit(data) == {"sentinel-watch"}


def test_a_sentinel_in_the_zone_of_the_watched_host_is_rejected(data: dict[str, Any]) -> None:
    acme_with_sentinel(data)
    del data["hosts"]["edge"]["modules"]["offsite"]
    data["services"]["acme-db"]["data"]["copies"] = ["host"]
    data["hosts"]["edge"]["zone"] = "datacenter"
    assert rules_hit(data) == {"sentinel-watch"}
    del data["hosts"]["edge"]["zone"]
    assert rules_hit(data) == set()


def test_a_watched_host_without_defaults_needs_explicit_checks(data: dict[str, Any]) -> None:
    data["hosts"]["edge"]["modules"]["sentinel"] = {"watch": "edge"}
    data["hosts"]["central"]["modules"]["sentinel"] = {"watch": "edge"}
    del data["hosts"]["edge"]["modules"]["sentinel"]
    assert rules_hit(data) == {"sentinel-checks"}
    data["hosts"]["central"]["modules"]["sentinel"]["checks"] = [
        {"name": "node", "kind": "http", "url": "http://edge:9100/"}
    ]
    assert rules_hit(data) == set()


def test_a_new_check_needs_its_kind_and_target(data: dict[str, Any]) -> None:
    acme_with_sentinel(data, checks=[{"name": "extra", "url": "http://x"}])
    assert rules_hit(data) == {"sentinel-checks"}
    acme_with_sentinel(data, checks=[{"name": "extra", "kind": "tls"}])
    assert rules_hit(data) == {"sentinel-checks"}
    acme_with_sentinel(data, checks=[{"name": "extra", "kind": "fresh"}])
    assert rules_hit(data) == {"sentinel-checks"}


@pytest.mark.parametrize(
    "settings",
    [
        {},
        {"watch": "central", "reminder": "6"},
        {"watch": "central", "reminder": "0h"},
        {"watch": "central", "failures_before_alert": 0},
        {"watch": "central", "interval": "1h"},
        {"watch": "central", "checks": [{"name": "x", "kind": "ping"}]},
        {"watch": "central", "checks": [{"name": "x", "url": "ftp://x"}]},
        {"watch": "central", "checks": [{"kind": "http"}]},
        {"watch": "central", "expose": [9100]},
        {"watch": "central", "extra": 1},
    ],
)
def test_sentinel_settings_schema(data: dict[str, Any], settings: dict[str, Any]) -> None:
    data["hosts"]["edge"]["modules"]["sentinel"] = settings
    assert rules_hit(data) == {"module-settings"}


def test_sentinel_alerts_are_rendered_into_central_grafana(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    tree = render_once(manifest_from(data), modules)
    rules = yaml.safe_load(tree["central/grafana/provisioning/alerting/rules.yaml"])
    sentinel = next(group for group in rules["groups"] if group["name"] == "sentinel")
    assert [rule["uid"] for rule in sentinel["rules"]] == [
        "aye-sentinel-notify-failing",
        "aye-sentinel-ping-failing",
        "aye-sentinel-stalled",
        "aye-sentinel-digest-overdue",
    ]


def test_merged_checks_replace_drop_add_and_keep_order() -> None:
    defaults: list[dict[str, Any]] = [
        {"name": "a", "kind": "http", "url": "http://a"},
        {"name": "b", "kind": "http", "url": "http://b"},
        {"name": "c", "kind": "fresh", "url": "http://c", "max_age_seconds": 180},
    ]
    merged = merged_checks(
        defaults,
        [
            {"name": "a", "enabled": False},
            {"name": "c", "max_age_seconds": 60, "enabled": True},
            {"name": "d", "kind": "tls", "host": "d"},
            {"name": "x", "enabled": False},
        ],
    )
    assert merged == [
        {"name": "b", "kind": "http", "url": "http://b"},
        {"name": "c", "kind": "fresh", "url": "http://c", "max_age_seconds": 60},
        {"name": "d", "kind": "tls", "host": "d"},
    ]
    assert defaults[2]["max_age_seconds"] == 180


def test_merged_checks_keep_inherited_fields_when_the_kind_is_repeated() -> None:
    merged = merged_checks(
        [{"name": "a", "kind": "http", "url": "http://a"}],
        [{"name": "a", "kind": "http", "contains": "ok"}],
    )
    assert merged == [{"name": "a", "kind": "http", "url": "http://a", "contains": "ok"}]


@pytest.mark.parametrize(
    ("settings", "url"),
    [
        ({"root_url": "https://g.example/"}, "https://g.example/api/health"),
        ({"root_url": "https://g.example", "expose": [3000]}, "https://g.example/api/health"),
        ({"root_url": "http://g.example", "expose": [3000]}, "http://10.0.0.1:3000/api/health"),
        ({"expose": [3000]}, "http://10.0.0.1:3000/api/health"),
        ({"root_url": "http://g.example"}, None),
        ({}, None),
    ],
)
def test_grafana_health(settings: dict[str, Any], url: str | None) -> None:
    assert grafana_health("10.0.0.1", settings) == url


@pytest.mark.parametrize(
    ("check", "problem"),
    [
        ({"name": "a", "kind": "http", "url": "https://a"}, None),
        ({"name": "a", "kind": "fresh", "url": "http://a"}, None),
        ({"name": "a", "kind": "tls", "host": "a"}, None),
        ({"name": "a", "kind": "tls"}, "check 'a' of kind tls needs host"),
        ({"name": "a", "kind": "http"}, "check 'a' of kind http needs an http(s) url"),
        (
            {"name": "a", "kind": "fresh", "url": "a"},
            "check 'a' of kind fresh needs an http(s) url",
        ),
        ({"name": "a", "url": "http://a"}, "check 'a' needs kind: one of http, fresh, tls"),
    ],
)
def test_check_problem(check: dict[str, Any], problem: str | None) -> None:
    assert check_problem(check) == problem


def test_a_metrics_server_that_does_not_expose_8428_gives_no_storage_checks(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    del data["hosts"]["central"]["modules"]["metrics"]["expose"]
    assert [c["name"] for c in rendered_checks(data, modules)] == ["grafana"]


def test_the_container_gets_the_runbook_of_the_message(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    environment = edge_compose(data, modules)["services"]["sentinel"]["environment"]
    assert environment["AYE_SENTINEL_RUNBOOK"] == (
        "https://github.com/AyeOps/ayeops/blob/main/docs/runbooks/sentinel-central-down.md"
    )


def findings(data: dict[str, Any]) -> list[tuple[str, str, str]]:
    return [
        (f.path, f.rule, f.message)
        for f in check_once(manifest_from(data), load_modules())
        if f.rule.startswith("sentinel-")
    ]


def test_sentinel_findings_name_the_setting_and_the_reason(data: dict[str, Any]) -> None:
    data["hosts"]["central"]["modules"]["sentinel"] = {"watch": "nowhere"}
    data["hosts"]["edge"]["modules"]["sentinel"] = {
        "watch": "edge",
        "checks": [{"name": "x", "kind": "tls"}],
    }
    assert findings(data) == [
        ("hosts.central.modules.sentinel.watch", "sentinel-watch", "unknown host 'nowhere'"),
        (
            "hosts.edge.modules.sentinel.checks",
            "sentinel-checks",
            "check 'x' of kind tls needs host",
        ),
        (
            "hosts.edge.modules.sentinel.watch",
            "sentinel-watch",
            "a sentinel on edge cannot watch edge: when the host dies, so does it",
        ),
    ]


def test_sentinel_finding_for_a_host_without_defaults(data: dict[str, Any]) -> None:
    data["hosts"]["central"]["modules"]["sentinel"] = {"watch": "edge"}
    assert findings(data) == [
        (
            "hosts.central.modules.sentinel.checks",
            "sentinel-checks",
            "edge runs no grafana with root_url or exposed 3000 and no metrics server exposing"
            " 8428, so there is nothing to check by default: list checks",
        )
    ]


def sentinel_environment(data: dict[str, Any], modules: dict[str, Module]) -> dict[str, Any]:
    environment: dict[str, Any] = edge_compose(data, modules)["services"]["sentinel"]["environment"]
    return environment


def test_the_example_sends_the_digest_at_eight_in_the_fleet_zone_from_the_samples_url(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    environment = sentinel_environment(data, modules)
    assert environment["AYE_SENTINEL_DIGEST_AT"] == "08:00"
    assert environment["AYE_SENTINEL_DIGEST_TIMEZONE"] == data["timezone"]
    assert environment["AYE_SENTINEL_DIGEST_URL"] == VM_QUERY
    passed = {k: str(v) for k, v in environment.items() if not str(v).startswith("${")}
    loaded = s.config_from(passed)
    assert (loaded.digest_at, loaded.digest_timezone, loaded.digest_url) == (
        "08:00",
        data["timezone"],
        VM_QUERY,
    )


def test_the_digest_takes_its_own_time_zone_and_url(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    digest = {"at": "21:30", "timezone": "Asia/Tokyo", "url": "https://vm.example/api/v1/query"}
    acme_with_sentinel(data, digest=digest)
    environment = sentinel_environment(data, modules)
    assert environment["AYE_SENTINEL_DIGEST_AT"] == "21:30"
    assert environment["AYE_SENTINEL_DIGEST_TIMEZONE"] == "Asia/Tokyo"
    assert environment["AYE_SENTINEL_DIGEST_URL"] == "https://vm.example/api/v1/query"
    assert rules_hit(data) == set()


def test_without_digest_the_container_and_grafana_know_nothing_of_it(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    acme_with_sentinel(data)
    environment = sentinel_environment(data, modules)
    assert not [name for name in environment if "DIGEST" in name]
    tree = render_once(manifest_from(data), modules)
    rules = yaml.safe_load(tree["central/grafana/provisioning/alerting/rules.yaml"])
    sentinel = next(group for group in rules["groups"] if group["name"] == "sentinel")
    assert "aye-sentinel-digest-overdue" not in [rule["uid"] for rule in sentinel["rules"]]


def test_the_digest_overdue_alert_fires_two_hours_after_the_due_time(
    modules: dict[str, Module],
) -> None:
    alert = next(a for a in modules["sentinel"].alerts if a["name"] == "sentinel-digest-overdue")
    assert alert["expr"] == "time() - ayeops_sentinel_digest_due_timestamp_seconds > bool 7200"
    assert (alert["severity"], alert["for"], alert["setting"]) == ("warning", "5m", "digest")


def test_a_digest_without_a_samples_check_needs_its_url(data: dict[str, Any]) -> None:
    acme_with_sentinel(data, digest={"at": "08:00"}, checks=[{"name": "samples", "enabled": False}])
    assert rules_hit(data) == {"sentinel-digest"}
    assert findings(data) == [
        (
            "hosts.edge.modules.sentinel.digest",
            "sentinel-digest",
            "the digest reads the backup metrics from the VictoriaMetrics of central and there"
            " is no samples check to take its address from: set digest.url",
        )
    ]
    data["hosts"]["edge"]["modules"]["sentinel"]["digest"]["url"] = VM_QUERY
    assert rules_hit(data) == set()
    data["hosts"]["edge"]["modules"]["sentinel"]["digest"] = None
    assert rules_hit(data) == set()


@pytest.mark.parametrize(
    "digest",
    [
        {},
        {"at": "8:00"},
        {"at": "24:00"},
        {"at": "08:00", "url": "ftp://vm"},
        {"at": "08:00", "timezone": "not a zone"},
        {"at": "08:00", "extra": 1},
        "08:00",
    ],
)
def test_digest_settings_schema(data: dict[str, Any], digest: Any) -> None:
    acme_with_sentinel(data, digest=digest)
    assert rules_hit(data) == {"module-settings"}


@pytest.mark.parametrize(
    ("settings", "checks", "url"),
    [
        (
            {"digest": {"url": "http://own"}},
            [{"name": "samples", "kind": "fresh", "url": "x"}],
            "http://own",
        ),
        (
            {"digest": {"url": ""}},
            [{"name": "samples", "kind": "fresh", "url": "http://s"}],
            "http://s",
        ),
        ({"digest": None}, [{"name": "samples", "kind": "fresh", "url": "http://s"}], "http://s"),
        (
            {},
            [
                {"name": "a", "kind": "fresh", "url": "http://a"},
                {"name": "samples", "kind": "fresh", "url": "http://s"},
            ],
            "http://s",
        ),
        ({}, [{"name": "samples", "kind": "http", "url": "http://s"}], ""),
        ({}, [{"name": "samples"}], ""),
        ({}, [{"name": "other", "kind": "fresh", "url": "http://o"}], ""),
        ({}, [{"name": "samples", "kind": "fresh"}], ""),
        ({}, [], ""),
    ],
)
def test_digest_url(settings: dict[str, Any], checks: list[dict[str, Any]], url: str) -> None:
    assert digest_url(settings, checks) == url


def test_the_sentinel_context_carries_the_digest_url(data: dict[str, Any]) -> None:
    manifest = manifest_from(data)
    settings = load_modules()["sentinel"].settings({"watch": "central"})
    context = sentinel_context(manifest, settings)
    assert context["digest_url"] == VM_QUERY
    assert sentinel_context(manifest, None) == {}
