from __future__ import annotations

import hashlib
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ACK_QUERY = (
    "mutation ($ids: [Int!]!) "
    "{ updateAlerts(input: {alertIDs: $ids, newStatus: StatusAcknowledged}) { id } }"
)
READ_QUERY = (
    "query ($input: AlertSearchOptions) { alerts(input: $input) { nodes "
    "{ alertID status summary details createdAt "
    "recentEvents(input: {limit: 100}) { nodes { timestamp message } } } "
    "pageInfo { endCursor hasNextPage } } }"
)
SERVICE = "AyeOps on-call"
POLICY = "AyeOps on-call"
BRIDGE_METHOD = "Telegram oncall bridge"
EMAIL_METHOD = "Email"
VOICE_METHOD = "Voice Twilio"
ACK_KEY = "oncall-bridge acknowledge"
READ_KEY = "oncall-bridge alert log reader"
WEBHOOK = "builtin-webhook"
EMAIL = "builtin-smtp-email"
VOICE = "builtin-twilio-voice"
USER = "builtin-user"
DEFAULT_STEPS = '[{"delay_minutes": 0, "notify": "person"}]'
ME = "query { user { id name email } }"
METHODS = (
    "query ($id: ID!) { user(id: $id) { contactMethods { id name disabled dest { type args } } } }"
)
POLICIES = "query ($s: String!) { escalationPolicies(input: {search: $s}) { nodes { id name } } }"
SERVICES = (
    "query ($s: String!) { services(input: {search: $s}) { nodes "
    "{ id name escalationPolicyID integrationKeys { id type href } } } }"
)
KEYS = "query { gqlAPIKeys { id name } }"
DELETE = "mutation ($ids: [TargetInput!]) { deleteAll(input: $ids) }"


class SetupError(RuntimeError):
    pass


Http = Callable[[urllib.request.Request], tuple[int, bytes]]


def urlopen_http(request: urllib.request.Request) -> tuple[int, bytes]:
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return int(response.status), response.read()
    except urllib.error.HTTPError as error:
        return int(error.code), error.read()


@dataclass(frozen=True)
class Settings:
    goalert: str
    public: str
    user: str
    password: str
    name: str
    email: str
    bridge: str
    secret: str
    steps: list[dict[str, Any]]
    send_email: bool
    smtp: dict[str, str]
    twilio: dict[str, str]
    phone: str
    voice_after: int
    state_dir: Path


def settings_from(environ: Mapping[str, str]) -> Settings:
    smtp: dict[str, str] = {}
    if environ.get("AYE_ONCALL_SMTP_ADDRESS"):
        smtp = {
            "Address": environ["AYE_ONCALL_SMTP_ADDRESS"],
            "From": environ.get("AYE_ONCALL_SMTP_FROM", ""),
            "Username": environ.get("AYE_ONCALL_SMTP_USER", ""),
            "Password": environ.get("AYE_ONCALL_SMTP_PASSWORD", ""),
            "DisableTLS": environ.get("AYE_ONCALL_SMTP_DISABLE_TLS", "false"),
        }
    twilio: dict[str, str] = {}
    if environ.get("AYE_ONCALL_TWILIO_ACCOUNT_SID"):
        twilio = {
            "AccountSID": environ["AYE_ONCALL_TWILIO_ACCOUNT_SID"],
            "AuthToken": environ.get("AYE_ONCALL_TWILIO_AUTH_TOKEN", ""),
            "FromNumber": environ.get("AYE_ONCALL_TWILIO_FROM_NUMBER", ""),
        }
    return Settings(
        goalert=environ.get("AYE_ONCALL_GOALERT", "http://goalert:8081").rstrip("/"),
        public=environ["AYE_ONCALL_PUBLIC_URL"].rstrip("/"),
        user=environ["AYE_ONCALL_ADMIN_USER"],
        password=environ["AYE_ONCALL_ADMIN_PASSWORD"],
        name=environ.get("AYE_ONCALL_PERSON_NAME", ""),
        email=environ.get("AYE_ONCALL_PERSON_EMAIL", ""),
        bridge=environ.get("AYE_ONCALL_BRIDGE", "http://oncall-bridge:8080").rstrip("/"),
        secret=environ["AYE_ONCALL_WEBHOOK_SECRET"],
        steps=list(json.loads(environ.get("AYE_ONCALL_ESCALATION") or DEFAULT_STEPS)),
        send_email=environ.get("AYE_ONCALL_EMAIL", "true") == "true",
        smtp=smtp,
        twilio=twilio,
        phone=environ.get("AYE_ONCALL_PERSON_PHONE", ""),
        voice_after=int(environ.get("AYE_ONCALL_VOICE_AFTER_MINUTES", "10")),
        state_dir=Path(environ.get("AYE_ONCALL_STATE", "/tmp")),
    )


