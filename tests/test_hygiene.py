from __future__ import annotations

import importlib.util
import json
import os
import sys
import time
from collections.abc import Mapping
from pathlib import Path
from types import ModuleType
from typing import Any, ClassVar

import pytest

from ayeops.registry import Module
from ayeops.render import DEFAULT_LOGGING, finalize_service, host_compose
from tests.conftest import ROOT, WAIT, manifest_from, render_once, rules_hit
from tests.test_checks import with_checks

RUNNER = ROOT / "src" / "ayeops" / "modules" / "hygiene" / "files" / "hygiene_runner.py"
GIB = 1024**3


def load_runner() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "ayeops.modules.hygiene.files.hygiene_runner", RUNNER
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


runner = load_runner()


class FakeApi:
    def __init__(self, responses: Mapping[str, Any], fail: str = "") -> None:
        self.responses = dict(responses)
        self.calls: list[tuple[str, str, dict[str, str]]] = []
        self.fail = fail

    def __call__(self, method: str, path: str, query: Mapping[str, str]) -> Any:
        self.calls.append((method, path, dict(query)))
        if self.fail and path.startswith(self.fail):
            raise RuntimeError(f"{path} refused")
        return self.responses.get(f"{method} {path.split('?', 1)[0]}")


NOW = 2_000_000_000.0
DAY = 86400


def docker(version: str = "1.56") -> FakeApi:
    return FakeApi(
        {
            "GET /version": {"ApiVersion": version},
            "GET /system/df": {
                "Images": [{"Id": "a"}, {"Id": "b"}, {"Id": "c"}],
                "BuildCache": [
                    {"Size": 30 * GIB, "Shared": False, "InUse": False},
                    {"Size": 4 * GIB, "Shared": True, "InUse": False},
                ],
                "Volumes": [
                    {
                        "Labels": {"com.docker.volume.anonymous": ""},
                        "UsageData": {"Size": 7, "RefCount": 0},
                    },
                    {"Labels": {}, "UsageData": {"Size": 900, "RefCount": 0}},
                ],
            },
            "GET /containers/json": [
                {"ImageID": "a", "State": "running", "Created": NOW - 9 * DAY, "SizeRw": 1},
                {"ImageID": "b", "State": "exited", "Created": NOW - 2 * DAY, "SizeRw": 40},
                {"ImageID": "b", "State": "exited", "Created": NOW - 3600, "SizeRw": 2},
            ],
            "GET /images/json": [
                {"Id": "a", "Created": NOW - 30 * DAY, "Size": 100, "SharedSize": 0},
                {"Id": "b", "Created": NOW - 30 * DAY, "Size": 100, "SharedSize": 0},
                {"Id": "c", "Created": NOW - 30 * DAY, "Size": 500, "SharedSize": 200},
                {"Id": "d", "Created": NOW - DAY, "Size": 800, "SharedSize": 0},
            ],
            "POST /containers/prune": {"ContainersDeleted": ["x"], "SpaceReclaimed": 40},
            "POST /images/prune": {
                "ImagesDeleted": [{"Deleted": "c"}, {"Untagged": "c:1"}],
                "SpaceReclaimed": 300,
            },
            "POST /build/prune": {"CachesDeleted": ["k1", "k2", "k3"], "SpaceReclaimed": 20 * GIB},
            "POST /volumes/prune": {"VolumesDeleted": ["v"], "SpaceReclaimed": 7},
        }
    )


def settings(**extra: Any) -> Any:
    values: dict[str, Any] = {"disk": None, "build_cache_keep": 10 * GIB, **extra}
    return runner.Settings(**values)


def clock(*values: float) -> Any:
    remaining = list(values)
    return lambda: remaining.pop(0)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("10g", 10 * GIB),
        (" 10G ", 10 * GIB),
        ("1.5m", 1572864),
        ("512", 512),
        ("2t", 2 * 1024**4),
        ("3k", 3072),
    ],
)
def test_parse_size_reads_binary_units(text: str, expected: int) -> None:
    assert runner.parse_size(text) == expected


@pytest.mark.parametrize("text", ["10gb", "g", "-1g", "10 g", ""])
def test_parse_size_refuses_other_spellings(text: str) -> None:
    with pytest.raises(ValueError, match=r"^not a size: "):
        runner.parse_size(text)


@pytest.mark.parametrize(
    ("text", "expected"), [("168h", 604800), ("30m", 1800), ("5s", 5), (" 24h ", 86400)]
)
def test_parse_duration_reads_docker_until_durations(text: str, expected: int) -> None:
    assert runner.parse_duration(text) == expected


@pytest.mark.parametrize("text", ["1d", "h", "1.5h", "24"])
def test_parse_duration_refuses_units_docker_does_not_take(text: str) -> None:
    with pytest.raises(ValueError, match=r"^not a duration: "):
        runner.parse_duration(text)


@pytest.mark.parametrize(
    ("text", "expected"),
    [("03:30", (3, 30)), ("23:59", (23, 59)), ("00:00", (0, 0)), ("19:05", (19, 5))],
)
def test_parse_schedule_reads_hh_mm(text: str, expected: tuple[int, int]) -> None:
    assert runner.parse_schedule(text) == expected


@pytest.mark.parametrize("text", ["24:00", "3:30", "03:60", "0330"])
def test_parse_schedule_refuses_anything_else(text: str) -> None:
    with pytest.raises(ValueError, match=r"^not an HH:MM time: "):
        runner.parse_schedule(text)


