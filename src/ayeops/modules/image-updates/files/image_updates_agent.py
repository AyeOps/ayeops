from __future__ import annotations

import http.client
import json
import os
import re
import sys
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

LABEL = "io.ayeops.image-updates"
DEFAULT_REGISTRY = "docker.io"
PINNED_TAG = re.compile(r"^v?\d+\.\d+\.\d+(?:[-+._].*)?$")
COUNTS = re.compile(
    r"\*\s+(\d+)\s+(?:(new)\s+tag\(s\)\s+found|tag\(s\)\s+(updated|unchanged|skipped|with error))"
)
COUNT_NAMES = {
    "new": "new",
    "updated": "update",
    "unchanged": "unchange",
    "skipped": "skip",
    "with error": "error",
}
STATES = ("current", "pending", "unknown", "pinned", "local", "skipped")
NOTIFIED = {"new", "update"}
HEARTBEAT = Path("/tmp/image-updates.alive")
API_TIMEOUT = 30.0
MAX_BODY = 1 << 20

Api = Callable[[str], Any]


def split_reference(reference: str) -> tuple[str, str | None, str | None]:
    name, _, digest = reference.partition("@")
    slash = name.rfind("/")
    colon = name.rfind(":")
    if colon > slash:
        return name[:colon], name[colon + 1 :], digest or None
    return name, None, digest or None


def normalize_repository(repository: str) -> str:
    parts = repository.split("/")
    first = parts[0]
    if len(parts) > 1 and ("." in first or ":" in first or first == "localhost"):
        return repository
    if len(parts) == 1:
        return f"{DEFAULT_REGISTRY}/library/{repository}"
    return f"{DEFAULT_REGISTRY}/{repository}"


def normalize(reference: str) -> tuple[str, str]:
    repository, tag, _ = split_reference(reference)
    return normalize_repository(repository), tag or "latest"


def is_floating(reference: str) -> bool:
    _, tag, digest = split_reference(reference)
    if digest:
        return False
    return not (tag and PINNED_TAG.match(tag))


def verdict(reference: str, labels: Mapping[str, str] | None) -> str:
    wanted = (labels or {}).get(LABEL, "").strip().lower()
    if wanted == "false":
        return "skipped"
    if not reference or reference.startswith("sha256:"):
        return "skipped"
    if wanted == "true" or is_floating(reference):
        return "watch"
    return "pinned"


def repo_digests(image: Mapping[str, Any]) -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for entry in image.get("RepoDigests") or []:
        repository, _, digest = str(entry).partition("@")
        if digest:
            found.setdefault(normalize_repository(repository), set()).add(digest)
    return found


def platform_of(image: Mapping[str, Any]) -> dict[str, str]:
    platform = {"os": str(image.get("Os") or "linux"), "arch": str(image.get("Architecture") or "")}
    if image.get("Variant"):
        platform["variant"] = str(image["Variant"])
    return {key: value for key, value in platform.items() if value}


@dataclass(frozen=True)
class Watched:
    container: str
    repository: str
    tag: str
    digests: frozenset[str]
    platform: tuple[tuple[str, str], ...]

    @property
    def name(self) -> str:
        return f"{self.repository}:{self.tag}"


@dataclass
class Scan:
    watched: list[Watched] = field(default_factory=list)
    states: dict[str, str] = field(default_factory=dict)


def container_name(container: Mapping[str, Any]) -> str:
    names = container.get("Names") or []
    if names:
        return str(names[0]).lstrip("/")
    return str(container.get("Id", ""))[:12]


def scan(api: Api) -> Scan:
    result = Scan()
    for container in api("/containers/json") or []:
        name = container_name(container)
        reference = str(container.get("Image") or "")
        decision = verdict(reference, container.get("Labels"))
        if decision != "watch":
            result.states[name] = decision
            continue
        image = api(f"/images/{container.get('ImageID', '')}/json") or {}
        repository, tag = normalize(reference)
        digests = repo_digests(image).get(repository, set())
        if not digests:
            result.states[name] = "local"
            continue
        result.watched.append(
            Watched(
                container=name,
                repository=repository,
                tag=tag,
                digests=frozenset(digests),
                platform=tuple(sorted(platform_of(image).items())),
            )
        )
    return result


def diun_images(watched: Iterable[Watched]) -> str:
    entries = {}
    for item in watched:
        entry: dict[str, Any] = {"name": item.name}
        if item.platform:
            entry["platform"] = dict(item.platform)
        entries[(item.name, item.platform)] = entry
    return json.dumps([entries[key] for key in sorted(entries)], indent=2, sort_keys=True) + "\n"


