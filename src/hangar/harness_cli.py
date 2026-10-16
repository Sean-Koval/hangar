"""`hangar harness`: harness locks (#133; docs/harness-lock.md). `freeze` writes a harness directory (native files +
`harness.lock`) from a turn that went well; `check` recomputes a directory's digests against its lock."""

from __future__ import annotations

import json
import re
from pathlib import Path

import typer

harness_app = typer.Typer(no_args_is_help=True, help="Harness locks: freeze a turn into native files + harness.lock, "
                                                     "and check a harness directory against its lock.")
_JOB = re.compile(r"^job-[0-9a-f]{8}$")


def _report(res: dict, as_json: bool) -> None:
    if as_json:
        typer.echo(json.dumps(res, indent=2))
    elif res["ok"]:
        typer.echo(f"ok: {res['name']} r{res['revision']}, {res['components']} component(s), "
                   f"harness_digest {res['digest']}")
    else:
        for p in res["problems"]:
            typer.secho(p, fg="red", err=True)
    if not res["ok"]:
        raise typer.Exit(1)


@harness_app.command("check")
def check(
    directory: Path = typer.Argument(..., help="A harness directory (holding harness.lock)."),
    as_json: bool = typer.Option(False, "--json", help="Print the result as JSON."),
):
    """Recompute every component's digest against the lock and print harness_digest; exit 1 on a mismatch (CI in
    the team harness repo)."""
    from hangar import harness_lock

    _report(harness_lock.check(directory), as_json)


@harness_app.command("freeze")
def freeze(
    ref: str | None = typer.Argument(None, help="A session name or id, or a job id (job-...)."),
    turn: int | None = typer.Option(None, "--turn", help="The turn to freeze (1-based; default the latest captured; "
                                                         "for a job, in the job's own turns)."),
    out: Path | None = typer.Option(None, "--out", help="The harness directory to write (created if missing)."),
    name: str | None = typer.Option(None, "--name", help="The harness's name (default: the lock already in --out, "
                                                         "else the environment's name)."),
    check_dir: Path | None = typer.Option(None, "--check", help="Instead: check this directory against its lock."),
    as_json: bool = typer.Option(False, "--json", help="Print the result as JSON."),
):
    """Freeze a turn into a harness directory: the instructions file, skills/<name>/, native/<runtime>/ and
    harness.lock, whose harness_digest equals the turn's. Fails, naming the gap, when content can't be recovered
    (a skill the sandbox had changed, truncated instructions, a source edited since). Writes files only: open the
    PR to the harness repo yourself."""
    from hangar import cli, harness_lock

    if check_dir is not None:
        return _report(harness_lock.check(check_dir), as_json)
    if not ref or out is None:
        raise typer.BadParameter("give a session or job and --out DIR (or --check DIR)")
    prev = None
    if (out / harness_lock.LOCK_FILE).exists():
        try:
            prev = harness_lock.load(out)
        except ValueError as e:
            typer.secho(f"{e}; fix or remove it first", fg="red", err=True)
            raise typer.Exit(1) from None
    params: dict = {k: v for k, v in (("turn", turn), ("name", name or (prev or {}).get("name"))) if v}
    path = f"/api/jobs/{ref}/harness" if _JOB.match(ref) else f"/api/sessions/{ref}/harness"
    with cli._client() as c:
        r = c.get(path, params=params)
    if r.status_code == 422 and isinstance(r.json(), dict) and r.json().get("gaps"):
        typer.secho("cannot freeze:", fg="red", err=True)
        for g in r.json()["gaps"]:
            typer.secho(f"  - {g}", fg="red", err=True)
        raise typer.Exit(1)
    if r.status_code >= 400:
        try:
            msg = r.json().get("error") or r.json().get("detail")
        except Exception:  # noqa: BLE001
            msg = r.text
        typer.secho(f"error {r.status_code}: {msg}", fg="red", err=True)
        raise typer.Exit(1)
    d = r.json()
    lock, files = harness_lock.unwire(d)
    lock["revision"] = harness_lock.next_revision(prev, lock)
    harness_lock.write(out, lock, files, prev)
    res = harness_lock.check(out)
    for w in d.get("warnings") or []:
        typer.secho(f"warning: {w}", fg="yellow", err=True)
    if as_json:
        typer.echo(json.dumps({**res, "warnings": d.get("warnings") or [], "out": str(out)}, indent=2))
    else:
        typer.echo(f"wrote {out}: {lock['name']} r{lock['revision']}, {len(lock['components'])} component(s), "
                   f"harness_digest {d['harness_digest']}")
    if not res["ok"] or res["digest"] != d["harness_digest"]:
        for p in res["problems"]:
            typer.secho(p, fg="red", err=True)
        raise typer.Exit(1)
