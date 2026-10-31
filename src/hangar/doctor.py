"""`hangar doctor`: check the things that break a deployment, with the fix for each.

Every check here comes from a real failure during setup: Cloud Build lacking actAs on its service account,
a GitHub token that can't see the repo, a webhook secret that isn't stored, an environment mapped for jobs that
doesn't exist or isn't built, checkpoint storage that isn't writable, sandboxes that can't start for quota.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx

from hangar.github import GitHub, parse_repo
from hangar.models import Credential, Sandbox, SandboxState, now

METADATA = "http://metadata.google.internal/computeMetadata/v1"


def _check(name: str, ok: bool | None, detail: str, fix: str | None = None,
           action: dict[str, Any] | None = None) -> dict[str, Any]:
    """ok: True = pass, False = fail, None = warning / not applicable.

    `action` (workstream setup) is a fix `hangar doctor --fix` can apply on its own: {"id": ..., ...}."""
    out = {"name": name, "status": "ok" if ok else ("fail" if ok is False else "warn"), "detail": detail,
           "fix": None if ok else fix}
    if action and not ok:
        out["action"] = action
    return out


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
    # --- isolation: Docker sandboxes can't reach each other's ports
    if (dp := fleet.providers.get("docker")) is not None and hasattr(dp, "client"):
        from hangar.providers.docker_net import doctor_check

        try:
            out.append(await asyncio.to_thread(doctor_check, dp.client, s))
        except Exception as e:  # noqa: BLE001 - a check, not a failure of the run
            out.append(_check("docker sandbox network", None, f"could not check: {e}"))

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
    recent = [x for x in h.store.list_where(Sandbox, state=SandboxState.ERROR)  # --- scale: indexed
              if x.created_at > now() - 86400]
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
                          "isn't, so hangar can't open PRs or comment",
                          "hangar setup github   (stores the token and the setting; or set it in hangar-site)"))
    if s.github_webhook_secret_credential:
        ok = h.store.get(Credential, s.github_webhook_secret_credential) is not None
        out.append(_check("github webhook secret", ok, "stored" if ok else
                          f"credential '{s.github_webhook_secret_credential}' is missing",
                          "hangar setup github   (generates and stores the secret, and sets it on the webhook)"))
    names = set(fleet.catalog.names())
    creds = {c.name for c in h.store.list(Credential)}
    for repo, envs in s.github_repos.items():
        # --- frontdoor: GitHub, Slack and webhook jobs take their check from the repo's policy or its environment
        policy = h.jobs.policies.get(repo)
        first = policy.environment if policy and policy.environment else next(
            (e.strip() for e in envs.split(",") if e.strip()), None)
        try:
            env_verify = fleet.catalog.get(first).verify if first in names else None
        except ValueError:  # an invalid spec: the environment check below reports it
            env_verify = None
        has =policy.verify if policy is not None and policy.verify is not None else env_verify
        out.append(_check(f"verify {repo}", True if has else None,
                          f"jobs run `{has}`" if has else "no check: jobs from GitHub, Slack and webhooks deliver PRs "
                          "nothing has verified",
                          f"hangar repo set {repo} --verify '<the repo's test command>'  (or `verify:` in the "
                          "environment)"))
        for env in [e.strip() for e in envs.split(",") if e.strip()]:
            if env not in names:
                out.append(_check(f"environment {env}", False, f"mapped for {repo} but doesn't exist",
                                  "add envs/<name>.yaml or create it with create_environment (extends)"))
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
                                  "build mapped environments at startup)",
                                  action={"id": "build_environment", "env": env}))

    # --- identity: environments that attach a control-plane credential fail to start (review of #81)
    from hangar.identity import doctor_check as exposure_check

    if (c := exposure_check(fleet.catalog, h.store, s)) is not None:
        out.append(c)
    # --- teamcreds: personal logins still shared, work no person owns without a team credential, unlinked senders
    from hangar.teamcreds import doctor_checks as team_checks

    out += team_checks(h)

    # --- workstream setup: each mapped repo's webhook exists, is active and sends the events hangar handles
    from hangar.setup_api import webhook_checks

    out += await webhook_checks(h, transport)
    # --- landing: repos that land on approval have the branch protection that keeps it safe
    from hangar.landing import doctor_checks as landing_checks

    out += await landing_checks(h)  # through the job service's GitHub transport, like the PRs themselves

    # --- workstream B: slack
    from hangar.slack import doctor_checks as slack_checks

    out += await slack_checks(h, transport)
    from hangar.inbox_notify import doctor_checks as inbox_checks  # --- inbox delivery (#157)

    out += inbox_checks(h)
    from hangar.jira import doctor_checks as jira_checks  # --- intake

    out += await jira_checks(h, transport)
    from hangar import pluginhost  # --- plugin host: each plugin, its version and what it added

    out += await pluginhost.doctor_checks(h)
    cfg = getattr(h, "config", None)  # --- KAN-217: a removed setting stored with `hangar config` before the upgrade
    for key, msg in (cfg.removed_settings() if hasattr(cfg, "removed_settings") else []):
        out.append(_check(f"removed setting {key}", False, msg, f"hangar config unset {key}"))

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
                          "hangar config set HANGAR_SESSION_STORE gs://<bucket>  (terraform output session_bucket), "
                          "then restart hangar"))
    # --- overnight/sweep: the newest database backup is recent enough for its schedule
    if (backups := getattr(h, "backups", None)) is not None:
        from hangar.backup import doctor_check

        if (c := await doctor_check(backups, s, "kubernetes" in fleet.providers)) is not None:
            out.append(c)
        from hangar.backup import isolation_check  # --- durable: backups apart from the checkpoints

        if (c := await isolation_check(backups, s)) is not None:
            out.append(c)
    from hangar.keys import doctor_checks as key_checks  # --- durable: the vault key and its rotation

    out += key_checks(h)
    # git gateway (workstream F): reachable, and every GitHub App credential can mint a token
    out += await fleet.gateway.doctor(transport)
    from hangar import posture  # --- intake posture (#123): gateway, egress proxy, intake environments' modes

    out += posture.doctor_checks(h)
    from hangar import egress  # --- KAN-174: the default mode and the hosts sandboxes were refused

    out += egress.doctor_checks(h)
    from hangar import power  # --- ops: the pools' power state, the last deploy, the background loops

    out += power.doctor_checks(h)
    from hangar import deploylag  # --- KAN-216: the deployed commit and how far it is behind main

    out += await deploylag.doctor_checks(h)
    from hangar import credhealth  # --- clouddoctor: logins that stopped working or expire soon

    out += credhealth.doctor_checks(h)
    from hangar import modelpolicy  # --- integ3 (review #105): jobs with no budget and no daily cap

    out += modelpolicy.doctor_checks(h)
    from hangar import isolation  # --- KAN-189: E2B's uid split default

    out += isolation.doctor_checks(h)
    from hangar import approvals  # --- approvals (#121): a lone person can't approve their own held jobs

    out += approvals.doctor_checks(h)
    from hangar import outbox  # --- outbox (#163, #189): a backlog, withheld objects, retention held for export

    out += outbox.doctor_checks(h)
    from hangar import harness_repo  # --- #138: the team harness repo: reachable, last refresh, channels

    out += harness_repo.doctor_checks(h)
    out += mcp_checks(h)  # --- #140: both MCP servers
    await asyncio.sleep(0)
    return out


def mcp_checks(h) -> list[dict[str, Any]]:
    """--- #140: each MCP endpoint is mounted and lists its tools, within its budget (tests/test_mcp_modern.py)."""
    from hangar import mcp_live

    servers = getattr(h, "mcp_servers", None)
    if not servers:
        return []
    out = []
    base = h.settings.public_url.rstrip("/")
    for path, name in ((mcp_live.PATHS[mcp_live.WORK], mcp_live.WORK), (mcp_live.PATHS[mcp_live.SANDBOX],
                                                                        mcp_live.SANDBOX)):
        srv = servers.get(path)
        if srv is None:
            out.append(_check(f"mcp {name}", False, f"{base}{path} isn't served", "upgrade or restart hangar"))
            continue
        tools = srv.listing()
        size = len(json.dumps([{"name": t.name, "description": t.description, "inputSchema": t.input_schema}
                               for t in tools], separators=(",", ":")))
        detail = f"{base}{path}: {len(tools)} tools, {size:,} bytes listed"
        if name == mcp_live.SANDBOX:
            detail += " (`hangar connect <client> --with-sandbox` adds it to a client)"
        out.append(_check(f"mcp {name}", bool(tools), detail, "the server lists no tools: check hangar's log"))
    return out
