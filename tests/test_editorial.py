"""The suite's editorial rule (agent-factory AGENTS.md, "Writing for the site"), measured on hangar's docs.

A page answers one question for one reader: paragraphs of at most 60 words, sentences of at most 25 (a semicolon
ends one) and at most 700 words of prose on a page. Prose is the Markdown in docs/ and README.md with front matter,
fenced code, HTML, headings, images, link targets and tables left out; a table cell is still held to the
paragraph and sentence limits. The measure is agent-factory's tests/test_editorial.py, ported.

Most pages were written before the rule, so this is a ratchet, not a gate. LONG_PAGES lists each page that breaks
it today with a reason and two ceilings: its words of prose and its count of long paragraphs and sentences. A page
fails when it goes past a ceiling; a page not listed (a new page, or one already split) must keep the whole rule.
When a page drops under 700 words with no long paragraph or sentence, its entry comes out. Shorten a page and lower
its ceilings with it: `python tests/test_editorial.py --ceilings` prints the current numbers. The report test lists
what is over and passes; `python tests/test_editorial.py` prints every page's numbers and its long passages.

It also keeps the site's map honest: every page the nav names exists (this replaces tests/test_docs_size.py).
"""

from __future__ import annotations

import re
import sys
import warnings
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
PARAGRAPH, SENTENCE, PAGE = 60, 25, 700

# Reasons for the pages not split yet. A design record and a contract are read whole; the rest wait for a split.
SPEC = "design record, read as a whole"
CONTRACT = "one versioned contract, read as a whole"
LATER = "not split yet (KAN-160 phase 2): "
REWRITE = "under 700 words; its long sentences wait for a rewrite (KAN-160 phase 2)"

