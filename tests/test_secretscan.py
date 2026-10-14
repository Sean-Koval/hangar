"""Secrets before anything leaves (--- egress): the scanner's patterns and stored values, file and line in a real
git change, a job that commits a token getting it back to fix before anything is pushed, PR text and artifacts
masked; redaction of encoded forms (hardening B4: `| rev`, `| base64`); build secrets."""

from __future__ import annotations

import base64
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from hangar import buildsecrets, secretscan
from hangar.models import CredentialKind
from hangar.redaction import Redactor, variants
from tests.conftest import scratch_root
from tests.test_jobs import jobs, sessions, world  # noqa: F401 - fixtures: a scripted agent and a git remote

GH = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"
# Every sample is built from pieces, so this file holds no whole token for GitHub's push protection to refuse.
SAMPLES = {
    "GitHub token": GH,
    "GitHub fine-grained token": "github_pat_" + "A1b2C3d4E5_" * 7 + "x" * 5,
    "Slack token": "xo" + "xb-" + "1234567890-0987654321-" + "AbCdEfGhIjKlMnOpQrStUvWx",
    "Slack webhook URL": "https://hooks.sl" + "ack.com/services/" + "T0ABCDEFG/B0ABCDEFG/" + "aBcDeFgHiJkLmNoPqRsTuVwX",
    "AWS access key id": "AK" + "IA2E3F4G5H6J7K8L9M",
    "AWS secret access key": "aws_secret_access_key = " + "wJalrXUtnFEMIK7MDENG" + "bPxRfiCYq1W2e3R4t5Y6",
    "Google API key": "AIza" + "Sy" + "B1c2D3e4F5g6H7i8J9k0L1m2N3o4P5q6R",
    "Anthropic key": "sk-ant-api03-" + "Ab1Cd2Ef3Gh4Ij5Kl6Mn7Op8Qr9St0Uv1Wx2Yz3Ab4Cd5Ef6Gh7Ij8Kl9Mn0Op1Qr2",
    "OpenAI key": "sk-proj-" + "Ab1Cd2Ef3Gh4Ij5Kl6Mn7Op8Qr9St0",
    "private key": "-----BEGIN OPENSSH PRIV" + "ATE KEY-----\n" + "b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQ" * 3,
    "hangar token": "hgr_" + "Zx9" * 15,
}


def test_each_kind_is_found_and_placeholders_are_not():
    for kind, sample in SAMPLES.items():
        found = secretscan.find(f"config = '{sample}'\n")
        assert [k for _, _, k in found] == [kind], (kind, found)
    for placeholder in ("AKIAIOSFODNN7EXAMPLE", "ghp_" + "x" * 36, "sk-proj-your_key_here_xxxxxxxxxxxxxxxxxxxx",
                        "def hgr_token(): pass", "AIza short", "sk-ant-not-a-key"):
        assert secretscan.find(placeholder) == [], placeholder


def test_stored_values_and_their_encodings_are_found(broker):
    value = "s3cr3t-value-for-tests-0123"
    broker.put("thing", "webhook", CredentialKind.GENERIC, value)
    kinds = {k for _, _, k in secretscan.find(f"a {value} b {base64.b64encode(value.encode()).decode()}", broker)}
    assert kinds == {"stored credential", "stored credential, encoded"}


def test_mask_replaces_every_secret_and_names_the_kinds():
    out, kinds = secretscan.mask(f"token {GH} and key {SAMPLES['AWS access key id']} here")
    assert GH not in out and "AKIA" not in out and out.count(secretscan.MASK) == 2
    assert kinds == ["AWS access key id", "GitHub token"]
    assert secretscan.mask("nothing here") == ("nothing here", [])


# ============================================================================ a real git change
def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "-C", str(repo), *args],
                          check=True, capture_output=True, text=True).stdout


@pytest.fixture
def repo():
    root = Path(tempfile.mkdtemp(prefix="scan-", dir=scratch_root()))
    r = root / "r"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    (r / "app.py").write_text(f"OLD = '{SAMPLES['Slack token']}'\nx = 1\ny = 2\n")
    git(r, "add", ".")
    git(r, "commit", "-qm", "base")
    yield r
    shutil.rmtree(root, ignore_errors=True)


