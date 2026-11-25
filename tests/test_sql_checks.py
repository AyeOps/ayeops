from __future__ import annotations

import contextlib
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest
import yaml

from ayeops.checks import Check, check_alert, sql_expr, sql_file
from ayeops.checks_sql import sql_query, sql_query_problem, statement_timeout
from ayeops.promql import may_be_empty
from ayeops.registry import Module
from ayeops.render import SCOPE, escape_provisioning
from ayeops.rules import check_nodata
from tests.conftest import ROOT, WAIT, manifest_from, render_once, rules_hit

FILES = ROOT / "src" / "ayeops" / "modules" / "checks" / "files"
QUERY = "select count(*) as value from orders where stuck"


def sql_alert(**extra: Any) -> dict[str, Any]:
    return {
        "op": ">",
        "threshold": 0,
        "for": "5m",
        "severity": "warning",
        "runbook": "orders-stuck",
        **extra,
    }


def with_sql(data: dict[str, Any], **extra: Any) -> dict[str, Any]:
    data["hosts"]["central"]["modules"]["checks"] = {}
    data["services"]["web"]["checks"] = {
        "stuck": {
            "type": "sql",
            "datasource": "acme-orders",
            "query": QUERY,
            "alert": sql_alert(),
            **extra,
        }
    }
    return data


def spec(data: dict[str, Any]) -> dict[str, Any]:
    stuck: dict[str, Any] = data["services"]["web"]["checks"]["stuck"]
    return stuck


def make(**alert: Any) -> Check:
    return Check(
        "web",
        "stuck",
        {"type": "sql", "datasource": "d", "query": QUERY, "alert": sql_alert(**alert)},
        "central",
    )


def services_rules(tree: dict[str, str]) -> dict[str, dict[str, Any]]:
    groups = yaml.safe_load(tree["central/grafana/provisioning/alerting/rules.yaml"])["groups"]
    return {rule["uid"]: rule for g in groups if g["name"] == "services" for rule in g["rules"]}


def test_a_declared_sql_check_passes_every_rule(data: dict[str, Any]) -> None:
    assert rules_hit(with_sql(data)) == set()


