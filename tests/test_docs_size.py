"""Every page in the docs site's map stays short enough to read (KAN-106).

A newcomer cannot find the one thing they need in a 20,000-word page. Pages are capped at WORD_CAP words of prose
(fenced code and HTML tags left out). The pages that are over it today are listed with a ceiling each and a reason;
the list shrinks, it never grows: a page that drops under the cap is removed from it, and one that grows past its
ceiling fails here. Design records (the specs) are read as a whole and are listed as such.
"""
from __future__ import annotations

import pathlib
import re

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
WORD_CAP = 2500

# page -> (ceiling, why it is over the cap)
OVER_CAP = {
    "security/threat-model.md": (2600, "one table: every goal of an attacker in a sandbox, read as a whole"),
    "releases/team-release-changes.md": (4000, "one table of 53 settings the team release changes; "
                                         "half a table hides rows"),
    "environments.md": (8600, "to split by task (image, services, mcp, cloud, the repo's own file)"),
    "gke.md": (7500, "to split: install, images and packs, operations"),
    "team-scale.md": (7000, "to split: sizing, caps, the reconciler"),
    "steering.md": (6000, "to split: the turn, approvals, the uid split"),
    "observability.md": (5500, "to split: events, metrics, traces"),
    "landing.md": (5400, "to split: the GitHub App, approvals, review rounds; KAN-140 added the inbox ack and denied"),
    "repo-policy.md": (4800, "to split: the file, shadows, caps"),
    "microvm.md": (4400, "to split by provider"),
    "acceptance.md": (4100, "to split: core checks, the rest"),
    "sessions.md": (3900, "to split: checkpoints, resume"),
    "team.md": (3800, "to split: people, credentials, linked accounts"),
    "harnesses.md": (3900, "to split: packs, models, the canary"),
    "intake.md": (3700, "to split: Jira, GitHub Projects"),
    "api/traces.md": (3400, "one versioned contract"),
    "computer-use.md": (3100, ""),
    "harness-lock.md": (3300, ""),
    "research/ax.md": (3100, "a research note, read as a whole"),
    "guide.md": (2900, "to move development notes out"),
    "cicd.md": (2700, ""),
    "e2b.md": (2700, ""),
    "api/outbox.md": (2600, "one versioned contract"),
    "specs/0002-results-and-landing.md": (10500, "design record"),
    "specs/0003-sandbox-security.md": (9400, "design record"),
    "specs/0010-sessions.md": (2800, "design record"),
    "specs/0012-cutting-edge.md": (7000, "design record"),
    "specs/0013-e2b-cube-fast-sandboxes.md": (9200, "design record"),
    "specs/0014-sister-tool-interfaces.md": (4600, "design record; KAN-140 added the stage estimate's row"),
    "specs/0015-governance-decision-point.md": (5700, "design record"),
}


def nav_pages() -> list[str]:
    """Every page mkdocs.yml's nav lists, in order."""
    class Loader(yaml.SafeLoader):
        pass
    Loader.add_multi_constructor("tag:yaml.org,2002:python/", lambda loader, suffix, node: None)
    cfg = yaml.load((ROOT / "mkdocs.yml").read_text(), Loader=Loader)
    out: list[str] = []

    def walk(item):
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, dict):
            for v in item.values():
                walk(v)
        elif isinstance(item, list):
            for v in item:
                walk(v)
    walk(cfg["nav"])
    return out


def words(page: str) -> int:
    text = (DOCS / page).read_text()
    text = re.sub(r"^---.*?---\n", "", text, flags=re.S)
    text = re.sub(r"```.*?```", "", text, flags=re.S)
    text = re.sub(r"<[^>]+>", "", text)
    # a Day twin (#only-light) repeats its Night image's alt text
    text = re.sub(r"^!\[[^\]]*\]\([^)]*#only-light\)$", "", text, flags=re.M)
    return len(re.findall(r"[A-Za-z0-9'’-]+", text))


def test_every_nav_page_exists():
    missing = [p for p in nav_pages() if not (DOCS / p).is_file()]
    assert not missing, f"nav lists pages that don't exist: {missing}"


def test_nav_pages_stay_under_the_word_cap():
    over = {}
    for page in nav_pages():
        n = words(page)
        ceiling = OVER_CAP.get(page, (WORD_CAP,))[0]
        if n > ceiling:
            over[page] = (n, ceiling)
    assert not over, "pages over their word cap (split them, or raise the ceiling with a reason): " + ", ".join(
        f"{p} {n} > {c}" for p, (n, c) in over.items())


def test_the_allowlist_only_names_pages_that_need_it():
    pages = set(nav_pages())
    stale = [p for p in OVER_CAP if p not in pages or words(p) <= WORD_CAP]
    assert not stale, f"remove from OVER_CAP (not in the nav, or under {WORD_CAP} words now): {stale}"
