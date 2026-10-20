# Context exchange: getting context in and results out

MCP clients (Claude Code, Codex, Cursor, other agents), services and the CLI hand work to cloud agents in [jobs](jobs.md) and [sessions](sessions.md). This page is how context moves both ways:

- **in:** the request, files, and your local work in progress (uncommitted changes and unpushed commits);
- **during:** progress, and questions the agent can't answer itself;
- **out:** a summary, a structured result, files (artifacts), and the code, ready to apply locally.

The design is [spec 0011 §2](specs/0011-factory-roadmap.md).

## A round trip from an MCP client

```text
new_session(name="csv-fix", repo=..., message="finish this and add tests",
            context=[<bundle of my unpushed commits>, <patch of my uncommitted changes>, {"name": "trace.txt", ...}],
            output_schema={"type": "object", "properties": {"tests_added": {"type": "integer"}}, ...})
wait_for_turn("csv-fix", timeout_seconds=600)      # progress notifications while it runs
  -> {"done": true, "outcome": "exited", "summary": "...", "result": {"tests_added": 3},
      "artifacts": [{"path": "coverage.html", "turn": 1, "uri": "hangar://sessions/csv-fix/artifacts/1/coverage.html"}]}
get_artifact("csv-fix", 1, "coverage.html")        # or read the resource URI
get_session_patch("csv-fix")                       # the whole change as one patch: git apply it locally
```

If the agent needs a decision, the turn ends as `needs_input` with the `question`; answer with `send_to_session` (or `follow_up_job`) and the agent continues in the same conversation.

From the CLI:

```bash
cd ~/src/app                                  # a branch with unpushed commits and uncommitted changes
hangar session new csv-fix "finish this and add tests" --from-local --schema result.schema.json -w
hangar session artifacts csv-fix              # list; --get coverage.html downloads one
hangar session patch csv-fix > csv-fix.patch
hangar session pull csv-fix                   # its commits on branch hangar/csv-fix, even if never pushed
hangar session pull csv-fix --checkout        # ...and switch to it with its uncommitted changes applied
```

`hangar job submit --from-local` does the same for a job.

## The agent's side: `HANGAR_IO`

Every turn gets `$HANGAR_IO`, a directory the agent reads and writes with ordinary file tools. For a job it is the job's directory, `~/.hangar/jobs/<job>`; for a session, `~/.hangar/sessions/<name>`.

```
$HANGAR_IO/
  in/<turn>/          attachments for this turn
  RESULT.md           summary for the people who asked
  out/result.json     structured result, validated against the turn's output_schema
  out/QUESTION.md     "I need a decision": the turn ends as needs_input
  out/progress.jsonl  {"ts": ..., "message": "...", "pct": 40} lines
  out/artifacts/**    files to hand back
```

Before each turn hangar clears `out/` (except `out/artifacts/`) and `RESULT.md`, and writes the turn's attachments. After the turn it reads the files and records them on the turn. The directory is checkpointed with the session, so it survives suspend and resume.

Every sandbox has the `hangar-agent` helper on its `PATH` (installed at sandbox setup, no image rebuild), and the prompt tells the agent about it:

| Command | Writes |
| --- | --- |
| `hangar-agent progress [--pct N] "running the tests"` | a line in `out/progress.jsonl` |
| `hangar-agent ask "Keep the old column names?"` | `out/QUESTION.md`; then the agent ends its turn |
| `hangar-agent question --option "Keep them" --option "Rename them" "Keep the old column names?"` | the same, plus `out/QUESTION.json` (`{"question", "options"}`): up to 5 answers to pick from, shown as `question_options` (each at most 80 characters, redacted) on the job, the session, the inbox item and the Fleet unit. A plain `ask` offers none |
| `hangar-agent artifact PATH [NAME]` | a copy under `out/artifacts/` (files or directories) |
| `hangar-agent result '<json>'` (or `-` for stdin, `@file`) | `out/result.json`, checked to be JSON |
| `hangar-agent done "summary"` | `RESULT.md` |

## In

