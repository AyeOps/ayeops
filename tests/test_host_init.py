from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from ayeops import hostinit
from ayeops.cli import main
from ayeops.comments import hash_comments
from ayeops.deploy import HELPER, REMOTE_ROOT, Result, deploy, hostside, tarball
from ayeops.hostinit import (
    InitError,
    Plan,
    build_plan,
    init_script,
    key_line,
    public_key,
    source_list,
    sudoers,
)
from ayeops.registry import load_modules
from tests.conftest import EXAMPLE, example_data, manifest_from, render_once

TOOLS = all(shutil.which(tool) for tool in ("sh", "tar", "cmp", "grep", "awk", "find"))
needs_shell = pytest.mark.skipif(not TOOLS, reason="needs sh, tar, cmp, grep, awk and find")
KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOkFakeDeployKey0123456789abcdefghijklmnop ci@acme"
SOURCE = "/usr/local/libexec/ayeops"

FAKES = {
    "id": """if [ "$#" -eq 1 ] && [ "$1" = -u ]; then echo 0; exit 0; fi
exit 1
""",
    "useradd": """for a in "$@"; do
  case $prev in
    --home-dir) home=$a ;;
    --shell) shell=$a ;;
  esac
  prev=$a
done
printf '%s:x:998:998::%s:%s\\n' "$a" "$home" "$shell" >> "$ROOT/etc/passwd"
printf '%s:!:20000::::::\\n' "$a" >> "$ROOT/etc/shadow"
echo "useradd $*" >> "$STATE/calls"
""",
    "chpasswd": """IFS=: read -r u h
awk -F: -v OFS=: -v u="$u" -v h="$h" '$1 == u { $2 = h } 1' "$ROOT/etc/shadow" > "$STATE/shadow"
mv "$STATE/shadow" "$ROOT/etc/shadow"
echo "chpasswd $*" >> "$STATE/calls"
""",
    "chown": """echo "$1" > "$STATE/$(echo "$2" | tr '/: ' '___').owner"
""",
    "chmod": """echo "${1#0}" > "$STATE/$(echo "$2" | tr '/: ' '___').mode"
""",
    "stat": """key=$STATE/$(echo "$3" | tr '/: ' '___')
owned=$(cat "$key.owner" 2>/dev/null || echo 1:1)
printf '%s %s\\n' "$owned" "$(cat "$key.mode" 2>/dev/null || echo 777)"
""",
    "visudo": """echo "visudo $*" >> "$STATE/calls"
if [ -f "$STATE/visudo-fails" ]; then exit 1; fi
if [ -f "$STATE/sudo-rs" ] && grep -q requiretty "$2"; then
  echo "$2:1:15: syntax error: unknown setting: 'requiretty'" >&2
  exit 1
fi
grep -q 'NOPASSWD: ' "$2"
""",
    "sudo": """echo "sudo $*" >> "$STATE/calls"
if [ "$1" = -V ]; then
  if [ -f "$STATE/sudo-rs" ]; then echo "sudo-rs 0.2.8"; else echo "Sudo version 1.9.16p2"; fi
  exit 0
fi
if [ "$1" = -n ]; then shift; fi
exec sh "$@"
""",
    "mv": """if [ "$1" = -f ]; then shift; fi
from=$STATE/$(echo "$1" | tr '/: ' '___')
to=$STATE/$(echo "$2" | tr '/: ' '___')
for ext in owner mode; do
  if [ -f "$from.$ext" ]; then cat "$from.$ext" > "$to.$ext"; fi
done
PATH=${PATH#*:} exec mv -f "$1" "$2"
""",
    "systemctl": """echo "systemctl $*" >> "$STATE/calls"
""",
    "docker": """echo "docker $* in $(pwd)" >> "$STATE/calls"
if [ "$2" = ps ]; then cat "$STATE/ps.json"; fi
""",
}


def posix(path: Path) -> str:
    text = path.as_posix()
    if os.name == "nt" and text[1:3] == ":/":
        return f"/{text[0].lower()}{text[2:]}"
    return text


