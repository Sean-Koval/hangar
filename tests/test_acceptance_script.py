"""scripts/acceptance/gke.sh against a real hangar on Docker, so the script the owner runs on GKE is known to work:
every check that needs no cluster runs for real (a sandbox, exec through hangar-envd, MCP files, sandbox-to-sandbox
isolation, a session's checkpoint, suspend and resume, a turn, the git gateway, the e2b SDK, backups, /metrics), the
GKE-only ones and the steered Claude Code turn are skipped, and nothing it made is left behind.

github.com is a local `git http-backend` behind the gateway (tests/test_gateway_git.py), and a stand-in `gh` deletes
the pushed branch there. A scripted harness stands in for the model: it answers the `model` check with the word it
was asked for.
Opt-in (needs Docker, the `minimal` image or the time to build it, and envd/build.sh's binary for the envd parts):
    HANGAR_E2E=1 uv run --all-extras pytest tests/test_acceptance_script.py -v
"""

from __future__ import annotations

import os
import re
import socket
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from hangar.envd import find_binaries
from tests.test_e2e_jobs_docker import _bridge_gateway, _free_port
from tests.test_exchange import world  # noqa: F401 - fixture: the scripted xenv environment
from tests.test_gateway_git import PAT, upstream  # noqa: F401 - fixture: acme/widgets behind git http-backend
from tests.test_mcp_modern import AppThread

e2e = pytest.mark.skipif(os.environ.get("HANGAR_E2E") != "1", reason="set HANGAR_E2E=1 to run")

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "acceptance" / "gke.sh"
TOKEN = "accept-e2e-admin-token"


def _listener() -> tuple[socket.socket, int]:
    """A port on the host that sandboxes reach: the isolation check's stand-in for the internet."""
    s = socket.socket()
    s.bind(("0.0.0.0", 0))
    s.listen(16)

    def accept() -> None:
        while True:
            try:
                c, _ = s.accept()
            except OSError:
                return
            c.close()

    threading.Thread(target=accept, daemon=True).start()
    return s, s.getsockname()[1]


def _fake_gh(bindir: Path, bare: Path) -> None:
    """`gh api -X DELETE repos/acme/widgets/git/refs/heads/<branch>`, against the local upstream."""
    bindir.mkdir()
    gh = bindir / "gh"
    gh.write_text("#!/bin/sh\n"
                  '[ "$1 $2 $3" = "api -X DELETE" ] || exit 2\n'
                  'ref="refs/heads/${4#repos/acme/widgets/git/refs/heads/}"\n'
                  f'git --git-dir "{bare}" rev-parse -q --verify "$ref" >/dev/null || '
                  '{ echo "gh: Reference does not exist (HTTP 422)" >&2; exit 1; }\n'  # as GitHub answers
                  f'exec git --git-dir "{bare}" update-ref -d "$ref"\n')
    gh.chmod(0o755)


def _env(tmp_path: Path, port: int, **extra: str) -> dict[str, str]:
    return {**os.environ, "HANGAR_URL": f"http://127.0.0.1:{port}", "HANGAR_TOKEN": TOKEN,
            "HANGAR_CLI_CONFIG": str(tmp_path / "no-cli.json"), "HANGAR_ACCEPT_PYTHON": sys.executable,
            "TMPDIR": str(tmp_path), **extra}


