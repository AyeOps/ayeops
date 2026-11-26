from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import yaml

from ayeops import __version__, secretkeys
from ayeops.defaults import UsageError, find_manifest
from ayeops.local_modules import with_local_modules
from ayeops.manifest import Manifest, load_manifest
from ayeops.permissions import Generated, catalog_generated, host_generated
from ayeops.registry import Module, load_modules
from ayeops.render import host_tree
from ayeops.rules import check_manifest

PREFIX = "AYE_SECRETS_"
IDENTITY_FIELDS = ("url", "client_id", "client_secret", "project_id", "environment", "path")
IDENTITY_KEYS = frozenset(f"{PREFIX}{field.upper()}" for field in IDENTITY_FIELDS)
REQUIRED = ("url", "client_id", "client_secret", "project_id", "environment")
KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
LINE = re.compile(r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")
SAFE = re.compile(r"^[A-Za-z0-9_./:@%+=,-]*$")
DOUBLE_ESCAPES = {"n": "\n", "r": "\r", "t": "\t", '"': '"', "\\": "\\", "$": "$"}
BATCH = 100
TIMEOUT = 30
PROVIDER_FLAGS = {
    "identity": "--identity",
    "url": "--url",
    "project_id": "--project",
    "environment": "--environment",
    "path": "--path",
}


class SecretsError(Exception):
    pass


class FolderMissing(SecretsError):
    pass


@dataclass(frozen=True)
class Scope:
    project_id: str
    environment: str
    path: str


@dataclass(frozen=True)
class Identity:
    url: str
    client_id: str
    client_secret: str
    scope: Scope


class SecretsProvider(Protocol):
    def login(self, client_id: str, client_secret: str) -> None: ...

    def read(self, scope: Scope) -> dict[str, str]: ...

    def keys(self, scope: Scope) -> set[str]: ...

    def ensure_folder(self, scope: Scope) -> None: ...

    def create(self, scope: Scope, values: Mapping[str, str]) -> None: ...

    def update(self, scope: Scope, key: str, value: str) -> None: ...


Opener = Callable[..., Any]


class Infisical:
    def __init__(self, url: str, opener: Opener = urllib.request.urlopen) -> None:
        self.base = url.rstrip("/")
        self.opener = opener
        self.token = ""

    def _call(
        self,
        method: str,
        path: str,
        query: Mapping[str, str] | None = None,
        body: Mapping[str, Any] | None = None,
    ) -> Any:
        url = f"{self.base}{path}"
        if query:
            url = f"{url}?{urllib.parse.urlencode(query)}"
        headers = {"Accept": "application/json", "User-Agent": f"ayeops/{__version__}"}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with self.opener(request, timeout=TIMEOUT) as response:
                raw = response.read()
        except urllib.error.HTTPError as error:
            message = _error_message(error)
            text = f"{method} {path}: HTTP {error.code}: {message}"
            if error.code == 404 and "folder" in message.lower():
                raise FolderMissing(text) from None
            raise SecretsError(text) from None
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            reason = getattr(error, "reason", error)
            raise SecretsError(f"{method} {path}: {reason}") from None
        return json.loads(raw) if raw else {}

    def login(self, client_id: str, client_secret: str) -> None:
        answer = self._call(
            "POST",
            "/api/v1/auth/universal-auth/login",
            body={"clientId": client_id, "clientSecret": client_secret},
        )
        token = answer.get("accessToken") if isinstance(answer, dict) else None
        if not token:
            raise SecretsError("login returned no access token")
        self.token = str(token)

    def _list(self, scope: Scope, values: bool) -> dict[str, Any]:
        answer = self._call(
            "GET",
            "/api/v4/secrets",
            query={
                "projectId": scope.project_id,
                "environment": scope.environment,
                "secretPath": scope.path,
                "viewSecretValue": str(values).lower(),
                "expandSecretReferences": str(values).lower(),
                "includeImports": str(values).lower(),
            },
        )
        return answer if isinstance(answer, dict) else {}

    def read(self, scope: Scope) -> dict[str, str]:
        answer = self._list(scope, values=True)
        merged: dict[str, str] = {}
        for imported in answer.get("imports") or []:
            merged.update(_pairs(imported.get("secrets") or []))
        merged.update(_pairs(answer.get("secrets") or []))
        return merged

    def keys(self, scope: Scope) -> set[str]:
        return set(_pairs(self._list(scope, values=False).get("secrets") or [], values=False))

    def ensure_folder(self, scope: Scope) -> None:
        parent = "/"
        for name in [part for part in scope.path.split("/") if part]:
            try:
                self._call(
                    "POST",
                    "/api/v2/folders",
                    body={
                        "projectId": scope.project_id,
                        "environment": scope.environment,
                        "name": name,
                        "path": parent,
                    },
                )
            except SecretsError as error:
                if " HTTP 400:" not in str(error) and " HTTP 409:" not in str(error):
                    raise
            parent = f"{parent.rstrip('/')}/{name}"

    def create(self, scope: Scope, values: Mapping[str, str]) -> None:
        items = sorted(values.items())
        for start in range(0, len(items), BATCH):
            self._call(
                "POST",
                "/api/v4/secrets/batch",
                body={
                    "projectId": scope.project_id,
                    "environment": scope.environment,
                    "secretPath": scope.path,
                    "secrets": [
                        {"secretKey": key, "secretValue": value}
                        for key, value in items[start : start + BATCH]
                    ],
                },
            )

    def update(self, scope: Scope, key: str, value: str) -> None:
        self._call(
            "PATCH",
            f"/api/v4/secrets/{urllib.parse.quote(key, safe='')}",
            body={
                "projectId": scope.project_id,
                "environment": scope.environment,
                "secretPath": scope.path,
                "secretValue": value,
            },
        )


PROVIDERS: dict[str, Callable[[str], SecretsProvider]] = {"infisical": Infisical}


def _error_message(error: urllib.error.HTTPError) -> str:
    try:
        data = json.loads(error.read() or b"{}")
    except (ValueError, OSError):
        data = {}
    message = data.get("message") if isinstance(data, dict) else None
    return str(message or error.reason or "request failed")


def _pairs(items: Iterable[Any], values: bool = True) -> dict[str, str]:
    pairs: dict[str, str] = {}
    for item in items:
        if isinstance(item, dict) and isinstance(item.get("secretKey"), str):
            pairs[item["secretKey"]] = str(item.get("secretValue") or "") if values else ""
    return pairs


def normalize_path(path: str) -> str:
    parts = [part for part in path.strip().split("/") if part]
    if any(part in {".", ".."} for part in parts):
        raise SecretsError(f"bad secret path {path!r}")
    return "/" + "/".join(parts)


def _unquote_double(text: str, number: int) -> str:
    out: list[str] = []
    index = 0
    while index < len(text):
        char = text[index]
        if char == "\\" and index + 1 < len(text):
            out.append(DOUBLE_ESCAPES.get(text[index + 1], "\\" + text[index + 1]))
            index += 2
            continue
        if char == '"':
            rest = text[index + 1 :].strip()
            if rest and not rest.startswith("#"):
                raise SecretsError(f"line {number}: text after the closing quote")
            return "".join(out)
        out.append(char)
        index += 1
    raise SecretsError(f"line {number}: unterminated double quote")


def parse_value(raw: str, number: int) -> str:
    if raw.startswith("'"):
        end = raw.find("'", 1)
        if end < 0:
            raise SecretsError(f"line {number}: unterminated single quote")
        rest = raw[end + 1 :].strip()
        if rest and not rest.startswith("#"):
            raise SecretsError(f"line {number}: text after the closing quote")
        return raw[1:end]
    if raw.startswith('"'):
        return _unquote_double(raw[1:], number)
    comment = re.search(r"\s#", raw)
    return (raw[: comment.start()] if comment else raw).strip()


def parse_env(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = LINE.match(stripped)
        if match is None:
            raise SecretsError(f"line {number} is not KEY=value")
        values[match.group(1)] = parse_value(match.group(2), number)
    return values


def format_value(key: str, value: str) -> str:
    if SAFE.match(value):
        return value
    if "'" not in value and "\n" not in value and "\r" not in value:
        return f"'{value}'"
    raise SecretsError(
        f"{key}: the value holds a quote or a line break, which a .env line cannot carry safely"
    )


def env_text(values: Mapping[str, str]) -> str:
    lines = []
    for key in sorted(values):
        if not KEY.match(key):
            raise SecretsError(f"{key!r} is not a valid variable name")
        lines.append(f"{key}={format_value(key, values[key])}\n")
    return "".join(lines)


def write_private(path: Path, text: str) -> None:
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp = Path(name)
    try:
        temp.chmod(0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        temp.replace(path)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise


def read_identity_file(path: Path) -> dict[str, str]:
    if os.name == "posix" and path.stat().st_mode & 0o077:
        raise SecretsError(f"{path} is readable by others, chmod 600 it")
    values = parse_env(path.read_text(encoding="utf-8"))
    return {key: value for key, value in values.items() if key in IDENTITY_KEYS}


def identity_from(
    args: argparse.Namespace, environ: Mapping[str, str], file_values: Mapping[str, str]
) -> Identity:
    found: dict[str, str] = {}
    for field in IDENTITY_FIELDS:
        name = f"{PREFIX}{field.upper()}"
        flag = getattr(args, field, None)
        found[field] = str(flag or environ.get(name) or file_values.get(name) or "")
    missing = [f"{PREFIX}{field.upper()}" for field in REQUIRED if not found[field]]
    if missing:
        raise SecretsError(f"set {', '.join(missing)}")
    if not re.match(r"^https?://", found["url"]):
        raise SecretsError(f"{PREFIX}URL must start with https:// or http://")
    scope = Scope(found["project_id"], found["environment"], normalize_path(found["path"] or "/"))
    return Identity(found["url"], found["client_id"], found["client_secret"], scope)


def where(scope: Scope) -> str:
    return f"{scope.environment}:{scope.path}"


def pull(provider: SecretsProvider, identity: Identity, out: Path) -> str:
    provider.login(identity.client_id, identity.client_secret)
    values = provider.read(identity.scope)
    write_private(out, env_text(values))
    return f"{len(values)} secret(s) from {where(identity.scope)} written to {out}"


def push(provider: SecretsProvider, identity: Identity, source: Path) -> str:
    values = parse_env(source.read_text(encoding="utf-8"))
    provider.login(identity.client_id, identity.client_secret)
    try:
        existing = provider.keys(identity.scope)
    except FolderMissing:
        provider.ensure_folder(identity.scope)
        existing = set()
    missing = {key: value for key, value in values.items() if key not in existing}
    if missing:
        provider.create(identity.scope, missing)
    kept = len(values) - len(missing)
    added = ", ".join(sorted(missing)) or "nothing"
    return f"{where(identity.scope)}: added {added}; kept {kept} existing value(s)"


def announce(message: str) -> None:
    print(f"secrets: open {message} and paste the key", flush=True)


def existing_keys(provider: SecretsProvider, scope: Scope) -> set[str]:
    try:
        return provider.keys(scope)
    except FolderMissing:
        return set()


def store(
    provider: SecretsProvider,
    identity: Identity,
    key: str,
    read_value: Callable[[], str],
    replace: bool,
    check: bool,
    refusal: str = "pass --replace",
) -> str:
    if not KEY.match(key):
        raise SecretsError(f"{key!r} is not a valid key name")
    provider.login(identity.client_id, identity.client_secret)
    exists = key in existing_keys(provider, identity.scope)
    if exists and not replace:
        raise SecretsError(f"{key} already exists in {where(identity.scope)}, {refusal}")
    value = read_value()
    if not value:
        raise SecretsError("the value is empty, nothing stored")
    if check:
        secretkeys.validate(key, value)
    if exists:
        provider.update(identity.scope, key, value)
    else:
        provider.ensure_folder(identity.scope)
        provider.create(identity.scope, {key: value})
    return secretkeys.describe(key, where(identity.scope), value, created=not exists)


def gen_shape(
    key: str, kind: str | None, size: int | None, catalog: Mapping[str, Generated]
) -> tuple[str, int]:
    found = catalog.get(key)
    if found is None:
        return kind or "base64url", secretkeys.DEFAULT_BYTES if size is None else size
    if found.copied:
        raise SecretsError(
            f"{key} is copied from another AyeOps host ({found.module} module), never generated;"
            " aye permissions names its source"
        )
    declared = f"{key} is {found.shape()} in the {found.module} module"
    if kind is not None and kind != found.format:
        raise SecretsError(f"{declared}, drop --format")
    if size is not None and size != found.size:
        raise SecretsError(f"{declared}, drop --bytes")
    return found.format, found.size or 0


def load_host(manifest_path: Path, host: str | None) -> tuple[Manifest, dict[str, Module], str]:
    try:
        manifest = load_manifest(manifest_path)
    except yaml.YAMLError:
        raise SecretsError(f"{manifest_path} is not valid YAML, run aye check first") from None
    modules = with_local_modules(manifest, load_modules())
    if check_manifest(manifest, modules):
        raise SecretsError(f"{manifest_path} has errors, run aye check first")
    if host is None and len(manifest.hosts) == 1:
        host = next(iter(manifest.hosts))
    if host is None or host not in manifest.hosts:
        known = ", ".join(manifest.hosts)
        raise SecretsError(f"--host must name a host of {manifest_path}: {known}")
    return manifest, modules, host


def host_secrets(manifest_path: Path, host: str | None) -> list[Generated]:
    manifest, modules, name = load_host(manifest_path, host)
    return host_generated(manifest, modules, name)


def host_variables(manifest: Manifest, modules: dict[str, Module], host: str) -> list[str]:
    return sorted(parse_env(host_tree(manifest, host, modules).get(".env.example", "")))


def read_env_file(path: Path) -> str:
    shown = path.as_posix()
    if path.is_dir():
        raise SecretsError(f"{shown} is a directory, name the .env file inside it")
    if not path.parent.is_dir():
        raise SecretsError(f"{path.parent.as_posix()} does not exist, run aye render first")
    if not path.exists():
        return ""
    try:
        return path.read_bytes().decode("utf-8-sig").replace("\r\n", "\n")
    except UnicodeDecodeError:
        raise SecretsError(f"{shown} is not UTF-8, nothing written") from None


def env_keys(path: Path, text: str) -> set[str]:
    try:
        return set(parse_env(text))
    except SecretsError as error:
        raise SecretsError(f"{path.as_posix()}: {error}, nothing written") from None


def appended(text: str, values: Mapping[str, str]) -> str:
    if text and not text.endswith("\n"):
        text += "\n"
    return text + env_text(values)


@dataclass(frozen=True)
class EnvFill:
    lines: list[str]
    unset: list[str]

    def text(self) -> str:
        return "\n".join(self.lines)


def gen_env_file(path: Path, manifest_path: Path, host: str | None) -> str:
    return fill_env_file(path, manifest_path, host).text()


def fill_env_file(path: Path, manifest_path: Path, host: str | None) -> EnvFill:
    text = read_env_file(path)
    present = env_keys(path, text)
    manifest, modules, name = load_host(manifest_path, host)
    wanted = host_generated(manifest, modules, name)
    missing = [item for item in wanted if item.env not in present]
    fresh = {
        item.env: secretkeys.generate(item.size or 0, item.format)
        for item in missing
        if not item.copied
    }
    if fresh:
        write_private(path, appended(text, fresh))
    shown = path.as_posix()
    lines = [secretkeys.describe(key, shown, value, created=True) for key, value in fresh.items()]
    kept = len(wanted) - len(missing)
    lines.append(f"{shown}: generated {len(fresh)}, kept {kept} existing value(s)")
    copied = [item for item in missing if item.copied]
    lines.extend(f"{item.env} in {shown}: missing, copy {item.source}" for item in copied)
    names = {item.env for item in wanted}
    foreign = [
        key
        for key in host_variables(manifest, modules, name)
        if key not in names and key not in present
    ]
    lines.extend(f"{key} in {shown}: missing, not generated, you must set it" for key in foreign)
    return EnvFill(lines, [item.env for item in copied] + foreign)


def gen_local(args: argparse.Namespace) -> str:
    if args.key is not None or args.format is not None or args.size is not None:
        raise SecretsError(
            "--manifest generates what the catalog asks; drop KEY, --format and --bytes"
        )
    given = [flag for field, flag in PROVIDER_FLAGS.items() if getattr(args, field, None)]
    if given:
        raise SecretsError(
            f"--env-file writes a local file and talks to no provider; drop {', '.join(given)}"
        )
    try:
        manifest_path = find_manifest(args.manifest)
    except UsageError as error:
        raise SecretsError(f"--env-file needs a manifest; {error}") from None
    return gen_env_file(args.env_file, manifest_path, args.host)


def gen_missing(provider: SecretsProvider, identity: Identity, wanted: list[Generated]) -> str:
    provider.login(identity.client_id, identity.client_secret)
    existing = existing_keys(provider, identity.scope)
    missing = [item for item in wanted if item.env not in existing]
    fresh = {
        item.env: secretkeys.generate(item.size or 0, item.format)
        for item in missing
        if not item.copied
    }
    if fresh:
        provider.ensure_folder(identity.scope)
        provider.create(identity.scope, fresh)
    folder = where(identity.scope)
    lines = [secretkeys.describe(key, folder, value, created=True) for key, value in fresh.items()]
    kept = len(wanted) - len(missing)
    lines.append(f"{folder}: generated {len(fresh)}, kept {kept} existing value(s)")
    lines.extend(
        f"{item.env} in {folder}: missing, copy {item.source}" for item in missing if item.copied
    )
    return "\n".join(lines)


def gen(provider: SecretsProvider, identity: Identity, args: argparse.Namespace) -> str:
    if args.manifest is not None:
        if args.key is not None or args.format is not None or args.size is not None:
            raise SecretsError(
                "--manifest generates what the catalog asks; drop KEY, --format and --bytes"
            )
        return gen_missing(provider, identity, host_secrets(args.manifest, args.host))
    if args.key is None:
        try:
            manifest_path = find_manifest(None)
        except UsageError as error:
            raise SecretsError(f"gen needs a KEY, or a manifest for its secrets; {error}") from None
        return gen_missing(provider, identity, host_secrets(manifest_path, args.host))
    if args.host is not None:
        raise SecretsError("--host goes with --manifest")
    kind, size = gen_shape(args.key, args.format, args.size, catalog_generated(load_modules()))
    return store(
        provider,
        identity,
        args.key,
        lambda: secretkeys.generate(size, kind),
        False,
        False,
        f"gen never overwrites; aye secrets set {args.key} --replace changes it",
    )


def read_hidden(key: str) -> str:
    return getpass.getpass(f"value for {key} (hidden): ").strip()


def read_from_clipboard() -> str:
    text, clearer = secretkeys.read_clipboard()
    secretkeys.clear_clipboard(clearer)
    return text.strip()


def run_with(
    provider: SecretsProvider,
    identity: Identity,
    command: list[str],
    environ: Mapping[str, str],
) -> int:
    provider.login(identity.client_id, identity.client_secret)
    values = provider.read(identity.scope)
    drop = [*IDENTITY_KEYS]
    env = secretkeys.child_env(environ, drop)
    env.update(values)
    return secretkeys.run_masked(command, env, [*values.values(), identity.client_secret])


def add_parser(commands: Any) -> None:
    parser: argparse.ArgumentParser = commands.add_parser(
        "secrets", help="fetch a .env from the secrets manager, or import one into it"
    )
    actions = parser.add_subparsers(dest="action", required=True)
    pull_parser = actions.add_parser("pull", help="write the secrets of one folder to a .env, 600")
    pull_parser.add_argument("-o", "--out", type=Path, default=Path(".env"))
    push_parser = actions.add_parser(
        "push", help="add the keys of a .env that the folder does not have yet"
    )
    push_parser.add_argument("source", type=Path)
    set_parser = actions.add_parser(
        "set", help="store one key typed hidden or pasted from the clipboard"
    )
    set_parser.add_argument("key")
    set_parser.add_argument("--from-clipboard", action="store_true")
    ask_parser = actions.add_parser(
        "ask", help="serve a one-time local page where a person pastes a key"
    )
    ask_parser.add_argument("key")
    ask_parser.add_argument("--hint", default="")
    for sub in (set_parser, ask_parser):
        sub.add_argument("--replace", action="store_true", help="overwrite an existing key")
        sub.add_argument(
            "--no-check", action="store_true", help="skip the provider check of the key"
        )
    gen_parser = actions.add_parser(
        "gen", help="generate a random key in its catalog format, never overwriting"
    )
    gen_parser.add_argument("key", nargs="?")
    gen_parser.add_argument("--bytes", type=int, dest="size")
    gen_parser.add_argument("--format", choices=secretkeys.FORMATS)
    gen_parser.add_argument(
        "--manifest",
        type=Path,
        help="generate every missing generated secret of --host; without KEY the default"
        " manifest, $AYE_MANIFEST, else ayeops.yaml or manifest.yaml here",
    )
    gen_parser.add_argument("--host", help="a host of the manifest, default its only host")
    gen_parser.add_argument(
        "--env-file",
        type=Path,
        help="write the missing secrets of --host to this local .env, 600, with no provider",
    )
    run_parser = actions.add_parser(
        "run", help="run a command with the folder in its environment, output masked"
    )
    run_parser.add_argument("argv", nargs=argparse.REMAINDER)
    everyone = (pull_parser, push_parser, set_parser, ask_parser, gen_parser, run_parser)
    for sub in everyone:
        sub.add_argument("--identity", type=Path, help="600 file with the AYE_SECRETS_* variables")
        sub.add_argument("--url", help="site URL, default AYE_SECRETS_URL")
        sub.add_argument("--project", dest="project_id", help="default AYE_SECRETS_PROJECT_ID")
        sub.add_argument("-e", "--environment", help="default AYE_SECRETS_ENVIRONMENT")
        sub.add_argument(
            "--path", "--folder", dest="path", help="folder, default AYE_SECRETS_PATH or /"
        )
        sub.add_argument("--provider", choices=sorted(PROVIDERS), default="infisical")


def act(provider: SecretsProvider, identity: Identity, args: argparse.Namespace) -> str:
    if args.action == "pull":
        return pull(provider, identity, args.out)
    if args.action == "push":
        return push(provider, identity, args.source)
    if args.action == "gen":
        return gen(provider, identity, args)
    check = not args.no_check
    if args.action == "ask":
        return store(
            provider,
            identity,
            args.key,
            lambda: secretkeys.ask(args.key, args.hint, announce, secretkeys.ASK_SECONDS),
            args.replace,
            check,
        )
    reader = read_from_clipboard if args.from_clipboard else lambda: read_hidden(args.key)
    return store(provider, identity, args.key, reader, args.replace, check)


def run(
    args: argparse.Namespace,
    environ: Mapping[str, str] | None = None,
    providers: Mapping[str, Callable[[str], SecretsProvider]] | None = None,
) -> int:
    environ = os.environ if environ is None else environ
    providers = PROVIDERS if providers is None else providers
    if args.action == "gen" and args.env_file is not None:
        try:
            return report(gen_local(args))
        except (OSError, SecretsError, secretkeys.KeyProblem) as error:
            print(f"secrets: {error}", file=sys.stderr)
            return 1
    try:
        file_values = read_identity_file(args.identity) if args.identity else {}
        identity = identity_from(args, environ, file_values)
    except (OSError, SecretsError) as error:
        print(f"secrets: {error}", file=sys.stderr)
        return 2
    provider = providers[args.provider](identity.url)
    try:
        if args.action == "run":
            command = args.argv[1:] if args.argv[:1] == ["--"] else args.argv
            if not command:
                print("secrets: run needs a command after --", file=sys.stderr)
                return 2
            return run_with(provider, identity, command, environ)
        message = act(provider, identity, args)
    except (OSError, SecretsError, secretkeys.KeyProblem) as error:
        print(f"secrets: {error}", file=sys.stderr)
        return 1
    return report(message)


def report(message: str) -> int:
    for line in message.splitlines():
        print(f"secrets: {line}")
    return 0
