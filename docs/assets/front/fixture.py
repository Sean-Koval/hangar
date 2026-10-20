"""A busy afternoon on one hangar, for the front page's console screenshots and the review-loop recording.

The demo hangar (demo.py) runs one real ticket end to end, which is what the recording shows; a team's console on a
Tuesday has four environments, six people and twenty-odd sandboxes in every state, which no scripted run produces
in the time a CI job has. `stub()` is that afternoon as the JSON the console's API returns (sandboxes, jobs, sessions,
people, usage, environments, the inbox, a patch), and `wire(page)` serves it over the demo server with Playwright's
request interception, so every pixel is the real console drawing fixture data. Decisions and messages the recording
makes are intercepted too and change the fixture, so the console shows the result as it would from the server.

    from fixture import Fixture
    fx = Fixture(); await fx.wire(page)
"""

# ruff: noqa: E501 - one sandbox per line reads better than wrapped rows
from __future__ import annotations

import hashlib
import json
import time

ENVS = [
    ("base-codex", "https://github.com/acme/checkout-api.git"),
    ("checkout-python", "https://github.com/acme/checkout-api.git"),
    ("rust-1.83-gpu", "https://github.com/acme/inference.git"),
    ("research", "https://github.com/acme/notebooks.git"),
]
PEOPLE = [
    ("sean", "Sean Koval"), ("priya", "Priya Raman"), ("marco", "Marco Bianchi"),
    ("dana", "Dana Whitfield"), ("li", "Li Wei"), ("omar", "Omar Haddad"),
]
HELD = "held"
# name, env, owner (None: hangar), kind, harness, sandbox state, job/session phase, age in minutes, title, extra
ROWS = [
    ("push-refunds", "base-codex", "sean", "job", "codex", "running", "running", 41, "Push the refunds migration to main",
     {HELD: ("Bash", "git push origin main", "after reading untrusted content")}),
    ("flaky-payments", "base-codex", "priya", "job", "claude-code", "running", "needs_input", 26, "Fix the flaky payments test",
     {"question": "Retry the whole test, or only the client call?"}),
    ("stripe-key", "base-codex", "omar", "job", "codex", "running", "running", 12, "Rotate the Stripe key in CI",
     {"denied": ("WebFetch", "https://hooks.example.net/collect", "deny-secrets-egress: no receipt issued")}),
    ("orders-pagination", "base-codex", "priya", "job", "claude-code", "running", "running", 5, "Add pagination to GET /orders", {}),
    ("tax-rules", "base-codex", "marco", "session", "codex", "running", "running", 24, "tax-rules", {}),
    ("partial-refunds", "base-codex", "sean", "session", "codex", "running", "idle", 27, "partial-refunds", {}),
    ("rate-limits", "base-codex", "dana", "job", "codex", "starting", "provisioning", 1, "Add rate limits to /refunds", {}),
    ("refunds-spike", "base-codex", "sean", "sandbox", "codex", "paused", None, 95, "refunds-spike", {}),
    ("lint-sweep", "base-codex", "li", "job", "codex", "running", "delivering", 33, "Fix the new ruff findings", {}),
    ("migrate-orders", "checkout-python", "marco", "sandbox", "claude-code", "error", None, 8, "migrate-orders",
     {"error": "image build failed: pip install exited 1 (psycopg2 needs pg_config)"}),
    ("report-query", "checkout-python", "dana", "job", "claude-code", "running", "failed", 52, "Speed up the orders report query", {"verify": 1}),
    ("timezone-invoices", "checkout-python", "priya", "job", "codex", "running", "verifying", 19, "Fix the timezone bug in invoices", {}),
    ("openapi-examples", "checkout-python", "li", "job", "claude-code", "running", "running", 31, "Add OpenAPI examples", {}),
    ("csv-export", "checkout-python", "sean", "sandbox", "claude-code", "running", None, 64, "csv-export", {"runs": True}),
    ("receipts-pdf", "checkout-python", "omar", "session", "codex", "running", "running", 14, "receipts-pdf", {}),
    ("docs-pass", "checkout-python", "dana", "sandbox", "codex", "paused", None, 180, "docs-pass", {}),
    ("simd-tokenizer", "rust-1.83-gpu", "priya", "job", "claude-code", "running", "verifying", 38, "Port the tokenizer to SIMD", {}),
    ("flash-attn", "rust-1.83-gpu", "omar", "session", "codex", "running", "running", 22, "bench-flash-attn", {}),
    ("borrow-planner", "rust-1.83-gpu", "dana", "job", "claude-code", "running", "delivering", 17, "Fix the borrow error in the planner", {}),
    ("gpu-smoke", "rust-1.83-gpu", "li", "job", "codex", "starting", "queued", 1, "GPU smoke tests", {}),
    ("kv-cache", "rust-1.83-gpu", "marco", "job", "codex", "running", "running", 46, "Shrink the KV cache for long prompts",
     {HELD: ("Edit", "Cargo.toml: bump candle-core 0.8 to 0.9", None)}),
    ("retrieval-eval", "research", "priya", "session", "claude-code", "paused", None, 190, "retrieval-eval", {}),
    ("nightly-notebooks", "research", None, "job", "codex", "running", "running", 9, "Re-run the nightly notebooks", {"requester": "schedule:nightly"}),
    ("embeddings-sweep", "research", "sean", "session", "claude-code", "running", "running", 58, "embeddings-sweep", {}),
    ("chart-styles", "research", "li", "sandbox", "codex", "running", None, 12, "chart-styles", {}),
    ("dedupe-orders", "base-codex", "marco", "job", "claude-code", "running", "succeeded", 70, "Deduplicate orders on import", {}),
]

