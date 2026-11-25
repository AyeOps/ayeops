from __future__ import annotations

import base64
import copy
import hashlib
import re
from pathlib import Path
from typing import Any

import pytest

from ayeops import secretkeys
from ayeops.cli import main
from ayeops.local_modules import spec_problems
from ayeops.permissions import catalog_generated, host_generated
from ayeops.registry import load_modules
from tests.conftest import EXAMPLE, example_data, manifest_from
from tests.test_secrets_cli import Store, printed, store

__all__ = ["store"]

FOLDER = ("prod", "/web")
ALPHABETS = {
    "hex": re.compile(r"^[0-9a-f]+$"),
    "base64": re.compile(r"^[A-Za-z0-9+/]+={0,2}$"),
    "base64url": re.compile(r"^[A-Za-z0-9_-]+$"),
    "garage-key-id": re.compile(r"^GK[0-9a-f]{24}$"),
}


def batches(store: Store) -> list[dict[str, Any]]:
    return [body for method, path, body in store.requests if path == "/api/v4/secrets/batch"]


def fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:8]


@pytest.mark.parametrize(
    ("kind", "size", "length"),
    [
        ("hex", 32, 64),
        ("hex", 16, 32),
        ("base64", 32, 44),
        ("base64", 33, 44),
        ("base64", 34, 48),
        ("base64url", 32, 43),
        ("base64url", 24, 32),
        ("garage-key-id", 0, 26),
    ],
)
def test_each_format_makes_its_alphabet_and_length(kind: str, size: int, length: int) -> None:
    value = secretkeys.generate(size, kind)
    assert len(value) == length == secretkeys.length(kind, size)
    assert ALPHABETS[kind].match(value)


@pytest.mark.parametrize("kind", ["hex", "base64", "base64url"])
def test_the_computed_length_matches_every_size(kind: str) -> None:
    for size in range(secretkeys.MIN_BYTES, 70):
        assert len(secretkeys.generate(size, kind)) == secretkeys.length(kind, size)


def test_base64_carries_the_asked_number_of_random_bytes() -> None:
    assert len(base64.b64decode(secretkeys.generate(32, "base64"))) == 32
    padded = secretkeys.generate(32, "base64url") + "="
    assert len(base64.urlsafe_b64decode(padded)) == 32
    assert len(bytes.fromhex(secretkeys.generate(16, "hex"))) == 16


@pytest.mark.parametrize("kind", ["hex", "base64", "base64url"])
def test_every_format_refuses_fewer_than_eight_bytes(kind: str) -> None:
    with pytest.raises(secretkeys.KeyProblem, match="at least 8"):
        secretkeys.generate(7, kind)
    assert secretkeys.generate(8, kind)


def test_values_differ_between_calls() -> None:
    for kind in secretkeys.FORMATS:
        assert secretkeys.generate(16, kind) != secretkeys.generate(16, kind)


def test_shapes_name_the_format_the_bytes_and_the_length() -> None:
    assert secretkeys.shape("hex", 32) == "hex, 32 bytes, 64 characters"
    assert secretkeys.shape("base64", 33) == "base64, 33 bytes, 44 characters"
    assert secretkeys.shape("base64url", 32) == "base64url, 32 bytes, 43 characters"
    assert secretkeys.shape("garage-key-id", None) == "garage-key-id, GK and 24 hex characters"


def test_every_builtin_generated_entry_declares_a_format() -> None:
    entries = [entry for module in load_modules().values() for entry in module.generated]
    assert entries
    assert all(entry["format"] in (*secretkeys.FORMATS, "copy") for entry in entries)


def test_a_name_declared_by_several_modules_has_one_shape_everywhere() -> None:
    shapes: dict[str, set[tuple[str, int | None]]] = {}
    for module in load_modules().values():
        for entry in module.generated:
            for env in entry["env"]:
                shapes.setdefault(env, set()).add((entry["format"], entry.get("bytes")))
    assert {env: found for env, found in shapes.items() if len(found) > 1} == {}
    assert len(shapes["AYE_ONCALL_WEBHOOK_SECRET"]) == 1


def generated_spec() -> dict[str, Any]:
    return copy.deepcopy(load_modules()["status"].spec)


