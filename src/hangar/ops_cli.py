"""`hangar status`, `hangar admin records` and `hangar cred check` (--- clouddoctor; opstatus.py, flightrec.py,
credhealth.py). Registered by cli.py. Admins."""

from __future__ import annotations

import time
from datetime import datetime

import typer

records_app = typer.Typer(no_args_is_help=True, help="Flight records of failed, timed-out and slow jobs and turns.")
_MARK = {"ok": ("✓", "green"), "warn": ("!", "yellow"), "fail": ("✗", "red"), "unknown": ("?", "yellow")}


def _cli():
    from hangar import cli

    return cli


def _ago(ts: float | None) -> str:
    if not ts:
        return "-"
    s = int(max(0, time.time() - ts))
    return f"{s}s" if s < 120 else f"{s // 60}m" if s < 7200 else f"{s // 3600}h" if s < 172800 else f"{s // 86400}d"


def _secs(x: float | None) -> str:
    return "-" if x is None else f"{x:.1f}s" if x < 120 else f"{x / 60:.1f}m"


def status(as_json: bool = typer.Option(False, "--json"),
           section: str | None = typer.Option(None, help="Only these sections, comma-separated: power, loops, "
                                              "harnesses, builds, failures, egress, requests, slo, credentials, "
                                              "nightly, log, models (and doctor, tests, caches)."),
           check_credentials: bool = typer.Option(False, "--check-credentials",
                                                  help="Check every credential now, not only stale results.")):
    """What an operator needs at a glance: power, loops, versions, builds, failures, denies, requests, SLOs."""
    cli = _cli()
    params: dict[str, str] = {"check_credentials": str(check_credentials).lower()}
    if section:
        params["section"] = section
    v = cli._call("GET", "/api/status", params=params)
    if as_json:
        return cli._print(v)
    if p := v.get("power"):
        line = {"up": "on", "down": "powered down", "starting": "powering on"}.get(p["state"], p["state"])
        typer.secho(f"sandboxes: {line}" + (f"; ready at {p['when']}" if p["state"] != "up" and p.get("when") else "")
                    + (f"; {p['jobs_waiting']} job(s) waiting" if p.get("jobs_waiting") else ""),
                    fg="green" if p["state"] == "up" else "yellow")
        from hangar.power_cli import hold_lines  # --- infra-state: the hold, and a deploy waiting for a power-up

        for line in hold_lines(p):
            typer.secho(line, fg="yellow")
    if d := v.get("last_deploy"):
        typer.echo(f"last deploy: {_ago(d['at'])} ago, {d.get('version') or d.get('image', '')}: "
                   f"{d['result'].replace('_', ' ')}")
    if loops := v.get("loops"):
        late = [n for n, x in loops.items() if not x["ok"]]
        typer.secho("loops: " + ", ".join(f"{n} {_ago(x.get('last'))}" for n, x in sorted(loops.items()))
                    + (f"  (overdue: {', '.join(late)})" if late else ""), fg="yellow" if late else None)
    if hs := v.get("harnesses"):
        typer.echo("harnesses: " + ", ".join(
            f"{n} {x['locked']}" + (f" (pack behind on {', '.join(x['behind'])})" if x["behind"] else "")
            for n, x in sorted(hs.items())))
    if b := v.get("builds"):
        running = [f"image {x['environment']}/{x['provider']} {_secs(x['seconds'])}" for x in b["images"]] + \
            [f"pack {x}" for x in b["packs"]] + \
            [f"repo {x['repo']} {x['environment']} {x['version']} ({x.get('stage') or x['state']})" for x in b["repos"]]
        typer.echo("builds: " + (", ".join(running) if running else "none running"))
    if f := v.get("failures"):
        typer.echo(f"failures (24h): {f['total']}, {f['transient']} transient")
        for cause, x in f["by_cause"].items():
            typer.echo(f"  {cause:14} {x['count']:4}  {'transient' if x['transient'] else '         '}  last "
                       f"{_ago(x['last_at'])} ago ({x['last']})  {', '.join(x['environments'][:4])}")
    if e := v.get("egress_denies"):
        typer.echo(f"egress denies (24h): {e['total']}")
        for x in e["by_host"][:8]:
            typer.echo(f"  {x['host']:40} {x['count']:5}  {x['sandboxes']} sandbox(es)  {', '.join(x['environments'])}")
    if r := v.get("requests"):
        typer.echo(f"requests waiting: {r['pending']}" + (f", oldest {_ago(time.time() - r['oldest_seconds'])}"
                                                          if r.get("oldest_seconds") else ""))
    if s := v.get("slo"):
        typer.echo("SLOs (24h):")
        for metric, paths in s.items():
            for path, x in paths.items():
                want = " ".join(f"{k[-3:]}<={x[k]}s" for k in ("target_p50", "target_p95") if x[k] is not None)
                mark = "" if x["ok"] is None else (" ok" if x["ok"] else " BREACHED")
                typer.secho(f"  {metric:10} {path:5} n={x['n']:<4} p50 {_secs(x['p50'])} p95 {_secs(x['p95'])}"
                            + (f"  ({want}){mark}" if want else ""), fg="red" if x["ok"] is False else None)
    if c := v.get("credentials"):
        typer.echo("credentials: " + (", ".join(f"{n} {k}" for k, n in sorted(c["counts"].items())) or "none"))
        for a in c["attention"]:
            mark, color = _MARK.get(a["status"], ("!", "yellow"))
            typer.secho(f"  {mark} {a['name']} ({a['owner']}): {a['why']}", fg=color)
        for p in c["pools"]:
            if len(p["logins"]) > 1:
                typer.echo(f"  pool {p['owner']}/{p['harness']}: {p['working']} of {len(p['logins'])} working, "
                           f"{p['leased']} in use")
    if m := v.get("models"):  # --- integ3 (review #105)
        bounds = [b for b in (f"${m['job_budget_usd']:g}/job" if m.get("job_budget_usd") else "",
                              f"${m['person_max_usd_per_day']:g}/person/day" if m.get("person_max_usd_per_day")
                              else "") if b]
        cap = ("NO job budget or daily cap (HANGAR_JOB_BUDGET_MAX_COST_USD, HANGAR_PERSON_MAX_USD_PER_DAY)"
               if m["uncapped"] else f"capped at {', '.join(bounds)}")
        typer.secho(f"models: jobs default to {m['job_default']} (Claude Code: {m['claude_code']}); {cap}"
                    + (f"; {len(m['repos_capped'])} repo(s) capped by policy" if m.get("repos_capped") else ""),
                    fg="yellow" if m["uncapped"] else None)
    if (bc := v.get("caches")) is not None:  # --- caches
        for line in cache_lines(bc):
            typer.echo(line)
    if doc := v.get("doctor"):
        last = doc.get("last_run") or {}
        typer.echo(f"cloud doctor: {doc.get('open', 0)} open finding(s)" +
                   (f"; last run {_ago(last.get('at'))} ago ({last.get('state')})" if last else ""))
    if (t := v.get("tests")) and t.get("repos"):  # --- testresults: the repos with the most flaky tests
        typer.echo(f"flaky tests (seen in the last {t['days']} days):")
        for r in t["repos"][:8]:
            top = ", ".join(f"{x['test']} ({x['times']}x)" for x in r["top"][:3])
            typer.echo(f"  {r['repo']:40} {r['flaky']:4}  {top[:140]}")
    if n := v.get("nightly"):
        typer.echo(f"nightly pass: {_ago(n['at'])} ago")
    for x in v.get("log") or []:
        when = datetime.fromtimestamp(x["at"]).strftime("%H:%M:%S")
        typer.secho(f"  {when} {x['level']:7} {x['logger']}: {x['message'][:160]}", fg="yellow")


