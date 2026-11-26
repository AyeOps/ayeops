from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from ayeops.offsite import drill_context, drill_lines, minute_of_day
from ayeops.portal.graph import build_graph
from ayeops.registry import Module
from ayeops.render import grafana_rules, host_compose
from ayeops.rules import check_memory_budget, parse_size
from tests.conftest import ROOT, WAIT, manifest_from, pinned, render_once, rules_hit, tag_of

DRILL = ROOT / "src" / "ayeops" / "modules" / "objectstore" / "files" / "restore_drill.sh"
DAY = 86400
NOON = 1_800_000_000 - 1_800_000_000 % DAY + 13 * 3600
PREFIX = "ayeops_offsite_drill_"
QUERY = "select count(*) as value from orders"
COUNT = "select count(*) as value from gone"
FRESH = "select (max(placed_at) > now() - interval '36 hours')::int as value from orders"


def drill_spec(**extra: Any) -> dict[str, Any]:
    return {
        "engine": "postgres",
        "version": 18,
        "checks": [{"name": "orders", "query": QUERY}, {"name": "fresh", "query": FRESH}],
        **extra,
    }


@pytest.fixture
def rest(data: dict[str, Any]) -> dict[str, Any]:
    data["hosts"]["central"]["modules"]["objectstore"] = {
        "provider": "rest-server",
        "quota": "40g",
        "expose": [8000],
    }
    data["hosts"]["edge"]["modules"]["offsite"]["restore_drill"] = drill_spec()
    return data


def drill_of(data: dict[str, Any]) -> dict[str, Any]:
    spec: dict[str, Any] = data["hosts"]["edge"]["modules"]["offsite"]["restore_drill"]
    return spec


def central(data: dict[str, Any], modules: dict[str, Module]) -> dict[str, Any]:
    result: dict[str, Any] = host_compose(manifest_from(data), "central", modules)["services"]
    return result


def problems_of(data: dict[str, Any]) -> list[str]:
    from ayeops.offsite import offsite_problems

    return [
        text for _, rule, text in offsite_problems(manifest_from(data)) if rule == "offsite-drill"
    ]


def test_a_drill_on_a_rest_server_store_passes_every_rule(rest: dict[str, Any]) -> None:
    assert rules_hit(rest) == set()


@pytest.mark.parametrize(
    ("setting", "value"),
    [
        ("engine", "mysql"),
        ("version", 13),
        ("version", "18"),
        ("every_days", 0),
        ("schedule", "7:00"),
        ("max_minutes", 1),
        ("jobs", 0),
        ("checks", []),
        ("checks", [{"name": "Orders", "query": QUERY}]),
        ("checks", [{"name": "orders"}]),
        ("unknown", True),
    ],
)
def test_drill_settings_outside_the_schema_are_rejected(
    rest: dict[str, Any], setting: str, value: Any
) -> None:
    drill_of(rest)[setting] = value
    assert rules_hit(rest) == {"module-settings"}


@pytest.mark.parametrize("missing", ["engine", "version", "checks"])
def test_engine_version_and_checks_are_required(rest: dict[str, Any], missing: str) -> None:
    del drill_of(rest)[missing]
    assert rules_hit(rest) == {"module-settings"}


def test_a_garage_store_or_a_url_target_cannot_host_the_drill(rest: dict[str, Any]) -> None:
    rest["hosts"]["central"]["modules"]["objectstore"] = {
        "bucket": "acme-offsite",
        "expose": [3900],
    }
    assert rules_hit(rest) == {"offsite-drill"}
    assert any("provider rest-server" in text for text in problems_of(rest))
    offsite = rest["hosts"]["edge"]["modules"]["offsite"]
    offsite["target"] = {"url": "s3:https://s3.example/bucket/edge"}
    assert rules_hit(rest) == {"offsite-drill"}


def test_the_drill_reports_through_node_on_the_store(rest: dict[str, Any]) -> None:
    modules = rest["hosts"]["central"]["modules"]
    for name in ("node", "reconciler", "hygiene"):
        modules.pop(name, None)
    assert "offsite-drill" in rules_hit(rest)
    assert "the drill reports through node on central" in problems_of(rest)


