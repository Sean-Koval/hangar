"""Data handling for a company install (KAN-296; docs/security/data-handling.md): the hash-chained audit table and
its retention, `hangar admin purge --person` and `hangar admin export --job`, and the locked audit sink in the
hangar-workload module companies deploy."""

from __future__ import annotations

import io
import json
import logging
import re
import sqlite3
import tarfile
import time
from pathlib import Path

import pytest

from hangar import datahandling
from hangar.models import ApiToken, AuditEvent, CredentialKind, Job, JobState, Person, Session, SessionState
from hangar.retention import Retention
from hangar.store import AUDIT_ANCHOR, Store

ROOT = Path(__file__).resolve().parents[1]
WORKLOAD = ROOT / "deploy/terraform/modules/hangar-workload"
KEY = b"k" * 32


def _store(path) -> Store:
    store = Store(path)
    store.set_audit_key(KEY)
    return store


def _events(store: Store, n: int, ts: float | None = None, actor: str = "admin") -> None:
    for i in range(n):
        ev = AuditEvent(actor=actor, action="job.approve", target=f"job-{i}")
        if ts is not None:
            ev.ts = ts + i
        store.audit(ev)


# ====================================================================== the chain
def test_a_fresh_chain_verifies_and_every_log_line_carries_its_link(tmp_path, caplog):
    store = _store(tmp_path / "hangar.db")
    with caplog.at_level(logging.INFO, logger="hangar.audit"):
        _events(store, 5)
    v = datahandling.verify_chain(store)
    assert v["ok"] and v["rows"] == 6 and v["broken_at"] is None and v["restarts"] == []  # 1: the chain started
    lines = [json.loads(r.getMessage()) for r in caplog.records if r.name == "hangar.audit"]
    rows = store.audit_rows()[1:]
    assert [(ln["seq"], ln["hash"]) for ln in lines] == [(r[0], r[4]) for r in rows]


def test_an_edited_row_breaks_the_chain_there(tmp_path):
    store = _store(tmp_path / "hangar.db")
    _events(store, 5)
    store._db.execute("UPDATE audit SET data = replace(data, 'job-2', 'job-9') WHERE id = 4")
    v = datahandling.verify_chain(store)
    assert not v["ok"] and v["broken_at"] == 4 and "edited" in v["reason"]


def test_a_removed_row_breaks_the_chain_at_the_next(tmp_path):
    store = _store(tmp_path / "hangar.db")
    _events(store, 5)
    store._db.execute("DELETE FROM audit WHERE id = 4")
    v = datahandling.verify_chain(store)
    assert not v["ok"] and v["broken_at"] == 5 and "removed" in v["reason"]


def test_relinking_an_edited_row_still_breaks_the_next(tmp_path):
    """Someone who edits a row and fixes its own digest and link (they'd need the vault's key) still breaks the row
    after it."""
    from hangar.auditchain import audit_digest, audit_link

    store = _store(tmp_path / "hangar.db")
    _events(store, 4)
    data = store.audit_rows()[2][2].replace("job-1", "job-7")
    prev = store.audit_rows()[1][4]
    store._db.execute("UPDATE audit SET data=?, digest=?, hash=? WHERE id=3",
                      (data, audit_digest(KEY, data), audit_link(KEY, prev, 3, audit_digest(KEY, data))))
    v = datahandling.verify_chain(store)
    assert not v["ok"] and v["broken_at"] == 4


def test_the_chain_cant_be_rebuilt_without_the_vault_key(tmp_path):
    """The links are HMACs: a plain SHA-256 chain rebuilt over an edit doesn't verify."""
    import hashlib

    store = _store(tmp_path / "hangar.db")
    _events(store, 3)
    rows = store.audit_rows()
    prev = rows[0][4]
    for rid, _ts, data, *_ in rows[1:]:
        data = data.replace("job-", "job-x")
        digest = hashlib.sha256(data.encode()).hexdigest()
        prev = hashlib.sha256(f"{prev}\n{rid}\n{digest}".encode()).hexdigest()
        store._db.execute("UPDATE audit SET data=?, digest=?, hash=? WHERE id=?", (data, digest, prev, rid))
    v = datahandling.verify_chain(store)
    assert not v["ok"] and v["broken_at"] == 2


def test_a_row_marked_redacted_by_hand_is_a_break(tmp_path):
    store = _store(tmp_path / "hangar.db")
    _events(store, 3)
    store._db.execute("UPDATE audit SET data = replace(data, 'job-1', 'job-7'), redacted = 1 WHERE id = 3")
    v = datahandling.verify_chain(store)
    assert not v["ok"] and v["broken_at"] == 3 and "redacted" in v["reason"]