def _gb(n: int | float) -> str:
    return f"{n / (1 << 30):.1f} GB" if n >= 1 << 30 else f"{n / (1 << 20):.0f} MB"


def cache_lines(bc: dict) -> list[str]:  # --- caches: `hangar status` and `hangar admin caches ls`
    """The build caches' sizes against their caps, and the biggest repos."""
    if not bc.get("enabled"):
        return [f"build caches: off ({bc.get('why') or bc.get('error') or '?'})"]
    s = bc.get("since_start") or {}
    out = [f"build caches: {_gb(bc['bytes'])} of {_gb(bc['cap_bytes'])} in {bc['repo_count']} repo(s) "
           f"(each up to {_gb(bc['repo_cap_bytes'])}); since start {s.get('hits', 0)} hits, {s.get('misses', 0)} "
           f"misses, {s.get('writes', 0)} writes, {s.get('denied', 0)} read-only refusals"
           + (f", {s['integrity_failures']} integrity failures" if s.get("integrity_failures") else "")]
    for r in bc.get("repos") or []:
        out.append(f"  {r['repo'][:48]:48} {_gb(r['bytes']):>9}  {r['entries']:6} entries  trusted "
                   f"{_gb(r['trusted_bytes'])}, private {_gb(r['private_bytes'])}  used {_ago(r['last_used'])} ago")
    return out