@e2e
def test_acceptance_script_on_docker(tmp_path, upstream, monkeypatch):  # noqa: F811
    from hangar.app import create_app
    from hangar.config import Settings
    from hangar.harnesses import HARNESSES, Harness
    from hangar.models import ApiToken, CredentialKind, Sandbox, Session

    answer = ('w=$(grep -o "hangar-accept-[0-9a-f-]*" "$HANGAR_PROMPT_FILE" | head -1); '
              'echo "{\\"type\\":\\"result\\",\\"result\\":\\"$w\\"}"')
    monkeypatch.setitem(HARNESSES, "scripted", Harness(name="scripted", title="Scripted", run=answer,
                                                       session_dirs=[".fake/sessions"]))
    envs = tmp_path / "envs"
    envs.mkdir()
    (envs / "accsession.yaml").write_text(
        "name: accsession\nbase: mirror.gcr.io/library/ubuntu:24.04\nharnesses: [scripted]\nharness_mode: bake\n"
        "resources: {cpus: 1, memory_gb: 1}\n")
    envd_dir = Path(os.environ.get("HANGAR_ENVD_DIR") or ROOT / "envd" / "dist")
    envd = bool(find_binaries(Settings(envd_dir=envd_dir)))
    host, gw_port = _bridge_gateway(), _free_port()
    s = Settings(data_dir=tmp_path / "data", env_dirs=[envs, ROOT / "envs"], reaper_interval_seconds=3600,
                 session_poll_seconds=1, admin_token=TOKEN, envd_dir=envd_dir,
                 gateway_url=f"http://{host}:{gw_port}", gateway_port=gw_port, gateway_host="0.0.0.0",
                 gateway_upstreams={"github.com": f"http://127.0.0.1:{upstream.port}"}, gateway_credential="gh-pat",
                 extra_ca_bundle=os.environ.get("HANGAR_EXTRA_CA_BUNDLE") or None)
    s.snapshot_dir.mkdir(parents=True, exist_ok=True)
    app = create_app(s)
    srv = AppThread(app)
    h = app.state.hangar
    h.broker.put("gh-pat", "git", CredentialKind.GIT_TOKEN, PAT)
    probe, port = _listener()
    bare = upstream.root / "acme/widgets.git"
    _fake_gh(tmp_path / "bin", bare)
    try:
        args = ["bash", str(SCRIPT), "--no-kubectl", "--env", "minimal", "--session-env", "accsession",
                "--mcp-env", "accsession",
                "--gateway-repo", "acme/widgets", "--exec-calls", "5", "--internet-probe", f"{host}:{port}",
                "--with-model", "--skip", "steer"]
        if not envd:
            args.append("--no-expect-envd")
        r = subprocess.run(args, env=_env(tmp_path, srv.port, PATH=f"{tmp_path / 'bin'}:{os.environ['PATH']}"),
                           capture_output=True, text=True, timeout=1800, cwd=tmp_path)
        out = r.stdout + r.stderr
        print(out)
        summary = out[out.rindex("== summary"):]
        status = {name: st for st, name in re.findall(r"^(PASS|FAIL|SKIP|INFO)\s+(\w+)", summary, re.M)}
        assert r.returncode == 0, out
        for check in ("doctor", "sandbox", "exec", "stream", "files", "mcp", "isolation", "session", "model", "gateway",
                      "e2b", "backup", "metrics"):
            assert status.get(check) == "PASS", (check, summary)
        assert status.get("k8s") == "SKIP" and "steer" not in status, summary  # --no-kubectl; --skip steer
        assert status.get("power") == "INFO"
        if envd:  # every timed exec went through the in-sandbox daemon, and B's daemon was out of A's reach
            assert re.search(r"PASS\s+exec .*envd \(\d+ calls", summary), summary
            assert re.search(r"PASS\s+stream\s.*first output after \d+ ms of", summary), summary
            assert re.search(r"ok\s+sandbox B's hangar-envd \([\d.]+:49983\): (timeout|refused)", out), out
        # The gateway check pushed through hangar to the upstream, and the cleanup's `gh` deleted the branch again.
        assert any(m == "POST" and p.endswith("/git-receive-pack") for m, p in upstream.requests)
        assert "refs/heads/hangar/accept-" not in upstream.refs() and "removed branch acme/widgets:" in out

        # Nothing the run made is left: sessions, live sandboxes, tokens, its work directory.
        assert not [x for x in h.store.list(Session) if x.name.startswith("accept-")]
        assert not [x for x in h.store.list(Sandbox) if "acceptance" in x.labels and x.state.value != "stopped"]
        assert not [t for t in h.store.list(ApiToken) if t.name.startswith("accept-")]
        assert not list(tmp_path.glob("hangar-accept.*"))
    finally:
        probe.close()
        srv.stop()


