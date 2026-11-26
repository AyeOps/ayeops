from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date
from pathlib import Path
from typing import Any

from ayeops import __version__, dashboard_sync, render_index, status_deploy
from ayeops import deploy as deploy_command
from ayeops import diff as diff_command
from ayeops import doctor as doctor_command
from ayeops import duties as duties_command
from ayeops import hostinit as hostinit_command
from ayeops import inventory as inventory_command
from ayeops import permissions as permissions_command
from ayeops import retire as retire_command
from ayeops import secrets as secrets_command
from ayeops import up as up_command
from ayeops.defaults import UsageError, add_manifest, find_manifest
from ayeops.discover import command as discover
from ayeops.dns import TOKEN_ENV, Cloudflare, DnsProvider, sync
from ayeops.local_modules import with_local_modules
from ayeops.manifest import load_manifest, schema_errors, unknown_profiles, version_problems
from ayeops.portal.pages import PortalDrift, portal_pages
from ayeops.registry import load_modules
from ayeops.render import render_tree, write_tree
from ayeops.report import main as report_main
from ayeops.rules import Finding, check_manifest, check_modules, check_warnings
from ayeops.stamp import current_stamp
from ayeops.status import document_json, seed_sql, status_document
from ayeops.yamlkeys import StrictYAMLError


def _report(findings: list[Finding]) -> None:
    for finding in findings:
        print(finding, file=sys.stderr)


def day(text: str) -> date:
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a day as YYYY-MM-DD") from None


def check(manifest_path: Path, today: date | None = None, strict: bool = False) -> int:
    manifest = load_manifest(manifest_path)
    modules = load_modules()
    findings = check_manifest(manifest, modules, today)
    warnings = check_warnings(manifest, today, modules)
    _report(findings + warnings)
    noted = f", {len(warnings)} warning(s)" if warnings else ""
    if findings:
        print(f"{manifest_path}: {len(findings)} error(s){noted}", file=sys.stderr)
        return 1
    if strict and warnings:
        print(
            f"{manifest_path}: {len(warnings)} warning(s), failed under --strict", file=sys.stderr
        )
        return 1
    print(f"{manifest_path}: ok{noted}")
    return 0


def _drift(manifest_path: Path, error: PortalDrift) -> int:
    for problem in error.problems:
        print(f"error [portal-drift] {problem}", file=sys.stderr)
    print(f"{manifest_path}: the portal disagrees with the manifest", file=sys.stderr)
    return 1


def render(
    manifest_path: Path,
    out_dir: Path,
    time: str | None = None,
    commit: str | None = None,
    check_only: bool = False,
) -> int:
    manifest = load_manifest(manifest_path)
    modules = load_modules()
    findings = check_manifest(manifest, modules)
    if findings:
        _report(findings)
        print(f"{manifest_path}: not rendered, fix the errors first", file=sys.stderr)
        return 1
    stamp = current_stamp(manifest_path, time, commit)
    try:
        tree = render_tree(manifest, modules, stamp)
    except PortalDrift as error:
        return _drift(manifest_path, error)
    if check_only:
        return _render_check(manifest_path, out_dir, tree)
    written, removed = render_index.sync(tree, out_dir)
    for path in removed:
        print(f"removed {path}")
    print(
        f"{manifest_path}: {len(written)} file(s) written to {out_dir},"
        f" {len(removed)} stale removed"
    )
    return 0


def _render_check(manifest_path: Path, out_dir: Path, tree: dict[str, str]) -> int:
    found = render_index.drift(tree, out_dir)
    for line in found.lines():
        print(line)
    if found:
        print(
            f"{manifest_path}: {out_dir} differs from the render: {len(found.added)} added,"
            f" {len(found.changed)} changed, {len(found.stale)} stale",
            file=sys.stderr,
        )
        return 1
    print(f"{manifest_path}: {out_dir} matches the render")
    return 0


def load_discovered(paths: list[Path]) -> list[dict[str, Any]]:
    reports: list[dict[str, Any]] = []
    for path in paths:
        data = json.loads(path.read_text(encoding="utf-8"))
        reports.extend(
            item for item in (data if isinstance(data, list) else [data]) if isinstance(item, dict)
        )
    return reports


