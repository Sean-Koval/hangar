"""KAN-216: how far production is behind main (deploylag.py). Doctor and the overview show the deployed commit, its
tag, and the commits and days it is behind main; GitHub being unreachable, or no token, makes the lag "unknown" and
fails nothing; the alert line is written only past the threshold while the infra is up; the console draws it with
Airframe's inline data, grey unless over the threshold."""

from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import httpx
import pytest

from hangar import deploylag, metrics, power, powerhold
from hangar.github import GitHub
from hangar.power import Power

NY = ZoneInfo("America/New_York")
ROOT = Path(__file__).resolve().parents[1]
DEPLOYED = "e0a79c8" + "0" * 33
MAIN = "f" * 40
RUN = "https://github.com/Sean-Koval/hangar/actions/runs/1"


def at(text: str) -> float:
    return datetime.fromisoformat(text).replace(tzinfo=NY).timestamp()


def _commit(sha: str, when: str, merge: bool) -> dict:
    return {"sha": sha, "parents": [{"sha": "a"}, {"sha": "b"}] if merge else [{"sha": "a"}],
            "commit": {"committer": {"date": when}}}


class FakeGitHub:
    """GitHub's REST answers deploylag asks for, counting the calls; `down` makes every call fail."""

    def __init__(self, behind: int = 383, down: bool = False):
        self.calls: list[str] = []
        self.behind, self.down = behind, down

    def handler(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path
        self.calls.append(path)
        if self.down:
            raise httpx.ConnectError("offline")
        if path.endswith("/git/ref/heads/main"):
            return httpx.Response(200, json={"object": {"sha": MAIN}})
        if "/compare/" in path:
            assert req.url.params["per_page"] == "100"
            if not self.behind:
                return httpx.Response(200, json={"status": "identical", "ahead_by": 0, "commits": []})
            return httpx.Response(200, json={"status": "ahead", "ahead_by": self.behind, "commits": [
                _commit("1", "2026-10-15T09:00:00Z", False),  # a branch commit, older than its merge
                _commit("2", "2026-10-17T14:00:00Z", True),  # the oldest merge production lacks
                _commit("3", "2026-10-28T14:00:00Z", True)]})
        if path.endswith("/tags"):
            return httpx.Response(200, json=[{"name": "v0.2.0", "commit": {"sha": "01c136c" + "0" * 33}},
                                             {"name": "v0.1.0", "commit": {"sha": DEPLOYED}}])
        return httpx.Response(404, json={"message": "Not Found"})


def _h(settings, store, gh: FakeGitHub | None, clock: float):
    p = Power(store, settings, clock=lambda: clock)
    transport = httpx.MockTransport(gh.handler) if gh else None
    jobs = SimpleNamespace(_github=lambda: GitHub("t", transport=transport) if gh else None)
    return SimpleNamespace(store=store, settings=settings, jobs=jobs, fleet=SimpleNamespace(power=p))


def _deploy(store, **kw):
    body = {"result": "passed", "sha": DEPLOYED, "version": "0.1.0", "run_url": RUN,
            "image": f"r/control-plane:{DEPLOYED}@sha256:1", **kw}
    return power.record_deploy(store, body, "deploy", clock=lambda: at("2026-10-17T16:00:00"))


def test_working_days_skip_the_weekend():
    mon = at("2026-10-26T10:00:00")
    assert deploylag.working_days(mon, at("2026-10-28T09:00:00"), NY) == 2  # Wednesday: not yet over 2
    assert deploylag.working_days(mon, at("2026-10-29T09:00:00"), NY) == 3
    fri = at("2026-10-23T18:00:00")
    assert deploylag.working_days(fri, at("2026-10-26T08:00:00"), NY) == 1  # the weekend doesn't count
    assert deploylag.working_days(mon, mon, NY) == 0


def test_days_count_from_the_oldest_merge_production_lacks():
    commits = [_commit("1", "2026-10-15T09:00:00Z", False), _commit("2", "2026-10-17T14:00:00Z", True)]
    assert deploylag.oldest_undeployed(commits) == datetime.fromisoformat("2026-10-17T14:00:00+00:00").timestamp()
    # squash merges: no merge commits, so every commit counts
    assert deploylag.oldest_undeployed(commits[:1]) == datetime.fromisoformat("2026-10-15T09:00:00+00:00").timestamp()
    assert deploylag.oldest_undeployed([]) is None


def test_a_rolled_back_deploy_runs_the_image_it_replaced(store):
    _deploy(store)
    power.record_deploy(store, {"result": "rolled_back", "sha": MAIN, "previous_image":
                                f"r/control-plane:{DEPLOYED}@sha256:1"}, "deploy")
    run = deploylag.running(store)
    assert run["sha"] == DEPLOYED and run["version"] == "0.1.0" and run["rolled_back_from"] == MAIN
    assert deploylag.repo(store, SimpleNamespace(deploy_repo=None)) == "Sean-Koval/hangar"
    assert deploylag.repo(store, SimpleNamespace(deploy_repo="o/r")) == "o/r"


async def test_doctor_shows_the_deployed_commit_its_tag_and_the_lag(settings, store):
    _deploy(store)
    gh = FakeGitHub()
    h = _h(settings, store, gh, at("2026-10-29T11:00:00"))
    (check,) = await deploylag.doctor_checks(h)
    assert check["name"] == "production lag" and check["status"] == "warn"
    assert check["detail"].startswith(
        "e0a79c8 (v0.1.0), deployed 17 Oct: 383 commits and 12 days behind main (9 working)")
    assert "over 2 working days" in check["detail"] and "docs/runbooks/deploy.md" in check["fix"]
    v = h.fleet.power.overview()["deploy_lag"]
    assert v["tag"] == "v0.1.0" and v["behind_commits"] == 383 and v["working_days_behind"] == 9
    assert v["over"] and v["alerting"] and v["main_sha"] == MAIN
    n = len(gh.calls)
    await deploylag.doctor_checks(h)  # cached: no second look within REFRESH
    assert len(gh.calls) == n
    # a new deploy looks at once; main's head is now deployed
    _deploy(store, sha=MAIN, image=f"r/control-plane:{MAIN}@sha256:2", version="0.3.0")
    gh.behind = 0
    (check,) = await deploylag.doctor_checks(h)
    assert check["status"] == "ok" and "nothing to deploy" in check["detail"], check


@pytest.mark.parametrize("gh", [None, FakeGitHub(down=True)], ids=["no token", "offline"])
async def test_without_github_the_lag_is_unknown_and_nothing_fails(settings, store, gh):
    h = _h(settings, store, gh, at("2026-10-29T11:00:00"))
    assert await deploylag.doctor_checks(h) == []  # no deploy recorded (a laptop): no line
    _deploy(store)
    (check,) = await deploylag.doctor_checks(h)
    assert check["status"] == "warn" and "commits behind main unknown" in check["detail"]
    assert check["detail"].startswith("e0a79c8, deployed 17 Oct")
    v = h.fleet.power.overview()["deploy_lag"]
    assert v["state"] == "unknown" and v["behind_commits"] is None and not v["over"] and not v["alerting"]


async def test_the_alert_fires_past_the_threshold_only_while_the_infra_is_up(settings, store, caplog, monkeypatch):
    caplog.set_level(logging.INFO, "hangar.slo")
    monkeypatch.setattr(metrics, "M", metrics.Metrics())
    _deploy(store)
    gh = FakeGitHub()
    h = _h(settings, store, gh, at("2026-10-30T09:00:00"))
    store.kv_set_json(deploylag.KEY, None)

    def lines():
        return [r.getMessage() for r in caplog.records if r.name == "hangar.slo" and "deploy_lag" in r.getMessage()]

    await deploylag.tick(h)
    assert re.fullmatch(r"event=deploy_lag commits=383 working_days=10 days=12", lines()[-1])
    assert metrics.M.deploy_lag_over._value.get() == 1 and metrics.M.deploy_lag_commits._value.get() == 383
    # powered down for the night: no line, though production is just as far behind
    p = h.fleet.power
    p.mark("down", "power.sh", up_cron="50 9 * * *", time_zone="America/New_York")
    n = len(lines())
    await deploylag.tick(h)
    assert len(lines()) == n and metrics.M.deploy_lag_over._value.get() == 0
    assert p.overview()["deploy_lag"]["over"] and not p.overview()["deploy_lag"]["alerting"]
    # up again, but held: still no line
    p.mark("up", "power.sh")
    powerhold.set_hold(store, {"until": p.clock() + 86400}, "sean", NY, p.clock())
    await deploylag.tick(h)
    assert len(lines()) == n
    powerhold.end_hold(store, None, "sean", p.clock())
    await deploylag.tick(h)
    assert len(lines()) == n + 1
    # a higher threshold: no line
    settings.deploy_lag_alert_working_days = 20
    await deploylag.tick(h)
    assert len(lines()) == n + 1


def test_the_console_draws_the_lag_as_grey_inline_data_and_orange_only_past_the_threshold():
    js = (ROOT / "src" / "hangar" / "ui" / "app.js").read_text()
    html = (ROOT / "src" / "hangar" / "ui" / "index.html").read_text()
    assert 'id="fleet-prod" class="af-page-head__sum fleet-prod" hidden' in html
    body = js[js.index("function drawProd"):]
    body = body[:body.index("\n}\n")]
    assert "af-meta__item" in body and 'mod: v.over ? "approval" : ""' in body
    assert "af-status--" not in body  # information, not a status
    assert "drawProd(ov)" in js
