from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

SCHEDULE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
PRODUCT = re.compile(r"^[a-z0-9][a-z0-9.+_-]{0,63}$")
VERSION = re.compile(r"^\d+(\.\d+)*$")
PREFIX = "ayeops_eol_"
NAP = 30
RETRY = 3600
REWRITE = 3600
DAY = 86400
SOON = 90
TIMEOUT = 15
LIMIT = 4 * 1024 * 1024
OS = "os"
FIELDS = ("name", "product", "cycle")
HEARTBEAT = Path("/tmp/eol.alive")
USER_AGENT = "ayeops-eol/1 (+https://github.com/AyeOps/ayeops)"

OS_PRODUCTS: dict[str, tuple[str, int]] = {
    "ubuntu": ("ubuntu", 2),
    "debian": ("debian", 1),
    "raspbian": ("debian", 1),
    "alpine": ("alpine-linux", 2),
    "rhel": ("rhel", 1),
    "rocky": ("rocky-linux", 1),
    "almalinux": ("almalinux", 1),
    "centos": ("centos", 1),
    "fedora": ("fedora", 1),
    "amzn": ("amazon-linux", 1),
    "opensuse-leap": ("opensuse", 2),
}


@dataclass(frozen=True)
class Component:
    name: str
    product: str
    cycle: str


def parse_schedule(text: str) -> tuple[int, int]:
    match = SCHEDULE.match(text.strip())
    if match is None:
        raise ValueError(f"not an HH:MM time: {text!r}")
    return int(match.group(1)), int(match.group(2))


def next_run(now: float, hour: int, minute: int) -> float:
    today = time.localtime(now)

    def at(offset: int) -> float:
        return time.mktime(
            (today.tm_year, today.tm_mon, today.tm_mday + offset, hour, minute, 0, 0, 0, -1)
        )

    target = at(0)
    return target if target > now else at(1)


