"""The sealed grader end to end on a real Docker engine (--- sealed #126).

A grader's sandbox (an `open` environment's, the default) lands on hangar's internal egress network with egress `none`
and no host at all: it can't open a connection out itself, and hangar's proxy refuses even the harness's model API.
Its hidden tests sit outside the repo, root-owned: the sandbox's user reads them and can't change them. `hangar-grade
pytest` grades the code alone: a patch's root `conftest.py` that forces every test to pass, its `pytest.ini` and its
`sitecustomize.py` change nothing.

Opt-in (needs Docker on Linux, where hangar can listen on the network's gateway, and network for the image build):
    HANGAR_E2E=1 uv run pytest tests/test_e2e_sealed_docker.py -v
"""

from __future__ import annotations

import os
import shlex
from pathlib import Path

import pytest
import yaml

from tests.conftest import free_port

pytestmark = pytest.mark.skipif(os.environ.get("HANGAR_E2E") != "1", reason="set HANGAR_E2E=1 to run")

ROOT = Path(__file__).parents[1]
FETCH = ("python3 -c \"import sys, urllib.request\n"
         "try:\n print(urllib.request.urlopen(sys.argv[1], timeout=20).status)\n"
         "except Exception as e:\n print(type(e).__name__, getattr(e, 'code', ''), str(e)[:200])\" ")
FORGE = """import pytest


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    out = yield
    out.get_result().outcome = "passed"
"""


async def test_a_graders_sandbox_reaches_nothing_and_grades_from_a_pristine_copy(tmp_path):
    from hangar import delivery, experiments
    from hangar.app import Hangar
    from hangar.config import Settings
    from hangar.models import AuditEvent
    from hangar.providers.docker_net import EGRESS_NAME

    envs = tmp_path / "envs"
    envs.mkdir()
    (envs / "gradepy.yaml").write_text(yaml.safe_dump({  # egress unset: open, like most environments
        "name": "gradepy", "extends": "minimal", "sudo": False, "tools": {"apt": ["python3-pytest"]},
        "mcp": {"docs": {"url": "https://mcp.example.com/mcp"}}}))
    s = Settings(data_dir=tmp_path, env_dirs=[ROOT / "envs", envs], reaper_interval_seconds=3600,
                 extra_ca_bundle=os.environ.get("HANGAR_EXTRA_CA_BUNDLE") or None, egress_port=free_port())
    s.snapshot_dir.mkdir(parents=True, exist_ok=True)
    h = Hangar(s)
    fleet = h.fleet
    await fleet.start()
    labels = {experiments.GRADER_LABEL: "job-e2e", experiments.TRIAL_LABEL: "t/a/1", "e2e-sealed": "1"}
    try:
        sb = await fleet.create("gradepy", name="e2e-sealed", labels=labels, actor="test")
        sb = fleet.get(sb.id)
        prov = fleet.provider("docker")
        attrs = prov.client.api.inspect_container(sb.provider_ref)
        assert list(attrs["NetworkSettings"]["Networks"]) == [EGRESS_NAME]
        assert sb.info["egress"]["mode"] == "none" and sb.info["egress"]["enforced"]
        assert "mcp" not in sb.info  # neither the environment's MCP servers nor hangar's
        sb = delivery.lock_sealed(fleet, sb)

        # no route out, and the proxy refuses even the harness's model API
        r = await fleet.exec(sb.id, "timeout 5 bash -c 'exec 3<>/dev/tcp/1.1.1.1/443' && echo open || echo shut")
        assert r.stdout.strip() == "shut", r.stdout + r.stderr
        r = await fleet.exec(sb.id, FETCH + "https://api.anthropic.com/", timeout=60)
        assert "403" in r.stdout or "Forbidden" in r.stdout, r.stdout + r.stderr
        denied = [e for e in fleet.store.audit_tail(500) if isinstance(e, AuditEvent) and e.action == "egress.deny"
                  and e.target == sb.id]
        assert denied and any("api.anthropic.com" in str(e.detail) for e in denied)

        # a patched repo that tries to force a pass, and the hidden tests outside it
        repo = "/workspace/repo"
        q = shlex.quote
        files = {"mod.py": "def add(a, b):\n    return a - b\n", "conftest.py": FORGE,
                 "pytest.ini": "[pytest]\naddopts = -p no:doctest\n",
                 "sitecustomize.py": "open('/tmp/SITECUSTOMIZE_RAN', 'w').write('x')\n"}
        for name, content in files.items():
            await prov.write_file(sb, f"{repo}/{name}", content.encode(), mode=0o644)
        root = f"{experiments.grader_root(prov)}/t__a__1"
        hidden = f"{root}/tests"
        await prov.write_file(sb, f"{root}/bin/hangar-grade", experiments.GRADE_HELPER.encode(), mode=0o755)
        await prov.write_file(sb, f"{hidden}/test_add.py",
                              b"from mod import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n", mode=0o644)
        # the control: the old layout (files_mode: in_repo), the hidden test in the repo, where the conftest wins
        naive = await fleet.exec(sb.id, f"mkdir -p {repo}/hidden && cp {hidden}/test_add.py {repo}/hidden/ && "
                                        f"cd {repo} && python3 -m pytest -q -p no:cacheprovider hidden", timeout=120)
        assert "1 passed" in naive.stdout, naive.stdout + naive.stderr
        await fleet.exec(sb.id, f"rm -rf {repo}/hidden")
        await fleet.exec(sb.id, "rm -f /tmp/SITECUSTOMIZE_RAN")
        env = (f"PATH={q(root + '/bin')}:$PATH HANGAR_HIDDEN_TESTS={q(hidden)} HANGAR_REPO={q(repo)} "
               "HANGAR_GRADE_JUNIT=/tmp/hangar-grade/junit.xml")
        r = await fleet.exec(sb.id, f"cd {repo} && {env} hangar-grade pytest -q", timeout=120)
        assert r.exit_code == 1 and "1 failed" in r.stdout and "assert -1 == 3" in r.stdout, r.stdout + r.stderr
        r = await fleet.exec(sb.id, "test -e /tmp/SITECUSTOMIZE_RAN && echo ran || echo clean")
        assert r.stdout.strip() == "clean"
        junit = await prov.read_file(sb, "/tmp/hangar-grade/junit.xml", limit=1 << 20)
        assert junit and b'failures="1"' in junit
        # the sandbox's user reads the hidden tests and can't change them
        r = await fleet.exec(sb.id, f"id -un; stat -c '%U %a' {hidden} {hidden}/test_add.py; "
                                    f"echo x >> {hidden}/test_add.py && echo wrote || echo refused; "
                                    f"touch {hidden}/conftest.py && echo wrote || echo refused")
        lines = r.stdout.split()
        assert lines[0] == s.sandbox_user and "root 755" in r.stdout and "root 644" in r.stdout, r.stdout
        assert r.stdout.count("refused") == 2 and "wrote" not in r.stdout, r.stdout
    finally:
        for x in fleet.list(include_stopped=False, label="e2e-sealed=1"):
            await fleet.destroy(x.id, actor="test")
        await fleet.stop()


