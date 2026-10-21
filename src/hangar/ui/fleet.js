/* hangar console: the Fleet and Needs-you pages, as Airframe theme-v12's FleetConsole (the "Decision deck").
   One model, read from GET /api/fleet and kept current by GET /api/fleet/stream (Server-Sent Events, with 4 s polling
   when the stream is not available), drawn two ways over one selection: List (AgentRow, the default) and Board
   (AgentCard). The DecisionDeck is the whole Needs-you page and a collapsible band atop the Fleet: one component, two
   placements. An agent opens in place into its console: the decision first, then what it is doing now, then the
   ActivityTimeline. Every sandbox action lives in that console, so both views get the same ones.
   A classic script loaded after app.js, whose helpers it uses ($, $$, api, esc, ic, status, meta, btn, avatar,
   agentAvatar, initialsOf, duration, plural, busy, toast, confirmAction, emptyState, navCount, state and the dialogs
   and terminals), with Airframe's expand, deck, roving and duration (window.airframe). */
"use strict";

const FLEET_VIEWS = ["list", "board"];
const FLEET_STORE = "hangar.fleetView";
const BAND_STORE = "hangar.deckBand";
const STAGES = ["plan", "edit", "test", "pr"];
const STAGE_WORD = { plan: "Plan", edit: "Edit", test: "Test", pr: "PR" };
// Attention first everywhere: what needs a person, then what failed, then what policy stopped, then the quiet states.
// The headline, each environment's order and the deck all follow this one list (the server sorts units by it too).
const ATTENTION = ["approval", "failed", "denied", "running", "queued", "off"];
const ROLE_RANK = Object.fromEntries(ATTENTION.map((r, i) => [r, i]));
// what the deck shows: decisions (a person is needed now), then what stopped (acknowledge, retry)
const DECIDE = new Set(["approval", "question"]);
const STOPPED = new Set(["failed", "denied", "alert", "abandon"]);
const POLL_MS = 4000;
const VERDICT_MS = 3200;  // how long a verdict line stays readable before the card leaves
const NUMBERS = ["None", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine", "Ten"];

const fleet = {
  view: "list", owner: null, data: null, byId: new Map(), open: null, opening: false,
  stream: null, streamKey: null, streamRetryAt: 0, live: false, loading: null,
  verdicts: new Map(),   // inbox item id -> {ok, text}: the optimistic verdict line, until the card leaves
  done: new Set(),       // inbox item ids decided here: kept out of the deck until the server stops listing them
  overrides: new Map(),  // unit id -> {from, until, patch}: the row flips at once when a decision lands
  deny: new Set(), invalid: new Set(), picks: new Map(), drafts: new Map(),
  deckTop: null, band: true, nav: null, painted: {}, attention: null, tl: null,
};
try { fleet.band = localStorage.getItem(BAND_STORE) !== "closed"; } catch { /* a private window */ }

/* ------------------------------------------------------------------ small words and numbers */
const clamp01 = (x) => Math.max(0, Math.min(1, Number(x) || 0));
const now = () => Date.now() / 1000;
// recency, as the Heartbeat reads: "now", "12 s", "41 min", "2 h"
const since = (ts) => { const s = Math.max(0, now() - (ts || now())); return s < 5 ? "now" : duration(s, { fine: false }); };
const money = (n) => (n == null ? "" : `$${Number(n).toFixed(2)}`);
const wall = (ts) => new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
const repoShort = (r) => (r || "").replace(/^https?:\/\/(www\.)?github\.com\//, "").replace(/\.git$/, "");
const number = (n) => NUMBERS[n] || String(n);
const tokensOf = (u) => (u.usage ? (u.usage.input_tokens || 0) + (u.usage.output_tokens || 0) + (u.usage.cache_write_tokens || 0) : 0);
const ownerName = (id) => (id ? fleet.data?.owners?.find((o) => o.id === id)?.name || id : "hangar");
const firstName = (id) => ownerName(id).split(" ")[0];
const personAvatar = (id, size = 22) => (id ? avatar("person", initialsOf(ownerName(id)), { size, name: ownerName(id) })
  : avatar("agent", "", { size, name: "hangar" }));
const meName = () => (state.me?.person ? firstName(state.me.person) : "you");
// the role's modifier on the row, the rail, the ring and the capsule: only the three that need someone carry one
const mod = (u) => (u.role === "approval" ? "approval" : u.role === "failed" ? "failed" : u.role === "denied" ? "denied" : "");
const markOnly = (role) => `<span class="af-status af-status--${role}" aria-hidden="true"><i class="af-status__mark"></i></span>`;

/* ------------------------------------------------------------------ the model */
// a unit as drawn: the server's, with an optimistic decision on top while the server catches up
function unitView(u) {
  const o = fleet.overrides.get(u.id);
  if (!o) return u;
  if (u.role !== o.from || Date.now() > o.until) { fleet.overrides.delete(u.id); return u; }
  return { ...u, ...o.patch };
}
const units = () => (fleet.data?.units || []).map(unitView);
function index() {
  fleet.byId = new Map((fleet.data?.units || []).map((u) => [u.id, u]));
  tails.see(fleet.byId.keys());  // tail numbers widen together when two agents share a code (tails.js)
  if (fleet.open) fleet.open = tails.resolve(fleet.open, fleet.byId.keys());  // #fleet?open=HGR-7Q2K works too
  if (fleet.open && !fleet.byId.has(fleet.open)) fleet.open = null;
  const listed = new Set((fleet.data?.inbox?.items || []).map((i) => i.id));
  for (const id of fleet.done) if (!listed.has(id) && !fleet.verdicts.has(id)) fleet.done.delete(id);
}
const unitOfItem = (it) => (fleet.data?.units || []).find((u) => (u.inbox || []).includes(it.id))
  || (it.job ? (fleet.data?.units || []).find((u) => u.job === it.job) : null)
  || (it.session ? (fleet.data?.units || []).find((u) => u.session?.name === it.session) : null) || null;
// what waits on a person: decisions by what expires first, then what stopped, newest first; decided ones stay while their
// verdict shows. The deck stacks the decisions only and lists what stopped under them; with no decision left, what
// stopped is the stack.
function queue() {
  const items = (fleet.data?.inbox?.items || []).filter((i) => (DECIDE.has(i.kind) || STOPPED.has(i.kind))
    && (!fleet.done.has(i.id) || fleet.verdicts.has(i.id)));
  const exp = (i) => i.expires_at || unitOfItem(i)?.ask_expires_at || Infinity;
  return items.sort((a, b) => (DECIDE.has(b.kind) - DECIDE.has(a.kind))
    || (DECIDE.has(a.kind) ? exp(a) - exp(b) || (a.since || 0) - (b.since || 0) : (b.since || 0) - (a.since || 0)));
}

function deckSets() {
  const q = queue(), decide = q.filter((i) => DECIDE.has(i.kind)), stopped = q.filter((i) => !DECIDE.has(i.kind));
  let stack = decide.length ? decide : stopped;
  const top = stack.findIndex((i) => i.id === fleet.deckTop);
  if (top > 0) stack = stack.slice(top).concat(stack.slice(0, top));
  return { stack, stopped: decide.length ? stopped : [] };
}

/* ------------------------------------------------------------------ loading: the stream, else polling */
function fleetHash() {
  const [v, q] = (location.hash || "").slice(1).split("?");
  const p = new URLSearchParams(q || "");
  let view = p.get("view");
  if (FLEET_VIEWS.includes(view)) { try { localStorage.setItem(FLEET_STORE, view); } catch {} }
  else { try { view = localStorage.getItem(FLEET_STORE); } catch { view = null; } }
  if (v === "fleet") fleet.owner = p.get("owner") || null;
  fleet.view = FLEET_VIEWS.includes(view) ? view : "list";
  if (p.get("open")) fleet.open = p.get("open");
}
const fleetQuery = () => {
  const q = new URLSearchParams();
  if ($("#show-stopped")?.checked) q.set("include_stopped", "true");
  if (fleet.owner && state.view === "fleet") q.set("owner", fleet.owner);
  return q.toString();
};

function applySnapshot(d) {
  fleet.data = d;
  state.inbox = d.inbox;
  index();
  draw();
}
async function fetchFleet() {
  if (fleet.loading) return fleet.loading;
  const q = fleetQuery();
  fleet.loading = api("GET", `/api/fleet${q ? `?${q}` : ""}`).then(applySnapshot).finally(() => { fleet.loading = null; });
  return fleet.loading;
}
function closeStream() {
  if (fleet.stream) fleet.stream.close();
  fleet.stream = null; fleet.streamKey = null; fleet.live = false;
}
// The stream: a one-use ticket (POST /api/fleet/tickets), then the same read as SSE. A stream that errors or closes
// falls back to polling every 4 s and tries again with a fresh ticket half a minute later.
async function openStream() {
  const key = fleetQuery();
  if (fleet.stream && fleet.streamKey === key) return;
  closeStream();
  if (Date.now() < fleet.streamRetryAt || typeof EventSource === "undefined") return;
  fleet.streamKey = key;
  let es;
  try {
    const { ticket } = await api("POST", "/api/fleet/tickets");
    if (fleet.streamKey !== key) return;
    es = new EventSource(`/api/fleet/stream?ticket=${encodeURIComponent(ticket)}${key ? `&${key}` : ""}`);
  } catch { fleet.streamKey = null; fleet.streamRetryAt = Date.now() + 30000; return; }
  fleet.stream = es;
  const on = (name, fn) => es.addEventListener(name, (e) => { try { fn(JSON.parse(e.data)); } catch { /* a torn frame: the next one carries the whole unit */ } });
  on("snapshot", (d) => { fleet.live = true; applySnapshot(d); });
  on("agent", (u) => upsertUnit(u));
  on("gone", ({ id }) => {
    if (!fleet.data) return;
    fleet.data.units = fleet.data.units.filter((u) => u.id !== id);
    index(); draw();
  });
  on("inbox", (ib) => { if (!fleet.data) return; fleet.data.inbox = ib; state.inbox = ib; index(); draw(); });
  on("summary", (s) => { if (!fleet.data) return; Object.assign(fleet.data, s); draw(); });
  on("close", () => { closeStream(); fleet.streamRetryAt = Date.now() + 30000; });
  es.onerror = () => { if (fleet.stream === es) { closeStream(); fleet.streamRetryAt = Date.now() + 30000; } };
}
// one agent changed: patch its row, card and console in place when only the live parts moved (the now-line, the
// heartbeat, the rail, the spend), so nothing steals focus; repaint when its shape changed (a status, a stage)
function upsertUnit(u) {
  if (!fleet.data) return;
  const i = fleet.data.units.findIndex((x) => x.id === u.id);
  const before = i >= 0 ? fleet.data.units[i] : null;
  if (i >= 0) fleet.data.units[i] = u; else fleet.data.units.push(u);
  index();
  if (before && shape(unitView(before)) === shape(unitView(u)) && patchUnit(unitView(u))) {
    if (fleet.open === u.id) followOpen();
    return;
  }
  draw();
}
const shape = (u) => JSON.stringify([u.role, u.label, u.stage, u.work, u.owner, u.environment, u.state, u.reason, u.ask,
  u.question, u.question_options, u.inbox, u.stage_pct == null, u.harness, u.model]);

async function loadFleet(quiet = false) {
  fleetHash();
  if (fleet.data) draw();
  openStream();
  if (fleet.live) return;
  try { await fetchFleet(); } catch (e) { if (!quiet && e.message !== "unauthorized") toast(e.message, true); }
}
async function loadNeeds(quiet = false) {
  if (state.view === "needs") openStream();
  if (fleet.live) { draw(); return; }
  try { await fetchFleet(); } catch (e) { if (!quiet && e.message !== "unauthorized") toast(e.message, true); }
}
window.addEventListener("hashchange", () => { if (state.view !== "fleet" && state.view !== "needs") closeStream(); });
$("#show-stopped").addEventListener("change", () => { closeStream(); loadFleet(); });

/* ------------------------------------------------------------------ the parts: rail, ring, capsule, spark, heartbeat */
// how far along the agent is: solid only from what it reported (stage_pct); otherwise the server's estimate, dotted
function progress(u) {
  const i = STAGES.indexOf(u.stage);
  if (i < 0) return null;
  if (u.stage_pct != null) return { i, f: clamp01(u.stage_pct / 100), est: false };
  return { i, f: clamp01((u.stage_estimate?.pct || 0) / 100), est: true, basis: u.stage_estimate?.basis };
}
const railP = (pr) => Math.min(100, (pr.i + pr.f) * 33.333);
function railLabel(u, pr) {
  const pct = Math.round(pr.f * 100);
  return `${STAGE_WORD[u.stage]} stage, ${pr.est ? `about ${pct}% (estimate${pr.basis ? `: ${pr.basis}` : ""})` : `${pct}%`}`;
}
function railHtml(u, labeled = false) {
  const pr = progress(u), lab = labeled ? STAGES.map((s) => `<span class="af-rail__lab">${STAGE_WORD[s]}</span>`).join("") : "";
  if (!pr) return `<span class="af-rail af-rail--none${labeled ? " af-rail--labeled" : ""}" data-rail="${esc(u.id)}" aria-hidden="true"><i class="af-rail__track"></i>${"<b class=\"af-rail__stop\"></b>".repeat(4)}${lab}</span>`;
  const p = railP(pr), m = mod(u);
  return `<span class="af-rail${m ? ` af-rail--${m}` : ""}${labeled ? " af-rail--labeled" : ""}${pr.est ? " af-rail--est" : ""}" data-rail="${esc(u.id)}" role="img"
    aria-label="${esc(railLabel(u, pr))}" title="${esc(railLabel(u, pr))}" style="--p:${p.toFixed(1)}%;--s:${(pr.i * 33.333).toFixed(1)}%"><i class="af-rail__track"></i><i class="af-rail__done"></i>${
    [0, 1, 2, 3].map((k) => `<b class="af-rail__stop${k * 33.333 <= p ? " is-on" : ""}"></b>`).join("")}${lab}<i class="af-rail__head"></i></span>`;
}
function ringHtml(u) {
  const r = 12, C = 2 * Math.PI * r, seg = C / 4, gap = 3.2, len = seg - gap, pr = progress(u), m = mod(u);
  if (!pr) return `<svg class="af-ring af-ring--none" viewBox="0 0 30 30" aria-hidden="true"><circle class="af-ring__track" cx="15" cy="15" r="${r}"/></svg>`;
  const arcs = STAGES.map((s, k) => {
    const off = -k * seg + gap / 2, fill = k < pr.i ? len : k === pr.i ? len * pr.f : 0;
    const est = k === pr.i && pr.est ? " af-ring__est" : "";
    return `<circle class="af-ring__track" cx="15" cy="15" r="${r}" stroke-dasharray="${len.toFixed(2)} ${(C - len).toFixed(2)}" stroke-dashoffset="${off.toFixed(2)}" transform="rotate(-90 15 15)"/>${fill
      ? `<circle class="af-ring__fill${est}" cx="15" cy="15" r="${r}" stroke-dasharray="${fill.toFixed(2)} ${(C - fill).toFixed(2)}" stroke-dashoffset="${off.toFixed(2)}" transform="rotate(-90 15 15)"/>` : ""}`;
  }).join("");
  return `<svg class="af-ring${m ? ` af-ring--${m}` : ""}${pr.est ? " af-ring--est" : ""}" viewBox="0 0 30 30" role="img" aria-label="${esc(railLabel(u, pr))}">${arcs}<text x="15" y="18">${u.stage === "pr" ? "PR" : STAGE_WORD[u.stage][0]}</text></svg>`;
}
function capsuleHtml(u) {
  const pr = progress(u), m = mod(u);
  if (!pr) return `<span class="af-capsule af-capsule--none" aria-hidden="true">${"<i class=\"af-capsule__seg\"></i>".repeat(4)}</span>`;
  return `<span class="af-capsule${m ? ` af-capsule--${m}` : ""}${pr.est ? " af-capsule--est" : ""}" role="img" aria-label="${esc(railLabel(u, pr))}">${STAGES.map((s, k) =>
    `<i class="af-capsule__seg${k < pr.i ? " is-done" : k === pr.i ? " is-now" : ""}" style="--p:${k === pr.i ? Math.round(pr.f * 100) : 0}%"></i>`).join("")}</span>`;
}
function sparkHtml(u, w = 84, h = 20) {
  const vals = (u.tokens_1h && u.tokens_1h.length ? u.tokens_1h : new Array(12).fill(0)).slice(-12);
  const n = vals.length, m = Math.max(1, ...vals);
  const y = (v) => (h - 1 - (v / m) * (h - 3)).toFixed(1);
  const pts = vals.map((v, i) => `${(i / Math.max(1, n - 1) * (w - 2) + 1).toFixed(1)},${y(v)}`).join(" ");
  const total = vals.reduce((a, b) => a + b, 0);
  return `<svg class="af-spark af-spark--mini" data-spark="${esc(u.id)}" width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" role="img" aria-label="${esc(`${kTok(total)} tokens in the last hour`)}"><polyline class="af-spark__line" points="${pts}"/><circle class="af-spark__end" cx="${w - 1}" cy="${y(vals[n - 1])}" r="1.8"/></svg>`;
}
// recency beside the now-line: it breathes while the agent runs, waits in orange on a person, is still otherwise
const waitingSince = (u) => (u.role === "approval" ? (fleet.data?.inbox?.items || []).find((i) => (u.inbox || []).includes(i.id))?.since || u.last_activity_at : u.last_activity_at);
const hbCls = (u) => `af-hb${u.role === "running" && u.live ? " af-hb--live" : ""}${u.role === "approval" ? " af-hb--wait" : ""}`;
const hbTitle = (u) => (u.role === "approval" ? `Waiting on a person for ${since(waitingSince(u))}` : `Last activity ${since(u.last_activity_at)}${since(u.last_activity_at) === "now" ? "" : " ago"}`);
const hbHtml = (u) => `<span class="${hbCls(u)}" data-hb="${esc(u.id)}" title="${esc(hbTitle(u))}"><i class="af-hb__dot"></i>${since(waitingSince(u))}</span>`;
// what the agent is doing now, in its words; the server's now-line when it has one
function nowLine(u) {
  const reason = u.reason?.text || "";
  if (u.role === "approval") {
    if (u.label === "Needs input") return "waiting on you: a question";
    const tool = /^([^:]+):\s*(.+?)(?:\s+\([^)]*\))?$/.exec(reason), cmd = tool ? tool[2] : reason;
    const push = /\bgit\s+push\b.*\b(main|master)\b/.exec(cmd || "");
    return `waiting on you: ${push ? `push to ${push[1]}` : (cmd || "an approval").slice(0, 70)}`;
  }
  if (u.role === "failed") return `stopped: ${reason || "failed"}`;
  if (u.role === "denied") return u.now || `blocked by policy, carrying on without it`;
  if (u.now) return u.now;
  if (u.role === "queued") return `waiting for a slot in ${u.environment}`;
  if (u.state === "paused") return "paused, nothing running";
  if (u.role === "off") return u.state === "stopped" ? "spun down" : word(u.state);
  if (u.session && u.session.state === "idle") return "idle, waiting for a message";
  return u.kind === "sandbox" ? "up, nobody driving it" : "working";
}
// the last two stages with their times ("Test 2 min, PR 43 min"); the open one counts up
function stagesHtml(u) {
  const st = (u.stages || []).filter((s) => STAGES.includes(s.stage));
  if (!st.length) {
    if (u.stage) return `<span class="af-stages" data-stages="${esc(u.id)}"><span class="is-now">${STAGE_WORD[u.stage]}<b>now</b></span></span>`;
    return `<span class="af-stages af-stages--none" data-stages="${esc(u.id)}">${u.role === "off" ? "not running" : "not started"}</span>`;
  }
  return `<span class="af-stages" data-stages="${esc(u.id)}">${st.slice(-2).map((s) => `<span class="${s.ended_at ? "" : "is-now"}">${STAGE_WORD[s.stage]}<b>${
    duration((s.ended_at || now()) - s.started_at, { fine: false })}</b></span>`).join("")}</span>`;
}
const stWord = (u) => status(u.role, u.label);

/* ------------------------------------------------------------------ List (AgentRow) and Board (AgentCard) */
const unitCls = (u, extra = "") => ["af-agent", extra, mod(u) === "approval" ? "af-agent--lit" : mod(u) ? `af-agent--${mod(u)}` : "",
  u.role === "off" ? "af-agent--off" : ""].filter(Boolean).join(" ");
const ariaOf = (u) => `${tailOf(u.id)}, ${u.label}, ${u.work}`;
const body = (u) => `<div class="af-agent__body"><div>${fleet.open === u.id ? `<div class="af-console" data-console="${esc(u.id)}">${consoleHtml(u)}</div>` : ""}</div></div>`;
function rowHtml(u) {
  return `<article class="${unitCls(u)}" data-id="${esc(u.id)}">
    <button type="button" class="af-agent__line" tabindex="-1" aria-expanded="false" aria-label="${esc(ariaOf(u))}" title="${esc(u.name)}">
      ${markOnly(u.role)}${idHtml(u.id, { codeOnly: true, cls: "af-agent__tail" })}
      <span class="af-agent__ttl"><span class="af-agent__work">${esc(u.work)}</span><span class="af-agent__sub"><span class="af-agent__now" data-now="${esc(u.id)}">${esc(nowLine(u))}</span>${hbHtml(u)}</span></span>
      <span class="af-agent__who">${personAvatar(u.owner)}<span>${esc(u.owner ? firstName(u.owner) : "hangar")}</span></span>
      <span class="af-agent__rig">${agentAvatar(u.harness, 22)}<span class="af-mono">${esc(u.model || u.harness || "")}</span></span>
      ${railHtml(u)}${stagesHtml(u)}${sparkHtml(u)}<span class="af-agent__cost" data-cost="${esc(u.id)}">${money(u.usage?.cost_usd)}</span>${ic("chevron-right").replace('class="af-ic', 'class="af-ic af-agent__chev')}
    </button>${body(u)}
  </article>`;
}
function cardHtml(u) {
  const loud = u.role === "approval" || u.role === "failed" || u.role === "denied";
  return `<article class="${unitCls(u, "af-agent--card")}" data-id="${esc(u.id)}">
    <button type="button" class="af-agent__face" tabindex="-1" aria-expanded="false" aria-label="${esc(ariaOf(u))}" title="${esc(u.name)}">
      <span class="af-agent__hd">${agentAvatar(u.harness, 22)}${idHtml(u.id, { codeOnly: true, cls: "af-agent__tail" })}<span data-ring="${esc(u.id)}">${ringHtml(u)}</span></span>
      <span class="af-agent__work">${esc(u.work)}</span><span class="af-agent__now" data-now="${esc(u.id)}">${esc(nowLine(u))}</span><span data-capsule="${esc(u.id)}">${capsuleHtml(u)}</span>
      <span class="af-agent__ft">${personAvatar(u.owner)}<span>${esc(u.owner ? firstName(u.owner) : "hangar")}</span>${loud ? stWord(u) : hbHtml(u)}</span>
    </button>${body(u)}
  </article>`;
}

/* ------------------------------------------------------------------ the decision: one body, in the deck and the console */
const DRAFT = (where, id, what) => `${where}:${id}:${what}`;
const draftOf = (where, id, what) => fleet.drafts.get(DRAFT(where, id, what)) || "";
// a small button; a kind may carry a second class ("danger af-btn--secondary": the confirmed deny, red words on a hairline)
const sbtn = (label, kind, attrs = {}, icon) => { const [k, ...more] = kind.split(" "); return btn(label, { kind: k, sm: true, icon, attrs, cls: more.join(" ") || undefined }); };
const harnessOf = (it, u) => u?.harness || "the agent";
// what kind of decision an inbox item is
function decisionKind(it) {
  if (it.kind === "question") return "question";
  if (it.kind === "approval") return it.approval ? "tool" : it.request ? "request" : "job";
  return it.kind === "denied" ? "denied" : "failed";
}
const roleOfItem = (it) => (DECIDE.has(it.kind) ? "approval" : it.kind === "denied" ? "denied" : "failed");
const wordOfItem = (it) => (it.kind === "question" ? "Needs input" : it.kind === "approval" ? "Needs approval"
  : it.kind === "denied" ? "Denied" : it.kind === "failed" ? "Failed" : word(it.kind));
// "the agent wants to run Bash: git push origin main" -> {tool, cmd}
function heldCall(it, u) {
  const m = /wants to run ([^:]+):\s*([\s\S]*)$/.exec(it.reason || "");
  if (m) return { tool: m[1].trim(), cmd: m[2].trim() };
  const r = /^([^:]+):\s*([\s\S]*)$/.exec(u?.reason?.text || "");
  return r ? { tool: r[1], cmd: r[2] } : { tool: "", cmd: it.reason || "" };
}
const clean = (text) => String(text || "").replace(/^the agent asks: /, "").replace(/^job waits for approval: /, "");
function pairs(list) {
  const xs = list.filter((x) => x && x[1] !== "" && x[1] != null);
  return xs.length ? `<div class="af-pairs">${xs.map(([k, v, mono]) => `<span><small>${esc(k)}</small>${mono ? `<span class="af-mono">${esc(v)}</span>` : esc(v)}</span>`).join("")}</div>` : "";
}
function reasonHtml(where, it, kind) {
  const id = `${where}-r-${it.id}`, bad = fleet.invalid.has(it.id), back = tailOf(unitOfItem(it)?.id || it.job || it.session) || "the agent";
  const verb = kind === "job" ? "rejecting" : "denying";
  return `<div class="af-reason"><label for="${esc(id)}">Reason for ${verb} (required, goes back to ${esc(back)})</label>
    <div class="af-input af-input--area af-input--sans${bad ? " is-invalid" : ""}"><textarea id="${esc(id)}" rows="2" data-draft="${esc(DRAFT(where, it.id, "reason"))}" aria-invalid="${bad}" placeholder="What the agent should do instead">${esc(draftOf(where, it.id, "reason"))}</textarea></div>
    ${bad ? `<span class="af-field__error">${status("failed", "Give a reason.")}</span>` : ""}
    <div class="af-acts"><div class="af-acts__end">${sbtn("Cancel", "ghost", { "data-dk": "cancel" })}${sbtn(kind === "job" ? "Reject job" : kind === "tool" && /\bgit\s+push\b/.test(heldCall(it, unitOfItem(it)).cmd) ? "Deny push" : "Deny", "danger af-btn--secondary", { "data-dk": "deny-confirm" })}</div></div></div>`;
}
const openLink = (u, it) => {
  const job = u?.job || it.job, ses = u?.session?.id;
  return job ? sbtn("Open job", "ghost", { "data-go": `job:${job}` }) : ses ? sbtn("Open session", "ghost", { "data-go": `session:${ses}` }) : "";
};
function decisionBody(it, u, where) {
  const v = fleet.verdicts.get(it.id);
  if (v) return `<div class="af-verdict-line">${ic(v.ok ? "check" : "info")}<span>${esc(v.text)}</span>${sbtn("Dismiss", "ghost", { "data-dk": "dismiss" })}</div>`;
  const kind = decisionKind(it), h = harnessOf(it, u), denying = fleet.deny.has(it.id);
  const files = u && (u.files_changed || u.commits) ? [u.files_changed ? plural(u.files_changed, "file") : "", u.commits ? plural(u.commits, "commit") : ""].filter(Boolean).join(", ") : "";
  if (kind === "tool") {
    const { tool, cmd } = heldCall(it, u), push = /\bgit\s+push\b/.test(cmd);
    const lead = push ? `${esc(h)} wants to push${/\bmain\b|\bmaster\b/.test(cmd) ? " to main" : ""}` : `${esc(h)} wants to run ${tool ? `<code>${esc(tool)}</code>` : "a tool"}`;
    const taint = /\(([^)]+)\)$/.exec(u?.reason?.text || "");
    return `<p class="af-lead">${lead}${taint ? `, <span class="af-muted">${esc(taint[1])}</span>` : ""}.</p>
      <pre class="af-cmd">${esc(cmd)}</pre>
      ${pairs([["Changes", files], ["Branch", u?.branch || "", true], ["Asked", it.since ? `${since(it.since)} ago` : ""], ["Expires", it.expires_at ? untilWords(it.expires_at) : ""]])}
      ${denying ? reasonHtml(where, it, kind) : `<div class="af-acts">${openLink(u, it)}<div class="af-acts__end">${sbtn("Deny", "danger", { "data-dk": "deny" })}${
        sbtn("Allow for this turn", "ghost", { "data-dk": "allow_always", title: "Also allows calls like this one until the turn ends; the next turn asks again" })}${sbtn(push ? "Approve push" : "Approve", "primary", { "data-dk": "allow" })}</div></div>`}`;
  }
  if (kind === "job" || kind === "request") {
    return `<p class="af-lead">${esc(clean(it.reason) || "A job waits for a trusted person before it starts.")}</p>
      ${pairs([["Requested", it.since ? `${since(it.since)} ago` : ""], ["Expires", it.expires_at ? untilWords(it.expires_at) : ""], ["Repo", repoShort(it.repo || u?.repo), true]])}
      ${denying ? reasonHtml(where, it, kind) : `<div class="af-acts">${openLink(u, it)}<div class="af-acts__end">${sbtn(kind === "job" ? "Reject" : "Deny", "danger", { "data-dk": "deny" })}${sbtn(kind === "job" ? "Approve job" : "Approve", "primary", { "data-dk": "allow" })}</div></div>`}`;
  }
  if (kind === "question") {
    const opts = (it.question_options && it.question_options.length ? it.question_options : u?.question_options || []).slice(0, 5);
    const free = draftOf(where, it.id, "answer"), pick = fleet.picks.get(it.id);
    return `<p class="af-lead">${esc(clean(it.reason) || u?.question || "The agent asks a question.")}</p>
      ${opts.length ? `<div class="af-chips" role="group" aria-label="Options">${opts.map((o, i) => `<button type="button" class="af-chip${/^[\w./-]+$/.test(o) ? " af-chip--mono" : ""}${free ? " is-dismissed" : ""}" aria-pressed="${pick === i}" data-dk="pick" data-i="${i}">${esc(o)}</button>`).join("")}</div>` : ""}
      <div class="af-input af-input--area af-input--sans"><textarea rows="${opts.length ? 1 : 2}" data-draft="${esc(DRAFT(where, it.id, "answer"))}" aria-label="${opts.length ? "Or answer in your own words" : "Your answer"}" placeholder="${opts.length ? "Or type an answer" : "The agent continues with this"}">${esc(free)}</textarea></div>
      <div class="af-acts">${openLink(u, it)}<span class="af-note">${u?.turn ? `Holding turn ${u.turn}` : ""}</span><div class="af-acts__end">${sbtn("Answer", "primary", { "data-dk": "answer" })}</div></div>`;
  }
  if (kind === "denied") {
    return `<p class="af-lead">${esc(u?.reason?.text || clean(it.reason).replace(/^policy denied /, "Policy denied "))}</p>
      <p class="af-ctx">A hard stop: ${esc(h)} was told and carries on without that step. Nothing to decide; acknowledge it to put it away.</p>
      <div class="af-acts">${openLink(u, it)}<div class="af-acts__end">${sbtn("Acknowledge", "secondary", { "data-dk": "ack" })}</div></div>`;
  }
  const job = u?.job || it.job;
  return `<p class="af-lead">${esc(u?.reason?.text || clean(it.reason))}</p>
    <p class="af-ctx">${job ? `Retry continues the same session: ${esc(h)} keeps its context and branch and re-runs the check. Start over is a new job from the base branch.` : "Acknowledge it to put it away for a week."}</p>
    <div class="af-acts">${openLink(u, it)}${sbtn("Acknowledge", "ghost", { "data-dk": "ack" })}<div class="af-acts__end">${job ? `${sbtn("Start over", "secondary", { "data-dk": "fresh" })}${sbtn("Retry", "primary", { "data-dk": "retry" }, "retry")}` : ""}</div></div>`;
}
function decisionCard(it, k, where) {
  const u = unitOfItem(it), role = roleOfItem(it), v = fleet.verdicts.get(it.id);
  const exp = DECIDE.has(it.kind) ? (it.expires_at || u?.ask_expires_at ? `${untilWords(it.expires_at || u.ask_expires_at)} left` : `asked ${since(it.since)} ago`) : `${since(it.since)} ago`;
  const work = u?.work || it.title || tailOf(it.job || it.session) || "";
  return `<article class="af-decision af-decision--${role}${v ? " af-decision--done" : ""}" data-k="${k}" data-item="${esc(it.id)}" aria-label="${esc(`${u ? tailOf(u.id) : tailOf(it.job || it.session)} ${wordOfItem(it)}: ${work}`)}"${k ? " inert" : ""}>
    <div class="af-decision__hd">${status(role, wordOfItem(it))}<span class="af-decision__exp">${esc(exp)}</span></div>
    <div class="af-decision__who">${u ? `<button type="button" class="af-decision__link" data-dk="jump" data-unit="${esc(u.id)}">${idHtml(u.id)}${esc(work)}</button>`
      : `<span class="af-decision__link">${tails.isRaw(it.job || it.session) ? idHtml(it.job || it.session) : `<span class="af-id">${esc(it.job || it.session || "")}</span>`}${esc(work)}</span>`}
      <span class="af-decision__by">${u ? `${agentAvatar(u.harness, 22)}${esc(u.harness || "agent")} for ${esc(u.owner ? firstName(u.owner) : "hangar")}` : esc(repoShort(it.repo))}</span></div>
    <div class="af-decision__bd">${decisionBody(it, u, where)}</div>
  </article>`;
}
// the deck: the stack (top card in flow, two peeking) and the side (count, previous and next, up next)
function deckHtml(where) {
  const { stack: q, stopped } = deckSets();
  const all = fleet.data?.units || [];
  const running = all.filter((u) => u.role === "running").length, queued = all.filter((u) => u.role === "queued").length;
  const lit = q.filter((i) => DECIDE.has(i.kind)).length;
  const stack = q.length ? q.map((it, k) => decisionCard(it, k, where)).join("")
    : `<div class="af-empty af-empty--compact af-deck__empty">${ic("check")}<div class="af-empty__body"><h3 class="af-empty__title">Nothing waits on you.</h3>
        <p class="af-empty__text">${plural(running, "agent")} running, ${queued} queued. You will hear when one needs you.</p></div></div>`;
  const when = (it) => (DECIDE.has(it.kind) ? (it.expires_at || unitOfItem(it)?.ask_expires_at ? `${untilWords(it.expires_at || unitOfItem(it).ask_expires_at)} left` : `${since(it.since)} ago`) : wordOfItem(it));
  const row = (it, attrs) => { const u = unitOfItem(it); return `<li><button type="button" ${attrs(u)}>${markOnly(roleOfItem(it))}${idHtml(u?.id || it.job || it.session || "")}<span class="af-deck__what">${esc(u?.work || it.title || tailOf(it.job || it.session) || "")}</span><span class="af-deck__when">${esc(when(it))}</span></button></li>`; };
  // what stopped, under the decisions: its own list, never in the stack or up next; a row opens the agent, whose console
  // holds retry, start over and acknowledge
  const stoppedHtml = stopped.length ? `<h3 class="af-deck__subtitle">Stopped</h3>
      <ul class="af-deck__stopped">${stopped.map((it) => row(it, (u) => (u ? `data-dk="jump" data-unit="${esc(u.id)}"` : it.job ? `data-go="job:${esc(it.job)}"` : "disabled"))).join("")}</ul>` : "";
  return `<section class="af-deck" aria-label="Needs you" data-deck="${where}">
    <div class="af-deck__stack">${stack}</div>
    <aside class="af-deck__side">
      <h2 class="af-deck__title">Needs you<span class="af-deck__count" aria-live="polite">${q.length ? `1 of ${q.length}` : ""}</span>${where === "band"
        ? `<span class="fleet-band__hide">${btn("Hide", { kind: "ghost", sm: true, icon: "chevron-down", cls: "fleet-flip-y", attrs: { "data-band": "close", "aria-expanded": "true", "aria-controls": "fleet-band" } })}</span>` : ""}</h2>
      <p>${lit ? "One decision at a time, soonest to expire on top. Decide and the next one rises; the agent continues the moment you do."
        : q.length ? "Nothing to decide right now: these stopped. Retry, start over, or acknowledge them to put them away." : "Decisions stack here, soonest to expire on top."}</p>
      ${q.length > 1 ? `<div class="af-deck__nav">${btn("", { kind: "secondary", sm: true, icon: "chevron-right", cls: "af-btn--icon fleet-flip", attrs: { "data-dk": "prev", "aria-label": "Previous" } })}${
        btn("", { kind: "secondary", sm: true, icon: "chevron-right", cls: "af-btn--icon", attrs: { "data-dk": "next", "aria-label": "Next" } })}<span class="af-note">Skip for now</span></div>
      <ul class="af-deck__upnext">${q.slice(1).map((it) => row(it, () => `data-dk="go" data-to="${esc(it.id)}"`)).join("")}</ul>` : ""}
      ${stoppedHtml}
    </aside>
  </section>`;
}

