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
from hangar.modelpolicy import Resolver, parse, pick, rank_google
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
    ("antigravity", "frontier", "gemini-3.8-flash-high"),  # agy's id: no -preview, a thinking level
])
def test_each_provider_ranks_its_recorded_list(harness, policy, want):
    assert pick(harness, recorded(modelpolicy.PROVIDER[harness]), policy) == want
    assert rank_google(recorded("google"), policy) == "gemini-3.8-flash"  # the raw Gemini id under agy's


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
    assert rank_google(pro, "frontier") == "gemini-3.1-pro-preview", "a newer version beats an older stable one"
    assert pick("antigravity", pro, "frontier") == "gemini-3.1-pro-high"
    assert rank_google([{"id": "gemini-3.8-pro-preview"}, {"id": "gemini-3.8-flash"}], "frontier") == \
        "gemini-3.8-flash", "same version: the stable one"
    assert rank_google([{"id": "gemini-3.8-flash"}, {"id": "gemini-3.8-pro"}], "frontier") == "gemini-3.8-pro"


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
    assert envs["agents-frontier"]["models"]["claude-code"] == {
        "model": "claude-fable-6", "policy": "frontier", "source": "live",
        "about": "`claude-fable-6` (`frontier`; no known price, so budgets can't count its cost: HANGAR_MODEL_PRICES)"}
    assert envs["agents"]["models"]["codex"] == {
        "model": "sonnet", "source": "pinned",
        "about": "`sonnet` (no known price, so budgets can't count its cost: HANGAR_MODEL_PRICES)"}


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


async def test_a_job_session_defaults_to_balanced_and_says_where_the_id_came_from(svc, world, fleet):  # noqa: F811
    """--- least-privilege: the owner's decision, `balanced` is the job default (Opus on Claude Code)."""
    s = svc.for_job(job_id="job-12345678", environment="agent", harness="codex", model=None, repo=None,
                    base_branch=None, branch="b", provider=None, labels={}, extra_dirs=[], idle_minutes=5)
    assert (s.model, s.model_policy) == ("gpt-6.1-sol", "balanced"), "no list: the built-in balanced for codex"
    c = svc.for_job(job_id="job-87654321", environment="agent", harness="claude-code", model=None, repo=None,
                    base_branch=None, branch="c", provider=None, labels={}, extra_dirs=[], idle_minutes=5)
    assert (c.model, c.model_policy) == ("claude-opus-5-5", "balanced")
    f = svc.for_job(job_id="job-11223344", environment="agent", harness="claude-code", model="frontier", repo=None,
                    base_branch=None, branch="d", provider=None, labels={}, extra_dirs=[], idle_minutes=5)
    assert (f.model, f.model_policy) == ("claude-fable-5-1", "frontier"), "a job can still ask for frontier"
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


# ====================================================================== review #105
def test_a_newer_lite_model_is_never_frontier():
    """Version first would make Google's next flash-lite (its cheapest tier) the `frontier` pick over a stronger
    flash or pro; a lite model is frontier only when nothing else is listed."""
    models = recorded("google") + [{"id": "gemini-3.9-flash-lite"}]
    assert rank_google(models, "frontier") == "gemini-3.8-flash"
    assert pick("antigravity", models, "frontier") == "gemini-3.8-flash-high"
    assert rank_google([{"id": "gemini-3.5-flash-lite"}, {"id": "gemini-3.1-pro-preview"}], "frontier") == \
        "gemini-3.1-pro-preview"
    assert rank_google([{"id": "gemini-3.5-flash-lite"}], "frontier") == "gemini-3.5-flash-lite"
    ors = recorded("openrouter") + [{"id": "google/gemini-3.9-flash-lite"}]
    assert pick("pi", ors, "frontier", "google") == "openrouter/google/gemini-3.8-flash"


def test_every_built_in_policy_has_a_seed(fleet):
    """With no list yet, a policy must still name a model: a session that ran its first turns on the harness's own
    default would switch model when the list arrives (pins resolve once; run_agent re-resolves an unset model).
    Codex's frontier is gpt-6-astra: OpenAI's docs list it for the API and the Codex CLI with no access gate."""
    r = Resolver(fleet)
    assert r.resolve("codex", "frontier").model == "gpt-6-astra"
    assert r.resolve("codex", "balanced").model == "gpt-6.1-sol"
    for vendor in ("openai", "google", "anthropic"):
        got = r.resolve("pi", f"frontier@{vendor}")
        assert got.source == "seed", vendor
        assert got.model == pick("pi", recorded("openrouter"), "frontier", vendor), "a seed is a listed id"
        bal = r.resolve("pi", f"balanced@{vendor}")
        assert bal.model == pick("pi", recorded("openrouter"), "balanced", vendor), vendor


