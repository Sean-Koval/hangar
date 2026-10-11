from hangar.auth import TokenAuth, bearer_from_headers


def test_tokens(store):
    auth = TokenAuth(store, admin_token="admin-secret")
    assert auth.verify("admin-secret") == "admin"
    raw, tok = auth.create("codex-laptop")
    assert raw.startswith("hgr_") and raw not in tok.model_dump_json()
    assert auth.verify(raw) == "codex-laptop"
    assert auth.verify("nope") is None and auth.verify(None) is None
    auth.revoke(tok.id)
    assert auth.verify(raw) is None


def test_bootstrap_only_when_empty(store):
    auth = TokenAuth(store)
    first = auth.ensure_bootstrap()
    assert first and auth.verify(first) == "bootstrap-admin"
    assert auth.ensure_bootstrap() is None
    assert TokenAuth(store, admin_token="x").ensure_bootstrap() is None


def test_bearer_parsing():
    assert bearer_from_headers({"authorization": "Bearer abc"}) == "abc"
    assert bearer_from_headers({"Authorization": "bearer  xyz "}) == "xyz"
    assert bearer_from_headers({}) is None