def local(year: int, month: int, day: int, hour: int, minute: int, second: int = 0) -> float:
    return time.mktime((year, month, day, hour, minute, second, 0, 0, -1))


def test_next_run_is_later_today_before_the_hour() -> None:
    assert runner.next_run(local(2026, 6, 10, 2, 0), 3, 30) == local(2026, 6, 10, 3, 30)


def test_next_run_moves_to_tomorrow_at_and_after_the_hour() -> None:
    assert runner.next_run(local(2026, 6, 10, 3, 30), 3, 30) == local(2026, 6, 11, 3, 30)
    assert runner.next_run(local(2026, 6, 10, 3, 30, 1), 3, 30) == local(2026, 6, 11, 3, 30)
    assert runner.next_run(local(2026, 6, 10, 23, 59), 3, 30) == local(2026, 6, 11, 3, 30)


def test_next_run_rolls_over_the_month_end() -> None:
    assert runner.next_run(local(2026, 6, 30, 4, 0), 3, 30) == local(2026, 7, 1, 3, 30)


@pytest.mark.parametrize(
    ("text", "expected"), [("1.48", (1, 48)), ("1.9", (1, 9)), ("2", (2, 0)), (" 1.56 ", (1, 56))]
)
def test_api_version_compares_as_numbers(text: str, expected: tuple[int, int]) -> None:
    assert runner.api_version(text) == expected


def test_api_1_9_is_older_than_1_48_although_it_sorts_later_as_text() -> None:
    assert runner.build_prune_query("1.9", 5) == {"all": "true", "keep-storage": "5"}


def test_build_prune_uses_max_used_space_from_api_1_48() -> None:
    assert runner.build_prune_query("1.48", 5) == {"all": "true", "max-used-space": "5"}
    assert runner.build_prune_query("1.56", 5) == {"all": "true", "max-used-space": "5"}
    assert runner.build_prune_query("1.47", 5) == {"all": "true", "keep-storage": "5"}


def test_filters_encode_docker_filter_json() -> None:
    query = runner.filters(dangling="false", until="168h")
    assert json.loads(query["filters"]) == {"dangling": ["false"], "until": ["168h"]}


def test_used_image_ids_skip_containers_without_an_image() -> None:
    assert runner.used_image_ids([{"ImageID": "a"}, {"ImageID": ""}, {}, {"ImageID": "a"}]) == {"a"}


def test_removable_containers_are_stopped_and_created_before_the_cutoff() -> None:
    containers = [
        {"Id": 1, "State": "exited", "Created": 99},
        {"Id": 2, "State": "created", "Created": 10},
        {"Id": 3, "State": "dead", "Created": 10},
        {"Id": 4, "State": "running", "Created": 10},
        {"Id": 5, "State": "paused", "Created": 10},
        {"Id": 6, "State": "restarting", "Created": 10},
        {"Id": 7, "State": "exited", "Created": 100},
        {"Id": 8, "State": "exited", "Created": 101},
    ]
    assert [c["Id"] for c in runner.removable_containers(containers, 100)] == [1, 2, 3]


def test_removable_images_are_unused_and_created_before_the_cutoff() -> None:
    images = [
        {"Id": "used", "Created": 1},
        {"Id": "old", "Created": 99},
        {"Id": "edge", "Created": 100},
        {"Id": "new", "Created": 200},
    ]
    assert [i["Id"] for i in runner.removable_images(images, {"used"}, 100)] == ["old"]


def test_image_unique_bytes_subtracts_the_shared_part() -> None:
    assert runner.image_unique_bytes({"Size": 100, "SharedSize": 30}) == 70
    assert runner.image_unique_bytes({"Size": 100, "SharedSize": -1}) == 100
    assert runner.image_unique_bytes({"Size": 100}) == 100


def test_image_unique_bytes_never_goes_negative_when_shared_exceeds_size() -> None:
    assert runner.image_unique_bytes({"Size": 100, "SharedSize": 130}) == 0


def test_build_cache_bytes_leave_out_records_shared_with_images() -> None:
    entries = [{"Size": 5, "Shared": False}, {"Size": 7, "Shared": True}, {"Size": 11}]
    assert runner.build_cache_bytes(entries) == 16


def test_build_cache_reclaimable_is_the_excess_over_keep_bounded_by_idle_records() -> None:
    idle = {"Size": 25, "InUse": False}
    busy = {"Size": 5, "InUse": True}
    assert runner.build_cache_reclaimable([idle, busy], 10) == 20
    assert runner.build_cache_reclaimable([{"Size": 5}, {"Size": 25, "InUse": True}], 10) == 5
    assert runner.build_cache_reclaimable([{"Size": 5}], 10) == 0
    assert runner.build_cache_reclaimable([{"Size": 10}], 10) == 0


def test_removable_volumes_are_anonymous_and_unreferenced() -> None:
    anonymous = {"com.docker.volume.anonymous": ""}
    volumes = [
        {"Name": "a", "Labels": anonymous, "UsageData": {"RefCount": 0, "Size": 3}},
        {"Name": "b", "Labels": anonymous, "UsageData": {"RefCount": 1, "Size": 3}},
        {"Name": "c", "Labels": {}, "UsageData": {"RefCount": 0, "Size": 3}},
        {"Name": "d", "Labels": None, "UsageData": {"RefCount": 0}},
        {"Name": "e", "Labels": anonymous},
    ]
    assert [v["Name"] for v in runner.removable_volumes(volumes)] == ["a"]
    assert runner.volume_bytes(volumes[0]) == 3
    assert runner.volume_bytes({"UsageData": {"Size": -1}}) == 0
    assert runner.volume_bytes({}) == 0


