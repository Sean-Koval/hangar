"""Server identity and stale tool lists (mcp_fresh): serverInfo.version names the tool surface, and a client calling
from an older tool list (the claude.ai apps never re-list) is told, in-band, to re-add the connector."""

from __future__ import annotations

import re

import mcp_types as types
import pytest
from mcp import Client

from hangar import mcp_fresh, mcp_live
from hangar.mcp_server import build_mcp, current_actor
from tests import test_exchange as xchg
from tests.test_mcp_modern import sc

world, svc, jobs = xchg.world, xchg.svc, xchg.jobs
pytestmark = pytest.mark.usefixtures("world")


def tool(name: str, desc: str = "d", args: tuple[str, ...] = ()) -> types.Tool:
    return types.Tool(name=name, description=desc,
                      input_schema={"type": "object", "properties": {a: {"type": "string"} for a in args}})


OLD = [tool("create_sandbox", args=("environment",)), tool("exec", args=("sandbox", "command"))]
NEW = [*OLD[:1], tool("exec", args=("sandbox", "command", "timeout")), tool("wait_for_turn"), tool("get_events")]


def fresh(store, tools, who: list[str]) -> mcp_fresh.Freshness:
    return mcp_fresh.Freshness(store, tools, mcp_fresh.RETIRED, lambda: who[0])


def test_version_is_the_package_and_a_hash_of_what_clients_cache():
    assert re.fullmatch(r"\d+\.\d+\.\d+\S*\+tools\.[0-9a-f]{12}", mcp_fresh.server_version(mcp_fresh.surface_hash(OLD)))
    assert mcp_fresh.surface_hash(OLD) == mcp_fresh.surface_hash([t.model_copy() for t in OLD])
    assert mcp_fresh.surface_hash(OLD) != mcp_fresh.surface_hash([OLD[0], tool("exec", "new words", ("sandbox",
                                                                                                   "command"))])


def test_a_caller_on_an_older_surface_is_told_once_an_hour(store):
    who = ["alice"]
    fresh(store, OLD, who).listed()  # alice lists, then hangar is upgraded
    f = fresh(store, NEW, who)
    note = f.note("create_sandbox")
    assert note and "2 tools, e.g. wait_for_turn, get_events" in note and "remove the hangar connector" in note
    assert f.note("create_sandbox") is None  # at most once an hour
    who[0] = "bob"
    f.listed()  # bob listed the current tools
    assert f.note("create_sandbox") is None


def test_new_arguments_on_the_called_tool_count_and_new_words_do_not(store):
    who = ["alice"]
    fresh(store, OLD, who).listed()
    f = fresh(store, [OLD[0], tool("exec", args=("sandbox", "command", "timeout"))], who)
    assert "exec has new arguments: timeout" in (f.note("exec") or "")
    who[0] = "carol"
    fresh(store, OLD, who).listed()
    reworded = fresh(store, [tool("create_sandbox", "better words", ("environment",)), OLD[1]], who)
    assert reworded.note("exec") is None


def test_a_caller_with_no_record_is_compared_with_the_first_surface_recorded(store):
    who = ["dave"]  # listed before hangar kept records, so its list is no newer than the first one recorded
    assert fresh(store, OLD, who).note("exec") is None
    assert "lacks 2 tools" in (fresh(store, NEW, who).note("exec") or "")


def test_retired_names_are_the_hidden_tools():
    assert set(mcp_fresh.RETIRED) == mcp_live.HIDDEN_TOOLS


async def test_over_mcp(fleet, svc, jobs, store):
    mcp = build_mcp(fleet, jobs, svc)
    async with Client(mcp) as c:
        assert c.server_info.version == mcp.fresh.version and "+tools." in c.server_info.version
        await c.list_tools()
        res = await c.call_tool("list_sandboxes", {})
        assert not [b for b in res.content if "connector" in b.text]  # a fresh list: nothing added
        # a tool this server doesn't have (a list from a server that did, or a guess)
        res = await c.call_tool("destroy_everything", {})
        assert res.is_error and "no tool named 'destroy_everything'" in res.content[0].text
        assert "remove the hangar connector and add it again" in res.content[0].text
        # an old name, only on lists from before it was merged: it works, and says the list is old
        res = await c.call_tool("validate_environment", {"spec": {"name": "x", "extends": "xenv"}})
        assert not res.is_error and "validate_environment is an old name for create_environment" \
            in res.content[-1].text
    # a client that listed an older surface and kept it
    old = mcp_fresh.surface_hash(OLD)
    store.kv_set_json(f"mcp:surface:{old}", {"first_seen": 0, "tools": {t.name: [] for t in OLD}})
    token = current_actor.set("claude-app")
    try:
        store.kv_set_json("mcp:listed:claude-app", {"hash": old, "at": 0})
        async with Client(build_mcp(fleet, jobs, svc)) as c:
            res = await c.call_tool("list_sandboxes", {})
            assert sc(res) == [] and "this client's list of them is older" in res.content[-1].text
            assert re.search(r"lacks \d+ tools, e.g. fleet_overview", res.content[-1].text)
            res = await c.call_tool("get_job", {"job": "job-00000000"})  # an error carries it too, once an hour
            assert "this client's list" not in res.content[0].text
    finally:
        current_actor.reset(token)
