from __future__ import annotations

import argparse
import ipaddress
import re
import shlex
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ayeops.defaults import add_manifest, only_host
from ayeops.deploy import HELPER, HELPER_DIR, REMOTE_ROOT, STEPS, SUDO_RS, hostside
from ayeops.local_modules import with_local_modules
from ayeops.manifest import Manifest, load_manifest
from ayeops.permissions import holds, module_context
from ayeops.registry import Module, load_modules
from ayeops.rules import check_manifest

USER = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
KEY = re.compile(
    r"^(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp(256|384|521)|sk-ssh-ed25519@openssh\.com"
    r"|sk-ecdsa-sha2-nistp256@openssh\.com) [A-Za-z0-9+/]+={0,3}( [\x21-\x7e][\x20-\x7e]*)?$"
)
HOMES = "/var/lib/ayeops"
END = "AYE_HOST_INIT_EOF"


class InitError(ValueError):
    pass


@dataclass(frozen=True)
class Plan:
    host: str
    user: str
    key: str | None = None
    sources: tuple[str, ...] = ()
    directories: tuple[tuple[str, str, str, str], ...] = field(default=())

    @property
    def target(self) -> str:
        return f"{REMOTE_ROOT}/{self.host}"

    @property
    def home(self) -> str:
        return f"{HOMES}/{self.user}"


def public_key(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) != 1 or not KEY.match(lines[0]):
        raise InitError("--ssh-key: expected one OpenSSH public key line without options")
    return lines[0]


def source_list(values: list[str]) -> tuple[str, ...]:
    sources = []
    for value in values:
        try:
            sources.append(str(ipaddress.ip_network(value, strict=False)))
        except ValueError as error:
            raise InitError(f"--from: {error}") from error
    return tuple(sources)


def module_directories(
    manifest: Manifest, modules: dict[str, Module], host: str
) -> tuple[tuple[str, str, str, str], ...]:
    found = []
    for name in sorted(manifest.host_modules(host)):
        if name not in modules:
            continue
        context = module_context(manifest, modules, host, name)
        for entry in modules[name].host_directories:
            path = context["settings"].get(entry["setting"])
            if path and holds(entry.get("when"), context):
                found.append((str(path), entry["owner"], entry["mode"], f"{name}: {entry['for']}"))
    return tuple(found)


def key_line(plan: Plan) -> str:
    options = ["restrict", f'command="{HELPER} ssh"']
    if plan.sources:
        options.append(f'from="{",".join(plan.sources)}"')
    return f"{','.join(options)} {plan.key}"


def sudoers_defaults(plan: Plan) -> str:
    return f"Defaults:{plan.user} !requiretty"


def sudoers_rule(plan: Plan) -> str:
    commands = ", \\\n    ".join(f"{HELPER} {plan.target} {step}" for step in STEPS)
    return f"{plan.user} ALL=(root) NOPASSWD: {commands}\n"


def sudoers(plan: Plan, sudo_rs: bool = False) -> str:
    rule = sudoers_rule(plan)
    return rule if sudo_rs else f"{sudoers_defaults(plan)}\n{rule}"


def heredoc(path: str, text: str, redirect: str = ">") -> str:
    if END in text:
        raise InitError(f"{END} inside the text of {path}")
    return f"cat {redirect} {path} <<'{END}'\n{text}{END}\n"


FUNCTIONS = """say() {
  echo "aye host-init: $*"
}

changed() {
  CHANGES=$((CHANGES + 1))
  say "changed: $*"
}

fail() {
  echo "aye host-init: $*" >&2
  exit 1
}

owner_mode() {
  stat -c '%u:%g %a' "$1"
}

ensure_dir() {
  if [ -L "$1" ]; then
    fail "$1 is a symbolic link"
  elif [ ! -d "$1" ]; then
    mkdir -p "$1"
    chown "$2" "$1"
    chmod "$3" "$1"
    changed "created $1, owner $2, mode $3"
  elif [ "$(owner_mode "$1")" != "$2 ${3#0}" ]; then
    chown "$2" "$1"
    chmod "$3" "$1"
    changed "$1 is now owner $2, mode $3"
  else
    say "ok: $1"
  fi
}

module_dir() {
  if [ -L "$1" ]; then
    say "warning: $1 is a symbolic link, left alone"
  elif [ ! -d "$1" ]; then
    mkdir -p "$1"
    chown "$2" "$1"
    chmod "$3" "$1"
    changed "created $1, owner $2, mode $3, for $4"
  elif [ "$(owner_mode "$1")" != "$2 ${3#0}" ]; then
    say "warning: $1 is $(owner_mode "$1"), $4 expects $2 $3; left alone"
  else
    say "ok: $1"
  fi
}

install_file() {
  if [ -L "$1" ]; then
    rm -f "$2"
    fail "$1 is a symbolic link"
  fi
  if [ -f "$1" ] && cmp -s "$2" "$1" && [ "$(owner_mode "$1")" = "$3 ${4#0}" ]; then
    rm -f "$2"
    say "ok: $1"
  else
    chown "$3" "$2"
    chmod "$4" "$2"
    mv -f "$2" "$1"
    changed "wrote $1"
  fi
}
"""