# What each agent is doing, for GET /api/fleet (KAN-140): its stage, the progress it reported itself (None: the server
# estimates from time, which the console draws dotted), the estimate, the now-line, files and commits so far, and how
# long ago it last did something. Rows not listed have no stage (idle, paused, a sandbox nobody drives).
LIVE = {
    "push-refunds": ("pr", None, 92, None, 6, 3, 300),
    "flaky-payments": ("test", None, 60, None, 3, 2, 1500),
    "stripe-key": ("edit", None, 45, "choosing another way to rotate the key", 2, 1, 40),
    "orders-pagination": ("edit", 40, None, "editing api/orders.py", 2, 0, 3),
    "tax-rules": ("plan", None, 70, None, 0, 0, 360),
    "report-query": ("test", None, 80, None, 4, 2, 600),
    "timezone-invoices": ("test", None, 35, "hangar runs the check: pytest -q", 3, 2, 20),
    "openapi-examples": ("edit", None, 55, "editing docs/openapi/orders.yaml", 5, 1, 9),
    "receipts-pdf": ("test", 70, None, "running pytest -q tests/test_receipts.py, 38 passed", 4, 2, 2),
    "simd-tokenizer": ("test", None, 50, "hangar runs the check: cargo test --release", 7, 4, 14),
    "flash-attn": ("plan", None, 30, "reading kernels/attention.rs", 0, 0, 6),
    "borrow-planner": ("pr", None, 60, "pushing hangar/borrow-planner and opening the PR", 2, 1, 11),
    "kv-cache": ("edit", None, 75, None, 3, 1, 240),
    "nightly-notebooks": ("edit", None, 20, "running notebooks/orders_daily.ipynb", 1, 0, 25),
    "embeddings-sweep": ("test", 55, None, "running python eval.py --sweep dims=256,512", 2, 3, 4),
    "lint-sweep": ("pr", None, 80, "opening the PR on acme/checkout-api", 11, 2, 18),
    "dedupe-orders": ("pr", 100, None, "draft PR #418 opened", 4, 3, 3500),
}
QUESTION_OPTIONS = {
    "flaky-payments": ["Retry the whole test", "Only the client call", "Skip it, file an issue"],
    "tax-rules": ["orders (Postgres 15)", "orders_legacy (Postgres 12)", "Both, legacy first"],
}
STAGE_SHARE = {"plan": 0.12, "edit": 0.5, "test": 0.25, "pr": 0.13}

# the change on the held job, as GET /api/jobs/{id}/patch returns it
PATCH = """diff --git a/src/auth/refresh.py b/src/auth/refresh.py
index 3f1a2b4..a41f2c0 100644
--- a/src/auth/refresh.py
+++ b/src/auth/refresh.py
@@ -118,7 +118,9 @@ def refresh(token: Token) -> Token:
     now = time.time()
-    if token.expires_at < now:
+    if token.expires_at - EARLY_REFRESH_S < now:
         return _issue(token.subject)
+    if token.revoked:
+        raise Revoked(token.subject)
     return token


@@ -140,6 +142,10 @@ def _issue(subject: str) -> Token:
     claims = {"sub": subject, "iat": int(time.time())}
+    claims["exp"] = claims["iat"] + TTL_S
+    if subject in _PINNED:
+        claims["pin"] = _PINNED[subject]
+    _audit(subject)
     return Token.sign(claims)
diff --git a/tests/test_refresh.py b/tests/test_refresh.py
index 9c0d1e2..b7e6f31 100644
--- a/tests/test_refresh.py
+++ b/tests/test_refresh.py
@@ -38,3 +38,9 @@ def test_refresh_keeps_subject():
     t = refresh(issue("ana"))
     assert t.subject == "ana"
+
+
+def test_refresh_early():
+    t = issue("ana", expires_in=EARLY_REFRESH_S - 1)
+    assert refresh(t) is not t
"""