/* ------------------------------------------------------------------ the opened agent: decision, now, activity */
function decisionFor(u) {
  return queue().find((i) => (u.inbox || []).includes(i.id)) || null;
}
function consoleHtml(u) {
  const it = decisionFor(u);
  let dec = "";
  if (it) dec = `<section class="af-console__panel af-console__panel--decide" aria-label="Decision" data-item="${esc(it.id)}">${decisionBody(it, u, "console")}</section>`;
  else if (u.reason && (u.role === "approval" || u.role === "failed" || u.role === "denied")) {
    dec = `<section class="af-console__panel af-console__panel--decide" aria-label="Decision"><p class="af-lead">${esc(u.reason.text)}</p>
      <div class="af-acts">${openLink(u, {})}</div></section>`;
  }
  const sb = u.state, running = sb === "running", paused = sb === "paused", live = running || paused;
  const act = (a, icon, label, kind = "ghost", extra = {}) => sbtn(label, kind, { "data-act": a, ...extra }, icon);
  const left = live && u.expires_at ? u.expires_at - now() : null;
  const actions = sb === "stopped" ? "" : `<div class="af-acts">
      ${running ? act("term", "terminal", "Terminal", "secondary") + act("agent", "agent", "Run agent", "secondary") + act("exec", "play", "Run command") + act("fork", "fork", "Fork") + act("snapshot", "snapshot", "Snapshot") + act("pause", "pause", "Pause") : ""}
      ${paused ? act("resume", "play", "Resume", "secondary") : ""}
      ${live ? act("extend", "duration", "+1 h", "ghost", { title: "Add one hour to the lease" }) : ""}
      ${dec ? "" : openLink(u, {})}
      <div class="af-acts__end">${act("destroy", "", "Destroy", "danger")}</div></div>`;
  const facts = [u.files_changed ? `<span>${plural(u.files_changed, "file")}</span>` : "", u.commits ? `<span>${plural(u.commits, "commit")}</span>` : "",
    tokensOf(u) ? `<span>${kTok(tokensOf(u))} tok</span>` : "", u.usage?.cost_usd != null ? `<span>${money(u.usage.cost_usd)}</span>` : ""].join("");
  return `${dec}<section class="af-console__panel af-console__panel--now" aria-label="Now">
      <div class="af-console__now">${hbHtml(u)}<span class="af-console__big" data-now="${esc(u.id)}">${esc(nowLine(u))}</span></div>
      ${railHtml(u, true)}
      <div class="af-pairs af-pairs--wide">
        <span><small>Owner</small>${esc(ownerName(u.owner))}</span>
        <span><small>Harness</small><span class="af-mono">${esc(u.harness || "none")}</span></span>
        <span><small>Model</small><span class="af-mono">${esc(u.model || "default")}</span></span>
        <span><small>Repo</small><span class="af-mono">${esc(repoShort(u.repo) || "none")}</span></span>
        ${u.branch ? `<span><small>Branch</small><span class="af-mono">${esc(u.branch)}</span></span>` : ""}
        <span><small>Age</small><span class="af-mono">${esc(duration(now() - u.created_at))}</span></span>
        <span><small>Spent</small><span class="af-mono" data-cost="${esc(u.id)}">${u.usage?.cost_usd != null ? `${money(u.usage.cost_usd)}, ${kTok(tokensOf(u))} tok` : "nothing yet"}</span></span>
        ${left != null ? `<span><small>Lease</small>${left > 0 ? `${esc(duration(left))} left` : "expiring"}</span>` : ""}
      </div>
      ${actions}
    </section>
    <section class="af-console__panel af-console__panel--hist" aria-label="Activity">
      <div class="af-timeline__head"><h4>Activity</h4><span class="af-facts">${facts}</span>${sparkHtml(u, 120, 22)}<span class="af-note">tokens, last hour</span></div>
      <ol class="af-timeline" data-timeline="${esc(u.id)}">${timelineHtml(u)}</ol>
    </section>`;
}

