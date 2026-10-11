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
    broker.put("codex", "codex", CredentialKind.AUTH_FILE, '{"refresh":"r1"}')
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
