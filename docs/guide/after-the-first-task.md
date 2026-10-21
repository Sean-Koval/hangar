# After the first task

Your first task ran ([getting started](../guide.md#first-task)). This page takes the next steps on a laptop. Delegate
from your own agent, run attempts in parallel, take work from GitHub and write an environment for your repo.

## Use the hangar skill

The [hangar skill](https://github.com/Sean-Koval/hangar/blob/main/skills/hangar/SKILL.md) teaches your calling agent to
turn a task or ticket into a cloud job or resumable session. It follows progress, handles questions, and reports
changes with verification evidence.

Copy the `skills/hangar/` folder into your client's configured skills directory, and reload its skills. For clients
using the shared convention, that is `~/.agents/skills/hangar/`. Connect that client to your hangar server
([connect your client](../guide.md#connect-your-client)); installing the skill alone does not connect MCP. For
example:

> Use the hangar skill to work on ticket APP-42 in my repo. Use our configured Python environment, run the project's tests, and return a draft PR with the results.

This skill runs in the client that delegates work. The separate
[repo-conventions skill](https://github.com/Sean-Koval/hangar/blob/main/envs/skills/repo-conventions/SKILL.md) guides
coding agents working inside sandboxes.

## Parallel attempts

Once a single task works, ask your MCP client:

> Create three claude-python sandboxes for my repo, each on its own work branch, using claude-api. Ask each agent to fix the same flaky test, run the project's tests, and report the changes and results. Let me review the attempts before removing the sandboxes.

To start from work you have already prepared, use `snapshot_sandbox` and `fork_sandbox`. Snapshot behavior varies by
provider; see [where it runs](where-it-runs.md).

For a task that should produce a draft PR, use a [job](../jobs/README.md): jobs support verification commands,
follow-ups, budgets and GitHub triggers. [Sessions](../sessions.md) preserve a conversation and repository state
across turns.

## Take work from GitHub

`uv run hangar setup github` asks for a repo and a token and stores them. It then registers the repo's webhook and
sends a test delivery. GitHub can't reach `localhost`, so on a laptop pass `--webhook-url` with a tunnel URL. See
[jobs: trigger from GitHub](../jobs/from-github-slack-jira.md#4-trigger-from-github).

## Your own environment

Save this as `envs/my-service.yaml`:

```yaml
name: my-service
extends: python
harnesses: [claude-code]
default_harness: claude-code
credentials: [claude-api]
repo:
  url: https://github.com/YOUR-USER/YOUR-REPO.git
  branch: main
  setup: uv sync
agent_config:
  instructions: |
    Run the project's tests before finishing.
resources:
  cpus: 2
  memory_gb: 4
warm_pool: 1
```

Replace the repo URL, branch and setup command with your project's values, then build it with
`uv run hangar env build my-service`. Ask your client to create a sandbox from `my-service`; it uses the configured
repo and credential. `warm_pool: 1` keeps one sandbox pre-started for faster claims.

Built-in foundations include `bare`, `python`, `node`, `go` and `polyglot`. [Environments](../environments.md) covers
tools, skills, secrets, MCP servers and multiple agent choices.

## Everyday commands

Run these from the hangar checkout:

| What you want | Command |
| --- | --- |
| List environments | `uv run hangar env list` |
| List sandboxes | `uv run hangar sb ls` |
| Run a command in a sandbox | `uv run hangar sb exec first-task "git status"` |
| What waits on you | `uv run hangar inbox` |
| Check client connections | `uv run hangar connect --list` |
| Inspect settings and their sources | `uv run hangar config list` |
| Freeze a good turn into a harness directory ([harness locks](../harness-lock.md)) | `uv run hangar harness freeze job-1a2b3c4d --turn 1 --out ../harnesses/team-default` |
| Check a harness directory against its lock | `uv run hangar harness check ../harnesses/team-default` |
| Check setup | `uv run hangar doctor` |
| Stop the local server | `uv run hangar down` |

For remote instances, run `uv run hangar login https://YOUR-HANGAR-HOST` once; fleet commands then use that server.
Next: [from a laptop to GKE and a team](production.md).