def test_a_redacted_row_edited_after_the_purge_is_a_break(tmp_path):
    store = _store(tmp_path / "hangar.db")
    _events(store, 3, actor="alice")
    assert len(store.audit_redact(datahandling.redactor(["alice"], "person:x"))) == 3
    assert datahandling.verify_chain(store)["ok"]
    store._db.execute("UPDATE audit SET data = replace(data, 'job-1', 'job-7') WHERE id = 3")
    v = datahandling.verify_chain(store)
    assert not v["ok"] and v["broken_at"] == 3


def test_the_redacted_digest_is_no_oracle_for_the_name(tmp_path):
    """The kept digest is an HMAC: putting a guessed name back and hashing it confirms nothing."""
    import hashlib

    store = _store(tmp_path / "hangar.db")
    _events(store, 1, actor="alice")
    store.audit_redact(datahandling.redactor(["alice"], "person:x"))
    _, _, data, digest, *_ = store.audit_rows()[1]
    guess = data.replace("person:x", "alice")
    assert hashlib.sha256(guess.encode()).hexdigest() != digest


def test_rows_from_before_the_chain_are_linked_on_start_and_that_is_audited(tmp_path):
    db = tmp_path / "hangar.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE audit (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, data TEXT)")
    for i in range(3):
        con.execute("INSERT INTO audit(ts, data) VALUES(?, ?)",
                    (1.0 + i, AuditEvent(actor="a", action="x", target=str(i)).model_dump_json()))
    con.commit()
    con.close()
    store = _store(db)
    v = datahandling.verify_chain(store)
    assert v["ok"] and v["rows"] == 4 and v["restarts"] == []
    assert store.audit_tail(1)[0].action == "audit.chain_started"
    assert store.audit_tail(1)[0].detail == {"rows": 3, "linked_before": 0, "anchor": False}
    _store(db)  # a second start links nothing more
    assert datahandling.verify_chain(store)["rows"] == 4


def test_rows_written_before_the_key_loads_are_linked_when_it_does(tmp_path):
    store = Store(tmp_path / "hangar.db")
    _events(store, 2)
    assert store.audit_rows()[0][4] is None
    store.set_audit_key(KEY)
    v = datahandling.verify_chain(store)
    assert v["ok"] and v["rows"] == 3


def test_clearing_links_and_restarting_doesnt_hide_an_edit(tmp_path):
    db = tmp_path / "hangar.db"
    store = _store(db)
    _events(store, 4)
    store._db.execute("UPDATE audit SET hash = NULL, data = replace(data, 'job-3', 'job-9') WHERE id = 5")
    store = _store(db)  # a restart links only rows that process wrote
    v = datahandling.verify_chain(store)
    assert not v["ok"] and v["broken_at"] == 5 and "without a link" in v["reason"]
    # clearing the "started" mark as well gets the rows linked again, and doctor sees the second start
    store._db.execute("DELETE FROM kv WHERE key = 'audit.chain.started'")
    store = _store(db)
    v = datahandling.verify_chain(store)
    assert v["restarts"]


def test_an_unlinked_row_before_linked_ones_stays_a_break(tmp_path):
    db = tmp_path / "hangar.db"
    store = _store(db)
    _events(store, 4)
    store._db.execute("UPDATE audit SET hash = NULL WHERE id = 3")
    store = _store(db)
    v = datahandling.verify_chain(store)
    assert not v["ok"] and v["broken_at"] == 3 and "without a link" in v["reason"]


# ====================================================================== retention
def test_retention_deletes_the_oldest_rows_and_the_chain_still_holds(tmp_path):
    store = _store(tmp_path / "hangar.db")
    old = time.time() - 500 * 86400
    store._db.execute("UPDATE audit SET ts = ?", (old,))  # the chain's start
    _events(store, 5, ts=old)
    _events(store, 3)
    assert datahandling.prune_audit(store, 400, batch=2) == 6
    assert len(store.audit_rows()) == 3
    anchor = json.loads(store.kv_get(AUDIT_ANCHOR))
    assert anchor["id"] == 6 and anchor["pruned"] == 6
    v = datahandling.verify_chain(store)
    assert v["ok"] and v["first_id"] == 7
    _events(store, 1)
    assert datahandling.verify_chain(store)["ok"]


