"""The team release's acceptance checks (scripts/acceptance/team_checks.py, docs/acceptance.md) without a cluster.

What can run against a real hangar does, on the fake provider: the scale smoke test (members over MCP, the per-person
cap, isolation, nothing left behind), Jira intake (a signed delivery, the linked person, a replay), the power cycle
(pools down: creates refused; up again; cleanup brings them back if a run dies half way), and gke.sh's core gate and
JSON report. The checks that need Docker, gVisor or GitHub are tested by their parts: the RFB client against hangar's
own view-only filter, the screenshot and egress verdicts, the synthetic token against hangar's secret scanner, the
fixture repo's commits and restore on a local repo, and the secrets and landing flows against stand-in APIs. The
Docker run is tests/test_e2e_acceptance_docker.py.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import struct
import subprocess
import sys
import time
import zlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from tests.conftest import ENVS, FakeProvider
from tests.test_exchange import world  # noqa: F401 - fixture: the scripted xenv environment
from tests.test_mcp_modern import AppThread

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "acceptance" / "gke.sh"
TOKEN = "accept-team-admin-token"

sys.path.insert(0, str(ROOT / "scripts" / "acceptance"))
try:
    import hangar_accept as ha
    import team_checks as tc
finally:
    sys.path.pop(0)


def _opts(**kw: Any) -> argparse.Namespace:
    p = argparse.ArgumentParser()
    tc.add_args(p)
    o = p.parse_args([])
    base = {"env": "xenv", "session_env": "xenv", "warm_env": "xenv", "repo": None, "model": None, "boot_timeout": 60,
            "turn_timeout": 60, "with_model": False, "no_kubectl": True}
    for k, v in {**base, **kw}.items():
        setattr(o, k, v)
    return o


def settings(**kw: Any):
    """Settings of a test's own for its `hangar_with`."""
    return pytest.mark.parametrize("hangar_with", [kw], indirect=True, ids=[",".join(kw)])


@pytest.fixture
def hangar_with(tmp_path, world, monkeypatch, request):  # noqa: F811
    """A real hangar on its own server, with the fake provider standing in for Docker (and the test's settings)."""
    from hangar.app import create_app
    from hangar.config import Settings

    s = Settings(data_dir=tmp_path / "data", env_dirs=[world["envs"], ENVS], reaper_interval_seconds=3600,
                 admin_token=TOKEN, session_poll_seconds=0.2, **getattr(request, "param", {}))
    s.snapshot_dir.mkdir(parents=True, exist_ok=True)
    app = create_app(s)
    srv = AppThread(app)
    h = app.state.hangar
    h.fleet.providers["docker"] = FakeProvider()
    h.fleet.provider_errors.pop("docker", None)
    monkeypatch.setattr(ha, "POLL_SECONDS", 0.2)
    try:
        yield SimpleNamespace(h=h, url=f"http://127.0.0.1:{srv.port}", settings=s,
                              api=ha.Api(f"http://127.0.0.1:{srv.port}", TOKEN), world=world)
    finally:
        srv.stop()


# ---------------------------------------------------------------------------- gke.sh: core gate and the report
def _script_env(tmp_path: Path, url: str, **extra: str) -> dict[str, str]:
    return {**os.environ, "HANGAR_URL": url, "HANGAR_TOKEN": TOKEN, "HANGAR_CLI_CONFIG": str(tmp_path / "none.json"),
            "HANGAR_ACCEPT_PYTHON": sys.executable, "TMPDIR": str(tmp_path), **extra}


def test_core_checks_are_listed_and_run(tmp_path):
    """gke.sh's CHECKS run every check the helper has, each with a `check` line, and CORE is a subset of them."""
    text = SCRIPT.read_text()
    checks = re.search(r'^CHECKS="([^"]+)"', text, re.M | re.S).group(1).split()
    core = re.search(r'^CORE="([^"]+)"', text, re.M).group(1).split()
    assert set(checks) == set(ha.CHECKS) - {"cleanup"}
    assert set(core) <= set(checks) and "doctor" in core and "computer" not in core
    for c in checks:
        assert re.search(rf"^check {re.escape(c)}\s", text, re.M), c


def test_every_option_parses(tmp_path):
    """Each option gke.sh passes on, and every one its --help names, gets past its parser (to the login check)."""
    text = SCRIPT.read_text()
    values = re.search(r'^VALUE_OPTS="([^"]+)"', text, re.M | re.S).group(1).split()
    flags = re.search(r'^FLAG_OPTS="([^"]+)"', text, re.M).group(1).split()
    helped = set(re.findall(r"^  (--[a-z-]+)", subprocess.run(["bash", str(SCRIPT), "--help"], capture_output=True,
                                                              text=True).stdout, re.M))
    assert helped <= {*values, *flags, "--record", "--report", "--gate", "--no-expect-envd", "--no-kubectl",
                      "--only", "--keep"}, helped
    env = {k: v for k, v in os.environ.items() if k not in ("HANGAR_URL", "HANGAR_TOKEN")}
    env.update(HANGAR_CLI_CONFIG=str(tmp_path / "none.json"), HANGAR_ACCEPT_PYTHON=sys.executable,
               TMPDIR=str(tmp_path))
    for opt in [*values, *flags, "--gate"]:
        args = [opt] if opt in flags else [opt, "core" if opt == "--gate" else "x"]
        r = subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True, env=env, timeout=60)
        assert "unknown option" not in r.stderr and "hangar login" in r.stderr, (opt, r.stderr)
    r = subprocess.run(["bash", str(SCRIPT), "--gate", "some"], capture_output=True, text=True, env=env)
    assert r.returncode == 2 and "--gate is all or core" in r.stderr


