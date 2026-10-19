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


# ---------------------------------------------------------------------------- all or nothing (KAN-35)
def _refuse(monkeypatch, *which):
    """cli._call as the api fixture's, but the server refuses the writes `which` names: an int is the Nth write of
    the run (1-based), a (method, path) pair every such write."""
    real, n = cli._call, [0]

    def call(method, path, **kw):
        if method != "GET":
            n[0] += 1
            if n[0] in which or (method, path) in which:
                typer.secho(f"error 400: refused {method} {path}", err=True)
                raise typer.Exit(1)
        return real(method, path, **kw)

    monkeypatch.setattr(cli, "_call", call)


def _state(c):
    """Everything apply can touch, as the API shows it (but when it was last written: a rollback is a write), and
    where each credential's value comes from."""
    h = c.app.state.hangar
    envs = {e["name"]: c.get(f"/api/environments/{e['name']}").json()["raw"] for e in c.get("/api/environments").json()}
    creds, repos = ([{k: v for k, v in x.items() if k != "updated_at"} for x in c.get(p).json()]
                    for p in ("/api/credentials", "/api/repos"))
    return envs, creds, repos, {x["name"]: h.vault.source(f"cred:{x['name']}") for x in creds}


ENV2 = ENV.replace("acme's python toolchain", "acme's python toolchain, v2")
CRED2 = CRED.replace("the team's API key, from the secret manager", "rotated")
POLICY2 = POLICY.replace("approvers: [alice]", "approvers: [bob]\nmax_open_agent_prs: 2")


def test_a_refused_first_write_applies_nothing(api, tmp_path, monkeypatch):
    c, calls = api
    before = _state(c)
    _refuse(monkeypatch, 1)
    d = _dir(tmp_path, **{"all.yaml": ENV + "---\n" + CRED + "---\n" + POLICY})
    r = run("-f", str(d))
    assert r.exit_code == 1, r.output
    assert f"environment acme-python ({d / 'all.yaml'}#1): could not create it" in r.output
    assert "nothing was applied" in r.output
    assert _state(c) == before
    assert writes(calls) == []  # the refused write never reached the server, and there was nothing to undo


def test_a_refused_middle_write_rolls_back_the_earlier_ones(api, tmp_path, monkeypatch):
    c, calls = api
    # live: an environment, a credential and a policy, and one more of each to be created
    assert run("-f", str(_dir(tmp_path, **{"a.yaml": ENV + "---\n" + CRED + "---\n" + POLICY}))).exit_code == 0
    before = _state(c)
    files = {"a.yaml": ENV2 + "---\n" + CRED2 + "---\n" + POLICY2,
             "b.yaml": ENV.replace("acme-python", "acme-node") + "---\n" + POLICY.replace("Acme/App", "acme/web")}
    d = _dir(tmp_path, **files)
    calls.clear()
    _refuse(monkeypatch, 4)  # envs (acme-node, acme-python), the credential, then acme/app's policy is refused
    r = run("-f", str(d))
    assert r.exit_code == 1, r.output
    assert f"repo acme/app ({d / 'a.yaml'}#3): could not change it" in r.output
    assert "rolled back 3 change(s): hangar is as it was before this run" in r.output
    assert _state(c) == before
    assert c.get("/api/environments/acme-node").status_code == 404

    _refuse(monkeypatch)  # the server takes everything again: the same files apply
    assert run("-f", str(d)).exit_code == 0
    assert c.get("/api/repos/policy", params={"repo": "acme/app"}).json()["approvers"] == ["bob"]


def test_a_refused_prune_delete_puts_back_what_was_pruned(api, tmp_path, monkeypatch):
    c, _ = api
    for repo in ("acme/old", "acme/stale"):
        assert c.put("/api/repos/policy", json={"repo": repo, "verify": "make", "max_open_agent_prs": 4,
                                                "approvers": ["carol"]}).status_code == 200
    before = _state(c)
    _refuse(monkeypatch, 3)  # acme/app is created, acme/old pruned, then deleting acme/stale is refused
    r = run("-f", str(_dir(tmp_path, **{"app.yaml": POLICY})), "--prune", "--json")
    assert r.exit_code == 1, r.output
    assert "repo acme/stale (pruned: no file declares it): could not delete it" in r.output
    out = json.loads(r.stdout)
    assert out["failed"]["name"] == "acme/stale" and out["left_changed"] == []
    assert [x["name"] for x in out["rolled_back"]] == ["acme/app", "acme/old"]
    assert _state(c) == before


def test_a_failing_rollback_names_what_it_left_changed(api, tmp_path, monkeypatch):
    c, _ = api
    _refuse(monkeypatch, 3, ("DELETE", "/api/environments/acme-python"))  # the credential's undo still works
    d = _dir(tmp_path, **{"all.yaml": ENV + "---\n" + CRED + "---\n" + POLICY})
    r = run("-f", str(d))
    assert r.exit_code == 1, r.output
    assert "rollback failed for 1 of 2 change(s); these objects are left changed:" in r.output
    assert f"environment acme-python ({d / 'all.yaml'}#1): created, not undone" in r.output
    assert "the other 1 change(s) were rolled back" in r.output
    assert c.get("/api/environments/acme-python").status_code == 200  # left, and said so
    assert all(x["name"] != "anthropic-team" for x in c.get("/api/credentials").json())
    assert c.get("/api/repos").json() == []



def test_only_a_saved_environment_can_be_deleted(api, tmp_path):
    """DELETE /api/environments/{name}: what a rollback removes a created environment with."""
    c, _ = api
    assert c.delete("/api/environments/plain").status_code == 409  # ships with hangar
    assert c.delete("/api/environments/nope").status_code == 404
    assert run("-f", str(_dir(tmp_path, **{"e.yaml": ENV}))).exit_code == 0
    assert c.put("/api/environments/acme-child", content="extends: acme-python\n").status_code == 200
    assert c.delete("/api/environments/acme-python").status_code == 409  # extended
    assert c.delete("/api/environments/acme-child").json() == {"deleted": "acme-child"}
    assert c.delete("/api/environments/acme-python").status_code == 200
    assert c.get("/api/environments/acme-python").status_code == 404


def test_only_admins_create_file_backed_credentials(api, tmp_path):
    """A member's token can store a value of their own, but not a reference to a mounted file (403)."""
    c, _ = api
    c.post("/api/people", json={"id": "mia", "role": "member"})
    tok = c.post("/api/people/mia/tokens", json={"name": "cc"}).json()["token"]
    body = {"name": "mine", "harness": "claude-code", "kind": "api_key",
            "value": "hangar-secret-ref:file:" + str(tmp_path / "secrets" / "mine")}
    r = c.post("/api/credentials", json=body, headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 403 and "only an admin" in r.json()["error"], r.text
    r = c.post("/api/credentials", json=body | {"value": "sk-ant-own-0123456789"},
               headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200, r.text
    r = c.post("/api/credentials", json=body | {"name": "team-ref", "team": True,
               "value": "hangar-secret-ref:file:" + str(tmp_path / "secrets" / "team-ref")})
    assert r.status_code == 200, r.text  # the admin's