def bundle(repo: Path, base: str = "main") -> str:
    out = repo.parent / "bundle"
    subprocess.run(["bash", "-c", secretscan.scan_script(str(repo), base, str(out))], check=True)
    return out.read_text()


@pytest.mark.skipif(not shutil.which("git"), reason="needs git")
def test_hits_name_the_file_and_line_of_added_lines_only(repo):
    git(repo, "checkout", "-qb", "work")
    (repo / "app.py").write_text(f"x = 1\ny = 2\nTOKEN = '{GH}'\n")  # the old Slack token is removed: not leaving
    (repo / "docs").mkdir()
    (repo / "docs" / "notes.md").write_text(f"line\nline\nline\nkey {SAMPLES['AWS access key id']}\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", f"add things\n\nuse {SAMPLES['OpenAI key']}")
    (repo / "scratch.txt").write_text(f"one\n{SAMPLES['Anthropic key']}\n")  # untracked, would be committed
    (repo / "blob.bin").write_bytes(b"\0\1" + GH.encode())  # binary: skipped
    sha = git(repo, "rev-parse", "HEAD").strip()
    hits = secretscan.scan_bundle(bundle(repo))
    assert sorted(h.line() for h in hits) == sorted([
        "app.py:3: GitHub token", "docs/notes.md:4: AWS access key id", "scratch.txt:2: Anthropic key",
        f"commit {sha[:12]} message: OpenAI key"])
    assert all(GH not in h.line() for h in hits)


@pytest.mark.skipif(not shutil.which("git"), reason="needs git")
def test_a_change_that_cant_be_read_fails_the_scan(repo):
    """Hangar never pushes what it couldn't check: an unknown base must not look like an empty change."""
    r = subprocess.run(["bash", "-c", secretscan.scan_script(str(repo), "no-such-base", str(repo.parent / "b"))])
    assert r.returncode not in (0, 141)


@pytest.mark.skipif(not shutil.which("git"), reason="needs git")
def test_a_big_change_is_scanned_fast(repo):
    import time

    (repo / "big.txt").write_text("".join(f"line {i} nothing to see here at all\n" for i in range(400_000))
                                  + f"leak {GH}\n")
    text = bundle(repo)
    assert len(text) > 15_000_000
    t0 = time.monotonic()
    hits = secretscan.scan_bundle(text)
    assert time.monotonic() - t0 < 5
    assert [h.line() for h in hits] == ["big.txt:400001: GitHub token"]


# ============================================================================ a job that commits a token
LEAKER = r"""#!/usr/bin/env bash
if grep -q "didn't push your branch" "$HANGAR_PROMPT_FILE"; then
  grep -q "config.py:2: GitHub token" "$HANGAR_PROMPT_FILE" || { echo "no location in the prompt"; exit 9; }
  grep -q "{TOKEN}" "$HANGAR_PROMPT_FILE" && { echo "the value is in the prompt"; exit 8; }
  printf 'A = 1\nTOKEN = os.environ["TOKEN"]\n' > config.py && git add config.py && git commit -q --amend --no-edit
  echo "moved the token to the environment" > "$HANGAR_JOB_DIR/RESULT.md"
else
  printf 'A = 1\nTOKEN = "{TOKEN}"\n' > config.py && git add config.py && git commit -qm "config"
  echo "added config" > "$HANGAR_JOB_DIR/RESULT.md"
fi
"""