def config_values(settings: Settings) -> list[dict[str, str]]:
    values = [
        {"id": "General.ApplicationName", "value": "AyeOps on-call"},
        {"id": "Webhook.Enable", "value": "true"},
        {"id": "Webhook.AllowedURLs", "value": settings.bridge + "/"},
        {"id": "SMTP.Enable", "value": str(settings.send_email and bool(settings.smtp)).lower()},
        {"id": "Twilio.Enable", "value": str(bool(settings.twilio)).lower()},
    ]
    values += [{"id": f"SMTP.{key}", "value": value} for key, value in settings.smtp.items()]
    values += [{"id": f"Twilio.{key}", "value": value} for key, value in settings.twilio.items()]
    return values


def bridge_url(settings: Settings) -> str:
    return f"{settings.bridge}/goalert/{settings.secret}"


def step_actions(step: Mapping[str, Any], user_id: str) -> list[dict[str, Any]]:
    if "webhook" in step:
        return [{"type": WEBHOOK, "args": {"webhook_url": str(step["webhook"])}}]
    return [{"type": USER, "args": {"user_id": user_id}}]


def step_delay(step: Mapping[str, Any]) -> int:
    return max(1, int(step.get("delay_minutes", 0)))


def policy_signature(settings: Settings, user_id: str) -> str:
    shape = [
        {"delay": step_delay(step), "actions": step_actions(step, user_id)}
        for step in settings.steps
    ]
    return hashlib.sha256(json.dumps(shape, sort_keys=True).encode()).hexdigest()[:10]


def wanted_methods(settings: Settings) -> list[tuple[str, str, dict[str, str], int]]:
    methods = [(BRIDGE_METHOD, WEBHOOK, {"webhook_url": bridge_url(settings)}, 0)]
    if settings.send_email and settings.smtp and settings.email:
        methods.append((EMAIL_METHOD, EMAIL, {"email_address": settings.email}, 0))
    if settings.twilio and settings.phone:
        voice = {"phone_number": settings.phone}
        methods.append((VOICE_METHOD, VOICE, voice, settings.voice_after))
    return methods


class GoAlert:
    def __init__(self, settings: Settings, http: Http = urlopen_http) -> None:
        self.settings = settings
        self.http = http
        self.token = ""

    def login(self) -> None:
        form = {"username": self.settings.user, "password": self.settings.password}
        request = urllib.request.Request(
            self.settings.goalert + "/api/v2/identity/providers/basic?noRedirect=1",
            data=urllib.parse.urlencode(form).encode(),
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Referer": self.settings.public + "/",
            },
        )
        code, data = self.http(request)
        if code != 200 or not data.strip():
            raise SetupError(f"login failed with HTTP {code}")
        self.token = data.decode().strip()

    def gql(self, query: str, variables: Mapping[str, Any] | None = None) -> dict[str, Any]:
        request = urllib.request.Request(
            self.settings.goalert + "/api/graphql",
            data=json.dumps({"query": query, "variables": dict(variables or {})}).encode(),
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + self.token},
        )
        code, data = self.http(request)
        answer = json.loads(data or b"{}")
        if code != 200 or answer.get("errors"):
            messages = [str(e.get("message", "")) for e in answer.get("errors") or []]
            raise SetupError(f"graphql HTTP {code}: {'; '.join(messages)[:300]}")
        return dict(answer.get("data") or {})

    def mutate(self, name: str, input_type: str, payload: Any, select: str = "") -> Any:
        query = f"mutation ($input: {input_type}) {{ {name}(input: $input) {select} }}"
        return self.gql(query, {"input": payload})[name]


