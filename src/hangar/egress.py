"""Hostname egress control (spec 0003 §5.5, --- egress): what a sandbox may reach on the internet, by name.

Three modes, set per environment (`egress:`) and tightened per repo (the repo policy's `egress:`):

* `open`: today's baseline. The network policy lets the sandbox reach the public internet directly (never private
  ranges or the metadata server); nothing is proxied.
* `allowlist`: the sandbox reaches nothing but the git gateway and hangar's egress proxy (and no DNS that recurses:
  k8s_pod_dns). The proxy lets a connection through when its host matches the allowlist: the harness's own API
  hosts, the presets (package registries by default) and the hosts listed, plus hosts an owner approved for the
  session or environment.
* `none`: the same network, with nothing on the list but the harness's API hosts (the agent has to reach its model)
  and approved requests.

The mode an environment's sandboxes get decides their *network*: `allowlist` and `none` sandboxes are placed where
only the proxy is reachable (a NetworkPolicy on GKE, an internal network on Docker). A repo policy can only tighten
the mode its environment gives (`open` < `allowlist` < `none`) and add hosts; when it asks for a proxied mode in an
`open` environment, the network stays open and the sandbox says its policy is not enforced.

The proxy (egress_proxy.py) authenticates each connection with a per-sandbox token, HMAC(vault key, sandbox id),
never stored: it works only while its sandbox runs. It does not terminate TLS; it checks the hostname in CONNECT and
the TLS SNI (or the HTTP Host) against it, and connects only to public addresses the name resolves to.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import ipaddress
import json
import logging
import re
import shlex
from typing import TYPE_CHECKING, Any, Literal, NamedTuple
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

from hangar.models import Sandbox, now

if TYPE_CHECKING:
    from hangar.config import Settings
    from hangar.envspec import EnvSpec
    from hangar.fleet import Fleet
    from hangar.providers.base import Provider
    from hangar.store import Store

log = logging.getLogger("hangar.egress")

Mode = Literal["open", "allowlist", "none"]
MODES: tuple[str, ...] = ("open", "allowlist", "none")
_STRICTNESS = {"open": 0, "allowlist": 1, "none": 2}
DEFAULT_PORTS = (443, 80)  # a rule without a port: HTTPS, and plain HTTP for mirrors that still serve it
MAX_RULES = 200

# Hosts per registry, from each registry's own documentation (checked 2026-10-14): PyPI's simple index and file host
# (docs.pypi.org/api), npm's registry, crates.io's sparse index, API (download redirects) and static files
# (doc.rust-lang.org/cargo/reference/registry-index.html), the Go module proxy and checksum database (go.dev/ref/mod),
# and GitHub's web, API, archive and release/raw content hosts (docs.github.com "About GitHub's IP addresses").
PRESETS: dict[str, tuple[str, ...]] = {
    "pypi": ("pypi.org", "files.pythonhosted.org"),
    "npm": ("registry.npmjs.org",),
    # --- egress2: not crates.io itself, the API that takes `cargo publish`; a build needs the sparse index and the
    # download host only (index.crates.io/config.json names them).
    "crates": ("index.crates.io", "static.crates.io"),
    "go": ("proxy.golang.org", "sum.golang.org"),
    "github": ("github.com", "api.github.com", "codeload.github.com", "objects.githubusercontent.com",
               "release-assets.githubusercontent.com", "raw.githubusercontent.com"),
    # --- heavy: Docker Hub for a heavy sandbox's dockerd (docs.docker.com/desktop/setup/allow-list, checked
    # 2026-10-15: the registry, its token service and its blob CDN, under both names the CDN has had). The registry
    # also takes pushes with anyone's token: prefer hangar's read-only mirror (HANGAR_HEAVY_MIRROR, regmirror.py).
    "docker": ("registry-1.docker.io", "auth.docker.io", "production.cloudfront.docker.com",
               "production.cloudflare.docker.com"),
}
# --- egress2: `packages` no longer includes `github`: GitHub takes writes with anyone's token, and sandboxes in
# gateway mode reach it through hangar's git gateway (and its read-only API path) instead.
PACKAGES = ("pypi", "npm", "crates", "go")
PRESETS["packages"] = tuple(h for p in PACKAGES for h in PRESETS[p])
DEFAULT_PRESETS = ("packages",)
# Presets served by hangar's read-only package mirror when it is on (pkgmirror.py): their registries' hosts, which
# also take uploads, aren't opened.
MIRRORED = ("pypi", "npm", "go")

# What each harness needs to reach its model and refresh its login, always allowed in a proxied mode (an agent that
# can't reach its model is no agent). Claude Code: code.claude.com/docs/en/network-config ("Network access
# requirements": api.anthropic.com, claude.ai, platform.claude.com for OAuth refresh). The others from their CLIs'
# documented endpoints; a provider not listed here (pi with another router) goes in `allow`.
_ANTHROPIC = ("api.anthropic.com", "claude.ai", "platform.claude.com")
_OPENAI = ("api.openai.com", "chatgpt.com", "auth.openai.com")
_GOOGLE = ("generativelanguage.googleapis.com", "cloudcode-pa.googleapis.com", "oauth2.googleapis.com")
HARNESS_HOSTS: dict[str, tuple[str, ...]] = {
    "claude-code": _ANTHROPIC,
    "codex": _OPENAI,
    "gemini": _GOOGLE,
    "antigravity": _GOOGLE,
    "pi": (*_ANTHROPIC, *_OPENAI, *_GOOGLE, "openrouter.ai"),
}

_LABEL = r"[a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9_])?"
_HOST_RE = re.compile(rf"^(?:{_LABEL}\.)*{_LABEL}$")
KV_ENV = "egress-allow:env:"
KV_SESSION = "egress-allow:session:"
TOKEN_USER_RE = re.compile(r"^sb-[A-Za-z0-9]{4,40}$")


class EgressError(ValueError):
    """An egress setting that can't be used; the message says why."""


