"""Rotating the vault key (keys.py): the key ring, re-sealing the vault, the pass over the session store, copies
during a rotation, a restart midway, retiring the old key, derived tokens, and the backup doctor checks."""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

import httpx
import pytest
from cryptography.fernet import Fernet, InvalidToken

from hangar import backup, computer, keys
from hangar.checkpoints import GCSStore, LocalStore
from hangar.egress import check_token, token
from hangar.models import Sandbox, SandboxState
from hangar.store import Store
from hangar.vault import Vault

OLD = Fernet.generate_key()
NEW = Fernet.generate_key()


@pytest.fixture
def store(settings):
    keys._RINGS.clear()
    yield Store(settings.db_path)
    keys._RINGS.clear()


def _rotation(settings, store, ring, blobs, live=(), extra=()) -> keys.Rotation:
    audit: list = []
    rot = keys.Rotation(settings, store, ring, [(blobs, False), *extra], lambda: list(live),
                        lambda *a, **kw: audit.append((a, kw)))
    rot.audited = audit
    return rot


def _readable(blob: bytes, key: bytes) -> bool:
    try:
        Fernet(key).decrypt(blob)
        return True
    except InvalidToken:
        return False


async def _until(rot: keys.Rotation, phase: str, timeout: float = 10) -> None:
    for _ in range(int(timeout / 0.02)):
        if (rot.ring.rotation or {}).get("phase") == phase:
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"rotation stuck in {rot.ring.rotation}")


@pytest.fixture(autouse=True)
def _quick(monkeypatch):
    monkeypatch.setattr(keys, "RETIRE_CHECK", 0.02)
    monkeypatch.setattr(keys, "PASS_SAVE_EVERY", 1)


# ====================================================================== the ring
def test_the_newest_key_encrypts_and_every_key_decrypts():
    v = Vault(Store(":memory:"), b",".join([NEW, OLD]))
    sealed_old = Fernet(OLD).encrypt(b"before")
    assert v.decrypt(sealed_old) == b"before"
    assert _readable(v.encrypt(b"after"), NEW) and not _readable(v.encrypt(b"after"), OLD)
    assert keys.parse(f" {NEW.decode()}\n{OLD.decode()},{NEW.decode()} ") == [NEW, OLD]
    with pytest.raises(ValueError, match="key 2"):
        keys.parse(f"{NEW.decode()},not-a-key")
    assert keys.key_id(NEW) != keys.key_id(OLD) and NEW.decode() not in keys.key_id(NEW)


def test_a_new_key_first_in_the_configuration_starts_a_pending_rotation(settings, store):
    settings.secret_key = OLD.decode()
    ring = keys.load(settings, store)
    assert ring.rotation is None and store.kv_get_json(keys.KV)["current"] == keys.key_id(OLD)
    settings.secret_key = f"{NEW.decode()},{OLD.decode()}"  # the operator put a new key first, and restarted
    ring = keys.load(settings, store)
    r = ring.rotation
    assert r["phase"] == "pending" and r["old"] == keys.key_id(OLD) and r["new"] == keys.key_id(NEW)
    assert ring.primary == NEW and keys.ring(settings) is ring
    # sandboxes from before keep the old key for their tokens; new ones get the new key
    assert ring.key_for(r["started_at"] - 1) == OLD and ring.key_for(time.time() + 1) == NEW
    assert ring.key_for() == NEW


