"""Secret scanning before anything leaves (--- egress): commits, commit messages, PR text and artifacts.

What counts as a secret: a stored credential's value (exact, the strongest signal), or a match for one of a few
high-signal token formats: GitHub, Slack, AWS, Google, Anthropic and OpenAI keys, private key blocks, and hangar's
own tokens. Only these: a pattern that fires on ordinary code would stop deliveries for nothing, and an agent told
"possible secret" too often learns to ignore it.

Where it runs:

* **A job's delivery.** Before hangar pushes the job's branch, the change since the base (committed, uncommitted and
  untracked files hangar would commit) and the new commits' messages are scanned. A hit blocks the push. While
  rounds are left the agent gets a follow-up turn naming each file and line and the kind of secret (never the
  value), to remove it, rewrite the commits that carry it, and finish; then hangar scans again.
* **PR titles and bodies** hangar writes: hits are masked before GitHub sees them.
* **Artifacts** handed back: text is masked; a binary file holding a stored credential's value is withheld.

Fast on large diffs: one pass per pattern over the whole text (each pattern starts with a literal, which CPython's
regex engine finds with a fast scan), a `find` per stored value, and file and line worked out only for hits.
"""

from __future__ import annotations

import re
import shlex
from typing import TYPE_CHECKING, NamedTuple

from hangar.models import Turn

if TYPE_CHECKING:
    from hangar.broker import Broker
    from hangar.jobs import JobService
    from hangar.models import Job

MAX_SCAN_BYTES = 64 * 1024 * 1024  # what hangar reads of a change; past it the rest isn't scanned, and it says so
MASK = "[hangar: secret removed]"

# (kind, pattern). Formats from the issuers' own documentation (checked 2026-10-14): GitHub's token prefixes
# (github.blog "Behind GitHub's new authentication token formats"; fine-grained PATs are github_pat_ + 82 chars),
# Slack token prefixes and incoming-webhook URLs (api.slack.com/authentication/token-types), AWS access key ids
# (docs.aws.amazon.com IAM "Understanding unique ID prefixes": AKIA long-term, ASIA temporary), Google API keys
# (AIza) and OAuth client secrets (GOCSPX-), Anthropic keys (sk-ant-api03-…, OAuth sk-ant-oat01-…), OpenAI keys
# (sk-proj-, sk-svcacct-, sk-admin-, and the legacy sk-…T3BlbkFJ… form), PEM/OpenSSH private keys, and hangar's
# own API tokens (hgr_) and git grants (hgs_).
_RAW: tuple[tuple[str, str], ...] = (
    ("GitHub token", r"\bgh[pousr]_[A-Za-z0-9]{36,255}\b"),
    ("GitHub fine-grained token", r"\bgithub_pat_[A-Za-z0-9_]{82}\b"),
    ("Slack token", r"\bxox[abposre]-[A-Za-z0-9-]{10,250}"),
    ("Slack app token", r"\bxapp-\d-[A-Z0-9]+-\d+-[a-z0-9]{32,}"),
    ("Slack webhook URL", r"https://hooks\.slack\.com/(?:services|workflows|triggers)/[A-Za-z0-9_/-]{20,}"),
    ("AWS access key id", r"\bA(?:KIA|SIA|BIA|CCA)[A-Z0-9]{16}\b"),
    ("AWS secret access key", r"(?i)aws_?secret_?access_?key[\"']?\s*[:=]\s*[\"']?[A-Za-z0-9/+]{40}(?![A-Za-z0-9/+])"),
    ("Google API key", r"\bAIza[0-9A-Za-z_-]{35}(?![0-9A-Za-z_-])"),
    ("Google OAuth client secret", r"\bGOCSPX-[A-Za-z0-9_-]{28}(?![A-Za-z0-9_-])"),
    ("GCP service account key", r"\"private_key_id\"\s*:\s*\"[0-9a-f]{40}\""),
    ("Anthropic key", r"\bsk-ant-[a-z]{2,8}\d{2}-[A-Za-z0-9_-]{40,}"),
    ("OpenAI key", r"\bsk-(?:proj|svcacct|admin)-[A-Za-z0-9_-]{20,}"),
    ("OpenAI key", r"\bsk-[A-Za-z0-9]{20}T3BlbkFJ[A-Za-z0-9]{20}\b"),
    ("private key", r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED |PGP )?PRIVATE KEY(?: BLOCK)?-----"),
    ("hangar token", r"\bhg[rs]_[A-Za-z0-9_-]{40,}"),
)
# A leading \b would stop CPython's regex engine from scanning for the pattern's literal prefix (a 0.6 s pass over
# 60 MB instead of 0.03 s), so it is checked on the few matches instead. Patterns without a literal prefix get a
# cheap guard: the regex runs only when a substring it needs is present.
PATTERNS: tuple[tuple[str, re.Pattern[str], bool], ...] = tuple(
    (k, re.compile(p.removeprefix(r"\b")), p.startswith(r"\b")) for k, p in _RAW)
