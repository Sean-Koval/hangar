# The agent's own computer, and an environment it can diagnose

Environment quality decides what a cloud agent produces, and environment failures are silent: a missing secret or a
blocked registry doesn't stop the run, it just makes the output worse. hangar gives every agent two things to
counter that:

- **Its own computer** (opt-in per environment): a virtual display, Chromium through Playwright, screenshots,
  recorded demos, and a screen people can watch or take over from hangar.
- **Self-diagnosis** (every sandbox): `hangar-agent env doctor` and an MCP server in the sandbox that say what in the
  environment works and what doesn't, and `hangar-agent env request` to ask the owner for a secret or a host.
  Requests are never granted automatically, and the agent keeps working on everything they don't block.

## Turning the computer on

```yaml
# envs/web.yaml
name: web
extends: node
computer: true            # or a map:
# computer:
#   resolution: 1440x900  # the display, the browser window and the recording frame (default 1280x800)
#   autostart: true       # start the display and VNC at claim and every turn (default)
#   browser_mcp: false    # Playwright's MCP server for every harness (adds Node 22 to the image)
#   record_max_seconds: 300
```

The image gets `xvfb`, `x11vnc`, `openbox`, `ffmpeg`, `xdotool`, fonts, and Playwright for Python pinned to
`playwright==1.63.0` with its Chromium in `/opt/hangar/computer/browsers` (`PLAYWRIGHT_BROWSERS_PATH`). That is
about 400 MB, which is why it is opt-in. It needs a Debian or Ubuntu base (apt). `browser_mcp: true` also installs
`@playwright/mcp@0.0.83` and the Chromium build its own Playwright wants.

At claim, and again at the start of every turn (a restore or resume brings no processes back), hangar starts
the display `:99`, the window manager and `x11vnc` on 127.0.0.1:5900, and sets `DISPLAY=:99` for every shell.
Starting is idempotent and takes about a second.

## What the agent runs

| Command | What it does |
|---|---|
| `hangar-agent computer start\|stop\|status` | the display, window manager and VNC server |
| `hangar-agent screenshot [URL] [--name N] [--full-page] [--wait-for SEL]` | a page (headless Chromium) or, without a URL, the whole display |
| `hangar-agent record start [--name N] [--max-seconds S]`, `record stop` | an MP4 of the display (10 fps, H.264, fragmented so a killed recorder still leaves a playable file) |
| `hangar-agent browse URL [--wait-for SEL] [--text] [--screenshot N]` | open a page; print status, title, console errors, page errors and failed requests as JSON |

Inside a job or session turn, screenshots land in the turn's artifacts as `screenshots/<name>.png` and recordings
as `demos/<name>.mp4`, through the existing [context exchange](context-exchange.md) (`hangar-agent artifact`).
Recordings are capped below `HANGAR_EXCHANGE_MAX_ARTIFACT_MB` and at `record_max_seconds`. Outside a turn they stay
in `~/.hangar/computer/`.

Sandboxes with a computer also get the **`hangar-verify-ui` skill** in the harness's skills directory. It walks the
agent through running the app, checking the page loads cleanly, driving the changed flow with a Playwright script,
and recording a short demo. Job prompts in such environments ask for exactly that when a change affects the UI.

### Where the media goes

- **Session events:** a `progress` event per screenshot or recording, with its `hangar://` artifact URI.
- **The job's PR body:** a "Screenshots and demos" section. By default (`HANGAR_PR_MEDIA_LINKS=off`) it lists the
  files and how to fetch them through hangar, signed in: `hangar session artifacts <session> --get <path>`, MCP
  `get_artifact`, or `GET /api/jobs/<id>/artifacts/<turn>/<path>`. Those are owner-scoped like every record, so
  only the job's owner and admins can open them.

  `HANGAR_PR_MEDIA_LINKS=signed` embeds images and links recordings instead, through signed links
  (`/api/media/<token>`) that name one artifact and expire after `HANGAR_PR_MEDIA_LINK_DAYS` (7). **The link is the
  credential:** anyone who can read the PR (or its notification emails) and reach hangar can open the file, and
  GitHub's image proxy fetches and caches embedded images. A screenshot of a private repo's app can show its code,
  data or admin pages, so turn this on only where everyone who reads the PRs may see the work (a public
  repository, say). Turning it back off revokes every link already posted: `/api/media/` answers only while it is
  on.

  Why not embedded and authenticated: GitHub renders PR images through its own proxy, which can't carry a hangar
  login, and the dashboard signs in with a bearer token rather than a cookie, so an authenticated link can't be an
  image in a PR or a plain click. Names plus the signed-in ways to fetch them are the safe default.

