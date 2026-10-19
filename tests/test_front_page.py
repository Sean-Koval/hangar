"""The front page (README.md) and the docs home page (docs/index.md) keep what #151 asked of them, and agree: a merge
dropped the README's security posture once (4fc0d45c), unnoticed, and their feature tables had drifted apart.
scripts/ci/select_tests.py runs this for changes to either."""

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


def features(text: str, prefix: str) -> list[str]:
    """The features table's rows, with relative links written from the docs directory."""
    rows = [line for line in section(text, "Features").splitlines() if line.startswith("|")]
    return [re.sub(rf"\]\({re.escape(prefix)}", "](", row) for row in rows]


def test_both_pages_show_one_features_table_with_a_status_per_row():
    readme, index = features(README, "docs/"), features(INDEX, "")
    assert readme == index, "README.md and docs/index.md show different features"
    assert readme[0] == "| Feature | What it gives your team | Status |"
    statuses = [row.rstrip(" |").rsplit("| ", 1)[1] for row in readme[2:]]
    assert statuses and all(re.fullmatch(r"Shipped|Shadow|Partial|Planned \(\[#\d+\]\(https://github\.com/Sean-Koval/"
                                         r"hangar/issues/\d+\)\)", s) for s in statuses), statuses
