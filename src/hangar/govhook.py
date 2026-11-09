"""The managed Tower hook: `governance.hook: required` (KAN-78; spec 0015 §9; docs/governance-hook.md).

A turn in an environment that requires it runs Claude Code with hangar's PreToolUse hook (tower_hook.py), which asks
Tower's `/hooks/claude-code` before every tool call and fails closed. The hook is hangar's, where the agent can't
reach it:

* **the script** is written as hangar's own user to /var/lib/hangar/tower_hook.py (0644), beside the driver. The
  agent can run it but not change it, nor the directory it's in (the uid split, isolation.py);
* **the settings** are the turn's hangar-owned `--settings` file (harden.py), loaded with `--setting-sources ''`: no
  user, project or local settings, so the agent's ~/.claude/settings.json, the repository's .claude/ and their
  `disableAllHooks` don't count. Managed settings outrank it and the uid split's setup checked the agent can't write
  them (isolation.SETUP). The hook runs on the system python3 by an absolute `env` and the system PATH, which the
  setup also checked, never through the agent's PATH;
* **the key** (`governance_hook_credential`, a coding agent's key in Tower) is a file of the run, removed when the
  run ends. The hook runs as the agent, so the agent can read it: it can ask Tower what the hook asks, nothing more.

Where hangar can't own all of that the turn is refused, whatever HANGAR_UID_SPLIT says: no split (GKE, E2B, Docker
with `sudo: true` or HANGAR_UID_SPLIT=off), no hangar-owned settings (harden.plan says why), no Tower URL or key, or
the installed script's digest isn't this one. Codex turns are refused too (`CODEX`, docs/governance-hook.md#codex).

One path per turn (§9): with the hook on, the driver answers from its local `deny-risky` rules instead of `ask` or
`governed`, so the driver never relays a call to Tower as well (governed.Governed.tick) and one call never raises two
approvals. Tower allowing a call never skips that guard rail: the hook prints nothing on an allow.

Integrity: the script's digest is the turn's record (`SessionTurn.governance_hook`, the manifest's `hooks` entry, so
it is part of harness_digest). Each turn reads the installed script before writing it again: a digest other than the
one hangar installed last is audited `governance.hook_tampered` and warned about on the turn; after writing, a digest
other than this file's refuses the turn.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import shlex
from pathlib import Path
from typing import TYPE_CHECKING, Any

from hangar import isolation

if TYPE_CHECKING:
    from hangar.config import Settings
    from hangar.harden import Plan
    from hangar.harnesses import Harness
    from hangar.models import Sandbox, Session, SessionTurn
    from hangar.providers.base import Provider

log = logging.getLogger("hangar.govhook")

SCRIPT = Path(__file__).with_name("tower_hook.py")
NAME = "tower_hook.py"
INSTALLED = f"{isolation.HOME}/{NAME}"
SOURCE = "hangar:tower_hook"
MATCHER = "*"  # every tool: Tower's from_hook maps what it knows and asks about the rest (use_tool)
KV = "govhook:"  # + sandbox id: the digest hangar last installed there
ENV = "/usr/bin/env"
SLACK = 60  # seconds between the hook's own deadline and the timeout Claude Code gives it
CODEX = ("Codex can't run hangar's Tower hook where the agent can't remove it: a hardened Codex turn runs with "
         "hooks off (`--disable hooks`), because turning them on also runs hooks the agent writes and trusts in "
         "~/.codex (docs/governance-hook.md#codex)")


def digest() -> str:
    return "sha256:" + hashlib.sha256(SCRIPT.read_bytes()).hexdigest()


def required(settings: Settings, spec: Any) -> bool:
    """The environment's `governance.hook`, or HANGAR_GOVERNANCE_HOOK: either one saying `required` is enough (an
    environment can tighten the default, never loosen it)."""
    gov = getattr(spec, "governance", None) if spec is not None else None
    return settings.governance_hook == "required" or (gov is not None and gov.hook == "required")


def required_for(svc: Any, s: Session) -> bool:
    spec = None
    with contextlib.suppress(KeyError, ValueError):
        spec = svc.fleet.catalog.get(s.environment)
    return required(svc.settings, spec)


def driver_policy(policy: str) -> str:
    """What the driver answers with when the hook is on (§9): Tower decides through the hook, and the driver keeps
    hangar's deny-risky rules as a guard rail, so `ask` and `governed` never ask anyone a second time."""
    return "deny-risky" if policy in ("ask", "governed") else policy


