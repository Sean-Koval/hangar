import pytest

from hangar.broker import CredentialError
from hangar.models import CredentialKind, Sandbox
from tests.conftest import FakeProvider


def test_put_maps_slots_and_encrypts(broker, store):
    c = broker.put("claude", "claude-code", CredentialKind.OAUTH_TOKEN, "sk-ant-oat01-secret")
    assert c.env_var == "CLAUDE_CODE_OAUTH_TOKEN" and not c.exclusive
    assert b"secret" not in store.get_secret("cred:claude")
    assert broker.vault.get_text("cred:claude") == "sk-ant-oat01-secret"
    codex = broker.put("codex", "codex", CredentialKind.AUTH_FILE, "{}")
    assert codex.exclusive and codex.file_path == ".codex/auth.json"


def test_unknown_slot_needs_custom_mapping(broker):
    with pytest.raises(CredentialError, match="no slot"):
        broker.put("x", "codex", CredentialKind.OAUTH_TOKEN, "v")
    c = broker.put("x", "custom", CredentialKind.GENERIC, "v", env_var="MY_TOKEN")
    assert c.env_var == "MY_TOKEN"


def test_exclusive_lease(broker):
    broker.put("codex", "codex", CredentialKind.AUTH_FILE, "{}")
    broker.acquire(["codex"], "sb-1")
    broker.acquire(["codex"], "sb-1")  # idempotent for the holder
    with pytest.raises(CredentialError, match="in use by sandbox sb-1"):
        broker.acquire(["codex"], "sb-2")
    broker.force_release("codex")
    broker.acquire(["codex"], "sb-2")


async def test_inject_and_write_back(broker):
    prov = FakeProvider()
    sb = Sandbox(name="t", environment="e", provider="fake")
    await prov.create(sb, "img")
    broker.put("key", "claude-code", CredentialKind.API_KEY, "sk-ant-api-1")
    # a person's own login: one whose account can't be told is still written back (team ones: test_writeback_*)
    broker.put("codex", "codex", CredentialKind.AUTH_FILE, '{"refresh":"r1"}', person="alice")
    creds = broker.acquire(["key", "codex"], sb.id)
    await broker.inject(sb, prov, creds)
    env = (await prov.read_file(sb, "/home/agent/.hangar/env")).decode()
    assert "export ANTHROPIC_API_KEY=sk-ant-api-1" in env
    assert await prov.read_file(sb, "/home/agent/.codex/auth.json") == b'{"refresh":"r1"}'
    # re-inject replaces rather than duplicates
    await broker.inject(sb, prov, creds)
    env = (await prov.read_file(sb, "/home/agent/.hangar/env")).decode()
    assert env.count("ANTHROPIC_API_KEY") == 1
    # the harness rotates its refresh token; release copies it back
    await prov.write_file(sb, "/home/agent/.codex/auth.json", b'{"refresh":"r2"}')
    await broker.release(sb, prov)
    assert broker.vault.get_text("cred:codex") == '{"refresh":"r2"}'
    assert broker.get("codex").leased_to is None


# --- KAN-168: write-back stores a changed login only when it is the same account