def fake_bin(tmp_path: Path) -> tuple[Path, Path, Path]:
    root = tmp_path / "root"
    state = tmp_path / "state"
    bin_dir = tmp_path / "bin"
    for directory in (root / "etc" / "sudoers.d", state, bin_dir):
        directory.mkdir(parents=True)
    (root / "etc" / "passwd").write_text(
        "root:x:0:0::/root:/bin/sh\n", encoding="utf-8", newline="\n"
    )
    (root / "etc" / "shadow").write_text("root:*:20000::::::\n", encoding="utf-8", newline="\n")
    (root / "etc" / "sudoers").write_text(
        "@includedir /etc/sudoers.d\n", encoding="utf-8", newline="\n"
    )
    for name, body in FAKES.items():
        path = bin_dir / name
        path.write_text(f"#!/bin/sh\n{body}", encoding="utf-8", newline="\n")
        path.chmod(0o755)
    return root, state, bin_dir


def shell_env(root: Path, state: Path, bin_dir: Path, **extra: str) -> dict[str, str]:
    path = f"{bin_dir.as_posix()}{os.pathsep}{os.environ.get('PATH', '')}"
    return {**os.environ, "PATH": path, "ROOT": root.as_posix(), "STATE": state.as_posix(), **extra}


def run_script(
    script: str, env: dict[str, str], tmp_path: Path, stdin: bytes | None = None
) -> subprocess.CompletedProcess[str]:
    path = tmp_path / "init.sh"
    path.write_text(script, encoding="utf-8", newline="\n")
    return subprocess.run(
        ["sh", path.as_posix()],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        input=stdin.decode() if stdin else None,
    )


def owner(state: Path, path: Path) -> str:
    key = path.as_posix().replace("/", "_").replace(":", "_").replace(" ", "_")
    owned = (state / f"{key}.owner").read_text(encoding="utf-8").strip()
    mode = (state / f"{key}.mode").read_text(encoding="utf-8").strip()
    return f"{owned} {mode}"


def plan_for(data: dict[str, Any], host: str = "central", **kwargs: Any) -> Plan:
    options: dict[str, Any] = {"user": "aye", "key_text": KEY, "sources": []}
    options.update(kwargs)
    return build_plan(
        manifest_from(data),
        load_modules(),
        host,
        options["user"],
        options["key_text"],
        options["sources"],
    )


def test_the_key_line_forces_the_helper_and_forbids_forwarding_and_a_pty() -> None:
    plan = Plan("central", "aye", KEY, source_list(["10.8.0.0/24", "192.168.1.7"]))
    assert key_line(plan) == (
        f'restrict,command="{HELPER} ssh",from="10.8.0.0/24,192.168.1.7/32" {KEY}'
    )
    assert key_line(Plan("central", "aye", KEY)) == f'restrict,command="{HELPER} ssh" {KEY}'


RULE = [
    f"aye ALL=(root) NOPASSWD: {HELPER} /opt/ayeops/central rules, \\",
    f"    {HELPER} /opt/ayeops/central pull, \\",
    f"    {HELPER} /opt/ayeops/central swap, \\",
    f"    {HELPER} /opt/ayeops/central up, \\",
    f"    {HELPER} /opt/ayeops/central ps, \\",
    f"    {HELPER} /opt/ayeops/central diff, \\",
    f"    {HELPER} /opt/ayeops/central timers",
]


def test_sudoers_allows_exactly_the_seven_steps_on_one_directory() -> None:
    text = sudoers(Plan("central", "aye"))
    assert text.splitlines() == ["Defaults:aye !requiretty", *RULE]
    assert not set("*?[]") & set(text)
    assert "ALL\n" not in text
    assert text.endswith("timers\n")


def test_the_sudo_rs_rule_is_the_same_command_list_without_defaults() -> None:
    plan = Plan("central", "aye")
    text = sudoers(plan, sudo_rs=True)
    assert text.splitlines() == RULE
    assert "Defaults" not in text
    assert sudoers(plan) == f"Defaults:aye !requiretty\n{text}"