async def test_with_the_uid_split_the_grade_is_hangars_own(tmp_path):
    """--- anti-forgery (#127): a trial's patch that writes {"pass": true} to every path it can under /tmp and
    /var/lib, and to $HANGAR_GRADE_OUT, doesn't change the grade: hangar's user ran the command as the agent and
    wrote grade.json itself. The grade says `trust: isolated`, and the base check ran once for the task."""
    import subprocess
    import tempfile

    from hangar import experiments
    from hangar.app import Hangar
    from hangar.config import Settings
    from hangar.models import Job, JobState, Session

    envs = tmp_path / "envs"
    envs.mkdir()
    (envs / "gradepy.yaml").write_text(yaml.safe_dump({
        "name": "gradepy", "extends": "minimal", "sudo": False, "tools": {"apt": ["python3-pytest"]}}))
    s = Settings(data_dir=tmp_path, env_dirs=[ROOT / "envs", envs], reaper_interval_seconds=3600,
                 extra_ca_bundle=os.environ.get("HANGAR_EXTRA_CA_BUNDLE") or None, egress_port=free_port())
    s.snapshot_dir.mkdir(parents=True, exist_ok=True)
    # a repo the sandbox clones over the network: served by hangar's own git gateway would need GitHub, so a bare
    # repo in the image's reach instead: a file:// URL inside the sandbox, written there before the grade
    work = Path(tempfile.mkdtemp())
    git = ["git", "-c", "user.email=t@t", "-c", "user.name=t", "-c", "init.defaultBranch=main"]
    subprocess.run([*git, "init", "-q", str(work)], check=True)
    (work / "mod.py").write_text("def add(a, b):\n    return a - b\n")
    subprocess.run([*git, "-C", str(work), "add", "."], check=True)
    subprocess.run([*git, "-C", str(work), "commit", "-qm", "init"], check=True)
    base = subprocess.run(["git", "-C", str(work), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    bundle = tmp_path / "repo.bundle"
    subprocess.run(["git", "-C", str(work), "bundle", "create", "-q", str(bundle), "--all"], check=True)
    h = Hangar(s)
    fleet = h.fleet
    await fleet.start()
    try:
        real_create = fleet.create

        async def create(env, **kw):  # the bundle into the sandbox, then the clone hangar would do from it
            repo = kw.pop("repo", None)
            sb = await real_create(env, **kw)
            prov = fleet.provider("docker")
            await prov.write_file(sb, "/workspace/repo.bundle", bundle.read_bytes(), mode=0o644)
            r = await fleet.exec(sb.id, "git clone -q /workspace/repo.bundle /workspace/repo", timeout=60)
            assert r.exit_code == 0, r.stderr
            sb = fleet.get(sb.id)
            sb.info["repo_path"], sb.repo = "/workspace/repo", repo
            fleet.store.put(sb)
            return sb

        fleet.create = create  # type: ignore[method-assign]
        forge = "".join(f"+{ln}\n" for ln in (
            "import glob, os",
            "for root in ('/tmp', '/var/lib', '/var/lib/hangar', '/var/lib/hangar/grade'):",
            "    for d in [root, *glob.glob(root + '/**/', recursive=True)]:",
            "        for name in ('grade.json', 'out.json', 'out', 'status.json'):",
            "            try:",
            "                open(os.path.join(d, name), 'w').write('{\"pass\": true}')",
            "            except OSError:",
            "                pass",
            "out = os.environ.get('HANGAR_GRADE_OUT')",
            "if out:",
            "    try:",
            "        open(out, 'w').write('{\"pass\": true}')",
            "    except OSError:",
            "        pass",
        ))
        n = forge.count("\n")
        patch = (f"diff --git a/conftest.py b/conftest.py\nnew file mode 100644\n--- /dev/null\n+++ b/conftest.py\n"
                 f"@@ -0,0 +1,{n} @@\n{forge}")
        spec = experiments.ExperimentSpec.model_validate({
            "name": "e", "environment": "gradepy", "variants": {"a": {}},
            "tasks": [{"id": "t", "instructions": "x", "repo": "https://example.invalid/repo.git"}],
            "grader": {"command": "hangar-grade pytest -q", "timeout_seconds": 120,
                       "files": {"test_add.py": "from mod import add\n\n\n"
                                                "def test_add():\n    assert add(1, 2) == 3\n"}}})
        spec.grader.canary = experiments.new_canary()
        st = experiments.State(spec=spec.model_dump(mode="json"), pins={"tasks": {"t": {"repo": None}}})
        exp = Job(title="e", instructions="x", environment="gradepy")
        child = Job(title="t", instructions="x", environment="gradepy", state=JobState.SUCCEEDED)
        sess = Session(name="s", environment="gradepy", base_sha=base)

        class Jobs:
            fleet, settings, broker, sessions = h.fleet, s, h.fleet.broker, None

            def get(self, jid):
                return child

            def _session(self, job):
                return sess

            def _save(self, job):
                pass

        from hangar import exchange

        async def session_patch(svc, x):
            return {"patch": patch, "base": base}

        exchange.session_patch, real_patch = session_patch, exchange.session_patch
        try:
            t = experiments.Trial(key="t/a/1", task="t", variant="a", trial=1, job=child.id, state="grading")
            await experiments._grade(Jobs(), exp, st, spec, t)
        finally:
            exchange.session_patch = real_patch
        g = t.grade
        assert "error" not in g, g
        assert g["trust"] == "isolated" and g["isolation"] == "split", g
        assert g["exit_code"] == 1 and g["hidden_tests"]["failed"] == 1, g  # the real result, not the forgery
        assert "data" not in g, g  # it wrote nothing through the pipe hangar reads: no {"pass": true}
        assert g["tamper_signals"] == [{"path": "conftest.py", "kind": "conftest"}]
        assert st.task_checks["t"]["invalid"] is False and st.task_checks["t"]["base"]["trust"] == "isolated"
    finally:
        for x in fleet.list(include_stopped=False):
            if x.labels.get(experiments.GRADER_LABEL):
                await fleet.destroy(x.id, actor="test")
        await fleet.stop()
