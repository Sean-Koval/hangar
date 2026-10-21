"""Which harness wrote a PR (#137; trailers.py): a delivered job's PR body ends with git trailers (`Hangar-Job`,
`Harness-Digest`, `Environment-Digest`, `Harness-Lock`) that `git interpret-trailers --parse` reads back; the commit
hangar lands carries them (a direct merge and auto-merge alike); the review packet shows the short digest and lock;
the body still goes through the secret scan; old turns without digests print no empty trailer; turns that ran with
different harness digests list each, with their turns."""

from __future__ import annotations

import json
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from hangar import tails, trailers
from hangar.jobs import handle_github
from tests.test_landing import HEAD, approval, gh, green, lands, pr_job, settle, svc  # noqa: F401 - fixtures

H1, H2 = "sha256:" + "a1" * 32, "sha256:" + "b2" * 32
E1 = "sha256:" + "e3" * 32


def _turns(_svc, monkeypatch, *digests):
    """The job's session turns, each (harness_digest, environment_digest), as their manifests recorded them."""
    ts = [SimpleNamespace(harness_digest=h, environment_digest=e) for h, e in digests]
    monkeypatch.setattr(trailers, "turns", lambda _jobs, _job: ts)


def _parse(text: str) -> list[tuple[str, str]]:
    out = subprocess.run(["git", "interpret-trailers", "--parse"], input=text, capture_output=True, text=True,
                         check=True).stdout
    return [tuple(x.split(": ", 1)) for x in out.splitlines()]


@pytest.mark.skipif(not shutil.which("git"), reason="needs git")
def test_the_pr_body_ends_with_trailers_git_reads(svc, monkeypatch):  # noqa: F811
    job = pr_job(svc, harness_lock={"name": "team-default", "revision": 14, "digest": H1})
    job.result.summary = "Fixed the CSV export.\n\nKey: value lines in a summary aren't trailers."
    _turns(svc, monkeypatch, (H1, E1), (H1, E1))
    body = svc._pr_body(job)
    assert _parse(body) == [("Hangar-Job", job.id), ("Harness-Digest", H1), ("Environment-Digest", E1),
                            ("Harness-Lock", "team-default@r14")]
    assert body.endswith("Harness-Lock: team-default@r14") and f"hangar job {tails.label(job.id)}" in body


def test_old_turns_print_no_empty_trailer_and_drifting_turns_list_each(svc, monkeypatch):  # noqa: F811
    job = pr_job(svc)
    _turns(svc, monkeypatch, (None, None))
    assert trailers.lines(svc, job) == [f"Hangar-Job: {job.id}"]
    _turns(svc, monkeypatch, (H1, E1), (H1, E1), (H2, E1), (H1, None), (H2, E1))
    assert trailers.lines(svc, job) == [f"Hangar-Job: {job.id}", f"Harness-Digest: {H1} (turns 1-2, 4)",
                                        f"Harness-Digest: {H2} (turns 3, 5)", f"Environment-Digest: {E1}"]
    _turns(svc, monkeypatch, (H2, E1), (H1, E1))
    assert trailers.lines(svc, job)[1:3] == [f"Harness-Digest: {H2} (turn 1)", f"Harness-Digest: {H1} (turn 2)"]


async def test_the_secret_scan_still_runs_on_the_body(svc, gh, monkeypatch):  # noqa: F811
    job = pr_job(svc)
    job.result.pr_number = None
    job.result.summary = "Done. The key was ghp_" + "Z" * 36 + " by the way."
    _turns(svc, monkeypatch, (H1, E1))
    await svc._ensure_pr(job, draft=False)
    opened = next(c for c in gh.calls if c.url.path == "/repos/o/r/pulls" and c.method == "POST")
    body = json.loads(opened.content)["body"]
    assert "ghp_" + "Z" * 36 not in body and body.endswith(f"Environment-Digest: {E1}")
    assert f"Harness-Digest: {H1}" in body  # a digest isn't taken for a secret


async def test_the_landed_commit_carries_the_trailers(svc, gh, monkeypatch):  # noqa: F811
    # GitHub merges it now (already mergeable): the squash commit's message ends with them
    lands(svc, approvers=["alice"], merge_method="squash")
    job = pr_job(svc, harness_lock={"name": "team-default", "revision": 14, "digest": H1})
    job.result.summary = "Fixed the CSV export."
    _turns(svc, monkeypatch, (H1, E1))
    gh.clean, gh.mergeable_state = True, "clean"
    await handle_github(svc, "pull_request_review", "d-1", approval(gh=gh))
    await settle(svc)
    merge = json.loads(next(c for c in gh.calls if c.url.path == "/repos/o/r/pulls/7/merge").content)
    assert merge["merge_method"] == "squash" and gh.merged_sha == HEAD
    msg = merge["commit_message"]
    assert msg.startswith("Fixed the CSV export.") and f"\n\nHangar-Job: {job.id}\nHarness-Digest: {H1}\n" in msg
    assert msg.endswith("Harness-Lock: team-default@r14")


async def test_auto_merge_carries_them_as_the_commit_body(svc, gh, monkeypatch):  # noqa: F811
    lands(svc, approvers=["alice"], merge_method="squash")
    job = pr_job(svc)
    _turns(svc, monkeypatch, (H1, E1))
    await handle_github(svc, "pull_request_review", "d-1", approval(gh=gh))
    await green(svc)
    arm = next(g for g in gh.graphql if "enablePullRequestAutoMerge" in g["query"])
    assert "commitBody: $body" in arm["query"]
    assert f"Hangar-Job: {job.id}\nHarness-Digest: {H1}" in arm["variables"]["body"]


def test_the_packet_shows_the_short_digest_and_lock(svc, monkeypatch):  # noqa: F811
    svc.settings.public_url = "https://hangar.example"
    job = pr_job(svc, harness_lock={"name": "team-default", "revision": 14, "digest": H1})
    _turns(svc, monkeypatch, (H1, E1))
    agent = next(x for x in svc.landing.packet(job).splitlines() if x.startswith("- **Agent:**"))
    assert agent.endswith(f" · harness [`sha256:a1a1a1a1…`](https://hangar.example/api/jobs/{job.id}/manifest) "
                          "(lock `team-default@r14`)"), agent
    _turns(svc, monkeypatch, (H2, E1), (H1, E1))
    agent = next(x for x in svc.landing.packet(job).splitlines() if x.startswith("- **Agent:**"))
    assert "`sha256:b2b2b2b2…`](https://hangar.example/api/jobs/" in agent and "(turn 1), [`sha256:a1a1a1a1…`]" in agent
    _turns(svc, monkeypatch, (None, None))
    job.harness_lock = None
    assert "harness" not in next(x for x in svc.landing.packet(job).splitlines() if x.startswith("- **Agent:**"))
