"""`hangar eval budget`: the team's monthly eval budget, spent and remaining, by author (#182, evalbudget.py)."""

from __future__ import annotations

import typer

eval_app = typer.Typer(no_args_is_help=True, help="Evals: the team's monthly eval budget.")


@eval_app.command("budget")
def eval_budget(as_json: bool = typer.Option(False, "--json")):
    """This month's eval spend: the team's budget, spent and remaining, and each author's cap."""
    from hangar.cli import _call, _print

    d = _call("GET", "/api/eval/budget")
    if as_json:
        return _print(d)
    if not d["on"]:
        typer.echo(f"{d['month']}: the eval budget is off (HANGAR_EVAL_BUDGET_USD_MONTH=0)")
        return
    t = d["team"]
    typer.echo(f"{d['month']}: team ${t['spent_usd']:,.2f} of ${t['budget_usd']:,.2f} spent, "
               f"${t['remaining_usd']:,.2f} left")
    for name, a in sorted(d["authors"].items()):
        typer.echo(f"  {name:20} ${a['spent_usd']:,.2f} of ${a['cap_usd']:,.2f}, ${a['remaining_usd']:,.2f} left")
