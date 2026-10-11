"""Environment specs: a declarative, layered description of a coding sandbox.

Layers are rendered into a Dockerfile in cache-friendly order, so changing a repo setup
command does not reinstall your harnesses:

    base -> system packages -> language runtimes (mise) -> agent harnesses
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
from typing import Any

import yaml
from pydantic import BaseModel, Field, field_validator

from hangar.harnesses import HARNESSES, get_harness
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
    harnesses: list[str] = Field(default_factory=list)
    dependencies: Dependencies = Field(default_factory=Dependencies)
    skills: list[Skill] = Field(default_factory=list)
    agent_config: AgentConfig = Field(default_factory=AgentConfig)
    files: dict[str, str] = Field(default_factory=dict)  # absolute paths or relative to $HOME
    env: dict[str, str] = Field(default_factory=dict)  # non-secret env baked into the image
    repo: RepoSpec | None = None
    credentials: list[str] = Field(default_factory=list)  # default credentials to attach
    resources: Resources = Field(default_factory=Resources)
    warm_pool: int = 0
    sudo: bool = True
    source_dir: Path | None = Field(default=None, exclude=True)

    @field_validator("harnesses")
    @classmethod
    def _known_harnesses(cls, v: list[str]) -> list[str]:
        for h in v:
            get_harness(h)
        return v


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
        elif isinstance(v, list) and isinstance(out.get(k), list) and k not in ("credentials",):
            out[k] = list(dict.fromkeys([*out[k], *v])) if all(isinstance(x, str) for x in v) else [*out[k], *v]
        else:
            out[k] = v
    return out


class EnvCatalog:
    """Loads *.yaml environment specs from one or more directories and resolves `extends`."""

    def __init__(self, dirs: list[Path]):
        self.dirs = dirs
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
        target = save_dir or (self.dirs[0] if self.dirs else Path("envs"))
        self._raw[name] = (data, target)
        spec = self.get(name)  # validates
        if save_dir is not None or self.dirs:
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


def _heredoc_run(script: str, user: str | None = None) -> str:
    """A RUN step executing a multi-line script via BuildKit heredoc."""
    prefix = f"USER {user}\n" if user else ""
    return f"{prefix}RUN <<'HANGAR_EOF'\nset -euxo pipefail\n{script.strip()}\nHANGAR_EOF\n"


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
           extra_ca: bytes | None = None) -> BuildContext:
    home = _home(user)
    files: dict[str, bytes] = {}
    harnesses = [get_harness(h) for h in spec.harnesses]

    needs = {n for h in harnesses for n in h.needs}
    mise = dict(spec.tools.mise)
    if "node" in needs or spec.dependencies.npm:
        mise.setdefault("node", "22")
    if spec.dependencies.pip:
        mise.setdefault("uv", "latest")

    df: list[str] = [
        f"# hangar environment: {spec.name}",
        f"FROM {spec.base}",
        "ENV DEBIAN_FRONTEND=noninteractive LANG=C.UTF-8 LC_ALL=C.UTF-8",
        "SHELL [\"/bin/bash\", \"-o\", \"pipefail\", \"-c\"]",
    ]

    # 1. system packages + sandbox user
    apt = list(dict.fromkeys([*BASE_APT, *spec.tools.apt]))
    df.append(
        _heredoc_run(
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
        df.append(_heredoc_run(spec.tools.script))

    # 2. language runtimes via mise
    path_env = f"{home}/.local/bin:{home}/.local/share/mise/shims"
    df.append(f"ENV PATH={path_env}:$PATH HANGAR_USER={user} HANGAR_WORKSPACE={workspace}")
    if mise:
        uses = " ".join(f"{shlex.quote(k)}@{shlex.quote(v)}" for k, v in sorted(mise.items()))
        df.append(
            _heredoc_run(
                "curl -fsSL https://mise.run | sh\n"
                f"mise use -g {uses}\n"
                "mise reshim",
                user=user,
            )
        )

    # 3. agent harnesses
    harness_env = {k: v for h in harnesses for k, v in h.env.items()}
    if harness_env:
        df.append("ENV " + " ".join(f"{k}={shlex.quote(v)}" for k, v in sorted(harness_env.items())))
    for h in harnesses:
        if h.install == "true" and not h.first_run:
            continue
        steps = [f"# harness: {h.name}", h.install]
        if mise:
            steps.append("mise reshim || true")
        if h.first_run:
            steps.append(h.first_run)
        steps.append(h.verify)
        df.append(_heredoc_run("\n".join(steps), user=user))

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
        df.append(_heredoc_run("\n".join(dep_steps), user=user))

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
        df.append(_heredoc_run("\n".join(git_skill_steps), user=user))

    if spec.env:
        df.append("ENV " + " ".join(f"{k}={shlex.quote(v)}" for k, v in sorted(spec.env.items())))

    # 6. optional baked repo
    if spec.repo and spec.repo.bake:
        dest = spec.repo.path or f"{workspace}/{repo_dirname(spec.repo.url)}"
        steps = [f"git clone {'--branch ' + shlex.quote(spec.repo.branch) + ' ' if spec.repo.branch else ''}"
                 f"{shlex.quote(spec.repo.url)} {dest}"]
        if spec.repo.setup:
            steps.append(f"cd {dest} && {spec.repo.setup}")
        df.append(_heredoc_run("\n".join(steps), user=user))

    # shell niceties: secrets env file sourced by every login/interactive shell
    df.append(
        _heredoc_run(
            f"mkdir -p {home}/.hangar && touch {home}/.hangar/env && chmod 700 {home}/.hangar\n"
            f"grep -q 'hangar/env' {home}/.bashrc || "
            "printf '\\n# hangar: injected credentials + env\\numask 022\\n"
            "set -a; [ -f ~/.hangar/env ] && . ~/.hangar/env; set +a\\n'"
            f" | tee -a {home}/.bashrc >> {home}/.profile\n"
            "printf 'set -g mouse on\\nset -g history-limit 50000\\n"
            "set -g default-terminal \"screen-256color\"\\n'"
            f" > {home}/.tmux.conf",
            user=user,
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
