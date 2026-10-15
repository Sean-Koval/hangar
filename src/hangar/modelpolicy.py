"""Model policies: the latest strong model per provider, resolved to a concrete id (--- canary, harness P5).

A `model:` value (an environment's, a repo policy's, a job's, a session's) is either a pinned id, passed through as
it is, or a policy:

* `frontier`: the strongest current model the provider lists for this account (asked for per environment, repo
  policy or job);
* `balanced`: the provider's everyday line (Anthropic's opus, OpenAI's sol, Google's flash). The default for jobs,
  `envs/bare.yaml`, env setup and the Cloud Doctor;
* `frontier@openai` (or `balanced@google`, ...): the same, from one vendor, for a harness that reaches several (pi
  on OpenRouter; it picks Anthropic's otherwise).

Policies are resolved when a sandbox or a session is created, from each provider's live model list: Anthropic's
`/v1/models` (Claude Code), OpenAI's (Codex), Google's Gemini API list (Antigravity, and Vertex, which serves the same
ids), OpenRouter's (pi). The concrete id is pinned on the session, so a conversation never changes model when a list
does (spec 0012: each model has its own prompt cache). Lists are cached for HANGAR_MODEL_LIST_TTL_MINUTES and
refreshed in the background, so a create never waits on a provider; when a list can't be had, the last id resolved
for that harness and policy is used, else a built-in one, and the session says so.

How "strongest" is decided, per provider (docs/harnesses.md has the reasoning):
* Anthropic: the model's `line` field (fable and mythos > opus > sonnet > haiku; the docs say not to infer it from
  the id), then the newest (the list is newest first). Active models only.
* OpenAI: `gpt-<version>-<tier>`, tier first (astra > sol > terra > luna), then version.
* Google: never a lite model while another is listed, then the newest version, then stable before preview, then
  the tier (pro > flash > flash-lite). Gemini's flash line moves faster than pro, so the tier comes after the version.
  That Gemini 3.8 Flash (stable) is stronger than Gemini 3.1 Pro Preview is not something Google's model pages say
  (they call 3.8 Flash "our most intelligent Flash model"); it rests on third-party indexes. Pin a Gemini id where it
  matters.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from hangar.fleet import Fleet

log = logging.getLogger("hangar.modelpolicy")

POLICIES = ("frontier", "balanced")
VENDORS = ("anthropic", "openai", "google")
PROVIDER = {"claude-code": "anthropic", "codex": "openai", "pi": "openrouter", "antigravity": "google",
            "gemini": "google"}
LISTS = {
    "anthropic": "https://api.anthropic.com/v1/models",
    "openai": "https://api.openai.com/v1/models",
    "google": "https://generativelanguage.googleapis.com/v1beta/models",
    "openrouter": "https://openrouter.ai/api/v1/models",  # public: no key
}
# Used only when no list was ever fetched and nothing was resolved before (checked 2026-10-14 against each vendor's
# model docs and OpenRouter's list). --- review #105: Codex's frontier is gpt-6-astra, OpenAI's "most capable model",
# which its docs list for the API and the Codex CLI with no access gate (developers.openai.com/api/docs/models/
# gpt-6-astra, /codex/models); gpt-6.1-sol is what Codex's docs recommend for everyday work, so it is `balanced`.
SEED: dict[str, dict[str, str]] = {
    "claude-code": {"frontier": "claude-fable-5-1", "balanced": "claude-opus-5-5"},
    "codex": {"frontier": "gpt-6-astra", "balanced": "gpt-6.1-sol"},
    "pi": {"frontier": "openrouter/anthropic/claude-fable-5.1", "balanced": "openrouter/anthropic/claude-opus-5.5"},
    "antigravity": {"frontier": "gemini-3.8-flash-high", "balanced": "gemini-3.8-flash-high"},
    "gemini": {"frontier": "gemini-3.8-flash", "balanced": "gemini-3.8-flash"},
}
# --- review #105: pi with a vendor (`frontier@openai`) has one too. Without it the first turns ran on pi's own
# default and the session switched model once the list arrived (an unset model is resolved again on each run).
PI_SEED: dict[tuple[str, str], str] = {
    ("frontier", "anthropic"): "openrouter/anthropic/claude-fable-5.1",
    ("balanced", "anthropic"): "openrouter/anthropic/claude-opus-5.5",
    ("frontier", "openai"): "openrouter/openai/gpt-6-astra", ("balanced", "openai"): "openrouter/openai/gpt-6.1-sol",
    ("frontier", "google"): "openrouter/google/gemini-3.8-flash",
    ("balanced", "google"): "openrouter/google/gemini-3.8-flash",
}
ANTHROPIC_LINES = {"fable": 4, "mythos": 4, "opus": 3, "sonnet": 2, "haiku": 1}
OPENAI_TIERS = {"astra": 4, "sol": 3, "terra": 2, "luna": 1}
GOOGLE_TIERS = {"pro": 3, "flash": 2, "flash-lite": 1}
BALANCED = {"anthropic": 3, "openai": 3, "google": 2}  # the tier `balanced` picks from
_OPENAI = re.compile(r"gpt-(\d+(?:\.\d+)?)-(astra|sol|terra|luna)")
_GOOGLE = re.compile(r"gemini-(\d+(?:\.\d+)?)-(pro|flash|flash-lite)(-preview)?")
_CLAUDE = re.compile(r"claude-(fable|mythos|opus|sonnet|haiku)-(\d+(?:[.-]\d+)?)")  # OpenRouter's ids only


@dataclass
class Resolution:
    model: str | None  # what the harness is given (None: its own default)
    policy: str | None = None  # the policy it came from (None: pinned, or nothing asked for)
    source: str = "pinned"  # pinned | default | live | cached | last | seed
    note: str | None = None  # why it isn't from a fresh list, for the session's or sandbox's events

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v is not None}


def parse(value: str | None) -> tuple[str, str | None] | None:
    """(policy, vendor) when `value` names a policy, else None (a pinned id)."""
    if not value:
        return None
    policy, _, vendor = value.strip().lower().partition("@")
    if policy not in POLICIES or (vendor and vendor not in VENDORS):
        return None
    return policy, vendor or None


def is_policy(value: str | None) -> bool:
    return parse(value) is not None


def _version(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.split(r"[.-]", v) if x.isdigit())


# ====================================================================== ranking (pure: recorded lists in, an id out)
def _pick(cands: list[tuple[tuple, str]]) -> str | None:
    return max(cands)[1] if cands else None


def rank_anthropic(models: list[dict[str, Any]], policy: str) -> str | None:
    """`models` newest first, as /v1/models lists them."""
    cands = []
    for i, m in enumerate(models):
        line = ANTHROPIC_LINES.get(str(m.get("line") or ""), 0)
        if not line or (m.get("lifecycle") or "active") != "active":
            continue
        if policy == "balanced" and line != BALANCED["anthropic"]:
            continue
        cands.append(((line, -i, str(m.get("id"))), str(m.get("id"))))
    return _pick(cands)


def rank_openai(models: list[dict[str, Any]], policy: str, strip: str = "") -> str | None:
    cands = []
    for m in models:
        mid = str(m.get("id") or "")
        if not (g := _OPENAI.fullmatch(mid.removeprefix(strip))):
            continue
        tier = OPENAI_TIERS[g.group(2)]
        if policy == "balanced" and tier != BALANCED["openai"]:
            continue
        cands.append(((tier, _version(g.group(1)), int(m.get("created") or 0)), mid))
    return _pick(cands)


def rank_google(models: list[dict[str, Any]], policy: str, strip: str = "") -> str | None:
    cands = []
    for m in models:
        mid = str(m.get("id") or "")
        if not (g := _GOOGLE.fullmatch(mid.removeprefix(strip))):
            continue
        tier = GOOGLE_TIERS[g.group(2)]
        if policy == "balanced" and tier != BALANCED["google"]:
            continue
        # --- review #105: a lite model only when nothing stronger is listed (a newer flash-lite isn't frontier)
        cands.append(((tier > GOOGLE_TIERS["flash-lite"], _version(g.group(1)), not g.group(3), tier,
                       int(m.get("created") or 0)), mid))
    return _pick(cands)


def rank_openrouter(models: list[dict[str, Any]], policy: str, vendor: str | None) -> str | None:
    """OpenRouter lists every vendor (`vendor/model`, plus `:batch` and `~…-latest` aliases, which are skipped).
    Without a vendor: Anthropic's, else OpenAI's, else Google's."""
    for v in ([vendor] if vendor else list(VENDORS)):
        mine = [m for m in models if str(m.get("id") or "").startswith(f"{v}/") and ":" not in str(m.get("id"))]
        if v == "anthropic":
            cands = []
            for m in mine:
                if (g := _CLAUDE.fullmatch(str(m["id"]).removeprefix("anthropic/"))) is None:
                    continue
                line = ANTHROPIC_LINES[g.group(1)]
                if policy == "balanced" and line != BALANCED["anthropic"]:
                    continue
                cands.append(((line, _version(g.group(2)), int(m.get("created") or 0)), str(m["id"])))
            got = _pick(cands)
        elif v == "openai":
            got = rank_openai(mine, policy, strip="openai/")
        else:
            got = rank_google(mine, policy, strip="google/")
        if got:
            return got
    return None


