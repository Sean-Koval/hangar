# semver harness eval

One coding task, graded by hidden tests, for comparing agent harnesses (and models) on hangar. First run:
pi vs Antigravity on Gemini 3.8 Flash, 2026-10-12. Every trial scored 116/116, so this task now
separates harnesses only on process (calls, tokens, cost, time, behaviour). Use it as a smoke test, not a ranking.

| File | What it is |
| --- | --- |
| `task.md` | The prompt every agent gets, verbatim |
| `oracle.js` → `oracle.json` | Expected answers from node-semver 7.8.5 (`npm i semver@7 && node oracle.js > oracle.json`) |
| `gen_hidden.py` → `hidden_test_semver.py` | 116 hidden unittest cases in 13 categories, generated from the oracle |
| `grade.sh` | Copies the agent's `semver.py` next to the hidden tests, runs them, prints `RESULT {json}` by category |
| `spans.py` | Normalizes a pi (`--mode json`) or Antigravity (`stream-json`) run log into tool and model spans |
| `classify.py` | Classifies tool spans with Microsoft Decision-1 (OpenRouter Decisions API) and keyword rules |
| `metrics.py` | Per-trial metrics: tokens, cost (reported or priced), time, categories, calls before first edit, test runs |

## Running it by hand

1. Create one sandbox per trial from `bare` with the harness you want (`create_sandbox(harness=...)`), then
   `git init` an empty `/workspace/semver`.
2. `run_agent(sandbox, prompt=task.md, model=...)` in `/workspace/semver`; give every run the same time budget.
3. When it finishes, fetch this folder into the sandbox (never before: the agent must not see the grader), then:
   ```bash
   KIT=. bash grade.sh
   python3 spans.py <pi|antigravity> ~/.hangar/runs/run-*.log > spans.jsonl
   OPENROUTER_API_KEY=... python3 classify.py spans.jsonl > cls.json      # Decision-1, ~$0.00001 per span
   python3 metrics.py spans.jsonl cls.json 0.75 3.75 0.075                 # $/M in, out, cached
   ```

## Known gaps (fix before trusting a ranking)

- Sandboxes mount every allowed harness pack, and pi's pack contains node-semver: the reference implementation is
  on disk. Mount only the selected pack for evals.
- Egress is open, so agents can read node-semver's docs online. Record or restrict it per run.
- Antigravity reports tokens but not cost; `metrics.py` prices them at the rates you pass.
- `run_eval` in hangar should replace these steps: parallel trials, budgets, grader injection, stored traces.
