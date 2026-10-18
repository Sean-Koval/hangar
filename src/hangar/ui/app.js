/* Hangar dashboard. Vanilla JS, no build step. Talks to /api with a bearer token. */
"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

/* ---- Airframe (vendored in /ui/airframe/): the helpers and behaviours come from airframe.js (window.airframe, loaded
   before this script): status, icon, meta, btn, avatar, toast, confirm, drawer. The console adds only what it needs on
   top: a bare mark, a word-caser, a readout. ---- */
const airframe = window.airframe;
// Airframe has six statuses, each color + shape + word: running (grey dot), queued (ring), approval (orange
// diamond), failed (red triangle), denied (red placard), off (small dot). Every console state maps to one of them;
// only approval, failed and denied carry color, so a screen with no color on it needs nobody.
const { status, btn, avatar } = airframe;
const ic = (name, size) => airframe.icon(name, { size });
const mark = (role) => `<span class="af-status af-status--${role}" aria-hidden="true"><i class="af-status__mark"></i></span>`;
const word = (s) => String(s || "").replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase());
// keyed inline data: [[key, value, modifier?]]; an item with no value is left out
const meta = (items) => airframe.meta(items.filter((x) => x && x[1] !== "" && x[1] != null));
// an error line: the failed mark and the words, never a red fill (Airframe's field error)
const errorLine = (text) => `<span class="af-field__error">${status("failed", text)}</span>`;
// an empty state: what is not here and what to do about it (Airframe EmptyState); `compact` is the one-row form
const emptyState = ({ kind = "no-data", icon, title, text = "", actions = "", compact = false, cls = "" }) =>
  `<div class="af-empty${compact ? " af-empty--compact" : ""}${cls ? ` ${cls}` : ""}" data-kind="${kind}">${icon ? ic(icon, compact ? undefined : 24) : ""}${compact
    ? `<div class="af-empty__body"><span class="af-empty__title">${esc(title)}</span>${text ? `<span class="af-empty__text">${text}</span>` : ""}</div>`
    : `<h3 class="af-empty__title">${esc(title)}</h3>${text ? `<p class="af-empty__text">${text}</p>` : ""}`}${actions
    ? `<div class="af-empty__act">${actions}</div>` : ""}</div>`;
// a readout: one unlit cell per digit or letter; punctuation sits between cells
const readout = (text, size = "m") => `<span class="af-readout af-readout--${size}">${[...String(text)].map((c) =>
  /[0-9a-z]/i.test(c) ? `<b>${esc(c)}</b>` : `<b class="af-readout__p">${esc(c)}</b>`).join("")}</span>`;
const tok = (name, el = document.documentElement) => getComputedStyle(el).getPropertyValue(`--af-${name}`).trim();

/* ---- Airframe Composer: the input a person steers an agent with. One send button, a queued row per message that
   waits for the agent's current tool call (with a take-back while it can still be taken back), and a disabled
   state that says why. `queued` items: {id, text, sub, takeBack}; `why` is the line under the box; `busy` draws
   the queued ring before it. ---- */
const queuedRow = (q) => `<div class="af-composer__queued" data-steer="${esc(q.id || "")}">
    <span class="af-lbl">Queued<span class="af-lbl__sub">${esc(q.sub || "applies after this tool call")}</span></span>
    <span class="af-composer__qmsg">${esc(q.text)}</span>
    ${q.takeBack ? btn("", { kind: "ghost", sm: true, icon: "close", attrs: { "data-take-back": q.id, "aria-label": "Take back the queued message" } }) : ""}
  </div>`;
function composerHtml({ id, placeholder, send = "Send", why = "", busy = false, disabled = false, queued = [], chips = [], note = "" }) {
  return `<div class="af-composer${disabled ? " is-disabled" : ""}" id="${esc(id)}">${queued.map(queuedRow).join("")}
    <div class="af-composer__box">
      <textarea class="af-composer__input" rows="1" placeholder="${esc(placeholder)}" aria-label="${esc(placeholder)}"${disabled ? " disabled" : ""}></textarea>
      <div class="af-composer__row">
        ${chips.length ? `<div class="af-composer__chips" role="group" aria-label="Suggestions">${chips.map((c) =>
          `<button type="button" class="af-chip af-chip--sm" data-chip${disabled ? " disabled" : ""}>${esc(c)}</button>`).join("")}</div>` : ""}
        ${note ? `<span class="af-diff__note">${ic("agent")}<span>${note}</span></span>` : ""}
        ${btn(send, { kind: "primary", sm: true, icon: "arrow-right", cls: "af-composer__send", disabled })}
      </div>
    </div>
    ${why ? `<p class="af-composer__why">${busy ? mark("queued") : ""}${why}</p>` : ""}
  </div>`;
}
// wires a composer: `onSend(text)` (async; the field clears on success), `onTakeBack(id)`; Ctrl/Cmd+Enter sends
function wireComposer(el, { onSend, onTakeBack, draft = "" }) {
  if (!el) return;
  const ta = $(".af-composer__input", el), sendBtn = $(".af-composer__send", el);
  ta.value = draft;
  if (!CSS.supports("field-sizing", "content")) {  // grow with the content where the browser doesn't do it itself
    const grow = () => { ta.style.height = "auto"; ta.style.height = `${Math.min(ta.scrollHeight, window.innerHeight * .4)}px`; };
    ta.addEventListener("input", grow); grow();
  }
  const send = () => {
    const text = ta.value.trim();
    if (!text || sendBtn.disabled) return;
    ta.value = "";  // cleared before the send, so a refresh mid-send doesn't carry the words back; restored if it fails
    const restore = () => { const cur = $(`#${CSS.escape(el.id)} .af-composer__input`) || ta; if (!cur.value) cur.value = text; };
    busy(sendBtn, async () => {
      try { if ((await onSend(text)) === false) restore(); }
      catch (e) { restore(); throw e; }
    });
  };
  sendBtn.addEventListener("click", send);
  ta.addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); send(); } });
  $$("[data-chip]", el).forEach((c) => c.addEventListener("click", () => { ta.value = c.textContent; ta.focus(); }));
  $$("[data-take-back]", el).forEach((b) => b.addEventListener("click", () => busy(b, () => onTakeBack(b.dataset.takeBack))));
}

/* ---- Airframe Question: an agent asks a person. The approval role (a person is needed now) with the word "Needs
   input"; the agent's options as chips when it offered any, free text otherwise (text wins); one primary, Answer.
   The answer goes back as the agent's next message, in place. ---- */
function questionHtml({ id, who, turn, question, context = "", options = [], asked, meanwhile = "holding the turn" }) {
  return `<article class="af-card af-card--lit af-question" id="${esc(id)}">
    <div class="af-card__top">
      <span class="af-lbl">${ic("agent")}Question<span class="af-lbl__sub">${esc(who)}</span>${turn ? `<span class="af-lbl__sub">turn ${turn}</span>` : ""}</span>
      ${status("approval", "Needs input", { lit: true })}
    </div>
    <p class="af-question__q">${esc(question)}</p>
    ${context ? `<p class="af-question__ctx">${context}</p>` : ""}
    ${options.length ? `<div class="af-question__opts" role="group" aria-label="Options">${options.slice(0, 5).map((o) =>
      `<button type="button" class="af-chip" aria-pressed="false" data-option>${esc(o)}</button>`).join("")}</div>` : ""}
    <label class="af-field af-question__free">
      <span class="af-field__label">${options.length ? "Or answer in your own words" : "Your answer"}</span>
      <span class="af-input af-input--area af-input--sans"><textarea rows="2" placeholder="${options.length ? "Free text dismisses the options" : "The agent continues with this"}"></textarea></span>
    </label>
    <div class="af-question__act">
      ${meta([["asked", asked ? ago(asked) : ""], ["while", meanwhile]])}
      ${btn("Answer", { kind: "primary", sm: true, attrs: { "data-answer": true } })}
    </div>
  </article>`;
}
// wires a question card: a pressed option is the answer unless the person wrote; `onAnswer(text)` sends it
function wireQuestion(el, onAnswer) {
  if (!el) return;
  const ta = $("textarea", el);
  $$("[data-option]", el).forEach((o) => o.addEventListener("click", () => {
    $$("[data-option]", el).forEach((x) => x.setAttribute("aria-pressed", String(x === o && x.getAttribute("aria-pressed") !== "true")));
  }));
  $("[data-answer]", el).addEventListener("click", (e) => {
    const picked = $('[data-option][aria-pressed="true"]', el);
    const text = ta.value.trim() || (picked ? picked.textContent : "");
    if (!text) { ta.focus(); return; }
    busy(e.currentTarget, () => onAnswer(text));
  });
}

const store = {
  get token() { try { return localStorage.getItem("hangar.token") || ""; } catch { return ""; } },
  set token(v) { try { v ? localStorage.setItem("hangar.token", v) : localStorage.removeItem("hangar.token"); } catch {} },
};
const state = { view: "fleet", envs: [], creds: [], harnesses: [], overview: null, sandboxes: [], selectedEnv: null,
  jobs: [], selectedJob: null, sessions: [], selectedSession: null };

async function api(method, path, body, { raw = false } = {}) {
  const opts = { method, headers: { Authorization: `Bearer ${store.token}` } };
  if (body !== undefined) {
    if (typeof body === "string") { opts.body = body; opts.headers["Content-Type"] = "application/yaml"; }
    else { opts.body = JSON.stringify(body); opts.headers["Content-Type"] = "application/json"; }
  }
  const r = await fetch(path, opts);
  if (r.status === 401) { showGate("That token was not accepted."); throw new Error("unauthorized"); }
  const data = raw ? r : await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || data.detail || `${r.status} ${r.statusText}`);
  return data;
}

// Airframe's toast: bottom centre, held while a dialog or drawer is open or a field has focus (alert inhibition), the
// failed mark for a bad outcome; `opts` adds an action ({label, onClick}) or `critical: true`.
const toast = (msg, bad = false, opts = {}) => airframe.toast(msg, { tone: bad ? "bad" : "plain", ...opts });
// Airframe's confirm on an af-dialog, in place of window.confirm(): the refusing button first, the verb last
const confirmAction = (title, body, ok, { danger = true, cancel = "Keep it" } = {}) => airframe.confirm({ title, body, ok, cancel, danger });

async function busy(btn, fn) {
  const label = btn?.innerHTML;  // buttons can carry an icon: keep the markup, not just the text
  if (btn) { btn.disabled = true; }
  try { return await fn(); }
  catch (e) { if (e.message !== "unauthorized") toast(e.message, true); }
  finally { if (btn) { btn.disabled = false; btn.innerHTML = label; } }
}

