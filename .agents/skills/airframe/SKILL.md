---
name: airframe
description: Build or change web UI, docs pages, charts or marketing pages for the agent-factory suite (hangar, flightdeck, Tower, ai-dlc, Spaceport) with Airframe, the shared design system. Use whenever you write HTML, CSS, JSX, Markdown with HTML, or SVG for these products, or when a user mentions Airframe, af- classes, --af- tokens, the dark cockpit, tail numbers or the theme-vN releases.
license: Apache-2.0
metadata:
  source: https://github.com/Sean-Koval/agent-factory/tree/main/airframe
  docs: https://sean-koval.github.io/agent-factory/airframe/
---

# Airframe

Airframe is the design source of truth for every tool in the suite. It lives in `airframe/` of
Sean-Koval/agent-factory; the tool repos vendor its generated files unmodified (`theme/airframe.css`,
`theme/airframe-components.css`, `theme/airframe-icons.svg`) with `sh scripts/sync-airframe.sh theme-vN`. If
this project has an `airframe/` folder, these files were installed from the Airframe registry; read the
component contracts in `airframe/components/*.md` before writing markup.

## The five rules

1. **Tokens only.** Every color, font, radius, spacing and shadow is `var(--af-<name>)`; never a hex, `rgb()`,
   `hsl()` or a font name of your own. The repos' guard tests fail on a color literal in UI files.
2. **Dark cockpit.** Night is the default theme (true black canvas) and Day must work; switch with
   `data-theme="light"` on `<html>` or any element. Running and queued are grey. Color appears only for
   *needs approval* (`status-approval`, orange diamond), *failed* (`status-failed`, red triangle) and *denied*
   (`status-denied` placard). There is no green, no blue, no spinner, no pulse.
3. **One hot color.** Signal yellow `brand` is for the one primary action per view, the focus ring and
   selection. It is never a status.
4. **Tonal, sharp, quiet.** Cards are `raised` on `canvas` with no border and no shadow; 2px controls, 4px
   cards, 1px lines; no gloss, gradients or blur. Separate by tone and space.
5. **Data first.** Real IDs as tail numbers (`HGR-7Q2K`, `FDK-EXP-0142`, `TWR-APR-0391`) in `af-id`, real
   numbers in DM Mono with tabular figures. Sentence case everywhere; the CSS uppercases labels and buttons.

## How to use the classes

- Load `airframe.css` (tokens) then `airframe-components.css` (classes). Icons: `<svg class="af-ic"
  aria-hidden="true"><use href="/airframe/airframe-icons.svg#af-<name>"/></svg>`.
- Compose markup from the `af-` classes exactly as the component README shows; do not restyle them. Put what
  Airframe has no component for in the product's own stylesheet, with tokens only, and propose anything shared
  as a variant in agent-factory rather than forking it.
- A product's hue (`--af-product-hangar` and the like) appears only in its mark, the docs header and the
  2px nav marker.
- Type: `.af-type-<style>` or the tokens `--af-font-display` (Archivo Expanded, `font-stretch:
  var(--af-display-stretch)`), `--af-font-sans` (Red Hat Text), `--af-font-mono` (DM Mono).
- Tailwind v4 projects: import `airframe-tailwind.css` after `airframe.css`; utilities are `bg-canvas`,
  `text-fg-muted`, `border-line`, `bg-brand text-on-brand`, `font-display`, `text-body`, `rounded-card`.

## Components (airframe/components/<Name>/README.md is the contract)