@pytest.mark.skipif(not shutil.which("tmux") or not shutil.which("git"), reason="needs tmux and git")
async def test_a_job_that_commits_a_token_fixes_it_before_anything_is_pushed(jobs, world, monkeypatch):  # noqa: F811
    from hangar.harnesses import HARNESSES, Harness
    from hangar.jobs import Caller, SubmitJob
    from tests.test_jobs import DONE, _remote_has, _until

    script = world["root"] / "leaker.sh"
    script.write_text(LEAKER.replace("{TOKEN}", GH))
    monkeypatch.setitem(HARNESSES, "scripted", Harness(name="scripted", title="Scripted", run=f"bash {script}"))
    pushes = []
    real = jobs._sh

    async def spy(job, command, **kw):
        if command.startswith("git push"):
            pushes.append(subprocess.run(["git", "--git-dir", str(world["remote"]), "branch", "--list"],
                                         capture_output=True, text=True).stdout)
        return await real(job, command, **kw)

    monkeypatch.setattr(jobs, "_sh", spy)
    job = jobs.submit(SubmitJob(instructions="add config", repo=world["url"], environment="jobenv"),
                      Caller("svc", "svc"))
    job = await _until(jobs, job.id, DONE, timeout=40)
    msgs = [e.message for e in job.events]
    assert job.state.value == "succeeded", "\n".join(msgs)
    assert [t.trigger for t in job.turns] == [None, "secrets"]
    assert any("a secret is in the change; sending it back" in m for m in msgs)
    assert len(pushes) == 1 and _remote_has(world["remote"], job.branch)  # pushed once, after the fix
    log = subprocess.run(["git", "--git-dir", str(world["remote"]), "log", "-p", job.branch], capture_output=True,
                         text=True).stdout
    assert GH not in log
    assert GH not in (job.result.summary or "") and all(GH not in m for m in msgs)


@pytest.mark.skipif(not shutil.which("tmux") or not shutil.which("git"), reason="needs tmux and git")
async def test_out_of_rounds_the_job_fails_unpushed(jobs, world, monkeypatch):  # noqa: F811
    from hangar.harnesses import HARNESSES, Harness
    from hangar.jobs import Caller, SubmitJob
    from tests.test_jobs import DONE, _remote_has, _until

    script = world["root"] / "stubborn.sh"
    script.write_text(f'printf "T = \\"{GH}\\"\\n" > t.py && git add t.py && git commit -qm t\n')
    monkeypatch.setitem(HARNESSES, "scripted", Harness(name="scripted", title="Scripted", run=f"bash {script}"))
    jobs.settings.secret_scan_rounds = 1
    job = jobs.submit(SubmitJob(instructions="x", repo=world["url"], environment="jobenv"), Caller("svc", "svc"))
    job = await _until(jobs, job.id, DONE, timeout=40)
    assert job.state.value == "failed" and job.reason == "secret_found", [e.message for e in job.events]
    assert not _remote_has(world["remote"], job.branch)
    assert "t.py:1: GitHub token" in job.events[-2].message or any("t.py:1" in e.message for e in job.events)


def test_pr_text_and_comments_are_masked(fleet):
    events = []
    svc = SimpleNamespace(settings=fleet.settings, broker=fleet.broker,
                          _event=lambda job, m, *a, **k: events.append(m))
    out = secretscan.clean(svc, object(), f"Fixes it. Token was {GH}.", "the PR body")
    assert GH not in out and secretscan.MASK in out and "GitHub token" in events[0] and GH not in events[0]
    svc.settings.secret_scan = "off"
    assert secretscan.clean(svc, None, GH, "x") == GH


def test_artifacts_are_masked_or_withheld(broker):
    value = "stored-artifact-secret-123"
    broker.put("thing", "webhook", CredentialKind.GENERIC, value)
    assert secretscan.artifact(broker, f"report {value} {GH}".encode()) == (
        f"report *** {secretscan.MASK}".encode())
    assert secretscan.artifact(broker, b"\xff\xfe binary " + value.encode()) is None
    assert secretscan.artifact(broker, b"\xff\xfe binary") == b"\xff\xfe binary"


# ============================================================================ redaction (hardening B4)
SECRET = "Sup3rS3cretValue-0123456789"


