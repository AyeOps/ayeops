from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from tests.conftest import ROOT

FILES = ROOT / "src" / "ayeops" / "modules" / "oncall" / "files"


def load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, FILES / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


setup = load("oncall_setup")
bridge = load("oncall_bridge")

ENV = {
    "AYE_ONCALL_PUBLIC_URL": "https://oncall.ops.acme.example/",
    "AYE_ONCALL_ADMIN_USER": "oncall",
    "AYE_ONCALL_ADMIN_PASSWORD": "pw",
    "AYE_ONCALL_PERSON_NAME": "Acme On-call",
    "AYE_ONCALL_PERSON_EMAIL": "oncall@acme.example",
    "AYE_ONCALL_WEBHOOK_SECRET": "s3cret",
    "AYE_ONCALL_SMTP_ADDRESS": "smtp.acme.example:587",
    "AYE_ONCALL_SMTP_FROM": "ops@acme.example",
    "AYE_ONCALL_SMTP_USER": "u",
    "AYE_ONCALL_SMTP_PASSWORD": "p",
}


def raw(request: urllib.request.Request) -> bytes:
    return request.data if isinstance(request.data, bytes) else b""


class FakeGoAlert:
    def __init__(self, state_dir: Path, deliver_code: bool = True) -> None:
        self.state_dir = state_dir
        self.deliver_code = deliver_code
        self.config: dict[str, str] = {}
        self.user = {"id": "u1", "name": "oncall", "email": ""}
        self.methods: list[dict[str, Any]] = []
        self.policies: list[dict[str, Any]] = []
        self.services: list[dict[str, Any]] = []
        self.keys: list[dict[str, Any]] = []
        self.ops: list[str] = []
        self.logins: list[dict[str, str]] = []
        self.next = 0

    def uid(self, prefix: str) -> str:
        self.next += 1
        return f"{prefix}{self.next}"

    def __call__(self, request: urllib.request.Request) -> tuple[int, bytes]:
        if request.full_url.endswith("/api/v2/identity/providers/basic?noRedirect=1"):
            form = dict(urllib.parse.parse_qsl(raw(request).decode()))
            self.logins.append({**form, "referer": request.get_header("Referer") or ""})
            return (200, b"session-token") if form.get("password") == "pw" else (400, b"bad")
        assert request.get_header("Authorization") == "Bearer session-token"
        body = json.loads(raw(request) or b"{}")
        query, variables = body["query"], body.get("variables") or {}
        data = self.answer(query, variables)
        return 200, json.dumps({"data": data}).encode()

    def answer(self, query: str, v: dict[str, Any]) -> dict[str, Any]:
        name = next(
            word for word in query.replace("(", " ").replace("{", " ").split() if word in OPS
        )
        self.ops.append(name)
        given: Any = v.get("input")
        if name == "setConfig":
            self.config.update({item["id"]: item["value"] for item in given})
            return {name: True}
        if name == "user":
            if "contactMethods" in query:
                return {"user": {"contactMethods": [dict(m) for m in self.methods]}}
            return {"user": dict(self.user)}
        if name == "updateUser":
            self.user.update({k: val for k, val in given.items() if k != "id"})
            return {name: True}
        if name == "deleteAll":
            gone = {t["id"] for t in v["ids"]}
            self.methods = [m for m in self.methods if m["id"] not in gone]
            return {name: True}
        if name == "createUserContactMethod":
            disabled = given["dest"]["type"] in ("builtin-webhook", "builtin-smtp-email")
            method = {
                "id": self.uid("cm"),
                "name": given["name"],
                "disabled": disabled,
                "dest": given["dest"],
            }
            self.methods.append(method)
            return {name: {"id": method["id"]}}
        if name == "sendContactMethodVerification":
            if self.deliver_code:
                (self.state_dir / "verification.json").write_text(
                    json.dumps({"code": "424242"}), "utf-8"
                )
            return {name: True}
        if name == "verifyContactMethod":
            assert given["code"] == 424242
            for method in self.methods:
                if method["id"] == given["contactMethodID"]:
                    method["disabled"] = False
            return {name: True}
        if name == "escalationPolicies":
            return {name: {"nodes": [dict(p) for p in self.policies if v["s"] in p["name"]]}}
        if name == "createEscalationPolicy":
            policy = {
                "id": self.uid("ep"),
                "name": given["name"],
                "repeat": given["repeat"],
                "steps": [],
            }
            self.policies.append(policy)
            return {name: {"id": policy["id"]}}
        if name == "createEscalationPolicyStep":
            policy = next(p for p in self.policies if p["id"] == given["escalationPolicyID"])
            policy["steps"].append({"delay": given["delayMinutes"], "actions": given["actions"]})
            return {name: {"id": self.uid("st")}}
        if name == "services":
            return {name: {"nodes": [dict(s) for s in self.services]}}
        if name == "createService":
            service = {
                "id": self.uid("sv"),
                "name": given["name"],
                "escalationPolicyID": given["escalationPolicyID"],
                "integrationKeys": [],
            }
            for key in given.get("newIntegrationKeys") or []:
                service["integrationKeys"].append(self.key(key["type"]))
            self.services.append(service)
            return {name: {"id": service["id"]}}
        if name == "updateService":
            next(s for s in self.services if s["id"] == given["id"])["escalationPolicyID"] = given[
                "escalationPolicyID"
            ]
            return {name: True}
        if name == "createIntegrationKey":
            next(s for s in self.services if s["id"] == given["serviceID"])[
                "integrationKeys"
            ].append(self.key(given["type"]))
            return {name: {"id": "ik"}}
        if name == "gqlAPIKeys":
            return {name: [{"id": k["id"], "name": k["name"]} for k in self.keys]}
        if name == "createGQLAPIKey":
            key = {
                "id": self.uid("key"),
                "name": given["name"],
                "query": given["query"],
                "role": given["role"],
            }
            self.keys.append(key)
            return {name: {"id": key["id"], "token": f"token-{key['id']}"}}
        raise AssertionError(query)

    def key(self, kind: str) -> dict[str, str]:
        token = self.uid("tok")
        return {
            "id": self.uid("ik"),
            "type": kind,
            "href": f"https://oncall.ops.acme.example/api/v2/grafana/incoming?token={token}",
        }


