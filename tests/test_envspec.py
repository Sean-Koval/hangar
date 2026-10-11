import pytest

from hangar.envspec import EnvCatalog, image_tag, render, repo_dirname
from tests.conftest import ENVS


@pytest.fixture
def catalog():
    return EnvCatalog([ENVS])


def test_extends_merges_layers(catalog):
    spec = catalog.get("agent")
    assert spec.harnesses == ["git", "claude-code", "codex"]
    assert spec.tools.apt == ["jq"]
    assert spec.tools.mise == {"python": "3.12"}
    assert spec.agent_config.instructions == "be careful"
    assert spec.repo.url.endswith("proj.git")


def test_render_layer_order_and_content(catalog):
    ctx = render(catalog.get("agent"))
    df = ctx.dockerfile
    order = [df.index(s) for s in ("apt-get install", "mise use -g", "# harness: claude-code",
                                   "# harness: codex", "npm install -g typescript", "COPY --chown")]
    assert order == sorted(order), "layers must be ordered for cache reuse"
    # codex needs node, so it is added to mise automatically
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


def test_noop_harness_is_skipped(catalog):
    assert "# harness: git" not in render(catalog.get("base")).dockerfile


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