def parse_os_release(text: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in text.splitlines():
        key, sep, value = line.strip().partition("=")
        if not sep or not key or key.startswith("#"):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        fields[key] = value
    return fields


def os_component(fields: Mapping[str, str]) -> Component | None:
    known = OS_PRODUCTS.get(fields.get("ID", "").strip().lower())
    version = fields.get("VERSION_ID", "").strip()
    if known is None or not VERSION.match(version):
        return None
    product, parts = known
    numbers = version.split(".")
    if len(numbers) < parts:
        return None
    return Component(OS, product, ".".join(numbers[:parts]))


def host_os(path: Path | None) -> tuple[Component | None, bool]:
    if path is None:
        return None, False
    try:
        fields = parse_os_release(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        return None, True
    return os_component(fields), True


def parse_date(text: Any) -> date | None:
    if not isinstance(text, str):
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def releases(body: Any) -> list[Mapping[str, Any]] | None:
    result = body.get("result") if isinstance(body, Mapping) else None
    found = result.get("releases") if isinstance(result, Mapping) else None
    if not isinstance(found, list):
        return None
    return [release for release in found if isinstance(release, Mapping)]


@dataclass(frozen=True)
class Status:
    component: Component
    known: bool
    security: date | None = None
    support: date | None = None
    ended: bool = False


def evaluate(component: Component, body: Any, today: date) -> Status | None:
    listed = releases(body)
    if listed is None:
        return None
    release = next((r for r in listed if str(r.get("name")) == component.cycle), None)
    if release is None:
        return Status(component, False)
    security = parse_date(release.get("eolFrom"))
    support = parse_date(release.get("eoasFrom")) if "eoasFrom" in release else security
    ended = release.get("isEol") is True or (security is not None and security <= today)
    return Status(component, True, security, support, ended)


@dataclass(frozen=True)
class Cached:
    fetched: float
    body: Mapping[str, Any]
    etag: str | None = None
    modified: str | None = None


def cache_file(cache: Path, product: str) -> Path:
    return cache / "products" / f"{product}.json"


def read_cached(path: Path) -> Cached | None:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
        fetched = float(loaded["fetched"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    body = loaded.get("body")
    if releases(body) is None:
        return None
    etag, modified = loaded.get("etag"), loaded.get("modified")
    return Cached(
        fetched,
        body,
        etag if isinstance(etag, str) else None,
        modified if isinstance(modified, str) else None,
    )


def write_atomic(path: Path, text: str) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def write_cached(path: Path, cached: Cached) -> None:
    data = {
        "fetched": cached.fetched,
        "etag": cached.etag,
        "modified": cached.modified,
        "body": cached.body,
    }
    write_atomic(path, json.dumps(data, sort_keys=True))


@dataclass(frozen=True)
class Fetched:
    ok: bool
    cached: Cached | None
    error: str = ""


Opener = Callable[[urllib.request.Request, float], Any]


def default_opener(request: urllib.request.Request, timeout: float) -> Any:
    return urllib.request.urlopen(request, timeout=timeout)


def request_for(api: str, product: str, previous: Cached | None) -> urllib.request.Request:
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if previous is not None and previous.etag:
        headers["If-None-Match"] = previous.etag
    if previous is not None and previous.modified:
        headers["If-Modified-Since"] = previous.modified
    return urllib.request.Request(f"{api}/products/{product}", headers=headers)


def fetch(api: str, product: str, previous: Cached | None, now: float, opener: Opener) -> Fetched:
    try:
        with opener(request_for(api, product, previous), TIMEOUT) as response:
            raw = response.read(LIMIT + 1)
            headers = response.headers
    except urllib.error.HTTPError as error:
        if error.code == 304 and previous is not None:
            return Fetched(True, replace(previous, fetched=now))
        return Fetched(False, previous, f"HTTP {error.code}")
    except (OSError, ValueError) as error:
        return Fetched(False, previous, type(error).__name__)
    if len(raw) > LIMIT:
        return Fetched(False, previous, "response too large")
    try:
        body = json.loads(raw)
    except ValueError:
        return Fetched(False, previous, "not JSON")
    if releases(body) is None:
        return Fetched(False, previous, "no releases in the response")
    return Fetched(True, Cached(now, body, headers.get("ETag"), headers.get("Last-Modified")))


@dataclass(frozen=True)
class State:
    last_success: float
    last_attempt: float | None = None
    results: Mapping[str, bool] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return all(self.results.values())


def read_state(path: Path) -> State | None:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
        success = float(loaded["last_success"])
        attempt = loaded.get("last_attempt")
        results = loaded.get("results") or {}
        return State(
            success,
            None if attempt is None else float(attempt),
            {str(key): value is True for key, value in dict(results).items()},
        )
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


def write_state(path: Path, state: State) -> None:
    data = {
        "last_success": state.last_success,
        "last_attempt": state.last_attempt,
        "results": dict(state.results),
    }
    write_atomic(path, json.dumps(data, sort_keys=True))


@dataclass(frozen=True)
class Settings:
    schedule: tuple[int, int] = (5, 30)
    api: str = "https://endoflife.date/api/v1"
    cache: Path = Path("/cache")
    textfile: Path = Path("/textfile/ayeops_eol.prom")
    os_release: Path | None = Path("/host/os-release")
    components: tuple[Component, ...] = ()
    unmapped: tuple[str, ...] = ()

    @property
    def state(self) -> Path:
        return self.cache / "state.json"


def components_from(text: str) -> tuple[Component, ...]:
    loaded = json.loads(text)
    if not isinstance(loaded, list):
        raise ValueError("AYE_EOL_COMPONENTS must be a JSON list")
    found = []
    for entry in loaded:
        values = [entry.get(key) if isinstance(entry, Mapping) else None for key in FIELDS]
        if not all(isinstance(value, str) and value for value in values):
            raise ValueError(f"a component needs name, product and cycle: {entry!r}")
        component = Component(*(str(value) for value in values))
        if not PRODUCT.match(component.product):
            raise ValueError(f"not an endoflife.date product: {component.product!r}")
        found.append(component)
    return tuple(found)


def unmapped_from(text: str) -> tuple[str, ...]:
    loaded = json.loads(text)
    if not isinstance(loaded, list) or not all(isinstance(i, str) and i for i in loaded):
        raise ValueError("AYE_EOL_UNMAPPED must be a JSON list of image references")
    return tuple(loaded)


def settings_from(environ: Mapping[str, str]) -> Settings:
    os_release = environ.get("AYE_EOL_OS_RELEASE", "/host/os-release")
    return Settings(
        schedule=parse_schedule(environ.get("AYE_EOL_SCHEDULE", "05:30")),
        api=environ.get("AYE_EOL_API", "https://endoflife.date/api/v1").rstrip("/"),
        cache=Path(environ.get("AYE_EOL_CACHE", "/cache")),
        textfile=Path(environ.get("AYE_EOL_TEXTFILE", "/textfile/ayeops_eol.prom")),
        os_release=Path(os_release) if os_release else None,
        components=components_from(environ.get("AYE_EOL_COMPONENTS", "[]")),
        unmapped=unmapped_from(environ.get("AYE_EOL_UNMAPPED", "[]")),
    )


def tracked(settings: Settings) -> tuple[list[Component], list[str]]:
    components = list(settings.components)
    unmapped = list(settings.unmapped)
    found, wanted = host_os(settings.os_release)
    if found is not None:
        components.append(found)
    elif wanted:
        unmapped.append(OS)
    return components, sorted(set(unmapped))


def products_of(components: Iterable[Component]) -> list[str]:
    return sorted({component.product for component in components})


def cached_all(settings: Settings, products: Iterable[str]) -> dict[str, Cached]:
    found = {}
    for product in products:
        cached = read_cached(cache_file(settings.cache, product))
        if cached is not None:
            found[product] = cached
    return found


def refresh(
    settings: Settings,
    products: list[str],
    previous: State,
    wall: Callable[[], float],
    opener: Opener,
) -> tuple[State, dict[str, str]]:
    (settings.cache / "products").mkdir(parents=True, exist_ok=True)
    results: dict[str, bool] = {}
    errors: dict[str, str] = {}
    for product in products:
        path = cache_file(settings.cache, product)
        fetched = fetch(settings.api, product, read_cached(path), wall(), opener)
        if fetched.ok and fetched.cached is not None:
            write_cached(path, fetched.cached)
        results[product] = fetched.ok
        if not fetched.ok:
            errors[product] = fetched.error
    finished = wall()
    ok = all(results.values())
    state = State(finished if ok else previous.last_success, finished, results)
    write_state(settings.state, state)
    return state, errors


def quoted(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def metric(name: str, value: float, **labels: str) -> str:
    rendered = ",".join(f'{key}="{quoted(val)}"' for key, val in sorted(labels.items()))
    suffix = "{" + rendered + "}" if rendered else ""
    number = int(value) if float(value).is_integer() else value
    return f"{PREFIX}{name}{suffix} {number}\n"


def today_of(now: float) -> date:
    return datetime.fromtimestamp(now, UTC).date()


def statuses(
    components: Iterable[Component], data: Mapping[str, Cached], today: date
) -> list[Status]:
    found = []
    for component in components:
        cached = data.get(component.product)
        status = evaluate(component, cached.body, today) if cached is not None else None
        if status is not None:
            found.append(status)
    return found


def status_lines(status: Status, today: date) -> list[str]:
    component = status.component
    labels = {"component": component.name, "product": component.product, "cycle": component.cycle}
    lines = [metric("cycle_known", int(status.known), **labels)]
    if not status.known:
        return lines
    lines.append(metric("security_ended", int(status.ended), **labels))
    if status.security is not None:
        days = (status.security - today).days
        lines.append(metric("security_days_left", days, date=status.security.isoformat(), **labels))
    if status.support is not None:
        days = (status.support - today).days
        lines.append(metric("support_days_left", days, date=status.support.isoformat(), **labels))
    return lines


def render_metrics(
    found: list[Status],
    unmapped: list[str],
    state: State,
    data: Mapping[str, Cached],
    now: float,
) -> str:
    today = today_of(now)
    lines = [metric("last_success_timestamp_seconds", round(state.last_success))]
    if state.last_attempt is not None:
        lines.append(metric("last_fetch_timestamp_seconds", round(state.last_attempt)))
        lines.append(metric("last_fetch_success", int(state.ok)))
    lines.append(metric("unmapped_components", len(unmapped)))
    for name in unmapped:
        lines.append(metric("unmapped", 1, component=name))
    for product, ok in sorted(state.results.items()):
        lines.append(metric("fetch_success", int(ok), product=product))
    for product, cached in sorted(data.items()):
        lines.append(metric("data_timestamp_seconds", round(cached.fetched), product=product))
    for status in found:
        lines.extend(status_lines(status, today))
    return "".join(lines)


def label(component: Component) -> str:
    return f"{component.product} {component.cycle} ({component.name})"


def summary(
    found: list[Status], state: State, errors: Mapping[str, str], today: date
) -> dict[str, Any]:
    soon = {
        label(s.component): (s.security - today).days
        for s in found
        if s.known and not s.ended and s.security is not None and (s.security - today).days < SOON
    }
    return {
        "event": "eol-fetch",
        "success": state.ok,
        "failed": dict(errors),
        "soon": soon,
        "ended": sorted(label(s.component) for s in found if s.known and s.ended),
        "unknown": sorted(label(s.component) for s in found if not s.known),
    }


def first_due(settings: Settings, products: list[str], now: float) -> float:
    data = cached_all(settings, products)
    if len(data) < len(products) or any(now - c.fetched >= DAY for c in data.values()):
        return now
    return next_run(now, *settings.schedule)


def prepare(settings: Settings, wall: float) -> State:
    (settings.cache / "products").mkdir(parents=True, exist_ok=True)
    state = read_state(settings.state)
    if state is None:
        state = State(wall)
        write_state(settings.state, state)
    return state


def write_metrics(settings: Settings, state: State, now: float) -> list[Status]:
    components, unmapped = tracked(settings)
    data = cached_all(settings, products_of(components))
    found = statuses(components, data, today_of(now))
    write_atomic(settings.textfile, render_metrics(found, unmapped, state, data, now))
    return found


def run_once(
    settings: Settings, state: State, wall: Callable[[], float], opener: Opener
) -> tuple[State, dict[str, Any]]:
    components, _ = tracked(settings)
    state, errors = refresh(settings, products_of(components), state, wall, opener)
    now = wall()
    found = write_metrics(settings, state, now)
    return state, summary(found, state, errors, today_of(now))


def serve(
    settings: Settings,
    state: State,
    wall: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
    keep_going: Callable[[], bool] = lambda: True,
    heartbeat: Path = HEARTBEAT,
    opener: Opener = default_opener,
    emit: Callable[[dict[str, Any]], None] = lambda line: print(
        json.dumps(line, sort_keys=True), flush=True
    ),
) -> None:
    due = first_due(settings, products_of(tracked(settings)[0]), wall())
    written: float | None = None
    while keep_going():
        heartbeat.touch()
        now = wall()
        if now >= due:
            state, line = run_once(settings, state, wall, opener)
            emit(line)
            written = wall()
            planned = next_run(written, *settings.schedule)
            due = planned if state.ok else min(planned, written + RETRY)
        elif written is None or now - written >= REWRITE:
            write_metrics(settings, state, now)
            written = now
        sleep(max(1.0, min(NAP, due - wall())))


def main(environ: Mapping[str, str] = os.environ) -> int:
    settings = settings_from(environ)
    state = prepare(settings, time.time())
    if environ.get("AYE_EOL_ONCE") == "1":
        state, line = run_once(settings, state, time.time, default_opener)
        print(json.dumps(line, sort_keys=True), flush=True)
        return 0 if state.ok else 1
    serve(settings, state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
