"""Replaying a recorded run through the model gateway (#179; docs/model-gateway.md#replay).

A tape (#177) holds what the model answered at every step of a run. Replaying it re-executes the run in a fresh
sandbox: the harness gets the taped answers instead of the provider's, runs every tool call for real, and each request
it sends (which carries those tool calls' outputs) is compared with the taped request of that step. The first request
that doesn't match is the **divergence**; a clean prefix means the sandbox's state at step N has been rebuilt by
re-execution, which is what a fork at a step (forks.py) and the nightly determinism report (determinism.py) need.

**Modes.** A replay is a plan stored in `<data>/replays/<id>.json`, bound to the sandbox whose labels carry its id
(`hangar.replay`) when the gateway attaches to it:

* `replay-strict`: every request must be one of the tape's; a miss is an error the harness can't retry (a
  provider-shaped 400), and a request past the tape's end is a miss. With `tolerate: tool` (the determinism report)
  a request that differs from the tape's only in tool outputs is recorded as a mismatch and the step is served anyway,
  so one replay finds every step whose output doesn't reproduce.
* `replay-prefix`: the tape's answers for steps 1..`at_seq`, while each request matches; the request after step
  `at_seq` is checked too (it carries step `at_seq`'s tool outputs), then forwarded to the provider: the run goes
  live from N. A divergence before that stops the replay (`on_divergence: stop`, the harness gets a 400) or goes live
  there (`live`).

**Matching.** Requests are compared as the tape stores them (redacted, normalized with the run's placeholders) and
then *loosened* on both sides: dates (`2026-10-18`), durations (`0.53s`, `120ms`), UUIDs and hex addresses are
replaced, and the request's `metadata` (Claude Code's per-session user id) is dropped. Requests are matched in order,
within a window of the next 8 unserved steps, because a harness's side calls (titles, subagents) interleave.

**Ids.** Taped answers carry placeholders (`<toolu:3>`). Served, each becomes a fresh concrete id (`toolu_hgr…`),
registered so the harness's next request maps it back to the same placeholder: a replayed request normalizes exactly
as the original did. `<ts>`, `<tmp>` and redacted secrets can't be restored and are served as written: a step whose
command used one diverges, as it should.

**Divergence kinds.** `model`: the requests differ outside tool outputs (the harness, its system prompt or its tools
changed: cross-harness replay diverges at step 1 by design). `tool`: they differ only in a tool's output. `network`:
a tool output differs and the replay's shows a network error (replays run with egress `none`, so a step that fetched
something diverges here instead of reaching out). `tree`: the working tree after an edit or a shell command isn't the
one the original recorded (#165), found by `tree_divergence` from both runs' `tree` events.

**Side effects.** Tools with effects outside the sandbox (MCP tools, Jira, Slack, web fetch and search) are served
from the tape, never re-executed: a replay sandbox has none of the environment's MCP servers and no credentials, and
the gateway puts the taped output in place of whatever the harness got before comparing (and, once live, before
forwarding upstream).

**Isolation.** A replay sandbox (`isolate`, fleet.py) has egress `none` (the posture floor), holds no credential (the
harness's API key stays in the control plane, behind the gateway), no `secrets:`, no cloud token, no MCP server, no
GitHub API and no build caches, and fetches git through the gateway's fetch-only grant.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import json
import logging
import re
import secrets as _secrets
from pathlib import Path
from typing import TYPE_CHECKING, Any

from hangar.models import now

if TYPE_CHECKING:
    from starlette.requests import Request
    from starlette.responses import Response

    from hangar.fleet import Fleet
    from hangar.models import ModelGrant, Sandbox

log = logging.getLogger("hangar.replay")

REPLAY_LABEL = "hangar.replay"  # on a replay's job, session and sandbox: the replay's id
FORK_OF_LABEL = "hangar.fork_of"  # on a fork's trial jobs: the run forked (`{session}.{run}`)
SCHEMA = "hangar.replay/v1"
MODES = ("replay-strict", "replay-prefix")
ID = re.compile(r"^rpl-[0-9a-f]{12}$")
WINDOW = 8
EXCERPT = 1500  # of a mismatched tool output (redacted and normalized as the tape is), for the determinism report
API_KINDS = ("toolu", "srvtoolu", "call", "fc", "msg", "resp", "rs", "ws", "ctc")
TOOL_KINDS = ("toolu", "srvtoolu", "call", "fc")
_PH = re.compile(r"<(toolu|srvtoolu|call|fc|msg|resp|rs|ws|ctc|sb|ses|run|job|snap|mgt|gnt):(\d+)>")
SIDE_EFFECT = re.compile(r"^(mcp__.+|WebFetch|WebSearch|web_fetch|web_search)$")
NETWORK = re.compile(
    r"Could not resolve host|Temporary failure in name resolution|Name or service not known|getaddrinfo|ENOTFOUND|"
    r"EAI_AGAIN|Network is unreachable|Connection refused|ECONNREFUSED|Failed to connect|Connection timed out|"
    r"ETIMEDOUT|No route to host|nodename nor servname|CONNECT tunnel failed|Could not connect|"
    r"Failed to establish a new connection|Max retries exceeded", re.I)
_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_DUR = re.compile(r"\b\d+(?:\.\d+)?\s?(?:ms|s|sec|secs|seconds)\b")
_UUID = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")
_ADDR = re.compile(r"\b0x[0-9a-fA-F]{6,}\b")


class ReplayError(ValueError):
    """A replay can't be planned as asked (a 4xx at the front door)."""


