"""The checks behind scripts/acceptance/gke.sh: each one against a running hangar, over its REST API and MCP endpoint,
with kubectl where a check needs the cluster itself.

    python scripts/acceptance/hangar_accept.py <check> --state FILE --summary FILE [options]

Exit status: 0 pass, 1 fail, 3 skip. Progress goes to stdout as it happens; the check's time and one-line result go
to --summary (two lines). Everything a check creates is recorded in --state, and `cleanup` removes it, so gke.sh
can clean up after a failure or Ctrl-C. Every name it creates starts with `accept-`.

The hangar is $HANGAR_URL with $HANGAR_TOKEN, else the one `hangar login` saved (~/.hangar/cli.json).
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import hashlib
import json
import os
import posixpath
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

PASS, FAIL, SKIP = 0, 1, 3
ENVD_PORT = 49983
LABEL = "acceptance"  # sandboxes and sessions this script creates carry acceptance=<run>
TERMINAL = ("idle", "suspended", "failed", "archived")


class Fail(Exception):
    pass


class Skip(Exception):
    pass


def say(msg: str) -> None:
    print(f"      {msg}", flush=True)


# ---------------------------------------------------------------------------- connection and state
def target() -> tuple[str, str]:
    """(url, token): $HANGAR_URL/$HANGAR_TOKEN, else what `hangar login` saved, as the hangar CLI does."""
    cfg = Path(os.environ.get("HANGAR_CLI_CONFIG", Path.home() / ".hangar" / "cli.json"))
    try:
        saved = json.loads(cfg.read_text())
    except (OSError, ValueError):
        saved = {}
    url = (os.environ.get("HANGAR_URL") or saved.get("url") or "").rstrip("/")
    token = os.environ.get("HANGAR_TOKEN") or (saved.get("token") if saved.get("url", "").rstrip("/") == url else None)
    if not url or not token:
        raise Fail("no hangar to test: run `hangar login https://<your hangar> --token-file ~/.hangar/gke-admin-token`"
                   " (or set HANGAR_URL and HANGAR_TOKEN)")
    return url, token


class Api:
    def __init__(self, url: str, token: str):
        self.url, self.token = url, token
        self.http = httpx.Client(base_url=url, headers={"Authorization": f"Bearer {token}"},
                                 timeout=httpx.Timeout(1800, connect=20))

    def call(self, method: str, path: str, ok: tuple[int, ...] = (200,), **kw: Any) -> Any:
        r = self.http.request(method, path, **kw)
        if r.status_code not in ok:
            try:
                msg = r.json().get("error") or r.json().get("detail")
            except (ValueError, AttributeError):
                msg = r.text[:300]
            raise Fail(f"{method} {path}: HTTP {r.status_code}: {msg}")
        return r.json() if r.content and "json" in r.headers.get("content-type", "") else r.text

    def exec(self, sb: str, command: str, cwd: str | None = None, timeout: int = 120) -> dict[str, Any]:
        return self.call("POST", f"/api/sandboxes/{sb}/exec", json={"command": command, "cwd": cwd, "timeout": timeout})

    def must(self, sb: str, command: str, cwd: str | None = None, timeout: int = 120) -> str:
        r = self.exec(sb, command, cwd, timeout)
        if r["exit_code"] != 0:
            raise Fail(f"`{command[:80]}` exited {r['exit_code']}: {(r['stderr'] or r['stdout'])[-300:].strip()}")
        return r["stdout"]

    def session(self, name: str) -> dict[str, Any]:
        return self.call("GET", f"/api/sessions/{name}")

    def settle(self, name: str, timeout: float, want_sandbox: bool | None = None) -> dict[str, Any]:
        """Wait until the session is at rest (and has, or hasn't, a sandbox)."""
        deadline, last = time.monotonic() + timeout, ""
        while True:
            s = self.session(name)
            if s["state"] != last:
                say(f"session {name}: {s['state']}" + (f" ({s['reason']})" if s.get("reason") else ""))
                last = s["state"]
            done = s["state"] in TERMINAL and not s["busy"]
            if done and (want_sandbox is None or bool(s.get("sandbox_id")) == want_sandbox):
                return s
            if time.monotonic() > deadline:
                raise Fail(f"session {name} still {s['state']} after {timeout:.0f}s: {s.get('reason')}")
            time.sleep(2)

    def scopes_token(self, state: State, suffix: str, scopes: list[str], person: str | None = None
                     ) -> tuple[str, str]:
        body = {"name": f"accept-{state.run}-{suffix}", "scopes": scopes, **({"person": person} if person else {})}
        r = self.call("POST", "/api/tokens", json=body)
        state.add("tokens", r["id"])
        return r["token"], r["id"]


class State:
    """What this run created, so cleanup can remove it whatever happened in between."""

    def __init__(self, path: Path):
        self.path = path
        try:
            self.d: dict[str, Any] = json.loads(path.read_text())
        except (OSError, ValueError):
            self.d = {}
        self.d.setdefault("run", time.strftime("%m%d%H%M%S") + "-" + secrets.token_hex(2))

    @property
    def run(self) -> str:
        return self.d["run"]

    def get(self, key: str, default: Any = None) -> Any:
        return self.d.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self.d[key] = value
        self.save()

    def add(self, key: str, value: Any) -> None:
        self.d.setdefault(key, [])
        if value not in self.d[key]:
            self.d[key].append(value)
        self.save()

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.d, indent=1))
        tmp.replace(self.path)


def pct(xs: list[float], p: float) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(p / 100 * (len(xs) - 1))))]


def ms(xs: list[float]) -> str:
    return f"p50 {pct(xs, 50) * 1000:.1f} ms, p95 {pct(xs, 95) * 1000:.1f} ms"


def kubectl(args: list[str], opts: argparse.Namespace, stdin: str | None = None, timeout: int = 120,
            namespace: str | None = None) -> str:
    """kubectl in hangar's namespace (or `namespace`); Skip when the check can't use it."""
    if opts.no_kubectl or not shutil.which("kubectl"):
        raise Skip("needs kubectl (or --no-kubectl was given)")
    r = subprocess.run(["kubectl", "-n", namespace or opts.namespace, *args], input=stdin, capture_output=True,
                       text=True, timeout=timeout)
    if r.returncode != 0:
        raise Fail(f"kubectl {' '.join(args[:3])}: {r.stderr.strip()[-300:]}")
    return r.stdout


def tcp(api: Api, sb: str, host: str, port: int, seconds: int = 5) -> str:
    """'open', 'refused' or 'timeout' for a TCP connection from inside the sandbox (bash's /dev/tcp, no tools)."""
    probe = f"exec 3<>/dev/tcp/{host}/{port}"
    cmd = (f"timeout {seconds} bash -c {shlex.quote(probe)} 2>/dev/null; rc=$?; "
           "if [ $rc = 0 ]; then echo open; elif [ $rc = 124 ]; then echo timeout; else echo refused; fi")
    return (api.exec(sb, cmd, timeout=seconds + 30)["stdout"].strip().splitlines() or ["refused"])[-1]


def envd_listening(api: Api, sb: str, wait: float) -> bool:
    """Whether hangar-envd listens in the sandbox, nudging hangar to start it (any exec does) for up to `wait` s."""
    deadline = time.monotonic() + wait
    while True:
        if tcp(api, sb, "127.0.0.1", ENVD_PORT, 2) == "open":
            return True
        if time.monotonic() > deadline:
            return False
        time.sleep(1)


def sandbox_ip(api: Api, sb: str) -> str:
    out = api.must(sb, "hostname -i 2>/dev/null || hostname -I").split()
    ips = [x for x in out if re.fullmatch(r"\d+\.\d+\.\d+\.\d+", x) and not x.startswith("127.")]
    if not ips:
        raise Fail(f"could not find the sandbox's address (hostname -i: {' '.join(out)[:100]})")
    return ips[0]


def scrape(api: Api) -> dict[str, float] | None:
    """hangar_sandbox_exec_seconds_{count,sum,bucket} samples from /metrics, or None when it can't be read."""
    r = api.http.get("/metrics")
    if r.status_code != 200:
        return None
    out: dict[str, float] = {}
    for ln in r.text.splitlines():
        if ln.startswith("hangar_sandbox_exec_seconds_"):
            k, _, v = ln.rpartition(" ")
            with contextlib.suppress(ValueError):
                out[k] = float(v)
    return out


def by_transport(before: dict[str, float], after: dict[str, float]) -> dict[str, tuple[int, float]]:
    """transport -> (calls, mean seconds) between two scrapes."""
    out = {}
    for t in ("envd", "exec", "error"):
        k = f'hangar_sandbox_exec_seconds_count{{transport="{t}"}}'
        n = int(after.get(k, 0) - before.get(k, 0))
        if n > 0:
            s = after.get(k.replace("_count", "_sum"), 0) - before.get(k.replace("_count", "_sum"), 0)
            out[t] = (n, s / n)
    return out


