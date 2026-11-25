from __future__ import annotations

import importlib.util
import subprocess
import sys
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
import yaml

from ayeops.registry import Module
from ayeops.render import SCOPE, escape_provisioning, held_back
from tests.conftest import ROOT, manifest_from, render_once, rules_hit

RUNNER = ROOT / "src" / "ayeops" / "modules" / "checks" / "files" / "check_runner.py"
RULE_SCOPE = escape_provisioning(SCOPE)


def held(expr: str) -> str:
    return str(held_back({"expr": expr}, True)["expr"])


def load_runner() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_runner", RUNNER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


runner = load_runner()


def alert(**extra: Any) -> dict[str, Any]:
    return {"for": "3m", "severity": "critical", "runbook": "web-down", **extra}


def with_checks(data: dict[str, Any]) -> dict[str, Any]:
    data["hosts"]["central"]["modules"]["checks"] = {}
    data["services"]["web"]["checks"] = {
        "homepage": {
            "type": "http",
            "url": "https://acme.example/health",
            "expect_status": 200,
            "expect_body": "ok",
            "alert": alert(),
        },
        "ssh": {"type": "tcp", "address": "10.8.0.1:22", "alert": alert(severity="warning")},
        "resolve": {
            "type": "dns",
            "name": "acme.example",
            "expect": "10\\.8\\.0\\.1",
            "alert": alert(),
        },
        "backup": {
            "type": "script",
            "command": "test -f /backups/latest",
            "mounts": ["/var/backups:/backups"],
            "interval": 300,
            "alert": alert(),
        },
        "upstream": {
            "type": "script",
            "command": "wget -q -O /dev/null https://upstream.acme.example",
            "network": True,
            "env": ["ACME_UPSTREAM_TOKEN"],
            "alert": alert(),
        },
    }
    return data


def checks_of(data: dict[str, Any]) -> dict[str, Any]:
    checks: dict[str, Any] = data["services"]["web"]["checks"]
    return checks


def rendered(data: dict[str, Any], modules: dict[str, Module]) -> dict[str, str]:
    return render_once(manifest_from(data), modules)


def compose(tree: dict[str, str], host: str) -> dict[str, Any]:
    document: dict[str, Any] = yaml.safe_load(tree[f"{host}/compose.yaml"])
    return document


def service_rules(tree: dict[str, str]) -> dict[str, dict[str, Any]]:
    groups = yaml.safe_load(tree["central/grafana/provisioning/alerting/rules.yaml"])["groups"]
    return {rule["uid"]: rule for g in groups if g["name"] == "services" for rule in g["rules"]}


def test_declared_checks_pass_every_rule(data: dict[str, Any]) -> None:
    assert rules_hit(with_checks(data)) == set()


