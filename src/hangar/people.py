"""People: who on the team owns which tokens, credentials, sandboxes, sessions and jobs (product P10, B5).

One hangar serves a team. A token belongs to a person, and a person is an `admin` (sees and acts on everything,
manages people and the vault) or a `member` (sees and acts on their own work only). Records carry the owner's
person id in `person`; that is also the key per-person quotas use (`identity.person()` for the caller).

* **Migration.** Tokens minted before people existed have no person: they belong to `OWNER`, an admin, as do the
  server's own admin token and the bootstrap token. Records made before have no person, so only admins see them.
* **Credentials.** A credential with a `person` is that person's own (their Claude or Codex login) and only ever
  goes into their sandboxes; one without is a team credential. Those stored before people became the owner's
  (teamcreds.migrate). A sandbox for a person who has their own credential for its harness uses that one instead
  of the team's (teamcreds.resolve).
* **Linked identities.** A person's GitHub user id, Slack workspace/user id and Jira account id or email: those
  accounts act as the person (`resolve_external`): trusted, their caps and login, visible to them, and only they
  continue their jobs from a front door (docs/team.md#who-is-trusted).
* **Visibility.** Enforced at the front doors (MCP tools and resources, REST routes), not inside the services:
  the job and session machinery runs work for everyone and must see all of it.
"""

from __future__ import annotations

import inspect
import re
from typing import TYPE_CHECKING, Any

import hangar.identity as identity
from hangar.models import ApiToken, Credential, Person, ScheduledJob, Session, Snapshot

if TYPE_CHECKING:
    from hangar.store import Store

OWNER = "owner"  # the admin person legacy tokens, the server's admin token and the bootstrap token belong to
ROLES = ("admin", "member")
MEMBER_SCOPES = ("user", "mcp", "jobs")  # what a member's tokens may hold: never admin, metrics or sandboxes
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,39}$")


class PeopleError(ValueError):
    pass


# ---------------------------------------------------------------------------- people
def check_id(person_id: str) -> None:
    if not _ID_RE.match(person_id or ""):
        raise PeopleError(f"person id '{person_id}' must be lowercase letters, digits, '.', '_' or '-' (≤40 chars)")


def get(store: Store, person_id: str | None) -> Person | None:
    """The person, or None. OWNER exists even before anyone was added (it holds the legacy tokens)."""
    if not person_id:
        return None
    p = store.get(Person, person_id)
    if p is None and person_id == OWNER:
        return Person(id=OWNER, name="owner", role="admin")
    return p


def list_people(store: Store) -> list[Person]:
    people = {p.id: p for p in store.list(Person)}
    people.setdefault(OWNER, Person(id=OWNER, name="owner", role="admin"))
    return sorted(people.values(), key=lambda p: (p.id != OWNER, p.id))


def add(store: Store, person_id: str, name: str = "", role: str = "member") -> Person:
    check_id(person_id)
    if role not in ROLES:
        raise PeopleError(f"role must be one of {', '.join(ROLES)}")
    if person_id == OWNER or store.get(Person, person_id) is not None:
        raise PeopleError(f"person '{person_id}' already exists")
    p = Person(id=person_id, name=name or person_id, role=role)  # type: ignore[arg-type]
    store.put(p)
    return p


def set_role(store: Store, person_id: str, role: str) -> Person:
    p = get(store, person_id)
    if p is None:
        raise KeyError(f"no person '{person_id}'")
    if role not in ROLES:
        raise PeopleError(f"role must be one of {', '.join(ROLES)}")
    if person_id == OWNER and role != "admin":
        raise PeopleError("the owner holds the server's admin token and stays an admin")
    p.role = role  # type: ignore[assignment]
    store.put(p)
    return p


