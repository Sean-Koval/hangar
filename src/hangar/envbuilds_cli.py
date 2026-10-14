"""`hangar env builds | build-repo | rollback | unpin | check` (--- envbuilds; envbuilds.py, docs/environments.md)."""

from __future__ import annotations

import json
import time
from pathlib import Path

import typer


def _cli():
    from hangar import cli

    return cli


def _when(ts: float | None) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "-"


def _row(b: dict) -> str:
    mark = "*" if b.get("active") else " "
    state = b["state"] + (f" ({b['stage']})" if b.get("stage") else "")
    what = b.get("error") or b.get("image") or ""
    return (f"{mark} {b['version']:>4}  {b['id']:13} {state:28} {(b.get('commit') or '')[:9]:9}  "
            f"{_when(b.get('finished_at') or b.get('created_at')):16}  {b.get('reason', ''):6}  {what[:90]}")


def register(env_app: typer.Typer) -> None:
    @env_app.command("builds")
    def env_builds(repo: str | None = typer.Argument(None, help="owner/repo or a git URL; none: every repo."),
                   env: str | None = typer.Option(None, "--env", "-e", help="The environment (default: as a job for "
                                                  "the repo picks it)."),
                   provider: str | None = typer.Option(None, "--provider"),
                   as_json: bool = typer.Option(False, "--json")):
        """A repo's environment builds, newest first (* = what new sandboxes start from), or every repo's."""
        params = {k: v for k, v in (("repo", repo), ("environment", env), ("provider", provider)) if v}
        r = _cli()._call("GET", "/api/envbuilds", params=params)
        if as_json:
            return _cli()._print(r)
        if not repo:
            if not r:
                typer.echo("No repo builds yet. A repo gets them when it has a .hangar/environment.yaml with an "
                           "install (docs/environments.md).")
            for h in r:
                act, latest = h.get("active") or {}, h.get("latest") or {}
                typer.echo(f"{h['repo']:40} {h['environment']:16} {h['provider']:10} "
                           f"active: {act.get('version') or '-'}{' (pinned)' if h.get('pinned') else ''}  "
                           f"latest: {latest.get('version') or '-'} {latest.get('state') or ''}")
            return
        typer.echo(f"{r['repo']} / {r['environment']} on {r['provider']}"
                   + ("  [pinned by an admin]" if r.get("pinned") else "") + ("  [building]" if r.get("running")
                                                                              else ""))
        for b in r["builds"]:
            typer.echo(_row(b))
        if not r["builds"]:
            typer.echo("no builds")

    @env_app.command("build-show")
    def env_build_show(build_id: str, log: bool = typer.Option(True, "--log/--no-log")):
        """One build: its config, image and the end of the install's output."""
        b = _cli()._call("GET", f"/api/envbuilds/{build_id}")
        out = log and b.get("log")
        typer.echo(json.dumps({k: v for k, v in b.items() if k != "log"}, indent=2, default=str))
        if out:
            typer.echo("\n--- install output (end)\n" + out)

    @env_app.command("build-repo")
    def env_build_repo(repo: str, env: str | None = typer.Option(None, "--env", "-e"),
                       provider: str | None = typer.Option(None, "--provider"),
                       wait: bool = typer.Option(False, "--wait", help="Wait for it to finish.")):
        """Build a repo's environment now (in the background): clone its default branch, run its install."""
        body = {"repo": repo, "environment": env, "provider": provider}
        b = _cli()._call("POST", "/api/envbuilds", json=body)
        typer.echo(f"build {b['version']} ({b['id']}) of {b['repo']} / {b['environment']}: {b['state']}")
        while wait and b["state"] in ("queued", "running"):
            time.sleep(5)
            b = _cli()._call("GET", f"/api/envbuilds/{b['id']}")
            typer.echo(f"  {b['state']}" + (f": {b['stage']}" if b.get("stage") else ""))
        if wait:
            typer.secho(b.get("error") or b.get("image") or b["state"],
                        fg="green" if b["state"] == "succeeded" else "red")
            if b["state"] != "succeeded":
                raise typer.Exit(1)

    @env_app.command("rollback")
    def env_rollback(build_id: str):
        """Admin: make an earlier build the one new sandboxes start from, pinned until `hangar env unpin`."""
        b = _cli()._call("POST", f"/api/envbuilds/{build_id}/rollback")
        typer.secho(f"{b['repo']} / {b['environment']}: {b['version']} ({b['id']}) is active and pinned", fg="green")

    @env_app.command("unpin")
    def env_unpin(repo: str, env: str | None = typer.Option(None, "--env", "-e"),
                  provider: str | None = typer.Option(None, "--provider")):
        """Admin: let new builds replace the active one again (the newest good build becomes active)."""
        r = _cli()._call("POST", "/api/envbuilds/unpin", json={"repo": repo, "environment": env,
                                                                 "provider": provider})
        typer.echo(f"{r['repo']} / {r['environment']}: unpinned; active: {r.get('active') or 'none'}")

    @env_app.command("check")
    def env_check(path: Path = typer.Argument(Path(".hangar/environment.yaml")),
                  repo: str | None = typer.Option(None, "--repo", help="Check `extends` against this repo's "
                                                  "allowlist and its build secrets against its policy."),
                  env: str | None = typer.Option(None, "--env", "-e")):
        """Validate a repo's .hangar/environment.yaml against this hangar (nothing is saved or built)."""
        r = _cli()._call("POST", "/api/envbuilds/validate", json={"content": path.read_text(), "repo": repo,
                                                                   "environment": env})
        for e in r.get("errors") or []:
            typer.secho(f"error: {e}", fg="red")
        for w in r.get("warnings") or []:
            typer.secho(f"warning: {w}", fg="yellow")
        if not r.get("valid"):
            raise typer.Exit(1)
        typer.secho(f"{path}: valid" + (f" (extends {r['environment']})" if r.get("environment") else ""),
                    fg="green")
