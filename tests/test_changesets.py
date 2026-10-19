"""Change sets (changesets.py, docs/changesets.md): profiles, submission, the four-repo checkout in one sandbox,
per-role checks, one linked PR per changed repo, delivery grants narrowed to one repo, and the built-in deliverers.

End to end on the fake provider with a scripted agent and four local bare remotes (infra, database, code, deploy)
that the sandboxes reach through `url.insteadOf`, while their PRs go to a fake GitHub (`MultiGH`)."""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import httpx
import pytest

from hangar import changesets
from hangar.harnesses import HARNESSES, Harness
from hangar.jobs import Caller, JobError, JobService, SubmitJob
from hangar.models import ChangeSet, CredentialKind, Job, JobState, Sandbox, SandboxState, Session
from hangar.repopolicy import RepoPolicy
from tests.conftest import FakeProvider, scratch_root
from tests.test_jobs import DONE, _until, sessions  # noqa: F401 - fixtures

pytestmark = pytest.mark.skipif(not shutil.which("tmux") or not shutil.which("git"), reason="needs tmux and git")

ROLES = ("infra", "database", "code", "deploy")
GIT = ["git", "-c", "user.email=t@t", "-c", "user.name=t", "-c", "init.defaultBranch=main"]

AGENT = r"""#!/usr/bin/env bash
mode=$(cat "$MODE_FILE" 2>/dev/null || echo three)
case "$mode" in
  three)
    (cd ../database && echo "create table pay(id int);" > 001.sql && git add -A && git commit -qm "db: add pay table")
    (cd ../infra && echo 'resource "x" "y" {}' > main.tf && git add -A && git commit -qm "infra: add the queue")
    echo "print('pay')" > app.py && git add -A && git commit -qm "code: pay endpoint"
    echo "not mine" > ../deploy/oops.txt
    echo "Three repos changed" > "$HANGAR_JOB_DIR/RESULT.md" ;;
  code-only)
    echo "print('only')" > only.py && git add -A && git commit -qm "code only"
    echo "Code only" > "$HANGAR_JOB_DIR/RESULT.md" ;;
esac
"""

BUMP = "#!/bin/sh\nset -e\nmkdir -p env\necho \"$2\" > \"env/$1.tag\"\n"


def url(role: str) -> str:
    return f"https://github.com/acme/{role}.git"


def profile_data(**over) -> dict:
    d = {"schema": "hangar.changeset-profile/v1", "name": "payments-bot", "environment": "multienv",
         "repos": {"infra": {"repo": url("infra")}, "database": {"repo": url("database")},
                   "code": {"repo": url("code")}, "deploy": {"repo": url("deploy"), "agent": "read"}},
         "land_order": ["database", "infra", "code"],
         "deploy": {"role": "deploy", "after": ["database", "infra", "code"],
                    "ready": {"kind": "image-ready", "image": "registry.example.com/payments-bot",
                              "tag": "{code.merge_sha}"},
                    "apply": {"kind": "command", "run": "sh bump.sh {environment} {code.merge_sha}"},
                    "environments": ["dev"]}}
    d.update(over)
    return d


