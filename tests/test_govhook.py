"""The managed, fail-closed Tower hook (KAN-78; govhook.py, tower_hook.py; spec 0015 §9).

The hook script against a fake Tower speaking Tower's /hooks contract; how a turn that requires it starts in a split
sandbox (the hook in hangar's settings, its key, its digest checked), where it is refused, one path per turn, the
manifest's record and tamper detection. The real Claude Code running the hook is tests/test_harden_real_clis.py.
"""

from __future__ import annotations

import contextlib
import io
import json
import shlex
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from hangar import govhook, isolation, manifest, tower_hook
from hangar import steer as steering
from hangar.config import Settings
from hangar.envspec import EnvSpec
from hangar.governance import Decision
from hangar.governed import Governed
from hangar.govevents import Receiver
from hangar.harnesses import HARNESSES
from hangar.models import Approval, CredentialKind, ExecResult, SessionTurn
from tests.test_governed import FakeTower
from tests.test_harden import split_turns  # noqa: F401 - fixture
from tests.test_steer import live, new, settle, svc  # noqa: F401 - fixtures

HOOK_INPUT = {"session_id": "cc-1", "hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_use_id": "toolu_1",
              "tool_input": {"command": "git push origin HEAD:main"}, "cwd": "/workspace/r"}


# ====================================================================== a fake Tower, /hooks only
class Tower:
    """Answers POST /hooks/claude-code with `first`, then GET /hooks/approvals/<id> with each of `then` in turn."""

    def __init__(self, first: tuple[int, object], then: list[tuple[int, object]] | None = None):
        self.first, self.then, self.seen = first, list(then or []), []
        tower = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code: int, body: object):
                raw = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(code)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_POST(self):
                n = int(self.headers.get("content-length") or 0)
                body = json.loads(self.rfile.read(n))
                tower.seen.append(("POST", self.path, self.headers.get("authorization"), body))
                self._send(*tower.first)

            def do_GET(self):
                tower.seen.append(("GET", self.path, self.headers.get("authorization"), None))
                self._send(*(tower.then.pop(0) if tower.then else
                             (200, {"decision": "wait", "approval": self.path.rsplit("/", 1)[-1]})))

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()


@pytest.fixture
def key_file(tmp_path):
    f = tmp_path / "run-1.tower-key"
    f.write_text("aw-key-123\n")
    return str(f)