function ago(ts) {
  if (!ts) return "";
  const s = Math.round(Date.now() / 1000 - ts);
  if (s < 60) return `${s} s ago`;
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} d ago`;
}
function until(ts) {
  if (!ts) return "never";
  const s = Math.round(ts - Date.now() / 1000);
  if (s <= 0) return "now";
  if (s < 3600) return `in ${Math.round(s / 60)} min`;
  return `in ${(s / 3600).toFixed(1)} h`;
}

/* ------------------------------------------------------------------ gate */
function showGate(err) {
  $("#shell").hidden = true;
  $("#gate").hidden = false;
  $("#gate-error").hidden = !err;
  $("#gate-error").innerHTML = err ? errorLine(err) : "";
  $("#gate-token").focus();
}
$("#gate-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  store.token = $("#gate-token").value.trim();
  try { await api("GET", "/api/me"); boot(); } catch {}
});
$("#signout").addEventListener("click", () => { store.token = ""; showGate(); });

/* ------------------------------------------------------------------ routing */
function route() {
  const [v, q] = (location.hash || "#fleet").slice(1).split("?");
  const linked = new URLSearchParams(q || "").get("job");  // #jobs?job=<id>: the inbox's links (#157)
  if (v === "jobs" && linked) state.selectedJob = linked;
  state.view = ["jobs", "sessions", "fleet", "environments", "credentials", "snapshots", "connect", "activity", "usage"].includes(v) ? v : "fleet";
  $$(".view").forEach((s) => (s.hidden = s.id !== `view-${state.view}`));
  moreSheet(false);
  markCurrent();
  ({ jobs: loadJobs, sessions: loadSessions, fleet: loadFleet, environments: loadEnvs, credentials: loadCreds, snapshots: loadSnaps, connect: loadConnect,
     activity: loadAudit, usage: loadUsage })[state.view]();
}
window.addEventListener("hashchange", route);
// the current section, in the top bar, the bottom tab bar and the More sheet; on a phone the sections past the first
// three live under More, so the More tab is current for them, and while the sheet is open
function markCurrent() {
  const more = !$("#more-sheet").hidden;
  $$("[data-view]").forEach((a) => {
    const cur = a.dataset.view === state.view && !(more && a.closest("#tabbar"));
    if (cur) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
  });
  if (more || $(`#more-sheet [data-view="${state.view}"]`)) $("#tab-more").setAttribute("aria-current", "page");
  else $("#tab-more").removeAttribute("aria-current");
}
// a count beside a section's name, in the top bar and the bottom tab bar alike
const navCount = (name, n) => $$(`[data-count="${name}"]`).forEach((el) => (el.textContent = n || ""));
// the More sheet (narrow screens): the six sections the tab bar has no room for, and the providers' state
function moreSheet(open) {
  const sheet = $("#more-sheet"), tab = $("#tab-more");
  sheet.hidden = !open;
  tab.setAttribute("aria-expanded", String(open));
  markCurrent();
  if (open) $("#more-sheet [data-view]").focus();
}
$("#tab-more").addEventListener("click", () => moreSheet($("#more-sheet").hidden));
$("#more-sheet").addEventListener("click", (e) => { if (e.target.closest("[data-view]")) moreSheet(false); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !$("#more-sheet").hidden) { moreSheet(false); $("#tab-more").focus(); } });

let pollTimer = null;
async function boot() {
  $("#gate").hidden = true;
  $("#shell").hidden = false;
  await refreshMeta();
  route();
  clearInterval(pollTimer);
  pollTimer = setInterval(() => {
    if (document.hidden) return;
    if (state.view === "fleet") loadFleet(true);
    if (state.view === "jobs") loadJobs(true);
    if (state.view === "sessions") loadSessions(true);
  }, 4000);
  loadJobs(true);  // fills the nav counts
  loadSessions(true);
}

async function refreshMeta() {
  const [ov, envs, creds, harnesses] = await Promise.all([
    api("GET", "/api/overview"), api("GET", "/api/environments"), api("GET", "/api/credentials"), api("GET", "/api/harnesses"),
  ]);
  Object.assign(state, { overview: ov, envs, creds, harnesses });
  // a provider is quiet while it works (running) and a failed triangle when it doesn't
  const providers = Object.entries(ov.providers).map(([n, p]) =>
    `<span class="prov af-status af-status--${p.ok ? "running" : "failed"}" title="${esc(p.error || (p.capabilities?.microvm ? "microVM isolation" : ""))}">
       <i class="af-status__mark"></i>${esc(n)}${p.ok ? "" : " unavailable"}${n === ov.default_provider && Object.keys(ov.providers).length > 1
         ? `<span class="hint">default</span>` : ""}</span>`).join("");
  $("#providers").innerHTML = providers;
  $("#more-providers").innerHTML = `<span class="af-lbl">Providers</span>${providers}`;  // the top bar hides them on a phone
}

/* ------------------------------------------------------------------ fleet */
// The Fleet page (four views of the sandboxes, one selection, the detail panel and every sandbox action) is fleet.js,
// loaded after this file: loadFleet, renderFleet. The new-sandbox form, the dialogs and the terminals stay here.

/* ---- new sandbox form ---- */
function openNewForm(env) {
  const f = $("#new-form");
  f.environment.innerHTML = state.envs.filter((e) => !e.error).map((e) =>
    `<option value="${esc(e.name)}">${esc(e.name)}${Object.keys(e.images || {}).length ? "" : " (builds on first use)"}</option>`).join("");
  if (env) f.environment.value = env;
  f.provider.innerHTML = Object.entries(state.overview.providers).filter(([, p]) => p.ok)
    .map(([n]) => `<option ${n === state.overview.default_provider ? "selected" : ""}>${esc(n)}</option>`).join("");
  syncNewCreds();
  f.hidden = false;
  f.environment.focus();
}
function syncNewCreds() {
  const f = $("#new-form");
  const env = state.envs.find((e) => e.name === f.environment.value);
  const defaults = new Set(env?.credentials || []);
  $("#new-creds").innerHTML = state.creds.length ? state.creds.map((c) =>
    `<label class="chip"><input type="checkbox" value="${esc(c.name)}" ${defaults.has(c.name) ? "checked" : ""}
      ${c.exclusive && c.leased_to ? "disabled" : ""}>${esc(c.name)}
      <span class="hint">${esc(c.harness)}${c.exclusive && c.leased_to ? ", in use" : ""}</span></label>`).join("")
    : `<span class="hint">No credentials yet. Add them under <a href="#credentials">Credentials</a>.</span>`;
  if (env?.repo && !f.repo.value) f.repo.placeholder = `${env.repo} (environment default)`;
}
$("#btn-new").addEventListener("click", () => openNewForm());
$("#new-cancel").addEventListener("click", () => ($("#new-form").hidden = true));
$("#new-form").environment.addEventListener("change", syncNewCreds);
$("#new-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target, btn = $("button[type=submit]", f);
  const body = Object.fromEntries([...new FormData(f)].filter(([, v]) => v !== ""));
  body.count = Number(body.count || 1);
  if (body.ttl_minutes) body.ttl_minutes = Number(body.ttl_minutes);
  body.credentials = $$("#new-creds input:checked").map((i) => i.value);
  const label = btn.innerHTML;
  btn.textContent = body.count > 1 ? `Starting ${body.count}…` : "Starting…";
  await busy(btn, async () => {
    const res = await api("POST", "/api/sandboxes", body);
    const bad = res.filter((r) => r.error);
    toast(bad.length ? `${res.length - bad.length} started, ${bad.length} failed: ${bad[0].error}` : `Started ${res.map((r) => r.name).join(", ")}`, !!bad.length);
    f.hidden = true; f.reset();
    await refreshMeta(); await loadFleet();
  });
  btn.innerHTML = label;
});

/* ---- dialogs: Airframe's af-dialog (one short form that creates or runs one thing). The footer runs refuse, accept:
   the refusing button is first in focus order so Enter never destroys, the verb is on the confirming one. ---- */
const field = (label, control, hint = "") => `<label class="af-field"><span class="af-field__label">${esc(label)}</span>${control}${hint
  ? `<span class="af-field__hint">${esc(hint)}</span>` : ""}</label>`;
const inputWell = (html, { area = false, sans = false } = {}) => `<span class="af-input${area ? " af-input--area" : ""}${sans ? " af-input--sans" : ""}">${html}</span>`;

function dialog({ title, body, ok, cancel = "Cancel", wide = false }, onSubmit) {
  const d = $("#dialog"), f = $("#dialog-form");
  d.classList.toggle("af-dialog--wide", wide);
  f.innerHTML = `<header class="af-dialog__head"><h2 class="af-dialog__title" id="dialog-title">${title}</h2></header>
    <div class="af-dialog__body">${body}</div>
    <footer class="af-dialog__foot">${btn(cancel, { kind: "secondary", type: "submit", attrs: { value: "cancel", formnovalidate: true } })}${btn(ok, { kind: "primary", type: "submit", attrs: { value: "ok", id: "dialog-ok" } })}</footer>`;
  f.onsubmit = async (e) => {
    if (e.submitter?.value !== "ok") return;
    e.preventDefault();
    await busy($("#dialog-ok"), async () => { if ((await onSubmit(new FormData(f))) !== false) d.close(); });
  };
  d.showModal();
  ($("input, textarea, select", f) || $('[value="cancel"]', f))?.focus();
}

function agentDialog(sb) {
  const env = state.envs.find((e) => e.name === sb.environment);
  const hs = (env?.harnesses || []).filter((h) => h !== "git");
  if (!hs.length) return toast(`${sb.environment} has no agent harness installed. Add one to the environment.`, true);
  dialog({ title: `Run an agent in <span class="af-mono">${esc(sb.name)}</span>`, ok: "Start agent", body:
    field("Harness", `<select name="harness">${hs.map((h) => `<option>${esc(h)}</option>`).join("")}</select>`) +
    field("Task", inputWell(`<textarea name="prompt" rows="6" required placeholder="Fix the failing test in tests/test_api.py and commit the change."></textarea>`, { area: true, sans: true }),
      "Runs headless in its own terminal session. The terminal opens so you can watch it.") },
  async (fd) => {
    const r = await api("POST", `/api/sandboxes/${sb.id}/agent`, { prompt: fd.get("prompt"), harness: fd.get("harness") });
    toast(`Agent ${r.harness} started (${r.run_id})`);
    openTerminal(sb, r.terminal_session);
    loadFleet();
  });
}

function execDialog(sb) {
  dialog({ title: `Run a command in <span class="af-mono">${esc(sb.name)}</span>`, ok: "Run", cancel: "Close", body:
    field("Command", inputWell(`<input name="command" required placeholder="pytest -q">`)) + `<pre id="exec-out" class="log" hidden></pre>` },
  async (fd) => {
    const r = await api("POST", `/api/sandboxes/${sb.id}/exec`, { command: fd.get("command") });
    const out = $("#exec-out");
    out.hidden = false;
    out.textContent = `$ ${fd.get("command")}\n${r.stdout}${r.stderr ? "\n" + r.stderr : ""}\n[exit ${r.exit_code}, ${r.duration_ms} ms]`;
    return false;  // keep the dialog open to show output
  });
}

