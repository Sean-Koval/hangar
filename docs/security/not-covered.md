# What the security model does not cover

The gaps, stated plainly, so nobody relies on a guarantee hangar doesn't make. The [threat model](threat-model.md) lists what is covered; this is part of the [security model](README.md).

What is **not** covered:

- **Docker on a laptop or VM is a trusted-use setup.** Sandboxes can't reach each other's ports ([networking](docker-network.md)), but they can reach the host (and the API port if hangar listens beyond loopback). They get `sudo` only in an environment that sets `sudo: true` (off by default since #124), and a job's sandbox runs with `no-new-privileges` under `HANGAR_INTAKE_POSTURE=enforce`, so sudo fails there anyway. On a cloud VM, block the metadata server as the single-VM install does (the rule above). For untrusted work use GKE (gVisor/Kata).
- **Inside the sandbox the agent is in charge.** hangar's own reads and writes there (attachments in, results and artifacts out) follow whatever the agent left in place. On Docker with `sudo: true` that grants nothing new; without sudo (the default), a symlink placed in `$HANGAR_IO` can make hangar write an attachment, or read a file, as root inside that container.
- **Without the gateway** a job's sandbox holds the environment's git token (`credential_mode: direct`) and can push wherever it reaches; landing never lands such a job. Builds and validations then hold no git credential, so private repos can't build.
- **Secrets the sandbox already holds.** The model credential is still injected. Redaction masks stored values and a few common encodings of them in results, artifacts and logs, but not every transformation ([redaction](secret-scanning.md#redaction)). A grant the agent copies out works from anywhere that reaches the gateway until its sandbox stops.
- **Secrets a scan doesn't recognize.** Pushes through the gateway are scanned, the agent's own included, but a secret encoded in a way the patterns don't know (or split, compressed, inside a binary file) gets through, as it does past [redaction](secret-scanning.md#redaction). A direct-mode sandbox pushing with its own token isn't scanned at all.
- **What else is in the pushed branch.** Review PRs, and keep secrets out of `push`-triggered workflows.
- **Egress.** Sandboxes in `open` environments can reach the public internet, except those of [intake jobs](front-doors.md#intake-posture) under `enforce`, which get at least `allowlist`. In `allowlist` and `none` environments, data can still go *to* an allowed host, in a URL or a body: the harness's own model API with the attacker's key is always allowed, and so are hosts in `allow`, approved hosts, MCP servers and the cloud token exchange. A host on a CDN that doesn't check `Host` against SNI (Fastly, Akamai: [what egress doesn't stop](egress.md#what-egress-doesnt-stop)) reaches any other site there. Without the gateway, the `pypi`, `npm` and `go` presets open the registries themselves, which take uploads; the sandbox says so. Which packages a sandbox downloads is a low-bandwidth signal in public download statistics.
- **Custom branches created outside the gateway.** A session refuses a custom branch another person's sandbox created ([branch creators](branch-creators.md)). A branch pushed from a laptop or a direct-mode sandbox has no record, so a session continues it.

## Accepted in the 1.0 review

These findings from the [1.0 security review](review-1.0.md) are mitigated, not closed. [Open findings and accepted risks](review/risks.md) has the owner of each.

| Finding | What is not covered | What limits it |
| --- | --- | --- |
| Approvals inside a turn on GKE and E2B (H-1) | Without the uid split, the agent shares a user with the driver. It could answer its own approval in an `ask` or `deny-risky` turn | `HANGAR_UID_SPLIT=required` refuses those turns. The Kubernetes manifests set it |
| Codex rules written during a turn (M-4) | A `.rules` file the agent writes mid-turn lets a Codex call run unasked | A rules file present when the turn starts refuses the turn |
| Tower decides only when governance is on (L-1) | `HANGAR_GOVERNANCE` is `off` by default, so Tower is never asked. Job release and landing don't ask it yet (KAN-33) | hangar's own approvals, gateway and landing checks decide, as before |
| The console's token and HTML (L-7) | The admin token sits in `sessionStorage`, which a script on the page can read. About 50 audited HTML sinks rely on `esc()` | A strict Content-Security-Policy runs no inline or outside script. `tests/test_ui_csp.py` fails on a new sink |