def run_hook(url: str, key_file: str, *extra: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = tower_hook.main(["--url", url, "--key-file", key_file, "--poll", "0.05", "--timeout", "2", *extra],
                               stdin=io.StringIO(json.dumps(HOOK_INPUT)))
    return code, out.getvalue(), err.getvalue()


def denied(out: str) -> str:
    d = json.loads(out)["hookSpecificOutput"]
    assert d["hookEventName"] == "PreToolUse" and d["permissionDecision"] == "deny"
    return d["permissionDecisionReason"]


# ====================================================================== the hook script
def test_tower_denies_and_the_hook_denies_with_its_reason(key_file):
    t = Tower((200, {"decision": "deny", "approval": "A-2", "reason": "pushes to main need a maintainer"}))
    try:
        code, out, _ = run_hook(t.url, key_file)
    finally:
        t.close()
    assert code == 0 and denied(out) == "Denied by Tower: pushes to main need a maintainer"
    [(method, path, auth, body)] = t.seen
    assert (method, path, auth) == ("POST", "/hooks/claude-code", "Bearer aw-key-123")
    assert body == HOOK_INPUT  # Tower's contract: the hook's JSON as Claude Code wrote it


def test_tower_allows_and_the_hook_says_nothing_so_hangars_own_checks_still_apply(key_file):
    t = Tower((200, {"decision": "allow", "reason": "inside the mandate"}))
    try:
        code, out, err = run_hook(t.url, key_file)
    finally:
        t.close()
    assert (code, out, err) == (0, "", "")  # never "allow": that would skip the driver's guard rail


def test_a_wait_is_followed_until_a_person_decides(key_file):
    t = Tower((200, {"decision": "wait", "approval": "A-7", "reason": "needs Dana"}),
              [(200, {"decision": "wait", "approval": "A-7"}), (200, {"decision": "allow", "reason": "Signed"})])
    try:
        code, out, _ = run_hook(t.url, key_file, "--wait", "10")
    finally:
        t.close()
    assert (code, out) == (0, "")
    assert [p for _, p, _, _ in t.seen] == ["/hooks/claude-code", "/hooks/approvals/A-7", "/hooks/approvals/A-7"]


def test_a_wait_past_the_deadline_is_a_deny_not_an_allow(key_file):
    t = Tower((200, {"decision": "wait", "approval": "A-8", "reason": "needs Dana"}))
    try:
        code, out, _ = run_hook(t.url, key_file, "--wait", "0.2")
    finally:
        t.close()
    assert code == 0 and "A-8 isn't signed yet" in denied(out)


@pytest.mark.parametrize("answer", [
    (503, {"decision": "deny"}),  # Tower's internal error: unavailable, not a policy deny
    (500, b"oops"),
    (401, {"error": "Unknown agent key"}),
    (200, b"not json"),
    (200, {"decision": "maybe"}),
    (200, {"decision": "wait"}),  # no approval id to follow
])
def test_anything_but_a_clean_answer_blocks_the_call(key_file, answer):
    t = Tower(answer)
    try:
        code, out, err = run_hook(t.url, key_file)
    finally:
        t.close()
    assert code == 2 and out == "" and "fails closed" in err


def test_tower_down_blocks_the_call(key_file):
    t = Tower((200, {}))
    url = t.url
    t.close()
    t.srv.server_close()
    code, out, err = run_hook(url, key_file)
    assert code == 2 and out == "" and "can't reach Tower" in err


def test_no_key_blocks_the_call(tmp_path):
    code, _, err = run_hook("http://127.0.0.1:9", str(tmp_path / "missing"))
    assert code == 2 and "no agent key" in err


def test_a_hook_that_cant_even_start_still_blocks(tmp_path, key_file):
    """Claude Code lets a call through on any exit but 2: the settings' command turns a crash into 2."""
    cmd = govhook.command(str(tmp_path / "run-1"), "http://127.0.0.1:9", 5,
                          script=str(tmp_path / "no-such-hook.py"))
    p = subprocess.run(["sh", "-c", cmd], input="{}", capture_output=True, text=True, timeout=30)
    assert p.returncode == 2
    assert cmd.startswith(f"{govhook.ENV} PATH=") and " python3 -I " in cmd and cmd.endswith("|| exit 2")


def test_the_installed_script_runs_on_the_system_python_from_its_settings_command(tmp_path):
    """The command in the settings file, run by a shell as Claude Code runs it: no agent PATH involved."""
    t = Tower((200, {"decision": "deny", "reason": "no"}))
    run = tmp_path / "run-1"
    (tmp_path / "run-1.tower-key").write_text("k")
    try:
        cmd = govhook.command(str(run), t.url, 5, script=tower_hook.__file__)
        p = subprocess.run(["/bin/sh", "-c", cmd], input=json.dumps(HOOK_INPUT), capture_output=True, text=True,
                           timeout=30, env={"PATH": str(tmp_path)})  # an agent PATH with nothing on it
    finally:
        t.close()
    assert p.returncode == 0 and denied(p.stdout) == "Denied by Tower: no"


# ====================================================================== settings and where it's required
def test_an_environment_can_require_the_hook_and_never_loosen_it():
    assert not govhook.required(Settings(), EnvSpec(name="e"))
    assert govhook.required(Settings(), EnvSpec(name="e", governance={"hook": "required"}))
    assert govhook.required(Settings(governance_hook="required"), EnvSpec(name="e", governance={"hook": "off"}))
    with pytest.raises(ValueError):
        EnvSpec(name="e", governance={"hook": "required", "other": 1})


def test_one_path_per_turn_the_driver_never_asks_a_second_time():
    assert govhook.driver_policy("governed") == govhook.driver_policy("ask") == "deny-risky"
    assert govhook.driver_policy("auto") == "auto" and govhook.driver_policy("deny-risky") == "deny-risky"


def test_hangars_hook_goes_first_and_the_environment_cant_switch_hooks_off():
    env = {"disableAllHooks": True, "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": []}], "Stop": [{}]}}
    out = govhook.merge(env, govhook.hooks("/r/run-1", "https://tower.test", 300))
    assert "disableAllHooks" not in out and out["hooks"]["Stop"] == [{}]
    first, second = out["hooks"]["PreToolUse"]
    assert first["matcher"] == "*" and first["hooks"][0]["timeout"] == 300 + govhook.SLACK
    assert govhook.INSTALLED in first["hooks"][0]["command"] and second == {"matcher": "Bash", "hooks": []}


