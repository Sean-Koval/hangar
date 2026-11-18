"""A task's workspace (KAN-192): its files given in the experiment spec, in place of a hosted repo. hangar commits them
(the same files, the same commit), keeps the commit as a bundle, pins it as the task's base, and each trial and grader
sandbox clones it with no remote. Isolation, grading and the trial cache then work as for any other task."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from hangar import experiments, pinsets, trialcache, trialiso, workspaces
from hangar.fleet import FleetError
from hangar.harnesses import HARNESSES
from hangar.jobs import Caller, JobError
from hangar.models import Job, JobState, Session, SessionTurn
from tests.test_grade_forgery import CMD, FIX, HIDDEN, WRONG
from tests.test_interfaces import DONE, jobs, world  # noqa: F401 - fixtures
from tests.test_jobs import _until

pytestmark = pytest.mark.skipif(not shutil.which("git"), reason="needs git")

MOD = {"mod.py": "def add(a, b):\n    return a - b\n"}
MOD_SHA = "00b928dc089b9ad49199035e0a32ccfc90b097e2"  # the same files make this commit on any hangar
BUDGET = {"max_cost_usd": 1}


def _git(path: Path | str, *args: str) -> str:
    return subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True, text=True).stdout.strip()


def _task(**extra) -> dict:
    return {"id": "t", "instructions": "Fix add.", "workspace": MOD, **extra}


# ====================================================================== the commit
def test_the_same_files_give_the_same_commit_and_hangar_keeps_its_bundle(settings):
    assert workspaces.commit(settings, MOD) == MOD_SHA == workspaces.commit(settings, dict(MOD))
    other = workspaces.commit(settings, {**MOD, "docs/readme.md": "hi\n"})
    assert other != MOD_SHA
    data = workspaces.bundle(settings, MOD_SHA)
    assert data.startswith(b"# v2 git bundle") or data.startswith(b"# v3 git bundle")
    with pytest.raises(workspaces.WorkspaceError, match="isn't a workspace commit"):
        workspaces.bundle(settings, "../../etc/passwd")
    with pytest.raises(workspaces.WorkspaceError, match="doesn't have workspace"):
        workspaces.bundle(settings, "f" * 40)
    (Path(settings.data_dir) / "workspaces" / f"{MOD_SHA}.bundle").unlink()  # lost: made again, the same
    assert workspaces.commit(settings, MOD) == MOD_SHA and workspaces.bundle(settings, MOD_SHA) == data


def test_every_file_goes_in_as_given_whatever_ignores_attributes_or_the_hosts_config_say(settings, tmp_path,
                                                                                       monkeypatch):
    files = {".gitignore": "*.log\n", "keep.log": "kept\n", ".gitattributes": "* text eol=crlf\n",
             'odd dir/"q"\\name.txt': "a\nb\n", "sub/.gitmodules": "[submodule]\n"}
    sha = workspaces.commit(settings, files)
    home = tmp_path / "home"
    (home / ".config/git").mkdir(parents=True)
    (home / ".config/git/ignore").write_text("*.log\n*.txt\n")
    (home / ".gitconfig").write_text("[core]\n\tautocrlf = true\n[user]\n\tname = someone\n")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GIT_AUTHOR_NAME", "someone else")
    shutil.rmtree(Path(settings.data_dir) / "workspaces")
    assert workspaces.commit(settings, files) == sha  # the host's git setup changes nothing
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(Path(settings.data_dir) / "workspaces" / f"{sha}.bundle"), str(clone)],
                   check=True)
    listed = subprocess.run(["git", "-C", str(clone), "ls-tree", "-r", "-z", "--name-only", "HEAD"], check=True,
                            capture_output=True, text=True).stdout.split("\0")
    assert sorted(x for x in listed if x) == sorted(files)  # none ignored, none renamed
    for path, text in files.items():
        assert _git(clone, "cat-file", "blob", f"HEAD:{path}") == text.strip()
    assert {m.split()[0] for m in _git(clone, "ls-tree", "-r", "HEAD").splitlines()} == {"100644"}


def test_a_workspace_is_the_tasks_repo_with_safe_paths_and_a_size_cap():
    t = experiments.Task.model_validate(_task())
    assert t.workspace == MOD and t.repo is None
    for bad in ({}, {"/etc/x": "a"}, {"a/../b": "a"}, {".git/config": "a"}, {"a//b": "a"}, {"./a": "a"},
                {"a\0b": "a"}, {"a\nb": "a"}, {".GIT/config": "a"}, {"sub/.Git/HEAD": "a"}, {"GIT~1/config": "a"},
                {".git./x": "a"}, {"cafe\u0301.txt": "a"}, {"a": "1", "a/b": "2"},
                {"a": "x" * (experiments.MAX_WORKSPACE + 1)}):
        with pytest.raises(ValidationError):
            experiments.Task.model_validate(_task(workspace=bad))
    for clash in ({"repo": "https://github.com/o/r.git"}, {"issue": "o/r#1"}, {"base_branch": "dev"}):
        with pytest.raises(ValidationError, match="a `workspace` is the task's repo"):
            experiments.Task.model_validate(_task(**clash))
    big = {"f": "x" * (experiments.MAX_WORKSPACE - 10)}
    tasks = [{**_task(workspace=big), "id": f"t{i}"} for i in range(17)]
    with pytest.raises(ValidationError, match="16 MB in all"):
        experiments.ExperimentSpec.model_validate({"name": "e", "environment": "x", "variants": {"a": {}},
                                                   "tasks": tasks})


def test_a_workspace_is_part_of_the_tasks_cache_identity():
    def digest(**extra) -> str:
        return trialcache.task_digest(experiments.Task.model_validate({"id": "t", "instructions": "x", **extra}), "e")

    plain = digest()
    assert digest(workspace=MOD) != plain
    assert digest(workspace=MOD) != digest(workspace={"mod.py": "def add(a, b):\n    return a + b\n"})
    assert digest(workspace={**MOD, "b": "1"}) == digest(workspace={"b": "1", **MOD})  # order doesn't count


# ====================================================================== the sandbox
async def test_a_sandbox_clones_the_workspace_with_no_remote(fleet, tmp_path):
    fleet.catalog.add({"name": "wenv", "harnesses": ["git"]}, save_dir=tmp_path)
    sha = workspaces.commit(fleet.settings, MOD)
    sb = await fleet.create("wenv", repo="https://github.com/o/r.git", workspace=sha, work_branch="trial-1")
    assert sb.repo is None and sb.branch is None and sb.info["workspace"] == sha  # no repo: no grant, no host
    box = fleet.providers["docker"].boxes[sb.id]
    path = box / sb.info["repo_path"].lstrip("/")
    assert (path / "mod.py").read_text() == MOD["mod.py"]
    assert _git(path, "rev-parse", "HEAD") == sha and _git(path, "remote") == ""
    assert _git(path, "symbolic-ref", "--short", "HEAD") == "trial-1"
    assert not list((box / "tmp").glob("hangar-workspace-*"))  # the bundle doesn't stay behind
    with pytest.raises(FleetError, match="doesn't have workspace"):
        await fleet.create("wenv", workspace="e" * 40)


async def test_no_git_credential_goes_into_a_workspace_sandbox(fleet, tmp_path):
    from hangar.models import CredentialKind

    fleet.broker.put("gh", "git", CredentialKind.GIT_TOKEN, "ghp_x")
    fleet.broker.put("npm", "custom", CredentialKind.GENERIC, "s3cret", env_var="NPM_TOKEN")
    fleet.catalog.add({"name": "cenv", "harnesses": ["git"], "credentials": ["gh", "npm"]}, save_dir=tmp_path)
    sha = workspaces.commit(fleet.settings, MOD)
    assert (await fleet.create("cenv", workspace=sha)).credentials == ["npm"]
    assert (await fleet.create("cenv")).credentials == ["gh", "npm"]  # as before, without a workspace


def test_only_the_runners_record_names_a_trials_workspace(fleet):
    exp, job = "job-exp", "job-trial"
    fleet.store.kv_set(f"{experiments.KV_WORKSPACE}{exp}:t", MOD_SHA)
    s = SimpleNamespace(labels={experiments.EXP_LABEL: exp, experiments.TRIAL_LABEL: "t/a/1"}, job_id=job)
    assert trialiso.workspace(fleet, s) is None  # labels anyone can set: not a trial
    fleet.store.kv_set(experiments.KV_TRIAL + job, exp)
    assert trialiso.workspace(fleet, s) == MOD_SHA
    assert trialiso.workspace(fleet, SimpleNamespace(labels={}, job_id=job)) is None


# ====================================================================== pin sets and the trials API
def _pin(fleet, repo: str | None, base: str) -> pinsets.PinSet:
    body = pinsets.PinSetBody(harness="tagent", environment="plain", environment_spec_digest="sha256:x",
                              egress_digest="sha256:y", grader_digest=pinsets.grader_digest(None),
                              tasks={"t": pinsets.TaskPin(repo=repo, base_sha=base)})
    p = pinsets.PinSet(id=pinsets.pin_id(body), **body.model_dump())
    fleet.store.put(p)
    return p


def test_a_trials_request_runs_only_the_workspace_its_pin_set_pinned(fleet):
    svc = SimpleNamespace(fleet=fleet)

    def ask(p: pinsets.PinSet, files: dict[str, str]) -> None:
        pinsets.start_trials(svc, pinsets.TrialsRequest(pin_set=p.id, task=_task(workspace=files), budget=BUDGET),
                             None)

    ws = _pin(fleet, None, MOD_SHA)
    with pytest.raises(JobError, match="its workspace is commit"):
        ask(ws, {"mod.py": "changed\n"})
    with pytest.raises(JobError, match="its workspace is commit"):  # a hosted repo's pin isn't a workspace
        ask(_pin(fleet, "https://github.com/o/r.git", MOD_SHA), MOD)
    with pytest.raises(JobError, match="no harness lock"):  # past the workspace check
        ask(ws, MOD)


# ====================================================================== grading a workspace task
@pytest.fixture
async def ws_grading(fleet, tmp_path, monkeypatch):
    from hangar import egress, exchange

    patches: dict[str, str] = {}

    async def session_patch(svc, s):
        return {"patch": patches.get(s.name, ""), "base": MOD_SHA}

    async def proxy_url(fleet_, prov, sb):
        return "http://127.0.0.1:9"

    monkeypatch.setattr(exchange, "session_patch", session_patch)
    monkeypatch.setattr(egress, "_proxy_url", proxy_url)
    fleet.catalog.add({"name": "genv", "harnesses": ["git"]}, save_dir=tmp_path)
    children: dict[str, tuple[Job, Session]] = {}

    class Jobs:
        def __init__(self):
            self.fleet, self.settings, self.broker, self.sessions = fleet, fleet.settings, fleet.broker, None

        def get(self, jid):
            return children[jid][0]

        def _session(self, job):
            return children[job.id][1]

        def _save(self, job):
            pass

    def trial(key: str, patch: str) -> experiments.Trial:
        s = Session(name=key.replace("/", "-"), environment="genv", base_sha=MOD_SHA)
        child = Job(title=key, instructions="x", environment="genv", state=JobState.SUCCEEDED)
        s.turns.append(SessionTurn(message="x", run_id="r1", summary="done"))
        children[child.id] = (child, s)
        patches[s.name] = patch
        task, variant, n = key.split("/")
        return experiments.Trial(key=key, task=task, variant=variant, trial=int(n), job=child.id, state="grading",
                                 started_at=1.0)

    yield Jobs(), trial


async def test_a_workspace_task_is_graded_on_a_clone_of_its_commit(ws_grading, fleet):
    svc, trial = ws_grading
    spec = experiments.ExperimentSpec.model_validate({
        "name": "e", "environment": "genv", "variants": {"a": {}}, "tasks": [_task(reference_patch=FIX)],
        "grader": {"command": CMD, "files": HIDDEN, "timeout_seconds": 60}})
    st = experiments.State(spec=spec.model_dump(mode="json"),
                           pins={"tasks": {"t": {"repo": None, "base_sha": MOD_SHA, "workspace": MOD_SHA}}})
    experiments.give_canary(spec, st.spec)
    exp = Job(title="e", instructions="x", environment="genv")
    shutil.rmtree(Path(fleet.settings.data_dir) / "workspaces", ignore_errors=True)  # the grader makes it again
    a, b = trial("t/a/1", FIX), trial("t/a/2", WRONG)
    await experiments._grade(svc, exp, st, spec, a)
    await experiments._grade(svc, exp, st, spec, b)
    assert a.grade["exit_code"] == 0 and b.grade["exit_code"] == 1, (a.grade, b.grade)
    check = st.task_checks["t"]
    assert check["invalid"] is False and check["base"]["exit_code"] == 1 and check["reference"]["exit_code"] == 0
    graders = [x for x in fleet.list(include_stopped=True) if x.labels.get(experiments.GRADER_LABEL) == exp.id]
    assert graders and all(x.repo is None and x.info.get("workspace") == MOD_SHA for x in graders)
    assert fleet.store.kv_get(f"{experiments.KV_WORKSPACE}{exp.id}:t") == MOD_SHA


# ====================================================================== an experiment, end to end
async def test_a_workspace_experiments_trial_sees_its_files_and_nothing_else(jobs, world, fleet,  # noqa: F811
                                                                             monkeypatch):
    """A trial of a task with no hosted repo: its agent finds the task's files at the workspace commit, with no
    remote and no other ref, and the experiment pins that commit."""
    root = Path(world["root"])
    agent = root / "look.sh"
    agent.write_text(r"""#!/usr/bin/env bash
