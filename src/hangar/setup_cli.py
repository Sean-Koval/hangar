"""Getting hangar running and connected: `hangar up` / `down`, `hangar connect`, `hangar setup github|slack`,
`hangar config` and `hangar doctor --fix`. cli.py registers these on the main app.

Everything here talks to the server through the same REST client as the rest of the CLI (cli._call), so it works
the same against a laptop hangar and a GKE one, except `up`/`down`, which manage a local server process.
"""

from __future__ import annotations

import json
import os
import re
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import typer

from hangar import mcp_clients
from hangar.mcp_clients import ClientConfigError

setup_app = typer.Typer(no_args_is_help=True, help="Wizards that connect hangar to GitHub and Slack.")
config_app = typer.Typer(no_args_is_help=True, help="Settings stored in hangar's database (survive redeploys).")

_MARKS = {"ok": ("✓", "green"), "warn": ("!", "yellow"), "fail": ("✗", "red")}


def _cli():
    from hangar import cli

    return cli


def _fail(msg: str, code: int = 1) -> typer.Exit:
    typer.secho(msg, fg="red", err=True)
    return typer.Exit(code)


def _host() -> str:
    return re.sub(r"[^A-Za-z0-9-]", "-", socket.gethostname().split(".")[0])[:40] or "host"


def _print_steps(res: dict) -> None:
    for s in res.get("steps") or []:
        mark, color = _MARKS.get(s["status"], ("?", None))
        typer.secho(f"{mark} {s['step']}: {s['detail']}", fg=color)


# ============================================================================ hangar connect
def _entry_target(client: str, e: dict) -> tuple[str | None, str]:
    """(url, auth) of an existing entry, for --list."""
    if client == "claude-desktop":
        args = e.get("args") or []
        url = next((a for a in args if isinstance(a, str) and a.startswith("http")), None)
        return url, "token" if ("--header-file" in args or "--header" in args) else "oauth"
    has = e.get("headers") or e.get("http_headers") or e.get("bearer_token_env_var")
    return e.get("url"), "token" if has else "oauth"


def list_clients() -> None:
    typer.echo(f"{'client':16} {'installed':10} {'config file':52} hangar")
    for name in mcp_clients.CLIENTS:
        try:
            path = mcp_clients.config_path(name)
        except ClientConfigError:
            typer.echo(f"{name:16} {'-':10} {'(not on this OS)':52} -")
            continue
        try:
            e = mcp_clients.current(name, path, "hangar")
            state = "-" if e is None else "{} ({})".format(*_entry_target(name, e))
        except (ClientConfigError, OSError) as err:
            state = f"unreadable: {err}"[:60]
        shown = str(path).replace(str(Path.home()), "~", 1)
        typer.echo(f"{name:16} {'yes' if mcp_clients.installed(name) else 'no':10} {shown:52} {state}")


