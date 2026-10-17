"""Harness locks (#133; docs/harness-lock.md): `hangar.harness-lock/v1`, `hangar harness freeze` and `check`.

The digests freeze writes must be the manifest's own (the declared `_dir_digest` form and the sandbox probe's `tree`
form); a frozen job turn yields a directory whose check passes and whose lock digest is that turn's harness_digest; a
turn whose skill the agent changed can't be frozen; content that can't be recovered is named; owner scoping follows
the manifests'."""

from __future__ import annotations

import asyncio
import base64
import copy
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import httpx
import jsonschema
import pytest
import yaml
from typer.testing import CliRunner

from hangar import harness_lock, manifest
from hangar.harnesses import HARNESSES, Harness
from hangar.jobs import Caller, JobService, SubmitJob
from hangar.models import CredentialKind, JobState, Session, SessionTurn
from hangar.sessions import SessionService
from hangar.vault import Vault
from tests.conftest import ENVS, scratch_root
from tests.test_jobs import Hooks, _until

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = json.loads((ROOT / harness_lock.SCHEMA_FILE).read_text())
GIT = ["git", "-c", "user.email=t@t", "-c", "user.name=t", "-c", "init.defaultBranch=main"]
SECRET = "hgsecret-LOCK-5d2e8a91c4f7"
SETTINGS = json.dumps({"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command",
                                                                               "command": "true"}]}]}})


def _files(tmp: Path, files: dict[str, bytes]) -> Path:
    for rel, data in files.items():
        p = tmp / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    return tmp


# ====================================================================== digests: the manifest's own forms
TRICKY = {"SKILL.md": b"Use it.\n", "a-b/x.txt": b"1", "a/b.txt": b"2", ".hidden": b"h", "Z.md": b"z",
          "dir/sub/deep.bin": bytes(range(256)), "empty": b""}


def test_files_digest_is_the_manifests_dir_digest(tmp_path):
    d = _files(tmp_path / "s", TRICKY)
    assert harness_lock.files_digest(harness_lock.read_dir(d)) == manifest._dir_digest(str(d))[0]
    (tmp_path / "e").mkdir()
    assert harness_lock.files_digest({}) == manifest._dir_digest(str(tmp_path / "e"))[0]


def test_tree_digest_is_what_the_sandbox_probe_reports(tmp_path):
    home = tmp_path / "home"
    d = _files(home / ".claude/skills/review", TRICKY)
    (home / ".claude/skills/empty").mkdir(parents=True)
    out = subprocess.run(["bash", "-c", manifest.probe_script(None, str(home), "claude-code")],
                         capture_output=True, text=True, check=True).stdout
    got = {x["name"]: x["digest"] for x in manifest.parse_probe(out)["skills"]}
    assert got["review"] == harness_lock.tree_digest(harness_lock.read_dir(d))
    assert got["empty"] == harness_lock.tree_digest({})


# ====================================================================== the lock: schema, identity, check
def _lock(**over) -> dict:
    sha = "sha256:" + "a" * 64
    lock = {"schema": harness_lock.SCHEMA, "name": "team-default", "revision": 3,
            "runtime": {"harness": "claude-code", "version": "2.1.296", "pack": None},
            "model": {"id": "claude-opus-5-5", "policy": "balanced"},
            "components": [{"id": "guide:CLAUDE.md", "digest": sha, "path": "CLAUDE.md"},
                           {"id": "skill:review", "digest": sha, "tree": sha, "path": "skills/review",
                            "source": "path:skills/review"},
                           {"id": "skill:verify-ui", "digest": None, "source": "hangar:computer"}],
            "binding": {"mcp": [{"name": "github", "transport": "http", "endpoint": "https://api.example/mcp/",
                                 "header_names": ["Authorization"]}],
                        "hooks": [{"event": "Stop", "source": "hangar:verify_gate"}],
                        "permissions": {"approvals": "deny-risky"}},
            "plugins": {"autoUpdate": False}, "remote": []}
    lock.update(over)
    return lock


