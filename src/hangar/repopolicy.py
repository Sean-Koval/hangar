"""Per-repo policy (--- frontdoor): the defaults every front door applies to a repo's jobs.

Work reaches hangar through many doors (MCP, REST, the CLI, GitHub issues, Slack, A2A, inbound webhooks, schedules,
and tracker intake next). Each used to carry its own idea of a job's defaults, and most carried none: a job from a
GitHub issue or Slack never had a `verify`. A `RepoPolicy` is one record per repo that `JobService.submit` applies
to every submission, whichever door it came through, so a new intake only has to name the repo.

Precedence, per field: what the job asked for, then the repo's policy, then the environment (its `verify:` and
`model:`), then hangar's settings (HANGAR_BUDGET_*, HANGAR_JOB_BUDGET_*). An explicit empty `verify` ("") at any
level means "no check" and stops the fallback.

Policies live in the store's kv table (`repo-policy:<repo>`), written durably like stored settings, because they
decide what runs. `HANGAR_GITHUB_REPOS` stays the allowlist of repos (and their environments) that untrusted and
service callers may use; a policy never widens it.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any, Literal, NamedTuple
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, ValidationError, field_validator

from hangar import modelpolicy as modelpolicy  # --- canary
from hangar.egress import EgressSpec  # --- egress
from hangar.github import parse_repo
from hangar.harnesses import get_harness
from hangar.models import Budget, JobBudget, now

if TYPE_CHECKING:
    from hangar.envspec import EnvSpec
    from hangar.jobs import JobService, SubmitJob
    from hangar.store import Store

log = logging.getLogger("hangar.repopolicy")

KV_PREFIX = "repo-policy:"
_GITHUB_SHORT = re.compile(r"^[\w.-]+/[\w.-]+$")
FIELDS = ("verify", "environment", "harness", "model", "budget", "job_budget", "max_usd_per_day",
          "credentials",  # --- teamcreds
          "egress", "build_secrets",  # --- egress
          "land", "approvers", "merge_method", "risk_paths",  # --- landing: the last four (landing.py)
          "require_stale_dismissal",  # --- trust
          "verify_reports", "rerun_flaky",  # --- testresults
          "heavy", "profile", "max_profile",  # --- heavy
          "allow_heavy_for_intake",  # --- #124
          "verify_allowed")  # --- #156
_LOGIN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")  # a GitHub login


HeavyMode = Literal["on", "allow", "deny"]  # --- heavy: a repo policy's `heavy`


def _heavy_mode(v: Any) -> Any:
    """`heavy: true`/`false` (how #117 first stored it) mean `on`/`deny`."""
    return {True: "on", False: "deny"}.get(v, v) if isinstance(v, bool) else v


class PolicyError(ValueError):
    pass


class RepoPolicy(BaseModel):
    """A repo's defaults. The budgets are explained in jobbudget.py."""

    repo: str  # the key: "owner/repo" (lower-case) for GitHub, else the git URL without credentials or ".git"
    verify: str | None = Field(None, max_length=2000)  # "" = no check, even if the environment has one
    environment: str | None = None
    harness: str | None = None
    # A model for every harness, or one per harness ({claude-code: opus, codex: gpt-5.5}), as in environments. A
    # plain string applies only to the policy's own harness (or, without one, when the job didn't pick a harness):
    # a model id means nothing to another harness. --- canary: a policy (`frontier`, `balanced`) applies to every one.
    model: str | dict[str, str] | None = None
    budget: Budget | None = None  # per agent turn, under the job's own (jobbudget.py)
    job_budget: JobBudget | None = None  # all of a job's turns together
    max_usd_per_day: float | None = Field(None, gt=0)  # the repo's agent turns, per UTC day
    # --- teamcreds: whose login its jobs' agents run with: the person the job runs for (`person`; a job for no one
    # fails), the team's (`team`), or the person's when they have one, else the team's (unset: person_then_team)
    credentials: Literal["person", "team", "person_then_team"] | None = None
    # --- egress: tightens the environment's egress mode and adds hosts for this repo's sandboxes (egress.py)
    egress: EgressSpec | None = None
    # --- egress: build secrets for this repo's environment builds (buildsecrets.py): {SECRET_ID: credential}
    build_secrets: dict[str, str] | None = None
    # --- landing (landing.py): `auto_after_approval` arms auto-merge once someone in `approvers` (GitHub logins) or
    # a code owner approves the PR's head and the required checks pass; None is manual. `risk_paths` are globs that
    # make a PR high risk in its review packet.
    land: Literal["manual", "auto_after_approval"] | None = None
    approvers: list[str] | None = Field(None, max_length=100)
    merge_method: Literal["squash", "merge", "rebase"] | None = None
    risk_paths: list[str] | None = Field(None, max_length=100)
    # --- trust: auto-merge is armed only on a base branch that dismisses stale approvals on a push (landing.py);
    # false opts the repo out of that check. None: required.
    require_stale_dismissal: bool | None = None
    # --- testresults: the reports `verify` writes (a repo's .hangar/environment.yaml `verify_reports` comes first),
    # and whether a check that failed only on tests on the repo's flaky list runs once more (testresults.py)
    verify_reports: list[str] | None = Field(None, max_length=16)
    rerun_flaky: bool | None = None
    # --- heavy (heavy.py): Docker on the heavy tier is deny by default (the owner's decision). `on`: the repo's
    # sandboxes run on it; `allow`: its .hangar/environment.yaml may ask for it; `deny` or unset: a request is not
    # honored. Then the profile its sandboxes get, and the largest one a job (label hangar.profile) or the repo's file
    # may ask for
    heavy: HeavyMode | None = None
    profile: Literal["standard", "large", "xlarge"] | None = None
    max_profile: Literal["standard", "large", "xlarge"] | None = None
    # --- #124: intake-triggered jobs (posture.py) may run Docker on the heavy tier for this repo (privileged in the
    # sandbox's VM); unset: they are refused under HANGAR_INTAKE_POSTURE=enforce
    allow_heavy_for_intake: bool | None = None
    # --- #156: globs a ticket's own verify command must match to become its job's `verify` (`make test*`,
    # `pytest tests/*`); otherwise the agent gets it only as a suggestion (intake.apply_ticket)
    verify_allowed: list[str] | None = Field(None, max_length=50)
    updated_at: float = Field(default_factory=now)
    updated_by: str | None = None

    @field_validator("heavy", mode="before")
    @classmethod
    def _heavy(cls, v: Any) -> Any:  # --- integration(wave 4): the booleans #117 stored read as on and deny
        return _heavy_mode(v)

    @field_validator("approvers")
    @classmethod
    def _logins(cls, v: list[str] | None) -> list[str] | None:  # --- landing
        if v is None:
            return None
        out = [x.strip().removeprefix("@") for x in v if x.strip()]
        if bad := [x for x in out if not _LOGIN.match(x)]:
            raise ValueError(f"not GitHub logins: {', '.join(bad[:5])}")
        return out

    @field_validator("verify_allowed")
    @classmethod
    def _allowed(cls, v: list[str] | None) -> list[str] | None:  # --- #156
        if v is None:
            return None
        out = [x.strip() for x in v if x.strip()]
        if bad := [x for x in out if "\n" in x or len(x) > 200 or x in ("*", "**")]:
            raise ValueError(f"verify_allowed globs must be one line, at most 200 characters, and narrower than "
                             f"'*': {', '.join(bad[:3])}")
        return out

    @field_validator("verify_reports")
    @classmethod
    def _reports(cls, v: list[str] | None) -> list[str] | None:  # --- testresults
        from hangar.testresults import check_globs

        return check_globs(v)

    @field_validator("model")
    @classmethod
    def _model_keys(cls, v: str | dict[str, str] | None) -> str | dict[str, str] | None:
        if isinstance(v, dict):
            for h in v:
                get_harness(h)
        return v

    @field_validator("build_secrets")  # --- egress
    @classmethod
    def _build_secret_ids(cls, v: dict[str, str] | None) -> dict[str, str] | None:
        from hangar.buildsecrets import check_ids

        return check_ids(v) if v else v

    @field_validator("harness")
    @classmethod
    def _harness(cls, v: str | None) -> str | None:
        if v and not get_harness(v).agent:
            raise ValueError(f"'{v}' is not an agent harness")
        return v

    def model_for(self, harness: str | None, job_harness: str | None) -> str | None:
        if isinstance(self.model, dict):
            return self.model.get(harness or "")
        if modelpolicy.is_policy(self.model):  # --- canary: `frontier` means something to every harness
            return self.model
        if self.harness:
            return self.model if harness == self.harness else None
        return None if job_harness else self.model


def repo_key(repo: str | None) -> str | None:
    """One name per repo, whatever URL a front door used: `owner/repo` for GitHub (case-insensitive there), else
    the URL without userinfo (never a credential) and without `.git`."""
    if not repo or not repo.strip():
        return None
    r = repo.strip()
    if _GITHUB_SHORT.match(r):
        return r.removesuffix(".git").lower()
    if gh := parse_repo(r):
        return gh.lower()
    u = urlsplit(r)
    if u.scheme and u.netloc:
        r = u._replace(netloc=u.netloc.rpartition("@")[2]).geturl()
    return r.rstrip("/").removesuffix(".git")


def allowed_envs(svc: JobService, key: str) -> list[str]:
    """The environments HANGAR_GITHUB_REPOS allows for a policy's repo (its entry may carry `.git` or not)."""
    if _GITHUB_SHORT.match(key):
        return svc.repo_envs(f"https://github.com/{key}.git")
    return svc.repo_envs(key) or svc.repo_envs(f"{key}.git")


class RepoPolicies:
    """The policies, in the store. Reads go to SQLite (one indexed row per submission); nothing is cached, so a
    change applies to the next job without a restart."""

    def __init__(self, store: Store):
        self.store = store

    def get(self, repo: str | None) -> RepoPolicy | None:
        key = repo_key(repo)
        if not key:
            return None
        raw = self.store.kv_get(KV_PREFIX + key)
        if raw is None:
            return None
        try:
            return RepoPolicy.model_validate_json(raw)
        except ValidationError as e:  # a harness renamed since: say so, and run the job on the other defaults
            log.warning("ignoring the unreadable policy for %s: %s", key, e)
            return None

    def list(self) -> list[RepoPolicy]:
        out = []
        for k in sorted(self.store.kv_keys(KV_PREFIX)):
            if (p := self.get(k[len(KV_PREFIX):])) is not None:
                out.append(p)
        return out

    def put(self, policy: RepoPolicy) -> RepoPolicy:
        self.store.kv_set(KV_PREFIX + policy.repo, policy.model_dump_json(), durable=True)
        return policy

    def delete(self, repo: str) -> bool:
        key = repo_key(repo)
        if not key or self.store.kv_get(KV_PREFIX + key) is None:
            return False
        self.store.kv_delete(KV_PREFIX + key, durable=True)
        return True


# ====================================================================== applying a policy to a submission
class Defaults(NamedTuple):
    environment: str | None
    harness: str | None
    model: str | None
    verify: str | None
    verify_source: str | None  # job | repo | environment
    notes: list[str]  # what the policy set, or why it couldn't (job events)


def environment(policy: RepoPolicy | None, allowed: list[str]) -> str | None:
    """The policy's environment, if the repo's allowlist (HANGAR_GITHUB_REPOS) permits it."""
    if policy is None or not policy.environment:
        return None
    if allowed and policy.environment not in allowed:
        log.warning("repo policy for %s names environment %s, which HANGAR_GITHUB_REPOS doesn't allow for it",
                    policy.repo, policy.environment)
        return None
    return policy.environment


def resolve(req: SubmitJob, policy: RepoPolicy | None, spec: EnvSpec) -> Defaults:
    """Fill what the job left unset from the repo's policy, then the environment."""
    notes: list[str] = []
    harness = req.harness
    if not harness and policy and policy.harness:
        if policy.harness in spec.agent_harnesses:
            harness = policy.harness
            notes.append(f"harness {harness}")
        else:
            notes.append(f"its harness {policy.harness} isn't enabled in environment {spec.name}; using the "
                         "environment's default")
    model = req.model
    if not model and policy and policy.model:
        model = policy.model_for(harness or spec.pick_harness(None), req.harness)
        if model:
            notes.append(f"model {model}")
    if req.verify is not None:
        verify, source = req.verify, "job"
    elif policy is not None and policy.verify is not None:
        verify, source = policy.verify, "repo"
        notes.append(f"verify `{verify}`" if verify else "no verify")
    elif spec.verify is not None:
        verify, source = spec.verify, "environment"
    else:
        verify, source = None, None
    if not (verify or "").strip():
        verify, source = None, None
    return Defaults(spec.name, harness, model, verify, source, notes)


# ====================================================================== changing a policy (CLI and REST)
class PolicyUpdate(BaseModel):
    """PUT /api/repos/policy: the fields to set. A field sent as null is cleared; one left out is kept."""

    repo: str = Field(min_length=1, max_length=500)
    verify: str | None = Field(None, max_length=2000)
    environment: str | None = None
    harness: str | None = None
    model: str | dict[str, str] | None = None
    budget: Budget | None = None
    job_budget: JobBudget | None = None
    max_usd_per_day: float | None = Field(None, gt=0)
    credentials: Literal["person", "team", "person_then_team"] | None = None  # --- teamcreds
    egress: EgressSpec | None = None  # --- egress
    build_secrets: dict[str, str] | None = None  # --- egress
    land: Literal["manual", "auto_after_approval"] | None = None  # --- landing
    approvers: list[str] | None = None
    merge_method: Literal["squash", "merge", "rebase"] | None = None
    risk_paths: list[str] | None = None
    require_stale_dismissal: bool | None = None  # --- trust
    verify_reports: list[str] | None = None  # --- testresults
    rerun_flaky: bool | None = None
    heavy: HeavyMode | None = None  # --- heavy
    profile: Literal["standard", "large", "xlarge"] | None = None
    max_profile: Literal["standard", "large", "xlarge"] | None = None
    allow_heavy_for_intake: bool | None = None  # --- #124
    verify_allowed: list[str] | None = None  # --- #156

    @field_validator("heavy", mode="before")
    @classmethod
    def _heavy(cls, v: Any) -> Any:
        return _heavy_mode(v)


def update(svc: JobService, body: PolicyUpdate, actor: str) -> RepoPolicy:
    """Merge the fields sent into the repo's policy, checked against the catalog and the allowlist."""
    key = repo_key(body.repo)
    if not key:
        raise PolicyError("repo is required")
    if "://" not in key and not _GITHUB_SHORT.match(key) and not key.startswith("git@"):
        raise PolicyError(f"'{body.repo}' is not a repo: use owner/repo for GitHub, or a git URL")
    sent = body.model_fields_set - {"repo"}
    if not sent:
        raise PolicyError("nothing to set: pass at least one of " + ", ".join(FIELDS))
    current = svc.policies.get(key)
    data: dict[str, Any] = current.model_dump() if current else {"repo": key}
    for f in sent:
        data[f] = getattr(body, f)
    data.update(repo=key, updated_at=now(), updated_by=actor)
    try:
        policy = RepoPolicy.model_validate(data)
    except ValueError as e:  # pydantic's ValidationError included
        raise PolicyError(_why(e)) from e
    _check(svc, policy)
    svc.policies.put(policy)
    svc.fleet.audit(actor, "repo.policy.set", key, fields=sorted(sent))
    return policy


def unset(svc: JobService, repo: str, fields: list[str], actor: str) -> RepoPolicy | None:
    """Clear some fields of a repo's policy, or (no fields) remove it. Returns what is left."""
    key = repo_key(repo)
    current = svc.policies.get(key) if key else None
    if current is None:
        raise PolicyError(f"no policy for '{repo}'")
    bad = [f for f in fields if f not in RepoPolicy.model_fields or f in ("repo", "updated_at", "updated_by")]
    if bad:
        raise PolicyError(f"unknown field(s): {', '.join(bad)} (fields: {', '.join(FIELDS)})")
    if not fields:
        svc.policies.delete(current.repo)
        svc.fleet.audit(actor, "repo.policy.delete", current.repo)
        return None
    policy = current.model_copy(update={**{f: None for f in fields}, "updated_at": now(), "updated_by": actor})
    svc.policies.put(policy)
    svc.fleet.audit(actor, "repo.policy.unset", current.repo, fields=sorted(fields))
    return policy


def _why(e: Exception) -> str:
    if isinstance(e, ValidationError):
        err = e.errors()[0]
        return f"{'.'.join(map(str, err['loc']))}: {err['msg']}"
    return str(e).strip("'\"")


def _check(svc: JobService, p: RepoPolicy) -> None:
    """Refuse a policy no job could run with, rather than failing every job that inherits it."""
    allowed = allowed_envs(svc, p.repo)
    if p.environment:
        try:
            spec = svc.fleet.catalog.get(p.environment)
        except KeyError as e:
            raise PolicyError(f"no environment '{p.environment}'") from e
        if allowed and p.environment not in allowed:
            raise PolicyError(f"environment '{p.environment}' isn't allowed for {p.repo} in HANGAR_GITHUB_REPOS "
                              f"(allowed: {', '.join(allowed)}); add it there first")
    else:
        spec = None
        name = allowed[0] if allowed else None
        if name:
            try:
                spec = svc.fleet.catalog.get(name)
            except KeyError:
                spec = None
    if p.harness and spec is not None and p.harness not in spec.agent_harnesses:
        raise PolicyError(f"harness '{p.harness}' isn't enabled in environment '{spec.name}' "
                          f"(enabled: {', '.join(spec.agent_harnesses) or 'none'})")


def view(svc: JobService, p: RepoPolicy) -> dict[str, Any]:
    """A policy and what it resolves against: the allowlisted environments and the environment's own verify."""
    d = p.model_dump(mode="json")
    allowed = allowed_envs(svc, p.repo)
    d["allowed_environments"] = allowed
    env = p.environment or (allowed[0] if allowed else None)
    try:
        spec = svc.fleet.catalog.get(env) if env else None
    except KeyError:
        spec = None
    d["environment_verify"] = spec.verify if spec else None
    d["effective_verify"] = (p.verify if p.verify is not None else d["environment_verify"]) or None
    if p.max_usd_per_day:
        from hangar.jobbudget import repo_spent_today

        d["spent_today_usd"] = round(repo_spent_today(svc, p.repo), 4)
    return d


def mount(app: Any, h: Any, admin: Any) -> None:
    """GET /api/repos, GET/DELETE /api/repos/policy?repo=…, PUT /api/repos/policy (admin tokens only). The repo is
    a query or body field, not a path segment: git URLs carry slashes and colons."""
    from fastapi import Body, HTTPException, Query

    svc: JobService = h.jobs

    @app.get("/api/repos")
    async def repos_list(_: str = admin):
        return [view(svc, p) for p in svc.policies.list()]

    @app.get("/api/repos/policy")
    async def repo_get(repo: str = Query(...), _: str = admin):
        p = svc.policies.get(repo)
        if p is None:
            raise HTTPException(404, f"no policy for '{repo}'")
        return view(svc, p)

    @app.put("/api/repos/policy")
    async def repo_put(body: PolicyUpdate = Body(...), who: str = admin):
        try:
            return view(svc, update(svc, body, who))
        except PolicyError as e:
            raise HTTPException(400, str(e)) from e

    @app.delete("/api/repos/policy")
    async def repo_delete(repo: str = Query(...), field: list[str] = Query(default_factory=list),
                          who: str = admin):
        try:
            p = unset(svc, repo, field, who)
        except PolicyError as e:
            raise HTTPException(404 if "no policy" in str(e) else 400, str(e)) from e
        return view(svc, p) if p else {"deleted": repo_key(repo)}