# ====================================================================== a turn that requires it
@pytest.fixture
def hook_on(svc, settings, broker):  # noqa: F811
    settings.governance_hook = "required"
    settings.governance_url = "https://tower.test"
    settings.governance_hook_credential = "tower-hook"
    broker.put("tower-hook", "webhook", CredentialKind.GENERIC, "aw-key-123", exposure="control_plane", team=True)
    return settings


@pytest.fixture
def sandbox_sh(monkeypatch):
    """What hangar's user's commands in the sandbox see: the installed hook's digest (`installed`), its check's
    answer, every command run."""
    state = SimpleNamespace(installed=govhook.digest(), check=None, calls=[])

    async def sh(prov, sb, command, timeout=60):
        state.calls.append(command)
        if command == govhook.BEFORE:
            return ExecResult(exit_code=0, stdout=(state.installed or "sha256:").split(":", 1)[1] + "\n", stderr="")
        if state.check is not None:
            return state.check
        return ExecResult(exit_code=0, stdout=govhook.digest().split(":", 1)[1] + "\n", stderr="")

    monkeypatch.setattr(isolation, "sh", sh)
    return state


async def _box(svc, live, name, split_turns, keep=True):  # noqa: F811
    from hangar import harden

    new(svc, live, name, "hello")
    s = await settle(svc, name)
    sb = svc.fleet.get(s.sandbox_id)
    puts = split_turns()
    if keep:
        svc.fleet.broker.vault.put(harden.VAULT + sb.id, json.dumps({"env": "export A=1\n", "claude_mcp": None}))
    return s, sb, puts


async def test_a_governed_turn_runs_with_the_hook_in_hangars_settings_and_one_path(
        svc, live, hook_on, split_turns, sandbox_sh):  # noqa: F811
    s, sb, puts = await _box(svc, live, "gh1", split_turns)
    s.approvals = "governed"
    turn = SessionTurn(message="x")
    run_id, command = await steering.prepare(svc, s, turn, sb, HARNESSES["live-claude"])
    steering.forget(svc, run_id)
    r = f"{isolation.RUNS}/{run_id}"
    # the driver answers from deny-risky: it holds nothing for Governed.tick to relay (spec 0015 §9)
    assert " --approvals deny-risky " in command and turn.approval_policy == "deny-risky"
    assert f"--setting-sources '' --settings {r}.settings.json" in command  # no user, project or local settings
    files = puts[isolation.RUNS]
    settings = json.loads(files[f"{run_id}.settings.json"][0])
    hook = settings["hooks"]["PreToolUse"][0]["hooks"][0]
    assert hook["command"] == govhook.command(r, "https://tower.test", 300)
    assert files[f"{run_id}.tower-key"] == (b"aw-key-123", 0o644)
    assert puts[isolation.HOME][govhook.NAME] == (govhook.SCRIPT.read_bytes(), 0o644)
    # the record, its digest, and the warning that says the driver keeps deny-risky
    assert turn.governance_hook["digest"] == govhook.digest() and turn.governance_hook["policy"] == "governed"
    assert turn.governance_hook["fail"] == "closed" and "no call is asked about twice" in turn.warnings[-1]
    assert svc.store.kv_get(govhook.KV + sb.id) == govhook.digest()
    assert any(c.startswith("export PATH=") and govhook.INSTALLED in c for c in sandbox_sh.calls)