# ---------------------------------------------------------------------------- checks
def _baseline(opts: argparse.Namespace) -> set[str]:
    """--- ops: the doctor checks that failed before a deploy (deploy.yml's `doctor-failing`); empty if unknown."""
    path = getattr(opts, "doctor_baseline", None)
    try:
        names = json.loads(Path(path).read_text()) if path else []
    except (OSError, ValueError):
        return set()
    return {n for n in names if isinstance(n, str)} if isinstance(names, list) else set()


def check_doctor(api: Api, state: State, opts: argparse.Namespace) -> str:
    checks = api.call("GET", "/api/doctor")
    fails = [c for c in checks if c["status"] == "fail"]
    warns = [c for c in checks if c["status"] == "warn"]
    for c in fails + warns:
        say(f"{c['status']}: {c['name']}: {c['detail']}" + (f"  (fix: {c['fix']})" if c.get("fix") else ""))
    # --- ops: a deploy rolls back for what it broke, not for what was already red before it (a rollback can't fix
    # that, and would block every deploy, the fix for it included); those are reported
    before = _baseline(opts)
    old = [c["name"] for c in fails if c["name"] in before]
    # --- integ3 (review #103): a check that says `deploy_gate: false` (credential health: a person's login expiring
    # during the deploy window) is reported, never a rollback
    off = [c["name"] for c in fails if c["name"] not in before and c.get("deploy_gate") is False]
    new = [c for c in fails if c["name"] not in before and c.get("deploy_gate") is not False]
    also = f"; already failing before the deploy: {', '.join(old)}" if old else ""
    also += f"; failing, but not a reason to roll back: {', '.join(off)}" if off else ""
    if new:
        raise Fail(f"{len(new)} {'new ' if before else ''}problem(s): {', '.join(c['name'] for c in new)}; `hangar "
                   f"doctor --fix` repairs what it can{also}")
    return f"{len(checks)} checks, no {'new ' if old or off else ''}failures" + (
        f", {len(warns)} warning(s)" if warns else "") + also


def doctor_failing() -> int:
    """--- ops: the names of the doctor's failing checks as JSON (deploy.yml's baseline before a rollout); 2 if
    hangar can't be asked, so the caller falls back to no baseline (every failure then counts)."""
    try:
        checks = Api(*target()).call("GET", "/api/doctor")
    except Exception as e:  # noqa: BLE001
        print(f"can't read the doctor: {type(e).__name__}: {e}", file=sys.stderr)
        return 2
    print(json.dumps(sorted({c["name"] for c in checks if c.get("status") == "fail"})))
    return 0


def check_k8s(api: Api, state: State, opts: argparse.Namespace) -> str:
    """The upgrade's manifests are applied: the new NetworkPolicies, the grace period, a ready pod."""
    dep = json.loads(kubectl(["get", "deploy", "hangar", "-o", "json"], opts))
    spec = dep["spec"]["template"]["spec"]
    image = spec["containers"][0]["image"]
    say(f"image {image}, {dep['status'].get('readyReplicas', 0)}/{dep['spec']['replicas']} ready")
    problems = []
    if dep["status"].get("readyReplicas", 0) < 1:
        problems.append("no ready hangar pod")
    if spec.get("terminationGracePeriodSeconds", 30) < 45:
        problems.append(f"terminationGracePeriodSeconds is {spec.get('terminationGracePeriodSeconds', 30)}, not 45")
    sbns = opts.sandbox_namespace
    pols = json.loads(kubectl(["get", "networkpolicy", "-o", "json"], opts, namespace=sbns))
    have = {p["metadata"]["name"] for p in pols.get("items", [])}
    need = {"sandbox-egress-baseline", "sandbox-deny-ingress", "allow-hangar-envd", "allow-hangar-gateway"}
    say(f"NetworkPolicies in {sbns}: {', '.join(sorted(have)) or 'none'}")
    if missing := need - have:
        problems.append(f"NetworkPolicies missing in {sbns}: {', '.join(sorted(missing))} (kubectl apply -k)")
    with contextlib.suppress(Fail):
        site = json.loads(kubectl(["get", "configmap", "hangar-site", "-o", "json"], opts)).get("data") or {}
        say("hangar-site sets: " + (", ".join(sorted(site)) or "nothing"))
    if problems:
        raise Fail("; ".join(problems))
    return f"manifests applied; {image.rsplit('/', 1)[-1]}"


def check_sandbox(api: Api, state: State, opts: argparse.Namespace) -> str:
    t0 = time.monotonic()
    sb = api.call("POST", "/api/sandboxes", json={
        "environment": opts.env, "name": f"accept-{state.run}-a", "ttl_minutes": 120,
        "labels": {LABEL: state.run}})[0]
    if "id" not in sb:  # the create failed: {"error": ...}
        raise Fail(sb.get("error") or str(sb))
    state.add("sandboxes", sb["id"])
    state.set("sandbox", sb["id"])
    secs = time.monotonic() - t0
    if sb["state"] != "running":
        raise Fail(f"sandbox {sb['id']} is {sb['state']}: {sb.get('error')}")
    out = api.must(sb["id"], "id -un; uname -m").split()
    say(f"{sb['id']} on {sb['provider']} ({sb.get('provider_ref')}), user {out[0]}, {out[-1]}")
    state.set("provider", sb["provider"])
    return f"{opts.env} sandbox {sb['id']} on {sb['provider']} in {secs:.1f}s"


def _need_sandbox(state: State) -> str:
    sb = state.get("sandbox")
    if not sb:
        raise Skip("needs the sandbox check")
    return sb


IN_POD = r"""
import asyncio, base64, json, sys, time
from hangar.config import get_settings
from hangar.envd import OFF
from hangar.models import Sandbox
from hangar.providers.kubernetes import KubernetesProvider

async def main():
    sb, n = Sandbox.model_validate_json(base64.b64decode(sys.argv[1])), int(sys.argv[2])
    p = KubernetesProvider(get_settings())
    envd = p.envd
    out = {"exec": [], "envd": [], "daemon": None}
    try:
        p.envd = OFF  # the provider's own path: pods/exec through the API server and kubelet
        for _ in range(n):
            t = time.perf_counter(); r = await p.exec(sb, "true", 30); out["exec"].append(time.perf_counter() - t)
            assert r.exit_code == 0, r
        p.envd = envd
        ep = await envd.ready(sb)
        if ep is not None:
            out["daemon"] = f"{ep.url} {ep.version}"
            for _ in range(n):
                t = time.perf_counter(); r = await p.exec(sb, "true", 30); out["envd"].append(time.perf_counter() - t)
                assert r.exit_code == 0 and r.transport == "envd", r
    finally:
        await envd.close()
    print("HANGAR-ACCEPT " + json.dumps(out))

asyncio.run(main())
"""


def check_exec(api: Api, state: State, opts: argparse.Namespace) -> str:
    sb = _need_sandbox(state)
    up = envd_listening(api, sb, opts.envd_wait)
    say(f"hangar-envd in the sandbox: {'listening' if up else f'not listening after {opts.envd_wait:.0f}s'}")
    for _ in range(15 if up else 0):  # the control plane finds a daemon in the background: wait until it uses it
        b = scrape(api)
        api.exec(sb, "true")
        a = scrape(api)
        if b is None or a is None or "envd" in by_transport(b, a):
            break
        time.sleep(1)
    before = scrape(api)
    times = []
    for _ in range(opts.exec_calls):
        t = time.perf_counter()
        r = api.exec(sb, "true")
        times.append(time.perf_counter() - t)
        if r["exit_code"] != 0:
            raise Fail(f"`true` exited {r['exit_code']}: {r['stderr'][-200:]}")
    after = scrape(api)
    say(f"{opts.exec_calls} x POST /api/sandboxes/{{id}}/exec `true`, from here: {ms(times)}")
    transport = "unknown (no /metrics)"
    if before is not None and after is not None:
        seen = by_transport(before, after)
        transport = " + ".join(f"{t} ({n} calls, server mean {s * 1000:.1f} ms)" for t, (n, s) in seen.items()) or "?"
        say(f"transport (from /metrics, any other execs on this hangar included): {transport}")
        envd_calls = seen.get("envd", (0, 0))[0]
        if opts.expect_envd and envd_calls < opts.exec_calls:
            raise Fail(f"expected every call through hangar-envd, saw {transport}. On GKE: is envd-network.yaml "
                       "applied (kubectl -n hangar-sandboxes get networkpolicy allow-hangar-envd)? Is HANGAR_ENVD off?")
    summary = f"{ms(times)} end to end; {transport}"
    if state.get("provider") == "kubernetes" and not opts.no_kubectl and shutil.which("kubectl"):
        sbj = api.call("GET", f"/api/sandboxes/{sb}")
        raw = base64.b64encode(json.dumps(sbj).encode()).decode()
        say(f"in the control-plane pod: {opts.exec_calls} x `true` through pods/exec, then through hangar-envd")
        out = kubectl(["exec", "-i", "deploy/hangar", "-c", "hangar", "--", "python3", "-", raw,
                       str(opts.exec_calls)], opts, stdin=IN_POD, timeout=600)
        line = next((ln for ln in out.splitlines() if ln.startswith("HANGAR-ACCEPT ")), None)
        if line is None:
            raise Fail(f"the in-pod measurement printed nothing usable: {out[-300:]}")
        m = json.loads(line.split(" ", 1)[1])
        say(f"pods/exec (hangar's fallback, `bash -lc`): {ms(m['exec'])}")
        if m["envd"]:
            say(f"hangar-envd at {m['daemon']}: {ms(m['envd'])}")
            summary += (f"; in-cluster pods/exec {pct(m['exec'], 50) * 1000:.1f} ms vs envd "
                        f"{pct(m['envd'], 50) * 1000:.1f} ms p50")
        else:
            say("hangar-envd: not reachable from the control plane")
            summary += f"; in-cluster pods/exec {pct(m['exec'], 50) * 1000:.1f} ms p50, envd unreachable"
    return summary