@pytest.mark.parametrize(
    "text",
    [
        "",
        f'command="sh" {KEY}',
        f"{KEY}\n{KEY}",
        "ssh-ed25519",
        "ssh-dss AAAAB3NzaC1kc3M= old",
        f"{KEY}é",
    ],
)
def test_a_key_file_with_options_or_more_than_one_key_is_refused(text: str) -> None:
    with pytest.raises(InitError, match="--ssh-key"):
        public_key(text)


def test_a_key_file_keeps_its_comment_and_drops_blank_lines() -> None:
    assert public_key(f"\n{KEY}\n\n") == KEY


def test_from_takes_addresses_and_networks_only() -> None:
    assert source_list(["10.0.0.1", "10.8.0.9/24", "fd00::1/64"]) == (
        "10.0.0.1/32",
        "10.8.0.0/24",
        "fd00::/64",
    )
    with pytest.raises(InitError, match="--from"):
        source_list(["10.0.0.0/8,0.0.0.0/0"])


@pytest.mark.parametrize("user", ["root ", "Aye", "a;b", "a" * 33, "1aye"])
def test_a_user_name_that_is_not_a_system_user_name_is_refused(user: str) -> None:
    with pytest.raises(InitError, match="--user"):
        plan_for(example_data(), user=user)


def test_an_unknown_host_is_refused() -> None:
    with pytest.raises(InitError, match="unknown host 'nowhere'"):
        plan_for(example_data(), host="nowhere")


def test_module_directories_come_from_the_manifest() -> None:
    data = example_data()
    assert [path for path, *_ in plan_for(data).directories] == ["/var/backups/ayeops/metrics"]
    data["hosts"]["central"]["modules"]["metrics"]["backup"] = False
    assert plan_for(data).directories == ()
    data["hosts"]["central"]["modules"]["metrics"]["backup"] = True
    data["hosts"]["central"]["modules"]["node"] = {"textfile_dir": "/var/lib/ayeops/textfile"}
    data["hosts"]["central"]["modules"]["objectstore"]["data_dir"] = "/srv/garage/data"
    data["hosts"]["central"]["modules"]["objectstore"]["meta_dir"] = "/srv/garage/meta"
    directories = plan_for(data).directories
    assert [(path, owner, mode) for path, owner, mode, _ in directories] == [
        ("/var/backups/ayeops/metrics", "0:0", "0700"),
        ("/var/lib/ayeops/textfile", "0:0", "0755"),
        ("/srv/garage/data", "0:0", "0700"),
        ("/srv/garage/meta", "0:0", "0700"),
    ]
    assert directories[0][3].startswith("metrics: the archives of the metrics store")
    assert directories[1][3].startswith("node: the textfile collectors")
    data["hosts"]["central"]["modules"]["objectstore"]["provider"] = "rest-server"
    del data["hosts"]["central"]["modules"]["objectstore"]["meta_dir"]
    for key in ("bucket", "region"):
        data["hosts"]["central"]["modules"]["objectstore"].pop(key, None)
    paths = [path for path, *_ in plan_for(data).directories]
    assert paths == ["/var/backups/ayeops/metrics", "/var/lib/ayeops/textfile", "/srv/garage/data"]


@needs_shell
def test_the_script_is_valid_sh_with_and_without_a_key(tmp_path: Path) -> None:
    for key in (KEY, None):
        script = init_script(plan_for(example_data(), key_text=key), "acme.yaml", "10.8.0.1")
        path = tmp_path / "init.sh"
        path.write_text(script, encoding="utf-8", newline="\n")
        done = subprocess.run(["sh", "-n", path.as_posix()], capture_output=True, check=False)
        assert done.returncode == 0, done.stderr