@records_app.command("ls")
def records_ls(hours: float = typer.Option(24, help="How far back."), cause: str | None = None,
               as_json: bool = typer.Option(False, "--json")):
    """Records of the last `hours`, newest first, and their causes."""
    cli = _cli()
    params: dict[str, str] = {"hours": str(hours)}
    if cause:
        params["cause"] = cause
    v = cli._call("GET", "/api/records", params=params)
    if as_json:
        return cli._print(v)
    if not v["records"]:
        return typer.echo("No failed, timed-out or slow runs.")
    for r in v["records"]:
        what = r.get("job_id") or r.get("session") or ""
        typer.echo(f"{r['id']:20} {_ago(r['at']):>4} {r['trigger']:9} {r['cause']:14} {what:18} {r['environment']}"
                   + (f"  {r['detail'][:60]}" if r.get("detail") else ""))


@records_app.command("show")
def records_show(record: str, show_log: bool = typer.Option(False, "--log", help="Print the run log's end too.")):
    """One record in full (JSON), and with --log the redacted end of its run log."""
    cli = _cli()
    cli._print(cli._call("GET", f"/api/records/{record}"))
    if show_log:
        with cli._client() as c:
            r = c.get(f"/api/records/{record}/log")
        if r.status_code >= 400:
            typer.secho(f"no log: {r.text[:300]}", fg="yellow", err=True)
        else:
            typer.echo(r.text)


def cred_check(names: list[str] = typer.Argument(None, help="Credentials to check (default: all)."),
               as_json: bool = typer.Option(False, "--json")):
    """Check credentials now, without calling a model: token age, codex login status, gcloud for service accounts."""
    cli = _cli()
    v = cli._call("POST", "/api/credentials/check", json={"names": names or None})
    if as_json:
        return cli._print(v)
    for r in v["results"]:
        mark, color = _MARK.get(r["status"], ("?", "yellow"))
        typer.secho(f"{mark} {r['name']}: {r['detail']}", fg=color)
    if not v["results"]:
        typer.echo("nothing to check")


doctor_app = typer.Typer(no_args_is_help=True, help="The Cloud Doctor: nightly root causes and fixes (docs/doctor.md).")


@doctor_app.command("run")
def doctor_run(wait: bool = typer.Option(False, "--wait", help="Wait for the run (not its jobs) to finish."),
               dry_run: bool = typer.Option(False, "--dry-run", help="Only say what a run would do; change nothing."),
               as_json: bool = typer.Option(False, "--json")):
    """Run the Cloud Doctor now over the records since its last run. Its root-cause jobs keep going after."""
    cli = _cli()
    if dry_run:  # --- integ3
        p = cli._call("GET", "/api/admin/doctor/plan")
        if as_json:
            return cli._print(p)
        typer.echo(f"dry run: {p['records']} new record(s) in {p['groups']} kind(s) since {_ago(p['since'])} ago; "
                   f"{p['open_fix_prs']} doctor PR(s) open; budget ${p['budget_usd']:g} a run")
        for i in p["items"]:
            typer.echo(f"  {i['action']:8} {i.get('cause') or '-':14} {i.get('repo') or i.get('environment') or '-'}"
                       f"  {i['why']}" + (f"  [blocked: {i['blocked']}]" if i.get("blocked") else "")
                       + (f"  ({i['harness']}, {i['model']})" if i.get("harness") else ""))
        return
    r = cli._call("POST", "/api/admin/doctor/run")
    typer.echo(f"run {r['id']}: {r.get('state', 'running')}")
    while wait and r.get("state") == "running":
        time.sleep(2)
        r = next((x for x in cli._call("GET", "/api/admin/doctor")["runs"] if x["id"] == r["id"]), r)
    if wait:
        _run_line(r)


