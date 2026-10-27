"""Credential health (--- clouddoctor): checks that call no model, recorded on the credential, and who hears."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

from hangar import credhealth, identity, inbox
from hangar.auth import Principal
from hangar.models import Credential, CredentialKind, now
from tests.conftest import scratch_root

ALICE = Principal("alice-laptop", "tok-a", ["user"], person="alice", role="member")
BOB = Principal("bob-laptop", "tok-b", ["user"], person="bob", role="member")
ADMIN = Principal("ops", "tok-o", ["admin"], person="owner", role="admin")
TOKEN = "sk-ant-oat01-" + "x" * 40
CODEX = json.dumps({"tokens": {"id_token": "a", "access_token": "b", "refresh_token": "c"},
                    "last_refresh": "2026-10-01T10:00:00Z"})


def _aged(store, name: str, days: float) -> None:
    c = store.get(Credential, name)
    c.updated_at = now() - days * 86400
    store.put(c)


async def test_a_setup_token_expires_a_year_after_it_was_stored(fleet, broker, store):
    broker.put("claude-a", "claude-code", CredentialKind.OAUTH_TOKEN, TOKEN, person="alice")
    [r] = await credhealth.check_all(fleet, "test")
    c = store.get(Credential, "claude-a")
    assert r["status"] == "ok" and c.health["status"] == "ok" and c.last_ok
    assert abs(c.expires_at - (c.updated_at + 365 * 86400)) < 1
    _aged(store, "claude-a", 360)
    [r] = await credhealth.check_all(fleet, "test")
    assert r["status"] == "warn" and "in 5 day(s)" in r["detail"] and "claude setup-token" in r["detail"]
    _aged(store, "claude-a", 400)
    [r] = await credhealth.check_all(fleet, "test")
    c = store.get(Credential, "claude-a")
    assert r["status"] == "fail" and c.health["status"] == "fail" and "expired on" in c.health["detail"]
    assert TOKEN not in json.dumps(c.model_dump(mode="json"))
    # storing it again starts over
    broker.put("claude-a", "claude-code", CredentialKind.OAUTH_TOKEN, TOKEN, person="alice")
    assert store.get(Credential, "claude-a").health is None


async def test_codex_logins_and_api_keys(fleet, broker):
    broker.put("cx", "codex", CredentialKind.AUTH_FILE, CODEX)
    broker.put("cx-empty", "codex", CredentialKind.AUTH_FILE, "{}")
    broker.put("ant", "claude-code", CredentialKind.API_KEY, "sk-ant-api03-" + "k" * 40)
    broker.put("gw", "claude-code", CredentialKind.API_KEY, "gateway-key-123")
    broker.put("git", "git", CredentialKind.GIT_TOKEN, "ghp_whatever")
    got = {r["name"]: r for r in await credhealth.check_all(fleet, "test")}
    assert got["cx"]["status"] == "ok" and "ChatGPT" in got["cx"]["detail"] and "2026-10-01" in got["cx"]["detail"]
    assert got["cx-empty"]["status"] == "fail" and "log in again" in got["cx-empty"]["detail"]
    assert got["ant"]["status"] == "ok" and got["gw"]["status"] == "warn"
    assert "git" not in got  # no check for git tokens
    # results younger than max_age are kept
    assert await credhealth.check_all(fleet, "test", max_age=3600) == []


def _fake_gcloud(tmp: Path, print_token_rc: int = 0, before: str = "") -> Path:
    log = tmp / "argv.log"
    keys = json.dumps([{"name": "projects/p/serviceAccounts/sa@p.iam.gserviceaccount.com/keys/abc123",
                        "validBeforeTime": before}]) if before else "[]"
    script = tmp / "gcloud"
    script.write_text(f"""#!/usr/bin/env bash
echo "$CLOUDSDK_CONFIG $*" >> {log}
case "$1 $2" in
  "auth activate-service-account") exit 0 ;;
  "auth print-access-token") [ {print_token_rc} = 0 ] && echo ya29.SECRET-ACCESS-TOKEN && exit 0
      echo "ERROR: (gcloud.auth.print-access-token) invalid_grant: Invalid JWT Signature." >&2; exit 1 ;;
  "iam service-accounts") echo '{keys}' ;;
