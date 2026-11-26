from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import yaml

from ayeops.manifest import Manifest
from ayeops.mcp_catalog import CATALOG_FILE, catalog_json
from ayeops.registry import Module, load_modules
from ayeops.yamlkeys import StrictYAMLError, load_strict

if TYPE_CHECKING:
    from ayeops.render import FleetRender

IMPLEMENTED = "agentgateway"
HUB_PORT = 3000
GRAFANA_URL = "http://grafana:3000"
METRICS_URL = "http://victoriametrics:8428"
GARAGE_ENDPOINT = "http://garage:3900"
GRAFANA_TARGET = "http://mcp-grafana:8000/mcp"
METRICS_TARGET = "http://mcp-victoriametrics:8080/mcp"
CATALOG_TARGET = "http://mcp-catalog:8000/mcp"
GRAFANA_SERVER_TOKEN = "AYE_MCP_GRAFANA_SERVER_TOKEN"
S3_DIR = "/opt/aye/s3"
OPENAPI_DIR = "openapi"
CONFIG_FILE = "mcp/agentgateway.yaml"
SINGLE = ("grafana", "victoriametrics")
ONE_PER_HOST = (*SINGLE, "catalog")
CATALOG_TOOLS = ("list_hosts", "list_services", "get_service", "list_alerts", "get_runbook")

S3_READS = (
    "s3_list_buckets",
    "s3_list_objects",
    "s3_get_object_metadata",
    "s3_get_object",
    "s3_list_connections",
)
S3_WRITES = ("s3_put_object", "s3_delete_object", "s3_copy_object", "s3_presign_url")
S3_DEFAULT = ("s3_list_buckets", "s3_list_objects", "s3_get_object_metadata")
S3_UNLOCKS = {"s3_put_object", "s3_delete_object", "s3_copy_object"}
READ_METHODS = {"get", "head", "options"}
HTTP_METHODS = ("get", "head", "options", "put", "post", "patch", "delete", "trace")

COMMON_KEYS = {"kind", "description"}
KIND_KEYS = {
    "grafana": {"tools", "allow_write"},
    "victoriametrics": {"url", "tools"},
    "s3": {"endpoint", "region", "path_style", "tools", "allow_write"},
    "mcp": {"url", "transport", "token", "image", "command", "tools", "allow_write"},
    "openapi": {"spec", "url", "token", "tools", "allow_write"},
    "cli": {"image", "binary", "commands"},
    "catalog": {"tools"},
}
KIND_REQUIRED = {"openapi": ("spec", "url"), "cli": ("image", "binary", "commands")}
NOT_IMPLEMENTED = {
    "contextforge": "its tools live in a database; the rendered import is in docs/mcp.md",
    "docker": "its catalog without the Docker socket is designed in docs/mcp.md",
    "microsoft": "it needs Kubernetes and Entra ID and has no arm64 image",
}
CANNOT_SERVE = {
    "contextforge": {"openapi": "ContextForge imports no OpenAPI spec as tools"},
    "docker": {
        "openapi": "the Docker MCP Gateway has no OpenAPI support",
        "cli": "the Docker MCP Gateway runs commands only through the Docker socket",
        "stdio": "the Docker MCP Gateway starts stdio servers only through the Docker socket",
    },
    "microsoft": {
        "openapi": "the Microsoft MCP Gateway has no OpenAPI support",
        "cli": "the Microsoft MCP Gateway has no command tools",
    },
}
KIND_NOT_YET = {
    "cli": "the command adapter is designed in docs/mcp.md and not implemented yet",
    "stdio": "an MCP server from an image is not implemented yet; give it a url",
}

Problem = tuple[str, str, str]


class EnvRef(str):
    __slots__ = ()


class ConfigDumper(yaml.SafeDumper):
    def determine_block_hints(self, text: str) -> str:
        hints = super().determine_block_hints(text)
        return hints if hints[:1].isdigit() else f"{self.best_indent}{hints}"


def _literal(dumper: yaml.SafeDumper, value: EnvRef) -> yaml.ScalarNode:
    return dumper.represent_scalar("tag:yaml.org,2002:str", str(value), style="|")


