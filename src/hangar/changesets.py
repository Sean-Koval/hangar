"""Change sets: one ticket's work across several repos, delivered as one PR per changed repo, landed in order, then
deployed through the trigger repo (docs/changesets.md).

A **profile** (`hangar.changeset-profile/v1`, admin config; kv `changeset-profile:<name>`) names the repos by role,
the order they land in and the deploy step:

    schema: hangar.changeset-profile/v1
    name: payments-bot
    environment: work-multirepo
    repos:
      infra:    {repo: https://github.com/acme/payments-infra.git}
      database: {repo: https://github.com/acme/payments-db.git}
      code:     {repo: https://github.com/acme/payments-bot.git}
      deploy:   {repo: https://github.com/acme/payments-deploy.git, agent: read}
    land_order: [database, infra, code]
    deploy:
      role: deploy
      after: [database, infra, code]
      ready: {kind: image-ready, image: registry.example.com/payments-bot, tag: "{code.merge_sha}"}
      apply: {kind: command, run: "./bump.sh dev {code.merge_sha}"}
      environments: [dev]

A job submitted with `changeset: <profile>` is the **parent**: one sandbox and one agent session holding every
role's checkout at `/workspace/<role>`, on each repo's branch (its policy's `branch_template`, default
`hangar/<job>`). Its fetch grant names every role's repo; it never holds a push grant. At the end of a turn hangar
checks each changed role (its repo policy's `verify`, in its checkout), scans it for secrets and pushes it with a
delivery grant pinned to that repo, ref and commit, then opens one PR per changed role as a **part job**
(`source: changeset`; it never runs an agent), each PR listing its siblings and ending with a
`Hangar-Changeset:` trailer. Each part lands under its own repo's policy (approvers, CODEOWNERS, self-approval,
taint) with one more gate: every earlier changed role in `land_order` has merged. The parent's sandbox is
checkpointed away before any part arms.

Once every changed role has merged the set is `deploying`: the control plane polls the deploy step's `ready`
deliverer, then runs its `apply` deliverer in a fresh sandbox on the deploy repo (no agent), pushes the change and
opens the deploy PR as one more part under the deploy repo's own policy. Its merge, the company CD's input, makes the
set `done`. A part closed without merging (or reverted, as the nightly pass sees it) makes it `broken`: later parts
disarm, the deploy step never runs, and `changeset.broken` is announced. Nothing is reverted automatically.

Not in v1: an atomic cross-repo merge, rollback, several deploy environments per set, best-of-N over a set. Session
checkpoints hold the primary role's checkout; the other roles resume from the branches hangar pushed for them.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import hashlib
import json
import logging
import re
import shlex
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from hangar.ext.deliverers import DeliverCtx, DeliverError, Merged, Ready, ticket_key
from hangar.ext.deliverers import get as get_deliverer
from hangar.models import ChangeSet, ChangeSetRole, DeployState, Job, JobState, Sandbox, Session, new_id, now

if TYPE_CHECKING:
    from hangar.fleet import Fleet
    from hangar.jobs import JobService, SubmitJob
    from hangar.providers.base import Provider

log = logging.getLogger("hangar.changesets")

SCHEMA = "hangar.changeset-profile/v1"
PROFILE_PREFIX = "changeset-profile:"
SET_PREFIX = "changeset:"
LABEL = "hangar.changeset"  # on a change set's jobs and sandboxes (shown, never trusted: sandbox_roles checks)
ROLE_LABEL = "hangar.changeset.role"
POLL_SECONDS = 60.0  # how often the deploy step's `ready` is asked
MAX_DEPLOY_ATTEMPTS = 3  # an `apply` that fails this often makes the set broken
DEPLOY_TTL_MINUTES = 30
ACTIVE = ("working", "landing", "deploying")
_ROLE = re.compile(r"^[a-z][a-z0-9-]{0,30}$")
_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")
_TICKET = re.compile(r"^[a-z][a-z0-9-]{0,30}:[^\s]{1,200}$")
_SLUG = re.compile(r"[^A-Za-z0-9._-]+")


class ChangeSetError(ValueError):
    pass


# ====================================================================== profiles
class RoleSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    repo: str = Field(min_length=1, max_length=500)
    agent: Literal["write", "read"] = "write"  # read: cloned for the agent to read; never delivered
    base_branch: str | None = Field(None, max_length=200)


class StepSpec(BaseModel):
    """A deliverer and its own settings (`kind` plus whatever that kind reads)."""

    model_config = ConfigDict(extra="allow")
    kind: str = Field(min_length=1, max_length=63)


class DeploySpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: str
    after: list[str] | None = None  # default: every role in land_order
    ready: StepSpec | None = None  # default: ready at once
    apply: StepSpec
    environments: list[str] = Field(default_factory=list, max_length=1)  # v1: one; promotion stays the company's


class Profile(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    schema_: Literal["hangar.changeset-profile/v1"] = Field(SCHEMA, alias="schema")
    name: str
    environment: str | None = None  # the parent job's environment (one image with every role's tools)
    primary: str | None = None  # the role whose repo is the parent job's `repo`: default `code`, else the last landed
    repos: dict[str, RoleSpec] = Field(min_length=1, max_length=8)
    land_order: list[str] = Field(min_length=1, max_length=8)
    deploy: DeploySpec | None = None
    updated_at: float | None = None
    updated_by: str | None = None

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        if not _NAME.match(v):
            raise ValueError(f"a profile name matches {_NAME.pattern}")
        return v

    @model_validator(mode="after")
    def _shape(self) -> Profile:
        if bad := [r for r in self.repos if not _ROLE.match(r)]:
            raise ValueError(f"role names match {_ROLE.pattern}: {', '.join(bad)}")
        keys = [_repo_key(s.repo) for s in self.repos.values()]
        if len(set(keys)) != len(keys):
            raise ValueError("each role names a different repo")
        writable = [r for r, s in self.repos.items() if s.agent == "write"]
        if len(set(self.land_order)) != len(self.land_order):
            raise ValueError("land_order names a role twice")
        if unknown := [r for r in self.land_order if r not in self.repos]:
            raise ValueError(f"land_order names roles that aren't in repos: {', '.join(unknown)}")
        if ro := [r for r in self.land_order if self.repos[r].agent != "write"]:
            raise ValueError(f"land_order names read-only roles: {', '.join(ro)}")
        if missing := [r for r in writable if r not in self.land_order]:
            raise ValueError(f"every role the agent writes lands in land_order; missing: {', '.join(missing)}")
        if self.primary is None:
            self.primary = "code" if "code" in writable else self.land_order[-1]
        if self.primary not in writable:
            raise ValueError(f"primary role {self.primary!r} must be a role the agent writes")
        if d := self.deploy:
            if d.role not in self.repos:
                raise ValueError(f"deploy.role {d.role!r} isn't in repos")
            if self.repos[d.role].agent == "write" or d.role in self.land_order:
                raise ValueError(f"deploy.role {d.role!r} is changed by its step, never by the agent: give it "
                                 "`agent: read` and leave it out of land_order")
            if d.after is not None and (bad := [r for r in d.after if r not in self.land_order]):
                raise ValueError(f"deploy.after names roles that don't land: {', '.join(bad)}")
            for step in (d.ready, d.apply):
                if step is not None and not _NAME.match(step.kind):
                    raise ValueError(f"a deliverer kind matches {_NAME.pattern}: {step.kind!r}")
        return self

    def digest(self) -> str:
        """sha256 over the profile's content (not who changed it when), recorded on each change set."""
        body = self.model_dump(mode="json", by_alias=True, exclude={"updated_at", "updated_by"})
        return "sha256:" + hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def deploy_after(self) -> list[str]:
        return list(self.deploy.after) if self.deploy and self.deploy.after is not None else list(self.land_order)


