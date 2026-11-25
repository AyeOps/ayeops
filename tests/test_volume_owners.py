from __future__ import annotations

from pathlib import Path

import yaml

RENDERED = Path(__file__).parents[1] / "examples" / "rendered"
WRITABLE_IN_EVERY_IMAGE = {"/tmp"}


def read_only(rest: str) -> bool:
    return "ro" in rest.split(":")[1:]


def non_root_named_mounts() -> list[tuple[str, str, str]]:
    found = []
    for path in sorted(RENDERED.glob("*/compose.yaml")):
        compose = yaml.safe_load(path.read_text(encoding="utf-8"))
        named = set(compose.get("volumes") or {})
        for name, service in (compose.get("services") or {}).items():
            user = str(service.get("user", ""))
            if not user or user.split(":")[0] in {"0", "root"}:
                continue
            for volume in service.get("volumes") or []:
                source, _, rest = str(volume).partition(":")
                if source in named and not read_only(rest):
                    found.append((f"{path.parent.name}/{name}", user, rest.split(":")[0]))
    return found


def test_the_example_has_a_non_root_service_with_a_named_volume() -> None:
    assert non_root_named_mounts()


def test_a_non_root_service_mounts_named_volumes_where_it_can_write() -> None:
    wrong = [m for m in non_root_named_mounts() if m[2] not in WRITABLE_IN_EVERY_IMAGE]
    assert not wrong, (
        "a new named volume takes its owner from the image directory it is mounted on, "
        f"so a non-root service cannot write a volume mounted elsewhere: {wrong}"
    )


def test_a_read_only_mount_does_not_need_to_own_its_volume() -> None:
    assert read_only("/run/grafana-reader:ro")
    assert not read_only("/tmp")
    assert not read_only("/tmp:rw")
