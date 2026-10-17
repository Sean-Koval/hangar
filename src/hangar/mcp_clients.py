"""`hangar connect <client>`: add hangar to an MCP client's own config file, next to the servers already there.

Where each client keeps its MCP servers, and the entry it expects (checked against each client; sources in
docs/clients.md):

- claude-code: `~/.claude.json` (or `$CLAUDE_CONFIG_DIR/.claude.json`), top-level `mcpServers` (user scope):
  `{"type": "http", "url", "headers"}`, as `claude mcp add --scope user --transport http` writes it.
- codex: `~/.codex/config.toml` (or `$CODEX_HOME/config.toml`), `[mcp_servers.<name>]` with `url`, `http_headers`,
  `startup_timeout_sec` and `tool_timeout_sec`.
- cursor: `~/.cursor/mcp.json`, `mcpServers`: `{"url", "headers"}`.
- vscode: `mcp.json` in the VS Code user folder (next to the user `settings.json`), `servers`:
  `{"type": "http", "url", "headers"}`.
- gemini: `~/.gemini/settings.json` (or `$GEMINI_CLI_HOME/.gemini/settings.json`), `mcpServers`:
  `{"url", "type": "http", "headers"}`, as `gemini mcp add --scope user --transport http` writes it.
- claude-desktop: `claude_desktop_config.json`, whose `mcpServers` only launch local (stdio) servers, so the entry
  runs the `mcp-remote` bridge with the token in a 0600 header file rather than on the command line.

Files are rewritten atomically and only the one server entry changes. A file that isn't plain JSON (comments) or
whose TOML can't be edited safely is left alone, and the entry is printed for you to paste.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

MCP_REMOTE = "mcp-remote@0.14.3"  # pinned: --header-file arrived in 0.14
TOOL_TIMEOUT_SEC = 900  # builds and forks take minutes
NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class ClientConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Client:
    name: str
    title: str
    binary: str | None
    oauth: bool  # signs in to remote MCP servers itself (OAuth discovery + dynamic registration)
    login: str  # what to do after writing an OAuth entry, or how to load the new config
    restart: str


CLIENTS: dict[str, Client] = {c.name: c for c in (
    Client("claude-code", "Claude Code", "claude", True, "run `claude mcp login {name}` (or /mcp in Claude Code)",
           "Start a new Claude Code session; `claude mcp get {name}` shows the connection."),
    Client("codex", "Codex", "codex", True, "run `codex mcp login {name}`",
           "Start a new Codex session; `codex mcp get {name}` shows the entry."),
    # overnight/sweep: Cursor's redirects (cursor://anysphere.cursor-mcp/..., http://localhost:8787/callback) are
    # allowed by default; connect_client warns when a changed HANGAR_OAUTH_REDIRECT_HOSTS leaves them out
    Client("cursor", "Cursor", "cursor", True, "open Cursor Settings → MCP and sign in to {name}",
           "Cursor picks it up in Settings → MCP (toggle the server if it was already listed)."),
    Client("vscode", "VS Code", "code", False, "",
           "VS Code picks it up from its user mcp.json; start the server from the MCP: List Servers command."),
    Client("gemini", "Gemini CLI", "gemini", True, "run `/mcp auth {name}` in Gemini CLI",
           "Start a new Gemini CLI session; /mcp lists the server."),
    Client("claude-desktop", "Claude Desktop", None, False, "",
           "Quit and reopen Claude Desktop. The entry runs `npx {bridge}`, so Node.js must be installed."),
)}


# --- #140: clients that drive sandboxes themselves get the hangar-sandbox server too by default
SANDBOX_BY_DEFAULT = frozenset({"claude-code", "codex"})


def get(name: str) -> Client:
    if name not in CLIENTS:
        raise ClientConfigError(f"unknown client '{name}'; choose from {', '.join(CLIENTS)}")
    return CLIENTS[name]


def _appdata() -> Path:
    return Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")


def config_path(client: str) -> Path:
    home = Path.home()
    if client == "claude-code":
        return Path(os.environ.get("CLAUDE_CONFIG_DIR") or home) / ".claude.json"
    if client == "codex":
        return Path(os.environ.get("CODEX_HOME") or home / ".codex") / "config.toml"
    if client == "cursor":
        return home / ".cursor" / "mcp.json"
    if client == "gemini":
        return Path(os.environ.get("GEMINI_CLI_HOME") or home) / ".gemini" / "settings.json"
    if client == "vscode":
        if sys.platform == "win32":
            return _appdata() / "Code" / "User" / "mcp.json"
        if sys.platform == "darwin":
            return home / "Library" / "Application Support" / "Code" / "User" / "mcp.json"
        return Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config") / "Code" / "User" / "mcp.json"
    if client == "claude-desktop":
        if sys.platform == "win32":
            return _appdata() / "Claude" / "claude_desktop_config.json"
        if sys.platform == "darwin":
            return home / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
        raise ClientConfigError("Claude Desktop runs on macOS and Windows; pass --config with its config file")
    raise ClientConfigError(f"unknown client '{client}'")


def installed(client: str) -> bool:
    c = get(client)
    if c.binary and shutil.which(c.binary):
        return True
    try:
        return config_path(client).parent.exists() and (client != "claude-code" or config_path(client).exists())
    except ClientConfigError:
        return False


def _container(client: str) -> str:
    return "servers" if client == "vscode" else "mcpServers"


def entry(client: str, url: str, token: str | None, header_file: Path | None = None) -> dict[str, Any]:
    """The server entry for `client`. No token = OAuth: the client signs in itself."""
    headers = {"Authorization": f"Bearer {token}"} if token else None
    if client == "codex":
        e: dict[str, Any] = {"url": url}
        if headers:
            e["http_headers"] = headers
        return {**e, "startup_timeout_sec": 20, "tool_timeout_sec": TOOL_TIMEOUT_SEC}
    if client == "claude-desktop":
        if not token:
            raise ClientConfigError("Claude Desktop signs in to remote servers only as a custom connector "
                                    "(Settings → Connectors); `hangar connect claude-desktop` uses a token")
        u = urlparse(url)
        extra = [] if u.scheme == "https" or u.hostname in ("localhost", "127.0.0.1") else ["--allow-http"]
        if header_file is not None:
            return {"command": "npx", "args": ["-y", MCP_REMOTE, url, "--header-file", str(header_file), *extra]}
        # Printed form: the README's workaround for clients that mangle spaces in args.
        return {"command": "npx", "args": ["-y", MCP_REMOTE, url, "--header", "Authorization:${AUTH_HEADER}", *extra],
                "env": {"AUTH_HEADER": f"Bearer {token}"}}
    if client == "cursor":
        return {"url": url, **({"headers": headers} if headers else {})}
    if client == "gemini":
        return {"url": url, "type": "http", **({"headers": headers} if headers else {})}
    return {"type": "http", "url": url, **({"headers": headers} if headers else {})}  # claude-code, vscode


def render(client: str, name: str, e: dict[str, Any]) -> str:
    """What the entry looks like in the client's file (for --print and for pasting by hand)."""
    if client == "codex":
        return _toml_table(name, e)
    return json.dumps({_container(client): {name: e}}, indent=2)