## Watching and taking over

```sh
hangar screen job-1a2b3c4d            # opens a view-only screen in your browser
hangar screen my-session --control    # your keyboard and mouse reach the sandbox
```

REST: `POST /api/screen/tickets {"ref", "control"}`. Each returns a link to `/ui/vnc.html` with a one-time ticket in
the URL fragment (browsers never send fragments to servers), valid for 60 seconds. The page is a
[noVNC](https://github.com/novnc/noVNC) 1.7.0 client. There is no MCP tool for it: claude.ai caches tool lists
indefinitely and hangar's listing is at its size cap, and a person opens a screen in a browser anyway.

How it stays private:

- **No port is exposed.** `x11vnc` listens on 127.0.0.1 inside the sandbox. hangar opens the provider's terminal
  stream into the sandbox (Docker exec, the Kubernetes exec websocket, or envd) and runs `hangar-computer bridge`,
  which relays the VNC connection as base64 lines. Lines survive every transport, including Kubernetes' exec
  stream, which decodes output as UTF-8.
- **Owner-scoped.** A ticket is minted only for the session's, job's or sandbox's owner, or an admin (a teammate's
  gets "not found", as everywhere else, #88). Each ticket is single-use (redeeming removes it) and only its hash is
  kept, so it can't be replayed, and a ticket that reaches a log (the websocket URL carries it) is already spent.
- **A ticket is a bearer for its 60 seconds.** Whoever opens the link first gets the screen, with the control the
  ticket was minted with; the websocket can't carry a hangar login, so the ticket can't be bound to the person who
  asked for it. Sharing one shares the screen once. The audit records where each was used from.
- **View-only is enforced by hangar.** Without `control`, hangar parses the viewer's side of the protocol and drops
  key, pointer, clipboard, resize, xvp and QEMU key messages. It ends the stream on anything it can't parse. The
  browser's view-only flag is not trusted.
- **Audited.** `screen.ticket` (who asked, view or control), `screen.view` / `screen.control` (with the client
  address) and `screen.end` (with how long) go to the audit log, and the session gets an event ("ops is watching
  the screen", "... took control of the screen"). At most 4 viewers per sandbox.
- **Inside the sandbox, VNC is open to everything.** `x11vnc` runs with `-nopw` on 127.0.0.1, and the X display has
  no access control, so any process in the sandbox can watch and drive the screen: the agent, its tools, a test
  server, and on Kubernetes the pod's sidecars too (they share its network namespace). That's accepted: they are
  all the agent's own processes, which already own the display. Nothing outside the sandbox can reach it.

## Under gVisor

GKE runs sandboxes under gVisor. What that means for the computer, and what hangar does about it:

| Limit | What hangar does |
|---|---|
| Chromium's own sandbox (its seccomp-bpf layer) can't start under gVisor's seccomp | Chromium runs with `chromium_sandbox=False` (Playwright's default too, and `--no-sandbox` for the browser MCP). gVisor is the isolation boundary, as for every agent process. |
| `/dev/shm` is small (64 MB is the container default) | `--disable-dev-shm-usage` whenever `/dev/shm` is under 1 GB, so renderers use `/tmp` instead of crashing. |
| No GPU | `--disable-gpu`; Chromium renders in software. |
| Chromium is syscall-heavy, and gVisor intercepts every syscall | Expect pages and screenshots 1.5–2x slower than under runc; the skill tells agents to raise timeouts rather than retry blindly. |
| MIT-SHM between X clients | `x11vnc -noshm` under gVisor. |

**What running Chromium without its sandbox costs.** Chromium's sandbox keeps a compromised renderer (a malicious or
hijacked page plus a Chromium bug) away from the rest of the machine. hangar turns it off on every runtime, so a
page that exploits Chromium runs code as the sandbox user, with everything the agent has: the workspace, the
credentials injected into the sandbox (the model login, git and MCP tokens in `~/.hangar/env`), and whatever the
egress policy lets out. What stops it there:

- **Under gVisor (GKE), gVisor is the boundary.** The exploit is in a user-space kernel, not on the node, which is
  the same boundary as for any code the agent runs (an `npm install` postinstall script has the same reach).
- **Under runc or Docker (local, or GKE without gVisor), only the container is.** That shares the host's kernel.
  Don't point agents at untrusted sites there.
- **Either way, limit the reach:** an egress allowlist for the environment, credentials scoped to the work and
  short-lived, and browsing only the app under test and what it needs. Playwright is pinned and should be bumped
  with Chromium security releases.
