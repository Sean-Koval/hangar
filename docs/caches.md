# Build caches

A new sandbox used to install every dependency and compile everything from nothing. On a big repo that is minutes
of every job and every session before the agent does anything useful, and an agent short on time skips the build
and the tests instead. hangar now keeps two kinds of cache across sandboxes, per repo:

- **Dependency caches**: the package managers' caches and the directories they install into (`~/.npm`,
  `node_modules`, `~/.cache/uv`, `.venv`, `~/.cargo`, `target`, the Go module and build caches, Maven, Gradle).
  They are restored after the clone, in the background: the sandbox is ready at once, and the agent is told the
  caches are warming.
- **Remote compile caches**, which hangar serves itself in the protocols the tools already speak: sccache's WebDAV
  backend, Bazel's HTTP remote cache, and the Turborepo remote cache API. Sandboxes come with the variables and the
  `~/.bazelrc` lines that point the tools there. Nothing in the repo has to change.

Both sit on top of [repo builds](environments.md) (#93). A build is an image of the default branch's checkout after
`install`, so a sandbox started from one already has `node_modules`. What a build doesn't have, the caches cover:
whatever lives in `$HOME`, a branch whose lockfile has moved on, a harness variant or provider that runs without
builds, and compile output.

Who may write what is the part that matters most. Members, branches and untrusted repos share this infrastructure,
so a cache written from one sandbox must never feed another person's build. The rules are below, and the threat model
is in [security.md](security.md#build-caches).

## Turning it on

Caches are on by default (`HANGAR_BUILD_CACHES=true`), and they need the git gateway. Sandboxes reach the caches at
`/cache` on the gateway's port, with the grant the gateway gave them (`hgs_…`), so they need `HANGAR_GATEWAY_URL`.
A sandbox without a grant gets no caches: that is a direct-mode sandbox, or one whose repo isn't on a host the gateway
serves. Jobs and sessions always have a grant when the gateway is configured.

| Setting | Default | |
|---|---|---|
| `HANGAR_BUILD_CACHES` | `true` | Off: no `/cache` routes, nothing restored, no variables set. |
| `HANGAR_BUILD_CACHE_STORE` | the session bucket's `caches/` prefix, else `<data_dir>/caches` | `gs://bucket[/prefix]` or a directory. |
| `HANGAR_BUILD_CACHE_MAX_GB` | 100 in a bucket, 10 on the data volume | All repos together. |
| `HANGAR_BUILD_CACHE_REPO_MAX_GB` | 20 | Per repo. |
| `HANGAR_BUILD_CACHE_RETENTION_DAYS` | 14 | An entry unused this long goes, in the nightly pass. |
| `HANGAR_BUILD_CACHE_PRIVATE_DAYS` | 3 | The same for an entry in a session's private layer. |
| `HANGAR_BUILD_CACHE_PRIVATE_WRITES` | `true` | Off: sessions and jobs only read. |
| `HANGAR_BUILD_CACHE_OBJECT_MAX_MB` | 4096 | The largest single object (a dependency archive, a compile output). |
| `HANGAR_BUILD_CACHE_TRANSFER_MB` | 256 | Cache bytes held in the control plane's memory at once: what uploads buffer and seal (up to four times a chunk each, or four times their length when it is smaller) and the chunks downloads haven't sent yet. Requests past it wait; an upload that sends nothing for 60 s is cut off. |
| `HANGAR_BUILD_CACHE_WAIT_MINUTES` | 10 | How long a repo build waits for its restore before `install`. |

On GKE the session bucket is where caches go, and hangar's service account can already write there. On Docker,
size the data volume for `HANGAR_BUILD_CACHE_MAX_GB`.

## Who writes, who reads

Each repo's caches have layers:

- **The shared (trusted) layer, one per environment.** Only hangar's own repo builds write it. A repo build runs the
  `install` of the default branch, or of the branch the environment names, in a sandbox hangar created for that
  build. hangar's build step saves the dependency caches after a successful `install` (`hangar-cache save`), and
  compile caches written during `install` (an `install` that runs `cargo build` with sccache, for example) land
  there too. The layer belongs to the repo's builds in one environment, and only that environment's sandboxes read
  it. An environment's image and `setup` run in its builds before `install`, and members create environments, so a
  build in one member's environment never writes what sandboxes of another environment read. A build with
  [build secrets](security.md#build-secrets) has every upload scanned for them from the start of `install`, and
  can't store what the scan can't look into (zstd, zip and the like), so its sccache and turbo entries aren't kept
  ([security.md](security.md#build-caches)).
- **A private layer per session (or sandbox) and branch.** A session's or job's sandbox reads its own private layer
  first and the shared layer second. Its writes, from sccache, Bazel, turbo or `hangar-cache save`, go to its
  private layer. Nothing else ever reads that layer: not another session, not another person, and not a build.
  Nothing is promoted from it to the shared layer. The layer belongs to the session, so the session's next sandbox
  (after a suspend, say) finds what it wrote. With `HANGAR_BUILD_CACHE_PRIVATE_WRITES=false`, sessions and jobs
  only read: sccache runs with `SCCACHE_WEBDAV_RW_MODE=READ_ONLY`, turbo with `TURBO_CACHE=local:rw,remote:r`, and
  Bazel with `--remote_upload_local_results=false`.
- **No repo reads another's.** The grant names one repo, and that repo is the only one the sandbox can read.

Agent-led setup's validation sandboxes run an agent-written branch, so they count as untrusted like any session. A
sandbox someone creates with a `hangar.build` label is not a build either. hangar trusts a sandbox as a build only
when the build it names is running, is of the same repo, and created that very sandbox.

## Dependency caches

| Kind | What | Keyed by |
|---|---|---|
| `npm` | `~/.npm` | `package-lock.json`, `npm-shrinkwrap.json` |
| `pnpm` | `~/.local/share/pnpm/store` | `pnpm-lock.yaml` |
| `yarn` | `~/.cache/yarn`, `~/.yarn/berry/cache` | `yarn.lock` |
| `node_modules` | `node_modules` in the checkout | any of the Node lockfiles, and `node --version` |
| `uv` | `~/.cache/uv` (`UV_CACHE_DIR`) | `uv.lock` |
| `pip` | `~/.cache/pip` (`PIP_CACHE_DIR`) | `requirements.txt`, `poetry.lock`, `Pipfile.lock` |
| `venv` | `.venv` in the checkout | the Python lockfiles, and `python3 --version` |
| `cargo` | `~/.cargo/registry`, `~/.cargo/git` | `Cargo.lock` |
| `cargo-target` | `target` in the checkout | `Cargo.lock`, and `rustc --version` |
| `go-mod` | `~/go/pkg/mod` (`GOMODCACHE`) | `go.sum` |
| `go-build` | `~/.cache/go-build` (`GOCACHE`) | `go.sum`, and `go version` |
| `maven` | `~/.m2/repository` | `pom.xml` |
| `gradle` | `~/.gradle/caches`, `~/.gradle/wrapper` | the Gradle wrapper properties, lockfile and build scripts |

- **Keys.** A key is `<toolchain>/<lockfile digest>`. The digest covers the git blob ids of the kind's lockfiles in
  the checkout's root. The toolchain is the version line of the tool that built the files, when the files depend on
  it (`node_modules`, `.venv`, `target`, the Go build cache), and `any` when they don't.
- **Fallback.** As with `restore-keys` in GitHub Actions, a restore takes the exact key first. If that misses, it
  takes the newest entry with the same toolchain: its private layer first, then the shared layer. A lockfile that
  changed still gets most of what it needs, and the package manager fetches the rest.
- **Entries are immutable.** A second save under the same key is refused (409). A new lockfile makes a new key.
- **In the background.** After the clone, hangar runs `hangar-cache restore` in the sandbox with `nohup` and moves
  on, without waiting for it. The agent's instructions say the caches are being restored, and that `hangar-cache
  wait` blocks until they are done (it returns at once when they are). `hangar-cache status` says what each kind got:
  `hit`, `partial` (a fallback key), `miss`, `skip`, or `error`.
- **Nothing is overwritten.** Archives are extracted with `--skip-old-files`, so whatever the sandbox already has
  wins. A directory in the checkout (`node_modules`, `.venv`, `target`) is restored only when git ignores it, so the
  agent's diff stays its own. It is not restored at all when the sandbox started from a repo build, whose image
  already has it.
- **Repo builds** wait for their restore before `install` (up to `HANGAR_BUILD_CACHE_WAIT_MINUTES`), since both
  write the same directories. They save after `install`, and the build's log (`hangar env builds`) says what was
  restored and what was saved.

In the sandbox:

```
hangar-cache status            # state: warming | warm | interrupted | off, and each kind's result
hangar-cache wait [SECONDS]    # block until the restore is done
hangar-cache save [KIND...]    # save the caches as they are now (to your private layer)
hangar-cache kinds             # the kinds this repo has, and their keys
```

## Compile caches

All three are served on the gateway's port under `/cache`, with the sandbox's grant as the credential.

**sccache** uses its WebDAV backend. The sandbox gets `SCCACHE_WEBDAV_ENDPOINT=<gateway>/cache/sccache/` and
`SCCACHE_WEBDAV_TOKEN=<grant>` (bearer), plus `SCCACHE_WEBDAV_RW_MODE=READ_ONLY` when it may not write. sccache's
WebDAV backend is opendal's `webdav` service, which by default checks for parent directories with `PROPFIND` and
creates them with `MKCOL` before a `PUT`. hangar answers both: keys are flat, so every directory exists. Reads are
`GET` and `HEAD`, and `DELETE`, `MOVE` and `COPY` are refused. `RUSTC_WRAPPER` points at
`~/.hangar/bin/hangar-rustc-wrapper`, which runs rustc through sccache when the image has `sccache` and runs rustc
directly when it doesn't, so a sandbox without sccache builds as before. An environment that maps one of these
variables itself (in `env:` or `secrets:`) keeps its own.

**Bazel** uses its HTTP caching protocol: `GET` and `PUT` under `/ac/` (action results) and `/cas/` (outputs, named by
their SHA-256). hangar adds a block to `~/.bazelrc`, Bazel's home rc file, which Bazel reads after the workspace's
`.bazelrc`:

```
# >>> hangar build caches (docs/caches.md)
build --remote_cache=http://hangar:<grant>@<gateway>/cache/bazel
build --remote_upload_local_results=false      # only when the sandbox may not write
# <<< hangar build caches
```

The rest of the file is kept. A `PUT` to `/cas/<sha256>` whose body doesn't hash to its name is refused.

**Turborepo** uses the remote cache API (`/v8/artifacts/...`). The sandbox gets `TURBO_API=<gateway>/cache/turbo`,
`TURBO_TOKEN=<grant>` and `TURBO_TEAM=hangar` (the team is ignored, since the grant decides the repo), plus
`TURBO_REMOTE_CACHE_READ_ONLY=1` and `TURBO_CACHE=local:rw,remote:r` when it may not write. hangar serves
`GET`/`HEAD`/`PUT /v8/artifacts/{hash}` (with `x-artifact-duration` and `x-artifact-tag`, so signed artifacts
verify), `POST /v8/artifacts` (the query), `GET /v8/artifacts/status` and `POST /v8/artifacts/events`.
Turborepo's artifact signatures (`TURBO_REMOTE_CACHE_SIGNATURE_KEY`) work through hangar unchanged: the tag is stored
and returned.

These were checked on 2026-10-15 against the tools' own documentation: sccache's `docs/Webdav.md` (0.17.0), opendal's
`webdav` service page, bazel.build's remote caching and bazelrc pages, and turborepo.dev's remote cache OpenAPI spec
and system environment variables page.

## Storage

- **Content-addressed and encrypted.** Each object is split into 8 MiB chunks. A chunk is named by the SHA-256 of
  its plaintext and sealed with the vault key (Fernet, as checkpoints are) before it is stored, under its repo's own
  prefix. Equal chunks within a repo are stored once. Equal chunks in two repos are two objects.
- **Checked on every read.** Each chunk must decrypt with a vault key and hash to its name, and the whole object
  must hash to its entry's digest. A chunk that fails is a miss: the entry is dropped, and a stream already under
  way is cut short, which every client treats as a failed fetch. `hangar-cache` then checks the archive's SHA-256
  against the digest hangar sent before it extracts anything.
- **The index** is `caches.db` on the data volume, apart from hangar's database: compile caches write many small
  entries, which don't belong in the state database or its backups. If the index is lost, the caches are lost, and
  the nightly sweep deletes the objects nothing names any more.
- **Retention.** When a write takes a repo over `HANGAR_BUILD_CACHE_REPO_MAX_GB`, or all repos over
  `HANGAR_BUILD_CACHE_MAX_GB`, entries are evicted down to 90%: private layers' first, then the least recently used.
  The nightly pass (`HANGAR_DOCTOR_SCHEDULE`) drops entries past their retention and sweeps leftover objects.
- **Key rotation.** A retired vault key makes the entries it sealed unreadable. They turn into misses and are
  dropped. Caches are rebuilt by the next build, so a rotation doesn't re-encrypt them.

## Operating them

`hangar status` shows the caches' size against the caps, the biggest repos (shared and private bytes, last use), and
since the last start the hits, misses, writes, read-only refusals and integrity failures. The same is in `GET
/api/status` and the `hangar://status` resource. Admins also have:

```
hangar admin caches ls                          # the same, on its own
hangar admin caches purge acme/app              # every entry of a repo (audited)
hangar admin caches purge acme/app --layer private
hangar admin caches purge --all
```

REST: `GET /api/caches`, `DELETE /api/caches?repo=&layer=` (`all=true` for every repo). Writes to dependency caches
are audited (`cache.write`). Compile cache writes are only counted, since there are thousands per build.

If you suspect a shared layer was poisoned, purge the repo. The next build writes it again.

## Measured

`tests/test_buildcache.py::test_bench_npm_install_cold_and_warm` (run it with `HANGAR_CACHE_BENCH=1`) installs a
fixture project, `tests/fixtures/cache-bench`, on the fake provider through a real gateway. The project is express,
eslint, typescript and lodash: 162 packages. It first runs in a repo build's sandbox with no caches (`npm ci`, then
hangar saves `~/.npm` and `node_modules`), then in a session's sandbox, which restores and has its dependencies
before running anything. Three runs on a 2-vCPU machine with a fast registry connection:

| | cold (no caches) | warm |
|---|---|---|
| Dependencies usable | `npm ci`: 3.9 s, 4.7 s, 5.0 s | restore 0.9 s, 1.0 s, 1.3 s (`node_modules` back; nothing to install) |
| `npm ci` anyway | the same | 2.6 s, 3.1 s, 3.1 s (from `~/.npm`, nothing fetched) |
| Saving (in the build) | 1.2 to 1.4 s, 17 MB compressed | |

The gain grows with the registry's distance and the size of the tree. It is largest for compiled output (`target`,
the Go build cache, sccache, Bazel and turbo), where warm means not compiling at all. Those weren't measured here,
since this machine runs no Cargo builds.

## Limits of this version

- Caches need the gateway: there are none for direct-mode sandboxes.
- The C and C++ compilers aren't wrapped with sccache (only rustc is), and Gradle's and ccache's HTTP caches aren't
  wired up, although the WebDAV endpoint speaks their protocol.
- Only lockfiles in the checkout's root are keyed. A monorepo's nested lockfiles share the root's key, and the
  fallback covers the rest.
- A restore runs once, after the clone. A sandbox whose lockfile changes mid-session restores nothing new. Its
  package manager fetches the difference, and `hangar-cache save` keeps it for the session's next sandbox.