def _toml_value(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int | float):
        return str(v)
    if isinstance(v, str):
        return json.dumps(v)  # a JSON string is a valid TOML basic string
    if isinstance(v, dict):
        return "{ " + ", ".join(f"{_toml_key(k)} = {_toml_value(x)}" for k, x in v.items()) + " }"
    if isinstance(v, list):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    raise TypeError(type(v))


def _toml_key(k: str) -> str:
    return k if re.fullmatch(r"[A-Za-z0-9_-]+", k) else json.dumps(k)


def _toml_table(name: str, e: dict[str, Any]) -> str:
    return f"[mcp_servers.{name}]\n" + "".join(f"{_toml_key(k)} = {_toml_value(v)}\n" for k, v in e.items())


def current(client: str, path: Path, name: str) -> dict[str, Any] | None:
    """The entry `name` in the client's file, or None. Raises when the file can't be read."""
    if not path.exists() or not path.read_text().strip():
        return None
    data = _load(client, path)
    if client == "codex":
        return (data.get("mcp_servers") or {}).get(name)
    box = data.get(_container(client))
    return box.get(name) if isinstance(box, dict) else None


def _load(client: str, path: Path) -> dict[str, Any]:
    text = path.read_text()
    try:
        data = tomllib.loads(text) if client == "codex" else json.loads(text)
    except (ValueError, tomllib.TOMLDecodeError) as e:
        kind = "TOML" if client == "codex" else "plain JSON (comments or trailing commas?)"
        raise ClientConfigError(f"{path} isn't valid {kind}: {e}") from e
    if not isinstance(data, dict):
        raise ClientConfigError(f"{path} doesn't hold a JSON object")
    return data


