"""`hangar person ...`: people on the team and their login tokens (admins). Registered by cli.py."""

from __future__ import annotations

import typer

person_app = typer.Typer(no_args_is_help=True, help="People on the team: who owns which tokens, credentials and work.")


def _cli():
    from hangar import cli

    return cli


def _login_hint(login: dict) -> None:
    typer.secho(f"login token {login['id']} (scope {', '.join(login['scopes'])}), shown once:", fg="green")
    typer.echo(login["token"])
    typer.echo("They run: hangar login <this hangar's URL>   (paste the token), then: hangar connect <client>")


@person_app.command("add")
def person_add(person_id: str = typer.Argument(..., help="A short lowercase id, e.g. alice."),
               name: str = typer.Option("", help="Display name."),
               role: str = typer.Option("member", help="member (their own work) or admin (everything)."),
               no_token: bool = typer.Option(False, "--no-token", help="Don't mint a login token now.")):
    """Add someone and mint their login token. Members see and act on their own sandboxes, sessions and jobs;
    admins on everyone's, and manage people and the vault."""
    p = _cli()._call("POST", "/api/people", json={"id": person_id, "name": name, "role": role, "token": not no_token})
    typer.secho(f"added {p['id']} ({p['role']})", fg="green")
    if p.get("login"):
        _login_hint(p["login"])


@person_app.command("ls")
def person_ls():
    for p in _cli()._call("GET", "/api/people"):
        typer.echo(f"{p['id']:20} {p['role']:7} tokens={p['tokens']:<3} {p['name']}")


@person_app.command("role")
def person_role(person_id: str, role: str = typer.Argument(..., help="member | admin")):
    p = _cli()._call("PATCH", f"/api/people/{person_id}", json={"role": role})
    typer.echo(f"{p['id']}: {p['role']}")


@person_app.command("token")
def person_token(person_id: str, name: str = typer.Option("login", help="Label, e.g. the machine.")):
    """Mint another login token for someone (a new laptop)."""
    _login_hint(_cli()._call("POST", f"/api/people/{person_id}/tokens", json={"name": name}))


@person_app.command("rm")
def person_rm(person_id: str):
    """Remove someone and revoke their tokens. Their sandboxes, sessions and jobs stay (admins see them); their
    own credentials stay but go into nobody's sandbox until an admin reassigns them (hangar cred owner)."""
    r = _cli()._call("DELETE", f"/api/people/{person_id}")
    typer.echo(f"removed {r['removed']}; revoked {len(r['revoked_tokens'])} token(s)")
