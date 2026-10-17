"""Landing (--- landing): an agent's PR goes from opened to merged on one human approval, and the job knows it.

The owner's decision 2: agent PRs come from a GitHub App, and the control plane, never the agent, arms auto-merge
after a person approves. GitHub's branch protection stays the gate; hangar only stops waiting for a click.

* **Identity.** With HANGAR_GITHUB_APP_CREDENTIAL set, hangar opens agent PRs, writes their comments and lands them
  as the App. Its installation tokens are minted per repo with `contents: write` and `pull_requests: write` and
  nothing else (no `workflows`, no `administration`), and never leave the control plane. The PR's author is then
  the App, so GitHub lets the person who asked for the work approve it (it refuses an author's own approval). Whether
  that approval lands the PR is the self-approval rule's call (#160, `self_approval` in the repo policy): by default
  only for a low-risk PR (docs and tests) with an independent AI review of the approved head; otherwise a second
  reviewer must approve. It applies only to untainted PRs: a tainted one always needs someone else (#130).
* **State.** `pull_request`, `pull_request_review`, `check_run`, `check_suite` and `status` webhooks keep
  `job.landing` current: open, approved, merged or closed; the head and its checks; when the required checks went
  green and when the PR merged.
* **The gate.** With `land: auto_after_approval` in the repo's policy, an approval of the PR's current head by a
  login in the policy's `approvers`, or by a code owner of every owned file it touches, plus green required checks,
  makes hangar arm auto-merge for exactly that commit (`expectedHeadOid`), or merge it with `sha` when it is already
  mergeable. Nothing pushed later rides along: a push clears the approval and disarms; the job's sandbox (and its
  gateway grant) is checkpointed away before arming, and hangar disarms before any agent turn starts on the job, so
  auto-merge is never armed while an agent can push.
* **The end.** A merged or closed PR ends the job's session: a final checkpoint, then the sandbox goes.
* **The packet.** One comment on each agent PR, from the job record: verification, cost, harness and model,
  files, risk tier, the AI review (#161, aireview.py), who may land it, and links to the session and recorded demos.
  It is edited, not reposted, as the PR changes.
"""

from __future__ import annotations

import asyncio
import base64
import collections
import contextlib
import logging
import re
import shlex
from datetime import datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import httpx

from hangar import aireview, baseline, reviewtrust, weekly
from hangar import modelpolicy as modelpolicy  # --- review #105
from hangar.events import DEFAULT_PRICES, usage_line
from hangar.gateway import AppTokens
from hangar.github import GitHub, GitHubError, parse_repo
from hangar.models import JOB_TERMINAL, ContextItem, Job, JobState, LandingState, ScheduledJob, now

if TYPE_CHECKING:
    from hangar.jobs import JobService
    from hangar.repopolicy import RepoPolicy

log = logging.getLogger("hangar.landing")

EVENTS = ("pull_request", "pull_request_review", "check_run", "check_suite", "status")
PERMISSIONS = {"contents": "write", "pull_requests": "write"}  # what the App's tokens may do; nothing else
PACKET = "<!-- hangar:packet -->"
OK = {"success", "neutral", "skipped"}
FAILED = {"failure", "timed_out", "startup_failure", "cancelled", "action_required", "error", "stale"}
# Always high risk: workflows, CODEOWNERS and the like; --- integ3 (review #102): and `.hangar/`, whose
# environment.yaml install and start commands run in every build (with its build secrets) and every sandbox
DEFAULT_RISK = (".github/**", ".hangar/**")
CODEOWNERS = (".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS")  # GitHub's search order
CACHE_SECONDS = 300  # required checks and CODEOWNERS per (repo, base)
MAX_FILES = 300
MAX_CHECKS = 100
IDLE_WAIT_SECONDS = 600  # how long ending a session waits for a running turn to stop
STALE = "“dismiss stale approvals when new commits are pushed”"  # --- trust: what arming needs of the base branch
# --- #137: `commitBody` ends with the job's trailers (Harness-Digest: ...), so the merged commit carries them
_ARM = ("mutation($id: ID!, $sha: GitObjectID!, $method: PullRequestMergeMethod!, $body: String) {"
        " enablePullRequestAutoMerge(input: {pullRequestId: $id, expectedHeadOid: $sha, mergeMethod: $method,"
        " commitBody: $body}) { pullRequest { number } } }")
_DISARM = "mutation($id: ID!) { disablePullRequestAutoMerge(input: {pullRequestId: $id}) { pullRequest { number } } }"


class LandingError(RuntimeError):
    pass


def _d(x: Any) -> dict[str, Any]:
    return x if isinstance(x, dict) else {}


def _str(x: Any) -> str | None:
    return x if isinstance(x, str) and x else None


def _ts(iso: Any) -> float | None:
    if not isinstance(iso, str):
        return None
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _short(sha: str | None) -> str:
    return (sha or "?")[:7]


def _code(path: str) -> str:
    """A path as inline code in the packet; the agent names the files, so a backtick can't end the span early."""
    return "`" + path.replace("`", "'").replace("\n", " ")[:200] + "`"


# ====================================================================== paths: CODEOWNERS and risk globs
def path_pattern(pattern: str) -> re.Pattern[str]:
    """A CODEOWNERS (gitignore-style) pattern as a regex over repo paths. A leading or inner `/` anchors it at the
    root, otherwise it matches at any depth; a match covers what is under it, except after a final `/*` (GitHub's
    `docs/*` owns docs/a.md, not docs/b/c.md); `*` stays within a segment and `**` crosses them."""
    p = pattern.strip()
    dir_only = p.endswith("/")
    p = p.strip("/")
    anchored = pattern.strip().startswith("/") or "/" in p
    body, i = "", 0
    while i < len(p):
        if p.startswith("**/", i):
            body, i = body + "(?:.*/)?", i + 3
        elif p.startswith("**", i):
            body, i = body + ".*", i + 2
        elif p[i] == "*":
            body, i = body + "[^/]*", i + 1
        elif p[i] == "?":
            body, i = body + "[^/]", i + 1
        else:
            body, i = body + re.escape(p[i]), i + 1
    head = "" if anchored else "(?:.*/)?"
    tail = "/.*" if dir_only else ("" if p.endswith("*") and not p.endswith("**") else "(?:/.*)?")
    return re.compile(f"^{head}{body}{tail}$")


def matches(pattern: str, path: str) -> bool:
    try:
        return bool(path_pattern(pattern).match(path))
    except re.error:
        return False


def owners_of(codeowners: str, path: str) -> list[str]:
    """The owners of `path`: the last matching line wins (a line with no owners leaves the path unowned)."""
    found: list[str] = []
    for line in codeowners.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        pattern, *owners = line.split()
        if matches(pattern, path):
            found = owners
    return found


_TESTS = re.compile(r"(^|/)(tests?|__tests__|spec)/|(^|/)test_[^/]*$|_test\.[^/]+$|\.(test|spec)\.[^/]+$")
_DOCS = re.compile(r"(^|/)docs?/|\.(md|mdx|rst|txt|adoc)$", re.I)


def risk(files: list[str], globs: list[str] | None) -> tuple[str, list[str]]:
    """(tier, the paths that made it high). High: any path matching the policy's `risk_paths` or DEFAULT_RISK;
    low: only docs and tests; normal otherwise."""
    pats = [*DEFAULT_RISK, *(globs or [])]
    high = [f for f in files if any(matches(g, f) for g in pats)]
    if high:
        return "high", high
    if files and all(_DOCS.search(f) or _TESTS.search(f) for f in files):
        return "low", []
    return "normal", []


# ====================================================================== checks
def _check_value(event: str, obj: dict[str, Any], payload: dict[str, Any]) -> tuple[str, str] | None:
    """(name, value) a check event records: its conclusion when done, else "pending"."""
    if event == "check_run":
        name = _str(obj.get("name"))
        done = obj.get("status") == "completed"
        return (name, str(obj.get("conclusion") or "pending") if done else "pending") if name else None
    if event == "check_suite":
        slug = _str(_d(obj.get("app")).get("slug")) or "suite"
        return f"suite:{slug}", str(obj.get("conclusion") or "pending") if obj.get("status") == "completed" \
            else "pending"
    name = _str(payload.get("context"))
    state = payload.get("state")
    return (name, str(state)) if name and isinstance(state, str) else None


