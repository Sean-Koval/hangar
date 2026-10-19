# The overnight stack: upgrade, prove it, roll back

Steps 4–6 of [upgrading from before the overnight stack](overnight-stack.md) (#48–#69), and what couldn't be checked without GKE.

## 4. Upgrade

```bash
git pull                      # main with the stack merged
uv sync                       # the laptop CLI: hangar admin, config, setup, connect are new
bash deploy/gke/up.sh
```

**What `up.sh` does now:**

- It builds `control-plane:<short sha>` with Cloud Build, including the Rust stage.
- It applies `deploy/k8s` with two files the kustomization now lists:
  - `sandbox-network.yaml`: the sandboxes' egress baseline (DNS, plus the internet minus private, CGNAT, reserved and link-local ranges), and a deny-all-ingress policy for sandbox pods (#48, #62);
  - `envd-network.yaml`: hangar's pods may connect to sandbox pods on port 49983 and nothing else (#60).

  Both are additive to GKE Agent Sandbox's managed policy, and Dataplane V2 enforces them. Sandboxes keep their internet access; other sandboxes can no longer open connections to them. hangar's own path in, exec through the API server, doesn't use the pod network.
- It sets the Deployment's `terminationGracePeriodSeconds: 45`.
- It leaves `HANGAR_SESSION_STORE` alone on an existing install: it fills in the bucket only when the `hangar` config map already names one, and a value in `hangar-site` wins anyway. Without either it prints a note: moving a running hangar to the bucket would strand the checkpoints of suspended sessions on the PVC.

The Deployment uses `Recreate`, so the URL answers 502 from the moment the old pod stops until the new one is ready. Sandboxes and running agent turns outlive the pod, as before, and the new pod re-attaches to them. A turn that started under the old version finishes as a one-shot turn: it can't be steered.

`terraform apply` isn't needed. The only terraform change is `microvm.tf`, an optional microVM node pool that is off by default (#64); a plan shows no changes besides a new `microvm_pool` output that is null. No new GCP API and no new IAM role are needed. Backups go to the session bucket, where the hangar service account already has `roles/storage.objectAdmin` (`deploy/terraform/gke/main.tf`). That covers writing, listing and deleting backups, and the server-side copies checkpoints now use.

**Steps after the rollout:**

1. **GitHub webhook events** (#65). Auto-fix needs `pull_request_review`, `pull_request_review_comment`, `check_run`, `check_suite` and `status`, on top of `issues` and `issue_comment`. Until the webhook sends them, `hangar doctor` fails with "github webhook <repo>: the webhook doesn't send …", and auto-fix never fires. Then either:
   - run `hangar doctor --fix`, which adds the events to the existing hook (only adding: other events and the TLS setting stay);
   - or run `hangar setup github` again, which also moves the GitHub settings into hangar's database.

   Both need the stored GitHub token to manage webhooks: on a fine-grained token, *Webhooks: Read and write*. Otherwise add the events in the repo's Settings → Webhooks.
2. **GitHub token permissions** (#65). Auto-fix reads failing Actions logs, which needs *Actions: Read* on a fine-grained token. GitHub doesn't open the check-runs API to fine-grained tokens. With one, auto-fix describes a failing check from its webhook payload and Actions log, without annotations; a classic token with `repo` gets those too ([jobs.md §4](../jobs/README.md)).
3. **New hangar token scopes** (#50, #51, #55, #61). Existing tokens keep working.
   - `mcp`: `/mcp` only. `hangar connect` mints these for MCP clients.
   - `metrics`: `GET /metrics` only.
   - `sandboxes`: the E2B API only, for the sandboxes the token creates.

   Mint them with `hangar token create <name> --scope <scope>`.
4. **Republish the Codex pack** (#69). #69 changed the wrapper script in Codex's harness pack, so the pack's tag changes. Run `uv run hangar pack publish codex` from a machine with Docker, as in [gke.md §3](../gke.md#harness-packs). Otherwise the first Codex sandbox after the upgrade builds the pack itself, with Cloud Build from the control-plane pod, and waits several minutes for it. Running sandboxes keep the pack they mounted. Environments that bake Codex into their image rebuild on their next use.
5. **Move repository settings that `ask` turns rely on into the environment** (#69, Docker only). On GKE nothing changes. On a Docker hangar, an `ask` or `deny-risky` turn ignores the repository's `.claude/settings*.json`, `.mcp.json` and skills: put allowed tools, rules and hooks in the environment's `agent_config.files: {.claude/settings.json: ...}`, MCP servers in `mcp:` and instructions in `agent_config.instructions` ([steering.md](../steering.md#hangar-owned-harness-settings)). A turn's warnings say what was left out.
6. **Optional manifests**, none of them in the kustomization:
   - `deploy/k8s/observability/` (#55) holds the Managed Prometheus or Prometheus Operator scrape config, which needs a `metrics` token in a Secret, and the OpenTelemetry Collector policy. Cloud Trace needs Google's collector and its own IAM roles ([observability.md](../observability.md)).
   - `deploy/k8s/e2b.yaml` (#61) gives E2B SDKs per-sandbox host names under your own domain. It needs the Gateway API on the cluster, Certificate Manager and a static IP ([gke.md §8](../gke.md#8-e2b-sdk-clients-optional)). Without it, SDKs work through the existing URL (`E2B_API_URL=https://<host>/e2b`, `E2B_SANDBOX_URL=https://<host>/e2b/sandbox`).
7. **Settings can move into the database.** `hangar config list` shows where each value comes from. Values in `hangar-site` keep winning; [gke.md §2a](../gke.md#2a-log-in-connect-and-configure) explains when to keep them there.

## 5. Prove it on your cluster

```bash
hangar login https://<your hangar> --token-file ~/.hangar/gke-admin-token   # if you haven't
bash scripts/acceptance/gke.sh                                              # free checks, ~5-10 min
bash scripts/acceptance/gke.sh --gateway-repo you/scratch-repo              # + the git gateway, if HANGAR_GATEWAY_URL is set
bash scripts/acceptance/gke.sh --with-model --session-env <env with a Claude credential>   # + paid turns
```

Each check prints PASS, FAIL or SKIP with its time:

| Check | What it proves |
| --- | --- |
| `doctor` | `hangar doctor` reports no failures (warnings are listed) |
| `k8s` | the new NetworkPolicies exist in `hangar-sandboxes`, the grace period is 45, the pod is ready |
| `sandbox` | a `minimal` sandbox starts |
| `exec` | 20 exec round trips: their p50/p95 from your laptop, and the transport they used (from `/metrics`: `envd` or `exec`). It fails unless all of them went through hangar-envd (`--no-expect-envd` relaxes that). It also measures **inside the control-plane pod**: 20 × `true` through pods/exec, then 20 × through hangar-envd, the in-cluster comparison spec 0013 asked for. |
| `stream` | a streamed exec over Server-Sent Events, through the ingress (#68): the first line arrives while the command still runs, so nothing in between buffers the stream. It fails when the command wasn't streamed through hangar-envd (`--no-expect-envd` relaxes that). |
| `files` | a 1 MiB file written and read back intact through MCP `write_file`/`read_file` |
| `isolation` | from sandbox A: sandbox B's hangar-envd port, the metadata server (169.254.169.254:80), GKE's Workload Identity metadata server (169.254.169.252:988), hangar's Service and hangar's API port on its pod are all closed, while the internet is open |
| `session` | a session with no model turn: a commit, a change and an untracked file, then checkpoint, suspend (its sandbox goes) and open (a new sandbox restores all three), each timed |
| `model` | `--with-model`: a short real turn whose answer contains a requested word, with its cost |
| `steer` | `--with-model`: a live turn with `approvals=ask`. The agent's command waits for an approval, a steer message is delivered while it waits, the command runs once allowed |
| `gateway` | `--gateway-repo`: the session's sandbox is in gateway mode with no GitHub token in it; a push to `hangar/accept-<run>` goes through and a push to `accept-<run>-outside`, outside the sandbox's grant like `main`, is refused by the gateway. It never pushes to `main`: if the gateway let a push through, the run's own branch takes it. Both branches are deleted afterwards with `gh`, if you have it. |
| `e2b` | the stock `e2b` SDK (if installed: `uv sync --extra e2b`) with a `sandboxes` token: create, run, files, list, kill |
| `backup` | `hangar admin backup` now and in the list, stored in `gs://` |
| `metrics` | `/metrics` serves hangar's series, and a `metrics` token reads it and nothing else |
| `power` | hangar's power state answers. With the sandbox pools on: what the last power-up pulled and the warm pools. With them off: a create is refused at once (HTTP 503) with when they're back |
| `warmstart` | two creates back to back on `--warm-env` (default `claude-python`, which keeps a warm pool): the first claims the warm pod, the second starts cold; both times are printed, and `--record FILE` keeps them as JSON lines |

The script runs `power.sh` only with `--power-cycle`, which powers the pools down (checking the refusal) and up again, timed, and refuses while others are working ([acceptance.md](../acceptance.md)). Everything the script creates is named `accept-<run>` and is removed at the end, also after a failure or Ctrl-C (`--keep` leaves it). `tests/test_acceptance_script.py` runs the script against a hangar on Docker, with GitHub replaced by a local git server behind the gateway. Every check but `k8s` and `steer` runs there for real, `model` with a scripted agent in place of Claude. `k8s`, `steer` and the in-cluster exec measurement run against stand-ins: a fake kubectl, a fake API, and the Docker provider in place of the Kubernetes one.

## 6. Rolling back

Put the previous image back:

```bash
kubectl -n hangar set image deploy/hangar hangar="$(cat ~/.hangar/pre-upgrade-image)"
kubectl -n hangar rollout status deploy/hangar
```

The tag is `control-plane:<short sha>` of the commit you deployed from, `control-plane:2e3e0a3` if that was main at `2e3e0a3`. Images stay in Artifact Registry. `kubectl -n hangar rollout undo deploy/hangar` does the same, and also restores the old grace period.

**What keeps working:** the database as it is (§2), every session, and every checkpoint, old or new (§2). Leave the new NetworkPolicies in place. The old version reaches sandboxes through exec, so the deny-ingress and envd policies don't affect it, and the egress baseline only allows.

**What stops working:**

- **Settings stored with `hangar config set` or `hangar setup`** are ignored: the old version reads only its environment. If `hangar setup github` moved your GitHub settings into the database, put them back in `hangar-site` before you roll back, or GitHub jobs stop.
- **Tokens with the new scopes** (`mcp`, `metrics`, `sandboxes`) are refused: the old version lets non-admin tokens reach only `/api/jobs`. MCP clients set up with `hangar connect` need an admin token until you upgrade again.
- **Everything new is gone:** steering and approvals, envd (daemons left running in sandboxes are ignored), streaming (background processes started with `process` keep running in their sandboxes, unseen), the E2B API, `/metrics`, auto-fix, backups (the files in `_backups/` stay), MCP tasks and the new MCP features.
- **Turns that are running** when you roll back were started under the new driver. Suspend sessions first, as `power.sh down` does, or let running turns finish.

## What couldn't be checked without GKE

These were checked on Docker and with fakes only. The acceptance script checks each on the cluster:

- **hangar-envd under gVisor** (runsc on GKE), streamed in over pods/exec and reached at the pod IP through Dataplane V2 with `envd-network.yaml`: the `exec` check, which fails when calls don't go through envd.
- **How the new NetworkPolicies interact with Agent Sandbox's managed policy**: the `isolation` check, from inside a sandbox.
- **GKE's exec latency, and envd's on GKE** (spec 0013's open item): the `exec` check prints both.
- **Backups and server-side copies in the bucket through Workload Identity**: the `backup` check, and the `session` check's checkpoint (each checkpoint after the first copies its unchanged parts).
- **E2B streaming through the Google load balancer**: the `e2b` check.
- **Server-Sent Events through the Google load balancer, unbuffered**: the `stream` check.
- **Following a turn's log under gVisor** (#68). In a Docker container GNU `tail -F` polls, hence its `-s 0.1`; gVisor wasn't measured. The `model` and `steer` checks (`--with-model`) run followed turns, but don't time how late their events arrive.
- **Cloud Build time with the Rust stage**: watch the first `up.sh`.