def test_report_gates_on_core_checks(tmp_path, capsys):
    """--gate core: a failed non-core check is reported, not counted (deploy.yml rolls back only for core ones)."""
    res = tmp_path / "r.jsonl"
    res.write_text("\n".join(json.dumps(r) for r in [
        {"check": "doctor", "status": "pass", "seconds": 1.0, "summary": "ok", "data": None},
        {"check": "computer", "status": "fail", "seconds": 9.5, "summary": "no Chromium",
         "data": {"timings": {"create": 4.2}}},
        {"check": "landing", "status": "skip", "seconds": 0.1, "summary": "needs --test-repo", "data": None},
    ]) + "\nnot json\n")
    out = tmp_path / "report.json"
    core = "doctor sandbox exec"
    assert ha.report(["--results", str(res), "--out", str(out), "--gate", "core", "--core", core]) == 0
    doc = json.loads(out.read_text())
    assert doc["failed"] == ["computer"] and doc["core_failed"] == [] and doc["exit"] == 0
    assert [c["core"] for c in doc["checks"]] == [True, False, False]
    assert doc["checks"][1]["timings"] == {"create": 4.2}
    printed = capsys.readouterr().out
    assert re.search(r"^FAIL\s+computer\s+9\.5s\s+no Chromium", printed, re.M)
    assert "none of them core: computer" in printed
    assert ha.report(["--results", str(res), "--gate", "all", "--core", core]) == 1
    res.write_text(json.dumps({"check": "exec", "status": "fail", "seconds": 2, "summary": "x"}) + "\n")
    assert ha.report(["--results", str(res), "--gate", "core", "--core", core]) == 1


def test_gke_sh_reports_and_gates_against_a_real_hangar(tmp_path, hangar_with):
    """gke.sh against a hangar: a non-core check that fails (the computer, which the fake provider has none of)
    fails the run with the default gate, not with --gate core; the JSON report has both, and nothing is left."""
    from hangar.models import ApiToken, Sandbox

    env = _script_env(tmp_path, hangar_with.url)
    base = ["bash", str(SCRIPT), "--no-kubectl", "--env", "xenv", "--session-env", "xenv", "--only", "metrics,computer"]
    r = subprocess.run([*base, "--gate", "core", "--report", str(tmp_path / "core.json")], env=env,
                       capture_output=True, text=True, timeout=300, cwd=tmp_path)
    out = r.stdout + r.stderr
    assert r.returncode == 0, out
    assert re.search(r"^FAIL\s+computer\s", out, re.M) and re.search(r"^PASS\s+metrics\s.*core", out, re.M), out
    doc = json.loads((tmp_path / "core.json").read_text())
    assert doc["gate"] == "core" and doc["failed"] == ["computer"] and doc["exit"] == 0 and doc["cleanup"] == "ok"
    assert "create" in {c["check"]: c for c in doc["checks"]}["computer"]["timings"]
    r = subprocess.run(base, env=env, capture_output=True, text=True, timeout=300, cwd=tmp_path)
    assert r.returncode == 1, r.stdout + r.stderr
    assert json.loads((tmp_path / "hangar-accept-report.json").read_text())["exit"] == 1
    h = hangar_with.h
    assert not [s for s in h.store.list(Sandbox) if "acceptance" in s.labels and s.state.value != "stopped"]
    assert not [t for t in h.store.list(ApiToken) if t.name.startswith("accept-")]
    assert "accept-computer" in h.fleet.catalog.names()  # the test environment stays, for the next run
    assert not list(tmp_path.glob("hangar-accept.*"))


