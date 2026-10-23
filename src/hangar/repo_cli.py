"""`hangar repo`: per-repo policy, the defaults every front door applies (--- frontdoor, repopolicy.py)."""

from __future__ import annotations

import json

import typer

repo_app = typer.Typer(no_args_is_help=True, help="Per-repo defaults for jobs from every front door "
                                                  "(verify, environment, harness, model, budgets).")


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


def _fields(**kw) -> dict | None:
    """A budget from the options given; it replaces the policy's whole budget of that kind."""
    out = {k: v for k, v in kw.items() if v is not None}
    return out or None


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
    if jb := p.get("job_budget"):
        bits.append("job budget: " + ", ".join(f"{k.removeprefix('max_')}={v}" for k, v in jb.items() if v))
    if p.get("credentials"):  # --- teamcreds
        bits.append(f"credentials: {p['credentials']}")
    if p.get("max_usd_per_day"):
        bits.append(f"daily cap: ${p['max_usd_per_day']:g} (${p.get('spent_today_usd') or 0:.2f} today)")
    if eg := p.get("egress"):  # --- egress
        bits.append("egress: " + (eg.get("mode") or "env") + (f" +{','.join(eg['allow'])}" if eg.get("allow") else ""))
    if p.get("land") == "auto_after_approval":  # --- landing
        who = ", ".join(p.get("approvers") or []) or "code owners"
        bits.append(f"land: on approval by {who} ({p.get('merge_method') or 'squash'})")
        if p.get("require_stale_dismissal") is False:  # --- trust
            bits.append("(stale approvals need not be dismissed)")
    if p.get("verify_reports"):  # --- testresults
        bits.append("reports: " + ",".join(p["verify_reports"]))
    if p.get("rerun_flaky"):
        bits.append("reruns flaky failures")
    if p.get("heavy") is not None or p.get("profile") or p.get("max_profile"):  # --- heavy
        bits.append("heavy: " + {"on": "on", "allow": "allow (the repo's file may ask)", "deny": "deny",
                                 None: "deny (default)"}.get(p.get("heavy"), str(p.get("heavy")))
                    + (f", profile {p['profile']}" if p.get("profile") else "")
                    + (f", up to {p['max_profile']}" if p.get("max_profile") else ""))
    if p.get("max_open_agent_prs") is not None:  # --- review capacity (#159)
        bits.append(f"open agent PRs per person: {p['max_open_agent_prs'] or 'no cap'}")
    if p.get("self_approval"):  # --- self-approval (#160)
        bits.append(f"self-approval: {p['self_approval']}")
    if p.get("ai_review") and p["ai_review"] != "off":  # --- AI review (#161)
        bits.append(f"AI review: {p['ai_review']}" + (f" ({p['ai_review_harness']})" if p.get("ai_review_harness")
                                                     else ""))
    if p.get("branch_template"):  # --- change sets (G7)
        bits.append(f"branch: {p['branch_template']}")
    if p.get("require_ticket") is not None:
        bits.append("ticket required" if p["require_ticket"] else "ticket not required")
    if p.get("title_template"):
        bits.append(f"PR title: {p['title_template']}")
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
    max_cost: float | None = typer.Option(None, "--max-cost", help="Budget per agent turn: USD."),
    max_tokens: int | None = typer.Option(None, "--max-tokens", help="Budget per agent turn: tokens."),
    max_minutes: int | None = typer.Option(None, "--max-minutes", help="Budget per agent turn: minutes."),
    job_max_cost: float | None = typer.Option(None, "--job-max-cost", help="Job budget: USD for all its turns."),
    job_max_tokens: int | None = typer.Option(None, "--job-max-tokens", help="Job budget: tokens for all its turns."),
    job_max_turns: int | None = typer.Option(None, "--job-max-turns", help="Job budget: agent turns in all."),
    max_usd_per_day: float | None = typer.Option(None, "--max-usd-per-day", help="Daily cap on the repo's spend "
                                                 "(UTC days); new jobs over it wait for approval."),
    credentials: str | None = typer.Option(None, "--credentials", help="Whose login jobs use: person (the person "
                                           "the job runs for), team, or person_then_team (the default)."),
    egress_mode: str | None = typer.Option(None, "--egress-mode", help="--- egress: allowlist or none; tightens "
                                           "the environment's mode for this repo's sandboxes."),
    egress_allow: list[str] = typer.Option([], "--egress-allow", help="A host the repo's sandboxes may reach "
                                           "(host, host:port or *.domain; repeat it). Replaces the list."),
    # --- landing (docs/landing.md)
    land: str | None = typer.Option(None, "--land", help="manual, or auto_after_approval: an approval from an "
                                                         "approver or code owner lands the PR once checks pass."),
    approver: list[str] = typer.Option([], "--approver", help="GitHub login whose approval lands the PR (repeat)."),
    merge_method: str | None = typer.Option(None, "--merge-method", help="squash (default), merge or rebase."),
    risk_path: list[str] = typer.Option([], "--risk-path", help="Glob that makes a PR high risk, e.g. 'infra/**' "
                                                                "(repeat)."),
    require_stale_dismissal: bool | None = typer.Option(  # --- trust
        None, "--require-stale-dismissal/--no-require-stale-dismissal",
        help="Arm auto-merge only when the base branch dismisses stale approvals on a push (the default); --no- "
             "opts the repo out."),
    verify_report: list[str] = typer.Option([], "--verify-report", help="A test or coverage report `verify` writes "
                                            "(JUnit, Cobertura, LCOV), relative to the repo, e.g. "
                                            "'build/**/TEST-*.xml' (repeat it). Replaces the list."),  # --- testresults
    rerun_flaky: bool | None = typer.Option(None, "--rerun-flaky/--no-rerun-flaky",
                                            help="Run the check once more when it failed only on tests on the "
                                                 "repo's flaky list."),
    heavy: str | None = typer.Option(None, "--heavy", help="Docker on the heavy tier (off unless allowed): on (the "
                                     "repo's sandboxes have it), allow (its .hangar/environment.yaml may ask for "
                                     "it) or deny (the default)."),
    profile: str | None = typer.Option(None, "--profile", help="standard, large or xlarge: the size of the repo's "
                                       "sandboxes (large and xlarge run on the heavy tier)."),
    max_profile: str | None = typer.Option(None, "--max-profile", help="The largest profile a job (label "
                                           "hangar.profile) or the repo's .hangar/environment.yaml may ask for."),
    allow_heavy_for_intake: bool | None = typer.Option(
        None, "--allow-heavy-for-intake/--no-allow-heavy-for-intake",
        help="Let jobs from GitHub, Slack, Jira and webhooks run Docker on the heavy tier for this repo (#124)."),
    max_open_agent_prs: int | None = typer.Option(None, "--max-open-agent-prs", help="Open agent PRs awaiting a "
                                                  "person before their new jobs here wait (0 = off; #159)."),
    self_approval: str | None = typer.Option(None, "--self-approval", help="May the job's own person be its PR's "
                                             "only approver: never, low_risk_with_ai_review (the default) or any "
                                             "(#160). A tainted PR always needs someone else."),
    ai_review: str | None = typer.Option(None, "--ai-review", help="First-pass review of agent PRs: hangar (a job "
                                         "on another harness), check:<name> (an org review bot's check) or off "
                                         "(#161)."),
    ai_review_harness: str | None = typer.Option(None, "--ai-review-harness", help="The reviewing harness (default: "
                                                 "codex for a claude-code PR, else claude-code)."),
    ai_review_model: str | None = typer.Option(None, "--ai-review-model", help="The reviewer's model or policy "
                                               "(default frontier)."),
    ai_review_budget: float | None = typer.Option(None, "--ai-review-budget", help="USD one AI review may spend "
                                                  "(default 1.50)."),
    verify_allowed: list[str] = typer.Option([], "--verify-allowed", help="A glob a ticket's own verify command "
                                             "may match to become its job's check, e.g. 'make test*' (repeat it). "
                                             "Replaces the list."),  # --- #156
    overnight: bool | None = typer.Option(None, "--overnight/--no-overnight", help="Its intake jobs run at night "
                                          "in the overnight lane (#186; hangar power overnight on)."),
    # --- change sets (G7, KAN-37): the company's naming conventions
    branch_template: str | None = typer.Option(None, "--branch-template", help="The repo's job branches, e.g. "
                                               "'feature/{ticket_key}-{job}'; it must contain {job}. '' clears it "
                                               "(hangar/<job>)."),
    require_ticket: bool | None = typer.Option(None, "--require-ticket/--no-require-ticket",
                                               help="Refuse a job for the repo that names no ticket."),
    title_template: str | None = typer.Option(None, "--title-template", help="The title of the repo's agent PRs, "
                                              "e.g. '{ticket_key}: {title}'. '' clears it."),
    as_json: bool = typer.Option(False, "--json"),
):
    """Set the repo's defaults; what you don't pass is kept. Jobs that set a field themselves still win.

    Example:  hangar repo set acme/app --verify "uv run pytest -q" --env python --model claude-code=opus"""
    body: dict = {"repo": repo}
    for k, v in (("verify", verify), ("environment", env), ("harness", harness), ("model", _models(model)),
                 ("max_usd_per_day", max_usd_per_day), ("credentials", credentials),  # --- teamcreds
                 ("budget", _fields(max_cost_usd=max_cost, max_tokens=max_tokens, max_minutes=max_minutes)),
                 ("job_budget", _fields(max_cost_usd=job_max_cost, max_tokens=job_max_tokens,
                                        max_turns=job_max_turns)),
                ("land", land), ("approvers", approver or None), ("merge_method", merge_method),  # --- landing
                ("risk_paths", risk_path or None),
                ("require_stale_dismissal", require_stale_dismissal),  # --- trust
                ("verify_reports", verify_report or None), ("rerun_flaky", rerun_flaky),  # --- testresults
                ("heavy", heavy), ("profile", profile), ("max_profile", max_profile),  # --- heavy
                ("allow_heavy_for_intake", allow_heavy_for_intake),  # --- #124
                ("max_open_agent_prs", max_open_agent_prs), ("self_approval", self_approval),  # --- #159, #160
                ("ai_review", ai_review), ("ai_review_harness", ai_review_harness),  # --- #161
                ("ai_review_model", ai_review_model), ("ai_review_budget_usd", ai_review_budget),
                ("verify_allowed", verify_allowed or None),  # --- #156
                ("overnight", overnight),  # --- #186
                ("require_ticket", require_ticket)):  # --- change sets (G7)
        if v is not None:
            body[k] = v
    for k, t in (("branch_template", branch_template), ("title_template", title_template)):  # --- change sets
        if t is not None:
            body[k] = t or None  # '' clears it: an explicit null, as `hangar repo unset` would
    if egress_mode is not None or egress_allow:  # --- egress
        body["egress"] = {k: v for k, v in (("mode", egress_mode), ("allow", egress_allow)) if v}
    if len(body) == 1:
        raise typer.BadParameter("pass at least one setting (see --help)")
    p = _cli()._call("PUT", "/api/repos/policy", json=body)
    if as_json:
        return _cli()._print(p)
    typer.secho(_line(p), fg="green")


