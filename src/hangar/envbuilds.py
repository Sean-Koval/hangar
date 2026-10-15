"""Versioned environment builds (--- envbuilds; docs/environments.md).

A repo that has a `.hangar/environment.yaml` (repoenv.py) with an `install` gets builds: the environment's image, then
the clone, then `install`, captured as an image. Sandboxes for that repo start from the repo's active build, so the
agent finds its dependencies installed instead of spending its first minutes (or its whole turn) installing them.

    build = base image (the environment's) + clone (the default branch) + install, as an image per provider

* **Where.** `install` is code from whoever can push to the repo, so it runs where agents run: in a sandbox of the
  environment (gVisor on GKE, hangar's network on Docker), never on the control plane or a builder. Only the
  checkout is kept (a tar of it, `ADD`ed onto the environment's image with no `RUN`), so nothing in $HOME (the
  sandbox's credentials and env) can end up in a build.
* **Keyed** by (repo, environment, provider, config digest, base image). The digest covers `install`, the build
  secret ids and the lockfiles' git blob ids (repoenv.digest), so a comment or a new terminal doesn't rebuild.
* **Never on the request path.** A claim only reads what is recorded. After the clone, one exec reads the repo's
  config and lockfiles; when they (or the environment's image) moved on from the active build, a rebuild starts in the
  background for the next sandbox. Startup's prebuild (#83) rebuilds the builds an environment change made stale.
* **A failed build never replaces the active one.** The sandbox gets the last good build, or the environment's own
  image with a warning in the agent's instructions and the job's events (`sb.info["env_build"]`).
* **History.** The last HANGAR_ENV_BUILDS_KEEP builds per repo, environment and provider (`hangar env builds`); an admin
  can roll back to one, which pins it until unpinned (audited).
* **Build secrets** (ids the repo names, credentials an admin maps; hangar.buildsecrets) reach the install command's
  environment only, are checked for in the checkout before it is captured, and never reach a running sandbox.

Records are small JSON documents in the store's kv table: `envbuild:<id>`, one head per repo, environment and provider
(`envbuild-head:...`: the active build, the history, the pin), and what the last clone of the default branch saw
(`envbuild-seen:<env>:<repo>`), which jobs use for the file's `extends` and `verify` before any clone.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import posixpath
import secrets as pysecrets
import shlex
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field, ValidationError

from hangar import repoenv, supervisor
from hangar.envspec import BuildContext, EnvSpec
from hangar.models import Sandbox, new_id, now
from hangar.repopolicy import repo_key

if TYPE_CHECKING:
    from hangar.fleet import Fleet
    from hangar.providers.base import Provider

log = logging.getLogger("hangar.envbuilds")

KV_BUILD = "envbuild:"
KV_HEAD = "envbuild-head:"
KV_SEEN = "envbuild-seen:"
LABEL = "hangar.build"  # on a build's own sandbox: it never uses (or schedules) builds itself
PROVIDERS = ("docker", "kubernetes")  # where a build can be captured as an image
LOG_TAIL = 16_000
FAILURES_MAX = 20
RETRY_AFTER = 1800  # seconds before a build that failed before reading the repo's config is tried again by itself


class EnvBuildError(RuntimeError):
    pass


class Build(BaseModel):
    id: str = Field(default_factory=lambda: new_id("bld"))
    seq: int = 0  # the version people see: v<seq>, per repo, environment and provider
    repo: str  # repo_key
    repo_url: str
    environment: str
    provider: str
    state: Literal["queued", "running", "succeeded", "failed", "interrupted"] = "queued"
    stage: str | None = None  # while running: cloning, installing, capturing, building the image
    reason: str = "manual"  # first | drift | base | manual
    key: str | None = None  # repoenv digest of the build's config and lockfiles
    base: str | None = None  # the environment image it was built on
    commit: str | None = None
    path: str | None = None  # where the checkout is in the image
    image: str | None = None
    size_bytes: int | None = None
    config: dict[str, Any] | None = None  # the repo's config at that commit
    error: str | None = None
    log: str = ""  # the end of the install's output, redacted
    actor: str = "system"
    created_at: float = Field(default_factory=now)
    started_at: float | None = None
    finished_at: float | None = None

    @property
    def version(self) -> str:
        return f"v{self.seq}"


class Head(BaseModel):
    """A repo's builds of one environment on one provider: the newest first, the active one, and a rollback's pin."""

    active: str | None = None
    ids: list[str] = Field(default_factory=list)
    seq: int = 0
    pinned: bool = False
    pinned_by: str | None = None
    pinned_at: float | None = None


class Seen(BaseModel):
    """What the last clone of the repo's default branch found (any provider)."""

    commit: str | None = None
    present: bool = False
    config: dict[str, Any] | None = None
    error: str | None = None
    digest: str | None = None
    at: float = Field(default_factory=now)


class Pick(BaseModel):
    """What `pick` decided for a sandbox about to be created."""

    image: str | None = None  # the build's image, or None: the environment's own
    build: Build | None = None


def _head_key(pname: str, env: str, repo: str) -> str:
    return f"{KV_HEAD}{pname}:{env}:{repo}"


def _ago(ts: float | None) -> str:
    if not ts:
        return "?"
    s = int(now() - ts)
    return f"{s // 60} min ago" if s < 7200 else f"{s // 3600} h ago" if s < 172800 else f"{s // 86400} days ago"


