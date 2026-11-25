from __future__ import annotations

import importlib.util
import io
import json
import sys
import zipfile
from collections.abc import Mapping
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

from ayeops.registry import Module
from tests.conftest import EXAMPLE_STAMP, ROOT, manifest_from, render_once, rules_hit

MODULES = ROOT / "src" / "ayeops" / "modules"
FIXTURES = ROOT / "tests" / "fixtures" / "github"
TOKEN = "ghp_secret-token-never-shown"
NOW = 1_792_800_000.0
NOW_MS = int(NOW * 1000)


def load_exporter() -> ModuleType:
    path = MODULES / "github" / "files" / "github_exporter.py"
    spec = importlib.util.spec_from_file_location("github_exporter", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


gx = load_exporter()


def usage_items() -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = json.loads((FIXTURES / "usage.json").read_text("utf-8"))[
        "usageItems"
    ]
    return items


def value(samples: list[Any], name: str, **pairs: str) -> float:
    wanted = gx.labels(**pairs)
    found = [v for n, labels, v in samples if n == name and labels == wanted]
    assert len(found) == 1, (name, pairs, found)
    return float(found[0])


def test_usage_is_summed_per_repository_and_sku() -> None:
    account = gx.Account("org", "acme", 2000.0)
    samples = gx.usage_samples(account, usage_items(), {})
    linux = {"account": "acme", "product": "actions", "sku": "Actions Linux", "unit": "Minutes"}
    assert value(samples, "ayeops_github_usage", repo="acme/shop", **linux) == 133.0
    assert value(samples, "ayeops_github_usage", repo="acme/site", **linux) == 500.0
    assert value(samples, "ayeops_github_usage_net_dollars", account="acme", product="actions") == (
        pytest.approx(0.32)
    )


def test_included_minutes_weigh_windows_twice_and_skip_public_and_larger_runners() -> None:
    account = gx.Account("org", "acme", 2000.0)
    private = gx.usage_samples(account, usage_items(), {"acme/shop": "private"})
    assert value(private, "ayeops_github_included_minutes_used", account="acme") == 133 + 20 + 500
    public_site = gx.usage_samples(account, usage_items(), {"acme/site": "public"})
    assert value(public_site, "ayeops_github_included_minutes_used", account="acme") == 153
    assert value(public_site, "ayeops_github_included_minutes", account="acme") == 2000


def test_user_items_without_an_organization_belong_to_the_user() -> None:
    account = gx.Account("user", "jane", 3000.0)
    item = {"product": "actions", "sku": "Actions macOS", "unitType": "Minutes", "quantity": 3}
    samples = gx.usage_samples(account, [{**item, "repositoryName": "app"}], {})
    assert value(samples, "ayeops_github_included_minutes_used", account="jane") == 30
    assert account.usage_path == "/users/jane/settings/billing/usage"
    assert gx.Account("org", "acme", 1).usage_path == "/organizations/acme/settings/billing/usage"


class FakeHttp:
    def __init__(self, routes: Mapping[str, Any]):
        self.routes = dict(routes)
        self.seen: list[tuple[str, dict[str, str]]] = []
        self.posted: list[tuple[str, bytes]] = []

    def get(self, url: str, headers: Mapping[str, str]) -> tuple[int, Mapping[str, str], bytes]:
        self.seen.append((url, dict(headers)))
        answer = self.routes.get(url)
        if answer is None:
            raise gx.GitHubError("HTTP 404", status=404)
        if isinstance(answer, tuple):
            return answer
        if isinstance(answer, bytes):
            return 200, {}, answer
        return 200, {}, json.dumps(answer).encode()

    def post(self, url: str, body: bytes) -> None:
        self.posted.append((url, body))


def exporter(http: FakeHttp, tmp_path: Path, **config: Any) -> Any:
    settings = {
        "accounts": [gx.Account("org", "acme", 2000.0)],
        "import_url": "http://victoriametrics:8428/api/v1/import/prometheus",
        "state_path": str(tmp_path / "imported.json"),
        **config,
    }
    logs: list[Mapping[str, Any]] = []
    made = gx.Exporter(gx.Config(**settings), gx.Client(TOKEN, http), logs.append, lambda: NOW)
    made.logs = logs
    return made


USAGE = "https://api.github.com/organizations/acme/settings/billing/usage?year=2026&month=10"


def test_a_poll_reads_this_month_and_looks_up_visibility_once_a_day(tmp_path: Path) -> None:
    http = FakeHttp(
        {
            USAGE: {"usageItems": usage_items()},
            "https://api.github.com/repos/acme/shop": {"private": True, "visibility": "private"},
            "https://api.github.com/repos/acme/site": {"private": False, "visibility": "public"},
        }
    )
    made = exporter(http, tmp_path)
    made.refresh()
    made.refresh()
    text = made.metrics()
    assert 'ayeops_github_included_minutes_used{account="acme"} 153' in text
    assert 'ayeops_github_query_success{account="acme"} 1' in text
    assert [url for url, _ in http.seen].count("https://api.github.com/repos/acme/site") == 1
    assert http.seen[0][1]["Authorization"] == f"Bearer {TOKEN}"
    assert TOKEN not in json.dumps(made.logs)


def test_a_failed_usage_query_reports_zero_success_and_logs_no_token(tmp_path: Path) -> None:
    made = exporter(FakeHttp({}), tmp_path)
    made.refresh()
    assert 'ayeops_github_query_success{account="acme"} 0' in made.metrics()
    assert made.logs[0]["event"] == "usage-failed"
    assert TOKEN not in json.dumps(made.logs)


def report_zip(*texts: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for index, text in enumerate(texts):
            archive.writestr(f"report-{index}.prom", text)
    return buffer.getvalue()


def artifact(number: int, name: str = "aye-report-test-ci-1-ab", **extra: Any) -> dict[str, Any]:
    return {
        "id": number,
        "name": name,
        "expired": False,
        "created_at": "2026-10-24T03:00:00Z",
        "archive_download_url": f"https://api.github.com/repos/acme/shop/actions/artifacts/{number}/zip",
        "workflow_run": {"id": 99, "head_branch": "main", "head_sha": "abc"},
        **extra,
    }


ARTIFACTS = "https://api.github.com/repos/acme/shop/actions/artifacts?per_page=100"
GOOD = f'aye_step_success{{project="shop",repo="evil/other",step="ci"}} 1 {NOW_MS - 1000}\n'


def test_report_artifacts_are_imported_once_with_the_repo_label_forced(tmp_path: Path) -> None:
    blob = "https://blob.example/zip?sig=1"
    http = FakeHttp(
        {
            ARTIFACTS: {
                "artifacts": [
                    artifact(1),
                    artifact(2, name="coverage-html"),
                    artifact(3, expired=True),
                    artifact(4, created_at="2026-01-01T00:00:00Z"),
                ]
            },
            artifact(1)["archive_download_url"]: (302, {"Location": blob}, b""),
            blob: report_zip(GOOD, f"node_cpu 1 {NOW_MS}\nnot a line\n"),
        }
    )
    made = exporter(http, tmp_path, accounts=[], reports=["acme/shop"])
    made.refresh()
    made.refresh()
    assert len(http.posted) == 1
    url, body = http.posted[0]
    assert url.endswith("/api/v1/import/prometheus")
    assert body.decode() == (
        f'aye_step_success{{project="shop",repo="acme/shop",step="ci"}} 1 {NOW_MS - 1000}\n'
    )
    blob_headers = next(headers for seen, headers in http.seen if seen == blob)
    assert "Authorization" not in blob_headers
    text = made.metrics()
    assert 'ayeops_github_reports_imported_total{repo="acme/shop"} 1' in text
    assert 'ayeops_github_report_lines_rejected_total{repo="acme/shop"} 2' in text
    assert 'ayeops_github_report_import_success{repo="acme/shop"} 1' in text
    assert json.loads((tmp_path / "imported.json").read_text("utf-8")) == {
        "imported": {"1": gx.iso_ms("2026-10-24T03:00:00Z")}
    }
    again = exporter(http, tmp_path, accounts=[], reports=["acme/shop"])
    again.refresh()
    assert len(http.posted) == 1


def test_a_broken_artifact_marks_the_import_failed_and_is_retried(tmp_path: Path) -> None:
    http = FakeHttp(
        {
            ARTIFACTS: {"artifacts": [artifact(1)]},
            artifact(1)["archive_download_url"]: b"not a zip",
        }
    )
    made = exporter(http, tmp_path, accounts=[], reports=["acme/shop"])
    made.refresh()
    assert 'ayeops_github_report_import_success{repo="acme/shop"} 0' in made.metrics()
    assert made.imported == {}
    assert made.logs[0]["event"] == "import-failed"


def test_an_unreadable_artifact_list_marks_the_import_failed(tmp_path: Path) -> None:
    made = exporter(FakeHttp({}), tmp_path, accounts=[], reports=["acme/shop"])
    made.refresh()
    assert 'ayeops_github_report_import_success{repo="acme/shop"} 0' in made.metrics()


def test_an_unwritable_state_keeps_the_billing_numbers_and_flags_the_import(
    tmp_path: Path,
) -> None:
    http = FakeHttp(
        {
            USAGE: {"usageItems": usage_items()},
            "https://api.github.com/repos/acme/shop": {"private": True, "visibility": "private"},
            "https://api.github.com/repos/acme/site": {"private": False, "visibility": "public"},
            "https://api.github.com/repos/acme/shop/actions/artifacts?per_page=100": {
                "artifacts": []
            },
        }
    )
    made = exporter(
        http, tmp_path, reports=["acme/shop"], state_path=str(tmp_path / "no" / "x.json")
    )
    made.refresh()
    text = made.metrics()
    assert 'ayeops_github_query_success{account="acme"} 1' in text
    assert 'ayeops_github_included_minutes_used{account="acme"} 153' in text
    assert 'ayeops_github_report_import_success{repo="acme/shop"} 0' in text
    assert any(record.get("event") == "state-save-failed" for record in made.logs)


def test_clean_report_keeps_only_recent_aye_series() -> None:
    oldest = NOW_MS - 3600_000
    text = "\n".join(
        [
            "# a comment",
            f'aye_tests{{outcome="passed",test="a, b=\\"c\\""}} 3 {NOW_MS}',
            f"aye_tests_flaky 0 {NOW_MS}",
            f"aye_x 1 {oldest - 1}",
            f"aye_x 1 {NOW_MS + 7200_000}",
            f"aye_x NaNa {NOW_MS}",
            f"go_gc 1 {NOW_MS}",
            f"aye_x{{bad label}} 1 {NOW_MS}",
        ]
    )
    kept, rejected = gx.clean_report(text, "acme/shop", NOW_MS, oldest)
    assert kept == [
        f'aye_tests{{outcome="passed",repo="acme/shop",test="a, b=\\"c\\""}} 3 {NOW_MS}',
        f'aye_tests_flaky{{repo="acme/shop"}} 0 {NOW_MS}',
    ]
    assert rejected == 5


def test_a_zip_bomb_is_refused() -> None:
    big = report_zip("a" * (gx.MAX_ARTIFACT_BYTES + 1))
    with pytest.raises(gx.GitHubError, match="more than 4 MiB"):
        gx.unzip_report(big)


def test_a_redirect_without_https_is_refused() -> None:
    url = "https://api.github.com/x"
    client = gx.Client(TOKEN, FakeHttp({url: (302, {"Location": "http://plain"}, b"")}))
    with pytest.raises(gx.GitHubError, match="https"):
        client.download(url)


def test_config_from_the_environment() -> None:
    config = gx.config_from(
        {
            "AYE_GITHUB_ACCOUNTS": '[{"org": "acme"}, {"user": "jane", "included_minutes": 3000}]',
            "AYE_GITHUB_REPORTS": "acme/shop, acme/site",
            "AYE_GITHUB_LOOKBACK_HOURS": "24",
        }
    )
    assert config.accounts == [
        gx.Account("org", "acme", 2000.0),
        gx.Account("user", "jane", 3000.0),
    ]
    assert config.reports == ["acme/shop", "acme/site"]
    assert config.lookback_hours == 24


def with_github(data: dict[str, Any], settings: dict[str, Any]) -> dict[str, Any]:
    data["hosts"]["central"]["modules"]["github"] = settings
    return data


def test_the_module_renders_the_token_from_the_environment_only(
    data: dict[str, Any], modules: dict[str, Module]
) -> None:
    tree = render_once(manifest_from(data), modules, EXAMPLE_STAMP)
    service = yaml.safe_load(tree["central/compose.yaml"])["services"]["github-exporter"]
    assert service["environment"]["AYE_GITHUB_TOKEN"] == "${AYE_GITHUB_TOKEN:?set AYE_GITHUB_TOKEN}"
    assert json.loads(service["environment"]["AYE_GITHUB_ACCOUNTS"]) == [
        {"org": "acme", "included_minutes": 2000}
    ]
    assert service["environment"]["AYE_GITHUB_REPORTS"] == "acme/shop"
    assert "github-state:/tmp" in service["volumes"]
    assert service["environment"]["AYE_GITHUB_STATE"] == "/tmp/imported.json"
    assert "AYE_GITHUB_TOKEN" in tree["central/.env.example"]


def test_the_module_needs_an_account_or_a_repository(data: dict[str, Any]) -> None:
    assert "module-settings" in rules_hit(with_github(data, {}))
    bad = {"accounts": [{"org": "acme", "user": "jane"}]}
    assert "module-settings" in rules_hit(with_github(data, bad))
    assert "module-settings" in rules_hit(with_github(data, {"reports": ["not a repo"]}))
    assert "module-settings" not in rules_hit(with_github(data, {"accounts": [{"user": "jane"}]}))
