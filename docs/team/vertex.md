# Claude Code on Vertex AI

How a team runs Claude Code jobs on Claude models served by Google Cloud's Vertex AI, billed to a GCP project. The
rest of the team setup is on [team setup](../team.md).

## When to use it

Use it when your company buys Claude through Google Cloud rather than from Anthropic. A Vertex credential is an
API-key-class login: jobs, schedules and webhooks take it. A subscription login never is
([subscription logins](subscription-logins.md)).

## Prepare the project

An admin of the GCP project does this once:

1. Enable the Vertex AI API (`aiplatform.googleapis.com`).
2. Enable the Claude models you want in Model Garden, and accept their terms.
3. Make a service account for hangar's jobs, and grant it a custom role that holds only
   `aiplatform.endpoints.predict` on the project.
4. Bind it to hangar's issuer (preferred), or make a key for it.

```bash
gcloud iam roles create hangarClaude --project PROJECT --permissions aiplatform.endpoints.predict
```

`roles/aiplatform.user` works too, but it is wider. It can also start training jobs, endpoints and batch
predictions. The agent can read a stored key and reach Vertex AI, so that spend would be outside every hangar budget.

Quota is per model and region. Check it before many jobs run at once.

## Without a stored key (preferred)

An environment with `cloud: gcp` gets a short-lived token per sandbox instead of a key ([short-lived cloud
credentials](../security/credentials.md#short-lived-cloud-credentials)). A key is readable by the agent and lives as
long as the key does; the token dies within the hour.

Set `CLAUDE_CODE_USE_VERTEX: "1"`, `ANTHROPIC_VERTEX_PROJECT_ID` and `CLOUD_ML_REGION` in that environment's `env:`.
Grant the narrow role to the federated principal, or to the service account it impersonates. hangar stores
no credential then, so `hangar doctor` has no line for it.

## With a service-account key

Store the key as a team credential for Claude Code:

```bash
hangar cred add claude-vertex --team --harness claude-code --kind generic --file sa-key.json
hangar cred check claude-vertex
```

A sandbox that gets it runs Claude Code with these set:

| Variable | Value |
|---|---|
| `CLAUDE_CODE_USE_VERTEX` | `1` |
| `ANTHROPIC_VERTEX_PROJECT_ID` | the key's project, unless the environment sets it |
| `CLOUD_ML_REGION` | `global`, unless the environment sets it |
| `GOOGLE_APPLICATION_CREDENTIALS` | the key file, readable by the sandbox user only |

To bill another project than the key's, set `ANTHROPIC_VERTEX_PROJECT_ID` in the environment's `env:`. To use a
region, set `CLOUD_ML_REGION` there, for example `us-east5`.

Adding a Vertex key doesn't move existing work to Google. An environment that names no credential keeps the team's
other Claude Code login, and gets the Vertex key only when it is the only one. To choose Vertex, name the key in the
environment's `credentials:`. An environment that names both gets Vertex, and the sandbox says so in its warnings.

## Egress

A Claude Code sandbox with a Vertex credential, or whose environment sets `CLAUDE_CODE_USE_VERTEX`, may reach:

- `aiplatform.googleapis.com`, Vertex AI's global endpoint;
- `oauth2.googleapis.com`, where the key's token is refreshed;
- `<region>-aiplatform.googleapis.com` for `CLOUD_ML_REGION`, and for each `VERTEX_REGION_CLAUDE_*` override.

Nothing else is added. These hosts show with source `harness:vertex` in `GET /api/egress/sandboxes/{ref}`.

## What doctor checks

The `claude on vertex` line checks each Vertex credential with one cheap call. gcloud gets a token for the key, which
Google refuses for a deleted or disabled key. Then Resource Manager's `testIamPermissions` asks whether the account
has `aiplatform.endpoints.predict` on the key's project, not one the environment names. That call reaches no model
and costs nothing.

- **fail**: Google refused the key, or the account lacks the permission.
- **warn**: not checked yet, or the second call didn't answer.
- **ok**: the key works and may call Claude on Vertex AI.

Doctor rechecks a result older than an hour. The check can't see whether the Claude models are enabled in Model
Garden. A job's first turn shows that.

## Usage and cost

Claude Code reports Vertex model ids such as `claude-sonnet-4-5@20250929`. `/api/usage` prices them as the same
model on Anthropic's list ([cost reporting](../cost-reporting.md)). Google's invoice is the bill of record.

## Not covered yet

- The model gateway doesn't route to Vertex. With `model_gateway: record`, a Vertex key isn't written into the
  sandbox, and an environment that sets `CLAUDE_CODE_USE_VERTEX` is refused.
- Bedrock has no credential kind or egress preset.
- An end-to-end job on repo 1 needs a real project, and is run by the owner.
