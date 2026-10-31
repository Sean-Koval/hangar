"""Intake posture: the secure defaults for jobs whose task text someone outside hangar could write (#123).

Jobs started from GitHub, Jira, Slack or an inbound webhook carry text that anyone with edit rights on
the ticket, issue or thread can write. Hangar fences that text as untrusted in the prompt (triggers.untrusted_note),
but a prompt is not a boundary. The intake posture adds two that are, for every *intake job*:

* **The git gateway.** The sandbox holds a fetch-only grant and hangar pushes (delivery.py), so no GitHub token is in
  reach of an injected prompt. An intake job whose repo the gateway doesn't serve (no
  HANGAR_GATEWAY_URL, or a host outside HANGAR_GATEWAY_UPSTREAMS) is refused at submit, with a message the front door
  relays to the ticket or thread.
* **An egress floor.** Its sandboxes get at least `allowlist`, even in an `open` environment: the label
  `hangar.egress.floor` rides from the job to its session and every sandbox the session starts (best-of-N attempts,
  follow-ups, resumes), and egress.resolve and the providers read it (the proxy-only network: the internal network on
  Docker, the NetworkPolicy on GKE). The allowlist is the environment's presets and hosts, the repo policy's, and the
  harness's API hosts. A warm sandbox of an `open` environment is never claimed for it: it cold-starts.

Both always apply (KAN-74): `HANGAR_INTAKE_POSTURE=enforce` is the only value. `warn`, the safe release's one-release
transition, and `off` were removed after v0.2.0, and config.REMOVED_SWITCHES refuses them. `mode()` still answers
`off` for unit tests and the docs demo, which run without a gateway or an egress proxy and replace it
(tests/conftest.py); no setting reaches that.

An intake job is one from those sources, one with an untrusted author (`untrusted_source`), or one with an untrusted
context item: auto-fix rounds (CI logs, review comments) and env setup started from an issue are covered that way.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from hangar.config import Settings
    from hangar.envspec import EnvSpec
    from hangar.models import ContextItem, Job, Sandbox

INTAKE_SOURCES = frozenset({"github", "github-projects", "jira", "slack", "webhook"})
FLOOR_LABEL = "hangar.egress.floor"  # on the job, its session and its sandboxes: the least egress they get
INTAKE_LABEL = "hangar.intake"  # on the job (and env setup's children): why it is an intake job
FLOOR = "allowlist"
FLOORS = ("allowlist", "none")
DOC = "docs/security/front-doors.md#intake-posture"


OFF = "off"  # only unit tests and the docs demo, by replacing mode(): no setting reaches it


def mode(settings: Settings) -> str:
    """`enforce`, always (KAN-74)."""
    return "enforce"


def is_intake_req(source: str | None, untrusted_source: str | None, context: Iterable[ContextItem] = (),
                  labels: Mapping[str, str] | None = None) -> bool:
    """Whether a job with these traits is an intake job (before the Job exists)."""
    return (source in INTAKE_SOURCES or bool(untrusted_source) or any(getattr(c, "untrusted", False) for c in context)
            or bool((labels or {}).get(INTAKE_LABEL)))


def is_intake(job: Job) -> bool:
    """`job.source` is a front door, its author is untrusted, or it carries untrusted context."""
    return is_intake_req(job.source, job.untrusted_source, job.context, job.labels)


def why_intake(source: str | None, untrusted_source: str | None, context: Iterable[ContextItem] = (),
               labels: Mapping[str, str] | None = None) -> str:
    if (labels or {}).get(INTAKE_LABEL):
        return str((labels or {})[INTAKE_LABEL])
    if source in INTAKE_SOURCES:
        return f"source:{source}"
    if untrusted_source:
        return "untrusted-author"
    return "untrusted-context"


def floor_of(labels: Mapping[str, str] | None) -> str | None:
    """The egress floor a set of labels asks for (`allowlist` or `none`), else None. Only ever a tightening."""
    v = (labels or {}).get(FLOOR_LABEL)
    return v if v in FLOORS else None


def gateway_serves(settings: Settings, repo: str | None) -> bool:
    """Whether a job on `repo` gets the git gateway's fetch-only grant instead of a token: the gateway is configured
    and serves the repo's host (any job without a repo, once it is configured)."""
    if not settings.gateway_url:
        return False
    if not repo:
        return True
    from hangar.gateway import parse_remote

    return parse_remote(repo, [h.lower() for h in settings.gateway_upstreams]) is not None


