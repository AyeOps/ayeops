from __future__ import annotations

import json
import re
import urllib.parse
from pathlib import Path
from typing import Any

import pytest

from tests.conftest import ROOT
from tests.test_autopilot_agent import (
    API_KEY,
    CATALOG,
    SECRET,
    Harness,
    api_answer,
    autopilot,
    grafana_alert,
    payload,
)


def fake(*parts: str) -> str:
    return "".join(parts)


JWT = fake("eyJ", "hbGciOiJIUzI1NiJ9.", "eyJ", "zdWIiOiIxMjM0NTY3ODkwIn0.", "dozjgNryP4J3jVmN")
GITHUB = fake("ghp", "_", "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8")
ANTHROPIC = fake("sk-", "ant-api03-", "PlantedKeyValue0001")
GITLAB = fake("glpat", "-", "PlantedInTheLabel012")
AWS = fake("AKIA", "IOSFODNN7EXAMPLE")
DB_PASSWORD = "pg-pass-pass-1"
PING_URL = "https://hc-ping.example/5b8f2a1c"
SHA = "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b"
DIGEST = "sha256:9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08"


def masked(text: str, masker: Any = None) -> tuple[str, dict[str, int]]:
    counts: dict[str, int] = {}
    out = (masker or autopilot.Masker()).text(text, counts)
    return out, counts


PATTERNS = [
    ("Authorization: Bearer ", "abc.DEF-123_ghi~jkl", "", "authorization"),
    ('"authorization": "Token ', "0a1b2c3d4e5f", '"', "authorization"),
    ("Authorization: Basic ", "dXNlcjpwYXNzd29yZA==", "", "authorization"),
    ("retrying with bearer ", "Zm9vYmFyMTIzNDU2", "", "authorization"),
    ("GET /export?user=bob&password=", "hunter2", "&page=2", "password"),
    ("GET /hook?token=", "abc123def", "&page=2", "token"),
    ('{"msg":"POST /hook?token=', "4f2a9c71", ' failed"}', "token"),
    ("path=/hook?token=", "4f2a9c71", " status=502", "token"),
    ("apiKey: ", "AbCdEfGhIj", "", "api-key"),
    ("GET /maps?api_key=", "K3yValue99", " HTTP/1.1", "api-key"),
    ('{"level":"error","msg":"login","passwd":"', "hunter2", '"}', "password"),
    ('{\\"secret\\":\\"', "s3cr3t-value", '\\"}', "secret"),
    ("DB_PASSWORD=", "hunter2", " DB_HOST=db", "password"),
    ("export CLIENT_SECRET='", "quoted value", "'", "secret"),
    ("password: ", "hunter2", "", "password"),
    ("x-api-key: ", "key-key-0000", "", "api-key"),
    ("apiKey => '", "Zz9-key", "'", "api-key"),
    ("redis://:", "r3dis-pass", "@cache:6379/0", "url-password"),
    ("dial postgres://app:", "pg-pass-9", "@db:5432/app failed", "url-password"),
    ("Authorization: Bearer ", JWT, "", "jwt"),
    ("cookie jwt ", JWT, " rejected", "jwt"),
    ("", AWS, " was denied", "aws-key"),
    ("sent ", ANTHROPIC, " to the api", "anthropic-key"),
    ("key ", fake("sk-", "proj-", "AbCdEfGhIjKlMnOpQrStUv"), "", "openai-key"),
    ("clone with ", GITHUB, "", "github-token"),
    ("", fake("github", "_pat_", "11ABCDEFG0123456789_abcdefghijklmnop"), "", "github-token"),
    ("push ", GITLAB, " refused", "gitlab-token"),
    ("slack ", fake("xox", "b-", "1234567890-AbCdEfGhIj"), " failed", "slack-token"),
    ("slack ", fake("xox", "p-", "1234567890-AbCdEfGhIj"), " failed", "slack-token"),
    ("charge with ", fake("sk", "_live_", "AbCdEfGhIj012345"), "", "stripe-key"),
    ("maps ", fake("AI", "za", "SyA1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q"), "", "google-key"),
    (
        "POST https://api.telegram.org/bot",
        fake("123456789", ":", "AAHfiqksKZ8WmR2zSjiQ7_v4TMAKdiHm9T0"),
        "/sendMessage",
        "telegram-token",
    ),
    (
        fake("loaded -----BEGIN OPENSSH ", "PRIVATE KEY-----\\n"),
        "b3BlbnNzaC1rZXktdjE",
        fake("\\n-----END OPENSSH ", "PRIVATE KEY----- from disk"),
        "private-key",
    ),
    (fake("-----BEGIN RSA ", "PRIVATE KEY-----\n"), "MIIEpAIBAAKCAQEA", " cut off", "private-key"),
    ("Cookie: ", "session=abc; csrftoken=zzz", "", "cookie"),
    ("using token ", SHA, "", "token"),
    ("token=", SHA, "", "token"),
    ("signature is ", "Zm9vYmFyYmF6cXV4MTIzNDU2Nzg5MA==", "", "secret"),
    ("dsn=", "sentry-dsn-value-1", "", "secret"),
]


