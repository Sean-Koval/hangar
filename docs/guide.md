# Getting started

Run hangar on a laptop in five minutes and give it a first task. This is the one Quickstart; the
[README](https://github.com/Sean-Koval/hangar/blob/main/README.md) repeats its five commands.
[Concepts](concepts.md) explains the words (environment, harness, sandbox, session, job) and what hangar gives you.
The [changelog](https://github.com/Sean-Koval/hangar/blob/main/CHANGELOG.md) says what each release added.

## Quickstart

Five minutes on a laptop. You need [Docker](https://docs.docker.com/get-started/get-docker/) (Docker Desktop, OrbStack
or Colima on macOS) and [uv](https://docs.astral.sh/uv/getting-started/installation/) (Python 3.12+ is managed by uv).

```bash
git clone https://github.com/Sean-Koval/hangar.git && cd hangar
uv sync --all-extras
uv run hangar up        # server, CLI login, the claude-python environment, MCP clients
uv run hangar cred add claude-api --harness claude-code --kind api_key --file anthropic-key.txt
uv run hangar doctor    # every check passes, or names its fix
```

`hangar up` does five things:

1. checks that Docker is running;
2. starts the control plane in the background (log: `~/.hangar/server.log`);
3. logs the CLI in;
4. builds the `claude-python` environment (the first build downloads tools, so allow a few minutes);
5. offers to connect each MCP client it finds.

Running it again is safe: it reuses the running server, the saved login and the built images. `--env NAME` builds
other environments, `--no-build` skips building, and `--connect codex` connects a client without asking. `hangar down`
stops the server.

Open the **console at [localhost:8787](http://localhost:8787)** to see your fleet, credentials, environments and live
terminals ([the web console](console.md)). Local state is in `~/.hangar`. If a doctor check fails, follow its fix;
`uv run hangar doctor --fix` applies the supported repairs.

To look around first without Docker or a key, run the demo hangar the front page's recording uses:
`uv run python docs/assets/front/demo.py`. It serves the console on :8787 and prints its token. Then run
`uv run python docs/assets/front/demo.py ticket` in another terminal to label issue #42.

### Credentials

Use **Credentials** in the console, or store a key from the CLI as above. Use an API key (an Anthropic Console key, an
OpenAI project key, a Claude Team or Enterprise key). Set a spend limit on it in the vendor's console; hangar's own job
and daily budgets cap spend inside hangar. For Codex, and for private repositories:

```bash
uv run hangar cred add codex-api --harness codex --kind api_key --file openai-key.txt
uv run hangar env build codex-node
uv run hangar cred add gh --harness git --kind git_token --value "$(gh auth token)"
```

Choose the credential when creating a sandbox, or list its name in your environment's `credentials` field. Storing a
key alone does not attach it to every sandbox.

Jobs, schedules, webhooks and experiments run on API keys only. A personal subscription login (Claude Pro or Max, a
ChatGPT sign-in) is for your own interactive sessions, with a warning: see
[subscription logins](team.md#subscription-logins).

### Connect your client

If `hangar up` did not connect your client, run `uv run hangar connect claude-code` (or `codex`, `cursor`, `vscode`,
`gemini`, `claude-desktop`). It adds hangar to the client's own config file, next to your other servers. Its fresh
token can reach the MCP endpoints and nothing else:

- `hangar` (`/mcp`: jobs, sessions, schedules);
- for Claude Code and Codex, `hangar-sandbox` (`/mcp/sandbox`: create_sandbox, exec, files, terminals;
  [the two servers](mcp.md#two-servers)).

Start a new client session so it reads the updated configuration. `hangar connect --list` shows what's connected;
[connecting clients](clients.md) covers all options, remote connections and OAuth.

### First task

In your connected MCP client, ask:

> Use hangar to create a claude-python sandbox for https://github.com/YOUR-USER/YOUR-REPO.git. Use the claude-api credential and a new work branch. Run Claude Code to explain the project structure and how to run its tests. Report the sandbox name and findings.

Replace the repository URL with your own. Start with a public repo, or add a Git credential for private repository
access. The same from the CLI:

```bash
uv run hangar sb create claude-python \
  --name first-task \
  --repo https://github.com/YOUR-USER/YOUR-REPO.git \
  --work-branch hangar/first-task \
  --cred claude-api

uv run hangar sb agent first-task \
  "Explain the project structure and how to run its tests."
```

Open the sandbox in the console to follow the work in its live terminal. When you finish reviewing it, remove it with
`uv run hangar sb rm first-task`. For Codex, use `codex-node` and `--cred codex-api` instead.

## Next steps

| To | Read |
| --- | --- |
| Delegate from your client with the skill, run parallel attempts, write your own environment | [After the first task](guide/after-the-first-task.md) |
| Run it on GKE, then for a team | [From a laptop to GKE and a team](guide/production.md) |
| Choose where sandboxes run, and see what has been proven live | [Where it runs](guide/where-it-runs.md) |
| Change settings, or work on hangar itself | [Configuration and development](guide/configuration.md) |
| Connect Codex, Claude Code, Cursor or another client | [Connecting clients](clients.md) · [Using MCP](mcp.md) |
| Hand a task to a cloud agent and get a PR back | [Jobs](jobs/README.md) |
| Keep working with an agent across days | [Sessions](sessions.md) |
| Write an environment for your repo | [Environments](environments.md#environment-specs) |
| Take work from GitHub, Jira or Slack | [Jobs: trigger from GitHub](jobs/from-github-slack-jira.md#4-trigger-from-github) · [Intake](intake.md) · [Slack](slack.md) |
| Let an approval land the PR | [Landing](landing.md) |
| Put a team on it, repo by repo | [The onboarding runbook](runbooks/onboarding.md) |
| Upgrade a running hangar | [Upgrading](upgrading.md) |
| Run it day to day, and when something breaks | [Runbooks](runbooks/README.md) · [Troubleshooting](troubleshooting.md) |

## Moved from this page

| Section | Now on |
| --- | --- |
| <span id="use-the-hangar-skill"></span>Use the hangar skill | [After the first task](guide/after-the-first-task.md#use-the-hangar-skill) |
| <span id="parallel-attempts"></span>Parallel attempts | [After the first task](guide/after-the-first-task.md#parallel-attempts) |
| <span id="take-work-from-github"></span>Take work from GitHub | [After the first task](guide/after-the-first-task.md#take-work-from-github) |
| <span id="your-own-environment"></span>Your own environment | [After the first task](guide/after-the-first-task.md#your-own-environment) |
| <span id="everyday-commands"></span>Everyday commands | [After the first task](guide/after-the-first-task.md#everyday-commands) |
| <span id="production-gke"></span>Production (GKE) | [From a laptop to GKE and a team](guide/production.md#production-gke) |
| <span id="a-team-on-one-hangar"></span>A team on one hangar | [From a laptop to GKE and a team](guide/production.md#a-team-on-one-hangar) |
| <span id="where-it-runs"></span>Where it runs | [Where it runs](guide/where-it-runs.md) |
| <span id="known-limits"></span>Known limits | [Where it runs: known limits](guide/where-it-runs.md#known-limits) |
| <span id="configuration"></span>Configuration | [Configuration and development](guide/configuration.md#configuration) |
| <span id="development"></span>Development | [Configuration and development](guide/configuration.md#development) |
| <span id="mcp-tools"></span>MCP tools | [Using MCP: the tools](mcp.md#the-tools) |
| <span id="environment-specs"></span>Environment specs | [Environments: environment specs](environments.md#environment-specs) |
