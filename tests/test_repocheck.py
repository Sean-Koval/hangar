"""`hangar repo check` (#155): one readiness report for a repo. GitHub, the gateway and Jira are fakes behind a
transport that fails the test on any request that could write (anything but GET, except the App's installation-token
mint)."""

from __future__ import annotations

import json
import time
from types import SimpleNamespace

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from typer.testing import CliRunner

import hangar.cli as cli
from hangar import repocheck
from hangar.jobs import JobService
from hangar.models import CredentialKind
from hangar.repopolicy import RepoPolicy

REPO = "acme/app"
GW = "http://gw.internal:8788"
JIRA = "https://acme.atlassian.net"
HOOK = "https://hangar.acme.dev/api/hooks/github"
SECURE = "name: secure\nharnesses: [claude-code]\ncredentials: [team-key]\negress:\n  mode: allowlist\n"


@pytest.fixture(scope="module")
def app_pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                             serialization.NoEncryption()).decode()


class Remote:
    """api.github.com for acme/app, hangar's gateway and Jira, all read-only. A write fails the test."""

    def __init__(self):
        self.calls: list[httpx.Request] = []
        self.writes: list[str] = []
        self.installed = True
        self.required = ["test"]
        self.classic: int | dict = 403  # the App has no Administration: read
        self.rules = [{"type": "pull_request", "parameters": {
            "required_approving_review_count": 1, "dismiss_stale_reviews_on_push": True,
            "require_last_push_approval": True}}]
        self.label = True
        self.statuses = ["To Do", "Ready for agent", "In Progress", "Needs info", "In Review", "Blocked"]

    def github_calls(self) -> int:
        return sum(r.url.host == "api.github.com" for r in self.calls)

    def __call__(self, r: httpx.Request) -> httpx.Response:
        self.calls.append(r)
        p, m = r.url.path, r.method
        if m == "POST" and r.url.host == "api.github.com" and p == "/app/installations/7/access_tokens":
            exp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 3600))
            return httpx.Response(201, json={"token": "ghs_app", "expires_at": exp})
        if m != "GET":
            self.writes.append(f"{m} {r.url}")
            raise AssertionError(f"repo check wrote: {m} {r.url}")
        if r.url.host == "gw.internal":
            return httpx.Response(200, json={"service": "hangar-gateway"}) if p == "/healthz" else httpx.Response(404)
        if r.url.host == "acme.atlassian.net":
            if p == "/rest/api/3/project/ABC/statuses":
                return httpx.Response(200, json=[{"name": "Task", "statuses": [{"name": n} for n in self.statuses]}])
            return httpx.Response(404, json={"errorMessages": ["no"]})
        base = f"/repos/{REPO}"
        if p == f"{base}/installation":
            return httpx.Response(200, json={"id": 7}) if self.installed else \
                httpx.Response(404, json={"message": "Not Found"})
        if p == base:
            return httpx.Response(200, json={"full_name": REPO, "default_branch": "main", "allow_auto_merge": True})
        if p == f"{base}/hooks":
            return httpx.Response(200, json=[{"id": 1, "active": True, "events": ["*"],
                                              "config": {"url": HOOK, "content_type": "json"}}])
        if p == f"{base}/labels/hangar":
            return httpx.Response(200, json={"name": "hangar"}) if self.label else \
                httpx.Response(404, json={"message": "Not Found"})
        if p == f"{base}/branches/main":
            return httpx.Response(200, json={"name": "main", "protection": {
                "required_status_checks": {"contexts": self.required}}})
        if p == f"{base}/branches/main/protection":
            if isinstance(self.classic, int):
                return httpx.Response(self.classic, json={"message": "Resource not accessible by integration"})
            return httpx.Response(200, json=self.classic)
        if p == f"{base}/rules/branches/main":
            return httpx.Response(200, json=self.rules)
        return httpx.Response(404, json={"message": "Not Found"})