def connect_client(client: str, auth: str = "token", name: str = "hangar", config: Path | None = None,
                   print_only: bool = False) -> None:
    cli = _cli()
    try:
        c = mcp_clients.get(client)
        path = config.expanduser() if config else mcp_clients.config_path(client)
    except ClientConfigError as e:
        raise _fail(str(e), 2) from e
    if not mcp_clients.NAME_RE.match(name):
        raise _fail(f"--name '{name}' must be letters, digits, '-' or '_'", 2)
    info = cli._call("GET", "/api/connect")
    url = info["mcp_url"]
    token = tok_id = None
    if auth == "oauth":
        if not c.oauth:
            raise _fail(f"{c.title} can't sign in to hangar with OAuth from its config file; use --auth token"
                        + (" (or add hangar as a custom connector under Settings → Connectors)"
                           if client == "claude-desktop" else ""), 2)
        if not info.get("oauth"):
            raise _fail("hangar's OAuth server is off: set HANGAR_PUBLIC_URL to the https URL clients reach it at "
                        "(or localhost), or use --auth token", 2)
    elif auth != "token":
        raise _fail("--auth must be token or oauth", 2)
    else:
        # A token per client and machine, limited to MCP: a leaked client config can't mint tokens or read the vault.
        res = cli._call("POST", "/api/tokens", json={"name": f"mcp-{client}-{_host()}", "scopes": ["mcp"]})
        token, tok_id = res["token"], res["id"]
    if print_only:
        e = mcp_clients.entry(client, url, token)
        typer.secho(f"# {c.title}: {path}", fg="cyan")
        typer.echo(mcp_clients.render(client, name, e))
        if tok_id:
            typer.secho(f"\nThis holds a new token ({tok_id}, scope mcp); revoke it with `hangar token revoke "
                        f"{tok_id}` if you don't use it.", fg="yellow", err=True)
        return
    header_file = None
    if client == "claude-desktop" and token:
        header_file = cli._config_path().parent / "mcp-headers" / f"claude-desktop-{name}.txt"
        mcp_clients.write_private(header_file, f"# hangar MCP token for Claude Desktop ({url})\n"
                                               f"Authorization: Bearer {token}\n")
        header_file.parent.chmod(0o700)
    e = mcp_clients.entry(client, url, token, header_file)
    try:
        how = mcp_clients.write(client, path, name, e)
    except (ClientConfigError, OSError) as err:
        typer.secho(f"{err}\nAdd this to {path} by hand:\n", fg="yellow", err=True)
        typer.echo(mcp_clients.render(client, name, e))
        raise typer.Exit(1) from err
    if tok_id:  # the entry now holds the new token: retire this client's older ones on this machine
        tok_name = f"mcp-{client}-{_host()}"
        for t in cli._call("GET", "/api/tokens"):
            if t["name"] == tok_name and t["id"] != tok_id:
                cli._call("DELETE", f"/api/tokens/{t['id']}")
    shown = str(path).replace(str(Path.home()), "~", 1)
    typer.secho(f"{how} '{name}' in {shown} ({c.title}) -> {url}" + (f" with token {tok_id} (scope mcp)"
                                                                    if tok_id else " (OAuth)"), fg="green")
    if auth == "oauth":
        typer.echo("Sign in: " + c.login.format(name=name) + ", then approve on hangar's page with an admin token.")
    typer.echo(c.restart.format(name=name, bridge=mcp_clients.MCP_REMOTE))


def connect(
    client: str = typer.Argument(None, help=f"One of: {', '.join(mcp_clients.CLIENTS)}."),
    list_: bool = typer.Option(False, "--list", help="Show each client, its config file and whether hangar is in it."),
    print_only: bool = typer.Option(False, "--print", help="Only print the entry (with a new token); write nothing."),
    auth: str = typer.Option("token", "--auth", help="token (a new token scoped to MCP) or oauth (the client signs "
                             "in itself; claude-code, codex, cursor, gemini; needs an https or localhost hangar)."),
    name: str = typer.Option("hangar", "--name", help="Server name in the client's config."),
    config: Path | None = typer.Option(None, "--config", help="Write this file instead of the client's default."),
):
    """Add this hangar to an MCP client's config, keeping the other servers there.

      hangar connect claude-code        hangar connect codex --auth oauth        hangar connect --list
    """
    if list_ or not client:
        list_clients()
        if not client and not list_:
            typer.echo("\nConnect one with: hangar connect <client>")
        return
    connect_client(client, auth, name, config, print_only)


# ============================================================================ hangar up / down
def _data_dir() -> Path:
    return Path(os.environ.get("HANGAR_DATA_DIR") or Path.home() / ".hangar").expanduser()


def _healthy(url: str) -> bool:
    try:
        return httpx.get(f"{url}/healthz", timeout=2).status_code == 200
    except httpx.HTTPError:
        return False


