"""REST/MCP scope parity (#122): every tool and route is in tests/fixtures/scope_matrix.yaml with the scope and owner
rule it enforces, and one verb's MCP and REST twins agree unless the matrix records why not.

#121 existed because `approve_job` (any MCP token) and `POST /api/jobs/{id}/approve` (admin tokens) enforced different
rules and nothing noticed. This test is what notices:

* (a) a tool, resource or route missing from the matrix, or enforcing another scope or owner rule than it says, fails,
  with a ready-to-paste YAML stub;
* (b) a verb whose MCP and REST entries differ in scope or owner rule fails, unless it records `exception: <reason>`;
* (c) every privileged verb (scope `admin` or `approvals.*`) with no exception is called on both surfaces as a member,
  an admin's `mcp`-only token and an admin login token, and must answer the same (ok | forbidden | not_found).

Scopes are read, not written down twice: a tool's from `__hangar_scope__` (mcp_server.tool()), a route's from its
FastAPI dependencies (`DEPENDENCY_SCOPES`). A route that checks its caller in its handler has no dependency to read,
so its entry says where (`enforced_by:`).
"""

from __future__ import annotations

import ast
import asyncio
import re
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.routing import APIRoute, APIWebSocketRoute
from fastapi.testclient import TestClient
from starlette.routing import Mount, Route

from hangar.mcp_live import HIDDEN_TOOLS

MATRIX = Path(__file__).parent / "fixtures" / "scope_matrix.yaml"
SRC = Path(__file__).parents[1] / "src" / "hangar"

# A route's FastAPI dependency (app.create_app's locals) -> (scope, owner rule). Owner rules: `admin` (admin tokens,
# every record), `guard` (a person's own records unless they're an admin: people.Guard / people.mine),
# `token` (a `jobs` token's own jobs unless admin: job_caller's `restricted`).
DEPENDENCY_SCOPES = {
    "actor": ("admin", "admin"),
    "person_actor": ("user", "guard"),
    "job_caller": ("jobs", "token"),
    "admin_job_caller": ("admin", "admin"),
    "approver": ("approvals.can_decide", "guard"),
}
SCOPES = {"admin", "user", "jobs", "mcp", "sandboxes", "metrics", "approvals.can_decide", "signed", "ticket",
          "grant", "link", "oauth", "public"}
OWNERS = {"admin", "guard", "token", "any", "own_grant"}
PRIVILEGED = re.compile(r"^(admin|approvals\..+)$")


# ====================================================================== introspection
def build_app(settings) -> Any:
    """The app with every optional router mounted: OAuth, A2A, E2B, the git gateway on the main port (and with it
    the GitHub proxy, package mirrors, the registry mirror and the build caches), previews, metrics."""
    from hangar.app import create_app

    settings.admin_token = "t0ken"
    settings.public_url = "http://localhost:8787"
    settings.gateway_url, settings.gateway_port = "http://localhost:8787", 0
    settings.heavy_mirror, settings.preview_domain = True, "preview.example.com"
    return create_app(settings)


def tools(server) -> dict[str, tuple[str | None, str | None]]:
    """Every registered tool (the hidden ones too): name -> (scope, owner rule)."""
    out = {}
    for t in server._tool_manager.list_tools():
        fn = t.fn
        out[t.name] = (getattr(fn, "__hangar_scope__", None),
                       "guard" if getattr(fn, "__hangar_guarded__", False) else None)
    return out


def resources(server) -> set[str]:
    rm = server._resource_manager
    return {str(r.uri) for r in rm.list_resources()} | {t.uri_template for t in rm.list_templates()}


def _deps(dependant, out: list[str]) -> list[str]:
    for sub in dependant.dependencies:
        out.append(getattr(sub.call, "__name__", ""))
        _deps(sub, out)
    return out


def routes(app) -> dict[str, tuple[str | None, str | None, str]]:
    """Every route as "METHOD path" -> (scope, owner rule, defining module); scope None when the handler checks the
    caller itself. Mounted apps are walked when they are FastAPI apps (the E2B API); /mcp, /ui are their own."""
    out: dict[str, tuple[str | None, str | None, str]] = {}

    def walk(rs, prefix: str) -> None:
        for r in rs:
            if isinstance(r, (APIRoute, APIWebSocketRoute)):
                found = [DEPENDENCY_SCOPES[d] for d in _deps(r.dependant, []) if d in DEPENDENCY_SCOPES]
                scope, owner = found[0] if found else (None, None)
                methods = sorted((getattr(r, "methods", None) or {"WS"}) - {"HEAD"}) if isinstance(r, APIRoute) \
                    else ["WS"]
                for m in methods:
                    out[f"{m} {prefix}{r.path}"] = (scope, owner, r.endpoint.__module__)
            elif isinstance(r, Mount):
                if r.path in ("/mcp", "/ui"):
                    out[f"MOUNT {r.path}"] = (None, None, "hangar.app")
                elif hasattr(r.app, "routes"):
                    walk(r.app.routes, prefix + r.path)
            elif isinstance(r, Route):
                for m in sorted((r.methods or {"GET"}) - {"HEAD", "OPTIONS"}):
                    out[f"{m} {prefix}{r.path}"] = (None, None, getattr(r.endpoint, "__module__", "?"))
    walk(app.routes, "")
    return out


