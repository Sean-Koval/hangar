# Getting started

Run hangar on a laptop in five minutes, then on GKE, then for a team. [Concepts](concepts.md) explains the words
(environment, harness, sandbox, session, job) and what hangar gives you; the [README](https://github.com/Sean-Koval/hangar/blob/main/README.md)
has the short version, and the [changelog](https://github.com/Sean-Koval/hangar/blob/main/CHANGELOG.md) what each release added.

## Quickstart (laptop, 5 minutes)

Requires Docker (Docker Desktop, OrbStack or Colima on macOS) and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/Sean-Koval/hangar && cd hangar
uv sync --all-extras
uv run hangar up
```

`hangar up` does five things:

1. checks that Docker is running;
2. starts the control plane in the background (log: `~/.hangar/server.log`);
3. logs the CLI in;
4. builds the `claude-python` environment;
5. offers to connect each MCP client it finds.

Running it again is safe: it reuses the running server, the saved login and the built images. `--env NAME` builds other environments, `--no-build` skips building, and `--connect codex` connects a client without asking. `hangar down` stops the server.

Then:

1. **Add credentials.** Use the dashboard at http://localhost:8787 (under **Credentials**), or:
   ```bash
   uv run hangar cred add claude-api --harness claude-code --kind api_key --file anthropic-key.txt
   uv run hangar cred add codex-api --harness codex --kind api_key --file openai-key.txt
   uv run hangar cred add gh --harness git --kind git_token --value "$(gh auth token)"
   ```
2. **Connect an MCP client**, if `hangar up` didn't. Run `uv run hangar connect claude-code` (or `codex`, `cursor`, `vscode`, `gemini`, `claude-desktop`). It adds hangar to the client's own config file, next to your other servers, with a fresh token that can reach the MCP endpoints and nothing else: `hangar` (`/mcp`: jobs, sessions, schedules) and, for Claude Code and Codex, `hangar-sandbox` (`/mcp/sandbox`: create_sandbox, exec, files, terminals; [the two servers](mcp.md#two-servers)). `hangar connect --list` shows what's connected. Details are in [docs/clients.md](clients.md).
3. **Ask your client to do something.** For example: *"Create 3 claude-python sandboxes on github.com/me/api, each on its own branch, and run the agent in each on 'make the flaky test in tests/test_sync.py deterministic'. Report which attempt passes."*
4. **Optional: take work from GitHub.** `uv run hangar setup github` asks for a repo and a token and stores them. It then registers the repo's webhook and sends a test delivery. GitHub can't reach `localhost`, so on a laptop pass `--webhook-url` with a tunnel URL. See [docs/jobs.md](jobs.md#4-trigger-from-github).

`uv run hangar doctor` checks the setup, and `hangar doctor --fix` applies the fixes it can make on its own.

## Production (GKE)

```bash
cp deploy/terraform/gke/terraform.tfvars.example deploy/terraform/gke/terraform.tfvars   # set project_id, region
uv run hangar up --gke --dry-run          # the terraform plan; nothing changes
uv run hangar up --gke                    # asks, then applies that plan, runs deploy/gke/up.sh and logs in
hangar doctor --fix                       # what's missing, and the fixes it can apply
hangar setup github                       # token, webhook secret, repo webhook, settings, test delivery
hangar connect claude-code                # and any other MCP client
```

`hangar up --gke` never applies a plan that destroys or replaces anything unless you pass `--allow-destroy`, and without a terminal it applies nothing unless you pass `--yes`. The steps it runs are the ones in [docs/gke.md](gke.md), which you can also run by hand. `up.sh` points `HANGAR_SESSION_STORE` at the checkpoint bucket terraform made, on a first install. If the certificate isn't Active yet, `hangar up --gke` prints the `hangar login` command to run once it is.

Settings you change with `hangar setup` or `hangar config set` are stored in hangar's database, on its volume. They survive redeploys and need no `kubectl`. The full walkthrough is in [docs/gke.md](gke.md).

## A team on one hangar

To put a team on hangar step by step (2 engineers on 1 repo, then 6 on 3), follow [the onboarding runbook](runbooks/onboarding.md); it ends with `hangar repo check` green for each repo. The [runbooks](runbooks/README.md) also cover the Friday harness review, run capture and governance break-glass.

An admin adds people; each gets a login token, connects their own clients and stores their own agent login:

```sh
hangar person add alice                       # a member; prints her login token once
hangar person add bob --role admin
hangar person ls                              # owner is the admin your existing tokens belong to
# alice, on her laptop:
hangar login https://hangar.example           # paste the token
hangar connect claude-code                    # an mcp token of her own
hangar cred add claude-alice --harness claude-code --kind api_key --file key.txt
```

Members see and act on their own sandboxes, sessions and jobs; admins on everyone's. Alice's sessions and jobs use her own API key; teammates without one of their own use a team API key (`hangar cred add NAME --team --kind api_key`). Jobs and other automated work never use a subscription login ([team.md](team.md#subscription-logins)). A job that waits for approval is approved by an admin or a repo policy approver, from `hangar job approve` or a login connection, never by its own person or requester ([upgrading.md](upgrading.md#the-safe-release-120132)). `hangar cred owner NAME PERSON|team` hands a credential to someone or back to the team, and `hangar person rm` revokes someone's tokens. See [security.md](security.md#people).

## Next steps

| To | Read |
| --- | --- |
| Connect Codex, Claude Code, Cursor or another client | [Connecting clients](clients.md) · [Using MCP](mcp.md) |
| Hand a task to a cloud agent and get a PR back | [Jobs](jobs.md) |
| Keep working with an agent across days | [Sessions](sessions.md) |
| Write an environment for your repo | [Environments](environments.md#environment-specs) |
| Take work from GitHub, Jira or Slack | [Jobs: trigger from GitHub](jobs.md#4-trigger-from-github) · [Intake](intake.md) · [Slack](slack.md) |
| Let an approval land the PR | [Landing](landing.md) |
| Put a team on it, repo by repo | [The onboarding runbook](runbooks/onboarding.md) |
| Upgrade a running hangar | [Upgrading](upgrading.md) |
| Run it day to day, and when something breaks | [Runbooks](runbooks/README.md) · [Troubleshooting](troubleshooting.md) |

## MCP tools

Moved to [Using MCP: the tools](mcp.md#the-tools).

## Environment specs

Moved to [Environments: environment specs](environments.md#environment-specs).

## Where it runs

| Where | How | Isolation |
| --- | --- | --- |
| Laptop | `hangar serve`, or `docker compose up` in `deploy/compose` | runc containers |
| Your Linux or WSL2 box | `sudo bash deploy/local/kata-install.sh`, then `HANGAR_DOCKER_RUNTIME=kata hangar serve` | Kata microVMs |
| Single Linux VM | `deploy/terraform/single-vm`: a GCE VM with nested virt, running Compose | gVisor (`runsc`) or Kata microVMs |
| GKE | `deploy/terraform/gke` + `deploy/k8s` | Agent Sandbox with gVisor; optional Kata pool |
| E2B | `HANGAR_ENABLED_PROVIDERS='["docker","e2b"]'` with `HANGAR_E2B_API_KEY` | Firecracker microVMs |

You can enable several providers on one control plane and pick one per sandbox. See [docs/gke.md](gke.md) for the cluster walkthrough.

On Docker and Kubernetes, hangar starts a small daemon in each sandbox, [hangar-envd](envd.md), and runs commands, file transfers and terminals through it instead of `docker exec` or the Kubernetes exec API: an `exec` takes about 7 ms instead of 70 on Docker. It speaks E2B's envd protocol. Build it with `envd/build.sh` (the control-plane image includes it); without it, or with `HANGAR_ENVD=off`, hangar uses exec as before.

Because of that, hangar also answers E2B's API: the e2b SDKs, and the agent frameworks built on them, run in your hangar sandboxes after you change `E2B_API_KEY` and a URL. See [docs/e2b.md](e2b.md).

## Configuration

Everything is set through `HANGAR_*` settings (see `src/hangar/config.py`), which come from two places:

- **The environment.** This includes `.env` and, on GKE, the `hangar` and `hangar-site` config maps. It is read at startup, and it wins.
- **Hangar's database.** Edit it with `hangar config set KEY VALUE`, `get`, `unset` and `list` (admin tokens only). Values are checked against each setting's type and survive redeploys. Most apply at once; `hangar config set` tells you when one needs a restart. Secrets are refused: they go in the vault (`hangar cred add`), and settings name the credential.

`hangar config list` shows each value and where it came from. The common settings:

| Variable | Purpose |
| --- | --- |
| `HANGAR_ADMIN_TOKEN` | Admin bearer token. If unset, a bootstrap token is generated once. |
| `HANGAR_SECRET_KEY` | Fernet key for the credential vault. If unset, a key file is created in the data dir. |
| `HANGAR_DATA_DIR` | State: SQLite DB, key, workspace snapshots. Default `~/.hangar`. |
| `HANGAR_ENV_DIRS` | JSON list of spec directories. Default `["envs"]`. |
| `HANGAR_DEFAULT_PROVIDER` / `HANGAR_ENABLED_PROVIDERS` | `docker`, `kubernetes`, `e2b` |
| `HANGAR_DOCKER_RUNTIME` | `runsc` (gVisor) or `kata` on Linux hosts |
| `HANGAR_K8S_RUNTIME_CLASS` | `gvisor` (default), `kata-qemu-runtime-rs` (Kata microVMs), or empty for runc |
| `HANGAR_REGISTRY` | Where cluster/E2B images are pushed, e.g. Artifact Registry |
| `HANGAR_EXTRA_CA_BUNDLE` | PEM file trusted inside every image, for corporate TLS-inspecting proxies |
| `HANGAR_DEFAULT_TTL_MINUTES` / `HANGAR_MAX_SANDBOXES` | Fleet guardrails |
| `HANGAR_E2B_GCP_SERVICE_ACCOUNT_JSON` / `HANGAR_E2B_AWS_*` / `HANGAR_E2B_REGISTRY_USERNAME` | How E2B pulls images from a private `HANGAR_REGISTRY` |

`hangar gc` lists containers left behind by crashed creates or other state directories, and `hangar gc --yes` removes them.

## Known limits

- **Forks on Kubernetes** copy `/workspace` as a tarball; GKE can only restore a sandbox from its own snapshots. With Pod snapshots enabled, Kubernetes sandboxes can be paused and resumed with memory intact (see [docs/gke.md](gke.md)). On Docker and E2B, forks are full snapshots.
- **What has run for real:**
  - **Docker provider:** end to end in CI.
  - **Kubernetes provider:** end to end over MCP against the real Agent Sandbox v1.0.6 controller on kind, with the runc runtime. This covered builds pushed to a registry, claims, exec, 300 KB file writes, terminals, TTL extension, forks and teardown.
  - **Kata microVMs (Docker provider):** end to end on WSL2. Each sandbox is a VM with its own kernel; it starts in about 2 s, and 4 start in parallel in 2.2 s. See [docs/local-microvms.md](local-microvms.md).
  - **GKE with the Agent Sandbox add-on and gVisor:** end to end, with the control plane in the cluster behind a Google-managed certificate. A remote MCP client created two sandboxes from the warm pool over HTTPS in 3.1 s. Each had the repo cloned on its own branch, a Claude Code token and a fine-grained GitHub token (that run predates [subscription logins being interactive-only](team.md#subscription-logins); fleets use API keys). Claude Code ran headless in both, made a change with tests, and opened a PR that passed CI ([#13](https://github.com/Sean-Koval/hangar/pull/13), [#14](https://github.com/Sean-Koval/hangar/pull/14)). `scripts/agent_fleet_demo.py` reproduces the run.
  - **Not yet run live:** Kata on GKE (the add-on only admits `gvisor` and `microvm`; see [docs/gke.md](gke.md)), Pod snapshots and E2B. They're covered by unit tests against fake APIs.
- **Antigravity (`agy`) subscription login** stores OAuth in the OS keyring, which headless containers may lack. Use a `GEMINI_API_KEY` credential for fleets.
- **Spend caps live in two places.** Set a workspace spend limit on each API key in the vendor's console (Anthropic Console, OpenAI project limits); hangar's job and daily budgets cap spend inside hangar.
- **The control plane is a single replica** with SQLite state, which is right for a personal or team instance.

## Development

```bash
uv sync --all-extras
uv run pytest -n 8                             # unit + API tests (fake provider), in parallel
scripts/ci/select_tests.py --base origin/main  # just the tests your branch can break (docs/ci.md)
HANGAR_E2E=1 uv run pytest tests/test_e2e_docker.py   # real Docker, driven over MCP
uv run ruff check src tests
uv run hangar serve --reload
```

The docs site builds the way `.github/workflows/docs.yml` builds it (Zensical, from `mkdocs.yml`):

```bash
pip install zensical==0.0.71
zensical build --strict    # into site/; a broken link or anchor fails it, as in CI
zensical serve             # http://localhost:8000, rebuilt on save
```

Code map: `fleet.py` (lifecycle), `envspec.py` (specs to Dockerfiles), `broker.py` + `vault.py` (credentials), `harnesses.py` (per-agent install, auth, run), `mcp_server.py`, `app.py` (REST, websockets, dashboard), `providers/` (docker, kubernetes, e2b), `ui/` (no-build dashboard).