# ---------------------------------------------------------------------------- scale smoke, on a real hangar
@settings(person_max_sessions=3)
def test_scale_smoke_caps_isolates_and_leaves_nothing(tmp_path, hangar_with):
    from hangar.models import ApiToken, Person, Sandbox, SessionState

    h, api = hangar_with.h, hangar_with.api
    state = ha.State(tmp_path / "state.json")
    opts = _opts(scale_sessions=5, scale_people=3)
    summary = tc.check_scale_smoke(api, state, opts)
    assert "5 sessions/3 people" in summary and "session 4 refused at the cap" in summary, summary
    data = state.get("data:scale-smoke")
    assert {"create_p50", "create_p95", "teardown"} <= set(data["timings"]), data
    people = state.get("people")
    assert len(people) == 3 and all(h.store.get(Person, p) for p in people)
    assert not [s for s in h.sessions.list() if s.name.startswith(f"accept-{state.run}")
                and s.state in (SessionState.IDLE, SessionState.STARTING)]
    assert "cleanup" not in summary
    ha.cleanup(api, state, opts)
    assert not [p for p in people if h.store.get(Person, p)]
    assert not [t for t in h.store.list(ApiToken) if t.name.startswith("accept-")]
    assert not [s for s in h.store.list(Sandbox) if s.person in people and s.state.value != "stopped"]


@pytest.mark.parametrize("total,people,cap,plan", [(20, 3, 15, [15, 3, 2]), (20, 3, 0, [8, 6, 6]),
                                                    (20, 3, 30, [18, 1, 1]), (5, 3, 3, [3, 1, 1])])
def test_split_sessions(total, people, cap, plan):
    assert tc.split_sessions(total, people, cap) == plan and sum(plan) == total


def test_job_fairness_from_intervals():
    def job(person, start, end):
        return {"person": person, "events": [{"ts": start - 1, "state": "queued"},
                                            {"ts": start, "state": "provisioning"}, {"ts": end, "state": "succeeded"}]}

    fair = [job("a", 0, 10), job("a", 0, 10), job("a", 10, 20), job("b", 1, 5)]
    assert tc.job_intervals_ok(fair, cap=2) == []
    assert "3 jobs running at once" in tc.job_intervals_ok([*fair, job("a", 2, 9)], cap=2)[0]
    late = [job("a", 0, 10), job("a", 10, 20), job("b", 25, 30)]
    assert "waited behind their batch" in tc.job_intervals_ok(late, cap=2)[0]


# ---------------------------------------------------------------------------- intake, on a real hangar
SECRET = "jira-acceptance-webhook-secret-0123"
JIRA = {"url": "https://jira.invalid", "email": "hangar@acme.invalid", "token_credential": "jira-api",
        "webhook_secret_credential": "jira-hook", "projects": {"ACC": {"repo": "acme/widgets"}},
        "trigger": {"label": "hangar"}}


@settings(jira=JIRA, github_repos={"acme/widgets": "xenv"})
def test_intake_makes_a_job_for_the_linked_person(tmp_path, hangar_with, monkeypatch):
    from hangar.models import CredentialKind, Job, JobState, Person

    h, api = hangar_with.h, hangar_with.api
    h.broker.put("jira-api", "webhook", CredentialKind.GENERIC, "jira-api-token")
    h.broker.put("jira-hook", "webhook", CredentialKind.GENERIC, SECRET)
    state = ha.State(tmp_path / "state.json")
    opts = _opts()
    with pytest.raises(ha.Skip, match="HANGAR_ACCEPT_JIRA_WEBHOOK_SECRET"):
        tc.check_intake(api, state, opts)
    monkeypatch.setenv("HANGAR_ACCEPT_JIRA_WEBHOOK_SECRET", SECRET)
    summary = tc.check_intake(api, state, opts)
    assert summary.startswith("ACC: a job for accept-") and "replay dropped" in summary, summary
    (jid,) = state.get("jobs")
    job = h.store.get(Job, jid)
    assert job.person == f"accept-{state.run}-jira" and job.source == "jira"
    assert job.labels["jira.project"] == "ACC"
    assert job.state in (JobState.CANCELLED, JobState.FAILED) or job.cancel_requested  # it may race provisioning
    ha.cleanup(api, state, opts)
    assert h.store.get(Person, f"accept-{state.run}-jira") is None


def test_intake_skips_without_jira(tmp_path, hangar_with):
    with pytest.raises(ha.Skip, match="HANGAR_JIRA"):
        tc.check_intake(hangar_with.api, ha.State(tmp_path / "s.json"), _opts())


def test_jira_payload_triggers_and_verifies():
    from hangar import jira
    from hangar.github import verify_signature

    cfg = jira.JiraConfig.model_validate({**JIRA, "trigger": {"status": "Ready for agent"}})
    p = tc.jira_payload("r1", "ACC", {"status": "Ready for agent"}, "acct-1")
    assert jira.JiraFrontDoor._triggered(cfg, p["webhookEvent"], p["issue"], None)
    assert jira._KEY.match(p["issue"]["key"]) and jira._account(p["user"], True) == "acct-1"
    assert abs(jira._timestamp(p["timestamp"]) - time.time()) < 5
    body = json.dumps(p).encode()
    assert verify_signature(SECRET, body, tc.jira_signature(SECRET, body))