@pytest.mark.parametrize(("before", "secret", "after", "kind"), PATTERNS)
def test_each_pattern_masks_its_value_and_names_the_kind(
    before: str, secret: str, after: str, kind: str
) -> None:
    out, counts = masked(before + secret + after)
    assert secret not in out
    assert f"[masked:{kind}]" in out
    assert counts == {kind: 1}


ORDINARY = [
    "2026-10-14T09:01:02.123Z ERROR request 6f1c2d3e-aaaa-bbbb-cccc-0123456789ab timed out",
    f"pulled nginx@{DIGEST} for web",
    f"deploy of commit {SHA} finished, image {DIGEST}",
    f"commit={SHA} digest={DIGEST}",
    "dial tcp 10.0.0.5:5432: connect: connection refused",
    "GET https://acme.example/api/orders?page=2&sort=desc 502",
    "invalid token: expired",
    "refresh token: signature is invalid",
    "max_tokens=4000 token_type=bearer expires_in=3600 token_count=12",
    "tests passed=12 bypass=1 compass=north",
    "basic authentication failed for user bob",
    "missing bearer token in request",
    "password_length=12 password_file=/run/secrets/db secret_name=db-creds",
    "cache key=users:42 author=alice keyboard=us",
    "password=null token=*** secret=${DB_SECRET} api_key=[REDACTED] pass=",
    "PWD=/home/app HOME=/root",
    "retry token=123 after 2 s",
    fake("request id=xeyJ", "abcd.efgh.ijkl"),
    '{"level":"warn","msg":"slow query","duration_ms":1532,"rows":7}',
    "http://grafana:3000/api/health answered 200",
]


@pytest.mark.parametrize("line", ORDINARY)
def test_ordinary_log_lines_pass_unchanged(line: str) -> None:
    assert masked(line) == (line, {})


def test_a_long_chain_of_pairs_is_scanned_in_one_pass() -> None:
    line = "a=" * 20000 + "token=abc123"
    out, counts = masked(line)
    assert out == "a=" * 20000 + "token=[masked:token]"
    assert counts == {"token": 1}


def test_masking_twice_changes_nothing_and_counts_nothing_more() -> None:
    line = (
        f"dial postgres://app:pg-pass-9@db Authorization: Bearer {JWT} token=abc123 "
        "password: hunter2 Cookie: a=b"
    )
    once, counts = masked(line)
    twice, more = masked(once)
    assert twice == once
    assert more == {}
    assert counts == {"url-password": 1, "jwt": 1, "token": 1, "cookie": 1, "password": 1}


