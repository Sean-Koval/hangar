"""The control plane's git caches on the data volume, after an upgrade from a root-run image (KAN-307).

The image runs as uid 1000 since KAN-170. A checkout that the older, root-run image left on the PVC stays owned by
root (fsGroup only changes its group), so git refuses it as "dubious ownership". The Nov 8 deploy of b5323b2 rolled
back on exactly that: doctor's harness repo line read `git fetch failed: fatal: detected dubious ownership in
repository at '/data/harness-repo/...'`.

GIT_TEST_ASSUME_DIFFERENT_OWNER makes git treat every repository as someone else's, so these tests hit that refusal
as any user, CI's included, with no root and no chown. Each one lays the cache out as the old image did (no
variable), then refreshes it the way the new image does (the variable set). The last test fails on a new raw git
call in the control plane, so the next cache added to the data volume can't bring this back.
"""

from __future__ import annotations

import ast
import subprocess
import types
from pathlib import Path

import pytest

from hangar import harness_repo, harness_thaw, plugins

ROOT = Path(__file__).resolve().parents[1]
GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false",
       "-c", "init.defaultBranch=main"]


def _git(*args: str, cwd: Path | None = None) -> str:
    return subprocess.run([*GIT, *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def _foreign_owner(monkeypatch: pytest.MonkeyPatch, upstream: Path) -> None:
    """From here on git treats every repo as another user's. The upstream is trusted, as an https remote needs no
    trust: in a global config, since git drops GIT_CONFIG_COUNT for a file:// remote's upload-pack."""
    cfg = upstream.parent / "gitconfig"
    cfg.write_text(f"[safe]\n\tdirectory = {upstream}\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(cfg))
    monkeypatch.setenv("GIT_TEST_ASSUME_DIFFERENT_OWNER", "1")


@pytest.fixture
def upstream(tmp_path: Path) -> dict:
    work = tmp_path / "work"
    work.mkdir()
    _git("init", "-q", cwd=work)
    (work / "README.md").write_text("one\n")
    _git("add", "-A", cwd=work)
    _git("commit", "-qm", "one", cwd=work)
    one = _git("rev-parse", "HEAD", cwd=work)
    (work / "README.md").write_text("two\n")
    _git("commit", "-qam", "two", cwd=work)
    two = _git("rev-parse", "HEAD", cwd=work)
    bare = tmp_path / "upstream.git"
    _git("clone", "-q", "--bare", str(work), str(bare))
    return {"bare": bare, "url": f"file://{bare}", "work": work, "one": one, "two": two}


def test_the_variable_reproduces_the_refusal(upstream, monkeypatch):
    """Not vacuous: a plain `git -C` on the cache fails here as it did in production."""
    _foreign_owner(monkeypatch, upstream["bare"])
    r = subprocess.run(["git", "-C", str(upstream["work"]), "rev-parse", "HEAD"], capture_output=True, text=True)
    assert r.returncode != 0 and "dubious ownership" in r.stderr


def test_the_harness_repo_refreshes_a_checkout_an_older_image_cloned(upstream, monkeypatch, tmp_path):
    settings = types.SimpleNamespace(data_dir=tmp_path / "data", harness_repo=upstream["url"])
    assert harness_repo._sync(settings, {})["ok"]  # the root-run image's clone
    _git("push", "-q", str(upstream["bare"]), "HEAD:main", cwd=upstream["work"])
    _foreign_owner(monkeypatch, upstream["bare"])
    r = harness_repo._sync(settings, {})  # doctor's "harness repo" line on the new image
    assert r == {"ok": True, "commit": upstream["two"], "error": None}
    assert harness_thaw._git(harness_repo.checkout_dir(settings), "rev-parse", "HEAD").strip() == upstream["two"]


def test_a_git_plugin_mirror_an_older_image_made_still_fetches(upstream, monkeypatch, tmp_path):
    plugins.configure_git(tmp_path / "data" / "plugin-git")
    try:
        p = plugins.Plugin(git=upstream["url"], ref=upstream["one"], name="w")
        assert plugins.fetch(p).is_dir()  # the root-run image's mirror, at commit one
        _foreign_owner(monkeypatch, upstream["bare"])
        tree = plugins.fetch(plugins.Plugin(git=upstream["url"], ref=upstream["two"], name="w"))
        assert (tree / "README.md").read_text() == "two\n"
    finally:
        plugins.configure_git(None)


# A raw git call is fine where git never meets a checkout on the data volume. Each entry says why.
RAW_GIT_OK = {
    "src/hangar/driver.py": "runs in the sandbox, and passes safe.directory=* itself",
    "src/hangar/exchange.py": "the CLI, on the user's own checkout",
}


def test_control_plane_git_calls_trust_the_data_volume():
    """Every `subprocess.*(["git", ...])` in src/hangar goes through harness_repo.safe_git, or is listed above."""
    raw = []
    for path in sorted((ROOT / "src" / "hangar").rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        for node in ast.walk(ast.parse(path.read_text(), rel)):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name) and node.func.value.id == "subprocess"
                    and node.args and isinstance(node.args[0], ast.List) and node.args[0].elts
                    and isinstance(node.args[0].elts[0], ast.Constant) and node.args[0].elts[0].value == "git"):
                raw.append(f"{rel}:{node.lineno}")
    stray = [r for r in raw if r.split(":")[0] not in RAW_GIT_OK]
    assert not stray, (f"git called without harness_repo.safe_git: {stray}. On the data volume a checkout an older "
                       "image made is owned by root, and git refuses it to uid 1000 (KAN-307)")
    assert {r.split(":")[0] for r in raw} == set(RAW_GIT_OK), "RAW_GIT_OK lists a file that no longer needs it"
