from __future__ import annotations

from typing import Any

import yaml

from ayeops.registry import Module
from ayeops.render import host_compose
from tests.conftest import EXAMPLE_STAMP, manifest_from, render_once, template_images
from tests.test_dashboards import exporter_metrics, metric_names

RULES = "central/grafana/provisioning/alerting/rules.yaml"
BACKLOG = (
    "(sum by (host, project, group) (vmagent_remotewrite_pending_data_bytes)"
    " > bool 1800 * sum by (host, project, group)"
    " (rate(vmagent_remotewrite_block_size_bytes_sum[1h])))"
    " + (sum by (host, project, group) (vmagent_remotewrite_pending_data_bytes)"
    " > bool sum by (host, project, group) (vm_persistentqueue_free_disk_space_bytes))"
)
DROPPED = (
    "sum by (host, project, group) (increase(vm_persistentqueue_blocks_dropped_total[1h]))"
    " + sum by (host, project, group) (increase(vmagent_remotewrite_packets_dropped_total[1h]))"
    " > bool 0"
)
FIRST_INMEMORY_QUEUES = (1, 146, 0)


def metrics_alerts(modules: dict[str, Module]) -> dict[str, dict[str, Any]]:
    return {alert["name"]: alert for alert in modules["metrics"].alerts}


def vmagent_command(data: dict[str, Any], host: str, modules: dict[str, Module]) -> list[str]:
    services = host_compose(manifest_from(data), host, modules)["services"]
    return list(services["vmagent"]["command"])


def rendered_rules(data: dict[str, Any], modules: dict[str, Module]) -> dict[str, dict[str, Any]]:
    tree = render_once(manifest_from(data), modules, EXAMPLE_STAMP)
    groups = yaml.safe_load(tree[RULES])["groups"]
    return {rule["uid"]: rule for group in groups for rule in group["rules"]}


def test_vmagent_sends_fresh_samples_ahead_of_its_disk_backlog(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    for host in ("central", "edge"):
        command = vmagent_command(data, host, modules)
        assert command[-4:] == [
            "-remoteWrite.tmpDataPath=/vmagent-data",
            "-remoteWrite.maxDiskUsagePerURL=1GB",
            "-remoteWrite.inmemoryQueues=1",
            "-httpListenAddr=:8429",
        ]


def test_the_disk_queue_is_capped_at_queue_max(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    data["hosts"]["edge"]["modules"]["metrics"]["queue_max"] = "2GB"
    command = vmagent_command(data, "edge", modules)
    assert "-remoteWrite.maxDiskUsagePerURL=2GB" in command
    assert "-remoteWrite.maxDiskUsagePerURL=1GB" not in command


def test_the_pinned_vmagent_knows_the_in_memory_queues() -> None:
    (tag,) = [tag for name, tag in template_images("metrics") if name == "victoriametrics/vmagent"]
    version = tuple(int(part) for part in tag.removeprefix("v").split("."))
    assert version >= FIRST_INMEMORY_QUEUES


def test_a_backlog_of_half_an_hour_of_traffic_or_half_the_room_warns_after_15_minutes(
    modules: dict[str, Module],
) -> None:
    assert metrics_alerts(modules)["metrics-shipping-backlog"] == {
        "name": "metrics-shipping-backlog",
        "title": "vmagent falls behind shipping metrics",
        "expr": BACKLOG,
        "for": "15m",
        "severity": "warning",
        "summary": "vmagent on {{ .Labels.host }} holds more than 30 minutes of its samples"
        " or half its queue room unshipped",
        "runbook": "metrics-shipping-backlog",
    }


def test_any_dropped_or_rejected_block_of_the_last_hour_is_critical_at_once(
    modules: dict[str, Module],
) -> None:
    assert metrics_alerts(modules)["metrics-shipping-dropped"] == {
        "name": "metrics-shipping-dropped",
        "title": "vmagent drops metrics",
        "expr": DROPPED,
        "for": "0m",
        "severity": "critical",
        "summary": "vmagent on {{ .Labels.host }} dropped metrics: its full queue discarded"
        " the oldest, or storage rejected a block",
        "runbook": "metrics-shipping-dropped",
        "dashboard": {"uid": "ayeops-health", "panel": 12},
    }


def test_the_queue_alerts_read_only_series_the_pinned_vmagent_exports(
    modules: dict[str, Module],
) -> None:
    alerts = metrics_alerts(modules)
    names = metric_names(alerts["metrics-shipping-backlog"]["expr"])
    names |= metric_names(alerts["metrics-shipping-dropped"]["expr"])
    assert names == {
        "vmagent_remotewrite_pending_data_bytes",
        "vmagent_remotewrite_block_size_bytes_sum",
        "vm_persistentqueue_free_disk_space_bytes",
        "vm_persistentqueue_blocks_dropped_total",
        "vmagent_remotewrite_packets_dropped_total",
    }
    assert names <= exporter_metrics()["vmagent"]


def test_the_vmagent_catalog_entry_names_the_queue_alerts_and_their_metrics(
    modules: dict[str, Module],
) -> None:
    entry = modules["metrics"].catalog["vmagent"]
    assert entry["alerts"] == [
        "target-down",
        "metrics-shipping-backlog",
        "metrics-shipping-dropped",
    ]
    assert set(entry["metrics"]) <= exporter_metrics()["vmagent"] | {"vm_promscrape_targets"}
    assert {
        "vmagent_remotewrite_pending_data_bytes",
        "vm_persistentqueue_blocks_dropped_total",
        "vmagent_remotewrite_packets_dropped_total",
    } <= set(entry["metrics"])


def test_the_rendered_queue_rules_are_held_back_by_host_down_and_link_the_queue_panel(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    rules = rendered_rules(data, modules)
    backlog = rules["aye-metrics-shipping-backlog"]
    dropped = rules["aye-metrics-shipping-dropped"]
    assert backlog["labels"]["severity"] == "warning"
    assert dropped["labels"]["severity"] == "critical"
    assert (backlog["for"], dropped["for"]) == ("15m", "0m")
    assert backlog["noDataState"] == dropped["noDataState"] == "OK"
    for rule, expr in ((backlog, BACKLOG), (dropped, DROPPED)):
        query = rule["data"][0]["model"]["expr"]
        assert query.startswith(f"({expr}) unless on (host) (")
        assert query.endswith(f" or 0 * ({expr})")
        assert rule["annotations"]["__dashboardUid__"] == "ayeops-health"
        assert rule["annotations"]["__panelId__"] == "12"
        assert rule["annotations"]["runbook_url"].endswith(f"/docs/runbooks/{rule['uid'][4:]}.md")
