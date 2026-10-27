/* The agent screen (vnc.html): noVNC over hangar's ticketed websocket. A file, not an inline script, so the page's
   Content-Security-Policy refuses every inline script (KAN-174). */
// A one-time ticket from POST /api/screen/tickets (or MCP open_screen) arrives in the fragment, which browsers
// never send to a server. Watching is view-only unless the ticket was minted for control; hangar enforces that
// itself, so the viewOnly flag here only spares a viewer from typing into nothing.
import RFB from "https://cdn.jsdelivr.net/npm/@novnc/novnc@1.7.0/core/rfb.js";

const params = new URLSearchParams(location.hash.slice(1));
history.replaceState(null, "", location.pathname);
const ticket = params.get("ticket");
const control = params.get("control") === "1";
const status = document.getElementById("status");
document.getElementById("mode").textContent = control ? "you have control" : "view only";
// Airframe statuses: connecting is queued (ring), connected is running (quiet dot), a lost screen is failed
const say = (text, role) => {
  document.getElementById("status-text").textContent = text;
  status.className = `af-status af-status--${role}${role === "failed" ? " af-status--lit" : ""}`;
};
// the screen area without a screen: an Airframe empty state that says what is not here and what to do (no-data
// for a missing ticket, after-action when the agent closed its screen, error when the connection failed)
const empty = document.getElementById("empty"), card = empty.firstElementChild;
const show = (kind, title, parts) => {
  card.dataset.kind = kind;
  card.querySelector(".af-ic").hidden = kind === "error";
  card.querySelector(".af-status").hidden = kind !== "error";
  card.querySelector(".af-empty__title").textContent = title;
  card.querySelector(".af-empty__text").replaceChildren(...nodes(parts));
  empty.hidden = false;
};
// the text is built as DOM nodes, never markup: the refusal reason comes from the sandbox's VNC server, which the
// agent controls (KAN-174). A part is a string, or {mono} / {b} for a run of mono or bold words.
const nodes = (parts) => parts.map((p) => {
  if (typeof p === "string") return document.createTextNode(p);
  const el = document.createElement(p.b !== undefined ? "b" : "span");
  if (p.mono !== undefined) el.className = "af-mono";
  el.textContent = p.b !== undefined ? p.b : p.mono;
  return el;
});

if (!ticket) {
  say("No ticket", "off");
  show("no-data", "No screen to show.", ["Open this page from a sandbox's ", { b: "Agent screen" }, " action, from your MCP client (",
    { mono: "open_screen" }, ") or with a ticket from ", { mono: "POST /api/screen/tickets" }, ". A ticket opens one screen once."]);
} else {
  const scheme = location.protocol === "https:" ? "wss" : "ws";
  const rfb = new RFB(document.getElementById("screen"),
                      `${scheme}://${location.host}/api/screen?ticket=${encodeURIComponent(ticket)}`,
                      { wsProtocols: ["binary"] });
  rfb.viewOnly = !control;
  rfb.scaleViewport = true;
  rfb.background = getComputedStyle(document.documentElement).getPropertyValue("--af-canvas").trim();
  rfb.addEventListener("connect", () => { say("Connected", "running"); empty.hidden = true; });
  rfb.addEventListener("disconnect", (e) => {
    const when = new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    if (e.detail.clean) {
      say("Disconnected", "off");
      show("after-action", "The agent's screen closed.", ["The sandbox ended its screen session at ", { mono: when },
        "; the sandbox itself may still be running. Open the screen again from the sandbox to get a new ticket."]);
    } else {
      say("The screen went away", "failed");
      show("error", "The screen went away.", ["The connection dropped at ", { mono: when }, ": the sandbox stopped, or the network did. "
        + "The sandbox's terminal and files are unaffected. Open the screen again from the sandbox for a new ticket."]);
    }
  });
  rfb.addEventListener("securityfailure", (e) => {
    say("Refused", "failed");
    show("error", "hangar refused this ticket.", [e.detail.reason ? { mono: String(e.detail.reason) } : "The ticket was used already or has expired",
      ". A ticket opens one screen once: open the screen again from the sandbox for a new one."]);
  });
}
