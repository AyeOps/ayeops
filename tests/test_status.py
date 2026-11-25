from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import jsonschema
import pytest
import yaml

from ayeops.cli import main
from ayeops.manifest import load_manifest
from ayeops.status import STATES, seed_sql, sign, slug, status_document
from tests.conftest import ROOT, manifest_from, rules_hit

CONTRACT = ROOT / "cloud" / "status" / "contract"
FIXTURES = CONTRACT / "fixtures"
FIXTURE_MANIFEST = FIXTURES / "acme.yaml"


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def fixture_document() -> dict[str, Any]:
    return status_document(load_manifest(FIXTURE_MANIFEST))


def push_cases(*groups: str) -> list[dict[str, Any]]:
    fixtures = load_json(FIXTURES / "push.json")
    return [case for group in groups for case in fixtures[group]]


def test_fixture_manifest_passes_check() -> None:
    assert main(["check", str(FIXTURE_MANIFEST)]) == 0


def test_components_document_matches_the_shared_fixture() -> None:
    assert fixture_document() == load_json(FIXTURES / "acme.components.json")


def test_components_document_follows_its_schema() -> None:
    jsonschema.validate(fixture_document(), load_json(CONTRACT / "components.schema.json"))


def test_seed_sql_matches_the_shared_fixture() -> None:
    expected = (FIXTURES / "acme.seed.sql").read_text(encoding="utf-8")
    assert seed_sql(fixture_document()) == expected


def test_internal_service_names_never_leave() -> None:
    text = json.dumps(fixture_document(), ensure_ascii=False) + seed_sql(fixture_document())
    for internal in ("api-worker", "billing-cron", "nightly invoices"):
        assert internal not in text


def test_services_sharing_a_title_become_one_component() -> None:
    api = [c for c in fixture_document()["components"] if c["id"] == "api"]
    assert api == [
        {"id": "api", "title": "API", "probes": ["https://api.acme.example/health"], "push": True}
    ]


def test_a_component_without_a_probe_expects_pushes() -> None:
    components = {c["id"]: c for c in fixture_document()["components"]}
    assert components["payments-checkout"]["push"] is True
    assert components["website"]["push"] is False


def test_components_keep_manifest_order() -> None:
    ids = [c["id"] for c in fixture_document()["components"]]
    assert ids == ["website", "api", "payments-checkout", "docs"]


def test_component_id_is_a_slug_of_the_title() -> None:
    assert slug("Payments & checkout") == "payments-checkout"
    assert slug("  API v2 ") == "api-v2"


def test_title_without_latin_letters_needs_an_explicit_id(data: dict[str, Any]) -> None:
    data["services"]["web"]["status"] = {"public": "Сайт"}
    assert "status-component" in rules_hit(data)
    data["services"]["web"]["status"]["id"] = "website"
    assert "status-component" not in rules_hit(data)


def test_one_component_id_cannot_carry_two_titles(data: dict[str, Any]) -> None:
    data["services"]["web"]["status"] = {"public": "Website"}
    data["services"]["web2"] = dict(data["services"]["web"])
    data["services"]["web2"]["status"] = {"public": "Web site", "id": "website"}
    assert "status-component" in rules_hit(data)


def test_a_probe_must_be_https(data: dict[str, Any]) -> None:
    data["services"]["web"]["status"]["probe"] = "http://acme.example"
    assert "schema" in rules_hit(data)


def test_status_page_hostname_must_be_a_hostname(data: dict[str, Any]) -> None:
    data["status_page"]["hostname"] = "https://status.acme.example"
    assert "schema" in rules_hit(data)


def test_tenant_defaults_to_the_project_title_and_english() -> None:
    data = yaml.safe_load(FIXTURE_MANIFEST.read_text(encoding="utf-8"))
    data["status_page"] = {"hostname": "status.acme.example"}
    document = status_document(manifest_from(data))
    assert (document["title"], document["locale"]) == ("acme", "en")


def test_seed_quotes_single_quotes() -> None:
    document = fixture_document()
    document["components"][0]["title"] = "Acme's site"
    assert "'Acme''s site'" in seed_sql(document)


def test_notify_reaches_the_document_with_its_minimum_state() -> None:
    assert fixture_document()["notify"] == {
        "telegram_chat_id": "-1001234567890",
        "min_state": "no_data",
    }


def test_notify_defaults_to_outages_only() -> None:
    data = yaml.safe_load(FIXTURE_MANIFEST.read_text(encoding="utf-8"))
    data["status_page"]["notify"] = {"telegram_chat_id": "100200300"}
    document = status_document(manifest_from(data))
    assert document["notify"] == {"telegram_chat_id": "100200300", "min_state": "outage"}
    assert "'100200300', 'outage', 'acme'," in seed_sql(document)


def test_seed_without_notify_clears_the_chat_and_the_notices() -> None:
    data = yaml.safe_load(FIXTURE_MANIFEST.read_text(encoding="utf-8"))
    del data["status_page"]["notify"]
    document = status_document(manifest_from(data))
    assert document["notify"] is None
    jsonschema.validate(document, load_json(CONTRACT / "components.schema.json"))
    seed = seed_sql(document)
    assert "'uk', NULL, 'outage', 'acme'," in seed
    assert "DELETE FROM notices WHERE tenant_id = 'acme';" in seed


