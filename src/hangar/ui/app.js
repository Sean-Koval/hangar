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
const emptyState = ({ kind = "no-data", icon, title, text = "", actions = "", compact = false, cls = "", counts = "" }) =>
  `<div class="af-empty${compact ? " af-empty--compact" : ""}${cls ? ` ${cls}` : ""}" data-kind="${kind}">${icon ? ic(icon, compact ? undefined : 24) : ""}${compact
    ? `<div class="af-empty__body"><span class="af-empty__title">${esc(title)}</span>${text ? `<span class="af-empty__text">${text}</span>` : ""}</div>`
    : `<h3 class="af-empty__title">${esc(title)}</h3>${counts ? `<div class="af-empty__counts">${counts}</div>` : ""}${text ? `<p class="af-empty__text">${text}</p>` : ""}`}${actions
    ? `<div class="af-empty__act">${actions}</div>` : ""}</div>`;
// the avatar triad (Airframe): agents are hexagonal with the harness's initials, or the agent glyph when there is none
const initialsOf = (name) => {
  const w = String(name || "").split(/[\s._-]+/).filter(Boolean);
  return ((w.length > 1 ? w[0][0] + w[1][0] : (w[0] || "?").slice(0, 2)) || "?").toUpperCase();
};
const agentAvatar = (harness, size = 28) => avatar("agent", harness ? initialsOf(harness) : "", { size, name: harness || "agent" });
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

/* ---- Airframe ApprovalCard v2: one held action waiting on a reviewer. The exact command, who asked and for whom,
   the policy that held it, when it expires and what happens then; the decision in inspect / refuse / accept order,
   "Allow for this turn" as the second accept with a hint; Deny opens a reason field, and the reason is required
   (it goes back to the agent); approve needs none. Decided ones become verdict rows. ---- */