def test_b4_rev_and_encodings_are_redacted(broker):
    """B4: `printf %s "$X" | rev` returned the secret reversed past broker.redact."""
    broker.put("leak", "webhook", CredentialKind.GENERIC, SECRET)
    r = broker.redact
    b = SECRET.encode()
    for shown in (SECRET[::-1], b.hex(), b.hex().upper(), base64.b64encode(b).decode(),
                  base64.urlsafe_b64encode(b).decode().rstrip("="),
                  base64.b64encode(b"X=" + b + b"\n").decode(),  # inside a longer string, any alignment
                  base64.b64encode(b"AB=" + b).decode(), base64.b64encode(b"ABCD=" + b).decode(),
                  ):
        assert "***" in r(f"out: {shown}\n"), shown
        assert SECRET not in r(shown)
    # wrapped at 76 columns, as base64(1) prints it
    long_ = "x" * 50 + SECRET + "y" * 50
    wrapped = base64.encodebytes(long_.encode()).decode()
    assert "\n" in wrapped.strip()
    out = r(wrapped)
    core = variants(SECRET)
    assert not any(v in out.replace("\n", "") for v in core)


def test_short_values_get_no_encodings_and_unrelated_text_is_untouched():
    red = Redactor(["shortval", SECRET])
    assert "shortval"[::-1] not in red.encoded
    text = "an ordinary log line with hex deadbeef and base64 aGVsbG8gd29ybGQ=\n" * 100
    assert red.text(text) == text


def test_redaction_is_rebuilt_when_credentials_change(broker):
    broker.put("a", "webhook", CredentialKind.GENERIC, SECRET)
    assert broker.redact(SECRET[::-1]) == "***"
    broker.delete("a")
    assert broker.redact(SECRET[::-1]) == SECRET[::-1]


# ============================================================================ build secrets
def test_build_secret_mapping_resolution_and_refusals(broker, settings):
    from hangar.envspec import EnvSpec

    broker.settings = settings
    broker.put("npm-ci", "generic", CredentialKind.GENERIC, "npm_tok_123456\n", env_var="X")
    broker.set_exposure("npm-ci", "control_plane")  # a build-only credential: no sandbox can map it
    spec = EnvSpec(name="e", build_secrets={"NPM_TOKEN": "npm-ci", "OTHER": "missing"})
    policy = SimpleNamespace(build_secrets={"OTHER": "npm-ci"})
    m = buildsecrets.mapping(spec, policy)
    assert m == {"NPM_TOKEN": "npm-ci", "OTHER": "npm-ci"}
    assert buildsecrets.resolve(broker, m, ["NPM_TOKEN"]) == {"NPM_TOKEN": b"npm_tok_123456"}
    with pytest.raises(buildsecrets.BuildSecretError, match="build secret NOPE isn't configured"):
        buildsecrets.resolve(broker, m, ["NOPE"])
    with pytest.raises(buildsecrets.BuildSecretError, match="OTHER"):
        buildsecrets.resolve(broker, {"OTHER": "missing"})
    broker.put("hook", "webhook", CredentialKind.GENERIC, "sign-key-1234")
    with pytest.raises(buildsecrets.BuildSecretError, match="signing key"):
        buildsecrets.resolve(broker, {"K": "hook"})
    with pytest.raises(ValueError):
        EnvSpec(name="e", build_secrets={"1BAD": "x"})
    assert "build_secrets" in __import__("hangar.identity").identity.ADMIN_FIELDS


def test_docker_args_write_0600_files_that_go_away():
    with buildsecrets.docker_args({"NPM_TOKEN": b"v1", "PIP_INDEX": b"v2"}) as args:
        assert args[0] == "--secret" and args[1].startswith("id=NPM_TOKEN,src=")
        p = Path(args[1].split("src=")[1])
        assert p.read_bytes() == b"v1" and oct(p.stat().st_mode & 0o777) == "0o600"
        assert oct(p.parent.stat().st_mode & 0o777) == "0o700"
        assert all(b"v1" not in a.encode() for a in args)  # values never on the command line
    assert not p.parent.exists()
    with buildsecrets.docker_args({}) as args:
        assert args == []
    assert buildsecrets.cloud_build_refusal({}) is None
    assert "NPM_TOKEN" in buildsecrets.cloud_build_refusal({"NPM_TOKEN": "npm-ci"})


