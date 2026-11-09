# The uid split

This page is part of [Steering a running agent](../steering.md).

The driver holds the run's key and answers for whoever approves. In a single user, code the agent runs (a test suite you allowed, say) can reach it, so where the sandbox allows it hangar runs each turn as two users (`src/hangar/isolation.py`):

- **`hangar`, a system user, runs the turn.** It runs the turn's script, the driver and their tmux session. It owns `/var/lib/hangar`:
  - the driver itself;
  - `runs/` (0755): each run's script, exports, prompt, log and exit status;
  - each run's `<run>.ctl/` (0700): the key, `in/` and `acks`.
- **The agent runs the harness, as the sandbox user, with every capability dropped.** The driver starts the harness through `setpriv`. Its environment is built as the agent, in the same order as a single-user run: `~/.hangar/env`, then the run's exports. The harness starts in the turn's directory.
- **The agent's readers work as before.** The agent's `~/.hangar/runs/<run>.log`, `.exit`, `.pid` and `.sh` are links into `runs/`, so `grep … ~/.hangar/runs/*.log` in the sandbox reads a run as it did.
- **hangar reads its own copy.** Those links are the agent's to point anywhere, so hangar never reads a split run through them. It reads the run's log, exit status and pid from `runs/` as `hangar`, with the system `PATH`. It takes the run's approval requests and answers from `<run>.ctl/approvals`, which the driver writes next to the run log, and which only `hangar` can write. The run log also carries the harness's output. The agent can write into that output, because the harness's stdout and stderr are its own. The driver also defuses a `hangar.*` line that comes from the harness: the line is kept in the log as text, so the parser never reads it as an event.
- **The dashboard's "Watch the agent" is a read-only view** of the run's tmux session.

What the agent can't do then (`tests/test_e2e_isolation_docker.py` checks each from inside a real sandbox while a turn waits on an approval):

- read a run's key or list its control directory;
- write a command, the driver, the turn's script or its exports;
- signal the driver, trace it (`ptrace`), or read its environment or memory;
- get back to the driver's uid (`setuid`);
- have a program it planted on its `PATH` run as root or as `hangar`.

`tests/test_e2e_harden_docker.py` checks these in a real sandbox:

- **Gain privileges through a setuid program.** The harness, and everything it starts (its tools, the verify gate, a one-shot run), runs with `no_new_privs`. A setuid-root `id` stays the agent's uid. A `NOPASSWD` sudo rule added behind the setup's back is refused with "the no new privileges flag is set". Tools that don't need sudo work as before (checked: `git`, `python3 -m venv`).
- **Fake what hangar reads.** In the test the agent points its links at a log and an exit file of its own, and writes a forged "approved" line into the run log through the harness. hangar still sees the turn running and the approval pending.
- **Change hangar's harness settings for the next turn, or turn off the Stop hook.** See [hangar-owned harness settings](harness-settings.md).

**How it starts.**

- Root starts the turn's tmux through `setpriv` as `hangar`, with `CAP_SETUID`, `CAP_SETGID` and `CAP_KILL` in its ambient set. The driver needs the first two to start the harness as the agent, and the third to stop it.
- Commands hangar runs as root or as `hangar` find programs through a fixed system `PATH`. The image's `PATH` starts with directories the agent owns (`~/.local/bin`), and Docker resolves an exec's command through it: a `timeout` planted there ran as root. This applied to the Docker provider's own root `mkdir` before this change too.
- The harness's `setpriv` clears every capability. A uid change between two non-root users keeps capabilities unless they are dropped: without that step the agent would keep `CAP_SETUID` and could become `hangar`.
- The mechanism was checked under runc (Docker 29.8) and under gVisor (`runsc` release-20260928, `runsc do`), with the same results. Kata runs a regular Linux guest kernel, so the semantics are runc's (not run here: no `/dev/kvm`).

**Where it applies.** The split needs Docker (runc, gVisor or Kata runtimes), whose exec picks the uid, and an agent that can't become root. hangar sets it up when a sandbox's first turn starts:

