"""Observability (docs/observability.md): OpenTelemetry spans and their parent/child structure, TRACEPARENT into
agent runs, the harnesses' own OTel config, /metrics content and auth, the usage view, and nothing at all when
it is all off."""

from __future__ import annotations

import os
import subprocess
import sys
import time
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from hangar import metrics, observe, usage
from hangar.app import create_app
from hangar.harnesses import HARNESSES
from hangar.jobs import Caller, SubmitJob
from hangar.mcp_server import build_mcp
from hangar.models import Job, JobState, Session, SessionTurn, Turn, Usage
from tests import test_gateway_git as gwt
from tests import test_jobs as tj
from tests.conftest import FakeProvider
from tests.test_jobs import DONE, _until

gw, upstream = gwt.gw, gwt.upstream  # the gateway tests' upstream and gateway listener
world, sessions, jobs = tj.world, tj.sessions, tj.jobs  # the job tests' scripted agent and remote

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
async def otel(settings):
    exp = InMemorySpanExporter()
    await observe.start(settings, exporter=exp)
    yield exp
    await observe.stop()


def by_name(exp: InMemorySpanExporter) -> dict[str, list]:
    out: dict[str, list] = {}
    for sp in exp.get_finished_spans():
        out.setdefault(sp.name, []).append(sp)
    return out


def one(spans: dict[str, list], name: str):
    assert len(spans.get(name, [])) == 1, (name, sorted(spans))
    return spans[name][0]


def parent_of(sp) -> int | None:
    return sp.parent.span_id if sp.parent else None


def sample(name: str, **labels) -> float | None:
    return metrics.M.registry.get_sample_value(name, labels)


# ---------------------------------------------------------------------------- off: no imports, no work
def test_off_by_default_imports_nothing(tmp_path):
    """No OTEL_* and HANGAR_METRICS_ENABLED=false: a running app never imports the OTel SDK or prometheus_client."""
    code = """
import sys
from fastapi.testclient import TestClient
from hangar.app import create_app
from hangar.config import Settings
s = Settings(data_dir=sys.argv[1], env_dirs=[], enabled_providers=[], admin_token="t", metrics_enabled=False)
with TestClient(create_app(s)) as c:
    assert c.get("/healthz").status_code == 200
    print(c.get("/metrics", headers={"Authorization": "Bearer t"}).status_code)
    print(c.get("/api/usage", headers={"Authorization": "Bearer t"}).status_code)
# FastAPI itself imports the OpenTelemetry *API* when it is installed (its native telemetry, unused here)
print(sorted(m for m in sys.modules if m.startswith(("opentelemetry.sdk", "opentelemetry.exporter",
                                                     "prometheus_client"))))
"""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("OTEL_", "HANGAR_"))}
    out = subprocess.run([sys.executable, "-c", code, str(tmp_path)], env=env, cwd=REPO, capture_output=True,
                         text=True, timeout=120)
    assert out.returncode == 0, out.stderr[-2000:]
    assert out.stdout.split() == ["404", "200", "[]"]


async def test_hooks_do_nothing_when_off(fleet, monkeypatch):
    """The decorators' fast path: with tracing and metrics off they don't even bind arguments."""
    await observe.stop()
    assert not observe.enabled() and metrics.M is None

    def boom(*a):
        raise AssertionError("telemetry work while off")

    monkeypatch.setattr(observe, "_safe", boom)
    sb = await fleet.create("plain")
    assert (await fleet.exec(sb.id, "echo hi")).stdout.strip() == "hi"
    await fleet.destroy(sb.id)


