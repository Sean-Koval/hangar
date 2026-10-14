"""REST routes for identity: where a credential may go (`hangar cred exposure`). Admin tokens only."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from pydantic import BaseModel

from hangar import identity


class ExposureBody(BaseModel):
    exposure: str | None = None  # sandbox | control_plane; null: the default for its kind and name


def mount(app: FastAPI, h: Any, A: Any) -> None:
    @app.put("/api/credentials/{name}/exposure")
    async def set_exposure(name: str, body: ExposureBody, who: str = A):
        c = h.broker.set_exposure(name, body.exposure)
        h.fleet.audit(who, "credential.exposure", name, exposure=body.exposure)
        return {**c.model_dump(mode="json"), "exposure": identity.exposure(c, h.settings)}
