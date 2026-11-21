"""KAN-143: the backup/restore drill and the upgrade/rollback rehearsal (scripts/ops/drill.py)."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts" / "ops"))

import drill  # noqa: E402


def _db(path: Path, rows: dict[str, list[tuple[str, str]]]) -> Path:
    con = sqlite3.connect(path)
    for table, items in rows.items():
        con.execute(f"CREATE TABLE {table} (key TEXT PRIMARY KEY, data TEXT NOT NULL)")
        con.executemany(f"INSERT INTO {table} VALUES (?, ?)", items)
    con.execute("CREATE TABLE audit (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, data TEXT)")
    con.commit()
    con.close()
    return path


def test_fingerprint_finds_lost_and_changed_rows(tmp_path: Path):
    a = drill.fingerprint(_db(tmp_path / "a.db", {"jobs": [("j1", "{}"), ("j2", "{}")]}))
    b = drill.fingerprint(_db(tmp_path / "b.db", {"jobs": [("j1", "{}")]}))
    c = drill.fingerprint(_db(tmp_path / "c.db", {"jobs": [("j1", "{}"), ("j2", '{"x": 1}')], "new": []}))
    assert drill.lost(a, b) == {"jobs": ["j2"]}
    assert drill.lost(a, c) == {}  # a changed row and a new table lose nothing
    assert drill.same(a, c) == ["jobs", "new"]
    assert drill.same(a, a) == []
    assert drill.lost(a, {}) == {"audit": ["<the whole table>"], "jobs": ["<the whole table>"]}


def _install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from hangar.app import Hangar
    from hangar.config import Settings
    from hangar.models import Job

    data = tmp_path / "install"
    data.mkdir()
    monkeypatch.delenv("HANGAR_SECRET_KEY", raising=False)
    h = Hangar(Settings(data_dir=data))
    h.store.put(Job(title="real", instructions="i", environment="python"))
    h.vault.put("a-secret", "value")
    h.store._db.close()
    return data


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def test_restore_drill_passes_on_a_seeded_database(tmp_path: Path):
    out = tmp_path / "report.json"
    assert drill.main(["restore", "--out", str(out)]) == 0
    rep = json.loads(out.read_text())
    assert rep["ok"] and rep["restore_seconds"] > 0
    # secrets: the seeded one and the audit chain's key, which hangar keeps in the vault (KAN-296)
    assert rep["rows"]["jobs"] == 3 and rep["rows"]["sessions"] == 2 and rep["rows"]["secrets"] == 2
    assert rep["loaded"]["pin_sets_checked"] == 1  # a seeded pin set still matches its id
    assert {c["check"] for c in rep["checks"]} >= {"SQLite integrity check", "the restore was applied at start"}


def test_restore_drill_copies_an_install_and_never_writes_to_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    data = _install(tmp_path, monkeypatch)
    before = {p.name: _sha(p) for p in data.iterdir() if p.is_file()}
    out = tmp_path / "report.json"
    assert drill.main(["restore", "--data-dir", str(data), "--out", str(out)]) == 0
    assert json.loads(out.read_text())["rows"]["jobs"] == 1
    # Nothing changed and nothing was added (no -wal or -shm beside the database either).
    assert {p.name: _sha(p) for p in data.iterdir()} == before


def test_restore_drill_fails_on_a_record_hangar_cant_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    data = _install(tmp_path, monkeypatch)
    con = sqlite3.connect(data / "hangar.db")
    con.execute("INSERT INTO jobs VALUES ('broken', '{\"not\": \"a job\"}')")
    con.commit()
    con.close()
    out = tmp_path / "report.json"
    assert drill.main(["restore", "--data-dir", str(data), "--out", str(out)]) == 1
    bad = {c["check"]: c.get("detail") for c in json.loads(out.read_text())["checks"] if not c["ok"]}
    load = bad.pop("every record loads, every secret decrypts, pin sets match their ids")
    assert any("jobs/broken" in e for e in load)
    assert bad == {"hangar read as many records of each kind as were backed up":
                   {"jobs": {"backed_up": 2, "loaded": 1}}}


def test_restore_drill_refuses_an_install_without_its_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    data = _install(tmp_path, monkeypatch)
    (data / "secret.key").unlink()
    assert drill.main(["restore", "--data-dir", str(data)]) == 1


def _previous_tag() -> str:
    """v0.2.0 where the checkout has it; a shallow CI checkout has no tags, so the rehearsal runs HEAD to HEAD."""
    ok = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "-q", "--verify", "v0.2.0^{commit}"],
                        capture_output=True).returncode == 0
    return "v0.2.0" if ok else "HEAD"


def test_upgrade_and_both_rollbacks_pass(tmp_path: Path):
    out = tmp_path / "report.json"
    assert drill.main(["upgrade", "--from", _previous_tag(), "--out", str(out)]) == 0
    rep = json.loads(out.read_text())
    assert rep["ok"]
    names = [c["check"] for c in rep["checks"]]
    assert any(n.startswith("rollback in place") for n in names)
    assert any(n.startswith("rollback by restore") for n in names)