class EnvBuilds:
    """The builds of one hangar. `of(fleet)` gives the fleet's."""

    def __init__(self, fleet: Fleet):
        self.fleet = fleet
        self.store = fleet.store
        self.settings = fleet.settings
        self._tasks: dict[str, asyncio.Task] = {}  # head key -> the build running for it
        self._sem: asyncio.Semaphore | None = None

    # ================================================================== records
    def get(self, build_id: str) -> Build:
        raw = self.store.kv_get(KV_BUILD + build_id)
        if raw is None:
            raise KeyError(f"no build '{build_id}'")
        return Build.model_validate_json(raw)

    def _put(self, b: Build) -> None:
        self.store.kv_set(KV_BUILD + b.id, b.model_dump_json())

    def head(self, pname: str, env: str, repo: str) -> Head:
        raw = self.store.kv_get(_head_key(pname, env, repo))
        return Head.model_validate_json(raw) if raw else Head()

    def _put_head(self, pname: str, env: str, repo: str, h: Head) -> None:
        self.store.kv_set(_head_key(pname, env, repo), h.model_dump_json(), durable=True)

    def history(self, pname: str, env: str, repo: str) -> list[Build]:
        out = []
        for i in self.head(pname, env, repo).ids:
            with contextlib.suppress(KeyError, ValidationError):
                out.append(self.get(i))
        return out

    def active(self, pname: str, env: str, repo: str) -> Build | None:
        h = self.head(pname, env, repo)
        if not h.active:
            return None
        with contextlib.suppress(KeyError, ValidationError):
            return self.get(h.active)
        return None

    def heads(self) -> list[dict[str, Any]]:
        """Every repo with builds: (provider, environment, repo) and its active build."""
        out = []
        for k in sorted(self.store.kv_keys(KV_HEAD)):
            pname, env, repo = k[len(KV_HEAD):].split(":", 2)
            h = self.head(pname, env, repo)
            act = self.active(pname, env, repo)
            latest = self.history(pname, env, repo)[:1]
            out.append({"repo": repo, "environment": env, "provider": pname, "pinned": h.pinned,
                        "active": _brief(act) if act else None, "latest": _brief(latest[0]) if latest else None})
        return out

    def seen(self, env: str, repo: str | None) -> Seen | None:
        key = repo_key(repo)
        raw = self.store.kv_get(f"{KV_SEEN}{env}:{key}") if key else None
        return Seen.model_validate_json(raw) if raw else None

    def seen_config(self, env: str | None, repo: str | None) -> repoenv.RepoEnvironment | None:
        """The repo's config as its default branch last had it, for decisions made before any clone."""
        if not env or not repo:
            return None
        s = self.seen(env, repo)
        if s is None or not s.config:
            return None
        try:
            return repoenv.RepoEnvironment.model_validate(s.config)
        except ValidationError:
            return None

    def newest_seen_config(self, repo: str | None, envs: list[str]) -> repoenv.RepoEnvironment | None:
        """The repo's config as the newest clone in one of `envs` saw it. Only those: what a sandbox recorded is as
        good as its environment (its `setup` and `install` run before the read, and members make environments)."""
        key = repo_key(repo)
        best: Seen | None = None
        for env in dict.fromkeys(envs) if key else ():
            with contextlib.suppress(ValidationError):
                raw = self.store.kv_get(f"{KV_SEEN}{env}:{key}")
                s = Seen.model_validate_json(raw) if raw else None
                if s and s.config and (best is None or s.at > best.at):
                    best = s
        if best is None:
            return None
        try:
            return repoenv.RepoEnvironment.model_validate(best.config)
        except ValidationError:
            return None

    def recover(self) -> int:
        """At startup: builds a restart cut short are `interrupted` (their sandboxes expire on their own TTL)."""
        n = 0
        for k in self.store.kv_keys(KV_BUILD):
            raw = self.store.kv_get(k)
            if not raw:
                continue
            with contextlib.suppress(ValidationError):
                b = Build.model_validate_json(raw)
                if b.state in ("queued", "running"):
                    b.state, b.stage, b.finished_at = "interrupted", None, now()
                    b.error = "the control plane restarted during the build; the next sandbox starts another"
                    self._put(b)
                    n += 1
        return n

    # ================================================================== on a sandbox's create
    def _supported(self, pname: str) -> str | None:
        """Why repo builds can't run on this provider, or None."""
        if not self.settings.env_builds:
            return "repo builds are off (HANGAR_ENV_BUILDS)"
        if pname not in PROVIDERS:
            return f"the {pname} provider doesn't run repo builds yet"
        return None

    def _base(self, spec: EnvSpec, pname: str) -> tuple[str, list[str]]:
        """The environment image a build of this environment starts from (the default harness's variant)."""
        if spec.image:
            return spec.image, []
        bake = self.fleet._bake_for(spec, pname, spec.pick_harness())
        return self.fleet._tag_for(spec, pname, bake)[0], bake

    async def pick(self, spec: EnvSpec, pname: str, harness: str | None, sb: Sandbox,
                   stage: Callable[[str], None] | None = None) -> Pick:
        """The image a new sandbox starts from: the repo's active build, when there is a good one for it. Anything
        unexpected here means the environment's own image, never a failed create."""
        try:
            return await self._pick(spec, pname, harness, sb, stage)
        except Exception as e:  # noqa: BLE001
            log.warning("could not pick a build for %s: %s", sb.id, e, exc_info=True)
            sb.info["env_build"] = self._info(None, fallback=True, reason=f"hangar couldn't look up the build: {e}")
            return Pick()

    async def _pick(self, spec: EnvSpec, pname: str, harness: str | None, sb: Sandbox,
                    stage: Callable[[str], None] | None) -> Pick:
        repo = repo_key(sb.repo)
        if not repo or LABEL in sb.labels or self._supported(pname):
            return Pick()
        b = self.active(pname, spec.name, repo)
        if b is None or not b.image:
            return Pick()
        if not spec.image and self.fleet._bake_for(spec, pname, harness) != self._base(spec, pname)[1]:
            sb.info["env_build"] = self._info(b, fallback=True, reason=(
                f"{harness} runs on its own image variant on this provider; builds are of the default harness's"))
            return Pick()
        prov = self.fleet.provider(pname)
        try:
            exists = await prov.image_exists(b.image)
        except Exception:  # noqa: BLE001 - can't tell: try it; a create that can't pull fails like any other
            exists = True
        if not exists:
            sb.info["env_build"] = self._info(b, fallback=True, reason=f"build {b.version}'s image {b.image} is gone")
            return Pick()
        if b.path and b.path != (want := checkout_path(self.settings, spec, sb)):
            sb.info["env_build"] = self._info(b, fallback=True, reason=(
                f"build {b.version} has the checkout at {b.path}, this sandbox clones to {want}"))
            return Pick()
        sb.info["env_build"] = self._info(b, fallback=False)
        if stage:
            stage(f"starting from {repo}'s environment build {b.version} ({(b.commit or '')[:9]}, "
                  f"built {_ago(b.finished_at)})")
        return Pick(image=b.image, build=b)

    def _info(self, b: Build | None, fallback: bool, reason: str | None = None, **extra: Any) -> dict[str, Any]:
        """sb.info["env_build"]: what the agent, `get_sandbox` and the job's events show."""
        d: dict[str, Any] = {"version": b.version if b else None, "id": b.id if b else None, "fallback": fallback,
                             "reason": reason, "built_at": b.finished_at if b else None,
                             "repo": b.repo if b else None, "commit": b.commit if b else None,
                             "config_digest": b.key if b else None, "image": None if fallback or not b else b.image,
                             "stale": False}
        d.update(extra)
        return d

    @staticmethod
    def provider_spec(spec: EnvSpec, sb: Sandbox) -> EnvSpec:
        """The spec the provider creates the sandbox from. A build's, or services other than the environment's, get
        one of their own: on Kubernetes a SandboxTemplate per build (one named after the environment would flip its
        warm pool between images) or per set of services (its sidecars), with no warm pool."""
        from hangar import envservices

        info = sb.info.get("env_build") or {}
        build = not info.get("fallback") and info.get("image") and info.get("id")
        return envservices.provider_spec(spec, sb, info["id"].removeprefix("bld-") if build else None)

    @staticmethod
    def prebuilt(sb: Sandbox) -> bool:
        """The sandbox's image already holds the repo's checkout (a build of it)."""
        info = sb.info.get("env_build") or {}
        return bool(info.get("image") and not info.get("fallback"))

    @staticmethod
    def checkout_script(sb: Sandbox, dest: str) -> str:
        """Bring a build's checkout to the branch the sandbox asked for (the default one when none): the build may
        be some commits behind. Untracked files `install` left (not ignored ones like node_modules) are removed, so
        the agent's diff is its own."""
        q = shlex.quote
        script = (f"cd {q(dest)} && git remote set-url origin {q(sb.repo or '')} && git fetch -q --prune origin && ")
        if sb.branch:
            script += f"git checkout -q -f -B {q(sb.branch)} {q('origin/' + sb.branch)}"
        else:
            script += ('b=$(git symbolic-ref -q --short refs/remotes/origin/HEAD) && '
                       'git checkout -q -f -B "${b#origin/}" "$b"')
        return script + " && git clean -fdq"

    # ================================================================== after the clone
    async def after_setup(self, prov: Provider, sb: Sandbox, spec: EnvSpec, harness: str | None,
                          stage: Callable[[str], None] | None = None) -> None:
        """Read the repo's config from the checkout, start what it declares under the supervisor, tell the agent
        about its environment, and schedule a rebuild when the build is behind. Never fails the create: problems are
        recorded (sb.info["setup_failures"]) and shown to the agent."""
        if not self.settings.env_builds or LABEL in sb.labels:
            return
        try:
            await self._after_setup(prov, sb, spec, harness, stage)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - the sandbox works without it; say why
            log.warning("environment setup of %s failed: %s", sb.id, e, exc_info=True)
            self.failure(sb, "environment", f"hangar couldn't set up the environment: {e}")

    def failure(self, sb: Sandbox, step: str, message: str) -> None:
        xs = sb.info.setdefault("setup_failures", [])
        xs.append({"ts": now(), "step": step, "message": self.fleet.broker.redact(message)[:2000]})
        del xs[:-FAILURES_MAX]

    async def _after_setup(self, prov: Provider, sb: Sandbox, spec: EnvSpec, harness: str | None,
                           stage: Callable[[str], None] | None) -> None:
        repo = repo_key(sb.repo)
        path = sb.info.get("repo_path")
        if not repo or not path:
            return
        res = await prov.exec(sb, repoenv.read_script(path) + "echo \"default $(git rev-parse -q --verify "
                              "origin/HEAD)\"\n", timeout=60)
        out = res.stdout
        default = next((ln[8:].strip() for ln in reversed(out.splitlines()) if ln.startswith("default ")), "")
        read = repoenv.parse_read("\n".join(ln for ln in out.split("\n") if not ln.startswith("default ")))
        if read.config is not None and read.config.lockfiles:  # its own lockfiles count too
            res = await prov.exec(sb, repoenv.read_script(path, read.config.lockfiles), timeout=60)
            read = repoenv.parse_read(res.stdout)
        on_default = bool(read.commit) and read.commit == default
        if on_default:
            self.store.kv_set(f"{KV_SEEN}{spec.name}:{repo}", Seen(
                commit=read.commit, present=read.present, error=read.error, digest=read.digest(),
                config=read.config.model_dump(mode="json", exclude_none=True) if read.config else None,
            ).model_dump_json())
        if read.error:
            self.failure(sb, "config", read.error)
        eff = spec
        if read.config is not None:
            try:
                eff = repoenv.apply(spec, read.config)
            except ValueError as e:
                self.failure(sb, "config", f"{repoenv.PATH}: {e}")
        pname = sb.provider
        info = sb.info.get("env_build")
        if eff.install:
            info = self._check_drift(sb, spec, pname, repo, read, on_default, info)
        elif info and not info.get("fallback"):
            info["stale"] = False
        if info is not None:
            sb.info["env_build"] = info
        running = {s["name"] for s in sb.info.get("services") or [] if s.get("kind") == "service"}
        named = {f["step"].removeprefix("service:") for f in sb.info.get("setup_failures") or []}
        if late := sorted(set(eff.services) - running - named):
            self.failure(sb, "services", f"{', '.join(late)}: not in the repo's config when this sandbox started (new, "
                         "or only on this branch); services start with the next sandbox")
        programs = bool(eff.start or eff.terminals or eff.services or eff.ports)
        if programs and (why := await supervisor.start(prov, sb, self.settings, eff, path)):
            self.failure(sb, "supervisor", why)
        if info is not None or programs or read.present:
            await supervisor.tell_agent(prov, sb, self.settings, harness, eff, path)
        if stage and info:
            stage(_event(info, eff))
        self.store.put(sb)

    def _check_drift(self, sb: Sandbox, spec: EnvSpec, pname: str, repo: str, read: repoenv.Read,
                     on_default: bool, info: dict[str, Any] | None) -> dict[str, Any]:
        """The sandbox's env_build info, and a rebuild in the background when the repo or the environment moved on.
        Only a checkout of the default branch (what builds are made from) can call for a first or a drift build; a
        new environment image calls for one whatever the sandbox's branch."""
        why = self._supported(pname)
        b = self.active(pname, spec.name, repo)
        digest = read.digest()
        base = self._base(spec, pname)[0] if why is None else None
        if why is None:
            if b is None or (on_default and b.key != digest):
                drift, key = ("first" if b is None else "drift"), digest
            else:
                drift, key = ("base" if b.base != base else None), b.key or ""
            if drift and (on_default or drift == "base") and self._should_build(pname, spec.name, repo, key, base):
                self.schedule(sb.repo or repo, spec.name, pname, reason=drift, actor=f"hangar:{sb.id}")
        building = self.running(pname, spec.name, repo)
        then = "; a rebuild is running" if building else ""
        if info is None or info.get("fallback") or b is None:  # the environment's own image
            latest = self.history(pname, spec.name, repo)[:1]
            if (info or {}).get("reason"):
                reason = info["reason"]  # type: ignore[index]
            elif why:
                reason = why
            elif latest and latest[0].state == "failed":
                reason = f"the latest build ({latest[0].version}) failed: {latest[0].error}"
            else:
                reason = "the repo has no build yet" + ("; the first one is running" if building else "")
            return self._info(None, fallback=True, reason=reason, repo=repo)
        if b.key != digest:
            info.update(stale=True, reason=(
                f"the repo's install or lockfiles changed since build {b.version}{then}" if on_default else
                f"this branch's install or lockfiles differ from build {b.version} (made from the default branch)"))
        elif b.base != base:
            info.update(stale=True, reason=f"the environment's image changed since build {b.version}{then}")
        return info

    def _should_build(self, pname: str, env: str, repo: str, digest: str, base: str | None) -> bool:
        """Not when one is running, nor when the latest build of this very config failed (it would again; a config
        or lockfile change, a new base or `hangar env build-repo` tries again), nor while an admin's rollback pins."""
        if self.running(pname, env, repo) or self.head(pname, env, repo).pinned:
            return False
        latest = self.history(pname, env, repo)[:1]
        if latest and latest[0].state == "failed" and latest[0].key is None:
            # --- review: it failed before reading the config (no room, a clone that failed): nothing says what
            # changed since, so not on every sandbox; RETRY_AFTER later, or `hangar env build-repo`.
            return now() - (latest[0].finished_at or 0) >= RETRY_AFTER
        return not (latest and latest[0].state == "failed" and latest[0].key == digest and latest[0].base == base)

    def running(self, pname: str, env: str, repo: str) -> bool:
        t = self._tasks.get(_head_key(pname, env, repo))
        return t is not None and not t.done()

    # ================================================================== building
    def schedule(self, repo_url: str, env: str, pname: str, reason: str = "manual", actor: str = "system") -> Build:
        """Start a build in the background (one at a time per repo, environment and provider) and return its record;
        the one already running when there is one."""
        rkey = repo_key(repo_url)
        if not rkey:
            raise EnvBuildError("a repo is required")
        if why := self._supported(pname):
            raise EnvBuildError(why)
        self.fleet.catalog.get(env)  # KeyError: no such environment
        hk = _head_key(pname, env, rkey)
        if self.running(pname, env, rkey):
            return self.history(pname, env, rkey)[0]
        h = self.head(pname, env, rkey)
        h.seq += 1
        b = Build(seq=h.seq, repo=rkey, repo_url=repo_url, environment=env, provider=pname, reason=reason,
                  actor=actor)
        self._put(b)
        h.ids.insert(0, b.id)
        dropped = self._trim(h)
        self._put_head(pname, env, rkey, h)
        self.fleet.audit(actor, "envbuild.start", rkey, build=b.id, environment=env, provider=pname, reason=reason)
        self._tasks[hk] = asyncio.create_task(self._run(b))
        for old in dropped:
            asyncio.create_task(self._forget(old))
        return b

    def _trim(self, h: Head) -> list[Build]:
        keep = self.settings.env_builds_keep
        out: list[Build] = []
        while len(h.ids) > keep:
            victim = next((i for i in reversed(h.ids) if i != h.active), None)
            if victim is None:
                break
            h.ids.remove(victim)
            with contextlib.suppress(KeyError, ValidationError):
                out.append(self.get(victim))
        return out

    async def _forget(self, b: Build) -> None:
        """A build out of the history: its record, its local image, and (Kubernetes) its sandbox template."""
        self.store.kv_delete(KV_BUILD + b.id)
        prov = self.fleet.providers.get(b.provider)
        if prov is None or not b.image:
            return
        try:
            if hasattr(prov, "delete_image"):
                await prov.delete_image(b.image)  # type: ignore[attr-defined]
            if b.provider == "kubernetes":
                await _delete_template(prov, f"hangar-{b.environment[:40]}-{b.id.removeprefix('bld-')}")
        except Exception:  # noqa: BLE001 - garbage; the registry's own cleanup policy is the backstop
            log.info("could not remove build %s's image or template", b.id, exc_info=True)

    async def _run(self, b: Build) -> None:
        if self._sem is None:
            self._sem = asyncio.Semaphore(self.settings.env_builds_concurrency)
        sb: Sandbox | None = None
        try:
            async with self._sem:
                b.state, b.started_at = "running", now()
                self._put(b)
                async with asyncio.timeout(self.settings.env_build_timeout_minutes * 60):
                    sb = await self._build(b)
            b.state = "succeeded"
        except asyncio.CancelledError:
            b.state, b.error = "interrupted", "the control plane stopped during the build"
            raise
        except Exception as e:  # noqa: BLE001 - recorded on the build; the active one stays
            b.state = "failed"
            b.error = self.fleet.broker.redact(
                str(e) if not isinstance(e, TimeoutError)
                else f"took longer than HANGAR_ENV_BUILD_TIMEOUT_MINUTES ({self.settings.env_build_timeout_minutes})")
            log.warning("build %s of %s (%s) failed: %s", b.id, b.repo, b.environment, b.error)
        finally:
            b.stage, b.finished_at = None, now()
            self._put(b)
            self.fleet.audit(b.actor, "envbuild.finish", b.repo, build=b.id, state=b.state, error=b.error)
            if b.state == "succeeded":
                self._promote(b)
            sid = sb.id if sb else self._build_sandbox(b)
            if sid:
                with contextlib.suppress(Exception):
                    await asyncio.shield(self.fleet.destroy(sid, actor=f"hangar:build:{b.id}"))

    def _build_sandbox(self, b: Build) -> str | None:
        """The build's sandbox when `_build` failed after creating it (the create itself cleans up after itself)."""
        for s in self.fleet.list(label=f"{LABEL}={b.id}"):
            return s.id
        return None

    def _promote(self, b: Build) -> None:
        h = self.head(b.provider, b.environment, b.repo)
        if h.pinned:
            b.error = f"built, not activated: an admin pinned {h.active} (`hangar env unpin`)"
            self._put(b)
            return
        h.active = b.id
        self._put_head(b.provider, b.environment, b.repo, h)

    def _stage(self, b: Build, what: str) -> None:
        b.stage = what
        self._put(b)

    async def _build(self, b: Build) -> Sandbox:
        f = self.fleet
        spec = f.catalog.get(b.environment)
        pname = b.provider
        prov = f.provider(pname)
        b.base, _ = self._base(spec, pname)
        self._stage(b, "starting a sandbox")
        branch = spec.repo.branch if spec.repo and repo_key(spec.repo.url) == b.repo else None
        # Only what the clone needs: never an agent's model credentials in a sandbox that runs the repo's code.
        creds = [c for c in spec.credentials if not f._is_agent(f._cred_harness(c))]
        sb = await f.create(b.environment, name=f"build-{b.id.removeprefix('bld-')}", repo=b.repo_url, branch=branch,
                            credentials=creds, provider=pname, labels={LABEL: b.id},
                            ttl_minutes=self.settings.env_build_timeout_minutes + 15, actor=f"hangar:build:{b.id}")
        path = b.path = sb.info.get("repo_path")
        if not path:
            raise EnvBuildError("the build's sandbox has no checkout")
        self._stage(b, "reading the repo's config")
        res = await prov.exec(sb, repoenv.read_script(path), timeout=60)
        read = repoenv.parse_read(res.stdout)
        if read.config is not None and read.config.lockfiles:
            read = repoenv.parse_read((await prov.exec(sb, repoenv.read_script(path, read.config.lockfiles),
                                                       timeout=60)).stdout)
        if read.error:
            raise EnvBuildError(read.error)
        b.commit, b.key = read.commit, read.digest()
        b.config = read.config.model_dump(mode="json", exclude_none=True) if read.config else None
        eff = repoenv.apply(spec, read.config)
        if not eff.install:
            raise EnvBuildError(f"nothing to build: neither {repoenv.PATH} nor environment {spec.name} has an install")
        values = await self._secrets(b, spec, read.config)
        self._stage(b, "installing")
        await self._install(b, prov, sb, path, eff.install, values)
        if values:
            await self._leak_scan(prov, sb, values, path)
        self._stage(b, "capturing the checkout")
        data = await self._capture(prov, sb, path)
        if values and (hits := await asyncio.to_thread(archive_leaks, data, values)):  # --- review: what's imaged
            raise EnvBuildError("install left build secret(s) in the checkout, which every sandbox would get: "
                                + "; ".join(hits) + ". Write them outside the repo, or remove them at the end of "
                                "install")
        b.size_bytes = len(data)
        self._stage(b, "building the image")
        b.image = await self._image(b, spec, prov, path, data)
        return sb

    async def _secrets(self, b: Build, spec: EnvSpec, cfg: repoenv.RepoEnvironment | None) -> dict[str, bytes]:
        """The build secret ids the repo names, resolved through hangar.buildsecrets (the environment's and the repo
        policy's mapping); values only for this build's install."""
        ids = cfg.build_secrets if cfg else []
        if not ids:
            return {}
        from hangar import buildsecrets
        from hangar.repopolicy import RepoPolicies

        mapping = buildsecrets.mapping(spec, RepoPolicies(self.store).get(b.repo_url))
        if missing := [i for i in ids if i not in mapping]:
            raise EnvBuildError(f"build secret(s) {', '.join(missing)} aren't mapped to a credential; an admin maps "
                                "them (build_secrets) on the environment or the repo's policy")
        return buildsecrets.resolve(self.fleet.broker, mapping, ids=ids)

    async def _install(self, b: Build, prov: Provider, sb: Sandbox, path: str, install: str,
                       values: dict[str, bytes]) -> None:
        q = shlex.quote
        text_values = {k: v.decode(errors="replace") for k, v in values.items()}
        cmd = f"cd {q(path)} && {{ {install}\n}} 2>&1"
        if values:
            # The values go through a 0600 file read and removed before `install` starts: never a command line.
            f = f"/tmp/.hangar-build-{pysecrets.token_hex(8)}"
            exports = "".join(f"export {k}={q(v)}\n" for k, v in text_values.items())
            await prov.write_file(sb, f, exports.encode(), 0o600)
            cmd = f"set -a; . {f}; set +a; rm -f {f}; {cmd}"
        left = self.settings.env_build_timeout_minutes * 60 - (now() - (b.started_at or now()))
        res = await prov.exec(sb, cmd, cwd=path, timeout=max(60, int(left)))
        text = res.stdout + (("\n" + res.stderr) if res.stderr.strip() else "")
        for v in text_values.values():
            if len(v) >= 4:
                text = text.replace(v, "***")
        b.log = self.fleet.broker.redact(text)[-LOG_TAIL:]
        if res.exit_code != 0:
            raise EnvBuildError(f"install exited with {res.exit_code}" + (": timed out" if res.exit_code == 124
                                                                          else ""))

    async def _leak_scan(self, prov: Provider, sb: Sandbox, values: dict[str, bytes], path: str) -> None:
        from hangar import buildsecrets

        hits = await buildsecrets.leak_scan(prov, sb, values, paths=[path])
        if hits:
            raise EnvBuildError("install left build secret(s) in the checkout, which every sandbox would get: "
                                + "; ".join(hits[:10]) + ". Write them outside the repo, or remove them at the end "
                                "of install")

    async def _capture(self, prov: Provider, sb: Sandbox, path: str) -> bytes:
        """The checkout as a gzip tar (uid 1000 as in the sandbox), within HANGAR_SNAPSHOT_MAX_MB."""
        from hangar.fleet import ARCHIVE_READ_COST

        q = shlex.quote
        tmp = f"/tmp/hangar-build-{pysecrets.token_hex(6)}.tgz"
        res = await prov.exec(sb, f"tar czf {tmp} --numeric-owner -C {q(posixpath.dirname(path))} "
                                  f"{q(posixpath.basename(path))}; rc=$?; echo; stat -c %s {tmp} 2>/dev/null; "
                                  "exit $rc", timeout=900)
        cap = self.settings.snapshot_max_mb * 1024 * 1024
        last = (res.stdout.strip().splitlines() or ["0"])[-1]
        size = int(last) if last.isdigit() else cap + 1
        if res.exit_code not in (0, 1) or size > cap:
            await prov.exec(sb, f"rm -f {tmp}", timeout=30)
            raise EnvBuildError(f"the checkout after install is over HANGAR_SNAPSHOT_MAX_MB "
                                f"({self.settings.snapshot_max_mb} MB)" if size > cap else
                                f"could not archive the checkout: {res.stderr[-300:]}")
        try:
            async with self.fleet.archive_memory.hold(size * ARCHIVE_READ_COST):
                data = await prov.read_file(sb, tmp, limit=cap)
        finally:
            await prov.exec(sb, f"rm -f {tmp}", timeout=30)
        if data is None or len(data) > cap:
            raise EnvBuildError("the checkout's archive went missing or grew past the cap while being read")
        return data

    async def _image(self, b: Build, spec: EnvSpec, prov: Provider, path: str, data: bytes) -> str:
        """`FROM <environment image>` + the checkout: no RUN, so nothing of the repo's runs on a builder."""
        from hangar import images

        registry = images.registry_for(self.settings, b.provider)
        repo = f"{(registry or self.settings.image_prefix).rstrip('/')}/{spec.name}-repo"
        tag = f"{repo}:{(b.key or 'none')[:12]}-{b.id.removeprefix('bld-')}"
        dockerfile = (f"# hangar build {b.id}: {b.repo} at {b.commit}, environment {spec.name}\n"
                      f"FROM {b.base}\n"
                      f"LABEL dev.hangar.build={json.dumps(b.id)} dev.hangar.repo={json.dumps(b.repo)} "
                      f"dev.hangar.commit={json.dumps(b.commit or '')}\n"
                      # The archive is uid 1000's (the sandbox user, by the image contract): --chown says so to
                      # builders that would extract it as root.
                      f"ADD --chown=1000:1000 workspace.tgz {posixpath.dirname(path) or '/'}/\n")
        ctx = BuildContext(dockerfile=dockerfile, files={"workspace.tgz": data}, digest=(b.key or "")[:16])
        async with self.fleet.archive_memory.hold(len(data) * 2):  # the context, and its tar while it's sent
            image = await prov.build_template(spec, ctx, tag)
        ref = await prov.image_digest(tag)
        return ref or prov.image_ref(image)

    # ================================================================== admin
    def activate(self, build_id: str, actor: str) -> Build:
        """Roll back (or forward) to a build of the history and pin it: drift rebuilds still run and are kept, but
        don't replace it until `unpin`."""
        b = self.get(build_id)
        if b.state != "succeeded" or not b.image:
            raise EnvBuildError(f"build {b.id} ({b.version}) is {b.state}; only a successful build can be active")
        h = self.head(b.provider, b.environment, b.repo)
        if b.id not in h.ids:
            raise EnvBuildError(f"build {b.id} is no longer in the history")
        prev = h.active
        h.active, h.pinned, h.pinned_by, h.pinned_at = b.id, True, actor, now()
        self._put_head(b.provider, b.environment, b.repo, h)
        self.fleet.audit(actor, "envbuild.rollback", b.repo, build=b.id, previous=prev, environment=b.environment,
                         provider=b.provider)
        return b

    def unpin(self, repo: str, env: str, pname: str, actor: str) -> Head:
        rkey = repo_key(repo) or ""
        h = self.head(pname, env, rkey)
        if not h.pinned:
            raise EnvBuildError(f"{rkey} ({env} on {pname}) isn't pinned")
        h.pinned, h.pinned_by, h.pinned_at = False, None, None
        latest = next((b for b in self.history(pname, env, rkey) if b.state == "succeeded" and b.image), None)
        if latest is not None:
            h.active = latest.id
        self._put_head(pname, env, rkey, h)
        self.fleet.audit(actor, "envbuild.unpin", rkey, environment=env, provider=pname, active=h.active)
        return h

    def refresh(self, env: str, pname: str) -> list[str]:
        """After the environment's image is ready (startup's prebuild): rebuild, in the background, the repo builds
        made on an older image of it, so the next sandbox doesn't fall behind the environment."""
        if self._supported(pname):
            return []
        try:
            spec = self.fleet.catalog.get(env)
            base = self._base(spec, pname)[0]
        except Exception:  # noqa: BLE001 - prebuild logs the environment's own failure
            return []
        started = []
        for k in self.store.kv_keys(f"{KV_HEAD}{pname}:{env}:"):
            repo = k[len(f"{KV_HEAD}{pname}:{env}:"):]
            b = self.active(pname, env, repo)
            if b is not None and b.base != base and self._should_build(pname, env, repo, b.key or "", base):
                self.schedule(b.repo_url, env, pname, reason="base", actor="hangar:prebuild")
                started.append(repo)
        return started


