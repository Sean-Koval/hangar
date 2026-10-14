"""Credential health (--- clouddoctor): whether each agent login still works, checked without calling a model.

An expired login fails silently in the worst place: a job at 02:00, a pool whose every login is dead, a teammate's
session that just stops. So each credential an agent uses gets a check that costs nothing:

* **Claude Code, `claude setup-token` tokens** (`oauth_token`): by age. The command makes a one-year token
  (code.claude.com/docs/en/authentication, "Generate a long-lived token"), so it expires a year after it was stored.
  API keys (`api_key`) don't expire; their format is checked.
* **Codex, ChatGPT logins** (`auth_file`): `codex login status`, which "exits with 0 when credentials are present"
  (learn.chatgpt.com/docs/developer-commands, codex login). It runs in the login's own sandbox when one is captured;
  where hangar runs no codex binary, the same question is answered from the file: an API key, or ChatGPT tokens with
  a refresh token. Codex refreshes those itself while it is used, so there is no fixed expiry to record.
* **Google service-account keys** (Vertex AI for pi and Antigravity, `generic` key files): gcloud, in a config
  directory of its own: `gcloud auth activate-service-account --key-file`, then `gcloud auth print-access-token`
  (Google refuses a deleted or disabled key; the token is thrown away), then, where the key may see it,
  `gcloud iam service-accounts keys list` for the key's `validBeforeTime`.

Each check records `health`, `last_ok` and `expires_at` on the credential. Checks run when a login is captured,
every night (the ops loop), and from `hangar status`. Anything that stops working, or expires within WARN_DAYS, is
in `hangar doctor`, `hangar status`, and the inbox of whoever must renew it: the person whose own it is, and admins
for the team's. Pools (several logins one sandbox holds at a time) are summed up: a pool with no working login left
is a failure even when each one alone is only a warning.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple

from hangar.models import Credential, CredentialKind, now

if TYPE_CHECKING:
    from hangar.auth import Principal
    from hangar.fleet import Fleet
    from hangar.store import Store

log = logging.getLogger("hangar.credhealth")

WARN_DAYS = 14
CLAUDE_TOKEN_DAYS = 365  # `claude setup-token`: "a one-year OAuth token"
TIMEOUT = 30.0  # per command
STATUS_MAX_AGE = 3600.0  # `hangar status` rechecks results older than this
STATUS_BUDGET = 15.0  # and spends at most this long doing it
CONCURRENCY = 4
NO_EXPIRY = "9999-"  # validBeforeTime of a key that never expires
# What a gcloud subprocess may see of hangar's own environment: the proxy and CA settings, nothing else.
_PASS_ENV = ("PATH", "LANG", "LC_ALL", "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "https_proxy", "http_proxy",
             "no_proxy", "REQUESTS_CA_BUNDLE", "SSL_CERT_FILE", "CLOUDSDK_CORE_CUSTOM_CA_CERTS_FILE")


class Result(NamedTuple):
    status: str  # ok | warn | fail | unknown
    detail: str
    expires_at: float | None = None
    method: str = ""


def _day(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d")


def _by_expiry(ok: str, expires_at: float | None, renew: str, method: str) -> Result:
    if expires_at is None:
        return Result("ok", ok, None, method)
    left = expires_at - now()
    if left <= 0:
        return Result("fail", f"expired on {_day(expires_at)}. {renew}", expires_at, method)
    if left <= WARN_DAYS * 86400:
        return Result("warn", f"expires on {_day(expires_at)}, in {max(1, int(-(-left // 86400)))} day(s). {renew}",
                      expires_at, method)
    return Result("ok", f"{ok}; good until {_day(expires_at)}", expires_at, method)


def has_check(c: Credential) -> bool:
    if c.harness == "claude-code" and c.kind in (CredentialKind.OAUTH_TOKEN, CredentialKind.API_KEY):
        return True
    if c.harness == "codex" and c.kind in (CredentialKind.AUTH_FILE, CredentialKind.API_KEY):
        return True
    return c.kind == CredentialKind.GENERIC and (c.file_path or "").endswith("gcp-sa.json")


# ====================================================================== the checks
def claude_token(c: Credential, value: str) -> Result:
    renew = (f"Run `claude setup-token` and store the new token: hangar cred add {c.name} --harness claude-code "
             "--kind oauth_token")
    stored = c.updated_at or c.created_at
    res = _by_expiry(f"a `claude setup-token` token stored {_day(stored)}", stored + CLAUDE_TOKEN_DAYS * 86400,
                     renew, "token age (setup-token tokens last a year)")
    if res.status == "ok" and not value.startswith("sk-ant-oat"):  # what setup-token prints (and the login captures)
        return res._replace(status="warn", detail="doesn't look like a `claude setup-token` token (those start "
                                                  "with sk-ant-oat); " + res.detail)
    return res


def api_key(c: Credential, value: str, prefix: str, vendor: str) -> Result:
    if not value.startswith(prefix):  # a gateway's own key can look different: a warning, not a failure
        return Result("warn", f"doesn't look like an {vendor} API key (those start with {prefix}); fine if it is "
                              "an LLM gateway's key", None, "key format")
    return Result("ok", f"an {vendor} API key: no expiry (a revoked key shows up as failed turns)", None,
                  "key format")


def codex_file(value: bytes) -> Result:
    """What `codex login status` answers, from the file itself: are credentials present."""
    try:
        d = json.loads(value)
    except ValueError:
        return Result("fail", "auth.json isn't JSON: log in again", None, "auth.json")
    if not isinstance(d, dict):
        return Result("fail", "auth.json isn't a JSON object: log in again", None, "auth.json")
    if d.get("OPENAI_API_KEY"):
        return Result("ok", "logged in with an API key", None, "auth.json (as codex login status reads it)")
    tokens = d.get("tokens") if isinstance(d.get("tokens"), dict) else {}
    if not tokens.get("refresh_token"):
        return Result("fail", "no ChatGPT tokens in auth.json: log in again (the dashboard's Credentials page, or "
                              "POST /api/logins with harness codex)", None, "auth.json")
    bit = ""
    if isinstance(d.get("last_refresh"), str):
        with contextlib.suppress(ValueError):
            t = datetime.fromisoformat(d["last_refresh"].replace("Z", "+00:00")).timestamp()
            bit = f"; tokens last refreshed {_day(t)} (codex refreshes them while it is used)"
    return Result("ok", "logged in with ChatGPT" + bit, None, "auth.json (as codex login status reads it)")


def _sa(value: bytes) -> dict[str, Any] | None:
    try:
        d = json.loads(value)
    except ValueError:
        return None
    return d if isinstance(d, dict) and d.get("type") == "service_account" and d.get("client_email") else None


async def _run(argv: list[str], env: dict[str, str]) -> tuple[int, str, str]:
    p = await asyncio.create_subprocess_exec(*argv, env=env, stdin=asyncio.subprocess.DEVNULL,
                                             stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(p.communicate(), TIMEOUT)
    except BaseException:
        with contextlib.suppress(ProcessLookupError):
            p.kill()
        await asyncio.shield(p.wait())
        raise
    return p.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace")


def _last_line(text: str) -> str:
    lines = [ln.strip() for ln in text.strip().splitlines() if ln.strip()]
    return lines[-1][:300] if lines else "no output"


async def gcloud_key(c: Credential, value: bytes, gcloud: str | None = None) -> Result:
    info = _sa(value)
    if info is None:
        return Result("fail", "not a Google service-account key file (type service_account, with client_email)",
                      None, "key format")
    gcloud = gcloud or shutil.which("gcloud")
    if not gcloud:
        return Result("unknown", "gcloud isn't installed where hangar runs, so this key can't be checked (the "
                                 "control-plane image has it unless built with INSTALL_GCLOUD=0)", None, "gcloud")
    email = str(info["client_email"])
    with tempfile.TemporaryDirectory(prefix="hangar-gcloud-") as d:
        key = Path(d) / "key.json"
        fd = os.open(key, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(value)
        # a config directory of its own: hangar's gcloud login and settings are never touched or used
        env = {k: os.environ[k] for k in _PASS_ENV if k in os.environ} | {
            "HOME": d, "CLOUDSDK_CONFIG": f"{d}/gcloud", "CLOUDSDK_CORE_DISABLE_PROMPTS": "1"}
        rc, _, err = await _run([gcloud, "auth", "activate-service-account", email, f"--key-file={key}", "--quiet"],
                                env)
        if rc:
            return Result("fail", f"gcloud couldn't activate the key: {_last_line(err)}", None, "gcloud")
        rc, _, err = await _run([gcloud, "auth", "print-access-token", email, "--quiet"], env)  # the token is dropped
        if rc:
            return Result("fail", f"Google refused the key (deleted or disabled, or the service account is): "
                                  f"{_last_line(err)}", None, "gcloud auth print-access-token")
        expires = None
        rc, out, _ = await _run([gcloud, "iam", "service-accounts", "keys", "list", f"--iam-account={email}",
                                 "--managed-by=user", "--format=json", "--quiet"], env)
        if rc == 0:  # best effort: a key may not be allowed to list its own account's keys
            with contextlib.suppress(ValueError, TypeError, AttributeError):
                kid = str(info.get("private_key_id") or "")
                for k in json.loads(out):
                    before = str(k.get("validBeforeTime") or "")
                    if kid and str(k.get("name", "")).endswith(f"/keys/{kid}") and before and \
                            not before.startswith(NO_EXPIRY):
                        expires = datetime.fromisoformat(before.replace("Z", "+00:00")).timestamp()
    renew = (f"Make a new key for {email} and store it: hangar cred add {c.name} --harness {c.harness} --kind "
             "generic --file key.json")
    return _by_expiry(f"Google issues tokens for {email}", expires, renew, "gcloud auth print-access-token")


async def check(c: Credential, value: bytes | None, gcloud: str | None = None) -> Result | None:
    """The check for `c` (None: there is none for its kind). Never raises."""
    if not has_check(c):
        return None
    if not value:
        return Result("fail", "no stored value in the vault: store it again", None, "vault")
    try:
        text = value.decode(errors="replace").strip()
        if c.harness == "claude-code":
            if c.kind == CredentialKind.OAUTH_TOKEN:
                return claude_token(c, text)
            return api_key(c, text, "sk-ant-", "Anthropic")
        if c.harness == "codex":
            if c.kind == CredentialKind.AUTH_FILE:
                return codex_file(value)
            return api_key(c, text, "sk-", "OpenAI")
        return await gcloud_key(c, value, gcloud)
    except TimeoutError:
        return Result("unknown", f"the check took over {TIMEOUT:.0f} s", None, "timeout")
    except Exception as e:  # noqa: BLE001 - a check that breaks says so; it never takes anything else down
        log.warning("credential check for %s failed", c.name, exc_info=True)
        return Result("unknown", f"the check failed: {type(e).__name__}", None, "error")


def record(store: Store, name: str, res: Result, by: str) -> Credential | None:
    """Write a result onto the credential as it is now (re-read: a lease may have changed meanwhile)."""
    c = store.get(Credential, name)
    if c is None:
        return None
    t = now()
    c.health = {"status": res.status, "detail": res.detail, "checked_at": t, "method": res.method, "by": by}
    if res.status != "unknown":
        c.expires_at = res.expires_at
    if res.status in ("ok", "warn"):
        c.last_ok = t
    store.put(c)
    return c


async def check_all(fleet: Fleet, by: str, max_age: float | None = None, names: list[str] | None = None,
                    budget: float | None = None) -> list[dict[str, Any]]:
    """Check every credential that has a check (or `names`), skipping results younger than `max_age`. With
    `budget`, stops waiting after that many seconds; the checks still running finish on their own and record."""
    creds = [c for c in fleet.store.list(Credential) if has_check(c) and (names is None or c.name in names)]
    if max_age is not None:
        creds = [c for c in creds if not c.health or now() - float(c.health.get("checked_at") or 0) > max_age]
    sem = asyncio.Semaphore(CONCURRENCY)
    gcloud = shutil.which("gcloud")

    async def one(c: Credential) -> dict[str, Any] | None:
        async with sem:
            value = await asyncio.to_thread(fleet.broker.vault.get, f"cred:{c.name}")
            res = await check(c, value, gcloud)
        if res is None:
            return None
        record(fleet.store, c.name, res, by)
        return {"name": c.name, **res._asdict()}

    tasks = [asyncio.create_task(one(c)) for c in creds]
    if not tasks:
        return []
    done, _ = await asyncio.wait(tasks, timeout=budget)
    return [r for t in done if not t.exception() and (r := t.result()) is not None]


async def on_capture(fleet: Fleet, cred: Credential, sandbox_id: str) -> None:
    """A login was just captured in `sandbox_id`: check it there, with the harness's own command where it has one.
    Never raises; never holds the capture up for more than TIMEOUT."""
    try:
        res: Result | None = None
        if cred.harness == "codex" and cred.kind == CredentialKind.AUTH_FILE:
            cmd = 'set -a; . "$HOME/.hangar/env" 2>/dev/null; set +a; codex login status'
            out = await asyncio.wait_for(fleet.exec(sandbox_id, cmd, timeout=int(TIMEOUT), quiet=True), TIMEOUT + 5)
            said = _last_line(fleet.broker.redact((out.stdout or "") + "\n" + (out.stderr or "")))
            res = Result("ok", f"codex login status: {said}", None, "codex login status") if out.exit_code == 0 \
                else Result("fail", f"codex login status exited {out.exit_code}: {said}", None, "codex login status")
        else:
            res = await check(cred, await asyncio.to_thread(fleet.broker.vault.get, f"cred:{cred.name}"))
        if res is not None:
            record(fleet.store, cred.name, res, "capture")
    except Exception:  # noqa: BLE001 - the login is stored either way; the nightly check looks again
        log.warning("checking the captured login %s failed", cred.name, exc_info=True)


# ====================================================================== what needs someone
def _attention(c: Credential) -> str | None:
    """ok | None when nothing is wrong; else the line people see."""
    h = c.health or {}
    left = (c.expires_at - now()) if c.expires_at else None
    if h.get("status") == "fail":
        return f"no longer works: {h.get('detail')}"
    if left is not None and left <= WARN_DAYS * 86400:
        return h.get("detail") or f"expires on {_day(c.expires_at or 0)}"
    return None


def _owner(c: Credential) -> str:
    return c.person or "team"


def pools(creds: list[Credential]) -> list[dict[str, Any]]:
    """Logins held by one sandbox at a time, grouped like teamcreds picks from them: whose, harness and kind."""
    groups: dict[tuple[str, str, str], list[Credential]] = {}
    for c in creds:
        if c.exclusive:
            groups.setdefault((_owner(c), c.harness, c.kind.value), []).append(c)
    out = []
    for (owner, harness, kind), cs in sorted(groups.items()):
        working = [c for c in cs if (c.health or {}).get("status") in ("ok", "warn")]
        out.append({"owner": owner, "harness": harness, "kind": kind, "logins": [c.name for c in cs],
                    "working": len(working), "leased": sum(1 for c in cs if c.leased_to),
                    "unchecked": sum(1 for c in cs if not c.health)})
    return out


def summary(store: Store) -> dict[str, Any]:
    creds = sorted(store.list(Credential), key=lambda c: c.name)
    counts: dict[str, int] = {}
    attention = []
    for c in creds:
        if not has_check(c):
            continue
        st = (c.health or {}).get("status", "unchecked")
        counts[st] = counts.get(st, 0) + 1
        if (why := _attention(c)) is not None:
            attention.append({"name": c.name, "owner": _owner(c), "harness": c.harness, "status": st,
                              "expires_at": c.expires_at, "why": why})
    return {"counts": counts, "attention": attention, "pools": pools(creds)}


def inbox_items(svc: Any, p: Principal | None) -> list[dict[str, Any]]:
    """inbox.SOURCES: a credential that stopped working or expires soon waits on whoever renews it: its person,
    or admins for the team's."""
    from hangar.inbox import _item, yours

    out = []
    for c in svc.store.list(Credential):
        if not has_check(c) or (why := _attention(c)) is None or not yours(c.person, p):
            continue
        out.append(_item("credential", f"credential {c.name} ({c.harness}) {why}"[:500],
                         "renew it, then: hangar cred check " + c.name,
                         float((c.health or {}).get("checked_at") or c.updated_at), credential=c.name))
    return out