def check_stream(api: Api, state: State, opts: argparse.Namespace) -> str:
    """A streamed exec over REST Server-Sent Events (#68), through the ingress: the first line arrives while the
    command still runs, so nothing between here and hangar-envd (the load balancer included) buffers the stream."""
    sb = _need_sandbox(state)
    command = "for i in 1 2 3 4 5; do echo tick$i; sleep 0.5; done"
    t0 = time.perf_counter()
    first, out, result, event = None, "", None, None
    with api.http.stream("POST", f"/api/sandboxes/{sb}/exec/stream", json={"command": command, "timeout": 60}) as r:
        if r.status_code != 200:
            r.read()
            raise Fail(f"POST /api/sandboxes/{{id}}/exec/stream: HTTP {r.status_code}: {r.text[:300]}")
        for line in r.iter_lines():
            if line.startswith("event: "):
                event = line[7:]
            elif line.startswith("data: "):
                data = json.loads(line[6:])
                if event == "stdout":
                    first = time.perf_counter() - t0 if first is None else first
                    out += data.get("data", "")
                elif event == "result":
                    result = data
                elif event in ("error", "close"):
                    raise Fail(f"the stream ended with `{event}`: {data}")
            elif not line:
                event = None
    total = time.perf_counter() - t0
    if result is None or result.get("exit_code") != 0:
        raise Fail(f"no successful result event: {result}")
    if out.split() != [f"tick{i}" for i in range(1, 6)]:
        raise Fail(f"the streamed output was {out[:200]!r}")
    if not result.get("streamed"):
        if opts.expect_envd:
            raise Fail("the command wasn't streamed (no hangar-envd in the sandbox): the output came in one piece")
        return f"not streamed (no hangar-envd): the output came at the end, {total * 1000:.0f} ms"
    say(f"first output after {first * 1000:.0f} ms; the command took {total * 1000:.0f} ms (it prints for 2 s)")
    if first is None or first > total - 1.5:
        raise Fail(f"the first line arrived after {first * 1000:.0f} ms of {total * 1000:.0f} ms: something between "
                   "here and hangar buffers Server-Sent Events")
    return f"first output after {first * 1000:.0f} ms of {total * 1000:.0f} ms"


async def _mcp(url: str, token: str, calls: list[tuple[str, dict[str, Any]]]) -> list[Any]:
    from mcp import Client
    from mcp.client.streamable_http import streamable_http_client
    from mcp.shared._httpx_utils import create_mcp_http_client

    out = []
    http = create_mcp_http_client(headers={"Authorization": f"Bearer {token}"})
    async with http, Client(streamable_http_client(f"{url}/mcp", http_client=http)) as c:
        for name, args in calls:
            res = await c.call_tool(name, args)
            if getattr(res, "is_error", False) or getattr(res, "isError", False):
                raise Fail(f"MCP {name}: " + " ".join(getattr(b, "text", "") for b in res.content)[:300])
            sc = getattr(res, "structured_content", None) or getattr(res, "structuredContent", None)
            if sc is None:
                sc = json.loads("".join(getattr(b, "text", "") for b in res.content) or "null")
            out.append(sc.get("result", sc) if isinstance(sc, dict) and set(sc) == {"result"} else sc)
    return out


CLIENT_DEADLINE = 230.0  # --- deadlines: claude.ai gives up on a tool call after about this long
POLL_SECONDS = 3.0
# --- #294: what hangar says while a sandbox waits for a node that can't come (providers/kubernetes._explain_event).
# "adding a node" isn't here: that one usually arrives.
NO_ROOM = ("the cluster can't add a node", "node pool is at its maximum size")
ROOM_GRACE = 90.0  # how long a no-room reason must last before a capacity-bound check gives up (and skips)


def no_room(stage: object) -> bool:
    return isinstance(stage, str) and any(m in stage for m in NO_ROOM)


def fleet_no_room(api: Api) -> str | None:
    """The reason a starting sandbox gives when the pool has no room and can't grow, with how many run, or None."""
    sbs = api.call("GET", "/api/sandboxes")
    why = next(((sb.get("info") or {}).get("stage") for sb in sbs
                if sb.get("state") == "starting" and no_room((sb.get("info") or {}).get("stage"))), None)
    if why is None:
        return None
    return f"{sum(1 for sb in sbs if sb.get('state') == 'running')} sandboxes running; {why}"


async def _mcp_within(url: str, token: str, name: str, args: dict[str, Any], deadline: float
                      ) -> tuple[Any, float, dict[str, Any]]:
    """Call one tool as a deadline-bound client does: give up after `deadline` seconds and disconnect. Returns
    (the result, or None if it gave up; the seconds the call took; what the server said about itself)."""
    from mcp import Client
    from mcp.client.streamable_http import streamable_http_client
    from mcp.shared._httpx_utils import create_mcp_http_client

    http = create_mcp_http_client(headers={"Authorization": f"Bearer {token}"})
    async with http, Client(streamable_http_client(f"{url}/mcp", http_client=http)) as c:
        t0 = time.perf_counter()
        tools = (await c.list_tools()).tools
        about = {"version": c.server_info.version, "tools": len(tools), "list_ms": (time.perf_counter() - t0) * 1000}
        t0 = time.perf_counter()
        try:
            async with asyncio.timeout(deadline):
                res = await c.call_tool(name, args)
        except TimeoutError:
            return None, time.perf_counter() - t0, about  # leaving the block disconnects, as the client would
    secs = time.perf_counter() - t0
    text = " ".join(getattr(b, "text", "") for b in res.content)
    if res.is_error:
        raise Fail(f"MCP {name}: {text[:300]}")
    sc = res.structured_content
    return (sc.get("result", sc) if isinstance(sc, dict) and set(sc) == {"result"} else sc), secs, about


def check_mcp(api: Api, state: State, opts: argparse.Namespace) -> str:
    """The way claude.ai uses hangar: create_sandbox over MCP on a multi-harness environment (a pack or an image
    may be built first) must answer within the client's 230 s, starting or running; a second create whose client
    drops after a few seconds must still come up (not end in `error`); then exec over MCP in the first."""
    base = {"environment": opts.mcp_env, "ttl_minutes": 120, "labels": {LABEL: state.run}}
    sb, secs, about = asyncio.run(_mcp_within(api.url, api.token, "create_sandbox",
                                              {**base, "name": f"accept-{state.run}-mcp"}, CLIENT_DEADLINE))
    say(f"server {about['version']}: {about['tools']} tools listed in {about['list_ms']:.0f} ms")
    if sb is None:
        raise Fail(f"create_sandbox on {opts.mcp_env} didn't answer within {CLIENT_DEADLINE:.0f}s, so claude.ai would "
                   "have shown an error; HANGAR_MCP_TOOL_WAIT_SECONDS should keep it under that")
    state.add("sandboxes", sb["id"])
    say(f"create_sandbox answered in {secs:.1f}s: {sb['id']} {sb['state']}"
        + (f" ({sb['stage']})" if sb.get("stage") else ""))
    if sb["state"] not in ("running", "starting", "pending"):
        raise Fail(f"create_sandbox answered {sb['state']}: {sb.get('error')}")
    drop_name = f"accept-{state.run}-mcp-drop"
    dropped, drop_secs, _ = asyncio.run(_mcp_within(api.url, api.token, "create_sandbox",
                                                    {**base, "name": drop_name}, opts.mcp_drop_after))
    say(f"a client that gives up after {opts.mcp_drop_after:.0f}s: " + (
        "dropped mid-create" if dropped is None else f"got its answer first ({drop_secs:.1f}s, {dropped['state']})"))
    up = _running(api, sb["id"], opts.boot_timeout)
    mine = [s for s in api.call("GET", "/api/sandboxes", params={"label": f"{LABEL}={state.run}"})
            if s["name"].startswith(drop_name)]
    for s in mine:
        state.add("sandboxes", s["id"])
    if not mine:
        raise Fail(f"the sandbox whose client dropped ({drop_name}) is gone: the disconnect cancelled its create")
    _running(api, mine[0]["id"], opts.boot_timeout)
    ran, = asyncio.run(_mcp(api.url, api.token, [("exec", {"sandbox": sb["id"], "command": "echo mcp-ok; id -un"})]))
    if ran["exit_code"] != 0 or "mcp-ok" not in ran["stdout"]:
        raise Fail(f"exec over MCP exited {ran['exit_code']}: {(ran['stderr'] or ran['stdout'])[-300:].strip()}")
    return (f"{opts.mcp_env}: answered in {secs:.0f}s ({sb['state']}), running after {up:.0f}s; a dropped client's "
            f"create still came up; exec ok; {about['version']}")


