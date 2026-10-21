# The heavy tier

Most real repos run their integration tests with `docker compose` or Testcontainers, and build with more than the
default 2 CPUs and 4 GiB.

A **heavy** sandbox is a VM of its own with Docker running natively inside it. So
`docker build`, `docker compose up` and Testcontainers work as on a laptop, and the containers it starts stay inside
that VM. The heavy tier also holds the bigger **resource profiles**. gVisor stays the default tier for everything else.

This page turns the tier on and says what decides whether a sandbox gets it. How Docker runs inside is on
[Docker in the sandbox](docker.md), and why the VM holds on [heavy-tier isolation](heavy-isolation.md).

```yaml
# envs/python-docker.yaml (an admin's environment)
name: python-docker
extends: python
heavy: true            # or `tier: heavy`: Docker in the image and in the sandbox
profile: large         # standard (2 vCPU / 4 GiB) | large (8 / 32 / 100 GiB disk) | xlarge (16 / 64 / 200)
```

```yaml
# .hangar/environment.yaml (the repo's)
heavy: true            # a request, off unless an admin allows it; see "What decides" below
services:
  deps: {compose: docker-compose.yml}              # a Compose stack the supervisor runs (heavy only)
  db: {compose: ops/compose.test.yml, port: 5432}  # ready once 5432 answers
  cache: docker-compose.cache.yml                  # short for {compose: docker-compose.cache.yml}
```

## Turning it on

The tier is off until an admin lists providers for it, as for `fast` and `isolated`:

```bash
HANGAR_TIERS='{"fast": ["e2b"], "isolated": ["kubernetes"], "heavy": ["kubernetes"]}'
```

A heavy sandbox only ever runs on a provider listed there, in order, falling back past one that is full or down. It
never runs anywhere else: like `isolated`, it is a security choice (a privileged sandbox needs a VM around it).

| Provider | What a heavy sandbox is | Setup |
| --- | --- | --- |
| `kubernetes` | a Kata pod (`HANGAR_HEAVY_RUNTIME_CLASS`, `kata-qemu-runtime-rs`) on the `sandboxes-heavy` pool | [Running on GKE](../gke.md#4a-the-heavy-tier-optional): the pool, kata-deploy, and the Agent Sandbox controller caveat |
| `e2b` | an E2B or CubeSandbox microVM | [the microVM tier](../microvm.md#the-heavy-tier-on-microvms) |
| `docker` | a privileged runc container: **root on the host** | `HANGAR_HEAVY_DOCKER_PRIVILEGED=true`, for CI and throwaway machines only |

## What decides

Before a sandbox starts (`heavy.decide`), hangar settles Docker, the profile, the caps and the warm pools.

**Docker is deny by default.** It comes from the environment (`heavy: true`, `tier: heavy`; admin fields). Else it
comes from the repo's policy (`hangar repo set acme/app --heavy on`). Else the repo's `.hangar/environment.yaml` can
ask for it (as its default branch was last seen), which is only a **request**.

A request is honored when an admin has allowed it for that repo (`hangar repo set acme/app --heavy allow`) and this
hangar has a heavy tier. Unset, or `--heavy deny`, it isn't. Otherwise anyone who can push the default branch could
give the repo's sandboxes root in a VM on costlier nodes.

A heavy repo on a plain environment gets that environment's heavy variant: the same spec, its image built with Docker
in it. The repo's builds then start from that variant too.

**A request that isn't allowed** runs on the default tier, without Docker, and says so plainly:

- a warning on the sandbox (`get_sandbox`'s `info.warnings`, the job's events);
- a "Docker isn't enabled for this repo" section in the agent's instructions that gives the admin command;
- a `WARN` on `hangar-agent env doctor`'s `tier` line:

  ```text
  WARN tier      tier standard (2 CPUs, 4 GiB, gvisor); Docker isn't enabled for acme/app: its .hangar/environment.yaml
                 asks for the heavy tier, and a repo's own file only requests it, and an admin hasn't allowed that for
                 this repo, so this sandbox runs on the default tier, without Docker. An admin enables it: hangar repo
                 set acme/app --heavy allow
  ```

**Profile.** First comes a job's label `hangar.profile` (`hangar job submit --profile large`, or `labels` on
`POST /api/jobs` and `create_sandbox`), then the repo's file. Each is allowed up to the policy's `max_profile`, else no
more than the policy's or the environment's own profile. Then come the policy's `profile`, then the environment's.

`large` and `xlarge` run on the heavy tier's nodes, with Docker or not: the gVisor pool's nodes can't hold them. A
profile replaces `resources`. Asking past what's allowed fails the create and says which `hangar repo set` raises it.

**Caps.** One person holds at most `HANGAR_HEAVY_PERSON_MAX` live sandboxes per profile (3 `large` or larger, 1
`xlarge`). The accounting is the same per person as for the other caps ([team scale](../team-scale.md)).

Work no person owns shares one team bucket with the same caps, so it can't take the heavy pool either. That is jobs
from GitHub, Slack or Jira senders linked to nobody, webhooks, and schedules without a person.

Repo builds are
hangar's own, one per repo and environment at a time, and aren't counted. Docker at the standard size isn't capped
beyond the usual sandbox caps.

**Warm pools** of heavy environments stay at 0 unless `HANGAR_HEAVY_WARM_POOLS=true`: a heavy node held warm costs
(see [team scale](../team-scale.md#heavy-tier)).

`heavy`, `tier: heavy` and a `profile` other than standard are admin fields on an environment, like `runtime_class`.
