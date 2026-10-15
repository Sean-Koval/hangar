"""GitHub Projects front door: an issue moved to a configured status column on a project board becomes a job.

GitHub sends `projects_v2_item` only to organization webhooks and GitHub Apps with the organization's "Projects" read
permission (GitHub's webhook docs; the event is in public preview), never to repository webhooks, and only for
organization-owned projects. Point the organization webhook (or the App's) at /api/hooks/github with hangar's GitHub
webhook secret: the event is verified, deduplicated and replay-checked there like issues and comments, then handled
here. An `edited` delivery carries the item's node ids and, for a single-select change, the field and option it
moved to; the issue itself (repository, number, text) and the item's current status come from one GraphQL query,
which also guards against an out-of-order delivery for an item that has moved on since.

The job is the issue's job (`source_ref` "github:owner/repo#N"), so everything issues already do works unchanged:
`/hangar approve` for an untrusted mover, `/hangar <answer>` to a question, "Closes #N" on the PR, comments on the
issue. What this adds is the board: hangar sets the item's status as the job starts, asks, delivers or fails.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from hangar import autoinstall as autoinstall  # --- autoinstall: an issue labelled for an env setup
from hangar import intake
from hangar.github import GitHubError
from hangar.jobs import JobService, SubmitJob
from hangar.models import ContextItem, Job, JobState

log = logging.getLogger("hangar.ghprojects")

_BOARD = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}/[1-9][0-9]{0,8}$")
_NODE = re.compile(r"^[A-Za-z0-9_=-]{1,100}$")
_REPO = re.compile(r"^[\w.-]+/[\w.-]+$")
FIELD_CACHE_SECONDS = 600


# ============================================================================ configuration (HANGAR_GITHUB_PROJECTS)
class Statuses(BaseModel):
    """The status column (single-select option name) to move the item to at each stage; None leaves it."""

    model_config = ConfigDict(extra="forbid")

    started: str | None = None
    needs_input: str | None = None
    delivered: str | None = None
    failed: str | None = None


class Board(BaseModel):
    model_config = ConfigDict(extra="forbid")

    trigger: str = Field(min_length=1)  # the status column that starts a job
    status_field: str = "Status"  # the single-select field the columns are
    repos: list[str] = Field(default_factory=list)  # owner/repo; empty: any repo in HANGAR_GITHUB_REPOS
    statuses: Statuses = Field(default_factory=Statuses)

    @model_validator(mode="after")
    def _check(self) -> Board:
        bad = [r for r in self.repos if not _REPO.match(r)]
        if bad:
            raise ValueError(f"repos are owner/repo: {', '.join(bad)}")
        loops = [k for k, v in self.statuses.model_dump().items() if v and v.casefold() == self.trigger.casefold()]
        if loops:  # hangar moving the item into the trigger column would start the job again
            raise ValueError(f"statuses.{loops[0]} is the trigger column")
        return self


class ProjectsConfigError(ValueError):
    pass


def boards(settings: Any) -> dict[str, Board]:
    """HANGAR_GITHUB_PROJECTS, validated, keyed by lower-case "org/number"."""
    out: dict[str, Board] = {}
    for key, raw in (settings.github_projects or {}).items():
        try:
            if not _BOARD.match(key):
                raise ValueError(f"'{key}' is not org/number")
            out[key.lower()] = Board.model_validate(raw)
        except (ValidationError, ValueError) as e:
            log.error("HANGAR_GITHUB_PROJECTS['%s'] is invalid: %s", key, e)
            raise ProjectsConfigError(f"GitHub Projects board '{key}' is misconfigured") from e
    return out


# ============================================================================ GraphQL
def graphql_url(api_url: str) -> str:
    """api.github.com/graphql; on GitHub Enterprise Server, <host>/api/v3 -> <host>/api/graphql."""
    base = api_url.rstrip("/")
    return base[: -len("/v3")] + "/graphql" if base.endswith("/api/v3") else base + "/graphql"


ITEM_QUERY = """
query($item: ID!) {
  node(id: $item) {
    ... on ProjectV2Item {
      id
      isArchived
      project { id number owner { ... on Organization { login } } }
      fieldValues(first: 50) {
        nodes {
          ... on ProjectV2ItemFieldSingleSelectValue {
            name
            optionId
            field { ... on ProjectV2SingleSelectField { id name } }
          }
        }
      }
      content {
        __typename
        ... on Issue {
          number
          title
          body
          url
          state
          repository { nameWithOwner }
          author { login ... on User { databaseId } }
          labels(first: 20) { nodes { name } }
          comments(last: 20) { nodes { author { login } body createdAt } }
        }
      }
    }
  }
}
"""

FIELD_QUERY = """
query($project: ID!, $name: String!) {
  node(id: $project) {
    ... on ProjectV2 {
      field(name: $name) {
        ... on ProjectV2SingleSelectField { id name options { id name } }
      }
    }
  }
}
"""

SET_STATUS = """
mutation($project: ID!, $item: ID!, $field: ID!, $option: String!) {
  updateProjectV2ItemFieldValue(input: {projectId: $project, itemId: $item, fieldId: $field,
                                        value: {singleSelectOptionId: $option}}) {
    projectV2Item { id }
  }
}
"""


class GraphQL:
    """GitHub's GraphQL API with the control plane's token (classic: the `project` scope; fine-grained or an App:
    the organization's Projects read and write)."""

    def __init__(self, token: str, api_url: str, transport: httpx.AsyncBaseTransport | None = None):
        self._fast = transport is not None  # tests: don't sleep between retries
        self._client = httpx.AsyncClient(transport=transport, timeout=30, follow_redirects=False, headers={
            "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json", "User-Agent": "hangar"})
        self.url = graphql_url(api_url)

    async def close(self) -> None:
        await self._client.aclose()

    async def __call__(self, query: str, **variables: Any) -> dict[str, Any]:
        """The query's `data`. Transient failures (a connection error, 502, 503, 504) are retried twice."""
        for attempt in range(3):
            try:
                r = await self._client.post(self.url, json={"query": query, "variables": variables})
            except httpx.HTTPError as e:
                if attempt == 2:
                    raise GitHubError(f"GitHub GraphQL: {type(e).__name__}") from e
            else:
                if r.status_code not in (502, 503, 504) or attempt == 2:
                    break
            await asyncio.sleep(0 if self._fast else 2 ** attempt)
        if r.status_code >= 400:
            raise GitHubError(f"GitHub GraphQL: HTTP {r.status_code}")
        try:
            body = r.json()
        except ValueError as e:
            raise GitHubError("GitHub GraphQL: not JSON") from e
        if body.get("errors"):
            raise GitHubError(f"GitHub GraphQL: {str((body['errors'][0] or {}).get('message'))[:300]}")
        return body.get("data") or {}


def client_for(svc: JobService) -> GraphQL | None:
    name = svc.settings.github_token_credential
    if not name:
        return None
    try:
        token = svc.broker.secret_text(name)
    except Exception:  # noqa: BLE001
        log.warning("GitHub token credential %s is not usable", name)
        return None
    return GraphQL(token, svc.settings.github_api_url, transport=svc.transport)


_fields: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}  # (project, field name) -> (expires, field)