# ============================================================================ hosts and rules
def normalize_host(host: str) -> str:
    """A hostname as the proxy compares it: lower-case, no trailing dot, IPv6 without brackets. Raises EgressError
    for anything that isn't a hostname or an IP address (a scheme, a path, a wildcard, non-ASCII)."""
    h = (host or "").strip().lower().rstrip(".")
    if h.startswith("[") and h.endswith("]"):
        h = h[1:-1]
    try:
        return str(ipaddress.ip_address(h))
    except ValueError:
        pass
    if not h or len(h) > 253 or not _HOST_RE.match(h) or h.isdigit():
        raise EgressError(f"'{host}' is not a hostname")
    return h


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


class Rule(NamedTuple):
    host: str  # normalized; for a wildcard, the suffix without "*."
    wildcard: bool  # "*.example.com": any name below example.com, not example.com itself
    port: int | None  # None: 443 and 80
    source: str  # where it came from: "harness", "preset:pypi", "environment", "repo", "approved:rq-…"

    @property
    def text(self) -> str:
        host = f"[{self.host}]" if ":" in self.host and self.port else self.host
        return ("*." if self.wildcard else "") + host + (f":{self.port}" if self.port else "")

    def matches(self, host: str, port: int) -> bool:
        if (port not in DEFAULT_PORTS) if self.port is None else port != self.port:
            return False
        if self.wildcard:
            return host.endswith("." + self.host)
        return host == self.host