def parse_profile(data: Any) -> Profile:
    """A profile from YAML/JSON data, or ChangeSetError naming the first problem."""
    try:
        return Profile.model_validate(data)
    except ValidationError as e:
        err = e.errors()[0]
        loc = ".".join(map(str, err["loc"]))
        raise ChangeSetError(f"{loc + ': ' if loc else ''}{err['msg'].removeprefix('Value error, ')}") from None


def _repo_key(repo: str) -> str:
    from hangar.repopolicy import repo_key

    return repo_key(repo) or repo


class Profiles:
    def __init__(self, svc: JobService):
        self.svc = svc
        self.store = svc.store

    def get(self, name: str) -> Profile | None:
        raw = self.store.kv_get(PROFILE_PREFIX + name)
        if raw is None:
            return None
        try:
            return Profile.model_validate_json(raw)
        except ValidationError as e:
            log.warning("ignoring the unreadable change-set profile %s: %s", name, e)
            return None

    def list(self) -> list[Profile]:
        return [p for k in sorted(self.store.kv_keys(PROFILE_PREFIX)) if (p := self.get(k[len(PROFILE_PREFIX):]))]

    def put(self, data: Any, actor: str) -> Profile:
        p = parse_profile(data if not isinstance(data, Profile) else data.model_dump(by_alias=True))
        env = p.environment
        if env:
            try:
                self.svc.fleet.catalog.get(env)
            except KeyError:
                raise ChangeSetError(f"no environment '{env}'") from None
        if p.deploy:
            for step in (p.deploy.ready, p.deploy.apply):
                if step is not None:
                    try:
                        get_deliverer(step.kind)
                    except DeliverError as e:
                        raise ChangeSetError(str(e)) from None
        p.updated_at, p.updated_by = now(), actor
        self.store.kv_set(PROFILE_PREFIX + p.name, p.model_dump_json(by_alias=True), durable=True)
        self.svc.fleet.audit(actor, "changeset.profile.set", p.name, digest=p.digest())
        return p

    def delete(self, name: str, actor: str) -> bool:
        if self.store.kv_get(PROFILE_PREFIX + name) is None:
            return False
        self.store.kv_delete(PROFILE_PREFIX + name, durable=True)
        self.svc.fleet.audit(actor, "changeset.profile.delete", name)
        return True


# ====================================================================== change sets (records)
class ChangeSets:
    """The records, in the kv table, cached by id: one instance per fleet (`records`), shared by the job service and
    the fleet's sandbox setup, so a record read and saved without an await between is never torn."""

    def __init__(self, store: Any):
        self.store = store
        self._cache: dict[str, ChangeSet] = {}

    def get(self, cs_id: str | None) -> ChangeSet | None:
        if not cs_id:
            return None
        if (cs := self._cache.get(cs_id)) is not None:
            return cs
        raw = self.store.kv_get(SET_PREFIX + cs_id)
        if raw is None:
            return None
        cs = ChangeSet.model_validate_json(raw)
        self._cache[cs_id] = cs
        return cs

    def save(self, cs: ChangeSet) -> None:
        cs.updated_at = now()
        self._cache[cs.id] = cs
        self.store.kv_set(SET_PREFIX + cs.id, cs.model_dump_json(), durable=True)

    def list(self, state: str | None = None) -> list[ChangeSet]:
        out = [cs for k in self.store.kv_keys(SET_PREFIX) if (cs := self.get(k[len(SET_PREFIX):])) is not None]
        return sorted((c for c in out if state is None or c.state == state), key=lambda c: -c.created_at)

    def of(self, job: Job) -> ChangeSet | None:
        return self.get(job.changeset) if job.changeset else None


def records(fleet: Fleet) -> ChangeSets:
    rec = fleet.__dict__.get("_changesets")
    if rec is None:
        rec = fleet.__dict__["_changesets"] = ChangeSets(fleet.store)
    return rec


def of(svc: JobService) -> ChangeSets:
    return records(svc.fleet)


def profiles(svc: JobService) -> Profiles:
    return Profiles(svc)


def load_profile(svc: JobService | None, cs: ChangeSet) -> Profile:
    """The profile as it was when the set was made (its record keeps a copy: a later edit changes new sets only)."""
    return Profile.model_validate(cs.profile_spec)


# ====================================================================== templates (repo policy, G7)
TEMPLATE_KEYS = ("job", "ticket", "ticket_key", "title", "role")
_TPL = re.compile(r"\{([a-z_]+)\}")


def check_template(t: str | None, what: str, need_job: bool = False) -> str | None:
    if t is None:
        return None
    if bad := [k for k in _TPL.findall(t) if k not in TEMPLATE_KEYS]:
        raise ValueError(f"{what}: unknown placeholder(s) {', '.join('{' + b + '}' for b in bad)} "
                         f"(known: {', '.join('{' + k + '}' for k in TEMPLATE_KEYS)})")
    if need_job and "{job}" not in t:
        raise ValueError(f"{what} must contain {{job}}: a job only ever pushes a new branch of its own")
    return t


def render(template: str, job_id: str, ticket: str | None, title: str = "", role: str = "") -> str:
    key = ticket_key(ticket) or ""
    vals = {"job": job_id, "ticket": ticket or "", "ticket_key": key, "title": title, "role": role}
    return _TPL.sub(lambda m: vals.get(m.group(1), m.group(0)), template)


def branch_for(policy: Any, job_id: str, ticket: str | None) -> tuple[str | None, str | None]:
    """(branch, note): the repo policy's `branch_template` rendered for the job, or (None, why not)."""
    t = getattr(policy, "branch_template", None) if policy is not None else None
    if not t:
        return None, None
    if "{ticket" in t and not ticket:
        return None, f"branch_template {t!r} needs a ticket; using hangar/{job_id}"
    slug = _SLUG.sub("-", ticket_key(ticket) or "").strip("-.")
    return render(t.replace("{ticket_key}", slug).replace("{ticket}", slug), job_id, ticket), None


