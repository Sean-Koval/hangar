"""The harness canary (canary.py, docs/harnesses.md): the lock's layout, resolving the newest stable releases from
the vendors' sources with their checksums, installing CLIs without root, the protocol records and their diff, the
review packet, and landing the bump PR on one approval. Offline: every upstream is a mock transport."""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import shutil
import xml.sax.saxutils
from pathlib import Path

import httpx
import pytest
import yaml

from hangar import canary
from hangar.harnesses import HARNESSES, LOCK_FILE, Harness, pack_digest, pack_script

PROTOCOL = Path(__file__).parent / "fixtures" / "protocol"
MANIFESTS = Path(__file__).parent / "fixtures" / "claude-manifest"
WORKFLOWS = Path(__file__).parent.parent / ".github" / "workflows"
REAL_VERIFY = canary.verify_signed
BAD_AUDIT: set[str] = set()


@pytest.fixture(autouse=True)
def fake_vendor_checks(monkeypatch):
    """Signatures the mocked upstream makes (`sig:` + the manifest's sha256), and npm's own audit, recorded."""
    audits: list[tuple[str, str, str]] = []

    def verify(data: bytes, sig: bytes) -> None:
        if sig != b"sig:" + hashlib.sha256(data).hexdigest().encode():
            raise ValueError("the manifest's signature doesn't verify")

    def audit(pkg: str, version: str, cpu: str) -> None:
        audits.append((pkg, version, cpu))
        if pkg in BAD_AUDIT:
            raise ValueError(f"npm audit signatures failed for {pkg}")

    monkeypatch.setattr(canary, "verify_signed", verify)
    monkeypatch.setattr(canary, "npm_audit", audit)
    BAD_AUDIT.clear()
    return audits


# ====================================================================== the lock
def test_the_lock_round_trips_in_its_own_layout():
    text = LOCK_FILE.read_text()
    lock = yaml.safe_load(text)
    assert canary.dump_lock(lock, canary.lock_header(text)) == text, "a bump PR's diff is only what changed"


def test_a_bump_names_itself_once_in_the_header():
    header = canary.lock_header(LOCK_FILE.read_text())
    once = canary.bumped_header(header, {"pi": ("1.1.0", "1.2.0")}, "2026-10-15")
    twice = canary.bumped_header(once, {"codex": ("0.162.1", "0.163.0")}, "2026-10-16")
    assert once.count("# Canary ") == 1 and twice.count("# Canary ") == 1
    assert twice.splitlines()[-1].startswith("# Canary 2026-10-16: codex 0.162.1 -> 0.163.0")


@pytest.mark.parametrize(("a", "b", "newer"), [
    ("2.1.300", "2.1.296", True), ("2.1.29", "2.1.296", False), ("0.163.0", "0.162.1", True),
    ("1.3.5", "1.3.5", False), ("1.10.0", "1.9.9", True), ("1.2.0-beta.1", "1.2.0", False)])
def test_versions_compare_numerically(a, b, newer):
    assert canary.newer(a, b) is newer


def test_the_pack_script_installs_elsewhere_only_when_asked():
    h = HARNESSES["codex"]
    assert pack_script(h) == pack_script(h, None, "/.hangar-version"), "the default is the pack's own (its digest)"
    moved = pack_script(h, prefix="/home/u/c x/codex", version_file="/home/u/c x/codex/.hangar-version")
    assert "PREFIX='/home/u/c x/codex' " in moved and "> '/home/u/c x/codex/.hangar-version'" in moved


# ====================================================================== resolve
CLAUDE_BIN = {"x86_64": b"claude-x64-2.1.300", "aarch64": b"claude-arm-2.1.300"}
AGY = {"x86_64": b"agy-x64-1.3.5", "aarch64": b"agy-arm-1.3.5"}