# ---------------------------------------------------------------------------- power, on a real hangar
@settings(power_providers=["docker"])
def test_power_cycle_down_refused_up(tmp_path, hangar_with, monkeypatch):
    """--power-cycle with commands that don't tell hangar (power.sh without kubectl): the check tells it, a create
    while down is refused at once, and the pools are up at the end, timed."""
    monkeypatch.setattr(tc, "POWER_POLL", 0.1)
    api = hangar_with.api
    state = ha.State(tmp_path / "state.json")
    opts = _opts(power_cycle=True, power_down_cmd="true", power_up_cmd="true", power_timeout=30)
    summary = tc.check_power(api, state, opts)
    assert summary.startswith("cycled: down in") and "a create was refused" in summary, summary
    assert tc._power(api)["state"] == "up" and state.get("power_cycle")["up"] is True
    data = state.get("data:power")
    assert {"down", "up", "create_before", "create_after"} <= set(data["timings"])
    assert tc.check_power(api, state, _opts()) == "sandboxes on"  # without --power-cycle: the state only


@settings(power_providers=["docker"])
def test_cleanup_powers_the_pools_back_on(tmp_path, hangar_with, monkeypatch):
    """A run that died with the pools down (power-up failed) leaves them on after cleanup."""
    monkeypatch.setattr(tc, "POWER_POLL", 0.1)
    api = hangar_with.api
    state = ha.State(tmp_path / "state.json")
    opts = _opts(power_cycle=True, power_down_cmd="true", power_up_cmd="false", power_timeout=10)
    with pytest.raises(ha.Fail, match="`false` exited 1"):
        tc.check_power(api, state, opts)
    assert tc._power(api)["state"] == "down" and state.get("power_cycle")["up"] is False
    ha.cleanup(api, state, _opts(power_up_cmd="true", power_timeout=10))
    assert tc._power(api)["state"] == "up"


def test_power_cycle_waits_for_a_quiet_hangar(tmp_path, monkeypatch):
    """Others' running work makes --power-cycle skip, unless --power-cycle-anyway."""
    api = _FakeApi({("GET", "/api/admin/power"): {"power": {"state": "up"}},
                    ("GET", "/api/sessions"): [{"name": "dana-work", "busy": True, "state": "running"}],
                    ("GET", "/api/jobs"): []})
    monkeypatch.setattr(ha, "check_power", lambda a, s, o: (s.set("data:power", {"state": "up"}), "sandboxes on")[1])
    state = ha.State(tmp_path / "s.json")
    with pytest.raises(ha.Skip, match="1 running session"):
        tc.check_power(api, state, _opts(power_cycle=True))


# ---------------------------------------------------------------------------- the computer, by its parts
class _Vnc:
    """An RFB 3.8 server with no password, behind hangar's view-only filter when the viewer has no control, as
    hangar's relay puts it: what the viewer sends goes through `computer.RfbFilter` first."""

    def __init__(self, view_only: bool):
        from hangar.computer import RfbFilter

        self.filter = RfbFilter() if view_only else None
        self.out = [b"RFB 003.008\n"]
        self.buf, self.phase = b"", "version"
        self.pointer: tuple[int, int] | None = None
        self.keys: list[int] = []
        self.requests = 0

    def send(self, data: bytes) -> None:
        self.buf += self.filter.feed(data) if self.filter else data
        while self._step():
            pass

    def recv(self, timeout: float | None = None) -> bytes:
        if not self.out:
            raise TimeoutError("nothing to read")
        return self.out.pop(0)

    def _take(self, n: int) -> bytes | None:
        if len(self.buf) < n:
            return None
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def _step(self) -> bool:
        if self.phase == "version" and self._take(12):
            self.out.append(bytes([1, 1]))
            self.phase = "security"
        elif self.phase == "security" and self._take(1):
            self.out.append(struct.pack(">I", 0))
            self.phase = "init"
        elif self.phase == "init" and self._take(1):
            fmt = bytes([32, 24, 0, 1]) + struct.pack(">HHH", 255, 255, 255) + bytes([16, 8, 0, 0, 0, 0])
            self.out.append(struct.pack(">HH", 1280, 800) + fmt + struct.pack(">I", 6) + b"hangar")
            self.phase = "msgs"
        elif self.phase == "msgs" and self.buf:
            kind = self.buf[0]
            size = {5: 6, 4: 8, 3: 10}[kind]
            msg = self._take(size)
            if msg is None:
                return False
            if kind == 5:
                self.pointer = struct.unpack(">HH", msg[2:6])
            elif kind == 4:
                self.keys.append(struct.unpack(">I", msg[4:8])[0])
            else:
                self.requests += 1
                self.out.append(b"\x00\x00" + struct.pack(">H", 1) + struct.pack(">HHHHi", 0, 0, 1, 1, 0) + b"\0" * 4)
        else:
            return False
        return True