def _jwt(claims: dict) -> str:
    import base64
    import json

    def enc(d: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")

    return f"{enc({'alg': 'RS256'})}.{enc(claims)}.c2ln"


def _codex(account: str, user: str, refresh: str) -> bytes:
    import json

    auth = {"chatgpt_account_id": account, "chatgpt_user_id": user}
    claims = {"iss": "https://auth.openai.com", "sub": user, "https://api.openai.com/auth": auth,
              "jti": f"issued-with-{refresh}"}  # a refresh issues new id and access tokens with the new refresh token
    return json.dumps({"tokens": {"id_token": _jwt(claims), "access_token": _jwt(claims), "refresh_token": refresh,
                                  "account_id": account}}).encode()


async def _release_after(broker, stored: bytes, read_back: bytes, person: str | None = None):
    prov = FakeProvider()
    sb = Sandbox(name="t", environment="e", provider="fake")
    await prov.create(sb, "img")
    broker.put("codex", "codex", CredentialKind.AUTH_FILE, stored, person=person)
    creds = broker.acquire(["codex"], sb.id)
    await broker.inject(sb, prov, creds)
    await prov.write_file(sb, "/home/agent/.codex/auth.json", read_back)
    await broker.release(sb, prov)
    assert broker.get("codex").leased_to is None
    return sb, [e for e in broker.store.audit_tail() if e.action == "credential.writeback_refused"]


async def test_writeback_same_account_refresh_is_stored(broker):
    old, new = _codex("acct-team", "user-1", "rt-old"), _codex("acct-team", "user-1", "rt-new")
    _, refused = await _release_after(broker, old, new, person="alice")
    assert broker.vault.get("cred:codex") == new
    assert refused == []


async def test_writeback_other_account_is_refused_and_audited(broker):
    old, evil = _codex("acct-team", "user-1", "rt-old"), _codex("acct-attacker", "user-9", "rt-evil")
    sb, refused = await _release_after(broker, old, evil)
    assert broker.vault.get("cred:codex") == old
    assert len(refused) == 1
    e = refused[0]
    assert e.target == "codex" and e.detail["sandbox"] == sb.id and e.detail["team"] is True
    assert "rt-" not in repr(e.model_dump())  # never the content
    # a person's own login is held to the same account too
    _, refused = await _release_after(broker, old, evil, person="alice")
    assert broker.vault.get("cred:codex") == old and len(refused) == 2


async def test_writeback_file_mixing_two_accounts_is_refused(broker):
    import json

    old = _codex("acct-team", "user-1", "rt-old")
    mixed = json.loads(old)
    mixed["tokens"]["access_token"] = json.loads(_codex("acct-attacker", "user-9", "x"))["tokens"]["access_token"]
    _, refused = await _release_after(broker, old, json.dumps(mixed).encode(), person="alice")
    assert broker.vault.get("cred:codex") == old and len(refused) == 1
    # the other way round: an attacker's id token, the stored file's access token
    mixed = json.loads(old)
    mixed["tokens"]["id_token"] = json.loads(_codex("acct-attacker", "user-9", "x"))["tokens"]["id_token"]
    _, refused = await _release_after(broker, old, json.dumps(mixed).encode(), person="alice")
    assert broker.vault.get("cred:codex") == old and len(refused) == 2


async def test_writeback_unparseable_team_login_is_refused(broker):
    old = _codex("acct-team", "user-1", "rt-old")
    for bad in (b"not json at all", b'{"tokens": {"refresh_token": "rt-evil"}}'):
        _, refused = await _release_after(broker, old, bad)
        assert broker.vault.get("cred:codex") == old
    assert len(refused) == 2
    assert all(e.detail["reason"].startswith("team credential") for e in refused)
    # nor is one that keeps the stored account_id but drops the tokens that say whose it is
    dropped = b'{"tokens": {"refresh_token": "rt-evil", "account_id": "acct-team"}}'
    _, refused = await _release_after(broker, old, dropped)
    assert broker.vault.get("cred:codex") == old and len(refused) == 3
    # a person's own login whose account can't be told is still stored, as before
    await _release_after(broker, old, b"not json at all", person="alice")
    assert broker.vault.get("cred:codex") == b"not json at all"


def test_account_of_gemini_and_unknown_files():
    from hangar.authident import account, same

    idt = _jwt({"iss": "https://accounts.google.com", "sub": "1234", "email": "A@x.com"})
    g = account(f'{{"access_token": "ya29.x", "refresh_token": "1//r", "id_token": "{idt}"}}')
    assert g == {"sub@https://accounts.google.com": "1234", "email": "a@x.com"}
    assert account('{"anthropic": {"type": "oauth", "refresh": "r", "access": "sk-ant-oat01-x"}}') is None
    assert account(b"\xff\xfe") is None and account("[]") is None
    assert same(g, None) is None and same(None, g) is None
    assert same(g, {**g, "openai.user_id": "u"}) is True  # a refresh may add a claim
    assert same(g, {"email": "a@x.com"}) is False  # but not drop one
    assert same(g, {**g, "email": "b@x.com"}) is False


# --- KAN-168 follow-up: the copy-and-swap a sandbox can do with unverified claims
def _swap(stored: bytes, refresh: str) -> bytes:
    import json

    d = json.loads(stored)
    d["tokens"]["refresh_token"] = refresh
    return json.dumps(d).encode()


async def test_a_team_login_is_never_written_back_even_naming_the_same_account(broker):
    """The bypass found on #366: the stored id and access tokens copied, another account's refresh token swapped in.
    For a team login nothing a sandbox returns is stored, so this can't move members to another account."""
    old = _codex("acct-team", "user-1", "rt-old")
    for back in (_swap(old, "rt-attacker"), _codex("acct-team", "user-1", "rt-new")):
        sb, refused = await _release_after(broker, old, back)
        assert broker.vault.get("cred:codex") == old
        newest = refused[0]  # audit_tail is newest first
        assert newest.detail["team"] is True and newest.detail["sandbox"] == sb.id
        assert newest.detail["reason"].startswith("team credential")
    assert len(refused) == 2


async def test_a_persons_login_with_only_its_refresh_token_swapped_is_refused(broker):
    old = _codex("acct-1", "user-1", "rt-old")
    _, refused = await _release_after(broker, old, _swap(old, "rt-attacker"), person="alice")
    assert broker.vault.get("cred:codex") == old and len(refused) == 1
    assert "refresh token changed" in refused[0].detail["reason"] and "rt-" not in repr(refused[0].model_dump())
    # a real refresh (new access and id tokens with the new refresh token) is still stored
    new = _codex("acct-1", "user-1", "rt-new")
    _, refused = await _release_after(broker, old, new, person="alice")
    assert broker.vault.get("cred:codex") == new and len(refused) == 1  # only the swap's, from above

def test_swapped():
    from hangar.authident import swapped

    old = b'{"tokens": {"access_token": "a1", "id_token": "i1", "refresh_token": "r1"}}'
    assert swapped(old, b'{"tokens": {"access_token": "a1", "id_token": "i1", "refresh_token": "r2"}}')
    assert not swapped(old, b'{"tokens": {"access_token": "a2", "id_token": "i2", "refresh_token": "r2"}}')
    assert not swapped(old, old)  # unchanged
    assert not swapped(old, b"not json") and not swapped(b'{"refresh": "r1"}', b'{"refresh": "r2"}')  # no access


# --- KAN-178: a login stored anew while a sandbox holds the old one is never overwritten by that sandbox
async def test_a_login_stored_again_while_leased_isnt_overwritten_by_the_old_sandbox(broker):
    prov = FakeProvider()
    sb = Sandbox(name="t", environment="e", provider="fake")
    await prov.create(sb, "img")
    dead, fresh = _codex("acct-1", "user-1", "rt-dead"), _codex("acct-1", "user-1", "rt-fresh")
    broker.put("codex", "codex", CredentialKind.AUTH_FILE, dead, person="alice")
    await broker.inject(sb, prov, broker.acquire(["codex"], sb.id))
    # the person logs in again and stores it while the job's sandbox, kept for follow-ups, still holds the dead file
    broker.put("codex", "codex", CredentialKind.AUTH_FILE, fresh, person="alice")
    assert broker.get("codex").leased_to is None
    [e] = [e for e in broker.store.audit_tail() if e.action == "credential.lease_dropped"]
    assert e.target == "codex" and e.detail["sandbox"] == sb.id and "rt-" not in repr(e.model_dump())
    await broker.release(sb, prov)  # the sandbox ends: its dead file is not written back
    assert broker.vault.get("cred:codex") == fresh
    # the next sandbox gets the new login
    sb2 = Sandbox(name="t2", environment="e", provider="fake")
    await prov.create(sb2, "img")
    await broker.inject(sb2, prov, broker.acquire(["codex"], sb2.id))
    assert await prov.read_file(sb2, "/home/agent/.codex/auth.json") == fresh


async def test_the_same_value_stored_again_keeps_the_lease(broker):
    prov = FakeProvider()
    sb = Sandbox(name="t", environment="e", provider="fake")
    await prov.create(sb, "img")
    old, new = _codex("acct-1", "user-1", "rt-old"), _codex("acct-1", "user-1", "rt-new")
    broker.put("codex", "codex", CredentialKind.AUTH_FILE, old, person="alice")
    broker.acquire(["codex"], sb.id)
    broker.put("codex", "codex", CredentialKind.AUTH_FILE, old, person="bob")  # an owner change, say
    assert broker.get("codex").leased_to == sb.id
    await prov.write_file(sb, "/home/agent/.codex/auth.json", new)  # codex refreshed in the sandbox
    await broker.release(sb, prov)
    assert broker.vault.get("cred:codex") == new  # the rotation is kept, as before
