"""`hangar inbox`: what needs you (--- landing, inbox.py)."""

from __future__ import annotations

import time

import typer

_ICON = {"review": "PR", "question": "??", "approval": "OK?", "failed": "!!"}


def inbox(as_json: bool = typer.Option(False, "--json")):
    """PRs waiting for your review, agents' questions, approvals and failed jobs, newest first."""
    from hangar import cli

    v = cli._call("GET", "/api/inbox")
    if as_json:
        return cli._print(v)
    xs = v.get("items") or []
    if not xs:
        return typer.echo("Nothing needs you.")
    for x in xs:
        ago = max(0, int(time.time() - (x.get("since") or time.time())))
        when = f"{ago // 3600}h" if ago >= 3600 else f"{ago // 60}m"
        what = x.get("job") or x.get("session") or ""
        typer.secho(f"{_ICON.get(x['kind'], x['kind']):3} {what:16} {when:>4}  {x['reason']}", bold=True)
        typer.echo(f"{'':26}{x['action']}")


def register(app: typer.Typer) -> None:
    app.command("inbox")(inbox)
