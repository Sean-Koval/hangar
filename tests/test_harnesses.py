import json
import tomllib

import pytest

from hangar.harnesses import (
    HARNESSES,
    McpServer,
    mcp_config,
    merge_codex_toml,
    pack_dockerfile,
    pack_script,
    pack_tag,
    resolve_refs,
)

SERVERS = {
    "gh": McpServer(url="https://api.example/mcp", headers={"Authorization": "Bearer t0k"}),
    "fs": McpServer(command="npx", args=["-y", "fs-server", "."], env={"ROOT": "/workspace"}),
}


def test_supported_agents():
    agents = {n for n, h in HARNESSES.items() if h.agent and not h.deprecated}
    assert agents == {"claude-code", "codex", "pi", "antigravity"}
    for n in agents:
        h = HARNESSES[n]
        assert h.install and h.run and h.skills_dir and h.instructions_file and h.mcp_format
        assert "$HANGAR_PROMPT_FILE" in h.run and "HANGAR_MODEL" in h.run, n


def test_codex_runs_without_its_own_sandbox_or_prompts():
    run = HARNESSES["codex"].run
    assert "codex exec --json" in run and "--dangerously-bypass-approvals-and-sandbox" in run
    assert "--skip-git-repo-check" in run and "--full-auto" not in run


@pytest.mark.parametrize("name", ["claude-code", "codex", "pi", "antigravity"])
def test_pack_build_is_self_contained(name):
    h = HARNESSES[name]
    df = pack_dockerfile(h)
    assert df.splitlines()[0].startswith("FROM mirror.gcr.io/library/node:22-bookworm AS pack-")
    assert f"FROM scratch\nCOPY --from=pack-{name} /opt/hangar/harness/{name}/ /" in df
    script = pack_script(h)
    assert f"PREFIX=/opt/hangar/harness/{name}" in script and h.verify in script
    tag = pack_tag(h, "hangar-env", "us-docker.pkg.dev/p/r")
    assert tag.startswith(f"us-docker.pkg.dev/p/r/harness-{name}:") and tag == pack_tag(h, "x", "us-docker.pkg.dev/p/r")


def test_mcp_json_formats():
    path, c = mcp_config("claude", SERVERS)
    assert path == ".claude.json"
    assert c["mcpServers"]["gh"] == {"type": "http", "url": "https://api.example/mcp",
                                     "headers": {"Authorization": "Bearer t0k"}}
    assert c["mcpServers"]["fs"]["type"] == "stdio" and c["mcpServers"]["fs"]["env"] == {"ROOT": "/workspace"}
    path, c = mcp_config("antigravity", SERVERS)
    assert path == ".gemini/config/mcp_config.json" and c["mcpServers"]["gh"]["serverUrl"].startswith("https://")
    path, c = mcp_config("pi", SERVERS)
    assert path == ".pi/agent/mcp.json" and c["mcpServers"]["gh"]["url"] == "https://api.example/mcp"
    json.dumps(c)


def test_codex_toml_block_is_valid_and_replaced_in_place():
    path, frag = mcp_config("codex", SERVERS)
    assert path == ".codex/config.toml"
    existing = 'model = "gpt-5.5"\n\n[mcp_servers.mine]\nurl = "https://keep.me/mcp"\n'
    once = merge_codex_toml(existing, frag)
    twice = merge_codex_toml(once, frag)
    assert once == twice, "re-applying the session must not duplicate servers"
    doc = tomllib.loads(twice)
    assert doc["model"] == "gpt-5.5" and doc["mcp_servers"]["mine"]["url"] == "https://keep.me/mcp"
    assert doc["mcp_servers"]["gh"]["http_headers"] == {"Authorization": "Bearer t0k"}
    assert doc["mcp_servers"]["fs"]["args"] == ["-y", "fs-server", "."]


def test_resolve_refs_reports_missing():
    missing: set[str] = set()
    out = resolve_refs({"a": "Bearer ${T}", "b": ["${U}"]}, {"T": "x"}, missing)
    assert out == {"a": "Bearer x", "b": ["${U}"]} and missing == {"U"}


def test_agent_output_tail_is_clipped():
    from hangar.fleet import _clip_tail

    out = _clip_tail("\n".join(["x" * 70000, *[f"line {i}" for i in range(5)]]))
    assert len(out) < 3000 and "chars clipped" in out and out.endswith("line 4")
    many = _clip_tail("\n".join("y" * 1500 for _ in range(100)))
    assert len(many) <= 24100 and many.startswith("[")
