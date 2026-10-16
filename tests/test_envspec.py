from pathlib import Path

import pytest

from hangar.envspec import EnvCatalog, image_tag, render, repo_dirname
from tests.conftest import ENVS


@pytest.fixture
def catalog():
    return EnvCatalog([ENVS])


def test_shipped_envs_load_and_render():
    """Test that all environments in the repo load and render correctly."""
    # Load all environments from the repo's envs/ directory
    envs_path = Path(__file__).parent.parent / "envs"
    catalog = EnvCatalog([envs_path])
    
    # Test each environment
    for name in catalog.names():
        spec = catalog.get(name)
        
        # If the spec has no image set, render it and check Dockerfile contains "FROM"
        if spec.image is None:
            ctx = render(spec)
            assert "FROM" in ctx.dockerfile, f"Environment {name} Dockerfile should contain 'FROM'"
        
        # If the environment has agent harnesses, check pick_harness() result
        if spec.agent_harnesses:
            picked = spec.pick_harness()
            assert picked in spec.harnesses, (
                f"Environment {name} pick_harness() result '{picked}' should be in harnesses list"
            )
            assert picked in spec.agent_harnesses, (
                f"Environment {name} pick_harness() result '{picked}' should be in agent_harnesses list"
            )



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


def test_warm_pool_hours():
    import datetime as dt
    from zoneinfo import ZoneInfo

    from hangar.envspec import EnvSpec, parse_hours, warm_pool_now

    assert parse_hours("Mon-Fri 08:00-20:00 America/New_York") == ({0, 1, 2, 3, 4}, 480, 1200, "America/New_York")
    assert parse_hours("sat,sun 22:00-02:00")[0] == {5, 6}
    spec = EnvSpec(name="x", warm_pool=2, warm_pool_hours="Mon-Fri 08:00-20:00 America/New_York")
    ny = ZoneInfo("America/New_York")
    tue_10am = dt.datetime(2026, 10, 13, 10, 0, tzinfo=ny).timestamp()
    tue_9pm = dt.datetime(2026, 10, 13, 21, 0, tzinfo=ny).timestamp()
    sat_10am = dt.datetime(2026, 10, 17, 10, 0, tzinfo=ny).timestamp()
    assert warm_pool_now(spec, tue_10am) == 2
    assert warm_pool_now(spec, tue_9pm) == 0 and warm_pool_now(spec, sat_10am) == 0
    assert warm_pool_now(EnvSpec(name="y", warm_pool=1)) == 1  # no schedule: always
    late = EnvSpec(name="w", warm_pool=1, warm_pool_hours="Fri 22:00-02:00 America/New_York")
    assert warm_pool_now(late, dt.datetime(2026, 10, 16, 23, 0, tzinfo=ny).timestamp()) == 1  # Fri 11pm
    assert warm_pool_now(late, dt.datetime(2026, 10, 17, 1, 0, tzinfo=ny).timestamp()) == 1  # Sat 1am: Friday's
    assert warm_pool_now(late, dt.datetime(2026, 10, 16, 1, 0, tzinfo=ny).timestamp()) == 0  # Fri 1am: Thursday's
    allday = EnvSpec(name="v", warm_pool=1, warm_pool_hours="Tue 00:00-00:00 America/New_York")
    assert warm_pool_now(allday, tue_9pm) == 1 and warm_pool_now(allday, sat_10am) == 0
    import pytest

    with pytest.raises(ValueError):
        EnvSpec(name="z", warm_pool=1, warm_pool_hours="weekdays 9-5")


# --- #132: how `extends` merges
def _cat(tmp_path, **specs):
    import yaml

    for name, data in specs.items():
        (tmp_path / f"{name}.yaml").write_text(yaml.safe_dump({"name": name, **data}))
    for n in ("a", "b", "c"):
        (tmp_path / "skills" / n).mkdir(parents=True, exist_ok=True)
        (tmp_path / "skills" / n / "SKILL.md").write_text(f"Use {n}.")
    return EnvCatalog([tmp_path])


