"""Grade anti-forgery (--- anti-forgery #127): trust levels, the base-must-fail check, canary strings, touched tests.

The uid split's grade wrapper is exercised for real where this machine allows it (root, setpriv, a `nobody` user):
the grader's command runs as another user and tries to overwrite the grade; the Docker end to end is
tests/test_e2e_sealed_docker.py."""

from __future__ import annotations

import json
import os
import pwd
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import jsonschema
import pytest

from hangar import experiments
from hangar.models import Job, JobState, Session, SessionTurn

ROOT = Path(__file__).resolve().parents[1]
INDEX_SCHEMA = json.loads((ROOT / "docs/api/experiment.schema.json").read_text())


# ====================================================================== trust
def test_trust_levels():
    t = experiments.trust_of
    assert t(True, "pristine", "hangar-grade") == "isolated"
    assert t(False, "pristine", "hangar-grade") == "best_effort"  # no uid split (GKE)
    assert t(True, "in_repo", "hangar-grade") == "low" and t(True, "pristine", "command") == "low"
    r = experiments._runner
    assert r("hangar-grade pytest -q") == "hangar-grade" and r("X=1 hangar-grade pytest") == "hangar-grade"
    assert r("pytest -q; hangar-grade pytest") == "command" and r("echo hangar-grade") == "command"
    assert r("bash -c 'hangar-grade pytest'") == "command" and r("'unclosed") == "command"


def test_a_grade_passes_on_its_hidden_tests_else_its_exit_code():
    p = experiments.passed
    assert p({"exit_code": 0}) is True and p({"exit_code": 1}) is False
    assert p({"exit_code": 0, "hidden_tests": {"total": 2, "failed": 1, "error": 0}}) is False
    assert p({"exit_code": 0, "hidden_tests": {"total": 2, "failed": 0, "error": 0}}) is True
    assert p({"exit_code": 0, "hidden_tests": {"total": 0}}) is True  # no cases: the exit code says
    assert p({"error": "x"}) is None and p({}) is None and p(None) is None


# ====================================================================== canaries
def test_the_canary_goes_into_each_hidden_test_file_as_a_comment():
    c = "HANGAR-CANARY-1234-abcd-5678"
    assert experiments.with_canary("tests/test_x.py", "def test(): pass", c) == \
        (f"def test(): pass\n# {c}\n", True)
    assert experiments.with_canary("a.test.ts", "x\n", c) == (f"x\n// {c}\n", True)
    assert experiments.with_canary("x_test.go", "", c) == (f"// {c}\n", True)
    assert experiments.with_canary("q.sql", "select 1;\n", c)[0].endswith(f"-- {c}\n")
    assert experiments.with_canary("data.json", "{}", c) == ("{}", False)  # JSON takes no comment
    assert experiments.with_canary("tests/test_x.py", "x", None) == ("x", False)
    n = experiments.new_canary()
    assert n.startswith(experiments.CANARY_PREFIX) and n != experiments.new_canary()
    experiments.Grader.model_validate({"command": "x", "canary": n})
    for bad in ("short", "has space in it here", "x" * 300):
        with pytest.raises(ValueError):
            experiments.Grader.model_validate({"command": "x", "canary": bad})


def test_the_canary_is_masked_everywhere_it_could_leave():
    c = "HANGAR-CANARY-aaaa-bbbb-cccc"
    v = {"spec": {"grader": {"canary": c}}, "notes": [f"saw {c.lower()}"], "n": 1, "trace": [{"x": f"..{c}.."}]}
    out = experiments.mask_canary(v, c)
    assert c.lower() not in json.dumps(out).lower() and out["n"] == 1
    assert out["spec"]["grader"]["canary"] == experiments.CANARY_MASK


