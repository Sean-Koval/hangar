# The repo's own file

A repo describes itself in `.hangar/environment.yaml`, committed next to its code. It says how to install its
dependencies, what to start, which processes to keep up and which check proves a change works. This page is the file, what it can't
set, and how its fields combine with the rest. The environment itself is on [environments](../environments.md).

## The file

```yaml
# .hangar/environment.yaml
extends: python                  # a hangar environment the repo's HANGAR_GITHUB_REPOS entry allows
install: uv sync --frozen        # idempotent; runs on every build, in the repo's root, never when a sandbox starts
start: ./scripts/seed-db.sh      # once each time a sandbox starts, before the terminals
terminals:                       # long-running processes the supervisor keeps up
  - name: web
    command: npm run dev -- --host 0.0.0.0
    cwd: frontend                # relative to the repo
    port: 5173                   # ready once something listens here...
  - name: worker
    command: uv run worker
    ready: test -f /tmp/worker.ready   # ...or once this exits 0
    autostart: false             # declared, started when the agent asks: hangar-agent svc start worker
    restart: on-failure          # always (default) | on-failure | never
services:                        # images beside the sandbox, reachable at their name (postgres:5432)
  postgres:
    image: postgres:17
    port: 5432
    env: {POSTGRES_PASSWORD: dev, PGDATA: /var/lib/postgresql/data/pgdata}
    volumes: [/var/lib/postgresql/data]   # empty, writable directories
ports:                           # the owner can open these in a browser, through hangar
  - {port: 5173, name: web}
verify: uv run pytest -q         # the check jobs run, unless the job names one
verify_reports: [reports/*.xml]  # JUnit, Cobertura or LCOV files it writes (most runners need none: see test results)
build_secrets: [NPM_TOKEN]       # ids only; an admin maps each to a credential (see Build secrets)
lockfiles: [vendor/*.lock]       # more files whose change means a rebuild
mocks:                           # stand-ins for real dependencies, so nobody mistakes them (agent-led setup)
  - {what: S3, how: "MinIO service at minio:9000", why: no cloud credentials in sandboxes}
heavy: true                      # Docker inside, on the heavy tier, where an admin allows it (see The heavy tier)
profile: large                   # standard | large | xlarge, up to what the repo's policy allows
```

Every field is optional. `mocks` is documentation: hangar applies nothing from it. But
[agent-led setup](agent-led-setup.md) requires every stand-in to be listed there and in its PR.

Each field the file sets replaces the environment's own. An environment spec takes the same `install`, `start`,
`terminals`, `services`, `ports` and `verify` fields. So a team can share defaults there and a repo can override them.
`verify_reports` names the report files a check writes; most runners need none ([test results](../jobs/test-results.md)).

`hangar env check` validates a file against your hangar without saving or building anything:

```console
$ hangar env check .hangar/environment.yaml --repo acme/app
.hangar/environment.yaml: valid (extends python)
```

The same check is `POST /api/envbuilds/validate {"content": ..., "repo": ...}`. In code, `hangar.repoenv` has
`parse` (validate), `dump` (write) and `check` (validate against a hangar); agent-led setup builds on these.

## What the repo can't set

The file is untrusted: anyone who can push to the repo writes it. It is parsed strictly: unknown fields are refused,
YAML aliases are refused, and it must be under 64 KiB. It can set only the fields above.

Everything an admin decides stays in hangar:

- the image and base (`image`, `base`);
- secrets and credentials (`secrets`, `credentials`), which stay in hangar, by name;
- MCP servers, Claude Code `plugins` and `runtime_class`;
- resources, `sudo`, and the git credential mode.

A file that names one of these is refused, and the message says it's decided in hangar.

`heavy`, `tier: heavy` and `profile` are requests that the repo's policy bounds
([what decides](heavy-tier.md#what-decides)). Docker is off unless an admin allows it for the repo
(`hangar repo set owner/repo --heavy allow`), and no other tier can be asked for.

hangar reads the file from the commit (`git cat-file`), never from the working tree. A symlink committed in its
place is read as the link's text, so it can't make hangar read another file in the sandbox.

## Resolution order

Per field: **what the job asked for, then the repo's file, then the repo's policy** ([repo policy](../repo-policy.md)),
**then the environment spec**.

Before a job's sandbox exists, hangar uses the file as the default branch had it at the last clone. So a repo's
`extends` and `verify` apply to every front door (GitHub issues, Slack, schedules, MCP) from the second job on.

`extends` picks the environment only when the repo's `HANGAR_GITHUB_REPOS` entry allows that environment. A repo with
no entry doesn't pick its environment. An environment decides credentials, secrets and egress, so an admin says which
ones a repo may name.

hangar uses the file only as last seen by a sandbox of the job's own environment, or of one the allowlist names. It
never uses the file as another environment's sandbox recorded it: its `setup` and `install` run before the read, and
members make environments.