def title_for(policy: Any, job: Job, title: str | None = None) -> str:
    t = getattr(policy, "title_template", None) if policy is not None else None
    base = title or job.title
    if not t or ("{ticket" in t and not job.ticket):
        return base
    return render(t, job.parent or job.id, job.ticket, base, job.role or "")[:250]


def check_ticket(ticket: str | None) -> str | None:
    if ticket is None:
        return None
    t = ticket.strip()
    if not _TICKET.match(t):
        raise ChangeSetError("ticket is scheme:key, e.g. jira:PAY-412 or github:owner/repo#12")
    return t


# ====================================================================== submit (jobs.py)
def prepare(svc: JobService, req: SubmitJob, restricted: bool) -> tuple[SubmitJob, Profile]:
    """A change set's submission, before the job is made: its profile, and the request with the primary repo and the
    profile's environment filled in. Refuses what a set can't do."""
    from hangar.jobs import JobError

    prof = profiles(svc).get(req.changeset or "")
    if prof is None:
        raise JobError(f"no change-set profile '{req.changeset}' (hangar changeset profiles)")
    primary = prof.repos[prof.primary or ""]
    if req.repo and _repo_key(req.repo) != _repo_key(primary.repo):
        raise JobError(f"change set {prof.name}: its primary repo is {primary.repo} (role {prof.primary}); leave "
                       "`repo` out")
    if req.strategy != "single":
        raise JobError("change sets run one attempt (best-of-N over a set isn't supported)")
    if req.deliver == "branch":
        raise JobError("change sets deliver PRs (deliver draft_pr or pr), or nothing (none)")
    if req.branch:
        raise JobError("change sets name their branches from each repo's policy; leave `branch` out")
    if restricted:  # a service token's allowlist covers every repo of the set, not only the primary
        if missing := [r for r, s in prof.repos.items() if not svc.repo_envs(s.repo)]:
            raise JobError("this token can only submit change sets whose repos are all in HANGAR_GITHUB_REPOS "
                           f"(not: {', '.join(missing)})")
    for role, s in prof.repos.items():
        p = svc.policies.get(s.repo)
        if s.agent == "write" and p is not None and getattr(p, "require_ticket", None) and not req.ticket:
            raise JobError(f"the {role} repo's policy requires a ticket (ticket='jira:KEY-1')")
    upd: dict[str, Any] = {"repo": primary.repo, "environment": req.environment or prof.environment}
    if primary.base_branch and not req.base_branch:
        upd["base_branch"] = primary.base_branch
    return req.model_copy(update=upd), prof


def create(svc: JobService, job: Job, prof: Profile) -> ChangeSet:
    """The set's record for a parent job just made (before it is queued)."""
    cs = ChangeSet(id=new_id("cs"), profile=prof.name, profile_digest=prof.digest(), ticket=job.ticket,
                   parent_job=job.id, primary=prof.primary or "", land_order=list(prof.land_order))
    for role, s in prof.repos.items():
        if role == prof.primary:
            branch = job.branch
        else:
            branch, _ = branch_for(svc.policies.get(s.repo), job.id, job.ticket)
            branch = branch or f"hangar/{job.id}"
        cs.roles[role] = ChangeSetRole(repo=s.repo, agent=s.agent, branch=branch,
                                       base_branch=job.base_branch if role == prof.primary else s.base_branch)
    if prof.deploy:
        cs.deploy = DeployState()
    cs.profile_spec = prof.model_dump(mode="json", by_alias=True)
    job.changeset = cs.id
    job.labels = {**job.labels, LABEL: cs.id}
    of(svc).save(cs)
    return cs


def created(svc: JobService, cs: ChangeSet) -> None:
    """After the parent job is stored (jobs.submit): `changeset.created`."""
    emit(svc, cs, "changeset.created", None, f"change set {cs.id} ({cs.profile}): "
         + ", ".join(r for r, v in cs.roles.items() if v.agent == "write") + "; lands " + " → ".join(cs.land_order),
         roles={r: v.repo for r, v in cs.roles.items()}, land_order=cs.land_order)


# ====================================================================== the parent's sandbox (fleet, gateway)
def sandbox_roles(fleet: Fleet, sb: Sandbox) -> ChangeSet | None:
    """The change set whose parent job this sandbox runs, or None. Labels are anyone's to set, so it is bound through
    hangar's own records: the session the sandbox was created for (by that session), and that session's job."""
    sid = sb.labels.get("hangar.session")
    if not sid or sb.created_by != f"session:{sid}":
        return None
    s = fleet.store.get(Session, sid)
    if s is None or not s.job_id:
        return None
    job = fleet.store.get(Job, s.job_id)
    if job is None or not job.changeset or job.parent:
        return None
    cs = records(fleet).get(job.changeset)
    return cs if cs is not None and cs.parent_job == job.id else None


def checkout(fleet: Fleet, role: str) -> str:
    return f"{fleet.settings.workspace_dir}/{role}"


def grant_repos(fleet: Fleet, sb: Sandbox, host: str, hosts: list[str]) -> tuple[list[str], dict[str, str]]:
    """(extra owner/repo names for the parent's fetch grant, {role: owner/repo} for every role on `host`)."""
    from hangar.gateway import parse_remote

    cs = sandbox_roles(fleet, sb)
    if cs is None:
        return [], {}
    extra: list[str] = []
    by_role: dict[str, str] = {}
    for role, r in cs.roles.items():
        remote = parse_remote(r.repo, hosts)
        if remote is None or remote[0] != host:
            sb.info.setdefault("warnings", []).append(
                f"change set {cs.id}: role {role}'s repo isn't on {host}, so the git gateway can't serve it")
            continue
        by_role[role] = remote[1]
        if role != cs.primary and remote[1] not in extra:
            extra.append(remote[1])
    return extra, by_role


