"""Trial isolation (KAN-234; docs/security/graders.md#what-a-trial-can-reach): a trial can't see the answer.

A task mined from a merged PR has its fix in two places a trial could reach: the task repo's later refs (`origin/main`,
other branches, tags) and the network (the public PR, or a public suite holding the hidden tests). Either makes an
A/B on mined tasks invalid. So:

* **The clone stops at the base** (experiment trials, the trials API's included). When a trial's session boots, after
  its repo is cloned (or restored from a checkpoint), the checkout is put on the pinned `base_sha` (a restored one
  keeps its own commits on top), every remote is removed (with its fetch refspec), every ref but the trial's branch
  is deleted, borrowed objects are copied in, the reflogs are expired and `git gc --prune=now` drops what nothing
  reaches. `git log --all`, `git branch -a` and `git tag` show nothing after the base.
* **No git grant past the clone.** The trial's gateway grant then covers no repository, so neither git nor the
  gateway's read-only GitHub API path (ghapi.py) answers it. The grant itself stays: the package mirror authenticates
  with it, and the model gateway's bearer lives beside it. Hot resume re-mints it as it was: closed.
* **Closed egress by default** (every eval-class job). One whose environment sets no `egress.mode` gets the
  `allowlist` floor (posture.FLOOR_LABEL): the harness's model API hosts, the package mirror (or the package
  presets), and no GitHub host, whatever a preset, the repo policy or an approval adds (egress.resolve `closed`).
  Without the gateway the clone reaches its git host until the session has booted (`setup_hosts`). An environment
  that sets egress keeps it; the sandbox's egress record says so, and when GitHub is reachable the manifest and the
  experiment's notes carry a warning.
"""

from __future__ import annotations

import shlex
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from hangar import egress, posture

if TYPE_CHECKING:
    from hangar.envspec import EnvSpec
    from hangar.fleet import Fleet
    from hangar.models import Sandbox, Session
    from hangar.sessions import SessionService

LABEL = "hangar.eval.egress"  # on an eval job, its session and sandboxes: where its egress came from
CLOSED = "closed"  # hangar's default: the allowlist floor, no GitHub
ENVIRONMENT = "environment"  # the environment set egress itself
FLOOR = "allowlist"
GITHUB_DOMAINS = ("github.com", "githubusercontent.com", "githubcopilot.com")  # and every name below them
SETUP = "eval-setup"  # the rule source of the clone's git host, without the gateway, until the clone is isolated
DONE = "eval_isolated"  # sb.info: the clone is isolated; its setup host is gone
DOC = "docs/security/graders.md#what-a-trial-can-reach"


# ====================================================================== egress
def explicit(spec: EnvSpec) -> bool:
    """Whether the environment sets its egress mode itself (else it would get HANGAR_EGRESS_DEFAULT)."""
    e = getattr(spec, "egress", None)
    return e is not None and e.mode is not None


def for_job(queue_class: str | None, spec: EnvSpec, labels: Mapping[str, str]) -> dict[str, str]:
    """A job's labels: an eval job's get LABEL, and the allowlist floor when its environment sets no egress (a floor
    already there, `none` for a shadow or a replay, stays). Other jobs are unchanged, less a LABEL someone set."""
    out = {k: v for k, v in labels.items() if k != LABEL}
    if queue_class != "eval":
        return out
    if explicit(spec):
        out[LABEL] = ENVIRONMENT
        return out
    out[LABEL] = CLOSED
    if posture.floor_of(out) is None:
        out[posture.FLOOR_LABEL] = FLOOR
    return out


def closed(sb: Sandbox) -> bool:
    """Whether the sandbox runs an eval job on hangar's closed default (no GitHub host, whatever adds one)."""
    return sb.labels.get(LABEL) == CLOSED


def is_github(host: str) -> bool:
    """A GitHub host (a wildcard rule's suffix too): github.com, its API and git hosts, its raw content and the
    Copilot API behind GitHub's MCP server, each of which can show a merged PR."""
    h = host.lower().rstrip(".")
    return any(h == d or h.endswith("." + d) for d in GITHUB_DOMAINS)


def setup_hosts(sb: Sandbox) -> tuple[str, ...]:
    """A closed eval sandbox's git host while its repo is cloned without the git gateway (through the gateway it
    needs none), until `isolate` ends it."""
    from urllib.parse import urlsplit

    if sb.info.get(DONE) or not sb.repo or (sb.info.get("git") or {}).get("mode") == "gateway":
        return ()
    u = urlsplit(sb.repo)
    return (u.hostname,) if u.scheme in ("https", "http") and u.hostname else ()


def github_rules(pol: egress.Policy) -> list[str]:
    """What lets a policy reach GitHub (the clone's setup host aside)."""
    if pol.mode == "open":
        return ["any host (egress open)"]
    return [r.text for r in pol.rules if is_github(r.host) and r.source != SETUP]


def warning(environment: str, mode: str, hosts: list[str]) -> str:
    return (f"environment {environment} lets trials reach GitHub (egress {mode}: {', '.join(hosts[:3])}"
            f"{', ...' if len(hosts) > 3 else ''}): a trial could read a mined task's merged PR or a public suite's "
            f"hidden tests, so its results may not be valid ({DOC})")


def plan(fleet: Fleet, environment: str) -> dict[str, Any]:
    """The egress an experiment's trials get in `environment`, for its index (`pins.trial_egress`). The repo policy
    and approvals aside: a trial's own manifest has what it got."""
    spec = fleet.catalog.get(environment)
    exp = explicit(spec)
    pol = egress.resolve(fleet.settings, spec, floor=None if exp else FLOOR, closed=not exp)
    hosts = github_rules(pol)
    out: dict[str, Any] = {"source": ENVIRONMENT if exp else CLOSED, "mode": pol.mode, "github": bool(hosts)}
    if hosts:
        out["warning"] = warning(spec.name, pol.mode, hosts)
    return out


