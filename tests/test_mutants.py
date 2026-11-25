from __future__ import annotations

import ast
import difflib
import fnmatch
import json
import re
import shutil
import subprocess
import tomllib
from pathlib import Path
from typing import Any

import pytest
import yaml

import ayeops.mutants
from ayeops.mutants import (
    CONFIG,
    MutantsError,
    Target,
    cache_keys,
    changed_functions,
    changed_lines,
    checked,
    diff_functions,
    dump_targets,
    floor_tenth,
    load_targets,
    main,
    module_name,
    mutated_functions,
    mutmut_counts,
    narrowed,
    parse_targets,
    patterns,
    raised,
    select,
    verdicts,
)
from tests.conftest import ROOT

PYPROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
MUTMUT = PYPROJECT["tool"]["mutmut"]
TARGETS = load_targets(ROOT / CONFIG)
WORKFLOW = yaml.safe_load((ROOT / ".github" / "workflows" / "mutants.yml").read_text("utf-8"))

RESULTS = """\
    ayeops.promql.x_parse__mutmut_1: killed
    ayeops.promql.x_parse__mutmut_2: survived
    ayeops.promql.xǁLexerǁnext__mutmut_1: timeout
    ayeops.promql.x_parse__mutmut_3: no tests
    ayeops.promql.x_parse__mutmut_4: not checked
    ayeops.deploy.x_run__mutmut_1: killed
    ayeops.deploy.x_run__mutmut_2: killed
    ayeops.deploy.x_run__mutmut_3: survived
"""


