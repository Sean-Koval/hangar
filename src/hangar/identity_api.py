"""REST for identity: people and their login tokens (admins), where a credential may go and whose it is, and the
self-service checks for the routes a member's `user` token reaches (their own tokens and credentials)."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

import hangar.identity as identity
from hangar import people, teamcreds
from hangar.models import ApiToken, Credential, CredentialKind


class ExposureBody(BaseModel):
    exposure: str | None = None  # sandbox | control_plane; null: the default for its kind and name


class OwnerBody(BaseModel):
    person: str | None = None  # null: a team credential


class PersonBody(BaseModel):
    id: str
    name: str = ""
    role: str = "member"
    token: bool = True  # mint their first login token (shown once)


class RoleBody(BaseModel):
    role: str


class LinkBody(BaseModel):  # --- teamcreds
    source: str = Field(max_length=40)  # github | slack | jira | email
    id: str = Field(min_length=1, max_length=320)


class LoginTokenBody(BaseModel):
    name: str = Field("login", max_length=100)


def _caller() -> Any:
    p = identity.current.get()
    if p is None:  # every route here sets it through its dependency
        raise HTTPException(401, "unauthorized")
    return p


def manages() -> bool:
    """Whether the REST caller manages everyone's tokens and credentials (an admin token), or only their own."""
    return _caller().admin


def _login_scopes(person: Any) -> list[str]:
    return ["admin"] if person.role == "admin" else ["user"]


def _mint_login(h: Any, person: Any, name: str, who: str) -> dict[str, Any]:
    raw, tok = h.auth.create(f"{name}-{person.id}"[:100], _login_scopes(person), person=person.id)
    h.fleet.audit(who, "token.create", tok.id, name=tok.name, scopes=tok.scopes, person=person.id)
    return {"token": raw, **tok.model_dump(mode="json", exclude={"token_hash"})}


# ---------------------------------------------------------------------------- self-service (app.py routes)
def tokens(h: Any) -> list[ApiToken]:
    """Every token for an admin; a member's own otherwise."""
    p = _caller()
    return [t for t in h.auth.list() if p.admin or people.token_person(t.person) == p.person]


def token_person(h: Any, wanted: str | None, scopes: list[str], callback_secret: str | None = None) -> str:
    """The person a new token belongs to: the one an admin names, else the caller. A member mints only their own,
    within people.MEMBER_SCOPES, and never bound to a signing key (its callbacks would be signed with a
    control-plane key, to a URL the member picks)."""
    p = _caller()
    target = wanted or p.person
    if not p.admin and target != p.person:
        raise identity.Forbidden("you can mint tokens only for yourself")
    if not p.admin and callback_secret:  # --- review
        raise identity.Forbidden("binding a token to a signing key (callback_secret) needs an admin token")
    person = people.get(h.store, target)
    if person is None:
        raise KeyError(f"no person '{target}'")
    people.check_token_scopes(person, scopes)
    return person.id


def check_token_revoke(h: Any, token_id: str) -> None:
    if not any(t.id == token_id for t in tokens(h)):
        raise KeyError(f"no token '{token_id}'")


def credentials(h: Any) -> list[Credential]:
    """Every credential for an admin; the team's shared ones and the member's own otherwise."""
    p = _caller()
    return [c for c in h.broker.list() if p.admin or c.person in (None, p.person)]


def credential_person(h: Any, name: str, harness: str, kind: CredentialKind, wanted: str | None,
                      exposure: str | None, team: bool = False) -> str | None:
    """Whose a credential being stored is. An admin may name anyone, make it the team's (`team`), or keep its
    current owner; a new one an admin names no owner for is their own (--- teamcreds: deny by default, so an admin's
    own login never becomes the team's by omission), except the control plane's own. A member stores only their
    own, never over someone else's or the team's, and never a control-plane kind."""
    p = _caller()
    existing = h.store.get(Credential, name)
    if team and wanted is not None:  # --- teamcreds
        raise ValueError("a credential is a person's own or the team's: pass person or team, not both")
    if p.admin:
        if wanted is not None and people.get(h.store, wanted) is None:
            raise KeyError(f"no person '{wanted}'")
        if team:  # --- teamcreds
            if existing is not None:
                teamcreds.check_team(existing)
            return None
        if wanted is not None or existing is not None:
            return wanted if wanted is not None else existing.person  # type: ignore[union-attr]
        control_plane = harness == "webhook" or kind == CredentialKind.GITHUB_APP or exposure == "control_plane" \
            or name in identity.reserved_names(h.settings)
        return None if control_plane else people.token_person(p.person)
    if team:  # --- teamcreds
        raise identity.Forbidden("team credentials are added by an admin")
    if wanted not in (None, p.person):
        raise identity.Forbidden("you can store credentials only for yourself")
    if existing is not None and existing.person != p.person:
        raise identity.Forbidden(f"credential '{name}' isn't yours; pick another name")
    if harness == "webhook" or kind == CredentialKind.GITHUB_APP or exposure:
        raise identity.Forbidden("control-plane credentials and exposure are set by an admin")
    if name in identity.reserved_names(h.settings):  # --- review: hangar would sign, verify or call GitHub with it
        raise identity.Forbidden(f"'{name}' is a name hangar's settings reserve for its own credentials; pick another")
    return p.person