def test_a_plain_dump_cannot_be_drilled_with_pg_restore(rest: dict[str, Any]) -> None:
    rest["hosts"]["edge"]["modules"]["offsite"]["verify"] = "none"
    assert rules_hit(rest) == {"offsite-drill"}


def test_check_queries_must_read_and_names_must_be_unique(rest: dict[str, Any]) -> None:
    drill_of(rest)["checks"] = [
        {"name": "orders", "query": f"with gone as (delete from orders returning 1) {COUNT}"},
        {"name": "orders", "query": QUERY},
    ]
    found = problems_of(rest)
    assert "the check name 'orders' is used twice" in found
    assert "the query must only read, DELETE is refused" in found


@pytest.mark.parametrize(
    ("schedule", "minutes", "clash"),
    [("10:30", 60, True), ("11:00", 30, True), ("10:30", 20, False), ("11:01", 600, False)],
)
def test_the_drill_window_must_not_hold_the_prune(
    rest: dict[str, Any], schedule: str, minutes: int, clash: bool
) -> None:
    drill_of(rest).update(schedule=schedule, max_minutes=minutes)
    assert (rules_hit(rest) == {"offsite-drill"}) is clash


def test_a_window_across_midnight_still_sees_the_prune(rest: dict[str, Any]) -> None:
    rest["hosts"]["central"]["modules"]["objectstore"]["prune_schedule"] = "00:30"
    drill_of(rest).update(schedule="23:45", max_minutes=60)
    assert rules_hit(rest) == {"offsite-drill"}


def test_drills_on_one_store_share_one_postgres_version(rest: dict[str, Any]) -> None:
    rest["hosts"]["third"] = {
        "arch": "amd64",
        "memory": "4g",
        "modules": {
            "metrics": {"role": "agent", "remote_write": {"host": "central"}},
            "node": {},
            "offsite": {
                "source": "/var/backups/third/*.dump",
                "target": {"host": "central"},
                "restore_drill": drill_spec(version=16),
            },
        },
    }
    assert rules_hit(rest) == {"offsite-drill"}
    assert any("use one version, not [16, 18]" in text for text in problems_of(rest))
    rest["hosts"]["third"]["modules"]["offsite"]["restore_drill"]["version"] = 18
    assert rules_hit(rest) == set()
    context = drill_context(manifest_from(rest), "central", {"provider": "rest-server"})
    assert context is not None
    assert [client["name"] for client in context["clients"]] == ["edge", "third"]


@pytest.mark.parametrize("version", [14, 15, 16, 17, 18])
def test_the_drill_runs_the_pinned_postgres_of_the_chosen_major(
    rest: dict[str, Any], modules: dict[str, Module], version: int
) -> None:
    drill_of(rest)["version"] = version
    image = tag_of(central(rest, modules)["offsite-drill"]["image"])
    assert image == pinned("objectstore", "postgres", rf"^{version}\.")


def test_the_drill_has_no_network_no_ports_and_reads_the_repositories_read_only(
    rest: dict[str, Any], modules: dict[str, Module]
) -> None:
    drill = central(rest, modules)["offsite-drill"]
    assert drill["network_mode"] == "none"
    assert "ports" not in drill
    assert drill["read_only"] is True
    assert drill["cap_drop"] == ["ALL"]
    assert set(drill["cap_add"]) == {"CHOWN", "DAC_OVERRIDE", "SETGID", "SETUID"}
    assert "reststore-data:/data:ro" in drill["volumes"]
    assert "/scratch" in drill["volumes"]
    assert "node-textfile:/textfile" in drill["volumes"]
    assert drill["environment"]["AYE_RESTSTORE_EDGE_RESTIC_PASSWORD"] == (
        "${AYE_RESTSTORE_EDGE_RESTIC_PASSWORD:?set AYE_RESTSTORE_EDGE_RESTIC_PASSWORD}"
    )
    assert not any("AYE_RESTSTORE_EDGE_PASSWORD" in key for key in drill["environment"])
    rest["hosts"]["central"]["modules"]["objectstore"]["data_dir"] = "/srv/offsite"
    assert "/srv/offsite:/data:ro" in central(rest, modules)["offsite-drill"]["volumes"]


