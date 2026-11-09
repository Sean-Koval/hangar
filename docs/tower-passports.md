# Tower passports on the job API

How does an agent holding a Tower passport call hangar's job API as the person it works for? This page is for the admin who turns that on. The contract is [spec 0015](specs/0015-governance-decision-point.md#9-identity).

## Turning it on

`HANGAR_JOB_PASSPORTS=on` makes `/api/jobs` and every route under it accept a passport as the bearer. It is `off` by default, and hangar's own tokens work as before either way.

| Setting | Default | What it is |
| --- | --- | --- |
| `HANGAR_JOB_PASSPORT_ISSUER` | `HANGAR_GOVERNANCE_URL` | Tower's URL: the passport's issuer, and where its keys are published |
| `HANGAR_JOB_PASSPORT_AUDIENCE` | `<issuer>/mcp` and `<issuer>` | The audiences accepted, comma-separated |
| `HANGAR_JOB_PASSPORT_AGENTS` | empty | Agents that may act as themselves, as `agent=person,...` |

With no issuer set, hangar accepts no passport and says so in its log.

Tower mints passports only for its own audiences, so hangar accepts those by default. A passport an agent holds for Tower's gateway therefore also works here. It acts only as the person it names, and only once an admin turns this on.

## What hangar checks

hangar reads Tower's keys from `<issuer>/.well-known/jwks.json` and checks each passport before it acts:

- an EdDSA signature by a key Tower publishes;
- the issuer and the audience;
- the expiry, with 60 seconds of clock skew;
- the chain of agents and its `act` claim, at most three agents.

A tampered, expired, wrong-audience or unknown-issuer passport is refused with 401. So is one whose subject hangar can't name. Every refusal is audited `passport.refused`, without the token.

## Who the caller is

| Subject | Runs as |
| --- | --- |
| `person:<email>` | The hangar person that email is linked to: `hangar person link alice email alice@example.com` |
| `agent:<id>` | The person listed for that agent in `HANGAR_JOB_PASSPORT_AGENTS` |
| Anything else | Refused |

The caller holds the `jobs` scope only, as a service token does. It sees only the jobs it submitted, under that person's caps. A passport never approves or rejects a job: it always names an agent, and approving is a person's own act.

## How Tower sees hangar's people

When hangar asks Tower about a release or a landing, it names each person in the form Tower uses:

- the hangar person, and the email linked to them;
- a GitHub account as `github:<login>`, with the login GitHub reported in the same event;
- a Jira account as `jira:<account id>`.

Tower maps a person by the email first. `github:<login>` maps only when Tower's `hangar.people` map has that key, so link each person's email in hangar.

hangar finds the person from the account's numeric id, never from the login, because a renamed login can be registered by someone else. When only the id is known, hangar sends `github-id:<id>`, which Tower maps to nobody. An unmapped signer leaves the approval waiting.
