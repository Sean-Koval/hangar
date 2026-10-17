"""Environment specs: a declarative, layered description of a coding sandbox.

An environment has three parts:

* **Image**: what is on disk. Built from this spec, or a prebuilt image you bring (`image:`).
* **Runtime**: where it runs and with what isolation (`resources`, `runtime_class`, the provider).
* **Session**: what is applied when a sandbox is claimed, for the one harness it was created for:
  credentials, `secrets`, `mcp` servers, `model`, harness config and PATH.

`harnesses` lists the agents an environment *allows*. They are not installed into the image: each
harness ships as a read-only pack mounted at start (see hangar.harnesses), or, on providers that cannot
mount images, only the selected harness is baked into a per-harness variant of the image.

Image layers are rendered into a Dockerfile in cache-friendly order, so changing a repo setup
command does not reinstall anything:

    base -> system packages -> language runtimes (mise) -> [baked harness]
         -> dependencies -> skills + agent config + files -> [baked repo]

The repo itself is normally cloned when a sandbox is claimed (so every sandbox is on a
fresh branch); set `repo.bake: true` to pre-clone and pre-install into the image, which
makes claims near-instant for big repos.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import re
import shlex
import tarfile
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from hangar import computer as _computer  # --- computer
from hangar import plugins as _plugins  # --- #136
from hangar.cloudcreds import CloudSpec  # --- egress
from hangar.computer import ComputerSpec  # --- computer
from hangar.egress import EgressSpec  # --- egress
from hangar.harnesses import HARNESSES, McpServer, get_harness, pack_script, pack_stage
from hangar.models import PROFILES, Resources
from hangar.plugins import Plugin  # --- #136: Claude Code plugins, from a read-only seed

DEFAULT_BASE = "ubuntu:24.04"
# --- heavy: what a heavy environment's image copies Docker from (HANGAR_HEAVY_DOCKER_IMAGE), and the packages
# dockerd needs from the base: iptables for its NAT and port publishing, e2fsprogs and mount for the loop-mounted disk
# under Kata (docs/environments.md#the-heavy-tier).
DOCKER_IMAGE = "docker:29.8.2-dind"
DOCKER_APT = ["iptables", "e2fsprogs", "mount", "pigz"]
DOCKER_BINARIES = ("docker", "dockerd", "containerd", "containerd-shim-runc-v2", "ctr", "runc", "docker-init",
                   "docker-proxy")
BASE_APT = [
    "ca-certificates", "curl", "git", "tmux", "sudo", "unzip", "xz-utils", "jq",
    "ripgrep", "less", "openssh-client", "python3", "python3-venv", "build-essential", "locales",
]


class Tools(BaseModel):
    apt: list[str] = Field(default_factory=list)
    # Language runtimes and CLIs via mise: {"node": "22", "python": "3.12", "uv": "latest", "go": "1.23"}
    mise: dict[str, str] = Field(default_factory=dict)
    script: str | None = None  # run as root after apt


class Dependencies(BaseModel):
    pip: list[str] = Field(default_factory=list)  # installed with `uv tool install` when uv is present, else pipx-style
    npm: list[str] = Field(default_factory=list)  # global npm packages
    script: str | None = None  # run as the sandbox user


class Skill(BaseModel):
    # Exactly one of path (relative to the spec file) or git ("url#subdir@ref").
    path: str | None = None
    git: str | None = None
    name: str | None = None
    harnesses: list[str] | None = None  # default: every installed harness with a skills_dir


class AgentConfig(BaseModel):
    instructions: str | None = None  # written to each harness's global instructions file
    files: dict[str, str] = Field(default_factory=dict)  # extra config files, path relative to $HOME


# --- envbuilds: what runs in a sandbox once it starts (docs/environments.md). The same models are used by an
# environment spec and by a repo's own .hangar/environment.yaml (repoenv.py), which may set only these.
_PROGRAM = r"^[a-z][a-z0-9-]{0,30}$"
ENVD_PORT_RESERVED = 49983  # hangar-envd's port (envd.PORT): never a terminal's, service's or preview's


def _port(v: int | None) -> int | None:
    if v is not None and (not 0 < v < 65536 or v == ENVD_PORT_RESERVED):
        raise ValueError(f"port {v} is not available (1-65535, not {ENVD_PORT_RESERVED})")
    return v


def _env(v: dict[str, str]) -> dict[str, str]:
    for k, val in v.items():
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", k) or len(val) > 4000 or "\0" in val:
            raise ValueError(f"invalid env entry '{k[:40]}'")
    return v


class Terminal(BaseModel):
    """A long-running process the supervisor keeps up: a dev server, a watcher (`hangar-agent svc`)."""

    model_config = ConfigDict(extra="forbid")
    name: str = Field(pattern=_PROGRAM)
    command: str = Field(min_length=1, max_length=4000)
    cwd: str | None = Field(None, max_length=500)  # relative to the repo checkout
    env: dict[str, str] = Field(default_factory=dict, max_length=64)  # not secrets: they live in hangar, by name
    port: int | None = None  # ready when something listens here
    ready: str | None = Field(None, max_length=2000)  # or when this command exits 0
    autostart: bool = True
    restart: Literal["always", "on-failure", "never"] = "always"

    @field_validator("port")
    @classmethod
    def _check_port(cls, v: int | None) -> int | None:
        return _port(v)

    @field_validator("env")
    @classmethod
    def _check_env(cls, v: dict[str, str]) -> dict[str, str]:
        return _env(v)

    @field_validator("cwd")
    @classmethod
    def _relative(cls, v: str | None) -> str | None:
        if v is not None and (v.startswith("/") or ".." in Path(v).parts or "\0" in v):
            raise ValueError("cwd is relative to the repo checkout, without '..'")
        return v


class Service(BaseModel):
    """A service image (Postgres, Redis...) run beside the sandbox: a pod sidecar on Kubernetes, a container on a
    network of the sandbox's own on Docker. Reachable from the sandbox at its name (and on Kubernetes at
    127.0.0.1), gone with the sandbox. Runs as uid 1000 without capabilities, like the sandbox.

    --- heavy: or a Compose stack, `compose: <file>` (relative to the repo checkout): the supervisor runs `docker
    compose -f <file> up` inside a heavy sandbox and keeps it up; `port` or `ready` say when it is ready (default:
    every container running and healthy, or exited 0). `services: {stack: docker-compose.yml}` is short for it."""

    model_config = ConfigDict(extra="forbid")
    image: str | None = Field(None, min_length=1, max_length=300, pattern=r"^[A-Za-z0-9][A-Za-z0-9._/:@+-]*$")
    port: int | None = None  # what it listens on; the readiness check and `hangar-agent svc status` use it
    compose: str | None = Field(None, min_length=1, max_length=300)  # --- heavy
    env: dict[str, str] = Field(default_factory=dict, max_length=64)
    command: list[str] | None = Field(None, max_length=64)
    args: list[str] | None = Field(None, max_length=64)
    volumes: list[str] = Field(default_factory=list, max_length=4)  # empty, writable dirs (e.g. a database's data)
    ready: str | None = Field(None, max_length=2000)  # a command run in the sandbox; default: the port answers
    cpus: float = Field(0.5, gt=0, le=4)
    memory_gb: float = Field(0.5, gt=0, le=8)

    @field_validator("port")
    @classmethod
    def _check_port(cls, v: int | None) -> int | None:
        return _port(v)

    @field_validator("env")
    @classmethod
    def _check_env(cls, v: dict[str, str]) -> dict[str, str]:
        return _env(v)

    @field_validator("compose")  # --- heavy
    @classmethod
    def _compose_path(cls, v: str | None) -> str | None:
        if v is not None and (v.startswith(("/", "-")) or ".." in Path(v).parts or "\0" in v or "\n" in v):
            raise ValueError("compose is a file relative to the repo checkout, without '..'")
        return v

    @model_validator(mode="after")
    def _kind(self) -> Service:  # --- heavy: an image service or a Compose stack
        if self.compose is not None:
            if self.image or self.command or self.args or self.volumes:
                raise ValueError("a compose service takes compose, and optionally port, ready and env; not image, "
                                 "command, args or volumes (those are the compose file's)")
        elif not self.image or self.port is None:
            raise ValueError("a service needs image and port (or compose: FILE for a Compose stack)")
        return self

    @field_validator("volumes")
    @classmethod
    def _abs(cls, v: list[str]) -> list[str]:
        for p in v:
            if not p.startswith("/") or ".." in Path(p).parts or len(p) > 300 or p.rstrip("/") in ("", "/proc",
                                                                                                  "/sys", "/dev"):
                raise ValueError(f"volume '{p[:60]}' must be an absolute path inside the service's image")
        return v

    @field_validator("command", "args")
    @classmethod
    def _argv(cls, v: list[str] | None) -> list[str] | None:
        if v is not None and any(len(a) > 4000 or "\0" in a for a in v):
            raise ValueError("arguments must be under 4000 characters, without NULs")
        return v


class PreviewPort(BaseModel):
    """A port the sandbox's owner may open through hangar's authenticated preview proxy (never public)."""

    model_config = ConfigDict(extra="forbid")
    port: int
    name: str | None = Field(None, pattern=_PROGRAM)

    @field_validator("port")
    @classmethod
    def _check_port(cls, v: int) -> int:
        return _port(v)  # type: ignore[return-value]


SERVICE_NAME = re.compile(r"^[a-z][a-z0-9-]{0,30}$")


def coerce_services(v: Any) -> Any:
    """--- heavy: `services: {stack: docker-compose.yml}` is `{stack: {compose: docker-compose.yml}}`."""
    if isinstance(v, dict):
        return {k: {"compose": x} if isinstance(x, str) else x for k, x in v.items()}
    return v


def check_services(v: dict[str, Service]) -> dict[str, Service]:
    if len(v) > 8:
        raise ValueError("at most 8 services")
    for name in v:
        if not SERVICE_NAME.match(name) or name in ("localhost", "hangar", "sandbox"):
            raise ValueError(f"invalid service name '{name[:40]}': lowercase letters, digits and '-'")
    return v


def check_programs(terminals: list[Terminal], services: dict[str, Service]) -> None:
    names = [t.name for t in terminals]
    if len(names) > 16:
        raise ValueError("at most 16 terminals")
    if dup := sorted({n for n in names if names.count(n) > 1} | (set(names) & set(services))):
        raise ValueError(f"names must be unique across terminals and services: {', '.join(dup)}")


class RepoSpec(BaseModel):
    url: str
    branch: str | None = None
    path: str | None = None  # checkout dir, default /workspace/<repo-name>
    setup: str | None = None  # run after clone (e.g. "uv sync", "npm ci")
    bake: bool = False  # clone + setup at image build time


class EnvSpec(BaseModel):
    name: str
    description: str = ""
    extends: str | None = None
    base: str = DEFAULT_BASE
    tools: Tools = Field(default_factory=Tools)
    # Prebuilt image to run as-is instead of building this spec (must meet the contract in
    # docs/specs/0006-environments-and-harness-packs.md). To customise a prebuilt image, use `base:` instead.
    image: str | None = None
    harnesses: list[str] = Field(default_factory=list)  # harnesses sandboxes of this env may use
    default_harness: str | None = None  # default: the first agent harness listed
    # pack: mount read-only harness packs (fails if the provider can't); bake: copy the selected harness
    # into a per-harness image variant; auto: pack when the provider supports image volumes, else bake.
    harness_mode: Literal["auto", "pack", "bake"] = "auto"
    # Default model for agent runs. Model ids are harness-specific, so prefer a map per harness:
    # {claude-code: opus, pi: openrouter/google/gemini-3.8-flash, antigravity: gemini-3.8-flash-high}.
    # A plain string applies to whichever harness the sandbox runs. --- canary: a policy, `frontier` or `balanced`
    # (modelpolicy.py), is resolved to the provider's concrete id when a sandbox or a session is created.
    model: str | dict[str, str] | None = None
    mcp: dict[str, McpServer] = Field(default_factory=dict)  # MCP servers, rendered per harness at claim
    secrets: dict[str, str] = Field(default_factory=dict)  # ENV_VAR -> credential name, injected at claim
    dependencies: Dependencies = Field(default_factory=Dependencies)
    skills: list[Skill] = Field(default_factory=list)
    # --- #136: Claude Code plugins, each `{path: <marketplace dir>, name, marketplace?, enabled?}`, installed at build
    # into the image's read-only seed (plugins.SEED) and enabled by hangar's own settings (docs/harnesses.md#plugins)
    plugins: list[Plugin] = Field(default_factory=list)
    agent_config: AgentConfig = Field(default_factory=AgentConfig)
    files: dict[str, str] = Field(default_factory=dict)  # absolute paths or relative to $HOME
    env: dict[str, str] = Field(default_factory=dict)  # non-secret env baked into the image
    repo: RepoSpec | None = None
    credentials: list[str] = Field(default_factory=list)  # default credentials to attach
    resources: Resources = Field(default_factory=Resources)
    warm_pool: int = 0
    # Keep the warm pool only during these hours (outside them it scales to 0, so idle nodes can go away), e.g.
    # "Mon-Fri 08:00-20:00 America/New_York". Days: Mon..Sun, ranges or commas; time zone optional (UTC).
    warm_pool_hours: str | None = None
    # Kubernetes only: RuntimeClass for this environment's pods, overriding HANGAR_K8S_RUNTIME_CLASS
    # ("gvisor", "kata-qemu-runtime-rs", or "" for runc). Lets gVisor and Kata environments share a cluster.
    runtime_class: str | None = None
    # --- providers-e2b: fast (microVMs restored from a snapshot), isolated or default; picks the provider when a
    # create names none (tiers.py, HANGAR_TIERS). Unset: HANGAR_DEFAULT_PROVIDER.
    tier: Literal["fast", "isolated", "default", "heavy"] | None = None
    # --- heavy: Docker inside the sandbox, on a VM-isolated tier (heavy.py, docs/environments.md#the-heavy-tier); the
    # image gets dockerd and the docker CLI. `tier: heavy` says the same. Needs sudo: dockerd runs as root in the VM.
    heavy: bool = False
    # --- heavy: a named size instead of `resources` (models.PROFILES); large and xlarge run on the heavy tier
    profile: Literal["standard", "large", "xlarge"] | None = None
    # --- #124: no passwordless sudo for the sandbox user unless asked (heavy needs it). Off by default: with sudo the
    # agent is root whenever it likes, which voids the uid split and hangar-owned files (isolation.py, harden.py).
    sudo: bool = False
    # --- workstream F: git gateway. direct: git credentials are written into the sandbox (the old way).
    # gateway: the sandbox gets a grant for its own branch and pushes through hangar's git gateway; no GitHub
    # token enters it. Unset: direct, except jobs and sessions use the gateway when HANGAR_GATEWAY_URL is set.
    credential_mode: Literal["direct", "gateway"] | None = None
    # --- workstream steer: how agents' tool permission prompts are answered in this environment's sessions and
    # jobs (auto | ask | deny-risky; docs/steering.md). Unset: HANGAR_APPROVALS.
    approvals: Literal["auto", "ask", "deny-risky"] | None = None
    # --- overnight/cache: prompt-cache TTL for this environment's sessions and jobs (auto | 5m | 1h | off; cache.py).
    # Unset: HANGAR_CACHE.
    cache: Literal["auto", "5m", "1h", "off"] | None = None
    # --- frontdoor: the check jobs in this environment run when neither the job nor its repo's policy names one
    # (repopolicy.py), e.g. "uv run pytest -q". Run in the repo checkout; "" means no check.
    verify: str | None = Field(None, max_length=2000)
    # --- egress: what sandboxes may reach on the internet: {mode: open|allowlist|none, presets: [...], allow: [...]}
    # (egress.py). Unset: HANGAR_EGRESS_DEFAULT. Needs an admin to set.
    egress: EgressSpec | None = None
    # --- egress: secrets for the image-build step only, never the running sandbox: {SECRET_ID: credential}
    # (buildsecrets.py). Needs an admin to set.
    build_secrets: dict[str, str] = Field(default_factory=dict)
    # --- egress: short-lived cloud credentials instead of stored keys: {gcp: {provider, service_account},
    # aws: {role_arn}, lifetime_minutes} (cloudcreds.py). Needs an admin to set.
    cloud: CloudSpec | None = None
    # --- computer: a display, Chromium (Playwright), VNC and screen recording for the agent (docs/computer-use.md).
    # `true` or {resolution, autostart, browser_mcp, record_max_seconds}.
    computer: ComputerSpec | None = None
    # --- envbuilds: the repo's own steps (docs/environments.md). `install` runs in a build sandbox after the clone, on
    # every build, never on a claim: it must be idempotent. `start` runs once each time a sandbox starts, then the
    # supervisor (`hangar-agent svc`) keeps `terminals` up. A repo's .hangar/environment.yaml may set each of these.
    install: str | None = Field(None, max_length=4000)
    start: str | None = Field(None, max_length=4000)
    terminals: list[Terminal] = Field(default_factory=list)
    services: dict[str, Service] = Field(default_factory=dict)
    ports: list[PreviewPort] = Field(default_factory=list, max_length=16)
    source_dir: Path | None = Field(default=None, exclude=True)
    # Resolved by the fleet before a provider sees the spec: harness name -> pack image to mount read-only.
    packs: dict[str, str] = Field(default_factory=dict, exclude=True)

    @field_validator("harnesses")
    @classmethod
    def _known_harnesses(cls, v: list[str]) -> list[str]:
        for h in v:
            get_harness(h)
        return v

    @field_validator("computer", mode="before")  # --- computer
    @classmethod
    def _computer_flag(cls, v: Any) -> Any:
        return _computer.coerce(v)

    @field_validator("warm_pool_hours")
    @classmethod
    def _check_hours(cls, v: str | None) -> str | None:
        if v:
            parse_hours(v)
        return v

    @field_validator("build_secrets")  # --- egress
    @classmethod
    def _build_secret_ids(cls, v: dict[str, str]) -> dict[str, str]:
        from hangar.buildsecrets import check_ids

        return check_ids(v)

    @field_validator("secrets")
    @classmethod
    def _secret_vars(cls, v: dict[str, str]) -> dict[str, str]:
        for var in v:
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", var):
                raise ValueError(f"invalid env var name in secrets: '{var}'")
        return v

    @field_validator("services", mode="before")
    @classmethod
    def _compose_short(cls, v: Any) -> Any:  # --- heavy
        return coerce_services(v)

    @field_validator("services")
    @classmethod
    def _check_services(cls, v: dict[str, Service]) -> dict[str, Service]:  # --- envbuilds
        return check_services(v)

    @model_validator(mode="after")
    def _unique_programs(self) -> EnvSpec:  # --- envbuilds
        check_programs(self.terminals, self.services)
        return self

    @model_validator(mode="after")
    def _heavy(self) -> EnvSpec:  # --- heavy
        if self.tier == "heavy":
            self.heavy = True
        elif self.heavy and self.tier is not None:
            raise ValueError(f"heavy is its own tier: drop `tier: {self.tier}`, or use `tier: heavy`")
        if self.heavy and not self.sudo:
            if "sudo" in self.model_fields_set:  # --- #124: said false; unset, heavy implies it
                raise ValueError("heavy needs sudo: true (dockerd runs as root inside the sandbox's VM)")
            self.sudo = True
        if self.profile is not None:
            self.resources = PROFILES[self.profile].model_copy()
        return self

    @model_validator(mode="after")
    def _plugins(self) -> EnvSpec:  # --- #136
        if self.plugins and "claude-code" not in self.harnesses:
            raise ValueError("plugins are Claude Code's: add claude-code to harnesses")
        if self.plugins and self.image:
            raise ValueError("plugins are installed when the image is built: a prebuilt `image:` can't get them "
                             "(use `base:` instead)")
        return self

    @model_validator(mode="after")
    def _default_harness_allowed(self) -> EnvSpec:
        if self.default_harness and self.default_harness not in self.agent_harnesses:
            raise ValueError(f"default_harness '{self.default_harness}' is not one of this environment's agent "
                             f"harnesses ({', '.join(self.agent_harnesses) or 'none'})")
        return self

    @field_validator("model")
    @classmethod
    def _model_keys(cls, v: str | dict[str, str] | None) -> str | dict[str, str] | None:
        if isinstance(v, dict):
            for h in v:
                get_harness(h)
        return v

    def model_for(self, harness: str | None) -> str | None:
        if isinstance(self.model, dict):
            return self.model.get(harness or "")
        return self.model

    @property
    def agent_harnesses(self) -> list[str]:
        return [h for h in self.harnesses if get_harness(h).agent]

    def pick_harness(self, requested: str | None = None) -> str | None:
        """The agent harness a sandbox runs: the requested one (must be allowed), else the default."""
        if requested:
            get_harness(requested)
            if requested not in self.agent_harnesses:
                raise ValueError(f"harness '{requested}' is not enabled in environment '{self.name}' "
                                 f"(allowed: {', '.join(self.agent_harnesses) or 'none'})")
            return requested
        return self.default_harness or next(iter(self.agent_harnesses), None)


NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")


def _safe_name(name: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}", name or ""):
        raise ValueError(f"invalid name '{name}': use letters, digits, '.', '_' or '-'")
    return name


def _contained_path(root: Path, rel: str) -> Path:
    """Resolve a spec-relative path, refusing anything outside the spec's directory."""
    if Path(rel).is_absolute():
        raise ValueError(f"skill path must be relative to the spec directory: {rel}")
    base = root.resolve()
    target = (base / rel).resolve()
    if not target.is_relative_to(base):
        raise ValueError(f"skill path escapes the spec directory: {rel}")
    if not target.is_dir():
        raise FileNotFoundError(f"skill path not found: {rel}")
    return target


