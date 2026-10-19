"""`hangar apply -f DIR`: hangar's admin configuration as reviewed files (--- company-deploy, docs/deploy-company.md).

A company keeps its repo policies, environments and credential references in its deploy repository and applies them
from CD, the way it applies Kubernetes manifests. Each YAML document names its schema:

    schema: hangar.environment/v1          # an environment spec (docs/environments.md), `name` and the rest
    schema: hangar.credential/v1           # a credential whose value is a mounted file, never a value
    schema: hangar.repo-policy/v1          # a repo's policy (docs/repo-policy.md), `repo` and its fields

Applying is declarative and goes through the REST API with the caller's token (an admin's): what a file declares is
what the object becomes, so a policy field the file leaves out is cleared. It is idempotent: an object that already
matches is left alone and reported unchanged. `--check` changes nothing and exits 1 on drift (anything apply would
create or change, and repo policies no file declares), for a scheduled drift job; `--prune` removes those policies.

Credential documents carry `source: file:/var/run/hangar-secrets/<name>` (vault.py): hangar stores the reference
and reads the file when the credential is used. Their values never pass through this command. The API returns no
value or source, so a credential counts as unchanged when its metadata matches.

A run is all or nothing: on the first write the server refuses, the writes already made are undone in reverse order
(the previous body put back, what was created deleted, what was pruned recreated) from the live state the plan read,
and the command exits 1 naming the refused document. A rollback write that fails too is reported per object.

Other tools add schemas to HANDLERS (a change-set profile, for one).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import typer
import yaml

Call = Callable[..., Any]  # cli._call: (method, path, **httpx kwargs) -> JSON, exits on an HTTP error


@dataclass
class Doc:
    schema: str
    name: str
    body: dict[str, Any]
    origin: str  # file:index, for messages


@dataclass
class Change:
    kind: str  # environment | credential | repo
    name: str
    action: str  # create | change | unchanged | live-only | delete
    fields: dict[str, tuple[Any, Any]] = field(default_factory=dict)  # field -> (live, declared)
    origin: str | None = None
    apply: Callable[[], None] | None = None
    undo: Callable[[], None] | None = None  # puts back the live state the plan read, if apply ran

    @property
    def drift(self) -> bool:
        return self.action != "unchanged"


class ApplyError(ValueError):
    pass


# ---------------------------------------------------------------------------- loading
def _files(paths: Iterable[Path]) -> list[Path]:
    out: list[Path] = []
    for p in paths:
        if p.is_dir():
            out += sorted(x for x in p.rglob("*") if x.suffix in (".yaml", ".yml") and x.is_file())
        elif p.is_file():
            out.append(p)
        else:
            raise ApplyError(f"{p}: no such file or directory")
    return out


def load(paths: Iterable[Path]) -> list[Doc]:
    """Every document in the files (directories recursively, *.yaml and *.yml), checked for a known schema and a
    name, each object declared once."""
    docs: list[Doc] = []
    seen: dict[tuple[str, str], str] = {}
    for f in _files(paths):
        try:
            raw = list(yaml.safe_load_all(f.read_text()))
        except yaml.YAMLError as e:
            raise ApplyError(f"{f}: not YAML ({e})") from None
        for i, d in enumerate(raw):
            if d is None:
                continue
            origin = f"{f}#{i + 1}" if len(raw) > 1 else str(f)
            if not isinstance(d, dict) or not isinstance(d.get("schema"), str):
                raise ApplyError(f"{origin}: each document needs `schema:` (one of {', '.join(sorted(HANDLERS))})")
            schema = d["schema"]
            if schema not in HANDLERS:
                raise ApplyError(f"{origin}: unknown schema '{schema}' (known: {', '.join(sorted(HANDLERS))})")
            body = {k: v for k, v in d.items() if k != "schema"}
            name = HANDLERS[schema].name(body, origin)
            key = (HANDLERS[schema].kind, name)
            if key in seen:
                raise ApplyError(f"{origin}: {key[0]} '{name}' is declared twice (also in {seen[key]})")
            seen[key] = origin
            docs.append(Doc(schema, name, body, origin))
    if not docs:
        raise ApplyError("no documents found")
    return docs


# ---------------------------------------------------------------------------- handlers
class Handler:
    kind = ""
    order = 0  # applied in this order: environments, then credentials, then the policies that name them

    def name(self, body: dict[str, Any], origin: str) -> str:
        raise NotImplementedError

    def plan(self, call: Call, docs: list[Doc], prune: bool) -> list[Change]:
        raise NotImplementedError


def _diff(live: dict[str, Any], want: dict[str, Any], keys: Iterable[str]) -> dict[str, tuple[Any, Any]]:
    return {k: (live.get(k), want.get(k)) for k in keys if live.get(k) != want.get(k)}


class Environments(Handler):
    kind, order = "environment", 0

    def name(self, body, origin):
        n = body.get("name")
        if not isinstance(n, str) or not n:
            raise ApplyError(f"{origin}: an environment needs `name:`")
        return n

    def plan(self, call, docs, prune):
        names = {e["name"] for e in call("GET", "/api/environments")}
        out = []
        for d in docs:
            want = {k: v for k, v in d.body.items() if k != "name"}
            live = call("GET", f"/api/environments/{d.name}") if d.name in names else None
            have = {k: v for k, v in (live.get("raw") or {}).items() if k != "name"} if live else None
            if have == want:
                out.append(Change(self.kind, d.name, "unchanged", origin=d.origin))
                continue

            def put(d=d):
                call("PUT", f"/api/environments/{d.name}", content=yaml.safe_dump(d.body, sort_keys=False),
                     headers={"Content-Type": "application/yaml"})

            def undo(d=d, raw=live and live.get("raw")):
                if raw is None:
                    call("DELETE", f"/api/environments/{d.name}")
                else:
                    call("PUT", f"/api/environments/{d.name}", content=yaml.safe_dump(raw, sort_keys=False),
                         headers={"Content-Type": "application/yaml"})

            out.append(Change(self.kind, d.name, "create" if live is None else "change",
                              _diff(have or {}, want, sorted({*want, *(have or {})})), d.origin, put, undo))
        return out


CREDENTIAL_FIELDS = ("harness", "kind", "description", "env_var", "file_path", "exposure", "person")


class Credentials(Handler):
    kind, order = "credential", 1

    def name(self, body, origin):
        n = body.get("name")
        if not isinstance(n, str) or not n:
            raise ApplyError(f"{origin}: a credential needs `name:`")
        src = body.get("source")
        if not isinstance(src, str) or not src.startswith("file:"):
            raise ApplyError(f"{origin}: credential '{n}' needs `source: file:<path>` (a mounted file); "
                             "values never go in these files")
        if "value" in body:
            raise ApplyError(f"{origin}: credential '{n}' has a `value`; declare its `source:` instead")
        for k in ("harness", "kind"):
            if not body.get(k):
                raise ApplyError(f"{origin}: credential '{n}' needs `{k}:`")
        bad = set(body) - {"name", "source", "team", *CREDENTIAL_FIELDS}
        if bad:
            raise ApplyError(f"{origin}: credential '{n}': unknown field(s) {', '.join(sorted(bad))}")
        return n

    def plan(self, call, docs, prune):
        from hangar.vault import reference

        live = {c["name"]: c for c in call("GET", "/api/credentials")}
        out = []
        for d in docs:
            b = d.body
            team = b.get("team", b.get("person") is None)
            # env_var and file_path default to the harness's slot: compared only when the file sets them
            keys = [k for k in CREDENTIAL_FIELDS if k in b or k in ("harness", "kind", "description", "person")]
            want = {**{k: b.get(k) for k in keys}, "person": None if team else b.get("person")}
            have = live.get(d.name)
            fields = _diff(have or {}, want, keys) if have else {}
            if have and not fields:
                out.append(Change(self.kind, d.name, "unchanged", origin=d.origin))
                continue

            def put(d=d, team=team):
                body = {k: d.body[k] for k in CREDENTIAL_FIELDS if d.body.get(k) is not None and k != "person"}
                body.update(name=d.name, value=reference(d.body["source"]), team=team)
                if not team:
                    body["person"] = d.body.get("person")
                call("POST", "/api/credentials", json=body)

            def undo(d=d, have=have):
                if have is None:
                    call("DELETE", f"/api/credentials/{d.name}")
                    return
                # the previous metadata, with the reference: hangar only takes the file named after the credential
                body = {k: have[k] for k in CREDENTIAL_FIELDS if have.get(k) is not None and k != "person"}
                if "exposure" not in d.body:  # left as stored when apply didn't send it
                    body.pop("exposure", None)
                body.update(name=d.name, value=reference(d.body["source"]), team=have.get("person") is None)
                if have.get("person") is not None:
                    body["person"] = have["person"]
                call("POST", "/api/credentials", json=body)

            out.append(Change(self.kind, d.name, "change" if have else "create",
                              fields or {"source": (None, b["source"])}, d.origin, put, undo))
        return out


class RepoPolicies(Handler):
    kind, order = "repo", 2

    def name(self, body, origin):
        from hangar.repopolicy import FIELDS, repo_key

        key = repo_key(body.get("repo"))
        if not key:
            raise ApplyError(f"{origin}: a repo policy needs `repo:`")
        bad = set(body) - {"repo", *FIELDS}
        if bad:
            raise ApplyError(f"{origin}: repo '{key}': unknown field(s) {', '.join(sorted(bad))} "
                             f"(fields: {', '.join(FIELDS)})")
        if not set(body) & set(FIELDS):
            raise ApplyError(f"{origin}: repo '{key}' declares no field")
        return key

    @staticmethod
    def normal(repo: str, body: dict[str, Any], origin: str = "") -> dict[str, Any]:
        """The policy as the API returns it, so a file and the live policy compare field by field."""
        from hangar.repopolicy import FIELDS, RepoPolicy

        try:
            p = RepoPolicy.model_validate({**{k: v for k, v in body.items() if k in FIELDS}, "repo": repo})
        except ValueError as e:
            raise ApplyError(f"{origin}: repo '{repo}': {e}") from None
        return {k: v for k, v in p.model_dump(mode="json").items() if k in FIELDS}

    @staticmethod
    def _restore(call: Call, repo: str, have: dict[str, Any] | None) -> Callable[[], None]:
        """Undo for a write to `repo`'s policy: the live one put back, or none if there was none."""
        from hangar.repopolicy import FIELDS

        def undo():
            if have is None:
                call("DELETE", "/api/repos/policy", params={"repo": repo})
            else:
                call("PUT", "/api/repos/policy", json={"repo": repo, **{k: have.get(k) for k in FIELDS}})

        return undo

    def plan(self, call, docs, prune):
        from hangar.repopolicy import FIELDS

        live = {p["repo"]: p for p in call("GET", "/api/repos")}
        out = []
        for d in docs:
            want = self.normal(d.name, d.body, d.origin)
            have = live.pop(d.name, None)
            fields = _diff({k: have.get(k) for k in FIELDS} if have else {}, want, FIELDS)
            if have and not fields:
                out.append(Change(self.kind, d.name, "unchanged", origin=d.origin))
                continue

            def put(d=d, want=want):
                call("PUT", "/api/repos/policy", json={"repo": d.name, **want})  # null clears: what isn't declared

            out.append(Change(self.kind, d.name, "change" if have else "create",
                              {k: v for k, v in fields.items() if v[1] is not None or v[0] is not None}, d.origin,
                              put, self._restore(call, d.name, have)))
        for repo, have in sorted(live.items()):  # policies no file declares
            def delete(repo=repo):
                call("DELETE", "/api/repos/policy", params={"repo": repo})

            out.append(Change(self.kind, repo, "delete" if prune else "live-only", apply=delete if prune else None,
                              undo=self._restore(call, repo, have) if prune else None))
        return out


