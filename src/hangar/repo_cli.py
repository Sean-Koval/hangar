"""`hangar repo`: per-repo policy, the defaults every front door applies (--- frontdoor, repopolicy.py)."""

from __future__ import annotations

import json

import typer

repo_app = typer.Typer(no_args_is_help=True, help="Per-repo defaults for jobs from every front door "
                                                  "(verify, environment, harness, model).")


def _cli():
    from hangar import cli

    return cli


def _models(values: list[str]) -> str | dict[str, str] | None:
    """`--model opus` (any harness) or `--model claude-code=opus --model codex=gpt-5.5` (one per harness)."""
    if not values:
        return None
    if len(values) == 1 and "=" not in values[0]:
        return values[0]
    out: dict[str, str] = {}
    for v in values:
        harness, sep, model = v.partition("=")
        if not sep or not harness or not model:
            raise typer.BadParameter(f"'{v}': use MODEL alone, or HARNESS=MODEL for each harness", param_hint="--model")
        out[harness] = model
    return out


def _line(p: dict) -> str:
    verify = p.get("effective_verify")
    src = "" if p.get("verify") is not None or not verify else " (environment)"
    model = p.get("model")
    model = ",".join(f"{h}={m}" for h, m in model.items()) if isinstance(model, dict) else model
    bits = [f"verify: {verify!r}{src}" if verify else "verify: none",
            f"env: {p.get('environment') or ', '.join(p.get('allowed_environments') or []) or '-'}"]
    if p.get("harness"):
        bits.append(f"harness: {p['harness']}")
    if model:
        bits.append(f"model: {model}")
    return f"{p['repo']:40} " + "  ".join(bits)


@repo_app.command("set")
def repo_set(
    repo: str = typer.Argument(..., help="owner/repo for GitHub, or a git URL."),
    verify: str | None = typer.Option(None, "--verify", help="The repo's check, e.g. 'uv run pytest -q'. '' turns "
                                                             "off the environment's."),
    env: str | None = typer.Option(None, "--env", "-e", help="Default environment for the repo's jobs."),
    harness: str | None = typer.Option(None, "--harness", help="Default harness (it must be enabled in the "
                                                               "environment)."),
    model: list[str] = typer.Option([], "--model", help="MODEL, or HARNESS=MODEL (repeat it, one per harness)."),
    as_json: bool = typer.Option(False, "--json"),
):
    """Set the repo's defaults; what you don't pass is kept. Jobs that set a field themselves still win.

    Example:  hangar repo set acme/app --verify "uv run pytest -q" --env python --model claude-code=opus"""
    body: dict = {"repo": repo}
    for k, v in (("verify", verify), ("environment", env), ("harness", harness), ("model", _models(model))):
        if v is not None:
            body[k] = v
    if len(body) == 1:
        raise typer.BadParameter("pass at least one of --verify, --env, --harness, --model")
    p = _cli()._call("PUT", "/api/repos/policy", json=body)
    if as_json:
        return _cli()._print(p)
    typer.secho(_line(p), fg="green")


@repo_app.command("unset")
def repo_unset(repo: str = typer.Argument(...),
               fields: list[str] = typer.Argument(None, help="Fields to clear (verify, environment, harness, "
                                                             "model). None: remove the repo's policy.")):
    """Clear fields of the repo's policy, or remove it."""
    r = _cli()._call("DELETE", "/api/repos/policy", params={"repo": repo, "field": fields or []})
    typer.echo(f"removed the policy for {r['deleted']}" if "deleted" in r else _line(r))


@repo_app.command("get")
def repo_get(repo: str):
    """The repo's policy, with the environment it falls back to."""
    _cli()._print(_cli()._call("GET", "/api/repos/policy", params={"repo": repo}))


@repo_app.command("list")
def repo_list(as_json: bool = typer.Option(False, "--json")):
    xs = _cli()._call("GET", "/api/repos")
    if as_json:
        return typer.echo(json.dumps(xs, indent=2))
    if not xs:
        typer.echo("No repo policies. Add one with: hangar repo set owner/repo --verify '…'")
    for p in xs:
        typer.echo(_line(p))
