"""sudo off by default; no-new-privileges for job sandboxes on Docker (#124)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from hangar import posture
from hangar.config import Settings
from hangar.envspec import EnvCatalog, EnvSpec, render
from hangar.images import env_tag
from hangar.jobs import Caller, JobError, JobService, SubmitJob
from hangar.models import Sandbox

ROOT = Path(__file__).parents[1]
GW = "http://hangar-gateway.hangar.svc:8788"
REPO = "https://github.com/o/r.git"


def _built(spec: EnvSpec) -> str:
    """The Dockerfile and its step scripts, as one text."""
    ctx = render(spec)
    return ctx.dockerfile + "".join(v.decode() if isinstance(v, bytes) else v for v in ctx.files.values())


def test_sudo_is_off_by_default_and_the_image_has_no_sudoers_rule():
    spec = EnvSpec(name="x")
    assert spec.sudo is False
    assert "NOPASSWD" not in _built(spec)
    assert "NOPASSWD:ALL" in _built(EnvSpec(name="x", sudo=True))


def test_the_image_tag_changes_with_the_default():
    """Every environment that didn't say sudo gets a new image (docs/upgrading.md: rebuild before deploying)."""
    s = Settings()
    assert env_tag(s, EnvSpec(name="x"), "docker")[0] != env_tag(s, EnvSpec(name="x", sudo=True), "docker")[0]


def test_heavy_implies_sudo_unless_told_no():
    assert EnvSpec(name="x", heavy=True).sudo is True
    assert EnvSpec(name="x", tier="heavy").sudo is True
    with pytest.raises(ValueError, match="needs sudo"):
        EnvSpec(name="x", heavy=True, sudo=False)


def test_shipped_envs_need_no_runtime_root():
    """base, bare, python and the rest install at build time (apt and mise run as root in the Dockerfile); none
    asks for sudo, so none of their images carries the sudoers rule."""
    cat = EnvCatalog([ROOT / "envs"])
    for name in cat.names():
        spec = cat.get(name)
        assert spec.sudo is spec.heavy, name
        if spec.image is None:
            assert "NOPASSWD" not in _built(spec), name


# ====================================================================== no-new-privileges
def _sb(labels=None, **info) -> Sandbox:
    return Sandbox(name="s", environment="x", provider="docker", labels=labels or {}, info=info)


def test_no_new_privs_for_job_sandboxes_under_enforce():
    s = Settings(intake_posture="enforce")
    assert posture.no_new_privs(s, _sb({"hangar.job": "job-1", "hangar.session": "ses-1"}))
    assert posture.no_new_privs(s, _sb({"hangar.experiment.grader": "1"}))
    assert not posture.no_new_privs(s, _sb({"hangar.session": "ses-1"}))  # a person's session
    assert not posture.no_new_privs(s, _sb())  # a sandbox someone made directly
    assert not posture.no_new_privs(s, _sb({"hangar.build": "b"}))  # a repo build (runs install, holds nothing)
    # dockerd needs root inside its VM
    assert not posture.no_new_privs(s, _sb({"hangar.job": "job-1"}, heavy={"docker": True, "vm": True}))
    assert posture.no_new_privs(s, _sb({"hangar.job": "job-1"}, heavy={"docker": False, "profile": "large"}))
    for p in ("warn", "off"):
        assert not posture.no_new_privs(Settings(intake_posture=p), _sb({"hangar.job": "job-1"}))


class _Containers:
    def __init__(self):
        self.runs: list[dict] = []

    def run(self, **kw):
        self.runs.append(kw)
        return SimpleNamespace(id="c1")

    def list(self, **kw):
        return [SimpleNamespace(name="hangar-pool-x-1", status="running", attrs={}, labels={}, id="w1",
                                rename=lambda n: None)]