def _token_ok(url: str, token: str) -> bool:
    try:
        return httpx.get(f"{url}/api/me", headers={"Authorization": f"Bearer {token}"}, timeout=10).status_code == 200
    except httpx.HTTPError:
        return False


def _find_envs() -> Path | None:
    """The environment specs to serve: ./envs, else the checkout this hangar is installed from."""
    import hangar

    for d in (Path.cwd() / "envs", Path(hangar.__file__).resolve().parents[2] / "envs"):
        if d.is_dir() and any(d.glob("*.yaml")):
            return d.resolve()
    return None


def _docker_ok() -> str | None:
    try:
        import docker

        docker.from_env(timeout=5).ping()
        return None
    except Exception as e:  # noqa: BLE001 - any failure means "not usable"
        return str(e).splitlines()[0][:200] if str(e) else type(e).__name__


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # The pid file can outlive the server (a reboot): only ever signal a process that is hangar.
    cmd = Path(f"/proc/{pid}/cmdline")
    if cmd.exists():
        return b"hangar" in cmd.read_bytes()
    r = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True)
    return r.returncode == 0 and "hangar" in r.stdout


def _start_server(data_dir: Path, port: int, timeout: int) -> int:
    env = dict(os.environ)
    env["HANGAR_DATA_DIR"] = str(data_dir)
    if port != 8787:  # the default public URL already says 8787 (and a .env may set its own)
        env.setdefault("HANGAR_PUBLIC_URL", f"http://localhost:{port}")
    if "HANGAR_ENV_DIRS" not in env:
        envs = _find_envs()
        if envs is None:
            typer.secho("no environment specs found (./envs); hangar starts with only saved environments",
                        fg="yellow")
        else:
            env["HANGAR_ENV_DIRS"] = json.dumps([str(envs)])
    log_path = data_dir / "server.log"
    with open(log_path, "ab") as log:
        p = subprocess.Popen([sys.executable, "-c", "from hangar.cli import app; app()", "serve", "--port", str(port)],
                             env=env, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                             start_new_session=True)
    (data_dir / "server.pid").write_text(f"{p.pid}\n")
    url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if p.poll() is not None:
            tail = log_path.read_text(errors="replace").splitlines()[-15:]
            (data_dir / "server.pid").unlink(missing_ok=True)
            raise _fail(f"hangar exited during startup (code {p.returncode}). Last lines of {log_path}:\n"
                        + "\n".join(tail))
        if _healthy(url):
            return p.pid
        time.sleep(0.5)
    raise _fail(f"hangar didn't answer on {url} within {timeout}s; see {log_path}")


def _save_login(url: str, token: str) -> Path:
    cli = _cli()
    cfg = cli._config_path()
    mcp_clients.write_private(cfg, json.dumps({"url": url, "token": token}))
    return cfg


def _login_local(url: str, data_dir: Path) -> str:
    """A working admin token for the local server: the saved login, the bootstrap token, or a new one."""
    cli = _cli()
    saved = cli._saved()
    candidates = [saved["token"]] if saved.get("url") == url and saved.get("token") else []
    boot = data_dir / "bootstrap-token"
    if boot.exists():
        candidates.append(boot.read_text().strip())
    for t in candidates:
        if t and _token_ok(url, t):
            return t
    from hangar.auth import TokenAuth
    from hangar.store import Store

    raw, _ = TokenAuth(Store(data_dir / "hangar.db")).create(f"cli-{_host()}")
    if not _token_ok(url, raw):
        raise _fail(f"the hangar on {url} doesn't use {data_dir}; log in with `hangar login {url}`")
    return raw


def _build_envs(names: list[str]) -> bool:
    cli = _cli()
    envs = {e["name"]: e for e in cli._call("GET", "/api/environments")}
    ok = True
    for name in names:
        e = envs.get(name)
        if e is None:
            typer.secho(f"! environment {name} doesn't exist; skipped", fg="yellow")
            continue
        if e.get("image") or (e.get("images") or {}).get("docker"):
            typer.secho(f"✓ environment {name} is built", fg="green")
            continue
        typer.secho(f"building environment {name} (the first build takes a few minutes; later ones are cached)",
                    fg="cyan")
        try:
            cli.env_build(name, None, False)
        except typer.Exit:
            ok = False
            typer.secho(f"✗ building {name} failed (see above); `hangar up` retries it", fg="red")
    return ok


