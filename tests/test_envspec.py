import pytest

from hangar.envspec import EnvCatalog, image_tag, render, repo_dirname
from tests.conftest import ENVS


@pytest.fixture
def catalog():
    return EnvCatalog([ENVS])


def test_extends_merges_layers(catalog):
    spec = catalog.get("agent")
    assert spec.harnesses == ["claude-code", "codex"], "a child's harness list replaces the parent's"
    assert spec.tools.apt == ["jq"]
    assert spec.tools.mise == {"python": "3.12"}
    assert spec.agent_config.instructions == "be careful"
    assert spec.repo.url.endswith("proj.git")


def expanded(ctx):
    """Dockerfile with each shipped step script inlined where it runs."""
    import re

    return re.sub(r"COPY (steps/\d+\.sh) \S+", lambda m: ctx.files[m.group(1)].decode(), ctx.dockerfile)


def test_dockerfile_is_portable_across_builders(catalog):
    ctx = render(catalog.get("agent"))
    assert "<<" not in ctx.dockerfile, "RUN heredocs need BuildKit; steps must ship as files"
    assert "# syntax=" not in ctx.dockerfile
    steps = [k for k in ctx.files if k.startswith("steps/")]
    assert steps and all(ctx.files[k].startswith(b"#!/bin/bash\nset -euxo pipefail") for k in steps)


def test_render_layer_order_and_content(catalog):
    ctx = render(catalog.get("agent"), bake=["codex"])
    df = expanded(ctx)
    order = [df.index(s) for s in ("FROM mirror.gcr.io/library/node:22-bookworm AS pack-codex", "apt-get install",
                                   "mise use -g", "# harness: codex", "npm install -g typescript", "COPY --chown")]
    assert order == sorted(order), "layers must be ordered for cache reuse"
    assert "COPY --from=pack-codex /opt/hangar/harness/codex /opt/hangar/harness/codex" in df
    assert "# harness: claude-code" not in df, "only the requested harness is baked"
    assert "@openai/codex@$VERSION" in ctx.files["packs/codex.sh"].decode()
    # npm dependencies pull in node; harnesses never do (packs bundle what they need)
    assert "node@22" in df and "python@3.12" in df
    # skill lands in every harness that has a skills dir
    assert "/home/agent/.claude/skills/demo/SKILL.md" in df
    assert "/home/agent/.codex/skills/demo/SKILL.md" in df
    # global instructions go to each harness's instructions file
    assert "/home/agent/.claude/CLAUDE.md" in df and "/home/agent/.codex/AGENTS.md" in df
    assert "/home/agent/.config/demo.txt" in df
    assert "ENV FOO=bar" in df
    assert "chown -R agent:agent" in df
    # repo is cloned at claim time, not baked
    assert "git clone" not in df
    assert any(k.startswith("skills/") for k in ctx.files)


def test_images_carry_no_harness_by_default(catalog):
    df = expanded(render(catalog.get("agent")))
    assert "# harness:" not in df and "pack-" not in df and "/opt/hangar" not in df
    assert render(catalog.get("agent")).digest != render(catalog.get("agent"), bake=["claude-code"]).digest


def test_cannot_bake_a_harness_the_env_does_not_allow(catalog):
    with pytest.raises(ValueError, match="not enabled"):
        render(catalog.get("agent"), bake=["pi"])


def test_harness_selection(catalog, tmp_path):
    spec = catalog.get("agent")
    assert spec.agent_harnesses == ["claude-code", "codex"]
    assert spec.pick_harness() == "claude-code"
    assert spec.pick_harness("codex") == "codex"
    with pytest.raises(ValueError, match="not enabled"):
        spec.pick_harness("pi")
    (tmp_path / "x.yaml").write_text("name: x\nharnesses: [codex]\ndefault_harness: pi\n")
    with pytest.raises(ValueError, match="default_harness"):
        EnvCatalog([tmp_path]).get("x")