def test_env_values_are_masked_with_the_name_of_their_variable() -> None:
    environ = {
        "AYE_AUTOPILOT_API_KEY": API_KEY,
        "AYE_AUTOPILOT_WEBHOOK_SECRET": SECRET,
        "AYE_SENTINEL_PING_URL": PING_URL,
        "AYE_GRAFANA_PG_PASSWORD": DB_PASSWORD,
        "AYE_AUTOPILOT_MODEL": "claude-sonnet-5-5",
        "AYE_SHORT_TOKEN": "abc1234",
        "AYE_EIGHT_TOKEN": "abcd-123",
        "HOME": "/nonexistent",
    }
    masker = autopilot.masker_from(environ, ["AYE_SENTINEL_PING_URL", "AYE_GRAFANA_PG_PASSWORD"])
    line = (
        f"key {API_KEY} hook {SECRET} ping {PING_URL} plain {DB_PASSWORD} "
        "model claude-sonnet-5-5 home /nonexistent short abc1234 eight abcd-123"
    )
    out, counts = masked(line, masker)
    for value in (API_KEY, SECRET, PING_URL, DB_PASSWORD):
        assert value not in out
    assert "[masked:env:AYE_AUTOPILOT_API_KEY]" in out
    assert "[masked:env:AYE_AUTOPILOT_WEBHOOK_SECRET]" in out
    assert "[masked:env:AYE_SENTINEL_PING_URL]" in out
    assert out.count("[masked:env:AYE_GRAFANA_PG_PASSWORD]") == 1
    assert "claude-sonnet-5-5" in out and "/nonexistent" in out and "abc1234" in out
    assert "[masked:env:AYE_EIGHT_TOKEN]" in out
    assert counts["env"] == 5


MODULE = ROOT / "src" / "ayeops" / "modules" / "autopilot"
OWN_SECRETS = {"AYE_AUTOPILOT_WEBHOOK_SECRET", "AYE_AUTOPILOT_API_KEY", "AYE_AUTOPILOT_OAUTH_TOKEN"}


def own_settings() -> list[str]:
    template = (MODULE / "compose.yaml.j2").read_text(encoding="utf-8")
    names = set(re.findall(r"^ +(AYE_AUTOPILOT_[A-Z_]+):", template, re.MULTILINE))
    return sorted(names - OWN_SECRETS)


def test_the_auth_mode_is_not_masked_as_a_secret_but_the_key_beside_it_is() -> None:
    environ = {
        "AYE_AUTOPILOT_AUTH": "subscription",
        "AYE_AUTOPILOT_API_KEY": API_KEY,
        "AYE_AUTOPILOT_OAUTH_TOKEN": "oauth-token-value-0001",
    }
    masker = autopilot.masker_from(environ, [])
    out, counts = masked(
        f"auth subscription renewed; key {API_KEY} token oauth-token-value-0001", masker
    )
    assert out == (
        "auth subscription renewed; key [masked:env:AYE_AUTOPILOT_API_KEY]"
        " token [masked:env:AYE_AUTOPILOT_OAUTH_TOKEN]"
    )
    assert counts == {"env": 2}
    labels = masker.labels({"plan": "subscription", "alertname": "x"}, counts)
    assert labels == {"plan": "subscription", "alertname": "x"}


def test_no_setting_of_the_agent_is_masked_by_value_even_when_listed() -> None:
    names = own_settings()
    assert "AYE_AUTOPILOT_AUTH" in names and "AYE_AUTOPILOT_LOKI_URL" in names
    environ = {name: f"plain-value-{index:03d}" for index, name in enumerate(names)}
    environ["AYE_AUTOPILOT_WEBHOOK_SECRET"] = SECRET
    masker = autopilot.masker_from(environ, names)
    line = " ".join(environ.values())
    out, counts = masked(line, masker)
    assert out == line.replace(SECRET, "[masked:env:AYE_AUTOPILOT_WEBHOOK_SECRET]")
    assert counts == {"env": 1}