async def test_leak_scan_names_the_file_and_id_never_the_value(fleet):
    from hangar.models import Sandbox

    prov = fleet.providers["docker"]
    sb = Sandbox(name="b", environment="plain", provider="docker")
    await prov.create(sb, "img")
    box = prov.boxes[sb.id]
    (box / "workspace" / "app").mkdir(parents=True)
    (box / "workspace" / "app" / ".npmrc").write_text("//registry.npmjs.org/:_authToken=npm_tok_123456\n")
    (box / "workspace" / "app" / "ok.txt").write_text("nothing\n")
    found = await buildsecrets.leak_scan(prov, sb, {"NPM_TOKEN": b"npm_tok_123456", "OTHER": b"not-present-xyz"},
                                         workspace="/workspace", home="/home/agent")
    assert len(found) == 1 and found[0].endswith("/workspace/app/.npmrc: NPM_TOKEN")
    assert "npm_tok_123456" not in found[0]
    assert not any(n.startswith("hangar-bs-scan") for n in os.listdir(box / "tmp"))


# ============================================================================ review
@pytest.mark.skipif(not shutil.which("git"), reason="needs git")
def test_a_secret_committed_and_deleted_later_still_leaves_and_is_found(repo):
    """`git push` sends every commit, so a token added in one commit and removed in the next is on GitHub for good;
    the net diff since the base doesn't show it. The commonest accident: commit .env, then delete it."""
    git(repo, "checkout", "-qb", "work")
    (repo / ".env").write_text(f"A=1\nTOKEN={GH}\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "wip")
    (repo / ".env").unlink()
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "drop .env")
    assert [h.line() for h in secretscan.scan_bundle(bundle(repo))] == [".env:2: GitHub token"]


def test_code_that_handles_pem_isnt_a_private_key():
    """A PEM header alone is what key-handling code checks for (gateway.py has one); a key has a body."""
    header = "-----BEGIN RSA PRIV" + "ATE KEY-----"
    for code in (f'if pem.startswith("{header}"):', f'"private_key": "{header}..."',
                 f"raise ValueError('expected {header}')"):
        assert secretscan.find(code) == [], code
    body = "MIIEowIBAAKCAQEA1x2y3z4" * 4
    for key in (f"{header}\n{body}\n", f'"private_key": "{header}\\n{body}\\n"'):
        assert [k for _, _, k in secretscan.find(key)] == ["private key"], key


def test_slack_shaped_test_values_arent_tokens():
    for fake in ("xo" + "xb-test-token", "xo" + "xb-1111-2222-slacktoken", "xo" + "xb-0123456789abcdef"):
        assert secretscan.find(f"token = '{fake}'") == [], fake


def test_more_common_token_formats_are_found():
    samples = {
        "npm token": "npm_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8",
        "PyPI token": "pypi-" + "AgEIcHlwaS5vcmc" + "CJGQ0ZjE4YzM1LTk4ZDQtNGQ5Yy1iYzg1LWI2YzQ4NGE5ZjAwZQACKlsz",
        "GitLab token": "glpat-" + "a1B2c3D4e5F6g7H8i9J0",
        "Stripe key": "sk_" + "live_" + "51H8a1B2c3D4e5F6g7H8i9J0k1",
        "Hugging Face token": "hf_" + "aBcDeFgHiJkLmNoPqRsTuVwXyZaBcDeFgH",
    }
    for kind, sample in samples.items():
        assert [k for _, _, k in secretscan.find(f"x = '{sample}'\n")] == [kind], kind


def test_values_of_12_to_15_characters_get_every_encoding_the_docs_promise():
    from urllib.parse import quote

    for n in (12, 13, 14, 15):
        value = "Q7xLm2Rv9/pW8sKd"[:n]
        red = Redactor([value])
        b = value.encode()
        for shown in (value[::-1], base64.b64encode(b).decode(), base64.b64encode(b"x" + b).decode(),
                      base64.b64encode(b"xy" + b).decode(), b.hex(), quote(value, safe=""), quote(value),
                      quote(value, safe="").replace("%2F", "%2f")):
            assert red.text(f"out: {shown}\n") != f"out: {shown}\n", (n, shown)


def test_no_test_file_holds_a_whole_secret_shaped_token():
    """GitHub's push protection refuses them, and hangar's own scan would stop a job that edits the file."""
    here = Path(__file__).parent
    for name in ("test_secretscan.py", "test_egress.py"):
        assert secretscan.find((here / name).read_text()) == [], name
