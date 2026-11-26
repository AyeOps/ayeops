from __future__ import annotations

import re
import shlex
from pathlib import Path
from typing import Any

import pytest
import yaml

from ayeops.cli import main
from ayeops.manifest import Manifest, load_manifest
from ayeops.registry import Module
from ayeops.render import ENV_REFERENCE, render_tree
from ayeops.rules import check_manifest
from ayeops.secrets import parse_env
from tests.conftest import EXAMPLE_STAMP, ROOT

QUICKSTART = ROOT / "examples" / "quickstart.yaml"
README = ROOT / "README.md"
CI = ROOT / ".github" / "workflows" / "ci.yml"
HOST = "local"
CREDENTIALED = {"ingress", "auth", "secrets", "oncall", "autopilot", "status", "cloudflare"}


def quick_start() -> str:
    text = README.read_text(encoding="utf-8")
    start = text.index("## Quick start\n")
    return text[start : text.index("\n## ", start + 1)]


def commands() -> list[str]:
    blocks = re.findall(r"```sh\n(.*?)```", quick_start(), re.S)
    return [line.strip() for block in blocks for line in block.splitlines() if line.strip()]


ENV_COMMAND = "uv run aye secrets gen "


def env_command() -> list[str]:
    found = [c for c in commands() if c.startswith(ENV_COMMAND)]
    assert len(found) == 1
    return shlex.split(found[0])


def run_step(argv: list[str]) -> int:
    assert argv[:3] == ["uv", "run", "aye"]
    rest = [str(QUICKSTART) if arg == "examples/quickstart.yaml" else arg for arg in argv[3:]]
    return main(rest)


def documented_env() -> set[str]:
    return set(re.findall(r"\b(AYE_[A-Z0-9_]+)=", quick_start()))


def tree(manifest: Manifest, modules: dict[str, Module]) -> dict[str, str]:
    return render_tree(manifest, modules, EXAMPLE_STAMP)


def compose(modules: dict[str, Module]) -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load(
        tree(load_manifest(QUICKSTART), modules)[f"{HOST}/compose.yaml"]
    )
    return data


def as_arch(arch: str) -> Manifest:
    data = yaml.safe_load(QUICKSTART.read_text(encoding="utf-8"))
    data["hosts"][HOST]["arch"] = arch
    return Manifest(data, QUICKSTART.parent)


def test_the_quickstart_passes_the_check(modules: dict[str, Module]) -> None:
    assert check_manifest(load_manifest(QUICKSTART), modules) == []


def test_the_quickstart_render_is_deterministic(modules: dict[str, Module]) -> None:
    first = tree(load_manifest(QUICKSTART), modules)
    assert first == tree(load_manifest(QUICKSTART), modules)
    assert f"{HOST}/compose.yaml" in first
    assert {path.split("/")[0] for path in first} == {HOST}


def test_the_quickstart_is_one_host_without_credentials_ingress_or_public_bind() -> None:
    data = yaml.safe_load(QUICKSTART.read_text(encoding="utf-8"))
    assert list(data["hosts"]) == [HOST]
    host = data["hosts"][HOST]
    assert host["arch"] == "amd64"
    assert "public_bind" not in host
    assert set(host["modules"]) == {"metrics", "node", "containers", "probes", "grafana"}
    assert not CREDENTIALED & set(host["modules"])
    assert all("expose" not in (s or {}) for s in host["modules"].values())
    assert data["alerting"]["contact"] == "none"
    assert host["modules"]["probes"]["http"]


def test_every_quickstart_port_binds_to_localhost(modules: dict[str, Module]) -> None:
    ports = [p for s in compose(modules)["services"].values() for p in s.get("ports", [])]
    assert ports
    assert all(p.startswith("127.0.0.1:") for p in ports)


@pytest.mark.parametrize("arch", ["amd64", "arm64"])
def test_every_quickstart_service_runs_on_the_chosen_arch_only(
    arch: str, modules: dict[str, Module]
) -> None:
    text = tree(as_arch(arch), modules)[f"{HOST}/compose.yaml"]
    platforms = set(re.findall(r"platform:\s*(\S+)", text))
    assert platforms == {f"linux/{arch}"}
    services = yaml.safe_load(text)["services"]
    assert all(s["platform"] == f"linux/{arch}" for s in services.values())


def test_the_documented_env_provides_every_variable_of_the_rendered_compose(
    modules: dict[str, Module],
) -> None:
    rendered = tree(load_manifest(QUICKSTART), modules)
    wanted = set(ENV_REFERENCE.findall(rendered[f"{HOST}/compose.yaml"]))
    assert wanted == {"AYE_GRAFANA_ADMIN_USER", "AYE_GRAFANA_ADMIN_PASSWORD"}
    assert documented_env() == wanted
    assert set(parse_env(rendered[f"{HOST}/.env.example"])) == wanted


def test_the_documented_env_command_writes_every_variable_the_compose_needs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    render = [c for c in commands() if c.startswith("uv run aye render ")]
    assert len(render) == 1
    assert run_step(shlex.split(render[0])) == 0
    argv = env_command()
    assert "python" not in argv
    capsys.readouterr()
    assert run_step(argv) == 0
    printed = capsys.readouterr().out
    rendered = tmp_path / "rendered" / HOST
    raw = (rendered / ".env").read_bytes()
    assert b"\r" not in raw
    values = parse_env(raw.decode("utf-8"))
    wanted = set(ENV_REFERENCE.findall((rendered / "compose.yaml").read_text(encoding="utf-8")))
    assert set(values) == wanted == documented_env()
    assert set(parse_env((rendered / ".env.example").read_text(encoding="utf-8"))) == wanted
    assert all(values.values())
    assert re.fullmatch(r"[0-9a-f]{16}", values["AYE_GRAFANA_ADMIN_USER"])
    assert re.fullmatch(r"[0-9a-f]{48}", values["AYE_GRAFANA_ADMIN_PASSWORD"])
    assert "you must set it" not in printed
    assert "rendered/local/.env: generated 2, kept 0 existing value(s)" in printed
    assert not any(value in printed for value in values.values())
    assert run_step(argv) == 0
    assert (rendered / ".env").read_bytes() == raw


def test_the_quick_start_runs_the_rendered_paths(modules: dict[str, Module]) -> None:
    steps = commands()
    assert "uv run aye check examples/quickstart.yaml" in steps
    assert "uv run aye render examples/quickstart.yaml -o rendered" in steps
    assert "docker compose -f rendered/local/compose.yaml up -d" in steps
    assert "docker compose -f rendered/local/compose.yaml down --volumes" in steps
    assert env_command()[-2:] == ["--env-file", "rendered/local/.env"]
    assert env_command()[5:9] == ["--manifest", "examples/quickstart.yaml", "--host", HOST]
    assert "127.0.0.1:3000:3000" in compose(modules)["services"]["grafana"]["ports"]


def test_ci_composes_the_rendered_quickstart() -> None:
    steps = yaml.safe_load(CI.read_text(encoding="utf-8"))["jobs"]["compose"]["steps"]
    runs = "\n".join(str(step.get("run", "")) for step in steps)
    assert "aye render examples/quickstart.yaml" in runs
    assert "config -q" in runs.split("examples/quickstart.yaml", 1)[1]
    readme = " ".join(env_command()).removeprefix("uv run ")
    assert f"uv run --locked {readme}" in runs
    assert "python -c" not in runs