def pick(harness: str, models: list[dict[str, Any]], policy: str, vendor: str | None = None) -> str | None:
    """The concrete id `harness` takes for `policy`, from its provider's list (None: nothing in it qualifies)."""
    provider = PROVIDER.get(harness)
    if provider == "anthropic":
        return rank_anthropic(models, policy)
    if provider == "openai":
        return rank_openai(models, policy)
    if provider == "openrouter":
        got = rank_openrouter(models, policy, vendor)
        return f"openrouter/{got}" if got else None
    if provider == "google":
        got = rank_google(models, policy)
        if got and harness == "antigravity":
            # agy names a model by its Gemini id without "-preview", plus a thinking level ("gemini-3.8-flash-high",
            # as `agy models` lists them); hangar runs it at high
            return got.removesuffix("-preview") + "-high"
        return got
    return None


# ====================================================================== the lists
def _normalize(provider: str, doc: dict[str, Any]) -> list[dict[str, Any]]:
    """Only what ranking needs: a provider's list, in its own order."""
    out: list[dict[str, Any]] = []
    if provider == "google":
        for m in doc.get("models") or []:
            if "generateContent" not in (m.get("supportedGenerationMethods") or ["generateContent"]):
                continue
            out.append({"id": str(m.get("name") or "").removeprefix("models/")})
        return out
    for m in doc.get("data") or []:
        if not isinstance(m, dict) or not m.get("id"):
            continue
        e: dict[str, Any] = {"id": str(m["id"])}
        if provider == "anthropic":
            e.update(line=m.get("line"), lifecycle=m.get("lifecycle") or "active", created=m.get("created_at"))
        elif isinstance(m.get("created"), int | float):
            e["created"] = int(m["created"])
        if provider == "openrouter":
            out_mod = (m.get("architecture") or {}).get("output_modalities") or ["text"]
            if out_mod != ["text"]:
                continue
        out.append(e)
    return out