// The activity: the stages the server saw, the job's own timeline (GET /api/jobs/{id}) and the agent's RunEvents
// (GET /api/{jobs,sessions}/{id}/events), filtered to what a person reads (commands, edits, progress, errors, asks),
// with a run of file changes collapsed to one line. Only the opened agent is followed, incrementally.
const JOB_KIND = { needs_input: "ask", awaiting_approval: "hold", failed: "fail", succeeded: "ok", cancelled: "off", verifying: "stage", delivering: "stage" };
function timelineEntries(u) {
  const tl = fleet.tl && fleet.tl.id === u.id ? fleet.tl : null;
  const out = [{ ts: u.created_at, k: "start", txt: `Sandbox started in ${u.environment}` }];
  for (const s of u.stages || []) {
    if (!STAGES.includes(s.stage)) continue;
    out.push({ ts: s.started_at, k: "stage", txt: `${STAGE_WORD[s.stage]} started` });
    if (s.ended_at) out.push({ ts: s.ended_at, k: "stage", txt: `${STAGE_WORD[s.stage]} done in ${duration(s.ended_at - s.started_at, { fine: false })}` });
  }
  for (const e of tl?.job || []) {
    if (["queued", "provisioning", "verifying", "delivering"].includes(e.state) && !e.message) continue;
    out.push({ ts: e.ts, k: JOB_KIND[e.state] || "job", txt: words(e.message || word(e.state)) });
  }
  let edit = null;
  for (const e of tl?.events || []) {
    if (e.kind !== "file_change") edit = null;
    if (e.kind === "file_change") {
      const path = (e.data?.path || e.text || "").split("\n")[0];
      if (edit) { edit.paths.add(path); edit.ts = e.ts; edit.txt = edit.paths.size === 1 ? `Edited ${path}` : `Edited ${edit.paths.size} files`; continue; }
      edit = { ts: e.ts, k: "files", paths: new Set([path]), txt: `Edited ${path}` };
      out.push(edit);
    } else if (e.kind === "command") out.push({ ts: e.ts, k: "cmd", txt: `Ran ${String(e.text || "").split("\n")[0].slice(0, 120)}` });
    else if (e.kind === "progress") out.push({ ts: e.ts, k: "progress", txt: words(String(e.text || "").split("\n")[0].slice(0, 160)) });
    else if (e.kind === "error") out.push({ ts: e.ts, k: "fail", txt: String(e.text || "error").split("\n")[0].slice(0, 160) });
    else if (e.kind === "approval_request") out.push({ ts: e.ts, k: "hold", txt: `Asked to run ${String(e.text || "").slice(0, 120)}` });
  }
  if (u.role === "approval" && u.reason && !out.some((x) => x.k === "hold" || x.k === "ask")) {
    out.push({ ts: waitingSince(u) || now(), k: u.label === "Needs input" ? "ask" : "hold", txt: u.label === "Needs input" ? `Asked: ${u.reason.text}` : `${u.reason.text} held for a person` });
  }
  if ((u.role === "failed" || u.role === "denied") && u.reason && !out.some((x) => x.k === "fail" || x.k === "deny")) {
    out.push({ ts: u.last_activity_at || now(), k: u.role === "failed" ? "fail" : "deny", txt: u.reason.text });
  }
  return out.filter((x) => x.ts).sort((a, b) => a.ts - b.ts).slice(-60);
}
function timelineHtml(u) {
  const es = timelineEntries(u);
  return es.map((e, i) => `<li class="af-timeline__ev af-timeline__ev--${e.k}${i === es.length - 1 ? " is-last" : ""}"><i class="af-timeline__dot"></i><time class="af-timeline__time" datetime="${new Date(e.ts * 1000).toISOString()}">${wall(e.ts)}<small>${since(e.ts) === "now" ? "now" : `${since(e.ts)} ago`}</small></time><span class="af-timeline__text">${esc(e.txt)}</span></li>`).join("");
}
async function followOpen(first = false) {
  const u = fleet.byId.get(fleet.open);
  if (!u) return;
  if (!fleet.tl || fleet.tl.id !== u.id) fleet.tl = { id: u.id, next: 0, events: [], job: [], busy: false, loaded: false };
  const tl = fleet.tl;
  if (tl.busy) return;
  tl.busy = true;
  try {
    const ref = u.job ? `jobs/${u.job}` : u.session ? `sessions/${u.session.id}` : null;
    const jobs = u.job && (first || !tl.loaded || u.job_state !== tl.jobState) ? api("GET", `/api/jobs/${u.job}`).then((j) => { tl.job = j.events || []; tl.jobState = u.job_state; }).catch(() => {}) : null;
    const evs = ref ? api("GET", `/api/${ref}/events?${tl.loaded ? `since=${tl.next}&limit=500` : "tail=300"}`).then((r) => {
      tl.events = tl.events.concat(r.events || []).filter((e) => e.kind !== "usage" && e.kind !== "tool_result" && e.kind !== "message").slice(-400);
      tl.next = r.next || tl.next;
    }).catch(() => {}) : null;
    await Promise.all([jobs, evs]);
    tl.loaded = true;
  } finally { tl.busy = false; }
  const ol = $(`[data-timeline="${CSS.escape(u.id)}"]`);
  if (ol && fleet.tl === tl) {
    const html = timelineHtml(unitView(fleet.byId.get(u.id) || u));
    if (ol.dataset.html !== html) {
      const atEnd = ol.scrollTop + ol.clientHeight >= ol.scrollHeight - 8;
      ol.innerHTML = html; ol.dataset.html = html;
      if (atEnd || first) toLatest(ol);
    }
  }
}

