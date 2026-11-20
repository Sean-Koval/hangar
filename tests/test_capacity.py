"""KAN-218: the capacity rehearsal's parts without a cluster.

The stub Messages API (scripts/bench/stub_model.py) answers as a short coding turn does. The rehearsal's environment
validates as an environment. The node and summary arithmetic is checked. And scripts/acceptance/capacity.py runs
end to end against a real hangar on the fake provider: boots, the per-person cap, turns, nothing left behind.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.test_acceptance_team import hangar_with  # noqa: F401 - fixture: a real hangar on the fake provider
from tests.test_exchange import world  # noqa: F401 - fixture: the scripted xenv environment

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "acceptance"))
try:
    import capacity as cap
    import hangar_accept as ha
finally:
    sys.path.pop(0)


def _stub_module():
    spec = importlib.util.spec_from_file_location("stub_model", ROOT / "scripts" / "bench" / "stub_model.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    mod.THINK = 0
    return mod


@pytest.fixture
def stub():
    mod = _stub_module()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), mod.Stub)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield httpx.Client(base_url=f"http://127.0.0.1:{srv.server_address[1]}", timeout=10)
    finally:
        srv.shutdown()


BASH = [{"name": "Bash", "input_schema": {"type": "object"}}, {"name": "Read", "input_schema": {"type": "object"}}]


def sse(text: str) -> list[tuple[str, dict[str, Any]]]:
    out = []
    for chunk in text.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in chunk.splitlines())
        out.append((lines["event"], json.loads(lines["data"])))
    return out


def test_the_stub_asks_for_the_work_then_ends_the_turn(stub: httpx.Client) -> None:
    first = stub.post("/v1/messages", json={"messages": [{"role": "user", "content": "go"}], "tools": BASH}).json()
    assert first["stop_reason"] == "tool_use"
    use = first["content"][0]
    assert use["type"] == "tool_use" and use["name"] == "Bash" and use["input"]["command"] == "sleep 20"
    after = {"messages": [{"role": "user", "content": "go"}, {"role": "assistant", "content": [use]},
                          {"role": "user", "content": [{"type": "tool_result", "tool_use_id": use["id"],
                                                        "content": "ok"}]}], "tools": BASH}
    done = stub.post("/v1/messages?beta=true", json=after).json()
    assert done["stop_reason"] == "end_turn" and done["content"] == [{"type": "text", "text": "DONE"}]
    assert done["usage"]["input_tokens"] > 0


def test_without_a_bash_tool_the_stub_just_answers(stub: httpx.Client) -> None:
    r = stub.post("/v1/messages", json={"messages": [{"role": "user", "content": "a title, please"}]}).json()
    assert r["stop_reason"] == "end_turn"


def test_the_stub_streams_the_events_the_cli_reads(stub: httpx.Client) -> None:
    r = stub.post("/v1/messages", json={"messages": [{"role": "user", "content": "go"}], "tools": BASH,
                                        "stream": True})
    assert r.headers["content-type"].startswith("text/event-stream")
    events = sse(r.text)
    names = [e for e, _ in events]
    assert names == ["message_start", "content_block_start", "content_block_delta", "content_block_stop",
                     "message_delta", "message_stop"]
    delta = events[2][1]["delta"]
    assert delta["type"] == "input_json_delta" and json.loads(delta["partial_json"])["command"] == "sleep 20"
    assert events[4][1]["delta"]["stop_reason"] == "tool_use"


def test_the_stub_answers_the_other_paths(stub: httpx.Client) -> None:
    assert stub.post("/v1/messages/count_tokens", json={"messages": []}).json() == {"input_tokens": 120}
    assert stub.get("/v1/anything").json() == {}
    assert stub.post("/v1/messages", content=b"not json").status_code == 400


def opts(*argv: str) -> argparse.Namespace:
    p = argparse.ArgumentParser()
    cap.add_args(p)
    return p.parse_args(["run", *argv])


def test_the_stub_environment_is_a_valid_environment() -> None:
    from hangar.envspec import EnvSpec

    o = opts("--think", "1.5", "--work", "uv run pytest -q", "--warm", "4")
    spec = EnvSpec(name="capacity-stub", **cap.stub_spec(o))
    assert spec.harnesses == ["claude-code"] and spec.warm_pool == 4
    assert spec.env["ANTHROPIC_BASE_URL"] == f"http://127.0.0.1:{cap.STUB_PORT}"
    (term,) = spec.terminals
    assert term.port == cap.STUB_PORT and term.env["STUB_WORK"] == "uv run pytest -q"
    assert term.env["STUB_THINK_SECONDS"] == "1.5"
    assert spec.files[cap.STUB_PATH] == cap.STUB.read_text()
    assert term.command.endswith(cap.STUB_PATH)


def test_node_counts_read_only_the_pool() -> None:
    def node(pool: str, ready: str) -> dict[str, Any]:
        return {"metadata": {"labels": {cap.POOL_LABEL: pool}},
                "status": {"conditions": [{"type": "Ready", "status": ready}]}}
    nodes = {"items": [node("sandboxes-gvisor", "True"), node("sandboxes-gvisor", "False"), node("system", "True")]}
    assert cap.node_counts(nodes, "sandboxes-gvisor") == (2, 1)


def test_scaling_finds_the_first_new_node_and_the_peak() -> None:
    samples = [{"t": 0, "nodes": 1, "ready": 1}, {"t": 15, "nodes": 1, "ready": 1}, {"t": 30, "nodes": 3, "ready": 1},
               {"t": 45, "nodes": 5, "ready": 4}, {"t": 60, "nodes": 5, "ready": 5}]
    got = cap.scaling(samples, 70.0)
    assert got == {"nodes_start": 1, "nodes_peak": 5, "ready_peak": 5, "first_new_node_s": 30, "peak_s": 45,
                   "all_idle_s": 70.0}
    assert cap.scaling([{"t": 0}], None) == {"nodes": "not measured (no kubectl)"}
    assert cap.scaling([{"t": 0, "nodes": 2, "ready": 2}], 5.0)["peak_s"] is None


def test_the_rehearsal_runs_end_to_end_on_the_fake_provider(tmp_path, hangar_with, monkeypatch) -> None:  # noqa: F811
    h, api = hangar_with.h, hangar_with.api
    h.settings.person_max_sessions, h.settings.person_max_running_jobs = 2, 1
    h.settings.github_repos = {hangar_with.world["url"]: "xenv"}
    state = ha.State(tmp_path / "state.json")
    o = opts("--env", "xenv", "--people", "2", "--sessions", "2", "--turns", "1", "--jobs", "1",
             "--job-repo", hangar_with.world["url"],
             "--no-kubectl", "--sample", "0.2", "--turn-timeout", "120", "--boot-timeout", "120")
    results: dict[str, Any] = {"url": api.url}
    failures = cap.run(api, state, o, results)
    assert results["boots"]["up"] == 4 and results["boots"]["failed"] == 0, results
    assert results["checks"]["session_cap"] == "pass", results["checks"]
    assert results["turns"]["ran"] == 4 and results["turns"]["failed"] == 0, results["turns"]
    assert results["checks"]["fairness"].startswith("pass"), results["checks"]
    assert failures == []
    assert results["nodes"] == {"nodes": "not measured (no kubectl)"}
    assert any("sandboxes" in s for s in results["samples"])
    assert cap.release(api, state, o) == []
    assert not [s for s in h.sessions.list() if s.name.startswith(f"accept-{state.run}")]
    results["passed"] = True
    assert "PASS" in cap.summary(results) and "| session_cap | pass |" in cap.summary(results)
