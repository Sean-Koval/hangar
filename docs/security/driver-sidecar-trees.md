# Trees and the verify gate under the sidecar

How does a turn under [the driver sidecar](driver-sidecar.md) keep per-edit trees and the verify gate? This page is for whoever reviews that design ([KAN-187](https://seanmkoval.atlassian.net/browse/KAN-187)).

## The checkout is in the agent's container

[Trees](../steering.md#trees) hash the working tree after each edit. The verify gate runs the job's check when the agent settles. Both need the checkout, and only the `sandbox` container has it. The driver must never run the agent's code as its own uid.

## Run through the relay

The driver sends a `run` request on the relay's `ctl` connection. The relay runs it as the agent, from the turn's directory, in the harness's environment. It sends back the exit status and the end of the output.

The relay serves these until the driver closes `ctl`, so the end tree still comes after the harness exits. A request past its timeout is killed.

- **The verify gate.** The check runs as the agent. Its rounds file is the agent's, as under the Docker split. A check that times out counts as exit 124.
- **Trees.** Each hash is `driver.py tree` in the agent's container. Its scratch directory is in the agent's home, removed at the turn's end. Each hash costs one more process start than under Docker.

## Why the trees aren't trusted

The agent could change what a hash in its own container returns, as in a turn without the uid split. So the driver writes these lines with `by: agent`, and hangar records them as `trusted: false`. Replays check such trees again, as they check a single-user turn's. The gate was never trusted: hangar's own verify after the turn decides delivery.

## Why not a shared checkout

The other option was the checkout on an emptyDir, read-write in `sandbox` and read-only in `hangar-driver`. The driver could then hash the tree itself, and trust it.

It wasn't taken. GKE Sandbox makes an emptyDir a tmpfs inside gVisor. The whole checkout, dependencies included, would count against the pod's memory, not its disk.

A Pod snapshot would carry it in its memory image too. This was read in the gVisor source, not checked on a cluster. The gate would still need the relay, since the check must run as the agent.
