from __future__ import annotations

import tomllib
from importlib.metadata import version
from pathlib import Path
from typing import Any

import pytest
import yaml

from ayeops import __version__
from ayeops.cli import main
from ayeops.manifest import schema_errors, version_problems
from ayeops.registry import Module
from ayeops.rules import check_manifest
from tests.conftest import ROOT, example_data, manifest_from


def pinned(spec: Any) -> dict[str, Any]:
    data = example_data()
    data["ayeops"] = spec
    return data


def written(tmp_path: Path, data: dict[str, Any]) -> Path:
    path = tmp_path / "pinned.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def test_the_running_version_is_the_installed_package_version_from_pyproject() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert __version__ == version("ayeops") == project["version"]


def test_the_example_pins_the_running_release_line() -> None:
    assert version_problems(example_data()) == []
    assert example_data()["ayeops"].startswith(">=")


@pytest.mark.parametrize(
    ("spec", "running"),
    [
        (">=0.2,<0.3", "0.2.0"),
        (">=0.2,<0.3", "0.2.7"),
        ("==0.2.0", "0.2.0"),
        ("~=0.2.1", "0.2.4"),
        (">=0.2", "1.0.0"),
        (">=0.2,<0.3", "0.2.1rc1"),
    ],
)
def test_a_running_version_inside_the_range_passes(spec: str, running: str) -> None:
    assert version_problems({"ayeops": spec}, running) == []


@pytest.mark.parametrize(
    ("spec", "running"),
    [
        (">=0.2,<0.3", "0.1.0"),
        (">=0.2,<0.3", "0.3.0"),
        ("==0.2.0", "0.2.1"),
        ("<0.2", "0.2.0"),
    ],
)
def test_a_running_version_outside_the_range_names_both(spec: str, running: str) -> None:
    [(path, rule, message)] = version_problems({"ayeops": spec}, running)
    assert (path, rule) == ("ayeops", "ayeops-version")
    assert spec in message and f"AyeOps {running} is running" in message


@pytest.mark.parametrize("spec", ["0.2.0", "latest", "=>0.2", "v0.2", ">=0.2;<0.3"])
def test_a_malformed_specifier_is_a_finding_with_an_example(spec: str) -> None:
    [(path, rule, message)] = version_problems({"ayeops": spec}, "0.2.0")
    assert (path, rule) == ("ayeops", "ayeops-version")
    assert repr(spec) in message and "'>=0.2,<0.3'" in message


def test_without_the_key_any_version_passes(modules: dict[str, Module]) -> None:
    data = example_data()
    del data["ayeops"]
    assert version_problems(data, "0.0.1") == []
    assert check_manifest(manifest_from(data), modules) == []


@pytest.mark.parametrize("spec", [0.2, "", ["0.2"]])
def test_a_pin_that_is_not_a_non_empty_string_is_a_schema_error(spec: Any) -> None:
    assert version_problems({"ayeops": spec}, "0.2.0") == []
    assert [path for path, _ in schema_errors(pinned(spec))] == ["ayeops"]


def test_an_unmet_pin_is_the_only_finding_of_the_check(modules: dict[str, Module]) -> None:
    data = pinned(f">{__version__}")
    data["unknown"] = True
    del data["services"]["web"]["alerts"]
    findings = check_manifest(manifest_from(data), modules)
    assert [(f.path, f.rule) for f in findings] == [("ayeops", "ayeops-version")]


def test_check_fails_on_an_unmet_pin_and_names_both_versions(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = written(tmp_path, pinned(">=99.0"))
    assert main(["check", str(path)]) == 1
    expected = f"the manifest needs AyeOps >=99.0, but AyeOps {__version__} is running"
    assert f"error [ayeops-version] ayeops: {expected}" in capsys.readouterr().err


def test_an_exact_pin_of_the_running_version_passes_the_check(
    modules: dict[str, Module],
) -> None:
    assert check_manifest(manifest_from(pinned(f"=={__version__}")), modules) == []


@pytest.mark.parametrize("command", ["render", "portal"])
def test_render_and_portal_refuse_an_unmet_pin(
    tmp_path: Path, command: str, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "out"
    assert main([command, str(written(tmp_path, pinned("<0.0.1"))), "-o", str(out)]) == 1
    assert not out.exists()
    assert "[ayeops-version]" in capsys.readouterr().err