def test_disk_used_percent_matches_df() -> None:
    assert runner.disk_used_percent(100, 20, 10) == 88.89
    assert runner.disk_used_percent(100, 100, 100) == 0.0
    assert runner.disk_used_percent(100, 0, 0) == 100.0
    assert runner.disk_used_percent(0, 0, 0) == 0.0


@pytest.mark.skipif(not hasattr(os.stat_result, "st_blocks"), reason="no st_blocks here")
def test_directory_bytes_counts_allocated_blocks(tmp_path: Path) -> None:
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "a.journal").write_bytes(b"x" * 10000)
    assert runner.directory_bytes(tmp_path) >= 10000


def test_last_success_reads_the_previous_textfile() -> None:
    text = (
        'ayeops_hygiene_images{phase="after"} 3\n'
        "ayeops_hygiene_last_success_timestamp_seconds 1700\n"
    )
    assert runner.last_success(text) == 1700.0
    assert runner.last_success("ayeops_hygiene_last_success_timestamp_seconds nan-ish\n") is None
    assert runner.last_success("other_last_success_timestamp_seconds 5\n") is None
    assert runner.last_success("") is None


def test_previous_success_falls_back_when_there_is_no_file(tmp_path: Path) -> None:
    assert runner.previous_success(tmp_path / "missing.prom", 42.0) == 42.0
    path = tmp_path / "h.prom"
    path.write_text("ayeops_hygiene_last_success_timestamp_seconds 7\n", encoding="utf-8")
    assert runner.previous_success(path, 42.0) == 7.0
    path.write_text("nothing\n", encoding="utf-8")
    assert runner.previous_success(path, 42.0) == 42.0


def test_metric_formats_integers_and_sorted_labels() -> None:
    assert runner.metric("x", 3.0) == "ayeops_hygiene_x 3\n"
    assert (
        runner.metric("x", 2.5, phase="a", kind="b") == 'ayeops_hygiene_x{kind="b",phase="a"} 2.5\n'
    )


def test_settings_come_from_the_environment_with_defaults() -> None:
    defaults = runner.settings_from({})
    assert defaults == runner.Settings()
    assert (defaults.volumes, defaults.dry_run, defaults.journal) == (False, False, None)
    custom = runner.settings_from(
        {
            "AYE_HYGIENE_SCHEDULE": "04:15",
            "AYE_HYGIENE_IMAGES_UNTIL": "72h",
            "AYE_HYGIENE_CONTAINERS_UNTIL": "2h",
            "AYE_HYGIENE_BUILD_CACHE_KEEP": "20g",
            "AYE_HYGIENE_VOLUMES": "true",
            "AYE_HYGIENE_DRY_RUN": "1",
            "AYE_HYGIENE_TEXTFILE": "/t/h.prom",
            "AYE_HYGIENE_DISK": "",
            "AYE_HYGIENE_JOURNAL": "/journal",
        }
    )
    assert custom == runner.Settings(
        schedule=(4, 15),
        images_until="72h",
        containers_until="2h",
        build_cache_keep=20 * GIB,
        volumes=True,
        dry_run=True,
        textfile=Path("/t/h.prom"),
        disk=None,
        journal=Path("/journal"),
    )


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("1", True),
        ("TRUE", True),
        ("yes", True),
        ("on", True),
        ("0", False),
        ("false", False),
        ("", False),
        (None, False),
    ],
)
def test_flag_reads_compose_booleans(value: str | None, expected: bool) -> None:
    assert runner.flag(value) is expected


def test_a_live_run_prunes_containers_then_images_then_build_cache() -> None:
    api = docker()
    report = runner.run_once(api, settings(), clock(NOW, NOW + 5))
    posts = [(path, query) for method, path, query in api.calls if method == "POST"]
    assert posts == [
        ("/containers/prune", {"filters": '{"until": ["24h"]}'}),
        ("/images/prune", {"filters": '{"dangling": ["false"], "until": ["168h"]}'}),
        ("/build/prune", {"all": "true", "max-used-space": str(10 * GIB)}),
    ]
    assert report.success and report.error == ""
    assert report.bytes == {"containers": 40, "images": 300, "build_cache": 20 * GIB}
    assert report.counts == {"containers": 1, "images": 2, "build_cache": 3}
    assert report.before == runner.Snapshot(images=3, build_cache=30 * GIB, disk_used=None)
    assert (report.started, report.finished) == (NOW, NOW + 5)


def test_volumes_are_pruned_only_when_enabled() -> None:
    api = docker()
    report = runner.run_once(api, settings(volumes=True), clock(NOW, NOW))
    assert api.calls[-2][:2] == ("POST", "/volumes/prune")
    assert api.calls[-2][2] == {}
    assert (report.bytes["volumes"], report.counts["volumes"]) == (7, 1)


def test_an_old_daemon_gets_keep_storage() -> None:
    api = docker(version="1.43")
    runner.run_once(api, settings(), clock(NOW, NOW))
    build = [query for method, path, query in api.calls if path == "/build/prune"]
    assert build == [{"all": "true", "keep-storage": str(10 * GIB)}]