def remove(store: Store, person_id: str) -> list[str]:
    """Delete a person and revoke their tokens (returned). Their records and credentials stay, now admin-only
    and unusable respectively, until an admin reassigns or deletes them."""
    if person_id == OWNER:
        raise PeopleError("the owner holds the server's admin token and legacy tokens; it can't be removed")
    if store.get(Person, person_id) is None:
        raise KeyError(f"no person '{person_id}'")
    revoked = [t.id for t in store.list(ApiToken) if t.person == person_id]
    for t in revoked:
        store.delete(ApiToken, t)
    # --- review: their schedules would keep starting jobs as them, with their own credentials
    for s in store.list(ScheduledJob):
        if s.person == person_id and s.enabled:
            s.enabled, s.last_error = False, f"paused: {person_id} was removed"
            store.put(s)
    store.delete(Person, person_id)
    return revoked


def token_person(tok_person: str | None) -> str:
    return tok_person or OWNER


def check_token_scopes(person: Person, scopes: list[str]) -> None:
    """A member's tokens may hold only MEMBER_SCOPES."""
    if person.role != "admin" and (bad := [s for s in scopes if s not in MEMBER_SCOPES]):
        raise PeopleError(f"{person.id} is a member; their tokens may hold {', '.join(MEMBER_SCOPES)}, not "
                          f"{', '.join(bad)}")


# ---------------------------------------------------------------------------- linked identities (--- teamcreds)
SOURCES = ("github", "slack", "jira", "email")
# What intake may call a source: a GitHub Projects item's actor is a GitHub user
_SOURCE_ALIASES = {"github_projects": "github", "github-projects": "github"}
_EXTERNAL_RE = {
    # --- review: a GitHub user's numeric id, never the login: a renamed login can be registered by anyone, who
    # would then act as this person (their trust, budget and own login). The REST link route looks a login up.
    "github": re.compile(r"^[1-9][0-9]{0,19}$"),
    # --- review: a Slack user id is unique only within its workspace (Slack's user object: use it "together with
    # team_id as a unique key"), and a Slack Connect channel carries other organizations' users
    "slack": re.compile(r"^[TE][A-Z0-9]{1,30}/[UW][A-Z0-9]{1,30}$"),
    "jira": re.compile(r"^[A-Za-z0-9][A-Za-z0-9.:_-]{0,127}$"),  # an Atlassian account id; a Data Center user key
    "email": re.compile(r"^[^@\s]{1,64}@[^@\s]{1,253}\.[^@\s]{2,63}$"),
}
_EXTERNAL_HINT = {"github": "a GitHub user id (a number; hangar person link looks one up from a login)",
                  "slack": "a Slack workspace and user id (like T0123ABCD/U012AB3CD)",
                  "jira": "a Jira (Atlassian) account id", "email": "an email address"}


def external_key(source: str, external_id: str | int) -> str:
    """`source:id`, normalized the way each source compares ids (emails ignore case)."""
    src = (source or "").strip().lower()
    src = _SOURCE_ALIASES.get(src, src)
    if src not in SOURCES:
        raise PeopleError(f"source must be one of {', '.join(SOURCES)}")
    v = str(external_id if external_id is not None else "").strip()
    v = v.lower() if src == "email" else v.upper() if src == "slack" else v
    if not _EXTERNAL_RE[src].match(v):
        raise PeopleError(f"'{external_id}' isn't {_EXTERNAL_HINT[src]}")
    return f"{src}:{v}"


def _linked_to(store: Store, key: str) -> Person | None:
    return next((p for p in list_people(store) if key in p.links), None)


def link(store: Store, person_id: str, source: str, external_id: str) -> Person:
    """Link an account elsewhere to a person (admins). One account links to one person."""
    key = external_key(source, external_id)
    p = get(store, person_id)
    if p is None:
        raise KeyError(f"no person '{person_id}'")
    other = _linked_to(store, key)
    if other is not None and other.id != p.id:
        raise PeopleError(f"{key} is linked to {other.id}; unlink it there first (hangar person unlink {other.id} "
                          f"{key.replace(':', ' ', 1)})")
    if key not in p.links:
        p.links.append(key)
        store.put(p)
    return p


def unlink(store: Store, person_id: str, source: str, external_id: str) -> Person:
    key = external_key(source, external_id)
    p = get(store, person_id)
    if p is None or key not in p.links:
        raise KeyError(f"{key} isn't linked to '{person_id}'")
    p.links.remove(key)
    store.put(p)
    return p


