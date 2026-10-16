"""Taint tracking (#129): whether an agent turn has read content nobody hangar trusts wrote (docs/security.md#taint).

Untrusted *inputs* were already marked at submit time (`ContextItem.untrusted`, `Job.untrusted_source`, the prompt's
fence). What the agent fetches *while it runs* wasn't: an issue comment through the GitHub MCP server, a web page, a
search result. This module records both, from the turn's normalized events (the same taxonomy the trace export uses:
`traces.tool_kind`), as they stream in.

A turn is tainted when:

* **context**: the turn (or its job) starts with untrusted text: an untrusted context item (issue text, CI logs and
  review comments for an auto-fix round) or a task written by someone untrusted (`Job.untrusted_source`);
* **web**: it calls a tool of kind `web` (WebFetch, WebSearch, Codex's web_search, ...), or runs `curl`/`wget` on a
  host that isn't the sandbox itself;
* **mcp**: it calls a tool of an MCP server whose `trust` is `untrusted`. Remote (HTTP) servers default to
  untrusted, local stdio servers to trusted, the built-in browser is untrusted, and a server hangar doesn't know
  (one the agent added itself) is untrusted;
* **file**: it reads a file hangar wrote from an untrusted context item (already tainted by `context`; recorded so
  the review packet can say it was read).

Taint is sticky: once a session is tainted, every later turn starts tainted, a fork inherits it, and a job mirrors its
sessions. It is only ever set, never cleared. Nothing here gates anything: #130 does (push, PR, landing, egress).
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from hangar.events import RunEvent
    from hangar.models import ContextItem, Job, Session, SessionTurn

MAX_SOURCES = 20
_URL_HOST = re.compile(r"\bhttps?://(?:[^/\s@]*@)?(\[[^\]]+\]|[^/\s:?#'\"]+)", re.I)
_LOCAL = re.compile(r"^(localhost|127(\.\d+){3}|0\.0\.0\.0|\[::1\]|::1)$", re.I)
_FETCHERS = {"curl", "wget"}


# ====================================================================== records
def source(kind: str, ref: str, seq: int = 0, turn: int | None = None) -> dict[str, Any]:
    """One taint source. `seq`: the event (in the session's event log) that read it; 0 for what the turn started
    with."""
    out: dict[str, Any] = {"kind": kind, "seq": int(seq or 0), "ref": ref[:200]}
    if turn is not None:
        out["turn"] = turn
    return out


def tainted(x: Any) -> bool:
    """Whether a session, turn or job (or its `taint` dict) is tainted."""
    t = x if isinstance(x, dict) or x is None else getattr(x, "taint", None)
    return bool(t and t.get("tainted"))


def _add(d: dict[str, Any], src: dict[str, Any]) -> bool:
    srcs = d.setdefault("sources", [])
    if len(srcs) >= MAX_SOURCES or any(s.get("kind") == src["kind"] and s.get("ref") == src["ref"] for s in srcs):
        return False
    srcs.append(dict(src))
    return True


def mark(s: Session, turn: SessionTurn, turn_no: int, src: dict[str, Any]) -> bool:
    """Record `src` on the turn and the session. True when this made the session tainted (its first source)."""
    first = not tainted(s.taint)
    if first:
        s.taint = {"tainted": True, "first_turn": turn_no, "first_seq": src["seq"], "sources": []}
    _add(s.taint, {**src, "turn": turn_no})
    if not tainted(turn.taint):
        turn.taint = {"tainted": True, "first_seq": src["seq"], "sources": []}
    _add(turn.taint, src)
    return first


def inherit(child: Session, parent: Session) -> None:
    """A fork carries its parent's taint (and so does every best-of-N attempt of a tainted job)."""
    if tainted(parent.taint):
        child.taint = {**parent.taint, "sources": [dict(x) for x in parent.taint.get("sources") or []],
                       "inherited_from": parent.id}


def _label(c: ContextItem) -> str:
    what = {"issue": "issue text", "log": "log", "diff": "diff"}.get(c.kind, "attachment")
    return f"{what} {c.name}"


def context_sources(items: list[ContextItem]) -> list[dict[str, Any]]:
    return [source("context", _label(c)) for c in items if c.untrusted]


def job_sources(job: Job | None) -> list[dict[str, Any]]:
    """What a job starts with that taints it: its untrusted task text and context."""
    if job is None:
        return []
    out = [source("context", f"task text from {job.untrusted_source}")] if job.untrusted_source else []
    return out + context_sources(job.context)


def seed_job(job: Job) -> None:
    """At submit: a job whose own input is untrusted is tainted before any turn runs."""
    srcs = job_sources(job)
    if srcs and not tainted(job.taint):
        job.taint = {"tainted": True, "first_turn": 1, "first_seq": 0, "sources": []}
        for x in srcs:
            _add(job.taint, x)


# ====================================================================== at turn start
def begin(s: Session, turn: SessionTurn, turn_no: int, extra: list[dict[str, Any]] | None = None) -> str | None:
    """Before a turn runs: inherit the session's taint, then add what the turn (and its job) start with. Returns the
    ref of the first source when this made the session tainted (for the `taint:` event), else None."""
    if tainted(s.taint) and not tainted(turn.taint):
        turn.taint = {"tainted": True, "first_seq": 0, "inherited": True, "sources": []}
    first = None
    for src in [*(extra or []), *context_sources(turn.context)]:
        if mark(s, turn, turn_no, src) and first is None:
            first = src["ref"]
    return first


# ====================================================================== streaming
def _norm(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", name)


def mcp_trust(fleet: Any, s: Session) -> dict[str, str]:
    """MCP server name -> trusted | untrusted, as the session's sandbox is configured (envdoctor's built-ins, then
    the environment's `mcp:`). Keys are also given normalized the way Claude Code names tools."""
    servers: dict[str, Any] = {}
    try:
        from hangar import envdoctor

        spec = fleet.catalog.get(s.environment)
        servers = {**envdoctor.builtin_mcp(spec, fleet.settings, ""), **spec.mcp}
    except Exception:  # noqa: BLE001 - a deleted environment, a test double: every MCP server is untrusted
        pass
    out: dict[str, str] = {}
    for name, srv in servers.items():
        t = getattr(srv, "trust", None) or ("untrusted" if getattr(srv, "url", None) else "trusted")
        out[name] = out[_norm(name)] = t
    return out


def host_of(text: str) -> str | None:
    m = _URL_HOST.search(text or "")
    return m.group(1).lower()[:120] if m else None


def _fetch_hosts(command: str) -> list[str]:
    """Hosts a shell command fetches with curl or wget (local ones aside)."""
    from hangar.traces import _SEGMENT, _words

    out = []
    for seg in _SEGMENT.split(command or ""):
        words = _words(seg.strip().lstrip("(").rstrip(")"))
        while words and words[0].rsplit("/", 1)[-1] in ("sudo", "env", "timeout", "time", "nice"):
            words = words[1:]
        if not words or words[0].rsplit("/", 1)[-1] not in _FETCHERS:
            continue
        for w in words[1:]:
            h = host_of(w) if "://" in w else None
            if h is None and not w.startswith("-") and re.fullmatch(r"[A-Za-z0-9.-]+\.[A-Za-z]{2,}(/\S*)?", w):
                h = w.split("/", 1)[0].lower()
            if h and not _LOCAL.match(h) and h not in out:
                out.append(h)
    return out


def classify(ev: RunEvent, trust: dict[str, str], untrusted_files: set[str]) -> dict[str, Any] | None:
    """The taint source an event is, if any. One dict lookup for most events."""
    from hangar.traces import _mcp_parts, tool_kind

    if ev.kind == "command":
        hosts = _fetch_hosts(ev.text)
        return source("web", hosts[0], ev.seq) if hosts else None
    if ev.kind != "tool_call":
        return None
    name = str(ev.data.get("tool") or "")
    kind = tool_kind(name)
    if kind == "web":
        return source("web", host_of(ev.text) or name, ev.seq)
    if kind == "mcp":
        server, _ = _mcp_parts(name)
        server = server or name
        if trust.get(server, trust.get(_norm(server), "untrusted")) == "untrusted":
            return source("mcp", server, ev.seq)
        return None
    if kind == "read" and untrusted_files:
        path = ev.text[len(name) + 1:] if ev.text.startswith(name + " ") else ev.text
        if "/.hangar/" in path and path.rsplit("/", 1)[-1] in untrusted_files:
            return source("file", path.rsplit("/", 1)[-1], ev.seq)
    return None


# ====================================================================== jobs
def sync_job(job: Job, sessions: list[Session]) -> str | None:
    """`Job.taint` mirrors its sessions (the main one, best-of-N attempts, the judge). Returns the first source's ref
    when this made the job tainted."""
    first = None
    for s in sorted(sessions, key=lambda z: z.created_at):
        if not tainted(s.taint):
            continue
        if not tainted(job.taint):
            job.taint = {"tainted": True, "first_turn": s.taint.get("first_turn"),
                         "first_seq": s.taint.get("first_seq"), "sources": []}
            srcs = s.taint.get("sources") or []
            first = srcs[0]["ref"] if srcs else "an earlier session"
        for x in s.taint.get("sources") or []:
            _add(job.taint, x)
    return first


def describe(src: dict[str, Any]) -> str:
    """One source as the review packet says it: "issue text jira-ABC-12.md (context)", "web: example.com (seq 41)"."""
    kind, ref, seq = src.get("kind"), src.get("ref"), src.get("seq") or 0
    if kind == "context":
        return f"{ref} (context)"
    return f"{kind}: {ref}" + (f" (seq {seq})" if seq else "")


def packet_line(job: Job) -> str | None:
    """--- landing: the review packet's line for a tainted job."""
    if not tainted(job.taint):
        return None
    srcs = job.taint.get("sources") or []
    shown = ", ".join(describe(x) for x in srcs[:8]) + (f" and {len(srcs) - 8} more" if len(srcs) > 8 else "")
    return f"- **Untrusted content read:** {shown or 'yes'}"
