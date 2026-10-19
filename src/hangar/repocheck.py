"""`hangar repo check OWNER/REPO` (#155): is this repo ready for agent work? One report for landing, the git gateway,
CI and the doors (GitHub, Jira, Slack), so onboarding ends with one green or red answer.

`hangar doctor` checks each subsystem across every repo; this runs the same kind of checks for one repo, plus what the
intake posture (#123), sudo (#124) and subscription-login (#125) defaults mean for it:

* GitHub: the repo is in HANGAR_GITHUB_REPOS, the GitHub App mints an installation token for it, hangar can read it,
  its webhook sends the events hangar handles, and the trigger label exists;
* the repo policy: `verify`, `land` with approvers or CODEOWNERS, and a daily cap;
* CI and the base branch: required checks, stale-approval dismissal and approval of the last push (branch protection
  or rulesets; classic protection needs Administration: read, which the App lacks, so unreadable is a warning);
* the environments its jobs run in: credential mode `gateway`, `sudo: false`, egress other than `open`, heavy only
  where the policy allows it for intake, and agent logins that are API keys rather than subscription logins;
* Jira: a project mapping to the repo, its trigger and the statuses hangar moves issues into; Slack: the channels that
  map to it (informational).

Read-only: GitHub, Jira and Slack only see GET requests, except the App's installation-token mint (POST
/app/installations/{id}/access_tokens), which writes nothing on the repo. GitHub answers are kept for landing's 5
minutes (Landing._cached), so a re-run doesn't spend rate limit. Control-plane calls only: it works with the sandbox
pools powered down.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import httpx

from hangar.github import GitHub, GitHubError

if TYPE_CHECKING:
    from hangar.envspec import EnvSpec
    from hangar.jobs import JobService
    from hangar.repopolicy import RepoPolicy

STAGES = ("started", "needs_input", "delivered", "failed")  # Jira statuses hangar moves an issue into


def _c(check: str, status: str, detail: str, fix: str | None = None) -> dict[str, Any]:
    return {"check": check, "status": status, "detail": detail, "fix": None if status == "ok" else fix}


def _d(x: Any) -> dict[str, Any]:
    return x if isinstance(x, dict) else {}


def summary(checks: list[dict[str, Any]]) -> dict[str, Any]:
    failing = sum(c["status"] == "fail" for c in checks)
    warnings = sum(c["status"] == "warn" for c in checks)
    return {"ready": not failing, "failing": failing, "warnings": warnings,
            "summary": "ready" if not failing else f"not ready ({failing} failing)"}


def mapped(settings: Any, repo: str) -> tuple[str | None, list[str]]:
    """The repo's key in HANGAR_GITHUB_REPOS (GitHub names are case-insensitive) and its environments."""
    for key, raw in (settings.github_repos or {}).items():
        k = key.strip().removesuffix(".git")
        if k.lower() == repo.lower() or k.lower().endswith(f"github.com/{repo.lower()}"):
            return key, [e.strip() for e in str(raw).split(",") if e.strip()]
    return None, []


def _same_repo(value: str, repo: str) -> bool:
    from hangar.repopolicy import repo_key

    return repo_key(value) == repo.lower()


# ====================================================================== the report
async def run(h: Any, repo: str, transport: httpx.AsyncBaseTransport | None = None) -> dict[str, Any]:
    """The readiness of `repo` (owner/repo): {repo, ready, failing, warnings, summary, checks: [...]}."""
    from hangar.repopolicy import repo_key

    name = repo_key(repo) or repo
    svc: JobService = h.jobs
    s = h.settings
    out: list[dict[str, Any]] = []
    key, envs = mapped(s, name)
    out.append(_c("allowlist", "ok" if key else "fail",
                  f"in HANGAR_GITHUB_REPOS ({key}: {', '.join(envs) or 'no environment'})" if key else
                  "not in HANGAR_GITHUB_REPOS: GitHub, Slack, Jira and webhook jobs can't start on it",
                  f"hangar setup github --repo {name}=<environment>   (or add it to HANGAR_GITHUB_REPOS)"))
    policy = svc.policies.get(name)
    github, owners = await _github(h, svc, name, policy)
    out += github
    out += _policy(h, name, policy, envs, owners)
    out += await _gateway(s, name, transport)
    for env in _environments(h, name, policy, envs):
        out += _environment(h, svc, name, policy, env)
    out += await _jira(h, name, transport)
    out += _slack(s, name)
    return {"repo": name, **summary(out), "checks": out}