- **Encrypted ClientHello is off.** The egress proxy refuses ClientHellos with ECH, which would hide the site
  behind its CDN's name ([security.md](security.md#egress-allowlists)), and Chromium sends a GREASE ECH extension by
  default. The image sets the `EncryptedClientHelloEnabled: false` managed policy for Chromium, Chrome and Chrome
  for Testing, and `hangar-agent browse` also passes `--disable-features=EncryptedClientHello`. A browser of the
  agent's own needs the same switch to work through the proxy.

`hangar-agent env doctor` reports the runtime, the `/dev/shm` size and any missing tool, so an agent can tell "the
environment can't" from "my test is wrong". An environment without `computer:` still has `hangar-computer`, which
says so plainly ("Xvfb is not installed: this environment has no computer ...") instead of failing obscurely.

## How each harness drives the browser

Every harness can run shell commands, so `hangar-agent screenshot|record|browse` and Playwright scripts
(`/opt/hangar/computer/venv/bin/python script.py`) work with all of them. Beyond that:

| Harness | Browser | Notes (checked against the vendors' docs on 2026-10-14) |
|---|---|---|
| Claude Code | Playwright scripts; Playwright MCP (`browser_mcp: true`) | Claude Code's own browser integration, [Claude in Chrome](https://code.claude.com/docs/en/chrome), needs a desktop Chrome with the extension and a `/login` session; it stays off with an API key or a `setup-token` login, so it doesn't apply in sandboxes. MCP servers come from `~/.claude.json`. |
| Codex | Playwright scripts; Playwright MCP | Codex's [MCP docs](https://developers.openai.com/codex/mcp) list Playwright as an example server. Stdio servers get only the variables named in `env` and `env_vars`, so the `hangar-env` server finds the turn's directory in its status file instead of `$HANGAR_IO`. |
| Antigravity (agy) | Playwright scripts; Playwright MCP | MCP servers come from `~/.gemini/config/mcp_config.json`. We found no built-in browser or computer use in the CLI. |
| pi | Playwright scripts | hangar writes `~/.pi/agent/mcp.json`, but pi reads MCP servers only through an extension, so rely on the shell commands. |

There is no model-routing subagent: the agent in the sandbox drives the browser itself.

## Self-diagnosis

`~/.hangar/envstatus.json` is written at claim and at the start of each turn. It holds names, versions and the
egress policy, never a secret value; build and setup messages, warnings and owners' notes pass through the
broker's redaction first, so a build secret or a credential that ended up in a log line is masked:

- the environment build (`sb.info["env_build"]`: version, whether it is a **fallback** image and why, whether a
  rebuild is running because the repo moved on);
- the declared services, supervised terminals and preview ports (`sb.info["services"]`), with their checks;
- the secret **names** the environment expects (`secrets:` and the `${VAR}`s its MCP servers use);
- recent setup failures (`sb.info["setup_failures"]`) and claim warnings;
- the egress policy (`egressapi.policy`: mode, allowlist, proxy, and whether it is actually enforced);
- the runtime (gvisor, kata, runc, docker, e2b), the computer, and the requests filed so far with their answers.

The build, services and setup failures come from the environment build and the service supervisor
(`hangar-agent svc`). Where they recorded nothing, the doctor says "unknown" rather than guessing.

```sh
hangar-agent env doctor                     # every check; exit 1 if something fails
hangar-agent env doctor --host pypi.org --host api.stripe.com:443 --json
```

The doctor checks what it can from inside: it runs each service's check (or connects to its port) and asks the
supervisor for its state, looks for each expected name in the environment and `~/.hangar/env`, and probes each
`--host` for real. It goes through the egress proxy when there is one (the proxy's 403 means "blocked by policy")
and connects directly otherwise. So "blocked by policy", "DNS failed" and "unreachable" are told apart.

The same report is an MCP server named `hangar-env`, written into every harness's MCP config. It is stdio, standard
library only (the image contract already requires `python3`), and speaks the handshake-era MCP versions
(2024-11-05 to 2025-11-25). It shows the agent nothing it can't already read in its own home directory.

- **Turning it off:** `HANGAR_ENV_DOCTOR_MCP=false` for every environment. For one environment, its own server of
  that name wins, so `mcp: {hangar-env: {command: "true", harnesses: []}}` removes it there. `hangar-agent env`
  works either way.
- **If it can't start**, the run goes on without it. hangar never marks it required: Codex stops only for servers
  with `required = true` and waits about a second for optional ones (`mcp_optional_startup_grace_ms`, its MCP docs,
  checked 2026-10-14). Claude Code and agy list a server that failed as failed and run without its tools. It runs
  from hangar's own file and the `python3` every image has, so the likely failure is a broken image, which the
  doctor itself can't help with.

| | |
|---|---|
| `hangar-env://doctor` | the report, without host probes |
| `hangar-env://status` | what hangar recorded |
| `hangar-env://requests` | requests filed and their answers |
| `hangar-env://egress/{host}` | one host probe |
| tool `env_doctor(hosts)` | the report (read-only) |
| tool `env_request(kind, target, why)` | file a request |

The tools are there because some harnesses show MCP resources to people rather than to the model.

## Requests: asking the owner, never granted automatically

```sh
hangar-agent env request secret STRIPE_KEY --why "the payment tests need a test-mode key"
hangar-agent env request egress registry.npmjs.org --why "npm ci fails: the registry is blocked"
hangar-agent env requests                   # what was filed and what the owner said
```

The request is a file under `$HANGAR_IO/out/requests/`. While the turn runs, hangar looks for new ones every
`HANGAR_ENV_REQUEST_POLL_SECONDS` (30; 0 means only when the turn ends) and again when it ends. Each one is
validated (a variable name or a host, and a reason, which is redacted) and recorded in the `env_requests` table.
Only the kind, the name or host and the reason are taken from the file: its state, scope and owner are hangar's.

An agent can't flood its owner. Per session, at most 5 new requests a turn and 5 waiting for an answer; per owner,
30 an hour across all their sessions and jobs; and a host or secret the owner denied isn't asked for again in that
session. Files over a limit are deleted without being read, and the turn gets a warning (at most 3). `hangar-agent
env request` says so before filing when 5 already wait.

The owner is told:

- **an MCP elicitation**, when someone is following the work with `wait_for_turn`: "Job job-1a2b (fix checkout):
  the agent asks for the secret STRIPE_KEY ... Its reason, in its own words: "..."", answered approve or deny with
  a note;
- **a session event and a job event**, and an audit entry (`env.request.secret`, `env.request.egress`);
- `GET /api/env-requests` and `hangar requests ls`; in MCP, `pending_approvals` on get_session, get_job and
  wait_for_turn, next to the tool calls the agent waits on (the id starts with `rq-`).

Answer with `hangar requests approve|deny <id> [--note ...]`, `POST /api/env-requests/<id>`, MCP
`answer_approval(ref, approval=<rq- id>, decision=allow|deny, message=<note>)` or the elicitation. Widening an
egress approval to the whole environment is REST and CLI only (`--scope environment`, admins). Only the work's
owner or an admin can decide. A decision is recorded once, audited, written into the sandbox's status file for the
agent (on a task of its own, so the answer doesn't wait on the sandbox), and added to the session's events.

**Nothing is ever granted automatically.** No path from what a sandbox writes leads to `decide`: it is called only
by a person's REST or CLI call, their MCP call, or their answer to the elicitation.

- **Secret:** approving means "handled". Store the credential and map it in the environment (`secrets:`) first. A
  request never carries a value, and the form says not to paste one.
- **Egress:** the decision goes to `egressapi.on_decided`, which allows the host for the session, or for the whole
  environment when an admin widens `scope` to `environment`.

`egressapi.py` is the interface between this and egress enforcement: the `EgressRequest` record, `policy`, `check`,
`may_widen` and `on_decided`. The version here is a stub that reports `open` and enforces nothing; the egress
allowlist replaces its bodies with the same signatures.

## Prompting for autonomy

Job prompts tell the agent that nobody is watching the run live and a blocked agent sits idle for hours. When the
environment blocks something, the agent runs `hangar-agent env doctor`, files the blocker with `env request`, keeps
working on everything else, and says in its summary what is still blocked. `hangar-agent ask` stays for decisions
only a person can make. In environments with a computer, the prompt also asks for end-to-end verification and a
recorded demo when the change affects the UI.

## Integration points

- **Supervised services (envbuilds).** The doctor reads `sb.info["env_build"]`, `["services"]` and
  `["setup_failures"]`, and calls `hangar-agent svc status --json` when the sandbox has the supervisor. Xvfb, the
  window manager and x11vnc are started by `hangar-computer computer start`. The supervisor can own them as
  supervised terminals instead; nothing else changes.
- **Egress enforcement.** See `egressapi.py` above. `envrequests.decide` is the single decision path, so an egress
  approval API calls it rather than writing rows.
- **An inbox.** `EnvRequests.listeners` is called with every new request; an attention inbox subscribes there.
