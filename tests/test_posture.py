"""Intake posture (#123): intake-triggered jobs need the git gateway and get an egress floor (posture.py)."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from hangar import egress, posture
from hangar.config import Settings
from hangar.envspec import EnvSpec
from hangar.jobs import Caller, JobError, JobService, SubmitJob
from hangar.models import AuditEvent, ContextItem, Job, Sandbox
from tests.conftest import ENVS

GW = "http://hangar-gateway.hangar.svc:8788"
REPO = "https://github.com/o/r.git"


def _settings(tmp_path, **kw) -> Settings:
    s = Settings(data_dir=tmp_path, env_dirs=[ENVS], default_provider="docker", enabled_providers=[],
                 reaper_interval_seconds=3600, **kw)
    s.snapshot_dir.mkdir(parents=True, exist_ok=True)
    return s


def test_the_default_is_enforce(monkeypatch):
    monkeypatch.delenv("HANGAR_INTAKE_POSTURE", raising=False)
    assert Settings().intake_posture == "enforce"
    assert Settings(intake_posture="enforce").intake_posture == "enforce"


@pytest.mark.parametrize("value", ["warn", "off", "WARN"])
def test_warn_and_off_are_refused_wherever_they_are_set(value, monkeypatch, tmp_path, store):
    """--- KAN-74: the safe release's switch back (`warn`) and `off` are gone. In the environment hangar doesn't
    start; `hangar config set` refuses them; stored before the upgrade they aren't applied and the doctor fails."""
    from pydantic import ValidationError

    from hangar.settings_store import SettingError, SettingsStore

    monkeypatch.setenv("HANGAR_INTAKE_POSTURE", value)
    with pytest.raises(ValidationError) as e:
        Settings(data_dir=tmp_path)
    assert f"HANGAR_INTAKE_POSTURE={value.lower()} was removed" in str(e.value)
    assert "HANGAR_GATEWAY_URL" in str(e.value)
    monkeypatch.delenv("HANGAR_INTAKE_POSTURE")
    s = Settings(data_dir=tmp_path)
    cfg = SettingsStore(store, s)
    with pytest.raises(SettingError, match="was removed"):
        cfg.set("HANGAR_INTAKE_POSTURE", value)
    store.kv_set("setting:intake_posture", json.dumps(value))  # stored by v0.2.0
    cfg.apply()
    assert s.intake_posture == "enforce"
    checks = posture.removed_checks(SimpleNamespace(settings=s, config=cfg), "intake_posture")
    assert [c["status"] for c in checks] == ["fail"] and "was removed" in checks[0]["detail"]
    assert checks[0]["fix"] == "hangar config unset HANGAR_INTAKE_POSTURE"


def test_the_posture_is_enforce_whatever_the_settings_say(settings):
    """No setting turns it off: only tests replace posture.mode (conftest.py)."""
    from tests.conftest import REAL_POSTURE_MODE

    assert REAL_POSTURE_MODE(settings) == "enforce"


@pytest.mark.parametrize(("source", "untrusted", "ctx", "want"), [
    ("github", None, [], True), ("github-projects", None, [], True), ("jira", None, [], True),
    ("slack", None, [], True), ("webhook", None, [], True),
    ("mcp", None, [], False), ("api", None, [], False), ("cli", None, [], False), ("schedule", None, [], False),
    ("env-setup", "github:outsider", [], True),  # env setup started from an outsider's issue
    ("api", None, [ContextItem(name="ci.md", untrusted=True, content="x")], True),  # an auto-fix round's logs
])
def test_what_counts_as_an_intake_job(source, untrusted, ctx, want):
    job = Job(title="t", instructions="i", environment="plain", source=source, untrusted_source=untrusted, context=ctx)
    assert posture.is_intake(job) is want


def test_the_gateway_must_serve_the_repos_host():
    assert "HANGAR_GATEWAY_URL" in (posture.gateway_problem(Settings(), REPO) or "")
    s = Settings(gateway_url=GW)
    assert posture.gateway_problem(s, REPO) is None and posture.gateway_problem(s, None) is None
    assert "HANGAR_GATEWAY_UPSTREAMS" in posture.gateway_problem(s, "https://gitlab.example/o/r.git")