async def status_field(gql: GraphQL, project: str, name: str) -> dict[str, Any]:
    """The project's status field and its options, cached for a while (boards rarely change their columns)."""
    key, t = (project, name.casefold()), time.time()
    if (hit := _fields.get(key)) and hit[0] > t:
        return hit[1]
    data = await gql(FIELD_QUERY, project=project, name=name)
    field = ((data.get("node") or {}).get("field")) or {}
    if not field.get("id"):
        raise GitHubError(f"the project has no single-select field '{name}'")
    if len(_fields) > 500:
        _fields.clear()
    _fields[key] = (t + FIELD_CACHE_SECONDS, field)
    return field


# ============================================================================ write-back
def tracker_ref(project: str, item: str) -> str:
    return f"ghproject:{project}:{item}"


async def update(svc: JobService, job: Job, ref: str, stage: str) -> None:
    """The `ghproject:` tracker: move the item to the stage's status column."""
    _, project, item = (ref.split(":") + ["", ""])[:3]
    board = boards(svc.settings).get(job.labels.get("ghproject.board", ""))
    target = getattr(board.statuses, stage, None) if board else None
    if not target or not (_NODE.match(project) and _NODE.match(item)):
        return
    gql = client_for(svc)
    if gql is None:
        raise GitHubError("HANGAR_GITHUB_TOKEN_CREDENTIAL is not set or not stored")
    try:
        field = await status_field(gql, project, board.status_field)
        option = next((o for o in field.get("options") or [] if str(o.get("name")).casefold() == target.casefold()),
                      None)
        if option is None:
            _fields.pop((project, board.status_field.casefold()), None)  # a renamed column: look again next time
            raise GitHubError(f"the board has no '{target}' column in {board.status_field}")
        await gql(SET_STATUS, project=project, item=item, field=field["id"], option=option["id"])
        svc._event(job, f"project item moved to {option['name']}")
    finally:
        await gql.close()


