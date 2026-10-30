""".github/CODEOWNERS keeps owning hangar's security-relevant code (KAN-220): the proposed auto-merge rule (AGENTS.md,
"Merging") skips a PR that touches a code-owned path, so a security module missing from the file would merge unread.

hangar's security code is flat in src/hangar, so no directory rule catches a new module. Three signals find one: its
name (egress, vault, token, webhook, review, plugin...), its code (it signs, verifies a signature or a secret,
encrypts, or loads code by name), or a function it defines that decides trust, approval, scope or admin rights. A
module any signal flags needs a CODEOWNERS entry, or a line in EXEMPT saying why not; this file is code-owned too, so
an exemption gets the owner's review."""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).parents[1]
SRC = ROOT / "src" / "hangar"
CODEOWNERS = ROOT / ".github" / "CODEOWNERS"
OWNER = "@Sean-Koval"

NAME = re.compile(r"auth|cred|secret|vault|key|token|login|egress|tls|proxy|mirror|gateway|gw|broker|isolat|sandbox"
                  r"|relay|sidecar|envd|preview|harden|taint|canar|sign|webhook|trigger|gov|approv|trust|posture"
                  r"|polic|identit|redact|scan|crypt|hmac|review|plugin|intake|fix|lock|result|report")
CODE = re.compile(r"\bcompare_digest\(|\bhmac\.new\(|\b(Multi)?Fernet\(|\bverify_signature\b"
                  r"|\bentry_points\(|\bimport_module\(|\bspec_from_file_location\(|\bexec_module\(")
DECIDES = re.compile(r"trust|approv|authori[sz]|scope|may_|allowed|admin|privileg")  # a function or method name

# Modules a signal flags that need no code owner, and why.
EXEMPT = {
    "src/hangar/canary.py": "the harness contract checks and nightly canary: model output, no trust boundary",
    "src/hangar/modelpolicy.py": "resolves a model policy to a model id; grants nothing",
    "src/hangar/sandbox_computer.py": "runs inside the sandbox as the agent: holds nothing the agent lacks",
    "src/hangar/sandbox_env.py": "runs inside the sandbox as the agent: reports its own environment",
    "src/hangar/jobs.py": "the job hub; it signs an outbound callback with the submitter's own secret (vault-held)",
    "src/hangar/cli.py": "a client of the REST API and of keys.py and approvals (owned): the decisions are theirs",
    "src/hangar/computer_cli.py": "a client of computer_api's REST routes; the server decides who may answer",
    "src/hangar/steer_cli.py": "a client of steer_api's REST routes (owned); the server decides who may approve",
    "src/hangar/review_cli.py": "a client of harness_review's REST routes (owned); the server checks visibility",
    "src/hangar/events.py": "the event hub; it records approvals the driver (owned) wrote and decides none",
    "src/hangar/envspec.py": "parses an environment spec; its `allowed` validator checks harness names, not trust",
    "src/hangar/sessions.py": "the session hub; taint.py (owned) decides what an untrusted context file means",
}


def rules() -> list[tuple[str, list[str]]]:
    out = []
    for line in CODEOWNERS.read_text().splitlines():
        if line.strip() and not line.lstrip().startswith("#"):
            pattern, *owners = line.split()
            out.append((pattern, owners))
    return out


def matches(pattern: str, path: str) -> bool:
    """GitHub's CODEOWNERS matching for the anchored patterns this file uses: `/dir/` owns everything under it, and
    `*` stays within one directory."""
    pattern = pattern.removeprefix("/")
    if pattern.endswith("/"):
        return path.startswith(pattern)
    return re.fullmatch(re.escape(pattern).replace(r"\*", "[^/]*"), path) is not None


def owned(path: str, entries: list[tuple[str, list[str]]] | None = None) -> bool:
    return any(matches(p, path) for p, _ in entries if p) if entries is not None else owned(path, rules())


def flagged(path: str, text: str) -> bool:
    defs = (n.name for n in ast.walk(ast.parse(text)) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)))
    return bool(NAME.search(Path(path).stem) or CODE.search(text) or any(DECIDES.search(d) for d in defs))