@dataclass
class State:
    digests: dict[str, str] = field(default_factory=dict)
    check_started: float = 0.0
    check_success: float = 0.0
    check_failed: float = 0.0
    counts: dict[str, int] = field(default_factory=dict)
    notifications: int = 0

    def to_json(self) -> str:
        return json.dumps(vars(self), sort_keys=True)

    @classmethod
    def from_json(cls, text: str) -> State:
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError("state is not an object")
        return cls(
            digests={str(k): str(v) for k, v in (data.get("digests") or {}).items()},
            check_started=float(data.get("check_started") or 0),
            check_success=float(data.get("check_success") or 0),
            check_failed=float(data.get("check_failed") or 0),
            counts={str(k): int(v) for k, v in (data.get("counts") or {}).items()},
            notifications=int(data.get("notifications") or 0),
        )


def apply_notification(state: State, payload: Any) -> bool:
    if not isinstance(payload, dict):
        raise ValueError("notification is not an object")
    status = str(payload.get("status") or "")
    digest = str(payload.get("digest") or "")
    image = str(payload.get("image") or "")
    if status not in NOTIFIED or not digest.startswith("sha256:") or not image:
        return False
    repository, tag = normalize(image)
    state.digests[f"{repository}:{tag}"] = digest
    state.notifications += 1
    return True


def parse_counts(text: str) -> dict[str, int]:
    return {
        COUNT_NAMES[found or other]: int(number) for number, found, other in COUNTS.findall(text)
    }


def apply_ping(state: State, path: str, body: str, now: float) -> bool:
    parts = [part for part in path.split("/") if part]
    if len(parts) < 2 or parts[0] != "hc":
        return False
    event = parts[2] if len(parts) > 2 else "success"
    if event == "start":
        state.check_started = now
    elif event == "fail":
        state.check_failed = now
    elif event == "success":
        state.check_success = now
        state.counts = parse_counts(body)
    else:
        return False
    return True


def classify(scan_result: Scan, state: State) -> dict[str, tuple[str, Watched | None]]:
    rows: dict[str, tuple[str, Watched | None]] = {
        name: (decision, None) for name, decision in scan_result.states.items()
    }
    for item in scan_result.watched:
        latest = state.digests.get(item.name)
        if latest is None:
            rows[item.container] = ("unknown", item)
        elif latest in item.digests:
            rows[item.container] = ("current", item)
        else:
            rows[item.container] = ("pending", item)
    return rows


def escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def metric(name: str, value: float, **labels: str) -> str:
    rendered = ",".join(f'{key}="{escape(val)}"' for key, val in sorted(labels.items()))
    suffix = "{" + rendered + "}" if rendered else ""
    number = int(value) if float(value).is_integer() else value
    return f"{name}{suffix} {number}\n"


CHECK_STAMPS = (
    ("check_started", "ayeops_image_updates_last_check_started_timestamp_seconds"),
    ("check_success", "ayeops_image_updates_last_check_success_timestamp_seconds"),
    ("check_failed", "ayeops_image_updates_last_check_failed_timestamp_seconds"),
)


def render_metrics(rows: Mapping[str, tuple[str, Watched | None]], state: State, now: float) -> str:
    lines = [metric("ayeops_image_updates_last_scan_timestamp_seconds", round(now))]
    for attribute, name in CHECK_STAMPS:
        stamp = getattr(state, attribute)
        if stamp:
            lines.append(metric(name, round(stamp)))
    for result, count in sorted(state.counts.items()):
        lines.append(metric("ayeops_image_updates_tags", count, result=result))
    lines.append(metric("ayeops_image_updates_notifications_total", state.notifications))
    totals = dict.fromkeys(STATES, 0)
    for decision, _ in rows.values():
        totals[decision] += 1
    for decision in STATES:
        lines.append(metric("ayeops_image_updates_containers", totals[decision], state=decision))
    for container, (decision, item) in sorted(rows.items()):
        if item is None or decision == "unknown":
            continue
        lines.append(
            metric(
                "ayeops_image_update_available",
                int(decision == "pending"),
                container=container,
                image=item.repository,
                tag=item.tag,
            )
        )
    return "".join(lines)


def write_atomic(path: Path, text: str) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def write_if_changed(path: Path, text: str) -> bool:
    try:
        if path.read_text(encoding="utf-8") == text:
            return False
    except OSError:
        pass
    write_atomic(path, text)
    return True


@dataclass(frozen=True)
class Settings:
    docker: str
    listen: tuple[str, int]
    state: Path
    images: Path
    textfile: Path
    interval: float


def address(text: str) -> tuple[str, int]:
    host, _, port = text.rpartition(":")
    if not host or not port.isdigit():
        raise ValueError(f"not host:port: {text!r}")
    return host, int(port)


