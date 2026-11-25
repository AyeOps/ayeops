from __future__ import annotations

import json
import posixpath
import re
import shlex
from typing import Any

from ayeops.checks_sql import SQL_END, sql_query_problem, sql_text
from ayeops.manifest import Manifest
from ayeops.registry import load_modules

S3_PORT = 3900
REST_PORT = 8000
REST_SERVER = "rest-server"
METRICS_USER = "metrics"
PROVIDER_PORTS = {"garage": S3_PORT, REST_SERVER: REST_PORT}
PROVIDER_ONLY = {"garage": ("bucket", "region", "meta_dir"), REST_SERVER: ("prune_schedule",)}
GLOB = re.compile(r"[*?\[]")
QUOTA = re.compile(r"^([0-9]+)([mgt])$")
QUOTA_FACTORS = {"m": 1024**2, "g": 1024**3, "t": 1024**4}
DRILL_DEFAULTS: dict[str, Any] = {
    "every_days": 7,
    "schedule": "13:00",
    "max_minutes": 60,
    "jobs": 2,
}
DRILL_CHECK_SECONDS = 300
DAY_MINUTES = 1440


def quota_bytes(text: str | None) -> int | None:
    if not text:
        return None
    match = QUOTA.match(str(text).strip().lower())
    if match is None:
        raise ValueError(f"not a quota: {text!r}")
    return int(match.group(1)) * QUOTA_FACTORS[match.group(2)]


def objectstore_settings(manifest: Manifest, host: str) -> dict[str, Any] | None:
    if host not in manifest.hosts:
        return None
    modules = manifest.host_modules(host)
    if "objectstore" not in modules:
        return None
    return load_modules()["objectstore"].settings(modules["objectstore"])


def store_port(store: dict[str, Any]) -> int:
    return PROVIDER_PORTS[str(store.get("provider") or "garage")]


def store_service(manifest: Manifest, central: str) -> str:
    store = objectstore_settings(manifest, central)
    return REST_SERVER if store is not None and store["provider"] == REST_SERVER else "garage"


def secret_env(host: str, suffix: str) -> str:
    return f"AYE_RESTSTORE_{host.upper().replace('-', '_')}_{suffix}"


def store_clients(manifest: Manifest, central: str) -> list[tuple[str, dict[str, Any]]]:
    clients = []
    for host in sorted(manifest.hosts):
        settings = manifest.host_modules(host).get("offsite")
        if settings is None or host == central:
            continue
        merged = load_modules()["offsite"].settings(settings)
        if (merged.get("target") or {}).get("host") == central:
            clients.append((host, merged))
    return clients


def reststore_context(
    manifest: Manifest, host: str, settings: dict[str, Any] | None
) -> dict[str, Any]:
    if settings is None or settings.get("provider") != REST_SERVER:
        garage = settings is not None
        return {"enabled": False, "garage": garage, "repos": [], "repos_json": "[]", "start": ""}
    repos = [
        {
            "name": client,
            "keep_daily": int(merged["keep_daily"]),
            "keep_weekly": int(merged["keep_weekly"]),
            "keep_monthly": int(merged["keep_monthly"]),
            "password_env": secret_env(client, "RESTIC_PASSWORD"),
            "user_env": secret_env(client, "PASSWORD"),
        }
        for client, merged in store_clients(manifest, host)
    ]
    retention = [{key: repo[key] for key in repo if key != "user_env"} for repo in repos]
    return {
        "enabled": True,
        "garage": False,
        "repos": repos,
        "repos_json": json.dumps(retention, separators=(",", ":")),
        "start": rest_server_script(
            [(str(repo["name"]), str(repo["user_env"])) for repo in repos],
            quota_bytes(settings.get("quota")),
        ),
    }


def drill_settings(merged: dict[str, Any]) -> dict[str, Any] | None:
    raw = merged.get("restore_drill")
    if not raw:
        return None
    return {**DRILL_DEFAULTS, **raw}


def minute_of_day(text: str) -> int:
    hours, _, minutes = str(text).partition(":")
    return int(hours) * 60 + int(minutes)


def store_drills(manifest: Manifest, central: str) -> list[tuple[str, dict[str, Any]]]:
    return [
        (client, drill)
        for client, merged in store_clients(manifest, central)
        for drill in [drill_settings(merged)]
        if drill is not None
    ]


