# The capacity rehearsal

Can the cluster hold the team model, 6 engineers with 10 sessions each, before the team arrives? This page is the
runbook for finding out on GKE without paying for a model. It is part of [Team scale](../team-scale.md).

The [team load test](load-test.md) measures hangar's own overhead on the fake provider. This rehearsal measures the
cluster: real sandboxes, real nodes, the autoscaler and the real Claude Code CLI.

## The stub model

`scripts/bench/stub_model.py` is a stand-in for Anthropic's Messages API, written with the standard library only. The
rehearsal's environment, `capacity-stub`, runs it as a terminal on 127.0.0.1 inside each sandbox. `ANTHROPIC_BASE_URL`
points Claude Code at it, and loopback skips the egress proxy.

Each turn looks like a short coding turn. The stub waits `--think` seconds, asks for one Bash call running `--work`,
then waits again and answers `DONE`. No request leaves the pod. The CLI still prices the stub's small fixed token
counts, so `/api/usage` shows a fraction of a cent per turn, though nothing is billed.

`python scripts/acceptance/capacity.py env` prints the environment and changes nothing. `--env NAME` runs the same
rehearsal on an existing environment instead, such as one on the cheapest real model.

## Before you start

| Check | How |
| --- | --- |
| Quotas and nodes | [GKE capacity](gke-capacity.md): `N2D_CPUS` at least 160, `sandbox_max_nodes` 10 |
| The session cap matches | `HANGAR_PERSON_MAX_SESSIONS` is 10 for the run, so session 11 can be refused. Put it back after |
| A repo for the jobs | The acceptance test repo, with `capacity-stub` added to its environments in `HANGAR_GITHUB_REPOS`. The jobs deliver nothing |
| A quiet cluster | No other sandboxes live: the preflight refuses otherwise, unless `--allow-busy` |
| kubectl | Credentials for the cluster (`gcloud container clusters get-credentials`), for the node counts |
| Pools up | `hangar power status` says `up` |

What it costs: the nodes the autoscaler adds, about 5 to 9 Spot `n2d-standard-16` for an hour. [What 60 sessions
cost](cost.md) has the prices.

## The run

```sh
export HANGAR_ACCEPT_TEST_REPO=you/accept-target
python scripts/acceptance/capacity.py run --warm-wait 300
```

`--warm-wait` gives the first build and the warm pool time to fill. In order, the script:

1. Saves `capacity-stub` when it changed, and adds 6 synthetic members, each with an MCP token.
2. Boots all 60 sessions at once over MCP. It records new_session to idle, p50 and p95, split into warm and cold.
3. Asks for a 61st session as the first person, which must be refused at the cap.
4. Runs 3 turns on every session at once (`--turns`), and records turn time p50 and p95.
5. Runs the fairness batch: the first person submits their running-jobs cap plus two jobs, the others one each. Nobody
   may run more than the cap, or wait behind the batch.
6. Samples the `sandboxes-gvisor` pool's nodes every 15 seconds throughout. It records the count at the start and the
   peak, and when the first node came, the peak came and every session was idle.

At the end it releases everything, also after a failure or Ctrl-C. `capacity.py cleanup` removes what a killed run
left.

## The 01:00 power-down

Start a second run at about 00:40 ET with `--watch-power 2400 --turns 0`. The sessions boot, then keep turning until
the pools go down. The report then records what happened to each session and job.

[Sandbox nodes off at night](../gke/power.md) says what should happen. `power.sh down` drains first: each session
checkpoints and suspends. A turn cut off mid-way resumes from its checkpoint after power-up. Jobs wait in the queue
and say when the pools are back. The rehearsal confirms or contradicts that. Record which.

## Recording the result

The script writes `capacity-report.json` and `capacity-summary.md`. Paste the summary on KAN-218, and add a row
here.

| Date | Sessions | Boot p50 / p95 | Warm / cold | Nodes start → peak | First node / peak | Cap | Fairness | Power-down |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| *not run yet* | | | | | | | | |

## Cost per engineer-day

That comes from real work, not the rehearsal. After two weeks of repo 1 (KAN-23), run `hangar usage --by requester
--since 14d`. Divide each person's cost by the days they worked. Then check the spend caps in [spend
guardrails](spend-guardrails.md) against it.