HANDLERS: dict[str, Handler] = {
    "hangar.environment/v1": Environments(),
    "hangar.credential/v1": Credentials(),
    "hangar.repo-policy/v1": RepoPolicies(),
}


def plan(call: Call, docs: list[Doc], prune: bool = False) -> list[Change]:
    """What applying the documents would do, read from the live API (nothing changed)."""
    out: list[Change] = []
    by_schema: dict[str, list[Doc]] = {}
    for d in docs:
        by_schema.setdefault(d.schema, []).append(d)
    for schema, h in sorted(HANDLERS.items(), key=lambda x: x[1].order):
        if schema in by_schema or (h.kind == "repo" and prune):
            out += h.plan(call, by_schema.get(schema, []), prune)
    return out


# ---------------------------------------------------------------------------- the command
_VERB = {"create": "created", "change": "changed", "delete": "deleted"}
_DRY = {"create": "would create", "change": "would change", "delete": "would delete"}


def _show(v: Any) -> str:
    return "-" if v is None else json.dumps(v, sort_keys=True) if isinstance(v, (dict, list)) else repr(v)


def _label(c: Change) -> str:
    return f"{c.kind} {c.name} ({c.origin or 'pruned: no file declares it'})"


def _apply_all(changes: list[Change], as_json: bool) -> None:
    """Every write, or none: on the first one that fails, the ones made are undone in reverse order and the command
    exits 1. A failing undo is reported with the object it left changed."""
    from hangar import cli

    done: list[Change] = []
    for c in changes:
        if c.apply is None or not c.drift:
            continue
        try:
            c.apply()
        except Exception as e:  # cli._call has said why on stderr and raised typer.Exit
            failed, why = c, _why(e)
            break
        done.append(c)
    else:
        return
    left: list[tuple[Change, str]] = []
    for c in reversed(done):
        try:
            if c.undo is None:
                raise RuntimeError("no way to undo it")
            c.undo()
        except Exception as e:
            left.append((c, _why(e)))
    if as_json:
        cli._print({"applied": False, "failed": {"kind": failed.kind, "name": failed.name, "origin": failed.origin,
                                                 "error": why},
                    "rolled_back": [{"kind": c.kind, "name": c.name, "origin": c.origin}
                                    for c in done if all(c is not x for x, _ in left)],
                    "left_changed": [{"kind": c.kind, "name": c.name, "origin": c.origin, "error": e}
                                     for c, e in left]})
    typer.secho(f"{_label(failed)}: could not {failed.action} it{f' ({why})' if why else ''}", fg="red", err=True)
    if not left:
        typer.secho(f"rolled back {len(done)} change(s): hangar is as it was before this run" if done else
                    "nothing was applied: hangar is as it was before this run", fg="red", err=True)
    else:
        typer.secho(f"rollback failed for {len(left)} of {len(done)} change(s); these objects are left changed:",
                    fg="red", err=True)
        for c, e in left:
            typer.secho(f"    {_label(c)}: {_VERB[c.action]}, not undone{f' ({e})' if e else ''}", fg="red", err=True)
        if len(left) < len(done):
            typer.secho(f"the other {len(done) - len(left)} change(s) were rolled back", fg="red", err=True)
    raise typer.Exit(1)


