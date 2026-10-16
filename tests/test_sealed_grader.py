"""The sealed grader (--- sealed #126): an experiment's grader runs agent-written code beside hidden tests, so its
sandbox reaches no host at all (egress `none` without even the harness's hosts), holds none of the environment's MCP
servers, computer or services, and grades from a pristine copy of the hidden tests that nothing the patch adds
(`conftest.py`, pytest settings, `sitecustomize.py`, a plugin) can load before.

The safe runner is exercised for real here (local Python and pytest, a fixture repo whose patch forges a pass); the
Docker end to end is tests/test_e2e_sealed_docker.py."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import jsonschema
import pytest

from hangar import delivery, egress, experiments
from hangar.config import Settings
from hangar.envspec import EnvSpec
from hangar.models import Sandbox

ROOT = Path(__file__).resolve().parents[1]
INDEX_SCHEMA = json.loads((ROOT / "docs/api/experiment.schema.json").read_text())
GRADER = {experiments.GRADER_LABEL: "job-exp", experiments.TRIAL_LABEL: "t/a/1"}


def _grader(**info) -> Sandbox:
    return Sandbox(name="g", environment="api", provider="docker", labels=dict(GRADER), info=info)


# ====================================================================== the policy
def test_a_sealed_sandbox_reaches_no_host_at_all():
    """`none` normally keeps the harness's hosts (an agent must reach its model); a grader runs no agent, so it gets
    nothing: no harness, MCP or cloud host and no approval, whatever the environment and the repo policy say."""
    env = EnvSpec(name="api", harnesses=["claude-code"], egress={"mode": "allowlist", "allow": ["docs.python.org"]},
                  mcp={"docs": {"url": "https://mcp.example.com/mcp"}})
    pol = egress.resolve(Settings(), env, egress.EgressSpec(mode="allowlist", allow=["x.example"]), "claude-code",
                         approved=[{"rule": "example.com", "request": "rq-1"}], sealed=True)
    assert pol.mode == "none" and pol.proxied and pol.enforced and pol.rules == () and pol.mirrors == ()
    for host in ("api.anthropic.com", "claude.ai", "mcp.example.com", "docs.python.org", "x.example", "example.com",
                 "pypi.org", "github.com"):
        assert pol.allows(host, 443) is None, host
    # an open environment too: the grader is on the proxy-only network anyway
    pol = egress.resolve(Settings(), EnvSpec(name="api", harnesses=["claude-code"]), harness="claude-code",
                         sealed=True)
    assert pol.mode == "none" and pol.proxied and pol.allows("api.anthropic.com", 443) is None
    assert pol.view()["allow"] == [] and pol.view()["mode"] == "none"


def test_network_mirror_adds_only_the_read_only_package_mirror():
    on = Settings(gateway_url="http://hangar-gateway.hangar.svc:8788")
    pol = egress.resolve(on, EnvSpec(name="api"), sealed=True, mirror=True)
    assert pol.mirrors == ("pypi", "npm", "go") and pol.rules == ()
    assert pol.allows("pypi.org", 443) is None and pol.allows("registry.npmjs.org", 443) is None
    off = egress.resolve(Settings(), EnvSpec(name="api"), sealed=True, mirror=True)  # no gateway: no mirror
    assert off.mirrors == () and any("package mirror" in n for n in off.notes)


def test_for_sandbox_seals_graders_and_nothing_else(fleet, tmp_path):
    """The egress proxy and the package mirror read a sandbox's policy through for_sandbox: that is what enforces."""
    fleet.catalog.add({"name": "api", "harnesses": ["claude-code"], "egress": {"mode": "allowlist"}},
                      save_dir=tmp_path)
    plain = Sandbox(name="p", environment="api", provider="docker", info={"harness": "claude-code"})
    assert egress.for_sandbox(fleet, plain).allows("api.anthropic.com", 443)
    g = _grader(harness="claude-code")
    assert delivery.sealed(g) and not delivery.sealed(plain)
    assert egress.for_sandbox(fleet, g).rules == ()
    egress.approve(fleet.store, "environment", "api", "example.com", None, "admin")  # approvals don't reach it
    assert egress.for_sandbox(fleet, plain).allows("example.com", 443)
    assert egress.for_sandbox(fleet, g).allows("example.com", 443) is None
    g.labels[delivery.NETWORK_LABEL] = "mirror"
    fleet.settings.gateway_url = "http://gw.example:8788"
    assert egress.for_sandbox(fleet, g).mirrors == ("pypi", "npm", "go")
    assert not delivery.sealed_mirror(plain)


