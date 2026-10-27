/* The theme, before first paint (index.html and vnc.html load this in <head>, blocking, so there is no flash): Night is
   the default (the brand's dark cockpit); a stored choice (localStorage hangar.theme, set by the switch in the console's
   top bar) or a link's ?theme=day|night wins, and "auto" follows the OS. A file, not an inline script, so the pages'
   Content-Security-Policy can refuse every inline script (KAN-174). */
(function () {
  var t = null;
  try {
    var q = new URLSearchParams(location.search).get("theme");
    t = q || localStorage.getItem("hangar.theme");
    if (q) localStorage.setItem("hangar.theme", q);
  } catch (e) {}
  if (t === "light" || t === "day") document.documentElement.dataset.theme = "light";
  else if (t === "auto") document.documentElement.removeAttribute("data-theme");
})();