async def fetch(provider: str, key: str | None, client: httpx.AsyncClient) -> list[dict[str, Any]]:
    """One provider's whole model list (every page)."""
    url, headers, params = LISTS[provider], {}, {}
    if provider == "anthropic":
        headers = {"x-api-key": key or "", "anthropic-version": "2023-06-01"}
        params = {"limit": 1000}
    elif provider == "openai":
        headers = {"authorization": f"Bearer {key}"}
    elif provider == "google":
        headers = {"x-goog-api-key": key or ""}
        params = {"pageSize": 1000}
    models: list[dict[str, Any]] = []
    for _ in range(20):
        r = await client.get(url, headers=headers, params=params)
        r.raise_for_status()
        doc = r.json()
        models += _normalize(provider, doc)
        if provider == "anthropic" and doc.get("has_more") and doc.get("last_id"):
            params = {**params, "after_id": doc["last_id"]}
        elif provider == "google" and doc.get("nextPageToken"):
            params = {**params, "pageToken": doc["nextPageToken"]}
        else:
            return models
    return models


# ====================================================================== the resolver
class Resolver:
    """Per control plane (`of(fleet)`). Resolution is synchronous and reads only the cache, so creating a sandbox or
    a session never waits on a provider; the cache is refreshed in the background."""

    def __init__(self, fleet: Fleet):
        self.fleet = fleet
        self._refreshing: dict[str, asyncio.Task] = {}
        self._task: asyncio.Task | None = None

    @property
    def ttl(self) -> float:
        return max(5, self.fleet.settings.model_list_ttl_minutes) * 60

    def _key(self, provider: str) -> str | None:
        if provider == "openrouter":
            return None
        name = self.fleet.settings.model_list_credentials.get(provider)
        if not name:
            raise LookupError(f"no {provider} key to list models with (HANGAR_MODEL_LIST_CREDENTIALS)")
        return self.fleet.broker.secret_text(name)

    def cached(self, provider: str) -> dict[str, Any] | None:
        with contextlib.suppress(ValueError, TypeError):
            got = json.loads(self.fleet.store.kv_get(f"modellist:{provider}") or "null")
            return got if isinstance(got, dict) and isinstance(got.get("models"), list) else None
        return None

    async def refresh(self, provider: str) -> list[dict[str, Any]]:
        """Fetch one provider's list now, and cache it (or the error, without the key)."""
        try:
            key = self._key(provider)
            async with httpx.AsyncClient(timeout=20) as client:
                models = await fetch(provider, key, client)
        except Exception as e:  # noqa: BLE001 - recorded, and the last good list stays in use
            msg = str(e) if isinstance(e, LookupError) else f"{type(e).__name__}: {e}"
            if isinstance(e, httpx.HTTPStatusError):
                msg = f"HTTP {e.response.status_code} from {LISTS[provider]}"
            self.fleet.store.kv_set(f"modellist-error:{provider}", json.dumps({"at": time.time(), "error": msg[:300]}))
            log.warning("could not list %s models: %s", provider, msg[:300])
            raise
        self.fleet.store.kv_set(f"modellist:{provider}", json.dumps({"at": time.time(), "models": models}))
        self.fleet.store.kv_delete(f"modellist-error:{provider}")
        return models

    def _listable(self, provider: str) -> bool:
        return provider == "openrouter" or bool(self.fleet.settings.model_list_credentials.get(provider))

    def _kick(self, provider: str) -> None:
        """Refresh in the background, once at a time per provider (no-op outside an event loop, or without a key)."""
        if not self._listable(provider) or ((t := self._refreshing.get(provider)) and not t.done()):
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return

        async def run() -> None:
            with contextlib.suppress(Exception):
                await self.refresh(provider)

        self._refreshing[provider] = loop.create_task(run())

    def in_use(self) -> set[str]:
        """The providers whose lists an environment's policy needs (repo policies and jobs ask lazily)."""
        out: set[str] = set()
        catalog = self.fleet.catalog
        for name in catalog.names():
            with contextlib.suppress(Exception):
                spec = catalog.get(name)
                out |= {PROVIDER[h] for h in spec.agent_harnesses if h in PROVIDER and is_policy(spec.model_for(h))}
        return out

    def start(self) -> None:
        """Keep the lists the environments' policies need fresh while hangar runs (fetched now, at startup); others
        are fetched on first use."""
        async def loop() -> None:
            while True:
                for p in self.in_use() | {p for p in LISTS if self.cached(p) is not None}:
                    self._kick(p)
                await asyncio.sleep(self.ttl)

        self._task = asyncio.create_task(loop())

    async def stop(self) -> None:
        tasks = [t for t in (self._task, *self._refreshing.values()) if t]
        for t in tasks:
            t.cancel()
        for t in tasks:
            with contextlib.suppress(BaseException):
                await t
        self._task, self._refreshing = None, {}

    def resolve(self, harness: str | None, value: str | None, record: bool = True) -> Resolution:
        """`value` for `harness` as a concrete id. A pinned id is returned as it is; a policy is resolved from the
        cached list (a stale one is used, and refreshed), else the last id resolved for it, else the built-in one.
        `record`: remember the id as the last one resolved, and refresh a missing or stale list (listings only
        look)."""
        if not value:
            return Resolution(None, source="default")
        parsed = parse(value)
        if parsed is None or not harness:
            return Resolution(value)
        policy, vendor = parsed
        name = f"{policy}@{vendor}" if vendor else policy
        provider = PROVIDER.get(harness)
        if provider is None:
            return Resolution(None, name, "default", f"{harness} has no model list; it runs its own default")
        got, age = self.cached(provider), None
        if (got is None or (age := time.time() - float(got.get("at") or 0)) >= self.ttl) and record:
            self._kick(provider)
        model = pick(harness, got["models"], policy, vendor) if got else None
        if model:
            if record:
                self.fleet.store.kv_set(f"modelpick:{harness}:{name}", json.dumps({"id": model, "at": time.time()}))
            if age is not None and age >= self.ttl:
                old = f"{int(age // 3600)} h" if age >= 7200 else f"{int(age // 60)} min"
                return Resolution(model, name, "cached", f"{provider}'s model list is {old} old (the refresh failed "
                                                         "or is running); resolved from it")
            return Resolution(model, name, "live")
        why = self._why(provider, got, name)
        with contextlib.suppress(ValueError, TypeError):
            last = json.loads(self.fleet.store.kv_get(f"modelpick:{harness}:{name}") or "null")
            if isinstance(last, dict) and last.get("id"):
                day = time.strftime("%Y-%m-%d", time.gmtime(float(last.get("at") or 0)))
                return Resolution(str(last["id"]), name, "last", f"{why}; using the last id resolved for {harness} "
                                                                f"{name}, {last['id']} ({day})")
        seed = SEED.get(harness, {}).get(policy)
        if vendor and harness == "pi":
            seed = PI_SEED.get((policy, vendor))  # --- review #105
        return Resolution(seed, name, "seed", f"{why}; using hangar's built-in {name} for {harness}"
                          + (f", {seed} (as of 2026-10-14)" if seed else ": none, so its own default"))

    def _why(self, provider: str, got: dict[str, Any] | None, name: str) -> str:
        if got is not None:
            return f"nothing in {provider}'s model list qualifies as {name}"
        if not self._listable(provider):
            return f"no {provider} key to list its models with (HANGAR_MODEL_LIST_CREDENTIALS)"
        with contextlib.suppress(ValueError, TypeError):
            err = json.loads(self.fleet.store.kv_get(f"modellist-error:{provider}") or "null")
            if isinstance(err, dict) and err.get("error"):
                return f"couldn't list {provider}'s models ({err['error']})"
        return f"{provider}'s model list hasn't been fetched yet"

    def concrete(self, harness: str | None, value: str | None) -> str | None:
        return self.resolve(harness, value).model

    def view(self, harness: str | None, value: str | None) -> dict[str, Any]:
        """--- review #105: what `value` resolves to now, for list_environments and `hangar env list`, with its
        price (a listing only looks: nothing is recorded or refreshed)."""
        from hangar.events import DEFAULT_PRICES

        r = self.resolve(harness, value, record=False)
        prices = {**DEFAULT_PRICES, **{k.lower(): v for k, v in (self.fleet.settings.model_prices or {}).items()}}
        return {**r.to_dict(), "about": describe(r.model, r.policy, prices)}