def blocking(st: LandingState, sha: str, required: frozenset[str], mergeable_state: str | None = None
             ) -> str | None:
    """Why the checks don't allow landing `sha` yet, or None. A failed check holds it even when it isn't required
    (an agent's red PR goes to auto-fix, not to main); a required one must pass, or GitHub must say the required
    ones did (`clean`/`unstable`/`has_hooks`) when hangar missed their webhooks."""
    seen = st.checks if st.checks_sha == sha else {}
    if failed := sorted(n for n, v in seen.items() if v in FAILED):
        return f"failed: {', '.join(failed[:5])}"
    if required:
        missing = sorted(n for n in required if seen.get(n) not in OK)
        if not missing or mergeable_state in ("clean", "unstable", "has_hooks"):
            return None
        return f"waiting for required checks: {', '.join(missing[:5])}"
    pending = sorted(n for n, v in seen.items() if v not in OK)
    return f"waiting for {', '.join(pending[:5])}" if pending else None


# ====================================================================== the service
class Landing:
    """Per JobService: the App identity, the landing state machine, and the work it does in the background."""

    def __init__(self, svc: JobService):
        self.svc = svc
        self.settings = svc.settings
        self._http: httpx.AsyncClient | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._tokens: AppTokens | None = None
        self._locks: dict[str, asyncio.Lock] = collections.defaultdict(asyncio.Lock)
        self._cache: dict[tuple[str, str, str], tuple[float, Any]] = {}
        # --- taint (#130): reviewers' GitHub user ids by login, from their reviews (people links are by id)
        self._login_ids: dict[str, int] = {}
        self._ai_due: dict[str, str] = {}  # --- AI review (#161): job id -> the head a review is due for
        # --- self-approval (#160): the requester's approval a missing AI review held: job id -> (login, sha, review)
        self._held: dict[str, tuple[str, str, int | None]] = {}

    def _bg(self, job: Job, coro: Any) -> None:
        """Landing work off the webhook's request (tracked, cancelled on shutdown); a failure is said on the job."""

        async def run() -> None:
            try:
                await coro
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - the next event or restart tries again
                log.exception("landing work for %s failed", job.id)
                self.svc._event(job, f"landing: {type(e).__name__}: {e}")

        self.svc._spawn(run())

    # ------------------------------------------------------------------ identity
    def _client(self) -> httpx.AsyncClient:
        loop = asyncio.get_running_loop()
        if self._http is None or self._loop is not loop:
            self._http = httpx.AsyncClient(transport=self.svc.transport, timeout=30, follow_redirects=False)
            self._loop = loop
            self._tokens = AppTokens(self.settings.github_api_url, self._client, PERMISSIONS)
        return self._http

    async def close(self) -> None:
        if self._http is not None:
            with contextlib.suppress(Exception):
                await self._http.aclose()
            self._http = None

    async def client(self, repo: str | None, job: Job | None = None) -> GitHub | None:
        """GitHub as the App, with a token for `repo` only; without an App (or when it can't act on the repo),
        the control plane's token, as before."""
        name = self.settings.github_app_credential
        if name and repo:
            try:
                value = self.svc.broker.secret_text(name)
                self._client()
                tok = await self._tokens.token(value, repo)  # type: ignore[union-attr]
                return GitHub(tok, self.settings.github_api_url, transport=self.svc.transport)
            except Exception as e:  # noqa: BLE001 - the App's errors never carry its key or tokens
                why = f"the GitHub App ({name}) can't act on {repo}: {str(e)[:300]}; using the control token"
                if job is not None:
                    self.svc._event(job, why)
                log.warning("%s", why)
        return self.svc._github()

    async def comment_client(self, job: Job | None, repo: str, number: int) -> GitHub | None:
        """The App for the job's own PR; the control token for anything else (issues need Issues: write, which the
        App doesn't have)."""
        if job is not None and job.result.pr_number == number and \
                (parse_repo(job.repo) or "").lower() == repo.lower():
            return await self.client(repo)
        return self.svc._github()

    # ------------------------------------------------------------------ delivery (jobs.py)
    def opened(self, job: Job, pr: dict[str, Any]) -> None:
        """The PR hangar opened or found for the job."""
        st = job.landing
        st.node_id = _str(pr.get("node_id")) or st.node_id
        st.draft = bool(pr.get("draft"))
        if st.pr_state is None or (st.pr_state == "closed" and pr.get("state") == "open"):
            st.pr_state = "open"
        st.opened_at = st.opened_at or _ts(pr.get("created_at")) or now()  # --- #168: weekly metrics
        if not st.draft and st.ready_at is None:
            st.ready_at = now()

    async def delivered(self, job: Job) -> None:
        """After hangar pushed to the job's PR: what it touches, an approval the push made stale, the packet."""
        repo = parse_repo(job.repo)
        if not repo or not job.result.pr_number:
            return
        st = job.landing
        with contextlib.suppress(Exception):  # the sandbox is the job's own; this is best effort
            sb = self.svc.fleet.get(job.sandbox_id)
            if st.push_mode != "direct":  # once a sandbox held a token, its approvals stay suspect
                st.push_mode = (sb.info.get("git") or {}).get("mode") or st.push_mode
            base = job.result.base_sha or f"origin/{job.base_branch}"
            res = await self.svc._sh(job, f"git diff --name-only {shlex.quote(base)}..HEAD | head -n {MAX_FILES}")
            if res.exit_code == 0:
                st.files = [x for x in res.stdout.splitlines() if x.strip()][:MAX_FILES]
        policy = self.svc.policies.get(job.repo)
        st.risk = risk(st.files, policy.risk_paths if policy else None)[0]
        self.pushed(job, job.result.head_sha, "hangar pushed")
        await self.resolve_approvers(job, policy)  # --- self-approval (#160), review capacity (#159)
        await self.post_packet(job)
        aireview.request(self.svc, job, "hangar pushed")  # --- AI review (#161)

    async def turn_starting(self, job: Job) -> None:
        """Before any agent turn on the job: no auto-merge stays armed while an agent can push. Refuses the turn
        when it can't disarm (try again; the approval itself stands if nothing is pushed)."""
        st = job.landing
        async with self._locks[job.id]:  # always: an arm in flight finishes first, then this sees it
            if st.armed_sha and not await self._disarm(job, "an agent turn is starting on this PR"):
                from hangar.jobs import JobError

                raise JobError("could not disarm the PR's auto-merge before the agent turn; try the follow-up again")

    def idle(self, job: Job) -> None:
        """The job's turn is over (jobs._process): land what waited for it, or end what the PR's end waited for."""
        st = job.landing
        if st.pr_state in ("merged", "closed") and not st.ended:
            self._bg(job, self.end(job))
        elif st.armed_sha and (st.armed_sha != st.head_sha or st.approved_sha != st.armed_sha):
            self._bg(job, self._disarm_locked(job, "the approval it was armed for no longer covers the PR's head"))
        elif st.pr_state == "approved" and not st.armed_sha:
            self._bg(job, self.evaluate(job))

    def restore(self) -> None:
        """After a restart: finish ending sessions, and land what was approved; AI reviews pick up (#161)."""
        for job in list(self.svc._jobs.values()):
            self.idle(job)
        aireview.restore(self.svc)

    def pushed(self, job: Job, sha: str | None, why: str) -> None:
        """A new head on the PR: its checks start over, and an approval of an earlier commit no longer counts."""
        st = job.landing
        if not sha:
            return
        st.head_sha = sha
        if st.checks_sha != sha:
            st.checks, st.checks_sha = {}, sha
        if st.approved_sha and st.approved_sha != sha:
            self.svc._event(job, f"{why} ({_short(sha)}) after @{st.approved_by}'s approval of "
                                 f"{_short(st.approved_sha)}: that approval doesn't cover it; it needs a new review")
            self._unapprove(st)
        if (held := self._held.get(job.id)) is not None and held[1] != sha:
            self._held.pop(job.id, None)  # --- self-approval (#160): it was of an older head
        if st.armed_sha and st.armed_sha != sha:
            self._bg(job, self._disarm_locked(job, f"{why} after it was armed"))
        self.svc._save(job)

    @staticmethod
    def _unapprove(st: LandingState) -> None:
        st.approved_by = st.approved_sha = st.approved_at = st.review_id = None
        if st.pr_state == "approved":
            st.pr_state = "open"

    # ------------------------------------------------------------------ webhooks
    async def handle(self, event: str, payload: dict[str, Any]) -> dict[str, Any]:
        """A verified webhook about one of hangar's PRs: record it now, and do what needs GitHub in the background
        (a webhook is answered within GitHub's 10 seconds; landing work must not ride on the request)."""
        from hangar.autofix import find_job

        repo = _str(_d(payload.get("repository")).get("full_name")) or ""
        action = payload.get("action")
        if event in ("pull_request", "pull_request_review"):
            pr = _d(payload.get("pull_request"))
            number = pr.get("number") if isinstance(pr.get("number"), int) else 0
            job = find_job(self.svc, repo, [number], [], None)
            if job is None:
                return {"ignored": "PR was not opened by a hangar job"}
            st = job.landing
            st.node_id = _str(pr.get("node_id")) or st.node_id
            head = _str(_d(pr.get("head")).get("sha"))
            if event == "pull_request":
                return self._pull_request(job, action, pr, head, payload)
            return self._review(job, action, _d(payload.get("review")), head)
        obj = _d(payload.get(event)) if event != "status" else payload
        if event != "status" and action != "completed" and not (event == "check_run" and action == "created"):
            return {"ignored": f"{event} {action}"}
        sha = _str(obj.get("head_sha") if event != "status" else payload.get("sha"))
        numbers = [p.get("number") for p in obj.get("pull_requests") or [] if isinstance(_d(p).get("number"), int)]
        branch = obj.get("head_branch") or _d(obj.get("check_suite")).get("head_branch")
        branches = [b for b in [branch] if isinstance(b, str)]
        if event == "status":
            branches = [n for b in payload.get("branches") or [] if isinstance(n := _d(b).get("name"), str)]
        job = find_job(self.svc, repo, numbers, branches, None)
        if job is None:
            return {"ignored": "not a hangar job's PR"}
        st = job.landing
        # hangar's own push is the head before `delivered` records it: its checks can arrive first
        if not sha or sha not in (st.head_sha, job.result.head_sha):
            return {"ignored": f"not the PR's head ({_short(st.head_sha or job.result.head_sha)})"}
        got = _check_value(event, obj, payload)
        if got is None:
            return {"ignored": "no check name"}
        if st.checks_sha != sha:
            st.checks, st.checks_sha = {}, sha
        if got[0] in st.checks or len(st.checks) < MAX_CHECKS:
            st.checks[got[0]] = got[1]
        self.svc._save(job)
        aireview.record_check(self.svc, job, got[0], got[1], sha)  # --- AI review (#161): `check:<name>`
        if got[1] in OK and st.ci_green_sha != sha:
            self._bg(job, self._green(job, sha))
        return {"ok": True, "landing": job.id, "check": got[0], "value": got[1]}

    def _pull_request(self, job: Job, action: Any, pr: dict[str, Any], head: str | None,
                      payload: dict[str, Any]) -> dict[str, Any]:
        st = job.landing
        st.draft = bool(pr.get("draft"))
        if action == "closed":
            if pr.get("merged"):
                st.pr_state = "merged"
                st.merged_at = _ts(pr.get("merged_at")) or now()
                st.merged_by = _str(_d(pr.get("merged_by")).get("login"))
                st.merge_commit_sha = _str(pr.get("merge_commit_sha")) or st.merge_commit_sha  # --- #168: reverts
                st.armed_sha = None
                took = ""
                if st.ci_green_at and st.ci_green_sha and st.ci_green_sha == head:
                    st.green_to_merge_seconds = max(0.0, st.merged_at - st.ci_green_at)
                    took = f"; {round(st.green_to_merge_seconds / 60, 1)} min from green checks to merged"
                self.svc._event(job, f"PR #{job.result.pr_number} merged" + (f" by @{st.merged_by}" if st.merged_by
                                                                            else "") + took)
                self.svc.fleet.audit(f"github:{st.merged_by or '?'}", "landing.merged", job.id,
                                     pr=job.result.pr_number, sha=head, approved_by=st.approved_by,
                                     armed=bool(st.armed_at), green_to_merge_seconds=st.green_to_merge_seconds)
            else:
                st.pr_state, st.closed_at, st.armed_sha = "closed", now(), None
                who = _str(_d(payload.get("sender")).get("login"))
                self.svc._event(job, f"PR #{job.result.pr_number} closed without merging" +
                                (f" by @{who}" if who else ""))
            self.svc._save(job)
            if job.id in self.svc._active:
                self._stop_turn(job)
            else:
                self.idle(job)
            return {"ok": True, "landing": job.id, "pr_state": st.pr_state}
        if action in ("opened", "ready_for_review", "reopened") and not st.draft and st.ready_at is None:
            st.ready_at = now()  # --- #168: review wait starts here
        if action == "labeled" and (reason := weekly.label_reason(_d(payload.get("label")).get("name"))) is not None:
            who = _str(_d(payload.get("sender")).get("login")) or "?"
            if why := weekly.set_abandon(self.svc, job, reason, f"github:{who}", "label"):  # --- #168
                self.svc._event(job, f"label {weekly.ABANDON_LABEL}{reason[:40]}: {why}")
        if action == "reopened":
            st.closed_at, st.ended = None, False
            st.pr_state = "approved" if st.approved_sha and st.approved_sha == (head or st.head_sha) else "open"
        elif action == "synchronize":
            self.pushed(job, head, "a push to the branch")
            aireview.request(self.svc, job, "a push to the branch")  # --- AI review (#161): debounced
        elif action in ("ready_for_review", "converted_to_draft") and st.pr_state == "approved":
            self._bg(job, self.evaluate(job))
        if action == "ready_for_review":  # --- AI review (#161): drafts (tainted PRs open as drafts) wait for this
            if head and not st.head_sha:
                st.head_sha = head
            aireview.request(self.svc, job, "ready for review")
        elif action == "auto_merge_disabled" and st.armed_sha:
            st.armed_sha = st.armed_at = None
            self.svc._event(job, "auto-merge was turned off on GitHub")
        elif action == "opened" and st.pr_state is None:
            st.pr_state = "open"
        if head and not st.head_sha:
            st.head_sha = head
        self.svc._save(job)
        return {"ok": True, "landing": job.id, "pr_state": st.pr_state}

    def _review(self, job: Job, action: Any, review: dict[str, Any], head: str | None) -> dict[str, Any]:
        st = job.landing
        user = _str(_d(review.get("user")).get("login")) or "?"
        if isinstance(uid := _d(review.get("user")).get("id"), int) and not isinstance(uid, bool):
            self._login_ids[user.lower()] = uid  # --- taint (#130)
        state = str(review.get("state") or "").lower()
        rid = review.get("id") if isinstance(review.get("id"), int) else None
        if action == "dismissed":
            if st.review_id is not None and rid == st.review_id:
                self.svc._event(job, f"@{user}'s approval was dismissed")
                self._unapprove(st)
                if st.armed_sha:
                    self._bg(job, self._disarm_locked(job, "the approval was dismissed"))
                self.svc._save(job)
            return {"ok": True, "landing": job.id, "pr_state": st.pr_state}
        if action != "submitted":
            return {"ignored": f"review {action}"}
        if st.first_review_at is None and not reviewtrust.is_bot(_d(review.get("user"))):  # --- #168
            st.first_review_at = _ts(review.get("submitted_at")) or now()
            st.first_review_by = user
            self.svc._save(job)
        if state == "changes_requested":
            if st.approved_sha:
                self.svc._event(job, f"@{user} requested changes after @{st.approved_by}'s approval: not landing")
                self._unapprove(st)
                if st.armed_sha:
                    self._bg(job, self._disarm_locked(job, f"@{user} requested changes"))
                self.svc._save(job)
            return {"ok": True, "landing": job.id, "pr_state": st.pr_state}
        if state != "approved":
            return {"ignored": f"review {state}"}
        sha = _str(review.get("commit_id"))
        current = head or st.head_sha or job.result.head_sha
        if not sha or sha != current:
            self.svc._event(job, f"@{user} approved {_short(sha)}, not the PR's head {_short(current)}: it doesn't "
                                 "count")
            return {"ignored": "approval of an older commit"}
        self._bg(job, self._approved(job, user, sha, rid))
        return {"ok": True, "landing": job.id, "approval": user}

    # ------------------------------------------------------------------ background work
    async def _approved(self, job: Job, user: str, sha: str, rid: int | None) -> None:
        async with self._locks[job.id]:
            st = job.landing
            if st.pr_state in ("merged", "closed"):
                return
            if sha not in (st.head_sha, job.result.head_sha):  # hangar's own push may not be recorded yet
                return self.svc._event(job, f"@{user}'s approval of {_short(sha)} came after a newer push: it "
                                            "doesn't count")
            policy = self.svc.policies.get(job.repo)
            # who may approve matters only where an approval lands the PR; elsewhere GitHub's rules decide
            auto = policy is not None and policy.land == "auto_after_approval"
            why = await self.qualifies(job, user) if auto else "an approving review"
            if why is None:
                self.svc._event(job, f"@{user} approved {_short(sha)}, but isn't in the repo policy's approvers or "
                                     "a code owner of what it touches: it doesn't land the PR")
                return
            if auto and (refusal := self._self_approval(job, user, sha)) is not None:  # --- self-approval (#160)
                self._refuse_self(job, user, sha, rid, refusal)
                return
            self._held.pop(job.id, None)
            if st.needs_second and not self._is_own(job, user):
                st.needs_second = False
            st.approved_by, st.approved_sha, st.approved_at, st.review_id = user, sha, now(), rid
            st.pr_state = "approved"
            self.svc._event(job, f"approved by @{user} ({why}) on {_short(sha)}")
            self.svc.fleet.audit(f"github:{user}", "landing.approved", job.id, pr=job.result.pr_number, sha=sha,
                                 via=why)
            await self._evaluate(job)

    async def evaluate(self, job: Job) -> None:
        async with self._locks[job.id]:
            await self._evaluate(job)

    def _note(self, job: Job, note: str) -> None:
        if job.landing.note != note:
            job.landing.note = note
            self.svc._event(job, f"not landing yet: {note}")

    async def _evaluate(self, job: Job) -> None:
        """Arm auto-merge for the approved head when everything allows it. Under the job's lock."""
        st, svc = job.landing, self.svc
        repo, number = parse_repo(job.repo), job.result.pr_number
        if st.pr_state != "approved" or not st.approved_sha or not repo or not number:
            return
        policy = svc.policies.get(job.repo)
        if policy is None or policy.land != "auto_after_approval":
            return self._note(job, "the repo's policy lands PRs by hand (land: manual): merge it on GitHub")
        if job.id in svc._active or (job.state not in JOB_TERMINAL and job.state != JobState.NEEDS_INPUT):
            return self._note(job, "an agent turn is queued or running on this job")
        if st.push_mode == "direct":  # the agent held a GitHub token: the approval may be its own
            return self._note(job, "its sandbox held a GitHub token (credential_mode: direct), so hangar doesn't "
                                   "land it; merge it on GitHub, or use the git gateway")
        if st.push_mode != "gateway":  # deny by default: unknown is not "held no token"
            return self._note(job, "hangar couldn't tell whether its sandbox pushed through the git gateway (or held "
                                   "a GitHub token), so it doesn't land it; merge it on GitHub")
        if st.armed_sha == st.approved_sha:
            return
        gh = await self.client(repo, job)
        if gh is None:
            return self._note(job, "no GitHub credential (HANGAR_GITHUB_APP_CREDENTIAL)")
        try:
            pr = _d(await gh._req("GET", f"/repos/{repo}/pulls/{int(number)}"))
            head = _str(_d(pr.get("head")).get("sha"))
            st.node_id = _str(pr.get("node_id")) or st.node_id
            if pr.get("state") != "open" or pr.get("merged"):
                return
            if head != st.approved_sha:
                self.pushed(job, head, "the PR moved")
                return
            st.head_sha = head  # GitHub's word; a push from here on changes it (and `_arm` looks)
            if pr.get("draft"):
                return self._note(job, "the PR is a draft: mark it ready for review to land it")
            if why := await self.stale_gate(gh, repo, _str(_d(pr.get("base")).get("ref")) or "main", policy):
                return self._note(job, why)  # --- trust
            required = await self.required_checks(gh, repo, _str(_d(pr.get("base")).get("ref")) or "main")
            if why := blocking(st, head, required, _str(pr.get("mergeable_state"))):
                return self._note(job, why)
            if why := await self._review_gone(gh, repo, int(number), st):
                self.svc._event(job, f"{why}: not landing")
                self._unapprove(st)
                return self._note(job, why)
            # Nothing the agent left running can push past the approval once armed: its sandbox (and with it the
            # gateway grant) goes first. A follow-up resumes from the checkpoint, and disarms before it starts.
            if not await self._release(job, "approved: the sandbox goes before auto-merge is armed"):
                return self._note(job, "the job's sandbox couldn't take its checkpoint, so it stays and auto-merge "
                                       "isn't armed")
            await self._arm(job, gh, repo, int(number), head, policy)
        except (GitHubError, httpx.HTTPError) as e:
            svc._event(job, f"landing: GitHub refused a call: {e}")
        finally:
            await gh.close()

    async def _arm(self, job: Job, gh: GitHub, repo: str, number: int, sha: str, policy: RepoPolicy) -> None:
        st, method = job.landing, (policy.merge_method or "squash")
        approved_by = st.approved_by  # a push while GitHub answers clears it
        if not st.node_id:
            raise LandingError("the PR has no node id")
        body = self.commit_body(job) if method != "rebase" else None  # --- #137: a rebase keeps the commits
        try:
            await gh.graphql(_ARM, {"id": st.node_id, "sha": sha, "method": method.upper(), "body": body})
        except GitHubError as e:
            if "clean status" not in str(e).lower():
                return self._note(job, f"GitHub didn't arm auto-merge: {str(e).removeprefix('GitHub GraphQL: ')}")
            # Already mergeable, so GitHub won't arm auto-merge: merge it now, only if the head is the approved one.
            try:
                await gh._req("PUT", f"/repos/{repo}/pulls/{number}/merge", json={
                    "sha": sha, "merge_method": method,
                    **({"commit_message": body} if body else {})})  # --- #137: ends with the trailers
            except GitHubError as m:
                if " 409 " in str(m):  # `sha` no longer the head: someone pushed after the approval
                    self._unapprove(st)
                    return self._note(job, "the head moved before the merge; it needs a new review")
                return self._note(job, f"GitHub refused the merge: {m}")
            st.note = None
            self.svc._event(job, f"merging {_short(sha)} ({method}) after @{st.approved_by}'s approval: it was "
                                 "already mergeable")
            self.svc.fleet.audit("hangar", "landing.merge", job.id, pr=number, sha=sha, approved_by=st.approved_by)
            self.svc._save(job)
            return
        st.armed_sha, st.armed_at, st.note = sha, now(), None
        self.svc._event(job, f"auto-merge armed for {_short(sha)} ({method}) after @{approved_by}'s approval; "
                             "GitHub merges it once its branch rules are met")
        self.svc.fleet.audit("hangar", "landing.arm", job.id, pr=number, sha=sha, approved_by=approved_by)
        # A push, a dismissal or a follow-up handled while GitHub was arming found nothing armed to undo
        # (`pushed` and the review handlers run off the lock): look again now that there is.
        svc = self.svc
        if st.head_sha != sha or st.approved_sha != sha or job.id in svc._active or \
                (job.state not in JOB_TERMINAL and job.state != JobState.NEEDS_INPUT):
            await self._disarm(job, "the PR moved, its approval went, or an agent turn was queued while arming")

    def commit_body(self, job: Job) -> str:
        """The merge commit's message body (#137): the agent's summary, then the job's trailers (`Hangar-Job`,
        `Harness-Digest`, ...), redacted. GitHub uses it for a squash or merge commit; a rebase keeps the commits
        as they are, so the trailers stay in the PR body alone."""
        from hangar import trailers

        summary = (job.result.summary or "").strip()
        return self.svc.broker.redact((summary + trailers.block(self.svc, job)).strip())

    async def _review_gone(self, gh: GitHub, repo: str, number: int, st: LandingState) -> str | None:
        """Why the approval hangar holds no longer stands on GitHub, or None. Review webhooks can come late, out of
        order (redeliver.py replays missed ones after a restart) or replayed, so before arming, GitHub's own record
        decides: the approver's latest review is an approval of this commit, and nobody's latest requests
        changes. The list is oldest first; a GitHub error raises, so nothing is armed."""
        latest: dict[str, tuple[str, str | None]] = {}
        for page in range(1, 11):
            got = await gh._req("GET", f"/repos/{repo}/pulls/{number}/reviews",
                                params={"per_page": 100, "page": page}) or []
            for r in got:
                login, state = _str(_d(_d(r).get("user")).get("login")), str(_d(r).get("state") or "").upper()
                if login and state in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED"):
                    latest[login.lower()] = (state, _str(r.get("commit_id")))
            if len(got) < 100:
                break
        else:
            return "the PR has more reviews than hangar reads"
        mine = latest.get((st.approved_by or "").lower())
        if mine != ("APPROVED", st.approved_sha):
            return f"@{st.approved_by}'s approval of {_short(st.approved_sha)} no longer stands on GitHub"
        if blockers := sorted(n for n, (s, _) in latest.items() if s == "CHANGES_REQUESTED"):
            return "changes requested by " + ", ".join(f"@{n}" for n in blockers[:5])
        return None

    async def _disarm_locked(self, job: Job, why: str) -> None:
        async with self._locks[job.id]:
            await self._disarm(job, why)

    async def _disarm(self, job: Job, why: str) -> bool:
        """Turn auto-merge off. True when it is off (also when GitHub says it already was)."""
        st, repo = job.landing, parse_repo(job.repo)
        if not st.armed_sha:
            return True
        gh = await self.client(repo, job) if repo else None
        if gh is None or not st.node_id:
            self.svc._event(job, f"could not disarm auto-merge ({why}): no GitHub credential or PR id")
            return False
        try:
            try:
                await gh.graphql(_DISARM, {"id": st.node_id})
            except (GitHubError, httpx.HTTPError) as e:
                # refused: off already (or the PR is no longer open) counts as off; anything else is a failure
                pr = _d(await gh._req("GET", f"/repos/{repo}/pulls/{int(job.result.pr_number or 0)}"))
                if pr.get("state") == "open" and pr.get("auto_merge") is not None:
                    self.svc._event(job, f"could not disarm auto-merge ({why}): {e}")
                    return False
        except (GitHubError, httpx.HTTPError) as e:
            self.svc._event(job, f"could not disarm auto-merge ({why}): {e}")
            return False
        finally:
            await gh.close()
        self.svc._event(job, f"auto-merge disarmed: {why}")
        self.svc.fleet.audit("hangar", "landing.disarm", job.id, pr=job.result.pr_number, sha=st.armed_sha, why=why)
        st.armed_sha = st.armed_at = None
        self.svc._save(job)
        return True

    async def _green(self, job: Job, sha: str) -> None:
        """Note when the required checks first all passed on the head (the start of 'green to merged')."""
        async with self._locks[job.id]:
            st, repo = job.landing, parse_repo(job.repo)
            if st.ci_green_sha == sha or st.checks_sha != sha or not repo:
                return
            if any(v not in OK for v in st.checks.values()):
                return
            gh = await self.client(repo)
            if gh is None:
                return
            try:
                required = await self.required_checks(gh, repo, job.base_branch or "main")
            finally:
                await gh.close()
            if blocking(st, sha, required) is None and (required or st.checks):
                st.ci_green_at, st.ci_green_sha = now(), sha
                self.svc._event(job, f"required checks passed on {_short(sha)}")
            if st.pr_state == "approved" and not st.armed_sha:
                await self._evaluate(job)

    def _stop_turn(self, job: Job) -> None:
        """The PR is gone; a running turn can't change that. It stops, and `idle` ends the session after."""
        if not job.cancel_requested:
            job.cancel_requested = True
            self.svc._event(job, f"PR #{job.result.pr_number} is {job.landing.pr_state}: stopping the agent's turn")

    async def end(self, job: Job) -> None:
        """A merged or closed PR: the session takes a final checkpoint and lets the sandbox go."""
        async with self._locks[job.id]:
            st = job.landing
            if st.ended or st.pr_state not in ("merged", "closed"):
                return
            if job.id in self.svc._active:
                return self._stop_turn(job)  # idle() comes back when the turn is over
            from hangar import hot_resume

            st.armed_sha = None
            job.autofix_state.pending, job.autofix_state.due_at = [], None
            why = f"PR #{job.result.pr_number} {st.pr_state}"
            if not await self._release(job, why):
                return  # the work isn't saved: its sandbox stays until it idles out, which checkpoints again
            if (s := self.svc._session(job)) is not None:
                await hot_resume.discard(self.svc.sessions, s.id)  # a paused hot-resume sandbox too
            st.ended = True
            if job.state == JobState.NEEDS_INPUT:
                self.svc._event(job, f"{why}: its question no longer needs an answer", JobState.CANCELLED,
                                reason=f"pr_{st.pr_state}")
            self.svc._event(job, f"{why}: the session's work is in its final checkpoint and its sandbox is gone")

    async def _release(self, job: Job, why: str) -> bool:
        """Checkpoint the job's session and let its sandbox go. False when the checkpoint failed (the sandbox stays,
        so no work is lost)."""
        s = self.svc._session(job)
        try:
            if s is not None and s.sandbox_id:
                await self.svc.sessions.release(s, why, checkpoint=True)
        except Exception as e:  # noqa: BLE001
            self.svc._event(job, f"{why}; the session couldn't take its checkpoint, so its sandbox stays: {e}")
            return False
        if s is not None and job.sandbox_id:
            job.sandbox_id = None
            self.svc._save(job)
        return True

    # ------------------------------------------------------------------ what GitHub says about the repo
    async def _cached(self, kind: str, repo: str, base: str, load) -> Any:
        key = (kind, repo.lower(), base)
        hit = self._cache.get(key)
        if hit and hit[0] > now():
            return hit[1]
        value = await load()
        self._cache[key] = (now() + CACHE_SECONDS, value)
        return value

    async def required_checks(self, gh: GitHub, repo: str, base: str) -> frozenset[str]:
        """The status checks the base branch requires: from its protection summary (`GET /branches/{b}`, Contents:
        read) and its rulesets (`GET /rules/branches/{b}`, Metadata: read); neither needs Administration."""

        async def load() -> frozenset[str]:
            names: set[str] = set()
            b = quote(base, safe="/")
            with contextlib.suppress(GitHubError, httpx.HTTPError):
                rs = _d(_d(_d(await gh._req("GET", f"/repos/{repo}/branches/{b}")).get("protection"))
                        .get("required_status_checks"))
                names |= {c for c in rs.get("contexts") or [] if isinstance(c, str)}
                names |= {c["context"] for c in rs.get("checks") or [] if isinstance(_d(c).get("context"), str)}
            with contextlib.suppress(GitHubError, httpx.HTTPError):
                for rule in await gh._req("GET", f"/repos/{repo}/rules/branches/{b}", params={"per_page": 100}) or []:
                    if _d(rule).get("type") == "required_status_checks":
                        names |= {c["context"] for c in _d(rule.get("parameters")).get("required_status_checks")
                                  or [] if isinstance(_d(c).get("context"), str)}
            return frozenset(names)

        return await self._cached("required", repo, base, load)

    async def stale_gate(self, gh: GitHub, repo: str, base: str, policy: RepoPolicy) -> str | None:
        """--- trust (#96 finding 7): why auto-merge can't be armed on `base`, or None. Hangar disarms on every push it
        hears of, but a person's push whose webhook it misses could merge on the approval of an earlier commit unless
        GitHub itself dismisses that approval. So the base must dismiss stale approvals (branch protection or a
        ruleset), unless the repo policy opts out (`require_stale_dismissal: false`). Unreadable counts as missing."""
        if policy.require_stale_dismissal is False:
            return None

        async def load() -> tuple[bool, bool]:
            admin = self.svc._github()  # classic protection needs Administration: read, which the App doesn't have
            try:
                missing, seen = await _protection(admin or gh, gh, repo, base)
            finally:
                if admin is not None:
                    await admin.close()
            return STALE not in missing, seen

        has, seen = await self._cached("stale", repo, base, load)
        opt_out = f"or opt the repo out: `hangar repo set {repo} --no-require-stale-dismissal`"
        if not seen:
            return (f"hangar couldn't read {base}'s branch protection or rulesets to check that it dismisses stale "
                    "approvals, so it doesn't arm auto-merge: give the control token Administration: read, use a "
                    f"ruleset, {opt_out}")
        if not has:
            return (f"{base} doesn't dismiss stale approvals when new commits are pushed, so a push after the approval "
                    f"could merge unreviewed: hangar doesn't arm auto-merge. Turn it on (`hangar doctor` says where), "
                    f"{opt_out}")
        return None

    async def codeowners(self, gh: GitHub, repo: str, base: str) -> str | None:
        async def load() -> str | None:
            for path in CODEOWNERS:
                try:
                    f = _d(await gh._req("GET", f"/repos/{repo}/contents/{path}", params={"ref": base}))
                except GitHubError:
                    continue
                if f.get("encoding") == "base64" and isinstance(f.get("content"), str):
                    return base64.b64decode(f["content"]).decode("utf-8", "replace")
            return None

        return await self._cached("codeowners", repo, base, load)

    async def qualifies(self, job: Job, login: str) -> str | None:
        """Why `login`'s approval lands the PR ("approver", "code owner"), or None. A tainted job (#130) also needs
        someone other than the job's own person or requester."""
        if (why := self._taint_refusal(job, login)) is not None:
            self.svc._event(job, why)
            return None
        return await self._qualifies(job, login)

    def _taint_refusal(self, job: Job, login: str) -> str | None:
        """--- taint (#130): the reason `login` can't land a tainted job's PR, or None. The approver's GitHub account
        must not be linked to the job's person, nor be (or be linked to whoever is) its requester: approvals.can_decide
        with kind="land", whose self-approval rule is the one every other approval uses (HANGAR_SELF_APPROVAL
        allow_solo included)."""
        from hangar import approvals, people, taint

        if not taint.tainted(job):
            return None
        uid = self._login_ids.get(login.lower())
        person = people.resolve_external(self.svc.store, "github", uid) if uid else None
        who = approvals.Approver(f"github:{login}", person.id if person else None, "github",
                                 frozenset({f"github:{login}", f"github:{login.lower()}"}),
                                 frozenset({login.lower()}), front_door=True)
        subject = approvals.Subject.of_job(job)._replace(what=f"PR #{job.result.pr_number}")
        d = approvals.can_decide(self.svc, who, subject, "land")
        if d.ok or not d.self_approved:  # only the self-approval part is this gate's; landing has its own approvers
            taint.audit(self.svc.fleet, f"github:{login}", "land", "allowed", job.id, pr=job.result.pr_number,
                        rule=d.rule if d.ok else None, self_approved=d.self_approved)
            return None
        taint.audit(self.svc.fleet, f"github:{login}", "land", "refused", job.id, pr=job.result.pr_number,
                    person=person.id if person else None)
        return (f"@{login} approved, but the agent read untrusted content (hangar:tainted) and @{login} is the job's "
                "own person or requester: another approver must review it before it lands")

    # ------------------------------------------------------------------ self-approval (#160)
    def _person_of_login(self, login: str) -> str | None:
        """The hangar person a GitHub login's account is linked to, by its numeric user id (never the login: a
        renamed login can be registered by anyone). Known from the account's reviews, or `resolve_approvers`."""
        from hangar import people

        uid = self._login_ids.get(login.lower())
        p = people.resolve_external(self.svc.store, "github", uid) if uid else None
        return p.id if p else None

    def _is_own(self, job: Job, login: str) -> bool:
        return bool(job.person) and self._person_of_login(login) == job.person

    def _self_approval(self, job: Job, login: str, sha: str) -> str | None:
        """Why the job's own person's approval doesn't land its PR, or None (it does, or it isn't theirs).

        Precisely: it applies to a person's job (team jobs, with no person, are unchanged) when the approving GitHub
        account (by numeric id) is linked to that person. It runs after the taint gate (#130), which has already
        refused the person's own approval of a tainted PR whatever this rule says, so it can only refuse more, never
        allow what that gate refused. The repo policy's `self_approval`: `any` lands it as before; `never` doesn't;
        `low_risk_with_ai_review` (the default) lands it only when the PR is low risk (docs and tests only) and an
        independent AI review (#161) of the approved head isn't blocking. HANGAR_SELF_APPROVAL=allow_solo lets it
        through while exactly one person exists, as for every other approval (there is no second reviewer)."""
        from hangar import people
        from hangar.repopolicy import DEFAULT_SELF_APPROVAL

        if not self._is_own(job, login):
            return None
        policy = self.svc.policies.get(job.repo)
        rule = (policy.self_approval if policy else None) or DEFAULT_SELF_APPROVAL
        st = job.landing
        if rule == "any":
            return None
        if getattr(self.settings, "self_approval", "deny") == "allow_solo" and \
                len(people.list_people(self.svc.store)) == 1:
            return None
        if rule == "never":
            why = "the repo policy's self_approval is never"
        elif st.risk != "low":
            why = f"risk {st.risk or 'unknown'}"
        elif aireview.counts_for(st, sha):
            return None
        else:
            r = st.ai_review or {}
            if r.get("head_sha") == sha and r.get("verdict") == "blocking":
                why = "the AI review of this commit is blocking"
            elif r.get("head_sha") == sha and r.get("verdict") in ("approve", "comment") and not r.get("independent"):
                why = "the AI review of this commit ran on the author's own harness"
            else:
                why = f"low risk, but no independent AI review of {_short(sha)} yet"
        return f"self-approval doesn't land this PR ({why}): it needs a second reviewer"

    def _refuse_self(self, job: Job, login: str, sha: str, rid: int | None, why: str) -> None:
        st = job.landing
        st.needs_second = True
        if st.pr_state == "approved" and st.approved_by and st.approved_by.lower() == login.lower():
            self._unapprove(st)
        if "no independent AI review" in why:  # an AI review on its way may still let this approval count
            self._held[job.id] = (login, sha, rid)
        self.svc._event(job, f"@{login}'s approval of {_short(sha)}: {why}")
        self._note(job, why)
        self.svc.fleet.audit(f"github:{login}", "landing.self_approval", job.id, pr=job.result.pr_number, sha=sha,
                             decision="refused", why=why, risk=st.risk)
        self.svc._save(job)

    async def retry_held_approval(self, job: Job) -> None:
        """An AI review was recorded: a requester's approval that waited for it is looked at again."""
        held = self._held.get(job.id)
        if held is None or held[1] != (job.landing.head_sha or job.result.head_sha):
            return
        await self._approved(job, *held)

    def needs_second_reviewer(self, job: Job) -> bool:
        """Whether the PR needs someone other than the job's own person to land it (#160, #130): what review
        capacity (#159) counts against the repo's approvers."""
        from hangar import taint
        from hangar.repopolicy import DEFAULT_SELF_APPROVAL

        st = job.landing
        if not job.person:
            return False
        if taint.tainted(job) or st.needs_second:
            return True
        policy = self.svc.policies.get(job.repo)
        if policy is None or policy.land != "auto_after_approval":
            return False
        rule = policy.self_approval or DEFAULT_SELF_APPROVAL
        if rule == "any":
            return False
        return rule == "never" or st.risk != "low" or not aireview.counts_for(st, st.head_sha or job.result.head_sha)

    async def resolve_approvers(self, job: Job, policy: RepoPolicy | None) -> None:
        """The hangar people linked to the repo policy's approvers (other than the job's own person): who a second
        review falls to. Logins are looked up for their numeric ids (`GET /users/{login}`, cached)."""
        from hangar import people

        st = job.landing
        if not job.person or policy is None or not policy.approvers:
            return
        found: list[str] = []
        gh = None
        try:
            for login in policy.approvers[:20]:
                uid = self._login_ids.get(login.lower())
                if uid is None:
                    if gh is None:
                        gh = self.svc._github() or await self.client(parse_repo(job.repo))
                    if gh is None:
                        break
                    with contextlib.suppress(GitHubError, httpx.HTTPError):
                        user = _d(await gh._req("GET", f"/users/{quote(login)}"))
                        if isinstance(user.get("id"), int) and not isinstance(user.get("id"), bool):
                            uid = self._login_ids[login.lower()] = user["id"]
                p = people.resolve_external(self.svc.store, "github", uid) if uid else None
                if p is not None and p.id != job.person and p.id not in found:
                    found.append(p.id)
        finally:
            if gh is not None:
                await gh.close()
        if found != st.approver_people:
            st.approver_people = found
            self.svc._save(job)

    def landing_rule(self, job: Job, policy: RepoPolicy | None) -> str:
        """The packet's "Landing rule" line: who may land it, and why."""
        from hangar import taint
        from hangar.repopolicy import DEFAULT_SELF_APPROVAL

        if policy is None or policy.land != "auto_after_approval":
            return "- **Landing rule:** merged by hand on GitHub (the repo policy's `land` is manual)"
        if not job.person:
            return "- **Landing rule:** any approver or code owner (a team job: nobody's own work)" + \
                (", other than whoever asked for it (it read untrusted content)" if taint.tainted(job) else "")
        if taint.tainted(job):
            return ("- **Landing rule:** an approver other than the job's own person: the agent read untrusted "
                    "content, so its requester can't be the reviewer")
        rule = policy.self_approval or DEFAULT_SELF_APPROVAL
        st = job.landing
        if rule == "any":
            return "- **Landing rule:** any approver, the job's own person included (`self_approval: any`)"
        if rule == "never":
            return "- **Landing rule:** a second reviewer: the job's own person can't be its only approver " \
                   "(`self_approval: never`)"
        if st.risk != "low":
            return (f"- **Landing rule:** a second reviewer: risk {st.risk or 'unknown'}, and only a low-risk PR "
                    "(docs and tests) with an independent AI review may land on its requester's approval")
        if aireview.counts_for(st, st.head_sha or job.result.head_sha):
            return ("- **Landing rule:** the job's own person may approve it alone: low risk, and an independent AI "
                    "review of this head isn't blocking")
        return ("- **Landing rule:** low risk: the job's own person may approve it alone once an independent AI "
                "review of this head isn't blocking; until then, a second reviewer")

    async def _qualifies(self, job: Job, login: str) -> str | None:
        policy = self.svc.policies.get(job.repo)
        if policy and login.lower() in {a.lower() for a in policy.approvers or []}:
            return "approver in the repo policy"
        repo, number = parse_repo(job.repo), job.result.pr_number
        gh = await self.client(repo) if repo else None
        if gh is None or not number:
            return None
        try:
            text = await self.codeowners(gh, repo, job.base_branch or "main")  # type: ignore[arg-type]
            if not text:
                return None
            files: list[str] = []
            for page in (1, 2, 3):
                got = await gh._req("GET", f"/repos/{repo}/pulls/{int(number)}/files",
                                    params={"per_page": 100, "page": page}) or []
                for f in got:  # a rename changes its old path too: moving a file out of infra/ touches infra/
                    files += [p for k in ("filename", "previous_filename") if isinstance(p := _d(f).get(k), str)]
                if len(got) < 100:
                    break
            else:
                return None  # more than hangar reads: the files it didn't see may be someone else's
        except (GitHubError, httpx.HTTPError) as e:
            log.info("landing: CODEOWNERS for %s: %s", repo, e)
            return None
        finally:
            await gh.close()
        # A code owner lands only what they own: every path the PR touches. An unowned one needs an approver from the
        # policy, or a code owner of one docs file could land changes anywhere.
        owned = [owners_of(text, f) for f in files]
        if not owned or not all(owned):
            return None
        teams: dict[str, bool] = {}
        for owners in owned:
            for o in owners:
                if await self._is_owner(login, o, teams):
                    break
            else:
                return None
        return "code owner"

    async def _is_owner(self, login: str, owner: str, teams: dict[str, bool]) -> bool:
        if owner.lower() == f"@{login.lower()}":
            return True
        if not owner.startswith("@") or "/" not in owner:
            return False  # an email, or someone else
        if owner not in teams:
            org, team = owner[1:].split("/", 1)
            gh = self.svc._github()  # team membership needs the org's Members: read, which the App doesn't hold
            teams[owner] = False
            if gh is not None:
                try:
                    m = await gh.team_membership(org, team, login)
                    teams[owner] = bool(m and m.get("state") == "active")
                except (GitHubError, httpx.HTTPError):
                    pass
                finally:
                    await gh.close()
        return teams[owner]

    # ------------------------------------------------------------------ the review packet
    def packet(self, job: Job) -> str:
        svc, r, st = self.svc, job.result, job.landing
        policy = svc.policies.get(job.repo)
        out = [PACKET, f"**Review packet** · hangar job `{job.id}`", ""]
        if job.verify and r.verify_exit_code is not None:
            v = baseline.verdict(job, r.verify_exit_code, r.new_failures)
            word = {"pass": "passed", "fail": f"failed (exit {r.verify_exit_code})",
                    "unknown": "failed on a red base; hangar can't tell if this change added failures"}[v]
            out.append(f"- **Verification:** `{job.verify}` {word}")
            if red := baseline.pr_line(job):
                out.append(f"  - {red}")
        else:
            out.append("- **Verification:** none ran" + ("" if job.verify else
                                                          " (no verify command; `hangar repo set` sets one)"))
        if r.touched_tests:
            out.append("  - the agent changed the check's own tests: "
                       + ", ".join(_code(p) for p in r.touched_tests[:10]))
        from hangar import testresults  # --- testresults: counts, new against the base, flaky, coverage

        out += testresults.packet_lines(svc, job)
        from hangar import taint  # --- taint (#129): what untrusted content the agent read

        if line := taint.packet_line(job):
            out.append(line)
        from hangar import harness_thaw  # --- harness locks (#134): the lock it ran from, turn 1's drift

        if line := harness_thaw.packet_line(svc, job):
            out.append(line)
        tier, why = risk(st.files, policy.risk_paths if policy else None)
        out.append(f"- **Risk:** {tier}" + (": touches " + ", ".join(_code(p) for p in why[:8]) if why else ""))
        shown = ", ".join(_code(f) for f in st.files[:15]) + (f" and {len(st.files) - 15} more"
                                                                if len(st.files) > 15 else "")
        stat = r.diffstat.splitlines()[-1].strip() if r.diffstat else ""
        out.append(f"- **Files:** {len(st.files)}" + (f" ({stat})" if stat else "") + (f": {shown}" if shown else ""))
        cost = f"- **Cost:** {usage_line(svc.usage(job))}"
        if b := job.job_budget:
            caps = [f"${b.max_cost_usd:g}" if b.max_cost_usd else "", f"{b.max_tokens:,} tokens" if b.max_tokens
                    else "", f"{b.max_turns} turns" if b.max_turns else ""]
            cost += " · job budget " + ", ".join(c for c in caps if c)
        else:
            cost += " · no job budget (`job_budget` in the repo policy, or HANGAR_JOB_BUDGET_MAX_COST_USD)"
        out.append(cost + f" · {len([t for t in job.turns if t.run_id])} agent turn(s)")
        version = next((t.harness_version for t in reversed(job.turns) if t.harness_version), None)
        s = svc._session(job)
        # --- review #105: the id the job's session ran (a policy, resolved and pinned there), and its price
        model = modelpolicy.describe(getattr(s, "model", None) or job.model, getattr(s, "model_policy", None),
                                     {**DEFAULT_PRICES, **{k.lower(): v for k, v in
                                                           (self.settings.model_prices or {}).items()}})
        from hangar import trailers  # --- #137: the harness digest(s) and lock it ran with, linking to the manifest

        tv = trailers.values(svc, job)
        harness = ""
        if tv["harness"]:
            url = f"{self.settings.public_url.rstrip('/')}/api/jobs/{job.id}/manifest"
            many = len(tv["harness"]) > 1
            shown = [f"[`{trailers.short(d)}`]({url})" + (f" ({trailers.ranges(ns)})" if many else "")
                     for d, ns in tv["harness"]]
            harness = " · harness " + ", ".join(shown) + (f" (lock `{tv['lock']}`)" if tv["lock"] else "")
        out.append(f"- **Agent:** {job.harness or 'default harness'}" + (f" {version}" if version else "")
                   + f" · model {model} · environment `{job.environment}`" + harness)
        if s:
            out.append(f"- **Session:** `{s.name}`: `hangar session pull {s.name}` checks it out locally; reply "
                       "`/hangar <message>` here to have the agent continue")
        base = self.settings.public_url.rstrip("/")
        demos = [a for a in r.artifacts if a.media_type.startswith("video/") or a.media_type == "image/gif"
                 or "demo" in a.path.lower()]
        if demos:
            out.append("- **Recorded demos:** " + ", ".join(
                f"[{_code(a.path)}]({base}/api/jobs/{job.id}/artifacts/{a.turn}/{quote(a.path)})" for a in demos[:10]))
        from hangar import autoinstall  # --- autoinstall: an env setup's PR: what was validated, mocked, missing

        out += autoinstall.packet_lines(svc, job)
        from hangar import posture  # --- intake posture (#123): the egress floor, or what warn let through

        try:
            pspec = svc.fleet.catalog.get(job.environment)
        except KeyError:
            pspec = None
        out += posture.packet_lines(self.settings, job, pspec)
        if line := aireview.packet_line(job):  # --- AI review (#161)
            out.append(line)
        elif aireview.wanted(svc, job) is not None:
            out.append("- **AI review:** pending")
        out.append(self.landing_rule(job, policy))  # --- self-approval (#160)
        if policy and policy.land == "auto_after_approval":
            who = ", ".join(f"@{a}" for a in policy.approvers or []) or "a code owner"
            out.append(f"- **Landing:** an approval from {who}" + (" or a code owner" if policy.approvers else "")
                       + " on the latest commit lands it once the required checks pass "
                         f"({policy.merge_method or 'squash'}); a later push needs a new review")
        else:
            out.append("- **Landing:** by hand (the repo policy's `land` is manual)")
        out += ["", f"_Requested by {job.requester} via {job.source}. Generated by hangar from the job record._"]
        return svc.broker.redact("\n".join(out))

    async def post_packet(self, job: Job) -> None:
        """Write the packet, or edit the one already there."""
        repo, number, st = parse_repo(job.repo), job.result.pr_number, job.landing
        if not repo or not number:
            return
        gh = await self.client(repo, job)
        if gh is None:
            return
        try:
            body = self.packet(job)
            if st.packet_comment_id:
                try:
                    await gh.edit_comment(repo, st.packet_comment_id, body)
                    return
                except GitHubError as e:
                    if " 404 " not in str(e):
                        raise
                    st.packet_comment_id = None  # deleted on GitHub: write a new one
            made = await gh.comment(repo, int(number), body)
            st.packet_comment_id = made.get("id") if isinstance(made, dict) and isinstance(made.get("id"), int) \
                else None
            self.svc._save(job)
        except Exception as e:  # noqa: BLE001 - the packet never fails a delivery
            self.svc._event(job, f"could not post the review packet: {e}")
        finally:
            await gh.close()