def ensure_user(api: GoAlert, settings: Settings) -> str:
    user = api.gql(ME)["user"]
    patch: dict[str, Any] = {"id": user["id"]}
    if settings.name and user.get("name") != settings.name:
        patch["name"] = settings.name
    if settings.email and user.get("email") != settings.email:
        patch["email"] = settings.email
    if len(patch) > 1:
        api.mutate("updateUser", "UpdateUserInput!", patch)
    return str(user["id"])


def ensure_methods(api: GoAlert, settings: Settings, user_id: str) -> list[str]:
    existing = {m["name"]: m for m in api.gql(METHODS, {"id": user_id})["user"]["contactMethods"]}
    created = []
    for name, kind, args, delay in wanted_methods(settings):
        current = existing.get(name)
        if current is not None and current["dest"] == {"type": kind, "args": args}:
            continue
        if current is not None:
            api.gql(DELETE, {"ids": [{"id": current["id"], "type": "contactMethod"}]})
        method = {
            "userID": user_id,
            "name": name,
            "dest": {"type": kind, "args": args},
            "enableStatusUpdates": kind == WEBHOOK,
            "newUserNotificationRule": {"delayMinutes": delay},
        }
        api.mutate("createUserContactMethod", "CreateUserContactMethodInput!", method, "{ id }")
        created.append(name)
    return created


def verify_webhook(
    api: GoAlert,
    settings: Settings,
    user_id: str,
    sleep: Callable[[float], None] = time.sleep,
    wait: float = 60.0,
) -> bool:
    methods = api.gql(METHODS, {"id": user_id})["user"]["contactMethods"]
    method = next((m for m in methods if m["name"] == BRIDGE_METHOD), None)
    if method is None or not method["disabled"]:
        return False
    codes = settings.state_dir / "verification.json"
    codes.unlink(missing_ok=True)
    payload = {"contactMethodID": method["id"]}
    api.mutate("sendContactMethodVerification", "SendContactMethodVerificationInput!", payload)
    waited = 0.0
    while waited < wait:
        code = str(read_json(codes).get("code") or "")
        if code.isdigit():
            check = {"contactMethodID": method["id"], "code": int(code)}
            api.mutate("verifyContactMethod", "VerifyContactMethodInput!", check)
            codes.unlink(missing_ok=True)
            return True
        sleep(1.0)
        waited += 1.0
    raise SetupError("the bridge did not receive the webhook verification code")


def ensure_policy(api: GoAlert, settings: Settings, user_id: str) -> str:
    name = f"{POLICY} {policy_signature(settings, user_id)}"
    for node in api.gql(POLICIES, {"s": name})["escalationPolicies"]["nodes"]:
        if node["name"] == name:
            return str(node["id"])
    policy = {"name": name, "repeat": 0, "description": "Rendered by AyeOps."}
    created = api.mutate("createEscalationPolicy", "CreateEscalationPolicyInput!", policy, "{ id }")
    policy_id = str(created["id"])
    for step in settings.steps:
        payload = {
            "escalationPolicyID": policy_id,
            "delayMinutes": step_delay(step),
            "actions": step_actions(step, user_id),
        }
        api.mutate(
            "createEscalationPolicyStep", "CreateEscalationPolicyStepInput!", payload, "{ id }"
        )
    return policy_id


def find_service(api: GoAlert) -> dict[str, Any] | None:
    nodes = api.gql(SERVICES, {"s": SERVICE})["services"]["nodes"]
    return next((dict(n) for n in nodes if n["name"] == SERVICE), None)


def ensure_service(api: GoAlert, policy_id: str) -> str:
    service = find_service(api)
    if service is None:
        payload = {
            "name": SERVICE,
            "escalationPolicyID": policy_id,
            "newIntegrationKeys": [{"type": "grafana", "name": "Grafana"}],
        }
        api.mutate("createService", "CreateServiceInput!", payload, "{ id }")
        service = find_service(api)
        if service is None:
            raise SetupError("service missing right after creation")
    if service["escalationPolicyID"] != policy_id:
        update = {"id": service["id"], "escalationPolicyID": policy_id}
        api.mutate("updateService", "UpdateServiceInput!", update)
    keys = [k for k in service["integrationKeys"] if k["type"] == "grafana"]
    if not keys:
        key = {"serviceID": service["id"], "type": "grafana", "name": "Grafana"}
        api.mutate("createIntegrationKey", "CreateIntegrationKeyInput!", key, "{ id }")
        refreshed = find_service(api) or {}
        keys = [k for k in refreshed.get("integrationKeys", []) if k["type"] == "grafana"]
        if not keys:
            raise SetupError("grafana integration key missing right after creation")
    return str(keys[0]["href"])