function forkDialog(sb) {
  dialog({ title: `Fork <span class="af-mono">${esc(sb.name)}</span>`, ok: "Fork", body:
    `<p>Snapshots this sandbox and starts copies with the same files, installed dependencies and repo state.
      Exclusive credentials stay with the original.</p>` +
    field("Copies", inputWell(`<input name="count" type="number" min="1" max="20" value="2">`)) +
    field("Work branch prefix", inputWell(`<input name="work_branch_prefix" placeholder="attempt">`), "Gives attempt-1, attempt-2, …") },
  async (fd) => {
    const res = await api("POST", `/api/sandboxes/${sb.id}/fork`, {
      count: Number(fd.get("count")), work_branch_prefix: fd.get("work_branch_prefix") || null });
    toast(`Forked into ${res.map((r) => r.name).join(", ")}`);
    loadFleet();
  });
}
/* ------------------------------------------------------------------ terminals */
const terms = new Map(); // key -> {term, ws, fit, el, tab, sb, session}
let activeTerm = null;

// The terminal drawer is Airframe's bottom drawer (no scrim: the page above stays usable); opening it lifts the toasts
// and pads the page through --af-drawer-inset. Its height is --af-drawer-h, which the grip resizes.
const showDrawer = () => airframe.drawer.open("#drawer", { scrim: false });
const hideDrawer = () => airframe.drawer.close("#drawer");

function openTerminal(sb, session = null, initial = null) {
  const key = `${sb.id}:${session || (initial ? "agent" : "shell")}`;
  showDrawer();
  if (terms.has(key)) return activateTerm(key);
  const el = document.createElement("div");
  el.className = "term";
  $("#drawer-body").appendChild(el);
  // xterm draws on a canvas and can't read CSS variables: hand it Airframe's terminal tokens (dark in both themes)
  const dk = (name) => tok(name, $("#drawer"));  // the drawer is a Night scope
  const term = new Terminal({
    fontFamily: dk("font-mono"), fontSize: 13, cursorBlink: true, scrollback: 10000,
    theme: { background: dk("terminal-bg"), foreground: dk("terminal-fg"), cursor: dk("brand"), cursorAccent: dk("terminal-bg"),
      selectionBackground: dk("line-strong"), red: dk("terminal-err"), brightRed: dk("terminal-err") },
  });
  const fit = new FitAddon.FitAddon();
  term.loadAddon(fit);
  term.open(el);
  // DM Mono may still be loading: re-measure the cells once it is in
  document.fonts?.load(`13px ${dk("font-mono")}`).then(() => { term.options.fontFamily = dk("font-mono"); fit.fit(); }).catch(() => {});
  const tab = document.createElement("button");
  tab.className = "tab";
  tab.setAttribute("role", "tab");
  tab.innerHTML = `${esc(sb.name)}<span class="hint">${session ? esc(session) : "shell"}</span><span class="x" title="Close" aria-label="Close">${ic("close")}</span>`;
  tab.addEventListener("click", (e) => { if (e.target.closest(".x")) closeTerm(key); else activateTerm(key); });
  const extra = $("#drawer-tabs .tab-extra");
  $("#drawer-tabs").insertBefore(tab, extra);
  const entry = { term, fit, el, tab, sb, session, ws: null, initial };
  terms.set(key, entry);
  activateTerm(key);
  connectTerm(entry);
}

async function ticket() {
  return (await api("POST", "/api/tickets")).ticket;
}

async function connectTerm(entry) {
  const { term, fit, sb, session } = entry;
  fit.fit();
  const proto = location.protocol === "https:" ? "wss" : "ws";
  let t;
  try { t = await ticket(); } catch (e) { term.writeln(`\r\n\x1b[31m${e.message}\x1b[0m`); return; }
  const q = new URLSearchParams({ ticket: t, cols: term.cols, rows: term.rows });
  if (session) q.set("session", session);
  const ws = new WebSocket(`${proto}://${location.host}/api/sandboxes/${sb.id}/attach?${q}`);
  ws.binaryType = "arraybuffer";
  entry.ws = ws;
  if (entry.initial) {  // e.g. resume the agent's conversation interactively in this shell
    const cmd = entry.initial;
    entry.initial = null;
    ws.addEventListener("open", () => setTimeout(() => ws.send(new TextEncoder().encode(cmd + "\r")), 400));
  }
  ws.onmessage = (ev) => {
    if (typeof ev.data === "string") {
      try { const m = JSON.parse(ev.data); if (m.type === "error") term.writeln(`\r\n\x1b[31m${m.message}\x1b[0m`); } catch { term.write(ev.data); }
    } else term.write(new Uint8Array(ev.data));
  };
  ws.onclose = () => term.writeln("\r\n\x1b[90m[disconnected; press Enter to reconnect]\x1b[0m");
  const enc = new TextEncoder();
  entry.dataSub?.dispose();
  entry.dataSub = term.onData((d) => {
    if (ws.readyState === WebSocket.OPEN) ws.send(enc.encode(d));
    else if (d === "\r" && ws.readyState === WebSocket.CLOSED) connectTerm(entry);
  });
  entry.resizeSub?.dispose();
  entry.resizeSub = term.onResize(({ cols, rows }) => {
    if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: "resize", cols, rows }));
  });
}

function activateTerm(key) {
  activeTerm = key;
  for (const [k, t] of terms) { t.el.hidden = k !== key; t.tab.classList.toggle("active", k === key); }
  const t = terms.get(key);
  requestAnimationFrame(() => { t.fit.fit(); t.term.focus(); });
}
function closeTerm(key) {
  const t = terms.get(key);
  if (!t) return;
  t.ws?.close(); t.term.dispose(); t.el.remove(); t.tab.remove(); terms.delete(key);
  if (!terms.size) hideDrawer();
  else if (activeTerm === key) activateTerm([...terms.keys()].at(-1));
}
function closeTerminalsFor(id) { for (const k of [...terms.keys()]) if (k.startsWith(id + ":")) closeTerm(k); }

(function initDrawer() {
  const tabs = $("#drawer-tabs");
  const extra = document.createElement("div");
  extra.className = "tab-extra";
  extra.innerHTML = `<button id="term-sessions" class="af-btn af-btn--ghost af-btn--sm" title="Attach to a terminal session an agent or MCP client is using">${ic("terminal")}Sessions…</button>
    <button id="term-hide" class="af-btn af-btn--ghost af-btn--sm">${ic("chevron-down")}Hide</button>`;
  tabs.appendChild(extra);
  $("#term-hide").addEventListener("click", hideDrawer);
  // Escape belongs to the program in the terminal (vim, less), not to the drawer
  $("#drawer-body").addEventListener("keydown", (e) => { if (e.key === "Escape") e.stopPropagation(); });
  $("#term-sessions").addEventListener("click", async () => {
    const t = terms.get(activeTerm);
    if (!t) return;
    const sessions = await api("GET", `/api/sandboxes/${t.sb.id}/terminals`).catch((e) => (toast(e.message, true), []));
    if (!sessions.length) return toast("No terminal sessions in this sandbox yet.");
    dialog({ title: `Attach to a session in <span class="af-mono">${esc(t.sb.name)}</span>`, ok: "Attach", body:
      `<p>You and the agent share the same terminal: you see what it types and can type back.</p>` +
      field("Session", `<select name="s">${sessions.map((s) => `<option>${esc(s)}</option>`).join("")}</select>`) },
    async (fd) => openTerminal(t.sb, fd.get("s")));
  });
  const grip = $("#drawer-grip");
  grip.addEventListener("pointerdown", (e) => {
    grip.setPointerCapture(e.pointerId);
    const move = (ev) => {
      const h = Math.min(Math.max(window.innerHeight - ev.clientY, 160), window.innerHeight - 120);
      document.documentElement.style.setProperty("--af-drawer-h", `${h}px`);
      terms.get(activeTerm)?.fit.fit();
    };
    grip.addEventListener("pointermove", move);
    grip.addEventListener("pointerup", () => grip.removeEventListener("pointermove", move), { once: true });
  });
  window.addEventListener("resize", () => terms.get(activeTerm)?.fit.fit());
})();

/* ------------------------------------------------------------------ jobs */
const JOB_ACTIVE = ["queued", "provisioning", "running", "verifying", "delivering"];
// job state -> Airframe role. Waiting on a person (an approval, the agent's question) is the orange diamond; a finished
// job is quiet (off), whether it succeeded or was cancelled; only a failure is red.
const JOB_STATE = { queued: "queued", provisioning: "queued", running: "running", verifying: "running", delivering: "running",
  succeeded: "off", failed: "failed", cancelled: "off", needs_input: "approval" };
JOB_STATE.awaiting_approval = "approval"; // workstream A: approve with `hangar job approve` or `/hangar approve`
const JOB_WORD = { needs_input: "Needs input", awaiting_approval: "Needs approval" };
const jobRole = (j) => JOB_STATE[j.state] || "off";
const jobWord = (j) => JOB_WORD[j.state] || word(j.state);

async function loadJobs(quiet = false) {
  try {
    state.jobs = await api("GET", "/api/jobs?limit=200");
    renderJobs();
  } catch (e) { if (!quiet && e.message !== "unauthorized") toast(e.message, true); }
}

function jobRepo(j) {
  return (j.repo || "").replace(/^https?:\/\/(www\.)?github\.com\//, "").replace(/\.git$/, "");
}

function renderJobs() {
  const active = state.jobs.filter((j) => j.active);
  navCount("jobs", active.length);
  if (state.view !== "jobs") return;
  const f = $("#jobs-filter").value;
  const shown = state.jobs.filter((j) => !f || (f === "active" ? j.active : j.state === f));
  const hidden = state.jobs.length - shown.length;
  $("#job-list").innerHTML = shown.length ? shown.map((j) => `<li><button data-job="${esc(j.id)}"
      class="job-row ${j.id === state.selectedJob ? "active" : ""} ${jobRole(j) === "approval" ? "is-attention" : ""}" data-tone="${jobRole(j)}">
      <span class="t">${esc(j.title)}</span>
      <span class="d">${status(jobRole(j), jobWord(j))}${meta([
        j.reason ? ["reason", j.reason.replace(/_/g, " "), jobRole(j) === "failed" ? "failed" : ""] : null,
        ["source", j.source], ["created", ago(j.created_at)], j.result?.pr_number ? ["pr", `#${j.result.pr_number}`] : null])}</span></button></li>`).join("")
    : `<li>${state.jobs.length
      ? emptyState({ kind: "after-action", icon: "job", title: `${hidden} job${hidden === 1 ? " is" : "s are"} hidden by the filter.`,
        actions: btn("Show all jobs", { sm: true, attrs: { "data-clear-filter": true } }) })
      : emptyState({ icon: "job", title: "No jobs yet.", text: `Submit one here, label a GitHub issue <code>hangar</code>, or call <code>submit_job</code> from an MCP client.` })}</li>`;
  if (state.selectedJob) renderJob(state.jobs.find((j) => j.id === state.selectedJob));
  else if (!$("#job-detail .af-empty")) $("#job-detail").innerHTML = emptyState({ compact: true, icon: "job", title: "No job selected.",
    text: "Choose one to see its timeline, result and sandbox." });
}
$("#jobs-filter").addEventListener("change", renderJobs);
$("#job-list").addEventListener("click", (e) => { if (e.target.closest("[data-clear-filter]")) { $("#jobs-filter").value = ""; renderJobs(); } });
$("#job-list").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-job]");
  if (!b) return;
  state.selectedJob = b.dataset.job;
  renderJobs();
  if (window.innerWidth < 860) $("#job-detail").scrollIntoView({ behavior: "smooth", block: "start" });
});