const HARNESS_WORD = { "claude-code": "Claude Code", codex: "Codex", pi: "pi", antigravity: "Antigravity" };
const harnessWord = (h) => HARNESS_WORD[h] || h || "the agent";
function untilWords(ts) {
  if (!ts) return "";
  const s = Math.round(ts - Date.now() / 1000);
  if (s <= 0) return "now";
  if (s < 3600) return `${Math.round(s / 60)} min`;
  const h = Math.floor(s / 3600), m = Math.round((s % 3600) / 60);  // "2 h 10 min", as Airframe's cards read
  return m ? `${h} h ${m} min` : `${h} h`;
}
// a: {id, kind: tool | job | request, cmd, tool, taint, policy, since, expires, who: {harness, sandbox, owner, requester},
//     job, session (names, for the inspect link and the words), allowAlways, approve, deny}
function approvalHtml(a) {
  const who = a.who || {};
  const consequence = a.kind === "job" ? "the job is dropped" : a.kind === "request" ? "the request lapses" : "it is denied and the agent carries on";
  const label = a.kind === "job" ? "Job approval" : a.kind === "request" ? "Request" : "Approval";
  const inspect = !a.inspect ? "" : a.job ? btn("Open job", { kind: "ghost", sm: true, attrs: { "data-go": `job:${a.job}` } })
    : a.sessionId ? btn("Open session", { kind: "ghost", sm: true, attrs: { "data-go": `session:${a.sessionId}` } }) : "";
  return `<article class="af-card af-card--lit af-approval" data-approval="${esc(a.id)}" data-kind="${esc(a.kind)}">
    <div class="af-card__top">
      <span class="af-lbl">${ic("approval")}${label}<span class="af-lbl__sub">${esc(a.id)}</span></span>
      ${status("approval", "Needs approval", { lit: true })}
    </div>
    <div class="af-approval__subject">
      <p class="af-approval__cmd">${esc(a.cmd)}</p>
      ${meta([["tool", a.tool && a.tool !== a.cmd ? a.tool : ""], ["taint", a.taint || ""], ["job", a.kind !== "job" ? a.job || "" : ""], ["session", a.job ? "" : a.session || ""]])}
    </div>
    <dl class="af-kv">
      <dt>Requested by</dt><dd>${who.sandbox ? `<span class="af-who">${agentAvatar(who.harness, 22)}<span class="af-id">${esc(who.sandbox)}</span></span> ` : ""}<span class="af-muted">${esc(
        who.sandbox ? harnessWord(who.harness) : who.requester || "someone")}${who.owner ? `, for ${esc(who.owner)}` : ""}${a.since ? `, ${ago(a.since)}` : ""}</span></dd>
      ${a.policy ? `<dt>Policy</dt><dd>${a.policy}</dd>` : ""}
      ${a.expires ? `<dt>Expires</dt><dd>in <span class="af-mono">${esc(untilWords(a.expires))}</span>, then ${consequence}</dd>` : ""}
    </dl>
    <div class="af-approval__act">
      ${meta([["reviewer", "you"]])}
      <div class="af-approval__btns">${inspect}${btn(a.deny || "Deny", { kind: "danger", sm: true, attrs: { "data-decide": "deny" } })}${a.allowAlways
        ? btn("Allow for this turn", { kind: "secondary", sm: true, attrs: { "data-decide": "allow_always" } }) : ""}${btn(a.approve || "Allow", { kind: "primary", sm: true, attrs: { "data-decide": "allow" } })}</div>
    </div>
    ${a.allowAlways ? `<p class="af-approval__hint">${ic("info")}<span>For this turn also allows calls like this one until the turn ends. Nothing is written to the harness's settings; the next turn asks again.</span></p>` : ""}
  </article>`;
}
// the reason block that takes the action row's place once Deny is pressed; the reason is required
function denyReasonHtml(a) {
  const back = a.who?.sandbox || a.job || a.session || "the requester";
  return `<div class="af-approval__reason" role="group" aria-labelledby="deny-lbl-${esc(a.id)}">
    <label class="af-field">
      <span class="af-field__label" id="deny-lbl-${esc(a.id)}">Reason for ${a.kind === "job" ? "rejecting" : "denying"} (required)</span>
      <span class="af-input af-input--area af-input--sans"><textarea rows="2" placeholder="What the agent should do instead"></textarea></span>
      <span class="af-field__error" hidden>${status("failed", `Give a reason. It goes back to ${esc(back)}.`)}</span>
    </label>
    <div class="af-approval__btns">${btn("Cancel", { kind: "ghost", sm: true, attrs: { "data-decide": "cancel" } })}${btn(a.deny || "Deny", { kind: "danger", sm: true, attrs: { "data-decide": "deny-confirm" } })}</div>
  </div>`;
}
// a click inside a card, delegated from the panel that holds it (panels repaint): `find(id)` gives the card's
// object, `after()` reloads. Deny asks for the reason first and refuses without one. True when it handled the click.
function openReason(el, a, text = "") {
  const act = $(".af-approval__act", el);
  if (!act || $(".af-approval__reason", el)) return;
  act.hidden = true;
  act.insertAdjacentHTML("afterend", denyReasonHtml(a));
  $(".af-approval__reason textarea", el).value = text;
}
// the open reason fields in a panel, so a repaint (the 4 s refresh) keeps what the person is writing
const openReasons = (root) => $$(".af-approval__reason", root).map((r) => [r.closest("[data-approval]").dataset.approval, $("textarea", r).value]);
function restoreReasons(root, saved, find) {
  for (const [id, text] of saved) {
    const el = $(`[data-approval="${CSS.escape(id)}"]`, root), a = el && find(id);
    if (a) openReason(el, a, text);
  }
}
function approvalClick(e, find, after) {
  const b = e.target.closest("[data-decide]");
  if (!b) return false;
  const el = b.closest("[data-approval]"), a = find(el.dataset.approval), d = b.dataset.decide;
  if (!a) return true;
  if (d === "deny") {
    openReason(el, a);
    $(".af-approval__reason textarea", el).focus();
  } else if (d === "cancel") {
    $(".af-approval__reason", el).remove();
    $(".af-approval__act", el).hidden = false;
  } else if (d === "deny-confirm") {
    const box = $(".af-approval__reason", el), ta = $("textarea", box), reason = ta.value.trim();
    if (!reason) {
      $(".af-input", box).classList.add("is-invalid"); ta.setAttribute("aria-invalid", "true");
      $(".af-field__error", box).hidden = false; ta.focus();
      return true;
    }
    busy(b, async () => { await decideApproval(a, "deny", reason); await after(); });
  } else busy(b, async () => { await decideApproval(a, d, ""); await after(); });
  return true;
}
// a decided action on one line: the denied placard with its reason, or a quiet check for allowed
function verdictHtml(v) {
  const denied = v.status === "denied";
  const when = v.decided_at ? new Date(v.decided_at * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "";
  return `<div class="af-verdict">
    ${denied ? status("denied", "Denied") : `<span class="af-verdict__ok">${ic("check")}${v.status === "allowed_always" ? "Allowed for the turn" : v.status === "cancelled" ? "Withdrawn" : "Allowed"}</span>`}
    <span class="af-verdict__body"><span class="af-verdict__cmd">${esc(v.detail || v.tool || "")}</span>${v.message || v.by
      ? `<span class="af-verdict__why">${esc(v.message || "")}${v.by ? `${v.message ? " " : ""}By ${esc(v.by)}.` : ""}</span>` : ""}${meta([["tool", v.tool && v.detail ? v.tool : ""]])}</span>
    <span class="af-verdict__time">${esc(when)}</span>
  </div>`;
}
// the held tool calls on a job or session, as cards; `ctx`: {harness, sandbox, owner, job (id), session (name),
// sessionId, inspect (false on the job's or session's own page)}
function toolApprovals(list, ctx) {
  return (list || []).map((ap) => {
    const req = (ap.tool || "").startsWith("env_request:");
    return { id: ap.id, kind: req ? "request" : "tool", cmd: ap.detail || ap.tool, tool: ap.tool, taint: ap.taint,
      policy: req ? `The agent asked for ${esc(ap.tool.slice(12))} access; nothing is granted until someone answers.`
        : `<span class="af-mono">approvals: ask</span>${ap.taint ? ` <span class="af-muted">and the turn read untrusted content</span>` : ""}`,
      since: ap.requested_at, expires: ap.expires_at, who: ctx, job: ctx.job, session: ctx.session, sessionId: ctx.sessionId,
      inspect: ctx.inspect !== false, allowAlways: !req, approve: req ? "Approve" : "Allow" };
  });
}
// a job waiting for a trusted person before it starts
function jobApproval(j, inspect = true) {
  return { id: j.id, kind: "job", cmd: j.title, policy: esc(j.approval?.reason || "Started from outside the team: a trusted person approves it."),
    since: j.created_at, expires: j.approval?.expires_at, who: { requester: j.requester, owner: j.person, harness: j.harness }, job: j.id,
    inspect, approve: "Approve job", deny: "Reject" };
}
// answers a card: a tool call on its job or session, a job waiting to start, or an environment request
async function decideApproval(a, decision, reason) {
  if (a.kind === "job") await api("POST", `/api/jobs/${a.job}/${decision === "deny" ? "reject" : "approve"}`, reason ? { reason } : {});
  else if (a.kind === "request") await api("POST", `/api/env-requests/${a.id}`, { decision: decision === "deny" ? "deny" : "approve", note: reason || null });
  else await api("POST", `${a.job ? `/api/jobs/${a.job}` : `/api/sessions/${a.sessionId}`}/approvals/${a.id}`, { decision, message: reason || null });
  toast(decision === "deny" ? `${a.kind === "job" ? "Rejected" : "Denied"}: ${a.cmd.slice(0, 80)}` : `${a.kind === "job" ? "Approved" : "Allowed"}: ${a.cmd.slice(0, 80)}`);
}

/* ---- Airframe Diff: the change the agent made, greyscale, with line comments that go back to the agent. The patch is
   GET /api/{jobs,sessions}/{ref}/patch (commits, uncommitted changes and untracked files against the base), parsed here
   into files, hunks and lines. A comment opens a thread under its line; the queued comments ride with the next message
   from the Composer (or go alone with "Send comments"), each with its file:line and the line itself. ---- */
const MAX_DIFF_LINES = 3000;
function parsePatch(text) {
  const files = [];
  let f = null, h = null, old = 0, neu = 0, lines = 0;
  for (const raw of text.split("\n")) {
    if (raw.startsWith("diff --git ")) {
      const m = /^diff --git a\/(.*?) b\/(.*)$/.exec(raw);
      f = { path: m ? m[2] : raw.slice(11), oldPath: m ? m[1] : "", hunks: [], added: 0, removed: 0, binary: false, mode: "" };
      files.push(f); h = null;
    } else if (!f) continue;
    else if (raw.startsWith("GIT binary patch") || raw.startsWith("Binary files")) f.binary = true;
    else if (raw.startsWith("new file mode")) f.mode = "new";
    else if (raw.startsWith("deleted file mode")) f.mode = "deleted";
    else if (raw.startsWith("rename to ")) f.mode = "renamed";
    else if (raw.startsWith("@@")) {
      const m = /^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@(.*)$/.exec(raw);
      old = m ? Number(m[1]) : 0; neu = m ? Number(m[2]) : 0;
      h = { header: raw, lines: [] }; f.hunks.push(h);
    } else if (h && lines < MAX_DIFF_LINES && !raw.startsWith("\\ No newline")) {
      const c = raw[0], text = raw.slice(1);
      if (c === "+") { h.lines.push({ kind: "add", old: "", new: neu++, text }); f.added++; }
      else if (c === "-") { h.lines.push({ kind: "del", old: old++, new: "", text }); f.removed++; }
      else if (c === " ") h.lines.push({ kind: "ctx", old: old++, new: neu++, text });
      else continue;
      lines++;
    }
  }
  return { files, truncated: lines >= MAX_DIFF_LINES };
}
// what the person wrote on the lines, per job or session, until the next message takes it to the agent
const review = new Map();  // ref -> [{path, line, kind, code, text, at}]
const reviewKey = (kind, id) => `${kind}/${id}`;
const lineKey = (path, kind, line) => `${path}:${kind === "del" ? "-" : "+"}${line}`;
// one file: header with the path, the stat and the tools; hunks; each line a grid with a Comment affordance
function diffFileHtml(f, i, key) {
  const dir = f.path.includes("/") ? f.path.slice(0, f.path.lastIndexOf("/") + 1) : "", base = f.path.slice(dir.length);
  const comments = review.get(key) || [];
  const line = (l) => {
    const n = l.kind === "del" ? l.old : l.new, k = lineKey(f.path, l.kind, n);
    const mine = comments.filter((c) => c.key === k);
    return `<div class="af-diff__line af-diff__line--${l.kind}" data-line="${esc(k)}"><span class="af-diff__n">${l.old}</span><span class="af-diff__n">${l.new}</span>` +
      `<span class="af-diff__s">${l.kind === "add" ? "+" : l.kind === "del" ? "\u2212" : " "}</span><code class="af-diff__code">${esc(l.text)}</code>` +
      `<button type="button" class="af-btn af-btn--ghost af-diff__cmt" data-comment="${esc(k)}" aria-pressed="${mine.length ? "true" : "false"}" aria-label="Comment on line ${n}">Comment</button></div>` +
      (mine.length ? threadHtml(k, mine, key) : "");
  };
  return `<div class="af-diff" id="diff-${i}" aria-label="Diff of ${esc(f.path)}">
    <header class="af-diff__file">${ic("diff")}<span class="af-diff__path"><small>${esc(dir)}</small>${esc(base)}</span>
      <span class="af-diff__stat"><b class="af-diff__add">+${f.added}</b><b class="af-diff__del">\u2212${f.removed}</b></span>
      ${meta([["file", f.mode || ""]])}
      <span class="af-diff__tools">${btn("Copy patch", { kind: "ghost", sm: true, attrs: { "data-copy-file": i } })}</span></header>
    ${f.binary ? `<div class="af-diff__more">binary file</div>` : f.hunks.map((h) => `<section class="af-diff__hunk">
      <div class="af-diff__hh"><code>${esc(h.header)}</code></div>${h.lines.map(line).join("")}</section>`).join("")}
  </div>`;
}
// the thread under a line: the comments so far and a Composer to add one; what is written goes with the next message
function threadHtml(k, mine, key, open = false) {
  return `<div class="af-diff__thread" role="group" aria-label="Comment thread on ${esc(k)}" data-thread="${esc(k)}">
    ${mine.map((c) => `<div class="af-comment">${avatar("person", initialsOf(state.me?.person || "you"), { size: 22, name: state.me?.person || "you" })}
      <span class="af-comment__who"><span class="af-comment__name">${esc(state.me?.person || "You")}</span><span class="af-comment__time">${esc(new Date(c.at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }))}</span></span>
      <p class="af-comment__text">${esc(c.text)}</p>${btn("", { kind: "ghost", sm: true, icon: "close", cls: "comment-x", attrs: { "data-uncomment": c.at, "aria-label": "Remove this comment" } })}</div>`).join("")}
    ${mine.length && !open ? "" : `<div class="af-composer"><div class="af-composer__box">
      <textarea class="af-composer__input" rows="1" placeholder="${mine.length ? "Reply" : "Comment for the agent"}" aria-label="Comment on ${esc(k)}"></textarea>
      <div class="af-composer__row"><span class="af-diff__note">${ic("agent")}<span>Sent to the agent with your next message</span></span>
        ${btn("Add", { kind: "primary", sm: true, cls: "af-composer__send", attrs: { "data-add-comment": k } })}</div></div></div>`}
  </div>`;
}
function diffHtml(patch, key) {
  if (!patch) return "";
  if (patch.error) return emptyState({ kind: "error", compact: true, title: "The change did not load.", text: esc(patch.error) });
  const { files, truncated } = patch.parsed;
  if (!files.length) return emptyState({ compact: true, icon: "diff", title: "No change yet.", text: `Nothing differs from <span class="af-mono">${esc((patch.base || "").slice(0, 7))}</span> ${patch.source === "sandbox" ? "in the sandbox" : `at ${esc(patch.source || "the last checkpoint")}`}.` });
  const list = files.map((f, i) => `<a class="diff-index__row" href="#diff-${i}" data-diff-jump="${i}"><span class="af-diff__path"><small>${esc(f.path.includes("/") ? f.path.slice(0, f.path.lastIndexOf("/") + 1) : "")}</small>${esc(f.path.slice(f.path.lastIndexOf("/") + 1))}</span>
    <span class="af-diff__stat"><b class="af-diff__add">+${f.added}</b><b class="af-diff__del">\u2212${f.removed}</b></span></a>`).join("");
  const n = (review.get(key) || []).length;
  return `<div class="diff-head">${meta([["against", (patch.base || "").slice(0, 7), "strong"], ["commit", (patch.head || "").slice(0, 7)], ["files", files.length],
      ["from", patch.source === "sandbox" ? "the sandbox" : patch.source || ""], ["as of", patch.as_of ? ago(patch.as_of) : ""]])}
      ${n ? `<span class="af-lbl">${n} comment${n === 1 ? "" : "s"} queued</span>${btn("Send comments", { kind: "secondary", sm: true, attrs: { "data-send-comments": true } })}` : ""}
      ${btn("Refresh", { kind: "ghost", sm: true, attrs: { "data-diff-refresh": true } })}</div>
    <nav class="diff-index" aria-label="Changed files">${list}</nav>
    <div class="diffs">${files.map((f, i) => diffFileHtml(f, i, key)).join("")}</div>
    ${truncated || patch.truncated ? `<p class="hint">The change is longer than shown (${patch.truncated ? "the server capped the patch" : `the first ${MAX_DIFF_LINES} lines`}); pull the branch for the rest.</p>` : ""}`;
}
// the queued comments as the text the agent gets: file:line, the line itself, the words
function reviewText(key) {
  const cs = review.get(key) || [];
  if (!cs.length) return "";
  return `Review comments on the change (${cs.length}):\n\n` + cs.map((c) => `${c.key}\n> ${c.code}\n${c.text}`).join("\n\n");
}
// the patch per job or session, fetched when asked and kept until the head moves
const patches = new Map();
async function loadPatch(kind, id, force = false) {
  const key = reviewKey(kind, id);
  const cur = patches.get(key);
  if (cur?.busy || (cur && !force && !cur.error)) return cur;
  patches.set(key, { ...cur, busy: true });
  try {
    const p = await api("GET", `/api/${kind}/${id}/patch`);
    patches.set(key, { ...p, parsed: parsePatch(p.patch || ""), busy: false });
  } catch (e) { patches.set(key, { error: e.message, busy: false }); }
  return patches.get(key);
}
// clicks inside a "Changes" section: show, refresh, copy, a comment thread, add or remove a comment, send them alone
function diffClick(e, kind, id, rerender, sendAlone) {
  const key = reviewKey(kind, id), box = e.currentTarget;
  const show = e.target.closest("[data-diff-show]"), refresh = e.target.closest("[data-diff-refresh]");
  if (show || refresh) { busy(show || refresh, async () => { await loadPatch(kind, id, !!refresh); rerender(); }); return true; }
  const copy = e.target.closest("[data-copy-file]");
  if (copy) {
    const f = patches.get(key)?.parsed.files[Number(copy.dataset.copyFile)];
    const text = patches.get(key).patch.split("diff --git ").find((part) => part.startsWith(`a/${f.oldPath || f.path} `));
    navigator.clipboard.writeText(`diff --git ${text}`).then(() => toast(`Copied the patch of ${f.path}`));
    return true;
  }
  const cmt = e.target.closest("[data-comment]");
  if (cmt) {  // Comment toggles the line's thread with its composer; the comments already there stay
    const row = cmt.closest(".af-diff__line"), k = cmt.dataset.comment;
    const mine = (review.get(key) || []).filter((c) => c.key === k);
    const thread = row.nextElementSibling?.matches?.("[data-thread]") ? row.nextElementSibling : null;
    if (thread && $(".af-composer", thread)) {
      if (mine.length) thread.outerHTML = threadHtml(k, mine, key, false); else thread.remove();
      cmt.setAttribute("aria-pressed", String(mine.length > 0));
    } else {
      if (thread) thread.remove();
      row.insertAdjacentHTML("afterend", threadHtml(k, mine, key, true));
      cmt.setAttribute("aria-pressed", "true");
      $(`[data-thread="${CSS.escape(k)}"] textarea`, box)?.focus();
    }
    return true;
  }
  const add = e.target.closest("[data-add-comment]");
  if (add) {
    const thread = add.closest("[data-thread]"), ta = $("textarea", thread), text = ta.value.trim();
    if (!text) return ta.focus(), true;
    const k = add.dataset.addComment, row = $(`[data-line="${CSS.escape(k)}"]`, box);
    const list = review.get(key) || [];
    list.push({ key: k, code: $(".af-diff__code", row)?.textContent || "", text, at: Date.now() });
    review.set(key, list);
    ta.value = "";  // the thread stays open for a reply, empty
    rerender();
    return true;
  }
  const rm = e.target.closest("[data-uncomment]");
  if (rm) {
    review.set(key, (review.get(key) || []).filter((c) => String(c.at) !== rm.dataset.uncomment));
    rerender();
    return true;
  }
  if (e.target.closest("[data-send-comments]")) { busy(e.target.closest("[data-send-comments]"), () => sendAlone()); return true; }
  const jump = e.target.closest("[data-diff-jump]");
  if (jump) { e.preventDefault(); $(`#diff-${jump.dataset.diffJump}`, box)?.scrollIntoView({ behavior: "smooth", block: "start" }); return true; }
  return false;
}
// the open threads and what is typed in them, so a repaint (the 4 s refresh) keeps them
const openThreads = (root) => $$("[data-thread] textarea", root).map((ta) => [ta.closest("[data-thread]").dataset.thread, ta.value]);
function restoreThreads(root, saved, key) {
  for (const [k, text] of saved) {
    const row = $(`[data-line="${CSS.escape(k)}"]`, root);
    if (!row) continue;
    const thread = row.nextElementSibling?.matches?.("[data-thread]") ? row.nextElementSibling : null;
    if (thread) thread.remove();
    row.insertAdjacentHTML("afterend", threadHtml(k, (review.get(key) || []).filter((c) => c.key === k), key, true));
    $(".af-diff__cmt", row).setAttribute("aria-pressed", "true");
    const ta = $(`[data-thread="${CSS.escape(k)}"] textarea`, root);
    if (ta) ta.value = text;
  }
}
// the "Changes" section of a job or session detail
function changesHtml(kind, id, hasWork) {
  const key = reviewKey(kind, id), p = patches.get(key);
  return `<h4 class="with-meta">Changes${p && !p.error && p.parsed ? meta([["files", p.parsed.files.length], ["+", p.parsed.files.reduce((n, f) => n + f.added, 0)], ["\u2212", p.parsed.files.reduce((n, f) => n + f.removed, 0)]]) : ""}</h4>
    <div class="changes" data-changes="${esc(key)}">${p && !p.busy ? diffHtml(p, key) : p?.busy ? `<p class="hint">Reading the change from ${hasWork ? "the sandbox" : "the last checkpoint"}…</p>`
      : `<div class="env-bar">${btn("Show the change", { kind: "secondary", sm: true, icon: "diff", attrs: { "data-diff-show": true } })}<span class="hint">The commits, uncommitted edits and new files against the base, from ${hasWork ? "the sandbox" : "the last checkpoint"}. Comment on a line and it goes to the agent with your next message.</span></div>`}</div>`;
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
  get theme() { try { return localStorage.getItem("hangar.theme") || ""; } catch { return ""; } },
  set theme(v) { try { v ? localStorage.setItem("hangar.theme", v) : localStorage.removeItem("hangar.theme"); } catch {} },
};