def test_no_drill_no_drill_container_and_no_drill_files(
    rest: dict[str, Any], modules: dict[str, Module]
) -> None:
    del rest["hosts"]["edge"]["modules"]["offsite"]["restore_drill"]
    assert "offsite-drill" not in central(rest, modules)
    tree = render_once(manifest_from(rest), modules)
    assert "central/objectstore/restore_drill.sh" not in tree
    assert "central/objectstore/drills.sh" not in tree


def test_the_driver_and_its_config_ship_next_to_the_compose_file(
    rest: dict[str, Any], modules: dict[str, Module]
) -> None:
    tree = render_once(manifest_from(rest), modules)
    assert tree["central/objectstore/restore_drill.sh"] == DRILL.read_text(encoding="utf-8")
    config = tree["central/objectstore/drills.sh"]
    assert config.startswith("drill edge 7 46800 3600 2 AYE_RESTSTORE_EDGE_RESTIC_PASSWORD\n")
    assert "check edge orders <<'AYEOPS_SQL_END'\nbegin transaction read only;\n" in config
    assert f"\n{QUERY}\n" in config


def test_drill_lines_use_the_minute_of_day_and_seconds() -> None:
    spec = {**drill_spec(), "every_days": 3, "schedule": "02:05", "max_minutes": 90, "jobs": 4}
    first = drill_lines("db-1", spec).splitlines()[0]
    assert first == "drill db-1 3 7500 5400 4 AYE_RESTSTORE_DB_1_RESTIC_PASSWORD"
    assert minute_of_day("23:59") == 1439


def test_the_memory_budget_counts_the_drill_container(
    rest: dict[str, Any], modules: dict[str, Module]
) -> None:
    rest["hosts"]["central"]["resources"] = {}
    limits = {name: s["mem_limit"] for name, s in central(rest, modules).items()}
    assert limits["offsite-drill"] == "1g"
    used = sum(parse_size(str(size)) for size in limits.values())
    reserved = parse_size("2g")
    room = used + reserved - parse_size("512m")
    rest["hosts"]["central"]["memory"] = f"{room * 100 // 85 // 1024**2}m"
    assert check_memory_budget(manifest_from(rest), "central", modules)
    del rest["hosts"]["edge"]["modules"]["offsite"]["restore_drill"]
    assert check_memory_budget(manifest_from(rest), "central", modules) == []


def test_the_drill_memory_follows_the_store_resources(
    rest: dict[str, Any], modules: dict[str, Module]
) -> None:
    rest["hosts"]["central"]["modules"]["objectstore"]["resources"] = {
        "offsite-drill": {"memory": "3g"}
    }
    assert central(rest, modules)["offsite-drill"]["mem_limit"] == "3g"


def drill_rules(data: dict[str, Any], modules: dict[str, Module]) -> dict[str, Any]:
    groups = grafana_rules(manifest_from(data), modules)["groups"]
    return {r["uid"]: r for g in groups if g["name"] == "offsite" for r in g["rules"]}


def test_the_drill_alert_exists_only_with_a_drill_and_fires_on_silence(
    data: dict[str, Any], rest: dict[str, Any], modules: dict[str, Module]
) -> None:
    rule = drill_rules(rest, modules)["aye-offsite-drill"]
    assert rule["noDataState"] == "Alerting"
    expr = rule["data"][0]["model"]["expr"]
    assert "time() > bool ayeops_offsite_drill_deadline_timestamp_seconds" in expr
    assert "ayeops_offsite_drill_ok == bool 0" in expr
    assert rule["annotations"]["runbook_url"].endswith("/docs/runbooks/offsite-drill.md")
    del rest["hosts"]["edge"]["modules"]["offsite"]["restore_drill"]
    assert "aye-offsite-drill" not in drill_rules(rest, modules)


def test_the_portal_graph_stays_clean_with_a_drill(
    rest: dict[str, Any], modules: dict[str, Module]
) -> None:
    graph = build_graph(manifest_from(rest), modules)
    assert graph.problems == []
    assert "central/offsite-drill" in graph.nodes


