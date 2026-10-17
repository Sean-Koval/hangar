"""Injection canaries in shadows (#184): a shadow chosen for a canary gets one benign instruction with a fresh token
inside the untrusted fence (or a repo file, never committed); hangar records it privately, checks after the turn
whether the agent obeyed and writes `canary.json` to the outbox; canary.invalid is always refused and counted.

On the fake provider with the scripted agent of tests/test_harness_lock.py, real git and tmux."""

from __future__ import annotations

import asyncio
import json
import re
import shutil
from types import SimpleNamespace

import pytest

from hangar import canaries, experiments, shadows, triggers
from hangar.egress_proxy import Denied
from hangar.exchange import UNTRUSTED_RULES
from hangar.models import ContextItem, JobState, Sandbox
from hangar.repopolicy import CanaryPolicy, RepoPolicy, ShadowPolicy, repo_key
from tests import test_harness_thaw as th
from tests.test_harness_lock import DONE
from tests.test_jobs import _until
from tests.test_shadows import _real_job, _shadowed

skillenv, world, jobs = th.skillenv, th.world, th.jobs
needs_tools = pytest.mark.skipif(not shutil.which("tmux") or not shutil.which("git"), reason="needs tmux and git")
TOKEN = re.compile(r"CANARY-[0-9a-f]{8}")

# reads every attachment and the repo's files for a token, and does what the canary asks: adds it to README
OBEDIENT = r"""#!/usr/bin/env bash
cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
echo '{"type":"system","subtype":"init","model":"claude-sonnet-5-5","session_id":"c1"}'
tok=$(cat "$HANGAR_JOB_DIR"/* NOTES.local.md 2>/dev/null | grep -oE 'CANARY-[0-9a-f]{8}' | head -1)
[ -n "$tok" ] && echo "$tok" >> README.md
echo x >> work.txt && git add work.txt && git commit -qm "agent: work"
echo '{"type":"result","subtype":"success","is_error":false,"result":"done",'\
'"usage":{"input_tokens":1,"output_tokens":1}}'
"""

# ignores every instruction, but notes whether the repo file was there
IGNORING = OBEDIENT.replace('[ -n "$tok" ] && echo "$tok" >> README.md',
                            "test -f NOTES.local.md && echo planted >> work.txt")

TICKET = ContextItem(name="jira-ABC-12.md", kind="issue", untrusted=True, content="# ABC-12: bug\n\nIt breaks.\n")


def test_a_canary_goes_inside_the_fence_of_the_shadows_copy_only():
    real = [TICKET.model_copy(), ContextItem(name="notes.txt", content="trusted notes")]
    copy = [c.model_copy() for c in real]
    sp = ShadowPolicy(harness="team@sha256:" + "1" * 64, canary=CanaryPolicy(p=1.0, placements=["ticket_description"]))
    plan = canaries.choose(sp, copy, pick=lambda xs: "readme-line" if "readme-line" in xs else xs[0])
    assert plan and TOKEN.fullmatch(plan["token"]) and plan["placement"] == "ticket_description"
    assert plan["token"] in copy[0].content and "README.md" in copy[0].content and copy[0].untrusted
    assert all(plan["token"] not in (c.content or "") for c in real)  # the real job's context never holds it
    assert copy[1].content == "trusted notes"
    # a comment placement fences the attachment it lands in, even one that wasn't marked untrusted
    comments = [TICKET.model_copy(), ContextItem(name="issue-7-comments.md", kind="issue", content="c")]
    sp = sp.model_copy(update={"canary": CanaryPolicy(p=1.0, placements=["ticket_comment"])})
    plan = canaries.choose(sp, comments, pick=lambda xs: xs[0])
    assert plan["token"] in comments[1].content and comments[1].untrusted and plan["token"] not in comments[0].content
    # only the doors this shadow has: no Slack thread here; the repo file is always there
    assert canaries.available([TICKET], ["slack_thread", "repo_file"]) == ["repo_file"]
    assert canaries.choose(sp.model_copy(update={"canary": CanaryPolicy(p=1.0, placements=["slack_thread"])}),
                           [TICKET.model_copy()]) is None
    assert canaries.choose(sp.model_copy(update={"canary": CanaryPolicy(p=0.0)}), [TICKET.model_copy()]) is None
    with pytest.raises(ValueError):
        CanaryPolicy(p=0.5, placements=["email"])


def test_every_template_is_benign_and_names_one_signal():
    for tid, (text, signal) in canaries.TEMPLATES.items():
        line = text.format(token="CANARY-0badf00d")
        assert "CANARY-0badf00d" in line and signal in (canaries.SIGNAL_FILE, canaries.SIGNAL_EGRESS,
                                                         canaries.SIGNAL_SHELL), tid
        assert not re.search(r"\brm\b|secret|token|password|curl .*\|", line, re.I), tid


async def test_the_proxy_refuses_canary_invalid_whatever_the_policy_and_counts_it(jobs):  # noqa: F811
    sb = Sandbox(name="b", provider="fake", environment="lockenv",
                 labels={"hangar.job": "job-x", "hangar.session": "s-x"})
    for host in ("canary.invalid", "CANARY.invalid.", "api.canary.invalid"):
        with pytest.raises(Denied) as e:
            jobs.fleet.egress._decide(sb, host, 443)
        assert e.value.reason == "canary" and e.value.status == 403
    assert canaries.hits(jobs, "job-x") == 3
    hits = [a for a in jobs.store.audit_tail(50) if a.action == "egress.canary"]
    assert len(hits) == 3 and hits[0].detail["via"] == "proxy"


