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

import httpx
import typer
import yaml

app = typer.Typer(no_args_is_help=True, add_completion=False, help="Fleet manager for coding-agent sandboxes.")
env_app = typer.Typer(no_args_is_help=True, help="Environment templates.")
sb_app = typer.Typer(no_args_is_help=True, help="Sandboxes.")
cred_app = typer.Typer(no_args_is_help=True, help="Credentials (API keys and subscription logins).")
token_app = typer.Typer(no_args_is_help=True, help="API tokens for MCP clients and the dashboard.")
pack_app = typer.Typer(no_args_is_help=True, help="Harness packs: agent CLIs shipped as read-only images.")
job_app = typer.Typer(no_args_is_help=True, help="Jobs: hand work to cloud agents (branch + PR when done).")
app.add_typer(env_app, name="env")
app.add_typer(sb_app, name="sb")
app.add_typer(cred_app, name="cred")
app.add_typer(token_app, name="token")
app.add_typer(pack_app, name="pack")
app.add_typer(job_app, name="job")


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
    host: str | None = typer.Option(None, help="Bind address (default from HANGAR_HOST, 127.0.0.1)."),
    port: int | None = typer.Option(None, help="Port (default 8787)."),
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


# ---------------------------------------------------------------------------- tokens
@token_app.command("create")
def token_create(name: str = typer.Argument(..., help="Label, e.g. 'codex-laptop'."),
                 scope: list[str] = typer.Option(["admin"], "--scope",
                                                 help="admin (everything) or jobs (only /api/jobs, for services). "
                                                      "Repeatable."),
                 callback_secret: str | None = typer.Option(
                     None, "--callback-secret",
                     help="jobs tokens: 'webhook' credential that signs this token's job callbacks.")):
    """Mint an API token (shown once).

    Uses the server's REST API when HANGAR_URL is set, otherwise the local state store.
    """
    if os.environ.get("HANGAR_URL"):
        res = _call("POST", "/api/tokens", json={"name": name, "scopes": scope,
                                                **({"callback_secret": callback_secret} if callback_secret else {})})
        typer.secho(f"token {res['id']} ({res['name']}):", fg="green")
        typer.echo(res["token"])
        return
    from hangar.app import Hangar
    from hangar.auth import SCOPES

    if bad := [x for x in scope if x not in SCOPES]:
        typer.secho(f"unknown scopes {bad}; choose from {list(SCOPES)}", fg="red", err=True)
        raise typer.Exit(2)
    try:
        raw, tok = Hangar().auth.create(name, scope, callback_secret=callback_secret)
    except ValueError as e:
        typer.secho(str(e), fg="red", err=True)
        raise typer.Exit(2) from e
    typer.secho(f"token {tok.id} ({name}):", fg="green")
    typer.echo(raw)


@token_app.command("list")
def token_list():
    """List API tokens (server REST API when HANGAR_URL is set, otherwise the local state store)."""
    if os.environ.get("HANGAR_URL"):
        for t in _call("GET", "/api/tokens"):
            typer.echo(f"{t['id']}  {t['name']:24}  last_used={t['last_used']}")
        return
    from hangar.app import Hangar

    for t in Hangar().auth.list():
        typer.echo(f"{t.id}  {t.name:24}  last_used={t.last_used}")


@token_app.command("revoke")
def token_revoke(token_id: str):
    """Revoke an API token (server REST API when HANGAR_URL is set, otherwise the local state store)."""
    if os.environ.get("HANGAR_URL"):
        _call("DELETE", f"/api/tokens/{token_id}")
    else:
        from hangar.app import Hangar

        Hangar().auth.revoke(token_id)
    typer.echo(f"revoked {token_id}")


# ---------------------------------------------------------------------------- environments
@env_app.command("list")
def env_list(as_json: bool = typer.Option(False, "--json", help="Print the raw list as JSON.")):
    envs = _call("GET", "/api/environments")
    if as_json:
        _print(envs)
        return
    for e in envs:
        imgs = ", ".join(f"{k}:{v.split(':')[-1]}" for k, v in (e.get("images") or {}).items()) or "not built"
        typer.echo(f"{e['name']:22} {', '.join(e.get('harnesses') or []):34} {imgs}")


@env_app.command("render")
def env_render(name: str, env_dir: Path = typer.Option(Path("envs"), "--dir")):
    """Print the Dockerfile an environment renders to (local, no server needed)."""
    from hangar.envspec import EnvCatalog, render

    spec = EnvCatalog([env_dir]).get(name)
    typer.echo(render(spec).dockerfile)


