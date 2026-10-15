"""A repo's own environment config: `.hangar/environment.yaml` (--- envbuilds; docs/environments.md).

The file lives with the code, so the environment changes in the same commit as the code that needs it:

    extends: python               # a hangar environment the repo's HANGAR_GITHUB_REPOS entry allows (none: ignored)
    install: uv sync --frozen     # idempotent; runs in a build sandbox on every build, never on a claim
    start: ./scripts/seed-db.sh   # once each time a sandbox starts, before the terminals
    terminals:                    # kept up by the supervisor (`hangar-agent svc`)
      - {name: web, command: npm run dev, cwd: frontend, port: 3000}
    services:                     # images beside the sandbox, reachable at their name
      postgres: {image: postgres:17, port: 5432, env: {POSTGRES_PASSWORD: dev}}
    ports: [{port: 3000, name: web}]   # previews through hangar's authenticated proxy
    verify: uv run pytest -q
    verify_reports: [reports/*.xml, "**/coverage.xml"]   # test and coverage reports verify writes (testresults)
    build_secrets: [NPM_TOKEN]    # ids only: an admin maps them to credentials (environment or repo policy)
    lockfiles: [vendor/*.lock]    # more files whose change means a rebuild (the usual lockfiles are watched anyway)
    mocks:                        # stand-ins for real dependencies, recorded so nobody mistakes them (autoinstall)
      - {what: S3, how: "MinIO service at minio:9000", why: no cloud credentials in sandboxes}

It is untrusted: anyone who can push to the repo writes it. It is parsed strictly (unknown fields are refused, no
YAML aliases, size caps) and can set only the fields above. What an admin decides stays out of its reach: the image
and base, secrets and credentials, MCP servers, the runtime class, resources, the git credential mode. Secrets stay
in hangar, by name.

Resolution, per field: the repo's file, then the repo's policy (repopolicy.py), then the environment spec. The file
is read from the commit (`git cat-file`), never from the working tree, so a symlink can't make hangar read anything
else in the sandbox.

`parse`, `dump` and `check` are the API for writing and validating a config (agent-led setup builds on them).
"""

from __future__ import annotations

import hashlib
import json
import re
import shlex
from typing import TYPE_CHECKING, Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from hangar.envspec import (
    NAME_RE,
    EnvSpec,
    PreviewPort,
    Service,
    Terminal,
    check_programs,
    check_services,
)

if TYPE_CHECKING:
    from hangar.config import Settings

PATH = ".hangar/environment.yaml"
MAX_BYTES = 64 * 1024
MAX_LOCKFILES = 2000  # entries of `git ls-files -s` hashed into the config digest
# Lockfiles watched in every repo, at any depth: a change to one is a change to what `install` produces.
LOCKFILES = (
    "package-lock.json", "npm-shrinkwrap.json", "pnpm-lock.yaml", "yarn.lock", "bun.lockb", "bun.lock", "uv.lock",
    "poetry.lock", "Pipfile.lock", "pdm.lock", "requirements*.txt", "Cargo.lock", "go.sum", "Gemfile.lock",
    "composer.lock", "mix.lock", "pubspec.lock", "Podfile.lock", "flake.lock", ".tool-versions", ".mise.toml",
    "mise.toml", ".nvmrc", ".python-version",
)
# The fields of an environment the repo's file may set (they replace the environment's).
RUNTIME_FIELDS = ("install", "start", "terminals", "services", "ports", "verify")
_ADMIN_HINT = ("is decided in hangar, not by the repo: an admin sets it on the environment (`hangar env`) or on the "
               "repo's policy (`hangar repo set`)")
_SECRET_ID = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")


class RepoEnvError(ValueError):
    pass


class Mock(BaseModel):
    """--- autoinstall: a stand-in the environment uses for a real dependency (MinIO for S3, a local Postgres with a
    dev password, a stub file), recorded in the file and the setup PR so it is never mistaken for the real thing."""

    model_config = ConfigDict(extra="forbid")
    what: str = Field(min_length=1, max_length=200)
    how: str = Field(min_length=1, max_length=500)
    why: str | None = Field(None, max_length=500)