def describe(model: str | None, policy: str | None, prices: dict[str, dict[str, float]]) -> str:
    """--- review #105: a model as the review packet and `hangar env list` show it: the id, the policy it came from,
    and its list price, since `frontier` can cost several times `balanced` (Fable 5.1 is $10/$50 per MTok, Opus 5.5
    $4/$20)."""
    from hangar.events import price_for

    if not model:
        return "the harness default"
    p = price_for(model, prices)
    price = (f"list price ${p.get('input', 0):g}/${p.get('output', 0):g} per MTok in/out" if p else
             "no known price, so budgets can't count its cost: HANGAR_MODEL_PRICES")
    return f"`{model}` ({f'`{policy}`; ' if policy else ''}{price})"


# --- integ3 (review #105): jobs are uncapped unless the owner sets a budget. Hangar sets no default cap, because what
# the team may spend is the owner's call; it says so in `hangar doctor` and `hangar status` instead.
def spend_guard(fleet: Fleet) -> dict[str, Any]:
    """Whether jobs have a bound on what they spend: a default job budget (HANGAR_JOB_BUDGET_MAX_COST_USD) or a daily
    cap per person (HANGAR_PERSON_MAX_USD_PER_DAY). Repo policies with their own `job_budget` or `max_usd_per_day` are
    listed, but each covers only its repo.

    --- least-privilege: jobs default to `balanced` now, but an environment, repo policy or job can still ask for
    `frontier` (several times the price), and any model spends without bound with neither cap set. So the warning is
    about the missing cap, whatever the default."""
    from hangar import pins
    from hangar.repopolicy import RepoPolicies

    s = fleet.settings
    capped = sorted(p.repo for p in RepoPolicies(fleet.store).list()
                    if p.max_usd_per_day or (p.job_budget and p.job_budget.max_cost_usd))
    job, day = s.job_budget_max_cost_usd or None, s.person_max_usd_per_day or None
    default = pins.JOB_DEFAULT
    uncapped = not job and not day
    return {"job_default": default, "claude_code": of(fleet).view("claude-code", default)["about"],
            "job_budget_usd": job, "person_max_usd_per_day": day, "repos_capped": capped, "uncapped": uncapped}


