# Enforced intake and API keys

The release after v0.2.0 removes the safe release's two warn switches (KAN-74, #223). This page is for the operator
who upgrades a running hangar: what breaks, what to do before you deploy, and how to check it.

## What changes

- **`HANGAR_INTAKE_POSTURE=warn` and `off` are gone.** Jobs from GitHub, GitHub Projects, Jira, Slack and inbound
  webhooks always need the git gateway. Their sandboxes always get at least `allowlist` egress
  ([intake posture](../security/front-doors.md#intake-posture)).
- **`HANGAR_SUBSCRIPTION_LOGINS=any` is gone.** Jobs, schedules, webhooks, experiments, replays and intake jobs never
  run on a subscription login. They need an API key ([subscription logins](../team/subscription-logins.md)).
- **People's own sessions don't change.** A person's `new_session`, `create_sandbox` or `run_agent` still uses their
  own Claude Pro or Max, ChatGPT or Google login, with a warning.

## What breaks on deploy

A removed value in the server's environment (the `hangar-site` config map, `.env`, Compose) stops hangar from
starting. `hangar serve` exits and logs one line per switch, with the fix:

```
hangar can't start: HANGAR_SUBSCRIPTION_LOGINS=any was removed after v0.2.0 (#223): jobs, schedules, webhooks, ...
```

A removed value stored with `hangar config set` doesn't stop it. hangar ignores the value and enforces. `hangar doctor`
fails with the same words until you unset it.

Either way, work that relied on a switch is refused. A Codex job on a ChatGPT login fails before a sandbox starts:

```
job needs an API-key credential for codex; subscription logins are interactive-only (docs/team.md#subscription-logins). add an OpenAI API key: hangar cred add codex-api --harness codex --kind api_key --file openai-key.txt (your own; an admin adds --team for work no person owns)
```

An intake job without the gateway is refused at submit, with a message on its issue, ticket or thread.

## Before you upgrade

Do these on the running v0.2.0, in order.

1. **Find where a switch is set.** `hangar config list` shows each value's source; the config map may hold one too.

    ```sh
    hangar config list | grep -E 'INTAKE_POSTURE|SUBSCRIPTION_LOGINS'
    kubectl -n hangar get configmap hangar-site -o yaml | grep -E 'INTAKE_POSTURE|SUBSCRIPTION_LOGINS'
    ```

2. **Store an API key for each harness your jobs use**, as the person whose jobs they are. An admin adds `--team` for
   work no person owns. Put a spend cap on each key in the vendor's console.

    ```sh
    hangar cred add codex-api --harness codex --kind api_key --file openai-key.txt          # an OpenAI API key
    hangar cred add claude-api --harness claude-code --kind api_key --file anthropic-key.txt
    ```

3. **Turn on the git gateway and the egress proxy** where `HANGAR_INTAKE_POSTURE=warn` is set
   ([turning it on](../security/git-gateway.md#turning-it-on)). On GKE, `deploy/k8s/config.yaml` sets both.
4. **Remove the switches** wherever step 1 found them.

    ```sh
    hangar config unset HANGAR_SUBSCRIPTION_LOGINS
    hangar config unset HANGAR_INTAKE_POSTURE
    kubectl -n hangar edit configmap hangar-site   # delete either key, then: kubectl -n hangar rollout restart deploy/hangar
    ```

5. **Run `hangar doctor`.** The `subscription logins` check must be ok: a warning that names people with only
   subscription logins means their jobs will be refused. The `intake posture: gateway` check must be ok too.
6. **Deploy** as [upgrading.md](../upgrading.md) says, then run `hangar doctor` again.

## Local Docker

`off` is gone as well: it was never the documented setting for local work. Jobs you submit yourself over MCP, REST or
the CLI aren't intake jobs, so a laptop without an intake front door sees no change.

With one, run hangar on a Linux Docker host with the gateway, or on GKE. Docker Desktop can't run the proxied
sandboxes intake jobs need.

## Rolling back

Roll the image back to v0.2.0 ([upgrading.md](../upgrading.md#the-procedure)). v0.2.0 still reads both switches. Set
one again only if a team must run without the gateway or the API keys for a while.
