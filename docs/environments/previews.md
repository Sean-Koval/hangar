# Preview ports

A declared port (`ports:` in [the repo's own file](repo-file.md)) can be opened by the sandbox's owner in a browser.
Nothing is public, and it never runs on hangar's own origin:

```console
$ hangar sb preview my-sandbox
 5173  web        https://5173-sb-1a2b3c4d.hangar-preview.example.net/__hangar/preview?t=...
```

## How a preview is kept apart

- **One origin per port per sandbox:** `<port>-<sandbox>.<HANGAR_PREVIEW_DOMAIN>`. What a dev server serves (the
  agent's HTML and scripts) can't reach the dashboard's token or another sandbox's preview.
- **Owner-scoped.** `GET /api/sandboxes/{ref}/previews` (the owner's `user` token, or an admin's) gives a link per
  port that works for two minutes, once.
- Opening the link signs that browser in to that origin only, with an HttpOnly cookie (`__Host-` on https). The cookie
  is signed and bound to the sandbox, the port and the person, valid `HANGAR_PREVIEW_SESSION_HOURS` (12).
- Every request checks it again against the sandbox's owner as it is then. `curl -H "Authorization: Bearer $TOKEN"`
  works too, with a `user` or admin token.
- **Deny by default.** Only declared ports, never hangar-envd's, only while the sandbox runs. Links and refusals are
  audited (`preview.link`, `preview.denied`, `preview.port.denied`).
- **Nothing of hangar's reaches the app:** the proxy drops the `Authorization` header and its own cookie. It reaches
  the port at the address the sandbox's hangar-envd proved, never loopback, link-local or another sandbox's. It is the
  same proxy as [E2B's sandbox URLs](../e2b.md). HTTP and WebSockets both work.

## Setting it up

Point wildcard DNS for the domain at hangar. On GKE, also put a wildcard certificate for `*.<domain>` on hangar's
ingress: a preview's host is one label under the domain. Then set, for example,
`HANGAR_PREVIEW_DOMAIN=hangar-preview.example.net`.

On a laptop, `HANGAR_PREVIEW_DOMAIN=localhost` is enough: browsers resolve `*.localhost` to 127.0.0.1. Dev servers must
listen on 0.0.0.0, and the agent is told so. Previews need hangar-envd (Docker and Kubernetes sandboxes). On
Kubernetes they also need the `allow-hangar-sandbox-ports` NetworkPolicy (`deploy/k8s/e2b.yaml`), since sandbox pods
take no other ingress.

**Give previews a registrable domain of their own**, not a subdomain of your company's or of hangar's host. 

The proxy drops hangar's credentials, but it can't tell the app's own cookies from others the browser sends. Under
`preview.example.com`, cookies your SSO or intranet set for `example.com` would reach the agent's app. A domain of its
own (as `githubusercontent.com` is GitHub's) also keeps the agent's pages from being same-site with your other apps.