# --------------------------------------------------------------------------- loading
# How `extends` merges (docs/environments.md "How `extends` merges"). Maps merge by key, a child's `null` deleting
# a key (MAP_FIELDS); scalars replace; lists follow LIST_MERGE, matched by dotted path as the merge recurses, and
# anything not listed replaces. A child's list is the whole list: it can drop what its parent had. Build inputs that
# only install more (apt packages, pip and npm tools) union instead: installing more never changes what the agent is
# told. `skills_remove: [name, ...]` then drops inherited skills by name without restating the list.
LIST_MERGE: dict[str, str] = {
    "skills": "replace", "harnesses": "replace", "credentials": "replace", "terminals": "replace",
    "ports": "replace",
    # additive build inputs (installing more never changes the agent's instructions)
    "tools.apt": "union", "dependencies.pip": "union", "dependencies.npm": "union",
    # egress hosts add up along a chain, as they always have (tests/test_egress.py); `presets` replaces ([]: none)
    "egress.allow": "union",
}  # anything else: replace
MAP_FIELDS = frozenset({"mcp", "secrets", "env", "files", "agent_config.files", "services", "tools.mise",
                        "build_secrets"})


def _deep_merge(base: dict[str, Any], over: dict[str, Any], path: str = "") -> dict[str, Any]:
    out = dict(base)
    for k, v in over.items():
        key = f"{path}.{k}" if path else k
        if v is None and path in MAP_FIELDS:
            out.pop(k, None)  # a child deletes one of its parent's entries
        elif isinstance(v, dict) and (isinstance(out.get(k), dict) or (key in MAP_FIELDS and out.get(k) is None)):
            out[k] = _deep_merge(out.get(k) or {}, v, key)
        elif isinstance(v, list) and isinstance(out.get(k), list) and LIST_MERGE.get(key) == "union":
            out[k] = list(dict.fromkeys([*out[k], *v]))
        else:
            out[k] = v
    return out


