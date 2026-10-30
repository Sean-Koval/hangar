"""Docs a new engineer reads first must not mislead them (KAN-219, split from KAN-81).

* `--kind oauth_token` stores a subscription login (Claude Pro or Max). Jobs refuse those (KAN-74,
  `teamcreds.py`), so the docs show the command only in sections about a person's own interactive use. The check is
  a grep over README.md and docs/: each hit's file and nearest heading must be on ALLOWED. To show the command
  somewhere new, put it under such a heading and add that heading here.
* The runbooks index, docs/runbooks/README.md, exists and is linked from README.md and docs/guide.md.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
NEEDLE = "--kind oauth_token"

# (file, nearest heading) where a subscription login may be shown: a person's own, interactive sessions only.
ALLOWED = {
    ("docs/team.md", "Your own interactive sessions"),
    ("docs/team/subscription-logins.md", "Subscription logins"),  # the page saying why jobs refuse them
}

_FENCE = re.compile(r"^\s*(```|~~~)")
_HEADING = re.compile(r"^#{1,6}\s+(.*?)\s*#*\s*$")


def _pages() -> list[Path]:
    return [ROOT / "README.md"] + sorted(DOCS.rglob("*.md"))


def hits(text: str) -> list[tuple[int, str | None]]:
    """(line number, nearest heading above it) for each line holding NEEDLE. A `#` line in a code fence is a shell
    comment, not a heading."""
    out: list[tuple[int, str | None]] = []
    heading: str | None = None
    fenced = False
    for n, line in enumerate(text.splitlines(), 1):
        if _FENCE.match(line):
            fenced = not fenced
        elif not fenced and (m := _HEADING.match(line)):
            heading = m.group(1)
        if NEEDLE in line:
            out.append((n, heading))
    return out


def test_hits_ignores_comments_in_code_fences():
    text = ("# Top\n\n## Your own interactive sessions\n\n"
            "```bash\n# a comment\nhangar cred add x --kind oauth_token\n```\n")
    assert hits(text) == [(7, "Your own interactive sessions")]
    assert hits("## Team credentials\n\nnever `--kind oauth_token`\n") == [(3, "Team credentials")]


def test_subscription_logins_only_in_personal_interactive_sections():
    bad = []
    for page in _pages():
        rel = page.relative_to(ROOT).as_posix()
        for n, heading in hits(page.read_text(encoding="utf-8")):
            if (rel, heading) not in ALLOWED:
                bad.append(f"{rel}:{n} under {heading!r}")
    assert not bad, (f"{NEEDLE} (a subscription login) outside a personal, interactive section; jobs refuse those "
                     "(docs/team/subscription-logins.md). Move it, or add the section to ALLOWED: " + ", ".join(bad))


def test_allowed_sections_still_exist():
    for rel, heading in ALLOWED:
        assert heading in {h for _, h in hits((ROOT / rel).read_text(encoding="utf-8"))}, (rel, heading)


def test_runbooks_index_is_linked():
    assert (DOCS / "runbooks" / "README.md").is_file()
    assert "(docs/runbooks/README.md)" in (ROOT / "README.md").read_text(encoding="utf-8")
    assert "(runbooks/README.md)" in (DOCS / "guide.md").read_text(encoding="utf-8")
