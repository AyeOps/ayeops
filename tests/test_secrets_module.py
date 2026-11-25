from __future__ import annotations

import calendar
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest
import yaml

from ayeops.registry import MODULES_DIR, Module
from ayeops.render import grafana_rules, host_compose, host_scrape
from ayeops.rules import parse_size
from tests.conftest import WAIT, manifest_from, pinned, render_once, rules_hit, tag_of

URL = "https://secrets.ops.acme.example"
SERVICES = {"infisical", "infisical-db", "infisical-cache", "infisical-backup"}
BACKUP = MODULES_DIR / "secrets" / "files" / "backup.sh"
RUN = MODULES_DIR / "backups" / "files" / "backup_run.sh"
DAY = 86400
NOW = calendar.timegm((2026, 1, 5, 4, 0, 0))
CLOCK = time.strftime("%Y%m%dT%H%MZ", time.gmtime(NOW))


def central(data: dict[str, Any]) -> dict[str, Any]:
    modules: dict[str, Any] = data["hosts"]["central"]["modules"]
    return modules


def with_secrets(data: dict[str, Any], **settings: Any) -> dict[str, Any]:
    central(data)["secrets"] = {"url": URL, **settings}
    central(data)["backups"] = {}
    central(data)["ingress"]["routes"]["secrets"] = "infisical:8080"
    return data


def services(data: dict[str, Any], modules: dict[str, Module]) -> dict[str, Any]:
    result: dict[str, Any] = host_compose(manifest_from(data), "central", modules)["services"]
    return result


def test_secrets_module_with_its_route_passes_every_rule(data: dict[str, Any]) -> None:
    assert rules_hit(with_secrets(data)) == set()


def test_url_is_required_and_must_be_a_site_url(data: dict[str, Any]) -> None:
    with_secrets(data)
    del central(data)["secrets"]["url"]
    assert rules_hit(data) == {"module-settings"}
    central(data)["secrets"]["url"] = "https://secrets.ops.acme.example/api"
    assert rules_hit(data) == {"module-settings"}


@pytest.mark.parametrize(
    ("setting", "value"),
    [("provider", "vault"), ("backup_hour", 24), ("backup_keep_days", 0), ("expose", [8080])],
)
def test_settings_outside_the_schema_are_rejected(
    data: dict[str, Any], setting: str, value: Any
) -> None:
    assert rules_hit(with_secrets(data, **{setting: value})) == {"module-settings"}


def test_secrets_needs_node_for_the_backup_textfile(data: dict[str, Any]) -> None:
    with_secrets(data)
    del central(data)["node"]
    assert "module-requires" in rules_hit(data)


def test_secrets_needs_backups_for_the_declaration_and_the_alerts_of_its_dump(
    data: dict[str, Any],
) -> None:
    with_secrets(data)
    del central(data)["backups"]
    assert rules_hit(data) == {"module-requires"}


def test_route_must_target_the_web_port_not_the_metrics_port(data: dict[str, Any]) -> None:
    with_secrets(data)
    central(data)["ingress"]["routes"]["secrets"] = "infisical:9464"
    assert rules_hit(data) == {"ingress-route"}


