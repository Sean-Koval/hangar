"""Core data models shared by the fleet manager, MCP tools, REST API and providers."""

from __future__ import annotations

import secrets
import time
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


def new_id(prefix: str) -> str:
    return f"{prefix}-{secrets.token_hex(4)}"


def now() -> float:
    return time.time()


class SandboxState(StrEnum):
    PENDING = "pending"
    STARTING = "starting"
    RUNNING = "running"
    PAUSED = "paused"
    STOPPING = "stopping"
    STOPPED = "stopped"
    ERROR = "error"


class Resources(BaseModel):
    cpus: float = 2.0
    memory_gb: float = 4.0
    disk_gb: float | None = None


class Sandbox(BaseModel):
    id: str = Field(default_factory=lambda: new_id("sb"))
    name: str
    environment: str
    provider: str
    state: SandboxState = SandboxState.PENDING
    provider_ref: str | None = None  # container id / claim name / e2b sandbox id
    image: str | None = None
    repo: str | None = None
    branch: str | None = None
    credentials: list[str] = Field(default_factory=list)
    labels: dict[str, str] = Field(default_factory=dict)
    resources: Resources = Field(default_factory=Resources)
    parent: str | None = None  # sandbox or snapshot it was forked from
    created_at: float = Field(default_factory=now)
    expires_at: float | None = None
    last_activity: float = Field(default_factory=now)
    error: str | None = None
    created_by: str | None = None
    info: dict[str, Any] = Field(default_factory=dict)


class SnapshotKind(StrEnum):
    FULL = "full"  # provider-native: docker commit, GKE pod snapshot, E2B snapshot
    WORKSPACE = "workspace"  # portable tarball of the workspace dir


class Snapshot(BaseModel):
    id: str = Field(default_factory=lambda: new_id("snap"))
    sandbox_id: str
    environment: str
    provider: str
    kind: SnapshotKind
    label: str | None = None
    ref: str  # image tag / tarball path / provider snapshot id
    created_at: float = Field(default_factory=now)
    size_bytes: int | None = None


class ExecResult(BaseModel):
    exit_code: int
    stdout: str
    stderr: str = ""
    duration_ms: int = 0
    truncated: bool = False


class CredentialKind(StrEnum):
    API_KEY = "api_key"
    OAUTH_TOKEN = "oauth_token"  # e.g. CLAUDE_CODE_OAUTH_TOKEN from `claude setup-token`
    AUTH_FILE = "auth_file"  # e.g. ~/.codex/auth.json (rotating refresh token -> leased)
    SSH_KEY = "ssh_key"
    GIT_TOKEN = "git_token"
    GENERIC = "generic"


class Credential(BaseModel):
    """Metadata only. Secret material lives encrypted in the vault, never in this model."""

    name: str
    harness: str  # claude-code | codex | antigravity | gemini | git | custom
    kind: CredentialKind
    description: str | None = None
    exclusive: bool = False  # only one sandbox may hold it at a time (rotating refresh tokens)
    leased_to: str | None = None
    created_at: float = Field(default_factory=now)
    updated_at: float = Field(default_factory=now)
    # How to materialize inside a sandbox; filled from the harness profile when omitted.
    env_var: str | None = None
    file_path: str | None = None


class ApiToken(BaseModel):
    id: str = Field(default_factory=lambda: new_id("tok"))
    name: str
    token_hash: str
    scopes: list[str] = Field(default_factory=lambda: ["admin"])
    created_at: float = Field(default_factory=now)
    last_used: float | None = None


class AuditEvent(BaseModel):
    ts: float = Field(default_factory=now)
    actor: str
    action: str
    target: str | None = None
    detail: dict[str, Any] = Field(default_factory=dict)