ConfigDumper.add_representer(EnvRef, _literal)


def env_name(source: str, suffix: str) -> str:
    return f"AYE_MCP_{source.upper()}_{suffix}"


def env_ref(name: str) -> EnvRef:
    return EnvRef(f"${name}")


def mcp_settings(manifest: Manifest, host: str) -> dict[str, Any] | None:
    raw = manifest.host_modules(host).get("mcp")
    if raw is None:
        return None
    return load_modules()["mcp"].settings(raw)


def host_has(manifest: Manifest, host: str, module: str) -> dict[str, Any] | None:
    raw = manifest.host_modules(host).get(module)
    if raw is None:
        return None
    return load_modules()[module].settings(raw)


def kind_of(source: dict[str, Any]) -> str:
    if source["kind"] == "mcp" and "url" not in source and "image" in source:
        return "stdio"
    return str(source["kind"])


def garage_region(manifest: Manifest, host: str) -> str | None:
    store = host_has(manifest, host, "objectstore")
    if store is None or store.get("provider") != "garage":
        return None
    return str(store["region"])


def default_url(manifest: Manifest, host: str, source: dict[str, Any]) -> str | None:
    kind = source["kind"]
    if kind == "grafana":
        return GRAFANA_URL if host_has(manifest, host, "grafana") is not None else None
    if source.get("url"):
        return str(source["url"])
    metrics = host_has(manifest, host, "metrics")
    if kind == "victoriametrics" and metrics is not None and metrics.get("role") == "server":
        return METRICS_URL
    return None


def s3_endpoint(manifest: Manifest, host: str, source: dict[str, Any]) -> tuple[str, str] | None:
    if source.get("endpoint"):
        return str(source["endpoint"]), str(source.get("region") or "us-east-1")
    region = garage_region(manifest, host)
    if region is None:
        return None
    return GARAGE_ENDPOINT, str(source.get("region") or region)


def openapi_operations(manifest: Manifest, spec: str) -> tuple[dict[str, str], str | None]:
    path = manifest.base_dir / spec
    if not path.is_file():
        return {}, f"{spec} does not exist next to the manifest"
    try:
        data = load_strict(path.read_text(encoding="utf-8"), spec, key_types=False)
    except StrictYAMLError as error:
        return {}, error.problems[0][2]
    except yaml.YAMLError as error:
        return {}, f"{spec} is not JSON or YAML: {error.__class__.__name__}"
    if not isinstance(data, dict) or not isinstance(data.get("paths"), dict):
        return {}, f"{spec} has no paths"
    operations: dict[str, str] = {}
    for route, item in sorted(data["paths"].items()):
        for method in HTTP_METHODS:
            operation = item.get(method) if isinstance(item, dict) else None
            if operation is None:
                continue
            operation_id = operation.get("operationId") if isinstance(operation, dict) else None
            if not operation_id:
                return {}, f"{method.upper()} {route} in {spec} has no operationId"
            operations[str(operation_id)] = method
    return operations, None


def openapi_spec(manifest: Manifest, spec: str) -> Any:
    return yaml.safe_load((manifest.base_dir / spec).read_text(encoding="utf-8"))


def write_tools(manifest: Manifest, source: dict[str, Any]) -> set[str] | None:
    kind = source["kind"]
    if kind == "s3":
        return set(S3_WRITES)
    if kind == "openapi":
        operations, _ = openapi_operations(manifest, str(source["spec"]))
        return {name for name, method in operations.items() if method not in READ_METHODS}
    if kind in ("victoriametrics", "catalog"):
        return set()
    return None


def known_tools(manifest: Manifest, source: dict[str, Any]) -> set[str] | None:
    kind = source["kind"]
    if kind == "s3":
        return {*S3_READS, *S3_WRITES}
    if kind == "openapi":
        operations, _ = openapi_operations(manifest, str(source["spec"]))
        return set(operations)
    if kind == "catalog":
        return set(CATALOG_TOOLS)
    return None


