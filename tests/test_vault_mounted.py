"""Credentials read from mounted files (--- company-deploy, vault.py): External Secrets syncs a Kubernetes Secret,
the Deployment mounts it at HANGAR_SECRETS_DIR, and a credential stores a reference to its own file there. The value
is read on every use, never stored, never logged, and only the credential's own file can be referenced."""

from __future__ import annotations

import logging
import os

import pytest

import hangar.identity as identity
from hangar.broker import Broker
from hangar.gateway import parse_app_credential
from hangar.models import CredentialKind
from hangar.store import Store
from hangar.vault import Vault, VaultReferenceError, load_or_create_key, reference

VALUE = "sk-mounted-0123456789abcdef"


@pytest.fixture
def mounted(tmp_path):
    d = tmp_path / "hangar-secrets"
    d.mkdir()
    return d


@pytest.fixture
def vault(tmp_path, mounted):
    store = Store(tmp_path / "state.db")
    return Vault(store, load_or_create_key(None, tmp_path), mounted=mounted)


def kube_secret(d, files: dict[str, str], gen: str = "..2026_10_19_00_00_00.1") -> None:
    """Lay files out as the kubelet does: <key> -> ..data/<key>, ..data -> the current generation."""
    (d / gen).mkdir()
    for k, v in files.items():
        (d / gen / k).write_text(v)
    tmp = d / "..data_tmp"
    tmp.symlink_to(gen)
    os.replace(tmp, d / "..data")
    for k in files:
        if not (d / k).is_symlink():
            (d / k).symlink_to(f"..data/{k}")


def test_a_reference_reads_the_file_each_time(vault, mounted):
    kube_secret(mounted, {"api": VALUE})
    vault.put("cred:api", reference(f"file:{mounted}/api"))
    assert vault.get("cred:api") == VALUE.encode()
    assert vault.source("cred:api") == f"file:{mounted}/api"
    kube_secret(mounted, {"api": "sk-rotated-0123456789"}, gen="..2026_10_19_01_00_00.2")  # ESO refreshed it
    assert vault.get("cred:api") == b"sk-rotated-0123456789"
    # the database holds the reference, not the value
    assert VALUE.encode() not in vault.decrypt(vault._store.get_secret("cred:api"))


def test_a_stored_value_still_works_and_replaces_a_reference(vault, mounted):
    vault.put("cred:plain", "pasted-0123456789")
    assert vault.get("cred:plain") == b"pasted-0123456789" and vault.source("cred:plain") == "vault"
    (mounted / "plain").write_text(VALUE)
    vault.put("cred:plain", reference(f"file:{mounted}/plain"))
    assert vault.get("cred:plain") == VALUE.encode()
    vault.put("cred:plain", "pasted-again-0123456789")
    assert vault.source("cred:plain") == "vault"
    assert vault.source("cred:none") is None


@pytest.mark.parametrize("source, why", [
    ("file:/etc/passwd", "must be"),
    ("file:{d}/../api", "must be"),
    ("file:{d}/sub/api", "must be"),
    ("file:relative/api", "must be"),
    ("file:{d}/other", "named after the credential"),
    ("env:API_KEY", "a source is file:"),
])
def test_only_the_credentials_own_file_can_be_referenced(vault, mounted, source, why):
    with pytest.raises(VaultReferenceError, match=why):
        vault.put("cred:api", reference(source.format(d=mounted)))
    assert vault.get("cred:api") is None


def test_only_credentials_hold_references(vault, mounted):
    with pytest.raises(VaultReferenceError, match="only credentials"):
        vault.put("a2a-push:x", reference(f"file:{mounted}/x"))


def test_missing_escaping_or_huge_files_have_no_value(vault, mounted, tmp_path, caplog):
    vault.put("cred:later", reference(f"file:{mounted}/later"))  # ESO hasn't synced yet: allowed, no value
    outside = tmp_path / "outside"
    outside.write_text(VALUE)
    (mounted / "escape").symlink_to(outside)
    vault.put("cred:escape", reference(f"file:{mounted}/escape"))
    (mounted / "huge").write_bytes(b"x" * ((1 << 20) + 1))
    vault.put("cred:huge", reference(f"file:{mounted}/huge"))
    with caplog.at_level(logging.WARNING, logger="hangar.vault"):
        assert vault.get("cred:later") is None
        assert vault.get("cred:escape") is None
        assert vault.get("cred:huge") is None
    assert "not mounted" in caplog.text and "outside" in caplog.text and "over" in caplog.text
    assert VALUE not in caplog.text


def test_a_moved_secrets_directory_reads_as_no_value(tmp_path, mounted):
    store = Store(tmp_path / "state.db")
    key = load_or_create_key(None, tmp_path)
    (mounted / "api").write_text(VALUE)
    Vault(store, key, mounted=mounted).put("cred:api", reference(f"file:{mounted}/api"))
    assert Vault(store, key, mounted=tmp_path / "elsewhere").get("cred:api") is None


def test_the_default_directory_comes_from_the_environment(tmp_path, mounted, monkeypatch):
    monkeypatch.setenv("HANGAR_SECRETS_DIR", str(mounted))
    v = Vault(Store(tmp_path / "state.db"), load_or_create_key(None, tmp_path))
    assert v.mounted_dir == mounted
    monkeypatch.delenv("HANGAR_SECRETS_DIR")
    assert str(v.mounted_dir) == "/var/run/hangar-secrets"


def test_the_broker_uses_and_redacts_a_mounted_credential(vault, mounted):
    kube_secret(mounted, {"anthropic": VALUE})
    broker = Broker(vault._store, vault)
    c = broker.put("anthropic", "claude-code", CredentialKind.API_KEY, reference(f"file:{mounted}/anthropic"),
                   reference_ok=True)
    assert VALUE not in c.model_dump_json()
    assert broker.secret_text("anthropic") == VALUE
    assert VALUE not in broker.redact(f"the key is {VALUE}")


def test_only_an_admin_path_stores_a_reference(vault, mounted):
    """The broker refuses a reference unless the caller allows it (the API passes reference_ok for admins only)."""
    kube_secret(mounted, {"anthropic": VALUE})
    broker = Broker(vault._store, vault)
    with pytest.raises(identity.Forbidden, match="only an admin"):
        broker.put("anthropic", "claude-code", CredentialKind.API_KEY, reference(f"file:{mounted}/anthropic"))
    assert vault.get("cred:anthropic") is None


def test_a_github_app_key_can_come_from_a_mounted_file(vault, mounted):
    """The App JSON is checked when used, not when the reference is stored (ESO may sync it later)."""
    import json

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    broker = Broker(vault._store, vault)
    c = broker.put("gh-app", "git", CredentialKind.GITHUB_APP, reference(f"file:{mounted}/gh-app"), reference_ok=True)
    assert c.kind == CredentialKind.GITHUB_APP and vault.source("cred:gh-app") == f"file:{mounted}/gh-app"
    pem = rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption()).decode()
    kube_secret(mounted, {"gh-app": json.dumps({"app_id": "99", "installation_id": 7, "private_key": pem})})
    assert parse_app_credential(broker.secret_text("gh-app")).app_id == "99"
    # a stored (non-reference) App key is still checked when it is stored
    from hangar.broker import CredentialError

    with pytest.raises(CredentialError):
        broker.put("gh-app-2", "git", CredentialKind.GITHUB_APP, "nope")