# ====================================================================== a whole rotation, key file mode
async def test_a_rotation_reseals_the_vault_reencrypts_the_store_and_retires_the_old_key(settings, store, tmp_path):
    (settings.data_dir / keys.FILE).write_bytes(OLD)
    ring = keys.load(settings, store)
    vault = Vault(store, ring)
    vault.put("cred:gh", "ghp_secret")
    blobs = LocalStore(tmp_path / "ses")
    parts = {f"ses-{i}/ckpt-{i}/repo.bundle.enc": f"part {i}".encode() for i in range(6)}
    parts["_backups/hangar-20261014T000000Z.db.gz.enc"] = b"a backup"
    parts["ses-1/artifacts/1/shot.png.enc"] = b"a screenshot"
    for k, v in parts.items():
        await blobs.put(k, Fernet(OLD).encrypt(v))
    await blobs.put("_doctor/x", b"not ours")  # not an .enc object: left alone
    await blobs.put("ses-9/odd.enc", b"garbage")  # an .enc no key reads: counted, left alone
    store.kv_set("steer-key:run-1", Fernet(OLD).encrypt(b"k").decode())
    rot = _rotation(settings, store, ring, blobs)
    from hangar import steer

    rot.resealers.append(steer.reseal)

    st = await rot.start("sean")
    assert st["rotation"]["sealed_rows"] == 2  # the vault row and the live turn's key
    assert _readable(store.get_secret("cred:gh"), ring.primary) and ring.primary != OLD
    assert _readable(store.kv_get("steer-key:run-1").encode(), ring.primary)
    new = ring.primary
    assert (settings.data_dir / keys.FILE).read_bytes().split() == [new, OLD]  # the key file: newest first
    await _until(rot, "retired")
    for k, v in parts.items():
        blob = await blobs.get(k)
        assert _readable(blob, new) and not _readable(blob, OLD) and Fernet(new).decrypt(blob) == v
    assert await blobs.get("_doctor/x") == b"not ours" and await blobs.get("ses-9/odd.enc") == b"garbage"
    p = rot.ring.rotation["passes"][blobs.description]
    assert (p["objects"], p["checked"], p["rotated"], p["unreadable"], p["finished"]) == (9, 9, 8, 1, True)
    # retired: hangar stops reading with the old key, and the key file no longer holds it
    assert (settings.data_dir / keys.FILE).read_bytes().split() == [new]
    assert ring.active == [new] and vault.get_text("cred:gh") == "ghp_secret"
    with pytest.raises(InvalidToken):
        vault.decrypt(Fernet(OLD).encrypt(b"made with the old key"))
    state = store.kv_get_json(keys.KV)
    assert state["current"] == keys.key_id(new) and state["retired"] == [keys.key_id(OLD)]
    assert [a[0][1] for a in rot.audited] == ["vault.rotate_key", "vault.retire_key"]
    assert NEW not in str(state).encode() and OLD not in str(state).encode()  # ids only, never keys
    await rot.stop()


async def test_a_rotation_carries_on_after_a_restart_midway(settings, store, tmp_path, monkeypatch):
    (settings.data_dir / keys.FILE).write_bytes(OLD)
    ring = keys.load(settings, store)
    blobs = LocalStore(tmp_path / "ses")
    for i in range(10):
        await blobs.put(f"ses-{i:02d}/c/repo.bundle.enc", Fernet(OLD).encrypt(b"x"))
    rot = _rotation(settings, store, ring, blobs)
    calls: list[int] = []
    real = keys.Rotation._reencrypt

    def flaky(self, blob):
        calls.append(1)
        if len(calls) == 4:
            raise OSError("the control plane went away")
        return real(self, blob)

    monkeypatch.setattr(keys.Rotation, "_reencrypt", flaky)
    await rot.start("sean")
    for _ in range(200):
        if (rot.ring.rotation or {}).get("error"):
            break
        await asyncio.sleep(0.01)
    await rot.stop()  # the restart
    assert rot.ring.rotation["phase"] == "passing" and rot.ring.rotation["passes"][blobs.description]["checked"] == 3

    keys._RINGS.clear()
    calls.clear()

    def counting(self, blob):
        calls.append(1)
        return real(self, blob)

    monkeypatch.setattr(keys.Rotation, "_reencrypt", counting)
    ring2 = keys.load(settings, store)  # the next start: the file holds both keys, the state the rotation
    assert ring2.primary == ring.primary and ring2.rotation["phase"] == "passing"
    rot2 = _rotation(settings, store, ring2, blobs)
    rot2.resume()
    await _until(rot2, "retired")
    for i in range(10):
        assert _readable(await blobs.get(f"ses-{i:02d}/c/repo.bundle.enc"), ring2.primary)
    p = ring2.rotation["passes"][blobs.description]
    assert p["checked"] == p["rotated"] == 10 and len(calls) == 7  # the 7 the first run hadn't done, the 4th too
    await rot2.stop()


