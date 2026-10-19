"""`image-ready`: ready once the image the code repo's CI builds is in its registry.

    ready: {kind: image-ready, image: "registry.example.com/payments-bot", tag: "{code.merge_sha}"}

Asked over the OCI distribution API (registry.py: Google registries with Workload Identity, others anonymously or
by their challenge). A missing tag is "not yet"; a registry that can't answer is "not yet" too, with a note, never
ready. Its `Ready` carries `image` (`image:tag`) and `digest`, for `{ready.image}` and `{ready.digest}` in `apply`.
It has no apply of its own."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from hangar.ext.deliverers import DeliverCtx, DeliverError, Ready


class ImageReady:
    kind = "image-ready"

    def __init__(self, registry: Callable[[], Any] | None = None):
        self._registry = registry  # tests pass a factory with a mock transport

    def _open(self) -> Any:
        from hangar.registry import Registry

        return self._registry() if self._registry is not None else Registry()

    async def ready(self, ctx: DeliverCtx) -> Ready | None:
        from hangar.registry import RegistryError

        image, tag = ctx.spec.get("image"), ctx.spec.get("tag", "latest")
        if not isinstance(image, str) or not image or ":" in image.rsplit("/", 1)[-1] or "@" in image:
            raise DeliverError("image-ready: `image` is a registry path without a tag (registry.example.com/app)")
        if not isinstance(tag, str) or not tag:
            raise DeliverError("image-ready: `tag` must be a string")
        ref = f"{image}:{ctx.render(tag)}"
        reg = self._open()
        try:
            digest = await reg.digest(ref)
        except RegistryError as e:
            ctx.note(f"image-ready: the registry couldn't say whether {ref} exists: {e}")
            return None
        finally:
            await reg.close()
        if not digest:
            return None
        return Ready(detail={"image": ref, "digest": digest}, note=f"{ref} is {digest[:19]}")

    async def apply(self, ctx: DeliverCtx, ready: Ready) -> None:
        raise DeliverError("image-ready is a ready check; name another kind (command, or a plugin's) for apply")


class _Plugin:
    """Built in (pyproject `hangar.plugins`, pluginhost.BUILTINS): registers the `image-ready` deliverer."""

    name = "image-ready"
    api = "hangar.plugin/v1"

    def __init__(self, d: Any):
        from hangar.ext import NoSettings

        self.Settings = NoSettings
        self.d = d

    def register(self, host: Any, settings: Any) -> None:
        host.add_deliverer(self.d)


INSTANCE = ImageReady()
PLUGIN = _Plugin(INSTANCE)