async def setup_checkouts(fleet: Fleet, prov: Provider, sb: Sandbox, stage: Any = None) -> None:
    """Clone every role but the primary (fleet._setup_repo clones that) into /workspace/<role>: a write role on its
    branch (continuing it if hangar pushed it before), a read role on its base. Records each base on first boot."""
    cs = sandbox_roles(fleet, sb)
    if cs is None:
        return
    q = shlex.quote
    bases: dict[str, tuple[str | None, str | None]] = {}
    for role, r in cs.roles.items():
        if role == cs.primary:
            continue
        dest = checkout(fleet, role)
        if stage:
            stage(f"cloning {role} ({r.repo})")
        script = f"git clone -q {'--branch ' + q(r.base_branch) + ' ' if r.base_branch else ''}-- {q(r.repo)} {q(dest)}"
        script += (f" && cd {q(dest)} && git rev-parse HEAD && git rev-parse --abbrev-ref HEAD"
                   " && (git config user.email >/dev/null || git config user.email hangar@localhost)"
                   " && (git config user.name >/dev/null || git config user.name hangar)")
        if r.agent == "write":
            b = q(r.branch)
            script += f" && ((git fetch -q origin {b} 2>/dev/null && git checkout -q -B {b} FETCH_HEAD) " \
                      f"|| git checkout -q -b {b})"
        res = await prov.exec(sb, script, timeout=600)
        if res.exit_code != 0:
            from hangar.fleet import FleetError

            raise FleetError(f"change set {cs.id}: cloning role {role} failed: {(res.stderr or res.stdout)[-600:]}")
        out = res.stdout.split()
        bases[role] = (out[0] if out else None, out[1] if len(out) > 1 else None)
    changed = False
    for role, (sha, branch) in bases.items():
        rr = cs.roles[role]
        if rr.base_sha is None and sha:
            rr.base_sha, rr.base_branch, changed = sha, rr.base_branch or branch, True
    if changed:
        records(fleet).save(cs)


def prompt_section(svc: JobService, job: Job) -> str | None:
    cs = of(svc).of(job)
    if cs is None:
        return None
    lines = [f"# Repositories (change set `{cs.id}`, profile {cs.profile})", "",
             "This task spans several repositories, each checked out in its own directory:"]
    for role, r in cs.roles.items():
        how = f"on branch `{r.branch}`" if r.agent == "write" else "read-only: don't change it"
        lines.append(f"- `{checkout(svc.fleet, role)}`: {role} ({r.repo}), {how}")
    lines += ["", "Commit in each repository you change (hangar opens one PR per changed repository, linked to each "
              "other, and lands them in this order: " + " → ".join(cs.land_order) + "). Leave a repository you "
              "don't need untouched."]
    return "\n".join(lines)


# ====================================================================== verify and delivery (end of the parent's turn)
async def _sh(svc: JobService, job: Job, cmd: str, cwd: str, timeout: int = 120):
    return await svc.fleet.exec(job.sandbox_id or "", cmd, cwd=cwd, timeout=timeout, actor=f"job:{job.id}")


async def _changed(svc: JobService, job: Job, cs: ChangeSet, role: str) -> bool:
    r = cs.roles[role]
    path = checkout(svc.fleet, role)
    base = _base(job, cs, role)
    res = await _sh(svc, job, f"git status --porcelain | head -n 1; git rev-list --count {shlex.quote(base)}..HEAD",
                    path)
    out = res.stdout.splitlines()
    dirty = len(out) > 1 and bool(out[0].strip())
    count = out[-1].strip() if out else "0"
    return r.agent == "write" and (dirty or (count.isdigit() and int(count) > 0))


def _base(job: Job, cs: ChangeSet, role: str) -> str:
    r = cs.roles[role]
    if role == cs.primary:
        return job.result.base_sha or f"origin/{job.base_branch}"
    return r.base_sha or f"origin/{r.base_branch or 'HEAD'}"


async def verify_roles(svc: JobService, job: Job) -> tuple[int | None, str | None, list[str]]:
    """Each changed role but the primary (jobs.py checks that one as for any job) runs its own repo policy's
    `verify` in its checkout. (exit code, tail, failing roles) of the first that failed; (None, None, []) when
    none failed or none has a check."""
    cs = of(svc).of(job)
    if cs is None:
        return None, None, []
    failed: list[str] = []
    first: tuple[int | None, str | None] = (None, None)
    for role in cs.land_order:
        if role == cs.primary or not await _changed(svc, job, cs, role):
            continue
        p = svc.policies.get(cs.roles[role].repo)
        cmd = (p.verify if p is not None else None) or ""
        if not cmd.strip():
            continue
        svc._event(job, f"verifying {role}: {cmd}")
        res = await _sh(svc, job, cmd, checkout(svc.fleet, role), timeout=svc.settings.job_verify_timeout_seconds)
        if res.exit_code != 0:
            failed.append(role)
            if first[0] is None:
                tail = svc.broker.redact(((res.stdout or "") + (res.stderr or ""))[-3500:])
                first = (res.exit_code, f"[{role}: `{cmd}` exited {res.exit_code}]\n{tail}")
        svc._event(job, f"{role} check: {'passed' if res.exit_code == 0 else f'failed (exit {res.exit_code})'}")
    return first[0], first[1], failed


async def _scan(svc: JobService, job: Job, path: str, base: str) -> None:
    """The secret scan of one role's change (secretscan.py's script, in that role's checkout)."""
    from hangar import secretscan

    mode = svc.settings.secret_scan
    if mode == "off":
        return
    out = f"/tmp/hangar-secretscan-{job.id}-{abs(hash(path)) % 10**8}"
    res = await _sh(svc, job, secretscan.scan_script(path, base, out), path, timeout=300)
    if res.exit_code not in (0, 141):
        if mode == "block":
            raise secretscan.Blocked([], f"hangar couldn't read {path}'s change to scan it for secrets")
        return
    data = await svc.fleet.read_file(job.sandbox_id or "", out, limit=secretscan.MAX_SCAN_BYTES)
    await _sh(svc, job, f"rm -f {shlex.quote(out)}", path, timeout=30)
    if data is None:
        if mode == "block":
            raise secretscan.Blocked([], "the secret scan's output is missing")
        return
    hits = secretscan.scan_bundle(data[:secretscan.MAX_SCAN_BYTES].decode("utf-8", errors="replace"), svc.broker)
    if not hits:
        return
    svc.fleet.audit(f"job:{job.id}", "secret.found", job.id, hits=[h.line() for h in hits[:50]], mode=mode,
                    person=job.person, path=path)
    if mode == "block":
        raise secretscan.Blocked(hits)
    svc._event(job, f"possible secret in what is pushed from {path}: " + "; ".join(h.line() for h in hits[:5]))