/* ---- theme: Night is the default (the dark cockpit), Day the alternative; the choice is kept in localStorage as
   hangar.theme and applied by the inline script in <head> before first paint, so there is no flash. The switch in the
   top bar flips between the two; "auto" (follow the OS) is reachable through ?theme=auto, as in Tower. ---- */
const themeInForce = () => document.documentElement.dataset.theme
  || (matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark");
function applyTheme(t) {
  if (t === "auto") document.documentElement.removeAttribute("data-theme"); else document.documentElement.dataset.theme = t;
  store.theme = t;
  syncThemeSwitch();
}
function syncThemeSwitch() {
  const b = $("#theme-switch");
  if (!b) return;
  const day = themeInForce() === "light";
  b.dataset.theme = day ? "light" : "dark";
  const label = day ? "Switch to Night" : "Switch to Day";
  b.setAttribute("aria-label", label); b.title = label;
}
$("#theme-switch")?.addEventListener("click", () => applyTheme(themeInForce() === "light" ? "dark" : "light"));
matchMedia("(prefers-color-scheme: light)").addEventListener?.("change", syncThemeSwitch);  // "auto" follows the OS
syncThemeSwitch();
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
const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;  // "1 checkpoint", "3 checkpoints": never "(s)"

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
  try { state.me = await api("GET", "/api/me"); boot(); } catch {}
});
$("#signout").addEventListener("click", () => { store.token = ""; showGate(); });