def _legacy_merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    """The rules before #132 (every list but four appended), kept only for `hangar env migrate-merge`."""
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _legacy_merge(out[k], v)
        elif isinstance(v, list) and isinstance(out.get(k), list) and k not in ("credentials", "harnesses",
                                                                                  "terminals", "ports"):
            out[k] = list(dict.fromkeys([*out[k], *v])) if all(isinstance(x, str) for x in v) else [*out[k], *v]
        else:
            out[k] = v
    return out


def skill_name(skill: Skill | dict[str, Any]) -> str | None:
    """The name a skill is installed under: its `name`, else its directory's (as `render` names it)."""
    d = skill.model_dump() if isinstance(skill, BaseModel) else skill
    if d.get("name"):
        return d["name"]
    if d.get("path"):
        return Path(d["path"]).name
    if d.get("git"):
        url, _, rest = d["git"].partition("#")
        subdir = rest.partition("@")[0]
        return Path(subdir).name if subdir else Path(url).stem
    return None


def remove_skills(skills: list[Any], remove: list[str], where: str) -> list[Any]:
    """`skills` without the named ones. Naming a skill that isn't there is an error that lists the known names."""
    known = [skill_name(s) for s in skills]
    if missing := [n for n in remove if n not in known]:
        raise ValueError(f"{where}: skills_remove names {', '.join(missing)}, which it doesn't have "
                         f"(known: {', '.join(sorted(n for n in known if n)) or 'none'})")
    return [s for s, n in zip(skills, known, strict=True) if n not in remove]


