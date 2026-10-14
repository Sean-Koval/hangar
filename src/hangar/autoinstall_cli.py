"""`hangar env setup <repo>`: agent-led environment setup (--- autoinstall; autoinstall.py, docs/environments.md)."""

from __future__ import annotations

import typer


def _cli():
    from hangar import cli

    return cli


def register(env_app: typer.Typer) -> None:
    @env_app.command("setup")
    def env_setup(repo: str = typer.Argument(..., help="owner/repo or a git URL."),
                  env: str | None = typer.Option(None, "--env", "-e", help="The environment (default: as a job for "
                                                 "the repo picks it)."),
                  harness: str | None = typer.Option(None, help="The agents' harness (default: the environment's)."),
                  model: str | None = typer.Option(None, help="The agents' model (default: the strongest current "
                                                   "model of the harness's provider)."),
                  notes: str | None = typer.Option(None, help="What the agents should know (which services are "
                                                   "real, what to skip)."),
                  max_usd: float | None = typer.Option(None, "--max-usd", help="The job budget in USD (default: the "
                                                       "repo policy's, else HANGAR_ENV_SETUP_MAX_COST_USD)."),
                  drift: bool = typer.Option(False, "--drift", help="Only when the repo's environment is broken: no "
                                             "valid config, or its latest build failed."),
                  every: str | None = typer.Option(None, "--every", help="Admin: a cron schedule (\"0 9 * * 1\") "
                                                   "that re-runs it with --drift instead of running it now."),
                  wait: bool = typer.Option(False, "--wait", "-w", help="Follow it until it finishes.")):
        """Have agents work out how the repo builds and tests, write a validated .hangar/environment.yaml for it, and
        open a PR with it (plus an AGENTS.md section and a review packet)."""
        from hangar import autoinstall

        cli = _cli()
        budget = {"max_cost_usd": max_usd} if max_usd else None
        if every:
            body = autoinstall.request(repo, notes, "drift", environment=env, harness=harness, model=model,
                                       job_budget=budget)
            name = "env-setup-" + "".join(ch if ch.isalnum() else "-" for ch in repo.lower().split("/")[-1])[:40]
            s = cli._call("POST", "/api/schedules", json={"name": name.removesuffix(".git").strip("-"),
                                                          "cron": every, "job": body})
            typer.echo(f"schedule {s['name']}: {s['cron']} ({s.get('timezone', 'UTC')}), next {s.get('next_run')}; "
                       "each run sets the repo up again only when its environment is broken")
            return
        j = cli._call("POST", "/api/env/setup", json={
            "repo": repo, "environment": env, "harness": harness, "model": model, "notes": notes,
            "mode": "drift" if drift else "setup", "job_budget": budget})
        typer.echo(f"env setup {j['id']} of {repo} ({j['environment']}): {j['state']}")
        if not wait:
            typer.echo(f"follow it: hangar job get {j['id']} --wait")
            return
        j = cli._wait_job(j["id"])
        r = j.get("result") or {}
        if r.get("summary"):
            typer.echo("\n" + r["summary"])
        if j["state"] == "needs_input":
            typer.echo(f"answer it: hangar job follow-up {j['id']} \"<your answer>\"")
        elif j["state"] != "succeeded":
            raise typer.Exit(1)
