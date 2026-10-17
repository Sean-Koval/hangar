"""`hangar review queue|label|status`: the Friday harness review (--- #169; docs/runbooks/harness-review.md).
Registered by cli.py."""

from __future__ import annotations

from urllib.parse import quote

import typer

review_app = typer.Typer(help="The Friday harness review: label the first failure of failed and abandoned jobs.",
                         no_args_is_help=True)


def _cli():
    from hangar import cli

    return cli


@review_app.command("queue")
def queue(since: str = typer.Option("7d", "--since", help="How far back: 7d, 12h."),
          include_infra: bool = typer.Option(False, "--include-infra", help="Also infra_error jobs (#166)."),
          json_out: bool = typer.Option(False, "--json")):
    """Failed jobs, abandoned PRs and jobs with 2+ correction steers, newest first, not yet labelled."""
    cli = _cli()
    got = cli._call("GET", "/api/review/queue", params={"since": since, "include_infra": include_infra})
    if json_out:
        cli._print(got)
        return
    if got.get("note"):
        typer.secho(got["note"], fg="green")
    st = got["status"]
    typer.echo(f"labelled: {st['candidate_tasks']}/{st['goal']} candidate tasks ({st['labelled']} labels)")
    if not got["rows"]:
        typer.echo("nothing to label")
    for r in got["rows"]:
        typer.echo(f"{r['job']}  [{', '.join(r['why'])}]  {r['title'][:70]}")
        typer.echo(f"    {r['repo'] or '-'}  {r['person'] or '-'}  {r['reason'] or r['abandon_reason'] or ''}")
        seq = f"  suggested --seq {r['suggested_seq']}" if r.get("suggested_seq") else ""
        typer.echo(f"    trace {r['links']['trace']}  timeline {r['links']['timeline'] or '-'}{seq}")


@review_app.command("label")
def label(job: str = typer.Argument(...),
          seq: int = typer.Option(None, "--seq", help="hangar.seq of the first failure (the timeline's seq)."),
          category: str = typer.Option(..., "--category",
                                       help="env | tooling | instructions | skill | model | ticket | flaky | other"),
          fix: str = typer.Option("none", "--fix", help="agents_md | skill | env | ticket_template | none"),
          note: str = typer.Option("", "--note"),
          task: bool = typer.Option(False, "--task", help="A candidate regression task for flightdeck.")):
    """Label a job's first failure (relabelling replaces the label, audited)."""
    got = _cli()._call("POST", f"/api/jobs/{quote(job, safe='')}/failure-label",
                       json={"seq": seq, "category": category, "fix": fix, "note": note, "candidate_task": task})
    st = got["status"]
    typer.echo(f"labelled {job}: {got['label']['category']} (fix {got['label']['fix']})"
               + (", candidate task" if got["label"]["candidate_task"] else ""))
    typer.echo(f"labelled: {st['candidate_tasks']}/{st['goal']}")
    if got.get("note"):
        typer.secho(got["note"], fg="green")


@review_app.command("status")
def status(json_out: bool = typer.Option(False, "--json")):
    """The count toward 20 candidate tasks, and labels by category."""
    cli = _cli()
    st = cli._call("GET", "/api/review/status")
    if json_out:
        cli._print(st)
        return
    typer.echo(f"labelled: {st['candidate_tasks']}/{st['goal']} candidate tasks ({st['labelled']} labels)")
    for k, v in st["categories"].items():
        typer.echo(f"  {k:<13} {v}")
    if st["reached"]:
        typer.secho(f"{st['goal']} reached: hand over to flightdeck", fg="green")


def register(app: typer.Typer) -> None:
    app.add_typer(review_app, name="review")