async def deliver(svc: JobService, job: Job, draft: bool) -> None:
    """The parent's delivery: per changed role, commit what the agent left, scan, push with a grant pinned to that
    repo, ref and commit, and open (or update) the role's PR as a part job. Then cross-link the PRs. Raises jobs'
    _DeliveryError / _SecretFound like a single-repo delivery."""
    from hangar import delivery, scratch, secretscan, taint
    from hangar.jobs import _DeliveryError, _SecretFound

    store = of(svc)
    cs = store.of(job)
    if cs is None:
        raise _DeliveryError(f"change set {job.changeset} is gone")
    q = shlex.quote
    commits: list[str] = []
    files: list[str] = []
    pushed: list[str] = []
    stats: list[str] = []
    frozen = cs.state == "broken"  # v1: a broken set stops; its new work waits for a new job
    if frozen:
        svc._event(job, f"change set {cs.id} is broken ({cs.reason}): this turn's work is not delivered; submit a "
                        "new job for it")
    sb = svc.fleet.get(job.sandbox_id or "")
    push_mode = (sb.info.get("git") or {}).get("mode")
    for role, r in cs.roles.items():
        if r.agent == "write":
            continue
        path = checkout(svc.fleet, role)
        st = await _sh(svc, job, "git status --porcelain | head -n 5", path)
        if st.stdout.strip():
            svc._event(job, f"{role} is read-only for the agent: its changes in {path} are not delivered")
    for role in cs.land_order:
        r = cs.roles[role]
        path = checkout(svc.fleet, role)
        base = _base(job, cs, role)
        st = await _sh(svc, job, "git status --porcelain", path)
        changed = [ln for ln in st.stdout.splitlines() if ln.strip()]
        if changed and job.deliver != "none":
            if role == cs.primary:
                await scratch.commit_leftovers(svc, job, len(changed))  # minus agent scratch files, as for any job
            elif len(changed) <= 2000:
                res = await _sh(svc, job, "git add -A && (git diff --cached --quiet || git commit -q -m "
                                + q(f"hangar: uncommitted changes from {job.id}") + ")", path)
                if res.exit_code == 0:
                    svc._event(job, f"{role}: committed {len(changed)} uncommitted file(s) the agent left")
        if role == cs.primary and job.deliver != "none":
            await scratch.drop_committed(svc, job, base)
        log_ = await _sh(svc, job, f"git log --oneline --no-decorate -n 50 {q(base)}..HEAD", path)
        role_commits = [ln for ln in log_.stdout.splitlines() if ln.strip()]
        if not role_commits:
            continue
        head = (await _sh(svc, job, "git rev-parse HEAD", path)).stdout.strip()
        from hangar.traces import diff_files

        names = await _sh(svc, job, f"git diff --name-status --no-renames -z {q(base)}..HEAD | head -c 400000", path)
        role_files, deleted = diff_files(names.stdout)
        stat = await _sh(svc, job, f"git diff --stat {q(base)}..HEAD | tail -n 30", path)
        commits += [f"{role}: {c}" for c in role_commits]
        files += [f"{role}/{f}" for f in role_files]
        if stat.stdout.strip():
            stats.append(f"{role}:\n{stat.stdout.strip()}")
        if role == cs.primary:
            job.result.head_sha = head
        if job.deliver == "none" or frozen:
            continue
        part = svc._jobs.get(cs.parts[role]) if role in cs.parts else None
        if part is not None and part.landing.pr_state in ("merged",):
            svc._event(job, f"{role}'s PR #{part.result.pr_number} already merged: its new commits are not "
                            "delivered (submit a new job for them)")
            continue
        try:
            await _scan(svc, job, path, base)
        except secretscan.Blocked as e:
            raise _SecretFound(e) from None
        res = await delivery.push(svc.fleet, job.sandbox_id or "", r.branch, head, f"job:{job.id}", repo=r.repo,
                                  cwd=path)
        if res.exit_code != 0:
            raise _DeliveryError(f"push of {role} failed: {(res.stderr or res.stdout)[-500:]}")
        pushed.append(role)
        svc._event(job, f"pushed {role}: {r.branch} ({r.repo})")
        if part is None:
            part = _new_part(svc, job, cs, role)
        part.result.base_sha = base if re.fullmatch(r"[0-9a-f]{40}", base) else part.result.base_sha
        part.result.head_sha, part.result.commits, part.result.pushed = head, role_commits, True
        part.result.files, part.result.deleted = role_files, deleted
        part.result.diffstat = stat.stdout.strip() or None
        part.result.summary = job.result.summary
        part.taint = copy.deepcopy(job.taint)
        part.landing.files = role_files[:300]
        if part.landing.push_mode != "direct":
            part.landing.push_mode = push_mode or part.landing.push_mode
        if role == cs.primary:
            part.verify, part.result.verify_exit_code = job.verify, job.result.verify_exit_code
        else:
            p = svc.policies.get(r.repo)
            part.verify = (p.verify if p is not None else None) or None
            part.result.verify_exit_code = job.result.verify_exit_code if part.verify else None
        svc._save(part)
        if job.deliver in ("pr", "draft_pr"):
            tainted = taint.tainted(part)
            await svc._ensure_pr(part, draft=draft or job.deliver == "draft_pr" or tainted)
            await svc.landing.delivered(part)
            if part.result.pr_number:
                emit(svc, cs, "changeset.pr_opened", f"{role}:{part.result.pr_number}",
                     f"{role}: PR #{part.result.pr_number} opened ({_short_repo(part.repo or '')})", part)
    job.result.commits = commits
    job.result.files = files
    job.result.diffstat = "\n".join(stats)[-4000:] or None
    job.result.pushed = bool(pushed)
    store.save(cs)
    if cs.parts and cs.state == "working":
        set_state(svc, cs, "landing", f"{len(cs.parts)} PR(s) open: " + ", ".join(cs.parts))
    if pushed and job.deliver in ("pr", "draft_pr"):
        await link_prs(svc, cs)


def _new_part(svc: JobService, parent: Job, cs: ChangeSet, role: str) -> Job:
    r = cs.roles[role]
    part = Job(title=parent.title, instructions=f"Part {role} of change set {cs.id} (job {parent.id}).",
               source="changeset", source_ref=None, requester=parent.requester, owner=parent.owner, repo=r.repo,
               base_branch=r.base_branch, branch=r.branch, environment=parent.environment, harness=parent.harness,
               model=parent.model, deliver=parent.deliver, state=JobState.SUCCEEDED, person=parent.person,
               changeset=cs.id, parent=parent.id, role=role, ticket=parent.ticket, autofix="off",
               labels={LABEL: cs.id, ROLE_LABEL: role, **_reply_label(svc, parent)}, keep_minutes=0)
    cs.parts[role] = part.id
    svc._jobs[part.id] = part
    svc._event(part, f"part {role} of change set {cs.id}, delivered from job {parent.id}", JobState.SUCCEEDED)
    svc.fleet.audit(f"job:{parent.id}", "changeset.part", part.id, changeset=cs.id, role=role, repo=r.repo)
    return part


def _reply_label(svc: JobService, parent: Job) -> dict[str, str]:
    """A part (one repo's PR) answers in the parent's conversation: its relay.reply_to, so events out about the PR
    (pr.opened, pr.landed) reach the room that asked."""
    from hangar.ext.notify import REPLY_TO_LABEL

    r = reply_to(svc, parent)
    return {REPLY_TO_LABEL: r} if r else {}


def parts(svc: JobService, cs: ChangeSet) -> dict[str, Job]:
    out: dict[str, Job] = {}
    for role, jid in cs.parts.items():
        with contextlib.suppress(KeyError):
            out[role] = svc.get(jid)
    return out