async def test_an_auto_turn_gets_the_hook_and_hangars_settings_too(
        svc, live, hook_on, split_turns, sandbox_sh):  # noqa: F811
    s, sb, puts = await _box(svc, live, "gh2", split_turns)
    s.approvals = "auto"
    turn = SessionTurn(message="x")
    run_id, command = await steering.prepare(svc, s, turn, sb, HARNESSES["live-claude"])
    steering.forget(svc, run_id)
    assert " --approvals auto " in command and "--base-env" in command and "--setting-sources ''" in command
    assert json.loads(puts[isolation.RUNS][f"{run_id}.settings.json"][0])["hooks"]["PreToolUse"]
    assert turn.governance_hook["policy"] == "auto" and not turn.warnings


async def test_no_split_refuses_the_turn_whatever_the_uid_split_setting(svc, live, hook_on):  # noqa: F811
    new(svc, live, "gh3", "hello")
    s = await settle(svc, "gh3")
    assert hook_on.uid_split != "required"  # unset (KAN-189) or auto: the hook refuses anyway
    with pytest.raises(steering.SteerError, match="only where the agent can't remove it"):
        await steering.prepare(svc, s, SessionTurn(message="x"), svc.fleet.get(s.sandbox_id),
                               HARNESSES["live-claude"])


async def test_without_hangar_owned_settings_the_turn_is_refused(
        svc, live, hook_on, split_turns, sandbox_sh):  # noqa: F811
    s, sb, _ = await _box(svc, live, "gh4", split_turns, keep=False)
    with pytest.raises(steering.SteerError, match="can't own the harness's settings"):
        await steering.prepare(svc, s, SessionTurn(message="x"), sb, HARNESSES["live-claude"])


@pytest.mark.parametrize("unset,match", [("governance_url", "HANGAR_GOVERNANCE_URL isn't set"),
                                         ("governance_hook_credential", "HANGAR_GOVERNANCE_HOOK_CREDENTIAL")])
async def test_without_tower_or_a_key_the_turn_is_refused(
        svc, live, hook_on, split_turns, sandbox_sh, unset, match):  # noqa: F811
    s, sb, _ = await _box(svc, live, f"gh5{unset[-3:]}", split_turns)
    setattr(hook_on, unset, None)
    with pytest.raises(steering.SteerError, match=match):
        await steering.prepare(svc, s, SessionTurn(message="x"), sb, HARNESSES["live-claude"])


async def test_codex_turns_are_refused_and_say_why(svc, live, hook_on):  # noqa: F811
    new(svc, live, "gh6", "hello")
    s = await settle(svc, "gh6")
    with pytest.raises(steering.SteerError, match="Codex can't run hangar's Tower hook"):
        await steering.prepare(svc, s, SessionTurn(message="x"), svc.fleet.get(s.sandbox_id),
                               HARNESSES["live-codex"])


async def test_a_hook_that_isnt_installed_as_hangar_meant_refuses_the_turn(
        svc, live, hook_on, split_turns, sandbox_sh):  # noqa: F811
    s, sb, _ = await _box(svc, live, "gh7", split_turns)
    sandbox_sh.check = ExecResult(exit_code=0, stdout="0" * 64 + "\n", stderr="")
    with pytest.raises(steering.SteerError, match="isn't in place: the installed hook has digest sha256:000"):
        await steering.prepare(svc, s, SessionTurn(message="x"), sb, HARNESSES["live-claude"])
    sandbox_sh.check = ExecResult(exit_code=3, stdout="no python3 >= 3.8 on the system PATH\n", stderr="")
    with pytest.raises(steering.SteerError, match="no python3 >= 3.8"):
        await steering.prepare(svc, s, SessionTurn(message="x"), sb, HARNESSES["live-claude"])


