"""Least privilege for the sandboxes whose agent, or repo code, never needs to push (--- least-privilege).

* **Jobs.** A job's agent commits and ends its turn; hangar verifies, scans for secrets, runs the landing checks and
  pushes. So a job's sandbox (every kind: best-of-N attempts, follow-ups, auto-fix rounds, env setup's agents, the
  Cloud Doctor's root-cause and fix jobs) holds a **fetch-only** gateway grant (`Gateway.push_scope`), whatever its
  environment's `credential_mode` says, whenever the gateway serves its repo.
* **Delivery** (`push`): after the checks, hangar mints a grant of its own beside the sandbox's, pinned to one ref and
  one commit and good for a few minutes, and runs the push in the sandbox with it: the pack comes from the sandbox's
  git, the authority from hangar. The gateway refuses any other ref or commit with it, and scans the pack as it scans
  every push. A copy the agent lifts can push only the commit hangar is pushing, to the branch it is pushing it to.
  Upstream the push is made with the gateway's credential (the GitHub App), as before.
* **Builds** (a repo build's sandbox, env setup's validation): they run the repo's `install`. They get the fetch-only
  grant for their clone and nothing else: no credential (without the gateway, not even git's), no `secrets:`, no
  cloud tokens; a build's mapped build secrets reach its install command alone (envbuilds._install).

* **Sealed** (an experiment's grader; part B adds replays and forks): it runs agent-written code against material that
  must not leave (hidden tests). It holds what a build holds, and on top of that egress `none` with *no* harness, MCP
  or cloud host (only hangar's read-only package mirror with `network: mirror`), always on the proxy-only network, and
  none of the environment's `mcp:`, `computer:` or services (`seal`, egress.resolve(sealed=True)).

Without the gateway a job's sandbox holds the environment's git token (credential_mode: direct), which hangar can't
narrow; landing refuses to land such a job, and `hangar doctor` says so.
"""

from __future__ import annotations

import secrets as pysecrets
import shlex
from typing import TYPE_CHECKING

from hangar.envbuilds import LABEL as BUILD_LABEL
from hangar.envbuilds import VALIDATE_LABEL
from hangar.models import Credential, ExecResult, Sandbox

if TYPE_CHECKING:
    from hangar.envspec import EnvSpec
    from hangar.fleet import Fleet

DELIVERY_MINUTES = 10  # a delivery grant's life: one push of at most HANGAR_GATEWAY_MAX_PUSH_MB
GRADER_LABEL = "hangar.experiment.grader"  # --- review(#114): experiments.GRADER_LABEL; it runs a trial's code
# --- sealed (#126): the labels of sandboxes that get the sealed profile. Part B adds its replay and fork labels here.
SEALED_LABELS = (GRADER_LABEL,)
NETWORK_LABEL = "hangar.sealed.network"  # "mirror": a sealed sandbox may read hangar's package mirror; else nothing


def builds_repo_code(sb: Sandbox) -> bool:
    """A repo build's sandbox or env setup's validation: it runs the repo's `install`, and holds nothing for it.
    --- review(#114): so does an experiment's grader, which runs the trial's agent-written patch."""
    return BUILD_LABEL in sb.labels or VALIDATE_LABEL in sb.labels or GRADER_LABEL in sb.labels


def sealed(sb: Sandbox) -> bool:
    """--- sealed (#126): a sandbox that runs agent-written code beside material that must not leave it (an
    experiment's grader and its hidden tests). A label only ever takes away from its own sandbox, so anyone setting
    it gains nothing."""
    return any(k in sb.labels for k in SEALED_LABELS)


def sealed_mirror(sb: Sandbox) -> bool:
    """A sealed sandbox that may read hangar's read-only package mirror (`Grader.network: mirror`)."""
    return sealed(sb) and sb.labels.get(NETWORK_LABEL) == "mirror"


