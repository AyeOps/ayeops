from __future__ import annotations

from typing import Any

from ayeops.manifest import Manifest
from ayeops.registry import load_modules

METRICS_PORT = 8428
GRAFANA_PORT = 3000
GRAFANA_HEALTHY = '"database": "ok"'
KINDS = ("http", "fresh", "tls")
RUNBOOK = "https://github.com/AyeOps/ayeops/blob/main/docs/runbooks/sentinel-central-down.md"
DIGEST_SOURCE = "samples"

Problem = tuple[str, str, str]


def watched_settings(manifest: Manifest, host: str) -> dict[str, dict[str, Any]]:
    known = load_modules()
    return {
        name: known[name].settings(overrides)
        for name, overrides in manifest.host_modules(host).items()
        if name in known
    }


def grafana_health(address: str, settings: dict[str, Any]) -> str | None:
    root = str(settings.get("root_url") or "").rstrip("/")
    if root.startswith("https://"):
        return f"{root}/api/health"
    if GRAFANA_PORT in (settings.get("expose") or []):
        return f"http://{address}:{GRAFANA_PORT}/api/health"
    return None


def default_checks(manifest: Manifest, watch: str) -> list[dict[str, Any]]:
    if watch not in manifest.hosts:
        return []
    address = str(manifest.hosts[watch].get("address") or watch)
    settings = watched_settings(manifest, watch)
    checks: list[dict[str, Any]] = []
    grafana = settings.get("grafana")
    url = grafana_health(address, grafana) if grafana is not None else None
    if url:
        checks.append({"name": "grafana", "kind": "http", "url": url, "contains": GRAFANA_HEALTHY})
    metrics = settings.get("metrics") or {}
    if metrics.get("role") == "server" and METRICS_PORT in (metrics.get("expose") or []):
        base = f"http://{address}:{METRICS_PORT}"
        checks.append({"name": "victoriametrics", "kind": "http", "url": f"{base}/health"})
        checks.append(
            {
                "name": "samples",
                "kind": "fresh",
                "url": f"{base}/api/v1/query",
                "max_age_seconds": 180,
            }
        )
    return checks


def merged_checks(
    defaults: list[dict[str, Any]], overrides: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    merged = {str(check["name"]): dict(check) for check in defaults}
    for override in overrides:
        name = str(override["name"])
        if override.get("enabled") is False:
            merged.pop(name, None)
            continue
        base = merged.get(name, {})
        if "kind" in override and override["kind"] != base.get("kind"):
            base = {}
        entry = {**base, **override}
        entry.pop("enabled", None)
        merged[name] = entry
    return list(merged.values())


def sentinel_checks(manifest: Manifest, settings: dict[str, Any]) -> list[dict[str, Any]]:
    watch = str(settings.get("watch") or "")
    return merged_checks(default_checks(manifest, watch), list(settings.get("checks") or []))


def digest_url(settings: dict[str, Any], checks: list[dict[str, Any]]) -> str:
    own = (settings.get("digest") or {}).get("url")
    if own:
        return str(own)
    for check in checks:
        if check["name"] == DIGEST_SOURCE and check.get("kind") == "fresh":
            return str(check.get("url") or "")
    return ""


def sentinel_context(manifest: Manifest, settings: dict[str, Any] | None) -> dict[str, Any]:
    if settings is None:
        return {}
    checks = sentinel_checks(manifest, settings)
    return {"checks": checks, "runbook": RUNBOOK, "digest_url": digest_url(settings, checks)}


def check_problem(check: dict[str, Any]) -> str | None:
    kind = check.get("kind")
    if kind not in KINDS:
        return f"check {check['name']!r} needs kind: one of {', '.join(KINDS)}"
    if kind == "tls":
        return None if check.get("host") else f"check {check['name']!r} of kind tls needs host"
    url = str(check.get("url") or "")
    if not url.startswith(("http://", "https://")):
        return f"check {check['name']!r} of kind {kind} needs an http(s) url"
    return None


def sentinel_problems(manifest: Manifest) -> list[Problem]:
    known = load_modules()
    if "sentinel" not in known:
        return []
    problems: list[Problem] = []
    for host in sorted(manifest.hosts):
        overrides = manifest.host_modules(host).get("sentinel")
        if overrides is None:
            continue
        settings = known["sentinel"].settings(overrides)
        path = f"hosts.{host}.modules.sentinel"
        watch = str(settings.get("watch") or "")
        if watch not in manifest.hosts:
            problems.append((f"{path}.watch", "sentinel-watch", f"unknown host {watch!r}"))
            continue
        if watch == host:
            problems.append(
                (
                    f"{path}.watch",
                    "sentinel-watch",
                    f"a sentinel on {host} cannot watch {host}: when the host dies, so does it",
                )
            )
        elif zone := manifest.shared_zone(host, watch):
            problems.append(
                (
                    f"{path}.watch",
                    "sentinel-watch",
                    f"{host} and {watch} are both in zone {zone!r}: one power or network outage"
                    " silences the host and its sentinel together",
                )
            )
        checks = sentinel_checks(manifest, settings)
        if not checks:
            problems.append(
                (
                    f"{path}.checks",
                    "sentinel-checks",
                    f"{watch} runs no grafana with root_url or exposed 3000 and no metrics server"
                    " exposing 8428, so there is nothing to check by default: list checks",
                )
            )
        for check in checks:
            problem = check_problem(check)
            if problem:
                problems.append((f"{path}.checks", "sentinel-checks", problem))
        if settings.get("digest") and not digest_url(settings, checks):
            problems.append(
                (
                    f"{path}.digest",
                    "sentinel-digest",
                    f"the digest reads the backup metrics from the VictoriaMetrics of {watch} and"
                    " there is no samples check to take its address from: set digest.url",
                )
            )
    return problems
