"""Activity events, usage and budgets (spec 0011 workstream C; 0007 M3 budgets and M6 parsers, 0003 M9 usage).

Every harness writes a JSON stream to its run log (`~/.hangar/runs/<run>.log`): Claude Code `stream-json`, Codex
`exec --json`, pi `--mode json` and Antigravity `stream-json`. While a turn runs, the session's poll loop reads the
log's new bytes by offset (never the whole log), parses them into one `RunEvent` shape, appends the events to a
per-session JSONL under the data dir, keeps the turn's usage (tokens and USD) current, and stops the turn when it
crosses its budget. The read position and parser state are saved on the turn, so a control-plane restart resumes
where it left off without duplicating events or usage.

Other workstreams add events with `Activity.append` (E feeds `progress` from `$HANGAR_IO/out/progress.jsonl`) and
read them with `Activity.read` / `SessionService.events`.
"""

from __future__ import annotations

import asyncio  # --- stream
import base64
import collections
import contextlib
import json
import logging
import os
import re
import shlex
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from hangar import isolation  # --- harden
from hangar.models import Budget, Session, SessionTurn, Usage, now

log = logging.getLogger("hangar.events")

Kind = Literal["message", "tool_call", "tool_result", "command", "file_change", "error", "usage", "progress",
               "approval_request"]
KINDS: tuple[str, ...] = ("message", "tool_call", "tool_result", "command", "file_change", "error", "usage", "progress",
                          "approval_request")  # workstream steer: a tool approval asked for, or how it was answered

MAX_LINE = 1024 * 1024  # longer log lines (huge tool outputs) are skipped, and counted
CHUNK = 128 * 1024  # raw bytes per read: base64 of it stays under the providers' 256 KiB exec output cap
READS_PER_POLL = 8
TEXT_MAX = 500
MESSAGE_MAX = 2000

# USD per million tokens, from the vendors' official pricing pages, looked up 2026-10-13:
#   Anthropic  https://platform.claude.com/docs/en/about-claude/pricing (cache_write = 5-minute writes;
#              Haiku 5.5 at its <=100k-token prompt tier)
#   OpenAI     https://developers.openai.com/api/docs/pricing (Standard tier, short context; gpt-5.6-terra and
#              gpt-5.6-luna from https://openai.com/api/pricing/). No cache-write price = the input price.
#   Google     https://ai.google.dev/gemini-api/docs/pricing (paid tier, prompts <=200k; 2026 prices for the
#              models whose price changes on 2027-01-01)
# Override or extend with HANGAR_MODEL_PRICES. A model missing here has no computed cost (tokens still count).
DEFAULT_PRICES: dict[str, dict[str, float]] = {
    # Anthropic
    "claude-fable-5-1": {"input": 10.0, "output": 50.0, "cache_write": 12.5, "cache_read": 0.25},
    "claude-mythos-5-1": {"input": 10.0, "output": 50.0, "cache_write": 12.5, "cache_read": 0.25},
    "claude-opus-5-5": {"input": 4.0, "output": 20.0, "cache_write": 5.0, "cache_read": 0.20},
    "claude-sonnet-5-5": {"input": 2.0, "output": 10.0, "cache_write": 2.5, "cache_read": 0.10},
    "claude-haiku-5-5": {"input": 0.10, "output": 0.50, "cache_write": 0.125, "cache_read": 0.01},
    "claude-fable-5": {"input": 10.0, "output": 50.0, "cache_write": 12.5, "cache_read": 1.0},
    "claude-mythos-5": {"input": 10.0, "output": 50.0, "cache_write": 12.5, "cache_read": 1.0},
    "claude-opus-5": {"input": 5.0, "output": 25.0, "cache_write": 6.25, "cache_read": 0.50},
    "claude-sonnet-5": {"input": 2.0, "output": 10.0, "cache_write": 2.5, "cache_read": 0.20},
    "claude-opus-4-8": {"input": 5.0, "output": 25.0, "cache_write": 6.25, "cache_read": 0.50},
    "claude-opus-4-7": {"input": 5.0, "output": 25.0, "cache_write": 6.25, "cache_read": 0.50},
    "claude-opus-4-6": {"input": 5.0, "output": 25.0, "cache_write": 6.25, "cache_read": 0.50},
    "claude-opus-4-5": {"input": 5.0, "output": 25.0, "cache_write": 6.25, "cache_read": 0.50},
    "claude-opus-4-1": {"input": 15.0, "output": 75.0, "cache_write": 18.75, "cache_read": 1.50},
    "claude-opus-4": {"input": 15.0, "output": 75.0, "cache_write": 18.75, "cache_read": 1.50},
    "claude-sonnet-4-6": {"input": 3.0, "output": 15.0, "cache_write": 3.75, "cache_read": 0.30},
    "claude-sonnet-4-5": {"input": 3.0, "output": 15.0, "cache_write": 3.75, "cache_read": 0.30},
    "claude-sonnet-4": {"input": 3.0, "output": 15.0, "cache_write": 3.75, "cache_read": 0.30},
    "claude-haiku-4-5": {"input": 1.0, "output": 5.0, "cache_write": 1.25, "cache_read": 0.10},
    "claude-3-5-haiku": {"input": 0.80, "output": 4.0, "cache_write": 1.0, "cache_read": 0.08},
    # OpenAI
    "gpt-6-astra": {"input": 10.0, "output": 50.0, "cache_write": 12.5, "cache_read": 1.0},
    "gpt-6.1-sol": {"input": 2.0, "output": 10.0, "cache_write": 2.5, "cache_read": 0.10},
    "gpt-6-luna": {"input": 0.10, "output": 0.50, "cache_write": 0.125, "cache_read": 0.01},
    "gpt-5.6-sol": {"input": 4.0, "output": 20.0, "cache_write": 5.0, "cache_read": 0.40},
    "gpt-5.6-cyber": {"input": 12.5, "output": 75.0, "cache_write": 15.625, "cache_read": 1.25},
    "gpt-5.6-terra": {"input": 2.0, "output": 12.0, "cache_read": 0.20},
    "gpt-5.6-luna": {"input": 0.20, "output": 1.20, "cache_read": 0.02},
    "gpt-5.3-codex": {"input": 1.75, "output": 14.0, "cache_read": 0.175},
    # Google
    "gemini-3.1-pro-preview": {"input": 2.0, "output": 12.0, "cache_read": 0.20},
    "gemini-3.8-flash": {"input": 0.75, "output": 3.75, "cache_read": 0.075},
    "gemini-3.6-flash": {"input": 0.75, "output": 3.75, "cache_read": 0.075},
    "gemini-3.5-flash-lite": {"input": 0.30, "output": 2.50, "cache_read": 0.03},
    "gemini-3-flash-preview": {"input": 0.50, "output": 3.0, "cache_read": 0.05},
    "gemini-2.5-pro": {"input": 1.25, "output": 10.0, "cache_read": 0.125},
}

# Which stream format each built-in harness writes; anything else is detected from its first JSON line.
FORMATS = {"claude-code": "claude", "codex": "codex", "pi": "pi", "antigravity": "agy"}
_SUFFIX = re.compile(r"^-(\d{8}|\d{4}-\d{2}-\d{2}|latest)$")


_THINKING = re.compile(r"-(minimal|low|medium|high)$")  # --- review #105: agy's ids end in a thinking level


