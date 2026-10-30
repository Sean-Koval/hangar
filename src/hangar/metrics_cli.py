"""`hangar metrics weekly [--week] [--repo] [--person] [--json]` and `hangar metrics standup` (--- #168;
docs/metrics.md). Registered by cli.py."""

from __future__ import annotations

import typer

metrics_app = typer.Typer(help="Adoption metrics from hangar's run store (definitions: docs/metrics.md).",
                          no_args_is_help=True)


def _cli():
    from hangar import cli

    return cli


def _pct(v: float | None) -> str:
    return f"{round(v * 100)}%" if v is not None else "-"


def _num(v: float | None, unit: str = "") -> str:
    return f"{v:g}{unit}" if v is not None else "-"


@metrics_app.command("weekly")
def weekly(week: str = typer.Option(None, "--week", help="An ISO week, e.g. 2026-W43 (default: this week)."),
           repo: str = typer.Option(None, "--repo", help="One repo (owner/repo)."),
           person: str = typer.Option(None, "--person", help="One person (yourself, unless you are an admin)."),
           json_out: bool = typer.Option(False, "--json", help="Every metric, as JSON.")):
    """The week's agent PR yield, agent share, review wait, green-to-merge, steer, abandon and revert rates, cost
    per merged PR, reviewer attention per merged PR and active delegators."""
    cli = _cli()
    params = {k: v for k, v in {"week": week, "repo": repo, "person": person}.items() if v}
    got = cli._call("GET", "/api/metrics/weekly", params=params)
    if json_out:
        cli._print(got)
        return
    m = got["metrics"]
    scope = " ".join(x for x in (got.get("repo"), got.get("person") and f"person {got['person']}") if x)
    typer.echo(f"week {got['week']} ({got['timezone']})" + (f", {scope}" if scope else ""))
    y = m["yield"]
    typer.echo(f"  yield            {_pct(y['value'])} ({y['merged']}/{y['terminal']} counted terminal agent jobs)")
    a = m["agent_share"]
    typer.echo(f"  agent share      {_pct(a['value'])} ({a['agent_merged']}/{_num(a['all_merged'])} merged PRs)")
    r = m["review_wait"]
    typer.echo(f"  review wait      p50 {_num(r['p50_hours'], ' h')}, p90 {_num(r['p90_hours'], ' h')} (n={r['n']})")
    typer.echo(f"  green-to-merge   p50 {_num(m['green_to_merge']['p50_hours'], ' h')}")
    s = m["steer_rate"]
    typer.echo(f"  steer rate       {_num(s['value'])} per merged PR (corrections {_num(s['corrections'])}, "
               f"unclassified {s['unclassified']})")
    ab = m["abandon_rate"]
    reasons = ", ".join(f"{k} {v}" for k, v in ab["by_reason"].items())
    typer.echo(f"  abandon rate     {_pct(ab['value'])} ({ab['abandoned']}/{ab['terminal']})"
               + (f": {reasons}" if reasons else ""))
    rv = m["revert_rate"]
    typer.echo(f"  revert rate      {_pct(rv['value'])} ({rv['reverted']}/{rv['cohort']} merged 14 days earlier)")
    c = m["cost_per_merged_pr"]
    typer.echo(f"  cost / merged PR {'$' + format(c['usd'], '.2f') if c['usd'] is not None else '-'}")
    ra = m["reviewer_attention"]
    typer.echo(f"  review attention {_num(ra['events_per_merged_pr'])} review events per merged PR "
               f"({ra['events']} over {ra['measured']}/{ra['merged']} merged PRs read; p50 {_num(ra['p50'])}, "
               f"p90 {_num(ra['p90'])})")
    typer.echo(f"  review minutes   {_num(ra['active_minutes'])}" + (f" ({ra['active_minutes_note']})"
                                                                     if ra["active_minutes"] is None else ""))
    typer.echo(f"  active delegators {m['active_delegators']['value']}")
    if m["excluded_infra_error"]:
        typer.echo(f"  ({m['excluded_infra_error']} infra_error job(s) left out)")


@metrics_app.command("standup")
def standup():
    """The standup line as it would be posted now."""
    typer.echo(_cli()._call("GET", "/api/metrics/standup")["line"])


def register(app: typer.Typer) -> None:
    app.add_typer(metrics_app, name="metrics")