@pytest.mark.parametrize(("model", "priced_as"), [
    ("gemini-3.8-flash-high", "gemini-3.8-flash"),  # agy: the Gemini id plus a thinking level
    ("gemini-3.1-pro-low", "gemini-3.1-pro-preview"),
    ("openrouter/anthropic/claude-fable-5.1", "claude-fable-5-1"),  # pi on OpenRouter: dotted versions
    ("anthropic/claude-opus-5.5", "claude-opus-5-5"),
    ("claude-fable-5-1", "claude-fable-5-1"),
    ("openrouter/openai/gpt-6.1-sol", "gpt-6.1-sol"),
])
def test_what_the_policies_resolve_to_is_priced_so_budgets_can_stop_it(model, priced_as):
    from hangar.events import DEFAULT_PRICES, price_for

    assert price_for(model, DEFAULT_PRICES) == DEFAULT_PRICES[priced_as]


def test_every_seed_is_priced():
    from hangar.events import DEFAULT_PRICES, price_for

    for harness, seeds in modelpolicy.SEED.items():
        for policy, model in seeds.items():
            assert price_for(model, DEFAULT_PRICES), (harness, policy, model)


def test_a_model_is_described_with_its_policy_and_list_price():
    from hangar.events import DEFAULT_PRICES

    assert modelpolicy.describe("claude-fable-5-1", "frontier", DEFAULT_PRICES) == \
        "`claude-fable-5-1` (`frontier`; list price $10/$50 per MTok in/out)"
    assert modelpolicy.describe("claude-opus-5-5", None, DEFAULT_PRICES) == \
        "`claude-opus-5-5` (list price $4/$20 per MTok in/out)"
    assert modelpolicy.describe("my-model", None, DEFAULT_PRICES) == \
        "`my-model` (no known price, so budgets can't count its cost: HANGAR_MODEL_PRICES)"
    assert modelpolicy.describe(None, None, DEFAULT_PRICES) == "the harness default"


def test_list_environments_says_what_each_model_costs(fleet):
    v = Resolver(fleet).view("claude-code", "frontier")
    assert (v["model"], v["policy"], v["source"]) == ("claude-fable-5-1", "frontier", "seed")
    assert v["about"] == "`claude-fable-5-1` (`frontier`; list price $10/$50 per MTok in/out)"
    assert fleet.store.kv_get("modelpick:claude-code:frontier") is None, "a listing only looks"


def test_jobs_with_no_budget_and_no_daily_cap_get_a_doctor_warning(fleet):
    """--- integ3 (review #105): nothing caps jobs unless the owner sets a budget. Hangar sets no default cap (the
    owner's call); the doctor and `hangar status` say so instead. --- least-privilege: jobs default to `balanced`
    now, and the warning still fires: it is about the missing budget, and `frontier` is still a job away."""
    from types import SimpleNamespace

    from hangar.repopolicy import RepoPolicies

    h = SimpleNamespace(fleet=fleet)
    g = modelpolicy.spend_guard(fleet)
    assert g["uncapped"] and g["job_default"] == "balanced" and "claude-opus-5-5" in g["claude_code"]
    [c] = modelpolicy.doctor_checks(h)
    assert c["name"] == "model spend" and c["status"] == "warn"
    assert c["detail"].startswith("jobs have no budget: they default to `balanced` (Claude Code: `claude-opus-5-5`")
    assert "`frontier` where an environment, repo policy or job asks for it" in c["detail"]
    assert "HANGAR_JOB_BUDGET_MAX_COST_USD" in c["detail"] and "hangar sets none" in c["fix"]
    RepoPolicies(fleet.store).put(RepoPolicy(repo="acme/app", max_usd_per_day=20))
    assert modelpolicy.spend_guard(fleet)["repos_capped"] == ["acme/app"]
    assert modelpolicy.doctor_checks(h)[0]["status"] == "warn"  # one repo's cap doesn't bound the rest
    fleet.settings.person_max_usd_per_day = 50
    [c] = modelpolicy.doctor_checks(h)
    assert c["status"] == "ok" and "$50 a person a day" in c["detail"]
    fleet.settings.person_max_usd_per_day = None
    fleet.settings.job_budget_max_cost_usd = 5
    assert modelpolicy.doctor_checks(h)[0]["detail"].endswith("bounded at $5 a job")