def ensure_ack_key(api: GoAlert, previous: Mapping[str, Any]) -> tuple[str, str]:
    known = {k["id"] for k in api.gql(KEYS)["gqlAPIKeys"]}
    if previous.get("ack_key_id") in known and previous.get("ack_token"):
        return str(previous["ack_key_id"]), str(previous["ack_token"])
    expires = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 5 * 365 * 86400))
    payload = {
        "name": ACK_KEY,
        "description": "Acknowledges alerts from the Telegram button.",
        "expiresAt": expires,
        "role": "user",
        "query": ACK_QUERY,
    }
    key = api.mutate("createGQLAPIKey", "CreateGQLAPIKeyInput!", payload, "{ id token }")
    return str(key["id"]), str(key["token"])


def query_hash(query: str) -> str:
    return hashlib.sha256(query.encode()).hexdigest()[:16]


def ensure_read_key(api: GoAlert, previous: Mapping[str, Any]) -> tuple[str, str]:
    known = {k["id"] for k in api.gql(KEYS)["gqlAPIKeys"]}
    current = previous.get("read_query") == query_hash(READ_QUERY)
    if current and previous.get("read_key_id") in known and previous.get("read_token"):
        return str(previous["read_key_id"]), str(previous["read_token"])
    expires = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 5 * 365 * 86400))
    payload = {
        "name": READ_KEY,
        "description": "Reads alerts and their log for the acknowledgements of the incidents.",
        "expiresAt": expires,
        "role": "user",
        "query": READ_QUERY,
    }
    key = api.mutate("createGQLAPIKey", "CreateGQLAPIKeyInput!", payload, "{ id token }")
    return str(key["id"]), str(key["token"])


def write_private(path: Path, data: Mapping[str, Any]) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data), "utf-8")
    temporary.chmod(0o600)
    temporary.replace(path)


def read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def run(settings: Settings, api: GoAlert) -> dict[str, Any]:
    api.login()
    api.mutate("setConfig", "[ConfigValueInput!]", config_values(settings))
    user_id = ensure_user(api, settings)
    created = ensure_methods(api, settings, user_id)
    verified = verify_webhook(api, settings, user_id)
    policy_id = ensure_policy(api, settings, user_id)
    grafana_url = ensure_service(api, policy_id)
    state_path = settings.state_dir / "setup.json"
    previous = read_json(state_path)
    key_id, token = ensure_ack_key(api, previous)
    read_id, read_token = ensure_read_key(api, previous)
    state = {
        "ok": True,
        "grafana_url": grafana_url,
        "ack_key_id": key_id,
        "ack_token": token,
        "read_key_id": read_id,
        "read_token": read_token,
        "read_query": query_hash(READ_QUERY),
        "policy": policy_id,
    }
    write_private(state_path, state)
    return {"created_methods": created, "verified_webhook": verified, "policy": policy_id}


def converge(
    environ: Mapping[str, str],
    report: Callable[[bool], None],
    sleep: Callable[[float], None] = time.sleep,
    pause: float = 10.0,
    longest: float = 300.0,
    http: Http = urlopen_http,
) -> dict[str, Any]:
    settings = settings_from(environ)
    api = GoAlert(settings, http)
    attempt = 0
    while True:
        try:
            summary = run(settings, api)
        except (SetupError, OSError, ValueError, KeyError, TypeError) as error:
            event = {"event": "oncall-setup-retry", "attempt": attempt, "error": str(error)[:300]}
            print(json.dumps(event), flush=True)
            report(False)
            sleep(min(longest, pause * 2 ** min(attempt, 8)))
            attempt += 1
            continue
        print(json.dumps({"event": "oncall-setup-done", **summary}), flush=True)
        report(True)
        return summary
