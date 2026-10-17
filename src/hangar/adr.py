"""`@hangar adr` (#170; docs/slack.md#hangar-adr): a Slack thread drafted into an ADR pull request.

Decisions made in Slack threads get lost. `@hangar adr [title]` in a thread on a mapped channel submits a job that
writes one `docs/decisions/NNNN-<slug>.md` from the thread, and hangar opens the PR. A thread is injection aimed at
the team's canon, so:

* **Untrusted, no egress.** The thread reaches the agent only as the untrusted `slack-thread.md` context item
  (slack.py's `_thread_context`, redacted here), which taints the job (#129): its PR opens as a draft labelled
  `hangar:tainted`, opened by hangar after the turn, never by the agent (#130). The sandbox gets egress `none`.
* **Agent-drafted.** The PR is labelled `agent-drafted` and `adr`, titled `ADR: <title>`, and links the thread. Landing
  never counts the requester's (or the job's person's) approval for it, whatever `self_approval` says (landing.py);
  another approver must review it.
* **Checked by hangar.** After the turn hangar verifies the change (`verify`): exactly one new ADR file, numbered next
  in `docs/decisions/`, with the frontmatter and sections of the template (docs/templates/adr.md), and an optional
  fenced `checks:` block that must be valid YAML (hangar never evaluates it: flightdeck runs the predicates). A failure
  fails the job's verify with the reason, and the agent gets a fix round like any failing check.
* **Path guard.** A change outside `docs/decisions/` opens the PR as a draft with a warning in the packet.
* **`checks:` are guarded.** Any PR (not only these) that removes or edits a line inside an existing ADR's `checks:`
  block is risk `high` in the review packet: loosening a team's checks takes a second person (landing.py).
"""

from __future__ import annotations

import hashlib
import re
import shlex
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

if TYPE_CHECKING:
    from hangar.github import GitHub
    from hangar.jobs import JobService
    from hangar.models import ContextItem, Job

KIND_LABEL = "hangar.kind"
KIND = "adr"
PERMALINK_LABEL = "slack.permalink"
HASH_LABEL = "adr.thread_hash"
FILE_LABEL = "adr.file"  # set by verify: the ADR file the job added
PR_LABELS = ["agent-drafted", "adr"]
DECISIONS = "docs/decisions"
TEMPLATE_DOC = "docs/templates/adr.md"
# What the job runs as its check: the directory must exist; hangar's own ADR check (verify) runs after it
VERIFY = f"test -d {DECISIONS}  # and hangar's ADR check (docs/slack.md#hangar-adr)"
REQUIRED_KEYS = ("id", "kind", "status", "label", "sources", "author", "supersedes")
SECTIONS = ("Context", "Decision", "Options considered", "Consequences", "Checks")
_FILE = re.compile(rf"^{DECISIONS}/(\d{{4}})-([a-z0-9][a-z0-9-]*)\.md$")
_FRONT = re.compile(r"\A---\n(.*?)\n---\n", re.S)
_CHECKS = re.compile(r"^```(?:ya?ml)?[ \t]*\n(checks:[^\n]*\n(?:(?!```).*\n)*?)```[ \t]*$", re.M)
PROMPT = Path(__file__).parent / "prompts" / "adr.md"


def is_adr(job: Job) -> bool:
    return job.labels.get(KIND_LABEL) == KIND


def agent_drafted(job: Job) -> bool:
    """Landing's hard rule: the requester's approval never lands it (an ADR job's PR)."""
    return is_adr(job)


def slug(title: str) -> str:
    return re.sub(r"-{2,}", "-", re.sub(r"[^a-z0-9]+", "-", title.lower())).strip("-")[:60].rstrip("-")


def thread_hash(content: str) -> str:
    return "sha256:" + hashlib.sha256(content.encode()).hexdigest()