@e2e
def test_in_pod_exec_measurement(tmp_path):
    """The exec check's in-cluster measurement (hangar_accept.IN_POD, which gke.sh runs in the control-plane pod with
    kubectl exec) times the provider's own exec and then hangar-envd. Kubernetes isn't here, so the Docker provider,
    which has the same exec and envd interface, stands in for it."""
    import base64
    import json

    from hangar.app import create_app
    from hangar.config import Settings

    envd_dir = Path(os.environ.get("HANGAR_ENVD_DIR") or ROOT / "envd" / "dist")
    if not find_binaries(Settings(envd_dir=envd_dir)):
        pytest.skip("no hangar-envd binary (run envd/build.sh)")
    sys.path.insert(0, str(ROOT / "scripts" / "acceptance"))
    try:
        from hangar_accept import IN_POD
    finally:
        sys.path.pop(0)
    s = Settings(data_dir=tmp_path / "data", env_dirs=[ROOT / "envs"], reaper_interval_seconds=3600,
                 admin_token=TOKEN, envd_dir=envd_dir)
    s.snapshot_dir.mkdir(parents=True, exist_ok=True)
    app = create_app(s)
    srv = AppThread(app)
    import httpx

    c = httpx.Client(base_url=f"http://127.0.0.1:{srv.port}", headers={"Authorization": f"Bearer {TOKEN}"},
                     timeout=600)
    sb = None
    try:
        sb = c.post("/api/sandboxes", json={"environment": "minimal", "name": "accept-inpod"}).json()[0]
        raw = base64.b64encode(json.dumps(sb).encode()).decode()
        shim = ("import sys, hangar.providers.docker as d, hangar.providers.kubernetes as k; "
                "k.KubernetesProvider = d.DockerProvider; sys.argv = ['-', *sys.argv[1:]]; exec(sys.stdin.read())")
        r = subprocess.run([sys.executable, "-c", shim, raw, "3"], input=IN_POD, capture_output=True, text=True,
                           timeout=300, env={**os.environ, "HANGAR_DATA_DIR": str(s.data_dir),
                                             "HANGAR_ENVD_DIR": str(envd_dir)})
        line = next((ln for ln in r.stdout.splitlines() if ln.startswith("HANGAR-ACCEPT ")), None)
        assert line, r.stdout + r.stderr
        m = json.loads(line.split(" ", 1)[1])
        assert len(m["exec"]) == 3 and len(m["envd"]) == 3 and m["daemon"].startswith("http://"), m
    finally:
        if sb and "id" in sb:
            c.delete(f"/api/sandboxes/{sb['id']}")
        c.close()
        srv.stop()


@e2e
def test_cleanup_runs_after_a_failed_check(tmp_path):
    """A check that fails (a sandbox in an environment that doesn't exist) skips the checks that need it, the others
    still run, cleanup still removes what they made, and the run exits non-zero."""
    from hangar.app import create_app
    from hangar.config import Settings
    from hangar.models import ApiToken

    s = Settings(data_dir=tmp_path / "data", env_dirs=[ROOT / "envs"], reaper_interval_seconds=3600,
                 admin_token=TOKEN)
    s.snapshot_dir.mkdir(parents=True, exist_ok=True)
    app = create_app(s)
    srv = AppThread(app)
    try:
        r = subprocess.run(["bash", str(SCRIPT), "--no-kubectl", "--only", "metrics,sandbox,exec",
                            "--env", "no-such-env"], env=_env(tmp_path, srv.port), capture_output=True, text=True,
                           timeout=300, cwd=tmp_path)
        out = r.stdout + r.stderr
        assert r.returncode == 1, out
        assert re.search(r"^FAIL\s+sandbox .*unknown environment", out, re.M), out
        assert re.search(r"^SKIP\s+exec .*needs the sandbox check", out, re.M), out
        assert re.search(r"^PASS\s+metrics", out, re.M), out
        assert "removed token" in out  # the metrics check's scoped token
        assert not [t for t in app.state.hangar.store.list(ApiToken) if t.name.startswith("accept-")]
    finally:
        srv.stop()