def test_a_floor_only_tightens():
    assert egress.floored("open", "allowlist") == "allowlist"
    assert egress.floored("none", "allowlist") == "none"  # a stricter environment stays stricter
    assert egress.floored("open", None) == "open" and egress.floored("open", "bogus") == "open"
    assert posture.floor_of({posture.FLOOR_LABEL: "open"}) is None  # a label can never loosen
    assert posture.lacks_floor({}, {posture.FLOOR_LABEL: "allowlist"})
    assert not posture.lacks_floor({posture.FLOOR_LABEL: "allowlist"}, {posture.FLOOR_LABEL: "allowlist"})
    assert not posture.lacks_floor({}, {})


def test_resolve_raises_an_open_environment_to_the_floor_with_its_presets_and_the_repos_hosts():
    s = Settings()
    spec = EnvSpec(name="api", egress=egress.EgressSpec(allow=["docs.python.org"]))
    assert egress.resolve(s, spec, harness="claude-code").mode == "open"
    repo = egress.EgressSpec(allow=["registry.example.com"])
    pol = egress.resolve(s, spec, repo, harness="claude-code", floor="allowlist")
    assert pol.mode == "allowlist" and pol.proxied and pol.enforced
    hosts = {r.host for r in pol.rules}
    assert {"pypi.org", "registry.npmjs.org", "api.anthropic.com", "docs.python.org", "registry.example.com"} <= hosts
    assert "example.com" not in hosts and pol.allows("example.com", 443) is None
    assert any("intake floor" in n for n in pol.notes)
    # a repo policy still tightens below the floor, never loosens it
    assert egress.resolve(s, spec, egress.EgressSpec(mode="none"), floor="allowlist").mode == "none"
    assert egress.proxied(s, spec) is False and egress.proxied(s, spec, "allowlist") is True


def test_the_provider_gets_a_proxied_copy_with_no_warm_pool():
    s = Settings()
    spec = EnvSpec(name="plain", warm_pool=2, egress=egress.EgressSpec(presets=["pypi"], allow=["a.example"]))
    plain = Sandbox(name="s", environment="plain", provider="docker")
    assert posture.provider_spec(s, spec, plain) is spec
    sb = Sandbox(name="s", environment="plain", provider="docker", labels={posture.FLOOR_LABEL: "allowlist"})
    got = posture.provider_spec(s, spec, sb)
    assert got.warm_pool == 0 and got.name != "plain" and egress.proxied(s, got)
    assert got.egress.presets == ["pypi"] and got.egress.allow == ["a.example"]
    from hangar.k8sgc import DERIVED  # the reaper sweeps the template once nothing uses it
    assert DERIVED.match(f"hangar-{got.name}")
    from hangar.providers.docker_net import EGRESS_NAME, network_for
    assert network_for(s, got) == EGRESS_NAME and network_for(s, spec) != EGRESS_NAME
    from hangar.providers.kubernetes import KubernetesProvider
    kp = KubernetesProvider.__new__(KubernetesProvider)
    kp.settings = s
    assert kp._pod_template(got, "img")["metadata"]["labels"][egress.K8S_LABEL] == "proxied"
    # an environment that is proxied already keeps its own spec (and warm pool)
    locked = spec.model_copy(update={"egress": egress.EgressSpec(mode="allowlist")})
    assert posture.provider_spec(s, locked, sb) is locked


async def test_attach_puts_a_floored_sandbox_behind_the_proxy(fleet, monkeypatch):
    class Net:
        async def egress_gateway(self, create=True):
            return "172.31.0.1"

    async def listen(host, relay=True):
        pass

    monkeypatch.setattr(fleet.egress, "listen", listen)
    sb = Sandbox(name="s", environment="plain", provider="docker", info={"harness": "claude-code"},
                 labels={posture.FLOOR_LABEL: "allowlist"})
    env = await egress.attach(fleet, SimpleNamespace(name="docker", network=Net()), sb, fleet.catalog.get("plain"))
    assert env["HTTPS_PROXY"].endswith("@172.31.0.1:3128")
    assert sb.info["egress"] == {**sb.info["egress"], "mode": "allowlist", "enforced": True, "floor": "allowlist",
                                 "floor_reason": "intake"}


