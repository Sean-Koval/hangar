/* hangar console: the Fleet page. The same sandboxes four ways, from Airframe theme-v3 (vendored in /ui/airframe/):
   Board (FleetView), Bays (FleetBays), Columns (FleetColumns) and Owners (FleetOwners). One selection is shared by all
   four and opens one detail panel (beside the Board, a drawer elsewhere) that carries every sandbox action.
   A classic script loaded after app.js, whose helpers it uses ($, api, esc, ic, status, meta, btn, avatar, ago, until,
   busy, emptyState, confirmAction) along with Airframe's drawer (window.airframe). */
"use strict";

const FLEET_VIEWS = ["board", "bays", "columns", "owners"];
const FLEET_STORE = "hangar.fleetView";
const STAGES = ["plan", "edit", "test", "pr"];
const STAGE_WORD = { plan: "Plan", edit: "Edit", test: "Test", pr: "PR" };
// Attention first everywhere: what needs a person, then what failed, then what policy stopped, then the quiet
// states. The page header's mark and counts, each pool's counts and the order of tiles all follow this one list.
const ATTENTION = ["approval", "failed", "denied", "running", "queued", "off"];
const ROLE_RANK = Object.fromEntries(ATTENTION.map((r, i) => [r, i]));
// sandbox state -> Airframe role and word, when nothing about its job or session says more
const SB_ROLE = { pending: "queued", starting: "queued", running: "running", paused: "off", stopping: "off", stopped: "off",
  error: "failed" };
const SB_WORD = { pending: "Starting", starting: "Starting", running: "Running", paused: "Paused", stopping: "Stopping",
  stopped: "Spun down", error: "Failed" };
const LIVE = ["running", "paused", "starting", "pending"];
const INTERNAL = new Set(["system", "reaper", "session", "jobs", "mcp"]);

const fleet = { view: "board", units: [], byId: new Map(), selected: null, people: {}, peopleAt: 0, spend: null,
  spendAt: 0, painted: {}, attention: null };