/* ------------------------------------------------------------------ routing */
function route() {
  const [v, q] = (location.hash || "#fleet").slice(1).split("?");
  const linked = new URLSearchParams(q || "").get("job");  // #jobs?job=<id>: the inbox's links (#157)
  if (v === "jobs" && linked) state.selectedJob = linked;
  state.view = ["needs", "jobs", "sessions", "fleet", "environments", "credentials", "snapshots", "connect", "activity", "usage"].includes(v) ? v : "fleet";
  $$(".view").forEach((s) => (s.hidden = s.id !== `view-${state.view}`));
  moreSheet(false);
  markCurrent();
  ({ needs: loadNeeds, jobs: loadJobs, sessions: loadSessions, fleet: loadFleet, environments: loadEnvs, credentials: loadCreds, snapshots: loadSnaps,
     connect: loadConnect, activity: loadAudit, usage: loadUsage })[state.view]();
}
window.addEventListener("hashchange", route);
// "Open job" / "Open session" on a card outside the Fleet page (fleet.js handles its own)
document.addEventListener("click", (e) => {
  const g = e.target.closest("[data-go]");
  if (g && !g.closest("#view-fleet")) { e.preventDefault(); goTo(g.dataset.go); }
});
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
  if (!location.hash) {
    // a person signs in to decide things: open on Needs you while something waits, on the Fleet otherwise
    await loadNeeds(true);
    const waiting = (state.inbox?.items || []).filter((i) => i.kind === "approval" || i.kind === "question").length;
    history.replaceState(null, "", waiting ? "#needs" : "#fleet");
  }
  route();
  clearInterval(pollTimer);
  let tick = 0;
  pollTimer = setInterval(() => {
    if (document.hidden) return;
    tick++;
    if (state.view === "fleet") loadFleet(true);
    if (state.view === "jobs") loadJobs(true);
    if (state.view === "sessions") loadSessions(true);
    if (state.view === "needs" || tick % 8 === 0) loadNeeds(true);  // the count elsewhere, every half minute
  }, 4000);
  loadJobs(true);  // fills the nav counts
  loadSessions(true);
  loadNeeds(true);
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