def pr_lines(svc: JobService, job: Job) -> list[str]:
    """The sibling table a part's PR body carries."""
    cs = of(svc).of(job)
    if cs is None or not job.parent:
        return []
    ps = parts(svc, cs)
    order = [*cs.land_order, *([d.role] if (d := _deploy_spec(svc, cs)) else [])]
    lines = ["", f"**Change set `{cs.id}`** (profile `{cs.profile}`"
             + (f", ticket {cs.ticket}" if cs.ticket else "") + "): one PR per changed repo, landing in order "
             + " → ".join(f"**{r}**" if r == job.role else r for r in order) + ".", "",
             "| Role | Repo | PR | State |", "|---|---|---|---|"]
    for role in order:
        p = ps.get(role)
        repo = cs.roles[role].repo if role in cs.roles else "?"
        if p is None:
            state = "waiting for the others to merge" if role == getattr(_deploy_spec(svc, cs), "role", None) \
                else "no change"
            lines.append(f"| {role} | {_short_repo(repo)} | - | {state} |")
            continue
        pr = f"[#{p.result.pr_number}]({p.result.pr_url})" if p.result.pr_number else "-"
        me = " (this PR)" if p.id == job.id else ""
        lines.append(f"| {role}{me} | {_short_repo(repo)} | {pr} | {p.landing.pr_state or 'pushed'} |")
    return lines


def _short_repo(repo: str) -> str:
    from hangar.github import parse_repo

    return parse_repo(repo) or repo


def _deploy_spec(svc: JobService, cs: ChangeSet) -> DeploySpec | None:
    try:
        return load_profile(svc, cs).deploy
    except (ChangeSetError, ValidationError):
        return None


async def link_prs(svc: JobService, cs: ChangeSet) -> None:
    """Rewrite each part's PR body now that every sibling has a number (one edit per PR)."""
    from hangar import secretscan
    from hangar.github import parse_repo

    for p in parts(svc, cs).values():
        repo = parse_repo(p.repo)
        if not repo or not p.result.pr_number:
            continue
        gh = await svc.landing.client(repo, p)
        if gh is None:
            continue
        try:
            body = secretscan.clean(svc, p, svc._pr_body(p), "the PR body")
            await gh._req("PATCH", f"/repos/{repo}/pulls/{int(p.result.pr_number)}", json={"body": body})
        except Exception as e:  # noqa: BLE001 - GitHubError and the like: the links are a convenience
            svc._event(p, f"could not link the change set's PRs in #{p.result.pr_number}: {e}")
        finally:
            await gh.close()


# ====================================================================== landing (landing.py)
def gate(svc: JobService, job: Job) -> str | None:
    """Why a part's approved PR must not arm yet, or None: the set is broken, the agent can still push, or an
    earlier role in land_order hasn't merged."""
    cs = of(svc).of(job)
    if cs is None or not job.parent:
        return None
    if cs.state == "broken":
        return f"change set {cs.id} is broken ({cs.reason}): nothing more of it lands"
    with contextlib.suppress(KeyError):
        parent = svc.get(job.parent)
        if parent.id in svc._active or (parent.state not in _settled()):
            return f"an agent turn is queued or running on change set {cs.id}'s job {parent.id}"
    order = cs.land_order
    before = order[:order.index(job.role)] if job.role in order else order
    for role in before:
        p = parts(svc, cs).get(role)
        if p is not None and p.landing.pr_state != "merged":
            why = f"waiting for {role} #{p.result.pr_number or '?'} to merge first (change set {cs.id})"
            if job.landing.pr_state == "approved":
                emit(svc, cs, "changeset.waiting", f"{job.role}:{role}", f"{job.role} PR #{job.result.pr_number} "
                     f"is approved and {why}", job, waiting_for=role)
            return why
    return None


def _settled() -> set[JobState]:
    from hangar.models import JOB_TERMINAL

    return {*JOB_TERMINAL, JobState.NEEDS_INPUT}


def set_state(svc: JobService, cs: ChangeSet, state: str, why: str, event: str | None = None) -> None:
    if cs.state == state:
        return
    cs.state = state  # type: ignore[assignment]
    if state == "broken":
        cs.reason = why
    of(svc).save(cs)
    name = event or f"changeset.{state}"
    with contextlib.suppress(KeyError):
        parent = svc.get(cs.parent_job)
        svc._event(parent, f"{name}: {why}")
        svc._spawn(svc._callback(parent, name))
    svc.fleet.audit("hangar", name, cs.id, why=why[:500])
    emit(svc, cs, name, None, why)


# ---------------------------------------------------------------------- events out (hangar.event/v1)
EVENTS = ("changeset.created", "changeset.pr_opened", "changeset.waiting", "changeset.landed",
          "changeset.landing", "changeset.deploying", "changeset.deployed", "changeset.done", "changeset.broken")


def reply_to(svc: JobService, parent: Job) -> str | None:
    """Where the parent's conversation is (its relay.reply_to label, a turn's reply_to, its source_ref)."""
    from hangar import events_out

    return events_out._reply_to(parent)


def emit(svc: JobService, cs: ChangeSet, kind: str, key: str | None, summary: str, part: Job | None = None,
         **data: Any) -> None:
    """A change set's fact to every notifier (events_out), with a stable id (`<cs>:<kind>[:<key>]`), so a repeat of
    the same fact is the same event and is sent once. It carries the parent's person and reply_to, so the room
    that asked hears it."""
    from hangar import events_out
    from hangar.ext.notify import Event

    d = events_out.of(svc)
    if not d.notifiers:
        return
    try:
        parent = svc.get(cs.parent_job)
    except KeyError:
        return
    from hangar import inbox

    links = {"job": u} if (u := inbox.job_url(svc, parent.id)) else {}
    if part is not None and part.result.pr_url and part.result.pr_url.startswith("https://"):
        links["pr"] = part.result.pr_url
    body = {"changeset": cs.id, "profile": cs.profile, "state": cs.state, "ticket": cs.ticket,
            **({"role": part.role, "part_job": part.id, "pr_number": part.result.pr_number} if part else {}),
            **{k: v for k, v in data.items() if v is not None}}
    d.emit(Event(id=f"{cs.id}:{kind}" + (f":{key}" if key else ""), type=kind, at=now(), job=parent.id,
                 session=parent.session_id, person=parent.person, repo=part.repo if part else parent.repo,
                 reply_to=reply_to(svc, parent), links=links, summary=svc.broker.redact(summary)[:500],
                 data={k: v for k, v in body.items() if v is not None}))


