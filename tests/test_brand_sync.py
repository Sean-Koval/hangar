"""The vendored brand files (README banner, favicon, header mark, social preview) are what agent-factory generated.

scripts/sync-brand.sh copies them from agent-factory's brand/hangar/ and records the commit and each file's sha256 in
docs/assets/brand/SOURCE. A hand edit, or a file copied from somewhere else, fails here; the docs workflow also runs
`sh scripts/sync-brand.sh --check`, which fetches the files again at that commit and compares.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

ROOT = Path(__file__).parents[1]
SOURCE = ROOT / "docs" / "assets" / "brand" / "SOURCE"
SYNC = (ROOT / "scripts" / "sync-brand.sh").read_text()


def records() -> dict[str, str]:
    return {m[2]: m[1] for m in re.finditer(r"^sha256 ([0-9a-f]{64}) (\S+)$", SOURCE.read_text(), re.M)}


def test_the_source_names_a_commit():
    assert re.search(r"^commit [0-9a-f]{40}$", SOURCE.read_text(), re.M), "SOURCE has no agent-factory commit"


def test_every_synced_file_is_recorded():
    block = re.search(r'^FILES="\n(.*?)^"', SYNC, re.M | re.S)
    assert block
    dests = {line.split()[1] for line in block.group(1).splitlines() if line.strip()}
    assert dests == set(records()), "SOURCE and sync-brand.sh's FILES list different files; re-run the sync"


def test_the_vendored_files_are_unmodified():
    changed = [dest for dest, digest in records().items()
               if hashlib.sha256((ROOT / dest).read_bytes()).hexdigest() != digest]
    assert not changed, f"edited after the sync (change agent-factory and re-run scripts/sync-brand.sh): {changed}"