def upstream(**over):
    """The vendors' release sources, as of a day when everything moved one release on."""
    state = {"claude_stable": "2.1.300", "claude_sum": None, "agy_arm_version": "1.3.5", "agy_sum": None,
             "codex_platforms": ("linux-x64", "linux-arm64"), "claude_sig": None, "provenance": {}, **over}

    def handler(req: httpx.Request) -> httpx.Response:
        u = str(req.url)
        if u == f"{canary.CLAUDE_RELEASES}/stable":
            return httpx.Response(200, text=state["claude_stable"] + "\n")
        if u.endswith("/manifest.json") or u.endswith("/manifest.json.sig"):
            v = u.split("/")[-2]
            sums = {p: state["claude_sum"] or hashlib.sha256(CLAUDE_BIN[a]).hexdigest()
                    for a, p in canary.CLAUDE_PLATFORMS.items()}
            body = json.dumps({"version": v, "platforms": {p: {"checksum": s, "binary": "claude"}
                                                           for p, s in sums.items()}}).encode()
            if u.endswith(".sig"):
                return httpx.Response(200, content=state["claude_sig"]
                                      or b"sig:" + hashlib.sha256(body).hexdigest().encode())
            return httpx.Response(200, content=body)
        if u.startswith(canary.CLAUDE_RELEASES):
            arch = next(a for a, p in canary.CLAUDE_PLATFORMS.items() if f"/{p}/" in u)
            return httpx.Response(200, content=CLAUDE_BIN[arch])
        if u == f"{canary.AGY_UPDATER}/releases":
            return httpx.Response(200, json=[{"version": "1.3.5", "execution_id": "6164039479459840"},
                                             {"version": "1.3.3", "execution_id": "5524738307653632"}])
        if u.startswith(f"{canary.AGY_UPDATER}/manifests/"):
            arch = "x86_64" if "amd64" in u else "aarch64"
            v = "1.3.5" if arch == "x86_64" else state["agy_arm_version"]
            path = canary.AGY_PLATFORMS[arch][1]
            return httpx.Response(200, json={"version": v, "url": f"{canary.AGY_ARCHIVES}/{v}-6164039479459840/"
                                                                  f"{path}.tar.gz",
                                             "sha512": state["agy_sum"] or hashlib.sha512(AGY[arch]).hexdigest()})
        if u.startswith(canary.AGY_ARCHIVES):
            return httpx.Response(200, content=AGY["x86_64" if "x64" in u else "aarch64"])
        if u.startswith(f"{canary.NPM}/-/npm/v1/attestations/"):
            spec = req.url.path.split("/attestations/", 1)[1].replace("%2f", "/").replace("%2F", "/")
            spec = spec.replace("%40", "@")
            pkg, _, version = spec.rpartition("@")
            repo = state["provenance"].get(spec, canary.NPM_REPOS.get(
                "codex" if pkg == "@openai/codex" else "pi" if "pi-coding" in pkg else "", ""))
            if repo is None:
                return httpx.Response(404)
            stmt = {"subject": [{"name": f"pkg:npm/{pkg.replace('@', '%40', 1)}@{version}"}],
                    "predicate": {"buildDefinition": {"externalParameters": {"workflow": {"repository": repo}}}}}
            return httpx.Response(200, json={"attestations": [{
                "predicateType": "https://slsa.dev/provenance/v1",
                "bundle": {"dsseEnvelope": {"payload": base64.b64encode(json.dumps(stmt).encode()).decode()}}}]})
        if u.startswith(canary.NPM):
            pkg = req.url.path.lstrip("/").replace("%2f", "/").replace("%2F", "/")
            latest = {"@openai/codex": "0.163.0", "@earendil-works/pi-coding-agent": "1.2.0"}[pkg]
            versions = {latest: {"dist": {"integrity": "sha512-abc"}}}
            if pkg == "@openai/codex":
                versions.update({f"{latest}-{p}": {"dist": {"integrity": "sha512-x"}}
                                 for p in state["codex_platforms"]})
            return httpx.Response(200, json={"dist-tags": {"latest": latest}, "versions": versions})
        return httpx.Response(404)

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_resolve_moves_every_harness_forward_with_verified_checksums():
    lock = yaml.safe_load(LOCK_FILE.read_text())
    cand, report = canary.resolve(lock, upstream())
    assert report["changes"] == {"claude-code": ["2.1.296", "2.1.300"], "codex": ["0.162.1", "0.163.0"],
                                 "pi": ["1.1.0", "1.2.0"], "antigravity": ["1.3.3", "1.3.5"]}
    assert cand["claude-code"] == {"version": "2.1.300", "sha256": {
        a: hashlib.sha256(b).hexdigest() for a, b in CLAUDE_BIN.items()}}
    assert cand["antigravity"] == {"version": "1.3.5", "build": "6164039479459840",
                                   "sha512": {a: hashlib.sha512(b).hexdigest() for a, b in AGY.items()}}
    assert "checked against each download" in report["harnesses"]["claude-code"]["checksums"]
    canary.harnesses_for(cand)  # valid as a lock