@pytest.mark.parametrize(
    "entry",
    [
        {"env": ["AYE_X"]},
        {"env": ["AYE_X"], "format": "hex"},
        {"env": ["AYE_X"], "format": "hex", "bytes": 4},
        {"env": ["AYE_X"], "format": "hex", "bytes": 65},
        {"env": ["AYE_X"], "format": "base32", "bytes": 16},
        {"env": ["AYE_X"], "format": "hex", "bytes": 16, "from": "AYE_Y on central"},
        {"env": ["AYE_X"], "format": "garage-key-id", "bytes": 12},
        {"env": ["AYE_X"], "format": "garage-key-id", "from": "AYE_Y on central"},
        {"env": ["AYE_X"], "format": "copy"},
        {"env": ["AYE_X"], "format": "copy", "from": "AYE_Y on central", "bytes": 16},
    ],
)
def test_the_module_schema_refuses_a_generated_entry_without_a_clear_shape(
    entry: dict[str, Any],
) -> None:
    spec = generated_spec()
    spec["generated"] = [entry]
    problems = spec_problems("modules.status", spec)
    assert problems
    assert all(path.startswith("modules.status.generated.0") for path, _, _ in problems)
    assert {rule for _, rule, _ in problems} == {"module-spec"}


@pytest.mark.parametrize(
    "entry",
    [
        {"env": ["AYE_X"], "format": "base64url", "bytes": 32},
        {"env": ["AYE_X"], "format": "garage-key-id"},
        {"env": ["AYE_X"], "format": "copy", "from": "AYE_Y on central"},
    ],
)
def test_the_module_schema_takes_each_shape(entry: dict[str, Any]) -> None:
    spec = generated_spec()
    spec["generated"] = [entry]
    assert spec_problems("modules.status", spec) == []


def test_the_catalog_knows_every_built_in_name_and_its_module() -> None:
    catalog = catalog_generated(load_modules())
    rpc = catalog["AYE_OBJECTSTORE_RPC_SECRET"]
    assert (rpc.module, rpc.format, rpc.size) == ("objectstore", "hex", 32)
    assert catalog["AYE_OFFSITE_ACCESS_KEY_ID"].copied
    assert not rpc.copied


def test_a_host_lists_each_name_once_sorted_with_its_rendered_source() -> None:
    manifest = manifest_from(example_data())
    central = host_generated(manifest, load_modules(), "central")
    names = [item.env for item in central]
    assert names == sorted(set(names))
    assert "AYE_ONCALL_WEBHOOK_SECRET" in names
    assert "AYE_OBJECTSTORE_RPC_SECRET" in names
    assert "AYE_RESTSTORE_METRICS_PASSWORD" not in names
    edge = {item.env: item for item in host_generated(manifest, load_modules(), "edge")}
    assert edge["AYE_OFFSITE_REST_PASSWORD"].source == (
        "AYE_RESTSTORE_EDGE_PASSWORD on central, when it runs rest-server"
    )
    assert edge["AYE_OFFSITE_RESTIC_PASSWORD"].shape() == "base64, 33 bytes, 44 characters"


@pytest.mark.parametrize(
    ("key", "pattern", "length"),
    [
        ("AYE_OBJECTSTORE_RPC_SECRET", "hex", 64),
        ("AYE_SECRETS_ENCRYPTION_KEY", "hex", 32),
        ("AYE_OBJECTSTORE_ADMIN_TOKEN", "base64", 44),
        ("AYE_AUTH_COOKIE_SECRET", "base64url", 43),
        ("AYE_OBJECTSTORE_ACCESS_KEY_ID", "garage-key-id", 26),
    ],
)
def test_gen_of_a_catalog_name_takes_its_format_without_flags(
    store: Store, capsys: pytest.CaptureFixture[str], key: str, pattern: str, length: int
) -> None:
    assert main(["secrets", "gen", key]) == 0
    value = store.folders[FOLDER][key]
    assert len(value) == length
    assert ALPHABETS[pattern].match(value)
    output = printed(capsys)
    assert value not in output
    assert output == (
        f"secrets: {key} in prod:/web: created, {length} characters, sha256 {fingerprint(value)}\n"
    )


def test_gen_of_a_catalog_name_accepts_flags_that_agree(store: Store) -> None:
    command = ["secrets", "gen", "AYE_ERRORS_DB_PASSWORD", "--bytes", "24", "--format", "hex"]
    assert main(command) == 0
    assert len(store.folders[FOLDER]["AYE_ERRORS_DB_PASSWORD"]) == 48