/* ------------------------------------------------------------------ the data: one unit per sandbox */
// The person a record belongs to, as fairness.person_of reads an actor ("ui:sean", "@sean:laptop",
// "oauth:client(sean)"); a record's own person wins. Hangar's own actors (jobs, session:…) have none.
function personOf(actor, person) {
  if (person) return person;
  let a = actor || "";
  for (const p of ["mcp:", "ui:", "e2b:"]) if (a.startsWith(p)) { a = a.slice(p.length); break; }
  if (!a || INTERNAL.has(a) || /^(session|job|hangar|schedule|trigger):/.test(a)) return null;
  if (a.startsWith("@") && a.includes(":")) return a.slice(1).split(":")[0] || null;
  const m = /^oauth:.*\((.+)\)$/.exec(a);
  return m && m[1] !== "?" ? m[1] : a;
}
const personName = (id) => (id ? fleet.people[id]?.name || id : "hangar");
const firstName = (id) => personName(id).split(" ")[0];
const ownerOf = (u) => (u.owner ? personName(u.owner) : u.actor || "hangar");
const initialsOf = (name) => {
  const w = String(name || "").split(/[\s._-]+/).filter(Boolean);
  return ((w.length > 1 ? w[0][0] + w[1][0] : (w[0] || "?").slice(0, 2)) || "?").toUpperCase();
};
const initials = (id) => initialsOf(personName(id));
// the avatar triad: people are round, agents hexagonal (the harness's initials; the agent glyph when there is none)
const personAvatar = (id, size = 28) => avatar("person", initials(id), { size, name: personName(id) });
const agentAvatar = (harness, size = 28) => avatar("agent", harness ? initialsOf(harness) : "", { size, name: harness || "agent" });
const repoShort = (r) => (r || "").replace(/^https?:\/\/(www\.)?github\.com\//, "").replace(/\.git$/, "");
const dur = (s) => (s < 3600 ? `${Math.max(1, Math.round(s / 60))} min`
  : `${Math.floor(s / 3600)} h${Math.round((s % 3600) / 60) ? ` ${Math.round((s % 3600) / 60)} min` : ""}`);
const left = (s) => (s <= 0 ? "expiring" : s < 3600 ? `${Math.round(s / 60)} min left` : `${(s / 3600).toFixed(1)} h left`);

// The stage a job's or session's phase gives: the agent's turn is editing, the check is testing, delivery is the PR.
// Anything else (queued, idle, waiting for approval to start) has no stage, so no pips and no track.
function stageOf(job, ses, held) {
  if (job) {
    if (job.state === "verifying") return "test";
    if (job.state === "delivering" || job.state === "succeeded") return "pr";
    if (job.state === "running" || job.state === "needs_input") return /\bgit\s+push\b|\bgh\s+pr\b/.test(held || "") ? "pr" : "edit";
    return null;
  }
  return ses && (ses.state === "running" || ses.busy) ? "edit" : null;
}

function buildUnits() {
  const now = Date.now() / 1000;
  const jobs = state.jobs || [], sessions = state.sessions || [];
  const jobById = new Map(jobs.map((j) => [j.id, j])), sesById = new Map(sessions.map((s) => [s.id, s]));
  const latest = (list, key) => {
    const m = new Map();
    for (const x of list) if (x[key] && (!m.has(x[key]) || x.created_at > m.get(x[key]).created_at)) m.set(x[key], x);
    return m;
  };
  const jobBySb = latest(jobs, "sandbox_id"), sesBySb = latest(sessions, "sandbox_id");
  return (state.sandboxes || []).map((sb) => {
    const ses = sesBySb.get(sb.id) || sesById.get((sb.created_by || "").replace(/^session:/, ""));
    const job = jobBySb.get(sb.id) || jobById.get((sb.created_by || "").replace(/^job:/, "")) || (ses?.job_id ? jobById.get(ses.job_id) : null);
    const kind = job ? "job" : ses ? "session" : "sandbox";
    const owner = job ? personOf(job.requester, job.person) : ses ? personOf(ses.owner, ses.person) : personOf(sb.created_by, sb.person);
    const seen = new Set();
    const pending = [...(job?.pending_approvals || []), ...(ses?.pending_approvals || [])]
      .filter((a) => a && !seen.has(a.id) && seen.add(a.id));
    const turn = ses?.turns?.at(-1);
    const denial = turn && !turn.finished_at
      ? (turn.approvals || []).filter((a) => a.status === "denied" && a.by === "policy").at(-1) : null;
    const question = job?.result?.question || ses?.needs_input || "";
    const live = LIVE.includes(sb.state);
    let role = SB_ROLE[sb.state] || "off", label = SB_WORD[sb.state] || word(sb.state), reason = null, ask = null;
    if (sb.state === "error") reason = ["Error", sb.error || "The sandbox stopped on an error."];
    else if (job?.state === "awaiting_approval") {
      role = "approval"; label = "Needs approval"; ask = { kind: "job", job: job.id };
      reason = ["Held", job.approval?.reason || "Started from outside the team: waits for a trusted person to approve it."];
    } else if (pending.length && live) {
      const a = pending[0];
      role = "approval"; label = "Needs approval";
      ask = { kind: "tool", approval: a.id, job: job?.id, session: ses?.id || job?.session_id };
      reason = ["Held", `${a.tool || a.kind || "A request"}${a.detail ? `: ${a.detail}` : a.summary ? `: ${a.summary}` : ""}`
        + `${a.taint ? ` (${a.taint})` : ""}`];
    } else if (question && live && (job?.state === "needs_input" || (ses && ses.state !== "running" && ses.needs_input))) {
      role = "approval"; label = "Needs input"; reason = ["Question", question];
    } else if (denial && live) {
      role = "denied"; label = "Denied";
      reason = ["Policy", `${denial.tool || "A tool"}${denial.detail ? `: ${denial.detail}` : ""} denied${denial.message ? `. ${denial.message}` : ""}`];
    } else if (job?.state === "failed" && live) {
      role = "failed"; label = "Failed";
      const r = job.result || {};
      reason = ["Failed", r.result_error || (r.verify_exit_code ? `The check failed (exit ${r.verify_exit_code}).`
        : `${word(job.reason || "failed")}.`)];
    }
    const stage = role === "off" || role === "queued" ? null : stageOf(job, ses, reason?.[1]);
    const runs = sb.info?.runs || [];
    return {
      id: sb.id, tail: sb.id.slice(-4), name: sb.name, env: sb.environment, provider: sb.provider, sb, job, ses, kind,
      owner, actor: job?.requester || ses?.owner || sb.created_by || "", harness: job?.harness || ses?.harness || sb.info?.harness || "", model: job?.model || ses?.model || "",
      role, label, reason, ask, stage, live,
      work: job?.title || ses?.title || (ses ? ses.name : "") || sb.name,
      task: job?.instructions || turn?.message || runs.at(-1)?.prompt || "",
      age: Math.max(0, now - sb.created_at),
      left: live && sb.expires_at ? sb.expires_at - now : null,
      leftFrac: live && sb.expires_at && sb.expires_at > sb.created_at
        ? Math.max(0, Math.min(1, (sb.expires_at - now) / (sb.expires_at - sb.created_at))) : 0,
    };
  });
}
// Stable order: attention first, then by when the sandbox started, so a refresh never moves a tile that didn't change.
const byAttention = (a, b) => ROLE_RANK[a.role] - ROLE_RANK[b.role] || a.sb.created_at - b.sb.created_at || a.id.localeCompare(b.id);
const envNames = (units) => [...new Set(units.map((u) => u.env))].sort();

/* ------------------------------------------------------------------ loading */
function fleetViewFromHash() {
  const q = new URLSearchParams((location.hash || "").split("?")[1] || "");
  let v = q.get("view");
  if (FLEET_VIEWS.includes(v)) { try { localStorage.setItem(FLEET_STORE, v); } catch {} return v; }
  try { v = localStorage.getItem(FLEET_STORE); } catch { v = null; }
  return FLEET_VIEWS.includes(v) ? v : "board";
}

async function fleetExtras() {
  // names for owners (5 min) and, for Owners, what each person spent today (1 min); both admin-only and optional
  const now = Date.now();
  const jobs = [];
  if (now - fleet.peopleAt > 300000) {
    fleet.peopleAt = now;
    jobs.push(api("GET", "/api/people").then((ps) => { fleet.people = Object.fromEntries(ps.map((p) => [p.id, p])); }).catch(() => {}));
  }
  if (fleet.view === "owners" && now - fleet.spendAt > 60000) {
    fleet.spendAt = now;
    const today = new Date().toISOString().slice(0, 10);
    jobs.push(api("GET", `/api/usage?group_by=session&since=${today}&limit=500`).then((r) => { fleet.spend = r.rows; }).catch(() => {}));
  }
  await Promise.all(jobs);
}

async function loadFleet(quiet = false) {
  fleet.view = fleetViewFromHash();
  if (state.sandboxes.length || fleet.units.length) renderFleet();  // switch views at once, on the data we have
  try {
    const [sbs, jobs, sessions] = await Promise.all([
      api("GET", `/api/sandboxes?include_stopped=${$("#show-stopped").checked}`),
      api("GET", "/api/jobs?limit=200").catch(() => state.jobs),
      api("GET", "/api/sessions?limit=200").catch(() => state.sessions),
    ]);
    Object.assign(state, { sandboxes: sbs, jobs, sessions });
    await fleetExtras();
    renderFleet();
    renderJobs(); renderSessions();  // the nav counts
  } catch (e) { if (!quiet && e.message !== "unauthorized") toast(e.message, true); }
}
$("#show-stopped").addEventListener("change", () => loadFleet());

/* ------------------------------------------------------------------ rendering */
function renderFleet() {
  const units = buildUnits().sort(byAttention);
  fleet.units = units;
  fleet.byId = new Map(units.map((u) => [u.id, u]));
  if (fleet.selected && !fleet.byId.has(fleet.selected)) fleet.selected = null;
  const root = $("#view-fleet");
  root.dataset.fleetView = fleet.view;
  root.classList.toggle("has-selection", !!fleet.selected);
  root.classList.toggle("is-empty", !units.length);
  $$(".fleet-views [data-fleet-view]").forEach((a) => {
    if (a.dataset.fleetView === fleet.view) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
  });
  navCount("fleet", units.filter((u) => u.live).length);
  $("#fleet-sub").innerHTML = summaryHtml(units);
  const body = { board: boardHtml, bays: baysHtml, columns: columnsHtml, owners: ownersHtml }[fleet.view](units);
  paint($("#bays"), body, `view:${fleet.view}`);
  paint($("#fleet-detail"), detailHtml(fleet.byId.get(fleet.selected)), "detail");
  placeDetail();
  // say it when something new needs a person (the screen reader hears it; the screen already shows it)
  const waiting = units.filter((u) => ROLE_RANK[u.role] <= 2).map((u) => u.id).sort().join(" ");
  if (fleet.attention !== null && waiting !== fleet.attention) {
    const n = units.filter((u) => u.role === "approval").length, bad = units.filter((u) => u.role === "failed" || u.role === "denied").length;
    $("#fleet-live").textContent = `${n} sandbox${n === 1 ? "" : "es"} need${n === 1 ? "s" : ""} you, ${bad} failed or denied.`;
  }
  fleet.attention = waiting;
}

// Repaint only what changed, keeping focus, scroll and the roving tab stop, so the 4 s refresh is invisible.
function paint(el, html, key) {
  if (fleet.painted[key] === html && el.dataset.painted === key) return;
  fleet.painted[key] = html;
  const active = document.activeElement;
  const inside = el.contains(active) && active !== el;
  const focusId = inside ? active.closest("[data-id]")?.dataset.id : null;
  const focusSel = inside ? focusKey(active) : "";
  const scrolls = [...el.querySelectorAll("[data-scroll]")].map((s) => [s.dataset.scroll, s.scrollLeft, s.scrollTop]);
  const own = [el.scrollLeft, el.scrollTop];
  el.innerHTML = html;
  el.dataset.painted = key;
  [el.scrollLeft, el.scrollTop] = own;
  for (const [k, l, t] of scrolls) {
    const s = el.querySelector(`[data-scroll="${CSS.escape(k)}"]`);
    if (s) { s.scrollLeft = l; s.scrollTop = t; }
  }
  tabStops(el);
  if (focusSel) {
    const scope = focusId ? el.querySelector(`[data-id="${CSS.escape(focusId)}"]`) : el;
    const target = !scope ? null : scope.matches(focusSel) ? scope : scope.querySelector(focusSel);
    if (target) { if (target.hasAttribute("data-tile")) target.tabIndex = 0; target.focus({ preventScroll: true }); }
  }
}
// what identifies a focused control across a repaint: its data attributes (the tile, the action, the run, the env)
const focusKey = (n) => ["data-tile", "data-act", "data-go", "data-select", "data-run", "data-env", "data-fleet-close"]
  .filter((a) => n.hasAttribute(a)).map((a) => `[${a}="${CSS.escape(n.getAttribute(a))}"]`).join("");
// one tab stop per board (the selected tile, else the first); arrow keys move within it
function tabStops(el) {
  for (const board of el.querySelectorAll("[data-board]")) {
    const tiles = [...board.querySelectorAll("[data-tile]")];
    tiles.forEach((t) => (t.tabIndex = -1));
    (tiles.find((t) => t.dataset.id === fleet.selected) || tiles[0])?.setAttribute("tabindex", "0");
  }
}

function counts(units) {
  const c = { running: 0, approval: 0, failed: 0, denied: 0, queued: 0, paused: 0, off: 0 };
  for (const u of units) {
    if (u.sb.state === "paused" && u.role === "off") c.paused++;
    else c[u.role]++;
  }
  return c;
}
// keyed counts in attention order: what needs a person first, then failed, then denied, then the quiet states
const countMeta = (c, keep = []) => meta([
  c.approval || keep.includes("approval") ? ["needs you", c.approval, c.approval ? "approval" : ""] : null,
  c.failed || keep.includes("failed") ? ["failed", c.failed, c.failed ? "failed" : ""] : null,
  c.denied || keep.includes("denied") ? ["denied", c.denied, c.denied ? "failed" : ""] : null,
  ["running", c.running],
  ["starting", c.queued],
  c.paused || keep.includes("paused") ? ["paused", c.paused] : null,
  c.off ? ["spun down", c.off] : null,
]);
const capHtml = (n, max, sm = false, unit = "slots") => max
  ? `<span class="af-cap${sm ? " af-cap--sm" : ""}"><span class="af-cap__bar" aria-hidden="true"><i style="width:${Math.min(100, Math.round(100 * n / max))}%"></i></span>${n} of ${max} ${unit}</span>`
  : `<span class="af-cap${sm ? " af-cap--sm" : ""}">${n} ${unit}</span>`;

function summaryHtml(units) {
  if (!units.length) return `<span class="af-muted">No sandboxes running.</span>`;
  const c = counts(units);
  // the header leads with what needs a person, not with the loudest state: a denied sandbox is a record, a held one is work
  const top = ATTENTION.find((r) => units.some((u) => u.role === r));
  const words = { approval: "Needs you", failed: "Failed", denied: "Denied", running: "Running", queued: "Starting",
    off: units.some((u) => u.sb.state === "paused") ? "Paused" : "Spun down" };
  const active = units.filter((u) => u.live).length;
  return `${status(top, words[top])}${countMeta(c, ["approval", "failed", "denied", "paused"])}${capHtml(active, state.overview?.max_sandboxes)}`;
}

/* ---- shared parts */
const tip = (u) => [`${u.id}  ${u.name}`, `${u.label}${u.reason ? `: ${u.reason[1]}` : ""}`, u.work !== u.name ? u.work : "",
  u.task ? u.task.slice(0, 600) : ""].filter(Boolean).join("\n");
const aria = (u) => `${u.id}, ${u.label}${u.stage ? `, ${STAGE_WORD[u.stage]} stage` : ""}, ${u.work}`;
const pipCls = (u, i) => (!u.stage ? "" : i < STAGES.indexOf(u.stage) ? " af-stand__pip--done" : i === STAGES.indexOf(u.stage) ? " af-stand__pip--now" : "");
const pips = (u) => `<span class="af-stand__pips" aria-hidden="true">${u.stage ? STAGES.map((_, i) => `<i class="af-stand__pip${pipCls(u, i)}"></i>`).join("") : ""}</span>`;
function track(u) {
  if (!u.stage) return "";  // no phase, no track
  const at = STAGES.indexOf(u.stage);
  return `<div class="af-track" aria-label="Stage ${STAGE_WORD[u.stage]}">${STAGES.map((s, i) =>
    `<span class="af-track__seg${i < at ? " af-track__seg--done" : i === at ? " af-track__seg--now" : ""}"><i></i>${STAGE_WORD[s]}</span>`).join("")}</div>`;
}
const note = (u) => (u.reason ? `<p class="af-bay__note"><b>${esc(u.reason[0])}</b>${esc(u.reason[1])}</p>` : "");
const sel = (u) => u.id === fleet.selected;
const stand = (u) => `<button type="button" class="af-stand af-stand--${u.role}" data-id="${esc(u.id)}" data-tile aria-pressed="${sel(u)}"
  aria-label="${esc(aria(u))}" title="${esc(tip(u))}"><span class="af-stand__tail">${esc(u.tail)}</span><i class="af-stand__mark"></i>${pips(u)}</button>`;
// the one action a bay or a card offers; everything else is in the detail panel
function oneAction(u, primaryOk = true) {
  const go = u.job ? `job:${u.job.id}` : u.ses ? `session:${u.ses.id}` : null;
  const b = (kind, attrs, label, icon) => btn(label, { kind, sm: true, icon, attrs });
  if (u.role === "approval") {
    const k = primaryOk ? "primary" : "secondary";
    return u.label === "Needs input" && go ? b(k, { "data-go": go }, "Answer") : b(k, { "data-select": u.id }, "Review");
  }
  if (u.role === "denied") return b("secondary", { "data-select": u.id }, "Decision");
  if (u.role === "failed") return u.job ? b("secondary", { "data-go": `job:${u.job.id}` }, "Open job") : b("secondary", { "data-select": u.id }, "Details");
  if (u.sb.state === "paused") return b("ghost", { "data-act": "resume" }, "Resume", "play");
  if (u.sb.state === "running") return b("ghost", { "data-act": "term" }, "Terminal", "terminal");
  if (go) return b("ghost", { "data-go": go }, u.job ? "Open job" : "Open session");
  return "";
}
const envIcon = (env) => ic(/gpu/i.test(env) ? "gpu" : "environment");
// an empty fleet (Airframe EmptyState, no-data): one action and one link (the page header already has the primary);
// the Bays view keeps a start bay per environment
const emptyView = (title, text, start = true) => emptyState({ icon: "fleet", title, text, cls: "fleet-empty", actions: start
  ? `${btn("Start a sandbox", { attrs: { "data-act": "new" } })}<a class="af-link" href="#environments">Environments</a>`
  : `<a class="af-link" href="#environments">Environments</a>` });

/* ---- Board: every sandbox as a stand, by environment (FleetView) */
function boardHtml(units) {
  if (!units.length) return emptyView("No sandboxes running.", "Start one from an environment; the first boot of an image takes about a minute.");
  const pools = envNames(units).map((env) => {
    const us = units.filter((u) => u.env === env);
    return `<section class="af-fleet__pool" aria-label="${esc(env)}">
      <div class="af-fleet__pool-head">${envIcon(env)}<span class="af-fleet__pool-name">${esc(env)}</span>${countMeta(counts(us))}
        <span class="af-fleet__cap">${us.filter((u) => u.live).length} live</span></div>
      <div class="af-fleet__board">${us.map(stand).join("")}</div></section>`;
  }).join("");
  const key = (role, w) => `<span class="af-viz-key__item"><span class="af-status af-status--${role}"><i class="af-status__mark"></i>${w}</span></span>`;
  return `<div class="af-fleet fleet-board"><div class="af-fleet__body" data-board>${pools}</div>
    <div class="af-viz-key">${key("running", "Running")}${key("queued", "Starting")}${key("approval", "Needs you")}${key("failed", "Failed")}
      <span class="af-viz-key__item"><span class="af-stand af-stand--denied fleet-key-stand" aria-hidden="true"></span>Denied</span>${key("off", "Paused or spun down")}
      <span class="af-viz-key__item"><span class="af-stand__pips fleet-key-pips" aria-hidden="true"><i class="af-stand__pip af-stand__pip--done"></i><i class="af-stand__pip af-stand__pip--done"></i><i class="af-stand__pip af-stand__pip--now"></i><i class="af-stand__pip"></i></span>stage: plan, edit, test, PR</span>
      <span class="af-viz-key__item fleet-key-hint">Arrow keys move, Enter opens</span></div></div>`;
}

/* ---- Bays: a box per sandbox you can read and act on (FleetBays) */
function bay(u) {
  const by = u.kind === "job" ? "job by" : u.kind === "session" ? "session of" : "started by";
  const time = u.live ? (u.sb.state === "paused" ? "paused" : u.left != null ? left(u.left) : "") : u.sb.state === "error" ? "" : word(u.sb.state);
  return `<article class="af-bay af-bay--${u.role}${sel(u) ? " is-selected" : ""}" data-id="${esc(u.id)}" data-tile aria-label="${esc(aria(u))}" title="${esc(tip(u))}">
    <div class="af-bay__top"><span class="af-bay__id">${esc(u.id)}</span>${status(u.role, u.label)}</div>
    <p class="af-bay__title">${esc(u.work)}</p>
    ${meta([[by, ownerOf(u)], ["harness", u.harness]])}
    ${track(u)}${note(u)}
    <div class="af-bay__foot"><span class="af-bay__time"><span><b>${dur(u.age)}</b>${time ? ` · ${esc(time)}` : ""}</span>
      <span class="af-bay__left" aria-hidden="true"><i style="width:${Math.round(100 * u.leftFrac)}%"></i></span></span>${oneAction(u)}</div>
  </article>`;
}
function baysHtml(units) {
  const envs = units.length ? envNames(units) : (state.envs || []).filter((e) => !e.error).map((e) => e.name).sort();
  if (!envs.length) return emptyView("No environments yet.", "Create one under Environments, then start a sandbox from it.", false);
  const start = (env) => `<button type="button" class="af-bay af-bay--new" data-act="new" data-env="${esc(env)}">${ic("plus")}Start in ${esc(env)}</button>`;
  if (!units.length) return `<div class="fleet-groups"><div class="af-bays">${envs.map(start).join("")}</div></div>`;
  return `<div class="fleet-groups" data-board>${envs.map((env) => {
    const us = units.filter((u) => u.env === env);
    const repo = repoShort(state.envs?.find((e) => e.name === env)?.repo || us[0]?.sb.repo || "");
    return `<section class="fleet-group" aria-label="${esc(env)}">
      <div class="af-fleet__pool-head">${envIcon(env)}<span class="af-fleet__pool-name">${esc(env)}</span>${meta([["sandboxes", us.length, "strong"], ["repo", repo]])}</div>
      <div class="af-bays">${us.map(bay).join("")}${start(env)}</div></section>`;
  }).join("")}</div>`;
}

/* ---- Columns: one column per state, attention on the left (FleetColumns) */
function mini(u) {
  return `<article class="af-mini af-mini--${u.role}${sel(u) ? " is-selected" : ""}" data-id="${esc(u.id)}" data-tile aria-label="${esc(aria(u))}" title="${esc(tip(u))}">
    <div class="af-mini__top"><b>${esc(u.id)}</b><span>${esc(u.env)}</span></div>
    <p class="af-mini__title">${esc(u.work)}</p>${u.stage ? pips(u) : ""}
    <div class="af-mini__top fleet-mini-foot"><span>${esc(u.owner ? firstName(u.owner) : u.actor || "hangar")}${u.harness ? ` · ${esc(u.harness)}` : ""}</span><span>${dur(u.age)}</span></div>
    ${ROLE_RANK[u.role] <= 2 ? `${note(u)}<div>${oneAction(u, u.role === "approval")}</div>` : ""}
  </article>`;
}
function columnsHtml(units) {
  const cols = [
    ["Needs you", "approval", (u) => u.role === "approval", "Nothing is waiting on you."],
    ["Failed or denied", "failed", (u) => u.role === "failed" || u.role === "denied", "Nothing failed."],
    ["Running", "running", (u) => u.role === "running", "Nothing is running.", true],
    ["Starting", "queued", (u) => u.role === "queued", "Nothing is starting."],
    ["Paused or spun down", "off", (u) => u.role === "off", "Nothing is paused."],
  ];
  return `<div class="af-statecols" data-board>${cols.map(([name, role, pick, empty, wide]) => {
    let us = units.filter(pick);
    if (wide) us = us.slice().sort((a, b) => a.env.localeCompare(b.env) || a.sb.created_at - b.sb.created_at || a.id.localeCompare(b.id));
    return `<section class="af-statecol${wide ? " af-statecol--wide" : ""}" aria-label="${esc(name)}">
      <div class="af-statecol__head">${status(role, name, { lit: role === "approval" || role === "failed" })}<span class="af-statecol__count">${us.length}</span></div>
      <div class="af-statecol__list">${us.length ? us.map(mini).join("") : `<p class="af-statecol__empty">${esc(empty)}</p>`}</div></section>`;
  }).join("")}</div>`;
}

/* ---- Owners: people down, environments across (FleetOwners) */
function ownersHtml(units) {
  if (!units.length) return emptyView("Nobody has a sandbox right now.", "Start one from an environment; it lands in your row.");
  const envs = envNames(units);
  const caps = state.overview?.person_caps || {};
  const people = [...new Set(units.map((u) => u.owner || ""))].sort((a, b) =>
    (a === "") - (b === "") || personName(a).localeCompare(personName(b)));
  const spend = {};
  for (const r of fleet.spend || []) {
    const s = (state.sessions || []).find((x) => x.name === r.key);
    const j = s?.job_id ? (state.jobs || []).find((x) => x.id === s.job_id) : null;
    const who = j ? personOf(j.requester, j.person) : s ? personOf(s.owner, s.person) : null;
    if (who) spend[who] = (spend[who] || 0) + (r.cost_usd || 0);
  }
  const cols = `--af-owners-cols:${envs.length}`;
  const head = `<div class="af-owners__row" style="${cols}"><div class="af-owners__h">Person<small>${caps.sandboxes ? `cap ${caps.sandboxes} each` : ""}</small></div>${envs.map((env) =>
    `<div class="af-owners__h">${envIcon(env)}${esc(env)}<small>${units.filter((u) => u.env === env).length}</small></div>`).join("")}</div>`;
  const rows = people.map((p) => {
    const mine = units.filter((u) => (u.owner || "") === p);
    const direct = mine.filter((u) => u.kind === "sandbox" && u.live).length;
    const sessions = (state.sessions || []).filter((s) => !s.job_id && ["starting", "idle", "running"].includes(s.state) && personOf(s.owner, s.person) === p).length;
    const need = mine.filter((u) => u.role === "approval").length;
    const who = `<span class="af-who">${p ? personAvatar(p) : avatar("agent", "", { size: 28, name: "hangar" })}<span class="af-who__name">${esc(p ? personName(p) : "hangar")}</span></span>`;
    return `<div class="af-owners__row" style="${cols}"><div class="af-owners__who">${who}
        ${p ? `${capHtml(direct, caps.sandboxes, true, "sandboxes")}${sessions ? capHtml(sessions, caps.sessions, true, "sessions") : ""}` : `<span class="af-cap af-cap--sm">${mine.length} started by hangar</span>`}
        ${meta([fleet.spend && p ? ["today", `$${(spend[p] || 0).toFixed(2)}`] : null, need ? ["needs you", need, "approval"] : null])}</div>
      ${envs.map((env) => {
        const here = mine.filter((u) => u.env === env);
        return `<div class="af-owners__cell">${here.length ? `<div class="af-fleet__board">${here.map(stand).join("")}</div>` : `<span class="af-owners__none">—</span>`}</div>`;
      }).join("")}</div>`;
  }).join("");
  return `<div class="af-panel af-owners" data-scroll="owners"><div class="af-owners__rows" data-board>${head}${rows}</div></div>`;
}

/* ---- the detail panel: everything about the selected sandbox, and every action. Airframe's Drawer parts (head, body,
   foot) in both of its places: a right drawer (airframe.drawer, no scrim) in Bays, Columns and Owners and on narrow
   screens, a docked panel beside the Board on wide ones. The decision sits in the foot: inspect, refuse, accept. */
function detailHtml(u) {
  if (!u) return `<div class="af-drawer__body">${emptyState({ compact: true, icon: "agent", title: "No sandbox selected.",
    text: "Arrow keys move across the board; Enter opens one." })}</div>`;
  const sb = u.sb, running = sb.state === "running", paused = sb.state === "paused", live = running || paused;
  const act = (a, icon, label, kind = "secondary", extra = {}) => btn(label, { kind, sm: true, icon, attrs: { "data-act": a, ...extra } });
  const runs = (sb.info?.runs || []).slice(-4).reverse();
  const branch = sb.info?.work_branch || sb.branch;
  const go = u.job ? `job:${u.job.id}` : u.ses ? `session:${u.ses.id}` : null;
  // a hold the console can decide: a job waiting to start, or a tool call a running turn asked for
  const decide = u.ask ? `${go ? btn(u.job ? "Open job" : "Open session", { kind: "ghost", sm: true, attrs: { "data-go": go } }) : ""}
      ${btn("Deny", { kind: "danger", sm: true, attrs: { "data-act": "deny" } })}${btn(u.ask.kind === "job" ? "Approve job" : "Allow", { kind: "primary", sm: true, attrs: { "data-act": "approve" } })}` : "";
  const answer = u.label === "Needs input" && go ? btn("Answer the agent", { kind: "primary", sm: true, icon: "session", attrs: { "data-go": go } }) : "";
  return `<header class="af-drawer__head">${agentAvatar(u.harness)}
      <h2 class="af-drawer__title"><span class="af-id">${esc(u.id)}</span> ${esc(u.work)}</h2>
      ${btn("", { kind: "ghost", sm: true, icon: "close", attrs: { "data-fleet-close": true, "aria-label": "Close" } })}</header>
    <div class="af-drawer__body fd" data-id="${esc(u.id)}" data-role="${u.role}">
      <div class="fd-state">${status(u.role, u.label, { lit: true })}${u.reason && u.ask?.kind === "tool" ? `<p class="fd-cmd">${esc(u.reason[1])}</p>` : note(u)}</div>
      ${track(u)}
      <dl class="af-kv fd-kv">
        <dt>Name</dt><dd class="af-mono">${esc(u.name)}</dd>
        <dt>${u.kind === "job" ? "Job" : u.kind === "session" ? "Session" : "Started by"}</dt><dd>${u.job
          ? `<a href="#jobs?job=${esc(u.job.id)}" data-go="job:${esc(u.job.id)}" class="af-mono">${esc(u.job.id)}</a>`
          : u.ses ? `<a href="#sessions" data-go="session:${esc(u.ses.id)}" class="af-mono">${esc(u.ses.name)}</a>` : esc(sb.created_by || "")}
          <span class="sub-val">${esc(ownerOf(u))}</span></dd>
        <dt>Where</dt><dd>${meta([["env", u.env, "strong"], ["provider", u.provider], ["harness", u.harness], ["model", u.model]])}</dd>
        <dt>Age</dt><dd>${dur(u.age)}</dd>
        ${live && u.left != null ? `<dt>Lease</dt><dd class="fd-lease"><span>${esc(left(u.left))}</span><span class="af-bay__left" aria-hidden="true"><i style="width:${Math.round(100 * u.leftFrac)}%"></i></span></dd>` : ""}
        ${sb.repo ? `<dt>Repo</dt><dd class="af-mono">${esc(repoShort(sb.repo))}</dd>` : ""}
        ${branch ? `<dt>Branch</dt><dd><code>${esc(branch)}</code></dd>` : ""}
        ${sb.credentials?.length ? `<dt>Credentials</dt><dd class="af-mono">${esc(sb.credentials.join(", "))}</dd>` : ""}
        ${sb.parent ? `<dt>Forked from</dt><dd class="af-mono">${esc(sb.parent)}</dd>` : ""}
        ${sb.info?.warm ? `<dt>Start</dt><dd>warm</dd>` : ""}
      </dl>
      ${sb.error && u.reason?.[0] !== "Error" ? `<p class="bay-err">${meta([["error", sb.error, "failed"]])}</p>` : ""}
      ${u.task ? `<details class="fd-task"><summary>Task</summary><p>${esc(u.task)}</p></details>` : ""}
      ${sb.state !== "stopped" ? `<div class="fd-actions">
        ${running ? act("term", "terminal", "Terminal") + act("agent", "agent", "Run agent") + act("exec", "play", "Run command")
          + act("fork", "fork", "Fork") + act("snapshot", "snapshot", "Snapshot") + act("pause", "pause", "Pause") : ""}
        ${paused ? act("resume", "play", "Resume") : ""}
        ${live ? act("extend", "duration", "+1 hour", "secondary", { title: "Add one hour" }) : ""}
        ${act("destroy", "close", "Destroy", "danger")}</div>` : ""}
      ${runs.length && running ? `<div class="fd-runs"><h4>Agent runs</h4>${runs.map((r) =>
        `<button type="button" class="fd-run" data-act="run" data-run="${esc(r.id)}" title="${esc(r.prompt)}">${ic("terminal")}<span class="af-mono">${esc(r.harness)}</span><span class="fd-run__prompt">${esc(r.prompt || "")}</span><span class="hint">${ago(r.started_at)}</span></button>`).join("")}</div>` : ""}
    </div>
    ${decide || answer ? `<footer class="af-drawer__foot" data-id="${esc(u.id)}">${decide || answer}</footer>` : ""}`;
}

/* ------------------------------------------------------------------ selection, actions, keys */
// The panel is docked beside the Board on a wide screen and a right drawer (Airframe Drawer, no scrim) everywhere
// else: in Bays, Columns and Owners, and on narrow screens. JS decides, so the drawer's open, close, Escape and focus
// return are airframe.drawer's; the terminal drawer below shortens it through --drawer-pad.
const DOCKED = window.matchMedia("(min-width: 1081px)");
const isDrawer = () => !(fleet.view === "board" && DOCKED.matches);
function placeDetail() {
  const el = $("#fleet-detail"), drawer = isDrawer();
  const wasOpen = el.classList.contains("af-drawer") && airframe.drawer.isOpen(el);
  if (wasOpen && !(drawer && fleet.selected)) airframe.drawer.close(el);
  el.classList.toggle("af-drawer", drawer);
  el.classList.toggle("af-drawer--right", drawer);
  el.classList.toggle("is-docked", !drawer);
  if (drawer && fleet.selected && !wasOpen) airframe.drawer.open(el, { scrim: false });
}
DOCKED.addEventListener("change", () => { if (state.view === "fleet") placeDetail(); });
// closed from outside (Escape, or airframe.drawer.close): the selection goes with it, and focus returns to the tile
$("#fleet-detail").addEventListener("af:close", () => { if (fleet.selected) { fleet.selected = null; renderFleet(); } });
function selectUnit(id) {
  fleet.selected = id && fleet.byId.has(id) ? id : null;
  renderFleet();
}
function closeDetail() {
  const id = fleet.selected;
  fleet.selected = null;
  renderFleet();
  $(`#bays [data-tile][data-id="${CSS.escape(id || "")}"]`)?.focus({ preventScroll: true });
}

function goTo(target) {
  const [kind, id] = target.split(/:(.+)/);
  if (kind === "job") { state.selectedJob = id; location.hash = `#jobs?job=${id}`; return; }
  const s = (state.sessions || []).find((x) => x.id === id);
  state.selectedSession = id;
  if (s?.job_id) $("#ses-jobs").checked = true;
  location.hash = "#sessions";
}

async function decide(u, allow, btn) {
  const a = u.ask;
  await busy(btn, async () => {
    if (a.kind === "job") {
      if (!allow && !(await confirmAction(`Reject ${a.job}?`, "The job won't run; its requester is told. Nothing has started yet.", "Reject", { cancel: "Keep waiting" }))) return;
      await api("POST", `/api/jobs/${a.job}/${allow ? "approve" : "reject"}`);
      toast(`${allow ? "Approved" : "Rejected"} ${a.job}`);
    } else {
      const path = a.job ? `/api/jobs/${a.job}/approvals/${a.approval}` : `/api/sessions/${a.session}/approvals/${a.approval}`;
      await api("POST", path, { decision: allow ? "allow" : "deny" });
      toast(`${allow ? "Allowed" : "Denied"}: ${u.reason?.[1] || a.approval}`);
    }
    await loadFleet();
  });
}

$("#view-fleet").addEventListener("click", async (e) => {
  if (e.target.closest("[data-fleet-close]")) return closeDetail();
  const go = e.target.closest("[data-go]");
  if (go) { e.preventDefault(); return goTo(go.dataset.go); }
  const pick = e.target.closest("[data-select]");
  if (pick) return selectUnit(pick.dataset.select);
  const b = e.target.closest("button[data-act]");
  if (!b) {
    const tile = e.target.closest("[data-tile]");
    if (tile && !e.target.closest("a, button:not([data-tile])")) selectUnit(tile.dataset.id === fleet.selected && isDrawer() ? null : tile.dataset.id);
    return;
  }
  const act = b.dataset.act;
  if (act === "new") { openNewForm(b.dataset.env); $("#new-form").scrollIntoView({ behavior: "smooth", block: "start" }); return; }
  const id = b.closest("[data-id]")?.dataset.id;
  const u = fleet.byId.get(id), sb = u?.sb;
  if (!sb) return;
  if (act === "approve" || act === "deny") return decide(u, act === "approve", b);
  if (act === "term") return openTerminal(sb);
  if (act === "run") return openTerminal(sb, b.dataset.run);
  if (act === "agent") return agentDialog(sb);
  if (act === "exec") return execDialog(sb);
  if (act === "fork") return forkDialog(sb);
  if (act === "destroy" && !(await confirmAction(`Destroy ${sb.name}?`, `Its filesystem is deleted unless you snapshot it first.${u.job || u.ses
    ? " The branch and the conversation are kept." : ""}`, "Destroy"))) return;
  await busy(b, async () => {
    if (act === "destroy") {
      await api("DELETE", `/api/sandboxes/${id}`); toast(`Destroyed ${sb.name}`); closeTerminalsFor(id);
    } else if (act === "snapshot") {
      const s = await api("POST", `/api/sandboxes/${id}/snapshot`, { kind: "auto" }); toast(`Snapshot ${s.id} taken (${s.kind})`);
    } else if (act === "pause") { await api("POST", `/api/sandboxes/${id}/pause`); toast(`Paused ${sb.name}`); }
    else if (act === "resume") { await api("POST", `/api/sandboxes/${id}/resume`); toast(`Resumed ${sb.name}`); }
    else if (act === "extend") { await api("POST", `/api/sandboxes/${id}/extend?minutes=60`); toast(`${sb.name} extended by 1 hour`); }
    await loadFleet();
  });
});

// Arrow keys move within a board (by position, so they work across pools, cells and columns); Enter and Space open
// the tile; Escape closes the drawer.
$("#view-fleet").addEventListener("keydown", (e) => {
  if (e.key === "Escape" && fleet.selected && isDrawer()) { e.preventDefault(); e.stopPropagation(); return closeDetail(); }
  const tile = e.target.closest?.("[data-tile]");
  if (!tile || e.target !== tile) return;
  if ((e.key === "Enter" || e.key === " ") && tile.tagName !== "BUTTON") { e.preventDefault(); return selectUnit(tile.dataset.id); }
  if (!["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "Home", "End"].includes(e.key)) return;
  const board = tile.closest("[data-board]");
  const tiles = [...board.querySelectorAll("[data-tile]")];
  const i = tiles.indexOf(tile), r = tile.getBoundingClientRect();
  let next = null;
  if (e.key === "ArrowRight") next = tiles[i + 1];
  else if (e.key === "ArrowLeft") next = tiles[i - 1];
  else if (e.key === "Home") next = tiles[0];
  else if (e.key === "End") next = tiles.at(-1);
  else {
    const down = e.key === "ArrowDown";
    const box = (t) => t.getBoundingClientRect();
    const cand = tiles.filter((t) => (down ? box(t).top > r.top + 4 : box(t).top < r.top - 4));
    if (cand.length) {
      const row = down ? Math.min(...cand.map((t) => box(t).top)) : Math.max(...cand.map((t) => box(t).top));
      const line = cand.filter((t) => Math.abs(box(t).top - row) < 4);
      next = line.reduce((b, t) => (Math.abs(box(t).left - r.left) < Math.abs(box(b).left - r.left) ? t : b));
    }
  }
  if (!next) return;
  e.preventDefault();
  tiles.forEach((t) => (t.tabIndex = -1));
  next.tabIndex = 0;
  next.focus();
  next.scrollIntoView({ block: "nearest", inline: "nearest" });
});
