# Repository guidance

## Design

- Airframe, in [Sean-Koval/agent-factory](https://github.com/Sean-Koval/agent-factory), is the design source of truth for the web console (`src/hangar/ui/`), the agent screen (`vnc.html`) and the docs site.
- Before any UI work, read `airframe/README.md`, the brand book in `airframe/guide/` (brand, Status, Data, Products) and the README of each component you touch in `airframe/components/<Name>/`, all in agent-factory.
- Use `var(--af-*)` tokens and `af-` classes only: no color literals, fonts or radii of your own. `tests/test_ui_airframe.py` fails on a hex, `rgb()` or `hsl()` color in `app.css`, `index.html`, `vnc.html`, `app.js` or `fleet.js`.
- The Fleet and Needs-you pages (`fleet.js`) are Airframe's FleetConsole (theme-v12): List (AgentRow) and Board (AgentCard), two views over one data model (`GET /api/fleet` and its stream) and one selection, and the DecisionDeck, which is the whole Needs-you page and a collapsible band atop the Fleet (one component, two placements). Owners is a filter (`?owner=`), not a view; Bays, Columns and Owners are retired. Read the FleetConsole, AgentRow, AgentCard, DecisionDeck, StageRail, Heartbeat and ActivityTimeline READMEs before changing it. Statuses come from the server (`fleet_api.py` maps a new sandbox state to one of the six); keep every sandbox action in the opened agent's console (`consoleHtml`) so both views get it, and fill a stage rail solid only from `stage_pct` (an estimate is dotted and labelled as one).
- Statuses follow the dark cockpit: running and queued stay grey; only needs approval (orange diamond), failed (red triangle) and denied (red placard) carry color. The brand yellow is for the primary action, focus and selection, never a status. hangar's hue (`--af-product-hangar`) appears only in its mark, the docs header and the 2px nav marker.
- Never edit the vendored files: `src/hangar/ui/airframe/` and `docs/stylesheets/airframe.css` and `airframe-docs.css`. Change Airframe in agent-factory, then refresh with `sh scripts/sync-airframe.sh theme-vN`. hangar's product hue for the docs lives in `docs/stylesheets/product.css`.
- Coding agents: the Airframe skill is committed at `.claude/skills/airframe/` (Claude Code) and `.agents/skills/airframe/` (Codex) with the rules and the class map; refresh it with `npx shadcn@latest add https://sean-koval.github.io/agent-factory/r/skill.json` and never edit it here.

## Docs

- The site is `mkdocs.yml` and `docs/`, built with Zensical as `.github/workflows/docs.yml` does. Build it locally with `pip install zensical==0.0.71`, then `zensical build --strict` (into `site/`, which is not committed) or `zensical serve` (http://localhost:8000, rebuilt on save). `--strict` fails on a broken link or anchor, as CI does.
- `tests/test_front_page.py` keeps README.md and `docs/index.md` honest: the security posture section in both, and one features table with a status per row.
- Write for the site as agent-factory's AGENTS.md says ("Writing for the site"): a page answers one question for one reader; a paragraph is at most 60 words, a sentence at most 25 words (a semicolon ends one), and a page at most 700 words of prose, or it is split. `tests/test_editorial.py` measures `docs/` and README.md (`python tests/test_editorial.py` prints every page's numbers). A page written before the rule is listed there with a reason and ceilings that only go down; a new page keeps the whole rule. When you split a page, keep its old URL and anchors working: leave the page as the first question's answer, with a "Moved from this page" table whose cells carry the old anchors.

## Tracking

- Work is tracked in Jira project KAN (<https://seanmkoval.atlassian.net>). GitHub Projects is retired; GitHub Issues are for outside reports only.
- Work tied to a ticket carries its key, so the GitHub for Jira app links it: the branch starts with it (`KAN-35-apply-atomic`), the PR title starts with it (`KAN-35: make hangar apply all or nothing`) and the PR body has a `Jira: KAN-35` line. Commits may mention it. Work without a ticket (typo fixes, outside contributions) needs no key.
- Lanes: only `lane:agent` tickets (job-sized: one repo or one change-set profile, acceptance criteria, a verify command) start a hangar job, when moved to "Ready for agent". `lane:pair` is Sean with Claude Code; `lane:human` is Sean only.

## Merging

- `.github/CODEOWNERS` lists hangar's security-relevant code (sandboxing, egress, credentials, auth, approvals, the gateway, taint, signing and webhooks), plus `deploy/`, `.github/` and `scripts/ci/`. `tests/test_codeowners.py` fails when a module that looks security-relevant has no entry.
- Proposed auto-merge rule (KAN-220; not in force until the owner adopts it): Claude may merge a PR itself only when all of these hold. Otherwise a person merges it.
  - It is a `lane:agent` ticket's PR, or a Dependabot PR.
  - Its CI is green: every required check passed on its head commit.
  - It touches no path `.github/CODEOWNERS` owns.