OPS = {
    "setConfig",
    "user",
    "updateUser",
    "deleteAll",
    "createUserContactMethod",
    "sendContactMethodVerification",
    "verifyContactMethod",
    "escalationPolicies",
    "createEscalationPolicy",
    "createEscalationPolicyStep",
    "services",
    "createService",
    "updateService",
    "createIntegrationKey",
    "gqlAPIKeys",
    "createGQLAPIKey",
}


def settings(tmp_path: Path, **overrides: str) -> Any:
    return setup.settings_from({**ENV, "AYE_ONCALL_STATE": str(tmp_path), **overrides})


def run(tmp_path: Path, fake: FakeGoAlert, **overrides: str) -> dict[str, Any]:
    parsed = settings(tmp_path, **overrides)
    result: dict[str, Any] = setup.run(parsed, setup.GoAlert(parsed, fake))
    return result


def test_a_first_run_writes_the_manifest_into_goalert(tmp_path: Path) -> None:
    fake = FakeGoAlert(tmp_path)
    summary = run(tmp_path, fake)
    assert summary["created_methods"] == ["Telegram oncall bridge", "Email"]
    assert summary["verified_webhook"] is True
    assert fake.logins == [
        {"username": "oncall", "password": "pw", "referer": "https://oncall.ops.acme.example/"}
    ]
    assert fake.config["Webhook.Enable"] == "true"
    assert fake.config["Webhook.AllowedURLs"] == "http://oncall-bridge:8080/"
    assert fake.config["SMTP.Enable"] == "true"
    assert fake.config["SMTP.Address"] == "smtp.acme.example:587"
    assert fake.config["Twilio.Enable"] == "false"
    assert (fake.user["name"], fake.user["email"]) == ("Acme On-call", "oncall@acme.example")
    webhook = next(m for m in fake.methods if m["name"] == "Telegram oncall bridge")
    assert webhook["dest"] == {
        "type": "builtin-webhook",
        "args": {"webhook_url": "http://oncall-bridge:8080/goalert/s3cret"},
    }
    assert webhook["disabled"] is False
    assert [p["repeat"] for p in fake.policies] == [0]
    assert fake.policies[0]["steps"] == [
        {"delay": 1, "actions": [{"type": "builtin-user", "args": {"user_id": "u1"}}]}
    ]
    saved = json.loads((tmp_path / "setup.json").read_text("utf-8"))
    assert saved["ok"] is True
    assert saved["grafana_url"].startswith(
        "https://oncall.ops.acme.example/api/v2/grafana/incoming?token="
    )
    assert fake.keys[0]["query"] == bridge.ACK_QUERY == setup.ACK_QUERY
    assert fake.keys[0]["role"] == "user"
    assert saved["ack_token"] == f"token-{fake.keys[0]['id']}"
    assert not (tmp_path / "verification.json").exists()