def _running(api: Api, sb: str, timeout: float, capacity_skip: bool = False) -> float:
    """Seconds until the sandbox is running; fails on `error`, `stopped` or the timeout. With `capacity_skip` (the
    capacity-bound checks, #294), a sandbox that waits ROOM_GRACE seconds on a pool that can't grow skips the check
    with the numbers, rather than timing out."""
    t0, last, blocked = time.monotonic(), None, None
    while True:
        s = api.call("GET", f"/api/sandboxes/{sb}")
        now = (s["state"], (s.get("info") or {}).get("stage"))
        if now != last:
            say(f"{sb}: {now[0]}" + (f" ({now[1]})" if now[1] and now[0] != "running" else ""))
            last = now
        if s["state"] == "running":
            return time.monotonic() - t0
        if s["state"] in ("error", "stopped"):
            raise Fail(f"sandbox {sb} ended {s['state']}: {s.get('error')}")
        blocked = (blocked or time.monotonic()) if no_room(now[1]) else None
        if capacity_skip and blocked is not None and time.monotonic() - blocked >= ROOM_GRACE:
            running = sum(1 for x in api.call("GET", "/api/sandboxes") if x.get("state") == "running")
            raise Skip(f"no room for another sandbox: {running} sandboxes running, and after "
                       f"{time.monotonic() - t0:.0f}s {sb} still waits: {now[1]}")
        if time.monotonic() - t0 > timeout:
            raise Fail(f"sandbox {sb} still {s['state']} after {timeout:.0f}s ({now[1]})")
        time.sleep(POLL_SECONDS)


def check_files(api: Api, state: State, opts: argparse.Namespace) -> str:
    """Write and read back a binary file through MCP's write_file/read_file (the provider's file transfer: envd or
    exec), and check it from a shell in the sandbox too."""
    sb = _need_sandbox(state)
    data = secrets.token_bytes(opts.file_kb * 1024)
    digest = hashlib.sha256(data).hexdigest()
    path = f"/tmp/accept-{state.run}/blob.bin"
    t0 = time.perf_counter()
    wrote, = asyncio.run(_mcp(api.url, api.token, [("write_file", {"sandbox": sb, "path": path, "base64_encoded": True,
                                                                     "content": base64.b64encode(data).decode()})]))
    t1 = time.perf_counter()
    got, = asyncio.run(_mcp(api.url, api.token, [("read_file", {"sandbox": sb, "path": path, "binary": True})]))
    t2 = time.perf_counter()
    if not got.get("exists") or base64.b64decode(got["base64"]) != data:
        back = base64.b64decode(got.get("base64") or "")
        raise Fail(f"read back {len(back)} bytes that don't match the {len(data)} written")
    inside = api.must(sb, f"sha256sum {shlex.quote(path)}").split()[0]
    if inside != digest:
        raise Fail(f"the file in the sandbox has sha256 {inside[:12]}, expected {digest[:12]}")
    say(f"wrote {wrote.get('bytes')} bytes in {(t1 - t0) * 1000:.0f} ms, read them back in {(t2 - t1) * 1000:.0f} ms "
        "(MCP, through the ingress)")
    return f"{opts.file_kb} KiB round trip intact (write {(t1 - t0) * 1000:.0f} ms, read {(t2 - t1) * 1000:.0f} ms)"


def check_isolation(api: Api, state: State, opts: argparse.Namespace) -> str:
    """From sandbox A: B's hangar-envd port, the metadata servers and the control plane's API port are closed, while
    the internet is open (so a closed port means a policy, not a sandbox without network)."""
    a = _need_sandbox(state)
    b = api.call("POST", "/api/sandboxes", json={
        "environment": opts.env, "name": f"accept-{state.run}-b", "ttl_minutes": 60, "labels": {LABEL: state.run}})[0]
    if "id" not in b:
        raise Fail(f"second sandbox: {b.get('error') or b}")
    state.add("sandboxes", b["id"])
    k8s = b["provider"] == "kubernetes"
    ip = sandbox_ip(api, b["id"])
    b_envd = envd_listening(api, b["id"], opts.envd_wait)
    say(f"sandbox B {b['id']} at {ip}; its hangar-envd {'listens' if b_envd else 'does not listen'} on {ENVD_PORT}")
    host, _, port = opts.internet_probe.rpartition(":")
    probes: list[tuple[str, str, int, bool]] = [(f"the internet ({opts.internet_probe})", host, int(port), True)]
    if b_envd:
        probes.append((f"sandbox B's hangar-envd ({ip}:{ENVD_PORT})", ip, ENVD_PORT, False))
    if k8s:
        probes += [("the metadata server (169.254.169.254:80)", "169.254.169.254", 80, False),
                   ("GKE's Workload Identity metadata server (169.254.169.252:988)", "169.254.169.252", 988, False),
                   ("hangar's API through its Service (hangar.hangar.svc:80)",
                    f"hangar.{opts.namespace}.svc.cluster.local", 80, False)]
        with contextlib.suppress(Fail, Skip):
            pod_ip = kubectl(["get", "pod", "-l", "app=hangar", "-o", "jsonpath={.items[0].status.podIP}"], opts)
            if pod_ip.strip():
                probes.append((f"hangar's API port on its pod ({pod_ip.strip()}:8787)", pod_ip.strip(), 8787, False))
    bad, notes = [], []
    for what, host, port, should_open in probes:
        got = tcp(api, a, host, port)
        ok = (got == "open") == should_open
        say(f"{'ok  ' if ok else 'BAD '} {what}: {got}")
        if not ok:
            (bad if not should_open else notes).append(f"{what} is {got}")
    if notes:
        raise Fail(f"{'; '.join(notes)}: with no way out, the closed ports below prove nothing")
    if bad:
        raise Fail("reachable from a sandbox: " + "; ".join(bad))
    if not b_envd:
        say("sandbox-to-sandbox not tested: B's daemon never listened (HANGAR_ENVD=off, or no binary)")
    if not k8s:
        say("metadata server and control-plane checks are for Kubernetes sandboxes; skipped here")
    return f"{len(probes) - 1} closed as they should be; the internet is reachable"


# ---------------------------------------------------------------------------- --- #293: whose turn, which login
TEAM_KEY_FIX = "hangar cred add NAME --team --harness {harness} --kind api_key --file key.txt"


def as_person(api: Api, state: State, opts: argparse.Namespace) -> Api:
    """The client that creates this run's sessions and jobs: `api` itself, or with --as PERSON one with an admin token
    of that person's, so the work is theirs and their own login reaches its turns (a deploy's admin token belongs to
    no person, and a personal subscription login never reaches work no person owns)."""
    who = getattr(opts, "as_person", None)
    if not who:
        return api
    if (made := getattr(opts, "_as_api", None)) is not None:
        return made
    try:
        tok, _ = api.scopes_token(state, f"as-{opts.check}", ["admin"], person=who)
    except Fail as e:
        raise Fail(f"--as {who}: couldn't mint a token for them ({e}); --as needs a person with the admin role "
                   "(hangar person ls)") from None
    opts._as_api = Api(api.url, tok)
    return opts._as_api


def need_login(api: Api, opts: argparse.Namespace, *, environment: str | None = None, person: str | None = None,
               repo: str | None = None, purpose: str = "interactive") -> None:
    """Skip a check whose agent turn would start with no login (#293): ask hangar which credentials a session (or a
    job) on `environment` for `person` would get. A hangar without the question (before #293) goes on."""
    params = {"environment": environment, "person": person, "repo": repo, "purpose": purpose}
    try:
        r = api.http.get("/api/credentials/login", params={k: v for k, v in params.items() if v})
    except httpx.HTTPError:
        return
    if r.status_code != 200:
        return
    d = r.json()
    if d.get("login") is not False:
        return
    harness = d.get("harness") or "its harness"
    whose = f"{person}'s" if person else \
        "this run's (its token belongs to no person, so no personal login reaches them)"
    raise Skip(f"no login for {harness} would reach {whose} turns on {d.get('environment')}: add a team API key ("
               + TEAM_KEY_FIX.format(harness=harness) + ")" + ("" if person else ", or rerun with --as PERSON"))