# ====================================================================== schedules (triggers.py)
def open_pr_job(svc: JobService, s: ScheduledJob) -> Job | None:
    """The schedule's last job, when its PR is still open: the next run updates that PR instead of opening one."""
    if not s.last_job_id:
        return None
    job = svc._jobs.get(s.last_job_id) or svc.store.get(Job, s.last_job_id)
    if job is None or not job.result.pr_number or job.landing.pr_state not in ("open", "approved"):
        return None
    return job


def update_open_pr(svc: JobService, s: ScheduledJob, run: str) -> Job | None:
    """Run the schedule as a follow-up on its open PR. None: there is none, or it can't take one (then a new job
    runs, and the old PR's job says why)."""
    from hangar.jobs import JobError

    job = open_pr_job(svc, s)
    if job is None:
        return None
    t = s.template
    msg = (f"Scheduled run ({run}) of '{s.name}'. Your PR #{job.result.pr_number} from the last run is still open: "
           f"update it for this run rather than starting over.\n\n{t.get('instructions') or ''}")
    try:
        ctx = [ContextItem.model_validate(c) for c in t.get("context") or []]
        return svc.follow_up(job.id, msg, requester=f"schedule:{s.name}", context=ctx)
    except (JobError, ValueError) as e:
        svc._event(job, f"schedule {s.name}: couldn't continue this PR ({e}); starting a new job instead")
        return None