@env_app.command("build")
def env_build(name: str, provider: str | None = None, force: bool = False):
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
    provider: str | None = typer.Option(None, help="Provider the image is for (default: server default)."),
    platform: str = typer.Option("linux/amd64", help="Target platform; GKE nodes are amd64 unless you use Arm pools."),
):
    """Build an environment on THIS machine, push it, and register it with the server.

    Use this when the control plane runs in a cluster without a builder: the server renders
    the build context, your docker builds and pushes it, and the server syncs its warm pool.
    """
    import shutil

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
    _build_and_push(tag, tar, platform)
    res = _call("POST", f"/api/environments/{name}/images", json={"image": tag, "provider": provider})
    typer.secho(f"registered {res['image']} for {res['provider']}", fg="green")


def _build_and_push(tag: str, tar: bytes, platform: str) -> None:
    import subprocess

    typer.secho(f"building {tag}", fg="cyan")
    has_buildx = subprocess.run(["docker", "buildx", "version"], capture_output=True).returncode == 0
    if has_buildx:
        subprocess.run(["docker", "buildx", "build", "--platform", platform, "-t", tag, "--push", "-"], input=tar,
                       check=True)
    else:  # docker-ce without the buildx plugin: legacy builder, then push
        subprocess.run(["docker", "build", "--platform", platform, "-t", tag, "-"], input=tar, check=True)
        subprocess.run(["docker", "push", tag], check=True)


@pack_app.command("list")
def pack_list(as_json: bool = typer.Option(False, "--json", help="Print the raw list as JSON.")):
    """List harness packs supported by the server."""
    harnesses = _call("GET", "/api/harnesses")
    if as_json:
        _print(harnesses)
        return
    for h in harnesses:
        agent = " agent" if h.get("agent") else ""
        dep = " (deprecated)" if h.get("deprecated") else ""
        typer.echo(f"{h['name']:16} {h.get('version') or 'latest':12}{agent}{dep}".rstrip())


@pack_app.command("render")
def pack_render(harness: str):
    """Print a harness pack's Dockerfile and install script (local, no server needed)."""
    from hangar.harnesses import get_harness, pack_dockerfile, pack_script

    h = get_harness(harness)
    typer.echo(pack_dockerfile(h) + "\n# packs/" + h.name + ".sh\n" + pack_script(h))


@pack_app.command("build")
def pack_build(harness: list[str], provider: str | None = None, force: bool = False):
    """Build harness packs on the server's builder."""
    for h in harness:
        _print(_call("POST", f"/api/harnesses/{h}/pack", json={"provider": provider, "force": force}))


@pack_app.command("publish")
def pack_publish(
    harness: list[str],
    provider: str | None = typer.Option(None, help="Provider the pack is for (default: server default)."),
    platform: str = typer.Option("linux/amd64", help="Target platform."),
):
    """Build harness packs on THIS machine, push them, and register them with the server."""
    import shutil

    if not shutil.which("docker"):
        typer.secho("docker is required for publish", fg="red")
        raise typer.Exit(2)
    for h in harness:
        with _client() as c:
            r = c.get(f"/api/harnesses/{h}/context", params={"provider": provider})
            if r.status_code >= 400:
                typer.secho(f"error: {r.text}", fg="red")
                raise typer.Exit(1)
            tag, tar = r.headers["X-Hangar-Image"], r.content
        _build_and_push(tag, tar, platform)
        res = _call("POST", f"/api/harnesses/{h}/images", json={"image": tag, "provider": provider})
        typer.secho(f"registered {res['image']}", fg="green")


@env_app.command("push")
def env_push(file: Path):
    """Create/update an environment on the server from a YAML file."""
    data = yaml.safe_load(file.read_text())
    _print(_call("PUT", f"/api/environments/{data.get('name') or file.stem}", content=file.read_text()))


# ---------------------------------------------------------------------------- sandboxes
@sb_app.command("ls")
def sb_ls(
    all: bool = typer.Option(False, "--all", "-a"),
    as_json: bool = typer.Option(False, "--json", help="Print the raw list as JSON."),
):
    sandboxes = _call("GET", "/api/sandboxes", params={"include_stopped": all})
    if as_json:
        _print(sandboxes)
        return
    for s in sandboxes:
        typer.echo(f"{s['id']:12} {s['name']:28} {s['state']:9} {s['environment']:18} {s['provider']}")


