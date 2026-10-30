# Job release and landing through Tower

How does hangar ask Tower before it releases an untrusted job or lands a PR? This page is for whoever turns governance on for those two approvals. The contract is [spec 0015](specs/0015-governance-decision-point.md). [Tower beside hangar](runbooks/tower.md) covers the settings, and [governed approvals](governed-approvals.md) covers tool calls and requests.

## What asks Tower

| What | When | Decision kind |
| --- | --- | --- |
| Job release | A job waits for a trusted person, and someone approves it: REST, MCP `approve_job`, or `/hangar approve` on GitHub or Jira | `job.release` |
| Landing | A PR is approved and hangar would arm auto-merge for that head | `landing.approve` |

hangar's own rule always runs first. Nobody approves their own work, and a refused self-approval never reaches Tower. Tower never makes hangar more permissive: after Tower's allow, hangar still pins the SHA, waits for the required checks and follows branch protection.

## The modes

`HANGAR_GOVERNANCE` sets the mode for every kind, and `HANGAR_GOVERNANCE_MODES` overrides it per kind, for example `job.release=shadow,landing.approve=shadow`.

| Mode | Who decides | What Tower sees |
| --- | --- | --- |
| `off` (default) | hangar's rules, as before | Nothing |
| `shadow` | hangar's rules | A dry run, with the person who acted as signer |
| `enforce` | Tower, failing closed | The question, with the person who acted as signer |

In `shadow`, both answers are kept on the decision as `governance`: the job's `approval.governance`, or the landing's `governance` for the head it covers. Each comparison is audited `governance.shadow`. A different answer is also audited `governance.disagree` and counted in `hangar_governance_disagreements_total`. A release's dry run runs in the background, so the job is queued at once.

## Job release in enforce

When a job starts waiting, hangar opens Tower's approval within seconds. Its id is kept as `approval.workspace_approval_id`.

| What happens | The job |
| --- | --- |
| Someone signs in Tower: Slack, Teams or the web | Queued, with Tower's receipt on `approval.receipt` |
| Tower or a person in Tower says no | Rejected, with Tower's reason posted on the issue or ticket |
| The approval expires or is withdrawn in Tower | Dropped, as hangar's own expiry drops it |
| A person approves in hangar and Tower allows | Queued, with the receipt |
| A person approves in hangar and Tower wants more | Still waiting; the issue says so |
| Tower refuses that person's signature | Still waiting; the person is told why |
| A person rejects in hangar | Rejected; hangar withdraws Tower's approval |

An `approval.decided` event from Tower settles the job at once. A status read every 60 seconds settles it if the event is lost.

## Landing in enforce

Once every landing check passes, hangar asks Tower about the approved head, with the reviewer as signer. It arms auto-merge only on Tower's `allow`. The receipt is kept on the landing and shown in the review packet.

A `wait` answer becomes the landing's note, naming the approval. hangar arms the PR once Tower allows it. A new head needs a new approval, and hangar withdraws the old one.

## When Tower can't answer

Nothing that goes wrong is ever an allow. In `enforce`:

- A waiting job stays waiting. Its issue or ticket is told "approval service unavailable" once per outage.
- A landing isn't armed. Its note says "approval service unavailable", and hangar asks again every 60 seconds.

[Break-glass](runbooks/break-glass-tower.md) covers a long outage.

## How hangar proves itself

`HANGAR_GOVERNANCE_AUTH` picks the credential for these calls.

| Value | Credential |
| --- | --- |
| `key` (default) | The service key in the vault credential `HANGAR_GOVERNANCE_CREDENTIAL` |
| `passport` | A five-minute token from hangar's own OIDC issuer, with subject `hangar:control-plane` |

With `passport`, hangar exchanges its token at Tower's token endpoint for a 15-minute passport. The endpoint is `HANGAR_GOVERNANCE_TOKEN_URL`, by default `<HANGAR_GOVERNANCE_URL>/oauth/token`. hangar checks the passport against Tower's published keys before it uses it: issuer, audience, expiry and the chain of agents. It must name the agent `hangar`.

A failed exchange fails every decision closed. Tower's decision API takes passports only once [Tower#14](https://github.com/Sean-Koval/tower/issues/14) lands, so keep `key` until then.
