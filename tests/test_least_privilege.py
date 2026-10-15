"""Least privilege (--- least-privilege, delivery.py).

(a) A job's sandbox, every kind of job, holds a fetch-only gateway grant: its agent can't push, not even its own
branch, whatever the environment's `credential_mode`. Hangar pushes the job's work itself, after its checks, with a
grant of its own pinned to that branch and that commit.
(b) A repo build's sandbox and env setup's validation run the repo's `install`: they get a fetch-only grant for the
clone and nothing else (no credential, no `secrets:`, no cloud tokens).

Each test failed on the code before this change. The git ones push with real git through the gateway to a
`git http-backend` upstream (tests/test_gateway_git)."""

from __future__ import annotations

import pytest

from hangar import delivery, envbuilds
from hangar.models import CredentialKind, Sandbox, SandboxGrant, Session, now
from tests.test_gateway_git import PAT, _box, _files_containing, _git, gw, upstream  # noqa: F401 - fixtures

JOB = {"hangar.job": "job-1", "hangar.session": "ses-job"}


def _posts(up) -> int:
    return sum(1 for m, p in up.requests if m == "POST" and p.endswith("git-receive-pack"))


def _sha(up, ref: str) -> str:
    return _git("--git-dir", str(up.root / "acme/widgets.git"), "rev-parse", "--verify", "-q", ref).strip()


# ============================================================================ (a) jobs only fetch
async def test_a_jobs_agent_cant_push_and_hangar_pushes_the_commit_it_checked(fleet, gw, upstream, store):  # noqa: F811
    sb = await fleet.create("auto", labels=JOB, push_branch="hangar/job-1")
    git = sb.info["git"]
    assert git["mode"] == "gateway" and git["push_refs"] == [] and git["fetch_only"], git
    [own] = fleet.gateway.grants(sb.id)
    assert own.push_refs == [] and not own.pins
    assert _files_containing(_box(fleet, sb), PAT) == []

    # The agent fetches, and pushes nothing: not its job's branch, not a new hangar/ branch, not main.
    assert (await fleet.exec(sb.id, "git fetch -q origin keep")).exit_code == 0
    posts = _posts(upstream)
    for ref in ("refs/heads/hangar/job-1", "refs/heads/hangar/scratch", "refs/heads/main"):
        res = await fleet.exec(sb.id, f"git commit -q --allow-empty -m x && git push origin HEAD:{ref}")
        assert res.exit_code != 0 and "may fetch but not push" in res.stderr, res.stderr
    assert _posts(upstream) == posts, "nothing reached the upstream's receive-pack"
    refs = upstream.refs()
    assert "refs/heads/hangar/job-1" not in refs and "refs/heads/hangar/scratch" not in refs

    # Hangar's delivery: the commit it checked, to the job's branch, with a grant that is gone afterwards.
    sha = (await fleet.exec(sb.id, "git rev-parse HEAD")).stdout.strip()
    res = await delivery.push(fleet, sb.id, "hangar/job-1", sha, "job:job-1")
    assert res.exit_code == 0, res.stderr
    assert _sha(upstream, "refs/heads/hangar/job-1") == sha
    assert [g.id for g in fleet.gateway.grants(sb.id)] == [own.id], "the delivery grant is dropped"
    assert not list((_box(fleet, sb) / "tmp").glob(".hangar-push-*")), "the token file is removed"
    events = store.audit_tail(200)
    granted = [e for e in events if e.action == "gateway.grant" and e.detail.get("delivery")]
    assert granted and granted[-1].detail["pin"] == sha and granted[-1].detail["push_refs"] == [
        "refs/heads/hangar/job-1"]
    assert any(e.action == "gw.git.deny" and e.detail.get("reason") == "fetch-only grant" for e in events)
    assert "hgs_" not in " ".join(e.model_dump_json() for e in events)

    # A follow-up's delivery updates the branch the same way (a fast-forward of what hangar pushed).
    await fleet.exec(sb.id, "git commit -q --allow-empty -m follow-up")
    sha2 = (await fleet.exec(sb.id, "git rev-parse HEAD")).stdout.strip()
    assert (await delivery.push(fleet, sb.id, "hangar/job-1", sha2, "job:job-1")).exit_code == 0
    assert _sha(upstream, "refs/heads/hangar/job-1") == sha2