# ============================================================================ a fake GitHub for four repos
class MultiGH:
    """api.github.com for acme/{infra,database,code,deploy}: PRs (heads read from the local remotes), their bodies,
    reviews, auto-merge (GraphQL), packet comments and branch rules with no required checks."""

    NUMBERS = {"acme/infra": 11, "acme/database": 12, "acme/code": 13, "acme/deploy": 14}

    def __init__(self, remotes: dict[str, Path]):
        self.remotes = remotes
        self.prs: dict[tuple[str, int], dict] = {}
        self.armed: dict[tuple[str, int], str] = {}
        self.reviews: dict[tuple[str, int], list[dict]] = {}
        self.calls: list[httpx.Request] = []
        self.comment_id = 900

    def head(self, repo: str, branch: str) -> str | None:
        r = subprocess.run(["git", "--git-dir", str(self.remotes[repo.split("/")[1]]), "rev-parse", "-q", "--verify",
                            f"refs/heads/{branch}"], capture_output=True, text=True)
        return r.stdout.strip() or None

    def pr(self, repo: str) -> dict | None:
        return next((p for (r, _), p in self.prs.items() if r == repo), None)

    def _view(self, repo: str, pr: dict) -> dict:
        return {**pr, "head": {"sha": self.head(repo, pr["head_ref"]), "ref": pr["head_ref"]}, "base": {"ref": "main"},
                "mergeable_state": "blocked", "auto_merge": {"merge_method": "squash"}
                if (repo, pr["number"]) in self.armed else None}

    def __call__(self, r: httpx.Request) -> httpx.Response:
        self.calls.append(r)
        p, m = r.url.path, r.method
        body = json.loads(r.content) if r.content else {}
        if p == "/graphql":
            q, v = body["query"], body["variables"]
            key = next(k for k, pr in self.prs.items() if pr["node_id"] == v["id"])
            if "enablePullRequestAutoMerge" in q:
                self.armed[key] = v["sha"]
                return httpx.Response(200, json={"data": {"enablePullRequestAutoMerge": {
                    "pullRequest": {"number": key[1]}}}})
            self.armed.pop(key, None)
            return httpx.Response(200, json={"data": {"disablePullRequestAutoMerge": {
                "pullRequest": {"number": key[1]}}}})
        mt = re.match(r"^/repos/(acme/[\w-]+)(/.*)?$", p)
        if not mt:
            return httpx.Response(404, json={"message": "Not Found"})
        repo, rest = mt[1], mt[2] or ""
        if rest == "":
            return httpx.Response(200, json={"full_name": repo, "default_branch": "main", "allow_auto_merge": True})
        if rest == "/pulls" and m == "GET":
            branch = r.url.params.get("head", ":").split(":", 1)[1]
            return httpx.Response(200, json=[self._view(repo, x) for (rr, _), x in self.prs.items()
                                             if rr == repo and x["head_ref"] == branch and x["state"] == "open"])
        if rest == "/pulls" and m == "POST":
            n = self.NUMBERS[repo]
            pr = {"number": n, "html_url": f"https://github.com/{repo}/pull/{n}", "node_id": f"PR_{repo}_{n}",
                  "state": "open", "draft": body["draft"], "title": body["title"], "body": body["body"],
                  "head_ref": body["head"], "base_ref": body["base"], "created_at": "2026-10-19T00:00:00Z"}
            self.prs[(repo, n)] = pr
            return httpx.Response(201, json=self._view(repo, pr))
        if mm := re.match(r"^/pulls/(\d+)$", rest):
            pr = self.prs.get((repo, int(mm[1])))
            if pr is None:
                return httpx.Response(404, json={"message": "Not Found"})
            if m == "PATCH":
                pr["body"] = body.get("body", pr["body"])
            return httpx.Response(200, json=self._view(repo, pr))
        if mm := re.match(r"^/pulls/(\d+)/reviews$", rest):
            return httpx.Response(200, json=self.reviews.get((repo, int(mm[1])), []))
        if re.match(r"^/pulls/(\d+)/files$", rest):
            return httpx.Response(200, json=[])
        if re.match(r"^/issues/(\d+)/comments$", rest) and m == "POST":
            self.comment_id += 1
            return httpx.Response(201, json={"id": self.comment_id})
        if re.match(r"^/issues/comments/(\d+)$", rest):
            return httpx.Response(200, json={"id": 1})
        if re.match(r"^/issues/(\d+)/labels$", rest):
            return httpx.Response(200, json=[])
        if rest == "/branches/main":
            return httpx.Response(200, json={"name": "main", "protected": True,
                                             "protection": {"required_status_checks": {"contexts": []}}})
        if rest.startswith("/rules/branches/"):
            return httpx.Response(200, json=[])
        return httpx.Response(404, json={"message": "Not Found"})