def test_http_tcp_and_dns_checks_become_blackbox_modules_with_one_job_each(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    tree = rendered(with_checks(data), modules)
    blackbox = yaml.safe_load(tree["central/checks/blackbox.yml"])["modules"]
    assert blackbox["check_web_homepage"]["http"]["valid_status_codes"] == [200]
    assert blackbox["check_web_homepage"]["http"]["fail_if_body_not_matches_regexp"] == ["ok"]
    assert blackbox["check_web_ssh"]["prober"] == "tcp"
    resolve = blackbox["check_web_resolve"]["dns"]
    assert resolve["query_name"] == "acme.example"
    assert resolve["validate_answer_rrs"]["fail_if_none_matches_regexp"] == ["10\\.8\\.0\\.1"]
    jobs = {
        job["job_name"]: job
        for job in yaml.safe_load(tree["central/vmagent/scrape.yaml"])["scrape_configs"]
    }
    homepage = jobs["check-web-homepage"]
    assert homepage["params"] == {
        "module": ["check_web_homepage"],
        "target": ["https://acme.example/health"],
    }
    assert homepage["static_configs"][0]["labels"] == {"check": "web-homepage", "service": "web"}
    assert jobs["check-web-resolve"]["params"]["target"] == ["1.1.1.1"]
    assert compose(tree, "central")["services"]["check-blackbox"]["ports"] == [
        "127.0.0.1:9116:9116"
    ]


def test_every_check_gets_an_alert_with_its_for_severity_and_runbook(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    rules = service_rules(rendered(with_checks(data), modules))
    homepage = rules["aye-web-homepage"]
    assert homepage["for"] == "3m"
    assert homepage["labels"] == {"severity": "critical", "scope": RULE_SCOPE, "service": "web"}
    assert homepage["annotations"]["runbook_url"] == "https://runbooks.acme.example/web-down"
    assert homepage["noDataState"] == "Alerting"
    assert homepage["data"][0]["model"]["expr"] == held(
        'probe_success{check="web-homepage"} == bool 0'
    )
    assert rules["aye-web-ssh"]["labels"]["severity"] == "warning"
    assert {"aye-web-resolve", "aye-web-backup", "aye-web-upstream", "aye-web-down"} <= set(rules)


def test_a_script_check_alert_fires_when_its_results_go_stale(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    expr = service_rules(rendered(with_checks(data), modules))["aye-web-backup"]["data"][0]
    assert expr["model"]["expr"] == held(
        '1 - (ayeops_check_success{check="web-backup"} * '
        '(time() - ayeops_check_last_run_timestamp_seconds{check="web-backup"} < bool 670))'
    )


@pytest.mark.parametrize(
    ("field", "rule"),
    [("for", "alert-for"), ("severity", "alert-severity"), ("runbook", "alert-runbook")],
)
def test_a_check_alert_needs_for_severity_and_runbook(
    data: dict[str, Any], field: str, rule: str
) -> None:
    del checks_of(with_checks(data))["ssh"]["alert"][field]
    assert rules_hit(data) == {rule}


def test_checks_alone_satisfy_the_metric_and_alert_rules(data: dict[str, Any]) -> None:
    web = with_checks(data)["services"]["web"]
    del web["metrics"]
    del web["alerts"]
    assert rules_hit(data) == set()


def test_checks_need_the_checks_module_on_the_probing_host(data: dict[str, Any]) -> None:
    del with_checks(data)["hosts"]["central"]["modules"]["checks"]
    assert rules_hit(data) == {"check-module"}


def test_a_check_with_from_runs_on_that_host(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    with_checks(data)
    data["hosts"]["edge"]["modules"]["checks"] = {}
    checks_of(data)["ssh"]["from"] = "edge"
    assert rules_hit(data) == set()
    tree = rendered(data, modules)
    assert "check_web_ssh" in yaml.safe_load(tree["edge/checks/blackbox.yml"])["modules"]
    assert "check_web_ssh" not in yaml.safe_load(tree["central/checks/blackbox.yml"])["modules"]


def test_a_check_from_an_unknown_host_is_rejected(data: dict[str, Any]) -> None:
    checks_of(with_checks(data))["ssh"]["from"] = "nowhere"
    assert rules_hit(data) == {"check-host"}


def test_script_checks_need_node_on_the_probing_host(data: dict[str, Any]) -> None:
    del with_checks(data)["hosts"]["central"]["modules"]["node"]
    del data["hosts"]["central"]["modules"]["reconciler"]
    assert "check-script-node" in rules_hit(data)


def test_a_check_timeout_must_be_shorter_than_its_interval(data: dict[str, Any]) -> None:
    checks_of(with_checks(data))["ssh"].update(timeout=60, interval=60)
    assert rules_hit(data) == {"check-timing"}


def test_a_field_of_another_check_type_is_a_schema_error(data: dict[str, Any]) -> None:
    checks_of(with_checks(data))["ssh"]["url"] = "https://acme.example"
    assert rules_hit(data) == {"schema"}


def test_a_check_alert_name_must_be_unique(data: dict[str, Any]) -> None:
    checks_of(with_checks(data))["ssh"]["alert"]["name"] = "web-down"
    assert rules_hit(data) == {"alert-unique"}


def test_script_runners_are_sandboxed_and_only_the_networked_one_has_a_network(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    services = compose(rendered(with_checks(data), modules), "central")["services"]
    isolated, networked = services["check-scripts"], services["check-scripts-net"]
    assert isolated["network_mode"] == "none"
    assert "network_mode" not in networked
    for runner_service in (isolated, networked):
        assert runner_service["read_only"] is True
        assert runner_service["cap_drop"] == ["ALL"]
        assert runner_service["security_opt"] == ["no-new-privileges:true"]
        assert runner_service["pids_limit"] == 64
        assert "ports" not in runner_service
    assert "/var/backups:/backups:ro" in isolated["volumes"]
    assert all(v.endswith(":ro") or v.startswith("node-textfile:") for v in isolated["volumes"])


def test_script_config_lands_in_a_file_and_env_is_passed_by_reference(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    tree = rendered(with_checks(data), modules)
    networked = compose(tree, "central")["services"]["check-scripts-net"]
    assert networked["environment"]["ACME_UPSTREAM_TOKEN"] == "${ACME_UPSTREAM_TOKEN}"
    assert "ACME_UPSTREAM_TOKEN=replace-me" in tree["central/.env.example"]
    config = yaml.safe_load(tree["central/checks/scripts.json"])
    assert config == [
        {
            "check": "web-backup",
            "command": "test -f /backups/latest",
            "env": [],
            "interval": 300,
            "service": "web",
            "timeout": 10,
        }
    ]
    assert "central/checks/check_runner.py" in tree


def test_without_checks_the_checks_module_renders_nothing(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    data["hosts"]["central"]["modules"]["checks"] = {}
    tree = rendered(data, modules)
    assert not any(path.startswith("central/checks/") for path in tree)
    assert not any(name.startswith("check-") for name in compose(tree, "central")["services"])


def test_runner_reads_metric_lines_and_ignores_the_rest() -> None:
    stdout = "rows 42\nage_seconds 1.5e3\nhello world\nbad nan-ish\ninf_value inf\n"
    assert runner.parse_values(stdout) == {"rows": 42.0, "age_seconds": 1500.0}


class FakeRun:
    def __init__(self, code: int = 0, stdout: str = "", error: Exception | None = None) -> None:
        self.code = code
        self.stdout = stdout
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def __call__(self, argv: list[str], **options: Any) -> Any:
        self.calls.append({"argv": argv, **options})
        if self.error is not None:
            raise self.error
        return SimpleNamespace(returncode=self.code, stdout=self.stdout)


CHECK = {"check": "web-backup", "service": "web", "command": "true", "timeout": 5, "env": ["A"]}


def test_runner_runs_the_command_as_nobody_with_only_the_listed_env() -> None:
    run = FakeRun(stdout="rows 3\n")
    result = runner.run_check(CHECK, run=run, user=65534, environ={"A": "1", "SECRET": "x"})
    call = run.calls[0]
    assert call["argv"] == ["/bin/sh", "-c", "true"]
    assert call["user"] == 65534 and call["group"] == 65534 and call["extra_groups"] == []
    assert call["timeout"] == 5.0
    assert call["env"]["A"] == "1"
    assert "SECRET" not in call["env"]
    assert result.exit_code == 0
    assert result.values == {"rows": 3.0}


def test_runner_reports_a_timeout_as_exit_code_124() -> None:
    run = FakeRun(error=subprocess.TimeoutExpired("sh", 5))
    assert runner.run_check(CHECK, run=run).exit_code == 124


def test_runner_reports_a_command_that_cannot_start_as_exit_code_127() -> None:
    assert runner.run_check(CHECK, run=FakeRun(error=OSError("no sh"))).exit_code == 127


def test_runner_drops_values_of_a_failed_run() -> None:
    assert runner.run_check(CHECK, run=FakeRun(code=1, stdout="rows 3\n")).values == {}


def test_runner_metrics_carry_success_exit_code_duration_last_run_and_values() -> None:
    results = {"web-backup": runner.Result(0, 0.25, 1000.0, {"rows": 3.0})}
    text = runner.render_metrics([CHECK], results, "check-scripts", 1010.0)
    labels = 'check="web-backup",service="web"'
    assert text.splitlines() == [
        'ayeops_checks_heartbeat_timestamp_seconds{runner="check-scripts"} 1010',
        f"ayeops_check_success{{{labels}}} 1",
        f"ayeops_check_exit_code{{{labels}}} 0",
        f"ayeops_check_duration_seconds{{{labels}}} 0.250",
        f"ayeops_check_last_run_timestamp_seconds{{{labels}}} 1000",
        f'ayeops_check_value{{{labels},name="rows"}} 3',
    ]


def test_runner_writes_its_textfile_atomically(tmp_path: Any) -> None:
    target = tmp_path / "ayeops_check_scripts.prom"
    instance = runner.Runner([CHECK], target, "check-scripts")
    instance.record(CHECK, runner.Result(1, 0.1, 5.0))
    assert 'ayeops_check_success{check="web-backup",service="web"} 0' in target.read_text()
    assert [p.name for p in tmp_path.iterdir()] == ["ayeops_check_scripts.prom"]