def portal(
    manifest_path: Path,
    out_dir: Path,
    discovered: list[Path] | None = None,
    time: str | None = None,
    commit: str | None = None,
) -> int:
    manifest = load_manifest(manifest_path)
    modules = with_local_modules(manifest, load_modules())
    findings = [Finding(*problem) for problem in version_problems(manifest.raw)]
    if not findings:
        errors = schema_errors(manifest.raw)
        findings = [Finding(path, "schema", message) for path, message in errors]
        findings += [Finding(*problem) for problem in unknown_profiles(manifest.raw)]
    if not findings:
        findings = check_modules(manifest, modules)
    if findings:
        _report(findings)
        print(f"{manifest_path}: no portal, fix the errors first", file=sys.stderr)
        return 1
    try:
        reports = load_discovered(discovered or [])
    except (OSError, ValueError) as error:
        print(f"--discovered: {error}", file=sys.stderr)
        return 2
    stamp = current_stamp(manifest_path, time, commit)
    try:
        pages = portal_pages(manifest, modules, stamp, reports)
    except PortalDrift as error:
        return _drift(manifest_path, error)
    written = write_tree(pages, out_dir)
    print(f"{manifest_path}: {len(written)} page(s) written to {out_dir}")
    return 0


def dns(manifest_path: Path, apply: bool, provider: DnsProvider | None = None) -> int:
    manifest = load_manifest(manifest_path)
    modules = with_local_modules(manifest, load_modules())
    findings = check_manifest(manifest, modules)
    if findings:
        _report(findings)
        print(f"{manifest_path}: no DNS plan, fix the errors first", file=sys.stderr)
        return 1
    if provider is None:
        token = os.environ.get(TOKEN_ENV, "")
        if not token:
            print(f"{TOKEN_ENV} is not set", file=sys.stderr)
            return 2
        provider = Cloudflare(token)
    return sync(manifest, modules, provider, apply, print)


def status(manifest_path: Path, sql: bool) -> int:
    manifest = load_manifest(manifest_path)
    findings = check_manifest(manifest, load_modules())
    if findings:
        _report(findings)
        print(f"{manifest_path}: no status components, fix the errors first", file=sys.stderr)
        return 1
    document = status_document(manifest)
    if not document["hostname"]:
        print(f"{manifest_path}: status_page.hostname is not set", file=sys.stderr)
        return 1
    sys.stdout.write(seed_sql(document) if sql else document_json(document))
    return 0


COMMON = """\
common commands; the manifest defaults to $AYE_MANIFEST, else ayeops.yaml or manifest.yaml here:
  aye up examples/quickstart.yaml --start  check, render, write the .env and start a host here
  aye check                                validate the manifest against the catalog rules
  aye render                               write a compose project per host into rendered/
  aye doctor --all                         read-only preflight of every host over ssh
  aye diff --all                           what a deploy would change on every host
  aye deploy --all --parallel 4            ship every host, canary first, central stack last

exit codes: 0 ok, 1 findings, drift or a failed step, 2 usage or environment error
aye <command> -h shows the options and examples of a command"""

OVERVIEW = f"""\
usage: aye <command> [manifest] [options]

AyeOps: one manifest describes the hosts and services of a small fleet.

{COMMON}; aye -h lists every command"""