def test_resolve_never_moves_back_and_refuses_what_doesnt_check_out():
    lock = yaml.safe_load(LOCK_FILE.read_text())
    cand, report = canary.resolve(lock, upstream(claude_stable="2.1.292"))
    assert cand["claude-code"] == lock["claude-code"] and report["harnesses"]["claude-code"]["status"] == "behind"
    cand, report = canary.resolve(lock, upstream(claude_sum="0" * 64))
    assert cand["claude-code"] == lock["claude-code"] and report["harnesses"]["claude-code"]["status"] == "error"
    assert "has sha256" in report["harnesses"]["claude-code"]["detail"]
    _, report = canary.resolve(lock, upstream(agy_sum="0" * 128))
    assert "sha512" in report["harnesses"]["antigravity"]["detail"]
    _, report = canary.resolve(lock, upstream(agy_arm_version="1.3.6"))
    assert "serves 1.3.5 on one platform" in report["harnesses"]["antigravity"]["detail"]
    cand, report = canary.resolve(lock, upstream(codex_platforms=("linux-x64",)))
    assert cand["codex"] == lock["codex"] and "no linux-arm64 build" in report["harnesses"]["codex"]["detail"]


def test_a_claude_release_needs_anthropics_signature_on_its_manifest():
    """The checksums are only as good as the manifest: it must carry a signature by Anthropic's release key, which
    hangar pins (it never takes the key from the release server)."""
    lock = yaml.safe_load(LOCK_FILE.read_text())
    cand, report = canary.resolve(lock, upstream(claude_sig=b"forged"))
    assert cand["claude-code"] == lock["claude-code"] and report["harnesses"]["claude-code"]["status"] == "error"
    assert "signature" in report["harnesses"]["claude-code"]["detail"]
    _, report = canary.resolve(lock, upstream())
    assert "signed by Anthropic's release key" in report["harnesses"]["claude-code"]["checksums"]


@pytest.mark.skipif(shutil.which("gpgv") is None, reason="gpgv is not installed")
def test_the_pinned_key_verifies_a_real_release_manifest_and_nothing_else(monkeypatch):
    data = (MANIFESTS / "2.1.296-manifest.json").read_bytes()
    sig = (MANIFESTS / "2.1.296-manifest.json.sig").read_bytes()
    REAL_VERIFY(data, sig)
    assert json.loads(data)["platforms"]["linux-x64"]["checksum"] == \
        yaml.safe_load(LOCK_FILE.read_text())["claude-code"]["sha256"]["x86_64"]
    with pytest.raises(ValueError, match="signature"):
        REAL_VERIFY(data.replace(b"24972e3b", b"24972e3c"), sig)
    monkeypatch.setattr(canary, "CLAUDE_KEY_FINGERPRINT", "0" * 40)
    with pytest.raises(ValueError, match="signature"):
        REAL_VERIFY(data, sig)