def parse_rule(text: str, source: str) -> Rule:
    """`host`, `host:port` or `*.domain[:port]`. No scheme, path, bare `*` (that is mode `open`) or `*` inside a
    name: a rule is meant to be read and reviewed."""
    t = (text or "").strip().lower()
    if not t:
        raise EgressError("an empty egress rule")
    if "/" in t or "@" in t:
        raise EgressError(f"'{text}': give a host (example.com, *.example.com, example.com:8443), not a URL")
    port = None
    m = re.match(r"^(\[[0-9a-f:.]+\]|[^:]+)(?::(\d{1,5}))?$", t)
    if not m:
        raise EgressError(f"'{text}' is not a host or host:port")
    t, p = m.group(1), m.group(2)
    if p is not None:
        port = int(p)
        if not 0 < port < 65536:
            raise EgressError(f"'{text}': port out of range")
    wildcard = t.startswith("*.")
    if wildcard:
        t = t[2:]
    if "*" in t:
        raise EgressError(f"'{text}': only a leading '*.' is allowed")
    host = normalize_host(t)
    if wildcard:
        if _is_ip(host):
            raise EgressError(f"'{text}': a wildcard needs a domain")
        if "." not in host:
            raise EgressError(f"'{text}': '*.{host}' would match a whole top-level domain")
    return Rule(host, wildcard, port, source)


class EgressSpec(BaseModel):
    """`egress:` on an environment or a repo policy."""

    model_config = ConfigDict(extra="forbid")
    mode: Mode | None = None  # unset: the environment's, then HANGAR_EGRESS_DEFAULT
    presets: list[str] | None = None  # unset: ["packages"] in allowlist mode; [] for none
    allow: list[str] = Field(default_factory=list, max_length=MAX_RULES)

    @field_validator("presets")
    @classmethod
    def _known_presets(cls, v: list[str] | None) -> list[str] | None:
        for p in v or []:
            if p not in PRESETS:
                raise ValueError(f"unknown egress preset '{p}' (presets: {', '.join(sorted(PRESETS))})")
        return v

    @field_validator("allow")
    @classmethod
    def _valid_rules(cls, v: list[str]) -> list[str]:
        for r in v:
            try:
                parse_rule(r, "check")
            except EgressError as e:
                raise ValueError(str(e)) from None
        return v


# ============================================================================ the effective policy
class Policy(NamedTuple):
    mode: str
    rules: tuple[Rule, ...]
    proxied: bool  # the sandbox's network admits only the proxy (its environment's mode isn't open)
    notes: tuple[str, ...] = ()
    mirrors: tuple[str, ...] = ()  # --- egress2: ecosystems it reads through hangar's package mirror

    @property
    def enforced(self) -> bool:
        return self.proxied or self.mode == "open"

    def allows(self, host: str, port: int) -> Rule | None:
        """The rule that lets host:port through, or None. Open lets everything through."""
        if self.mode == "open":
            return Rule("*", True, None, "open")
        return next((r for r in self.rules if r.matches(host, port)), None)

    def view(self) -> dict[str, Any]:
        return {"mode": self.mode, "enforced": self.enforced, "allow": [r.text for r in self.rules],
                "mirrors": list(self.mirrors), "notes": list(self.notes)}


def env_mode(settings: Settings, spec: EnvSpec) -> str:
    """The environment's mode: its `egress.mode`, else HANGAR_EGRESS_DEFAULT. Decides the network class."""
    e = getattr(spec, "egress", None)
    return (e.mode if e is not None and e.mode else None) or settings.egress_default


def proxied(settings: Settings, spec: EnvSpec) -> bool:
    """Whether the environment's sandboxes go on the proxy-only network."""
    return env_mode(settings, spec) != "open"


def _presets_for(e: EgressSpec | None, mode: str) -> list[str]:
    if e is not None and e.presets is not None:
        return list(e.presets)
    return list(DEFAULT_PRESETS) if mode == "allowlist" else []