/* ------------------------------------------------------------------ needs you */
// What waits on a person, from GET /api/inbox (the same list the CLI, Slack and MCP clients get): approvals and
// questions first, as the cards they are decided on; then failed jobs and PRs waiting for review, as rows.
async function loadNeeds(quiet = false) {
  try {
    state.inbox = await api("GET", "/api/inbox");
    renderNeeds();
  } catch (e) { if (!quiet && e.message !== "unauthorized") toast(e.message, true); }
}
state.needsCards = [];
function needsCard(item) {
  const job = item.job ? state.jobs.find((j) => j.id === item.job) : null;
  const ses = item.session ? state.sessions.find((x) => x.name === item.session) : null;
  const ctx = { harness: job?.harness || ses?.harness, sandbox: job?.sandbox_id || ses?.sandbox_id, owner: job?.person || job?.requester || ses?.person,
    job: job?.id, session: ses?.name, sessionId: ses?.id };
  if (item.approval) {
    const ap = [...(job?.pending_approvals || []), ...(ses?.pending_approvals || [])].find((a) => a.id === item.approval);
    if (ap) return toolApprovals([ap], ctx)[0];
    return { ...toolApprovals([{ id: item.approval, tool: "", detail: item.reason, requested_at: item.since, expires_at: item.expires_at }], ctx)[0], cmd: item.reason };
  }
  if (item.request) return toolApprovals([{ id: item.request, tool: "env_request:", detail: item.reason, requested_at: item.since }], ctx)[0];
  if (job?.state === "awaiting_approval") return jobApproval(job);
  return { id: item.id, kind: "job", cmd: item.reason, since: item.since, expires: item.expires_at, who: { requester: "someone" }, job: item.job,
    inspect: true, approve: "Approve job", deny: "Reject" };
}
function renderNeeds() {
  const items = state.inbox?.items || [];
  const approvals = items.filter((i) => i.kind === "approval"), questions = items.filter((i) => i.kind === "question");
  const failed = items.filter((i) => i.kind === "failed"), review = items.filter((i) => i.kind === "review");
  navCount("needs", approvals.length + questions.length);
  if (state.view !== "needs") return;
  const n = approvals.length + questions.length;
  const running = state.jobs.filter((j) => j.active).length, sessions = state.sessions.filter((x) => ["idle", "running", "starting"].includes(x.state) && !x.job_id).length;
  $("#needs-sum").innerHTML = (n ? status("approval", `${n} waiting on you`, { lit: true }) : status("running", "Nothing is waiting on you"))
    + meta([["approvals", approvals.length], ["questions", questions.length], ["failed", failed.length], ["review", review.length]]);
  const box = $("#needs-list");
  const open = openReasons(box), drafts = Object.fromEntries($$(".af-question", box).map((q) => [q.id, $("textarea", q).value]));
  state.needsCards = approvals.map(needsCard);
  const qcards = questions.map((item) => {
    const job = item.job ? state.jobs.find((j) => j.id === item.job) : null;
    const ses = item.session ? state.sessions.find((x) => x.name === item.session) : null;
    return { id: `needs-q-${item.id}`, job, ses, html: questionHtml({ id: `needs-q-${item.id}`, who: job?.id || item.session || "", turn: ses?.turns?.length,
      question: item.reason.replace(/^the agent asks: /, ""), asked: item.since, meanwhile: "holding the sandbox" }) };
  });
  const row = (i, st) => `<div class="af-verdict">${st}<span class="af-verdict__body"><span class="af-verdict__cmd">${esc(i.title || i.job || "")}</span>
      <span class="af-verdict__why">${esc(i.reason)}</span>${meta([["repo", (i.repo || "").replace(/^https?:\/\/(www\.)?github\.com\//, "").replace(/\.git$/, "")]])}</span>
      <span class="af-verdict__time">${i.links?.pr ? `<a class="af-btn af-btn--ghost af-btn--sm" href="${esc(i.links.pr)}" target="_blank" rel="noopener">Open PR</a>` : ""}${i.job
        ? btn("Open job", { kind: "ghost", sm: true, attrs: { "data-go": `job:${i.job}` } }) : ""}</span></div>`;
  box.innerHTML = !items.length
    ? emptyState({ kind: "after-action", cls: "af-empty--clear", icon: "approval", title: "Nothing is waiting on you.",
        counts: status("running", `${running} job${running === 1 ? "" : "s"} running`) + status("running", `${sessions} session${sessions === 1 ? "" : "s"} up`),
        text: "They work on their own. A held tool call, a job waiting to start or a question lands here first." })
    : `${state.needsCards.length || qcards.length ? `<div class="cards">${state.needsCards.map(approvalHtml).join("")}${qcards.map((q) => q.html).join("")}</div>` : ""}
      ${failed.length ? `<h3 class="section-title">Failed</h3><div class="verdicts">${failed.map((i) => row(i, status("failed", "Failed"))).join("")}</div>` : ""}
      ${review.length ? `<h3 class="section-title">Waiting for review</h3><div class="verdicts">${review.map((i) => row(i, status("queued", "Review"))).join("")}</div>` : ""}`;
  const findCard = (id) => state.needsCards.find((c) => c.id === id);
  restoreReasons(box, open, findCard);
  for (const q of qcards) {
    const el = $(`#${CSS.escape(q.id)}`);
    if (drafts[q.id]) $("textarea", el).value = drafts[q.id];
    wireQuestion(el, async (text) => {
      if (q.job) await api("POST", `/api/jobs/${q.job.id}/follow-up`, { message: text });
      else if (q.ses) await api("POST", `/api/sessions/${q.ses.id}/send`, { message: text });
      toast("Sent; the agent continues with your answer");
      await Promise.all([loadJobs(true), loadSessions(true)]);
      await loadNeeds();
    });
  }
  box.onclick = (e) => approvalClick(e, findCard, async () => { await Promise.all([loadJobs(true), loadSessions(true)]); await loadNeeds(); });
}