def archive_leaks(data: bytes, values: dict[str, bytes], limit: int = 10) -> list[str]:
    """Entries of a captured checkout (gzip tar) holding a build secret, as "<path>: <secret id>", never the value.

    The sandbox's own scan (hangar.buildsecrets.leak_scan) runs where install's code ran: a shell function or a
    process install left behind can hide a file from it, or write one after it. This reads the bytes the image gets.
    Like that scan it looks for each value's longest line (8 bytes or more), so it catches a secret written as is
    (an .npmrc, a config file), not one install encoded on purpose."""
    import io
    import tarfile

    needles = {}
    for sid, v in values.items():
        n = max(v.strip().splitlines() or [b""], key=len).strip()
        if len(n) >= 8:
            needles[sid] = n
    if not needles:
        return []
    keep = max(len(n) for n in needles.values()) - 1
    out: list[str] = []
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r|gz") as tf:
            for m in tf:
                found = {sid for sid, n in needles.items()
                         if n in m.name.encode(errors="replace") or n in m.linkname.encode(errors="replace")}
                f = tf.extractfile(m) if m.isfile() else None
                tail = b""
                while f is not None and len(found) < len(needles) and (chunk := f.read(1 << 20)):
                    buf = tail + chunk
                    found |= {sid for sid, n in needles.items() if n in buf}
                    tail = buf[-keep:]
                shown = m.name[:300]
                for n in needles.values():  # a name can hold the value too
                    shown = shown.replace(n.decode(errors="replace"), "***")
                out += [f"{shown}: {sid}" for sid in sorted(found)]
                if len(out) >= limit:
                    break
    except (tarfile.TarError, OSError, EOFError) as e:
        raise EnvBuildError(f"could not read the captured checkout to check it for build secrets: {e}") from e
    return out[:limit]


