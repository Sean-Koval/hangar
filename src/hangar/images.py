"""Images built before anyone waits: which environment images and harness packs the current code needs on a provider,
which of them are missing, and building those ahead of time (`hangar images plan | build`, docs/gke.md).

Every tag is content-addressed (an environment's rendered build, a pack's recipe and locked version), so the list is
a function of the code, the environment specs and a few settings, and needs no running hangar. CI builds what's
missing on every push to main, and deploy/gke/up.sh before rollout; the cluster then only looks the tags up in the
registry. It still builds on first use what CI couldn't know about: environments created through the API, and
settings that change tags (registry, sandbox user, workspace, CA bundle) set only in hangar's database.
"""

from __future__ import annotations

import asyncio
import shutil
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from typing import Any

from hangar.config import Settings
from hangar.envspec import BuildContext, EnvCatalog, EnvSpec, image_tag, render
from hangar.harnesses import (
    HARNESSES,
    get_harness,
    pack_digest,
    pack_dockerfile,
    pack_script,
    pack_tag,
    version_from_log,
)

REGISTRY_PROVIDERS = ("kubernetes", "e2b")  # providers that pull from HANGAR_REGISTRY (same rule as Fleet)
PLANNED = ("kubernetes", "docker")
Log = Callable[[str], Awaitable[None] | None]


@dataclass
class Item:
    kind: str  # "environment" | "pack"
    name: str  # the environment, or the harness
    tag: str
    harness: str | None = None  # pack: its harness; environment: the harness baked in (bake mode only)
    version: str | None = None  # pack: the locked version
    status: str = "unchecked"  # present | missing | unknown | error, then built | failed
    ref: str | None = None  # repo@sha256:... once the registry knows it
    installed: str | None = None  # pack: what the CLI reported at build
    detail: str | None = None
    ctx: BuildContext | None = field(default=None, repr=False, compare=False)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("ctx")
        return {k: v for k, v in d.items() if v is not None}


def registry_for(settings: Settings, provider: str) -> str | None:
    return settings.registry if provider in REGISTRY_PROVIDERS else None


def env_tag(settings: Settings, spec: EnvSpec, provider: str, bake: list[str] | None = None
            ) -> tuple[str, BuildContext]:
    """An environment image's tag and build, exactly as Fleet computes them (Fleet._tag_for calls this)."""
    extra = settings.extra_ca_bundle.expanduser().read_bytes() if settings.extra_ca_bundle else None
    ctx = render(spec, settings.sandbox_user, settings.workspace_dir, extra, bake=bake,
                 docker_image=settings.heavy_docker_image)  # --- heavy
    return image_tag(spec, ctx, settings.image_prefix, registry_for(settings, provider)), ctx


def pack_item(settings: Settings, harness: str, provider: str) -> Item:
    h = get_harness(harness)
    ctx = BuildContext(dockerfile=pack_dockerfile(h), files={f"packs/{h.name}.sh": pack_script(h).encode()},
                       digest=pack_digest(h))
    return Item("pack", harness, pack_tag(h, settings.image_prefix, registry_for(settings, provider)),
                harness=harness, version=h.version, ctx=ctx)


def image_volumes(settings: Settings, provider: str) -> bool:
    """Whether the provider mounts packs. Docker detects it at runtime; the plan assumes it does unless
    HANGAR_DOCKER_IMAGE_VOLUMES says otherwise."""
    if provider == "kubernetes":
        return settings.k8s_image_volumes
    if provider == "docker":
        return settings.docker_image_volumes is not False
    return False


def plan(settings: Settings, catalog: EnvCatalog, provider: str, packs: bool | None = None) -> list[Item]:
    """Every environment image and harness pack `provider` needs, unchecked. Environments with a prebuilt `image:`
    aren't hangar's to build and are left out; a spec that doesn't load is an `error` item."""
    if provider not in PLANNED:
        raise ValueError(f"images plan covers {' and '.join(PLANNED)}, not {provider}")
    if provider in REGISTRY_PROVIDERS and not settings.registry:
        raise ValueError(f"set HANGAR_REGISTRY (or --registry): {provider} pulls images from a registry")
    vols = image_volumes(settings, provider) if packs is None else packs
    envs: dict[str, Item] = {}
    harness_packs: dict[str, Item] = {}
    for name in catalog.names():
        try:
            spec = catalog.get(name)
            if spec.image:
                continue
            if spec.harness_mode == "pack" and not vols:
                raise ValueError("sets harness_mode: pack, but the provider can't mount packs")
            if vols and spec.harness_mode != "bake":
                variants: list[list[str]] = [[]]
                for h in spec.agent_harnesses:
                    if HARNESSES[h].install and h not in harness_packs:
                        harness_packs[h] = pack_item(settings, h, provider)
            else:  # bake: the default harness's image first (what `build` makes), then the others' (made on first use)
                default = spec.pick_harness()
                order = [default, *(h for h in spec.agent_harnesses if h != default)] if default else []
                variants = [[h] if h and HARNESSES[h].install else [] for h in order] or [[]]
            for bake in variants:
                tag, ctx = env_tag(settings, spec, provider, bake)
                envs.setdefault(tag, Item("environment", name, tag, harness=bake[0] if bake else None, ctx=ctx))
        except Exception as e:  # noqa: BLE001 - one bad spec shouldn't hide the rest of the plan
            envs[f"error:{name}"] = Item("environment", name, "", status="error", detail=str(e))
    return [*envs.values(), *(harness_packs[h] for h in sorted(harness_packs))]