def test_while_it_is_set_up_without_the_gateway_only_its_repos_host(fleet, tmp_path):
    """Without the git gateway the clone goes through the proxy: the repo's host only, and only until the grader
    locks the sandbox, before the code it grades runs."""
    fleet.catalog.add({"name": "api", "harnesses": ["claude-code"]}, save_dir=tmp_path)
    g = _grader()
    g.repo = "https://github.com/acme/app.git"
    assert delivery.sealed_setup_hosts(g) == ("github.com",)
    pol = egress.for_sandbox(fleet, g)
    assert pol.allows("github.com", 443) and pol.allows("api.github.com", 443) is None
    g.info["git"] = {"mode": "gateway", "repo": "acme/app"}  # through the gateway it needs no host
    assert delivery.sealed_setup_hosts(g) == ()
    g.info.pop("git")
    g.info["sealed"] = {"locked": True}
    assert delivery.sealed_setup_hosts(g) == () and egress.for_sandbox(fleet, g).rules == ()
    g.repo = "file:///srv/repo.git"
    g.info.pop("sealed")
    assert delivery.sealed_setup_hosts(g) == ()


def test_seal_takes_mcp_computer_services_secrets_cloud_and_the_warm_pool():
    env = EnvSpec.model_validate({
        "name": "full", "harnesses": ["claude-code"], "mcp": {"docs": {"url": "https://mcp.example.com/mcp"}},
        "computer": True, "services": {"db": {"image": "postgres:17", "port": 5432}}, "secrets": {"DB": "db"},
        "warm_pool": 2,
        "egress": {"mode": "allowlist", "allow": ["example.com"]}})
    g = _grader()
    s = delivery.seal(env, g)
    assert s.mcp == {} and s.computer is None and s.services == {} and s.secrets == {} and s.cloud is None
    assert s.warm_pool == 0 and s.egress.mode == "none" and s.egress.allow == [] and s.egress.presets == []
    assert egress.proxied(Settings(), s)  # the provider puts it on the proxy-only network
    assert s.name == env.name  # the image is still the environment's
    p = delivery.provider_spec(s, g)
    assert p.name == "full-sealed" and p.warm_pool == 0  # a Kubernetes template of its own
    plain = Sandbox(name="x", environment="full", provider="docker")
    assert delivery.seal(env, plain) is env and delivery.provider_spec(env, plain) is env


async def test_a_graders_sandbox_starts_sealed(fleet, tmp_path, monkeypatch):
    """fleet.create with the grader's label: proxied with no host, no MCP config written, no services, no secrets,
    no build caches or GitHub API, no repo config started, no warm pool refill."""
    from hangar import envbuilds
    from hangar.models import CredentialKind

    fleet.broker.put("db", "custom", CredentialKind.GENERIC, "db_SECRETvalue", env_var="UNUSED")
    fleet.catalog.add({"name": "full", "harnesses": ["claude-code"], "secrets": {"DB_PASSWORD": "db"},
                       "mcp": {"docs": {"url": "https://mcp.example.com/mcp", "headers": {"X": "y"}}},
                       "services": {"db": {"image": "postgres:17", "port": 5432}}, "warm_pool": 1}, save_dir=tmp_path)
    proxied: list[str] = []

    async def proxy_url(fleet_, prov, sb):
        proxied.append(sb.id)
        return "http://127.0.0.1:9"

    monkeypatch.setattr(egress, "_proxy_url", proxy_url)
    refills: list = []

    async def refill(*a):
        refills.append(a)

    monkeypatch.setattr(fleet, "_refill", refill)
    after: list = []

    async def after_setup(self, prov, sb, spec, harness, stage=None):
        after.append(sb.id)

    monkeypatch.setattr(envbuilds.EnvBuilds, "after_setup", after_setup)
    sb = await fleet.create("full", labels=dict(GRADER))
    box = fleet.providers["docker"].boxes[sb.provider_ref]
    sb = fleet.get(sb.id)
    assert proxied == [sb.id] and sb.info["egress"]["mode"] == "none"
    assert "mcp" not in sb.info and not sb.info.get("service_specs")
    env_file = (box / "home/agent/.hangar/env").read_text()
    assert "DB_PASSWORD" not in env_file and "db_SECRETvalue" not in env_file
    assert not (box / "home/agent/.claude.json").exists() or "mcp.example.com" not in \
        (box / "home/agent/.claude.json").read_text()
    assert refills == []
    # an ordinary sandbox of the same environment keeps all of it
    plain = await fleet.create("full")
    assert "docs" in fleet.get(plain.id).info.get("mcp", [])


