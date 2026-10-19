# Egress allowlists

What a sandbox can reach on the internet, by hostname, and the proxy that enforces it. Part of the [security model](README.md); the Docker network is on [docker-network.md](docker-network.md) and the read-only registries on [package-mirrors.md](package-mirrors.md).

An environment says what its sandboxes may reach on the internet, by hostname:

```yaml
name: api
egress:
  mode: allowlist            # open | allowlist | none
  presets: [pypi, npm]       # default [packages]: pypi, npm, crates and go
  allow: [docs.python.org, "*.internal.example.com", "registry.example.com:8443"]
```

| Mode | Network | What gets through |
| --- | --- | --- |
| `open` (`HANGAR_EGRESS_DEFAULT`'s default for people's own work; never for an [intake job](front-doors.md#intake-posture) under `enforce`) | the public internet directly, as before | everything public |
| `allowlist` | only the git gateway and hangar's egress proxy (no DNS) | the harness's own hosts (its model API and login refresh), the environment's remote MCP servers, the presets, `allow`, and hosts an owner approved |
| `none` | the same | the harness's own hosts, the environment's MCP servers and approved hosts only |

A rule is a host (`pypi.org`, ports 443 and 80), a host and port, or `*.domain` (any name below it, not the domain itself). There is no `*` rule: that is mode `open`.

- **Presets** (`egress.PRESETS`): `pypi`, `npm` and `go` are served by hangar's [read-only package mirror](package-mirrors.md) when the git gateway is configured, and their registries' own hosts stay closed. Without the gateway they open pypi.org and files.pythonhosted.org, registry.npmjs.org, and proxy.golang.org and sum.golang.org, and the sandbox carries a warning that those hosts also take uploads. `crates` opens index.crates.io and static.crates.io, the read-only sparse index and download host; not crates.io, the API that takes `cargo publish`. `packages` is those four. `github` (github.com, api.github.com, codeload.github.com, objects/release-assets/raw.githubusercontent.com) is no longer part of `packages`: GitHub takes writes with anyone's token, and gateway-mode sandboxes reach their repo through the [gateway](git-gateway.md) instead. Name it on its own for an environment that needs it.
- **The harness's hosts** are always allowed in a proxied mode, since an agent can't work without its model: Claude Code's are `api.anthropic.com`, `claude.ai` and `platform.claude.com` ([Claude Code's network requirements](https://code.claude.com/docs/en/network-config#network-access-requirements)); Codex's `api.openai.com`, `chatgpt.com` and `auth.openai.com`; Gemini and Antigravity's `generativelanguage.googleapis.com`, `cloudcode-pa.googleapis.com` and `oauth2.googleapis.com`. A pi router not among them goes in `allow`.
- **Per repo.** A repo policy's `egress` (`PUT /api/repos/policy`, admin) adds presets and hosts for that repo's sandboxes and can tighten the mode (`open` < `allowlist` < `none`). It can't widen it. A repo policy that asks for `allowlist` in an `open` environment isn't enforced, because the network decides that; the sandbox carries a warning saying so.
- **Who sets it.** `egress` on an environment needs an admin, like `secrets:` and `image`.
- **Git.** Clones and pushes to GitHub go through the [git gateway](git-gateway.md), which jobs and sessions use by default, and so does reading the repo's API. The gateway is reachable in every mode, so they need no rule. A `credential_mode: direct` environment clones from github.com itself through the proxy, which needs the `github` preset named on its own; in mode `none` its clone fails.
- **Asking for a host.** The agent runs `hangar-agent env request egress <host> --why '…'`, and the proxy's refusal says exactly that. The session's or job's owner, or an admin, approves or denies the request. An approval adds the host to that session's allowlist; an admin can widen it to the whole environment. It applies to the next connection, and is audited as `egress.approve`. Nothing is granted automatically. Admins list approvals with `GET /api/egress/approvals?environment=…` (or `session=…`) and revoke one with `DELETE` and `rule=` (audited as `egress.revoke`). `GET /api/egress/sandboxes/<id>` shows what a sandbox may reach and why.

**How it is enforced.** An environment's mode decides the network its sandboxes start on:

- **GKE.** Pods of a proxied environment carry `dev.hangar/egress=proxied`. `sandbox-network.yaml`'s internet baseline leaves them out. `gateway.yaml`'s `sandbox-egress-proxied` gives them the proxy on hangar's pods (port 3128, `HANGAR_EGRESS_URL=http://hangar-gateway.hangar.svc:3128`), and `allow-hangar-gateway` gives them the git gateway. They get no DNS: kube-dns resolves any name by asking the internet, so a sandbox that may query it can send data out in the names it looks up. Their pods' resolver is loopback (`dnsPolicy: None`), and hangar's own Service names (the proxy, the git gateway, an in-cluster `HANGAR_OTEL_SANDBOX_ENDPOINT`) are in `/etc/hosts`, resolved by the control plane when it writes the template. Tools hand the proxy a host name, so they need no resolver of their own. Their SandboxTemplate carries the same rules as its `networkPolicy`, so Agent Sandbox applies that policy instead of its default one, which allows the public internet. The template stays `Managed`.
- **Docker.** Their containers go on `hangar-egress` (bridge `hangar1`), an *internal* network: Docker gives it no route out, but its gateway address reaches the host. hangar listens there with the proxy and relays the git gateway's port; `host.docker.internal` points at that address in those containers. DNS on an internal network answers container names only, from Docker 26.0.0 (and 25.0.4, 23.0.11); older engines forward it to the host's resolver (CVE-2024-29018), so hangar refuses to start a proxied sandbox on them. This needs hangar on the Docker host (Linux). Docker Desktop's VM has that address and the Mac doesn't, so a proxied sandbox fails to start there with a message that says so; keep `open` for local work. A host firewall that drops input (ufw's default) must accept TCP 3128 and 8788 from `hangar1`.
- **E2B and other providers** can't keep a sandbox off the internet, so a proxied environment fails to start there rather than run unenforced.

**The proxy** is part of the control plane, next to the git gateway (`egress_proxy.py`). It is an HTTP CONNECT proxy that never terminates TLS. For each connection it:

1. authenticates the sandbox. HTTPS_PROXY carries `<sandbox id>:<HMAC(vault key, sandbox id)>`; the value is never stored and is refused once the sandbox isn't running. 30 bad credentials a minute from one address are refused without a check;
2. checks host and port against the sandbox's policy: its environment, its repo's policy and its session's approvals, cached for 5 seconds;
3. resolves the name itself and connects only to public addresses, to the address it checked. A name that resolves into the VPC, the cluster, loopback or the metadata server is refused. `HANGAR_EGRESS_PRIVATE_HOSTS` lists internal mirrors that may resolve to private addresses, but never to loopback or link-local ones;
4. reads the client's first bytes before relaying any of them: a whole TLS ClientHello, or a plain HTTP request head, which must carry the host in `Host`. Anything else is cut off. The hello is parsed strictly (`egress_tls.py`):
   - exactly one `server_name` extension holding one `host_name` (RFC 6066 allows no more), and it must be the CONNECT host. A hello without SNI passes only when the CONNECT target is an IP address;
   - no Encrypted ClientHello: a hello carrying `encrypted_client_hello` (0xfe0d, [RFC 9849](https://www.rfc-editor.org/rfc/rfc9849.html) §5), `ech_outer_extensions` (0xfd00, §5.1) or draft ESNI's `encrypted_server_name` (0xffce) is refused, since its SNI in clear names the front end, not the site. GREASE ECH (§6.2) looks the same and is refused too, so hangar's computer image turns ECH off in Chromium (the `EncryptedClientHelloEnabled` policy, and `--disable-features=EncryptedClientHello` where hangar launches it). curl, Python, Node and Go don't send ECH unless asked;
   - nothing that hides or blurs the name: SSL 2.0-style and SSL 3.0 hellos (no extensions), an extension sent twice ([RFC 8446](https://www.rfc-editor.org/rfc/rfc8446.html) §4.2 forbids it, and a server might read the other copy), a hello whose extensions don't parse, and a second handshake message in the first flight. A hello split over several records is reassembled (up to 64 KB);
   - after a HelloRetryRequest the client sends a second hello in clear; it gets the same checks before it goes upstream, so a first hello without ECH can't be followed by one with it.

   Absolute-form `http://` requests are forwarded one per connection, without `Proxy-Authorization`;
5. relays bytes both ways until a side closes or nothing moves for 15 minutes. A sandbox has at most `HANGAR_EGRESS_MAX_CONNECTIONS_PER_SANDBOX` (128) tunnels open, and all of one person's sandboxes together at most `HANGAR_EGRESS_MAX_CONNECTIONS_PER_PERSON` (640), so six people at their caps fit the proxy's 4096. A tunnel whose client stops reading holds at most about 320 KB of the proxy's memory. On shutdown the proxy takes no new connection from the signal on, and open tunnels (a model's stream mid-answer) get `HANGAR_EGRESS_DRAIN_SECONDS` (20, inside the pod's 45-second grace) to finish.

Why there and not a separate proxy: the decision needs the sandbox, its person, its session's approvals and the audit log, all of which live in the control plane. A connection costs one SQLite read by primary key, and relaying is socket copying with 64 KB reads. An off-the-shelf proxy (Smokescreen, Squid) would need per-sandbox roles, a way to receive approvals live, and its logs mapped back to people. A Rust proxy in `envd/`'s workspace would need the same policy over an API. The relay is the part to move out if a profile ever shows it costing the control plane.

**What is recorded.** Every denied connection is an `egress.deny` audit entry with the sandbox, person, environment, session, job, host, port and reason. A sandbox gets at most 30 a minute; the next entry records how many were dropped (`suppressed_before`). It also appears in the events of the sandbox's session. Allowed connections are summed per sandbox and host into `egress.allow` entries every minute: connections, bytes out and in, and the rule that allowed them. Metrics: `hangar_egress_connections{decision,reason,mode}`, `hangar_egress_bytes{direction}`, `hangar_egress_open_connections`.

Remote MCP servers count toward the allowlist only when someone who may widen egress configured them: in an environment that sets `egress`, or one an admin owns; a member's environment that adds or repoints one gets a warning instead. Tools that ignore `HTTPS_PROXY` (some JVM builds, raw sockets) simply fail in a proxied environment.

## What egress doesn't stop

The allowlist narrows where data can go and records who sent how much where. It doesn't make an allowed host safe:

- **Anything sent to an allowed host.** A URL or a body to any allowed host can carry data. That includes the harness's own model API with a key the attacker brings (it is always allowed), hosts in `allow`, approved hosts, the environment's MCP servers, and the cloud token exchange. Prefer exact hosts to wildcards: `*.googleapis.com` includes `storage.googleapis.com`, where anyone can write to their own bucket.
- **Another site on an allowed host's front end.** Without TLS interception the proxy pins the address and checks the SNI, but can't see inside TLS. A `Host` header or HTTP/2 `:authority` that names another customer of the same CDN reaches that site wherever the CDN doesn't check it against the SNI (domain fronting). Where the major CDNs stand (checked 2026-10-14):

  | CDN | Fronting between customers |
  | --- | --- |
  | Google | Blocked since April 2018 ([The Register](https://www.theregister.com/2018/04/19/google_domain_fronting/)). |
  | Amazon CloudFront | Blocked since April 2018: the certificate's account must own the request's distribution, else 421 ([AWS](https://aws.amazon.com/blogs/security/enhanced-domain-protections-for-amazon-cloudfront-requests)). |
  | Cloudflare | SNI and `Host` must match, else 421 ([Cloudflare](https://developers.cloudflare.com/support/troubleshooting/http-status-codes/4xx-client-error/error-421/)). It does coalesce HTTP/2 and HTTP/3 connections across hostnames one certificate covers. ECH would have hidden the real name: it is refused now. |
  | Azure Front Door, Azure CDN (classic) | Blocked unless both names are in the same Azure subscription, 421 otherwise: new resources from November 2022, enabled globally from 8 January 2024 ([Microsoft](https://learn.microsoft.com/en-us/answers/questions/1461316/take-action-to-stop-domain-fronting)). |
  | Fastly | Announced a block from 27 February 2024 ([Risky Business](https://www.risky.biz/fastly-to-block-domain-fronting-in-2024/)), yet a March 2025 test still fronted through it ([Compass Security](https://blog.compass-security.com/2025/03/bypassing-web-filters-part-3-domain-fronting/)). Treat it as open. |
  | Akamai | Found susceptible, with 21 other CDNs of 30, in a 2023 measurement ([arXiv:2310.17851](https://arxiv.org/abs/2310.17851)). Treat it as open. |

  So an allowed host on Fastly or Akamai can reach any other site there. PyPI is on Fastly: with the package mirror on, sandboxes don't reach PyPI's hosts at all, but without the gateway (the `pypi` preset opens them) they do. Look up where a host in `allow` is served before adding it. TLS 1.2 renegotiation sends a new hello encrypted, out of the proxy's sight; the large CDNs don't accept client-initiated renegotiation.
- **Low-bandwidth channels through what is allowed:** which packages and versions a sandbox downloads shows in a registry's public download statistics, and the mirror's cache hides repeats but not first fetches.
- **`github` named as a preset** (direct-mode environments) is GitHub with whatever token the agent has or brings.