class RepoEnvironment(BaseModel):
    """The repo file's schema. Its models are the environment spec's own (envspec.Terminal, Service, PreviewPort)."""

    model_config = ConfigDict(extra="forbid")
    extends: str | None = Field(None, max_length=63)
    install: str | None = Field(None, max_length=4000)
    start: str | None = Field(None, max_length=4000)
    terminals: list[Terminal] | None = None
    services: dict[str, Service] | None = None
    ports: list[PreviewPort] | None = Field(None, max_length=16)
    verify: str | None = Field(None, max_length=2000)
    build_secrets: list[str] = Field(default_factory=list, max_length=32)
    lockfiles: list[str] = Field(default_factory=list, max_length=32)
    mocks: list[Mock] = Field(default_factory=list, max_length=32)  # --- autoinstall: documentation, never applied
    # --- testresults: JUnit, Cobertura or LCOV files `verify` writes, relative to the repo (`**/` for any depth)
    verify_reports: list[str] | None = None

    @field_validator("extends")
    @classmethod
    def _env_name(cls, v: str | None) -> str | None:
        if v is not None and not NAME_RE.match(v):
            raise ValueError(f"'{v[:40]}' is not an environment name")
        return v

    @field_validator("services")
    @classmethod
    def _services(cls, v: dict[str, Service] | None) -> dict[str, Service] | None:
        return check_services(v) if v is not None else v

    @field_validator("build_secrets")
    @classmethod
    def _ids(cls, v: list[str]) -> list[str]:
        for s in v:
            if not _SECRET_ID.match(s):
                raise ValueError(f"'{s[:40]}' is not a secret id (letters, digits and '_', like NPM_TOKEN)")
        return list(dict.fromkeys(v))

    @field_validator("verify_reports")
    @classmethod
    def _reports(cls, v: list[str] | None) -> list[str] | None:  # --- testresults
        from hangar.testresults import check_globs

        return check_globs(v)

    @field_validator("lockfiles")
    @classmethod
    def _paths(cls, v: list[str]) -> list[str]:
        for p in v:
            if not p or len(p) > 200 or p.startswith(("/", ":", "-")) or ".." in p.split("/") or "\0" in p:
                raise ValueError(f"lockfile '{p[:40]}' must be a path or glob relative to the repo, without '..'")
        return v

    @model_validator(mode="after")
    def _unique(self) -> RepoEnvironment:
        check_programs(self.terminals or [], self.services or {})
        return self


class _NoAliases(yaml.SafeLoader):
    """A loader that refuses YAML aliases: `*a` lets a 64 KiB file expand into gigabytes once validated."""

    def compose_node(self, parent, index):  # type: ignore[override]
        if self.check_event(yaml.AliasEvent):
            raise yaml.YAMLError("YAML aliases (*name) aren't allowed in .hangar/environment.yaml")
        return super().compose_node(parent, index)


def _where(e: ValidationError) -> str:
    out = []
    for err in e.errors()[:5]:
        loc = ".".join(str(x) for x in err["loc"]) or "(top)"
        msg = err["msg"].removeprefix("Value error, ")
        if err["type"] == "extra_forbidden":
            msg = "is not a field of .hangar/environment.yaml"
        out.append(f"{loc}: {msg}")
    return "; ".join(out)