def _environments(h: Any, repo: str, policy: RepoPolicy | None, envs: list[str]) -> list[str]:
    """Every environment the repo's jobs may run in: the policy's, the allowlist's, and Jira projects' own."""
    out = ([policy.environment] if policy is not None and policy.environment else []) + envs
    from hangar import jira

    with contextlib.suppress(Exception):
        cfg = jira.load(h.settings)
        for p in (cfg.projects.values() if cfg else []):
            if p.environment and (_same_repo(p.repo, repo) or any(_same_repo(r, repo) for r in p.components.values())):
                out.append(p.environment)
    return list(dict.fromkeys(out))


# ====================================================================== GitHub: the App, access, webhook, label, CI
async def _app_token(svc: JobService, repo: str) -> tuple[GitHub | None, dict[str, Any]]:
    """GitHub as the App for `repo` (landing's identity and token cache), and the check saying whether it minted."""
    s = svc.settings
    name = s.github_app_credential
    if not name:
        return None, _c("github app", "warn", "HANGAR_GITHUB_APP_CREDENTIAL is not set: agent PRs come from the "
                        "control token, whose owner can't approve them, and nothing lands on approval",
                        "create a GitHub App and install it on the repo (docs/landing.md), then hangar config set "
                        "HANGAR_GITHUB_APP_CREDENTIAL <credential>")
    lnd = svc.landing
    try:
        value = svc.broker.secret_text(name)
        lnd._client()
        tok = await lnd._tokens.token(value, repo)  # type: ignore[union-attr]
    except Exception as e:  # noqa: BLE001 - the App's errors never carry its key or tokens
        return None, _c("github app", "fail", f"the GitHub App ({name}) can't mint a token for {repo}: "
                        f"{str(e)[:300]}", f"install the App on {repo}: https://github.com/settings/installations "
                        "(or the organization's), with Contents and Pull requests: read and write")
    return GitHub(tok, s.github_api_url, transport=svc.transport), _c(
        "github app", "ok", f"the App ({name}) is installed on {repo}: an installation token minted")


async def _github(h: Any, svc: JobService, repo: str,
                  policy: RepoPolicy | None) -> tuple[list[dict[str, Any]], bool | None]:
    """The GitHub checks, and whether the base branch has a CODEOWNERS file (None: couldn't read the repo)."""
    out: list[dict[str, Any]] = []
    owners: bool | None = None
    app, check = await _app_token(svc, repo)
    out.append(check)
    control = svc._github()
    gh = app or control
    try:
        if gh is None:
            out.append(_c("github access", "fail", "no GitHub credential to read the repo with",
                          "hangar setup github   (or set HANGAR_GITHUB_APP_CREDENTIAL)"))
            return out, owners
        lnd = svc.landing
        try:
            info = await lnd._cached("readiness:repo", repo, "", lambda: gh._req("GET", f"/repos/{repo}"))
        except (GitHubError, httpx.HTTPError) as e:
            out.append(_c("github access", "fail", f"can't read the repo: {str(e)[:300]}",
                          "install the GitHub App on the repo, or give the control token access to it"))
            return out, owners
        info = _d(info)
        base = info.get("default_branch") if isinstance(info.get("default_branch"), str) else "main"
        out.append(_c("github access", "ok", f"{'the App' if app else 'the control token'} reads {repo} "
                      f"(default branch {base})"))
        out.append(await _webhook(h, control, repo))
        out.append(await _label(svc, gh, repo))
        out += await _branch(svc, gh, control, repo, base, policy, info)
        with contextlib.suppress(GitHubError, httpx.HTTPError):
            owners = bool(await lnd.codeowners(gh, repo, base))
    finally:
        for c in {id(x): x for x in (app, control) if x is not None}.values():
            await c.close()
    return out, owners


