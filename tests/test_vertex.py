"""KAN-291: Claude Code on Vertex AI as a team model route: the credential, its egress, its check, its price.

No test calls Google: the credential check's two calls (gcloud and testIamPermissions) are faked."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from hangar import credhealth, egress, teamcreds, turnlogin
from hangar.config import Settings
from hangar.envspec import EnvSpec
from hangar.events import DEFAULT_PRICES, price_for
from hangar.harnesses import slot_for
from hangar.models import Credential, CredentialKind, Sandbox
from tests.conftest import scratch_root
from tests.test_credhealth import SA, _fake_gcloud

KEY = json.dumps({**json.loads(SA), "project_id": "acme-ml"})


def is_agent(h: str | None) -> bool:
    return h in ("claude-code", "codex", "pi", "antigravity")


# ====================================================================== the credential
def test_a_claude_code_service_account_key_turns_claude_code_to_vertex(tmp_path):
    slot = slot_for("claude-code", CredentialKind.GENERIC)
    assert slot is not None and slot.file_path == ".config/hangar/gcp-sa.json" and not slot.exclusive
    (tmp_path / ".config/hangar").mkdir(parents=True)
    (tmp_path / ".hangar").mkdir()
    (tmp_path / ".config/hangar/gcp-sa.json").write_text(KEY)
    env = {"HOME": str(tmp_path), "PATH": os.environ["PATH"]}
    subprocess.run(["sh", "-c", slot.post_inject], env=env, check=True)

    def sourced(**extra: str) -> dict[str, str]:
        out = subprocess.run(["sh", "-c", f"set -a; . {tmp_path}/.hangar/env; env"], env={**env, **extra},
                             check=True, capture_output=True, text=True).stdout
        return dict(line.split("=", 1) for line in out.splitlines() if "=" in line)

    got = sourced()
    assert got["CLAUDE_CODE_USE_VERTEX"] == "1" and got["ANTHROPIC_VERTEX_PROJECT_ID"] == "acme-ml"
    assert got["CLOUD_ML_REGION"] == "global"
    assert got["GOOGLE_APPLICATION_CREDENTIALS"] == f"{tmp_path}/.config/hangar/gcp-sa.json"
    # the environment's own project and region win
    got = sourced(ANTHROPIC_VERTEX_PROJECT_ID="acme-billing", CLOUD_ML_REGION="us-east5")
    assert got["ANTHROPIC_VERTEX_PROJECT_ID"] == "acme-billing" and got["CLOUD_ML_REGION"] == "us-east5"


def test_a_project_id_that_isnt_one_writes_nothing(tmp_path):
    slot = slot_for("claude-code", CredentialKind.GENERIC)
    (tmp_path / ".config/hangar").mkdir(parents=True)
    (tmp_path / ".hangar").mkdir()
    bad = {**json.loads(KEY), "project_id": "acme;touch pwned"}
    (tmp_path / ".config/hangar/gcp-sa.json").write_text(json.dumps(bad))
    env = {"HOME": str(tmp_path), "PATH": os.environ["PATH"]}
    rc = subprocess.run(["sh", "-c", slot.post_inject], env=env, capture_output=True).returncode
    assert rc != 0 and not (tmp_path / ".hangar/env").exists()


def test_a_sandbox_with_both_logins_is_warned(store, broker):
    broker.put("claude-vertex", "claude-code", CredentialKind.GENERIC, KEY, team=True)
    broker.put("claude-api", "claude-code", CredentialKind.API_KEY, "sk-ant-api03-" + "k" * 40, team=True)
    both = teamcreds._warnings({n: store.get(Credential, n) for n in ("claude-vertex", "claude-api")},
                               ["claude-vertex", "claude-api"], "automated")
    assert both == [teamcreds.VERTEX_OVERLAP]
    assert teamcreds._warnings({"claude-vertex": store.get(Credential, "claude-vertex")}, ["claude-vertex"],
                               "automated") == []


def test_adding_a_vertex_key_leaves_existing_team_jobs_where_they_were(store, broker):
    """KAN-291 review: an environment that names no credential takes every team Claude Code login; a Vertex key
    added beside an Anthropic key must not move those jobs (and their bill) to Google."""
    broker.put("claude-api", "claude-code", CredentialKind.API_KEY, "sk-ant-api03-" + "k" * 40, team=True)
    before = teamcreds.resolve(store, [], None, "claude-code", False, is_agent, purpose="automated")
    broker.put("claude-vertex", "claude-code", CredentialKind.GENERIC, KEY, team=True)
    after = teamcreds.resolve(store, [], None, "claude-code", False, is_agent, purpose="automated")
    assert before.names == after.names == ["claude-api"] and after.warnings == []
    # a person's own Vertex key beside their own Anthropic key doesn't move their work either
    broker.put("ana-api", "claude-code", CredentialKind.API_KEY, "sk-ant-api03-" + "a" * 40, person="ana")
    broker.put("ana-vertex", "claude-code", CredentialKind.GENERIC, KEY, person="ana")
    assert teamcreds.resolve(store, [], "ana", "claude-code", False, is_agent, purpose="automated").names == ["ana-api"]


def test_an_environment_chooses_vertex_by_naming_it(store, broker):
    broker.put("claude-api", "claude-code", CredentialKind.API_KEY, "sk-ant-api03-" + "k" * 40, team=True)
    broker.put("claude-vertex", "claude-code", CredentialKind.GENERIC, KEY, team=True)
    got = teamcreds.resolve(store, ["claude-vertex"], None, "claude-code", False, is_agent, purpose="automated")
    assert got.names == ["claude-vertex"] and got.warnings == []
    # naming both keeps both, and the sandbox is told Claude Code uses Vertex
    got = teamcreds.resolve(store, ["claude-api", "claude-vertex"], None, "claude-code", False, is_agent,
                            purpose="automated")
    assert sorted(got.names) == ["claude-api", "claude-vertex"] and got.warnings == [teamcreds.VERTEX_OVERLAP]


def test_it_is_an_api_key_class_login_that_automated_jobs_take(store, broker):
    c = broker.put("claude-vertex", "claude-code", CredentialKind.GENERIC, KEY, team=True)
    assert not teamcreds.is_subscription(c)
    teamcreds.check_team("claude-vertex", "claude-code", CredentialKind.GENERIC)  # no refusal
    got = teamcreds.resolve(store, [], None, "claude-code", False, is_agent, purpose="automated").names
    assert got == ["claude-vertex"]
    assert "docs/team/vertex.md" in teamcreds.refusal("claude-code")
    assert "vertex" not in teamcreds.refusal("codex")


def test_a_vertex_credential_is_a_login_for_the_turn(store, broker):
    broker.put("claude-vertex", "claude-code", CredentialKind.GENERIC, KEY, team=True)
    sb = Sandbox(id="sb-v", name="v", environment="plain", provider="docker", credentials=["claude-vertex"])
    assert turnlogin.has_credential(store, sb, "claude-code")
    assert not turnlogin.has_credential(store, sb, "codex")


# ====================================================================== egress
def _spec(**env: str) -> EnvSpec:
    return EnvSpec(name="api", harnesses=["claude-code"], egress={"mode": "allowlist"}, env=env)


def test_a_vertex_credential_opens_vertex_ai_and_nothing_else():
    s = Settings()
    plain = egress.resolve(s, _spec(), harness="claude-code")
    assert plain.allows("aiplatform.googleapis.com", 443) is None
    pol = egress.resolve(s, _spec(), harness="claude-code", vertex=True)
    for host in egress.VERTEX_HOSTS:
        assert pol.allows(host, 443).source == "harness:vertex"
    assert pol.allows("us-east5-aiplatform.googleapis.com", 443) is None
    assert pol.allows("generativelanguage.googleapis.com", 443) is None  # not Google's whole harness list
    # another harness's sandbox gets nothing for it
    assert egress.resolve(s, _spec(), harness="codex", vertex=True).allows("aiplatform.googleapis.com", 443) is None
    # mode none keeps it, like the harness's own hosts
    none = EnvSpec(name="api", harnesses=["claude-code"], egress={"mode": "none"})
    assert egress.resolve(s, none, harness="claude-code", vertex=True).allows("aiplatform.googleapis.com", 443)


def test_regions_add_their_endpoint_and_a_bad_region_is_named():
    s = Settings()
    pol = egress.resolve(s, _spec(CLOUD_ML_REGION="us-east5", VERTEX_REGION_CLAUDE_4_5_SONNET="europe-west1"),
                         harness="claude-code", vertex=True)
    assert pol.allows("us-east5-aiplatform.googleapis.com", 443)
    assert pol.allows("europe-west1-aiplatform.googleapis.com", 443)
    bad = egress.resolve(s, _spec(CLOUD_ML_REGION="evil.example.com/x"), harness="claude-code", vertex=True)
    assert any("is not a Vertex AI region" in n for n in bad.notes)
    assert not any("evil" in r.host for r in bad.rules)


def test_an_environment_on_vertex_through_its_cloud_binding_gets_the_hosts_too():
    s = Settings()
    pol = egress.resolve(s, _spec(CLAUDE_CODE_USE_VERTEX="1"), harness="claude-code")
    assert pol.allows("aiplatform.googleapis.com", 443).source == "harness:vertex"
    for value in ("0", "false", "False", "no", "off", ""):  # Claude Code's own reading of the variable
        off = egress.resolve(s, _spec(CLAUDE_CODE_USE_VERTEX=value), harness="claude-code")
        assert off.allows("aiplatform.googleapis.com", 443) is None, value
    on = egress.resolve(s, _spec(CLAUDE_CODE_USE_VERTEX="True"), harness="claude-code")
    assert on.allows("aiplatform.googleapis.com", 443) is not None


def test_for_sandbox_sees_the_vertex_credential(store, broker):
    broker.put("claude-vertex", "claude-code", CredentialKind.GENERIC, KEY, team=True)
    broker.put("claude-api", "claude-code", CredentialKind.API_KEY, "sk-ant-api03-" + "k" * 40, team=True)
    broker.put("pi-vertex", "pi", CredentialKind.GENERIC, KEY, team=True)
    assert egress.has_vertex_credential(store, ["claude-api", "claude-vertex"])
    assert not egress.has_vertex_credential(store, ["claude-api", "pi-vertex", "gone"])


# ====================================================================== the doctor's cheap call
@pytest.fixture
def gcloud_dir():
    tmp = Path(scratch_root()) / "gcloud-vertex"
    tmp.mkdir()
    try:
        yield tmp
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _iam(monkeypatch, code: int, body: dict) -> list:
    calls: list = []

    async def post(url: str, token: str, payload: dict):
        calls.append((url, token, payload))
        return code, body

    monkeypatch.setattr(credhealth, "_post_json", post)
    return calls


async def test_the_check_asks_for_the_predict_permission_without_calling_a_model(store, broker, gcloud_dir,
                                                                                 monkeypatch):
    calls = _iam(monkeypatch, 200, {"permissions": ["aiplatform.endpoints.predict"]})
    c = broker.put("claude-vertex", "claude-code", CredentialKind.GENERIC, KEY, team=True)
    assert credhealth.has_check(c)
    res = await credhealth.check(c, KEY.encode(), str(_fake_gcloud(gcloud_dir)))
    assert res.status == "ok" and "may call Claude on Vertex AI in acme-ml" in res.detail
    [(url, token, payload)] = calls
    assert url == "https://cloudresourcemanager.googleapis.com/v1/projects/acme-ml:testIamPermissions"
    assert token == "ya29.SECRET-ACCESS-TOKEN" and payload == {"permissions": ["aiplatform.endpoints.predict"]}
    assert "ya29" not in res.detail and "ya29" not in res.method
    # an API key keeps its own check, and pi's key isn't asked about Vertex permissions for Claude
    pi = broker.put("pi-vertex", "pi", CredentialKind.GENERIC, KEY)
    calls.clear()
    await credhealth.check(pi, KEY.encode(), str(_fake_gcloud(gcloud_dir)))
    assert calls == []


async def test_a_key_without_the_permission_fails_and_says_what_to_grant(broker, gcloud_dir, monkeypatch):
    c = broker.put("claude-vertex", "claude-code", CredentialKind.GENERIC, KEY, team=True)
    _iam(monkeypatch, 200, {})
    res = await credhealth.check(c, KEY.encode(), str(_fake_gcloud(gcloud_dir)))
    assert res.status == "fail" and "aiplatform.endpoints.predict" in res.detail
    _iam(monkeypatch, 403, {"error": {"status": "PERMISSION_DENIED"}})
    assert (await credhealth.check(c, KEY.encode(), str(_fake_gcloud(gcloud_dir)))).status == "fail"
    # a refused key fails before any Vertex call
    calls = _iam(monkeypatch, 200, {"permissions": ["aiplatform.endpoints.predict"]})
    bad = await credhealth.check(c, KEY.encode(), str(_fake_gcloud(gcloud_dir, print_token_rc=1)))
    assert bad.status == "fail" and "invalid_grant" in bad.detail and calls == []


async def test_an_unanswered_call_is_a_warn_never_an_ok(broker, gcloud_dir, monkeypatch):
    c = broker.put("claude-vertex", "claude-code", CredentialKind.GENERIC, KEY, team=True)
    _iam(monkeypatch, 403, {"error": {"details": [{"reason": "SERVICE_DISABLED"}]}})
    res = await credhealth.check(c, KEY.encode(), str(_fake_gcloud(gcloud_dir)))
    assert res.status == "warn" and "Cloud Resource Manager API is off" in res.detail

    async def boom(*_a):
        raise OSError("no route")

    monkeypatch.setattr(credhealth, "_post_json", boom)
    res = await credhealth.check(c, KEY.encode(), str(_fake_gcloud(gcloud_dir)))
    assert res.status == "warn" and "wasn't checked (OSError)" in res.detail


async def test_doctor_has_a_claude_on_vertex_line(fleet, store, broker, gcloud_dir, monkeypatch):
    h = SimpleNamespace(store=store, fleet=fleet)
    assert await credhealth.vertex_doctor_checks(h) == []  # no Vertex credential, no line
    broker.put("claude-vertex", "claude-code", CredentialKind.GENERIC, KEY, team=True)
    monkeypatch.setattr(shutil, "which", lambda _: str(_fake_gcloud(gcloud_dir)))
    _iam(monkeypatch, 200, {"permissions": ["aiplatform.endpoints.predict"]})
    [line] = await credhealth.vertex_doctor_checks(h)
    assert line["name"] == "claude on vertex" and line["status"] == "ok" and line["deploy_gate"] is False
    assert "claude-vertex (team)" in line["detail"] and "acme-ml" in line["detail"]
    assert store.get(Credential, "claude-vertex").health["by"] == "doctor"
    # a fresh result isn't checked again; a failing one fails the line
    calls = _iam(monkeypatch, 200, {})
    [line] = await credhealth.vertex_doctor_checks(h)
    assert calls == [] and line["status"] == "ok"
    c = store.get(Credential, "claude-vertex")
    c.health = {**c.health, "checked_at": 0}
    store.put(c)
    [line] = await credhealth.vertex_doctor_checks(h)
    assert line["status"] == "fail" and "aiplatform.endpoints.predict" in line["fix"]


# ====================================================================== usage
def test_vertex_model_ids_are_priced():
    """Claude Code on Vertex reports ids like claude-sonnet-4-5@20250929; /api/usage prices them."""
    for model in [m for m in DEFAULT_PRICES if m.startswith("claude-")]:
        assert price_for(f"{model}@20260101", DEFAULT_PRICES) is DEFAULT_PRICES[model], model
