# The GKE egress default

What a sandbox on GKE can reach when its environment doesn't set `egress.mode`, and what to do when a job needs more. Part of [egress allowlists](egress.md).

The Kubernetes manifests set `HANGAR_EGRESS_DEFAULT: "allowlist"` (`deploy/k8s/config.yaml` and `deploy/kustomize/base/config.yaml`). Every environment without its own `egress.mode` sends its sandboxes through hangar's [egress proxy](egress.md). Docker on a laptop keeps `open`, since Docker Desktop can't run a proxied sandbox.

## What gets through

| Reached | How |
| --- | --- |
| The harness's model API and login refresh | always, per harness: Anthropic, OpenAI, Google (Vertex AI's global endpoint included), OpenRouter for pi |
| pip, uv, npm, yarn and Go modules | hangar's read-only [package mirror](package-mirrors.md) on the gateway |
| Rust crates | the sparse index and download host (`crates` preset) |
| The repo: clone, push, PRs, issues and CI logs | the [git gateway](git-gateway.md) and its read-only API (`$HANGAR_GITHUB_API`) |
| The environment's remote MCP servers | when a shipped file or an admin configured them |
| A cloud's token exchange | when the environment sets `cloud:` |
| Anything else | after an owner approves the agent's request |

Environment images are built by Cloud Build, outside any sandbox, so `tools` and `dependencies` install as before. A repo's `install` (its [build](../environments/builds.md)) runs in a sandbox, so it gets the same allowlist.

## What gets refused

These are the common ones:

- **GitHub itself:** `gh`, `pip install git+https://github.com/…`, release downloads and raw.githubusercontent.com. Git goes through the gateway.
- **Toolchains fetched at run time:** `mise install`, rustup, nvm, nodejs.org and Playwright's browsers. Bake them into the image instead.
- **Other package sources:** Maven and Gradle, Poetry's own sources, conda, Hugging Face, and Docker Hub outside the [heavy tier](../environments/heavy-tier.md)'s mirror.
- **Lockfile URLs:** `uv sync --frozen` downloads from files.pythonhosted.org, which is closed. A plain `uv sync` resolves again through the mirror.
- **Uncached Go versions:** the mirror serves only what proxy.golang.org already holds.
- **Web pages:** curl to a docs site, and Claude Code's WebFetch, which fetches from the sandbox. Web search runs on the model's side and works.
- **Regional Vertex AI:** `<region>-aiplatform.googleapis.com` goes in `allow`.

## Seeing what a job was refused

Each refusal is recorded with its host:

- **The session's and job's events** show `egress denied: <host>:<port>` with the request command.
- **The agent** gets a 403 naming `hangar-agent env request egress <host>`.
- **A failed job's flight record** names its cause as `egress` and the host refused most.
- **`hangar doctor`** lists the hosts refused in the last 24 hours, by environment, under "egress denials (24h)". Its "egress default" check names the environments the default decides.
- **The [failure doctor](../doctor.md)** files a request for the admins when agents keep being refused the same host.
- **`hangar status`** has the same 24 hours by host. The audit log has every `egress.deny`, and the metric is `hangar_egress_connections{decision="deny"}`.

## Letting a job through

Pick the narrowest that works:

1. **Approve the request** for the session, or for the whole environment as an admin.
2. **Add the host** to the environment's list: `egress: {allow: [docs.example.com]}`. Hosts add up along `extends`.
3. **Open the environment** with `egress: {mode: open}` and a comment saying why. Its sandboxes reach the internet directly, as before.
4. **Open everything** by setting `HANGAR_EGRESS_DEFAULT: "open"` in your overlay's `hangar-site` ConfigMap. A ConfigMap key wins over `hangar config set`.

Intake jobs (GitHub, Slack, Jira, webhooks) get `allowlist` under `enforce` whatever you choose here ([intake posture](front-doors.md#intake-posture)).