async def _webhook(h: Any, control: GitHub | None, repo: str) -> dict[str, Any]:
    from hangar.setup_api import HOOK_EVENTS, find_hook, hook_problem, hook_url, unreachable

    s = h.settings
    if not s.github_webhook_secret_credential or control is None:
        return _c("github webhook", "fail", "GitHub webhooks aren't set up (HANGAR_GITHUB_WEBHOOK_SECRET_CREDENTIAL "
                  "or HANGAR_GITHUB_TOKEN_CREDENTIAL is unset): no issue, review or CI event reaches hangar",
                  f"hangar setup github --repo {repo}=<environment>")
    url = hook_url(h)
    if far := unreachable(url):
        return _c("github webhook", "warn", f"GitHub can't deliver to {url} ({far})",
                  "set HANGAR_PUBLIC_URL to hangar's public https URL")
    try:
        hook = await h.jobs.landing._cached("readiness:hook", repo, url, lambda: find_hook(control, repo, url))
    except (GitHubError, httpx.HTTPError) as e:
        return _c("github webhook", "warn", f"can't list the repo's webhooks: {str(e)[:200]}",
                  f"check by hand that {repo} has a webhook to {url} (JSON) with events {', '.join(HOOK_EVENTS)}")
    problem = hook_problem(hook)
    return _c("github webhook", "ok" if problem is None else "fail",
              f"{url} receives {', '.join(HOOK_EVENTS)}" if problem is None else f"{problem} to {url}",
              f"hangar doctor --fix   (or: hangar setup github --repo {repo}=<environment>)")


async def _label(svc: JobService, gh: GitHub, repo: str) -> dict[str, Any]:
    label = svc.settings.github_trigger_label or "hangar"

    async def load() -> bool:
        try:
            await gh._req("GET", f"/repos/{repo}/labels/{quote(label, safe='')}")
            return True
        except GitHubError as e:
            if ": 404 " in str(e):
                return False
            raise

    try:
        has = await svc.landing._cached(f"readiness:label:{label}", repo, "", load)
    except (GitHubError, httpx.HTTPError) as e:
        return _c("trigger label", "warn", f"can't read the label '{label}': {str(e)[:200]}",
                  f"gh label create {label} --repo {repo}")
    return _c("trigger label", "ok" if has else "warn",
              f"issues labelled '{label}' start jobs" if has else
              f"the label '{label}' doesn't exist, so nobody can label an issue to start a job (a `/hangar` comment "
              "still works)", f"gh label create {label} --repo {repo} --description 'hangar: an agent works on this'")


async def _protection(admin: GitHub | None, gh: GitHub, repo: str, base: str) -> dict[str, Any]:
    """What the base branch's classic protection and rulesets require, and which of the two could be read."""
    have = {"reviews": False, "stale": False, "last_push": False}
    classic = rules = False
    b = quote(base, safe="/")
    try:
        prot = _d(_d(await (admin or gh)._req("GET", f"/repos/{repo}/branches/{b}/protection"))
                  .get("required_pull_request_reviews"))
        classic = True
        have["reviews"] |= (prot.get("required_approving_review_count") or 0) >= 1
        have["stale"] |= bool(prot.get("dismiss_stale_reviews"))
        have["last_push"] |= bool(prot.get("require_last_push_approval"))
    except GitHubError as e:
        classic = " 404 " in str(e) and "not protected" in str(e).lower()  # readable: there is none
    except httpx.HTTPError:
        pass
    with contextlib.suppress(GitHubError, httpx.HTTPError):
        for rule in await gh._req("GET", f"/repos/{repo}/rules/branches/{b}", params={"per_page": 100}) or []:
            rules = True
            if _d(rule).get("type") == "pull_request":
                prm = _d(rule.get("parameters"))
                have["reviews"] |= (prm.get("required_approving_review_count") or 0) >= 1
                have["stale"] |= bool(prm.get("dismiss_stale_reviews_on_push"))
                have["last_push"] |= bool(prm.get("require_last_push_approval"))
    return {**have, "classic": classic, "rules": rules}