def _run_line(r: dict) -> None:
    typer.echo(f"{r['id']} {_ago(r.get('at'))} ago by {r.get('by')}: {r.get('state')}; {r.get('records', 0)} new "
               f"record(s), {r.get('groups', 0)} kind(s), {r.get('transient', 0)} transient, root-cause jobs "
               f"{', '.join(r.get('rca_jobs') or []) or 'none'}" + (f"; {r['error']}" if r.get("error") else ""))


@doctor_app.command("ls")
def doctor_ls(all_: bool = typer.Option(False, "--all", help="Closed findings too."),
              as_json: bool = typer.Option(False, "--json")):
    """Open findings (and the last runs)."""
    cli = _cli()
    v = cli._call("GET", "/api/admin/doctor", params={"all": str(all_).lower()})
    if as_json:
        return cli._print(v)
    for r in v["runs"][:3]:
        _run_line(r)
    if not v["findings"]:
        return typer.echo("No findings.")
    for f in v["findings"]:
        where = f.get("pr_url") or f.get("request_id") or ""
        typer.echo(f"{f['id']:14} {f['state']:10} {f.get('confidence') or '-':6} {f['runs']:3} runs  {f['summary']}"
                   + (f"  {where}" if where else ""))
        if f.get("note"):
            typer.echo(f"{'':15}{f['note']}")


@doctor_app.command("show")
def doctor_show(finding: str):
    """One finding in full: its evidence, root cause, fix, and what was done about it."""
    cli = _cli()
    cli._print(cli._call("GET", f"/api/admin/doctor/findings/{finding}"))


@doctor_app.command("dismiss")
def doctor_dismiss(finding: str, note: str | None = typer.Option(None, help="Why (shown with the finding).")):
    """Close a finding the doctor shouldn't act on; a new failure of its kind opens a new one."""
    cli = _cli()
    f = cli._call("POST", f"/api/admin/doctor/findings/{finding}/dismiss", json={"note": note})
    typer.echo(f"{f['id']}: dismissed")


def register(app: typer.Typer, admin_app: typer.Typer, cred_app: typer.Typer) -> None:
    app.command("status")(status)
    admin_app.add_typer(records_app, name="records")
    admin_app.add_typer(doctor_app, name="doctor")
    admin_app.add_typer(caches_app, name="caches")  # --- caches
    cred_app.command("check")(cred_check)


caches_app = typer.Typer(no_args_is_help=True, help="Dependency and compile caches (docs/caches.md).")  # --- caches


@caches_app.command("ls")
def caches_ls(as_json: bool = typer.Option(False, "--json")):
    """Cache sizes per repo against the caps."""
    cli = _cli()
    v = cli._call("GET", "/api/caches")
    if as_json:
        return cli._print(v)
    for line in cache_lines(v):
        typer.echo(line)


@caches_app.command("purge")
def caches_purge(repo: str | None = typer.Argument(None, help="owner/repo or a git URL."),
                 layer: str | None = typer.Option(None, help="trusted or private (default: both)."),
                 all_repos: bool = typer.Option(False, "--all", help="Every repo's caches.")):
    """Delete a repo's caches (all of them, or one layer): after a suspected poisoning, or to start over."""
    cli = _cli()
    params = {k: v for k, v in (("repo", repo), ("layer", layer)) if v}
    if all_repos:
        params["all"] = "true"
    r = cli._call("DELETE", "/api/caches", params=params)
    typer.secho(f"purged {r['entries']} entr{'y' if r['entries'] == 1 else 'ies'} ({r['layer']}) of "
                f"{r['repo'] or 'every repo'}", fg="green")