def configure(settings, broker, store, fleet, tmp_path, app_pem, envs: dict[str, str] | None = None) -> None:
    d = tmp_path / "repo-envs"
    d.mkdir(exist_ok=True)
    for name, text in (envs or {"secure": SECURE}).items():
        (d / f"{name}.yaml").write_text(text)
    fleet.catalog.dirs.insert(0, d)
    fleet.catalog.reload()
    broker.put("gh", "git", CredentialKind.GIT_TOKEN, "ghp_control")
    broker.put("gh-webhook", "webhook", CredentialKind.GENERIC, "s3cret")
    broker.put("hangar-app", "git", CredentialKind.GITHUB_APP, json.dumps({"app_id": "99", "private_key": app_pem}))
    broker.put("team-key", "claude-code", CredentialKind.API_KEY, "sk-ant-api-xxxxxxxxxxxx", team=True)
    broker.put("jira-token", "webhook", CredentialKind.GENERIC, "jt")
    broker.put("jira-secret", "webhook", CredentialKind.GENERIC, "js")
    settings.github_token_credential, settings.github_webhook_secret_credential = "gh", "gh-webhook"
    settings.github_app_credential = "hangar-app"
    settings.github_repos = {REPO: next(iter(envs or {"secure": SECURE}))}
    settings.public_url = "https://hangar.acme.dev"
    settings.gateway_url = GW
    settings.intake_posture = "enforce"
    settings.jira = {"url": JIRA, "email": "bot@acme.dev", "token_credential": "jira-token",
                     "webhook_secret_credential": "jira-secret", "projects": {"ABC": {"repo": REPO}},
                     "trigger": {"status": "Ready for agent"},
                     "statuses": {"started": "In Progress", "needs_input": "Needs info", "delivered": "In Review",
                                  "failed": "Blocked"}}
    settings.slack_channels = {"C0123": REPO}


@pytest.fixture
def remote() -> Remote:
    return Remote()


@pytest.fixture
def h(fleet, settings, broker, store, tmp_path, app_pem, remote):
    configure(settings, broker, store, fleet, tmp_path, app_pem)
    svc = JobService(fleet, None, transport=httpx.MockTransport(remote))
    svc.policies.put(RepoPolicy(repo=REPO, verify="uv run pytest -q", land="auto_after_approval",
                                approvers=["alice"], max_usd_per_day=50))
    return SimpleNamespace(jobs=svc, settings=settings, fleet=fleet, store=store, broker=broker)


async def check(h, remote: Remote) -> tuple[dict, dict[str, dict]]:
    r = await repocheck.run(h, REPO, h.jobs.transport)
    assert not remote.writes
    return r, {c["check"]: c for c in r["checks"]}


async def test_a_ready_repo_is_ready_and_a_rerun_reads_github_from_the_cache(h, remote):
    r, by = await check(h, remote)
    assert r["ready"] and r["summary"] == "ready" and r["failing"] == 0, [c for c in r["checks"] if c["status"] != "ok"]
    assert all(set(c) == {"check", "status", "detail", "fix"} for c in r["checks"])
    assert all(c["fix"] is None for c in r["checks"] if c["status"] == "ok")
    for name in ("allowlist", "github app", "github access", "github webhook", "trigger label", "repo policy",
                 "verify", "landing", "daily cap", "required checks", "stale approvals", "review rules",
                 "intake posture", "git gateway", "credential mode secure", "sudo secure", "egress secure",
                 "credentials secure", "jira", "jira transitions ABC", "slack"):
        assert by[name]["status"] == "ok", (name, by.get(name))
    # the App can't read classic protection by design: a warning, and the rulesets decide
    assert by["classic branch protection"]["status"] == "warn" and "Administration: read" in \
        by["classic branch protection"]["detail"]
    assert "C0123" in by["slack"]["detail"] and "team-key" in by["credentials secure"]["detail"]
    n = remote.github_calls()
    again, _ = await check(h, remote)
    assert again == r and remote.github_calls() == n  # landing's 5-minute cache: no GitHub rate limit spent


async def test_no_app_installed_fails_and_reads_with_the_control_token(h, remote):
    remote.installed = False
    r, by = await check(h, remote)
    assert by["github app"]["status"] == "fail" and "not installed" in by["github app"]["detail"]
    assert "settings/installations" in by["github app"]["fix"]
    assert by["github access"]["status"] == "ok" and "control token" in by["github access"]["detail"]
    assert not r["ready"] and r["summary"] == "not ready (1 failing)"


