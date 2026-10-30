# The driver's own user on E2B

Can an `ask` or `deny-risky` turn on E2B keep hangar's driver out of the agent's reach? This page answers that for whoever decides the next step. Part of the [security model](README.md), beside [the driver sidecar on GKE](driver-sidecar.md).

**Status: refused by default (KAN-189).** A second user inside the microVM looks feasible but isn't built. The follow-up is [KAN-200](https://seanmkoval.atlassian.net/browse/KAN-200).

## What happens today

An E2B sandbox is one microVM with one user and no second container, so there is no [uid split](../steering.md#the-uid-split). The driver, its run key and the harness settings would belong to the agent's user. The agent could then answer its own approvals (security review H-1).

So when `HANGAR_UID_SPLIT` is unset, E2B treats it as `required`. A turn with `ask`, `deny-risky` or `governed` approvals is refused there, saying why. `auto` turns run as before. Docker and Kubernetes keep their own defaults.

An operator who sets `HANGAR_UID_SPLIT=auto` explicitly gets the old behaviour: those turns run on E2B with a warning. `hangar doctor` flags that setting, and `off` too, as a warning.

## Could hangar add a second user?

Probably. Docker's split needs two things: a way to run commands as root, and a way to run them as a chosen user. E2B's envd gives both. It picks each command's user from the request, so hangar's E2B provider can already run the split's setup script as root (`exec(user="root")`).

The setup script itself is provider-neutral. It adds the `hangar` user, checks that the agent has no `sudo`, and checks that `setpriv` keeps the driver's capabilities. A Firecracker guest is a real kernel, so root there should have the capabilities it needs.

Three pieces are Docker-only today, in `isolation.py`:

- `put` writes the driver, the run key and the run's files through a Docker exec with a tar on its stdin. On E2B it would write each file through envd as `hangar`, then set its mode.
- `start` reads the image's `PATH` from the container's config. On E2B it would come from the template.
- `open_terminal` opens the dashboard's read-only view as `hangar` through Docker. envd's terminals take a user too.

`harden.keep` and `isolation.supported` would then accept `e2b`. That is a few hundred lines with tests, so it isn't part of this change.

## What is unverified

Nothing here was run on E2B. This machine has no KVM, and the claims come from hangar's code and E2B's documented API.

- **envd on loopback.** The agent can reach envd inside its own VM. If envd answered it without the access token, the agent could run anything as root. The split would protect nothing, so this must be checked first.
- **No token at all.** CubeSandbox's envd can run without one (`HANGAR_E2B_ALLOW_UNAUTHENTICATED_ENVD`). Such a sandbox must never be split.
- **sudo in the template.** E2B's own base images give their user `sudo`. If a template built from hangar's image does too, the setup script finds it and the turn stays refused.
- **The rest.** Does `setpriv` keep ambient capabilities on E2B's guest kernel? Does envd write a file as `hangar` with the owner hangar expects? Does a resumed or forked snapshot keep the user and its modes?