async def _branch(svc: JobService, gh: GitHub, admin: GitHub | None, repo: str, base: str,
                  policy: RepoPolicy | None, info: dict[str, Any]) -> list[dict[str, Any]]:
    lnd = svc.landing
    out: list[dict[str, Any]] = []
    where = (f"https://github.com/{repo}/settings/branches (or a ruleset on {base})")
    try:
        required = await lnd.required_checks(gh, repo, base)
    except (GitHubError, httpx.HTTPError):
        required = frozenset()
    out.append(_c("required checks", "ok" if required else "fail",
                  f"{base} requires {', '.join(sorted(required))}" if required else
                  f"{base} requires no status checks, so a PR can merge with CI red or not run",
                  f"{where}: require status checks to pass before merging, and pick the CI jobs"))
    p = await lnd._cached("readiness:protection", repo, base, lambda: _protection(admin, gh, repo, base))
    seen = p["classic"] or p["rules"]
    if not p["classic"]:
        out.append(_c("classic branch protection", "warn",
                      "couldn't read classic protection (it needs Administration: read, which the App doesn't have by "
                      "design)" + ("; the rulesets were read instead" if p["rules"] else " and found no ruleset"),
                      "give the control token Administration: read, or move the rules to a ruleset "
                      f"(https://github.com/{repo}/settings/rules)"))
    opted_out = policy is not None and policy.require_stale_dismissal is False
    auto = policy is not None and policy.land == "auto_after_approval"
    if p["stale"]:
        out.append(_c("stale approvals", "ok", f"{base} dismisses stale approvals when new commits are pushed"))
    else:
        unread = "" if seen else " (hangar couldn't read its protection or rulesets)"
        out.append(_c("stale approvals", "warn" if opted_out else "fail",
                      f"{base} doesn't dismiss stale approvals when new commits are pushed{unread}"
                      + ("; the repo policy opted out (require_stale_dismissal: false)" if opted_out else
                         ": a push after an approval could merge unreviewed, and hangar won't arm auto-merge"),
                      f"{where}: dismiss stale pull request approvals when new commits are pushed"))
    gaps = [w for k, w in (("reviews", "a required approving review"),
                           ("last_push", "approval of the most recent reviewable push")) if not p[k]]
    out.append(_c("review rules", "warn" if gaps else "ok",
                  f"{base} requires a fresh approval of the latest push" if not gaps else
                  f"{base} doesn't require {' or '.join(gaps)}",
                  f"{where}: require a pull request with 1+ approvals and approval of the most recent reviewable "
                  "push"))
    if auto and info.get("allow_auto_merge") is False:
        out.append(_c("auto-merge", "fail", "the policy lands on approval, but the repo doesn't allow auto-merge",
                      f"https://github.com/{repo}/settings: allow auto-merge"))
    return out


# ====================================================================== the repo policy
def _policy(h: Any, repo: str, policy: RepoPolicy | None, envs: list[str],
            owners: bool | None) -> list[dict[str, Any]]:
    set_ = f"hangar repo set {repo}"
    if policy is None:
        verify = _env_verify(h, envs[0]) if envs else None
        out = [_c("repo policy", "fail", "no repo policy: no landing rule, approvers or daily cap",
                  f"{set_} --verify '<test command>' --land auto_after_approval --approver <login> "
                  "--max-usd-per-day 50")]
        out.append(_c("verify", "ok" if verify else "fail", f"jobs run `{verify}` (environment)" if verify else
                      "no check: jobs deliver PRs nothing has verified", f"{set_} --verify '<test command>'"))
        return out
    out = [_c("repo policy", "ok", f"set by {policy.updated_by or '?'}")]
    env = policy.environment or (envs[0] if envs else None)
    verify = policy.verify if policy.verify is not None else (_env_verify(h, env) if env else None)
    src = "repo policy" if policy.verify is not None else "environment"
    out.append(_c("verify", "ok" if verify else "fail", f"jobs run `{verify}` ({src})" if verify else
                  "no check: jobs deliver PRs nothing has verified", f"{set_} --verify '<test command>'"))
    if policy.land is None:
        out.append(_c("landing", "warn", "land is unset: approved PRs wait for someone to merge them",
                      f"{set_} --land auto_after_approval --approver <login>   (or --land manual to say so)"))
    elif policy.land == "manual":
        out.append(_c("landing", "ok", "land: manual (people merge approved PRs)"))
    else:
        who = ", ".join(policy.approvers or [])
        ok = bool(who) or bool(owners)
        out.append(_c("landing", "ok" if ok else ("warn" if owners is None else "fail"),
                      f"lands on approval by {who or 'code owners (CODEOWNERS)'}" if ok else
                      "land: auto_after_approval with no approvers, and hangar couldn't read the repo to look for a "
                      "CODEOWNERS file" if owners is None else
                      "land: auto_after_approval, but there are no approvers and no CODEOWNERS file, so no approval "
                      "can land a PR", f"{set_} --approver <login>   (or add a CODEOWNERS file)"))
    out.append(_c("daily cap", "ok" if policy.max_usd_per_day else "warn",
                  f"${policy.max_usd_per_day:g} a day" if policy.max_usd_per_day else
                  "no max_usd_per_day: a runaway intake loop spends without limit", f"{set_} --max-usd-per-day 50"))
    return out