def write(client: str, path: Path, name: str, e: dict[str, Any]) -> str:
    """Add or replace server `name` in the client's config; every other key is kept. Returns added|updated|unchanged."""
    if not NAME_RE.match(name):
        raise ClientConfigError(f"server name '{name}' must be letters, digits, '-' or '_'")
    path = path.resolve() if path.is_symlink() else path  # keep a dotfiles symlink pointing where it did
    text = path.read_text() if path.exists() else ""
    before = current(client, path, name) if text.strip() else None
    if before == e:
        return "unchanged"
    if client == "codex":
        new = _toml_replace(path, text, name, e)
    else:
        data = _load(client, path) if text.strip() else {}
        box = data.setdefault(_container(client), {})
        if not isinstance(box, dict):
            raise ClientConfigError(f"{path}: '{_container(client)}' isn't an object")
        box[name] = e
        new = json.dumps(data, indent=2) + "\n"
    write_private(path, new)
    return "updated" if before is not None else "added"


_HEADER = re.compile(r"^\s*\[")


def _toml_replace(path: Path, text: str, name: str, e: dict[str, Any]) -> str:
    """Drop `[mcp_servers.<name>]` (and its sub-tables) and append the new table. The result is parsed back and
    compared with the original, so nothing else in the file can change unnoticed."""
    own = re.compile(r"^\s*\[\s*mcp_servers\s*\.\s*(?:" + re.escape(name) + r'|"' + re.escape(name)
                     + r'")\s*(?:\.[^\]]*)?\]\s*(?:#.*)?$')
    out, skipping = [], False
    for line in text.splitlines(keepends=True):
        if _HEADER.match(line):
            skipping = bool(own.match(line))
        if not skipping:
            out.append(line)
    body = "".join(out).rstrip("\n")
    new = (body + "\n\n" if body else "") + _toml_table(name, e)
    try:
        old_data, new_data = tomllib.loads(text) if text.strip() else {}, tomllib.loads(new)
    except tomllib.TOMLDecodeError as err:
        raise ClientConfigError(f"{path}: couldn't edit it safely ({err})") from err
    (old_data.get("mcp_servers") or {}).pop(name, None)
    got = (new_data.get("mcp_servers") or {}).pop(name, None)
    if not old_data.get("mcp_servers"):
        old_data.pop("mcp_servers", None)
    if not new_data.get("mcp_servers"):
        new_data.pop("mcp_servers", None)
    if got != e or old_data != new_data:
        raise ClientConfigError(f"{path}: couldn't edit it safely ('{name}' is defined in an inline or dotted form)")
    return new


def write_private(path: Path, text: str) -> None:
    """Atomic replace; a new file is 0600, an existing one keeps its owner's bits but loses group and other access,
    since it now holds a bearer token (clients often create these files 0644)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode & 0o700 if path.exists() else 0o600
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