async def test_a_hook_changed_between_turns_is_detected_and_installed_again(
        svc, live, hook_on, split_turns, sandbox_sh):  # noqa: F811
    s, sb, puts = await _box(svc, live, "gh8", split_turns)
    h = HARNESSES["live-claude"]
    run_id, _ = await steering.prepare(svc, s, SessionTurn(message="x"), sb, h)
    steering.forget(svc, run_id)
    sandbox_sh.installed = "sha256:" + "f" * 64  # something wrote over hangar's file
    turn = SessionTurn(message="y")
    run_id, _ = await steering.prepare(svc, s, turn, sb, h)
    steering.forget(svc, run_id)
    assert any("Tower hook in this sandbox has digest sha256:fff" in w for w in turn.warnings)
    [e] = [e for e in svc.store.audit_tail(200) if e.action == "governance.hook_tampered"]
    assert e.detail["expected"] == govhook.digest() and e.detail["found"] == sandbox_sh.installed
    assert turn.governance_hook["digest"] == govhook.digest()  # installed again, and checked
    sandbox_sh.installed = None  # gone
    turn = SessionTurn(message="z")
    run_id, _ = await steering.prepare(svc, s, turn, sb, h)
    steering.forget(svc, run_id)
    assert any("Tower hook in this sandbox is gone" in w for w in turn.warnings)


# ====================================================================== one approval, the manifest
async def test_the_driver_relay_never_asks_tower_about_a_hooked_turn(svc, live, settings, broker):  # noqa: F811
    """Belt and braces for §9: even a pending flagged call on a `governed` turn that ran with the hook is not
    relayed, so Tower sees it once, through /hooks."""
    from tests.test_govevents import configure

    configure(settings, broker)
    tower = FakeTower(lambda req: Decision(outcome="wait", approval_id="A-1", status="waiting"))
    gov = Governed(svc, Receiver(svc, tower=tower))
    new(svc, live, "gh9", "hello")
    s = await settle(svc, "gh9")
    turn = SessionTurn(message="x", live=True, approval_policy="governed", run_id="run-x")
    turn.approvals.append(Approval(id="ap-000000000001", tool="Bash", detail="git push --force", risky="force-push"))
    s.turns.append(turn)
    gov.tick(s, turn)
    await gov.drain()
    assert len(tower.asked) == 1  # without the hook: relayed once
    turn2 = SessionTurn(message="y", live=True, approval_policy="governed", run_id="run-y",
                        governance_hook={"digest": govhook.digest()})
    turn2.approvals.append(Approval(id="ap-000000000002", tool="Bash", detail="git push --force", risky="force-push"))
    s.turns.append(turn2)
    gov.tick(s, turn2)
    await gov.drain()
    assert len(tower.asked) == 1 and turn2.approvals[0].governance is None  # with it: never


def test_the_manifest_records_the_hooks_digest_in_harness_digest():
    rec = {"runtime": "claude-code", "source": govhook.SOURCE, "path": govhook.INSTALLED, "digest": govhook.digest(),
           "matcher": "*", "fail": "closed", "policy": "governed"}
    turn = SessionTurn(message="x", governance_hook=rec)
    [entry] = manifest._hooks(EnvSpec(name="e"), "claude-code", {}, turn)
    assert entry["source"] == "hangar:tower_hook" and entry["digest"] == govhook.digest()
    assert entry["event"] == "PreToolUse" and entry["requested_approvals"] == "governed"
    m = {"hooks": [entry]}
    other = {"hooks": [{**entry, "digest": "sha256:" + "0" * 64}]}
    assert manifest.section_digest(m, manifest.HARNESS_SECTIONS) != manifest.section_digest(
        other, manifest.HARNESS_SECTIONS)  # a different script is a different harness
    assert manifest._hooks(EnvSpec(name="e"), "claude-code", {}, SessionTurn(message="x")) == []


def test_the_command_quotes_what_it_is_given():
    cmd = govhook.command("/var/lib/hangar/runs/run-1", "https://tower.test/a b", 30)
    assert "'https://tower.test/a b'" in cmd and shlex.split(cmd)[-5:] == ["--wait", "30", "||", "exit", "2"]


