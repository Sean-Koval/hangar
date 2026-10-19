# 0012 Cutting edge: what to adopt next

Status: partly built (2026-10-15): items 1, 3–7 and 15 shipped; the rest isn't scheduled. Was: proposal. · Date: 2026-10-13 · Builds on: 0003 (gateway, egress), 0004 (performance, routing), 0006 (environments), 0007 (policy, experiments, learnings), 0008 (forks, swarms), 0010 (sessions), 0011 (factory roadmap)

The goal is for hangar to be the best way to run coding agents on your own infrastructure, and to give MCP clients cloud sandboxes, with easy setup and current methods. This spec compares hangar with the cloud coding agents and sandbox platforms as of October 2026. It names what to adopt, in what order, and how to tell whether each change worked.

Every external claim carries a source tag such as [S12], resolved in §10. Claims I could not check against a primary source are marked *unverified*, and §9 lists them. Numbers marked *measured* come from experiments on this machine (§4.3), whose scripts are in [`docs/research/0012/`](https://github.com/Sean-Koval/hangar/tree/main/docs/research/0012).

## 1. Summary: the decisions

1. **Vendors now run their agent loops on customer compute, so hangar should be the compute.** By October 2026 the major vendors all ship this:
   - Claude Code cloud sessions run on self-hosted runners [S3];
   - Cursor runs cloud agents on self-hosted workers [S19];
   - Devin runs on Outposts [S22];
   - Ona runs on runners in your VPC [S25];
   - Warp Oz offers self-hosted environments [S26].

   Each vendor supplies a runner binary that dials out. It needs a machine with the repo's toolchain, and a provisioner to create one per session. That provisioner is exactly what hangar's environments, warm pools, gateway and egress policy already are. **Item 2 makes hangar the provisioner, starting with Claude Code's `spawn-runner` hook.** Engineers then get claude.ai, the mobile app, `--teleport` and auto-fix on hangar's sandboxes, without hangar rebuilding any of that UX.
2. **Hangar's protocol surface is current; its gaps are in the agent loop.** Hangar already serves MCP 2026-07-28 with tasks and MRTR elicitation, and A2A 1.0 with 0.3 compatibility (docs/mcp.md, docs/a2a.md). That puts it ahead of most clients. What it lacks is what the hosted products do inside a session:
   - steering a running turn;
   - environments that rebuild themselves;
   - a verify gate inside the agent's loop;
   - a PR loop that fixes CI failures and review comments;
   - egress levels, and credentials injected from outside the sandbox;
   - cache-aware session handling.
3. **Prompt caching is the cheapest cost and latency lever, and today hangar works against it.** Hangar suspends idle sessions after 30 minutes, and agents pause on `needs_input` for human-scale gaps. Claude Code with an API key uses a 5-minute TTL [S6], and GPT-5.6+ caches live for 30 minutes [S15]. On Opus 5.5, re-reading a 150k-token conversation after the cache expires costs $0.75; reading it from cache costs $0.03 [S11]. Item 1 sets a TTL policy, never changes model mid-session, and staggers best-of-N forks.
4. **Memory fork is now cheap on the stack hangar already uses.** In a local test, a gVisor checkpoint restored into two independent sandboxes from one image. A 256 MB process restored in 0.3–1.25 s, against a 1.4–2.2 s cold start (*measured*, §4.3). GKE Pod Snapshots went GA in May 2026 [S41]. The docs show a snapshot restored into a *new* Pod named by annotation [S40], which may allow forks on GKE, not just pause and resume. Item 8 verifies this live before anything is built on it.
5. **Providers.** Keep Docker, GKE and E2B. Add the vendor-runner pools from decision 1. Don't add Daytona, Modal, Vercel, Cloudflare or Morph as providers now: each is an E2B-like hosted VM platform, and hangar's value is the owner's own cluster (§4.2). Revisit this if an owner asks for one, through the OpenAI Agents SDK's sandbox interface (item 14), which already speaks to seven of them [S17].

## 2. Method

- Primary sources only: vendor docs, specs, release notes and repos, fetched on 2026-10-13. Secondary sources are used only where noted.
- Local CLIs were checked for flags this spec relies on: Claude Code 2.1.296, Codex 0.162.1 and pi 1.1.0, all in the overnight scratchpad. The Claude Code checks covered `self-hosted-runner`, `orchestrator`, `--input-format stream-json`, `--exclude-dynamic-system-prompt-sections` and `CLAUDE_CODE_PROMPT_CACHE_TTL`; the Codex checks covered `codex queue` and `codex app-server`.
- Two experiments on this machine: gVisor checkpoint/restore and fork, and dependency install with and without a warm cache (§4.3). This is a shared 2-vCPU, 8 GB VM with other agents running, so treat the timings as relative, not absolute.
- What hangar has today was read from `docs/*.md`, specs 0001–0011, and `src/hangar` at `origin/overnight/perf`.

## 3. Cloud coding agents

### 3.1 What each does well

| Product | Environment setup and caching | Snapshot / fork | Steering and review | Secrets and network | Runs on your compute |
| --- | --- | --- | --- | --- | --- |
| **Claude Code cloud sessions** [S1][S2] | Setup script, then a filesystem snapshot reused by later sessions. Rebuilt when the script or allowed hosts change, and after about 7 days. Not cached if setup takes over about 5 minutes. SessionStart hooks run per session. 4 vCPU, 16 GB, 30 GB disk. | An idle VM pauses with its files and resumes on the next message. Conversation restored after expiry. `--teleport` pulls a session to a local terminal. | Queue messages mid-turn. Diff view with inline comments sent to Claude. Auto-fix watches a PR for CI failures and review comments, and asks when a comment is ambiguous. | Network levels None, Trusted and Custom. A GitHub proxy keeps the token out of the VM and allows only branch pushes. On Pro/Max, an agent proxy injects API keys for listed hosts. | Yes. Self-hosted environments (public beta, Team/Enterprise): runners poll a queue, and an orchestrator calls your `spawn-runner` hook per session [S3][S5]. |
| **OpenAI Codex cloud** [S12][S13] | Setup script plus a maintenance script on cache resume. Container cache up to 12 h, invalidated when the scripts, variables or secrets change. The new Codex Cloud keeps VM state up to 7 days after the last turn. | Each task gets its own workspace. | `turn/steer` and `thread/fork` in app-server [S14]. `codex queue` (local CLI). | Secrets reach setup scripts only. Agent internet access is off by default. | "Connecting private services" is documented, but the mechanism was *unverified* this session. |
| **Cursor cloud agents** [S18][S19] | Agent-led setup, a saved snapshot, or a Dockerfile in `.cursor/environment.json`. Background builds. | Snapshot of the base environment. | Team follow-ups. Take over the agent's desktop. Screenshot and video artifacts. | Secrets injected at start. Admins can restrict egress domains. | Yes. `agent worker start` dials out. A k8s template runs `agent worker controller --spawn`, one pod per request, with warm pods via `--warm-idle`. |
| **Google Jules** [S20] | Per-repo setup script, then "Run and Snapshot". The snapshot is reused for later tasks. | Snapshot. | n/a | Not documented. | No |
| **Devin** [S21][S22] | YAML blueprints (`initialize`, `maintenance`, `knowledge`) build one bootable snapshot per org. Rebuilt on change and about every 24 h. Pin to any build from the last 7 days. | Each session boots a fresh copy of the snapshot. | n/a | Secrets are removed before the snapshot is saved. | Yes. Outposts: `devin worker start --outpost=…` on your machines, with an open-source k8s operator. Partners: Namespace, Modal, Daytona, E2B, Cloudflare. Vercel has its own guide. |
| **GitHub Copilot cloud agent** [S23] | `copilot-setup-steps.yml`, an Actions job. Larger or self-hosted runners via ARC. | `snapshot:` key, undocumented. | PR-native. | Built-in firewall, which doesn't work on self-hosted runners. | Yes, through Actions runners. |
| **Factory** [S24] | Droid Computers: persistent machines, managed or your own. Managed ones auto-pause when idle. | Persistence, no snapshots. | Slack, app, CLI. | Relay; no public ports. | Yes (BYOM) |
| **Ona** (ex-Gitpod) [S25] | Dev Container environments with prebuilds. | Ephemeral VMs. | Automations triggered by issue trackers, Sentry, webhooks and cron. | Command deny list and kernel-level "Veto". | Yes. Runners in your VPC; the management plane stays hosted. |
| **Warp Oz** [S26] | Docker sandboxes. | n/a | Hundreds of parallel agents, schedules, audit links. | n/a | Self-hosted environments for enterprise. |
| **Amp** [S27] | Orbs: one machine per thread with code, tools and plugins. | n/a | Same thread across web, iOS and CLI. Multi-model. | n/a | *Unverified* |
| **OpenHands** [S28] | Agent server in a Docker, process or remote runtime. | n/a | n/a | n/a | Yes (open source) |
| **hangar today** | Env images with a baked repo and `setup` (`repo.bake`), harness packs, warm pools. Rebuilds are manual (`hangar env publish`). | Sessions checkpoint the conversation, commits and WIP. `fork_session` and best-of-N. Local Kata and Docker do full snapshots. GKE has disk forks; Pod Snapshots pause and resume. | Follow-ups between turns. `needs_input` and elicitation. Dashboard terminal take-over. A send during a running turn gets an error (`session … is busy`). | Git gateway: no token in the sandbox, own branch only. No egress allowlist. The model key and GitHub REST are still in the sandbox (docs/security.md). | It *is* your compute. |

### 3.2 What hangar lacks, in priority order

1. **Hosting other vendors' runners.** Claude, Cursor, Devin, Ona and Warp all ask customers for a provisioner. Hangar is one but doesn't speak their hooks (item 2).
2. **Steering a running turn.** Claude queues messages, Codex has `turn/steer` [S14], Cursor allows follow-ups. Hangar refuses a send while the session is busy (item 3).
3. **Self-maintaining environments.** Every product rebuilds its cached environment by itself: Claude every ~7 days or on change, Devin every ~24 h, Codex caches for 12 h and runs a maintenance script. Hangar needs `hangar env publish`, and has no maintenance step on claim (item 4).
4. **PR loop.** Claude's auto-fix reacts to CI failures and review comments. Hangar acts only on `/hangar` comments (item 6).
5. **Network levels and credentials kept outside the sandbox** for everything except git (item 7).
6. **Verification inside the agent's loop.** Hangar verifies after the turn and runs a repair turn. Claude Code's own guidance is a Stop hook that blocks the end of a turn until the check passes [S8] (item 5).
7. **Code intelligence.** Claude Code cloud sessions *don't start* language-server plugins [S9]. Hangar can, which makes this a differentiator, not just a gap (item 9).
8. **Review UX inside the client.** The diff, inline comments and an answer form exist on claude.ai and Cursor. MCP Apps can put them in Claude, VS Code and Goose [S47] (item 10).

## 4. Sandbox infrastructure

### 4.1 Comparison

"1 vCPU + 4 GiB for an hour" is the list price for that shape. Hosts that bill CPU only while it is active are noted. GKE's figure is hangar's own estimate: an on-demand n2-standard-8 at about $280/month (docs/sessions.md), split per vCPU with 4 GB each. Spot capacity is cheaper (*unverified*).

| Platform | Isolation | Start / restore | Memory snapshot | Fork (one → many) | 1 vCPU + 4 GiB, 1 h | Self-host |
| --- | --- | --- | --- | --- | --- | --- |
| **E2B** [S29][S30][S31] | Firecracker | *unverified* this session | Yes (memory + filesystem); pause/resume | Yes: one snapshot → many sandboxes; the source keeps running | $0.115 | Yes. `e2b-dev/infra` is Apache-2.0, with GCP Terraform and a single-host "Embed" for evaluation |
| **Daytona** [S32][S33] | VM | Resume "under a second"; "under 90 ms from snapshots" (vendor claim via [S22]) | Yes: pause keeps memory, processes and connections (July 2026) | `_experimental_fork()` | $0.115 | Public repo archived June 2026. AGPL v0.190.0 continues as the community fork Nightona |
| **Modal** [S34] | gVisor or VM runtime | n/a | Alpha: 7-day retention, terminates the sandbox, same instance type | Yes: filesystem snapshots → many; memory restore makes an exact clone | $0.167 | No |
| **Morph** [S35] | VM | *unverified* | Docs list snapshots, branching, pause | "Create branches from instances" | *unverified* | No |
| **Vercel Sandbox** [S37] | Firecracker | Restore time counts toward create | No (filesystem only) | `Sandbox.fork`, filesystem | $0.098 at 10% CPU, $0.213 at 100% (active-CPU billing) | No |
| **Cloudflare Sandbox / Containers** [S36] | Container on Cloudflare | n/a | No; backup and restore of the workspace | No | $0.108 at full CPU (active-CPU billing) | No |
| **Fly Sprites** [S38, secondary] | VM | Checkpoint about 300 ms; async restore | Not stated (disk overlay) | n/a | ~$0.245 (derived) | No |
| **Northflank** [S39] | Kata or gVisor microVM | "Under a second" (claim) | Not stated | Not stated | $0.017/vCPU-h; memory not listed | BYOC |
| **GKE Agent Sandbox + Pod Snapshots** [S40][S41][S42] | gVisor (Kata with the open-source controller) | Warm claim 1.5 s, cold 17 s (hangar, measured 2026-10-11). Snapshot restore: "a few seconds" for the kernel, memory faulted in lazily | Yes: memory, rootfs, `emptyDir`, `tmpfs`. GA May 2026 | Restore into a **new** Pod by annotation `podsnapshot.gke.io/ps-name`. `rootfs-only` scope (1.35.3+) drops the spec-hash match. Whether one snapshot can seed N Pods at once is *unverified* | ~$0.05/vCPU-h with 4 GB, on demand (your project) | Yes. It's your cluster |
| **Firecracker** (raw) [S43] | microVM | n/a | Full and diff snapshots (diff is a developer preview). `MAP_PRIVATE` or UFFD lazy loading | Yes, with care: VMGenID reseeds the kernel RNG, but user-space unique state is cloned | n/a | Yes |
| **CRIU / Kubernetes checkpoint** [S44] | Container | n/a | Kubelet checkpoint API is beta (forensic). **No native restore** | Restore outside k8s only | n/a | Yes, but CRIU isn't packaged in Ubuntu 24.04 (checked with apt here) |
| **gVisor `runsc checkpoint`** (local) | gVisor | *Measured*: §4.3 | Yes | Yes: one image → two sandboxes (*measured*) | n/a | Yes |

### 4.2 What to support, and what to borrow

- **Providers:** keep Docker (dev, plus Kata microVMs), GKE (production) and E2B (hosted burst, or self-hosted Firecracker).
  - *Add* pools for vendor runners (item 2). These are not new sandbox backends: they run inside hangar's existing providers.
  - *Don't add* Daytona, Modal, Vercel, Cloudflare or Morph now. Each needs a vendor account and keeps code off the owner's infrastructure, which is the opposite of hangar's purpose. Daytona's public code has also stopped [S33].
  - An owner who wants one of these can reach most of them through item 14's Agents SDK interface. The SDK ships Blaxel, Cloudflare, Daytona, E2B, Modal, Runloop and Vercel [S17].
- **Borrow for GKE:**
  - **Memory fork through Pod Snapshots (item 8).** The how-to restores a named snapshot into a new Pod with a matching spec [S40]. Hangar's docs say GKE "restores a sandbox only from its own snapshots", which looks too conservative. It needs a live test. The local gVisor proof in §4.3 shows the mechanism works on the same runtime.
  - **Lazy restore.** Both GKE and Firecracker let the app run before memory is fully loaded [S40][S43]. Measure "first useful event", not "pod Running" (GKE's own advice [S40]).
  - **An in-sandbox daemon like E2B's envd** [S31][S42]: a process, filesystem, PTY and port API inside the sandbox, so hangar doesn't go through the Kubernetes API server for every exec. Agent Sandbox already ships an envd example. Build it only if GKE exec latency turns out to be the bottleneck (item 15). Docker exec p50 today is 75 ms (`bench/results/2026-10-13-perf-docker-ab-after.json`), with no GKE number yet.
  - **Uniqueness after restore.** Any memory fork must re-randomize what has to stay unique: session ids, `HANGAR_*` grant tokens, RNG-derived state. GKE says to re-create secrets and read the changed environment from `/proc/gvisor/spec_environ` [S40]; Firecracker says user-space state is not reseeded [S43]. Hangar's per-sandbox gateway grant must be minted *after* a fork, never inherited.

### 4.3 Experiments

**gVisor checkpoint/restore and fork** (runsc release-20261005.0, systrap, `--network=none`).

The workload is a Python process that imports modules and builds N MB of heap, standing in for a warmed language server or test runner. It writes a heartbeat file every 200 ms. Each run checkpoints the process, then restores the *same image* into two new sandbox IDs, `f1` then `f2`.

| Heap | Cold start → ready | Checkpoint | Image | Restore f1 → ready | Restore f2 → ready | Both forks progressing |
| --- | --- | --- | --- | --- | --- | --- |
| 64 MB | 0.94 s | 0.09 s | 82 MB | 0.19 s | 0.25 s | yes |
| 256 MB (2 runs) | 1.38 / 2.22 s | 0.33 / 0.57 s | 288 MB | 0.30 / 0.62 s | 0.30 / 1.25 s | yes |
| 512 MB | 2.86 s | 0.88 s | 562 MB | 1.06 s | 80.6 s | **no**: f1 was gone after f2 restored |

Findings:

1. **One image can be restored into several independent sandboxes, and each continues on its own.** So the memory-fork mechanism under GKE Pod Snapshots works on gVisor itself.
2. **Restore time grows with image size, about 1 s per 0.5 GB here.** Checkpoint is cheap.
3. **Each fork holds its own copy of memory.** At 512 MB, two forks plus the host load exhausted this 8 GB VM: f2 took 80 s and f1 died, probably killed for memory (*unverified*; the kernel log was not checked). Memory forks need admission control on node memory. Their real payoff is state that is expensive to rebuild, such as a language-server index or a warmed dev server. A synthetic heap is cheap to rebuild.

**Dependency install with and without a cache.** Two runs each. Python used hangar's own `uv.lock` (187 packages, 210 MB venv). Node used a TypeScript/Vite/Vitest/ESLint/React manifest (76 MB `node_modules`).

| | Empty cache | Warm cache, no venv/node_modules | Already installed |
| --- | --- | --- | --- |
| `uv sync --frozen` | 3.2 / 10.5 s | 0.11 / 0.13 s | 0.09 s |
| `npm install` then `npm ci --prefer-offline` | 30.5 / 31.3 s | 2.3 / 2.8 s | 2.3 s |

A warm package cache removes 96–99% of install time. With one, an incremental "maintenance" step on claim (item 4) costs well under 3 s, so the baked layer may be days old without hurting start time.

## 5. Protocols

| Protocol | State in October 2026 | Hangar today | Decision |
| --- | --- | --- | --- |
| **MCP 2026-07-28** [S45] | Stateless (no handshake, capabilities on every request). `server/discover`. MRTR `input_required` replaces server-to-client requests. Tasks moved into an extension. `subscriptions/listen`. `ttlMs`/`cacheScope` on list results. Dynamic client registration deprecated in favor of Client ID Metadata Documents. | Serves 2026-07-28 and the 2025 revisions on one endpoint, with tasks, MRTR elicitation and subscriptions (docs/mcp.md) | Keep. Return deterministic `tools/list` order for client prompt caching [S45]. Check that the OAuth server accepts Client ID Metadata Documents (*unverified* in hangar's `oauth.py`). |
| **MCP roadmap** (updated 2026-08-22) [S46] | Triggers & Events (server push, webhooks), DPoP and agent identity (workload identity federation, ID-JAG, RFC 8693), a `tools/call` result redesign, progressive discovery, HTTP over stdio. | n/a | When Triggers & Events lands, replace `wait_for_turn` polling with push. When DPoP and agent identity land, let a hangar sandbox call MCP servers under a delegated, narrower identity (0007's MCP gateway). No work until the specs settle. |
| **MCP Apps** [S47] | Official extension. `_meta.ui.resourceUri` points to a `ui://` HTML resource, rendered in a sandboxed iframe that can call tools. Hosts: Claude, Claude Desktop, VS Code Copilot, M365 Copilot, Goose, Postman. | None | **Adopt (item 10):** a session-review app with the diff and inline comments → `send_to_session`, the event timeline, and the `needs_input` form. |
| **MCP Registry** [S48] | Preview. Public servers only; it "does not support private servers". | n/a | Skip. Every hangar is private. Publish the `server.json` shape in `hangar connect` output if clients start reading it. |
| **A2A 1.0** [S49] | Stable since April 2026. 150+ organisations. In Azure AI Foundry, Copilot Studio and Bedrock AgentCore. Signed Agent Cards. | 1.0 + 0.3 over JSON-RPC (docs/a2a.md) | Done. Add signed agent cards when a client requires them. |
| **ACP** (Agent Client Protocol) [S50][S51] | v1 stable. v2 is a draft (2026-07-20). The remote Streamable HTTP/WebSocket transport RFD is "Active" (2026-07-02). Registry in Zed and JetBrains. About 45 agents, including Claude (adapter), Codex (adapter), pi (adapter), Gemini, Cursor, Copilot. | None | **Adopt as a stdio adapter (item 11):** `hangar acp` presents a hangar session as an ACP agent, so Zed and JetBrains users drive cloud sessions from the editor. Add the remote transport when it leaves "Active". |
| **Claude self-hosted runner** [S3][S4][S5] | Runners poll `api.anthropic.com`. The orchestrator runs `spawn-runner` with a signed work-order JWT, `CLAUDE_RUNNER_ORDER_ID` (idempotency) and repo hints. Exit codes: 0 ok, 1 retry, ≥2 give up. `--min-idle` pre-warms. Lifecycle hooks `checkout`, `post-session` and `command`. `--exec-path` wrapper. Prometheus `/metrics`. Verified locally with `claude self-hosted-runner orchestrator --help` (2.1.296). | None | **Adopt (item 2).** |
| **Codex app-server** [S14] | JSON-RPC over stdio (WebSocket experimental): `thread/start\|resume\|fork`, `turn/start\|steer\|interrupt`, streamed item events. Marked `[experimental]` in 0.162.1. | Runs `codex exec` / `exec resume` / `exec fork` | Use `codex queue --thread` for steering now (item 3). Move to app-server once it drops "experimental". |
| **OpenAI Agents SDK sandboxes** [S17] | Seven providers plus bring-your-own. Manifest for portable workspaces. Snapshot and rehydrate. Harness kept separate from compute. | Planned as 0008 M12 | Item 14, priority low. |
| **Claude Agent SDK** [S7] | `excludeDynamicSections` / `--exclude-dynamic-system-prompt-sections` moves per-machine context out of the system prompt so caches are shared across machines | Runs the CLI | Use the CLI flag for `swarm`/`map` runs that span different checkout paths (item 1). No SDK migration needed. |

## 6. Agent effectiveness on hangar

### 6.1 Environment and context engineering
- **Instructions:** keep `CLAUDE.md`/`AGENTS.md` short. Claude Code's guidance is that bloated files get ignored, and anything needed only sometimes belongs in a skill [S8]. 0008's evidence points the same way: curated skills gave +16.2 pp, self-generated ones −1.3 pp, and 2–3 focused skills beat comprehensive ones (0008 §2, SkillsBench). Hangar's `skills:` and `agent_config.instructions` are the right shape. What's missing is measuring them (item 13).
- **Cached dependency layers:** §4.3. Item 4 makes them keep themselves fresh.
- **Language servers:** item 9. Install the server binary and the code-intelligence plugin in the image, so the agent gets diagnostics after every edit [S9]. Whether plugins start under `claude -p` was not verified (no API key here). That is the first test.
- **Stable paths:** Claude Code's cache is "scoped to one machine and directory". The system prompt carries the working directory, platform, shell and OS [S6]. Hangar's fixed `/workspace/<repo>` checkout and pinned images already give forks the same prefix. Keep it that way.

### 6.2 Verification loops
Hangar verifies after the turn (0002, `repo.verify`) and gives the agent one repair turn. Claude Code's guidance is to make the check a gate the agent can't stop past [S8]: a Stop hook that runs the check and blocks the turn from ending until it passes (with a cap on consecutive blocks), or a `/goal` condition. Item 5 injects a Stop hook that runs `repo.verify` in Claude sandboxes. Hangar's post-turn verification stays as the referee, because a gate inside the sandbox can be edited by the agent. For Codex and pi, the repair turn remains the mechanism. Ask for evidence (test output) in `RESULT.md`, as [S8] recommends.

### 6.3 Learning from outcomes (0007/0008)
Hangar already records verified outcomes, repair turns, `needs_input` questions and PR review comments (once item 6 reads them). The cheapest learning loop is ACE-style small deltas to a repo skill, proposed as a PR and A/B-tested with 0007's experiments (0008 §2, §5.3). Item 13 limits this to one source: review comments and repair causes on merged PRs.

### 6.4 Model routing, cost and latency
Current list prices per MTok (input / output) [S11][S16]:

- Anthropic: Opus 5.5 $4 / $20, Sonnet 5.5 $2 / $10, Haiku 5.5 $0.10 / $0.50 (≤100k prompt).
- OpenAI: gpt-6.1-sol $2 / $10, gpt-6-luna $0.10 / $0.50, gpt-5.3-codex $1.75 / $14.

Route **per session or job, at creation**, never per turn: a model switch makes the next request re-read the whole conversation uncached [S6]. Sensible defaults:

- the strongest model for planning, best-of-N judging and anything with `needs_input`;
- Sonnet-class models for well-specified issue fixes with a verify command;
- Haiku- or luna-class models for `map` shards and labeling.

Choose and confirm these with 0007 experiments, on cost per *verified* task (0004 §5.8). That is item 12.

### 6.5 Prompt caching and long sessions
Facts:

- Anthropic: 5-minute writes cost 1.25× input, 1-hour writes 2×. Reads cost 0.1× generally, and 0.05× on Opus 5.5 and Sonnet 5.5 [S10].
- Claude Code: the main conversation uses a 1-hour TTL only on a subscription within plan usage. API keys and cloud providers get 5 minutes. `CLAUDE_CODE_PROMPT_CACHE_TTL=1h` or the `promptCacheTtl` setting overrides this [S6].
- Claude Code: a resumed conversation keeps the system prompt it started with, so a resume in a fresh sandbox can still hit the cache [S6].
- Claude Code: a fan-out holds all but the first agent for up to 5 s so the rest read the first one's cache [S6].
- OpenAI: GPT-5.6+ reads cost 0.1× and writes 1.25× (0.05× reads for gpt-6.1-sol), with a TTL of 30 m [S15].

What a session that returns after a gap costs, at 150k tokens of context (Anthropic prices [S11]):

| Model | Cache still warm (read) | Cache expired (re-write at 5m price) | Extra cost of 1h TTL per 100k tokens written |
| --- | --- | --- | --- |
| Opus 5.5 | $0.03 | $0.75 | $0.30 |
| Sonnet 5.5 | $0.015 | $0.375 | $0.15 |

At about 400k tokens written in a session, the 1-hour TTL pays for itself once there are **two gaps of 5–60 minutes**. A `needs_input` round trip with a human, or a follow-up after review, is usually such a gap. Gaps longer than an hour lose the cache either way.

Best-of-N: four forks launched together from a 100k-token prefix each write it: 4 × $0.50 = $2.00 on Opus 5.5. Staggered, one writes and three read: $0.50 + 3 × $0.02 = $0.56. That assumes simultaneous identical requests don't share a write in flight, which is inferred from Claude Code's own fan-out hold [S6] and not measured. Item 1 implements both policies.

## 7. Prioritised items

Effort: S ≤ 1 day, M ≈ 2–3 days, L ≈ 4–6 days (as in the specs README).

| # | Item | Why | Effort | Depends on | Success measure |
| --- | --- | --- | --- | --- | --- |
| 1 | **Cache-aware sessions.** (a) Set `CLAUDE_CODE_PROMPT_CACHE_TTL=1h` for API-key credentials on sessions and jobs that can get follow-ups. (b) Never change model or effort between turns: the model is fixed at session creation, and a harness or model switch is the existing handoff. (c) Hold best-of-N attempts 2..N until attempt 1 emits its first assistant event, or 5 s. (d) Pass `--exclude-dynamic-system-prompt-sections` for `swarm`/`map` across checkout paths. (e) Price 1-hour writes at 2× (today `events.py` prices every cache write at the 5-minute rate). | §6.5: up to 25× cost difference on a resumed turn; latency of the first turn back | S | events.py usage (built) | Cache-read share of input tokens per session ≥ 85% on multi-turn sessions. Cost of the first turn after a 5–60 min gap down ≥ 80%. Best-of-N total input cost down ~70% for N = 4. All read from existing usage events. |
| 2 | **Vendor runner pools ("outposts").** `hangar runners` creates a pool from an environment. Claude first: run `claude self-hosted-runner orchestrator` with a hangar `spawn-runner` hook. The hook creates a hangar sandbox from the env, idempotent on `CLAUDE_RUNNER_ORDER_ID`, writes the work order to a 0600 file, starts `claude self-hosted-runner --capacity 1`, and returns 0 / 1 / ≥2 per the exit contract. `--min-idle` maps to the env's warm pool, and `post-session` to hangar's checkpoint. Then Cursor (`agent worker start`) and Devin (`devin worker start --outpost`). | Decision 1: the vendors' UX (claude.ai, mobile, teleport, auto-fix, routines) on the owner's compute, with hangar's egress, gateway and envs | M (Claude); S each after | An Anthropic Team/Enterprise org with self-hosted environments enabled (**owner decision**); the egress allowlist must include `api.anthropic.com` | A session started on claude.ai lands in a hangar sandbox; p50 from submit to first event ≤ 10 s from a warm pool; zero duplicate sandboxes per order id (test with redelivery); runner metrics scraped. |
| 3 | **Steer a running turn.** `send_to_session` on a busy session queues the message into the running turn instead of failing. Claude: run with `--input-format stream-json` and keep stdin open. Codex: `codex queue --thread <id> --message …`. pi: `--mode rpc`. Antigravity: queue for the next turn. MCP `send_to_session(steer=true)`; dashboard "send now". | Every hosted product has it; it saves whole turns when the agent heads the wrong way | M | Harness profiles (built) | Steer acknowledged in the event stream within 5 s (p95). Fake-agent tests for all three modes. |
| 4 | **Self-maintaining environments.** (a) A repo-local `.hangar/env.yaml` overlay, also read from `.devcontainer` when present. (b) `hangar env init --from-repo`: an agent drafts the env spec and verify command in a sandbox and proves them by running them, the way Devin and Cursor generate setup [S21][S18]. (c) Rebuild the baked layer automatically when a lockfile digest changes on the default branch, and every 24 h. (d) A `maintenance:` command run on claim (`git fetch && uv sync`). (e) Per-env package caches kept in the image. | Gap 3. Measured: a warm cache cuts installs from 3–31 s to 0.1–2.8 s | M | 0007 M2 builds without a laptop; 0004 M3 image distribution | `ready_s` p50 ≤ 10 s on a repo whose lockfile changed yesterday. Zero manual `env publish` in a week of normal work. |
| 5 | **Verify gate inside the loop.** Inject a Claude Code Stop hook that runs `repo.verify` and blocks ending the turn until it passes, capped. Post-turn verification stays authoritative. Ask for evidence in `RESULT.md`. | [S8]: the difference between a session you watch and one you walk away from | S | `repo.verify` (built) | First-delivery verified-pass rate up, repair turns per job down; A/B with 0007 on 20 tasks. |
| 6 | **PR auto-fix loop.** Subscribe to `check_run`/`check_suite` failures and `pull_request_review` / `review_comment` on hangar-opened PRs. Each event becomes a follow-up turn in the job's session. Ambiguous comments become `needs_input`. Rate-limited per PR; off per repo. | Gap 4. Hangar already has sessions, follow-ups and needs_input; only the triggers are missing | M | GitHub App webhooks (0003); front-door trust rules (built) | Share of hangar PRs that reach green with no human push. Median time from CI failure to fix push ≤ 15 min. |
| 7 | **Egress levels and credential injection.** Network levels None / Trusted / Custom per env, enforced by NetworkPolicy plus an egress proxy on the gateway listener. The proxy also injects API keys for listed hosts (model API, GitHub REST, others), so no secret is readable in the sandbox. Deny by default for graded runs. | Gap 5. The model key and GitHub REST token are still in the sandbox (docs/security.md) | L | 0003 M6–M7, M9 (decision 9 of the specs README for the subscription token) | A sandbox test finds no credential in env, files or `/proc`. Blocked requests are audited. The Trusted list covers package installs for the bundled envs. |
| 8 | **Memory fork on GKE.** Test live whether a Pod Snapshot can be restored into N new sandboxes (annotation `podsnapshot.gke.io/ps-name`, `rootfs-only` scope). If yes, `fork_session(kind="memory")` with node-memory admission control and gateway grants re-minted after restore. If no, document that and keep disk forks. | Decision 4; 0008 M2's open question | M | Pod Snapshots enabled (built, optional) | 0008 targets: memory fork N = 10 < 5 s. A forked dev server answers within 2 s of the restore. No grant shared between forks (test). |
| 9 | **Pre-warmed code intelligence.** Env `lsp: [pyright, typescript, gopls…]` installs the server and Claude's code-intelligence plugin [S9]. Warm the index in the baked layer, or in the memory snapshot once item 8 works. | Claude's cloud sessions lack it, so it's a differentiator. Diagnostics after every edit catch type errors before tests | S (+ M for warm index) | Item 4 | First verify whether plugins load under `claude -p` (needs an API key). Then A/B with 0007: type-check failures at verify and turns per task. |
| 10 | **MCP App for session review.** `ui://hangar/session` attached to `get_session`/`wait_for_turn`: diff view, inline comment → `send_to_session`, events timeline, `needs_input` form, artifacts. | Review UX where the user already is: Claude, VS Code Copilot, Goose [S47] | M | Resources and subscriptions (built) | Renders in Claude Desktop and VS Code. A comment on a diff line reaches the agent as a follow-up. |
| 11 | **ACP adapter.** `hangar acp` (stdio, ACP v1): `session/new` → `new_session`, `session/prompt` → `send_to_session` with streamed updates from events, `session/load` → resume, cancel → suspend or stop. Listed in the ACP registry later. | Zed and JetBrains get cloud sessions with the editor's diff and permission UI [S50][S51] | M | Item 3 for mid-prompt updates | Zed and JetBrains can create, prompt, cancel and reload a hangar session. |
| 12 | **Session-level model routing.** Pick the model at session or job creation from the task class, with per-repo defaults chosen by 0007 experiments on cost per verified task. | §6.4 | M | 0007 M7; item 1(b) | Cost per verified task −20% at equal pass rate (0004 §5.8 target). |
| 13 | **Learning from review.** Weekly job: cluster review comments and repair causes on merged hangar PRs, propose ≤ 3 small skill or `CLAUDE.md` deltas as a PR, each with a paired 0007 experiment. | 0008's ACE / SkillsBench evidence; humans merge | M | Item 6 (review comments), 0007 M7 | Accepted deltas show +pp verified pass on held-out tasks. Repeat review comments of the same cluster fall. |
| 14 | **Agents SDK sandbox provider** (0008 M12). Implement the SDK's bring-your-own sandbox for hangar, so SDK agents get hangar sandboxes, and hangar can reach the SDK's seven providers if an owner wants one. | MCP is hangar's front door for agents; this is the door for programs | M | None | An SDK `SandboxAgent` runs a task in a hangar sandbox and survives a sandbox loss through snapshot and rehydrate [S17]. |
| 15 | **`hangard` in-sandbox daemon** (conditional). An envd-style process, file, PTY and event API inside the sandbox, reached through the gateway listener with a per-sandbox token. | Only if GKE exec through the API server is slow: measure first | L | Item 7's gateway listener | Build only if GKE `exec` p50 is more than 3× Docker's 75 ms. Then exec p50 ≤ 100 ms and API-server QPS down. |

## 8. Recommended next three waves

Each wave is a set of parallel workstreams, one PR each, like 0011 §3.

**Wave 3: the agent loop.** Items 1, 3, 5, 6, and 4(c–e). These are small to medium and need no new infrastructure or owner decision; most are harness flags plus front-door triggers.

- *Exit:*
  - a steered turn;
  - a PR that went red and was fixed by the agent without a human;
  - a multi-turn session with ≥ 85% cache reads;
  - an env whose baked layer rebuilt itself after a lockfile change.

**Wave 4: the owner's compute as the platform.** Items 2 (Claude first), 7, 8, 9, and 4(a–b).

- *Owner decision first:* an Anthropic Team or Enterprise org with self-hosted environments, for item 2.
- *Exit:*
  - a claude.ai session running on a hangar GKE sandbox behind the egress proxy;
  - no secrets readable in sandboxes;
  - a measured yes or no on GKE memory forks;
  - language-server diagnostics in Claude sandboxes.

**Wave 5: meet users in their tools; learn.** Items 10, 11, 12, 13, then 14 and 15 as needed.

- *Exit:*
  - review and answer a session from Claude Desktop (MCP App) and from Zed (ACP);
  - routing on by default with a measured cost drop;
  - a first learned-skill PR with its experiment.

## 9. What I couldn't verify

- **E2B:** start and restore latency, and snapshot pricing. Not stated on the pages fetched [S29][S30].
- **Morph:** latency, pricing, memory branching details. Only the docs index was reachable [S35].
- **Cloud start latency** for Modal, Vercel and Cloudflare: none stated on the fetched pages.
- **Northflank and Daytona:** the latency figures are vendor claims.
- **Fly Sprites:** figures come from a secondary source [S38].
- **GKE Pod Snapshots:**
  - whether one snapshot can seed *several* new Pods at once;
  - real restore times for coding workloads. The 89% startup reduction and "8 s" figures are for model serving [S41].
- **Codex Cloud:** how the new version reaches private services, and whether it has a self-hosted runner [S13].
- **Amp:** whether orbs can run on customer compute [S27].
- **Self-hosted Claude Code sessions:** whether Claude Code's LSP plugins start in `claude -p` mode, and in self-hosted cloud sessions. The doc only says Anthropic-hosted cloud sessions don't start them [S9].
- **gVisor fork at 512 MB:** that f1 was OOM-killed is inferred from memory pressure; the kernel log was not checked.
- **Best-of-N write sharing:** that concurrent identical requests each pay a cache write is inferred, not measured.
- **Hangar's OAuth server:** whether it accepts Client ID Metadata Documents was not checked.
- **Pricing:** every price was list price on 2026-10-13; regional and committed-use prices differ.

## 10. Sources

Fetched 2026-10-13.

**Cloud coding agents**

- S1 [Claude Code: use Claude Code in the cloud](https://code.claude.com/docs/en/claude-code-on-the-web)
- S2 [Claude Code: cloud environments](https://code.claude.com/docs/en/cloud-environments)
- S3 [Claude Code: self-hosted environments](https://code.claude.com/docs/en/self-hosted-environments)
- S4 [Self-hosted environments reference](https://code.claude.com/docs/en/self-hosted-environments-reference)
- S5 [Customize self-hosted sessions (hooks, `spawn-runner`)](https://code.claude.com/docs/en/self-hosted-environments-configuration)
- S12 [Codex Cloud (legacy) environments](https://learn.chatgpt.com/docs/environments/cloud-environment), redirected from developers.openai.com/codex/cloud/environments
- S13 [Using Codex Cloud](https://help.openai.com/en/articles/20001545-using-codex-cloud)
- S18 [Cursor cloud agents](https://cursor.com/docs/cloud-agent)
- S19 [Cursor self-hosted cloud agents](https://cursor.com/docs/cloud-agent/self-hosted); [changelog 2026-03-25](https://cursor.com/changelog/03-25-26)
- S20 [Jules environment setup](https://jules.google/docs/environment/)
- S21 [Devin blueprints](https://docs.devin.ai/onboard-devin/environment/blueprints)
- S22 [Devin Outposts](https://docs.devin.ai/cloud/outposts/overview)
- S23 [Copilot cloud agent environment](https://docs.github.com/en/copilot/how-tos/copilot-on-github/customize-copilot/customize-cloud-agent/customize-the-agent-environment)
- S24 [Factory Droid Computers](https://docs.factory.ai/cli/features/droid-computers)
- S25 [How Ona works](https://ona.com/docs/ona/understanding/how-ona-works)
- S26 [Warp launches Oz](https://warp.dev/newsroom/2026/2/10/warp-launches-oz-the-orchestration-platform-for-cloud-coding-agents)
- S27 [Amp manual](https://ampcode.com/manual)
- S28 [OpenHands runtimes](https://docs.openhands.dev/usage/runtimes/overview)

**Claude Code behavior and models**

- S6 [How Claude Code uses prompt caching](https://code.claude.com/docs/en/prompt-caching)
- S7 [Agent SDK: modifying system prompts](https://code.claude.com/docs/en/agent-sdk/modifying-system-prompts)
- S8 [Claude Code best practices](https://code.claude.com/docs/en/best-practices)
- S9 [Code intelligence plugins](https://code.claude.com/docs/en/plugins/code-intelligence)
- S10 [Anthropic prompt caching](https://platform.claude.com/docs/en/build-with-claude/prompt-caching)
- S11 [Anthropic pricing](https://platform.claude.com/docs/en/about-claude/pricing)
- S15 [OpenAI prompt caching](https://developers.openai.com/api/docs/guides/prompt-caching)
- S16 [OpenAI pricing](https://developers.openai.com/api/docs/pricing)

**Protocols and SDKs**

- S14 [Codex app-server README](https://github.com/openai/codex/blob/main/codex-rs/app-server/README.md)
- S17 [The next evolution of the Agents SDK (2026-04-15)](https://openai.com/index/the-next-evolution-of-the-agents-sdk/)
- S45 [MCP 2026-07-28 changelog](https://modelcontextprotocol.io/specification/2026-07-28/changelog)
- S46 [MCP roadmap](https://modelcontextprotocol.io/development/roadmap)
- S47 [MCP Apps](https://modelcontextprotocol.io/extensions/apps/overview)
- S48 [MCP Registry](https://modelcontextprotocol.io/registry/about)
- S49 [A2A at one year (Linux Foundation, 2026-04-09)](https://linuxfoundation.org/press/a2a-protocol-surpasses-150-organizations-lands-in-major-cloud-platforms-and-sees-enterprise-production-use-in-first-year)
- S50 ACP:
  - [introduction](https://agentclientprotocol.com/overview/introduction)
  - [agents](https://agentclientprotocol.com/get-started/agents)
  - [v2 draft](https://agentclientprotocol.com/announcements/acp-v2-draft)
  - [remote transport RFD](https://agentclientprotocol.com/rfds/streamable-http-websocket-transport)
- S51 [The ACP Registry is live (Zed)](https://zed.dev/blog/acp-registry)

**Sandbox platforms and runtimes**

- S29 E2B: [billing](https://docs.e2b.dev/billing), [pricing](https://e2b.dev/pricing)
- S30 [E2B snapshots](https://docs.e2b.dev/sandbox/snapshots)
- S31 [e2b-dev/infra](https://github.com/e2b-dev/infra)
- S32 Daytona: [VM pause and fork](https://www.daytona.io/dotfiles/vms-pause-and-fork), [pricing](https://www.daytona.io/pricing)
- S33 [Nightona README](https://github.com/nightona-co/nightona)
- S34 Modal: [sandbox snapshots](https://modal.com/docs/guide/sandbox-snapshots), [pricing](https://modal.com/pricing)
- S35 [Morph Cloud docs](https://cloud.morph.so/docs/documentation/overview)
- S36 Cloudflare: [Containers and Sandboxes GA](https://developers.cloudflare.com/changelog/2026-04-13-containers-sandbox-ga), [Containers pricing](https://developers.cloudflare.com/containers/pricing/)
- S37 Vercel Sandbox: [pricing](https://vercel.com/docs/sandbox/pricing), [snapshots](https://vercel.com/docs/sandbox/concepts/snapshots)
- S38 [Simon Willison on Sprites (secondary)](https://simonwillison.net/2026/Jan/9/sprites-dev/)
- S39 [Northflank sandboxes](https://northflank.com/product/sandboxes)
- S40 GKE Pod snapshots: [concepts](https://docs.cloud.google.com/kubernetes-engine/docs/concepts/pod-snapshots), [restore](https://docs.cloud.google.com/kubernetes-engine/docs/how-to/pod-snapshots)
- S41 [InfoQ: GKE Pod Snapshots benchmarks (2026-09-27)](https://infoq.com/news/2026/09/gke-pod-snapshots-benchmarks)
- S42 Agent Sandbox: [README](https://github.com/kubernetes-sigs/agent-sandbox), [envd example](https://agent-sandbox.sigs.k8s.io/docs/use-cases/examples/envd-sandbox/)
- S43 [Firecracker snapshot support](https://github.com/firecracker-microvm/firecracker/blob/main/docs/snapshotting/snapshot-support.md)
- S44 [KEP-2008 forensic container checkpointing](https://www.kubernetes.dev/resources/keps/2008/)

**Local verification**

- gVisor runsc release-20261005.0, from `gs://gvisor/releases/release/20261005/x86_64/gvisor.tar.zstd` (sha512 checked).
- `claude` 2.1.296, `codex` 0.162.1 and `pi` 1.1.0 `--help` output.
- Hangar's own `bench/results/2026-10-13-perf-docker-ab-after.json`.