def test_the_schema_and_the_runtime_check_agree():
    good = _lock()
    jsonschema.validate(good, SCHEMA)
    assert harness_lock.problems(good) == []
    bad = [
        ({"plugins": {"autoUpdate": True}}, "autoUpdate must be false"),
        ({"revision": 0}, "revision"),
        ({"name": "Team Default"}, "name"),
        ({"components": [{"id": "skill:x", "digest": "sha256:" + "a" * 64, "path": "../x"}]}, "path"),
        ({"components": [{"id": "skill:x", "digest": None}]}, "reference"),
        ({"binding": {"mcp": [{"name": "g", "transport": "http", "headers": {"A": "v"}}]}}, "names only"),
    ]
    for over, msg in bad:
        lock = _lock(**over)
        assert any(msg in p for p in harness_lock.problems(lock)), (over, harness_lock.problems(lock))
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(lock, SCHEMA)


def test_identity_ignores_name_revision_paths_and_provenance():
    base = harness_lock.harness_digest(_lock())
    same = _lock(name="other", revision=9, model={"id": "claude-opus-5-5", "policy": "frontier"})
    same["components"][1].update(path="elsewhere/review", source="path:x")
    same["runtime"]["pack"] = "reg/pack@sha256:1"
    assert harness_lock.harness_digest(same) == base
    for over in ({"model": {"id": "claude-sonnet-5-5"}}, {"runtime": {"harness": "claude-code", "version": "2.1.297"}},
                 {"binding": {**_lock()["binding"], "permissions": {"approvals": "auto"}}}):
        assert harness_lock.harness_digest(_lock(**over)) != base, over
    moved = _lock()
    moved["components"][1]["digest"] = "sha256:" + "b" * 64
    assert harness_lock.harness_digest(moved) != base


def test_check_names_each_mismatch(tmp_path):
    files = {"CLAUDE.md": b"Be careful.\n", "skills/review/SKILL.md": b"Review it.\n"}
    skill = {"SKILL.md": files["skills/review/SKILL.md"]}
    lock = _lock(components=[
        {"id": "guide:CLAUDE.md", "digest": harness_lock._sha(files["CLAUDE.md"]), "path": "CLAUDE.md"},
        {"id": "skill:review", "digest": harness_lock.files_digest(skill), "tree": harness_lock.tree_digest(skill),
         "path": "skills/review"}])
    harness_lock.write(tmp_path, lock, files)
    res = harness_lock.check(tmp_path)
    assert res["ok"] and res["digest"] == harness_lock.harness_digest(lock), res
    jsonschema.validate(yaml.safe_load((tmp_path / "harness.lock").read_text()), SCHEMA)
    (tmp_path / "skills/review/SKILL.md").write_text("Review it, quickly.\n")
    (tmp_path / "skills/extra").mkdir()
    (tmp_path / "native/claude-code").mkdir(parents=True)
    (tmp_path / "native/claude-code/settings.json").write_text("{}")
    (tmp_path / "CLAUDE.md").unlink()
    res = harness_lock.check(tmp_path)
    assert not res["ok"]
    text = "\n".join(res["problems"])
    assert "skill:review: skills/review/ has" in text and "guide:CLAUDE.md: CLAUDE.md is missing" in text
    assert "skills/extra is not in the lock" in text and "native/claude-code/settings.json is not in the lock" in text
    assert not harness_lock.check(tmp_path / "nowhere")["ok"]


