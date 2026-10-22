# What a compromised sandbox does not get

hangar assumes the agent inside a sandbox can take it over completely. This page lists what stays out of its reach
even then. It is part of the [security model](README.md).

The attacker controls everything inside one sandbox: the agent's process, its files and `~/.hangar/env` (so every
command hangar runs there). It also controls its git binary and its network traffic. Six things stay out of reach.
The [threat model](threat-model.md) lists every goal, what stops it and the test that checks it.

## 1. The GitHub token

With the [git credential gateway](git-gateway.md) the token stays in the control plane. A sandbox gets a grant that
works only at hangar's gateway, only for its repo, only while it runs.

Every job's sandbox holds a fetch-only grant. hangar pushes the commit it verified, after the secret scan and the
landing checks, with a grant pinned to that ref and commit for minutes. Builds and validations hold nothing else.

## 2. A host of its choosing

In `allowlist` and `none` environments the network admits only the gateway and the [egress proxy](egress.md), with no
DNS. The proxy checks each connection's host by name, reads the whole ClientHello and refuses Encrypted ClientHello.

`pypi`, `npm` and `go` are served by hangar's read-only package mirror, so the registries' upload hosts stay closed.
Approvals come from the owner, never from the agent.

## 3. A secret in a branch, a PR or a thread

Before a push, the change and its commit messages are [scanned](secret-scanning.md). The scan looks for stored values,
their encodings and high-signal token formats. A hit blocks the push and goes back to the agent with file and line,
never the value.

Every push through the gateway, the agent's own included, is scanned again from the pack. Results, events, artifacts
and audit entries are [redacted](secret-scanning.md#redaction).

## 4. A credential it can keep

[Credentials](credentials.md) are written into a running sandbox at claim time, never into an image. Control-plane
credentials (webhook secrets, the Slack token, GitHub App keys) never enter a sandbox by any path. Build secrets reach
only the build step.

A `cloud:` environment gets a per-sandbox OIDC token that the cloud exchanges for short-lived credentials; no cloud
key is stored. Subscription logins go only into sandboxes a person starts for themselves.

## 5. Its own work landed

Once an agent has read untrusted content (a ticket, a web page, an untrusted MCP server), the turn is
[tainted](taint.md) for good. Its PR opens as a draft labelled `hangar:tainted`, and a second person must approve it to
land. Its egress requests need an admin or a repo approver.

One approval rule holds on every path, and never the job's own person or requester. Intake jobs run under
[posture](front-doors.md#intake-posture) `enforce`: no gateway, no job; at least `allowlist` egress; no sudo.

## 6. The grade of its own trial

An experiment's [grader](graders.md) runs sealed. It has egress `none` with no host at all, and no credential. Its git
grant is revoked before the patch's code runs, and hidden tests come from a pristine copy. hangar's own user writes the
grade, where the sandbox has the uid split.

## Shared infrastructure

Shared infrastructure gets the same treatment. [Build caches](build-caches.md) are written only by a repo's own builds
and sealed with the vault key. The [agent's screen](screen.md) is reached only through hangar. The
[vault key ring](vault-key.md) encrypts everything at rest and can be rotated.