def parse(text: str | bytes) -> RepoEnvironment:
    """The repo's file, validated, or RepoEnvError saying what's wrong (and where) in plain words."""
    raw = text.encode() if isinstance(text, str) else text
    if len(raw) > MAX_BYTES:
        raise RepoEnvError(f"{PATH} is over {MAX_BYTES // 1024} KiB")
    try:
        data = yaml.load(raw.decode("utf-8"), Loader=_NoAliases)  # noqa: S506 - a SafeLoader without aliases
    except (UnicodeDecodeError, yaml.YAMLError) as e:
        raise RepoEnvError(f"{PATH} is not valid YAML: {str(e).splitlines()[0][:200]}") from e
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise RepoEnvError(f"{PATH} must be a mapping of fields (install, start, terminals, ...)")
    admin = sorted(k for k in data if isinstance(k, str) and k in EnvSpec.model_fields
                   and k not in RepoEnvironment.model_fields)
    if admin:
        raise RepoEnvError(f"{', '.join(admin)} {_ADMIN_HINT}")
    try:
        return RepoEnvironment.model_validate(data)
    except ValidationError as e:
        raise RepoEnvError(f"{PATH}: {_where(e)}") from e


def dump(cfg: RepoEnvironment) -> str:
    """The config as YAML, fields that are unset left out: what agent-led setup writes to the repo."""
    return yaml.safe_dump(cfg.model_dump(mode="json", exclude_none=True, exclude_defaults=True), sort_keys=False)


def apply(spec: EnvSpec, cfg: RepoEnvironment | None) -> EnvSpec:
    """The environment with the repo file's fields in place of its own (each field the file sets replaces the
    environment's). The repo policy's `verify` sits between them; jobs resolve that (repopolicy.resolve)."""
    if cfg is None:
        return spec
    update = {f: getattr(cfg, f) for f in RUNTIME_FIELDS if getattr(cfg, f) is not None}
    if not update:
        return spec
    out = spec.model_copy(update=update)
    check_programs(out.terminals, out.services)
    return out


def digest(cfg: RepoEnvironment | None, lockfiles: str) -> str:
    """What a build depends on in the repo: the config's build fields and the lockfiles' blob ids (`git ls-files -s`).
    A comment, a new terminal or a changed verify doesn't change it, so it doesn't rebuild."""
    h = hashlib.sha256()
    if cfg is not None:
        h.update(json.dumps({"install": cfg.install, "build_secrets": cfg.build_secrets,
                             "lockfiles": cfg.lockfiles}, sort_keys=True).encode())
    h.update(b"\0")
    for line in sorted(lockfiles.splitlines()[:MAX_LOCKFILES]):
        h.update(line.strip().encode() + b"\n")
    return h.hexdigest()[:16]


def read_script(repo_path: str, cfg_lockfiles: list[str] | None = None) -> str:
    """One exec in the checkout: the commit, the file's bytes (from the commit, base64), and the lockfiles."""
    q = shlex.quote
    specs = [f":(glob)**/{n}" for n in LOCKFILES] + [f":(glob){p}" for p in cfg_lockfiles or []]
    return (f"cd {q(repo_path)} || exit 3\n"
            "echo \"commit $(git rev-parse HEAD 2>/dev/null)\"\n"
            f"if git cat-file -e HEAD:{PATH} 2>/dev/null; then\n"
            f"  echo \"config $(git cat-file -s HEAD:{PATH})\"\n"
            f"  git cat-file blob HEAD:{PATH} | head -c {MAX_BYTES + 1} | base64 | tr -d '\\n'; echo\n"
            "else echo 'config none'; echo; fi\n"
            f"git ls-files -s -- {' '.join(q(s) for s in specs)} | head -n {MAX_LOCKFILES}\n")


class Read(BaseModel):
    """What `read_script` found in a checkout."""

    commit: str | None = None
    present: bool = False
    config: RepoEnvironment | None = None
    error: str | None = None  # the file is there but invalid
    lockfiles: str = ""

    def digest(self) -> str:
        return digest(self.config, self.lockfiles)