// What a running job's turn can take (GET /api/jobs/{id}/approvals: running, live, pending, decided), fetched while
// the job is active and selected; a change re-renders the detail. The job view itself doesn't say whether the turn is
// live (steerable) or one-shot.
const turnInfo = new Map();
async function fetchTurnInfo(j) {
  const cur = turnInfo.get(j.id);
  if (cur?.busy) return;
  turnInfo.set(j.id, { ...cur, busy: true });
  try {
    const v = await api("GET", `/api/jobs/${j.id}/approvals`);
    turnInfo.set(j.id, { ...v, busy: false });
    if ((cur?.live !== v.live || cur?.running !== v.running) && state.selectedJob === j.id) renderJob(state.jobs.find((x) => x.id === j.id));
  } catch { turnInfo.set(j.id, { ...cur, busy: false }); }
}
const inflight = new Map();  // job id -> a steer on its way (the direct route answers once the agent has it)

// the Composer's state for a job: steer while a live turn runs, follow up after, and say why when neither works
function jobComposer(j, info) {
  const live = !!j.sandbox_id;
  if (j.state === "awaiting_approval") return { disabled: true, why: "Waiting for approval. Approve or reject it from the Fleet page; a reply comes after." };
  if (j.active) {
    if (j.state === "running" && info?.running && !info.live) return { disabled: true,
      why: `${esc(j.harness || "These")} turns run one-shot and can't take a message mid-turn; wait for the turn, or cancel the job.` };
    if (j.state === "running" && (!info || info.running)) return { mode: "steer", placeholder: "Steer the agent", busy: true,
      why: "The agent is working; your message applies after this tool call." };
    return { disabled: true, why: `The job is ${esc(word(j.state).toLowerCase())}; a reply after it finishes runs another turn.` };
  }
  if (live || j.session?.checkpoints) return { mode: "follow-up", placeholder: "Reply to the agent",
    why: "Runs another turn in the same sandbox and branch; the PR updates." };
  return { disabled: true, why: `The sandbox was released with no checkpoint. Submit a new job; branch <span class="af-mono">${esc(j.branch)}</span> has the work.` };
}

function renderJob(j) {
  const box = $("#job-detail");
  if (!j) { box.innerHTML = emptyState({ kind: "after-action", compact: true, icon: "job", title: "That job is no longer listed.", text: "It may have been cleaned up, or the filter hides it." }); return; }
  // keep a half-typed message across the 4s refresh
  const draft = $("#job-composer .af-composer__input")?.value || $("#job-question textarea")?.value || "";
  const r = j.result || {};
  const info = turnInfo.get(j.id);
  if (j.active && j.state === "running") fetchTurnInfo(j);
  const question = j.state === "needs_input" && r.question;
  const cm = question ? null : jobComposer(j, info);
  const pending = inflight.get(j.id);
  const verify = j.verify ? (r.verify_exit_code == null ? `<span class="hint">not run yet</span>`
    : r.verify_exit_code === 0 ? `<span class="passed">${ic("check", 14)}passed</span>`
      : status("failed", `Failed, exit ${r.verify_exit_code}`)) : "";
  const turns = j.turns || [];
  const live = !!j.sandbox_id;
  const role = jobRole(j);
  box.classList.toggle("af-card--lit", role === "approval" && !question);  // lit only while it waits on a person; a question card lights itself
  box.innerHTML = `
    <div class="detail-head"><h3>${esc(j.title)}</h3>${status(role, jobWord(j), { lit: true })}
      ${j.reason ? meta([["reason", j.reason.replace(/_/g, " "), role === "failed" ? "failed" : ""]]) : ""}</div>
    <dl class="kv af-kv">
      <dt>Asked by</dt><dd>${esc(j.requester)} via ${esc(j.source)}${j.source_ref ? ` (${esc(j.source_ref.replace(/^github:/, ""))})` : ""}, ${ago(j.created_at)}</dd>
      <dt>Where</dt><dd>${meta([["env", j.environment, "strong"], ["harness", j.harness || "default agent"], ["model", j.model || ""]])}</dd>
      ${j.repo ? `<dt>Repo</dt><dd><span class="af-mono">${esc(jobRepo(j))}</span></dd>
        <dt>Branch</dt><dd><code>${esc(j.branch)}</code>${j.base_branch ? ` <span class="sub-val">from</span> <code>${esc(j.base_branch)}</code>` : ""}</dd>` : ""}
      ${r.pr_url ? `<dt>Pull request</dt><dd><a href="${esc(r.pr_url)}" target="_blank" rel="noopener" class="af-mono">#${esc(r.pr_number)}</a></dd>` : ""}
      ${j.verify ? `<dt>Check</dt><dd><code>${esc(j.verify)}</code> <span class="sub-val">${verify}</span></dd>` : ""}
      ${r.commits?.length ? `<dt>Commits</dt><dd>${r.commits.map((c) => `<div><code>${esc(c)}</code></div>`).join("")}</dd>` : ""}
      ${j.context?.length ? `<dt>Context</dt><dd>${j.context.map((c) => c.url
        ? `<div><a class="af-mono" href="${esc(c.url)}" target="_blank" rel="noopener">${esc(c.name)}</a></div>`
        : `<div><code>${esc(c.name)}</code><span class="sub-val">${c.size} B</span></div>`).join("")}</dd>` : ""}
      <dt>Sandbox</dt><dd>${live ? `<code>${esc(j.sandbox_id)}</code>` : `<span class="hint">released; a reply resumes it from the last checkpoint</span>`}</dd>
      ${j.session ? `<dt>Session</dt><dd class="row"><a href="#sessions" data-goto-ses="${esc(j.session_id)}" class="af-mono">${esc(j.session.name)}</a>
        ${status(sesRole(j.session), sesWord(j.session))}<span class="hint">${j.session.checkpoints} checkpoint(s)</span></dd>` : ""}
    </dl>
    <div class="env-bar">
      ${live && j.terminal_session ? `<button id="job-watch" type="button" class="af-btn ${j.active ? "af-btn--primary" : "af-btn--secondary"}">${ic("terminal")}${j.active ? "Watch the agent" : "Agent's terminal"}</button>` : ""}
      ${live ? `<button id="job-shell" type="button" class="af-btn af-btn--secondary">${ic("terminal")}Open a shell</button>` : ""}
      ${j.active ? `<button id="job-cancel" type="button" class="af-btn af-btn--danger">Cancel job</button>` : ""}
    </div>
    ${r.summary ? `<h4>Agent's summary</h4><pre class="log summary">${esc(r.summary)}</pre>` : ""}
    ${r.verify_tail && r.verify_exit_code ? `<h4>Check output</h4><pre class="log">${esc(r.verify_tail)}</pre>` : ""}
    ${question ? `<h4>The agent asks</h4>${questionHtml({ id: "job-question", who: j.id, turn: turns.length, question: r.question, asked: j.updated_at,
        meanwhile: live ? "holding the sandbox" : "the sandbox is released; your answer resumes it" })}`
      : `<h4>${cm.mode === "steer" ? "Steer the agent" : "Reply to the agent"}</h4>${composerHtml({ id: "job-composer", ...cm,
        queued: pending ? [{ id: "", text: pending, sub: "on its way; applies after this tool call" }] : [] })}`}
    ${activityBlock("jobs", j.id, j.usage)}
    <h4>Timeline</h4>
    <ol class="timeline">${(j.events || []).slice().reverse().map((e) => `<li data-tone="${JOB_STATE[e.state] || "off"}">${mark(JOB_STATE[e.state] || "off")}
      <span class="when" title="${new Date(e.ts * 1000).toLocaleString()}">${ago(e.ts)}</span>
      <span class="what">${esc(e.message)}</span></li>`).join("")}</ol>
    ${turns.length > 1 ? `<h4>Turns</h4><ol class="turns">${turns.map((t, i) => `<li><b>${i + 1}. ${esc(t.kind.replace("_", "-"))}</b>
      ${meta([["by", t.requester || ""], ["exit", t.exit_code ?? ""]])}
      ${t.message ? `<div class="hint">${esc(t.message)}</div>` : ""}</li>`).join("")}</ol>` : ""}
    <details><summary>Instructions</summary><pre class="log summary">${esc(j.instructions)}</pre></details>`;
  fillActivity(box);
  const sb = { id: j.sandbox_id, name: j.id };
  // the reply: an answer to the agent's question and a follow-up both run another turn; a steer lands in this one
  const followUp = async (message) => {
    await api("POST", `/api/jobs/${j.id}/follow-up`, { message });
    toast(`Sent; ${j.id} is running another turn`);
    await loadJobs();
  };
  wireQuestion($("#job-question"), (text) => { $("#job-question textarea").value = ""; return followUp(text); });
  if ($("#job-question textarea")) $("#job-question textarea").value = draft;
  wireComposer($("#job-composer"), { draft, onSend: async (text) => {
    if (cm.mode !== "steer") return followUp(text);
    // the direct route answers once the agent has the message: show it queued meanwhile (a refresh keeps the row)
    $("#job-composer").prepend(airframe.el(queuedRow({ text, sub: "on its way; applies after this tool call" })));
    inflight.set(j.id, text);
    try {
      await api("POST", `/api/jobs/${j.id}/steer`, { message: text });
      toast("The agent has your message; it applies after this tool call.");
    } catch (e) {
      if (e.message !== "unauthorized") toast(e.message, true);
      return false;  // not taken: the words come back to the box
    } finally { inflight.delete(j.id); renderJob(j); }
    await loadJobs();
  } });
  $("#job-watch")?.addEventListener("click", () => openTerminal(sb, j.terminal_session));
  $("#job-shell")?.addEventListener("click", () => openTerminal(sb));
  $("[data-goto-ses]", box)?.addEventListener("click", (e) => {
    state.selectedSession = e.target.dataset.gotoSes;
    $("#ses-jobs").checked = true;
  });
  $("#job-cancel")?.addEventListener("click", async (e) => {
    const b = e.currentTarget;
    if (!(await confirmAction(`Cancel ${j.id}?`, "The agent is stopped. Work committed so far stays in the sandbox and on the branch.", "Cancel job", { cancel: "Keep running" }))) return;
    busy(b, async () => { await api("POST", `/api/jobs/${j.id}/cancel`); toast(`Cancelling ${j.id}`); loadJobs(); });
  });
}

/* ------------------------------------------------------------------ sessions */
// session state -> Airframe role: a live session is quiet (running or idle), one the agent asked a question in waits
// on a person (orange diamond), a saved one is off; only a failed boot is red.
const SES_TONE = { starting: "queued", idle: "running", running: "running", suspending: "off", suspended: "off",
  failed: "failed", archived: "off" };
const sesRole = (x) => (x.needs_input && x.state !== "running" ? "approval" : SES_TONE[x.state] || "off");
const sesWord = (x) => (x.needs_input && x.state !== "running" ? "Needs input" : word(x.state));

async function loadSessions(quiet = false) {
  try {
    state.sessions = await api("GET", "/api/sessions?limit=200");
    renderSessions();
  } catch (e) { if (!quiet && e.message !== "unauthorized") toast(e.message, true); }
}

function renderSessions() {
  const showJobs = $("#ses-jobs").checked;
  const mine = state.sessions.filter((x) => showJobs || !x.job_id);
  navCount("sessions", mine.filter((x) => ["idle", "running", "starting"].includes(x.state)).length);
  if (state.view !== "sessions") return;
  $("#session-list").innerHTML = mine.length ? mine.map((x) => `<li><button data-ses="${esc(x.id)}"
      class="job-row ${x.id === state.selectedSession ? "active" : ""} ${sesRole(x) === "approval" ? "is-attention" : ""}" data-tone="${sesRole(x)}">
      <span class="t af-mono">${esc(x.name)}</span>
      <span class="d">${status(sesRole(x), `${sesWord(x)}${x.busy && x.state !== "running" ? "…" : ""}`)}${meta([
        ["env", x.environment], ["active", ago(x.last_active_at)]])}</span></button></li>`).join("")
    : `<li>${emptyState({ icon: "session", title: "No sessions yet.", text: `Start one here, or from an MCP client with <code>new_session</code>.` })}</li>`;
  if (state.selectedSession) renderSession(state.sessions.find((x) => x.id === state.selectedSession));
  else if (!$("#session-detail .af-empty")) $("#session-detail").innerHTML = emptyState({ compact: true, icon: "session", title: "No session selected.",
    text: "Choose one to resume it, talk to its agent, or open its terminal." });
}
$("#ses-jobs").addEventListener("change", renderSessions);
$("#session-list").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-ses]");
  if (!b) return;
  state.selectedSession = b.dataset.ses;
  renderSessions();
  if (window.innerWidth < 860) $("#session-detail").scrollIntoView({ behavior: "smooth", block: "start" });
});

// the Composer's state for a session: a send to a busy live session queues and applies after the tool call (with a
// take-back while it is on its way); an idle or suspended one starts a turn; archived and job sessions say why not
function sesComposer(x) {
  const t = x.turns.at(-1);
  if (x.state === "archived") return { disabled: true, why: "This session is archived. Its conversation is kept; start a new session to continue the work." };
  if (x.job_id) return { disabled: true, why: `This session carries job <span class="af-mono">${esc(x.job_id)}</span>; reply on the job, whose turns are verified and delivered.` };
  if (x.busy || x.state === "running") {
    if (t && t.run_id && !t.live && !t.finished_at) return { disabled: true,
      why: `${esc(x.harness || "These")} turns run one-shot and can't take a message mid-turn; wait for the turn to end.` };
    const steers = (t?.steers || []).filter((st) => st.status === "sending");
    const failed = (t?.steers || []).filter((st) => st.status === "failed" && st.ts > Date.now() / 1000 - 600).at(-1);
    return { placeholder: "Steer the agent", busy: !failed, queued: steers.map((st) => ({ id: st.id, text: st.text, takeBack: true })),
      why: failed ? `${mark("failed")}Not delivered: ${esc(failed.error || "the agent did not take the message")}. Send it again.`
        : "The agent is busy; your message will queue and apply after this tool call." };
  }
  if (x.state === "suspended") return { placeholder: "What next?", why: "Resumes the session, then the agent continues with this." };
  return { placeholder: "What next?", why: "The agent continues its own conversation with everything it knew." };
}

