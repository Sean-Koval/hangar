# Harness lock, outbox and doors: adopt & capture and frontier

What this release adds, what you configure and what changes for people. The steps are on [the procedure page](adopt-capture-procedure.md); every release is listed on [release notes](README.md).

Everything merged since the safe release, for a hangar that runs it: six engineers on three repos, planned work
through Jira, the inbox in Slack, review load gating admission, every run captured for flightdeck, harnesses as locked
directories (P2, #133–#171, #189, #221), and the frontier work that landed with it (P4: the MCP split #140, record and
replay #177 and #179, trials and pin sets #178, the trial cache #180, the eval lane and budget #181 and #182, shadows
and canaries #183 and #184, the overnight lane #186). It ships in v0.2.0 with the safe release, and this guide
starts from it: do [its procedure](safe-release.md) first if you haven't. It includes
[the repo-conventions hand-off](#the-repo-conventions-hand-off-143) and [plugins from a seed](#claude-code-plugins-from-a-seed-136).

Most of it is off until you turn it on. What changes by itself is the MCP endpoint your clients use, who may approve
their own agent's PR, how many agent PRs a person can have waiting before new jobs wait, Slack DMs, the PR body's
separator, and every turn's digests.

This release changes no node pool and only the images that bake in the `repo-conventions` skill, so it deploys by
day with `deploy.yml`.

```bash
kubectl -n hangar get deploy hangar -o jsonpath='{.spec.template.spec.containers[0].image}' | tee ~/.hangar/pre-upgrade-image
hangar admin backup                                                # 1. back up, record the image
terraform -chdir=deploy/terraform/gke apply                        # 2. the hangar-runs bucket (additions only)
uv run hangar images plan && uv run hangar images build --registry "$REG"   # 3. the repo-conventions images
hangar setup slack --manifest slack-app.yaml                       # 4. re-apply the app manifest: im:write, interactivity
gh workflow run deploy.yml --ref main                              # 5. by day, 10:00–01:00 ET; approve it
hangar connect claude-code                                         # 6. every client: hangar and hangar-sandbox
hangar repo set acme/app --ai-review hangar                        # 7. the review rules, per repo
hangar person link alice slack T0123ABCD/U012AB3CD                 # 8. Slack links, for DMs
#                                                                    9. turn on what you want: run capture (#273), ...
hangar doctor                                                      # 10. all green
```

## What you configure

`hangar config set NAME VALUE` keeps a setting in hangar's database; `hangar-site` wins over it. Repo policy fields
are set with `hangar repo set OWNER/REPO`, people's delivery with `hangar person set ID`.

| Setting | Values (default first) | What it does | Step |
| --- | --- | --- | --- |
| MCP client entries | `hangar` (`/mcp`), `hangar-sandbox` (`/mcp/sandbox`) | `/mcp` lists the work tools (jobs, sessions, schedules, discovery); the drive tools (sandboxes, `exec`, files, terminals, `run_agent`, snapshots, environment builds) are on `/mcp/sandbox`, with the same tokens and OAuth grants (#140). For this release the drive tools still answer on `/mcp`, unlisted, with a note an hour to re-add the connector | [6](adopt-capture-procedure.md#6-re-add-mcp-clients) |
| Repo policy `self_approval` (`--self-approval`) | `low_risk_with_ai_review`, `never`, `any` | Whether the job's own person can be the only approver of its PR, under `land: auto_after_approval`: by default only for a low-risk PR (docs and tests) with an independent AI review of that head that isn't `blocking` (#160). `any` is the old behaviour | [7](adopt-capture-procedure.md#7-the-review-rules-per-repo) |
| Repo policy `ai_review` (`--ai-review`) | `off`, `hangar`, `check:<name>` | An independent first-pass review of every agent PR: `hangar` runs a review job on the other harness (Codex for a Claude Code PR, else Claude Code; model `frontier`, $1.50 a review) and posts one `COMMENT` review; `check:<name>` reads your review bot's check (#161). **While it is `off`, no self-approval lands** under the default rule | [7](adopt-capture-procedure.md#7-the-review-rules-per-repo) |
| `HANGAR_PERSON_MAX_OPEN_AGENT_PRS` | `5` (`0` is off) | Once a person has this many agent PRs awaiting review, their new jobs wait as `queued` with reason `awaiting_capacity` (#159). Repo policy `max_open_agent_prs` (`--max-open-agent-prs`) overrides it for a repo, `HANGAR_PERSON_MAX_OPEN_AGENT_PRS_OVERRIDES` (`{}`) for a person; a team job's bucket gets the cap times the policy's approvers. Only a job's first turn waits; AI review jobs and trials never do | [7](adopt-capture-procedure.md#7-the-review-rules-per-repo) |
| Delivery (`hangar person set ID`) | `--dm questions`, `all`, `off`; `--digest 10:15,16:00`; `--tz America/New_York`; weekdays only | A person with a linked Slack account gets a DM for each question and approval of their own agents, and a digest at each `--digest` time (#157, #158). `all` adds the team's work (for admins). The Slack app needs the `im:write` scope | [4](adopt-capture-procedure.md#4-the-slack-app-imwrite-and-interactivity), [8](adopt-capture-procedure.md#8-people-and-the-inbox) |
| Terraform `enable_run_bucket` | `true` | The `<project>-hangar-runs` bucket for run capture: hangar's account may create objects and nothing else, and a deny policy takes away delete and overwrite. Applying it needs `roles/iam.denyAdmin`. `run_bucket_readers` (`[]`) are the readers (flightdeck's compactor); tapes expire after `run_tape_days` (`30`) | [2](adopt-capture-procedure.md#2-terraform-the-run-bucket) |
| `HANGAR_OUTBOX_URL` | unset (off) | Run capture (#163): every finished run written once, redacted, as `hangar.run/v1` objects. `HANGAR_OUTBOX_SESSIONS` (`all`, `jobs_only`); a deleted session's files wait up to `HANGAR_EXPORT_GRACE_DAYS` (`30`) for their export (#189). On GKE `deploy/gke/up.sh` sets it from terraform's `run_bucket` (#273); read at start, so a change needs a restart | [9](adopt-capture-procedure.md#9-turn-on-what-you-want) |
| `HANGAR_JIRA` `trigger` | `{"status": "Ready for agent"}` from `hangar setup jira` | The default door for planned work (#156). An existing `HANGAR_JIRA` keeps its trigger. A ticket's acceptance criteria reach the job as an untrusted `acceptance.md`; its verify command is used only if it matches the repo policy's `verify_allowed` (`--verify-allowed`, unset) | [9](adopt-capture-procedure.md#9-turn-on-what-you-want) |
| `HANGAR_HARNESS_REPO`, `HANGAR_DEFAULT_HARNESS` | unset, unset | The team harness repo, a path or a git URL with `#ref`, refreshed every `HANGAR_HARNESS_REPO_REFRESH_SECONDS` (`600`) and on a `push` webhook; the lock intake jobs that name no harness run from (#138). `HANGAR_HARNESS_DRIFT` (`warn`, `fail`) | [9](adopt-capture-procedure.md#9-turn-on-what-you-want) |
| `HANGAR_STANDUP_CHANNEL` | unset (off) | A weekday standup line at `HANGAR_STANDUP_TIME` (`10:18`) in `HANGAR_STANDUP_TIMEZONE` (`America/New_York`) (#168) | [9](adopt-capture-procedure.md#9-turn-on-what-you-want) |
| `HANGAR_GITHUB_APP_SLUG` | unset | Narrows the digest's review-queue link to the App's PRs (#158) | [9](adopt-capture-procedure.md#9-turn-on-what-you-want) |
| `HANGAR_EVAL_WINDOW` | `20:00-00:15` (in `HANGAR_EVAL_TZ`, `America/New_York`), `always` | Eval-class jobs (experiment trials, shadows, replays) start only in the window, after waiting interactive jobs, in at most `HANGAR_EVAL_MAX_RUNNING` (`0`: half the job slots), and with `HANGAR_EVAL_TPM_BUDGET` (unset) under a token rate (#181) | — |
| `HANGAR_EVAL_BUDGET_USD_MONTH`, `HANGAR_EVAL_AUTHOR_CAP_USD_MONTH` | `1500` (`0` is off), `400` | Monthly eval spend, the team's and each author's (`hangar person set ID --eval-cap`) (#182) | — |
| `HANGAR_TRIAL_INFRA_RETRIES` | `2` | A trial infrastructure ended (`infra_error`) runs again, at most 10% of an experiment's trials (#166) | — |
| Off until you set them | — | Repo policy `shadow` and `shadow.canary` (#183, #184); an environment's `model_gateway: record` and tapes (#177; `HANGAR_DETERMINISM_SCHEDULE`, `0 21 * * *`, replays only recorded runs, #179); `HANGAR_TRIAL_SENTINEL` (`[]`, #180); terraform `enable_overnight_lane` (`false`) with `hangar power overnight on` and repo policy `overnight` (#186) | [9](adopt-capture-procedure.md#9-turn-on-what-you-want) |

## What changes for people

| Who | Before → now | What to do |
| --- | --- | --- |
| **MCP client users** | every tool on `/mcp` → work tools on `/mcp`, drive tools on `/mcp/sandbox` (`hangar-sandbox`); a client connected before keeps calling them on `/mcp` for this release, with a note to re-add the connector (#140) | `hangar connect claude-code` (or `codex`) again writes both; in claude.ai, ChatGPT or Claude Desktop add `<public_url>/mcp/sandbox` as a second connector ([mcp.md](../mcp.md#two-servers)) |
| **Engineers who approve their own agent's PR** | their approval landed it → it lands only when the PR is low risk with an independent AI review of that head; otherwise it waits for a second reviewer, with a `landing.note` and an inbox `review` item for the repo's approvers. Tainted PRs (every Jira-door job) need a second person as before (#160) | turn on `ai_review` for the repo, or ask a teammate; `--self-approval any` brings the old rule back ([landing.md](../landing.md#self-approval-who-may-be-the-only-approver)) |
| **Engineers with many open agent PRs** | new jobs started at once → past 5 agent PRs awaiting their review, new jobs wait as `awaiting_capacity` until one merges or closes; follow-ups and fix rounds don't wait. The count is taken at the first start, so it can apply at once (#159) | review or close one; an admin lets a job through with `hangar job admit <id>` ([team-scale.md](../team-scale.md#review-capacity)) |
| **Anyone with a linked Slack account** | nothing in Slack outside job threads → a DM for each question and approval on their own work, and a digest at 10:15 and 16:00 on weekdays (#157, #158) | `hangar person set <id> --dm off` opts out; `--digest ""` drops the digest ([team.md](../team/linked-accounts.md#inbox-delivery-dms-and-the-digest)) |
| **Slack thread repliers** | a reply was a follow-up → a reply can be classified (`fix:`, `redirect:`, `info:`, or the buttons under hangar's answer); only corrections count against the agent (#162) | nothing; the buttons need the app's interactivity (step 4) |
| **Scripts that read hangar's PR bodies** | sections separated by `---` → by `___`, and the body ends in trailers (`Hangar-Job`, `Harness-Digest`, `Environment-Digest`, `Harness-Lock` when thawed), which the squash or merge commit carries too (not with `merge_method: rebase`) (#137) | split on `___`, or read the trailers with `git interpret-trailers` ([jobs/working-with-the-agent.md](../jobs/working-with-the-agent.md#the-pull-request)) |
| **Anything that compares digests** (flightdeck, a team harness repo's `channels.yaml`) | → every turn's `harness_digest` changes once, plugins or not: #221 narrowed the identity's plugin section to `plugins[*].{name,marketplace,digest,enabled}`, and the section's name is part of what is hashed. Every environment's `environment.spec_digest`, and so every turn's `environment_digest` and `config_digest`, changes too, because every field of the spec is digested and it gained `plugins` (#136, PR #215) and `model_gateway` (#177, PR #257): a hangar from before #136 sees both changes, one that already had #136 sees one | compare digests only within one side of the upgrade; re-key `channels.yaml` and any `name@sha256:` reference written before, from the digest `hangar harness check DIR` prints ([harness-lock.md](../harness-lock.md#identity)) |
| **Experiment authors** | trials started at once, on any model name, with no monthly cap → a floating model alias is refused naming the concrete ids (#167); trials wait for the 20:00–00:15 eval window (#181) and count against $1,500 a month for the team and $400 per author (#182); infrastructure-ended trials are retried (#166); an environment spec edited mid-experiment ends its running trials as `infra_error` / `drift` (#178) | pin dated model ids; `HANGAR_EVAL_WINDOW=always` and `HANGAR_EVAL_BUDGET_USD_MONTH=0` give the old behaviour ([jobs/experiments.md](../jobs/experiments.md)) |
| **Whoever writes Jira tickets** | the description was the task → its acceptance criteria also reach the job as an untrusted `acceptance.md`, and the queued comment hints when there are none; a verify command in the ticket is used only where `verify_allowed` matches (#156) | write tickets from the [Jira template](https://github.com/Sean-Koval/hangar/blob/main/docs/templates/jira-agent-ticket.md) ([intake.md](../intake.md#agent-tickets)) |
| **Reviewers** | → the packet's Agent line links the harness digest and lock (#137); removing or changing a line of an existing ADR's `checks:` block makes a PR high risk (#170) | nothing |

## The repo-conventions hand-off (#143)

The `repo-conventions` skill told the agent to put its hand-off summary in its final message, while the job prompt
asks for `RESULT.md` in the job's directory. Its step 5 now says the same as the prompt: `hangar-agent done
"<summary>"` or the task's `RESULT.md` path, never the repo, and the final message only when the task names neither.

The skill is baked into the images of `claude-python`, `codex-node`, `polyglot` and `hangar` (and any environment that
lists it), so their image tags change once, as they did for #82: prebuild with `hangar images plan` / `hangar images
build` before the rollout. Their manifests' `harness_digest` changes too (the skill is part of the harness), so a
harness lock frozen from one of them before this release reports drift when thawed there
([harness locks](../harness-lock.md)): freeze it again after the rollout.

## Claude Code plugins from a seed (#136)

Nothing to do: an environment without `plugins:` behaves as before (its manifests record `plugins: null`). Its
digests still change across this release: the spec's new `plugins` field changes every environment's `spec_digest`,
and #221 later re-keyed every turn's `harness_digest`
([adopt & capture, what changes for people](#what-changes-for-people)). What changes:

- An environment can declare Claude Code plugins (`plugins:`), installed into a read-only seed in its image and
  loaded in every turn, `ask` and `deny-risky` included ([harnesses.md](../harnesses.md#plugins)). Plugins the
  environment's `~/.claude/settings.json` enables from anywhere else still don't load in those turns: move them into
  a local marketplace next to the spec and declare them.
- Hardened Claude Code turns now run with `CLAUDE_CODE_PLUGIN_CACHE_DIR` on a new directory each turn, so they read
  nothing of `~/.claude/plugins`.
- An environment whose `~/.claude/settings.json` has `extraKnownMarketplaces` gets `"autoUpdate": false` on each, so
  its image tag changes: prebuild it (`hangar images plan` / `hangar images build`) before the rollout.
- The canary refuses a Claude Code lock older than 2.1.273 (seed directories need it; the lock is 2.1.296).