async def _local_tags() -> set[str]:
    proc = await asyncio.create_subprocess_exec(
        "docker", "image", "ls", "--format", "{{.Repository}}:{{.Tag}}",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"docker image ls failed: {err.decode(errors='replace').strip()[-300:]}")
    return set(out.decode().split())


async def check(items: list[Item], provider: str, registry: Any = None, concurrency: int = 8) -> list[Item]:
    """Mark each item present, missing or unknown: one registry request per tag (Registry.check), or for local
    Docker one `docker image ls`. Never a CLI process per tag."""
    todo = [i for i in items if i.status != "error"]
    if not todo:
        return items
    if provider in REGISTRY_PROVIDERS:
        from hangar.registry import Registry, RegistryError

        reg = registry or Registry()
        try:
            found = await reg.check([i.tag for i in todo], concurrency=concurrency)
        finally:
            if registry is None:
                await reg.close()
        for i in todo:
            r = found[i.tag]
            if isinstance(r, RegistryError):
                i.status, i.detail = "unknown", str(r)
            elif r:
                i.status, i.ref = "present", f"{i.tag.rsplit(':', 1)[0]}@{r}"
            else:
                i.status = "missing"
        return items
    try:
        local = await _local_tags()
    except (OSError, RuntimeError) as e:
        for i in todo:
            i.status, i.detail = "unknown", str(e)
        return items
    for i in todo:
        i.status = "present" if i.tag in local else "missing"
    return items


def _ar_project(tag: str) -> str | None:
    """The project of an Artifact Registry tag (REGION-docker.pkg.dev/PROJECT/...), where Cloud Build should run."""
    host, _, rest = tag.partition("/")
    return rest.split("/", 1)[0] if host.endswith("-docker.pkg.dev") and "/" in rest else None


def pick_builder(builder: str, provider: str, docker_ok: bool) -> str:
    if builder not in ("auto", "docker", "cloudbuild"):
        raise ValueError(f"unknown builder {builder}: auto, docker or cloudbuild")
    if builder == "cloudbuild" and provider not in REGISTRY_PROVIDERS:
        raise ValueError("Cloud Build pushes to a registry; the docker provider builds locally (--builder docker)")
    if builder == "auto":
        if docker_ok:
            return "docker"
        if provider in REGISTRY_PROVIDERS and shutil.which("gcloud"):
            return "cloudbuild"
        raise ValueError("no builder: start Docker, or install gcloud for Cloud Build")
    if builder == "docker" and not docker_ok:
        raise ValueError("docker isn't available (the CLI or its daemon)")
    return builder


async def build_missing(items: list[Item], settings: Settings, provider: str, builder: str = "auto", jobs: int = 2,
                        log: Callable[[str], None] | None = None, registry: Any = None) -> list[Item]:
    """Build and push every `missing` item, `jobs` at a time. Each ends `built` (with its digest and, for a pack, the
    installed version) or `failed` (with the end of its log); the others keep going."""
    from hangar.providers._build import cloud_build, docker_available, docker_build

    todo = [i for i in items if i.status == "missing"]
    if not todo:
        return items
    builder = pick_builder(builder, provider, await docker_available())
    push = provider in REGISTRY_PROVIDERS
    sem = asyncio.Semaphore(max(1, jobs))
    reg = None
    if push:
        from hangar.registry import Registry

        reg = registry or Registry()

    async def one(item: Item) -> None:
        lines: list[str] = []

        def out(line: str) -> None:
            lines.append(line)
            if log:
                log(f"[{item.name}{'/' + item.harness if item.kind == 'environment' and item.harness else ''}] "
                    f"{line}")

        async with sem:
            assert item.ctx is not None
            try:
                out(f"building {item.tag} ({builder})")
                if builder == "cloudbuild":
                    await cloud_build(item.ctx, item.tag, out, settings.k8s_build_service_account,
                                      project=_ar_project(item.tag))
                else:
                    await docker_build(item.ctx, item.tag, log=out, platform="linux/amd64" if push else None,
                                       push=push)
                if reg is not None:
                    item.ref = await reg.pinned(item.tag)
                if item.kind == "pack":
                    item.installed = version_from_log(lines)
                item.status, item.detail = "built", None
            except Exception as e:  # noqa: BLE001 - report it, build the rest
                item.status, item.detail = "failed", str(e)[-2000:]
                out(f"FAILED: {str(e).splitlines()[0] if str(e) else type(e).__name__}")

    try:
        await asyncio.gather(*(one(i) for i in todo))
    finally:
        if reg is not None and registry is None:
            await reg.close()
    return items


def table(items: list[Item]) -> str:
    rows = [("KIND", "NAME", "HARNESS", "STATUS", "TAG")]
    for i in items:
        rows.append((i.kind, i.name, (i.harness or "") + (f"@{i.version}" if i.version else ""), i.status,
                     i.tag or (i.detail or "")[:100]))
    widths = [max(len(r[c]) for r in rows) for c in range(4)]
    return "\n".join("  ".join(r[c].ljust(widths[c]) for c in range(4)) + "  " + r[4] for r in rows)