# ============================================================================ fixtures
@pytest.fixture
def multi(monkeypatch):
    """Four bare remotes (deploy carries bump.sh), the scripted agent, and an environment for the set."""
    root = Path(tempfile.mkdtemp(prefix="cs-", dir=scratch_root()))
    remotes: dict[str, Path] = {}
    for role in ROLES:
        remote, seed = root / f"{role}.git", root / f"seed-{role}"
        subprocess.run([*GIT, "init", "-q", "--bare", str(remote)], check=True)
        subprocess.run([*GIT, "init", "-q", str(seed)], check=True)
        (seed / "README.md").write_text(f"{role}\n")
        if role == "deploy":
            (seed / "bump.sh").write_text(BUMP)
        subprocess.run([*GIT, "-C", str(seed), "add", "."], check=True)
        subprocess.run([*GIT, "-C", str(seed), "commit", "-qm", "init"], check=True)
        subprocess.run([*GIT, "-C", str(seed), "push", "-q", str(remote), "HEAD:refs/heads/main"], check=True)
        subprocess.run(["git", "--git-dir", str(remote), "symbolic-ref", "HEAD", "refs/heads/main"], check=True)
        remotes[role] = remote
    agent = root / "agent.sh"
    agent.write_text(AGENT)
    mode = root / "mode"
    monkeypatch.setitem(HARNESSES, "multiagent", Harness(name="multiagent", title="Multi",
                                                         run=f"MODE_FILE={mode} bash {agent}"))
    envs = root / "envs"
    envs.mkdir()
    (envs / "multienv.yaml").write_text("name: multienv\nharnesses: [multiagent]\n")
    gitconfig = "".join(f'[url "file://{remotes[r]}"]\n\tinsteadOf = {url(r)}\n' for r in ROLES)
    real_create = FakeProvider.create

    async def create(self, sandbox, image, spec=None):  # every sandbox reaches the four remotes by their URLs
        await real_create(self, sandbox, image, spec)
        (self.boxes[sandbox.id] / "home/agent/.gitconfig").write_text(gitconfig)

    monkeypatch.setattr(FakeProvider, "create", create)
    yield {"root": root, "remotes": remotes, "mode": mode, "envs": envs}
    shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
async def cjobs(fleet, multi, settings, sessions, broker):  # noqa: F811
    settings.job_poll_seconds = 0.1
    settings.job_concurrency = 2
    broker.put("gh", "git", CredentialKind.GIT_TOKEN, "ghp_control")
    settings.github_token_credential = "gh"
    fleet.catalog.dirs.insert(0, multi["envs"])
    fleet.catalog.reload()
    gh = MultiGH(multi["remotes"])
    svc = JobService(fleet, sessions, transport=httpx.MockTransport(gh))
    svc.gh = gh
    for role in ("infra", "database", "code", "deploy"):
        svc.policies.put(RepoPolicy(repo=f"acme/{role}", land="auto_after_approval", approvers=["alice"],
                                    require_stale_dismissal=False))
    changesets.profiles(svc).put(profile_data(), "admin")
    await svc.start()
    yield svc
    await svc.stop()
    for box in fleet.providers["docker"].boxes.values():
        subprocess.run(["tmux", "kill-server"], env={"TMUX_TMPDIR": f"{box}/tmp", "PATH": "/usr/bin:/bin"},
                       capture_output=True)


async def deliver_three(svc: JobService, **kw) -> tuple[Job, ChangeSet]:
    job = svc.submit(SubmitJob(instructions="Add the pay endpoint", changeset="payments-bot", deliver="pr",
                               ticket="jira:PAY-412", **kw), Caller("sean", "sean"))
    job = await _until(svc, job.id, DONE, timeout=60)
    while job.id in svc._active:
        await asyncio.sleep(0.05)
    return job, changesets.of(svc).of(job)


def remote_has(remote: Path, branch: str) -> bool:
    return subprocess.run(["git", "--git-dir", str(remote), "rev-parse", "-q", "--verify", f"refs/heads/{branch}"],
                          capture_output=True).returncode == 0