async def test_with_hangar_secret_key_the_operator_puts_the_new_key_first(settings, store, tmp_path):
    settings.secret_key = OLD.decode()
    rot = _rotation(settings, store, keys.load(settings, store), LocalStore(tmp_path / "ses"))
    with pytest.raises(keys.RotationError, match="put a new one first in HANGAR_SECRET_KEY"):
        await rot.start("sean")
    assert "HANGAR_SECRET_KEY" in rot.status()["next"]
    settings.secret_key = f"{NEW.decode()},{OLD.decode()}"
    rot = _rotation(settings, store, keys.load(settings, store), LocalStore(tmp_path / "ses"))
    assert rot.status()["rotation"]["phase"] == "pending"
    await rot.start("sean")
    await _until(rot, "retired")
    st = rot.status()
    assert st["next"].startswith(f"remove key {keys.key_id(OLD)} from HANGAR_SECRET_KEY")
    assert [k["retired"] for k in st["keys"]] == [False, True]
    keys._RINGS.clear()
    ring = keys.load(settings, store)  # restarted with the retired key still configured: not read with
    assert ring.active == [NEW] and ring.rotation["phase"] == "retired"
    await rot.stop()


async def test_sandboxes_from_before_the_rotation_hold_the_old_key_until_they_stop(settings, store, tmp_path):
    (settings.data_dir / keys.FILE).write_bytes(OLD)
    ring = keys.load(settings, store)
    before = Sandbox(name="long", environment="plain", provider="docker", state=SandboxState.RUNNING,
                     created_at=time.time() - 60)
    live = [before]
    rot = _rotation(settings, store, ring, LocalStore(tmp_path / "ses"), live=live)
    rot.live_sandboxes = lambda: list(live)
    await rot.start("sean")
    await _until(rot, "passed")
    await asyncio.sleep(0.1)
    assert rot.ring.rotation["phase"] == "passed" and rot.status()["blockers"] == [f"long ({before.id})"]
    # its tokens: envd and the egress proxy derive from the key it was started under; a new sandbox's from the new
    assert keys.key_for(settings, before.created_at) == OLD and keys.key_for(settings, time.time()) == ring.primary
    cred = token(keys.key_for(settings, before.created_at), before.id)
    assert any(check_token(k, before.id, cred) for k in keys.verify_keys(settings))
    with pytest.raises(keys.RotationError, match="still run with tokens of the old key"):
        rot.retire("sean")
    live.clear()  # it stopped
    await _until(rot, "retired")
    assert not any(check_token(k, before.id, cred) for k in keys.verify_keys(settings))
    await rot.stop()


async def test_retiring_early_is_refused_before_the_pass_and_forced_after(settings, store, tmp_path):
    (settings.data_dir / keys.FILE).write_bytes(OLD)
    ring = keys.load(settings, store)
    rot = _rotation(settings, store, ring, LocalStore(tmp_path / "ses"),
                    live=[Sandbox(name="x", environment="plain", provider="docker", created_at=0)])
    with pytest.raises(keys.RotationError, match="not started"):
        rot.retire("sean")
    await rot.start("sean")
    await _until(rot, "passed")
    st = rot.retire("sean", force=True)
    assert st["rotation"]["phase"] == "retired" and st["rotation"]["forced"] is True
    await rot.stop()


# ====================================================================== copies and derived tokens
async def test_copies_during_a_rotation_reencrypt_what_the_old_key_sealed(settings, store, tmp_path):
    settings.secret_key = f"{NEW.decode()},{OLD.decode()}"
    store.kv_set(keys.KV, json.dumps({"current": keys.key_id(OLD)}))
    ring = keys.load(settings, store)
    blobs = keys.Resealing(LocalStore(tmp_path / "ses"), ring)
    await blobs.put("a/1/p.enc", Fernet(OLD).encrypt(b"old part"))
    await blobs.put("a/1/q.enc", Fernet(NEW).encrypt(b"new part"))
    assert await blobs.copy("a/1/p.enc", "a/2/p.enc") and await blobs.copy("a/1/q.enc", "a/2/q.enc")
    assert _readable(await blobs.get("a/2/p.enc"), NEW) and Fernet(NEW).decrypt(await blobs.get("a/2/p.enc")) == \
        b"old part"
    assert await blobs.get("a/2/q.enc") == await blobs.get("a/1/q.enc")  # already new: copied as it is
    assert not await blobs.copy("a/1/none.enc", "a/2/none.enc")
    ring.rotation = None  # no rotation: a plain copy
    await blobs.copy("a/1/p.enc", "a/3/p.enc")
    assert await blobs.get("a/3/p.enc") == await blobs.get("a/1/p.enc")


