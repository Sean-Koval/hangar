# The harness canary

This page is part of [Harnesses: versions, the contract, the canary and models](../harnesses.md).

Every night `harness-canary.yml`:

1. **Resolves the newest stable release of each harness** from the sources the lock was verified against:
   - Claude Code: `downloads.claude.ai/claude-code-releases/stable`, then that version's `manifest.json`;
   - Codex and pi: npm's `latest` dist-tag (for Codex, the linux-x64 and linux-arm64 builds must be published);
   - Antigravity: the updater's `manifests/linux_{amd64,arm64}.json`, with the build id from its `/releases` list.

   A harness only moves forward. Claude Code's stable channel can trail the lock (2.1.292 against the locked 2.1.296
   on 2026-10-14); that is reported as `behind` and nothing changes.
2. **Checks the checksums, and where they come from.** A checksum from the server that serves the binary proves
   little, so each source is checked against something that server alone can't forge:
   - **Claude Code:** the version's `manifest.json` must carry a good signature (`manifest.json.sig`) by Anthropic's
     Claude Code release key. The key is checked in (`src/hangar/keys/claude-code.asc`) with the fingerprint
     code.claude.com/docs/en/setup publishes (`31DD DE24 DDFA B679 F42D 7BD2 BAA9 29FF 1A7E CACE`), and is never
     fetched from the release server. Then each binary, for both architectures, must match the signed sha256.
   - **Codex and pi (npm):** `npm audit signatures` checks the registry's signatures and the Sigstore provenance
     attestations of everything the candidate installs (with `--ignore-scripts`, for linux-x64 and, for Codex,
     linux-arm64). Each package the recipe installs must also carry SLSA provenance naming it, built from its own
     repository (`github.com/openai/codex`, `github.com/earendil-works/pi`).
   - **Antigravity:** Google publishes no signature (no `.sig`, `.asc` or checksum file beside the archives, checked
     2026-10-14). The sha512 comes from the updater's manifest, a separate service from the bucket that serves the
     archives, and each archive must match it. That is the whole of it, and the packet says so.

   It also checks that the updater serves the URL the install recipe would build from the version and the build id.
3. **Installs the candidate CLIs and runs the contract tests against them**, in a job with a read-only token and no
   secrets. A process in a job can read the job's token and every secret the job references, so nothing an upstream
   release ships (npm install scripts, the new binaries, the tests that run them) runs in the job that pushes and
   holds the App key. The unchanged CLIs come from the contract workflow's cache.
4. **Diffs the protocols** against the recorded (locked) ones. A *breaking* change is one where something the driver
   sends or reads is gone: a method, a field, or a value such as an approval decision or a sandbox mode. Everything else
   is listed for the reviewer.
5. **If everything passed, opens one lock-bump PR** from `canary/harness-lock` (or updates the open one). A separate
   `publish` job does it, which runs none of the candidates' code: it checks what the first job handed it (`canary
   check`: a valid lock, two JSON records) and commits exactly those three files. It changes the lock (with one `# Canary <date>:` line in its header) and the protocol records. Its body is the review packet:
   versions and sources, checksum checks, test results, and the protocol diff. A newer candidate on a later night
   turns off auto-merge on the old head before it pushes, and the same candidate only refreshes the packet.
6. **If anything failed, opens one issue** labelled `harness-canary` (or comments on the open one). It has the
   failing tests with their output, the protocol diff, and resolver errors.

`workflow_dispatch` with `dry_run` does everything but open the PR or issue. The packet is in the run's summary and
its `harness-canary` artifact.

## Landing the bump: one approval, never without it

The bump PR lands the way an agent's PR does with `land: auto_after_approval` ([landing.md](../landing.md)). It runs in
CI rather than in the control plane, so it works with the cluster down. On an approval, `harness-canary-land.yml`
checks it from the default branch's code, not the PR's. These must all hold:

- the PR is the canary's: head `canary/harness-lock` in this repository;
- the approval is of the PR's current head, by an approver: the `CANARY_APPROVERS` repository variable
  (comma-separated logins), else the repository's owner;
- the approver isn't the PR's author, and nobody's latest review requests changes;
- **the head is the canary's own push**: GitHub's record of the branch's pushes (`GET /repos/…/activity`) says a bot
  pushed it. The author is always a bot, so "not the author" alone would let an approver push a change onto the bump
  and approve it themselves. A person's commit on the canary branch is landed by hand, by someone else;
- the PR changes exactly `src/hangar/harnesses.lock.yaml`, `tests/fixtures/protocol/codex-app-server.json` and
  `tests/fixtures/protocol/claude-stream-json.json`, and removes none of them (hardening-r2). Not the directory: pytest
  collects `tests/` recursively, so a `conftest.py` or `test_*.py` among the fixtures would run in CI.

When they hold, it arms auto-merge for exactly that commit (`expectedHeadOid`), or merges it at that commit when it is
already mergeable. GitHub's branch protection still decides when it merges: required checks, stale approvals
dismissed on a push. Anything else, and nothing is armed; the run's summary says why.

## Setup

- **Branch protection on `main`**, as [landing.md §3](../landing.md#3-the-branch-protection-to-enable) describes, and
  *Allow auto-merge*. Without required approvals, auto-merge would merge a bump without anyone's approval. The
  land workflow still never arms one without an approval.
- **A GitHub App for the PR** (recommended): repository secrets `CANARY_APP_CLIENT_ID` and `CANARY_APP_PRIVATE_KEY`
  for a dedicated App with Contents and Pull requests read and write, installed on this repo only. Don't reuse
  hangar's own App: its key would sit in this repository's secrets with that App's wider reach. Without Workflows
  permission it can't change `.github/workflows/`, which is what you want. The PR then comes
  from the App, so `pr.yml` and `harness-contract.yml` run on it, and the merge's push runs `ci.yml` and
  `images.yml`, which builds the new packs. Without the App, the PR is opened with the workflow token. GitHub runs no
  workflow for it, so close and reopen the PR to run the checks, and run `images` by hand after the merge. The PR body
  says so.
- **`contract` as a required check** on `main`, so a bump whose locked CLIs don't reproduce the recorded protocols
  can't merge.
- Optionally, the `CANARY_APPROVERS` repository variable.

## After a bump

`images.yml` builds the new packs on the merge's push. A new CLI version applies to **sessions created after the
bump**: a session records the locked version when it is created (`harness_version` on the session). A follow-up on
an older session that lands on a sandbox running the newer CLI is treated as a harness switch. The turn hands off (a
summary of the conversation in a new conversation) instead of resuming a conversation written by another version. The
turn carries a warning, the session's activity shows it, and `session.harness_version` is audited. A sandbox that was
already running keeps its pack, so its session resumes as before. Sandboxes that bake the harness into the image don't
record `harness_lock`, so for them nothing changes.