def test_the_script_embeds_the_packaged_helper_scripts_byte_for_byte() -> None:
    script = init_script(plan_for(example_data()), "acme.yaml", "10.8.0.1")
    assert f"<<'AYE_HOST_INIT_EOF'\n{hostside('aye-host.sh')}AYE_HOST_INIT_EOF\n" in script
    assert f"<<'AYE_HOST_INIT_EOF'\n{hostside('pull.sh')}AYE_HOST_INIT_EOF\n" in script
    assert f"<<'AYE_HOST_INIT_EOF'\n{hostside('swap.sh')}AYE_HOST_INIT_EOF\n" in script
    assert f"<<'AYE_HOST_INIT_EOF'\n{hostside('diff.sh')}AYE_HOST_INIT_EOF\n" in script
    assert f"<<'AYE_HOST_INIT_EOF'\n{hostside('timers.sh')}AYE_HOST_INIT_EOF\n" in script
    assert 'install_file "$LIB/timers.sh" "$LIB/timers.sh.aye-new" 0:0 0644' in script
    assert script.splitlines()[-1] == (
        "say 'deploy with: aye deploy acme.yaml --host central --ssh aye@10.8.0.1 --helper'"
    )
    assert [text for _, text in hash_comments(script) if text != "#!/bin/sh"] == []


@needs_shell
def test_the_script_sets_up_the_host_under_a_fake_root_and_is_idempotent(tmp_path: Path) -> None:
    root, state, bin_dir = fake_bin(tmp_path)
    data = example_data()
    data["hosts"]["central"]["modules"]["node"] = {"textfile_dir": "/var/lib/ayeops/textfile"}
    plan = plan_for(data, sources=["10.8.0.0/24"])
    script = init_script(plan, "acme.yaml", "10.8.0.1", root.as_posix())
    env = shell_env(root, state, bin_dir)
    first = run_script(script, env, tmp_path)
    assert first.returncode == 0, first.stdout + first.stderr
    lines = first.stdout.splitlines()
    assert "aye host-init: changed: created system user aye" in lines
    assert "aye host-init: changed: password of aye set to *, no password login" in lines
    assert any(line.endswith("/opt/ayeops/central, owner 998:998, mode 0750") for line in lines)
    assert any(
        "/var/lib/ayeops/textfile, owner 0:0, mode 0755, for node:" in line for line in lines
    )
    assert "aye:x:998:998::/var/lib/ayeops/aye:/bin/sh" in (root / "etc/passwd").read_text("utf-8")
    assert "aye:*:" in (root / "etc/shadow").read_text("utf-8")
    keys = root / "var/lib/ayeops/aye/.ssh/authorized_keys"
    assert keys.read_text("utf-8") == key_line(plan) + "\n"
    assert owner(state, keys) == "0:0 644"
    target = root / "opt/ayeops/central"
    assert target.is_dir()
    assert owner(state, target) == "998:998 750"
    helper = root / "usr/local/libexec/ayeops/aye-host"
    assert helper.read_text("utf-8") == hostside("aye-host.sh")
    assert owner(state, helper) == "0:0 755"
    assert (root / "usr/local/libexec/ayeops/pull.sh").read_text("utf-8") == hostside("pull.sh")
    assert owner(state, root / "usr/local/libexec/ayeops/pull.sh") == "0:0 644"
    assert (root / "usr/local/libexec/ayeops/swap.sh").read_text("utf-8") == hostside("swap.sh")
    assert (root / "usr/local/libexec/ayeops/diff.sh").read_text("utf-8") == hostside("diff.sh")
    timers = root / "usr/local/libexec/ayeops/timers.sh"
    assert timers.read_text("utf-8") == hostside("timers.sh")
    assert owner(state, timers) == "0:0 644"
    assert not [line for line in lines if "systemctl" in line]
    rule = root / "etc/sudoers.d/ayeops-aye-central"
    assert rule.read_text("utf-8") == sudoers(plan)
    assert owner(state, rule) == "0:0 440"
    assert sorted(path.name for path in (root / "etc/sudoers.d").iterdir()) == [rule.name]
    calls = (state / "calls").read_text("utf-8")
    assert "visudo -cf " in calls
    assert ".ayeops-aye-central.aye-new" in calls
    assert first.stdout.rstrip().splitlines()[-2].endswith(" change(s)")

    second = run_script(script, env, tmp_path)
    assert second.returncode == 0, second.stdout + second.stderr
    assert not [line for line in second.stdout.splitlines() if "changed:" in line]
    assert "aye host-init: 0 change(s)" in second.stdout.splitlines()
    passwd = (root / "etc/passwd").read_text("utf-8").splitlines()
    assert [line for line in passwd if line.startswith("aye:")] == [
        "aye:x:998:998::/var/lib/ayeops/aye:/bin/sh"
    ]