intake.TRACKERS["ghproject"] = update


# ============================================================================ intake
def _status(item: dict[str, Any], field_name: str) -> str | None:
    for v in ((item.get("fieldValues") or {}).get("nodes")) or []:
        if isinstance(v, dict) and str(((v.get("field") or {}).get("name")) or "").casefold() == field_name.casefold():
            return v.get("name")
    return None


async def handle(svc: JobService, delivery: str | None, payload: dict[str, Any]) -> dict[str, Any]:
    """A verified `projects_v2_item` delivery (from handle_github)."""
    if payload.get("action") != "edited":
        return {"ignored": f"projects_v2_item {str(payload.get('action'))[:20]}"}
    try:
        configured = boards(svc.settings)
    except ProjectsConfigError as e:
        return {"error": str(e)}
    if not configured:
        return {"ignored": "no GitHub Projects board is configured (HANGAR_GITHUB_PROJECTS)"}
    item = payload.get("projects_v2_item") or {}
    if item.get("content_type") != "Issue":
        return {"ignored": "not an issue"}
    fv = (payload.get("changes") or {}).get("field_value") or {}
    if fv.get("field_type") not in (None, "single_select"):
        return {"ignored": "not a status change"}
    org = str((payload.get("organization") or {}).get("login") or "")
    board = configured.get(f"{org}/{fv.get('project_number')}".lower()) if fv.get("project_number") else None
    if fv.get("project_number") and board is None:
        return {"ignored": f"project {org}/{fv.get('project_number')} is not configured"}
    if board is not None:  # cheap checks first: most edits on a board are not a move into the trigger column
        if fv.get("field_name") and str(fv["field_name"]).casefold() != board.status_field.casefold():
            return {"ignored": "not the status field"}
        to = fv.get("to")
        if isinstance(to, dict) and to.get("name") and str(to["name"]).casefold() != board.trigger.casefold():
            return {"ignored": "not the trigger column"}
    node = str(item.get("node_id") or "")
    if not _NODE.match(node):
        return {"ignored": "no item id"}
    login = str((payload.get("sender") or {}).get("login") or "")
    sender_id = (payload.get("sender") or {}).get("id")  # --- review: people are linked by GitHub user id
    gql = client_for(svc)
    if gql is None:
        return {"error": "can't read the project item: HANGAR_GITHUB_TOKEN_CREDENTIAL is not set or not stored"}
    try:
        data = (await gql(ITEM_QUERY, item=node)).get("node") or {}
    except (GitHubError, httpx.HTTPError) as e:
        return {"error": f"can't read the project item: {e}"}
    finally:
        await gql.close()
    project = data.get("project") or {}
    key = f"{((project.get('owner') or {}).get('login')) or org}/{project.get('number')}".lower()
    board = configured.get(key)
    if board is None:
        return {"ignored": f"project {key} is not configured"}
    if data.get("isArchived"):
        return {"ignored": "the item is archived"}
    now = _status(data, board.status_field)
    if (now or "").casefold() != board.trigger.casefold():  # moved on since, or a delivery out of order
        return {"ignored": f"the item is in '{now}', not '{board.trigger}'"}
    issue = data.get("content") or {}
    repo = str(((issue.get("repository") or {}).get("nameWithOwner")) or "")
    number = issue.get("number")
    if issue.get("__typename") != "Issue" or not _REPO.match(repo) or not isinstance(number, int):
        return {"ignored": "not an issue"}
    if issue.get("state") == "CLOSED":
        return {"ignored": f"{repo}#{number} is closed"}
    if board.repos and repo.lower() not in {r.lower() for r in board.repos}:
        return {"ignored": f"{repo} is not one of this board's repos"}
    envs = svc.repo_envs(f"https://github.com/{repo}")
    if not envs:
        return {"ignored": f"repo '{repo}' is not in HANGAR_GITHUB_REPOS"}
    return await _submit(svc, delivery, board, key, project, data, issue, repo, number, login, sender_id)