def test_write_replaces_what_the_lock_manages_and_nothing_else(tmp_path):
    (tmp_path / "README.md").write_text("the team's harness\n")
    one = {"skills/a/SKILL.md": b"a", "skills/a/old.txt": b"old"}
    lock = _lock(components=[{"id": "skill:a", "digest": harness_lock.files_digest({"SKILL.md": b"a", "old.txt":
                                                                                    b"old"}), "path": "skills/a"}])
    harness_lock.write(tmp_path, lock, one)
    two = {"skills/b/SKILL.md": b"b"}
    lock2 = _lock(components=[{"id": "skill:b", "digest": harness_lock.files_digest({"SKILL.md": b"b"}),
                               "path": "skills/b"}])
    harness_lock.write(tmp_path, lock2, two, prev=lock)
    assert not (tmp_path / "skills/a").exists() and (tmp_path / "skills/b/SKILL.md").read_bytes() == b"b"
    assert (tmp_path / "README.md").exists() and harness_lock.check(tmp_path)["ok"]
    with pytest.raises(ValueError, match="outside"):
        harness_lock.write(tmp_path, lock2, {"../escape": b"x"})
    with pytest.raises(ValueError, match="outside"):
        harness_lock.unwire({"lock": lock2, "files": {"/etc/passwd": ""}})


def test_the_revision_moves_only_when_the_harness_does():
    a = _lock(revision=1)
    assert harness_lock.next_revision(None, a) == 1
    assert harness_lock.next_revision({**a, "revision": 4}, {**a, "name": "renamed"}) == 4
    assert harness_lock.next_revision({**a, "revision": 4}, _lock(model={"id": "m2", "policy": None})) == 5


# ====================================================================== freeze from a manifest (unit)
@pytest.fixture
def skillenv(tmp_path, monkeypatch):
    monkeypatch.setitem(HARNESSES, "tagent", Harness(name="tagent", title="t", run="true",
                                                     skills_dir=".tagent/skills",
                                                     instructions_file=".tagent/AGENTS.md"))
    envs = tmp_path / "envs"
    _files(envs, {"skills/alpha/SKILL.md": b"Use alpha.\n", "skills/beta/SKILL.md": b"Use beta.\n",
                  "skills/beta/ref/notes.md": b"More.\n"})
    (envs / "lockenv.yaml").write_text(json.dumps({
        "name": "lockenv", "harnesses": ["tagent"], "skills": [{"path": "skills/alpha"}, {"path": "skills/beta"}],
        "agent_config": {"instructions": "Run the tests first.", "files": {".tagent/settings.json": SETTINGS}},
        "mcp": {"github": {"url": "https://api.example/mcp?token=x", "headers": {"Authorization": "Bearer x"}},
                "local": {"command": "npx", "args": ["some-mcp"], "env": {"API_KEY": "x"}}}}))
    return envs


def _turn_manifest(fleet, envs, *, effective=True, **over):
    fleet.catalog.dirs.insert(0, envs)
    fleet.catalog.reload()
    spec = fleet.catalog.get("lockenv")
    s = Session(name="s1", environment="lockenv", harness="tagent", model="claude-opus-5-5", person=None)
    t = SessionTurn(message="x", run_id="run-1")
    s.turns.append(t)
    probe = None
    if effective:
        probe = {"global": None, "repo": [], "head": None, "skills": [
            {"name": n, "digest": harness_lock.tree_digest(harness_lock.read_dir(envs / "skills" / n)), "files": 1}
            for n in ("alpha", "beta")]}
    m = manifest.build(fleet, spec, "tagent", s=s, turn=t, n=1, probe=probe)
    for k, v in over.items():
        m[k] = v
    return m, spec


