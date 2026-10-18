# The model gateway (record mode)

Replay-as-fork, steer-point replay and the nightly determinism report need what the model actually answered. With
`model_gateway: record`, a sandbox's harness calls its model provider through hangar's gateway instead of directly:
the API key stays in the control plane, and every request and response is kept as a **tape**: normalized, redacted
and hash-chained as it is recorded. [Replay](#replay) serves a tape back to rebuild a run by re-execution (#179).

Record mode is for automated runs on API keys (part A's credential policy, [team.md](team.md)). A subscription login
(a Claude Pro or Max login, a ChatGPT `auth.json`) can't go through a recording proxy, and `record` refuses to start a
sandbox that holds one.

## Turning it on

1. The git gateway must be configured (`HANGAR_GATEWAY_URL`, [security.md](security.md#git-credential-gateway)): the
   model gateway serves `/model/{provider}/...` on the same port (8788), which sandboxes may already reach.
2. Set it on the environment:

   ```yaml
   name: evals
   harnesses: [claude-code]
   model_gateway: record   # off (the default): the harness calls its provider directly
   ```

3. Attach an API key for the harness (`hangar cred add anthropic --harness claude-code --kind api_key`), as before.

A sandbox of that environment then gets:

| Harness | Provider | In the sandbox |
| --- | --- | --- |
| `claude-code` | `anthropic` | `ANTHROPIC_BASE_URL=<gateway>/model/anthropic`, `ANTHROPIC_API_KEY=hgm_…` |
| `codex` | `openai` | `OPENAI_BASE_URL=<gateway>/model/openai/v1`, `CODEX_API_KEY=hgm_…` |

`hgm_…` is a per-sandbox bearer, minted like the git gateway's grants: stored hashed, compared in constant time, good
only for that provider, only while the sandbox runs, and revoked with the sandbox. The real API key never enters the
sandbox. Other harnesses (pi, Antigravity, vendor-loop agents) have no gateway path yet: their calls go direct, and the
sandbox carries a warning saying so. Codex's path is wired but not yet qualified against the real CLI; Claude Code's
is (`tests/test_modelgw.py`).

| Setting | Default | Meaning |
| --- | --- | --- |
| `HANGAR_MODEL_GATEWAY_UPSTREAMS` | unset | Where each provider's calls go, `{"anthropic": "https://api.anthropic.com"}` (unset: the provider's own API) |
| `HANGAR_MODEL_TAPE_DAYS` | 30 | How long tapes stay in `<data>/tapes/` (the bucket's lifecycle rule does the same in GCS) |

## What the gateway forwards

The request body goes upstream byte for byte, with every header except the hop-by-hop ones, the bearer and
`accept-encoding` (the gateway asks for an uncompressed stream so it can keep a copy). Prompt-caching fields
(`cache_control`) and `anthropic-beta` reach the provider untouched, so costs don't change. The real key goes as
`x-api-key` (Anthropic) or `Authorization: Bearer` (OpenAI). Responses stream back as they arrive, SSE included.

Only model calls are recorded (`POST` to `/messages`, `/responses`, `/chat/completions`); model lists and token
counts are forwarded and not recorded.

If the provider can't be reached, or the control plane restarts mid-call, the harness gets a provider-shaped 502 and
retries as after any provider 5xx; an attempt it gives up on is classified `infra_error` / `provider_5xx` (#166).
Recording a call never fails it: a tape problem is logged, and the answer still reaches the harness. Measured on the
scripted model API (`test_bench_the_gateway_adds_under_50ms_p50`), the gateway adds ~15 ms p50 per call.

## Tapes

A tape belongs to a run: a session's turn, `{session_id}.{run_id}` (a sandbox outside any session: `{sandbox}.sandbox`).
It is written to `<data>/tapes/<run>/tape/` as calls complete and, when the turn ends, to the outbox under
`v1/tapes/{date}/<run>/tape/` ([api/outbox.md](api/outbox.md#tapes)).

```
tape/00001.json   {seq, req_hash, req_delta, resp, resp_hash, prev_hash, at, usage}
tape/00002.json
...
tape/index.json   the chain's head, the run, provider, model, the harness manifest's digest, untrusted
```

**Normalized.** Volatile request headers are dropped (only `anthropic-version`, `anthropic-beta`, `openai-beta` and
`content-type` are kept). Tool-call and message ids (`toolu_…`, `call_…`, `msg_…`, `resp_…`), hangar's own ids
(`sb-…`, `ses-…`, `run-…`, `job-…`), timestamps and `/tmp/...` paths become stable placeholders (`<toolu:1>`, `<ts>`,
`<tmp>`): the same id is the same placeholder throughout the run, in requests and responses alike.

**Redacted before hashing.** Every string goes through the broker's redaction (stored credential values and their
encodings) and secretscan's masking (the token formats it knows), so a secret a tool printed and the agent sent back
to the model is `[hangar: secret removed]` in the tape, and the hashes are of the redacted form.

**Deltas.** A conversation's prefix repeats in every call, so `req_delta` is `{"base": seq, "append": [...]}`: the
messages (Anthropic) or input items (OpenAI) a request adds to an earlier one of the run (the longest match among the
last 8, since subagents interleave conversations), plus `set` / `unset` for other top-level fields and `outer` for a
changed path or header. A request that extends none is `{"full": ...}`. On the scripted model API a real Claude Code
turn's tape is ~46 KB gzipped.

**Hash-chained.** `req_hash` is the SHA-256 of the full normalized request (canonical JSON: sorted keys, no spaces),
`resp_hash` of the normalized response, and `prev_hash` the hash of the previous entry; the first entry's is the run's
genesis hash, `sha256("hangar.tape/v1\n" + run)`. An entry changed, dropped, added or reordered breaks the chain.
`hangar.modelgw.validate(<dir>/tape)` rebuilds every request from its deltas and checks every hash and the chain up to
`index.json`'s `head`.

**Responses.** A streamed response is kept as its events, `{"event": name, "data": ...}`, and re-chunked on replay;
`usage` comes from the final events (Anthropic's `message_start` and `message_delta`, OpenAI's `response.completed`).
A response past 64 MB, or one the harness hung up on, is marked `truncated`.

**Untrusted.** `index.json`'s `untrusted` is true, with `untrusted_reasons`, when the run's job has an untrusted
source (`untrusted_source`) or an untrusted context item, or when its taint fired ([security.md](security.md#taint)).
A tainted tape can replay an attacker's tool call: it can't serve as promotion evidence without review (flightdeck's
rule), and replays run with egress `none`.

## Replay

A replay (#179, `src/hangar/replay.py`) re-executes a recorded run in a fresh sandbox: the harness gets the tape's
answers instead of the provider's, runs every tool call for real, and each request it sends, which carries those
tools' outputs, is compared with the tape's request for that step. The first that doesn't match is the
**divergence**. A replay is made for a sandbox labelled with its id (`hangar.replay`) by a [fork at a
step](sessions.md#fork-at-a-step) or the [determinism report](observability.md#the-determinism-report); its plan and
progress are kept in `<data>/replays/<id>.json`, so a control-plane restart carries on.

| Mode | |
| --- | --- |
| `replay-strict` | Every request must be one of the tape's. A miss, or a request past the tape's end, is an error the harness can't retry (a provider-shaped 400), and the turn ends. With `tolerate: tool` (the determinism report), a request that differs only in tool outputs is recorded as a mismatch and the step served anyway, so one replay finds every step that doesn't reproduce. |
| `replay-prefix` | The tape's answers for steps 1..`at_seq` while each request matches; the request after step `at_seq` is checked too (it carries that step's tool outputs), then forwarded to the provider, recorded on a tape of its own: the run goes live from N. A divergence before that stops the replay (`on_divergence: stop`) or goes live there (`live`). |

**Matching.** Both sides are compared as the tape stores them (redacted, normalized with the run's placeholders), then
loosened: dates, durations (`0.53s`, `120ms`), UUIDs and hex addresses are replaced, and the request's `metadata`
(Claude Code's per-session user id) is dropped. Requests match in order within a window of the next 8 unserved steps,
since a harness's side calls interleave. Served answers get fresh concrete ids for their placeholders (`<toolu:3>`
becomes `toolu_hgr…`), registered so the harness's next request normalizes to the same placeholder. `<ts>`, `<tmp>`
and redacted secrets can't be restored and are served as written: a step whose command used one diverges.

**Kinds.** `model`: the requests differ outside tool outputs (another harness, system prompt or tool list; the first
differing field is named). `tool`: only a tool's output differs (the tool and its command or file are named).
`network`: a tool output differs and the replay's shows a network error, as it will with egress `none`. `tree`: the
working tree after an edit or command differs from the run's `tree` events (#165), checked when the replay's turn has
ended; new tapes' `index.json` carries `tool_ids` (the tool call ids behind the placeholders) for that.

**Side effects** (MCP tools, Jira, Slack, web fetch and search) are answered from the tape: the replay sandbox has no
MCP server, and the gateway puts the taped output in place of the replay's before comparing and, once live, before
forwarding upstream.

**Isolation.** A replay sandbox goes through the gateway whatever its environment's `model_gateway` says. It has
egress `none`, holds no credential (it is created with the harness's API key only, which the gateway takes out), no
`secrets:`, cloud token, MCP server, GitHub API or build cache, and its git grant is fetch-only.

## Limits of this version

- Vendor-loop agents and Gemini/Antigravity have no gateway path.
- While the pools are powered down there are no sandboxes, so no traffic.