def test_no_hangar_to_test(tmp_path):
    """Without a login or HANGAR_URL the script says how to log in and creates nothing."""
    env = {k: v for k, v in os.environ.items() if k not in ("HANGAR_URL", "HANGAR_TOKEN")}
    r = subprocess.run(["bash", str(SCRIPT)], env={**env, "HANGAR_CLI_CONFIG": str(tmp_path / "none.json"),
                                                   "HANGAR_ACCEPT_PYTHON": sys.executable, "TMPDIR": str(tmp_path)},
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 2 and "hangar login" in r.stderr, r.stdout + r.stderr
    assert not list(tmp_path.glob("hangar-accept.*"))


KUBECTL = """#!/bin/sh
# stand-in kubectl: answers the k8s check's three reads from files, and logs each call
echo "$*" >> "$(dirname "$0")/calls"
case "$*" in
  "-n hangar get deploy hangar -o json") cat "$(dirname "$0")/deploy.json" ;;
  "-n hangar-sandboxes get networkpolicy -o json") cat "$(dirname "$0")/policies.json" ;;
  "-n hangar get configmap hangar-site -o json") echo '{"data": {"HANGAR_SESSION_STORE": "gs://b"}}' ;;
  *) echo "unexpected: $*" >&2; exit 1 ;;
esac
"""


@pytest.mark.parametrize("grace,policies,ok", [
    (45, ["sandbox-egress-baseline", "sandbox-deny-ingress", "allow-hangar-envd", "allow-hangar-gateway"], True),
    (30, ["sandbox-egress-baseline", "sandbox-deny-ingress", "allow-hangar-envd", "allow-hangar-gateway"], False),
    (45, ["allow-hangar-gateway"], False),  # a pre-stack cluster: kustomization not reapplied
])
def test_k8s_check_reads_the_cluster(tmp_path, monkeypatch, grace, policies, ok):
    """The `k8s` check against a stand-in kubectl: the upgrade's NetworkPolicies and grace period, or what's missing."""
    import argparse
    import json

    sys.path.insert(0, str(ROOT / "scripts" / "acceptance"))
    try:
        import hangar_accept as ha
    finally:
        sys.path.pop(0)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "kubectl").write_text(KUBECTL)
    (bindir / "kubectl").chmod(0o755)
    (bindir / "deploy.json").write_text(json.dumps({
        "spec": {"replicas": 1, "template": {"spec": {"terminationGracePeriodSeconds": grace,
                                                      "containers": [{"image": "r/control-plane:abc1234"}]}}},
        "status": {"readyReplicas": 1}}))
    (bindir / "policies.json").write_text(json.dumps({"items": [{"metadata": {"name": n}} for n in policies]}))
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    opts = argparse.Namespace(no_kubectl=False, namespace="hangar", sandbox_namespace="hangar-sandboxes")
    if ok:
        assert ha.check_k8s(None, None, opts) == "manifests applied; control-plane:abc1234"
    else:
        with pytest.raises(ha.Fail, match="terminationGracePeriodSeconds is 30|missing in hangar-sandboxes"):
            ha.check_k8s(None, None, opts)
    assert (bindir / "calls").read_text().count("\n") == 3
    with pytest.raises(ha.Skip):
        ha.check_k8s(None, None, argparse.Namespace(**{**vars(opts), "no_kubectl": True}))


class _SteerHangar:
    """Just enough of hangar's REST API for the `steer` check: a session whose live turn asks to run a command,
    holds it until it's allowed, and takes a steer message meanwhile."""

    def __init__(self, asks: bool):
        self.asks, self.polls, self.calls = asks, 0, []
        self.s = {"state": "idle", "busy": False, "sandbox_id": "sb-1", "turns": [], "reason": None}
        self.allowed = False

    def call(self, method, path, ok=(200,), **kw):
        self.calls.append((method, path, kw.get("json")))
        name = "accept-r1-steer"
        if (method, path) == ("POST", "/api/sessions"):
            assert kw["json"]["approvals"] == "ask" and kw["json"]["name"] == name
            return self.s
        if (method, path) == ("GET", f"/api/sessions/{name}"):
            t = self.s["turns"][-1] if self.s["turns"] else None
            if t and (self.allowed or not self.asks) and not t["finished_at"]:
                t.update(finished_at=1.0, exit_code=0, live=True, summary="DONE STEERED",
                         approvals=[{"status": "allowed"}] if self.asks else [],
                         steers=[{"status": "delivered"}] if self.asks else [])
                self.s.update(state="idle", busy=False)
            return self.s
        if (method, path) == ("POST", f"/api/sessions/{name}/send"):
            self.s["turns"].append({"finished_at": None, "message": kw["json"]["message"]})
            self.s.update(state="running", busy=True)
            return self.s
        if (method, path) == ("GET", f"/api/sessions/{name}/approvals"):
            self.polls += 1
            pending = [{"id": "apr-1", "tool": "Bash", "detail": "echo ok > f"}] if self.asks and self.polls > 1 else []
            return {"pending": pending, "live": True}
        if (method, path) == ("POST", f"/api/sessions/{name}/steer"):
            return {"status": "delivered"}
        if (method, path) == ("POST", f"/api/sessions/{name}/approvals/apr-1"):
            assert kw["json"] == {"decision": "allow"}
            self.allowed = True
            return {}
        if (method, path) == ("GET", "/api/sandboxes/sb-1"):
            return {"info": {"repo_path": "/workspace/r"}}
        raise AssertionError(f"unexpected {method} {path}")

    def exec(self, sb, command, cwd=None, timeout=120):
        assert sb == "sb-1" and cwd == "/workspace/r" and command.startswith("cat ")
        return {"exit_code": 0, "stdout": "ok\n" if self.allowed else "", "stderr": ""}