PARENT = {"harnesses": ["claude-code"], "skills": [{"path": "skills/a"}, {"path": "skills/b"}],
          "tools": {"apt": ["jq"], "mise": {"python": "3.12", "node": "22"}},
          "dependencies": {"pip": ["ruff"], "npm": ["pnpm"]}, "env": {"A": "1", "B": "2"},
          "credentials": ["x"], "secrets": {"TOKEN": "tok"}}


def test_a_child_skills_list_replaces_the_parents(tmp_path):
    cat = _cat(tmp_path, parent=PARENT, child={"extends": "parent", "skills": [{"path": "skills/a"}]})
    assert [s.path for s in cat.get("child").skills] == ["skills/a"]


def test_skills_remove_drops_inherited_skills_by_name(tmp_path):
    cat = _cat(tmp_path, parent=PARENT, child={"extends": "parent", "skills_remove": ["b"]},
               grandchild={"extends": "child"})
    assert [s.path for s in cat.get("child").skills] == ["skills/a"]
    assert [s.path for s in cat.get("grandchild").skills] == ["skills/a"]  # removed once; not re-applied
    assert "skills_remove" not in cat.get("child").model_dump()


def test_skills_remove_naming_an_unknown_skill_lists_the_known_ones(tmp_path):
    cat = _cat(tmp_path, parent=PARENT, child={"extends": "parent", "skills_remove": ["nope"]})
    with pytest.raises(ValueError, match=r"skills_remove names nope.*known: a, b"):
        cat.get("child")


def test_build_inputs_union_and_other_lists_replace(tmp_path):
    cat = _cat(tmp_path, parent=PARENT, child={
        "extends": "parent", "tools": {"apt": ["ripgrep"]}, "dependencies": {"pip": ["pytest"], "npm": ["tsx"]},
        "credentials": ["y"]})
    c = cat.get("child")
    assert c.tools.apt == ["jq", "ripgrep"]  # union: installing more never changes the agent's instructions
    assert c.dependencies.pip == ["ruff", "pytest"] and c.dependencies.npm == ["pnpm", "tsx"]
    assert c.credentials == ["y"] and c.harnesses == ["claude-code"]  # replaced; unset: inherited
    assert [s.path for s in c.skills] == ["skills/a", "skills/b"]  # unset: inherited


def test_maps_merge_by_key_and_null_deletes_one(tmp_path):
    cat = _cat(tmp_path, parent=PARENT, child={
        "extends": "parent", "env": {"B": None, "C": "3"}, "tools": {"mise": {"node": None, "go": "1.24"}},
        "secrets": {"TOKEN": None}}, orphan={"env": {"X": None, "Y": "1"}})
    c = cat.get("child")
    assert c.env == {"A": "1", "C": "3"}
    assert c.tools.mise == {"python": "3.12", "go": "1.24"} and c.secrets == {}
    assert cat.get("orphan").env == {"Y": "1"}


def test_shipped_envs_resolve_the_same_under_the_new_rules():
    """The migration dry-run: no shipped environment's effective skill set (or anything else) changes."""
    from hangar.envspec import merge_changes

    assert merge_changes(EnvCatalog([Path(__file__).parent.parent / "envs"])) == {}


def test_the_migration_dry_run_reports_lost_skills_and_lists_that_differ(tmp_path):
    from hangar.envspec import merge_changes

    cat = _cat(tmp_path, parent={**PARENT, "ports": [{"port": 3000}]}, child={
        "extends": "parent", "skills": [{"path": "skills/c"}], "env": {"C": "3"}, "agent_config": {"files": {}}})
    (tmp_path / "saved").mkdir()
    changes = merge_changes(cat)
    assert set(changes) == {"child"}
    assert "skills lost: a, b" in changes["child"]
    assert not any(line.startswith("env") for line in changes["child"])  # maps merged before too


def test_the_migration_dry_run_reads_saved_environments(tmp_path):
    from hangar.envspec import merge_changes

    shipped, saved = tmp_path / "shipped", tmp_path / "saved"
    shipped.mkdir()
    _cat(shipped, parent=PARENT)
    cat = EnvCatalog([shipped], save_dir=saved)
    cat.add({"name": "mine", "extends": "parent", "skills": [{"name": "b", "path": "skills/b"}]})
    assert merge_changes(EnvCatalog([shipped], save_dir=saved)) == {"mine": ["skills lost: a"]}