def doctor_checks(h: Any) -> list[dict[str, Any]]:
    """--- integ3 (review #105): a warning while jobs have no job budget and no daily cap."""
    from hangar.doctor import _check

    g = spend_guard(h.fleet)
    if g["uncapped"]:
        repos = f"; {len(g['repos_capped'])} repo policies set their own" if g["repos_capped"] else ""
        return [_check("model spend", None, f"jobs have no budget: they default to `{g['job_default']}` (Claude Code: "
                       f"{g['claude_code']}), `frontier` where an environment, repo policy or job asks for it, and "
                       "neither HANGAR_JOB_BUDGET_MAX_COST_USD nor HANGAR_PERSON_MAX_USD_PER_DAY is set, so a job's "
                       f"spend is bounded only by the budget it brings{repos}",
                       "pick the amounts (hangar sets none): hangar config set HANGAR_JOB_BUDGET_MAX_COST_USD <usd>, "
                       "and/or HANGAR_PERSON_MAX_USD_PER_DAY <usd> (docs/upgrading.md, docs/harnesses.md)")]
    bounds = [b for b in (f"${g['job_budget_usd']:g} a job" if g["job_budget_usd"] else "",
                          f"${g['person_max_usd_per_day']:g} a person a day" if g["person_max_usd_per_day"] else "")
              if b]
    return [_check("model spend", True, f"jobs default to `{g['job_default']}`, bounded at {' and '.join(bounds)}")]


def of(fleet: Fleet) -> Resolver:
    """The fleet's resolver (made on first use, so Fleet itself needs no change)."""
    r = fleet.__dict__.get("_models")
    if r is None:
        r = fleet.__dict__["_models"] = Resolver(fleet)
    return r