def check_credential_delete(h: Any, name: str) -> None:
    p = _caller()
    c = h.broker.get(name)
    if not p.admin and c.person != p.person:
        raise identity.Forbidden(f"credential '{name}' isn't yours")


# ---------------------------------------------------------------------------- admin routes
def mount(app: FastAPI, h: Any, A: Any) -> None:
    @app.put("/api/credentials/{name}/exposure")
    async def set_exposure(name: str, body: ExposureBody, who: str = A):
        c = h.broker.set_exposure(name, body.exposure)
        h.fleet.audit(who, "credential.exposure", name, exposure=body.exposure)
        return {**c.model_dump(mode="json"), "exposure": identity.exposure(c, h.settings)}

    @app.put("/api/credentials/{name}/owner")
    async def set_owner(name: str, body: OwnerBody, who: str = A):
        c = h.broker.get(name)
        if body.person is not None and people.get(h.store, body.person) is None:
            raise KeyError(f"no person '{body.person}'")
        if body.person is None:  # --- teamcreds: never someone's own subscription login
            teamcreds.check_team(c)
        c.person = body.person
        h.store.put(c)
        h.fleet.audit(who, "credential.owner", name, person=body.person)
        return {**c.model_dump(mode="json"), "owner": teamcreds.owner(c, h.settings),  # --- teamcreds
                "warning": teamcreds.sharing_warning(c)}

    # --- teamcreds: what the migration made the owner's, and when
    @app.get("/api/credentials/migration")
    async def credential_migration(_: str = A):
        rec = teamcreds.migration(h.store)
        if rec is None:
            raise HTTPException(404, "the credential migration hasn't run; it runs when hangar starts")
        return rec

    @app.get("/api/people")
    async def list_people(_: str = A):
        counts: dict[str, int] = {}
        for t in h.auth.list():
            counts[people.token_person(t.person)] = counts.get(people.token_person(t.person), 0) + 1
        return [{**p.model_dump(mode="json"), "tokens": counts.get(p.id, 0)} for p in people.list_people(h.store)]

    @app.post("/api/people")
    async def add_person(body: PersonBody, who: str = A):
        p = people.add(h.store, body.id, body.name, body.role)
        h.fleet.audit(who, "person.add", p.id, role=p.role)
        out: dict[str, Any] = p.model_dump(mode="json")
        if p.role == "member":  # --- review: what goes into their sandboxes, readable there (the CLI warns)
            out["shared_credentials"] = sorted(c.name for c in h.broker.list() if c.person is None
                                               and identity.exposure(c, h.settings) == "sandbox")
        if body.token:
            out["login"] = _mint_login(h, p, "login", who)
        return out

    @app.patch("/api/people/{person_id}")
    async def set_role(person_id: str, body: RoleBody, who: str = A):
        p = people.set_role(h.store, person_id, body.role)
        h.fleet.audit(who, "person.role", person_id, role=p.role)
        return p.model_dump(mode="json")

    @app.delete("/api/people/{person_id}")
    async def remove_person(person_id: str, who: str = A):
        if person_id == identity.person():
            raise HTTPException(400, "you can't remove yourself")
        revoked = people.remove(h.store, person_id)
        h.fleet.audit(who, "person.remove", person_id, revoked=revoked)
        return {"removed": person_id, "revoked_tokens": revoked}

    # --- teamcreds: a person's accounts elsewhere, so the jobs they start there run for them
    @app.post("/api/people/{person_id}/links")
    async def link_person(person_id: str, body: LinkBody, who: str = A):
        ext, login = await teamcreds.link_id(h, body.source, body.id)  # --- review: a GitHub login -> its id
        p = people.link(h.store, person_id, body.source, ext)
        h.fleet.audit(who, "person.link", person_id, link=people.external_key(body.source, ext), login=login)
        return {**p.model_dump(mode="json"), **({"looked_up": {"login": login, "id": ext}} if login else {})}

    @app.delete("/api/people/{person_id}/links")
    async def unlink_person(person_id: str, source: str = Query(...), id: str = Query(...), who: str = A):
        ext, _ = await teamcreds.link_id(h, source, id)  # --- review
        p = people.unlink(h.store, person_id, source, ext)
        h.fleet.audit(who, "person.unlink", person_id, link=people.external_key(source, ext))
        return p.model_dump(mode="json")

    @app.post("/api/people/{person_id}/tokens")
    async def login_token(person_id: str, body: LoginTokenBody, who: str = A):
        p = people.get(h.store, person_id)
        if p is None:
            raise KeyError(f"no person '{person_id}'")
        return _mint_login(h, p, body.name, who)
