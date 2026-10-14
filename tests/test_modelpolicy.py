"""Model policies (modelpolicy.py, pins.py): `frontier` and `balanced` resolved to a concrete id per provider, cached
with a TTL, the last id (or a built-in one) when a list can't be had, and the id pinned on sandboxes and sessions.

The lists are recordings: fixtures/models/openrouter.json is OpenRouter's public list (2026-10-14, trimmed); the
Anthropic, OpenAI and Google ones follow each API's documented response (Anthropic's `/v1/models`, newest first with
`line` and `lifecycle`, as docs.claude.com describes it; OpenAI's `/v1/models`; Gemini's `models.list`) with the ids
those vendors list today.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import httpx
import pytest

from hangar import modelpolicy, pins
from hangar.modelpolicy import Resolver, parse, pick
from hangar.models import CredentialKind
from hangar.repopolicy import RepoPolicy
from hangar.sessions import NewSession
from tests.test_session_streams import settle, svc, world  # noqa: F401 - fixtures

FIX = Path(__file__).parent / "fixtures" / "models"
FETCH = modelpolicy.fetch  # the real one, against a mock transport


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    """No provider is ever called from a test (OpenRouter's list needs no key, so a refresh would)."""
    async def fetch(provider, key, client):
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(modelpolicy, "fetch", fetch)


def recorded(provider: str) -> list[dict]:
    files = {"google": ["google.json", "google-2.json"]}.get(provider, [f"{provider}.json"])
    return [m for f in files for m in modelpolicy._normalize(provider, json.loads((FIX / f).read_text()))]


def cache(fleet, provider: str, models: list[dict] | None = None, age: float = 0) -> None:
    fleet.store.kv_set(f"modellist:{provider}", json.dumps(
        {"at": time.time() - age, "models": recorded(provider) if models is None else models}))


# ====================================================================== ranking
def test_policy_names_and_pinned_ids():
    assert parse("frontier") == ("frontier", None) and parse(" Balanced ") == ("balanced", None)
    assert parse("frontier@openai") == ("frontier", "openai")
    for pinned in (None, "", "opus", "claude-opus-5-5", "frontier@nobody", "openrouter/google/gemini-3.8-flash"):
        assert parse(pinned) is None


@pytest.mark.parametrize(("harness", "policy", "want"), [
    # Anthropic: the `line` field, not the id; deprecated models never; newest first within a line
    ("claude-code", "frontier", "claude-fable-5-1"),
    ("claude-code", "balanced", "claude-opus-5-5"),
    # OpenAI: tier, then version; only gpt-<version>-<tier> (no -pro, dated or special-purpose ids)
    ("codex", "frontier", "gpt-6-astra"),
    ("codex", "balanced", "gpt-6.1-sol"),
    # Google: stable before preview: 3.8 Flash leads 3.1 Pro Preview; image and embedding models never
    ("gemini", "frontier", "gemini-3.8-flash"),
    ("gemini", "balanced", "gemini-3.8-flash"),
    ("antigravity", "frontier", "gemini-3.8-flash-high"),  # agy's id: no -preview, a thinking level
])
def test_each_provider_ranks_its_recorded_list(harness, policy, want):
    assert pick(harness, recorded(modelpolicy.PROVIDER[harness]), policy) == want


def test_pi_on_openrouter_picks_per_vendor_and_skips_aliases_and_batch_ids():
    models = recorded("openrouter")
    assert pick("pi", models, "frontier") == "openrouter/anthropic/claude-fable-5.1"
    assert pick("pi", models, "balanced") == "openrouter/anthropic/claude-opus-5.5"
    assert pick("pi", models, "frontier", "openai") == "openrouter/openai/gpt-6-astra"
    assert pick("pi", models, "frontier", "google") == "openrouter/google/gemini-3.8-flash"
    assert pick("pi", [m for m in models if not m["id"].startswith("anthropic/")], "frontier") == \
        "openrouter/openai/gpt-6-astra", "no Anthropic model: the next vendor's"


def test_google_newest_version_then_stable_then_tier_and_agy_drops_the_preview_suffix():
    pro = [{"id": "gemini-3.1-pro-preview"}, {"id": "gemini-2.5-flash"}]
    assert pick("gemini", pro, "frontier") == "gemini-3.1-pro-preview", "a newer version beats an older stable one"
    assert pick("antigravity", pro, "frontier") == "gemini-3.1-pro-high"
    assert pick("gemini", [{"id": "gemini-3.8-pro-preview"}, {"id": "gemini-3.8-flash"}], "frontier") == \
        "gemini-3.8-flash", "same version: the stable one"
    assert pick("gemini", [{"id": "gemini-3.8-flash"}, {"id": "gemini-3.8-pro"}], "frontier") == "gemini-3.8-pro"


def test_nothing_qualifies():
    assert pick("claude-code", [{"id": "claude-x", "line": None}], "frontier") is None
    assert pick("codex", [{"id": "o9"}], "balanced") is None
    assert pick("plain-agent", recorded("openai"), "frontier") is None


# ====================================================================== the lists
async def test_fetch_pages_through_each_list_with_its_own_auth():
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        if req.url.host == "api.anthropic.com":
            if req.url.params.get("after_id") == "claude-opus-4-8":
                return httpx.Response(200, json={"data": [{"id": "claude-old", "line": "opus", "lifecycle": "retired"}],
                                                 "has_more": False})
            return httpx.Response(200, json={**json.loads((FIX / "anthropic.json").read_text()), "has_more": True})
        if req.url.host == "generativelanguage.googleapis.com":
            page = "google-2.json" if req.url.params.get("pageToken") == "page-2" else "google.json"
            return httpx.Response(200, json=json.loads((FIX / page).read_text()))
        if req.url.host == "api.openai.com":
            return httpx.Response(200, json=json.loads((FIX / "openai.json").read_text()))
        return httpx.Response(200, json=json.loads((FIX / "openrouter.json").read_text()))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        ant = await FETCH("anthropic", "sk-ant", client)
        goo = await FETCH("google", "AIza", client)
        oai = await FETCH("openai", "sk-oai", client)
        orr = await FETCH("openrouter", None, client)
    assert [m["id"] for m in ant][-1] == "claude-old" and ant[0] == {
        "id": "claude-haiku-5-5", "line": "haiku", "lifecycle": "active", "created": "2026-10-07T00:00:00Z"}
    assert "gemini-embedding-2" not in [m["id"] for m in goo] and "gemini-2.5-pro" in [m["id"] for m in goo]
    assert {"id": "gpt-6-astra", "created": 1788552838} in oai
    assert not [m for m in orr if m["id"].startswith("google/gemini-3-pro-image")], "image output: not a coding model"
    ant_req = next(r for r in seen if r.url.host == "api.anthropic.com")
    assert ant_req.headers["x-api-key"] == "sk-ant" and ant_req.headers["anthropic-version"] == "2023-06-01"
    assert ant_req.url.params["limit"] == "1000"
    assert next(r for r in seen if r.url.host == "api.openai.com").headers["authorization"] == "Bearer sk-oai"
    assert next(r for r in seen if r.url.host.startswith("generative")).headers["x-goog-api-key"] == "AIza"
    assert "authorization" not in next(r for r in seen if r.url.host == "openrouter.ai").headers


async def test_refresh_caches_the_list_and_records_an_error_without_the_key(fleet, broker, monkeypatch):
    broker.put("antkey", "claude-code", CredentialKind.API_KEY, "sk-ant-SECRET")
    fleet.settings.model_list_credentials = {"anthropic": "antkey"}
    status = {"code": 200}

    async def fake_fetch(provider, key, client):
        assert key == "sk-ant-SECRET"
        if status["code"] != 200:
            req = httpx.Request("GET", modelpolicy.LISTS[provider], headers={"x-api-key": key})
            raise httpx.HTTPStatusError("nope sk-ant-SECRET", request=req, response=httpx.Response(401, request=req))
        return recorded(provider)

    monkeypatch.setattr(modelpolicy, "fetch", fake_fetch)
    r = Resolver(fleet)
    await r.refresh("anthropic")
    assert r.cached("anthropic")["models"][0]["id"] == "claude-haiku-5-5"
    status["code"] = 401
    with pytest.raises(httpx.HTTPStatusError):
        await r.refresh("anthropic")
    err = fleet.store.kv_get("modellist-error:anthropic")
    assert "401" in err and "SECRET" not in err
    assert r.cached("anthropic") is not None, "the last good list stays"
    with pytest.raises(LookupError, match="HANGAR_MODEL_LIST_CREDENTIALS"):
        await r.refresh("openai")


# ====================================================================== resolution
def test_resolution_live_stale_last_and_seed(fleet):
    r = Resolver(fleet)
    assert r.resolve("claude-code", "claude-opus-5-5").to_dict() == {"model": "claude-opus-5-5", "source": "pinned"}
    assert r.resolve("claude-code", None).model is None
    # no key, never listed: the built-in id, and why
    seed = r.resolve("claude-code", "frontier")
    assert (seed.model, seed.source) == ("claude-fable-5-1", "seed")
    assert "HANGAR_MODEL_LIST_CREDENTIALS" in seed.note and "built-in" in seed.note
    # a fresh list
    cache(fleet, "anthropic")
    live = r.resolve("claude-code", "balanced")
    assert (live.model, live.policy, live.source, live.note) == ("claude-opus-5-5", "balanced", "live", None)
    # a stale one is still used, and says so
    cache(fleet, "anthropic", age=r.ttl + 60)
    stale = r.resolve("claude-code", "balanced")
    assert stale.source == "cached" and stale.model == "claude-opus-5-5" and "old" in stale.note
    # the list is gone (or useless): the last id resolved for this harness and policy
    cache(fleet, "anthropic", models=[])
    last = r.resolve("claude-code", "balanced")
    assert (last.model, last.source) == ("claude-opus-5-5", "last") and "last id resolved" in last.note
    # a listing only looks
    cache(fleet, "openai")
    assert r.resolve("codex", "frontier", record=False).model == "gpt-6-astra"
    assert fleet.store.kv_get("modelpick:codex:frontier") is None
    # a harness with no model list runs its own default, never the policy's name
    assert r.resolve("plain-agent", "frontier").model is None


async def test_a_stale_list_is_refreshed_in_the_background_once(fleet, broker, monkeypatch):
    broker.put("oaikey", "codex", CredentialKind.API_KEY, "sk-oai")
    fleet.settings.model_list_credentials = {"openai": "oaikey"}
    calls: list[str] = []
    gate = asyncio.Event()

    async def fake_fetch(provider, key, client):
        calls.append(provider)
        await gate.wait()
        return [{"id": "gpt-7-astra", "created": 2}]

    monkeypatch.setattr(modelpolicy, "fetch", fake_fetch)
    r = modelpolicy.of(fleet)
    cache(fleet, "openai", age=r.ttl + 1)
    assert r.resolve("codex", "frontier").model == "gpt-6-astra", "a create never waits on the provider"
    r.resolve("codex", "frontier")
    await asyncio.sleep(0)
    assert calls == ["openai"]
    gate.set()
    await asyncio.sleep(0.05)
    assert r.resolve("codex", "frontier").to_dict() == {"model": "gpt-7-astra", "policy": "frontier", "source": "live"}
    await r.stop()


def test_a_repo_policy_names_a_policy_for_every_harness():
    p = RepoPolicy(repo="acme/app", model="frontier", harness="codex")
    assert p.model_for("claude-code", "claude-code") == "frontier"
    assert RepoPolicy(repo="acme/app", model="gpt-6.1-sol", harness="codex").model_for("claude-code", None) is None


# ====================================================================== pinned on sandboxes and sessions
@pytest.fixture
def creds(broker):
    broker.put("claude", "claude-code", CredentialKind.API_KEY, "sk-claude")
    broker.put("oai", "codex", CredentialKind.API_KEY, "sk-codex")
    broker.put("orkey", "pi", CredentialKind.API_KEY, "sk-or")
    broker.put("ghtok", "generic-secret", CredentialKind.GENERIC, "ghp_123", env_var="UNUSED_GH")
    return broker


async def test_a_sandbox_runs_the_id_resolved_at_create(fleet, creds, tmp_path):
    fleet.catalog.dirs = [tmp_path / "saved", *fleet.catalog.dirs]
    fleet.clone_environment("agents", "agents-frontier", {"model": "frontier"})
    cache(fleet, "anthropic")
    sb = await fleet.create("agents-frontier", harness="claude-code")
    assert sb.info["model"] == "claude-fable-5-1" and sb.info["model_policy"] == "frontier"
    cache(fleet, "anthropic", models=[{"id": "claude-fable-6", "line": "fable", "lifecycle": "active"},
                                      {"id": "claude-opus-6", "line": "opus", "lifecycle": "active"}])
    run = await fleet.run_agent(sb.id, "hi")
    assert run["model"] == "claude-fable-5-1", "the list moved; this sandbox didn't"
    await fleet.terminal_kill(sb.id, run["run_id"])
    run = await fleet.run_agent(sb.id, "hi", model="balanced")
    assert run["model"] == "claude-opus-6", "a policy named for one run is resolved for it"
    await fleet.terminal_kill(sb.id, run["run_id"])
    envs = {e["name"]: e for e in fleet.environments()}
    assert envs["agents-frontier"]["models"]["claude-code"] == {"model": "claude-fable-6", "policy": "frontier",
                                                                "source": "live"}
    assert envs["agents"]["models"]["codex"] == {"model": "sonnet", "source": "pinned"}


@pytest.fixture
def model_agent(monkeypatch, world):  # noqa: F811
    """The session tests' scripted agent, as if its provider were Anthropic."""
    monkeypatch.setitem(modelpolicy.PROVIDER, "preset-agent", "anthropic")
    (world["envs"] / "sesenv.yaml").write_text("name: sesenv\nharnesses: [preset-agent, capture-agent, plain-agent]\n"
                                               "default_harness: preset-agent\nmodel: frontier\n")


async def test_a_session_keeps_the_model_it_was_created_with(svc, world, fleet, model_agent):  # noqa: F811
    cache(fleet, "anthropic")
    svc.fleet.catalog.reload()
    svc.new(NewSession(name="pin", environment="sesenv", repo=world["url"], message="start"), owner="admin",
            requester="sean")
    s = await settle(svc, "pin")
    assert (s.model, s.model_policy) == ("claude-fable-5-1", "frontier")
    cache(fleet, "anthropic", models=[{"id": "claude-fable-6", "line": "fable", "lifecycle": "active"}])
    svc.send("pin", "continue", requester="sean")
    s = await settle(svc, "pin")
    assert s.model == "claude-fable-5-1", "spec 0012: never another model mid-conversation"
    runs = fleet.get(s.sandbox_id).info["runs"]
    assert {r["model"] for r in runs} == {"claude-fable-5-1"}
    svc.send("pin", "continue", requester="sean", model="frontier")  # a deliberate switch: resolved now, warned
    s = await settle(svc, "pin")
    assert s.model == "claude-fable-6" and any("model switched" in w for w in s.turns[-1].warnings)


async def test_a_job_session_defaults_to_frontier_and_says_where_the_id_came_from(svc, world, fleet):  # noqa: F811
    s = svc.for_job(job_id="job-12345678", environment="agent", harness="codex", model=None, repo=None,
                    base_branch=None, branch="b", provider=None, labels={}, extra_dirs=[], idle_minutes=5)
    assert (s.model, s.model_policy) == ("gpt-6.1-sol", "frontier"), "no list: the built-in frontier for codex"
    events = svc.activity.read(s.id, 0, 10, False)
    assert any(e.data.get("warning") == "model_resolution" and "built-in" in e.text for e in events)


# ====================================================================== the CLI version a session is pinned to
async def test_a_follow_up_on_a_newer_cli_hands_off(svc, world, fleet):  # noqa: F811
    svc.new(NewSession(name="ver", environment="sesenv", repo=world["url"], message="start"), owner="admin",
            requester="sean")
    s = await settle(svc, "ver")
    assert s.harness_session and s.turns[0].mode == "new"
    s.harness_version = "1.0.0"
    sb = fleet.get(s.sandbox_id)
    sb.info["harness_lock"] = "1.0.0"
    fleet.store.put(sb)
    svc.send("ver", "continue", requester="sean")
    s = await settle(svc, "ver")
    assert s.turns[-1].mode == "resume", "same version: the conversation resumes"
    sb = fleet.get(s.sandbox_id)
    sb.info["harness_lock"] = "1.1.0"  # the lock moved and this sandbox runs the new CLI
    fleet.store.put(sb)
    svc.send("ver", "continue", requester="sean")
    s = await settle(svc, "ver")
    assert s.turns[-1].mode == "handoff" and "1.0.0 -> 1.1.0" in s.turns[-1].warnings[0]
    assert s.harness_version == "1.1.0"
    assert [a.detail for a in fleet.store.audit_tail() if a.action == "session.harness_version"] == [
        {"harness": "preset-agent", "old": "1.0.0", "new": "1.1.0"}]
    svc.send("ver", "continue", requester="sean")
    s = await settle(svc, "ver")
    assert s.turns[-1].mode == "resume", "the new conversation is the new version's: it resumes"


def test_a_sandbox_that_doesnt_know_its_version_changes_nothing():
    from hangar.models import Sandbox, Session, SessionTurn

    class Svc:
        pass

    s = Session(name="x", environment="e", harness="codex", harness_session="th-1", harness_session_for="codex",
                harness_version="0.162.1")
    t = SessionTurn(message="m")
    pins.before_turn(Svc(), s, Sandbox(name="b", environment="e", provider="docker", info={"harness": "codex"}), t)
    assert s.harness_session == "th-1" and not t.warnings
    old = Session(name="y", environment="e", harness="codex", harness_session="th-2", harness_session_for="codex")
    sb = Sandbox(name="b", environment="e", provider="docker", info={"harness": "codex", "harness_lock": "0.163.0"})
    pins.before_turn(Svc(), old, sb, t)
    assert old.harness_version == "0.163.0" and old.harness_session == "th-2", "a session from before pins: adopts"