def part_closed(svc: JobService, job: Job) -> None:
    """A part's PR merged or closed (landing's pull_request webhook)."""
    cs = of(svc).of(job)
    if cs is None or not job.parent:
        return
    if job.landing.pr_state != "merged":
        broken(svc, cs, f"{job.role} PR #{job.result.pr_number} was closed without merging")
        return
    if cs.deploy is not None and cs.deploy.job == job.id:
        cs.deploy.state = "merged"
        emit(svc, cs, "changeset.deployed", job.role, f"{job.role} PR #{job.result.pr_number} merged: the deploy "
             "is your CD's now", job, merge_sha=job.landing.merge_commit_sha)
        set_state(svc, cs, "done", f"deploy PR #{job.result.pr_number} merged")
        return
    emit(svc, cs, "changeset.landed", job.role, f"{job.role} PR #{job.result.pr_number} landed", job,
         merge_sha=job.landing.merge_commit_sha)
    if cs.state == "broken":
        return
    ps = parts(svc, cs)
    for p in ps.values():  # the next role may be approved and waiting for this one
        if p.id != job.id and p.landing.pr_state == "approved" and not p.landing.armed_sha:
            svc.landing._bg(p, svc.landing.evaluate(p))
    landed = [r for r in cs.land_order if r in ps]
    if all(ps[r].landing.pr_state == "merged" for r in landed):
        prof = _deploy_spec(svc, cs)
        if prof is None:
            set_state(svc, cs, "done", "every PR merged: " + ", ".join(landed))
        else:
            set_state(svc, cs, "deploying", "every PR merged (" + ", ".join(landed) + f"); the {prof.role} step "
                      "runs once it is ready")
            svc._spawn(step(svc, cs))


def broken(svc: JobService, cs: ChangeSet, why: str) -> None:
    if cs.state in ("broken", "done"):
        return
    set_state(svc, cs, "broken", why)
    for p in parts(svc, cs).values():
        if p.landing.armed_sha:
            svc.landing._bg(p, svc.landing._disarm_locked(p, f"change set {cs.id} is broken"))
        if p.landing.pr_state in ("open", "approved"):
            svc.landing._note(p, f"change set {cs.id} is broken ({why}): nothing more of it lands")


def all_ended(svc: JobService, job: Job) -> Job | None:
    """After a part's end: the parent job, when every part of the set has ended (its session can go now)."""
    cs = of(svc).of(job)
    if cs is None:
        return None
    ps = parts(svc, cs)
    if any(not p.landing.ended for r, p in ps.items() if r in cs.land_order):
        return None
    with contextlib.suppress(KeyError):
        return svc.get(cs.parent_job)
    return None


def parts_of(svc: JobService, parent: Job) -> list[Job]:
    cs = of(svc).of(parent)
    if cs is None or parent.parent or cs.parent_job != parent.id:
        return []
    return list(parts(svc, cs).values())


# ====================================================================== the deploy role
async def deploy_loop(svc: JobService) -> None:
    while True:
        try:
            await tick(svc)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - the next tick tries again
            log.exception("change-set tick failed")
        await asyncio.sleep(POLL_SECONDS)


async def tick(svc: JobService) -> None:
    """Every set still landing or deploying: a reverted part breaks it; a deploying one polls its step."""
    for cs in of(svc).list():
        if cs.state not in ("landing", "deploying"):
            continue
        for role, p in parts(svc, cs).items():
            if p.landing.reverted_at and cs.state != "done":
                broken(svc, cs, f"{role} PR #{p.result.pr_number} was reverted")
                break
        if cs.state == "deploying":
            await step(svc, cs)


_stepping: set[str] = set()


async def step(svc: JobService, cs: ChangeSet) -> None:
    """Poll the deploy step's `ready`; once ready, run `apply` and open the deploy PR. One at a time per set."""
    if cs.id in _stepping or cs.state != "deploying" or cs.deploy is None:
        return
    if cs.deploy.state not in ("waiting", "ready"):
        return
    _stepping.add(cs.id)
    try:
        await _step(svc, cs)
    finally:
        _stepping.discard(cs.id)


def _ctx(svc: JobService, cs: ChangeSet, prof: Profile, block: StepSpec) -> DeliverCtx:
    merged: dict[str, Merged] = {}
    for role, p in parts(svc, cs).items():
        if role in cs.land_order:
            merged[role] = Merged(repo=p.repo or "", pr=p.result.pr_number,
                                  merge_sha=p.landing.merge_commit_sha or p.result.head_sha)

    def note(text: str) -> None:
        if cs.deploy is not None and cs.deploy.note != text:
            cs.deploy.note = text
            with contextlib.suppress(KeyError):
                svc._event(svc.get(cs.parent_job), f"changeset.deploy: {text}")

    def audit(action: str, **detail: Any) -> None:
        svc.fleet.audit(f"changeset:{cs.id}", action, cs.id, **detail)

    d = prof.deploy
    return DeliverCtx(changeset=cs, role=d.role if d else "", merged=merged,
                      environment=d.environments[0] if d and d.environments else None,
                      spec=block.model_dump(), audit=audit, note=note)


async def _step(svc: JobService, cs: ChangeSet) -> None:
    prof = load_profile(svc, cs)
    d, st = prof.deploy, cs.deploy
    if d is None or st is None:
        return
    ready_block = d.ready or StepSpec(kind="command")
    ctx = _ctx(svc, cs, prof, ready_block)
    try:
        ready = await get_deliverer(ready_block.kind).ready(ctx)
    except DeliverError as e:
        return _failed(svc, cs, f"{ready_block.kind}: {e}")
    st.polled_at = now()
    if ready is None:
        ctx.note(f"waiting: {ready_block.kind} isn't ready yet")
        of(svc).save(cs)
        return
    st.state, st.ready = "applying", dict(ready.detail)
    of(svc).save(cs)
    ctx.note(f"{ready_block.kind} is ready" + (f": {ready.note}" if ready.note else "") + f"; running {d.apply.kind}")
    try:
        await _apply(svc, cs, prof, ready)
    except DeliverError as e:
        st.state = "waiting"
        return _failed(svc, cs, f"{d.apply.kind}: {e}")
    st.state, st.note = "opened", None
    of(svc).save(cs)


def _failed(svc: JobService, cs: ChangeSet, why: str) -> None:
    st = cs.deploy
    if st is None:
        return
    st.attempts += 1
    st.note = why[:1000]
    with contextlib.suppress(KeyError):
        svc._event(svc.get(cs.parent_job), f"changeset.deploy: attempt {st.attempts} failed: {why[:500]}")
    if st.attempts >= MAX_DEPLOY_ATTEMPTS:
        st.state = "failed"
        broken(svc, cs, f"the deploy step failed {st.attempts} times: {why[:300]}")
    of(svc).save(cs)


