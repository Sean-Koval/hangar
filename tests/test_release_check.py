"""KAN-145: a release tag must match the version and have a changelog section (scripts/release_check.py)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import release_check  # noqa: E402


def _repo(tmp_path: Path, ver: str, changelog: str) -> Path:
    (tmp_path / "pyproject.toml").write_text(f'[project]\nname = "hangar"\nversion = "{ver}"\n')
    (tmp_path / "CHANGELOG.md").write_text(changelog)
    return tmp_path


LOG = "# Changelog\n\n## Unreleased\n\n- next\n\n## v1.0.0: the release\n\n- one\n- two\n\n## v0.2.0\n\n- old\n"


def test_a_matching_tag_prints_its_section(tmp_path: Path):
    problems, notes = release_check.check("v1.0.0", _repo(tmp_path, "1.0.0", LOG))
    assert problems == [] and notes == "- one\n- two"


@pytest.mark.parametrize(("tag", "ver", "log", "want"), [
    ("1.0.0", "1.0.0", LOG, "is not vX.Y.Z"),
    ("v1.0.0", "0.2.0", LOG, "pyproject.toml says '0.2.0'"),
    ("v1.1.0", "1.1.0", LOG, "no '## v1.1.0' section"),
    ("v1.0.0", "1.0.0", "## v1.0.0\n\n## v0.2.0\n- old\n", "is empty"),
    ("v1.0.0", "1.0.0", "## v1.0.0-rc.1\n\n- rc\n", "no '## v1.0.0' section"),  # a prefix is not the version
])
def test_a_tag_that_breaks_the_checklist_is_refused(tmp_path: Path, tag, ver, log, want):
    problems, _ = release_check.check(tag, _repo(tmp_path, ver, log))
    assert any(want in p for p in problems), problems


def test_main_matches_the_current_tag():
    """The repo as it stands passes for the version pyproject.toml names (its changelog section exists)."""
    ver = release_check.version()
    problems, notes = release_check.check(f"v{ver}")
    assert problems == [] and notes
