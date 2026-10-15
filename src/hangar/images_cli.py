"""`hangar images plan | build` (images.py, docs/gke.md). Local: they read the checkout's environments and harness lock,
need no running hangar, and work while the cluster is powered down. Registered by cli.py."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import typer

images_app = typer.Typer(no_args_is_help=True, help="Environment images and harness packs, built before anyone waits.")

_PROVIDER = typer.Option("kubernetes", "--provider", "-p", help="kubernetes or docker.")
_REGISTRY = typer.Option(None, "--registry", help="Registry the cluster pulls from (default: HANGAR_REGISTRY).")
_ENV_DIR = typer.Option(None, "--env-dir", help="Environment specs (default: HANGAR_ENV_DIRS, else ./envs). "
                        "Repeatable.")
_PACKS = typer.Option(None, "--packs/--no-packs", help="Override whether the provider mounts harness packs.")
_ONLY = typer.Option(None, "--only", help="Only these environments or harnesses. Repeatable.")


def _plan(provider: str, registry: str | None, env_dir: list[Path] | None, packs: bool | None,
          only: list[str] | None):
    from hangar import images
    from hangar.config import Settings
    from hangar.envspec import EnvCatalog

    over = {"registry": registry} if registry else {}
    if env_dir:
        over["env_dirs"] = env_dir
    settings = Settings(**over)
    try:
        items = images.plan(settings, EnvCatalog(settings.env_dirs), provider, packs)
    except ValueError as e:
        typer.secho(str(e), fg="red", err=True)
        raise typer.Exit(2) from e
    if only:
        items = [i for i in items if i.name in only]
    return settings, items


def _report(items, as_json: bool, provider: str) -> None:
    from hangar import images

    if as_json:
        missing = sum(i.status in ("missing", "failed") for i in items)
        typer.echo(json.dumps({"provider": provider, "missing": missing,
                               "items": [i.to_dict() for i in items]}, indent=2))
    else:
        typer.echo(images.table(items))


@images_app.command("plan")
def plan(provider: str = _PROVIDER, registry: str | None = _REGISTRY, env_dir: list[Path] | None = _ENV_DIR,
         packs: bool | None = _PACKS, only: list[str] | None = _ONLY,
         as_json: bool = typer.Option(False, "--json", help="Print JSON instead of a table."),
         check: bool = typer.Option(True, "--check/--no-check", help="Look each tag up (registry API, or the local "
                                    "Docker for --provider docker).")):
    """Every environment image and harness pack tag this code needs on PROVIDER, and which are missing.
    Exit code 0 when nothing is missing, 3 when something is, 1 when the registry couldn't say."""
    from hangar import images

    _, items = _plan(provider, registry, env_dir, packs, only)
    if check:
        asyncio.run(images.check(items, provider))
    _report(items, as_json, provider)
    states = {i.status for i in items}
    if states & {"unknown", "error"}:
        raise typer.Exit(1)
    if "missing" in states:
        raise typer.Exit(3)


@images_app.command("build")
def build(provider: str = _PROVIDER, registry: str | None = _REGISTRY, env_dir: list[Path] | None = _ENV_DIR,
          packs: bool | None = _PACKS, only: list[str] | None = _ONLY,
          builder: str = typer.Option("auto", "--builder", help="docker (local daemon), cloudbuild (gcloud), or "
                                      "auto: docker when it runs here, else Cloud Build."),
          jobs: int = typer.Option(2, "--jobs", "-j", help="Builds at a time."),
          as_json: bool = typer.Option(False, "--json", help="Print the result as JSON (build logs go to stderr)."),
          dry_run: bool = typer.Option(False, "--dry-run", help="Show what would be built, build nothing.")):
    """Build and push the missing images and packs from `hangar images plan`. Run by CI on every push to main and by
    deploy/gke/up.sh before rollout, so no create waits on a build. Exit code 1 when a build failed or a tag's state
    is unknown."""
    from hangar import images

    settings, items = _plan(provider, registry, env_dir, packs, only)
    asyncio.run(images.check(items, provider))
    if not dry_run:
        try:
            asyncio.run(images.build_missing(items, settings, provider, builder, jobs,
                                             log=lambda line: print(line, file=sys.stderr, flush=True)))
        except ValueError as e:
            typer.secho(str(e), fg="red", err=True)
            raise typer.Exit(2) from e
    _report(items, as_json, provider)
    if {i.status for i in items} & {"failed", "unknown", "error"}:
        raise typer.Exit(1)
