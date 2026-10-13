# Google AX and Agent Substrate: an assessment for hangar

Date: 2026-10-13 · Workstream G of [spec 0011](../specs/0011-factory-roadmap.md) · Sources: the `main` branches of [google/ax](https://github.com/google/ax) and [agent-substrate/substrate](https://github.com/agent-substrate/substrate) as of this date (AX pins Substrate `v0.0.0-20260918…` in its [`go.mod`](https://github.com/google/ax/blob/main/go.mod)), plus the [launch post](https://cloud.google.com/blog/products/ai-machine-learning/agent-executor-googles-distributed-agent-runtime). Every claim below links to its source; nothing was run against a live cluster.

## Recommendation

1. **Don't build on AX now.** Spec 0011 §4 got this right. AX is a preview whose README promises "major breaking changes prior to a stable release". It duplicates hangar's control-plane role (its own Redis store, workspaces and models). It also hides the one Substrate feature hangar would want, memory snapshots: AX's suspend keeps `/workspace` only.
2. **Interoperate through A2A, which hangar now serves** ([a2a.md](../a2a.md)). One correction to §4: AX itself has no A2A or MCP endpoint, only a gRPC API. What can delegate to hangar is an *agent running in* an AX Task (ADK, LangGraph, any A2A client), which Google's launch post lists as a supported workload.
3. **If we ever add a provider here, target Agent Substrate directly, not AX.** Hangar and AX sit at the same layer; Substrate is the layer below both. A `providers/substrate.py` would get `MEMORY`-fidelity suspend and resume, which is hot resume for 0010 M4, without a second orchestrator in the path. The `providers/ax.py` design below shows what the AX route would take and why it is the weaker one.
4. **Revisit** when any trigger in §8 fires. The likeliest are Substrate actor forking landing, or Substrate exposing exec, files and memory snapshots through a stable API.

## 1. What they are

**AX (Agent Executor)** is "a high-throughput, declarative orchestrator to run billions of autonomous agent workloads in a cluster". It is written in Go, Apache-2.0, and was announced on 2026-05-20 as "available now in preview" ([README](https://github.com/google/ax#readme), [blog](https://cloud.google.com/blog/products/ai-machine-learning/agent-executor-googles-distributed-agent-runtime)). It has three binaries ([DESIGN.md](https://github.com/google/ax/blob/main/DESIGN.md)):
- `ax`: a kubectl-shaped CLI;
- `ax-server`: a gRPC API that keeps state in **Redis** rather than CRDs ("storing millions of short-lived tasks as Kubernetes CRDs pushes etcd past its comfort zone");
- `ax-task-runner`: PID 1 inside every task.

**Agent Substrate** is the runtime under AX: "a secure-by-default agent execution runtime engineered to run millions of sandboxes with 10x higher density than standard container runtimes", claiming "sub-500ms resume operations at over 500 suspend/resume activations per second" ([README](https://github.com/agent-substrate/substrate#readme)). It maps many **Actors** onto fewer warm **Workers**, the pods of a `WorkerPool`. It runs gVisor or microVMs (cloud-hypervisor), keeps snapshots in GCS or S3, and routes traffic through the `atenet` router, which "resumes suspended Actors on demand" ([glossary](https://github.com/agent-substrate/substrate/blob/main/docs/glossary.md)). It is "pre-1.0" with no backward-compatibility guarantees.

## 2. Primitives side by side

| Concern | Hangar | AX | Substrate |
| --- | --- | --- | --- |
| Unit of isolation | `Sandbox` (container or microVM via a `Provider`: Docker, GKE Agent Sandbox, E2B) | `Task`: image, command, CPU and memory limits, `workspaces`, `debug` ([manifests](https://github.com/google/ax/blob/main/docs/manifests.md)) | `Actor` from an `ActorTemplate`, scheduled onto a `Worker` |
| Environment and toolchain | `environment` (0006): image build, harness packs, `repo`, `mcp:`, `secrets:` | `Workspace`: git repos, files, MCP servers and registries, skill registries, and a `goal` that an Antigravity agent turns into setup on first boot ([concepts](https://github.com/google/ax/blob/main/docs/concepts.md)) | `ActorTemplate`: images, snapshot config, `DurableDir` volumes |
| Model config | Per harness (credentials in the vault, `model` per job or session) | `Model`: provider, model id, params, a k8s Secret reference. Used by AX's own components, such as the goal bootstrap | none |
| Long-lived work | `Session` (0010): harness conversation, git bundle, WIP and untracked files, checkpointed per turn; resumes in a fresh sandbox | `ax suspend` / `ax resume` on a Task | Suspend, Pause and Resume of an Actor |
| Requested work | `Job` (0009): instructions and context → verified branch and PR, follow-ups, callbacks, GitHub trigger | none: a Task runs a command; "lets the agent compose as many [tasks] as its work demands" | none |
| Who drives the agent | Hangar: it writes the prompt, runs the harness CLI per turn, verifies, delivers | The Task's own `command`; AX "does not try to model" the agent's shape | The workload itself |
| Interfaces | MCP, REST, CLI, GitHub, A2A (this PR) | gRPC `ax.v1alpha1.AX` (Get/List/Create/Delete/Suspend/Resume/WatchTask; workspaces; models) | gRPC `ateapi`, `kubectl-ate`, the `atenet` HTTP router |

The mapping isn't one to one. An AX **Task** covers hangar's **sandbox** plus the command running in it. A **Workspace** is most of an **environment**. A **session** and a **job** have no AX counterpart: they are the parts of hangar above the sandbox, which spec 0011 §4 calls hangar's value. That is why AX is a *peer* of hangar's sandbox-and-environment layer, and Substrate is the layer under it.

## 3. Checkpoints and suspend, compared with 0010

| | Hangar 0010 checkpoints | AX suspend (today) | Substrate snapshots |
| --- | --- | --- | --- |
| What is kept | Harness conversation files, unpushed commits (bundle), uncommitted and untracked files, the job context dir | `/workspace` only: "Agent Substrate snapshots it when a task is suspended and restores it into a fresh container … the same files but a new process tree" ([runner.md](https://github.com/google/ax/blob/main/docs/runner.md)) | By template `preferredFidelity`: `VOLUMES` (DurableDir only), `ROOTFS` (microVM only), or `MEMORY`: "volumes, rootfs changes, and process memory … to resume hot" ([glossary](https://github.com/agent-substrate/substrate/blob/main/docs/glossary.md#snapshots)) |
| Running processes | Lost: dev servers and tmux windows are 0010 M4 (GKE Pod Snapshots, opt-in) | Lost | Kept with `MEMORY` |
| Resume cost | A fresh sandbox (warm pool or cold: node scale-up plus image pull), then replay of commits and files, then `--resume` of the harness conversation | Fresh container and the runner re-reads its marker files | "sub-500ms" per the README; Pause keeps the checkpoint node-local for an even faster resume |
| Portability | Checkpoints are plain files (GCS or local), encrypted, independent of the provider; a session can resume on another provider | Tied to the Substrate cluster | Tied to the Substrate cluster and the gVisor version ("gvisor upgrade → loss of memory snapshot" is an open lifecycle question on the [roadmap](https://github.com/agent-substrate/substrate/blob/main/docs/roadmap.md)) |
| Branching | Planned: `fork_session` (workstream D) from the latest checkpoint | Roadmap: "Stateful Task Branching" ([roadmap](https://github.com/google/ax/blob/main/docs/roadmap.md)) | Roadmap, "Coming Soon": "Actor Forking/Cloning … from an existing checkpoint" |

Hangar's checkpoints and Substrate snapshots complement each other. Hangar's are semantic and portable: they survive a provider change, a Substrate upgrade and a lost cluster. Substrate's are fast and complete, including memory. A Substrate-backed provider would use `MEMORY` suspend as the *hot* path and keep 0010 checkpoints as the durable fallback. That is the shape 0010 M4 already plans for GKE Pod Snapshots.

The launch post's "durable execution … via an event log and snapshotting", "session consistency" and "trajectory branching" ([blog](https://cloud.google.com/blog/products/ai-machine-learning/agent-executor-googles-distributed-agent-runtime)) are not in the open-source repo yet. The repo's docs describe Redis-backed resources, phases and conditions, and list branching as roadmap. Spec 0011 §4 repeats the blog's claims. They should be read as direction, not as what ships.

## 4. Harness support (bring your own harness)

§4 says bring-your-own-harness "is still on its roadmap". That's only half right:

- **What runs today:** any image and command. A Task's `spec.image` can be any image that has an executable at `/usr/local/bin/ax-task-runner`, and the runner starts `spec.command` ([runner.md](https://github.com/google/ax/blob/main/docs/runner.md)). Substrate ships a [Claude Code multiplex demo](https://github.com/agent-substrate/substrate/blob/main/demos/claude-code-multiplex/README.md), with three Claude Code agents on two pods, although it needed runtime workarounds for two open atelet bugs (#189, #197). So Claude Code, Codex, pi or Antigravity can run in an AX Task now.
- **What's on the roadmap:** decoupling AX's *own* built-in harness. Workspace `goal` setup always uses Antigravity with `GEMINI_API_KEY` ([sandbox.md](https://github.com/google/ax/blob/main/docs/sandbox.md)). The roadmap item is "Allow Customization: decouple the built-in workspace bootstrap and coding agent harness".
- **What hangar would miss:** a way to *drive* a harness turn by turn. AX has no exec or turn API. Process and file access exist only as Substrate guest services, and only when the Task sets `debug: true`, which "allow[s] arbitrary process execution and file access inside the sandbox" ([sandbox.md](https://github.com/google/ax/blob/main/docs/sandbox.md#guest-services)). Hangar's whole model (inject credentials, write the prompt, run `claude -p … --resume`, read `RESULT.md`, verify, push) runs over exec and files. So a hangar provider on AX would have to run every Task in debug mode. AX's docs frame that mode as a debugging aid, not as the control path.

## 5. MCP and A2A

- **MCP:** AX Workspaces pre-wire MCP servers (static endpoints or a Google registry query) for the agent in the Task ([manifests](https://github.com/google/ax/blob/main/docs/manifests.md)). Hangar already has the equivalent in environment `mcp:` (0006). AX doesn't *serve* MCP: there's no MCP endpoint for clients to drive AX. Substrate's roadmap has "Native MCP Server Hosting" (MCP servers as Actors).
- **A2A:** the AX repo has no A2A endpoint. The launch post lists "any agent using the Agent2Agent Protocol (A2A)" among the workloads you can run, and Substrate's roadmap has an "Actor-to-Actor (A2A) Calling Model". So the interop path is *agent-to-agent*: an ADK or LangGraph agent in an AX Task calls hangar's `/a2a` with a `jobs` token. This needs nothing from AX beyond egress to hangar. Substrate's egress credential injection ([docs](https://github.com/agent-substrate/substrate/blob/main/docs/egress-credential-injection.md)) could even keep the hangar token out of the Task.

## 6. Kubernetes and operations

- **Substrate:** GKE **1.36 with Kubernetes beta APIs enabled at cluster creation** (`PodCertificateRequest`, `ClusterTrustBundle`; "GKE only honors `enableK8sBetaApis` at creation time"), or GKE 1.37+. "Versions below 1.36 are not supported." ([setup-gcp](https://github.com/agent-substrate/substrate/blob/main/tools/setup-gcp/README.md)) It also needs:
  - a node label `ate.dev/substrate-version` on every pool, or new nodes run no workers;
  - a GCS (or S3) snapshot bucket with Workload Identity grants;
  - a PostgreSQL state store (Cloud SQL supported);
  - its own system namespace `ate-system`.
- **AX on top:** Redis, images built and pushed with `ko`, the `ax-system` namespace, and the Go toolchain for the CLI ([README](https://github.com/google/ax#quick-start)).
- **Hangar's cluster today:** GKE Standard on the RAPID channel at 1.35.2+, for Agent Sandbox ([gke.md](../gke.md)). Adopting Substrate means a **new cluster** (or recreating ours with beta APIs on), plus Postgres, plus a second sandbox runtime next to Agent Sandbox. Its `gvisor` sandbox class overlaps with ours; its `microvm` class is an option hangar has only locally (Kata, [local-microvms.md](../local-microvms.md)).

## 7. Maturity

| Signal | AX | Substrate |
| --- | --- | --- |
| Stated status | "heavy development … major breaking changes prior to a stable release"; API `ax.io/v1alpha1` | "pre-1.0 … APIs and behavior may still change significantly"; "the project is VERY young" |
| Age | Open-sourced 2026-05-20 (about 5 months) | Similar; the AX integration is a roadmap item ("Tight integration with Agent Executor") |
| Roadmap items that hangar would need | Task budgets and approval policies, SPIFFE identity, custom harness, branching, a migration to "the new Actor" API | Forking, user authorization, an S3 plugin, a gVisor upgrade story, audit logging |
| Known rough edges | Tasks are immutable once created; `ax ssh` needs debug mode; the runner doesn't read command exit codes back ("The control plane does not currently read the command's exit status back") | Demo workarounds for open atelet bugs; node-label pitfalls on autoscaling |

## 8. Provider design: `providers/ax.py`

Hangar's `Provider` interface ([`providers/base.py`](../../src/hangar/providers/base.py)) is small on purpose: "everything above this layer … is built on `exec`, `read_file` and `write_file`". Here is how each method would map onto AX:

| `Provider` method | On AX | Notes |
| --- | --- | --- |
| `capabilities` | `pause=True`, `full_snapshots=False`, `warm_pools=True` (Substrate golden snapshots and WorkerPools), `microvm=True` if the template uses the microVM class, `local_build=False`, `image_volumes=False` | AX exposes no snapshot or fork RPC |
| `build_template` | Not AX's job: build the environment image as today (Cloud Build to `HANGAR_REGISTRY`), **plus** a layer that adds `/usr/local/bin/ax-task-runner` | Harness packs can't be image volumes; bake them in (`k8s_image_volumes=false` path) |
| `create(sandbox, image, spec)` | `CreateTask` with `image`, `command: ["sleep", "infinity"]` (hangar drives the work), `resources` from `sandbox.resources`, `env`, `debug: true`, and one Workspace with **no** git entries (hangar clones with its own credential flow). Wait for condition `Ready`. Set `provider_ref = "<atespace>/<task>"` | Task names must be RFC 1123 labels. One AX atespace per hangar deployment |
| `status` | `GetTask` phase → `Running` → RUNNING, `Suspended` → PAUSED, `Failed` → ERROR, `Terminating` → STOPPING | |
| `destroy` | `DeleteTask` (blocks until the actor is gone) | |
| `pause` / `resume` | `SuspendTask` / `ResumeTask` | Today this keeps `/workspace` only; processes restart. So pause is not hot. |
| `list_refs` | `ListTasks` in the atespace | For reconciliation |
| `exec` | Substrate guest **Process** service over gRPC through `atenet-router.ate-system`, with metadata `ate-target-actor: <atespace>/<task>` ([networking](https://github.com/google/ax/blob/main/docs/networking.md)). Start `bash -lc <command>`, stream output, wait for the exit code | Needs Python stubs generated from [agent-substrate/env](https://github.com/agent-substrate/env) protos; no Python client is published. Needs `debug: true`. |
| `read_file` / `write_file` | Guest **File system** service (streaming read and write) | Same |
| `open_terminal` | Process service with a PTY (what `ax ssh` uses) | |
| `explain_pending` | Task conditions (`WorkspaceReady`, `Ready`) and the reason | |
| `set_expiry` | none: hangar's reaper enforces TTLs, as on Docker | AX lists "token/timeout budgets" on its roadmap |
| `snapshot` / `delete_snapshot` | not available; `fork_session` keeps using 0010 checkpoints | "Stateful Task Branching" is on the roadmap |

**What's missing or awkward:**
- **A control path for exec and files.** The only one is debug-mode guest services. We'd be building on an interface AX describes as for debugging, with arbitrary process execution open to anyone who can reach the router with the actor header. The router must be locked to the hangar control plane by NetworkPolicy, and Substrate's own "user authorization" is still a roadmap item.
- **Immutable Tasks.** Hangar's `extend` and TTL changes are control-plane-only anyway, but nothing about a running Task can change.
- **No memory snapshots through AX.** AX suspends with `/workspace` fidelity, so it buys nothing over hangar's own checkpoints for resume quality.
- **Two orchestrators.** AX's Redis state, workspace bootstrap, `Model`s and goal-driven Antigravity setup are all unused, but deployed and upgraded, and they break with every v1alpha1 change.
- **Cluster prerequisites** (§6) and no GKE Agent Sandbox features (warm pools as `SandboxWarmPool`, Pod Snapshots).

**What it would buy:**
- **Density:** Substrate's multiplexing of idle actors onto shared workers ("30x+ oversubscription" in the [demo](https://github.com/agent-substrate/substrate#demo)). Coding sessions are idle most of the time, waiting on the model or on a human, which is exactly the pattern it targets. Hangar today pins a pod per live sandbox and releases it only on suspend.
- **Start time:** golden-snapshot restores instead of a pod start. Hangar's cold path is node scale-up (2–4 min on GKE) plus an image pull ([config](../../src/hangar/config.py)); warm pools hide it only for prebuilt environments.
- **Hot suspend:** only via Substrate `MEMORY` fidelity, which AX doesn't expose today.

**Why Substrate directly is the better target.** `providers/substrate.py` would use the same guest services for exec and files. It would create Actors from an `ActorTemplate` per environment image, with `preferredFidelity: MEMORY`. It would suspend and resume through `ateapi`, and so get *hot* resume. The router already "resumes suspended Actors on demand", which fits sessions. There would be no AX server, no Redis and no workspace bootstrap to carry. Hangar stays the orchestrator, and AX remains what it is: a peer we talk to over A2A.

## 9. Triggers for revisiting

Revisit this when any of these happens:

1. **Substrate actor forking ships** (roadmap "Coming Soon"). Memory-level `fork_session` and best-of-N (workstream D) would then be a few hundred milliseconds instead of N cold sandboxes.
2. **Substrate gets a supported, authorized exec and file API**, not debug-mode guest services, and a Python client, or "user authorization" lands on the control plane.
3. **AX exposes `MEMORY` fidelity** (its "Migration to the New Actor" item) **and** v1 of its API. Only then does `providers/ax.py` beat `providers/substrate.py`.
4. **Hangar's own numbers say so:**
   - more than about 30% of session resumes or job starts pay a cold pod start;
   - or idle sandboxes held for `keep_minutes` cost more than the sessions bucket and a Postgres would;
   - or a 0010 M4 Pod Snapshots trial shows we need hot resume and GKE Pod Snapshots can't deliver it.
5. **GKE ships Substrate as a managed add-on**, or Agent Sandbox and Substrate converge, which would remove the cluster-recreate cost in §6.
6. **A customer or partner runs AX** and wants their agents to use hangar. That needs nothing new: point them at [a2a.md](../a2a.md).

## Ideas worth borrowing now

These are already in §4 and still hold, with one addition:
- **Declarative workspaces that pre-wire MCP servers and skills:** 0006 `mcp:` plus a skills registry.
- **A single-writer event log for run state:** workstream C's events.
- **Branching from checkpoints:** workstream D's `fork_session`.
- **New: goal-driven environment setup.** AX hands a plain-language `goal` ("install dependencies and run the test suite") to an agent on first boot and records a marker so it never reruns. Hangar could do the same for environments without a build recipe: run a setup turn, then snapshot the result as the environment's image.
