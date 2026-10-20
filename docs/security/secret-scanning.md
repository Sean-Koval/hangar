# Secret scanning and redaction

What hangar scans before a push and masks in everything it shows, and what gets past both. Part of the [security model](README.md).

## Secret scanning

Before hangar pushes a job's branch it scans what would leave:

- each new commit's own patch, since `git push` sends every commit: a token one commit adds and a later one deletes still leaves, so the net diff since the base isn't enough. Commits the remote already has don't count;
- the uncommitted change and untracked-but-not-ignored files (what hangar would commit). Added lines only;
- the new commits' messages.

What counts as a secret:

- any stored credential's value, as is or encoded ([redaction](#redaction));
- a match for a high-signal token format: GitHub (`ghp_`/`gho_`/`ghu_`/`ghs_`/`ghr_`, `github_pat_`), Slack (`xox?-`, `xapp-`, webhook URLs), AWS access key ids (`AKIA`, `ASIA`) and `aws_secret_access_key = …`, Google API keys (`AIza`), OAuth client secrets (`GOCSPX-`) and service-account key ids, Anthropic (`sk-ant-…`), OpenAI (`sk-proj-`, `sk-svcacct-`, `sk-admin-`, legacy keys), PEM and OpenSSH private keys (the header and the start of a body: code that checks for the header isn't a key), npm (`npm_`), PyPI (`pypi-`), GitLab (`glpat-`), Stripe live keys, Hugging Face (`hf_`), and hangar's own `hgr_`/`hgs_` tokens. Slack tokens need Slack's numbered shape, so `xoxb-test-token` fixtures pass.

Documentation placeholders such as `AKIAIOSFODNN7EXAMPLE` are ignored.

**On a hit the push doesn't happen.** The agent gets a follow-up turn listing each file and line (or commit) and the kind of secret, never the value. It is asked to remove the secret, rewrite the commits that carry it, and finish; hangar then scans again. After `HANGAR_SECRET_SCAN_ROUNDS` (2) such turns, or when the job's budget is spent, the job fails with reason `secret_found`, and nothing is pushed. Every hit is audited as `secret.found` (locations and kinds only). A best-of-N attempt that holds a secret isn't pushed either. `HANGAR_SECRET_SCAN=warn` turns blocking into job events, and `off` turns scanning off.

**Text hangar posts is masked.** PR titles and bodies, and comments on GitHub issues and in Slack threads, have every secret replaced with `[hangar: secret removed]`, and a job event says what kind was removed.

**Artifacts** handed back by the agent are masked the same way when they are text. A binary artifact that contains a stored credential's value is withheld, with a warning on the turn.

**Large diffs are fast to scan.** hangar reads at most 64 MB of a change. Each pattern makes one pass over the text; each starts with a literal, which CPython's regex engine finds with a fast scan, or runs only when a substring it needs is there. File and line are worked out only for hits. A 60 MB change scans in about half a second.

**Not covered:**

- binary files in the diff;
- a secret split or encoded in a way the patterns don't know;
- anything past the first 64 MB of a change.

The sandbox's own git produces the diff, so a compromised sandbox can hide things from this scan. The scan exists to catch accidents, the common case: a `.env` file or a pasted token. What leaves through the gateway is scanned again there, from the pack itself (below).

### The agent's own pushes

A session's agent pushes on its own, through the [git gateway](git-gateway.md), and the sandbox can't be trusted to scan that. So the gateway reads the whole push and scans it before forwarding any of it (`pushscan.py`):

- **Self-contained packs.** The gateway adds `no-thin` to receive-pack's capability advertisement ("A client MUST NOT send a thin pack if the server advertises the no-thin capability", [gitprotocol-capabilities](https://git-scm.com/docs/gitprotocol-capabilities)), so every delta's base is in the pack and every object can be rebuilt without a copy of the repository. A pack that still refers to an object outside itself is refused.
- **What it reads.** Every object of the pack, inflated within a budget (4x `HANGAR_GATEWAY_SCAN_MAX_MB`) and with its deltas applied; the pack's checksum must match, so what was scanned is what GitHub gets. It scans the new commits and tags as written (messages and every header), file names, and the text of every new file at the path the new trees give it. Binary files are skipped, as the delivery scan skips them.
- **Only what is new.** A new blob is a whole file, so a hit in a file that held the same text in the commit the push builds on doesn't count: the gateway reads that file from GitHub's contents API with its own credential, only when there is a hit. If it can't tell, the hit stands.
- **On a hit the push is refused,** the way git shows a refusal: `remote:` lines naming each `file:line` and kind (never the value), and every ref `! [remote rejected] (hangar: possible secret: …)`. Nothing reaches GitHub. It is audited as `secret.found` with `source: gateway`, and shown in the session's events. `HANGAR_SECRET_SCAN=warn` lets it through, audited; `off` turns it off.
- **Bounded.** The gateway holds a push (in memory up to 8 MB, then on disk) of at most `HANGAR_GATEWAY_SCAN_MAX_MB` (64), and reads it into memory only once one of the two scan slots is free. Every object counts 256 bytes against the budget besides its contents, and a delta that says it builds more than what is left of the budget is refused before it is built. A bigger push, a pack that can't be read, or one with more text than the scanner reads (64 MB) or more than 1,000 possible secrets, is refused in block mode, since hangar doesn't forward what it couldn't check, and let through with a `secret.unscanned` entry in warn mode.
- **Fast at 60 sessions.** The parse and the scan run in a worker thread, not on the event loop that serves every sandbox's git and proxy traffic, two pushes at a time, in one pass per pattern over all of a push's text. A 17 MB push of source code parses in about 0.15 s and scans in about 0.45 s; a push of a few files takes milliseconds.

A determined agent can still encode a secret in a way the patterns don't know: like the delivery scan, this one catches accidents and careless exfiltration, not every one. A direct-mode sandbox pushing with its own token isn't scanned at all.

## Redaction

Tool results, events, turn summaries, artifacts and audit entries are passed through `broker.redact`. It masks every stored credential value (8 characters or more) as `***`. For values of 12 characters or more it also masks cheap, common encodings:

- the value reversed (`rev`, hardening B4);
- hex, in lower and upper case (`xxd -p`);
- base64, standard and URL-safe, at each of the three alignments a value can have inside a longer string, so `echo "X=$SECRET" | base64` is caught as well as `printf %s "$SECRET" | base64`;
- percent-encoding, with or without `/` escaped, in upper- or lower-case escapes.

Line-wrapped output, such as `base64`'s 76 columns and `xxd -p`'s 60, is matched across the line breaks. Below 12 characters an encoded form would match too much ordinary text.

**What redaction can't stop:**

- any transformation not on that list: gzip, base32, an XOR, two layers of encoding, every other character, a value split across commands;
- secrets hangar never stored;
- a secret sent out over the network instead of printed.

An agent that can read a secret can always get it out in a form redaction doesn't know. Redaction keeps secrets out of logs, transcripts and results by accident. The protections that hold against a determined agent keep the secret out of the sandbox altogether: the [git gateway](git-gateway.md), [build secrets](credentials.md#build-secrets), [short-lived cloud credentials](credentials.md#short-lived-cloud-credentials) and control-plane-only credentials.