def allowed_tools(manifest: Manifest, source: dict[str, Any]) -> list[str] | None:
    writes = list(source.get("allow_write") or [])
    tools = source.get("tools")
    kind = source["kind"]
    if tools is None:
        if kind in SINGLE:
            return None
        if kind == "s3":
            tools = list(S3_DEFAULT)
        elif kind == "openapi":
            operations, _ = openapi_operations(manifest, str(source["spec"]))
            tools = [name for name, method in operations.items() if method in READ_METHODS]
        elif kind == "catalog":
            tools = list(CATALOG_TOOLS)
        else:
            tools = []
    return sorted({*tools, *writes})


def allow_rule(name: str, tools: list[str] | None) -> dict[str, str]:
    rule = f"mcp.tool.target == {json.dumps(name)}"
    if tools is not None:
        rule += f" && mcp.tool.name in {json.dumps(tools)}"
    return {"allow": rule}


def bearer(source_env: str) -> dict[str, Any]:
    return {"backendAuth": {"key": {"value": env_ref(source_env)}}}


def s3_target(manifest: Manifest, host: str, name: str, source: dict[str, Any]) -> dict[str, Any]:
    endpoint, region = s3_endpoint(manifest, host, source) or (GARAGE_ENDPOINT, "garage")
    readonly = not S3_UNLOCKS & set(source.get("allow_write") or [])
    return {
        "cmd": f"{S3_DIR}/mcp-s3",
        "args": [],
        "clear_env": True,
        "env": {
            "AWS_ACCESS_KEY_ID": env_ref(env_name(name, "ACCESS_KEY_ID")),
            "AWS_SECRET_ACCESS_KEY": env_ref(env_name(name, "SECRET_ACCESS_KEY")),
            "AWS_REGION": region,
            "AWS_EC2_METADATA_DISABLED": "true",
            "S3_ENDPOINT": endpoint,
            "S3_USE_PATH_STYLE": str(source.get("path_style", True)).lower(),
            "S3_CONNECTION_NAME": name,
            "MCP_S3_EXT_READONLY": str(readonly).lower(),
            "SSL_CERT_FILE": f"{S3_DIR}/ca-certificates.crt",
            "HOME": "/tmp",
        },
    }


def target(manifest: Manifest, host: str, name: str, source: dict[str, Any]) -> dict[str, Any]:
    kind = source["kind"]
    result: dict[str, Any] = {"name": name}
    if kind == "grafana":
        result["mcp"] = {"host": GRAFANA_TARGET}
        result["policies"] = bearer(GRAFANA_SERVER_TOKEN)
    elif kind == "victoriametrics":
        result["mcp"] = {"host": METRICS_TARGET}
    elif kind == "catalog":
        result["mcp"] = {"host": CATALOG_TARGET}
    elif kind == "s3":
        result["stdio"] = s3_target(manifest, host, name, source)
    elif kind == "openapi":
        result["openapi"] = {
            "schema": {"file": f"/config/{OPENAPI_DIR}/{name}.json"},
            "host": str(source["url"]),
        }
    else:
        transport = "sse" if source.get("transport") == "sse" else "mcp"
        result[transport] = {"host": str(source["url"])}
    if kind in ("mcp", "openapi") and source.get("token"):
        result["policies"] = bearer(env_name(name, "TOKEN"))
    return result


def agentgateway_config(manifest: Manifest, host: str, settings: dict[str, Any]) -> dict[str, Any]:
    sources = sorted(settings["sources"].items())
    return {
        "config": {"adminAddr": "off"},
        "mcp": {
            "prefixMode": "always",
            "failureMode": "failOpen",
            "targets": [target(manifest, host, name, source) for name, source in sources],
            "policies": {
                "timeout": {"requestTimeout": str(settings["timeout"])},
                "mcpAuthorization": {
                    "rules": [
                        allow_rule(name, allowed_tools(manifest, source))
                        for name, source in sources
                    ]
                },
            },
        },
    }


def dump_config(config: dict[str, Any]) -> str:
    return yaml.dump(
        config,
        Dumper=ConfigDumper,
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
        width=1000,
    )


