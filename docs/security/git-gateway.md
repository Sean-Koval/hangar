# The git credential gateway

The GitHub token stays in the control plane; a sandbox gets a grant that fetches, and pushes only where it is allowed. Part of the [security model](README.md).

By default (`credential_mode: direct`) a sandbox gets the GitHub token itself, in `GH_TOKEN` and `~/.git-credentials`, and can push to any branch that token reaches. With the gateway, the token stays in the control plane:

- The sandbox gets a **grant**: an `hgs_…` token that works only at hangar's gateway, only for the sandbox's repo, and only while the sandbox runs. hangar stores its hash; the token itself is written once, to the sandbox's `~/.git-credentials`.
- The sandbox's git is pointed at the gateway with `url.<gateway>/git/github.com/.insteadOf https://github.com/` (and the `git@github.com:` and `ssh://` forms). Clones, fetches and pushes keep working with the usual URLs.
- **What a grant may push depends on the sandbox** ([least privilege, below](#jobs-only-fetch-hangar-delivers)). A job's sandbox, a repo build's and env setup's validation get a **fetch-only** grant: hangar pushes a job's work itself. A session's sandbox (and one a person creates) gets a **branch-scoped** grant, because its agent is meant to push its own branch.
- The gateway reads each push's ref updates before any of the pack is forwarded. A branch-scoped grant allows only the sandbox's own branch (the session branch, or `work_branch`), plus *creating* new branches under `refs/heads/hangar/` (`HANGAR_GATEWAY_PUSH_REFS`); a branch a sandbox created that way becomes its own, and nobody else can update it. That rule never creates a branch under `hangar/session/`, another session's branch, or one that would block either. It refuses pushes to other branches, deletes, tags (`HANGAR_GATEWAY_ALLOW_TAGS`), push options and signed pushes. Git shows the reason: `! [remote rejected] main -> main (hangar: this sandbox may push only to hangar/job-…)`.
- **Every push is scanned for secrets before any of it goes upstream** ([below](secret-scanning.md#the-agents-own-pushes)). A hit refuses the push with the file and line, never the value.
- Allowed requests are streamed to GitHub with the real credential: a PAT from the vault, or a **GitHub App** installation token minted for that one repo with `contents: write` only (all a push needs), cached in memory until shortly before it expires.
- Pushes and refusals are in the audit log (`gw.git.push`, `gw.git.deny`) with the refs and the result. The grant is revoked when the sandbox is destroyed, expires or disappears; a fork gets its own grant.
- **The repo's REST API, read-only.** The same grant reads `GET /gh/api/repos/<owner>/<repo>/…` on the gateway (`ghapi.py`): PRs, review comments, issues, check runs and CI logs. Only the grant's own repository, only GET and HEAD. Upstream it uses an installation token that asks for read permissions only (or, if the App lacks one of them, the push token; the gateway lets only reads through either way). Redirects to CI logs and archives are followed by the gateway, only to GitHub's content hosts and Azure blob storage, without the token. In the sandbox, `HANGAR_GITHUB_API` names it and `~/.netrc` holds the grant for the gateway's host: `curl -sn "$HANGAR_GITHUB_API/repos/acme/app/pulls/12/comments"`.

The control plane's own GitHub calls don't go through the gateway: it opens and comments on agent PRs as the GitHub App when `HANGAR_GITHUB_APP_CREDENTIAL` is set, and comments on issues with `HANGAR_GITHUB_TOKEN_CREDENTIAL` ([landing.md](../landing.md#what-each-identity-does)).

## Turning it on

1. Store the credential the gateway uses upstream. Either a fine-grained PAT limited to the repos agents work on (Contents read & write):

   ```sh
   hangar cred add github-pat --harness git --kind git_token --file ~/.hangar/gh-token
   ```

   or a GitHub App (recommended; see below).
2. Tell hangar where sandboxes reach the gateway, and which credential to use when an environment doesn't attach one:

   | Where | Settings |
   | --- | --- |
   | GKE | `HANGAR_GATEWAY_URL=http://hangar-gateway.hangar.svc:8788` (`deploy/k8s/gateway.yaml` adds the Service and the NetworkPolicy) |
   | Docker on a laptop | `HANGAR_GATEWAY_URL=http://host.docker.internal:8788` and `HANGAR_GATEWAY_HOST=172.17.0.1` (the docker bridge; `0.0.0.0` also works but listens on every interface) |
   | Both | `HANGAR_GATEWAY_CREDENTIAL=github-app` (optional when environments list a `git_token`/`github_app` credential) |

   The gateway listens on its own port (`HANGAR_GATEWAY_PORT`, default 8788) and serves nothing but `/git` and `/healthz`, so a network policy can let sandboxes reach it without reaching the API, dashboard or MCP endpoint. `HANGAR_GATEWAY_PORT=0` serves `/git` on the main port instead; then `/git` is reachable wherever the main port is (through your ingress, too), so a grant copied out of a sandbox works from there for as long as the sandbox lives.
3. Choose which sandboxes use it. With the gateway configured, **jobs, repo builds and env setup's validation always use it**, fetch-only, whatever their environment says, for every repo on a gateway host (`HANGAR_GATEWAY_UPSTREAMS`). **Sessions use it by default** (`HANGAR_GATEWAY_FOR_JOBS=true`, which now governs sessions only). For other sandboxes, or to opt a session environment out, set it on the environment:

   ```yaml
   name: api
   credential_mode: gateway   # or: direct
   credentials: [claude-api, github-app]
   repo: {url: https://github.com/me/api.git}
   ```

4. Run `hangar doctor`: it checks that the gateway answers and that every GitHub App credential can mint a token.

In gateway mode no git secret is written into the sandbox, by any path: `git_token` and `github_app` credentials are not injected, SSH keys are left out (with a warning), and an environment whose `secrets:` maps a git credential into an env var fails to start. A `github_app` credential is never injected in direct mode either. If an environment asks for the gateway and `HANGAR_GATEWAY_URL` isn't set, its sandboxes fail to start rather than fall back to the token.

What changes for agents: in a session, `git push` works for its own branch and fails with a clear message elsewhere; in a job, every push fails with `hangar: this sandbox may fetch but not push` (the job's instructions already say to commit and let hangar deliver). All github.com git traffic goes through the gateway, so fetching *other* repositories over git (a `git+https://github.com/...` dependency, a second clone) is refused; bake those into the image or vendor them.

**The trade-off for `gh`.** In gateway mode `gh` has no token and, in a proxied environment, no host: `github.com` and `api.github.com` aren't allowed hosts any more (the `packages` preset dropped `github`), since GitHub takes writes from anyone's token and so is a way out. `gh` can't be pointed at the gateway either: it speaks to an Enterprise host over HTTPS only. What an agent needs from GitHub it gets another way: hangar opens, comments on and lands PRs for jobs; the agent reads its repo's PRs, reviews, issues and CI through the read-only API above (`curl -sn "$HANGAR_GITHUB_API/…"`). An environment that wants `gh` with a token runs in `credential_mode: direct` with the `github` preset, and accepts that its agent can write anywhere that token reaches.

## Jobs only fetch; hangar delivers

A job's agent never needs to push: it commits and ends its turn, and hangar verifies, scans for secrets, runs the landing checks and pushes. So, following least privilege:

- **Every job's sandbox gets a fetch-only grant.** That is every kind of job: a plain job, its follow-ups and auto-fix rounds, each best-of-N attempt, env setup's goal and setup agents, and the Cloud Doctor's root-cause job (which reads untrusted evidence) and fix job. The gateway answers a fetch-only grant's push with `403` at its first request, before anything reaches GitHub, and audits it (`gw.git.deny`, `fetch-only grant`). No job kind needs a push grant.
- **Delivery pushes as hangar, after the checks.** hangar mints a second grant for the sandbox, pinned to one ref (the job's branch, or an attempt's `<branch>-attempt-<n>`) and one commit (the head it checked), good for 10 minutes and dropped when the push ends (`gateway.grant`, `delivery: true`, with the commit). It runs `git push <commit>:<branch>` in the sandbox with that grant in an HTTP header for the gateway's URL only (from a 0600 file read and removed first, never a command line or `~/.git-credentials`), with the sandbox's hooks off. The gateway refuses anything else with it (another commit, another ref, a tag, a delete) and scans the pack like every push. Upstream the push is made with the gateway's credential (the GitHub App). A copy the agent lifts while the push runs can push only that commit to that branch, which hangar is doing anyway.
- **`credential_mode: direct` doesn't reopen it.** With the gateway configured, a job's sandbox is in gateway mode whatever its environment's `credential_mode` (or `HANGAR_GATEWAY_FOR_JOBS`) says, so no git credential is written into it; a git credential the environment's `secrets:` map is left out, with a warning on the sandbox. That holds for a job with no repo too, and for a repo on a host the gateway doesn't serve (`HANGAR_GATEWAY_UPSTREAMS`), which then gets no grant and no credential at all: a private repo there can't be cloned by a job. Without the gateway (`HANGAR_GATEWAY_URL` unset) a job's sandbox holds the environment's git token and can push wherever it reaches; consistently, landing never lands such a job ([landing.md](../landing.md)) and `hangar doctor` warns (`landing: agent tokens`).
- **Grants from before.** At startup the gateway narrows every grant whose sandbox is a job's, a build's or a validation's to fetch-only (`gateway.narrow`; the token stays, so a job's sandbox kept for follow-ups across an upgrade keeps fetching), and drops delivery grants a restart cut short.
- **What the pin doesn't stop.** It names the commit, not the branch's value before it, and the gateway has no repository to tell a fast-forward from a force push. So an agent that lifts the grant while hangar's push runs could force-push that same commit over commits someone else added to the job's branch since (hangar's own push isn't forced, and fails then). Auto-fix already pauses when someone else pushed to the branch; branch protection on `hangar/**` that refuses force pushes closes it. The commit itself is named by the sandbox's git (`rev-parse`), like everything the in-sandbox checks see; the check the agent can't influence is the gateway's scan of the pack ([below](secret-scanning.md#the-agents-own-pushes)) and, for landing, GitHub's view of the PR.
- **Sessions keep a branch-scoped grant**, since pushing their own branch is what a session's agent is for. Their pushes are scanned in the gateway ([below](secret-scanning.md#the-agents-own-pushes)). To push from a job's work by hand, fork its session (`fork_session`): the fork is a session.
- **Repo builds and env setup's validation** run the repo's `install` (the validation's unmerged and agent-written). Their sandbox gets the fetch-only grant for its clone, the [build secrets](credentials.md#build-secrets) an admin mapped (a build's only, in `install`'s environment), and egress per policy. Nothing else: no credential of the environment (no harness login, no `git_token`, no tool key), none of its `secrets:`, no OIDC cloud token. Without the gateway it gets no git credential at all, so a private repo's build fails and says why.

## Creating a GitHub App

1. GitHub → Settings → Developer settings → GitHub Apps → **New GitHub App** (under the organization if the repos are an org's).
2. Name it (e.g. `hangar-agents`), set any homepage URL, and turn **Webhook** off.
3. Repository permissions: **Contents: Read and write**, **Pull requests: Read and write** (Metadata: read is added automatically). Leave everything else, in particular **Workflows** and **Administration**, at *No access*: without Workflows, GitHub refuses pushes that change `.github/workflows/`.
4. Create it, note the **App ID**, then **Generate a private key** (a `.pem` file downloads).
5. **Install App** on only the repositories agents work on. The installation id is the number at the end of the installation's settings URL; it's optional (hangar looks it up per repo).
6. Store it without printing the key:

   ```sh
   jq -n --arg id 123456 --rawfile key ~/Downloads/hangar-agents.*.private-key.pem \
     '{app_id: $id, private_key: $key}' | hangar cred add github-app --harness git --kind github_app
   rm ~/Downloads/hangar-agents.*.private-key.pem
   ```

The push is made as the App; commits keep the agent's git identity. Set `HANGAR_GITHUB_APP_CREDENTIAL` to the same credential and the App also opens the jobs' PRs and lands them on approval; the gateway then uses it for sandboxes that have no git credential of their own, so `HANGAR_GATEWAY_CREDENTIAL` can stay unset ([landing.md](../landing.md#1-the-github-app)).

## What the gateway does and doesn't cover

- It covers: a prompt-injected agent reading the GitHub token, pushing to `main` or another agent's branch, deleting branches, pushing tags, and using a dead sandbox's grant; and a job's agent (or a build's `install`) pushing anything at all, so `push`-triggered workflows never run on a branch an agent pushed by itself. Grant tokens are bound to one sandbox, stored hashed, compared in constant time, and never accepted by the main API; hangar API tokens are never accepted by the gateway.
- It covers secrets in what the agent pushes, as far as the [push scan](secret-scanning.md#the-agents-own-pushes) recognizes them.
- It doesn't cover: what else is *in* the pushed branch (review the PR; keep secrets out of `push`-triggered workflows), egress filtering (that is the environment's `egress` mode, [egress allowlists](egress.md)), or the model credential, which is still injected unless the environment records through the [model gateway](../model-gateway.md).
- Force pushes to the sandbox's own branch are allowed (agents rebase); force pushes anywhere else are refused like any other update. Keep branch protection on `main` as a second wall.
- Inside the cluster the gateway speaks plain HTTP, so the grant crosses the pod network unencrypted. It is worth little: it dies with the sandbox and reaches one branch of one repo.

