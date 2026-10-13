"""A scripted local stand-in for the Anthropic Messages and OpenAI Responses APIs (streaming).

Only for capturing the real harness CLIs' output formats offline (capture.sh): the CLIs are real, the model is not,
and nothing leaves the machine. Per conversation: write a file, then run a shell command, then answer.
Usage: python3 mock_model.py PORT   (CAPTURE_DIR = the agent's working directory; CAPTURE_FAIL=1 returns HTTP 400)
"""

import gzip
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Model(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _body(self) -> dict:
        if self.headers.get("transfer-encoding", "").lower() == "chunked":
            raw = b""
            while size := int(self.rfile.readline().strip() or b"0", 16):
                raw += self.rfile.read(size)
                self.rfile.readline()
            self.rfile.readline()
        else:
            raw = self.rfile.read(int(self.headers.get("content-length") or 0))
        if self.headers.get("content-encoding") == "gzip":
            raw = gzip.decompress(raw)
        try:
            return json.loads(raw or b"{}")
        except ValueError:
            return {}

    def _json(self, obj: dict, status: int = 200) -> None:
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _sse(self, events: list[tuple[str, dict]]) -> None:
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("connection", "close")  # one response per connection: clients know where it ends
        self.close_connection = True
        self.end_headers()
        for name, data in events:
            self.wfile.write(f"event: {name}\ndata: {json.dumps(data)}\n\n".encode())
            self.wfile.flush()

    def do_GET(self):
        self._json({"data": [], "models": []})

    def do_POST(self):
        req = self._body()
        anthropic = "/messages" in self.path
        if os.environ.get("CAPTURE_FAIL"):
            if anthropic:
                return self._json({"type": "error", "error": {
                    "type": "invalid_request_error", "message": "prompt is too long: 210000 tokens > 200000 maximum"}},
                    400)
            return self._json({"error": {"type": "invalid_request_error", "code": "context_length_exceeded",
                                         "message": "Your input exceeds the context window of this model."}}, 400)
        if self.path.startswith("/v1/messages/count_tokens"):
            return self._json({"input_tokens": 1200})
        return self._anthropic(req) if anthropic else self._openai(req)

    def _anthropic(self, req: dict) -> None:
        tools = {t.get("name") for t in req.get("tools") or []}
        done = sum(1 for m in req.get("messages") or [] if isinstance(m.get("content"), list)
                   for c in m["content"] if isinstance(c, dict) and c.get("type") == "tool_result")
        if done == 0 and {"Write", "write"} & tools:
            name = "Write" if "Write" in tools else "write"
            args = ({"file_path": os.environ.get("CAPTURE_DIR", ".") + "/hello.txt"} if name == "Write"
                    else {"path": "hello.txt"})
            block = {"type": "tool_use", "id": "toolu_01WriteCapture0001", "name": name,
                     "input": {**args, "content": "hello from the capture\n"}}
        elif done == 1 and {"Bash", "bash"} & tools:
            name = "Bash" if "Bash" in tools else "bash"
            block = {"type": "tool_use", "id": "toolu_01BashCapture0001", "name": name,
                     "input": {"command": "cat hello.txt && ls", **({"description": "Show the file"}
                                                                   if name == "Bash" else {})}}
        else:
            block = {"type": "text", "text": "Wrote hello.txt and listed the directory. Done." if tools
                     else "Capture run"}
        usage = {"input_tokens": 1200 + 300 * done, "output_tokens": 40,
                 "cache_creation_input_tokens": 500 if done == 0 else 0,
                 "cache_read_input_tokens": 0 if done == 0 else 2000}
        stop = "tool_use" if block["type"] == "tool_use" else "end_turn"
        msg = {"id": f"msg_capture{done}", "type": "message", "role": "assistant",
               "model": req.get("model", "claude-sonnet-4-5"), "content": [], "stop_reason": None,
               "stop_sequence": None, "usage": {**usage, "output_tokens": 1}}
        if not req.get("stream"):
            return self._json({**msg, "content": [block], "stop_reason": stop, "usage": usage})
        if block["type"] == "text":
            start, delta = {"type": "text", "text": ""}, {"type": "text_delta", "text": block["text"]}
        else:
            start = {"type": "tool_use", "id": block["id"], "name": block["name"], "input": {}}
            delta = {"type": "input_json_delta", "partial_json": json.dumps(block["input"])}
        self._sse([
            ("message_start", {"type": "message_start", "message": msg}),
            ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": start}),
            ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": delta}),
            ("content_block_stop", {"type": "content_block_stop", "index": 0}),
            ("message_delta", {"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None},
                               "usage": {"output_tokens": usage["output_tokens"]}}),
            ("message_stop", {"type": "message_stop"}),
        ])

    def _openai(self, req: dict) -> None:
        tools = {t.get("name") for t in req.get("tools") or []}
        done = sum(1 for i in req.get("input") or [] if isinstance(i, dict) and i.get("type") in (
            "function_call_output", "custom_tool_call_output"))
        if done == 0 and "exec_command" in tools:
            item = {"type": "function_call", "id": "fc_capture0", "call_id": "call_capture0", "name": "exec_command",
                    "arguments": json.dumps({"cmd": "printf 'hello from the capture\\n' > hello.txt && cat hello.txt"}),
                    "status": "completed"}
        elif done == 1 and "apply_patch" in tools:  # a freeform ("custom") tool
            item = {"type": "custom_tool_call", "id": "ctc_capture1", "call_id": "call_capture1", "name": "apply_patch",
                    "input": "*** Begin Patch\n*** Add File: notes.md\n+# Notes\n*** End Patch\n",
                    "status": "completed"}
        else:
            item = {"type": "message", "id": f"msg_capture{done}", "role": "assistant", "status": "completed",
                    "content": [{"type": "output_text", "text": "Created hello.txt and notes.md. Done.",
                                 "annotations": []}]}
        usage = {"input_tokens": 3000 + 400 * done, "input_tokens_details": {"cached_tokens": 2048 * done},
                 "output_tokens": 60, "output_tokens_details": {"reasoning_tokens": 20},
                 "total_tokens": 3060 + 400 * done}
        resp = {"id": f"resp_capture{done}", "object": "response", "status": "in_progress", "model": req.get("model"),
                "output": []}
        self._sse([
            ("response.created", {"type": "response.created", "response": resp}),
            ("response.output_item.added", {"type": "response.output_item.added", "output_index": 0,
                                            "item": {**item, "status": "in_progress"}}),
            ("response.output_item.done", {"type": "response.output_item.done", "output_index": 0, "item": item}),
            ("response.completed", {"type": "response.completed", "response": {
                **resp, "status": "completed", "output": [item], "usage": usage}}),
        ])


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), Model).serve_forever()
