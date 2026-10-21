# README banner, favicon and marks

`brand/` holds the README banner (Night and Day), the GitHub social preview, the favicon and the header mark.
They are generated in [Sean-Koval/agent-factory](https://github.com/Sean-Koval/agent-factory) by
`scripts/banners.py` from Airframe's tokens, product marks and fonts (text is outlined, so GitHub renders
it without web fonts), and vendored here unmodified:

```bash
sh scripts/sync-brand.sh theme-vN      # or a commit; also refreshes src/hangar/ui/favicon.svg
sh scripts/sync-brand.sh --check       # do the files still match the commit SOURCE records? (the docs CI)
```

A sync records the agent-factory commit and each file's sha256 in `brand/SOURCE`; `tests/test_brand_sync.py`
fails on a file that differs from it, and the docs workflow fetches the files again at that commit and warns when
agent-factory main has newer ones. Re-run the sync whenever agent-factory regenerates the banners (a new theme).
Never edit them here; change Airframe or the banner copy in agent-factory. `social.png` is uploaded by hand
in the repository settings (Social preview).

# Front page assets

`front/` holds the recordings, screenshots and diagram the README and the docs home page
(`docs/index.md`) show, and the scripts that make them. Nothing in it is drawn by hand; every still is
1440x900 at 2x in Night (Needs you and the Fleet also in Day), every recording under 20 s with one task each:

| Asset | Made by |
| --- | --- |
| `hangar-demo.gif`, `hangar-demo.mp4` | `make.py`: Playwright drives the console on :8787 while issue #42 becomes a job, a sandbox run and draft PR #43, ending on Needs you. It opens on the ticket over the live console (no title card), follows the job's detail panel as a close-up while the agent works, and runs under 20 s; ffmpeg encodes the frames (MP4 1280x800, GIF 960 wide) |
| `ui-jobs.png`, `ui-sessions.png` | `make.py`, from the same run |
| `ui-hero.png`, `ui-hero-light.png` | `make.py`, from the same run: the delivered job's panel from a 780 px window at 2x, Night and Day, for the docs home's hero (it links to the MP4) |
| `inbox.png` | `make.py`: an asciinema recording of `hangar inbox` against the same server, played with asciinema-player |
| `ui-needs.png`, `ui-fleet.png`, `ui-review.png`, and `ui-needs-light.png`, `ui-fleet-light.png` in Day | `make.py --only console`, cropped to what each caption names (the Needs-you title and deck at an 820 px viewport, the Fleet with the console header, headline, deck band and first rows at 1280 px, the diff with its comment), at 2x, so their text reads at a README's width; the console on `fixture.py` (a team's afternoon: four environments, six people, held tool calls, a question, a failed run, a PR to review) served with Playwright's request interception |
| `hangar-review.gif`, `hangar-review.mp4` | `make.py --only console`: the review loop on the fixture (decide a held call, open the job, read the diff, comment back) |
| `ui-fleet-board.png` | `make.py`, the Fleet's Board with an agent open in place ([the web console](../console.md)), from the demo run |
| `hangar-suite.svg`, `hangar-suite-stacked.svg`, `-dark.png`, `-light.png` | `diagram.py` (drawn in the style of agent-factory's `scripts/diagrams.py`; the docs home inlines the SVGs, the stacked one on phones) and `make.py` (PNGs in both Airframe flavors, for the README) |

They run against `demo.py`: the real control plane with the test suite's fake provider
(sandboxes are directories), a scripted agent that edits, tests and commits in Claude Code's
`stream-json`, and a stand-in GitHub API for the fictional `acme/checkout-api`. It also works
on its own for a look around: `uv run python docs/assets/front/demo.py`.

To re-render after a UI or CLI change, run from the repository root (Chromium from
`playwright install chromium`), look at the results, and commit them:

```bash
uv run --with playwright==1.56.0 --with asciinema==2.4.0 --with imageio-ffmpeg==0.6.0 \
  python docs/assets/front/make.py            # or --only diagram / ui / console, --out DIR
```

`make.py` fails if an asset is missing or over 3 MB. The `front-page` workflow runs it on
changes to these scripts, the web UI or the Airframe stylesheet, and weekly; it checks the
committed SVG is current and uploads the fresh renders as the `front-page-assets` artifact.