async def test_no_required_checks_fails(h, remote):
    remote.required = []
    remote.rules = [x for x in remote.rules if x["type"] != "required_status_checks"]
    _, by = await check(h, remote)
    assert by["required checks"]["status"] == "fail" and "settings/branches" in by["required checks"]["fix"]


async def test_stale_approvals_and_last_push(h, remote):
    remote.rules = [{"type": "pull_request", "parameters": {"required_approving_review_count": 1}}]
    _, by = await check(h, remote)
    assert by["stale approvals"]["status"] == "fail" and by["review rules"]["status"] == "warn"
    h.jobs.landing._cache.clear()
    remote.classic = {"required_pull_request_reviews": {"required_approving_review_count": 1,
                                                        "dismiss_stale_reviews": True,
                                                        "require_last_push_approval": True}}
    _, by = await check(h, remote)
    assert by["stale approvals"]["status"] == "ok" and "classic branch protection" not in by


async def test_credential_mode_direct(h, remote, settings, fleet, tmp_path):
    (tmp_path / "repo-envs" / "secure.yaml").write_text(SECURE + "credential_mode: direct\n")
    fleet.catalog.reload()
    _, by = await check(h, remote)
    # the gateway still serves the repo: jobs fetch through it, but people's sessions there hold a token
    assert by["credential mode secure"]["status"] == "warn"
    settings.gateway_url = None
    r, by = await check(h, remote)
    assert by["credential mode secure"]["status"] == "fail" and by["git gateway"]["status"] == "fail"
    assert "credential_mode: gateway" in by["credential mode secure"]["fix"] and not r["ready"]


async def test_egress_open(h, remote, settings, fleet, tmp_path):
    (tmp_path / "repo-envs" / "secure.yaml").write_text(
        "name: secure\nharnesses: [claude-code]\ncredentials: [team-key]\negress:\n  mode: open\n")
    fleet.catalog.reload()
    _, by = await check(h, remote)
    assert by["egress secure"]["status"] == "warn"  # enforce floors intake jobs; other doors' jobs run open
    settings.intake_posture = "warn"
    _, by = await check(h, remote)
    assert by["egress secure"]["status"] == "fail" and by["intake posture"]["status"] == "warn"
    assert "egress.mode: allowlist" in by["egress secure"]["fix"]


async def test_sudo(h, remote, settings, fleet, tmp_path):
    (tmp_path / "repo-envs" / "secure.yaml").write_text(SECURE + "sudo: true\n")
    fleet.catalog.reload()
    _, by = await check(h, remote)
    assert by["sudo secure"]["status"] == "warn" and "no-new-privileges" in by["sudo secure"]["detail"]
    settings.intake_posture = "off"
    _, by = await check(h, remote)
    assert by["sudo secure"]["status"] == "fail" and by["intake posture"]["status"] == "fail"


async def test_a_subscription_credential_fails(h, remote, broker, store, settings, fleet, tmp_path):
    from hangar.models import Credential

    store.delete(Credential, "team-key")  # the only claude-code login left is someone's subscription
    (tmp_path / "repo-envs" / "secure.yaml").write_text(SECURE.replace("[team-key]", "[alice-login]"))
    fleet.catalog.reload()
    broker.put("alice-login", "claude-code", CredentialKind.OAUTH_TOKEN, "sk-ant-oat01-xxxxxxxxxxxx", person="alice")
    _, by = await check(h, remote)
    assert by["credentials secure"]["status"] == "fail", by["credentials secure"]
    assert "--kind api_key" in by["credentials secure"]["fix"]
    settings.subscription_logins = "any"  # now jobs would run on it: still not ready
    broker.put("team-sub", "claude-code", CredentialKind.OAUTH_TOKEN, "sk-ant-oat01-yyyyyyyyyyyy")
    (tmp_path / "repo-envs" / "secure.yaml").write_text(SECURE.replace("[team-key]", "[team-sub]"))
    fleet.catalog.reload()
    _, by = await check(h, remote)
    assert by["credentials secure"]["status"] == "fail" and "subscription" in by["credentials secure"]["detail"]


