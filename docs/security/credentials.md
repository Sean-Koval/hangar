# Credentials in sandboxes

How a credential reaches a sandbox and what keeps it from leaving: delivery, build secrets and short-lived cloud credentials. Part of the [security model](README.md).

## Credential handling

- **Delivery.** Secrets are written into a running sandbox at claim time, to `~/.hangar/env` (mode 0600) or to the harness's own file. Sandbox images never contain them.
- **Forks.** A fork scrubs inherited secret files before re-injecting only the credentials it was granted.
- **Subscription logins are interactive-only.** An `oauth_token` or `auth_file` credential (Claude Pro or Max, a ChatGPT or Google sign-in) goes only into sandboxes and sessions a person starts for themselves, with a warning on the session, the sandbox and each turn's manifest. Every job's sandbox (every front door, best-of-N attempts, follow-ups, autofix, environment setup, the Cloud Doctor), schedules, webhooks, experiment trials and graders, and replay and fork sandboxes of them refuse one and use an API key: the vendors' terms don't allow automated use of consumer logins. No setting relaxes this: `HANGAR_SUBSCRIPTION_LOGINS=any` was [removed](../upgrading.md#removed-switches) after v0.2.0. Spend limits on API keys are set at the vendor; hangar's budgets cap spend inside hangar ([team.md](../team/subscription-logins.md)).
- **Exclusive credentials** are ones with rotating refresh tokens, like a Codex ChatGPT login. Each is leased to one sandbox. When that sandbox stops, the possibly rotated file is copied back into the vault.
- **Snapshots** of a running sandbox (Docker `commit`) may contain secrets that were present at snapshot time. They stay in the local image store and are never pushed.
- **Control-plane credentials never enter a sandbox.** Some credentials exist only for hangar itself: webhook signing keys (`webhook` credentials, the GitHub webhook secret, inbound webhook keys), the Slack bot token and signing secret, GitHub App keys, and the GitHub tokens hangar calls GitHub with (`HANGAR_GITHUB_TOKEN_CREDENTIAL`, `HANGAR_GATEWAY_CREDENTIAL`, `HANGAR_GITHUB_APP_CREDENTIAL`). hangar refuses to put them in a sandbox by any path: an environment's `secrets:` (refused when the environment is saved, and again when a sandbox starts), an attached credential's env var or file, and so MCP config resolution, which only sees what those wrote. With the webhook secret anyone could forge trusted GitHub deliveries. `hangar cred ls` marks them `control-plane`. To share the control plane's GitHub token with direct-mode sandboxes on purpose, an admin runs `hangar cred exposure <name> sandbox`; signing keys, bot tokens and App keys can't be shared.
- **API keys can stay out of the sandbox.** With `model_gateway: record` on the environment, a Claude Code or Codex sandbox gets a per-sandbox `hgm_` bearer and the [model gateway](../model-gateway.md)'s URL instead of the API key: the gateway, on the git gateway's port, sends the vault's key upstream and records the calls as redacted, hash-chained tapes. The bearer is good for one provider, only while its sandbox runs. Record mode refuses subscription logins (they can't be proxied), and a tape of a tainted run is marked `untrusted`.
- **Exposure inside the sandbox.** Anything you inject is readable by the agent in that sandbox, and by any MCP client holding a hangar token (`exec`/`read_file` can read `~/.hangar/env`). Prefer scoped credentials: fine-grained GitHub tokens limited to the target repo, and API keys with spend limits. For git, use the [gateway](git-gateway.md): the token then never enters the sandbox.
- **Forks** run `tmux kill-server` before scrubbing secret files, so shells that exported the parent's secrets don't survive into the copy. A full memory snapshot (E2B) still carries any secrets held in other long-running processes' memory, so fork from a clean point when it matters.
- **Audit redaction.** Commands, prompts and terminal input are recorded in the audit log with stored credential values masked as `***`. Secrets that were never stored in the vault can't be recognized, so don't paste unrelated secrets into agent terminals. See [what redaction can and can't stop](secret-scanning.md#redaction).
- **Browser streams.** The dashboard never puts its token in a URL. Websocket terminals and build-log streams use single-use tickets that expire after 60 seconds.

## Build secrets

Some secrets are needed only to build an environment, such as a private registry's read token. They go to the build step and never to a running sandbox. They can be named only where an admin decides:

```yaml
name: api
build_secrets: {NPM_TOKEN: npm-ci-token}      # secret id -> vault credential; needs an admin
```

A repo policy can set its own (`PUT /api/repos/policy` with `build_secrets`, admin), which win for that repo. A repo's own environment file lists only the ids it needs (`build_secrets: [NPM_TOKEN]`); an id the admin hasn't mapped fails the build and names the id. So anyone who can push to the repo can ask for a mapped secret by id, but can't point an id at another credential.

How a build uses them (`hangar.buildsecrets`, used by repo environment builds):

| Build step | How the value gets there | What keeps it out of the sandbox |
| --- | --- | --- |
| Dockerfile (`docker build`) | `--secret id=NPM_TOKEN,src=<file>`; the Dockerfile uses `RUN --mount=type=secret,id=NPM_TOKEN[,env=NPM_TOKEN] …` | BuildKit mounts it for that one instruction and doesn't write it to a layer. The file is 0600 in a 0700 temp directory, removed when the build ends, and never on a command line or in the build log. |
| Install step in a build sandbox | env vars on that one exec, the only secrets the build sandbox holds ([what else it holds](git-gateway.md#jobs-only-fetch-hangar-delivers): a fetch-only grant) | Never written to `~/.hangar/env`. Before the result is captured, `leak_scan` greps the workspace and `$HOME` for each value (patterns from a 0600 file, not the command line) and fails the build naming the file and the id, never the value (`npm config set` writes the token to `.npmrc`). The build sandbox is destroyed. |
| Cloud Build | refused | Cloud Build reads Secret Manager, not hangar's vault: build that image with Docker. |

**What a build passes on.** A repo build's checkout, `install`'s work, goes into every sandbox started from the build, so it is trusted like the repo's default branch. Its `.git` is the exception: hooks and git config are things git runs, not code anyone reviews, so before the checkout is captured hangar removes the hooks (keeping the samples) and every config key but the remotes, branches, user and core settings (`envbuilds.GIT_SCRUB`, editing the file without following its includes). hangar's own git commands in a sandbox started from a build run with hooks and fsmonitor off, which also covers builds made before. On Kubernetes, a build's SandboxTemplate is deleted only once no live sandbox runs on it.

Mark a build-only credential control-plane only (`hangar cred exposure npm-ci-token control_plane`) so no environment can also map it into a running sandbox. Signing keys, bot tokens and GitHub App keys are refused as build secrets. **A build secret reaches the code the build runs, and that code can send it wherever the build's network allows.** Give builds tokens that can only read the registry they're for. The build sandbox follows the environment's [egress mode](egress.md).

## Short-lived cloud credentials

Don't store a GCP service-account key or an AWS access key as a credential: the agent can read it, and it lives as long as the key does. Give the environment `cloud:` instead (admin only):

```yaml
name: infra
cloud:
  gcp:
    provider: projects/123456789/locations/global/workloadIdentityPools/hangar/providers/hangar
    service_account: ci-agent@my-proj.iam.gserviceaccount.com   # optional: impersonate it
  aws:
    role_arn: arn:aws:iam::123456789012:role/hangar-agents
    region: us-east-1                                            # optional
  lifetime_minutes: 60                                           # 5 to 60
```

hangar is an OIDC issuer (`cloudcreds.py`). Each sandbox of the environment gets an ID token, signed RS256 with a key kept in hangar's vault, with these claims:

- `iss`: `HANGAR_OIDC_ISSUER`, or `HANGAR_PUBLIC_URL`;
- `sub`: `hangar:env:<environment>`, where the environment is the one whose own file sets `cloud:`. Only an admin can write that, so a member's environment that `extends` it speaks for it, and can't take another's subject by taking its name (before an admin creates it, or after one is removed);
- `aud`: the cloud's audience;
- `exp`: `lifetime_minutes` from now;
- `environment` (the same name) and `person` (who started the sandbox): hangar vouches for both, so grant on them;
- `sandbox_environment`, `repo`, `sandbox`, `session` and `job`: they describe the sandbox, but whoever starts one picks its repo and environment, so **don't grant on them**. A condition on `repo` lets any member who can start a sandbox with `cloud:` name that repo.

The token is written to `~/.hangar/cloud/` (0600). While the sandbox runs, hangar replaces it once less than half its life is left, checking every 5 minutes. It is never renewed after the sandbox stops, and each issuance is audited as `cloud.token`. Discovery is public at `/.well-known/openid-configuration`, and the public keys at `/oidc/jwks.json`. `POST /api/cloud/rotate` (admin) adds a new signing key and signs with it at once. The keys it replaces stay published for an hour (the longest a token can live), so tokens already issued still verify, and then drop out of the JWKS. For a key that leaked, `POST /api/cloud/rotate?drop_old=true` unpublishes the old keys now, and the tokens they signed stop working. Where the JWKS is uploaded (GCP's `--jwk-json-path`), upload it again right after rotating: tokens signed with the new key fail until Google has it.

**Scheduled rotation.** `HANGAR_OIDC_KEY_ROTATION_DAYS` (0, off) makes a new signing key every that many days. The next key goes into the JWKS a week (at most half the period) before it signs anything, audited as `cloud.key.next` with the time it takes over, so a relying party that caches the JWKS has it in time; where the JWKS is uploaded, upload it again after that entry and before then. When it takes over (`cloud.key.rotated`) the key before it retires and leaves the JWKS an hour later. It is off by default because an uploaded JWKS that isn't refreshed would make every token fail after the switch; turn it on where the cloud fetches the JWKS itself (AWS, or GCP without `--jwk-json-path`).

**Lifetime.** A token lives at most 60 minutes (`lifetime_minutes`, 5 to 60). It is renewed every few minutes while its sandbox runs, so the lifetime only bounds how long a copy outlives the sandbox.

**GCP (Workload Identity Federation).** The sandbox gets the `external_account` credential configuration that `gcloud iam workload-identity-pools create-cred-config --credential-source-file=… --credential-source-type=text` produces. `GOOGLE_APPLICATION_CREDENTIALS` and `CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE` point client libraries and gcloud at it. google-auth reads the token file again on every refresh, so a renewed token is picked up. Set up the pool once:

```sh
curl -s https://hangar.example.com/oidc/jwks.json > hangar-jwks.json   # or let Google fetch it, if it can reach hangar
gcloud iam workload-identity-pools create hangar --location=global
gcloud iam workload-identity-pools providers create-oidc hangar --location=global --workload-identity-pool=hangar \
  --issuer-uri=https://hangar.example.com --jwk-json-path=hangar-jwks.json \
  --attribute-mapping='google.subject=assertion.sub,attribute.environment=assertion.environment,attribute.person=assertion.person' \
  --attribute-condition='assertion.environment in ["infra"]'
gcloud iam service-accounts add-iam-policy-binding ci-agent@my-proj.iam.gserviceaccount.com \
  --role=roles/iam.workloadIdentityUser \
  --member='principal://iam.googleapis.com/projects/123456789/locations/global/workloadIdentityPools/hangar/subject/hangar:env:infra'
```

- **The issuer can stay private.** Uploading the JWKS (`--jwk-json-path`, up to 8 keys) means Google never has to reach hangar, which suits a control plane behind IAP. Re-upload after `rotate`.
- **The audience** is the provider's default, `https://iam.googleapis.com/projects/…/providers/…`. Set `gcp.audience` if the provider was created with `--allowed-audiences`.
- **Tokens:** Google accepts RS256 tokens whose `exp` is at most 24 hours after `iat`.

**AWS (`AssumeRoleWithWebIdentity`).** The sandbox gets `AWS_ROLE_ARN`, `AWS_WEB_IDENTITY_TOKEN_FILE` and `AWS_ROLE_SESSION_NAME` (`hangar-<sandbox id>`), which every AWS SDK and the CLI use to assume the role from the token file and re-read it. The token's `aud` is `aws.audience` (`sts.amazonaws.com`). Create an IAM OIDC identity provider for the issuer URL with that audience as its client id. The role's trust policy **must** match the condition key `<issuer without https://>:sub` against `hangar:env:<environment>`, as well as `:aud`. Every environment's token has the same `aud`, and `AWS_ROLE_ARN` is just a variable the agent can change, so a trust policy that checks only `aud` lets any hangar sandbox with AWS credentials assume the role. For the same reason, on GCP grant to `principal://…/subject/hangar:env:<environment>` (or an attribute condition on `environment`), never to the whole pool (`principalSet://…/*`). **AWS fetches the discovery document and JWKS itself**, so the issuer URL must be publicly reachable over HTTPS. If hangar sits behind IAP, publish the two documents somewhere public (`/.well-known/openid-configuration` with `jwks_uri` pointing at the JWKS file) and set `HANGAR_OIDC_ISSUER` to that URL.

**Egress.** In `allowlist` and `none` environments the token exchange is let through automatically: `sts.googleapis.com` and `iamcredentials.googleapis.com`, `sts.amazonaws.com` and `sts.<region>.amazonaws.com`.

**What it does and doesn't cover.**

- The agent can read the token and use it, which is the point, but only:
  - for the audience it names;
  - for what the cloud grants its subject, which you can narrow per environment and person (not per repo: see the claims above);
  - for at most `lifetime_minutes` after the sandbox stops.
- An access token the cloud already issued from it lives its own lifetime (GCP's default is an hour).
- Don't give a sandbox a role you wouldn't give the agent.
- The token is not stored in the vault, so redaction doesn't mask it; it's short-lived instead.
- Tested against google-auth's own `external_account` code with a fake STS. A live pool and an AWS account weren't available to this change.

