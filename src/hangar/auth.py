"""Bearer-token auth shared by the MCP endpoint, REST API, websocket terminal and dashboard.

Tokens are random 32-byte secrets shown once; only a SHA-256 hash is stored. Codex and
Claude Code send them via `bearer_token_env_var` / `--header "Authorization: Bearer ..."`.

--- relay (I-§3.3): a bot (the Symphony bot) holds a `relay` token and names, on every MCP request, the person it
acts for in `X-Hangar-On-Behalf-Of` (`resolve_delegation`). Hangar trusts the bot to have authenticated that account
in its own system, and nothing more:

* **Only relay sources.** The claim names a `people.RELAY_SOURCES` account (`symphony:<user id>`), never a GitHub,
  Slack, Jira or email identity, which hangar's own doors verify themselves: a relay can't borrow those.
* **Only linked people.** The account must be linked to a hangar person (`hangar person link alice symphony <id>`).
  The request then runs as that person's principal: their visibility, their caps and credentials, `mcp` scope only
  (never admin, sandboxes, metrics or a login, whatever the relay token or the person's own tokens hold), with
  `via` naming the relay so every record and audit entry carries both (`identity.actor_name`).
* **Unlinked accounts** are refused (`HANGAR_RELAY_UNLINKED=refuse`, the default), or with `approve` may only submit
  a job that waits for a trusted person's approval, as an untrusted door's does (`approvals.relay_hold`).
* **Approvals through a relay** need a `relay_approve` token (`approvals.from_principal`); the person's own rule
  applies, and nobody approves their own work, even while only one person exists.
* **A relay token does nothing on its own:** without the header it is refused, it holds only relay scopes, only an
  admin person's token can hold them, and REST and OAuth consent never accept it.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import secrets
import time
from collections.abc import Callable, Mapping
from typing import Any, NamedTuple, NoReturn

import hangar.identity as identity
from hangar.models import ApiToken, Budget, now
from hangar.store import Store

PREFIX = "hgr_"
# admin: everything; jobs: only /api/jobs* (service callers); mcp: only /mcp; metrics: only GET /metrics (--- observe)
SCOPES = ("admin", "jobs", "mcp", "metrics")
SCOPES += ("user",)  # --- identity: a person's own login: /mcp, and their own tokens and credentials over REST
# --- relay: a bot acting for the linked person each request names (X-Hangar-On-Behalf-Of); relay_approve: it may also
# approve and reject jobs as them. Admin persons' tokens only (not in people.MEMBER_SCOPES), and nothing else beside.
SCOPES += ("relay", "relay_approve")
RELAY_SCOPES = ("relay", "relay_approve")
RELAY_HEADER = "x-hangar-on-behalf-of"
RELAY_UNLINKED = ("refuse", "approve")  # HANGAR_RELAY_UNLINKED: what an account linked to nobody gets
_MAX_CLAIM = 1024
_REPLY_TO_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}(:[A-Za-z0-9_=+/.-]{1,128}){1,3}$")
_RESERVED_NAMES = {"admin", "system"}
log = logging.getLogger("hangar.auth")


class Principal(NamedTuple):
    name: str  # display name (not unique)
    id: str  # token id: the ownership key
    scopes: list[str]
    callback_secret: str | None = None
    max_budget: Budget | None = None  # jobs tokens: caps the budget their jobs may ask for (workstream C)
    person: str | None = None  # --- identity: the person the token belongs to (the stable key for quotas)
    role: str | None = None  # --- identity: that person's role, admin | member
    # --- relay: set on a principal resolve_delegation made. `via`: "relay:<relay token name>"; `on_behalf_of`: the
    # account the relay vouched for ("symphony:<id>"); `reply_to`: where the bot wants answers posted; `may_approve`:
    # the relay token holds relay_approve; `held`: an unlinked account (person None) whose jobs wait for approval
    via: str | None = None
    on_behalf_of: str | None = None
    reply_to: str | None = None
    may_approve: bool = False
    held: bool = False

    @property
    def admin(self) -> bool:
        return "admin" in self.scopes

    @property
    def relay(self) -> bool:
        """--- relay: a relay token's own principal (it acts only on behalf of someone)."""
        return bool(set(self.scopes) & set(RELAY_SCOPES))


