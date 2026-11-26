- **The live intake check** (KAN-27): `scripts/acceptance/intake_live.py` walks one real Jira ticket through intake on
  the deployed hangar. A person moves the ticket when asked. It checks for exactly one job for the mover, no duplicate
  after a second move while that job is open, and a succeeded job with a draft PR whose title and `Jira:` line carry
  the key. `preflight` spends nothing. The checklist is [The live intake check](docs/runbooks/intake-check.md).