def load_matrix() -> dict[str, dict[str, Any]]:
    raw = yaml.safe_load(MATRIX.read_text())
    return {k: v for k, v in raw.items() if not k.startswith("_")}


def _side(v: Any, side: str) -> Any:
    """`scope`/`owner` are one value for both surfaces, or {mcp: ..., rest: ...} when they differ."""
    return v.get(side) if isinstance(v, dict) else v


def _list(v: Any) -> list[str]:
    return [] if v is None else [v] if isinstance(v, str) else list(v)


def stub(verb: str, mcp: str | None = None, rest: str | None = None, scope: str | None = None,
         owner: str | None = None) -> str:
    lines = [f"{verb}:", "  server: hangar", f"  mcp: {mcp or 'null'}"]
    lines.append(f'  rest: "{rest}"' if rest else "  rest: null")
    lines += [f"  scope: {scope or '<admin | user | jobs | mcp | approvals.can_decide | ...>'}",
              f"  owner: {owner or '<guard | admin | token | any>'}"]
    if scope is None:
        lines.append("  enforced_by: <where the handler checks its caller>")
    return "\n".join(lines)


# ====================================================================== fixtures
@pytest.fixture(scope="module")
def world(tmp_path_factory):
    from hangar.config import Settings
    from tests.conftest import ENVS

    d = tmp_path_factory.mktemp("parity")
    s = Settings(data_dir=d, env_dirs=[ENVS], default_provider="docker", enabled_providers=[],
                 reaper_interval_seconds=3600)
    s.snapshot_dir.mkdir(parents=True, exist_ok=True)
    app = build_app(s)
    server = app.state.mcp
    return {"app": app, "tools": tools(server), "resources": resources(server), "routes": routes(app),
            "matrix": load_matrix(), "settings": s}


# ====================================================================== (a) the matrix covers what exists
def test_the_matrix_is_well_formed_and_sorted(world):
    m = world["matrix"]
    assert list(m) == sorted(m), "keep tests/fixtures/scope_matrix.yaml sorted by verb"
    seen: dict[str, str] = {}
    for verb, e in m.items():
        assert e.get("server") in ("hangar", "hangar-sandbox"), f"{verb}: server is hangar | hangar-sandbox (#140)"
        for side in ("mcp", "rest"):
            s, o = _side(e.get("scope"), side), _side(e.get("owner"), side)
            if e.get(side) or (side == "mcp" and e.get("mcp_resource")):
                assert s in SCOPES, f"{verb}: {side} scope {s!r} isn't one of {sorted(SCOPES)}"
                assert o in OWNERS, f"{verb}: {side} owner {o!r} isn't one of {sorted(OWNERS)}"
        for key in ("mcp", "rest", "mcp_resource"):
            for x in _list(e.get(key)):
                assert (key, x) not in seen, f"{key} {x} is in both {seen.get((key, x))} and {verb}"  # type: ignore
                seen[(key, x)] = verb  # type: ignore[index]


def test_every_tool_is_in_the_matrix_with_the_scope_it_enforces(world):
    m = world["matrix"]
    by_tool = {t: (v, e) for v, e in m.items() for t in _list(e.get("mcp"))}
    problems = []
    for name, (scope, owner) in sorted(world["tools"].items()):
        if name not in by_tool:
            problems.append(f"tool {name} isn't in the matrix. Add, in sorted order:\n"
                            + stub(f"<noun>.{name}", mcp=name, scope=scope, owner=owner))
            continue
        verb, e = by_tool[name]
        if scope is None:
            problems.append(f"tool {name} isn't registered through mcp_server.tool(), so its scope is unknown")
        elif _side(e["scope"], "mcp") != scope:
            problems.append(f"{verb}: tool {name} enforces scope {scope}, the matrix says {_side(e['scope'], 'mcp')}")
        if _side(e["owner"], "mcp") != owner:
            problems.append(f"{verb}: tool {name} owner rule is {owner}, the matrix says {_side(e['owner'], 'mcp')}")
    gone = sorted(set(by_tool) - set(world["tools"]))
    problems += [f"{by_tool[t][0]}: tool {t} no longer exists; remove it from the matrix" for t in gone]
    assert not problems, "\n\n".join(problems)