def drill_lines(client: str, drill: dict[str, Any]) -> str:
    words = [
        "drill",
        client,
        str(drill["every_days"]),
        str(minute_of_day(drill["schedule"]) * 60),
        str(int(drill["max_minutes"]) * 60),
        str(drill["jobs"]),
        secret_env(client, "RESTIC_PASSWORD"),
    ]
    text = " ".join(shlex.quote(word) for word in words) + "\n"
    for check in drill.get("checks") or []:
        head = " ".join(shlex.quote(word) for word in ("check", client, str(check["name"])))
        body = sql_text(str(check["query"]), None, DRILL_CHECK_SECONDS)
        text += f"{head} <<'{SQL_END}'\n{body}{SQL_END}\n"
    return text


def drill_context(
    manifest: Manifest, host: str, settings: dict[str, Any] | None
) -> dict[str, Any] | None:
    if settings is None or settings.get("provider") != REST_SERVER:
        return None
    drills = store_drills(manifest, host)
    if not drills:
        return None
    return {
        "engine": str(drills[0][1]["engine"]),
        "version": max(int(drill["version"]) for _, drill in drills),
        "clients": [
            {"name": client, "password_env": secret_env(client, "RESTIC_PASSWORD")}
            for client, _ in drills
        ],
        "config": "".join(drill_lines(client, drill) for client, drill in drills),
    }


def drill_problems(
    manifest: Manifest, host: str, merged: dict[str, Any], path: str
) -> list[tuple[str, str, str]]:
    drill = drill_settings(merged)
    if drill is None:
        return []
    where = f"{path}.restore_drill"
    problems: list[tuple[str, str, str]] = []
    if merged.get("verify") != "pg_dump":
        problems.append(
            (where, "offsite-drill", "a postgres drill restores pg_dump custom-format files only")
        )
    names = [str(check["name"]) for check in drill.get("checks") or []]
    for name in sorted({name for name in names if names.count(name) > 1}):
        problems.append((where, "offsite-drill", f"the check name {name!r} is used twice"))
    for index, check in enumerate(drill.get("checks") or []):
        problem = sql_query_problem(str(check.get("query") or ""))
        if problem:
            problems.append((f"{where}.checks.{index}.query", "offsite-drill", problem))
    central = str((merged.get("target") or {}).get("host") or "")
    store = objectstore_settings(manifest, central) if central and central != host else None
    if store is None or store["provider"] != REST_SERVER:
        problems.append(
            (
                where,
                "offsite-drill",
                "the drill runs on the store host and reads the repository from its disk,"
                " read-only: it needs target.host with an objectstore of provider rest-server",
            )
        )
        return problems
    if "node" not in manifest.host_modules(central):
        problems.append((where, "offsite-drill", f"the drill reports through node on {central}"))
    versions = {int(other["version"]) for _, other in store_drills(manifest, central)}
    if len(versions) > 1:
        problems.append(
            (
                f"{where}.version",
                "offsite-drill",
                f"the drills on {central} share one PostgreSQL container: use one version,"
                f" not {sorted(versions)}",
            )
        )
    start = minute_of_day(drill["schedule"])
    prune = minute_of_day(store["prune_schedule"])
    if (prune - start) % DAY_MINUTES <= int(drill["max_minutes"]):
        problems.append(
            (
                f"{where}.schedule",
                "offsite-drill",
                f"the prune on {central} at {store['prune_schedule']} UTC falls into the drill"
                f" from {drill['schedule']} UTC for {drill['max_minutes']} minutes:"
                " a prune deletes packs the drill may still read",
            )
        )
    return problems


def rest_server_script(users: list[tuple[str, str]], max_size: int | None) -> str:
    htpasswd = "/run/rest-server/htpasswd"
    lines = ["set -eu", "umask 077", f": > {htpasswd}"]
    for user, variable in [(METRICS_USER, "AYE_RESTSTORE_METRICS_PASSWORD"), *users]:
        lines.append(f'printf "%s" "${variable}" | htpasswd -B -i {htpasswd} {user}')
    command = [
        "exec rest-server --path /data --listen :8000",
        f"--htpasswd-file {htpasswd}",
        "--append-only --private-repos --prometheus",
    ]
    if max_size:
        command.append(f"--max-size {max_size}")
    lines.append(" ".join(command))
    return "\n".join(lines) + "\n"


def source_dir(source: str) -> str:
    return posixpath.dirname(source) or "/"


