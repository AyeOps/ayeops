from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

from tests.conftest import EXAMPLE, ROOT

PAGES = ROOT / "docs" / "runbooks"
CATALOG_FROM = """
import json
import sys
from pathlib import Path

sys.path.insert(0, sys.argv[1])

import ayeops
from ayeops.manifest import load_manifest
from ayeops.mcp_catalog import builtin_runbooks, catalog_json
from ayeops.registry import load_modules

catalog = json.loads(catalog_json(load_manifest(Path(sys.argv[2])), load_modules()))
found = {"package": ayeops.__file__, "runbooks": str(builtin_runbooks())}
print(json.dumps({**found, "catalog": catalog}))
"""


@pytest.fixture(scope="module")
def dist(tmp_path_factory: pytest.TempPathFactory) -> Path:
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv is not on PATH")
    out = tmp_path_factory.mktemp("dist")
    subprocess.run(
        [uv, "build", "--no-sources", "--quiet", "--out-dir", str(out), str(ROOT)],
        check=True,
        timeout=300,
    )
    return out


def built(dist: Path, pattern: str) -> Path:
    [found] = dist.glob(pattern)
    return found


def pages() -> dict[str, bytes]:
    return {page.name: page.read_bytes() for page in sorted(PAGES.glob("*.md"))}


def test_the_wheel_carries_every_runbook_page_inside_the_package(dist: Path) -> None:
    with zipfile.ZipFile(built(dist, "*.whl")) as wheel:
        names = wheel.namelist()
        packaged = {
            name.removeprefix("ayeops/runbooks/"): wheel.read(name)
            for name in names
            if name.startswith("ayeops/runbooks/")
        }
    assert packaged == pages()
    assert len(packaged) > 50
    assert not [name for name in names if name.startswith("docs/")]


def test_the_sdist_carries_the_pages_the_wheel_is_built_from(dist: Path) -> None:
    with tarfile.open(built(dist, "*.tar.gz")) as sdist:
        names = {name.split("/", 1)[1] for name in sdist.getnames() if "/" in name}
    assert {f"docs/runbooks/{name}" for name in pages()} <= names
    assert not [name for name in names if name.startswith(("tests/", "examples/", "cloud/"))]


def test_the_catalog_of_an_installed_wheel_reads_runbooks_through_the_package(
    dist: Path, tmp_path: Path
) -> None:
    site = tmp_path / "site"
    with zipfile.ZipFile(built(dist, "*.whl")) as wheel:
        wheel.extractall(site)
    result = subprocess.run(
        [sys.executable, "-c", CATALOG_FROM, str(site), str(EXAMPLE)],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        timeout=300,
        check=True,
    )
    found = json.loads(result.stdout)
    assert Path(found["package"]).is_relative_to(site)
    assert Path(found["runbooks"]) == site / "ayeops" / "runbooks"
    assert not (tmp_path / "docs").exists()
    catalog = found["catalog"]
    module_pages = {
        name: page["text"]
        for name, page in catalog["runbooks"].items()
        if page["url"].startswith("https://github.com/AyeOps/ayeops/blob/main/docs/runbooks/")
    }
    assert len(module_pages) > 50
    for name, text in module_pages.items():
        assert text == (PAGES / f"{name}.md").read_text(encoding="utf-8")[:20000]
    committed = ROOT / "examples" / "rendered" / "central" / "mcp-catalog" / "catalog.json"
    assert catalog["runbooks"] == json.loads(committed.read_text(encoding="utf-8"))["runbooks"]