def test_freeze_reproduces_the_turns_harness_digest(fleet, skillenv, tmp_path):
    m, spec = _turn_manifest(fleet, skillenv)
    b = harness_lock.freeze(fleet, m, spec, name="team")
    assert b["digest"] == m["harness_digest"]
    lock = b["lock"]
    jsonschema.validate(lock, SCHEMA)
    assert [c["id"] for c in lock["components"]] == ["guide:AGENTS.md", "skill:alpha", "skill:beta",
                                                    "native:tagent/settings.json"]
    assert lock["binding"]["mcp"][0] == {"name": "github", "transport": "http", "endpoint": "https://api.example/mcp",
                                         "header_names": ["Authorization"]}
    assert lock["binding"]["mcp"][1]["env_names"] == ["API_KEY"] and "Bearer" not in json.dumps(lock)
    assert any(h["source"] == "environment:.tagent/settings.json" and h["digest"] for h in lock["binding"]["hooks"])
    assert b["files"]["AGENTS.md"] == b"Run the tests first."
    assert b["files"]["skills/beta/ref/notes.md"] == b"More.\n"
    out = tmp_path / "harness"
    harness_lock.write(out, lock, b["files"])
    res = harness_lock.check(out)
    assert res["ok"] and res["digest"] == m["harness_digest"], res
    assert any("best effort" in w for w in b["warnings"])  # tagent isn't claude-code or codex


def test_freeze_names_what_it_cannot_recover(fleet, skillenv):
    # the agent changed a skill in the sandbox: its effective digest matches no declared source
    m, spec = _turn_manifest(fleet, skillenv)
    m["skills"]["effective"][1]["digest"] = "sha256:" + "0" * 64
    with pytest.raises(harness_lock.FreezeError) as e:
        harness_lock.freeze(fleet, m, spec, name="team")
    assert len(e.value.gaps) == 1 and e.value.gaps[0].startswith("skill:beta: the sandbox's copy differs")
    # instructions truncated in the manifest, and the environment's changed since: nothing has the digest
    m, spec = _turn_manifest(fleet, skillenv)
    m["instructions"]["global"]["declared"].update(truncated=True, digest="sha256:" + "1" * 64)
    with pytest.raises(harness_lock.FreezeError, match=r"guide:AGENTS.md: its text was truncated.*16000"):
        harness_lock.freeze(fleet, m, spec, name="team")
    # truncated, but the environment still has the declared text: recovered from it
    m, spec = _turn_manifest(fleet, skillenv)
    m["instructions"]["global"]["declared"]["truncated"] = True
    m["instructions"]["global"]["declared"]["text"] = "Run the"
    assert harness_lock.freeze(fleet, m, spec, name="team")["files"]["AGENTS.md"] == b"Run the tests first."
    # a skill's source edited after the turn
    m, spec = _turn_manifest(fleet, skillenv)
    (skillenv / "skills/alpha/SKILL.md").write_text("Use alpha, differently.\n")
    with pytest.raises(harness_lock.FreezeError, match=r"skill:alpha: its source skills/alpha changed since the turn"):
        harness_lock.freeze(fleet, m, spec, name="team")
    # not a turn; a plugin without a digest; a bad name
    m, spec = _turn_manifest(fleet, skillenv)
    with pytest.raises(harness_lock.FreezeError, match="a turn's captured manifest"):
        harness_lock.freeze(fleet, {**m, "subject": {**m["subject"], "kind": "session"}}, spec, name="team")
    (skillenv / "skills/alpha/SKILL.md").write_text("Use alpha.\n")
    m, spec = _turn_manifest(fleet, skillenv)
    with pytest.raises(harness_lock.FreezeError, match=r"plugin:p@None: the turn recorded no digest"):
        harness_lock.freeze(fleet, {**m, "plugins": [{"name": "p"}]}, spec, name="team")
    with pytest.raises(harness_lock.FreezeError, match="name 'Bad Name'"):
        harness_lock.freeze(fleet, m, spec, name="Bad Name")


def test_freeze_never_writes_a_secret(fleet, skillenv, broker):
    broker.put("seeded", "webhook", CredentialKind.GENERIC, SECRET)
    (skillenv / "skills/beta/ref/notes.md").write_text(f"token: {SECRET}\n")
    m, spec = _turn_manifest(fleet, skillenv)
    with pytest.raises(harness_lock.FreezeError) as e:
        harness_lock.freeze(fleet, m, spec, name="team")
    assert any(g.startswith("skills/beta/ref/notes.md: holds a value hangar redacts") for g in e.value.gaps)
    assert SECRET not in str(e.value)