# ============================================================================ profiles
@pytest.mark.parametrize("change,why", [
    ({"land_order": ["database", "infra"]}, "missing: code"),
    ({"land_order": ["database", "infra", "code", "deploy"]}, "read-only roles: deploy"),
    ({"land_order": ["database", "infra", "code", "nope"]}, "aren't in repos: nope"),
    ({"repos": {"code": {"repo": url("code")}, "Bad": {"repo": url("infra")}}, "land_order": ["code"],
      "deploy": None}, "role names"),
    ({"repos": {"code": {"repo": url("code")}, "infra": {"repo": url("code")}}, "land_order": ["code", "infra"],
      "deploy": None}, "different repo"),
    ({"primary": "deploy"}, "primary role"),
    ({"deploy": {"role": "code", "apply": {"kind": "command", "run": "x"}}}, "changed by its step"),
    ({"deploy": {"role": "deploy", "after": ["deploy"], "apply": {"kind": "command", "run": "x"}}},
     "don't land: deploy"),
    ({"deploy": {"role": "deploy", "apply": {"kind": "command"}, "environments": ["dev", "prod"]}}, "at most 1"),
    ({"schema": "hangar.changeset-profile/v2"}, "schema"),
])
def test_profile_validation(change, why):
    with pytest.raises(changesets.ChangeSetError, match=re.escape(why)):
        changesets.parse_profile(profile_data(**change))


def test_profile_defaults_and_digest():
    p = changesets.parse_profile(profile_data())
    assert p.primary == "code" and p.deploy_after() == ["database", "infra", "code"]
    assert p.digest() == changesets.parse_profile(profile_data()).digest()
    assert p.digest() != changesets.parse_profile(profile_data(land_order=["infra", "database", "code"])).digest()
    nocode = profile_data(repos={"svc": {"repo": url("code")}, "db": {"repo": url("database")}},
                          land_order=["db", "svc"], deploy=None)
    assert changesets.parse_profile(nocode).primary == "svc"  # no `code` role: the last to land


async def test_profiles_are_admin_config(fleet, settings, multi):
    fleet.catalog.dirs.insert(0, multi["envs"])
    fleet.catalog.reload()
    svc = JobService(fleet, None)
    store = changesets.profiles(svc)
    with pytest.raises(changesets.ChangeSetError, match="no environment 'nope'"):
        store.put(profile_data(environment="nope"), "admin")
    bad = profile_data()
    bad["deploy"]["apply"]["kind"] = "acme-argocd-bump"
    with pytest.raises(changesets.ChangeSetError, match="no deliverer of kind 'acme-argocd-bump'"):
        store.put(bad, "admin")
    p = store.put(profile_data(), "admin")
    assert store.get("payments-bot").digest() == p.digest() and [x.name for x in store.list()] == ["payments-bot"]
    assert any(e.action == "changeset.profile.set" for e in fleet.store.audit_tail(10))
    assert store.delete("payments-bot", "admin") and store.get("payments-bot") is None