def test_an_npm_candidate_needs_npms_audit_and_provenance_from_its_own_repository(fake_vendor_checks):
    lock = yaml.safe_load(LOCK_FILE.read_text())
    cand, report = canary.resolve(lock, upstream())
    assert cand["codex"]["version"] == "0.163.0" and cand["pi"]["version"] == "1.2.0"
    assert {("@openai/codex", "0.163.0", "x64"), ("@openai/codex", "0.163.0", "arm64"),
            ("@earendil-works/pi-coding-agent", "1.2.0", "x64")} <= set(fake_vendor_checks)
    assert "provenance from https://github.com/openai/codex" in report["harnesses"]["codex"]["checksums"]
    _, report = canary.resolve(lock, upstream(
        provenance={"@openai/codex@0.163.0-linux-arm64": "https://github.com/x/y"}))
    assert report["harnesses"]["codex"]["status"] == "error"
    assert "provenance" in report["harnesses"]["codex"]["detail"]
    _, report = canary.resolve(lock, upstream(provenance={"@earendil-works/pi-coding-agent@1.2.0": None}))
    assert report["harnesses"]["pi"]["status"] == "error"
    BAD_AUDIT.add("@openai/codex")
    _, report = canary.resolve(lock, upstream())
    assert report["harnesses"]["codex"]["status"] == "error" and "audit" in report["harnesses"]["codex"]["detail"]


# ====================================================================== install
@pytest.fixture
def fake_harness(monkeypatch):
    """A harness whose recipe writes a CLI that prints its version, like the real ones do."""
    h = Harness(name="claude-code", title="fake", version="1.0.0", verify="claude --version", install=(
        'mkdir -p "$PREFIX/bin"\nprintf \'#!/bin/sh\\necho "%s (Fake)"\\n\' "$VERSION" > "$PREFIX/bin/claude"\n'
        'chmod 755 "$PREFIX/bin/claude"\n[ "$VERSION" != 6.6.6 ] || { echo "download failed" >&2; exit 4; }'))
    built: list[str] = []
    real = canary.pack_script

    def script(h, prefix=None, version_file="/.hangar-version"):
        built.append(h.version)
        return real(h, prefix, version_file).replace('rm -rf "$HOME" /tmp/*-npm', 'rm -rf "$HOME"')

    def harnesses_for(lock):
        return {"claude-code": h.model_copy(update={"version": lock["claude-code"]["version"]})}

    monkeypatch.setattr(canary, "harnesses_for", harnesses_for)
    monkeypatch.setattr(canary, "pack_script", script)
    return built


def test_install_runs_the_recipe_without_root_and_keeps_what_it_has(fake_harness, tmp_path):
    dest = tmp_path / "clis"
    got = canary.install({"claude-code": {"version": "1.0.0"}}, dest, ["claude-code"], log=lambda m: None)
    assert got == {"claude-code": "1.0.0 (Fake)"} and fake_harness == ["1.0.0"]
    assert (dest / "claude-code" / ".hangar-version").read_text().strip() == "1.0.0 (Fake)"
    canary.install({"claude-code": {"version": "1.0.0"}}, dest, ["claude-code"], log=lambda m: None)
    assert fake_harness == ["1.0.0"], "cached: same recipe, same place"
    canary.install({"claude-code": {"version": "1.1.0"}}, dest, ["claude-code"], log=lambda m: None)
    assert fake_harness == ["1.0.0", "1.1.0"] and "1.1.0" in (dest / "claude-code/.hangar-version").read_text()
    assert canary.test_env(dest) == {"HANGAR_TEST_CLAUDE": str((dest / "claude-code/bin/claude").resolve())}
    with pytest.raises(RuntimeError, match="download failed"):
        canary.install({"claude-code": {"version": "6.6.6"}}, dest, ["claude-code"], log=lambda m: None)


def test_install_fails_when_the_cli_isnt_the_locked_version(monkeypatch, tmp_path):
    h = Harness(name="claude-code", title="fake", version="2.0.0", verify="claude --version", install=(
        'mkdir -p "$PREFIX/bin"\nprintf \'#!/bin/sh\\necho 1.9.9\\n\' > "$PREFIX/bin/claude"\n'
        'chmod 755 "$PREFIX/bin/claude"'))
    monkeypatch.setattr(canary, "harnesses_for", lambda lock: {"claude-code": h})
    with pytest.raises(RuntimeError, match="the lock pins 2.0.0"):
        canary.install({}, tmp_path, ["claude-code"], log=lambda m: None)