# page -> (words of prose, long paragraphs and sentences, why it is over). Ceilings only go down.
LONG_PAGES: dict[str, tuple[int, int, str]] = {
    "README.md": (661, 9, "the repo's front page: the features table and the quickstart the docs home repeats"),
    "a2a.md": (1292, 10, LATER + "the agent card, the task lifecycle"),
    "acceptance.md": (2064, 48, LATER + "core checks, the rest"),
    "activity.md": (1067, 7, LATER + "usage, budgets"),
    "api/fleet.md": (811, 14, CONTRACT),
    "api/outbox.md": (1517, 26, CONTRACT),
    "api/traces.md": (1710, 31, CONTRACT),
    "api/trials.md": (953, 12, CONTRACT),
    "caches.md": (1784, 26, LATER + "what is cached, the store"),
    "changesets.md": (1397, 21, LATER + "the profile, the apply"),
    "ci.md": (1635, 8, LATER + "the pre-merge job, main's full run"),
    "cicd.md": (1625, 32, LATER + "the setup script, the pipeline"),
    "computer-use.md": (2263, 36, LATER + "the desktop, the screen, the tools"),
    "context-exchange.md": (741, 12, LATER + "patches in, patches out"),
    "deploy-company.md": (1370, 12, LATER + "the base, the components, the Terraform module"),
    "doctor.md": (1400, 31, LATER + "the checks, the fixes"),
    "e2b.md": (1847, 25, LATER + "the API, the SDKs"),
    "envd.md": (1971, 41, LATER + "the daemon, its API"),
    "gke.md": (5741, 81, LATER + "install, images and packs, operations"),
    "harness-lock.md": (2528, 53, LATER + "the lock, the team harness repo"),
    "harnesses.md": (3114, 44, LATER + "packs, models, the canary"),
    "index.md": (279, 6, "the docs home; tests/test_front_page.py pins its security posture sentence"),
    "intake.md": (2874, 49, LATER + "Jira, GitHub Projects"),
    "jobs/agent-loop.md": (1432, 12, LATER + "the loop, its stops"),
    "jobs/experiments.md": (1651, 34, LATER + "the trial, the grade"),
    "jobs/from-github-slack-jira.md": (882, 15, LATER + "one page per door"),
    "jobs/reference.md": (589, 12, "a reference: one entry per label, outcome and setting, looked up, not read"),
    "jobs/test-results.md": (1239, 27, LATER + "the report, the flaky list"),
    "jobs/working-with-the-agent.md": (1356, 26, LATER + "questions, steering"),
    "landing.md": (3793, 62, LATER + "the GitHub App, approvals, review rounds"),
    "local-microvms.md": (262, 2, REWRITE),
    "metrics.md": (526, 3, REWRITE),
    "microvm.md": (3221, 28, LATER + "by provider"),
    "model-gateway.md": (1160, 25, LATER + "the gateway, tapes"),
    "observability.md": (2530, 31, LATER + "events, metrics, traces"),
    "plugins.md": (737, 1, LATER + "plugins, events out"),
    "releases/adopt-capture-procedure.md": (838, 12, "an upgrade procedure, followed step by step"),
    "releases/adopt-capture.md": (531, 22, REWRITE),
    "releases/overnight-stack-defaults.md": (125, 23, "one list of changed defaults; half a list hides rows"),
    "releases/overnight-stack-procedure.md": (1273, 15, "an upgrade procedure, followed step by step"),
    "releases/overnight-stack.md": (789, 6, LATER + "to split by question"),
    "releases/team-release-changes.md": (36, 52, LATER + "to split by question"),
    "releases/team-release-operations.md": (1259, 25, "an upgrade procedure, followed step by step"),
    "releases/team-release-procedure.md": (1632, 33, "an upgrade procedure, followed step by step"),
    "releases/team-release-sandboxes.md": (1968, 20, "an upgrade procedure, followed step by step"),
    "releases/team-release.md": (92, 1, REWRITE),
    "repo-policy.md": (2628, 37, LATER + "the file, shadows, caps"),
    "research/ax.md": (1711, 21, "a research note, read as a whole"),
    "security/build-caches.md": (357, 10, REWRITE),
    "security/credentials.md": (1804, 22, LATER + "the mechanism, its settings, its limits"),
    "security/docker-network.md": (339, 7, REWRITE),
    "security/egress.md": (1723, 28, LATER + "the mechanism, its settings, its limits"),
    "security/front-doors.md": (1406, 32, LATER + "the mechanism, its settings, its limits"),
    "security/git-gateway.md": (1993, 28, LATER + "the mechanism, its settings, its limits"),
    "security/graders.md": (511, 11, REWRITE),
    "security/not-covered.md": (560, 7, REWRITE),
    "security/package-mirrors.md": (493, 10, REWRITE),
    "security/people.md": (921, 14, LATER + "the mechanism, its settings, its limits"),
    "security/screen.md": (390, 5, REWRITE),
    "security/secret-scanning.md": (1220, 20, LATER + "the mechanism, its settings, its limits"),
    "security/taint.md": (867, 18, LATER + "the mechanism, its settings, its limits"),
    "security/threat-model.md": (56, 34, "one table: every goal of an attacker in a sandbox, read as a whole"),
    "security/vault-key.md": (965, 18, LATER + "the mechanism, its settings, its limits"),
    "sessions.md": (2293, 20, LATER + "checkpoints, resume"),
    "slack.md": (1661, 20, LATER + "the app, the commands"),
    "specs/0001-agent-runs.md": (5059, 30, SPEC),
    "specs/0002-results-and-landing.md": (5384, 27, SPEC),
    "specs/0003-sandbox-security.md": (5798, 30, SPEC),
    "specs/0004-supervisor-scheduling-performance.md": (5180, 30, SPEC),
    "specs/0005-traces-and-decision-model.md": (6523, 48, SPEC),
    "specs/0006-environments-and-harness-packs.md": (1464, 7, SPEC),
    "specs/0007-experiments-policy-and-team-learning.md": (2453, 17, SPEC),
    "specs/0008-swarm-improvement-loop-and-rl.md": (2833, 21, SPEC),
    "specs/0009-jobs-and-the-factory-front-door.md": (1945, 10, SPEC),
    "specs/0010-sessions.md": (1903, 8, SPEC),
    "specs/0011-factory-roadmap.md": (723, 17, SPEC),
    "specs/0012-cutting-edge.md": (2948, 27, SPEC),
    "specs/0013-e2b-cube-fast-sandboxes.md": (4647, 14, SPEC),
    "specs/0014-sister-tool-interfaces.md": (3341, 61, SPEC),
    "specs/0015-governance-decision-point.md": (2463, 35, SPEC),
    "specs/README.md": (340, 22, "the index of design records, one entry per spec"),
    "specs/decisions.md": (1522, 15, SPEC),
    "steering.md": (4891, 61, LATER + "the turn, approvals, the uid split"),
    "streaming.md": (1095, 23, LATER + "the stream, the replay"),
    "team-scale.md": (4583, 86, LATER + "sizing, caps, the reconciler"),
    "templates/adr.md": (100, 1, "the ADR template: placeholder prose an author replaces"),
    "triggers.md": (1769, 30, LATER + "triggers, schedules"),
    "troubleshooting.md": (509, 5, "one entry per symptom, looked up, not read"),
    "upgrading.md": (1111, 22, LATER + "the steps, rollback"),
}