def test_rendered_compose_with_a_drill_is_valid_yaml(
    rest: dict[str, Any], modules: dict[str, Module]
) -> None:
    parsed = yaml.safe_load(render_once(manifest_from(rest), modules)["central/compose.yaml"])
    assert parsed["services"]["offsite-drill"]["entrypoint"] == ["/bin/sh", "/app/restore_drill.sh"]


DUMP = b"PGDMP" + b"\x01" * 4091
SHA = hashlib.sha256(DUMP).hexdigest()
SNAPSHOT_ID = "a1b2c3d4" + "0" * 56
FILE = "/var/backups/edge/e.dump"


def listing(sha: str) -> str:
    snapshot = {
        "time": "2026-10-31T04:30:00+02:00",
        "paths": [FILE],
        "hostname": "edge",
        "tags": ["ayeops", f"sha256={sha}"],
        "id": SNAPSHOT_ID,
        "short_id": SNAPSHOT_ID[:8],
        "message_type": "snapshot",
        "struct_type": "snapshot",
    }
    nodes = [
        {"name": "var", "type": "dir", "path": "/var", "struct_type": "node"},
        {"name": "e.dump", "type": "file", "path": FILE, "size": 4096, "struct_type": "node"},
    ]
    lines = [json.dumps(item, separators=(",", ":")) for item in [snapshot, *nodes]]
    return "\n".join(lines) + "\n"


RESTIC = """#!/bin/sh
echo "$*" >>"$T/restic.args"
case " $* " in
  *" ls "*)
    [ -z "${T_LS_FAIL:-}" ] || { echo "Fatal: repository does not exist" >&2; exit 1; }
    cat "$T/ls.json"
    ;;
  *" dump "*)
    [ -z "${T_HANG:-}" ] || exec sleep 60 >/dev/null 2>&1
    echo $(($(cat "$T/clock") + 30)) >"$T/clock"
    cat "$T/dump.bin"
    ;;
esac
"""

INITDB = """#!/bin/sh
echo "initdb $*" >>"$T/calls"
mkdir -p "$2"
: >"$2/postgresql.conf"
"""

PG_CTL = """#!/bin/sh
echo "pg_ctl $*" >>"$T/calls"
case " $* " in
  *" start"*) : >"$2/postmaster.pid" ;;
  *" stop"*) rm -f "$2/postmaster.pid" ;;
esac
"""

CREATEDB = """#!/bin/sh
echo "createdb $*" >>"$T/calls"
"""

PG_RESTORE = """#!/bin/sh
echo "pg_restore $*" >>"$T/calls"
echo "restore sees password=${RESTIC_PASSWORD:-}." >>"$T/calls"
echo $(($(cat "$T/clock") + 90)) >"$T/clock"
[ -z "${T_RESTORE_FAIL:-}" ] || { echo "pg_restore: error: could not execute query" >&2; exit 1; }
"""

PSQL = """#!/bin/sh
echo "psql $*" >>"$T/calls"
case " $* " in
  *" -c "*) echo 7340032 ;;
  *) for last; do :; done
     case "$last" in *fresh.sql) echo "${T_FRESH-1}" ;; *) echo 1250 ;; esac ;;
esac
"""

STUBS = {
    "initdb": INITDB,
    "pg_ctl": PG_CTL,
    "createdb": CREATEDB,
    "pg_restore": PG_RESTORE,
    "psql": PSQL,
}

CONFIG = drill_lines(
    "edge", {**drill_spec(), "every_days": 7, "schedule": "13:00", "max_minutes": 60, "jobs": 2}
)


