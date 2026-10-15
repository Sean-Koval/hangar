"""Harness selection at claim time: credentials, config, MCP servers, models and packs for one harness."""

import asyncio
import json
import tomllib

import pytest

from hangar.fleet import FleetError
from hangar.models import CredentialKind


@pytest.fixture
def creds(broker):
    broker.put("claude", "claude-code", CredentialKind.API_KEY, "sk-claude")
    broker.put("oai", "codex", CredentialKind.API_KEY, "sk-codex")
    broker.put("orkey", "pi", CredentialKind.API_KEY, "sk-or")
    broker.put("ghtok", "generic-secret", CredentialKind.GENERIC, "ghp_123", env_var="UNUSED_GH")
    return broker


async def _env(fleet, sb) -> str:
    return (await fleet.read_file(sb.id, "/home/agent/.hangar/env")).decode()


async def _json(fleet, sb, path):
    return json.loads(await fleet.read_file(sb.id, path))


async def test_default_harness_gets_only_its_credentials(fleet, creds):
    sb = await fleet.create("agents")
    assert sb.info["harness"] == "claude-code"
    env = await _env(fleet, sb)
    assert "sk-claude" in env and "sk-codex" not in env and "sk-or" not in env
    assert sb.credentials == ["claude"]
    assert 'export PATH="/opt/hangar/harness/claude-code/bin:$PATH"' in env
    assert "DISABLE_AUTOUPDATER=1" in env and "HANGAR_HARNESS=claude-code" in env
    assert "GH_TOKEN=ghp_123" in env, "`secrets:` maps a credential to an env var"


async def test_claude_config_and_mcp(fleet, creds):
    sb = await fleet.create("agents")
    cfg = await _json(fleet, sb, "/home/agent/.claude.json")
    assert cfg["hasCompletedOnboarding"] is True
    assert cfg["mcpServers"]["github"] == {"type": "http", "url": "https://api.example/mcp",
                                           "headers": {"Authorization": "Bearer ghp_123"}}
    assert "only-pi" not in cfg["mcpServers"], "harness-scoped servers go only to that harness"
    assert "private" not in cfg["mcpServers"]
    assert any("'private' skipped: needs NOPE" in w for w in sb.info["warnings"])
    assert sb.info["mcp"] == ["github", "hangar-env"]  # --- computer: hangar's own doctor, in every harness
    assert cfg["mcpServers"]["hangar-env"] == {"type": "stdio", "command": "/home/agent/.hangar/bin/hangar-env",
                                               "args": ["mcp"]}


async def test_pi_sandbox(fleet, creds):
    sb = await fleet.create("agents", harness="pi")
    env = await _env(fleet, sb)
    assert "OPENROUTER_API_KEY=sk-or" in env and "sk-claude" not in env
    mcp = await _json(fleet, sb, "/home/agent/.pi/agent/mcp.json")
    assert set(mcp["mcpServers"]) == {"github", "only-pi", "hangar-env"}  # --- computer


async def test_codex_mcp_toml_preserves_user_config(fleet, creds):
    sb = await fleet.create("agents", harness="codex")
    doc = tomllib.loads((await fleet.read_file(sb.id, "/home/agent/.codex/config.toml")).decode())
    assert doc["mcp_servers"]["github"]["http_headers"]["Authorization"] == "Bearer ghp_123"


async def test_harness_must_be_enabled(fleet, creds):
    with pytest.raises(FleetError, match="not enabled"):
        await fleet.create("agents", harness="antigravity")
    with pytest.raises(FleetError, match="unknown harness"):
        await fleet.create("agents", harness="nope")


async def test_explicit_credentials_are_honoured(fleet, creds):
    sb = await fleet.create("agents", harness="pi", credentials=["orkey", "claude"])
    assert sb.credentials == ["orkey", "claude"]


async def test_run_agent_model_and_harness(fleet, creds):
    sb = await fleet.create("agents", harness="pi")
    with pytest.raises(FleetError, match="was created for pi"):
        await fleet.run_agent(sb.id, "hi", harness="codex")
    for model, expect in ((None, "sonnet"), ("openrouter/qwen/qwen3-coder", "openrouter/qwen/qwen3-coder")):
        run = await fleet.run_agent(sb.id, "hi", model=model)
        assert run["harness"] == "pi" and run["model"] == expect
        script = (await fleet.read_file(sb.id, f"/home/agent/.hangar/runs/{run['run_id']}.sh")).decode()
        assert f"export HANGAR_MODEL={expect}" in script and "pi -p --mode json" in script
        await fleet.terminal_kill(sb.id, run["run_id"])