async def test_a_delivery_grant_pushes_one_commit_to_one_branch(fleet, gw, upstream, settings):  # noqa: F811
    """What an agent that lifted hangar's delivery grant (a lingering process, a hook) could do with it: push the
    commit hangar is pushing, to the branch it is pushing it to. Nothing else, tags included where they are on."""
    settings.gateway_allow_tags = True
    sb = await fleet.create("auto", labels=JOB, push_branch="hangar/job-2")
    await fleet.exec(sb.id, "git commit -q --allow-empty -m vetted")
    vetted = (await fleet.exec(sb.id, "git rev-parse HEAD")).stdout.strip()
    await fleet.exec(sb.id, "git commit -q --allow-empty -m unvetted")
    token, gid = fleet.gateway.mint_delivery(fleet.get(sb.id), "refs/heads/hangar/job-2", vetted, 10)
    hdr = f"-c 'http.{settings.gateway_url}/.extraHeader=Authorization: Bearer {token}'"
    posts = _posts(upstream)
    for what in ("HEAD:refs/heads/hangar/job-2", f"{vetted}:refs/heads/hangar/other", f"{vetted}:refs/heads/main",
                 f"{vetted}:refs/tags/v9", ":refs/heads/keep"):
        res = await fleet.exec(sb.id, f"git {hdr} push origin {what}")
        assert res.exit_code != 0 and "hangar: this delivery grant pushes only" in res.stderr, (what, res.stderr)
    assert _posts(upstream) == posts
    res = await fleet.exec(sb.id, f"git {hdr} push -q origin {vetted}:refs/heads/hangar/job-2")
    assert res.exit_code == 0, res.stderr
    assert _sha(upstream, "refs/heads/hangar/job-2") == vetted
    g = fleet.store.get(SandboxGrant, gid)
    assert g.expires_at <= now() + 600
    fleet.gateway.extend(fleet.get(sb.id))
    assert fleet.store.get(SandboxGrant, gid).expires_at == g.expires_at, "extending a sandbox doesn't extend it"
    fleet.gateway.drop(gid)
    assert fleet.gateway.verify(token) is None


async def test_credential_mode_direct_doesnt_give_a_job_a_token(fleet, gw, upstream, settings, tmp_path):  # noqa: F811
    """An environment that says `credential_mode: direct`, and even HANGAR_GATEWAY_FOR_JOBS=false, can't put a git
    token in a job's sandbox while the gateway serves its repo. A git credential its `secrets:` map is left out."""
    settings.gateway_for_jobs = False
    fleet.catalog.add({"name": "direct", "harnesses": ["git"], "credentials": ["gh-pat"], "credential_mode": "direct",
                       "secrets": {"MY_GH": "gh-pat"},
                       "repo": {"url": "https://github.com/acme/widgets.git", "branch": "main"}}, save_dir=tmp_path)
    sb = await fleet.create("direct", labels=JOB, push_branch="hangar/job-3")
    assert sb.info["git"]["mode"] == "gateway" and sb.info["git"]["push_refs"] == []
    assert _files_containing(_box(fleet, sb), PAT) == []
    assert any("MY_GH" in w and "only fetches" in w for w in sb.info.get("warnings", [])), sb.info.get("warnings")
    res = await fleet.exec(sb.id, "git commit -q --allow-empty -m x && git push origin HEAD:refs/heads/hangar/job-3")
    assert res.exit_code != 0 and "may fetch but not push" in res.stderr


