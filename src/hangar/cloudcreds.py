"""Short-lived cloud credentials (--- egress): an OIDC token per sandbox instead of stored cloud keys.

A long-lived GCP service-account key or AWS access key stored as a secret is readable by the agent and outlives
the sandbox. Instead hangar is an OIDC issuer. For an environment with `cloud:` it mints each sandbox an ID token
(RS256), scoped to the cloud's audience, valid for `lifetime_minutes` (60), and refreshed in place while the
sandbox runs. The cloud exchanges it for its own short-lived credentials:

* **GCP** (Workload Identity Federation, docs.cloud.google.com/iam/docs/workload-identity-federation-with-other-
  providers): an OIDC provider in a workload identity pool trusts hangar's issuer. Hangar's JWKS can be uploaded
  with `--jwk-json-path` (`GET /oidc/jwks.json`), so the issuer doesn't have to be reachable from Google. The sandbox
  gets an `external_account` credential configuration whose `credential_source` is the token file;
  `GOOGLE_APPLICATION_CREDENTIALS` and `CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE` point client libraries and gcloud
  at it. google-auth reads the file again on every refresh, so a token hangar replaces is picked up. STS exchanges
  it, optionally impersonating a service account.
* **AWS** (`AssumeRoleWithWebIdentity`): an IAM OIDC identity provider trusts the issuer. AWS fetches the
  discovery document and JWKS over HTTPS itself, so the issuer URL must be reachable from AWS. The SDKs' standard
  `AWS_ROLE_ARN` / `AWS_WEB_IDENTITY_TOKEN_FILE` / `AWS_ROLE_SESSION_NAME` variables do the exchange and re-read the
  file.

Claims: `sub` is `hangar:env:<environment>` (a stable subject to grant on), plus `environment`, `repo`, `person`,
`sandbox`, `session` and `job` for attribute conditions. A token is readable by the agent: it is a credential, but
one that dies within the hour, works only for the audience it names, only grants what its subject is granted, and
stops being renewed when the sandbox stops.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import re
import secrets
import shlex
import time
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from hangar.models import Sandbox, SandboxState

if TYPE_CHECKING:
    from hangar.envspec import EnvSpec
    from hangar.fleet import Fleet
    from hangar.providers.base import Provider

log = logging.getLogger("hangar.cloud")

VAULT_KEY = "oidc:signing-keys"
MAX_KEYS = 4  # published at once (GCP takes up to 8 uploaded keys)
REFRESH_EVERY = 300.0  # seconds between passes of the refresh loop
TOKEN_DIR = ".hangar/cloud"
_POOL = re.compile(r"^(?://iam\.googleapis\.com/)?(projects/\d+/locations/global/workloadIdentityPools/[a-z0-9-]{4,32}"
                   r"/providers/[a-z0-9-]{4,32})$")
_SA = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]@[a-z0-9-]+\.iam\.gserviceaccount\.com$")
_ROLE = re.compile(r"^arn:aws[a-z-]*:iam::\d{12}:role/[\w+=,.@/-]{1,512}$")


class GcpCloud(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # The workload identity pool provider: projects/NUMBER/locations/global/workloadIdentityPools/POOL/providers/ID
    provider: str
    service_account: str | None = None  # impersonate it; unset: grant roles to the federated principal directly
    audience: str | None = None  # the token's aud; default the provider's default, https://iam.googleapis.com/<provider>

    @field_validator("provider")
    @classmethod
    def _provider(cls, v: str) -> str:
        m = _POOL.match(v.strip())
        if not m:
            raise ValueError("provider must be projects/<number>/locations/global/workloadIdentityPools/<pool>/"
                             "providers/<id> (the project number, not its id)")
        return m.group(1)

    @field_validator("service_account")
    @classmethod
    def _sa(cls, v: str | None) -> str | None:
        if v is not None and not _SA.match(v):
            raise ValueError(f"'{v}' is not a service account email")
        return v

    @property
    def token_audience(self) -> str:
        return self.audience or f"https://iam.googleapis.com/{self.provider}"


class AwsCloud(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role_arn: str
    audience: str = "sts.amazonaws.com"  # the client id registered on the IAM OIDC provider
    region: str | None = None  # also allow sts.<region>.amazonaws.com through egress

    @field_validator("role_arn")
    @classmethod
    def _role(cls, v: str) -> str:
        if not _ROLE.match(v):
            raise ValueError(f"'{v}' is not an IAM role ARN")
        return v


class CloudSpec(BaseModel):
    """`cloud:` on an environment (admin): which clouds its sandboxes get short-lived credentials for."""

    model_config = ConfigDict(extra="forbid")
    gcp: GcpCloud | None = None
    aws: AwsCloud | None = None
    lifetime_minutes: int = Field(60, ge=5, le=720)


class CloudError(RuntimeError):
    pass


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def egress_hosts(cloud: CloudSpec | None) -> list[str]:
    """What a proxied sandbox must reach to exchange its token."""
    if cloud is None:
        return []
    out = []
    if cloud.gcp:
        out += ["sts.googleapis.com", "iamcredentials.googleapis.com"]
    if cloud.aws:
        out += ["sts.amazonaws.com"] + ([f"sts.{cloud.aws.region}.amazonaws.com"] if cloud.aws.region else [])
    return out


# ============================================================================ the issuer
class Issuer:
    """Hangar's OIDC issuer: RSA keys in the vault, the discovery document, the JWKS, and token minting."""

    def __init__(self, fleet: Fleet):
        self.fleet = fleet
        self.settings = fleet.settings
        self._keys: list[dict[str, Any]] | None = None
        self._task: asyncio.Task | None = None

    @property
    def url(self) -> str:
        return (self.settings.oidc_issuer or self.settings.public_url).rstrip("/")

    # ------------------------------------------------------------------ keys
    def keys(self) -> list[dict[str, Any]]:
        """[{kid, pem, created}], newest first; one is made the first time it's needed."""
        if self._keys is None:
            raw = self.fleet.broker.vault.get(VAULT_KEY)
            self._keys = json.loads(raw) if raw else []
            if not self._keys:
                self._keys = [self._new_key()]
                self._save()
        return self._keys

    def rotate(self) -> str:
        """A new signing key; the older ones stay published (up to MAX_KEYS) so tokens they signed still verify.
        Returns the new kid. Re-upload the JWKS where the cloud doesn't fetch it (GCP with --jwk-json-path)."""
        self._keys = [self._new_key(), *self.keys()][:MAX_KEYS]
        self._save()
        return self._keys[0]["kid"]

    @staticmethod
    def _new_key() -> dict[str, Any]:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption()).decode()
        return {"kid": secrets.token_hex(8), "pem": pem, "created": time.time()}

    def _save(self) -> None:
        self.fleet.broker.vault.put(VAULT_KEY, json.dumps(self._keys))

    def jwks(self) -> dict[str, Any]:
        """The public keys (never a private part)."""
        from cryptography.hazmat.primitives.serialization import load_pem_private_key

        out = []
        for k in self.keys():
            pub = load_pem_private_key(k["pem"].encode(), password=None).public_key().public_numbers()  # type: ignore[union-attr]
            out.append({"kty": "RSA", "use": "sig", "alg": "RS256", "kid": k["kid"],
                        "n": _b64url(pub.n.to_bytes((pub.n.bit_length() + 7) // 8, "big")),
                        "e": _b64url(pub.e.to_bytes((pub.e.bit_length() + 7) // 8, "big"))})
        return {"keys": out}

    def discovery(self) -> dict[str, Any]:
        """/.well-known/openid-configuration: just what a relying party needs to verify hangar's ID tokens."""
        return {"issuer": self.url, "jwks_uri": f"{self.url}/oidc/jwks.json", "response_types_supported": ["id_token"],
                "subject_types_supported": ["public"], "id_token_signing_alg_values_supported": ["RS256"],
                "claims_supported": ["sub", "aud", "exp", "iat", "iss", "jti", "environment", "repo", "person",
                                     "sandbox", "session", "job"]}

    # ------------------------------------------------------------------ tokens
    def mint(self, sb: Sandbox, audience: str, lifetime_minutes: int, now_: float | None = None) -> tuple[str, float]:
        """An RS256 ID token for a sandbox and one audience. Returns (token, expiry)."""
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding
        from cryptography.hazmat.primitives.serialization import load_pem_private_key

        from hangar.repopolicy import repo_key

        k = self.keys()[0]
        t = int(now_ if now_ is not None else time.time())
        exp = t + lifetime_minutes * 60
        claims = {"iss": self.url, "sub": f"hangar:env:{sb.environment}", "aud": audience, "iat": t - 30,
                  "nbf": t - 30, "exp": exp, "jti": secrets.token_hex(12), "environment": sb.environment,
                  "repo": repo_key(sb.repo) or "", "person": sb.person or "", "sandbox": sb.id,
                  "session": sb.labels.get("hangar.session", ""), "job": sb.labels.get("hangar.job", "")}
        head = _b64url(json.dumps({"alg": "RS256", "typ": "JWT", "kid": k["kid"]}, separators=(",", ":")).encode())
        body = _b64url(json.dumps(claims, separators=(",", ":")).encode())
        key = load_pem_private_key(k["pem"].encode(), password=None)
        sig = key.sign(f"{head}.{body}".encode(), padding.PKCS1v15(), hashes.SHA256())  # type: ignore[union-attr]
        return f"{head}.{body}.{_b64url(sig)}", float(exp)

    # ------------------------------------------------------------------ sandboxes
    def _home(self) -> str:
        return f"/home/{self.settings.sandbox_user}"

    def _paths(self) -> tuple[str, str, str]:
        d = f"{self._home()}/{TOKEN_DIR}"
        return d, f"{d}/gcp-token", f"{d}/aws-token"

    async def attach(self, prov: Provider, sb: Sandbox, spec: EnvSpec) -> dict[str, str]:
        """Write a new sandbox's tokens and (GCP) credential configuration; return the env vars that point the
        cloud SDKs at them. Nothing for an environment without `cloud:`."""
        cloud: CloudSpec | None = getattr(spec, "cloud", None)
        if cloud is None or not (cloud.gcp or cloud.aws):
            return {}
        if not self.settings.oidc_issuer and not self.settings.public_url.startswith("https://"):
            raise CloudError("cloud: needs HANGAR_OIDC_ISSUER (an https URL) or an https HANGAR_PUBLIC_URL to issue "
                             "tokens from (docs/security.md#short-lived-cloud-credentials)")
        d, gcp_tok, aws_tok = self._paths()
        env: dict[str, str] = {}
        exps = await self._write_tokens(prov, sb, cloud)
        if cloud.gcp:
            cfg = gcp_config(cloud.gcp, gcp_tok)
            await prov.write_file(sb, f"{d}/gcp.json", json.dumps(cfg, indent=2).encode(), mode=0o600)
            env["GOOGLE_APPLICATION_CREDENTIALS"] = env["CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE"] = f"{d}/gcp.json"
        if cloud.aws:
            env |= {"AWS_ROLE_ARN": cloud.aws.role_arn, "AWS_WEB_IDENTITY_TOKEN_FILE": aws_tok,
                    "AWS_ROLE_SESSION_NAME": f"hangar-{sb.id}"[:64]}
            if cloud.aws.region:
                env["AWS_REGION"] = cloud.aws.region
        sb.info["cloud"] = {"gcp": cloud.gcp.provider if cloud.gcp else None,
                            "aws": cloud.aws.role_arn if cloud.aws else None, "expires_at": min(exps),
                            "lifetime_minutes": cloud.lifetime_minutes}
        self.fleet.audit(sb.created_by or "system", "cloud.token", sb.id, environment=sb.environment,
                         person=sb.person, gcp=sb.info["cloud"]["gcp"], aws=sb.info["cloud"]["aws"],
                         lifetime_minutes=cloud.lifetime_minutes)
        return env

    async def _write_tokens(self, prov: Provider, sb: Sandbox, cloud: CloudSpec) -> list[float]:
        d, gcp_tok, aws_tok = self._paths()
        exps = []
        for want, aud, path in ((cloud.gcp, cloud.gcp.token_audience if cloud.gcp else "", gcp_tok),
                                (cloud.aws, cloud.aws.audience if cloud.aws else "", aws_tok)):
            if want is None:
                continue
            tok, exp = self.mint(sb, aud, cloud.lifetime_minutes)
            # write then rename, so an SDK reading the file mid-refresh never sees half a token
            await prov.write_file(sb, f"{path}.new", tok.encode(), mode=0o600)
            res = await prov.exec(sb, f"mv -f {shlex.quote(path + '.new')} {shlex.quote(path)}", timeout=30)
            if res.exit_code != 0:
                raise CloudError(f"could not write the cloud token: {(res.stderr or res.stdout)[-200:]}")
            exps.append(exp)
        return exps

    async def refresh(self) -> int:
        """Renew tokens of running sandboxes past half their lifetime. Returns how many were renewed."""
        n = 0
        for sb in self.fleet.store.list_where(Sandbox, state=SandboxState.RUNNING):
            info = sb.info.get("cloud") or {}
            if not info or info.get("expires_at", 0) - time.time() > info.get("lifetime_minutes", 60) * 30:
                continue
            try:
                spec = self.fleet.catalog.get(sb.environment)
                cloud: CloudSpec | None = getattr(spec, "cloud", None)
                if cloud is None:
                    continue
                _, prov = self.fleet._running(sb.id)
                exps = await self._write_tokens(prov, sb, cloud)
            except Exception as e:  # noqa: BLE001 - one sandbox must not stop the others' renewal
                log.warning("cloud token for %s not renewed: %s", sb.id, e)
                continue
            cur = self.fleet.store.get(Sandbox, sb.id)
            if cur is not None and cur.state == SandboxState.RUNNING:
                cur.info.setdefault("cloud", {})["expires_at"] = min(exps)
                self.fleet.store.put(cur)
            n += 1
        return n

    async def _loop(self) -> None:
        from hangar import metrics

        while True:
            await asyncio.sleep(REFRESH_EVERY)
            try:
                await self.refresh()
                metrics.loop_tick("cloud-tokens")
            except Exception:  # noqa: BLE001
                log.exception("cloud token refresh failed")
                metrics.loop_failed("cloud-tokens")

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(BaseException):
                await self._task
            self._task = None


def gcp_config(gcp: GcpCloud, token_file: str) -> dict[str, Any]:
    """The `external_account` credential configuration `gcloud iam workload-identity-pools create-cred-config
    --credential-source-file=… --credential-source-type=text` writes (google-auth identity_pool reads the file
    on every refresh)."""
    cfg: dict[str, Any] = {
        "universe_domain": "googleapis.com",
        "type": "external_account",
        "audience": f"//iam.googleapis.com/{gcp.provider}",
        "subject_token_type": "urn:ietf:params:oauth:token-type:jwt",
        "token_url": "https://sts.googleapis.com/v1/token",
        "credential_source": {"file": token_file, "format": {"type": "text"}},
    }
    if gcp.service_account:
        cfg["service_account_impersonation_url"] = ("https://iamcredentials.googleapis.com/v1/projects/-/"
                                                    f"serviceAccounts/{gcp.service_account}:generateAccessToken")
    return cfg


def mount(app: Any, h: Any, admin: Any) -> None:
    """Public: GET /.well-known/openid-configuration and GET /oidc/jwks.json (no token: relying parties fetch them).
    Admin: POST /api/cloud/rotate."""
    issuer: Issuer = h.fleet.cloud

    @app.get("/.well-known/openid-configuration")
    async def oidc_discovery():
        return issuer.discovery()

    @app.get("/oidc/jwks.json")
    async def oidc_jwks():
        return issuer.jwks()

    @app.post("/api/cloud/rotate")
    async def oidc_rotate(who: str = admin):
        kid = issuer.rotate()
        h.fleet.audit(who, "cloud.rotate", kid)
        return {"kid": kid, "jwks": issuer.jwks()}