# A line that is not prose: a heading, a snippet include, an HTML tag, an image, a line of buttons, an admonition's
# title line, an attr_list or a definition marker.
SKIP_LINE = re.compile(r"^\s*(#|--8<--|<[a-z/!]|!\[|\[.*\]\(.*\)(\{[^}]*\})?\s*$|!!!|\?\?\?|:)")
TABLE_RULE = re.compile(r"^\s*\|?\s*:?-{3,}")


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


def pages() -> list[Path]:
    """README.md and every Markdown file under docs/ but the brand notes (assets/)."""
    return [ROOT / "README.md"] + [p for p in sorted(DOCS.rglob("*.md")) if p.relative_to(DOCS).parts[0] != "assets"]


def name(page: Path) -> str:
    return str(page.relative_to(DOCS)) if page.is_relative_to(DOCS) else page.name


def plain(md: str) -> str:
    md = re.sub(r"<[^>]+>", "", md)
    md = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", md)  # link text only
    md = re.sub(r"`[^`]*`", "code", md)  # a code span is one word
    md = re.sub(r"[*_]{1,2}", "", md)
    md = re.sub(r"\{\s*[.#][^}]*\}", "", md)  # attr_list
    md = md.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("\\|", "|")
    return md.strip()


def prose(text: str) -> tuple[list[str], int]:
    """The page's paragraphs (a list item and a table cell are each one) and its words of prose (tables left out)."""
    text = re.sub(r"\A---\n.*?\n---\n", "", text, flags=re.S)  # front matter
    text = re.sub(r"```.*?```", "", text, flags=re.S)  # fenced code
    text = re.sub(r"<style>.*?</style>|<figure.*?</figure>|<!--.*?-->", "", text, flags=re.S)
    paragraphs: list[str] = []
    count = 0
    for block in re.split(r"\n\s*\n", text):
        lines = [ln for ln in block.splitlines() if ln.strip() and not SKIP_LINE.match(ln)]
        current: list[str] = []
        for line in lines + [""]:
            item = re.match(r"^\s*(?:[-*]|\d+\.)\s+", line)
            if TABLE_RULE.match(line):
                continue
            if not line or line.lstrip().startswith("|") or item:
                if current:  # the paragraph (or list item) so far ends here
                    paragraphs.append(plain(" ".join(current)))
                    count += words(paragraphs[-1])
                    current = []
            if line.lstrip().startswith("|"):  # a table: every cell is read on its own, and none is prose
                paragraphs.extend(plain(c) for c in re.split(r"(?<!\\)\|", line.strip().strip("|")) if plain(c))
            elif line:
                current.append(line[item.end():] if item else line.strip())
    return [p for p in paragraphs if p], count


