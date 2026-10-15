"""`hangar requests` and `hangar screen`: answer what agents ask for, and watch their screens (--- computer)."""

from __future__ import annotations

import typer

requests_app = typer.Typer(no_args_is_help=True, help="Secrets and hosts agents asked for (never granted "
                                                      "automatically).")


def _cli():
    from hangar import cli

    return cli


@requests_app.command("ls")
def requests_ls(session: str | None = typer.Option(None, "--session", help="A session name or id."),
                state: str = typer.Option("pending", help="pending | approved | denied | all"),
                as_json: bool = typer.Option(False, "--json")):
    """Requests agents filed with `hangar-agent env request`."""
    cli = _cli()
    params = {"state": "" if state == "all" else state, **({"session": session} if session else {})}
    rows = cli._call("GET", "/api/env-requests", params=params)
    if as_json:
        cli._print(rows)
        return
    if not rows:
        typer.echo(f"no {state} requests")
    for r in rows:
        typer.echo(f"{r['id']}  {r['state']:9} {r['kind']:7} {r['target']:30} {r['session']}: {r['why'][:80]}")


def _answer(rid: str, decision: str, note: str | None, scope: str | None) -> None:
    cli = _cli()
    body = {"decision": decision, "note": note, **({"scope": scope} if scope else {})}
    r = cli._call("POST", f"/api/env-requests/{rid}", json=body)
    typer.echo(f"{r['id']}: {r['state']}" + (f" ({r['message']})" if r.get("message") else ""))


@requests_app.command("approve")
def requests_approve(rid: str, note: str | None = typer.Option(None, help="For the agent; never a secret value."),
                     scope: str | None = typer.Option(None, help="Egress: session (default) or environment.")):
    """Approve a request. For a secret, store the credential and map it in the environment first."""
    _answer(rid, "approve", note, scope)


@requests_app.command("deny")
def requests_deny(rid: str, note: str | None = typer.Option(None, help="Why, for the agent.")):
    """Deny a request; the agent reads your note."""
    _answer(rid, "deny", note, None)


def screen(ref: str = typer.Argument(..., help="A job id, session name or sandbox id."),
           control: bool = typer.Option(False, "--control", help="Your keyboard and mouse reach the screen."),
           open_browser: bool = typer.Option(True, "--open/--no-open")):
    """Watch an agent's screen (environments with `computer:`). The link works once, within 60 seconds."""
    cli = _cli()
    r = cli._call("POST", "/api/screen/tickets", json={"ref": ref, "control": control})
    typer.echo(r["url"])
    if open_browser:
        import webbrowser

        webbrowser.open(r["url"])