def test_the_sql_check_runs_in_a_sandboxed_postgres_runner_with_the_password_by_reference(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    tree = render_once(manifest_from(with_sql(data)), modules)
    service = yaml.safe_load(tree["central/compose.yaml"])["services"]["check-sql"]
    assert service["image"].startswith("postgres:18.")
    assert service["read_only"] is True
    assert service["cap_drop"] == ["ALL"]
    assert "cap_add" not in service
    assert service["environment"]["AYE_GRAFANA_PG_PASSWORD"] == "${AYE_GRAFANA_PG_PASSWORD}"
    assert "check-scripts" not in yaml.safe_load(tree["central/compose.yaml"])["services"]
    config = tree["central/checks/sql-checks.sh"]
    assert config.startswith(
        "check web-stuck web 60 10 10.8.0.1 5432 acme grafana_ro AYE_GRAFANA_PG_PASSWORD prefer"
        " <<'AYEOPS_SQL_END'\n"
    )
    assert config.endswith("rollback;\nAYEOPS_SQL_END\n")
    assert {"central/checks/sql_runner.sh", "central/checks/sql_metrics.awk"} <= set(tree)


def test_a_tls_datasource_requires_tls_from_psql(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    with_sql(data)["hosts"]["central"]["modules"]["grafana"]["datasources"][0]["tls"] = True
    tree = render_once(manifest_from(data), modules)
    assert " AYE_GRAFANA_PG_PASSWORD require <<" in tree["central/checks/sql-checks.sh"]


def test_the_query_runs_in_a_read_only_transaction_with_a_statement_timeout() -> None:
    check = make()
    assert sql_file(check) == (
        "begin transaction read only;\n"
        "set local statement_timeout = '9s';\n"
        'select "value"::numeric from (\n'
        f"{QUERY}\n"
        ") as ayeops_check;\n"
        "rollback;\n"
    )


def test_a_label_column_is_selected_before_the_value_and_a_trailing_semicolon_dropped() -> None:
    check = Check(
        "web",
        "stuck",
        {"type": "sql", "query": "select q as worker, 1 as value from w ;\n", "label": "worker"},
        "central",
    )
    text = sql_file(check)
    assert 'select "worker"::text, "value"::numeric from (\n' in text
    assert "\nselect q as worker, 1 as value from w\n) as ayeops_check;" in text


@pytest.mark.parametrize(("timeout", "expected"), [(10, "9s"), (2, "1s"), (1, "1s")])
def test_the_server_cancels_the_statement_a_second_before_the_runner_kills_psql(
    timeout: int, expected: str
) -> None:
    assert statement_timeout(timeout) == expected


@pytest.mark.parametrize(
    "query",
    [
        "select 1 as value",
        "SELECT 1 AS value;",
        "  with x as (select 1 as value) select value from x",
        "-- note\nselect updated_at, 1 as value from t",
        "/* a */ select 1 as value from created",
    ],
)
def test_reading_queries_pass_the_guard(query: str) -> None:
    assert sql_query_problem(query) is None


@pytest.mark.parametrize(
    ("query", "words"),
    [
        ("delete from t", "SELECT or WITH"),
        ("", "SELECT or WITH"),
        ("-- select\ndelete from t", "SELECT or WITH"),
        ("selected from t", "SELECT or WITH"),
        ("select 1; drop table t", "one statement"),
        ("with d as (delete from t returning 1) select 1 as value", "DELETE is refused"),
        ("select 1 as value into t2", "INTO is refused"),
        ("select nextval('s') as value; update t set a = 1", "one statement"),
        ("select 1 as value \\! rm -rf /", "backslash"),
        ("select 1 as value\nAYEOPS_SQL_END\n", "AYEOPS_SQL_END"),
        ("with u as (select 1) insert into t select 1", "INSERT is refused"),
    ],
)
def test_writing_or_smuggling_queries_are_refused(query: str, words: str) -> None:
    problem = sql_query_problem(query)
    assert problem is not None and words in problem


def test_a_refused_query_is_reported_by_aye_check(data: dict[str, Any]) -> None:
    spec(with_sql(data))["query"] = "update orders set stuck = false"
    assert rules_hit(data) == {"check-sql-query"}


@pytest.mark.parametrize("uid", ["nowhere", "acme-loki"])
def test_the_datasource_must_be_a_known_postgres_datasource(data: dict[str, Any], uid: str) -> None:
    with_sql(data)["hosts"]["central"]["modules"]["grafana"]["datasources"].append(
        {"name": "Logs", "uid": "acme-loki", "type": "loki", "url": "http://loki:3100"}
    )
    spec(data)["datasource"] = uid
    assert rules_hit(data) == {"check-sql-datasource"}


def test_sql_checks_need_node_on_the_probing_host(data: dict[str, Any]) -> None:
    del with_sql(data)["hosts"]["central"]["modules"]["node"]
    del data["hosts"]["central"]["modules"]["reconciler"]
    assert "check-script-node" in rules_hit(data)


@pytest.mark.parametrize(
    ("change", "value"),
    [("op", "=~"), ("threshold", "900"), ("no_data", "maybe")],
)
def test_a_bad_alert_condition_is_a_schema_error(
    data: dict[str, Any], change: str, value: str
) -> None:
    spec(with_sql(data))["alert"][change] = value
    assert rules_hit(data) == {"schema"}


@pytest.mark.parametrize("field", ["op", "threshold"])
def test_an_sql_alert_needs_its_condition(data: dict[str, Any], field: str) -> None:
    del spec(with_sql(data))["alert"][field]
    assert rules_hit(data) == {"schema"}


def test_the_condition_fields_are_only_for_sql_checks(data: dict[str, Any]) -> None:
    with_sql(data)["services"]["web"]["checks"]["ssh"] = {
        "type": "tcp",
        "address": "10.8.0.1:22",
        "alert": sql_alert(),
    }
    assert rules_hit(data) == {"schema"}


def test_the_sql_timeout_leaves_psql_room_to_connect(data: dict[str, Any]) -> None:
    spec(with_sql(data))["timeout"] = 1
    assert rules_hit(data) == {"schema"}


def test_the_alert_compares_with_bool_and_fills_missing_values_with_ok() -> None:
    assert sql_expr(make(op="<", threshold=1)) == (
        '(max(ayeops_check_sql_value{check="web-stuck"} < bool 1) or vector(0)) + '
        '(1 - max(ayeops_check_sql_ok{check="web-stuck"} * '
        '(time() - ayeops_check_sql_last_run_timestamp_seconds{check="web-stuck"} < bool 190)))'
    )


def test_no_data_alerting_fills_a_missing_value_with_firing() -> None:
    assert "or vector(1)) + " in sql_expr(make(no_data="alerting"))
    assert "or vector(0)) + " in sql_expr(make(no_data="ok"))


def test_the_staleness_window_follows_interval_and_timeout() -> None:
    check = Check("web", "stuck", {**make().spec, "interval": 300, "timeout": 20}, "central")
    assert "< bool 680)))" in sql_expr(check)


@pytest.mark.parametrize("op", [">", ">=", "<", "<=", "==", "!="])
@pytest.mark.parametrize("no_data", ["ok", "alerting"])
def test_generated_sql_alerts_pass_the_filter_nodata_rule(op: str, no_data: str) -> None:
    alert = check_alert(make(op=op, threshold=5, no_data=no_data))
    assert not may_be_empty(alert["expr"])
    assert check_nodata("x", alert, "Alerting") == []


def test_the_condition_fields_stay_out_of_the_grafana_rule(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    spec(with_sql(data))["alert"].update(no_data="alerting", title="Orders stuck")
    rule = services_rules(render_once(manifest_from(data), modules))["aye-web-stuck"]
    assert rule["title"] == "Orders stuck"
    assert rule["noDataState"] == "Alerting"
    assert rule["labels"] == {
        "severity": "warning",
        "scope": escape_provisioning(SCOPE),
        "service": "web",
    }
    alert = check_alert(make(no_data="alerting"))
    assert not {"op", "threshold", "no_data"} & set(alert)


def metrics(stdout: str, code: int = 0) -> list[str]:
    awk = shutil.which("awk")
    if awk is None:
        pytest.skip("awk is not installed")
    done = subprocess.run(
        [
            awk,
            "-F",
            "\t",
            "-v",
            "check=web-stuck",
            "-v",
            "service=web",
            "-v",
            f"code={code}",
            "-v",
            "begin=10.25",
            "-v",
            "end=10.5",
            "-v",
            "now=99",
            "-f",
            str(FILES / "sql_metrics.awk"),
        ],
        input=stdout.encode(),
        capture_output=True,
        check=True,
    )
    return done.stdout.decode().splitlines()


LABELS = 'check="web-stuck",service="web"'
TAIL = [
    f"ayeops_check_sql_ok{{{LABELS}}} 1",
    f"ayeops_check_sql_duration_seconds{{{LABELS}}} 0.250",
    f"ayeops_check_sql_last_run_timestamp_seconds{{{LABELS}}} 99",
]


def test_the_runner_writes_value_ok_duration_and_last_run() -> None:
    assert metrics("900\n") == [f"ayeops_check_sql_value{{{LABELS}}} 900", *TAIL]


def test_extra_rows_without_a_label_keep_only_the_first_so_the_textfile_stays_valid() -> None:
    assert metrics("1.5\n-2\n") == [f"ayeops_check_sql_value{{{LABELS}}} 1.5", *TAIL]


def test_labels_are_escaped_and_null_or_text_values_skipped() -> None:
    lines = metrics('a"b\\c\t3\nnull\t\nn\tNaN\nnull\t4\nnull\t5\n')
    assert lines == [
        f'ayeops_check_sql_value{{{LABELS},label="a\\"b\\\\c"}} 3',
        f'ayeops_check_sql_value{{{LABELS},label="null"}} 4',
        *TAIL,
    ]


def test_a_failed_query_reports_not_ok_and_no_value() -> None:
    lines = metrics("900\n", code=2)
    assert lines[0] == f"ayeops_check_sql_ok{{{LABELS}}} 0"
    assert not any(line.startswith("ayeops_check_sql_value") for line in lines)


FAKE_PSQL = """#!/bin/sh
mkdir "$AYE_TEST_BUSY" 2>/dev/null || echo overlap >>"$AYE_TEST_LOG"
sleep 0.3
rmdir "$AYE_TEST_BUSY" 2>/dev/null
echo 7
"""


@pytest.mark.skipif(
    sys.platform == "win32" or not all(shutil.which(t) for t in ("sh", "flock", "timeout")),
    reason="needs a POSIX shell with flock and timeout",
)
def test_the_runner_runs_one_query_at_a_time_so_a_role_connection_limit_holds(
    tmp_path: Path,
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    psql = bin_dir / "psql"
    psql.write_text(FAKE_PSQL)
    psql.chmod(0o755)
    line = (
        "check web-{n} web 60 5 db 5432 acme ro AYE_PW prefer <<'AYEOPS_SQL_END'\n"
        "select 1\nAYEOPS_SQL_END\n"
    )
    config = tmp_path / "sql-checks.sh"
    config.write_text("".join(line.format(n=n) for n in range(4)))
    textfile = tmp_path / "ayeops_check_sql.prom"
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "AYE_CHECKS_CONFIG": str(config),
        "AYE_CHECKS_TEXTFILE": str(textfile),
        "AYE_SQL_STATE": str(tmp_path / "state"),
        "AYE_SQL_METRICS": str(FILES / "sql_metrics.awk"),
        "AYE_TEST_BUSY": str(tmp_path / "busy"),
        "AYE_TEST_LOG": str(tmp_path / "log"),
    }
    process = subprocess.Popen(
        ["sh", str(FILES / "sql_runner.sh")],
        env=env,
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + WAIT
        state = tmp_path / "state"
        while time.monotonic() < deadline and len(list(state.glob("*.prom"))) < 4:
            time.sleep(0.2)
    finally:
        if sys.platform != "win32":
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=WAIT)
    assert len(list(state.glob("*.prom"))) == 4
    assert not (tmp_path / "log").exists()


@pytest.mark.parametrize(
    ("query", "message"),
    [
        ("drop table t", "the query must start with SELECT or WITH"),
        ("select 1; select 2", "the query must be one statement, without ; inside"),
        (
            "select '\\' as value",
            "the query must not hold a backslash, psql would read it as a command",
        ),
        ("select 1 as AYEOPS_SQL_END", "the query must not hold AYEOPS_SQL_END"),
        ("select 1 as value into t", "the query must only read, INTO is refused"),
    ],
)
def test_the_guard_explains_what_it_refused(query: str, message: str) -> None:
    assert sql_query_problem(query) == message


def test_only_the_trailing_semicolon_and_space_are_dropped() -> None:
    assert sql_query("  select 1 as X;; \n") == "select 1 as X"
    assert sql_query("select 1 as X ;") == "select 1 as X"