def resolve_external(store: Store, source: str, external_id: str | int | None) -> Person | None:
    """The person an account elsewhere is linked to, for a front door to attribute a job (`Caller(...,
    person=p.id)`): a GitHub user's numeric id (the payload's `sender.id`, never the login), a Slack `TEAM/USER`
    (the poster's own workspace: `user_team` in a Slack Connect channel), a Jira account id (Data Center: the user
    key) or an email. None for anyone linked to nobody, and for an id that isn't one. A linked account acts as its
    person (docs/team.md#who-is-trusted); anyone else's work runs for the team under the front door's own trust
    rule. A Jira id with an `@` is looked up as an email."""
    if external_id is None or external_id == "" or isinstance(external_id, bool):
        return None
    external_id = str(external_id)
    if (source or "").strip().lower() == "jira" and "@" in external_id:
        source = "email"
    try:
        key = external_key(source, external_id)
    except PeopleError:
        return None
    return _linked_to(store, key)


# ---------------------------------------------------------------------------- visibility
def visible(person: str | None) -> bool:
    """Whether the current caller may see and act on a record owned by `person`."""
    p = identity.current.get()
    return p is None or p.role == "admin" or (person is not None and person == p.person)


def usable(creds: list[Credential]) -> list[Credential]:
    """The credentials the current caller may attach: the team's shared ones and their own (all, for admins)."""
    p = identity.current.get()
    if p is None or p.role == "admin":
        return creds
    return [c for c in creds if c.person is None or c.person == p.person]


def scope() -> str | None:
    """--- durable: `mine` as a store filter: None for everything (admins, in-process callers), else the person
    whose records the caller sees ("" when the caller has none, which matches nothing)."""
    p = identity.current.get()
    if p is None or p.role == "admin":
        return None
    return p.person or ""


def mine(records: list[Any]) -> list[Any]:
    """The records (with a `person`) the current caller may see."""
    p = identity.current.get()
    if p is None or p.role == "admin":
        return records
    return [r for r in records if r.person is not None and r.person == p.person]


class Guard:
    """Checks the sandboxes, sessions, jobs and schedules an MCP tool call names before the tool runs. A record
    the caller may not see is reported exactly as a missing one, so its existence doesn't leak.

    Arguments are recognized by name, so a new tool with a `sandbox` argument is covered without a change here.
    `NOT_REFS` lists the arguments that share a name but mean something else (a terminal's tmux session, the
    environment a clone extends)."""

    REFS = {"sandbox": "sandbox", "sandboxes": "sandboxes", "source": "source", "session": "session",
            "job": "job", "ref": "ref", "schedule": "schedule"}
    NOT_REFS = {"terminal_open": {"session"}, "terminal_send": {"session"}, "terminal_read": {"session"},
                "terminal_close": {"session"}, "clone_environment": {"source"},
                # --- approvals (#121): approvals.decide checks these itself (a repo's approvers see its held jobs)
                "approve_job": {"job"}, "reject_job": {"job"}}

    def __init__(self, fleet: Any, sessions: Any = None, jobs: Any = None, triggers: Any = None):
        self.fleet, self.sessions, self.jobs, self.triggers = fleet, sessions, jobs, triggers

    def args(self, tool: str, sig: inspect.Signature, a: tuple, kw: dict[str, Any]) -> None:
        p = identity.current.get()
        if p is None or p.role == "admin":
            return
        try:
            bound = sig.bind_partial(*a, **kw).arguments
        except TypeError:
            bound = kw
        skip = self.NOT_REFS.get(tool, set())
        for name, value in bound.items():
            kind = self.REFS.get(name)
            if kind and name not in skip and value:
                for v in value if isinstance(value, list) else [value]:
                    self.check(kind, str(v))

    def check(self, kind: str, ref: str) -> None:
        from hangar.fleet import FleetError

        if kind in ("sandbox", "sandboxes"):
            if not visible(self.fleet.get(ref).person):
                raise FleetError(f"no sandbox '{ref}'")
        elif kind == "source":
            snap = self.fleet.store.get(Snapshot, ref)
            if snap is not None:
                if not visible(snap.person):
                    raise FleetError(f"no sandbox '{ref}'")
            elif not visible(self.fleet.get(ref).person):
                raise FleetError(f"no sandbox '{ref}'")
        elif kind == "session" and self.sessions is not None:
            self.session(self.sessions.get(ref), ref)
        elif kind == "job" and self.jobs is not None:
            self.job(ref)
        elif kind == "ref" and self.sessions is not None:  # a job id or a session name
            from hangar.jobs import JobNotFound

            try:
                job = self.jobs.get(ref) if self.jobs is not None else None
            except JobNotFound:
                job = None
            if job is not None:
                self.job(ref)
            else:
                self.session(self.sessions.get(ref), ref)
        elif kind == "schedule" and self.triggers is not None:
            if not visible(self.triggers.get_schedule(ref).person):
                raise KeyError(f"no schedule '{ref}'")

    def session(self, s: Session, ref: str) -> None:
        from hangar.sessions import SessionNotFound

        if not visible(s.person):
            raise SessionNotFound(f"no session '{ref}'")

    def job(self, ref: str) -> None:
        from hangar.jobs import JobNotFound

        if not visible(self.jobs.get(ref).person):
            raise JobNotFound(f"no job '{ref}'")