def _env_verify(h: Any, env: str) -> str | None:
    try:
        return h.fleet.catalog.get(env).verify
    except Exception:  # noqa: BLE001 - the environment check reports it
        return None


# ====================================================================== the git gateway and the posture
async def _gateway(s: Any, repo: str, transport: httpx.AsyncBaseTransport | None) -> list[dict[str, Any]]:
    from hangar import posture

    out: list[dict[str, Any]] = []
    p = posture.mode(s)
    out.append(_c("intake posture", {"enforce": "ok", "warn": "warn"}.get(p, "fail"),
                  {"enforce": "enforce: intake jobs need the gateway and get at least an allowlist egress",
                   "warn": "warn: intake jobs run as before, and their events say what enforce would change"}.get(
                      p, "off: intake jobs run with a GitHub token when there is no gateway, and on open egress"),
                  f"HANGAR_INTAKE_POSTURE=enforce ({posture.DOC})"))
    url = f"https://github.com/{repo}.git"
    if not posture.gateway_serves(s, url):
        out.append(_c("git gateway", "fail", posture.gateway_problem(s, url) or "the gateway doesn't serve the repo",
                      "set HANGAR_GATEWAY_URL (and HANGAR_GATEWAY_UPSTREAMS for github.com); "
                      "docs/security/git-gateway.md"))
        return out
    try:
        async with httpx.AsyncClient(transport=transport, timeout=5) as c:
            r = await c.get(f"{s.gateway_url.rstrip('/')}/healthz")
        ok = r.status_code == 200 and _d(r.json()).get("service") == "hangar-gateway"
        out.append(_c("git gateway", "ok" if ok else "warn",
                      f"{s.gateway_url} serves github.com: jobs fetch through it and hangar pushes" if ok else
                      f"{s.gateway_url} answered {r.status_code}, not the gateway",
                      "check HANGAR_GATEWAY_URL, HANGAR_GATEWAY_PORT and the hangar-gateway Service"))
    except (httpx.HTTPError, ValueError) as e:
        out.append(_c("git gateway", "warn", f"the control plane can't reach {s.gateway_url} ({type(e).__name__}); "
                      "sandboxes may still", "from inside a sandbox: curl $HANGAR_GATEWAY_URL/healthz"))
    return out


