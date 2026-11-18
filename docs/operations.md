---
hide:
  - toc
---

# Operations: deploy it, prove it, keep it running

For the admin who runs one hangar for a team. Deploy it once, prove every deploy with the acceptance checks, run it
from the runbooks, and upgrade one release at a time.

<figure class="af-figure af-figure--wide">
--8<-- "operate-loop.svg"
<figcaption>Deploy once; then every deploy and every upgrade is proved on the cluster before the team depends on it.</figcaption>
</figure>
<figure class="af-figure af-figure--stacked">
--8<-- "operate-loop-stacked.svg"
<figcaption>Deploy once; then every deploy and every upgrade is proved on the cluster before the team depends on it.</figcaption>
</figure>

## Deploy

<div class="af-tools">
  <a class="af-tool" href="gke/"><small>The owner's install</small><b>Running on GKE</b><span>One control-plane pod and sandbox pods under gVisor, from terraform and one script.</span></a>
  <a class="af-tool" href="deploy-company/"><small>An existing cluster</small><b>Your company's cluster</b><span>A Kustomize base, a Terraform module that never creates a cluster, and released images.</span></a>
  <a class="af-tool" href="cicd/"><small>From GitHub Actions</small><b>Deploying from CI/CD</b><span>Build the image, wait for your approval, roll out, prove it and roll back on failure.</span></a>
  <a class="af-tool" href="local-microvms/"><small>Your own machine</small><b>Local microVMs</b><span>With Kata registered in Docker, every sandbox is a lightweight VM with its own kernel.</span></a>
</div>

## Prove

<div class="af-tools hg-tools--3">
  <a class="af-tool" href="acceptance/"><small>On the real cluster</small><b>Acceptance</b><span>A script that talks to hangar as its users do, over REST and MCP, and proves the release does what it says.</span></a>
  <a class="af-tool" href="troubleshooting/"><small>From the symptom</small><b>Troubleshooting</b><span>Start with <code>hangar doctor</code>; each failing line says how to fix it.</span></a>
  <a class="af-tool" href="doctor/"><small>Every night</small><b>The Cloud Doctor</b><span>Reads the flight records of failed, timed-out and slow runs and finds the causes a retry won't fix.</span></a>
</div>

## Run

<div class="af-tools">
  <a class="af-tool" href="runbooks/"><small>What to do, in order</small><b>Runbooks</b><span>Onboarding a team, on call, each incident, backups, drills and break-glass.</span></a>
  <a class="af-tool" href="runbooks/onboarding/"><small>Weeks 1 to 4</small><b>Onboarding a team</b><span>2 engineers on 1 repo, then 6 on 3, until <code>hangar repo check</code> is green for each repo.</span></a>
  <a class="af-tool" href="runbooks/on-call/"><small>When an alert fires</small><b>On call</b><span>Who answers hangar's and Tower's alerts, how fast, and the escalation path.</span></a>
  <a class="af-tool" href="team-scale/"><small>6 engineers, about 60 sandboxes</small><b>Team scale</b><span>What keeps one control plane responsive and fair, how to size GKE for it and what it costs.</span></a>
</div>

## Upgrade

<div class="af-tools hg-tools--3">
  <a class="af-tool" href="upgrading/"><small>The same every time</small><b>Upgrading</b><span>Back up, prebuild images, deploy in the right window, run <code>hangar doctor</code>.</span></a>
  <a class="af-tool" href="releases/"><small>Newest first</small><b>Release notes</b><span>Per release: which defaults change, what you configure, the ordered steps and how to roll back.</span></a>
  <a class="af-tool" href="runbooks/drills/"><small>Before each release</small><b>Recovery drills</b><span>Prove that hangar's database can be restored, upgraded and rolled back.</span></a>
</div>

## Next

What "up" means is on [the SLOs](slos.md), and who hears about it on [alerting](alerting.md). Before a team depends
on hangar, a reviewer reads [reviewing hangar for production](security/review/index.md).