@repo_app.command("unset")
def repo_unset(repo: str = typer.Argument(...),
               fields: list[str] = typer.Argument(None, help="Fields to clear (verify, environment, harness, "
                                                             "model, budget, job_budget, max_usd_per_day, "
                                                             "credentials, egress, build_secrets, land, approvers, "
                                                             "merge_method, risk_paths, "
                                                             "require_stale_dismissal, verify_reports, "
                                                             "rerun_flaky, heavy, profile, "
                                                             "max_profile, allow_heavy_for_intake, "
                                                             "max_open_agent_prs, self_approval, ai_review, "
                                                             "ai_review_harness, ai_review_model, "
                                                             "ai_review_budget_usd, verify_allowed, "
                                                             "overnight, branch_template, require_ticket, "
                                                             "title_template). None: "
                                                             "remove the repo's policy.")):
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


@repo_app.command("check")
def repo_check(repo: str = typer.Argument(..., help="owner/repo on GitHub."),
               as_json: bool = typer.Option(False, "--json")):
    """Is the repo ready for agent work? Landing, the git gateway, CI and the doors (GitHub, Jira, Slack), each ok,
    warn or fail with the fix. Exits 1 when anything fails. Read-only (repocheck.py, #155).

    Example:  hangar repo check acme/app"""
    from hangar.github import parse_repo

    name = repo.strip().removesuffix(".git")
    if "/" not in name or name.count("/") != 1:
        name = parse_repo(repo) or ""
    owner, _, rest = name.partition("/")
    if not owner or not rest:
        raise typer.BadParameter("use owner/repo, e.g. acme/app", param_hint="REPO")
    r = _cli()._call("GET", f"/api/repos/{owner}/{rest}/readiness")
    if as_json:
        _cli()._print(r)
    else:
        marks = {"ok": ("✓", "green"), "warn": ("!", "yellow"), "fail": ("✗", "red")}
        typer.echo(f"{r['repo']}:")
        for c in r["checks"]:
            mark, color = marks.get(c["status"], ("?", None))
            typer.secho(f"{mark} {c['check']}: {c['detail']}", fg=color)
            if c.get("fix"):
                typer.echo(f"    fix: {c['fix']}")
        typer.secho(f"\n{r['summary']}" + (f", {r['warnings']} warning(s)" if r.get("warnings") else ""),
                    fg="green" if r["ready"] else "red")
    raise typer.Exit(0 if r["ready"] else 1)