def test_the_longer_env_value_wins_and_every_hit_counts() -> None:
    masker = autopilot.Masker({"AYE_A_TOKEN": "abcdefgh", "AYE_B_TOKEN": "abcdefgh-longer"})
    out, counts = masked("abcdefgh-longer then abcdefgh and abcdefgh", masker)
    assert out == (
        "[masked:env:AYE_B_TOKEN] then [masked:env:AYE_A_TOKEN] and [masked:env:AYE_A_TOKEN]"
    )
    assert counts == {"env": 3}


def test_a_value_masked_already_keeps_the_text_around_it() -> None:
    masker = autopilot.Masker({"AYE_SHOP_DB_PASSWORD": "Kx7-shop-db"})
    line = f'{{"dsn":"postgres://shop:Kx7-shop-db@db:5432/shop","auth":"Bearer {JWT}"}}'
    out, counts = masked(line, masker)
    assert out == (
        '{"dsn":"postgres://shop:[masked:env:AYE_SHOP_DB_PASSWORD]@db:5432/shop",'
        '"auth":"Bearer [masked:jwt]"}'
    )
    assert counts == {"env": 1, "jwt": 1}


def test_a_private_key_is_masked_whole_even_around_a_known_value() -> None:
    masker = autopilot.Masker({"AYE_KEY_PASSPHRASE": "inside-the-key"})
    line = fake(
        "-----BEGIN ", "PRIVATE KEY-----\\nab inside-the-key cd\\n-----END ", "PRIVATE KEY-----"
    )
    assert masked(line, masker) == ("[masked:private-key]", {"private-key": 1})


def test_an_env_value_in_url_encoded_form_is_masked_too() -> None:
    value = "p@ss/w0rd+x"
    masker = autopilot.Masker({"AYE_DB_PASSWORD": value})
    out, counts = masked(f"dsn host=db password_url={urllib.parse.quote(value, safe='')}", masker)
    assert "p%40ss%2Fw0rd%2Bx" not in out
    assert "[masked:env:AYE_DB_PASSWORD]" in out
    assert counts == {"env": 1}


def test_a_listed_name_masks_its_assignment_whatever_the_name_says() -> None:
    masker = autopilot.Masker(names=["AYE_TELEGRAM_CHAT_ID"])
    out, counts = masked("AYE_TELEGRAM_CHAT_ID=100200300 OTHER_ID=7", masker)
    assert out == "AYE_TELEGRAM_CHAT_ID=[masked:secret] OTHER_ID=7"
    assert counts == {"secret": 1}


@pytest.mark.parametrize(
    ("key", "kind"),
    [
        ("password", "password"),
        ("PGPASSWORD", "password"),
        ("db_passwd", "password"),
        ("pass", "password"),
        ("x-api-key", "api-key"),
        ("apiKey", "api-key"),
        ("AWS_SECRET_ACCESS_KEY", "api-key"),
        ("AYE_OBJECTSTORE_ACCESS_KEY_ID", "api-key"),
        ("clientSecret", "secret"),
        ("AYE_ONCALL_ENCRYPTION_KEY", "secret"),
        ("private_key", "secret"),
        ("credentials", "secret"),
        ("X-Amz-Signature", "secret"),
        ("access_token", "token"),
        ("authToken", "token"),
        ("jwt", "token"),
        ("session_id", "token"),
        ("cookie", "cookie"),
        ("passed", None),
        ("bypass", None),
        ("max_tokens", None),
        ("token_type", None),
        ("password_file", None),
        ("tokenizer", None),
        ("secretary", None),
        ("keyboard", None),
        ("key", None),
        ("PWD", None),
        ("AYE_AUTOPILOT_DOSSIER_TOKENS", None),
        ("", None),
    ],
)
def test_keys_are_classified_by_their_words(key: str, kind: str | None) -> None:
    assert autopilot.secret_kind(key) == kind


