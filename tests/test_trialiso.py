"""Trial isolation (KAN-234; docs/security/graders.md#what-a-trial-can-reach): an experiment trial can't see the answer.

A task mined from a merged PR has its fix in the task repo's later refs and on GitHub. A trial's clone stops at its
pinned base (no remote, no ref, no object past it), its git grant closes once it is cloned, and an eval job whose
environment sets no egress gets the closed allowlist: the model API and the package mirror, no GitHub host."""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import jsonschema
import pytest
import yaml
from starlette.testclient import TestClient

from hangar import egress, egress_proxy, experiments, trialiso
from hangar.envspec import EnvCatalog, EnvSpec
from hangar.fleet import Fleet
from hangar.harnesses import HARNESSES
from hangar.jobs import Caller
from hangar.models import JobState, Sandbox, SandboxState
from tests.conftest import ENVS, FakeProvider, free_port
from tests.test_egress import _echo, ask, auth, client_hello
from tests.test_interfaces import DONE, GIT, INDEX_SCHEMA, SCHEMA, jobs, world  # noqa: F401 - fixtures
from tests.test_jobs import _until

needs_git = pytest.mark.skipif(not shutil.which("git"), reason="needs git")


def _git(*args: str, cwd: Path | None = None) -> str:
    return subprocess.run([*GIT, *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def _later(seed: Path, remote: Path) -> dict[str, str]:
    """The answer, past the base: a fix on main, a branch with another commit, and tags on both."""
    base = _git("rev-parse", "HEAD", cwd=seed)
    (seed / "fix.py").write_text("FIXED = True\n")
    _git("add", "fix.py", cwd=seed)
    _git("commit", "-qm", "the merged PR's fix", cwd=seed)
    fix = _git("rev-parse", "HEAD", cwd=seed)
    _git("tag", "v2", cwd=seed)
    _git("checkout", "-qb", "pr-branch", base, cwd=seed)
    (seed / "other.py").write_text("x = 1\n")
    _git("add", "other.py", cwd=seed)
    _git("commit", "-qm", "another branch", cwd=seed)
    branch = _git("rev-parse", "HEAD", cwd=seed)
    _git("tag", "-a", "-m", "annotated", "v2-rc", cwd=seed)
    _git("push", "-q", str(remote), "main", "pr-branch", "--tags", cwd=seed)
    return {"base": base, "fix": fix, "branch": branch}


# ====================================================================== the clone
@needs_git
def test_the_script_leaves_nothing_after_the_base(tmp_path):
    remote, seed = tmp_path / "remote.git", tmp_path / "seed"
    _git("init", "-q", "--bare", str(remote))
    _git("init", "-q", str(seed))
    (seed / "README.md").write_text("hello\n")
    _git("add", ".", cwd=seed)
    _git("commit", "-qm", "base", cwd=seed)
    _git("push", "-q", str(remote), "HEAD:refs/heads/main", cwd=seed)
    shas = _later(seed, remote)
    # a reference clone borrows the objects of another repository holding the fix: they must not stay reachable
    ref = tmp_path / "cache.git"
    _git("clone", "-q", "--mirror", str(remote), str(ref))
    work = tmp_path / "work"
    _git("clone", "-q", "--reference", str(ref), str(remote), str(work))
    _git("checkout", "-qb", "hangar/job-1", cwd=work)  # the clone is on main's tip, with the fix
    out = subprocess.run(["bash", "-c", trialiso.script("hangar/job-1", shas["base"])], cwd=work,
                         capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert out.stdout.split("HANGAR-REFS", 1)[1].split() == ["refs/heads/hangar/job-1"]
    assert _git("log", "--all", "--format=%H", cwd=work).split() == [shas["base"]]
    assert _git("branch", "-a", cwd=work) == "* hangar/job-1"
    assert _git("tag", cwd=work) == "" and _git("remote", cwd=work) == ""
    assert "remote." not in _git("config", "--list", "--local", cwd=work)  # no fetch refspec to pull them later
    assert not (work / ".git/objects/info/alternates").exists()
    for sha in (shas["fix"], shas["branch"]):
        assert subprocess.run([*GIT, "cat-file", "-e", sha], cwd=work).returncode != 0, sha
    assert not (work / "fix.py").exists()
    # nothing to fetch from: a later `git fetch` has no remote to ask
    assert subprocess.run([*GIT, "fetch", "origin"], cwd=work, capture_output=True).returncode != 0


async def test_a_trials_clone_shows_nothing_after_its_base(jobs, world, fleet, monkeypatch):  # noqa: F811
    """In the trial's own sandbox, as its agent runs: `git log --all`, `git branch -a` and `git tag` stop at the
    pinned base, though the task repo's main has the fix on top, another branch and tags."""
    root = Path(world["root"])
    shas = _later(root / "seed", Path(world["remote"]))
    agent = root / "look.sh"
    agent.write_text(r"""#!/usr/bin/env bash
cd "$(git rev-parse --show-toplevel)"
cat "$STREAM.before"
{ git log --all --format=%H; echo ---; git branch -a; echo ---; git tag; echo ---; git remote; } > "$HOME/gitstate.txt"
cat "$STREAM.after"
""")
    monkeypatch.setitem(HARNESSES, "tagent", HARNESSES["tagent"].model_copy(
        update={"run": f"STREAM={root}/stream bash {agent}"}))
    spec = experiments.ExperimentSpec.model_validate({
        "name": "mined", "environment": "tenv", "trials": 1, "keep_minutes": 5,
        "tasks": [{"id": "t", "instructions": "Fix it.", "repo": world["url"], "base_sha": shas["base"]}],
        "variants": {"a": {}}})
    exp = await _until(jobs, experiments.submit(jobs, spec, Caller("svc", "svc")).id, DONE, timeout=120)
    assert exp.state == JobState.SUCCEEDED, [e.message for e in exp.events]
    v = experiments.view(jobs, exp)
    jsonschema.validate(v, INDEX_SCHEMA)
    [t] = v["trials"]
    child = jobs.get(t["job"])
    box = fleet.providers["docker"].boxes[child.sandbox_id]
    log, branches, tags, remotes = (box / "home/agent/gitstate.txt").read_text().split("---\n")
    assert log.split() == [shas["base"]], log
    assert branches.strip() == f"* {child.branch}" and tags.strip() == "" and remotes.strip() == ""
    assert jobs._session(child).base_sha == shas["base"] == child.result.base_sha
    sb = fleet.get(child.sandbox_id)
    iso = sb.info["trial_isolation"]
    assert iso["refs"] == [f"refs/heads/{child.branch}"] and iso["base_sha"] == shas["base"]
    m = experiments.trial_file(jobs, exp, t["key"], "manifest.json")
    jsonschema.validate(m, SCHEMA)
    assert m["repo"]["isolation"]["remotes"] == [] and m["repo"]["base_sha"] == shas["base"]
    # tenv sets no egress: the closed default, and no warning
    eff = m["egress"]["effective"]
    assert eff["eval"] == "closed" and eff["github"] is False and eff["mode"] == "allowlist"
    assert v["pins"]["trial_egress"] == {"source": "closed", "mode": "allowlist", "github": False}
    assert not [n for n in v["notes"] if "GitHub" in n]


def test_labels_alone_never_isolate_an_interactive_session(fleet):
    """A session that isn't a trial or an eval job is left alone, whatever labels it carries."""
    s = SimpleNamespace(labels={experiments.EXP_LABEL: "job-x", experiments.TRIAL_LABEL: "t/a/1"}, job_id="job-y")
    assert trialiso._trial(fleet, s) is None
    s.job_id = None
    assert trialiso._trial(fleet, s) is None


# ====================================================================== the git grant
def test_a_closed_grant_fetches_nothing_and_keeps_its_token(fleet):
    """KAN-80 found a fetch-only grant fetches any branch: once a trial is cloned, its grant covers no repository,
    so the gateway refuses every fetch (and the GitHub API path) before it asks upstream."""
    gw = fleet.gateway
    sb = Sandbox(name="t", environment="tenv", provider="docker", state=SandboxState.RUNNING,
                 repo="https://github.com/acme/app.git")
    fleet.store.put(sb)
    token = gw.mint(sb, "github.com", ["acme/app"], [], "gh")
    gw.close(sb.id)
    [g] = gw.grants(sb.id)
    assert g.repos == [] and g.push_refs == [] and gw.verify(token) is not None  # the package mirror still knows it
    with TestClient(gw.asgi_app()) as c:
        r = c.get("/git/github.com/acme/app.git/info/refs?service=git-upload-pack", auth=("x", token))
        assert r.status_code == 404 and "not found" in r.text
    denied = [e for e in fleet.store.audit_tail(50) if e.action == "gw.git.deny"]
    assert denied and denied[-1].detail["reason"] == "repo not granted"
    assert any(e.action == "gateway.close" for e in fleet.store.audit_tail(50))


# ====================================================================== egress
def _spec(**egress_block) -> EnvSpec:
    return EnvSpec(name="e", harnesses=["claude-code"], **({"egress": egress_block} if egress_block else {}))


def _sb(labels: dict[str, str], **info) -> Sandbox:
    return Sandbox(name="t", environment="e", provider="docker", state=SandboxState.RUNNING, labels=labels,
                   info={"harness": "claude-code", **info}, repo="https://github.com/acme/app.git")


def test_an_eval_job_on_an_environment_without_egress_gets_the_closed_allowlist(settings):
    plain = _spec()
    labels = trialiso.for_job("eval", plain, {"hangar.egress.eval": "spoofed"})
    assert labels == {trialiso.LABEL: "closed", "hangar.egress.floor": "allowlist", "hangar.egress.eval": "spoofed"}
    assert trialiso.for_job("interactive", plain, {trialiso.LABEL: "environment"}) == {}  # nobody else sets it
    assert trialiso.for_job("eval", plain, {"hangar.egress.floor": "none"})["hangar.egress.floor"] == "none"
    sb = _sb(labels, git={"mode": "gateway"})
    pol = egress.resolve(settings, plain, None, "claude-code", floor="allowlist", closed=True)
    assert pol.mode == "allowlist" and pol.proxied
    assert pol.allows("api.anthropic.com", 443) is not None  # the model API
    assert pol.allows("files.pythonhosted.org", 443) is not None  # the packages (the mirror, when it is on)
    for h in egress.PRESETS["github"]:
        assert pol.allows(h, 443) is None, h
    assert trialiso.closed(sb) and trialiso.setup_hosts(sb) == ()  # through the gateway: no host to clone from


def test_the_closed_default_drops_github_whoever_adds_it(settings):
    """A preset or `allow` without a mode, the repo policy and an approval: none of them opens GitHub to a trial."""
    env = _spec(presets=["packages", "github"], allow=["*.githubusercontent.com", "example.org"])
    repo = egress.EgressSpec(presets=["github"], allow=["api.github.com"])
    approved = [{"rule": "github.com", "request": "rq-1"}, {"rule": "pypi.org", "request": "rq-2"}]
    pol = egress.resolve(settings, env, repo, "claude-code", approved, floor="allowlist", closed=True)
    assert [r.text for r in pol.rules if trialiso.is_github(r.host)] == []
    assert pol.allows("example.org", 443) is not None and pol.allows("pypi.org", 443) is not None
    assert any("left out" in n and "github.com" in n for n in pol.notes)
    assert trialiso.github_rules(pol) == []
    # the same environment with a mode set is the environment's own choice: it keeps GitHub, with a warning
    env2 = _spec(mode="allowlist", presets=["packages", "github"])
    pol2 = egress.resolve(settings, env2, None, "claude-code")
    assert "github.com" in trialiso.github_rules(pol2)


def test_without_the_gateway_the_clone_reaches_its_git_host_until_it_is_isolated(fleet, settings):
    sb = _sb(trialiso.for_job("eval", _spec(), {}), git={"mode": "direct"})
    assert trialiso.setup_hosts(sb) == ("github.com",)
    pol = egress.resolve(settings, _spec(), None, "claude-code", floor="allowlist", closed=True,
                         setup_hosts=trialiso.setup_hosts(sb))
    assert pol.allows("github.com", 443).source == trialiso.SETUP
    assert trialiso.github_rules(pol) == []  # the clone's own host isn't a warning
    assert pol.allows("api.github.com", 443) is None
    sb.info[trialiso.DONE] = True
    assert trialiso.setup_hosts(sb) == ()


def test_the_experiment_plan_says_what_trials_reach(fleet, tmp_path):
    envs = tmp_path / "envs"
    envs.mkdir()
    (envs / "plain.yaml").write_text(yaml.safe_dump({"name": "plain", "harnesses": ["claude-code"]}))
    (envs / "hub.yaml").write_text(yaml.safe_dump({"name": "hub", "harnesses": ["claude-code"], "egress": {
        "mode": "allowlist", "presets": ["packages", "github"]}}))
    (envs / "wide.yaml").write_text(yaml.safe_dump({"name": "wide", "harnesses": ["claude-code"],
                                                    "egress": {"mode": "open"}}))
    fleet.catalog.dirs.insert(0, envs)
    fleet.catalog.reload()
    assert trialiso.plan(fleet, "plain") == {"source": "closed", "mode": "allowlist", "github": False}
    hub = trialiso.plan(fleet, "hub")
    assert hub["source"] == "environment" and hub["github"] is True and "reach GitHub" in hub["warning"]
    wide = trialiso.plan(fleet, "wide")
    assert wide["mode"] == "open" and wide["github"] is True and "any host" in wide["warning"]


@pytest.fixture
async def closed_proxy(settings, store, broker, tmp_path, monkeypatch):
    """The egress proxy on loopback, a running eval sandbox of an environment that sets no egress, and an upstream
    that echoes. github.com and api.anthropic.com both resolve to it; the upstream's port counts as HTTPS."""
    up_port, px_port = free_port(), free_port()
    envs = tmp_path / "envs"
    envs.mkdir()
    (envs / "plain.yaml").write_text(yaml.safe_dump({"name": "plain", "harnesses": ["claude-code"]}))
    settings.egress_port = px_port
    f = Fleet(settings, store, broker, EnvCatalog([ENVS, envs]))
    f.providers["docker"] = FakeProvider()
    spec = f.catalog.get("plain")
    sb = Sandbox(name="s", environment="plain", provider="docker", state=SandboxState.RUNNING, person="ann",
                 labels=trialiso.for_job("eval", spec, {"hangar.session": "ses-1"}),
                 info={"harness": "claude-code", "git": {"mode": "gateway"}})
    store.put(sb)
    import socket

    real = socket.getaddrinfo

    def fake_dns(host, *a, **k):
        if host in ("github.com", "api.anthropic.com"):
            return real("127.0.0.1", *a, **k)
        return real(host, *a, **k)

    monkeypatch.setattr(socket, "getaddrinfo", fake_dns)
    monkeypatch.setattr(egress_proxy, "public", lambda ip: str(ip) == "127.0.0.1")
    monkeypatch.setattr(egress, "DEFAULT_PORTS", (*egress.DEFAULT_PORTS, up_port))
    server = await asyncio.start_server(_echo, "127.0.0.1", up_port)
    await f.egress.listen("127.0.0.1", relay=False)
    yield SimpleNamespace(fleet=f, sb=sb, up=up_port, px=px_port, store=store)
    await f.egress.stop()
    server.close()


async def test_a_closed_trial_reaches_its_model_and_not_github(closed_proxy):
    p = closed_proxy
    first, r, w = await ask(p.px, f"CONNECT github.com:{p.up} HTTP/1.1\r\n{auth(p.fleet, p.sb)}")
    assert first.startswith(b"HTTP/1.1 403"), first
    w.close()
    first, r, w = await ask(p.px, f"CONNECT api.anthropic.com:{p.up} HTTP/1.1\r\n{auth(p.fleet, p.sb)}")
    assert first.startswith(b"HTTP/1.1 200"), first
    hello = client_hello("api.anthropic.com")
    w.write(hello)
    await w.drain()
    assert await asyncio.wait_for(r.read(65536), 5) == b"ECHO:" + hello
    w.close()


# ====================================================================== an environment that opens GitHub
async def test_an_experiment_whose_environment_opens_github_runs_with_a_warning(jobs, world, fleet):  # noqa: F811
    envs = Path(world["envs"])
    tenv = json.loads((envs / "tenv.yaml").read_text())
    (envs / "hubenv.yaml").write_text(json.dumps({**tenv, "name": "hubenv", "egress": {
        "mode": "allowlist", "presets": ["packages", "github"]}}))
    fleet.catalog.reload()
    spec = experiments.ExperimentSpec.model_validate({
        "name": "hub", "environment": "hubenv", "trials": 1, "keep_minutes": 5,
        "tasks": [{"id": "t", "instructions": "Look.", "repo": world["url"]}], "variants": {"a": {}}})
    exp = await _until(jobs, experiments.submit(jobs, spec, Caller("svc", "svc")).id, DONE, timeout=120)
    assert exp.state == JobState.SUCCEEDED, [e.message for e in exp.events]
    v = experiments.view(jobs, exp)
    jsonschema.validate(v, INDEX_SCHEMA)
    te = v["pins"]["trial_egress"]
    assert te["source"] == "environment" and te["github"] is True
    assert any("reach GitHub" in n for n in v["notes"])  # the report
    [t] = v["trials"]
    m = experiments.trial_file(jobs, exp, t["key"], "manifest.json")
    jsonschema.validate(m, SCHEMA)
    assert any("reach GitHub" in w for w in m["warnings"])  # the manifest
    child = jobs.get(t["job"])
    eff = fleet.get(child.sandbox_id).info["egress"]
    assert eff["eval"] == "environment" and eff["github"] is True