async def test_packs_are_mounted_not_baked(fleet, creds):
    fake = fleet.providers["docker"]
    fake.image_volumes = True
    first = await fleet.create("agents")
    assert "claude-code" in first.info["packs"]
    await asyncio.gather(*fleet._pack_builds.values())  # the others may still be building in the background
    a = await fleet.create("agents")
    b = await fleet.create("agents", harness="codex")
    assert set(a.info["packs"]) == {"claude-code", "codex", "pi"}, "every allowed harness is mounted read-only"
    assert fake.packs_mounted[-1] == a.info["packs"] == b.info["packs"]
    assert a.image == b.image, "one harness-free image serves every harness"
    pack_images = {t for t in fake.images if "/harness-" in t}
    assert len(pack_images) == 3


def stale_codex_pack(fleet, gate: asyncio.Event) -> None:
    """An upgrade changed the codex pack: its new tag isn't built yet, and building it takes minutes."""
    fake = fleet.providers["docker"]
    fleet.store.kv_set("pack:docker:codex", "an-older-codex-pack")
    fake.images = {t for t in fake.images if "/harness-codex" not in t}
    real = fake.build_template

    async def build(spec, ctx, tag, log=None):
        if "/harness-codex" in tag:
            await gate.wait()
        return await real(spec, ctx, tag, log)

    fake.build_template = build


async def test_a_stale_pack_for_another_harness_does_not_hold_up_a_create(fleet, creds):
    fleet.providers["docker"].image_volumes = True
    await fleet.create("agents")  # the environment and its packs are built
    gate = asyncio.Event()
    stale_codex_pack(fleet, gate)
    a = await asyncio.wait_for(fleet.create("agents"), 10)
    assert "codex" not in a.info["packs"] and "claude-code" in a.info["packs"]
    gate.set()
    await asyncio.gather(*fleet._pack_builds.values())
    b = await fleet.create("agents")
    assert "codex" in b.info["packs"]


async def test_a_create_still_waits_on_its_own_harness_pack(fleet, creds):
    fleet.providers["docker"].image_volumes = True
    await fleet.create("agents")
    gate = asyncio.Event()
    stale_codex_pack(fleet, gate)
    pending = asyncio.create_task(fleet.create("agents", harness="codex"))
    await asyncio.sleep(0.3)
    assert not pending.done(), "a codex sandbox needs the codex pack"
    gate.set()
    sb = await asyncio.wait_for(pending, 10)
    assert "codex" in sb.info["packs"]


async def test_without_image_volumes_only_the_selected_harness_is_baked(fleet, creds):
    fake = fleet.providers["docker"]
    a = await fleet.create("agents")
    b = await fleet.create("agents", harness="pi")
    c = await fleet.create("agents", harness="pi")
    assert a.image != b.image and b.image == c.image
    assert "packs" not in a.info and fake.packs_mounted[-1] == {}
    assert not any("/harness-" in t for t in fake.images)


async def test_fork_keeps_the_parents_harness(fleet, creds):
    parent = await fleet.create("agents", harness="pi")
    (fork,) = await fleet.fork(parent.id, count=1)
    assert fork.info["harness"] == "pi"
    assert "OPENROUTER_API_KEY=sk-or" in await _env(fleet, fork)


async def test_validate_and_clone(fleet, creds, tmp_path):
    fleet.catalog.dirs = [tmp_path / "saved", *fleet.catalog.dirs]  # clones persist here, not into fixtures
    ok = fleet.validate_environment({"name": "t", "extends": "agents", "harnesses": ["pi"],
                                     "credentials": ["missing-cred"]})
    assert ok["valid"] and ok["default_harness"] == "pi"
    assert any("missing-cred" in w for w in ok["warnings"])
    assert "t" not in fleet.catalog.names(), "validation must not register the env"
    bad = fleet.validate_environment({"name": "t", "harnesses": ["nope"]})
    assert not bad["valid"] and "unknown harness" in bad["errors"][0]
    spec = fleet.clone_environment("agents", "agents-pi", {"default_harness": "pi"})
    assert spec.pick_harness() == "pi" and spec.mcp.keys() == {"github", "private", "only-pi"}
    with pytest.raises(FleetError, match="already exists"):
        fleet.clone_environment("agents", "agents-pi")