# ============================================================================ submission
async def test_submit_resolves_the_profile_and_refuses_what_a_set_cant_do(fleet, settings, multi):
    fleet.catalog.dirs.insert(0, multi["envs"])
    fleet.catalog.reload()
    svc = JobService(fleet, None)
    changesets.profiles(svc).put(profile_data(), "admin")
    me = Caller("sean", "sean")
    with pytest.raises(JobError, match="no change-set profile 'nope'"):
        svc.submit(SubmitJob(instructions="x", changeset="nope"), me)
    with pytest.raises(JobError, match="primary repo"):
        svc.submit(SubmitJob(instructions="x", changeset="payments-bot", repo=url("infra")), me)
    with pytest.raises(JobError, match="one attempt"):
        svc.submit(SubmitJob(instructions="x", changeset="payments-bot", strategy="best_of_n", n=2), me)
    with pytest.raises(JobError, match="branches"):
        svc.submit(SubmitJob(instructions="x", changeset="payments-bot", branch="hangar/mine"), me)
    with pytest.raises(JobError, match="deliver PRs"):
        svc.submit(SubmitJob(instructions="x", changeset="payments-bot", deliver="branch"), me)
    with pytest.raises(JobError, match="ticket is scheme:key"):
        svc.submit(SubmitJob(instructions="x", changeset="payments-bot", ticket="PAY 412"), me)
    svc.policies.put(RepoPolicy(repo="acme/database", require_ticket=True))
    with pytest.raises(JobError, match="database repo's policy requires a ticket"):
        svc.submit(SubmitJob(instructions="x", changeset="payments-bot"), me)
    with pytest.raises(JobError, match="repos are all in HANGAR_GITHUB_REPOS"):
        svc.submit(SubmitJob(instructions="x", changeset="payments-bot", ticket="jira:PAY-1"),
                   Caller("bot", "bot", restricted=True))
    svc.policies.put(RepoPolicy(repo="acme/infra", branch_template="feature/{ticket_key}-{job}"))
    job = svc.submit(SubmitJob(instructions="x", changeset="payments-bot", ticket="jira:PAY-412"), me)
    cs = changesets.of(svc).of(job)
    assert job.repo == url("code") and job.environment == "multienv" and job.ticket == "jira:PAY-412"
    assert cs.parent_job == job.id and cs.primary == "code" and cs.state == "working"
    assert cs.profile_digest == changesets.profiles(svc).get("payments-bot").digest()
    assert cs.roles["infra"].branch == f"feature/PAY-412-{job.id}" and cs.roles["code"].branch == job.branch
    assert cs.roles["database"].branch == f"hangar/{job.id}" and cs.roles["deploy"].agent == "read"
    assert job.labels[changesets.LABEL] == cs.id and any("change set cs-" in e.message for e in job.events)
    # a later edit of the profile changes new sets only
    changesets.profiles(svc).put(profile_data(land_order=["infra", "database", "code"]), "admin")
    assert changesets.of(svc).get(cs.id).land_order == ["database", "infra", "code"]


async def test_single_repo_conventions_from_the_repo_policy(fleet, settings, multi):
    """G7 for any job: `branch_template`, `require_ticket` and `title_template` from the repo policy."""
    fleet.catalog.dirs.insert(0, multi["envs"])
    fleet.catalog.reload()
    svc = JobService(fleet, None)
    me = Caller("sean", "sean")
    svc.policies.put(RepoPolicy(repo="acme/code", require_ticket=True, branch_template="feature/{ticket_key}-{job}",
                                title_template="{ticket_key}: {title}"))
    with pytest.raises(JobError, match="requires a ticket"):
        svc.submit(SubmitJob(instructions="x", repo=url("code"), environment="multienv"), me)
    job = svc.submit(SubmitJob(instructions="Fix the bug", repo=url("code"), environment="multienv",
                               ticket="jira:PAY-9"), me)
    assert job.branch == f"feature/PAY-9-{job.id}" and job.changeset is None
    assert changesets.title_for(svc.policies.get(job.repo), job) == "PAY-9: Fix the bug"
    with pytest.raises(JobError, match="under hangar/"):  # a caller's own branch still lives under hangar/
        svc.submit(SubmitJob(instructions="x", repo=url("code"), environment="multienv", ticket="jira:PAY-9",
                             branch="feature/mine"), me)
    with pytest.raises(ValueError, match="must contain"):
        RepoPolicy(repo="acme/code", branch_template="feature/{ticket_key}")
    with pytest.raises(ValueError, match="unknown placeholder"):
        RepoPolicy(repo="acme/code", title_template="{nope}: {title}")