# ====================================================================== the spec
def test_files_mode_and_network_default_to_pristine_and_none():
    g = experiments.Grader.model_validate({"command": "hangar-grade pytest"})
    assert g.files_mode == "pristine" and g.network == "none"
    old = experiments.ExperimentSpec.model_validate({  # a spec from before: still valid, now pristine
        "name": "e", "environment": "plain", "tasks": [{"id": "t", "instructions": "x"}], "variants": {"a": {}},
        "grader": {"command": "pytest -q tests/hidden", "files": {"tests/hidden/test_x.py": "def test(): pass"}}})
    assert old.grader.files_mode == "pristine"
    with pytest.raises(ValueError):
        experiments.Grader.model_validate({"command": "x", "network": "open"})
    with pytest.raises(ValueError):
        experiments.Grader.model_validate({"command": "x", "files_mode": "both"})
    for bad in ("../x.py", "/etc/x", "a/../../b", "a//b", "./a"):
        with pytest.raises(ValueError):
            experiments.Grader.model_validate({"command": "x", "files": {bad: ""}})


def test_the_schema_documents_the_new_grade_fields_additively():
    trial = INDEX_SCHEMA["properties"]["trials"]["items"]
    grade = trial["properties"]["grade"]["properties"]
    for k in ("files_mode", "network", "trust", "tamper_signals", "hidden_tests", "runner"):
        assert k in grade, k
    assert "grade" in trial["required"] and not set(grade) & set(trial["required"])
    v = {"schema": experiments.SCHEMA, "id": "job-1", "name": "e", "state": "succeeded", "spec": {}, "pins": {},
         "counts": {}, "notes": [], "trials": [{
             "key": "t/a/1", "task": "t", "variant": "a", "trial": 1, "job": None, "state": "done", "manifests": [],
             "config_digest": None, "verify": None, "stored": [], "drift": [], "links": {},
             "grade": {"command": "x", "exit_code": 0, "files_mode": "pristine", "network": "none",
                       "trust": "best_effort", "runner": "hangar-grade",
                       "tamper_signals": [{"path": "conftest.py", "kind": "conftest"}],
                       "hidden_tests": {"passed": 1, "failed": 0, "error": 0, "skipped": 0, "total": 1}}}]}
    jsonschema.validate(v, INDEX_SCHEMA)
    v["trials"][0]["grade"] = {"command": "x"}  # a grade from before
    jsonschema.validate(v, INDEX_SCHEMA)