def up(
    port: int = typer.Option(8787, help="Port for the dashboard, REST and MCP."),
    env: list[str] = typer.Option(["claude-python"], "--env", "-e", help="Environment to build (repeatable)."),
    build: bool = typer.Option(True, "--build/--no-build", help="Build the environments now."),
    connect_to: list[str] = typer.Option([], "--connect", help="Connect this MCP client (repeatable)."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Connect every detected client without asking."),
    timeout: int = typer.Option(120, help="Seconds to wait for the server to start."),
):
    """Run hangar on this machine with Docker: start the server, log in, build environments, connect clients.

    Safe to run again: it reuses the running server, the saved login and built images."""
    data_dir = _data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    data_dir.chmod(0o700)
    if why := _docker_ok():
        raise _fail(f"Docker isn't reachable ({why}). Start Docker Desktop, OrbStack or Colima (macOS), or the "
                    "docker service (Linux: sudo systemctl start docker), then run `hangar up` again.")
    typer.secho("✓ docker is running", fg="green")
    url = f"http://127.0.0.1:{port}"
    pid_file = data_dir / "server.pid"
    if _healthy(url):
        pid = pid_file.read_text().strip() if pid_file.exists() else "?"
        typer.secho(f"✓ hangar is already running on {url} (pid {pid})", fg="green")
    else:
        pid = _start_server(data_dir, port, timeout)
        typer.secho(f"✓ started hangar on {url} (pid {pid}, log {data_dir / 'server.log'})", fg="green")
    token = _login_local(url, data_dir)
    previous = _cli()._saved().get("url")
    if previous and previous != url:
        typer.secho(f"! the CLI was logged in to {previous}; it now uses {url} (`hangar login {previous}` to switch "
                    "back)", fg="yellow")
    os.environ.pop("HANGAR_URL", None)  # this process: talk to the server just started, with the saved login
    os.environ.pop("HANGAR_TOKEN", None)
    cfg = _save_login(url, token)
    typer.secho(f"✓ logged in (saved to {cfg})", fg="green")
    built = _build_envs(env) if build and env else True
    targets = list(connect_to)
    if not targets:
        detected = []
        for c in mcp_clients.CLIENTS:
            try:
                there = mcp_clients.installed(c) and mcp_clients.current(c, mcp_clients.config_path(c), "hangar")
            except (ClientConfigError, OSError):
                continue
            if mcp_clients.installed(c) and not there:
                detected.append(c)
        if yes:
            targets = detected
        elif detected and sys.stdin.isatty():
            targets = [c for c in detected if typer.confirm(f"Connect {mcp_clients.CLIENTS[c].title} to hangar?",
                                                            default=True)]
        elif detected:
            typer.echo("Connect an MCP client: " + "   ".join(f"hangar connect {c}" for c in detected))
    for c in targets:
        try:
            connect_client(c)
        except typer.Exit as e:
            if e.exit_code:
                typer.secho(f"couldn't connect {c}; run `hangar connect {c}` to retry", fg="yellow")
    typer.echo(f"\nDashboard: http://localhost:{port}   (sign in with the token in {cfg})")
    typer.echo("Next: add model credentials (dashboard → Credentials, or `hangar cred add`), then ask your MCP "
               "client to create a sandbox. `hangar doctor` checks the setup; `hangar down` stops the server.")
    if not built:
        raise typer.Exit(1)


def down():
    """Stop the hangar server `hangar up` started (sandboxes keep running until their TTL)."""
    pid_file = _data_dir() / "server.pid"
    try:
        pid = int(pid_file.read_text().strip())
    except (OSError, ValueError):
        typer.echo("hangar isn't running (no server.pid from `hangar up`)")
        return
    if not _pid_alive(pid):
        pid_file.unlink(missing_ok=True)
        typer.echo("hangar isn't running")
        return
    os.kill(pid, signal.SIGTERM)
    for _ in range(60):
        if not _pid_alive(pid):
            break
        time.sleep(0.5)
    else:
        raise _fail(f"hangar (pid {pid}) is still shutting down; check {_data_dir() / 'server.log'}")
    pid_file.unlink(missing_ok=True)
    typer.secho(f"stopped hangar (pid {pid})", fg="green")


# ============================================================================ hangar setup
def _secret(label: str, file: Path | None, stdin: bool, interactive: bool, allow_keep: bool) -> str | None:
    """A secret from a file, stdin or a hidden prompt; never echoed. None = keep the stored one."""
    if file:
        return file.expanduser().read_text().strip() or None
    if stdin:
        return sys.stdin.read().strip() or None
    if not interactive:
        return None
    hint = " (input hidden; Enter keeps the stored one)" if allow_keep else " (input hidden)"
    v = typer.prompt(label + hint, hide_input=True, default="", show_default=False).strip()
    return v or None


def _repo_pairs(pairs: list[str], interactive: bool, envs: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for p in pairs:
        repo, _, env = p.partition("=")
        if not env:
            if not interactive:
                raise _fail(f"--repo {p}: give the environment too, e.g. --repo {repo}=python", 2)
            env = typer.prompt(f"Environment for {repo} ({', '.join(envs)})")
        out[repo.strip()] = env.strip()
    while interactive and not out:
        repo = typer.prompt("GitHub repo to take work from (owner/repo)").strip()
        env = typer.prompt(f"Environment jobs on {repo} run in ({', '.join(envs)})",
                           default="python" if "python" in envs else (envs[0] if envs else "")).strip()
        out[repo] = env
        if typer.confirm("Add another repo?", default=False):
            out.update(_repo_pairs([], interactive, envs) or {})
    return out


@setup_app.command("github")
def setup_github(
    repo: list[str] = typer.Option([], "--repo", help="owner/repo=environment (repeatable)."),
    token_file: Path | None = typer.Option(None, "--token-file", help="Read the GitHub token from a file."),
    token_stdin: bool = typer.Option(False, "--token-stdin", help="Read the GitHub token from stdin."),
    token_from_gh: bool = typer.Option(False, "--token-from-gh", help="Use `gh auth token` (the gh CLI's login)."),
    webhook_url: str | None = typer.Option(None, "--webhook-url", help="Public base URL GitHub delivers to (default: "
                                           "HANGAR_PUBLIC_URL), e.g. a tunnel to your laptop."),
    no_webhook: bool = typer.Option(False, "--no-webhook", help="Don't register the repo webhook."),
    rotate_secret: bool = typer.Option(False, "--rotate-secret", help="Generate a new webhook secret."),
    token_credential: str = typer.Option("gh-control", "--token-credential", help="Vault name for the token."),
    secret_credential: str = typer.Option("gh-webhook", "--secret-credential", help="Vault name for the secret."),
    non_interactive: bool = typer.Option(False, "--non-interactive", help="Never prompt (for scripts)."),
):
    """Connect hangar to GitHub: token, webhook secret, repo webhook, settings, and a test delivery.

      hangar setup github                                   (asks for everything)

      hangar setup github --repo acme/app=python --token-file ~/.gh-token --non-interactive
    """
    from hangar.setup_api import HOOK_EVENTS, TOKEN_HELP

    cli = _cli()
    interactive = not non_interactive and sys.stdin.isatty()
    envs = [e["name"] for e in cli._call("GET", "/api/environments")]
    repos = _repo_pairs(repo, interactive, envs)
    if token_from_gh:
        r = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True)
        if r.returncode != 0 or not r.stdout.strip():
            raise _fail("`gh auth token` didn't return a token; run `gh auth login` first", 2)
        token = r.stdout.strip()
    else:
        if interactive and not (token_file or token_stdin):
            typer.echo(TOKEN_HELP)
        token = _secret("GitHub token", token_file, token_stdin, interactive, allow_keep=True)
    typer.secho(f"Setting up GitHub for {', '.join(repos) or 'the mapped repos'} (webhook events: "
                f"{', '.join(HOOK_EVENTS)})", fg="cyan")
    res = cli._call("POST", "/api/setup/github", json={
        "token": token, "repos": repos, "webhook_url": webhook_url, "register_webhook": not no_webhook,
        "rotate_secret": rotate_secret, "token_credential": token_credential, "secret_credential": secret_credential})
    _print_steps(res)
    if not res.get("ok"):
        raise typer.Exit(1)
    typer.secho("\nGitHub is connected: label an issue `hangar` or comment `/hangar <instructions>` on it.", fg="green")


def _manifest(base: str) -> str | None:
    import hangar

    for d in (Path.cwd() / "docs", Path(hangar.__file__).resolve().parents[2] / "docs"):
        f = d / "slack-app-manifest.yaml"
        if f.is_file():
            return f.read_text().replace("https://hangar.example.com", base)
    return None


@setup_app.command("slack")
def setup_slack(
    channel: list[str] = typer.Option([], "--channel", help="CHANNEL_ID=owner/repo (repeatable)."),
    user: list[str] = typer.Option([], "--user", help="Slack user id allowed to start jobs (repeatable; default "
                                   "anyone in a mapped channel)."),
    signing_secret_file: Path | None = typer.Option(None, "--signing-secret-file"),
    bot_token_file: Path | None = typer.Option(None, "--bot-token-file"),
    manifest: Path | None = typer.Option(None, "--manifest", help="Write the Slack app manifest, filled in with this "
                                         "hangar's URL, to this file."),
    non_interactive: bool = typer.Option(False, "--non-interactive", help="Never prompt (for scripts)."),
):
    """Connect hangar to a Slack app: secrets, channel map, and a signed test delivery.

      hangar setup slack --manifest slack-app.yaml     (then create the app from it and paste its two secrets)
    """
    cli = _cli()
    interactive = not non_interactive and sys.stdin.isatty()
    base = cli._call("GET", "/api/connect")["mcp_url"].removesuffix("/mcp")
    if manifest or interactive:
        text = _manifest(base)
        dest = manifest or Path("hangar-slack-manifest.yaml")
        if text is None:
            typer.secho("docs/slack-app-manifest.yaml isn't here; use the one from the hangar repo", fg="yellow")
        else:
            dest.write_text(text)
            typer.echo(f"Wrote the Slack app manifest for {base} to {dest}. At https://api.slack.com/apps choose "
                       "Create New App → From a manifest, paste it, and install the app to the workspace.")
    signing = _secret("Signing secret (Basic Information)", signing_secret_file, False, interactive, True)
    bot = _secret("Bot User OAuth Token, xoxb-… (OAuth & Permissions)", bot_token_file, False, interactive, True)
    channels = {}
    for c in channel:
        ch, _, repo = c.partition("=")
        if not repo:
            raise _fail(f"--channel {c}: use CHANNEL_ID=owner/repo", 2)
        channels[ch.strip()] = repo.strip()
    if interactive and not channels:
        ch = typer.prompt("Channel id to take work from (channel name → About; Enter to skip)", default="",
                          show_default=False).strip()
        if ch:
            channels[ch] = typer.prompt(f"Repo for {ch} (owner/repo)").strip()
    res = cli._call("POST", "/api/setup/slack", json={"signing_secret": signing, "bot_token": bot,
                                                       "channels": channels, "users": user or None})
    _print_steps(res)
    if not res.get("ok"):
        raise typer.Exit(1)
    urls = res.get("urls") or {}
    typer.secho(f"\nSlack is connected. In the app's Event Subscriptions, retry the request URL "
                f"({urls.get('events')}), then /invite @hangar in each mapped channel.", fg="green")


# ============================================================================ hangar config
def _setting_line(d: dict) -> str:
    v = json.dumps(d["value"]) if not isinstance(d["value"], str) else d["value"]
    return f"{d['key']} = {v}   [{d['source']}]"


@config_app.command("list")
def config_list(all_: bool = typer.Option(False, "--all", help="Include settings left at their defaults."),
                as_json: bool = typer.Option(False, "--json")):
    """Settings and where each value comes from: environment (wins), hangar config, or default."""
    items = _cli()._call("GET", "/api/settings", params={"all": all_})
    if as_json:
        return _cli()._print(items)
    for d in items:
        typer.echo(_setting_line(d))
        if d["source"] == "environment" and d.get("stored") is not None:
            typer.secho(f"    (hangar config has {json.dumps(d['stored'])}, overridden by the environment)",
                        fg="yellow")


@config_app.command("get")
def config_get(key: str):
    typer.echo(_setting_line(_cli()._call("GET", f"/api/settings/{key}")))


@config_app.command("set")
def config_set(key: str, value: str = typer.Argument(..., help="JSON for lists, maps, numbers and booleans; "
                                                     "otherwise a plain string.")):
    """Store a setting in hangar's database. Example:  hangar config set HANGAR_GITHUB_REPOS '{"acme/app": "python"}'

    Secrets are refused (use `hangar cred add`); the server's environment wins where both are set."""
    d = _cli()._call("PUT", f"/api/settings/{key}", json={"value": value})
    typer.secho(_setting_line(d), fg="green")
    _setting_notes(d)


@config_app.command("unset")
def config_unset(key: str):
    """Remove a stored setting (it falls back to the environment or the default)."""
    d = _cli()._call("DELETE", f"/api/settings/{key}")
    typer.secho(_setting_line(d), fg="green")
    _setting_notes(d)


def _setting_notes(d: dict) -> None:
    if d["source"] == "environment":
        typer.secho(f"    {d['key']} is also set in the server's environment (on GKE: the hangar or hangar-site config "
                    "map), which wins: remove it there for the stored value to apply.", fg="yellow")
    elif d.get("restart"):
        typer.secho("    applies after hangar restarts (`hangar down && hangar up`, or "
                    "`kubectl -n hangar rollout restart deploy/hangar`)", fg="yellow")


# ============================================================================ hangar doctor --fix
def doctor_fix(checks: list[dict], yes: bool) -> int:
    """Offer each automatic fix; print the command for the rest. Returns the number of fixes applied."""
    cli = _cli()
    applied = 0
    for c in checks:
        if c["status"] == "ok":
            continue
        action = c.get("action")
        if not action:
            if c.get("fix"):
                typer.echo(f"  {c['name']}: run  {c['fix']}")
            continue
        what = {"build_environment": f"build environment {action.get('env')}",
                "github_webhook": f"register/repair the webhook on {action.get('repo')}"}.get(action["id"])
        if what is None:
            typer.echo(f"  {c['name']}: run  {c['fix']}")
            continue
        if not (yes or (sys.stdin.isatty() and typer.confirm(f"Fix '{c['name']}': {what}?", default=True))):
            typer.echo(f"  {c['name']}: skipped; to fix it yourself: {c['fix']}")
            continue
        if action["id"] == "build_environment":
            try:
                cli.env_build(action["env"], None, False)
                applied += 1
            except typer.Exit:
                typer.secho(f"✗ building {action['env']} failed", fg="red")
        else:
            res = cli._call("POST", "/api/setup/github/webhook", json={"repo": action["repo"]})
            _print_steps(res)
            applied += bool(res.get("ok"))
    return applied
