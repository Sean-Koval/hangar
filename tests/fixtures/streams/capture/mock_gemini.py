"""A local HTTPS-intercepting proxy that plays a scripted Gemini API (streamGenerateContent, alt=sse).

Only for capturing the real agy CLI's stream-json output offline (capture.sh): agy trusts a throwaway CA through
SSL_CERT_FILE and sends its model requests here through HTTPS_PROXY. Nothing reaches Google.
Usage: python3 mock_gemini.py PORT CERT KEY   (CAPTURE_DIR = the agent's working directory)
"""

import json
import os
import socket
import ssl
import sys
import threading
from http.server import BaseHTTPRequestHandler


class Gemini(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_POST(self):
        try:
            req = json.loads(self.rfile.read(int(self.headers.get("content-length") or 0)) or b"{}")
        except ValueError:  # telemetry and other non-model calls
            self.send_response(404)
            self.send_header("content-length", "0")
            self.end_headers()
            return
        decls = {d.get("name") for t in req.get("tools") or [] for d in t.get("functionDeclarations") or []}
        done = sum(1 for c in req.get("contents") or [] for p in c.get("parts") or [] if "functionResponse" in p)
        cwd = os.environ.get("CAPTURE_DIR", ".")
        # agy validates arguments against its own schemas: these are the fields its tools require (agy 1.3.3).
        if done == 0 and "write_to_file" in decls:
            part = {"functionCall": {"name": "write_to_file", "args": {
                "TargetFile": f"{cwd}/hello.txt", "CodeContent": "hello from the capture\n", "Overwrite": False,
                "Description": "Create hello.txt", "toolSummary": "Creating hello.txt", "toolAction": "Create file"}}}
        elif done == 1 and "run_command" in decls:
            part = {"functionCall": {"name": "run_command", "args": {
                "CommandLine": "cat hello.txt && ls", "Cwd": cwd, "WaitMsBeforeAsync": 2000,
                "toolSummary": "Showing the file", "toolAction": "Run command"}}}
        else:
            part = {"text": "Wrote hello.txt and listed the directory. Done."}
        chunk = {"candidates": [{"content": {"role": "model", "parts": [part]}, "finishReason": "STOP", "index": 0}],
                 "usageMetadata": {"promptTokenCount": 5000 + 600 * done, "candidatesTokenCount": 50,
                                   "totalTokenCount": 5080 + 600 * done, "thoughtsTokenCount": 30,
                                   "cachedContentTokenCount": 4096 if done else 0},
                 "modelVersion": "gemini-3.1-pro-preview", "responseId": f"capture{done}"}
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("connection", "close")
        self.close_connection = True
        self.end_headers()
        self.wfile.write(f"data: {json.dumps(chunk)}\r\n\r\n".encode())
        self.wfile.flush()


def tunnel(conn: socket.socket, ctx: ssl.SSLContext) -> None:
    """Answer CONNECT, then speak TLS (with our certificate) and HTTP on the tunnel."""
    try:
        f = conn.makefile("rb")
        line = f.readline()
        while f.readline() not in (b"\r\n", b"\n", b""):
            pass
        if line.startswith(b"CONNECT"):
            conn.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
            Gemini(ctx.wrap_socket(conn, server_side=True), ("127.0.0.1", 0), None)
    except (OSError, ssl.SSLError) as e:
        print("tunnel:", e, file=sys.stderr)
    finally:
        conn.close()


if __name__ == "__main__":
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(sys.argv[2], sys.argv[3])
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", int(sys.argv[1])))
    srv.listen(16)
    while True:
        c, _ = srv.accept()
        threading.Thread(target=tunnel, args=(c, ctx), daemon=True).start()