def resolve(settings: Settings, spec: EnvSpec, repo: EgressSpec | None = None, harness: str | None = None,
            approved: list[dict[str, Any]] | None = None,
            mcp_trusted: set[tuple[str, int | None]] | None = None, sealed: bool = False,
            mirror: bool = False, setup_hosts: tuple[str, ...] = ()) -> Policy:
    """The policy for a sandbox of `spec`, under its repo's policy, running `harness`, with approved requests.
    `mcp_trusted` (from `trusted_mcp`) limits which of the spec's remote MCP servers count; None takes them all,
    for a spec the caller vouches for.

    --- sealed (#126): `sealed` is a grader's (delivery.sealed): mode `none` with nothing at all on the list, not
    even the harness's hosts (`none` normally keeps them: an agent must reach its model, and a grader runs no agent),
    the MCP servers' or the cloud's, and no approval, whatever the environment and the repo policy say. Always on the
    proxy-only network. `mirror` (`Grader.network: mirror`) adds hangar's read-only package mirror when it is on.
    `setup_hosts`: the repo's git host while the sandbox is set up without the git gateway, before any of the code it
    grades runs (for_sandbox; delivery.lock_sealed ends it)."""
    if sealed:
        from hangar.pkgmirror import available

        notes: list[str] = []
        mirrors: tuple[str, ...] = ()
        if mirror and available(settings):
            mirrors = MIRRORED
        elif mirror:
            notes.append("network: mirror asks for hangar's read-only package mirror, which is off "
                         "(HANGAR_GATEWAY_URL and HANGAR_PACKAGE_MIRROR): this sandbox reaches nothing")
        rules = []
        for h in setup_hosts:
            with contextlib.suppress(EgressError):
                rules.append(Rule(normalize_host(h), False, None, "sealed-setup"))
        return Policy("none", tuple(rules), True, tuple(notes), mirrors)
    env = getattr(spec, "egress", None)
    base = env_mode(settings, spec)
    mode, notes = base, []
    if repo is not None and repo.mode:
        if _STRICTNESS[repo.mode] > _STRICTNESS[base]:
            mode = repo.mode
        elif repo.mode != base:
            notes.append(f"the repo policy asks for egress {repo.mode}, but environment {spec.name} is {base}; a repo "
                         "policy can only tighten it")
    is_proxied = base != "open"
    if mode != "open" and not is_proxied:
        notes.append(f"egress {mode} is not enforced: environment {spec.name}'s network is open (set egress.mode on "
                     "the environment)")
    rules: list[Rule] = []
    mirrors: list[str] = []
    if mode != "open":
        rules += [Rule(h, False, None, "harness") for h in HARNESS_HOSTS.get(harness or "", ())]
        for key, r in _mcp_rules(spec):  # the environment's remote MCP servers, where an admin configured them
            if mcp_trusted is None or (r.host, r.port) in mcp_trusted:
                rules.append(r)
            else:
                notes.append(f"MCP server '{key}' ({r.text}) isn't on the egress allowlist: it comes from an "
                             "environment that neither sets egress nor belongs to an admin, and only an admin widens "
                             f"egress (egress.allow, or approve `{request_hint(r.host, r.port or 443)}`)")
        from hangar.cloudcreds import egress_hosts  # its cloud's token exchange (cloudcreds.py)

        rules += [Rule(h, False, None, "cloud") for h in egress_hosts(getattr(spec, "cloud", None))]
        if mode == "allowlist":  # none: the harness's hosts and approved requests only
            presets = _presets_for(env, mode)
            presets += [p for p in (repo.presets or [] if repo else []) if p not in presets]
            # --- egress2: pypi, npm and go through hangar's read-only mirror when it is on (pkgmirror.py)
            from hangar.pkgmirror import available

            mirror = available(settings) and is_proxied
            public: list[str] = []
            for p in presets:
                for part in PACKAGES if p == "packages" else (p,):
                    if part in MIRRORED and mirror:
                        mirrors += [part] if part not in mirrors else []
                        continue
                    rules += [Rule(h, False, None, f"preset:{p}") for h in PRESETS[part]]
                    public += [part] if part in MIRRORED and part not in public else []
            if public and is_proxied:
                notes.append(f"{', '.join(public)}: reached at the public registries, whose hosts also take uploads "
                             "(hangar's read-only package mirror needs HANGAR_GATEWAY_URL and HANGAR_PACKAGE_MIRROR)")
            rules += [parse_rule(r, "environment") for r in (env.allow if env else [])]
            rules += [parse_rule(r, "repo") for r in (repo.allow if repo else [])]
        for a in approved or []:
            try:
                rules.append(parse_rule(a["rule"], f"approved:{a.get('request') or a.get('by') or '?'}"))
            except (EgressError, KeyError, TypeError):
                continue
    seen: set[tuple[str, bool, int | None]] = set()
    out = []
    for r in rules:
        if (k := (r.host, r.wildcard, r.port)) not in seen:
            seen.add(k)
            out.append(r)
    return Policy(mode, tuple(out[:MAX_RULES * 4]), is_proxied, tuple(notes), tuple(mirrors))