def settings_from(environ: Mapping[str, str]) -> Settings:
    interval = float(environ.get("AYE_IMAGES_INTERVAL", "60"))
    if interval < 10:
        raise ValueError("AYE_IMAGES_INTERVAL must be at least 10 seconds")
    return Settings(
        docker=environ.get("AYE_IMAGES_DOCKER", "http://image-updates-proxy:2375"),
        listen=address(environ.get("AYE_IMAGES_LISTEN", "0.0.0.0:8098")),
        state=Path(environ.get("AYE_IMAGES_STATE", "/data/agent-state.json")),
        images=Path(environ.get("AYE_IMAGES_FILE", "/data/images.json")),
        textfile=Path(environ.get("AYE_IMAGES_TEXTFILE", "/textfile/ayeops_image_updates.prom")),
        interval=interval,
    )


def docker_api(
    base: str,
    timeout: float = API_TIMEOUT,
    connect: Callable[[str, int, float], Any] | None = None,
) -> Api:
    parts = urlsplit(base)
    if parts.scheme != "http" or not parts.hostname:
        raise ValueError(f"the docker API must be http://host:port, not {base!r}")
    host, port = parts.hostname, parts.port or 2375
    opener = connect or (lambda h, p, t: http.client.HTTPConnection(h, p, timeout=t))

    def call(path: str) -> Any:
        connection = opener(host, port, timeout)
        try:
            connection.request("GET", path)
            response = connection.getresponse()
            body = response.read()
        finally:
            connection.close()
        if response.status >= 400:
            raise RuntimeError(f"GET {path}: {response.status} {body[:200]!r}")
        return json.loads(body) if body else None

    return call


class Agent:
    def __init__(self, settings: Settings, api: Api, wall: Callable[[], float] = time.time) -> None:
        self.settings = settings
        self.api = api
        self.wall = wall
        self.lock = threading.Lock()
        self.state = self.load()
        self.last_scan: Scan | None = None

    def load(self) -> State:
        try:
            return State.from_json(self.settings.state.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return State()

    def save(self) -> None:
        write_atomic(self.settings.state, self.state.to_json())

    def refresh(self) -> Scan:
        result = scan(self.api)
        self.last_scan = result
        write_if_changed(self.settings.images, diun_images(result.watched))
        self.publish()
        return result

    def publish(self) -> None:
        if self.last_scan is None:
            return
        with self.lock:
            text = render_metrics(classify(self.last_scan, self.state), self.state, self.wall())
        write_atomic(self.settings.textfile, text)

    def notification(self, body: bytes) -> bool:
        payload = json.loads(body.decode("utf-8"))
        with self.lock:
            changed = apply_notification(self.state, payload)
            if changed:
                self.save()
        if changed:
            self.publish()
        return changed

    def ping(self, path: str, body: bytes) -> bool:
        with self.lock:
            changed = apply_ping(self.state, path, body.decode("utf-8", "replace"), self.wall())
            if changed:
                self.save()
        if changed:
            self.publish()
        return changed


def handler_for(agent: Agent) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def reply(self, status: int) -> None:
            self.send_response(status)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self) -> None:
            self.reply(200 if self.path == "/health" else 404)

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                self.reply(413)
                return
            body = self.rfile.read(length)
            try:
                if self.path == "/diun":
                    agent.notification(body)
                    self.reply(204)
                elif self.path.startswith("/hc/"):
                    self.reply(200 if agent.ping(self.path, body) else 404)
                else:
                    self.reply(404)
            except (ValueError, UnicodeDecodeError):
                self.reply(400)

        def log_message(self, format: str, *args: Any) -> None:
            return

    return Handler


def log(event: str, **fields: Any) -> None:
    print(json.dumps({"event": event, **fields}, sort_keys=True), flush=True)


def serve(
    agent: Agent,
    sleep: Callable[[float], None] = time.sleep,
    heartbeat: Path = HEARTBEAT,
    going: Callable[[], bool] = lambda: True,
) -> None:
    while going():
        try:
            result = agent.refresh()
            heartbeat.touch()
            log("scan", watched=len(result.watched), others=len(result.states))
        except (OSError, RuntimeError, ValueError) as error:
            log("scan-failed", error=str(error))
        sleep(agent.settings.interval)


def main(environ: Mapping[str, str] = os.environ) -> int:
    settings = settings_from(environ)
    agent = Agent(settings, docker_api(settings.docker))
    server = ThreadingHTTPServer(settings.listen, handler_for(agent))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    if environ.get("AYE_IMAGES_ONCE") == "1":
        agent.refresh()
        server.shutdown()
        return 0
    serve(agent)
    return 0


if __name__ == "__main__":
    sys.exit(main())