# ====================================================================== comparison
def loose(obj: Any) -> Any:
    """A normalized request with what legitimately varies between two runs of the same steps taken out."""
    if isinstance(obj, str):
        s = _UUID.sub("<uuid>", obj)
        s = _DATE.sub("<date>", s)
        s = _DUR.sub("<dur>", s)
        return _ADDR.sub("<addr>", s)
    if isinstance(obj, dict):
        return {k: loose(v) for k, v in obj.items() if k != "metadata"}
    if isinstance(obj, list):
        return [loose(v) for v in obj]
    return obj


def _turns(req: dict[str, Any], turns: str) -> list[Any]:
    body = req.get("body")
    v = body.get(turns) if isinstance(body, dict) else None
    return v if isinstance(v, list) else []


def results_of(req: dict[str, Any], turns: str) -> dict[str, dict[str, Any]]:
    """{tool call id: its result block} in a (normalized) request: Anthropic's `tool_result` blocks, OpenAI's
    `function_call_output` items."""
    out: dict[str, dict[str, Any]] = {}
    for item in _turns(req, turns):
        if not isinstance(item, dict):
            continue
        if item.get("type") == "function_call_output" and isinstance(item.get("call_id"), str):
            out[item["call_id"]] = item
        content = item.get("content")
        if isinstance(content, list):
            for b in content:
                if isinstance(b, dict) and b.get("type") == "tool_result" and isinstance(b.get("tool_use_id"), str):
                    out[b["tool_use_id"]] = b
    return out


def calls_of(req: dict[str, Any], turns: str) -> dict[str, dict[str, Any]]:
    """{tool call id: {name, input}} of the tool calls a request's conversation holds."""
    out: dict[str, dict[str, Any]] = {}
    for item in _turns(req, turns):
        if not isinstance(item, dict):
            continue
        if item.get("type") == "function_call" and isinstance(item.get("call_id"), str):
            args: Any = item.get("arguments")
            with contextlib.suppress(ValueError, TypeError):
                args = json.loads(args) if isinstance(args, str) else args
            out[item["call_id"]] = {"name": item.get("name"), "input": args}
        content = item.get("content")
        if isinstance(content, list):
            for b in content:
                if isinstance(b, dict) and b.get("type") == "tool_use" and isinstance(b.get("id"), str):
                    out[b["id"]] = {"name": b.get("name"), "input": b.get("input")}
    return out