def check_token_name(name: str) -> None:
    if not name or ":" in name or name.lower() in _RESERVED_NAMES or len(name) > 100:
        raise ValueError(f"token name '{name}' is not allowed (no ':', not 'admin' or 'system', ≤100 chars)")


def check_relay_scopes(scopes: list[str]) -> None:
    """--- relay: a relay token holds relay scopes only, so it can't also act as itself (REST, MCP, OAuth consent)."""
    if set(scopes) & set(RELAY_SCOPES) and (other := [s for s in scopes if s not in RELAY_SCOPES]):
        raise ValueError(f"a relay token holds only {' or '.join(RELAY_SCOPES)}, not also {', '.join(other)}: it acts "
                         "only for the people it names; mint another token for anything else")


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class TokenAuth:
    def __init__(self, store: Store, admin_token: str | None = None):
        self.store = store
        self._admin_hash = hash_token(admin_token) if admin_token else None
        self._tickets: dict[str, tuple[str, float]] = {}

    # Browsers can't set headers on WebSocket/EventSource, so the dashboard trades its token
    # for a single-use ticket that lives 60s. Tickets (unlike tokens) are safe to put in URLs/logs.
    def issue_ticket(self, actor: str, ttl: float = 60) -> str:
        t = time.time()
        self._tickets = {k: v for k, v in self._tickets.items() if v[1] > t}
        ticket = "tkt_" + secrets.token_urlsafe(24)
        self._tickets[ticket] = (actor, t + ttl)
        return ticket

    def redeem_ticket(self, ticket: str | None) -> str | None:
        if not ticket:
            return None
        actor, exp = self._tickets.pop(ticket, (None, 0.0))
        return actor if actor and exp > time.time() else None

    def create(self, name: str, scopes: list[str] | None = None,
               callback_secret: str | None = None, max_budget: Budget | None = None,
               person: str | None = None) -> tuple[str, ApiToken]:
        check_token_name(name)
        check_relay_scopes(scopes or ["admin"])  # --- relay
        raw = PREFIX + secrets.token_urlsafe(32)
        tok = ApiToken(name=name, token_hash=hash_token(raw), scopes=scopes or ["admin"],
                       callback_secret=callback_secret, max_budget=max_budget, person=person)  # --- identity
        self.store.put(tok)
        return raw, tok

    def list(self) -> list[ApiToken]:
        return sorted(self.store.list(ApiToken), key=lambda t: t.created_at)

    def revoke(self, token_id: str) -> None:
        self.store.delete(ApiToken, token_id)

    def identify(self, raw: str | None) -> tuple[str, str] | None:
        """(actor name, token id) for a valid admin or `user` token. The admin token's id is 'admin'.

        Used for OAuth consent: the grant acts as this token (--- identity: a member's grant sees their own work),
        so service tokens (`mcp`, `jobs`...) are refused."""
        p = self.principal(raw)
        return (p.name, p.id) if p and (p.admin or "user" in p.scopes) else None

    def principal(self, raw: str | None) -> Principal | None:
        if not raw:
            return None
        h = hash_token(raw)
        if self._admin_hash and hmac.compare_digest(h, self._admin_hash):
            return self._of(None)
        for tok in self.store.list(ApiToken):
            if hmac.compare_digest(h, tok.token_hash):
                if now() - (tok.last_used or 0) >= 60:  # perf: not a database write on every request
                    tok.last_used = now()
                    self.store.put(tok)
                return self._of(tok)
        return None

    def _of(self, tok: ApiToken | None) -> Principal | None:
        """--- identity: a token's principal, with its person. None (the server's admin token) and tokens minted
        before people belong to people.OWNER. A removed person's tokens stop working; a member's never hold
        more than people.MEMBER_SCOPES, whatever they were minted with."""
        from hangar import people

        person = people.get(self.store, people.token_person(tok.person if tok else None))
        if person is None:
            return None
        if tok is None:
            return Principal("admin", "admin", ["admin"], person=person.id, role=person.role)
        scopes = list(tok.scopes) if person.role == "admin" else [s for s in tok.scopes if s in people.MEMBER_SCOPES]
        return Principal(tok.name, tok.id, scopes, tok.callback_secret, tok.max_budget, person.id, person.role)

    # --- front-door-auth
    def valid(self, raw: str | None) -> bool:
        """Whether `raw` is a live token, without touching last_used (the body-size guard asks before the route)."""
        return self.scopes_of(raw) is not None

    def scopes_of(self, raw: str | None) -> list[str] | None:
        """A live token's scopes, without touching last_used (--- overnight/sweep: the body-size guard sizes the cap
        by what the token may reach)."""
        if not raw:
            return None
        h = hash_token(raw)
        if self._admin_hash and hmac.compare_digest(h, self._admin_hash):
            return ["admin"]
        tok = next((t for t in self.store.list(ApiToken) if hmac.compare_digest(h, t.token_hash)), None)
        p = self._of(tok) if tok else None  # --- identity: as the routes will see it
        return p.scopes if p else None

    def by_id(self, token_id: str | None) -> Principal | None:
        """--- identity: the principal of a token by its id (an OAuth grant acts as the token that approved it)."""
        if token_id == "admin":
            return self._of(None) if self._admin_hash else None
        tok = self.store.get(ApiToken, token_id) if token_id else None
        return self._of(tok) if tok else None

    def exists(self, token_id: str | None) -> bool:
        if token_id == "admin":
            return self._admin_hash is not None
        return bool(token_id) and self.store.get(ApiToken, token_id) is not None

    def verify_scopes(self, raw: str | None) -> tuple[str, list[str]] | None:
        """(actor name, scopes) for a valid token, else None."""
        p = self.principal(raw)
        return (p.name, p.scopes) if p else None

    def verify(self, raw: str | None, scope: str = "admin") -> str | None:
        """Return the actor name for a valid token that holds `admin` (or `scope`), else None."""
        res = self.verify_scopes(raw)
        if res is None:
            return None
        name, scopes = res
        return name if "admin" in scopes or scope in scopes else None

    def ensure_bootstrap(self) -> str | None:
        """If no admin token is configured and no tokens exist, mint one and return it (shown once)."""
        if self._admin_hash or self.store.list(ApiToken):
            return None
        raw, _ = self.create("bootstrap-admin")
        return raw