_GUARDS = {"AWS secret access key": ("ecret", "ECRET")}
# Documentation placeholders the formats above also match (AWS's AKIAIOSFODNN7EXAMPLE and friends).
_PLACEHOLDER = re.compile(r"(?i)example|x{8,}|0{16,}|1234567890abcdef|your[_-]?(?:key|token)")


class Hit(NamedTuple):
    kind: str  # "GitHub token", or "stored credential"
    where: str  # "src/config.py:12", "commit 1a2b3c4d message", "PR body"

    def line(self) -> str:
        return f"{self.where}: {self.kind}"


def _values(broker: Broker | None) -> list[str]:
    return broker._secrets() if broker is not None else []


def find(text: str, broker: Broker | None = None) -> list[tuple[int, int, str]]:
    """(start, end, kind) of every secret in `text`, stored values first. Overlaps are kept; callers dedupe."""
    from hangar.redaction import Redactor

    out: list[tuple[int, int, str]] = []
    vals = _values(broker)
    for kind, values in (("stored credential", vals), ("stored credential, encoded", Redactor(vals).encoded)):
        for v in values:
            i = text.find(v)
            while i != -1:
                out.append((i, i + len(v), kind))
                i = text.find(v, i + len(v))
    for kind, rx, boundary in PATTERNS:
        if (g := _GUARDS.get(kind)) and not any(x in text for x in g):
            continue
        for m in rx.finditer(text):
            s = m.start()
            if boundary and s > 0 and (text[s - 1].isalnum() or text[s - 1] == "_"):
                continue
            if not _PLACEHOLDER.search(m.group(0)):
                out.append((s, m.end(), kind))
    return out


def mask(text: str, broker: Broker | None = None) -> tuple[str, list[str]]:
    """`text` with every secret replaced by MASK, and the kinds found (for an event that says what was removed)."""
    spans = sorted(find(text, broker))
    if not spans:
        return text, []
    out, pos, kinds = [], 0, []
    for start, end, kind in spans:
        if end <= pos:
            continue
        out.append(text[pos:max(pos, start)])
        out.append(MASK)
        pos = end
        kinds.append(kind)
    out.append(text[pos:])
    return "".join(out), sorted(set(kinds))


