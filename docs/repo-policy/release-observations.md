# Offline multi-repository release observations

This page is part of [Repo policy: the defaults every front door applies](../repo-policy.md).

`hangar.release_readiness` is a Python planning library for a bounded multi-repository
release: code, build, infrastructure, database scope, activation, production and rollout.
It does not run a release, collect provider evidence, grant approval, or change repo
policy. It has no MCP, REST or CLI entry point. Existing `/mcp` job intake remains the
entry point for coding jobs; connecting a Symphony-side agent or another MCP client does
not require Tower. This library does not establish live qualification of those clients.

The [generic profile](https://github.com/Sean-Koval/hangar/blob/main/examples/release-profile.json)
uses separate code, infrastructure, database and deployment repositories. Provider
specifics such as Jenkins build IDs or Jira references are observation metadata; no
provider is imported or contacted. Jira can retain the high-level change and GitHub
Projects the finer work items without either becoming release approval authority.
Repository names, revisions, branch targets, approvers and environments are explicit
profile data. One profile has one target environment; infrastructure approval, activation,
production and rollout must match it. Shared-infrastructure approval scopes are not
modeled by this first contract. Secrets and credentials do not belong in these files.

```python
from hangar.release_readiness import evaluate, profile_digest

# profile and observations are caller-owned JSON data, read outside this library.
report = evaluate(profile, observations, as_of="2026-10-18T12:00:00Z")
```

The profile and observation JSON schemas live in `docs/api/release-profile.schema.json`
and `docs/api/release-observation.schema.json`. Schema validation describes shape;
`ReleaseProfile.model_validate` additionally checks dependency order and cross-stage
identity. `profile_digest(profile)` is the canonical full-profile digest. Every
observation must bind it and the release ID, exact requirement ID, repository, revision
and bindings. The digest detects a different profile; it does not authenticate evidence.

- A build must refer to the merged code commit and carry an immutable artifact digest.
- Infrastructure and production approvals must refer to the exact repository, revision
  and PR. Merge observations distinguish the reviewed PR head from the landed commit;
  squash/rebase/merge commits need not equal that reviewed head. Production also binds
  environment and artifact.
- Activation binds the infrastructure revision and artifact; production binds activation;
  rollout binds the production revision, environment and artifact. Later stages cannot
  satisfy while a prerequisite is missing, uncertain or blocked.
- Database scope is an explicit release-bound determination, with an actor and reason.
  A `not_required` observation is a caller assertion, not an authorized exemption.
  `required` needs a matching migration observation. The example is not a statement of
  any company's database policy; obtain that policy before configuring a real release.
- Observation states are `observed`, `not_satisfied` and `unknown`. They are never
  `verified`. Every record needs a source reference and UTC timestamp; the caller passes
  an explicit UTC `as_of`, and profiles require a freshness limit of at most one day.
  Future or stale observations are unknown. Negative or contradictory observations
  block. Missing observations stay missing. Invalid profiles are rejected.

Reports use `satisfied_by_observation`, `missing`, `unknown` and `blocked`. `status`
only covers configured requirements. `omitted_stages` is explicit, and `release_status`
and `rollout_status` remain unknown when their scope is omitted. Even a complete report
with every observation satisfied always returns `external_verification: false` and
`authorization_granted: false`. Source references and approver names are not independently
checked. A merge receipt is not rollout evidence.

Native live head/check/approval revalidation in `landing.py`, GitHub protection, SRE
approval and production gates remain authoritative. Provider-backed evidence collection,
authenticated multi-repository MCP access, durable release state, Jenkins/ArgoCD
integration, trigger-repository writes and production deployment are future work. This
library does not claim those acceptance criteria are met.
