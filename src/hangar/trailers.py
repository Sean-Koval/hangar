"""Which harness wrote a PR (#137; docs/jobs.md#the-pull-request, docs/landing.md): git trailers at the end of a
delivered job's PR body and in the merge commit hangar lands, so a reviewer, a merged-PR metric or `git log` can join
a change to the harness and environment it was written with.

    Hangar-Job: job-1a2b3c4d
    Harness-Digest: sha256:...
    Environment-Digest: sha256:...
    Harness-Lock: team-default@r14          (only for a job thawed from a lock, #134)
    Hangar-Changeset: cs-1a2b3c4d           (only for a change set's PR; Hangar-Job names the agent's job)

Values come from the job's session turns' manifests (`SessionTurn.harness_digest`, `environment_digest`; #131): the
latest turn's. When the turns ran with different harness digests (a multi-turn job whose harness changed), there is
one `Harness-Digest:` line per distinct digest, in the order they first ran, each with the turns it covers
(`sha256:... (turns 1-2)`); the latest turn's is last. A turn recorded before digests existed adds nothing: a trailer
is never printed with an empty value.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from hangar.jobs import JobService
    from hangar.models import Job

JOB, HARNESS, ENVIRONMENT, LOCK = "Hangar-Job", "Harness-Digest", "Environment-Digest", "Harness-Lock"
CHANGESET = "Hangar-Changeset"  # --- change sets (changesets.py)


def _agent_job(jobs: JobService, job: Job) -> Job:
    """The job whose agent wrote the change: a change set's part (one repo's PR) was written by its parent."""
    if getattr(job, "parent", None):
        try:
            return jobs.get(job.parent)
        except KeyError:
            return job
    return job


def ranges(turns: list[int]) -> str:
    """[1, 2, 3, 5] -> "turns 1-3, 5"."""
    parts: list[str] = []
    start = prev = turns[0]
    for n in [*turns[1:], None]:
        if n is not None and n == prev + 1:
            prev = n
            continue
        parts.append(f"{start}" if start == prev else f"{start}-{prev}")
        if n is not None:
            start = prev = n
    return ("turn " if len(turns) == 1 else "turns ") + ", ".join(parts)


def turns(jobs: JobService, job: Job) -> list[Any]:
    """The job's session turns, in order (their numbers are the session's, as its manifests are)."""
    return list(getattr(jobs._session(_agent_job(jobs, job)), "turns", None) or [])


def values(jobs: JobService, job: Job) -> dict[str, Any]:
    """{harness: [(digest, [turn numbers])] in first-run order with the latest turn's last, environment: digest of
    the latest turn that has one, lock: "name@rN" or None}."""
    seen: dict[str, list[int]] = {}
    latest_h = latest_e = None
    for n, t in enumerate(turns(jobs, job), 1):
        if t.harness_digest:
            seen.setdefault(t.harness_digest, []).append(n)
            latest_h = t.harness_digest
        if t.environment_digest:
            latest_e = t.environment_digest
    harness = [(d, ns) for d, ns in seen.items() if d != latest_h]
    if latest_h:
        harness.append((latest_h, seen[latest_h]))
    p = getattr(_agent_job(jobs, job), "harness_lock", None) or {}
    lock = f"{p['name']}@r{p['revision']}" if p.get("name") and p.get("revision") else None
    return {"harness": harness, "environment": latest_e, "lock": lock}


def lines(jobs: JobService, job: Job) -> list[str]:
    """The trailer lines, in order; `Hangar-Job` always, the rest only when known."""
    v = values(jobs, job)
    out = [f"{JOB}: {_agent_job(jobs, job).id}"]
    many = len(v["harness"]) > 1
    out += [f"{HARNESS}: {d}" + (f" ({ranges(ns)})" if many else "") for d, ns in v["harness"]]
    if v["environment"]:
        out.append(f"{ENVIRONMENT}: {v['environment']}")
    if v["lock"]:
        out.append(f"{LOCK}: {v['lock']}")
    if cs := getattr(job, "changeset", None):
        out.append(f"{CHANGESET}: {cs}")
    return out


def block(jobs: JobService, job: Job) -> str:
    """The trailers as a final paragraph (a blank line, then the lines), to end a PR body or commit message with."""
    return "\n\n" + "\n".join(lines(jobs, job))


def short(digest: str | None) -> str | None:
    """`sha256:abcd1234…`, as the review packet shows a digest."""
    return f"{digest[:15]}…" if digest else None