def test_derived_tokens_from_before_a_rotation_still_check_until_it_is_retired(settings, store):
    settings.secret_key = OLD.decode()
    keys.load(settings, store)
    link = computer.media_token(settings, "ses-1", 1, "shot.png", time.time() + 60)
    settings.secret_key = f"{NEW.decode()},{OLD.decode()}"
    keys._RINGS.clear()
    ring = keys.load(settings, store)
    assert computer.read_media_token(settings, link) == ("ses-1", 1, "shot.png")
    fresh = computer.media_token(settings, "ses-1", 1, "shot.png", time.time() + 60)
    assert fresh != link and computer.read_media_token(settings, fresh)[0] == "ses-1"
    ring.retire(keys.key_id(OLD))
    with pytest.raises(KeyError):
        computer.read_media_token(settings, link)
    assert computer.read_media_token(settings, fresh)[0] == "ses-1"


# ====================================================================== backups apart from the checkpoints
def _gcs(status: int) -> GCSStore:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"items": []} if status == 200 else {"error": "no"})

    return GCSStore("gs://backups-bucket", transport=httpx.MockTransport(handler), token="t")


@pytest.mark.parametrize("session_store,backup_store,list_status,want", [
    ("gs://ses", None, None, "warn"),  # beside the checkpoints, under the same account
    ("gs://ses", "gs://ses", None, "warn"),
    ("gs://ses", "gs://backups-bucket", 403, "ok"),  # a bucket of their own, write-only for hangar
    ("gs://ses", "gs://backups-bucket", 200, "warn"),  # of their own, but hangar's account reads it
    (None, None, None, None),  # no bucket: PD snapshots are the advice (the backups check says so)
])
async def test_the_doctor_warns_when_backups_share_the_checkpoints_bucket_and_account(
        settings, session_store, backup_store, list_status, want):
    settings.session_store, settings.backup_store = session_store, backup_store
    blobs = _gcs(list_status) if list_status else LocalStore(settings.data_dir / "b")
    c = await backup.isolation_check(SimpleNamespace(blobs=blobs), settings)
    assert (c["status"] if c else None) == want, c
    if want == "warn":
        assert "HANGAR_BACKUP_STORE" in c["fix"] and "objectCreator" in c["fix"]


async def test_a_write_only_backup_bucket_still_takes_backups(settings, store, monkeypatch):
    settings.backup_store, settings.backup_keep, settings.backup_hours = "gs://backups-bucket", 2, 24
    puts: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST":
            puts.append(req.url.params["name"])
            return httpx.Response(200, json={})
        return httpx.Response(403, json={"error": "objectCreator only"})

    blobs = GCSStore("gs://backups-bucket", transport=httpx.MockTransport(handler), token="t")
    assert backup.backup_store(settings, LocalStore(settings.data_dir / "s")) is not None
    b = backup.Backups(settings, Vault(store, NEW), blobs)
    out = await b.run()
    assert puts and out["pruned"] == []
    c = await backup.doctor_check(b, settings, True)
    assert c["status"] == "warn" and "write-only" in c["detail"]


# ====================================================================== over REST, in the app
def test_rotate_key_over_rest(settings):
    from fastapi.testclient import TestClient

    from hangar.app import create_app

    keys._RINGS.clear()
    settings.admin_token = "t0ken"
    app = create_app(settings)
    auth = {"Authorization": "Bearer t0ken"}
    with TestClient(app) as c:
        h = app.state.hangar
        h.vault.put("cred:x", "value")
        assert c.get("/api/admin/rotate-key", headers=auth).json()["rotation"] is None
        assert c.post("/api/admin/rotate-key/retire", headers=auth).status_code == 409  # nothing to retire
        r = c.post("/api/admin/rotate-key", headers=auth)
        assert r.status_code == 200 and r.json()["rotation"]["sealed_rows"] == 1
        for _ in range(250):
            st = c.get("/api/admin/rotate-key", headers=auth).json()
            if st["rotation"]["phase"] == "retired":
                break
            time.sleep(0.02)
        assert st["rotation"]["phase"] == "retired" and [k["retired"] for k in st["keys"]] == [False, True]
        assert h.vault.get_text("cred:x") == "value"
        assert c.post("/api/admin/rotate-key", headers={"Authorization": "Bearer nope"}).status_code == 401
    keys._RINGS.clear()


