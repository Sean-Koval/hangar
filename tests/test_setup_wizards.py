"""`hangar setup github|slack` and `hangar doctor --fix`, against fake GitHub and Slack APIs."""

import json

import httpx
import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

import hangar.cli as cli
from hangar.app import create_app
from hangar.github import verify_signature
from hangar.models import Credential, CredentialKind
from hangar.setup_api import HOOK_EVENTS, unreachable
from tests.conftest import FakeProvider

runner = CliRunner()
TOKEN = "github_pat_" + "x" * 40


class FakeGitHub:
    """Repos, their webhooks and deliveries. A ping is "delivered" by checking its signature with the secret the
    hook was registered with against the secret in hangar's vault (what hangar's route does)."""

    def __init__(self, vault_secret, can_hooks=True, readable=("acme/app",), read_only_hooks=()):
        self.vault_secret = vault_secret  # () -> the webhook secret hangar holds
        self.can_hooks, self.readable = can_hooks, set(readable)
        self.read_only_hooks = set(read_only_hooks)  # repos where the token has Webhooks: Read only
        self.hooks: dict[str, list[dict]] = {}
        self.deliveries: dict[int, list[dict]] = {}
        self.calls: list[tuple[str, str]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == f"Bearer {TOKEN}"
        m, path = request.method, request.url.path
        self.calls.append((m, path))
        parts = path.strip("/").split("/")
        if len(parts) < 3 or parts[0] != "repos" or f"{parts[1]}/{parts[2]}" not in self.readable:
            return httpx.Response(404, json={"message": "Not Found"})
        repo, rest = f"{parts[1]}/{parts[2]}", parts[3:]
        if not rest and m == "GET":
            return httpx.Response(200, json={"full_name": repo, "default_branch": "main",
                                             "permissions": {"admin": False, "push": True, "pull": True}})
        if rest[:1] == ["hooks"] and not self.can_hooks:
            return httpx.Response(403, json={"message": "Resource not accessible by personal access token"})
        if rest[:1] == ["hooks"] and repo in self.read_only_hooks and m in ("POST", "PATCH") and rest[2:] != ["pings"]:
            return httpx.Response(403, json={"message": "Resource not accessible by personal access token"})
        hooks = self.hooks.setdefault(repo, [])
        if rest == ["hooks"] and m == "GET":
            return httpx.Response(200, json=[{**h, "config": {**h["config"], "secret": "********"}} for h in hooks])
        if rest == ["hooks"] and m == "POST":
            body = json.loads(request.content)
            if any(h["config"]["url"] == body["config"]["url"] for h in hooks):
                return httpx.Response(422, json={"message": "Hook already exists on this repository"})
            h = {"id": 100 + len(hooks), "active": body.get("active", True), "events": body["events"],
                 "config": body["config"]}
            hooks.append(h)
            return httpx.Response(201, json=h)
        hook = next((h for h in hooks if len(rest) >= 2 and str(h["id"]) == rest[1]), None)
        if hook is None:
            return httpx.Response(404, json={"message": "Not Found"})
        if len(rest) == 2 and m == "PATCH":
            body = json.loads(request.content)
            hook.update({k: v for k, v in body.items() if k in ("active", "events", "config")})
            return httpx.Response(200, json=hook)
        if rest[2:] == ["pings"] and m == "POST":
            ok = hook["config"].get("secret") == self.vault_secret()
            ds = self.deliveries.setdefault(hook["id"], [])
            ds.insert(0, {"id": 900 + len(ds), "event": "ping", "status_code": 202 if ok else 401})
            return httpx.Response(204)
        if rest[2:] == ["deliveries"] and m == "GET":
            return httpx.Response(200, json=self.deliveries.get(hook["id"], []))
        return httpx.Response(404, json={"message": "Not Found"})


class _Keep:
    def __init__(self, c):
        self.c = c

    def __enter__(self):
        return self.c

    def __exit__(self, *a):
        return False


@pytest.fixture
def client(settings, monkeypatch):
    settings.admin_token = "t0ken"
    settings.public_url = "https://hangar.acme.dev"
    app = create_app(settings)
    with TestClient(app) as c:
        app.state.hangar.fleet.providers["docker"] = FakeProvider()
        c.headers["Authorization"] = "Bearer t0ken"
        monkeypatch.setattr(cli, "_client", lambda: _Keep(c))
        yield c


def _gh(client, **kw) -> FakeGitHub:
    h = client.app.state.hangar

    def secret():
        try:
            return h.broker.secret_text("gh-webhook")
        except Exception:  # noqa: BLE001
            return None

    gh = FakeGitHub(secret, **kw)
    h.jobs.transport = httpx.MockTransport(gh)
    return gh


def _setup(client, **body):
    return client.post("/api/setup/github", json={"token": TOKEN, "repos": {"acme/app": "plain"}, **body}).json()


def test_unreachable():
    assert unreachable("http://localhost:8787")
    assert unreachable("http://10.1.2.3/x") and unreachable("http://hangar.hangar.svc:8787")
    assert unreachable("https://34.8.98.95.sslip.io") is None and unreachable("https://8.8.8.8") is None


def test_setup_github_end_to_end(client):
    h = client.app.state.hangar
    gh = _gh(client)
    res = _setup(client)
    assert res["ok"], res
    steps = {s["step"]: s for s in res["steps"]}
    assert steps["local delivery"]["status"] == "ok"
    assert steps["webhook: acme/app"]["detail"].startswith("created https://hangar.acme.dev/api/hooks/github")
    assert steps["test delivery: acme/app"]["status"] == "ok" and "202" in steps["test delivery: acme/app"]["detail"]
    # credentials in the vault, settings stored and live, the secret never returned
    assert h.broker.secret_text("gh-control") == TOKEN
    secret = h.broker.secret_text("gh-webhook")
    assert len(secret) == 64 and secret not in json.dumps(res) and TOKEN not in json.dumps(res)
    assert h.settings.github_repos == {"acme/app": "plain"} and h.settings.github_token_credential == "gh-control"
    assert client.get("/api/settings/github_repos").json()["source"] == "hangar config"
    [hook] = gh.hooks["acme/app"]
    assert hook["events"] == list(HOOK_EVENTS) and hook["config"]["content_type"] == "json"
    assert hook["config"]["secret"] == secret and hook["config"]["url"] == "https://hangar.acme.dev/api/hooks/github"
    # the webhook route is live with the stored secret
    body = b'{"zen": "x"}'
    import hashlib
    import hmac

    sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    assert verify_signature(secret, body, sig)
    r = client.post("/api/hooks/github", content=body, headers={"x-github-event": "ping", "x-hub-signature-256": sig})
    assert r.status_code == 202
    # running it again keeps the secret and updates the same hook (idempotent)
    res = _setup(client)
    assert res["ok"] and h.broker.secret_text("gh-webhook") == secret and len(gh.hooks["acme/app"]) == 1
    assert {s["step"]: s for s in res["steps"]}["webhook: acme/app"]["detail"].startswith("updated")
    audit = json.dumps(client.get("/api/audit").json())
    assert secret not in audit and TOKEN not in audit


def test_push_only_hook_is_repaired_and_secret_aligned(client):
    """The owner's real failure: a hand-made webhook that only sent push events (and a secret nobody remembered)."""
    gh = _gh(client)
    gh.hooks["acme/app"] = [{"id": 7, "active": True, "events": ["push"],
                             "config": {"url": "https://hangar.acme.dev/api/hooks/github",
                                        "content_type": "form", "secret": "old"}}]
    res = _setup(client)
    assert res["ok"], res
    [hook] = gh.hooks["acme/app"]
    assert set(hook["events"]) == {"push", *HOOK_EVENTS} and hook["config"]["content_type"] == "json"
    assert hook["config"]["secret"] == client.app.state.hangar.broker.secret_text("gh-webhook")


def test_token_problems_store_nothing(client):
    h = client.app.state.hangar
    _gh(client, can_hooks=False)
    res = _setup(client)
    assert not res["ok"] and "Webhooks: Read and write" in res["steps"][0]["detail"]
    assert h.store.get(Credential, "gh-control") is None and h.store.get(Credential, "gh-webhook") is None
    assert h.settings.github_repos == {}
    _gh(client, readable=())
    res = _setup(client)
    assert not res["ok"] and "can't read acme/app (HTTP 404)" in res["steps"][0]["detail"]
    assert "personal-access-tokens" in res["steps"][0]["detail"]  # what token to make
    res = client.post("/api/setup/github", json={"token": TOKEN, "repos": {"acme/app": "nope"}}).json()
    assert not res["ok"] and "unknown environment nope" in res["steps"][0]["detail"]
    res = client.post("/api/setup/github", json={"repos": {"acme/app": "plain"}}).json()
    assert not res["ok"] and "isn't stored" in res["steps"][0]["detail"]


def test_without_webhook_permission_no_webhook_mode_works(client):
    """Adding the webhook by hand needs a secret the user knows: theirs, never one hangar made up and hid."""
    h = client.app.state.hangar
    _gh(client, can_hooks=False)
    res = _setup(client, register_webhook=False)
    assert not res["ok"] and "--webhook-secret-file" in json.dumps(res)
    assert h.store.get(Credential, "gh-webhook") is None
    res = _setup(client, register_webhook=False, webhook_secret="my-own-secret-0123456789")
    assert res["ok"], res
    assert h.broker.secret_text("gh-webhook") == "my-own-secret-0123456789"
    assert "my-own-secret" not in json.dumps(res)
    assert {s["step"]: s for s in res["steps"]}["webhook"]["status"] == "warn"
    # rotating to a secret nobody can see, with no webhook to give it to, would break every delivery
    res = _setup(client, register_webhook=False, rotate_secret=True)
    assert not res["ok"] and h.broker.secret_text("gh-webhook") == "my-own-secret-0123456789"


def test_rotation_that_github_refuses_keeps_the_working_secret(client):
    """Webhooks: Read passes the token check, but the PATCH that sets the new secret is refused: the vault must keep
    the secret GitHub still signs with, or every delivery fails."""
    h = client.app.state.hangar
    gh = _gh(client)
    assert _setup(client)["ok"]
    old = h.broker.secret_text("gh-webhook")
    gh.read_only_hooks.add("acme/app")
    res = _setup(client, rotate_secret=True)
    assert not res["ok"]
    assert h.broker.secret_text("gh-webhook") == old == gh.hooks["acme/app"][0]["config"]["secret"]
    assert "kept" in json.dumps(res)
    # several repos, one refused: the ones already moved to the new secret are put back
    gh.read_only_hooks.clear()
    gh.readable.add("acme/lib")
    assert _setup(client, repos={"acme/lib": "plain"})["ok"]
    gh.read_only_hooks.add("acme/lib")
    res = _setup(client, repos={"acme/app": "plain", "acme/lib": "plain"}, rotate_secret=True)
    assert not res["ok"] and h.broker.secret_text("gh-webhook") == old
    assert gh.hooks["acme/app"][0]["config"]["secret"] == old
    # and a rotation GitHub accepts moves every hook and the vault together; the pings prove it
    gh.read_only_hooks.clear()
    res = _setup(client, repos={"acme/app": "plain", "acme/lib": "plain"}, rotate_secret=True)
    assert res["ok"], res
    new = h.broker.secret_text("gh-webhook")
    assert new != old and {gh.hooks[r][0]["config"]["secret"] for r in ("acme/app", "acme/lib")} == {new}
    assert {s["step"]: s for s in res["steps"]}["test delivery: acme/lib"]["status"] == "ok"


def test_repair_keeps_the_hooks_tls_choice(client):
    """doctor --fix only adds what's missing: a hook set to skip TLS verification (self-signed hangar) stays so."""
    gh = _gh(client)
    assert _setup(client)["ok"]
    hook = gh.hooks["acme/app"][0]
    hook["config"]["insecure_ssl"] = "1"
    hook["events"] = ["push"]
    assert client.post("/api/setup/github/webhook", json={"repo": "acme/app"}).json()["ok"]
    assert hook["config"]["insecure_ssl"] == "1" and set(hook["events"]) == {"push", *HOOK_EVENTS}
    # a token that can only read hooks: the fix fails, says why, and changes nothing
    hook["events"] = ["push"]
    gh.read_only_hooks.add("acme/app")
    r = runner.invoke(cli.app, ["doctor", "--fix", "--yes"])
    assert "Webhooks: Read and write" in r.output and r.exit_code == 1, r.output
    assert hook["events"] == ["push"]


def test_laptop_skips_webhook_but_wires_everything_else(client):
    h = client.app.state.hangar
    h.settings.public_url = "http://localhost:8787"
    gh = _gh(client)
    res = _setup(client)
    steps = {s["step"]: s for s in res["steps"]}
    assert res["ok"] and steps["webhook"]["status"] == "warn" and "--webhook-url" in steps["webhook"]["detail"]
    assert steps["local delivery"]["status"] == "ok" and not gh.hooks.get("acme/app")
    # a tunnel URL registers the hook there, and doctor checks that URL from then on
    res = _setup(client, webhook_url="https://abc.trycloudflare.com")
    assert res["ok"], res
    assert gh.hooks["acme/app"][0]["config"]["url"] == "https://abc.trycloudflare.com/api/hooks/github"
    checks = {c["name"]: c for c in client.get("/api/doctor").json()}
    assert checks["github webhook acme/app"]["status"] == "ok"


def test_pinned_environment_settings_are_flagged(client):
    h = client.app.state.hangar
    h.config.pinned.add("github_repos")
    _gh(client)
    res = _setup(client)
    assert any(s["status"] == "warn" and "HANGAR_GITHUB_REPOS" in s["detail"] and "hangar-site" in s["detail"]
               for s in res["steps"])


def test_doctor_finds_and_fixes_a_broken_webhook(client):
    gh = _gh(client)
    assert _setup(client)["ok"]
    gh.hooks["acme/app"][0]["events"] = ["push"]
    checks = {c["name"]: c for c in client.get("/api/doctor").json()}
    c = checks["github webhook acme/app"]
    assert c["status"] == "fail" and "doesn't send issues" in c["detail"]
    assert c["action"] == {"id": "github_webhook", "repo": "acme/app"}
    r = runner.invoke(cli.app, ["doctor", "--fix", "--yes"])
    assert "✓ test delivery: acme/app" in r.output, r.output
    assert set(gh.hooks["acme/app"][0]["events"]) >= set(HOOK_EVENTS)
    checks = {c["name"]: c for c in client.get("/api/doctor").json()}
    assert checks["github webhook acme/app"]["status"] == "ok"
    gh.hooks["acme/app"].clear()
    assert {c["name"]: c for c in client.get("/api/doctor").json()}["github webhook acme/app"]["detail"] \
        .startswith("no webhook")
    res = client.post("/api/setup/github/webhook", json={"repo": "acme/app"}).json()
    assert res["ok"] and gh.hooks["acme/app"]
    assert not client.post("/api/setup/github/webhook", json={"repo": "evil/repo"}).json()["ok"]


def test_doctor_fix_builds_and_prints_the_rest(client, monkeypatch):
    h = client.app.state.hangar
    h.config.set("github_repos", '{"acme/app": "plain"}')
    h.broker.put("m", "claude-code", CredentialKind.API_KEY, "sk-ant-xxxxxxxxxxxx")
    h.fleet.catalog.add({"name": "built-later", "extends": "plain", "credentials": ["m"]},
                        save_dir=h.settings.saved_env_dir)
    h.config.set("github_repos", '{"acme/app": "built-later"}')
    built = []
    monkeypatch.setattr(cli, "env_build", lambda name, provider=None, force=False: built.append(name))
    r = runner.invoke(cli.app, ["doctor", "--fix", "--yes"])
    assert built == ["built-later"], r.output
    assert "github token (not automatic): hangar setup github" in r.output


def test_cli_setup_github_never_echoes_the_token(client):
    gh = _gh(client)
    r = runner.invoke(cli.app, ["setup", "github", "--repo", "acme/app=plain", "--token-stdin", "--non-interactive"],
                      input=TOKEN + "\n")
    assert r.exit_code == 0, r.output
    assert TOKEN not in r.output and "✓ test delivery: acme/app" in r.output and gh.hooks["acme/app"]
    r = runner.invoke(cli.app, ["setup", "github", "--repo", "acme/app", "--non-interactive"])
    assert r.exit_code == 2 and "=python" in r.output


# ---------------------------------------------------------------------------- slack
def _slack(client, ok=True):
    calls = []

    def route(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.url.path == "/api/auth.test"
        if not ok:
            return httpx.Response(200, json={"ok": False, "error": "invalid_auth"})
        return httpx.Response(200, json={"ok": True, "user": "hangar", "team": "Acme", "team_id": "T1"})

    client.app.state.hangar.jobs.transport = httpx.MockTransport(route)
    return calls


def test_setup_slack(client):
    h = client.app.state.hangar
    h.config.set("github_repos", '{"acme/app": "plain"}')
    _slack(client)
    body = {"signing_secret": "s" * 32, "bot_token": "xoxb-1-2-abc", "channels": {"C0123ABCD": "acme/app"},
            "users": ["U0456EFGH"]}
    res = client.post("/api/setup/slack", json=body).json()
    assert res["ok"], res
    assert {s["step"]: s for s in res["steps"]}["local delivery"]["status"] == "ok"
    assert res["urls"]["events"] == "https://hangar.acme.dev/api/hooks/slack/events"
    assert h.settings.slack_channels == {"C0123ABCD": "acme/app"} and h.settings.slack_users == ["U0456EFGH"]
    assert h.broker.secret_text("slack-bot") == "xoxb-1-2-abc" and "xoxb-1-2-abc" not in json.dumps(res)
    # bad inputs store nothing new
    _slack(client, ok=False)
    res = client.post("/api/setup/slack", json={**body, "bot_token": "xoxb-wrong"}).json()
    assert not res["ok"] and "invalid_auth" in json.dumps(res) and h.broker.secret_text("slack-bot") == "xoxb-1-2-abc"
    res = client.post("/api/setup/slack", json={**body, "channels": {"C0999XYZ": "other/repo"}}).json()
    assert not res["ok"] and "hangar setup github --repo other/repo" in res["steps"][0]["detail"]
    res = client.post("/api/setup/slack", json={**body, "channels": {"general": "acme/app"}}).json()
    assert not res["ok"] and "isn't a channel id" in res["steps"][0]["detail"]


def test_cli_setup_slack_with_files_and_manifest(client, tmp_path):
    client.app.state.hangar.config.set("github_repos", '{"acme/app": "plain"}')
    _slack(client)
    (tmp_path / "sig").write_text("s" * 32)
    (tmp_path / "bot").write_text("xoxb-1-2-abc\n")
    man = tmp_path / "manifest.yaml"
    r = runner.invoke(cli.app, ["setup", "slack", "--channel", "C0123ABCD=acme/app", "--signing-secret-file",
                                str(tmp_path / "sig"), "--bot-token-file", str(tmp_path / "bot"), "--manifest",
                                str(man), "--non-interactive"])
    assert r.exit_code == 0, r.output
    assert "xoxb" not in r.output and "https://hangar.acme.dev/api/hooks/slack/events" in man.read_text()
    assert "hangar.acme.dev" in r.output


def test_cli_manual_webhook_with_own_secret(client, tmp_path):
    h = client.app.state.hangar
    _gh(client, can_hooks=False)
    (tmp_path / "tok").write_text(TOKEN)
    (tmp_path / "sec").write_text("s3cret-" + "a" * 20 + "\n")
    args = ["setup", "github", "--repo", "acme/app=plain", "--token-file", str(tmp_path / "tok"), "--no-webhook",
            "--non-interactive"]
    r = runner.invoke(cli.app, args)
    assert r.exit_code == 1 and "--webhook-secret-file" in r.output
    r = runner.invoke(cli.app, [*args, "--webhook-secret-file", str(tmp_path / "sec")])
    assert r.exit_code == 0, r.output
    assert "s3cret" not in r.output and h.broker.secret_text("gh-webhook") == "s3cret-" + "a" * 20
