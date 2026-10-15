"""Fleet manager: the single source of truth for sandbox lifecycle.

The MCP server, REST API, dashboard and CLI are thin layers over this class.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import copy  # --- review(identity)
import json
import logging
import re
import secrets
import shlex
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import hangar.identity as identity  # --- identity
import hangar.people as people  # --- identity
from hangar import cloudcreds as cloudcreds  # --- egress
from hangar import egress as egress  # --- egress
from hangar import envbuilds as envbuilds  # --- envbuilds
from hangar import envdoctor as envdoctor  # --- computer
from hangar import envservices as envservices  # --- envbuilds
from hangar import exchange, harden, images, isolation, observe  # --- observe, isolation, harden, images
from hangar import fairness as fairness  # --- scale
from hangar import ghapi as ghapi  # --- egress2
from hangar import k8sgc as k8sgc  # --- durable
from hangar import metrics as metrics  # --- reconciler
from hangar import modelpolicy as modelpolicy  # --- canary: `model: frontier` resolved to an id
from hangar import opstatus as opstatus  # --- clouddoctor: the day's failures and credential health in the overview
from hangar import power as power  # --- ops: the sandbox pools' power schedule
from hangar import reconciler as reconciler  # --- reconciler
from hangar import slo as slo  # --- clouddoctor: create times by path, for `hangar status`
from hangar import supervisor as supervisor  # --- envbuilds
from hangar import teamcreds as teamcreds  # --- teamcreds
from hangar.broker import AUTH_FILE_LIMIT, Broker, CredentialError
from hangar.config import Settings
from hangar.egress_proxy import EgressProxy  # --- egress
from hangar.envspec import BuildContext, EnvCatalog, EnvSpec, render, repo_dirname, warm_pool_now
from hangar.gateway import Gateway
from hangar.harnesses import (
    HARNESSES,
    Harness,
    deep_merge,
    get_harness,
    mcp_config,
    merge_codex_toml,
    pack_digest,
    pack_dockerfile,
    pack_script,
    pack_tag,
    resolve_refs,
    version_from_log,  # --- images
)
from hangar.membudget import MemoryBudget  # --- scale
from hangar.models import (
    AuditEvent,
    Credential,
    CredentialKind,
    ExecResult,
    Resources,
    Sandbox,
    SandboxState,
    Snapshot,
    SnapshotKind,
    new_id,
    now,
)
from hangar.providers import Provider, ProviderError, load_provider
from hangar.store import Store

log = logging.getLogger("hangar.fleet")

ACTIVE = {SandboxState.PENDING, SandboxState.STARTING, SandboxState.RUNNING, SandboxState.PAUSED}
_NAME_RE = re.compile(r"[^a-z0-9-]+")
_ENV_NAME = re.compile(r"[A-Z_][A-Z0-9_]*")
# --- overnight/sweep: what hangar reads back from files the sandbox can grow (it decides their size)
CONFIG_READ_LIMIT = 8 * 1024 * 1024  # a harness config hangar merges into (~/.claude.json, ~/.codex/config.toml)
# --- scale: B12. Bytes of memory per archive byte while hangar holds a workspace archive. Reading one back, a provider
# can hold its base64 text and the decoded bytes at once (pods/exec without envd); writing one, the bytes and a copy
# (Docker's tar stream, a frame being encoded).
ARCHIVE_READ_COST = 3
ARCHIVE_WRITE_COST = 2


class FleetError(RuntimeError):
    pass


class PoweredDown(FleetError):  # --- ops
    """The sandbox pools are powered down (power.py): the create is refused at once, saying when they're back."""


def _slug(s: str) -> str:
    return _NAME_RE.sub("-", s.lower()).strip("-")[:40] or "sandbox"


def _clip_tail(text: str, line_max: int = 2000, total_max: int = 24000) -> str:
    """Keep agent output readable by MCP clients: some harnesses (pi --mode json) emit one JSON event per
    line with the whole conversation inside, so a 60-line tail can be megabytes."""
    lines = [ln if len(ln) <= line_max else f"{ln[:line_max]}… [{len(ln) - line_max} chars clipped]"
             for ln in text.splitlines()]
    out: list[str] = []
    size = 0
    for ln in reversed(lines):
        size += len(ln) + 1
        if size > total_max:
            out.append(f"[{len(lines) - len(out)} earlier lines clipped]")
            break
        out.append(ln)
    return "\n".join(reversed(out))


class BuildJob:
    def __init__(self, env: str, provider: str):
        self.id = new_id("build")
        self.env = env
        self.provider = provider
        self.state = "running"
        self.image: str | None = None
        self.error: str | None = None
        self.started_at = now()
        self.finished_at: float | None = None
        self.key: tuple[Any, ...] | None = None  # --- deadlines: what it builds; asking for it again gets this job
        self.lines: collections.deque[str] = collections.deque(maxlen=4000)
        self.total = 0  # monotonic count of emitted lines (the deque drops old ones)
        self._cond = asyncio.Condition()
        self.task: asyncio.Task | None = None

    async def emit(self, line: str) -> None:
        self.lines.append(line)
        self.total += 1
        async with self._cond:
            self._cond.notify_all()

    async def follow(self) -> AsyncIterator[str]:
        seen = 0
        while True:
            snapshot, total = list(self.lines), self.total
            first = total - len(snapshot)  # absolute index of snapshot[0]
            for line in snapshot[max(seen - first, 0):]:
                yield line
            seen = total
            if self.state != "running":
                return
            async with self._cond:
                try:
                    await asyncio.wait_for(self._cond.wait(), timeout=2)
                except TimeoutError:
                    pass

    def summary(self) -> dict[str, Any]:
        return {
            "id": self.id, "environment": self.env, "provider": self.provider, "state": self.state,
            "image": self.image, "error": self.error, "started_at": self.started_at,
            "finished_at": self.finished_at, "log_tail": list(self.lines)[-15:],
        }