async def _submit(svc: JobService, delivery: str | None, board: Board, key: str, project: dict[str, Any],
                  item: dict[str, Any], issue: dict[str, Any], repo: str, number: int, login: str,
                  sender_id: Any = None) -> dict[str, Any]:
    person = intake.person_for(svc.store, "github", sender_id)  # --- review: the id; a login can change hands
    # Moving an item needs write access to the project, not to the repo: a linked person, or someone on
    # HANGAR_GITHUB_TRUSTED, is trusted; anyone else's move waits for `/hangar approve` on the issue.
    trusted = person is not None or (svc.trust.configured and await svc.trust.trusted(login))
    author = str(((issue.get("author") or {}).get("login")) or "?")
    author_trusted = (author.lower() == login.lower() and trusted) or \
        intake.person_for(svc.store, "github", (issue.get("author") or {}).get("databaseId")) is not None or \
        (svc.trust.configured and await svc.trust.trusted(author))
    ref = f"github:{repo}#{number}"
    title = " ".join(str(issue.get("title") or "").split())[:300] or f"issue {number}"
    labels = ", ".join(str(n.get("name")) for n in ((issue.get("labels") or {}).get("nodes")) or [] if n)
    context = [ContextItem(name=f"issue-{number}.md", kind="issue", untrusted=not author_trusted, content=(
        f"# {title}\n\n{issue.get('body') or '(no description)'}\n\n---\n{issue.get('url', '')}\nOpened by {author}"
        + (f"; labels: {labels}" if labels else "")))]
    comments = [c for c in ((issue.get("comments") or {}).get("nodes")) or []
                if isinstance(c, dict) and "<!-- hangar -->" not in (c.get("body") or "")]
    if comments:  # whoever can comment on the issue wrote these: fenced as untrusted
        text = "\n\n".join(f"## {((c.get('author') or {}).get('login')) or '?'} ({c.get('createdAt', '')})\n\n"
                           f"{c.get('body') or ''}" for c in comments)
        context.append(ContextItem(name=f"issue-{number}-comments.md", kind="issue", untrusted=True,
                                   content=text[:50_000]))
    req = SubmitJob(
        instructions=(f"Resolve GitHub issue {repo}#{number}: {title}\n\nThe issue text and its discussion are in "
                      f"the context files `issue-{number}.md` and `issue-{number}-comments.md` (if present)."),
        title=f"#{number}: {title}"[:200], repo=f"https://github.com/{repo}.git", context=context,
        idempotency_key=f"ghproject-delivery:{delivery}" if delivery else None,
        labels={"github.kind": "issue", "github.issue": str(number), "ghproject.board": key,
                intake.TRACKER: tracker_ref(str(project.get("id") or ""), str(item.get("id") or "")),
                **autoinstall.kind_labels(svc.settings, [  # --- autoinstall: an env setup
                    str(n.get("name")) for n in ((issue.get("labels") or {}).get("nodes")) or [] if n])})
    requester = f"github:{login or 'unknown'}"
    res = intake.submit(svc, req, source="github-projects", ref=ref, requester=requester, person=person,
                        trusted=trusted, who=login or "someone", restricted=False)
    job = svc._jobs.get(res.get("job") or "")
    if res.get("error"):
        svc._spawn(svc._comment(None, ref, f"hangar couldn't start a job for this issue: {res['error']}"))
    elif res.get("ok") and job is not None:
        if job.state == JobState.AWAITING_APPROVAL and job.untrusted_source:
            from hangar.triggers import awaiting_comment

            note = awaiting_comment(svc, job, login or "someone")
        elif job.state == JobState.AWAITING_APPROVAL:
            from hangar.jobbudget import held_message

            note = held_message(job)
        else:
            note = (f"Queued as hangar job `{job.id}` in `{job.environment}` (moved to {board.trigger} on the "
                    "project board). I'll comment here when the agent starts and again with the result.")
        svc._spawn(svc._comment(job, ref, note))
    return res
