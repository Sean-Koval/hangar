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
    assert "**Security posture.** hangar treats every sandbox as hostile." in INDEX
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


GUIDE = (ROOT / "docs" / "guide.md").read_text()


def commands(text: str, heading: str) -> str:
    """The first fenced bash block under a heading."""
    m = re.search(r"```bash\n(.*?)```", section(text, heading), re.S)
    assert m, f"no bash block under '## {heading}'"
    return m.group(1)


def test_there_is_one_quickstart_and_the_readme_repeats_it():
    """The docs home sends people to the guide; the README's five commands are the guide's five commands."""
    assert "## Quickstart" not in INDEX and "## Getting started" not in INDEX
    assert "](guide.md#quickstart)" in INDEX
    assert commands(README, "Quickstart") == commands(GUIDE, "Quickstart")


def test_the_docs_home_is_a_front_door():
    """A hero, three audiences, the diagram, one console image, the features table and three links: outside the
    features table, under 500 words of prose (alt text and markup aside), and no more than three images."""
    prose = INDEX.replace(section(INDEX, "Features"), "")
    words = re.sub(r"!\[[^\]]*\]\([^)]*\)|aria-label=\"[^\"]*\"|<[^>]+>|\]\([^)]*\)|[|*`#-]", " ", prose).split()
    assert len(words) < 500, len(words)
    assert len(re.findall(r"^!\[", INDEX, re.M)) <= 3, "one screenshot and the diagram in both flavors"


def test_the_fleet_views_are_explained_on_the_console_page_only():
    """Board, Bays, Columns and Owners are walked through in docs/console.md; the README and the docs home name the
    Fleet once and link there."""
    for text, name in ((README, "README.md"), (INDEX, "docs/index.md")):
        assert not re.search(r"\bBays\b.*\bColumns\b.*\bOwners\b", text), f"{name} explains the Fleet views"
        assert "console.md)" in text, f"{name} does not link to the console page"
    console = (ROOT / "docs" / "console.md").read_text()
    assert all(f"\n### {v}\n" in console for v in ("Board", "Bays", "Columns", "Owners"))


def prose(text: str) -> str:
    """Markdown without its fenced blocks, inline code and link targets: what a person reads."""
    text = re.sub(r"^\s*(```|~~~).*?^\s*\1\s*$", "", text, flags=re.M | re.S)
    return re.sub(r"`[^`]*`|\]\([^)]*\)", "", text)


def test_the_product_name_is_lowercase_in_prose():
    """The brand book: product names keep their case everywhere, even at the start of a sentence. hangar is
    lowercase in the README and every docs page; code, commands and link targets are left alone."""
    hits = []
    for path in [ROOT / "README.md", *sorted((ROOT / "docs").rglob("*.md"))]:
        for line in prose(path.read_text()).splitlines():
            if re.search(r"\bHangar\b", line):
                hits.append(f"{path.relative_to(ROOT)}: {line.strip()[:80]}")
    assert not hits, "write 'hangar', not 'Hangar':\n" + "\n".join(hits[:20])