def _new_session(api: Api, state: State, name: str, opts: argparse.Namespace, **extra: Any) -> dict[str, Any]:
    """A session with no first message: it boots a sandbox and waits. idle_minutes 0: it stays up until suspended.
    With --as PERSON it is that person's (#293)."""
    body = {"name": name, "environment": opts.session_env, "repo": opts.repo, "idle_minutes": 0,
            "labels": {LABEL: state.run}, "model": opts.model,
            **({"budget": {"max_cost_usd": opts.max_usd}} if getattr(opts, "max_usd", None) else {}),  # --- acceptance
            **extra}
    as_person(api, state, opts).call("POST", "/api/sessions", json={k: v for k, v in body.items() if v is not None})
    state.add("sessions", name)
    return api.settle(name, opts.boot_timeout, want_sandbox=True)


def _repo_path(api: Api, sandbox_id: str) -> str:
    path = (api.call("GET", f"/api/sandboxes/{sandbox_id}").get("info") or {}).get("repo_path")
    if not path:
        raise Fail(f"the session's sandbox has no repo checkout ({sandbox_id}); is --repo reachable from it?")
    return path


def check_session(api: Api, state: State, opts: argparse.Namespace) -> str:
    """A session without a model turn: commit, change and add files in its sandbox, checkpoint, suspend (the sandbox
    goes), open again (a new sandbox restores the checkpoint) and find all three."""
    name = f"accept-{state.run}"
    t0 = time.monotonic()
    s = _new_session(api, state, name, opts)
    boot = time.monotonic() - t0
    if s["state"] != "idle":
        raise Fail(f"session {name} is {s['state']}: {s.get('reason')}")
    state.set("session", name)
    first = s["sandbox_id"]
    repo = _repo_path(api, first)
    mark = f"hangar acceptance {state.run}"
    api.must(first, f"git commit -q --allow-empty -m {shlex.quote(mark)} && "
                    f"echo {shlex.quote(mark)} > accept-untracked.txt && "
                    f"f=$(git ls-files | head -1) && [ -n \"$f\" ] && echo {shlex.quote(mark)} >> \"$f\"", cwd=repo)
    t = time.monotonic()
    ck = api.call("POST", f"/api/sessions/{name}/checkpoint")["checkpoint"]
    if not ck:
        raise Fail("POST /checkpoint stored no checkpoint")
    ck_secs = time.monotonic() - t
    say(f"checkpoint {ck['id']}: {', '.join(f'{p} {n} B' for p, n in sorted(ck['parts'].items()))} in {ck_secs:.1f}s")
    t = time.monotonic()
    api.call("POST", f"/api/sessions/{name}/suspend")
    s = api.settle(name, opts.boot_timeout, want_sandbox=False)
    sus_secs = time.monotonic() - t
    if s["state"] != "suspended":
        raise Fail(f"suspend left the session {s['state']}: {s.get('reason')}")
    gone = api.call("GET", f"/api/sandboxes/{first}")["state"]
    t = time.monotonic()
    api.call("POST", f"/api/sessions/{name}/open")
    s = api.settle(name, opts.boot_timeout, want_sandbox=True)
    res_secs = time.monotonic() - t
    if s["state"] != "idle" or s["sandbox_id"] == first:
        raise Fail(f"open after suspend: {s['state']}, sandbox {s['sandbox_id']}: {s.get('reason')}")
    if "older checkpoint" in (s.get("reason") or ""):
        raise Fail(s["reason"])
    repo = _repo_path(api, s["sandbox_id"])
    out = api.must(s["sandbox_id"], "git log -1 --format=%s; cat accept-untracked.txt; git status --porcelain",
                   cwd=repo)
    lines = out.splitlines()
    if lines[:2] != [mark, mark] or not any(ln.startswith(" M ") for ln in lines[2:]):
        raise Fail(f"the restored sandbox doesn't have the work: {out[-400:]!r}")
    say(f"first sandbox {first} is {gone} after suspend; reopened in {s['sandbox_id']} with the commit, the change "
        "and the untracked file")
    return (f"boot {boot:.1f}s, checkpoint {ck_secs:.1f}s, suspend {sus_secs:.1f}s, resume + restore {res_secs:.1f}s; "
            f"{s['checkpoint_count']} checkpoint(s)")


def _turn(api: Api, name: str, message: str, timeout: float, on_wait: Callable[[], bool] | None = None
          ) -> dict[str, Any]:
    """Send a message and wait for its turn to end (`on_wait` runs every poll while it does; True stops calling it)."""
    before = len(api.session(name)["turns"])
    api.call("POST", f"/api/sessions/{name}/send", json={"message": message})
    deadline = time.monotonic() + timeout
    while True:
        s = api.session(name)
        t = s["turns"][-1] if len(s["turns"]) > before else None
        if t and t.get("finished_at") and not s["busy"]:
            return s
        if on_wait is not None and on_wait():
            on_wait = None
        if time.monotonic() > deadline:
            raise Fail(f"the turn is still running after {timeout:.0f}s ({s['state']})")
        time.sleep(2)


def check_model(api: Api, state: State, opts: argparse.Namespace) -> str:
    """A real, short agent turn in the session (it costs a few cents)."""
    if not opts.with_model:  # --- acceptance: decided here, so every check's result is in the report
        raise Skip("costs money: run with --with-model")
    name = state.get("session")
    if not name:
        raise Skip("needs the session check")
    sess = api.session(name)  # --- #293: the session's own person and environment decide its login
    need_login(api, opts, environment=sess.get("environment") or opts.session_env, person=sess.get("person"))
    word = f"hangar-accept-{state.run}"
    t0 = time.monotonic()
    s = _turn(api, name, f"Reply with exactly this text and nothing else, without using any tools: {word}",
              opts.turn_timeout)
    t = s["turns"][-1]
    u = t.get("usage") or {}
    say(f"turn: exit {t['exit_code']}, outcome {t.get('outcome')}, live {t.get('live')}, cache TTL "
        f"{t.get('cache_ttl')}, {u.get('input_tokens', 0)} in / {u.get('output_tokens', 0)} out tokens, "
        f"${u.get('cost_usd') or 0:.4f}")
    say(f"summary: {(t.get('summary') or '')[:200]!r}")
    if t["exit_code"] != 0:
        raise Fail(f"the turn exited {t['exit_code']} ({t.get('outcome')}: {t.get('outcome_reason')}); does "
                   f"{opts.session_env} have a model credential?")
    if word not in (t.get("summary") or ""):
        raise Fail("the agent's answer doesn't contain the word it was asked to say")
    return f"{s['harness']} turn in {time.monotonic() - t0:.1f}s, ${u.get('cost_usd') or 0:.4f}, live={t.get('live')}"


def check_steer(api: Api, state: State, opts: argparse.Namespace) -> str:
    """A live turn with approvals=ask: the agent asks to run a command, hangar holds it until it is allowed, and a
    steer message reaches the turn while it waits (it costs a few cents).

    The command writes outside the repository (#293): Claude Code allows a write inside its working directory on its
    own, so `echo ok > file` in the repo never reached hangar as an approval. A write elsewhere always asks."""
    if not opts.with_model:  # --- acceptance
        raise Skip("costs money: run with --with-model")
    name = f"accept-{state.run}-steer"
    need_login(api, opts, environment=opts.session_env, person=getattr(opts, "as_person", None))  # --- #293
    s = _new_session(api, state, name, opts, approvals="ask")
    if s["state"] != "idle":
        raise Fail(f"session {name} is {s['state']}: {s.get('reason')}")
    repo = _repo_path(api, s["sandbox_id"])
    home = api.must(s["sandbox_id"], 'printf %s "$HOME"').strip() or "/tmp"
    outside = home if repo.rstrip("/") != home.rstrip("/") else posixpath.dirname(home)  # never inside the checkout
    target = posixpath.join(outside, f"accept-steer-{state.run}.txt")
    seen: dict[str, Any] = {}

    def wait() -> bool:
        ap = api.call("GET", f"/api/sessions/{name}/approvals")
        for a in ap["pending"]:
            if target not in (a.get("detail") or ""):  # something else the agent asked first: let it, keep waiting
                say(f"approval {a['id']} pending for something else: {a['tool']} {a['detail'][:120]!r}; allowing")
                api.call("POST", f"/api/sessions/{name}/approvals/{a['id']}", json={"decision": "allow"})
                continue
            seen["approval"] = a
            say(f"approval {a['id']} pending: {a['tool']} {a['detail'][:120]!r} (live={ap['live']})")
            try:  # whatever happens to it, the approval is answered next, so the turn doesn't sit waiting
                st = api.call("POST", f"/api/sessions/{name}/steer",
                              json={"message": "When you are done, end your reply with the word STEERED."})
            except Fail as e:
                st = {"status": "failed", "error": str(e)}
            seen["steer"] = st
            say(f"steer: {st['status']}")
            api.call("POST", f"/api/sessions/{name}/approvals/{a['id']}", json={"decision": "allow"})
            say("allowed")
            return True
        return False

    s = _turn(api, name, f"Use your shell tool to run exactly this command: echo ok > {target} . Then reply DONE.",
              opts.turn_timeout, on_wait=wait)
    t = s["turns"][-1]
    approvals, steers = [a["status"] for a in t.get("approvals", [])], [x["status"] for x in t.get("steers", [])]
    say(f"turn: exit {t['exit_code']}, live {t.get('live')}, approvals {approvals}, steers {steers}")
    if not t.get("live"):
        raise Fail("the turn ran one-shot, not under hangar's driver (HANGAR_LIVE_TURNS off, or no python3 in the "
                   "sandbox?)")
    if "approval" not in seen:
        raise Fail(f"the agent's command never waited for an approval (it writes {target}, outside the repository)")
    held = next((a for a in t.get("approvals", []) if a.get("id") == seen["approval"]["id"]), None)
    if held is None:
        raise Fail(f"approval {seen['approval']['id']} isn't in the turn's approvals")
    if held.get("status") not in ("allowed", "allowed_always") or held.get("by") == "policy":
        raise Fail(f"approval {held['id']} ended {held.get('status')} by {held.get('by')}, not allowed by this check")
    if seen["steer"]["status"] not in ("delivered", "queued"):
        raise Fail(f"the steer message was {seen['steer']['status']}: {seen['steer'].get('error')}")
    if api.exec(s["sandbox_id"], f"cat {shlex.quote(target)}", cwd=repo)["stdout"].strip() != "ok":
        raise Fail("the allowed command didn't run")
    said = "STEERED" in (t.get("summary") or "")
    say(f"the agent {'acted on' if said else 'did not visibly act on'} the steer message (model behaviour: not "
        "checked)")
    return f"approval held and allowed, steer {seen['steer']['status']}, command ran"


