"""Runtime settings. Every field can be set with a HANGAR_* environment variable."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="HANGAR_", env_file=".env", extra="ignore")

    # --- server -------------------------------------------------------------
    host: str = "127.0.0.1"
    port: int = 8787
    public_url: str = "http://localhost:8787"
    # Bootstrap admin token. If unset on first start, one is generated and printed once.
    admin_token: str | None = None

    # --- state --------------------------------------------------------------
    data_dir: Path = Field(default_factory=lambda: Path.home() / ".hangar")
    # Fernet key for the credential vault. If unset, a key file is created in data_dir.
    secret_key: str | None = None

    # --- environments -------------------------------------------------------
    env_dirs: list[Path] = Field(default_factory=lambda: [Path("envs")])
    image_prefix: str = "hangar-env"
    # Registry to push template images to (required for kubernetes provider), e.g.
    # us-central1-docker.pkg.dev/my-proj/hangar
    registry: str | None = None
    # PEM bundle trusted inside every image (corporate TLS-inspecting proxies, private CAs).
    extra_ca_bundle: Path | None = None

    # --- providers ----------------------------------------------------------
    default_provider: Literal["docker", "kubernetes", "e2b"] = "docker"
    enabled_providers: list[str] = Field(default_factory=lambda: ["docker"])

    # docker
    docker_runtime: str | None = None  # e.g. "runsc" for gVisor, "kata" if configured
    docker_network: str | None = None

    # kubernetes / agent-sandbox
    k8s_namespace: str = "hangar-sandboxes"
    k8s_in_cluster: bool = False
    k8s_runtime_class: str | None = "gvisor"  # gvisor | kata-qemu | None (runc)
    k8s_gke_node_selector: bool = True  # add sandbox.gke.io/runtime selector+toleration for gvisor
    k8s_pod_snapshots: bool = False  # GKE Pod snapshots (podsnapshot.gke.io) available

    # e2b
    e2b_api_key: str | None = None
    e2b_domain: str | None = None

    # --- fleet defaults -----------------------------------------------------
    default_ttl_minutes: int = 240
    max_sandboxes: int = 50
    reaper_interval_seconds: int = 60
    sandbox_user: str = "agent"
    workspace_dir: str = "/workspace"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "hangar.db"

    @property
    def snapshot_dir(self) -> Path:
        return self.data_dir / "snapshots"


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    s.data_dir.mkdir(parents=True, exist_ok=True)
    s.snapshot_dir.mkdir(parents=True, exist_ok=True)
    return s