def _mcp_rule(url: Any) -> Rule | None:
    if not isinstance(url, str) or not url:
        return None
    try:
        u = urlsplit(url)
        if u.hostname and "$" not in u.netloc:
            default = 443 if u.scheme == "https" else 80
            return Rule(normalize_host(u.hostname), False, None if (u.port or default) == default else u.port, "mcp")
    except (ValueError, EgressError):
        pass
    return None


def _mcp_rules(spec: EnvSpec) -> list[tuple[str, Rule]]:
    out = []
    for key, srv in (getattr(spec, "mcp", None) or {}).items():
        if (r := _mcp_rule(getattr(srv, "url", None))) is not None:
            out.append((key, r))
    return out


def trusted_mcp(fleet: Fleet, environment: str) -> set[tuple[str, int | None]]:
    """The remote MCP servers (host, port) that count toward an environment's allowlist: those written by someone who
    may widen egress. `egress:` needs an admin and `mcp:` doesn't, so a member's environment that extends a locked one
    could otherwise add any host as an MCP server (or repoint the parent's). An environment in the chain counts when
    it sets `egress` itself (an admin wrote it), when its recorded owner is an admin, or when it has no owner and
    ships with hangar's environment files rather than the API's save directory. Each is judged on its own: a member's
    edit to an environment an admin's extends doesn't count."""
    from hangar import people

    cat, out, seen = fleet.catalog, set(), set()
    name: str | None = environment
    while name and name not in seen and name in cat.names():
        seen.add(name)
        data, src = cat._raw[name]
        owner = fleet.store.kv_get(f"identity:env-owner:{name}")
        if owner:
            p = people.get(fleet.store, owner)
            ok = p is not None and p.role == "admin"
        else:
            ok = cat.save_dir is None or src != cat.save_dir
        if ok or (isinstance(data.get("egress"), dict) and data["egress"]):
            for srv in (data.get("mcp") or {}).values():
                if isinstance(srv, dict) and (r := _mcp_rule(srv.get("url"))) is not None:
                    out.add((r.host, r.port))
        name = data.get("extends")
    return out


def repo_egress(store: Store, repo: str | None) -> EgressSpec | None:
    """The repo policy's `egress:` for a sandbox's repo, if any."""
    if not repo:
        return None
    from hangar.repopolicy import RepoPolicies

    p = RepoPolicies(store).get(repo)
    return getattr(p, "egress", None) if p is not None else None


def for_sandbox(fleet: Fleet, sb: Sandbox, spec: EnvSpec | None = None) -> Policy:
    """The policy in force for a sandbox now: its environment, its repo's policy, approvals for its session and
    environment."""
    spec = spec or fleet.catalog.get(sb.environment)
    from hangar import delivery

    if delivery.sealed(sb):  # --- sealed (#126): nothing of the environment's, the repo's or approvals
        return resolve(fleet.settings, spec, sealed=True, mirror=delivery.sealed_mirror(sb),
                       setup_hosts=delivery.sealed_setup_hosts(sb))
    session = sb.labels.get("hangar.session")
    approved = approvals(fleet.store, environment=sb.environment, session=session)
    return resolve(fleet.settings, spec, repo_egress(fleet.store, sb.repo), sb.info.get("harness"), approved,
                   trusted_mcp(fleet, sb.environment))