def imported_names(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            names.add(node.module)
            names.update(f"{node.module}.{alias.name}" for alias in node.names)
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "spec_from_file_location"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            names.add(node.args[0].value)
    return names


def test_mutmut_mutates_exactly_the_modules_of_the_mutants_file() -> None:
    assert MUTMUT["source_paths"] == ["src/"]
    assert MUTMUT["only_mutate"] == sorted(TARGETS)


def test_mutmut_runs_exactly_the_tests_mapped_to_the_modules() -> None:
    mapped = {test for target in TARGETS.values() for test in target.tests}
    assert MUTMUT["pytest_add_cli_args_test_selection"] == sorted(mapped)


def test_mutmut_is_pinned_to_an_exact_version() -> None:
    (requirement,) = PYPROJECT["dependency-groups"]["mutants"]
    assert re.fullmatch(r"mutmut==\d+\.\d+\.\d+", requirement)


def test_mutmut_options_never_spread_the_tests_over_xdist_workers() -> None:
    args = MUTMUT.get("pytest_add_cli_args", []) + MUTMUT["pytest_add_cli_args_test_selection"]
    assert not {"-n", "--numprocesses", "--dist"} & set(args)
    assert "-n" not in str(PYPROJECT["tool"]["pytest"]["ini_options"].get("addopts", ""))


@pytest.mark.parametrize("target", TARGETS.values(), ids=lambda t: t.path)
def test_every_mapped_test_file_imports_its_module(target: Target) -> None:
    assert (ROOT / target.path).is_file()
    assert target.tests
    for test in target.tests:
        assert test.startswith("tests/test_") and (ROOT / test).is_file(), test
        assert target.module in imported_names(ROOT / test), f"{test} never imports {target.module}"


def test_the_mutants_file_is_kept_in_the_form_score_write_produces() -> None:
    text = (ROOT / CONFIG).read_text(encoding="utf-8")
    assert dump_targets(parse_targets(text).values()) == text


@pytest.mark.parametrize(
    ("path", "module"),
    [
        ("src/ayeops/promql.py", "ayeops.promql"),
        ("src/ayeops/portal/disks.py", "ayeops.portal.disks"),
        (
            "src/ayeops/modules/hygiene/files/hygiene_runner.py",
            "ayeops.modules.hygiene.files.hygiene_runner",
        ),
    ],
)
def test_a_module_is_named_the_way_mutmut_names_its_mutants(path: str, module: str) -> None:
    assert module_name(path) == module


def promql(score: float | None = None) -> Target:
    return Target("src/ayeops/promql.py", ("tests/test_promql.py",), score)


def deploy(score: float | None = None) -> Target:
    return Target("src/ayeops/deploy.py", ("tests/test_deploy.py",), score)


def test_the_score_is_detected_over_checked_mutants_per_module() -> None:
    first, second = verdicts([promql(), deploy()], RESULTS)
    assert (first.mutants, first.score) == (4, 50.0)
    assert (second.mutants, second.measured) == (3, 66.6)
    assert (first.status, first.failed) == ("no baseline", False)


@pytest.mark.parametrize(
    ("baseline", "status", "failed"),
    [
        (50.0, "ok", False),
        (49.9, "above baseline", False),
        (50.1, "below baseline", True),
    ],
)
def test_a_module_fails_only_below_its_baseline(baseline: float, status: str, failed: bool) -> None:
    (verdict,) = verdicts([promql(baseline)], RESULTS)
    assert (verdict.status, verdict.failed) == (status, failed)


def test_a_module_with_a_baseline_that_did_not_run_fails() -> None:
    (verdict,) = verdicts([promql(80.0)], "    ayeops.promql.x_a__mutmut_1: not checked\n")
    assert (verdict.status, verdict.failed) == ("not run", True)
    (verdict,) = verdicts([promql()], "")
    assert (verdict.status, verdict.failed) == ("not run", False)


def test_a_score_is_floored_to_a_tenth_so_the_same_run_meets_its_own_baseline() -> None:
    assert floor_tenth(200 / 3) == 66.6
    assert floor_tenth(57.3) == 57.3
    assert floor_tenth(100.0) == 100.0


def test_writing_raises_baselines_and_never_lowers_them() -> None:
    targets = {t.path: t for t in (promql(40.0), deploy(90.0))}
    found = verdicts(targets.values(), RESULTS)
    assert raised(targets, found) == {
        "src/ayeops/promql.py": promql(50.0),
        "src/ayeops/deploy.py": deploy(90.0),
    }


def test_the_mutants_file_rejects_a_module_without_tests_or_a_bad_score() -> None:
    with pytest.raises(MutantsError, match="tests must be"):
        parse_targets('[modules."src/a.py"]\ntests = []\n')
    with pytest.raises(MutantsError, match="score must be"):
        parse_targets('[modules."src/a.py"]\ntests = ["tests/test_a.py"]\nscore = 101\n')
    with pytest.raises(MutantsError, match="lists no"):
        parse_targets("")


def write_config(path: Path, *targets: Target) -> Path:
    config = path / "mutants.toml"
    config.write_text(dump_targets(targets), encoding="utf-8")
    return config


def run_score(tmp_path: Path, config: Path, *extra: str) -> tuple[int, str]:
    results = tmp_path / "results.txt"
    results.write_text(RESULTS, encoding="utf-8")
    summary = tmp_path / "summary.md"
    code = main(
        ["--config", str(config), "score", str(results), *extra],
        {"GITHUB_STEP_SUMMARY": str(summary)},
    )
    return code, summary.read_text(encoding="utf-8") if summary.exists() else ""


def test_score_fails_the_job_below_the_baseline_and_says_what_to_do(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = write_config(tmp_path, promql(60.0), deploy())
    code, summary = run_score(tmp_path, config)
    assert code == 1
    assert "| `src/ayeops/promql.py` | 4 | 50.0 | 60.0 | below baseline |" in summary
    assert 'Raise the baseline: set score = 66.6 under [modules."src/ayeops/deploy.py"]' in summary
    assert "mutation score below the baseline: src/ayeops/promql.py" in capsys.readouterr().err


def test_score_gates_only_the_modules_asked_for(tmp_path: Path) -> None:
    config = write_config(tmp_path, promql(60.0), deploy(60.0))
    code, summary = run_score(tmp_path, config, "--modules", "src/ayeops/deploy.py")
    assert code == 0
    assert "promql" not in summary


def test_score_write_raises_the_baselines_in_the_file(tmp_path: Path) -> None:
    config = write_config(tmp_path, promql(55.0), deploy())
    code, _ = run_score(tmp_path, config, "--write")
    assert code == 1
    assert load_targets(config) == {
        "src/ayeops/deploy.py": deploy(66.6),
        "src/ayeops/promql.py": promql(55.0),
    }


def test_score_refuses_a_module_outside_the_mutants_file(tmp_path: Path) -> None:
    config = write_config(tmp_path, promql())
    code, _ = run_score(tmp_path, config, "--modules", "src/ayeops/nope.py")
    assert code == 2


def test_select_hands_the_matrix_a_json_list(tmp_path: Path) -> None:
    config = write_config(tmp_path, promql(), deploy())
    output = tmp_path / "output"
    assert main(["--config", str(config), "select"], {"GITHUB_OUTPUT": str(output)}) == 0
    line = output.read_text(encoding="utf-8")
    assert line.startswith("modules=") and line.endswith("\n")
    assert json.loads(line.removeprefix("modules=")) == [
        "src/ayeops/deploy.py",
        "src/ayeops/promql.py",
    ]


@pytest.mark.parametrize("after", ["", '\n[tool.other]\nkey = "value"\n'])
def test_narrowing_rewrites_only_the_mutmut_table(after: str) -> None:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8") + after
    target = TARGETS["src/ayeops/strict.py"]
    data = tomllib.loads(narrowed(text, target))
    expected = tomllib.loads(text)
    mutmut = expected["tool"]["mutmut"]
    mutmut["only_mutate"] = ["src/ayeops/strict.py"]
    mutmut["pytest_add_cli_args_test_selection"] = [
        "tests/test_injection.py",
        "tests/test_strict.py",
    ]
    assert data == expected


def uses(workflow: dict[Any, Any]) -> list[str]:
    return [
        str(step["uses"])
        for job in workflow["jobs"].values()
        for step in job["steps"]
        if "uses" in step
    ]


def steps(job: str) -> dict[str, dict[str, Any]]:
    found = WORKFLOW["jobs"][job]["steps"]
    return {str(step.get("name") or step.get("uses") or step.get("run")): step for step in found}


def test_a_pull_request_touching_a_module_starts_the_workflow_and_tests_alone_do_not() -> None:
    paths = WORKFLOW[True]["pull_request"]["paths"]
    assert paths == [*sorted(TARGETS), "src/ayeops/mutants.py", ".github/workflows/mutants.yml"]


def test_the_full_run_is_started_by_hand_only_and_can_skip_the_cache() -> None:
    assert set(WORKFLOW[True]) == {"pull_request", "workflow_dispatch"}
    fresh = WORKFLOW[True]["workflow_dispatch"]["inputs"]["fresh"]
    assert (fresh["type"], fresh["default"]) == ("boolean", False)


def test_every_action_of_the_workflow_is_pinned_to_a_commit() -> None:
    for action in uses(WORKFLOW):
        assert action == "./ci/github" or re.fullmatch(r"[\w./-]+@[0-9a-f]{40}", action), action


def test_the_workflow_keeps_one_mutmut_process_per_mutant_on_every_core() -> None:
    run = steps("mutants")["mutmut run"]["run"]
    assert 'uv run --no-sync mutmut run --max-children "$(nproc)" "${names[@]}"' in run
    assert 'mapfile -t names < "$RUNNER_TEMP/mutants-functions.txt"' in run
    report = steps("mutants")["report the mutation score"]
    assert report["continue-on-error"] is True
    assert report["with"]["mutants"] == "mutants/results.txt"


def test_a_pull_request_is_gated_on_its_changed_functions_and_a_full_run_on_the_baselines() -> None:
    found = steps("mutants")
    pull = "github.event_name == 'pull_request'"
    full = "github.event_name != 'pull_request'"
    assert found["functions the pull request changed"]["if"] == pull
    assert found["no surviving mutant in the changed functions"]["if"].startswith(pull)
    assert "ayeops.mutants gate" in found["no surviving mutant in the changed functions"]["run"]
    for name in (
        "score against the baseline",
        "report the mutation score",
        "cache keys of the module",
    ):
        assert found[name]["if"] == full, name
    assert "--modules" in found["score against the baseline"]["run"]
    assert "--write" in steps("baselines")["raise the baselines that improved"]["run"]


def test_the_cache_holds_only_the_verdicts_and_falls_back_from_the_source_to_the_tests() -> None:
    found = steps("mutants")
    restore = found["restore the verdicts of the last full run"]
    save = found["save the verdicts for the next full run"]
    assert restore["uses"].startswith("actions/cache/restore@")
    assert save["uses"].startswith("actions/cache/save@")
    assert restore["with"]["path"] == save["with"]["path"] == "mutants/**/*.meta"
    assert restore["with"]["key"] == save["with"]["key"]
    assert restore["with"]["restore-keys"].splitlines() == [
        "${{ steps.key.outputs.key }}-",
        "${{ steps.key.outputs.prefix }}-",
    ]
    assert "!inputs.fresh" in restore["if"]
    assert save["if"].startswith("always()")
    names = list(found)
    assert names.index("restore the verdicts of the last full run") < names.index("mutmut run")


def test_a_module_is_named_the_way_mutmut_names_its_methods() -> None:
    assert patterns("ayeops.promql", ["x_parse", "xǁLexerǁnext"]) == [
        "ayeops.promql.x_parse__mutmut_*",
        "ayeops.promql.xǁLexerǁnext__mutmut_*",
    ]
    (pattern,) = patterns("ayeops.promql", ["x_parse"])
    assert fnmatch.fnmatchcase("ayeops.promql.x_parse__mutmut_12", pattern)
    assert not fnmatch.fnmatchcase("ayeops.promql.x_parse_all__mutmut_1", pattern)


SOURCE = """\
import re

LIMIT = 3


def parse(text):
    words = text.split()
    return words


class Lexer:
    size = 2

    def next(self):
        return self.size + 1

    def limit(self):
        return LIMIT

    @property
    def width(self):
        return 1

    @staticmethod
    def make():
        return Lexer()

    class Token:
        def kind(self):
            return 0


@cache
def cached():
    return 1


async def fetch():
    return 2


def outer():
    def inner():
        return 2

    return inner()


def gone():
    return 0
"""


def unified(old: str, new: str) -> str:
    return "".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True), n=0))