def test_a_table_pruned_empty_starts_again_from_the_anchor(tmp_path):
    store = _store(tmp_path / "hangar.db")
    _events(store, 3, ts=time.time() - 30 * 86400)
    store._db.execute("UPDATE audit SET ts = ?", (time.time() - 30 * 86400,))  # the chain's start too
    assert datahandling.prune_audit(store, 7) == 4 and store.audit_rows() == []
    _events(store, 2)
    assert datahandling.verify_chain(store)["ok"]
    store._db.execute(f"DELETE FROM kv WHERE key = '{AUDIT_ANCHOR}'")  # the anchor is part of the chain
    assert not datahandling.verify_chain(store)["ok"]


def test_retention_off_keeps_everything_and_a_row_inside_retention_stops_it(tmp_path):
    store = _store(tmp_path / "hangar.db")
    old = time.time() - 500 * 86400
    _events(store, 2, ts=old)
    _events(store, 1)  # new
    _events(store, 2, ts=old)  # older timestamps after a newer row: kept, so the rest stays one chain
    assert datahandling.prune_audit(store, 0) == 0
    store._db.execute("UPDATE audit SET ts = ? WHERE id = 1", (old,))  # the chain's start
    assert datahandling.prune_audit(store, 400) == 3
    assert len(store.audit_rows()) == 3 and datahandling.verify_chain(store)["ok"]


async def test_the_retention_sweep_prunes_the_audit_table(settings, store):
    store.set_audit_key(KEY)
    store._db.execute("DELETE FROM audit")
    settings.audit_retention_days = 30
    _events(store, 4, ts=time.time() - 60 * 86400)
    out = await Retention(store, settings).sweep()
    assert out["audit_pruned"] == 4


# ====================================================================== doctor
class _H:
    def __init__(self, store, settings, vault=None):
        self.store, self.settings, self.vault = store, settings, vault


async def test_doctor_reports_the_chain_and_retention(settings, store):
    store.set_audit_key(KEY)
    _events(store, 3)
    checks = {c["name"]: c for c in await datahandling.doctor_checks(_H(store, settings))}
    assert checks["audit chain"]["status"] == "ok" and checks["audit retention"]["status"] == "ok"
    assert "audit chain restarts" not in checks
    store._db.execute("DELETE FROM audit WHERE id = 1")
    checks = {c["name"]: c for c in await datahandling.doctor_checks(_H(store, settings))}
    assert checks["audit chain"]["status"] == "fail" and checks["audit chain"]["fix"]
    settings.audit_retention_days = 0
    checks = {c["name"]: c for c in await datahandling.doctor_checks(_H(store, settings))}
    assert checks["audit retention"]["status"] == "warn"


# ====================================================================== purge
def test_the_redactor_replaces_names_only_in_the_fields_that_name_people():
    rw = datahandling.redactor(["alice", "ui:alice-laptop", "tok-123", "github:alice"], "person:x")
    out = json.loads(rw(json.dumps({"actor": "ui:alice-laptop", "target": "tok-123", "detail": {
        "person": "alice", "by": "alice and bob", "note": "alice left; malice stays", "list": ["alice"],
        "who": "owner:github:alice", "repo": "github:alice/app#3"}})))
    assert out["actor"] == "person:x" and out["target"] == "person:x"
    assert out["detail"]["person"] == "person:x" and out["detail"]["list"] == ["person:x"]
    assert out["detail"]["by"] == "person:x and bob"  # an identity field: replaced inside it
    assert out["detail"]["note"] == "alice left; malice stays"  # free text: only a whole value is replaced
    assert out["detail"]["who"] == "owner:person:x"
    assert out["detail"]["repo"] == "github:alice/app#3"  # a repository, not the person
    unchanged = '{"actor":"bob"}'
    assert rw(unchanged) is unchanged


def test_a_common_name_is_replaced_only_with_a_kind_in_front():
    """A person called `admin` mustn't turn the admin token's entries, or a branch called `main`, into theirs."""
    rw = datahandling.redactor(["admin", "main"], "person:x")
    row = json.dumps({"actor": "admin", "action": "a", "target": "main", "detail": {"person": "admin"}})
    assert rw(row) == row
    out = json.loads(rw(json.dumps({"actor": "person:admin", "action": "a", "target": None, "detail": {}})))
    assert out["actor"] == "person:person:x"


def test_a_display_name_is_replaced_only_in_entries_that_name_the_person():
    rw = datahandling.redactor(["alice"], "person:x", ["Alice Example"])
    mine = json.loads(rw(json.dumps({"actor": "admin", "action": "person.add", "target": "alice",
                                     "detail": {"name": "Alice Example"}})))
    assert mine["target"] == "person:x" and mine["detail"]["name"] == "person:x"
    other = json.dumps({"actor": "admin", "action": "note", "target": "bob", "detail": {"name": "Alice Example"}})
    assert rw(other) == other