# ============================================================================ end to end: four repos, three PRs
async def test_four_repo_change_set_delivers_three_linked_prs(cjobs, multi, fleet):
    job, cs = await deliver_three(cjobs)
    assert job.state == JobState.SUCCEEDED, [e.message for e in job.events]
    gh, remotes = cjobs.gh, multi["remotes"]
    # one sandbox held every role at /workspace/<role>; the agent read the layout in its prompt
    box = fleet.providers["docker"].boxes[job.sandbox_id]
    for role in ROLES:
        assert (box / f"workspace/{role}/.git").is_dir(), role
    prompt = next((box / "home/agent/.hangar/runs").glob("*.prompt")).read_text()
    assert f"change set `{cs.id}`" in prompt and "/workspace/deploy`: deploy" in prompt and "read-only" in prompt
    assert cs.roles["database"].base_sha and cs.roles["infra"].base_branch == "main"
    # three parts, each pushed to its own repo on its branch, one PR each; deploy (read-only) delivered nothing
    assert sorted(cs.parts) == ["code", "database", "infra"] and cs.state == "landing"
    assert any("deploy is read-only" in e.message for e in job.events)
    for role in ("database", "infra", "code"):
        part = cjobs.get(cs.parts[role])
        assert part.source == "changeset" and part.parent == job.id and part.role == role
        assert part.repo == url(role) and part.state == JobState.SUCCEEDED and part.autofix == "off"
        assert remote_has(remotes[role], part.branch) and part.result.pr_number == MultiGH.NUMBERS[f"acme/{role}"]
        assert part.landing.pr_state == "open" and part.result.head_sha == gh.head(f"acme/{role}", part.branch)
    assert not remote_has(remotes["deploy"], job.branch) and gh.pr("acme/deploy") is None
    assert job.result.pr_number is None and len(job.result.commits) == 3
    assert {c.split(":")[0] for c in job.result.commits} == {"database", "infra", "code"}
    # cross-linked: every PR body lists its siblings' PRs, in landing order, and ends with the change set trailer
    for role in ("database", "infra", "code"):
        body = gh.pr(f"acme/{role}")["body"]
        for other in ("database", "infra", "code"):
            n = MultiGH.NUMBERS[f"acme/{other}"]
            assert f"[#{n}](https://github.com/acme/{other}/pull/{n})" in body, (role, other)
        assert "| deploy | acme/deploy | - | waiting for the others to merge |" in body
        assert body.rstrip().endswith(f"Hangar-Changeset: {cs.id}") and f"Hangar-Job: {job.id}" in body
        assert body.index("database") < body.index("infra") < body.index("| code")
    assert gh.pr("acme/code")["title"] == job.title
    # the view shows the set on the parent
    v = cjobs.view(job)["changeset"]
    assert v["id"] == cs.id and v["parts"]["infra"]["pr_number"] == 11


async def test_a_role_that_did_not_change_gets_no_pr(cjobs, multi):
    multi["mode"].write_text("code-only")
    job, cs = await deliver_three(cjobs)
    assert job.state == JobState.SUCCEEDED and list(cs.parts) == ["code"]
    assert "| database | acme/database | - | no change |" in cjobs.gh.pr("acme/code")["body"]


async def test_a_roles_own_check_fails_the_job(cjobs, multi):
    cjobs.policies.put(RepoPolicy(repo="acme/infra", verify="test -f missing.tf", land="auto_after_approval",
                                  approvers=["alice"]))
    job, cs = await deliver_three(cjobs, verify_in_loop=False)
    assert job.state == JobState.FAILED and job.reason == "verify_failed", [e.message for e in job.events]
    assert "[infra: `test -f missing.tf` exited 1]" in job.result.verify_tail
    assert any(e.message == "infra check: failed (exit 1)" for e in job.events)
    assert all(cjobs.gh.pr(f"acme/{r}")["draft"] for r in ("database", "infra", "code"))  # delivered as drafts


async def test_follow_up_on_a_part_goes_to_the_parent(cjobs, multi):
    job, cs = await deliver_three(cjobs)
    part = cjobs.get(cs.parts["infra"])
    multi["mode"].write_text("code-only")
    got = cjobs.follow_up(part.id, "also add only.py", requester="sean")
    assert got.id == job.id and len(got.turns) == 2
    job = await _until(cjobs, job.id, DONE, timeout=60)
    assert job.state == JobState.SUCCEEDED
    code = cjobs.get(cs.parts["code"])
    assert code.result.head_sha == cjobs.gh.head("acme/code", code.branch) and len(code.result.commits) == 2


# ============================================================================ the parent's sandbox and its grants
def _parent_sandbox(fleet, svc: JobService, created_by: str | None = None) -> tuple[Sandbox, ChangeSet]:
    changesets.profiles(svc).put(profile_data(), "admin")
    job = svc.submit(SubmitJob(instructions="x", changeset="payments-bot", ticket="jira:PAY-1"), Caller("s", "s"))
    s = Session(name="job-x", title="t", owner="jobs", environment="multienv", repo=job.repo, branch=job.branch,
                job_id=job.id)
    fleet.store.put(s)
    sb = Sandbox(id="sb-cs", name="sb-cs", environment="multienv", provider="fake", state=SandboxState.RUNNING,
                 labels={"hangar.session": s.id, "hangar.job": job.id}, created_by=created_by or f"session:{s.id}",
                 repo=job.repo)
    fleet.store.put(sb)
    return sb, changesets.of(svc).of(job)