def sealed_setup_hosts(sb: Sandbox) -> tuple[str, ...]:
    """The hosts a sealed sandbox reaches while it is set up: its repo's git host, when it clones without the git
    gateway (through the gateway it needs no host), until `lock_sealed`. Before that only hangar's commands ran in it
    (the clone, the base checkout, `git apply`), none of the code it grades."""
    from urllib.parse import urlsplit

    if not sealed(sb) or (sb.info.get("sealed") or {}).get("locked") or not sb.repo:
        return ()
    if (sb.info.get("git") or {}).get("mode") == "gateway":
        return ()
    u = urlsplit(sb.repo)
    return (u.hostname,) if u.scheme in ("https", "http") and u.hostname else ()


def lock_sealed(fleet: Fleet, sb: Sandbox) -> Sandbox:
    """Before a sealed sandbox runs the code it grades: no host at all from now on (the setup's git host goes), its
    git grant revoked (and the GitHub API and caches behind it), the egress proxy's cached policy dropped. Saved
    before it returns, so a failure leaves the grade erroring, not the sandbox open."""
    sb = fleet.get(sb.id)
    sb.info["sealed"] = {**(sb.info.get("sealed") or {}), "locked": True}
    fleet.store.put(sb)
    fleet.gateway.revoke(sb.id)
    fleet.egress.invalidate(sb.id)
    return sb


def seal(spec: EnvSpec, sb: Sandbox) -> EnvSpec:
    """The environment as a sealed sandbox gets it: egress `none` (egress.for_sandbox resolves it with
    sealed=True: no harness, MCP or cloud host), no `mcp:`, no `computer:`, no services, no `secrets:`, no cloud
    identity, and never from (or refilling) the warm pool. Called after the image is resolved, so the image is the
    environment's own (built, or its repo build's). Anything not sealed is returned as it is."""
    if not sealed(sb):
        return spec
    from hangar.egress import EgressSpec

    return spec.model_copy(update={
        "egress": EgressSpec(mode="none", presets=[], allow=[]), "mcp": {}, "computer": None, "services": {},
        "secrets": {}, "cloud": None, "warm_pool": 0, "warm_pool_hours": None})


def provider_spec(spec: EnvSpec, sb: Sandbox) -> EnvSpec:
    """A sealed sandbox's spec as its provider gets it: a name of its own, so on Kubernetes it has a SandboxTemplate
    of its own (the environment's would flip to the proxied network and back)."""
    if not sealed(sb):
        return spec
    return spec.model_copy(update={"name": f"{spec.name[:40]}-sealed"[:63], "warm_pool": 0, "warm_pool_hours": None})


def clone_credentials(fleet: Fleet, names: list[str], labels: dict[str, str]) -> list[str]:
    """The credentials a build's or validation's sandbox is created with: git ones only (the gateway picks its
    upstream credential among them; none is injected). Nothing else is even leased to it, so no login a team pool
    adds is held by it, and no sign-in file is copied back from it into the vault when it is released."""
    from hangar.gateway import GIT_KINDS

    if BUILD_LABEL not in labels and VALIDATE_LABEL not in labels:
        return names
    return [n for n in names if (c := fleet.store.get(Credential, n)) is not None and c.kind in GIT_KINDS]


def secrets(fleet: Fleet, sb: Sandbox, spec: EnvSpec) -> dict[str, str]:
    """The environment's `secrets:` for this sandbox: in a fetch-only gateway sandbox (a job's on a direct-mode
    environment), minus the ones that map a git credential, which would hand the agent the token the gateway keeps."""
    from hangar.gateway import GIT_KINDS

    if (sb.info.get("git") or {}).get("mode") != "gateway" or not fleet.gateway.fetch_only(sb):
        return dict(spec.secrets)
    out: dict[str, str] = {}
    dropped: list[str] = []
    for var, name in spec.secrets.items():
        c = fleet.store.get(Credential, name)
        if c is not None and c.kind in GIT_KINDS:
            dropped.append(var)
        else:
            out[var] = name
    if dropped:
        sb.info.setdefault("warnings", []).append(
            f"secrets: {', '.join(dropped)} left out: they map a git credential, and a job's sandbox only fetches "
            "(hangar pushes its work)")
    return out


