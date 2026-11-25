from __future__ import annotations

import os
import shutil
import subprocess
import sys
import types
from pathlib import Path
from typing import Any

import pytest
import yaml

from ayeops.permissions import credentials
from ayeops.registry import MODULES_DIR, Module
from ayeops.render import grafana_rules, host_compose, host_scrape
from ayeops.rules import parse_size
from tests.conftest import WAIT, manifest_from, pinned, render_once, rules_hit, tag_of

URL = "https://errors.ops.acme.example"
APPS = ("glitchtip", "glitchtip-worker")
SERVICES = {"glitchtip", "glitchtip-worker", "glitchtip-db", "glitchtip-cache", "glitchtip-backup"}
BACKUP = MODULES_DIR / "errors" / "files" / "backup.sh"
SECRETS_BACKUP = MODULES_DIR / "secrets" / "files" / "backup.sh"
RUN = MODULES_DIR / "backups" / "files" / "backup_run.sh"
SETTINGS = MODULES_DIR / "errors" / "files" / "ayeops_settings.py"
CLOSED = ("ENABLE_USER_REGISTRATION", "ENABLE_SOCIAL_APPS_USER_REGISTRATION")


def central(data: dict[str, Any]) -> dict[str, Any]:
    modules: dict[str, Any] = data["hosts"]["central"]["modules"]
    return modules


def with_errors(data: dict[str, Any], **settings: Any) -> dict[str, Any]:
    central(data)["errors"] = {"url": URL, **settings}
    central(data)["backups"] = {}
    central(data)["ingress"]["routes"]["errors"] = "glitchtip:8000"
    return data


def services(data: dict[str, Any], modules: dict[str, Module]) -> dict[str, Any]:
    result: dict[str, Any] = host_compose(manifest_from(data), "central", modules)["services"]
    return result


def test_errors_module_with_its_route_passes_every_rule(data: dict[str, Any]) -> None:
    assert rules_hit(with_errors(data)) == set()


def test_url_is_required_and_must_be_a_site_url(data: dict[str, Any]) -> None:
    with_errors(data)
    del central(data)["errors"]["url"]
    assert rules_hit(data) == {"module-settings"}
    central(data)["errors"]["url"] = "https://errors.ops.acme.example/ui"
    assert rules_hit(data) == {"module-settings"}


@pytest.mark.parametrize(
    ("setting", "value"),
    [
        ("provider", "sentry"),
        ("retention_days", 0),
        ("retention_days", 366),
        ("backup_hour", 24),
        ("backup_keep_days", 0),
        ("expose", [8000]),
    ],
)
def test_settings_outside_the_schema_are_rejected(
    data: dict[str, Any], setting: str, value: Any
) -> None:
    assert rules_hit(with_errors(data, **{setting: value})) == {"module-settings"}


def test_errors_needs_node_for_the_backup_textfile(data: dict[str, Any]) -> None:
    with_errors(data)
    del central(data)["node"]
    assert "module-requires" in rules_hit(data)


def test_errors_needs_backups_for_the_declaration_and_the_alerts_of_its_dump(
    data: dict[str, Any],
) -> None:
    with_errors(data)
    del central(data)["backups"]
    assert rules_hit(data) == {"module-requires"}


def test_route_must_target_the_web_port(data: dict[str, Any]) -> None:
    with_errors(data)
    central(data)["ingress"]["routes"]["errors"] = "glitchtip:9000"
    assert rules_hit(data) == {"ingress-route"}