def offsite_context(
    manifest: Manifest, host: str, settings: dict[str, Any] | None
) -> dict[str, Any]:
    if settings is None:
        return {}
    directory = source_dir(str(settings["source"]))
    target = settings["target"]
    base = {"source_dir": directory, "append_only": False, "rest_user": ""}
    if "url" in target:
        return {
            **base,
            "repository": str(target["url"]),
            "region": str(settings.get("region") or ""),
            "quota_bytes": quota_bytes(settings.get("quota")),
            "append_only": bool(target.get("append_only")),
        }
    central = str(target["host"])
    store = objectstore_settings(manifest, central)
    if store is None:
        return {**base, "repository": "", "region": "", "quota_bytes": None}
    address = manifest.hosts[central].get("address", central)
    if store["provider"] == REST_SERVER:
        return {
            **base,
            "repository": f"rest:http://{address}:{REST_PORT}/{host}/",
            "region": "",
            "quota_bytes": None,
            "append_only": True,
            "rest_user": host,
        }
    return {
        **base,
        "repository": f"s3:http://{address}:{S3_PORT}/{store['bucket']}/{host}",
        "region": str(store["region"]),
        "quota_bytes": None,
    }


def offsite_problems(manifest: Manifest) -> list[tuple[str, str, str]]:
    problems: list[tuple[str, str, str]] = []
    for host in sorted(manifest.hosts):
        settings = manifest.host_modules(host).get("offsite")
        if settings is None:
            continue
        path = f"hosts.{host}.modules.offsite"
        merged = load_modules()["offsite"].settings(settings)
        if not any(int(merged[key]) > 0 for key in ("keep_daily", "keep_weekly", "keep_monthly")):
            problems.append(
                (path, "offsite-retention", "keep at least one snapshot: a keep_* above 0")
            )
        directory = source_dir(str(merged["source"]))
        if GLOB.search(directory) or directory == "/":
            problems.append(
                (
                    f"{path}.source",
                    "offsite-source",
                    f"the directory {directory!r} is mounted read-only into the agent:"
                    " name a real directory, the wildcard goes into the file name only",
                )
            )
        problems += drill_problems(manifest, host, merged, path)
        target = merged.get("target") or {}
        if "host" in target:
            problems += target_problems(manifest, host, str(target["host"]), f"{path}.target")
            if merged.get("quota") or merged.get("region"):
                problems.append(
                    (
                        path,
                        "offsite-target",
                        f"quota and region come from the objectstore on {target['host']}:"
                        " set them only with a url target",
                    )
                )
    return problems


def target_problems(
    manifest: Manifest, host: str, central: str, path: str
) -> list[tuple[str, str, str]]:
    if central == host:
        return [(path, "offsite-target", "an off-site copy on the same host is not off-site")]
    if central not in manifest.hosts:
        return [(path, "offsite-target", f"unknown host {central!r}")]
    store = objectstore_settings(manifest, central)
    if store is None:
        return [(path, "offsite-target", f"{central} runs no objectstore")]
    problems = []
    if zone := manifest.shared_zone(host, central):
        problems.append(
            (
                path,
                "offsite-target",
                f"{host} and {central} are both in zone {zone!r}: a copy there is not off-site",
            )
        )
    port = store_port(store)
    if port not in (store.get("expose") or []):
        problems.append((path, "offsite-target", f"{central} does not expose {port}"))
    if store["provider"] == REST_SERVER and host == METRICS_USER:
        problems.append(
            (path, "offsite-target", f"the host name {host!r} is the rest-server metrics user")
        )
    if not manifest.hosts[central].get("address"):
        problems.append((path, "offsite-target", f"{central} has no address"))
    return problems


def objectstore_problems(manifest: Manifest) -> list[tuple[str, str, str]]:
    problems: list[tuple[str, str, str]] = []
    for host in sorted(manifest.hosts):
        raw = manifest.host_modules(host).get("objectstore")
        if raw is None:
            continue
        path = f"hosts.{host}.modules.objectstore"
        provider = str(raw.get("provider") or "garage")
        for other, keys in PROVIDER_ONLY.items():
            if other == provider:
                continue
            problems += [
                (f"{path}.{key}", "objectstore-provider", f"{key} belongs to {other}")
                for key in keys
                if key in raw
            ]
        port = PROVIDER_PORTS[provider]
        problems += [
            (f"{path}.expose", "objectstore-provider", f"{provider} listens on {port}, not {item}")
            for item in raw.get("expose") or []
            if item != port
        ]
    return problems