def _price_names(m: str) -> list[str]:
    """--- review #105: the ids model policies resolve to, as the table names them: OpenRouter's dotted Claude
    versions (`claude-fable-5.1`), and agy's Gemini ids (`gemini-3.8-flash-high`, without `-preview`)."""
    names = [m]
    if m.startswith("claude-") and "." in m:
        names.append(m.replace(".", "-"))
    if m.startswith("gemini-") and (base := _THINKING.sub("", m)) != m:
        names += [base, base + "-preview"]
    return names


def price_for(model: str | None, table: dict[str, dict[str, float]]) -> dict[str, float] | None:
    """The price entry for a model id: exact, or a known id followed by a date / `latest` suffix. Provider
    prefixes (`anthropic/`, `google-vertex/`) and context tags (`[1m]`) are ignored."""
    if not model:
        return None
    for m in _price_names(model.lower().rsplit("/", 1)[-1].split("[", 1)[0].replace("@", "-").strip()):
        if m in table:
            return table[m]
        best = max((k for k in table if m.startswith(k) and _SUFFIX.match(m[len(k):])), key=len, default=None)
        if best:
            return table[best]
    return None


def _rates(price: dict[str, float]) -> tuple[float, float, float, float]:
    """--- overnight/cache: (input, cache read, 5-minute write, 1-hour write) per MTok. A 1-hour write costs 2x
    input on every Anthropic model (pricing page, 2026-10-13); only Anthropic reports 1-hour writes."""
    i = price.get("input", 0.0)
    return i, price.get("cache_read", i), price.get("cache_write", i), price.get("cache_write_1h", 2 * i)


def cost_of(u: Usage, price: dict[str, float]) -> float:
    i, read, w5, w1h = _rates(price)
    long = min(u.cache_write_1h_tokens, u.cache_write_tokens)
    return (u.input_tokens * i + u.output_tokens * price.get("output", 0.0) + u.cache_read_tokens * read
            + (u.cache_write_tokens - long) * w5 + long * w1h) / 1_000_000


def saved_of(u: Usage, price: dict[str, float]) -> float:
    """--- overnight/cache: what caching saved on these tokens against sending them all as plain input: reads are
    cheaper than input, writes dearer (negative when the writes were never read back)."""
    i, read, w5, w1h = _rates(price)
    long = min(u.cache_write_1h_tokens, u.cache_write_tokens)
    return (u.cache_read_tokens * (i - read) + (u.cache_write_tokens - long) * (i - w5) + long * (i - w1h)) / 1e6


def _sum_known(a: float | None, a_used: bool, b: float | None, b_used: bool) -> float | None:
    """A sum of costs (or savings) that is unknown as soon as one side used tokens without a known value."""
    if (a is None and a_used) or (b is None and b_used):
        return None
    return (a or 0.0) + (b or 0.0)


def add_usage(a: Usage | None, b: Usage | None) -> Usage | None:
    """Sum two usages. The cost is only known if both are (or one side used no tokens)."""
    if a is None or b is None:
        return a or b
    cost: float | None
    if a.cost_usd is None and a.tokens + a.cache_read_tokens:
        cost = None
    elif b.cost_usd is None and b.tokens + b.cache_read_tokens:
        cost = None
    else:
        cost = (a.cost_usd or 0.0) + (b.cost_usd or 0.0)
    src = {a.cost_source, b.cost_source} - {None}
    return Usage(input_tokens=a.input_tokens + b.input_tokens, output_tokens=a.output_tokens + b.output_tokens,
                 cache_read_tokens=a.cache_read_tokens + b.cache_read_tokens,
                 cache_write_tokens=a.cache_write_tokens + b.cache_write_tokens, cost_usd=cost,
                 cost_source=(src.pop() if len(src) == 1 else "mixed") if src else None,
                 model=a.model if a.model == b.model else None,
                 # --- overnight/cache
                 cache_write_1h_tokens=a.cache_write_1h_tokens + b.cache_write_1h_tokens,
                 saved_usd=_sum_known(a.saved_usd, bool(a.cache_read_tokens + a.cache_write_tokens),
                                      b.saved_usd, bool(b.cache_read_tokens + b.cache_write_tokens)))


def total_usage(turns: list[Any]) -> Usage | None:
    """Usage over turns (anything with a `.usage`), including a session turn's structured-result repair run."""
    out: Usage | None = None
    for t in turns:
        out = add_usage(out, getattr(t, "usage", None))
        out = add_usage(out, getattr(t, "repair_usage", None))
    return out


def usage_line(u: Usage | None) -> str:
    """'12.3k tokens (+40.1k cached), $0.42' for timelines, comments and the CLI."""
    if u is None:
        return "no usage recorded"

    def k(n: int) -> str:
        return f"{n / 1000:.1f}k" if n >= 1000 else str(n)

    s = f"{k(u.tokens)} tokens" + (f" (+{k(u.cache_read_tokens)} cached)" if u.cache_read_tokens else "")
    return s + (f", ${u.cost_usd:.2f}" if u.cost_usd is not None else ", cost unknown")


def resolve_budget(req: Budget | None, settings: Any, cap: Budget | None = None) -> Budget | None:
    """What a caller asked for, else the HANGAR_BUDGET_* defaults, each capped by the token's max budget."""
    vals: dict[str, Any] = {}
    for f in ("max_minutes", "max_tokens", "max_cost_usd"):
        v = getattr(req, f) if req else None
        v = getattr(settings, f"budget_{f}", None) if v is None else v
        c = getattr(cap, f) if cap else None
        vals[f] = c if v is None else (v if c is None else min(v, c))
    return Budget(**vals) if any(v is not None for v in vals.values()) else None


def over_budget(b: Budget | None, u: Usage | None, started_at: float | None) -> str | None:
    """Why a turn is over its budget, or None."""
    if b is None:
        return None
    if b.max_minutes and started_at and now() - started_at > b.max_minutes * 60:
        return f"ran {int((now() - started_at) // 60)} min; the budget is {b.max_minutes} min"
    if u is None:
        return None
    if b.max_tokens and u.tokens > b.max_tokens:
        return f"used {u.tokens:,} tokens; the budget is {b.max_tokens:,}"
    if b.max_cost_usd and u.cost_usd is not None and u.cost_usd > b.max_cost_usd:
        return f"spent ${u.cost_usd:.2f}; the budget is ${b.max_cost_usd:.2f}"
    return None


class RunEvent(BaseModel):
    seq: int = 0  # position in the session's event log; the `since` cursor
    ts: float = Field(default_factory=now)
    turn: int = 0  # 1-based turn number in the session
    run_id: str = ""
    kind: Kind
    text: str = ""
    data: dict[str, Any] = Field(default_factory=dict)
    off: int | None = None  # byte offset of the source line in the run log (dedupes re-reads after a restart)


def _clip(s: Any, n: int = TEXT_MAX) -> str:
    s = s if isinstance(s, str) else json.dumps(s, ensure_ascii=False, default=str)
    s = s.strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def _small(d: dict[str, Any]) -> dict[str, Any]:
    """Keep event data small: scalars only, strings clipped."""
    out: dict[str, Any] = {}
    for k, v in d.items():
        if v is None:
            continue
        if isinstance(v, str):
            out[k] = _clip(v, 300)
        elif isinstance(v, bool | int | float):
            out[k] = v
    return out


