# Build caches as shared infrastructure

Shared dependency and compile caches as a trust boundary. Part of the [security model](README.md); how to use caches is on [caches.md](../caches.md).

Dependency and compile caches ([caches.md](../caches.md)) are shared infrastructure. Members, branches and repos of
different trust all use them, and what one sandbox writes another sandbox builds from. A poisoned entry (a
`node_modules` with a backdoor, a Bazel action result naming a malicious output, a turbo artifact with a different
`dist/`) runs in the next sandbox that restores it, under that person's credentials, and can end up in their PR. So
the rules are about who can write what other sandboxes read.

**Assets.** A repo's shared cache layer, which every sandbox of the repo reads. Each session's private layer. The
cached bytes themselves (dependencies and compile output of private repos). And the control plane's memory and
storage.

**Adversaries.** An agent (or the repo code it runs) in a session or job sandbox: a member's own, another member's,
or one working on an untrusted branch or a PR from outside. Someone who can create sandboxes with labels of their
choosing. A sandbox of a different repo. Someone who can read or write the cache bucket.

| Threat | What stops it | Tests |
| --- | --- | --- |
| A session or job writes the repo's shared layer | Writes from anything but a repo build go to the writer's own private layer, or are refused (403) with `HANGAR_BUILD_CACHE_PRIVATE_WRITES=false`. The shared layer is written only by a sandbox hangar created for a running repo build of that repo, and that build runs the `install` of the default branch (or of the branch the environment names). | `test_buildcache.py` |
| A member's environment writes what everyone's sandboxes read | Members can create environments, and an environment's image and `setup` run in its builds before `install`. Each environment has its own shared layer per repo, written by builds in that environment and read only by sandboxes of that environment. That is the trust a sandbox already places in its environment, which runs its `setup` too. | `test_buildcache.py` |
| Faking a build | A `hangar.build` label is anyone's to set on a sandbox they create. A sandbox is trusted only if the build it names exists, is running, is of the same repo, and created that sandbox (actor `hangar:build:<id>`, which no principal name can be). Agent-led setup's validation sandboxes (an agent-written branch) are never trusted. | `test_buildcache.py` |
| Reading or writing another session's private layer | The layer is named by the session and branch. A sandbox's `hangar.session` label counts only for a session of the same person; otherwise the layer is the sandbox's own. Nothing is promoted from a private layer, and builds don't read private layers. | `test_buildcache.py` |
| Reading another repo's caches | The credential is the sandbox's git grant, which names one repo, and every lookup is within that repo. Chunks live under a per-repo prefix and are never shared across repos, so whether content exists in another repo can't be probed. A delivery grant reads nothing. | `test_buildcache.py` |
| Swapping or damaging stored objects (bucket access, a bad disk) | Chunks are sealed with the vault key (Fernet: encrypted and authenticated) and named by the SHA-256 of their plaintext; every read checks both and the whole object's digest. A failure is a miss and the entry is dropped. `hangar-cache` checks the archive's digest again before extracting. A Bazel CAS write must hash to its name. | `test_buildcache.py` |
| A build secret in a shared cache | Every upload from a build that has build secrets, from the start of `install` (sccache, Bazel and turbo upload while it runs) to the save after it, is scanned in the stream, gzip inflated (bounded), for each value, its longest line, its percent-encoding and its base64 at every alignment. A hit refuses the upload (422) and nothing is stored; so does a format the scan can't look into (zstd, zip, xz, bzip2, 7z, lz4: sccache's entries and turbo's artifacts among them), a damaged gzip stream, or data after one. Such a build writes no sccache or turbo entries to the shared layer. The build's log says which kind failed, never the value. | `test_buildcache.py` |
| A restore overwriting the sandbox's own work | Archives are extracted with `--skip-old-files` into `$HOME` or the checkout only (GNU tar drops leading `/` and `..` members). Directories in the checkout are restored only when git ignores them, and not at all over a repo build's image. | `test_buildcache.py` |
| A session filling the repo's caches to push the shared entries out | Caps per repo and in total. Eviction takes private layers' entries first, then the least recently used, and private entries expire after 3 days unused. 64 requests in flight per sandbox, and objects up to `HANGAR_BUILD_CACHE_OBJECT_MAX_MB`. Cache bytes held in memory are capped (`HANGAR_BUILD_CACHE_TRANSFER_MB`). | `test_buildcache.py` |
| A grant after its sandbox stops | Grants are verified as the gateway verifies them (hashed, constant time, live sandbox only). A verified grant is reused for 5 seconds, so a stopped sandbox's grant works for at most 5 seconds more. | `test_buildcache.py` |

What is **not** covered:

- **The default branch is trusted.** Whatever its `install` writes into the caches (or into the build's image)
  every sandbox of the repo in that environment gets. That is the same trust the repo build itself has: protect the
  default branch with reviews. The same goes for a branch an environment names (`repo.branch`): its owner decides.
- **A session poisons only itself.** A sandbox can put anything in its own private layer, and its session's later
  sandboxes read it. That is the session's own work, like its branch.
- **Compile caches trust their keys.** Bazel's action cache, sccache and turbo entries are named by hashes the client
  computes, so an entry can't be checked against its key (only the CAS can). This is why only trusted builds write
  the shared layer. Turborepo's own artifact signatures (`TURBO_REMOTE_CACHE_SIGNATURE_KEY`) add a check on the
  client and pass through hangar unchanged.
- **After an incident**, purge the repo's caches (`hangar admin caches purge <repo>`, audited). The next build
  writes them again.

