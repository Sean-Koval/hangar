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
| Model policies | an environment's or repo policy's `model:` (`balanced`, which jobs and `envs/bare.yaml` default to, or `frontier`) | when a sandbox or session is created |

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
2. **Checks the checksums, and where they come from.** A checksum from the server that serves the binary proves
   little, so each source is checked against something that server alone can't forge:
   - **Claude Code:** the version's `manifest.json` must carry a good signature (`manifest.json.sig`) by Anthropic's
     Claude Code release key. The key is checked in (`src/hangar/keys/claude-code.asc`) with the fingerprint
     code.claude.com/docs/en/setup publishes (`31DD DE24 DDFA B679 F42D 7BD2 BAA9 29FF 1A7E CACE`), and is never
     fetched from the release server. Then each binary, for both architectures, must match the signed sha256.
   - **Codex and pi (npm):** `npm audit signatures` checks the registry's signatures and the Sigstore provenance
     attestations of everything the candidate installs (with `--ignore-scripts`, for linux-x64 and, for Codex,
     linux-arm64). Each package the recipe installs must also carry SLSA provenance naming it, built from its own
     repository (`github.com/openai/codex`, `github.com/earendil-works/pi`). Gemini publishes no provenance; it gets
     the registry-signature check only.
   - **Antigravity:** Google publishes no signature (no `.sig`, `.asc` or checksum file beside the archives, checked
     2026-10-14). The sha512 comes from the updater's manifest, a separate service from the bucket that serves the
     archives, and each archive must match it. That is the whole of it, and the packet says so.

   It also checks that the updater serves the URL the install recipe would build from the version and the build id.
3. **Installs the candidate CLIs and runs the contract tests against them**, in a job with a read-only token and no
   secrets. A process in a job can read the job's token and every secret the job references, so nothing an upstream
   release ships (npm install scripts, the new binaries, the tests that run them) runs in the job that pushes and
   holds the App key. The unchanged CLIs come from the contract workflow's cache.
4. **Diffs the protocols** against the recorded (locked) ones. A *breaking* change is one where something the driver
   sends or reads is gone: a method, a field, or a value such as an approval decision or a sandbox mode. Everything else
   is listed for the reviewer.
5. **If everything passed, opens one lock-bump PR** from `canary/harness-lock` (or updates the open one). A separate
   `publish` job does it, which runs none of the candidates' code: it checks what the first job handed it (`canary
   check`: a valid lock, two JSON records) and commits exactly those three files. It changes the lock (with one `# Canary <date>:` line in its header) and the protocol records. Its body is the review packet:
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
- **the head is the canary's own push**: GitHub's record of the branch's pushes (`GET /repos/…/activity`) says a bot
  pushed it. The author is always a bot, so "not the author" alone would let an approver push a change onto the bump
  and approve it themselves. A person's commit on the canary branch is landed by hand, by someone else;
- the PR changes exactly `src/hangar/harnesses.lock.yaml`, `tests/fixtures/protocol/codex-app-server.json` and
  `tests/fixtures/protocol/claude-stream-json.json`, and removes none of them (hardening-r2). Not the directory: pytest
  collects `tests/` recursively, so a `conftest.py` or `test_*.py` among the fixtures would run in CI.

When they hold, it arms auto-merge for exactly that commit (`expectedHeadOid`), or merges it at that commit when it is
already mergeable. GitHub's branch protection still decides when it merges: required checks, stale approvals
dismissed on a push. Anything else, and nothing is armed; the run's summary says why.

### Setup