def test_freeze_takes_a_trials_skills_from_the_experiments_snapshot(fleet, skillenv):
    """A variant's path skill: the experiment stored its files when it started; freeze uses them even after the
    environment's copy changed (they hash to what the turn declared)."""
    m, spec = _turn_manifest(fleet, skillenv)
    alpha = harness_lock.read_dir(skillenv / "skills/alpha")
    declared = m["skills"]["declared"][0]["digest"]
    overlay = {"skills": [{"name": "alpha", "source": "path:skills/alpha", "digest": declared,
                           "files": {k: base64.b64encode(v).decode() for k, v in alpha.items()}}]}
    (skillenv / "skills/alpha/SKILL.md").write_text("edited after the experiment started\n")
    b = harness_lock.freeze(fleet, m, spec, name="team", overlay=overlay)
    assert b["files"]["skills/alpha/SKILL.md"] == b"Use alpha.\n" and b["digest"] == m["harness_digest"]


# ====================================================================== end to end: a job's turns
AGENT = r"""#!/usr/bin/env bash
cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
echo '{"type":"system","subtype":"init","model":"claude-sonnet-5-5","session_id":"c1"}'
echo "touched $(date +%s%N)" >> "$HOME/.tagent/skills/alpha/SKILL.md"   # the agent edits a skill
echo x >> work.txt && git add work.txt && git commit -qm "agent: work"
echo '{"type":"result","subtype":"success","is_error":false,"result":"done",'\
'"usage":{"input_tokens":1,"output_tokens":1}}'
"""


@pytest.fixture
def world(monkeypatch, skillenv):
    root = Path(tempfile.mkdtemp(prefix="hl-", dir=scratch_root()))
    remote, seed = root / "remote.git", root / "seed"
    subprocess.run([*GIT, "init", "-q", "--bare", str(remote)], check=True)
    subprocess.run([*GIT, "init", "-q", str(seed)], check=True)
    (seed / "README.md").write_text("hello\n")
    subprocess.run([*GIT, "-C", str(seed), "add", "."], check=True)
    subprocess.run([*GIT, "-C", str(seed), "commit", "-qm", "init"], check=True)
    subprocess.run([*GIT, "-C", str(seed), "push", "-q", str(remote), "HEAD:refs/heads/main"], check=True)
    subprocess.run(["git", "--git-dir", str(remote), "symbolic-ref", "HEAD", "refs/heads/main"], check=True)
    (root / "agent.sh").write_text(AGENT)
    monkeypatch.setitem(HARNESSES, "tagent", Harness(name="tagent", title="t", run=f"bash {root}/agent.sh",
                                                     skills_dir=".tagent/skills",
                                                     instructions_file=".tagent/AGENTS.md"))
    yield {"root": root, "url": f"file://{remote}", "envs": skillenv}
    shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
async def jobs(fleet, world, settings, store):
    settings.session_poll_seconds = settings.job_poll_seconds = 0.1
    fleet.catalog.dirs.insert(0, world["envs"])
    fleet.catalog.reload()
    fake = fleet.providers["docker"]
    create = fake.create

    async def create_with_baked_skills(sandbox, image, spec=None):  # the image's skills, as a build bakes them
        await create(sandbox, image, spec)
        for name in ("alpha", "beta"):
            dst = fake.boxes[sandbox.provider_ref] / "home/agent/.tagent/skills" / name
            shutil.copytree(world["envs"] / "skills" / name, dst, dirs_exist_ok=True)

    fake.create = create_with_baked_skills
    sessions = SessionService(fleet, Vault(store, b"0" * 43 + b"="))
    await sessions.start()
    svc = JobService(fleet, sessions, transport=httpx.MockTransport(Hooks()))
    await svc.start()
    yield svc
    await svc.stop()
    await sessions.stop()
    for box in fake.boxes.values():
        subprocess.run(["tmux", "kill-server"], env={"TMUX_TMPDIR": f"{box}/tmp", "PATH": "/usr/bin:/bin"},
                       capture_output=True)


