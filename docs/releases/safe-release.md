# Secure intake and API keys: the safe release

Secure defaults for intake jobs, API keys for automated work, one approval rule, sealed graders, taint gates, new manifest fields and the `extends` merge rules. Every release is listed on [release notes](README.md).

The release that makes hangar safe to hand to a second engineer. Intake jobs (from GitHub, GitHub Projects, Jira,
Slack and inbound webhooks) get boundaries that hold even when a prompt injection steers the agent; automated work
runs on API keys; every approval path follows one rule; experiment graders run sealed; a turn that read untrusted
content can't push, land or open egress on the agent's word; manifests gain the harness and environment digests;
and `extends` lists replace instead of append.

**Timeline (the owner's decision, 2026-10-17).** The security changes #123–#125 warn for one release, then enforce:

- **This release** ships them enforcing by default, with a switch back to warn for each, so you can upgrade before
  the gateway, the egress proxy and everyone's API keys are in place: `HANGAR_INTAKE_POSTURE=warn` (#123, and the
  intake side of #124) and `HANGAR_SUBSCRIPTION_LOGINS=any` (#125). Under a warn switch the work runs as before and
  says so: a `posture.warn` event, an audit entry and a review-packet line, or a warning on the sandbox, session and
  turn manifest; `hangar doctor` reports it.
- **The next release** enforces: those two switches go, and intake jobs without the gateway, and jobs with only a
  subscription login, are refused. Turn the switches off (unset them) as soon as the procedure below is done, and
  before you upgrade again.

`sudo: false` (#124) is the default for environments with no warn switch: an environment that needs root at runtime
for people's own sessions sets `sudo: true` (not used for intake jobs under `enforce`).

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
| API-key credentials: a person's own (`hangar cred add NAME --harness H --kind api_key`) or the team's (`--team`) | — | **Required** for every job, schedule, webhook, experiment and replay: they never use a subscription login (`oauth_token`, `auth_file`) | [1](#1-add-api-key-credentials) |
| `HANGAR_SUBSCRIPTION_LOGINS` | `interactive`, `any` | `interactive`: subscription logins only in a person's own sessions and sandboxes, with a warning. `any`: jobs use them too, with a warning, and `hangar doctor` fails. **This release only** (#125). Trials and graders never get one | [1](#1-add-api-key-credentials) |
| `HANGAR_GATEWAY_URL` | unset | The git gateway: job sandboxes fetch only and hangar pushes. **Required** for intake jobs under `enforce` (#123); also needed for build caches and private repo builds. `deploy/k8s/config.yaml` sets it on GKE | [2](#2-turn-on-the-gateway-and-the-egress-proxy) |
| `HANGAR_EGRESS_URL` | unset | The egress proxy that `allowlist` and `none` environments, and intake jobs' sandboxes, go through. **Required** on GKE for intake jobs' allowlist floor. `deploy/k8s/config.yaml` sets it | [2](#2-turn-on-the-gateway-and-the-egress-proxy) |
| `HANGAR_INTAKE_POSTURE` | `enforce`, `warn`, `off` | `enforce`: intake jobs need the gateway (refused at submit, with a message on the issue, ticket or thread, without it), their sandboxes get at least `allowlist` egress and cold-start, and on Docker run with `no-new-privileges`; an intake job on a heavy environment needs the repo policy's `allow_heavy_for_intake`. `warn`: they run as before, with a `posture.warn` event. **`warn` is for this release only** (#123, #124). `off` is not recommended ([security/front-doors.md](../security/front-doors.md#intake-posture)) | [2](#2-turn-on-the-gateway-and-the-egress-proxy) |
| `HANGAR_SELF_APPROVAL` | `deny`, `allow_solo` | `deny`: nobody approves their own job, landing or egress request. `allow_solo` lets a self-approval through while exactly one person exists (audited `self_approved: true`) (#121) | [6](#6-hangar-doctor) |
| `sudo:` on an environment | `false` | `false`: the agent isn't root, and on Docker the [uid split](../steering.md#the-uid-split) applies. Set `true` only for an environment whose people need runtime root (#124) | [4](#4-prebuild-images) |

## What changes for people

| Who | Before → now | What to do |
| --- | --- | --- |
| **Someone with only a subscription login** (Claude Pro or Max, ChatGPT, Google) | their jobs ran on it → a job fails before it starts, with the `hangar cred add … --kind api_key` command on its ticket or thread; their sessions and sandboxes keep it, with a warning (#125) | add an API key with a spend cap in the vendor's console (step 1) |
| **Approvers** | `approve_job` over MCP took any token of an admin, and nothing checked approver ≠ requester → MCP, REST (`hangar job approve`), GitHub and Jira `/hangar approve` all use one rule: an admin, or a linked person in the repo policy's `approvers`, over MCP and REST with a login token only (not the `mcp` token `hangar connect` writes), and **never your own job** (#121) | approve from `hangar job approve` or a login (OAuth) connection; a team of one sets `HANGAR_SELF_APPROVAL=allow_solo` |
| **Whoever files an issue, ticket or Slack request** | intake jobs ran with the environment's git token and open egress → they fetch only, egress is at least `allowlist`, the sandbox cold-starts (30–60 s), and on Docker sudo fails (#123, #124) | a host the job needs: add it to the environment or the repo policy (`egress.allow`, presets) |
| **Reviewers of agent PRs** | → a turn that read untrusted content (issue text from an outsider, any Jira issue, a web page, an untrusted MCP server) is tainted for good: its PR opens as a **draft** labelled `hangar:tainted`, a **second person** (not the job's own, nor its requester) must approve it to land, and egress requests for it need an admin or a repo approver (#129, #130) | use `approvals: deny-risky` for intake jobs ([security/taint.md](../security/taint.md#taint-gates)) |
| **People with saved environments** | `EnvSpec.sudo` was `true` → `false`; a runtime `apt-get` fails (#124). `extends` appended lists → component lists (`skills` among them) replace the parent's; `skills_remove` drops inherited ones (#132) | move installs to `tools.apt`, or `sudo: true`; act on step 3's report |
| **Experiment authors** | graders ran with the environment's network and credentials, hidden tests inside the repo → graders are sealed (egress `none`, no credential, fetch-only, git grant revoked before the patch runs), hidden tests run from a pristine copy, hangar owns the grade on Docker, hidden tests that pass on the base make the task `invalid_task`, and canaries flag `contaminated` trials (#126, #127) | a grader that needs packages sets `grader.network: mirror`; `files_mode: in_repo` keeps the old layout at `trust: low` ([jobs/experiments.md](../jobs/experiments.md#grading)) |
| **Jira admins using an Automation rule** | the static token started a job any time → only within 10 minutes of the trigger transition, at most 3 a day per issue; wrong-event and bad-token requests alert the admins (#128) | prefer the signed webhook; rotate with `hangar cred add NAME --rotate` ([intake.md](../intake.md#fallback-an-automation-rule)) |
| **The sister tool, reading manifests** | `config_digest` only → `harness_digest` and `environment_digest` too, optional and additive in `hangar.manifest/v1` (#131) | nothing |

## 1. Add API-key credentials

Before anything else, so no job is left with only a subscription login. A team key for work no person owns, and one
for each person who submits jobs (or let them fall back to the team's, the repo policy's `credentials:` mode).
Set a spend limit on each key in the vendor's console; hangar's own budgets ([step 8 of the team release](team-release-procedure.md#8-budgets-then-job-concurrency)) cap spend inside hangar.

```bash
hangar cred add claude-team --team --harness claude-code --kind api_key --file anthropic-key.txt
hangar cred add codex-team --team --harness codex --kind api_key --file openai-key.txt
hangar cred ls                       # kind and owner of each: who still holds only an oauth_token or auth_file
```

Only if some people can't have a key by the rollout: `hangar config set HANGAR_SUBSCRIPTION_LOGINS any` for this
release ([team.md](../team.md#subscription-logins)), and unset it once they do.

## 2. Turn on the gateway and the egress proxy

On GKE both are in `deploy/k8s/config.yaml` (`HANGAR_GATEWAY_URL`, `HANGAR_EGRESS_URL`,
`HANGAR_INTAKE_POSTURE=enforce`); check that your `hangar-site` doesn't override them. On Docker, set the gateway
([security/git-gateway.md](../security/git-gateway.md#turning-it-on); on a laptop `http://host.docker.internal:8788`) and the proxy as in
[step 10a of the team release](team-release-sandboxes.md#10a-egress-allowlists). Without them on the day, set
`HANGAR_INTAKE_POSTURE=warn` for this release, and unset it when they work.

## 3. Run the merge dry run

```bash
uv run hangar env migrate-merge --dry-run --data-dir /path/to/hangar-data
```

It names each saved environment that resolves differently under the new `extends` rules
([the merge rules](#the-extends-merge-rules-132)). Fix each one before the rollout.

## 4. Prebuild images

`sudo: false` changes every environment's image tag (#124), and an environment changed in step 3 gets a new one too.
Build them before anyone waits on a first sandbox:

```bash
uv run hangar images plan                           # what will build
uv run hangar images build --registry "$REG"        # or let the images workflow build them in CI
```

## 5. Deploy in the power-down window

On GKE roll out by hand between 01:00 and 09:50 ET, while the sandbox pools are powered down and the control plane is
up ([team-scale.md](../team-scale.md#applying-it)): no running sandbox is cut off, and events wait in the queue. This
release changes every environment's image, so it is an upgrade for the power-down window, not a routine `deploy.yml`
deploy, which runs 10:00–01:00 and refuses while the pools are down ([when to deploy](../upgrading.md#when-to-deploy)).

```bash
bash deploy/gke/up.sh
```

On Docker: `uv run hangar down`, update the checkout, `uv run hangar up`.

## 6. `hangar doctor`

```bash
hangar doctor
```

All green means: the intake posture is `enforce` with the gateway and the egress proxy reachable, no
`HANGAR_SUBSCRIPTION_LOGINS=any`, a team credential for work no person owns, and approvals possible (with one person and
`HANGAR_SELF_APPROVAL=deny`, doctor warns that nobody can approve that person's work: add a second person, or
`allow_solo`). It lists the environments with `sudo: true` and whether intake jobs use them. Then start one intake
job (a `hangar`-labelled issue, or a Jira ticket) and check its timeline: `Egress: allowlist (intake floor)` in the
review packet, and no `posture.warn` event.

Rolling back: as for [the team release](team-release-operations.md#rolling-back-this-release), redeploy the image you recorded. A warn
switch is the lighter way back for one control: `HANGAR_INTAKE_POSTURE=warn` or `HANGAR_SUBSCRIPTION_LOGINS=any`.

## The `extends` merge rules (#132)

`extends` used to append every list except `harnesses`, `credentials`, `terminals` and `ports`, so a child could
never drop a parent's skill. Now component lists (`skills` and every list not named here) replace the parent's,
the additive build inputs (`tools.apt`, `dependencies.pip`/`npm`) union, maps still merge by key (`null` now
deletes a key), and `skills_remove` drops inherited skills ([environments.md](../environments.md#how-extends-merges)).
Experiment variants gain `skills_remove` too.

Before you upgrade, run the dry run against the checkout you'll deploy and your data volume (it reads the shipped
specs and the saved ones in `<data>/envs`, and needs no running hangar):

```bash
uv run hangar env migrate-merge --dry-run --data-dir /path/to/hangar-data   # or with HANGAR_DATA_DIR set
```

It names each environment that resolves differently: skills lost or gained, and other fields that change (a
child's `skills: [x]` now means only `x`; `harnesses`, `credentials`, `terminals` and `ports` already replaced). No
shipped environment changes. For each one it names, either restate the parent's entries in the
child's list or switch to `skills_remove`. An environment whose resolved spec changes gets a new image tag:
prebuild with `hangar images plan` / `hangar images build` before the rollout, as in step 3 below.