# ============================================================================ approvals
def _kv_key(scope: str, key: str) -> str:
    return (KV_ENV if scope == "environment" else KV_SESSION) + key


def approvals(store: Store, environment: str | None = None, session: str | None = None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for scope, key in (("environment", environment), ("session", session)):
        if key:
            raw = store.kv_get(_kv_key(scope, key))
            if raw:
                try:
                    out += [a for a in json.loads(raw) if isinstance(a, dict)]
                except ValueError:
                    log.warning("ignoring unreadable egress approvals for %s %s", scope, key)
    return out


def approve(store: Store, scope: str, key: str, host: str, port: int | None, actor: str,
            request: str | None = None) -> Rule:
    """Add host[:port] to a session's or an environment's allowlist (what an owner's approval does)."""
    if scope not in ("session", "environment"):
        raise EgressError("scope must be session or environment")
    rule = parse_rule(f"{host}:{port}" if port else host, f"approved:{request or actor}")
    cur = approvals(store, **{scope: key})
    if not any(a.get("rule") == rule.text for a in cur):
        if len(cur) >= MAX_RULES:
            raise EgressError(f"the {scope} already has {MAX_RULES} approved hosts")
        cur.append({"rule": rule.text, "by": actor, "at": now(), "request": request})
        store.kv_set(_kv_key(scope, key), json.dumps(cur), durable=True)
    return rule


def revoke(store: Store, scope: str, key: str, rule: str) -> bool:
    cur = approvals(store, **{scope: key})
    left = [a for a in cur if a.get("rule") != rule]
    if len(left) == len(cur):
        return False
    store.kv_set(_kv_key(scope, key), json.dumps(left), durable=True)
    return True


# ============================================================================ the sandbox's credential for the proxy
def token(key: bytes, sandbox_id: str) -> str:
    """The proxy password for a sandbox: never stored, worthless once the sandbox stops running."""
    return hmac.new(key, b"hangar-egress:" + sandbox_id.encode(), hashlib.sha256).hexdigest()


def check_token(key: bytes, sandbox_id: str, password: str) -> bool:
    if not TOKEN_USER_RE.match(sandbox_id or "") or len(password or "") != 64:
        return False
    return hmac.compare_digest(token(key, sandbox_id), password)


def proxy_env(proxy: str, sandbox_id: str, password: str, no_proxy: list[str]) -> dict[str, str]:
    """The variables tools read: both spellings, since libcurl (git, cargo) reads only lower-case http_proxy and
    others only upper-case. Basic auth in the URL is what curl, pip, npm, Go, uv and Claude Code accept."""
    u = urlsplit(proxy)
    url = f"{u.scheme}://{sandbox_id}:{password}@{u.netloc}"
    np = ",".join(dict.fromkeys(["localhost", "127.0.0.1", "::1", *no_proxy]))
    env = {}
    for k in ("HTTPS_PROXY", "HTTP_PROXY"):
        env[k] = env[k.lower()] = url
    env["NO_PROXY"] = env["no_proxy"] = np
    return env


async def attach(fleet: Fleet, prov: Provider, sb: Sandbox, spec: EnvSpec) -> dict[str, str]:
    """Decide a new sandbox's egress and return the env vars that point its tools at the proxy (none when open).
    Called with the other per-sandbox setup, so forks and resumed sessions get it too. Fails closed: a proxied
    environment whose proxy can't be reached from the sandbox doesn't start."""
    pol = for_sandbox(fleet, sb, spec)
    info: dict[str, Any] = {"mode": pol.mode, "enforced": pol.enforced}
    for n in pol.notes:
        sb.info.setdefault("warnings", []).append(n)
    if not pol.proxied:  # open, or tightened by a repo policy in an open environment (the note says so)
        sb.info["egress"] = info
        return {}
    proxy = await _proxy_url(fleet, prov, sb)
    info["proxy"] = urlsplit(proxy).netloc
    from hangar import keys  # --- durable: during a key rotation, a sandbox from before keeps its credential

    password = token(keys.key_for(fleet.settings, sb.created_at), sb.id)
    env = proxy_env(proxy, sb.id, password, _direct_hosts(fleet.settings))
    if pol.mirrors:  # --- egress2: package managers read through hangar's mirror; the environment's own env wins
        from hangar import pkgmirror

        menv, npmrc = pkgmirror.sandbox_env(fleet.settings, pol.mirrors, sb.id, password)
        env.update({k: v for k, v in menv.items() if k not in (getattr(spec, "env", None) or {})})
        await pkgmirror.write_npmrc(prov, sb, f"/home/{fleet.settings.sandbox_user}", npmrc)
        info["mirrors"] = list(pol.mirrors)
    sb.info["egress"] = info
    return env


def _direct_hosts(settings: Settings) -> list[str]:
    """hangar's own services a proxied sandbox reaches directly, not through the proxy: the git gateway and (review)
    an in-cluster OTel collector the harness exports to, which the proxy would refuse as a private address. A
    collector on the internet goes through the proxy like any host (put it in `allow`)."""
    from hangar.observe import sandbox_endpoint

    out = [h for h in [urlsplit(settings.gateway_url).hostname if settings.gateway_url else None] if h]
    otel = sandbox_endpoint(settings)
    if otel and (h := urlsplit(otel).hostname):
        try:
            internal = ipaddress.ip_address(h).is_private
        except ValueError:
            internal = h.endswith((".svc", ".svc.cluster.local", ".cluster.local", ".internal"))
        if internal:
            out.append(h)
    return out


def k8s_pod_dns(settings: Settings) -> dict[str, Any]:
    """The DNS of a proxied environment's pods (review): none, and hangar's own Service names in /etc/hosts.

    kube-dns answers any name by asking the internet, so a sandbox that may query it can send data out in the names
    it looks up (`<secret>.attacker.example`), whatever the proxy allows. A proxied sandbox needs no resolver: its
    tools hand the proxy a host name and the proxy resolves it. What it does look up itself is hangar's proxy, git
    gateway and OTel collector, resolved here, in the control plane, to their Service addresses. The resolver is
    loopback, where nothing answers, so any other lookup fails at once. Blocking: run it in a thread."""
    import socket

    names = [h for u in (settings.egress_url, *_direct_hosts(settings)) if u
             for h in [urlsplit(u).hostname if "://" in u else u] if h and not _is_ip(h)]
    by_ip: dict[str, list[str]] = {}
    for h in dict.fromkeys(names):
        try:
            ip = next(i[4][0] for i in socket.getaddrinfo(h, None, socket.AF_INET, socket.SOCK_STREAM))
        except (OSError, StopIteration) as e:
            raise EgressError(f"can't resolve {h} for proxied sandboxes' /etc/hosts (they have no DNS): {e}") from e
        by_ip.setdefault(ip, []).append(h)
    return {"dnsPolicy": "None", "dnsConfig": {"nameservers": ["127.0.0.1"]},
            "hostAliases": [{"ip": ip, "hostnames": hs} for ip, hs in sorted(by_ip.items())]}


async def _proxy_url(fleet: Fleet, prov: Provider, sb: Sandbox) -> str:
    from hangar.fleet import FleetError

    if not fleet.egress.port:
        raise FleetError("egress allowlist/none needs hangar's egress proxy, and HANGAR_EGRESS_PORT is 0")
    if prov.name == "docker":
        net = getattr(prov, "network", None)
        ip = await net.egress_gateway() if net is not None else None
        if not ip:
            raise FleetError("egress allowlist/none needs hangar's egress network on Docker, and it isn't there "
                             "(HANGAR_DOCKER_NETWORK names a network of your own)")
        try:
            await fleet.egress.listen(ip)
        except OSError as e:
            raise FleetError(
                f"egress allowlist/none: hangar can't listen on the egress network's gateway {ip} ({e.strerror or e}). "
                "This needs hangar on the Docker host itself (Linux); with Docker Desktop, use egress.mode: open for "
                "local work") from e
        return f"http://{ip}:{fleet.egress.port}"
    if prov.name == "kubernetes":
        if not fleet.settings.egress_url:
            raise FleetError("egress allowlist/none needs HANGAR_EGRESS_URL (e.g. "
                             "http://hangar-gateway.hangar.svc:3128) and deploy/k8s/gateway.yaml's egress port "
                             "(docs/security.md)")
        return fleet.settings.egress_url
    raise FleetError(f"egress allowlist/none isn't supported on the {prov.name} provider: its sandboxes can't be kept "
                     "off the internet yet; use an environment with egress.mode: open there")


K8S_LABEL = "dev.hangar/egress"
_HANGAR_PODS = {"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "hangar"}},
                "podSelector": {"matchLabels": {"app": "hangar"}}}


def k8s_template_policy() -> dict[str, Any]:
    """The `networkPolicy` of a proxied environment's SandboxTemplate (Agent Sandbox: the controller applies it as the
    template's shared policy instead of its default, which allows the public internet). Egress: hangar's pods on the
    gateway and proxy ports, and no DNS (k8s_pod_dns); no ingress here (hangar's own policies, envd-network.yaml and
    e2b.yaml, admit hangar's pods). The same as deploy/k8s/gateway.yaml's sandbox-egress-proxied."""
    hangar = {"to": [_HANGAR_PODS], "ports": [{"protocol": "TCP", "port": 8788}, {"protocol": "TCP", "port": 3128}]}
    return {"ingress": [], "egress": [hangar]}


def describe(fleet: Fleet, sb: Sandbox) -> dict[str, Any]:
    """What a sandbox may reach, for people and for the agent (`hangar-agent env doctor`): no secrets."""
    try:
        pol = for_sandbox(fleet, sb)
    except Exception as e:  # noqa: BLE001 - an environment removed since
        return {"mode": "unknown", "error": str(e)}
    out = pol.view()
    if (cur := sb.info.get("egress")) and cur.get("proxy"):
        out["proxy"] = cur["proxy"]
    return out


def mount(app: Any, h: Any, admin: Any) -> None:
    """Admin REST: what a sandbox may reach (GET /api/egress/sandboxes/{id}), the hosts owners approved
    (GET /api/egress/approvals?environment=…|session=…), and revoking one (DELETE, same query plus rule=)."""
    from fastapi import HTTPException, Query

    fleet: Fleet = h.fleet

    @app.get("/api/egress/sandboxes/{ref}")
    async def egress_sandbox(ref: str, _: str = admin):
        try:
            sb = fleet.get(ref)
        except Exception as e:  # noqa: BLE001 - FleetError: unknown sandbox
            raise HTTPException(404, str(e)) from e
        return describe(fleet, sb)

    def _scope(environment: str | None, session: str | None) -> tuple[str, str]:
        if bool(environment) == bool(session):
            raise HTTPException(400, "give one of environment= or session=")
        return ("environment", environment) if environment else ("session", session)  # type: ignore[return-value]

    @app.get("/api/egress/approvals")
    async def egress_approvals(environment: str | None = Query(None), session: str | None = Query(None),
                               _: str = admin):
        scope, key = _scope(environment, session)
        return approvals(fleet.store, **{scope: key})

    @app.delete("/api/egress/approvals")
    async def egress_revoke(rule: str = Query(...), environment: str | None = Query(None),
                            session: str | None = Query(None), who: str = admin):
        scope, key = _scope(environment, session)
        if not revoke(fleet.store, scope, key, rule):
            raise HTTPException(404, f"no approved host '{rule}' for {scope} {key}")
        fleet.audit(who, "egress.revoke", key, scope=scope, rule=rule)
        fleet.egress.invalidate()
        return {"revoked": rule, scope: key}


def request_hint(host: str, port: int) -> str:
    target = host if port in DEFAULT_PORTS else f"{host}:{port}"
    return f"hangar-agent env request egress {shlex.quote(target)} --why '<what you need it for>'"
