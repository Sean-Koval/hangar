"""A turn's login, checked before it starts (#293).

A turn whose harness gets no login runs anyway, and the CLI ends it in seconds with its own message (Claude Code's
"Not logged in · Please run /login", exit 1), recorded as the agent's failure. On the first live GKE qualification
that was a session made with the deploy's admin token: no person, so no personal login, and no team credential for
claude-code. hangar now refuses such a turn up front, says why and what to add, and records it as
`infra_error` / `no_login` (configuration, not the agent).

The check is cheap where it can be: a credential for the harness among the sandbox's (or one that sets one of the
harness's login variables) is enough. Otherwise one shell probe asks the sandbox whether any of the harness's login
variables is set in the turn's environment (`~/.hangar/env`, where hangar writes credentials, secrets and the model
gateway's bearer) or any of its login files exists (an interactive `/login`, a captured auth file). A probe that
can't run lets the turn start: the check is there to explain a failure, never to cause one.
"""

from __future__ import annotations

import shlex
from typing import TYPE_CHECKING

from hangar.harnesses import HARNESSES
from hangar.models import Credential

if TYPE_CHECKING:
    from hangar.models import Sandbox

INFRA_REASON = "no_login"
PROBE_TIMEOUT = 20
# Ways a harness logs in that aren't one of its credential slots: cloud providers, a base URL with its own token,
# an interactive login's file.
OTHER_VARS: dict[str, tuple[str, ...]] = {
    "claude-code": ("ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_BEDROCK",
                    "ANTHROPIC_BASE_URL"),
    "codex": ("OPENAI_API_KEY", "OPENAI_BASE_URL"),
}
OTHER_FILES: dict[str, tuple[str, ...]] = {
    "claude-code": (".claude/.credentials.json",),
    "codex": (".codex/auth.json",),
}


class NoLogin(Exception):
    """The turn's harness would start with no login."""


def login_vars(harness: str) -> list[str]:
    h = HARNESSES.get(harness)
    slots = [c.env_var for c in (h.credentials if h else []) if c.env_var]
    return list(dict.fromkeys([*slots, *OTHER_VARS.get(harness, ())]))


def login_files(harness: str) -> list[str]:
    h = HARNESSES.get(harness)
    files = [c.file_path for c in (h.credentials if h else []) if c.file_path]
    files += [f.capture_file for f in (h.logins if h else []) if f.capture_file]
    return list(dict.fromkeys([*files, *OTHER_FILES.get(harness, ())]))


def needs_login(harness: str | None) -> bool:
    """An agent harness with ways to log in (a helper, or a harness with no credential slots, needs none)."""
    h = HARNESSES.get(harness or "")
    return bool(h and h.agent and h.credentials)


def has_credential(store: object, sb: Sandbox, harness: str) -> bool:
    """A credential among the sandbox's that logs the harness in: one for it, or one that sets a login variable."""
    wanted = set(login_vars(harness))
    for name in sb.credentials:
        c = store.get(Credential, name)  # type: ignore[attr-defined]
        if c is not None and (c.harness == harness or (c.env_var and c.env_var in wanted)):
            return True
    return False


def probe_command(harness: str, home: str) -> str:
    """Exit 0 when the turn's environment has a login for `harness`, 3 when it has none."""
    q = shlex.quote
    tests = [f'[ -n "${{{v}:-}}" ]' for v in login_vars(harness)]
    tests += [f"[ -s {q(f if f.startswith('/') else f'{home}/{f}')} ]" for f in login_files(harness)]
    return (f"set -a; [ -f {q(home + '/.hangar/env')} ] && . {q(home + '/.hangar/env')}; set +a; "
            f"if {' || '.join(tests) or 'true'}; then exit 0; else exit 3; fi")


