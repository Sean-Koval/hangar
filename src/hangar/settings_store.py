"""Settings kept in the control plane's database, so they survive redeploys and change without kubectl.

`hangar config set` (and the `hangar setup` wizards) write here. Precedence, highest first:

1. the process environment and `.env` (on GKE, the `hangar` and `hangar-site` config maps become environment
   variables), and explicit `Settings(...)` arguments;
2. this store;
3. the defaults in `config.py`.

So a value pinned by a deployment can't be changed from the API; `hangar config list` shows where each value comes
from. Secrets are refused: they belong in the vault, and settings name vault credentials instead.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from pydantic import TypeAdapter, ValidationError

from hangar.config import Settings
from hangar.store import Store

log = logging.getLogger("hangar.settings")

KV_PREFIX = "setting:"

# Never stored here, with the reason given to whoever tries.
REFUSED: dict[str, str] = {
    "admin_token": "it is a secret: keep it in the hangar Secret or the environment",
    "secret_key": "it is the vault key: keep it in the hangar Secret or the environment",
    "e2b_api_key": "it is a secret: set HANGAR_E2B_API_KEY in the environment (hangar Secret)",
    "e2b_aws_access_key_id": "it is a credential: set it in the environment (hangar Secret)",
    "e2b_aws_secret_access_key": "it is a secret: set it in the environment (hangar Secret)",
    "e2b_registry_password": "it is a secret: set it in the environment (hangar Secret)",
    "e2b_gcp_service_account_json": "it is a key: set it in the environment (hangar Secret)",
    "data_dir": "it says where this database lives: set HANGAR_DATA_DIR in the environment",
    "host": "it is read before the server starts: use `hangar serve --host` or HANGAR_HOST",
    "port": "it is read before the server starts: use `hangar serve --port` or HANGAR_PORT",
}

# Read once at startup (providers, listeners, OAuth, worker pools): a change applies after the next restart.
_RESTART = ("public_url", "oauth_", "env_dirs", "image_prefix", "registry", "extra_ca_bundle", "default_provider",
            "enabled_providers", "docker_", "k8s_", "e2b_", "gateway_", "a2a_", "job_concurrency", "session_store",
            "prebuild_environments", "github_api_url", "sandbox_user", "events_max_mb",
            "mcp_stateless_http",  # integrate: #52's MCP transport mode is chosen when the app is built
            "io_threads")  # integrate: #53's I/O pool and the Docker connection pool are sized at startup


class SettingError(ValueError):
    pass


def field_name(key: str) -> str:
    """`HANGAR_GITHUB_REPOS`, `github-repos` and `github_repos` all name the field `github_repos`."""
    k = key.strip()
    if k.upper().startswith("HANGAR_"):
        k = k[7:]
    return k.lower().replace("-", "_")


def env_var(name: str) -> str:
    return f"HANGAR_{name.upper()}"


def needs_restart(name: str) -> bool:
    return any(name == p or (p.endswith("_") and name.startswith(p)) for p in _RESTART)


def _adapter(name: str) -> TypeAdapter:
    return TypeAdapter(Settings.model_fields[name].annotation)


def parse(name: str, raw: Any) -> Any:
    """Validate a value for a field. Strings are read the way the environment is: JSON first (lists, maps, numbers,
    booleans), then as a plain string."""
    ad = _adapter(name)
    if isinstance(raw, str):
        try:
            return ad.validate_json(raw)
        except ValidationError:
            pass
    try:
        return ad.validate_python(raw)
    except ValidationError as e:
        msg = "; ".join(x["msg"] for x in e.errors()[:3])
        raise SettingError(f"invalid value for {env_var(name)}: {msg}") from e


def _jsonable(name: str, value: Any) -> Any:
    return _adapter(name).dump_python(value, mode="json")


def _default(name: str) -> Any:
    return Settings.model_fields[name].get_default(call_default_factory=True)


class SettingsStore:
    """Overlay of database-stored settings on the live `Settings` object (shared by every component)."""

    def __init__(self, store: Store, settings: Settings):
        self.store = store
        self.settings = settings
        # Everything the environment, .env or the constructor set: those win over stored values.
        self.pinned: set[str] = set(settings.model_fields_set)

    def check(self, key: str) -> str:
        name = field_name(key)
        if not re.fullmatch(r"[a-z][a-z0-9_]*", name) or name not in Settings.model_fields:
            raise SettingError(f"unknown setting '{key}' (see `hangar config list --all`)")
        if name in REFUSED:
            raise SettingError(f"{env_var(name)} can't be stored by hangar: {REFUSED[name]}")
        return name

    def stored(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for k in self.store.kv_keys(KV_PREFIX):
            name = k[len(KV_PREFIX):]
            try:
                out[name] = json.loads(self.store.kv_get(k) or "null")
            except ValueError:
                log.warning("ignoring unreadable stored setting %s", env_var(name))
        return out

    def apply(self) -> None:
        """At startup: put stored values into the settings object, unless the environment pinned them."""
        for name, value in self.stored().items():
            if name not in Settings.model_fields or name in REFUSED or name in self.pinned:
                continue
            try:
                setattr(self.settings, name, parse(name, value))
            except SettingError as e:
                log.warning("ignoring stored setting: %s", e)

    def describe(self, name: str) -> dict[str, Any]:
        stored = self.stored()
        source = "environment" if name in self.pinned else "hangar config" if name in stored else "default"
        return {"key": env_var(name), "value": _jsonable(name, getattr(self.settings, name)), "source": source,
                "stored": stored.get(name), "restart": needs_restart(name)}

    def list(self, everything: bool = False) -> list[dict[str, Any]]:
        out = []
        for name in Settings.model_fields:
            if name in REFUSED:
                continue
            d = self.describe(name)
            if everything or d["source"] != "default":
                out.append(d)
        return out

    def set(self, key: str, raw: Any) -> dict[str, Any]:
        name = self.check(key)
        value = parse(name, raw)
        self.store.kv_set(KV_PREFIX + name, json.dumps(_jsonable(name, value)), durable=True)
        if name not in self.pinned:
            setattr(self.settings, name, value)
        return self.describe(name)

    def unset(self, key: str) -> dict[str, Any]:
        name = self.check(key)
        self.store.kv_delete(KV_PREFIX + name, durable=True)
        if name not in self.pinned:
            setattr(self.settings, name, _default(name))
        return self.describe(name)

    def merge_map(self, key: str, items: dict[str, Any]) -> dict[str, Any]:
        """Add entries to a map setting (repos, channels), keeping the ones already there."""
        name = self.check(key)
        current = getattr(self.settings, name) or {}
        if not isinstance(current, dict):
            raise SettingError(f"{env_var(name)} is not a map")
        return self.set(name, {**current, **items})


def mount(app, h, admin) -> None:
    """REST routes: GET /api/settings, GET/PUT/DELETE /api/settings/{key}. Admin tokens only."""
    from fastapi import Body, HTTPException

    cfg: SettingsStore = h.config

    def guard(fn, *a):
        try:
            return fn(*a)
        except SettingError as e:
            raise HTTPException(400, str(e)) from e

    @app.get("/api/settings")
    async def settings_list(all: bool = False, _: str = admin):
        return cfg.list(everything=all)

    @app.get("/api/settings/{key}")
    async def settings_get(key: str, _: str = admin):
        return cfg.describe(guard(cfg.check, key))

    @app.put("/api/settings/{key}")
    async def settings_put(key: str, value: Any = Body(..., embed=True), who: str = admin):
        d = guard(cfg.set, key, value)
        h.fleet.audit(who, "settings.set", d["key"])
        return d

    @app.delete("/api/settings/{key}")
    async def settings_delete(key: str, who: str = admin):
        d = guard(cfg.unset, key)
        h.fleet.audit(who, "settings.unset", d["key"])
        return d