def test_tracing_is_configured_by_the_standard_variables(monkeypatch):
    t = observe.tracing_exporter
    assert t({}) is None  # unlike the SDK default (otlp to localhost), nothing set means off
    assert t({"OTEL_EXPORTER_OTLP_ENDPOINT": "http://c:4318"}) == "otlp"
    assert t({"OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": "http://c:4318/v1/traces"}) == "otlp"
    assert t({"OTEL_TRACES_EXPORTER": "console"}) == "console"
    assert t({"OTEL_TRACES_EXPORTER": "none", "OTEL_EXPORTER_OTLP_ENDPOINT": "http://c:4318"}) is None
    assert t({"OTEL_SDK_DISABLED": "true", "OTEL_EXPORTER_OTLP_ENDPOINT": "http://c:4318"}) is None
    assert t({"OTEL_TRACES_EXPORTER": "zipkin"}) is None
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4318")
    exp = observe._span_exporter("otlp")
    assert type(exp).__module__.startswith("opentelemetry.exporter.otlp.proto.http")
    assert exp._endpoint == "http://collector:4318/v1/traces"  # the SDK appends the signal path
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_PROTOCOL", "http/json")
    assert observe._span_exporter("otlp") is None


# ---------------------------------------------------------------------------- spans
async def test_sandbox_lifecycle_spans(fleet, otel, tmp_path):
    fleet.catalog.add({"name": "pooled", "warm_pool": 1}, save_dir=tmp_path)
    fake = fleet.providers["docker"]
    claims = iter([True, False])
    real_create = fake.create

    async def create(sandbox, image, spec=None):
        await real_create(sandbox, image, spec)
        sandbox.info["warm"] = next(claims)  # what docker/kubernetes record for a pool claim

    fake.create = create
    with observe.span("test.root") as root:
        sb = await fleet.create("pooled")
        await fleet.exec(sb.id, "echo hi")
        await fleet.write_file(sb.id, "x.txt", b"abc")
        await fleet.destroy(sb.id)
    other = await fleet.create("pooled")
    await fleet.exec(other.id, "echo outside")  # exec and file I/O only trace inside another span
    spans = by_name(otel)
    create = spans["sandbox.create"][0]
    assert parent_of(create) == root.context.span_id
    assert create.attributes["hangar.sandbox.id"] == sb.id and create.attributes["hangar.sandbox.warm"] is True
    for child in ("sandbox.image", "sandbox.start", "sandbox.start_wait", "sandbox.setup"):
        assert parent_of(spans[child][0]) == create.context.span_id, child
    ex = one(spans, "sandbox.exec")
    assert parent_of(ex) == root.context.span_id and ex.attributes["hangar.exec.exit_code"] == 0
    assert one(spans, "sandbox.write_file").attributes["hangar.file.bytes"] == 3
    assert parent_of(one(spans, "sandbox.destroy")) == root.context.span_id
    assert len({s.context.trace_id for s in spans["sandbox.create"]}) == 2  # the second create is its own trace
    # metrics: create latency and warm-pool hits/misses, no sandbox ids
    assert sample("hangar_sandbox_create_seconds_count", provider="docker", environment="pooled", result="ok") == 2
    assert sample("hangar_warm_pool_claims_total", provider="docker", environment="pooled", result="hit") == 1
    assert sample("hangar_warm_pool_claims_total", provider="docker", environment="pooled", result="miss") == 1


async def test_failed_create_is_an_error_span(fleet, otel):
    with pytest.raises(KeyError):
        await fleet.create("nope")
    assert sample("hangar_sandbox_create_seconds_count", provider="docker", environment="nope", result="error") == 1
    sp = by_name(otel)["sandbox.create"][-1]
    assert sp.status.status_code.name == "ERROR" and sp.attributes["error.type"] == "KeyError"


