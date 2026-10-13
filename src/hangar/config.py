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
    # Built-in OAuth 2.1 server for MCP clients that need it (ChatGPT/claude.ai connectors).
    # public_url must be https (or localhost) for OAuth to be enabled.
    oauth_enabled: bool = True
    # Redirect hosts OAuth clients may register (suffix match). "*" allows any (not recommended).
    oauth_redirect_hosts: list[str] = Field(default_factory=lambda: [
        "chatgpt.com", "chat.openai.com", "openai.com", "claude.ai", "claude.com", "localhost", "127.0.0.1"])
    oauth_max_clients: int = 200
    oauth_grant_days: int = 90  # absolute lifetime of an approval, refreshes included

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
    # Mount harness packs with `--mount type=image` (Docker 28+ with the containerd image store). None = detect.
    docker_image_volumes: bool | None = None

    # kubernetes / agent-sandbox
    k8s_namespace: str = "hangar-sandboxes"
    k8s_in_cluster: bool = False
    k8s_runtime_class: str | None = "gvisor"  # gvisor | kata-qemu-runtime-rs | None (runc)
    k8s_gke_node_selector: bool = True  # add sandbox.gke.io/runtime selector+toleration for gvisor
    k8s_pod_snapshots: bool = False  # GKE Pod snapshots (podsnapshot.gke.io): full memory+fs forks
    k8s_snapshot_service_account: str = "hangar-sandbox"  # KSA with GCS access for the snapshot bucket
    k8s_snapshot_timeout_seconds: int = 600
    # Service account Cloud Build runs as for in-cluster builds (the control plane's own GSA). Without it
    # Cloud Build uses the project's default SA, which the control plane usually may not act as.
    k8s_build_service_account: str | None = None
    # Mount harness packs as OCI image volumes (Kubernetes >= 1.35, stable in 1.36). Off: bake into images.
    k8s_image_volumes: bool = True

    # e2b
    e2b_api_key: str | None = None
    e2b_domain: str | None = None
    # How E2B pulls environment images from HANGAR_REGISTRY (private registries need one of these):
    e2b_gcp_service_account_json: str | None = None  # path or JSON; Artifact Registry / GCR reader key
    e2b_aws_access_key_id: str | None = None  # ECR
    e2b_aws_secret_access_key: str | None = None
    e2b_aws_region: str | None = None
    e2b_registry_username: str | None = None  # any other registry (basic auth)
    e2b_registry_password: str | None = None

    # --- fleet defaults -----------------------------------------------------
    default_ttl_minutes: int = 240
    max_sandboxes: int = 50
    reaper_interval_seconds: int = 60
    sandbox_user: str = "agent"
    workspace_dir: str = "/workspace"

    # --- jobs (spec 0009) ---------------------------------------------------
    job_concurrency: int = 4  # jobs provisioning/running at once; the rest wait in the queue
    job_poll_seconds: float = 5.0
    job_timeout_minutes: int = 60  # per agent turn
    job_keep_minutes: int = 120  # sandbox kept after a turn for follow-ups and take-over
    job_verify_timeout_seconds: int = 1800
    job_max_context_bytes: int = 2_000_000
    job_max_context_items: int = 50
    # Hosts allowed for plain-http callbacks or private-address callbacks (https to public hosts always allowed).
    job_callback_hosts: list[str] = Field(default_factory=list)

    # --- GitHub (jobs delivery and trigger) ---------------------------------
    github_api_url: str = "https://api.github.com"
    # Vault credential holding a GitHub token the *control plane* uses to open PRs and comment.
    github_token_credential: str | None = None
    # Vault credential holding the webhook secret; the webhook route is off until this is set.
    github_webhook_secret_credential: str | None = None
    # Repos that may trigger jobs, mapped to the environment to run them in: {"owner/repo": "env"}.
    github_repos: dict[str, str] = Field(default_factory=dict)
    github_trigger_label: str = "hangar"

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
