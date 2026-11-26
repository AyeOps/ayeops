from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).parents[1]
GITHUB = ROOT / ".github"
FORMS = GITHUB / "ISSUE_TEMPLATE"
REPOSITORY = "https://github.com/AyeOps/ayeops"
COMMUNITY_FILES = [
    "LICENSE",
    "NOTICE",
    "README.md",
    "SECURITY.md",
    "CONTRIBUTING.md",
    "CODE_OF_CONDUCT.md",
    "CHANGELOG.md",
    ".github/CODEOWNERS",
    ".github/pull_request_template.md",
    ".github/ISSUE_TEMPLATE/config.yml",
    ".github/ISSUE_TEMPLATE/bug_report.yml",
    ".github/ISSUE_TEMPLATE/module_request.yml",
    ".github/ISSUE_TEMPLATE/docs_problem.yml",
]


def text(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def form(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


@pytest.mark.parametrize("name", COMMUNITY_FILES)
def test_the_community_file_exists_and_is_not_empty(name: str) -> None:
    assert text(name).strip()


@pytest.mark.parametrize("target", ["CONTRIBUTING.md", "SECURITY.md"])
def test_the_readme_links_to_contributing_and_security(target: str) -> None:
    assert f"]({target})" in text("README.md")


def test_the_development_commands_live_in_contributing_not_in_the_readme() -> None:
    assert "## Development" not in text("README.md")
    assert "mutmut run" not in text("README.md")
    contributing = text("CONTRIBUTING.md")
    for command in [
        "uv run ruff check . && uv run ruff format --check .",
        "uv run mypy",
        "uv run python -m ayeops.comments .",
        "uv run python -m ayeops.texthygiene .",
        "uv run aye check examples/acme.yaml",
        "uv run --group mutants mutmut run",
        "rm -rf examples/rendered",
    ]:
        assert command in contributing


def test_security_reports_go_to_github_private_reporting() -> None:
    security = text("SECURITY.md")
    assert f"{REPOSITORY}/security/advisories/new" in security
    assert "Report a vulnerability" in security
    assert "0.4.x" in security


def test_the_code_of_conduct_is_the_contributor_covenant_without_a_private_email() -> None:
    conduct = text("CODE_OF_CONDUCT.md")
    assert "Contributor Covenant" in conduct
    assert "version 2.1" in conduct
    assert "[INSERT CONTACT METHOD]" not in conduct
    assert not re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", conduct)


def test_codeowners_names_the_maintainer_by_login() -> None:
    lines = [line.split() for line in text(".github/CODEOWNERS").splitlines() if line.strip()]
    assert ["*", "@mykhailoklimnyk"] in lines
    assert all(owner.startswith("@") and "." not in owner for line in lines for owner in line[1:])


@pytest.mark.parametrize("path", sorted(FORMS.glob("*.yml")), ids=lambda path: path.name)
def test_issue_templates_are_yaml_forms(path: Path) -> None:
    data = form(path)
    if path.name == "config.yml":
        return
    assert data["name"] and data["description"]
    assert data["body"]
    ids = [item["id"] for item in data["body"] if "id" in item]
    assert len(ids) == len(set(ids))
    assert any(item.get("validations", {}).get("required") for item in data["body"])


def test_blank_issues_are_off_and_security_reports_point_to_the_policy() -> None:
    config = form(FORMS / "config.yml")
    assert config["blank_issues_enabled"] is False
    urls = [link["url"] for link in config["contact_links"]]
    assert f"{REPOSITORY}/security/policy" in urls


def test_the_changelog_keeps_an_unreleased_section_with_stable_and_preview_modules() -> None:
    changelog = text("CHANGELOG.md")
    assert "## [Unreleased]" in changelog
    assert "Keep a Changelog" in changelog
    assert "`autopilot` (preview)" in changelog
    assert "shadow mode" in changelog


def test_the_changelog_names_every_built_in_module() -> None:
    changelog = text("CHANGELOG.md")
    modules = sorted(path.name for path in (ROOT / "src" / "ayeops" / "modules").iterdir())
    assert [name for name in modules if f"`{name}`" not in changelog] == []


def test_notice_credits_every_vendored_fixture_that_exists() -> None:
    notice = text("NOTICE")
    vendored = re.findall(r"tests/fixtures/[\w./-]+\.json", notice)
    assert vendored
    assert [name for name in vendored if not (ROOT / name).is_file()] == []
    for name in [
        "tests/fixtures/agentgateway/config.schema.json",
        "tests/fixtures/vulns/trivy-alpine-39.json",
    ]:
        assert name in notice
    assert notice.count("Apache License 2.0") >= 2


def test_requires_python_matches_the_version_ci_tests() -> None:
    project = tomllib.loads(text("pyproject.toml"))["project"]
    tested = text(".python-version").strip()
    assert project["requires-python"] == f">={tested}"