# ====================================================================== hangar doctor
async def doctor_checks(h: Any) -> list[dict[str, Any]]:
    """For each repo whose policy lands on approval: the branch protection that keeps it safe, auto-merge allowed,
    the App able to mint its tokens, and pushes that don't put a GitHub token in the sandbox."""
    from hangar.doctor import _check

    svc: JobService = h.jobs
    s = h.settings
    repos = [p for p in svc.policies.list() if p.land == "auto_after_approval" and parse_repo(f"https://github.com/"
                                                                                               f"{p.repo}")]
    out: list[dict[str, Any]] = []
    if not repos:
        return out
    if not s.github_app_credential:
        out.append(_check("landing identity", None, "HANGAR_GITHUB_APP_CREDENTIAL is not set: agent PRs are opened "
                          "with the control token, and its owner can't approve PRs they authored",
                          "create a GitHub App (docs/landing.md), then hangar config set "
                          "HANGAR_GITHUB_APP_CREDENTIAL <credential>"))
    if not s.gateway_url:  # --- least-privilege: with it, jobs always fetch through it and never push
        out.append(_check("landing: agent tokens", None, "jobs don't use the git gateway, so a job's sandbox holds a "
                          "GitHub token that could push and arm auto-merge itself; branch protection is then the "
                          "only gate, and hangar doesn't land such jobs",
                          "set HANGAR_GATEWAY_URL (docs/security.md#git-credential-gateway)"))
    for p in repos:
        repo = p.repo
        gh = await svc.landing.client(repo)
        if gh is None:
            out.append(_check(f"landing {repo}", False, "no GitHub credential to read the repo with",
                              "set HANGAR_GITHUB_APP_CREDENTIAL or HANGAR_GITHUB_TOKEN_CREDENTIAL"))
            continue
        admin = svc._github()  # classic protection needs Administration: read, which the App doesn't have
        try:
            info = _d(await gh._req("GET", f"/repos/{repo}"))
            base = _str(info.get("default_branch")) or "main"
            missing, seen = await _protection(admin or gh, gh, repo, base)
            if info.get("allow_auto_merge") is False:
                missing.append("“Allow auto-merge” in the repo's settings")
            if not await svc.landing.required_checks(gh, repo, base):
                missing.append("required status checks (CI must pass before a merge)")
            fix = (f"https://github.com/{repo}/settings/branches (or a ruleset on {base}): require a pull request "
                   "with 1+ approvals, dismiss stale approvals when new commits are pushed, require approval of "
                   "the most recent reviewable push, and required status checks; and allow auto-merge "
                   f"(https://github.com/{repo}/settings)")
            unread = "" if seen else (" (hangar couldn't read the branch protection; give the control token "
                                      "Administration: read, or use a ruleset)")
            if (STALE in missing or not seen) and p.require_stale_dismissal is not False:  # --- trust: arming refuses
                out.append(_check(f"landing {repo}", False, f"{base} is missing: {'; '.join(missing)}{unread}. Until "
                                  f"it dismisses stale approvals, hangar won't arm auto-merge on {repo}",
                                  fix + f"; or opt out: hangar repo set {repo} --no-require-stale-dismissal"))
            elif missing:
                out.append(_check(f"landing {repo}", None, f"{base} is missing: {'; '.join(missing)}{unread}" +
                                  ("; the repo policy opted out of requiring stale approvals to be dismissed"
                                   if p.require_stale_dismissal is False else ""), fix))
            else:
                out.append(_check(f"landing {repo}", True, f"{base} needs a fresh approval of the latest push and "
                                  "passing checks before anything merges"))
        except (GitHubError, httpx.HTTPError) as e:
            out.append(_check(f"landing {repo}", None, f"could not read the repo: {e}",
                              "install the GitHub App on the repo, or give the control token access"))
        finally:
            await gh.close()
            if admin is not None:
                await admin.close()
    return out


