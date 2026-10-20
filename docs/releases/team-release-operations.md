# The team release: steps 12–14 and rolling back

The Cloud Doctor, the console, the vault key, deploy.yml, clients and proof, and how to roll back. Part of the [team release](team-release.md) (#75–#117); steps 1–8 are on [the procedure](team-release-procedure.md), 9–11 on [sandboxes and front doors](team-release-sandboxes.md).

## 12. Operations: the Cloud Doctor, the console and the vault key

### 12a. The Cloud Doctor

**Optional**, on by default in report-only form: every night at 00:30 (`HANGAR_DOCTOR_SCHEDULE`, in the power schedule's zone) it reads the flight records, counts transient failures, files requests for hosts and secrets agents keep needing, and, **with a team API key** for its harness, starts root-cause jobs and opens fix PRs only when sure ([doctor.md](../doctor.md)). Every PR goes through landing and a human approval.

```bash
hangar cred add claude-team --team --harness claude-code --kind api_key --file team-key.txt   # if step 7 didn't
hangar config set HANGAR_DOCTOR_SPECS_REPO acme/hangar-envs         # optional: where environment specs live as files
hangar config set HANGAR_DOCTOR_MODEL frontier                      # optional: the default is balanced
hangar admin doctor run --dry-run                                   # what a run would do now, changing nothing
```

- **Model:** `balanced` for every harness (resolved and pinned on each job's session): Opus on Claude Code, which any team key can use. `HANGAR_DOCTOR_MODEL=frontier` asks for Fable; on a team key that may lack Fable access, `best` instead, which Claude Code resolves against the key in use.
- **Budget:** `HANGAR_DOCTOR_MAX_USD` ($10 a run) and `HANGAR_DOCTOR_JOB_MAX_USD` ($3 a job); at most 3 doctor PRs open at once.
- **Specs:** without `HANGAR_DOCTOR_SPECS_REPO` (and `HANGAR_DOCTOR_SPECS_PATHS`), a fix to an environment spec is reported instead of opened.

### 12b. The operator console

Nothing to set. `hangar status` (`GET /api/status`, `hangar://status`; admins) shows the power state and last deploy, the loops' heartbeats, harness versions, builds, the day's failures by cause, egress denies, waiting requests, SLOs, credential health, the `models` line, the Cloud Doctor and recent warnings. `hangar admin records ls|show` reads the flight records; `hangar cred check` checks credentials now ([observability.md](../observability.md)).

### 12c. Rotating the vault key

**Optional**; nothing changes until you do it. Rotate on a schedule, when an operator who could read the key leaves, and at once when it may have leaked. The full procedure, with every command, is [security/vault-key.md](../security/vault-key.md#rotating-the-vault-key); in short: put a new key first in Secret Manager (`<new>,<old>`) and the `hangar` Secret, restart, run `hangar admin rotate-key` (it re-seals the vault, re-encrypts checkpoints and backups in the background, and retires the old key when nothing holds it; `--status` shows where it is), then remove the old key from the configuration and **disable** (not destroy) its Secret Manager version until every backup made before the rotation has aged out. Take a backup afterwards.

## 13. Deploying with deploy.yml

**Optional**; required to deploy through `.github/workflows/deploy.yml` rather than `up.sh`. The setup is [below](../upgrading.md#deploying-with-deployyml): the `production` environment with you as its required reviewer and only `main` and `v*` tags allowed; its five environment secrets; the `hangar-deploy` service account with `roles/container.clusterViewer` and a Role on the `hangar` Deployment. `bash deploy/gke/setup-cicd.sh` does all of it, with the images workflow's secrets (step 3), the vault key (step 1) and the package mirrors (step 10a), and prints the rest; [cicd.md](../cicd.md) walks through the first deploy.

## 14. Clients and proof

- **claude.ai users remove the hangar connector and add it again** (Settings → Connectors): the apps never refetch the tool list ([clients.md](../clients.md)). CLIs refetch it when a session starts. Waves 2, 3 and 4 add no MCP tools (the inbox and the console are resources, requests ride on `pending_approvals` and `answer_approval`, listings gain a `cursor` field; Wave 4's manifests, traces and test results are resources, REST and CLI): 54 tools, 24,647 B.
- `bash scripts/acceptance/gke.sh` runs the checks from the overnight guide's [§5](overnight-stack-procedure.md#5-prove-it-on-your-cluster) plus `mcp` (`create_sandbox` over MCP on a multi-harness environment within claude.ai's 230 s, a second create whose client gives up after 5 s still comes up, then `exec`), `power` (a create refused while the pools are off) and `warmstart` (a warm and a cold create, timed). It prints the server's version with its tool-list hash.
- **Acceptance fixtures (optional; each check skips without its own):** a test repo in `HANGAR_GITHUB_REPOS` with the GitHub App installed, *Allow auto-merge*, and branch protection that requires code owners' review; a second account's classic `repo` token (`HANGAR_ACCEPT_APPROVER_TOKEN`); a token that writes to the test and fixture repos (`HANGAR_ACCEPT_GITHUB_TOKEN`); a fixture repo with an unprotected default branch; a Jira project mapped to the test repo (`HANGAR_ACCEPT_JIRA_WEBHOOK_SECRET`). For deploy.yml, the repos are `production` variables (`HANGAR_ACCEPT_TEST_REPO`, `HANGAR_ACCEPT_FIXTURE_REPO`) and the tokens `production` secrets. [acceptance.md](../acceptance.md#before-the-first-run-fixtures) has each one.
- **The first live run follows [acceptance.md](../acceptance.md)**, in its order: the core checks, the operator's views (`status`, a Cloud Doctor dry run), then the team release's own (`computer`, `egress`, `services`, `envbuild`, `secrets`, `landing`, `intake`, `scale-smoke`, `power --power-cycle`, `prepull`), with the fixtures each needs and what each failure most likely means. The harness contract and canary are proved in GitHub Actions, not on the cluster: `contract` green on the commit you deploy.
- `uv run hangar images plan --registry "$REG"` exits 0, and `hangar pack list` shows each pack's locked and installed version.
- The first 09:50 power-up should report `prepull.ready: true` in `hangar power`.

## Rolling back this release

In this order:

1. **Remove members first** (`hangar person rm <id>`, which revokes their tokens). The previous version has no people: every `mcp` token would see and act on everyone's work, and `user` tokens are refused.
2. **Disarm landing.** `hangar repo set REPO --land manual` on each landing repo, and disable auto-merge on any agent PR the inbox shows as approved: the previous version doesn't disarm on a push.
3. **Know what goes open.** The previous version ignores `egress`, `cloud`, `build_secrets`, `computer`, `services` and `ports`: proxied environments lose their allowlist as the previous version rewrites their templates and starts their sandboxes, cloud tokens stop being minted, and secret scanning stops. Take sensitive environments out of service first. It also ignores `heavy`, `tier: heavy` and `profile`: take heavy environments out of service too, or their sandboxes start on the default tier without Docker.
4. **Hand archived sandbox records back.** The previous version runs on the upgraded file (it ignores the archive table and keeps the indexes up to date), but it can't see archived records:
   ```bash
   kubectl -n hangar exec deploy/hangar -- python3 -c "import sqlite3; db = sqlite3.connect('/data/hangar.db'); \
   db.execute('INSERT OR IGNORE INTO sandboxes(key, data) SELECT key, data FROM sandboxes_archive'); db.commit()"
   ```
5. **Hand archived jobs and sessions back, moved, not copied** (the previous version doesn't read the archive, and changes only the main table's copy, so a copy left behind would be stale):
   ```bash
   kubectl -n hangar exec deploy/hangar -- python3 -c "import sqlite3; db = sqlite3.connect('/data/hangar.db'); \
   [db.execute(q.format(t=t)) for t in ('jobs', 'sessions') for q in ( \
   'INSERT OR IGNORE INTO {t}(key, data) SELECT key, data FROM {t}_archive', 'DELETE FROM {t}_archive')]; \
   db.commit()"
   ```
   The previous version loads every job and session at startup again, so give its pod the memory the history needs. (An upgraded hangar drops an archive row whose key is also in the main table when it starts, so a copy an earlier rollback left is harmless.)
6. **One vault key.** Finish a rotation (or don't start one), then put the single key `hangar admin rotate-key --status` lists as encrypting back in `HANGAR_SECRET_KEY`: the previous version reads one key and doesn't start with two. With a key file, `secret.key` holds one key once a rotation is retired.
7. **Set `HANGAR_JOB_CONCURRENCY` back to 4** if you raised it: the previous version has no job budgets or daily caps.
8. **Suspend live sessions** (as `power.sh down` does), or let running turns finish.
9. **The power schedule:** set `power_up_cron = "0 10 * * *"` and `sandbox_warm_nodes = 0`, and `terraform apply`. The old `power.sh` is in the old checkout; the Cloud Run jobs embed whichever script the last apply read.
10. **Put the previous image back:**
   ```bash
   kubectl -n hangar set image deploy/hangar hangar="$(cat ~/.hangar/pre-upgrade-image)"
   kubectl -n hangar rollout status deploy/hangar
   ```

What stays: the node pool (the previous version runs on it unchanged), the database, every session and checkpoint, and the old pack tags in Artifact Registry, which the previous version mounts by tag as before. What the previous version ignores: repo policies, budgets, `person` on records and the owners the migration set (every credential is shared again: remove members first, step 1), `packinfo` digests, the drain (the new `power.sh` treats its 404 as settled), the stale-tool-list records, links, environment builds and requests, flight records, the Cloud Doctor's findings, model policies (a session pinned to `claude-fable-5-1` keeps that id), the key ring's state and `HANGAR_BACKUP_STORE` (backups go beside the checkpoints again), Wave 4's repo policy fields (`heavy`, `profile`, `max_profile`, `verify_reports`, `rerun_flaky`), test runs and flaky lists, turn manifests and experiments, and the build caches (their objects stay in the `caches/` prefix until you delete it). The heavy pool, its node service account, the Docker Hub remote repository and `heavy-policy.yaml` stay; the policy refuses nothing the previous version creates. GitHub replay marks are compatible both ways. The next upgrade archives the records again; the credential migration doesn't run twice.