class Fleet:
    def __init__(self, settings: Settings, store: Store, broker: Broker, catalog: EnvCatalog):
        self.settings = settings
        self.store = store
        self.broker = broker
        broker.settings = settings  # --- identity: the broker refuses the credentials these settings name
        self.catalog = catalog
        self.providers: dict[str, Provider] = {}
        self.provider_errors: dict[str, str] = {}
        self.builds: dict[str, BuildJob] = {}
        self._build_locks: dict[str, asyncio.Lock] = collections.defaultdict(asyncio.Lock)
        self._pack_builds: dict[str, asyncio.Task] = {}  # packs rebuilt in the background (see _resolve_packs)
        self._reaper: asyncio.Task | None = None
        self.logins: dict[str, dict[str, Any]] = {}
        # Called with a running sandbox just before it is destroyed (any reason: user, reaper, job). Sessions
        # (spec 0010) use it to checkpoint first. Hooks must be quick and must not raise.
        self.pre_destroy: list[Callable[[Sandbox], Awaitable[None]]] = []
        # Live dashboard/CLI terminals per sandbox. Someone watching or typing keeps sessions awake.
        self.attached: collections.Counter[str] = collections.Counter()
        self._warm_applied: dict[str, int] = {}
        # --- review(perf): an agent_status long-poll holds an I/O worker thread for its whole wait; at most half
        # the pool waits at once (sessions and MCP clients alike), so waiters can never starve other provider calls.
        self._waiters: asyncio.Semaphore | None = None  # sized from settings.io_pool on first use
        # --- overnight/sweep: set on a shutdown signal (shutdown.py); long-polls stop waiting and answer
        self.closing = asyncio.Event()
        # --- workstream F: git gateway. Grants for sandboxes in credential_mode: gateway (spec 0003).
        self.gateway = Gateway(self)
        self.egress = EgressProxy(self)  # --- egress: the hostname-allowlist proxy (egress_proxy.py)
        self.cloud = cloudcreds.Issuer(self)  # --- egress: short-lived cloud credentials (cloudcreds.py)
        self.tier_down: dict[str, float] = {}  # --- providers-e2b: provider -> monotonic time it may be tried again
        # --- scale: B12. Workspace archives in the control plane's memory at once (snapshots, forks, restores).
        self.archive_memory = MemoryBudget(settings.workspace_transfer_mb * 1024 * 1024)
        self.reconciler = reconciler.Reconciler(self)  # --- reconciler: rows a restart left unfinished, replaced pods
        self.power = power.Power(store, settings)  # --- ops: creates on powered-down pools answer at once

    # ================================================================== setup
    async def start(self) -> None:
        for name in dict.fromkeys([self.settings.default_provider, *self.settings.enabled_providers]):
            try:
                self.providers[name] = load_provider(name, self.settings)
            except Exception as e:  # noqa: BLE001 - surface in UI, keep serving other providers
                self.provider_errors[name] = str(e)
                log.warning("provider %s unavailable: %s", name, e)
        await self.reconciler.run(boot=True)  # --- reconciler: before sessions and jobs look at their sandboxes
        self.reconciler.start()
        self._reaper = asyncio.create_task(self._reaper_loop())
        self.cloud.start()  # --- egress: renews sandboxes' cloud tokens

    async def stop(self) -> None:
        if self._reaper:
            self._reaper.cancel()
        await self.reconciler.stop()  # --- reconciler: destroys in flight get a moment to finish
        await self.cloud.stop()  # --- egress
        for t in self._pack_builds.values():
            t.cancel()
        for p in self.providers.values():
            await p.close()

    def provider(self, name: str | None) -> Provider:
        name = name or self.settings.default_provider
        if name in self.providers:
            return self.providers[name]
        reason = self.provider_errors.get(name, "not enabled (add it to HANGAR_ENABLED_PROVIDERS)")
        raise FleetError(f"provider '{name}' unavailable: {reason}")

    def audit(self, actor: str, action: str, target: str | None = None, **detail: Any) -> None:
        # Commands, prompts and terminal input can contain pasted secrets; mask any stored credential value.
        detail = self.broker.redact(detail)
        self.store.audit(AuditEvent(actor=actor, action=action, target=target, detail=detail))

    # ================================================================== environments
    def environments(self) -> list[dict[str, Any]]:
        self.catalog.reload()
        out = []
        for name in self.catalog.names():
            try:
                spec = self.catalog.get(name)
            except Exception as e:  # noqa: BLE001
                out.append({"name": name, "error": str(e)})
                continue
            images = {p: self.store.kv_get(f"image:{p}:{name}") for p in self.providers}
            out.append({
                "name": name, "description": spec.description, "extends": spec.extends, "base": spec.base,
                "harnesses": spec.harnesses, "default_harness": spec.pick_harness(),
                "harness_mode": spec.harness_mode, "image": spec.image, "model": spec.model,
                "mcp": sorted(spec.mcp), "secrets": sorted(spec.secrets),
                "repo": spec.repo.url if spec.repo else None,
                "skills": len(spec.skills), "warm_pool": spec.warm_pool,
                "resources": spec.resources.model_dump(), "credentials": spec.credentials,
                "images": {k: v for k, v in images.items() if v},
                # --- images: per agent harness, the locked version and what each provider's pack installed
                "harness_versions": {h: {"locked": HARNESSES[h].version,
                                         "installed": {p: v for p in self.providers
                                                       if (v := self.harness_version(p, h))}}
                                     for h in spec.agent_harnesses if HARNESSES[h].install},
                # --- canary: what `model` resolves to now for each harness (a policy: from the provider's list)
                "models": {h: modelpolicy.of(self).view(h, spec.model_for(h))  # --- review #105: and its price
                           for h in spec.agent_harnesses},
            })
        return out

    def _extra_ca(self) -> bytes | None:
        p = self.settings.extra_ca_bundle
        return p.expanduser().read_bytes() if p else None

    def environment_detail(self, name: str) -> dict[str, Any]:
        spec = self.catalog.get(name)
        if spec.image:
            return {"spec": spec.model_dump(mode="json"), "raw": self.catalog.raw(name),
                    "dockerfile": f"# prebuilt image, not built by hangar\n# {spec.image}\n", "digest": None}
        ctx = render(spec, self.settings.sandbox_user, self.settings.workspace_dir, self._extra_ca())
        return {"spec": spec.model_dump(mode="json"), "raw": self.catalog.raw(name), "dockerfile": ctx.dockerfile,
                "digest": ctx.digest}

    def save_environment(self, data: dict[str, Any], actor: str = "system") -> EnvSpec:
        identity.check_environment(data, self.store, self.settings)  # --- identity: admin fields, control-plane secrets
        people.claim_environment(self.store, self.catalog, data)  # --- identity: members change only their own
        spec = self.catalog.add(data)
        self.audit(actor, "environment.save", spec.name)
        return spec

    def validate_environment(self, data: dict[str, Any]) -> dict[str, Any]:
        """Check a spec without saving it: resolves `extends`, validates, renders the Dockerfile."""
        name = str(data.get("name") or "")
        problems: list[str] = []
        # --- review(identity): on a copy of the catalog. Swapping the spec into the shared one (from a sync tool's
        # worker thread) let sandboxes starting meanwhile, anyone's, use the caller's spec.
        scratch = copy.copy(self.catalog)
        scratch._raw = {**self.catalog._raw, name: (data, self.catalog.dirs[0] if self.catalog.dirs else None)}
        try:
            spec = scratch.get(name)
            dockerfile = None if spec.image else render(
                spec, self.settings.sandbox_user, self.settings.workspace_dir, self._extra_ca()).dockerfile
        except Exception as e:  # noqa: BLE001 - reported to the caller
            return {"valid": False, "errors": [str(e)]}
        known = {c.name for c in self.broker.list()}
        for c in [*spec.credentials, *spec.secrets.values()]:
            if c not in known:
                problems.append(f"credential '{c}' is not in the vault yet")
        if not spec.agent_harnesses:
            problems.append("no agent harness enabled: sandboxes can run commands but not run_agent")
        for h in spec.harnesses:
            if HARNESSES[h].deprecated:
                problems.append(f"{h}: {HARNESSES[h].deprecated}")
        return {"valid": True, "warnings": problems, "spec": spec.model_dump(mode="json"),
                "default_harness": spec.pick_harness(), "dockerfile": dockerfile}

    def clone_environment(self, source: str, name: str, overrides: dict[str, Any] | None = None,
                          actor: str = "system") -> EnvSpec:
        """New environment that `extends` an existing one, with optional overrides (lists merge, maps merge)."""
        self.catalog.get(source)
        if name in self.catalog.names():
            raise FleetError(f"environment '{name}' already exists")
        data = {**(overrides or {}), "name": name, "extends": source}
        data.setdefault("description", f"{source} + customisations")
        return self.save_environment(data, actor=actor)

    def _tag_for(self, spec: EnvSpec, provider: str, bake: list[str] | None = None) -> tuple[str, Any]:
        return images.env_tag(self.settings, spec, provider, bake)  # --- images: one tag rule for CI and hangar

    # ================================================================== harness packs
    def _uses_packs(self, spec: EnvSpec, pname: str) -> bool:
        can = self.provider(pname).capabilities.image_volumes
        if spec.harness_mode == "pack" and not can:
            raise FleetError(f"environment {spec.name} sets harness_mode: pack but the {pname} provider can't "
                             "mount images; use harness_mode: auto or bake")
        return can and spec.harness_mode != "bake"

    def _bake_for(self, spec: EnvSpec, pname: str, harness: str | None) -> list[str]:
        if spec.image or self._uses_packs(spec, pname) or not harness:
            return []
        return [harness] if HARNESSES[harness].install else []

    async def build_pack(self, harness: str, provider: str | None = None, force: bool = False,
                         actor: str = "system", log_fn: Any = None) -> str:
        """Build and publish one harness pack (an OCI image holding PACK_ROOT/<harness>)."""
        h = get_harness(harness)
        if not h.install:
            raise FleetError(f"{harness} has nothing to install")
        pname = provider or self.settings.default_provider
        prov = self.provider(pname)
        tag, ctx = self.pack_context(harness, pname)
        async with self._build_locks[f"{pname}:pack:{harness}"]:
            if not force and self.store.kv_get(f"pack:{pname}:{harness}") == tag:
                if self._pack_info(pname, harness).get("tag") != tag:  # --- images: recorded before digests were
                    return await self._record_pack(prov, pname, harness, tag)
                return self._pack_mount(pname, harness, tag)  # built or registered (hangar pack publish) earlier
            version = None
            if force or not await prov.image_exists(tag):
                lines: list[str] = []

                async def logged(line: str) -> None:  # --- images: the build log names the installed version
                    lines.append(line)
                    if log_fn:
                        await log_fn(line)

                await logged(f"building harness pack {tag}")
                tag = await prov.build_template(EnvSpec(name=f"harness-{h.name}"), ctx, tag, log=logged)
                version = version_from_log(lines)
                self.audit(actor, "harness.pack_build", harness, provider=pname, image=tag)
            self.store.kv_set(f"pack:{pname}:{harness}", tag)
            return await self._record_pack(prov, pname, harness, tag, version)  # --- images: mounted by digest

    def pack_context(self, harness: str, provider: str | None = None) -> tuple[str, BuildContext]:
        """Pack image tag + build context, for building on another machine (hangar pack publish)."""
        h = get_harness(harness)
        if not h.install:
            raise FleetError(f"{harness} has nothing to install")
        pname = provider or self.settings.default_provider
        registry = self.settings.registry if pname in ("kubernetes", "e2b") else None
        ctx = BuildContext(dockerfile=pack_dockerfile(h), files={f"packs/{h.name}.sh": pack_script(h).encode()},
                           digest=pack_digest(h))
        return pack_tag(h, self.settings.image_prefix, registry), ctx

    async def register_pack(self, harness: str, image: str, provider: str | None = None,
                            actor: str = "system") -> str:
        """Record a pack built elsewhere (hangar pack publish). --- images: returns what sandboxes mount, the image's
        digest when the registry tells it."""
        get_harness(harness)
        pname = provider or self.settings.default_provider
        prov = self.provider(pname)
        tag, _, digest = image.partition("@")
        if digest and not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
            raise FleetError(f"not a sha256 digest: {digest[:80]}")
        repo = tag[:i] if (i := tag.rfind(":")) > tag.rfind("/") else tag
        if hasattr(prov, "_known_images"):
            prov._known_images.add(tag)  # type: ignore[attr-defined]
        async with self._build_locks[f"{pname}:pack:{harness}"]:
            self.store.kv_set(f"pack:{pname}:{harness}", tag)
            ref = await self._record_pack(prov, pname, harness, tag, pinned=f"{repo}@{digest}" if digest else None)
        self.audit(actor, "harness.pack_register", harness, provider=pname, image=ref)
        return ref

    # --- images: per pack, what sandboxes mount (repo@sha256:... when the provider's registry tells the digest, so a
    # tag that moves can't mix versions on a fleet), the CLI version it installed, and the ref it replaced.
    def _pack_info(self, pname: str, harness: str) -> dict[str, Any]:
        try:
            info = json.loads(self.store.kv_get(f"packinfo:{pname}:{harness}") or "{}")
        except ValueError:
            return {}
        return info if isinstance(info, dict) else {}

    def _pack_mount(self, pname: str, harness: str, tag: str) -> str:
        info = self._pack_info(pname, harness)
        return (info.get("ref") or tag) if info.get("tag") == tag else tag

    async def _record_pack(self, prov: Provider, pname: str, harness: str, tag: str, version: str | None = None,
                           pinned: str | None = None) -> str:
        ref = await prov.image_digest(tag) or pinned
        if ref and version is None:
            version = await prov.pack_version(ref)
        old = self._pack_info(pname, harness)
        info = {"tag": tag, "ref": ref, "version": version, "locked": get_harness(harness).version}
        if (prev := old.get("ref") or old.get("tag")) and prev != (ref or tag):
            info["prev"] = prev  # the last good pack, for whoever wants to keep mounting it during a rebuild
        self.store.kv_set(f"packinfo:{pname}:{harness}", json.dumps(info))
        return ref or tag

    def harness_version(self, pname: str, harness: str) -> str | None:
        """The installed CLI version of the pack sandboxes on `pname` mount for `harness`, when it is known."""
        info = self._pack_info(pname, harness)
        tag = self.store.kv_get(f"pack:{pname}:{harness}")
        return info.get("version") if tag and info.get("tag") == tag else None

    async def _resolve_packs(self, spec: EnvSpec, pname: str, actor: str, log_fn: Any = None,
                             only: str | None = None, lazy: bool = False) -> EnvSpec:
        """Attach the pack image for every agent harness the env allows (all are mounted, read-only, so a
        warm sandbox can be claimed for any of them; only the selected one is configured).

        `lazy`: wait only on the pack of `only` (a sandbox runs just the harness it was created for; None waits on
        none). The others are mounted when they are already built; a missing or stale one (its harness changed in
        an upgrade) is built in the background rather than holding anyone up. Creates, the image build a create
        triggers and scheduled warm-pool syncs are lazy; explicit builds and prebuild build every pack."""
        spec.packs = {}
        if not self._uses_packs(spec, pname):
            return spec
        for hname in spec.agent_harnesses:
            if not HARNESSES[hname].install:
                continue
            if lazy and hname != only:
                tag = pack_tag(get_harness(hname), self.settings.image_prefix,
                               self.settings.registry if pname in ("kubernetes", "e2b") else None)
                if self.store.kv_get(f"pack:{pname}:{hname}") == tag:
                    spec.packs[hname] = self._pack_mount(pname, hname, tag)  # --- images
                else:
                    self._pack_in_background(hname, pname, actor)
                continue
            spec.packs[hname] = await self.build_pack(hname, pname, actor=actor, log_fn=log_fn)
        return spec

    def refresh_stale_packs(self) -> list[str]:
        """At startup: rebuild, in the background, every pack this hangar used before whose harness changed in the
        upgrade (its tag moved), so the first sandbox for that harness after a deploy doesn't wait on the build."""
        started = []
        for pname, prov in self.providers.items():
            if not prov.capabilities.image_volumes:
                continue
            registry = self.settings.registry if pname in ("kubernetes", "e2b") else None
            for hname, h in HARNESSES.items():
                had = self.store.kv_get(f"pack:{pname}:{hname}")
                if h.install and had and had != pack_tag(h, self.settings.image_prefix, registry):
                    self._pack_in_background(hname, pname, "upgrade")
                    started.append(f"{pname}:{hname}")
        if started:
            log.info("rebuilding packs whose harness changed: %s", ", ".join(started))
        return started

    def _pack_in_background(self, harness: str, pname: str, actor: str) -> None:
        key = f"{pname}:{harness}"
        if (t := self._pack_builds.get(key)) and not t.done():
            return

        async def run() -> None:
            try:
                await self.build_pack(harness, pname, actor=actor)
            except Exception:  # noqa: BLE001 - a later create retries; this one did not need the pack
                log.warning("background build of the %s pack on %s failed", harness, pname, exc_info=True)

        self._pack_builds[key] = asyncio.create_task(run())

    async def build(self, env: str, provider: str | None = None, force: bool = False, actor: str = "system",
                    wait: bool = True, for_create: tuple[str | None] | None = None) -> BuildJob:
        """Build an environment's image and its harness packs. `for_create=(harness,)`: the build a sandbox create
        waits on; it builds the image and that harness's pack side by side and leaves the other packs to the
        background (see _resolve_packs), so a create never waits on a harness it won't run."""
        pname = provider or self.settings.default_provider
        prov = self.provider(pname)
        spec = self.catalog.get(env)
        if spec.image:
            raise FleetError(f"environment {env} uses the prebuilt image {spec.image}; there is nothing to build")
        tag, ctx = self._tag_for(spec, pname, self._bake_for(spec, pname, spec.pick_harness()))
        # --- deadlines (B17): asking again while this very build runs (as MCP clients do to see where it is) gets
        # that build back, not a second job queued behind it with an empty log, nor (force) a second Cloud Build. A
        # create's build (one harness's pack) and a full one are different builds.
        key = (env, pname, tag, for_create)
        running = next((j for j in self.builds.values() if j.state == "running" and j.key == key), None)
        if running is not None:
            if wait and running.task:
                await asyncio.shield(running.task)
            return running
        job = BuildJob(env, pname)
        job.key = key
        self.builds[job.id] = job

        async def _run() -> None:
            async with self._build_locks[f"{pname}:{env}"]:
                try:
                    async def image() -> None:
                        if not force and await prov.image_exists(tag):
                            await job.emit(f"image {tag} is up to date")
                            job.image = prov.image_ref(tag)
                        else:
                            await job.emit(f"building {env} -> {tag}")
                            job.image = await prov.build_template(spec, ctx, tag, log=job.emit)

                    packs = (self._resolve_packs(spec, pname, actor, log_fn=job.emit, only=for_create[0], lazy=True)
                             if for_create else self._resolve_packs(spec, pname, actor, log_fn=job.emit))
                    done = await asyncio.gather(packs, image(), return_exceptions=True)
                    if errs := [d for d in done if isinstance(d, BaseException)]:
                        raise errs[0]
                    self.store.kv_set(f"image:{pname}:{env}", job.image)
                    if spec.warm_pool:
                        live = self._warm(spec)
                        await job.emit(f"syncing warm pool to {live.warm_pool}"
                                       + (f" (schedule: {spec.warm_pool_hours})" if spec.warm_pool_hours else ""))
                        await prov.sync_warm_pool(live, job.image)
                    job.state = "succeeded"
                    self.audit(actor, "environment.build", env, provider=pname, image=job.image)
                except Exception as e:  # noqa: BLE001
                    job.state = "failed"
                    job.error = str(e)
                    await job.emit(f"ERROR: {e}")
                    log.exception("build failed")
                finally:
                    job.finished_at = now()

        job.task = asyncio.create_task(_run())
        if wait:
            await job.task
        return job

    def build_context(self, env: str, provider: str | None = None) -> tuple[str, bytes]:
        """Image tag + build-context tar, for building on another machine (laptop/CI) and registering after."""
        pname = provider or self.settings.default_provider
        spec = self.catalog.get(env)
        tag, ctx = self._tag_for(spec, pname, self._bake_for(spec, pname, spec.pick_harness()))
        return tag, ctx.tar()

    async def register_image(self, env: str, image: str, provider: str | None = None,
                             actor: str = "system") -> dict[str, Any]:
        """Record an externally built image for an environment and sync its warm pool."""
        pname = provider or self.settings.default_provider
        prov = self.provider(pname)
        spec = self.catalog.get(env)
        ref = prov.image_ref(image)
        self.store.kv_set(f"image:{pname}:{env}", ref)
        if hasattr(prov, "_known_images"):
            prov._known_images.add(image)  # type: ignore[attr-defined]
        await self._resolve_packs(spec, pname, actor)
        await prov.sync_warm_pool(self._warm(spec), ref)
        self.audit(actor, "environment.register_image", env, provider=pname, image=ref)
        return {"environment": env, "provider": pname, "image": ref}

    @observe.traced("sandbox.image")  # --- observe
    async def _ensure_image(self, spec: EnvSpec, pname: str, actor: str, harness: str | None = None,
                            stage: Callable[[str], None] | None = None) -> str:
        if spec.image:
            return spec.image
        prov = self.provider(pname)
        bake = self._bake_for(spec, pname, harness)
        tag, ctx = self._tag_for(spec, pname, bake)
        registered = self.store.kv_get(f"image:{pname}:{spec.name}")
        if registered and registered == prov.image_ref(tag):
            return registered
        if await prov.image_exists(tag):
            ref = prov.image_ref(tag)
            if bake == self._bake_for(spec, pname, spec.pick_harness()):
                # Built earlier (another replica, a lost data volume): record it, so warm pools and
                # `hangar doctor` see the environment as built.
                self.store.kv_set(f"image:{pname}:{spec.name}", ref)
            return ref
        if stage:
            stage(f"building the {spec.name} image (first use or the environment changed; usually a few minutes)")
        if bake and bake != self._bake_for(spec, pname, spec.pick_harness()):
            # A non-default harness on a provider without packs: build that variant directly.
            return await prov.build_template(spec, ctx, tag)
        job = await self.build(spec.name, pname, actor=actor, for_create=(harness,))
        if job.state != "succeeded" or not job.image:
            raise FleetError(f"build of {spec.name} failed: {job.error}")
        return job.image

    # ================================================================== sandboxes
    def get(self, ref: str) -> Sandbox:
        sb = self.store.get(Sandbox, ref)
        if sb is None:
            matches = self.store.list_where(Sandbox, name=ref, state=ACTIVE)  # --- scale: indexed, not a scan
            if len(matches) == 1:
                sb = matches[0]
        if sb is None:
            raise FleetError(f"no sandbox '{ref}'")
        return sb

    def list(self, include_stopped: bool = False, label: str | None = None) -> list[Sandbox]:
        # --- scale: live sandboxes from the state index; settled ones only when asked (retention.py bounds them)
        sbs = self.store.list(Sandbox) if include_stopped else self.store.list_where(Sandbox, state=ACTIVE)
        if label:
            k, _, v = label.partition("=")
            sbs = [s for s in sbs if s.labels.get(k) == v or (not v and k in s.labels)]
        return sorted(sbs, key=lambda s: s.created_at, reverse=True)

    def _touch(self, sb: Sandbox) -> None:
        sb.last_activity = now()
        self.store.put(sb)

    def touch(self, ref: str, min_interval: float = 30.0) -> None:
        """Record activity on a sandbox (throttled): terminals in use, API calls on its behalf."""
        with contextlib.suppress(FleetError):
            sb = self.get(ref)
            if now() - (sb.last_activity or 0) >= min_interval:
                self._touch(sb)

    def detach(self, ref: str) -> None:
        sid = self.get(ref).id if not ref.startswith("sb-") else ref
        self.attached[sid] -= 1
        if self.attached[sid] <= 0:
            del self.attached[sid]
        self.touch(sid, 0)

    @observe.traced("sandbox.create")  # --- observe
    async def create(
        self,
        environment: str,
        name: str | None = None,
        repo: str | None = None,
        branch: str | None = None,
        work_branch: str | None = None,
        credentials: list[str] | None = None,
        provider: str | None = None,
        ttl_minutes: int | None = None,
        labels: dict[str, str] | None = None,
        resources: Resources | None = None,
        harness: str | None = None,
        actor: str = "system",
        _image: str | None = None,
        _parent: str | None = None,
        _restore_workspace: str | None = None,
        _skip_repo: bool = False,
        progress: Callable[[str], None] | None = None,
        push_branch: str | None = None,
        tier: str | None = None,
        person: str | None = None,  # --- identity: the owning person (people.py); picks their own credentials
    ) -> Sandbox:
        """`progress` gets short plain-language updates while the sandbox starts (image build, waiting for a
        node, cloning...), so callers can show where the time goes. `push_branch` (default: `work_branch`) is
        the branch a gateway-mode sandbox may push. `tier` (or the environment's) picks the provider when none
        is named (tiers.py)."""
        # --- providers-e2b: no provider named: the tier's first one with room (an isolated one is never given up).
        # Must stay the first statement, so locals() is exactly the arguments.
        from hangar import tiers

        if tier := tiers.route(self, environment, tier, provider):
            call = {k: v for k, v in locals().items() if k not in ("self", "tier", "tiers")}
            return await tiers.create(self, tier, call)
        active = self.store.list_where(Sandbox, state=ACTIVE)  # --- scale: indexed, not a scan
        if len(active) >= self.settings.max_sandboxes:  # --- providers-e2b: paused for hot resume, they give way
            from hangar import hot_resume

            if await hot_resume.make_room(self):
                active = self.store.list_where(Sandbox, state=ACTIVE)  # --- scale
        if len(active) >= self.settings.max_sandboxes:
            raise FleetError(f"fleet is at max_sandboxes={self.settings.max_sandboxes}; destroy some first")
        fairness.check_sandboxes(self.settings, active, actor, person)  # --- scale: one person can't take the fleet
        spec = self.catalog.get(environment)
        pname = provider or self.settings.default_provider
        prov = self.provider(pname)
        self.power.check(pname)  # --- ops: the pools are off for the night: say when, don't wait for a node
        try:
            hname = spec.pick_harness(harness)
        except ValueError as e:
            raise FleetError(str(e)) from e
        if credentials is not None:
            cred_names = list(dict.fromkeys(credentials))
        else:
            # Only the selected harness's credentials (plus non-agent ones such as git) go into the sandbox.
            cred_names = [c for c in dict.fromkeys(spec.credentials)
                          if self._cred_harness(c) in (hname, None) or not self._is_agent(self._cred_harness(c))]
        # --- identity: never another person's credential; the person's own one for this harness when they have it
        # --- teamcreds: else the team's (the repo policy's `credentials`), waiting while every login is leased.
        # Nothing awaits from here to broker.acquire, so the free login it picks is still free there.
        cred_names = await teamcreds.for_sandbox(self, cred_names, person, hname, credentials is not None,
                                                 repo or (spec.repo.url if spec.repo else None), progress)
        for c in [*cred_names, *spec.secrets.values()]:
            self.broker.get(c)  # fail fast on typos
        for c in spec.secrets.values():  # --- identity: and on control-plane credentials mapped in (B4)
            self.broker.check_sandbox(c)
        people.check_secrets(self.store, spec.secrets, person)  # --- identity review: and on another person's own
        ttl = ttl_minutes if ttl_minutes is not None else self.settings.default_ttl_minutes
        base_name = _slug(name or f"{environment}-{new_id('x')[2:]}")
        taken = {s.name for s in active}
        unique = base_name if base_name not in taken else f"{base_name}-{new_id('x')[2:6]}"
        sb = Sandbox(
            name=unique,
            environment=environment,
            provider=pname,
            repo=None if _skip_repo else repo or (spec.repo.url if spec.repo else None),
            branch=None if _skip_repo else branch or (spec.repo.branch if spec.repo else None),
            credentials=cred_names,
            labels=labels or {},
            resources=resources or spec.resources,
            parent=_parent,
            expires_at=now() + ttl * 60 if ttl else None,
            created_by=actor,
            person=person,  # --- identity
        )
        if hname:
            sb.info["harness"] = hname
            # --- canary: the environment's model for it, a policy resolved now and pinned for the sandbox's runs
            res = modelpolicy.of(self).resolve(hname, spec.model_for(hname))
            sb.info.update({k: v for k, v in (("model", res.model), ("model_policy", res.policy),
                                              ("model_note", res.note)) if v})
        self.reconciler.own(sb.id, "create")  # --- reconciler: this task owns the row until it settles
        self.store.put(sb)
        self.audit(actor, "sandbox.create", sb.id, environment=environment, provider=pname, name=sb.name)
        def stage(msg: str) -> None:
            sb.info["stage"] = msg
            reconciler.note_stage(sb, msg)  # --- reconciler: the flight record keeps where it went
            self.store.put(sb)
            if progress:
                with contextlib.suppress(Exception):
                    progress(msg)

        try:
            creds = self.broker.acquire(cred_names, sb.id)  # fail before starting anything
            sb.state = SandboxState.STARTING
            self.store.put(sb)
            # --- envbuilds: the repo's last good build of this environment, when it has one (envbuilds.py)
            build = None if _image or _skip_repo else await envbuilds.of(self).pick(spec, pname, hname, sb, stage)
            image = _image or (build.image if build else None) or \
                await self._ensure_image(spec, pname, actor, hname, stage=stage)
            if not _image:  # --- envbuilds: the services it starts with (Kubernetes sidecars, Docker containers)
                envservices.prepare(self, spec, sb, build.build.config if build and build.build else None)
            sb.image = image
            pack_started, last_stage = time.monotonic(), 0.0

            async def pack_stage(msg: str) -> None:  # build_pack reports only when it really builds; at most 1/s
                nonlocal last_stage
                if (t := time.monotonic()) - last_stage >= 1:
                    last_stage = t
                    stage(f"building the {hname} harness pack, {int(t - pack_started)}s so far (first use, or the "
                          f"harness changed in an upgrade; usually a few minutes): {msg[-160:]}")

            await self._resolve_packs(spec, pname, actor, log_fn=pack_stage, only=hname, lazy=True)
            if spec.packs:
                sb.info["packs"] = dict(spec.packs)
                if hname in spec.packs and (v := self.harness_version(pname, hname)):  # --- images
                    sb.info["harness_version"] = v
                if hname in spec.packs:  # --- canary: the locked version its pack was built for (pins.py)
                    sb.info["harness_lock"] = get_harness(hname).version
            stage("starting the sandbox")
            with observe.span("sandbox.start", {"hangar.sandbox.id": sb.id, "hangar.provider": pname}):  # --- observe
                # image may be a snapshot ref (forks); --- envbuilds: a build's own template on Kubernetes
                await prov.create(sb, image, envbuilds.EnvBuilds.provider_spec(self._warm(spec), sb))
            self.store.put(sb)
            await self._wait_running(prov, sb, stage=stage)
            if _parent:
                await self._scrub_inherited_secrets(prov, sb)
            # workstream F: in gateway mode this mints the grant and drops git secrets before anything is injected
            creds = await self.gateway.attach(prov, sb, spec, creds, push_branch or work_branch, _parent)
            await self._apply_session(prov, sb, spec, hname, creds)
            if _restore_workspace:
                await self._restore_workspace(prov, sb, _restore_workspace)
            elif sb.repo:
                stage(f"cloning {repo_dirname(sb.repo)}")
                await self._setup_repo(prov, sb, spec, work_branch, stage=stage)
                # --- envbuilds: the repo's config: the supervisor, the agent's instructions, a rebuild if behind
                await envbuilds.of(self).after_setup(prov, sb, spec, hname, stage)
                # integration (#93 + #94): the doctor's status file carries what the repo's config just added
                # (services, programs, setup failures, the build), not only what the claim knew
                await envdoctor.write_status(prov, sb, f"/home/{self.settings.sandbox_user}",
                                             envdoctor.build_status(self.settings, sb, spec, fleet=self))
            sb.info.pop("stage", None)
            sb.state = SandboxState.RUNNING
            sb.error = None
            self._touch(sb)
            slo.created(sb, fork=_parent is not None or _image is not None)  # --- clouddoctor
            # --- ops: + the day's size; --- envbuilds: never from a repo build's image
            if self._warm(spec).warm_pool and pname in self.providers and _image is None and not build:
                asyncio.create_task(self._refill(self._warm(spec), pname, image))
            self.reconciler.disown(sb.id)  # --- reconciler
            return sb
        except BaseException as e:  # includes CancelledError (client cancelled / tool timeout)
            sb.state = SandboxState.ERROR
            sb.error = str(e) or type(e).__name__
            self.store.put(sb)
            self.reconciler.disown(sb.id)  # --- reconciler
            # --- reconciler: the lease goes now, not in the cleanup below, which a shutdown can cut short
            reconciler.release_leases(self.store, sb.id)
            log.warning("create failed for %s: %s", sb.id, sb.error)

            async def _cleanup() -> None:
                try:
                    self.gateway.revoke(sb.id)  # workstream F
                    harden.forget(self, sb.id)  # --- harden
                    await self.broker.release(sb, prov, write_back=False)
                    await prov.destroy(sb)
                except Exception:  # noqa: BLE001
                    log.exception("cleanup after failed create of %s", sb.id)

            await asyncio.shield(asyncio.ensure_future(_cleanup()))
            if not isinstance(e, Exception):
                raise
            raise FleetError(f"failed to create sandbox: {e}") from e

    def _cred_harness(self, name: str) -> str | None:
        c = self.store.get(Credential, name)
        return c.harness if c else None

    @staticmethod
    def _is_agent(harness: str | None) -> bool:
        return bool(harness and harness in HARNESSES and HARNESSES[harness].agent)

    @observe.traced("sandbox.setup")  # --- observe
    async def _apply_session(self, prov: Provider, sb: Sandbox, spec: EnvSpec, hname: str | None,
                             creds: list[Credential]) -> None:
        """Per-sandbox setup for the selected harness: PATH and env, credentials, secrets, config and MCP."""
        h: Harness | None = HARNESSES.get(hname) if hname else None
        prelude: list[str] = []
        extra_env: dict[str, str] = {}
        # --- review #102: agent-led setup's validation sandbox runs an unmerged, agent-written install. It gets the
        # git credential its clone needs and nothing else: no other credential, no `secrets:`, no cloud tokens.
        validating = envbuilds.VALIDATE_LABEL in sb.labels
        if validating:
            from hangar.gateway import GIT_KINDS

            creds = [c for c in creds if c.kind in GIT_KINDS]
        if h:
            if h.install:
                extra = "".join(f":{h.prefix}/{d}" for d in h.path_extra)  # --- images: the pack carries no wrapper
                prelude.append(f'export PATH="{h.prefix}/bin:$PATH{extra}"')
            extra_env.update(h.env)
            extra_env["HANGAR_HARNESS"] = h.name
        for var, cred in ({} if validating else spec.secrets).items():  # --- review #102
            extra_env[var] = self.broker.sandbox_secret(cred)  # --- identity: refuses control-plane credentials
        extra_env.update(await egress.attach(self, prov, sb, spec))  # --- egress: HTTPS_PROXY in a proxied env
        if not validating:  # --- review #102
            extra_env.update(await self.cloud.attach(prov, sb, spec))  # --- egress: cloud OIDC tokens, no stored keys
        extra_env.update(ghapi.sandbox_env(self.settings, sb))  # --- egress2: the read-only GitHub API (gateway)
        # workstream E: the `hangar-agent` helper (context exchange), installed per sandbox: no image rebuild
        await exchange.install_helper(prov, sb, f"/home/{self.settings.sandbox_user}")
        await supervisor.install(prov, sb, f"/home/{self.settings.sandbox_user}")  # --- envbuilds: `hangar-agent svc`
        prelude.append(exchange.PATH_LINE)
        await envdoctor.install(self, prov, sb, spec, h, extra_env)  # --- computer: hangar-env, the display, status
        env = await self.broker.inject(sb, prov, creds, extra_env=extra_env, prelude=prelude)
        if not h:
            return
        home = f"/home/{self.settings.sandbox_user}"
        for rel, patch in h.config.items():
            await self._merge_json(prov, sb, f"{home}/{rel}", patch)
        servers = {n: s for n, s in spec.mcp.items() if s.harnesses is None or h.name in s.harnesses}
        servers.update(envdoctor.builtin_mcp(spec, self.settings, home))  # --- computer: hangar-env, browser
        resolved = {}
        for n, srv in servers.items():
            missing: set[str] = set()
            fields = resolve_refs(srv.model_dump(include={"url", "headers", "command", "args", "env"}), env, missing)
            if missing:
                # Skip rather than fail: the server needs a credential this sandbox wasn't given.
                sb.info.setdefault("warnings", []).append(
                    f"MCP server '{n}' skipped: needs {', '.join(sorted(missing))} (add it to `secrets:` or attach "
                    "a credential that sets it)")
                continue
            resolved[n] = srv.model_copy(update=fields)
        if resolved and h.mcp_format:
            rel, content = mcp_config(h.mcp_format, resolved)
            if isinstance(content, str):
                if (cur := await self._read_config(prov, sb, f"{home}/{rel}")) is not None:
                    await prov.write_file(sb, f"{home}/{rel}", merge_codex_toml(cur.decode(), content).encode(),
                                          mode=0o600)
            else:
                await self._merge_json(prov, sb, f"{home}/{rel}", content)
            sb.info["mcp"] = sorted(resolved)
        await observe.configure_harness(prov, sb, h.name, home, self.settings)  # --- observe: Codex's [otel]
        harden.keep(self, prov, sb, h, resolved, env, prelude)  # --- harden: hangar's own copy

    async def _read_config(self, prov: Provider, sb: Sandbox, path: str) -> bytes | None:
        """A config file to merge into (b"" when missing), or None, with a warning, when it is over
        CONFIG_READ_LIMIT: then it is left as it is rather than read whole or overwritten."""
        raw = await prov.read_file(sb, path, limit=CONFIG_READ_LIMIT)
        if raw is not None and len(raw) > CONFIG_READ_LIMIT:
            sb.info.setdefault("warnings", []).append(
                f"{path} is over {CONFIG_READ_LIMIT // 1024 // 1024} MB; hangar's settings were not merged into it")
            return None
        return raw or b""

    async def _merge_json(self, prov: Provider, sb: Sandbox, path: str, patch: dict[str, Any]) -> None:
        raw = await self._read_config(prov, sb, path)  # --- overnight/sweep: capped
        if raw is None:
            return
        try:
            cur = json.loads(raw) if raw else {}
        except ValueError:
            cur = {}
        await prov.write_file(sb, path, json.dumps(deep_merge(cur, patch), indent=2).encode(), mode=0o600)

    def _warm(self, spec: EnvSpec) -> EnvSpec:
        """The spec with the warm pool size that applies now (warm_pool_hours schedules it to 0 off-hours)."""
        n = max(warm_pool_now(spec), self.power.warm_for(spec.name))  # --- ops: sized for the day at power-up
        return spec if n == spec.warm_pool else spec.model_copy(update={"warm_pool": n})

    async def sync_scheduled_pools(self) -> None:
        """Scale scheduled warm pools up or down when their working hours start or end."""
        for name in self.catalog.names():
            try:
                spec = self.catalog.get(name)
            except Exception:  # noqa: BLE001 - a broken spec must not stop the others
                continue
            if not spec.warm_pool or not spec.warm_pool_hours:
                continue
            want = warm_pool_now(spec)
            for pname, prov in self.providers.items():
                image = self.store.kv_get(f"image:{pname}:{name}")
                key = f"{pname}:{name}"
                if not image or self._warm_applied.get(key) == want:
                    continue
                try:
                    await self._resolve_packs(spec, pname, "scheduler", lazy=True)  # the reaper never waits on a build
                    await prov.sync_warm_pool(spec.model_copy(update={"warm_pool": want}), image)
                    self._warm_applied[key] = want
                    log.info("warm pool %s on %s -> %d (schedule %s)", name, pname, want, spec.warm_pool_hours)
                except Exception:  # noqa: BLE001
                    log.exception("scheduled warm pool sync failed for %s", key)

    async def prebuild(self, names: list[str], pname: str | None = None) -> None:
        """Build environment images ahead of first use (at startup), so the first job or session doesn't wait on
        a build. Missing images are built one at a time; existing ones return immediately."""
        pname = pname or self.settings.default_provider
        for name in dict.fromkeys(names):
            try:
                spec = self.catalog.get(name)
                if spec.image:
                    continue
                await self._ensure_image(spec, pname, "prebuild")
                await self._resolve_packs(spec, pname, "prebuild")
                log.info("prebuild: %s ready on %s", name, pname)
                if stale := envbuilds.of(self).refresh(name, pname):  # --- envbuilds: repo builds on its old image
                    log.info("prebuild: rebuilding %s's repo builds on its new image: %s", name, ", ".join(stale))
            except Exception as e:  # noqa: BLE001
                log.warning("prebuild of %s failed: %s", name, e)

    async def _refill(self, spec: EnvSpec, pname: str, image: str) -> None:
        try:
            await self.providers[pname].sync_warm_pool(spec, image)
        except Exception:  # noqa: BLE001
            log.exception("warm pool refill failed")

    @observe.traced("sandbox.start_wait")  # --- observe
    async def _wait_running(self, prov: Provider, sb: Sandbox, timeout: float | None = None,
                            stage: Callable[[str], None] | None = None) -> None:
        timeout = timeout or self.settings.sandbox_start_timeout_seconds
        start = time.monotonic()
        deadline = start + timeout
        last_check, why = start, None
        while time.monotonic() < deadline:
            st = await prov.status(sb)
            if st == SandboxState.RUNNING:
                return
            if st in (SandboxState.ERROR, SandboxState.STOPPED):
                raise ProviderError(f"sandbox entered state {st}" + (f" ({why})" if why else ""))
            if time.monotonic() - last_check > 10:  # say why it's taking a while
                last_check = time.monotonic()
                with contextlib.suppress(Exception):
                    why = await prov.explain_pending(sb) or why
                if why and stage:
                    stage(f"waiting for the sandbox to start ({int(time.monotonic() - start)}s): {why}")
            await asyncio.sleep(0.5)
        raise ProviderError(f"timed out after {int(timeout)}s waiting for the sandbox to start"
                            + (f"; last seen: {why}" if why else ""))

    async def _scrub_inherited_secrets(self, prov: Provider, sb: Sandbox) -> None:
        """A fork inherits the parent's filesystem; drop secrets so leases and env are re-issued cleanly."""
        home = f"/home/{self.settings.sandbox_user}"
        paths = [f"{home}/.hangar/env", f"{home}/.git-credentials"]
        for c in self.store.list(Credential):
            if c.file_path:
                paths.append(c.file_path if c.file_path.startswith("/") else f"{home}/{c.file_path}")
        res = await prov.exec(
            sb, "tmux kill-server 2>/dev/null; rm -f " + " ".join(map(shlex.quote, paths))
            + f" && touch {home}/.hangar/env && chmod 600 {home}/.hangar/env", timeout=30)
        if res.exit_code != 0:
            raise FleetError(f"could not scrub inherited secrets from fork: {res.stderr[-300:]}")

    @observe.traced("sandbox.repo")  # --- observe
    async def _setup_repo(self, prov: Provider, sb: Sandbox, spec: EnvSpec, work_branch: str | None,
                          stage: Callable[[str], None] | None = None) -> None:
        ws = self.settings.workspace_dir
        dest = (spec.repo.path if spec.repo and spec.repo.path and spec.repo.url == sb.repo else None) or \
            f"{ws}/{repo_dirname(sb.repo)}"
        baked = bool(spec.repo and spec.repo.bake and spec.repo.url == sb.repo)
        prebuilt = envbuilds.EnvBuilds.prebuilt(sb)  # --- envbuilds: the repo's build holds its checkout, installed
        q = shlex.quote
        if prebuilt:  # --- envbuilds
            script = envbuilds.EnvBuilds.checkout_script(sb, dest)
        elif baked:
            script = f"cd {q(dest)} && git fetch --all --prune -q"
            if sb.branch:
                script += f" && git checkout -q {q(sb.branch)} && git pull -q --ff-only || true"
        else:
            script = f"git clone -q {'--branch ' + q(sb.branch) + ' ' if sb.branch else ''}-- {q(sb.repo)} {q(dest)}"
        if work_branch:
            script += f" && cd {q(dest)} && git checkout -q -b {q(work_branch)}"
        res = await prov.exec(sb, script, timeout=600)
        if res.exit_code != 0:
            raise FleetError(f"repo setup failed: {(res.stderr or res.stdout)[-800:]}")
        setup = spec.repo.setup if spec.repo and spec.repo.url == sb.repo and not baked and not prebuilt else None
        if setup and stage:
            stage(f"running the repo's setup: {setup[:80]}")
        if setup:
            res = await prov.exec(sb, setup, cwd=dest, timeout=1800)
            if res.exit_code != 0:
                raise FleetError(f"repo setup command failed: {(res.stderr or res.stdout)[-800:]}")
        sb.info["repo_path"] = dest
        if work_branch:
            sb.info["work_branch"] = work_branch

    @observe.traced("sandbox.destroy")  # --- observe
    async def destroy(self, ref: str, actor: str = "system") -> Sandbox:
        sb = self.get(ref)
        if (running := self.reconciler.destroying(sb.id)) is not None:  # --- reconciler: one destroy at a time
            await asyncio.shield(running)
            return self.get(sb.id)
        prov = self.providers.get(sb.provider)
        if prov and sb.state == SandboxState.RUNNING:
            for hook in list(self.pre_destroy):
                try:
                    await asyncio.wait_for(hook(sb), timeout=self.settings.pre_destroy_timeout_seconds)
                except Exception as e:  # noqa: BLE001 - never block a destroy
                    log.warning("pre-destroy hook for %s failed: %s", sb.id, e)
        sb.state = SandboxState.STOPPING
        self.store.put(sb)
        # --- reconciler: the rest on a task of its own, so a caller cancelled now (a client that gave up, a
        # shutdown) can't leave the sandbox stopping with its pod and lease; after a restart the reconciler ends it
        return await asyncio.shield(self.reconciler.detach(sb.id, self._finish_destroy(sb, prov, actor)))

    async def _finish_destroy(self, sb: Sandbox, prov: Provider | None, actor: str) -> Sandbox:
        self.gateway.revoke(sb.id)  # workstream F: the grant dies before the sandbox does
        if prov:
            await self.broker.release(sb, prov, write_back=True)
            try:
                await prov.destroy(sb)
            except Exception as e:  # noqa: BLE001
                log.warning("destroy %s: %s", sb.id, e)
        sb.state = SandboxState.STOPPED
        self.store.put(sb)
        harden.forget(self, sb.id)  # --- harden
        self.audit(actor, "sandbox.destroy", sb.id, name=sb.name)
        return sb

    async def destroy_many(self, refs: list[str] | None = None, label: str | None = None,
                           actor: str = "system") -> list[str]:
        targets = [self.get(r) for r in refs] if refs else self.list(label=label)
        await asyncio.gather(*(self.destroy(s.id, actor) for s in targets), return_exceptions=True)
        return [s.id for s in targets]

    async def pause(self, ref: str, actor: str = "system") -> Sandbox:
        sb = self.get(ref)
        # --- reconciler: on GKE the pod goes away long before the row says paused; no pass may judge it gone
        self.reconciler.own(sb.id, "pause")
        try:
            await self.provider(sb.provider).pause(sb)
        finally:
            self.reconciler.disown(sb.id)
        sb.state = SandboxState.PAUSED
        self.store.put(sb)
        self.audit(actor, "sandbox.pause", sb.id)
        return sb

    async def resume(self, ref: str, actor: str = "system") -> Sandbox:
        sb = self.get(ref)
        self.reconciler.own(sb.id, "resume")  # --- reconciler: a new pod comes up under the claim
        try:
            await self.provider(sb.provider).resume(sb)
        finally:
            self.reconciler.disown(sb.id)
        sb.state = SandboxState.RUNNING
        self._touch(sb)
        self.audit(actor, "sandbox.resume", sb.id)
        return sb

    async def extend(self, ref: str, minutes: int, actor: str = "system") -> Sandbox:
        sb = self.get(ref)
        sb.expires_at = max(sb.expires_at or now(), now()) + minutes * 60
        self.store.put(sb)
        self.gateway.extend(sb)  # workstream F
        prov = self.providers.get(sb.provider)
        if prov and sb.state in ACTIVE:
            await prov.set_expiry(sb, sb.expires_at)
        self.audit(actor, "sandbox.extend", sb.id, minutes=minutes)
        return sb

    def _running(self, ref: str) -> tuple[Sandbox, Provider]:
        sb = self.get(ref)
        if sb.state == SandboxState.PAUSED:
            raise FleetError(f"sandbox {sb.id} is paused; resume it first")
        if sb.state != SandboxState.RUNNING:
            raise FleetError(f"sandbox {sb.id} is {sb.state}")
        return sb, self.provider(sb.provider)

    # ================================================================== I/O
    @observe.traced("sandbox.exec")  # --- observe
    async def exec(self, ref: str, command: str, cwd: str | None = None, timeout: int = 120,
                   actor: str = "system", quiet: bool = False) -> ExecResult:
        """`quiet` is for hangar's own housekeeping (checkpoints, idle checks): it neither counts as activity
        nor fills the audit log."""
        sb, prov = self._running(ref)
        if not quiet:
            self._touch(sb)
        res = await prov.exec(sb, command, timeout=timeout, cwd=cwd or sb.info.get("repo_path"))
        if not quiet:
            self.touch(sb.id, 0)  # a long command is activity for its whole run, not just its start
            self.audit(actor, "sandbox.exec", sb.id, command=command[:500], exit_code=res.exit_code)
        return res

    @observe.traced("sandbox.read_file")  # --- observe
    async def read_file(self, ref: str, path: str, limit: int | None = None) -> bytes | None:
        sb, prov = self._running(ref)
        self._touch(sb)
        return await prov.read_file(sb, self._abs(sb, path), limit=limit)

    @observe.traced("sandbox.write_file")  # --- observe
    async def write_file(self, ref: str, path: str, data: bytes, actor: str = "system") -> str:
        sb, prov = self._running(ref)
        p = self._abs(sb, path)
        await prov.write_file(sb, p, data)
        self._touch(sb)
        self.audit(actor, "sandbox.write_file", sb.id, path=p, bytes=len(data))
        return p

    @observe.traced("sandbox.list_files")  # --- observe
    async def list_files(self, ref: str, path: str = ".", depth: int = 1) -> str:
        sb, prov = self._running(ref)
        self.touch(sb.id)
        p = self._abs(sb, path)
        res = await prov.exec(
            sb, f"find {shlex.quote(p)} -mindepth 1 -maxdepth {int(depth)} -not -path '*/.git/*' "
                f"-printf '%y %10s %P\\n' 2>/dev/null | sort -k3 | head -500", timeout=30)
        return res.stdout

    def _abs(self, sb: Sandbox, path: str) -> str:
        if path.startswith("/"):
            return path
        if path.startswith("~/"):
            return f"/home/{self.settings.sandbox_user}/{path[2:]}"
        base = sb.info.get("repo_path") or self.settings.workspace_dir
        return f"{base}/{path}" if path not in (".", "") else base

    # ================================================================== terminals (tmux)
    async def terminal_open(self, ref: str, session: str = "main", command: str | None = None,
                            cwd: str | None = None) -> dict[str, Any]:
        sb, prov = self._running(ref)
        s = shlex.quote(_slug(session))
        wd = shlex.quote(cwd or sb.info.get("repo_path") or self.settings.workspace_dir)
        cmd = f" {shlex.quote(command)}" if command else ""
        res = await prov.exec(
            sb, f"tmux has-session -t {s} 2>/dev/null || tmux new-session -d -s {s} -x 200 -y 50 -c {wd}{cmd}",
            timeout=20)
        if res.exit_code != 0:
            raise FleetError(f"tmux failed: {res.stderr}")
        self._touch(sb)
        return {"sandbox": sb.id, "session": _slug(session)}

    async def terminal_list(self, ref: str) -> list[str]:
        sb, prov = self._running(ref)
        res = await prov.exec(sb, "tmux list-sessions -F '#{session_name}' 2>/dev/null || true", timeout=15)
        return [s for s in res.stdout.split() if s]

    async def terminal_read(self, ref: str, session: str = "main", lines: int = 200) -> str:
        sb, prov = self._running(ref)
        self.touch(sb.id)  # someone watching a terminal is using the sandbox
        capture = f"capture-pane -p -J -t {shlex.quote(_slug(session))} -S -{int(lines)}"
        if isolation.run_split(sb, _slug(session)):  # --- isolation: a split run's terminal is hangar's user's
            res = await isolation.sh(prov, sb, f"{isolation.TMUX} {capture}", timeout=15)
        else:
            res = await prov.exec(sb, f"tmux {capture}", timeout=15)
        if res.exit_code != 0:
            raise FleetError(f"no terminal session '{session}' in {sb.id} (open one with terminal_open)")
        return res.stdout.rstrip("\n")

    async def terminal_send(self, ref: str, session: str = "main", text: str = "", enter: bool = True,
                            keys: list[str] | None = None, wait_ms: int = 1500, wait_for: str | None = None,
                            lines: int = 60, actor: str = "system") -> str:
        sb, prov = self._running(ref)
        s = shlex.quote(_slug(session))
        parts = []
        if text:
            parts.append(f"tmux send-keys -t {s} -l -- {shlex.quote(text)}")
        for k in keys or []:
            parts.append(f"tmux send-keys -t {s} {shlex.quote(k)}")
        if enter:
            parts.append(f"tmux send-keys -t {s} Enter")
        if parts:
            res = await prov.exec(sb, " && ".join(parts), timeout=15)
            if res.exit_code != 0:
                raise FleetError(f"terminal_send failed: {res.stderr or 'no such session'}")
        self.audit(actor, "sandbox.terminal_send", sb.id, session=session, text=text[:300], keys=keys or [])
        deadline = time.monotonic() + max(wait_ms, 0) / 1000
        pattern = re.compile(wait_for, re.MULTILINE) if wait_for else None
        out = ""
        delay = 0.05  # perf: most prompts are back within ~100 ms; look early, then back off to the old 0.3 s
        while True:
            await asyncio.sleep(delay if pattern else max(wait_ms, 0) / 1000)
            delay = min(0.3, delay * 2)
            out = await self.terminal_read(sb.id, session, lines)
            if not pattern or pattern.search(out) or time.monotonic() >= deadline:
                break
        self._touch(sb)
        return out

    async def terminal_kill(self, ref: str, session: str) -> None:
        sb, prov = self._running(ref)
        await prov.exec(sb, f"tmux kill-session -t {shlex.quote(_slug(session))} 2>/dev/null || true", timeout=15)

    async def attach(self, ref: str, session: str | None = None, cols: int = 120, rows: int = 32):
        """Live PTY for the dashboard. Attaching to a tmux session lets you watch the same
        terminal an MCP client or agent is driving."""
        sb, prov = self._running(ref)
        if session and (watch := isolation.attach_command(sb, session)) is not None:  # --- isolation: a split run
            self._touch(sb)
            term = await isolation.open_terminal(prov, sb, watch, cols, rows)
            self.attached[sb.id] += 1
            return term
        if session:
            await self.terminal_open(sb.id, session)
            cmd = ["tmux", "attach-session", "-t", _slug(session)]
        else:
            wd = sb.info.get("repo_path") or self.settings.workspace_dir
            cmd = ["bash", "-lc", f"cd {shlex.quote(wd)} 2>/dev/null; exec bash -l"]
        self._touch(sb)
        term = await prov.open_terminal(sb, cmd, cols, rows)
        self.attached[sb.id] += 1
        return term

    # ================================================================== agent runs
    async def run_agent(self, ref: str, prompt: str, harness: str | None = None, cwd: str | None = None,
                        extra_args: str = "", model: str | None = None, actor: str = "system",
                        env: dict[str, str] | None = None, run_id: str | None = None,
                        command: str | None = None) -> dict[str, Any]:
        """`run_id` and `command` (workstream steer): a run id chosen by the caller, and a command that replaces the
        harness's one-shot `run` (a session's live turn runs the harness under hangar's driver)."""
        sb, prov = self._running(ref)
        spec = self.catalog.get(sb.environment)
        selected = sb.info.get("harness")
        if harness and selected and harness != selected:
            raise FleetError(f"sandbox {sb.name} was created for {selected}; its credentials and config are for "
                             f"that harness. Create a sandbox with harness={harness} instead.")
        try:
            hname = spec.pick_harness(harness or selected)
        except ValueError as e:
            raise FleetError(str(e)) from e
        if not hname:
            raise FleetError(f"environment {sb.environment} has no agent harness enabled")
        h = get_harness(hname)
        models = modelpolicy.of(self)  # --- canary: a policy is a concrete id by the time a run starts
        model = models.concrete(hname, model) or (sb.info.get("model") if hname == selected else None) \
            or models.concrete(hname, spec.model_for(hname))
        if run_id is not None and not re.fullmatch(r"run-[0-9a-f]{8}", run_id):
            raise FleetError(f"invalid run id '{run_id}'")
        run_id = run_id or new_id("run")
        home = f"/home/{self.settings.sandbox_user}"
        split = await isolation.ensure(self, prov, sb)  # --- isolation: the run as hangar's user, the agent as its own
        rdir = isolation.runs_dir(self.settings, split)
        wd = cwd or sb.info.get("repo_path") or self.settings.workspace_dir
        env = {**observe.run_env(self.settings, sb, run_id, hname), **(env or {})}  # --- observe: TRACEPARENT, OTel
        exports = (
            f"export HANGAR_PROMPT_FILE={rdir}/{run_id}.prompt HANGAR_RUN_ID={run_id}\n"
            + (f"export HANGAR_MODEL={shlex.quote(model)}\n" if model else "")
            + "".join(f"export {k}={shlex.quote(v)}\n" for k, v in (env or {}).items() if _ENV_NAME.fullmatch(k)))
        if split:
            res = await isolation.start(prov, sb, self.settings, run_id, prompt, exports, command or
                                        isolation.agent_command(self.settings, run_id, wd, h.run + " " + extra_args))
        else:
            await prov.write_file(sb, f"{rdir}/{run_id}.prompt", prompt.encode(), mode=0o600)
            script = (
                "#!/usr/bin/env bash\n"
                "set -a; [ -f ~/.hangar/env ] && . ~/.hangar/env; set +a\n"
                + exports +
                f"cd {shlex.quote(wd)}\n"
                f"{{ {command or h.run + ' ' + extra_args}; }} 2>&1 | tee {rdir}/{run_id}.log\n"
                f"echo ${{PIPESTATUS[0]}} > {rdir}/{run_id}.exit\n"
            )
            await prov.write_file(sb, f"{rdir}/{run_id}.sh", script.encode(), mode=0o700)
            res = await prov.exec(sb, f"tmux new-session -d -s {run_id} -x 200 -y 50 -c {shlex.quote(wd)} "
                                      f"'bash {rdir}/{run_id}.sh; exec bash -l'", timeout=20)
        if res.exit_code != 0:
            raise FleetError(f"failed to start agent: {res.stderr or res.stdout}")
        runs = sb.info.setdefault("runs", [])
        hver = sb.info.get("harness_version") if hname == selected else None  # --- images: what the pack installed
        runs.append({"id": run_id, "harness": hname, "model": model, "started_at": now(), "prompt": prompt[:200],
                     **({"split": True} if split else {}), **({"harness_version": hver} if hver else {})})
        self._touch(sb)
        self.audit(actor, "agent.run", sb.id, run=run_id, harness=hname, model=model, prompt=prompt[:300])
        return {"sandbox": sb.id, "run_id": run_id, "harness": hname, "model": model, "terminal_session": run_id,
                "log": f"{home}/.hangar/runs/{run_id}.log",  # --- isolation: a link to it under the split
                "harness_version": hver}  # --- images

    async def agent_status(self, ref: str, run_id: str, tail_lines: int = 60, quiet: bool = False,
                           wait_seconds: float = 0, grow_from: int | None = None) -> dict[str, Any]:
        """`wait_seconds` (perf): first wait in the sandbox, up to that long (max 60), for the run to finish, or
        with `grow_from` for its log to pass that many bytes. One round trip that answers within ~0.2 s of the
        change, where a caller sleeping between polls noticed it half a poll interval late on average. When every
        wait slot is taken, it sleeps here instead and then looks once."""
        sb, prov = self._running(ref)
        if not quiet:
            self.touch(sb.id)  # a client following an agent counts as use; hangar's own polling doesn't
        if not re.fullmatch(r"run-[0-9a-f]{8}", run_id):
            raise FleetError(f"invalid run id '{run_id}'")
        # --- harden: a split run's status and log are read where hangar's user wrote them, as that user: the agent's
        # ~/.hangar/runs links (and the commands on its PATH) are the agent's to point anywhere
        split = isolation.run_split(sb, run_id)
        rdir = isolation.runs_dir(self.settings, split)
        wait_seconds = 0.0 if self.closing.is_set() else max(0.0, min(60.0, float(wait_seconds)))
        if self._waiters is None:
            self._waiters = asyncio.Semaphore(max(1, self.settings.io_pool // 2))
        if wait_seconds and self._waiters.locked():
            with contextlib.suppress(TimeoutError):  # --- overnight/sweep: a shutdown ends the sleep
                await asyncio.wait_for(self.closing.wait(), wait_seconds)
            wait_seconds = 0.0
        wait = ""
        wake = f"{rdir}/.wake-{run_id}-{secrets.token_hex(4)}"
        if wait_seconds:
            # --- isolation: -L, the log of a split run is a link (isolation.py)
            grew = (f" || [ \"$(stat -L -c %s {rdir}/{run_id}.log 2>/dev/null || echo 0)\" -gt {int(grow_from)} ]"
                    if grow_from is not None else "")
            wait = (f"for i in $(seq {max(1, round(wait_seconds * 5))}); do {{ [ -s {rdir}/{run_id}.exit ]{grew} "
                    f"|| [ -e {wake} ]; }} && break; sleep 0.2; done; rm -f {wake}; ")
        cmd = (f"{wait}cat {rdir}/{run_id}.exit 2>/dev/null || echo RUNNING; echo '---'; "
               f"tail -n {int(tail_lines)} {rdir}/{run_id}.log 2>/dev/null")
        async with self._waiters if wait_seconds else contextlib.nullcontext():
            if wait_seconds:
                res = await self._wakeable(prov, sb, cmd, 20 + int(wait_seconds), wake, split)
            elif split:
                res = await isolation.sh(prov, sb, cmd, timeout=20)
            else:
                res = await prov.exec(sb, cmd, timeout=20)
        head, sep, tail = res.stdout.partition("---\n")
        if not sep:  # review(perf): cut off (the sandbox went away mid-call): not a run that finished with -1
            raise FleetError(f"could not read the status of {run_id} (exit {res.exit_code}): "
                             f"{(res.stderr or res.stdout).strip()[-200:]}")
        status = head.strip()
        tail = _clip_tail(tail)
        if status in ("RUNNING", ""):  # "": the exit file exists but is still being written
            return {"run_id": run_id, "state": "running", "exit_code": None, "output_tail": tail}
        return {"run_id": run_id, "state": "finished",
                "exit_code": int(status) if re.fullmatch(r"-?\d+", status) else -1, "output_tail": tail}

    async def _wakeable(self, prov: Provider, sb: Sandbox, cmd: str, timeout: int, wake: str,
                        split: bool = False) -> ExecResult:
        """A status command that waits in the sandbox. On shutdown, `wake` is created so the wait ends and the
        status comes back now (--- overnight/sweep: a 25 s MCP long-poll held pod shutdown that long). `split`: as
        hangar's user (--- harden)."""
        def run(c: str, t: int) -> Any:
            return isolation.sh(prov, sb, c, timeout=t) if split else prov.exec(sb, c, timeout=t)

        call = asyncio.ensure_future(run(cmd, timeout))
        closing = asyncio.ensure_future(self.closing.wait())
        try:
            await asyncio.wait({call, closing}, return_when=asyncio.FIRST_COMPLETED)
            if not call.done():
                with contextlib.suppress(Exception):
                    await run(f"touch {wake}", 10)
            return await call
        finally:
            closing.cancel()
            call.cancel()  # only does something when this was cancelled itself

    # ================================================================== snapshots / forks
    @observe.traced("sandbox.snapshot")  # --- observe
    async def snapshot(self, ref: str, label: str | None = None, kind: str = "auto",
                       actor: str = "system") -> Snapshot:
        sb, prov = self._running(ref)
        snap_id = new_id("snap")
        use_full = kind == "full" or (kind == "auto" and prov.capabilities.full_snapshots)
        if use_full:
            try:
                ref_ = await prov.snapshot(sb, snap_id)
                snap = Snapshot(id=snap_id, sandbox_id=sb.id, environment=sb.environment, provider=sb.provider,
                                kind=SnapshotKind.FULL, label=label, ref=ref_)
            except NotImplementedError as e:
                if kind == "full":
                    raise FleetError(f"{sb.provider} provider has no full snapshots; use kind=workspace") from e
                use_full = False
        if not use_full:
            snap = await self._workspace_snapshot(prov, sb, snap_id, label)
        snap.person = sb.person  # --- identity
        self.store.put(snap)
        self.audit(actor, "sandbox.snapshot", sb.id, snapshot=snap.id, kind=snap.kind.value)
        return snap

    async def _workspace_snapshot(self, prov: Provider, sb: Sandbox, snap_id: str, label: str | None) -> Snapshot:
        tmp = f"/tmp/{snap_id}.tgz"
        # --- scale: tar prints the archive's size too, so an oversized one is refused before hangar reads any of it
        res = await prov.exec(sb, f"tar czf {tmp} -C {self.settings.workspace_dir} .; rc=$?; "
                                  f"echo; stat -c %s {tmp} 2>/dev/null; exit $rc", timeout=900)
        if res.exit_code not in (0, 1):  # 1 = files changed while reading, still usable
            raise FleetError(f"workspace snapshot failed: {res.stderr[-500:]}")
        cap = self.settings.snapshot_max_mb * 1024 * 1024  # --- overnight/sweep: the sandbox decides its size
        size = int(last) if (last := (res.stdout.strip().splitlines() or ["0"])[-1]).isdigit() else cap
        if size > cap:
            await prov.exec(sb, f"rm -f {tmp}", timeout=30)
            raise FleetError(f"the workspace archive is over HANGAR_SNAPSHOT_MAX_MB ({self.settings.snapshot_max_mb} "
                             "MB); remove build outputs from the workspace, or use kind=full where the provider "
                             "has it")
        async with self.archive_memory.hold(size * ARCHIVE_READ_COST):  # --- scale: B12, bounded memory
            data = await prov.read_file(sb, tmp, limit=cap)
            await prov.exec(sb, f"rm -f {tmp}", timeout=30)
            if data is None:
                raise FleetError("workspace snapshot produced no archive")
            if len(data) > cap:
                raise FleetError(f"the workspace archive is over HANGAR_SNAPSHOT_MAX_MB "
                                 f"({self.settings.snapshot_max_mb} MB); remove build outputs from the workspace, or "
                                 "use kind=full where the provider has it")
            path = self.settings.snapshot_dir / f"{snap_id}.tgz"
            await asyncio.to_thread(path.write_bytes, data)  # --- scale: not on the event loop
        return Snapshot(id=snap_id, sandbox_id=sb.id, environment=sb.environment, provider=sb.provider,
                        kind=SnapshotKind.WORKSPACE, label=label, ref=str(path), size_bytes=len(data))

    async def _restore_workspace(self, prov: Provider, sb: Sandbox, archive: str) -> None:
        from pathlib import Path

        # --- scale: B12. Each fork child holds the archive while it uploads; the budget bounds how many at once.
        async with self.archive_memory.hold(Path(archive).stat().st_size * ARCHIVE_WRITE_COST):
            data = await asyncio.to_thread(Path(archive).read_bytes)
            tmp = f"/tmp/restore-{sb.id}.tgz"
            await prov.write_file(sb, tmp, data)
            del data
        res = await prov.exec(sb, f"tar xzf {tmp} -C {self.settings.workspace_dir} && rm -f {tmp}", timeout=900)
        if res.exit_code != 0:
            raise FleetError(f"workspace restore failed: {res.stderr[-500:]}")

    def snapshots(self, sandbox: str | None = None) -> list[Snapshot]:
        snaps = self.store.list(Snapshot)
        if sandbox:
            snaps = [s for s in snaps if s.sandbox_id == sandbox]
        return sorted(snaps, key=lambda s: s.created_at, reverse=True)

    async def delete_snapshot(self, snap_id: str, actor: str = "system") -> None:
        snap = self.store.get(Snapshot, snap_id)
        if not snap:
            raise FleetError(f"no snapshot {snap_id}")
        if snap.kind == SnapshotKind.WORKSPACE:
            from pathlib import Path

            Path(snap.ref).unlink(missing_ok=True)
        elif snap.provider in self.providers:
            await self.providers[snap.provider].delete_snapshot(snap.ref)
        self.store.delete(Snapshot, snap_id)
        self.audit(actor, "snapshot.delete", snap_id)

    async def fork(self, source: str, count: int = 1, name_prefix: str | None = None,
                   credentials: list[str] | None = None, work_branch_prefix: str | None = None,
                   provider: str | None = None, ttl_minutes: int | None = None,
                   labels: dict[str, str] | None = None, actor: str = "system") -> list[Sandbox]:
        """Clone a live sandbox (snapshotting it first) or an existing snapshot into N new sandboxes."""
        if count < 1 or count > 20:
            raise FleetError("count must be between 1 and 20")
        snap = self.store.get(Snapshot, source)
        if snap is None:
            snap = await self.snapshot(source, label="fork", actor=actor)
        parent = self.store.get(Sandbox, snap.sandbox_id)
        if credentials is None and parent:
            # Inherit everything except exclusive creds, which stay with the parent.
            credentials = [c for c in parent.credentials if not self.broker.get(c).exclusive]
        pname = provider or snap.provider
        prefix = _slug(name_prefix or (parent.name if parent else snap.environment))
        same_provider = pname == snap.provider and snap.kind == SnapshotKind.FULL

        async def _one(i: int) -> Sandbox:
            sb = await self.create(
                environment=snap.environment,
                name=f"{prefix}-f{i + 1}-{new_id('x')[2:6]}",
                repo=None if same_provider or snap.kind == SnapshotKind.WORKSPACE
                else (parent.repo if parent else None),
                credentials=credentials or [],
                provider=pname,
                ttl_minutes=ttl_minutes,
                labels={**(parent.labels if parent else {}), **(labels or {}), "fork-of": snap.sandbox_id},
                harness=parent.info.get("harness") if parent else None,
                actor=actor,
                _image=snap.ref if same_provider else None,
                _parent=snap.id,
                _restore_workspace=snap.ref if snap.kind == SnapshotKind.WORKSPACE else None,
                _skip_repo=same_provider or snap.kind == SnapshotKind.WORKSPACE,
                push_branch=f"{work_branch_prefix}-{i + 1}" if work_branch_prefix else None,  # workstream F
                person=parent.person if parent else snap.person,  # --- identity: a fork is its source's person's
            )
            if parent and parent.info.get("repo_path"):
                sb.info["repo_path"] = parent.info["repo_path"]
                self.store.put(sb)
            if work_branch_prefix and sb.info.get("repo_path"):
                await self.exec(sb.id, f"git checkout -q -b {shlex.quote(f'{work_branch_prefix}-{i + 1}')}",
                                actor=actor)
                sb.info["work_branch"] = f"{work_branch_prefix}-{i + 1}"
                self.store.put(sb)
            return sb

        results = await asyncio.gather(*(_one(i) for i in range(count)), return_exceptions=True)
        ok = [r for r in results if isinstance(r, Sandbox)]
        errs = [str(r) for r in results if isinstance(r, BaseException)]
        if not ok:
            raise FleetError("all forks failed: " + "; ".join(errs))
        if errs:
            log.warning("some forks failed: %s", errs)
        return ok

    # ================================================================== logins
    async def start_login(self, harness: str, credential_name: str, environment: str | None = None,
                          provider: str | None = None, actor: str = "system") -> dict[str, Any]:
        h = get_harness(harness)
        if not h.logins:
            raise FleetError(f"{harness} has no interactive login flow; add an API key instead")
        flow = h.logins[0]
        env = environment or next(
            (e for e in self.catalog.names() if harness in self.catalog.get(e).harnesses), None)
        if not env:
            raise FleetError(f"no environment enables {harness}; add it to one's harnesses to host the login")
        sb = await self.create(env, name=f"login-{harness}", credentials=[], provider=provider, ttl_minutes=30,
                               _skip_repo=True, harness=harness,
                               labels={"purpose": "login"}, actor=actor)
        await self.terminal_open(sb.id, "login", cwd=f"/home/{self.settings.sandbox_user}")
        await self.terminal_send(sb.id, "login", flow.command, wait_ms=500, actor=actor)
        login_id = new_id("login")
        self.logins[login_id] = {"id": login_id, "harness": harness, "credential": credential_name,
                                 "sandbox": sb.id, "session": "login", "flow": flow.model_dump(mode="json")}
        self.audit(actor, "credential.login_start", credential_name, harness=harness, sandbox=sb.id)
        return {**self.logins[login_id], "instructions": flow.notes or "Complete the login in the terminal."}

    async def complete_login(self, login_id: str, actor: str = "system", person: str | None = None) -> Credential:
        lg = self.logins.get(login_id)
        if not lg:
            raise FleetError(f"no login session {login_id}")
        h = get_harness(lg["harness"])
        flow = h.logins[0]
        value: str | bytes | None = None
        kind = flow.captures
        if flow.token_regex:
            out = await self.terminal_read(lg["sandbox"], lg["session"], lines=2000)
            m = re.findall(flow.token_regex + r"(?![0-9A-Za-z_\-])", out)
            value = m[-1] if m else None
        elif flow.capture_file:
            value = await self.read_file(lg["sandbox"], f"~/{flow.capture_file}", limit=AUTH_FILE_LIMIT)
            if value is not None and len(value) > AUTH_FILE_LIMIT:  # --- overnight/sweep: not a login file
                raise FleetError(f"~/{flow.capture_file} is over {AUTH_FILE_LIMIT // 1024} KB, which no login "
                                 "writes; not storing it")
        if not value:
            raise FleetError("login not finished yet: no token/credential file found. Finish the flow in the "
                             "terminal, then complete again.")
        cred = self.broker.put(lg["credential"], lg["harness"], kind, value,
                               description=f"{h.title} login captured {time.strftime('%Y-%m-%d')}",
                               person=person)  # --- teamcreds: the login is its person's own, not the team's
        from hangar import credhealth  # --- clouddoctor: checked where it was made (codex login status)

        await credhealth.on_capture(self, cred, lg["sandbox"])
        await self.destroy(lg["sandbox"], actor=actor)
        self.logins.pop(login_id, None)
        self.audit(actor, "credential.login_complete", cred.name, harness=lg["harness"])
        return cred

    # ================================================================== housekeeping
    async def gc(self, delete: bool = False, actor: str = "system") -> list[dict[str, str]]:
        """Find (and optionally remove) provider resources no active sandbox in this store owns.

        Dry-run by default: if several hangar instances share one Docker engine, each sees the
        others' sandboxes as orphans, so review the list before deleting.
        """
        settled = (SandboxState.STOPPED, SandboxState.ERROR)
        live = [s for s in self.store.list(Sandbox) if s.state not in settled]  # incl. pending/stopping
        known = {s.provider_ref for s in live if s.provider_ref} | {f"name:hangar-{s.id}" for s in live}
        found: list[dict[str, str]] = []
        for pname, prov in self.providers.items():
            if not hasattr(prov, "orphans"):
                continue
            for o in await prov.orphans(known):  # type: ignore[attr-defined]
                found.append({**o, "provider": pname})
                if delete:
                    await prov.remove_ref(o["ref"])  # type: ignore[attr-defined]
        if delete and found:
            self.audit(actor, "fleet.gc", None, removed=[o["name"] for o in found])
        return found

    def _mark_gone(self, sb: Sandbox, reason: str) -> None:
        sb.state = SandboxState.STOPPED
        sb.error = reason
        self.store.put(sb)
        self.gateway.revoke(sb.id)  # workstream F
        harden.forget(self, sb.id)  # --- harden
        for c in self.store.list(Credential):
            if c.leased_to == sb.id:  # the auth file is gone with the sandbox; keep the vault copy
                c.leased_to = None
                self.store.put(c)
        self.audit("reaper", "sandbox.gone", sb.id, reason=reason)

    async def reconcile(self) -> None:
        """One reconciler pass (reconciler.py): sandboxes whose backing container/pod/VM died, vanished or was
        replaced are marked stopped, and rows a dead task left pending, starting or stopping are finished."""
        await self.reconciler.run()  # --- reconciler

    async def _reaper_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self.settings.reaper_interval_seconds)
                t = now()
                expired = [sb for sb in self.list() if sb.expires_at and sb.expires_at < t]
                for sb in expired:
                    log.info("expiring %s", sb.id)
                # concurrently: a destroy may first checkpoint a session (pre-destroy hook), which takes a while
                await asyncio.gather(*(self.destroy(sb.id, actor="reaper") for sb in expired),
                                     return_exceptions=True)
                await self.sync_scheduled_pools()  # --- reconciler: reconcile runs on its own task now
                await envservices.sweep(self)  # --- envbuilds: services whose sandbox is gone (Docker)
                await k8sgc.sweep(self)  # --- durable: build and services templates nothing uses (Kubernetes)
                metrics.loop_tick("reaper")  # --- reconciler: heartbeat
            except asyncio.CancelledError:
                return
            except Exception:  # noqa: BLE001
                log.exception("reaper iteration failed")
                metrics.loop_failed("reaper")  # --- reconciler

    def overview(self) -> dict[str, Any]:
        sbs = self.list()
        return {
            "sandboxes": {st.value: sum(1 for s in sbs if s.state == st) for st in ACTIVE},
            "total_active": len(sbs),
            "max_sandboxes": self.settings.max_sandboxes,
            "providers": {
                name: {"ok": True, "capabilities": p.capabilities.model_dump()} for name, p in self.providers.items()
            } | {name: {"ok": False, "error": err} for name, err in self.provider_errors.items()},
            "default_provider": self.settings.default_provider,
            "environments": len(self.catalog.names()),
            "credentials": len(self.broker.list()),
            "harnesses": {k: {"title": v.title, "agent": v.agent, "version": v.version or "latest",
                              "deprecated": v.deprecated} for k, v in HARNESSES.items()},
        } | self.power.overview() | opstatus.overview_fields(self)  # --- ops; --- clouddoctor: failures, credentials


__all__ = ["Fleet", "FleetError", "CredentialError", "CredentialKind"]