@needs_shell
def test_a_rule_that_visudo_rejects_is_never_installed(tmp_path: Path) -> None:
    root, state, bin_dir = fake_bin(tmp_path)
    (state / "visudo-fails").write_text("", encoding="utf-8")
    script = init_script(plan_for(example_data()), "acme.yaml", "x", root.as_posix())
    done = run_script(script, shell_env(root, state, bin_dir), tmp_path)
    assert done.returncode == 1
    assert "visudo rejected the sudoers rule, nothing installed" in done.stderr
    assert list((root / "etc/sudoers.d").iterdir()) == []


@needs_shell
def test_on_sudo_rs_the_script_installs_the_rule_without_requiretty(tmp_path: Path) -> None:
    root, state, bin_dir = fake_bin(tmp_path)
    (state / "sudo-rs").write_text("", encoding="utf-8")
    plan = plan_for(example_data())
    script = init_script(plan, "acme.yaml", "10.8.0.1", root.as_posix())
    env = shell_env(root, state, bin_dir)
    first = run_script(script, env, tmp_path)
    assert first.returncode == 0, first.stdout + first.stderr
    assert (
        "aye host-init: skipped: Defaults:aye !requiretty, sudo-rs has no requiretty"
        in first.stdout.splitlines()
    )
    rule = root / "etc/sudoers.d/ayeops-aye-central"
    assert rule.read_text("utf-8") == sudoers(plan, sudo_rs=True)
    assert owner(state, rule) == "0:0 440"
    assert "sudo -V" in (state / "calls").read_text("utf-8").splitlines()
    second = run_script(script, env, tmp_path)
    assert second.returncode == 0, second.stdout + second.stderr
    assert "aye host-init: 0 change(s)" in second.stdout.splitlines()
    (state / "sudo-rs").unlink()
    third = run_script(script, env, tmp_path)
    assert third.returncode == 0, third.stdout + third.stderr
    assert "aye host-init: 1 change(s)" in third.stdout.splitlines()
    assert rule.read_text("utf-8") == sudoers(plan)


@needs_shell
def test_the_script_refuses_a_regular_account_and_a_non_root_run(tmp_path: Path) -> None:
    root, state, bin_dir = fake_bin(tmp_path)
    with (root / "etc/passwd").open("a", encoding="utf-8") as passwd:
        passwd.write("aye:x:1000:1000::/home/aye:/bin/bash\n")
    script = init_script(plan_for(example_data()), "acme.yaml", "x", root.as_posix())
    done = run_script(script, shell_env(root, state, bin_dir), tmp_path)
    assert done.returncode == 1
    assert "aye is a regular account (uid 1000)" in done.stderr
    (bin_dir / "id").write_text("#!/bin/sh\necho 1000\n", encoding="utf-8", newline="\n")
    done = run_script(script, shell_env(root, state, bin_dir), tmp_path)
    assert (done.returncode, done.stderr.strip()) == (1, "aye host-init: run this as root")


@needs_shell
def test_a_module_directory_that_exists_with_another_owner_is_left_alone(tmp_path: Path) -> None:
    root, state, bin_dir = fake_bin(tmp_path)
    data = example_data()
    data["hosts"]["central"]["modules"]["node"] = {"textfile_dir": "/srv/textfile"}
    (root / "srv/textfile").mkdir(parents=True)
    script = init_script(plan_for(data), "acme.yaml", "x", root.as_posix())
    done = run_script(script, shell_env(root, state, bin_dir), tmp_path)
    assert done.returncode == 0, done.stderr
    assert any(
        line.endswith(
            "node: the textfile collectors, which write as root in their containers,"
            " and node-exporter, which reads expects 0:0 0755; left alone"
        )
        for line in done.stdout.splitlines()
    )