def why_not(settings: Settings, h: Harness) -> str | None:
    """Why the hook can't be installed for this harness and these settings (None: it can, given a split)."""
    if h.live != "claude":
        return CODEX if h.live == "codex" else f"{h.name} has no hook hangar can own"
    if not settings.governance_url:
        return "HANGAR_GOVERNANCE_URL isn't set"
    if getattr(settings, "governance_hook_auth", "key") == "passport":  # --- KAN-33: no stored key; hangar's issuer
        if not (settings.oidc_issuer or settings.public_url.startswith("https://")):
            return "HANGAR_GOVERNANCE_HOOK_AUTH=passport needs HANGAR_OIDC_ISSUER or an https HANGAR_PUBLIC_URL"
        return None
    if not settings.governance_hook_credential:
        return "HANGAR_GOVERNANCE_HOOK_CREDENTIAL isn't set"
    return None


def command(run: str, url: str, wait: int, script: str = INSTALLED) -> str:
    """The hook's command in the settings file. The system python3 by an absolute path and the system PATH, -I (no
    PYTHON* variables, user site-packages or modules beside the script), and `|| exit 2`: a hook that can't start
    exits 127 or 1, which Claude Code would treat as no answer and let the call through."""
    from hangar.providers.docker import SYSTEM_PATH

    q = shlex.quote
    return (f"{ENV} PATH={SYSTEM_PATH} python3 -I {q(script)} --url {q(url)} --key-file {q(run + '.tower-key')} "
            f"--wait {int(wait)} || exit 2")


def hooks(run: str, url: str, wait: int) -> dict[str, Any]:
    return {"matcher": MATCHER, "hooks": [{"type": "command", "command": command(run, url, wait),
                                           "timeout": int(wait) + SLACK}]}


def merge(settings: dict[str, Any], entry: dict[str, Any]) -> dict[str, Any]:
    """hangar's hook first in PreToolUse; the environment's own hooks stay. A deny from any hook wins in Claude Code,
    so another hook can add a refusal, never take hangar's away."""
    out = dict(settings)
    hk = dict(out.get("hooks") or {})
    hk["PreToolUse"] = [entry, *(hk.get("PreToolUse") or [])]
    out["hooks"] = hk
    out.pop("disableAllHooks", None)  # the environment's own settings can't switch hangar's hook off either
    return out


def apply(fleet: Any, hard: Plan, run_id: str, wait: int, passport: tuple[str, float] | None = None) -> dict[str, Any]:
    """Add the hook and its key to a hardened Claude Code turn's files. Returns the turn's record. Raises
    ValueError with the reason when it can't. `passport`: the sandbox's own (`sandbox_passport`) instead of the
    shared agent key (--- KAN-33)."""
    settings = fleet.settings
    r = f"{isolation.RUNS}/{run_id}"
    name = f"{run_id}.settings.json"
    if name not in hard.files or hard.files[name][0] is None:
        raise ValueError("the turn has no hangar-owned settings file")
    if passport is not None:
        key = passport[0]
    else:
        try:
            key = fleet.broker.secret_text(settings.governance_hook_credential)
        except Exception as e:  # noqa: BLE001 - CredentialError, or no vault: no hook, no turn
            raise ValueError(f"the hook's key ({settings.governance_hook_credential}) can't be read: {e}") from e
    if not key or "\n" in key:
        raise ValueError(f"the hook's key ({settings.governance_hook_credential}) is empty or not one line")
    entry = hooks(r, settings.governance_url or "", wait)
    doc = merge(json.loads(hard.files[name][0] or b"{}"), entry)
    hard.files[name] = (json.dumps(doc).encode(), 0o644)
    hard.files[f"{run_id}.tower-key"] = (key.encode(), 0o644)  # the hook runs as the agent: it must read it
    return {"runtime": "claude-code", "source": SOURCE, "path": INSTALLED, "digest": digest(), "matcher": MATCHER,
            "url": settings.governance_url, "wait_seconds": int(wait), "timeout_seconds": int(wait) + SLACK,
            "fail": "closed", "auth": "passport" if passport is not None else "key",
            **({"key_expires_at": passport[1]} if passport is not None else {})}