async def test_job_phases_turn_span_and_traceparent(jobs, world, fleet, otel, monkeypatch, settings):
    """One trace per job run: job.process > provision > session.boot > sandbox.create; the agent turn is an
    invoke_agent span whose context the agent gets as TRACEPARENT; verify and deliver follow."""
    settings.otel_sandbox_endpoint = "http://otel-collector.observability.svc:4318"
    h = HARNESSES["scripted"]
    monkeypatch.setitem(HARNESSES, "scripted", h.model_copy(update={"run": (
        'echo "$TRACEPARENT $OTEL_RESOURCE_ATTRIBUTES" > "$HANGAR_JOB_DIR/../trace-$HANGAR_JOB_ID"; ' + h.run)}))
    job = jobs.submit(SubmitJob(instructions="Add fix.txt", repo=world["url"], environment="jobenv",
                                verify="test -f fix.txt"), Caller("alice", "alice"))
    job = await _until(jobs, job.id, DONE)
    assert job.state == JobState.SUCCEEDED, [e.message for e in job.events]
    spans = by_name(otel)
    proc = one(spans, "job.process")
    assert proc.parent is None and proc.attributes["hangar.job.id"] == job.id
    trace_id = proc.context.trace_id
    prov = one(spans, "job.provision")
    boot = one(spans, "session.boot")
    create = one(spans, "sandbox.create")
    assert parent_of(prov) == proc.context.span_id
    assert parent_of(boot) == prov.context.span_id and parent_of(create) == boot.context.span_id
    turn = one(spans, "invoke_agent scripted")
    assert parent_of(turn) == proc.context.span_id
    a = turn.attributes
    assert a["gen_ai.operation.name"] == "invoke_agent" and a["gen_ai.agent.name"] == "scripted"
    assert a["hangar.job.id"] == job.id and a["hangar.turn.outcome"] == "exited" and a["hangar.run.id"]
    assert turn.status.status_code.name == "UNSET"
    # the checkpoint after the turn is part of it; verify and deliver are the job's
    ck = [s for s in spans["checkpoint.capture"] if parent_of(s) == turn.context.span_id]
    assert ck and ck[0].attributes["hangar.checkpoint.stored"] is True
    verify = one(spans, "job.verify")
    assert parent_of(verify) == proc.context.span_id and verify.attributes["hangar.verify.exit_code"] == 0
    assert parent_of(one(spans, "job.deliver")) == proc.context.span_id
    assert {s.context.trace_id for s in otel.get_finished_spans() if s.name != "test.root"} == {trace_id}
    # the agent ran under the turn span
    box = fleet.providers["docker"].boxes[job.sandbox_id]
    tp, res = (box / f"home/agent/.hangar/jobs/trace-{job.id}").read_text().split()
    assert tp == f"00-{trace_id:032x}-{turn.context.span_id:016x}-01"
    assert f"hangar.run.id={a['hangar.run.id']}" in res and "hangar.harness=scripted" in res
    # metrics: the turn and the jobs gauge
    assert sample("hangar_turn_seconds_count", harness="scripted", outcome="exited") == 1


async def test_turn_usage_attributes_and_metrics(otel):
    """GenAI usage attributes (input includes cached tokens, per semconv), and tokens, cost and budget stops in
    /metrics with bounded labels."""
    s = Session(name="s", environment="env1", harness="claude-code", model="claude-sonnet-4-5")
    turn = SessionTurn(message="m", requester="alice", run_id="run-0000000a", harness="claude-code",
                       started_at=time.time() - 30, finished_at=time.time(),
                       usage=Usage(input_tokens=1000, output_tokens=200, cache_read_tokens=5000, cache_write_tokens=10,
                                   cost_usd=0.5, cost_source="harness", model="claude-sonnet-4-5"),
                       outcome="over_budget", outcome_reason="spent $0.50; the budget is $0.40")

    @observe.turn_end
    async def end_turn(self, s, turn):
        return "over_budget", ""

    await end_turn(None, s, turn)
    sp = one(by_name(otel), "invoke_agent claude-code")
    a = sp.attributes
    assert a["gen_ai.usage.input_tokens"] == 6010 and a["gen_ai.usage.output_tokens"] == 200
    assert a["gen_ai.usage.cache_read.input_tokens"] == 5000 and a["gen_ai.provider.name"] == "anthropic"
    assert a["hangar.cost_usd"] == 0.5 and a["error.type"] == "over_budget"
    assert sp.status.status_code.name == "ERROR" and sp.start_time < sp.end_time
    lbl = {"model": "claude-sonnet-4-5", "harness": "claude-code", "environment": "env1", "requester": "alice"}
    assert sample("hangar_tokens_total", kind="input", **lbl) == 1000
    assert sample("hangar_tokens_total", kind="cache_read", **lbl) == 5000
    assert sample("hangar_cost_usd_total", **lbl) == 0.5
    assert sample("hangar_budget_stops_total", harness="claude-code", environment="env1") == 1
    assert 25 < sample("hangar_turn_seconds_sum", harness="claude-code", outcome="over_budget") < 35
    # an unknown price is counted as such, never as $0 spent
    turn2 = turn.model_copy(update={"usage": Usage(input_tokens=5, model="my-vllm"), "requester": "bob"})
    await end_turn(None, s, turn2)
    assert sample("hangar_unpriced_turns_total", model="my-vllm", harness="claude-code") == 1
    # free-form labels are capped
    metrics.M.label.cap = len(metrics.M.label.seen["requester"])
    await end_turn(None, s, turn.model_copy(update={"requester": "carol"}))
    assert sample("hangar_cost_usd_total", **{**lbl, "requester": "other"}) == 0.5