def init_script(plan: Plan, manifest_path: str, address: str, root: str = "") -> str:
    def at(path: str) -> str:
        return shlex.quote(f"{root}{path}")

    quoted = {
        "U": shlex.quote(plan.user),
        "HOME_ENTRY": shlex.quote(plan.home),
        "HOME_DIR": at(plan.home),
        "BASE": at(REMOTE_ROOT),
        "D": at(plan.target),
        "LIB": at(HELPER_DIR),
        "SUDOERS_D": at("/etc/sudoers.d"),
        "SUDOERS_MAIN": at("/etc/sudoers"),
        "PASSWD": at("/etc/passwd"),
        "SHADOW": at("/etc/shadow"),
    }
    sudoers_file = f"ayeops-{plan.user}-{plan.host}"
    lines = ["#!/bin/sh", "set -eu", "umask 022", "CHANGES=0"]
    lines += [f"{name}={value}" for name, value in quoted.items()]
    lines.append(FUNCTIONS)
    lines.append(
        """if [ "$(id -u)" != 0 ]; then
  fail "run this as root"
fi
for tool in sudo visudo awk cmp grep find tar stat chown chmod chpasswd; do
  command -v "$tool" >/dev/null 2>&1 || fail "$tool is not installed"
done
if ! command -v docker >/dev/null 2>&1; then
  say "warning: docker is not installed; aye deploy needs docker compose"
fi
for tool in sha256sum base64; do
  command -v "$tool" >/dev/null 2>&1 || say "warning: $tool is not installed; aye diff needs it"
done
if ! command -v systemctl >/dev/null 2>&1; then
  say "warning: systemctl is not installed; backup jobs with a command need systemd timers"
fi

passwd_entry() {
  awk -F: -v u="$U" '$1 == u { print $3 ":" $4 ":" $6 ":" $7 }' "$PASSWD"
}

entry=$(passwd_entry)
if [ -z "$entry" ]; then
  if command -v useradd >/dev/null 2>&1; then
    useradd --system --user-group --home-dir "$HOME_ENTRY" --no-create-home --shell /bin/sh "$U"
  elif command -v adduser >/dev/null 2>&1; then
    addgroup -S "$U"
    adduser -S -D -H -h "$HOME_ENTRY" -s /bin/sh -G "$U" "$U"
  else
    fail "neither useradd nor adduser is installed"
  fi
  changed "created system user $U"
  entry=$(passwd_entry)
fi
IFS=: read -r uid gid home shell <<EOF
$entry
EOF
if [ "$uid" -ge 1000 ]; then
  fail "$U is a regular account (uid $uid); pick another --user"
fi
if [ "$home" != "$HOME_ENTRY" ]; then
  fail "$U has the home $home, not $HOME_ENTRY; pick another --user"
fi
case $shell in
  */nologin | */false) fail "$U has the shell $shell; ssh cannot run the helper through it" ;;
esac
if [ "$(awk -F: -v u="$U" '$1 == u { print $2 }' "$SHADOW")" != "*" ]; then
  echo "$U:*" | chpasswd -e
  changed "password of $U set to *, no password login"
else
  say "ok: $U has no password"
fi
"""
    )
    lines += [
        'ensure_dir "$HOME_DIR" 0:0 0755',
        'ensure_dir "$HOME_DIR/.ssh" 0:0 0755',
    ]
    if plan.key is None:
        lines.append('say "skipped: no --ssh-key, $HOME_DIR/.ssh/authorized_keys left as it is"')
    else:
        lines += [
            heredoc('"$HOME_DIR/.ssh/authorized_keys.aye-new"', key_line(plan) + "\n").rstrip(),
            'install_file "$HOME_DIR/.ssh/authorized_keys" '
            '"$HOME_DIR/.ssh/authorized_keys.aye-new" 0:0 0644',
        ]
    lines += [
        'ensure_dir "$BASE" 0:0 0755',
        'ensure_dir "$D" "$uid:$gid" 0750',
        'ensure_dir "$LIB" 0:0 0755',
        heredoc('"$LIB/aye-host.aye-new"', hostside("aye-host.sh")).rstrip(),
        'install_file "$LIB/aye-host" "$LIB/aye-host.aye-new" 0:0 0755',
        heredoc('"$LIB/pull.sh.aye-new"', hostside("pull.sh")).rstrip(),
        'install_file "$LIB/pull.sh" "$LIB/pull.sh.aye-new" 0:0 0644',
        heredoc('"$LIB/swap.sh.aye-new"', hostside("swap.sh")).rstrip(),
        'install_file "$LIB/swap.sh" "$LIB/swap.sh.aye-new" 0:0 0644',
        heredoc('"$LIB/diff.sh.aye-new"', hostside("diff.sh")).rstrip(),
        'install_file "$LIB/diff.sh" "$LIB/diff.sh.aye-new" 0:0 0644',
        heredoc('"$LIB/timers.sh.aye-new"', hostside("timers.sh")).rstrip(),
        'install_file "$LIB/timers.sh" "$LIB/timers.sh.aye-new" 0:0 0644',
        '[ -d "$SUDOERS_D" ] || fail "$SUDOERS_D does not exist"',
        f"""SUDO_VERSION=$(sudo -V 2>/dev/null || true)
case $SUDO_VERSION in
  "{SUDO_RS}"*)
    : > "$SUDOERS_D/.{sudoers_file}.aye-new"
    say "skipped: {sudoers_defaults(plan)}, sudo-rs has no requiretty"
    ;;
  *)
    printf '%s\\n' {shlex.quote(sudoers_defaults(plan))} > "$SUDOERS_D/.{sudoers_file}.aye-new"
    ;;
esac""",
        heredoc(f'"$SUDOERS_D/.{sudoers_file}.aye-new"', sudoers_rule(plan), ">>").rstrip(),
        f"""if ! visudo -cf "$SUDOERS_D/.{sudoers_file}.aye-new" >/dev/null; then
  rm -f "$SUDOERS_D/.{sudoers_file}.aye-new"
  fail "visudo rejected the sudoers rule, nothing installed"
fi
install_file "$SUDOERS_D/{sudoers_file}" "$SUDOERS_D/.{sudoers_file}.aye-new" 0:0 0440
if ! grep -Eq '^[@#]includedir[[:space:]]+/etc/sudoers.d' "$SUDOERS_MAIN"; then
  say "warning: $SUDOERS_MAIN does not include /etc/sudoers.d"
fi""",
    ]
    for path, owner, mode, purpose in plan.directories:
        lines.append(f"module_dir {at(path)} {owner} {mode} {shlex.quote(purpose)}")
    deploy = f"aye deploy {manifest_path} --host {plan.host} --ssh {plan.user}@{address} --helper"
    lines += [
        'say "$CHANGES change(s)"',
        f"say {shlex.quote('deploy with: ' + deploy)}",
    ]
    return "\n".join(lines) + "\n"


