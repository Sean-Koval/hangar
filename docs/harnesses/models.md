# Models: the latest strong model per provider

This page is part of [Harnesses: versions, the contract, the canary and models](../harnesses.md).

A `model:` value is either a pinned id (`claude-opus-5-5`, `gpt-6.1-sol`), passed through as it is, or a **policy**:

| Policy | Picks |
| --- | --- |
| `balanced` | the provider's everyday line: Anthropic's opus, OpenAI's sol, Google's flash. **The default** for jobs, `envs/bare.yaml`'s every harness, agent-led env setup and the Cloud Doctor. On Claude Code it is Opus (today `claude-opus-5-5`, by the `line` rule below) |
| `frontier` | the strongest current model the provider lists for your account, at several times the price. Asked for per environment, repo policy or job |
| `frontier@openai`, `balanced@google`, … | the same, from one vendor, for a harness that reaches several (pi on OpenRouter; Anthropic's otherwise) |

Environments (`model: frontier`, or per harness `model: {claude-code: frontier}`) and repo policies (`hangar repo set
acme/app --model frontier`) accept a policy. A policy in a repo policy applies to every harness, where a pinned id applies only
to the policy's own harness. So do `submit_job(model=…)`, `send_to_session(model=…)` and `run_agent(model=…)`.

## Where the ids come from

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
  thinking-level ids, so job budgets and the daily caps ([repo-policy.md](../repo-policy/budgets.md#budgets)) count its cost;
- **set a budget**: no job budget or daily cap is set by default, and `hangar doctor` (`model spend`) and `hangar
  status` warn until one is. `HANGAR_JOB_BUDGET_MAX_COST_USD`, a repo policy's `job_budget` and `max_usd_per_day`,
  and `HANGAR_PERSON_MAX_USD_PER_DAY` bound it;
- the review packet on each agent PR names the model the job's session ran, the policy it came from and its list
  price (`model `claude-fable-5-1` (`frontier`; list price $10/$50 per MTok in/out)`), and says when the job had no
  budget; `hangar env list` shows the same for each environment's harnesses;
- ask for `frontier` where the last bit of capability matters more than the price.

## Concrete ids and floating aliases

A pinned id is only reproducible if the provider can't move what it means. hangar calls an id **concrete** when it is
in the provider's cached model list or has a known versioned shape (`claude-opus-5-5`, `claude-sonnet-4-20250514`,
`gpt-6-astra`, `gpt-*-YYYY-MM-DD`, `gemini-3.8-flash`, `openrouter/anthropic/claude-opus-5.5`), and a **floating
alias** when it is `*-latest`, one of OpenRouter's `~vendor/…` aliases (or `openrouter/auto`), or a bare family name
(`opus`, `sonnet`, `haiku`, `gpt-5`, `gemini-pro`), even when a provider lists it. An id that is neither listed nor of
a known shape is never treated as concrete. With no list cached (no list credential, or not fetched yet), only the
shape decides and hangar says so (`modelpolicy.is_concrete`; every built-in id above is of a known shape).

- **Experiments refuse aliases.** An experiment (and the trials API, #178) won't start when a variant's model, after
  its policy is resolved, isn't concrete; the error names the alias and the ids `balanced` and `frontier` resolve to
  now. A policy is fine: it is resolved once, at the start, and every trial gets that id
  ([jobs/experiments.md](../jobs/experiments.md)).
- **Jobs and sessions keep working with aliases**, but their manifests record `model.concrete: false` (the outbox's `run.json`, #163, carries it as
  `pinned: false`), so evaluation leaves them out.

## Pinned, cached, and what happens when a list can't be had

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
  `--kind api_key` ([team.md](../team/subscription-logins.md)).
