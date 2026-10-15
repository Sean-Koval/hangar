---
name: hangar-verify-ui
description: "Use after changing anything a person sees in a web app (pages, components, styles, flows): run the app in this sandbox, verify the change end to end in a real browser, and record a short demo and screenshots that hangar attaches to the PR."
---

# Verify a UI change end to end, and record a demo

This sandbox is your own computer: a virtual display (`DISPLAY=:99`), Chromium through Playwright, and a screen
recorder. People can watch the screen live, so work in the open. Everything below works with any agent harness:
it is plain shell.

## 1. Run the app

- Start the dev server the way the repo documents it (`npm run dev`, `uv run ...`, the environment's services).
  Run long-lived servers in the background (`nohup ... > /tmp/dev.log 2>&1 &`, or `hangar-agent svc` when the
  environment has a supervisor) and wait until the port answers: `curl -sf http://127.0.0.1:3000 >/dev/null`.
- If it doesn't start because of the environment (a missing secret, a blocked host, a service down), run
  `hangar-agent env doctor`, file the blocker with `hangar-agent env request ...`, and verify what you can without it.

## 2. Check the page loads cleanly

```sh
hangar-agent browse http://127.0.0.1:3000/the/page --wait-for "main"
```

It prints the HTTP status, title, console errors, page errors and failed requests as JSON. Fix every error your
change introduced before going on. `--text` adds the visible text; `--screenshot NAME` saves a screenshot.

## 3. Drive the flow you changed

Write a short Playwright script for the exact flow (the browser on the display is visible to anyone watching):

```python
# /tmp/flow.py: run with /opt/hangar/computer/venv/bin/python /tmp/flow.py
from playwright.sync_api import expect, sync_playwright

with sync_playwright() as p:
    b = p.chromium.launch(headless=False, args=["--disable-dev-shm-usage", "--window-size=1280,800",
                                                 "--window-position=0,0"])
    page = b.new_page(no_viewport=True)
    page.goto("http://127.0.0.1:3000/login")
    page.get_by_label("Email").fill("demo@example.com")
    page.get_by_role("button", name="Sign in").click()
    expect(page.get_by_text("Welcome")).to_be_visible()
    page.wait_for_timeout(800)  # let a viewer of the recording see the result
    b.close()
```

If the repo has its own end-to-end tests (Playwright, Cypress), run the relevant ones too, and add one for the flow
you changed when the repo has a place for them. If an MCP browser tool (`browser`) is available to you, you may use
it instead of a script; it also runs on the display.

## 4. Record the demo and take screenshots

```sh
hangar-agent record start --name checkout-flow
/opt/hangar/computer/venv/bin/python /tmp/flow.py
hangar-agent record stop
hangar-agent screenshot http://127.0.0.1:3000/the/page --name after --full-page
```

Keep demos short (under a minute) and focused on what changed. They are handed back as `demos/*.mp4` and
`screenshots/*.png`, and hangar attaches them to the pull request. Stop a recording before you end your turn.
For a before/after, take the "before" screenshot on the base branch first (`git stash`, screenshot, `git stash pop`).

## 5. Say what you verified

In your summary, list the flows you exercised, what you checked, and anything you could not verify and why.

## If the computer misbehaves

- `hangar-agent computer status` shows what runs; `hangar-agent computer start` (re)starts the display and VNC.
- "Xvfb not installed": the environment has no computer; say so in your summary instead of guessing.
- Chromium crashes on large pages: keep `--disable-dev-shm-usage`. Under gVisor, Chromium's own sandbox is off by
  design (gVisor is the boundary) and pages run 1.5-2x slower; raise timeouts rather than retrying blindly.
