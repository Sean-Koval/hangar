# Running an environment on your computer

How `hangar env local` starts an environment spec on a laptop the way hangar starts it in the cloud. Use it when a
local run must count as evidence about a cloud session.

## What it uses

The local run uses hangar's own code, so it can't drift from a hangar sandbox:

- **The image** is `envspec.render`'s build: the Dockerfile and context a hangar build gets. The harness is baked
  in, since there are no pack mounts on a laptop (`--harness none` leaves it out).
- **The allowlist** is `egress.resolve`'s: the policy a hangar sandbox of the spec gets, with no package mirror and
  no approvals. `hangar env local policy` prints it.
- **The sidecar** enforces it with the rule match and address checks of hangar's [egress proxy](../security/egress.md).
  It is `egress_sidecar.py`, run in `python:3.12-slim` pinned by digest (the control plane's base image).

## The commands

```sh
hangar env local up agent --env-dir environments --harnesses harnesses \
  --contract environments/agent.env.example --clone code=CODE_REPO --report-cmd 'ai-dlc doctor'
hangar env local exec -- claude      # a command in the running instance
hangar env local check agent ...     # the acceptance checks below
hangar env local down
```

`--contract` names the variables passed in, besides the spec's `secrets`. Only names set in your shell are passed,
through an env file, never on the command line. `--clone DIR=VAR` clones `$VAR` to `/workspace/DIR` through the
proxy. `--extra-ca FILE` trusts a CA in the image, for a network that intercepts TLS.

## The agent CLI and its MCP servers

The baked harness is set up as a hangar claim sets it up. Its pinned CLI goes first on `PATH`, so
`hangar env local exec -- claude` works.

The spec's `mcp` servers go into the harness's config in the container (`~/.claude.json`, mode 600). Each `${VAR}` is
filled from the passed variables, over stdin. A server whose variable isn't set is skipped, and the log says so.

Claude Code takes `CLAUDE_CODE_OAUTH_TOKEN` (from `claude setup-token`, a subscription login) or
`ANTHROPIC_API_KEY`. Name the one you use in the contract.

`--release-config` refuses to start when the agent's Atlassian account is a person's
([agent identity](agent-identity.md)).

## The container and its network

The agent container mounts nothing from the host: no home directory, checkout or Docker socket. It sits on an
internal Docker network, which has no route out. Its only neighbour is the sidecar, which is also on Docker's
default bridge.

The sidecar serves two things:

- **An HTTP CONNECT proxy** (`HTTPS_PROXY`). Anything but CONNECT gets 405, so plain HTTP fails closed. The host and
  port must match the allowlist, and the name must resolve to public addresses only. The tunnel goes to an address
  it checked.
- **A DNS server that refuses every query.** The agent's `--dns` is the sidecar. Docker's resolver answers container
  names itself and forwards every other name there, where it is refused.

So no name on the internet resolves inside the agent, allowed or not. Tools reach allowed hosts by name through the
proxy, which resolves them itself. DNS can't carry data out, and nothing reaches port 53 anywhere else. Recent
Docker already stops forwarding for internal networks; the sidecar makes that hold on every version.

## The checks

`hangar env local check` starts two fresh instances and fails unless all of these hold:

- both print the same report: the model, the spec's `env`, each clone's commit and `--report-cmd`'s output;
- neither has a mount;
- an allowed URL (`--allowed-url`, default GitHub) is reached through the proxy;
- an off-list host, plain HTTP and a request that skips the proxy are refused;
- `getent hosts` fails for an off-list host and for an allowed one;
- a DNS query sent straight to `1.1.1.1:53`, over UDP or TCP, gets nothing back;
- the agent probe: `claude --version` is hangar's pin, and `claude -p "say ok" --output-format json` succeeds in the
  first instance. Without a Claude credential it fails and says which variables to set. `--no-agent-probe` skips it.

## On macOS

Egress is enforced the same way on macOS, by construction. Docker Desktop runs every container in its Linux VM, and
the internal network, the sidecar and the `--dns` setting all live inside it. Nothing depends on the Mac's own
firewall or resolver.

That is a design claim until it is measured. `hangar env local check` on a Mac is the qualification: its DNS checks
are the ones that would catch a resolver leak. Its egress and DNS checks have passed on Linux (Docker 29.9); the
macOS run is tracked on KAN-472. Colima and OrbStack also run containers in a Linux VM, but are unqualified.

## What it doesn't do

The sidecar is a subset of the control plane's proxy. It doesn't read the TLS ClientHello, so it doesn't check
that SNI names the CONNECT host. It has no per-sandbox credential, audit log or approvals: on a laptop the one
instance is the only client.