# ====================================================================== the trial's record
PATCH = """diff --git a/tests/test_core.py b/tests/test_core.py
--- a/tests/test_core.py
+++ b/tests/test_core.py
@@ -1 +1 @@
-assert x
+assert True
diff --git a/tests/test_new.py b/tests/test_new.py
new file mode 100644
--- /dev/null
+++ b/tests/test_new.py
@@ -0,0 +1 @@
+x
diff --git a/src/app.py b/src/app.py
--- a/src/app.py
+++ b/src/app.py
@@ -1 +1 @@
-a
+b
"""


def test_touched_tests_come_from_the_job_else_the_patch():
    spec = experiments.ExperimentSpec.model_validate({
        "name": "e", "environment": "plain", "variants": {"a": {}},
        "tasks": [{"id": "t", "instructions": "x"}, {"id": "v", "instructions": "x", "verify": "pytest tests/x.py"}]})
    child = Job(title="t", instructions="x", environment="plain")
    t = experiments.Trial(key="t/a/1", task="t", variant="a", trial=1)
    experiments._touched(spec, t, child, PATCH)
    assert t.touched_tests == ["tests/test_core.py"]  # an existing test changed; a new one is fine
    t2 = experiments.Trial(key="v/a/1", task="v", variant="a", trial=1)
    experiments._touched(spec, t2, child, PATCH)  # the verify names other files
    assert t2.touched_tests == []
    t3 = experiments.Trial(key="t/a/1", task="t", variant="a", trial=1, touched_tests=["tests/a.py"])
    experiments._touched(spec, t3, child, PATCH)  # the job's own check wins
    assert t3.touched_tests == ["tests/a.py"]


def test_the_schema_documents_the_new_fields_additively():
    trial = INDEX_SCHEMA["properties"]["trials"]["items"]
    props, grade = trial["properties"], trial["properties"]["grade"]["properties"]
    for k in ("trust", "task_invalid", "contaminated", "contamination", "touched_tests"):
        assert k in props and k not in trial["required"], k
    for k in ("task_invalid", "isolation", "canary_files"):
        assert k in grade, k
    assert "task_checks" in INDEX_SCHEMA["properties"] and "task_checks" not in INDEX_SCHEMA["required"]
    assert "canary" in json.dumps(INDEX_SCHEMA["properties"]["spec"])
    assert grade["trust"]["enum"] == ["isolated", "best_effort", "low"]


# ====================================================================== the experiment, on the fake provider
def _git_world():
    from tests.conftest import scratch_root

    root = Path(tempfile.mkdtemp(prefix="forgery-", dir=scratch_root()))
    remote, seed = root / "remote.git", root / "seed"
    git = ["git", "-c", "user.email=t@t", "-c", "user.name=t", "-c", "init.defaultBranch=main"]
    subprocess.run([*git, "init", "-q", "--bare", str(remote)], check=True)
    subprocess.run([*git, "init", "-q", str(seed)], check=True)
    (seed / "mod.py").write_text("def add(a, b):\n    return a - b\n")
    subprocess.run([*git, "-C", str(seed), "add", "."], check=True)
    subprocess.run([*git, "-C", str(seed), "commit", "-qm", "init"], check=True)
    subprocess.run([*git, "-C", str(seed), "push", "-q", str(remote), "HEAD:refs/heads/main"], check=True)
    base = subprocess.run(["git", "-C", str(seed), "rev-parse", "HEAD"], capture_output=True,
                          text=True).stdout.strip()
    return remote, base


FIX = ("diff --git a/mod.py b/mod.py\n--- a/mod.py\n+++ b/mod.py\n@@ -1,2 +1,2 @@\n def add(a, b):\n"
       "-    return a - b\n+    return a + b\n")
WRONG = ("diff --git a/mod.py b/mod.py\n--- a/mod.py\n+++ b/mod.py\n@@ -1,2 +1,2 @@\n def add(a, b):\n"
         "-    return a - b\n+    return a * b\n")
HIDDEN = {"test_add.py": "from mod import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n"}


