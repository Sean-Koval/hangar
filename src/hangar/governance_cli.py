"""`hangar governance ...`: break-glass while Tower can't answer (KAN-33, spec 0015 §7). Registered by cli.py.

    hangar governance local --until 2h --reason "Tower down, INC-12"   # an admin starts it
    hangar governance local --confirm                                    # a second admin turns it on
    hangar governance local --end                                        # either admin, as soon as Tower is back
    hangar governance status
"""

from __future__ import annotations

from datetime import UTC, datetime

import typer

governance_app = typer.Typer(no_args_is_help=True, help="Tower governance: break-glass while Tower is unreachable.")


def _cli():
    from hangar import cli

    return cli


def _show(v: dict) -> None:
    if v.get("state") == "off":
        typer.echo("break-glass: off")
        return
    until = datetime.fromtimestamp(v["until"], UTC).strftime("%Y-%m-%d %H:%M UTC")
    color = "red" if v["state"] == "active" else "yellow"
    typer.secho(f"break-glass: {v['state'].upper()} until {until} ({v['left_seconds'] // 60} min left)", fg=color,
                bold=True)
    typer.echo(f"  started by {v['started_by']}, confirmed by {v.get('confirmed_by') or '(nobody yet)'}")
    typer.echo(f"  reason: {v['reason']}")


@governance_app.command("local")
def local(until: str = typer.Option(None, help="How long: 2h, 90m (at most 24h), or an RFC 3339 time."),
          reason: str = typer.Option(None, help="Why: the incident, so the audit log says."),
          confirm: bool = typer.Option(False, "--confirm", help="Confirm break-glass another admin started."),
          end: bool = typer.Option(False, "--end", help="End break-glass now.")):
    """Break-glass: while Tower can't answer, an admin's approval of a job release or a landing in `enforce` is
    decided by hangar's own rules. One admin starts it, a second confirms it; it lasts at most 24 hours, ends on its
    own, and every step and decision is audited. A Tower that answers still decides."""
    if sum((bool(until or reason), confirm, end)) != 1:
        raise typer.BadParameter("give --until and --reason to start, or --confirm, or --end")
    if end:
        r = _cli()._call("POST", "/api/governance/break-glass/end")
        typer.echo("break-glass ended" if r.get("ended") else "break-glass was not on")
        return
    if confirm:
        v = _cli()._call("POST", "/api/governance/break-glass/confirm")
    else:
        if not until or not reason:
            raise typer.BadParameter("starting break-glass needs both --until and --reason")
        v = _cli()._call("POST", "/api/governance/break-glass", json={"until": until, "reason": reason})
    _show(v)
    if v.get("state") != "active":
        typer.echo("A second admin confirms it: hangar governance local --confirm")


@governance_app.command("status")
def status():
    """Whether break-glass is off, waiting for confirmation, or active."""
    _show(_cli()._call("GET", "/api/governance/break-glass"))
