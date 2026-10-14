"""Keep agent scratch files out of delivered PRs (--- frontdoor).

Delivery commits whatever the agent left (`git add -A`), and agents leave notes: hangar's own `repo-conventions`
skill once told them to write `HANDOFF.md`, and a `RESULT.md` meant for the job directory sometimes lands in the
repo. Hangar already keeps the agent's summary, so these files only add noise to the PR.

The rule is narrow on purpose, so it never drops work someone asked for. A file is scratch when all of these hold:

* it is new on the job's branch: the base commit doesn't have it (a file the repo tracks is never touched);
* it is `HANDOFF.md`, `RESULT.md` or `NOTES.md` at the repo root (any case), or a `*.orig` / `*.rej` patch
  leftover anywhere;
* the task doesn't name it: neither the instructions nor any follow-up mention the file name.

Scratch the agent left uncommitted is not staged. Scratch the agent committed itself is removed from the branch in
a separate commit (`git rm --cached`), so the change is visible and revertible, and the file stays in the sandbox.
"""

from __future__ import annotations

import fnmatch
import shlex
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from hangar.jobs import JobService
    from hangar.models import Job

ROOT_NAMES = {"handoff.md", "result.md", "notes.md"}
ANYWHERE = ("*.orig", "*.rej")
COMMIT_MESSAGE = "hangar: drop agent scratch files"


def is_scratch(path: str, task_text: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    hit = ("/" not in path and name.lower() in ROOT_NAMES) or any(fnmatch.fnmatch(name, p) for p in ANYWHERE)
    return hit and name.lower() not in task_text


def task_text(job: Job) -> str:
    """Everything people asked for, lower-cased: a file it names is wanted, not scratch."""
    return "\n".join([job.title or "", job.instructions, *(t.message or "" for t in job.turns)]).lower()


def _paths(out: str) -> list[str]:
    return [p for p in out.split("\0") if p]


async def commit_leftovers(svc: JobService, job: Job, count: int) -> None:
    """`git add -A && git commit`, minus new scratch files (they stay in the sandbox, unstaged)."""
    q = shlex.quote
    add = await svc._sh(job, "git add -A")
    if add.exit_code != 0:
        svc._event(job, f"could not stage the {count} file(s) the agent left: {(add.stderr or add.stdout)[-300:]}")
        return
    new = await svc._sh(job, "git diff --cached --name-only --diff-filter=A -z")
    text = task_text(job)
    held = [p for p in _paths(new.stdout) if is_scratch(p, text)]
    if held:
        await svc._sh(job, "git reset -q -- " + " ".join(q(p) for p in held))
        _note(svc, job, held, "left out of the commit")
    res = await svc._sh(job, "git diff --cached --quiet || git commit -q -m "
                             + q(f"hangar: uncommitted changes from {job.id}"))
    if res.exit_code == 0 and count > len(held):
        svc._event(job, f"committed {count - len(held)} uncommitted file(s) the agent left")


async def drop_committed(svc: JobService, job: Job, base: str) -> None:
    """Take scratch files the agent committed on the branch back out, in a commit of their own."""
    q = shlex.quote
    res = await svc._sh(job, f"git diff --name-only --diff-filter=A -z {q(base)} HEAD --")
    if res.exit_code != 0:
        return
    text = task_text(job)
    drop = [p for p in _paths(res.stdout) if is_scratch(p, text)]
    if not drop:
        return
    rm = await svc._sh(job, "git rm -q --cached -- " + " ".join(q(p) for p in drop)
                            + " && git commit -q -m " + q(COMMIT_MESSAGE))
    if rm.exit_code == 0:
        _note(svc, job, drop, "removed from the branch")
    else:
        svc._event(job, f"could not remove agent scratch files {', '.join(drop[:5])}: "
                        f"{(rm.stderr or rm.stdout)[-300:]}")


def _note(svc: JobService, job: Job, paths: list[str], what: str) -> None:
    job.result.dropped_files = sorted({*job.result.dropped_files, *paths})[:50]
    svc._event(job, f"agent scratch file(s) {what}: {', '.join(paths[:10])} (they stay in the sandbox; name a file "
                    "in the task to deliver it)")
