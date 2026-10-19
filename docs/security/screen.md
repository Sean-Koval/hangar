# Screens, previews and the agent's requests

How screens, previews and the agent's own requests are reached only through hangar. Part of the [security model](README.md).

- **Screens** ([computer-use.md](../computer-use.md)): VNC listens on 127.0.0.1 inside the sandbox and is reached only
  through hangar, over the provider's own exec stream; no port is exposed. Tickets are single-use, last 60 seconds,
  are minted only for the work's owner or an admin, and are stored hashed. Watching is view-only unless the ticket
  asked for control, and hangar enforces that by dropping the viewer's input messages itself. Every ticket, view
  (with the client address), takeover and disconnect is audited. A ticket is a bearer for its 60 seconds: whoever
  opens it first gets the screen.
- **Inside a sandbox, VNC and the X display have no access control.** Any process there (and, on Kubernetes, the
  pod's sidecars) can watch and drive the screen. They are the agent's own processes, so this is accepted.
- **Chromium runs without its own sandbox** on every runtime (it can't start under gVisor). A page that exploits
  Chromium gets the sandbox user and everything the agent has (workspace, injected credentials, allowed egress).
  Under gVisor, gVisor is what keeps it off the node; under runc or Docker only the container does. Limit the reach
  with an egress allowlist and short-lived, narrowly scoped credentials; don't browse untrusted sites under runc.
- **Media in PR bodies** is listed by name by default and fetched through hangar, signed in and owner-scoped.
  `HANGAR_PR_MEDIA_LINKS=signed` embeds it through signed links (HMAC with a key derived from the vault key) that
  name one artifact and expire (`HANGAR_PR_MEDIA_LINK_DAYS`). Such a link is its own credential: anyone who reads
  the PR and can reach hangar opens it. Use it only where every PR reader may see the work; turning it off revokes
  them all.
- **Requests** (`hangar-agent env request`) carry a variable name or a host and a redacted reason, never a value.
  They are never granted automatically: only the owner or an admin decides, once, and every decision is audited.
  A request file can't set its own state, and an agent can't flood its owner (5 new a turn, 5 waiting per
  session, 30 an hour per owner, no re-asking a denied target).
- **What a sandbox learns about itself** (`~/.hangar/envstatus.json`, the `hangar-env` MCP server) is names,
  versions, health checks and the egress policy, never a secret value; messages in it are redacted like logs.