def check_gateway(api: Api, state: State, opts: argparse.Namespace) -> str:
    """Through the git gateway from the session's sandbox: a push to hangar/accept-<run> goes through, a push to a
    branch outside the sandbox's grant is refused, and no GitHub token is in the sandbox.

    The refused push goes to accept-<run>-outside, not main: the gateway refuses both the same way (the grant's push
    refs), and if it let the push through (HANGAR_GATEWAY_PUSH_REFS widened, or a bug), the owner's main is
    untouched and cleanup deletes the branch."""
    if not opts.gateway_repo:
        raise Skip("needs --gateway-repo OWNER/REPO (a repo the gateway's credential can push to)")
    name = state.get("session")
    if not name:
        raise Skip("needs the session check (run with --repo https://github.com/OWNER/REPO.git)")
    s = api.session(name)
    if f"github.com/{opts.gateway_repo}".lower() not in (s.get("repo") or "").lower():
        raise Skip(f"the session's repo is {s.get('repo')}, not {opts.gateway_repo}: leave out --repo")
    if not s.get("sandbox_id"):
        api.call("POST", f"/api/sessions/{name}/open")
        s = api.settle(name, opts.boot_timeout, want_sandbox=True)
    sb = api.call("GET", f"/api/sandboxes/{s['sandbox_id']}")
    git = (sb.get("info") or {}).get("git") or {}
    say(f"sandbox git mode: {git}")
    if git.get("mode") != "gateway":
        raise Fail("the session's sandbox isn't in gateway mode: set HANGAR_GATEWAY_URL "
                   "(http://hangar-gateway.hangar.svc:8788 on GKE; docs/security/README.md)")
    repo = sb["info"]["repo_path"]
    leaks = api.exec(sb["id"], "env | grep -c -E '^(GH_TOKEN|GITHUB_TOKEN)=' ; "
                               "grep -c -E 'gh[pousr]_|github_pat_' ~/.git-credentials 2>/dev/null || true", cwd=repo)
    if any(x.strip() not in ("", "0") for x in leaks["stdout"].splitlines()):
        raise Fail(f"a GitHub token is visible in the sandbox: {leaks['stdout']!r}")
    branch = f"hangar/accept-{state.run}"
    state.add("branches", {"repo": opts.gateway_repo, "branch": branch})
    api.must(sb["id"], "git commit -q --allow-empty -m 'hangar acceptance: gateway push'", cwd=repo)
    t = time.monotonic()
    ok = api.exec(sb["id"], f"git push -q origin HEAD:refs/heads/{branch} 2>&1", cwd=repo, timeout=180)
    push_secs = time.monotonic() - t
    if ok["exit_code"] != 0:
        raise Fail(f"push to {branch} failed: {ok['stdout'][-400:]}")
    say(f"pushed {branch} in {push_secs:.1f}s")
    outside = f"accept-{state.run}-outside"
    state.add("branches", {"repo": opts.gateway_repo, "branch": outside})
    no = api.exec(sb["id"], f"git push origin HEAD:refs/heads/{outside} 2>&1", cwd=repo, timeout=180)
    why = [ln.strip() for ln in no["stdout"].splitlines() if "(hangar: " in ln] or \
        no["stdout"].strip().splitlines()[-1:]
    say(f"push to {outside} (outside the grant): exit {no['exit_code']}: {why[0] if why else ''}")
    if no["exit_code"] == 0:
        raise Fail(f"the gateway let the sandbox push to {outside}, outside its grant (HANGAR_GATEWAY_PUSH_REFS?)")
    if "(hangar: " not in no["stdout"]:
        raise Fail(f"the push to {outside} failed, but not with the gateway's refusal: {no['stdout'][-300:]}")
    return (f"push to {branch} ok ({push_secs:.1f}s), push outside the grant refused, no GitHub token in the "
            "sandbox")


def check_backup(api: Api, state: State, opts: argparse.Namespace) -> str:
    t = time.monotonic()
    r = api.call("POST", "/api/admin/backup")
    secs = time.monotonic() - t
    listed = api.call("GET", "/api/admin/backups")
    names = [b["name"] for b in listed["backups"]]
    say(f"backed up {r['db_bytes']} bytes as {r['name']} in {r['where']} ({secs:.1f}s); {len(names)} kept, "
        + (f"every {listed['every_hours']:g} h" if listed["every_hours"] else "no schedule (HANGAR_BACKUP_HOURS)"))
    if r["name"] not in names:
        raise Fail(f"{r['name']} isn't in the list of backups")
    # `where` is for people ("GCS bucket gs://…", or a directory): the bucket is named inside it, not at its start
    if state.get("provider") == "kubernetes" and "gs://" not in str(listed["where"]):
        raise Fail(f"backups go to {listed['where']}, the control plane's own disk: point HANGAR_SESSION_STORE at the "
                   "bucket, or snapshot the disk (docs/gke.md §7)")
    return f"{r['name']} in {listed['where']}"


def check_metrics(api: Api, state: State, opts: argparse.Namespace) -> str:
    r = api.http.get("/metrics")
    if r.status_code == 404:
        raise Fail("/metrics is off (HANGAR_METRICS_ENABLED=false)")
    if r.status_code != 200:
        raise Fail(f"GET /metrics: HTTP {r.status_code}")
    # declared families (a labelled gauge with no samples yet, like sessions on an empty hangar, has only its TYPE)
    names = {ln.split()[2] for ln in r.text.splitlines() if ln.startswith("# TYPE ")}
    want = {"hangar_sandboxes", "hangar_sessions", "hangar_sandbox_exec_seconds", "hangar_http_request_seconds"}
    if missing := want - names:
        raise Fail(f"missing series: {', '.join(sorted(missing))}")
    token, _ = api.scopes_token(state, "metrics", ["metrics"])
    hdr = {"Authorization": f"Bearer {token}"}
    scoped, other = api.http.get("/metrics", headers=hdr).status_code, api.http.get("/api/sessions", headers=hdr)
    say(f"{len(names)} series; a `metrics` token: /metrics {scoped}, /api/sessions {other.status_code}")
    if scoped != 200 or other.status_code != 403:
        raise Fail("a `metrics`-scoped token should read /metrics and nothing else")
    return f"{len(names)} series; the metrics scope works and is confined"