def checkout_path(settings: Any, spec: EnvSpec, sb: Sandbox) -> str:
    """Where Fleet._setup_repo puts the sandbox's checkout (the same rule)."""
    from hangar.envspec import repo_dirname

    if spec.repo and spec.repo.path and spec.repo.url == sb.repo:
        return spec.repo.path
    return f"{settings.workspace_dir}/{repo_dirname(sb.repo or '')}"


def job_defaults(fleet: Fleet, repo: str | None, allowed: list[str], environment: str | None = None
                 ) -> tuple[repoenv.RepoEnvironment | None, str | None]:
    """For a job's submission, before any clone: the repo's config as its default branch last had it, and the
    environment its `extends` names. The file comes before the repo's policy, which comes before the environment
    (repoenv's resolution order).

    The file is the repo's to write, and an environment decides credentials, secrets and egress, so `extends` picks
    only among the environments the repo's HANGAR_GITHUB_REPOS entry (an admin) allows; with no entry it picks none.
    The config is read only as the job's own environment (when it names one) or those allowed environments saw it,
    never as another environment's sandbox (a member's, whose `setup` ran first) recorded it."""
    if not fleet.settings.env_builds or not repo:
        return None, None
    if environment:
        return of(fleet).seen_config(environment, repo), None
    cfg = of(fleet).newest_seen_config(repo, allowed)
    if cfg is None or not cfg.extends:
        return cfg, None
    if cfg.extends not in allowed or cfg.extends not in fleet.catalog.names():
        log.info("%s's %s extends %s, which isn't allowed for it here; ignoring", repo, repoenv.PATH, cfg.extends)
        return cfg, None
    return cfg, cfg.extends