async def test_model_map_per_harness(fleet, creds, tmp_path):
    fleet.catalog.dirs = [tmp_path / "saved", *fleet.catalog.dirs]
    fleet.clone_environment("agents", "agents-models", {"model": {"pi": "openrouter/google/gemini-3.8-flash"}})
    sb = await fleet.create("agents-models", harness="pi")
    run = await fleet.run_agent(sb.id, "hi")
    assert run["model"] == "openrouter/google/gemini-3.8-flash"
    await fleet.terminal_kill(sb.id, run["run_id"])
    other = await fleet.create("agents-models", harness="codex", credentials=[])
    run = await fleet.run_agent(other.id, "hi")
    assert run["model"] is None, "no entry for codex: the harness's own default"
    await fleet.terminal_kill(other.id, run["run_id"])



def gate_pack(fleet, harness: str, gate: asyncio.Event) -> None:
    fake = fleet.providers["docker"]
    real = fake.build_template

    async def build(spec, ctx, tag, log=None):
        if f"/harness-{harness}" in tag:
            await gate.wait()
        return await real(spec, ctx, tag, log)

    fake.build_template = build


async def test_first_use_builds_the_image_without_waiting_on_other_harnesses_packs(fleet, creds):
    """The half of tonight's incident #75 missed: an environment whose image needs building (first use, an edit,
    an upgrade that changes the render) used to build every harness's pack, one by one, before the image."""
    fleet.providers["docker"].image_volumes = True
    gate = asyncio.Event()
    gate_pack(fleet, "codex", gate)
    sb = await asyncio.wait_for(fleet.create("agents"), 10)
    assert sb.state.value == "running" and "codex" not in sb.info["packs"]
    gate.set()
    await asyncio.gather(*fleet._pack_builds.values())
    assert "codex" in (await fleet.create("agents")).info["packs"]


async def test_lazy_pack_resolution_never_waits(fleet, creds):
    """Scheduled warm-pool syncs (run by the reaper) resolve packs lazily: a stale pack must not stall TTL expiry."""
    fleet.providers["docker"].image_volumes = True
    gate = asyncio.Event()
    gate_pack(fleet, "codex", gate)
    spec = fleet.catalog.get("agents")
    await asyncio.wait_for(fleet._resolve_packs(spec, "docker", "scheduler", lazy=True), 5)
    gate.set()
    await asyncio.gather(*fleet._pack_builds.values())


async def test_a_create_waiting_on_its_own_pack_says_so(fleet, creds):
    fleet.providers["docker"].image_volumes = True
    await fleet.create("agents")
    gate = asyncio.Event()
    stale_codex_pack(fleet, gate)
    pending = asyncio.create_task(fleet.create("agents", harness="codex"))
    for _ in range(100):
        starting = [s for s in fleet.list(include_stopped=True) if s.info.get("harness") == "codex"]
        if starting and "harness pack" in (starting[0].info.get("stage") or ""):
            break
        await asyncio.sleep(0.05)
    assert "building the codex harness pack" in starting[0].info["stage"]
    gate.set()
    sb = await asyncio.wait_for(pending, 10)
    assert "codex" in sb.info["packs"]


async def test_startup_rebuilds_packs_whose_harness_changed(fleet, creds):
    fleet.providers["docker"].image_volumes = True
    await fleet.create("agents")
    await asyncio.gather(*fleet._pack_builds.values())
    assert fleet.refresh_stale_packs() == [], "nothing changed"
    gate = asyncio.Event()
    stale_codex_pack(fleet, gate)
    assert fleet.refresh_stale_packs() == ["docker:codex"]
    gate.set()
    await asyncio.gather(*fleet._pack_builds.values())
    assert fleet.store.kv_get("pack:docker:codex") != "an-older-codex-pack"