@pytest.mark.parametrize(
    ("flags", "named"),
    [
        (["--bytes", "16"], "drop --bytes"),
        (["--format", "base64url"], "drop --format"),
    ],
)
def test_gen_refuses_flags_that_contradict_the_catalog(
    store: Store, capsys: pytest.CaptureFixture[str], flags: list[str], named: str
) -> None:
    assert main(["secrets", "gen", "AYE_OBJECTSTORE_RPC_SECRET", *flags]) == 1
    assert store.folders == {}
    assert store.requests == []
    error = printed(capsys)
    assert "AYE_OBJECTSTORE_RPC_SECRET is hex, 32 bytes, 64 characters in the objectstore" in error
    assert named in error


def test_gen_refuses_bytes_for_a_garage_key_id(store: Store) -> None:
    assert main(["secrets", "gen", "AYE_OBJECTSTORE_ACCESS_KEY_ID", "--bytes", "12"]) == 1
    assert store.folders == {}


def test_gen_refuses_a_name_that_is_copied_from_another_host(
    store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["secrets", "gen", "AYE_OFFSITE_ACCESS_KEY_ID"]) == 1
    assert store.requests == []
    error = printed(capsys)
    assert "copied from another AyeOps host (offsite module), never generated" in error


def test_gen_of_another_name_takes_the_format_and_bytes_it_is_given(store: Store) -> None:
    assert main(["secrets", "gen", "MY_TOKEN", "--format", "hex", "--bytes", "16"]) == 0
    value = store.folders[FOLDER]["MY_TOKEN"]
    assert ALPHABETS["hex"].match(value)
    assert len(value) == 32


def test_gen_of_another_name_defaults_to_url_safe_base64_of_24_bytes(store: Store) -> None:
    assert main(["secrets", "gen", "MY_TOKEN"]) == 0
    value = store.folders[FOLDER]["MY_TOKEN"]
    assert ALPHABETS["base64url"].match(value)
    assert len(value) == 32


def test_gen_of_a_catalog_name_never_overwrites(store: Store) -> None:
    store.folders[FOLDER] = {"AYE_STATUS_PUSH_SECRET": "kept-value"}
    assert main(["secrets", "gen", "AYE_STATUS_PUSH_SECRET"]) == 1
    assert store.folders[FOLDER] == {"AYE_STATUS_PUSH_SECRET": "kept-value"}