def changed(new: str, old: str = SOURCE) -> list[str]:
    return changed_functions(old, new, unified(old, new))


def test_hunks_give_the_removed_and_the_added_lines() -> None:
    diff = "@@ -3 +3,2 @@ a\n-x\n+y\n+z\n@@ -10,2 +11,0 @@ b\n-p\n-q\n@@ -20,0 +21 @@\n+r\n"
    assert changed_lines(diff) == ({3, 10, 11}, {3, 4, 21})
    assert changed_lines("") == (set(), set())


@pytest.mark.parametrize(
    ("before", "after", "functions"),
    [
        ("    return words\n", "    return words[1:]\n", ["x_parse"]),
        ("        return 2\n\n    return", "        return 3\n\n    return", ["x_outer"]),
        ("return self.size + 1", "return self.size - 1", ["xǁLexerǁnext"]),
        ("    return 2\n\n\ndef outer", "    return 3\n\n\ndef outer", ["x_fetch"]),
        (
            "    def make():\n        return Lexer()",
            "    def make():\n        return None",
            ["xǁLexerǁmake"],
        ),
        ("@staticmethod\n    def make():", "@classmethod\n    def make(cls):", ["xǁLexerǁmake"]),
        ("            return 0\n", "            return 1\n", []),
        ("        return 1\n\n    @static", "        return 2\n\n    @static", []),
        ("@cache\n", "@lru_cache\n", []),
        ("    return 1\n\n\nasync", "    return 5\n\n\nasync", []),
    ],
    ids=[
        "body",
        "nested",
        "method",
        "async",
        "staticmethod",
        "decorator-of-a-staticmethod",
        "nested-class",
        "property",
        "decorator",
        "decorated",
    ],
)
def test_a_changed_line_maps_to_the_top_level_function_mutmut_mutates(
    before: str, after: str, functions: list[str]
) -> None:
    assert SOURCE.count(before) == 1
    assert changed(SOURCE.replace(before, after)) == functions