def test_a_dry_run_never_posts_and_estimates_what_a_run_would_free() -> None:
    api = docker()
    report = runner.run_once(api, settings(dry_run=True, volumes=True), clock(NOW, NOW + 1))
    assert {method for method, _, _ in api.calls} == {"GET"}
    assert report.success and report.dry_run
    assert report.counts == {"containers": 1, "images": 1, "volumes": 1}
    assert report.bytes == {"containers": 40, "images": 300, "build_cache": 20 * GIB, "volumes": 7}
    assert report.after == report.before


def test_a_failing_prune_is_reported_not_raised() -> None:
    api = docker()
    api.fail = "/images/prune"
    report = runner.run_once(api, settings(), clock(NOW, NOW + 2))
    assert not report.success
    assert report.error == "RuntimeError: /images/prune refused"
    assert report.after is None
    assert ("POST", "/build/prune") not in [(m, p) for m, p, _ in api.calls]
    assert report.finished == NOW + 2


def test_render_metrics_of_a_live_run() -> None:
    report = runner.run_once(docker(), settings(), clock(NOW, NOW + 5))
    text = runner.render_metrics(report, settings(), NOW + 5)
    lines = set(text.splitlines())
    assert "ayeops_hygiene_last_success_timestamp_seconds 2000000005" in lines
    assert "ayeops_hygiene_last_run_timestamp_seconds 2000000005" in lines
    assert "ayeops_hygiene_last_run_success 1" in lines
    assert "ayeops_hygiene_run_duration_seconds 5" in lines
    assert "ayeops_hygiene_dry_run 0" in lines
    assert f"ayeops_hygiene_build_cache_keep_bytes {10 * GIB}" in lines
    assert 'ayeops_hygiene_reclaimed_bytes{kind="images"} 300' in lines
    assert 'ayeops_hygiene_removed{kind="build_cache"} 3' in lines
    assert 'ayeops_hygiene_images{phase="before"} 3' in lines
    assert f'ayeops_hygiene_build_cache_bytes{{phase="after"}} {30 * GIB}' in lines
    assert not any("reclaimable" in line or "volumes" in line for line in lines)
    assert not any("disk_used" in line or "journal" in line for line in lines)


def test_render_metrics_of_a_dry_run_name_the_estimate_apart() -> None:
    report = runner.run_once(docker(), settings(dry_run=True), clock(NOW, NOW))
    report.journal = 1234
    report.before = runner.Snapshot(images=3, build_cache=1, disk_used=55.5)
    text = runner.render_metrics(report, settings(), NOW - 9)
    lines = set(text.splitlines())
    assert "ayeops_hygiene_dry_run 1" in lines
    assert 'ayeops_hygiene_reclaimable_bytes{kind="build_cache"} 21474836480' in lines
    assert 'ayeops_hygiene_removable{kind="images"} 1' in lines
    assert 'ayeops_hygiene_disk_used_percent{phase="before"} 55.5' in lines
    assert "ayeops_hygiene_journal_bytes 1234" in lines
    assert "ayeops_hygiene_last_success_timestamp_seconds 1999999991" in lines
    assert not any("reclaimed" in line or "removed" in line for line in lines)


def test_record_keeps_the_last_success_when_a_run_fails(tmp_path: Path) -> None:
    textfile = tmp_path / "ayeops_hygiene.prom"
    api = docker()
    api.fail = "/build/prune"
    assert runner.record(api, settings(textfile=textfile), 123.0) == 123.0
    text = textfile.read_text(encoding="utf-8")
    assert "ayeops_hygiene_last_success_timestamp_seconds 123\n" in text
    assert "ayeops_hygiene_last_run_success 0\n" in text
    assert not (tmp_path / "ayeops_hygiene.prom.tmp").exists()


def test_record_moves_the_last_success_on_a_good_run(tmp_path: Path) -> None:
    textfile = tmp_path / "ayeops_hygiene.prom"
    after = runner.record(docker(), settings(textfile=textfile), 123.0)
    assert after > 123.0
    assert runner.last_success(textfile.read_text(encoding="utf-8")) == float(round(after))


def test_startup_metrics_start_the_stale_clock() -> None:
    text = runner.startup_metrics(settings(), 77.0)
    assert text == (
        "ayeops_hygiene_last_success_timestamp_seconds 77\n"
        f"ayeops_hygiene_build_cache_keep_bytes {10 * GIB}\n"
    )


def compose_of(data: dict[str, Any], host: str, modules: dict[str, Module]) -> dict[str, Any]:
    return host_compose(manifest_from(data), host, modules)


def test_the_example_is_valid_with_hygiene(data: dict[str, Any]) -> None:
    assert rules_hit(data) == set()


