# Harnesses: versions, the contract, the canary and models

Hangar runs four agent CLIs (Claude Code, Codex, pi and Antigravity) and drives two of them over undocumented, fast-moving
protocols. This page covers how their versions are pinned and moved forward, what CI checks before a version reaches
anyone, and how each one gets "the latest strong model" without a stale default.

| | Where | When |
| --- | --- | --- |
| The pinned CLI versions | `src/hangar/harnesses.lock.yaml` | changed only by a reviewed PR |
| The contract tests | `.github/workflows/harness-contract.yml` | PRs that touch harness, driver or isolation code; every push to main |
| The canary | `.github/workflows/harness-canary.yml` | nightly, 03:17 ET (07:17 UTC), and by hand |
| Landing the canary's PR | `.github/workflows/harness-canary-land.yml` | on an approval of it |
| Model policies | an environment's or repo policy's `model:` (`frontier`, `balanced`) | when a sandbox or session is created |

None of these talk to the cluster, so they all work while it is powered down.

## The lock

`src/hangar/harnesses.lock.yaml` pins every harness hangar installs: an exact version, plus the vendor's checksums where the
vendor publishes the binary directly (Claude Code's sha256 per platform from its release manifest; Antigravity's
build id and sha512 per archive). npm packages (Codex, pi, Gemini) are checked by npm against the registry's sha512
integrity at install. A pack build fails unless the CLI reports the locked version ([gke.md](gke.md#3-environment-images)).

A sandbox records the locked version its pack was built for (`harness_lock`) next to what the CLI reported
(`harness_version`).

## The contract tests

The real-CLI tests (`tests/test_*real_clis.py` and the real-CLI part of `tests/test_cache.py`) start each CLI under
hangar's driver against scripted model APIs (`tests/fixtures/live/fake_models.py`), offline:

- approvals, steering and interrupts (`test_driver_real_clis.py`);
- the hardened turn: what a turn asks for, rules and MCP servers (`test_harden_real_clis.py`);
- the verify gate (`test_verify_gate_real_clis.py`);
- prompt-cache TTLs (`test_cache.py`);
- **the protocols** (`test_protocol_real_clis.py`). For Codex, the app-server JSON Schema the CLI generates
  (`codex app-server generate-json-schema`), cut down to the methods `driver.Codex` sends and receives and the types they
  reach. For Claude Code, the stream-json messages a live turn writes (a permission prompt, an interrupt with a
  follow-up), as message type and field paths. What the driver relies on (`canary.CODEX_USES`, `canary.CLAUDE_DRIVER`)
  must be there for any version. The locked version must also produce exactly the record in `tests/fixtures/protocol/`.

`harness-contract.yml` installs the locked CLIs with the packs' own recipes (`python -m hangar.canary install`, no
root; checksums and the version check included), sets `HANGAR_TEST_CLAUDE`, `HANGAR_TEST_CODEX` and `HANGAR_TEST_PI`,
and runs those files. A skipped test fails the job: every CLI the lock pins must actually run. The CLIs are cached by
the lock and the recipes. A run that finds them takes about a minute, and a cold one about two (the four installs
take ~40 s).

Run the same thing locally:

```bash
uv run python -m hangar.canary install --dest ~/.cache/hangar-clis --env-file .env.clis
env $(cat .env.clis) PATH="$PATH:$HOME/.cache/hangar-clis/codex/libexec/codex-path" \
  uv run pytest tests/test_*real_clis.py tests/test_cache.py
```

Antigravity has no real-CLI test yet: its install (the checksum, then `agy --version` naming the locked version) is its
contract.

## The canary

Every night `harness-canary.yml`:

1. **Resolves the newest stable release of each harness** from the sources the lock was verified against:
   - Claude Code: `downloads.claude.ai/claude-code-releases/stable`, then that version's `manifest.json`;
   - Codex, pi and Gemini: npm's `latest` dist-tag (for Codex, the linux-x64 and linux-arm64 builds must be published);
   - Antigravity: the updater's `manifests/linux_{amd64,arm64}.json`, with the build id from its `/releases` list.

   A harness only moves forward. Claude Code's stable channel can trail the lock (2.1.292 against the locked 2.1.296
   on 2026-10-14); that is reported as `behind` and nothing changes.