def test_a_first_run_also_creates_a_key_that_can_only_read_the_alert_log(tmp_path: Path) -> None:
    fake = FakeGoAlert(tmp_path)
    run(tmp_path, fake)
    saved = json.loads((tmp_path / "setup.json").read_text("utf-8"))
    reader = next(k for k in fake.keys if k["name"] == setup.READ_KEY)
    assert reader["query"] == bridge.READ_QUERY == setup.READ_QUERY
    assert reader["query"].startswith("query ")
    assert "mutation" not in reader["query"]
    assert reader["role"] == "user"
    assert saved["read_token"] == f"token-{reader['id']}"
    assert saved["read_key_id"] == reader["id"]
    assert saved["read_token"] != saved["ack_token"]
    assert saved["read_query"] == setup.query_hash(setup.READ_QUERY)


def test_a_changed_read_query_mints_a_new_read_key_and_keeps_the_acknowledge_key(
    tmp_path: Path,
) -> None:
    fake = FakeGoAlert(tmp_path)
    run(tmp_path, fake)
    path = tmp_path / "setup.json"
    before = json.loads(path.read_text("utf-8"))
    path.write_text(json.dumps({**before, "read_query": "older"}), "utf-8")
    run(tmp_path, fake)
    after = json.loads(path.read_text("utf-8"))
    assert after["ack_token"] == before["ack_token"]
    assert after["read_token"] != before["read_token"]
    assert [k["name"] for k in fake.keys].count(setup.READ_KEY) == 2


def test_a_second_run_changes_nothing_and_keeps_the_acknowledge_key(tmp_path: Path) -> None:
    fake = FakeGoAlert(tmp_path)
    run(tmp_path, fake)
    before = json.loads((tmp_path / "setup.json").read_text("utf-8"))
    fake.ops.clear()
    summary = run(tmp_path, fake)
    assert summary == {"created_methods": [], "verified_webhook": False, "policy": before["policy"]}
    assert json.loads((tmp_path / "setup.json").read_text("utf-8")) == before
    assert not {
        "createUserContactMethod",
        "createEscalationPolicy",
        "createService",
        "createGQLAPIKey",
        "deleteAll",
    } & set(fake.ops)
    assert len(fake.methods) == 2
    assert len(fake.keys) == 2


def test_a_lost_state_file_mints_a_new_acknowledge_key(tmp_path: Path) -> None:
    fake = FakeGoAlert(tmp_path)
    run(tmp_path, fake)
    (tmp_path / "setup.json").unlink()
    run(tmp_path, fake)
    assert len(fake.keys) == 4


