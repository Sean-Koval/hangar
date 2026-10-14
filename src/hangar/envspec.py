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

import hashlib
import io
import re
import shlex
import tarfile
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

from hangar.cloudcreds import CloudSpec  # --- egress
from hangar.egress import EgressSpec  # --- egress
from hangar.harnesses import HARNESSES, McpServer, get_harness, pack_script, pack_stage
from hangar.models import Resources

DEFAULT_BASE = "ubuntu:24.04"
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
    # A plain string applies to whichever harness the sandbox runs.
    model: str | dict[str, str] | None = None
    mcp: dict[str, McpServer] = Field(default_factory=dict)  # MCP servers, rendered per harness at claim
    secrets: dict[str, str] = Field(default_factory=dict)  # ENV_VAR -> credential name, injected at claim
    dependencies: Dependencies = Field(default_factory=Dependencies)
    skills: list[Skill] = Field(default_factory=list)
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
    tier: Literal["fast", "isolated", "default"] | None = None
    sudo: bool = True
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
    source_dir: Path | None = Field(default=None, exclude=True)
    # Resolved by the fleet before a provider sees the spec: harness name -> pack image to mount read-only.
    packs: dict[str, str] = Field(default_factory=dict, exclude=True)

    @field_validator("harnesses")
    @classmethod
    def _known_harnesses(cls, v: list[str]) -> list[str]:
        for h in v:
            get_harness(h)
        return v

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
def _deep_merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        # `harnesses` and `credentials` replace the parent's: a child narrows which agents it allows.
        elif isinstance(v, list) and isinstance(out.get(k), list) and k not in ("credentials", "harnesses"):
            out[k] = list(dict.fromkeys([*out[k], *v])) if all(isinstance(x, str) for x in v) else [*out[k], *v]
        else:
            out[k] = v
    return out


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

    def get(self, name: str, _seen: tuple[str, ...] = ()) -> EnvSpec:
        if name not in self._raw:
            raise KeyError(f"unknown environment '{name}'. Known: {', '.join(self.names()) or '(none)'}")
        if name in _seen:
            raise ValueError(f"circular extends: {' -> '.join((*_seen, name))}")
        data, src = self._raw[name]
        if parent := data.get("extends"):
            parent_spec = self.get(parent, (*_seen, name))
            merged = _deep_merge(parent_spec.model_dump(exclude={"source_dir"}), data)
        else:
            merged = data
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
           extra_ca: bytes | None = None, bake: list[str] | None = None) -> BuildContext:
    """Render the image build. `bake` lists harnesses to copy into the image (the fallback when packs
    can't be mounted); by default the image carries no harness binaries at all."""
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

    df: list[str] = [f"# hangar environment: {spec.name}"]
    baked = [h for h in baked if h.install]
    for h in baked:  # build stages must precede the final stage
        files[f"packs/{h.name}.sh"] = pack_script(h).encode()
        df.append(pack_stage(h))
    df += [
        f"FROM {spec.base}",
        "ENV DEBIAN_FRONTEND=noninteractive LANG=C.UTF-8 LC_ALL=C.UTF-8",
        "SHELL [\"/bin/bash\", \"-o\", \"pipefail\", \"-c\"]",
    ]

    # 1. system packages + sandbox user
    apt = list(dict.fromkeys([*BASE_APT, *spec.tools.apt]))
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
    if spec.tools.script:
        df.append(_step(spec.tools.script))

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
