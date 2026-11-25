from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from ayeops.cli import main
from ayeops.render import dump_yaml
from ayeops.retire import RULES, provisioned, read_script, retire_rules


def rules(*uids: str, org: int = 1, deleted: tuple[str, ...] = ()) -> str:
    data: dict[str, object] = {
        "apiVersion": 1,
        "groups": [
            {
                "orgId": org,
                "name": "g",
                "folder": "AyeOps",
                "interval": "1m",
                "rules": [{"uid": uid, "title": uid} for uid in uids],
            }
        ],
    }
    if deleted:
        data["deleteRules"] = [{"orgId": 1, "uid": uid} for uid in deleted]
    return dump_yaml(data)


def deletions(text: str) -> list[dict[str, object]]:
    return list(yaml.safe_load(text).get("deleteRules") or [])


def test_a_rule_that_left_the_render_is_deleted_and_the_rest_ship_unchanged() -> None:
    rendered = rules("aye-a")
    text, retired = retire_rules(rules("aye-a", "aye-b", "aye-c"), rendered)
    assert retired == ["aye-b", "aye-c"]
    assert deletions(text) == [{"orgId": 1, "uid": "aye-b"}, {"orgId": 1, "uid": "aye-c"}]
    assert yaml.safe_load(text)["groups"] == yaml.safe_load(rendered)["groups"]


def test_nothing_gone_keeps_the_rendered_text_byte_for_byte() -> None:
    rendered = rules("aye-a", "aye-b") + "\n"
    assert retire_rules(rules("aye-b", "aye-a"), rendered) == (rendered, [])


def test_a_first_deploy_without_a_live_file_retires_nothing() -> None:
    rendered = rules("aye-a")
    assert retire_rules("", rendered) == (rendered, [])


def test_earlier_deletions_are_kept_but_not_reported_again() -> None:
    text, retired = retire_rules(rules("aye-a", deleted=("aye-old",)), rules("aye-a"))
    assert retired == []
    assert deletions(text) == [{"orgId": 1, "uid": "aye-old"}]


def test_a_rule_that_comes_back_is_no_longer_deleted() -> None:
    rendered = rules("aye-a", "aye-old")
    assert retire_rules(rules("aye-a", deleted=("aye-old",)), rendered) == (rendered, [])


def test_the_deletion_keeps_the_organisation_of_the_rule() -> None:
    text, _ = retire_rules(rules("aye-a", "aye-b", org=3), rules("aye-a", org=3))
    assert deletions(text) == [{"orgId": 3, "uid": "aye-b"}]


def test_provisioned_skips_entries_without_a_uid_and_defaults_the_organisation() -> None:
    text = dump_yaml(
        {
            "groups": [{"rules": [{"uid": "aye-a"}, {"title": "no uid"}]}],
            "deleteRules": [{"uid": "aye-x"}, {"orgId": 2}],
        }
    )
    assert provisioned(text) == ({"aye-a": 1}, {"aye-x": 1})


@pytest.mark.parametrize("text", ["- a\n- b\n", "just text\n"])
def test_a_live_file_that_is_not_a_mapping_is_refused(text: str) -> None:
    with pytest.raises(ValueError, match="not a mapping"):
        retire_rules(text, rules("aye-a"))


@pytest.mark.skipif(not shutil.which("sh"), reason="needs sh")
def test_read_script_prints_the_file_or_nothing_and_survives_spaces(tmp_path: Path) -> None:
    target = tmp_path / "with space" / RULES
    script = read_script(target.as_posix())
    missing = subprocess.run(["sh", "-c", script], capture_output=True, text=True, check=False)
    assert (missing.returncode, missing.stdout) == (0, "")
    target.parent.mkdir(parents=True)
    target.write_text("apiVersion: 1\n", "utf-8")
    present = subprocess.run(["sh", "-c", script], capture_output=True, text=True, check=False)
    assert (present.returncode, present.stdout) == (0, "apiVersion: 1\n")


def test_the_cli_rewrites_the_rendered_file_and_names_what_it_retired(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    live = tmp_path / "live.yaml"
    rendered = tmp_path / "rules.yaml"
    live.write_text(rules("aye-a", "aye-gone"), "utf-8")
    rendered.write_text(rules("aye-a"), "utf-8")
    assert main(["retire-rules", str(live), str(rendered)]) == 0
    assert capsys.readouterr().out == "retired aye-gone\n"
    assert deletions(rendered.read_text("utf-8")) == [{"orgId": 1, "uid": "aye-gone"}]


def test_the_cli_reports_an_unreadable_file_and_leaves_the_render_alone(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rendered = tmp_path / "rules.yaml"
    rendered.write_text(rules("aye-a"), "utf-8")
    assert main(["retire-rules", str(tmp_path / "absent.yaml"), str(rendered)]) == 1
    assert capsys.readouterr().err.startswith("retire-rules: ")
    assert rendered.read_text("utf-8") == rules("aye-a")
