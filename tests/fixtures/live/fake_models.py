"""Scripted model APIs for running the real Claude Code and Codex CLIs offline (test_driver_real_clis).

Anthropic Messages (`POST /v1/messages`, SSE) and OpenAI Responses (`POST /v1/responses`, SSE): the first call of a
turn asks for one shell command, `touch probe.txt`; once its result is back, the model answers in text and quotes
the last user text it saw, so a test can tell that a steer reached it.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


def _sse(events: list[dict[str, Any]]) -> bytes:
    return b"".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n".encode() for e in events)


def _texts(content: Any) -> list[str]:
    if isinstance(content, str):
        return [content]
    return [c.get("text", "") for c in content or [] if isinstance(c, dict) and c.get("type") in ("text", "input_text")]


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    delay = 0.0  # seconds before each answer, so a test can act while the model "thinks"
    call: tuple[str, dict[str, Any]] = ("Bash", {"command": "touch probe.txt"})  # Claude Code's one tool call
    shell = "touch probe.txt"  # Codex's one command
    # --- overnight/cache: what each Messages request carried (its cache_control TTLs, anthropic-beta), and the
    # usage the Anthropic side reports (give it a cache_creation split to price 1-hour writes)
    seen: list[dict[str, Any]] = []
    usage: dict[str, Any] = {"input_tokens": 50, "output_tokens": 1, "cache_read_input_tokens": 0,
                             "cache_creation_input_tokens": 0}

    def log_message(self, *_: Any) -> None:
        pass

    def _send(self, body: bytes, kind: str = "application/json", code: int = 200) -> None:
        self.send_response(code)
        self.send_header("content-type", kind)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - http.server's name
        self._send(b'{"data": [], "models": [], "has_more": false}')

    def do_POST(self) -> None:  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers.get("content-length") or 0)) or b"{}")
        if "count_tokens" in self.path:
            return self._send(b'{"input_tokens": 10}')
        if self.path.startswith("/v1/messages"):  # --- overnight/cache
            ttls: list[str] = []
            _ttls(body, ttls)
            self.seen.append({"path": self.path, "model": body.get("model"), "ttls": ttls,
                              "beta": self.headers.get("anthropic-beta") or "", "tools": len(body.get("tools") or [])})
        if self.delay:
            time.sleep(self.delay)
        if self.path.startswith("/v1/messages"):
            return self._anthropic(body)
        return self._openai(body)

    def _anthropic(self, body: dict[str, Any]) -> None:
        msgs = body.get("messages") or []
        last = (msgs[-1] if msgs else {}).get("content")
        answered = isinstance(last, list) and any(isinstance(c, dict) and c.get("type") == "tool_result"
                                                  for c in last)
        tools = [t.get("name") for t in body.get("tools") or []]
        ran = json.dumps(msgs).count('"tool_result"')
        said = " | ".join(t[:80] for m in msgs[-2:] for t in _texts(m.get("content")) if t.strip())
        msg = {"id": f"msg_{time.time_ns()}", "type": "message", "role": "assistant", "model": body.get("model"),
               "content": [], "stop_reason": None, "stop_sequence": None,
               "usage": dict(self.usage)}
        events: list[dict[str, Any]] = [{"type": "message_start", "message": msg}]
        last = " ".join(t for m in msgs[-2:] for t in _texts(m.get("content")))  # AGAIN: call it in a later turn
        if self.call[0] in tools and not answered and (ran == 0 or "AGAIN" in last) and "RUN" in json.dumps(msgs):
            events += [{"type": "content_block_start", "index": 0, "content_block": {
                           "type": "tool_use", "id": f"toolu_{time.time_ns()}", "name": self.call[0], "input": {}}},
                       {"type": "content_block_delta", "index": 0, "delta": {
                           "type": "input_json_delta", "partial_json": json.dumps(self.call[1])}}]
            stop = "tool_use"
        else:
            events += [{"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
                       {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta",
                                                                             "text": "done: " + said}}]
            stop = "end_turn"
        events += [{"type": "content_block_stop", "index": 0},
                   {"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None},
                    "usage": {"output_tokens": 12}},
                   {"type": "message_stop"}]
        self._send(_sse(events), "text/event-stream")

    def _openai(self, body: dict[str, Any]) -> None:
        items = body.get("input") or []
        tools = [t.get("name") or t.get("type") for t in body.get("tools") or []]
        users = [n for n, i in enumerate(items) if isinstance(i, dict) and i.get("role") == "user"]
        again = bool(users) and "AGAIN" in json.dumps(items[users[-1]])  # a later turn: from its message on
        answered = any(isinstance(i, dict) and i.get("type") == "function_call_output"
                       for i in items[users[-1] if again else 0:])
        rid = f"resp_{time.time_ns()}"
        events: list[dict[str, Any]] = [{"type": "response.created", "response": {"id": rid}}]
        if tools and not answered:
            name = "exec_command" if "exec_command" in tools else "shell"
            args = {"cmd": self.shell} if name == "exec_command" else {"command": ["bash", "-lc", self.shell]}
            item = {"type": "function_call", "id": "fc_1", "call_id": f"call_{time.time_ns()}", "name": name,
                    "arguments": json.dumps(args), "status": "completed"}
        else:
            said = " | ".join(t[:80] for i in items[-3:] if isinstance(i, dict) and i.get("role") == "user"
                              for t in _texts(i.get("content")) if t.strip())
            item = {"type": "message", "id": "msg_1", "role": "assistant", "status": "completed",
                    "content": [{"type": "output_text", "text": "done: " + said, "annotations": []}]}
        events += [{"type": "response.output_item.added", "output_index": 0, "item": item},
                   {"type": "response.output_item.done", "output_index": 0, "item": item},
                   {"type": "response.completed", "response": {"id": rid, "usage": {
                       "input_tokens": 100, "input_tokens_details": {"cached_tokens": 20}, "output_tokens": 10,
                       "output_tokens_details": {"reasoning_tokens": 0}, "total_tokens": 110}}}]
        self._send(_sse(events), "text/event-stream")


def _ttls(o: Any, out: list[str]) -> None:
    """Every cache_control in a request body, as its ttl ("5m" when it has none: the API's default)."""
    if isinstance(o, dict):
        if isinstance(o.get("cache_control"), dict):
            out.append(o["cache_control"].get("ttl") or "5m")
        for v in o.values():
            _ttls(v, out)
    elif isinstance(o, list):
        for v in o:
            _ttls(v, out)


def serve(delay: float = 0.0, call: tuple[str, dict[str, Any]] | None = None,
          shell: str | None = None, usage: dict[str, Any] | None = None) -> tuple[ThreadingHTTPServer, int]:
    """`call` (Claude Code) and `shell` (Codex) replace the turn's one tool call, `touch probe.txt`. The requests
    are recorded in `srv.RequestHandlerClass.seen`."""
    handler = type("H", (Handler,), {"delay": delay, "call": call or Handler.call, "shell": shell or Handler.shell,
                                     "seen": [], "usage": usage or Handler.usage})
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]
