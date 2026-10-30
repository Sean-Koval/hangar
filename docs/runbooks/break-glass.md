# Runbook: governance break-glass

What to do when Tower (the Govern plane) is unreachable, or denies what it shouldn't. Also how to put a decision kind
back from `enforce` to `shadow` or `off`. The design is [spec 0015](../specs/0015-governance-decision-point.md).
hangar enforces, Tower decides whether a person or a policy allows something, and nothing that goes wrong on the way
is ever an allow.

**Break-glass itself isn't built yet.** Read [what is built today](#today-hangar-decides-alone) first. With every
kind `off` (the default), hangar never asks Tower. The [procedure](break-glass-tower.md) covers a kind in `enforce`.

## Today: hangar decides alone

What is built (spike #171, PR #208):

- `src/hangar/governance.py`, the client side of the `hangar.decision/v1` contract. It has the request and decision
  models, `LocalDecisionPoint` (hangar's own rules) and `HttpDecisionPoint` (Tower over HTTP).
- `HttpDecisionPoint` allows 2 s to connect and 5 s in all, with one retry on a 401 with a fresh token. Its circuit
  breaker opens after 5 failures in 30 s, for 30 s, and it carries the fail-closed table.
- `GovernanceRouter` sets `off`, `shadow` or `enforce` per kind. The `BreakGlass` record needs two different admins,
  for at most 24 hours.
- `tests/test_governance.py`, the contract tests against a fake Tower: every failure fails closed for every kind, and
  shadow never changes an outcome. Break-glass needs two admins and lasts at most 24 hours.

Every approval path calls it through `HANGAR_GOVERNANCE` and `HANGAR_GOVERNANCE_MODES`:
[job release and landing](../tower-release-landing.md) and [governed approvals](../governed-approvals.md).

What isn't built:

- **No command, no doctor check.** `hangar governance local` (break-glass) and the doctor's `governance` check are
  planned.

With every kind `off`, every approval is decided by hangar's own rules, as before. That is an admin or a linked repo approver, never on
their own job (`approvals.decide`, #121), the taint gate (#130) and the self-approval rule (#160).

If every kind is `off` and Tower is down, or denies, **there is nothing to do in hangar**: nothing waits on Tower.
Tower's side (its outage behaviour, its decisions API) is in Tower's own docs.

## Moved from this page

| Section | Now on |
| --- | --- |
| <span id="once-tower-decides-planned"></span>Once Tower decides (planned) | [Once Tower decides](break-glass-tower.md) |
| <span id="the-modes"></span>The modes | [Once Tower decides: the modes](break-glass-tower.md#the-modes) |
| <span id="when-tower-is-unreachable"></span>When Tower is unreachable | [Once Tower decides](break-glass-tower.md#when-tower-is-unreachable) |
| <span id="putting-a-kind-back-from-enforce"></span>Putting a kind back from enforce | [Once Tower decides](break-glass-tower.md#putting-a-kind-back-from-enforce) |
| <span id="break-glass"></span>Break-glass | [Once Tower decides: break-glass](break-glass-tower.md#break-glass) |
| <span id="when-tower-denies-what-it-shouldnt"></span>When Tower denies what it shouldn't | [Once Tower decides](break-glass-tower.md#when-tower-denies-what-it-shouldnt) |