esac
""")
    script.chmod(0o755)
    return script


SA = json.dumps({"type": "service_account", "client_email": "sa@p.iam.gserviceaccount.com",
                 "private_key_id": "abc123",
                 "private_key": "-----BEGIN PRIVATE KEY-----\nAAA\n-----END PRIVATE KEY-----\n"})


async def test_service_account_keys_are_checked_with_gcloud_in_a_config_of_their_own(fleet, broker, store):
    tmp = Path(scratch_root()) / "gcloud-ok"
    tmp.mkdir()
    try:
        g = _fake_gcloud(tmp, before=_iso(now() + 5 * 86400))
        broker.put("vertex", "pi", CredentialKind.GENERIC, SA)
        c = store.get(Credential, "vertex")
        assert credhealth.has_check(c)
        res = await credhealth.check(c, SA.encode(), str(g))
        assert res.status == "warn" and res.expires_at and "sa@p.iam" in res.detail
        argv = (tmp / "argv.log").read_text()
        assert "auth activate-service-account sa@p.iam.gserviceaccount.com --key-file=" in argv
        assert "auth print-access-token sa@p.iam.gserviceaccount.com" in argv
        cfg = argv.split()[0]
        assert cfg.endswith("/gcloud") and "hangar-gcloud-" in cfg and not Path(cfg).parent.exists()
        assert "ya29" not in res.detail
        # a refused key fails, with gcloud's own words
        bad = await credhealth.check(c, SA.encode(), str(_fake_gcloud(tmp, print_token_rc=1)))
        assert bad.status == "fail" and "invalid_grant" in bad.detail
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _iso(ts: float) -> str:
    from datetime import UTC, datetime

    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


async def test_without_gcloud_a_key_is_unknown(fleet, broker, store, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda _: None)
    broker.put("vertex", "antigravity", CredentialKind.GENERIC, SA)
    res = await credhealth.check(store.get(Credential, "vertex"), SA.encode())
    assert res.status == "unknown" and "gcloud isn't installed" in res.detail


async def test_a_captured_codex_login_is_checked_with_codex_login_status(fleet, broker, store):
    sb = await fleet.create("plain", name="login-codex")
    box = fleet.providers["docker"].boxes[sb.id]
    bindir = box / "home/agent/bin"
    bindir.mkdir(parents=True)
    (bindir / "codex").write_text('#!/bin/sh\n[ "$1 $2" = "login status" ] && echo "Logged in using ChatGPT" && '
                                  'exit 0\nexit 9\n')
    (bindir / "codex").chmod(0o755)
    with (box / "home/agent/.hangar/env").open("a") as f:
        f.write(f"PATH={bindir}:$PATH\n")
    cred = broker.put("cx", "codex", CredentialKind.AUTH_FILE, CODEX, person="alice")
    await credhealth.on_capture(fleet, cred, sb.id)
    c = store.get(Credential, "cx")
    assert c.health["status"] == "ok" and c.health["by"] == "capture"
    assert c.health["detail"] == "codex login status: Logged in using ChatGPT"
    (bindir / "codex").write_text("#!/bin/sh\necho 'Not logged in'\nexit 1\n")
    await credhealth.on_capture(fleet, cred, sb.id)
    c = store.get(Credential, "cx")
    assert c.health["status"] == "fail" and "exited 1: Not logged in" in c.health["detail"]
    # a sandbox that's gone never fails the capture
    await credhealth.on_capture(fleet, cred, "sb-gone")
    # --- review: `codex login status` prints an API key's first 8 and last 5 characters; neither is kept
    (bindir / "codex").write_text("#!/bin/sh\necho 'Logged in using an API key - sk-proj-***Q7xZ9' >&2\nexit 0\n")
    await credhealth.on_capture(fleet, cred, sb.id)
    c = store.get(Credential, "cx")
    assert c.health["status"] == "ok" and "Q7xZ9" not in json.dumps(c.health), c.health
    assert "API key" in c.health["detail"]


async def test_a_check_of_a_replaced_value_isnt_recorded_on_the_new_one(fleet, broker, store, monkeypatch):
    """--- review: a value stored again while its old one was being checked starts over; the old result is dropped."""
    import asyncio

    broker.put("cx", "codex", CredentialKind.AUTH_FILE, "{}")  # fails
    real = credhealth.check

    async def slow(c, value, gcloud=None):
        res = await real(c, value, gcloud)
        broker.put("cx", "codex", CredentialKind.AUTH_FILE, CODEX)  # renewed meanwhile
        await asyncio.sleep(0)
        return res

    monkeypatch.setattr(credhealth, "check", slow)
    await credhealth.check_all(fleet, "test")
    assert store.get(Credential, "cx").health is None  # the next check looks at the new value


async def test_who_hears_about_an_expiring_credential(fleet, broker, store, monkeypatch):
    broker.put("alice-claude", "claude-code", CredentialKind.OAUTH_TOKEN, TOKEN, person="alice")
    broker.put("team-claude", "claude-code", CredentialKind.OAUTH_TOKEN, TOKEN)
    for n in ("alice-claude", "team-claude"):
        _aged(store, n, 358)
    await credhealth.check_all(fleet, "test")
    svc = type("Svc", (), {"store": store})()
    mine = {i["credential"] for i in credhealth.inbox_items(svc, ALICE)}
    assert mine == {"alice-claude"}
    assert credhealth.inbox_items(svc, BOB) == []
    assert {i["credential"] for i in credhealth.inbox_items(svc, ADMIN)} == {"team-claude"}
    credhealth.register_inbox()
    credhealth.register_inbox()
    assert inbox.SOURCES.count(credhealth.inbox_items) == 1
    tok = identity.current.set(ALICE)
    try:
        assert credhealth.inbox_items(svc, identity.principal())[0]["kind"] == "credential"
    finally:
        identity.current.reset(tok)
    h = type("H", (), {"store": store})()
    [chk] = credhealth.doctor_checks(h)
    assert chk["status"] == "warn" and "alice-claude" in chk["detail"]


async def test_a_pool_with_no_working_login_fails_the_doctor(fleet, broker, store):
    for n in ("codex-1", "codex-2"):
        broker.put(n, "codex", CredentialKind.AUTH_FILE, "{}", person="alice")
    await credhealth.check_all(fleet, "test")
    s = credhealth.summary(store)
    [pool] = s["pools"]
    assert pool == {"owner": "alice", "harness": "codex", "kind": "auth_file", "logins": ["codex-1", "codex-2"],
                    "working": 0, "leased": 0, "unchecked": 0}
    [chk] = credhealth.doctor_checks(type("H", (), {"store": store})())
    assert chk["status"] == "fail" and "alice's codex pool has no working login" in chk["detail"]
    assert os.environ.get("CLOUDSDK_CONFIG") is None


def test_credential_health_never_rolls_back_a_deploy(store):
    """--- integ3 (review #103): `doctor` is a core acceptance check, and a person's login can expire during a deploy
    window; credential health's results say `deploy_gate: false`, so the gate reports them instead of rolling back."""
    from types import SimpleNamespace

    from hangar import credhealth

    out = credhealth.doctor_checks(SimpleNamespace(store=store))
    assert out and all(c["name"] == "credential health" and c["deploy_gate"] is False for c in out)


# --- KAN-178: a run whose login the vendor refused marks it failed, and no file check says ok again
async def test_a_login_the_vendor_refused_stays_failed_until_stored_again(fleet, broker, store):
    from hangar import doctor  # noqa: F401 - doctor_checks imports from it

    broker.put("codex-chatgpt", "codex", CredentialKind.AUTH_FILE, CODEX, person="owner")
    broker.put("ant", "claude-code", CredentialKind.API_KEY, "sk-ant-api03-" + "k" * 40, person="owner")
    [r] = [x for x in await credhealth.check_all(fleet, "nightly") if x["name"] == "codex-chatgpt"]
    assert r["status"] == "ok"  # the file has a refresh token; whether OpenAI takes it, it can't say
    line = credhealth.login_rejected(store, ["codex-chatgpt", "ant", "gone"], "codex", "run-87be38a0")
    assert line.startswith(credhealth.REFUSED_PREFIXES)
    assert "codex-chatgpt needs a fresh `codex login`; re-store the credential" in line
    assert "hangar cred add codex-chatgpt --harness codex --kind auth_file" in line
    c = store.get(Credential, "codex-chatgpt")
    assert c.health["status"] == "fail" and c.health["method"] == credhealth.REFUSED_METHOD
    assert c.health["by"] == "run:run-87be38a0" and "run-87be38a0" in c.health["detail"]
    assert store.get(Credential, "ant").health["status"] == "ok"  # another harness's credential is left alone
    assert CODEX not in json.dumps(c.model_dump(mode="json"))
    # the nightly check keeps the refusal: the file can't contradict the vendor
    got = {x["name"]: x for x in await credhealth.check_all(fleet, "nightly")}
    assert got["codex-chatgpt"]["status"] == "fail"
    assert store.get(Credential, "codex-chatgpt").health["status"] == "fail"
    # so health, doctor and the owner's inbox say so
    s = credhealth.summary(store)
    assert [a["name"] for a in s["attention"]] == ["codex-chatgpt"]
    [chk] = credhealth.doctor_checks(type("H", (), {"store": store})())
    assert chk["status"] == "fail" and "codex-chatgpt" in chk["detail"] and "fresh `codex login`" in chk["detail"]
    # storing it again starts over, and the next check looks at the new value
    broker.put("codex-chatgpt", "codex", CredentialKind.AUTH_FILE, CODEX.replace('"c"', '"c2"'), person="owner")
    assert store.get(Credential, "codex-chatgpt").health is None
    got = {x["name"]: x for x in await credhealth.check_all(fleet, "nightly")}
    assert got["codex-chatgpt"]["status"] == "ok"


def test_a_refused_login_with_no_credential_on_record_still_says_what_to_do(store):
    line = credhealth.login_rejected(store, [], "codex", "run-1")
    assert line.startswith(credhealth.REFUSED_PREFIXES) and "log in again" in line