@pytest.fixture
def app_client(settings):
    from fastapi.testclient import TestClient

    from hangar.app import create_app

    settings.admin_token = "t0ken"
    app = create_app(settings)
    with TestClient(app) as c:
        yield c, app.state.hangar, {"Authorization": "Bearer t0ken"}


def _alice(h) -> tuple[Job, Session]:
    h.store.put(Person(id="alice", name="Alice Example", links=["email:alice@example.com"]))
    h.store.put(Person(id="bob", name="Bob"))
    _, tok = h.auth.create("alice-laptop", scopes=["user"], person="alice")
    h.auth.create("bob-laptop", scopes=["user"], person="bob")
    h.broker.put("alice-claude", "claude-code", CredentialKind.OAUTH_TOKEN, "sk-alice-secret", person="alice")
    s = Session(name="alice-s", environment="default", person="alice", state=SessionState.SUSPENDED)
    job = Job(title="t", instructions="fix it", environment="default", person="alice", session_id=s.id,
              state=JobState.SUCCEEDED)
    h.store.put(s)
    h.store.put(job)
    h.store.put(Job(title="b", instructions="bob's", environment="default", person="bob", state=JobState.SUCCEEDED))
    h.fleet.audit(f"ui:{tok.name}", "job.submit", job.id, person="alice", by="alice@example.com")
    h.fleet.audit("ui:bob-laptop", "job.submit", "job-b", person="bob", note="reviewed alice's change")
    return job, s


def test_purge_deletes_their_records_and_pseudonymises_their_audit_entries(app_client):
    c, h, auth = app_client
    job, s = _alice(h)
    plan = c.post("/api/admin/purge", json={"person": "alice", "dry_run": True}, headers=auth).json()
    assert plan["deleted"]["jobs"] == 1 and plan["deleted"]["sessions"] == 1 and plan["deleted"]["credentials"] == 1
    assert h.store.get(Job, job.id) is not None  # a dry run deletes nothing

    r = c.post("/api/admin/purge", json={"person": "alice"}, headers=auth)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["errors"] == [] and out["audit_rows_redacted"] >= 1 and out["person_record_deleted"]
    assert h.store.get(Job, job.id) is None and h.store.get(Session, s.id) is None
    assert h.store.get(Person, "alice") is None and h.vault.get_text("cred:alice-claude") is None
    assert [t.name for t in h.store.list(ApiToken)] == ["bob-laptop"]
    assert h.store.get(Person, "bob") is not None
    table = "\n".join(r[2] for r in h.store.audit_rows())
    assert "alice@example.com" not in table and "alice-laptop" not in table and '"alice"' not in table
    assert out["pseudonym"] in table and "reviewed alice's change" in table  # free text in bob's entry stays
    assert "bob-laptop" in table and '"person":"bob"' in table
    v = datahandling.verify_chain(h.store)
    assert v["ok"] and v["redacted"] >= 1
    # the same person purged again (records left elsewhere) gets the same pseudonym
    assert datahandling.pseudonym(h, "alice") == out["pseudonym"]


def test_purge_takes_what_their_tokens_and_accounts_own_and_their_waiting_files(app_client):
    """Jobs from GitHub or Slack and older ones record an owner or requester, not a person; a session deleted
    earlier leaves its files waiting for the outbox; an environment they created is claimed in their name."""
    c, h, auth = app_client
    _alice(h)
    tok = next(t for t in h.store.list(ApiToken) if t.person == "alice")
    gh = Job(title="g", instructions="from github", environment="default", requester="email:alice@example.com",
             state=JobState.SUCCEEDED)
    by_token = Job(title="t", instructions="by token", environment="default", owner=tok.id, state=JobState.FAILED)
    h.store.put(gh)
    h.store.put(by_token)
    waiting = h.settings.data_dir / "pending-delete" / "s-old"
    (waiting / "events").mkdir(parents=True)
    (waiting / "marker.json").write_text(json.dumps({"session_id": "s-old", "person": "alice"}))
    h.store.kv_set("identity:env-owner:alice-env", "alice")
    out = c.post("/api/admin/purge", json={"person": "alice"}, headers=auth).json()
    assert out["errors"] == []
    assert h.store.get(Job, gh.id) is None and h.store.get(Job, by_token.id) is None
    assert not waiting.exists() and h.store.kv_get("identity:env-owner:alice-env") is None