def test_every_resource_is_in_the_matrix(world):
    listed = {r for e in world["matrix"].values() for r in _list(e.get("mcp_resource"))}
    missing = sorted(world["resources"] - listed)
    assert not missing, "resources missing from tests/fixtures/scope_matrix.yaml (`mcp_resource:`):\n" + \
        "\n\n".join(stub(f"<noun>.{u.split('://')[1].split('/')[0]}", scope="mcp", owner="guard")
                    .replace("  mcp: null", f'  mcp: null\n  mcp_resource: "{u}"') for u in missing)
    assert not listed - world["resources"], f"no longer registered: {sorted(listed - world['resources'])}"


def test_every_route_is_in_the_matrix_with_the_scope_it_enforces(world):
    m = world["matrix"]
    by_route = {r: (v, e) for v, e in m.items() for r in _list(e.get("rest"))}
    problems = []
    for key, (scope, owner, module) in sorted(world["routes"].items()):
        if key not in by_route:
            problems.append(f"route {key} ({module}) isn't in the matrix. Add, in sorted order:\n"
                            + stub("<noun>.<verb>", rest=key, scope=scope, owner=owner))
            continue
        verb, e = by_route[key]
        if scope is None:
            if not e.get("enforced_by"):
                problems.append(f"{verb}: {key} has no auth dependency; say where it checks its caller "
                                "(`enforced_by:`)")
            continue
        if _side(e["scope"], "rest") != scope:
            problems.append(f"{verb}: {key} enforces scope {scope}, the matrix says {_side(e['scope'], 'rest')}")
        if _side(e["owner"], "rest") != owner:
            problems.append(f"{verb}: {key} owner rule is {owner}, the matrix says {_side(e['owner'], 'rest')}")
    gone = sorted(set(by_route) - set(world["routes"]))
    problems += [f"{by_route[r][0]}: route {r} no longer exists; remove it from the matrix" for r in gone]
    assert not problems, "\n\n".join(problems)


def test_tools_are_registered_only_through_tool():
    """mcp_server.tool() stamps the scope and wraps people.Guard; a module calling `mcp.tool(` itself would skip
    both. (Reads every module's source, so CI runs this file for any hangar change.)"""
    calls = []
    for p in sorted(SRC.rglob("*.py")):
        for node in ast.walk(ast.parse(p.read_text(), str(p))):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "tool" \
                    and isinstance(node.func.value, ast.Name) and node.func.value.id == "mcp":
                calls.append(p.relative_to(SRC).as_posix())
    assert calls == ["mcp_server.py"], f"register tools through mcp_server's tool(), not mcp.tool(): {calls}"


# ====================================================================== (b) one verb, one rule
def test_mcp_and_rest_twins_agree_or_say_why(world):
    problems = []
    for verb, e in world["matrix"].items():
        mcp = e.get("mcp") or e.get("mcp_resource")  # a resource is MCP's read path for the verb
        if not (mcp and e.get("rest")):
            if e.get("exception"):
                problems.append(f"{verb}: an exception needs both an MCP and a REST side")
            continue
        differ = [k for k in ("scope", "owner") if _side(e[k], "mcp") != _side(e[k], "rest")]
        if differ and not e.get("exception"):
            problems.append(f"{verb}: MCP {mcp} and REST {e['rest']} differ in {' and '.join(differ)} "
                            f"({ {k: e[k] for k in differ} }); make them agree, or record `exception: <reason>`")
        if not differ and e.get("exception"):
            problems.append(f"{verb}: the surfaces agree now; remove its exception")
    assert not problems, "\n".join(problems)


# ====================================================================== (c) privileged verbs behave the same
def _outcome(status: int | None, text: str) -> str:
    if status is None:  # MCP
        return "not_found" if re.match(r"^(Error executing tool \w+: )?no \w+ '", text) else \
            "error" if "not waiting" in text else "forbidden"
    return {200: "ok", 201: "ok", 403: "forbidden", 404: "not_found"}.get(status, f"http {status}")


# How to call each privileged verb on each surface: the tool's arguments for a held job, or a resource to read, and
# the REST method and path.
CALLS: dict[str, dict[str, Any]] = {
    "job.approve": {"args": lambda ref: {"job": ref}, "rest": ("POST", "/api/jobs/{ref}/approve")},
    "job.reject": {"args": lambda ref: {"job": ref}, "rest": ("POST", "/api/jobs/{ref}/reject")},
    "status.get": {"resource": "hangar://status", "rest": ("GET", "/api/status")},
}