def job_config(fleet: Fleet, repo: str | None, environment: str,
               cfg: repoenv.RepoEnvironment | None) -> repoenv.RepoEnvironment | None:
    """The config whose `verify` a job uses once its environment is decided: `cfg` (job_defaults'), else the repo's
    file as that environment last saw it."""
    if cfg is not None or not fleet.settings.env_builds or not repo:
        return cfg
    return of(fleet).seen_config(environment, repo)


def environment_for(jobs: Any, repo: str, environment: str | None) -> str:
    """The environment a repo's builds are of: the one named, else as a job for the repo would pick it."""
    if environment:
        jobs.fleet.catalog.get(environment)  # KeyError: unknown
        return environment
    allowed = jobs.repo_envs(repo)
    _, env = job_defaults(jobs.fleet, repo, allowed)
    from hangar import repopolicy

    env = env or repopolicy.environment(jobs.policies.get(repo), allowed) or (allowed[0] if allowed else None)
    if not env:
        raise EnvBuildError(f"name the environment: {repo} isn't in HANGAR_GITHUB_REPOS and its policy names none")
    return env


def view(b: Build, h: Head | None = None) -> dict[str, Any]:
    d = b.model_dump(mode="json")
    d["version"] = b.version
    if h is not None:
        d["active"], d["pinned"] = h.active == b.id, h.pinned and h.active == b.id
    return d


