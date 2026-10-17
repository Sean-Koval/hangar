"""CLI for the sister tool's interfaces (--- interfaces; spec 0014): `hangar trace export`, `hangar session|job|env
manifest`, and `hangar experiment run|get|cancel|export|freeze-variants`."""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

import typer
import yaml

trace_app = typer.Typer(no_args_is_help=True, help="Traces of runs, sessions and jobs as OTLP/JSON (hangar.trace/v1).")
experiment_app = typer.Typer(no_args_is_help=True,
                             help="Experiments: tasks x variants x trials, pinned (an index of trials).")
_JOB = re.compile(r"^job-[0-9a-f]{8}$")


def _out(obj: dict, output: Path | None) -> None:
    from hangar import cli

    if output is None:
        return cli._print(obj)
    output.write_text(json.dumps(obj, indent=1, default=str))
    typer.echo(f"wrote {output}")


def trace_export(
    ref: str = typer.Argument(..., help="A session name or id, or a job id (job-...)."),
    turn: int | None = typer.Option(None, "--turn", help="One run: the session's turn N (1-based)."),
    output: Path | None = typer.Option(None, "-o", "--output", help="Write to this file instead of stdout."),
):
    """Export a run, a session or a job as OTLP/JSON (docs/api/traces.md). POST it to a collector's /v1/traces, or
    open it in the sister tool."""
    from hangar import cli

    if _JOB.match(ref):
        if turn is not None:
            raise typer.BadParameter("--turn is for sessions; a job's export has all its turns")
        return _out(cli._call("GET", f"/api/jobs/{ref}/trace"), output)
    return _out(cli._call("GET", f"/api/sessions/{ref}/trace", params={"turn": turn} if turn else None), output)


def session_manifest(
    ref: str = typer.Argument(..., help="Session name or id."),
    turn: int | None = typer.Option(None, "--turn", help="Turn N (1-based); default: the latest captured."),
    output: Path | None = typer.Option(None, "-o", "--output"),
):
    """The effective harness manifest a session's turn ran with (hangar.manifest/v1)."""
    from hangar import cli

    _out(cli._call("GET", f"/api/sessions/{ref}/manifest", params={"turn": turn} if turn else None), output)


def job_manifest(
    job_id: str = typer.Argument(...),
    turn: int | None = typer.Option(None, "--turn", help="The job's turn N (1-based); default: the latest."),
    output: Path | None = typer.Option(None, "-o", "--output"),
):
    """The effective harness manifest a job's turn ran with (hangar.manifest/v1)."""
    from hangar import cli

    _out(cli._call("GET", f"/api/jobs/{job_id}/manifest", params={"turn": turn} if turn else None), output)


def env_manifest(
    name: str = typer.Argument(..., help="Environment name."),
    harness: str | None = typer.Option(None, "--harness", help="Default: the environment's default harness."),
    output: Path | None = typer.Option(None, "-o", "--output"),
):
    """What a new sandbox of the environment would run the harness with (hangar.manifest/v1)."""
    from hangar import cli

    _out(cli._call("GET", f"/api/environments/{name}/manifest", params={"harness": harness} if harness else None),
         output)


def _print_index(v: dict) -> None:
    counts = ", ".join(f"{n} {k}" for k, n in sorted((v.get("counts") or {}).items()))
    typer.secho(f"{v['id']}  {v.get('name')}  {v['state']}  ({counts})", bold=True)
    for k, p in (v.get("pins") or {}).get("variants", {}).items():
        typer.echo(f"  variant {k}: {p.get('harness')}@{p.get('harness_version') or '?'} on {p.get('model')}, "
                   f"approvals {p.get('approvals')}")
    for t in v.get("trials") or []:
        verify = (t.get("verify") or {}).get("passed")
        grade = (t.get("grade") or {}).get("exit_code")
        ref = t["key"] + (f"~{t['attempt']}" if (t.get("attempt") or 1) > 1 else "")  # --- #166: a retry
        typer.echo(f"  {ref:40} {t['state']:9} {t.get('outcome') or '':10} verify={verify} "
                   f"grade={grade} {t.get('job') or ''}" + ("  DRIFT" if t.get("drift") else "")
                   + (f"  EXCLUDED (infra_error/{t.get('infra_reason')})" if t.get("excluded") else ""))
    for n in v.get("notes") or []:
        typer.secho(f"  note: {n}", fg="yellow")


def experiment_run(
    spec_file: Path = typer.Argument(..., exists=True, readable=True, help="YAML or JSON experiment spec."),
    wait: bool = typer.Option(False, "--wait", help="Wait until every trial has finished."),
    as_json: bool = typer.Option(False, "--json"),
):
    """Start an experiment: every task x variant x trial as its own job, under pinned conditions."""
    from hangar import cli

    body = yaml.safe_load(spec_file.read_text())
    v = cli._call("POST", "/api/experiments", json=body)
    while wait and v["state"] not in ("succeeded", "failed", "cancelled"):
        time.sleep(10)
        v = cli._call("GET", f"/api/experiments/{v['id']}")
    return cli._print(v) if as_json else _print_index(v)