- **Branch protection on `main`**, as [landing.md §3](landing.md#3-the-branch-protection-to-enable) describes, and
  *Allow auto-merge*. Without required approvals, auto-merge would merge a bump without anyone's approval. The
  land workflow still never arms one without an approval.
- **A GitHub App for the PR** (recommended): repository secrets `CANARY_APP_CLIENT_ID` and `CANARY_APP_PRIVATE_KEY`
  for a dedicated App with Contents and Pull requests read and write, installed on this repo only. Don't reuse
  hangar's own App: its key would sit in this repository's secrets with that App's wider reach. Without Workflows
  permission it can't change `.github/workflows/`, which is what you want. The PR then comes
  from the App, so `pr.yml` and `harness-contract.yml` run on it, and the merge's push runs `ci.yml` and
  `images.yml`, which builds the new packs. Without the App, the PR is opened with the workflow token. GitHub runs no
  workflow for it, so close and reopen the PR to run the checks, and run `images` by hand after the merge. The PR body
  says so.
- **`contract` as a required check** on `main`, so a bump whose locked CLIs don't reproduce the recorded protocols
  can't merge.
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
| `balanced` | the provider's everyday line: Anthropic's opus, OpenAI's sol, Google's flash. **The default** for jobs, `envs/bare.yaml`'s every harness, agent-led env setup and the Cloud Doctor. On Claude Code it is Opus (today `claude-opus-5-5`, by the `line` rule below) |
| `frontier` | the strongest current model the provider lists for your account, at several times the price. Asked for per environment, repo policy or job |
| `frontier@openai`, `balanced@google`, … | the same, from one vendor, for a harness that reaches several (pi on OpenRouter; Anthropic's otherwise) |

Environments (`model: frontier`, or per harness `model: {claude-code: frontier}`) and repo policies (`hangar repo set
acme/app --model frontier`) accept a policy. A policy in a repo policy applies to every harness, where a pinned id applies only
to the policy's own harness. So do `submit_job(model=…)`, `send_to_session(model=…)` and `run_agent(model=…)`.

### Where the ids come from

| Harness | List | How "strongest" is decided |
| --- | --- | --- |
| Claude Code | Anthropic `GET /v1/models` | the `line` field (fable and mythos, then opus, sonnet, haiku), then the newest. Active models only |
| Codex | OpenAI `GET /v1/models` | `gpt-<version>-<tier>`: the tier (astra, sol, terra, luna), then the version |
| Antigravity | Google's Gemini API `models.list` (Vertex serves the same ids) | never a flash-lite model while another is listed; then the newest version, then stable before preview, then the tier (pro, flash, flash-lite). agy's id is the Gemini id without `-preview`, plus `-high` |
| pi | OpenRouter `GET /api/v1/models` (public) | per vendor as above, on OpenRouter's ids (`openrouter/anthropic/claude-fable-5.1`); `:batch` ids and `~…-latest` aliases skipped |

The Anthropic side was checked against the API reference on 2026-10-14 (platform.claude.com/docs/en/api/models/list,
which docs.claude.com redirects to). It says "More recently released models are listed first". Each model has
`id`, `display_name`, `created_at`, `lifecycle` (`active`, `deprecated`, `retired`) and `line` (`haiku`, `sonnet`,
`opus`, `fable`, `mythos`, or null; "do not infer a line from the id"). Pages use `limit` (up to 1000), `after_id`
and `has_more`. Mythos is Fable for verified organizations; it appears only in their lists.

Why Google's tier comes after the version: Gemini's flash line moves faster than pro. **Not verified against
Google's own pages:** that Gemini 3.8 Flash (stable, September 2026) is stronger than Gemini 3.1 Pro Preview
(February). Google's model list (ai.google.dev/gemini-api/docs/models, read 2026-10-14) calls 3.8 Flash "our most
intelligent Flash model, engineered for long-horizon software engineering" and lists 3.1 Pro as a preview, but
compares neither with the other; the ranking rests on third-party indexes. Where it matters, pin a Gemini id. A lite
model is never `frontier` while anything else is listed, so Google's next flash-lite doesn't take over by being newest.

Verified on 2026-10-14 against the vendors' pages: Anthropic's models overview and pricing (Fable 5.1 "for demanding
reasoning and long-horizon agentic work", $10/$50 per MTok; Opus 5.5 the general starting point, $4/$20); OpenAI's
models and pricing pages (gpt-6-astra "our most capable model", $10/$50, listed for the API and the Codex CLI;
gpt-6.1-sol "near-Astra performance at a lower cost", $2/$10). OpenRouter's `-pro` variants (`gpt-6-astra-pro`) are
not on OpenAI's pages and are never picked.

**What `frontier` costs.** On Anthropic it picks Claude Fable 5.1 ($10/$50 per MTok, against Opus 5.5's $4/$20), and
on OpenAI gpt-6-astra ($10/$50, against gpt-6.1-sol's $2/$10): roughly 2.5 to 5 times `balanced`. That is why jobs
default to `balanced` (the owner's decision); `frontier` stays a line away for an environment, a repo policy or one
job. Either way:

- every id a policy resolves to is in hangar's price table, including OpenRouter's dotted ids and agy's
  thinking-level ids, so job budgets and the daily caps ([repo-policy.md](repo-policy.md#budgets)) count its cost;
- **set a budget**: no job budget or daily cap is set by default, and `hangar doctor` (`model spend`) and `hangar
  status` warn until one is. `HANGAR_JOB_BUDGET_MAX_COST_USD`, a repo policy's `job_budget` and `max_usd_per_day`,
  and `HANGAR_PERSON_MAX_USD_PER_DAY` bound it;
- the review packet on each agent PR names the model the job's session ran, the policy it came from and its list
  price (`model `claude-fable-5-1` (`frontier`; list price $10/$50 per MTok in/out)`), and says when the job had no
  budget; `hangar env list` shows the same for each environment's harnesses;
- ask for `frontier` where the last bit of capability matters more than the price.

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
  `balanced`: `claude-opus-5-5`, Codex `gpt-6.1-sol`; `frontier`: `claude-fable-5-1`, Codex `gpt-6-astra`; pi
  `openrouter/anthropic/claude-opus-5.5` and `…/claude-fable-5.1` (and one per vendor for `frontier@openai` and the
  like); `gemini-3.8-flash-high`. Every built-in policy has one, so a
  session never starts on the harness's own default and switches model when the list arrives. The session's activity
  says which, and why.
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
  OpenAI key from the same organization, or pin `codex: gpt-6-astra` if you have it. A ChatGPT login is also interactive-only:
  jobs, schedules and experiments run Codex on an OpenAI API key, or a ChatGPT Enterprise Codex access token stored with
  `--kind api_key` ([team.md](team.md#subscription-logins)).
