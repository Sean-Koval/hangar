"""`hangar changeset`: change-set profiles (admin config) and the change sets jobs made (changesets.py,
docs/changesets.md)."""

from __future__ import annotations

import json
from pathlib import Path

import typer

changeset_app = typer.Typer(no_args_is_help=True, help="Change sets: one ticket across several repos, one PR per "
                                                       "changed repo, landed in order, then the deploy step.")


def _cli():
    from hangar import cli

    return cli


def _read(path: Path) -> dict:
    import yaml

    try:
        data = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError) as e:
        raise typer.BadParameter(f"{path}: {e}", param_hint="FILE") from None
    if not isinstance(data, dict):
        raise typer.BadParameter(f"{path}: a profile is a mapping", param_hint="FILE")
    return data


@changeset_app.command("validate")
def validate(file: Path = typer.Argument(..., help="A hangar.changeset-profile/v1 YAML or JSON file.")):
    """Check a profile offline (no control plane): its shape, roles, land_order and deploy step.

    Example:  hangar changeset validate policy/changesets/payments-bot.yaml"""
    from hangar.changesets import ChangeSetError, parse_profile

    try:
        p = parse_profile(_read(file))
    except ChangeSetError as e:
        typer.secho(f"{file}: {e}", fg="red", err=True)
        raise typer.Exit(1) from None
    typer.echo(f"{p.name}: ok ({p.digest()}); lands " + " → ".join(p.land_order)
               + (f", then {p.deploy.role} ({p.deploy.apply.kind})" if p.deploy else ""))


@changeset_app.command("profile-set")
def profile_set(file: Path = typer.Argument(..., help="A hangar.changeset-profile/v1 YAML or JSON file.")):
    """Create or replace a profile (admin).

    Example:  hangar changeset profile-set payments-bot.yaml"""
    p = _cli()._call("PUT", "/api/changesets/profiles", json=_read(file))
    typer.echo(f"profile {p['name']} set ({p['digest']})")


@changeset_app.command("profiles")
def profiles(as_json: bool = typer.Option(False, "--json")):
    """The profiles."""
    xs = _cli()._call("GET", "/api/changesets/profiles")
    if as_json:
        return typer.echo(json.dumps(xs, indent=2))
    if not xs:
        typer.echo("No change-set profiles. Add one with: hangar changeset profile-set FILE")
    for p in xs:
        deploy = p.get("deploy") or {}
        typer.echo(f"{p['name']:24} {', '.join(p['repos'])}  lands {' → '.join(p['land_order'])}"
                   + (f", then {deploy['role']}" if deploy else ""))


@changeset_app.command("profile")
def profile(name: str):
    """One profile, as JSON."""
    _cli()._print(_cli()._call("GET", f"/api/changesets/profiles/{name}"))


@changeset_app.command("profile-rm")
def profile_rm(name: str):
    """Remove a profile (sets already made keep their copy)."""
    typer.echo(f"deleted {_cli()._call('DELETE', f'/api/changesets/profiles/{name}')['deleted']}")


@changeset_app.command("list")
def list_(state: str | None = typer.Option(None, help="working | landing | deploying | done | broken"),
          as_json: bool = typer.Option(False, "--json")):
    """Change sets, newest first."""
    xs = _cli()._call("GET", "/api/changesets", params={"state": state} if state else None)
    if as_json:
        return typer.echo(json.dumps(xs, indent=2))
    for cs in xs:
        parts = ", ".join(f"{r} #{p.get('pr_number') or '-'} {p.get('pr_state') or 'pushed'}"
                          for r, p in (cs.get("parts") or {}).items())
        typer.echo(f"{cs['id']}  {cs['state']:9} {cs['profile']:20} job {cs['parent_job']}  {parts}"
                   + (f"  ({cs['reason']})" if cs.get("reason") else ""))


@changeset_app.command("get")
def get(cs_id: str):
    """One change set, as JSON: its roles, parts, landing order and deploy step."""
    _cli()._print(_cli()._call("GET", f"/api/changesets/{cs_id}"))