def test_a_module_level_change_maps_to_the_functions_that_use_its_names() -> None:
    assert changed(SOURCE.replace("LIMIT = 3", "LIMIT = 4")) == ["xǁLexerǁlimit"]
    assert changed(SOURCE.replace("import re\n", "import re\nimport json\n")) == []
    assert changed(SOURCE.replace("import re\n", "import re as words\n")) == ["x_parse"]


def test_a_class_attribute_change_maps_to_the_methods_of_its_class_that_use_it() -> None:
    assert changed(SOURCE.replace("size = 2", "size = 3")) == ["xǁLexerǁnext"]


def test_removed_lines_map_through_the_old_source() -> None:
    assert changed(
        SOURCE.replace("    words = text.split()\n    return words\n", "    return text\n")
    ) == ["x_parse"]
    assert changed(SOURCE.replace("\n    return words\n", "\n")) == ["x_parse"]
    assert changed(SOURCE.replace("\n\ndef gone():\n    return 0\n", "")) == []
    assert changed(SOURCE.replace("LIMIT = 3\n\n\n", "")) == ["xǁLexerǁlimit"]


def test_a_renamed_function_runs_under_its_new_name_only() -> None:
    assert changed(SOURCE.replace("def gone():", "def kept():")) == ["x_kept"]