class EnvCatalog:
    """Loads *.yaml environment specs from one or more directories and resolves `extends`.

    Later directories override earlier ones. `save_dir` is where specs created through the API/MCP are written;
    the server points it at the data volume so they survive redeploys (the shipped envs dir is in the image)."""

    def __init__(self, dirs: list[Path], save_dir: Path | None = None):
        self.save_dir = save_dir
        self.dirs = [*dirs, save_dir] if save_dir is not None and save_dir not in dirs else list(dirs)
        self._raw: dict[str, tuple[dict[str, Any], Path]] = {}
        self.reload()

    def reload(self) -> None:
        self._raw.clear()
        for d in self.dirs:
            if not d.exists():
                continue
            for f in sorted([*d.glob("*.yaml"), *d.glob("*.yml")]):
                data = yaml.safe_load(f.read_text()) or {}
                name = data.get("name") or f.stem
                data["name"] = name
                self._raw[name] = (data, f.parent)

    def names(self) -> list[str]:
        return sorted(self._raw)

    def add(self, data: dict[str, Any], save_dir: Path | None = None) -> EnvSpec:
        """Register (and optionally persist) a spec created through the API/UI."""
        name = data["name"]
        if not NAME_RE.match(str(name)):
            raise ValueError(f"invalid environment name '{name}': lowercase letters, digits, '.', '_', '-'")
        target = save_dir or self.save_dir or (self.dirs[0] if self.dirs else Path("envs"))
        self._raw[name] = (data, target)
        spec = self.get(name)  # validates
        if save_dir is not None or self.save_dir is not None or self.dirs:
            target.mkdir(parents=True, exist_ok=True)
            (target / f"{name}.yaml").write_text(yaml.safe_dump(data, sort_keys=False))
        return spec

    def raw(self, name: str) -> dict[str, Any]:
        return self._raw[name][0]

    def get(self, name: str, _seen: tuple[str, ...] = (), _merge: Any = _deep_merge) -> EnvSpec:
        if name not in self._raw:
            raise KeyError(f"unknown environment '{name}'. Known: {', '.join(self.names()) or '(none)'}")
        if name in _seen:
            raise ValueError(f"circular extends: {' -> '.join((*_seen, name))}")
        data, src = self._raw[name]
        if parent := data.get("extends"):
            parent_spec = self.get(parent, (*_seen, name), _merge=_merge)
            # --- #124: a parent's default sudo (false) isn't a "no" its children inherit (heavy implies sudo)
            skip = {"source_dir"} | ({"sudo"} if "sudo" not in parent_spec.model_fields_set else set())
            merged = _merge(parent_spec.model_dump(exclude=skip), data)
        else:
            merged = _merge({}, data)  # a `null` map entry with no parent to delete from is simply left out
        if (remove := merged.pop("skills_remove", None)) is not None:  # never inherited: applied here, then gone
            if not isinstance(remove, list) or not all(isinstance(x, str) for x in remove):
                raise ValueError(f"environment {name}: skills_remove is a list of skill names")
            merged["skills"] = remove_skills(merged.get("skills") or [], remove, f"environment {name}")
        spec = EnvSpec.model_validate(merged)
        spec.source_dir = src
        return spec