# ====================================================================== --- KAN-33 (#174): per-sandbox passports
PASS = "govhook-pass:"  # kv: + run id -> {"sandbox", "at"}: a run whose hook holds a passport hangar renews
RENEW_BELOW = 600.0  # seconds left: the refresh loop runs every 300 s, a passport lives 900 s
RUN_MAX = 48 * 3600  # a run older than this is no longer renewed


def _person_email(fleet: Any, person: str | None) -> str | None:
    from hangar import people

    p = people.get(fleet.store, person) if person else None
    return next((k.split(":", 1)[1] for k in (p.links if p else []) if k.startswith("email:")), None)


async def sandbox_passport(fleet: Any, sb: Sandbox, *, client: Any = None) -> tuple[str, float]:
    """The sandbox's own passport for Tower's hooks: hangar mints the sandbox's token (`sub: hangar:env:<env>`, its
    person, and the email linked to that person, for Tower's `requester_claim`) for Tower's token endpoint,
    exchanges it (RFC 8693), and checks the passport (issuer, audience, expiry, chain, the agent). A sandbox with a
    person must get a passport naming a person. Returns (passport, expiry); ValueError says why not."""
    from hangar.governance import CredentialUnavailable, PassportCredential, PassportRefused, PassportVerifier

    settings = fleet.settings
    url = (settings.governance_url or "").rstrip("/")
    if not url:
        raise ValueError("HANGAR_GOVERNANCE_URL isn't set")
    token_url = settings.governance_token_url or f"{url}/oauth/token"
    email = _person_email(fleet, sb.person)

    def mint() -> str:
        return fleet.cloud.mint(sb, token_url, 5, email=email)[0]

    verifier = PassportVerifier(url, [f"{url}/hooks", f"{url}/mcp", url], client=client)
    cred = PassportCredential(mint, token_url, agent=settings.governance_hook_agent, verifier=verifier, client=client)
    try:
        passport, exp = await cred.exchange()
        claims = await verifier.verify(passport)
    except (CredentialUnavailable, PassportRefused) as e:
        raise ValueError(f"Tower gave this sandbox no passport: {e}") from None
    finally:
        if client is None:
            await cred.close()
    if sb.person and not str(claims.get("sub") or "").startswith("person:"):
        raise ValueError(f"Tower's passport for this sandbox names {claims.get('sub')!r}, not its person")
    return passport, exp


def follow(fleet: Any, sb: Sandbox, run_id: str, expires: float) -> None:
    """Renew this run's passport while it runs (`refresh`)."""
    import time

    fleet.store.kv_set(PASS + run_id, json.dumps({"sandbox": sb.id, "at": time.time(), "expires": expires}),
                       durable=True)


def renew_script(run_id: str) -> str:
    """As hangar's user: replace the run's key with the new one beside it, only while the run's key is still there
    (a run that ended removed it, and its passport must not come back)."""
    q = shlex.quote
    key = q(f"{isolation.RUNS}/{run_id}.tower-key")
    new = q(f"{isolation.RUNS}/{run_id}.tower-key.new")
    return f"if [ -e {key} ]; then mv -f {new} {key} && echo renewed; else rm -f {new}; echo gone; fi"


async def refresh(fleet: Any, *, client: Any = None) -> int:
    """Renew the passports of running hooked runs that have under RENEW_BELOW seconds left. Returns how many."""
    import time

    from hangar.models import Sandbox as SandboxModel
    from hangar.models import SandboxState

    n = 0
    for key in fleet.store.kv_keys(PASS):
        run_id = key[len(PASS):]
        try:
            rec = json.loads(fleet.store.kv_get(key) or "{}")
        except ValueError:
            rec = {}
        sb = fleet.store.get(SandboxModel, rec.get("sandbox") or "")
        if sb is None or sb.state != SandboxState.RUNNING or time.time() - float(rec.get("at") or 0) > RUN_MAX:
            fleet.store.kv_delete(key, durable=True)
            continue
        if float(rec.get("expires") or 0) - time.time() > RENEW_BELOW:
            continue
        try:
            passport, exp = await sandbox_passport(fleet, sb, client=client)
            _, prov = fleet._running(sb.id)
            await isolation.put(prov, sb, isolation.RUNS, {f"{run_id}.tower-key.new": (passport.encode(), 0o644)})
            res = await isolation.sh(prov, sb, renew_script(run_id), timeout=30)
        except Exception as e:  # noqa: BLE001 - one run must not stop the others; its hook fails closed
            log.warning("run %s: the Tower hook's passport wasn't renewed: %s", run_id, e)
            continue
        if "gone" in (res.stdout or "") or res.exit_code != 0:
            fleet.store.kv_delete(key, durable=True)  # the run ended
            continue
        rec["expires"] = exp
        fleet.store.kv_set(key, json.dumps(rec), durable=True)
        n += 1
    return n