@pytest.fixture
async def grading(fleet, tmp_path, monkeypatch):
    """What _grade needs of a job service, a pinned repo, and the trials' patches and summaries to choose."""
    from hangar import egress, exchange

    if not shutil.which("git"):
        pytest.skip("needs git")
    remote, base = _git_world()
    patches: dict[str, str] = {}

    async def session_patch(svc, s):
        return {"patch": patches.get(s.name, ""), "base": base}

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

    def trial(key: str, patch: str, summary: str | None = None) -> experiments.Trial:
        s = Session(name=key.replace("/", "-"), environment="genv", base_sha=base)
        child = Job(title=key, instructions="x", environment="genv", state=JobState.SUCCEEDED)
        if summary:
            child.result.summary = summary
            s.turns.append(SessionTurn(message="x", run_id="r1", summary=summary))
        children[child.id] = (child, s)
        patches[s.name] = patch
        task, variant, n = key.split("/")
        return experiments.Trial(key=key, task=task, variant=variant, trial=int(n), job=child.id, state="grading",
                                 started_at=1.0)

    def experiment(grader: dict, **task) -> tuple[experiments.ExperimentSpec, experiments.State, Job]:
        spec = experiments.ExperimentSpec.model_validate({
            "name": "e", "environment": "genv", "variants": {"a": {}},
            "tasks": [{"id": "t", "instructions": "x", "repo": f"file://{remote}", **task}],
            "grader": {"files": HIDDEN, "timeout_seconds": 60, **grader}})
        spec.grader.canary = spec.grader.canary or experiments.new_canary()
        st = experiments.State(spec=spec.model_dump(mode="json"), pins={"tasks": {"t": {"repo": f"file://{remote}"}}})
        return spec, st, Job(title="e", instructions="x", environment="genv")

    yield {"jobs": Jobs(), "trial": trial, "experiment": experiment, "base": base}


CMD = f"HANGAR_GRADE_PYTHON={sys.executable} hangar-grade pytest -q"


async def test_the_base_must_fail_and_a_reference_patch_must_pass(grading, fleet):
    jobs, trial, experiment = grading["jobs"], grading["trial"], grading["experiment"]
    spec, st, exp = experiment({"command": CMD}, reference_patch=FIX)
    a, b = trial("t/a/1", FIX), trial("t/a/2", WRONG)
    import asyncio

    await asyncio.gather(experiments._grade(jobs, exp, st, spec, a), experiments._grade(jobs, exp, st, spec, b))
    check = st.task_checks["t"]
    assert check["state"] == "done" and check["invalid"] is False, check
    assert check["base"]["exit_code"] == 1 and check["reference"]["exit_code"] == 0
    graders = [x for x in fleet.list(include_stopped=True) if x.labels.get(experiments.GRADER_LABEL) == exp.id]
    keys = sorted(x.labels[experiments.TRIAL_LABEL] for x in graders)
    assert keys == ["t/a/1", "t/a/2", "t/base/0", "t/reference/0"]  # one check for two trials grading at once
    assert a.grade["exit_code"] == 0 and b.grade["exit_code"] == 1 and "task_invalid" not in a.grade
    assert a.grade["trust"] == "best_effort" and a.grade["canary_files"] == 1  # the fake has no uid split
    # graded again (a restart): the check isn't run again
    await experiments._grade(jobs, exp, st, spec, a)
    assert len([x for x in fleet.list(include_stopped=True)
                if x.labels.get(experiments.GRADER_LABEL) == exp.id]) == 5


async def test_hidden_tests_that_pass_on_base_make_the_task_invalid(grading):
    jobs, trial, experiment = grading["jobs"], grading["trial"], grading["experiment"]
    spec, st, exp = experiment({"command": CMD, "files": {"test_any.py": "def test_ok():\n    assert True\n"}})
    t = trial("t/a/1", FIX)
    await experiments._grade(jobs, exp, st, spec, t)
    assert st.task_checks["t"]["invalid"] is True and st.task_checks["t"]["reason"] == "hidden tests pass on base"
    assert t.grade["task_invalid"] is True and any("invalid_task t" in n for n in st.notes)


