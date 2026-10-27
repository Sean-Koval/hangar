"""Security headers on the web console and the agent screen (KAN-174, finding L-7).

The console renders agent-controlled text (job titles, summaries, events, branch names, PR titles) and holds the admin
token, so one missed escape must not run script. Every HTML page under `/` and `/ui/` carries a strict
Content-Security-Policy: scripts only from hangar itself and the exact pinned CDN files the pages load (xterm, its fit
addon, noVNC), never inline, never `eval`; no framing; no plugins; no `<base>` rewrites. Every `/ui/` file is
`nosniff`, so an uploaded or mistyped file is never run as script, and the pages send no referrer.

Styles stay looser than scripts: `style-src` allows inline styles because xterm's DOM renderer writes `<style>`
elements and Airframe's StageRail, ring and capsule carry their progress as `style="--p:…"`. CSS can't run script; the
token is never in the DOM for a selector to read.
"""

from __future__ import annotations

import re

from fastapi.staticfiles import StaticFiles
from starlette.types import Scope

# the CDN files the pages load, pinned to the versions index.html and vnc.js name (a path source: nothing else on
# jsdelivr may run). noVNC's rfb.js imports its siblings, so its source is the package directory.
XTERM = "https://cdn.jsdelivr.net/npm/@xterm/xterm@5.5.0/"
XTERM_FIT = "https://cdn.jsdelivr.net/npm/@xterm/addon-fit@0.10.0/lib/addon-fit.min.js"
NOVNC = "https://cdn.jsdelivr.net/npm/@novnc/novnc@1.7.0/"
SCRIPTS = ("'self'", XTERM + "lib/xterm.min.js", XTERM_FIT, NOVNC)
STYLES = ("'self'", "'unsafe-inline'", "https://fonts.googleapis.com", XTERM + "css/xterm.min.css")
FONTS = ("'self'", "https://fonts.gstatic.com")
HOST = re.compile(r"\A[A-Za-z0-9.-]+(:\d{1,5})?\Z|\A\[[0-9A-Fa-f:.]+\](:\d{1,5})?\Z")


def csp(host: str | None = None) -> str:
    """The policy. `connect-src 'self'` covers ws: and wss: to the same host in current browsers (CSP Level 3); the
    request's own host is named too, for the browsers that don't (the terminal, the build log and the agent screen
    are websockets and Server-Sent Events back to hangar)."""
    connect = ["'self'"]
    if host and HOST.match(host):
        connect += [f"ws://{host}", f"wss://{host}"]
    return "; ".join([
        "default-src 'none'",
        "script-src " + " ".join(SCRIPTS),
        "style-src " + " ".join(STYLES),
        "font-src " + " ".join(FONTS),
        "img-src 'self' data:",
        "connect-src " + " ".join(connect),
        "manifest-src 'self'",
        "object-src 'none'",
        "base-uri 'none'",
        "form-action 'self'",
        "frame-ancestors 'none'",
    ])


def _host(scope: Scope) -> str | None:
    for k, v in scope.get("headers") or []:
        if k == b"host":
            return v.decode("latin-1")
    return None


def page_headers(host: str | None) -> dict[str, str]:
    """What an HTML page of the console or the agent screen carries."""
    return {
        "Content-Security-Policy": csp(host),
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
        "X-Frame-Options": "DENY",  # frame-ancestors, for the browsers that predate it
        "Cache-Control": "no-cache",  # a new release's page (and its policy) is fetched, not served from cache
    }


class ConsoleFiles(StaticFiles):
    """`/ui/`: every file `nosniff`; an HTML page (vnc.html) the full set of page headers."""

    async def get_response(self, path: str, scope: Scope):
        response = await super().get_response(path, scope)
        response.headers["X-Content-Type-Options"] = "nosniff"
        if response.headers.get("content-type", "").startswith("text/html"):
            for k, v in page_headers(_host(scope)).items():
                response.headers[k] = v
        return response