def clone_hint(sb: Sandbox) -> str:
    """Why a clone may have failed: the sandbox had no git credential, by design."""
    git = sb.info.get("git") or {}
    if builds_repo_code(sb) and git.get("mode") != "gateway":
        return ("\nhangar: a repo build or env-setup validation runs the repo's install, so its sandbox holds no git "
                "credential; it clones a private repo only through the git gateway's fetch-only grant "
                "(HANGAR_GATEWAY_URL, docs/security/git-gateway.md)")
    if git.get("mode") == "gateway" and not git.get("repo"):
        return ("\nhangar: the git gateway doesn't serve this repo's host (HANGAR_GATEWAY_UPSTREAMS), and this "
                "sandbox holds no git credential of its own (a job's, a build's and a validation's never do while the "
                "gateway is configured)")
    return ""


async def push(fleet: Fleet, sandbox_id: str, branch: str, sha: str, actor: str, timeout: int = 300,
               repo: str | None = None, cwd: str | None = None) -> ExecResult:
    """Push `sha` to `branch` of the sandbox's origin, as hangar: in gateway mode with a delivery grant pinned to that
    ref and commit (dropped when the push ends), else with the sandbox's own git credential (direct mode). The
    sandbox's hooks don't run in it. --- change sets: `repo` (a role's clone URL) and `cwd` (its checkout) push one
    role of a change set, with a grant for that repo alone."""
    q = shlex.quote
    ref = f"refs/heads/{branch}"
    cmd = f"git -c core.hooksPath=/dev/null push -q origin {q(sha)}:{q(ref)}"
    sb, prov = fleet._running(sandbox_id)
    git = sb.info.get("git") or {}
    if git.get("mode") != "gateway" or not git.get("repo"):
        return await fleet.exec(sandbox_id, cmd, cwd=cwd, timeout=timeout, actor=actor)
    from hangar.gateway import GatewayError, parse_remote

    gw = fleet.gateway
    name = None
    if repo is not None:
        remote = parse_remote(repo, gw.hosts)
        if remote is None:
            return ExecResult(exit_code=1, stdout="", stderr=f"hangar: the git gateway doesn't serve {repo}")
        name = remote[1]
    try:
        token, grant = gw.mint_delivery(sb, ref, sha, DELIVERY_MINUTES, repo=name)
    except GatewayError as e:
        return ExecResult(exit_code=1, stdout="", stderr=f"hangar: {e}")
    fleet.audit(actor, "gateway.grant", sb.id, repo=name or git.get("repo"), push_refs=[ref], pin=sha, delivery=True)
    # The token reaches git through a 0600 file read and removed before the push: never a command line (which the
    # audit log records) and never ~/.git-credentials. The header goes to the gateway's URL only; an empty value
    # first drops any extraHeader the agent configured for it.
    key = f"http.{(fleet.settings.gateway_url or '').rstrip('/')}/.extraHeader"
    env = (f"GIT_CONFIG_COUNT=2\nGIT_CONFIG_KEY_0={q(key)}\nGIT_CONFIG_VALUE_0=''\n"
           f"GIT_CONFIG_KEY_1={q(key)}\nGIT_CONFIG_VALUE_1={q('Authorization: Bearer ' + token)}\n")
    path = f"/tmp/.hangar-push-{pysecrets.token_hex(8)}"
    try:
        await prov.write_file(sb, path, env.encode(), 0o600)
        return await fleet.exec(sandbox_id, f"set -a; . {path}; set +a; rm -f {path}; {cmd}", cwd=cwd,
                                timeout=timeout, actor=actor)
    finally:
        gw.drop(grant)
