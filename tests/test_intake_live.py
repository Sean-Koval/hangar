"""KAN-27: scripts/acceptance/intake_live.py against a stand-in hangar and GitHub (no cluster, no Jira, no model).

The person's moves in Jira are played by the fake: the first `ask` makes the job, the second (the move out and back)
makes a duplicate only when the test says hangar would.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "acceptance"))
try:
    import hangar_accept as ha
    import intake_live as il
finally:
    sys.path.pop(0)

KEY = "ACC-7"
PR = "https://github.com/acme/target/pull/12"


class FakeHangar:
    def __init__(self, *, duplicate: bool = False, end: str = "succeeded", pr: str | None = PR,
                 trigger: dict[str, Any] | None = None, power: str = "up", open_job: bool = False):
        self.jobs: list[dict[str, Any]] = []
        self.duplicate, self.end, self.pr, self.power = duplicate, end, pr, power
        self.trigger = trigger if trigger is not None else {"status": "Ready for agent"}
        self.polls = 0
        if open_job:
            self.jobs.append(self._job("job-old", time.time() - 3600, "running"))

    def _job(self, jid: str, at: float, state: str = "running") -> dict[str, Any]:
        return {"id": jid, "state": state, "person": "sean", "source": "jira", "source_ref": f"jira:{KEY}",
                "title": f"{KEY}: fix the thing", "labels": {"jira.issue": KEY}, "created_at": at,
                "result": {}, "usage": {"cost_usd": 0.42}}

    def move(self, n: int) -> None:
        if n == 1 or self.duplicate:
            self.jobs.insert(0, self._job(f"job-{n}", time.time()))

    def handler(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path == "/api/settings/HANGAR_JIRA":
            return httpx.Response(200, json={"value": {"trigger": self.trigger,
                                                       "projects": {"ACC": {"repo": "acme/target"}}}})
        if path == "/api/admin/power":
            return httpx.Response(200, json={"power": {"state": self.power}})
        if path == "/api/doctor":
            return httpx.Response(200, json=[{"name": "jira", "status": "ok", "detail": "token works"}])
        if path == "/api/jobs":
            assert req.url.params["source"] == "jira"
            return httpx.Response(200, json=self.jobs)
        if path.startswith("/api/jobs/"):
            job = next(j for j in self.jobs if j["id"] == path.rsplit("/", 1)[1])
            self.polls += 1
            if self.polls > 2 and job["state"] not in il.TERMINAL:
                job["state"] = self.end
                if self.pr:
                    job["result"] = {"pr_url": self.pr}
            return httpx.Response(200, json=job)
        return httpx.Response(404, json={"detail": path})


def github(draft: bool = True, title: str = f"{KEY}: fix the thing", body: str = f"Done.\n\nJira: {KEY}\n"):
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/repos/acme/target/pulls/12"
        assert req.headers["Authorization"] == "Bearer gh-test"
        return httpx.Response(200, json={"draft": draft, "title": title, "body": body})
    return httpx.MockTransport(handler)


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch):
    def make(fake: FakeHangar, gh: httpx.MockTransport | None = None, token: str | None = "gh-test"):
        api = ha.Api("http://hangar.test", "t")
        api.http = httpx.Client(base_url="http://hangar.test", transport=httpx.MockTransport(fake.handler))
        moves = iter(range(1, 10))
        monkeypatch.setattr(il, "ask", lambda prompt, opts: fake.move(next(moves)))
        monkeypatch.setattr(il.time, "sleep", lambda s: None)
        monkeypatch.setattr(il, "GITHUB_TRANSPORT", gh or github())
        for name in ("HANGAR_ACCEPT_GITHUB_TOKEN", "GITHUB_TOKEN", "GH_TOKEN"):
            monkeypatch.delenv(name, raising=False)
        if token:
            monkeypatch.setenv("HANGAR_ACCEPT_GITHUB_TOKEN", token)
        return api
    return make


def opts(**kw: Any) -> argparse.Namespace:
    base = {"ticket": KEY, "start_timeout": 5, "dup_wait": 0, "job_timeout": 5, "poll": 0, "yes": True}
    return argparse.Namespace(**{**base, **kw})


def test_a_good_run_passes_every_check(world) -> None:
    api = world(FakeHangar())
    results: dict[str, Any] = {"ticket": KEY}
    assert il.run(api, opts(), results) == []
    assert results["job"] == "job-1" and results["pr"] == PR and results["state"] == "succeeded"
    assert set(results["checks"].values()) == {"pass"}
    assert {"one_job", "no_duplicate", "linked_person", "succeeded", "pr_opened", "pr_draft", "pr_title",
            "pr_jira_line"} <= set(results["checks"])
    assert results["cost_usd"] == 0.42


def test_a_second_move_that_starts_a_job_fails(world) -> None:
    results: dict[str, Any] = {}
    failures = il.run(world(FakeHangar(duplicate=True)), opts(), results)
    assert results["checks"]["no_duplicate"].startswith("fail: 2 jobs")
    assert any(f.startswith("no_duplicate") for f in failures)


def test_a_pr_that_isnt_a_draft_or_linked_fails(world) -> None:
    results: dict[str, Any] = {}
    il.run(world(FakeHangar(), gh=github(draft=False, body="no key here")), opts(), results)
    assert results["checks"]["pr_draft"].startswith("fail")
    assert results["checks"]["pr_jira_line"].startswith("fail")
    assert results["checks"]["pr_title"] == "pass"


def test_without_a_github_token_the_pr_is_not_checked_never_passed(world) -> None:
    results: dict[str, Any] = {}
    assert il.run(world(FakeHangar(), token=None), opts(), results) == []
    for k in ("pr_draft", "pr_title", "pr_jira_line"):
        assert results["checks"][k].startswith("not checked")


def test_a_failed_job_without_a_pr_fails(world) -> None:
    results: dict[str, Any] = {}
    failures = il.run(world(FakeHangar(end="failed", pr=None)), opts(), results)
    assert results["checks"]["succeeded"].startswith("fail: the job ended failed")
    assert results["checks"]["pr_opened"].startswith("fail: no PR")
    assert len(failures) == 2


def test_no_job_after_the_move_stops_the_run(world, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeHangar()
    api = world(fake)
    monkeypatch.setattr(il, "ask", lambda prompt, opts: None)  # the webhook never arrives
    with pytest.raises(ha.Fail, match="a job for ACC-7 after the move"):
        il.run(api, opts(start_timeout=0), {})


@pytest.mark.parametrize("fake, why", [
    (FakeHangar(open_job=True), "already has an open job"),
    (FakeHangar(power="night"), "pools are night"),
    (FakeHangar(trigger={}), "no trigger"),
])
def test_preflight_names_what_isnt_ready(world, fake: FakeHangar, why: str) -> None:
    problems, _ = il.preflight(world(fake), KEY)
    assert any(why in p for p in problems), problems


def test_preflight_refuses_an_unmapped_project_and_a_bad_key(world) -> None:
    api = world(FakeHangar())
    assert any("isn't mapped" in p for p in il.preflight(api, "OTHER-1")[0])
    assert "isn't a Jira issue key" in il.preflight(api, "not a key")[0][0]
    assert il.preflight(api, KEY)[0] == []


def test_the_summary_is_a_table_to_paste(world) -> None:
    results: dict[str, Any] = {"ticket": KEY, "url": "http://hangar.test"}
    results["passed"] = not il.run(world(FakeHangar()), opts(), results)
    text = il.summary(results)
    assert "PASS" in text and "| one_job | pass |" in text and PR in text
    json.dumps(results)  # the report is JSON