async def test_a_repoless_job_on_a_direct_environment_gets_no_token(fleet, gw, upstream, tmp_path):  # noqa: F811
    """No repo (a Slack question, say) or a host the gateway doesn't serve: still no git token in a job's sandbox."""
    fleet.catalog.add({"name": "direct-norepo", "harnesses": ["git"], "credentials": ["gh-pat"],
                       "credential_mode": "direct"}, save_dir=tmp_path)
    sb = await fleet.create("direct-norepo", labels=JOB)
    assert sb.info["git"]["mode"] == "gateway" and fleet.gateway.grants(sb.id) == []
    assert _files_containing(_box(fleet, sb), PAT) == []
    other = Sandbox(name="g", environment="direct-norepo", provider="docker", labels=JOB,
                    repo="https://gitlab.com/acme/widgets.git")
    assert fleet.gateway.mode_for(fleet.catalog.get("direct-norepo"), other, None) == "gateway"
    other.info["git"] = {"mode": "gateway", "repo": None}
    assert "doesn't serve this repo's host" in delivery.clone_hint(other)


async def test_grants_from_before_the_upgrade_are_narrowed_at_startup(fleet, gw, upstream):  # noqa: F811
    """A job's sandbox kept for follow-ups across the upgrade held `hangar/**` and its branch. At startup its grant
    keeps its token and loses its push refs; a delivery grant a restart cut short goes."""
    sb = await fleet.create("auto", labels=JOB, push_branch="hangar/job-4")
    [own] = fleet.gateway.grants(sb.id)
    own.push_refs = ["refs/heads/hangar/job-4", "refs/heads/hangar/**"]  # as the old code minted it
    fleet.store.put(own)
    session = await fleet.create("auto", labels={"hangar.session": "ses-person"}, push_branch="hangar/session/p")
    fleet.gateway.mint_delivery(fleet.get(sb.id), "refs/heads/hangar/job-4", "a" * 40, 10)
    assert fleet.gateway.narrow() == 2
    assert [g.push_refs for g in fleet.gateway.grants(sb.id)] == [[]]
    assert fleet.gateway.grants(session.id)[0].push_refs[0] == "refs/heads/hangar/session/p", "sessions keep theirs"
    res = await fleet.exec(sb.id, "git commit -q --allow-empty -m x && git push origin HEAD:refs/heads/hangar/job-4")
    assert res.exit_code != 0 and "may fetch but not push" in res.stderr
    assert (await fleet.exec(sb.id, "git fetch -q origin keep")).exit_code == 0, "same token, still fetches"


@pytest.mark.parametrize("repo", ["https://github.com/acme/widgets.git", None, "https://gitlab.com/acme/w.git"])
@pytest.mark.parametrize("labels", [
    {"hangar.job": "job-1", "hangar.session": "ses-1"},  # a job, a follow-up, an auto-fix round
    {"hangar.job": "job-1", "hangar.attempt": "2", "hangar.session": "ses-2"},  # a best-of-N attempt
    {"hangar.job": "job-1", "hangar.doctor": "rca", "hangar.session": "ses-3"},  # the Cloud Doctor's root cause
    {"hangar.job": "job-1", "hangar.doctor": "fix", "hangar.session": "ses-4"},  # and its fix
    {"hangar.job": "job-1", "hangar.env-setup": "setup", "hangar.session": "ses-5"},  # env setup's agents
    {envbuilds.VALIDATE_LABEL: "ab" * 16, "hangar.env-setup.parent": "job-1"},  # env setup's validation
    {envbuilds.LABEL: "bld-1"},  # a repo build
])
def test_no_job_kind_or_build_gets_a_push_grant(fleet, labels, repo):
    fleet.settings.gateway_url, fleet.settings.gateway_for_jobs = "http://gw.test", False
    spec = fleet.catalog.get(fleet.catalog.names()[0]).model_copy(update={"credential_mode": "direct"})
    sb = Sandbox(name="x", environment=spec.name, provider="docker", repo=repo, labels=labels)
    assert fleet.gateway.mode_for(spec, sb, None) == "gateway"
    assert fleet.gateway.push_scope(sb, "hangar/job-1") == []