/* ------------------------------------------------------------------ jobs */
const JOB_ACTIVE = ["queued", "provisioning", "running", "verifying", "delivering"];
// job state -> Airframe role. Waiting on a person (an approval, the agent's question) is the orange diamond; a finished
// job is quiet (off), whether it succeeded or was cancelled; only a failure is red.
const JOB_STATE = { queued: "queued", provisioning: "queued", running: "running", verifying: "running", delivering: "running",
  succeeded: "off", failed: "failed", cancelled: "off", needs_input: "approval" };
JOB_STATE.awaiting_approval = "approval"; // workstream A: approve with `hangar job approve` or `/hangar approve`
const JOB_WORD = { needs_input: "Needs input", awaiting_approval: "Needs approval" };
const jobRole = (j) => (j.pending_approvals?.length && j.active ? "approval" : JOB_STATE[j.state] || "off");
const jobWord = (j) => (j.pending_approvals?.length && j.active ? "Needs approval" : JOB_WORD[j.state] || word(j.state));

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
  // with no job open the panel's empty state carries the view's one primary, New job (the header's is secondary)
  else if (!$("#job-detail .af-empty")) $("#job-detail").innerHTML = emptyState({ compact: true, icon: "job", title: "No job selected.",
    text: "Choose one to see its timeline, result and sandbox.", actions: btn("New job", { kind: "primary", sm: true, attrs: { "data-new-job": true } }) });
}
$("#job-detail").addEventListener("click", (e) => { if (e.target.closest("[data-new-job]")) $("#btn-new-job").click(); });
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
  } catch { turnInfo.delete(j.id); }  // unknown: the Composer assumes a live turn and the server says otherwise
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
  const reasons = openReasons(box), threads = openThreads(box), focused = !!document.activeElement?.closest?.("[data-thread]");
  const rkey = reviewKey("jobs", j.id), queued = (review.get(rkey) || []).length;
  const r = j.result || {};
  const info = turnInfo.get(j.id);
  if (j.active && j.state === "running") fetchTurnInfo(j);
  else if (j.pending_approvals?.length && !info) fetchTurnInfo(j);
  const question = j.state === "needs_input" && r.question;
  const cm = question ? null : jobComposer(j, info);
  const pending = inflight.get(j.id);
  // what waits on a person, as Airframe approval cards: the job itself (a trusted person starts it) or held tool calls
  const cards = [...(j.state === "awaiting_approval" ? [jobApproval(j, false)] : []),
    ...toolApprovals(j.pending_approvals, { harness: j.harness, sandbox: j.sandbox_id, owner: j.person || j.requester, job: j.id, inspect: false })];
  const verdicts = (info?.decided || []).filter((v) => v.status !== "pending").slice(-5).reverse();
  const verify = j.verify ? (r.verify_exit_code == null ? `<span class="hint">not run yet</span>`
    : r.verify_exit_code === 0 ? `<span class="passed">${ic("check", 14)}passed</span>`
      : status("failed", `Failed, exit ${r.verify_exit_code}`)) : "";
  const turns = j.turns || [];
  const live = !!j.sandbox_id;
  const role = jobRole(j);
  box.classList.remove("af-card--lit");  // the cards that wait on a person light themselves
  box.innerHTML = `
    <div class="detail-head"><h3>${esc(j.title)}</h3>${status(role, jobWord(j), { lit: true })}
      ${j.reason ? meta([["reason", j.reason.replace(/_/g, " "), role === "failed" ? "failed" : ""]]) : ""}</div>
    ${cards.length ? `<div class="cards">${cards.map(approvalHtml).join("")}</div>` : ""}
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
        ${status(sesRole(j.session), sesWord(j.session))}<span class="hint">${plural(j.session.checkpoints, "checkpoint")}</span></dd>` : ""}
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
        note: queued && !cm.disabled ? `${queued} review comment${queued === 1 ? " goes" : "s go"} with this message` : "",
        queued: pending ? [{ id: "", text: pending, sub: "on its way; applies after this tool call" }] : [] })}`}
    ${live || j.session?.checkpoints || r.commits?.length ? changesHtml("jobs", j.id, live) : ""}
    ${verdicts.length ? `<h4>Decisions</h4><div class="verdicts">${verdicts.map(verdictHtml).join("")}</div>` : ""}
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
  const findCard = (id) => cards.find((c) => c.id === id);
  restoreReasons(box, reasons, findCard);
  restoreThreads(box, threads, rkey);
  if (focused) $("[data-thread] textarea", box)?.focus({ preventScroll: true });
  // the reply: an answer to the agent's question and a follow-up both run another turn; a steer lands in this one;
  // the review comments on the change ride along with the message (or go alone)
  const withReview = (text) => { const rv = reviewText(rkey); return rv ? (text ? `${rv}\n\n---\n\n${text}` : rv) : text; };
  const followUp = async (message, plain = false) => {
    await api("POST", `/api/jobs/${j.id}/follow-up`, { message: plain ? message : withReview(message) });
    if (!plain) review.delete(rkey);
    toast(`Sent; ${j.id} is running another turn`);
    await loadJobs();
  };
  const steer = async (text) => {
    // the direct route answers once the agent has the message: show it queued meanwhile (a refresh keeps the row)
    const shown = text || `Review comments (${(review.get(rkey) || []).length})`;
    $("#job-composer")?.prepend(airframe.el(queuedRow({ text: shown, sub: "on its way; applies after this tool call" })));
    inflight.set(j.id, shown);
    try {
      await api("POST", `/api/jobs/${j.id}/steer`, { message: withReview(text) });
      review.delete(rkey);
      toast("The agent has your message; it applies after this tool call.");
    } catch (e) {
      if (e.message !== "unauthorized") toast(e.message, true);
      return false;  // not taken: the words come back to the box
    } finally { inflight.delete(j.id); renderJob(j); }
    await loadJobs();
  };
  const sendAlone = () => (cm?.mode === "steer" ? steer("") : followUp(""));
  box.onclick = (e) => approvalClick(e, findCard, async () => { turnInfo.delete(j.id); await loadJobs(); })
    || diffClick(e, "jobs", j.id, () => renderJob(j), sendAlone);
  wireQuestion($("#job-question"), (text) => { $("#job-question textarea").value = ""; return followUp(text, true); });
  if ($("#job-question textarea")) $("#job-question textarea").value = draft;
  wireComposer($("#job-composer"), { draft, onSend: (text) => (cm.mode === "steer" ? steer(text) : followUp(text)) });
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
const sesRole = (x) => (x.pending_approvals?.length ? "approval" : x.needs_input && x.state !== "running" ? "approval" : SES_TONE[x.state] || "off");
const sesWord = (x) => (x.pending_approvals?.length ? "Needs approval" : x.needs_input && x.state !== "running" ? "Needs input" : word(x.state));

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
    text: "Choose one to resume it, talk to its agent, or open its terminal.", actions: btn("New session", { kind: "primary", sm: true, attrs: { "data-new-session": true } }) });
}
$("#session-detail").addEventListener("click", (e) => { if (e.target.closest("[data-new-session]")) $("#btn-new-session").click(); });
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
  const reasons = openReasons(box), threads = openThreads(box), focused = !!document.activeElement?.closest?.("[data-thread]");
  const rkey = reviewKey("sessions", x.id), queued = (review.get(rkey) || []).length;
  const live = !!x.sandbox_id && ["idle", "running"].includes(x.state);
  const ck = x.checkpoints?.at(-1);
  const keep = x.idle_minutes;
  const role = sesRole(x);
  const question = x.needs_input && role === "approval" && !x.job_id;
  const cm = question ? null : sesComposer(x);
  const form = !cm?.disabled || question;  // the message carries the panel's one primary action
  const cards = toolApprovals(x.pending_approvals, { harness: x.harness, sandbox: x.sandbox_id, owner: x.person || x.owner, job: x.job_id,
    session: x.name, sessionId: x.id, inspect: false });
  const verdicts = (x.turns.at(-1)?.approvals || []).filter((v) => v.status !== "pending").slice(-5).reverse();
  box.classList.remove("af-card--lit");  // the cards that wait on a person light themselves
  box.innerHTML = `
    <div class="detail-head"><h3 class="af-mono">${esc(x.name)}</h3>${status(role, sesWord(x), { lit: true })}</div>
    ${x.reason ? (role === "failed" ? `<p class="detail-reason">${errorLine(x.reason)}</p>` : `<p class="hint">${esc(x.reason)}</p>`) : ""}
    ${cards.length ? `<div class="cards">${cards.map(approvalHtml).join("")}</div>` : ""}
    ${question ? questionHtml({ id: "ses-question", who: x.name, turn: x.turns.length, question: x.needs_input, asked: x.last_active_at,
      meanwhile: live ? "holding the sandbox" : "the sandbox is released; your answer resumes it" }) : ""}
    <dl class="kv af-kv">
      <dt>Where</dt><dd>${meta([["env", x.environment, "strong"], ["harness", x.harness || "default agent"], ["model", x.model || ""]])}</dd>
      ${x.repo ? `<dt>Repo</dt><dd><span class="af-mono">${esc(jobRepo(x))}</span></dd><dt>Branch</dt><dd><code>${esc(x.branch)}</code></dd>` : ""}
      <dt>Conversation</dt><dd>${x.harness_session ? `<code>${esc(x.harness_session)}</code> <span class="sub-val">resumes natively</span>`
        : `<span class="hint">${x.turns.length ? "summary handoff on the next turn" : "starts with the first message"}</span>`}</dd>
      <dt>Saved</dt><dd>${x.checkpoint_count ? `${plural(x.checkpoint_count, "checkpoint")}, last ${ago(x.last_checkpoint_at)}
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
    ${question ? "" : `<h4>${cm.busy || cm.queued?.length ? "Steer the agent" : "Message the agent"}</h4>${composerHtml({ id: "ses-composer", ...cm,
      note: queued && !cm.disabled ? `${queued} review comment${queued === 1 ? " goes" : "s go"} with this message` : "" })}`}
    ${x.turns.length && (live || x.checkpoint_count) ? changesHtml("sessions", x.id, live) : ""}
    ${verdicts.length ? `<h4>Decisions</h4><div class="verdicts">${verdicts.map(verdictHtml).join("")}</div>` : ""}
    ${x.turns.length ? activityBlock("sessions", x.id, x.usage) : ""}
    ${x.turns.length ? `<h4>Turns</h4><ol class="turns">${x.turns.slice().reverse().map((t) => `<li>
      <b>${esc(t.message.split("\n")[0].slice(0, 140))}</b>
      ${meta([["harness", t.harness || ""], ["mode", t.mode], t.exit_code != null ? ["exit", t.exit_code] : t.run_id ? ["exit", "running"] : null,
        ["started", t.started_at ? ago(t.started_at) : ""]])}
      ${t.summary ? `<div class="turn-summary">${esc(t.summary.slice(0, 600))}</div>` : ""}</li>`).join("")}</ol>` : ""}`;
  fillActivity(box);
  const sb = { id: x.sandbox_id, name: x.name };
  const findCard = (id) => cards.find((c) => c.id === id);
  restoreReasons(box, reasons, findCard);
  restoreThreads(box, threads, rkey);
  if (focused) $("[data-thread] textarea", box)?.focus({ preventScroll: true });
  // one route for all of it: a send starts a turn, or steers the running one (queued server-side, with a take-back);
  // the review comments on the change ride along with the message (or go alone)
  const withReview = (text) => { const rv = reviewText(rkey); return rv ? (text ? `${rv}\n\n---\n\n${text}` : rv) : text; };
  const send = async (message, plain = false) => {
    await api("POST", `/api/sessions/${x.id}/send`, { message: plain ? message : withReview(message) });
    if (!plain) review.delete(rkey);
    toast(x.state === "suspended" ? `Resuming ${x.name}, then sending` : x.busy || x.state === "running" ? "Queued; it applies after this tool call" : "Sent");
    await loadSessions();
  };
  box.onclick = (e) => approvalClick(e, findCard, loadSessions) || diffClick(e, "sessions", x.id, () => renderSession(x), () => send(""));
  // (a send with no words of its own carries the review comments alone)
  wireQuestion($("#ses-question"), (text) => { $("#ses-question textarea").value = ""; return send(text, true); });
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
    <p class="hint">${images.length ? "Built: " + images.map(([p, i]) => `${esc(p)} <code>${esc(i.split(":").pop())}</code>`).join(", ") + "."
      : "Not built yet. It builds automatically the first time a sandbox needs it."}
      Current digest <code>${esc(d.digest)}</code>.</p>
    ${field("Spec (YAML)", inputWell(`<textarea id="env-yaml" spellcheck="false">${esc(d.yaml)}</textarea>`, { area: true }))}
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
      ["cost", "Spend", usd(t.cost_usd), t.unpriced_turns ? `+ ${plural(t.unpriced_turns, "turn")} with no known price` : "list prices", "USD"],
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
        <td class="af-num af-table__mono" title="${r.unpriced_turns ? `${plural(r.unpriced_turns, "turn")} with no known price` : ""}">${usd(r.cost_usd)}${r.unpriced_turns ? " +" : ""}</td></tr>`).join("")
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
  const tipText = (d) => `${d.key}: ${usd(d.cost_usd)}, ${kTok(d.tokens)} tokens, ${plural(d.turns, "turn")}`
    + (d.budget_stops ? `, ${plural(d.budget_stops, "budget stop")}` : "");
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
if (store.token) api("GET", "/api/me").then((me) => { state.me = me; boot(); }).catch(() => showGate());
else showGate();