async def test_the_docker_provider_sets_no_new_privileges_and_skips_warm_containers(monkeypatch):
    from hangar.providers import docker as dmod
    from hangar.providers.docker import DockerProvider

    prov = DockerProvider.__new__(DockerProvider)
    prov.settings = Settings(intake_posture="enforce")
    prov.client = SimpleNamespace(containers=_Containers())
    prov.network = None
    prov.envd = SimpleNamespace(enabled=False)

    claimed = []

    async def claim(image, net=None):
        claimed.append(image)
        return None

    monkeypatch.setattr(prov, "_claim_warm", claim)
    monkeypatch.setattr(dmod, "network_for", lambda s, spec: "hangar-sandboxes")
    spec = EnvSpec(name="x", sudo=True, warm_pool=2)
    job = Sandbox(name="j", environment="x", provider="docker", labels={"hangar.job": "job-1"})
    await prov.create(job, "img", spec)
    kw = prov.client.containers.runs[-1]
    assert kw["security_opt"] == ["no-new-privileges"] and job.info["no_new_privs"] is True
    assert claimed == []  # warm containers started without the option: never claimed for it
    person = Sandbox(name="p", environment="x", provider="docker", labels={"hangar.session": "ses-1"})
    await prov.create(person, "img", spec)
    assert "security_opt" not in prov.client.containers.runs[-1] and person.info["no_new_privs"] is False
    assert claimed == ["img"]


def test_the_manifest_records_sudo_and_no_new_privs():
    from hangar.manifest import _no_new_privs

    assert _no_new_privs(_sb(no_new_privs=True), "docker") is True
    assert _no_new_privs(_sb(), "docker") is False
    assert _no_new_privs(_sb(), "kubernetes") is True  # allowPrivilegeEscalation: false
    assert _no_new_privs(_sb(heavy={"docker": True}), "kubernetes") is False  # privileged in its VM
    assert _no_new_privs(None, "docker") is None


# ====================================================================== heavy and intake
@pytest.fixture
def svc(fleet, tmp_path):
    d = tmp_path / "heavy-envs"
    d.mkdir()
    (d / "dind.yaml").write_text("name: dind\nheavy: true\nharnesses: [git]\n")
    fleet.catalog.dirs.insert(0, d)
    fleet.catalog.reload()
    svc = JobService(fleet, None)
    svc.settings.intake_posture = "enforce"
    svc.settings.gateway_url = GW
    svc.settings.github_repos = {"o/r": "dind, plain"}
    return svc


def test_an_intake_job_on_a_heavy_environment_needs_the_repo_policys_say_so(svc):
    req = SubmitJob(instructions="x", repo=REPO, environment="dind")
    with pytest.raises(JobError, match="allow-heavy-for-intake"):
        svc.submit(req, Caller("x", "x"), source="github", source_ref="github:o/r#1")
    assert svc.submit(req, Caller("sean", "sean"), source="mcp")  # a person's own job: unaffected


def test_the_repo_policy_allows_heavy_for_intake(svc):
    from hangar.repopolicy import PolicyUpdate, update

    update(svc, PolicyUpdate(repo="o/r", allow_heavy_for_intake=True), "admin")
    job = svc.submit(SubmitJob(instructions="x", repo=REPO, environment="dind"), Caller("x", "x"),
                     source="github", source_ref="github:o/r#2")
    assert job.labels[posture.INTAKE_LABEL] == "source:github"
    svc.settings.intake_posture = "warn"
    update(svc, PolicyUpdate(repo="o/r", allow_heavy_for_intake=False), "admin")
    job = svc.submit(SubmitJob(instructions="x", repo=REPO, environment="dind"), Caller("x", "x"),
                     source="github", source_ref="github:o/r#3")
    assert any("allow-heavy-for-intake" in e.message for e in job.events)  # warn: runs, says so


def test_the_doctor_lists_environments_with_sudo(fleet, tmp_path):
    d = tmp_path / "sudo-envs"
    d.mkdir()
    (d / "rooty.yaml").write_text("name: rooty\nsudo: true\n")
    fleet.catalog.dirs.insert(0, d)
    fleet.catalog.reload()
    fleet.settings.intake_posture = "enforce"
    fleet.settings.github_repos = {"o/r": "rooty"}
    checks = {c["name"]: c for c in posture.doctor_checks(SimpleNamespace(settings=fleet.settings, fleet=fleet))}
    c = checks["environments with sudo"]
    assert c["status"] == "warn" and "rooty" in c["detail"] and "intake jobs run in rooty" in c["detail"]
    assert "no-new-privileges" in c["detail"]