cd "$(git rev-parse --show-toplevel)"
cat "$STREAM.before"
{ git log --all --format=%H; echo ---; git remote; echo ---; cat mod.py; } > "$HOME/gitstate.txt"
cat "$STREAM.after"
""")
    monkeypatch.setitem(HARNESSES, "tagent", HARNESSES["tagent"].model_copy(
        update={"run": f"STREAM={root}/stream bash {agent}"}))
    spec = experiments.ExperimentSpec.model_validate({
        "name": "files", "environment": "tenv", "trials": 1, "keep_minutes": 5, "tasks": [_task()],
        "variants": {"a": {}}})
    exp = await _until(jobs, experiments.submit(jobs, spec, Caller("svc", "svc")).id, DONE, timeout=120)
    assert exp.state == JobState.SUCCEEDED, [e.message for e in exp.events]
    v = experiments.view(jobs, exp)
    assert v["pins"]["tasks"]["t"]["base_sha"] == MOD_SHA and v["pins"]["tasks"]["t"]["repo"] is None
    [t] = v["trials"]
    child = jobs.get(t["job"])
    box = fleet.providers["docker"].boxes[child.sandbox_id]
    log, remotes, mod = (box / "home/agent/gitstate.txt").read_text().split("---\n")
    assert log.split() == [MOD_SHA] and remotes.strip() == "" and mod == MOD["mod.py"]
    sb = fleet.get(child.sandbox_id)
    assert sb.repo is None and sb.info["workspace"] == MOD_SHA
    assert sb.info["trial_isolation"]["base_sha"] == MOD_SHA and sb.info["trial_isolation"]["git_grant"] == "none"
    assert not [w for w in sb.info.get("warnings", []) if "trial isolation" in w]