# ====================================================================== review
async def test_a_backup_from_before_the_rotation_can_still_be_restored_after_it(settings, store, tmp_path):
    """--- review: the pass re-encrypts a backup's outer layer, but the database inside still holds the vault sealed
    with the old key. Retiring dropped that key from the key file, so restoring any backup made before the
    rotation brought back a vault nothing could decrypt."""
    (settings.data_dir / keys.FILE).write_bytes(OLD)
    ring = keys.load(settings, store)
    Vault(store, ring).put("cred:gh", "ghp_secret")
    from hangar.backup import snapshot

    snapshot(settings.db_path, tmp_path / "before-rotation.db")  # what a backup holds, inside its encryption
    rot = _rotation(settings, store, ring, LocalStore(tmp_path / "ses"))
    await rot.start("sean")
    await _until(rot, "retired")
    await rot.stop()
    new = ring.primary
    assert (settings.data_dir / keys.FILE).read_bytes().split() == [new]  # the previous image reads one key

    keys._RINGS.clear()  # the backup restored, and hangar started on it
    restored = Store(tmp_path / "before-rotation.db")
    ring2 = keys.load(settings, restored)
    assert Vault(restored, ring2).get_text("cred:gh") == "ghp_secret"
    assert ring2.primary == new and ring2.rotation["phase"] == "pending"  # and it moves to the new key again
    keys._RINGS.clear()
    assert keys.load(settings, store).active == [new]  # the live database: retired stays retired


async def test_the_pass_waits_while_the_old_key_is_missing(settings, store, tmp_path):
    """--- review: with the old key gone from HANGAR_SECRET_KEY midway (removed too early, a typo), the pass
    counted what it sealed as unreadable, finished, and retired the key: putting it back no longer helped."""
    settings.secret_key = OLD.decode()
    keys.load(settings, store)
    blobs = LocalStore(tmp_path / "ses")
    await blobs.put("ses-1/c/repo.bundle.enc", Fernet(OLD).encrypt(b"work"))
    settings.secret_key = f"{NEW.decode()},{OLD.decode()}"
    keys._RINGS.clear()
    rot = _rotation(settings, store, keys.load(settings, store), blobs)
    rot.resume = lambda: None  # stopped right after the re-seal: the pass starts after the next restart
    await rot.start("sean")
    settings.secret_key = NEW.decode()  # restarted without the old key
    keys._RINGS.clear()
    rot = _rotation(settings, store, keys.load(settings, store), blobs)
    rot.resume()
    await asyncio.sleep(0.3)
    r = rot.ring.rotation
    assert r["phase"] in ("sealed", "passing") and keys.key_id(OLD) in r["error"]
    await rot.stop()
    settings.secret_key = f"{NEW.decode()},{OLD.decode()}"  # put back
    keys._RINGS.clear()
    rot = _rotation(settings, store, keys.load(settings, store), blobs)
    rot.resume()
    await _until(rot, "retired")
    assert Fernet(NEW).decrypt(await blobs.get("ses-1/c/repo.bundle.enc")) == b"work"
    await rot.stop()


def test_the_first_start_with_two_keys_finds_the_one_that_sealed_the_vault(settings, store):
    """--- review: upgrading with a new key already first recorded the new key as the one that sealed what is
    stored: no rotation was pending, and the old key looked unneeded."""
    Vault(store, OLD).put("cred:gh", "ghp_secret")  # the release before: one key
    settings.secret_key = f"{NEW.decode()},{OLD.decode()}"
    ring = keys.load(settings, store)
    assert store.kv_get_json(keys.KV)["current"] == keys.key_id(OLD)
    assert ring.rotation["phase"] == "pending" and ring.rotation["old"] == keys.key_id(OLD)
