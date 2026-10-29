"""Claude Code plugins from a git marketplace, pinned by commit (#222, KAN-76; plugins.py, docs/harnesses.md#plugins).

A spec names `{git, ref, path?, name, marketplace?}`. The control plane fetches the commit before the image build,
from the git gateway's upstream for its host with the gateway's credential, and the commit's tree is then a local
marketplace like any other. A branch or a tag is refused, naming the commit to pin. Every repo here is a local bare
repo: no test touches the network.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import jsonschema
import pytest

from hangar import harness_repo, manifest, plugins
from hangar.envspec import EnvSpec, render
from hangar.fleet import FleetError

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = json.loads((ROOT / "docs/api/manifest.schema.json").read_text())
GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false",
       "-c", "tag.gpgsign=false", "-c", "init.defaultBranch=main"]


def _git(*args: str, cwd: Path | None = None) -> str:
    return subprocess.run([*GIT, *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def _write_mkt(root: Path, body: str) -> None:
    pdir = root / "market" / "workflow"
    (pdir / ".claude-plugin").mkdir(parents=True, exist_ok=True)
    (pdir / ".claude-plugin" / "plugin.json").write_text(json.dumps({"name": "workflow", "version": "2.0.0"}))
    (pdir / "skills" / "plan").mkdir(parents=True, exist_ok=True)
    (pdir / "skills" / "plan" / "SKILL.md").write_text(f"---\nname: plan\ndescription: x\n---\n{body}\n")
    (root / "market" / ".claude-plugin").mkdir(parents=True, exist_ok=True)
    (root / "market" / ".claude-plugin" / "marketplace.json").write_text(json.dumps({
        "name": "team", "owner": {"name": "x"}, "plugins": [{"name": "workflow", "source": "./workflow"}]}))


@pytest.fixture
def repo(tmp_path):
    """A bare repo at <tmp>/upstream/acme/harness.git: commit `one` (tagged v1), then `two` on main."""
    work = tmp_path / "work"
    work.mkdir()
    _git("init", "-q", cwd=work)
    _write_mkt(work, "Plan first.")
    (work / "README.md").write_text("team harness\n")
    _git("add", "-A", cwd=work)
    _git("commit", "-qm", "one", cwd=work)
    one = _git("rev-parse", "HEAD", cwd=work)
    _git("tag", "-a", "v1", "-m", "v1", cwd=work)
    _write_mkt(work, "Plan twice.")
    _git("commit", "-qam", "two", cwd=work)
    two = _git("rev-parse", "HEAD", cwd=work)
    bare = tmp_path / "upstream" / "acme" / "harness.git"
    bare.parent.mkdir(parents=True)
    _git("clone", "-q", "--bare", str(work), str(bare))
    plugins.configure_git(tmp_path / "plugin-git")
    yield {"bare": bare, "url": f"file://{bare}", "one": one, "two": two, "upstream": tmp_path / "upstream"}
    plugins.configure_git(None)


def _spec(git: str, ref: str, **plugin) -> EnvSpec:
    return EnvSpec.model_validate({"name": "e", "harnesses": ["claude-code"], "plugins": [
        {"git": git, "ref": ref, "path": "market", "name": "workflow", **plugin}]})


# ====================================================================== the declaration
def test_a_git_plugin_names_a_url_without_credentials_and_a_ref():
    ok = EnvSpec.model_validate({"name": "e", "harnesses": ["claude-code"], "plugins": [
        {"git": "https://github.com/acme/harness", "ref": "a" * 40, "name": "workflow"}]})
    assert ok.plugins[0].path is None  # the marketplace is the repo's root
    for bad, why in [({"git": "https://github.com/acme/harness", "name": "w"}, "needs ref"),
                     ({"git": "https://x-access-token:ghp_1@github.com/acme/h", "ref": "a" * 40, "name": "w"},
                      "no credential"),
                     ({"git": "/srv/harness", "ref": "a" * 40, "name": "w"}, "file:///"),
                     ({"path": "m", "ref": "a" * 40, "name": "w"}, "ref goes with git"),
                     ({"name": "w"}, "give path")]:
        with pytest.raises(ValueError, match=why):
            EnvSpec.model_validate({"name": "e", "harnesses": ["claude-code"], "plugins": [bad]})


# ====================================================================== the image build
def test_a_spec_at_a_commit_builds_a_seed_with_that_commits_plugin(repo):
    spec = _spec(repo["url"], repo["one"])
    ctx = render(spec)
    assert f"COPY plugins/team/ {plugins.SEED}/marketplaces/team/" in ctx.dockerfile
    assert ctx.files["plugins/team/workflow/skills/plan/SKILL.md"].endswith(b"Plan first.\n")  # commit one's
    assert not any(k.startswith("plugins/team/README") for k in ctx.files)  # only the marketplace directory
    assert "workflow@team" in ctx.files["plugins/seed.sh"].decode()
    assert "git clone" not in ctx.dockerfile and "git fetch" not in ctx.dockerfile  # the build never runs git
    # the other commit is another tree, so another image
    later = render(_spec(repo["url"], repo["two"]))
    assert later.files["plugins/team/workflow/skills/plan/SKILL.md"].endswith(b"Plan twice.\n")
    assert later.digest != ctx.digest


def test_the_manifest_records_the_commit_and_the_digest(fleet, repo):
    spec = _spec(repo["url"], repo["one"].upper())  # a commit id in either case is the same pin
    m = manifest.build(fleet, spec, "claude-code")
    jsonschema.validate(m, SCHEMA)
    [p] = m["plugins"]
    assert (p["name"], p["marketplace"], p["version"]) == ("workflow", "team", "2.0.0")
    assert p["git"] == repo["url"] and p["commit"] == repo["one"]
    assert p["digest"].startswith("sha256:") and p["files"] == 2
    two = manifest.build(fleet, _spec(repo["url"], repo["two"]), "claude-code")
    assert two["plugins"][0]["commit"] == repo["two"]
    assert two["plugins"][0]["digest"] != p["digest"] and two["harness_digest"] != m["harness_digest"]


# ====================================================================== floating refs
def test_a_branch_or_a_tag_is_refused_naming_the_commit_to_pin(repo):
    with pytest.raises(ValueError, match=f"`main` .* is a branch.*ref: {repo['two']}"):
        render(_spec(repo["url"], "main"))
    with pytest.raises(ValueError, match=f"`v1` .* is a tag.*ref: {repo['one']}"):
        render(_spec(repo["url"], "v1"))
    with pytest.raises(ValueError, match="abbreviated commit"):
        render(_spec(repo["url"], repo["one"][:12]))
    with pytest.raises(ValueError, match="has no commit"):
        render(_spec(repo["url"], "f" * 40))
    assert not (plugins.git_cache() / "trees").exists() or not any((plugins.git_cache() / "trees").rglob("SKILL.md"))


# ====================================================================== through the git gateway
async def test_the_fetch_goes_through_the_gateways_upstream_with_its_credential(fleet, repo, monkeypatch):
    fleet.settings.gateway_upstreams = {"git.example.com": f"file://{repo['upstream']}"}
    calls: list[tuple[str, dict[str, str]]] = []

    async def auth_env(f, url):
        assert f is fleet
        return {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "hangar.test", "GIT_CONFIG_VALUE_0": "control-plane"}

    real = plugins._gitrun

    def gitrun(args, env=None):
        calls.append((" ".join(args), dict(env or {})))
        return real(args, env)

    monkeypatch.setattr(harness_repo, "_auth_env", auth_env)
    monkeypatch.setattr(plugins, "_gitrun", gitrun)
    spec = _spec("https://git.example.com/acme/harness", repo["one"])
    assert plugins.pending(spec) == ["workflow"]
    with pytest.raises(ValueError, match="isn't fetched yet"):
        render(spec)  # a remote marketplace is read only through prefetch
    await plugins.prefetch(fleet, spec)
    fetches = [(a, e) for a, e in calls if " fetch " in f" {a} "]
    assert fetches and all(f"file://{repo['bare']}" in a for a, _ in fetches)  # the upstream the gateway forwards to
    assert all(e.get("GIT_CONFIG_VALUE_0") == "control-plane" for _, e in fetches)  # the gateway's credential
    assert not any("hgs_" in a or "hgs_" in json.dumps(e) for a, e in calls)  # never a sandbox's grant
    assert plugins.pending(spec) == []
    ctx = render(spec)
    assert ctx.files["plugins/team/workflow/skills/plan/SKILL.md"].endswith(b"Plan first.\n")

    calls.clear()
    with pytest.raises(ValueError, match="Building the image names the commit"):
        render(_spec("https://git.example.com/acme/harness", "main"))  # no lookup outside the gateway
    assert calls == []
    with pytest.raises(ValueError, match="git gateway's upstreams"):
        await plugins.prefetch(fleet, _spec("https://elsewhere.example/acme/harness", repo["one"]))
    fleet.settings.gateway_upstreams = {"git.example.com": f"file://{repo['upstream']}"}
    with pytest.raises(ValueError, match=f"is a branch.*ref: {repo['two']}"):
        await plugins.prefetch(fleet, _spec("https://git.example.com/acme/harness", "main"))


async def test_a_build_fetches_first_and_validation_waits_for_it(fleet, repo, monkeypatch, tmp_path):
    monkeypatch.setattr(fleet.catalog, "save_dir", tmp_path / "envs")  # not the shared fixtures' directory
    fleet.settings.gateway_upstreams = {"git.example.com": f"file://{repo['upstream']}"}

    async def auth_env(f, url):
        return {}

    monkeypatch.setattr(harness_repo, "_auth_env", auth_env)
    data = {"name": "gitplug", "harnesses": ["claude-code"], "plugins": [
        {"git": "https://git.example.com/acme/harness", "ref": repo["one"], "path": "market", "name": "workflow"}]}
    v = fleet.validate_environment(data)
    assert v["valid"] and v["dockerfile"] is None and any("git gateway" in w for w in v["warnings"])
    fleet.save_environment(data)
    built: list = []
    prov = fleet.providers["docker"]

    async def build_template(spec, ctx, tag, log=None):
        built.append(ctx)
        prov.images.add(tag)
        return tag

    monkeypatch.setattr(prov, "build_template", build_template)
    job = await fleet.build("gitplug", "docker")
    assert job.state == "succeeded", job.error
    [ctx] = [c for c in built if "plugins/seed.sh" in c.files]
    assert ctx.files["plugins/team/workflow/skills/plan/SKILL.md"].endswith(b"Plan first.\n")
    assert fleet.validate_environment(data)["dockerfile"]  # fetched now: the Dockerfile shows

    fleet.save_environment({**data, "plugins": [{**data["plugins"][0], "ref": "main"}]})
    with pytest.raises(FleetError, match=f"is a branch.*ref: {repo['two']}"):
        await fleet._prefetch_plugins(fleet.catalog.get("gitplug"))