# --------------------------------------------------------------------------- rendering
def _home(user: str) -> str:
    return f"/home/{user}"


def _resolve_home_path(path: str, user: str) -> str:
    return path if path.startswith("/") else f"{_home(user)}/{path}"


class BuildContext(BaseModel):
    dockerfile: str
    files: dict[str, bytes] = Field(default_factory=dict)  # path in context -> content
    digest: str

    def tar(self) -> bytes:
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tf:
            for path, data in {"Dockerfile": self.dockerfile.encode(), **self.files}.items():
                info = tarfile.TarInfo(path)
                info.size = len(data)
                info.mode = 0o644
                tf.addfile(info, io.BytesIO(data))
        return buf.getvalue()


def render(spec: EnvSpec, user: str = "agent", workspace: str = "/workspace",
           extra_ca: bytes | None = None, bake: list[str] | None = None,
           docker_image: str | None = None) -> BuildContext:
    """Render the image build. `bake` lists harnesses to copy into the image (the fallback when packs
    can't be mounted); by default the image carries no harness binaries at all. --- heavy: a heavy environment's
    image also gets Docker, copied from `docker_image` (HANGAR_HEAVY_DOCKER_IMAGE)."""
    home = _home(user)
    files: dict[str, bytes] = {}
    step_no = 0

    def _step(script: str, as_user: str | None = None) -> str:
        """One build step. Steps are shipped as script files rather than RUN heredocs so the
        Dockerfile works on every builder: BuildKit, the legacy builder, Cloud Build, Kaniko."""
        nonlocal step_no
        step_no += 1
        name = f"steps/{step_no:02d}.sh"
        files[name] = f"#!/bin/bash\nset -euxo pipefail\n{script.strip()}\n".encode()
        prefix = f"USER {as_user}\n" if as_user else "USER root\n"
        return f"{prefix}COPY {name} /tmp/hangar-steps/{step_no:02d}.sh\nRUN bash /tmp/hangar-steps/{step_no:02d}.sh\n"
    harnesses = [get_harness(h) for h in spec.harnesses]
    baked = [get_harness(h) for h in dict.fromkeys(bake or [])]
    for h in baked:
        if h.name not in spec.harnesses:
            raise ValueError(f"cannot bake harness '{h.name}': not enabled in environment '{spec.name}'")

    mise = dict(spec.tools.mise)
    if spec.dependencies.npm:
        mise.setdefault("node", "22")
    if spec.dependencies.pip:
        mise.setdefault("uv", "latest")
    if spec.computer and spec.computer.browser_mcp:  # --- computer: Playwright's MCP server runs on Node
        mise.setdefault("node", "22")

    df: list[str] = [f"# hangar environment: {spec.name}"]
    baked = [h for h in baked if h.install]
    for h in baked:  # build stages must precede the final stage
        files[f"packs/{h.name}.sh"] = pack_script(h).encode()
        df.append(pack_stage(h))
    if spec.plugins:  # --- #136: the seed is built with the locked Claude Code, in the pack's own build stage
        claude = get_harness(_plugins.HARNESS)
        if claude.name not in {h.name for h in baked}:
            files[f"packs/{claude.name}.sh"] = pack_script(claude).encode()
            df.append(pack_stage(claude))
        stage, seed_files = _plugins.build(spec, f"{claude.prefix}/bin/claude")
        files.update(seed_files)
        df.append(stage)
    if spec.heavy:  # --- heavy: static binaries, so an Alpine-based image's Docker runs on any base
        df.append(f"FROM {docker_image or DOCKER_IMAGE} AS hangar-docker")
    df += [
        f"FROM {spec.base}",
        "ENV DEBIAN_FRONTEND=noninteractive LANG=C.UTF-8 LC_ALL=C.UTF-8",
        "SHELL [\"/bin/bash\", \"-o\", \"pipefail\", \"-c\"]",
    ]

    # 1. system packages + sandbox user
    apt = list(dict.fromkeys([*BASE_APT, *(DOCKER_APT if spec.heavy else []), *spec.tools.apt]))  # --- heavy
    df.append(
        _step(
            "apt-get update\n"
            f"apt-get install -y --no-install-recommends {' '.join(apt)}\n"
            "rm -rf /var/lib/apt/lists/*\n"
            # ubuntu images ship a uid-1000 'ubuntu' user; reuse uid 1000 for the sandbox user.
            "if id ubuntu >/dev/null 2>&1; then userdel -r ubuntu || true; fi\n"
            f"id {user} >/dev/null 2>&1 || useradd -m -u 1000 -s /bin/bash {user}\n"
            + (f"echo '{user} ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/{user}\n" if spec.sudo else "")
            + f"mkdir -p {workspace} && chown {user}:{user} {workspace}"
        )
    )
    if extra_ca:
        files["certs/hangar-extra-ca.crt"] = extra_ca
        df.append("COPY certs/hangar-extra-ca.crt /usr/local/share/ca-certificates/hangar-extra-ca.crt")
        df.append("RUN update-ca-certificates >/dev/null")
        df.append("ENV SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt "
                  "REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt "
                  "NODE_EXTRA_CA_CERTS=/usr/local/share/ca-certificates/hangar-extra-ca.crt "
                  "GIT_SSL_CAINFO=/etc/ssl/certs/ca-certificates.crt")
    if spec.heavy:  # --- heavy: Docker's binaries, whichever of them this release ships (containerd may be built in)
        df.append("COPY --from=hangar-docker /usr/local/bin/ /opt/hangar/docker/bin/")
        df.append("COPY --from=hangar-docker /usr/local/libexec/docker/cli-plugins/ "
                  "/usr/local/libexec/docker/cli-plugins/")
        df.append(_step(
            f"for b in {' '.join(DOCKER_BINARIES)}; do\n"
            "  if [ -f /opt/hangar/docker/bin/$b ]; then ln -sf /opt/hangar/docker/bin/$b /usr/local/bin/$b; fi\n"
            "done\n"
            "dockerd --version && docker --version && docker compose version"))
    if spec.tools.script:
        df.append(_step(spec.tools.script))
    if spec.computer:  # --- computer: display, VNC, ffmpeg, Chromium
        df.append(_step(_computer.root_script(spec.computer, user)))
        df.append(_computer.IMAGE_ENV)

    # 2. language runtimes via mise
    path_env = f"{home}/.local/bin:{home}/.local/share/mise/shims"
    df.append(f"ENV PATH={path_env}:$PATH HANGAR_USER={user} HANGAR_WORKSPACE={workspace}")
    if mise:
        uses = " ".join(f"{shlex.quote(k)}@{shlex.quote(v)}" for k, v in sorted(mise.items()))
        df.append(
            _step(
                "curl -fsSL https://mise.run | sh\n"
                f"mise use -g {uses}\n"
                "mise reshim",
                as_user=user,
            )
        )

    # 3. baked harnesses (fallback for providers that can't mount packs): same prefix and layout as a pack
    for h in baked:
        df.append(f"# harness: {h.name}\nCOPY --from=pack-{h.name} {h.prefix} {h.prefix}")
    if spec.plugins:  # --- #136: root's and read-only, out of the agent's reach
        df.append(f"# claude code plugin seed\nCOPY --chown=root:root --from=hangar-claude-seed {_plugins.SEED} "
                  f"{_plugins.SEED}")

    # 4. dependencies
    dep_steps: list[str] = []
    if spec.dependencies.npm:
        dep_steps.append("npm install -g " + " ".join(map(shlex.quote, spec.dependencies.npm)))
    for pkg in spec.dependencies.pip:
        dep_steps.append(f"uv tool install {shlex.quote(pkg)}")
    if dep_steps and mise:
        dep_steps.append("mise reshim || true")
    if spec.dependencies.script:
        dep_steps.append(spec.dependencies.script)
    if dep_steps:
        df.append(_step("\n".join(dep_steps), as_user=user))
    if spec.computer and (mcp_step := _computer.user_script(spec.computer)):  # --- computer
        df.append(_step(mcp_step, as_user=user))

    # 5. skills, agent config and files -> staged in the build context, then COPY'd
    staged: dict[str, str] = {}  # context path -> absolute destination
    git_skill_steps: list[str] = []
    skill_targets = [h for h in harnesses if h.skills_dir]
    for i, skill in enumerate(spec.skills):
        targets = [h for h in skill_targets if skill.harnesses is None or h.name in skill.harnesses]
        if skill.path:
            src = _contained_path(spec.source_dir or Path("."), skill.path)
            sname = _safe_name(skill.name or src.name)
            for f in sorted(p for p in src.rglob("*") if p.is_file() and not p.is_symlink()):
                if not f.resolve().is_relative_to(src.resolve()):
                    continue  # symlinked dirs pointing outside the skill
                rel = f.relative_to(src).as_posix()
                ctx = f"skills/{i}/{rel}"
                files[ctx] = f.read_bytes()
                for h in targets:
                    staged[ctx + f"@{h.name}"] = f"{home}/{h.skills_dir}/{sname}/{rel}"
        elif skill.git:
            url, _, rest = skill.git.partition("#")
            subdir, _, ref = rest.partition("@")
            sname = _safe_name(skill.name or (Path(subdir).name if subdir else Path(url).stem))
            if subdir and (subdir.startswith("/") or ".." in Path(subdir).parts):
                raise ValueError(f"skill git subdir must be relative: {subdir}")
            tmp = f"/tmp/skill-{i}"
            git_skill_steps.append(
                f"git clone --depth 1 {'--branch ' + shlex.quote(ref) + ' ' if ref else ''}{shlex.quote(url)} {tmp}"
            )
            for h in targets:
                dest = f"{home}/{h.skills_dir}/{sname}"
                git_skill_steps.append(f"mkdir -p {dest} && cp -r {tmp}/{shlex.quote(subdir or '.')}/. {dest}/")
            git_skill_steps.append(f"rm -rf {tmp}")

    def stage_text(dest: str, content: str) -> None:
        if dest.endswith("/.claude/settings.json"):  # --- #136: no marketplace auto-update in a sandbox
            with contextlib.suppress(ValueError):
                loaded = json.loads(content)
                if (pinned := _plugins.pin_marketplaces(loaded)) is not loaded:
                    content = json.dumps(pinned, indent=2)
        ctx = "files/" + hashlib.sha1(dest.encode()).hexdigest()[:12] + "-" + Path(dest).name
        files[ctx] = content.encode()
        staged[ctx] = dest

    if spec.agent_config.instructions:
        for h in harnesses:
            if h.instructions_file:
                stage_text(f"{home}/{h.instructions_file}", spec.agent_config.instructions)
    for rel, content in spec.agent_config.files.items():
        stage_text(_resolve_home_path(rel, user), content)
    for path, content in spec.files.items():
        stage_text(_resolve_home_path(path, user), content)

    for ctx, dest in sorted(staged.items(), key=lambda kv: kv[1]):
        src = ctx.split("@")[0]
        owner = f"{user}:{user}" if dest.startswith(home) or dest.startswith(workspace) else "root:root"
        df.append(f"COPY --chown={owner} {src} {dest}")
    # COPY creates missing parent dirs as root; hand the top-level dirs under $HOME back to the user.
    top_dirs = sorted({f"{home}/{d.removeprefix(home + '/').split('/')[0]}" for d in staged.values()
                       if d.startswith(home + "/") and "/" in d.removeprefix(home + "/")})
    if top_dirs:
        df.append("USER root\nRUN chown -R " + f"{user}:{user} " + " ".join(top_dirs))
    if git_skill_steps:
        df.append(_step("\n".join(git_skill_steps), as_user=user))

    if spec.env:
        df.append("ENV " + " ".join(f"{k}={shlex.quote(v)}" for k, v in sorted(spec.env.items())))

    # 6. optional baked repo
    if spec.repo and spec.repo.bake:
        dest = spec.repo.path or f"{workspace}/{repo_dirname(spec.repo.url)}"
        steps = [f"git clone {'--branch ' + shlex.quote(spec.repo.branch) + ' ' if spec.repo.branch else ''}"
                 f"{shlex.quote(spec.repo.url)} {dest}"]
        if spec.repo.setup:
            steps.append(f"cd {dest} && {spec.repo.setup}")
        df.append(_step("\n".join(steps), as_user=user))

    # shell niceties: secrets env file sourced by every login/interactive shell
    df.append(
        _step(
            f"mkdir -p {home}/.hangar && touch {home}/.hangar/env && chmod 700 {home}/.hangar\n"
            f"grep -q 'hangar/env' {home}/.bashrc || "
            "printf '\\n# hangar: injected credentials + env\\numask 022\\n"
            "set -a; [ -f ~/.hangar/env ] && . ~/.hangar/env; set +a\\n'"
            f" | tee -a {home}/.bashrc >> {home}/.profile\n"
            "printf 'set -g mouse on\\nset -g history-limit 50000\\n"
            "set -g default-terminal \"screen-256color\"\\n'"
            f" > {home}/.tmux.conf",
            as_user=user,
        )
    )
    df.append(f"USER {user}\nWORKDIR {workspace}")
    df.append('CMD ["sleep", "infinity"]')

    dockerfile = "\n".join(df) + "\n"
    h = hashlib.sha256(dockerfile.encode())
    for k in sorted(files):
        h.update(k.encode())
        h.update(files[k])
    return BuildContext(dockerfile=dockerfile, files=files, digest=h.hexdigest()[:16])