def sentences(paragraph: str) -> list[str]:
    # a sentence ends at . ! ? before a space and a letter, a quote or a bracket, and at a semicolon;
    # "e.g." and version numbers do not end one
    parts = re.split(r"(?<!\be\.g)(?<!\bi\.e)(?<=[.!?])\s+(?=[A-Za-z\"'(])|;\s+", paragraph)
    return [p for p in parts if p.strip()]


def words(s: str) -> int:
    return len(s.split())


def measure(page: Path) -> tuple[int, list[str]]:
    """A page's words of prose, and its passages over the paragraph or sentence limit."""
    paragraphs, count = prose(page.read_text())
    long: list[str] = []
    for para in paragraphs:
        if words(para) > PARAGRAPH:
            long.append(f"paragraph of {words(para)} words: {para[:80]}…")
        long.extend(f"sentence of {words(s)} words: {s[:80]}…" for s in sentences(para) if words(s) > SENTENCE)
    return count, long


def test_every_nav_page_exists():
    missing = [p for p in nav_pages() if not (DOCS / p).is_file()]
    assert not missing, f"nav lists pages that don't exist: {missing}"


def test_no_page_gets_longer():
    """The ratchet: a listed page stays under its ceilings, and a page not listed keeps the whole rule."""
    failures = []
    for page in pages():
        rel = name(page)
        n, long = measure(page)
        cap, long_cap, _ = LONG_PAGES.get(rel, (PAGE, 0, ""))
        if n > cap:
            failures.append(f"{rel}: {n} words of prose, over its ceiling of {cap}; split it by question")
        if len(long) > long_cap:
            failures.append(f"{rel}: {len(long)} long paragraphs or sentences, over its ceiling of {long_cap}:\n    "
                            + "\n    ".join(long))
    assert not failures, "\n" + "\n".join(failures)


def test_the_allowlist_only_names_pages_that_need_it():
    known = {name(p): p for p in pages()}
    stale = []
    for rel, (_, _, why) in LONG_PAGES.items():
        if not why:
            stale.append(f"{rel}: give the reason it is over")
        if rel not in known:
            stale.append(f"{rel}: no such page")
            continue
        n, long = measure(known[rel])
        if n <= PAGE and not long:
            stale.append(f"{rel}: {n} words and no long passage now; take it off LONG_PAGES")
    assert not stale, "\n" + "\n".join(stale)


def test_report_what_is_over():
    """Report mode: list the pages over 700 words, longest first, and pass. The ratchet above is the gate."""
    over = sorted(((measure(p)[0], name(p)) for p in pages()), reverse=True)
    over = [(n, rel) for n, rel in over if n > PAGE]
    if over:
        warnings.warn(f"{len(over)} of {len(pages())} pages are over {PAGE} words of prose: "
                      + ", ".join(f"{rel} ({n})" for n, rel in over), stacklevel=1)


def test_the_rule_is_written_down():
    agents = (ROOT / "AGENTS.md").read_text()
    for phrase in ("60 words", "25 words", "700 words"):
        assert phrase in agents, "AGENTS.md states the editorial rule this test enforces"


if __name__ == "__main__":
    if "--ceilings" in sys.argv:  # the numbers to paste into LONG_PAGES after shortening a page
        for page in pages():
            n, long = measure(page)
            if n > PAGE or long:
                print(f"    {name(page)!r}: ({n}, {len(long)}, ...),")
        sys.exit()
    over = 0
    for page in pages():
        n, long = measure(page)
        over += n > PAGE
        print(f"{name(page):55} words={n:5} long={len(long):3}")
        for line in long:
            print(f"    {line}")
    print(f"{over} of {len(pages())} pages are over {PAGE} words of prose")