# ====================================================================== concreteness (#167)
@pytest.mark.parametrize(("harness", "model", "want"), [
    # Anthropic: a line with major and minor, or a dated snapshot
    ("claude-code", "claude-opus-5-5", True), ("claude-code", "claude-fable-5-1", True),
    ("claude-code", "claude-sonnet-4-20250514", True), ("claude-code", "claude-3-5-sonnet-20241022", True),
    ("claude-code", "claude-opus-latest", False), ("claude-code", "opus", False), ("claude-code", "sonnet", False),
    ("claude-code", "haiku", False), ("claude-code", "claude-opus", False), ("claude-code", "fable", False),
    ("claude-code", "claude-opus-5", False),  # a major alone: not a known shape (it is concrete when listed)
    # OpenAI: gpt-<version>-<tier>, or a dated snapshot
    ("codex", "gpt-6-astra", True), ("codex", "gpt-6.1-sol", True), ("codex", "gpt-6-astra-2026-09-03", True),
    ("codex", "gpt-4o-2024-08-06", True), ("codex", "o3-2025-04-16", True),
    ("codex", "gpt-5", False), ("codex", "gpt-6.1", False), ("codex", "gpt-4o", False), ("codex", "o3", False),
    ("codex", "gpt-astra-latest", False), ("codex", "gpt-5-mini", False),
    # Google: gemini-<version>-<tier>, optionally a preview or revision; agy's thinking level
    ("gemini", "gemini-3.8-flash", True), ("gemini", "gemini-3.1-pro-preview", True),
    ("gemini", "gemini-2.5-flash-preview-05-20", True), ("gemini", "gemini-1.5-pro-002", True),
    ("antigravity", "gemini-3.8-flash-high", True),
    ("gemini", "gemini-flash-latest", False), ("gemini", "gemini-pro", False), ("gemini", "gemini-3", False),
    # OpenRouter (pi): the vendor's id with a dotted version; `~…-latest` aliases and the router itself never
    ("pi", "openrouter/anthropic/claude-opus-5.5", True), ("pi", "openrouter/openai/gpt-6-astra", True),
    ("pi", "openrouter/google/gemini-3.8-flash", True), ("pi", "anthropic/claude-fable-5.1", True),
    ("pi", "openrouter/~anthropic/claude-fable-latest", False), ("pi", "~openai/gpt-astra-latest", False),
    ("pi", "openrouter/auto", False), ("pi", "openrouter/anthropic/claude-opus", False),
    # neither listed nor of a known shape, a policy, nothing: never concrete
    ("claude-code", "my-finetune", False), ("claude-code", "balanced", False), ("codex", "frontier@openai", False),
    ("claude-code", None, False), ("claude-code", "", False),
])
def test_is_concrete_by_shape(harness, model, want):
    assert modelpolicy.is_concrete(harness, model) is want


def test_every_seed_is_concrete():
    for harness, policies in modelpolicy.SEED.items():
        for model in policies.values():
            assert modelpolicy.concreteness(harness, model) == (True, "pattern"), (harness, model)
    for model in modelpolicy.PI_SEED.values():
        assert modelpolicy.is_concrete("pi", model), model


def test_a_listed_id_is_concrete_but_a_listed_alias_never_is():
    """The provider's list decides for ids of no known shape (`claude-opus-5`, `gpt-5.3-codex`); an alias stays an
    alias even when a provider lists it (OpenRouter lists `~vendor/…-latest`; OpenAI lists `gpt-5`)."""
    ant, oai, ors = recorded("anthropic"), recorded("openai"), recorded("openrouter")
    assert modelpolicy.concreteness("claude-code", "claude-opus-5", ant) == (True, "list")
    assert modelpolicy.concreteness("codex", "gpt-5.3-codex", oai) == (True, "list")
    assert modelpolicy.concreteness("codex", "gpt-5.3-codex") == (False, "unknown")
    assert modelpolicy.concreteness("pi", "openrouter/anthropic/claude-opus-5", ors) == (True, "list")
    assert modelpolicy.concreteness("pi", "~anthropic/claude-fable-latest", ors) == (False, "alias")
    assert modelpolicy.concreteness("codex", "gpt-5", [{"id": "gpt-5"}]) == (False, "alias")
    assert modelpolicy.concreteness("antigravity", "gemini-3.1-pro-high", recorded("google")) == (True, "list")
    assert modelpolicy.concreteness("claude-code", "claude-opus-4-9", ant) == (True, "pattern")  # unlisted, versioned


def test_with_no_list_cached_the_shape_decides_and_says_so(fleet, caplog):
    r = Resolver(fleet)
    with caplog.at_level("WARNING", logger="hangar.modelpolicy"):
        ok, basis, note = r.concreteness("claude-code", "claude-opus-5-5")
    assert (ok, basis) == (True, "pattern") and "no anthropic model list is cached" in note
    assert "judged by its shape alone" in caplog.text
    ok, basis, note = r.concreteness("claude-code", "claude-opus-5")  # unknown without the list: never concrete
    assert (ok, basis) == (False, "unknown") and note
    cache(fleet, "anthropic")
    assert r.concreteness("claude-code", "claude-opus-5") == (True, "list", None)
    assert r.concreteness("claude-code", "opus")[:2] == (False, "alias")
    assert r.suggest("claude-code") == {"frontier": "claude-fable-5-1", "balanced": "claude-opus-5-5"}