async def test_the_parents_grant_names_every_role_and_a_forged_label_gets_nothing(fleet, settings, multi):
    fleet.catalog.dirs.insert(0, multi["envs"])
    fleet.catalog.reload()
    svc = JobService(fleet, None)
    sb, cs = _parent_sandbox(fleet, svc)
    assert changesets.sandbox_roles(fleet, sb).id == cs.id
    extra, roles = changesets.grant_repos(fleet, sb, "github.com", ["github.com"])
    assert sorted(extra) == ["acme/database", "acme/deploy", "acme/infra"] and roles["code"] == "acme/code"
    forged = sb.model_copy(update={"id": "sb-forged", "created_by": "mallory"})
    assert changesets.sandbox_roles(fleet, forged) is None
    assert changesets.grant_repos(fleet, forged, "github.com", ["github.com"]) == ([], {})


async def test_one_roles_delivery_grant_cant_push_another(fleet, settings, broker, multi):
    from hangar.gateway import GatewayError
    from tests.test_gateway import _basic, _client, _stub

    settings.gateway_url = "http://gw.test:8788"
    broker.put("pat", "git", CredentialKind.GIT_TOKEN, "ghp_stubUPSTREAMsecret0987654321")
    sb = Sandbox(id="sb-1", name="sb-1", environment="e", provider="fake", state=SandboxState.RUNNING)
    fleet.store.put(sb)
    gw = fleet.gateway
    gw.mint(sb, "github.com", ["acme/code", "acme/infra", "acme/database"], [], "pat")  # the parent's: fetch-only
    sha = "c" * 40
    token, grant = gw.mint_delivery(sb, "refs/heads/hangar/job-1", sha, 5, repo="acme/infra")
    assert next(g for g in gw.grants(sb.id) if g.id == grant).repos == ["acme/infra"]
    with pytest.raises(GatewayError, match="doesn't cover acme/deploy"):
        gw.mint_delivery(sb, "refs/heads/hangar/job-1", sha, 5, repo="acme/deploy")
    _stub(fleet, body=b"001f# service=git-receive-pack\n0000", ctype="application/x-git-receive-pack-advertisement")
    async with _client(fleet) as c:
        push = "/git/github.com/acme/{}.git/info/refs?service=git-receive-pack"
        assert (await c.get(push.format("infra"), headers=_basic(token))).status_code == 200
        for other in ("code", "database"):
            assert (await c.get(push.format(other), headers=_basic(token))).status_code == 404, other
    whole, _ = gw.mint_delivery(sb, "refs/heads/hangar/job-1", sha, 5)  # a single-repo job's: as before
    assert sorted(gw.verify(whole).repos) == ["acme/code", "acme/database", "acme/infra"]


def test_the_profile_schema_takes_what_the_design_shows():
    """The design's profile (docs/changesets.md) parses as written, `agent: read` and all."""
    doc = (Path(__file__).parents[1] / "docs" / "changesets.md").read_text()
    import yaml

    block = re.search(r"```yaml\n(schema: hangar\.changeset-profile/v1\n.*?)```", doc, re.S)
    assert block, "docs/changesets.md shows a profile"
    p = changesets.parse_profile(yaml.safe_load(block.group(1)))
    assert p.deploy and p.deploy.apply.kind == "command" and p.repos["deploy"].agent == "read"


def test_ticket_key():
    assert changesets.ticket_key("jira:PAY-412") == "PAY-412" and changesets.ticket_key(None) is None
    assert changesets.check_ticket(" github:acme/code#12 ") == "github:acme/code#12"
    with pytest.raises(changesets.ChangeSetError):
        changesets.check_ticket("nope")
