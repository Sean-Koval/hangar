"""`hangar outbox status|retry|backfill` and `hangar session delete [--force-now]` (--- outbox #163, #189;
docs/api/outbox.md). Registered by cli.py. Admins."""

from __future__ import annotations

from datetime import datetime

import typer

outbox_app = typer.Typer(help="Run capture (the outbox): what is waiting to be uploaded, retrying it, backfilling.",
                         no_args_is_help=True)


def _cli():
    from hangar import cli

    return cli


def _at(ts: float | None) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else "-"


@outbox_app.command("status")
def status(json_out: bool = typer.Option(False, "--json", help="The raw status.")):
    """Objects pending, done and withheld, the oldest pending one, and retention held for the export."""
    cli = _cli()
    st = cli._call("GET", "/api/admin/outbox")
    if json_out:
        cli._print(st)
        return
    if not st["enabled"]:
        typer.echo("outbox: off (set HANGAR_OUTBOX_URL)")
        return
    c = st["counts"]
    typer.echo(f"outbox: {st['sink']}")
    typer.echo(f"  done {c.get('done', 0)}, pending {st['pending']} ({round(st['pending_bytes'] / 2**20, 1)} MiB), "
               f"withheld {st['failed_count']}")
    if st["pending"]:
        typer.echo(f"  oldest pending since {_at(st['oldest_pending_at'])}; next try {_at(st['next_retry_at'])}")
    if st.get("last_error"):
        typer.secho(f"  last error: {st['last_error']}", fg="yellow")
    for f in st["failed"]:
        typer.secho(f"  withheld {f['key']}: {f['error']}", fg="yellow")
    pd = st["pending_delete"]
    if pd["sessions"]:
        typer.echo(f"  {pd['sessions']} deleted session(s) wait for their export "
                   f"({round(pd['bytes'] / 2**20, 1)} MiB)")


@outbox_app.command("retry")
def retry():
    """Try every pending object now, instead of waiting out its backoff (after an outage)."""
    typer.echo(f"retrying {_cli()._call('POST', '/api/admin/outbox/retry')['retried']} object(s)")


@outbox_app.command("backfill")
def backfill(since: str = typer.Option("7d", "--since", help="How far back: 7d, 12h, 30m.")):
    """Capture runs finished in the window that have nothing queued, from what hangar still has on disk."""
    got = _cli()._call("POST", "/api/admin/outbox/backfill", json={"since": since})
    typer.echo(f"captured {got['runs']} run(s); {got['skipped']} already queued")


def session_delete(ref: str = typer.Argument(...),
                   force_now: bool = typer.Option(False, "--force-now",
                                                  help="Delete its event log and manifests now, even when the outbox "
                                                       "hasn't exported its runs (personal-data deletion; audited).")):
    """Delete a session record. Its event log and manifests go once the outbox exported its runs."""
    from urllib.parse import quote

    _cli()._call("DELETE", f"/api/sessions/{quote(ref, safe='')}" + ("?force_now=true" if force_now else ""))
    typer.echo(f"deleted {ref}" + (" (and its files, now)" if force_now else ""))


def register(app: typer.Typer, session_app: typer.Typer) -> None:
    app.add_typer(outbox_app, name="outbox")
    session_app.command("delete")(session_delete)