def _brief(args: Any) -> str:
    """One line describing a tool call's arguments."""
    if not isinstance(args, dict):
        return _clip(args, 200)
    for key in ("command", "CommandLine", "cmd", "file_path", "path", "TargetFile", "AbsolutePath", "pattern",
                "Query", "query", "url", "Url", "description"):
        if isinstance(args.get(key), str) and args[key].strip():
            return _clip(args[key], 200)
    return _clip(args, 200)


def _text_of(content: Any) -> str:
    """Text of a tool result / message content, whichever shape the harness uses."""
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        return _text_of(content.get("content") if "content" in content else content.get("text", ""))
    if isinstance(content, list):
        return "\n".join(_text_of(c) for c in content if isinstance(c, dict | str))
    return ""


_FILE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit",  # claude
               "write", "edit",  # pi
               "write_to_file", "replace_file_content", "multi_replace_file_content", "sed_file", "notebook_edit"}
_SHELL_TOOLS = {"Bash", "bash", "run_command", "shell"}


class Parser:
    """Incremental parser for one turn's run log. Feed it new bytes; it carries partial lines over."""

    def __init__(self, fmt: str | None = None, model: str | None = None,
                 prices: dict[str, dict[str, float]] | None = None):
        self.fmt = fmt  # claude | codex | pi | agy | None (detect from the first JSON line)
        self.model = model  # used for pricing when the stream doesn't name one (codex, agy)
        self.prices = DEFAULT_PRICES if prices is None else prices
        self.next = 0  # log offset of the next byte to read
        self.carry = b""
        self.skipping = False  # inside a line over MAX_LINE
        self.dropped = 0
        self.usage = Usage()
        self.unpriced = False  # some tokens had no price: the cost is a lower bound
        self.final = False  # the harness reported its own totals
        self.tools: dict[str, list[Any]] = {}  # open tool calls: id -> [name, path]
        self.seen: list[str] = []  # claude message ids already counted / agy steps done
        self.texts: dict[str, str] = {}  # agy: text deltas per step

    # ------------------------------------------------------------------ state
    @property
    def offset(self) -> int:
        """Where a resumed parser starts: the beginning of the line not yet complete."""
        return self.next - len(self.carry)

    def state(self) -> dict[str, Any]:
        return {"offset": self.offset if not self.skipping else self.next, "skip": self.skipping, "fmt": self.fmt,
                "model": self.model, "usage": self.usage.model_dump(), "unpriced": self.unpriced,
                "final": self.final, "tools": dict(list(self.tools.items())[-50:]), "seen": self.seen[-50:],
                "dropped": self.dropped}

    def load(self, st: dict[str, Any]) -> None:
        self.next = int(st.get("offset") or 0)
        self.skipping = bool(st.get("skip"))
        self.fmt = st.get("fmt") or self.fmt
        self.model = st.get("model") or self.model
        with contextlib.suppress(Exception):
            self.usage = Usage.model_validate(st.get("usage") or {})
        self.unpriced = bool(st.get("unpriced"))
        self.final = bool(st.get("final"))
        self.tools = dict(st.get("tools") or {})
        self.seen = list(st.get("seen") or [])
        self.dropped = int(st.get("dropped") or 0)

    def view(self) -> Usage:
        u = self.usage.model_copy()
        if self.unpriced and u.cost_source != "harness":
            u.cost_usd = None
        return u

    # ------------------------------------------------------------------ input
    def feed(self, data: bytes) -> list[RunEvent]:
        out: list[RunEvent] = []
        base = self.next - len(self.carry)
        buf = self.carry + data
        self.next += len(data)
        pos = 0
        while (nl := buf.find(b"\n", pos)) >= 0:
            if self.skipping:
                self.skipping = False  # the tail of an over-long line
            else:
                out += self._line(buf[pos:nl], base + pos)
            pos = nl + 1
        rest = buf[pos:]
        if not self.skipping and len(rest) > MAX_LINE:
            self.dropped += 1
            self.skipping = True
        self.carry = b"" if self.skipping else rest
        return out

    def flush(self) -> list[RunEvent]:
        """The turn is over: parse a last line that never got its newline."""
        rest, self.carry = self.carry, b""
        return self._line(rest, self.next - len(rest)) if rest and not self.skipping else []

    def _line(self, raw: bytes, off: int) -> list[RunEvent]:
        line = raw.decode("utf-8", errors="replace").strip()
        if not line.startswith("{"):
            return []  # stderr and other plain text share the log
        try:
            e = json.loads(line)
        except ValueError:
            return []
        if not isinstance(e, dict):
            return []
        if str(e.get("type") or "").startswith("hangar."):  # workstream steer: the turn driver's own events
            evs = _driver_event(e)
            for ev in evs:
                ev.off = off
            return evs
        if self.fmt is None:
            self.fmt = _detect(e)
        handler = {"claude": self._claude, "codex": self._codex, "pi": self._pi, "agy": self._agy}.get(self.fmt or "")
        if handler is None:
            return []
        try:
            evs = handler(e)
        except Exception as ex:  # noqa: BLE001 - a malformed event must never stop collection
            log.debug("unparseable %s event at %d: %s", self.fmt, off, ex)
            return []
        for ev in evs:
            ev.off = off
        return evs

    # ------------------------------------------------------------------ helpers
    def _ev(self, kind: str, text: Any = "", **data: Any) -> RunEvent:
        return RunEvent(kind=kind, text=_clip(text, MESSAGE_MAX if kind == "message" else TEXT_MAX),
                        data=_small(data))

    def _count(self, delta: Usage, cost: float | None = None, model: str | None = None) -> RunEvent:
        """Add one model call's usage: its cost as the harness reported it, else from the price table."""
        model = model or self.model
        src = "harness" if cost is not None else None
        price = price_for(model, self.prices)  # --- overnight/cache: savings are priced even when cost is reported
        if cost is None and price is not None:
            cost, src = cost_of(delta, price), "prices"
        if cost is None and delta.tokens + delta.cache_read_tokens:
            self.unpriced = True  # the running cost is only a lower bound now; view() reports it as unknown
        u = self.usage
        sources = {u.cost_source, src} - {None}
        self.usage = Usage(input_tokens=u.input_tokens + delta.input_tokens,
                           output_tokens=u.output_tokens + delta.output_tokens,
                           cache_read_tokens=u.cache_read_tokens + delta.cache_read_tokens,
                           cache_write_tokens=u.cache_write_tokens + delta.cache_write_tokens,
                           cost_usd=(u.cost_usd or 0.0) + (cost or 0.0),
                           cost_source=(sources.pop() if len(sources) == 1 else "mixed") if sources else None,
                           model=model or u.model,
                           cache_write_1h_tokens=u.cache_write_1h_tokens + delta.cache_write_1h_tokens,
                           saved_usd=_sum_known(u.saved_usd, bool(u.cache_read_tokens + u.cache_write_tokens),
                                                saved_of(delta, price) if price else None,
                                                bool(delta.cache_read_tokens + delta.cache_write_tokens)))
        return self._usage_event()

    def _total(self, total: Usage, cost: float | None) -> RunEvent:
        """The harness's own totals for the turn replace the running sum."""
        price = price_for(total.model or self.model or self.usage.model, self.prices)
        if cost is not None:
            total.cost_usd, total.cost_source, self.unpriced = cost, "harness", False
        else:
            total.cost_usd = cost_of(total, price) if price else None
            total.cost_source = "prices" if price else None
            self.unpriced = price is None and bool(total.tokens + total.cache_read_tokens)
        total.saved_usd = saved_of(total, price) if price else None  # --- overnight/cache
        total.model = total.model or self.usage.model or self.model
        self.usage = total
        self.final = True
        return self._usage_event(final=True)

    def _usage_event(self, final: bool = False) -> RunEvent:
        u = self.view()
        return self._ev("usage", usage_line(u), input_tokens=u.input_tokens, output_tokens=u.output_tokens,
                        cache_read_tokens=u.cache_read_tokens, cache_write_tokens=u.cache_write_tokens,
                        cost_usd=u.cost_usd, cost_source=u.cost_source, model=u.model, final=final,
                        # --- overnight/cache
                        cache_write_1h_tokens=u.cache_write_1h_tokens, cache_hit_ratio=u.cache_hit_ratio,
                        saved_usd=u.saved_usd)

    def _tool_start(self, tid: str, name: str, args: Any) -> RunEvent:
        path = None
        if isinstance(args, dict):
            path = next((args[k] for k in ("file_path", "path", "TargetFile", "notebook_path", "AbsolutePath")
                         if isinstance(args.get(k), str)), None)
        self.tools[tid] = [name, path]
        if name in _SHELL_TOOLS:
            return self._ev("command", _brief(args), id=tid, tool=name)
        # --- interfaces: which subagent a Claude Code Task/Agent call starts (the trace's `subagent` span)
        sub = args.get("subagent_type") if name in ("Task", "Agent") and isinstance(args, dict) else None
        return self._ev("tool_call", f"{name} {_brief(args)}".strip(), id=tid, tool=name, subagent=sub)

    def _tool_end(self, tid: str, ok: bool, output: Any, path: str | None = None, **data: Any) -> list[RunEvent]:
        name, opath = self.tools.pop(tid, [data.pop("tool", None), None])
        out = [self._ev("tool_result", _text_of(output), id=tid, tool=name, ok=ok, **data)]
        path = path or opath
        if ok and name in _FILE_TOOLS and path:
            out.append(self._ev("file_change", path, path=path, op=data.get("op") or "write", tool=name))
        return out

    # ------------------------------------------------------------------ Claude Code (stream-json --verbose)
    def _claude(self, e: dict[str, Any]) -> list[RunEvent]:
        t = e.get("type")
        out: list[RunEvent] = []
        if t == "system":
            if e.get("subtype") == "init":
                self.model = e.get("model") or self.model
                out.append(self._ev("progress", f"agent started ({e.get('model') or 'default model'})",
                                    session_id=e.get("session_id"), model=e.get("model"), source="harness"))
            elif e.get("subtype") == "task_summary" and e.get("detail"):
                out.append(self._ev("progress", e["detail"], source="harness"))
        elif t == "assistant":
            m = e.get("message") or {}
            model = m.get("model") if m.get("model") != "<synthetic>" else None
            for c in m.get("content") or []:
                if not isinstance(c, dict):
                    continue
                if c.get("type") == "text" and c.get("text", "").strip():
                    out.append(self._ev("message", c["text"], role="assistant"))
                elif c.get("type") == "tool_use":
                    out.append(self._tool_start(c.get("id", ""), c.get("name", "?"), c.get("input")))
            # One `assistant` line per content block, all carrying the same message usage: count it once.
            mid, u = m.get("id"), m.get("usage")
            if model and mid and isinstance(u, dict) and mid not in self.seen:
                self.seen.append(mid)
                out.append(self._count(Usage(
                    input_tokens=u.get("input_tokens") or 0, output_tokens=u.get("output_tokens") or 0,
                    cache_read_tokens=u.get("cache_read_input_tokens") or 0,
                    cache_write_tokens=u.get("cache_creation_input_tokens") or 0,
                    cache_write_1h_tokens=_claude_1h(u)), model=model))
        elif t == "user":
            m = e.get("message") or {}
            res = e.get("tool_use_result") if isinstance(e.get("tool_use_result"), dict) else {}
            for c in m.get("content") or [] if isinstance(m.get("content"), list) else []:
                if isinstance(c, dict) and c.get("type") == "tool_result":
                    op = res.get("type") if res.get("type") in ("create", "update") else None
                    out += self._tool_end(c.get("tool_use_id", ""), not c.get("is_error"), c.get("content"),
                                          path=res.get("filePath"), op=op)
        elif t == "result":
            u = e.get("usage") or {}
            cost = e.get("total_cost_usd")
            out.append(self._total(Usage(
                input_tokens=u.get("input_tokens") or 0, output_tokens=u.get("output_tokens") or 0,
                cache_read_tokens=u.get("cache_read_input_tokens") or 0,
                cache_write_tokens=u.get("cache_creation_input_tokens") or 0, cache_write_1h_tokens=_claude_1h(u),
                model=next(iter(e.get("modelUsage") or {}), None)), float(cost) if cost is not None else None))
            if e.get("is_error") or (e.get("subtype") or "success") != "success":
                out.append(self._ev("error", e.get("result") or e.get("subtype") or "the agent stopped with an error",
                                    subtype=e.get("subtype"), status=e.get("api_error_status"),
                                    reason=e.get("terminal_reason")))
        elif t == "rate_limit_event":
            info = e.get("rate_limit_info") or {}
            if info.get("status") not in (None, "allowed"):
                out.append(self._ev("progress", f"rate limit: {info.get('status')}", source="harness",
                                    status=info.get("status")))
        return out

    # ------------------------------------------------------------------ Codex (exec --json)
    def _codex(self, e: dict[str, Any]) -> list[RunEvent]:
        t = e.get("type")
        item = e.get("item") or {}
        it = item.get("type")
        iid = item.get("id", "")
        out: list[RunEvent] = []
        if t == "thread.started":
            out.append(self._ev("progress", "agent started", thread_id=e.get("thread_id"), source="harness"))
        elif t == "item.started" or (t == "item.completed" and iid not in self.tools and it in (
                "command_execution", "file_change", "mcp_tool_call", "web_search")):
            if it == "command_execution":
                out.append(self._tool_start(iid, "shell", {"command": item.get("command", "")}))
            elif it == "file_change":
                paths = [c.get("path", "") for c in item.get("changes") or []]
                out.append(self._tool_start(iid, "apply_patch", {"path": ", ".join(paths)}))
            elif it == "mcp_tool_call":
                out.append(self._tool_start(iid, f"{item.get('server', 'mcp')}.{item.get('tool', '?')}",
                                            item.get("arguments")))
            elif it == "web_search":
                out.append(self._tool_start(iid, "web_search", {"query": item.get("query", "")}))
        if t == "item.completed":
            if it == "agent_message" and item.get("text"):
                out.append(self._ev("message", item["text"], role="assistant"))
            elif it == "command_execution":
                code = item.get("exit_code")
                out += self._tool_end(iid, code == 0, (item.get("aggregated_output") or "")[-2000:], exit_code=code)
            elif it == "file_change":
                ok = item.get("status") == "completed"
                out += self._tool_end(iid, ok, ", ".join(f"{c.get('kind')} {c.get('path')}"
                                                         for c in item.get("changes") or []))
                if ok:
                    out += [self._ev("file_change", c.get("path", ""), path=c.get("path"), op=c.get("kind"),
                                     tool="apply_patch") for c in item.get("changes") or []]
            elif it in ("mcp_tool_call", "web_search"):
                out += self._tool_end(iid, item.get("status") in (None, "completed"), item.get("result") or "")
            elif it == "error":
                out.append(self._ev("error", item.get("message", ""), fatal=False))
        elif t == "turn.completed":
            u = e.get("usage") or {}
            cached = u.get("cached_input_tokens") or 0
            written = u.get("cache_write_input_tokens") or 0
            # OpenAI counts cached (and cache-written) tokens inside input_tokens.
            out.append(self._count(Usage(input_tokens=max(0, (u.get("input_tokens") or 0) - cached - written),
                                         output_tokens=u.get("output_tokens") or 0, cache_read_tokens=cached,
                                         cache_write_tokens=written)))
            self.final = True
        elif t == "turn.failed":
            out.append(self._ev("error", (e.get("error") or {}).get("message", "turn failed"), fatal=True))
        elif t == "error":
            out.append(self._ev("error", e.get("message", ""), fatal=False))
        return out

    # ------------------------------------------------------------------ pi (--mode json)
    def _pi(self, e: dict[str, Any]) -> list[RunEvent]:
        t = e.get("type")
        out: list[RunEvent] = []
        if t == "session":
            out.append(self._ev("progress", "agent started", session_id=e.get("id"), source="harness"))
        elif t == "message_end":
            m = e.get("message") or {}
            if m.get("role") != "assistant":
                return out
            text = " ".join(c.get("text", "") for c in m.get("content") or []
                            if isinstance(c, dict) and c.get("type") == "text").strip()
            if text:
                out.append(self._ev("message", text, role="assistant"))
            u = m.get("usage") or {}
            if u:
                cost = (u.get("cost") or {}).get("total") if isinstance(u.get("cost"), dict) else None
                delta = Usage(input_tokens=u.get("input") or 0, output_tokens=u.get("output") or 0,
                              cache_read_tokens=u.get("cacheRead") or 0, cache_write_tokens=u.get("cacheWrite") or 0,
                              cache_write_1h_tokens=u.get("cacheWrite1h") or 0)  # --- overnight/cache
                # pi prices from its own catalog; a model it has no price for reports 0.
                out.append(self._count(delta, cost if cost else None, model=m.get("model")))
            if m.get("stopReason") == "error":
                out.append(self._ev("error", m.get("errorMessage") or "model error", fatal=True))
        elif t == "tool_execution_start":
            out.append(self._tool_start(e.get("toolCallId", ""), e.get("toolName", "?"), e.get("args")))
        elif t == "tool_execution_end":
            out += self._tool_end(e.get("toolCallId", ""), not e.get("isError"), e.get("result"),
                                  tool=e.get("toolName"), duration_ms=e.get("durationMs"))
        elif t == "auto_retry_start":
            out.append(self._ev("error", e.get("errorMessage") or "retrying", fatal=False, attempt=e.get("attempt")))
        return out

    # ------------------------------------------------------------------ Antigravity (stream-json)
    def _agy(self, e: dict[str, Any]) -> list[RunEvent]:
        ev = e.get("event")
        out: list[RunEvent] = []
        if ev == "init":
            out.append(self._ev("progress", "agent started", conversation_id=e.get("conversation_id"),
                                source="harness"))
        elif ev == "step_update":
            su = e.get("step_update") or {}
            st, state, idx = su.get("step_type"), su.get("state"), str(su.get("step_index"))
            if st == "agent_response":
                if su.get("text_delta"):
                    self.texts[idx] = self.texts.get(idx, "") + su["text_delta"]
                if state == "DONE" and f"r{idx}" not in self.seen:
                    self.seen.append(f"r{idx}")
                    if (text := self.texts.pop(idx, "").strip()):
                        out.append(self._ev("message", text, role="assistant"))
                    u = su.get("usage") or {}
                    if u:  # input excludes cache reads; output includes thinking
                        out.append(self._count(Usage(input_tokens=u.get("input_tokens") or 0,
                                                     output_tokens=u.get("output_tokens") or 0,
                                                     cache_read_tokens=u.get("cache_read_tokens") or 0)))
            elif st == "tool":
                ti = su.get("tool_info") or {}
                name = su.get("tool_name") or ti.get("name") or "?"
                params = ti.get("parameters") or {}
                tid = f"step-{idx}"
                if tid not in self.tools and f"t{idx}" not in self.seen:
                    out.append(self._tool_start(tid, name, params))
                if state in ("DONE", "ERROR") and f"t{idx}" not in self.seen:
                    self.seen.append(f"t{idx}")
                    err = ti.get("error") or {}
                    out += self._tool_end(tid, state == "DONE", ti.get("output") or err.get("message") or "",
                                          duration_s=su.get("duration_seconds"))
            elif st == "error_message" and state == "DONE":
                out.append(self._ev("error", su.get("error") or "the model request failed", fatal=False))
        elif ev == "result":
            r = e.get("result") or {}
            u = r.get("usage") or {}
            if u:
                out.append(self._total(Usage(input_tokens=u.get("input_tokens") or 0,
                                             output_tokens=u.get("output_tokens") or 0,
                                             cache_read_tokens=u.get("cache_read_tokens") or 0), None))
            if r.get("status") not in (None, "SUCCESS"):
                out.append(self._ev("error", r.get("error") or r.get("status"), fatal=True, status=r.get("status")))
        return out