async def test_mcp_tool_calls(fleet, otel):
    from mcp import Client

    async with Client(build_mcp(fleet)) as c:
        await c.call_tool("fleet_overview", {})
        res = await c.call_tool("exec", {"sandbox": "nope", "command": "true"})
        assert res.is_error
    spans = by_name(otel)
    ok, bad = one(spans, "tools/call fleet_overview"), one(spans, "tools/call exec")
    assert ok.kind.name == "SERVER" and ok.attributes["mcp.method.name"] == "tools/call"
    assert ok.attributes["gen_ai.tool.name"] == "fleet_overview" and "error.type" not in ok.attributes
    assert bad.attributes["error.type"] == "tool_error"
    assert sample("hangar_mcp_calls_total", tool="fleet_overview", status="ok") == 1
    assert sample("hangar_mcp_calls_total", tool="exec", status="tool_error") == 1


async def test_gateway_pushes_and_denials(fleet, gw, upstream, otel):
    sb = await fleet.create("gw", work_branch="hangar/job-1")
    res = await fleet.exec(sb.id, "git commit -q --allow-empty -m w && git push -q origin HEAD:refs/heads/hangar/job-1")
    assert res.exit_code == 0, res.stderr
    assert (await fleet.exec(sb.id, "git push -q origin HEAD:main")).exit_code != 0
    assert sample("hangar_gateway_pushes_total", result="ok") == 1
    assert sample("hangar_gateway_denials_total", reason="ref_policy") == 1
    names = {s.name for s in otel.get_finished_spans()}
    assert {"POST /git/git-receive-pack", "GET /git/info/refs"} <= names
    push = next(s for s in otel.get_finished_spans() if s.attributes.get("hangar.gateway.push") == "ok")
    assert push.attributes["hangar.sandbox.id"] == sb.id
    assert sample("hangar_http_request_seconds_count", method="POST", route="/git/git-receive-pack", status="200") == 2


# ---------------------------------------------------------------------------- the app: REST spans, /metrics, /api/usage
@pytest.fixture
def app_client(settings, monkeypatch):
    exp = InMemorySpanExporter()
    monkeypatch.setenv("OTEL_TRACES_EXPORTER", "otlp")
    monkeypatch.setattr(observe, "_span_exporter", lambda kind: exp)
    settings.admin_token = "t0ken"
    app = create_app(settings)
    with TestClient(app) as c:
        app.state.hangar.fleet.providers["docker"] = FakeProvider()
        app.state.hangar.fleet.provider_errors.pop("docker", None)
        c.headers["Authorization"] = "Bearer t0ken"
        c.exp = exp
        c.hangar = app.state.hangar
        yield c


def test_rest_spans_are_named_by_route_and_join_the_callers_trace(app_client):
    c = app_client
    tid, sid = "4bf92f3577b34da6a3ce929d0e0e4736", "00f067aa0ba902b7"
    r = c.get("/api/sandboxes/sb-12345678", headers={"traceparent": f"00-{tid}-{sid}-01"})
    assert r.status_code == 404
    c.get("/metrics")
    c.get("/healthz")
    observe._provider.force_flush()
    spans = c.exp.get_finished_spans()
    sp = next(s for s in spans if s.name == "GET /api/sandboxes/{ref}")
    assert f"{sp.context.trace_id:032x}" == tid and f"{sp.parent.span_id:016x}" == sid
    assert sp.kind.name == "SERVER" and sp.attributes["http.response.status_code"] == 404
    assert not any("sb-12345678" in s.name for s in spans)
    assert not any(s.name in ("GET /metrics", "GET /healthz") for s in spans)  # scrapes and probes: no spans