async def test_a_reference_patch_that_fails_flags_the_task(grading):
    jobs, trial, experiment = grading["jobs"], grading["trial"], grading["experiment"]
    spec, st, exp = experiment({"command": CMD}, reference_patch=WRONG)
    t = trial("t/a/1", FIX)
    await experiments._grade(jobs, exp, st, spec, t)
    check = st.task_checks["t"]
    assert check["invalid"] is True and check["reason"] == "the reference patch fails the hidden tests"
    assert t.grade["task_invalid"] is True and t.grade["exit_code"] == 0


async def test_a_trial_that_had_the_canary_is_contaminated(grading, fleet):
    """The fixture: the hidden test (with its canary) leaked into the agent's context, and the agent echoed it in
    its summary and copied the test into its patch."""
    jobs, trial, experiment = grading["jobs"], grading["trial"], grading["experiment"]
    spec, st, exp = experiment({"command": CMD})
    canary = spec.grader.canary
    leaked = trial("t/a/1", FIX + "diff --git a/leak.py b/leak.py\nnew file mode 100644\n--- /dev/null\n+++ "
                   f"b/leak.py\n@@ -0,0 +1 @@\n+# {canary}\n", summary=f"Found the tests ({canary}). Fixed add().")
    clean = trial("t/a/2", FIX, summary="Fixed add().")
    await experiments._grade(jobs, exp, st, spec, leaked)
    await experiments._grade(jobs, exp, st, spec, clean)
    assert leaked.contaminated is True and leaked.contamination == ["patch", "result"]
    assert clean.contaminated is False and clean.contamination == []
    assert any("t/a/1 is contaminated" in n for n in st.notes)
    seen = [e for e in fleet.store.audit_tail(200) if e.action == "grade.canary_seen"]
    assert len(seen) == 1 and seen[0].detail["trial"] == "t/a/1" and canary not in json.dumps(seen[0].detail)
    # nothing stored with the trial carries the canary itself
    assert canary not in json.dumps(leaked.model_dump()) and canary not in json.dumps(st.task_checks)


async def test_a_canary_in_the_trace_or_the_egress_records_counts_too(grading, fleet, settings):
    from hangar.models import AuditEvent

    jobs, trial, experiment = grading["jobs"], grading["trial"], grading["experiment"]
    spec, st, exp = experiment({"command": CMD})
    canary = spec.grader.canary
    t = trial("t/a/1", FIX)
    experiments._write(experiments._dir(settings, exp.id, t.key), {"trace.json": {"x": f"... {canary} ..."}})
    t.stored = ["trace.json"]
    child = jobs.get(t.job)
    child.sandbox_id = "sb-agent1"
    fleet.store.audit(AuditEvent(actor="sandbox:sb-agent1", action="egress.deny", target="sb-agent1",
                                 detail={"host": f"{canary.lower()}.attacker.example", "port": 443}))
    await experiments._canary_scan(jobs, exp, st, spec, t, child, FIX)
    assert t.contaminated and t.contamination == ["trace", "egress"]


async def test_the_index_exposes_trust_and_validity_and_hides_the_canary(grading):
    jobs, trial, experiment = grading["jobs"], grading["trial"], grading["experiment"]
    spec, st, exp = experiment({"command": CMD})
    canary = spec.grader.canary
    t = trial("t/a/1", FIX, summary=f"saw {canary}")
    await experiments._grade(jobs, exp, st, spec, t)
    st.trials = [t]
    exp.labels = {experiments.KIND_LABEL: experiments.KIND}
    exp.result.data = st.model_dump(mode="json", by_alias=True)

    class J:
        broker = jobs.broker

    v = experiments.view(J(), exp)  # type: ignore[arg-type]
    jsonschema.validate(v, INDEX_SCHEMA)
    assert canary not in json.dumps(v)
    row = v["trials"][0]
    assert row["trust"] == "best_effort" and row["task_invalid"] is False and row["contaminated"] is True