_DECIDED = {"allow": "allowed", "allow_always": "allowed_always", "deny": "denied", "cancelled": "cancelled"}


def _driver_event(e: dict[str, Any]) -> list[RunEvent]:
    """workstream steer: hangar.* lines the turn driver (driver.py) writes into the run log."""
    t, aid = e["type"].removeprefix("hangar."), e.get("id")
    if t == "approval_request" and isinstance(aid, str):
        detail = e.get("detail") if isinstance(e.get("detail"), str) else ""
        data = _small({"id": aid, "tool": e.get("tool"), "status": "pending", "policy": e.get("policy"),
                       "expires_at": e.get("expires_at"), "taint": e.get("taint"),  # --- taint (#130): why it asks
                       "args_hash": e.get("args_hash"), "risky": e.get("risky")})  # --- KAN-77, KAN-79
        data["detail"] = detail[:2200]  # whole (driver.shown): the person approving must see all of it
        return [RunEvent(kind="approval_request", text=_clip(f"{e.get('tool') or '?'}: {detail}"), data=data)]
    if t == "approval_resolved" and isinstance(aid, str) and e.get("decision") in _DECIDED:
        status = _DECIDED[e["decision"]]
        by = e.get("by") or "hangar"
        return [RunEvent(kind="approval_request", text=_clip(f"{status} by {by}"
                                                             + (f": {e['message']}" if e.get("message") else "")),
                         data=_small({"id": aid, "status": status, "by": by, "message": e.get("message"),
                                      "receipt": e.get("receipt"), "approval": e.get("approval")}))]  # --- KAN-77
    if t in ("steer", "interrupt"):
        text = e.get("text") if isinstance(e.get("text"), str) else ""
        what = "steered" if t == "steer" else "interrupted"
        return [RunEvent(kind="progress", text=_clip(f"{what}: {text}" if text else what),
                         data=_small({"source": "hangar", "action": t, "command": aid}))]
    if t == "taint":  # --- taint (#130): the driver saw the turn read untrusted content (a web tool)
        ref = e.get("ref") if isinstance(e.get("ref"), str) else "the driver"
        return [RunEvent(kind="progress", text=_clip(f"driver: tainted ({ref})"),
                         data=_small({"source": "hangar", "action": "driver_taint", "ref": ref}))]
    if t == "tree":  # --- #165: the working tree's git tree id after a tool, or at the turn's start or end
        return tree_events(e.get("tree"), tool_id=e.get("tool_use_id"), phase=e.get("phase"), ms=e.get("ms"),
                           skipped=e.get("skipped"))
    if t == "verify":  # --- overnight/autofix: the driver's verify gate ran the job's check
        ok = e.get("passed") is True
        text = "the job's check passed" if ok else (f"the job's check failed (exit {e.get('exit_code')}); sent back "
                                                    f"to the agent ({e.get('round')} of {e.get('of')})")
        return [RunEvent(kind="progress", text=_clip(text),
                         data=_small({"source": "hangar", "action": "verify", "passed": ok,
                                      "exit_code": e.get("exit_code"), "round": e.get("round")}))]
    return []


