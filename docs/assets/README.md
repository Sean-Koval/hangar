# README banner, favicon and marks

`brand/` holds the README banner (Night and Day), the GitHub social preview, the favicon and the header mark.
They are generated in [Sean-Koval/agent-factory](https://github.com/Sean-Koval/agent-factory) by
`scripts/banners.py` from Airframe's tokens, product marks and fonts (text is outlined, so GitHub renders
it without web fonts), and vendored here unmodified:

```bash
sh scripts/sync-brand.sh theme-vN      # or a commit; also refreshes src/hangar/ui/favicon.svg
```

Never edit them here; change Airframe or the banner copy in agent-factory. `social.png` is uploaded by hand
in the repository settings (Social preview).

# Front page assets

`front/` holds the recording, screenshots and diagram the README and the docs home page
(`docs/index.md`) show, and the scripts that make them. Nothing in it is drawn by hand:

| Asset | Made by |
| --- | --- |
| `hangar-demo.gif`, `hangar-demo.mp4` | `make.py`: Playwright drives the web UI on :8787 while issue #42 becomes a job, a sandbox run and draft PR #43; the terminal part is an asciinema recording of `hangar inbox` played with asciinema-player; ffmpeg encodes the frames |
| `ui-jobs.png`, `ui-sessions.png`, `inbox.png` | `make.py`, from the same run |
| `hangar-suite.svg`, `-dark.png`, `-light.png` | `diagram.py` (the agent-factory diagram helpers, copied) and `make.py` (PNGs in both Airframe flavors) |

They run against `demo.py`: the real control plane with the test suite's fake provider
(sandboxes are directories), a scripted agent that edits, tests and commits in Claude Code's
`stream-json`, and a stand-in GitHub API for the fictional `acme/checkout-api`. It also works
on its own for a look around: `uv run python docs/assets/front/demo.py`.

To re-render after a UI or CLI change, run from the repository root (Chromium from
`playwright install chromium`), look at the results, and commit them:

```bash
uv run --with playwright==1.56.0 --with asciinema==2.4.0 --with imageio-ffmpeg==0.6.0 \
  python docs/assets/front/make.py            # or --only diagram / --only ui, --out DIR
```

`make.py` fails if an asset is missing or over 3 MB. The `front-page` workflow runs it on
changes to these scripts, the web UI or the Airframe stylesheet, and weekly; it checks the
committed SVG is current and uploads the fresh renders as the `front-page-assets` artifact.