def experiment_get(exp_id: str = typer.Argument(...), as_json: bool = typer.Option(False, "--json")):
    """The experiment's index: pins, and per trial its job, verification, grade and stored files."""
    from hangar import cli

    v = cli._call("GET", f"/api/experiments/{exp_id}")
    return cli._print(v) if as_json else _print_index(v)


def experiment_cancel(exp_id: str = typer.Argument(...)):
    """Stop starting trials and cancel the running ones."""
    from hangar import cli

    _print_index(cli._call("POST", f"/api/experiments/{exp_id}/cancel"))


def experiment_export(
    exp_id: str = typer.Argument(...),
    out: Path = typer.Option(..., "-o", "--output", help="Directory for index.json and trials/<key>/*.json"),
):
    """Everything the sister tool reads about an experiment, as files. Superseded by the outbox
    (HANGAR_OUTBOX_URL, docs/api/outbox.md), which captures every trial's runs as they finish."""
    from hangar import cli

    v = cli._call("GET", f"/api/experiments/{exp_id}")
    out.mkdir(parents=True, exist_ok=True)
    (out / "index.json").write_text(json.dumps(v, indent=1, default=str))
    n = 0
    for t in v.get("trials") or []:
        d = out / "trials" / (t["key"].replace("/", "__") + (f"~{t['attempt']}" if (t.get("attempt") or 1) > 1
                                                              else ""))
        for name, link in (t.get("links") or {}).items():
            if name in ("manifest", "trace") and link:
                d.mkdir(parents=True, exist_ok=True)
                (d / f"{name}.json").write_text(json.dumps(cli._call("GET", link), indent=1, default=str))
                n += 1
    typer.echo(f"wrote {out}/index.json and {n} trial file(s)")


def experiment_freeze_variants(
    exp_id: str = typer.Argument(..., help="An experiment whose variants use the (deprecated) overlay fields."),
    out: Path = typer.Option(..., "--out", help="Where the harness directories go, one per variant "
                                                "(<out>/<experiment>-<variant>/): the harness repo's harnesses/."),
    spec_out: Path | None = typer.Option(None, "-o", "--output", help="Write the locked spec here instead of "
                                                                      "printing it."),
):
    """Freeze each overlay variant from a completed trial (turn 1, `hangar harness freeze`) into a harness
    directory, and print the equivalent spec with `harness_lock: name@sha256:...` variants (#135). Commit the
    directories to the harness registry (HANGAR_HARNESS_REPO) before running the printed spec."""
    from hangar import cli, experiments, harness_lock

    v = cli._call("GET", f"/api/experiments/{exp_id}")
    spec, locks = v.get("spec") or {}, {}
    for vname, var in (spec.get("variants") or {}).items():
        if (var or {}).get("harness_lock"):
            continue  # already a lock
        t = experiments.freeze_source(v, vname)
        if t is None:
            typer.secho(f"variant {vname}: no finished trial with a captured turn 1 to freeze from", fg="red", err=True)
            raise typer.Exit(1)
        name = experiments.lock_name(spec.get("name") or exp_id, vname)
        d = cli._call("GET", f"/api/jobs/{t['job']}/harness", params={"turn": 1, "name": name})
        root = out / name
        prev = harness_lock.load(root) if (root / harness_lock.LOCK_FILE).exists() else None
        lock, files = harness_lock.unwire(d)
        lock["revision"] = harness_lock.next_revision(prev, lock)
        harness_lock.write(root, lock, files, prev)
        res = harness_lock.check(root)
        if not res["ok"] or res["digest"] != d["harness_digest"]:
            for p in res["problems"] or [f"{root}: digest {res['digest']} isn't the trial's {d['harness_digest']}"]:
                typer.secho(p, fg="red", err=True)
            raise typer.Exit(1)
        for w in d.get("warnings") or []:
            typer.secho(f"warning: variant {vname}: {w}", fg="yellow", err=True)
        locks[vname] = f"{name}@{d['harness_digest']}"
        typer.secho(f"variant {vname}: froze trial {t['key']} ({t['job']}) into {root} r{lock['revision']}",
                    err=True)
    text = yaml.safe_dump(experiments.locked_spec(spec, locks), sort_keys=False)
    if spec_out is None:
        typer.echo(text, nl=False)
    else:
        spec_out.write_text(text)
        typer.echo(f"wrote {spec_out}")


def register(app: typer.Typer, session_app: typer.Typer, job_app: typer.Typer, env_app: typer.Typer) -> None:
    trace_app.command("export")(trace_export)
    app.add_typer(trace_app, name="trace")
    session_app.command("manifest")(session_manifest)
    job_app.command("manifest")(job_manifest)
    env_app.command("manifest")(env_manifest)
    experiment_app.command("run")(experiment_run)
    experiment_app.command("get")(experiment_get)
    experiment_app.command("cancel")(experiment_cancel)
    experiment_app.command("export")(experiment_export)
    experiment_app.command("freeze-variants")(experiment_freeze_variants)  # --- #135
    app.add_typer(experiment_app, name="experiment")