# ====================================================================== protocol records
def test_the_recorded_protocols_have_what_the_driver_uses():
    """The checked-in records come from the locked CLIs (test_protocol_real_clis keeps them honest); the driver's
    needs must resolve against them, or the canary would call every candidate broken."""
    codex = json.loads((PROTOCOL / "codex-app-server.json").read_text())
    claude = json.loads((PROTOCOL / "claude-stream-json.json").read_text())
    assert canary.codex_missing(codex) == []
    assert canary.claude_missing(claude) == []
    assert (PROTOCOL / "codex-app-server.json").read_text() == canary.dump_surface(codex)


def test_a_lost_field_or_value_is_breaking_and_an_added_one_is_not():
    codex = json.loads((PROTOCOL / "codex-app-server.json").read_text())
    cand = copy.deepcopy(codex)
    decision = cand["types"]["CommandExecutionApprovalDecision"]
    decision["oneOf"] = [b for b in decision["oneOf"] if b.get("enum") != ["decline"]]
    cand["types"]["TurnSteerParams"]["properties"].pop("expectedTurnId")
    cand["server_notifications"]["all"].append("thread/brandNew")
    d = canary.diff_surface("codex", codex, cand)
    assert d["breaking"] == [
        "client: turn/steer params .expectedTurnId is gone",
        "server_requests: item/commandExecution/requestApproval answer .decision no longer takes decline"]
    assert any("thread/brandNew" in a for a in d["added"])
    assert canary.diff_surface("codex", codex, codex) == {"breaking": [], "added": [], "changed": []}

    claude = json.loads((PROTOCOL / "claude-stream-json.json").read_text())
    cand = copy.deepcopy(claude)
    cand["control_request/can_use_tool"].remove("request.tool_name")
    cand["result/success"].append("brand_new_field")
    d = canary.diff_surface("claude", claude, cand)
    assert d["breaking"] == ["control_request/can_use_tool: request.tool_name is gone"]
    assert any("brand_new_field" in a for a in d["added"])


def test_codex_surface_reads_the_generated_bundle(tmp_path):
    (tmp_path / "v2").mkdir()
    (tmp_path / "ClientRequest.json").write_text(json.dumps({"definitions": {
        "TurnInterruptParams": {"type": "object", "description": "prose", "properties": {
            "threadId": {"type": "string"}, "turnId": {"type": "string"}}}},
        "oneOf": [{"properties": {"method": {"enum": ["turn/interrupt"]},
                                  "params": {"$ref": "#/definitions/TurnInterruptParams"}}},
                  {"properties": {"method": {"enum": ["somethingElse"]}, "params": {}}}]}))
    (tmp_path / "v2" / "TurnInterruptResponse.json").write_text(json.dumps({"type": "object", "title": "T"}))
    s = canary.codex_surface(tmp_path)
    assert s["client"]["all"] == ["somethingElse", "turn/interrupt"]
    assert s["client"]["driver"]["turn/interrupt"] == {"params": {"$ref": "TurnInterruptParams"},
                                                       "answer": {"type": "object"}}
    assert s["types"]["TurnInterruptParams"]["properties"]["turnId"] == {"type": "string"}
    assert "description" not in json.dumps(s)
    missing = canary.codex_missing(s)
    assert "client: turn/start is gone" in missing and not any("turn/interrupt" in m for m in missing)


def test_claude_shape_drops_values_and_map_keys():
    shape = canary.claude_shape([
        {"type": "result", "subtype": "success", "modelUsage": {"claude-opus-5-5": {"inputTokens": 1}},
         "usage": {"input_tokens": 1}},
        {"type": "control_request", "request_id": "r", "request": {"subtype": "can_use_tool", "tool_name": "Bash"}}])
    assert shape == {"control_request/can_use_tool": ["request", "request.subtype", "request.tool_name", "request_id",
                                                      "type"],
                     "result/success": ["modelUsage", "subtype", "type", "usage", "usage.input_tokens"]}