def parse_read(stdout: str) -> Read:
    import base64
    import binascii

    lines = stdout.split("\n")
    out = Read()
    if len(lines) < 3 or not lines[0].startswith("commit ") or not lines[1].startswith("config "):
        raise RepoEnvError("could not read the checkout (no git repository?)")
    out.commit = lines[0][7:].strip() or None
    out.lockfiles = "\n".join(ln for ln in lines[3:] if ln.strip())
    if lines[1] == "config none":
        return out
    out.present = True
    try:
        size = int(lines[1].split()[1])
        if size > MAX_BYTES:
            raise RepoEnvError(f"{PATH} is over {MAX_BYTES // 1024} KiB")
        out.config = parse(base64.b64decode(lines[2], validate=True))
    except (RepoEnvError, ValueError, binascii.Error) as e:
        out.error = str(e) if isinstance(e, RepoEnvError) else f"could not read {PATH}: {e}"
    return out


# ====================================================================== checking a config against a hangar
def _glob(pattern: str) -> re.Pattern[str]:
    parts = re.split(r"(\*\*|\*)", pattern)
    return re.compile("".join(".*" if p == "**" else "[^/]*" if p == "*" else re.escape(p) for p in parts) + r"\Z")


def service_image_allowed(settings: Settings, image: str) -> bool:
    """HANGAR_ENV_SERVICE_IMAGES: the images services may use. `*` stays within one path segment, so the default
    `*` is Docker's official images (`postgres:17`, `redis@sha256:...`) and never `evil.example/x`. Never hangar's own
    (environments, and repo builds holding their repos' checkouts), whatever the patterns say."""
    own = [f"{p.rstrip('/')}/" for p in (settings.registry, settings.image_prefix) if p]
    if any(image.startswith(p) for p in own):
        return False
    return any(_glob(p).match(image) for p in settings.env_service_images)


def check(text: str | bytes, settings: Settings, catalog: Any, repo: str | None = None,
          allowed_envs: list[str] | None = None, environment: str | None = None,
          secret_ids: set[str] | None = None) -> dict[str, Any]:
    """Validate a config the way hangar will use it: the schema, then `extends` (it exists, and the repo's allowlist
    permits it), service images, and build secret ids an admin has mapped (`secret_ids`, when known).
    {"valid", "errors", "warnings", "config", "environment"}: nothing is saved or built."""
    try:
        cfg = parse(text)
    except RepoEnvError as e:
        return {"valid": False, "errors": [str(e)], "warnings": []}
    errors: list[str] = []
    warnings: list[str] = []
    env = environment or cfg.extends
    if cfg.extends:
        if cfg.extends not in catalog.names():
            errors.append(f"extends: no environment '{cfg.extends}' in this hangar")
        elif allowed_envs and cfg.extends not in allowed_envs:
            errors.append(f"extends: '{cfg.extends}' isn't allowed for {repo} (HANGAR_GITHUB_REPOS allows "
                          f"{', '.join(allowed_envs)})")
        elif repo and not allowed_envs:
            warnings.append(f"extends: ignored for {repo}, which has no HANGAR_GITHUB_REPOS entry (an admin says "
                            "which environments a repo may name)")
    spec = None
    if env and env in catalog.names():
        try:
            spec = apply(catalog.get(env), cfg)
        except (KeyError, ValueError) as e:
            errors.append(str(e))
    for name, svc in (cfg.services or {}).items():
        if not service_image_allowed(settings, svc.image):
            errors.append(f"services.{name}: image '{svc.image}' isn't allowed here (HANGAR_ENV_SERVICE_IMAGES)")
    if secret_ids is not None and (missing := [s for s in cfg.build_secrets if s not in secret_ids]):
        errors.append(f"build_secrets: {', '.join(missing)} not mapped to a credential; an admin maps them on the "
                      "environment or the repo policy (build_secrets)")
    if cfg.install is None and spec is not None and not spec.install:
        warnings.append("no install: sandboxes start from the environment's image, with nothing prebuilt")
    if any(t.port is None and t.ready is None for t in cfg.terminals or []):
        warnings.append("terminals without `port` or `ready` count as ready as soon as they start")
    return {"valid": not errors, "errors": errors, "warnings": warnings,
            "config": cfg.model_dump(mode="json", exclude_none=True), "environment": env}
