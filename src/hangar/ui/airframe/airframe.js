/*! Airframe helpers. One ES module, no dependencies, no build step. Source: airframe/js/airframe.js in
    Sean-Koval/agent-factory; published as theme/airframe.js; do not edit the copy.

    import { status, icon, meta, btn, avatar, obj, tag, toast, confirm, drawer } from "./airframe/airframe.js";

    String helpers return markup that matches the component contracts in airframe/components/<Name>/README.md,
    with every value escaped. Behaviours: toast (bottom centre, held while a person is mid-decision), confirm (an
    af-dialog that replaces window.confirm) and drawer (open/close with Escape, focus return and an inert page). */

const ROLES = { running: "Running", queued: "Queued", approval: "Needs approval", failed: "Failed", denied: "Denied", off: "Off" };
const KINDS = new Set(["primary", "secondary", "ghost", "danger"]);
const AVATARS = new Set(["person", "agent", "firm"]);
const SIZES = new Set([22, 28, 44]);

/** Where the icon sprite is served from; set once at startup when it is not next to the page. */
export const config = { icons: "airframe-icons.svg", toastTimeout: 3500, toastTimeoutWithAction: 7000, toastMax: 3 };

/** Escapes text for HTML content and attribute values. Every helper runs its inputs through this. */
export function esc(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

function attrs(extra) {
  if (!extra) return "";
  return Object.entries(extra)
    .filter(([, v]) => v !== false && v !== null && v !== undefined)
    .map(([k, v]) => (v === true ? ` ${esc(k)}` : ` ${esc(k)}="${esc(v)}"`))
    .join("");
}

function classes(...names) {
  return names.filter(Boolean).join(" ");
}

// ---------------------------------------------------------------- string helpers

/** A status: color + shape + word. `role` is one of the six; `word` specializes it ("Holding at PR", "Needs input",
 *  "Waiting on 2 others" on the queued role). `lit` colors the word too, only where the status is the subject. */
export function status(role, word, { lit = false, title } = {}) {
  if (!(role in ROLES)) throw new RangeError(`Airframe has six statuses; "${role}" is not one of them`);
  const cls = classes("af-status", `af-status--${role}`, lit && "af-status--lit");
  return `<span class="${cls}"${attrs({ title })}><i class="af-status__mark"></i>${esc(word ?? ROLES[role])}</span>`;
}

/** An icon from the sprite. Decorative unless `label` is given. `size` is 14, 20 or 24 (16 by default). */
export function icon(name, { size, label } = {}) {
  const cls = classes("af-ic", size && `af-ic--${size}`);
  const a11y = label ? ` role="img" aria-label="${esc(label)}"` : ' aria-hidden="true"';
  return `<svg class="${cls}"${a11y}><use href="${esc(config.icons)}#af-${esc(name)}"/></svg>`;
}

/** Keyed inline data: KEY value pairs, never "a · b · c". Items are [key, value] or [key, value, tone] with tone
 *  "strong", "approval" or "failed"; an object {key: value} works for plain items. */
export function meta(items) {
  const list = Array.isArray(items) ? items : Object.entries(items);
  const out = list.map((it) => {
    const [key, val, tone] = Array.isArray(it) ? it : [it.key, it.val, it.tone];
    const cls = classes("af-meta__item", tone && `af-meta__item--${tone}`);
    return `<span class="${cls}"><span class="af-meta__key">${esc(key)}</span><span class="af-meta__val">${esc(val)}</span></span>`;
  });
  return `<span class="af-meta">${out.join("")}</span>`;
}

/** A button. `kind` primary | secondary | ghost | danger; `sm` for 28px; `icon` an icon name before the text;
 *  `text` for the sentence-case Red Hat Text variant (where people read; mono capitals stay the default); `cls` adds classes. */
export function btn(label, { kind = "secondary", sm = false, icon: ic, text = false, disabled = false, type = "button", cls: more, attrs: extra } = {}) {
  if (!KINDS.has(kind)) throw new RangeError(`unknown button kind "${kind}"`);
  const cls = classes("af-btn", `af-btn--${kind}`, sm && "af-btn--sm", text && "af-btn--text", !label && "af-btn--icon", more);
  const body = `${ic ? icon(ic) : ""}${esc(label)}`;
  return `<button type="${esc(type)}" class="${cls}"${attrs({ disabled, ...extra })}>${body}</button>`;
}

/** An avatar: people round, agents hexagonal, firms square. `initials` or, for an agent without them, the agent glyph.
 *  `size` 22 | 28 | 44 (30, the nav size, when omitted); `name` becomes the title and label. */
export function avatar(kind, initials, { size, name, pending = false, attrs: extra } = {}) {
  if (!AVATARS.has(kind)) throw new RangeError(`avatar kind must be person, agent or firm, not "${kind}"`);
  if (size !== undefined && !SIZES.has(size)) throw new RangeError(`avatar sizes are 22, 28 and 44, not ${size}`);
  const cls = classes("af-avatar", `af-avatar--${kind}`, size && `af-avatar--${size}`, pending && "af-avatar--pending");
  const inner = initials ? `<span>${esc(initials)}</span>` : (kind === "agent" ? icon("agent") : icon("user"));
  const a11y = name ? { title: name, "aria-label": name, role: "img" } : { "aria-hidden": "true" };
  return `<span class="${cls}"${attrs({ ...a11y, ...extra })}>${inner}</span>`;
}

/** An object chip: a business object by reference. `source` is an icon name (pr, job, database, …), `key` the mono key,
 *  `title` and `state` words; `href` makes it a link; `restricted` shows the key only with the word "restricted";
 *  `approval` puts the approval mark on the state (the one color a chip may carry); `trust` {source, read, by} is the
 *  line shown on hover or focus. */
export function obj({ source, key, title, state, href, restricted = false, approval = false, trust, attrs: extra } = {}) {
  if (!key) throw new TypeError("an object chip needs a key");
  const cls = classes("af-obj", restricted && "af-obj--restricted", href && !restricted && "af-obj--link");
  const tagName = href && !restricted ? "a" : "span";
  const glyph = icon(restricted ? "lock" : (source || "database"));
  let body = `${glyph}<span class="af-obj__key">${esc(key)}</span>`;
  if (restricted) {
    body += `<span class="af-obj__state">restricted</span>`;
  } else {
    if (title) body += `<span class="af-obj__title">${esc(title)}</span>`;
    if (state) {
      body += approval
        ? `<span class="af-obj__state af-status af-status--approval"><i class="af-status__mark"></i>${esc(state)}</span>`
        : `<span class="af-obj__state">${esc(state)}</span>`;
    }
  }
  if (trust) {
    const items = [trust.source && ["source", trust.source], trust.read && ["read", trust.read], trust.by && ["by", trust.by]].filter(Boolean);
    body += `<span class="af-obj__trust" role="tooltip">${meta(items)}</span>`;
  }
  const a = { ...(tagName === "a" ? { href } : { tabindex: trust ? "0" : undefined }), ...extra };
  return `<${tagName} class="${cls}"${attrs(a)}>${body}</${tagName}>`;
}

/** A tag: small mono capitals in a hairline box (cedar, prod, opus). Not a status. */
export function tag(text, { attrs: extra } = {}) {
  return `<span class="af-tag"${attrs(extra)}>${esc(text)}</span>`;
}

/** Parses one markup string into its element. */
export function el(html) {
  const t = document.createElement("template");
  t.innerHTML = html.trim();
  return t.content.firstElementChild;
}

// ---------------------------------------------------------------- alert inhibition

const FIELD = 'input:not([type="button"]):not([type="submit"]):not([type="reset"]):not([type="checkbox"]):not([type="radio"]):not([type="range"]), textarea, [contenteditable]:not([contenteditable="false"])';
const held = [];

/** True while a person is mid-decision: a modal af-dialog or an af-drawer is open, or a text field has focus. Non-critical
 *  toasts are held until it is false again (Airbus inhibits alerts during a phase; Airframe inhibits them during a decision). */
export function inhibited() {
  if (typeof document === "undefined") return false;
  if (document.querySelector(".af-drawer.is-open")) return true;
  try { if (document.querySelector("dialog.af-dialog:modal")) return true; }
  catch (e) { if (document.querySelector("dialog.af-dialog[open]")) return true; } // no :modal support: any open one
  const a = document.activeElement;
  return !!(a && a.matches && a.matches(FIELD) && !a.disabled);
}

/** Shows the toasts that were held, in order, once nothing inhibits them. Called by the behaviours; call it after
 *  closing a dialog or drawer of your own. */
export function flush() {
  while (held.length && !inhibited()) {
    const [msg, opts] = held.shift();
    show(msg, opts);
  }
}

if (typeof document !== "undefined") {
  document.addEventListener("focusout", () => setTimeout(flush, 0));
  document.addEventListener("close", (e) => { if (e.target && e.target.matches && e.target.matches("dialog.af-dialog")) setTimeout(flush, 0); }, true);
}

// ---------------------------------------------------------------- toast

function region() {
  let r = document.querySelector("body > .af-toasts:not(.af-toasts--inline)");
  if (!r) {
    r = el('<div class="af-toasts" role="status" aria-live="polite"></div>');
    document.body.appendChild(r);
  }
  return r;
}

function show(msg, { tone = "plain", action, timeout, html = false } = {}) {
  const t = el(`<div class="af-toast${tone === "bad" ? " af-toast--bad" : ""}"></div>`);
  if (tone === "bad") t.insertAdjacentHTML("beforeend", '<span class="af-status af-status--failed" aria-hidden="true"><i class="af-status__mark"></i></span>');
  const m = el('<span class="af-toast__msg"></span>');
  if (html) m.innerHTML = msg; else m.textContent = msg;
  t.appendChild(m);
  const dismiss = () => {
    if (!t.isConnected) return;
    t.classList.add("is-leaving");
    setTimeout(() => t.remove(), 180);
  };
  if (action) {
    const b = el(btn(action.label, { kind: "ghost", sm: true, cls: "af-toast__act" }));
    b.addEventListener("click", () => { dismiss(); if (action.onClick) action.onClick(); });
    t.appendChild(b);
  }
  const r = region();
  while (r.children.length >= config.toastMax) r.firstElementChild.remove();
  r.appendChild(t);
  const ms = timeout ?? (action || tone === "bad" ? config.toastTimeoutWithAction : config.toastTimeout);
  if (ms > 0) setTimeout(dismiss, ms);
  t.dismiss = dismiss;
  return t;
}

/** Shows a toast at the bottom centre. `tone` "plain" | "bad" (the failed mark); `action` {label, onClick}; `timeout` ms
 *  (0 keeps it); `critical: true` shows it even while a dialog or drawer is open or a field has focus. Non-critical toasts
 *  are held meanwhile and shown afterwards. Returns the element, or null when held. */
export function toast(msg, opts = {}) {
  if (!opts.critical && inhibited()) {
    held.push([msg, opts]);
    return null;
  }
  return show(msg, opts);
}

// ---------------------------------------------------------------- confirm (af-dialog)

/** A confirm built on af-dialog: title, body, and a footer in inspect / refuse / accept order. Resolves true when the
 *  person confirms. `ok` is the verb on the confirming button (primary, or danger when `danger` is true), `cancel` the
 *  word on the refusing one; `inspect` {label, onClick} adds a ghost button on the left; `body` may be markup with `html`. */
export function confirm({ title, body = "", ok = "Confirm", cancel = "Cancel", danger = false, inspect, html = false, text = false } = {}) {
  if (!title) throw new TypeError("confirm needs a title");
  const kind = danger ? "danger" : "primary";
  const d = el(`<dialog class="af-dialog${danger ? " af-dialog--danger" : ""}" aria-labelledby="af-confirm-title">
  <form method="dialog" class="af-dialog__form">
    <header class="af-dialog__head"><h2 class="af-dialog__title" id="af-confirm-title">${esc(title)}</h2></header>
    <div class="af-dialog__body"></div>
    <footer class="af-dialog__foot">${inspect ? btn(inspect.label, { kind: "ghost", text, attrs: { value: "inspect" } }) : ""}${btn(cancel, { kind: "secondary", text, type: "submit", attrs: { value: "cancel" } })}${btn(ok, { kind, text, type: "submit", attrs: { value: "ok" } })}</footer>
  </form></dialog>`);
  const b = d.querySelector(".af-dialog__body");
  if (html) b.innerHTML = body; else if (body) { const p = document.createElement("p"); p.textContent = body; b.appendChild(p); }
  if (inspect) d.querySelector('[value="inspect"]').addEventListener("click", (e) => { e.preventDefault(); if (inspect.onClick) inspect.onClick(); });
  const before = document.activeElement;
  document.body.appendChild(d);
  return new Promise((resolve) => {
    d.addEventListener("close", () => {
      const yes = d.returnValue === "ok";
      d.remove();
      if (before && before.focus && document.contains(before)) before.focus();
      setTimeout(flush, 0);
      resolve(yes);
    }, { once: true });
    d.addEventListener("click", (e) => { if (e.target === d) d.close("cancel"); });
    d.showModal();
    const first = d.querySelector('[value="cancel"]');
    if (first) first.focus();
  });
}

// ---------------------------------------------------------------- drawer

const FOCUSABLE = 'a[href], button:not([disabled]), input:not([disabled]), textarea:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])';
const open = new Map(); // drawer element -> { before, inerted, scrim, onKey }

function resolve(target) {
  const d = typeof target === "string" ? document.querySelector(target) : target;
  if (!d || !d.classList.contains("af-drawer")) throw new TypeError("drawer.open needs an .af-drawer element or selector");
  return d;
}

function inertOutside(d) {
  const keep = new Set([d]);
  const inerted = [];
  for (let node = d; node && node.parentElement && node !== document.body; node = node.parentElement) {
    for (const sib of node.parentElement.children) {
      if (keep.has(sib) || sib === node || sib.matches(".af-toasts, .af-scrim, script, style, link")) continue;
      if (!sib.inert) { sib.inert = true; inerted.push(sib); }
    }
    keep.add(node.parentElement);
  }
  return inerted;
}

function scrimFor(d) {
  let s = d.previousElementSibling;
  if (!(s && s.classList.contains("af-scrim"))) {
    s = el('<div class="af-scrim"></div>');
    d.parentElement.insertBefore(s, d);
  }
  return s;
}

export const drawer = {
  /** Opens a drawer (`.af-drawer.af-drawer--right|--bottom`): shows the scrim, makes the rest of the page inert, moves focus
   *  in, closes on Escape or a click on the scrim, and lifts toasts above a bottom drawer. */
  open(target, { scrim = true } = {}) {
    const d = resolve(target);
    if (open.has(d)) return d;
    const before = document.activeElement;
    const s = scrim ? scrimFor(d) : null;
    if (s) { s.classList.add("is-open"); s.onclick = () => drawer.close(d); }
    d.classList.add("is-open");
    if (!d.hasAttribute("role")) d.setAttribute("role", "dialog");
    if (scrim) d.setAttribute("aria-modal", "true");
    if (d.classList.contains("af-drawer--bottom")) document.documentElement.classList.add("af-drawer-open--bottom");
    const inerted = scrim ? inertOutside(d) : [];
    const onKey = (e) => { if (e.key === "Escape") { e.preventDefault(); drawer.close(d); } };
    document.addEventListener("keydown", onKey);
    open.set(d, { before, inerted, scrim: s, onKey });
    const first = d.querySelector(".af-drawer__body " + FOCUSABLE) || d.querySelector(FOCUSABLE);
    if (first) first.focus(); else { d.tabIndex = -1; d.focus(); }
    d.dispatchEvent(new CustomEvent("af:open", { bubbles: true }));
    return d;
  },
  /** Closes a drawer, restores the page and returns focus to where it was; held toasts show afterwards. */
  close(target) {
    const d = resolve(target);
    const st = open.get(d);
    if (!st) return d;
    open.delete(d);
    d.classList.remove("is-open");
    d.removeAttribute("aria-modal");
    if (st.scrim) { st.scrim.classList.remove("is-open"); st.scrim.onclick = null; }
    for (const n of st.inerted) n.inert = false;
    document.removeEventListener("keydown", st.onKey);
    if (![...open.keys()].some((x) => x.classList.contains("af-drawer--bottom"))) document.documentElement.classList.remove("af-drawer-open--bottom");
    if (st.before && st.before.focus && document.contains(st.before)) st.before.focus();
    d.dispatchEvent(new CustomEvent("af:close", { bubbles: true }));
    setTimeout(flush, 0);
    return d;
  },
  /** Toggles a drawer. */
  toggle(target) {
    const d = resolve(target);
    return open.has(d) ? drawer.close(d) : drawer.open(d);
  },
  isOpen(target) {
    return open.has(resolve(target));
  },
};

export default { config, esc, status, icon, meta, btn, avatar, obj, tag, el, toast, confirm, drawer, inhibited, flush };
