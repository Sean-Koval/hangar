"""Keep agent scratch files out of delivered PRs (--- frontdoor).

Delivery commits whatever the agent left (`git add -A`), and agents leave notes: hangar's own `repo-conventions`
skill once told them to write `HANDOFF.md`, and a `RESULT.md` meant for the job directory sometimes lands in the
repo. Hangar already keeps the agent's summary, so these files only add noise to the PR.

The rule is narrow on purpose, so it never drops work someone asked for. A file is scratch when all of these hold:

* it is new on the job's branch: the base commit doesn't have it (a file the repo tracks is never touched);
* it is `HANDOFF.md`, `RESULT.md` or `NOTES.md` at the repo root (any case), or a `*.orig` / `*.rej` patch
  leftover anywhere: one next to the file it was made from (`a.py.orig` beside `a.py`), so a golden file that
  only shares the suffix is work;
* the task doesn't name it: neither the instructions, any follow-up, nor the context people attached (an issue,
  a ticket or a thread is where intake puts the request) mention the file name.

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


def is_scratch(path: str, task_text: str, leftovers: set[str] | frozenset[str] = frozenset()) -> bool:
    """`leftovers`: the `*.orig` / `*.rej` paths that sit next to the file they were made from."""
    name = path.rsplit("/", 1)[-1]
    hit = ("/" not in path and name.lower() in ROOT_NAMES) or path in leftovers
    return hit and name.lower() not in task_text


def task_text(job: Job) -> str:
    """Everything people asked for, lower-cased: a file it names is wanted, not scratch. Context counts: a GitHub
    issue's (or a ticket's) text reaches the agent as a context file, not in the instructions."""
    items = [*job.context, *(c for t in job.turns for c in t.context)]
    ctx = [f"{c.name}\n{c.content if c.encoding == 'utf-8' and c.content else ''}" for c in items
           if c.kind not in ("patch", "bundle")]
    return "\n".join([job.title or "", job.instructions, *(t.message or "" for t in job.turns), *ctx]).lower()


async def _leftovers(svc: JobService, job: Job, paths: list[str]) -> set[str]:
    """The `*.orig` / `*.rej` files among `paths` whose original (the path without the suffix) exists."""
    cands = [p for p in paths if any(fnmatch.fnmatch(p.rsplit("/", 1)[-1], g) for g in ANYWHERE)]
    if not cands:
        return set()
    res = await svc._sh(job, "for p in " + " ".join(shlex.quote(p) for p in cands)
                             + "; do [ -e \"${p%.*}\" ] && printf '%s\\0' \"$p\"; done; true")
    return set(_paths(res.stdout)) & set(cands)


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
    text, added = task_text(job), _paths(new.stdout)
    leftovers = await _leftovers(svc, job, added)
    held = [p for p in added if is_scratch(p, text, leftovers)]
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
    text, added = task_text(job), _paths(res.stdout)
    leftovers = await _leftovers(svc, job, added)
    drop = [p for p in added if is_scratch(p, text, leftovers)]
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