# ====================================================================== at submit
@pytest.fixture
def svc(fleet):
    svc = JobService(fleet, None)  # not started: these check submission only
    svc.settings.intake_posture = "enforce"
    svc.settings.github_repos = {"o/r": "plain"}
    return svc


def test_enforce_refuses_an_intake_job_without_the_gateway_and_says_how(svc):
    req = SubmitJob(instructions="fix it", repo=REPO, environment="plain")
    for source in ("github", "jira", "slack", "webhook", "github-projects"):
        with pytest.raises(JobError, match="HANGAR_GATEWAY_URL") as e:
            svc.submit(req, Caller("x", "x"), source=source, source_ref=f"{source}:1")
        assert "HANGAR_INTAKE_POSTURE" not in str(e.value)  # one fix, not a weaker posture (KAN-74)
    # a person's own submission (MCP, the API, the CLI) is unaffected
    job = svc.submit(req, Caller("sean", "sean"), source="mcp")
    assert posture.FLOOR_LABEL not in job.labels and posture.INTAKE_LABEL not in job.labels
    refused = [e for e in svc.fleet.store.audit_tail(50) if isinstance(e, AuditEvent) and e.action == "job.refused"]
    assert refused and "HANGAR_GATEWAY_URL" in refused[-1].detail["detail"]


def test_enforce_with_the_gateway_floors_the_job_and_its_session(svc):
    svc.settings.gateway_url = GW
    job = svc.submit(SubmitJob(instructions="fix it", repo=REPO, environment="plain"), Caller("x", "x"),
                     source="jira", source_ref="jira:ACME-1")
    assert job.labels[posture.FLOOR_LABEL] == "allowlist" and job.labels[posture.INTAKE_LABEL] == "source:jira"
    assert any("egress at least allowlist" in e.message for e in job.events)
    from hangar.sessions import SessionService
    from hangar.vault import Vault

    ss = SessionService(  # the session is made at provision; it takes the job's labels, and its sandboxes take its own
svc.fleet, Vault(svc.fleet.store, b"0" * 43 + b"="))
    s = ss.for_job(job_id=job.id, environment=job.environment, harness=None, model=None, repo=job.repo,
                   base_branch=None, branch=job.branch, provider=None, labels=job.labels, extra_dirs=[],
                   idle_minutes=5)
    assert s.labels[posture.FLOOR_LABEL] == "allowlist"


def test_the_review_packet_shows_the_floor(svc):
    svc.settings.gateway_url = GW
    job = svc.submit(SubmitJob(instructions="fix it", repo=REPO, environment="plain"), Caller("x", "x"),
                     source="webhook", source_ref="sentry:1", untrusted_source="webhook:sentry")
    assert posture.packet_lines(svc.settings, job) == ["- **Egress:** allowlist (intake floor)"]


def test_an_untrusted_follow_up_floors_a_persons_job(svc):
    svc.settings.gateway_url = GW
    job = svc.submit(SubmitJob(instructions="fix it", repo=REPO, environment="plain"), Caller("sean", "sean"),
                     source="mcp")
    assert posture.FLOOR_LABEL not in job.labels
    svc._posture_follow_up(job, [ContextItem(name="ci-failures.md", kind="log", untrusted=True, content="x")],
                           "hangar:autofix")
    assert job.labels[posture.FLOOR_LABEL] == "allowlist"
    assert job.labels[posture.INTAKE_LABEL] == "untrusted-follow-up"
    # without the gateway, enforce refuses that turn (the auto-fix round is dropped with the reason)
    svc.settings.gateway_url = None
    other = svc.submit(SubmitJob(instructions="fix it", repo=REPO, environment="plain"), Caller("sean", "sean"),
                       source="mcp")
    with pytest.raises(JobError, match="HANGAR_GATEWAY_URL"):
        svc._posture_follow_up(other, [ContextItem(name="r.md", untrusted=True, content="x")], "hangar:autofix")


