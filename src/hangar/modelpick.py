"""The strongest current model for a harness, from its provider's own model list (--- autoinstall).

Agent-led environment setup (autoinstall.py) runs both of its agents on a strong current model rather than the
environment's everyday default, and never on a model id written into hangar: ids go stale within months. So it asks
the provider which models exist now:

* **Which list.** The harness's provider: Claude Code -> Anthropic (`GET /v1/models`), Codex -> OpenAI
  (`GET /v1/models`), Antigravity and Gemini CLI -> Google (`GET /v1beta/models`), pi -> OpenRouter
  (`GET /api/v1/models`, public). The request uses the team's API key for that harness (a team credential, never a
  person's own), read on the control plane and never logged. Without one, OpenRouter's public list stands in for
  the provider's (its ids are the provider's, prefixed).
* **Which model.** General-purpose models only (no mini, nano, lite, haiku, flash, embedding, audio, image or other
  special-purpose ids; Anthropic's retired and deprecated ones are left out), of the newest generation (released
  within 180 days of the newest, or the newest major version when the list has no dates). Among those, the most
  capable by list price (HANGAR_MODEL_PRICES and events.DEFAULT_PRICES; a model not priced yet takes its line's
  price, so a new release isn't passed over), then the newest.
* **Pinning.** HANGAR_ENV_SETUP_MODELS (`{"claude-code": "...", "codex": "..."}`) pins a harness's model when an
  admin disagrees with the pick, and a job's own `model` always wins. When no list can be read, the harness's own
  default stands (the CLI's current recommendation), and the job's events say so.

Lists are cached for HANGAR_ENV_SETUP_MODEL_CACHE_HOURS (6) per provider and key.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
from datetime import datetime
from typing import TYPE_CHECKING, Any, NamedTuple

import httpx

from hangar.models import Credential, CredentialKind

if TYPE_CHECKING:
    from hangar.fleet import Fleet

log = logging.getLogger("hangar.modelpick")

# harness -> the provider whose list it picks from (pi: the provider its API key's env var names)
PROVIDERS: dict[str, str] = {"claude-code": "anthropic", "codex": "openai", "antigravity": "google",
                             "gemini": "google", "pi": "openrouter"}
ENDPOINTS = {
    "anthropic": "https://api.anthropic.com/v1/models?limit=1000",
    "openai": "https://api.openai.com/v1/models",
    "google": "https://generativelanguage.googleapis.com/v1beta/models?pageSize=1000",
    "openrouter": "https://openrouter.ai/api/v1/models",
}
FAMILY = {"anthropic": "claude-", "openai": "gpt-", "google": "gemini-"}
# Ids with one of these words are small, cheap or special-purpose models, not the strongest general one.
_NOT_GENERAL = frozenset(
    "mini nano lite luna haiku flash embed embedding audio realtime tts transcribe whisper image imagen dall veo "
    "search moderation instruct cyber computer vision tuning aqa gemma learnlm robotics oss chat exp live".split())


def _general(mid: str) -> bool:
    return not _NOT_GENERAL & set(re.split(r"[-./_:]", mid.lower()))
RECENT_DAYS = 180
TIMEOUT = 10.0


class Model(NamedTuple):
    id: str
    created: float | None  # epoch seconds, when the list gives a release date
    line: str | None = None  # Anthropic's `line` (opus, sonnet...): the model's family, as the API says it


class Pick(NamedTuple):
    model: str | None  # None: the harness's own default
    source: str  # in plain words, for the job's events and the review packet


_cache: dict[str, tuple[float, list[Model]]] = {}
_locks: dict[str, asyncio.Lock] = {}


def _version(mid: str) -> tuple[int, ...]:
    """claude-opus-5-5 -> (5, 5); gpt-5.6-sol -> (5, 6); gemini-3.1-pro-preview -> (3, 1). Dates aren't versions."""
    return tuple(int(x) for x in re.findall(r"(?<!\d)(\d{1,3})(?!\d)", mid.rsplit("/", 1)[-1])[:3])


def _line(m: Model) -> str:
    """The model's line: the API's, else its id without numbers and dates (claude-opus-5-5 -> claude-opus)."""
    if m.line:
        return m.line
    base = re.sub(r"[-.]?\d+", "", m.id.rsplit("/", 1)[-1].lower())
    return re.sub(r"-+", "-", base).strip("-")


def _price(mid: str, table: dict[str, dict[str, float]]) -> float | None:
    from hangar.events import price_for

    p = price_for(mid, table)
    return p.get("output") if p else None


def rank(models: list[Model], provider: str, table: dict[str, dict[str, float]]) -> str | None:
    """The strongest general model of the newest generation in a provider's list (see the module docstring)."""
    family = FAMILY.get(provider, "")
    cands = [m for m in models if m.id.rsplit("/", 1)[-1].startswith(family) and _general(m.id)]
    if not cands:
        return None
    dated = [m.created for m in cands if m.created]
    if dated:
        newest = max(dated)
        cands = [m for m in cands if (m.created or 0) >= newest - RECENT_DAYS * 86400]
    else:
        top = max(_version(m.id)[:1] for m in cands)
        cands = [m for m in cands if _version(m.id)[:1] == top]
    by_line: dict[str, float] = {}
    for m in cands:  # what each line costs at its newest priced model
        if (p := _price(m.id, table)) is not None:
            by_line[_line(m)] = max(by_line.get(_line(m), 0.0), p)
    for mid in table:  # a line priced only under its older ids still counts (a release newer than the table)
        p = _price(mid, table)
        if p is not None:
            by_line.setdefault(_line(Model(mid, None)), p)

    def key(m: Model) -> tuple:
        p = _price(m.id, table)
        return (p if p is not None else by_line.get(_line(m), -1.0), m.created or 0.0, _version(m.id), m.id)

    return max(cands, key=key).id