_TREE = re.compile(r"^[0-9a-f]{40,64}$")


def tree_events(tree: Any, *, tool_id: Any = None, phase: Any = None, ms: Any = None, skipped: Any = None,
                source: str = "hangar") -> list[RunEvent]:
    """--- #165: a `progress` event with action `tree`: {tree, id (the tool call), phase (start | end), ms} or
    {skipped: slow | error, ...}. Whether it can be trusted is stamped where it is recorded (TurnWatcher)."""
    phase = phase if phase in ("start", "end") else None
    tid = str(tool_id)[:200] if isinstance(tool_id, str | int) and not phase else None
    ms = ms if isinstance(ms, int) and not isinstance(ms, bool) else None
    if isinstance(skipped, str) and skipped:
        return [RunEvent(kind="progress", text=_clip(f"tree hashing stopped ({skipped[:40]})"),
                         data=_small({"source": source, "action": "tree", "skipped": skipped[:40], "phase": phase,
                                      "id": tid, "ms": ms}))]
    if not isinstance(tree, str) or not _TREE.match(tree):
        return []
    where = f"at the turn's {phase}" if phase else "after a tool"
    return [RunEvent(kind="progress", text=f"tree {tree[:12]} {where}",
                     data=_small({"source": source, "action": "tree", "tree": tree, "phase": phase, "id": tid,
                                  "ms": ms}))]