2. **Checks the checksums.** It downloads each Claude Code binary and Antigravity archive, for both architectures,
   and compares them with the vendor's published sha256 and sha512. It also checks that the updater serves the URL the
   install recipe would build from the version and the build id.
3. **Installs the candidate CLIs and runs the contract tests against them.** The unchanged CLIs come from the
   contract workflow's cache.
4. **Diffs the protocols** against the recorded (locked) ones. A *breaking* change is one where something the driver
   sends or reads is gone: a method, a field, or a value such as an approval decision or a sandbox mode. Everything else
   is listed for the reviewer.
5. **If everything passed, opens one lock-bump PR** from `canary/harness-lock` (or updates the open one). It changes
   the lock (with one `# Canary <date>:` line in its header) and the protocol records. Its body is the review packet:
   versions and sources, checksum checks, test results, and the protocol diff. A newer candidate on a later night
   turns off auto-merge on the old head before it pushes, and the same candidate only refreshes the packet.
6. **If anything failed, opens one issue** labelled `harness-canary` (or comments on the open one). It has the
   failing tests with their output, the protocol diff, and resolver errors.

`workflow_dispatch` with `dry_run` does everything but open the PR or issue. The packet is in the run's summary and
its `harness-canary` artifact.

### Landing the bump: one approval, never without it

The bump PR lands the way an agent's PR does with `land: auto_after_approval` ([landing.md](landing.md)). It runs in
CI rather than in the control plane, so it works with the cluster down. On an approval, `harness-canary-land.yml`
checks it from the default branch's code, not the PR's. These must all hold:

- the PR is the canary's: head `canary/harness-lock` in this repository;
- the approval is of the PR's current head, by an approver: the `CANARY_APPROVERS` repository variable
  (comma-separated logins), else the repository's owner;
- the approver isn't the PR's author, and nobody's latest review requests changes;
- the PR changes nothing but `src/hangar/harnesses.lock.yaml` and `tests/fixtures/protocol/` (hardening-r2).

When they hold, it arms auto-merge for exactly that commit (`expectedHeadOid`), or merges it at that commit when it is
already mergeable. GitHub's branch protection still decides when it merges: required checks, stale approvals
dismissed on a push. Anything else, and nothing is armed; the run's summary says why.

### Setup