def test_seed_never_holds_a_bot_token() -> None:
    assert "token" not in seed_sql(fixture_document()).lower()


@pytest.mark.parametrize(
    "notify",
    [
        {"telegram_chat_id": 100200300},
        {"telegram_chat_id": "@acme"},
        {"telegram_chat_id": "100200300", "min_state": "maintenance"},
        {"telegram_chat_id": "100200300", "bot_token": "x"},
        {"min_state": "outage"},
    ],
)
def test_notify_refuses_what_the_worker_cannot_use(
    data: dict[str, Any], notify: dict[str, Any]
) -> None:
    data["status_page"]["notify"] = notify
    assert "schema" in rules_hit(data)


def test_access_reaches_the_document_and_the_seed() -> None:
    document = fixture_document()
    aud = "fb52298607fa036764eb5e6e31b89bab96294fddc185ce6324890af471c374f0"
    assert document["access"] == {"team": "acme", "aud": aud}
    assert f"'no_data', 'acme', '{aud}')" in seed_sql(document)
    assert "access_aud = excluded.access_aud" in seed_sql(document)


def test_seed_without_access_clears_it_so_admin_closes() -> None:
    data = yaml.safe_load(FIXTURE_MANIFEST.read_text(encoding="utf-8"))
    del data["status_page"]["access"]
    document = status_document(manifest_from(data))
    assert document["access"] is None
    jsonschema.validate(document, load_json(CONTRACT / "components.schema.json"))
    assert "'no_data', NULL, NULL)" in seed_sql(document)


def test_the_example_manifest_commits_no_access_application() -> None:
    data = yaml.safe_load((ROOT / "examples" / "acme.yaml").read_text(encoding="utf-8"))
    assert "access" not in data["status_page"]


@pytest.mark.parametrize(
    "access",
    [
        {"team": "acme"},
        {"aud": "a" * 64},
        {"team": "acme.cloudflareaccess.com", "aud": "a" * 64},
        {"team": "https://acme.cloudflareaccess.com", "aud": "a" * 64},
        {"team": "Acme", "aud": "a" * 64},
        {"team": "acme", "aud": "A" * 64},
        {"team": "acme", "aud": "a" * 63},
        {"team": "acme", "aud": "a" * 64, "secret": "x"},
    ],
)
def test_access_refuses_what_the_worker_cannot_use(
    data: dict[str, Any], access: dict[str, Any]
) -> None:
    data["status_page"]["access"] = access
    assert "schema" in rules_hit(data)


@pytest.mark.parametrize("case", push_cases("valid", "rejected"), ids=lambda case: case["name"])
def test_push_fixture_follows_the_schema(case: dict[str, Any]) -> None:
    jsonschema.validate(json.loads(case["body"]), load_json(CONTRACT / "push.schema.json"))


@pytest.mark.parametrize("case", push_cases("invalid"), ids=lambda case: case["name"])
def test_off_contract_push_fixture_fails_the_schema(case: dict[str, Any]) -> None:
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(json.loads(case["body"]), load_json(CONTRACT / "push.schema.json"))


def test_python_signatures_match_the_shared_fixtures() -> None:
    secret = load_json(FIXTURES / "push.json")["secret"]
    for case in push_cases("valid", "invalid", "rejected"):
        key = case.get("signed_with", secret)
        assert sign(case["body"].encode("utf-8"), key) == case["signature"], case["name"]


def test_push_schema_lists_the_five_states() -> None:
    schema = load_json(CONTRACT / "push.schema.json")
    assert tuple(schema["properties"]["components"]["items"]["properties"]["state"]["enum"]) == (
        STATES
    )


def test_cli_prints_the_components_document(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["status", str(FIXTURE_MANIFEST)]) == 0
    assert json.loads(capsys.readouterr().out) == load_json(FIXTURES / "acme.components.json")


def test_cli_prints_the_seed_with_sql(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["status", str(FIXTURE_MANIFEST), "--sql"]) == 0
    assert capsys.readouterr().out == (FIXTURES / "acme.seed.sql").read_text(encoding="utf-8")


def test_cli_refuses_a_manifest_without_a_status_page(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    data = yaml.safe_load(FIXTURE_MANIFEST.read_text(encoding="utf-8"))
    del data["status_page"]
    path = tmp_path / "manifest.yaml"
    data["hosts"]["central"]["compose"] = [str(FIXTURES / "apps.compose.yaml")]
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    assert main(["status", str(path)]) == 1
    assert "status_page.hostname is not set" in capsys.readouterr().err


def test_cli_refuses_a_manifest_that_fails_check(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    data = yaml.safe_load(FIXTURE_MANIFEST.read_text(encoding="utf-8"))
    data["services"]["web"]["status"] = {"public": "Сайт"}
    data["hosts"]["central"]["compose"] = [str(FIXTURES / "apps.compose.yaml")]
    path = tmp_path / "manifest.yaml"
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    assert main(["status", str(path)]) == 1
    assert "[status-component]" in capsys.readouterr().err


def test_example_manifest_probes_its_website_and_pushes_its_mcp_endpoint() -> None:
    document = status_document(load_manifest(ROOT / "examples" / "acme.yaml"))
    assert document["components"] == [
        {"id": "website", "title": "Website", "probes": ["https://acme.example"], "push": False},
        {"id": "mcp-endpoint", "title": "MCP endpoint", "probes": [], "push": True},
    ]
