# Computer use under gVisor, and self-diagnosis

This page is part of [The agent's own computer, and an environment it can diagnose](../computer-use.md).

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
  behind its CDN's name ([security/egress.md](../security/egress.md)), and Chromium sends a GREASE ECH extension by
  default. The image sets the `EncryptedClientHelloEnabled: false` managed policy for Chromium, Chrome and Chrome
  for Testing, and `hangar-agent browse` also passes `--disable-features=EncryptedClientHello`. A browser of the
  agent's own needs the same switch to work through the proxy.

`hangar-agent env doctor` reports the runtime, the `/dev/shm` size and any missing tool, so an agent can tell "the
environment can't" from "my test is wrong". An environment without `computer:` still has `hangar-computer`, which
says so plainly ("Xvfb is not installed: this environment has no computer ...") instead of failing obscurely.

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