def mount(app: Any, h: Any, admin: Any, member: Any) -> None:
    """REST: builds per repo (members read and start them; an admin rolls back and unpins), and validating a config.

    GET  /api/envbuilds[?repo=&environment=&provider=]   every repo with builds, or one repo's history
    GET  /api/envbuilds/{id}                             one build: state, log, config, image
    POST /api/envbuilds {repo, environment?, provider?}  build now (in the background)
    POST /api/envbuilds/{id}/rollback                    admin: make it active, pinned
    POST /api/envbuilds/unpin {repo, environment?, provider?}   admin
    POST /api/envbuilds/validate {content, repo?, environment?} check a .hangar/environment.yaml (nothing saved)
    """
    from fastapi import Body, HTTPException, Query

    fleet = h.fleet
    eb = of(fleet)

    def _resolve(body: dict[str, Any]) -> tuple[str, str, str]:
        repo = str(body.get("repo") or "").strip()
        if not repo_key(repo):
            raise HTTPException(400, "repo is required (owner/repo or a git URL)")
        if "://" not in repo and not repo.startswith("git@"):
            repo = f"https://github.com/{repo}.git"
        try:
            env = environment_for(h.jobs, repo, body.get("environment"))
        except (KeyError, EnvBuildError) as e:
            raise HTTPException(400, str(e).strip("'\"")) from e
        return repo, env, body.get("provider") or fleet.settings.default_provider

    @app.get("/api/envbuilds")
    async def builds_list(repo: str | None = Query(None), environment: str | None = Query(None),
                          provider: str | None = Query(None), _: str = member):
        if not repo:
            return eb.heads()
        url, env, pname = _resolve({"repo": repo, "environment": environment, "provider": provider})
        rkey = repo_key(url) or ""
        hd = eb.head(pname, env, rkey)
        return {"repo": rkey, "environment": env, "provider": pname, "pinned": hd.pinned, "active": hd.active,
                "running": eb.running(pname, env, rkey),
                "builds": [{**view(b, hd), "log": None, "config": None} for b in eb.history(pname, env, rkey)]}

    @app.get("/api/envbuilds/{build_id}")
    async def builds_get(build_id: str, _: str = member):
        try:
            b = eb.get(build_id)
        except KeyError as e:
            raise HTTPException(404, f"no build '{build_id}'") from e
        return view(b, eb.head(b.provider, b.environment, b.repo))

    @app.post("/api/envbuilds")
    async def builds_start(body: dict[str, Any] = Body(...), who: str = member):
        url, env, pname = _resolve(body)
        try:
            return view(eb.schedule(url, env, pname, reason="manual", actor=who))
        except (KeyError, EnvBuildError) as e:
            raise HTTPException(400, str(e).strip("'\"")) from e

    @app.post("/api/envbuilds/unpin")
    async def builds_unpin(body: dict[str, Any] = Body(...), who: str = admin):
        url, env, pname = _resolve(body)
        try:
            hd = eb.unpin(url, env, pname, who)
        except EnvBuildError as e:
            raise HTTPException(400, str(e)) from e
        return {"repo": repo_key(url), "environment": env, "provider": pname, "active": hd.active, "pinned": False}

    @app.post("/api/envbuilds/validate")
    async def builds_validate(body: dict[str, Any] = Body(...), _: str = member):
        content = body.get("content")
        if not isinstance(content, str):
            raise HTTPException(400, "content: the .hangar/environment.yaml text")
        repo = body.get("repo")
        allowed = h.jobs.repo_envs(repo) if repo else []
        ids = None
        from hangar import buildsecrets
        from hangar.repopolicy import RepoPolicies

        try:
            env = body.get("environment") or repoenv.parse(content).extends
            if env and env in fleet.catalog.names():
                ids = set(buildsecrets.mapping(fleet.catalog.get(env), RepoPolicies(fleet.store).get(repo)))
        except repoenv.RepoEnvError:
            pass
        return repoenv.check(content, fleet.settings, fleet.catalog, repo=repo, allowed_envs=allowed,
                             environment=body.get("environment"), secret_ids=ids)

    @app.post("/api/envbuilds/{build_id}/rollback")
    async def builds_rollback(build_id: str, who: str = admin):
        try:
            b = eb.activate(build_id, who)
        except KeyError as e:
            raise HTTPException(404, f"no build '{build_id}'") from e
        except EnvBuildError as e:
            raise HTTPException(400, str(e)) from e
        return view(b, eb.head(b.provider, b.environment, b.repo))