class _GatewayHangar:
    """Just enough of hangar's REST API for the `gateway` check: a session whose sandbox pushes through a gateway
    that lets `allowed` refs through and answers the others with `refusal`."""

    def __init__(self, refusal: str, allowed: tuple[str, ...] = ("refs/heads/hangar/",)):
        self.refusal, self.allowed, self.state = refusal, allowed, None
        self.pushed: list[tuple[str, list]] = []  # (ref, the state's branches when the push was made)

    def call(self, method, path, ok=(200,), **kw):
        if (method, path) == ("GET", "/api/sessions/accept-r1"):
            return {"repo": "https://github.com/me/scratch.git", "sandbox_id": "sb-1", "state": "idle", "busy": False}
        if (method, path) == ("GET", "/api/sandboxes/sb-1"):
            return {"id": "sb-1", "info": {"repo_path": "/workspace/scratch", "git": {"mode": "gateway"}}}
        raise AssertionError(f"unexpected {method} {path}")

    def exec(self, sb, command, cwd=None, timeout=120):
        if m := re.search(r"git push .*HEAD:(\S+)", command):
            ref = m.group(1)
            self.pushed.append((ref, list(self.state.get("branches", []))))
            if ref.startswith(self.allowed):
                return {"exit_code": 0, "stdout": "", "stderr": ""}
            out = (f"To http://hangar-gateway.hangar.svc:8788/git/github.com/me/scratch.git\n"
                   f" ! [remote rejected] HEAD -> {ref.removeprefix('refs/heads/')} ({self.refusal})\n"
                   "error: failed to push some refs\n") if self.refusal else ""
            return {"exit_code": 1 if self.refusal else 0, "stdout": out, "stderr": ""}
        return {"exit_code": 0, "stdout": "0\n" if "grep -c" in command else "", "stderr": ""}


@pytest.mark.parametrize("refusal,result", [
    ("hangar: this sandbox may push only to refs/heads/hangar/**", "pass"),
    ("", "let the sandbox push"),  # the gateway's push refs allow everything (HANGAR_GATEWAY_PUSH_REFS)
    ("fetch first", "not with the gateway's refusal"),  # refused by GitHub, not hangar: only the URL says hangar
])
def test_gateway_check_never_pushes_to_main(tmp_path, monkeypatch, refusal, result):
    """The `gateway` check pushes only to branches named for its run, recorded for cleanup before the push. Its
    refused push stands in for main with a branch of its own, so a gateway that lets it through (a wide
    HANGAR_GATEWAY_PUSH_REFS, or a bug) leaves nothing on the owner's main, and the refusal must be hangar's own."""
    import argparse

    sys.path.insert(0, str(ROOT / "scripts" / "acceptance"))
    try:
        import hangar_accept as ha
    finally:
        sys.path.pop(0)
    fake = _GatewayHangar(refusal)
    monkeypatch.setattr(ha.Api, "call", lambda self, *a, **kw: fake.call(*a, **kw))
    monkeypatch.setattr(ha.Api, "exec", lambda self, *a, **kw: fake.exec(*a, **kw))
    api = ha.Api("http://hangar.invalid", "t")
    state = fake.state = ha.State(tmp_path / "state.json")
    state.set("run", "r1")
    state.set("session", "accept-r1")
    opts = argparse.Namespace(gateway_repo="me/scratch", boot_timeout=60)
    if result == "pass":
        assert "refused" in ha.check_gateway(api, state, opts)
    else:
        with pytest.raises(ha.Fail, match=result):
            ha.check_gateway(api, state, opts)
    assert len(fake.pushed) == 2
    for ref, recorded in fake.pushed:
        assert "main" not in ref and "accept-r1" in ref, ref
        assert {"repo": "me/scratch", "branch": ref.removeprefix("refs/heads/")} in recorded  # cleanup deletes it
    assert len(state.get("branches")) == 2


