"""`hangar session steer | interrupt | approvals` (workstream steer; docs/steering.md). Registered on the session
command group by cli.py."""

from __future__ import annotations

import time

import typer


def _cli():
    from hangar import cli  # cli.py registers these commands at import time

    return cli


def steer(ref: str = typer.Argument(..., help="Session name or id."),
          message: str = typer.Argument(..., help="What to tell the agent while it works.")):
    """Tell the agent something while its turn runs; it reads the message at its next step (Claude Code and
    Codex). `hangar session send` does the same for a running session."""
    x = _cli()._call("POST", f"/api/sessions/{ref}/steer", json={"message": message})
    typer.secho(f"{x['session']}: message delivered to turn {x['turn']}", fg="green")


def interrupt(ref: str = typer.Argument(..., help="Session name or id."),
              message: str | None = typer.Argument(None, help="Carry on with this instead of stopping.")):
    """Stop the running turn. With a message, the agent stops what it was doing and carries on with that."""
    x = _cli()._call("POST", f"/api/sessions/{ref}/interrupt", json={"message": message} if message else None)
    what = "interrupted; it carries on with your message" if message else "interrupted"
    typer.secho(f"{x['session']}: turn {x['turn']} {what}"
                + (" (as the next turn)" if x.get("status") == "queued" else ""), fg="green")


def approvals(ref: str = typer.Argument(..., help="Session name or id."),
              allow: str | None = typer.Option(None, "--allow", help="Allow this approval id."),
              always: str | None = typer.Option(None, "--always", help="Allow it, and calls like it for the rest of "
                                                "the conversation."),
              deny: str | None = typer.Option(None, "--deny", help="Deny this approval id."),
              message: str | None = typer.Option(None, "--message", "-m", help="Note to the agent (with --deny)."),
              watch: bool = typer.Option(False, "--watch", "-w", help="Keep listing until the turn ends."),
              as_json: bool = typer.Option(False, "--json")):
    """List the tool calls the agent waits on (approvals=ask), or answer one."""
    cli = _cli()
    chosen = [(d, a) for d, a in (("allow", allow), ("allow_always", always), ("deny", deny)) if a]
    if len(chosen) > 1:
        typer.secho("answer one approval at a time", fg="red", err=True)
        raise typer.Exit(2)
    if chosen:
        decision, aid = chosen[0]
        a = cli._call("POST", f"/api/sessions/{ref}/approvals/{aid}", json={"decision": decision, "message": message})
        return typer.secho(f"{a['id']} {a['status']}: {a['tool']} {a['detail'][:200]}", fg="green")
    shown: set[str] = set()
    while True:
        v = cli._call("GET", f"/api/sessions/{ref}/approvals")
        if as_json:
            cli._print(v)
        for a in v["pending"]:
            if a["id"] not in shown and not as_json:
                shown.add(a["id"])
                left = int((a.get("expires_at") or time.time()) - time.time())
                typer.echo(f"{a['id']}  {a['tool']}: {a['detail'][:300]}  (denied in {max(0, left)}s if unanswered)")
        if not watch or not v["running"]:
            if not v["pending"] and not as_json:
                typer.echo("No tool calls are waiting." + ("" if v["running"] else " (no turn is running)"))
            if v["pending"] and not as_json:
                typer.echo(f"Answer with: hangar session approvals {ref} --allow <id> | --deny <id> [-m why]")
            return None
        time.sleep(2)


def register(session_app: typer.Typer) -> None:
    session_app.command("steer")(steer)
    session_app.command("interrupt")(interrupt)
    session_app.command("approvals")(approvals)
