from __future__ import annotations

import argparse
import shlex
import sys
from pathlib import Path
from typing import Any

import yaml

from ayeops.render import dump_yaml

RULES = "grafana/provisioning/alerting/rules.yaml"


def provisioned(text: str) -> tuple[dict[str, int], dict[str, int]]:
    data = yaml.safe_load(text) or {}
    if not isinstance(data, dict):
        raise ValueError("alert rules file is not a mapping")
    rules = {
        str(rule["uid"]): int(group.get("orgId", 1))
        for group in data.get("groups") or []
        for rule in group.get("rules") or []
        if rule.get("uid")
    }
    deleted = {
        str(entry["uid"]): int(entry.get("orgId", 1))
        for entry in data.get("deleteRules") or []
        if entry.get("uid")
    }
    return rules, deleted


def retire_rules(live: str, rendered: str) -> tuple[str, list[str]]:
    old_rules, old_deleted = provisioned(live)
    new_rules, new_deleted = provisioned(rendered)
    gone = {**old_deleted, **old_rules, **new_deleted}
    for uid in new_rules:
        gone.pop(uid, None)
    if not gone:
        return rendered, []
    data: dict[str, Any] = yaml.safe_load(rendered) or {}
    data["deleteRules"] = [{"orgId": gone[uid], "uid": uid} for uid in sorted(gone)]
    return dump_yaml(data), sorted(set(gone) - set(old_deleted) - set(new_deleted))


def read_script(path: str) -> str:
    quoted = shlex.quote(path)
    return f"if [ -f {quoted} ]; then cat {quoted}; fi"


def add_parser(commands: Any) -> None:
    parser: argparse.ArgumentParser = commands.add_parser(
        "retire-rules",
        help="add deleteRules for alert rules that the live file has and the rendered one lost",
    )
    parser.add_argument("live", type=Path, help="the rules.yaml Grafana runs with now")
    parser.add_argument("rendered", type=Path, help="the new rules.yaml, rewritten in place")


def run(args: argparse.Namespace) -> int:
    try:
        text, retired = retire_rules(args.live.read_text("utf-8"), args.rendered.read_text("utf-8"))
    except (OSError, ValueError, yaml.YAMLError) as error:
        print(f"retire-rules: {error}", file=sys.stderr)
        return 1
    args.rendered.write_text(text, "utf-8", newline="\n")
    for uid in retired:
        print(f"retired {uid}")
    return 0