def gateway_problem(settings: Settings, repo: str | None) -> str | None:
    """What keeps an intake job off the gateway, in words, or None."""
    if gateway_serves(settings, repo):
        return None
    if not settings.gateway_url:
        return ("intake-triggered jobs need the git gateway, and HANGAR_GATEWAY_URL is not set: without it the "
                "sandbox would hold a GitHub token that the ticket's text could steer the agent to use")
    return (f"the git gateway doesn't serve {repo} (its host isn't in HANGAR_GATEWAY_UPSTREAMS), so an "
            "intake-triggered job's sandbox would get no fetch-only grant")


def env_note(settings: Settings, spec: EnvSpec) -> str | None:
    """What the egress floor changes for an environment, or None when it is proxied already."""
    from hangar import egress

    if egress.proxied(settings, spec):
        return None
    return f"environment {spec.name} is egress {egress.env_mode(settings, spec)}"


class Assessment:
    """What the posture decides for one intake job at submit."""

    def __init__(self, posture: str, reason: str, problems: list[str], floor: bool) -> None:
        self.posture, self.reason, self.problems, self.floor = posture, reason, problems, floor

    def with_reason(self, reason: str) -> Assessment:
        return Assessment(self.posture, reason, self.problems, self.floor)

    @property
    def refused(self) -> bool:
        return self.posture == "enforce" and bool(self.problems)

    def message(self) -> str:
        fix = "set HANGAR_GATEWAY_URL (and HANGAR_GATEWAY_UPSTREAMS for the repo's host)" \
            if any("gateway" in p for p in self.problems) else \
            "the repo policy's allow_heavy_for_intake, or a non-heavy environment"
        return f"{'; '.join(self.problems)}. Fix: {fix} ({DOC})"


def assess(settings: Settings, spec: EnvSpec, repo: str | None, source: str | None, untrusted_source: str | None,
           context: Iterable[ContextItem] = (), labels: Mapping[str, str] | None = None,
           docker: bool = False, allow_heavy: bool | None = None) -> Assessment | None:
    """None for a job that isn't an intake job (or in a unit test that turns the posture off). `docker`: its
    sandboxes run Docker on the heavy tier; `allow_heavy`: the repo policy's `allow_heavy_for_intake` (#124)."""
    context = list(context)
    p = mode(settings)
    if p == OFF or not is_intake_req(source, untrusted_source, context, labels):
        return None
    problems = [x for x in (gateway_problem(settings, repo), heavy_problem(spec.name, docker, allow_heavy)) if x]
    return Assessment(p, why_intake(source, untrusted_source, context, labels), problems, floor=True)


def mark(labels: dict[str, str], a: Assessment) -> dict[str, str]:
    """The job's labels with its posture: why it is intake, and the egress floor its sandboxes get."""
    out = {**labels, INTAKE_LABEL: a.reason[:100]}
    if a.floor and floor_of(out) is None:
        out[FLOOR_LABEL] = FLOOR
    return out


def lacks_floor(have: Mapping[str, str], want: Mapping[str, str]) -> bool:
    """Whether a sandbox with labels `have` misses the floor `want` asks for (a session's sandbox from before its job
    became an intake job, or one parked then)."""
    from hangar.egress import _STRICTNESS

    w = floor_of(want)
    return w is not None and _STRICTNESS[floor_of(have) or "open"] < _STRICTNESS[w]


JOB_LABELS = ("hangar.job", "hangar.experiment.grader")  # a job's session (best-of-N attempts too), a grader


def no_new_privs(settings: Settings, sb: Sandbox) -> bool:
    """--- #124: whether a Docker sandbox runs with `no-new-privileges`, so sudo (or any setuid program) fails there as
    it does on GKE, even in an environment whose image has sudo: every job's sandbox. The uid split switches users
    with `docker exec --user` and setpriv's capabilities, no setuid binary, so it keeps working. A heavy sandbox with
    Docker is left alone: dockerd runs as root (through sudo) inside its own VM. A repo build's sandbox isn't a
    job's."""
    if mode(settings) == OFF or (sb.info.get("heavy") or {}).get("docker"):
        return False
    return any(k in sb.labels for k in JOB_LABELS)


