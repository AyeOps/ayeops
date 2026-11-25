from __future__ import annotations

import importlib.util
import json
import sys
import urllib.request
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

from tests.conftest import ROOT

SCRIPT = ROOT / "ci" / "image_digest.py"
FIXTURES = ROOT / "tests" / "fixtures" / "image_digest"
TRIVY = "sha256:790fd66d1697395d8693d1acaa1ea702406a20f109b6fc7bac8db623eca6c06c"
OAUTH2_PROXY = "sha256:8e4bec4f9fe4de10de63070209204075e4407503170aa19893d6e8d99f083d1b"
OTHER = "sha256:" + "0" * 64
TRIVY_URL = "https://registry-1.docker.io/v2/aquasec/trivy/manifests/0.76.1"
TRIVY_TOKEN_URL = (
    "https://auth.docker.io/token?service=registry.docker.io"
    "&scope=repository%3Aaquasec%2Ftrivy%3Apull"
)
QUAY_URL = "https://quay.io/v2/oauth2-proxy/oauth2-proxy/manifests/v7.15.5-alpine"
TOKEN = json.dumps({"token": "anonymous-pull", "expires_in": 300}).encode()


def load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("image_digest", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


digest = load_script()
Response = tuple[int, dict[str, str], bytes]


def captured(name: str, body: bytes = b"") -> Response:
    lines = (FIXTURES / name).read_text(encoding="utf-8").splitlines()
    fields = (line.partition(":") for line in lines[1:] if line)
    headers = {key.strip().lower(): value.strip() for key, _, value in fields}
    return int(lines[0].split()[1]), headers, body


class Registry:
    def __init__(self, answers: dict[str, list[Response]]) -> None:
        self.answers = answers
        self.requests: list[urllib.request.Request] = []

    def __call__(self, request: urllib.request.Request) -> Response:
        self.requests.append(request)
        queue = self.answers[request.full_url]
        return queue.pop(0) if len(queue) > 1 else queue[0]

    def to(self, url: str) -> list[urllib.request.Request]:
        return [request for request in self.requests if request.full_url == url]


def docker_hub(index: str = "docker-hub-index.http") -> Registry:
    return Registry(
        {
            TRIVY_URL: [captured("docker-hub-challenge.http"), captured(index)],
            TRIVY_TOKEN_URL: [(200, {}, TOKEN)],
        }
    )


def no_sleep(seconds: float) -> None:
    raise AssertionError(f"slept {seconds}")


@pytest.mark.parametrize(
    ("reference", "registry", "repository", "name", "tag", "pin"),
    [
        (
            "python:3.13.16-alpine3.23",
            "docker.io",
            "library/python",
            "python",
            "3.13.16-alpine3.23",
            None,
        ),
        (
            f"aquasec/trivy:0.76.1@{TRIVY}",
            "docker.io",
            "aquasec/trivy",
            "aquasec/trivy",
            "0.76.1",
            TRIVY,
        ),
        (
            "ghcr.io/google/cadvisor:v0.60.6",
            "ghcr.io",
            "google/cadvisor",
            "ghcr.io/google/cadvisor",
            "v0.60.6",
            None,
        ),
        (
            "quay.io/oauth2-proxy/oauth2-proxy:v7.15.5-alpine",
            "quay.io",
            "oauth2-proxy/oauth2-proxy",
            "quay.io/oauth2-proxy/oauth2-proxy",
            "v7.15.5-alpine",
            None,
        ),
        (
            "docker.io/library/docker:29.9.1-dind",
            "docker.io",
            "library/docker",
            "docker.io/library/docker",
            "29.9.1-dind",
            None,
        ),
        (
            "index.docker.io/busybox:1.37.0",
            "docker.io",
            "library/busybox",
            "index.docker.io/busybox",
            "1.37.0",
            None,
        ),
        (
            "registry.local:5000/tool:1.0",
            "registry.local:5000",
            "tool",
            "registry.local:5000/tool",
            "1.0",
            None,
        ),
        ("localhost/tool:1.0", "localhost", "tool", "localhost/tool", "1.0", None),
    ],
)
def test_a_reference_splits_into_registry_repository_tag_and_digest(
    reference: str, registry: str, repository: str, name: str, tag: str, pin: str | None
) -> None:
    parsed = digest.parse(reference)
    assert (parsed.registry, parsed.repository, parsed.name, parsed.tag, parsed.digest) == (
        registry,
        repository,
        name,
        tag,
        pin,
    )
    assert parsed.tagged == f"{name}:{tag}"


def test_docker_hub_is_asked_at_its_registry_host_and_others_at_their_own() -> None:
    assert digest.parse("python:3.13.16").api == "registry-1.docker.io"
    assert digest.parse("ghcr.io/google/cadvisor:v0.60.6").api == "ghcr.io"


@pytest.mark.parametrize(
    ("reference", "says"),
    [
        ("python", "no tag"),
        (f"python@{TRIVY}", "no tag"),
        ("registry.local:5000/tool", "no tag"),
        ("python:3.13@sha256:abc", "not sha256"),
        ("python:3.13@md5:" + "a" * 32, "not sha256"),
        ("Python:3.13", "not a repository name"),
        ("python:-3.13", "no tag"),
    ],
)
def test_a_reference_without_an_exact_tag_or_with_a_bad_digest_is_refused(
    reference: str, says: str
) -> None:
    with pytest.raises(digest.DigestError, match=says):
        digest.parse(reference)


def test_the_token_url_comes_from_the_challenge_of_docker_hub() -> None:
    _, headers, _ = captured("docker-hub-challenge.http")
    assert digest.token_url(headers["www-authenticate"], "aquasec/trivy") == TRIVY_TOKEN_URL


def test_the_token_url_comes_from_the_challenge_of_ghcr() -> None:
    _, headers, _ = captured("ghcr-challenge.http")
    assert digest.token_url(headers["www-authenticate"], "google/cadvisor") == (
        "https://ghcr.io/token?service=ghcr.io&scope=repository%3Agoogle%2Fcadvisor%3Apull"
    )


def test_a_challenge_without_scope_asks_for_pull_of_the_repository() -> None:
    url = digest.token_url('Bearer realm="https://auth.example/token"', "acme/tool")
    assert url == "https://auth.example/token?scope=repository%3Aacme%2Ftool%3Apull"


@pytest.mark.parametrize(
    "challenge",
    ['Basic realm="registry"', 'Bearer realm="http://auth.example/token"', "Bearer", ""],
)
def test_a_challenge_that_is_not_an_https_bearer_realm_is_refused(challenge: str) -> None:
    with pytest.raises(digest.DigestError):
        digest.token_url(challenge, "acme/tool")


def test_the_token_is_read_from_token_or_access_token() -> None:
    assert digest.token_from(TOKEN) == "anonymous-pull"
    assert digest.token_from(b'{"access_token": "other"}') == "other"
    for body in (b"<html>", b"[]", b'{"token": ""}'):
        with pytest.raises(digest.DigestError):
            digest.token_from(body)


def test_docker_hub_answers_the_index_digest_after_an_anonymous_token() -> None:
    registry = docker_hub()
    assert digest.resolve(digest.parse("aquasec/trivy:0.76.1"), registry, no_sleep) == TRIVY
    first, second = registry.to(TRIVY_URL)
    assert first.get_method() == second.get_method() == "HEAD"
    assert not first.has_header("Authorization")
    assert second.get_header("Authorization") == "Bearer anonymous-pull"
    accept = second.get_header("Accept") or ""
    assert "application/vnd.oci.image.index.v1+json" in accept
    assert "application/vnd.docker.distribution.manifest.list.v2+json" in accept
    assert len(registry.to(TRIVY_TOKEN_URL)) == 1


def test_a_registry_that_answers_without_a_token_needs_none() -> None:
    registry = Registry({QUAY_URL: [captured("quay-index.http")]})
    reference = digest.parse("quay.io/oauth2-proxy/oauth2-proxy:v7.15.5-alpine")
    assert digest.resolve(reference, registry, no_sleep) == OAUTH2_PROXY
    assert len(registry.requests) == 1


def test_a_single_platform_manifest_is_not_pinned() -> None:
    registry = Registry({QUAY_URL: [captured("quay-platform-manifest.http")]})
    reference = digest.parse("quay.io/oauth2-proxy/oauth2-proxy:v7.15.5-alpine")
    with pytest.raises(digest.DigestError, match="not a multi-arch index"):
        digest.resolve(reference, registry, no_sleep)


def test_a_missing_tag_fails_at_once_without_a_retry() -> None:
    registry = docker_hub("docker-hub-missing.http")
    with pytest.raises(digest.DigestError, match="answered 404") as raised:
        digest.resolve(digest.parse("aquasec/trivy:0.76.1"), registry, no_sleep)
    assert not isinstance(raised.value, digest.Unavailable)


def test_an_answer_without_a_digest_is_refused() -> None:
    status, headers, _ = captured("quay-index.http")
    del headers["docker-content-digest"]
    registry = Registry({QUAY_URL: [(status, headers, b"")]})
    with pytest.raises(digest.DigestError, match="no sha256 digest"):
        digest.resolve(digest.parse("quay.io/oauth2-proxy/oauth2-proxy:v7.15.5-alpine"), registry)


def test_a_busy_registry_is_asked_again_with_a_growing_pause() -> None:
    busy: Response = (503, {}, b"")
    registry = Registry({QUAY_URL: [busy, (429, {}, b""), captured("quay-index.http")]})
    slept: list[float] = []
    reference = digest.parse("quay.io/oauth2-proxy/oauth2-proxy:v7.15.5-alpine")
    assert digest.resolve(reference, registry, slept.append) == OAUTH2_PROXY
    assert slept == [15, 30]


def test_a_registry_busy_four_times_fails_as_unavailable() -> None:
    registry = Registry({QUAY_URL: [(502, {}, b"")]})
    slept: list[float] = []
    reference = digest.parse("quay.io/oauth2-proxy/oauth2-proxy:v7.15.5-alpine")
    with pytest.raises(digest.Unavailable):
        digest.resolve(reference, registry, slept.append)
    assert slept == [15, 30, 45]
    assert len(registry.requests) == 4


def test_a_refused_token_request_is_reported() -> None:
    registry = Registry(
        {TRIVY_URL: [captured("docker-hub-challenge.http")], TRIVY_TOKEN_URL: [(403, {}, b"")]}
    )
    with pytest.raises(digest.DigestError, match="403 to the token request"):
        digest.resolve(digest.parse("aquasec/trivy:0.76.1"), registry, no_sleep)


def test_a_pin_that_matches_the_tag_passes_the_check() -> None:
    assert digest.check(digest.parse(f"aquasec/trivy:0.76.1@{TRIVY}"), TRIVY) is None


def test_a_tag_pushed_again_fails_the_check_with_both_digests() -> None:
    problem = digest.check(digest.parse(f"aquasec/trivy:0.76.1@{OTHER}"), TRIVY)
    assert problem is not None
    assert problem.startswith("aquasec/trivy:0.76.1: the tag now points to " + TRIVY)
    assert OTHER in problem and "pushed again" in problem


def test_a_reference_without_a_digest_fails_the_check_with_the_pin_to_use() -> None:
    problem = digest.check(digest.parse("aquasec/trivy:0.76.1"), TRIVY)
    assert problem == (
        f"aquasec/trivy:0.76.1: not pinned by digest; pin it as aquasec/trivy:0.76.1@{TRIVY}"
    )


def test_main_prints_each_tag_once_with_its_digest(capsys: pytest.CaptureFixture[str]) -> None:
    code = digest.main(
        ["aquasec/trivy:0.76.1", f"aquasec/trivy:0.76.1@{OTHER}"], docker_hub(), no_sleep
    )
    assert code == 0
    assert capsys.readouterr().out == f"aquasec/trivy:0.76.1@{TRIVY}\n"


def test_main_check_fails_on_a_moved_tag_and_passes_on_a_current_pin(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert digest.main(["--check", f"aquasec/trivy:0.76.1@{TRIVY}"], docker_hub(), no_sleep) == 0
    assert capsys.readouterr().err == ""
    assert digest.main(["--check", f"aquasec/trivy:0.76.1@{OTHER}"], docker_hub(), no_sleep) == 1
    assert "the tag now points to" in capsys.readouterr().err


def test_main_check_reads_the_image_lines_of_a_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    compose = tmp_path / "compose.yaml"
    compose.write_text(
        f"services:\n  vulns:\n    image: aquasec/trivy:0.76.1@{OTHER}\n", encoding="utf-8"
    )
    assert digest.main(["--check", "--file", str(compose)], docker_hub(), no_sleep) == 1
    assert OTHER in capsys.readouterr().err


def test_main_check_reports_a_reference_it_cannot_parse(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert digest.main(["--check", "python"], Registry({}), no_sleep) == 1
    assert "python: no tag" in capsys.readouterr().err


def test_main_write_repins_every_image_line_and_leaves_templated_ones(tmp_path: Path) -> None:
    template = tmp_path / "compose.yaml.j2"
    template.write_text(
        "services:\n"
        "  vulns:\n"
        f"    image: aquasec/trivy:0.76.1@{OTHER}\n"
        "  oauth2-proxy:\n"
        "    image: quay.io/oauth2-proxy/oauth2-proxy:v7.15.5-alpine\n"
        "  app:\n"
        "    image: {{ settings.image }}\n",
        encoding="utf-8",
    )
    registry = docker_hub()
    registry.answers[QUAY_URL] = [captured("quay-index.http")]
    assert digest.main(["--write", "--file", str(template)], registry, no_sleep) == 0
    assert template.read_text(encoding="utf-8") == (
        "services:\n"
        "  vulns:\n"
        f"    image: aquasec/trivy:0.76.1@{TRIVY}\n"
        "  oauth2-proxy:\n"
        f"    image: quay.io/oauth2-proxy/oauth2-proxy:v7.15.5-alpine@{OAUTH2_PROXY}\n"
        "  app:\n"
        "    image: {{ settings.image }}\n"
    )


def test_main_write_leaves_the_file_alone_when_one_image_fails(tmp_path: Path) -> None:
    template = tmp_path / "compose.yaml.j2"
    text = "services:\n  vulns:\n    image: aquasec/trivy:0.76.1\n"
    template.write_text(text, encoding="utf-8")
    assert (
        digest.main(
            ["--write", "--file", str(template)], docker_hub("docker-hub-missing.http"), no_sleep
        )
        == 1
    )
    assert template.read_text(encoding="utf-8") == text


@pytest.mark.parametrize(
    "argv",
    [[], ["--write", "python:3.13.16"], ["--check", "--write", "--file", "x"]],
)
def test_main_refuses_arguments_that_do_nothing_or_contradict(argv: list[str]) -> None:
    with pytest.raises(SystemExit):
        digest.main(argv, Registry({}), no_sleep)


def image_tags_workflow() -> dict[Any, Any]:
    path = ROOT / ".github" / "workflows" / "image-tags.yml"
    data: dict[Any, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data


def test_the_tag_check_runs_weekly_and_on_changes_to_any_image_line() -> None:
    triggers = image_tags_workflow()[True]
    assert triggers["schedule"] == [{"cron": "41 5 * * 1"}]
    watched = {"src/ayeops/modules/*/compose.yaml.j2", "examples/rendered/*/compose.yaml"}
    assert watched <= set(triggers["pull_request"]["paths"])
    assert watched <= set(triggers["push"]["paths"])
    assert "workflow_dispatch" in triggers


def test_the_tag_check_reads_every_template_and_rendered_compose_read_only() -> None:
    workflow = image_tags_workflow()
    assert workflow["permissions"] == {"contents": "read"}
    steps = workflow["jobs"]["digests"]["steps"]
    run = steps[-1]["run"]
    assert "src/ayeops/modules/*/compose.yaml.j2 examples/rendered/*/compose.yaml" in run
    assert "python -I ci/image_digest.py --check" in run
    assert steps[0]["with"]["persist-credentials"] is False


def test_a_pull_request_checks_only_the_pins_it_adds_and_the_week_checks_them_all() -> None:
    steps = image_tags_workflow()["jobs"]["digests"]["steps"]
    changed, full = steps[-2], steps[-1]
    assert changed["if"] == "github.event_name == 'pull_request' || github.event_name == 'push'"
    assert (
        full["if"] == "github.event_name == 'schedule' || github.event_name == 'workflow_dispatch'"
    )
    assert 'git diff --unified=0 "$BASE" HEAD' in changed["run"]
    assert "python -I ci/image_digest.py --check" in changed["run"]
    assert changed["env"]["BASE"] == (
        "${{ github.event.pull_request.base.sha || github.event.before }}"
    )
    assert steps[0]["with"]["fetch-depth"] == 0