def test_hygiene_service_reaches_the_socket_without_a_network(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    service = compose_of(data, "edge", modules)["services"]["hygiene"]
    assert service["network_mode"] == "none"
    assert service["read_only"] is True
    assert service["cap_drop"] == ["ALL"]
    assert "/var/run/docker.sock:/var/run/docker.sock" in service["volumes"]
    assert "/etc/localtime:/etc/localtime:ro" in service["volumes"]
    assert "/var/lib/docker:/disk:ro" in service["volumes"]
    assert "/var/log/journal:/journal:ro" in service["volumes"]
    assert "/var/lib/ayeops/textfile:/textfile" in service["volumes"]
    assert service["environment"] == {
        "AYE_HYGIENE_SCHEDULE": "03:30",
        "AYE_HYGIENE_IMAGES_UNTIL": "168h",
        "AYE_HYGIENE_CONTAINERS_UNTIL": "24h",
        "AYE_HYGIENE_BUILD_CACHE_KEEP": "10g",
        "AYE_HYGIENE_VOLUMES": "false",
        "AYE_HYGIENE_DRY_RUN": "false",
        "AYE_HYGIENE_TEXTFILE": "/textfile/ayeops_hygiene.prom",
        "AYE_HYGIENE_DISK": "/disk",
        "AYE_HYGIENE_JOURNAL": "/journal",
    }
    assert runner.settings_from(service["environment"]) == runner.Settings(journal=Path("/journal"))


def test_hygiene_settings_reach_the_runner(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    data["hosts"]["edge"]["modules"]["node"]["textfile_dir"] = "/var/lib/ayeops/textfile"
    data["hosts"]["edge"]["modules"]["hygiene"] = {
        "schedule": "05:10",
        "build_cache_keep": "20g",
        "volumes": True,
        "dry_run": True,
        "journal_dir": None,
        "disk_path": "/srv/docker",
    }
    service = compose_of(data, "edge", modules)["services"]["hygiene"]
    environment = service["environment"]
    assert (environment["AYE_HYGIENE_VOLUMES"], environment["AYE_HYGIENE_DRY_RUN"]) == (
        "true",
        "true",
    )
    assert environment["AYE_HYGIENE_JOURNAL"] == ""
    assert not any(v.endswith(":/journal:ro") for v in service["volumes"])
    assert "/srv/docker:/disk:ro" in service["volumes"]
    assert "/var/lib/ayeops/textfile:/textfile" in service["volumes"]
    parsed = runner.settings_from(environment)
    assert (parsed.schedule, parsed.build_cache_keep, parsed.journal) == ((5, 10), 20 * GIB, None)


def test_hygiene_requires_node_for_its_textfile(data: dict[str, Any]) -> None:
    del data["hosts"]["edge"]["modules"]["node"]
    data["hosts"]["edge"]["modules"].pop("disk-forecast")
    assert "module-requires" in rules_hit(data)


def test_hygiene_ships_its_runner(data: dict[str, Any], modules: dict[str, Module]) -> None:
    tree = render_once(manifest_from(data), modules)
    assert tree["edge/hygiene/hygiene_runner.py"] == RUNNER.read_text(encoding="utf-8").replace(
        "\r\n", "\n"
    )


def every_builtin_service(data: dict[str, Any], modules: dict[str, Module]) -> dict[str, Any]:
    with_checks(data)
    data["hosts"]["edge"]["modules"]["probes"] = {"http": ["https://a.example"]}
    return {
        f"{host}/{name}": service
        for host in data["hosts"]
        for name, service in compose_of(data, host, modules)["services"].items()
    }


def test_every_rendered_service_rotates_its_docker_logs(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    services = every_builtin_service(data, modules)
    assert {s["labels"]["io.ayeops.module"] for s in services.values()} >= {
        "metrics",
        "logs",
        "node",
        "containers",
        "checks",
        "probes",
        "hygiene",
        "grafana",
    }
    for name, service in services.items():
        assert service["logging"] == {
            "driver": "json-file",
            "options": {"max-size": "10m", "max-file": "3"},
        }, name


def test_services_do_not_share_one_logging_dict(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    services = list(compose_of(data, "edge", modules)["services"].values())
    services[0]["logging"]["options"]["max-size"] = "1g"
    assert services[1]["logging"]["options"]["max-size"] == "10m"
    assert DEFAULT_LOGGING["options"] == {"max-size": "10m", "max-file": "3"}


def test_a_service_or_module_logging_overrides_the_default(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    manifest = manifest_from(data)
    node = modules["node"]
    journald = {"driver": "journald"}
    own = finalize_service(manifest, "edge", node, {}, "node-exporter", {"logging": journald})
    assert own["logging"] == journald
    local = {"driver": "local", "options": {"max-size": "50m"}}
    module = Module("demo", node.path, {**node.spec, "name": "demo", "logging": local})
    assert finalize_service(manifest, "edge", module, {}, "node-exporter", {})["logging"] == local


def test_next_run_keeps_wall_clock_time_in_winter() -> None:
    assert runner.next_run(local(2026, 1, 15, 1, 0), 3, 30) == local(2026, 1, 15, 3, 30)


def test_missing_fields_count_as_zero() -> None:
    assert runner.removable_containers([{"State": "exited"}], 1) == [{"State": "exited"}]
    assert runner.removable_images([{"Id": "x"}], set(), 1) == [{"Id": "x"}]
    assert runner.image_unique_bytes({}) == 0
    assert runner.build_cache_bytes([{}, {"Size": None}]) == 0


def test_a_tiny_full_filesystem_is_full() -> None:
    assert runner.disk_used_percent(1, 0, 0) == 100.0


class Stat:
    def __init__(self, size: int, blocks: int | None) -> None:
        self.st_size = size
        if blocks is not None:
            self.st_blocks = blocks


def test_allocated_bytes_prefers_blocks_and_falls_back_to_size() -> None:
    assert runner.allocated_bytes(Stat(10, 3)) == 1536
    assert runner.allocated_bytes(Stat(10, None)) == 10
    assert runner.allocated_bytes(Stat(10, 0)) == 0


def test_directory_bytes_walks_subdirectories_and_skips_unreadable_files(tmp_path: Path) -> None:
    (tmp_path / "sub").mkdir()
    for name in ("a", "b", "sub/c"):
        (tmp_path / name).write_text("x", encoding="utf-8")
    sizes = {"a": Stat(0, None), "b": Stat(0, 2), "c": Stat(7, None)}

    def lstat(path: Path) -> Stat:
        if path.name == "a":
            raise PermissionError(path)
        return sizes[path.name]

    assert runner.directory_bytes(tmp_path, lstat) == 1031
    assert runner.directory_bytes(tmp_path / "missing", lstat) == 0


class Vfs:
    f_blocks = 100
    f_bfree = 20
    f_bavail = 10


def test_disk_usage_reads_statvfs_of_the_path() -> None:
    seen: list[Path] = []

    def statvfs(path: Path) -> Vfs:
        seen.append(path)
        return Vfs()

    assert runner.disk_usage(Path("/disk"), statvfs) == 88.89
    assert seen == [Path("/disk")]
    assert runner.disk_usage(None, statvfs) is None
    assert runner.disk_usage(Path("/disk"), None) is None


def test_disk_usage_of_a_missing_path_is_unknown() -> None:
    def statvfs(path: Path) -> Vfs:
        raise FileNotFoundError(path)

    assert runner.disk_usage(Path("/nope"), statvfs) is None


def gets(api: FakeApi) -> list[tuple[str, dict[str, str]]]:
    return [(path, query) for method, path, query in api.calls if method == "GET"]


def test_a_live_run_asks_only_for_image_and_build_cache_usage() -> None:
    api = docker()
    runner.run_once(api, settings(), clock(NOW, NOW))
    assert gets(api) == [
        ("/version", {}),
        ("/system/df?type=image&type=build-cache", {}),
        ("/system/df?type=image&type=build-cache", {}),
    ]


def test_a_dry_run_lists_containers_with_sizes_and_images_with_shared_sizes() -> None:
    api = docker()
    runner.run_once(api, settings(dry_run=True, volumes=True), clock(NOW, NOW))
    assert gets(api) == [
        ("/version", {}),
        ("/system/df?type=image&type=build-cache", {}),
        ("/containers/json", {"all": "true", "size": "true"}),
        ("/images/json", {"shared-size": "true"}),
        ("/system/df?type=build-cache", {}),
        ("/system/df?type=volume", {}),
    ]


def test_a_daemon_without_api_version_is_treated_as_old() -> None:
    api = docker()
    api.responses["GET /version"] = {}
    report = runner.run_once(api, settings(), clock(NOW, NOW))
    assert report.success
    assert [q for _, p, q in api.calls if p == "/build/prune"] == [
        {"all": "true", "keep-storage": str(10 * GIB)}
    ]


def test_a_daemon_that_does_not_answer_leaves_no_snapshot() -> None:
    api = docker()
    api.fail = "/version"
    report = runner.run_once(api, settings(), clock(NOW, NOW + 1))
    assert (report.success, report.before, report.after) == (False, None, None)
    assert (report.bytes, report.counts) == ({}, {})


def test_prune_answers_without_numbers_count_as_nothing_freed() -> None:
    api = docker()
    for path in ("/containers/prune", "/images/prune", "/build/prune"):
        api.responses[f"POST {path}"] = {}
    api.responses["GET /containers/json"] = [{"State": "exited", "Created": 1}]
    report = runner.run_once(api, settings(), clock(NOW, NOW))
    assert report.bytes == {"containers": 0, "images": 0, "build_cache": 0}
    assert report.counts == {"containers": 0, "images": 0, "build_cache": 0}
    dry = runner.run_once(api, settings(dry_run=True), clock(NOW, NOW))
    assert dry.bytes["containers"] == 0


def test_a_run_measures_the_journal_even_when_it_fails(tmp_path: Path) -> None:
    (tmp_path / "system.journal").write_bytes(b"x" * 4096)
    api = docker()
    api.fail = "/version"
    report = runner.run_once(api, settings(journal=tmp_path), clock(NOW, NOW))
    assert report.journal is not None and report.journal >= 4096


def test_run_duration_keeps_milliseconds() -> None:
    report = runner.Report(started=0.0, dry_run=False, finished=1.23456)
    text = runner.render_metrics(report, settings(), 0.0)
    assert "ayeops_hygiene_run_duration_seconds 1.235\n" in text


def test_every_kind_reaches_the_textfile() -> None:
    report = runner.run_once(docker(), settings(volumes=True), clock(NOW, NOW))
    lines = set(runner.render_metrics(report, settings(), NOW).splitlines())
    for kind in ("containers", "images", "build_cache", "volumes"):
        assert f'ayeops_hygiene_reclaimed_bytes{{kind="{kind}"}} {report.bytes[kind]}' in lines
        assert f'ayeops_hygiene_removed{{kind="{kind}"}} {report.counts[kind]}' in lines


def test_the_temporary_file_is_not_a_prom_file_node_exporter_would_read() -> None:
    path = Path("/textfile/ayeops_hygiene.prom")
    assert runner.temporary_path(path) == Path("/textfile/ayeops_hygiene.prom.tmp")


def test_serve_sleeps_in_naps_until_the_schedule_and_then_records(tmp_path: Path) -> None:
    start = local(2026, 6, 10, 3, 28)
    due = local(2026, 6, 10, 3, 30)
    times = [start, start, due, due, due + 5, due + 6]
    slept: list[float] = []
    turns = [True, True, False]
    heartbeat = tmp_path / "alive"
    textfile = tmp_path / "ayeops_hygiene.prom"
    success = runner.serve(
        docker(),
        settings(textfile=textfile),
        1.0,
        wall=lambda: times.pop(0),
        sleep=slept.append,
        heartbeat=heartbeat,
        going=lambda: turns.pop(0),
    )
    assert slept == [60]
    assert success == due + 5
    assert heartbeat.exists()
    assert times == []
    assert runner.last_success(textfile.read_text(encoding="utf-8")) == round(due + 5)


def test_serve_never_sleeps_past_the_schedule(tmp_path: Path) -> None:
    start = local(2026, 6, 10, 3, 29, 50)
    slept: list[float] = []
    turns = [True, False]
    runner.serve(
        docker(),
        settings(textfile=tmp_path / "h.prom"),
        1.0,
        wall=lambda: start,
        sleep=slept.append,
        heartbeat=tmp_path / "alive",
        going=lambda: turns.pop(0),
    )
    assert slept == [10]


def test_main_once_prunes_and_writes_the_textfile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = docker()
    sockets: list[str] = []

    def connect(path: str) -> FakeApi:
        sockets.append(path)
        return api

    monkeypatch.setattr(runner, "docker_api", connect)
    textfile = tmp_path / "ayeops_hygiene.prom"
    environ = {
        "AYE_HYGIENE_ONCE": "1",
        "AYE_HYGIENE_TEXTFILE": str(textfile),
        "AYE_HYGIENE_DISK": "",
    }
    assert runner.main(environ) == 0
    assert sockets == ["/var/run/docker.sock"]
    assert "ayeops_hygiene_last_run_success 1\n" in textfile.read_text(encoding="utf-8")


def test_the_healthcheck_watches_the_heartbeat_the_runner_touches(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    service = compose_of(data, "edge", modules)["services"]["hygiene"]
    assert runner.HEARTBEAT.as_posix() == "/tmp/hygiene.alive"
    assert runner.HEARTBEAT.as_posix() in service["healthcheck"]["test"][-1]
    assert runner.NAP * 5 <= 300


def test_statvfs_is_the_os_one() -> None:
    assert runner.STATVFS is getattr(os, "statvfs", None)


def test_request_target_appends_the_query() -> None:
    assert runner.request_target("/version", {}) == "/version"
    assert runner.request_target("/images/json", {"a": "1", "b": "x y"}) == "/images/json?a=1&b=x+y"
    assert runner.request_target("/system/df?type=image", {"a": "1"}) == "/system/df?type=image&a=1"


class FakeResponse:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self.body = body

    def read(self) -> bytes:
        return self.body


class FakeConnection:
    made: ClassVar[list[FakeConnection]] = []

    def __init__(self, socket_path: str, timeout: float) -> None:
        self.socket_path = socket_path
        self.timeout = timeout
        self.sent: list[tuple[str, str]] = []
        self.closed = False
        self.reply = FakeResponse(200, b'{"ApiVersion": "1.56"}')
        FakeConnection.made.append(self)

    def request(self, method: str, target: str) -> None:
        self.sent.append((method, target))

    def getresponse(self) -> FakeResponse:
        return self.reply

    def close(self) -> None:
        self.closed = True


def test_docker_api_sends_one_request_per_call_and_decodes_json() -> None:
    FakeConnection.made = []
    api = runner.docker_api("/run/docker.sock", connect=FakeConnection)
    assert api("GET", "/version", {"a": "1"}) == {"ApiVersion": "1.56"}
    (connection,) = FakeConnection.made
    assert (connection.socket_path, connection.timeout) == ("/run/docker.sock", 1800.0)
    assert connection.sent == [("GET", "/version?a=1")]
    assert connection.closed is True


def test_docker_api_closes_and_raises_on_an_error_status() -> None:
    class Refusing(FakeConnection):
        def __init__(self, socket_path: str, timeout: float) -> None:
            super().__init__(socket_path, timeout)
            self.reply = FakeResponse(400, b"x" * 400)

    FakeConnection.made = []
    api = runner.docker_api("/s", connect=Refusing)
    with pytest.raises(RuntimeError) as raised:
        api("POST", "/images/prune", {})
    assert str(raised.value) == f"POST /images/prune: 400 {b'x' * 300!r}"
    assert FakeConnection.made[0].closed is True


def test_docker_api_passes_399_and_empty_bodies() -> None:
    class Odd(FakeConnection):
        def __init__(self, socket_path: str, timeout: float) -> None:
            super().__init__(socket_path, timeout)
            self.reply = FakeResponse(399, b"")

    assert runner.docker_api("/s", connect=Odd)("GET", "/x", {}) is None


def test_docker_api_raises_on_401() -> None:
    class Unauthorized(FakeConnection):
        def __init__(self, socket_path: str, timeout: float) -> None:
            super().__init__(socket_path, timeout)
            self.reply = FakeResponse(401, b"no")

    with pytest.raises(RuntimeError, match=r"^GET /x: 401 b'no'$"):
        runner.docker_api("/s", connect=Unauthorized)("GET", "/x", {})


def test_unix_connection_keeps_the_socket_path() -> None:
    connection = runner.UnixConnection("/var/run/docker.sock", 5.0)
    assert (connection.host, connection.socket_path, connection.timeout) == (
        "localhost",
        "/var/run/docker.sock",
        5.0,
    )


@pytest.mark.skipif(sys.platform != "win32", reason="windows has no unix sockets here")
def test_unix_connection_refuses_on_windows() -> None:
    with pytest.raises(OSError, match=r"^the docker socket needs a unix host$"):
        runner.UnixConnection("/var/run/docker.sock", 5.0).connect()


def test_docker_api_talks_http_over_a_unix_socket(tmp_path: Path) -> None:
    import socket
    import threading

    if sys.platform == "win32":
        pytest.skip("needs unix sockets")
    path = str(tmp_path / "docker.sock")
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(path)
    server.listen(1)
    seen: list[bytes] = []

    def answer() -> None:
        client, _ = server.accept()
        seen.append(client.recv(65536))
        body = b'{"ok": true}'
        client.sendall(
            b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
            + f"Content-Length: {len(body)}\r\n\r\n".encode()
            + body
        )
        client.close()

    thread = threading.Thread(target=answer)
    thread.start()
    assert runner.docker_api(path, timeout=WAIT)("GET", "/version", {"a": "1"}) == {"ok": True}
    thread.join(WAIT)
    server.close()
    assert seen[0].startswith(b"GET /version?a=1 HTTP/1.1\r\n")


def test_log_prints_one_sorted_json_line(capsys: pytest.CaptureFixture[str]) -> None:
    report = runner.Report(
        started=10.0,
        dry_run=True,
        finished=12.26,
        success=True,
        before=runner.Snapshot(images=3, build_cache=4, disk_used=5.0),
        bytes={"images": 1},
        counts={"images": 2},
        journal=9,
        error="e",
    )
    runner.log(report)
    out = capsys.readouterr().out
    assert out.count("\n") == 1
    assert out == json.dumps(json.loads(out), sort_keys=True) + "\n"
    assert json.loads(out) == {
        "after": None,
        "before": {"build_cache": 4, "disk_used": 5.0, "images": 3},
        "bytes": {"images": 1},
        "counts": {"images": 2},
        "dry_run": True,
        "error": "e",
        "journal_bytes": 9,
        "seconds": 2.3,
        "success": True,
    }


def test_log_seconds_round_to_tenths(capsys: pytest.CaptureFixture[str]) -> None:
    runner.log(runner.Report(started=1.0, dry_run=False, finished=3.06))
    assert json.loads(capsys.readouterr().out)["seconds"] == 2.1


def test_main_without_once_writes_the_startup_file_then_serves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sockets: list[str] = []
    served: list[tuple[float, str]] = []
    textfile = tmp_path / "ayeops_hygiene.prom"

    def connect(path: str) -> FakeApi:
        sockets.append(path)
        return docker()

    def serve(api: Any, settings: Any, success_at: float) -> float:
        served.append((success_at, textfile.read_text(encoding="utf-8")))
        return success_at

    monkeypatch.setattr(runner, "docker_api", connect)
    monkeypatch.setattr(runner, "serve", serve)
    environ = {"AYE_HYGIENE_TEXTFILE": str(textfile), "AYE_HYGIENE_SOCKET": "/s.sock"}
    assert runner.main(environ) == 0
    assert sockets == ["/s.sock"]
    ((success_at, text),) = served
    assert text == runner.startup_metrics(runner.settings_from(environ), success_at)


def test_main_keeps_a_previous_textfile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    textfile = tmp_path / "ayeops_hygiene.prom"
    textfile.write_text(
        "ayeops_hygiene_last_success_timestamp_seconds 5\nkeep 1\n", encoding="utf-8"
    )
    served: list[float] = []
    monkeypatch.setattr(runner, "docker_api", lambda path: docker())

    def serve(api: Any, settings: Any, at: float) -> float:
        served.append(at)
        return at

    monkeypatch.setattr(runner, "serve", serve)
    assert runner.main({"AYE_HYGIENE_TEXTFILE": str(textfile)}) == 0
    assert served == [5.0]
    assert "keep 1\n" in textfile.read_text(encoding="utf-8")


def test_serve_runs_until_told_to_stop_by_default() -> None:
    import inspect

    assert inspect.signature(runner.serve).parameters["going"].default() is True


def test_serve_keeps_going_after_a_run(tmp_path: Path) -> None:
    due = local(2026, 6, 10, 3, 30)
    times = [due - 1, due, due, due + 1, due + 2, due + 3]
    slept: list[float] = []
    turns = [True, True, False]
    runner.serve(
        docker(),
        settings(textfile=tmp_path / "h.prom"),
        1.0,
        wall=lambda: times.pop(0),
        sleep=slept.append,
        heartbeat=tmp_path / "alive",
        going=lambda: turns.pop(0),
    )
    assert times == []
    assert slept == [60]


def test_the_runner_runs_as_a_script(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import runpy

    textfile = tmp_path / "ayeops_hygiene.prom"
    monkeypatch.setenv("AYE_HYGIENE_ONCE", "1")
    monkeypatch.setenv("AYE_HYGIENE_TEXTFILE", str(textfile))
    monkeypatch.setenv("AYE_HYGIENE_DISK", "")
    monkeypatch.setenv("AYE_HYGIENE_SOCKET", str(tmp_path / "missing.sock"))
    with pytest.raises(SystemExit) as raised:
        runpy.run_path(str(RUNNER), run_name="__main__")
    assert raised.value.code == 0
    assert "ayeops_hygiene_last_run_success 0\n" in textfile.read_text(encoding="utf-8")