def hexid(s: str, n: int = 8) -> str:
    return hashlib.sha1(s.encode()).hexdigest()[:n]


def stub(now: float | None = None) -> dict:
    """The afternoon as the console's API would return it, keyed by what each route serves."""
    now = now or time.time()
    sandboxes, jobs, sessions, usage = [], [], [], []
    for i, (name, env, owner, kind, harness, sb_state, phase, age, title, extra) in enumerate(ROWS):
        repo = dict(ENVS)[env]
        created = now - age * 60
        sid = "sb-" + hexid(name)
        job_id = "job-" + hexid("j" + name) if kind == "job" else None
        ses_id = "ses-" + hexid("s" + name) if kind in ("job", "session") else None
        created_by = f"job:{job_id}" if kind == "job" else f"session:{ses_id}" if kind == "session" else f"ui:{owner}"
        runs = [{"id": "run-" + hexid("r" + name + str(k)), "harness": harness, "started_at": now - (40 - k * 15) * 60, "prompt": p}
                for k, p in enumerate(["Reproduce the CSV unicode bug with a test", "Fix it and keep the old function name"])] if extra.get("runs") else []
        sandboxes.append({
            "id": sid, "name": name if kind == "sandbox" else f"ses-{name}-{hexid(name, 4)}", "environment": env, "provider": "docker",
            "state": sb_state, "repo": repo, "branch": None, "credentials": ["claude-team"] if harness == "claude-code" else ["codex-api"],
            "labels": {}, "resources": {"cpus": 2.0, "memory_gb": 4.0}, "parent": "sb-" + hexid("csv-export") if name == "chart-styles" else None,
            "created_at": created, "expires_at": created + 4 * 3600, "last_activity": now - 30, "error": extra.get("error"),
            "created_by": created_by, "person": owner if kind == "sandbox" else None,
            "info": {"harness": harness, "work_branch": f"agent/{name}", "runs": runs, "warm": i % 5 == 0},
        })
        turns: list[dict] = []
        if ses_id:
            approvals = []
            if extra.get(HELD):
                tool, detail, taint = extra[HELD]
                approvals.append({"id": "apr-" + hexid(name), "tool": tool, "detail": detail, "status": "pending",
                                  "requested_at": now - 300, "expires_at": now + 3000, "taint": taint})
            if extra.get("denied"):
                tool, detail, msg = extra["denied"]
                approvals.append({"id": "apr-" + hexid(name), "tool": tool, "detail": detail, "status": "denied", "by": "policy",
                                  "message": msg, "requested_at": now - 200, "decided_at": now - 199})
            live = phase in ("running", "needs_input") or (kind == "session" and phase == "running")
            turns.append({"message": title if kind == "job" else f"Work on {name}", "approvals": approvals,
                          "finished_at": None if live else now - 120, "started_at": created + 60, "run_id": "run-x", "live": live,
                          "harness": harness, "mode": "new"})
            pending = [a for a in approvals if a["status"] == "pending"]
            ses_state = "running" if phase == "running" else "idle" if sb_state == "running" else "suspended" if sb_state == "paused" else "starting"
            sessions.append({
                "id": ses_id, "name": name if kind == "session" else job_id, "title": title if kind == "session" else "",
                "state": ses_state, "owner": "jobs" if kind == "job" else f"ui:{owner}", "person": owner,
                "sandbox_id": sid, "job_id": job_id, "harness": harness, "model": "gpt-5.1" if harness == "codex" else "claude-sonnet-5",
                "needs_input": extra.get("question"), "busy": phase == "running", "turns": turns, "pending_approvals": pending,
                "created_at": created, "last_active_at": now - 60, "checkpoint_count": 1, "last_checkpoint_at": now - 400, "checkpoints": [],
                "environment": env, "repo": repo, "branch": f"hangar/{name}", "idle_minutes": 30, "usage": None,
            })
            usage.append({"key": name if kind == "session" else job_id, "cost_usd": round(0.15 + (i % 7) * 0.37, 2)})
        if job_id:
            events = [{"ts": created, "state": "queued", "message": "queued"}, {"ts": created + 40, "state": "provisioning", "message": "provisioning sandbox"},
                      {"ts": created + 70, "state": "running", "message": "agent turn 1 started"}]
            result: dict = {"question": extra.get("question"), "verify_exit_code": extra.get("verify")}
            if phase == "needs_input":
                events.append({"ts": now - 1500, "state": "needs_input", "message": f"agent asked: {extra['question']}"})
            if phase == "failed":
                result.update(verify_tail="FAILED tests/test_report.py::test_totals - assert 41 == 38\n1 failed, 37 passed in 4.21s",
                              summary="Rewrote the report query with a window function; one total still disagrees with the fixture.")
                events.append({"ts": now - 600, "state": "failed", "message": "check failed (exit 1)"})
            if extra.get(HELD):
                events.append({"ts": now - 300, "state": "running", "message": f"approval requested: {extra[HELD][1]}"})
            if phase == "succeeded":
                result.update(summary="Orders are deduplicated on import by (shop, external_id); the importer logs what it dropped.",
                              pr_url="https://github.com/acme/checkout-api/pull/418", pr_number=418, verify_exit_code=0)
                events.append({"ts": now - 3600, "state": "succeeded", "message": "draft PR #418 opened"})
            jobs.append({
                "id": job_id, "title": title, "state": phase, "active": phase not in ("succeeded", "failed", "cancelled"),
                "reason": "verify_failed" if phase == "failed" else None, "requester": extra.get("requester") or f"ui:{owner}",
                "person": owner, "source": "api", "sandbox_id": sid, "session_id": ses_id, "environment": env, "harness": harness,
                "model": "gpt-5.1" if harness == "codex" else "claude-sonnet-5", "repo": repo, "branch": f"hangar/{job_id}",
                "instructions": f"{title}. Keep the change small, add a regression test, and explain what you changed in the PR.",
                "result": result, "approval": None,
                "pending_approvals": [a for a in (turns[0]["approvals"] if turns else []) if a["status"] == "pending"],
                "created_at": created, "events": events, "turns": [], "context": [], "usage": None,
            })
    # one job from outside the team, waiting for a trusted person before it starts
    for j in jobs:
        if j["title"] == "Add rate limits to /refunds":
            j.update(state="awaiting_approval", active=True, requester="github:rando", person=None,
                     approval={"requested_by": "github:rando", "reason": "github:rando isn't on this repo's trusted list", "expires_at": now + 5 * 3600})
            j["events"].append({"ts": now - 60, "state": "awaiting_approval", "message": "waiting for approval: github:rando isn't on this repo's trusted list"})
            for sb in sandboxes:
                if sb["id"] == j["sandbox_id"]:
                    sb["state"] = "pending"
    # a session whose agent stopped to ask, and a busy one with a steer on its way
    for s in sessions:
        if s["name"] == "tax-rules":
            s.update(state="idle", busy=False, last_active_at=now - 360,
                     needs_input="Which database should the migration target? The repo has orders (Postgres 15) and orders_legacy (Postgres 12).")
            s["turns"][-1].update(finished_at=now - 360, live=True)
        if s["name"] == "receipts-pdf":
            s["turns"][-1].update(live=True, steers=[{"id": "str-0a1b2c3d", "text": "Use the legacy table and keep the old index.", "status": "sending", "ts": now - 5}])
        if s["name"] == "partial-refunds":
            s["turns"] = [{"message": "Design partial refunds: refund line items, not whole orders.", "harness": "codex", "mode": "new",
                           "exit_code": 0, "started_at": now - 1800, "summary": "Sketched partial refunds in REFUNDS.md; next: the API shape.",
                           "approvals": [], "steers": [], "live": False, "finished_at": now - 1700, "run_id": "run-x"}]
            s["last_summary"] = s["turns"][0]["summary"]
            s["harness_session"] = "a2f95019-0c25-430f-b0f9-3c23ecf361d4"
            s["resume_command"] = "codex resume a2f95019"
    people = [{"id": p, "name": n, "role": "admin" if p == "sean" else "member"} for p, n in PEOPLE]
    return {"now": now, "sandboxes": sandboxes, "jobs": jobs, "sessions": sessions, "people": people,
            "usage": {"group_by": "session", "since": now - 3600, "rows": usage, "total": {}},
            "envs": [{"name": e, "repo": r, "harnesses": ["codex", "claude-code"], "images": {"docker": f"hangar-env/{e}:abc"}} for e, r in ENVS]}