def heavy_problem(spec_name: str, docker: bool, allowed: bool | None) -> str | None:
    """--- #124: an intake job on a heavy environment (Docker, root inside the sandbox's VM) needs the repo policy's
    `allow_heavy_for_intake: true`."""
    if not docker or allowed:
        return None
    return (f"environment {spec_name} runs Docker (root inside the sandbox's VM), and intake-triggered jobs don't run "
            "there unless the repo policy allows it (hangar repo set <repo> --allow-heavy-for-intake)")


def inherit(labels: Mapping[str, str]) -> dict[str, str]:
    """The posture labels a job hands on to the jobs and sandboxes it starts (env setup's children, its validation)."""
    return {k: v for k, v in labels.items() if k in (INTAKE_LABEL, FLOOR_LABEL)}


# ====================================================================== the provider's spec
def provider_spec(settings: Settings, spec: EnvSpec, sb: Sandbox) -> EnvSpec:
    """The spec the provider creates a floored sandbox from. An environment that is proxied already is unchanged.
    Otherwise a copy of its own, with the floor as its egress mode and no warm pool: its network follows (Docker's
    internal network, a Kubernetes SandboxTemplate with the proxied label and policy), and none of the environment's
    open warm sandboxes is claimed. Its name carries a suffix of the shape k8sgc sweeps (`s` and 8 hex digits)."""
    from hangar import egress

    floor = floor_of(sb.labels)
    if floor is None or egress.proxied(settings, spec):
        return spec
    e = getattr(spec, "egress", None)
    tag = "s" + hashlib.sha256(f"{spec.name}:floor:{floor}".encode()).hexdigest()[:8]
    copy = egress.EgressSpec(mode=floor, presets=e.presets if e else None, allow=list(e.allow) if e else [])  # type: ignore[arg-type]
    return spec.model_copy(update={"name": f"{spec.name[:40]}-{tag}", "warm_pool": 0, "warm_pool_hours": None,
                                   "egress": copy})


# ====================================================================== review packet, doctor
def packet_lines(settings: Settings, job: Job, spec: EnvSpec | None = None) -> list[str]:
    """--- landing: the review packet's posture line: the floor the job's sandboxes had."""
    if floor := floor_of(job.labels):
        return [f"- **Egress:** {floor} (intake floor)"]
    return []


def intake_environments(settings: Settings) -> list[str]:
    """The environments intake jobs run in: those mapped for repos (GitHub, Slack and Jira run only
    on repos in HANGAR_GITHUB_REPOS) and the inbound webhooks' own."""
    out: list[str] = []
    for raw in (settings.github_repos or {}).values():
        out += [e.strip() for e in str(raw).split(",") if e.strip()]
    for cfg in (settings.inbound_webhooks or {}).values():
        if isinstance(cfg, dict) and isinstance(cfg.get("environment"), str):
            out.append(cfg["environment"])
    return list(dict.fromkeys(out))


def intake_doors(settings: Settings) -> list[str]:
    """--- #236: the front doors that can start intake jobs here, by name (any part of one configured counts)."""
    doors = {"GitHub": settings.github_webhook_secret_credential, "Slack": settings.slack_signing_secret_credential,
             "Jira": settings.jira, "inbound webhooks": settings.inbound_webhooks}
    return [name for name, cfg in doors.items() if cfg]


def gateway_fix(settings: Settings, fleet: Any) -> str:
    """--- #236: the one command that turns the git gateway on where this hangar runs. On GKE the manifest sets it; on
    Docker sandboxes reach the host at host.docker.internal, and on Linux the gateway must listen on the docker
    bridge (it binds HANGAR_HOST, 127.0.0.1, otherwise), which needs a restart."""
    import sys

    doc = "docs/security/git-gateway.md#turning-it-on"
    if "kubernetes" in list(fleet.providers):
        return f"set HANGAR_GATEWAY_URL=http://hangar-gateway.hangar.svc:8788 (deploy/k8s/config.yaml; {doc})"
    cmd = "hangar config set HANGAR_GATEWAY_URL http://host.docker.internal:8788"
    if sys.platform == "linux" and not settings.gateway_host:
        cmd += " && hangar config set HANGAR_GATEWAY_HOST 172.17.0.1"
    return f"{cmd}, then restart (hangar down && hangar up) and store a git credential for it ({doc})"