def test_every_privileged_verb_without_an_exception_has_a_behaviour_check(world):
    need = sorted(v for v, e in world["matrix"].items() if (e.get("mcp") or e.get("mcp_resource")) and e.get("rest")
                  and not e.get("exception") and PRIVILEGED.match(str(_side(e["scope"], "rest"))))
    assert need == sorted(CALLS), f"add these privileged verbs to CALLS in {Path(__file__).name}: " \
                                  f"{sorted(set(need) - set(CALLS))}"


@pytest.fixture(scope="module")
def client(world):
    from hangar import people
    from tests.conftest import FakeProvider

    with TestClient(world["app"], base_url="http://localhost:8787") as c:
        h = world["app"].state.hangar
        h.fleet.providers["docker"] = FakeProvider()
        h.jobs._queue = asyncio.Queue()  # approved jobs stay queued: this is about who may, not what runs
        admin = {"Authorization": "Bearer t0ken"}
        member = c.post("/api/people", json={"id": "mo", "role": "member"}, headers=admin).json()["login"]["token"]
        c.post("/api/people", json={"id": "bea", "role": "member", "token": False}, headers=admin)
        connect = c.post("/api/tokens", json={"name": "cc", "scopes": ["mcp"]}, headers=admin).json()["token"]
        assert people.get(h.store, "mo") is not None
        yield {"c": c, "h": h, "tokens": {"member": member, "admin_mcp_only": connect, "admin": "t0ken"}}


def _held(h) -> str:
    """Bea's job, held by her daily cap: someone else's to every caller here."""
    from hangar.jobs import Caller, SubmitJob
    from hangar.triggers import new_approval

    return h.jobs.submit(SubmitJob(instructions="x", environment="plain", deliver="none"),
                         Caller("@bea:login", "tok-bea", person="bea"), source="mcp",
                         approval=new_approval(h.jobs, "@bea:login", "budget: daily cap is spent")).id


def _mcp_call(c, token: str, tool: str, args: dict) -> tuple[str, str]:
    from tests.test_identity import _call, _mcp_session

    res = _call(c, _mcp_session(c, token), tool, args)
    text = res["content"][0]["text"]
    return ("ok", text) if not res.get("isError") else (_outcome(None, text), text)


def _mcp_read(c, token: str, uri: str) -> tuple[str, str]:
    from tests.test_identity import _mcp_session

    r = c.post("/mcp", headers=_mcp_session(c, token), json={"jsonrpc": "2.0", "id": 3, "method": "resources/read",
                                                             "params": {"uri": uri}})
    body = r.text
    if "text/event-stream" in r.headers.get("content-type", ""):
        body = next(ln[5:] for ln in body.splitlines() if ln.startswith("data:"))
    import json

    out = json.loads(body)
    if "error" in out:
        msg = out["error"].get("message", "")
        return ("not_found" if "no " in msg and "'" in msg else "forbidden"), msg
    return "ok", out["result"]["contents"][0].get("text", "")


@pytest.mark.parametrize("who", ["member", "admin_mcp_only", "admin"])
@pytest.mark.parametrize("verb", sorted(CALLS))
def test_privileged_verbs_answer_the_same_on_both_surfaces(world, client, verb, who):
    e, call = world["matrix"][verb], CALLS[verb]
    c, h, token = client["c"], client["h"], client["tokens"][who]
    a, b = (_held(h), _held(h)) if "args" in call else ("", "")
    method, path = call["rest"]
    r = c.request(method, path.format(ref=a), headers={"Authorization": f"Bearer {token}"})
    rest = _outcome(r.status_code, r.text)
    mcp, text = _mcp_call(c, token, e["mcp"], call["args"](b)) if "args" in call else \
        _mcp_read(c, token, call["resource"])
    assert rest == mcp, f"{verb} as {who}: REST {rest} ({r.text[:200]}), MCP {mcp} ({text[:200]})"
    if rest != "ok" and "args" in call:  # the same refusal text (#121); resources answer in JSON-RPC's own words
        msg = r.json().get("error", "")
        assert msg.replace(a, "J") in text.replace(b, "J"), (msg, text)


def test_the_matrix_covers_the_visible_and_hidden_tools(world):
    """Acceptance (#122): the 54 visible tools and the 3 hidden ones (mcp_live.HIDDEN_TOOLS)."""
    listed = {t for e in world["matrix"].values() for t in _list(e.get("mcp"))}
    assert HIDDEN_TOOLS <= listed and len(listed - HIDDEN_TOOLS) == len(set(world["tools"]) - HIDDEN_TOOLS)