def test_env_setup_children_inherit_the_floor():
    labels = {posture.FLOOR_LABEL: "allowlist", posture.INTAKE_LABEL: "source:github", "github.issue": "3"}
    assert posture.inherit(labels) == {posture.FLOOR_LABEL: "allowlist", posture.INTAKE_LABEL: "source:github"}
    assert posture.is_intake_req("env-setup", None, (), posture.inherit(labels))


# ====================================================================== the doctor
def test_the_doctor_reports_the_posture(fleet):
    s = fleet.settings
    s.intake_posture = "enforce"
    s.github_repos = {"o/r": "plain"}
    s.github_webhook_secret_credential = "gh-hook"
    h = SimpleNamespace(settings=s, fleet=fleet)
    checks = {c["name"]: c for c in posture.doctor_checks(h)}
    assert checks["intake posture: gateway"]["status"] == "fail" and "job from GitHub is refused" in checks[
        "intake posture: gateway"]["detail"]
    assert checks["intake environment plain"]["detail"] == "egress open; intake jobs get allowlist (intake floor)"
    assert checks["intake environment plain"]["status"] == "ok"
    s.gateway_url = GW
    checks = {c["name"]: c for c in posture.doctor_checks(h)}
    assert checks["intake posture: gateway"]["status"] == "ok"


def test_a_fresh_laptop_without_intake_doors_is_not_a_doctor_failure(fleet, monkeypatch):
    """--- #236: `hangar up` on a fresh machine: enforce, no gateway, and no front door that could send an intake job.
    Nothing is refused, so the doctor warns (with the one command that turns the gateway on) instead of failing; the
    first door configured makes it a failure that names that door."""
    s = fleet.settings
    s.intake_posture, s.gateway_url, s.gateway_host = "enforce", None, None
    s.github_repos, s.github_webhook_secret_credential, s.slack_signing_secret_credential = {}, None, None
    s.jira, s.inbound_webhooks = {}, {}
    monkeypatch.setattr("sys.platform", "linux")
    h = SimpleNamespace(settings=s, fleet=fleet)
    gw = {c["name"]: c for c in posture.doctor_checks(h)}["intake posture: gateway"]
    assert gw["status"] == "warn" and "nothing is refused yet" in gw["detail"]
    assert gw["fix"].startswith("hangar config set HANGAR_GATEWAY_URL http://host.docker.internal:8788 && "
                                "hangar config set HANGAR_GATEWAY_HOST 172.17.0.1")
    assert "HANGAR_INTAKE_POSTURE" not in gw["fix"]  # one fix, not a weaker posture
    for field, value, door in (("slack_signing_secret_credential", "slack-sig", "Slack"),
                               ("inbound_webhooks", {"sentry": {"secret": "x"}}, "inbound webhooks"),
                               ("jira", {"url": "https://acme.atlassian.net"}, "Jira")):
        setattr(s, field, value)
        gw = {c["name"]: c for c in posture.doctor_checks(h)}["intake posture: gateway"]
        assert gw["status"] == "fail" and door in gw["detail"] and "HANGAR_GATEWAY_URL" in gw["fix"]
    # the posture itself is unchanged: an intake job is still refused without the gateway
    spec = fleet.catalog.get("plain")
    assert posture.assess(s, spec, REPO, "github", None).refused
    monkeypatch.setattr("sys.platform", "darwin")
    s.inbound_webhooks = s.jira = {}
    s.slack_signing_secret_credential = None
    assert "HANGAR_GATEWAY_HOST" not in posture.gateway_fix(s, fleet)
    s.gateway_url = GW
    assert {c["name"]: c for c in posture.doctor_checks(h)}["intake posture: gateway"]["status"] == "ok"
