# README visuals

The banner and architecture overview use the dashboard's design tokens from
[`src/hangar/ui/app.css`](../../src/hangar/ui/app.css) and its hangar icon from
[`src/hangar/ui/index.html`](../../src/hangar/ui/index.html).

- Background: `#1e2833`; panels: `#26323f` and `#2d3a48`.
- Borders: `#3a4857`; text: `#e8ecef`; secondary text: `#a9b4be`.
- Accent: `#f2c230`; running state: `#4fb39a`.
- Headings: Barlow Condensed Bold; supporting text: Barlow Regular.

Lettering is converted to SVG paths so GitHub image rendering does not depend on
installed fonts, remote font requests, or embedded font support. Accessible titles,
descriptions, and README alt text preserve the meaning of the outlined lettering.
The font source is [The Barlow Project](https://github.com/jpt/barlow), distributed
through [Google Fonts](https://github.com/google/fonts) under the
[SIL Open Font License](OFL.txt).

The architecture graphic is an overview, not a deployment topology. It follows
[`docs/guide.md`](../guide.md) and the component wiring in
[`src/hangar/app.py`](../../src/hangar/app.py). Keep its labels and the README's
adjacent explanation in sync when the architecture changes.