@sb_app.command("create")
def sb_create(
    environment: str,
    name: str | None = None,
    repo: str | None = None,
    branch: str | None = None,
    work_branch: str | None = None,
    cred: list[str] = typer.Option(None, "--cred", "-c", help="Credential name (repeatable)."),
    provider: str | None = None,
    ttl: int | None = typer.Option(None, help="Minutes until auto-destroy."),
    count: int = 1,
    harness: str | None = typer.Option(None, help="Which of the environment's agents (default: its default)."),
):
    body = {"environment": environment, "name": name, "repo": repo, "branch": branch, "work_branch": work_branch,
            "credentials": cred or None, "provider": provider, "ttl_minutes": ttl, "count": count,
            "harness": harness}
    for s in _call("POST", "/api/sandboxes", json=body):
        typer.echo(f"{s.get('id', 'ERR'):12} {s.get('name') or s.get('error')}")


@sb_app.command("exec")
def sb_exec(sandbox: str, command: list[str], timeout: int = 120):
    r = _call("POST", f"/api/sandboxes/{sandbox}/exec", json={"command": " ".join(command), "timeout": timeout})
    sys.stdout.write(r["stdout"])
    sys.stderr.write(r["stderr"])
    raise typer.Exit(r["exit_code"])


@sb_app.command("agent")
def sb_agent(sandbox: str, prompt: str, harness: str | None = None, model: str | None = None):
    _print(_call("POST", f"/api/sandboxes/{sandbox}/agent", json={"prompt": prompt, "harness": harness,
                                                                  "model": model}))


@sb_app.command("fork")
def sb_fork(sandbox: str, count: int = 1, work_branch_prefix: str | None = None):
    for s in _call("POST", f"/api/sandboxes/{sandbox}/fork",
                   json={"count": count, "work_branch_prefix": work_branch_prefix}):
        typer.echo(f"{s['id']:12} {s['name']}")


@sb_app.command("snapshot")
def sb_snapshot(sandbox: str, label: str | None = None, kind: str = "auto"):
    _print(_call("POST", f"/api/sandboxes/{sandbox}/snapshot", json={"label": label, "kind": kind}))


@sb_app.command("rm")
def sb_rm(sandboxes: list[str]):
    for s in sandboxes:
        r = _call("DELETE", f"/api/sandboxes/{s}")
        typer.echo(f"destroyed {r['id']} ({r['name']})")


@app.command()
def gc(yes: bool = typer.Option(False, "--yes", help="Remove what was found (default: just list).")):
    """List (or remove with --yes) leftover containers no active sandbox owns."""
    found = _call("POST", "/api/gc", params={"delete": yes})
    if not found:
        typer.echo("nothing to clean up")
        return
    for o in found:
        typer.echo(f"{'removed' if yes else 'orphan '}  {o['provider']:10} {o['name']:40} {o['status']}")
    if not yes:
        typer.secho("re-run with --yes to remove (check first if other hangar instances share this engine)",
                    fg="yellow")


# ---------------------------------------------------------------------------- credentials
@cred_app.command("ls")
def cred_ls():
    for c in _call("GET", "/api/credentials"):
        lease = f" leased_to={c['leased_to']}" if c.get("leased_to") else ""
        typer.echo(f"{c['name']:24} {c['harness']:12} {c['kind']:12}{' exclusive' if c['exclusive'] else ''}{lease}")


@cred_app.command("add")
def cred_add(
    name: str,
    harness: str = typer.Option(..., help="claude-code | codex | pi | antigravity | git (or any label)"),
    kind: str = typer.Option(..., help="api_key | oauth_token | auth_file | git_token | ssh_key | generic"),
    value: str | None = typer.Option(None, help="Secret value; omit to read from --file or stdin."),
    file: Path | None = typer.Option(None, help="Read the secret from a file (e.g. ~/.codex/auth.json)."),
    env_var: str | None = typer.Option(None, "--env-var", help="Env var to inject as, overriding the harness "
                                       "default (e.g. OPENAI_API_KEY for a pi credential)."),
    description: str | None = None,
):
    """Store a credential. Examples:

      claude setup-token | hangar cred add claude-max --harness claude-code --kind oauth_token

      hangar cred add codex-chatgpt --harness codex --kind auth_file --file ~/.codex/auth.json

      hangar cred add openrouter --harness pi --kind api_key --file ~/.hangar/openrouter-key

      hangar cred add gcp-vertex --harness antigravity --kind generic --file sa-key.json
    """
    secret = value if value is not None else (file.expanduser().read_text() if file else sys.stdin.read())
    if kind == "oauth_token" and harness == "claude-code":
        import re

        m = re.findall(r"sk-ant-oat[0-9A-Za-z_\-]{20,}", secret)
        secret = m[-1] if m else secret.strip()
    c = _call("POST", "/api/credentials", json={"name": name, "harness": harness, "kind": kind, "value": secret,
                                                "description": description, "env_var": env_var})
    typer.secho(f"stored {c['name']} ({c['harness']}/{c['kind']}{', exclusive' if c['exclusive'] else ''})",
                fg="green")