def build_plan(
    manifest: Manifest,
    modules: dict[str, Module],
    host: str,
    user: str,
    key_text: str | None,
    sources: list[str],
) -> Plan:
    if host not in manifest.hosts:
        raise InitError(f"unknown host {host!r}")
    if not USER.match(user):
        raise InitError(f"--user: {user!r} is not a valid system user name")
    return Plan(
        host,
        user,
        public_key(key_text) if key_text is not None else None,
        source_list(sources),
        module_directories(manifest, modules, host),
    )


def add_parser(commands: Any) -> None:
    parser: argparse.ArgumentParser = commands.add_parser(
        "host-init",
        help="print a root script that gives AyeOps a deploy user and nothing more on one host",
    )
    add_manifest(parser)
    parser.add_argument("--host", help="the host to prepare, default the only host of the manifest")
    parser.add_argument("--user", default="aye", help="system user to create, default aye")
    parser.add_argument("--ssh-key", type=Path, help="public key file of the deploy key")
    parser.add_argument(
        "--from",
        dest="sources",
        action="append",
        default=[],
        metavar="CIDR",
        help="address or network the deploy key may connect from, repeatable",
    )


def run(args: argparse.Namespace) -> int:
    host = args.host or only_host(args.manifest)
    manifest = load_manifest(args.manifest)
    modules = with_local_modules(manifest, load_modules())
    findings = check_manifest(manifest, modules)
    if findings:
        for finding in findings:
            print(finding, file=sys.stderr)
        print(f"{args.manifest}: no host-init script, fix the errors first", file=sys.stderr)
        return 1
    try:
        key_text = args.ssh_key.read_text(encoding="utf-8") if args.ssh_key else None
        plan = build_plan(manifest, modules, host, args.user, key_text, args.sources)
    except (OSError, InitError) as error:
        print(f"host-init: {error}", file=sys.stderr)
        return 2
    address = str(manifest.hosts[host].get("address") or host)
    sys.stdout.write(init_script(plan, args.manifest.as_posix(), address))
    return 0