def _why(e: BaseException) -> str:
    return "" if isinstance(e, typer.Exit) else str(e) or type(e).__name__


def apply(
    file: list[Path] = typer.Option(..., "--file", "-f", help="A YAML file or a directory of them (recursively). "
                                    "Repeatable."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would change; change nothing."),
    check: bool = typer.Option(False, "--check", help="Report drift and exit 1 if there is any; change nothing."),
    prune: bool = typer.Option(False, "--prune", help="Remove repo policies no file declares."),
    as_json: bool = typer.Option(False, "--json"),
):
    """Apply environments, credential references and repo policies from files; what's live and matches is left alone.

    Example:  hangar apply -f policy/             # in CD, with an admin token (HANGAR_URL, HANGAR_TOKEN)
              hangar apply -f policy/ --check     # drift: exit 1 when hangar differs from the files"""
    from hangar import cli

    try:
        docs = load(file)
        changes = plan(cli._call, docs, prune=prune)
    except ApplyError as e:
        raise typer.BadParameter(str(e), param_hint="--file") from None
    write = not (dry_run or check)
    if write:
        _apply_all(changes, as_json)
    if as_json:
        cli._print({"applied": write, "items": [
            {"kind": c.kind, "name": c.name, "action": c.action, "origin": c.origin,
             "fields": {k: {"live": a, "declared": b} for k, (a, b) in c.fields.items()}} for c in changes]})
    else:
        for c in changes:
            if c.action == "unchanged":
                typer.echo(f"{c.kind} {c.name}: unchanged")
                continue
            if c.action == "live-only":
                typer.secho(f"{c.kind} {c.name}: live, but no file declares it (--prune removes it)", fg="yellow")
                continue
            verb = (_VERB if write else _DRY)[c.action]
            typer.secho(f"{c.kind} {c.name}: {verb}", fg="green" if write else "yellow")
            for k, (a, b) in sorted(c.fields.items()):
                typer.echo(f"    {k}: {_show(a)} -> {_show(b)}")
        n = sum(c.drift for c in changes)
        typer.echo(f"{len(changes)} object(s), {n} {'changed' if write else 'to change'}" if n else
                   f"{len(changes)} object(s), all up to date")
    if check and any(c.drift for c in changes):
        raise typer.Exit(1)


def register(app: typer.Typer) -> None:
    app.command("apply")(apply)
