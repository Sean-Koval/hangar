"""hangar CLI.

Local commands (`serve`, `token create`, `env render`) act on this machine's state.
Fleet commands talk to a running server over its REST API, so the same CLI drives a
laptop instance or a GKE deployment: set HANGAR_URL and HANGAR_TOKEN.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Optional

import httpx
import typer
import yaml

app = typer.Typer(no_args_is_help=True, add_completion=False, help="Fleet manager for coding-agent sandboxes.")
env_app = typer.Typer(no_args_is_help=True, help="Environment templates.")
sb_app = typer.Typer(no_args_is_help=True, help="Sandboxes.")
cred_app = typer.Typer(no_args_is_help=True, help="Credentials (API keys and subscription logins).")
token_app = typer.Typer(no_args_is_help=True, help="API tokens for MCP clients and the dashboard.")
app.add_typer(env_app, name="env")
app.add_typer(sb_app, name="sb")
app.add_typer(cred_app, name="cred")
app.add_typer(token_app, name="token")


def _client() -> httpx.Client:
    url = os.environ.get("HANGAR_URL", "http://localhost:8787")
    token = os.environ.get("HANGAR_TOKEN")
    if not token:
        boot = Path(os.environ.get("HANGAR_DATA_DIR", Path.home() / ".hangar")) / "bootstrap-token"
        if boot.exists():
            token = boot.read_text().strip()
    if not token:
        typer.secho("HANGAR_TOKEN is not set (and no local bootstrap token found)", fg="red", err=True)
        raise typer.Exit(2)
    return httpx.Client(base_url=url, headers={"Authorization": f"Bearer {token}"}, timeout=httpx.Timeout(1800))


def _call(method: str, path: str, **kw):
    with _client() as c:
        r = c.request(method, path, **kw)
    if r.status_code >= 400:
        try:
            msg = r.json().get("error") or r.json().get("detail")
        except Exception:  # noqa: BLE001
            msg = r.text
        typer.secho(f"error {r.status_code}: {msg}", fg="red", err=True)
        raise typer.Exit(1)
    return r.json()


def _print(obj) -> None:
    typer.echo(json.dumps(obj, indent=2, default=str))


# ---------------------------------------------------------------------------- server
@app.command()
def serve(
    host: Optional[str] = typer.Option(None, help="Bind address (default from HANGAR_HOST, 127.0.0.1)."),
    port: Optional[int] = typer.Option(None, help="Port (default 8787)."),
    reload: bool = typer.Option(False, help="Auto-reload on code changes (dev)."),
):
    """Run the control plane: dashboard at /, REST at /api, MCP at /mcp."""
    import logging

    import uvicorn

    from hangar.config import get_settings

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    s = get_settings()
    uvicorn.run("hangar.app:create_app", factory=True, host=host or s.host, port=port or s.port, reload=reload,
                log_level="info")


@app.command()
def connect():
    """Print config snippets for connecting Codex / Claude Code to this hangar."""
    d = _call("GET", "/api/connect")
    typer.secho("# Codex  (~/.codex/config.toml)", fg="cyan")
    typer.echo(d["codex_config_toml"])
    typer.secho("# Codex CLI", fg="cyan")
    typer.echo(d["codex_cli"] + "\n")
    typer.secho("# Claude Code", fg="cyan")
    typer.echo(d["claude_code_cli"] + "\n")
    typer.secho("Then: export HANGAR_TOKEN=<token from `hangar token create`>", fg="yellow")


# ---------------------------------------------------------------------------- tokens (local)
@token_app.command("create")
def token_create(name: str = typer.Argument(..., help="Label, e.g. 'codex-laptop'.")):
    """Mint an API token directly in the local state store (shown once)."""
    from hangar.app import Hangar

    raw, tok = Hangar().auth.create(name)
    typer.secho(f"token {tok.id} ({name}):", fg="green")
    typer.echo(raw)


@token_app.command("list")
def token_list():
    from hangar.app import Hangar

    for t in Hangar().auth.list():
        typer.echo(f"{t.id}  {t.name:24}  last_used={t.last_used}")


@token_app.command("revoke")
def token_revoke(token_id: str):
    from hangar.app import Hangar

    Hangar().auth.revoke(token_id)
    typer.echo(f"revoked {token_id}")


# ---------------------------------------------------------------------------- environments
@env_app.command("list")
def env_list():
    for e in _call("GET", "/api/environments"):
        imgs = ", ".join(f"{k}:{v.split(':')[-1]}" for k, v in (e.get("images") or {}).items()) or "not built"
        typer.echo(f"{e['name']:22} {', '.join(e.get('harnesses') or []):34} {imgs}")


@env_app.command("render")
def env_render(name: str, env_dir: Path = typer.Option(Path("envs"), "--dir")):
    """Print the Dockerfile an environment renders to (local, no server needed)."""
    from hangar.envspec import EnvCatalog, render

    spec = EnvCatalog([env_dir]).get(name)
    typer.echo(render(spec).dockerfile)


@env_app.command("build")
def env_build(name: str, provider: Optional[str] = None, force: bool = False):
    """Build an environment image (streams the build log)."""
    job = _call("POST", f"/api/environments/{name}/build", json={"provider": provider, "force": force})
    with _client() as c, c.stream("GET", f"/api/builds/{job['id']}/stream") as r:
        event = None
        for line in r.iter_lines():
            if line.startswith("event: "):
                event = line[7:]
            elif line.startswith("data: "):
                data = json.loads(line[6:])
                if event == "done":
                    color = "green" if data["state"] == "succeeded" else "red"
                    typer.secho(f"\n{data['state']}: {data.get('image') or data.get('error')}", fg=color)
                    if data["state"] != "succeeded":
                        raise typer.Exit(1)
                else:
                    typer.echo(data)


@env_app.command("publish")
def env_publish(
    name: str,
    provider: Optional[str] = typer.Option(None, help="Provider the image is for (default: server default)."),
    platform: str = typer.Option("linux/amd64", help="Target platform; GKE nodes are amd64 unless you use Arm pools."),
):
    """Build an environment on THIS machine, push it, and register it with the server.

    Use this when the control plane runs in a cluster without a builder: the server renders
    the build context, your docker builds and pushes it, and the server syncs its warm pool.
    """
    import asyncio
    import shutil
    import subprocess

    if not shutil.which("docker"):
        typer.secho("docker is required for publish", fg="red")
        raise typer.Exit(2)
    with _client() as c:
        r = c.get(f"/api/environments/{name}/context", params={"provider": provider})
        if r.status_code >= 400:
            typer.secho(f"error: {r.text}", fg="red")
            raise typer.Exit(1)
        tag = r.headers["X-Hangar-Image"]
        tar = r.content
    typer.secho(f"building {tag}", fg="cyan")
    subprocess.run(["docker", "buildx", "build", "--platform", platform, "-t", tag, "--push", "-"], input=tar,
                   check=True)
    res = _call("POST", f"/api/environments/{name}/images", json={"image": tag, "provider": provider})
    typer.secho(f"registered {res['image']} for {res['provider']}", fg="green")


@env_app.command("push")
def env_push(file: Path):
    """Create/update an environment on the server from a YAML file."""
    data = yaml.safe_load(file.read_text())
    _print(_call("PUT", f"/api/environments/{data.get('name') or file.stem}", content=file.read_text()))


# ---------------------------------------------------------------------------- sandboxes
@sb_app.command("ls")
def sb_ls(all: bool = typer.Option(False, "--all", "-a")):
    for s in _call("GET", "/api/sandboxes", params={"include_stopped": all}):
        typer.echo(f"{s['id']:12} {s['name']:28} {s['state']:9} {s['environment']:18} {s['provider']}")


@sb_app.command("create")
def sb_create(
    environment: str,
    name: Optional[str] = None,
    repo: Optional[str] = None,
    branch: Optional[str] = None,
    work_branch: Optional[str] = None,
    cred: list[str] = typer.Option(None, "--cred", "-c", help="Credential name (repeatable)."),
    provider: Optional[str] = None,
    ttl: Optional[int] = typer.Option(None, help="Minutes until auto-destroy."),
    count: int = 1,
):
    body = {"environment": environment, "name": name, "repo": repo, "branch": branch, "work_branch": work_branch,
            "credentials": cred or None, "provider": provider, "ttl_minutes": ttl, "count": count}
    for s in _call("POST", "/api/sandboxes", json=body):
        typer.echo(f"{s.get('id', 'ERR'):12} {s.get('name') or s.get('error')}")


@sb_app.command("exec")
def sb_exec(sandbox: str, command: list[str], timeout: int = 120):
    r = _call("POST", f"/api/sandboxes/{sandbox}/exec", json={"command": " ".join(command), "timeout": timeout})
    sys.stdout.write(r["stdout"])
    sys.stderr.write(r["stderr"])
    raise typer.Exit(r["exit_code"])


@sb_app.command("agent")
def sb_agent(sandbox: str, prompt: str, harness: Optional[str] = None):
    _print(_call("POST", f"/api/sandboxes/{sandbox}/agent", json={"prompt": prompt, "harness": harness}))


@sb_app.command("fork")
def sb_fork(sandbox: str, count: int = 1, work_branch_prefix: Optional[str] = None):
    for s in _call("POST", f"/api/sandboxes/{sandbox}/fork",
                   json={"count": count, "work_branch_prefix": work_branch_prefix}):
        typer.echo(f"{s['id']:12} {s['name']}")


@sb_app.command("snapshot")
def sb_snapshot(sandbox: str, label: Optional[str] = None, kind: str = "auto"):
    _print(_call("POST", f"/api/sandboxes/{sandbox}/snapshot", json={"label": label, "kind": kind}))


@sb_app.command("rm")
def sb_rm(sandboxes: list[str]):
    for s in sandboxes:
        r = _call("DELETE", f"/api/sandboxes/{s}")
        typer.echo(f"destroyed {r['id']} ({r['name']})")


# ---------------------------------------------------------------------------- credentials
@cred_app.command("ls")
def cred_ls():
    for c in _call("GET", "/api/credentials"):
        lease = f" leased_to={c['leased_to']}" if c.get("leased_to") else ""
        typer.echo(f"{c['name']:24} {c['harness']:12} {c['kind']:12}{' exclusive' if c['exclusive'] else ''}{lease}")


@cred_app.command("add")
def cred_add(
    name: str,
    harness: str = typer.Option(..., help="claude-code | codex | antigravity | gemini | git"),
    kind: str = typer.Option(..., help="api_key | oauth_token | auth_file | git_token | ssh_key"),
    value: Optional[str] = typer.Option(None, help="Secret value; omit to read from --file or stdin."),
    file: Optional[Path] = typer.Option(None, help="Read the secret from a file (e.g. ~/.codex/auth.json)."),
    description: Optional[str] = None,
):
    """Store a credential. Examples:

      claude setup-token | hangar cred add claude-max --harness claude-code --kind oauth_token

      hangar cred add codex-chatgpt --harness codex --kind auth_file --file ~/.codex/auth.json
    """
    secret = value if value is not None else (file.expanduser().read_text() if file else sys.stdin.read())
    if kind == "oauth_token" and harness == "claude-code":
        import re

        m = re.findall(r"sk-ant-oat[0-9A-Za-z_\-]{20,}", secret)
        secret = m[-1] if m else secret.strip()
    c = _call("POST", "/api/credentials", json={"name": name, "harness": harness, "kind": kind,
                                                "value": secret, "description": description})
    typer.secho(f"stored {c['name']} ({c['harness']}/{c['kind']}{', exclusive' if c['exclusive'] else ''})",
                fg="green")


@cred_app.command("rm")
def cred_rm(name: str):
    _call("DELETE", f"/api/credentials/{name}")
    typer.echo(f"deleted {name}")


if __name__ == "__main__":
    app()