DONE = {JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED}


async def _recorded(jobs, job, n):
    s = jobs._session(job)
    for _ in range(200):
        if len(s.turns) >= n and s.turns[n - 1].manifest:
            return s
        await asyncio.sleep(0.05)
    raise AssertionError(f"turn {n} of {job.id} has no manifest")


@pytest.mark.skipif(not shutil.which("tmux") or not shutil.which("git"), reason="needs tmux and git")
async def test_freezing_a_jobs_turn_and_a_turn_whose_skill_the_agent_changed(jobs, world, tmp_path):
    job = jobs.submit(SubmitJob(instructions="work", repo=world["url"], environment="lockenv", deliver="none",
                                keep_minutes=60), Caller("svc", "svc"))
    job = await _until(jobs, job.id, DONE, timeout=40)
    assert job.state == JobState.SUCCEEDED, [e.message for e in job.events]
    s = await _recorded(jobs, job, 1)
    # turn 1: the sandbox had the declared skills; freezing it gives a directory that checks, with its digest
    b = harness_lock.freeze_job(jobs, job, 1)
    out = tmp_path / "team-harness"
    lock, files = harness_lock.unwire(json.loads(json.dumps(harness_lock.wire(b))))
    harness_lock.write(out, lock, files)
    res = harness_lock.check(out)
    assert res["ok"], res
    assert res["digest"] == s.turns[0].harness_digest == manifest.for_job(jobs, job, 1)["harness_digest"]
    assert lock["name"] == "lockenv" and lock["model"]["id"] == s.model
    # turn 2: the agent edited skill alpha during turn 1, so the sandbox's copy is no longer the declared one
    while job.id in jobs._active:
        await asyncio.sleep(0.05)
    jobs.follow_up(job.id, "more", requester="svc")
    job = await _until(jobs, job.id, DONE, timeout=40)
    assert job.state == JobState.SUCCEEDED and len(job.turns) == 2, [e.message for e in job.events]
    await _recorded(jobs, job, 2)
    with pytest.raises(harness_lock.FreezeError) as e:
        harness_lock.freeze_job(jobs, job, 2)
    assert [g.split(":")[0:2] for g in e.value.gaps] == [["skill", "alpha"]], e.value.gaps
    assert "changed in the sandbox" in e.value.gaps[0]


# ====================================================================== REST and the CLI
def _client_for(c):
    """cli._client() talking to a TestClient."""
    import contextlib

    @contextlib.contextmanager
    def client():
        yield c

    return client


