#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

DOCKER_HUB = "docker.io"
DOCKER_HUB_ALIASES = ("docker.io", "index.docker.io", "registry-1.docker.io")
DOCKER_HUB_API = "registry-1.docker.io"
INDEX_TYPES = (
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
)
ACCEPT = ", ".join(INDEX_TYPES)
DIGEST = re.compile(r"^sha256:[a-f0-9]{64}$")
TAG = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,127}$")
NAME = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)*$")
CHALLENGE_FIELD = re.compile(r'(\w+)="([^"]*)"')
IMAGE_LINE = re.compile(r"^(?P<lead>[ \t]*image:[ \t]*)(?P<ref>[^\s\"'{}$]+)[ \t]*$", re.M)
TRIES = 4
TIMEOUT = 30
WORKERS = 8

Headers = Mapping[str, str]
Response = tuple[int, Headers, bytes]
Fetch = Callable[[urllib.request.Request], Response]
Sleep = Callable[[float], None]


class DigestError(Exception):
    pass


class Unavailable(DigestError):
    pass


@dataclass(frozen=True)
class Reference:
    registry: str
    repository: str
    name: str
    tag: str
    digest: str | None

    @property
    def tagged(self) -> str:
        return f"{self.name}:{self.tag}"

    @property
    def api(self) -> str:
        return DOCKER_HUB_API if self.registry == DOCKER_HUB else self.registry

    def pinned(self, digest: str) -> str:
        return f"{self.tagged}@{digest}"


def is_registry(component: str) -> bool:
    return "." in component or ":" in component or component == "localhost"


def parse(reference: str) -> Reference:
    name, at, digest = reference.strip().partition("@")
    if at and not DIGEST.match(digest):
        raise DigestError(f"{reference}: the digest is not sha256 and 64 hex digits")
    head, _, last = name.rpartition("/")
    base, colon, tag = last.partition(":")
    if not colon or not TAG.match(tag):
        raise DigestError(f"{reference}: no tag; pin an exact version tag")
    path = f"{head}/{base}" if head else base
    first, slash, rest = path.partition("/")
    if slash and is_registry(first):
        registry, repository = first, rest
    else:
        registry, repository = DOCKER_HUB, path
    if registry in DOCKER_HUB_ALIASES:
        registry = DOCKER_HUB
        if "/" not in repository:
            repository = f"library/{repository}"
    if not NAME.match(repository):
        raise DigestError(f"{reference}: {repository!r} is not a repository name")
    return Reference(registry, repository, path, tag, digest or None)


def token_url(challenge: str, repository: str) -> str:
    scheme, _, params = challenge.strip().partition(" ")
    if scheme.lower() != "bearer":
        raise DigestError(f"the registry asks for {scheme or 'no'} authentication, not a token")
    fields = dict(CHALLENGE_FIELD.findall(params))
    realm = fields.get("realm", "")
    if not realm.startswith("https://"):
        raise DigestError(f"the token realm {realm!r} is not an https URL")
    query = {"scope": fields.get("scope") or f"repository:{repository}:pull"}
    if fields.get("service"):
        query = {"service": fields["service"], **query}
    return f"{realm}?{urllib.parse.urlencode(query)}"


def token_from(body: bytes) -> str:
    try:
        data = json.loads(body)
    except ValueError as error:
        raise DigestError("the token answer is not JSON") from error
    token = data.get("token") or data.get("access_token") if isinstance(data, dict) else None
    if not isinstance(token, str) or not token:
        raise DigestError("the token answer carries no token")
    return token


def lowered(headers: Iterable[tuple[str, str]]) -> dict[str, str]:
    return {key.lower(): value for key, value in headers}


def http_fetch(request: urllib.request.Request) -> Response:
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return response.status, lowered(response.headers.items()), response.read()
    except urllib.error.HTTPError as error:
        return error.code, lowered(error.headers.items()), error.read()
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise Unavailable(f"{request.full_url}: {error}") from error


def failed(reference: Reference, status: int, what: str) -> DigestError:
    message = f"{reference.tagged}: the registry answered {status} to the {what}"
    if status == 429 or status >= 500:
        return Unavailable(message)
    return DigestError(message)


def head(reference: Reference, token: str | None) -> urllib.request.Request:
    headers = {"Accept": ACCEPT}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    url = f"https://{reference.api}/v2/{reference.repository}/manifests/{reference.tag}"
    return urllib.request.Request(url, method="HEAD", headers=headers)


def index_digest(reference: Reference, headers: Headers) -> str:
    media = headers.get("content-type", "").split(";", 1)[0].strip()
    if media not in INDEX_TYPES:
        raise DigestError(
            f"{reference.tagged}: {media or 'no media type'} is not a multi-arch index; "
            "the kit pins one digest for linux/amd64 and linux/arm64"
        )
    digest = headers.get("docker-content-digest", "")
    if not DIGEST.match(digest):
        raise DigestError(f"{reference.tagged}: the registry sent no sha256 digest")
    return digest