# ====================================================================== submitting (from slack.py)
def instructions(title: str | None, permalink: str | None, digest: str) -> str:
    """The job's fixed instructions, from src/hangar/prompts/adr.md."""
    t = (title or "").strip()
    return PROMPT.read_text().format(
        title=t or "(none given: choose a short title that names the decision)",
        slug=slug(t) if t else "<slug of your title>", permalink=permalink or "(no permalink: Slack didn't give one)",
        thread_hash=digest, decisions=DECISIONS)


def labels(channel: str, thread_ts: str, user: str, permalink: str | None, digest: str) -> dict[str, str]:
    from hangar import posture

    out = {KIND_LABEL: KIND, "slack.channel": channel, "slack.thread": thread_ts, "slack.user": user,
           HASH_LABEL: digest, posture.FLOOR_LABEL: "none"}  # egress none: the agent reads a thread, nothing else
    if permalink:
        out[PERMALINK_LABEL] = permalink[:500]
    return out


def title(title: str | None) -> str:
    return f"ADR: {title.strip()}"[:200] if title and title.strip() else "ADR (drafted from a Slack thread)"


def redacted(jobs: JobService, items: list[ContextItem]) -> list[ContextItem]:
    """The thread with stored secrets masked: customer data and secrets in a thread never reach the agent or the PR."""
    return [c.model_copy(update={"content": jobs.broker.redact(c.content)}) if c.content else c for c in items]


async def repo_ready(jobs: JobService, repo_url: str) -> bool | None:
    """Whether the repo has `docs/decisions/` on its default branch (None: hangar can't ask GitHub; verify checks)."""
    from hangar.github import GitHubError, parse_repo

    repo = parse_repo(repo_url)
    gh = jobs._github() if repo else None
    if gh is None or repo is None:
        return None
    try:
        got = await gh._req("GET", f"/repos/{repo}/contents/{DECISIONS}")
        return isinstance(got, list)
    except GitHubError as e:
        return False if re.search(r": 404\b", str(e)) else None
    except Exception:  # noqa: BLE001 - a network error: let the job's verify decide
        return None
    finally:
        await gh.close()


# ====================================================================== parsing an ADR file
def checks_block(text: str) -> str | None:
    """The fenced ```yaml block whose first line is `checks:`, verbatim, or None."""
    m = _CHECKS.search(text)
    return m.group(1) if m else None


def problems_in(path: str, text: str, number: int, want_slug: str | None, permalink: str | None) -> list[str]:
    """What's wrong with an ADR file's content (empty: it follows docs/templates/adr.md)."""
    out: list[str] = []
    m = _FRONT.match(text)
    if m is None:
        return [f"{path}: no YAML frontmatter (a `---` block first; see {TEMPLATE_DOC})"]
    try:
        front = yaml.safe_load(m.group(1))
    except yaml.YAMLError as e:
        return [f"{path}: the frontmatter isn't valid YAML: {str(e).splitlines()[0]}"]
    if not isinstance(front, dict):
        return [f"{path}: the frontmatter isn't a mapping"]
    if missing := [k for k in REQUIRED_KEYS if k not in front]:
        out.append(f"{path}: frontmatter lacks {', '.join(missing)}")
    if front.get("kind") != "decision":
        out.append(f"{path}: frontmatter `kind` must be `decision`")
    if front.get("status") != "proposed":
        out.append(f"{path}: frontmatter `status` must be `proposed` (a person accepts it by merging)")
    if str(front.get("id", "")).lstrip("0") != str(number).lstrip("0"):
        out.append(f"{path}: frontmatter `id` must be {number:04d}, the file's number")
    if not str(front.get("author") or "").startswith("agent:"):
        out.append(f"{path}: frontmatter `author` must be `agent:<model>`")
    srcs = front.get("sources")
    if not isinstance(srcs, list) or not srcs:
        out.append(f"{path}: frontmatter `sources` must list the Slack thread (its permalink and content hash)")
    elif permalink and not any(permalink in str(s) for s in srcs):
        out.append(f"{path}: frontmatter `sources` must include the thread's permalink {permalink}")
    if want_slug and not path.endswith(f"-{want_slug}.md"):
        out.append(f"{path}: name it {DECISIONS}/{number:04d}-{want_slug}.md, after the title")
    body = text[m.end():]
    heads = {h.strip() for h in re.findall(r"^##\s+(.+?)\s*$", body, re.M)}
    if missing := [s for s in SECTIONS if s not in heads]:
        out.append(f"{path}: missing section(s) {', '.join('## ' + s for s in missing)}")
    if (block := checks_block(body)) is not None:
        try:
            doc = yaml.safe_load(block)
        except yaml.YAMLError as e:
            first = " ".join(str(e).split())[:300]
            out.append(f"{path}: the `checks:` block isn't valid YAML ({first}); fix its syntax (hangar doesn't run "
                       "the predicates, but it keeps only well-formed ones)")
        else:
            if not isinstance(doc, dict) or not isinstance(doc.get("checks"), list):
                out.append(f"{path}: the `checks:` block must be a YAML list under `checks:`")
    elif re.search(r"^\s*checks:", body, re.M) and "```" not in body.split("## Checks", 1)[-1][:2000]:
        out.append(f"{path}: put `checks:` in a fenced ```yaml block under ## Checks")
    return out