def repo_dirname(url: str) -> str:
    name = url.rstrip("/").split("/")[-1].split(":")[-1]
    return name[:-4] if name.endswith(".git") else name


def image_tag(spec: EnvSpec, ctx: BuildContext, prefix: str, registry: str | None = None) -> str:
    repo = f"{prefix}/{spec.name}"
    if registry:
        repo = f"{registry.rstrip('/')}/{spec.name}"
    return f"{repo}:{ctx.digest}"


def merge_changes(catalog: EnvCatalog) -> dict[str, list[str]]:
    """What resolving each environment under the #132 rules changes against the rules before (`hangar env
    migrate-merge --dry-run`): per environment, the skills it loses or gains and the other fields that differ.
    Environments that resolve the same are left out; one that no longer resolves says why."""
    out: dict[str, list[str]] = {}
    for name in catalog.names():
        try:
            old = catalog.get(name, _merge=_legacy_merge)
        except Exception:  # noqa: BLE001 - it didn't resolve before either: nothing changes
            continue
        try:
            new = catalog.get(name)
        except Exception as e:  # noqa: BLE001
            out[name] = [f"no longer resolves: {e}"]
            continue
        lines: list[str] = []
        before, after = [skill_name(x) for x in old.skills], [skill_name(x) for x in new.skills]
        if lost := [n for n in before if n not in after]:
            lines.append(f"skills lost: {', '.join(str(n) for n in lost)}")
        if gained := [n for n in after if n not in before]:
            lines.append(f"skills gained: {', '.join(str(n) for n in gained)}")
        a, b = old.model_dump(mode="json", exclude={"skills"}), new.model_dump(mode="json", exclude={"skills"})
        for path in _diff(a, b):
            lines.append(f"{path}: {_short(_at(a, path))} -> {_short(_at(b, path))}")
        if old.skills != new.skills and not lost and not gained:
            lines.append("skills: same names, different entries")
        if lines:
            out[name] = lines
    return out