@pytest.mark.parametrize("asks", [True, False])
def test_steer_check_flow(tmp_path, monkeypatch, asks):
    """The `steer` check (paid, so not in the Docker run) against a stand-in API: it steers, then answers the
    approval, then checks the turn ran live and the command ran; a turn that never asks fails the check."""
    import argparse

    sys.path.insert(0, str(ROOT / "scripts" / "acceptance"))
    try:
        import hangar_accept as ha
    finally:
        sys.path.pop(0)
    monkeypatch.setattr(ha.time, "sleep", lambda _s: None)
    fake = _SteerHangar(asks)
    monkeypatch.setattr(ha.Api, "call", lambda self, *a, **kw: fake.call(*a, **kw))
    monkeypatch.setattr(ha.Api, "exec", lambda self, *a, **kw: fake.exec(*a, **kw))
    api = ha.Api("http://hangar.invalid", "t")
    state = ha.State(tmp_path / "state.json")
    state.set("run", "r1")
    opts = argparse.Namespace(session_env="claude-python", repo="https://github.com/o/r.git", model=None,
                              boot_timeout=60, turn_timeout=60)
    if asks:
        assert ha.check_steer(api, state, opts) == "approval held and allowed, steer delivered, command ran"
        order = [p.rsplit("/", 1)[-1] for m, p, _ in fake.calls if m == "POST"]
        assert order == ["sessions", "send", "steer", "apr-1"]
    else:
        with pytest.raises(ha.Fail, match="never waited for an approval"):
            ha.check_steer(api, state, opts)
    assert state.get("sessions") == ["accept-r1-steer"]  # cleanup will find it


@pytest.mark.parametrize("wait,ok", [(0.5, True), (30, False)])
def test_mcp_check_over_http(tmp_path, monkeypatch, world, wait, ok):  # noqa: F811
    """--- deadlines: the `mcp` check against a real hangar over HTTP, on nodes that take a few seconds to start a
    sandbox: it passes when create_sandbox answers within the client's deadline and a dropped client's create still
    comes up, and fails, as claude.ai would, when the server holds the call past the deadline."""
    import argparse

    from hangar.app import create_app
    from hangar.config import Settings
    from hangar.models import Sandbox
    from tests.conftest import ENVS, FakeProvider

    sys.path.insert(0, str(ROOT / "scripts" / "acceptance"))
    try:
        import hangar_accept as ha
    finally:
        sys.path.pop(0)
    s = Settings(data_dir=tmp_path / "data", env_dirs=[world["envs"], ENVS], reaper_interval_seconds=3600,
                 admin_token=TOKEN, mcp_tool_wait_seconds=wait)
    s.snapshot_dir.mkdir(parents=True, exist_ok=True)
    app = create_app(s)
    srv = AppThread(app)
    h = app.state.hangar
    fake = FakeProvider()
    real = fake.create

    async def cold_node(sandbox, image, spec=None):
        import asyncio

        await asyncio.sleep(2)
        await real(sandbox, image, spec)

    fake.create = cold_node
    h.fleet.providers["docker"] = fake
    h.fleet.provider_errors.pop("docker", None)
    monkeypatch.setattr(ha, "CLIENT_DEADLINE", 1.5)  # claude.ai's 230 s, scaled to the 2 s node
    monkeypatch.setattr(ha, "POLL_SECONDS", 0.2)
    try:
        api = ha.Api(f"http://127.0.0.1:{srv.port}", TOKEN)
        state = ha.State(tmp_path / "state.json")
        opts = argparse.Namespace(mcp_env="xenv", mcp_drop_after=0.5, boot_timeout=30)
        if ok:
            summary = ha.check_mcp(api, state, opts)
            assert "a dropped client's create still came up; exec ok" in summary and "+tools." in summary
            made = [h.store.get(Sandbox, i) for i in state.get("sandboxes")]
            assert len(made) == 2 and all(m.state.value == "running" for m in made)
        else:
            with pytest.raises(ha.Fail, match="didn't answer within 2s, so claude.ai would have shown an error"):
                ha.check_mcp(api, state, opts)
    finally:
        srv.stop()