# ====================================================================== after the turn (jobs.py)
async def _git(jobs: JobService, job: Job, cmd: str) -> tuple[int, str]:
    res = await jobs._sh(job, cmd)
    return res.exit_code, res.stdout or ""


async def verify(jobs: JobService, job: Job) -> list[str]:
    """Hangar's ADR check of the job's change against its base: the problems (empty: it passes). Records the ADR
    file it found in the job's labels (`adr.file`), for the packet's numbering check."""
    q = shlex.quote
    base = job.result.base_sha or f"origin/{job.base_branch}"
    code, listing = await _git(jobs, job, f"git ls-tree --name-only {q(base)} -- {DECISIONS}/")
    have = [p for p in listing.splitlines() if p.strip()]
    if code != 0 or not have:
        return [f"the repo has no {DECISIONS}/ directory on its base: a team opts in to `@hangar adr` by adding it "
                f"(and an ADR template: {TEMPLATE_DOC} in hangar's docs)"]
    _, names = await _git(jobs, job, f"git diff --name-status --no-renames {q(base)} HEAD --")
    added = [ln.split("\t", 1)[1] for ln in names.splitlines() if ln.startswith("A\t")]
    adrs = [p for p in added if p.startswith(f"{DECISIONS}/") and p.endswith(".md")]
    if not adrs:
        return [f"no new ADR file: write one {DECISIONS}/NNNN-<slug>.md (see {TEMPLATE_DOC})"]
    if len(adrs) > 1:
        return [f"more than one new ADR file ({', '.join(adrs[:5])}): one decision per PR"]
    path = adrs[0]
    job.labels[FILE_LABEL] = path
    m = _FILE.match(path)
    if m is None:
        return [f"{path}: name it {DECISIONS}/NNNN-<slug>.md (four digits, a lowercase slug)"]
    numbers = [int(x.group(1)) for p in have if (x := re.match(rf"^{DECISIONS}/(\d{{4}})-", p))]
    want = max(numbers, default=0) + 1
    number = int(m.group(1))
    out = []
    if number != want:
        out.append(f"{path}: the next number in {DECISIONS}/ is {want:04d}, not {number:04d}")
    code, text = await _git(jobs, job, f"git show {q('HEAD:' + path)}")
    if code != 0:
        return [*out, f"{path}: hangar couldn't read it from the commit"]
    title = job.title[5:].strip() if job.title.startswith("ADR: ") else None
    out += problems_in(path, text, number, slug(title) if title else None, job.labels.get(PERMALINK_LABEL))
    return out


def outside(job: Job) -> list[str]:
    """Files the change touches outside docs/decisions/ (the path guard: such a PR opens as a draft)."""
    return [f for f in [*job.result.files, *job.result.deleted] if not f.startswith(f"{DECISIONS}/")]