def test_infisical_db_cache_and_backup_run_with_pinned_multi_arch_images(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    rendered = services(with_secrets(data), modules)
    assert set(rendered) >= SERVICES
    images = {name: tag_of(rendered[name]["image"]) for name in SERVICES}
    assert images == {
        "infisical": pinned("secrets", "infisical/infisical"),
        "infisical-db": pinned("secrets", "postgres"),
        "infisical-cache": pinned("secrets", "valkey/valkey"),
        "infisical-backup": pinned("secrets", "postgres"),
    }
    assert all(rendered[name]["healthcheck"]["test"] for name in SERVICES)


def test_only_the_web_port_is_published_and_only_on_loopback(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    rendered = services(with_secrets(data), modules)
    assert rendered["infisical"]["ports"] == ["127.0.0.1:8090:8080"]
    assert not any("ports" in rendered[name] for name in SERVICES - {"infisical"})
    exposed = services(with_secrets(data, expose=[8090]), modules)
    assert exposed["infisical"]["ports"] == ["10.8.0.1:8090:8080"]


def test_the_whole_module_fits_in_about_one_and_a_half_gigabytes(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    rendered = services(with_secrets(data), modules)
    total = sum(parse_size(str(rendered[name]["mem_limit"])) for name in SERVICES)
    assert total <= parse_size("1.5g")
    assert parse_size(str(rendered["infisical"]["mem_limit"])) == parse_size("1g")
    heap = rendered["infisical"]["environment"]["NODE_OPTIONS"]
    assert heap == "--max-old-space-size=768"


def test_the_heap_follows_a_raised_memory_limit(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    raised = with_secrets(data, resources={"infisical": {"memory": "2g", "cpus": 2.0}})
    rendered = services(raised, modules)["infisical"]
    assert rendered["mem_limit"] == "2g"
    assert rendered["environment"]["NODE_OPTIONS"] == "--max-old-space-size=1536"


def test_every_secret_comes_from_a_required_variable(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    rendered = services(with_secrets(data), modules)
    app = rendered["infisical"]["environment"]
    assert app["ENCRYPTION_KEY"].startswith("${AYE_SECRETS_ENCRYPTION_KEY:?")
    assert app["AUTH_SECRET"].startswith("${AYE_SECRETS_AUTH_SECRET:?")
    assert "${AYE_SECRETS_DB_PASSWORD:?" in app["DB_CONNECTION_URI"]
    assert app["DB_CONNECTION_URI"].endswith("@infisical-db:5432/infisical")
    assert app["REDIS_URL"].endswith("@infisical-cache:6379")
    db = rendered["infisical-db"]["environment"]["POSTGRES_PASSWORD"]
    assert db.startswith("${AYE_SECRETS_DB_PASSWORD:?")
    assert rendered["infisical-backup"]["environment"]["PGPASSWORD"] == db
    cache = rendered["infisical-cache"]
    assert cache["environment"]["VALKEYCLI_AUTH"].startswith("${AYE_SECRETS_CACHE_PASSWORD:?")
    assert '"$$VALKEYCLI_AUTH"' in cache["command"][-1]
    env = render_once(manifest_from(data), modules)["central/.env.example"]
    for name in ("ENCRYPTION_KEY", "AUTH_SECRET", "DB_PASSWORD", "CACHE_PASSWORD"):
        assert f"AYE_SECRETS_{name}=" in env


def test_site_url_comes_from_the_setting_and_telemetry_stays_home(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    app = services(with_secrets(data), modules)["infisical"]["environment"]
    assert app["SITE_URL"] == URL
    assert app["TELEMETRY_ENABLED"] == "false"
    assert app["OTEL_EXPORT_TYPE"] == "prometheus"
    assert app["OTEL_DROP_HIGH_CARDINALITY_METERS"] == "true"


def test_google_login_needs_both_google_variables_and_is_off_by_default(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    app = services(with_secrets(data), modules)["infisical"]["environment"]
    assert "CLIENT_ID_GOOGLE_LOGIN" not in app
    app = services(with_secrets(data, google_login=True), modules)["infisical"]["environment"]
    assert app["CLIENT_ID_GOOGLE_LOGIN"].startswith("${AYE_SECRETS_GOOGLE_CLIENT_ID:?")
    assert app["CLIENT_SECRET_GOOGLE_LOGIN"].startswith("${AYE_SECRETS_GOOGLE_CLIENT_SECRET:?")


def test_vmagent_scrapes_the_infisical_prometheus_exporter(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    scrape = host_scrape(manifest_from(with_secrets(data)), "central", modules)
    assert scrape is not None
    jobs = {job["job_name"]: job for job in scrape["scrape_configs"]}
    assert jobs["infisical"]["static_configs"] == [{"targets": ["infisical:9464"]}]


def test_database_and_dumps_live_in_named_volumes_and_the_dump_reaches_node_exporter(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    compose = host_compose(manifest_from(with_secrets(data)), "central", modules)
    assert {"secrets-db", "secrets-backups", "node-textfile"} <= set(compose["volumes"])
    assert compose["services"]["infisical-db"]["volumes"] == ["secrets-db:/var/lib/postgresql"]
    backup = compose["services"]["infisical-backup"]
    assert "secrets-backups:/backups" in backup["volumes"]
    assert "node-textfile:/textfile" in backup["volumes"]
    assert "./backups/backup_run.sh:/backup_run.sh:ro" in backup["volumes"]
    assert "/tmp/backup.alive" in backup["healthcheck"]["test"][1]
    assert (backup["read_only"], backup["cap_drop"]) == (True, ["ALL"])
    assert backup["environment"]["AYE_BACKUP_HOUR"] == "3"
    assert backup["environment"]["AYE_BACKUP_KEEP_DAYS"] == "14"
    tree = render_once(manifest_from(data), modules)
    assert tree["central/secrets/backup.sh"] == BACKUP.read_text(encoding="utf-8")
    assert tree["central/backups/backup_run.sh"] == RUN.read_text(encoding="utf-8")
    declared = compose["services"]["backup-jobs"]["environment"]["AYE_BACKUP_JOBS"]
    assert declared == f"secrets={DAY + 2 * 3600}"


def test_the_dump_writes_into_the_host_textfile_directory_when_node_has_one(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    with_secrets(data)
    central(data)["node"] = {"textfile_dir": "/var/lib/ayeops/textfile"}
    compose = host_compose(manifest_from(data), "central", modules)
    assert (
        "/var/lib/ayeops/textfile:/textfile" in compose["services"]["infisical-backup"]["volumes"]
    )
    assert "node-textfile" not in compose["volumes"]


def test_ingress_serves_the_route_below_the_domain(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    tree = render_once(manifest_from(with_secrets(data)), modules)
    http = yaml.safe_load(tree["central/ingress/dynamic/routes.yml"])["http"]
    assert http["routers"]["secrets"]["rule"] == "Host(`secrets.ops.acme.example`)"
    assert http["services"]["secrets"]["loadBalancer"]["servers"] == [
        {"url": "http://infisical:8080"}
    ]


def test_the_dump_is_covered_by_the_backup_alerts_and_has_none_of_its_own(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    groups = grafana_rules(manifest_from(with_secrets(data)), modules)["groups"]
    assert not [g for g in groups if g["name"] == "secrets"]
    backups = next(g for g in groups if g["name"] == "backups")
    assert {r["uid"] for r in backups["rules"]} == {"aye-backup-failed", "aye-backup-missing"}
    assert modules["secrets"].alerts == []
    assert modules["secrets"].catalog["infisical-backup"]["alerts"] == [
        "backup-missing",
        "backup-failed",
    ]


def run_backup(
    tmp_path: Path,
    hour: int = 0,
    keep: int = 14,
    fail: bool = False,
    clock: str | None = CLOCK,
) -> tuple[Path, str]:
    shell = shutil.which("sh")
    if shell is None:
        pytest.skip("no POSIX shell")
    backups = tmp_path / "backups"
    textfile = tmp_path / "textfile"
    backups.mkdir(exist_ok=True)
    textfile.mkdir(exist_ok=True)
    fake = tmp_path / "pg_dump"
    body = 'printf PG > "${1#--file=}"; exit 1' if fail else 'printf PGDMP > "${1#--file=}"'
    fake.write_text(f"#!/bin/sh\nshift\n{body}\n", encoding="utf-8", newline="\n")
    fake.chmod(0o755)
    env = {
        **os.environ,
        "AYE_BACKUP_DIR": str(backups),
        "AYE_TEXTFILE_DIR": str(textfile),
        "AYE_BACKUP_HOUR": str(hour),
        "AYE_BACKUP_KEEP_DAYS": str(keep),
        "AYE_PG_DUMP": str(fake),
        "AYE_BACKUP_RUN": RUN.as_posix(),
        "AYE_BACKUP_ALIVE": (tmp_path / "alive").as_posix(),
        "AYE_BACKUP_ONCE": "1",
    }
    env.pop("AYE_BACKUP_CLOCK", None)
    if clock is not None:
        env["AYE_BACKUP_CLOCK"] = f"echo {clock}"
    subprocess.run([shell, str(BACKUP)], env=env, check=True, timeout=WAIT, capture_output=True)
    assert (tmp_path / "alive").is_file()
    report = textfile / "ayeops_backup_job_secrets.prom"
    return backups, report.read_text(encoding="utf-8") if report.exists() else ""


def metric(text: str, name: str) -> str:
    prefix = f'ayeops_backup_{name}{{backup="secrets"}} '
    (line,) = [line for line in text.splitlines() if line.startswith(prefix)]
    return line.removeprefix(prefix)


def dumps(backups: Path) -> list[str]:
    return sorted(p.name for p in backups.iterdir() if p.name.endswith(".dump"))


def old_dump(backups: Path, days: int) -> Path:
    stamp = time.strftime("%Y%m%dT0300Z", time.gmtime(NOW - days * DAY))
    path = backups / f"infisical-{stamp}.dump"
    path.write_bytes(b"PGDMP")
    mtime = time.time() - days * DAY
    os.utime(path, (mtime, mtime))
    return path


def test_first_run_dumps_at_once_and_reports_age_and_size(tmp_path: Path) -> None:
    before = int(time.time())
    backups, metrics = run_backup(tmp_path, hour=23, clock=None)
    after = int(time.time())
    (name,) = dumps(backups)
    named = calendar.timegm(time.strptime(name, "infisical-%Y%m%dT%H%MZ.dump"))
    assert before - 60 < named <= after
    assert metric(metrics, "last_size_bytes") == "5"
    assert metric(metrics, "last_run_success") == "1"
    assert metric(metrics, "last_exit_code") == "0"
    stamp = int(metric(metrics, "last_success_timestamp_seconds"))
    assert before <= stamp <= after
    assert not [p for p in backups.iterdir() if p.name.endswith(".tmp")]


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
def test_node_exporter_can_read_the_metrics_but_not_the_dumps(tmp_path: Path) -> None:
    backups, _ = run_backup(tmp_path, hour=23)
    metrics = (tmp_path / "textfile" / "ayeops_backup_job_secrets.prom").stat().st_mode & 0o777
    assert metrics == 0o644
    for dump in backups.iterdir():
        assert dump.stat().st_mode & 0o077 == 0


def test_one_dump_per_day_and_none_before_the_backup_hour(tmp_path: Path) -> None:
    backups, first = run_backup(tmp_path)
    backups, metrics = run_backup(tmp_path)
    assert len(dumps(backups)) == 1
    assert metrics == first
    for path in backups.iterdir():
        path.unlink()
    old_dump(backups, 1)
    backups, _ = run_backup(tmp_path, hour=99)
    assert len(dumps(backups)) == 1


def test_the_clock_sets_the_day_the_hour_and_the_name_of_a_dump(tmp_path: Path) -> None:
    backups, _ = run_backup(tmp_path, hour=3, clock="20260105T2359Z")
    assert dumps(backups) == ["infisical-20260105T2359Z.dump"]
    run_backup(tmp_path, hour=3, clock="20260106T0259Z")
    assert len(dumps(backups)) == 1
    run_backup(tmp_path, hour=3, clock="20260106T0300Z")
    run_backup(tmp_path, hour=3, clock="20260106T2359Z")
    assert dumps(backups)[1:] == ["infisical-20260106T0300Z.dump"]
    run_backup(tmp_path, hour=10, clock="20260107T0959Z")
    run_backup(tmp_path, hour=10, clock="20260107T1000Z")
    assert dumps(backups)[2:] == ["infisical-20260107T1000Z.dump"]


def test_dumps_older_than_keep_days_are_removed(tmp_path: Path) -> None:
    (tmp_path / "backups").mkdir()
    gone = old_dump(tmp_path / "backups", 30)
    kept = old_dump(tmp_path / "backups", 2)
    backups, _ = run_backup(tmp_path, keep=14)
    assert not gone.exists()
    assert kept.exists()
    assert len(dumps(backups)) == 2


def test_failed_dump_leaves_no_file_and_reports_failure_but_keeps_the_last_success(
    tmp_path: Path,
) -> None:
    (tmp_path / "backups").mkdir()
    previous = old_dump(tmp_path / "backups", 2)
    backups, metrics = run_backup(tmp_path, fail=True)
    assert dumps(backups) == [previous.name]
    assert not [p for p in backups.iterdir() if p.name.endswith(".tmp")]
    assert metric(metrics, "last_run_success") == "0"
    assert metric(metrics, "last_exit_code") == "1"
    assert "last_success_timestamp_seconds" not in metrics
    (tmp_path / "pg_dump").write_text(
        '#!/bin/sh\nshift\nprintf PGDMP > "${1#--file=}"\n', encoding="utf-8", newline="\n"
    )
    backups, metrics = run_backup(tmp_path)
    assert metric(metrics, "last_run_success") == "1"
    assert len(dumps(backups)) == 2