- **Branch protection on `main`**, as [landing.md §3](landing.md#3-the-branch-protection-to-enable) describes, and
  *Allow auto-merge*. Without required approvals, auto-merge would merge a bump without anyone's approval. The
  land workflow still never arms one without an approval.
- **A GitHub App for the PR** (recommended): repository secrets `CANARY_APP_CLIENT_ID` and `CANARY_APP_PRIVATE_KEY`
  for an App with Contents and Pull requests read and write on this repo (hangar's own App will do). The PR then comes
  from the App, so `pr.yml` and `harness-contract.yml` run on it, and the merge's push runs `ci.yml` and
  `images.yml`, which builds the new packs. Without the App, the PR is opened with the workflow token. GitHub runs no
  workflow for it, so close and reopen the PR to run the checks, and run `images` by hand after the merge. The PR body
  says so.
- Optionally, the `CANARY_APPROVERS` repository variable.

### After a bump

`images.yml` builds the new packs on the merge's push. A new CLI version applies to **sessions created after the
bump**: a session records the locked version when it is created (`harness_version` on the session). A follow-up on
an older session that lands on a sandbox running the newer CLI is treated as a harness switch. The turn hands off (a
summary of the conversation in a new conversation) instead of resuming a conversation written by another version. The
turn carries a warning, the session's activity shows it, and `session.harness_version` is audited. A sandbox that was
already running keeps its pack, so its session resumes as before. Sandboxes that bake the harness into the image don't
record `harness_lock`, so for them nothing changes.

## Models: the latest strong model per provider

A `model:` value is either a pinned id (`claude-opus-5-5`, `gpt-6.1-sol`), passed through as it is, or a **policy**:

| Policy | Picks |
| --- | --- |
| `frontier` | the strongest current model the provider lists for your account. **The default for jobs**, and `envs/bare.yaml`'s for every harness |
| `balanced` | the provider's everyday line: Anthropic's opus, OpenAI's sol, Google's flash |
| `frontier@openai`, `balanced@google`, … | the same, from one vendor, for a harness that reaches several (pi on OpenRouter; Anthropic's otherwise) |

Environments (`model: frontier`, or per harness `model: {codex: balanced}`) and repo policies (`hangar repo set acme/app
--model frontier`) accept a policy. A policy in a repo policy applies to every harness, where a pinned id applies only
to the policy's own harness. So do `submit_job(model=…)`, `send_to_session(model=…)` and `run_agent(model=…)`.

### Where the ids come from

| Harness | List | How "strongest" is decided |
| --- | --- | --- |
| Claude Code | Anthropic `GET /v1/models` | the `line` field (fable and mythos, then opus, sonnet, haiku), then the newest. Active models only |
| Codex | OpenAI `GET /v1/models` | `gpt-<version>-<tier>`: the tier (astra, sol, terra, luna), then the version |
| Antigravity | Google's Gemini API `models.list` (Vertex serves the same ids) | the newest version, then stable before preview, then the tier (pro, flash, flash-lite). agy's id is the Gemini id without `-preview`, plus `-high` |
| pi | OpenRouter `GET /api/v1/models` (public) | per vendor as above, on OpenRouter's ids (`openrouter/anthropic/claude-fable-5.1`); `:batch` ids and `~…-latest` aliases skipped |

The Anthropic side was checked against the API reference on 2026-10-14 (platform.claude.com/docs/en/api/models/list,
which docs.claude.com redirects to). It says "More recently released models are listed first". Each model has
`id`, `display_name`, `created_at`, `lifecycle` (`active`, `deprecated`, `retired`) and `line` (`haiku`, `sonnet`,
`opus`, `fable`, `mythos`, or null; "do not infer a line from the id"). Pages use `limit` (up to 1000), `after_id`
and `has_more`. Mythos is Fable for verified organizations; it appears only in their lists.

Why Google's tier comes last: Gemini's flash line moves faster than pro. Gemini 3.8 Flash (stable, September 2026)
leads Gemini 3.1 Pro Preview (February) by Google's own positioning ("the most intelligent Flash model", built for
long-horizon software engineering) and on independent indexes. That was also why `envs/bare.yaml` ran flash for pi
and Antigravity. That was a hand-picked id; now it is resolved.

Why `frontier` costs more: on Anthropic it picks Claude Fable 5.1 ($10/$50 per MTok, against Opus 5.5's $4/$20), and on
OpenAI gpt-6-astra when the account has it. Use `balanced` where that matters, and job budgets
([repo-policy.md](repo-policy.md#budgets)).

### Pinned, cached, and what happens when a list can't be had

- **Pinned at creation.** A sandbox resolves its environment's model when it is created (`get_sandbox` → `model`),
  and a session or job's session when it is created (`get_session` → `model`, `model_policy`). Its turns use that id
  until someone switches on purpose (`send_to_session(model=…)`, with the usual cold-cache warning). A newer model in
  the list never changes a running conversation (spec 0012: each model has its own prompt cache).
- **Cached.** Lists are fetched in the background, at startup for the providers the environments' policies need and
  then every `HANGAR_MODEL_LIST_TTL_MINUTES` (360). A create never waits on a provider. A stale list is still used, and
  refreshed.
- **Fallback, said out loud.** With no list (no key, the provider unreachable, nothing qualifying), hangar uses the
  last id it resolved for that harness and policy. Failing that, it uses a built-in one, dated 2026-10-14:
  `claude-fable-5-1`, Codex `gpt-6.1-sol` (Codex's default model; gpt-6-astra needs OpenAI's trusted access),
  `openrouter/anthropic/claude-fable-5.1`, `gemini-3.8-flash-high`. The session's activity says which, and why.
  `list_environments` shows what each harness's `model` resolves to now (`models`, with `source`: `live`,
  `cached`, `last`, `seed` or `pinned`).
- **Keys.** OpenRouter's list is public. The others need an API key in the vault, named in
  `HANGAR_MODEL_LIST_CREDENTIALS`:

  ```sh
  hangar cred add anthropic-models --harness webhook --kind generic   # paste the key; never a sandbox's
  hangar config set HANGAR_MODEL_LIST_CREDENTIALS '{"anthropic": "anthropic-models", "openai": "openai-models", "google": "gemini-models"}'
  ```

  The key only lists models. Lists are per account, so use a key from the account the agents run on: an Anthropic key
  without Mythos access never resolves to Mythos. Codex with a ChatGPT login has no API key to list with. Give it an
  OpenAI key from the same organization, or pin `codex: gpt-6-astra` if you have it.