# ====================================================================== tamper signals
PATCH = """diff --git a/conftest.py b/conftest.py
new file mode 100644
--- /dev/null
+++ b/conftest.py
@@ -0,0 +1 @@
+x = 1
diff --git a/pkg/sitecustomize.py b/pkg/sitecustomize.py
new file mode 100644
--- /dev/null
+++ b/pkg/sitecustomize.py
@@ -0,0 +1 @@
+x = 1
diff --git a/hook.pth b/hook.pth
new file mode 100644
--- /dev/null
+++ b/hook.pth
@@ -0,0 +1 @@
+import os
diff --git a/pyproject.toml b/pyproject.toml
--- a/pyproject.toml
+++ b/pyproject.toml
@@ -1 +1,2 @@
+[tool.pytest.ini_options]
diff --git a/setup.cfg b/setup.cfg
--- a/setup.cfg
+++ b/setup.cfg
@@ -1 +1,2 @@
+[metadata]
diff --git a/old/conftest.py b/old/conftest.py
deleted file mode 100644
--- a/old/conftest.py
+++ /dev/null
@@ -1 +0,0 @@
-x = 1
diff --git a/src/app.py b/src/app.py
--- a/src/app.py
+++ b/src/app.py
@@ -1 +1 @@
-a
+b
"""


def test_the_patchs_files_that_can_hook_a_test_run_are_signals():
    assert experiments.patch_files(PATCH) == ["conftest.py", "pkg/sitecustomize.py", "hook.pth", "pyproject.toml",
                                             "setup.cfg", "src/app.py"]  # a deleted file isn't one it adds
    hits, configs = experiments.tamper_candidates(PATCH)
    assert hits == [{"path": "conftest.py", "kind": "conftest"},
                    {"path": "pkg/sitecustomize.py", "kind": "sitecustomize"}, {"path": "hook.pth", "kind": "pth"}]
    assert configs == ["pyproject.toml", "setup.cfg"]  # decided by their content, read after the patch is applied
    assert experiments.tamper_config("pyproject.toml", "[project]\n[tool.pytest.ini_options]\naddopts='-x'\n")
    assert experiments.tamper_config("setup.cfg", "[metadata]\nname = x\n") is None
    assert experiments.tamper_config("setup.cfg", "[tool:pytest]\naddopts = -x\n")
    assert experiments.tamper_config("tox.ini", "[pytest]\n")
    assert experiments.tamper_config("pyproject.toml", "[tool.ruff]\n") is None


# ====================================================================== the safe runner, for real
FORGE_CONFTEST = """import pytest


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    out = yield
    out.get_result().outcome = "passed"
"""


@pytest.fixture
def forged(tmp_path):
    """A repo whose code is wrong and whose patch tries every way to make the hidden test pass anyway, and the
    hidden tests (with a conftest of their own, whose fixture must still work)."""
    repo, hidden = tmp_path / "repo", tmp_path / "hidden"
    repo.mkdir()
    hidden.mkdir()
    (repo / "mod.py").write_text("def add(a, b):\n    return a - b\n")
    (repo / "conftest.py").write_text(FORGE_CONFTEST)  # forces every test to pass
    (repo / "pytest.ini").write_text("[pytest]\naddopts = -p forgeplug\n")
    (repo / "forgeplug.py").write_text(FORGE_CONFTEST)
    marker = tmp_path / "SITECUSTOMIZE_RAN"
    (repo / "sitecustomize.py").write_text(f"open({str(marker)!r}, 'w').write('x')\n")
    dist = repo / "forge-1.0.dist-info"  # a pytest plugin declared by an entry point, found on sys.path
    dist.mkdir()
    (dist / "METADATA").write_text("Metadata-Version: 2.1\nName: forge\nVersion: 1.0\n")
    (dist / "entry_points.txt").write_text("[pytest11]\nforge = forgeplug\n")
    (hidden / "conftest.py").write_text("import pytest\n\n\n@pytest.fixture\ndef three():\n    return 3\n")
    (hidden / "test_add.py").write_text("from mod import add\n\n\n"
                                        "def test_add(three):\n    assert add(1, 2) == three\n")
    helper = tmp_path / "bin" / "hangar-grade"
    helper.parent.mkdir()
    helper.write_text(experiments.GRADE_HELPER)
    helper.chmod(0o755)
    return {"repo": repo, "hidden": hidden, "helper": helper, "marker": marker, "junit": tmp_path / "junit.xml"}