def _brief(b: Build) -> dict[str, Any]:
    return {"id": b.id, "version": b.version, "state": b.state, "commit": (b.commit or "")[:12] or None,
            "finished_at": b.finished_at, "error": b.error, "stage": b.stage}


def _event(info: dict[str, Any], eff: EnvSpec) -> str:
    """One line for the job's events and the sandbox's stage."""
    if info.get("fallback"):
        return f"environment: no repo build in use ({info.get('reason')}); the agent is told to run install itself"
    msg = f"environment: build {info.get('version')} of {info.get('repo')} ({(info.get('commit') or '')[:9]})"
    if info.get("stale"):
        msg += f"; {info.get('reason')}"
    return msg


async def _delete_template(prov: Any, name: str) -> None:
    from hangar.providers.kubernetes import EXT_GROUP, EXT_V, ApiException

    for plural in ("sandboxwarmpools", "sandboxtemplates"):
        try:
            await asyncio.to_thread(prov.crd.delete_namespaced_custom_object, EXT_GROUP, EXT_V, prov.ns, plural, name)
        except ApiException as e:
            if e.status != 404:
                raise


def of(fleet: Fleet) -> EnvBuilds:
    """The fleet's builds (made on first use, so Fleet itself needs no change)."""
    eb = fleet.__dict__.get("_envbuilds")
    if eb is None:
        eb = fleet.__dict__["_envbuilds"] = EnvBuilds(fleet)
    return eb


def wait_for_tasks(fleet: Fleet) -> list[asyncio.Task]:
    """The builds running now (tests and shutdown)."""
    return [t for t in of(fleet)._tasks.values() if not t.done()]


__all__ = ["Build", "EnvBuildError", "EnvBuilds", "Head", "Pick", "Seen", "of"]
