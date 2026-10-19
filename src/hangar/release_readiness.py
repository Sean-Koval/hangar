"""Offline multi-repository release observations, never approval or deployment authority.

Inputs are untrusted observations. This module does no I/O and cannot verify their
provenance. Native live branch protection, SRE and production approval gates remain
authoritative. Provider adapters, persistence and execution are deliberately absent.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Identity = Annotated[str, Field(min_length=1, max_length=256, pattern=r"^\S(?:[^\r\n]*\S)?$")]
Revision = Annotated[str, Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")]
Kind = Literal["merge", "build", "approval", "database_scope", "migration", "rollout"]
StageId = Literal["code", "build", "infrastructure", "database", "activation", "production", "rollout"]
Status = Literal["missing", "unknown", "blocked", "satisfied_by_observation"]


class ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Requirement(ClosedModel):
    id: Identity
    kind: Kind
    repository: Annotated[str, Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")]
    revision: Revision
    bindings: dict[Identity, Identity] = Field(min_length=1, max_length=16)

    @model_validator(mode="after")
    def required_bindings(self) -> Requirement:
        required = {
            "merge": {"target", "pull_request", "reviewed_head"},
            "build": {"artifact", "build_id"},
            "approval": {"pull_request", "environment", "approver"},
            "database_scope": {"change", "decision", "reason", "actor"},
            "migration": {"change"},
            "rollout": {"artifact", "environment"},
        }[self.kind]
        if not required <= self.bindings.keys():
            raise ValueError(f"{self.kind} requires bindings: {sorted(required)}")
        artifact = self.bindings.get("artifact")
        if artifact is not None and not re.fullmatch(r"sha256:[0-9a-f]{64}", artifact):
            raise ValueError("artifact must be an immutable sha256 digest")
        if self.kind == "merge" and not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", self.bindings["reviewed_head"]):
            raise ValueError("reviewed_head must be an immutable commit SHA")
        if self.kind == "database_scope" and self.bindings["decision"] not in {"required", "not_required"}:
            raise ValueError("database scope decision must be required or not_required")
        return self


class Stage(ClosedModel):
    id: StageId
    depends_on: list[StageId] = Field(max_length=6)
    requirements: list[Requirement] = Field(min_length=1, max_length=16)


class ReleaseProfile(ClosedModel):
    version: Literal[1]
    release: Identity
    environment: Identity
    max_age_seconds: int = Field(gt=0, le=86400)
    stages: list[Stage] = Field(min_length=1, max_length=7)

    @field_validator("version", mode="before")
    @classmethod
    def integer_version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("version must be the integer 1")
        return value

    @model_validator(mode="after")
    def ordered_unique_stages(self) -> ReleaseProfile:
        seen: set[str] = set()
        requirements: set[str] = set()
        for stage in self.stages:
            if stage.id in seen or len(set(stage.depends_on)) != len(stage.depends_on):
                raise ValueError("duplicate stage or dependency")
            if not set(stage.depends_on) <= seen:
                raise ValueError("dependencies must name earlier stages (no cycles or unknown stages)")
            seen.add(stage.id)
            for requirement in stage.requirements:
                if requirement.id in requirements:
                    raise ValueError("requirement IDs must be unique across the profile")
                requirements.add(requirement.id)
        _validate_release_links(self.stages)
        for stage in self.stages:
            for requirement in stage.requirements:
                environment = requirement.bindings.get("environment")
                if environment is not None and environment != self.environment:
                    raise ValueError("observation requirement environment must match release target")
                if stage.id in {"activation", "production", "rollout"} and environment is None:
                    raise ValueError("activation, production and rollout must bind the release environment")
        return self


def _validate_release_links(stages: list[Stage]) -> None:
    """Bounded delivery semantics, not an executable policy language."""
    by_stage = {stage.id: {r.kind: r for r in stage.requirements} for stage in stages}
    required = {
        "code": {"merge"},
        "build": {"build"},
        "infrastructure": {"merge", "approval"},
        "database": {"database_scope"},
        "activation": {"merge"},
        "production": {"merge", "approval"},
        "rollout": {"rollout"},
    }
    for stage in stages:
        kinds = set(by_stage[stage.id])
        allowed = required[stage.id] | ({"migration"} if stage.id == "database" else set())
        if len(kinds) != len(stage.requirements) or not required[stage.id] <= kinds <= allowed:
            raise ValueError(f"invalid requirement kinds for {stage.id}")
        if "approval" in kinds:
            merge, approval = by_stage[stage.id]["merge"], by_stage[stage.id]["approval"]
            if (merge.repository, merge.bindings["reviewed_head"], merge.bindings["pull_request"]) != (
                approval.repository,
                approval.revision,
                approval.bindings["pull_request"],
            ):
                raise ValueError("approval must bind the exact merge repository, reviewed head and PR")

    def need(stage_id: str, dependency: str) -> None:
        stage = next(s for s in stages if s.id == stage_id)
        if dependency not in stage.depends_on:
            raise ValueError(f"{stage_id} must depend on {dependency}")

    if "build" in by_stage:
        need("build", "code")
        code, build = by_stage["code"]["merge"], by_stage["build"]["build"]
        if (code.repository, code.revision) != (build.repository, build.revision):
            raise ValueError("build must bind the exact merged code revision and repository")
    if "database" in by_stage:
        db = by_stage["database"]
        scope = db["database_scope"]
        migration = db.get("migration")
        if (scope.bindings["decision"] == "required") != (migration is not None):
            raise ValueError("required database change needs migration; not_required cannot have one")
        if migration and (scope.repository, scope.revision, scope.bindings["change"]) != (
            migration.repository,
            migration.revision,
            migration.bindings["change"],
        ):
            raise ValueError("migration must bind the scoped database change")
    if "activation" in by_stage:
        for dependency in ("build", "infrastructure", "database"):
            need("activation", dependency)
        activation = by_stage["activation"]["merge"]
        infra = by_stage["infrastructure"]["merge"]
        if (
            activation.repository != infra.repository
            or activation.bindings.get("infrastructure_revision") != infra.revision
        ):
            raise ValueError("activation must bind its infrastructure revision and repository")
    if "production" in by_stage:
        need("production", "activation")
        activation = by_stage["activation"]["merge"]
        merge, approval = by_stage["production"]["merge"], by_stage["production"]["approval"]
        if merge.bindings.get("activation_revision") != activation.revision:
            raise ValueError("production must bind the activation revision")
        if merge.bindings.get("environment") != approval.bindings["environment"]:
            raise ValueError("production approval environment mismatch")
    if "rollout" in by_stage:
        need("rollout", "production")
        rollout, merge = by_stage["rollout"]["rollout"], by_stage["production"]["merge"]
        if (rollout.repository, rollout.revision, rollout.bindings["environment"]) != (
            merge.repository,
            merge.revision,
            merge.bindings["environment"],
        ):
            raise ValueError("rollout must bind production repository, revision and environment")
    for stage_id in ("activation", "production", "rollout"):
        if stage_id in by_stage:
            artifact = by_stage["build"]["build"].bindings["artifact"]
            if any(r.bindings.get("artifact") != artifact for r in by_stage[stage_id].values()):
                raise ValueError("activation, production approval and rollout must bind the built artifact")


def _timestamp(value: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError("timestamps must be explicit timezone-aware strings")
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("invalid timestamp") from exc
    if stamp.utcoffset() != timedelta(0):
        raise ValueError("timestamps must use UTC")
    return stamp


class Observation(Requirement):
    release: Identity
    profile_digest: Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
    status: Literal["observed", "not_satisfied", "unknown"]
    observed_at: str
    source: Identity

    @field_validator("observed_at")
    @classmethod
    def valid_timestamp(cls, value: str) -> str:
        _timestamp(value)
        return value


def profile_digest(profile: dict) -> str:
    """Identity of the full validated profile, not authentication or evidence."""
    parsed = ReleaseProfile.model_validate(profile)
    canonical = json.dumps(parsed.model_dump(), sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()


def _requirement_status(
    requirement: Requirement, observations: list[Observation], profile: ReleaseProfile, digest: str, as_of: datetime
) -> tuple[Status, str]:
    matching = [item for item in observations if item.id == requirement.id]
    if not matching:
        return "missing", "no observation"
    expected = requirement.model_dump()
    for item in matching:
        identity = {key: getattr(item, key) for key in expected}
        if item.release != profile.release or item.profile_digest != digest or identity != expected:
            return "blocked", "observation identity does not match release and requirement"
    if len({item.status for item in matching}) > 1:
        return "blocked", "conflicting observations"
    if any(item.status == "not_satisfied" for item in matching):
        return "blocked", "negative observation"
    if any(
        not timedelta(0) <= as_of - _timestamp(item.observed_at) <= timedelta(seconds=profile.max_age_seconds)
        for item in matching
    ):
        return "unknown", "future or stale observation"
    if any(item.status == "unknown" for item in matching):
        return "unknown", "uncertain observation"
    return "satisfied_by_observation", "unverified caller observation only"


def _aggregate(statuses: list[Status]) -> Status:
    candidates: tuple[Status, ...] = ("blocked", "missing", "unknown")
    for candidate in candidates:
        if candidate in statuses:
            return candidate
    return "satisfied_by_observation"


def evaluate(profile: dict, observations: list[dict], *, as_of: str) -> dict:
    """Evaluate an explicit snapshot. Never collect evidence or grant authorization.

    Ordering requirements and dependencies is declarative; these are exact comparisons,
    not a rule language. An absent database-scope determination remains missing.
    """
    parsed = ReleaseProfile.model_validate(profile)
    digest = profile_digest(profile)
    if not isinstance(observations, list) or len(observations) > 256:
        raise ValueError("observations must be a list of at most 256 entries")
    observed = [Observation.model_validate(item) for item in observations]
    timestamp = _timestamp(as_of)
    known_ids = {r.id for stage in parsed.stages for r in stage.requirements}
    if any(item.id not in known_ids for item in observed):
        raise ValueError("observation names an unknown requirement")
    stages = []
    states: dict[str, Status] = {}
    for stage in parsed.stages:
        requirements = []
        for requirement in stage.requirements:
            status, reason = _requirement_status(requirement, observed, parsed, digest, timestamp)
            requirements.append({"id": requirement.id, "status": status, "reason": reason})
        blocked_by = [dep for dep in stage.depends_on if states[dep] != "satisfied_by_observation"]
        state = "blocked" if blocked_by else _aggregate([r["status"] for r in requirements])
        states[stage.id] = state
        stages.append({"id": stage.id, "status": state, "blocked_by": blocked_by, "requirements": requirements})
    omitted = sorted(
        {"code", "build", "infrastructure", "database", "activation", "production", "rollout"} - states.keys()
    )
    return {
        "version": 1,
        "release": parsed.release,
        "profile_digest": digest,
        "as_of": as_of,
        "status": _aggregate(list(states.values())),
        "stages": stages,
        "scope": "configured_requirements_only",
        "omitted_stages": omitted,
        "release_status": "unknown" if omitted else _aggregate(list(states.values())),
        "rollout_status": states.get("rollout", "unknown"),
        "external_verification": False,
        "authorization_granted": False,
        "notice": "Offline caller observations only; native live approval and deployment gates still apply.",
    }