@pytest.mark.parametrize("view_only", [True, False])
def test_rfb_client_against_hangars_view_only_filter(view_only):
    """The computer check's RFB client speaks what hangar's filter parses (an unknown message would end the stream),
    and the filter drops its pointer and keys for a view-only viewer but lets the framebuffer request through."""
    vnc = _Vnc(view_only)
    rfb = tc.Rfb(vnc)
    assert rfb.handshake() == (1280, 800, "hangar") and rfb.bpp == 4
    rfb.pointer(653, 417, buttons=1)
    rfb.key(0x61)
    rfb.alive()
    assert vnc.requests == 1
    if view_only:
        assert vnc.pointer is None and vnc.keys == []
    else:
        assert vnc.pointer == (653, 417) and vnc.keys == [0x61, 0x61]


def _png(path: Path, w: int, h: int, pixel, truncate: bool = False) -> None:
    raw = b"".join(b"\x00" + b"".join(bytes(pixel(x, y)) for x in range(w)) for y in range(h))

    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xffffffff)

    data = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))
    path.write_bytes(data[:-20] if truncate else data)


def _png_info(path: Path) -> dict[str, Any]:
    out = subprocess.run([sys.executable, "-c", tc.PNG_INFO, str(path)], capture_output=True, text=True, check=True)
    return json.loads(out.stdout.split(" ", 1)[1])


def test_screenshot_verdicts(tmp_path):
    """The in-sandbox PNG reader and its verdict: a whole PNG of the display's size, and not blank when it should
    show a page."""
    page, blank, small, cut = (tmp_path / n for n in ("page.png", "blank.png", "small.png", "cut.png"))
    _png(page, 64, 40, lambda x, y: ((x * 4) % 256, (y * 6) % 256, (x * y) % 256))
    _png(blank, 64, 40, lambda x, y: (255, 255, 255))
    _png(small, 32, 40, lambda x, y: (x, y, 0))
    _png(cut, 64, 40, lambda x, y: (x, y, 0), truncate=True)
    assert tc.png_problems(_png_info(page), (64, 40), "page", content=True) == []
    assert "blank" in tc.png_problems(_png_info(blank), (64, 40), "page", content=True)[0]
    assert tc.png_problems(_png_info(blank), (64, 40), "display", content=False) == []
    assert "is 32x40, not 64x40" in tc.png_problems(_png_info(small), (64, 40), "page", content=False)[0]
    assert "isn't a complete PNG" in tc.png_problems(_png_info(cut), (64, 40), "page", content=False)[0]


# ---------------------------------------------------------------------------- egress, by its parts
GOOD = {"proxy": "hangar-gateway.hangar.svc", "proxy_resolves": ["10.8.0.12"], "allowed_connect": 200,
        "allowed_https": 200, "denied_connect": 403, "direct": "TimeoutError: timed out",
        "dns": "gaierror: [Errno -3] Temporary failure in name resolution", "resolvers": ["127.0.0.1"]}


@pytest.mark.parametrize("change,why", [
    ({}, None),
    ({"proxy": None}, "no HTTPS_PROXY"),
    ({"proxy_resolves": "gaierror: no"}, "hostAliases"),
    ({"allowed_connect": 403}, "answered 403 for the allowed"),
    ({"allowed_https": "URLError: x"}, "https://example.com/ through the proxy"),
    ({"denied_connect": 200}, "not 403"),
    ({"direct": "connected"}, "direct connection to 1.1.1.1:443 went through"),
    ({"dns": ["140.82.112.3"]}, "it has a resolver"),
])
def test_egress_verdicts(change, why):
    view = {"mode": "allowlist", "enforced": True}
    problems = tc.egress_problems(view, {**GOOD, **change}, _opts())
    assert (problems == []) if why is None else (len(problems) == 1 and why in problems[0]), problems
    assert "enforced False" in tc.egress_problems({"mode": "allowlist", "enforced": False}, GOOD, _opts())[0]


def test_egress_probe_runs_where_python3_is(tmp_path):
    """The probe is plain python3: with no proxy it reports the direct connection and the lookup it tried."""
    env = {k: v for k, v in os.environ.items() if k.lower() not in ("https_proxy", "http_proxy")}
    r = subprocess.run([sys.executable, "-c", tc.EGRESS_PROBE, "example.com", "www.iana.org", "127.0.0.1:9",
                        "localhost"], capture_output=True, text=True, env=env, timeout=60)
    out = json.loads(r.stdout.split("HANGAR-ACCEPT ", 1)[1])
    assert out["proxy"] is None and "allowed_connect" not in out
    assert out["direct"] != "connected" and isinstance(out["dns"], list)


# ---------------------------------------------------------------------------- secrets, by its parts
def test_synthetic_token_is_a_secret_only_once_joined():
    """The pieces join in the sandbox into what hangar's scanner calls a GitHub token; neither piece is one, and no
    token literal is in the checks' source (GitHub's push protection would refuse the commit)."""
    from hangar import secretscan

    head, tail = tc.synthetic_token()
    assert [k for _, _, k in secretscan.find(head + tail + "\n")] == ["GitHub token"]
    assert not secretscan.find(head) and not secretscan.find(tail)
    src = (ROOT / "scripts" / "acceptance" / "team_checks.py").read_text()
    assert not re.search(r"gh[pousr]_[A-Za-z0-9]{20}", src)