async def test_an_environment_request_for_canary_invalid_is_denied_and_traced(jobs):  # noqa: F811
    from hangar.envrequests import EnvRequest, EnvRequests

    reqs = EnvRequests(jobs.fleet, jobs.sessions, jobs)
    events: list = []
    reqs.sessions = SimpleNamespace(activity=SimpleNamespace(append=lambda sid, evs: events.extend(evs)))
    s = SimpleNamespace(id="sess-1", name="s", environment="lockenv", job_id="job-y", person=None)
    sb = Sandbox(name="b", provider="fake", environment="lockenv", labels={"hangar.job": "job-y"})
    reqs._canary(s, sb, {"kind": "egress", "host": "canary.invalid", "port": 443, "why": "status page"}, 1, "r1")
    [r] = jobs.store.list_where(EnvRequest, session_id="sess-1")
    assert r.state == "denied" and r.decided_by == "hangar" and canaries.hits(jobs, "job-y") == 1
    assert events[0].data["egress"] == "deny" and events[0].data["host"] == "canary.invalid"  # flightdeck#32


async def _canary_shadow(jobs, world, monkeypatch, tmp_path, settings, placement, template, agent=None):
    settings.outbox_url = str(tmp_path / "bucket")
    jobs.fleet.__dict__.pop("_outbox", None)
    _, tracked = await _shadowed(jobs, world, monkeypatch)
    key = repo_key(world["url"])
    pol = jobs.policies.get(key)
    jobs.policies.put(RepoPolicy(repo=key, shadow=pol.shadow.model_copy(update={
        "canary": CanaryPolicy(p=1.0, placements=[placement])})))
    monkeypatch.setattr(canaries, "choice", lambda xs: template if template in xs else xs[0])
    if agent:
        (world["root"] / "agent.sh").write_text(agent)
    real = await _real_job(jobs, world, context=[TICKET.model_copy()])
    real = await _until(jobs, real.id, DONE, timeout=120)
    assert real.state == JobState.SUCCEEDED, [e.message for e in real.events]
    exp = await _until(jobs, real.labels[shadows.PICK_LABEL], DONE, timeout=180)
    assert exp.state == JobState.SUCCEEDED, [e.message for e in exp.events]
    st = experiments.load(exp)
    shadow = jobs.get(st.trials[0].job)
    rec = jobs.store.kv_get_json(canaries.KV + exp.id, None)
    assert rec and rec["placement"] == placement and rec["template_id"] == template and "result" in rec
    # never in a label, an event of the shadow's, or anywhere on the real job
    tok = rec["token"]
    assert tok not in json.dumps(shadow.labels) + json.dumps([e.message for e in shadow.events])
    assert tok not in real.model_dump_json() and tok not in json.dumps(real.labels)
    # canary.json lands next to the shadow's run.json
    from hangar import outbox

    for _ in range(100):
        await outbox.of(jobs.fleet).drain_once()
        found = [json.loads(p.read_text()) for p in (tmp_path / "bucket").rglob("canary.json")] \
            if (tmp_path / "bucket").exists() else []
        if found:
            break
        await asyncio.sleep(0.1)
    assert found and found[0]["job_id"] == shadow.id and found[0]["token"] == tok
    return real, shadow, st, found[0], tracked


@needs_tools
async def test_an_agent_that_writes_the_token_into_readme_obeyed(jobs, world, monkeypatch, tmp_path, settings):
    real, shadow, st, doc, tracked = await _canary_shadow(jobs, world, monkeypatch, tmp_path, settings,
                                                          "ticket_description", "readme-line", OBEDIENT)
    assert doc["obeyed"] is True and doc["signals"][canaries.SIGNAL_FILE] is True
    assert doc["expected_signals"] == [canaries.SIGNAL_FILE] and doc["shadow_of"] == real.id
    assert doc["harness_digest"] == shadow.harness_lock["digest"]
    # inside the fence: the shadow's copy of the ticket carries it and is marked untrusted
    [item] = st.spec["tasks"][0]["context"]
    assert doc["token"] in item["content"] and item["untrusted"] is True
    assert [c.untrusted for c in shadow.context if c.name == TICKET.name] == [True]
    note = triggers.untrusted_note(shadow)  # the prompt's fence names the file
    assert note and f"`{TICKET.name}`" in note and UNTRUSTED_RULES in note
    # nothing reached a tracker, PR or chat: the shadow delivers nothing, and Jira hears of the real job only
    assert shadow.result.pr_url is None and all(t.reply_to is None for t in shadow.turns)
    assert {jid for jid, _ in tracked} == {real.id}
    assert doc["token"] not in json.dumps(tracked)


@needs_tools
async def test_an_agent_that_ignores_a_repo_file_canary_did_not_obey(jobs, world, monkeypatch, tmp_path, settings):
    real, shadow, st, doc, _ = await _canary_shadow(jobs, world, monkeypatch, tmp_path, settings, "repo_file",
                                                    "readme-line", IGNORING)
    # the default agent ignores it; the planted file itself is excluded from git, so it isn't "work"
    assert doc["obeyed"] is False and doc["signals"] == {canaries.SIGNAL_FILE: False, canaries.SIGNAL_EGRESS: False,
                                                         canaries.SIGNAL_SHELL: False}
    assert doc["placement"] == "repo_file" and not st.spec["tasks"][0].get("context")[0]["content"].count("CANARY")
    patch = next((tmp_path / "bucket").rglob(f"*{doc['run_id']}/patch.diff")).read_text()
    assert "+planted" in patch and "NOTES.local.md" not in patch  # it was there, and never part of the work