def artifact(broker: Broker, data: bytes, patterns: bool = True) -> bytes | None:
    """An artifact as it may leave the sandbox: UTF-8 text redacted (stored values and their encodings) and, with
    `patterns`, secret-shaped tokens masked; a binary file is returned as is unless it holds a stored value, then
    None (it can't be masked without breaking it)."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None if any(v.encode() in data for v in _values(broker)) else data
    red = broker.redact(text)
    if patterns:
        red, _ = mask(red)
    return red.encode() if red != text else data


def clean(jobs: JobService, job: Job | None, text: str, what: str) -> str:
    """Text hangar is about to post (a PR title or body, a comment): secrets masked, and a job event (never the
    value) when there were any. Off with HANGAR_SECRET_SCAN=off."""
    if jobs.settings.secret_scan == "off" or not text:
        return text
    out, kinds = mask(text, jobs.broker)
    if kinds and job is not None:
        jobs._event(job, f"removed what looks like a secret ({', '.join(kinds)}) from {what}")
    return out


# ============================================================================ scanning a change
_SECTION_COMMITS = "\0HANGAR-SCAN commits\n"
_SECTION_DIFF = "\0HANGAR-SCAN diff\n"


def scan_script(repo: str, base: str, out: str, cap: int = MAX_SCAN_BYTES) -> str:
    """A shell script, run in the sandbox, that writes what would leave to `out`: the new commits' messages, then
    the change since `base` as a zero-context diff (committed and uncommitted), then untracked files that aren't
    ignored, as added lines. Binary files are skipped (git prints "Binary files differ"). At most `cap` bytes."""
    q = shlex.quote
    return (
        f"set -o pipefail; cd {q(repo)} || exit 3; {{ "
        f"printf '\\0HANGAR-SCAN commits\\n'; "
        f"git log --no-color --format='commit %H%n%B%x00' {q(base)}..HEAD -- 2>/dev/null; "
        f"printf '\\0HANGAR-SCAN diff\\n'; "
        f"git -c core.quotepath=off diff --no-color --no-ext-diff --no-textconv --no-renames -U0 {q(base)} --; "
        "git -c core.quotepath=off ls-files -z --others --exclude-standard | "
        "while IFS= read -r -d '' f; do [ -f \"$f\" ] && [ ! -L \"$f\" ] || continue; "
        "grep -Iq . \"$f\" 2>/dev/null || continue; "
        "printf '+++ b/%s\\n@@ -0,0 +1 @@\\n' \"$f\"; sed 's/^/+/' \"$f\"; echo; done; "
        f"}} | head -c {int(cap)} > {q(out)}")


def locate(text: str, start: int) -> str | None:
    """Where an offset in a scan bundle is: "path:line" for an added line, "commit <sha> message" for a commit
    message, None for a removed or context line (a secret the change takes away isn't leaving)."""
    diff_at = text.find(_SECTION_DIFF)
    if diff_at == -1 or start < diff_at:
        c = text.rfind("commit ", 0, start)
        while c > 0 and text[c - 1] not in "\n\0":
            c = text.rfind("commit ", 0, c)
        sha = text[c + 7:c + 47] if c != -1 else ""
        return f"commit {sha[:12]} message" if re.fullmatch(r"[0-9a-f]{40}", sha) else "a commit message"
    ls = text.rfind("\n", 0, start) + 1
    if not text.startswith("+", ls) or text.startswith("+++ ", ls):
        return None
    hunk = text.rfind("\n@@ ", diff_at, start)
    head = text.rfind("\n+++ ", diff_at, start)
    if hunk == -1 or head == -1 or head > hunk:
        return None
    path = text[head + 5:text.find("\n", head + 1)]
    path = path[2:] if path.startswith("b/") else path
    m = re.match(r"@@ -\S+ \+(\d+)", text[hunk + 1:hunk + 64])
    first = int(m.group(1)) if m else 1
    added = text.count("\n+", hunk, ls)  # added lines between the hunk header and this one
    return f"{path}:{first + added}"


def scan_bundle(text: str, broker: Broker | None = None, limit: int = 50) -> list[Hit]:
    """The secrets in a scan bundle that would leave: in added lines and commit messages."""
    hits: list[Hit] = []
    seen: set[tuple[str, str]] = set()
    for start, _end, kind in sorted(find(text, broker)):
        where = locate(text, start)
        if where is None or (where, kind) in seen:
            continue
        seen.add((where, kind))
        hits.append(Hit(kind, where))
        if len(hits) >= limit:
            break
    return hits


class Blocked(RuntimeError):
    """What would be pushed holds a secret (or couldn't be read to check)."""

    def __init__(self, hits: list[Hit], reason: str | None = None):
        self.hits, self.reason = hits, reason
        lines = "; ".join(h.line() for h in hits[:5]) + (f"; and {len(hits) - 5} more" if len(hits) > 5 else "")
        super().__init__(f"not pushed: {reason or 'possible secret in ' + lines}")


async def scan_sandbox(jobs: JobService, sandbox_id: str, base: str, actor: str) -> list[Hit]:
    """Scan what pushing the sandbox's repo (HEAD and its working tree) over `base` would deliver. Raises Blocked
    when the change can't be read: hangar never pushes what it couldn't check."""
    fleet = jobs.fleet
    sb = fleet.get(sandbox_id)
    repo = sb.info.get("repo_path")
    if not repo:
        return []
    out = f"/tmp/hangar-secretscan-{sandbox_id}"
    res = await fleet.exec(sandbox_id, scan_script(repo, base, out), timeout=300, actor=actor)
    if res.exit_code not in (0, 141):  # 141: head closed the pipe at the cap
        raise Blocked([], "hangar couldn't read the change to scan it for secrets: "
                          f"{jobs.broker.redact((res.stderr or res.stdout)[-300:].strip()) or res.exit_code}")
    data = await fleet.read_file(sandbox_id, out, limit=MAX_SCAN_BYTES)
    await fleet.exec(sandbox_id, f"rm -f {shlex.quote(out)}", timeout=30, actor=actor)
    if data is None:
        raise Blocked([], "the secret scan's output is missing")
    return scan_bundle(data[:MAX_SCAN_BYTES].decode("utf-8", errors="replace"), jobs.broker)


async def before_push(jobs: JobService, job: Job, sandbox_id: str | None = None) -> None:
    """Raise Blocked if delivering the job would push a secret (HANGAR_SECRET_SCAN=block, the default); with
    `warn` the hits only become job events."""
    mode = jobs.settings.secret_scan
    if mode == "off":
        return
    base = job.result.base_sha or f"origin/{job.base_branch}"
    try:
        hits = await scan_sandbox(jobs, sandbox_id or job.sandbox_id or "", base, f"job:{job.id}")
    except Blocked:
        if mode == "block":
            raise
        return
    if not hits:
        return
    jobs.fleet.audit(f"job:{job.id}", "secret.found", job.id, hits=[h.line() for h in hits[:50]], mode=mode,
                     person=job.person)
    if mode == "block":
        raise Blocked(hits)
    jobs._event(job, "possible secret in what is pushed: " + "; ".join(h.line() for h in hits[:5]))


def fix_turn(jobs: JobService, job: Job, turn: Turn, blocked: Blocked) -> Turn | None:
    """The follow-up turn that tells the agent where the secrets are, or None (with a job event saying why) when
    rounds are used up, the job's budget is spent, or the block wasn't about a secret it can remove."""
    from hangar import jobbudget

    if not blocked.hits or job.cancel_requested:
        return None
    of = jobs.settings.secret_scan_rounds
    used = 0
    for t in reversed(job.turns):
        if t.trigger != "secrets":
            break
        used += 1
    if used >= of:
        jobs._event(job, f"secrets are still in the change after {used} round(s) of fixes; not pushing it")
        return None
    if spent := jobbudget.exhausted(jobs, job):
        jobs._event(job, f"a secret is in the change; no fix turn: {spent}")
        return None
    jobs._event(job, f"a secret is in the change; sending it back to the agent (round {used + 1} of {of})")
    return Turn(kind="follow_up", requester="hangar", reply_to=turn.reply_to, trigger="secrets", budget=turn.budget,
                message=fix_message(blocked.hits, used + 1, of))


def fix_message(hits: list[Hit], n: int, of: int) -> str:
    lines = "\n".join(f"- {h.line()}" for h in hits[:30])
    more = f"\n- … and {len(hits) - 30} more" if len(hits) > 30 else ""
    return (f"hangar didn't push your branch: what it would deliver contains what looks like a secret (round {n} of "
            f"{of}):\n\n{lines}{more}\n\nRemove each one, and keep it out of history: amend or rebase the commits "
            "that carry it (a later commit that deletes it still pushes the old one). Read secrets from the "
            "environment or a file outside the repo instead. If one is a test fixture or a placeholder, replace it "
            "with an obviously fake value. Then finish; hangar scans again before it pushes.")