# ====================================================================== --- KAN-33: a per-sandbox passport
async def test_a_passport_turn_writes_the_sandboxs_own_passport_not_the_shared_key(
        svc, live, hook_on, split_turns, sandbox_sh, monkeypatch):  # noqa: F811
    import time

    asked = []

    async def passport(fleet, sb, **kw):
        asked.append(sb.id)
        return "pp.jwt.sig", time.time() + 900

    monkeypatch.setattr(govhook, "sandbox_passport", passport)
    s, sb, puts = await _box(svc, live, "gp1", split_turns)
    hook_on.governance_hook_auth, hook_on.oidc_issuer = "passport", "https://hangar.test"
    turn = SessionTurn(message="x")
    run_id, _ = await steering.prepare(svc, s, turn, sb, HARNESSES["live-claude"])
    steering.forget(svc, run_id)
    # hangar's user writes it beside the run: the agent can read it (the hook runs as the agent), never change it
    assert puts[isolation.RUNS][f"{run_id}.tower-key"] == (b"pp.jwt.sig", 0o644) and asked == [sb.id]
    assert turn.governance_hook["auth"] == "passport" and turn.governance_hook["key_expires_at"] > time.time()
    assert json.loads(svc.store.kv_get(govhook.PASS + run_id))["sandbox"] == sb.id  # renewed while it runs


async def test_no_passport_no_turn(svc, live, hook_on, split_turns, sandbox_sh, monkeypatch):  # noqa: F811
    async def refused(fleet, sb, **kw):
        raise ValueError("Tower gave this sandbox no passport: HTTP 400 invalid_grant")

    monkeypatch.setattr(govhook, "sandbox_passport", refused)
    s, sb, _ = await _box(svc, live, "gp2", split_turns)
    hook_on.governance_hook_auth, hook_on.oidc_issuer = "passport", "https://hangar.test"
    with pytest.raises(steering.SteerError, match="no passport"):
        await steering.prepare(svc, s, SessionTurn(message="x"), sb, HARNESSES["live-claude"])


def test_passport_mode_needs_hangars_issuer_not_a_stored_key():
    s = Settings(governance_url="https://tower.test", governance_hook_auth="passport", public_url="http://x")
    claude = SimpleNamespace(live="claude", name="claude-code")
    assert "HANGAR_OIDC_ISSUER" in govhook.why_not(s, claude)
    s.oidc_issuer = "https://hangar.test"
    assert govhook.why_not(s, claude) is None  # no HANGAR_GOVERNANCE_HOOK_CREDENTIAL needed


class PersonTower:
    """Tower's token endpoint for a sandbox: a federated `hangar:env:*` token with an email becomes a person's
    passport (its requester_claim), for agent `hangar`."""

    def __init__(self, sub: str | None = "person:alice@example.com"):
        from tests.test_passports import Tower

        self.t, self.sub, self.subjects = Tower(), sub, []

    def __call__(self, request):
        from urllib.parse import parse_qs

        import httpx

        from tests.test_passports import b64d

        if request.url.path == "/oauth/token":
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            self.subjects.append(json.loads(b64d(form["subject_token"].split(".")[1])))
            claims = self.t.passport(sub=self.sub, chain=["hangar"], aud="https://tower.test/hooks")
            return httpx.Response(200, json={"access_token": self.t.sign(claims), "expires_in": 900})
        return self.t(request)