`context` takes the same items on `submit_job`, `follow_up_job`, `new_session` and `send_to_session` (REST, MCP and the CLI's `--file PATH` and `--text TEXT`):

| `kind` | Content | What happens |
| --- | --- | --- |
| `text`, `file`, `log`, `issue`, `diff` | text, or base64 with `"encoding": "base64"` | Written to `in/<turn>/<name>` (a job's first-turn context goes in the job directory itself, as before). Up to 50 items and 2 MB per turn. |
| `url` | `url` | Listed in the prompt. |
| `patch` | a `git diff --binary` | Applied on top of the branch before the turn, as uncommitted changes. If it doesn't apply, the turn fails with git's reason. Up to 10 MB. |
| `bundle` | a base64 git bundle, plus `base_commit` | Its commits are fetched and the work branch moved to them: a fast-forward, or a fresh start if the session has no commits of its own yet (the session's base becomes `base_commit`). `base_commit` must be on the remote. Up to 50 MB. |

Build both from a checkout yourself:

```bash
git bundle create local.bundle HEAD --not --remotes      # base_commit: git merge-base HEAD @{upstream}
git diff --binary HEAD > local.patch                      # plus untracked files: git diff --no-index /dev/null <file>
```

…or let `--from-local` do it.

## During

- **Progress.** `wait_for_turn(ref, timeout_seconds)` (MCP, up to 600 s) blocks until the turn ends and sends MCP progress notifications: the job's timeline, `progress.jsonl` lines, and, while the agent writes none, the latest line of its run log. Call it again if `done` is false.
- **Questions.** If `out/QUESTION.md` exists when the turn ends, the turn's `outcome` is `needs_input`:
  - a session goes idle with `needs_input` set to the question;
  - a job goes to state `needs_input` (not terminal). Nothing is verified or delivered yet: the agent's commits stay in the job's session (checkpointed) and are pushed when a later turn finishes. A GitHub job posts the question as a comment on the issue or PR; reply `/hangar <answer>` there (only a trusted person's answer is taken: it steers the agent). A `job.needs_input` callback is sent. Cancelling a job that waits for an answer posts the outcome to its issue or thread.
  - The answer is an ordinary follow-up (`follow_up_job`, `send_to_session`, `hangar job follow-up`, `hangar session send`, a Slack thread reply, an A2A message), and the agent resumes its own conversation. The question survives a suspend and a control-plane restart; `needs_input` is cleared as soon as the answering turn starts.
  - A turn that wrote a question but was then stopped (over budget, timed out, cancelled) doesn't wait for an answer: the job finishes with that reason and the question is not carried into the next prompt.

## Out

- **Summary:** `RESULT.md`, else the agent's last message.
- **Structured result:** with `output_schema` (on `submit_job`, `follow_up_job`, `new_session`, `send_to_session`; follow-ups inherit a job's schema), the agent is asked for `out/result.json`. hangar validates it with JSON Schema. If it is missing or invalid, the agent gets **one** repair turn in the same conversation; if that fails too, the turn records `result_error`. The repair run's events and usage count toward the turn (`repair_usage`), it is held to the turn's budget, and a control-plane restart re-attaches to it. The result is on the turn (`result`), on `get_job` (`result.data`) and in job callbacks.
- **Artifacts:** after each turn, new or changed files under `out/artifacts/` are stored with the session's checkpoints, encrypted, under `<session>/artifacts/<turn>/`. Files from earlier turns stay in place for the agent to update; unchanged ones are not stored again. Symlinks are skipped, text files are redacted for known secrets, and the caps (50 new files, 25 MB each, 100 MB per turn) skip files with a warning on the turn rather than failing it. Read them with:
  - MCP `get_artifact(ref, turn, path)`: text inline, binary as base64, up to 512 KB;
  - the MCP resource `hangar://sessions/<name>/artifacts/<turn>/<path>`;
  - REST `GET /api/sessions/{ref}/artifacts/{turn}/{path}` or `GET /api/jobs/{id}/artifacts/{turn}/{path}` (always a download);
  - `hangar session artifacts NAME [--get PATH]`.
- **Code:** `get_session_patch(ref)` (MCP; REST `GET /api/sessions/{ref}/patch`, `GET /api/jobs/{id}/patch`; `hangar session patch`) is the diff from the session's base to its working tree, untracked files included: live from the sandbox if it's up, else from the last checkpoint. `hangar session pull NAME` downloads the last checkpoint's commits (a bundle) and uncommitted changes through `GET /api/sessions/{ref}/pull`, fetches the commits into branch `hangar/<name>`, and, with `--checkout`, switches to it and applies the uncommitted changes. It refuses to overwrite a local `hangar/<name>` with commits of its own (`--force`), never touches your current branch without `--checkout`, and never overwrites your untracked files.

## Settings

| Variable | Default | |
| --- | --- | --- |
| `HANGAR_EXCHANGE_MAX_PATCH_MB` | 10 | `patch` items, and the largest `get_session_patch` |
| `HANGAR_EXCHANGE_MAX_BUNDLE_MB` | 50 | `bundle` items |
| `HANGAR_EXCHANGE_MAX_RESULT_KB` | 1024 | `out/result.json` |
| `HANGAR_EXCHANGE_MAX_ARTIFACTS` | 50 | New artifacts collected per turn |
| `HANGAR_EXCHANGE_MAX_ARTIFACT_MB` | 25 | Per artifact |
| `HANGAR_EXCHANGE_MAX_ARTIFACTS_MB` | 100 | Per turn |
| `HANGAR_EXCHANGE_INLINE_ARTIFACT_KB` | 512 | Largest `get_artifact` answer |
| `HANGAR_EXCHANGE_REPAIR_TIMEOUT_MINUTES` | 10 | The repair turn for a missing or invalid result |