def has_catalog(settings: dict[str, Any]) -> bool:
    return any(source["kind"] == "catalog" for source in settings["sources"].values())


def mcp_files(
    manifest: Manifest,
    host: str,
    modules: dict[str, Module] | None = None,
    renders: FleetRender | None = None,
) -> dict[str, str]:
    settings = mcp_settings(manifest, host)
    if settings is None or settings["provider"] != IMPLEMENTED:
        return {}
    files = {CONFIG_FILE: dump_config(agentgateway_config(manifest, host, settings))}
    if has_catalog(settings):
        files[CATALOG_FILE] = catalog_json(manifest, modules or load_modules(), renders)
    for name, source in sorted(settings["sources"].items()):
        if source["kind"] == "openapi":
            spec = openapi_spec(manifest, str(source["spec"]))
            files[f"mcp/{OPENAPI_DIR}/{name}.json"] = (
                json.dumps(spec, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
            )
    return files


def hub_secrets(settings: dict[str, Any]) -> list[str]:
    names: set[str] = set()
    for name, source in settings["sources"].items():
        kind = source["kind"]
        if kind == "grafana":
            names.add(GRAFANA_SERVER_TOKEN)
        elif kind == "s3":
            names.update({env_name(name, "ACCESS_KEY_ID"), env_name(name, "SECRET_ACCESS_KEY")})
        elif kind in ("mcp", "openapi") and source.get("token"):
            names.add(env_name(name, "TOKEN"))
    return sorted(names)


def mcp_context(manifest: Manifest, host: str, settings: dict[str, Any] | None) -> dict[str, Any]:
    if settings is None:
        return {
            "enabled": False,
            "s3": False,
            "grafana": None,
            "victoriametrics": None,
            "catalog": False,
        }
    sources = settings["sources"]
    grafana = None
    metrics = None
    for _, source in sorted(sources.items()):
        if source["kind"] == "grafana" and grafana is None:
            grafana = {
                "url": GRAFANA_URL,
                "enable_write_tools": ",".join(sorted(source.get("allow_write") or [])),
            }
        if source["kind"] == "victoriametrics" and metrics is None:
            metrics = {"url": default_url(manifest, host, source) or METRICS_URL}
    return {
        "enabled": True,
        "s3": any(source["kind"] == "s3" for source in sources.values()),
        "grafana": grafana,
        "victoriametrics": metrics,
        "catalog": has_catalog(settings),
        "secrets": hub_secrets(settings),
    }


def source_problems(
    manifest: Manifest, host: str, path: str, provider: str, source: dict[str, Any]
) -> list[Problem]:
    problems: list[Problem] = []
    kind = source["kind"]
    shape = kind_of(source)
    if not str(source.get("description") or "").strip():
        problems.append(
            (path, "mcp-description", "a source needs a description: the assistant reads it")
        )
    for key in sorted(set(source) - COMMON_KEYS - KIND_KEYS[kind]):
        problems.append((f"{path}.{key}", "mcp-source", f"{key} does not belong to kind {kind}"))
    missing = [key for key in KIND_REQUIRED.get(kind, ()) if key not in source]
    if missing:
        needs = ", ".join(missing)
        return [*problems, (path, "mcp-source", f"kind {kind} needs {needs}")]
    unable = CANNOT_SERVE.get(provider, {}).get(shape)
    if unable:
        problems.append((f"{path}.kind", "mcp-kind", unable))
    elif provider == IMPLEMENTED and shape in KIND_NOT_YET:
        problems.append((f"{path}.kind", "mcp-kind", KIND_NOT_YET[shape]))
    if kind == "mcp" and ("url" in source) == ("image" in source):
        problems.append((path, "mcp-source", "an mcp source has either url or image"))
    if kind == "mcp" and "tools" not in source:
        problems.append(
            (path, "mcp-tools", "name the tools that read; AyeOps cannot tell what they do")
        )
    if kind == "grafana" and default_url(manifest, host, source) is None:
        problems.append(
            (
                path,
                "mcp-source",
                f"run grafana on {host}: mcp-grafana reads the Grafana of its own host with the"
                " grafana-reader token; for another Grafana, run the hub there",
            )
        )
    if kind == "victoriametrics" and default_url(manifest, host, source) is None:
        problems.append(
            (path, "mcp-source", f"set url, or run metrics with role: server on {host}")
        )
    if kind == "s3" and s3_endpoint(manifest, host, source) is None:
        problems.append(
            (path, "mcp-source", f"set endpoint, or run the Garage objectstore on {host}")
        )
    if kind == "openapi":
        _, error = openapi_operations(manifest, str(source["spec"]))
        if error:
            return [*problems, (f"{path}.spec", "mcp-source", error)]
    if kind == "cli":
        problems += command_problems(path, source.get("commands") or {})
    return problems + tool_problems(manifest, path, source)


def tool_problems(manifest: Manifest, path: str, source: dict[str, Any]) -> list[Problem]:
    problems: list[Problem] = []
    writes = write_tools(manifest, source)
    known = known_tools(manifest, source)
    listed = list(source.get("tools") or [])
    allowed = list(source.get("allow_write") or [])
    if known is not None:
        for tool in sorted(set(listed + allowed) - known):
            problems.append((path, "mcp-tools", f"{tool} is not a tool of this source"))
    if writes is not None:
        for tool in sorted(set(listed) & writes):
            problems.append(
                (f"{path}.tools", "mcp-write", f"{tool} writes: move it to allow_write")
            )
        reads = set(allowed) - writes
        if known is not None:
            reads &= known
        for tool in sorted(reads):
            problems.append(
                (f"{path}.allow_write", "mcp-write", f"{tool} does not write: list it in tools")
            )
    return problems


def command_problems(path: str, commands: dict[str, Any]) -> list[Problem]:
    problems: list[Problem] = []
    for name, command in sorted(commands.items()):
        where = f"{path}.commands.{name}"
        if not str(command.get("description") or "").strip():
            problems.append((where, "mcp-description", "a command needs a description"))
        if command.get("write") and not command.get("allow_write"):
            problems.append(
                (where, "mcp-write", "the command writes: set allow_write: true or drop it")
            )
        if command.get("allow_write") and not command.get("write"):
            problems.append((where, "mcp-write", "allow_write without write: true"))
    return problems


def host_problems(manifest: Manifest, host: str, settings: dict[str, Any]) -> list[Problem]:
    path = f"hosts.{host}.modules.mcp"
    provider = str(settings["provider"])
    problems: list[Problem] = []
    if provider in NOT_IMPLEMENTED:
        problems.append(
            (
                f"{path}.provider",
                "mcp-provider",
                f"{provider} is not implemented yet: {NOT_IMPLEMENTED[provider]}; use agentgateway",
            )
        )
    sources = settings["sources"]
    for kind in ONE_PER_HOST:
        names = sorted(name for name, source in sources.items() if source["kind"] == kind)
        if len(names) > 1:
            problems.append(
                (f"{path}.sources", "mcp-source", f"one {kind} source per host: {', '.join(names)}")
            )
    for name, source in sorted(sources.items()):
        problems += source_problems(manifest, host, f"{path}.sources.{name}", provider, source)
    return problems + open_problems(manifest, host)


def open_problems(manifest: Manifest, host: str) -> list[Problem]:
    ingress = host_has(manifest, host, "ingress") or {}
    auth = host_has(manifest, host, "auth") or {}
    routes = ingress.get("routes") or {}
    return [
        (
            f"hosts.{host}.modules.auth.open",
            "mcp-open",
            f"{route} reaches the MCP hub, which trusts whoever reaches it; keep it signed in",
        )
        for route in sorted(set(auth.get("open") or []))
        if str(routes.get(route, "")).rsplit(":", 1)[0] == IMPLEMENTED
    ]


def mcp_problems(manifest: Manifest) -> list[Problem]:
    problems: list[Problem] = []
    for host in sorted(manifest.hosts):
        settings = mcp_settings(manifest, host)
        if settings is not None:
            problems += host_problems(manifest, host, settings)
    return problems
