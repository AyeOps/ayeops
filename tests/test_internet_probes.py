from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from ayeops.discover import probe
from ayeops.discover.suggest import suggest
from ayeops.portal.graph import is_probe_job
from ayeops.registry import MODULES_DIR, Module
from ayeops.render import host_compose, host_scrape
from tests.conftest import ROOT, WAIT, manifest_from, render_once, rules_hit

ROUTE = ROOT / "tests" / "fixtures" / "discover" / "proc-net-route.txt"


def edge_probes(data: dict[str, Any], **settings: Any) -> dict[str, Any]:
    data["hosts"]["edge"]["modules"]["probes"] = settings
    return data


def jobs_of(data: dict[str, Any], modules: dict[str, Module]) -> dict[str, dict[str, Any]]:
    scrape = host_scrape(manifest_from(data), "edge", modules)
    assert scrape is not None
    return {job["job_name"]: job for job in scrape["scrape_configs"]}


def test_internet_probes_ping_the_gateway_and_public_addresses_and_ask_resolvers(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    jobs = jobs_of(edge_probes(data, internet=True, gateway="192.168.1.1"), modules)
    icmp = jobs["blackbox-icmp"]
    assert icmp["params"] == {"module": ["icmp"]}
    assert icmp["scrape_interval"] == icmp["scrape_timeout"] == "15s"
    assert icmp["static_configs"] == [
        {"targets": ["192.168.1.1"], "labels": {"layer": "gateway"}},
        {"targets": ["1.1.1.1", "8.8.8.8"], "labels": {"layer": "internet"}},
    ]
    assert icmp["relabel_configs"][-1] == {
        "target_label": "__address__",
        "replacement": "blackbox:9115",
    }
    dns = jobs["blackbox-dns"]
    assert dns["params"] == {"module": ["dns"]}
    assert dns["static_configs"] == [
        {"targets": ["1.1.1.1", "8.8.8.8"], "labels": {"layer": "dns"}}
    ]
    assert "blackbox" in jobs
    assert "blackbox-http" not in jobs


def test_a_gateway_alone_pings_only_the_gateway(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    jobs = jobs_of(edge_probes(data, gateway="10.0.0.1", internet_interval="30s"), modules)
    assert jobs["blackbox-icmp"]["static_configs"] == [
        {"targets": ["10.0.0.1"], "labels": {"layer": "gateway"}}
    ]
    assert jobs["blackbox-icmp"]["scrape_interval"] == "30s"
    assert "blackbox-dns" not in jobs


def test_blackbox_config_carries_only_the_probers_in_use(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    def config(**settings: Any) -> dict[str, Any]:
        tree = render_once(manifest_from(edge_probes(data, **settings)), modules)
        loaded: dict[str, Any] = yaml.safe_load(tree["edge/probes/blackbox.yml"])["modules"]
        return loaded

    full = config(internet=True, dns_query="example.org")
    assert set(full) == {"http_2xx", "icmp", "dns"}
    assert full["icmp"]["prober"] == "icmp"
    assert full["dns"]["dns"]["query_name"] == "example.org"
    assert full["dns"]["dns"]["valid_rcodes"] == ["NOERROR"]
    assert set(config(gateway="10.0.0.1")) == {"http_2xx", "icmp"}
    assert set(config(http=["https://acme.example"])) == {"http_2xx"}


def test_blackbox_may_ping_unprivileged_only_when_it_pings(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    services = host_compose(manifest_from(edge_probes(data, internet=True)), "edge", modules)
    sysctls = services["services"]["blackbox"]["sysctls"]
    assert sysctls == {"net.ipv4.ping_group_range": "0 2147483647"}
    del data["hosts"]["central"]["modules"]["probes"]["links"]
    central = host_compose(manifest_from(data), "central", modules)["services"]["blackbox"]
    assert "sysctls" not in central


def test_without_urls_internet_or_gateway_no_blackbox_runs(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    data = edge_probes(data, domains=["acme.example"])
    services = host_compose(manifest_from(data), "edge", modules)["services"]
    assert "blackbox" not in services
    assert "rdap-exporter" in services
    assert not {"blackbox", "blackbox-icmp", "blackbox-dns"} & set(jobs_of(data, modules))
    assert "edge/probes/blackbox.yml" not in render_once(manifest_from(data), modules)


@pytest.mark.parametrize(
    "settings",
    [
        {"gateway": "router.lan"},
        {"gateway": "192.168.1"},
        {"internet_targets": []},
        {"dns_servers": ["1.1.1.1", "1.1.1.1"]},
        {"dns_query": "not a name"},
        {"internet_interval": "15"},
        {"internet": "yes"},
    ],
)
def test_internet_settings_are_validated(data: dict[str, Any], settings: dict[str, Any]) -> None:
    assert "module-settings" in rules_hit(edge_probes(data, **settings))


def test_internet_probes_pass_the_catalog_and_portal_checks(data: dict[str, Any]) -> None:
    assert rules_hit(edge_probes(data, internet=True, gateway="192.168.1.1")) == set()


def test_probe_jobs_are_not_mistaken_for_scraped_services() -> None:
    address = {"target_label": "__address__", "replacement": "blackbox:9115"}
    assert is_probe_job({"relabel_configs": [{"target_label": "instance"}, address]})
    assert not is_probe_job({"relabel_configs": [{"target_label": "__address__"}]})
    assert not is_probe_job({"relabel_configs": [{"target_label": "instance", "replacement": "x"}]})
    assert not is_probe_job({"static_configs": [{"targets": ["node-exporter:9100"]}]})


def test_the_default_route_with_the_lowest_metric_names_the_gateway() -> None:
    assert probe.parse_proc_route(ROUTE.read_text(encoding="utf-8")) == "192.168.1.1"


def test_no_default_route_through_a_gateway_means_no_gateway() -> None:
    lines = ROUTE.read_text(encoding="utf-8").splitlines()
    header, rows = lines[0], lines[1:]
    wlan = rows[0]
    assert probe.parse_proc_route("\n".join([header, wlan])) == "192.168.2.1"
    assert probe.parse_proc_route("\n".join([wlan])) is None
    assert probe.parse_proc_route("\n".join([header, *rows[2:]])) is None
    assert probe.parse_proc_route("") is None
    eight = "\t".join(wlan.split("\t")[:8])
    assert probe.parse_proc_route("\n".join([header, eight])) == "192.168.2.1"
    assert probe.parse_proc_route(header + "\nwlan0 00000000 0102A8C0 0003 0 0 600") is None
    assert probe.parse_proc_route("\n".join([header, wlan.replace("\t0003\t", "\t0002\t")])) is None
    assert probe.parse_proc_route("\n".join([header, wlan.replace("\t0003\t", "\t0001\t")])) is None
    gateway_host = wlan.replace("\t600\t00000000", "\t600\tFFFFFFFF")
    assert probe.parse_proc_route("\n".join([header, gateway_host])) is None
    other_net = wlan.replace("wlan0\t00000000", "wlan0\t0001A8C0")
    assert probe.parse_proc_route("\n".join([header, other_net])) is None


def test_discover_reads_the_gateway_from_the_route_table(tmp_path: Path) -> None:
    host = probe.Host(root=tmp_path, run=lambda argv: probe.Result(1, "", ""), machine="x86_64")
    assert probe.gateway_info(host, {}) is None
    (tmp_path / "proc" / "net").mkdir(parents=True)
    (tmp_path / "proc" / "net" / "route").write_text(ROUTE.read_text("utf-8"), encoding="utf-8")
    assert probe.gateway_info(host, {}) == "192.168.1.1"


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def run_reboot(tmp_path: Path, running: str, kernels: list[str], flag: str = "") -> list[str]:
    shell = shutil.which("sh")
    if shell is None:
        pytest.skip("no POSIX shell")
    root = tmp_path / "host"
    root.mkdir(parents=True)
    for kernel in kernels:
        (root / "usr" / "lib" / "modules" / kernel).mkdir(parents=True)
    if flag:
        write(root / flag, "*** System restart required ***\n")
    release = tmp_path / "osrelease"
    write(release, running + "\n")
    out = tmp_path / "textfile"
    out.mkdir()
    env = {
        **os.environ,
        "AYE_HOST_ROOT": root.as_posix(),
        "AYE_OSRELEASE_FILE": release.as_posix(),
        "AYE_TEXTFILE_DIR": out.as_posix(),
        "AYE_REBOOT_ONCE": "1",
    }
    script = (MODULES_DIR / "node" / "files" / "reboot.sh").as_posix()
    subprocess.run([shell, script], env=env, check=True, timeout=WAIT)
    return (out / "reboot.prom").read_text(encoding="utf-8").splitlines()


def expected(flag: int, kernel: int) -> list[str]:
    return [
        f'ayeops_reboot_required{{reason="flag"}} {flag}',
        f'ayeops_reboot_required{{reason="kernel"}} {kernel}',
    ]


def test_a_newer_installed_kernel_asks_for_a_reboot(tmp_path: Path) -> None:
    kernels = ["7.2.6-200.fc44.x86_64", "7.2.10-200.fc44.x86_64", "7.1.9-300.fc44.x86_64"]
    assert run_reboot(tmp_path, "7.2.6-200.fc44.x86_64", kernels) == expected(0, 1)


def test_running_the_newest_kernel_needs_no_reboot(tmp_path: Path) -> None:
    kernels = ["7.0.0-1013-oracle", "7.0.0-1009-oracle", "6.18.2-1020-oracle", "extramodules-9"]
    assert run_reboot(tmp_path, "7.0.0-1013-oracle", kernels) == expected(0, 0)


@pytest.mark.parametrize("flag", ["run/reboot-required", "var/run/reboot-required"])
def test_the_distribution_flag_asks_for_a_reboot_on_its_own(tmp_path: Path, flag: str) -> None:
    kernels = ["7.0.0-1013-oracle"]
    assert run_reboot(tmp_path, "7.0.0-1013-oracle", kernels, flag) == expected(1, 0)


def test_no_modules_directory_means_no_kernel_signal(tmp_path: Path) -> None:
    assert run_reboot(tmp_path, "7.0.0-1", []) == expected(0, 0)


@pytest.mark.parametrize(
    ("running", "kernels", "kernel"),
    [
        ("6.9.0", ["6.10.0", "6.9.0"], 1),
        ("6.10.0", ["6.10.0", "6.9.0"], 0),
        ("6.9.0-1", ["6.9.0", "6.9.0-1"], 0),
        ("6.9.0", ["6.9.0", "6.9.0-1"], 1),
        ("6.9.0-rc1", ["6.9.0-rc2"], 1),
        ("6.9.0-rc2", ["6.9.0-rc1"], 0),
    ],
)
def test_versions_compare_numbers_as_numbers_and_the_longer_one_wins(
    tmp_path: Path, running: str, kernels: list[str], kernel: int
) -> None:
    assert run_reboot(tmp_path, running, kernels) == expected(0, kernel)


def test_reboot_collector_runs_only_where_enabled(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    edge = host_compose(manifest_from(data), "edge", modules)["services"]["reboot"]
    assert "/:/host:ro" in edge["volumes"]
    assert "/var/lib/ayeops/textfile:/textfile" in edge["volumes"]
    assert edge["environment"]["AYE_REBOOT_INTERVAL"] == "600"
    assert "reboot" not in host_compose(manifest_from(data), "central", modules)["services"]
    data["hosts"]["edge"]["modules"]["node"]["textfile_dir"] = "/var/lib/ayeops/textfile"
    edge = host_compose(manifest_from(data), "edge", modules)["services"]["reboot"]
    assert "/var/lib/ayeops/textfile:/textfile" in edge["volumes"]


def test_discover_suggests_internet_probes_with_the_found_gateway() -> None:
    report = {"system": {"hostname": "box", "arch": "amd64"}, "gateway": "192.168.1.1"}
    modules = suggest(report)["hosts"]["box"]["modules"]
    assert modules["probes"] == {"internet": True, "gateway": "192.168.1.1"}
    assert "probes" not in suggest({"system": {"hostname": "box"}})["hosts"]["box"]["modules"]