# Run as hangar's user. The digest of what is there now (or -), then nothing else.
BEFORE = f"sha256sum {INSTALLED} 2>/dev/null | cut -d' ' -f1 || true"


def check_script(run_id: str) -> str:
    """After writing: the system python3 runs the hook, the settings file names it, and the installed digest."""
    from hangar.providers.docker import SYSTEM_PATH

    q = shlex.quote
    s = q(f"{isolation.RUNS}/{run_id}.settings.json")
    return (f"export PATH={SYSTEM_PATH}; [ -x {ENV} ] || {{ echo 'no {ENV}'; exit 3; }}; "
            "python3 -I -c 'import sys; sys.exit(sys.version_info < (3, 8))' 2>/dev/null "
            "|| { echo 'no python3 >= 3.8 on the system PATH'; exit 3; }; "
            f"grep -qF {q(INSTALLED)} {s} || {{ echo 'the settings file does not name the hook'; exit 3; }}; "
            f"sha256sum {q(INSTALLED)} | cut -d' ' -f1")


async def before(fleet: Any, prov: Provider, sb: Sandbox, s: Session, turn: SessionTurn) -> None:
    """Tamper check: what hangar finds of the hook it installed last time. Never fails the turn: hangar writes the
    script again next, and checks that."""
    last = fleet.store.kv_get(KV + sb.id)
    if not last:
        return
    try:
        res = await isolation.sh(prov, sb, BEFORE, timeout=30)
        found = "sha256:" + res.stdout.strip() if res.stdout.strip() else None
    except Exception as e:  # noqa: BLE001
        log.warning("sandbox %s: could not read the Tower hook: %s", sb.id, e)
        return
    if found == last:
        return
    what = "is gone" if found is None else f"has digest {found}, not {last}"
    turn.warnings.append(f"hangar's Tower hook in this sandbox {what} since hangar installed it: something changed a "
                         "file of hangar's own user. hangar installs it again for this turn "
                         "(docs/governance-hook.md#integrity)")
    fleet.audit("hangar", "governance.hook_tampered", s.id, sandbox=sb.id, expected=last, found=found)


async def verify(fleet: Any, prov: Provider, sb: Sandbox, run_id: str) -> str | None:
    """After writing the hook and the run's files: None when the hook is there as hangar meant it, else why not."""
    try:
        res = await isolation.sh(prov, sb, check_script(run_id), timeout=30)
    except Exception as e:  # noqa: BLE001
        return f"hangar couldn't check the hook it installed ({e})"[:300]
    out = res.stdout.strip().splitlines()
    if res.exit_code != 0:
        return (out[-1] if out else (res.stderr or "").strip()[-200:] or f"exit {res.exit_code}")[:300]
    found = "sha256:" + (out[-1] if out else "")
    if found != digest():
        return f"the installed hook has digest {found}, not {digest()}"
    fleet.store.kv_set(KV + sb.id, found, durable=True)
    return None


def forget(fleet: Any, sandbox_id: str) -> None:
    with contextlib.suppress(Exception):
        fleet.store.kv_delete(KV + sandbox_id)


def manifest_entry(turn: SessionTurn | None, harness: str | None) -> dict[str, Any] | None:
    """The manifest's `hooks` entry for a turn that ran with the hook (part of harness_digest: event, source and
    digest)."""
    rec = turn.governance_hook if turn is not None else None
    if not rec:
        return None
    return {"event": "PreToolUse", "source": SOURCE, "harness": harness, "digest": rec.get("digest"),
            "path": rec.get("path"), "matcher": rec.get("matcher"), "fail": rec.get("fail"),
            "requested_approvals": rec.get("policy")}
