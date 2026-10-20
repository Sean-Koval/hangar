/* hangar's tail numbers (KAN-114): the short ID a person reads, derived from the real one, as Airframe's brand book
   sets them out (guide/brand.md, "Tail-number IDs"): the product prefix HGR, a kind segment, a short code; uppercase,
   hyphenated. A sandbox is the plain one (HGR-7Q2K); the rest name their kind (HGR-JOB-4F7A, HGR-SES-…, HGR-APR-…,
   HGR-SNAP-…, HGR-REQ-…, HGR-RUN-…, HGR-BLD-…, HGR-TOK-…). Environments, credentials and people keep their names.

   Display only: the API, the store and every URL keep the real ID, and the page keeps it in `title` and `data-raw-id`
   (hover shows it, copying a tail copies it). The code is Crockford base32 (no I, L, O or U) of a 53-bit hash of the
   whole real ID, four characters long; when two IDs this page has seen share a kind and a four-character code, both
   grow one character at a time until they differ, so two live objects never read the same. src/hangar/tails.py is the
   same function for what the server writes for people (Slack, the PR body); tests/test_tails.py holds them equal. */
(function (root) {
  "use strict";
  const ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ";
  const PRODUCT = "HGR";
  const KINDS = { sb: "", job: "JOB", ses: "SES", apr: "APR", ap: "APR", snap: "SNAP", rq: "REQ", run: "RUN", bld: "BLD", tok: "TOK" };
  const MIN = 4, MAX = 10;  // 4 is Airframe's length; 10 characters carry 50 bits, past any collision a fleet will meet
  const RAW = /^(sb|job|ses|apr|ap|snap|rq|run|bld|tok)-[0-9a-f]{4,32}$/;
  // a real ID inside escaped text: not part of a path, a branch or a longer word (hangar/job-e0123d68 stays as typed)
  const IN_TEXT = /(^|[^\w/.=#-])((?:sb|job|ses|apr|ap|snap|rq|run|bld|tok)-[0-9a-f]{8,32})(?![\w/-])/g;

  // cyrb53 (public domain, bryc): a 53-bit string hash with good spread, exact in a double
  function hash53(s) {
    let h1 = 0xdeadbeef, h2 = 0x41c6ce57;
    for (let i = 0; i < s.length; i++) {
      const c = s.charCodeAt(i);
      h1 = Math.imul(h1 ^ c, 2654435761);
      h2 = Math.imul(h2 ^ c, 1597334677);
    }
    h1 = Math.imul(h1 ^ (h1 >>> 16), 2246822507) ^ Math.imul(h2 ^ (h2 >>> 13), 3266489909);
    h2 = Math.imul(h2 ^ (h2 >>> 16), 2246822507) ^ Math.imul(h1 ^ (h1 >>> 13), 3266489909);
    return 4294967296 * (2097151 & h2) + (h1 >>> 0);
  }
  const full = new Map();  // id -> its ten-character code
  function code(id) {
    let c = full.get(id);
    if (c === undefined) {
      const h = hash53(id);
      c = "";
      for (let i = 0; i < MAX; i++) c += ALPHABET[Math.floor(h / 2 ** (45 - 5 * i)) % 32];
      full.set(id, c);
    }
    return c;
  }
  const kindOf = (id) => { const m = RAW.exec(String(id ?? "")); return m ? KINDS[m[1]] : null; };
  const isRaw = (id) => kindOf(id) !== null;

  // the IDs this page has seen, grouped by kind and four-character code; a group of more than one widens its codes
  const groups = new Map(), width = new Map();
  function see(id) {
    const kind = kindOf(id);
    if (kind === null || width.has(id)) return;
    const key = `${kind}|${code(id).slice(0, MIN)}`;
    const g = groups.get(key) || new Set();
    g.add(id); groups.set(key, g);
    for (const a of g) {
      let n = MIN;
      for (const b of g) {
        if (a === b) continue;
        let k = 0;
        const ca = code(a), cb = code(b);
        while (k < MAX && ca[k] === cb[k]) k++;
        n = Math.max(n, k + 1);
      }
      width.set(a, Math.min(n, MAX));
    }
  }
  const seeAll = (ids) => { for (const id of ids || []) if (id) see(id); };
  // the code alone (7Q2K): a Fleet row or card, whose group already says hangar
  function short(id) {
    if (!isRaw(id)) return String(id ?? "");
    see(id);
    return code(id).slice(0, width.get(id));
  }
  // the whole tail number (HGR-7Q2K, HGR-JOB-4F7A); anything that isn't a hangar ID comes back as it was
  function tail(id) {
    const kind = kindOf(id);
    if (kind === null) return String(id ?? "");
    return `${PRODUCT}-${kind ? `${kind}-` : ""}${short(id)}`;
  }
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  // the tail as markup: Airframe's af-id, the real ID in its title (hover) and data-raw-id (copy)
  function html(id, { codeOnly = false, cls = "af-id" } = {}) {
    if (!isRaw(id)) return esc(id);
    return `<span class="${cls}" title="${esc(id)}" data-raw-id="${esc(id)}">${esc(codeOnly ? short(id) : tail(id))}</span>`;
  }
  // escaped text (a timeline line, an audit row) with each real ID in it shown as its tail
  const inText = (escaped) => String(escaped ?? "").replace(IN_TEXT, (_, pre, id) => `${pre}${html(id)}`);
  // plain text with each real ID shown as its tail: a toast, an aria-label
  const inPlain = (text) => String(text ?? "").replace(IN_TEXT, (_, pre, id) => `${pre}${tail(id)}`);
  // what a person typed or a link carried (HGR-7Q2K, hgr-job-4f7a, 7Q2K, or the real ID) -> the real ID among `ids`;
  // no single match gives the input back
  function resolve(text, ids) {
    const s = String(text ?? "").trim();
    if (!s) return s;
    const list = [...(ids || [])];
    if (list.includes(s)) return s;
    const want = s.toUpperCase().replace(/^HGR-/, "");
    const hit = list.filter((id) => {
      const kind = kindOf(id);
      if (kind === null) return false;
      const c = want.startsWith(`${kind}-`) && kind ? want.slice(kind.length + 1) : kind ? null : want;
      return c != null && c.length >= MIN && code(id).startsWith(c);
    });
    return hit.length === 1 ? hit[0] : s;
  }

  const api = { tail, short, html, inText, inPlain, resolve, see: seeAll, isRaw, code, hash53, ALPHABET, MIN, MAX };
  root.tails = api;
  if (typeof module === "object" && module.exports) module.exports = api;

  // copying a tail copies the real ID: the selection sits inside one tail, or is exactly one tail's text
  if (typeof document !== "undefined") {
    document.addEventListener("copy", (e) => {
      const sel = document.getSelection?.();
      if (!sel || sel.isCollapsed || !e.clipboardData) return;
      const at = (n) => (n?.nodeType === 1 ? n : n?.parentElement)?.closest?.("[data-raw-id]");
      const a = at(sel.anchorNode), b = at(sel.focusNode);
      const el = a && a === b ? a : null;
      if (!el || sel.toString().trim() !== el.textContent.trim()) return;
      e.clipboardData.setData("text/plain", el.dataset.rawId);
      e.preventDefault();
    });
  }
})(typeof window !== "undefined" ? window : globalThis);