def doctor_checks(h: Any) -> list[dict[str, Any]]:
    from hangar.doctor import _check

    s = summary(h.store)
    bad = [a for a in s["attention"] if a["status"] == "fail"]
    soon = [a for a in s["attention"] if a["status"] != "fail"]
    dead = [p for p in s["pools"] if len(p["logins"]) > 1 and not p["working"] and not p["unchecked"]]
    out = []
    if bad or dead:
        out.append(_check("credential health", False, "; ".join(
            [f"{a['name']}: {a['why']}" for a in bad] +
            [f"{p['owner']}'s {p['harness']} pool has no working login ({', '.join(p['logins'])})" for p in dead]),
            "renew each one, then `hangar cred check NAME`"))
    elif soon:
        out.append(_check("credential health", None, "; ".join(f"{a['name']}: {a['why']}" for a in soon),
                          "renew each one before it expires, then `hangar cred check NAME`"))
    else:
        n = sum(s["counts"].values())
        unchecked = s["counts"].get("unchecked", 0)
        out.append(_check("credential health", True, f"{n - unchecked} of {n} agent credential(s) checked and "
                          "working" + (f"; {unchecked} not checked yet (hangar cred check)" if unchecked else "")))
    return out


# ====================================================================== front doors
def mount(app: Any, h: Any, admin: Any) -> None:
    """POST /api/credentials/check (admins): check now, all or `names`."""
    from fastapi import Body

    @app.post("/api/credentials/check")
    async def credentials_check(body: dict[str, Any] = Body(default={}), who: str = admin):
        names = body.get("names") if isinstance(body.get("names"), list) else None
        res = await check_all(h.fleet, who, names=[str(n) for n in names] if names else None, budget=60)
        h.fleet.audit(who, "credential.check", None, checked=[r["name"] for r in res])
        return {"results": res, **summary(h.store)}


def register_inbox() -> None:
    """Credentials that need renewing show in the inbox (inbox.SOURCES), once."""
    from hangar import inbox

    if inbox_items not in inbox.SOURCES:
        inbox.SOURCES.append(inbox_items)