def test_every_service_runs_a_pinned_image_with_a_healthcheck(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    rendered = services(with_errors(data), modules)
    assert set(rendered) >= SERVICES
    images = {name: tag_of(rendered[name]["image"]) for name in SERVICES}
    assert images == {
        "glitchtip": pinned("errors", "glitchtip/glitchtip"),
        "glitchtip-worker": pinned("errors", "glitchtip/glitchtip"),
        "glitchtip-db": pinned("errors", "postgres"),
        "glitchtip-cache": pinned("errors", "valkey/valkey"),
        "glitchtip-backup": pinned("errors", "postgres"),
    }
    assert all(rendered[name]["healthcheck"]["test"] for name in SERVICES)


def test_sign_ups_and_new_organizations_are_closed_in_web_and_worker(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    rendered = services(with_errors(data), modules)
    for name in APPS:
        environment = rendered[name]["environment"]
        assert all(environment[key] == "false" for key in CLOSED)
        assert environment["ENABLE_ORGANIZATION_CREATION"] == "false"
        assert "ENABLE_OPEN_USER_REGISTRATION" not in environment


def test_the_ui_route_is_behind_the_sign_in_and_the_ingest_port_is_not(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    exposed = with_errors(data, expose=[8095])
    tree = render_once(manifest_from(exposed), modules)
    routers = yaml.safe_load(tree["central/ingress/dynamic/routes.yml"])["http"]["routers"]
    assert routers["errors"]["rule"] == "Host(`errors.ops.acme.example`)"
    assert routers["errors"]["middlewares"] == ["sso"]
    web = services(exposed, modules)["glitchtip"]
    assert web["ports"] == ["10.8.0.1:8095:8000"]
    assert "10.8.0.1" in web["environment"]["ALLOWED_HOSTS"].split(",")


def test_only_the_web_port_is_published_and_only_on_loopback_by_default(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    rendered = services(with_errors(data), modules)
    assert rendered["glitchtip"]["ports"] == ["127.0.0.1:8095:8000"]
    assert not any("ports" in rendered[name] for name in SERVICES - {"glitchtip"})


def test_every_secret_comes_from_a_required_variable(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    rendered = services(with_errors(data), modules)
    for name in APPS:
        environment = rendered[name]["environment"]
        assert environment["SECRET_KEY"].startswith("${AYE_ERRORS_SECRET_KEY:?")
        assert "${AYE_ERRORS_DB_PASSWORD:?" in environment["DATABASE_URL"]
        assert environment["DATABASE_URL"].endswith("@glitchtip-db:5432/glitchtip")
        assert "${AYE_ERRORS_CACHE_PASSWORD:?" in environment["VALKEY_URL"]
        assert environment["VALKEY_URL"].endswith("@glitchtip-cache:6379/0")
    db = rendered["glitchtip-db"]["environment"]["POSTGRES_PASSWORD"]
    assert db.startswith("${AYE_ERRORS_DB_PASSWORD:?")
    assert rendered["glitchtip-backup"]["environment"]["PGPASSWORD"] == db
    cache = rendered["glitchtip-cache"]
    assert cache["environment"]["VALKEYCLI_AUTH"].startswith("${AYE_ERRORS_CACHE_PASSWORD:?")
    env = render_once(manifest_from(data), modules)["central/.env.example"]
    for name in ("SECRET_KEY", "DB_PASSWORD", "CACHE_PASSWORD"):
        assert f"AYE_ERRORS_{name}=" in env


def test_the_web_container_migrates_before_it_serves_and_the_worker_waits_for_it(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    rendered = services(with_errors(data), modules)
    web = rendered["glitchtip"]
    script = web["command"][-1]
    assert web["command"][:2] == ["sh", "-c"]
    assert script.index("manage.py migrate") < script.index("exec ./bin/run-web.sh")
    assert "maintain_partitions" in script
    assert web["environment"]["SERVER_ROLE"] == "web"
    worker = rendered["glitchtip-worker"]
    assert worker["environment"]["SERVER_ROLE"] == "worker"
    assert "command" not in worker
    assert worker["depends_on"] == {"glitchtip": {"condition": "service_healthy"}}


def test_site_retention_and_scope_come_from_the_settings(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    default = services(with_errors(data), modules)["glitchtip"]["environment"]
    assert default["GLITCHTIP_DOMAIN"] == URL
    assert default["CSRF_TRUSTED_ORIGINS"] == URL
    assert default["ALLOWED_HOSTS"].split(",")[0] == "errors.ops.acme.example"
    assert default["GLITCHTIP_RETENTION_DAYS"] == "30"
    assert default["ENABLE_OBSERVABILITY_API"] == "true"
    assert (default["GLITCHTIP_ENABLE_UPTIME"], default["GLITCHTIP_ENABLE_LOGS"]) == (
        "false",
        "false",
    )
    longer = services(with_errors(data, retention_days=90), modules)["glitchtip"]
    assert longer["environment"]["GLITCHTIP_RETENTION_DAYS"] == "90"


def test_mail_settings_reach_glitchtip_with_the_password_from_the_environment(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    environment = services(with_errors(data), modules)["glitchtip"]["environment"]
    assert environment["EMAIL_HOST_PASSWORD"].startswith("${AYE_SMTP_PASSWORD:?")
    assert environment["EMAIL_HOST_USER"].startswith("${AYE_SMTP_USER:?")
    assert (environment["EMAIL_USE_TLS"], environment["EMAIL_USE_SSL"]) == ("true", "false")
    data.pop("mail")
    bare = services(with_errors(data), modules)["glitchtip"]["environment"]
    assert not [key for key in bare if key.startswith("EMAIL_")]


def test_the_application_containers_drop_every_capability(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    rendered = services(with_errors(data), modules)
    for name in (*APPS, "glitchtip-backup"):
        assert rendered[name]["cap_drop"] == ["ALL"]
        assert rendered[name]["security_opt"] == ["no-new-privileges:true"]
    assert rendered["glitchtip-backup"]["read_only"] is True


def test_vmagent_scrapes_the_glitchtip_metrics(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    scrape = host_scrape(manifest_from(with_errors(data)), "central", modules)
    assert scrape is not None
    jobs = {job["job_name"]: job for job in scrape["scrape_configs"]}
    assert jobs["glitchtip"]["static_configs"] == [{"targets": ["glitchtip:8000"]}]
    assert jobs["glitchtip"]["metrics_path"] == "/metrics"


def test_the_whole_module_fits_in_two_gigabytes(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    rendered = services(with_errors(data), modules)
    total = sum(parse_size(str(rendered[name]["mem_limit"])) for name in SERVICES)
    assert total <= parse_size("2g")


def test_database_and_dumps_live_in_named_volumes_and_the_dump_reaches_node_exporter(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    compose = host_compose(manifest_from(with_errors(data)), "central", modules)
    assert {"errors-db", "errors-backups", "node-textfile"} <= set(compose["volumes"])
    assert compose["services"]["glitchtip-db"]["volumes"] == ["errors-db:/var/lib/postgresql"]
    backup = compose["services"]["glitchtip-backup"]
    assert "errors-backups:/backups" in backup["volumes"]
    assert "node-textfile:/textfile" in backup["volumes"]
    assert "./backups/backup_run.sh:/backup_run.sh:ro" in backup["volumes"]
    assert "/tmp/backup.alive" in backup["healthcheck"]["test"][1]
    assert backup["environment"]["AYE_BACKUP_HOUR"] == "4"
    declared = compose["services"]["backup-jobs"]["environment"]["AYE_BACKUP_JOBS"]
    assert declared == f"errors={26 * 3600}"
    tree = render_once(manifest_from(with_errors(data)), modules)
    assert tree["central/errors/backup.sh"] == BACKUP.read_text(encoding="utf-8")


def test_the_dump_script_names_only_the_error_tracker() -> None:
    text = BACKUP.read_text(encoding="utf-8")
    assert "\njob=errors\nprefix=glitchtip\n" in text
    assert "infisical" not in text
    assert "secrets" not in text
    other = SECRETS_BACKUP.read_text(encoding="utf-8")
    assert text == other.replace("job=secrets", "job=errors").replace(
        "prefix=infisical", "prefix=glitchtip"
    )


def test_the_dump_is_covered_by_the_backup_alerts_and_has_none_of_its_own(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    groups = grafana_rules(manifest_from(with_errors(data)), modules)["groups"]
    assert not [g for g in groups if g["name"] == "errors"]
    backups = next(g for g in groups if g["name"] == "backups")
    assert {r["uid"] for r in backups["rules"]} == {"aye-backup-failed", "aye-backup-missing"}
    assert modules["errors"].alerts == []


def run_named_backup(tmp_path: Path) -> tuple[list[str], str]:
    shell = shutil.which("sh")
    if shell is None:
        pytest.skip("no POSIX shell")
    backups = tmp_path / "backups"
    textfile = tmp_path / "textfile"
    backups.mkdir()
    textfile.mkdir()
    fake = tmp_path / "pg_dump"
    fake.write_text(
        '#!/bin/sh\nshift\nprintf PGDMP > "${1#--file=}"\n', encoding="utf-8", newline="\n"
    )
    fake.chmod(0o755)
    env = {
        **os.environ,
        "AYE_BACKUP_DIR": str(backups),
        "AYE_TEXTFILE_DIR": str(textfile),
        "AYE_BACKUP_HOUR": "0",
        "AYE_PG_DUMP": str(fake),
        "AYE_BACKUP_RUN": RUN.as_posix(),
        "AYE_BACKUP_ALIVE": (tmp_path / "alive").as_posix(),
        "AYE_BACKUP_ONCE": "1",
        "AYE_BACKUP_CLOCK": "echo 20260105T0400Z",
    }
    subprocess.run([shell, str(BACKUP)], env=env, check=True, timeout=WAIT, capture_output=True)
    names = sorted(p.name for p in backups.iterdir())
    return names, (textfile / "ayeops_backup_job_errors.prom").read_text(encoding="utf-8")


def test_the_dump_and_its_metrics_carry_the_names_the_module_sets(tmp_path: Path) -> None:
    names, metrics = run_named_backup(tmp_path)
    assert names == ["glitchtip-20260105T0400Z.dump"]
    assert 'ayeops_backup_last_size_bytes{backup="errors"} 5' in metrics.splitlines()
    assert 'ayeops_backup_last_run_success{backup="errors"} 1' in metrics.splitlines()
    assert "secrets" not in metrics
    assert [p.name for p in (tmp_path / "textfile").iterdir()] == ["ayeops_backup_job_errors.prom"]


def test_web_and_worker_load_the_settings_that_trust_the_ingress_https_header(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    rendered = services(with_errors(data), modules)
    for name in APPS:
        assert rendered[name]["environment"]["DJANGO_SETTINGS_MODULE"] == "ayeops_settings"
        assert (
            rendered[name]["volumes"][0]
            == "./errors/ayeops_settings.py:/code/ayeops_settings.py:ro"
        )
    assert rendered["glitchtip-worker"]["volumes"] == [
        "./errors/ayeops_settings.py:/code/ayeops_settings.py:ro"
    ]
    assert rendered["glitchtip"]["volumes"] == [
        "./errors/ayeops_settings.py:/code/ayeops_settings.py:ro",
        "./errors/google_app.py:/code/google_app.py:ro",
    ]
    tree = render_once(manifest_from(with_errors(data)), modules)
    assert tree["central/errors/ayeops_settings.py"] == SETTINGS.read_text(encoding="utf-8")


def test_the_settings_keep_every_glitchtip_setting_and_mark_forwarded_https_as_secure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = types.ModuleType("glitchtip.settings")
    vars(base).update(GLITCHTIP_URL=URL, CSRF_TRUSTED_ORIGINS=[URL], helper=object())
    monkeypatch.setitem(sys.modules, "glitchtip", types.ModuleType("glitchtip"))
    monkeypatch.setitem(sys.modules, "glitchtip.settings", base)
    loaded: dict[str, Any] = {}
    exec(compile(SETTINGS.read_text(encoding="utf-8"), str(SETTINGS), "exec"), loaded)
    assert loaded["GLITCHTIP_URL"] == URL
    assert loaded["CSRF_TRUSTED_ORIGINS"] == [URL]
    assert "helper" not in loaded
    assert loaded["SECURE_PROXY_SSL_HEADER"] == ("HTTP_X_FORWARDED_PROTO", "https")


GOOGLE_STEP = "manage.py shell --no-imports < /code/google_app.py"
GOOGLE_APP = MODULES_DIR / "errors" / "files" / "google_app.py"


def test_the_google_app_step_runs_after_the_migrations_and_only_with_google_auth(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    web = services(with_errors(data), modules)["glitchtip"]
    script = web["command"][-1]
    assert script.index("manage.py migrate") < script.index(GOOGLE_STEP)
    assert script.index(GOOGLE_STEP) < script.index("exec ./bin/run-web.sh")
    assert f"createcachetable && python {GOOGLE_STEP} && exec ./bin/run-web.sh" in script
    assert web["environment"]["AYE_AUTH_CLIENT_ID"].startswith("${AYE_AUTH_CLIENT_ID:?")
    assert web["environment"]["AYE_AUTH_CLIENT_SECRET"].startswith("${AYE_AUTH_CLIENT_SECRET:?")
    assert "./errors/google_app.py:/code/google_app.py:ro" in web["volumes"]
    worker = services(with_errors(data), modules)["glitchtip-worker"]
    assert "AYE_AUTH_CLIENT_ID" not in worker["environment"]
    assert all("google_app" not in volume for volume in worker["volumes"])
    tree = render_once(manifest_from(with_errors(data)), modules)
    assert tree["central/errors/google_app.py"] == GOOGLE_APP.read_text(encoding="utf-8")


def test_without_google_auth_the_web_container_has_no_google_step(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    with_errors(data)
    del central(data)["auth"]
    del central(data)["ingress"]["routes"]["grafana"]
    web = services(data, modules)["glitchtip"]
    assert "google_app" not in web["command"][-1]
    assert "AYE_AUTH_CLIENT_ID" not in web["environment"]
    assert all("google_app" not in volume for volume in web["volumes"])


def test_with_oidc_auth_the_web_container_has_no_google_step(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    with_errors(data)
    central(data)["auth"].update(provider="oidc", issuer="https://sso.acme.example", endpoints=None)
    web = services(data, modules)["glitchtip"]
    assert "google_app" not in web["command"][-1]
    assert "AYE_AUTH_CLIENT_ID" not in web["environment"]


class FakeApps:
    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}

    def update_or_create(self, provider: str, defaults: dict[str, Any]) -> None:
        self.rows.setdefault(provider, {"provider": provider}).update(defaults)


def run_google_app(monkeypatch: pytest.MonkeyPatch, apps: FakeApps) -> None:
    models = types.ModuleType("allauth.socialaccount.models")
    vars(models)["SocialApp"] = types.SimpleNamespace(objects=apps)
    monkeypatch.setitem(sys.modules, "allauth", types.ModuleType("allauth"))
    monkeypatch.setitem(sys.modules, "allauth.socialaccount", types.ModuleType("x"))
    monkeypatch.setitem(sys.modules, "allauth.socialaccount.models", models)
    exec(compile(GOOGLE_APP.read_text(encoding="utf-8"), str(GOOGLE_APP), "exec"), {})


def test_a_restore_onto_an_empty_database_brings_the_google_provider_back(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("AYE_AUTH_CLIENT_ID", "id-1")
    monkeypatch.setenv("AYE_AUTH_CLIENT_SECRET", "secret-1")
    apps = FakeApps()
    run_google_app(monkeypatch, apps)
    assert apps.rows == {
        "google": {
            "provider": "google",
            "name": "Google",
            "client_id": "id-1",
            "secret": "secret-1",
        }
    }
    assert capsys.readouterr().out == ""


def test_a_rotated_client_updates_the_google_provider_instead_of_adding_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    apps = FakeApps()
    monkeypatch.setenv("AYE_AUTH_CLIENT_ID", "id-1")
    monkeypatch.setenv("AYE_AUTH_CLIENT_SECRET", "secret-1")
    run_google_app(monkeypatch, apps)
    monkeypatch.setenv("AYE_AUTH_CLIENT_ID", "id-2")
    monkeypatch.setenv("AYE_AUTH_CLIENT_SECRET", "secret-2")
    run_google_app(monkeypatch, apps)
    assert list(apps.rows) == ["google"]
    assert apps.rows["google"]["client_id"] == "id-2"
    assert apps.rows["google"]["secret"] == "secret-2"


def test_the_settings_let_an_existing_user_sign_in_with_google_on_the_first_try(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "glitchtip", types.ModuleType("glitchtip"))
    monkeypatch.setitem(sys.modules, "glitchtip.settings", types.ModuleType("glitchtip.settings"))
    loaded: dict[str, Any] = {}
    exec(compile(SETTINGS.read_text(encoding="utf-8"), str(SETTINGS), "exec"), loaded)
    assert loaded["SOCIALACCOUNT_EMAIL_AUTHENTICATION"] is True
    assert loaded["SOCIALACCOUNT_EMAIL_AUTHENTICATION_AUTO_CONNECT"] is True


def test_google_sign_in_still_creates_no_accounts(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    web = services(with_errors(data), modules)["glitchtip"]
    assert web["environment"]["ENABLE_SOCIAL_APPS_USER_REGISTRATION"] == "false"
    assert "sites" not in GOOGLE_APP.read_text(encoding="utf-8")


CALLBACK = f"{URL}/accounts/google/login/callback/"


def grants(data: dict[str, Any], modules: dict[str, Module], module: str) -> list[str]:
    found = credentials(manifest_from(data), modules, "central", module)
    return [grant for c in found if "AYE_AUTH_CLIENT_ID" in c.env for grant in c.grants]


def test_the_permissions_ask_for_the_glitchtip_callback_on_the_google_client(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    with_errors(data)
    found = credentials(manifest_from(data), modules, "central", "errors")
    assert [(c.issuer, c.env) for c in found] == [
        ("Google", ("AYE_AUTH_CLIENT_ID", "AYE_AUTH_CLIENT_SECRET"))
    ]
    assert grants(data, modules, "errors") == [f"redirect URI {CALLBACK} on the same client"]
    assert grants(data, modules, "auth")[-1].endswith(f" and {CALLBACK}")


def test_without_google_the_permissions_ask_for_no_glitchtip_callback(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    with_errors(data)
    central(data)["auth"].update(provider="oidc", issuer="https://sso.acme.example", endpoints=None)
    assert credentials(manifest_from(data), modules, "central", "errors") == []
    assert all(CALLBACK not in grant for grant in grants(data, modules, "auth"))
    del central(data)["auth"]
    del central(data)["ingress"]["routes"]["grafana"]
    assert credentials(manifest_from(data), modules, "central", "errors") == []
