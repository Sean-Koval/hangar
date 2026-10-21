# What hangar gives you

What hangar's control plane holds, what it gives you that a laptop does not, and what it emits. The words are on
[concepts](../concepts.md).

## Inside the control plane

Connect Codex, Claude Code, or any MCP client to hangar. Then start isolated sandboxes from environment templates of
your tools, agent harness, dependencies, skills, and repo. hangar handles the logins and runs agents
in them. It forks a prepared sandbox into N parallel attempts, and tears it all down when you're done.

The same control plane runs on your laptop (Docker) or on a single VM (Docker + gVisor or Kata microVMs). It also runs
on GKE (Agent Sandbox with gVisor or Kata). E2B is supported as a backend too.

<figure class="af-figure af-figure--wide">
--8<-- "control-plane.svg"
<figcaption>Inside the control plane: clients above it, providers below it</figcaption>
</figure>
<figure class="af-figure af-figure--stacked">
--8<-- "control-plane-stacked.svg"
<figcaption>Inside the control plane: clients above it, providers below it</figcaption>
</figure>

## What you get

- **Environments as layered specs.** Base image, then system packages and runtimes (via mise), then dependencies, then
  skills and agent config, then the repo. Layers are ordered for Docker cache reuse, and the image tag is
  content-addressed. Start from a default (`bare`, `python`, `node`, `go`, `polyglot`), or bring a prebuilt image.
- **Pick the agent per sandbox, not per image.** An environment lists the harnesses it allows (Claude Code, Codex, pi,
  Antigravity). Each harness ships once as a read-only *harness pack* mounted at start, so adding agents doesn't
  bloat images. `create_sandbox(harness=...)` applies only that agent's credentials, config and MCP servers.
  Providers that can't mount images get a per-harness image instead.
- **Fast, concurrent sandboxes.** Warm pools keep sandboxes pre-started. Claiming one, cloning the repo and checking
  out a work branch took about 0.6s in testing; four in parallel took about 1.2s.
- **Clone two ways.** Rebuild from the spec, or snapshot a live sandbox and fork it into up to 20 copies. Each copy has
  the same files, installed deps and repo state.
- **Logins for Claude Code, Codex, pi and Antigravity.**
  - API keys (Anthropic Console, OpenAI project, Claude Team or Enterprise keys) work everywhere: sessions, sandboxes,
    jobs, schedules, webhooks and experiments.
  - Subscription logins (Claude Pro or Max, a ChatGPT or Google sign-in) are for a person's own interactive sessions
    and sandboxes only, with a warning. Jobs and other automated work refuse them
    ([subscription logins](../team.md#subscription-logins)). A sign-in runs inside a short-lived sandbox, and you
    finish it in the dashboard terminal.
  - Credentials are encrypted at rest and injected into `~/.hangar/env` or their usual files at claim time.
  - Codex's rotating refresh token is *leased* to one sandbox at a time. It is written back when that sandbox stops,
    so sandboxes never log each other out.
- **Terminals that agents and humans share.** MCP clients drive persistent tmux sessions (`terminal_send` /
  `terminal_read`). From the dashboard you attach to the same session to watch or type along.
- **Agents inside sandboxes.** `run_agent` starts Claude Code, Codex, pi or `agy` (Antigravity) headless on a task,
  with an optional `model`. One orchestrator session can fan work out across the fleet this way.
- **Bearer tokens or OAuth.** `hangar connect <client>` writes the client's config with a token scoped to MCP.
  ChatGPT connectors and other OAuth-only clients use hangar's built-in OAuth 2.1 server. It has discovery, dynamic
  registration, PKCE, and a consent page where you approve with a hangar token. See [connecting clients](../clients.md).
- **A web console.** Jobs, sessions, and the fleet as a list or a board with the decision deck
  ([the web console](../console.md)). It also has live terminals, the environment editor with streaming builds,
  credentials and sign-in, snapshots, MCP connect snippets with token management, and an audit log.
- **Traces, metrics and cost.** OpenTelemetry traces from the control plane when `OTEL_*` is set; Claude Code's and
  Codex's own spans join them. Prometheus `/metrics` for a scrape-only token. A usage view of spend by day, repo and
  requester. See [observability](../observability.md).

## What hangar emits

hangar is the run plane of the agent factory: it runs the work and emits data about it, and sister tools interpret
it. Every turn records a
[manifest](../specs/0014-sister-tool-interfaces.md#2-the-effective-harness-manifest-hangarmanifestv1) of the
configuration it ran with. [Trace export](../api/traces.md) gives each run as OTLP on the OpenTelemetry GenAI
conventions. [Run capture](../api/outbox.md) writes every finished run to a bucket once.

[Experiment jobs](../jobs/experiments.md) run variants against graders. flightdeck reads all of these to evaluate
harnesses. Live metrics, traces and cost are in [activity](../activity.md) and [observability](../observability.md).

Tower, the Govern plane, is to decide approvals once hangar asks it. Its decision API is built
([spec 0015](../specs/0015-governance-decision-point.md)); hangar's wiring is on the
[roadmap](https://sean-koval.github.io/agent-factory/roadmap/#p2-adopt-and-capture).