EXAMPLES = {
    "check": ["aye check", "aye check ops/ayeops.yaml --strict --today 2026-12-01"],
    "duties": ["aye duties", "aye duties ops/ayeops.yaml --today 2027-01-15"],
    "render": ["aye render", "aye render ops/ayeops.yaml -o ops/rendered --check"],
    "up": ["aye up examples/quickstart.yaml --start", "aye up --host local"],
    "down": ["aye down examples/quickstart.yaml --stop", "aye down --volumes --stop"],
    "portal": ["aye portal -o portal --discovered central.json"],
    "dns": ["aye dns", "aye dns --apply"],
    "status": ["aye status --sql > seed.sql", "aye status deploy --dry-run"],
    "deploy": [
        "aye deploy --host central",
        "aye deploy --all --parallel 4 --dry-run",
        "aye deploy ops/ayeops.yaml --host central --ssh ops-central --sudo",
    ],
    "diff": ["aye diff --host central", "aye diff --all"],
    "doctor": ["aye doctor --host central", "aye doctor --all --parallel 4"],
    "host-init": ["aye host-init --host central --ssh-key deploy.pub --from 10.8.0.0/24"],
    "inventory": ["aye inventory -o inventory.yml", "aye inventory --format json"],
    "permissions": ["aye permissions"],
    "retire-rules": [
        "aye retire-rules live-rules.yaml rendered/central/grafana/provisioning/alerting/rules.yaml"
    ],
    "discover": [
        "aye discover --ssh web-1 --sudo -o web-1.json",
        "aye discover --suggest web-1.json",
        "aye discover --adopt web-1.json -o adopt/ayeops.yaml --until 2026-12-31",
    ],
    "secrets": ["aye secrets gen --env-file rendered/local/.env", "aye secrets pull -o .env"],
    "dashboards": [
        "aye dashboards drift ops/ayeops.yaml",
        "aye dashboards pull ops/ayeops.yaml --uid sales-funnel",
    ],
}

SECRETS_EXAMPLES = {
    "pull": ["aye secrets pull --identity ~/.config/ayeops/central.env -o .env"],
    "push": ["aye secrets push rendered/central/.env"],
    "set": ["aye secrets set AYE_TELEGRAM_BOT_TOKEN --from-clipboard"],
    "ask": ["aye secrets ask AYE_CLOUDFLARE_DNS_API_TOKEN --hint 'Zone DNS edit'"],
    "gen": [
        "aye secrets gen --env-file rendered/local/.env",
        "aye secrets gen --manifest ops/ayeops.yaml --host central",
        "aye secrets gen AYE_AUTH_COOKIE_SECRET",
    ],
    "run": ["aye secrets run --folder /central -- docker compose up -d"],
}

DASHBOARDS_EXAMPLES = {
    "drift": [
        "aye dashboards drift ops/ayeops.yaml",
        "aye dashboards drift --url http://127.0.0.1:3000 --format json",
    ],
    "pull": [
        "aye dashboards pull ops/ayeops.yaml",
        "aye dashboards pull --uid sales-funnel --force",
    ],
}


def example_text(examples: list[str]) -> str:
    heading = "examples" if len(examples) > 1 else "example"
    return f"{heading}:\n" + "\n".join(f"  {line}" for line in examples)


def subcommands(parser: argparse.ArgumentParser) -> Any:
    return next(
        action for action in parser._actions if isinstance(action, argparse._SubParsersAction)
    )