# ====================================================================== the uid split's wrapper, for real
def _can_switch_users() -> bool:
    try:
        pwd.getpwnam("nobody")
    except KeyError:
        return False
    if os.geteuid() != 0 or not shutil.which("setpriv"):
        return False
    r = subprocess.run(["setpriv", "--reuid=nobody", "--regid=nogroup", "--clear-groups", "true"], capture_output=True)
    return r.returncode == 0


@pytest.mark.skipif(not _can_switch_users(), reason="needs root, setpriv and a nobody user")
def test_the_wrapper_owns_the_grade_and_the_agent_cant_forge_it():
    """The grader's command runs as another user (nobody stands in for the agent) and writes {"pass": true} to every
    path it can, and tries the wrapper's grade.json too; the grade stays the wrapper's. What it sends through
    $HANGAR_GRADE_OUT (a pipe) is what hangar reads."""
    from hangar.driver import LAUNCH

    d = Path(tempfile.mkdtemp(prefix="wrap-", dir="/dev/shm" if Path("/dev/shm").is_dir() else None))
    d.chmod(0o755)  # pytest's tmp_path is under a 0700 directory the other user can't pass
    tmp_path = d
    out = tmp_path / "grade"
    out.mkdir(mode=0o700)
    shared = tmp_path / "shared"
    shared.mkdir(mode=0o755)
    env = shared / "agent.env"
    env.write_text("export HANGAR_GRADE_OUT=/proc/self/fd/198\nexport HANGAR_GRADE_JUNIT=/proc/self/fd/199\n")
    env.chmod(0o644)
    wrapper = tmp_path / "wrapper.py"
    wrapper.write_text(experiments.GRADE_WRAPPER)
    forge = ("id -un; for p in " + f"{out}/grade.json {out}/out {tmp_path}/grade.json /tmp/hangar-grade/out.json; "
             "do printf '{\"pass\": true}' > \"$p\" 2>/dev/null && echo \"wrote $p\" || echo \"refused $p\"; done; "
             "printf '{\"score\": 0.5}' > \"$HANGAR_GRADE_OUT\" && echo sent; "
             "printf '<testsuite tests=\"1\" failures=\"1\"><testcase name=\"t\"><failure/></testcase></testsuite>' "
             "> \"$HANGAR_GRADE_JUNIT\"; exit 3")
    home = tmp_path / "home"
    home.mkdir()
    res = subprocess.run([sys.executable, "-I", str(wrapper), "nobody", str(home), str(env), str(shared), "30",
                          str(out), LAUNCH, forge], capture_output=True, text=True, timeout=60,
                         env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin"})
    assert "hangar-grade-wrapper: done 3" in res.stdout, res.stdout + res.stderr
    grade = json.loads((out / "grade.json").read_text())
    assert grade["exit_code"] == 3 and grade["pipe_owner_ok"] is True
    assert grade["stdout"].splitlines()[0] == "nobody"
    assert "sent" in grade["stdout"] and "wrote" not in grade["stdout"], grade["stdout"]
    assert (out / "out").read_bytes() == b'{"score": 0.5}'
    assert b'failures="1"' in (out / "junit.xml").read_bytes()
    # a command that never ends is stopped at its timeout, and says 124
    res = subprocess.run([sys.executable, "-I", str(wrapper), "nobody", str(home), str(env), str(shared), "1",
                          str(out), LAUNCH, "sleep 30 & sleep 30"], capture_output=True, text=True, timeout=60,
                         env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin"})
    assert json.loads((out / "grade.json").read_text())["exit_code"] == 124, res.stdout + res.stderr
    shutil.rmtree(d, ignore_errors=True)