# ====================================================================== the packet
def junit(tmp_path: Path, cases: dict[str, str | None]) -> Path:
    body = "".join(
        f'<testcase classname="tests.t" name="{n}">'
        + ("" if r is None else '<skipped/>' if r == "skip" else
           f'<failure message="{xml.sax.saxutils.escape(r)}">trace</failure>')
        + "</testcase>" for n, r in cases.items())
    p = tmp_path / f"junit-{len(list(tmp_path.iterdir()))}.xml"
    p.write_text(f"<testsuites><testsuite>{body}</testsuite></testsuites>")
    return p


def test_the_packet_passes_only_when_everything_ran_and_passed(tmp_path):
    resolved = {"harnesses": {"pi": {"locked": "1.1.0", "upstream": "1.2.0", "status": "bump", "source": "npm"}},
                "changes": {"pi": ["1.1.0", "1.2.0"]}}
    clean = {"codex": {"breaking": [], "added": ["x"], "changed": []}}
    ok = canary.junit_results([junit(tmp_path, {"a": None, "b": None})])
    assert canary.failed(resolved, clean, ok) == []
    body = canary.packet(resolved, clean, ok, "https://run")
    assert "| pi | 1.1.0 | 1.2.0 | bump |" in body and "2 passed, 0 failed, 0 skipped" in body
    bad = canary.junit_results([junit(tmp_path, {"a": "assert 1 == 2", "b": "skip"})])
    why = canary.failed(resolved, {"codex": {"breaking": ["gone"], "added": [], "changed": []}}, bad)
    assert why == ["1 contract test(s) failed", "1 contract test(s) skipped: a CLI is missing",
                   "no contract test ran", "codex: 1 breaking protocol change(s)"]
    assert "assert 1 == 2" in canary.packet(resolved, clean, bad)
    errored = {"harnesses": {"codex": {"status": "error", "detail": "HTTPError"}}, "changes": {}}
    assert canary.failed(errored, clean, ok) == ["codex: HTTPError"]
    assert canary.failed(resolved, {"codex": {"missing": "no record"}}, ok) == ["codex: no record"]
    assert canary.failed(resolved, clean, canary.junit_results([])) == ["no contract test ran"]


def test_the_junit_command_fails_on_a_skip(tmp_path, capsys):
    assert canary.main(["junit", str(junit(tmp_path, {"a": None}))]) == 0
    assert canary.main(["junit", str(junit(tmp_path, {"a": None, "b": "skip"}))]) == 1
    assert "every CLI the lock pins must be installed" in capsys.readouterr().out


# ====================================================================== landing the bump PR
REPO = "Sean-Koval/hangar"
HEAD = "a" * 40


def github(**over):
    state = {"ref": canary.CANARY_BRANCH, "head_repo": REPO, "author": "hangar-agents[bot]", "state": "open",
             "reviews": [{"user": {"login": "Sean-Koval"}, "state": "APPROVED", "commit_id": HEAD}],
             "files": [{"filename": "src/hangar/harnesses.lock.yaml"},
                       {"filename": "tests/fixtures/protocol/codex-app-server.json"}],
             "arm": None, "auto_merge": None, "pushed_while_arming": False,
             "pushes": [{"activity_type": "force_push", "after": HEAD, "ref": f"refs/heads/{canary.CANARY_BRANCH}",
                         "actor": {"login": "hangar-agents[bot]", "type": "Bot"}}], **over}
    calls: list[tuple[str, str, object]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content) if req.content else None
        calls.append((req.method, req.url.path, body))
        p = req.url.path
        if p == f"/repos/{REPO}/pulls/7":
            armed = any(c[1] == "/graphql" for c in calls)
            head = "c" * 40 if armed and state["pushed_while_arming"] else HEAD
            return httpx.Response(200, json={
                "state": state["state"], "merged": False, "node_id": "PR_7", "user": {"login": state["author"]},
                "auto_merge": {"merge_method": "squash"} if armed and not state["arm"] else state["auto_merge"],
                "head": {"ref": state["ref"], "sha": head, "repo": {"full_name": state["head_repo"]}}})
        if p.endswith("/reviews"):
            return httpx.Response(200, json=state["reviews"])
        if p.endswith("/files"):
            return httpx.Response(200, json=state["files"])
        if p == f"/repos/{REPO}/activity":
            assert req.url.params["ref"] == f"refs/heads/{canary.CANARY_BRANCH}"
            return httpx.Response(200, json=state["pushes"])
        if p == "/graphql":
            if state["arm"]:
                return httpx.Response(200, json={"errors": [{"message": state["arm"]}]})
            return httpx.Response(200, json={"data": {"enablePullRequestAutoMerge": {"pullRequest": {"number": 7}}}})
        if p.endswith("/merge"):
            return httpx.Response(200, json={"merged": True})
        return httpx.Response(404)

    return httpx.Client(base_url="https://api.github.com", transport=httpx.MockTransport(handler)), calls