# ====================================================================== the environments
def _environment(h: Any, svc: JobService, repo: str, policy: RepoPolicy | None, env: str) -> list[dict[str, Any]]:
    from hangar import egress, posture

    s = h.settings
    try:
        spec: EnvSpec = h.fleet.catalog.get(env)
    except Exception as e:  # noqa: BLE001 - KeyError, or a spec that doesn't load
        return [_c(f"environment {env}", "fail", f"mapped for {repo} but doesn't load: {str(e)[:200]}",
                   "add envs/<name>.yaml or create it with create_environment, or fix the mapping")]
    out: list[dict[str, Any]] = []
    p = posture.mode(s)
    url = f"https://github.com/{repo}.git"
    serves = posture.gateway_serves(s, url)
    # credential mode: with the gateway serving the repo, jobs are fetch-only through it whatever the spec says
    if spec.credential_mode == "direct":
        out.append(_c(f"credential mode {env}", "warn" if serves else "fail",
                      "credential_mode: direct: " + ("jobs still fetch through the gateway, but people's sessions in "
                                                     "it hold a GitHub token" if serves else
                                                     "its sandboxes hold a GitHub token an injected prompt could use, "
                                                     "and landing refuses to land their PRs"),
                      f"set credential_mode: gateway on {env} (docs/security/git-gateway.md)"))
    else:
        out.append(_c(f"credential mode {env}", "ok" if serves else "fail",
                      "gateway: sandboxes hold a fetch-only grant, never a GitHub token" if serves else
                      "resolves to direct: there is no gateway for the repo, so its sandboxes hold a GitHub token",
                      "set HANGAR_GATEWAY_URL (docs/security/git-gateway.md)"))
    docker = svc._runs_docker(spec, url)
    if spec.sudo and not docker:
        out.append(_c(f"sudo {env}", "warn" if p == "enforce" else "fail",
                      "sudo: true" + (" (job sandboxes on Docker run with no-new-privileges under enforce, so it fails "
                                      "there)" if p == "enforce" else ": the agent can become root in its sandbox"),
                      f"set sudo: false on {env} and install at build time (tools.apt, setup); docs/environments.md"))
    else:
        out.append(_c(f"sudo {env}", "ok", "sudo: false" if not spec.sudo else
                      "sudo: true for Docker on the heavy tier (dockerd runs as root in the sandbox's VM)"))
    if docker:
        allowed = bool(policy and policy.allow_heavy_for_intake)
        out.append(_c(f"heavy {env}", "ok" if allowed else ("fail" if p == "enforce" else "warn"),
                      "runs Docker on the heavy tier; the repo policy allows it for intake jobs" if allowed else
                      posture.heavy_problem(env, True, False) or "", f"hangar repo set {repo} --allow-heavy-for-intake"
                      "   (or a non-heavy environment)"))
    m = egress.env_mode(s, spec)
    if m != "open":
        out.append(_c(f"egress {env}", "ok", f"egress {m}: its sandboxes are on the proxy-only network"))
    elif p == "enforce":
        out.append(_c(f"egress {env}", "warn", "egress open: intake jobs get the allowlist floor, but jobs from MCP, "
                      "REST and schedules on this repo reach the whole internet",
                      f"set egress.mode: allowlist on {env} (docs/security/egress.md)"))
    else:
        out.append(_c(f"egress {env}", "fail", f"egress open and HANGAR_INTAKE_POSTURE={p}: an agent steered by a "
                      "ticket's text can send the repo anywhere",
                      f"set egress.mode: allowlist on {env}, or HANGAR_INTAKE_POSTURE=enforce"))
    out.append(_credentials(h, repo, policy, env, spec))
    return out


def _credentials(h: Any, repo: str, policy: RepoPolicy | None, env: str, spec: EnvSpec) -> dict[str, Any]:
    """The agent login a job on the repo that no person owns would run with: an API key, not a subscription login."""
    from hangar.models import Credential, CredentialKind
    from hangar.teamcreds import CredentialError, SubscriptionRefused, mode_for, resolve

    fleet, store = h.fleet, h.store
    name = f"credentials {env}"
    fix = ("add a team API key: hangar cred add NAME --team --harness HARNESS --kind api_key --file key.txt, and "
           f"list it under credentials: in {env} (docs/team.md#subscription-logins)")
    wanted = policy.harness if policy is not None else None
    try:
        harness = wanted if wanted in spec.agent_harnesses else spec.pick_harness(None)
    except ValueError as e:
        return _c(name, "fail", str(e)[:200], fix)
    if not harness or not fleet._is_agent(harness):
        return _c(name, "warn", f"{env} enables no agent harness", f"list a harness under harnesses: in {env}")
    names = [c for c in spec.credentials if fleet._cred_harness(c) in (harness, None)
             or not fleet._is_agent(fleet._cred_harness(c))]
    try:
        got = resolve(store, names, None, harness, False, fleet._is_agent, mode_for(store, repo), h.settings,
                      purpose="automated").names
    except SubscriptionRefused as e:
        return _c(name, "fail", f"{harness}: {str(e)[:300]}", fix)
    except CredentialError as e:
        return _c(name, "fail", f"{harness}: {str(e)[:300]}", fix)
    creds = [c for n in got if (c := store.get(Credential, n)) is not None and c.harness == harness]
    subs = [c.name for c in creds if c.kind in (CredentialKind.OAUTH_TOKEN, CredentialKind.AUTH_FILE)]
    if subs:
        return _c(name, "fail", f"{harness} jobs would run on subscription logins ({', '.join(subs)}), which the "
                  "vendors' terms reserve for one person's interactive use", fix)
    if not creds:
        return _c(name, "fail", f"{harness}: jobs no person owns would find no login", fix)
    return _c(name, "ok", f"{harness}: API keys ({', '.join(c.name for c in creds)})")


