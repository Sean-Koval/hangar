"""Call one hangar MCP tool over Streamable HTTP, the way a remote MCP client would.

    HANGAR_URL=https://hangar.example.com HANGAR_TOKEN=hgr_... \
        uv run python scripts/mcp_call.py create_sandbox '{"environment": "claude-python"}'
    uv run python scripts/mcp_call.py --list          # list the server's tools

HANGAR_TOKEN_FILE can point at a file instead of putting the token in the environment.
"""

import asyncio
import json
import os
import sys
from pathlib import Path

from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client


def _token() -> str:
    if t := os.environ.get("HANGAR_TOKEN"):
        return t
    return Path(os.environ.get("HANGAR_TOKEN_FILE", "~/.hangar/gke-admin-token")).expanduser().read_text().strip()


async def main(argv: list[str]) -> int:
    url = os.environ.get("HANGAR_URL", "http://localhost:8787").rstrip("/") + "/mcp"
    http = create_mcp_http_client(headers={"Authorization": f"Bearer {_token()}"})
    async with http, Client(streamable_http_client(url, http_client=http)) as c:
        if argv[:1] == ["--list"]:
            for t in (await c.list_tools()).tools:
                print(t.name)
            return 0
        res = await c.call_tool(argv[0], json.loads(argv[1]) if len(argv) > 1 else {})
        sc = getattr(res, "structured_content", None) or getattr(res, "structuredContent", None)
        if sc is not None:
            out = sc.get("result", sc) if isinstance(sc, dict) and set(sc) == {"result"} else sc
            print(json.dumps(out, indent=2) if not isinstance(out, str) else out)
        else:
            print("\n".join(getattr(b, "text", str(b)) for b in res.content))
        return 1 if getattr(res, "is_error", False) or getattr(res, "isError", False) else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