def test_session_fields_validate(tmp_path):
    (tmp_path / "m.yaml").write_text("name: m\nmcp: {gh: {url: https://x/mcp, command: npx}}\n")
    (tmp_path / "s.yaml").write_text("name: s\nsecrets: {'bad-var': cred}\n")
    (tmp_path / "ok.yaml").write_text(
        "name: ok\nharnesses: [pi]\nmodel: openrouter/qwen/qwen3-coder\n"
        "mcp: {gh: {url: https://x/mcp, headers: {Authorization: 'Bearer ${GH_TOKEN}'}}}\n"
        "secrets: {OPENROUTER_API_KEY: openrouter}\n")
    cat = EnvCatalog([tmp_path])
    with pytest.raises(ValueError, match="exactly one"):
        cat.get("m")
    with pytest.raises(ValueError, match="env var name"):
        cat.get("s")
    ok = cat.get("ok")
    assert ok.mcp["gh"].headers["Authorization"] == "Bearer ${GH_TOKEN}" and ok.secrets == {
        "OPENROUTER_API_KEY": "openrouter"}


def test_extra_ca_is_trusted(catalog):
    ctx = render(catalog.get("plain"), extra_ca=b"-----BEGIN CERTIFICATE-----\nX\n")
    assert "update-ca-certificates" in ctx.dockerfile
    assert "NODE_EXTRA_CA_CERTS" in ctx.dockerfile
    assert ctx.files["certs/hangar-extra-ca.crt"].startswith(b"-----BEGIN")


def test_digest_is_stable_and_content_addressed(catalog):
    a = render(catalog.get("agent"))
    b = render(catalog.get("agent"))
    assert a.digest == b.digest
    spec = catalog.get("agent")
    spec.env["FOO"] = "baz"
    assert render(spec).digest != a.digest


def test_tags(catalog):
    spec = catalog.get("plain")
    ctx = render(spec)
    assert image_tag(spec, ctx, "hangar-env") == f"hangar-env/plain:{ctx.digest}"
    assert image_tag(spec, ctx, "x", "us-docker.pkg.dev/p/r") == f"us-docker.pkg.dev/p/r/plain:{ctx.digest}"
    assert repo_dirname("git@github.com:org/thing.git") == "thing"


def test_errors(tmp_path):
    (tmp_path / "a.yaml").write_text("name: a\nextends: b\n")
    (tmp_path / "b.yaml").write_text("name: b\nextends: a\n")
    (tmp_path / "c.yaml").write_text("name: c\nharnesses: [nope]\n")
    cat = EnvCatalog([tmp_path])
    with pytest.raises(ValueError, match="circular"):
        cat.get("a")
    with pytest.raises(ValueError, match="unknown harness"):
        cat.get("c")
    with pytest.raises(KeyError):
        cat.get("missing")


def test_add_persists(tmp_path):
    cat = EnvCatalog([tmp_path])
    cat.add({"name": "new", "description": "x"})
    assert (tmp_path / "new.yaml").exists()
    assert EnvCatalog([tmp_path]).get("new").description == "x"


def test_skill_paths_cannot_escape_spec_dir(tmp_path):
    secret = tmp_path / "data"
    secret.mkdir()
    (secret / "secret.key").write_text("k")
    envs = tmp_path / "envs"
    envs.mkdir()
    (envs / "abs.yaml").write_text(f"name: abs\nskills: [{{path: {secret}}}]\n")
    (envs / "rel.yaml").write_text("name: rel\nskills: [{path: ../data}]\n")
    cat = EnvCatalog([envs])
    for name in ("abs", "rel"):
        with pytest.raises(ValueError, match="relative|escapes"):
            render(cat.get(name))


def test_invalid_env_name_rejected(tmp_path):
    with pytest.raises(ValueError, match="invalid environment name"):
        EnvCatalog([tmp_path]).add({"name": "../evil"})