// Show the latest events whole: the list scrolls past its max-height, so trim the window to the whole entries that fit,
// counted from the newest, and scroll to the end; the first entry in view is never cut in half.
function toLatest(ol) {
  ol.style.maxHeight = "";
  const cap = ol.clientHeight;
  if (ol.scrollHeight > cap + 1) {
    let h = 0;
    for (const li of [...ol.children].reverse()) { if (h + li.offsetHeight > cap) break; h += li.offsetHeight; }
    if (h) ol.style.maxHeight = `${h}px`;
  }
  ol.scrollTop = ol.scrollHeight;
}

/* ------------------------------------------------------------------ the pages */
const ENV_ICON = (env) => ic(/gpu/i.test(env) ? "gpu" : "environment");
function headline(us) {
  const c = Object.fromEntries(ATTENTION.map((r) => [r, us.filter((u) => u.role === r).length]));
  const atWork = c.running + c.approval, need = c.approval, stopped = c.failed + c.denied;
  const n = atWork === 1 ? "One agent at work." : `${number(atWork)} agent${atWork === 1 ? "" : "s"} at work.`;
  return `${n} ${need ? `<em>${number(need)} need${need === 1 ? "s" : ""} you</em>` : "None need you"}${stopped ? `, ${stopped === 1 ? "one" : stopped} stopped` : ""}.`;
}
function draw() {
  if (!fleet.data) return;
  counts();
  if (state.view === "fleet") drawFleet();
  if (state.view === "needs") drawNeeds();
  announce();
}
function counts() {
  const us = units(), q = queue().filter((i) => DECIDE.has(i.kind) && !fleet.verdicts.has(i.id)).length;
  navCount("fleet", us.filter((u) => u.live).length);
  navCount("needs", q);
  const top = $("#nav-needs");
  if (top) { top.className = q ? "af-nav__badge" : "af-nav__count"; top.setAttribute("aria-label", q ? `${q} need you` : ""); }
}
function drawFleet() {
  const us = units(), d = fleet.data, root = $("#view-fleet");
  root.dataset.fleetView = fleet.view;
  $$("button[data-fleet-view]", root).forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.fleetView === fleet.view)));
  $("#fleet-title").innerHTML = us.length ? headline(us) : "No agents at work.";
  const spent = us.reduce((n, u) => n + (u.usage?.cost_usd || 0), 0);
  $("#fleet-sub").textContent = `${d.slots?.used ?? us.filter((u) => u.live).length}${d.slots?.max ? ` of ${d.slots.max}` : ""} slots in use.${spent ? ` ${money(spent)} spent by the live turns.` : ""}`;
  // the owners filter (?owner=): everyone, then each person with a count
  const owners = d.owners || [];
  paint($("#fleet-owners"), owners.length > 1 || fleet.owner ? `<span class="af-note">Owner</span><div class="af-chips" role="group" aria-label="Owner">
    <button type="button" class="af-chip" data-owner="" aria-pressed="${!fleet.owner}">Everyone</button>${owners.map((o) =>
      `<button type="button" class="af-chip fleet-owner" data-owner="${esc(o.id)}" aria-pressed="${fleet.owner === o.id}">${o.id === "none" ? "Nobody" : esc(o.name || o.id)}<span class="af-note">${o.count}</span></button>`).join("")}</div>` : "", "owners");
  // the deck band: the same deck as Needs you, collapsible; gone when nothing waits
  const q = queue();
  const band = $("#fleet-band");
  band.hidden = !q.length;
  if (q.length) {
    const sq = deckSets().stack, lit = DECIDE.has(sq[0].kind), n = sq.length;
    paint(band, fleet.band ? deckHtml("band") : `<button type="button" class="fleet-band__bar" data-band="open" aria-expanded="false" aria-controls="fleet-band">${markOnly(roleOfItem(sq[0]))}
      <span class="fleet-band__what"><b>${lit ? "Needs you" : "Stopped"}</b> ${lit ? (n === 1 ? "one decision" : `${n} decisions`) : (n === 1 ? "one agent" : `${n} agents`)}: ${esc(unitOfItem(sq[0])?.work || sq[0].title || "")}</span>${ic("chevron-down")}<span class="af-note">Show</span></button>`, `band:${fleet.band}`);
  }
  $("#fleet-note").textContent = `${us.filter((u) => u.role === "running").length} running, ${us.filter((u) => u.role === "queued").length} queued${fleet.owner ? `, ${firstName(fleet.owner === "none" ? null : fleet.owner)}'s only` : ""}`;
  const envs = [...new Set(us.map((u) => u.environment))].sort();
  const view = fleet.view === "board" ? cardHtml : rowHtml;
  const groups = envs.map((env) => {
    const here = us.filter((u) => u.environment === env).sort((a, b) => ROLE_RANK[a.role] - ROLE_RANK[b.role] || a.created_at - b.created_at || a.id.localeCompare(b.id));
    const live = here.filter((u) => u.live).length;
    return `<section class="af-runway__group" aria-label="${esc(env)}"><div class="af-runway__head"><h3>${ENV_ICON(env)}${esc(env)}</h3><span class="af-note">${plural(here.length, "agent")}, ${live} live</span></div>
      <div class="${fleet.view === "board" ? "af-board" : "af-runway"}" role="group" aria-label="${esc(env)} agents">${here.map(view).join("")}</div></section>`;
  }).join("");
  const empty = emptyState({ icon: "fleet", title: fleet.owner ? "Nobody's agents match." : "No agents at work.", cls: "fleet-empty",
    text: fleet.owner ? "Clear the owner filter to see everyone's." : "Start a sandbox from an environment, or hand an agent a job; the first boot of an image takes about a minute.",
    actions: fleet.owner ? btn("Everyone", { attrs: { "data-owner": "" } }) : `${btn("Start a sandbox", { attrs: { "data-act": "new" } })}<a class="af-link" href="#environments">Environments</a>` });
  const changed = paint($("#fleet-body"), us.length ? groups : empty, `body:${fleet.view}`);
  $("#fleet-key").hidden = !us.length;
  if (changed) afterBodyPaint();
  if (fleet.open && fleet.byId.has(fleet.open)) followOpen();
}
// a fresh body: the opened agent opens in place (once, with the accordion), and the roving tab stop is put back
function afterBodyPaint() {
  const root = $("#fleet-body");
  if (!fleet.nav) fleet.nav = airframe.roving(root);
  const open = fleet.open ? $(`.af-agent[data-id="${CSS.escape(fleet.open)}"]`, root) : null;
  if (open) {
    const line = $(".af-agent__line, .af-agent__face", open);
    if (fleet.opening) {
      fleet.opening = false;
      requestAnimationFrame(() => { airframe.expand.open(open, { exclusive: false }); followOpen(true); });
    } else { open.classList.add("is-open"); line.setAttribute("aria-expanded", "true"); }
  }
  const active = fleet.focus ? $(`.af-agent[data-id="${CSS.escape(fleet.focus)}"] :is(.af-agent__line, .af-agent__face)`, root) : null;
  const first = active || $(".af-agent__line, .af-agent__face", root);
  if (first) fleet.nav.setActive(first, false);
}
function drawNeeds() {
  const items = fleet.data?.inbox?.items || [], q = queue();
  const lit = q.filter((i) => DECIDE.has(i.kind)).length, stopped = q.length - lit;
  $("#needs-sum").innerHTML = (lit ? status("approval", `${number(lit)} waiting on you`, { lit: true }) : status("running", "Nothing is waiting on you"))
    + meta([["stopped", stopped], ["review", items.filter((i) => i.kind === "review").length]]);
  paint($("#needs-deck"), deckHtml("page"), "needs");
  const review = items.filter((i) => i.kind === "review");
  // the PRs waiting on a person's review on GitHub: a sentence-case heading and one neutral AgentRow-shaped row each
  // (queued mark, tail, title and why, repo, the two links); waiting on others stays grey
  paint($("#needs-review"), review.length ? `<div class="fleet-title needs-review__title"><h2>Waiting for review</h2><span class="af-note">on GitHub, ${plural(review.length, "pull request")}</span></div>
    <ul class="needs-review__list">${review.map((i) => { const u = unitOfItem(i), pr = i.links?.pr || i.pr; return `<li class="af-agent needs-review__row">${markOnly("queued")}
      ${u ? idHtml(u.id, { codeOnly: true, cls: "af-agent__tail" }) : `<span class="af-agent__tail"></span>`}
      <span class="af-agent__ttl"><span class="af-agent__work">${esc(i.title || u?.work || tailOf(i.job) || "")}</span><span class="af-agent__sub"><span class="af-agent__now">${esc(i.reason || "")}</span></span></span>
      <span class="af-agent__rig">${i.repo ? `<span class="af-mono">${esc(repoShort(i.repo))}</span>` : ""}</span>
      <span class="needs-review__acts">${pr ? `<a class="af-btn af-btn--ghost af-btn--sm" href="${esc(pr)}" target="_blank" rel="noopener">Open PR</a>` : ""}${i.job ? sbtn("Open job", "ghost", { "data-go": `job:${i.job}` }) : ""}</span></li>`; }).join("")}</ul>` : "", "review");
}
// say it when what needs someone changes (the screen reader hears it; the screen already shows it)
function announce() {
  const us = units(), waiting = us.filter((u) => ROLE_RANK[u.role] <= 2).map((u) => u.id).sort().join(" ");
  if (fleet.attention !== null && waiting !== fleet.attention) {
    const n = us.filter((u) => u.role === "approval").length, bad = us.filter((u) => u.role === "failed" || u.role === "denied").length;
    $("#fleet-live").textContent = `${n} agent${n === 1 ? "" : "s"} need${n === 1 ? "s" : ""} you, ${bad} failed or denied.`;
  }
  fleet.attention = waiting;
}