def test_custom_masks_cover_the_whole_match_or_only_their_first_group() -> None:
    masker = autopilot.Masker(extra=[r"ACME-[0-9]{6}", r"customer=(\w+)"])
    out, counts = masked("order ACME-123456 for customer=globex done", masker)
    assert out == "order [masked:custom] for customer=[masked:custom] done"
    assert counts == {"custom": 2}


@pytest.mark.parametrize("pattern", ["(unclosed", "x*", "(?:a|)"])
def test_a_broken_or_empty_matching_custom_mask_stops_the_agent(pattern: str) -> None:
    with pytest.raises(ValueError, match="mask"):
        autopilot.compiled_masks([pattern])
    with pytest.raises(ValueError, match="mask"):
        autopilot.settings_from(
            {"AYE_AUTOPILOT_WEBHOOK_SECRET": SECRET, "AYE_AUTOPILOT_MASK": json.dumps([pattern])}
        )


def test_settings_read_the_masks_and_the_names_given_for_masking() -> None:
    settings = autopilot.settings_from(
        {
            "AYE_AUTOPILOT_WEBHOOK_SECRET": SECRET,
            "AYE_AUTOPILOT_MASK": json.dumps([r"ACME-\d+$"]),
            "AYE_AUTOPILOT_MASK_ENV": json.dumps(["AYE_GRAFANA_PG_PASSWORD"]),
        }
    )
    assert settings.mask == (r"ACME-\d+$",)
    assert settings.mask_env == ("AYE_GRAFANA_PG_PASSWORD",)
    plain = autopilot.settings_from({"AYE_AUTOPILOT_WEBHOOK_SECRET": SECRET})
    assert (plain.mask, plain.mask_env) == ((), ())


def test_secret_label_keys_are_masked_whole_and_other_labels_by_pattern() -> None:
    counts: dict[str, int] = {}
    labels = autopilot.Masker().labels(
        {"api_key": "plain", "token": "null", "url": f"https://u:pw-123@h/{JWT}", "host": "c"},
        counts,
    )
    assert labels == {
        "api_key": "[masked:api-key]",
        "token": "null",
        "url": "https://u:[masked:url-password]@h/[masked:jwt]",
        "host": "c",
    }
    assert counts == {"api-key": 1, "url-password": 1, "jwt": 1}


def test_data_masks_every_string_and_key_and_keeps_numbers() -> None:
    counts: dict[str, int] = {}
    data = {"a": [f"token={SHA}", 3, None, True], f"k {GITHUB}": {"n": 1.5}, "t": ("x",)}
    assert autopilot.Masker().data(data, counts) == {
        "a": ["token=[masked:token]", 3, None, True],
        "k [masked:github-token]": {"n": 1.5},
        "t": ["x"],
    }
    assert counts == {"token": 1, "github-token": 1}


PLANTED_LOGS = [
    f'{{"level":"error","msg":"auth failed","token":"{GITHUB}"}}',
    f"ERROR dial postgres://app:{DB_PASSWORD}@db:5432/app refused",
    "x" * 190 + f" Authorization: Bearer {JWT}",
    f"ERROR ping {PING_URL} timed out",
    fake(
        "ERROR -----BEGIN EC ",
        "PRIVATE KEY-----\\nMHcCAQEEIBplantedkey\\n-----END EC PRIVATE KEY-----",
    ),
    f"ERROR failed with {AWS} and {ANTHROPIC}",
    "ERROR retry 6f1c2d3e-aaaa-bbbb-cccc-0123456789ab after 5 attempts",
]
PLANTED = [
    GITHUB,
    DB_PASSWORD,
    JWT,
    JWT[:30],
    PING_URL,
    "MHcCAQEEIBplantedkey",
    AWS,
    ANTHROPIC,
    "label-secret-value",
    GITLAB,
    "Summ4ry-Pass",
    API_KEY,
]