def guarded(fn: Any, guard: Guard) -> Any:
    """`fn` (an MCP tool, sync or async) with `guard` checking the records its arguments name first."""
    import functools

    name, sig = fn.__name__, inspect.signature(fn)
    if inspect.iscoroutinefunction(fn):
        @functools.wraps(fn)
        async def awrapped(*a, **kw):
            guard.args(name, sig, a, kw)
            return await fn(*a, **kw)
        return awrapped

    @functools.wraps(fn)
    def wrapped(*a, **kw):
        guard.args(name, sig, a, kw)
        return fn(*a, **kw)
    return wrapped


# ---------------------------------------------------------------------------- environments
def claim_environment(store: Store, catalog: Any, data: dict[str, Any]) -> None:
    """A member may create environments and change the ones they created, never anyone else's: an environment's
    MCP servers and setup run in every sandbox started from it, a teammate's included."""
    name = str(data.get("name") or "")
    key = f"identity:env-owner:{name}"
    p = identity.current.get()
    if p is None or not name:
        return
    if p.role != "admin" and name in catalog.names() and store.kv_get(key) != p.person:
        raise identity.Forbidden(f"environment '{name}' isn't yours to change; save it under a new name "
                                 f"(extends: {name})")
    if name not in catalog.names() and p.person:
        store.kv_set(key, p.person)


# ---------------------------------------------------------------------------- credentials
def credentials_for(store: Store, names: list[str], person: str | None, harness: str | None, explicit: bool,
                    is_agent: Any, mode: str = "person_then_team") -> list[str]:
    """The credentials a sandbox owned by `person` gets (--- teamcreds: teamcreds.resolve, which fleet.create
    calls through teamcreds.for_sandbox so it can wait for a leased login).

    `names` is what the caller passed (`explicit`) or the environment's list for the selected harness. Another
    person's credential is refused when named, and dropped from an environment's list. When the person has their
    own credentials for the sandbox's agent harness, those replace the team's for it."""
    from hangar.teamcreds import resolve

    return resolve(store, names, person, harness, explicit, is_agent, mode).names


def check_secrets(store: Store, secrets: dict[str, str], person: str | None) -> None:
    """`secrets:` names credentials outside credentials_for: refuse another person's own one (--- review)."""
    from hangar.broker import CredentialError

    for var, name in secrets.items():
        c = store.get(Credential, name)
        if c is not None and c.person is not None and c.person != person:
            raise CredentialError(f"secrets: {var}: credential '{name}' is {c.person}'s own and goes only into "
                                  "their sandboxes")
