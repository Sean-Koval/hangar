"""`hangar doctor`: check the things that break a deployment, with the fix for each.

Every check here comes from a real failure during setup: Cloud Build lacking actAs on its service account,
a GitHub token that can't see the repo, a webhook secret that isn't stored, an environment mapped for jobs that
doesn't exist or isn't built, checkpoint storage that isn't writable, sandboxes that can't start for quota.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from hangar.github import GitHub, parse_repo
from hangar.models import Credential, Sandbox, SandboxState, now

METADATA = "http://metadata.google.internal/computeMetadata/v1"


def _check(name: str, ok: bool | None, detail: str, fix: str | None = None) -> dict[str, Any]:
    """ok: True = pass, False = fail, None = warning / not applicable."""
    return {"name": name, "status": "ok" if ok else ("fail" if ok is False else "warn"), "detail": detail,
            "fix": None if ok else fix}


async def _metadata_token(transport=None) -> str | None:
    try:
        async with httpx.AsyncClient(transport=transport, timeout=3) as c:
            r = await c.get(f"{METADATA}/instance/service-accounts/default/token",
                            headers={"Metadata-Flavor": "Google"})
            return r.json()["access_token"] if r.status_code == 200 else None
    except (httpx.HTTPError, ValueError, KeyError):
        return None


async def run(h, transport: httpx.AsyncBaseTransport | None = None) -> list[dict[str, Any]]:
    """`h` is the app's Hangar (fleet, broker, sessions, jobs, settings)."""
    s, fleet, out = h.settings, h.fleet, []

    # providers
    ov = fleet.overview()
    for name, p in ov["providers"].items():
        out.append(_check(f"provider {name}", p["ok"], "reachable" if p["ok"] else p.get("error", "unavailable"),
                          "check the provider's credentials / cluster access (docs/gke.md)"))

    # Cloud Build can run as the build service account (actAs on itself)
    sa = getattr(s, "k8s_build_service_account", None)
    if "kubernetes" in fleet.providers and sa:
        token = await _metadata_token(transport)
        if token is None:
            out.append(_check("cloud build permissions", None, "not running on GCP; skipped"))
        else:
            try:
                async with httpx.AsyncClient(transport=transport, timeout=10) as c:
                    r = await c.post(f"https://iam.googleapis.com/v1/projects/-/serviceAccounts/{sa}:testIamPermissions",
                                     json={"permissions": ["iam.serviceAccounts.actAs"]},
                                     headers={"Authorization": f"Bearer {token}"})
                granted = "iam.serviceAccounts.actAs" in (r.json().get("permissions") or [])
                out.append(_check(
                    "cloud build permissions", granted,
                    f"control plane {'can' if granted else 'can NOT'} run Cloud Build as {sa}",
                    f"gcloud iam service-accounts add-iam-policy-binding {sa} --member=serviceAccount:{sa} "
                    "--role=roles/iam.serviceAccountUser   (or terraform apply in deploy/terraform/gke)"))
            except (httpx.HTTPError, ValueError) as e:
                out.append(_check("cloud build permissions", None, f"could not check: {e}"))

    # sandboxes that failed to start recently (quota, scheduling, image)
    recent = [x for x in h.store.list(Sandbox) if x.state == SandboxState.ERROR and x.created_at > now() - 86400]
    if recent:
        last = max(recent, key=lambda x: x.created_at)
        quota = any("quota" in (x.error or "").lower() for x in recent)
        out.append(_check("recent sandbox starts", False if quota else None,
                          f"{len(recent)} failed in the last 24h; latest: {(last.error or '')[:300]}",
                          "raise the GCE quota named above, or use smaller sandbox node disks "
                          "(terraform sandbox_disk_gb)" if quota else "see the error above"))
    else:
        out.append(_check("recent sandbox starts", True, "no failed starts in the last 24h"))

    # jobs: GitHub token, webhook secret, mapped repos -> environments
    if s.github_token_credential:
        try:
            token = h.broker.secret_text(s.github_token_credential)
        except Exception as e:  # noqa: BLE001
            out.append(_check("github token", False, f"credential '{s.github_token_credential}' unusable: {e}",
                              f"hangar cred add {s.github_token_credential} --harness webhook --kind generic "
                              "--file <token file>"))
            token = None
        if token:
            gh = GitHub(token, s.github_api_url, transport=transport)
            try:
                for repo in s.github_repos:
                    full = parse_repo(f"https://github.com/{repo}") or repo
                    try:
                        await gh.default_branch(full)
                        out.append(_check(f"github access {full}", True, "token can read the repo"))
                    except Exception as e:  # noqa: BLE001
                        out.append(_check(f"github access {full}", False, f"token can't read the repo: {e}",
                                          "give the fine-grained token access to this repo with Issues and "
                                          "Pull requests read & write"))
            finally:
                await gh.close()
    elif s.github_repos:
        out.append(_check("github token", False, "HANGAR_GITHUB_REPOS is set but HANGAR_GITHUB_TOKEN_CREDENTIAL "
                          "isn't, so hangar can't open PRs or comment", "set it in the hangar-site config map"))
    if s.github_webhook_secret_credential:
        ok = h.store.get(Credential, s.github_webhook_secret_credential) is not None
        out.append(_check("github webhook secret", ok, "stored" if ok else
                          f"credential '{s.github_webhook_secret_credential}' is missing",
                          f"hangar cred add {s.github_webhook_secret_credential} --harness webhook --kind generic "
                          "--file ~/.hangar/gh-webhook-secret"))
    names = set(fleet.catalog.names())
    creds = {c.name for c in h.store.list(Credential)}
    for repo, envs in s.github_repos.items():
        for env in [e.strip() for e in envs.split(",") if e.strip()]:
            if env not in names:
                out.append(_check(f"environment {env}", False, f"mapped for {repo} but doesn't exist",
                                  "add envs/<name>.yaml or create it with clone_environment"))
                continue
            spec = fleet.catalog.get(env)
            missing = [c for c in spec.credentials if c not in creds]
            built = any(h.store.kv_get(f"image:{p}:{env}") for p in fleet.providers) or bool(spec.image)
            if missing:
                out.append(_check(f"environment {env}", False, f"credentials not in the vault: {', '.join(missing)}",
                                  "hangar cred add <name> ... for each, or drop them from the environment"))
            elif not spec.credentials:
                out.append(_check(f"environment {env}", False, "no credentials: agents can't call a model or push",
                                  "list model and git credentials under `credentials:` in the environment"))
            else:
                out.append(_check(f"environment {env}", True if built else None,
                                  "ready" if built else "not built yet: the first job will wait for a build",
                                  f"hangar env build {env}  (or leave HANGAR_PREBUILD_ENVIRONMENTS unset to "
                                  "build mapped environments at startup)"))

    # checkpoint storage is writable
    key = f"_doctor/{now():.0f}"
    try:
        await h.sessions.blobs.put(key, b"ok")
        ok = await h.sessions.blobs.get(key) == b"ok"
        await h.sessions.blobs.delete_prefix("_doctor/")
        out.append(_check("session checkpoint storage", ok, h.sessions.blobs.description,
                          "check HANGAR_SESSION_STORE and the bucket's IAM (terraform output session_bucket)"))
    except Exception as e:  # noqa: BLE001
        out.append(_check("session checkpoint storage", False, f"{h.sessions.blobs.description}: {e}",
                          "check HANGAR_SESSION_STORE and the bucket's IAM (roles/storage.objectAdmin for the "
                          "hangar service account)"))
    if not (s.session_store or "").startswith("gs://") and "kubernetes" in fleet.providers:
        out.append(_check("durable checkpoints", None, "checkpoints are on the control plane's disk",
                          "set HANGAR_SESSION_STORE=gs://<bucket> (terraform output session_bucket) in hangar-site"))
    # git gateway (workstream F): reachable, and every GitHub App credential can mint a token
    out += await fleet.gateway.doctor(transport)
    await asyncio.sleep(0)
    return out