# ---------------------------------------------------------------------------- --- relay: delegated identity
class DelegationRefused(identity.Forbidden):
    """A relay claim hangar won't act on. The MCP endpoint answers 403 with the text."""


class Claim(NamedTuple):
    source: str
    id: str
    reply_to: str | None = None

    @property
    def key(self) -> str:
        return f"{self.source}:{self.id}"


def parse_claim(value: str | Mapping[str, Any]) -> Claim:
    """An `X-Hangar-On-Behalf-Of` value: `symphony:<user id>`, or JSON `{"source", "id", "reply_to"?}`. Raises
    DelegationRefused for anything else, including a source no relay may vouch for."""
    from hangar import people

    data: Any = value
    if isinstance(value, str):
        if len(value) > _MAX_CLAIM:
            raise DelegationRefused(f"{RELAY_HEADER} is longer than {_MAX_CLAIM} characters")
        text = value.strip()
        if text[:1] in ("{", "["):
            try:
                data = json.loads(text)
            except ValueError as e:
                raise DelegationRefused(f"{RELAY_HEADER} isn't valid JSON") from e
        else:
            src, _, ext = text.partition(":")
            data = {"source": src, "id": ext}
    if not isinstance(data, Mapping) or set(data) - {"source", "id", "reply_to"}:
        raise DelegationRefused(f"{RELAY_HEADER} must be source:id or a JSON object with source, id and reply_to")
    source = str(data.get("source") or "").strip().lower()
    if source not in people.RELAY_SOURCES:
        raise DelegationRefused(f"a relay vouches only for {', '.join(people.RELAY_SOURCES)} accounts, not "
                                f"'{source}' (hangar's own doors verify GitHub, Slack, Jira and email accounts)")
    ext = data.get("id")
    if isinstance(ext, bool) or not isinstance(ext, str | int):
        raise DelegationRefused(f"{RELAY_HEADER}: id must be a string or a number")
    try:
        key = people.external_key(source, ext)
    except people.PeopleError as e:
        raise DelegationRefused(f"{RELAY_HEADER}: {e}") from e
    reply_to = data.get("reply_to")
    if reply_to is not None and not (isinstance(reply_to, str) and _REPLY_TO_RE.match(reply_to)
                                     and reply_to.startswith(f"{source}:")):
        raise DelegationRefused(f"{RELAY_HEADER}: reply_to must be '{source}:<stream id>[:<message id>]'")
    return Claim(source, key.split(":", 1)[1], reply_to)