class Drill:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.bin = root / "bin"
        self.textfile = root / "textfile"
        self.scratch = root / "scratch"
        for directory in (self.bin, self.textfile, self.scratch):
            directory.mkdir()
        for name, body in {**STUBS, "restic": RESTIC}.items():
            path = self.bin / name
            path.write_text(body, encoding="utf-8", newline="\n")
            path.chmod(0o755)
        (root / "dump.bin").write_bytes(DUMP)
        (root / "drills.sh").write_text(CONFIG, encoding="utf-8", newline="\n")
        self.tag(SHA)
        self.set_clock(NOON)

    def tag(self, sha: str) -> None:
        (self.root / "ls.json").write_text(listing(sha), encoding="utf-8", newline="\n")

    def set_clock(self, moment: int) -> None:
        (self.root / "clock").write_text(f"{moment}\n", encoding="utf-8")

    def run(self, **extra: str) -> subprocess.CompletedProcess[str]:
        shell = shutil.which("sh")
        if shell is None:
            pytest.skip("no POSIX shell")
        env = {
            **os.environ,
            "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
            "T": self.root.as_posix(),
            "AYE_DRILL_CONFIG": (self.root / "drills.sh").as_posix(),
            "AYE_DRILL_DATA": (self.root / "data").as_posix(),
            "AYE_DRILL_SCRATCH": self.scratch.as_posix(),
            "AYE_DRILL_TEXTFILE_DIR": self.textfile.as_posix(),
            "AYE_DRILL_WORK": (self.root / "work").as_posix(),
            "AYE_RESTIC": (self.bin / "restic").as_posix(),
            "AYE_DRILL_USER": "",
            "AYE_DRILL_SETSID": "",
            "AYE_DRILL_CLOCK": f"cat {(self.root / 'clock').as_posix()}",
            "AYE_DRILL_POLL": "1",
            "AYE_DRILL_ONCE": "1",
            "AYE_RESTSTORE_EDGE_RESTIC_PASSWORD": "s3cret",
            **extra,
        }
        return subprocess.run(
            [shell, DRILL.as_posix()],
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=WAIT,
        )

    def metrics(self) -> dict[str, str]:
        text = (self.textfile / f"{PREFIX}edge.prom").read_text(encoding="utf-8")
        return dict(line.rsplit(" ", 1) for line in text.splitlines())

    def calls(self) -> str:
        path = self.root / "calls"
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def restic_args(self) -> str:
        path = self.root / "restic.args"
        return path.read_text(encoding="utf-8") if path.exists() else ""


def series(name: str, **labels: str) -> str:
    inner = ",".join(f'{key}="{value}"' for key, value in {"repo": "edge", **labels}.items())
    return f"{PREFIX}{name}{{{inner}}}"


@pytest.fixture
def drill(tmp_path: Path) -> Drill:
    return Drill(tmp_path)


def test_a_passing_drill_records_rto_sizes_checks_and_cleans_up(drill: Drill) -> None:
    done = drill.run()
    assert done.returncode == 0, done.stderr
    found = drill.metrics()
    finished = NOON + 120
    assert found[series("ok")] == "1"
    assert found[series("last_attempt_timestamp_seconds")] == str(NOON)
    assert found[series("last_success_timestamp_seconds")] == str(finished)
    assert found[series("since_timestamp_seconds")] == str(NOON)
    assert found[series("deadline_timestamp_seconds")] == str(finished + 8 * DAY)
    assert found[series("fetch_seconds")] == "30"
    assert found[series("restore_seconds")] == "90"
    assert found[series("duration_seconds")] == "120"
    assert found[series("dump_bytes")] == str(len(DUMP))
    assert found[series("restored_bytes")] == "7340032"
    assert found[series("check_value", check="orders")] == "1250"
    assert found[series("check_ok", check="orders")] == "1"
    assert found[series("check_ok", check="fresh")] == "1"
    assert list(drill.scratch.iterdir()) == []
    assert "drill passed in 120s" in done.stderr


def test_restic_only_reads_without_a_lock_or_a_cache_from_the_clients_repository(
    drill: Drill,
) -> None:
    drill.run()
    lines = drill.restic_args().splitlines()
    repo = (drill.root / "data" / "edge").as_posix()
    assert lines == [
        f"-r {repo} --no-lock --no-cache ls --json --host edge latest",
        f"-r {repo} --no-lock --no-cache dump {SNAPSHOT_ID} {FILE}",
    ]