def _map_results(req: dict[str, Any], turns: str, fn: Any) -> dict[str, Any]:
    """A copy of `req` with each tool result block replaced by fn(call id, block)."""
    req = copy.deepcopy(req)
    for item in _turns(req, turns):
        if not isinstance(item, dict):
            continue
        content = item.get("content")
        if isinstance(content, list):
            for i, b in enumerate(content):
                if isinstance(b, dict) and b.get("type") == "tool_result" and isinstance(b.get("tool_use_id"), str):
                    content[i] = fn(b["tool_use_id"], b)
    body = req.get("body")
    if isinstance(body, dict) and isinstance(body.get(turns), list):
        body[turns] = [fn(x["call_id"], x) if isinstance(x, dict) and x.get("type") == "function_call_output"
                       and isinstance(x.get("call_id"), str) else x for x in body[turns]]
    return req


def substitute(req: dict[str, Any], turns: str, subs: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """The taped result in place of the live one for each call in `subs` (side effects, tolerated mismatches)."""
    if not subs:
        return req
    return _map_results(req, turns, lambda cid, b: subs.get(cid, b))


def _blank(cid: str, b: dict[str, Any]) -> dict[str, Any]:
    b = dict(b)
    for k in ("content", "output", "is_error"):
        b.pop(k, None)
    return b


def _text(block: Any) -> str:
    if isinstance(block, str):
        return block
    if isinstance(block, dict):
        return " ".join(_text(v) for k, v in block.items() if k in ("content", "output", "text"))
    if isinstance(block, list):
        return " ".join(_text(v) for v in block)
    return ""


def where(call: dict[str, Any] | None) -> dict[str, Any]:
    """A tool call as the report names it: the tool, and its command or its file."""
    if not call:
        return {}
    inp = call.get("input") if isinstance(call.get("input"), dict) else {}
    out: dict[str, Any] = {"tool": call.get("name")}
    for k in ("command", "cmd"):
        if isinstance(inp.get(k), str | list):
            out["command"] = inp[k] if isinstance(inp[k], str) else " ".join(map(str, inp[k]))
            break
    for k in ("file_path", "path", "notebook_path"):
        if isinstance(inp.get(k), str):
            out["file"] = inp[k]
            break
    return out


def classify(live: dict[str, Any], expected: dict[str, Any], turns: str,
             calls: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Why `live` isn't `expected`: {kind: model|tool|network, call?, tool?, command?, file?}."""
    if loose(_map_results(live, turns, _blank)) != loose(_map_results(expected, turns, _blank)):
        return {"kind": "model", "detail": _first_difference(loose(live), loose(expected), turns)}
    got, want = results_of(live, turns), results_of(expected, turns)
    for cid, b in got.items():
        w = want.get(cid)
        if w is not None and loose(b) != loose(w):
            kind = "network" if NETWORK.search(_text(b)) and not NETWORK.search(_text(w)) else "tool"
            return {"kind": kind, "call": cid, **where(calls.get(cid)), "got": _text(b)[:EXCERPT],
                    "want": _text(w)[:EXCERPT]}
    return {"kind": "model", "detail": "the requests differ"}


def _first_difference(a: dict[str, Any], b: dict[str, Any], turns: str) -> str:
    ab, bb = a.get("body") or {}, b.get("body") or {}
    if not isinstance(ab, dict) or not isinstance(bb, dict):
        return "the request body"
    for k in sorted(set(ab) | set(bb)):
        if k == turns:
            continue
        if ab.get(k) != bb.get(k):
            return f"`{k}`"
    at, bt = ab.get(turns) or [], bb.get(turns) or []
    for i, (x, y) in enumerate(zip(at, bt, strict=False)):
        if x != y:
            return f"`{turns}[{i}]`"
    if len(at) != len(bt):
        return f"`{turns}` has {len(at)} items, the tape's {len(bt)}"
    for k in ("path", "method", "headers"):
        if a.get(k) != b.get(k):
            return f"the request's {k}"
    return "the request"


def prompt_of(req: dict[str, Any], provider: str) -> str | None:
    """The prompt a harness was started with, from its run's first request: the last text the user turn carries
    that isn't one of the harness's own reminders (Claude Code's `<system-reminder>`, Codex's environment)."""
    turns = "input" if provider == "openai" else "messages"
    items = _turns(req, turns)
    first = next((x for x in items if isinstance(x, dict) and x.get("role") == "user"), None)
    if first is None:
        return None
    content = first.get("content")
    if isinstance(content, str):
        return content
    texts = [b.get("text") for b in content or [] if isinstance(b, dict) and b.get("type") in ("text", "input_text")
             and isinstance(b.get("text"), str)]
    own = ("<system-reminder>", "<environment_context>", "<user_instructions>", "<command-")
    texts = [t for t in texts if not t.lstrip().startswith(own)]
    return texts[-1] if texts else None


def emitted(entries: list[dict[str, Any]]) -> dict[str, int]:
    """{tool call placeholder: the step whose answer made the call}."""
    out: dict[str, int] = {}
    for e in entries:
        resp = e.get("resp") or {}
        blob = json.dumps(resp.get("sse") if "sse" in resp else resp.get("body"))
        for m in re.finditer(r'"(?:id|call_id)": "(<(?:toolu|srvtoolu|call|fc):\d+>)"', blob):
            out.setdefault(m.group(1), e["seq"])
    return out


# ====================================================================== plans
def root(settings: Any) -> Path:
    return Path(settings.data_dir) / "replays"


def tape_dir(settings: Any, run: str) -> Path:
    return Path(settings.data_dir) / "tapes" / run / "tape"


def _path(settings: Any, rid: str) -> Path:
    if not ID.match(rid or ""):
        raise KeyError(f"no replay '{rid}'")
    return root(settings) / f"{rid}.json"


def load_plan(settings: Any, rid: str) -> dict[str, Any]:
    p = _path(settings, rid)
    try:
        return json.loads(p.read_text())
    except FileNotFoundError as e:
        raise KeyError(f"no replay '{rid}'") from e


def save_plan(settings: Any, plan: dict[str, Any]) -> None:
    plan["updated_at"] = now()
    d = root(settings)
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / f".{plan['id']}.tmp"
    tmp.write_text(json.dumps(plan, indent=1, sort_keys=True))
    tmp.replace(d / f"{plan['id']}.json")


def read_tape(settings: Any, run: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """A finished run's validated tape: (index, entries). ReplayError when there is none or it doesn't verify."""
    from hangar import modelgw

    d = tape_dir(settings, run)
    if not (d / "index.json").exists():
        raise ReplayError(f"run {run} has no tape: it didn't run with `model_gateway: record`, or its tape is older "
                          f"than HANGAR_MODEL_TAPE_DAYS ({settings.model_tape_days} days)")
    try:
        index = modelgw.validate(d)
    except (ValueError, KeyError, OSError) as e:
        raise ReplayError(f"run {run}'s tape doesn't verify: {e}") from e
    if not index.get("final"):
        raise ReplayError(f"run {run} is still being recorded: fork it once its turn has ended")
    entries = [json.loads(p.read_text()) for p in sorted(d.glob("[0-9]*.json"))]
    return index, entries


def plan(fleet: Fleet, run: str, *, mode: str, at_seq: int | None = None, on_divergence: str = "stop",
         tolerate: str | None = None, purpose: str = "fork", parent: str | None = None, trial: str | None = None,
         actor: str = "system") -> dict[str, Any]:
    """A replay of `run`'s tape, waiting for the sandbox that will carry its id (REPLAY_LABEL)."""
    if mode not in MODES:
        raise ReplayError(f"mode must be one of {', '.join(MODES)}")
    index, entries = read_tape(fleet.settings, run)
    n = len(entries)
    at = n if at_seq is None else at_seq
    if not 0 <= at <= n:
        raise ReplayError(f"at_seq {at} is outside run {run}'s tape (steps 1..{n})")
    first = _requests(index, entries).get(1)
    p = {
        "schema": SCHEMA, "id": "rpl-" + _secrets.token_hex(6), "run": run, "mode": mode, "at_seq": at,
        "entries": n, "provider": index.get("provider"), "model": index.get("model"),
        "harness": index.get("harness"), "harness_digest": index.get("harness_digest"),
        "on_divergence": on_divergence if on_divergence in ("stop", "live") else "stop",
        "tolerate": tolerate if tolerate == "tool" else None, "purpose": purpose, "parent": parent, "trial": trial,
        "tainted": bool(index.get("untrusted")), "untrusted_reasons": index.get("untrusted_reasons") or [],
        "prompt": prompt_of(first, index.get("provider") or "anthropic") if first else None,
        "status": "pending", "sandbox_id": None, "served": [], "verified_through": 0, "divergence": None,
        "mismatches": [], "live_since": None, "ids": {}, "created_by": actor, "created_at": now(),
    }
    save_plan(fleet.settings, p)
    return p


def _requests(index: dict[str, Any], entries: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    from hangar import modelgw

    turns = modelgw.PROVIDERS.get(index.get("provider") or "", modelgw.PROVIDERS["anthropic"]).turns
    by_seq: dict[int, dict[str, Any]] = {}
    for e in entries:
        by_seq[e["seq"]] = modelgw.apply_delta(by_seq, e["req_delta"], turns)
    return by_seq


def labelled(sb: Sandbox) -> bool:
    return bool(sb.labels.get(REPLAY_LABEL))


def labelled_id(fleet: Fleet, sandbox_id: str) -> bool:
    """Whether the sandbox is a replay's (in memory, else its labels)."""
    from hangar.models import Sandbox

    if (r := fleet.__dict__.get("_replays")) is not None and sandbox_id in r.by_sandbox:
        return True
    sb = fleet.store.get(Sandbox, sandbox_id)
    return sb is not None and labelled(sb)


def isolate(fleet: Fleet, sb: Sandbox, harness: str | None, cred_names: list[str]) -> list[str]:
    """--- fleet.create: a replay sandbox's egress floor is `none` and the only credential it is created with is the
    harness's API key, which the model gateway takes out again (modelgw.attach): it ends up holding none."""
    from hangar import posture
    from hangar.models import Credential, CredentialKind

    if not labelled(sb):
        return cred_names
    sb.labels[posture.FLOOR_LABEL] = "none"
    keep = []
    for name in cred_names:
        c = fleet.store.get(Credential, name)
        if c is not None and c.harness == harness and c.kind == CredentialKind.API_KEY:
            keep = [name]
            break
    return keep


# ====================================================================== the engine
class Replay:
    """A plan bound to its sandbox: the tape in memory, the steps served, the id map."""

    def __init__(self, fleet: Fleet, p: dict[str, Any]):
        from hangar import modelgw

        self.fleet, self.p = fleet, p
        self.index, self.entries = read_tape(fleet.settings, p["run"])
        self.info = modelgw.PROVIDERS.get(p.get("provider") or "", modelgw.PROVIDERS["anthropic"])
        self.turns = self.info.turns
        self.reqs = _requests(self.index, self.entries)
        self.by_seq = {e["seq"]: e for e in self.entries}
        self.calls: dict[str, dict[str, Any]] = {}
        self.taped_results: dict[str, dict[str, Any]] = {}
        for seq in sorted(self.reqs):
            self.calls.update(calls_of(self.reqs[seq], self.turns))
            for cid, b in results_of(self.reqs[seq], self.turns).items():
                self.taped_results.setdefault(cid, b)
        self.side = {cid for cid, c in self.calls.items() if SIDE_EFFECT.match(str(c.get("name") or ""))}
        self.subs = {cid: self.taped_results[cid] for cid in self.side if cid in self.taped_results}
        for m in p.get("mismatches") or []:  # tolerated ones stay substituted after a restart
            if m.get("call") in self.taped_results:
                self.subs[m["call"]] = self.taped_results[m["call"]]
        self.norm = modelgw.Normalizer(dict(p.get("ids") or {}))
        self.loose_hash = {seq: modelgw.digest(loose(r)) for seq, r in self.reqs.items()}
        self.served: set[int] = set(p.get("served") or [])
        self.lock = asyncio.Lock()

    # ------------------------------------------------------------------ state
    @property
    def id(self) -> str:
        return self.p["id"]

    @property
    def live(self) -> bool:
        return self.p.get("status") == "live"

    @property
    def stopped(self) -> bool:
        return self.p.get("status") in ("diverged", "done", "stopped")

    def save(self) -> None:
        self.p["served"] = sorted(self.served)
        self.p["ids"] = self.norm.ids
        save_plan(self.fleet.settings, self.p)

    def prefix_done(self) -> bool:
        return all(s in self.served for s in range(1, self.p["at_seq"] + 1))

    # ------------------------------------------------------------------ one request
    def normalize(self, method: str, path: str, headers: dict[str, str], body: bytes) -> dict[str, Any]:
        from hangar import modelgw, outbox

        try:
            parsed: Any = json.loads(body) if body else None
        except ValueError:
            parsed = body.decode(errors="replace")
        hdrs = {k.lower(): v for k, v in headers.items() if k.lower() in modelgw._KEEP_REQ}
        return self.norm(outbox.of(self.fleet)._scrub({"method": method, "path": path, "headers": hdrs,
                                                       "body": parsed}))

    def match(self, live: dict[str, Any]) -> tuple[int | None, int | None]:
        """(the unserved step this request is, None) or (None, the step it should have been)."""
        from hangar import modelgw

        cand = [s for s in range(1, len(self.entries) + 1) if s not in self.served][:WINDOW]
        if not cand:
            return None, None
        h = modelgw.digest(loose(substitute(live, self.turns, self.subs)))
        for s in cand:
            if self.loose_hash[s] == h:
                return s, None
        mine = _turns(live, self.turns)

        def shared(s: int) -> int:
            theirs = _turns(self.reqs[s], self.turns)
            n = 0
            for a, b in zip(mine, theirs, strict=False):
                if loose(a) != loose(b):
                    break
                n += 1
            return n

        return None, max(cand, key=lambda s: (shared(s), -s))

    def decide(self, live: dict[str, Any]) -> dict[str, Any]:
        """What to do with a request: {"serve": seq} | {"forward": True} | {"error": message}. Updates the plan."""
        if self.live:
            return {"forward": True}
        if self.stopped:
            why = (self.p.get("divergence") or {}).get("message") or "the replay has ended"
            return {"error": why}
        verifying = self.prefix_done()  # the request after step at_seq: checked, then forwarded (prefix mode)
        seq, expected = self.match(live)
        if seq is not None:
            if verifying and self.p["mode"] == "replay-prefix":
                self.p["verified_through"] = max(self.p["verified_through"], seq)
                return self._go_live(seq)
            self.served.add(seq)
            self.p["verified_through"] = max(self.p["verified_through"], seq)
            self.p["status"] = "replaying"
            return {"serve": seq}
        if expected is None:  # nothing left on the tape
            if self.p["mode"] == "replay-prefix":
                return self._go_live(len(self.entries) + 1)
            return self._diverge({"seq": len(self.entries) + 1, "kind": "model",
                                  "detail": "the harness asked past the end of the tape"})
        why = classify(substitute(live, self.turns, self.subs), self.reqs[expected], self.turns, self.calls)
        why["seq"] = expected
        if self.p["mode"] == "replay-strict" and self.p.get("tolerate") == "tool" and why["kind"] in ("tool",
                                                                                                       "network"):
            self.p["mismatches"].append(why)
            if why.get("call") in self.taped_results:
                self.subs[why["call"]] = self.taped_results[why["call"]]
            self.served.add(expected)
            self.p["status"] = "replaying"
            return {"serve": expected}
        return self._diverge(why)

    def _go_live(self, seq: int) -> dict[str, Any]:
        self.p["status"], self.p["live_since"] = "live", seq
        return {"forward": True}

    def _diverge(self, why: dict[str, Any]) -> dict[str, Any]:
        what = why.get("command") or why.get("file") or why.get("tool") or why.get("detail") or ""
        why["message"] = f"hangar: the replay of {self.p['run']} diverged at step {why['seq']} ({why['kind']}" + \
            (f": {str(what)[:200]}" if what else "") + ")"
        if self.p.get("divergence") is None:
            self.p["divergence"] = {k: v for k, v in why.items() if k != "message"} | {"message": why["message"]}
        if self.p["mode"] == "replay-prefix" and self.p.get("on_divergence") == "live":
            return self._go_live(why["seq"])
        self.p["status"] = "diverged"
        return {"error": why["message"]}

    # ------------------------------------------------------------------ serving a taped answer
    def _concrete(self, kind: str, n: str) -> str | None:
        ph = f"<{kind}:{n}>"
        rev = next((raw for raw, v in self.norm.ids.items() if v == ph), None)
        if rev is not None:
            return rev
        if kind not in API_KINDS:
            return None
        raw = f"{kind}_hgr{self.id[4:]}{int(n):05d}"
        self.norm.ids[raw] = ph
        return raw

    def concrete(self, obj: Any) -> Any:
        if isinstance(obj, str):
            return _PH.sub(lambda m: self._concrete(m.group(1), m.group(2)) or m.group(0), obj)
        if isinstance(obj, dict):
            return {k: self.concrete(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self.concrete(v) for v in obj]
        return obj

    def answer(self, seq: int) -> tuple[int, dict[str, str], bytes]:
        resp = self.by_seq[seq]["resp"]
        status = int(resp.get("status") or 200)
        headers = {k: v for k, v in (resp.get("headers") or {}).items() if k == "content-type"}
        if "sse" in resp:
            out = bytearray()
            for ev in self.concrete(resp["sse"]):
                if ev.get("event"):
                    out += f"event: {ev['event']}\n".encode()
                data = ev.get("data")
                out += b"data: " + (json.dumps(data, ensure_ascii=False) if not isinstance(data, str)
                                    else data).encode() + b"\n\n"
            headers.setdefault("content-type", "text/event-stream")
            return status, headers, bytes(out)
        body = self.concrete(resp.get("body"))
        headers.setdefault("content-type", "application/json")
        return status, headers, (json.dumps(body, ensure_ascii=False) if not isinstance(body, str) else body).encode()

    def rewrite(self, body: bytes) -> bytes:
        """--- live: the request forwarded upstream with side effects' taped outputs in place of the replay's."""
        if not self.side or not body:
            return body
        try:
            parsed = json.loads(body)
        except ValueError:
            return body
        sides = {raw for raw, ph in self.norm.ids.items() if ph in self.subs}
        if not sides:
            return body
        wrapped = {"body": parsed}

        def put(cid: str, b: dict[str, Any]) -> dict[str, Any]:
            if cid not in sides:
                return b
            taped = self.concrete(self.subs[self.norm.ids[cid]])
            return {**b, **{k: v for k, v in taped.items() if k in ("content", "output", "is_error")}}

        return json.dumps(_map_results(wrapped, self.turns, put)["body"], ensure_ascii=False).encode()


def _error(provider: str, message: str) -> Response:
    from starlette.responses import JSONResponse

    if provider == "openai":
        return JSONResponse({"error": {"message": message, "type": "invalid_request_error",
                                       "code": "hangar_replay"}}, 400)
    return JSONResponse({"type": "error", "error": {"type": "invalid_request_error", "message": message}}, 400)


class Replays:
    """The fleet's replays in memory, by sandbox."""

    def __init__(self, fleet: Fleet):
        self.fleet = fleet
        self.by_sandbox: dict[str, Replay] = {}

    def bind(self, sb: Sandbox) -> dict[str, Any] | None:
        """--- modelgw.attach: the sandbox carrying a replay's id takes it over."""
        rid = sb.labels.get(REPLAY_LABEL)
        if not rid:
            return None
        try:
            p = load_plan(self.fleet.settings, rid)
        except KeyError as e:
            raise ReplayError(f"sandbox {sb.id} names replay {rid}, which doesn't exist") from e
        if p.get("sandbox_id") not in (None, sb.id):
            raise ReplayError(f"replay {rid} is bound to sandbox {p['sandbox_id']} already")
        p["sandbox_id"], p["status"] = sb.id, "replaying" if p["status"] == "pending" else p["status"]
        save_plan(self.fleet.settings, p)
        self.by_sandbox[sb.id] = Replay(self.fleet, p)
        return p

    def get(self, sandbox_id: str) -> Replay | None:
        r = self.by_sandbox.get(sandbox_id)
        if r is not None:
            return r
        from hangar.models import Sandbox

        sb = self.fleet.store.get(Sandbox, sandbox_id)
        rid = sb.labels.get(REPLAY_LABEL) if sb is not None else None
        if not rid:
            return None
        try:  # after a restart: the plan on disk carries what was served and the id map
            p = load_plan(self.fleet.settings, rid)
            if p.get("sandbox_id") != sandbox_id:
                return None
            r = self.by_sandbox[sandbox_id] = Replay(self.fleet, p)
            return r
        except (KeyError, ReplayError):
            log.warning("sandbox %s: replay %s can't be loaded", sandbox_id, rid)
            return None

    def forget(self, sandbox_id: str) -> None:
        self.by_sandbox.pop(sandbox_id, None)

    async def handle(self, gw: Any, request: Request, grant: ModelGrant, provider: str, body: bytes) -> Response:
        """A replay sandbox's model call: a taped answer, an error, or the call forwarded (live)."""
        from starlette.responses import Response

        from hangar import modelgw

        r = self.get(grant.sandbox_id)
        rest = request.path_params.get("rest", "")
        recorded = request.method == "POST" and rest.rstrip("/").endswith(modelgw.RECORDED_PATHS)
        if r is None or not recorded:
            return await gw.forward(request, grant, provider, body)
        async with r.lock:
            live = r.normalize(request.method, "/" + rest, dict(request.headers), body)
            act = r.decide(live)
            if "serve" in act:
                status, headers, raw = r.answer(act["serve"])
            r.save()
        if "error" in act:
            self.fleet.audit("system", "replay.diverged", grant.sandbox_id, replay=r.id,
                             divergence=r.p.get("divergence"))
            return _error(provider, act["error"])
        if "forward" in act:
            return await gw.forward(request, grant, provider, r.rewrite(body))
        return Response(raw, status_code=status, headers=headers)


def of(fleet: Fleet) -> Replays:
    r = fleet.__dict__.get("_replays")
    if r is None:
        r = fleet.__dict__["_replays"] = Replays(fleet)
    return r


def prompt_override(fleet: Fleet, job: Any) -> str | None:
    """--- jobs._prompt: a replay's first turn is started with the original run's prompt, as its tape recorded it,
    so its first request is the original's."""
    rid = job.labels.get(REPLAY_LABEL)
    if not rid or len(job.turns) > 1:
        return None
    try:
        return load_plan(fleet.settings, rid).get("prompt")
    except KeyError:
        return None


# ====================================================================== trees (#165)
def _tree_lines(events: list[Any]) -> tuple[str | None, dict[str, str]]:
    """(the start tree, {tool call id: the tree after it}) from a run's `tree` progress events."""
    start, after = None, {}
    for ev in events:
        d = getattr(ev, "data", None) or {}
        if getattr(ev, "kind", None) != "progress" or d.get("action") != "tree" or not d.get("tree"):
            continue
        if d.get("phase") == "start":
            start = start or d["tree"]
        elif d.get("id"):
            after[str(d["id"])] = d["tree"]
    return start, after


def tree_divergence(original: list[Any], replayed: list[Any], original_ids: dict[str, str],
                    replay_ids: dict[str, str], entries: list[dict[str, Any]], upto: int) -> dict[str, Any] | None:
    """The first step at or before `upto` whose working tree differs between the two runs: their start trees, then the
    tree after each edit or command whose call a step up to `upto - 1` made (its result is in step `upto`'s request).
    Either side having a line the other lacks is a difference too: the other's tree didn't change there."""
    o_start, o_after = _tree_lines(original)
    r_start, r_after = _tree_lines(replayed)
    if o_start and r_start and o_start != r_start:
        return {"seq": 1, "kind": "tree", "expected": o_start, "got": r_start, "detail": "the starting tree"}
    o = {original_ids[k]: v for k, v in o_after.items() if k in original_ids}
    r = {replay_ids[k]: v for k, v in r_after.items() if k in replay_ids}
    made = emitted(entries)
    for ph in sorted(set(o) | set(r), key=lambda p: (made.get(p, 10**9), p)):
        seq = made.get(ph)
        if seq is None or seq + 1 > upto:
            continue
        if o.get(ph) != r.get(ph):
            return {"seq": seq + 1, "kind": "tree", "call": ph, "expected": o.get(ph), "got": r.get(ph)}
    return None