def patched_helper(tmp_path: Path, bin_dir: Path) -> tuple[Path, Path]:
    base = tmp_path / "opt" / "ayeops"
    lib = tmp_path / "lib"
    lib.mkdir()
    text = hostside("aye-host.sh")
    path_line = "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    assert path_line in text
    text = text.replace(path_line, f"PATH={posix(bin_dir)}:$PATH")
    text = text.replace(f"BASE={REMOTE_ROOT}", f"BASE={posix(base)}")
    text = text.replace(f"LIB={SOURCE}", f"LIB={posix(lib)}")
    helper = lib / "aye-host"
    helper.write_text(text, encoding="utf-8", newline="\n")
    (lib / "pull.sh").write_text(hostside("pull.sh"), encoding="utf-8", newline="\n")
    (lib / "swap.sh").write_text(hostside("swap.sh"), encoding="utf-8", newline="\n")
    (lib / "diff.sh").write_text(hostside("diff.sh"), encoding="utf-8", newline="\n")
    units = tmp_path / "units"
    units.mkdir()
    timers = hostside("timers.sh").replace("\nU=/etc/systemd/system\n", f"\nU={posix(units)}\n")
    (lib / "timers.sh").write_text(timers, encoding="utf-8", newline="\n")
    (base / "central").mkdir(parents=True)
    return helper, base


def helper_run(
    helper: Path, env: dict[str, str], *args: str, stdin: bytes | None = None
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["sh", helper.as_posix(), *args], env=env, input=stdin, capture_output=True, check=False
    )


@needs_shell
def test_the_helper_refuses_every_path_outside_its_directory(tmp_path: Path) -> None:
    _, state, bin_dir = fake_bin(tmp_path)
    helper, base = patched_helper(tmp_path, bin_dir)
    env = shell_env(tmp_path, state, bin_dir)
    (base / "file").write_text("", encoding="utf-8")
    refused = [
        "/etc",
        posix(base),
        f"{posix(base)}/",
        f"{posix(base)}/../etc",
        f"{posix(base)}/central/..",
        f"{posix(base)}/central/sub",
        f"{posix(base)}/.hidden",
        f"{posix(base)}/missing",
        f"{posix(base)}/file",
        f"{posix(base)}/a b",
        f"{posix(base)}x/central",
    ]
    for directory in refused:
        done = helper_run(helper, env, directory, "rules")
        assert done.returncode == 64, directory
        assert b"is not a directory under" in done.stderr, directory
    for args in [(), ("rules",), (f"{posix(base)}/central", "shell"), ("a", "b", "c")]:
        done = helper_run(helper, env, *args)
        assert done.returncode == 64, args
        assert b"usage: aye-host" in done.stderr or b"is not a directory" in done.stderr
    assert not (state / "calls").exists()


@needs_shell
@pytest.mark.skipif(os.name == "nt", reason="needs real symbolic links")
def test_the_helper_refuses_a_link_in_place_of_its_directory(tmp_path: Path) -> None:
    _, state, bin_dir = fake_bin(tmp_path)
    helper, base = patched_helper(tmp_path, bin_dir)
    (base / "linked").symlink_to(tmp_path)
    done = helper_run(helper, shell_env(tmp_path, state, bin_dir), f"{base}/linked", "rules")
    assert done.returncode == 64


@needs_shell
@pytest.mark.skipif(os.name == "nt", reason="needs real symbolic links")
def test_the_helper_never_reads_a_rules_file_that_is_a_link(tmp_path: Path) -> None:
    _, state, bin_dir = fake_bin(tmp_path)
    helper, base = patched_helper(tmp_path, bin_dir)
    secret = tmp_path / "shadow"
    secret.write_text("root:secret\n", encoding="utf-8")
    rules = base / "central/grafana/provisioning/alerting/rules.yaml"
    rules.parent.mkdir(parents=True)
    rules.symlink_to(secret)
    env = shell_env(tmp_path, state, bin_dir)
    done = helper_run(helper, env, f"{posix(base)}/central", "rules")
    assert done.returncode == 0
    assert done.stdout == b""


