"""The front page (README.md) and the docs home page (docs/index.md) keep what #151 asked of them: a merge dropped the
README's security posture once (4fc0d45c), unnoticed. scripts/ci/select_tests.py runs this for changes to either."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).parents[1]
README = (ROOT / "README.md").read_text()
INDEX = (ROOT / "docs" / "index.md").read_text()


def section(text: str, heading: str) -> str:
    m = re.search(rf"^## {re.escape(heading)}\n(.*?)(?=^## |\Z)", text, re.M | re.S)
    assert m, f"no '## {heading}' section"
    return m.group(1)


def test_the_readme_keeps_its_security_posture():
    body = section(README, "Security posture")
    assert README.index("\n## Features") < README.index("\n## Security posture"), "it follows the features"
    assert "treats every sandbox as hostile" in body
    assert "](docs/security.md)" in body and "](docs/upgrading.md#the-safe-release" in body


def test_the_docs_home_page_keeps_its_security_posture():
    assert "**Security posture.** Hangar treats every sandbox as hostile." in INDEX
    assert "](security.md)" in INDEX and "](upgrading.md#the-safe-release" in INDEX
