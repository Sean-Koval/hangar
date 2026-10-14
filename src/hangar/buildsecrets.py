"""Build secrets (--- egress): credentials for the image-build step only, never the running sandbox.

A private package registry token, say, is needed to build an environment and nowhere after. Where it can be named
is limited to places only admins change, because a build runs code from the repo and has a network:

* an environment's `build_secrets: {NPM_TOKEN: npm-ci-token}` (secret id -> vault credential); setting it needs an
  admin, like `secrets:`;
* a repo policy's `build_secrets` (`PUT /api/repos/policy`, admin), which wins for that repo.

A repo's own environment file (the envbuilds workstream's `.hangar/environment.yaml`) may only list the *ids* it
needs (`build_secrets: [NPM_TOKEN]`); an id missing from the mapping fails the build and says which. So whoever
can push to a repo can ask for a build secret by id but can't point an id at another credential.

How a build gets them (the build side is envbuilds'):

* A Dockerfile build: `with docker_args(values) as args: docker_build(ctx, tag, secret_args=args)`, and the
  Dockerfile uses `RUN --mount=type=secret,id=NPM_TOKEN[,env=NPM_TOKEN] ...`. BuildKit mounts the value for that
  one instruction and doesn't write it to a layer. Each value is a 0600 file in a 0700 temp directory, gone when the
  `with` block ends; values never appear on a command line.
* Cloud Build: hangar's vault isn't Secret Manager, so `cloud_build_refusal` says to build that image with Docker.
* An install step run in a build sandbox: the values go into that one exec's environment, never `~/.hangar/env`,
  and `leak_scan` checks before the result is captured that the step didn't write one to disk (`npm config set`
  writes the token to `.npmrc`).

A credential used this way should be marked control-plane only (`hangar cred exposure NAME control_plane`) so no
environment can also map it into a running sandbox. Signing keys, bot tokens and GitHub App keys are refused here as
everywhere. A build secret reaches the code the build runs, which can send it wherever the build's network allows:
give builds tokens that can only read the registry they're for.
"""

from __future__ import annotations

import contextlib
import os
import re
import shlex
import shutil
import tempfile
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any

import hangar.identity as identity

if TYPE_CHECKING:
    from hangar.broker import Broker
    from hangar.envspec import EnvSpec
    from hangar.models import Sandbox
    from hangar.providers.base import Provider

ID_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")


class BuildSecretError(RuntimeError):
    """A build secret that can't be given; the message names the id, never a value."""


def check_ids(mapping: dict[str, str]) -> dict[str, str]:
    """Validate a `build_secrets` mapping's ids (used by EnvSpec and RepoPolicy validators)."""
    for k in mapping:
        if not ID_RE.match(k):
            raise ValueError(f"invalid build secret id '{k}': letters, digits and '_', not starting with a digit")
    return mapping


def mapping(spec: EnvSpec | None, policy: Any = None) -> dict[str, str]:
    """id -> credential for a build of `spec` for a repo with `policy` (a RepoPolicy or None); the repo's wins."""
    out = dict(getattr(spec, "build_secrets", None) or {})
    out.update(getattr(policy, "build_secrets", None) or {})
    return out


def resolve(broker: Broker, mapping_: dict[str, str], ids: list[str] | None = None) -> dict[str, bytes]:
    """The values for `ids` (default: every id in the mapping). Raises BuildSecretError naming the first id that is
    unknown, maps to a missing credential, or to a signing key, bot token or GitHub App key."""
    want = list(mapping_) if ids is None else list(dict.fromkeys(ids))
    out: dict[str, bytes] = {}
    for sid in want:
        name = mapping_.get(sid)
        if name is None:
            raise BuildSecretError(f"build secret {sid} isn't configured: an admin maps it to a credential in the "
                                   "environment's or the repo policy's build_secrets")
        try:
            cred = broker.get(name)
        except Exception as e:  # CredentialError: unknown credential
            raise BuildSecretError(f"build secret {sid}: {e}") from None
        if identity.locked(cred, broker.settings):
            raise BuildSecretError(f"build secret {sid}: credential '{name}' is a signing key, bot token or GitHub "
                                   "App key, which never leaves the control plane")
        value = broker.vault.get(f"cred:{name}")
        if value is None:
            raise BuildSecretError(f"build secret {sid}: credential '{name}' has no stored value")
        out[sid] = value.strip()
    return out


@contextlib.contextmanager
def docker_args(values: dict[str, bytes]) -> Iterator[list[str]]:
    """`--secret id=X,src=FILE` arguments for `docker build`, with each value in a 0600 file under a 0700 temp
    directory that is removed when the block ends (even on error)."""
    if not values:
        yield []
        return
    d = tempfile.mkdtemp(prefix="hangar-bs-")
    try:
        os.chmod(d, 0o700)
        args: list[str] = []
        for sid, v in values.items():
            if not ID_RE.match(sid):
                raise BuildSecretError(f"invalid build secret id '{sid}'")
            p = os.path.join(d, sid)
            fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(v)
            args += ["--secret", f"id={sid},src={p}"]
        yield args
    finally:
        shutil.rmtree(d, ignore_errors=True)


def cloud_build_refusal(mapping_: dict[str, str]) -> str | None:
    """Why a Cloud Build can't take these secrets, or None when there are none."""
    if not mapping_:
        return None
    return (f"build secrets ({', '.join(sorted(mapping_))}) need a Docker build: Cloud Build can only read Secret "
            "Manager, and hangar's vault isn't that. Build this image with --builder docker")


async def leak_scan(prov: Provider, sb: Sandbox, values: dict[str, bytes], paths: list[str] | None = None,
                    home: str = "/home/agent", workspace: str = "/workspace") -> list[str]:
    """Files under `paths` (default: the workspace and $HOME) that contain a build secret's value, as
    "<path>: <secret id>", never the value. The values go to the sandbox in a 0600 file that is removed again;
    grep reads patterns from it, so no value is on a command line."""
    if not values:
        return []
    paths = paths or [workspace, home]
    pat = f"/tmp/hangar-bs-scan-{sb.id}"
    found: list[str] = []
    try:
        for sid, v in values.items():
            # A multi-line value (a key file) can't be one grep pattern: look for its longest line.
            needle = max(v.strip().splitlines() or [b""], key=len).strip()
            if len(needle) < 8:
                continue
            await prov.write_file(sb, pat, needle + b"\n", mode=0o600)
            res = await prov.exec(sb, f"grep -rlIF --exclude={shlex.quote(os.path.basename(pat))} -f {shlex.quote(pat)}"
                                      f" -- {' '.join(shlex.quote(p) for p in paths)} 2>/dev/null | head -n 50",
                                  timeout=300)
            found += [f"{ln}: {sid}" for ln in res.stdout.splitlines() if ln.strip()]
    finally:
        with contextlib.suppress(Exception):
            await prov.exec(sb, f"rm -f {shlex.quote(pat)}", timeout=30)
    return found
