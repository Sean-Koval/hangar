"""`hangar up --gke`: the GKE install as one command that shows what it will do before doing it.

It wraps what the docs already do by hand (docs/gke.md): `terraform plan` for deploy/terraform/gke, then, once you
approve the plan, `terraform apply` of exactly that plan, `deploy/gke/up.sh`, and `hangar login`. A plan that destroys
or replaces anything is never applied on `--yes` alone; `--dry-run` stops after the plan, having changed nothing.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import typer

TF_DIR = Path("deploy/terraform/gke")
UP_SH = Path("deploy/gke/up.sh")
TOKEN_FILE = Path("~/.hangar/gke-admin-token")


def _fail(msg: str, code: int = 1) -> typer.Exit:
    typer.secho(msg, fg="red", err=True)
    return typer.Exit(code)


def checkout() -> Path | None:
    """The hangar checkout holding the terraform and up.sh: the current directory, else the one hangar runs from."""
    import hangar

    for d in (Path.cwd(), Path(hangar.__file__).resolve().parents[2]):
        if (d / TF_DIR / "main.tf").is_file() and (d / UP_SH).is_file():
            return d
    return None


def _tf(root: Path, *args: str, capture: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(["terraform", f"-chdir={root / TF_DIR}", *args], text=True,
                          capture_output=capture, stdin=subprocess.DEVNULL)


def plan_changes(root: Path, plan: Path) -> dict[str, list[str]]:
    """Resource addresses by what the plan does to them: create, update, destroy (deletes and replacements)."""
    r = _tf(root, "show", "-json", str(plan), capture=True)
    if r.returncode != 0:
        raise _fail(f"terraform show failed: {r.stderr.strip()[:500]}")
    out: dict[str, list[str]] = {"create": [], "update": [], "destroy": []}
    for rc in json.loads(r.stdout).get("resource_changes") or []:
        actions = rc.get("change", {}).get("actions") or []
        if "delete" in actions:
            out["destroy"].append(rc["address"] + (" (replaced)" if "create" in actions else ""))
        elif "create" in actions:
            out["create"].append(rc["address"])
        elif "update" in actions:
            out["update"].append(rc["address"])
    return out


def _confirm(question: str, yes: bool) -> bool:
    if yes:
        return True
    if not sys.stdin.isatty():
        return False
    return typer.confirm(question, default=False)


def _url(root: Path, domain: str | None) -> str | None:
    if domain:
        return f"https://{domain}"
    r = _tf(root, "output", "-raw", "static_ip", capture=True)
    ip = r.stdout.strip() if r.returncode == 0 else ""
    return f"https://{ip}.sslip.io" if ip and " " not in ip else None


def up_gke(dry_run: bool, yes: bool, allow_destroy: bool, domain: str | None, data_dir: Path) -> None:
    root = checkout()
    if root is None:
        raise _fail(f"run this from a hangar checkout: it needs {TF_DIR} and {UP_SH}", 2)
    missing = [t for t in ("terraform", "gcloud", "kubectl") if not shutil.which(t)]
    if missing:
        raise _fail(f"missing {', '.join(missing)}: install them (docs/gke.md) and log in with "
                    "`gcloud auth login && gcloud auth application-default login`", 2)
    if not (root / TF_DIR / "terraform.tfvars").is_file():
        raise _fail(f"no {TF_DIR}/terraform.tfvars: cp {TF_DIR}/terraform.tfvars.example {TF_DIR}/terraform.tfvars "
                    "and set project_id, region and zone", 2)
    steps = ["terraform apply (the plan below, exactly)", f"bash {UP_SH}: build the control plane with Cloud Build, "
             "deploy it, and keep its admin token in ~/.hangar/gke-admin-token", "hangar login to it"]
    typer.secho("hangar up --gke will:\n" + "\n".join(f"  {i}. {s}" for i, s in enumerate(steps, 1)), fg="cyan")
    typer.secho("\n== terraform init", fg="cyan")
    if _tf(root, "init", "-input=false").returncode != 0:
        raise _fail("terraform init failed (see above)")
    data_dir.mkdir(parents=True, exist_ok=True)
    plan = data_dir / "gke.tfplan"
    typer.secho("\n== terraform plan (read-only)", fg="cyan")
    try:
        if _tf(root, "plan", "-input=false", f"-out={plan}").returncode != 0:
            raise _fail("terraform plan failed (see above); nothing was changed")
        changes = plan_changes(root, plan)
        typer.echo(f"\nPlan: {len(changes['create'])} to create, {len(changes['update'])} to change, "
                   f"{len(changes['destroy'])} to destroy or replace.")
        for a in changes["destroy"]:
            typer.secho(f"  destroys {a}", fg="red")
        if dry_run:
            typer.secho("\nDry run: nothing was changed. Without --dry-run, hangar up --gke would go on with:\n"
                        + "\n".join(f"  {s}" for s in steps), fg="yellow")
            return
        if any(changes.values()):
            if changes["destroy"] and not allow_destroy:
                raise _fail("the plan destroys or replaces the resources above, so hangar won't apply it for you. "
                            f"Review it, then: terraform -chdir={TF_DIR} apply (or pass --allow-destroy)")
            if not _confirm("Apply this plan?", yes):
                raise _fail("not applied (pass --yes to apply without asking)")
            typer.secho("\n== terraform apply", fg="cyan")
            if _tf(root, "apply", "-input=false", str(plan)).returncode != 0:
                raise _fail("terraform apply failed (see above); running hangar up --gke again picks up from here")
        else:
            typer.secho("✓ infrastructure is up to date", fg="green")
    finally:
        plan.unlink(missing_ok=True)
    if not _confirm(f"Deploy the control plane ({UP_SH})?", yes):
        raise _fail(f"not deployed: run bash {UP_SH} when you're ready")
    typer.secho(f"\n== {UP_SH}", fg="cyan")
    env = {**os.environ, **({"DOMAIN": domain} if domain else {})}
    if subprocess.run(["bash", str(root / UP_SH)], env=env, stdin=subprocess.DEVNULL).returncode != 0:
        raise _fail(f"{UP_SH} failed (see above); it is safe to run again")
    _login(root, domain)


def _login(root: Path, domain: str | None) -> None:
    from hangar.setup_cli import _save_login, _token_ok

    url, tok = _url(root, domain), TOKEN_FILE.expanduser()
    token = tok.read_text().strip() if tok.is_file() else ""
    if url and token and _token_ok(url, token):
        os.environ.pop("HANGAR_URL", None)
        os.environ.pop("HANGAR_TOKEN", None)
        typer.secho(f"✓ logged in to {url} (saved to {_save_login(url, token)})", fg="green")
        typer.echo("Next: hangar doctor --fix, hangar setup github, hangar connect claude-code")
        return
    typer.secho(f"Not logged in yet: the certificate takes 15-60 minutes the first time. Once it's Active:\n"
                f"  hangar login {url or 'https://<static-ip>.sslip.io'} --token-file {TOKEN_FILE}", fg="yellow")