def pr_lines(job: Job) -> list[str]:
    if not is_adr(job):
        return []
    link = job.labels.get(PERMALINK_LABEL)
    return [f"Drafted by an agent from a Slack thread: {link or 'its permalink was not available'} "
            f"(content {job.labels.get(HASH_LABEL, '?')}). A person other than the requester reviews and merges it."]


async def label_pr(jobs: JobService, gh: GitHub, repo: str, number: int, job: Job) -> None:
    """`agent-drafted` and `adr` on the PR: as the App (Pull requests: write covers a PR's labels), else with the
    control token."""
    try:
        await gh.add_labels(repo, number, PR_LABELS)
        return
    except Exception as e:  # noqa: BLE001 - GitHubError or the network: try the control token
        first = e
    ctl = jobs._github()
    if ctl is None:
        jobs._event(job, f"could not label PR #{number} {', '.join(PR_LABELS)}: {first}")
        return
    try:
        await ctl.add_labels(repo, number, PR_LABELS)
    except Exception as e:  # noqa: BLE001
        jobs._event(job, f"could not label PR #{number} {', '.join(PR_LABELS)}: {e}")
    finally:
        await ctl.close()


# ====================================================================== any PR: loosening `checks:` (landing.py)
def _check_lines(text: str | None) -> list[str]:
    block = checks_block(text or "")
    return [ln.strip() for ln in (block or "").splitlines()[1:] if ln.strip() and not ln.strip().startswith("#")]


async def loosened_checks(jobs: JobService, job: Job, files: list[str]) -> list[str]:
    """The existing ADRs whose `checks:` block this PR removes or changes a line of (or deletes with the file)."""
    q = shlex.quote
    base = job.result.base_sha or f"origin/{job.base_branch}"
    out = []
    for f in files:
        if not (f.startswith(f"{DECISIONS}/") and f.endswith(".md")):
            continue
        code, old = await _git(jobs, job, f"git show {q(base + ':' + f)}")
        if code != 0:
            continue  # a new file: nothing to loosen
        before = _check_lines(old)
        if not before:
            continue
        code, new = await _git(jobs, job, f"git show {q('HEAD:' + f)}")
        after = _check_lines(new if code == 0 else "")
        left = list(after)
        for ln in before:
            if ln in left:
                left.remove(ln)
            else:
                out.append(f)
                break
    return out


def packet_lines(jobs: JobService, job: Job) -> list[str]:
    """The packet's ADR lines: provenance, the path guard's warning, a number another ADR job also took."""
    if not is_adr(job):
        return []
    lines = [f"- **ADR:** agent-drafted from a Slack thread ({job.labels.get(PERMALINK_LABEL) or 'no permalink'}, "
             f"content {job.labels.get(HASH_LABEL, '?')}); hangar validated its format and `checks:` syntax and "
             "doesn't run the checks (flightdeck does)"]
    if out := outside(job):
        lines.append("  - **warning:** it changes files outside `docs/decisions/` ("
                     + ", ".join(f"`{p}`" for p in out[:8]) + "), so it opened as a draft: review those first")
    path = job.labels.get(FILE_LABEL)
    if path and (m := _FILE.match(path)):
        same = [j for j in jobs._jobs.values() if j.id != job.id and is_adr(j) and j.repo == job.repo
                and (j.labels.get(FILE_LABEL) or "").startswith(f"{DECISIONS}/{m.group(1)}-")
                and j.landing.pr_state not in ("merged", "closed")]
        for j in same[:3]:
            lines.append(f"  - **numbering conflict:** job `{j.id}`"
                         + (f" (PR #{j.result.pr_number})" if j.result.pr_number else "")
                         + f" also adds ADR {m.group(1)}: whichever merges second renumbers (a follow-up turn)")
    return lines


def context_digest(items: list[ContextItem]) -> str:
    return thread_hash("\n".join(c.content or "" for c in items))