def test_mcp_over_http_joins_the_clients_trace(app_client):
    c = app_client
    h = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    r = c.post("/mcp", headers=h, json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}})
    h |= {"mcp-session-id": r.headers["mcp-session-id"], "mcp-protocol-version": "2025-06-18"}
    c.post("/mcp", headers=h, json={"jsonrpc": "2.0", "method": "notifications/initialized"})
    tid, sid = "4bf92f3577b34da6a3ce929d0e0e4736", "00f067aa0ba902b7"
    r = c.post("/mcp", headers={**h, "traceparent": f"00-{tid}-{sid}-01"}, json={
        "jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "fleet_overview", "arguments": {}}})
    assert r.status_code == 200
    observe._provider.force_flush()
    spans = c.exp.get_finished_spans()
    tool = next(s for s in spans if s.name == "tools/call fleet_overview")
    http = next(s for s in spans if s.context.span_id == tool.parent.span_id)
    assert http.name == "POST /mcp" and f"{http.parent.span_id:016x}" == sid and f"{tool.context.trace_id:032x}" == tid


def test_metrics_endpoint_auth_and_content(app_client):
    c = app_client
    scrape = c.post("/api/tokens", json={"name": "prom", "scopes": ["metrics"]}).json()["token"]
    svc = c.post("/api/tokens", json={"name": "svc", "scopes": ["jobs"]}).json()["token"]
    sb = c.post("/api/sandboxes", json={"environment": "plain"}).json()
    sb = sb[0] if isinstance(sb, list) else sb
    assert c.get("/metrics", headers={"Authorization": ""}).status_code == 401
    assert c.get("/metrics", headers={"Authorization": f"Bearer {svc}"}).status_code == 403
    assert c.get("/api/sandboxes", headers={"Authorization": f"Bearer {scrape}"}).status_code == 403
    r = c.get("/metrics", headers={"Authorization": f"Bearer {scrape}"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain")
    body = r.text
    assert 'hangar_sandboxes{provider="docker",state="running"} 1.0' in body
    for name in ("hangar_sandbox_create_seconds_bucket", "hangar_event_loop_lag_seconds_bucket",
                 "hangar_io_pool_queue_depth", "hangar_io_pool_max_threads", 'route="/api/sandboxes"'):
        assert name in body, name
    assert sb["id"] not in body and "sb-" not in body  # no ids as labels
    assert c.get("/metrics").status_code == 200  # admin too


def test_metrics_can_be_turned_off(settings):
    settings.admin_token = "t"
    settings.metrics_enabled = False
    with TestClient(create_app(settings)) as c:
        assert c.get("/metrics", headers={"Authorization": "Bearer t"}).status_code == 404


def test_usage_route(app_client):
    c = app_client
    h = c.hangar
    s = Session(name="u1", environment="plain", repo="https://github.com/acme/widgets.git")
    s.turns = [SessionTurn(message="m", requester="alice", run_id="run-00000001", started_at=time.time(),
                           usage=Usage(input_tokens=10, output_tokens=5, cost_usd=0.25, model="m1"))]
    h.sessions._sessions[s.id] = s
    r = c.get("/api/usage", params={"group_by": "requester", "since": "1d"})
    assert r.status_code == 200
    d = r.json()
    assert d["rows"][0]["key"] == "alice" and d["total"]["cost_usd"] == 0.25 and d["total"]["tokens"] == 15
    assert c.get("/api/usage", params={"group_by": "job_id"}).status_code == 400
    assert c.get("/api/usage", params={"since": "last tuesday"}).status_code == 400
    assert c.get("/api/usage", headers={"Authorization": ""}).status_code == 401


# ---------------------------------------------------------------------------- usage aggregation
DAY = 86400
T0 = 1_791_000_000.0  # 2026-10-03 (UTC)


def _turn(ts: float, cost: float | None = None, tokens: int = 100, **kw) -> SessionTurn:
    u = Usage(input_tokens=tokens, output_tokens=0, cost_usd=cost, model=kw.pop("model", "m1"))
    return SessionTurn(message="m", run_id=kw.pop("run_id", "run-00000001"), started_at=ts, usage=u, **kw)


def _world():
    a = Session(name="a", environment="e1", repo="https://github.com/acme/widgets.git", model="m1")
    a.turns = [_turn(T0, 1.0, requester="alice"),
               _turn(T0 + DAY, None, requester="alice", model="m2"),  # a model without a price
               _turn(T0 + DAY, 0.5, requester="alice", outcome="over_budget", outcome_reason="spent $0.50",
                     repair_usage=Usage(input_tokens=50, cost_usd=0.25)),
               _turn(T0 - 40 * DAY, 9.0, requester="alice")]  # before `since`
    fork = Session(name="a-fork", environment="e1", repo=a.repo, parent_id=a.id)
    fork.turns = [t.model_copy(update={"run_id": None, "usage": None, "repair_usage": None}) for t in a.turns[:3]]
    fork.turns.append(_turn(T0 + DAY, 2.0, requester="alice"))
    j1 = Job(title="j1", instructions="x", environment="e2", requester="bob", repo="https://git.example/x.git")
    j1.turns = [Turn(run_id="run-00000009", started_at=T0, usage=Usage(input_tokens=999, cost_usd=99.0))]
    b = Session(name="job-1", environment="e2", job_id=j1.id, repo=j1.repo, owner="jobs")
    b.turns = [_turn(T0, 3.0)]  # no requester on the turn: the job's
    legacy = Job(title="old", instructions="x", environment="e3", requester="carol",
                 repo="https://tok:secret@git.example/y.git")
    legacy.turns = [Turn(run_id="run-0000000b", started_at=T0, usage=Usage(input_tokens=7, cost_usd=0.07))]
    return (SimpleNamespace(_sessions={x.id: x for x in (a, fork, b)}),
            SimpleNamespace(_jobs={j.id: j for j in (j1, legacy)}))


def test_usage_aggregation():
    ses, jbs = _world()
    since = T0 - DAY
    day = usage.aggregate(ses, jbs, "day", since)
    assert [r["key"] for r in day["rows"]] == ["2026-10-03", "2026-10-04"]
    d1, d2 = day["rows"]
    # day 1: a's first turn, the job session's turn (not the job's mirror of it), the legacy job's turn
    assert d1["turns"] == 3 and d1["cost_usd"] == pytest.approx(1.0 + 3.0 + 0.07)
    # day 2: unpriced turn, the budget stop with its repair run, the fork's own turn (not its copied history)
    assert d2["turns"] == 3 and d2["cost_usd"] == pytest.approx(0.5 + 0.25 + 2.0)
    assert d2["unpriced_turns"] == 1 and d2["budget_stops"] == 1 and d2["input_tokens"] == 100 * 3 + 50
    t = day["total"]
    assert t["turns"] == 6 and t["cost_usd"] == pytest.approx(6.82) and t["budget_stops"] == 1
    assert day["budget_stops"][0]["reason"] == "spent $0.50" and day["budget_stops"][0]["repo"] == "acme/widgets"
    req = {r["key"]: r for r in usage.aggregate(ses, jbs, "requester", since)["rows"]}
    assert set(req) == {"alice", "bob", "carol"} and req["bob"]["cost_usd"] == 3.0
    assert list(req) == ["alice", "bob", "carol"]  # most expensive first
    repos = {r["key"] for r in usage.aggregate(ses, jbs, "repo", since)["rows"]}
    assert repos == {"acme/widgets", "https://git.example/x", "https://git.example/y"}  # never userinfo
    models = {r["key"]: r["turns"] for r in usage.aggregate(ses, jbs, "model", since)["rows"]}
    assert models["m2"] == 1
    everything = usage.aggregate(ses, jbs, "day", 0)
    assert everything["total"]["turns"] == 7
    with pytest.raises(ValueError):
        usage.aggregate(ses, jbs, "sandbox", since)


def test_parse_since():
    now = 2_000_000.0
    assert usage.parse_since("7d", now) == now - 7 * DAY
    assert usage.parse_since("12h", now) == now - 12 * 3600
    assert usage.parse_since("", now) == now - 30 * DAY
    assert usage.parse_since("1700000000", now) == 1_700_000_000
    assert usage.parse_since("2026-10-01", now) == 1_790_812_800
    with pytest.raises(ValueError):
        usage.parse_since("yesterday", now)


# ---------------------------------------------------------------------------- the harnesses' own telemetry
def test_run_env_for_agents(settings):
    sb = SimpleNamespace(id="sb-1", environment="py env")
    assert observe.run_env(settings, sb, "run-1", "claude-code") == {}  # no tracing, no sandbox endpoint
    settings.otel_sandbox_endpoint = "http://otel-collector.observability.svc:4318/"
    env = observe.run_env(settings, sb, "run-1", "claude-code")
    assert env["CLAUDE_CODE_ENABLE_TELEMETRY"] == "1" and env["CLAUDE_CODE_ENHANCED_TELEMETRY_BETA"] == "1"
    assert env["OTEL_EXPORTER_OTLP_ENDPOINT"] == "http://otel-collector.observability.svc:4318"
    assert env["OTEL_EXPORTER_OTLP_PROTOCOL"] == "http/protobuf" and env["OTEL_TRACES_EXPORTER"] == "otlp"
    assert "hangar.environment=py%20env" in env["OTEL_RESOURCE_ATTRIBUTES"]
    assert "OTEL_EXPORTER_OTLP_HEADERS" not in env  # no credentials go into sandboxes
    codex = observe.run_env(settings, sb, "run-1", "codex")
    assert set(codex) == {"OTEL_RESOURCE_ATTRIBUTES"}  # codex reads its exporters from config.toml
    for bad in ('http://x"y:4318', "ftp://x", "http://user:pw@x:4318", "x:4318", "http://x/$(id)"):
        settings.otel_sandbox_endpoint = bad
        assert observe.sandbox_endpoint(settings) is None, bad


class _Files:
    def __init__(self, files: dict[str, bytes]):
        self.files = files

    async def read_file(self, sb, path, limit=None):
        return self.files.get(path)

    async def write_file(self, sb, path, data, mode=0o644):
        self.files[path] = data


async def test_codex_otel_config(settings):
    settings.otel_sandbox_endpoint = "http://collector:4318"
    path = "/home/agent/.codex/config.toml"
    mcp = '# >>> hangar mcp >>>\n[mcp_servers.github]\nurl = "https://api.example/mcp"\n# <<< hangar mcp <<<\n'
    prov = _Files({path: ('model = "gpt-5.5"\n\n' + mcp).encode()})
    sb = SimpleNamespace(info={})
    for _ in range(2):  # idempotent: one managed block
        await observe.configure_harness(prov, sb, "codex", "/home/agent", settings)
    text = prov.files[path].decode()
    assert text.count("[otel]") == 1 and text.startswith('model = "gpt-5.5"')
    cfg = tomllib.loads(text)
    assert cfg["model"] == "gpt-5.5" and cfg["mcp_servers"]["github"]["url"] == "https://api.example/mcp"
    o = cfg["otel"]
    assert o["trace_exporter"]["otlp-http"] == {"endpoint": "http://collector:4318/v1/traces", "protocol": "binary"}
    assert o["exporter"]["otlp-http"]["endpoint"] == "http://collector:4318/v1/logs"
    assert o["metrics_exporter"]["otlp-http"]["endpoint"] == "http://collector:4318/v1/metrics"
    # the environment's own [otel] wins; hangar says why it did nothing
    own = _Files({path: b'[otel]\nexporter = "none"\n'})
    await observe.configure_harness(own, sb, "codex", "/home/agent", settings)
    assert own.files[path] == b'[otel]\nexporter = "none"\n' and "already has an [otel]" in sb.info["warnings"][0]
    other = _Files({})
    await observe.configure_harness(other, sb, "claude-code", "/home/agent", settings)
    assert other.files == {}
