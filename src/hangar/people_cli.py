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
    if shared := p.get("shared_credentials"):  # --- review; --- teamcreds: the team credentials
        typer.secho(f"{p['id']}'s sandboxes get these team credentials where they have no login of their own, and "
                    f"{p['id']} can read them there: {', '.join(shared)}. To make one someone's own: hangar cred "
                    f"owner NAME PERSON. {p['id']} adds their own with hangar cred add.", fg="yellow")
    if p.get("login"):
        _login_hint(p["login"])


@person_app.command("ls")
def person_ls():
    for p in _cli()._call("GET", "/api/people"):
        links = f"  [{', '.join(p['links'])}]" if p.get("links") else ""  # --- teamcreds
        typer.echo(f"{p['id']:20} {p['role']:7} tokens={p['tokens']:<3} {p['name']}{links}")


@person_app.command("role")
def person_role(person_id: str, role: str = typer.Argument(..., help="member | admin")):
    p = _cli()._call("PATCH", f"/api/people/{person_id}", json={"role": role})
    typer.echo(f"{p['id']}: {p['role']}")


@person_app.command("token")
def person_token(person_id: str, name: str = typer.Option("login", help="Label, e.g. the machine.")):
    """Mint another login token for someone (a new laptop)."""
    _login_hint(_cli()._call("POST", f"/api/people/{person_id}/tokens", json={"name": name}))


# --- teamcreds
@person_app.command("link")
def person_link(person_id: str,
                source: str = typer.Argument(..., help="github | slack | jira | email"),
                external_id: str = typer.Argument(..., help="GitHub login or user id, Slack TEAM/USER id, Jira "
                                                            "account id, or email.")):
    """Link someone's account elsewhere to them: it then acts as them. Its jobs from GitHub, Slack, Jira or GitHub
    Projects start without approval, run with their own login, count against their caps and budget, and show up in
    their lists; and only they continue their jobs there. A GitHub login is stored as its numeric user id, which a
    rename doesn't change.

    Example:  hangar person link alice github alice-gh;  hangar person link alice slack T0123ABCD/U012AB3CD"""
    p = _cli()._call("POST", f"/api/people/{person_id}/links", json={"source": source, "id": external_id})
    if seen := p.get("looked_up"):  # --- review
        typer.echo(f"GitHub {seen['login']} is user id {seen['id']}")
    typer.echo(f"{p['id']}: {', '.join(p['links'])}")


@person_app.command("unlink")
def person_unlink(person_id: str, source: str, external_id: str):
    """Unlink an account: its jobs run for the team again."""
    p = _cli()._call("DELETE", f"/api/people/{person_id}/links", params={"source": source, "id": external_id})
    typer.echo(f"{p['id']}: {', '.join(p['links']) or 'no linked accounts'}")


# --- inbox delivery (#157, #158)
@person_app.command("set")
def person_set(person_id: str,
               dm: str | None = typer.Option(None, "--dm", help="off | questions (their own agents' questions and "
                                                                  "approvals, at once) | all (admins: the team's "
                                                                  "too)."),
               quiet: str | None = typer.Option(None, "--quiet", help="Hold DMs in this window of their day, e.g. "
                                                                        "20:00-08:00 (\"\" clears it). Approvals "
                                                                        "that would expire meanwhile still go."),
               tz: str | None = typer.Option(None, "--tz", help="Their IANA time zone, e.g. America/New_York."),
               digest: str | None = typer.Option(None, "--digest", help="Local digest times, e.g. 10:15,16:00 "
                                                                          "(\"\" turns the digest off)."),
               digest_weekends: bool | None = typer.Option(None, "--digest-weekends/--no-digest-weekends",
                                                           help="Send the digest on Saturdays and Sundays too."),
               eval_cap: str | None = typer.Option(None, "--eval-cap", help="Admins: their monthly eval cap in USD "
                                                                             "(\"\" for the default, "
                                                                             "HANGAR_EVAL_AUTHOR_CAP_USD_MONTH).")):
    """How the inbox reaches someone in Slack: a DM for each agent question or approval, and a digest twice a day.
    Anyone may change their own; admins anyone's.

    Example:  hangar person set alice --dm questions --tz Europe/Berlin --digest 09:30,16:00 --quiet 19:00-08:00"""
    if eval_cap is not None:  # --- #182: an admin's call, apart from the person's own delivery settings
        r = _cli()._call("PUT", f"/api/people/{person_id}/eval-cap",
                         json={"usd": float(eval_cap) if eval_cap.strip() else None})
        cap = r["eval_cap_usd_month"]
        typer.echo(f"{r['id']}: eval cap {'the default' if cap is None else f'${cap:,.2f}'} a month")
        if all(v is None for v in (dm, quiet, tz, digest, digest_weekends)):
            return
    body = {"dm": dm, "quiet": quiet, "tz": tz, "digest_weekends": digest_weekends,
            "digest": None if digest is None else [x.strip() for x in digest.split(",") if x.strip()]}
    p = _cli()._call("PUT", f"/api/people/{person_id}/delivery", json={k: v for k, v in body.items() if v is not None})
    typer.echo(f"{p['id']}: dm={p['notify']} tz={p['tz']} digest={','.join(p['digest']) or 'off'}"
               f"{' (weekends too)' if p['digest_weekends'] else ''} quiet={p.get('quiet') or 'none'}")


@person_app.command("rm")
def person_rm(person_id: str):
    """Remove someone and revoke their tokens. Their sandboxes, sessions and jobs stay (admins see them); their
    own credentials stay but go into nobody's sandbox until an admin reassigns them (hangar cred owner)."""
    r = _cli()._call("DELETE", f"/api/people/{person_id}")
    typer.echo(f"removed {r['removed']}; revoked {len(r['revoked_tokens'])} token(s)")
