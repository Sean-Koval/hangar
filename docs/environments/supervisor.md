# The service supervisor: `hangar-agent svc`

One command for the agent to see, start, restart and read the logs of the environment's processes. They are the
terminals and `start` from [the repo's own file](repo-file.md), the [services](services.md) and, on the heavy tier,
Compose stacks.
A process that exits is restarted, so the agent doesn't have to look after it.

```console
$ hangar-agent svc status
NAME    KIND      STATE    READY  PID   RESTARTS  UP   PORT  LOG
web     terminal  running  yes    4121  0         12m  5173  ~/.hangar/svc/logs/web.log
worker  terminal  stopped  no     -     0         -    -     ~/.hangar/svc/logs/worker.log
start   start     done     -      -     0         -    -     ~/.hangar/svc/logs/start.log
$ hangar-agent svc wait web --timeout 120     # exit 1 says which failed or isn't ready
$ hangar-agent svc logs web -n 200 | rg -i error
$ hangar-agent svc restart web                # also: start, stop, --all
$ hangar-agent svc add docs --port 8000 -- python3 -m http.server 8000   # supervise a process of your own
```

## What it does

When a sandbox starts, hangar writes `~/.hangar/svc/manifest.json` from the effective config and runs
`hangar-svc up`, which returns at once. The supervisor then:

1. starts the Compose stacks (heavy tier), and waits for them and the environment's services to be ready (up to
   180 s);
2. runs `start` once, in the repo;
3. starts every terminal with `autostart` (default true), each in its own process group, through `bash -l` (so
   `~/.hangar/env` applies);
4. checks readiness: `port` (something listens on it) or `ready` (the command exits 0), every second until ready,
   then every 15 s;
5. restarts a terminal that exits, with backoff from 0.5 s to 60 s. One that exits 10 times in a row within 30 s of
   starting is `failed` until someone starts it again. `restart: on-failure` leaves a clean exit alone; `never` runs
   it once.

Output goes to `~/.hangar/svc/logs/<name>.log` (rotated at 10 MB to `.log.1`). These are files the agent reads and
searches with the tools it already has, rather than output hangar fetches for it. `hangar-agent svc status --json` is
the same view for tools. The supervisor's own log is `~/.hangar/svc/supervisor.log`.

## Compose stacks under the service supervisor

A `services:` entry with `compose: FILE` (relative to the checkout) is a Compose stack. The supervisor runs
`docker compose -p NAME -f FILE up` in the foreground, so the containers' output is its log
(`hangar-agent svc logs NAME`). A stop or restart stops the containers (SIGTERM).

Stacks start with the services, before `start`, and `start` and the terminals wait for them. A stack is ready when
`port` answers, or `ready` exits 0. By default it is ready when every service in the file has a container running
(and healthy, if it has a healthcheck) or exited 0.

`env` is passed to Compose (for variable substitution). A stack takes only `compose`, `port`, `ready` and `env`:
images, commands and volumes are the file's. Stacks come from the checkout itself, so one added in a commit starts
right away, unlike image services.

`hangar-agent svc add NAME --compose FILE` supervises one for this run. In a sandbox without Docker, a stack is a
setup failure that says the [heavy tier](heavy-tier.md) is needed.

## Why a script and not hangar-envd

The supervisor runs the agent's own processes, as the agent's user, and the agent drives it. hangar-envd is hangar's
authenticated transport into a sandbox. It isn't on every provider (E2B brings its own envd), and it's replaced on
upgrades, which would take every dev server down with it. Process control the agent drives would also widen what
talks to it.

The supervisor is a standard-library python3 script (python3 is in the
[image contract](../specs/0006-environments-and-harness-packs.md)). It is installed in each sandbox at claim like
`hangar-agent`, with no image rebuild. It holds nothing secret. Its socket and state live in `~/.hangar/svc` (0700).
Under the [uid split](../steering.md#the-uid-split) it stays the agent's: hangar's own user never runs it, and it never
reaches hangar's runs.

In the source, the in-sandbox half is `svc_agent.py`. The control-plane half (install, manifest, the agent's
instructions) is `svc_control.py`, named `supervisor.py` before #144. Neither is the run supervisor of specs
[0001](../specs/0001-agent-runs.md) and [0004](../specs/0004-supervisor-scheduling-performance.md), which are
superseded and deferred.