// Repaint only what changed, keeping focus, a half-written reason or answer, and scroll, so a live update is invisible.
function paint(el, html, key) {
  if (fleet.painted[el.id] === html && el.dataset.painted === key) return false;
  fleet.painted[el.id] = html;
  const active = document.activeElement, inside = el.contains(active) && active !== el;
  const sel = inside ? focusKey(active) : "", scope = inside ? active.closest("[data-id], [data-item]") : null;
  const scopeSel = scope ? (scope.dataset.id ? `[data-id="${CSS.escape(scope.dataset.id)}"]` : `[data-item="${CSS.escape(scope.dataset.item)}"]`) : "";
  const caret = inside && active.matches("textarea") ? [active.selectionStart, active.selectionEnd] : null;
  const scrolls = $$(".af-timeline", el).map((o) => [o.dataset.timeline, o.scrollTop, o.style.maxHeight]);
  el.innerHTML = html;
  el.dataset.painted = key;
  for (const [id, top, max] of scrolls) { const o = $(`[data-timeline="${CSS.escape(id || "")}"]`, el); if (o) { o.style.maxHeight = max; o.scrollTop = top; } }
  if (sel) {
    const sc = scopeSel ? $(scopeSel, el) : el;
    const t = sc && (sc.matches(sel) ? sc : $(sel, sc));
    if (t) { t.focus({ preventScroll: true }); if (caret && t.setSelectionRange) t.setSelectionRange(...caret); }
  }
  return true;
}
const focusKey = (n) => ["data-dk", "data-act", "data-draft", "data-owner", "data-fleet-view", "data-i", "data-to", "data-band", "data-go"]
  .filter((a) => n.hasAttribute(a)).map((a) => `[${a}="${CSS.escape(n.getAttribute(a))}"]`).join("")
  || (n.matches(".af-agent__line") ? ".af-agent__line" : n.matches(".af-agent__face") ? ".af-agent__face" : "");