async def _apply(svc: JobService, cs: ChangeSet, prof: Profile, ready: Ready) -> None:
    """The deploy step: a fresh sandbox on the deploy repo, the apply deliverer, then hangar commits, pushes and opens
    the PR as a part job under that repo's policy. The sandbox goes at the end, whatever happened."""
    from hangar import delivery, secretscan

    d = prof.deploy
    assert d is not None and cs.deploy is not None
    role, r = d.role, cs.roles[d.role]
    parent = svc.get(cs.parent_job)
    part = svc._jobs.get(cs.parts[role]) if role in cs.parts else None
    if part is None:
        part = _new_part(svc, parent, cs, role)
        part.title = f"Deploy {cs.ticket or cs.id}: {parent.title}"[:200]  # the PR's title: the policy's template
        cs.deploy.job = part.id
        of(svc).save(cs)
    env = prof.environment or parent.environment
    actor = f"changeset:{cs.id}"
    try:
        sb = await svc.fleet.create(env, repo=r.repo, branch=r.base_branch, work_branch=r.branch,
                                    labels={"hangar.job": part.id, LABEL: cs.id, ROLE_LABEL: role},
                                    ttl_minutes=DEPLOY_TTL_MINUTES, actor=actor, person=parent.person,
                                    purpose="automated")
    except Exception as e:  # noqa: BLE001 - FleetError, a refused login, a full fleet: tried again next poll
        raise DeliverError(f"could not start the deploy step's sandbox: {e}") from None
    try:
        path = sb.info.get("repo_path") or checkout(svc.fleet, role)

        async def run(cmd: str, timeout: int = 600):
            return await svc.fleet.exec(sb.id, cmd, cwd=path, timeout=timeout, actor=actor)

        base = (await run("git rev-parse HEAD")).stdout.strip()
        ctx = _ctx(svc, cs, prof, d.apply)
        ctx.checkout, ctx.exec = path, run
        await get_deliverer(d.apply.kind).apply(ctx, ready)
        q = shlex.quote
        msg = part.title
        res = await run("git add -A && git diff --cached --quiet")
        if res.exit_code == 0:
            raise DeliverError("the step changed nothing in the deploy repo")
        res = await run("git -c user.email=hangar@localhost -c user.name=hangar commit -q -m " + q(msg)
                        + " -m " + q(f"Hangar-Changeset: {cs.id}"))
        if res.exit_code != 0:
            raise DeliverError(f"could not commit the step's change: {(res.stderr or res.stdout)[-300:]}")
        head = (await run("git rev-parse HEAD")).stdout.strip()
        if svc.settings.secret_scan != "off":
            out = f"/tmp/hangar-secretscan-{part.id}"
            scan = await run(secretscan.scan_script(path, base, out), timeout=300)
            data = await svc.fleet.read_file(sb.id, out, limit=secretscan.MAX_SCAN_BYTES)
            if scan.exit_code not in (0, 141) or data is None:
                raise DeliverError("couldn't scan the step's change for secrets")
            if hits := secretscan.scan_bundle(data.decode("utf-8", errors="replace"), svc.broker):
                raise DeliverError("possible secret in the step's change: " + "; ".join(h.line() for h in hits[:3]))
        push = await delivery.push(svc.fleet, sb.id, r.branch, head, actor, repo=r.repo, cwd=path)
        if push.exit_code != 0:
            raise DeliverError(f"push failed: {(push.stderr or push.stdout)[-400:]}")
        names = await run(f"git diff --name-only {q(base)}..HEAD | head -n 300")
        part.result.base_sha, part.result.head_sha, part.result.pushed = base, head, True
        part.result.commits = [f"{head[:7]} {msg}"]
        part.result.files = [x for x in names.stdout.splitlines() if x.strip()]
        part.result.summary = f"The {role} step of change set {cs.id} ({d.apply.kind}), after " + ", ".join(
            f"{k} #{m.pr}" for k, m in ctx.merged.items()) + " merged."
        part.landing.files = part.result.files[:300]
        part.landing.push_mode = (sb.info.get("git") or {}).get("mode") or part.landing.push_mode
        svc._save(part)
        svc._event(parent, f"changeset.deploy: pushed {r.branch} to {r.repo}")
        if parent.deliver in ("pr", "draft_pr"):
            await svc._ensure_pr(part, draft=parent.deliver == "draft_pr")
            if not part.result.pr_number:
                raise DeliverError("the deploy PR wasn't opened (see the part job's events)")
            emit(svc, cs, "changeset.pr_opened", f"{role}:{part.result.pr_number}",
                 f"{role}: deploy PR #{part.result.pr_number} opened ({_short_repo(r.repo)})", part)
            await svc.landing.delivered(part)
            await link_prs(svc, cs)
    finally:
        with contextlib.suppress(Exception):
            await svc.fleet.destroy(sb.id, actor=actor)


# ====================================================================== views and the API
def view(svc: JobService, cs: ChangeSet) -> dict[str, Any]:
    d = cs.model_dump(mode="json")
    d["parts"] = {role: {"job": p.id, "repo": p.repo, "branch": p.branch, "pr_number": p.result.pr_number,
                         "pr_url": p.result.pr_url, "pr_state": p.landing.pr_state, "note": p.landing.note}
                  for role, p in parts(svc, cs).items()}
    return d


def mount(app: Any, h: Any, admin: Any) -> None:
    """GET /api/changesets, GET /api/changesets/{id}; GET/PUT /api/changesets/profiles, GET/DELETE
    /api/changesets/profiles/{name} (admin tokens; mounted with the repo policies, repopolicy.mount)."""
    from fastapi import Body, HTTPException, Query

    svc: JobService = h.jobs

    @app.get("/api/changesets/profiles")
    async def cs_profiles(_: str = admin):
        return [{**p.model_dump(mode="json", by_alias=True), "digest": p.digest()} for p in profiles(svc).list()]

    @app.put("/api/changesets/profiles")
    async def cs_profile_put(body: dict[str, Any] = Body(...), who: str = admin):
        try:
            p = profiles(svc).put(body, who)
        except ChangeSetError as e:
            raise HTTPException(400, str(e)) from e
        return {**p.model_dump(mode="json", by_alias=True), "digest": p.digest()}

    @app.get("/api/changesets/profiles/{name}")
    async def cs_profile_get(name: str, _: str = admin):
        p = profiles(svc).get(name)
        if p is None:
            raise HTTPException(404, f"no change-set profile '{name}'")
        return {**p.model_dump(mode="json", by_alias=True), "digest": p.digest()}

    @app.delete("/api/changesets/profiles/{name}")
    async def cs_profile_delete(name: str, who: str = admin):
        if not profiles(svc).delete(name, who):
            raise HTTPException(404, f"no change-set profile '{name}'")
        return {"deleted": name}

    @app.get("/api/changesets")
    async def cs_list(state: str | None = Query(None), _: str = admin):
        return [view(svc, cs) for cs in of(svc).list(state)]

    @app.get("/api/changesets/{cs_id}")
    async def cs_get(cs_id: str, _: str = admin):
        cs = of(svc).get(cs_id)
        if cs is None:
            raise HTTPException(404, f"no change set '{cs_id}'")
        return view(svc, cs)
