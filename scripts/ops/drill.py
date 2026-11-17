"""Backup/restore drill and upgrade/rollback rehearsal for hangar's database (KAN-143).

    uv run python scripts/ops/drill.py restore [--data-dir DIR] [--out report.json]
    uv run python scripts/ops/drill.py upgrade [--from v0.2.0] [--to WORKTREE|<ref>] [--data-dir DIR] [--out FILE]

Both work in a temporary directory and never write to a real install. With --data-dir, the drill copies that
install's database (SQLite's backup API, read-only on the source) and uses its key (HANGAR_SECRET_KEY, else the data
dir's secret.key); without it, the drill seeds a database of its own.

restore: back the database up off its disk, lose the disk, restore the latest backup into a clean data directory
with the same steps `hangar admin restore` and the next start take (stage, apply), start hangar on it, and check:
- every table holds exactly the rows it held at the backup (a per-table row count and digest);
- SQLite's integrity check passes, every record loads with hangar's models, and every vault secret decrypts;
- every pin set's id still matches its content (pin sets are content-addressed).
The report records the time to restore, and states the RPO as the backup interval.

upgrade: the procedure in docs/upgrading.md, on a seeded database. The previous tag seeds it and takes the pre-upgrade
backup; the candidate starts on it (startup runs the migrations) and must load every record with nothing lost; the
candidate writes a record; then both rollbacks are rehearsed:
- in place: the previous tag starts on the database the candidate migrated, and must load every record, the
  candidate's included;
- by restore: the previous tag restores the pre-upgrade backup, which must match the database before the upgrade.

Each step runs in its own process with that version's source on the path (`git archive` of the ref) and this
checkout's environment, so run it with `uv run`. Exit status 0 means every check passed.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
WORKTREE = "WORKTREE"


# --------------------------------------------------------------------------- pure SQLite: any version, no hangar
def fingerprint(db: Path) -> dict[str, dict[str, Any]]:
    """Each table's row count, keys and a digest of every row, read without hangar's code."""
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table' "
                                            "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        out: dict[str, dict[str, Any]] = {}
        for t in tables:
            cols = [r[1] for r in con.execute(f'PRAGMA table_info("{t}")')]
            order = ", ".join(f'"{c}"' for c in cols)
            h = hashlib.sha256()
            keys: list[str] = []
            n = 0
            for row in con.execute(f'SELECT * FROM "{t}" ORDER BY {order}'):
                h.update(json.dumps([_plain(v) for v in row]).encode())
                if "key" in cols:
                    keys.append(str(row[cols.index("key")]))
                elif "name" in cols:
                    keys.append(str(row[cols.index("name")]))
                n += 1
            out[t] = {"rows": n, "sha256": h.hexdigest(), "keys": sorted(keys)}
        return out
    finally:
        con.close()


def _plain(v: Any) -> Any:
    return base64.b64encode(v).decode() if isinstance(v, bytes) else v


def integrity(db: Path) -> str:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return con.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        con.close()


def lost(before: dict[str, dict[str, Any]], after: dict[str, dict[str, Any]]) -> dict[str, list[str]]:
    """Keys (or, for keyless tables like the audit log, a row count) that `before` had and `after` doesn't."""
    out: dict[str, list[str]] = {}
    for t, b in before.items():
        a = after.get(t)
        if a is None:
            out[t] = ["<the whole table>"]
        elif b["keys"]:
            gone = sorted(set(b["keys"]) - set(a["keys"]))
            if gone:
                out[t] = gone
        elif a["rows"] < b["rows"]:
            out[t] = [f"{b['rows'] - a['rows']} rows"]
    return out


def same(before: dict[str, dict[str, Any]], after: dict[str, dict[str, Any]]) -> list[str]:
    """Tables whose rows differ in any way."""
    return sorted(t for t in before.keys() | after.keys()
                  if (before.get(t) or {}).get("sha256") != (after.get(t) or {}).get("sha256"))