def message(harness: str, person: str | None) -> str:
    who = f"{person}'s sandbox" if person else "this session (it belongs to no person, so no personal login reaches it)"
    return (f"no login for {harness} reaches {who}: the turn wasn't started. Add a team credential (hangar cred add "
            f"NAME --team --harness {harness} --kind api_key --file key.txt)"
            + ("" if person else ", or start the session as a person") + "; docs/team.md")


async def check(fleet: object, sb: Sandbox, harness: str | None, person: str | None) -> None:
    """Raise NoLogin when a turn of `harness` in `sb` would start with no login."""
    if not harness or not needs_login(harness):
        return
    if has_credential(fleet.store, sb, harness):  # type: ignore[attr-defined]
        return
    home = f"/home/{fleet.settings.sandbox_user}"  # type: ignore[attr-defined]
    try:
        res = await fleet.exec(sb.id, probe_command(harness, home), timeout=PROBE_TIMEOUT,  # type: ignore[attr-defined]
                               quiet=True)  # housekeeping: not activity, not audited
    except Exception:  # noqa: BLE001 - a probe that can't run says nothing: let the turn start
        return
    if getattr(res, "exit_code", 0) == 3:
        raise NoLogin(message(harness, person))


def preview(h: object, environment: str | None, harness: str | None = None, person: str | None = None,
            repo: str | None = None, purpose: str = "interactive") -> dict[str, object]:
    """Whether a session on `environment` for `person` (None: no person, as with a deploy's admin token) would get a
    login for its harness: the credentials it would get, as fleet.create resolves them. `login` is True or False,
    or None when the environment could log in some other way hangar can't see from here (its secrets set a login
    variable, or the harness needs no login)."""
    from hangar.teamcreds import CredentialError, mode_for, resolve

    fleet = h.fleet  # type: ignore[attr-defined]
    environment = environment or h.sessions._env_for_repo(repo)  # type: ignore[attr-defined] - the repo's first
    if not environment:
        raise ValueError("environment is required (or a repo listed in HANGAR_GITHUB_REPOS)")
    spec = fleet.catalog.get(environment)
    hname = spec.pick_harness(harness)
    out: dict[str, object] = {"environment": environment, "harness": hname, "person": person, "credentials": [],
                              "login": None, "fix": None}
    if not hname or not needs_login(hname):
        return out
    names = [c for c in dict.fromkeys(spec.credentials)
             if fleet._cred_harness(c) in (hname, None) or not fleet._is_agent(fleet._cred_harness(c))]
    try:
        got = resolve(fleet.store, names, person, hname, False, fleet._is_agent, mode_for(fleet.store, repo),
                      fleet.settings, purpose=purpose).names  # type: ignore[arg-type]
    except CredentialError as e:
        got, out["error"] = [], str(e)
    out["credentials"] = got
    wanted = set(login_vars(hname))
    creds = [fleet.store.get(Credential, n) for n in got]
    if any(c is not None and (c.harness == hname or (c.env_var and c.env_var in wanted)) for c in creds):
        out["login"] = True
    elif wanted & set(spec.secrets) or wanted & set(spec.env):
        out["login"] = None  # the environment sets a login variable itself
    else:
        out["login"] = False
        out["fix"] = message(hname, person)
    return out


def mount(app: object, h: object, admin: object) -> None:
    """GET /api/credentials/login: would a session get a login for its harness (#293)."""
    from fastapi import HTTPException

    @app.get("/api/credentials/login")  # type: ignore[attr-defined]
    async def credentials_login(environment: str | None = None, harness: str | None = None, person: str | None = None,
                                repo: str | None = None, purpose: str = "interactive", _: str = admin):
        if purpose not in ("interactive", "automated"):
            raise HTTPException(422, "purpose is interactive or automated")
        try:
            return preview(h, environment, harness, person, repo, purpose)
        except KeyError as e:
            raise HTTPException(404, f"no environment {environment!r}") from e
        except ValueError as e:
            raise HTTPException(422, str(e)) from e