def test_a_new_file_runs_every_function_mutmut_mutates() -> None:
    expected = sorted(mutated_functions(SOURCE))
    assert changed_functions(None, SOURCE, unified("", SOURCE)) == expected
    assert expected == [
        "x_fetch",
        "x_gone",
        "x_outer",
        "x_parse",
        "xǁLexerǁlimit",
        "xǁLexerǁmake",
        "xǁLexerǁnext",
    ]


def test_an_old_source_that_does_not_parse_only_loses_the_removed_lines() -> None:
    new = SOURCE.replace("    return words\n", "    return words[1:]\n")
    assert changed_functions("def (:\n", new, unified(SOURCE, new)) == ["x_parse"]


@pytest.mark.parametrize("path", TARGETS)
def test_mutmut_mutates_no_function_the_mapping_skips(path: str) -> None:
    pytest.importorskip("mutmut.mutation.file_mutation")
    source = (ROOT / path).read_text(encoding="utf-8")
    counts = mutmut_counts(path, source)
    assert counts is not None
    assert set(counts) <= mutated_functions(source)


def fake_git(names: str | None, diffs: dict[str, str], old: dict[str, str]) -> Any:
    def git(*args: str) -> str | None:
        if args[0] == "diff":
            assert args[args.index("--no-renames") + 1] == "origin/main...HEAD"
            return names if "--name-only" in args else diffs.get(args[-1], "")
        if args[0] == "merge-base":
            assert args[1:] == ("origin/main", "HEAD")
            return "fork\n"
        assert args[0] == "show"
        revision, _, path = args[1].partition(":")
        assert revision == "fork"
        return old.get(path)

    return git


def paths(found: list[Target]) -> list[str]:
    return [target.path for target in found]


def promql_change(tmp_path: Path, new: str, names: str = "src/ayeops/promql.py\n") -> Any:
    module = tmp_path / "src" / "ayeops" / "promql.py"
    module.parent.mkdir(parents=True, exist_ok=True)
    module.write_text(new, encoding="utf-8")
    diffs = {"src/ayeops/promql.py": unified(SOURCE, new)}
    return fake_git(names, diffs, {"src/ayeops/promql.py": SOURCE})


def test_without_a_base_every_module_is_selected() -> None:
    assert paths(select(TARGETS, "", fake_git(None, {}, {}))) == list(TARGETS)


def test_a_module_whose_mutated_functions_changed_is_selected(tmp_path: Path) -> None:
    git = promql_change(
        tmp_path, SOURCE.replace("return words", "return []"), "src/ayeops/promql.py\nREADME.md\n"
    )
    assert paths(select(TARGETS, "origin/main", git, tmp_path)) == ["src/ayeops/promql.py"]
    assert diff_functions("src/ayeops/promql.py", "origin/main", git, tmp_path) == ["x_parse"]


def test_a_module_whose_change_mutmut_never_mutates_is_not_selected(tmp_path: Path) -> None:
    git = promql_change(tmp_path, SOURCE.replace("@cache", "@lru_cache"))
    assert select(TARGETS, "origin/main", git, tmp_path) == []


@pytest.mark.parametrize(
    "names",
    [
        "tests/test_promql.py\n",
        "tests/conftest.py\n",
        "pyproject.toml\ntests/mutants.toml\n",
        ".github/workflows/mutants.yml\nsrc/ayeops/mutants.py\n",
    ],
)
def test_tests_and_the_mutation_setup_alone_select_nothing(names: str) -> None:
    assert select(TARGETS, "origin/main", fake_git(names, {}, {})) == []


def test_a_failed_diff_is_an_error_not_an_empty_selection(tmp_path: Path) -> None:
    with pytest.raises(MutantsError, match="fetch the base branch"):
        select(TARGETS, "origin/main", fake_git(None, {}, {}))

    def no_fork(*args: str) -> str | None:
        return None if args[0] == "merge-base" else "src/ayeops/promql.py\n"

    with pytest.raises(MutantsError, match="fetch the base branch"):
        diff_functions("src/ayeops/promql.py", "origin/main", no_fork, tmp_path)


def git_repo(tmp_path: Path, *commands: list[str]) -> None:
    for command in commands:
        subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *command],
            cwd=tmp_path,
            check=True,
            capture_output=True,
        )