| Name | Group | What it is |
|---|---|---|
| Avatar | Foundations | `af-avatar af-avatar--person\|agent\|firm` + `--22\|28\|44`: people round, agents hexagonal, firms square; `af-avatars` stacks; `af-who` beside a name. No hue, no photos. |
| Button | Foundations | `af-btn af-btn--primary\|secondary\|ghost\|danger`, `af-btn--sm`, `af-btn--icon`; `af-btn--text` for sentence-case where people read. One primary per view; Deny is danger beside Approve. |
| Icons | Foundations | 55 stroke glyphs on a 20px grid from the sprite; `af-ic`, `af-ic--14\|20\|24`. |
| InlineData | Foundations | `af-meta` > `af-meta__item` > `af-meta__key` + `af-meta__val`: KEY value, never "a · b · c". |
| Label | Foundations | `af-lbl` with optional `af-lbl__sub` and icon: small tracked mono capitals naming a card or section. |
| ObjectChip | Foundations | `af-obj` (`a` with `af-obj--link`, or `span`) > source icon, `af-obj__key`, `__title`, `__state`; `af-obj--restricted` shows the key only; the approval mark is the one color; `af-obj__trust` on hover. Never a pill. |
| ProductMarks | Foundations | `af-mark af-hue--<product>`: outline tile with the product glyph; the hue's only other home is the nav marker. |
| Readout | Foundations | `af-readout af-readout--s\|m\|l`: mono digits on unlit cells in a recessed window. |
| Status | Foundations | `af-status af-status--running\|queued\|approval\|failed\|denied\|off` + `<i class="af-status__mark">` + word; `af-status--lit` colors the word. |
| Card | Cards | `af-card` with `af-card__top`, `__body`, `__title`, `__desc`, `__foot`; `af-card--lit` only while a person is needed; `af-grid af-grid--2\|3\|4`. |
| EnvironmentCard | Cards | An image a sandbox boots from: name, purpose, digest, harness; one failed build. |
| SummaryCards | Cards | The four numbers at the top of a console: running, waiting on you, spend today, turn to PR. |
| Composer | Controls | `af-composer` > `af-composer__queued` (Queued · applies after this tool call, with a take-back), `af-composer__box` (textarea, `af-chip` suggestions, one primary Send), `af-composer__why`; `is-disabled` says why. |
| Controls | Controls | Switch, segmented control (`af-seg`), text input with key hint, select, `af-input--area` textarea, `af-chip` options; square and recessed. |
| Navigation | Navigation | 56px app header with product switcher, mono tabs with the 2px product marker, search, avatar; breadcrumb row. |
| PageHeader | Patterns | `af-page-head` (`--s` for 24 px, `--rule`) > `__text` (`__title`, `__lede`, `__sum` with status + `af-meta` + `af-cap`) + `__actions`. Stacks below 760 px. |
| EmptyState | Patterns | `af-empty[data-kind=no-data\|after-action\|error]`, `af-empty--clear` for "Nothing is waiting on you.", `af-empty--compact`; one primary, one link at most; errors: no icon, no second action. |
| Toast | Patterns | `af-toasts[role=status]` > `af-toast` (`--bad` with the failed mark, `__msg`, one `__act`); bottom centre; held while a dialog, drawer or field is active. `toast()` in airframe.js. |
| Dialog | Patterns | `<dialog class="af-dialog">` > `af-dialog__form` > `__head` (`__title`), `__body`, `__foot` in inspect, refuse, accept order; `--danger` makes the verb a danger button. `confirm()` in airframe.js replaces `window.confirm()`. |
| Drawer | Patterns | `af-scrim` + `af-drawer af-drawer--right\|bottom` > `__head`, `__body`, `__foot`; `is-open`; toast above drawer above scrim. `drawer.open/close` in airframe.js. |
| MasterCaution | Alerts | The one large block of color: flat caution (orange) or warning (red) lamp with a black legend; dark when clear. |
| ApprovalCard | Governance | A held action waiting on a reviewer: command, policy, checks, expiry; inspect / deny (reason required) / approve, approve edited, approve for the session; `af-approval__quorum`; waiting on others is the grey queued ring; verdict rows. |
| Question | Governance | `af-card af-card--lit af-question`: an agent asks a person; the approval diamond with the word Needs input; `af-question__q`, `__ctx`, `__opts` chips, `__free` text (dismisses the options), one primary Answer. |
| Diff | Data | `af-diff` > `__file` (`__path`, `__stat` +N −M), `__hunk` (`__hh`, `__line--ctx\|add\|del` with numbers, sign column, `__code`, `__cmt`), `__thread` with `af-comment` and a Composer; `af-diff__hunk--failed` carries the only color. Greyscale: no red, no green. |
| FleetTable | Data | Dense 32px rows on a tonal panel: tail number, environment, harness and model, status, CPU, age, cost. |
| Terminal | Data | Always dark in both themes; quiet ✓ lines, red ✗ lines, prompt and cursor in brand. |
| TraceSpans | Data | One agent turn as a span tree on a timeline; the critical path in brand. |
| FleetBays | Fleet | `af-bays` > `af-bay af-bay--<state>`: a box per sandbox with task, owner, `af-track` stage track, lease left, one action. |
| FleetColumns | Fleet | `af-statecols`: one column per state, attention first, Running two wide. |
| FleetOwners | Fleet | `af-owners`: people down, environments across, stand tiles in the cells, share of the per-person cap. |
| FleetView | Fleet | Pools of `af-stand` tiles, switchable to session lanes; the whole fleet at a glance. |
| SessionLanes | Fleet | One row per agent session over the last 60 minutes: stages, waits, outcomes. |
| Hero | Marketing | Display headline, lede, two actions and a display-window panel over the dot field. |

## Helpers (airframe.js, optional)

`theme/airframe.js` is one ES module with no dependencies: `status(role, word)`, `icon(name)`, `meta([[key, val]])`,
`btn(label, {kind, sm, icon, text})`, `avatar(kind, initials, {size})`, `obj({source, key, title, state, href, restricted,
approval, trust})`, `tag(text)` return markup matching the contracts with every value escaped; `toast(msg, {tone, action,
timeout, critical})`, `await confirm({title, body, ok, cancel, danger})` and `drawer.open/close(el)` carry the behaviour,
including alert inhibition (non-critical toasts wait while a dialog or drawer is open or a field has focus). Use it instead
of writing your own `status()`/`btn()` builders; in a React app compose the same classes.

## Copy

Short, plain, specific: "HGR-3MX9 is holding at PR. Push to main needs your approval." Numbers are the proof
("38 of 41 passed"). No emoji, no exclamation marks, no lorem ipsum, no "we". Status words: Running, Queued,
Needs approval, Needs input (a question, same diamond), Waiting on 2 others (queued ring, grey: nobody here is needed),
Failed, Denied, Spun down. A deny needs a reason; an approve may carry a note. Diffs are greyscale. Live surfaces say
"Agent has control" (grey) or "You have control" (brand).

## When something is missing

Do not invent a color or a component. Say what Airframe lacks, build the minimum with tokens in the product's
own CSS, and note it for agent-factory. The brand book (`airframe/guide/brand.md`, `Status.md`, `Data.md`,
`Products.md`) has the detailed rules; the gallery renders every component in both themes:
https://sean-koval.github.io/agent-factory/airframe/gallery/
