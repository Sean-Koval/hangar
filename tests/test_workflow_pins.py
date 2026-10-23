"""Every third-party action is pinned by full commit SHA, with its version in a comment (KAN-171, security review M-6).

A tag can be moved by whoever controls the action's repository, and several jobs hold `id-token: write`, the deploy
identity or an App key. Dependabot (.github/dependabot.yml) bumps the SHAs and their comments."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).parents[1]
USES = re.compile(r"^\s*(?:-\s+)?uses:\s*(\S+)")
PINNED = re.compile(r"^[\w.-]+/[\w.-]+(?:/[\w./-]+)?@[0-9a-f]{40}$")
COMMENT = re.compile(r"@[0-9a-f]{40}\s+#\s*v\d+(?:\.\d+)*")


def _uses() -> list[tuple[str, int, str, str]]:
    out = []
    for wf in sorted((ROOT / ".github").rglob("*.y*ml")):
        for n, line in enumerate(wf.read_text().splitlines(), 1):
            if (m := USES.match(line)) and not m.group(1).startswith(("./", "docker://")):
                out.append((str(wf.relative_to(ROOT)), n, m.group(1), line))
    return out


def test_every_action_is_pinned_by_commit_sha_with_its_version():
    found = _uses()
    assert found, "no workflows found"
    unpinned = [f"{f}:{n}: {ref}" for f, n, ref, _ in found if not PINNED.match(ref)]
    assert not unpinned, "pin these by full commit SHA (`owner/repo@<sha>  # vX.Y.Z`):\n" + "\n".join(unpinned)
    no_version = [f"{f}:{n}" for f, n, _, line in found if not COMMENT.search(line)]
    assert not no_version, "add the version after the SHA (`  # vX.Y.Z`):\n" + "\n".join(no_version)


def test_dependabot_bumps_the_pinned_actions():
    text = (ROOT / ".github" / "dependabot.yml").read_text()
    assert re.search(r'package-ecosystem:\s*"?github-actions"?', text)
