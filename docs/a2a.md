# A2A: hand hangar work from another agent

Hangar is an [A2A](https://a2a-protocol.org/) agent. An agent built with Google ADK, an agent running on AX, or any other A2A client can send hangar a coding task and get back a branch, a pull request, a summary and (when asked for) structured data. It doesn't need to know about hangar's REST API or MCP tools. The design is in [spec 0011](specs/0011-factory-roadmap.md) §3, workstream G.

```
A2A client ──SendMessage──► /a2a ──► job (queue → sandbox → agent → verify → PR)
     ▲   ◄── Task, status updates (SSE), push notifications ──┘
     └── SendMessage with the same contextId ──► follow-up turn on the same job and branch
```

## Protocol version

Hangar implements **A2A 1.0** ([specification](https://a2a-protocol.org/v1.0.0/specification/), [`a2a.proto`](https://github.com/a2aproject/A2A/blob/main/specification/a2a.proto)) over the **JSON-RPC binding**. Because many clients still speak 0.3, the same endpoint also accepts **A2A 0.3** ([specification](https://a2a-protocol.org/v0.3.0/specification/)).

| | 1.0 | 0.3 |
| --- | --- | --- |
| Selected by | `A2A-Version: 1.0`, or a 1.0 method name with no header | `A2A-Version: 0.3`, or a 0.3 method name with no header |
| Methods | `SendMessage`, `SendStreamingMessage`, `GetTask`, `ListTasks`, `CancelTask`, `SubscribeToTask`, `Create`/`Get`/`List`/`DeleteTaskPushNotificationConfig(s)`, `GetExtendedAgentCard` | `message/send`, `message/stream`, `tasks/get`, `tasks/cancel`, `tasks/resubscribe`, `tasks/pushNotificationConfig/set`/`get`/`list`/`delete`, `agent/getAuthenticatedExtendedCard` |
| Wire format | ProtoJSON: parts as `{"text"}`, `{"raw"}`, `{"url"}`, `{"data"}`; states such as `TASK_STATE_WORKING`; stream items wrapped as `{"task"}`, `{"statusUpdate"}`, `{"artifactUpdate"}` | `kind` discriminators; states such as `working`, `input-required`; `final` on status updates |

Other `A2A-Version` values get `VersionNotSupportedError` (-32009). The gRPC and HTTP+JSON bindings are not served.

## Discovery and auth

- **Agent card:** `GET /.well-known/agent-card.json` (public, cached for 5 minutes). It lists both interfaces, `streaming: true`, `pushNotifications: true`, a bearer security scheme and two skills:
  - **`code_change`**: make a change and deliver a branch and a draft PR (the default);
  - **`answer_question_about_repo`**: read the repo and answer without changing it (no branch or PR).

  The card carries the 0.3 fields too (`url`, `protocolVersion`, `preferredTransport`, the 0.3 form of the security scheme), so 0.3 and 1.0 clients both read it.
- **Endpoint:** `POST /a2a`, with `Authorization: Bearer <hangar token>`.
- **Tokens:** an admin token, or a `jobs`-scoped token with the same fence as the REST API ([jobs.md §1](jobs.md#1-give-a-service-a-token)). A `jobs` token can work only on repos in `HANGAR_GITHUB_REPOS`, in their mapped environments, can't pick a provider, and sees only its own tasks.
- A missing or unknown token gets HTTP 401 with `WWW-Authenticate: Bearer realm="hangar"`. A token without the `jobs` or `admin` scope gets 403.

```bash
hangar token create adk-lead --scope jobs     # prints the token once
```

## Sending work

A message turns into a job like this:

| A2A | Hangar |
| --- | --- |
| Text parts | The instructions (joined with blank lines). A message needs at least one. |
| File parts (`raw`, or 0.3 `file.bytes`) | Context files in `~/.hangar/jobs/<job>/`, under their `filename` |
| URL parts (`url`, or 0.3 `file.uri`) | Links listed in the prompt (hangar doesn't fetch them) |
| Data parts | Context files `data-<n>.json` |
| `metadata` on the request or the message | Options: `repo`, `base_branch`, `environment`, `harness`, `model`, `verify`, `deliver`, `title`, `timeout_minutes`, `keep_minutes`, `skill`, `output_schema`. Keys may be top level or under `"hangar"`, in snake_case or camelCase. |
| `metadata.schema` on a text part ([spec §6.8](https://a2a-protocol.org/v1.0.0/specification/#68-structured-data-exchange)) | `output_schema` |
| `messageId` | Idempotency key: resending a message returns the same task |
| `contextId` you choose | Kept on the job (label `a2a.context`), so later messages in that context find it |

With no `repo`, hangar uses `HANGAR_A2A_DEFAULT_REPO`, and then the environment's repo. Limits are the jobs limits: 64 KB of instructions, 50 context items and 2 MB of context, plus 100 parts and `HANGAR_A2A_MAX_REQUEST_BYTES` per request. `output_schema` is rejected with a clear error until structured results land (spec 0011 §2, workstream E).

`SendMessage` blocks until the task finishes or needs input, as the spec asks, for up to `HANGAR_A2A_MAX_WAIT_SECONDS` (30 minutes), and then returns the task as it stands. Coding jobs take minutes, so prefer `SendStreamingMessage`, push notifications, or `configuration.returnImmediately: true` (0.3: `blocking: false`) and polling with `GetTask`.

## Tasks, contexts and follow-ups

**One task is one job, and the job is also the task's context.** The task id is the job id. Unless the client chose its own `contextId`, the context id is the job id too.

- **Follow-up:** send a message with the task's `contextId` and no `taskId`. Hangar runs a follow-up turn on the same job: same branch, same sandbox or session, and the agent resumes its own conversation. The response is the same task, working again. (The spec would let a server open a new task in the context; hangar keeps one, because a job has one branch and one PR.)
- **Answering a question:** when the task is `input-required`, reply with its `taskId` (and `contextId`), as the spec describes. That is a follow-up too.
- A message with the `taskId` of a finished task is refused (`UnsupportedOperationError`, as the spec requires), and so is a message to a task that is still working. Hangar can't add a message to a turn that is already running.

| Job state | Task state |
| --- | --- |
| `queued`, `awaiting_approval` | `TASK_STATE_SUBMITTED` |
| `provisioning`, `running`, `verifying`, `delivering` | `TASK_STATE_WORKING` |
| `needs_input` | `TASK_STATE_INPUT_REQUIRED`, with the agent's question as the status message |
| `succeeded` | `TASK_STATE_COMPLETED` |
| `failed` | `TASK_STATE_FAILED`, with the cause in the status message |
| `cancelled` | `TASK_STATE_CANCELED` |

`awaiting_approval` maps to `submitted`, not `auth-required`. A maintainer approves the job, and a client that sees `auth-required` would ask its user for credentials, which wouldn't help.

What comes back on a task:

- **Status message:** the latest job event, such as "agent turn 1 starting (claude-code)" or "succeeded; 2 commit(s) on hangar/job-…; https://github.com/…/pull/12".
- **Artifacts**, each with a stable id:
  - `Summary`: the agent's `RESULT.md` (secrets redacted);
  - `Pull request` (or `Branch` if no PR was opened): a text part with the link, and a data part with `prUrl`, `prNumber`, `branch`, `baseBranch`, `headSha`, `commits` and `verifyExitCode`;
  - `Diffstat`;
  - `Structured result`: a data part, once structured results exist.
- **History:** what was asked (the instructions, then each follow-up) and each turn's summary. Use `historyLength` to trim it.
- **`metadata.hangar`:** the job id, state, reason, repo, branch, environment, harness, model and number of turns.

## Streaming and push notifications

`SendStreamingMessage` and `SubscribeToTask` stream Server-Sent Events. The first item is the task. Then comes one status update per job event (provisioning, agent turn starting, verifying, delivering, …), then the artifacts, then the final status. The stream closes when the task finishes or needs input. Idle streams get an SSE comment every 15 seconds, so proxies don't close them.

For push notifications, set `configuration.taskPushNotificationConfig` (0.3: `pushNotificationConfig`) on the message, or call `CreateTaskPushNotificationConfig`. Hangar POSTs to the URL whenever the task state changes:
- a `statusUpdate` while the task works;
- the full `task` (with artifacts) when it finishes or needs input;
- for configs made over 0.3, the 0.3 task each time.

Each POST carries:
- `Authorization: <scheme> <credentials>` from the config;
- `X-A2A-Notification-Token: <token>`;
- for `jobs` tokens with a bound callback key, the same `X-Hangar-Signature` as [job callbacks](jobs.md#3-verify-callbacks).

Push URLs are checked like job callbacks: public `https` hosts only, unless the host is in `HANGAR_JOB_CALLBACK_HOSTS`, and the name is resolved again before each send. Delivery is at least once (three tries per state). Token and credentials are stored encrypted in the vault, and `Get`/`List` never return them.

## Examples

### curl

```bash
curl -sS https://hangar.example.com/a2a -H "Authorization: Bearer $HANGAR_TOKEN" -H 'A2A-Version: 1.0' \
  -H 'Content-Type: application/json' -d @- <<'EOF'
{"jsonrpc": "2.0", "id": 1, "method": "SendMessage", "params": {
  "message": {"messageId": "4b1c…", "role": "ROLE_USER", "parts": [
    {"text": "CSV export drops non-ASCII characters. Fix it and add a regression test."},
    {"raw": "VHJhY2ViYWNrIChtb3N0IHJlY2VudCBjYWxsIGxhc3QpOiAuLi4=", "filename": "trace.txt", "mediaType": "text/plain"}]},
  "metadata": {"repo": "https://github.com/acme/app", "environment": "python", "verify": "pytest -q"},
  "configuration": {"returnImmediately": true}}}
EOF
```

### Google ADK

ADK's [`RemoteA2aAgent`](https://adk.dev/a2a/quickstart-consuming/) makes hangar a sub-agent. It works with `a2a-sdk` 1.x (A2A 1.0) and 0.3.x. Give it an HTTP client that sends the token, and name the repo through request metadata:

```python
import os

import httpx
from a2a.client import ClientConfig, ClientFactory
from google.adk.agents import Agent
from google.adk.agents.remote_a2a_agent import AGENT_CARD_WELL_KNOWN_PATH, RemoteA2aAgent

hangar_http = httpx.AsyncClient(
    headers={"Authorization": f"Bearer {os.environ['HANGAR_TOKEN']}"},
    timeout=httpx.Timeout(30.0, read=None),  # streams stay open while the coding agent works
)

hangar = RemoteA2aAgent(
    name="hangar",
    description="Makes code changes in acme/app in a sandbox and opens a pull request.",
    agent_card=f"https://hangar.example.com{AGENT_CARD_WELL_KNOWN_PATH}",
    a2a_client_factory=ClientFactory(ClientConfig(httpx_client=hangar_http, streaming=True)),
    a2a_request_meta_provider=lambda ctx, message: {"repo": "https://github.com/acme/app", "environment": "python"},
)

root_agent = Agent(
    model="gemini-2.5-flash",
    name="lead",
    instruction="Triage bug reports. Hand code changes to the hangar agent, then report its pull request.",
    sub_agents=[hangar],
)
```

`AGENT_CARD_WELL_KNOWN_PATH` is `/.well-known/agent-card.json`. ADK keeps the task's `contextId`, so the next delegation in the same ADK session is a follow-up on the same job and PR. When hangar asks a question (`input-required`), ADK sends the user's answer with the `taskId`, and that continues the job. To skip request metadata, set `HANGAR_A2A_DEFAULT_REPO`, or map the repo for the token in `HANGAR_GITHUB_REPOS`.

### The A2A Python SDK

```python
from a2a.client import ClientConfig, ClientFactory
from a2a.types import Message, Part, Role, SendMessageRequest
from google.protobuf.struct_pb2 import Struct

client = await ClientFactory(ClientConfig(httpx_client=hangar_http)).create_from_url("https://hangar.example.com")
meta = Struct()
meta.update({"repo": "https://github.com/acme/app"})
async for event in client.send_message(SendMessageRequest(
        message=Message(message_id="m1", role=Role.ROLE_USER, parts=[Part(text="Fix the CSV unicode bug")]),
        metadata=meta)):
    print(event)
```

## Settings

| Variable | Default | |
| --- | --- | --- |
| `HANGAR_A2A_ENABLED` | `true` | Serve the agent card and `/a2a` |
| `HANGAR_A2A_DEFAULT_REPO` | | Repo for tasks that don't name one |
| `HANGAR_A2A_POLL_SECONDS` | 1.0 | How often streams and push notifications check for job changes |
| `HANGAR_A2A_MAX_WAIT_SECONDS` | 1800 | How long a blocking `SendMessage` waits |
| `HANGAR_A2A_MAX_REQUEST_BYTES` | 4 000 000 | Largest request body |
| `HANGAR_A2A_MAX_PUSH_CONFIGS` | 5 | Push notification configs per task |

The card's URLs come from `HANGAR_PUBLIC_URL`.

## Known limits

- **Follow-ups keep the task id.** A follow-up reopens the same task instead of creating a new one in the context (see above).
- **`ListTasks` cursors are offsets.** Its `pageToken` is an opaque offset, not a stable cursor. Tasks that change between pages can move.
- **No extended agent card**, and no gRPC or HTTP+JSON binding.
- **Attachments on follow-ups** need workstream E's follow-up context. Until then a follow-up takes text only.
- **No per-agent identity yet.** Push notifications aren't signed for admin tokens, and per-agent identity (SPIFFE, signed agent cards) isn't implemented.
