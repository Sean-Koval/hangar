"""A stub of Anthropic's Messages API for capacity rehearsals (KAN-218): the real Claude Code CLI runs real turns
against it, and no model is called and nothing is spent.

    STUB_PORT=8080 STUB_THINK_SECONDS=3 STUB_WORK="sleep 20" python3 stub_model.py

A turn goes like a short coding turn: the first answer waits STUB_THINK_SECONDS (the model's latency), then asks for
one Bash tool call running STUB_WORK (the agent's work: tests, a build). The answer after the tool's result waits again
and ends the turn with STUB_REPLY. A request that offers no Bash tool gets the reply at once. Streaming (SSE) and plain
JSON answers are both served, `count_tokens` answers a fixed estimate, and any other path answers `{}`.

Standard library only, so it runs as an environment service on a stock Python image (docs/team-scale/rehearsal.md).
Its usage numbers are small fixed values: a stub turn costs $0 at any price.
"""

import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

THINK = float(os.environ.get("STUB_THINK_SECONDS", "3"))
WORK = os.environ.get("STUB_WORK", "sleep 20")
REPLY = os.environ.get("STUB_REPLY", "DONE")
MODEL = "claude-stub"
USAGE = {"input_tokens": 120, "output_tokens": 24, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
seq = [0]


def ident(prefix):
    seq[0] += 1
    return f"{prefix}_stub{os.getpid()}x{seq[0]:08d}"


def answered(messages):
    """Whether the conversation's last user message carries a tool result: the work is done, end the turn."""
    last = messages[-1] if messages else {}
    content = last.get("content")
    return isinstance(content, list) and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content)


def reply(body):
    """The answer's content blocks and stop reason."""
    tools = {t.get("name") for t in body.get("tools") or [] if isinstance(t, dict)}
    if "Bash" in tools and not answered(body.get("messages") or []):
        block = {"type": "tool_use", "id": ident("toolu"), "name": "Bash",
                 "input": {"command": WORK, "description": "Run the rehearsal's stand-in for the agent's work"}}
        return [block], "tool_use"
    return [{"type": "text", "text": REPLY}], "end_turn"


def events(blocks, stop):
    msg = {"id": ident("msg"), "type": "message", "role": "assistant", "model": MODEL, "content": [],
           "stop_reason": None, "stop_sequence": None, "usage": {**USAGE, "output_tokens": 1}}
    yield "message_start", {"type": "message_start", "message": msg}
    for i, b in enumerate(blocks):
        if b["type"] == "text":
            yield "content_block_start", {"type": "content_block_start", "index": i,
                                          "content_block": {"type": "text", "text": ""}}
            yield "content_block_delta", {"type": "content_block_delta", "index": i,
                                          "delta": {"type": "text_delta", "text": b["text"]}}
        else:
            yield "content_block_start", {"type": "content_block_start", "index": i,
                                          "content_block": {**b, "input": {}}}
            yield "content_block_delta", {"type": "content_block_delta", "index": i,
                                          "delta": {"type": "input_json_delta", "partial_json": json.dumps(b["input"])}}
        yield "content_block_stop", {"type": "content_block_stop", "index": i}
    yield "message_delta", {"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None},
                            "usage": {"output_tokens": USAGE["output_tokens"]}}
    yield "message_stop", {"type": "message_stop"}


class Stub(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # one line a request, to stdout
        print(f"{self.command} {self.path.split('?')[0]} {args[1] if len(args) > 1 else ''}", flush=True)

    def send_json(self, code, obj):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self.send_json(200, {})

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("content-length") or 0))
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            return self.send_json(400, {"type": "error", "error": {"type": "invalid_request_error",
                                                                     "message": "body is not JSON"}})
        path = self.path.split("?")[0]
        if path.endswith("/count_tokens"):
            return self.send_json(200, {"input_tokens": USAGE["input_tokens"]})
        if not path.endswith("/messages"):
            return self.send_json(200, {})
        blocks, stop = reply(body)
        time.sleep(THINK)
        if not body.get("stream"):
            return self.send_json(200, {"id": ident("msg"), "type": "message", "role": "assistant", "model": MODEL,
                                        "content": blocks, "stop_reason": stop, "stop_sequence": None,
                                        "usage": USAGE})
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("cache-control", "no-cache")
        self.send_header("connection", "close")
        self.end_headers()
        for name, data in events(blocks, stop):
            self.wfile.write(f"event: {name}\ndata: {json.dumps(data)}\n\n".encode())
            self.wfile.flush()
        self.close_connection = True


if __name__ == "__main__":
    port = int(os.environ.get("STUB_PORT", "8080"))
    print(f"stub model on 127.0.0.1:{port}: think {THINK}s, work {WORK!r}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", port), Stub).serve_forever()