def _diff(a: Any, b: Any, path: str = "") -> list[str]:
    if isinstance(a, dict) and isinstance(b, dict):
        return [p for k in sorted(set(a) | set(b)) for p in _diff(a.get(k), b.get(k), f"{path}.{k}" if path else k)]
    return [] if a == b else [path]


def _at(d: Any, path: str) -> Any:
    for part in path.split("."):
        d = d.get(part) if isinstance(d, dict) else None
    return d


def _short(v: Any) -> str:
    text = json.dumps(v, default=str)
    return text if len(text) <= 120 else text[:117] + "..."


__all__ = ["EnvSpec", "EnvCatalog", "BuildContext", "render", "image_tag", "repo_dirname", "HARNESSES"]


_DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
_HOURS_RE = re.compile(r"^\s*([A-Za-z,\-]+)\s+(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})(?:\s+(\S+))?\s*$")


def parse_hours(text: str) -> tuple[set[int], int, int, str]:
    """"Mon-Fri 08:00-20:00 America/New_York" -> ({0..4}, 480, 1200, "America/New_York")."""
    m = _HOURS_RE.match(text or "")
    if not m:
        raise ValueError(f"warm_pool_hours '{text}': expected like 'Mon-Fri 08:00-20:00 America/New_York'")
    days: set[int] = set()
    for part in m.group(1).lower().split(","):
        a, _, b = part.partition("-")
        if a[:3] not in _DAYS or (b and b[:3] not in _DAYS):
            raise ValueError(f"warm_pool_hours: unknown day in '{part}'")
        i, j = _DAYS.index(a[:3]), _DAYS.index((b or a)[:3])
        days |= set(range(i, j + 1)) if i <= j else set(range(i, 7)) | set(range(0, j + 1))
    start, end = int(m.group(2)) * 60 + int(m.group(3)), int(m.group(4)) * 60 + int(m.group(5))
    tz = m.group(6) or "UTC"
    from zoneinfo import ZoneInfo

    ZoneInfo(tz)  # validate
    return days, start, end, tz


def warm_pool_now(spec: EnvSpec, when: float | None = None) -> int:
    """The warm pool size that applies right now (spec.warm_pool, or 0 outside warm_pool_hours)."""
    if not spec.warm_pool or not spec.warm_pool_hours:
        return max(0, spec.warm_pool)
    import datetime as _dt
    from zoneinfo import ZoneInfo

    days, start, end, tz = parse_hours(spec.warm_pool_hours)
    t = _dt.datetime.fromtimestamp(when if when is not None else _dt.datetime.now().timestamp(), ZoneInfo(tz))
    minute, day = t.hour * 60 + t.minute, t.weekday()
    if start == end:  # "00:00-00:00": all day on the listed days
        inside = day in days
    elif start < end:
        inside = day in days and start <= minute < end
    else:  # past midnight: the early-morning part belongs to the window that started the day before
        inside = (day in days and minute >= start) or ((day - 1) % 7 in days and minute < end)
    return spec.warm_pool if inside else 0
