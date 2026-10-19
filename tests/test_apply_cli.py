"""`hangar apply -f` (--- company-deploy, apply_cli.py): config as code over the real REST API. Applying is
idempotent, `--check` reports drift with exit 1 and changes nothing, undeclared policy fields are cleared, credential
documents carry only a mounted-file source and never a value."""

from __future__ import annotations

import json
import textwrap

import pytest
import typer
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from hangar import apply_cli, cli
from hangar.app import create_app
from tests.conftest import FakeProvider

POLICY = """\
schema: hangar.repo-policy/v1
repo: Acme/App
verify: uv run pytest -q
environment: agent
land: auto_after_approval
approvers: [alice]
"""
ENV = """\
schema: hangar.environment/v1
name: acme-python
extends: plain
description: acme's python toolchain
"""
CRED = """\
schema: hangar.credential/v1
name: anthropic-team
harness: claude-code
kind: api_key
source: file:SECRETS/anthropic-team
description: the team's API key, from the secret manager
"""


@pytest.fixture
def api(settings, monkeypatch, tmp_path):
    """cli._call routed into a real app, as an admin; yields (client, calls)."""
    monkeypatch.setenv("HANGAR_SECRETS_DIR", str(tmp_path / "secrets"))
    settings.admin_token = "t0ken"
    app = create_app(settings)
    calls: list[tuple[str, str]] = []
    with TestClient(app) as c:
        app.state.hangar.fleet.providers["docker"] = FakeProvider()
        c.headers["Authorization"] = "Bearer t0ken"

        def call(method, path, **kw):
            calls.append((method, path))
            r = c.request(method, path, **kw)
            if r.status_code >= 400:
                typer.secho(f"error {r.status_code}: {r.text}", err=True)
                raise typer.Exit(1)
            return r.json()

        monkeypatch.setattr(cli, "_call", call)
        yield c, calls


def _dir(tmp_path, **files):
    """The files, with SECRETS standing for the test's secrets directory (HANGAR_SECRETS_DIR)."""
    d = tmp_path / "policy"
    for name, text in files.items():
        p = d / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(text).replace("SECRETS", str(tmp_path / "secrets")))
    return d


def run(*args):
    return CliRunner().invoke(cli.app, ["apply", *args])


def writes(calls):
    return [c for c in calls if c[0] != "GET"]


def test_apply_creates_then_is_idempotent(api, tmp_path):
    c, calls = api
    d = _dir(tmp_path, **{"envs/python.yaml": ENV, "creds.yaml": CRED, "repos/app.yaml": POLICY})
    r = run("-f", str(d))
    assert r.exit_code == 0, r.output
    assert "environment acme-python: created" in r.output
    assert "credential anthropic-team: created" in r.output
    assert "repo acme/app: created" in r.output
    # environments first, then credentials, then the policies that may name them
    assert [p for m, p in writes(calls)] == ["/api/environments/acme-python", "/api/credentials", "/api/repos/policy"]
    p = c.get("/api/repos/policy", params={"repo": "acme/app"}).json()
    assert p["verify"] == "uv run pytest -q" and p["approvers"] == ["alice"] and p["land"] == "auto_after_approval"
    assert c.get("/api/environments/acme-python").json()["spec"]["description"] == "acme's python toolchain"
    cred = next(x for x in c.get("/api/credentials").json() if x["name"] == "anthropic-team")
    assert cred["person"] is None  # a team credential, not the admin's own

    calls.clear()
    r = run("-f", str(d))
    assert r.exit_code == 0, r.output
    assert writes(calls) == []  # nothing to do the second time
    assert "3 object(s), all up to date" in r.output
    assert run("-f", str(d), "--check").exit_code == 0