def test_changed_escalation_steps_make_a_new_policy_and_move_the_service(tmp_path: Path) -> None:
    fake = FakeGoAlert(tmp_path)
    run(tmp_path, fake)
    steps = [
        {"delay_minutes": 5, "webhook": "http://autopilot:8080/diagnose"},
        {"delay_minutes": 0, "notify": "person"},
    ]
    run(tmp_path, fake, AYE_ONCALL_ESCALATION=json.dumps(steps))
    assert len(fake.policies) == 2
    newest = fake.policies[-1]
    assert newest["steps"] == [
        {
            "delay": 5,
            "actions": [
                {
                    "type": "builtin-webhook",
                    "args": {"webhook_url": "http://autopilot:8080/diagnose"},
                }
            ],
        },
        {"delay": 1, "actions": [{"type": "builtin-user", "args": {"user_id": "u1"}}]},
    ]
    assert fake.services[0]["escalationPolicyID"] == newest["id"]


def test_a_moved_webhook_address_replaces_the_contact_method(tmp_path: Path) -> None:
    fake = FakeGoAlert(tmp_path)
    run(tmp_path, fake)
    run(tmp_path, fake, AYE_ONCALL_WEBHOOK_SECRET="rotated")
    webhooks = [m for m in fake.methods if m["name"] == "Telegram oncall bridge"]
    assert len(webhooks) == 1
    assert webhooks[0]["dest"]["args"]["webhook_url"].endswith("/goalert/rotated")
    assert "deleteAll" in fake.ops


def test_without_mail_there_is_no_email_contact_method(tmp_path: Path) -> None:
    fake = FakeGoAlert(tmp_path)
    run(tmp_path, fake, AYE_ONCALL_SMTP_ADDRESS="")
    assert [m["name"] for m in fake.methods] == ["Telegram oncall bridge"]
    assert fake.config["SMTP.Enable"] == "false"
    fresh = FakeGoAlert(tmp_path)
    run(tmp_path, fresh, AYE_ONCALL_EMAIL="false")
    assert [m["name"] for m in fresh.methods] == ["Telegram oncall bridge"]


def test_twilio_adds_a_delayed_voice_call_only_with_a_phone(tmp_path: Path) -> None:
    parsed = settings(
        tmp_path,
        AYE_ONCALL_TWILIO_ACCOUNT_SID="AC1",
        AYE_ONCALL_TWILIO_AUTH_TOKEN="t",
        AYE_ONCALL_TWILIO_FROM_NUMBER="+15550000",
        AYE_ONCALL_PERSON_PHONE="+380501234567",
        AYE_ONCALL_VOICE_AFTER_MINUTES="15",
    )
    methods = setup.wanted_methods(parsed)
    assert methods[-1] == (
        "Voice Twilio",
        "builtin-twilio-voice",
        {"phone_number": "+380501234567"},
        15,
    )
    values = {v["id"]: v["value"] for v in setup.config_values(parsed)}
    assert values["Twilio.Enable"] == "true"
    assert values["Twilio.AccountSID"] == "AC1"
    without_phone = settings(tmp_path, AYE_ONCALL_TWILIO_ACCOUNT_SID="AC1")
    assert all(m[1] != "builtin-twilio-voice" for m in setup.wanted_methods(without_phone))


def test_a_webhook_code_that_never_arrives_fails_the_setup(tmp_path: Path) -> None:
    fake = FakeGoAlert(tmp_path, deliver_code=False)
    parsed = settings(tmp_path)
    api = setup.GoAlert(parsed, fake)
    api.login()
    user_id = setup.ensure_user(api, parsed)
    setup.ensure_methods(api, parsed, user_id)
    naps: list[float] = []
    with pytest.raises(setup.SetupError, match="verification code"):
        setup.verify_webhook(api, parsed, user_id, sleep=naps.append, wait=3.0)
    assert naps == [1.0, 1.0, 1.0]


