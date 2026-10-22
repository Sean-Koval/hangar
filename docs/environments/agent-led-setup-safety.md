# Agent-led setup: re-runs, budget and safety

When [agent-led setup](agent-led-setup.md) runs again, which models it uses and what it may spend, and what its
sandboxes may hold.

## Re-running on drift

`mode: drift` (`--drift`; the schedules `--every` creates) runs setup only when the repo's environment is broken.
That is when the default branch has no config or an invalid one. It is also when the repo's latest environment build
failed (after a lockfile or CI change).

It doesn't run while an earlier env-setup PR for the repo is still open. Otherwise it ends at once (`no_changes`), with
no agent started. The agents get the failing build's output and update the existing file.

## Models and budget

Both agents run on the `balanced` model policy, the same one jobs default to
([models](../harnesses.md#models-the-latest-strong-model-per-provider)). It is resolved from the provider's model list
when each agent's session is created, and pinned there. The list is read with `HANGAR_MODEL_LIST_CREDENTIALS`, or is
OpenRouter's public list for pi.

A job's own `model` wins, and `HANGAR_ENV_SETUP_MODELS` pins a model or a policy per harness. The job's events, its
report and the PR's packet name the id, the policy and its list price. They also say where the id came from: a live
list, the last id resolved, or hangar's built-in one.

The job budget is the job's, else the repo policy's, else `HANGAR_ENV_SETUP_MAX_COST_USD` and
`HANGAR_ENV_SETUP_MAX_TURNS`. The goal job gets a quarter of it (the proposal and the judging; at least one turn). The
setup job gets the rest (every attempt), so together they can't exceed it.

A budget of fewer than 2 turns is refused. When the goal job's turns run out, later outputs are judged by their exit
codes, and the job's events say so.

## The PR holds what was validated

The packet names the commit hangar validated. If the setup branch's head has moved since, the PR isn't opened and the
job fails `validation_stale`. Neither agent's sandbox nor the validation sandbox can push (fetch-only grants), so that
takes someone else with write access. `.hangar/**` is high risk in every PR's packet.

## Safety

The validation sandbox runs code an agent wrote on an unmerged branch, so it gets less than a build. It holds a
fetch-only grant of the git gateway for its clone (without the gateway, no git credential at all), and nothing else.

It has no credential of the environment, none of its `secrets:`, no cloud tokens and no build secrets. Its egress is
the environment's and the repo policy's, as for any sandbox.

The file it validates can't name what an admin decides (refused like any `.hangar/environment.yaml`). Its services
must be on `HANGAR_ENV_SERVICE_IMAGES`. `mocks` are documentation only, shown as inline code in the PR and its packet.
The PR lands through the landing path like any agent PR: a person approves it.

A repo whose install needs a private registry token fails validation and names the token. An admin then maps it as a
[build secret](build-secrets.md#build-secrets) for the merged file.

The env-setup job holds no job slot while it waits. A background task drives its stages and keeps its state in the
job's `result.data`, so a restart resumes it. A follow-up to it answers whichever of its agents asked a question.
Cancelling it cancels both agents and removes the validation sandbox.