def resolve_once(reference: Reference, fetch: Fetch) -> str:
    status, headers, _ = fetch(head(reference, None))
    if status == 401:
        url = token_url(headers.get("www-authenticate", ""), reference.repository)
        token_status, _, body = fetch(urllib.request.Request(url))
        if token_status != 200:
            raise failed(reference, token_status, "token request")
        status, headers, _ = fetch(head(reference, token_from(body)))
    if status != 200:
        raise failed(reference, status, "manifest request")
    return index_digest(reference, headers)


def resolve(
    reference: Reference, fetch: Fetch = http_fetch, sleep: Sleep = time.sleep, tries: int = TRIES
) -> str:
    for attempt in range(1, tries + 1):
        try:
            return resolve_once(reference, fetch)
        except Unavailable as error:
            if attempt == tries:
                raise
            print(f"{error}, attempt {attempt}", file=sys.stderr)
            sleep(attempt * 15)
    raise AssertionError("unreachable")


def check(reference: Reference, current: str) -> str | None:
    if reference.digest is None:
        return f"{reference.tagged}: not pinned by digest; pin it as {reference.pinned(current)}"
    if reference.digest != current:
        return (
            f"{reference.tagged}: the tag now points to {current}, the pin says "
            f"{reference.digest}; the tag was pushed again, find out why before you pin it"
        )
    return None


def references_in(text: str) -> list[str]:
    return [match.group("ref") for match in IMAGE_LINE.finditer(text)]


def repinned(text: str, digests: Mapping[str, str]) -> str:
    def swap(match: re.Match[str]) -> str:
        reference = parse(match.group("ref"))
        return f"{match.group('lead')}{reference.pinned(digests[reference.tagged])}"

    return IMAGE_LINE.sub(swap, text)


def resolve_all(
    references: Iterable[Reference], fetch: Fetch = http_fetch, sleep: Sleep = time.sleep
) -> dict[str, str | DigestError]:
    unique = {reference.tagged: reference for reference in references}

    def one(reference: Reference) -> str | DigestError:
        try:
            return resolve(reference, fetch, sleep)
        except DigestError as error:
            return error

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        results = list(pool.map(one, unique.values()))
    return dict(zip(unique, results, strict=True))


def parsed(texts: Iterable[str]) -> tuple[list[Reference], list[str]]:
    references: list[Reference] = []
    problems: list[str] = []
    for text in texts:
        try:
            references.append(parse(text))
        except DigestError as error:
            problems.append(str(error))
    return references, problems


def run(args: argparse.Namespace, fetch: Fetch, sleep: Sleep) -> int:
    texts = {Path(path): Path(path).read_text(encoding="utf-8") for path in args.file}
    found = [reference for text in texts.values() for reference in references_in(text)]
    references, problems = parsed([*args.references, *found])
    results = resolve_all(references, fetch, sleep)
    digests = {tagged: value for tagged, value in results.items() if isinstance(value, str)}
    problems += [str(value) for value in results.values() if isinstance(value, DigestError)]
    if args.check:
        for reference in references:
            if reference.tagged in digests:
                problem = check(reference, digests[reference.tagged])
                if problem:
                    problems.append(problem)
    elif args.write:
        if not problems:
            for path, text in texts.items():
                updated = repinned(text, digests)
                if updated != text:
                    path.write_text(updated, encoding="utf-8", newline="\n")
                    print(f"pinned {path}")
    else:
        for tagged, digest in digests.items():
            print(f"{tagged}@{digest}")
    for problem in dict.fromkeys(problems):
        print(problem, file=sys.stderr)
    return 1 if problems else 0


def arguments(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Print the multi-arch index digest of image references, check pins, repin files"
    )
    parser.add_argument("references", nargs="*", help="name:tag or name:tag@sha256:digest")
    parser.add_argument(
        "--file",
        action="append",
        default=[],
        metavar="FILE",
        help="also take the reference of every image: line of FILE",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--check",
        action="store_true",
        help="fail when a reference has no digest or its tag now points to another digest",
    )
    mode.add_argument(
        "--write",
        action="store_true",
        help="rewrite every image: line of each --file to name:tag@digest of its tag",
    )
    args = parser.parse_args(argv)
    if args.write and not args.file:
        parser.error("--write needs at least one --file")
    if not args.references and not args.file:
        parser.error("name at least one reference or --file FILE")
    return args


def main(
    argv: Sequence[str] | None = None, fetch: Fetch = http_fetch, sleep: Sleep = time.sleep
) -> int:
    return run(arguments(argv), fetch, sleep)


if __name__ == "__main__":
    sys.exit(main())