def test_a_wrong_password_and_graphql_errors_are_setup_errors(tmp_path: Path) -> None:
    parsed = settings(tmp_path, AYE_ONCALL_ADMIN_PASSWORD="nope")
    with pytest.raises(setup.SetupError, match="login failed with HTTP 400"):
        setup.GoAlert(parsed, FakeGoAlert(tmp_path)).login()

    def failing(request: urllib.request.Request) -> tuple[int, bytes]:
        return 200, json.dumps({"errors": [{"message": "can only contain letters"}]}).encode()

    api = setup.GoAlert(settings(tmp_path), failing)
    with pytest.raises(setup.SetupError, match="can only contain letters"):
        api.gql("query { user { id } }")


def test_converge_retries_with_growing_pauses_until_goalert_answers(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeGoAlert(tmp_path)
    down = {"left": 4}

    def flaky(request: urllib.request.Request) -> tuple[int, bytes]:
        if down["left"]:
            down["left"] -= 1
            raise OSError("connection refused")
        return fake(request)

    reports: list[bool] = []
    naps: list[float] = []
    env = {**ENV, "AYE_ONCALL_STATE": str(tmp_path)}
    summary = setup.converge(env, reports.append, naps.append, pause=10.0, longest=60.0, http=flaky)
    assert summary["verified_webhook"] is True
    assert reports == [False, False, False, False, True]
    assert naps == [10.0, 20.0, 40.0, 60.0]
    events = [json.loads(line)["event"] for line in capsys.readouterr().out.splitlines()]
    assert events == ["oncall-setup-retry"] * 4 + ["oncall-setup-done"]


def test_the_bridge_loads_setup_in_a_fresh_isolated_interpreter_as_in_its_container(
    tmp_path: Path,
) -> None:
    child = tmp_path / "child.py"
    lines = [
        "import importlib.util, sys",
        "path = sys.argv[1]",
        "spec = importlib.util.spec_from_file_location('oncall_bridge', path)",
        "bridge = importlib.util.module_from_spec(spec)",
        "sys.modules['oncall_bridge'] = bridge",
        "spec.loader.exec_module(bridge)",
        "setup = bridge.load_setup()",
        "env = {'AYE_ONCALL_PUBLIC_URL': 'https://x', 'AYE_ONCALL_ADMIN_USER': 'a',",
        "       'AYE_ONCALL_ADMIN_PASSWORD': 'p', 'AYE_ONCALL_WEBHOOK_SECRET': 's'}",
        "print(setup.settings_from(env).bridge)",
    ]
    child.write_text(chr(10).join(lines) + chr(10), "utf-8")
    result = subprocess.run(
        [sys.executable, "-I", str(child), str(FILES / "oncall_bridge.py")],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "http://oncall-bridge:8080"


def test_the_bridge_loads_setup_from_its_own_folder() -> None:
    loaded = bridge.load_setup()
    assert loaded.ACK_QUERY == bridge.ACK_QUERY
    with pytest.raises(FileNotFoundError):
        bridge.load_setup(FILES / "missing.py")


def test_names_goalert_accepts_have_no_brackets() -> None:
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_' ")
    for name in (
        setup.BRIDGE_METHOD,
        setup.EMAIL_METHOD,
        setup.VOICE_METHOD,
        setup.SERVICE,
        setup.POLICY,
        setup.ACK_KEY,
        setup.READ_KEY,
    ):
        assert set(name) <= allowed, name


def test_the_state_file_is_private(tmp_path: Path) -> None:
    path = tmp_path / "setup.json"
    setup.write_private(path, {"ack_token": "x"})
    assert json.loads(path.read_text("utf-8")) == {"ack_token": "x"}
    if sys.platform != "win32":
        assert path.stat().st_mode & 0o777 == 0o600