# ====================================================================== the doors: Jira, Slack
async def _jira(h: Any, repo: str, transport: httpx.AsyncBaseTransport | None) -> list[dict[str, Any]]:
    from hangar import jira

    s = h.settings
    if not s.jira:
        return [_c("jira", "ok", "Jira intake is off (HANGAR_JIRA unset): not a door for this repo")]
    try:
        cfg = jira.load(s)
    except jira.JiraConfigError as e:
        return [_c("jira", "fail", str(e), "fix HANGAR_JIRA (docs/intake.md)")]
    assert cfg is not None
    keys = [k for k, p in cfg.projects.items()
            if _same_repo(p.repo, repo) or any(_same_repo(r, repo) for r in p.components.values())]
    if not keys:
        return [_c("jira", "warn", "no Jira project maps to the repo",
                   f"add a project to HANGAR_JIRA's projects: {{\"ABC\": {{\"repo\": \"{repo}\"}}}} (docs/intake.md)")]
    trig = cfg.trigger
    how = " and ".join(x for x in (f"moving an issue to '{trig.status}'" if trig.status else "",
                                   f"labelling it '{trig.label}'" if trig.label else "") if x)
    out = [_c("jira", "ok", f"{', '.join(keys)} -> {repo}; {how} starts a job")]
    want = {stage: getattr(cfg.statuses, stage) for stage in STAGES}
    unset = [k for k, v in want.items() if not v]
    try:
        token = h.broker.secret_text(cfg.token_credential)
    except Exception as e:  # noqa: BLE001
        out.append(_c("jira transitions", "fail", f"the Jira token ({cfg.token_credential}) is unusable: {e}",
                      f"hangar cred add {cfg.token_credential} --harness webhook --kind generic"))
        return out
    api = jira.JiraAPI(cfg, token, transport=transport)
    try:
        for k in keys:
            async def load(k: str = k) -> list[str]:
                res = await api._req("GET", f"{api.api}/project/{quote(k, safe='')}/statuses") or []
                return sorted({str(st.get("name")) for t in res if isinstance(t, dict)
                               for st in t.get("statuses") or [] if isinstance(st, dict) and st.get("name")})

            try:
                have = await h.jobs.landing._cached("readiness:jira-statuses", k, cfg.url, load)
            except (jira.JiraError, httpx.HTTPError) as e:
                out.append(_c(f"jira transitions {k}", "warn", f"couldn't read {k}'s statuses: {str(e)[:200]}",
                              "check the Jira token can browse the project"))
                continue
            folded = {x.casefold() for x in have}
            missing = [f"{stage} '{v}'" for stage, v in want.items() if v and v.casefold() not in folded]
            if trig.status and trig.status.casefold() not in folded:
                missing.insert(0, f"trigger '{trig.status}'")
            if missing:
                out.append(_c(f"jira transitions {k}", "fail",
                              f"{k}'s workflow has no status for {', '.join(missing)} (it has: "
                              f"{', '.join(have[:20]) or 'none'})",
                              "fix HANGAR_JIRA's trigger.status and statuses to name statuses in the project's "
                              "workflow (docs/intake.md)"))
            elif unset:
                out.append(_c(f"jira transitions {k}", "warn",
                              f"statuses resolve; {', '.join(unset)} unset, so hangar leaves the issue's status alone "
                              "at those stages", "set HANGAR_JIRA's statuses (docs/intake.md)"))
            else:
                out.append(_c(f"jira transitions {k}", "ok", "trigger and " + ", ".join(
                    f"{stage} '{v}'" for stage, v in want.items()) + " are in the workflow"))
    finally:
        await api.close()
    return out


def _slack(s: Any, repo: str) -> list[dict[str, Any]]:
    """Informational only: which Slack channels start jobs on the repo."""
    if not (s.slack_channels or s.slack_bot_token_credential):
        return [_c("slack", "ok", "Slack intake is off: not a door for this repo")]
    chans = sorted(ch for ch, r in (s.slack_channels or {}).items() if _same_repo(r, repo))
    return [_c("slack", "ok" if chans else "warn",
               f"channels {', '.join(chans)} start jobs on it" if chans else "no Slack channel maps to the repo",
               f"hangar setup slack --channel <channel id>={repo}")]


# ====================================================================== REST
def mount(app: Any, h: Any, admin: Any) -> None:
    """GET /api/repos/{owner}/{repo}/readiness (admin tokens only)."""

    @app.get("/api/repos/{owner}/{repo}/readiness")
    async def repo_readiness(owner: str, repo: str, _: str = admin):
        return await run(h, f"{owner}/{repo}", h.jobs.transport)