def copy_db(src: Path, dest: Path) -> None:
    """A consistent copy of a live database, opened read-only, so the drill never writes to it."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    s = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    d = sqlite3.connect(str(dest))
    try:
        s.backup(d)
    finally:
        d.close()
        s.close()
    os.chmod(dest, 0o600)


# --------------------------------------------------------------------------- the steps, run inside one version
def _hangar(data_dir: Path):
    from hangar.app import Hangar
    from hangar.config import Settings

    data_dir.mkdir(parents=True, exist_ok=True)
    return Hangar(Settings(data_dir=data_dir))


def step_seed(data_dir: Path, tag: str) -> dict[str, Any]:
    """Records of every kind a restore must bring back, built only from fields v0.2.0 and later share."""
    from hangar.models import (
        ApiToken,
        AuditEvent,
        Credential,
        CredentialKind,
        Job,
        Person,
        ScheduledJob,
        Session,
    )

    h = _hangar(data_dir)
    recs = [
        Person(id=f"drill-{tag}@example.invalid"),
        ApiToken(name=f"drill-{tag}", token_hash=hashlib.sha256(tag.encode()).hexdigest()),
        Credential(name=f"drill-{tag}", harness="claude", kind=CredentialKind.API_KEY),
        ScheduledJob(name=f"drill-{tag}", cron="0 4 * * *", template={"title": "drill"}),
        *[Job(title=f"drill {tag} {i}", instructions="nothing", environment="python") for i in range(3)],
        *[Session(name=f"drill-{tag}-{i}", environment="python") for i in range(2)],
    ]
    for r in recs:
        h.store.put(r)
    h.vault.put(f"drill-{tag}", f"not a real secret ({tag})")
    h.store.audit(AuditEvent(actor="drill", action="drill.seed", target=tag))
    return {"records": len(recs), "secrets": 1, "audit": 1}


def step_load(data_dir: Path, apply: bool = True) -> dict[str, Any]:
    """Start hangar on the database (a staged restore applied first, as create_app does), then read everything."""
    from hangar import store as store_mod

    applied = None
    if apply:
        from hangar.backup import apply_pending
        from hangar.config import Settings

        applied = apply_pending(Settings(data_dir=data_dir))
    h = _hangar(data_dir)
    con = sqlite3.connect(f"file:{data_dir / 'hangar.db'}?mode=ro", uri=True)
    errors: list[str] = []
    kinds: dict[str, int] = {}
    try:
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for cls, (table, _) in store_mod._TABLES.items():
            keys = [r[0] for t in (table, f"{table}_archive") if t in tables
                    for r in con.execute(f'SELECT key FROM "{t}"')]
            for k in keys:
                try:
                    if h.store.get(cls, k) is None:
                        errors.append(f"{table}/{k}: missing")
                except Exception as e:  # noqa: BLE001 - reported, not raised: the drill lists every failure
                    errors.append(f"{table}/{k}: {type(e).__name__}: {str(e)[:200]}")
            kinds[table] = len(keys)
        names = [r[0] for r in con.execute("SELECT name FROM secrets")]
    finally:
        con.close()
    bad = 0
    for n in names:
        try:
            h.vault._raw(n)  # decrypt only: a sealed reference names a mounted file this machine may not have
        except Exception as e:  # noqa: BLE001
            bad += 1
            errors.append(f"secrets/{n}: {type(e).__name__}")
    pins = _check_pins(h, errors)
    return {"kinds": kinds, "secrets": len(names), "secrets_failed": bad, "pin_sets_checked": pins,
            "audit": len(h.store.audit_tail(1_000_000)), "errors": errors, "applied_restore": applied}


def _check_pins(h: Any, errors: list[str]) -> int:
    try:
        from hangar.pinsets import PinSet, PinSetBody, pin_id
    except ImportError:
        return 0
    n = 0
    for p in h.store.list(PinSet):
        body = PinSetBody.model_validate(p.model_dump(include=set(PinSetBody.model_fields)))
        if pin_id(body) != p.id:
            errors.append(f"pin_sets/{p.id}: its content no longer matches its id")
        n += 1
    return n


def step_backup(data_dir: Path) -> dict[str, Any]:
    import asyncio

    h = _hangar(data_dir)
    t = time.monotonic()
    res = asyncio.run(h.backups.run())
    return {"name": res["name"], "db_bytes": res["db_bytes"], "stored_bytes": res["stored_bytes"],
            "where": res["where"], "seconds": round(time.monotonic() - t, 3)}


def step_stage(data_dir: Path, name: str) -> dict[str, Any]:
    """`hangar admin restore --local <name>`: fetch, decrypt, check and stage, without starting hangar."""
    import asyncio

    from hangar import keys
    from hangar.backup import Backups, backup_store
    from hangar.checkpoints import make_store
    from hangar.config import Settings
    from hangar.store import Store
    from hangar.vault import Vault

    s = Settings(data_dir=data_dir)
    s.data_dir.mkdir(parents=True, exist_ok=True)
    # A Store on a scratch file, only to hold the key ring: the real database is what the restore replaces.
    scratch = Store(data_dir / ".drill-keys.db")
    vault = Vault(scratch, keys.load(s, scratch))
    b = Backups(s, vault, backup_store(s, make_store(s.session_store, s.data_dir)))
    res = asyncio.run(b.stage(name))
    scratch._db.close()
    for suffix in ("", "-wal", "-shm"):
        Path(f"{data_dir / '.drill-keys.db'}{suffix}").unlink(missing_ok=True)
    return {"name": res["name"], "db_bytes": res["db_bytes"]}


STEPS = {"seed": step_seed, "load": step_load, "backup": step_backup, "stage": step_stage}


def _step_main(argv: list[str]) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("step", choices=sorted(STEPS))
    p.add_argument("--src", required=True)
    p.add_argument("--data-dir", required=True, type=Path)
    p.add_argument("--arg", default=None)
    a = p.parse_args(argv)
    sys.path.insert(0, a.src)
    import logging

    logging.disable(logging.WARNING)
    fn = STEPS[a.step]
    out = fn(a.data_dir, a.arg) if a.arg is not None else fn(a.data_dir)
    print("DRILL-RESULT " + json.dumps(out))
    return 0


# --------------------------------------------------------------------------- the parent: versions, timing, report
class Drill:
    def __init__(self, work: Path, key: str, backup_dir: Path):
        self.work = work
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("HANGAR_")}
        # The vault key from outside the data disk (Secret Manager on GKE); backups on another store (a bucket).
        self.env.update(HANGAR_SECRET_KEY=key, HANGAR_BACKUP_STORE=str(backup_dir), HANGAR_BACKUP_HOURS="0")
        self.log: list[dict[str, Any]] = []

    def run(self, step: str, src: Path, data_dir: Path, arg: str | None = None, label: str = "") -> dict[str, Any]:
        cmd = [sys.executable, "-I", str(Path(__file__).resolve()), "_step", step, "--src", str(src),
               "--data-dir", str(data_dir)] + (["--arg", arg] if arg is not None else [])
        t = time.monotonic()
        p = subprocess.run(cmd, env={**self.env, "HANGAR_DATA_DIR": str(data_dir)}, cwd=self.work,
                           capture_output=True, text=True, timeout=600)
        secs = round(time.monotonic() - t, 3)
        line = next((ln for ln in p.stdout.splitlines() if ln.startswith("DRILL-RESULT ")), None)
        if p.returncode != 0 or line is None:
            raise DrillError(f"{label or step} failed (exit {p.returncode}): {p.stderr.strip()[-2000:]}")
        res = json.loads(line.removeprefix("DRILL-RESULT "))
        self.log.append({"step": label or step, "seconds": secs})
        return {**res, "_seconds": secs}


class DrillError(RuntimeError):
    pass


def new_key() -> str:
    return base64.urlsafe_b64encode(os.urandom(32)).decode()


def export(ref: str, dest: Path) -> Path:
    """The ref's source tree (src/ only), or this checkout's own src/ for WORKTREE."""
    if ref == WORKTREE:
        return ROOT / "src"
    dest.mkdir(parents=True, exist_ok=True)
    tar = dest / "src.tar"
    with tar.open("wb") as f:
        subprocess.run(["git", "-C", str(ROOT), "archive", "--format=tar", ref, "src"], stdout=f, check=True)
    with tarfile.open(tar) as t:
        t.extractall(dest, filter="data")
    tar.unlink()
    return dest / "src"


def _check(report: dict[str, Any], name: str, ok: bool, detail: Any = None) -> bool:
    report["checks"].append({"check": name, "ok": bool(ok), **({"detail": detail} if detail else {})})
    return ok


def restore_drill(data_dir: Path | None, work: Path) -> dict[str, Any]:
    src = ROOT / "src"
    live = work / "data"
    backups = work / "offsite"
    if data_dir is not None:
        if not (data_dir / "hangar.db").is_file():
            raise DrillError(f"no hangar.db in {data_dir}")
        copy_db(data_dir / "hangar.db", live / "hangar.db")
    d = Drill(work, _install_key(data_dir) if data_dir else new_key(), backups)
    report: dict[str, Any] = {"drill": "restore", "source": str(data_dir) if data_dir else "seeded", "checks": []}
    if data_dir is None:
        d.run("seed", src, live, "restore", label="seed")
    before = fingerprint(live / "hangar.db")
    b = d.run("backup", src, live, label="backup")
    shutil.move(str(live), str(work / "lost-disk"))  # the disk is gone; the key and the backup store are not

    clean = work / "restored"
    t = time.monotonic()
    d.run("stage", src, clean, b["name"], label="stage the latest backup")
    from_backup = clean / "restore-pending.db"
    staged = fingerprint(from_backup)
    loaded = d.run("load", src, clean, label="start hangar on it and read everything")
    rto = round(time.monotonic() - t, 3)

    _check(report, "the restored database holds exactly the rows backed up", not same(before, staged),
           same(before, staged))
    _check(report, "SQLite integrity check", integrity(clean / "hangar.db") == "ok")
    _check(report, "every record loads, every secret decrypts, pin sets match their ids", not loaded["errors"],
           loaded["errors"][:20])
    _check(report, "the restore was applied at start", bool(loaded["applied_restore"]))
    rows = {t: v["rows"] for t, v in before.items() if v["rows"]}
    report.update(backup={k: b[k] for k in ("name", "db_bytes", "stored_bytes", "seconds")}, rows=rows,
                  loaded={k: loaded[k] for k in ("secrets", "pin_sets_checked", "audit")},
                  restore_seconds=rto, steps=d.log)
    report["ok"] = all(c["ok"] for c in report["checks"])
    return report


def _install_key(data_dir: Path) -> str:
    """HANGAR_SECRET_KEY, else the key file and its retired keys (keys.py), as one comma-separated ring."""
    key = os.environ.get("HANGAR_SECRET_KEY", "")
    if not key:
        files = [data_dir / "secret.key", data_dir / "secret.key.retired"]
        key = ",".join(k for f in files if f.is_file() for k in f.read_text().split())
    if not key:
        raise DrillError("set HANGAR_SECRET_KEY to the install's key (its data dir has no secret.key)")
    return key


def upgrade_drill(from_ref: str, to_ref: str, work: Path, data_dir: Path | None = None) -> dict[str, Any]:
    old = export(from_ref, work / "trees" / "from")
    new = export(to_ref, work / "trees" / "to")
    data = work / "data"
    if data_dir is not None:
        if not (data_dir / "hangar.db").is_file():
            raise DrillError(f"no hangar.db in {data_dir}")
        copy_db(data_dir / "hangar.db", data / "hangar.db")
    d = Drill(work, _install_key(data_dir) if data_dir else new_key(), work / "offsite")
    report: dict[str, Any] = {"drill": "upgrade", "from": from_ref, "to": to_ref,
                              "source": str(data_dir) if data_dir else "seeded", "checks": []}

    if data_dir is None:
        d.run("seed", old, data, "before-upgrade", label=f"{from_ref}: seed")
    base = d.run("load", old, data, label=f"{from_ref}: start and read everything")
    _check(report, f"{from_ref} reads its own database", not base["errors"], base["errors"][:20])
    pre = d.run("backup", old, data, label=f"{from_ref}: the pre-upgrade backup")
    fp0 = fingerprint(data / "hangar.db")

    up = d.run("load", new, data, label=f"{to_ref}: start (migrations) and read everything")
    fp1 = fingerprint(data / "hangar.db")
    _check(report, f"{to_ref} starts on {from_ref}'s database and reads every record", not up["errors"],
           up["errors"][:20])
    _check(report, "nothing was lost in the upgrade", not lost(fp0, fp1), lost(fp0, fp1))
    d.run("seed", new, data, "after-upgrade", label=f"{to_ref}: write records")
    fp2 = fingerprint(data / "hangar.db")

    back = d.run("load", old, data, label=f"{from_ref}: roll back in place and read everything")
    _check(report, f"rollback in place: {from_ref} reads every record, {to_ref}'s included", not back["errors"],
           back["errors"][:20])
    _check(report, "rollback in place: nothing was lost", not lost(fp2, fingerprint(data / "hangar.db")),
           lost(fp2, fingerprint(data / "hangar.db")))

    shutil.move(str(data), str(work / "after-upgrade"))
    t = time.monotonic()
    d.run("stage", old, data, pre["name"], label=f"{from_ref}: restore the pre-upgrade backup")
    staged = fingerprint(data / "restore-pending.db")
    restored = d.run("load", old, data, label=f"{from_ref}: start on the restore and read everything")
    rto = round(time.monotonic() - t, 3)
    _check(report, "rollback by restore: the database is as it was before the upgrade", not same(fp0, staged),
           same(fp0, staged))
    _check(report, "rollback by restore: every record loads", not restored["errors"], restored["errors"][:20])

    report.update(new_tables=sorted(fp1.keys() - fp0.keys()),
                  rewritten_by_upgrade=[t for t in same(fp0, fp1) if t in fp0],
                  restore_seconds=rto, steps=d.log)
    report["ok"] = all(c["ok"] for c in report["checks"])
    return report


def render(report: dict[str, Any]) -> str:
    lines = [f"{'PASS' if report['ok'] else 'FAIL'}: {report['drill']} drill"]
    for c in report["checks"]:
        lines.append(f"  {'ok ' if c['ok'] else 'BAD'} {c['check']}" + ("" if c["ok"] else f": {c.get('detail')}"))
    for s in report["steps"]:
        lines.append(f"  {s['seconds']:8.2f}s  {s['step']}")
    lines.append(f"  time to restore: {report['restore_seconds']:.2f}s")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    if argv[:1] == ["_step"]:
        return _step_main(argv[1:])
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("restore", help="back up, lose the disk, restore, verify")
    u = sub.add_parser("upgrade", help="upgrade from a tag to the candidate, then roll back both ways")
    u.add_argument("--from", dest="from_ref", default="v0.2.0")
    u.add_argument("--to", dest="to_ref", default=WORKTREE, help=f"a ref, or {WORKTREE} for this checkout")
    for s in (r, u):
        s.add_argument("--data-dir", type=Path, help="an install's data dir to copy (default: a seeded database)")
        s.add_argument("--out", type=Path, help="write the report as JSON here")
        s.add_argument("--keep", action="store_true", help="keep the temporary directory")
    a = p.parse_args(argv)
    work = Path(tempfile.mkdtemp(prefix="hangar-drill-"))
    try:
        rep = (restore_drill(a.data_dir, work) if a.cmd == "restore"
               else upgrade_drill(a.from_ref, a.to_ref, work, a.data_dir))
    except DrillError as e:
        print(f"FAIL: {e}", file=sys.stderr)
        return 1
    finally:
        if not a.keep:
            shutil.rmtree(work, ignore_errors=True)
    rep["at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    print(render(rep))
    if a.out:
        a.out.write_text(json.dumps(rep, indent=2) + "\n")
    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