def test_gen_of_an_existing_key_points_to_set_replace_not_to_a_flag_of_its_own(
    store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    store.folders[FOLDER] = {"MY_TOKEN": "kept-value"}
    assert main(["secrets", "gen", "MY_TOKEN"]) == 1
    assert store.folders[FOLDER] == {"MY_TOKEN": "kept-value"}
    assert printed(capsys) == (
        "secrets: MY_TOKEN already exists in prod:/web, gen never overwrites;"
        " aye secrets set MY_TOKEN --replace changes it\n"
    )


def host_gen(host: str, manifest: Path = EXAMPLE) -> list[str]:
    return ["secrets", "gen", "--manifest", str(manifest), "--host", host]


def test_gen_for_a_host_creates_every_missing_name_in_its_shape_in_one_batch(
    store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    store.folders[FOLDER] = {"AYE_GRAFANA_ADMIN_USER": "kept-admin"}
    assert main(host_gen("central")) == 0
    folder = store.folders[FOLDER]
    wanted = host_generated(manifest_from(example_data()), load_modules(), "central")
    assert sorted(folder) == [item.env for item in wanted]
    assert folder["AYE_GRAFANA_ADMIN_USER"] == "kept-admin"
    for item in wanted:
        if item.env == "AYE_GRAFANA_ADMIN_USER":
            continue
        value = folder[item.env]
        assert len(value) == secretkeys.length(item.format, item.size or 0), item.env
        assert ALPHABETS[item.format].match(value), item.env
    assert len(batches(store)) == 1
    output = printed(capsys)
    assert not any(value in output for value in folder.values())
    lines = output.splitlines()
    made = len(wanted) - 1
    assert len(lines) == made + 1
    assert lines[-1] == f"secrets: prod:/web: generated {made}, kept 1 existing value(s)"
    rpc = folder["AYE_OBJECTSTORE_RPC_SECRET"]
    assert (
        "secrets: AYE_OBJECTSTORE_RPC_SECRET in prod:/web: created, 64 characters,"
        f" sha256 {fingerprint(rpc)}"
    ) in lines


def test_gen_for_a_host_with_nothing_missing_writes_nothing(
    store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(host_gen("central")) == 0
    before = dict(store.folders[FOLDER])
    capsys.readouterr()
    store.requests.clear()
    assert main(host_gen("central")) == 0
    assert store.folders[FOLDER] == before
    assert batches(store) == []
    writes = [path for method, path, _ in store.requests if method != "GET"]
    assert writes == ["/api/v1/auth/universal-auth/login"]
    summary = f"secrets: prod:/web: generated 0, kept {len(before)} existing value(s)\n"
    assert printed(capsys) == summary


def test_gen_for_a_host_names_the_copies_it_leaves_to_the_owner(
    store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    store.folders[FOLDER] = {"AYE_OFFSITE_ACCESS_KEY_ID": "GKcopied"}
    assert main(host_gen("edge")) == 0
    folder = store.folders[FOLDER]
    assert sorted(folder) == ["AYE_OFFSITE_ACCESS_KEY_ID", "AYE_OFFSITE_RESTIC_PASSWORD"]
    assert folder["AYE_OFFSITE_ACCESS_KEY_ID"] == "GKcopied"
    restic = folder["AYE_OFFSITE_RESTIC_PASSWORD"]
    assert printed(capsys).splitlines() == [
        f"secrets: AYE_OFFSITE_RESTIC_PASSWORD in prod:/web: created, 44 characters,"
        f" sha256 {fingerprint(restic)}",
        "secrets: prod:/web: generated 1, kept 1 existing value(s)",
        "secrets: AYE_OFFSITE_REST_PASSWORD in prod:/web: missing, copy"
        " AYE_RESTSTORE_EDGE_PASSWORD on central, when it runs rest-server",
        "secrets: AYE_OFFSITE_SECRET_ACCESS_KEY in prod:/web: missing, copy"
        " AYE_OBJECTSTORE_SECRET_ACCESS_KEY on central, when it runs Garage",
    ]


def test_gen_for_a_host_creates_a_missing_folder_first(store: Store) -> None:
    assert main([*host_gen("edge"), "--folder", "/edge/app"]) == 0
    assert "AYE_OFFSITE_RESTIC_PASSWORD" in store.folders[("prod", "/edge/app")]


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        (["AYE_X"], "drop KEY, --format and --bytes"),
        (["--bytes", "16"], "drop KEY, --format and --bytes"),
        (["--format", "hex"], "drop KEY, --format and --bytes"),
    ],
)
def test_gen_for_a_host_takes_no_key_or_shape_flags(
    store: Store, capsys: pytest.CaptureFixture[str], extra: list[str], message: str
) -> None:
    assert main([*host_gen("central"), *extra]) == 1
    assert store.requests == []
    assert message in printed(capsys)


@pytest.mark.parametrize(
    ("command", "message"),
    [
        (["secrets", "gen", "--manifest", str(EXAMPLE)], "--host must name a host of"),
        (host_gen("nowhere"), "--host must name a host of"),
        (["secrets", "gen"], "gen needs a KEY, or a manifest for its secrets; no manifest"),
        (["secrets", "gen", "AYE_X", "--host", "central"], "--host goes with --manifest"),
    ],
)
def test_gen_usage_errors_touch_nothing(
    store: Store, capsys: pytest.CaptureFixture[str], command: list[str], message: str
) -> None:
    assert main(command) == 1
    assert store.requests == []
    error = printed(capsys)
    assert message in error


def test_the_unknown_host_error_lists_the_hosts(
    store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(host_gen("nowhere")) == 1
    assert printed(capsys).endswith(": central, edge\n")


def test_gen_for_a_host_of_a_manifest_with_errors_stores_nothing(
    store: Store, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    broken = tmp_path / "broken.yaml"
    text = EXAMPLE.read_text(encoding="utf-8").replace("quota: 40g", "quota: lots", 1)
    broken.write_text(text, encoding="utf-8")
    assert main(host_gen("central", broken)) == 1
    assert store.requests == []
    assert "has errors, run aye check first" in printed(capsys)


def test_gen_for_a_host_of_a_manifest_that_is_not_yaml_stores_nothing(
    store: Store, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    broken = tmp_path / "broken.yaml"
    broken.write_text("hosts: [unclosed\n", encoding="utf-8")
    assert main(host_gen("central", broken)) == 1
    assert store.requests == []
    assert "is not valid YAML, run aye check first" in printed(capsys)


def test_gen_for_a_host_of_a_missing_manifest_stores_nothing(
    store: Store, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(host_gen("central", tmp_path / "absent.yaml")) == 1
    assert store.requests == []
    assert printed(capsys).startswith("secrets: ")