class Fixture:
    """The afternoon, served over the demo server; decisions and messages change it like the server would."""

    def __init__(self) -> None:
        self.data = stub()
        self.now = self.data["now"]
        self.sent: list[dict] = []  # what the recording sent to agents (comments, answers)
        self.acked: set[str] = set()  # inbox items put away (POST /api/inbox/{id}/ack)

    # ---- what the inbox lists from this data, as GET /api/inbox does
    def inbox(self) -> dict:
        now, items = self.now, []
        for j in self.data["jobs"]:
            ref = {"job": j["id"], "title": j["title"], "repo": j["repo"]}
            if j["state"] == "needs_input" and j["result"].get("question"):
                items.append({"kind": "question", "reason": f"the agent asks: {j['result']['question']}", "since": now - 1500, **ref, "id": f"inb-q-{j['id']}",
                              "question_options": QUESTION_OPTIONS.get(self.row_of(j["sandbox_id"]), [])})
            elif j["state"] == "awaiting_approval":
                items.append({"kind": "approval", "reason": f"job waits for approval: {j['approval']['reason']}", "since": now - 60,
                              "expires_at": j["approval"]["expires_at"], **ref, "id": f"inb-a-{j['id']}"})
            elif j["state"] == "failed":
                items.append({"kind": "failed", "reason": "job failed (verify_failed)", "since": now - 600, **ref, "id": f"inb-f-{j['id']}"})
            elif j["state"] == "succeeded" and j["result"].get("pr_url"):
                items.append({"kind": "review", "reason": f"PR #{j['result']['pr_number']} waits for your review", "since": now - 3600, **ref,
                              "pr": j["result"]["pr_url"], "id": f"inb-r-{j['id']}", "links": {"pr": j["result"]["pr_url"]}})
            for a in j.get("pending_approvals") or []:
                items.append({"kind": "approval", "reason": f"the agent wants to run {a['tool']}: {a['detail']}", "since": a["requested_at"],
                              "expires_at": a.get("expires_at"), "session": j["id"], "job": j["id"], "approval": a["id"], "id": f"inb-t-{a['id']}"})
        for s in self.data["sessions"]:
            turn = (s.get("turns") or [{}])[-1]
            for a in turn.get("approvals") or []:  # KAN-140: policy stopped a step; the agent carries on without it
                if a["status"] == "denied" and a.get("by") == "policy" and not turn.get("finished_at"):
                    items.append({"kind": "denied", "reason": f"policy denied {a['tool']}: {a['detail']} ({a.get('message') or ''})", "since": a.get("decided_at") or now,
                                  "session": s["name"], "job": s["job_id"], "approval": a["id"], "tool": a["tool"], "id": f"inb-d-{a['id']}"})
            if s["job_id"]:
                continue
            for a in s.get("pending_approvals") or []:
                items.append({"kind": "approval", "reason": f"the agent wants to run {a['tool']}: {a['detail']}", "since": a["requested_at"],
                              "expires_at": a.get("expires_at"), "session": s["name"], "approval": a["id"], "id": f"inb-t-{a['id']}"})
            if s.get("needs_input"):
                items.append({"kind": "question", "reason": f"the agent asks: {s['needs_input']}", "since": s["last_active_at"], "session": s["name"], "id": f"inb-q-{s['id']}",
                              "question_options": QUESTION_OPTIONS.get(s["name"], [])})
        items = [i for i in items if i["id"] not in self.acked]
        for i in items:
            i.setdefault("links", {})
        items.sort(key=lambda i: i["since"], reverse=True)
        counts: dict[str, int] = {}
        for i in items:
            counts[i["kind"]] = counts.get(i["kind"], 0) + 1
        return {"person": "sean", "counts": counts, "items": items}

    def patch(self) -> dict:
        return {"session": "x", "branch": "hangar/x", "base": "fed98cfd0312", "head": "429461a6ec9a", "source": "sandbox",
                "as_of": self.now - 20, "bytes": len(PATCH), "truncated": False, "files": [], "patch": PATCH}

    def decide(self, approval_id: str, decision: str, message: str | None, by: str = "sean") -> None:
        """A decision on a held tool call: the pending approval becomes a verdict, as the server would record it."""
        status = "denied" if decision == "deny" else "allowed_always" if decision == "allow_always" else "allowed"
        for s in self.data["sessions"]:
            for t in s["turns"]:
                for a in t["approvals"]:
                    if a["id"] == approval_id and a["status"] == "pending":
                        a.update(status=status, by=by, message=message or "", decided_at=time.time())
            s["pending_approvals"] = [a for a in s["pending_approvals"] if a["id"] != approval_id]
        for j in self.data["jobs"]:
            j["pending_approvals"] = [a for a in j["pending_approvals"] if a["id"] != approval_id]

    def row_of(self, sandbox_id: str) -> str:
        """The ROWS name a sandbox was made from."""
        return next((r[0] for r in ROWS if "sb-" + hexid(r[0]) == sandbox_id), "")

    # ---- GET /api/fleet, as fleet_api.FleetRead builds it from this data (docs/api/fleet.md)
    def fleet(self, owner: str | None = None, include_stopped: bool = False) -> dict:
        now, d = self.now, self.data
        ib = self.inbox()
        jobs = {j["id"]: j for j in d["jobs"]}
        sessions = {s["id"]: s for s in d["sessions"]}
        names = dict(PEOPLE)
        units = []
        for sb in d["sandboxes"]:
            if sb["state"] == "stopped" and not include_stopped:
                continue
            row = self.row_of(sb["id"])
            made = sb["created_by"]
            job = jobs.get(made.removeprefix("job:")) if made.startswith("job:") else None
            ses = sessions.get(job["session_id"]) if job else sessions.get(made.removeprefix("session:"))
            person = job["person"] if job else ses["person"] if ses else sb["person"]
            live_sb = sb["state"] in ("running", "paused", "starting", "pending")
            turn = (ses or {}).get("turns", [{}])[-1] if ses and ses.get("turns") else {}
            pending = (ses or {}).get("pending_approvals") or []
            denial = next((a for a in reversed(turn.get("approvals") or []) if a["status"] == "denied" and a.get("by") == "policy"), None) \
                if turn and not turn.get("finished_at") else None
            question = (job or {}).get("result", {}).get("question") if job else (ses or {}).get("needs_input")
            role = {"pending": "queued", "starting": "queued", "running": "running", "paused": "off", "stopped": "off", "error": "failed"}[sb["state"]]
            label = {"pending": "Starting", "starting": "Starting", "running": "Running", "paused": "Paused", "stopped": "Spun down", "error": "Failed"}[sb["state"]]
            reason = ask = None
            if sb["state"] == "error":
                reason = {"title": "Error", "text": sb["error"]}
            elif job and job["state"] == "awaiting_approval":
                role, label, ask = "approval", "Needs approval", {"kind": "job", "job": job["id"]}
                reason = {"title": "Held", "text": job["approval"]["reason"]}
            elif pending and live_sb:
                a = pending[0]
                role, label = "approval", "Needs approval"
                ask = {"kind": "tool", "approval": a["id"], "job": job["id"] if job else None, "session": ses["id"]}
                reason = {"title": "Held", "text": f"{a['tool']}: {a['detail']}" + (f" ({a['taint']})" if a.get("taint") else "")}
            elif question and live_sb and ((job and job["state"] == "needs_input") or (ses and not job and ses["state"] != "running")):
                role, label = "approval", "Needs input"
                ask = {"kind": "question", "job": job["id"] if job else None, "session": ses["id"] if ses else None}
                reason = {"title": "Question", "text": question}
            elif denial and live_sb:
                role, label = "denied", "Denied"
                reason = {"title": "Policy", "text": f"{denial['tool']}: {denial['detail']} denied. {denial.get('message') or ''}".strip()}
            elif job and job["state"] == "failed" and live_sb:
                role, label = "failed", "Failed"
                reason = {"title": "Failed", "text": "The check failed (exit 1): tests/test_report.py::test_totals, assert 41 == 38."}
            lv = LIVE.get(row) if role not in ("off", "queued") else None
            age = now - sb["created_at"]
            stage = pct = est = None
            stages: list[dict] = []
            now_line, files, commits, quiet = None, 0, 0, max(30, now - sb["last_activity"])
            if lv:
                stage, pct, est_pct, now_line, files, commits, quiet = lv
                k = ("plan", "edit", "test", "pr").index(stage)
                t = sb["created_at"] + 60
                for i, st in enumerate(("plan", "edit", "test", "pr")[: k + 1]):
                    span = (age - 60) * STAGE_SHARE[st] / sum(STAGE_SHARE[x] for x in ("plan", "edit", "test", "pr")[: k + 1])
                    stages.append({"stage": st, "started_at": t, "ended_at": None if i == k else t + span})
                    t += span
                if pct is None:
                    est = {"pct": est_pct, "elapsed_s": round(now - stages[-1]["started_at"]), "typical_s": round((now - stages[-1]["started_at"]) * 100 / max(est_pct, 1)),
                           "basis": f"median of 9 recent {stage} stages", "samples": 9}
            h = int(hexid(row or sb["id"], 6), 16)
            waiting = role in ("approval", "failed", "denied")
            buckets = [0] * 12 if not lv else [((h >> (i * 2)) % 9 + 2) * (1400 + (h % 7) * 300) for i in range(12)]
            if waiting and lv:
                buckets = [v if i < 8 else 0 for i, v in enumerate(buckets)]
            cost = next((u["cost_usd"] for u in d["usage"]["rows"] if u["key"] in (row, (job or {}).get("id"))), None)
            tokens = sum(buckets) * 3 if lv else 0
            mine = [i["id"] for i in ib["items"] if (job and i.get("job") == job["id"]) or (ses and i.get("session") == ses["name"])]
            exp = next((i.get("expires_at") for i in ib["items"] if i["id"] in mine and i.get("expires_at")), None)
            u = {
                "id": sb["id"], "tail": sb["id"][-4:], "name": sb["name"], "environment": sb["environment"], "provider": sb["provider"],
                "state": sb["state"], "kind": "job" if job else "session" if ses else "sandbox", "job": job["id"] if job else None,
                "job_state": job["state"] if job else None,
                "session": {"id": ses["id"], "name": ses["name"], "state": ses["state"]} if ses else None,
                "owner": person, "actor": (job or {}).get("requester") or (ses or {}).get("owner") or sb["created_by"],
                "harness": sb["info"]["harness"], "model": (job or ses or {}).get("model"), "repo": sb["repo"],
                "branch": (job or ses or {}).get("branch") or sb["info"].get("work_branch"),
                "role": role, "label": label, "reason": reason, "ask": ask, "live": live_sb,
                "work": (job["title"] if job else None) or (ses["title"] or ses["name"] if ses else None) or sb["name"],
                "task": (job or {}).get("instructions") or "", "question": question if role == "approval" and label == "Needs input" else None,
                "question_options": QUESTION_OPTIONS.get(row, []) if label == "Needs input" else [],
                "now": now_line, "last_activity_at": now - quiet,
                "stage": stage, "stage_pct": pct, "stage_estimate": est, "stages": stages,
                "files_changed": files, "commits": commits,
                "usage": {"input_tokens": tokens, "output_tokens": tokens // 20, "cache_read_tokens": tokens * 6, "cache_write_tokens": tokens // 10,
                          "cost_usd": cost} if cost is not None else None,
                "tokens_1h": buckets, "pr": (job or {}).get("result", {}).get("pr_url"), "created_at": sb["created_at"], "expires_at": sb["expires_at"],
                "turn": len((ses or {}).get("turns") or []), "inbox": mine, "ask_expires_at": exp,
            }
            if owner is not None and (u["owner"] or "none") != owner:
                continue
            units.append(u)
        rank = {r: i for i, r in enumerate(("approval", "failed", "denied", "running", "queued", "off"))}
        units.sort(key=lambda u: (rank[u["role"]], u["created_at"], u["id"]))
        counts = {r: sum(1 for u in units if u["role"] == r) for r in rank}
        owners: dict[str, int] = {}
        for u in units:
            owners[u["owner"] or "none"] = owners.get(u["owner"] or "none", 0) + 1
        return {"at": now, "person": "sean", "units": units, "counts": counts,
                "owners": [{"id": k, "name": names.get(k) if k != "none" else None, "count": n} for k, n in sorted(owners.items(), key=lambda kv: (-kv[1], kv[0]))],
                "inbox": {"counts": ib["counts"], "items": ib["items"]},
                "slots": {"used": sum(1 for u in units if u["live"]), "max": 120}}

    def events(self, ref: str) -> list[dict]:
        """An agent's RunEvents (GET /api/{jobs,sessions}/{id}/events) for its timeline: reads, edits, commands, progress."""
        sb = next((x for x in self.data["sandboxes"] if ref in (x["created_by"].split(":", 1)[-1],) or ref == x["id"]), None)
        job = next((j for j in self.data["jobs"] if j["id"] == ref), None)
        ses = next((x for x in self.data["sessions"] if ref in (x["id"], x["name"])), None)
        sid = (job or {}).get("sandbox_id") or (ses or {}).get("sandbox_id") or (sb or {}).get("id")
        u = next((x for x in self.fleet()["units"] if x["id"] == sid), None)
        if not u or not u["stages"]:
            return []
        out: list[dict] = []
        files = ["checkout/refunds.py", "checkout/models.py", "tests/test_refunds.py", "checkout/export.py"]
        for st in u["stages"]:
            t0, t1 = st["started_at"], st["ended_at"] or self.now - 20
            if st["stage"] == "plan":
                out += [{"ts": t0 + 5, "kind": "command", "text": "rg -n 'def refund' checkout"}, {"ts": t0 + 40, "kind": "progress", "text": "Plan: change the rounding, then add a test"}]
            elif st["stage"] == "edit":
                out += [{"ts": t0 + 10 + i * 20, "kind": "file_change", "text": f, "data": {"path": f}} for i, f in enumerate(files[: max(1, u["files_changed"])])]
                if u["commits"]:
                    out.append({"ts": (t0 + t1) / 2 + 30, "kind": "command", "text": 'git commit -m "Round refunds half-even"'})
            elif st["stage"] == "test":
                out += [{"ts": t0 + 4, "kind": "command", "text": "pytest -q"}, {"ts": t0 + 50, "kind": "progress", "text": "pytest: 202 passed, 1 failed" if u["role"] == "failed" else "pytest: 202 passed"}]
            elif st["stage"] == "pr":
                out += [{"ts": t0 + 6, "kind": "command", "text": "git push origin main" if u["role"] == "approval" else "git push -u origin HEAD"}]
        return [{"seq": i + 1, "turn": 1, "run_id": "run-x", **e} for i, e in enumerate(sorted(out, key=lambda e: e["ts"]))]

    # ---- Playwright wiring
    async def wire(self, page) -> None:
        d = self.data

        async def fixed(route, body, status=200):
            await route.fulfill(status=status, content_type="application/json", body=json.dumps(body))

        async def overview(route):
            r = await route.fetch()
            o = await r.json()
            o.update(max_sandboxes=120, person_caps={"sandboxes": 20, "sessions": 15})
            await route.fulfill(response=r, json=o)

        async def decided(route):
            body = json.loads(route.request.post_data or "{}")
            self.decide(route.request.url.rsplit("/", 1)[-1], body.get("decision", "allow"), body.get("message"))
            await fixed(route, {"ok": True})

        async def sent(route):
            body = json.loads(route.request.post_data or "{}")
            self.sent.append({"url": route.request.url, **body})
            await fixed(route, {"ok": True, "status": "queued", "id": "str-" + hexid(str(len(self.sent)))})

        await page.route("**/api/jobs/*/patch", lambda r: fixed(r, self.patch()))
        await page.route("**/api/sessions/*/patch", lambda r: fixed(r, self.patch()))
        await page.route("**/api/inbox", lambda r: fixed(r, self.inbox()))
        await page.route("**/api/sandboxes?*", lambda r: fixed(r, d["sandboxes"]))
        await page.route("**/api/jobs?limit=200", lambda r: fixed(r, d["jobs"]))
        await page.route("**/api/sessions?limit=200", lambda r: fixed(r, d["sessions"]))
        await page.route("**/api/people", lambda r: fixed(r, d["people"]))
        await page.route("**/api/usage?group_by=session*", lambda r: fixed(r, d["usage"]))
        await page.route("**/api/environments", lambda r: fixed(r, d["envs"]))
        await page.route("**/api/overview", overview)
        await page.route("**/api/jobs/*/approvals", lambda r: fixed(r, {"live": True, "approvals": []}))
        await page.route("**/api/jobs/*/approvals/*", decided)
        await page.route("**/api/sessions/*/approvals/*", decided)
        await page.route("**/api/jobs/*/steer", sent)
        await page.route("**/api/jobs/*/follow-up", sent)
        await page.route("**/api/sessions/*/send", sent)
        events = [{"seq": 1, "ts": self.now - 900, "kind": "message", "text": "Looking at the refund flow."},
                  {"seq": 2, "ts": self.now - 800, "kind": "command", "text": "python3 -m unittest -q"},
                  {"seq": 3, "ts": self.now - 700, "kind": "tool_result", "text": "Ran 41 tests in 4.2s OK"}]

        async def activity(route):  # the first call tails; later polls ask for what is new since the cursor: nothing
            new = events if "tail=" in route.request.url else []
            await fixed(route, {"events": new, "next": len(events)})

        async def agent_events(route):  # the opened agent's timeline: everything on the first read, nothing new after
            ref = route.request.url.split("/events")[0].rsplit("/", 1)[-1]
            evs = self.events(ref)
            new = evs if "tail=" in route.request.url else []
            await fixed(route, {"events": new, "next": len(evs)})

        await page.route("**/api/jobs/*/events?*", agent_events)
        await page.route("**/api/sessions/*/events?*", agent_events)

        from urllib.parse import parse_qs, urlparse

        async def fleet_read(route):
            q = parse_qs(urlparse(route.request.url).query)
            await fixed(route, self.fleet((q.get("owner") or [None])[0], (q.get("include_stopped") or ["false"])[0] == "true"))

        async def ack(route):
            self.acked.add(route.request.url.split("/api/inbox/")[1].split("/")[0])
            await fixed(route, {"ok": True})

        def job_of(url: str) -> dict | None:
            jid = url.split("/api/jobs/")[1].split("/")[0].split("?")[0]
            return next((j for j in d["jobs"] if j["id"] == jid), None)

        async def retry(route):
            j = job_of(route.request.url)
            if j and "fresh=1" not in route.request.url:
                j.update(state="running", active=True, reason=None)
            await fixed(route, {"id": "job-" + hexid("again" + (j or {}).get("id", "")), "mode": "fresh" if "fresh=1" in route.request.url else "same"})

        async def job_decided(route):
            j = job_of(route.request.url)
            if j:
                j.update(state="queued" if route.request.url.endswith("/approve") else "cancelled", approval=None)
            await fixed(route, {"ok": True})

        async def answered(route):
            j = job_of(route.request.url) if "/api/jobs/" in route.request.url else None
            if j:
                j.update(state="running")
                j["result"]["question"] = None
            await sent(route)

        async def one_job(route):
            j = job_of(route.request.url)
            await fixed(route, j or {"error": "no such job"}, 200 if j else 404)

        await page.route("**/api/jobs/job-*", one_job)
        await page.route("**/api/fleet?*", fleet_read)
        await page.route("**/api/fleet", fleet_read)
        await page.route("**/api/fleet/tickets", lambda r: fixed(r, {"error": "the fixture has no stream; the console polls"}, 404))
        await page.route("**/api/inbox/*/ack", ack)
        await page.route("**/api/jobs/*/retry*", retry)
        await page.route("**/api/jobs/*/approve", job_decided)
        await page.route("**/api/jobs/*/reject", job_decided)
        await page.route("**/api/jobs/*/follow-up", answered)