def _claude_1h(u: dict[str, Any]) -> int:
    """--- overnight/cache: the 1-hour part of a Claude usage's cache writes (`cache_creation`, verified with Claude
    Code 2.1.296 in both `assistant` and `result` lines)."""
    cc = u.get("cache_creation")
    n = cc.get("ephemeral_1h_input_tokens") if isinstance(cc, dict) else 0
    return n if isinstance(n, int) and n > 0 else 0


def _detect(e: dict[str, Any]) -> str | None:
    if "event" in e and isinstance(e.get("event"), str):
        return "agy"
    t = str(e.get("type") or "")
    if t in ("system", "assistant", "user", "result", "stream_event", "rate_limit_event"):
        return "claude"
    if t.startswith(("thread.", "turn.", "item.")):
        return "codex"
    if t in ("session", "agent_start", "turn_start", "message_start", "message_end", "tool_execution_start"):
        return "pi"
    return None


def parse_log(data: bytes, harness: str | None = None, model: str | None = None,
              prices: dict[str, dict[str, float]] | None = None) -> tuple[list[RunEvent], Usage]:
    """Parse a whole run log at once (tests, offline tools)."""
    p = Parser(FORMATS.get(harness or ""), model, prices)
    evs = p.feed(data) + p.flush()
    return evs, p.view()


# ====================================================================== storage
_SEQ = re.compile(rb'^\{"seq":\s*(\d+)')


class EventLog:
    """Per-session JSONL of events, `<root>/<session id>.jsonl`. Rotates to `.1.jsonl` past `max_bytes`."""

    def __init__(self, root: Path, max_bytes: int):
        self.root = root
        self.max_bytes = max(64 * 1024, max_bytes)
        self._last: dict[str, int] = {}

    def _paths(self, sid: str) -> tuple[Path, Path]:
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", sid)
        return self.root / f"{safe}.1.jsonl", self.root / f"{safe}.jsonl"

    def last_seq(self, sid: str) -> int:
        if sid not in self._last:
            n = 0
            for p in reversed(self._paths(sid)):
                if p.exists() and (n := _last_seq_in(p)):
                    break
            cur = self._paths(sid)[1]
            with contextlib.suppress(OSError), open(cur, "rb+") as f:  # a write torn by a crash: end that line
                f.seek(0, os.SEEK_END)
                if f.tell():
                    f.seek(-1, os.SEEK_END)
                    if f.read(1) != b"\n":
                        f.write(b"\n")
            self._last[sid] = n
        return self._last[sid]

    def append(self, sid: str, events: list[RunEvent]) -> list[RunEvent]:
        if not events:
            return []
        self.root.mkdir(parents=True, exist_ok=True)
        seq = self.last_seq(sid)
        lines = []
        for ev in events:
            seq += 1
            ev.seq = seq
            lines.append(ev.model_dump_json(exclude_none=True) + "\n")
        old, cur = self._paths(sid)
        with open(cur, "ab") as f:
            f.write("".join(lines).encode())
        self._last[sid] = seq
        with contextlib.suppress(OSError):
            if cur.stat().st_size > self.max_bytes:
                os.replace(cur, old)
        return events

    def read(self, sid: str, since: int = 0, limit: int = 200, tail: int | None = None) -> list[RunEvent]:
        """Events after `since`, oldest first, at most `limit`; or with `tail`, the last `tail` events."""
        lines: collections.deque[bytes] = collections.deque(maxlen=tail) if tail else collections.deque()
        for p in self._paths(sid):
            if not p.exists():
                continue
            with open(p, "rb") as f:
                for line in f:
                    m = _SEQ.match(line)
                    if not m or int(m.group(1)) <= since:
                        continue
                    lines.append(line)
                    if tail is None and len(lines) >= limit:
                        break
            if tail is None and len(lines) >= limit:
                break
        out: list[RunEvent] = []
        for line in lines:
            with contextlib.suppress(ValueError):
                out.append(RunEvent.model_validate_json(line))
        return out

    def max_off(self, sid: str, run_id: str) -> int:
        """The furthest log offset already recorded for a run (re-reads below it are duplicates)."""
        best = -1
        for p in self._paths(sid):
            if not p.exists():
                continue
            with open(p, "rb") as f:
                for line in f:
                    if run_id.encode() not in line:
                        continue
                    with contextlib.suppress(ValueError):
                        ev = RunEvent.model_validate_json(line)
                        if ev.run_id == run_id and ev.off is not None:
                            best = max(best, ev.off)
        return best

    def drop(self, sid: str) -> None:
        for p in self._paths(sid):
            with contextlib.suppress(OSError):
                p.unlink()
        self._last.pop(sid, None)


def _last_seq_in(p: Path) -> int:
    with open(p, "rb") as f:
        f.seek(0, os.SEEK_END)
        f.seek(max(0, f.tell() - 65536))
        lines = f.read().splitlines()
    for line in reversed(lines):
        if m := _SEQ.match(line):
            return int(m.group(1))
    return 0