def planted_harness(tmp_path: Path, **extra: Any) -> Harness:
    h = Harness(tmp_path, log_lines=PLANTED_LOGS, **extra)
    h.engine.collector.masker = autopilot.masker_from(
        {
            "AYE_AUTOPILOT_API_KEY": API_KEY,
            "AYE_SENTINEL_PING_URL": PING_URL,
            "AYE_GRAFANA_PG_PASSWORD": DB_PASSWORD,
        },
        ["AYE_SENTINEL_PING_URL", "AYE_GRAFANA_PG_PASSWORD"],
    )
    return h


def planted_alert() -> dict[str, Any]:
    alert = grafana_alert(api_token="label-secret-value", note=f"push {GITLAB}")
    alert["annotations"]["summary"] = f"login with password=Summ4ry-Pass failed, key {API_KEY}"
    return alert


def test_the_dossier_sent_to_the_model_holds_no_planted_secret(tmp_path: Path) -> None:
    h = planted_harness(tmp_path)
    h.engine.on_webhook(payload(planted_alert()))
    (call,) = h.model_calls()
    prompt = call["messages"][0]["content"]
    run = h.lines()[0]["run"]
    kept = (tmp_path / "dossiers" / f"{run}.json").read_text("utf-8")
    for secret in PLANTED:
        assert secret not in prompt, secret
        assert secret not in kept, secret
    assert "eyJ" not in prompt
    dossier = json.loads(prompt)
    assert dossier["alert"]["labels"]["api_token"] == "[masked:token]"
    assert dossier["alert"]["labels"]["note"] == "push [masked:gitlab-token]"
    assert "password=[masked:password]" in dossier["alert"]["summary"]
    assert "[masked:env:AYE_AUTOPILOT_API_KEY]" in dossier["alert"]["summary"]
    assert "6f1c2d3e-aaaa-bbbb-cccc-0123456789ab" in prompt
    assert dossier["logs"]["lines"] == len(PLANTED_LOGS)


def test_the_journal_counts_masked_values_per_kind_without_values(tmp_path: Path) -> None:
    h = planted_harness(tmp_path)
    h.engine.on_webhook(payload(planted_alert()))
    (line,) = h.lines()
    assert line["masked"] == {
        "anthropic-key": 1,
        "aws-key": 1,
        "env": 3,
        "github-token": 1,
        "gitlab-token": 1,
        "jwt": 1,
        "password": 1,
        "private-key": 1,
        "token": 1,
    }
    assert all(isinstance(found, int) for found in line["masked"].values())
    text = h.engine.counters.render()
    assert 'ayeops_autopilot_masked_total{kind="env"} 3' in text
    assert 'ayeops_autopilot_masked_total{kind="jwt"} 1' in text
    assert 'ayeops_autopilot_masked_total{kind="cookie"} 0' in text