def test_an_approvers_approval_of_the_head_arms_auto_merge_for_that_commit():
    gh, calls = github()
    assert "auto-merge armed for aaaaaaaaaaaa after @sean-koval" in canary.land(gh, REPO, 7, ["Sean-Koval"])
    arm = next(b for m, p, b in calls if p == "/graphql")
    assert arm["variables"] == {"id": "PR_7", "sha": HEAD, "method": "SQUASH"}
    assert "expectedHeadOid: $sha" in arm["query"]


def test_a_push_while_arming_is_disarmed_again():
    gh, calls = github(pushed_while_arming=True)
    with pytest.raises(canary.LandRefused, match="head moved to cccccccccccc while arming"):
        canary.land(gh, REPO, 7, ["Sean-Koval"])
    graphql = [b["query"] for m, p, b in calls if p == "/graphql"]
    assert "enablePullRequestAutoMerge" in graphql[0] and "disablePullRequestAutoMerge" in graphql[1]


def test_an_already_mergeable_pr_is_merged_at_the_approved_head():
    gh, calls = github(arm="Pull request is in clean status")
    assert canary.land(gh, REPO, 7, ["sean-koval"]).startswith("merged aaaaaaaaaaaa")
    assert ("PUT", f"/repos/{REPO}/pulls/7/merge", {"sha": HEAD, "merge_method": "squash"}) in calls


@pytest.mark.parametrize(("over", "why"), [
    ({"ref": "feature/x"}, "not the canary's PR"),
    ({"head_repo": "someone/fork"}, "not the canary's PR"),
    ({"state": "closed"}, "isn't open"),
    ({"reviews": [{"user": {"login": "Sean-Koval"}, "state": "APPROVED", "commit_id": "b" * 40}]}, "no approval"),
    ({"reviews": [{"user": {"login": "mallory"}, "state": "APPROVED", "commit_id": HEAD}]}, "no approval"),
    ({"author": "Sean-Koval"}, "no approval"),  # the author can't be the approver
    ({"reviews": [{"user": {"login": "Sean-Koval"}, "state": "APPROVED", "commit_id": HEAD},
                  {"user": {"login": "Sean-Koval"}, "state": "DISMISSED", "commit_id": HEAD}]}, "no approval"),
    ({"reviews": [{"user": {"login": "Sean-Koval"}, "state": "APPROVED", "commit_id": HEAD},
                  {"user": {"login": "bob"}, "state": "CHANGES_REQUESTED", "commit_id": HEAD}]}, "changes requested"),
    ({"files": [{"filename": "src/hangar/harnesses.lock.yaml"}, {"filename": ".github/workflows/x.yml"}]},
     "more than the lock"),
    ({"files": [{"filename": "src/hangar/x.py", "previous_filename": "src/hangar/harnesses.lock.yaml"}]},
     "more than the lock"),
    # pytest collects tests/ recursively: a conftest.py or test file among the fixtures would run in CI
    ({"files": [{"filename": "tests/fixtures/protocol/conftest.py"}]}, "more than the lock"),
    ({"files": [{"filename": "tests/fixtures/protocol/test_x.py"}]}, "more than the lock"),
    ({"files": [{"filename": "src/hangar/harnesses.lock.yaml", "status": "removed"}]}, "removes"),
    # the head must be the canary's own push: an approver who pushed it would be approving their own change
    ({"pushes": [{"activity_type": "push", "after": HEAD, "ref": f"refs/heads/{canary.CANARY_BRANCH}",
                  "actor": {"login": "Sean-Koval", "type": "User"}}]}, "pushed by @Sean-Koval"),
    ({"pushes": []}, "no record of who pushed"),
])
def test_nothing_is_armed_unless_every_condition_holds(over, why):
    gh, calls = github(**over)
    with pytest.raises(canary.LandRefused, match=why):
        canary.land(gh, REPO, 7, ["Sean-Koval"])
    assert not [c for c in calls if c[1] == "/graphql" or c[1].endswith("/merge")]