// a live update that only moved the now-line, the heartbeat, the rail, the spark or the spend: patch it in place
function patchUnit(u) {
  const rows = $$(`.af-agent[data-id="${CSS.escape(u.id)}"]`);
  if (!rows.length) return state.view !== "fleet";
  for (const n of $$(`[data-now="${CSS.escape(u.id)}"]`)) {
    const t = nowLine(u);
    if (n.textContent !== t) { n.textContent = t; if (airframe.duration("dur")) n.animate([{ opacity: 0.35 }, { opacity: 1 }], { duration: 400 }); }
  }
  for (const n of $$(`[data-hb="${CSS.escape(u.id)}"]`)) { n.className = hbCls(u); n.title = hbTitle(u); n.lastChild.textContent = since(waitingSince(u)); }
  for (const n of $$(`[data-rail="${CSS.escape(u.id)}"]`)) {
    const pr = progress(u);
    if (!pr) continue;
    const p = railP(pr);
    n.style.setProperty("--p", `${p.toFixed(1)}%`);
    n.style.setProperty("--s", `${(pr.i * 33.333).toFixed(1)}%`);
    n.setAttribute("aria-label", railLabel(u, pr));
    n.classList.toggle("af-rail--est", pr.est);
    $$(".af-rail__stop", n).forEach((s, k) => s.classList.toggle("is-on", k * 33.333 <= p));
  }
  for (const n of $$(`[data-ring="${CSS.escape(u.id)}"]`)) n.innerHTML = ringHtml(u);
  for (const n of $$(`[data-capsule="${CSS.escape(u.id)}"]`)) n.innerHTML = capsuleHtml(u);
  for (const n of $$(`[data-spark="${CSS.escape(u.id)}"]`)) { const w = Number(n.getAttribute("width")), h = Number(n.getAttribute("height")); n.outerHTML = sparkHtml(u, w, h); }
  for (const n of $$(`[data-stages="${CSS.escape(u.id)}"]`)) n.outerHTML = stagesHtml(u);
  for (const n of $$(`[data-cost="${CSS.escape(u.id)}"]`)) n.textContent = n.closest(".af-console") ? (u.usage?.cost_usd != null ? `${money(u.usage.cost_usd)}, ${kTok(tokensOf(u))} tok` : "nothing yet") : money(u.usage?.cost_usd);
  fleet.painted["fleet-body"] = null;  // the next full paint compares against fresh markup
  return true;
}
// the clock moves the heartbeats and the open stages even when nothing arrives (every 5 s; 15 s under reduced motion)
setInterval(() => {
  if (document.hidden || !fleet.data || (state.view !== "fleet" && state.view !== "needs")) return;
  for (const u of units()) {
    for (const n of $$(`[data-hb="${CSS.escape(u.id)}"]`)) { const t = since(waitingSince(u)); if (n.lastChild.textContent !== t) n.lastChild.textContent = t; }
    for (const n of $$(`[data-stages="${CSS.escape(u.id)}"]`)) { const h = stagesHtml(u); if (n.outerHTML !== h) n.outerHTML = h; }
  }
}, airframe.duration("dur") ? 5000 : 15000);