@pytest.mark.skipif(shutil.which("git") is None, reason="no git")
@pytest.mark.parametrize(
    ("counts", "patterns_written", "table"),
    [
        (
            {"x_parse": 3, "xǁLexerǁnext": 2},
            ["ayeops.promql.x_parse__mutmut_*", "ayeops.promql.xǁLexerǁnext__mutmut_*"],
            "| `ayeops.promql.x_parse` | 3 |",
        ),
        ({"x_parse": 3}, ["ayeops.promql.x_parse__mutmut_*"], "| `ayeops.promql.x_parse` | 3 |"),
        (
            None,
            ["ayeops.promql.x_parse__mutmut_*", "ayeops.promql.xǁLexerǁnext__mutmut_*"],
            "| `ayeops.promql.x_parse` | ? |",
        ),
        ({}, [], "No function of ayeops.promql that mutmut mutates changed."),
    ],
    ids=["mutmut", "no-mutants-in-one", "without-mutmut", "none"],
)
def test_functions_writes_the_patterns_of_the_changed_functions_mutmut_mutates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    counts: dict[str, int] | None,
    patterns_written: list[str],
    table: str,
) -> None:
    module = tmp_path / "src" / "ayeops" / "promql.py"
    module.parent.mkdir(parents=True)
    module.write_text(SOURCE, encoding="utf-8")
    config = write_config(tmp_path, promql())
    git_repo(tmp_path, ["init", "-q", "-b", "main"], ["add", "."], ["commit", "-q", "-m", "a"])
    new = SOURCE.replace("return words", "return []").replace("self.size + 1", "self.size")
    git_repo(tmp_path, ["switch", "-q", "-c", "topic"])
    module.write_text(new, encoding="utf-8")
    git_repo(tmp_path, ["commit", "-q", "-am", "b"])
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(ayeops.mutants, "mutmut_counts", lambda path, source: counts)
    output, summary, written = tmp_path / "output", tmp_path / "summary", tmp_path / "names"
    env = {"GITHUB_OUTPUT": str(output), "GITHUB_STEP_SUMMARY": str(summary)}
    args = ["--config", str(config), "functions", "src/ayeops/promql.py"]
    assert main([*args, "--base", "main", "--output", str(written)], env) == 0
    assert written.read_text(encoding="utf-8").splitlines() == patterns_written
    assert output.read_text(encoding="utf-8") == f"count={len(patterns_written)}\n"
    assert table in summary.read_text(encoding="utf-8")


GATED = """\
    ayeops.promql.x_parse__mutmut_1: killed
    ayeops.promql.x_parse__mutmut_2: timeout
    ayeops.promql.x_parse__mutmut_3: caught by type check
    ayeops.promql.x_parse__mutmut_4: suspicious
    ayeops.promql.xǁLexerǁnext__mutmut_1: killed
    ayeops.promql.xǁLexerǁnext__mutmut_2: survived
    ayeops.promql.x_lex__mutmut_1: no tests
    ayeops.promql.x_idle__mutmut_1: not checked
    ayeops.promql.x_other__mutmut_1: survived
"""


def test_the_gate_counts_only_the_mutants_of_the_changed_functions() -> None:
    wanted = patterns("ayeops.promql", ["x_parse", "xǁLexerǁnext", "x_lex", "x_idle"])
    parse, method, lex, idle = checked(GATED, wanted)
    assert (parse.mutants, parse.missed, parse.status) == (3, (), "ok")
    assert (method.mutants, method.missed, method.status) == (
        2,
        ("ayeops.promql.xǁLexerǁnext__mutmut_2",),
        "survivors",
    )
    assert (lex.missed, lex.status) == (("ayeops.promql.x_lex__mutmut_1",), "survivors")
    assert (idle.mutants, idle.status) == (0, "not run")


def run_gate(tmp_path: Path, names: list[str]) -> tuple[int, str]:
    results, functions = tmp_path / "results.txt", tmp_path / "functions.txt"
    results.write_text(GATED, encoding="utf-8")
    functions.write_text("".join(f"{line}\n" for line in names), encoding="utf-8")
    summary = tmp_path / "summary.md"
    code = main(
        ["gate", str(results), "--functions", str(functions)],
        {"GITHUB_STEP_SUMMARY": str(summary)},
    )
    return code, summary.read_text(encoding="utf-8") if summary.exists() else ""