class _FakeApi(ha.Api):
    """hangar's REST API from a table, or a function of (method, path, body)."""

    def __init__(self, table: dict[tuple[str, str], Any] | None = None, fn=None):
        self.table, self.fn, self.calls = table or {}, fn, []

        def handler(req: httpx.Request) -> httpx.Response:
            body = json.loads(req.content) if req.content else None
            self.calls.append((req.method, req.url.path, body))
            key = (req.method, req.url.path)
            if self.fn is not None:
                got = self.fn(req.method, req.url.path, body, req)
                if got is not None:
                    return got if isinstance(got, httpx.Response) else httpx.Response(200, json=got)
            if key in self.table:
                return httpx.Response(200, json=self.table[key])
            return httpx.Response(404, json={"error": f"no {key}"})

        super().__init__("http://hangar.invalid", "t")
        self.http = httpx.Client(base_url="http://hangar.invalid", transport=httpx.MockTransport(handler))


def _github(routes: dict[tuple[str, str], Any], seen: list | None = None) -> httpx.MockTransport:
    def handler(req: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append((req.method, req.url.path, json.loads(req.content) if req.content else None))
        for (m, pattern), val in routes.items():
            if m == req.method and re.fullmatch(pattern, req.url.path):
                val = val(req) if callable(val) else val
                return val if isinstance(val, httpx.Response) else httpx.Response(200, json=val)
        return httpx.Response(404, json={"message": "Not Found"})
    return httpx.MockTransport(handler)


@pytest.mark.parametrize("outcome", ["blocked", "pushed", "no_location"])
def test_secrets_check_flow(tmp_path, monkeypatch, outcome):
    monkeypatch.setattr(ha, "POLL_SECONDS", 0)
    sent: dict[str, Any] = {}
    job = {"id": "job-1", "branch": "hangar/job-1", "state": "queued", "reason": None, "events": [], "result": {}}

    def fn(method, path, body, req):
        if (method, path) == ("POST", "/api/jobs"):
            sent.update(body)
            return job
        if (method, path) == ("GET", "/api/jobs/job-1"):
            fname = re.search(r"f=(\S+);", sent["verify"]).group(1)
            where = f"{fname}:1: GitHub token" if outcome != "no_location" else "a commit: GitHub token"
            return {**job, "state": "failed", "reason": "secret_found",
                    "result": {"pushed": outcome == "pushed"},
                    "events": [{"message": f"not pushed: possible secret in {where}"}]}
        return None

    api = _FakeApi(fn=fn)
    state = ha.State(tmp_path / "s.json")
    monkeypatch.setattr(tc, "GITHUB_TRANSPORT", _github({}))
    opts = _opts(with_model=True, test_repo="me/accept-target")
    if outcome == "blocked":
        assert "secret_found, events name accept-secret-" in tc.check_secrets(api, state, opts)
    else:
        with pytest.raises(ha.Fail, match="pushed hangar/job-1|don't name"):
            tc.check_secrets(api, state, opts)
    assert sent["deliver"] == "branch" and sent["job_budget"] == {"max_turns": 1} and not sent["verify_in_loop"]
    head, tail = re.search(r"printf '%s%s\\n' (\S+) (\S+) >", sent["verify"]).groups()
    assert head.strip("'") + tail.strip("'") not in json.dumps(sent)  # only the sandbox ever joins them
    assert state.get("jobs") == ["job-1"] and {"repo": "me/accept-target", "branch": "hangar/job-1"} in \
        state.get("branches")
    with pytest.raises(ha.Skip, match="--with-model"):
        tc.check_secrets(api, state, _opts(test_repo="me/accept-target"))


# ---------------------------------------------------------------------------- landing, by its parts
@pytest.mark.parametrize("mode", ["armed", "merged", "no_approver", "not_bot"])
def test_landing_check_flow(tmp_path, monkeypatch, mode):
    """The landing check against stand-in hangar and GitHub APIs: the PR's author is the App; an approval arms
    auto-merge (or merges at once, which says the disarm wasn't checked) and a push disarms it; the repo policy it
    changed is recorded so cleanup puts it back."""
    monkeypatch.setattr(ha, "POLL_SECONDS", 0)
    monkeypatch.setattr(tc.time, "sleep", lambda _s: None)
    if mode == "no_approver":
        monkeypatch.delenv("HANGAR_ACCEPT_APPROVER_TOKEN", raising=False)
    else:
        monkeypatch.setenv("HANGAR_ACCEPT_APPROVER_TOKEN", "approver-token")
    monkeypatch.setenv("HANGAR_ACCEPT_GITHUB_TOKEN", "writer-token")
    world_: dict[str, Any] = {"approved": False, "pushed": False}
    head = "a" * 40

    def hangar(method, path, body, req):
        if path == "/api/settings/HANGAR_GITHUB_APP_CREDENTIAL":
            return {"value": "github-app"}
        if (method, path) == ("GET", "/api/repos/policy"):
            return httpx.Response(404, json={"error": "no policy"})
        if (method, path) == ("PUT", "/api/repos/policy"):
            assert body == {"repo": "me/accept-target", "land": "auto_after_approval", "approvers": ["alice"]}
            return {}
        if (method, path) == ("POST", "/api/jobs"):
            assert body["deliver"] == "pr"
            return {"id": "job-1", "branch": "hangar/job-1", "state": "queued"}
        if (method, path) == ("GET", "/api/jobs/job-1"):
            landing = {"pr_state": "open"}
            if world_["approved"] and not world_["pushed"]:
                landing = ({"pr_state": "merged"} if mode == "merged" else
                           {"pr_state": "approved", "approved_sha": head, "armed_sha": head})
            return {"id": "job-1", "branch": "hangar/job-1", "state": "succeeded", "reason": None, "events": [],
                    "result": {"pr_number": 7}, "landing": landing}
        return None

    def approve(req):
        world_["approved"] = True
        return {"id": 1}

    def push(req):
        assert req.headers["authorization"] == "Bearer writer-token"
        world_["pushed"] = True
        return httpx.Response(201, json={})

    author = {"login": "hangar-agents[bot]", "type": "Bot"} if mode != "not_bot" else {"login": "sean", "type": "User"}
    seen: list = []
    monkeypatch.setattr(tc, "GITHUB_TRANSPORT", _github({
        ("GET", "/user"): {"login": "alice"},
        ("GET", "/repos/me/accept-target/pulls/7"): lambda r: {
            "number": 7, "user": author, "head": {"sha": head, "ref": "hangar/job-1"},
            "auto_merge": {"merge_method": "squash"} if world_["approved"] and not world_["pushed"] else None},
        ("POST", "/repos/me/accept-target/pulls/7/reviews"): approve,
        ("PUT", r"/repos/me/accept-target/contents/accept-landing-.*-push\.txt"): push,
    }, seen))
    api = _FakeApi(fn=hangar)
    state = ha.State(tmp_path / "s.json")
    opts = _opts(with_model=True, test_repo="me/accept-target", landing_timeout=5)
    if mode == "not_bot":
        with pytest.raises(ha.Fail, match="opened by sean"):
            tc.check_landing(api, state, opts)
        return
    summary = tc.check_landing(api, state, opts)
    assert state.get("prs") == [{"repo": "me/accept-target", "number": 7, "branch": "hangar/job-1"}]
    if mode == "no_approver":
        assert "approval not simulated" in summary and state.get("policy_restore") is None
        return
    assert state.get("policy_restore") == {"repo": "me/accept-target", "old": None}
    if mode == "armed":
        assert "armed auto-merge" in summary and "a push disarmed it" in summary, summary
        review = next(b for m, p, b in seen if p.endswith("/reviews"))
        assert review["commit_id"] == head and review["event"] == "APPROVE"
    else:
        assert "merged at once" in summary and not world_["pushed"]


def test_landing_skips_cleanly(tmp_path):
    api = _FakeApi()
    state = ha.State(tmp_path / "s.json")
    with pytest.raises(ha.Skip, match="--test-repo"):
        tc.check_landing(api, state, _opts(with_model=True))
    with pytest.raises(ha.Skip, match="HANGAR_GITHUB_APP_CREDENTIAL"):
        tc.check_landing(api, state, _opts(with_model=True, test_repo="me/r"))
    assert not [c for c in api.calls if c[0] != "GET"]  # it made nothing


def test_cleanup_closes_prs_and_puts_policy_back(tmp_path, monkeypatch):
    monkeypatch.setenv("HANGAR_ACCEPT_GITHUB_TOKEN", "writer-token")
    seen: list = []
    monkeypatch.setattr(tc, "GITHUB_TRANSPORT", _github({
        ("PATCH", "/repos/me/r/pulls/7"): {"state": "closed"},
        ("DELETE", "/repos/me/r/git/refs/heads/hangar/job-1"): httpx.Response(204),
    }, seen))
    api = _FakeApi({("GET", "/api/jobs/job-1"): {"state": "succeeded"}, ("PUT", "/api/repos/policy"): {},
                    ("DELETE", "/api/people/accept-r-p1"): {}})
    state = ha.State(tmp_path / "s.json")
    state.add("jobs", "job-1")
    state.add("prs", {"repo": "me/r", "number": 7, "branch": "hangar/job-1"})
    state.add("people", "accept-r-p1")
    state.set("policy_restore", {"repo": "me/r", "old": {"land": "manual", "approvers": ["bob"]}})
    left: list[str] = []

    def attempt(what, fn):
        try:
            fn()
        except Exception:  # noqa: BLE001
            left.append(what)

    tc.cleanup_first(api, state, attempt, _opts())
    tc.cleanup_last(api, state, attempt, _opts())
    assert left == []
    assert [(m, p) for m, p, _ in seen] == [("PATCH", "/repos/me/r/pulls/7"),
                                            ("DELETE", "/repos/me/r/git/refs/heads/hangar/job-1")]
    assert ("PUT", "/api/repos/policy", {"repo": "me/r", "land": "manual", "approvers": ["bob"]}) in api.calls
    assert ("DELETE", "/api/people/accept-r-p1", None) in api.calls
    assert not [c for c in api.calls if c[1].endswith("/cancel")]  # the job had finished
    assert state.get("policy_restore")["restored"]


# ---------------------------------------------------------------------------- envbuild, by its parts
def _git(*args: str, cwd: Path | None = None) -> str:
    env = {"PATH": os.environ["PATH"], "HOME": str(cwd or "/nonexistent"), "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    return subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True).stdout


def test_fixture_repo_commits_and_restores(tmp_path):
    """The envbuild check's commits to the fixture repo's default branch, and putting the files back."""
    seed, bare = tmp_path / "seed", tmp_path / "fixture.git"
    seed.mkdir()
    _git("init", "-q", "-b", "trunk", cwd=seed)
    (seed / ".gitignore").write_text("*.log\n")
    _git("add", ".", cwd=seed)
    _git("commit", "-qm", "init", cwd=seed)
    _git("clone", "-q", "--bare", str(seed), str(bare))
    fx = tc.FixtureRepo(f"file://{bare}", None)
    files = {tc.FIXTURE_FILE: tc.fixture_config("r1"), tc.FIXTURE_IGNORE: ".accept/\n"}
    sha, old = fx.commit(files, "fixture")
    assert old == {tc.FIXTURE_FILE: None, tc.FIXTURE_IGNORE: "*.log\n"}
    assert _git("--git-dir", str(bare), "rev-parse", "trunk").strip() == sha
    assert "install:" in _git("--git-dir", str(bare), "show", f"trunk:{tc.FIXTURE_FILE}")
    fx.commit(old, "put back")
    assert _git("--git-dir", str(bare), "show", f"trunk:{tc.FIXTURE_IGNORE}") == "*.log\n"
    with pytest.raises(subprocess.CalledProcessError):
        _git("--git-dir", str(bare), "show", f"trunk:{tc.FIXTURE_FILE}")
    state = ha.State(tmp_path / "s.json")
    state.set("fixture", {"url": f"file://{bare}", "old": {tc.FIXTURE_FILE: None}, "restored": False})
    tc._restore_fixture(state)  # nothing to change: no new commit, and it's marked done
    assert state.get("fixture")["restored"]


@pytest.mark.parametrize("broken", [False, True])
def test_fixture_config_is_a_valid_repo_environment(broken):
    """What the envbuild check commits passes hangar's strict parser for .hangar/environment.yaml."""
    from hangar import repoenv

    cfg = repoenv.parse(tc.fixture_config("0101-ab12", broken=broken))
    assert ("exit 3" in cfg.install) is broken and cfg.terminals[0].port == 8000 and cfg.ports[0].port == 8000


def test_envbuild_skips_without_a_fixture_repo(tmp_path, monkeypatch):
    with pytest.raises(ha.Skip, match="--fixture-repo"):
        tc.check_envbuild(_FakeApi(), ha.State(tmp_path / "s.json"), _opts())
    monkeypatch.delenv("HANGAR_ACCEPT_GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(tc.shutil, "which", lambda _x: None)
    with pytest.raises(ha.Skip, match="HANGAR_ACCEPT_GITHUB_TOKEN"):
        tc.check_envbuild(_FakeApi(), ha.State(tmp_path / "s.json"), _opts(fixture_repo="me/fixture"))


@pytest.mark.parametrize("ref,repo", [
    ("us-docker.pkg.dev/p/hangar/python:abc123", "us-docker.pkg.dev/p/hangar/python"),
    ("us-docker.pkg.dev/p/hangar/python@sha256:00ff", "us-docker.pkg.dev/p/hangar/python"),
    ("localhost:5000/hangar/minimal:1@sha256:00ff", "localhost:5000/hangar/minimal"),
    ("minimal", "minimal"),
])
def test_image_repo(ref, repo):
    assert tc.image_repo(ref) == repo


def test_deploy_rolls_back_only_for_core_checks():
    """deploy.yml gates on the core checks and keeps the report; it never runs paid turns or a power cycle."""
    import yaml

    wf = yaml.safe_load((ROOT / ".github" / "workflows" / "deploy.yml").read_text())
    steps = {s.get("name") or s.get("uses"): s for s in wf["jobs"]["deploy"]["steps"]}
    run = steps["Acceptance"]["run"]
    assert "--gate core" in run and "--report accept-report.json" in run and "--record accept.jsonl" in run
    assert "--with-model" not in run and "--power-cycle" not in run
    assert "secrets.HANGAR_ACCEPT_GITHUB_TOKEN" in json.dumps(steps["Acceptance"]["env"])
    assert "accept-report.json" in steps["Summary (version, tool-list diff, acceptance)"]["run"]