def resolve_delegation(auth: TokenAuth, who: Principal | None, headers: Mapping[str, str], *,
                       unlinked: str = "refuse", audit: Callable[..., Any] | None = None) -> Principal | None:
    """The principal an MCP request runs as (`MCPAuthMiddleware` calls this with the token's principal and the
    request's lowercase headers, before its scope check).

    * No `X-Hangar-On-Behalf-Of`: `who` unchanged, except that a relay token is refused (it acts only for someone).
    * The header with any other token, or none: refused / None (the 401 path), so a claim is never silently dropped.
    * A relay token's claim for a linked account: that person's principal, `mcp` scope only, `via` the relay.
    * For an account linked to nobody: refused, or (`unlinked="approve"`, and only once the submit path holds such
      jobs: `approvals.HOLD_SUBMIT`) a held principal with no person that may only submit work for approval.

    Raises DelegationRefused, after `audit(actor, "relay.refused", target, why=...)` when `audit` is given."""
    from hangar import approvals, people

    raw = headers.get(RELAY_HEADER)
    if who is None:
        return None
    if raw is None:
        if who.relay:
            _refuse(audit, who, None, f"token '{who.name}' is a relay token: it acts only on behalf of someone, named "
                                      f"in {RELAY_HEADER}")
        return who
    if who.via is not None:  # already delegated: never twice
        _refuse(audit, who, None, "a delegated request can't delegate again")
    if not who.relay:
        _refuse(audit, who, None, f"{RELAY_HEADER} needs a relay token (scope relay, minted by an admin); token "
                                  f"'{who.name}' has {sorted(who.scopes)}")
    try:
        claim = parse_claim(raw)
    except DelegationRefused as e:
        _refuse(audit, who, None, str(e))
    via = f"relay:{who.name}"
    may_approve = "relay_approve" in who.scopes
    person = people.resolve_external(auth.store, claim.source, claim.id)
    if person is None:
        if unlinked not in RELAY_UNLINKED:
            _refuse(audit, who, claim, f"HANGAR_RELAY_UNLINKED is '{unlinked}', not one of {', '.join(RELAY_UNLINKED)}")
        hint = f"an admin links it with `hangar person link <person> {claim.source} {claim.id}`"
        if unlinked != "approve":
            _refuse(audit, who, claim, f"{claim.key} isn't linked to a hangar person; {hint}")
        if not approvals.HOLD_SUBMIT:
            _refuse(audit, who, claim, f"{claim.key} isn't linked to a hangar person, and this hangar can't hold a "
                                       f"relayed job for approval yet; {hint}")
        return Principal(via, who.id, ["mcp"], person=None, role="member", via=via, on_behalf_of=claim.key,
                         reply_to=claim.reply_to, held=True)
    return Principal(via, who.id, ["mcp"], person=person.id, role=person.role, via=via, on_behalf_of=claim.key,
                     reply_to=claim.reply_to, may_approve=may_approve)


def _refuse(audit: Callable[..., Any] | None, who: Principal, claim: Claim | None, why: str) -> NoReturn:
    log.info("relay refused for %s: %s", who.name, why)
    if audit is not None:
        audit(identity.actor_name(who), "relay.refused", claim.key if claim else who.id, why=why)
    raise DelegationRefused(why)


def bearer_from_headers(headers) -> str | None:
    auth = headers.get("authorization") or headers.get("Authorization")
    if auth and auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return None
