/* Hangar dashboard. Vanilla JS, no build step. Talks to /api with a bearer token. */
"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const store = {
  get token() { try { return localStorage.getItem("hangar.token") || ""; } catch { return ""; } },
  set token(v) { try { v ? localStorage.setItem("hangar.token", v) : localStorage.removeItem("hangar.token"); } catch {} },
};
const state = { view: "fleet", envs: [], creds: [], harnesses: [], overview: null, sandboxes: [], selectedEnv: null };

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

let toastTimer;
function toast(msg, bad = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.toggle("bad", bad);
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (t.hidden = true), bad ? 7000 : 3500);
}

async function busy(btn, fn) {
  const label = btn?.textContent;
  if (btn) { btn.disabled = true; }
  try { return await fn(); }
  catch (e) { if (e.message !== "unauthorized") toast(e.message, true); }
  finally { if (btn) { btn.disabled = false; btn.textContent = label; } }
}

function ago(ts) {
  if (!ts) return "";
  const s = Math.round(Date.now() / 1000 - ts);
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 86400) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
}
function until(ts) {
  if (!ts) return "never";
  const s = Math.round(ts - Date.now() / 1000);
  if (s <= 0) return "now";
  if (s < 3600) return `in ${Math.round(s / 60)}m`;
  return `in ${(s / 3600).toFixed(1)}h`;
}

/* ------------------------------------------------------------------ gate */
function showGate(err) {
  $("#shell").hidden = true;
  $("#gate").hidden = false;
  $("#gate-error").hidden = !err;
  $("#gate-error").textContent = err || "";
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
  const v = (location.hash || "#fleet").slice(1);
  state.view = ["fleet", "environments", "credentials", "snapshots", "connect", "activity"].includes(v) ? v : "fleet";
  $$(".view").forEach((s) => (s.hidden = s.id !== `view-${state.view}`));
  $$(".rail a[data-view]").forEach((a) => a.classList.toggle("active", a.dataset.view === state.view));
  ({ fleet: loadFleet, environments: loadEnvs, credentials: loadCreds, snapshots: loadSnaps, connect: loadConnect,
     activity: loadAudit })[state.view]();
}
window.addEventListener("hashchange", route);

let pollTimer = null;
async function boot() {
  $("#gate").hidden = true;
  $("#shell").hidden = false;
  await refreshMeta();
  route();
  clearInterval(pollTimer);
  pollTimer = setInterval(() => { if (state.view === "fleet" && !document.hidden) loadFleet(true); }, 4000);
}

async function refreshMeta() {
  const [ov, envs, creds, harnesses] = await Promise.all([
    api("GET", "/api/overview"), api("GET", "/api/environments"), api("GET", "/api/credentials"), api("GET", "/api/harnesses"),
  ]);
  Object.assign(state, { overview: ov, envs, creds, harnesses });
  $("#providers").innerHTML = Object.entries(ov.providers).map(([n, p]) =>
    `<div class="prov" title="${esc(p.error || (p.capabilities?.microvm ? "microVM isolation" : ""))}">
       <span class="dot ${p.ok ? "ok" : "bad"}"></span>${esc(n)}${n === ov.default_provider ? " (default)" : ""}</div>`).join("");
}

/* ------------------------------------------------------------------ fleet */
async function loadFleet(quiet = false) {
  try {
    const sbs = await api("GET", `/api/sandboxes?include_stopped=${$("#show-stopped").checked}`);
    state.sandboxes = sbs;
    renderFleet();
  } catch (e) { if (!quiet && e.message !== "unauthorized") toast(e.message, true); }
}
$("#show-stopped").addEventListener("change", () => loadFleet());