def explain(commands: Any, examples: dict[str, list[str]]) -> None:
    for action in commands._choices_actions:
        if action.dest not in examples:
            continue
        sub: argparse.ArgumentParser = commands.choices[action.dest]
        sub.description = sub.description or action.help
        sub.epilog = example_text(examples[action.dest])
        sub.formatter_class = argparse.RawDescriptionHelpFormatter


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="aye",
        description="AyeOps ops kit: one manifest describes the hosts and services of a fleet",
        epilog=COMMON,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"aye {__version__}")
    commands = parser.add_subparsers(dest="command", required=True, metavar="<command>")
    check_parser = commands.add_parser("check", help="validate a manifest and the catalog rules")
    add_manifest(check_parser)
    check_parser.add_argument(
        "--today",
        type=day,
        metavar="YYYY-MM-DD",
        help="the day waiver ends and duty last_done days are measured against,"
        " default today in the manifest timezone",
    )
    check_parser.add_argument(
        "--strict",
        action="store_true",
        help="fail on warnings too: unused modules, externals, profiles and runbook pages,"
        " waivers about to end",
    )
    duties_parser = commands.add_parser(
        "duties", help="list the recurring duties with their due day, exit 1 when one is overdue"
    )
    add_manifest(duties_parser)
    duties_parser.add_argument(
        "--today",
        type=day,
        metavar="YYYY-MM-DD",
        help="the day due days are measured against, default today in the manifest timezone",
    )
    render_parser = commands.add_parser("render", help="render compose projects per host")
    add_manifest(render_parser)
    render_parser.add_argument(
        "-o", "--out", type=Path, default=up_command.DEFAULT_OUT, help="default rendered"
    )
    render_parser.add_argument(
        "--check",
        action="store_true",
        help="write nothing, list added, changed and stale files, exit 1 unless identical",
    )
    up_command.add_parser(commands)
    portal_parser = commands.add_parser(
        "portal", help="write the portal pages: where things run and how to reach each service"
    )
    add_manifest(portal_parser)
    portal_parser.add_argument("-o", "--out", type=Path, required=True)
    portal_parser.add_argument(
        "--discovered", type=Path, action="append", help="JSON report of aye discover, repeatable"
    )
    for sub in (render_parser, portal_parser):
        sub.add_argument("--build-time", help="footer build time, default now or SOURCE_DATE_EPOCH")
        sub.add_argument("--commit", help="footer manifest commit, default git rev-parse")
    dns_parser = commands.add_parser(
        "dns", help="plan the A records of every ingress route, --apply to write them"
    )
    add_manifest(dns_parser)
    dns_parser.add_argument("--apply", action="store_true", help="write the planned records")
    status_parser = commands.add_parser(
        "status",
        help="print the public status page components as JSON, --sql for a D1 seed;"
        " aye status deploy -h deploys the Worker",
    )
    add_manifest(status_parser)
    status_parser.add_argument("--sql", action="store_true", help="print a D1 seed instead")
    commands.add_parser(
        "report",
        help="push CI, test, coverage, mutation and deploy metrics, see aye report -h",
        add_help=False,
    )
    deploy_command.add_parser(commands)
    diff_command.add_parser(commands)
    doctor_command.add_parser(commands)
    hostinit_command.add_parser(commands)
    inventory_command.add_parser(commands)
    permissions_command.add_parser(commands)
    retire_command.add_parser(commands)
    discover.add_parser(commands)
    secrets_command.add_parser(commands)
    dashboard_sync.add_parser(commands)
    explain(commands, EXAMPLES)
    explain(subcommands(commands.choices["secrets"]), SECRETS_EXAMPLES)
    explain(subcommands(commands.choices["dashboards"]), DASHBOARDS_EXAMPLES)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    try:
        return run_command(arguments)
    except StrictYAMLError as error:
        _report([Finding(*problem) for problem in error.problems])
        print(f"{error.source}: {len(error.problems)} error(s)", file=sys.stderr)
        return 1
    except UsageError as error:
        print(f"aye: {error}", file=sys.stderr)
        return 2
    except FileNotFoundError as error:
        print(f"aye: {error.filename or error}: {error.strerror or 'not found'}", file=sys.stderr)
        return 2


def run_command(arguments: list[str]) -> int:
    if not arguments:
        print(OVERVIEW)
        return 0
    if arguments[:1] == ["report"]:
        return report_main(arguments[1:])
    if arguments[:2] == ["status", "deploy"]:
        return status_deploy.main(arguments[2:])
    args = build_parser().parse_args(arguments)
    if args.command != "secrets" and getattr(args, "manifest", False) is None:
        args.manifest = find_manifest(None)
    if args.command == "check":
        return check(args.manifest, args.today, args.strict)
    if args.command == "duties":
        return duties_command.run(args.manifest, args.today)
    if args.command in ("up", "down"):
        return up_command.run(args, render)
    if args.command == "dns":
        return dns(args.manifest, args.apply)
    if args.command == "portal":
        return portal(args.manifest, args.out, args.discovered, args.build_time, args.commit)
    if args.command == "status":
        return status(args.manifest, args.sql)
    if args.command == "deploy":
        return deploy_command.run(args)
    if args.command == "diff":
        return diff_command.run(args)
    if args.command == "doctor":
        return doctor_command.run(args)
    if args.command == "host-init":
        return hostinit_command.run(args)
    if args.command == "inventory":
        return inventory_command.run(args)
    if args.command == "permissions":
        return permissions_command.run(args)
    if args.command == "retire-rules":
        return retire_command.run(args)
    if args.command == "discover":
        return discover.run(args)
    if args.command == "secrets":
        return secrets_command.run(args)
    if args.command == "dashboards":
        return dashboard_sync.run(args, os.environ, dashboard_sync.urlopen_http)
    return render(args.manifest, args.out, args.build_time, args.commit, args.check)
