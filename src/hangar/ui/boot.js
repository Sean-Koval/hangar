/* Airframe's helpers and behaviours (status, meta, btn, avatar, toast, confirm, drawer) are one ES module, vendored with
   the CSS. The console's classic scripts reach it as window.airframe: deferred scripts and module scripts run in
   document order, so this module is in place before app.js and fleet.js. A file, not an inline script, so the
   console's Content-Security-Policy can refuse every inline script (KAN-174). */
import airframe from "/ui/airframe/airframe.js";

airframe.config.icons = "/ui/airframe/airframe-icons.svg";
window.airframe = airframe;
