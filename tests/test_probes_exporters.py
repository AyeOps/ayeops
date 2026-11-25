from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from tests.conftest import WAIT

ROOT = Path(__file__).resolve().parents[1]

MODULES = ROOT / "src" / "ayeops" / "modules"
BOOTSTRAP = {
    "services": [
        [["com", "net"], ["http://rdap.verisign.example/", "https://rdap.verisign.example/"]],
        [["example"], ["https://rdap.example-registry.example/v1"]],
        [["co.example"], ["https://rdap.co-registry.example/"]],
    ]
}


def load_rdap() -> ModuleType:
    path = MODULES / "probes" / "files" / "rdap_exporter.py"
    spec = importlib.util.spec_from_file_location("rdap_exporter", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rdap = load_rdap()


def test_bootstrap_lookup_prefers_https_and_the_longest_suffix() -> None:
    assert rdap.rdap_base(BOOTSTRAP, "acme.com") == "https://rdap.verisign.example/"
    assert rdap.rdap_base(BOOTSTRAP, "shop.co.example") == "https://rdap.co-registry.example/"
    assert rdap.rdap_base(BOOTSTRAP, "acme.example") == "https://rdap.example-registry.example/v1/"


def test_bootstrap_lookup_returns_none_for_unknown_tld() -> None:
    assert rdap.rdap_base(BOOTSTRAP, "acme.zz") is None


def test_expiry_is_read_from_the_expiration_event() -> None:
    record = {
        "events": [
            {"eventAction": "registration", "eventDate": "2020-01-01T00:00:00Z"},
            {"eventAction": "expiration", "eventDate": "2030-01-01T00:00:00Z"},
        ]
    }
    assert rdap.expiry_timestamp(record) == 1893456000.0
    assert rdap.expiry_timestamp({"events": []}) is None


def test_lookup_queries_the_registry_named_by_the_bootstrap() -> None:
    calls: list[str] = []

    def fetch(url: str) -> Any:
        calls.append(url)
        if url == "bootstrap":
            return BOOTSTRAP
        return {"events": [{"eventAction": "expiration", "eventDate": "2030-01-01T00:00:00Z"}]}

    assert rdap.lookup(["acme.com"], "bootstrap", fetch) == {"acme.com": 1893456000.0}
    assert calls == ["bootstrap", "https://rdap.verisign.example/domain/acme.com"]


def test_failed_lookups_are_reported_as_unknown_not_dropped() -> None:
    def fetch(url: str) -> Any:
        if url == "bootstrap":
            return BOOTSTRAP
        raise OSError("registry down")

    results = rdap.lookup(["acme.com", "acme.zz"], "bootstrap", fetch)
    assert results == {"acme.com": None, "acme.zz": None}
    text = rdap.render_metrics(results, 0.0)
    assert 'ayeops_domain_rdap_success{domain="acme.com"} 0' in text
    assert "ayeops_domain_expiry_days" not in text


def test_unreachable_bootstrap_marks_every_domain_unknown() -> None:
    def fetch(url: str) -> Any:
        raise OSError("offline")

    assert rdap.lookup(["a.com", "b.net"], "bootstrap", fetch) == {"a.com": None, "b.net": None}


def test_metrics_report_days_until_expiry() -> None:
    text = rdap.render_metrics({"acme.com": 86400.0 * 31}, 86400.0)
    assert 'ayeops_domain_rdap_success{domain="acme.com"} 1' in text
    assert 'ayeops_domain_expiry_days{domain="acme.com"} 30.00' in text
    assert 'ayeops_domain_expiry_timestamp_seconds{domain="acme.com"} 2678400' in text


def test_exporter_serves_the_last_refresh() -> None:
    exporter = rdap.Exporter(
        ["acme.com"], "bootstrap", lambda url: (_ for _ in ()).throw(OSError("offline"))
    )
    exporter.refresh()
    assert exporter.metrics(0.0) == 'ayeops_domain_rdap_success{domain="acme.com"} 0\n'


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode())


def run_battery(tmp_path: Path) -> str:
    shell = shutil.which("sh")
    if shell is None:
        pytest.skip("no POSIX shell")
    out = tmp_path / "textfile"
    out.mkdir()
    env = {
        **os.environ,
        "AYE_POWER_SUPPLY_DIR": (tmp_path / "power_supply").as_posix(),
        "AYE_TEXTFILE_DIR": out.as_posix(),
        "AYE_BATTERY_ONCE": "1",
    }
    script = (MODULES / "node" / "files" / "battery.sh").as_posix()
    subprocess.run([shell, script], env=env, check=True, timeout=WAIT)
    return (out / "battery.prom").read_text(encoding="utf-8")


def test_battery_collector_reports_capacity_status_and_ac(tmp_path: Path) -> None:
    supply = tmp_path / "power_supply"
    write(supply / "BAT0" / "type", "Battery\n")
    write(supply / "BAT0" / "capacity", "17\n")
    write(supply / "BAT0" / "status", "Discharging\n")
    write(supply / "AC" / "type", "Mains\n")
    write(supply / "AC" / "online", "0\n")
    lines = set(run_battery(tmp_path).splitlines())
    assert lines == {
        'ayeops_battery_capacity_percent{battery="BAT0"} 17',
        'ayeops_battery_status{battery="BAT0",status="Discharging"} 1',
        "ayeops_power_on_ac 0",
    }


def test_battery_collector_reports_ac_when_any_supply_is_online(tmp_path: Path) -> None:
    supply = tmp_path / "power_supply"
    write(supply / "AC" / "type", "Mains\n")
    write(supply / "AC" / "online", "0\n")
    write(supply / "USB-C" / "type", "USB\n")
    write(supply / "USB-C" / "online", "1\n")
    assert run_battery(tmp_path).splitlines() == ["ayeops_power_on_ac 1"]


def test_battery_collector_stays_silent_about_ac_without_a_mains_supply(
    tmp_path: Path,
) -> None:
    (tmp_path / "power_supply").mkdir()
    assert run_battery(tmp_path) == ""