def _parse(provider: str, body: Any) -> list[Model]:
    out: list[Model] = []
    if provider == "google":
        for m in (body or {}).get("models") or []:
            name = str(m.get("baseModelId") or str(m.get("name") or "").removeprefix("models/"))
            methods = m.get("supportedGenerationMethods") or []
            if name and (not methods or "generateContent" in methods):
                out.append(Model(name, None))
        return out
    for m in (body or {}).get("data") or []:
        mid = str(m.get("id") or "")
        if not mid or (provider == "anthropic" and m.get("lifecycle") not in (None, "active")):
            continue
        created: float | None = None
        if isinstance(m.get("created"), (int, float)):
            created = float(m["created"])
        elif isinstance(m.get("created_at"), str):
            try:
                created = datetime.fromisoformat(m["created_at"].replace("Z", "+00:00")).timestamp()
            except ValueError:
                created = None
        if created is not None and created < 86400:  # "an epoch value if the release date is unknown"
            created = None
        out.append(Model(mid, created, m.get("line") if isinstance(m.get("line"), str) else None))
    return out


async def fetch(provider: str, key: str | None, transport: httpx.AsyncBaseTransport | None = None,
                ttl_hours: float = 6.0) -> list[Model]:
    """A provider's model list (cached per provider and key). Raises httpx errors when it can't be read."""
    ck = f"{provider}:{hashlib.sha256((key or '').encode()).hexdigest()[:16]}"
    hit = _cache.get(ck)
    if hit and time.monotonic() - hit[0] < ttl_hours * 3600:
        return hit[1]
    lock = _locks.setdefault(ck, asyncio.Lock())
    async with lock:
        hit = _cache.get(ck)
        if hit and time.monotonic() - hit[0] < ttl_hours * 3600:
            return hit[1]
        headers = {"User-Agent": "hangar"}
        if provider == "anthropic" and key:
            headers.update({"x-api-key": key, "anthropic-version": "2023-06-01"})
        elif provider == "google" and key:
            headers["x-goog-api-key"] = key
        elif key:
            headers["Authorization"] = f"Bearer {key}"
        async with httpx.AsyncClient(transport=transport, timeout=TIMEOUT, follow_redirects=False) as c:
            r = await c.get(ENDPOINTS[provider], headers=headers)
            r.raise_for_status()
            models = _parse(provider, r.json())
        _cache[ck] = (time.monotonic(), models)
        return models


def _team_key(fleet: Fleet, harness: str) -> tuple[str | None, str]:
    """The team's API key for the harness (the provider it reaches, from its env var for pi), or None."""
    provider = PROVIDERS.get(harness, "")
    for c in fleet.store.list(Credential):
        if c.harness != harness or c.kind != CredentialKind.API_KEY or c.person is not None:
            continue
        if harness == "pi":  # pi takes any provider's key; its env var says which (OPENROUTER_API_KEY...)
            provider = {"ANTHROPIC_API_KEY": "anthropic", "OPENAI_API_KEY": "openai",
                        "GEMINI_API_KEY": "google"}.get(c.env_var or "", "openrouter")
        try:
            return fleet.broker.secret_text(c.name), provider
        except Exception:  # noqa: BLE001 - an unreadable credential is no key; never say more
            continue
    return None, provider


async def pick(fleet: Fleet, harness: str | None, transport: httpx.AsyncBaseTransport | None = None) -> Pick:
    """The model agent-led setup runs `harness` on. Never raises: no list means the harness's own default."""
    s = fleet.settings
    if not harness:
        return Pick(None, "the environment's default harness and model")
    if pinned := (s.env_setup_models or {}).get(harness):
        return Pick(pinned, "pinned by HANGAR_ENV_SETUP_MODELS")
    if harness not in PROVIDERS:
        return Pick(None, f"{harness}'s own default (hangar can't list its models)")
    key, provider = _team_key(fleet, harness)
    from hangar.events import DEFAULT_PRICES

    table = {**DEFAULT_PRICES, **{k.lower(): v for k, v in (s.model_prices or {}).items()}}
    try:
        if key:
            models = await fetch(provider, key, transport, s.env_setup_model_cache_hours)
            best = rank(models, provider, table)
            src = f"the strongest current model in {provider}'s model list"
        else:
            models = await fetch("openrouter", None, transport, s.env_setup_model_cache_hours)
            want = {"anthropic": "anthropic/", "openai": "openai/", "google": "google/"}.get(provider)
            if provider == "openrouter":
                best = rank([m for m in models if m.id.startswith(("anthropic/", "openai/", "google/"))],
                            "openrouter", table)
                best = best and f"openrouter/{best}"
            else:
                mine = [Model(m.id.removeprefix(want or ""), m.created) for m in models
                        if want and m.id.startswith(want)]
                best = rank(mine, provider, table)
                if best and provider == "anthropic":  # OpenRouter writes claude-opus-5.5; the API, claude-opus-5-5
                    best = best.replace(".", "-")
            src = f"the strongest current {provider} model in OpenRouter's public list (no team API key to ask " \
                  f"{provider} directly)"
    except (httpx.HTTPError, ValueError) as e:
        log.info("could not list %s models for %s: %s", provider, harness, type(e).__name__)
        return Pick(None, f"{harness}'s own default ({provider}'s model list couldn't be read: {type(e).__name__})")
    if not best:
        return Pick(None, f"{harness}'s own default (no general model in {provider}'s list)")
    return Pick(best, src)
