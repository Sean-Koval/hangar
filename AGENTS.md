# Repository guidance

## Design

- Airframe, in [Sean-Koval/agent-factory](https://github.com/Sean-Koval/agent-factory), is the design source of truth for the web console (`src/hangar/ui/`), the agent screen (`vnc.html`) and the docs site.
- Before any UI work, read `airframe/README.md`, the brand book in `airframe/guide/` (brand, Status, Data, Products) and the README of each component you touch in `airframe/components/<Name>/`, all in agent-factory.
- Use `var(--af-*)` tokens and `af-` classes only: no color literals, fonts or radii of your own. `tests/test_ui_airframe.py` fails on a hex, `rgb()` or `hsl()` color in `app.css`, `index.html`, `vnc.html` or `app.js`.
- Statuses follow the dark cockpit: running and queued stay grey; only needs approval (orange diamond), failed (red triangle) and denied (red placard) carry color. The brand yellow is for the primary action, focus and selection, never a status. hangar's hue (`--af-product-hangar`) appears only in its mark, the docs header and the 2px nav marker.
- Never edit the vendored files: `src/hangar/ui/airframe/` and `docs/stylesheets/airframe.css` and `airframe-docs.css`. Change Airframe in agent-factory, then refresh with `sh scripts/sync-airframe.sh theme-vN`. hangar's product hue for the docs lives in `docs/stylesheets/product.css`.