# ====================================================================== collection
class Activity:
    """Event storage and turn watchers for one control plane. Owned by SessionService."""

    def __init__(self, settings: Any, redact: Callable[[Any], Any]):
        self.settings = settings
        self.redact = redact
        self.log = EventLog(settings.data_dir / "events", int(settings.events_max_mb) * 1024 * 1024)
        # --- stream: called with (session or job id, events) after every append, for live streams; must not raise
        self.listeners: list[Callable[[str, list[RunEvent]], None]] = []
        from hangar.livestate import Live  # KAN-140: what each live turn is doing, for GET /api/fleet

        self.live = Live()

    @property
    def prices(self) -> dict[str, dict[str, float]]:
        return {**DEFAULT_PRICES, **{k.lower(): v for k, v in (self.settings.model_prices or {}).items()}}

    def append(self, session_id: str, events: list[RunEvent]) -> list[RunEvent]:
        """Record events (redacted). Workstream E feeds `progress` events here."""
        for ev in events:
            ev.text = self.redact(ev.text)
            ev.data = self.redact(ev.data)
        out = self.log.append(session_id, events)
        for fn in self.listeners:  # --- stream
            try:
                fn(session_id, out)
            except Exception:  # noqa: BLE001 - a listener must never lose the events
                log.exception("event listener failed")
        return out

    def read(self, session_id: str, since: int = 0, limit: int = 200, tail: int | None = None) -> list[RunEvent]:
        return self.log.read(session_id, since=max(0, since), limit=max(1, min(limit, 1000)),
                             tail=max(1, min(tail, 1000)) if tail else None)

    def watch(self, fleet: Any, s: Session, turn: SessionTurn, save: Callable[[], None],
              taint_files: set[str] | None = None, on_taint: Callable[[str], None] | None = None) -> TurnWatcher:
        return TurnWatcher(self, fleet, s, turn, save, taint_files, on_taint)