def test_check_reports_drift_and_changes_nothing(api, tmp_path):
    c, calls = api
    d = _dir(tmp_path, **{"app.yaml": POLICY})
    assert run("-f", str(d)).exit_code == 0
    # someone changes the policy by hand
    assert c.put("/api/repos/policy", json={"repo": "acme/app", "verify": "make test", "max_open_agent_prs": 3}
                 ).status_code == 200
    c.put("/api/repos/policy", json={"repo": "acme/other", "verify": "true"})
    calls.clear()
    r = run("-f", str(d), "--check")
    assert r.exit_code == 1, r.output
    assert writes(calls) == []
    assert "repo acme/app: would change" in r.output
    assert "verify: 'make test' -> 'uv run pytest -q'" in r.output
    assert "max_open_agent_prs: 3 -> -" in r.output  # not declared: apply clears it
    assert "repo acme/other: live, but no file declares it" in r.output
    out = json.loads(run("-f", str(d), "--check", "--json").output)
    assert {i["name"]: i["action"] for i in out["items"]} == {"acme/app": "change", "acme/other": "live-only"}
    assert out["applied"] is False

    r = run("-f", str(d))  # applying fixes the declared policy, leaves the undeclared one
    assert r.exit_code == 0, r.output
    p = c.get("/api/repos/policy", params={"repo": "acme/app"}).json()
    assert p["verify"] == "uv run pytest -q" and p["max_open_agent_prs"] is None
    assert c.get("/api/repos/policy", params={"repo": "acme/other"}).status_code == 200

    r = run("-f", str(d), "--prune")
    assert r.exit_code == 0 and "repo acme/other: deleted" in r.output, r.output
    assert c.get("/api/repos/policy", params={"repo": "acme/other"}).status_code == 404
    assert run("-f", str(d), "--check").exit_code == 0


def test_dry_run_changes_nothing(api, tmp_path):
    c, calls = api
    r = run("-f", str(_dir(tmp_path, **{"a.yaml": POLICY + "---\n" + ENV})), "--dry-run")
    assert r.exit_code == 0, r.output
    assert "would create" in r.output and writes(calls) == []
    assert c.get("/api/repos").json() == []


def test_mounted_credential_is_used_and_never_echoed(api, tmp_path, monkeypatch):
    c, _ = api
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "anthropic-team").write_text("sk-ant-mounted-0123456789abcdef\n")
    r = run("-f", str(_dir(tmp_path, **{"c.yaml": CRED})), "--json")
    assert r.exit_code == 0, r.output
    assert "sk-ant-mounted" not in r.output
    h = c.app.state.hangar
    assert h.vault.source("cred:anthropic-team") == f"file:{secrets / 'anthropic-team'}"
    assert h.broker.secret_text("anthropic-team") == "sk-ant-mounted-0123456789abcdef"
    listed = c.get("/api/credentials").text
    assert "sk-ant-mounted" not in listed and str(secrets) not in listed
    # the database holds the reference only
    assert b"sk-ant-mounted" not in h.vault.decrypt(h.store.get_secret("cred:anthropic-team"))


@pytest.mark.parametrize("text, why", [
    ("repo: acme/app\nverify: x\n", "needs `schema:`"),
    ("schema: hangar.nope/v1\nname: x\n", "unknown schema 'hangar.nope/v1'"),
    ("schema: hangar.repo-policy/v1\nrepo: acme/app\nverfy: x\n", "unknown field(s) verfy"),
    ("schema: hangar.repo-policy/v1\nrepo: acme/app\n", "declares no field"),
    (CRED.replace("source: file:SECRETS/anthropic-team", "value: sk-ant-xyz"), "needs `source"),
    (CRED + "value: sk-ant-xyz\n", "has a `value`"),
    (POLICY + "---\n" + POLICY, "declared twice"),
])
def test_bad_files_are_refused_before_anything_is_applied(api, tmp_path, text, why):
    _, calls = api
    r = run("-f", str(_dir(tmp_path, **{"x.yaml": text})))
    assert r.exit_code == 2 and why in r.output, r.output
    assert writes(calls) == []


def test_a_refused_reference_is_the_servers_400(api, tmp_path):
    _, calls = api
    bad = CRED.replace("file:SECRETS/anthropic-team", "file:/etc/passwd")
    r = run("-f", str(_dir(tmp_path, **{"c.yaml": bad})))
    assert r.exit_code == 1 and "must be" in r.output, r.output


def test_load_reads_directories_and_multi_document_files(tmp_path):
    d = _dir(tmp_path, **{"a/one.yaml": POLICY + "---\n" + ENV, "b/two.yml": CRED, "c/notes.txt": "ignored"})
    docs = apply_cli.load([d])
    assert [(x.schema, x.name) for x in docs] == [
        ("hangar.repo-policy/v1", "acme/app"), ("hangar.environment/v1", "acme-python"),
        ("hangar.credential/v1", "anthropic-team")]
    assert docs[0].origin.endswith("one.yaml#1")