def test_postgres_listens_on_a_socket_only_restores_in_parallel_and_is_stopped(
    drill: Drill,
) -> None:
    drill.run()
    calls = drill.calls()
    scratch = drill.scratch.as_posix()
    assert f"initdb -D {scratch}/pg -U postgres -A trust -E UTF8 --no-sync" in calls
    assert (
        f"pg_restore -h {scratch}/run -U postgres -d drill --no-owner --no-privileges"
        f" --exit-on-error -j 2 {scratch}/dump"
    ) in calls
    assert f"pg_ctl -D {scratch}/pg -m immediate -w stop" in calls
    assert calls.index("pg_restore") < calls.index("stop")


def test_the_server_config_turns_tcp_off(drill: Drill, tmp_path: Path) -> None:
    keep = tmp_path / "kept.conf"
    stub = drill.bin / "createdb"
    stub.write_text(
        CREATEDB + f'cp "$T/scratch/pg/postgresql.conf" "{keep.as_posix()}"\n',
        encoding="utf-8",
        newline="\n",
    )
    drill.run()
    text = keep.read_text(encoding="utf-8")
    assert "listen_addresses = ''" in text
    assert f"unix_socket_directories = '{drill.scratch.as_posix()}/run'" in text


def test_a_check_that_is_not_positive_fails_the_drill_and_names_itself(drill: Drill) -> None:
    done = drill.run(T_FRESH="0")
    found = drill.metrics()
    assert found[series("ok")] == "0"
    assert found[series("check_ok", check="fresh")] == "0"
    assert found[series("check_value", check="fresh")] == "0"
    assert found[series("check_ok", check="orders")] == "1"
    assert series("last_success_timestamp_seconds") not in found
    assert found[series("deadline_timestamp_seconds")] == str(NOON + 8 * DAY)
    assert "check fresh returned '0'" in done.stderr
    assert list(drill.scratch.iterdir()) == []


def test_a_non_numeric_check_result_fails_without_a_value(drill: Drill) -> None:
    drill.run(T_FRESH="")
    found = drill.metrics()
    assert found[series("check_ok", check="fresh")] == "0"
    assert series("check_value", check="fresh") not in found


def test_a_changed_dump_never_reaches_postgres(drill: Drill) -> None:
    drill.tag("f" * 64)
    done = drill.run()
    found = drill.metrics()
    assert found[series("ok")] == "0"
    assert "differs from the recorded" in done.stderr
    assert "initdb" not in drill.calls()
    assert series("duration_seconds") not in found
    assert found[series("dump_bytes")] == str(len(DUMP))


def test_a_file_that_is_not_a_custom_format_dump_fails(drill: Drill) -> None:
    plain = b"-- PostgreSQL database dump\n"
    (drill.root / "dump.bin").write_bytes(plain)
    drill.tag(hashlib.sha256(plain).hexdigest())
    done = drill.run()
    assert drill.metrics()[series("ok")] == "0"
    assert "is not a pg_dump custom-format archive" in done.stderr


def test_a_failed_restore_stops_postgres_and_wipes_the_scratch(drill: Drill) -> None:
    done = drill.run(T_RESTORE_FAIL="1")
    assert drill.metrics()[series("ok")] == "0"
    assert "pg_restore: error: could not execute query" in done.stderr
    assert "stop" in drill.calls()
    assert list(drill.scratch.iterdir()) == []


def test_a_missing_repository_or_password_fails_with_the_cause(drill: Drill) -> None:
    done = drill.run(T_LS_FAIL="1")
    assert drill.metrics()[series("ok")] == "0"
    assert "restic ls: Fatal: repository does not exist" in done.stderr
    (drill.textfile / f"{PREFIX}edge.prom").unlink()
    done = drill.run(AYE_RESTSTORE_EDGE_RESTIC_PASSWORD="")
    assert "AYE_RESTSTORE_EDGE_RESTIC_PASSWORD is empty" in done.stderr
    assert drill.restic_args().count(" ls ") == 1