function renderFleet() {
  const sbs = state.sandboxes;
  const active = sbs.filter((s) => ["running", "paused", "starting", "pending"].includes(s.state));
  $("#nav-count").textContent = active.length || "";
  const ov = state.overview;
  $("#fleet-sub").textContent = active.length
    ? `${active.filter((s) => s.state === "running").length} running, ${active.filter((s) => s.state === "paused").length} paused, ` +
      `${ov ? ov.max_sandboxes - active.length : "?"} bays free`
    : "No sandboxes running.";
  const bays = $("#bays");
  if (!sbs.length) {
    bays.innerHTML = `<div class="empty-floor"><p>The floor is empty. Start a sandbox from one of your
      ${state.envs.length} environments, or ask your MCP client to create one.</p>
      <button class="primary" data-act="new">New sandbox</button></div>`;
    return;
  }
  bays.innerHTML = sbs.map(bayHtml).join("");
}

function bayHtml(s) {
  const running = s.state === "running", paused = s.state === "paused";
  const live = running || paused;
  const branch = s.info?.work_branch || s.branch;
  const runs = (s.info?.runs || []).slice(-4);
  return `<article class="bay" data-state="${esc(s.state)}" data-id="${esc(s.id)}">
    <div><div class="bay-name">${esc(s.name)}</div><div class="bay-id">${esc(s.id)}${s.parent ? ` · forked from ${esc(s.parent)}` : ""}</div></div>
    <div class="bay-meta">
      <div class="state"><span class="dot"></span>${esc(s.state)}${s.info?.warm ? " (warm start)" : ""}</div>
      <div>${esc(s.environment)} on <b>${esc(s.provider)}</b></div>
    </div>
    <div class="bay-meta">
      ${s.repo ? `<div title="${esc(s.repo)}"><b>${esc(s.repo.replace(/^https?:\/\/(www\.)?github\.com\//, "").replace(/\.git$/, ""))}</b></div>` : "<div class='hint'>no repo</div>"}
      ${branch ? `<div>${esc(branch)}</div>` : ""}
      <div class="hint">${live ? `expires ${until(s.expires_at)}` : `created ${ago(s.created_at)}`}${s.credentials.length ? ` · ${s.credentials.length} credential${s.credentials.length > 1 ? "s" : ""}` : ""}</div>
    </div>
    <div class="bay-actions">
      ${running ? `<button data-act="term">Terminal</button><button data-act="agent">Run agent</button>
        <button data-act="exec">Run command</button><button data-act="fork">Fork</button>
        <button data-act="snapshot">Snapshot</button><button data-act="pause">Pause</button>` : ""}
      ${paused ? `<button data-act="resume">Resume</button>` : ""}
      ${live ? `<button data-act="extend" title="Add one hour">+1h</button>` : ""}
      ${s.state !== "stopped" ? `<button class="danger" data-act="destroy">Destroy</button>` : ""}
    </div>
    ${s.error ? `<p class="bay-err">${esc(s.error)}</p>` : ""}
    ${runs.length && running ? `<div class="bay-runs"><span class="hint">Agent runs:</span>${runs.map((r) =>
      `<button data-act="run" data-run="${esc(r.id)}" title="${esc(r.prompt)}">${esc(r.harness)} · ${ago(r.started_at)}</button>`).join("")}</div>` : ""}
  </article>`;
}

$("#bays").addEventListener("click", async (e) => {
  const btn = e.target.closest("button[data-act]");
  if (!btn) return;
  if (btn.dataset.act === "new") return openNewForm();
  const id = btn.closest(".bay").dataset.id;
  const sb = state.sandboxes.find((s) => s.id === id);
  const act = btn.dataset.act;
  if (act === "term") return openTerminal(sb);
  if (act === "run") return openTerminal(sb, btn.dataset.run);
  if (act === "agent") return agentDialog(sb);
  if (act === "exec") return execDialog(sb);
  if (act === "fork") return forkDialog(sb);
  await busy(btn, async () => {
    if (act === "destroy") {
      if (!confirm(`Destroy ${sb.name}? Its filesystem is deleted unless you snapshot it first.`)) return;
      await api("DELETE", `/api/sandboxes/${id}`); toast(`Destroyed ${sb.name}`); closeTerminalsFor(id);
    } else if (act === "snapshot") {
      const s = await api("POST", `/api/sandboxes/${id}/snapshot`, { kind: "auto" }); toast(`Snapshot ${s.id} taken (${s.kind})`);
    } else if (act === "pause") { await api("POST", `/api/sandboxes/${id}/pause`); toast(`Paused ${sb.name}`); }
    else if (act === "resume") { await api("POST", `/api/sandboxes/${id}/resume`); toast(`Resumed ${sb.name}`); }
    else if (act === "extend") { await api("POST", `/api/sandboxes/${id}/extend?minutes=60`); toast(`${sb.name} extended by 1h`); }
    await loadFleet();
  });
});

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
  btn.textContent = body.count > 1 ? `Starting ${body.count}…` : "Starting…";
  await busy(btn, async () => {
    const res = await api("POST", "/api/sandboxes", body);
    const bad = res.filter((r) => r.error);
    toast(bad.length ? `${res.length - bad.length} started, ${bad.length} failed: ${bad[0].error}` : `Started ${res.map((r) => r.name).join(", ")}`, !!bad.length);
    f.hidden = true; f.reset();
    await refreshMeta(); await loadFleet();
  });
});

/* ---- dialogs ---- */
function dialog(html, onSubmit) {
  const d = $("#dialog"), f = $("#dialog-form");
  f.innerHTML = html + `<div class="form-foot"><button value="cancel" formnovalidate class="ghost">Cancel</button>
    <button value="ok" class="primary" id="dialog-ok"></button></div>`;
  $("#dialog-ok").textContent = f.dataset.ok || "OK";
  f.onsubmit = async (e) => {
    if (e.submitter?.value !== "ok") return;
    e.preventDefault();
    await busy($("#dialog-ok"), async () => { if ((await onSubmit(new FormData(f))) !== false) d.close(); });
  };
  d.showModal();
  $("input, textarea, select", f)?.focus();
}

function agentDialog(sb) {
  const env = state.envs.find((e) => e.name === sb.environment);
  const hs = (env?.harnesses || []).filter((h) => h !== "git");
  if (!hs.length) return toast(`${sb.environment} has no agent harness installed. Add one to the environment.`, true);
  $("#dialog-form").dataset.ok = "Start agent";
  dialog(`<h3>Run an agent in ${esc(sb.name)}</h3>
    <label>Harness<select name="harness">${hs.map((h) => `<option>${esc(h)}</option>`).join("")}</select></label>
    <label>Task<textarea name="prompt" rows="6" required placeholder="Fix the failing test in tests/test_api.py and commit the change."></textarea></label>
    <p class="hint">Runs headless in its own terminal session. The terminal opens so you can watch it.</p>`,
  async (fd) => {
    const r = await api("POST", `/api/sandboxes/${sb.id}/agent`, { prompt: fd.get("prompt"), harness: fd.get("harness") });
    toast(`Agent ${r.harness} started (${r.run_id})`);
    openTerminal(sb, r.terminal_session);
    loadFleet();
  });
}

function execDialog(sb) {
  $("#dialog-form").dataset.ok = "Run";
  dialog(`<h3>Run a command in ${esc(sb.name)}</h3>
    <label>Command<input name="command" required placeholder="pytest -q"></label>
    <pre id="exec-out" class="log" hidden></pre>`,
  async (fd) => {
    const r = await api("POST", `/api/sandboxes/${sb.id}/exec`, { command: fd.get("command") });
    const out = $("#exec-out");
    out.hidden = false;
    out.textContent = `$ ${fd.get("command")}\n${r.stdout}${r.stderr ? "\n" + r.stderr : ""}\n[exit ${r.exit_code}, ${r.duration_ms} ms]`;
    return false;  // keep the dialog open to show output
  });
}

function forkDialog(sb) {
  $("#dialog-form").dataset.ok = "Fork";
  dialog(`<h3>Fork ${esc(sb.name)}</h3>
    <p class="sub">Snapshots this sandbox and starts copies with the same files, installed dependencies and repo state.
      Exclusive credentials stay with the original.</p>
    <label>Copies<input name="count" type="number" min="1" max="20" value="2"></label>
    <label>Work branch prefix<input name="work_branch_prefix" placeholder="attempt (gives attempt-1, attempt-2, …)"></label>`,
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

function openTerminal(sb, session = null) {
  const key = `${sb.id}:${session || "shell"}`;
  $("#drawer").hidden = false;
  document.body.style.setProperty("--drawer-pad", "var(--drawer-h)");
  if (terms.has(key)) return activateTerm(key);
  const el = document.createElement("div");
  el.className = "term";
  $("#drawer-body").appendChild(el);
  const term = new Terminal({
    fontFamily: '"JetBrains Mono", ui-monospace, monospace', fontSize: 13, cursorBlink: true, scrollback: 10000,
    theme: { background: "#12181f", foreground: "#e8ecef", cursor: "#f2c230", selectionBackground: "#3a4857" },
  });
  const fit = new FitAddon.FitAddon();
  term.loadAddon(fit);
  term.open(el);
  const tab = document.createElement("button");
  tab.className = "tab";
  tab.setAttribute("role", "tab");
  tab.innerHTML = `${esc(sb.name)}<span class="hint">${session ? esc(session) : "shell"}</span><span class="x" title="Close">✕</span>`;
  tab.addEventListener("click", (e) => { if (e.target.classList.contains("x")) closeTerm(key); else activateTerm(key); });
  const extra = $("#drawer-tabs .tab-extra");
  $("#drawer-tabs").insertBefore(tab, extra);
  const entry = { term, fit, el, tab, sb, session, ws: null };
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
  if (!terms.size) { $("#drawer").hidden = true; document.body.style.removeProperty("--drawer-pad"); }
  else if (activeTerm === key) activateTerm([...terms.keys()].at(-1));
}
function closeTerminalsFor(id) { for (const k of [...terms.keys()]) if (k.startsWith(id + ":")) closeTerm(k); }

(function initDrawer() {
  const tabs = $("#drawer-tabs");
  const extra = document.createElement("div");
  extra.className = "tab-extra";
  extra.innerHTML = `<button id="term-sessions" title="Attach to a terminal session an agent or MCP client is using">Sessions…</button>
    <button id="term-hide">Hide</button>`;
  tabs.appendChild(extra);
  $("#term-hide").addEventListener("click", () => { $("#drawer").hidden = true; document.body.style.removeProperty("--drawer-pad"); });
  $("#term-sessions").addEventListener("click", async () => {
    const t = terms.get(activeTerm);
    if (!t) return;
    const sessions = await api("GET", `/api/sandboxes/${t.sb.id}/terminals`).catch((e) => (toast(e.message, true), []));
    if (!sessions.length) return toast("No terminal sessions in this sandbox yet.");
    $("#dialog-form").dataset.ok = "Attach";
    dialog(`<h3>Attach to a session in ${esc(t.sb.name)}</h3>
      <p class="sub">You and the agent share the same terminal: you see what it types and can type back.</p>
      <label>Session<select name="s">${sessions.map((s) => `<option>${esc(s)}</option>`).join("")}</select></label>`,
    async (fd) => openTerminal(t.sb, fd.get("s")));
  });
  const grip = $("#drawer-grip");
  grip.addEventListener("pointerdown", (e) => {
    grip.setPointerCapture(e.pointerId);
    const move = (ev) => {
      const h = Math.min(Math.max(window.innerHeight - ev.clientY, 160), window.innerHeight - 120);
      document.documentElement.style.setProperty("--drawer-h", `${h}px`);
      terms.get(activeTerm)?.fit.fit();
    };
    grip.addEventListener("pointermove", move);
    grip.addEventListener("pointerup", () => grip.removeEventListener("pointermove", move), { once: true });
  });
  window.addEventListener("resize", () => terms.get(activeTerm)?.fit.fit());
})();

/* ------------------------------------------------------------------ environments */
async function loadEnvs() {
  state.envs = await api("GET", "/api/environments");
  $("#env-list").innerHTML = state.envs.map((e) => `<li><button data-env="${esc(e.name)}" class="${e.name === state.selectedEnv ? "active" : ""}">
      <span class="t">${esc(e.name)}</span>
      <span class="d">${e.error ? `<span class="error">${esc(e.error)}</span>` : esc(e.description || (e.harnesses || []).join(", "))}</span></button></li>`).join("");
  if (state.selectedEnv) showEnv(state.selectedEnv);
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
    <div class="env-bar"><h3>${esc(name)}</h3>
      ${s.extends ? `<span class="tag">extends ${esc(s.extends)}</span>` : ""}
      ${s.warm_pool ? `<span class="tag">warm pool ${s.warm_pool}</span>` : ""}</div>
    <div class="env-facts">
      ${(s.harnesses || []).map((h) => `<span class="tag">${esc(h)}</span>`).join("")}
      ${Object.entries(s.tools?.mise || {}).map(([k, v]) => `<span class="tag">${esc(k)} ${esc(v)}</span>`).join("")}
      ${s.repo ? `<span class="tag">repo ${esc(s.repo.url)}${s.repo.bake ? " (baked)" : ""}</span>` : ""}
      <span class="tag">${s.resources.cpus} CPU, ${s.resources.memory_gb} GB</span>
    </div>
    <p class="hint">${images.length ? "Built: " + images.map(([p, i]) => `${esc(p)} <code>${esc(i.split(":").pop())}</code>`).join(", ")
      : "Not built yet. It builds automatically the first time a sandbox needs it."}
      Current digest <code>${esc(d.digest)}</code>.</p>
    <label>Spec (YAML)<textarea id="env-yaml" spellcheck="false">${esc(d.yaml)}</textarea></label>
    <div class="env-bar">
      <button id="env-save">Save</button>
      <select id="env-prov">${provs.map((p) => `<option>${esc(p)}</option>`).join("")}</select>
      <label class="check"><input type="checkbox" id="env-force"> Rebuild from scratch</label>
      <button class="primary" id="env-build">Build</button>
      <button id="env-start">Start sandbox</button>
      <button class="ghost" id="env-df">Show Dockerfile</button>
    </div>
    <pre id="env-dockerfile" hidden>${esc(d.dockerfile)}</pre>
    <pre id="env-log" class="log" hidden></pre>`;
  $("#env-save").onclick = (e) => busy(e.target, async () => {
    await api("PUT", `/api/environments/${encodeURIComponent(name)}`, $("#env-yaml").value);
    toast(`Saved ${name}`); await loadEnvs();
  });
  $("#env-df").onclick = () => { const p = $("#env-dockerfile"); p.hidden = !p.hidden; };
  $("#env-start").onclick = () => { location.hash = "#fleet"; setTimeout(() => openNewForm(name), 50); };
  $("#env-build").onclick = (e) => busy(e.target, async () => {
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
  $("#dialog-form").dataset.ok = "Create";
  dialog(`<h3>New environment</h3>
    <label>Name<input name="name" required pattern="[a-z0-9-]+" placeholder="my-service"></label>
    <label>Start from<select name="extends">${state.envs.filter((e) => !e.error).map((e) => `<option>${esc(e.name)}</option>`).join("")}</select></label>
    <label>Repo (optional)<input name="repo" placeholder="https://github.com/org/repo.git"></label>
    <label>Setup command after clone (optional)<input name="setup" placeholder="uv sync"></label>`,
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
      <td><b>${esc(c.name)}</b>${c.description ? `<div class="muted">${esc(c.description)}</div>` : ""}</td>
      <td>${esc(c.harness)}</td>
      <td>${esc(c.kind.replace("_", " "))}<div class="muted">${esc(c.env_var || c.file_path || "")}</div></td>
      <td>${c.exclusive ? (c.leased_to ? `<span class="tag warn">in use by ${esc(c.leased_to)}</span>` : `<span class="tag">exclusive, free</span>`) : `<span class="tag">shared</span>`}</td>
      <td>${c.leased_to ? `<button data-release="${esc(c.name)}" title="Use only if the holding sandbox is gone">Release</button> ` : ""}
        <button class="danger" data-del="${esc(c.name)}">Delete</button></td></tr>`).join("")
    : `<tr><td colspan="5" class="muted">No credentials yet. Add an API key on the right, or sign in with a subscription below.</td></tr>`;
  $("#login-buttons").innerHTML = state.harnesses.filter((h) => h.logins.length).map((h) =>
    `<button data-login="${esc(h.name)}" title="${esc(h.logins[0].notes)}">Sign in to ${esc(h.title)}</button>`).join("");
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
  if (del && confirm(`Delete credential ${del.dataset.del}? Running sandboxes keep their copy until they stop.`)) {
    await busy(del, async () => { await api("DELETE", `/api/credentials/${encodeURIComponent(del.dataset.del)}`); toast("Deleted"); loadCreds(); });
  }
  if (rel) await busy(rel, async () => { await api("POST", `/api/credentials/${encodeURIComponent(rel.dataset.release)}/release`); toast("Released"); loadCreds(); });
});
$("#login-buttons").addEventListener("click", (e) => {
  const b = e.target.closest("[data-login]");
  if (!b) return;
  const h = state.harnesses.find((x) => x.name === b.dataset.login);
  $("#dialog-form").dataset.ok = "Start sign-in";
  dialog(`<h3>Sign in to ${esc(h.title)}</h3>
    <p class="sub">${esc(h.logins[0].notes)}</p>
    <label>Save as credential<input name="credential" required value="${esc(h.name)}-login"></label>`,
  async (fd) => {
    const lg = await api("POST", "/api/logins", { harness: h.name, credential: fd.get("credential") });
    const sb = { id: lg.sandbox, name: `sign-in ${h.name}` };
    openTerminal(sb, lg.session);
    showLoginBar(lg);
  });
});
function showLoginBar(lg) {
  $("#dialog-form").dataset.ok = "Capture credential";
  setTimeout(() => dialog(`<h3>Finish signing in</h3>
    <p class="sub">Complete the flow in the terminal below (open the link, paste the code back). When it says you
      are signed in, capture the credential. The sign-in sandbox is destroyed afterwards.</p>`,
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
      <td><b>${esc(s.id)}</b>${s.label ? `<div class="muted">${esc(s.label)}</div>` : ""}</td>
      <td>${esc(s.sandbox_id)}</td><td>${esc(s.environment)}</td>
      <td><span class="tag">${esc(s.kind)}</span> <span class="muted">${esc(s.provider)}</span></td>
      <td>${ago(s.created_at)}</td>
      <td><button data-fork="${esc(s.id)}">Start copy</button> <button class="danger" data-sdel="${esc(s.id)}">Delete</button></td></tr>`).join("")
    : `<tr><td colspan="6" class="muted">No snapshots. Snapshot a running sandbox from the fleet, or fork one.</td></tr>`;
}
$("#snap-table").addEventListener("click", async (e) => {
  const f = e.target.closest("[data-fork]"), d = e.target.closest("[data-sdel]");
  if (f) await busy(f, async () => { const r = await api("POST", `/api/snapshots/${f.dataset.fork}/fork`, { count: 1 }); toast(`Started ${r[0].name}`); });
  if (d && confirm("Delete this snapshot?")) await busy(d, async () => { await api("DELETE", `/api/snapshots/${d.dataset.sdel}`); loadSnaps(); });
});

/* ------------------------------------------------------------------ connect */
async function loadConnect() {
  const c = await api("GET", "/api/connect");
  const block = (title, body, note = "") => `<div class="snippet"><h3>${esc(title)}</h3><pre>${esc(body)}</pre>
    <button class="ghost" data-copy="${esc(body)}">Copy</button>${note ? `<p class="hint">${esc(note)}</p>` : ""}</div>`;
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
    <td><b>${esc(a.client_name)}</b><div class="muted">${esc((a.redirect_uris[0] || "").replace(/^https?:\/\//, "").split("/")[0])}</div></td>
    <td class="muted">${a.active ? `approved by ${esc(a.approved_by || "?")}, ${ago(a.last_issued)}` : "no active session"}</td>
    <td><button class="danger" data-oauth-revoke="${esc(a.client_id)}">Revoke</button></td></tr>`).join("")
    : `<tr><td class="muted">No apps connected with OAuth yet.</td></tr>`;
}
$("#oauth-table").addEventListener("click", async (e) => {
  const b = e.target.closest("[data-oauth-revoke]");
  if (b && confirm("Revoke this app? It loses access immediately and must be approved again.")) {
    await busy(b, async () => { await api("DELETE", `/api/oauth/clients/${encodeURIComponent(b.dataset.oauthRevoke)}`); loadOAuth(); });
  }
});
$("#connect-snippets").addEventListener("click", (e) => {
  const b = e.target.closest("[data-copy]");
  if (b) navigator.clipboard.writeText(b.dataset.copy).then(() => toast("Copied"));
});
async function loadTokens() {
  const toks = await api("GET", "/api/tokens");
  $("#token-table tbody").innerHTML = toks.map((t) => `<tr><td><b>${esc(t.name)}</b><div class="muted">${esc(t.id)}</div></td>
    <td class="muted">${t.last_used ? "used " + ago(t.last_used) : "never used"}</td>
    <td><button class="danger" data-revoke="${esc(t.id)}">Revoke</button></td></tr>`).join("");
}
$("#token-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  await busy($("button", e.target), async () => {
    const t = await api("POST", "/api/tokens", { name: e.target.name.value });
    const box = $("#token-new");
    box.hidden = false;
    box.innerHTML = `Token for <b>${esc(t.name)}</b>, shown once:<br><code>${esc(t.token)}</code><br>
      <button class="ghost" data-copy-token>Copy</button>`;
    $("[data-copy-token]", box).onclick = () => navigator.clipboard.writeText(t.token).then(() => toast("Copied"));
    e.target.reset(); loadTokens();
  });
});
$("#token-table").addEventListener("click", async (e) => {
  const b = e.target.closest("[data-revoke]");
  if (b && confirm("Revoke this token? Clients using it stop working immediately.")) {
    await busy(b, async () => { await api("DELETE", `/api/tokens/${b.dataset.revoke}`); loadTokens(); });
  }
});

/* ------------------------------------------------------------------ activity */
async function loadAudit() {
  const ev = await api("GET", "/api/audit?limit=300");
  $("#audit-table tbody").innerHTML = ev.map((a) => `<tr>
    <td class="muted" title="${new Date(a.ts * 1000).toLocaleString()}">${ago(a.ts)}</td>
    <td>${esc(a.actor)}</td><td>${esc(a.action)}</td><td>${esc(a.target || "")}</td>
    <td class="muted">${esc(Object.entries(a.detail).map(([k, v]) => `${k}=${typeof v === "object" ? JSON.stringify(v) : v}`).join("  ").slice(0, 160))}</td></tr>`).join("");
}

/* ------------------------------------------------------------------ start */
if (store.token) api("GET", "/api/me").then(boot).catch(() => showGate());
else showGate();