@needs_shell
def test_the_helper_runs_its_steps_inside_its_directory(tmp_path: Path) -> None:
    _, state, bin_dir = fake_bin(tmp_path)
    helper, base = patched_helper(tmp_path, bin_dir)
    env = shell_env(tmp_path, state, bin_dir)
    target = f"{posix(base)}/central"
    assert helper_run(helper, env, target, "rules").stdout == b""
    rules = base / "central/grafana/provisioning/alerting/rules.yaml"
    rules.parent.mkdir(parents=True)
    rules.write_text("apiVersion: 1\n", encoding="utf-8", newline="\n")
    assert helper_run(helper, env, target, "rules").stdout == b"apiVersion: 1\n"
    pulled = helper_run(helper, env, target, "pull", stdin=b"services: {}\n")
    assert (pulled.returncode, pulled.stdout) == (0, b"staged\n"), pulled.stderr
    assert not (base / "central/compose.yaml").exists()
    assert not (base / "central/.aye-staging").exists()
    swapped = helper_run(helper, env, target, "swap", stdin=tarball({"compose.yaml": "c\n"}))
    assert swapped.returncode == 0, swapped.stderr
    assert swapped.stdout == b"changed compose.yaml\n"
    assert (base / "central/compose.yaml").read_text("utf-8") == "c\n"
    (state / "ps.json").write_text("[]", encoding="utf-8")
    assert helper_run(helper, env, target, "up").returncode == 0
    assert helper_run(helper, env, target, "ps").stdout == b"[]"
    calls = (state / "calls").read_text("utf-8").splitlines()
    assert [call.split(" in ")[0] for call in calls] == [
        f"docker compose --project-directory {target} -f {target}/.aye-staging/compose.yaml"
        " pull --quiet --policy missing --ignore-buildable",
        "docker compose up -d --remove-orphans",
        "docker compose ps --all --format json",
    ]
    assert all(call.endswith("/opt/ayeops/central") for call in calls)


@needs_shell
def test_the_ssh_key_runs_the_helper_through_sudo_and_nothing_else(tmp_path: Path) -> None:
    _, state, bin_dir = fake_bin(tmp_path)
    helper, base = patched_helper(tmp_path, bin_dir)
    target = f"{posix(base)}/central"
    self_path = f"{posix(tmp_path)}/lib/aye-host"

    def over_ssh(command: str) -> subprocess.CompletedProcess[bytes]:
        env = shell_env(tmp_path, state, bin_dir, SSH_ORIGINAL_COMMAND=command)
        return helper_run(helper, env, "ssh")

    rules = base / "central/grafana/provisioning/alerting/rules.yaml"
    rules.parent.mkdir(parents=True)
    rules.write_text("ok\n", encoding="utf-8", newline="\n")
    done = over_ssh(f"sudo -n {self_path} {target} rules")
    assert (done.returncode, done.stdout) == (0, b"ok\n")
    assert (state / "calls").read_text("utf-8") == f"sudo -n {self_path} {target} rules\n"
    for command in [
        "",
        "sh",
        "id",
        f"sudo -n {self_path} {target} rules; id",
        f"sudo -n {self_path} {target} rules extra",
        f"sudo -n {self_path} {target} pull --policy always",
        f"sudo -n {self_path} {target} pull;id",
        f"sudo -n /bin/sh {target} rules",
        f"sudo {self_path} {target} rules x",
        f"sudo -n {self_path} $(id) rules",
        f"sudo -n {self_path} {target}/* rules",
        "scp -t /tmp",
    ]:
        refused = over_ssh(command)
        assert refused.returncode == 64, command
        assert b"this key runs" in refused.stderr, command
    assert (state / "calls").read_text("utf-8").count("\n") == 1


