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


def builds_repo_code(sb: Sandbox) -> bool:
    """A repo build's sandbox or env setup's validation: it runs the repo's `install`, and holds nothing for it.
    --- review(#114): so does an experiment's grader, which runs the trial's agent-written patch."""
    return BUILD_LABEL in sb.labels or VALIDATE_LABEL in sb.labels or GRADER_LABEL in sb.labels


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
                "(HANGAR_GATEWAY_URL, docs/security.md#git-credential-gateway)")
    if git.get("mode") == "gateway" and not git.get("repo"):
        return ("\nhangar: the git gateway doesn't serve this repo's host (HANGAR_GATEWAY_UPSTREAMS), and this "
                "sandbox holds no git credential of its own (a job's, a build's and a validation's never do while the "
                "gateway is configured)")
    return ""


async def push(fleet: Fleet, sandbox_id: str, branch: str, sha: str, actor: str, timeout: int = 300) -> ExecResult:
    """Push `sha` to `branch` of the sandbox's origin, as hangar: in gateway mode with a delivery grant pinned to that
    ref and commit (dropped when the push ends), else with the sandbox's own git credential (direct mode). The
    sandbox's hooks don't run in it."""
    q = shlex.quote
    ref = f"refs/heads/{branch}"
    cmd = f"git -c core.hooksPath=/dev/null push -q origin {q(sha)}:{q(ref)}"
    sb, prov = fleet._running(sandbox_id)
    git = sb.info.get("git") or {}
    if git.get("mode") != "gateway" or not git.get("repo"):
        return await fleet.exec(sandbox_id, cmd, timeout=timeout, actor=actor)
    from hangar.gateway import GatewayError

    gw = fleet.gateway
    try:
        token, grant = gw.mint_delivery(sb, ref, sha, DELIVERY_MINUTES)
    except GatewayError as e:
        return ExecResult(exit_code=1, stdout="", stderr=f"hangar: {e}")
    fleet.audit(actor, "gateway.grant", sb.id, repo=git.get("repo"), push_refs=[ref], pin=sha, delivery=True)
    # The token reaches git through a 0600 file read and removed before the push: never a command line (which the
    # audit log records) and never ~/.git-credentials. The header goes to the gateway's URL only; an empty value
    # first drops any extraHeader the agent configured for it.
    key = f"http.{(fleet.settings.gateway_url or '').rstrip('/')}/.extraHeader"
    env = (f"GIT_CONFIG_COUNT=2\nGIT_CONFIG_KEY_0={q(key)}\nGIT_CONFIG_VALUE_0=''\n"
           f"GIT_CONFIG_KEY_1={q(key)}\nGIT_CONFIG_VALUE_1={q('Authorization: Bearer ' + token)}\n")
    path = f"/tmp/.hangar-push-{pysecrets.token_hex(8)}"
    try:
        await prov.write_file(sb, path, env.encode(), 0o600)
        return await fleet.exec(sandbox_id, f"set -a; . {path}; set +a; rm -f {path}; {cmd}", timeout=timeout,
                                actor=actor)
    finally:
        gw.drop(grant)