def test_a_run_without_secrets_journals_an_empty_count(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.engine.on_webhook(payload(grafana_alert()))
    assert h.lines()[0]["masked"] == {}


def test_a_failed_dossier_still_journals_what_was_masked(tmp_path: Path) -> None:
    h = planted_harness(tmp_path)

    def broken(value: Any, counts: dict[str, int]) -> Any:
        raise RuntimeError("cap failed")

    h.engine.collector.masker.data = broken
    h.engine.on_webhook(payload(planted_alert()))
    (line,) = h.lines()
    assert line["status"] == "failed"
    assert line["masked"]["token"] == 1
    assert line["masked"]["github-token"] == 1


def test_the_posted_verdict_is_masked_too(tmp_path: Path) -> None:
    sent: list[str] = []
    h = planted_harness(tmp_path, post=sent.append)
    h.http.model_answer = api_answer(
        [
            {
                "cause": f"the token {GITHUB} expired",
                "confidence": "high",
                "evidence": [],
                "action": "needs-a-person",
                "target": "web",
            }
        ]
    )
    h.engine.on_webhook(payload(planted_alert()))
    (text,) = sent
    assert GITHUB not in text
    assert "[masked:github-token]" in text
    assert h.lines()[0]["masked"]["github-token"] == 2


def test_runner_none_keeps_the_masked_dossier_and_calls_no_model(tmp_path: Path) -> None:
    sent: list[str] = []
    h = Harness(tmp_path, log_lines=PLANTED_LOGS, post=sent.append)
    h.engine.config = autopilot.Config(limits=h.limits, runner_name="none")
    h.engine.on_webhook(payload(planted_alert()))
    assert h.model_calls() == []
    assert sent == []
    (line,) = h.lines()
    assert line["status"] == "ok"
    assert line["reason"] == "runner none: no model call"
    assert line["cost_usd"] == 0.0
    assert "hypotheses" not in line
    kept = (tmp_path / "dossiers" / f"{line['run']}.json").read_text("utf-8")
    assert GITHUB not in kept and JWT not in kept
    assert 'ayeops_autopilot_runs_total{status="ok"} 1' in h.engine.counters.render()


def test_runner_none_needs_no_key_and_refuses_a_model_call(tmp_path: Path) -> None:
    environ = {
        "AYE_AUTOPILOT_WEBHOOK_SECRET": SECRET,
        "AYE_AUTOPILOT_RUNNER": "none",
        "AYE_AUTOPILOT_STATE": str(tmp_path),
    }
    settings = autopilot.settings_from(environ)
    runner = autopilot.runner_from(settings, environ, Harness(tmp_path).http)
    with pytest.raises(autopilot.ModelError):
        runner(autopilot.ModelRequest("m", "medium", "s", "p", {}, 0.5, 10.0))


def test_the_agent_masks_with_the_catalog_names_and_its_own_secrets(tmp_path: Path) -> None:
    catalog = tmp_path / "catalog.json"
    catalog.write_text(
        json.dumps({"rules": {}, "services": {}, "secret_names": ["AYE_SENTINEL_PING_URL"]}),
        "utf-8",
    )
    environ = {
        "AYE_AUTOPILOT_WEBHOOK_SECRET": SECRET,
        "AYE_AUTOPILOT_API_KEY": API_KEY,
        "AYE_AUTOPILOT_STATE": str(tmp_path / "state"),
        "AYE_AUTOPILOT_CATALOG": str(catalog),
        "AYE_AUTOPILOT_MASK": json.dumps(["ACME-[0-9]+"]),
        "AYE_AUTOPILOT_MASK_ENV": json.dumps(["AYE_SHOP_LOGIN"]),
        "AYE_SENTINEL_PING_URL": PING_URL,
        "AYE_SHOP_LOGIN": DB_PASSWORD,
        "AYE_AUTOPILOT_MODEL": "claude-sonnet-5-5",
    }
    engine = autopilot.engine_from(
        autopilot.settings_from(environ), environ, Harness(tmp_path).http
    )
    assert engine.collector.catalog.secret_names == frozenset({"AYE_SENTINEL_PING_URL"})
    line = f"{SECRET} {API_KEY} {PING_URL} {DB_PASSWORD} ACME-42 claude-sonnet-5-5"
    out, counts = masked(line, engine.collector.masker)
    assert out == (
        "[masked:env:AYE_AUTOPILOT_WEBHOOK_SECRET] [masked:env:AYE_AUTOPILOT_API_KEY] "
        "[masked:env:AYE_SENTINEL_PING_URL] [masked:env:AYE_SHOP_LOGIN] "
        "[masked:custom] claude-sonnet-5-5"
    )
    assert counts == {"env": 4, "custom": 1}


def test_a_catalog_without_names_still_masks(tmp_path: Path) -> None:
    catalog = tmp_path / "catalog.json"
    catalog.write_text(json.dumps({"rules": {}, "services": {}}), "utf-8")
    assert autopilot.load_catalog(catalog).secret_names == frozenset()
    assert CATALOG.secret_names == frozenset()