def test_a_drill_past_max_minutes_is_killed_and_cleaned_up(drill: Drill) -> None:
    config = drill.root / "drills.sh"
    config.write_text(
        config.read_text(encoding="utf-8").replace(" 3600 2 ", " 1 2 ", 1),
        encoding="utf-8",
        newline="\n",
    )
    done = drill.run(T_HANG="1")
    assert done.returncode == 0, done.stderr
    assert drill.metrics()[series("ok")] == "0"
    assert "stopped after 1 seconds" in done.stderr
    assert list(drill.scratch.iterdir()) == []


def test_leftovers_of_a_killed_container_are_wiped_at_start(drill: Drill) -> None:
    (drill.scratch / "pg").mkdir()
    (drill.scratch / "pg" / "postmaster.pid").write_text("1\n", encoding="utf-8")
    (drill.scratch / "dump").write_bytes(DUMP)
    drill.set_clock(NOON - 3600)
    write_state(drill, NOON - DAY, NOON - DAY + 60, NOON - DAY)
    drill.run()
    assert list(drill.scratch.iterdir()) == []
    assert "stop" in drill.calls()
    assert "pg_restore" not in drill.calls()


def write_state(drill: Drill, attempt: int | None, success: int | None, since: int) -> None:
    lines = [f"{series('since_timestamp_seconds')} {since}"]
    if attempt is not None:
        lines.append(f"{series('last_attempt_timestamp_seconds')} {attempt}")
        lines.append(f"{series('ok')} {0 if success is None else 1}")
    if success is not None:
        lines.append(f"{series('last_success_timestamp_seconds')} {success}")
    (drill.textfile / f"{PREFIX}edge.prom").write_text("\n".join(lines) + "\n", "utf-8")


@pytest.mark.parametrize(
    ("now", "attempt", "success", "runs"),
    [
        (NOON + 6 * DAY + 60, NOON, NOON + 60, False),
        (NOON + 7 * DAY - 60, NOON, NOON + 60, False),
        (NOON + 7 * DAY, NOON, NOON + 60, True),
        (NOON + 7 * DAY + 5 * 3600, NOON, NOON + 60, True),
        (NOON + 7 * DAY + 60, NOON + 7 * DAY, NOON + 60, False),
        (NOON + 8 * DAY, NOON + 7 * DAY, NOON + 60, True),
        (NOON + 2 * DAY - 60, NOON, None, True),
        (NOON - 60, NOON - DAY, None, False),
    ],
)
def test_drills_run_at_the_slot_every_days_and_retry_once_a_day(
    drill: Drill, now: int, attempt: int, success: int | None, runs: bool
) -> None:
    write_state(drill, attempt, success, NOON - DAY)
    drill.set_clock(now)
    drill.run()
    assert ("pg_restore" in drill.calls()) is runs


def test_the_first_drill_runs_at_once_whatever_the_time_of_day(drill: Drill) -> None:
    drill.set_clock(NOON - 5 * 3600)
    drill.run()
    assert "pg_restore" in drill.calls()
    found = drill.metrics()
    assert found[series("since_timestamp_seconds")] == str(NOON - 5 * 3600)
    assert found[series("ok")] == "1"


def test_between_drills_the_textfile_is_left_as_the_last_drill_wrote_it(drill: Drill) -> None:
    drill.run()
    before = (drill.textfile / f"{PREFIX}edge.prom").read_text(encoding="utf-8")
    drill.set_clock(NOON + 3 * DAY)
    drill.run()
    assert (drill.textfile / f"{PREFIX}edge.prom").read_text(encoding="utf-8") == before
    assert drill.calls().count("pg_restore") == 1


def test_textfiles_of_clients_without_a_drill_are_removed(drill: Drill) -> None:
    gone = drill.textfile / f"{PREFIX}retired.prom"
    gone.write_text(f'{PREFIX}ok{{repo="retired"}} 0\n', encoding="utf-8")
    other = drill.textfile / "ayeops_offsite.prom"
    other.write_text("ayeops_offsite_snapshots 3\n", encoding="utf-8")
    drill.run()
    assert not gone.exists()
    assert other.exists()


def test_postgres_and_its_tools_never_see_the_repository_password(drill: Drill) -> None:
    drill.run()
    assert "restore sees password=." in drill.calls()