def doctor_checks(h: Any) -> list[dict[str, Any]]:
    """`hangar doctor`: the posture, the gateway, the egress proxy, and the effective mode of intake environments."""
    import sys

    from hangar import egress
    from hangar.doctor import _check

    s, fleet = h.settings, h.fleet
    p = mode(s)
    out: list[dict[str, Any]] = removed_checks(h, "intake_posture")
    if p == OFF:
        return out
    gw = bool(s.gateway_url)
    doors = intake_doors(s)  # --- #236: refusing intake jobs only matters once something can send one
    if gw:
        out.append(_check("intake posture: gateway", True, f"enforce; HANGAR_GATEWAY_URL={s.gateway_url}"))
    elif doors:
        out.append(_check("intake posture: gateway", False, f"enforce, and HANGAR_GATEWAY_URL is not set: every "
                          f"job from {', '.join(doors)} is refused", gateway_fix(s, fleet)))
    else:
        out.append(_check("intake posture: gateway", None, "enforce; no intake front door is configured (GitHub, "
                          "Slack, Jira, webhooks), so nothing is refused yet. Once one is, its "
                          "jobs need the git gateway, which isn't set up", gateway_fix(s, fleet)))
    # the egress proxy each provider needs for a floored sandbox
    provs = list(fleet.providers)
    if "kubernetes" in provs:
        ok = bool(s.egress_url) and bool(s.egress_port) or None
        out.append(_check("intake posture: egress proxy (kubernetes)", ok,
                          f"HANGAR_EGRESS_URL={s.egress_url}" if s.egress_url else "HANGAR_EGRESS_URL is not set: an "
                          "intake job in an open environment can't get its allowlist floor and fails to start",
                          "set HANGAR_EGRESS_URL=http://hangar-gateway.hangar.svc:3128 (deploy/k8s/config.yaml)"))
    if "docker" in provs:
        local = sys.platform != "linux"
        out.append(_check("intake posture: egress proxy (docker)", None if local or not s.egress_port else True,
                          "Docker Desktop can't run proxied sandboxes (hangar must listen on the egress network's "
                          "gateway): under enforce, intake jobs in open environments fail to start here" if local else
                          (f"hangar's proxy on the egress network's gateway, port {s.egress_port}" if s.egress_port
                           else "HANGAR_EGRESS_PORT is 0: proxied sandboxes can't start"),
                          "run intake work (GitHub, Slack, Jira, webhooks) on a Linux Docker host or GKE "
                          "(docs/security/front-doors.md#intake-posture)"))
    others = [n for n in provs if n not in ("docker", "kubernetes")]
    if others:
        out.append(_check("intake posture: other providers", None, f"{', '.join(others)} can't keep a sandbox off the "
                          "internet: an intake job in an open environment fails to start there",
                          "run intake environments on docker or kubernetes, or give them egress.mode: allowlist"))
    intake = intake_environments(s)
    sudo = []  # --- #124: environments whose sandbox user has passwordless sudo
    for name in fleet.catalog.names():
        try:
            if fleet.catalog.get(name).sudo:
                sudo.append(name)
        except Exception:  # noqa: BLE001 - an environment that doesn't load is reported elsewhere
            continue
    if sudo:
        used = [n for n in sudo if n in intake]
        nnp = "Docker job sandboxes run with no-new-privileges, so sudo fails there"
        out.append(_check("environments with sudo", None, f"{', '.join(sudo)} set sudo: true"
                          + (f"; intake jobs run in {', '.join(used)}" if used else "") + f" ({nnp})",
                          "set sudo: false and install at build time (tools.apt, setup), or keep sudo: true only for "
                          "people's own sessions (docs/environments.md)"))
    for name in intake:
        try:
            spec = fleet.catalog.get(name)
        except KeyError:
            continue  # the landing and front-door checks report a missing environment
        m = egress.env_mode(s, spec)
        eff = m if m != "open" else f"{FLOOR} (intake floor)"
        out.append(_check(f"intake environment {name}", True, f"egress {m}; intake jobs get {eff}"))
    return out


def removed_checks(h: Any, field: str) -> list[dict[str, Any]]:
    """--- KAN-74: a removed switch stored with `hangar config` before the upgrade (one in the environment stops
    hangar from starting). It isn't applied, so hangar enforces, but the doctor fails on it until it is unset."""
    from hangar.doctor import _check

    cfg = getattr(h, "config", None)
    if cfg is None or not hasattr(cfg, "removed"):
        return []
    return [_check(f"removed setting {key}", False, msg, f"hangar config unset {key}")
            for key, msg in cfg.removed() if key == "HANGAR_" + field.upper()]