def _env(w, **extra) -> dict[str, str]:
    return {"PATH": f"{w['helper'].parent}:/usr/bin:/bin", "HOME": str(w["repo"].parent),
            "HANGAR_HIDDEN_TESTS": str(w["hidden"]), "HANGAR_REPO": str(w["repo"]),
            "HANGAR_GRADE_JUNIT": str(w["junit"]), "HANGAR_GRADE_PYTHON": sys.executable,
            "PYTEST_ADDOPTS": "-p forgeplug", "PYTHONPATH": str(w["repo"]), **extra}


def test_the_forgery_works_without_the_safe_runner(forged):
    """The control: run naively from the repo, the patch makes the wrong code pass."""
    res = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(forged["hidden"])],
                         cwd=forged["repo"], env={**os.environ, "PYTHONPATH": str(forged["repo"])},
                         capture_output=True, text=True, timeout=120)
    assert res.returncode == 0 and "passed" in res.stdout, res.stdout + res.stderr
    assert forged["marker"].exists()  # sitecustomize.py ran


def test_hangar_grade_pytest_ignores_everything_the_patch_added(forged):
    res = subprocess.run(["hangar-grade", "pytest", "-q"], cwd=forged["repo"], env=_env(forged),
                         capture_output=True, text=True, timeout=120)
    out = res.stdout + res.stderr
    assert res.returncode == 1 and "1 failed" in out and "assert -1 == 3" in out, out
    assert not forged["marker"].exists(), "sitecustomize.py ran under python -I"
    from hangar import testreports

    r = testreports.parse_junit(forged["junit"].read_bytes())
    assert [(c.name, c.outcome) for c in r.cases] == [("test_add", "failed")]
    # the right code passes, with the hidden tests' own fixture
    (forged["repo"] / "mod.py").write_text("def add(a, b):\n    return a + b\n")
    res = subprocess.run(["hangar-grade", "pytest", "-q"], cwd=forged["repo"], env=_env(forged),
                         capture_output=True, text=True, timeout=120)
    assert res.returncode == 0 and "1 passed" in res.stdout, res.stdout + res.stderr
    assert not list(forged["repo"].rglob("__pycache__")) and not (forged["hidden"] / ".pytest_cache").exists()


def test_hangar_grade_refuses_what_it_doesnt_run(forged):
    res = subprocess.run(["hangar-grade", "jest"], env=_env(forged), capture_output=True, text=True, timeout=30)
    assert res.returncode == 2 and "usage" in res.stderr
    res = subprocess.run(["hangar-grade", "pytest"], env=_env(forged, HANGAR_HIDDEN_TESTS="/nonexistent"),
                         capture_output=True, text=True, timeout=30)
    assert res.returncode == 2 and "HANGAR_HIDDEN_TESTS" in res.stderr