def test_a_purge_that_fails_partway_keeps_the_person_and_can_run_again(app_client, monkeypatch):
    c, h, auth = app_client
    job, _ = _alice(h)

    def broken(name):
        raise RuntimeError("vault unavailable")

    monkeypatch.setattr(h.broker, "delete", broken)
    out = c.post("/api/admin/purge", json={"person": "alice"}, headers=auth).json()
    assert out["errors"] and not out["person_record_deleted"]
    assert h.store.get(Person, "alice") is not None and h.store.get(Job, job.id) is None
    assert "alice@example.com" not in "\n".join(r[2] for r in h.store.audit_rows())  # redacted before anything
    monkeypatch.undo()
    again = c.post("/api/admin/purge", json={"person": "alice"}, headers=auth).json()
    assert again["errors"] == [] and again["person_record_deleted"] and again["pseudonym"] == out["pseudonym"]
    assert h.store.get(Person, "alice") is None and h.vault.get_text("cred:alice-claude") is None
    assert datahandling.verify_chain(h.store)["ok"]


def test_purge_refuses_live_work_the_owner_and_nobody(app_client):
    c, h, auth = app_client
    _, s = _alice(h)
    s.state, s.sandbox_id = SessionState.IDLE, "sb-1"
    h.store.put(s)
    r = c.post("/api/admin/purge", json={"person": "alice"}, headers=auth)
    assert r.status_code == 409 and "alice-s" in r.json()["error"]
    assert c.post("/api/admin/purge", json={"person": "owner"}, headers=auth).status_code == 400
    assert c.post("/api/admin/purge", json={"person": "nobody"}, headers=auth).status_code == 404
    assert c.post("/api/admin/purge", json={"person": "alice"}).status_code == 401


# ====================================================================== export
def test_export_holds_the_job_its_session_log_and_its_audit_entries(app_client):
    c, h, auth = app_client
    job, s = _alice(h)
    log_dir = h.sessions.activity.log._paths(s.id)[1]
    log_dir.parent.mkdir(parents=True, exist_ok=True)
    log_dir.write_text('{"seq":1,"kind":"text"}\n')
    r = c.get(f"/api/admin/export/jobs/{job.id}", headers=auth)
    assert r.status_code == 200 and r.headers["content-type"] == "application/gzip"
    with tarfile.open(fileobj=io.BytesIO(r.content), mode="r:gz") as tar:
        files = {m.name.split("/", 1)[1]: tar.extractfile(m).read() for m in tar.getmembers()}
    assert json.loads(files["job.json"])["id"] == job.id
    assert files["instructions.md"] == b"fix it"
    assert {"context.json", "events.json", "result.json", "manifest.json"} <= set(files)
    assert files[f"sessions/{s.id}/events/{log_dir.name}"].startswith(b'{"seq":1')
    audit = [json.loads(ln) for ln in files["audit.jsonl"].decode().splitlines()]
    assert any(a["action"] == "job.submit" and a["target"] == job.id for a in audit)
    assert all(job.id in json.dumps(a) or s.id in json.dumps(a) for a in audit)
    manifest = json.loads(files["manifest.json"])
    assert manifest["files"]["job.json"]["bytes"] == len(files["job.json"])
    assert h.store.audit_tail(1)[0].action == "admin.export"
    # a webhook URL or its secret is a credential: withheld
    job.callback_url, job.callback_secret = "https://hooks.example.com/T0/B0/xyz", "s3cret"
    h.store.put(job)
    r = c.get(f"/api/admin/export/jobs/{job.id}", headers=auth)
    with tarfile.open(fileobj=io.BytesIO(r.content), mode="r:gz") as tar:
        rec = json.loads(next(tar.extractfile(m).read() for m in tar.getmembers() if m.name.endswith("job.json")))
    assert rec["callback_url"] == rec["callback_secret"] == "(withheld)"
    assert c.get("/api/admin/export/jobs/job-nope", headers=auth).status_code == 404


# ====================================================================== the company module's audit sink
def test_the_workload_module_offers_the_locked_audit_sink():
    tf = "\n".join(p.read_text() for p in WORKLOAD.glob("*.tf"))
    for var in ("enable_audit_export", "audit_retention_days", "audit_bucket_locked"):
        assert re.search(rf'variable "{var}"', tf), var
    assert re.search(r'resource "google_logging_project_bucket_config" "audit"', tf)
    assert re.search(r"locked\s*=\s*var\.audit_bucket_locked", tf)
    assert re.search(r"retention_days\s*=\s*var\.audit_retention_days", tf)
    assert re.search(r'resource "google_logging_project_sink" "audit"', tf) and "unique_writer_identity" in tf
    assert 'textPayload:\\"hangar.audit: {\\"' in tf
    assert "roles/logging.admin" not in tf and "roles/logging.configWriter" not in tf
    assert re.search(r'output "audit_log"', tf)