@needs_shell
def test_deploy_through_the_helper_swaps_atomically_and_starts_compose(tmp_path: Path) -> None:
    _, state, bin_dir = fake_bin(tmp_path)
    helper, base = patched_helper(tmp_path, bin_dir)
    env = shell_env(tmp_path, state, bin_dir)
    compose = yaml.safe_load(
        render_once(manifest_from(example_data()), load_modules())["edge/compose.yaml"]
    )
    rows = [{"Service": name, "State": "running"} for name in compose["services"]]
    (state / "ps.json").write_text(json.dumps(rows), encoding="utf-8")
    (base / "edge").mkdir()
    (base / "edge" / ".env").write_text("SECRET=1\n", encoding="utf-8", newline="\n")
    seen: list[str] = []

    def runner(argv: list[str], stdin: bytes | None, timeout: float) -> Result:
        seen.append(argv[-1])
        words = argv[-1].split()
        assert words[:3] == ["sudo", "-n", HELPER]
        directory = words[3].replace(REMOTE_ROOT, posix(base))
        done = helper_run(helper, env, directory, words[4], stdin=stdin)
        return Result(done.returncode, done.stdout.decode(), done.stderr.decode())

    assert deploy(EXAMPLE, "edge", "aye@edge", runner=runner, helper=True) == 0
    assert [command.split()[-1] for command in seen] == ["pull", "swap", "up", "timers", "ps"]
    unit = "ayeops-edge-backup-acme-db"
    installed = tmp_path / "units"
    assert sorted(path.name for path in installed.iterdir()) == [
        f"{unit}.service",
        f"{unit}.timer",
    ]
    rendered = base / "edge" / "backups" / "systemd" / f"{unit}.timer"
    assert (installed / f"{unit}.timer").read_bytes() == rendered.read_bytes()
    calls = (state / "calls").read_text("utf-8").splitlines()
    assert [call for call in calls if call.startswith("systemctl")] == [
        "systemctl daemon-reload",
        f"systemctl --quiet enable {unit}.timer",
        f"systemctl restart {unit}.timer",
    ]
    assert (base / "edge" / "compose.yaml").is_file()
    assert (base / "edge" / ".env").read_text("utf-8") == "SECRET=1\n"
    assert not (base / "edge" / ".aye-staging").exists()


def test_the_cli_prints_the_script_and_reports_bad_input(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    key = tmp_path / "deploy.pub"
    key.write_text(KEY + "\n", encoding="utf-8", newline="\n")
    argv = ["host-init", str(EXAMPLE), "--host", "edge", "--ssh-key", str(key)]
    assert main([*argv, "--from", "10.8.0.0/24"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("#!/bin/sh\nset -eu\n")
    assert 'from="10.8.0.0/24"' in out
    assert main([*argv, "--from", "nowhere"]) == 2
    assert capsys.readouterr().err.startswith("host-init: --from: ")
    assert main(["host-init", str(EXAMPLE), "--host", "edge", "--ssh-key", str(tmp_path)]) == 2
    assert capsys.readouterr().err.startswith("host-init: ")
    assert main(["host-init", str(EXAMPLE), "--host", "nowhere"]) == 2
    assert capsys.readouterr().err == "host-init: unknown host 'nowhere'\n"


def test_the_cli_refuses_a_manifest_that_fails_the_check(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    data = example_data()
    del data["services"]["web"]["description"]
    broken = tmp_path / "broken.yaml"
    broken.write_text(yaml.safe_dump(data), encoding="utf-8")
    assert main(["host-init", str(broken), "--host", "edge"]) == 1
    assert (
        capsys.readouterr()
        .err.splitlines()[-1]
        .endswith("no host-init script, fix the errors first")
    )


def test_the_helper_lives_where_the_key_line_points() -> None:
    assert f"{SOURCE}/aye-host" == HELPER
    assert hostinit.key_line(Plan("h", "aye", KEY)).startswith(f'restrict,command="{HELPER} ssh"')


@needs_shell
def test_the_script_warns_when_backup_timers_would_have_no_systemd(tmp_path: Path) -> None:
    if shutil.which("systemctl"):
        pytest.skip("this host has a real systemctl")
    root, state, bin_dir = fake_bin(tmp_path)
    (bin_dir / "systemctl").unlink()
    script = init_script(plan_for(example_data()), "acme.yaml", "x", root.as_posix())
    done = run_script(script, shell_env(root, state, bin_dir), tmp_path)
    assert done.returncode == 0, done.stderr
    assert (
        "aye host-init: warning: systemctl is not installed;"
        " backup jobs with a command need systemd timers"
    ) in done.stdout.splitlines()
