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
                                              "nightly, log (and doctor)."),
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
    if doc := v.get("doctor"):
        last = doc.get("last_run") or {}
        typer.echo(f"cloud doctor: {doc.get('open', 0)} open finding(s)" +
                   (f"; last run {_ago(last.get('at'))} ago ({last.get('state')})" if last else ""))
    if n := v.get("nightly"):
        typer.echo(f"nightly pass: {_ago(n['at'])} ago")
    for x in v.get("log") or []:
        when = datetime.fromtimestamp(x["at"]).strftime("%H:%M:%S")
        typer.secho(f"  {when} {x['level']:7} {x['logger']}: {x['message'][:160]}", fg="yellow")


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
def doctor_run(wait: bool = typer.Option(False, "--wait", help="Wait for the run (not its jobs) to finish.")):
    """Run the Cloud Doctor now over the records since its last run. Its root-cause jobs keep going after."""
    cli = _cli()
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
    cred_app.command("check")(cred_check)