# ====================================================================== the grader, on the fake provider
async def test_a_grade_runs_from_the_pristine_copy_and_records_its_signals(fleet, tmp_path, monkeypatch):
    """`_grade` on a trial whose patch adds a forging conftest.py: the hidden tests are written outside the repo and
    pass or fail on the code alone; the conftest is a tamper signal; in_repo keeps the old layout, as `trust: low`."""
    import shutil
    import tempfile

    from hangar import exchange
    from hangar.models import Job, JobState, Session
    from tests.conftest import scratch_root

    if not shutil.which("git"):
        pytest.skip("needs git")
    root = Path(tempfile.mkdtemp(prefix="sealed-", dir=scratch_root()))  # no /tmp/ in it: the fake remaps that
    remote = root / "remote.git"
    seed = root / "seed"
    git = ["git", "-c", "user.email=t@t", "-c", "user.name=t", "-c", "init.defaultBranch=main"]
    subprocess.run([*git, "init", "-q", "--bare", str(remote)], check=True)
    subprocess.run([*git, "init", "-q", str(seed)], check=True)
    (seed / "mod.py").write_text("def add(a, b):\n    return a - b\n")
    subprocess.run([*git, "-C", str(seed), "add", "."], check=True)
    subprocess.run([*git, "-C", str(seed), "commit", "-qm", "init"], check=True)
    subprocess.run([*git, "-C", str(seed), "push", "-q", str(remote), "HEAD:refs/heads/main"], check=True)
    base = subprocess.run(["git", "-C", str(seed), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    patch = ("diff --git a/conftest.py b/conftest.py\nnew file mode 100644\n--- /dev/null\n+++ b/conftest.py\n"
             "@@ -0,0 +1,7 @@\n" + "".join("+" + ln + "\n" for ln in FORGE_CONFTEST.splitlines()))

    async def session_patch(svc, s):
        return {"patch": patch, "base": base}

    monkeypatch.setattr(exchange, "session_patch", session_patch)

    async def proxy_url(fleet_, prov, sb):
        return "http://127.0.0.1:9"

    monkeypatch.setattr(egress, "_proxy_url", proxy_url)
    fleet.catalog.add({"name": "genv", "harnesses": ["git"]}, save_dir=tmp_path)
    py = sys.executable
    cmd = (f"HANGAR_GRADE_PYTHON={py} hangar-grade pytest -q; rc=$?; "
           "test ! -e test_add.py && printf '{\"rc\": %s}' $rc > \"$HANGAR_GRADE_OUT\"; exit $rc")
    hidden = {"test_add.py": "from mod import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n"}
    spec = experiments.ExperimentSpec.model_validate({
        "name": "e", "environment": "genv", "tasks": [{"id": "t", "instructions": "x", "repo": f"file://{remote}"}],
        "variants": {"a": {}}, "grader": {"command": cmd, "files": hidden, "timeout_seconds": 60}})
    s = Session(name="s", environment="genv", base_sha=base)
    child = Job(title="t", instructions="x", environment="genv", state=JobState.SUCCEEDED)
    exp = Job(title="e", instructions="x", environment="genv")

    class Jobs:  # what _grade uses of the job service
        def __init__(self):
            self.fleet, self.settings, self.broker, self.sessions = fleet, fleet.settings, fleet.broker, None

        def get(self, jid):
            return child

        def _session(self, job):
            return s

        def _save(self, job):
            pass

    st = experiments.State(spec=spec.model_dump(mode="json"), pins={"tasks": {"t": {"repo": f"file://{remote}"}}})
    t = experiments.Trial(key="t/a/1", task="t", variant="a", trial=1, job=child.id, state="grading")
    await experiments._grade(Jobs(), exp, st, spec, t)
    g = t.grade
    assert "error" not in g, g
    assert g["applied"] is True and g["files_mode"] == "pristine" and g["network"] == "none"
    assert g["trust"] == "best_effort" and g["runner"] == "hangar-grade"
    assert g["tamper_signals"] == [{"path": "conftest.py", "kind": "conftest"}]
    assert g["exit_code"] == 1 and g["data"] == {"rc": 1}, g  # the forging conftest didn't make it pass
    assert g["hidden_tests"]["failed"] == 1 and g["hidden_tests"]["total"] == 1
    graders = [x for x in fleet.list(include_stopped=True) if x.labels.get(experiments.GRADER_LABEL) == exp.id]
    # the trial's grader, and (#127) the task's base check before it: both sealed and locked
    assert len(graders) == 2 and all(x.info["sealed"] == {"locked": True} for x in graders)
    assert all(fleet.gateway.grants(x.id) == [] for x in graders)
    assert st.task_checks["t"]["invalid"] is False  # the hidden tests fail on the base, as they should
    # in_repo: the hidden tests in the repo (the old layout), so the forging conftest wins, and the grade says how
    # far to trust it
    spec.grader = spec.grader.model_copy(update={"files_mode": "in_repo", "command": f"{py} -m pytest -q -p "
                                                 "no:cacheprovider test_add.py"})
    st.spec = spec.model_dump(mode="json")
    await experiments._grade(Jobs(), exp, st, spec, t)
    assert t.grade["files_mode"] == "in_repo" and t.grade["trust"] == "low" and t.grade["exit_code"] == 0, t.grade