# ---------------------------------------------------------------------------- --- ops: power and warm starts
def check_power(api: Api, state: State, opts: argparse.Namespace) -> str:
    """Hangar answers with the sandbox pools off (the control plane stays up): what it reports about them, and while
    they are off, a create refused at once with when they're back instead of a wait for a node that won't come."""
    r = api.http.get("/api/admin/power")
    if r.status_code == 404:
        raise Skip("this hangar has no power state (it predates the always-on control plane)")
    if r.status_code != 200:
        raise Fail(f"GET /api/admin/power: HTTP {r.status_code}")
    p = r.json()["power"]
    state.set("data:power", {"state": p["state"], "when": p.get("when"), "last_up": p.get("last_up")})
    if p["state"] == "up":
        last = p.get("last_up") or {}
        pre = last.get("prepull") or {}
        warm = ", ".join(f"{k}={v}" for k, v in sorted((p.get("warm_pools") or {}).items()))
        say("sandboxes on" + (f"; last power-up pulled {pre['images']} images onto {pre.get('nodes', '?')} node(s) in "
                              f"{pre.get('seconds')} s" if pre.get("images") is not None else "")
            + (f"; warm pools {warm}" if warm else ""))
        return "sandboxes on" + (" (the recorded down state is stale)" if p.get("stale") else "")
    t0 = time.perf_counter()
    rr = api.http.post("/api/sandboxes", json={"environment": opts.env, "name": f"accept-{state.run}-power",
                                               "ttl_minutes": 30, "labels": {LABEL: state.run}})
    secs = time.perf_counter() - t0
    if rr.status_code == 200:
        for sb in rr.json():
            if "id" in sb:
                state.add("sandboxes", sb["id"])
        raise Fail(f"sandboxes are {p['state']}, yet a create was accepted")
    msg = (rr.json() if "json" in rr.headers.get("content-type", "") else {}).get("error", rr.text)
    say(f"create refused in {secs * 1000:.0f} ms (HTTP {rr.status_code}): {msg}")
    if rr.status_code != 503 or secs > 10:
        raise Fail(f"a create with the pools {p['state']} should be refused at once with HTTP 503 "
                   f"(got {rr.status_code} after {secs:.1f}s)")
    return f"sandboxes {p['state']} until {p.get('when') or '?'}; a create was refused in {secs * 1000:.0f} ms"


def check_warmstart(api: Api, state: State, opts: argparse.Namespace) -> str:
    """Create latency from a warm pod against a cold start: two creates back to back on --warm-env (an environment
    with a warm pool). The first claims the warm pod; the second finds the pool empty (it refills in the background)
    and starts cold. Each create's time is to `running`, as a client waits for it."""
    runs = []
    for i in (1, 2):
        t0 = time.monotonic()
        sb = api.call("POST", "/api/sandboxes", json={
            "environment": opts.warm_env, "name": f"accept-{state.run}-warm{i}", "ttl_minutes": 30,
            "labels": {LABEL: state.run}})[0]
        if "id" not in sb:
            raise Fail(sb.get("error") or str(sb))
        state.add("sandboxes", sb["id"])
        _running(api, sb["id"], opts.boot_timeout, capacity_skip=True)
        secs = time.monotonic() - t0
        warm = bool((api.call("GET", f"/api/sandboxes/{sb['id']}").get("info") or {}).get("warm"))
        runs.append({"path": "warm" if warm else "cold", "seconds": round(secs, 2)})
        say(f"create {i}: {secs:.1f}s ({runs[-1]['path']})")
    state.set("data:warmstart", {"environment": opts.warm_env, "creates": runs})
    by = {r["path"]: r["seconds"] for r in reversed(runs)}
    if "warm" not in by:
        return (f"{opts.warm_env}: no warm pod was ready (is warm_pool set?): cold {runs[0]['seconds']:.1f}s and "
                f"{runs[1]['seconds']:.1f}s")
    return f"{opts.warm_env}: warm {by['warm']:.1f}s" + (f", cold {by['cold']:.1f}s" if "cold" in by else
                                                          " (both warm)")


def tools() -> int:
    """The server's MCP version and tool names as JSON (deploy.yml diffs them across a deploy); 2 if unreachable."""
    async def listed(url: str, token: str) -> dict[str, Any]:
        from mcp import Client
        from mcp.client.streamable_http import streamable_http_client
        from mcp.shared._httpx_utils import create_mcp_http_client

        http = create_mcp_http_client(headers={"Authorization": f"Bearer {token}"})
        async with http, Client(streamable_http_client(f"{url}/mcp", http_client=http)) as c:
            names = sorted(t.name for t in (await c.list_tools()).tools)
            return {"version": c.server_info.version, "tools": names}

    try:
        url, token = target()
        out = asyncio.run(asyncio.wait_for(listed(url, token), 60))
    except Exception as e:  # noqa: BLE001 - an old or stopped server: the caller records "unreachable"
        print(f"can't list the tools: {type(e).__name__}: {e}", file=sys.stderr)
        return 2
    print(json.dumps(out))
    return 0


# ---------------------------------------------------------------------------- cleanup
def drop_session(api: Api, name: str, wait: float = 600) -> None:
    """Interrupt, suspend and delete a session (gone already is fine)."""
    r = api.http.get(f"/api/sessions/{name}")
    if r.status_code == 404:
        return
    s = r.json()
    if s["busy"] or s["state"] == "running":
        with contextlib.suppress(Fail):
            api.call("POST", f"/api/sessions/{name}/interrupt")
        api.settle(name, min(wait, 300))
    if s.get("sandbox_id") or api.session(name).get("sandbox_id"):
        api.call("POST", f"/api/sessions/{name}/suspend")
        api.settle(name, wait, want_sandbox=False)
    api.call("DELETE", f"/api/sessions/{name}")


def destroy_sandbox(api: Api, sid: str) -> None:
    r = api.http.get(f"/api/sandboxes/{sid}")
    if r.status_code == 200 and r.json()["state"] not in ("stopped", "error"):
        api.call("DELETE", f"/api/sandboxes/{sid}")


def release(api: Api, state: State, before: dict[str, list[str]]) -> list[str]:
    """--- #294: when a check ends, pass or fail, the sessions and sandboxes it made go, so the next check has the
    capacity (on a one-node pool, scale-smoke's leftovers starved prepull and warmstart until the run's cleanup).
    The run's shared sandbox and session (`sandbox`, `session`: later checks use them) stay. Returns what's left,
    which the run's cleanup tries again."""
    keep = {state.get("sandbox"), state.get("session")}
    left = []
    for name in [n for n in state.get("sessions", []) if n not in before["sessions"] and n not in keep]:
        try:
            drop_session(api, name, wait=300)
        except Exception as e:  # noqa: BLE001 - the run's cleanup tries again
            left.append(f"session {name}")
            say(f"could not release session {name} yet: {e}")
    for sid in [x for x in state.get("sandboxes", []) if x not in before["sandboxes"] and x not in keep]:
        try:
            destroy_sandbox(api, sid)
        except Exception as e:  # noqa: BLE001
            left.append(f"sandbox {sid}")
            say(f"could not release sandbox {sid} yet: {e}")
    return left


def cleanup(api: Api, state: State, opts: argparse.Namespace) -> str:
    """Remove everything this run created. Never fails part-way: each step reports and the next one runs."""
    left: list[str] = []

    def attempt(what: str, fn: Callable[[], Any]) -> None:
        try:
            fn()
            say(f"removed {what}")
        except Exception as e:  # noqa: BLE001 - keep cleaning up
            left.append(what)
            say(f"could not remove {what}: {e}")

    team_checks.cleanup_first(api, state, attempt, opts)  # --- acceptance: pools on, jobs, PRs, requests
    for name in reversed(state.get("sessions", [])):
        attempt(f"session {name}", lambda name=name: drop_session(api, name))
    swept = [sb["id"] for sb in api.call("GET", "/api/sandboxes", params={"label": f"{LABEL}={state.run}"})]
    for sid in dict.fromkeys([*state.get("sandboxes", []), *swept]):
        attempt(f"sandbox {sid}", lambda sid=sid: destroy_sandbox(api, sid))
    for tid in state.get("tokens", []):
        attempt(f"token {tid}", lambda tid=tid: api.call("DELETE", f"/api/tokens/{tid}"))
    for b in state.get("branches", []):
        def branch(b: dict[str, str] = b) -> None:
            if not shutil.which("gh"):
                raise Fail("no gh CLI: delete it in GitHub, or with "
                           f"`gh api -X DELETE repos/{b['repo']}/git/refs/heads/{b['branch']}`")
            r = subprocess.run(["gh", "api", "-X", "DELETE", f"repos/{b['repo']}/git/refs/heads/{b['branch']}"],
                               capture_output=True, text=True, timeout=60)
            if r.returncode != 0 and "Reference does not exist" not in r.stdout + r.stderr:
                raise Fail((r.stderr or r.stdout).strip()[-200:])
        attempt(f"branch {b['repo']}:{b['branch']}", branch)
    team_checks.cleanup_last(api, state, attempt, opts)  # --- acceptance: people, policy, the fixture's file
    if left:
        raise Fail("left behind: " + "; ".join(left))
    return "nothing left behind"


CHECKS: dict[str, Callable[[Api, State, argparse.Namespace], str]] = {
    "doctor": check_doctor, "k8s": check_k8s, "sandbox": check_sandbox, "exec": check_exec, "stream": check_stream,
    "files": check_files, "mcp": check_mcp,
    "isolation": check_isolation, "session": check_session, "model": check_model, "steer": check_steer,
    "gateway": check_gateway, "backup": check_backup, "metrics": check_metrics,
    "power": check_power, "warmstart": check_warmstart,  # --- ops
    "cleanup": cleanup,
}