def test_the_gate_passes_when_every_mutant_of_the_changed_functions_is_detected(
    tmp_path: Path,
) -> None:
    code, summary = run_gate(tmp_path, patterns("ayeops.promql", ["x_parse"]))
    assert code == 0
    assert "| `ayeops.promql.x_parse` | 3 | 3 | 0 | ok |" in summary


def test_the_gate_fails_on_a_survivor_and_names_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, summary = run_gate(tmp_path, patterns("ayeops.promql", ["x_parse", "xǁLexerǁnext"]))
    assert code == 1
    key = "ayeops.promql.xǁLexerǁnext__mutmut_2"
    assert f"survived: {key} (mutmut show {key})" in summary
    assert "surviving or untested mutants in: ayeops.promql.xǁLexerǁnext" in capsys.readouterr().err


def test_the_gate_fails_on_a_function_whose_mutants_did_not_run(tmp_path: Path) -> None:
    code, summary = run_gate(tmp_path, patterns("ayeops.promql", ["x_idle"]))
    assert code == 1
    assert "| not run |" in summary


def test_the_gate_passes_when_no_function_changed(tmp_path: Path) -> None:
    assert run_gate(tmp_path, []) == (0, "")


def test_the_gate_refuses_a_missing_file(tmp_path: Path) -> None:
    code = main(["gate", str(tmp_path / "nope.txt"), "--functions", str(tmp_path / "nope")], {})
    assert code == 2


def cache_root(tmp_path: Path) -> Path:
    for name in ("pyproject.toml", "uv.lock"):
        shutil.copy(ROOT / name, tmp_path / name)
    for name, text in {
        "src/ayeops/promql.py": SOURCE,
        "tests/test_promql.py": "def test_a() -> None:\n    pass\n",
        "tests/conftest.py": "",
        "tests/fixtures/a/b.txt": "b\n",
    }.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(text, encoding="utf-8")
    return tmp_path


def edit(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert old in text
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


def test_the_cache_key_follows_the_source_and_its_prefix_the_tests_and_settings(
    tmp_path: Path,
) -> None:
    root = cache_root(tmp_path)
    prefix, key = cache_keys(promql(), root)
    assert re.fullmatch(r"mutants-1-ayeops\.promql-[0-9a-f]{16}", prefix)
    assert re.fullmatch(re.escape(prefix) + r"-[0-9a-f]{16}", key)
    edit(root / "src/ayeops/promql.py", "return words", "return []")
    moved, other = cache_keys(promql(), root)
    assert moved == prefix and other != key
    for path, old, new in [
        ("tests/test_promql.py", "pass", "assert True"),
        ("tests/conftest.py", "", "X = 1\n"),
        ("tests/fixtures/a/b.txt", "b", "c"),
        ("pyproject.toml", "mutmut==", "mutmut>="),
        ("pyproject.toml", 'source_paths = ["src/"]', 'source_paths = ["src/", "ci/"]'),
        ("uv.lock", 'name = "pyyaml"\nversion = "', 'name = "pyyaml"\nversion = "0'),
    ]:
        edit(root / path, old, new)
        changed_prefix, _ = cache_keys(promql(), root)
        assert changed_prefix != prefix, (path, new)
        prefix = changed_prefix


def test_a_release_version_bump_keeps_the_cache_key(tmp_path: Path) -> None:
    root = cache_root(tmp_path)
    keys = cache_keys(promql(), root)
    version = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"][
        "version"
    ]
    edit(root / "pyproject.toml", f'version = "{version}"', 'version = "9.9.9"')
    edit(
        root / "uv.lock",
        f'name = "ayeops"\nversion = "{version}"',
        'name = "ayeops"\nversion = "9.9.9"',
    )
    edit(
        root / "pyproject.toml",
        "pytest_add_cli_args_test_selection = [",
        'pytest_add_cli_args_test_selection = [\n    "tests/test_other.py",',
    )
    assert cache_keys(promql(), root) == keys


def test_cache_key_hands_both_keys_to_the_workflow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = cache_root(tmp_path)
    config = write_config(root, promql())
    monkeypatch.chdir(root)
    output = root / "output"
    assert (
        main(
            ["--config", str(config), "cache-key", "src/ayeops/promql.py"],
            {"GITHUB_OUTPUT": str(output)},
        )
        == 0
    )
    prefix, key = cache_keys(promql(), root)
    assert output.read_text(encoding="utf-8") == f"prefix={prefix}\nkey={key}\n"