/* ------------------------------------------------------------------ deciding: optimistic verdict, then reload */
function itemById(id) { return (fleet.data?.inbox?.items || []).find((i) => i.id === id); }
// a verdict lands: the card shows it at once, the agent's row flips, and after a few seconds the card leaves and the
// fleet reloads. A 404 or 409 means someone else got there first (or it expired): say so, and let the card go.
async function decide(it, verdict, send, flip) {
  const u = unitOfItem(it);
  fleet.verdicts.set(it.id, verdict);
  fleet.done.add(it.id);
  fleet.deny.delete(it.id); fleet.invalid.delete(it.id);
  if (u && flip) fleet.overrides.set(u.id, { from: u.role, until: Date.now() + 15000, patch: flip });
  draw();
  try { await send(); }
  catch (e) {
    if (e.status === 404 || e.status === 409) fleet.verdicts.set(it.id, { ok: false, text: `Already settled: someone else decided it, or it expired. ${e.message && e.message.length < 120 ? e.message : ""}`.trim() });
    else {
      fleet.verdicts.delete(it.id); fleet.done.delete(it.id); if (u) fleet.overrides.delete(u.id);
      draw();
      if (e.message !== "unauthorized") toast(e.message, true);
      return;
    }
    if (u) fleet.overrides.delete(u.id);
    draw();
  }
  setTimeout(() => leave(it.id), VERDICT_MS);
}
async function leave(id) {
  const cards = $$(`.af-decision[data-item="${CSS.escape(id)}"]`);
  await Promise.all(cards.map((c) => (c.closest(".af-deck__stack") ? airframe.deck.leave(c) : Promise.resolve())));
  fleet.verdicts.delete(id);
  if (fleet.deckTop === id) fleet.deckTop = null;
  fleet.painted = {};
  try { await fetchFleet(); } catch { draw(); }
}
const pathOf = (it, u) => (it.job || u?.job ? `/api/jobs/${it.job || u.job}` : `/api/sessions/${encodeURIComponent(u?.session?.id || it.session)}`);
function act(dk, it, el, where) {
  const u = unitOfItem(it), h = harnessOf(it, u), kind = decisionKind(it), me = meName();
  const running = (now) => ({ role: "running", label: "Running", reason: null, now, last_activity_at: Date.now() / 1000 });
  if (dk === "deny") { fleet.deny.add(it.id); draw(); setTimeout(() => $(`[data-draft="${CSS.escape(DRAFT(where, it.id, "reason"))}"]`)?.focus(), 30); return; }
  if (dk === "cancel") { fleet.deny.delete(it.id); fleet.invalid.delete(it.id); draw(); return; }
  if (dk === "pick") { const i = Number(el.dataset.i); fleet.picks.set(it.id, fleet.picks.get(it.id) === i ? undefined : i); draw(); return; }
  if (dk === "dismiss") { leave(it.id); return; }
  if (dk === "deny-confirm") {
    const reason = draftOf(where, it.id, "reason").trim();
    if (!reason) { fleet.invalid.add(it.id); draw(); setTimeout(() => $(`[data-draft="${CSS.escape(DRAFT(where, it.id, "reason"))}"]`)?.focus(), 30); return; }
    fleet.drafts.delete(DRAFT(where, it.id, "reason"));
    if (kind === "job") return decide(it, { ok: false, text: `Rejected by ${me}: "${reason}". The job is dropped.` }, () => api("POST", `/api/jobs/${it.job}/reject`, { reason }));
    if (kind === "request") return decide(it, { ok: false, text: `Denied by ${me}: "${reason}".` }, () => api("POST", `/api/env-requests/${it.request}`, { decision: "deny", note: reason }), running("reading the deny reason"));
    return decide(it, { ok: false, text: `Denied by ${me}: "${reason}". ${h} was told and is revising.` },
      () => api("POST", `${pathOf(it, u)}/approvals/${it.approval}`, { decision: "deny", message: reason }), running("reading the deny reason"));
  }
  if (dk === "allow" || dk === "allow_always") {
    if (kind === "job") return decide(it, { ok: true, text: `Approved by ${me}; the job starts.` }, () => api("POST", `/api/jobs/${it.job}/approve`, {}));
    if (kind === "request") return decide(it, { ok: true, text: `Approved by ${me}; ${h} continues.` }, () => api("POST", `/api/env-requests/${it.request}`, { decision: "approve", note: null }), running("continuing with the access granted"));
    const { cmd } = heldCall(it, u);
    return decide(it, { ok: true, text: `${dk === "allow_always" ? "Allowed for this turn" : "Approved"} by ${me}; ${h} is running ${cmd.slice(0, 60)}.` },
      () => api("POST", `${pathOf(it, u)}/approvals/${it.approval}`, { decision: dk, message: null }), running(`running ${cmd.slice(0, 50)}`));
  }
  if (dk === "answer") {
    const free = draftOf(where, it.id, "answer").trim(), opts = it.question_options?.length ? it.question_options : u?.question_options || [];
    const pick = fleet.picks.get(it.id), ans = free || (pick != null ? opts[pick] : "");
    if (!ans) { $(`[data-draft="${CSS.escape(DRAFT(where, it.id, "answer"))}"]`)?.focus(); return; }
    fleet.drafts.delete(DRAFT(where, it.id, "answer")); fleet.picks.delete(it.id);
    const send = it.job || u?.job ? () => api("POST", `/api/jobs/${it.job || u.job}/follow-up`, { message: ans })
      : () => api("POST", `/api/sessions/${encodeURIComponent(u?.session?.id || it.session)}/send`, { message: ans });
    return decide(it, { ok: true, text: `Answered "${ans.slice(0, 80)}". ${h} continues${u?.turn ? ` with turn ${u.turn + 1}` : ""}.` }, send, running(`applying your answer: ${ans.slice(0, 40)}`));
  }
  if (dk === "retry") return decide(it, { ok: true, text: `Retrying in the same session: ${h} re-runs the check and fixes what failed.` },
    () => api("POST", `/api/jobs/${it.job || u.job}/retry`), running("re-running the failing check"));
  if (dk === "fresh") return decide(it, { ok: true, text: `Started over as a new job from the base branch; this one stays as it ended.` },
    () => api("POST", `/api/jobs/${it.job || u.job}/retry?fresh=1`).then((r) => { const v = fleet.verdicts.get(it.id); if (v && r?.id) v.text = `Started over as ${tailOf(r.id)}, from the base branch; this one stays as it ended.`; }));
  if (dk === "ack") {
    fleet.done.add(it.id);
    api("POST", `/api/inbox/${encodeURIComponent(it.id)}/ack`).catch((e) => { if (e.status !== 404 && e.status !== 409) { fleet.done.delete(it.id); toast(e.message, true); draw(); } });
    leave(it.id);
  }
}