async def test_the_sandbox_passport_is_exchanged_from_its_own_token_and_checked(fleet, settings):
    import httpx

    from hangar import people
    from hangar.models import Sandbox

    settings.governance_url, settings.oidc_issuer = "https://tower.test", "https://hangar.test"
    people.add(fleet.store, "alice")
    people.link(fleet.store, "alice", "email", "alice@example.com")
    sb = Sandbox(name="b", environment="plain", provider="docker", person="alice")
    tw = PersonTower()
    client = httpx.AsyncClient(transport=httpx.MockTransport(tw))
    from tests.test_passports import ORIGIN

    assert ORIGIN == "https://tower.test"
    passport, exp = await govhook.sandbox_passport(fleet, sb, client=client)
    [sub] = tw.subjects
    assert (sub["sub"], sub["person"], sub["email"], sub["aud"]) == ("hangar:env:plain", "alice", "alice@example.com",
                                                                     "https://tower.test/oauth/token")
    assert sub["exp"] - sub["iat"] <= 6 * 60 and passport.count(".") == 2 and exp > 0
    # a sandbox with a person must get that person's passport, not an agent's own
    with pytest.raises(ValueError, match="not its person"):
        await govhook.sandbox_passport(fleet, sb, client=httpx.AsyncClient(
            transport=httpx.MockTransport(PersonTower(sub=None))))
    # a passport for another agent is refused (governance_hook_agent)
    settings.governance_hook_agent = "claude-code"
    with pytest.raises(ValueError, match="no passport"):
        await govhook.sandbox_passport(fleet, sb, client=client)


async def test_refresh_renews_running_runs_and_forgets_ended_ones(fleet, monkeypatch):
    import time

    from hangar.models import Sandbox, SandboxState

    sb = Sandbox(name="b", environment="plain", provider="docker", state=SandboxState.RUNNING)
    gone = Sandbox(name="g", environment="plain", provider="docker", state=SandboxState.STOPPED)
    fleet.store.put(sb)
    fleet.store.put(gone)
    govhook.follow(fleet, sb, "run-a", time.time() + 100)  # due
    govhook.follow(fleet, sb, "run-b", time.time() + 800)  # not yet
    govhook.follow(fleet, sb, "run-c", time.time() + 100)  # its run ended: the key is gone
    govhook.follow(fleet, gone, "run-d", time.time() + 100)
    puts, cmds = [], []

    async def passport(fleet_, sb_, **kw):
        return "new.jwt.sig", time.time() + 900

    async def put(prov, sb_, parent, files):
        puts.append((parent, files))

    async def sh(prov, sb_, command, timeout=60):
        cmds.append(command)
        return ExecResult(exit_code=0, stdout="gone\n" if "run-c" in command else "renewed\n", stderr="")

    monkeypatch.setattr(govhook, "sandbox_passport", passport)
    monkeypatch.setattr(isolation, "put", put)
    monkeypatch.setattr(isolation, "sh", sh)
    monkeypatch.setattr(fleet, "_running", lambda sid: (sb, object()))
    assert await govhook.refresh(fleet) == 1
    assert puts[0] == (isolation.RUNS, {"run-a.tower-key.new": (b"new.jwt.sig", 0o644)})
    assert json.loads(fleet.store.kv_get(govhook.PASS + "run-a"))["expires"] > time.time() + 800
    assert fleet.store.kv_get(govhook.PASS + "run-b") is not None
    assert fleet.store.kv_get(govhook.PASS + "run-c") is None and fleet.store.kv_get(govhook.PASS + "run-d") is None
    # the swap only replaces a key that is still there: a run that ended never gets its passport back
    script = govhook.renew_script("run-a")
    assert script.startswith(f"if [ -e {isolation.RUNS}/run-a.tower-key ]") and "rm -f" in script


def test_a_renewed_key_replaces_only_a_key_still_there(tmp_path, monkeypatch):
    monkeypatch.setattr(isolation, "RUNS", str(tmp_path))
    (tmp_path / "r.tower-key.new").write_text("new")
    assert subprocess.run(govhook.renew_script("r"), shell=True, capture_output=True, text=True).stdout == "gone\n"
    assert not (tmp_path / "r.tower-key.new").exists() and not (tmp_path / "r.tower-key").exists()
    (tmp_path / "r.tower-key").write_text("old")
    (tmp_path / "r.tower-key.new").write_text("new")
    assert subprocess.run(govhook.renew_script("r"), shell=True, capture_output=True, text=True).stdout == "renewed\n"
    assert (tmp_path / "r.tower-key").read_text() == "new"