def test_a_session_that_carries_a_job_is_fetch_only_and_a_persons_session_pushes_its_branch(fleet):
    """A job's session is known by its record too (sandboxes adopted before the label), and a session people drive
    keeps its branch-scoped grant."""
    fleet.settings.gateway_url = "http://gw.test"
    carries = Session(name="j", environment="e", owner="jobs", job_id="job-9")
    mine = Session(name="mine", environment="e", branch="hangar/session/mine")
    fleet.store.put(carries)
    fleet.store.put(mine)
    adopted = Sandbox(name="a", environment="e", provider="docker", labels={"hangar.session": carries.id})
    assert fleet.gateway.push_scope(adopted, "hangar/job-9") == []
    person = Sandbox(name="p", environment="e", provider="docker", labels={"hangar.session": mine.id})
    assert fleet.gateway.push_scope(person, mine.branch)[0] == "refs/heads/hangar/session/mine"


# ============================================================================ (b) builds get nothing
async def test_a_build_sandbox_gets_no_credential_secret_or_cloud_token(fleet, tmp_path, monkeypatch):
    """Without the gateway a build's clone gets no git token either: it runs the repo's install."""
    fleet.broker.put("gh", "git", CredentialKind.GIT_TOKEN, "ghp_buildCLONEtoken0123456789")
    fleet.broker.put("npm", "custom", CredentialKind.GENERIC, "npm_SECRETvalue", env_var="NPM_TOKEN")
    fleet.broker.put("db", "custom", CredentialKind.GENERIC, "db_SECRETvalue", env_var="UNUSED")
    login = fleet.broker.put("login", "custom", CredentialKind.AUTH_FILE, "login_SECRETvalue",
                             file_path=".config/tool/auth.json")
    login.exclusive = True  # a sign-in file: leased to one sandbox, copied back into the vault on release
    fleet.store.put(login)
    fleet.catalog.add({"name": "credenv", "harnesses": ["git"], "credentials": ["gh", "npm", "login"],
                       "secrets": {"DB_PASSWORD": "db"}}, save_dir=tmp_path)
    clouds: list[str] = []
    real = fleet.cloud.attach

    async def attach(prov, sb, spec):
        clouds.append(sb.id)
        return await real(prov, sb, spec)

    monkeypatch.setattr(fleet.cloud, "attach", attach)
    prov = fleet.providers["docker"]
    sb = await fleet.create("credenv", credentials=["gh", "npm", "login"], labels={envbuilds.LABEL: "bld-1"})
    box = _box(fleet, sb)
    for value in ("ghp_buildCLONEtoken", "npm_SECRETvalue", "db_SECRETvalue", "login_SECRETvalue"):
        assert _files_containing(box, value) == [], value
    assert sb.id not in clouds, "no cloud token"
    # not even leased: what `install` writes at the sign-in file's path never goes back into the vault
    assert fleet.store.get(type(login), "login").leased_to is None and sb.credentials == ["gh"]
    (box / "home/agent/.config/tool").mkdir(parents=True, exist_ok=True)
    (box / "home/agent/.config/tool/auth.json").write_text("planted by install")
    await fleet.destroy(sb.id)
    assert fleet.broker.secret_text("login") == "login_SECRETvalue"
    plain = await fleet.create("credenv", credentials=["gh", "npm"])  # the label is what takes them away
    env = (await prov.read_file(plain, "/home/agent/.hangar/env") or b"").decode()
    assert "npm_SECRETvalue" in env and "db_SECRETvalue" in env and plain.id in clouds
    assert "git gateway" in delivery.clone_hint(sb)


async def test_a_build_sandbox_clones_through_a_fetch_only_grant(fleet, gw, upstream):  # noqa: F811
    sb = await fleet.create("auto", labels={envbuilds.LABEL: "bld-2"})
    assert sb.info["git"]["mode"] == "gateway" and sb.info["git"]["push_refs"] == []
    box = _box(fleet, sb)
    assert (box / "workspace/widgets/README").read_text() == "hi\n"
    assert _files_containing(box, PAT) == []
    res = await fleet.exec(sb.id, "git commit -q --allow-empty -m x && git push origin HEAD:refs/heads/hangar/b")
    assert res.exit_code != 0 and "may fetch but not push" in res.stderr
    assert delivery.clone_hint(sb) == ""