def test_rest_freeze_is_owner_scoped_and_the_cli_writes_the_directory(settings, skillenv, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from hangar import cli
    from hangar.app import create_app
    from hangar.models import Job

    settings.admin_token = "t0ken"
    settings.env_dirs = [skillenv, ENVS]
    admin = {"Authorization": "Bearer t0ken"}
    with TestClient(create_app(settings)) as c:
        h = c.app.state.hangar
        login = c.post("/api/people", json={"id": "alice"}, headers=admin).json()["login"]
        alice = {"Authorization": f"Bearer {login['token']}"}
        digests = {}
        for name, person in (("alices", "alice"), ("bobs", "bob")):
            s = Session(name=name, environment="lockenv", harness="tagent", model="claude-opus-5-5", person=person)
            t = SessionTurn(message="x", run_id=f"run-{name}")
            s.turns.append(t)
            m = manifest.build(h.fleet, h.fleet.catalog.get("lockenv"), "tagent", s=s, turn=t, n=1)
            manifest.store(settings).put(s.id, 1, m)
            t.manifest, t.harness_digest = m["digest"], m["harness_digest"]
            digests[name] = m["harness_digest"]
            h.store.put(s)
        r = c.get("/api/sessions/alices/harness?turn=1&name=team", headers=alice)
        assert r.status_code == 200, r.text
        assert r.json()["harness_digest"] == digests["alices"] and r.json()["lock"]["name"] == "team"
        r = c.get("/api/sessions/bobs/harness", headers=alice)
        assert r.status_code == 404 and "no session" in r.json()["error"]  # as if it didn't exist
        assert c.get("/api/sessions/bobs/harness", headers=admin).status_code == 200
        assert c.get("/api/sessions/alices/harness?turn=2", headers=alice).status_code == 404
        # jobs: a jobs token freezes only the jobs it submitted
        tok = c.post("/api/tokens", json={"name": "svc", "scopes": ["jobs"]}, headers=admin).json()
        other = Job(title="t", instructions="x", environment="lockenv", owner="tok-someone-else")
        h.store.put(other)
        assert c.get(f"/api/jobs/{other.id}/harness", headers={"Authorization": f"Bearer {tok['token']}"}
                     ).status_code == 404
        # the CLI: writes the directory, checks it, keeps the revision when nothing changed
        monkeypatch.setattr(cli, "_client", _client_for(TestClient(c.app, headers=alice)))
        out = tmp_path / "harness"
        runner = CliRunner()
        r = runner.invoke(cli.app, ["harness", "freeze", "alices", "--turn", "1", "--out", str(out), "--name",
                                    "team"])
        assert r.exit_code == 0, r.output
        assert "team r1" in r.output
        first = harness_lock.load(out)
        assert first["revision"] == 1 and harness_lock.check(out)["ok"]
        r = runner.invoke(cli.app, ["harness", "freeze", "alices", "--out", str(out)])
        assert r.exit_code == 0, r.output
        assert harness_lock.load(out)["revision"] == 1 and harness_lock.load(out)["name"] == "team"
        r = runner.invoke(cli.app, ["harness", "check", str(out)])
        assert r.exit_code == 0 and "ok: team r1" in r.output
        r = runner.invoke(cli.app, ["harness", "freeze", "--check", str(out), "--json"])
        assert r.exit_code == 0 and json.loads(r.output)["ok"]
        (out / "AGENTS.md").write_text("changed by hand\n")
        r = runner.invoke(cli.app, ["harness", "check", str(out)])
        assert r.exit_code == 1 and "guide:AGENTS.md" in r.output
        # --- #251: freezing over a directory with edits its lock doesn't record is refused, and nothing is touched
        (out / "skills/alpha/local.md").write_text("an uncommitted note\n")
        r = runner.invoke(cli.app, ["harness", "freeze", "alices", "--out", str(out)])
        assert r.exit_code == 1 and "doesn't match its own harness.lock" in r.output
        assert "guide:AGENTS.md" in r.output and "--force" in r.output
        assert (out / "AGENTS.md").read_text() == "changed by hand\n" and (out / "skills/alpha/local.md").exists()
        r = runner.invoke(cli.app, ["harness", "freeze", "alices", "--out", str(out), "--force"])
        assert r.exit_code == 0, r.output
        assert "warning: overwriting (--force): guide:AGENTS.md" in r.output
        assert harness_lock.check(out)["ok"] and not (out / "skills/alpha/local.md").exists()
        assert harness_lock.overwrite_problems(out) == [] and harness_lock.overwrite_problems(tmp_path / "new") == []
        # a gap is reported, not written
        (skillenv / "skills/alpha/SKILL.md").write_text("edited since\n")
        r = runner.invoke(cli.app, ["harness", "freeze", "alices", "--out", str(tmp_path / "other")])
        assert r.exit_code == 1 and "skill:alpha: its source skills/alpha changed" in r.output
        assert not (tmp_path / "other").exists()


def test_copy_of_lock_is_independent():  # dump/load round trip keeps the lock exactly
    lock = _lock()
    again = yaml.safe_load(harness_lock.dump(copy.deepcopy(lock)))
    assert again == lock
