---
name: repo-conventions
description: Conventions for working in hangar sandboxes - branching, commits, tests and hand-off notes. Use at the start and end of any coding task.
---

# Working in a hangar sandbox

1. **Orient**: `git status`, read the README and any AGENTS.md / CLAUDE.md in the repo.
2. **Branch**: you are usually already on a work branch (`git branch --show-current`). If you are on
   `main`, create `agent/<short-task-name>` first.
3. **Test early**: find and run the test command before changing code so you know the baseline.
4. **Commit small**: one logical change per commit, imperative subject line under 72 chars.
5. **Hand off**: finish by writing `HANDOFF.md` in the repo root with what changed, how it was
   verified, and anything left undone. The orchestrator reads this file.