def test_disarm_turns_auto_merge_off_only_when_it_is_on():
    gh, calls = github()
    canary.disarm(gh, REPO, 7)
    assert not [c for c in calls if c[1] == "/graphql"]
    gh, calls = github(auto_merge={"merge_method": "squash"})
    canary.disarm(gh, REPO, 7)
    assert "disablePullRequestAutoMerge" in next(b for m, p, b in calls if p == "/graphql")["query"]


def test_the_contract_installs_cover_every_tested_cli():
    """Every HANGAR_TEST_* a real-CLI test reads is one `install` sets (else CI would skip it, and fail on the skip)."""
    import re

    read = set()
    for f in Path(__file__).parent.glob("test_*.py"):
        read |= set(re.findall(r'environ\.get\("(HANGAR_TEST_[A-Z]+)"\)', f.read_text()))
    read.discard("HANGAR_TEST_ROOT")
    assert read <= set(canary.TEST_ENV.values()), read - set(canary.TEST_ENV.values())
    assert all(HARNESSES[h].install for h in canary.CONTRACT)
    assert pack_digest(HARNESSES["codex"])  # the stamp names it


# ====================================================================== the workflows' trust boundaries
def _jobs(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text())["jobs"]


def _writes(job: dict) -> bool:
    return any(v == "write" for v in (job.get("permissions") or {}).values())


def test_the_candidate_clis_never_run_where_a_write_token_or_a_secret_is():
    """The canary installs and runs unreviewed upstream releases (npm, new binaries). Any process in a job can read
    the job's token and every secret the job references from the runner, so those steps run in a job with a
    read-only token and no secrets, and the job that pushes and opens the PR runs none of them."""
    for name in ("harness-canary.yml", "harness-contract.yml"):
        for jname, job in _jobs(name).items():
            text = yaml.safe_dump(job)
            if "canary install" in text or "pytest" in text:
                assert not _writes(job), f"{name}:{jname} runs the CLIs with a write token"
                assert "secrets." not in text, f"{name}:{jname} runs the CLIs where a secret is"
    publish = [j for j in _jobs("harness-canary.yml").values() if _writes(j)]
    assert publish, "the canary still publishes"
    for job in publish:
        text = yaml.safe_dump(job)
        assert "canary install" not in text and "pytest" not in text and "setup-node" not in text
        assert "harnesses.lock.yaml" in text


def test_publish_checks_what_the_candidates_job_handed_it(tmp_path, capsys):
    """`publish` copies files written where the candidates ran: the lock must be a valid lock and the records JSON."""
    proto = tmp_path / "protocol"
    proto.mkdir()
    for f in ("codex-app-server.json", "claude-stream-json.json"):
        (proto / f).write_text((PROTOCOL / f).read_text())
    lock = tmp_path / "lock.yaml"
    lock.write_text(LOCK_FILE.read_text())
    assert canary.main(["check", "--lock", str(lock), "--protocol", str(proto)]) == 0
    lock.write_text(LOCK_FILE.read_text() + "evil:\n  version: 1.0.0\n")
    assert canary.main(["check", "--lock", str(lock), "--protocol", str(proto)]) == 1
    lock.write_text(LOCK_FILE.read_text())
    (proto / "claude-stream-json.json").write_text("[1, 2")
    assert canary.main(["check", "--lock", str(lock), "--protocol", str(proto)]) == 1
    (proto / "claude-stream-json.json").unlink()
    assert canary.main(["check", "--lock", str(lock), "--protocol", str(proto)]) == 1
    assert "claude-stream-json.json" in capsys.readouterr().err
