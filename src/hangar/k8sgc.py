"""Garbage collection of the SandboxTemplates hangar makes per repo build and per set of services (the #99 review).

On Kubernetes a sandbox started from a repo's environment build, or with services other than its environment's, gets
a SandboxTemplate and an empty warm pool of its own: `hangar-<env>-<build id>` or `hangar-<env>-s<services digest>`
(envbuilds.EnvBuilds.provider_spec). Nothing deleted the services' ones, so one appeared for every set of services a
repo ever declared. A build's was deleted as soon as the build fell out of the history, even under a session still
running from it (after a rollback or an unpin).

Now the reaper sweeps them: a template of that shape that no live sandbox was started from, made more than GRACE ago
(a create applies it a moment before its claim), goes with its warm pool. A sandbox records its template at create
(`info["k8s_template"]`); for one started before that, it is worked out the same way the create did.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import logging
import re
import time
from collections.abc import AsyncIterator
from datetime import datetime
from typing import TYPE_CHECKING, Any

from hangar.models import Sandbox

if TYPE_CHECKING:
    from hangar.fleet import Fleet

log = logging.getLogger("hangar.k8sgc")

DERIVED = re.compile(r"^hangar-(?P<env>.+)-(?:s?[0-9a-f]{8})$")
GRACE = 600.0  # seconds a new template is left alone: its create may not have made its claim yet
EVERY = 600.0  # seconds between two sweeps (the reaper runs every minute)
LABELS = "app.kubernetes.io/managed-by=hangar"
_last: dict[int, float] = {}


def _races(prov: Any) -> tuple[collections.Counter[str], set[str]]:
    """--- review: (creates in flight per spec name, spec names whose template a sweep is deleting), per provider."""
    return prov.__dict__.setdefault("_k8sgc_creating", collections.Counter()), \
        prov.__dict__.setdefault("_k8sgc_deleting", set())


@contextlib.asynccontextmanager
async def creating(prov: Any, name: str) -> AsyncIterator[None]:
    """--- review: a create applying or claiming from `hangar-<name>`. The sweep leaves the template alone meanwhile,
    and a create that comes while the sweep deletes it waits, then applies it again (the delete clears the cache)."""
    n, deleting = _races(prov)
    while name in deleting:
        await asyncio.sleep(0.05)
    n[name] += 1
    try:
        yield
    finally:
        n[name] -= 1
        if n[name] <= 0:
            del n[name]


def _in_use(fleet: Fleet, name: str, env: str) -> bool:
    """--- review: a live sandbox runs on the template, or one of its environment's can't be told (read now)."""
    for sb in fleet.store.list_active(Sandbox):
        if sb.provider == "kubernetes" and ((t := template_of(fleet, sb)) == name or
                                            (t is None and sb.environment[:40] == env)):
            return True
    return False


def template_of(fleet: Fleet, sb: Sandbox) -> str | None:
    """The SandboxTemplate a Kubernetes sandbox was started from; None when that can't be told any more."""
    if name := sb.info.get("k8s_template"):
        return name
    from hangar.envbuilds import EnvBuilds

    try:
        return f"hangar-{EnvBuilds.provider_spec(fleet.catalog.get(sb.environment), sb).name}"
    except (KeyError, ValueError):
        return None


async def delete(prov: Any, name: str) -> None:
    """A template and its warm pool (the pool first: it refers to the template). Gone already is fine."""
    from hangar.providers.kubernetes import EXT_GROUP, EXT_V, ApiException

    for plural in ("sandboxwarmpools", "sandboxtemplates"):
        try:
            await asyncio.to_thread(prov.crd.delete_namespaced_custom_object, EXT_GROUP, EXT_V, prov.ns, plural, name)
        except ApiException as e:
            if e.status != 404:
                raise
    prov._synced.pop(name.removeprefix("hangar-"), None)  # a later create applies it again


def _created(t: dict[str, Any]) -> float:
    ts = (t.get("metadata") or {}).get("creationTimestamp")
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return time.time()  # can't tell: as if new


async def sweep(fleet: Fleet, force: bool = False) -> list[str]:
    """Delete the build and services templates nothing uses; returns their names. At most every EVERY seconds."""
    prov = fleet.providers.get("kubernetes")
    if prov is None or getattr(prov, "crd", None) is None:
        return []
    t = time.monotonic()
    if not force and t - _last.get(id(fleet), -EVERY) < EVERY:
        return []
    _last[id(fleet)] = t
    from hangar.providers.kubernetes import EXT_GROUP, EXT_V

    found = await asyncio.to_thread(prov.crd.list_namespaced_custom_object, EXT_GROUP, EXT_V, prov.ns,
                                    "sandboxtemplates", label_selector=LABELS)
    base = {f"hangar-{n}" for n in fleet.catalog.names()}
    used: set[str] = set()
    unsure: set[str] = set()  # environments with a live sandbox whose template can't be told: theirs all stay
    for sb in fleet.store.list_active(Sandbox):
        if sb.provider != "kubernetes":
            continue
        if (name := template_of(fleet, sb)) is not None:
            used.add(name)
        else:
            unsure.add(sb.environment[:40])
    now = time.time()
    gone: list[str] = []
    for tmpl in (found or {}).get("items") or []:
        name = (tmpl.get("metadata") or {}).get("name") or ""
        m = DERIVED.match(name)
        if not m or name in base or name in used or m["env"] in unsure or now - _created(tmpl) < GRACE:
            continue
        # --- review: the sandboxes were read before the deletes before this one, each a wait on the cluster. A
        # create in flight, or one saved since, keeps it; from here to the end of the delete, creates of it wait.
        key = name.removeprefix("hangar-")
        creates, deleting = _races(prov)
        if creates.get(key) or _in_use(fleet, name, m["env"]):
            continue
        deleting.add(key)
        try:
            await delete(prov, name)
            gone.append(name)
        except Exception as e:  # noqa: BLE001 - the next sweep tries again
            log.warning("could not delete SandboxTemplate %s: %s", name, e)
        finally:
            deleting.discard(key)
    if gone:
        log.info("deleted %d SandboxTemplate(s) no sandbox uses: %s", len(gone), ", ".join(gone[:10]))
    return gone