def unowned(modules: dict[str, str], entries: list[tuple[str, list[str]]]) -> list[str]:
    """The flagged modules among {path: source} that no entry owns and EXEMPT doesn't excuse."""
    return sorted(p for p, text in modules.items() if flagged(p, text) and p not in EXEMPT and not owned(p, entries))


def modules() -> dict[str, str]:
    return {p.relative_to(ROOT).as_posix(): p.read_text() for p in sorted(SRC.rglob("*.py"))
            if p.name != "__init__.py"}


def tracked() -> list[str]:
    return [p.relative_to(ROOT).as_posix() for p in ROOT.rglob("*")
            if p.is_file() and not {".git", ".venv", "node_modules", "site", "target"} & set(p.parts)]


def test_every_entry_is_anchored_owned_and_matches_a_file():
    files = tracked()
    for pattern, owners in rules():
        assert pattern.startswith("/"), f"{pattern}: anchor it, as matches() assumes"
        assert OWNER in owners, f"{pattern}: owned by {OWNER}"
        assert any(matches(pattern, f) for f in files), f"{pattern} matches nothing: a stale entry"


def test_it_says_a_second_approver_will_be_added():
    comments = [line for line in CODEOWNERS.read_text().splitlines() if line.startswith("#")]
    assert any("second approver will be added" in line for line in comments)


def test_every_security_relevant_module_has_a_code_owner():
    missing = unowned(modules(), rules())
    assert not missing, (f"add these to .github/CODEOWNERS, or to EXEMPT in {Path(__file__).name} with a reason: "
                         f"{missing}")


def test_exemptions_are_current():
    mods = modules()
    for path in EXEMPT:
        assert path in mods, f"{path} is gone: drop it from EXEMPT"
        assert flagged(path, mods[path]), f"{path} isn't flagged any more: drop it from EXEMPT"
        assert not owned(path), f"{path} is both exempt and owned"


def test_the_paths_the_ticket_names_are_owned():
    for path in ["deploy/k8s/deployment.yaml", "deploy/kustomize/base/deployment.yaml", ".github/workflows/ci.yml",
                 ".github/CODEOWNERS", "tests/test_codeowners.py", "src/hangar/providers/kubernetes.py",
                 *(f"src/hangar/{m}.py" for m in ["isolation", "steer", "harden", "egress", "egress_proxy",
                                                  "egress_tls", "egressapi", "gateway", "broker", "teamcreds",
                                                  "taint", "triggers", "events_out", "auth", "oauth", "vault",
                                                  # the PR #415 review's gaps
                                                  "aireview", "autofix", "intake", "testresults", "plugins",
                                                  "pluginhost", "mcp_server", "harness_lock", "harness_review"])]:
        assert owned(path), f"{path} has no code owner"


def test_a_new_security_module_without_an_entry_fails():
    entries = rules()
    new = {
        "src/hangar/webhook_inbox.py": "",  # by name
        "src/hangar/notes.py": "import hmac\nok = hmac.compare_digest(a, b)\n",  # by what it does
        "src/hangar/egress_dns.py": "",  # a name the egress*.py glob already owns
        "src/hangar/loader.py": "import importlib\nm = importlib.import_module(name)\n",  # it loads code by name
        "src/hangar/merging.py": "def may_merge(pr):\n    return pr.green\n",  # it decides who may do what
        "src/hangar/weekly2.py": "def f():\n    return 1\n",  # none of them
    }
    assert unowned(new, entries) == ["src/hangar/loader.py", "src/hangar/merging.py", "src/hangar/notes.py",
                                     "src/hangar/webhook_inbox.py"]


def test_agents_md_proposes_the_auto_merge_rule():
    agents = (ROOT / "AGENTS.md").read_text()
    section = agents.split("## Merging", 1)[1].split("\n## ", 1)[0]
    assert "Proposed" in section and "lane:agent" in section and "Dependabot" in section
    assert ".github/CODEOWNERS" in section
    # the sole code owner authors hangar's PRs, so the settings must say how a PR gets approved at all (PR #415)
    assert "never lets a PR's author approve it" in section and "bypass" in section and "Recommended" in section