def note_egress(sb: Sandbox, spec: EnvSpec, pol: egress.Policy, info: dict[str, Any]) -> None:
    """From egress.attach: an eval sandbox's egress record says where it came from and whether GitHub is open."""
    src = sb.labels.get(LABEL)
    if src not in (CLOSED, ENVIRONMENT):
        return
    hosts = github_rules(pol)
    info.update({"eval": src, "github": bool(hosts)})
    if "floor" in info and src == CLOSED:
        info["floor_reason"] = "eval"
    if hosts:
        info["warnings"] = [warning(spec.name, pol.mode, hosts)]


# ====================================================================== the clone
def _trial(fleet: Fleet, s: Session) -> tuple[str, str] | None:
    """(experiment, task) for a real trial's session: its labels, and the runner's own record (labels anyone can
    set; KV_TRIAL only the runner)."""
    from hangar import experiments

    exp, key = s.labels.get(experiments.EXP_LABEL), s.labels.get(experiments.TRIAL_LABEL)
    if not exp or not key or not s.job_id or fleet.store.kv_get(experiments.KV_TRIAL + s.job_id) != exp:
        return None
    return exp, key.split("/")[0]


def script(branch: str, pin: str | None) -> str:
    """Put the checkout on `pin` (when given) as `branch`, and leave nothing in the repo but that branch's history:
    no remote (and so no fetch refspec), no other ref, no reflog, no borrowed objects, nothing unreachable."""
    q = shlex.quote
    keep = f"refs/heads/{branch}"
    steps = ["set -e", "export GIT_TERMINAL_PROMPT=0"]
    if pin:
        steps.append(f"git cat-file -e {q(pin)}^{{commit}} 2>/dev/null || git fetch -q origin {q(pin)}")
        steps.append(f"git checkout -q -B {q(branch)} {q(pin)}")
    steps += [
        f'[ "$(git symbolic-ref -q HEAD)" = {q(keep)} ] || git checkout -q -B {q(branch)}',
        'for r in $(git remote); do git remote remove "$r"; done',
        f"git for-each-ref --format='%(refname)' | while read -r ref; do "
        f'[ "$ref" = {q(keep)} ] || git update-ref --no-deref -d "$ref"; done',
        'gd=$(git rev-parse --git-dir); rm -f "$gd/FETCH_HEAD" "$gd/ORIG_HEAD" "$gd/MERGE_HEAD" "$gd/CHERRY_PICK_HEAD"',
        "git reflog expire --expire=now --expire-unreachable=now --all",
        # objects borrowed from another repository (a reference clone) come in, and the link goes
        'if [ -s "$gd/objects/info/alternates" ]; then git repack -a -d -q; rm -f "$gd/objects/info/alternates"; fi',
        "git -c gc.auto=0 -c gc.reflogExpire=now -c gc.reflogExpireUnreachable=now gc -q --prune=now",
        "echo HANGAR-REFS; git for-each-ref --format='%(refname)'",
    ]
    return "\n".join(steps)


async def isolate(sessions: SessionService, s: Session, sb: Sandbox) -> None:
    """From SessionService._boot, after the repo is cloned and restored. A trial's checkout goes to its pinned base
    with nothing past it, and its git grant closes. Any closed eval sandbox loses the clone's setup host. Not an
    eval sandbox: nothing. Fails the boot when it can't (fail closed)."""
    from hangar import experiments
    from hangar.sessions import SessionError

    fleet = sessions.fleet
    if sb.labels.get(LABEL) not in (CLOSED, ENVIRONMENT) and _trial(fleet, s) is None:
        return
    repo = sb.info.get("repo_path")
    if repo and (t := _trial(fleet, s)) is not None:
        exp, task = t
        pin = fleet.store.kv_get(f"{experiments.KV_BASE}{exp}:{task}") or s.base_sha
        move = pin if pin and not s.checkpoints and pin != s.base_sha else None  # a restored tree keeps its commits
        res = await sessions._sh(sb, script(s.branch, move), cwd=repo, timeout=900)
        if res.exit_code != 0:
            raise SessionError(f"could not isolate the trial's checkout at {str(pin)[:12]}: "
                               f"{(res.stderr or res.stdout)[-400:]}")
        if move:
            s.base_sha = move
        refs = [x.strip() for x in res.stdout.split("HANGAR-REFS", 1)[-1].splitlines() if x.strip()]
        git = sb.info.get("git") or {}
        if git.get("mode") == "gateway":
            fleet.gateway.close(sb.id)
            sb.info["git"] = {**git, "push_refs": [], "closed": True}
            grant = "closed"
        else:
            grant = "direct"  # no grant to close: the environment's own credential (docs/security/graders.md)
            sb.info.setdefault("warnings", []).append(
                "trial isolation: this sandbox's git runs without the gateway, so hangar can't close its access to "
                "the task repo; set HANGAR_GATEWAY_URL")
        sb.info["trial_isolation"] = {"base_sha": s.base_sha, "branch": s.branch, "refs": refs, "remotes": [],
                                      "git_grant": grant}
        fleet.audit(f"session:{s.id}", "trial.isolate", sb.id, base_sha=s.base_sha, refs=refs, git_grant=grant)
    sb.info[DONE] = True
    fleet.store.put(sb)
    fleet.egress.invalidate(sb.id)  # the clone's setup host goes from the proxy's next connection
