"""Check a release tag against hangar's version and changelog, as the suite's release checklist asks (KAN-145).

usage: python3 scripts/release_check.py vX.Y.Z > notes.md

Fails when the tag isn't vX.Y.Z[-pre], when pyproject.toml's version disagrees with it, or when CHANGELOG.md has no
`## vX.Y.Z` section. Prints the section's body, which the GitHub release carries above the image digests.
flightdeck's scripts/release_check.py is the same check; release.yml runs this one before anything is built.
"""

from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def version(root: Path = ROOT) -> str:
    return tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]


def section(ver: str, root: Path = ROOT) -> str | None:
    """The body of CHANGELOG.md's `## v<ver>` section (any title after the version), or None."""
    text = (root / "CHANGELOG.md").read_text()
    m = re.search(rf"^## v{re.escape(ver)}(?![\w.-])[^\n]*\n(.*?)(?=^## |\Z)", text, re.M | re.S)
    return m.group(1).strip() if m else None


def check(tag: str, root: Path = ROOT) -> tuple[list[str], str]:
    """(problems, notes) for a tag."""
    if not re.fullmatch(r"v\d+\.\d+\.\d+(-[0-9A-Za-z.]+)?", tag):
        return [f"{tag!r} is not vX.Y.Z[-pre]"], ""
    want = tag[1:]
    problems = []
    if (have := version(root)) != want:
        problems.append(f"pyproject.toml says {have!r}, the tag says {want!r}")
    notes = section(want, root)
    if notes is None:
        problems.append(f"CHANGELOG.md has no '## v{want}' section: move the Unreleased entries under it")
    elif not notes:
        problems.append(f"CHANGELOG.md's '## v{want}' section is empty")
    return problems, notes or ""


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print(__doc__, file=sys.stderr)
        return 2
    problems, notes = check(argv[0])
    for p in problems:
        print(f"release check: {p}", file=sys.stderr)
    if problems:
        return 1
    try:
        print(notes)
    except BrokenPipeError:  # `| head`: the reader left; nothing is wrong with the tag
        sys.stderr.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