class TurnWatcher:
    """Follows one running turn's log: events into the session's log, usage onto the turn, budget checks."""

    def __init__(self, activity: Activity, fleet: Any, s: Session, turn: SessionTurn, save: Callable[[], None],
                 taint_files: set[str] | None = None, on_taint: Callable[[str], None] | None = None):
        self.a, self.fleet, self.s, self.turn, self.save = activity, fleet, s, turn, save
        # --- taint (#129): which MCP servers are untrusted, and the untrusted context files, for taint.observe
        from hangar import taint  # import cycle

        self.taint_trust = taint.mcp_trust(fleet, s)
        self.taint_files = taint_files or set()
        self.on_taint = on_taint
        self._pending: list[tuple[RunEvent, dict[str, Any]]] = []
        self.parser = Parser(FORMATS.get(turn.harness or s.harness or ""), s.model, activity.prices)
        self.errors = 0
        if turn.event_cursor:  # re-attached after a restart: continue from the saved read position
            self.parser.load(turn.event_cursor)
        # Events recorded after the cursor was last saved (a restart in between) are not recorded twice. The usage
        # saved with the cursor is consistent with its offset, so re-reading those lines counts them exactly once.
        self.floor = activity.log.max_off(s.id, turn.run_id or "") if turn.run_id else -1
        self.number = next((i for i, t in enumerate(s.turns, 1) if t is turn), len(s.turns))
        self.log_path = f"/home/{fleet.settings.sandbox_user}/.hangar/runs/{turn.run_id}.log"
        # --- harden: a split run's log is read where hangar's user writes it, as that user (the agent's link to it is
        # the agent's to point anywhere), and its approvals from the driver's own file, not from the log
        self.split, self.approvals_read = False, 0
        with contextlib.suppress(Exception):
            self.split = bool(turn.run_id) and isolation.run_split(fleet.get(s.sandbox_id), turn.run_id)
            if self.split:
                self.log_path = f"{isolation.RUNS}/{turn.run_id}.log"
        self._follow: asyncio.Task[str] | None = None  # --- stream: the log followed live (sandbox_streams.follow)
        self._follows = 0
        self._fed = False

    def note(self, kind: str, text: str, **data: Any) -> None:
        """An event hangar itself adds to the turn (e.g. why it was stopped)."""
        self._record([RunEvent(kind=kind, text=_clip(text), data=_small(data))])

    def _record(self, evs: list[RunEvent], from_log: bool = True) -> None:
        if self.split and from_log:  # --- harden: anything in the log may be the agent's; see _approvals
            evs = [ev for ev in evs if ev.kind != "approval_request"]
        for ev in evs:  # --- #165: only a split run's log is out of the agent's reach (and hangar's own exec's trees)
            if ev.kind == "progress" and ev.data.get("action") == "tree":
                ev.data["trusted"] = self.split
        keep = []
        for ev in evs:
            if ev.off is not None and ev.off <= self.floor:
                continue
            ev.turn, ev.run_id = self.number, self.turn.run_id or ""
            keep.append(ev)
        if keep:
            tainting = self._taint(keep)  # --- taint (#129): classified now; their seq is known once appended
            try:
                self.a.append(self.s.id, keep)
                if self.s.job_id:
                    # --- interactions: a job's sessions (best-of-N attempts, the judge, the winner that carries
                    # on) also feed the job's own stream, so its readers keep one cursor across all of them.
                    attempt = self.s.labels.get("hangar.attempt")
                    extra = {"attempt": int(attempt)} if attempt and attempt.isdigit() else {}
                    self.a.append(self.s.job_id, [ev.model_copy(update={"data": {**ev.data, **extra}})
                                                  for ev in keep])
            except OSError as e:  # e.g. a full disk: lose the events, not the turn
                log.warning("session %s: could not record events: %s", self.s.id, e)
            if tainting:
                self._taint_seen()
            self.a.live.observe(self.s, self.number, self.turn, keep)  # KAN-140: the fleet's live picture
            from hangar.steer import track  # workstream steer: approvals asked and answered (import cycle)

            if track(self.turn, keep, on_gate=self._gated):
                self.save()
            from hangar import cache  # --- overnight/cache: a first response releases the turn's held siblings

            cache.watched(self.s.id, keep)

    def _gated(self, a: Any) -> None:
        """--- taint (#130): an approval the taint gate asked for was answered by the policy or a timeout."""
        from hangar import taint
        from hangar.steer import _sink  # import cycle

        with contextlib.suppress(Exception):
            taint.audit(self.fleet, f"{a.by or 'policy'}", _sink(a), a.status, self.s.id, run=self.turn.run_id,
                        approval=a.id, why=a.taint)

    def _taint(self, evs: list[RunEvent]) -> bool:
        """--- taint (#129): note which of these events read untrusted content (one lookup per event). Their seq
        isn't known until they're appended, so _taint_seen records them after. True when any did."""
        from hangar import taint  # import cycle

        self._pending = [(ev, src) for ev in evs
                         if (src := taint.classify(ev, self.taint_trust, self.taint_files)) is not None]
        return bool(self._pending)

    def _taint_seen(self) -> None:
        """Record what _taint found; on the session's first source, a `taint:` event and the listener."""
        from hangar import taint  # import cycle

        first = None
        for ev, src in self._pending:
            if src is None:
                continue
            src["seq"] = ev.seq
            if taint.mark(self.s, self.turn, self.number, src) and first is None:
                first = src["ref"]
        self._pending = []
        self.save()
        if first is not None:
            self._record([RunEvent(kind="progress", text=f"taint: read untrusted content from {first}",
                                   data={"source": "hangar", "action": "taint", "ref": first[:200]})],
                         from_log=False)
            if self.on_taint is not None:
                try:
                    self.on_taint(first)
                except Exception:  # noqa: BLE001 - recording taint never breaks a turn
                    log.exception("session %s: taint listener failed", self.s.id)

    async def poll(self) -> str | None:
        """Read what the log gained since the last poll. Returns why the turn is over budget, or None."""
        if self.turn.run_id and self.s.sandbox_id:
            if self._follow is not None and not self._follow.done():  # --- stream: followed live; keep the cursor
                self._save_fed()
            else:
                try:
                    await self._read(final=False)
                except Exception:  # noqa: BLE001 - activity is best effort; it must never break the turn
                    log.exception("session %s: collecting events failed", self.s.id)
                self._start_follow()
            try:
                await self._approvals()  # --- harden (integrate: every poll, also while the log is followed)
            except Exception:  # noqa: BLE001
                log.exception("session %s: collecting approvals failed", self.s.id)
        return over_budget(self.s.budget, self.turn.usage, self.turn.started_at)

    # --- stream: between polls, the log is followed through hangar-envd (`tail -F`), so each event is recorded
    # (and reaches live streams) as the agent writes it rather than up to a poll interval later. Polling takes over
    # whenever following isn't possible or stops (no daemon, the log truncated, the sandbox gone).
    # integrate (#69 x #68): a split run's follower tails hangar's own log in RUNS (0644, so envd, which runs as the
    # agent, can read it), never the agent's link. Its bytes still pass through a process of the agent's uid, so what
    # it feeds is informational, as the log's events and usage already are: approvals are dropped from it (_record)
    # and read from the driver's file each poll, the exit file only tells it when to stop, and the turn's status,
    # exit code and summary come from hangar's own reads (fleet.agent_status, sessions).
    def _start_follow(self) -> None:
        closing = getattr(self.fleet, "closing", None)
        if not getattr(self.fleet.settings, "stream_turn_logs", False) or closing is None or closing.is_set():
            return
        if self._follow is not None:
            why = self._follow.result() if not self._follow.cancelled() and not self._follow.exception() else "error"
            if why in ("ended", "gone", "shutdown", "destroyed", "capped"):  # capped: review #68
                self._follows = 3  # nothing to gain from trying again
        if self._follows >= 3:
            return
        self._follows += 1
        from hangar.sandbox_streams import follow  # import cycle

        exit_path = self.log_path.removesuffix(".log") + ".exit"
        self._follow = asyncio.create_task(follow(self.fleet, self.s.sandbox_id or "", self.log_path, exit_path,
                                                  self.parser.next, self._feed))

    def _feed(self, data: bytes) -> None:
        self._record(self.parser.feed(data))
        self._fed = True

    def _save_fed(self) -> None:
        if self._fed:
            self._fed = False
            p = self.parser
            self.turn.usage = p.view() if (p.usage.tokens + p.usage.cache_read_tokens or p.final) else None
            self.turn.event_cursor = p.state()
            self.save()

    async def _stop_follow(self) -> None:
        if self._follow is not None:
            self._follow.cancel()
            await asyncio.gather(self._follow, return_exceptions=True)
            self._save_fed()

    async def finish(self, alive: bool = True) -> None:
        """The turn is over: read the rest of the log (if the sandbox is still there) and close the cursor."""
        await self._stop_follow()  # --- stream
        if alive and self.turn.run_id and self.s.sandbox_id:
            try:
                await self._read(final=True)
                await self._approvals()  # --- harden
            except Exception:  # noqa: BLE001
                log.exception("session %s: collecting events failed", self.s.id)
        p = self.parser
        self.turn.usage = p.view() if (p.usage.tokens + p.usage.cache_read_tokens or p.final) else self.turn.usage
        self.turn.event_cursor = None
        self.save()
        self.a.live.finished(self.s, self.turn)  # KAN-140
        from hangar import cache  # --- overnight/cache: siblings held for this turn go ahead whatever happened

        cache.STAGGER.responded(self.s.id)

    async def _read(self, final: bool) -> None:
        p, changed = self.parser, False
        q = shlex.quote(self.log_path)
        for _ in range(READS_PER_POLL * (4 if final else 1)):
            try:
                res = await self._sh(f"stat -L -c %s {q} 2>/dev/null || echo 0; "  # --- isolation: -L, a link
                                     f"tail -c +{p.next + 1} {q} 2>/dev/null | head -c {CHUNK} | base64 -w0")
                size_s, _, b64 = res.stdout.partition("\n")
                data = base64.b64decode(b64.strip() or b"")
                size = int(size_s.strip() or 0)
            except Exception as e:  # noqa: BLE001 - sandbox busy or gone: try again next poll
                self.errors += 1
                if self.errors in (1, 10):
                    log.warning("session %s: could not read run log %s: %s", self.s.id, self.turn.run_id, e)
                break
            if size < p.offset:  # the log was truncated or replaced: start over
                p.next, p.carry = 0, b""
                continue
            if not data:
                break
            self._record(p.feed(data))
            changed = True
            if p.next >= size:
                break
        if final:
            self._record(p.flush())
        if changed:
            self.turn.usage = p.view() if (p.usage.tokens + p.usage.cache_read_tokens or p.final) else None
            self.turn.event_cursor = p.state()
            self.save()

    async def _sh(self, command: str) -> Any:
        if self.split:  # --- harden: as hangar's user
            sb, prov = self.fleet._running(self.s.sandbox_id)
            return await isolation.sh(prov, sb, command, timeout=30)
        return await self.fleet.exec(self.s.sandbox_id, command, timeout=30, actor=f"session:{self.s.id}", quiet=True)

    async def _approvals(self) -> None:
        """--- harden: a split turn's approval requests and answers, from the driver's own copy in its control
        directory (driver.Control.record), which only hangar's user can write. Read whole each time the watcher
        starts (after a restart too): only what changes the turn's approvals is recorded."""
        if not (self.split and self.turn.live and self.turn.run_id):
            return
        from hangar.steer import track  # import cycle

        side = False  # --- KAN-186: a sidecar run's control directory is in its driver container's CTL
        with contextlib.suppress(Exception):
            side = isolation.run_sidecar(self.fleet.get(self.s.sandbox_id), self.turn.run_id)
        path = shlex.quote(f"{isolation.ctl_dir(self.fleet.settings, self.turn.run_id, True, side)}/approvals")
        for _ in range(READS_PER_POLL):
            try:
                res = await self._sh(f"tail -c +{self.approvals_read + 1} {path} 2>/dev/null | head -c {CHUNK} "
                                     "| base64 -w0")
                data = base64.b64decode(res.stdout.strip() or b"")
            except Exception:  # noqa: BLE001 - sandbox busy or gone: the next poll reads on from here
                return
            end = data.rfind(b"\n") + 1  # whole lines only (one is at most a few KB)
            if not end:
                return
            self.approvals_read += end
            fresh = []
            for line in data[:end].splitlines():
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if isinstance(e, dict) and str(e.get("type") or "").startswith("hangar.approval_"):
                    fresh += [ev for ev in _driver_event(e) if track(self.turn, [ev])]
            if fresh:
                self._record(fresh, from_log=False)
                self.save()
            if len(data) < CHUNK:
                return