function renderSession(x) {
  const box = $("#session-detail");
  if (!x) { box.innerHTML = emptyState({ kind: "after-action", compact: true, icon: "session", title: "That session is no longer listed." }); return; }
  const draft = $("#ses-composer .af-composer__input")?.value || $("#ses-question textarea")?.value || "";
  const live = !!x.sandbox_id && ["idle", "running"].includes(x.state);
  const ck = x.checkpoints?.at(-1);
  const keep = x.idle_minutes;
  const role = sesRole(x);
  const question = x.needs_input && role === "approval" && !x.job_id;
  const cm = question ? null : sesComposer(x);
  const form = !cm?.disabled || question;  // the message carries the panel's one primary action
  box.classList.toggle("af-card--lit", role === "approval" && !question);  // a question card lights itself
  box.innerHTML = `
    <div class="detail-head"><h3 class="af-mono">${esc(x.name)}</h3>${status(role, sesWord(x), { lit: true })}</div>
    ${x.reason ? (role === "failed" ? `<p class="detail-reason">${errorLine(x.reason)}</p>` : `<p class="hint">${esc(x.reason)}</p>`) : ""}
    ${question ? questionHtml({ id: "ses-question", who: x.name, turn: x.turns.length, question: x.needs_input, asked: x.last_active_at,
      meanwhile: live ? "holding the sandbox" : "the sandbox is released; your answer resumes it" }) : ""}
    <dl class="kv af-kv">
      <dt>Where</dt><dd>${meta([["env", x.environment, "strong"], ["harness", x.harness || "default agent"], ["model", x.model || ""]])}</dd>
      ${x.repo ? `<dt>Repo</dt><dd><span class="af-mono">${esc(jobRepo(x))}</span></dd><dt>Branch</dt><dd><code>${esc(x.branch)}</code></dd>` : ""}
      <dt>Conversation</dt><dd>${x.harness_session ? `<code>${esc(x.harness_session)}</code> <span class="sub-val">resumes natively</span>`
        : `<span class="hint">${x.turns.length ? "summary handoff on the next turn" : "starts with the first message"}</span>`}</dd>
      <dt>Saved</dt><dd>${x.checkpoint_count ? `${x.checkpoint_count} checkpoint(s), last ${ago(x.last_checkpoint_at)}
        <span class="sub-val">${esc(ck?.reason || "")}</span>${ck?.warnings?.length ? ` ${meta([["warnings", ck.warnings.join("; "), "failed"]])}` : ""}`
        : `<span class="hint">nothing yet</span>`}</dd>
      <dt>Idle</dt><dd><select id="ses-idle">${[[15, "suspend after 15 min idle"], [30, "after 30 min"],
        [120, "after 2 h"], [480, "after 8 h"], [0, "stay up until I suspend it"]].map(([v, t]) =>
        `<option value="${v}" ${v === keep ? "selected" : ""}>${t}</option>`).join("")}${[15, 30, 120, 480, 0].includes(keep)
        ? "" : `<option value="${keep}" selected>after ${keep} min</option>`}</select></dd>
      ${x.job_id ? `<dt>Job</dt><dd><a href="#jobs" data-goto-job="${esc(x.job_id)}" class="af-mono">${esc(x.job_id)}</a></dd>` : ""}
    </dl>
    <div class="env-bar">
      ${live ? "" : (x.state === "archived" ? "" : `<button id="ses-open" type="button" class="af-btn ${form ? "af-btn--secondary" : "af-btn--primary"}">${ic("play")}${x.checkpoint_count ? "Resume" : "Open"}</button>`)}
      ${live && x.resume_command && x.state !== "running" ? `<button id="ses-agent" type="button" class="af-btn af-btn--secondary">${ic("agent")}Talk to the agent here</button>` : ""}
      ${live && x.terminal_session ? `<button id="ses-watch" type="button" class="af-btn af-btn--secondary">${ic("terminal")}Last turn's terminal</button>` : ""}
      ${live ? `<button id="ses-shell" type="button" class="af-btn af-btn--secondary">${ic("terminal")}Open a shell</button><button id="ses-suspend" type="button" class="af-btn af-btn--ghost">${ic("spindown")}Suspend</button>` : ""}
    </div>
    ${x.last_summary ? `<h4>Agent's last summary</h4><pre class="log summary">${esc(x.last_summary)}</pre>` : ""}
    ${question ? "" : `<h4>${cm.busy || cm.queued?.length ? "Steer the agent" : "Message the agent"}</h4>${composerHtml({ id: "ses-composer", ...cm })}`}
    ${x.turns.length ? activityBlock("sessions", x.id, x.usage) : ""}
    ${x.turns.length ? `<h4>Turns</h4><ol class="turns">${x.turns.slice().reverse().map((t) => `<li>
      <b>${esc(t.message.split("\n")[0].slice(0, 140))}</b>
      ${meta([["harness", t.harness || ""], ["mode", t.mode], t.exit_code != null ? ["exit", t.exit_code] : t.run_id ? ["exit", "running"] : null,
        ["started", t.started_at ? ago(t.started_at) : ""]])}
      ${t.summary ? `<div class="turn-summary">${esc(t.summary.slice(0, 600))}</div>` : ""}</li>`).join("")}</ol>` : ""}`;
  fillActivity(box);
  const sb = { id: x.sandbox_id, name: x.name };
  // one route for all of it: a send starts a turn, or steers the running one (queued server-side, with a take-back)
  const send = async (message) => {
    await api("POST", `/api/sessions/${x.id}/send`, { message });
    toast(x.state === "suspended" ? `Resuming ${x.name}, then sending` : x.busy || x.state === "running" ? "Queued; it applies after this tool call" : "Sent");
    await loadSessions();
  };
  wireQuestion($("#ses-question"), (text) => { $("#ses-question textarea").value = ""; return send(text); });
  if ($("#ses-question textarea")) $("#ses-question textarea").value = draft;
  wireComposer($("#ses-composer"), { draft, onSend: send, onTakeBack: async (id) => {
    await api("POST", `/api/sessions/${x.id}/steers/${id}/withdraw`);
    toast("Taken back; the agent never saw it.");
    await loadSessions();
  } });
  $("#ses-open")?.addEventListener("click", (e) => busy(e.currentTarget, async () => {
    await api("POST", `/api/sessions/${x.id}/open`, {}); toast(`Resuming ${x.name}`); loadSessions(); }));
  $("#ses-shell")?.addEventListener("click", () => openTerminal(sb));
  $("#ses-watch")?.addEventListener("click", () => openTerminal(sb, x.terminal_session));
  $("#ses-agent")?.addEventListener("click", () => openTerminal(sb, null, x.resume_command));
  $("#ses-suspend")?.addEventListener("click", (e) => busy(e.currentTarget, async () => {
    await api("POST", `/api/sessions/${x.id}/suspend`); toast(`Suspending ${x.name}: saving, then releasing the sandbox`);
    closeTerminalsFor(x.sandbox_id); loadSessions(); }));
  $("#ses-idle")?.addEventListener("change", (e) => busy(e.currentTarget, async () => {
    await api("POST", `/api/sessions/${x.id}/idle`, { idle_minutes: Number(e.target.value) }); loadSessions(); }));
  $("[data-goto-job]", box)?.addEventListener("click", (e) => { state.selectedJob = e.target.dataset.gotoJob; });
}

/* ------------------------------------------------------------------ activity (workstream C) */
// What the agent did, from /api/{jobs,sessions}/<id>/events: the newest events, fetched incrementally by seq.
const ACT_LABEL = { message: "said", tool_call: "tool", tool_result: "result", command: "ran", file_change: "changed",
  error: "error", progress: "progress" };
// quiet marks for what the agent did; only an error event carries color
const ACT_TONE = { error: "failed" };
const activity = new Map(); // "jobs:<id>" -> { next, events, busy }

function usageMeta(u) {
  if (!u) return "";
  const k = (n) => (n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n));
  const tokens = u.input_tokens + u.output_tokens + u.cache_write_tokens;
  return meta([["tokens", k(tokens)], ["cached", u.cache_read_tokens ? k(u.cache_read_tokens) : ""],
    ["cost", u.cost_usd == null ? "" : `$${u.cost_usd.toFixed(2)}`]]);
}

function activityBlock(kind, id, usage) {
  return `<h4 class="with-meta">Activity${usageMeta(usage)}</h4>
    <ol class="timeline activity" data-activity="${kind}/${esc(id)}"></ol>`;
}

function drawActivity(el, st) {
  const shown = st.events.filter((e) => e.kind !== "usage").slice(-40).reverse();
  el.innerHTML = shown.length ? shown.map((e) => `<li data-tone="${ACT_TONE[e.kind] || "off"}">${mark(ACT_TONE[e.kind] || "off")}
      <span class="when" title="${new Date(e.ts * 1000).toLocaleString()}">${ago(e.ts)}</span>
      <span class="what"><b>${esc(ACT_LABEL[e.kind] || e.kind)}</b> ${esc(e.text)}</span></li>`).join("")
    : `<li class="hint">${st.loaded ? "No activity recorded." : "Loading…"}</li>`;
}

async function fillActivity(box) {
  const el = $("[data-activity]", box);
  if (!el) return;
  const key = el.dataset.activity;
  const st = activity.get(key) || { next: 0, events: [], loaded: false };
  activity.set(key, st);
  drawActivity(el, st);
  if (st.busy) return;
  st.busy = true;
  try {
    const q = st.loaded ? `since=${st.next}&limit=500` : "tail=100";
    const r = await api("GET", `/api/${key}/events?${q}`);
    st.events = st.events.concat(r.events).slice(-100);
    st.next = r.next || st.next;
    st.loaded = true;
    const cur = $(`[data-activity="${CSS.escape(key)}"]`);
    if (cur) drawActivity(cur, st);
  } catch { /* the next refresh retries */ } finally { st.busy = false; }
}

function syncSessionHarness() {
  const f = $("#session-form");
  const env = state.envs.find((e) => e.name === f.environment.value);
  const hs = (env?.harnesses || []).filter((h) => h !== "git");
  f.harness.innerHTML = `<option value="">${esc(env?.default_harness || "default")} (default)</option>` +
    hs.filter((h) => h !== env?.default_harness).map((h) => `<option>${esc(h)}</option>`).join("");
}
$("#btn-new-session").addEventListener("click", () => {
  const f = $("#session-form");
  const envs = state.envs.filter((e) => !e.error && (e.harnesses || []).some((h) => h !== "git"))
    .sort((a, b) => (!!b.repo - !!a.repo) || ((b.credentials || []).length - (a.credentials || []).length));
  f.environment.innerHTML = envs.map((e) => `<option value="${esc(e.name)}">${esc(e.name)}${e.repo ? ` (${esc(jobRepo(e))})` : ""}</option>`).join("");
  syncSessionHarness();
  f.hidden = false;
  f.name.focus();
});
$("#session-form").environment.addEventListener("change", syncSessionHarness);
$("#session-cancel").addEventListener("click", () => ($("#session-form").hidden = true));
$("#session-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target;
  await busy($("button[type=submit]", f), async () => {
    const body = Object.fromEntries([...new FormData(f)].filter(([, v]) => v !== ""));
    const x = await api("POST", "/api/sessions", body);
    toast(`Starting ${x.name}`);
    f.hidden = true; f.reset();
    state.selectedSession = x.id;
    await loadSessions();
  });
});

/* ---- new job ---- */
function syncJobHarness() {
  const f = $("#job-form");
  const env = state.envs.find((e) => e.name === f.environment.value);
  const hs = (env?.harnesses || []).filter((h) => h !== "git");
  f.harness.innerHTML = `<option value="">${esc(env?.default_harness || "default")} (default)</option>` +
    hs.filter((h) => h !== env?.default_harness).map((h) => `<option>${esc(h)}</option>`).join("");
  f.repo.placeholder = env?.repo ? `${env.repo} (environment default)` : "https://github.com/org/repo.git";
}
$("#btn-new-job").addEventListener("click", () => {
  const f = $("#job-form");
  // Environments with a repo and credentials first: those are the ones set up for jobs.
  const agentEnvs = state.envs.filter((e) => !e.error && (e.harnesses || []).some((h) => h !== "git"))
    .sort((a, b) => (!!b.repo - !!a.repo) || ((b.credentials || []).length - (a.credentials || []).length));
  f.environment.innerHTML = agentEnvs.map((e) => `<option value="${esc(e.name)}">${esc(e.name)}${e.repo ? ` (${esc(jobRepo(e))})` : ""}</option>`).join("");
  syncJobHarness();
  f.hidden = false;
  f.instructions.focus();
});
$("#job-form").environment.addEventListener("change", syncJobHarness);
$("#job-cancel-form").addEventListener("click", () => ($("#job-form").hidden = true));
$("#job-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target;
  await busy($("button[type=submit]", f), async () => {
    const body = Object.fromEntries([...new FormData(f)].filter(([k, v]) => v !== "" && k !== "links"));
    if (body.timeout_minutes) body.timeout_minutes = Number(body.timeout_minutes);
    const context = [];
    for (const file of $("#job-files").files) {
      const buf = new Uint8Array(await file.arrayBuffer());
      let text = null;
      try { text = new TextDecoder("utf-8", { fatal: true }).decode(buf); } catch {}
      if (text !== null) context.push({ name: file.name, kind: "file", content: text });
      else {
        let bin = "";
        for (let i = 0; i < buf.length; i += 0x8000) bin += String.fromCharCode(...buf.subarray(i, i + 0x8000));
        context.push({ name: file.name, kind: "file", content: btoa(bin), encoding: "base64" });
      }
    }
    f.links.value.split(/\s+/).filter(Boolean).forEach((u, i) => context.push({ name: `link-${i + 1}`, kind: "url", url: u }));
    body.context = context;
    const j = await api("POST", "/api/jobs", body);
    toast(`Queued ${j.id}`);
    f.hidden = true; f.reset();
    state.selectedJob = j.id;
    await loadJobs();
  });
});

/* ------------------------------------------------------------------ environments */
async function loadEnvs() {
  state.envs = await api("GET", "/api/environments");
  $("#env-list").innerHTML = state.envs.map((e) => `<li><button data-env="${esc(e.name)}" class="${e.name === state.selectedEnv ? "active" : ""}">
      <span class="t">${esc(e.name)}</span>
      <span class="d">${e.error ? errorLine(e.error) : esc(e.description || (e.harnesses || []).join(", "))}</span></button></li>`).join("")
    || `<li>${emptyState({ icon: "environment", title: "No environments yet.", text: "Create one from a base; it builds the first time a sandbox needs it." })}</li>`;
  if (state.selectedEnv) showEnv(state.selectedEnv);
  else if (!$("#env-detail .af-empty")) $("#env-detail").innerHTML = emptyState({ compact: true, icon: "environment", title: "No environment selected.",
    text: "Choose one to edit or build it." });
}
$("#env-list").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-env]");
  if (!b) return;
  state.selectedEnv = b.dataset.env;
  $$("#env-list button").forEach((x) => x.classList.toggle("active", x === b));
  showEnv(b.dataset.env);
});

async function showEnv(name) {
  const box = $("#env-detail");
  const d = await api("GET", `/api/environments/${encodeURIComponent(name)}`).catch((e) => (toast(e.message, true), null));
  if (!d) return;
  const env = state.envs.find((e) => e.name === name) || {};
  const s = d.spec;
  const images = Object.entries(env.images || {});
  const provs = Object.entries(state.overview.providers).filter(([, p]) => p.ok).map(([n]) => n);
  box.innerHTML = `
    <div class="detail-head"><h3 class="af-mono">${esc(name)}</h3>${meta([["extends", s.extends || ""], ["warm pool", s.warm_pool || ""]])}</div>
    ${meta([
      ["harnesses", (s.harnesses || []).join(", "), "strong"],
      ...Object.entries(s.tools?.mise || {}).map(([k, v]) => [k, v]),
      s.repo ? ["repo", `${s.repo.url}${s.repo.bake ? " (baked)" : ""}`] : null,
      ["cpu", s.resources.cpus], ["memory", `${s.resources.memory_gb} GB`]])}
    <p class="hint">${images.length ? "Built: " + images.map(([p, i]) => `${esc(p)} <code>${esc(i.split(":").pop())}</code>`).join(", ")
      : "Not built yet. It builds automatically the first time a sandbox needs it."}
      Current digest <code>${esc(d.digest)}</code>.</p>
    <label>Spec (YAML)<textarea id="env-yaml" spellcheck="false">${esc(d.yaml)}</textarea></label>
    <div class="env-bar">
      <button id="env-save" type="button" class="af-btn af-btn--secondary">Save</button>
      <select id="env-prov" aria-label="Provider">${provs.map((p) => `<option>${esc(p)}</option>`).join("")}</select>
      <label class="check"><input type="checkbox" id="env-force"> Rebuild from scratch</label>
      <button class="af-btn af-btn--primary" id="env-build" type="button">Build</button>
      <button id="env-start" type="button" class="af-btn af-btn--secondary">${ic("sandbox")}Start sandbox</button>
      <button class="af-btn af-btn--ghost" id="env-df" type="button">Show Dockerfile</button>
    </div>
    <pre id="env-dockerfile" hidden>${esc(d.dockerfile)}</pre>
    <pre id="env-log" class="log" hidden></pre>`;
  $("#env-save").onclick = (e) => busy(e.currentTarget, async () => {
    await api("PUT", `/api/environments/${encodeURIComponent(name)}`, $("#env-yaml").value);
    toast(`Saved ${name}`); await loadEnvs();
  });
  $("#env-df").onclick = () => { const p = $("#env-dockerfile"); p.hidden = !p.hidden; };
  $("#env-start").onclick = () => { location.hash = "#fleet"; setTimeout(() => openNewForm(name), 50); };
  $("#env-build").onclick = (e) => busy(e.currentTarget, async () => {
    const job = await api("POST", `/api/environments/${encodeURIComponent(name)}/build`,
      { provider: $("#env-prov").value, force: $("#env-force").checked });
    followBuild(job);
  });
}

async function followBuild(job) {
  const log = $("#env-log");
  log.hidden = false;
  log.textContent = "";
  const es = new EventSource(`/api/builds/${job.id}/stream?ticket=${encodeURIComponent(await ticket())}`);
  es.onmessage = (ev) => {
    const atBottom = log.scrollTop + log.clientHeight >= log.scrollHeight - 20;
    log.textContent += JSON.parse(ev.data) + "\n";
    if (atBottom) log.scrollTop = log.scrollHeight;
  };
  es.addEventListener("done", (ev) => {
    const s = JSON.parse(ev.data);
    es.close();
    toast(s.state === "succeeded" ? `Built ${s.environment}` : `Build failed: ${s.error?.split("\n")[0]}`, s.state !== "succeeded");
    refreshMeta().then(loadEnvs);
  });
}

$("#btn-new-env").addEventListener("click", () => {
  dialog({ title: "New environment", ok: "Create", body:
    field("Name", inputWell(`<input name="name" required pattern="[a-z0-9-]+" placeholder="my-service">`)) +
    field("Start from", `<select name="extends">${state.envs.filter((e) => !e.error).map((e) => `<option>${esc(e.name)}</option>`).join("")}</select>`) +
    field("Repo (optional)", inputWell(`<input name="repo" placeholder="https://github.com/org/repo.git">`)) +
    field("Setup command after clone (optional)", inputWell(`<input name="setup" placeholder="uv sync">`)) },
  async (fd) => {
    const name = fd.get("name");
    const spec = { name, extends: fd.get("extends"), description: "" };
    if (fd.get("repo")) spec.repo = { url: fd.get("repo"), ...(fd.get("setup") ? { setup: fd.get("setup") } : {}) };
    // JSON is valid YAML, and avoids quoting bugs from string-built YAML.
    await api("PUT", `/api/environments/${encodeURIComponent(name)}`, JSON.stringify(spec, null, 2));
    state.selectedEnv = name;
    await loadEnvs();
    toast(`Created ${name}. Adjust the spec, then build it.`);
  });
});

/* ------------------------------------------------------------------ credentials */
async function loadCreds() {
  state.creds = await api("GET", "/api/credentials");
  $("#cred-table tbody").innerHTML = state.creds.length ? state.creds.map((c) => `<tr>
      <td><span class="af-table__id">${esc(c.name)}</span>${c.description ? `<div class="muted">${esc(c.description)}</div>` : ""}</td>
      <td class="af-table__mono">${esc(c.harness)}</td>
      <td>${esc(c.kind.replace("_", " "))}<div class="muted af-mono">${esc(c.env_var || c.file_path || "")}</div></td>
      <td>${c.exclusive ? (c.leased_to ? `<span class="af-tag">exclusive</span> ${meta([["held by", c.leased_to]])}`
        : `<span class="af-tag">exclusive</span> <span class="muted">free</span>`) : `<span class="af-tag">shared</span>`}</td>
      <td class="act">${c.leased_to ? `<button type="button" class="af-btn af-btn--secondary af-btn--sm" data-release="${esc(c.name)}" title="Use only if the holding sandbox is gone">Release</button>` : ""}
        <button type="button" class="af-btn af-btn--ghost af-btn--sm" data-del="${esc(c.name)}">Delete</button></td></tr>`).join("")
    : `<tr><td colspan="5">${emptyState({ compact: true, icon: "secret", title: "No credentials yet.", text: "Add an API key on the right, or sign in with a subscription below." })}</td></tr>`;
  $("#login-buttons").innerHTML = state.harnesses.filter((h) => h.logins.length).map((h) =>
    `<button type="button" class="af-btn af-btn--secondary" data-login="${esc(h.name)}" title="${esc(h.logins[0].notes)}">${ic("user")}Sign in to ${esc(h.title)}</button>`).join("");
  const f = $("#cred-form");
  f.harness.innerHTML = state.harnesses.map((h) => `<option value="${esc(h.name)}">${esc(h.title)}</option>`).join("");
  syncCredKinds();
}
function syncCredKinds() {
  const f = $("#cred-form");
  const h = state.harnesses.find((x) => x.name === f.harness.value);
  f.kind.innerHTML = (h?.credential_kinds || []).map((k) => `<option value="${esc(k.kind)}">${esc(k.kind.replace("_", " "))}</option>`).join("");
  syncSlot();
}
function syncSlot() {
  const f = $("#cred-form");
  const h = state.harnesses.find((x) => x.name === f.harness.value);
  const k = h?.credential_kinds.find((x) => x.kind === f.kind.value);
  $("#cred-slot").textContent = k ? `Injected as ${k.env_var ? `$${k.env_var}` : `~/${k.file_path}`}` +
    (k.exclusive ? ". Exclusive: refresh tokens rotate, so it is lent to one sandbox at a time and saved back when that sandbox stops." : ".") : "";
}
$("#cred-form").harness.addEventListener("change", syncCredKinds);
$("#cred-form").kind.addEventListener("change", syncSlot);
$("#cred-file").addEventListener("change", async (e) => {
  const file = e.target.files[0];
  if (file) $("#cred-form").value.value = await file.text();
});
$("#cred-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target;
  await busy($("button[type=submit]", f), async () => {
    const body = Object.fromEntries(new FormData(f));
    if (!body.description) delete body.description;
    const c = await api("POST", "/api/credentials", body);
    toast(`Saved ${c.name}`); f.reset(); await loadCreds(); syncCredKinds();
  });
});
$("#cred-table").addEventListener("click", async (e) => {
  const del = e.target.closest("[data-del]"), rel = e.target.closest("[data-release]");
  if (del && await confirmAction(`Delete credential ${del.dataset.del}?`, "Running sandboxes keep their copy until they stop; new ones can't use it.", "Delete")) {
    await busy(del, async () => { await api("DELETE", `/api/credentials/${encodeURIComponent(del.dataset.del)}`); toast(`Deleted ${del.dataset.del}`); loadCreds(); });
  }
  if (rel) await busy(rel, async () => { await api("POST", `/api/credentials/${encodeURIComponent(rel.dataset.release)}/release`); toast("Released"); loadCreds(); });
});
$("#login-buttons").addEventListener("click", (e) => {
  const b = e.target.closest("[data-login]");
  if (!b) return;
  const h = state.harnesses.find((x) => x.name === b.dataset.login);
  dialog({ title: `Sign in to ${esc(h.title)}`, ok: "Start sign-in", body:
    `<p>${esc(h.logins[0].notes)}</p>` +
    field("Save as credential", inputWell(`<input name="credential" required value="${esc(h.name)}-login">`)) },
  async (fd) => {
    const lg = await api("POST", "/api/logins", { harness: h.name, credential: fd.get("credential") });
    const sb = { id: lg.sandbox, name: `sign-in ${h.name}` };
    openTerminal(sb, lg.session);
    showLoginBar(lg);
  });
});
function showLoginBar(lg) {
  setTimeout(() => dialog({ title: "Finish signing in", ok: "Capture credential", cancel: "Not yet", body:
    `<p>Complete the flow in the terminal below (open the link, paste the code back). When it says you
      are signed in, capture the credential. The sign-in sandbox is destroyed afterwards.</p>` },
  async () => {
    const c = await api("POST", `/api/logins/${lg.id}/complete`);
    toast(`Saved ${c.name}`);
    closeTerminalsFor(lg.sandbox);
    loadCreds();
  }), 400);
}

/* ------------------------------------------------------------------ snapshots */
async function loadSnaps() {
  const snaps = await api("GET", "/api/snapshots");
  $("#snap-table tbody").innerHTML = snaps.length ? snaps.map((s) => `<tr>
      <td><span class="af-table__id">${esc(s.id)}</span>${s.label ? `<div class="muted">${esc(s.label)}</div>` : ""}</td>
      <td class="af-table__mono">${esc(s.sandbox_id)}</td><td class="af-table__mono">${esc(s.environment)}</td>
      <td><span class="af-tag">${esc(s.kind)}</span><span class="af-table__sub">${esc(s.provider)}</span></td>
      <td class="af-table__mono">${ago(s.created_at)}</td>
      <td class="act"><button type="button" class="af-btn af-btn--secondary af-btn--sm" data-fork="${esc(s.id)}">${ic("fork")}Start copy</button>
        <button type="button" class="af-btn af-btn--ghost af-btn--sm" data-sdel="${esc(s.id)}">Delete</button></td></tr>`).join("")
    : `<tr><td colspan="6">${emptyState({ compact: true, icon: "snapshot", title: "No snapshots yet.", text: "Snapshot a running sandbox from the fleet, or fork one." })}</td></tr>`;
}
$("#snap-table").addEventListener("click", async (e) => {
  const f = e.target.closest("[data-fork]"), d = e.target.closest("[data-sdel]");
  if (f) await busy(f, async () => { const r = await api("POST", `/api/snapshots/${f.dataset.fork}/fork`, { count: 1 }); toast(`Started ${r[0].name}`); });
  if (d && await confirmAction(`Delete snapshot ${d.dataset.sdel}?`, "It can't be forked or restored afterwards. Sandboxes started from it keep running.", "Delete")) {
    await busy(d, async () => { await api("DELETE", `/api/snapshots/${d.dataset.sdel}`); toast(`Deleted ${d.dataset.sdel}`); loadSnaps(); });
  }
});

/* ------------------------------------------------------------------ connect */
async function loadConnect() {
  const c = await api("GET", "/api/connect");
  const block = (title, body, note = "") => `<div class="snippet"><h3>${esc(title)}</h3><pre>${esc(body)}</pre>
    ${note ? `<p class="hint">${esc(note)}</p>` : ""}<button type="button" class="af-btn af-btn--ghost af-btn--sm" data-copy="${esc(body)}">${ic("copy")}Copy</button></div>`;
  $("#connect-snippets").innerHTML =
    block("MCP endpoint", c.mcp_url) +
    block("Codex (~/.codex/config.toml)", c.codex_config_toml, "Then export HANGAR_TOKEN in the shell Codex runs from.") +
    block("Codex CLI", c.codex_cli) +
    block("Claude Code", c.claude_code_cli) +
    `<div class="snippet"><h3>ChatGPT and other OAuth clients</h3><p class="${c.oauth ? "sub" : "hint"}">${esc(c.chatgpt)}</p></div>`;
  loadTokens();
  loadOAuth();
}
async function loadOAuth() {
  const apps = (await api("GET", "/api/oauth/clients")).filter((a) => a.approved_by || a.active);
  $("#oauth-table tbody").innerHTML = apps.length ? apps.map((a) => `<tr>
    <td><span class="af-table__id">${esc(a.client_name)}</span><div class="muted af-mono">${esc((a.redirect_uris[0] || "").replace(/^https?:\/\//, "").split("/")[0])}</div></td>
    <td class="muted">${a.active ? `approved by ${esc(a.approved_by || "?")}, ${ago(a.last_issued)}` : "no active session"}</td>
    <td class="act"><button type="button" class="af-btn af-btn--ghost af-btn--sm" data-oauth-revoke="${esc(a.client_id)}">Revoke</button></td></tr>`).join("")
    : `<tr><td>${emptyState({ compact: true, title: "No apps connected with OAuth yet." })}</td></tr>`;
}
$("#oauth-table").addEventListener("click", async (e) => {
  const b = e.target.closest("[data-oauth-revoke]");
  if (b && await confirmAction("Revoke this app?", "It loses access immediately and must be approved again to connect.", "Revoke", { cancel: "Keep access" })) {
    await busy(b, async () => { await api("DELETE", `/api/oauth/clients/${encodeURIComponent(b.dataset.oauthRevoke)}`); toast("Revoked"); loadOAuth(); });
  }
});
$("#connect-snippets").addEventListener("click", (e) => {
  const b = e.target.closest("[data-copy]");
  if (b) navigator.clipboard.writeText(b.dataset.copy).then(() => toast("Copied"));
});
async function loadTokens() {
  const toks = await api("GET", "/api/tokens");
  $("#token-table tbody").innerHTML = toks.map((t) => `<tr><td><span class="af-table__id">${esc(t.name)}</span><div class="muted af-mono">${esc(t.id)}</div></td>
    <td class="muted">${t.last_used ? "used " + ago(t.last_used) : "never used"}</td>
    <td class="act"><button type="button" class="af-btn af-btn--ghost af-btn--sm" data-revoke="${esc(t.id)}">Revoke</button></td></tr>`).join("");
}
$("#token-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  await busy($("button", e.target), async () => {
    const t = await api("POST", "/api/tokens", { name: e.target.name.value });
    const box = $("#token-new");
    box.hidden = false;
    box.innerHTML = `<span class="af-lbl">${ic("secret")}Token for ${esc(t.name)}<span class="af-lbl__sub">shown once</span></span>
      <code>${esc(t.token)}</code><button type="button" class="af-btn af-btn--secondary af-btn--sm" data-copy-token>${ic("copy")}Copy</button>`;
    $("[data-copy-token]", box).onclick = () => navigator.clipboard.writeText(t.token).then(() => toast("Copied"));
    e.target.reset(); loadTokens();
  });
});
$("#token-table").addEventListener("click", async (e) => {
  const b = e.target.closest("[data-revoke]");
  if (b && await confirmAction("Revoke this token?", "Clients using it stop working immediately. Create a new one to reconnect them.", "Revoke", { cancel: "Keep it" })) {
    await busy(b, async () => { await api("DELETE", `/api/tokens/${b.dataset.revoke}`); toast("Revoked"); loadTokens(); });
  }
});

/* ------------------------------------------------------------------ activity */
async function loadAudit() {
  const ev = await api("GET", "/api/audit?limit=300");
  $("#audit-table tbody").innerHTML = ev.map((a) => `<tr>
    <td class="af-table__mono" title="${new Date(a.ts * 1000).toLocaleString()}">${ago(a.ts)}</td>
    <td>${esc(a.actor)}</td><td class="af-table__mono">${esc(a.action)}</td><td class="af-table__mono">${esc(a.target || "")}</td>
    ${((d) => `<td class="af-table__mono clip" title="${esc(d)}">${esc(d.slice(0, 160))}</td>`)(Object.entries(a.detail).map(([k, v]) => `${k}=${typeof v === "object" ? JSON.stringify(v) : v}`).join("  "))}</tr>`).join("")
    || `<tr><td colspan="5">${emptyState({ compact: true, icon: "log", title: "Nothing recorded yet.", text: "Every action by a person or an MCP client lands here." })}</td></tr>`;
}

/* ------------------------------------------------------------------ usage (--- observe) */
const usd = (v) => (v >= 100 ? `$${Math.round(v).toLocaleString()}` : `$${v.toFixed(2)}`);
const kTok = (n) => (n >= 1e6 ? `${(n / 1e6).toFixed(1)}M` : n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n));

async function loadUsage() {
  const since = $("#usage-range").value;
  const q = (g, limit) => api("GET", `/api/usage?group_by=${g}&since=${since}&limit=${limit}`);
  try {
    const [days, repos, people] = await Promise.all([q("day", 200), q("repo", 10), q("requester", 10)]);
    const t = days.total;
    // summary cards: one readout each, a keyed foot (Airframe SummaryCards)
    const range = `${since.replace(/\D/g, "")} d`;
    $("#usage-tiles").innerHTML = [
      ["cost", "Spend", usd(t.cost_usd), t.unpriced_turns ? `+ ${t.unpriced_turns} turn(s) with no known price` : "list prices", "USD"],
      ["model", "Tokens", kTok(t.tokens), t.cache_read_tokens ? `+ ${kTok(t.cache_read_tokens)} cache reads` : "no cache reads", "in and out"],
      ["agent", "Agent turns", String(t.turns), "jobs and sessions", ""],
      ["policy", "Budget stops", String(t.budget_stops), t.budget_stops ? "turns stopped over their budget" : "none in range", ""],
    ].map(([icon, label, value, note, scope]) => `<article class="af-card tile">
      <div class="af-card__top"><span class="af-lbl">${ic(icon)}${esc(label)}</span><span class="af-card__meta">${esc(range)}</span></div>
      <div class="af-card__value" aria-label="${esc(`${label}: ${value}`)}">${readout(value)}</div>
      <div class="af-card__foot"><span>${esc(note)}</span><span>${esc(scope)}</span></div></article>`).join("");
    drawSpend(days.rows, days.since);
    const rows = (sel, data) => {
      $(`${sel} tbody`).innerHTML = data.rows.length ? data.rows.map((r) => `<tr><td class="af-table__mono">${esc(r.key)}</td>
        <td class="af-num af-table__mono">${r.turns}</td><td class="af-num af-table__mono">${kTok(r.tokens)}</td>
        <td class="af-num af-table__mono" title="${r.unpriced_turns ? `${r.unpriced_turns} turn(s) with no known price` : ""}">${usd(r.cost_usd)}${r.unpriced_turns ? " +" : ""}</td></tr>`).join("")
        : `<tr><td colspan="4">${emptyState({ compact: true, title: "No agent turns in this range." })}</td></tr>`;
    };
    rows("#usage-repos", repos);
    rows("#usage-requesters", people);
    $("#usage-stops tbody").innerHTML = days.budget_stops.length ? days.budget_stops.map((b) => `<tr>
      <td class="af-table__id">${esc(b.job || b.session || "")}</td><td class="af-table__mono">${esc(b.repo)}</td><td>${esc(b.requester || "")}</td>
      <td>${esc(b.reason || "")}</td>
      <td class="af-table__mono" title="${new Date(b.ts * 1000).toLocaleString()}">${ago(b.ts)}</td></tr>`).join("")
      : `<tr><td colspan="5">${emptyState({ compact: true, title: "No turn was stopped over its budget in this range." })}</td></tr>`;
  } catch (e) { if (e.message !== "unauthorized") toast(e.message, true); }
}
$("#usage-range").addEventListener("change", () => loadUsage());

function drawSpend(rows, since) {
  // One bar per UTC day of the range, empty days included; hover or focus shows the day's numbers.
  const byDay = new Map(rows.map((r) => [r.key, r]));
  const days = [];
  for (let d = Math.floor(since / 86400) * 86400000; d <= Date.now(); d += 86400000) {
    const key = new Date(d).toISOString().slice(0, 10);
    days.push(byDay.get(key) || { key, cost_usd: 0, tokens: 0, turns: 0, budget_stops: 0 });
  }
  const max = Math.max(0, ...days.map((d) => d.cost_usd));
  const box = $("#usage-days");
  if (!max) { box.innerHTML = emptyState({ kind: "after-action", compact: true, icon: "cost", title: "No priced spend in this range.", text: "Turns on a model with no known price count their tokens only." }); return; }
  // greyscale bars; a day with a budget stop is the one attention series (chart-5), "look here"
  const tipText = (d) => `${d.key}: ${usd(d.cost_usd)}, ${kTok(d.tokens)} tokens, ${d.turns} turn(s)`
    + (d.budget_stops ? `, ${d.budget_stops} budget stop(s)` : "");
  const tipHtml = (d) => `<b>${esc(d.key)}</b> ${meta([["spend", usd(d.cost_usd), "strong"], ["tokens", kTok(d.tokens)], ["turns", d.turns],
    d.budget_stops ? ["budget stops", d.budget_stops] : null])}`;
  const stops = days.some((d) => d.budget_stops);
  box.innerHTML = `<div class="bars-axis"><span>${usd(max)}</span><span>$0</span></div><div class="bars-plot">${
    days.map((d, i) => `<div class="bar-slot" tabindex="0" aria-label="${esc(tipText(d))}" data-i="${i}">
      <div class="bar${d.budget_stops ? " stop" : ""}" style="height:${(100 * d.cost_usd / max).toFixed(2)}%"></div></div>`).join("")}</div>
    <div class="bars-x"><span>${esc(days[0].key)}</span><span>${esc(days[days.length - 1].key)}</span></div>
    ${stops ? `<div class="af-viz-key"><span class="af-viz-key__item"><i class="key-stop"></i>Day with a budget stop</span></div>` : ""}
    <div class="bar-tip" hidden></div>`;
  const tip = $(".bar-tip", box);
  const show = (el) => {
    tip.innerHTML = tipHtml(days[Number(el.dataset.i)]);
    tip.hidden = false;
    const r = el.getBoundingClientRect(), b = box.getBoundingClientRect();
    const left = r.left - b.left + r.width / 2 - tip.offsetWidth / 2;
    tip.style.left = `${Math.min(Math.max(0, left), b.width - tip.offsetWidth)}px`;
  };
  $$(".bar-slot", box).forEach((el) => {
    el.addEventListener("mouseenter", () => show(el));
    el.addEventListener("focus", () => show(el));
    el.addEventListener("mouseleave", () => (tip.hidden = true));
    el.addEventListener("blur", () => (tip.hidden = true));
  });
}

/* ------------------------------------------------------------------ start */
if (store.token) api("GET", "/api/me").then(boot).catch(() => showGate());
else showGate();