async def _protection(admin: GitHub, gh: GitHub, repo: str, base: str) -> tuple[list[str], bool]:
    """What the base branch's protection or rulesets lack of what landing relies on, and whether either could be
    read. Classic protection needs Administration: read; `GET /rules/branches/{b}` needs Metadata: read."""
    have = {"reviews": False, "stale": False, "last_push": False}
    seen = False
    b = quote(base, safe="/")
    try:
        prot = _d(_d(await admin._req("GET", f"/repos/{repo}/branches/{b}/protection"))
                  .get("required_pull_request_reviews"))
        seen = True
        have["reviews"] |= (prot.get("required_approving_review_count") or 0) >= 1
        have["stale"] |= bool(prot.get("dismiss_stale_reviews"))
        have["last_push"] |= bool(prot.get("require_last_push_approval"))
    except GitHubError as e:
        seen = seen or " 404 " in str(e) and "not protected" in str(e).lower()
    with contextlib.suppress(GitHubError, httpx.HTTPError):
        for rule in await gh._req("GET", f"/repos/{repo}/rules/branches/{b}", params={"per_page": 100}) or []:
            seen = True
            if _d(rule).get("type") == "pull_request":
                prm = _d(rule.get("parameters"))
                have["reviews"] |= (prm.get("required_approving_review_count") or 0) >= 1
                have["stale"] |= bool(prm.get("dismiss_stale_reviews_on_push"))
                have["last_push"] |= bool(prm.get("require_last_push_approval"))
    names = {"reviews": "required approving reviews", "stale": STALE,  # --- trust: STALE
             "last_push": "“require approval of the most recent reviewable push”"}
    return [names[k] for k, v in have.items() if not v], seen