async def test_a_missing_jira_transition_fails(h, remote):
    remote.statuses = ["To Do", "Ready for agent", "In Progress", "Blocked"]
    r, by = await check(h, remote)
    c = by["jira transitions ABC"]
    assert c["status"] == "fail" and "needs_input 'Needs info'" in c["detail"] and "delivered 'In Review'" in \
        c["detail"] and not r["ready"]


async def test_doors_and_policy_gaps(h, remote, settings):
    remote.label = False
    settings.jira = None
    settings.slack_channels = {"C0999": "acme/other"}
    settings.slack_bot_token_credential = "slack-bot"
    h.jobs.policies.put(RepoPolicy(repo=REPO, land="auto_after_approval"))
    _, by = await check(h, remote)
    assert by["trigger label"]["status"] == "warn" and "gh label create hangar" in by["trigger label"]["fix"]
    assert by["jira"]["status"] == "ok" and "off" in by["jira"]["detail"]
    assert by["slack"]["status"] == "warn"  # informational: it never fails the report
    assert by["landing"]["status"] == "fail" and "CODEOWNERS" in by["landing"]["detail"]
    assert by["daily cap"]["status"] == "warn"


async def test_a_repo_outside_the_allowlist(h, remote, settings):
    settings.github_repos = {}
    _, by = await check(h, remote)
    assert by["allowlist"]["status"] == "fail" and "hangar setup github --repo acme/app=" in by["allowlist"]["fix"]


# ====================================================================== REST and CLI
class _Keep:
    def __init__(self, c):
        self.c = c

    def __enter__(self):
        return self.c

    def __exit__(self, *a):
        return False


@pytest.fixture
def client(settings, monkeypatch, tmp_path, app_pem, remote):
    from hangar.app import create_app
    from tests.conftest import FakeProvider

    settings.admin_token = "t0ken"
    app = create_app(settings)
    with TestClient(app) as c:
        hg = app.state.hangar
        hg.fleet.providers["docker"] = FakeProvider()
        configure(hg.settings, hg.broker, hg.store, hg.fleet, tmp_path, app_pem)
        hg.jobs.transport = httpx.MockTransport(remote)
        c.headers["Authorization"] = "Bearer t0ken"
        monkeypatch.setattr(cli, "_client", lambda: _Keep(c))
        yield c


def test_rest_and_cli_json_return_the_same_report_and_exit_1_on_a_failure(client, remote):
    runner = CliRunner()
    remote.required = []
    rest = client.get(f"/api/repos/{REPO}/readiness").json()
    assert rest["repo"] == REPO and not rest["ready"] and rest["summary"].startswith("not ready")
    r = runner.invoke(cli.app, ["repo", "check", REPO, "--json"])
    assert r.exit_code == 1 and json.loads(r.output) == rest
    r = runner.invoke(cli.app, ["repo", "check", REPO])
    assert r.exit_code == 1, r.output
    assert "✗ required checks:" in r.output and "fix: https://github.com/acme/app/settings/branches" in r.output
    assert "not ready (" in r.output.splitlines()[-1]
    assert not remote.writes
    # admin only
    assert client.get(f"/api/repos/{REPO}/readiness", headers={"Authorization": "Bearer nope"}).status_code == 401


def test_cli_exits_0_on_a_ready_repo(monkeypatch):
    rep = {"repo": REPO, "ready": True, "failing": 0, "warnings": 1, "summary": "ready",
           "checks": [{"check": "allowlist", "status": "ok", "detail": "in", "fix": None},
                      {"check": "slack", "status": "warn", "detail": "no channel", "fix": "hangar setup slack"}]}
    seen = []
    monkeypatch.setattr(cli, "_call", lambda m, p, **kw: seen.append((m, p)) or rep)
    r = CliRunner().invoke(cli.app, ["repo", "check", "https://github.com/acme/app.git"])
    assert r.exit_code == 0, r.output
    assert seen == [("GET", "/api/repos/acme/app/readiness")]
    assert "✓ allowlist: in" in r.output and "! slack: no channel" in r.output and "ready, 1 warning(s)" in r.output
    assert CliRunner().invoke(cli.app, ["repo", "check", "nope"]).exit_code == 2
