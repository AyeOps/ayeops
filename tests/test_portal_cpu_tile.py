from __future__ import annotations

import json
import re
import shutil
import subprocess
from typing import cast

import pytest

from ayeops.portal.i18n import STRINGS
from ayeops.portal.pages import asset
from tests.conftest import WAIT

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")


def view(used: float, limit: float | None, lang: str = "en") -> dict[str, str]:
    source = asset("live.js")
    helpers = re.search(r"  const pct = .*?\n", source)
    cores = re.search(r"  const cores = .*?\n", source)
    block = re.search(r"  const cpuView = .*?\n  \};\n", source, re.S)
    assert helpers and cores and block
    texts = {key: value[lang] for key, value in STRINGS.items() if key.startswith("js.of_")}
    script = (
        f"const texts = {json.dumps(texts)};\n"
        "const T = (key, vars) => texts[key].replace(/\\{(\\w+)\\}/g, (m, k) => vars[k]);\n"
        f"{helpers.group(0)}{cores.group(0)}{block.group(0)}\n"
        f"console.log(JSON.stringify(cpuView({used}, {json.dumps(limit)})));\n"
    )
    done = subprocess.run(
        ["node", "-e", script], capture_output=True, encoding="utf-8", timeout=WAIT, check=True
    )
    return cast(dict[str, str], json.loads(done.stdout))


def test_a_container_at_its_limit_shows_100_percent() -> None:
    assert view(0.1, 0.1)["text"] == "100 % · 0.1 of 0.1 cpu"


def test_a_container_at_half_its_limit_shows_50_percent() -> None:
    assert view(0.25, 0.5)["text"] == "50 % · 0.25 of 0.5 cpu"


def test_the_issue_example_shows_the_share_of_the_limit_and_the_absolute_cores() -> None:
    assert view(0.088, 0.1)["text"] == "88 % · 0.088 of 0.1 cpu"


def test_the_share_is_in_ukrainian_too() -> None:
    assert view(0.088, 0.1, "uk")["text"] == "88 % · 0.088 з 0.1 cpu"


def test_a_container_near_its_limit_is_coloured_and_one_below_is_not() -> None:
    assert view(0.08, 0.1)["cls"] == "warn"
    assert view(0.079, 0.1)["cls"] == ""
    assert view(0.1, 0.1)["cls"] == "warn"


def test_without_a_limit_the_share_is_of_one_core() -> None:
    assert view(0.25, None) == {"text": "25 % of a core", "cls": ""}