@cred_app.command("rm")
def cred_rm(name: str):
    _call("DELETE", f"/api/credentials/{name}")
    typer.echo(f"deleted {name}")


# ---------------------------------------------------------------------------- jobs
_JOB_DONE = {"succeeded", "failed", "cancelled"}


def _job_line(j: dict) -> str:
    pr = (j.get("result") or {}).get("pr_url") or ""
    reason = f" ({j['reason']})" if j.get("reason") else ""
    return f"{j['id']}  {j['state']:12}{reason:16} {j['title'][:60]:60} {pr}"


def _wait_job(job_id: str) -> dict:
    import time

    seen = 0
    while True:
        j = _call("GET", f"/api/jobs/{job_id}")
        for ev in j["events"][seen:]:
            typer.echo(f"  [{ev['state']}] {ev['message']}")
        seen = len(j["events"])
        if j["state"] in _JOB_DONE:
            return j
        time.sleep(5)


@job_app.command("submit")
def job_submit(
    instructions: str = typer.Argument(None, help="What to do. Omit to read from --file or stdin."),
    repo: str | None = typer.Option(None, help="Git URL (default: the environment's repo)."),
    env: str | None = typer.Option(None, "--env", "-e", help="Environment (default: the repo's mapped one)."),
    title: str | None = None,
    harness: str | None = None,
    model: str | None = None,
    base: str | None = typer.Option(None, help="Base branch (default: the repo's default branch)."),
    verify: str | None = typer.Option(None, help="The repo's own check, e.g. 'pytest -q'."),
    deliver: str = typer.Option("draft_pr", help="draft_pr | pr | branch | none"),
    context: list[Path] = typer.Option([], "--context", "-c", help="File to hand the agent (repeatable)."),
    link: list[str] = typer.Option([], "--link", help="URL the agent may read (repeatable)."),
    file: Path | None = typer.Option(None, "--file", "-f", help="Read instructions from a file."),
    timeout: int | None = typer.Option(None, help="Minutes per agent turn."),
    wait: bool = typer.Option(False, "--wait", "-w", help="Follow the job until it finishes."),
):
    """Submit a job. Example:

      hangar job submit "fix the unicode bug in CSV export" --repo https://github.com/me/app \\
          --env python -c trace.txt --verify "pytest -q" --wait
    """
    import base64 as b64

    text = instructions or (file.expanduser().read_text() if file else sys.stdin.read())
    items = []
    for pth in context:
        data = pth.expanduser().read_bytes()
        try:
            items.append({"name": pth.name, "kind": "file", "content": data.decode()})
        except UnicodeDecodeError:
            items.append({"name": pth.name, "kind": "file", "content": b64.b64encode(data).decode(),
                          "encoding": "base64"})
    items += [{"name": f"link-{i + 1}", "kind": "url", "url": u} for i, u in enumerate(link)]
    j = _call("POST", "/api/jobs", json={
        "instructions": text, "repo": repo, "environment": env, "title": title, "harness": harness, "model": model,
        "base_branch": base, "verify": verify, "deliver": deliver, "context": items, "timeout_minutes": timeout})
    typer.secho(f"{j['id']} queued: {j['title']}", fg="green")
    if wait:
        j = _wait_job(j["id"])
        typer.echo(_job_line(j))
        if (j.get("result") or {}).get("summary"):
            typer.echo("\n" + j["result"]["summary"])
        raise typer.Exit(0 if j["state"] == "succeeded" else 1)


@job_app.command("list")
def job_list(state: str | None = None, limit: int = 20, as_json: bool = typer.Option(False, "--json")):
    jobs = _call("GET", "/api/jobs", params={"state": state, "limit": limit} if state else {"limit": limit})
    if as_json:
        return _print(jobs)
    for j in jobs:
        typer.echo(_job_line(j))


@job_app.command("get")
def job_get(job_id: str, wait: bool = typer.Option(False, "--wait", "-w")):
    _print(_wait_job(job_id) if wait else _call("GET", f"/api/jobs/{job_id}"))


@job_app.command("follow-up")
def job_follow_up(job_id: str, message: str, wait: bool = typer.Option(False, "--wait", "-w")):
    """Continue a finished job in the same sandbox and branch."""
    j = _call("POST", f"/api/jobs/{job_id}/follow-up", json={"message": message})
    typer.secho(f"{j['id']} {j['state']}", fg="green")
    if wait:
        typer.echo(_job_line(_wait_job(job_id)))


@job_app.command("cancel")
def job_cancel(job_id: str):
    j = _call("POST", f"/api/jobs/{job_id}/cancel")
    typer.echo(f"{j['id']} {j['state']}")


if __name__ == "__main__":
    app()
