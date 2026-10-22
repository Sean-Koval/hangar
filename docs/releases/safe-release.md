# Secure intake and API keys: the safe release

Secure defaults for intake jobs, API keys for automated work, one approval rule, sealed graders, taint gates, new manifest fields and the `extends` merge rules. Every release is listed on [release notes](README.md).

The release that makes hangar safe to hand to a second engineer, shipped in v0.2.0. Intake jobs get boundaries that
hold when a prompt injection steers the agent. Automated work runs on API keys, every approval follows one
rule, and graders run sealed. A turn that read untrusted content can't push, land or open egress on the agent's word.

## Warn for one release, then enforce

The owner decided on 2026-10-17 that the security changes #123–#125 warn for one release, then enforce.

- **v0.2.0, this release,** enforces them by default. Two switches turn them back to warn:
  `HANGAR_INTAKE_POSTURE=warn` (#123, and #124 for intake jobs) and `HANGAR_SUBSCRIPTION_LOGINS=any` (#125). They let
  you upgrade before the gateway, the egress proxy and everyone's API keys are ready.
- **Under a switch** the work runs as before and says so: a `posture.warn` event, audit entry and review-packet line,
  or a warning on the manifest. `hangar doctor` reports it.
- **The next release** removes both switches (#223). It refuses intake jobs without the gateway, and jobs with only a
  subscription login. Unset the switches as soon as the steps below are done.

`sudo: false` (#124) has no switch. An environment whose people need root at runtime sets `sudo: true`.

```bash
hangar cred add claude-team --team --harness claude-code --kind api_key --file key.txt   # 1. API keys first
hangar config set HANGAR_GATEWAY_URL http://hangar-gateway.hangar.svc:8788               # 2. the gateway and
hangar config set HANGAR_EGRESS_URL http://hangar-gateway.hangar.svc:3128                #    the egress proxy
uv run hangar env migrate-merge --dry-run --data-dir /path/to/hangar-data                # 3. the merge dry run
uv run hangar images plan && uv run hangar images build --registry "$REG"                # 4. prebuild images
bash deploy/gke/up.sh                                                                    # 5. 01:00–09:50 ET
hangar doctor                                                                            # 6. all green
```

## What you configure

`hangar config set NAME VALUE` keeps a setting in hangar's database; `hangar-site` wins over it.

| Setting | Values (default first) | What it does | Step |
| --- | --- | --- | --- |
| API-key credentials: a person's own (`hangar cred add NAME --harness H --kind api_key`) or the team's (`--team`) | — | **Required** for every job, schedule, webhook, experiment and replay. They never use a subscription login (`oauth_token`, `auth_file`) | [1](#1-add-api-key-credentials) |
| `HANGAR_SUBSCRIPTION_LOGINS` | `interactive`, `any` | `interactive`: subscription logins only in a person's own sessions and sandboxes, with a warning. `any`: jobs use them too, with a warning, and `hangar doctor` fails. **This release only** (#125). Trials and graders never get one | [1](#1-add-api-key-credentials) |
| `HANGAR_GATEWAY_URL` | unset | The git gateway: job sandboxes fetch only, and hangar pushes. **Required** for intake jobs under `enforce` (#123), and for build caches and private repo builds. `deploy/k8s/config.yaml` sets it on GKE | [2](#2-turn-on-the-gateway-and-the-egress-proxy) |
| `HANGAR_EGRESS_URL` | unset | The egress proxy for `allowlist` and `none` environments and for intake jobs' sandboxes. **Required** on GKE for the intake jobs' allowlist floor. `deploy/k8s/config.yaml` sets it | [2](#2-turn-on-the-gateway-and-the-egress-proxy) |
| `HANGAR_INTAKE_POSTURE` | `enforce`, `warn`, `off` | `enforce`: an intake job without the gateway is refused at submit, with a message on its issue, ticket or thread. Its sandbox gets at least `allowlist` egress and cold-starts; on Docker it runs with `no-new-privileges`. A heavy environment needs the repo policy's `allow_heavy_for_intake`. `warn`: **this release only** (#123, #124). `off`: [not recommended](../security/front-doors.md#intake-posture) | [2](#2-turn-on-the-gateway-and-the-egress-proxy) |
| `HANGAR_SELF_APPROVAL` | `deny`, `allow_solo` | `deny`: nobody approves their own job, landing or egress request. `allow_solo` lets a self-approval through while exactly one person exists, audited as `self_approved: true` (#121) | [6](#6-hangar-doctor) |
| `sudo:` on an environment | `false` | `false`: the agent isn't root, and on Docker the [uid split](../steering.md#the-uid-split) applies. Set `true` only for an environment whose people need runtime root (#124) | [4](#4-prebuild-images) |

## What changes for people

| Who | Before → now | What to do |
| --- | --- | --- |
| **Someone with only a subscription login** (Claude Pro or Max, ChatGPT, Google) | Their jobs ran on it. Now a job fails before it starts, with the `hangar cred add … --kind api_key` fix on its ticket or thread. Their own sessions and sandboxes keep it, with a warning (#125) | Add an API key with a spend cap in the vendor's console (step 1) |
| **Approvers** | `approve_job` over MCP took any admin token, with no check against the requester. Now every path follows one rule (#121): an admin or a repo approver, never on their own job. MCP and REST need a login token, not the `mcp` token `hangar connect` writes | Approve with `hangar job approve` or a login (OAuth) connection. A team of one sets `HANGAR_SELF_APPROVAL=allow_solo` |
| **Whoever files an issue, ticket or Slack request** | Intake jobs ran with the environment's git token and open egress. Now they fetch only, egress is at least `allowlist`, and the sandbox cold-starts (30–60 s). On Docker, sudo fails (#123, #124) | Add a host the job needs to the environment or the repo policy (`egress.allow`, presets) |
| **Reviewers of agent PRs** | A turn that read untrusted content (an outsider's issue, any Jira issue, a web page, an untrusted MCP server) stays tainted. Its PR opens as a **draft** labelled `hangar:tainted`. A **second person**, not the job's own or its requester, must approve it to land. Its egress requests need an admin or a repo approver (#129, #130) | Use `approvals: deny-risky` for intake jobs ([taint gates](../security/taint.md#taint-gates)) |
| **People with saved environments** | `EnvSpec.sudo` was `true` and is now `false`, so a runtime `apt-get` fails (#124). In `extends`, component lists such as `skills` replace the parent's; `skills_remove` drops inherited ones (#132) | Move installs to `tools.apt`, or set `sudo: true`. Act on step 3's report |
| **Experiment authors** | Graders ran with the environment's network and credentials. Now they are sealed: egress `none`, no credential, fetch-only (#126). Hidden tests run from a pristine copy. Tests that pass on the base make the task `invalid_task`, and canaries flag `contaminated` trials (#127) | A grader that needs packages sets `grader.network: mirror` ([grading](../jobs/experiments.md#grading)) |
| **Jira admins using an Automation rule** | The static token started a job at any time. Now it works only within 10 minutes of the trigger transition, at most 3 times a day per issue. Bad requests alert the admins (#128) | Prefer the signed webhook. Rotate with `hangar cred add NAME --rotate` ([intake.md](../intake.md#fallback-an-automation-rule)) |
| **The sister tool, reading manifests** | `hangar.manifest/v1` gains optional `harness_digest` and `environment_digest` beside `config_digest` (#131) | Nothing |

## 1. Add API-key credentials

Do this first, so no job is left with only a subscription login. Add a team key for work no person owns, and one for
each person who submits jobs. A person without one falls back to the team's, as the repo policy's `credentials:` mode
says.

Set a spend limit on each key in the vendor's console. hangar's own budgets cap spend inside hangar
([step 8 of the team release](team-release-procedure.md#8-budgets-then-job-concurrency)).

```bash
hangar cred add claude-team --team --harness claude-code --kind api_key --file anthropic-key.txt
hangar cred add codex-team --team --harness codex --kind api_key --file openai-key.txt
hangar cred ls                       # kind and owner of each: who still holds only an oauth_token or auth_file
```

If some people can't have a key by the rollout, set `HANGAR_SUBSCRIPTION_LOGINS=any` for this release
([subscription logins](../team/subscription-logins.md)). Unset it once they have one.

## 2. Turn on the gateway and the egress proxy

On GKE, `deploy/k8s/config.yaml` sets `HANGAR_GATEWAY_URL`, `HANGAR_EGRESS_URL` and `HANGAR_INTAKE_POSTURE=enforce`.
Check that your `hangar-site` doesn't override them. On Docker, [turn on the gateway](../security/git-gateway.md#turning-it-on)
(on a laptop, `http://host.docker.internal:8788`). Set up the proxy as in
[step 10a of the team release](team-release-sandboxes.md#10a-egress-allowlists). If either isn't ready on the day,
set `HANGAR_INTAKE_POSTURE=warn` for this release, and unset it once both work.

## 3. Run the merge dry run

```bash
uv run hangar env migrate-merge --dry-run --data-dir /path/to/hangar-data
```

It names each saved environment that resolves differently under [the new `extends` rules](safe-release-merge.md).
Fix each one before the rollout.

## 4. Prebuild images

`sudo: false` changes every environment's image tag (#124), and an environment changed in step 3 gets a new one too.
Build them before anyone waits on a first sandbox:

```bash
uv run hangar images plan                           # what will build
uv run hangar images build --registry "$REG"        # or let the images workflow build them in CI
```

## 5. Deploy in the power-down window

This release changes every environment's image, so on GKE it goes out by hand between 01:00 and 09:50 ET. The
sandbox pools are down then and the control plane is up ([team-scale.md](../team-scale.md#applying-it)). No running
sandbox is cut off, and events wait in the queue. A routine `deploy.yml` deploy refuses while the pools are down
([when to deploy](../upgrading.md#when-to-deploy)).

```bash
bash deploy/gke/up.sh
```

On Docker: `uv run hangar down`, update the checkout, `uv run hangar up`.

## 6. `hangar doctor`

```bash
hangar doctor
```

All green means:

- the intake posture is `enforce`, with the gateway and the egress proxy reachable;
- `HANGAR_SUBSCRIPTION_LOGINS=any` is not set;
- a team credential covers work no person owns;
- someone can approve each person's work. With one person and `HANGAR_SELF_APPROVAL=deny` it warns: add a second
  person, or set `allow_solo`.

It also lists the environments with `sudo: true`, and whether intake jobs use them. Then start one intake job (a
`hangar`-labelled issue, or a Jira ticket). Its review packet says `Egress: allowlist (intake floor)`, and its
timeline has no `posture.warn` event.

## Rolling back

Redeploy the image you recorded, as for [the team release](team-release-operations.md#rolling-back-this-release). A
warn switch is the lighter way back for one control: `HANGAR_INTAKE_POSTURE=warn` or `HANGAR_SUBSCRIPTION_LOGINS=any`.

## The `extends` merge rules (#132)

In `extends`, component lists such as `skills` now replace the parent's instead of appending, and `skills_remove`
drops inherited skills. Step 3's dry run names each environment this changes. [The merge rules](safe-release-merge.md)
say what changed and how to fix an environment.