def whoami() -> int:
    """`url as actor` for gke.sh's header, or why the hangar can't be used (exit 2)."""
    try:
        api = Api(*target())
        who = api.call("GET", "/api/me")["actor"]
    except (Fail, httpx.HTTPError) as e:
        print(f"can't use the hangar: {e}", file=sys.stderr)
        return 2
    print(f"{api.url} as {who}")
    return 0


def main(argv: list[str]) -> int:
    if argv[:1] == ["whoami"]:
        return whoami()
    if argv[:1] == ["tools"]:  # --- ops
        return tools()
    if argv[:1] == ["doctor-failing"]:  # --- ops
        return doctor_failing()
    if argv[:1] == ["report"]:  # --- acceptance
        return report(argv[1:])
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("check", choices=sorted(CHECKS))
    p.add_argument("--state", type=Path, required=True)
    p.add_argument("--summary", type=Path)
    p.add_argument("--env", default="minimal", help="environment for plain sandboxes")
    p.add_argument("--session-env", default="claude-python", help="environment for sessions (needs an agent harness)")
    p.add_argument("--repo", default=None, help="the sessions' repo (default: --gateway-repo's, else a public one)")
    p.add_argument("--gateway-repo", default=None, help="OWNER/REPO on github.com the gateway may push to")
    p.add_argument("--model", default=None)
    p.add_argument("--namespace", default="hangar")
    p.add_argument("--sandbox-namespace", default="hangar-sandboxes")
    p.add_argument("--no-kubectl", action="store_true")
    p.add_argument("--exec-calls", type=int, default=20)
    p.add_argument("--expect-envd", action="store_true")
    p.add_argument("--envd-wait", type=float, default=90.0)
    p.add_argument("--file-kb", type=int, default=1024)
    p.add_argument("--internet-probe", default="github.com:443", help="host:port a sandbox must reach")
    p.add_argument("--boot-timeout", type=float, default=1200.0)
    p.add_argument("--mcp-env", default="python", help="a multi-harness environment for the mcp check")
    p.add_argument("--mcp-drop-after", type=float, default=5.0, help="when the mcp check's impatient client leaves")
    p.add_argument("--turn-timeout", type=float, default=900.0)
    p.add_argument("--warm-env", default="claude-python", help="an environment with a warm pool (warmstart check)")
    p.add_argument("--record", type=Path, default=None, help="append each check's result to this JSON-lines file")
    p.add_argument("--doctor-baseline", type=Path, default=None,
                   help="JSON list of doctor checks already failing before a deploy: reported, not failed on")
    p.add_argument("--with-model", action="store_true", help="run the checks that need real agent turns")
    p.add_argument("--as", dest="as_person", default=None, metavar="PERSON",
                   help="create the sessions and jobs as this (admin) person, so their own login reaches the turns")
    p.add_argument("--keep", action="store_true",
                   help="leave what each check made until the run's cleanup (by default it goes when the check ends)")
    team_checks.add_args(p)  # --- acceptance
    opts = p.parse_args(argv)
    if opts.gateway_repo and not re.fullmatch(r"[\w.-]+/[\w.-]+", opts.gateway_repo):
        p.error("--gateway-repo is OWNER/REPO")
    team_checks.check_args(p, opts)
    opts.repo = opts.repo or (f"https://github.com/{opts.gateway_repo}.git" if opts.gateway_repo
                              else "https://github.com/octocat/Hello-World.git")
    state = State(opts.state)
    state.save()
    t0 = time.monotonic()
    code, summary = PASS, ""
    before = {k: list(state.get(k, [])) for k in ("sessions", "sandboxes")}
    api: Api | None = None
    try:
        api = Api(*target())
        summary = CHECKS[opts.check](api, state, opts)
    except Skip as e:
        code, summary = SKIP, str(e)
    except Fail as e:
        code, summary = FAIL, str(e)
    except (httpx.HTTPError, subprocess.TimeoutExpired, OSError) as e:
        code, summary = FAIL, f"{type(e).__name__}: {e}"
    except Exception as e:  # noqa: BLE001 - one check's bug must not stop the others or the cleanup
        code, summary = FAIL, f"unexpected {type(e).__name__}: {e}"
    if api is not None and opts.check != "cleanup" and not opts.keep:  # --- #294: free the capacity now
        try:
            if left := release(api, state, before):
                summary += f" (left for the run's cleanup: {', '.join(left)})"
        except Exception as e:  # noqa: BLE001 - never turns the check's result into a crash
            say(f"could not release what {opts.check} made: {type(e).__name__}: {e}")
    if opts.summary:
        opts.summary.write_text(f"{time.monotonic() - t0:.1f}\n{' '.join(summary.split())}\n")
    else:
        print(summary)
    if opts.record and opts.check != "cleanup":  # --- ops: deploy.yml's run summary reads these
        with opts.record.open("a") as f:
            f.write(json.dumps({"check": opts.check, "status": {PASS: "pass", FAIL: "fail", SKIP: "skip"}[code],
                                "seconds": round(time.monotonic() - t0, 1), "summary": " ".join(summary.split()),
                                "data": state.get(f"data:{opts.check}")}) + "\n")
    return code


# ---------------------------------------------------------------------------- --- acceptance: the run's report
def report(argv: list[str]) -> int:
    """The summary table and the JSON report of a run, from the checks' result lines (--results). Exit status: 1 when
    a check the gate counts failed (--gate core: only the --core checks, the ones deploy.yml rolls back for; all:
    any), else 0."""
    p = argparse.ArgumentParser(prog="hangar_accept.py report")
    p.add_argument("--results", type=Path, required=True, help="the JSON lines --record wrote")
    p.add_argument("--out", type=Path, default=None, help="where to write the JSON report")
    p.add_argument("--gate", choices=["all", "core"], default="all")
    p.add_argument("--core", default="", help="the core checks, comma or space separated")
    p.add_argument("--target", default="")
    p.add_argument("--cleanup", default="", help="how the cleanup went: ok, incomplete or kept")
    p.add_argument("--state", type=Path, default=None, help="the run's state file: what its agent turns cost")
    o = p.parse_args(argv)
    spend = (State(o.state).get("spend") if o.state and o.state.is_file() else None) or None
    core = {c for c in re.split(r"[,\s]+", o.core) if c}
    rows: list[dict[str, Any]] = []
    with contextlib.suppress(OSError):
        for line in o.results.read_text().splitlines():
            with contextlib.suppress(ValueError):
                r = json.loads(line)
                if isinstance(r, dict) and r.get("check"):
                    rows.append(r)
    out = []
    for r in rows:
        data = r.get("data") if isinstance(r.get("data"), dict) else {}
        out.append({"check": r["check"], "status": r.get("status"), "core": r["check"] in core,
                    "seconds": r.get("seconds"), "summary": r.get("summary", ""), "timings": data.get("timings") or {},
                    "data": data or None})
    failed = [r["check"] for r in out if r["status"] == "fail"]
    core_failed = [c for c in failed if c in core]
    gated = core_failed if o.gate == "core" else failed
    print("== summary" + (" (the exit status counts the core checks only)" if o.gate == "core" else ""))
    for r in out:
        print(f"{str(r['status']).upper():<4}  {r['check']:<11} {float(r['seconds'] or 0):7.1f}s  "
              f"{'core' if r['core'] else '    '}  {r['summary']}")
    doc = {"version": 1, "target": o.target, "finished_at": time.time(), "gate": o.gate, "core": sorted(core),
           "cleanup": o.cleanup or None, "checks": out, "failed": failed, "core_failed": core_failed,
           "spend": spend, "exit": 1 if gated else 0}
    if o.out:
        o.out.write_text(json.dumps(doc, indent=1) + "\n")
    print()
    if gated:
        print(f"{len(gated)} {'core ' if o.gate == 'core' else ''}check(s) failed: {', '.join(gated)}")
    elif failed:
        print(f"{len(failed)} check(s) failed, none of them core: {', '.join(failed)}")
    else:
        print("no failures")
    if spend and (spend.get("usd") or spend.get("unpriced") or spend.get("jobs")):  # --with-model's, each --max-usd

        def n(k: int, what: str) -> str:
            return f"{k} {what}{'' if k == 1 else 's'}"

        print(f"model spend: ${spend['usd']:.2f} ({n(spend['jobs'], 'job')}, {n(spend['sessions'], 'session')}"
              + (f"; {n(spend['unpriced'], 'turn')} without a price" if spend.get("unpriced") else "") + ")")
    if o.cleanup and o.cleanup != "ok":
        print(f"cleanup: {o.cleanup}")
    if o.out:
        print(f"report: {o.out}")
    return doc["exit"]


# --- acceptance: the team release's checks (team_checks.py) build on this module. Run as a script this module is
# __main__: registering it under its own name first means team_checks shares its classes (Fail, Skip, Api, State).
sys.modules.setdefault("hangar_accept", sys.modules[__name__])
import team_checks  # noqa: E402

CHECKS.update(team_checks.CHECKS)

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