- it adds the user with `useradd --system`;
- it checks for `setpriv` (util-linux), `tmux` and `bash`;
- it checks that the agent can't `sudo`, asking again under a terminal (`script`) when sudo wants one (`Defaults requiretty`: the agent can get a terminal whenever it likes);
- it checks, as the agent, that it can't write any directory on that fixed system `PATH` or the commands root and `hangar` run from it (`setpriv`, `tmux`, `bash`, `python3`, `tar`, `timeout`, `env`, `tee`). `hangar` holds `CAP_SETUID`, so whatever runs as it can become root.
- it checks whether hangar can own the harness's settings. The agent must not be able to change the harness's binary in its pack or baked prefix, any directory above it, or the managed settings in `/etc/claude-code` and `/etc/codex`. If it can, the sandbox is still split, and the reason is kept in `sb.info.isolation.harness_why`.

The answer is cached in `sb.info.isolation` with a version (`v`). A sandbox set up before the checks changed, for example before the `requiretty` check, is probed again at its next turn.

Nothing is built into images, so existing environments get the split without a rebuild. Everywhere else turns run single-user, as before, and `sb.info.isolation.why` says why:

- **environments with `sudo: true`.** The agent is root whenever it likes, so a second user would protect nothing. `sudo` is `false` by default (#124), so default environments get the split. A job's Docker sandbox under `HANGAR_INTAKE_POSTURE=enforce` runs with `no-new-privileges`, so sudo fails there even where the image has it. The split execs as each user with `docker exec --user`, with no setuid binary, so it keeps working there.
- **Kubernetes without the driver sidecar.** No process in a GKE Sandbox container can switch users. With `HANGAR_K8S_DRIVER_SIDECAR`, a turn that asks runs its driver in a container of its own ([the driver sidecar](../security/driver-sidecar.md)).
- **E2B.** One microVM, one user. Unset, `HANGAR_UID_SPLIT` is `required` there ([E2B](../security/e2b-driver-user.md)).
- **`HANGAR_UID_SPLIT=off`.**

A turn under any policy but `auto` in a single-user sandbox carries a warning. The warning says what isn't protected: code the person allows can change the harness's settings, environment and binary so later turns don't ask, and can fake the run log, approval requests and exit status hangar reads. With `HANGAR_UID_SPLIT=required` the turn fails instead, saying why. The same applies when the sandbox is split but hangar can't own the harness's settings there.

`required` covers `deny-risky` and `governed` as well as `ask`. An agent that can switch the prompts off, or answer for the driver, gets past rules and Tower as it would get past a person. `auto` turns run either way.

**What the split doesn't cover.**

- **The harness is a process of the agent's uid.** Docker leaves same-uid `ptrace` to the kernel's Yama setting, and this repo's Docker hosts have no Yama at all. Where the kernel allows it, code running as the agent can change the running harness in memory. That code can be code you allowed in this turn, or a process it left running into a later turn. Changed this way, the harness can skip prompts or print anything into the run log. hangar's own state (approvals, exit status, answers) doesn't come from the harness. Tokens and cost read from the log can still be faked, and so can budget stops. Closing this needs the harness to run non-dumpable, or a ptrace policy for the sandbox. That is a follow-up.
- **Commands hangar runs as the agent outside a turn don't get `no_new_privs`** (`hangar sb exec`, hangar-envd, including [streamed commands and background processes](../streaming.md)). A setuid program in the image that grants root without a password still undoes the split from there. That is why the setup refuses an agent with sudo. Images ship no other such program by default (root's password is locked).
- **`auto` turns start the harness as before:** the agent's binary on its `PATH`, its environment and its settings. There is nothing to approve, so the only control there is the job's verify gate, which stays advisory ([jobs §7](../jobs/agent-loop.md)). hangar's own check after the turn decides delivery.
- **deny-risky resolves paths as `hangar`.** It can't follow symlinks inside directories only the agent can enter, such as a repository under the agent's home (0750). It is a guard rail, as before.