/* ------------------------------------------------------------------ opening, keys and the sandbox actions */
function openUnit(id) {
  if (fleet.open === id) return closeUnit();
  fleet.open = id; fleet.focus = id; fleet.opening = true; fleet.tl = null;
  fleet.painted["fleet-body"] = null;
  drawFleet();
}
function closeUnit() {
  const id = fleet.open, el = id ? $(`#fleet-body .af-agent[data-id="${CSS.escape(id)}"]`) : null;
  fleet.open = null; fleet.tl = null;
  if (el) airframe.expand.close(el);
  setTimeout(() => { if (!fleet.open) { fleet.painted["fleet-body"] = null; drawFleet(); } }, airframe.duration("dur-open") + 20);
}
function goTo(target) {
  const [kind, id] = target.split(/:(.+)/);
  if (kind === "job") { state.selectedJob = id; location.hash = `#jobs?job=${id}`; return; }
  const s = (state.sessions || []).find((x) => x.id === id);
  state.selectedSession = id;
  if (s?.job_id) $("#ses-jobs").checked = true;
  location.hash = "#sessions";
}
function setHash(params) {
  const p = new URLSearchParams();
  const view = params.view ?? fleet.view, owner = params.owner === undefined ? fleet.owner : params.owner;
  if (view !== "list") p.set("view", view);
  if (owner) p.set("owner", owner);
  history.replaceState(null, "", `#fleet${p.toString() ? `?${p}` : ""}`);
  try { localStorage.setItem(FLEET_STORE, view); } catch {}
  fleet.view = view; fleet.owner = owner || null;
}

// one delegated handler for both pages: decisions (data-dk), the views, the owners, the band, the rows, the actions
async function onClick(e) {
  const t = e.target;
  const dk = t.closest("[data-dk]");
  if (dk) {
    e.preventDefault();
    const d = dk.dataset.dk;
    const stack = dk.closest(".af-deck")?.querySelector(".af-deck__stack");
    if (d === "next" || d === "prev") { const top = airframe.deck[d](stack); fleet.deckTop = top?.dataset.item || null; return; }
    if (d === "go") { const cards = $$(":scope > .af-decision", stack), i = cards.findIndex((c) => c.dataset.item === dk.dataset.to); const top = airframe.deck.go(stack, i); fleet.deckTop = top?.dataset.item || null; return; }
    if (d === "jump") {
      const id = dk.dataset.unit;
      if (state.view !== "fleet") { location.hash = "#fleet"; fleet.open = null; setTimeout(() => openUnit(id), 60); return; }
      if (fleet.open !== id) openUnit(id);
      return;
    }
    const where = dk.closest("[data-deck]")?.dataset.deck || "console";
    const id = dk.closest("[data-item]")?.dataset.item, it = id && itemById(id);
    if (it) act(d, it, dk, where);
    return;
  }
  const fv = t.closest("button[data-fleet-view]");
  if (fv) { e.preventDefault(); setHash({ view: fv.dataset.fleetView }); fleet.painted["fleet-body"] = null; drawFleet(); return; }
  const ow = t.closest("button[data-owner]");
  if (ow) { setHash({ owner: ow.dataset.owner || null }); closeStream(); fleet.painted = {}; loadFleet(); return; }
  const band = t.closest("button[data-band]");
  if (band) {
    fleet.band = band.dataset.band === "open";
    try { localStorage.setItem(BAND_STORE, fleet.band ? "open" : "closed"); } catch {}
    drawFleet();
    $("#fleet-band [data-band]")?.focus();
    return;
  }
  const go = t.closest("[data-go]");
  if (go) { e.preventDefault(); return goTo(go.dataset.go); }
  const line = t.closest(".af-agent__line, .af-agent__face");
  if (line && !t.closest(".af-console")) { openUnit(line.closest(".af-agent").dataset.id); return; }
  const b = t.closest("button[data-act]");
  if (!b) return;
  const a = b.dataset.act;
  if (a === "new") { openNewForm(); $("#new-form").scrollIntoView({ behavior: "smooth", block: "start" }); return; }
  const u = fleet.byId.get(b.closest("[data-id]")?.dataset.id);
  if (!u) return;
  const sb = { id: u.id, name: u.name, environment: u.environment, state: u.state };
  if (a === "term") return openTerminal(sb);
  if (a === "agent") return agentDialog(sb);
  if (a === "exec") return execDialog(sb);
  if (a === "fork") return forkDialog(sb);
  if (a === "destroy" && !(await confirmAction(`Destroy ${tailOf(u.id)}?`, `Its filesystem is deleted unless you snapshot it first.${u.job || u.session
    ? " The branch and the conversation are kept." : ""}`, "Destroy"))) return;
  await busy(b, async () => {
    if (a === "destroy") { await api("DELETE", `/api/sandboxes/${u.id}`); toast(`Destroyed ${tailOf(u.id)}`); closeTerminalsFor(u.id); fleet.open = null; }
    else if (a === "snapshot") { const s = await api("POST", `/api/sandboxes/${u.id}/snapshot`, { kind: "auto" }); toast(`Snapshot ${tailOf(s.id)} taken (${s.kind})`); }
    else if (a === "pause") { await api("POST", `/api/sandboxes/${u.id}/pause`); toast(`Paused ${tailOf(u.id)}`); }
    else if (a === "resume") { await api("POST", `/api/sandboxes/${u.id}/resume`); toast(`Resumed ${tailOf(u.id)}`); }
    else if (a === "extend") { await api("POST", `/api/sandboxes/${u.id}/extend?minutes=60`); toast(`${tailOf(u.id)} extended by 1 hour`); }
    await fetchFleet();
  });
}
for (const root of [$("#view-fleet"), $("#view-needs")]) {
  root.addEventListener("click", onClick);
  // drafts survive every repaint; free text dims the question's chips
  root.addEventListener("input", (e) => {
    const k = e.target.dataset?.draft;
    if (!k) return;
    fleet.drafts.set(k, e.target.value);
    if (k.endsWith(":answer")) $$(".af-chip[data-dk=pick]", e.target.closest(".af-decision__bd, .af-console__panel")).forEach((c) => c.classList.toggle("is-dismissed", !!e.target.value));
    if (k.endsWith(":reason") && e.target.value.trim()) {
      const id = k.split(":")[1];
      if (fleet.invalid.delete(id)) { e.target.closest(".af-input").classList.remove("is-invalid"); e.target.setAttribute("aria-invalid", "false"); e.target.closest(".af-reason").querySelector(".af-field__error")?.remove(); }
    }
  });
  // the deck's arrows: left and right move through the deck when focus is on its side or a card's header
  root.addEventListener("af:deck", (e) => {
    const deckEl = e.target.closest(".af-deck"), n = e.detail.count;
    const count = deckEl && $(".af-deck__count", deckEl);
    const cards = $$(":scope > .af-decision", e.target), q = deckSets().stack;
    if (count && n) count.textContent = `${q.findIndex((i) => i.id === e.detail.top?.dataset.item) + 1 || 1} of ${n}`;
    void cards;
  });
}
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape" || state.view !== "fleet" || !fleet.open) return;
  if (e.target.closest?.(".af-dialog, .terminal-drawer, #drawer")) return;
  e.preventDefault();
  const id = fleet.open;
  closeUnit();
  $(`#fleet-body .af-agent[data-id="${CSS.escape(id)}"] :is(.af-agent__line, .af-agent__face)`)?.focus({ preventScroll: true });
});
// Ctrl+Enter (Cmd+Enter) in a reason or an answer sends it
document.addEventListener("keydown", (e) => {
  if (e.key !== "Enter" || !(e.ctrlKey || e.metaKey)) return;
  const k = e.target.dataset?.draft;
  if (!k) return;
  const box = e.target.closest(".af-decision__bd, .af-console__panel");
  $(k.endsWith(":reason") ? "[data-dk=deny-confirm]" : "[data-dk=answer]", box)?.click();
});
// the opened agent's activity follows on every poll (the stream wakes it too)
setInterval(() => { if (!document.hidden && state.view === "fleet" && fleet.open) followOpen(); }, POLL_MS);
