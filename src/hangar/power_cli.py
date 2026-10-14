"""`hangar power ...`: the sandbox pools' power state, and powering them on early (power.py, docs/gke.md). Registered
by cli.py. Admins."""

from __future__ import annotations

import time
from datetime import datetime

import typer

power_app = typer.Typer(help="The sandbox node pools' power schedule: status, and powering them on early.",
                        invoke_without_command=True)


def _cli():
    from hangar import cli

    return cli


def _at(ts: float | None) -> str:
    return datetime.fromtimestamp(ts).strftime("%a %H:%M") if ts else "-"


def _show(o: dict) -> None:
    p = o["power"]
    color = "green" if p["state"] == "up" else "yellow"
    line = {"up": "on", "down": "powered down", "starting": "powering on"}.get(p["state"], p["state"])
    if p["state"] != "up" and p.get("when"):
        line += f"; ready at {p['when']}"
    typer.secho(f"sandboxes: {line}" + (" (should have been on by now; power.sh never said so)" if p["stale"] else ""),
                fg=color)
    if p["jobs_waiting"]:
        typer.echo(f"jobs waiting for power: {p['jobs_waiting']}")
    if last := p.get("last_up"):
        pre = last.get("prepull") or {}
        typer.echo(f"last power-up: {_at(last.get('at'))} by {last.get('by')}" + (
            f"; {pre['images']} images pulled onto {pre.get('nodes', '?')} node(s) in {pre.get('seconds')} s"
            if pre.get("images") is not None else ""))
    if p.get("last_down"):
        typer.echo(f"last power-down: {_at(p['last_down'])}")
    if p.get("warm_pools"):
        typer.echo("warm pools today: " + ", ".join(f"{k}={v}" for k, v in sorted(p["warm_pools"].items())))
    if d := o.get("last_deploy"):
        typer.echo(f"last deploy: {_at(d['at'])} {d.get('version') or d.get('image', '')}: "
                   f"{d['result'].replace('_', ' ')}" + (f"  {d['run_url']}" if d.get("run_url") else ""))
    loops = o.get("loops") or {}
    late = [n for n, v in loops.items() if not v["ok"]]
    now = time.time()
    typer.echo("loops: " + ", ".join(
        f"{n} {int(now - v['last'])}s ago" if v.get("last") else f"{n} not yet" for n, v in sorted(loops.items())))
    if late:
        typer.secho(f"overdue: {', '.join(late)} (hangar doctor)", fg="yellow")


@power_app.callback()
def power(ctx: typer.Context):
    """Show the power state (the same as `hangar power status`)."""
    if ctx.invoked_subcommand is None:
        status(as_json=False)


@power_app.command("status")
def status(as_json: bool = typer.Option(False, "--json")):
    """Whether sandboxes are on, when they come back, jobs waiting, the last power-up and deploy, loop heartbeats."""
    o = _cli()._call("GET", "/api/admin/power")
    if as_json:
        return _cli()._print(o)
    _show(o)


@power_app.command("up")
def up(wait: bool = typer.Option(False, "--wait", help="Wait until the pools report they're on (up to 20 min)."),
       timeout: float = typer.Option(1200, help="With --wait: seconds to wait.")):
    """Power the sandbox pools on now instead of at the scheduled time: runs the power-up job (its floor node, image
    prepull and warm pools, then queued jobs start)."""
    r = _cli()._call("POST", "/api/admin/power/up")
    typer.secho(r.get("message") or r.get("state"), fg="green")
    if r.get("execution"):
        typer.echo(f"power-up job execution: {r['execution']}")
    if not wait or r.get("state") == "up":
        return
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(15)
        p = _cli()._call("GET", "/api/admin/power")["power"]
        if p["state"] == "up":
            typer.secho("sandboxes are on", fg="green")
            return
        typer.echo(f"  {p['state']}" + (f", ready at {p['when']}" if p.get("when") else ""))
    typer.secho("still not on; see `hangar power` and the power-up job's logs", fg="red", err=True)
    raise typer.Exit(1)
